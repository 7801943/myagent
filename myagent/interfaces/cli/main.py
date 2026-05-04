"""
CLI 主入口：交互式 ReAct 循环。
User Input → Agent.run() → Stream → UI 渲染。

重构：使用 AgentFactory 替代手动构建 Agent，消除与 WebSocket 的重复代码。
"""
from __future__ import annotations

import asyncio
import click
import sys

from myagent.core.agent import Agent
from myagent.core.factory import AgentFactory
from myagent.core.hook import HookManager
from myagent.core.cancellation import AgentCancelledError
from myagent.context.message import ToolCall
from myagent.interfaces.cli.ui import CliUI, print_warning
from myagent.utils.logging import get_logger, setup_logging

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
@click.option("--no-safety", is_flag=True, help="禁用安全检查（仅开发调试）")
@click.pass_context
def chat(ctx, message, session_id, system_prompt, show_tools, image, no_safety):
    """与 Agent 对话"""
    asyncio.run(_chat(ctx.obj["config_path"], message, session_id, system_prompt, show_tools, image, no_safety))


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
    show_tools: bool = False, images: tuple = (), no_safety: bool = False
):
    # 使用 AgentFactory 构建 Agent（共享构建逻辑）
    factory = AgentFactory(config_path=config_path)

    # 准备 Hooks（CLI 特有的终端打印回调）
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

    # 注册超时警告回调
    hooks.on("timeout_warning", lambda ctx, **kw: print_warning(kw.get("message", "操作超时")))

    # 构建 CLI 审批 handler
    hitl_cfg = factory.app_config.get("hitl", {})
    approval_handler = None
    if hitl_cfg.get("enabled", True):
        async def _cli_approval_handler(tool_calls: list[ToolCall]) -> list[bool]:
            """CLI 人工审批：逐个询问用户是否批准工具调用。"""
            decisions = []
            for tc in tool_calls:
                ui.print(f"\n⚠ 工具需要审批: {tc.name}")
                ui.print(f"  参数: {tc.arguments}")
                choice = input("  批准执行？[y/N]: ").strip().lower()
                decisions.append(choice in ("y", "yes"))
            return decisions
        approval_handler = _cli_approval_handler

    # 通过工厂创建 Agent
    agent = factory.create_agent(
        hooks=hooks,
        approval_handler=approval_handler,
        no_safety=no_safety,
    )

    # 创建会话（如果指定了 session_id）
    session = agent.create_session(session_id=session_id)

    # 覆盖系统提示词（如果命令行指定了）
    if system_prompt:
        session.context.set_system(system_prompt)

    if images:
        from myagent.vision.image_handler import ImageHandler
        from myagent.context.message import ContentBlock

        provider = agent._router.current_provider
        provider_type = "anthropic" if provider and "anthropic" in provider.name else "openai"
        handler = ImageHandler(capabilities=provider.capabilities if provider else None)

        content_blocks = []
        if message:
            content_blocks.append(ContentBlock(type="text", text=message))
        for img_path in images:
            block = await handler.prepare(img_path, provider_type=provider_type)
            content_blocks.append(block)
        
        session.context.add_user_message(content_blocks)
        try:
            response = await agent.run("")
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