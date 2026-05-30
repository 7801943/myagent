import re

from myagent.core.hook import HookContext, HookManager
from myagent.context.manager import ContextManager
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

_COMMAND_RE = re.compile(r"^\s*/(\S+)(?:\s+(.*))?")

async def check_system_commands(
    context: ContextManager, 
    ctx: HookContext, 
    hooks: HookManager
) -> None:
    """
    检查用户最新消息中的系统控制指令（/model, /new, /clear 等）。
    通过 ctx.system_command_handler 回调处理。
    """
    await hooks.emit("state_change", ctx, state="system_check")

    messages = context.get_messages()
    user_msgs = [m for m in messages if getattr(m, "role", None) == "user"]
    if not user_msgs:
        return

    last_msg = user_msgs[-1]
    content = (getattr(last_msg, "content", None) or "").strip()
    m = _COMMAND_RE.match(content)
    if m:
        cmd = m.group(1)
        args = m.group(2) or ""
        await hooks.emit("system_command", ctx, command=cmd, args=args)
        if ctx.system_command_handler:
            await ctx.system_command_handler(cmd, args, ctx)
        logger.info(f"System command: /{cmd} {args}")
