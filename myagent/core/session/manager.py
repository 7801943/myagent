"""
SessionManager：多用户会话管理 + 组件构建 + TTL 清理。

不再依赖 AgentFactory，直接构建 ProviderRouter / ToolManager / SafetyGuard 等组件。
每个 Session 独占一套 Harness（per-session），彻底规避竞态问题。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from myagent.core.session.session import Session
from myagent.core.harness import AgentHarness
from myagent.core.events import EventBus
from myagent.core.models import UserContext, SessionData
from myagent.providers.router import ProviderRouter
from myagent.providers.openai_provider import OpenAIProvider
from myagent.providers.anthropic_provider import AnthropicProvider
from myagent.tools.manager import ToolManager
from myagent.safety.policy import PolicyEngine
from myagent.safety.cli_fence import CLIFence
from myagent.safety.content_rules import InputContentFilter, OutputContentFilter
from myagent.safety.secrets import SecretManager
from myagent.utils.config import load_yaml_config, AgentConfig
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.context.state import StateStore

logger = get_logger(__name__)


class SessionManager:
    """
    顶层会话管理器。
    不再依赖 AgentFactory，直接构建 ProviderRouter / ToolManager / SafetyGuard 等组件。
    """

    def __init__(
        self,
        *,
        config_path: str = "config.yaml",
        state_store: "StateStore | None" = None,
        session_ttl_seconds: int = 3600,
    ):
        self._config_path = config_path
        self._state_store = state_store
        self._sessions: dict[str, Session] = {}
        # per-session Harness：每次 create_session / restore_session 都新建独立实例

        self._session_ttl = session_ttl_seconds
        self._cleanup_interval = 300
        self._running = False
        self._cleanup_task: asyncio.Task | None = None

        # ── 加载配置并缓存 ──
        self._raw = load_yaml_config(config_path)
        app_config = self._raw.get("agent", self._raw) if self._raw else {}
        self._config = AgentConfig(**app_config)

        # 预加载系统提示词
        self._system_prompt: str = self._load_system_prompt()

    @property
    def context_window_size(self) -> int:
        if self._config.providers:
            for p in self._config.providers:
                if p.priority == 1:
                    return p.context_window_size
            return self._config.providers[0].context_window_size
        return 128000

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def config(self) -> AgentConfig:
        return self._config

    async def start(self) -> None:
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(f"SessionManager TTL cleanup started (TTL={self._session_ttl}s)")

    async def stop(self) -> None:
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("SessionManager TTL cleanup stopped")

    async def _cleanup_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._cleanup_interval)
            await self._evict_expired()

    async def _evict_expired(self) -> None:
        now = datetime.now(timezone.utc)
        to_evict = []
        for sid, session in self._sessions.items():
            if (now - session.last_active_at).total_seconds() > self._session_ttl:
                to_evict.append(sid)
        for sid in to_evict:
            session = self._sessions.pop(sid)
            session.unregister_events()
            if not session.has_user_message():
                if self._state_store:
                    await self._state_store.clear_session(sid)
                logger.info(f"Empty session discarded (TTL): {sid}")
            else:
                await session.save()
                logger.info(f"Session evicted (TTL): {sid}")

    # ── 组件构建（替代 AgentFactory） ──

    def _build_router(self) -> ProviderRouter:
        providers = []
        for p_cfg in self._config.providers:
            if p_cfg.type.lower() == "openai":
                p = OpenAIProvider(
                    name=p_cfg.name,
                    model=p_cfg.model,
                    api_key=p_cfg.api_key or "sk-dummy",
                    api_base=p_cfg.api_base,
                )
            elif p_cfg.type.lower() == "anthropic":
                p = AnthropicProvider(
                    name=p_cfg.name,
                    model=p_cfg.model,
                    api_key=p_cfg.api_key or "sk-dummy",
                )
            else:
                continue
            p._context_window_size = p_cfg.context_window_size
            providers.append(p)
        if not providers:
            raise RuntimeError("未配置任何 Provider，请检查 config.yaml")
        return ProviderRouter(providers)

    def _load_system_prompt(self) -> str:
        sys_prompt = self._config.system_prompt or "你是一个智能助手，可以帮助用户完成各种任务。"
        if self._config.system_prompt_file:
            prompt_path = Path(self._config.system_prompt_file)
            if prompt_path.exists():
                lines = []
                with open(prompt_path, "r", encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                            continue
                        lines.append(line.rstrip('\n'))
                sys_prompt = "\n".join(lines)
            else:
                logger.warning(f"system_prompt_file {self._config.system_prompt_file} not found.")
        return sys_prompt

    def _build_safety_components(self, no_safety: bool = False) -> tuple[PolicyEngine | None, list] | None:
        """构建安全策略引擎和规则链，返回 (policy_engine, rules) 或 None。"""
        safety_cfg = self._config.safety
        if no_safety or not safety_cfg.enabled:
            logger.info("Safety disabled by config or flag")
            return None
        rules_path = safety_cfg.rules_path
        rules_cfg = {}
        if Path(rules_path).exists():
            with open(rules_path) as f:
                rules_cfg = yaml.safe_load(f) or {}
        else:
            raise RuntimeError(
                f"Safety is enabled but rules file was not found: {rules_path}"
            )
        policy_cfg = rules_cfg.get("policy_engine", {})
        policy_engine = PolicyEngine(
            tool_policies=policy_cfg.get("tool_policies", []),
            default_action=policy_cfg.get("default_action", safety_cfg.default_action),
        )
        cli_policies = rules_cfg.get("cli_policies", {})
        default_cli_policy = rules_cfg.get("default_cli_policy", "whitelist")
        rules = [
            CLIFence(
                policies=cli_policies,
                default_policy=default_cli_policy,
            ),
            InputContentFilter(),
            OutputContentFilter(),
        ]
        logger.info(f"Safety enabled: policy_engine + {len(rules)} rules loaded")
        return policy_engine, rules

    def _build_secret_manager(self) -> SecretManager:
        secrets_cfg = self._config.secrets
        return SecretManager(
            env_prefix=secrets_cfg.env_prefix,
            sensitive_fields=secrets_cfg.sensitive_fields or None,
        )

    def _build_tool_manager(self) -> ToolManager:
        hr_cfg = self._config.hot_reload
        tools_dir = (hr_cfg.watch_dir if hr_cfg and hr_cfg.enabled else "myagent/tools/tools_store")
        runner_cfg = self._config.sandbox.model_dump()
        manager = ToolManager(tools_dir=tools_dir, runner_config=runner_cfg)
        manager._register_builtin_tools()
        return manager

    def _create_harness(
        self,
        no_safety: bool = False,
    ) -> AgentHarness:
        """
        创建独立的 per-session AgentHarness 实例。
        每个 Session 独占一套 LLMClient + ToolInterface + EventBus。
        """
        router = self._build_router()
        safety_parts = self._build_safety_components(no_safety=no_safety)
        secret_manager = self._build_secret_manager()
        tool_manager = self._build_tool_manager()
        events = EventBus()

        from myagent.core.llm import LLMClient
        from myagent.core.tools import ToolInterface

        llm_client = LLMClient(router=router, events=events)
        tool_interface = ToolInterface(
            tool_manager=tool_manager,
            policy_engine=safety_parts[0] if safety_parts else None,
            rules=safety_parts[1] if safety_parts else None,
            secret_manager=secret_manager,
        )
        harness = AgentHarness(
            llm_client=llm_client,
            tool_interface=tool_interface,
            events=events,
            max_iterations=self._config.max_iterations,
        )
        logger.info("Per-session Harness created")
        return harness

    async def create_session(
        self,
        user: UserContext,
        session_id: str | None = None,
        approval_handler=None,
        system_prompt: str | None = None,
        max_tokens_budget: int | None = None,
        context_window_size: int | None = None,
        tool_result_max_chars: int | None = None,
        no_safety: bool = False,
        workspace_root: str | None = None,
    ) -> Session:
        harness = self._create_harness(no_safety=no_safety)
        effective_prompt = system_prompt or self._system_prompt

        if not workspace_root:
            root_dir = self._config.root_dir
            if root_dir:
                workspace_root = str(Path(root_dir).expanduser())

        # 从配置文件解析默认值（config.yaml → ContextConfig）
        effective_budget = max_tokens_budget if max_tokens_budget is not None else self._config.context.max_tokens_budget
        effective_window = context_window_size if context_window_size is not None else self.context_window_size
        effective_max_chars = tool_result_max_chars if tool_result_max_chars is not None else self._config.context.tool_result_max_chars

        session = Session(
            session_id=session_id,
            harness=harness,
            user=user,
            state_store=self._state_store,
            system_prompt=effective_prompt,
            max_tokens_budget=effective_budget,
            context_window_size=effective_window,
            tool_result_max_chars=effective_max_chars,
            workspace_root=workspace_root,
            hitl_enabled=self._config.hitl.enabled,
            approval_timeout=self._config.hitl.approval_timeout,
        )
        self._sessions[session.id] = session

        # 如果提供了外部审批 handler（如 CLI），覆盖默认的 ClientBridge handler
        if approval_handler:
            session._approval_handler = approval_handler

        session.make_command_handler()

        # 注入 PromptRenderer（SSPT 动态渲染）
        try:
            renderer = self.create_prompt_renderer()
            session.set_prompt_renderer(renderer)
        except Exception as e:
            logger.warning(f"Failed to create PromptRenderer: {e}")

        if workspace_root and session.workspace:
            await session.workspace.update("user", "set_root", {"root_path": workspace_root})
            session.data.workspace.state = session.workspace.snapshot().to_dict()

        # 启动 ToolManager（创建 JsonRpcProxy + 热加载扫描）
        try:
            await harness.tool_interface.start()
            await session.update_tools()
        except Exception as e:
            logger.warning(f"ToolManager start failed (non-fatal): {e}")

        logger.info(f"Session created: {session.id} for user: {user.user_id}")
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def restore_session(
        self,
        session_id: str,
        user: UserContext,
        approval_handler=None,
        max_tokens_budget: int | None = None,
        context_window_size: int | None = None,
        tool_result_max_chars: int | None = None,
    ) -> Session:
        if not self._state_store:
            raise RuntimeError("No StateStore configured")

        harness = self._create_harness()

        agent_run_state, metadata_dict = await self._state_store.load_state(session_id)

        # 从配置文件解析默认值（config.yaml → ContextConfig）
        effective_budget = max_tokens_budget if max_tokens_budget is not None else self._config.context.max_tokens_budget
        effective_window = context_window_size if context_window_size is not None else self.context_window_size
        effective_max_chars = tool_result_max_chars if tool_result_max_chars is not None else self._config.context.tool_result_max_chars

        session = Session(
            session_id=session_id,
            harness=harness,
            user=user,
            state_store=self._state_store,
            max_tokens_budget=effective_budget,
            context_window_size=effective_window,
            tool_result_max_chars=effective_max_chars,
            hitl_enabled=self._config.hitl.enabled,
            approval_timeout=self._config.hitl.approval_timeout,
        )

        if approval_handler:
            session._approval_handler = approval_handler

        if isinstance(metadata_dict, dict):
            restored_data = SessionData.model_validate(metadata_dict)
            restored_data.model.available = session.data.model.available
            restored_data.tool.tools = session.data.tool.tools
            session.data = restored_data
            restored_policy = restored_data.safety.active_policy
            try:
                session._apply_safety_policy(restored_policy)
            except ValueError:
                fallback_state = session._harness.tool_interface.get_cli_policy_state()
                logger.warning(
                    "Stored safety policy '%s' is unavailable; using default '%s'",
                    restored_policy,
                    fallback_state["active_policy"],
                )
                session._sync_safety_policy_state()
            session._context._last_usage_input_tokens = restored_data.context.token_usage.used
        else:
            session.agent_run_state = agent_run_state

        messages = await self._state_store.load_messages(session_id)
        if messages:
            session._context.restore_from(messages)

        workspace_json = await self._state_store.load_workspace(session_id)
        if workspace_json:
            try:
                from myagent.core.workspace import WorkspaceManager, WorkspaceState
                ws_data = json.loads(workspace_json)
                ws_state = WorkspaceState.from_dict(ws_data)
                session.workspace = WorkspaceManager()
                session.workspace.restore_from(ws_state)
                session.workspace.set_on_change(session._on_workspace_change)
            except Exception as e:
                logger.warning(f"Failed to restore workspace: {e}")

        session.make_command_handler()

        # 启动 ToolManager
        try:
            await harness.tool_interface.start()
        except Exception as e:
            logger.warning(f"ToolManager start failed for restored session (non-fatal): {e}")

        self._sessions[session.id] = session
        logger.info(f"Session restored: {session_id}")
        return session

    async def join_session(
        self,
        user: UserContext,
        session_id: str | None = None,
        config_override: dict | None = None,
        approval_handler=None,
    ) -> Session:
        cfg = config_override or {}

        if session_id and session_id in self._sessions:
            return self._sessions[session_id]

        if session_id and self._state_store:
            try:
                session = await self.restore_session(
                    session_id=session_id,
                    user=user,
                    approval_handler=approval_handler,
                    **cfg,
                )
                return session
            except Exception as e:
                logger.warning(f"join_session: restore failed ({e}), fallback to create")

        session = await self.create_session(
            user=user,
            session_id=session_id,
            approval_handler=approval_handler,
            **cfg,
        )
        return session

    async def delete_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session.unregister_events()
        if self._state_store:
            await self._state_store.clear_session(session_id)
        logger.info(f"Session deleted: {session_id}")

    def get_user_active_session(self, user_id: str) -> Session | None:
        for sid in reversed(list(self._sessions.keys())):
            session = self._sessions.get(sid)
            if session and session.user.user_id == user_id:
                return session
        return None

    async def list_sessions(self, user_id: str | None = None) -> list[dict]:
        if self._state_store:
            sessions = await self._state_store.list_all_sessions()
            return sessions
        result = []
        for sid, session in self._sessions.items():
            result.append({
                "session_id": sid,
                "agent_state": session.data.context.agent_run_state,
                "session_state": session.data.context.session_state,
                "metadata": session.data.model_dump(),
            })
        return result

    async def get_session_messages(self, session_id: str) -> list:
        session = self._sessions.get(session_id)
        if session:
            return session._context.messages
        if self._state_store:
            return await self._state_store.load_messages(session_id)
        return []

    # ── SSPT: Prompt 模板 ──

    def load_prompt_template(self):
        from myagent.prompt.template import PromptTemplate
        template_path = self._config.prompt_template_path
        if Path(template_path).exists():
            return PromptTemplate.from_yaml(template_path)
        logger.warning(f"prompt_template.yaml not found at {template_path}, using default")
        return PromptTemplate.default()

    def create_prompt_renderer(self):
        from myagent.prompt.renderer import PromptRenderer
        template = self.load_prompt_template()
        return PromptRenderer(template)
