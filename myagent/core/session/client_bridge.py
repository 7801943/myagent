"""
ClientBridge: WS 多客户端管理 + 审批桥接。

职责：
1. 管理多个 WS 客户端的 Hook 回调注册 / 取消
2. 事件广播到所有已连接的 WS 客户端
3. 人工审批桥接（Future 管理 + 广播审批请求到 WS → 等待客户端响应）

从 Session 提取，使 Session 退化为纯状态容器 + 转发层。
"""
import asyncio
from typing import Any
from uuid import uuid4

from myagent.context.message import ToolCall
from myagent.core.hook import HookHandle, HookManager
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


# ─── ClientHandle ────────────────────────────────────────────

class ClientHandle:
    """代表一个客户端的连接句柄，断开时调用 detach() 清理。"""

    def __init__(self, hook_handles: list[HookHandle], bridge: "ClientBridge", sender):
        self._hook_handles = hook_handles
        self._bridge = bridge
        self._sender = sender

    def detach(self) -> None:
        """断开连接时清理所有注册。"""
        for h in self._hook_handles:
            h.unregister()
        self._hook_handles.clear()
        self._bridge.remove_ws_notify(self._sender)


# ─── ClientBridge ────────────────────────────────────────────

class ClientBridge:
    """
    WS 多客户端管理器。

    持有 HookManager 引用 + session_id，负责：
      - attach_client: 注册 WS 事件回调到 HookManager
      - notify_clients: 广播自定义消息到所有 WS 客户端
      - approval_handler / resolve_approval: 人工审批 Future 桥接
    """

    def __init__(self, hooks: HookManager, session_id: str):
        self._hooks = hooks
        self._session_id = session_id
        self._ws_notifiers: list = []
        self._pending_approvals: dict[str, _PendingApproval] = {}

    # ── WS 通知管理 ──

    @property
    def has_clients(self) -> bool:
        return len(self._ws_notifiers) > 0

    def add_ws_notify(self, callback) -> None:
        if callback not in self._ws_notifiers:
            self._ws_notifiers.append(callback)

    def remove_ws_notify(self, callback) -> None:
        if callback in self._ws_notifiers:
            self._ws_notifiers.remove(callback)

    async def notify_clients(self, msg_type: str, data: dict) -> None:
        """广播自定义消息到所有已连接的 WS 客户端。"""
        for notify in list(self._ws_notifiers):
            try:
                await notify(msg_type, data)
            except Exception:
                logger.warning("Client notify failed (connection may be closed)")

    # ── 客户端连接管理 ──

    def attach_client(self, sender) -> ClientHandle:
        """
        将一个客户端（WebSocket）接入。
        所有回调通过 topic=session_id 注册到 HookManager。
        """
        hooks = self._hooks
        sid = self._session_id
        handles: list[HookHandle] = []

        async def _on_stream(ctx, delta):
            await sender({"type": "text_delta", "text": delta})

        async def _on_thinking_stream(ctx, delta):
            await sender({"type": "thinking_delta", "text": delta})

        async def _on_stream_start(ctx):
            await sender({"type": "stream_start"})

        async def _on_stream_end(ctx, resuming=False):
            await sender({"type": "stream_end", "resuming": resuming})

        async def _on_tool_start(ctx, tool_name, args, call_id):
            await sender({"type": "tool_start", "tool_name": tool_name,
                          "args": args, "call_id": call_id})

        async def _on_tool_end(ctx, tool_name, result, call_id, latency_ms):
            await sender({"type": "tool_end", "tool_name": tool_name,
                          "result": result.content, "latency_ms": latency_ms,
                          "call_id": call_id})

        async def _on_tool_error(ctx, tool_name, error, call_id):
            await sender({"type": "tool_error", "tool_name": tool_name,
                          "error": str(error), "call_id": call_id})

        async def _on_state_change(ctx, state):
            await sender({"type": "state_change", "state": state})

        async def _on_error(ctx, error):
            await sender({"type": "error", "message": str(error)})

        async def _on_safety_blocked(ctx, rule, reason, action, call_id="", tool_name=""):
            await sender({"type": "safety_blocked", "rule": rule,
                          "reason": reason, "action": action})

        async def _on_timeout_warning(ctx, **kw):
            await sender({"type": "timeout_warning", **kw})

        handles.append(hooks.on("stream", _on_stream, topic=sid))
        handles.append(hooks.on("thinking_stream", _on_thinking_stream, topic=sid))
        handles.append(hooks.on("stream_start", _on_stream_start, topic=sid))
        handles.append(hooks.on("stream_end", _on_stream_end, topic=sid))
        handles.append(hooks.on("tool_start", _on_tool_start, topic=sid))
        handles.append(hooks.on("tool_end", _on_tool_end, topic=sid))
        handles.append(hooks.on("tool_error", _on_tool_error, topic=sid))
        handles.append(hooks.on("state_change", _on_state_change, topic=sid))
        handles.append(hooks.on("error", _on_error, topic=sid))
        handles.append(hooks.on("safety_blocked", _on_safety_blocked, topic=sid))
        handles.append(hooks.on("timeout_warning", _on_timeout_warning, topic=sid))

        async def _ws_notify_wrapper(msg_type: str, data: dict) -> None:
            await sender({"type": msg_type, **data})

        self.add_ws_notify(_ws_notify_wrapper)
        return ClientHandle(handles, self, _ws_notify_wrapper)

    # ── 审批管理（原 VirtualApprovalHandler） ──

    async def approval_handler(self, tool_calls: list[ToolCall]) -> list[bool]:
        """
        会话级审批回调：广播 hitl_request → 等待客户端响应 → 返回决策。
        超时默认全部拒绝。
        """
        ticket_id = uuid4().hex[:8]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_approvals[ticket_id] = _PendingApproval(future=fut, tool_calls=tool_calls)

        await self.notify_clients("hitl_request", {
            "ticket_id": ticket_id,
            "tool_calls": [
                {"name": tc.name, "args": tc.arguments, "call_id": tc.id}
                for tc in tool_calls
            ],
        })

        try:
            decisions = await asyncio.wait_for(fut, timeout=120.0)
            return decisions
        except asyncio.TimeoutError:
            return [False] * len(tool_calls)
        finally:
            self._pending_approvals.pop(ticket_id, None)

    def resolve_approval(self, ticket_id: str, decisions: list[bool]) -> None:
        """任意客户端调用此方法完成审批。"""
        pa = self._pending_approvals.get(ticket_id)
        if pa and not pa.future.done():
            pa.future.set_result(decisions)


# ─── 内部数据类 ──────────────────────────────────────────────

class _PendingApproval:
    """等待审批的工单（内部使用）。"""
    __slots__ = ("future", "tool_calls")

    def __init__(self, future: asyncio.Future, tool_calls: list):
        self.future = future
        self.tool_calls = tool_calls
