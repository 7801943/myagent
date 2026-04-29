"""
CLI 主入口：交互式 ReAct 循环。
User Input → Agent.run() → Stream → UI 渲染。
Phase 2 增强：SafetyGuard + Sandbox + HITL + SecretManager + 工具注册。
"""
import asyncio
import click
import sys
from pathlib import Path

from myagent.core.agent import Agent
from myagent.core.hook import HookContext, HookManager
from myagent.core.cancellation import AgentCancelledError
from myagent.core.hitl import CLIHITLController
from myagent.interfaces.cli.ui import CliUI, print_warning
from myagent.utils.logging import get_logger, setup_logging
from myagent.utils.config import load_yaml_config, AgentConfig
from myagent.providers.openai_provider import OpenAIProvider
from myagent.providers.anthropic_provider import AnthropicProvider
from myagent.providers.router import ProviderRouter
from myagent.observability.audit_logger import AuditLogger
from myagent.observability.backends.jsonl_backend import JsonlAuditBackend

# Phase 2 imports
from myagent.safety.guard import SafetyGuard
from myagent.safety.policy import PolicyEngine
from myagent.safety.cli_fence import CLIFence
from myagent.safety.content_rules import InputContentFilter, OutputContentFilter
from myagent.tools.registry import ToolRegistry
from myagent.tools.sandbox import SubprocessSandbox
from myagent.tools.sandbox.subprocess_sandbox import ResourceLimits
from myagent.tools.cli_tool import CLITool
from myagent.tools.file_tools import FileReadTool, FileWriteTool
from myagent.tools.secrets import SecretManager

logger = get_logger(__name__)

@click.group()
@click.option("--config", default="config.yaml", help="配置文件路径")
@click.option("--log-level", default="INFO", help="日志级别")
@click.pass_context
def cli(ctx, config, log_level):
    """MyAgent — 全自研 Python Agent 框架"""
    setup_logging(level=log_level)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command()
@click.argument("message", required=False)
@click.option("--session-id", default=None, help="会话 ID（用于多轮对话）")
@click.option("--system-prompt", default=None, help="System Prompt")
@click.option("--show-tools", is_flag=True, help="显示工具调用详情")
@click.option("--image", multiple=True, help="附带图像文件路径（可多次指定）")
@click.option("--sandbox-backend", default="subprocess", type=click.Choice(["subprocess", "docker"]), help="沙盒后端选择")
@click.option("--no-safety", is_flag=True, help="禁用安全检查（仅开发调试）")
@click.pass_context
def chat(ctx, message, session_id, system_prompt, show_tools, image, sandbox_backend, no_safety):
    """与 Agent 对话"""
    asyncio.run(_chat(ctx.obj["config_path"], message, session_id, system_prompt, show_tools, image, sandbox_backend, no_safety))


async def interactive_loop(agent: Agent) -> None:
    """启动交互式 CLI 循环。"""
    ui = CliUI()
    ui.print("🤖 MyAgent CLI — 输入 'exit' 或 'quit' 退出\n")

    while True:
        try:
            user_input = input("👤 You: ").strip()
        except (EOFError, KeyboardInterrupt):
            ui.print("\n👋 再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            ui.print("👋 再见！")
            break

        try:
            ui.print("\n🤖 Assistant: ")
            response = await agent.run(user_input)
            ui.print(f"\n\n{response}\n")
        except Exception as e:
            ui.print_error(f"执行出错: {e}")
            logger.exception("Agent run failed")


async def _chat(
    config_path: str, message: str | None, session_id: str | None, system_prompt: str | None,
    show_tools: bool = False, images: tuple = (), sandbox_backend: str = "subprocess", no_safety: bool = False
):
    # 加载配置
    raw = load_yaml_config(config_path)
    # config.yaml 顶层可能是 agent: {...}
    app_config = raw.get("agent", raw) if raw else {}
    config_obj = AgentConfig(**app_config)

    # 构建 Provider
    providers = []
    for p_cfg in config_obj.providers:
        if p_cfg.type.lower() == "openai":
            providers.append(OpenAIProvider(
                name=p_cfg.name,
                model=p_cfg.model,
                api_key=p_cfg.api_key or "sk-dummy",  # 防止空 key 报错
                api_base=p_cfg.api_base,
            ))
        elif p_cfg.type.lower() == "anthropic":
            providers.append(AnthropicProvider(
                name=p_cfg.name,
                model=p_cfg.model,
                api_key=p_cfg.api_key or "sk-dummy",
            ))

    if not providers:
        print("❌ 未配置任何 Provider，请检查 config.yaml")
        sys.exit(1)

    router = ProviderRouter(providers)

    # 准备 Hooks
    hooks = HookManager()
    ui = CliUI(show_tools=show_tools)

    @hooks.hook("stream")
    async def _on_stream(ctx, delta):
        ui.print_stream_delta(delta)

    @hooks.hook("thinking_stream")
    async def _on_thinking_stream(ctx, delta):
        ui.print_thinking_delta(delta)

    @hooks.hook("tool_start")
    async def _on_tool_start(ctx, tool_name, args, call_id):
        ui.print_tool_call(tool_name, args, call_id)

    @hooks.hook("tool_end")
    async def _on_tool_end(ctx, tool_name, result, call_id, latency_ms):
        ui.print_tool_result(tool_name, result.content, latency_ms)

    @hooks.hook("error")
    async def _on_error(ctx, error):
        ui.print_error(str(error))

    # 注册超时警告回调（看门狗发出 timeout_warning 时打印黄色警告）
    hooks.on("timeout_warning", lambda ctx, **kw: print_warning(kw.get("message", "操作超时")))

    # 审计：直接传 AuditLogger 给 Agent（内联），不再通过 Hook 注册
    audit_logger = None
    if config_obj.audit.enabled:
        jsonl_backend = JsonlAuditBackend(file_path=f"{config_obj.audit.jsonl_log_dir}/audit.jsonl")
        audit_logger = AuditLogger(backend=jsonl_backend)

    # 准备系统提示词
    sys_prompt_base = config_obj.system_prompt or "你是一个智能助手，可以帮助用户完成各种任务。"
    if config_obj.system_prompt_file:
        prompt_path = Path(config_obj.system_prompt_file)
        if prompt_path.exists():
            lines = []
            with open(prompt_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                        continue
                    lines.append(line.rstrip('\n'))
            sys_prompt_base = "\n".join(lines)
        else:
            print(f"Warning: system_prompt_file {config_obj.system_prompt_file} not found. Using fallback.")
    sys_prompt = system_prompt or sys_prompt_base

    # ── Phase 2: 构建安全系统 ──
    safety_cfg = app_config.get("safety", {})
    safety_guard = None

    if not no_safety and safety_cfg.get("enabled", False):
        # 加载策略规则
        rules_path = safety_cfg.get("rules_path", "./config/safety_rules.yaml")
        rules_cfg = {}
        if Path(rules_path).exists():
            import yaml
            with open(rules_path) as f:
                rules_cfg = yaml.safe_load(f) or {}

        policy_cfg = rules_cfg.get("policy_engine", {})
        tool_policies = policy_cfg.get("tool_policies", [])

        policy_engine = PolicyEngine(
            tool_policies=tool_policies,
            default_action=policy_cfg.get("default_action", safety_cfg.get("default_action", "allow")),
        )

        cli_fence_cfg = rules_cfg.get("cli_fence", {})

        # 构建规则链
        rules: list = [
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

    # ── Phase 2: 构建沙盒 ──
    sandbox_cfg = app_config.get("sandbox", {})
    sandbox = SubprocessSandbox(
        limits=ResourceLimits(
            max_cpu_seconds=sandbox_cfg.get("max_cpu_seconds", 30),
            max_memory_mb=sandbox_cfg.get("max_memory_mb", 512),
            max_output_bytes=sandbox_cfg.get("max_output_bytes", 102400),
            timeout_seconds=sandbox_cfg.get("timeout_seconds", 60.0),
        )
    )

    # ── Phase 2: 构建密钥管理 ──
    secrets_cfg = app_config.get("secrets", {})
    secret_manager = SecretManager(
        env_prefix=secrets_cfg.get("env_prefix", "MYAGENT_SECRET_"),
        sensitive_fields=secrets_cfg.get("sensitive_fields"),
    )

    # ── Phase 2: 构建 HITL 控制器 ──
    hitl_cfg = app_config.get("hitl", {})
    hitl_callback = None
    if hitl_cfg.get("enabled", True):
        hitl_controller = CLIHITLController(timeout=hitl_cfg.get("timeout", 120))
        hitl_callback = hitl_controller.request_approval

    # ── Phase 2: 注册工具 ──
    tool_registry = ToolRegistry()
    tool_registry.register(CLITool(sandbox))
    tool_registry.register(FileReadTool())
    tool_registry.register(FileWriteTool())
    logger.info("Registered tools: cli_execute, file_read, file_write")

    # 构建 Agent（Phase 2 增强版 — HookManager + 审计内联 + 超时配置）
    agent = Agent(
        provider_router=router,
        hooks=hooks,
        tool_registry=tool_registry,
        system_prompt=sys_prompt,
        max_iterations=config_obj.max_iterations,
        safety_guard=safety_guard,
        secret_manager=secret_manager,
        hitl_callback=hitl_callback,
        audit_logger=audit_logger,
        timeout_config=config_obj.timeout,
    )

    if images:
        from myagent.vision.image_handler import ImageHandler
        from myagent.context.message import ContentBlock

        provider = router.current_provider
        provider_type = "anthropic" if provider and "anthropic" in provider.name else "openai"
        handler = ImageHandler(capabilities=provider.capabilities if provider else None)

        content_blocks = []
        if message:
            content_blocks.append(ContentBlock(type="text", text=message))
        for img_path in images:
            block = await handler.prepare(img_path, provider_type=provider_type)
            content_blocks.append(block)
        
        # 使用 Agent.run() 统一入口（支持取消 + 审计内联）
        agent._context.add_user_message(content_blocks)
        try:
            response = await agent.run("")  # 消息已手动添加到上下文
            ui.print(f"\n\n🤖 Assistant: {response}\n")
        except AgentCancelledError:
            ui.print("\n\n⚠ 操作已取消\n")
        except Exception as e:
            logger.error(f"Agent run error: {e}")
            ui.print_error(f"执行出错: {e}")
    elif message:
        try:
            response = await agent.run(message)
            ui.print(f"\n\n🤖 Assistant: {response}\n")
        except AgentCancelledError:
            ui.print("\n\n⚠ 操作已取消\n")
    else:
        await interactive_loop(agent)

if __name__ == "__main__":
    cli()