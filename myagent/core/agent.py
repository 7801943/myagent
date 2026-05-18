"""
Agent + AgentFactory：无状态 AI 引擎 + 统一构建工厂。

职责：
1. Agent：纯 AI 引擎（Provider + Tools + Safety + ReAct 循环）
2. AgentFactory：统一的 Agent 构建工厂（CLI 和 WebSocket 共用）

不再负责：
- Session 管理（→ SessionManager）
- 取消操作（→ Session）
- 审计日志（→ 标准 logger）
- 幂等缓存（→ ToolManager.IdempotencyCache）
"""
import asyncio
from pathlib import Path
from typing import Callable, Awaitable

import yaml

from myagent.providers.router import ProviderRouter
from myagent.providers.openai_provider import OpenAIProvider
from myagent.providers.anthropic_provider import AnthropicProvider
from myagent.tools.manager import ToolManager
from myagent.tools.api import ToolResult
from myagent.core.hook import HookContext, HookManager
from myagent.core.turns import TurnKind, StreamResult
from myagent.context.manager import ContextManager
from myagent.utils.config import load_yaml_config, AgentConfig
from myagent.utils.logging import get_logger
from myagent.safety.guard import SafetyGuard
from myagent.safety.policy import PolicyEngine
from myagent.safety.cli_fence import CLIFence
from myagent.safety.content_rules import InputContentFilter, OutputContentFilter
from myagent.safety.secrets import SecretManager

logger = get_logger(__name__)


class Agent:
    """
    无状态 AI 引擎：Provider + Tools + Safety + ReAct 循环。
    原 Agent + AgentLoop 合并。

    不再持有 Session 状态，通过 run(context, ctx) 接受外部传入的上下文。
    """

    def __init__(
        self,
        *,
        provider_router: ProviderRouter,
        tool_manager: ToolManager | None = None,
        safety_guard=None,
        secret_manager=None,
        max_iterations: int = 100,
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
    ):
        """
        无状态 AI 引擎。hooks 始终自建（共享广播中心），外部通过 agent.hooks.on() 注册回调。
        不再接受外部 hooks 注入，确保多客户端共享同一个 HookManager。
        """
        self._router = provider_router
        self._tool_manager = tool_manager or ToolManager()
        self._hooks = HookManager()  # 始终自建，不暴露给外部
        self._safety_guard = safety_guard
        self._secret_manager = secret_manager
        self._max_iterations = max_iterations
        self._approval_handler = approval_handler
        # 不再持有 _system_command_handler, _workspace_root, _active_file_path, _session_meta
        # 这些通过 HookContext 由 Session.chat() 每次注入

    @property
    def hooks(self) -> HookManager:
        return self._hooks

    @property
    def tools(self) -> ToolManager:
        return self._tool_manager

    @property
    def tool_manager(self) -> ToolManager:
        return self._tool_manager

    @property
    def router(self) -> ProviderRouter:
        return self._router

    def add_tool(self, tool) -> None:
        self._tool_manager.register(tool)

    def add_hook(self, event: str, callback) -> None:
        """注册 hook 回调（代理到 HookManager.on()）。"""
        self._hooks.on(event, callback)

    async def run(self, context: ContextManager, ctx: HookContext) -> StreamResult:
            from myagent.core.turns import TurnResult
            
            # 1. 初始化起始状态载体（不再需要 current_kind, current_data 等散装变量）
            state = TurnResult(kind=None, next_turn=TurnKind.SYSTEM, data=None)

            try:
                for iteration in range(self._max_iterations):
                    ctx.iteration = iteration + 1
                    logger.debug(f"Iteration {ctx.iteration}, turn={state.next_turn.name}")

                    # 2. 从状态载体中提取动作并执行
                    turn = self._create_turn(state.next_turn, context, ctx)
                    state = await turn.execute(ctx, input_data=state.data, source=state.kind)

                    # 3. 检查流转是否结束
                    if state.next_turn is None:
                        await self._hooks.emit("state_change", ctx, state="idle")
                        return state.stream_result

                # 4. for...else: 处理达到最大迭代次数
                logger.warning(f"Agent reached max iterations ({self._max_iterations})")
                msg = "达到最大迭代次数限制，终止执行。"
                await context.add_assistant_message(content=msg, tool_calls=None)
                return StreamResult(text=msg, stop_reason="max_iterations")

            except asyncio.CancelledError:
                # 5. 取消处理
                cancel_msg = "[系统] 操作已取消"
                logger.info(f"Agent cancelled at iteration {getattr(ctx, 'iteration', 0)}")
                await context.add_assistant_message(content=cancel_msg, tool_calls=None)
                return StreamResult(text=cancel_msg, stop_reason="cancelled")
            
    def _create_turn(self, kind: TurnKind, context: ContextManager, ctx: HookContext):
        """Turn 工厂。每次动态获取 tool_schemas，支持运行时热加载。"""
        if kind == TurnKind.MODEL:
            from myagent.core.turns import ModelTurn
            # 数据源：优先从 ctx.session_meta（会话级），降级到 ToolManager 直接调用（CLI 单会话场景）
            if ctx.session_meta:
                tool_schemas = ctx.session_meta.tool.tools
            else:
                tool_schemas = self._tool_manager.list_schemas() if self._tool_manager else None
            return ModelTurn(
                provider_router=self._router,
                context=context,
                tool_schemas=tool_schemas,
                hooks=self._hooks,
                timeout=120.0,
            )
        elif kind == TurnKind.TOOL:
            from myagent.core.turns import ToolTurn
            return ToolTurn(
                context=context,
                tool_executor=self._execute_tool_batch,
                hooks=self._hooks,
                timeout=60.0,
                approval_handler=self._approval_handler,
            )
        elif kind == TurnKind.SYSTEM:
            from myagent.core.turns import SystemTurn
            # 从 ctx 获取会话级指令处理器
            return SystemTurn(
                context=context,
                hooks=self._hooks,
                timeout=30.0,
                system_command_handler=ctx.system_command_handler,
            )
        else:
            raise ValueError(f"Unknown TurnKind: {kind}")

    async def execute_tool(self, name: str, args: dict, tool_call_id: str, skip_safety: bool = False) -> ToolResult:
        """
        工具执行钩子链：Safety → Secret → Execute（含幂等缓存）。

        幂等缓存（IdempotencyCache）已下沉到 ToolManager.execute() 中，
        通过 tool_call_id 参数自动启用。
        """
        if not skip_safety and self._safety_guard:
            guard_result = await self._safety_guard.check_tool_call(name, args)
            if guard_result.is_denied:
                return ToolResult(
                    content=f"安全策略拒绝执行工具 '{name}': {guard_result.reason}",
                    is_error=True,
                    metadata={"denied_by": guard_result.rule_name, "tool_call_id": tool_call_id},
                )
            if guard_result.requires_hitl:
                return ToolResult(
                    content=f"工具 '{name}' 需要人工审批: {guard_result.reason}",
                    is_error=False,
                    metadata={
                        "needs_approval": True,
                        "reason": guard_result.reason,
                        "tool_call_id": tool_call_id,
                    },
                )
            if guard_result.decision and hasattr(guard_result.decision, 'value') and guard_result.decision.value == "rewrite" and guard_result.rewritten_args:
                args = guard_result.rewritten_args

        if self._secret_manager:
            args = self._secret_manager.inject_secrets(name, args)

        return await self._tool_manager.execute(name, tool_call_id=tool_call_id, **args)

    async def _execute_tool_batch(self, tool_calls: list, skip_safety: bool = False) -> list:
        """批量执行工具。tool_calls = list[ToolCall]。"""
        tasks = [self.execute_tool(tc.name, tc.arguments, tc.id, skip_safety) for tc in tool_calls]
        return await asyncio.gather(*tasks)

    async def start_hot_reload(self) -> None:
        if self._tool_manager:
            await self._tool_manager.start()

    async def stop_hot_reload(self) -> None:
        if self._tool_manager:
            await self._tool_manager.stop()


class AgentFactory:
    """
    统一的 Agent 构建工厂。CLI 和 WebSocket 共用。

    用法：
        factory = AgentFactory(config_path="config.yaml")
        agent = factory.create_agent(approval_handler=my_handler)
    """

    def __init__(
        self,
        config_path: str = "config.yaml",
    ):
        """
        初始化工厂，加载并缓存配置。

        Args:
            config_path: YAML 配置文件路径
        """
        self._config_path = config_path

        # 加载配置（只加载一次）
        self._raw = load_yaml_config(config_path)
        app_config = self._raw.get("agent", self._raw) if self._raw else {}
        self._config = AgentConfig(**app_config)

        # 预加载系统提示词（供外部通过 factory.system_prompt 获取）
        self._system_prompt: str = self._load_system_prompt()

    @property
    def context_window_size(self) -> int:
        """获取上下文窗口大小（从 active provider 的配置中读取）。"""
        if self._config.providers:
            # 优先取 priority=1 的 provider
            for p in self._config.providers:
                if p.priority == 1:
                    return p.context_window_size
            # 降级取第一个
            return self._config.providers[0].context_window_size
        return 128000

    @property
    def system_prompt(self) -> str:
        """获取系统提示词。"""
        return self._system_prompt

    @property
    def config(self) -> AgentConfig:
        """获取解析后的 Agent 配置（强类型，所有配置段均已建模）。"""
        return self._config

    def create_agent(
        self,
        *,
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
        no_safety: bool = False,
    ) -> Agent:
        """
        创建纯 Agent 实例（无 session 管理）。
        Agent 始终自建 HookManager（共享广播中心），外部通过 agent.hooks.on() 注册回调。

        Args:
            approval_handler: 可选的人工审批回调
            no_safety: 是否禁用安全检查

        Returns:
            完全配置好的 Agent 实例
        """
        # ── 1. 构建 ProviderRouter ──
        router = self._build_router()

        # ── 2. 加载系统提示词 ──
        system_prompt = self._load_system_prompt()
        self._system_prompt = system_prompt  # 缓存供外部获取

        # ── 3. 构建安全系统 ──
        safety_guard = self._build_safety_guard(no_safety=no_safety)

        # ── 4. 构建密钥管理 ──
        secret_manager = self._build_secret_manager()

        # ── 5. 构建工具管理器 ──
        tool_manager = self._build_tool_manager()

        # ── 6. 组装 Agent（hooks 始终自建，不传外部 hooks）──
        agent = Agent(
            provider_router=router,
            tool_manager=tool_manager,
            safety_guard=safety_guard,
            secret_manager=secret_manager,
            max_iterations=self._config.max_iterations,
            approval_handler=approval_handler,
        )

        logger.info("Agent created")
        return agent

    def _build_router(self) -> ProviderRouter:
        """构建多模型路由器。"""
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
            # 注入 context_window_size 供 Session._init_meta_from_agent() 读取
            p._context_window_size = p_cfg.context_window_size
            providers.append(p)

        if not providers:
            raise RuntimeError("未配置任何 Provider，请检查 config.yaml")

        return ProviderRouter(providers)

    def _load_system_prompt(self) -> str:
        """加载系统提示词（从配置或文件）。"""
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
                logger.warning(
                    f"system_prompt_file {self._config.system_prompt_file} not found. Using fallback."
                )

        return sys_prompt

    def _build_safety_guard(self, no_safety: bool = False) -> SafetyGuard | None:
        """构建安全守卫系统。"""
        safety_cfg = self._config.safety

        if no_safety or not safety_cfg.enabled:
            return None

        # 加载策略规则
        rules_path = safety_cfg.rules_path
        rules_cfg = {}
        if Path(rules_path).exists():
            with open(rules_path) as f:
                rules_cfg = yaml.safe_load(f) or {}
            logger.info(f"Safety rules loaded from {rules_path}")
        else:
            logger.warning(
                f"Safety rules file not found: {rules_path}. "
                f"PolicyEngine and CLIFence will use empty config. "
                f"Please check the path or create the file."
            )

        policy_cfg = rules_cfg.get("policy_engine", {})
        policy_engine = PolicyEngine(
            tool_policies=policy_cfg.get("tool_policies", []),
            default_action=policy_cfg.get(
                "default_action", safety_cfg.default_action
            ),
        )

        cli_fence_cfg = rules_cfg.get("cli_fence", {})
        rules = [
            CLIFence(
                allowed_commands=cli_fence_cfg.get("allowed_commands"),
                approval_commands=cli_fence_cfg.get("approval_commands"),
                denied_patterns=cli_fence_cfg.get("denied_patterns"),
                denied_paths=cli_fence_cfg.get("denied_paths"),
            ),
            InputContentFilter(),
            OutputContentFilter(),
        ]

        safety_guard = SafetyGuard(
            policy_engine=policy_engine,
            rules=rules,
        )
        logger.info("SafetyGuard enabled with PolicyEngine + 3 rules")
        return safety_guard

    def _build_secret_manager(self) -> SecretManager:
        """构建密钥管理器。"""
        secrets_cfg = self._config.secrets
        return SecretManager(
            env_prefix=secrets_cfg.env_prefix,
            sensitive_fields=secrets_cfg.sensitive_fields or None,
        )

    def _build_tool_manager(self) -> ToolManager:
        hr_cfg = self._config.hot_reload
        tools_dir = (hr_cfg.watch_dir
                     if hr_cfg and hr_cfg.enabled
                     else "myagent/tools/tools_store")

        runner_cfg = self._config.sandbox.model_dump()

        manager = ToolManager(tools_dir=tools_dir,
                              runner_config=runner_cfg)
        manager._register_builtin_tools()
        logger.info(
            "Registered builtin tools: cli_execute, file_read, file_write")
        return manager

    # ── SSPT: Prompt 模板加载与渲染器创建 ──

    def load_prompt_template(self):
        """加载 prompt 模板配置。"""
        from myagent.prompt.template import PromptTemplate

        template_path = self._config.prompt_template_path
        if Path(template_path).exists():
            return PromptTemplate.from_yaml(template_path)
        logger.warning(f"prompt_template.yaml not found at {template_path}, using default")
        return PromptTemplate.default()

    def create_prompt_renderer(self):
        """创建 prompt 渲染器。"""
        from myagent.prompt.renderer import PromptRenderer

        template = self.load_prompt_template()
        return PromptRenderer(template)
