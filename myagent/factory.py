"""
AgentFactory：统一的 Agent 构建工厂。
从 CLI 和 WebSocket 的重复构建逻辑中抽取，确保两个接口共享相同的组件初始化流程。

职责：
1. 加载配置文件（config.yaml）
2. 构建 ProviderRouter（多模型路由）
3. 构建安全系统（SafetyGuard + PolicyEngine + 规则链）
4. 构建沙盒（SubprocessSandbox / DockerSandbox）
5. 构建密钥管理（SecretManager）
6. 构建工具注册表（CLITool + FileTools + MCP Tools）
7. 构建审计日志器（AuditLogger）
8. 加载系统提示词
9. 组装 Agent 实例

注意：
- hooks 和 approval_handler 由调用方提供（CLI/WebSocket 的回调方式不同）
- 未来扩展：
  - [AUTH] 用户鉴权后可按用户维度隔离 Agent 实例
  - [MCP] FastMCP 协议工具将在此处注册到 ToolRegistry
"""
from pathlib import Path
from typing import Callable

import yaml

from myagent.core.agent import Agent
from myagent.core.hook import HookManager
from myagent.utils.config import load_yaml_config, AgentConfig, TimeoutConfig
from myagent.utils.logging import get_logger
from myagent.providers.openai_provider import OpenAIProvider
from myagent.providers.anthropic_provider import AnthropicProvider
from myagent.providers.router import ProviderRouter
from myagent.observability.audit_logger import AuditLogger
from myagent.observability.backends.jsonl_backend import JsonlAuditBackend
from myagent.safety.guard import SafetyGuard
from myagent.safety.policy import PolicyEngine
from myagent.safety.cli_fence import CLIFence
from myagent.safety.content_rules import InputContentFilter, OutputContentFilter
from myagent.tools.manager import ToolManager
from myagent.runtime.sandbox import SubprocessSandbox
from myagent.runtime.sandbox.subprocess_sandbox import ResourceLimits
from myagent.tools.builtin.cli_tool import CLITool
from myagent.tools.builtin.file_tools import FileReadTool, FileWriteTool
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
        state_store=None,
    ):
        """
        初始化工厂，加载并缓存配置。

        Args:
            config_path: YAML 配置文件路径
            state_store: 可选的 StateStore 实例（用于会话持久化）
        """
        self._config_path = config_path
        self._state_store = state_store

        # 加载配置（只加载一次）
        self._raw = load_yaml_config(config_path)
        self._app_config = self._raw.get("agent", self._raw) if self._raw else {}
        self._config_obj = AgentConfig(**self._app_config)

    @property
    def context_window_size(self) -> int:
        """获取上下文窗口大小（从第一个 provider 配置中读取）。"""
        if self._config_obj.providers:
            return self._config_obj.providers[0].context_window_size
        return 128000

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
        approval_handler=None,
        no_safety: bool = False,
    ) -> Agent:
        """
        创建完整的 Agent 实例。

        Args:
            hooks: 由调用方构建的 HookManager（CLI 用终端打印，WebSocket 用 JSON 推送）
            approval_handler: 可选的人工审批回调 async (tool_calls) -> list[bool]
            no_safety: 是否禁用安全检查（仅 CLI 调试用）

        Returns:
            完全配置好的 Agent 实例
        """
        # ── 1. 构建 ProviderRouter ──
        router = self._build_router()

        # ── 2. 构建审计日志器 ──
        audit_logger = self._build_audit_logger()

        # ── 3. 加载系统提示词 ──
        system_prompt = self._load_system_prompt()

        # ── 4. 构建安全系统 ──
        safety_guard = self._build_safety_guard(no_safety=no_safety)

        # ── 5. 构建沙盒 ──
        sandbox = self._build_sandbox()

        # ── 6. 构建密钥管理 ──
        secret_manager = self._build_secret_manager()

        # ── 7. 构建工具管理器 ──
        tool_manager = self._build_tool_manager(sandbox)

        # ── 8. 从分散配置构建 TimeoutConfig ──
        tools_cfg = self._app_config.get("tools", {})
        hitl_cfg = self._app_config.get("hitl", {})
        timeout_config = TimeoutConfig(
            llm_generation=self._config_obj.llm_timeout,
            tool_batch=tools_cfg.get("batch_timeout", 60.0),
            human_approval=hitl_cfg.get("approval_timeout", 300.0),
        )

        # ── 9. 组装 Agent ──
        agent = Agent(
            provider_router=router,
            hooks=hooks,
            tool_manager=tool_manager,
            system_prompt=system_prompt,
            max_iterations=self._config_obj.max_iterations,
            safety_guard=safety_guard,
            secret_manager=secret_manager,
            approval_handler=approval_handler,
            audit_logger=audit_logger,
            timeout_config=timeout_config,
            state_store=self._state_store,
            max_tokens_budget=self._config_obj.max_tokens_budget,
            context_window_size=self.context_window_size,
            tool_result_max_chars=self._config_obj.tool_result_max_chars,
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

    def _build_audit_logger(self) -> AuditLogger | None:
        """构建审计日志器。"""
        if not self._config_obj.audit.enabled:
            return None

        audit_dir = Path(self._config_obj.audit.jsonl_log_dir)
        audit_dir.mkdir(parents=True, exist_ok=True)
        jsonl_backend = JsonlAuditBackend(
            file_path=f"{self._config_obj.audit.jsonl_log_dir}/audit.jsonl"
        )
        return AuditLogger(backend=jsonl_backend)

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

    def _build_sandbox(self) -> SubprocessSandbox:
        """构建沙盒环境。

        TODO: [MCP] FastMCP 协议支持后，MCP 工具的沙盒可能需要不同的隔离策略。
        """
        sandbox_cfg = self._app_config.get("sandbox", {})
        return SubprocessSandbox(
            limits=ResourceLimits(
                max_cpu_seconds=sandbox_cfg.get("max_cpu_seconds", 30),
                max_memory_mb=sandbox_cfg.get("max_memory_mb", 512),
                max_output_bytes=sandbox_cfg.get("max_output_bytes", 102400),
                timeout_seconds=sandbox_cfg.get("timeout_seconds", 60.0),
            )
        )

    def _build_secret_manager(self) -> SecretManager:
        """构建密钥管理器。"""
        secrets_cfg = self._app_config.get("secrets", {})
        return SecretManager(
            env_prefix=secrets_cfg.get("env_prefix", "MYAGENT_SECRET_"),
            sensitive_fields=secrets_cfg.get("sensitive_fields"),
        )

    def _build_tool_manager(self, sandbox: SubprocessSandbox) -> ToolManager:
        """构建工具管理器。

        注册内置工具到 ToolManager。
        """
        hr_cfg = self._config_obj.hot_reload
        tools_dir = hr_cfg.watch_dir if hr_cfg and hr_cfg.enabled else "myagent/tools/tools_store"

        manager = ToolManager(tools_dir=tools_dir)
        manager.register(CLITool(sandbox))
        manager.register(FileReadTool())
        manager.register(FileWriteTool())
        logger.info("Registered builtin tools: cli_execute, file_read, file_write")
        return manager

