"""
AgentFactory：统一的 Agent 构建工厂。
从 myagent/factory.py 移入 core/，Phase 1 简化。

Phase 1 变更：
  - 删除 audit_logger 相关代码
  - 删除 TimeoutConfig（超时简化为模块级常量）
  - 删除 state_store 参数（Session 层面管理）
  - 返回纯 Agent（无 session 管理）
"""
from pathlib import Path
from typing import Callable, Awaitable

import yaml

from myagent.core.agent import Agent
from myagent.core.hook import HookManager
from myagent.core.hook import HookContext
from myagent.utils.config import load_yaml_config, AgentConfig
from myagent.utils.logging import get_logger
from myagent.providers.openai_provider import OpenAIProvider
from myagent.providers.anthropic_provider import AnthropicProvider
from myagent.providers.router import ProviderRouter
from myagent.safety.guard import SafetyGuard
from myagent.safety.policy import PolicyEngine
from myagent.safety.cli_fence import CLIFence
from myagent.safety.content_rules import InputContentFilter, OutputContentFilter
from myagent.tools.manager import ToolManager
from myagent.safety.secrets import SecretManager

logger = get_logger(__name__)


class AgentFactory:
    """
    统一的 Agent 构建工厂。CLI 和 WebSocket 共用。

    用法：
        factory = AgentFactory(config_path="config.yaml")
        agent = factory.create_agent(hooks=my_hooks, approval_handler=my_handler)
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
        self._app_config = self._raw.get("agent", self._raw) if self._raw else {}
        self._config_obj = AgentConfig(**self._app_config)

        # 预加载系统提示词（供外部通过 factory.system_prompt 获取）
        self._system_prompt: str = self._load_system_prompt()

    @property
    def context_window_size(self) -> int:
        """获取上下文窗口大小（从第一个 provider 配置中读取）。"""
        if self._config_obj.providers:
            return self._config_obj.providers[0].context_window_size
        return 128000

    @property
    def system_prompt(self) -> str:
        """获取系统提示词。"""
        return self._system_prompt

    @property
    def config(self) -> AgentConfig:
        """获取解析后的 Agent 配置。"""
        return self._config_obj

    @property
    def app_config(self) -> dict:
        """获取原始应用配置 dict（包含 safety、sandbox 等未在 AgentConfig 中定义的字段）。"""
        return self._app_config

    def create_agent(
        self,
        *,
        hooks: HookManager,
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
        no_safety: bool = False,
    ) -> Agent:
        """
        创建纯 Agent 实例（无 session 管理）。

        Args:
            hooks: 由调用方构建的 HookManager
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

        # ── 6. 组装 Agent ──
        agent = Agent(
            provider_router=router,
            hooks=hooks,
            tool_manager=tool_manager,
            safety_guard=safety_guard,
            secret_manager=secret_manager,
            max_iterations=self._config_obj.max_iterations,
            approval_handler=approval_handler,
        )

        logger.info("Agent created")
        return agent

    def _build_router(self) -> ProviderRouter:
        """构建多模型路由器。"""
        providers = []
        for p_cfg in self._config_obj.providers:
            if p_cfg.type.lower() == "openai":
                providers.append(OpenAIProvider(
                    name=p_cfg.name,
                    model=p_cfg.model,
                    api_key=p_cfg.api_key or "sk-dummy",
                    api_base=p_cfg.api_base,
                ))
            elif p_cfg.type.lower() == "anthropic":
                providers.append(AnthropicProvider(
                    name=p_cfg.name,
                    model=p_cfg.model,
                    api_key=p_cfg.api_key or "sk-dummy",
                ))

        if not providers:
            raise RuntimeError("未配置任何 Provider，请检查 config.yaml")

        return ProviderRouter(providers)

    def _load_system_prompt(self) -> str:
        """加载系统提示词（从配置或文件）。"""
        sys_prompt = self._config_obj.system_prompt or "你是一个智能助手，可以帮助用户完成各种任务。"

        if self._config_obj.system_prompt_file:
            prompt_path = Path(self._config_obj.system_prompt_file)
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
                    f"system_prompt_file {self._config_obj.system_prompt_file} not found. Using fallback."
                )

        return sys_prompt

    def _build_safety_guard(self, no_safety: bool = False) -> SafetyGuard | None:
        """构建安全守卫系统。"""
        safety_cfg = self._app_config.get("safety", {})

        if no_safety or not safety_cfg.get("enabled", False):
            return None

        # 加载策略规则
        rules_path = safety_cfg.get("rules_path", "./config/safety_rules.yaml")
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
                "default_action", safety_cfg.get("default_action", "allow")
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
        secrets_cfg = self._app_config.get("secrets", {})
        return SecretManager(
            env_prefix=secrets_cfg.get("env_prefix", "MYAGENT_SECRET_"),
            sensitive_fields=secrets_cfg.get("sensitive_fields"),
        )

    def _build_tool_manager(self) -> ToolManager:
        hr_cfg = self._config_obj.hot_reload
        tools_dir = (hr_cfg.watch_dir
                     if hr_cfg and hr_cfg.enabled
                     else "myagent/tools/tools_store")

        runner_cfg = self._app_config.get("sandbox", {})

        manager = ToolManager(tools_dir=tools_dir,
                              runner_config=runner_cfg)
        manager._register_builtin_tools()
        logger.info(
            "Registered builtin tools: cli_execute, file_read, file_write")
        return manager