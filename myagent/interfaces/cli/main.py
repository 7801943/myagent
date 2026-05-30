"""
CLI 主入口：交互式 ReAct 循环。
User Input → Session.chat() → Stream → UI 渲染。

Harness 重构：
  - 移除 AgentFactory 依赖
  - session.agent → session.harness
  - SessionManager 直接构建所有组件
"""
from __future__ import annotations

import asyncio
import click
import sys
from pathlib import Path

from myagent.core.session import Session, SessionManager
from myagent.core.models import UserContext
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


async def interactive_loop(session: Session) -> None:
    """启动交互式 CLI 循环。支持 @image <path> 语法附带图像。"""
    ui = CliUI()
    ui.print("🤖 MyAgent CLI — 输入 'exit' 或 'quit' 退出")
    ui.print("   💡 附带图像: 在消息中使用 @image <文件路径>")
    ui.print("   💡 示例: 描述这张图片 @image photo.jpg @image diagram.png\n")

    harness = session.harness

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
            # 解析 @image 指令
            text_parts, image_paths = _parse_image_refs(user_input)
            text = text_parts.strip()

            if image_paths:
                # 多模态模式
                from myagent.vision.image_handler import ImageHandler
                from myagent.context.message import ContentBlock

                provider = harness.router.current_provider
                provider_type = "anthropic" if provider and "anthropic" in provider.name else "openai"
                handler = ImageHandler(capabilities=provider.capabilities if provider else None)

                content_blocks: list[ContentBlock] = []
                if text:
                    content_blocks.append(ContentBlock(type="text", text=text))

                loaded = 0
                for img_path in image_paths:
                    p = Path(img_path)
                    if not p.exists():
                        ui.print(f"  ⚠ 图像文件不存在: {img_path}")
                        continue
                    size_kb = p.stat().st_size / 1024
                    block = await handler.prepare(img_path, provider_type=provider_type)
                    content_blocks.append(block)
                    loaded += 1
                    ui.print(f"  📎 已加载: {p.name} ({size_kb:.0f}KB)")

                if loaded == 0:
                    ui.print("  ❌ 没有成功加载任何图像")
                    continue

                ui.print(f"  → 共加载 {loaded} 张图像，正在发送...\n")
                ui.print("🤖 Assistant: ")
                response = await session.chat(content_blocks)
            else:
                # 纯文本模式
                ui.print("\n🤖 Assistant: ")
                response = await session.chat(text)

            ui.print(f"\n\n{response}\n")
        except Exception as e:
            ui.print_error(f"执行出错: {e}")
            logger.exception("Session chat failed")


def _parse_image_refs(user_input: str) -> tuple[str, list[str]]:
    """
    从用户输入中解析 @image <path> 引用。
    """
    import re
    image_paths: list[str] = []

    pattern = r'@image\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))'
    for match in re.finditer(pattern, user_input):
        path = match.group(1) or match.group(2) or match.group(3)
        image_paths.append(path)

    text = re.sub(pattern, '', user_input).strip()
    text = re.sub(r'\s+', ' ', text).strip()

    return text, image_paths


async def _chat(
    config_path: str, message: str | None, session_id: str | None, system_prompt: str | None,
    show_tools: bool = False, images: tuple = (), no_safety: bool = False
):
    # 创建 SessionManager（内部自动构建所有组件）
    session_manager = SessionManager(config_path=config_path)

    # 准备 UI
    ui = CliUI(show_tools=show_tools)

    # CLI 回调列表（注册后保存 HookHandle，用于 finally 清理）
    cli_hook_handles = []

    # 构建 CLI 审批 handler
    hitl_cfg = session_manager.config.hitl
    approval_handler = None
    if hitl_cfg.enabled:
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

    # 创建默认用户上下文
    user = UserContext(user_id="cli_default", username="CLI User")

    # 读取 root_dir 配置
    root_dir = session_manager.config.root_dir or None

    # 创建会话
    session = await session_manager.create_session(
        user=user,
        session_id=session_id,
        approval_handler=approval_handler,
        no_safety=no_safety,
        system_prompt=system_prompt,
        context_window_size=session_manager.context_window_size,
        workspace_root=root_dir,
    )

    # 将 CLI hooks 注册到 session 的 harness HookManager 上（全局 topic=None）
    harness = session.harness

    def _on_stream(ctx, delta):
        ui.print_stream_delta(delta)

    async def _on_thinking_stream(ctx, delta):
        ui.print_thinking_delta(delta)

    def _on_tool_start(ctx, tool_name, args, call_id):
        ui.print_tool_call(tool_name, args, call_id)

    def _on_tool_end(ctx, tool_name, result, call_id, latency_ms):
        ui.print_tool_result(tool_name, result.content, latency_ms)

    def _on_error(ctx, error):
        ui.print_error(str(error))

    def _on_timeout_warning(ctx, **kw):
        print_warning(kw.get("message", "操作超时"))

    cli_hook_handles.append(harness.hooks.on("stream", _on_stream))
    cli_hook_handles.append(harness.hooks.on("thinking_stream", _on_thinking_stream))
    cli_hook_handles.append(harness.hooks.on("tool_start", _on_tool_start))
    cli_hook_handles.append(harness.hooks.on("tool_end", _on_tool_end))
    cli_hook_handles.append(harness.hooks.on("error", _on_error))
    cli_hook_handles.append(harness.hooks.on("timeout_warning", _on_timeout_warning))

    # 工具热加载已在 SessionManager.create_session() 中通过
    # harness.tool_interface.start() 启动，无需重复调用

    try:
        if images:
            from myagent.vision.image_handler import ImageHandler
            from myagent.context.message import ContentBlock

            provider = harness.router.current_provider
            provider_type = "anthropic" if provider and "anthropic" in provider.name else "openai"
            handler = ImageHandler(capabilities=provider.capabilities if provider else None)

            content_blocks = []
            if message:
                content_blocks.append(ContentBlock(type="text", text=message))
            for img_path in images:
                block = await handler.prepare(img_path, provider_type=provider_type)
                content_blocks.append(block)

            try:
                response = await session.chat(content_blocks)
                ui.print(f"\n\n🤖 Assistant: {response}\n")
            except asyncio.CancelledError:
                ui.print("\n\n⚠ 操作已取消\n")
            except Exception as e:
                logger.error(f"Session chat error: {e}")
                ui.print_error(f"执行出错: {e}")
        elif message:
            try:
                response = await session.chat(message)
                ui.print(f"\n\n🤖 Assistant: {response}\n")
            except asyncio.CancelledError:
                ui.print("\n\n⚠ 操作已取消\n")
        else:
            await interactive_loop(session)
    finally:
        # 清理：停止 ToolManager（含热加载 + Proxy）
        try:
            await harness.tool_interface.stop()
        except Exception:
            pass

if __name__ == "__main__":
    cli()