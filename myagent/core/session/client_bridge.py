"""
ClientBridge: WS 多客户端管理 + 审批桥接。

职责：
1. 管理多个 WS 客户端的 EventBus 回调注册 / 取消
2. 事件广播到所有已连接的 WS 客户端
3. 人工审批桥接（Future 管理 + 广播审批请求到 WS → 等待客户端响应）

从 Session 提取，使 Session 退化为纯状态容器 + 转发层。
"""
import asyncio
from uuid import uuid4

from myagent.context.message import ToolCall
from myagent.core.events import (
    Error,
    EventBus,
    EventHandle,
    SafetyBlocked,
    StateChange,
    StreamDelta,
    StreamEnd,
    StreamStart,
    ThinkingDelta,
    TimeoutWarning,
    ToolEnd,
    ToolError,
    ToolStart,
)
from myagent.core.session.serializer import truncate_tool_display_content
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


# ─── ClientHandle ────────────────────────────────────────────

class ClientHandle:
    """代表一个客户端的连接句柄，断开时调用 detach() 清理。"""

    def __init__(self, event_handles: list[EventHandle], bridge: "ClientBridge", sender):
        self._event_handles = event_handles
        self._bridge = bridge
        self._sender = sender

    def detach(self) -> None:
        """断开连接时清理所有注册。"""
        for h in self._event_handles:
            h.unregister()
        self._event_handles.clear()
        self._bridge.remove_ws_notify(self._sender)


# ─── ClientBridge ────────────────────────────────────────────

class ClientBridge:
    """
    WS 多客户端管理器。

    持有 EventBus 引用 + session_id，负责：
      - attach_client: 注册 WS 事件回调到 EventBus
      - notify_clients: 广播自定义消息到所有 WS 客户端
      - approval_handler / resolve_approval: 人工审批 Future 桥接
    """

    def __init__(
        self,
        events: EventBus,
        session_id: str,
        approval_timeout: float = 300.0,
    ):
        self._events = events
        self._session_id = session_id
        self._approval_timeout = approval_timeout
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
        所有回调通过 topic=session_id 注册到 EventBus。
        """
        events = self._events
        sid = self._session_id
        handles: list[EventHandle] = []

        async def _on_stream(event: StreamDelta):
            await sender({"type": "text_delta", "text": event.delta})

        async def _on_thinking_stream(event: ThinkingDelta):
            await sender({"type": "thinking_delta", "text": event.delta})

        async def _on_stream_start(event: StreamStart):
            await sender({"type": "stream_start"})

        async def _on_stream_end(event: StreamEnd):
            await sender({"type": "stream_end", "resuming": event.resuming})

        async def _on_tool_start(event: ToolStart):
            await sender({"type": "tool_start", "tool_name": event.tool_name,
                          "args": event.args, "call_id": event.call_id})

        async def _on_tool_end(event: ToolEnd):
            # 前端 tool chip 只展示摘要，截断超长结果
            result_text = event.result.content
            if isinstance(result_text, str):
                result_text = truncate_tool_display_content(result_text)
            await sender({"type": "tool_end", "tool_name": event.tool_name,
                          "result": result_text, "latency_ms": event.latency_ms,
                          "call_id": event.call_id})

        async def _on_tool_error(event: ToolError):
            await sender({"type": "tool_error", "tool_name": event.tool_name,
                          "error": str(event.error), "call_id": event.call_id})

        async def _on_state_change(event: StateChange):
            await sender({"type": "state_change", "state": event.state})

        async def _on_error(event: Error):
            await sender({"type": "error", "message": str(event.error)})

        async def _on_safety_blocked(event: SafetyBlocked):
            await sender({"type": "safety_blocked", "rule": event.rule,
                          "reason": event.reason, "action": event.action})

        async def _on_timeout_warning(event: TimeoutWarning):
            await sender({"type": "timeout_warning", **event.payload()})

        handles.append(events.on(StreamDelta, _on_stream, topic=sid))
        handles.append(events.on(ThinkingDelta, _on_thinking_stream, topic=sid))
        handles.append(events.on(StreamStart, _on_stream_start, topic=sid))
        handles.append(events.on(StreamEnd, _on_stream_end, topic=sid))
        handles.append(events.on(ToolStart, _on_tool_start, topic=sid))
        handles.append(events.on(ToolEnd, _on_tool_end, topic=sid))
        handles.append(events.on(ToolError, _on_tool_error, topic=sid))
        handles.append(events.on(StateChange, _on_state_change, topic=sid))
        handles.append(events.on(Error, _on_error, topic=sid))
        handles.append(events.on(SafetyBlocked, _on_safety_blocked, topic=sid))
        handles.append(events.on(TimeoutWarning, _on_timeout_warning, topic=sid))

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
        decisions: list[bool] = []
        for tool_call in tool_calls:
            ticket_id = uuid4().hex[:12]
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._pending_approvals[ticket_id] = _PendingApproval(
                future=fut,
                tool_calls=[tool_call],
            )

            await self.notify_clients("hitl_request", {
                "ticket_id": ticket_id,
                "tool_name": tool_call.name,
                "args": tool_call.arguments,
                "call_id": tool_call.id,
                "reason": "安全策略要求人工审批",
                "timeout_seconds": self._approval_timeout,
            })

            try:
                response = await asyncio.wait_for(
                    fut,
                    timeout=self._approval_timeout,
                )
                decisions.append(bool(response[0]) if response else False)
            except asyncio.TimeoutError:
                decisions.append(False)
            finally:
                self._pending_approvals.pop(ticket_id, None)
        return decisions

    def resolve_approval(self, ticket_id: str, decisions: list[bool]) -> None:
        """任意客户端调用此方法完成审批。"""
        pa = self._pending_approvals.get(ticket_id)
        if pa and not pa.future.done():
            pa.future.set_result([bool(decisions[0])] if decisions else [False])


# ─── 内部数据类 ──────────────────────────────────────────────

class _PendingApproval:
    """等待审批的工单（内部使用）。"""
    __slots__ = ("future", "tool_calls")

    def __init__(self, future: asyncio.Future, tool_calls: list):
        self.future = future
        self.tool_calls = tool_calls
