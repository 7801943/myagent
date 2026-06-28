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
    from myagent.context.user_state import UserStateStoreRegistry
    from myagent.prompt.skills import SkillRegistry

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
        state_store_registry: "UserStateStoreRegistry | None" = None,
        session_ttl_seconds: int = 3600,
    ):
        self._config_path = config_path
        self._state_store = state_store
        self._state_store_registry = state_store_registry
        self._sessions: dict[tuple[str, str], Session] = {}
        # per-session Harness：每次 create_session / restore_session 都新建独立实例

        self._session_ttl = session_ttl_seconds
        self._cleanup_interval = 300
        self._running = False
        self._cleanup_task: asyncio.Task | None = None

        # ── 加载配置并缓存 ──
        self._config_file = Path(config_path).expanduser()
        self._config_dir = self._config_file.resolve().parent if self._config_file.exists() else Path.cwd()
        self._raw = load_yaml_config(config_path)
        app_config = self._raw.get("agent", self._raw) if self._raw else {}
        self._config = AgentConfig(**app_config)

        # 预加载系统提示词
        self._system_prompt: str = self._load_system_prompt()

    def _user_key(self, user: UserContext | str | None) -> str:
        if isinstance(user, UserContext):
            raw = user.username or user.user_id
        else:
            raw = user or "default"
        safe = self._resolve_skill_username(UserContext(user_id=str(raw), username=str(raw)))
        return safe

    def _session_key(self, user: UserContext | str | None, session_id: str) -> tuple[str, str]:
        return (self._user_key(user), session_id)

    async def _state_store_for_user(self, user: UserContext):
        if self._state_store_registry:
            return await self._state_store_registry.get_store(self._user_key(user))
        return self._state_store

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
        for key, session in self._sessions.items():
            if (now - session.last_active_at).total_seconds() > self._session_ttl:
                to_evict.append(key)
        for key in to_evict:
            session = self._sessions.pop(key)
            session.unregister_events()
            # [BUG-FIX] 停止 ToolManager，释放 JsonRpcProxy 连接 + 热加载 Task
            await self._cleanup_session_resources(session)
            if not session.has_user_message():
                store = await self._state_store_for_user(session.user)
                if store:
                    await store.clear_session(session.id)
                logger.info(f"Empty session discarded (TTL): {session.id}")
            else:
                await session.save()
                logger.info(f"Session evicted (TTL): {session.id}")

    # ── 组件构建（替代 AgentFactory） ──

    def _build_router(self) -> ProviderRouter:
        providers = []
        for p_cfg in self._config.providers:
            thinking_supported = (
                p_cfg.thinking.supported
                if p_cfg.thinking.supported is not None
                else p_cfg.model.lower().startswith("glm-5")
            )
            thinking_enabled = bool(thinking_supported and p_cfg.thinking.default_enabled)
            if p_cfg.type.lower() == "openai":
                p = OpenAIProvider(
                    name=p_cfg.name,
                    model=p_cfg.model,
                    api_key=p_cfg.api_key or "sk-dummy",
                    api_base=p_cfg.api_base,
                    thinking_supported=thinking_supported,
                    thinking_enabled=thinking_enabled,
                    thinking_enabled_extra_body=p_cfg.thinking.enabled_extra_body,
                    thinking_disabled_extra_body=p_cfg.thinking.disabled_extra_body,
                )
            elif p_cfg.type.lower() == "anthropic":
                p = AnthropicProvider(
                    name=p_cfg.name,
                    model=p_cfg.model,
                    api_key=p_cfg.api_key or "sk-dummy",
                )
                p.thinking_supported = thinking_supported
                p.thinking_enabled = thinking_enabled
                p.thinking_enabled_extra_body = p_cfg.thinking.enabled_extra_body
                p.thinking_disabled_extra_body = p_cfg.thinking.disabled_extra_body
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

    def _resolve_skill_username(self, user: UserContext) -> str:
        raw = (user.username or user.user_id or "default").strip()
        safe = raw.replace("/", "_").replace("\\", "_")
        return safe if safe and safe not in {".", ".."} else "default"

    def _build_skill_registry(self, user: UserContext) -> "SkillRegistry":
        """根据公共 Skill 目录和当前用户可见性构建 SkillRegistry。"""
        from myagent.prompt.skills import SkillRegistry

        username = self._resolve_skill_username(user)
        registry = SkillRegistry(username=username)
        skill_cfg = self._config.skills
        if not skill_cfg.enabled:
            logger.info("Skill system disabled")
            return registry

        visible = user.preferences.get("visible_skills")
        visible_names = None if visible in (None, ["*"], "*") else set(visible)
        common_dir = getattr(skill_cfg, "common_dir", "") or "prompts/skills/common"
        skill_root = Path(common_dir)
        if not skill_root.is_absolute():
            skill_root = self._config_dir / skill_root
        registry.load_from_common_dir(
            skill_root,
            active_names=set(skill_cfg.active),
            visible_names=visible_names,
        )
        return registry

    def _build_tool_manager(self, skill_registry: "SkillRegistry | None" = None) -> ToolManager:
        hr_cfg = self._config.hot_reload
        tools_dir = (hr_cfg.watch_dir if hr_cfg and hr_cfg.enabled else "myagent/tools/tools_store")
        runner_cfg = self._config.sandbox.model_dump()
        # [RISK-FIX] builtin tools 已在 ToolManager.__init__ 中自动注册
        manager = ToolManager(
            tools_dir=tools_dir,
            runner_config=runner_cfg,
            skill_registry=skill_registry,
        )
        return manager

    def _build_workspace_resolver(self, user: UserContext):
        """Build the private/public workspace resolver for a user."""
        from myagent.core.workspace_resolver import WorkspaceResolver, safe_workspace_username

        username = self._user_key(user)
        group = str(user.preferences.get("group") or ("admin" if username == "admin" else "user"))
        raw_workspace = self._raw.get("workspace", {})
        if not raw_workspace and isinstance(self._raw.get("agent"), dict):
            raw_workspace = self._raw.get("agent", {}).get("workspace", {})
        base_dir = Path(raw_workspace.get("base_dir") or "workspaces")
        if not base_dir.is_absolute():
            base_dir = self._config_dir / base_dir
        private_template = raw_workspace.get("private_dir_template") or str(base_dir / "users" / "{username}")
        public_dir = Path(raw_workspace.get("public_dir") or (base_dir / "public"))
        safe_user = safe_workspace_username(username)

        user_workspace = user.preferences.get("workspace") if isinstance(user.preferences, dict) else {}
        private_override = user_workspace.get("private_dir") if isinstance(user_workspace, dict) else None
        private_dir = Path(private_override or private_template.format(username=safe_user))
        if not private_dir.is_absolute():
            private_dir = self._config_dir / private_dir
        if not public_dir.is_absolute():
            public_dir = self._config_dir / public_dir

        return WorkspaceResolver(
            username=safe_user,
            group=group,
            private_root=private_dir,
            public_root=public_dir,
        )

    def _normalize_restored_workspace_state(self, ws_state, workspace_resolver):
        """Convert legacy workspace snapshots to the current visible workspace paths."""
        if not workspace_resolver:
            return ws_state
        root_path = str(getattr(ws_state, "root_path", "") or "")
        if root_path.startswith("workspace://"):
            ws_state.root_path = workspace_resolver.virtual_root
            self._canonicalize_workspace_paths(ws_state, workspace_resolver)
            return self._ensure_virtual_workspace_roots(ws_state, workspace_resolver)

        legacy_area = None
        try:
            legacy_root = Path(root_path).expanduser().resolve()
            if legacy_root == workspace_resolver.public_root:
                legacy_area = "public"
            elif legacy_root == workspace_resolver.private_root:
                legacy_area = "private"
        except Exception:
            legacy_area = None

        ws_state.root_path = workspace_resolver.virtual_root
        if not legacy_area:
            # Unknown legacy root: discard stale tree and let resolver rescan.
            ws_state.files = []
            ws_state.open_files = []
            ws_state.active_file_index = None
            ws_state.expanded_dirs = []
            return ws_state

        def _prefix(path: str) -> str:
            raw = str(path or "").strip("/")
            try:
                area, inner = workspace_resolver._split_virtual_path(raw)
                return workspace_resolver._join_virtual(area, inner)
            except ValueError:
                pass
            return workspace_resolver._join_virtual(legacy_area, raw)

        for file_info in ws_state.files:
            file_info.path = _prefix(file_info.path)
            area = workspace_resolver.virtual_path_area(file_info.path) or legacy_area
            workspace_resolver._apply_permissions(file_info, area)
        for tab in ws_state.open_files:
            tab.path = _prefix(tab.path)
        ws_state.expanded_dirs = [_prefix(path) for path in ws_state.expanded_dirs]
        return self._ensure_virtual_workspace_roots(ws_state, workspace_resolver)

    def _canonicalize_workspace_paths(self, ws_state, workspace_resolver):
        """Rewrite legacy private/public paths to the current visible directory names."""
        def _canonical(path: str) -> str:
            raw = str(path or "").strip("/")
            try:
                area, inner = workspace_resolver._split_virtual_path(raw)
                return workspace_resolver._join_virtual(area, inner)
            except ValueError:
                return raw

        for file_info in ws_state.files:
            file_info.path = _canonical(file_info.path)
        for tab in ws_state.open_files:
            tab.path = _canonical(tab.path)
        ws_state.expanded_dirs = [_canonical(path) for path in ws_state.expanded_dirs]
        return ws_state

    def _ensure_virtual_workspace_roots(self, ws_state, workspace_resolver):
        """Ensure restored workspace snapshots have visible configured roots."""
        for file_info in ws_state.files:
            area = workspace_resolver.virtual_path_area(file_info.path)
            if area:
                workspace_resolver._apply_permissions(file_info, area)

        existing = {str(file_info.path or "") for file_info in ws_state.files}
        roots = []
        for root_path, area in (
            (workspace_resolver.private_virtual_root, "private"),
            (workspace_resolver.public_virtual_root, "public"),
        ):
            if root_path not in existing:
                roots.append(workspace_resolver._dir_info(root_path, area))
        if roots:
            ws_state.files = roots + ws_state.files

        known_dirs = {str(file_info.path or "") for file_info in ws_state.files if file_info.is_dir}
        expanded_dirs = []
        for root_path in workspace_resolver.root_virtual_paths:
            if root_path in known_dirs:
                expanded_dirs.append(root_path)
        for path in ws_state.expanded_dirs:
            if path and path in known_dirs and path not in expanded_dirs:
                expanded_dirs.append(path)
        ws_state.expanded_dirs = expanded_dirs
        return ws_state

    async def _initial_workspace_state(self, workspace_resolver):
        """Build the default workspace tree with configured first-level entries."""
        from myagent.core.workspace import WorkspaceState

        state = WorkspaceState(root_path=workspace_resolver.virtual_root)
        files = await workspace_resolver.scan_dir(None)
        expanded_dirs: list[str] = []
        for root_dir in workspace_resolver.root_virtual_paths:
            try:
                files.extend(await workspace_resolver.scan_dir(root_dir))
                expanded_dirs.append(root_dir)
            except Exception as exc:
                logger.warning("Failed to scan workspace root '%s': %s", root_dir, exc)
        state.files = files
        state.expanded_dirs = expanded_dirs
        return state

    async def _hydrate_workspace_roots(self, ws_state, workspace_resolver):
        """Merge current configured root entries into a restored workspace snapshot."""
        if not ws_state.files:
            return await self._initial_workspace_state(workspace_resolver)

        existing = {str(file_info.path or "") for file_info in ws_state.files}
        additions = []
        for virtual_dir in (None, *workspace_resolver.root_virtual_paths):
            try:
                entries = await workspace_resolver.scan_dir(virtual_dir)
            except Exception as exc:
                logger.warning("Failed to hydrate workspace dir '%s': %s", virtual_dir or "/", exc)
                continue
            for entry in entries:
                if entry.path not in existing:
                    additions.append(entry)
                    existing.add(entry.path)
        if additions:
            ws_state.files.extend(additions)
        return self._ensure_virtual_workspace_roots(ws_state, workspace_resolver)

    def _create_harness(
        self,
        no_safety: bool = False,
        skill_registry: "SkillRegistry | None" = None,
        user: UserContext | None = None,
        workspace_resolver=None,
    ) -> AgentHarness:
        """
        创建独立的 per-session AgentHarness 实例。
        每个 Session 独占一套 LLMClient + ToolInterface + EventBus。
        """
        router = self._build_router()
        safety_parts = self._build_safety_components(no_safety=no_safety)
        secret_manager = self._build_secret_manager()
        tool_manager = self._build_tool_manager(skill_registry=skill_registry)
        events = EventBus()

        from myagent.core.llm import LLMClient
        from myagent.core.tools import ToolInterface

        llm_client = LLMClient(router=router, events=events)
        tool_interface = ToolInterface(
            tool_manager=tool_manager,
            policy_engine=safety_parts[0] if safety_parts else None,
            rules=safety_parts[1] if safety_parts else None,
            secret_manager=secret_manager,
            user=user,
            workspace_resolver=workspace_resolver,
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
        skill_registry = self._build_skill_registry(user)
        workspace_resolver = self._build_workspace_resolver(user)
        harness = self._create_harness(
            no_safety=no_safety,
            skill_registry=skill_registry,
            user=user,
            workspace_resolver=workspace_resolver,
        )
        effective_prompt = system_prompt or self._system_prompt
        state_store = await self._state_store_for_user(user)

        if not workspace_root and workspace_resolver:
            workspace_root = workspace_resolver.virtual_root
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
            state_store=state_store,
            system_prompt=effective_prompt,
            max_tokens_budget=effective_budget,
            context_window_size=effective_window,
            tool_result_max_chars=effective_max_chars,
            workspace_root=workspace_root,
            workspace_resolver=workspace_resolver,
            hitl_enabled=self._config.hitl.enabled,
            approval_timeout=self._config.hitl.approval_timeout,
            skill_registry=skill_registry,
        )
        self._sessions[self._session_key(user, session.id)] = session

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

    def get_session(self, session_id: str, user: UserContext | str | None = None) -> Session | None:
        if user is not None:
            return self._sessions.get(self._session_key(user, session_id))
        for (_username, sid), session in self._sessions.items():
            if sid == session_id:
                return session
        return None

    async def restore_session(
        self,
        session_id: str,
        user: UserContext,
        approval_handler=None,
        max_tokens_budget: int | None = None,
        context_window_size: int | None = None,
        tool_result_max_chars: int | None = None,
    ) -> Session:
        state_store = await self._state_store_for_user(user)
        if not state_store:
            raise RuntimeError("No StateStore configured")

        skill_registry = self._build_skill_registry(user)
        workspace_resolver = self._build_workspace_resolver(user)
        harness = self._create_harness(
            skill_registry=skill_registry,
            user=user,
            workspace_resolver=workspace_resolver,
        )

        agent_run_state, metadata_dict = await state_store.load_state(session_id)

        # 从配置文件解析默认值（config.yaml → ContextConfig）
        effective_budget = max_tokens_budget if max_tokens_budget is not None else self._config.context.max_tokens_budget
        effective_window = context_window_size if context_window_size is not None else self.context_window_size
        effective_max_chars = tool_result_max_chars if tool_result_max_chars is not None else self._config.context.tool_result_max_chars

        session = Session(
            session_id=session_id,
            harness=harness,
            user=user,
            state_store=state_store,
            max_tokens_budget=effective_budget,
            context_window_size=effective_window,
            tool_result_max_chars=effective_max_chars,
            workspace_root=getattr(workspace_resolver, "virtual_root", None),
            workspace_resolver=workspace_resolver,
            hitl_enabled=self._config.hitl.enabled,
            approval_timeout=self._config.hitl.approval_timeout,
            skill_registry=skill_registry,
        )

        if approval_handler:
            session._approval_handler = approval_handler

        if isinstance(metadata_dict, dict):
            restored_data = SessionData.model_validate(metadata_dict)
            restored_active = restored_data.model.active or {}
            restored_provider_key = (
                restored_active.get("provider_key")
                or restored_active.get("provider_name")
                or ""
            )
            if restored_provider_key:
                try:
                    provider = session.harness.router.set_provider(restored_provider_key)
                    if restored_active.get("thinking_enabled") is not None:
                        provider.thinking_enabled = bool(
                            restored_active.get("thinking_enabled")
                            and getattr(provider, "thinking_supported", False)
                        )
                except ValueError:
                    logger.warning(
                        "Stored provider '%s' is unavailable; using configured default",
                        restored_provider_key,
                    )
            session._sync_model_state_from_router()
            restored_data.model = session.data.model
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

        messages = await state_store.load_messages(session_id)
        if messages:
            session._context.restore_from(messages)

        workspace_json = await state_store.load_workspace(session_id)
        if workspace_json:
            try:
                from myagent.core.workspace import WorkspaceManager, WorkspaceState
                ws_data = json.loads(workspace_json)
                ws_state = WorkspaceState.from_dict(ws_data)
                ws_state = self._normalize_restored_workspace_state(ws_state, workspace_resolver)
                if workspace_resolver:
                    ws_state = await self._hydrate_workspace_roots(ws_state, workspace_resolver)
                # [BUG-FIX] 传入 root_path 初始化，确保 WorkspaceManager.root_path 立即可用
                # （restore_from 虽也会设置，但构造时初始化更安全，避免中间状态窗口）
                session.workspace = WorkspaceManager(
                    root_path=ws_state.root_path,
                    resolver=workspace_resolver,
                )
                session.workspace.restore_from(ws_state)
                session.workspace.set_on_change(session._on_workspace_change)
            except Exception as e:
                logger.warning(f"Failed to restore workspace: {e}")

        session.make_command_handler()

        # 注入 PromptRenderer（SSPT 动态渲染）
        # [BUG-FIX] restore_session 必须与 create_session 一样注入 PromptRenderer，
        # 否则恢复的会话无法动态刷新 system prompt，workspace 文件变更（如打开新文件）
        # 无法被 LLM 上下文感知。
        try:
            renderer = self.create_prompt_renderer()
            session.set_prompt_renderer(renderer)
        except Exception as e:
            logger.warning(f"Failed to create PromptRenderer for restored session: {e}")

        # 启动 ToolManager
        try:
            await harness.tool_interface.start()
            await session.update_tools()
        except Exception as e:
            logger.warning(f"ToolManager start failed for restored session (non-fatal): {e}")

        self._sessions[self._session_key(user, session.id)] = session
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

        key = self._session_key(user, session_id) if session_id else None
        if key and key in self._sessions:
            return self._sessions[key]

        if session_id and await self._state_store_for_user(user):
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

    async def delete_session(self, session_id: str, user: UserContext | str | None = None) -> None:
        key = self._session_key(user, session_id) if user is not None else None
        session = self._sessions.pop(key, None) if key else self.get_session(session_id)
        if session and key is None:
            self._sessions.pop(self._session_key(session.user, session.id), None)
        if session:
            session.unregister_events()
            # [BUG-FIX] 停止 ToolManager，释放资源
            await self._cleanup_session_resources(session)
        store = await self._state_store_for_user(session.user if session else UserContext(user_id=str(user or "default"), username=str(user or "default")))
        if store:
            await store.clear_session(session_id)
        logger.info(f"Session deleted: {session_id}")

    async def _cleanup_session_resources(self, session: Session) -> None:
        """[BUG-FIX] 清理 Session 持有的 per-session 资源。

        每个 Session 独占一套 Harness → ToolInterface → ToolManager，
        ToolManager 内部持有 JsonRpcProxy 连接 + _watch_task asyncio.Task。
        不显式 stop 会导致 fd / task 泄漏。
        """
        try:
            harness = getattr(session, "_harness", None)
            if harness:
                await harness.tool_interface.stop()
                logger.debug(f"ToolManager stopped for session: {session.id}")
        except Exception as e:
            logger.warning(f"Failed to stop ToolManager for session {session.id}: {e}")

    def get_user_active_session(self, user_id: str) -> Session | None:
        for sid in reversed(list(self._sessions.keys())):
            session = self._sessions.get(sid)
            if session and session.user.user_id == user_id:
                return session
        return None

    async def list_sessions(self, user_id: str | None = None) -> list[dict]:
        if self._state_store_registry and user_id:
            store = await self._state_store_registry.get_store(self._user_key(user_id))
            sessions = await store.list_all_sessions()
            return sessions
        if self._state_store:
            sessions = await self._state_store.list_all_sessions()
            return sessions
        result = []
        for (_username, sid), session in self._sessions.items():
            if user_id and self._user_key(user_id) != self._user_key(session.user):
                continue
            result.append({
                "session_id": sid,
                "agent_state": session.data.context.agent_run_state,
                "session_state": session.data.context.session_state,
                "metadata": session.data.model_dump(),
            })
        return result

    async def get_session_messages(self, session_id: str, user: UserContext | str | None = None) -> list:
        session = self.get_session(session_id, user=user)
        if session:
            return session._context.messages
        if self._state_store_registry and user is not None:
            store = await self._state_store_registry.get_store(self._user_key(user))
            return await store.load_messages(session_id)
        if self._state_store:
            return await self._state_store.load_messages(session_id)
        return []

    async def notify_public_workspace_changed(self, changed_paths: list[str]) -> None:
        """Notify all active sessions that public workspace files changed."""
        for session in list(self._sessions.values()):
            resolver = getattr(session.workspace, "resolver", None) if session.workspace else None
            if not resolver:
                continue
            public_paths = [
                path for path in changed_paths
                if resolver.virtual_path_area(path) == "public"
            ]
            if public_paths:
                await session.workspace.update("user", "files_changed", {"changed_paths": public_paths})

    # ── SSPT: Prompt 模板 ──

    def load_prompt_template(self):
        from myagent.prompt.template import PromptTemplate
        template_path = Path(self._config.prompt_template_path).expanduser()
        if not template_path.is_absolute():
            template_path = self._config_dir / template_path
        if template_path.exists():
            return PromptTemplate.from_yaml(template_path)
        logger.warning(f"prompt_template.yaml not found at {template_path}, using default")
        return PromptTemplate.default()

    def create_prompt_renderer(self):
        from myagent.prompt.renderer import PromptRenderer
        template = self.load_prompt_template()
        return PromptRenderer(template)
