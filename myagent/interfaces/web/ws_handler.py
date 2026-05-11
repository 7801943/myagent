"""
WebSocket Handler：处理 WebSocket 连接的完整生命周期。

Phase 1 变更：
  - 使用 SessionManager 创建/恢复/删除会话
  - 使用 UserContext 标识用户
  - 使用 Session.chat() 替代 Agent.run()
  - 导入路径 myagent.factory → myagent.core.factory
  - 导入路径 myagent.core.session → Session（新）
"""
import asyncio
import json
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from myagent.core.agent import Agent
from myagent.core.factory import AgentFactory
from myagent.core.hook import HookManager
from myagent.core.session import Session
from myagent.core.session_manager import SessionManager, UserContext
from myagent.context.message import ToolCall
from myagent.context.state import StateStore
from myagent.interfaces.websocket.lock import WebSocketLock
from myagent.interfaces.web.ws_models import INCOMING_MESSAGE_TYPES
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class WebSocketApprovalHandler:
    """
    WebSocket 模式下的人工审批 handler。
    签名：async (tool_calls: list[ToolCall]) -> list[bool]
    """

    def __init__(self, websocket: WebSocket):
        self._ws = websocket
        self._pending_futures: dict[str, asyncio.Future] = {}

    async def __call__(self, tool_calls: list[ToolCall]) -> list[bool]:
        """approval_handler 接口：逐个发送审批请求，等待所有回复。"""
        futures = {}
        for tc in tool_calls:
            fut = asyncio.get_event_loop().create_future()
            self._pending_futures[tc.id] = fut
            futures[tc.id] = fut

        for tc in tool_calls:
            args = tc.arguments
            if isinstance(args, str):
                try:
                    import json as _json
                    args = _json.loads(args)
                except Exception:
                    pass
            try:
                await self._ws.send_text(json.dumps({
                    "type": "hitl_request",
                    "tool_name": tc.name,
                    "reason": f"工具 '{tc.name}' 需要人工审批",
                    "args": args,
                    "call_id": tc.id,
                }, ensure_ascii=False))
            except Exception:
                self._pending_futures.pop(tc.id, None)
                futures.pop(tc.id, None)

        if not futures:
            return [False] * len(tool_calls)

        decisions = []
        for tc in tool_calls:
            if tc.id not in futures:
                decisions.append(False)
                continue
            try:
                result = await asyncio.wait_for(futures[tc.id], timeout=120.0)
                decisions.append(result)
            except asyncio.TimeoutError:
                decisions.append(False)
            finally:
                self._pending_futures.pop(tc.id, None)

        return decisions

    def handle_response(self, call_id: str, approved: bool) -> None:
        """处理客户端发来的审批回复。"""
        fut = self._pending_futures.pop(call_id, None)
        if fut and not fut.done():
            fut.set_result(approved)


def _serialize_messages(messages: list) -> list[dict]:
    """将消息列表序列化为前端可显示的 dict 列表。"""
    history = []
    for msg in messages:
        entry: dict = {"role": msg.role, "content": ""}

        if hasattr(msg, 'content') and msg.content:
            if isinstance(msg.content, str):
                entry["content"] = msg.content
            elif isinstance(msg.content, list):
                parts = []
                for block in msg.content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(block.get("text") or "")
                    elif hasattr(block, 'type') and block.type == "text":
                        parts.append(getattr(block, 'text', '') or "")
                entry["content"] = "".join(parts)
            else:
                entry["content"] = str(msg.content)

        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": getattr(tc, 'id', None) if not isinstance(tc, dict) else tc.get('id'),
                    "name": getattr(tc, 'name', None) if not isinstance(tc, dict) else tc.get('name'),
                    "arguments": getattr(tc, 'arguments', {}) if not isinstance(tc, dict) else tc.get('arguments', {}),
                }
                for tc in msg.tool_calls
            ]

        if hasattr(msg, 'tool_call_id') and msg.tool_call_id:
            entry["tool_call_id"] = msg.tool_call_id
        if hasattr(msg, 'tool_name') and msg.tool_name:
            entry["tool_name"] = msg.tool_name
        if hasattr(msg, 'metadata') and msg.metadata:
            entry["metadata"] = msg.metadata

        history.append(entry)

    return history


class WebSocketHandler:
    """
    WebSocket 连接处理器。
    使用 SessionManager 管理会话，Session.chat() 执行对话。
    """

    def __init__(
        self,
        websocket: WebSocket,
        session_manager: SessionManager,
        state_store: StateStore,
    ):
        self._ws = websocket
        self._session_manager = session_manager
        self._state_store = state_store
        self._ws_lock = WebSocketLock()
        self._session: Session | None = None
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._session_id: str = ""
        self._approval_handler: WebSocketApprovalHandler | None = None
        self._hooks: HookManager | None = None

    async def run(self) -> None:
        """WebSocket 连接主循环。"""
        await self._ws.accept()

        # 初始化会话
        self._session_id = uuid4().hex[:16]
        logger.info(f"WebSocket client connected, session: {self._session_id}")

        # 发送连接确认（含上下文窗口大小）
        factory = self._session_manager._factory
        context_window_size = factory.context_window_size
        await self._send_json({
            "type": "connected",
            "session_id": self._session_id,
            "context_window_size": context_window_size,
        })

        # 构建 hooks 和 approval_handler
        self._approval_handler = WebSocketApprovalHandler(self._ws)
        self._hooks = HookManager()
        self._register_ws_hooks()

        # 创建默认用户上下文
        user = UserContext(user_id="ws_default", username="WebSocket User")

        # 通过 SessionManager 创建初始会话
        try:
            self._session = self._session_manager.create_session(
                user=user,
                session_id=self._session_id,
                hooks=self._hooks,
                approval_handler=self._approval_handler,
                context_window_size=context_window_size,
            )
            # 启动工具热加载
            try:
                await self._session._agent.start_hot_reload()
            except Exception as e:
                logger.warning(f"Hot reload start failed (non-fatal): {e}")
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            await self._send_json({"type": "error", "message": f"会话初始化失败: {e}"})
            await self._ws.close()
            return

        try:
            async for raw_message in self._ws.iter_text():
                await self._dispatch_message(raw_message)

        except WebSocketDisconnect:
            logger.info(f"WebSocket client disconnected, session: {self._session_id}")
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
        finally:
            # 清理：停止热加载器
            if self._session and self._session._agent:
                await self._session._agent.stop_hot_reload()
            self._cleanup()

    def _register_ws_hooks(self) -> None:
        """注册 WebSocket Hook 回调：将 Agent 事件推送到 WebSocket。"""

        @self._hooks.hook("stream")
        async def _on_stream(ctx, delta):
            await self._send_json({"type": "text_delta", "text": delta})

        @self._hooks.hook("thinking_stream")
        async def _on_thinking_stream(ctx, delta):
            await self._send_json({"type": "thinking_delta", "text": delta})

        @self._hooks.hook("stream_start")
        async def _on_stream_start(ctx):
            await self._send_json({"type": "stream_start"})

        @self._hooks.hook("stream_end")
        async def _on_stream_end(ctx, resuming=False):
            await self._send_json({"type": "stream_end", "resuming": resuming})

        @self._hooks.hook("tool_start")
        async def _on_tool_start(ctx, tool_name, args, call_id):
            await self._send_json({
                "type": "tool_start",
                "tool_name": tool_name,
                "args": args,
                "call_id": call_id,
            })

        @self._hooks.hook("tool_end")
        async def _on_tool_end(ctx, tool_name, result, call_id, latency_ms):
            await self._send_json({
                "type": "tool_end",
                "tool_name": tool_name,
                "result": result.content,
                "latency_ms": latency_ms,
                "call_id": call_id,
            })

        @self._hooks.hook("tool_error")
        async def _on_tool_error(ctx, tool_name, error, call_id):
            await self._send_json({
                "type": "tool_error",
                "tool_name": tool_name,
                "error": str(error),
                "call_id": call_id,
            })

        @self._hooks.hook("safety_blocked")
        async def _on_safety_blocked(ctx, rule, reason, action, call_id="", tool_name=""):
            await self._send_json({
                "type": "safety_blocked",
                "rule": rule,
                "reason": reason,
                "action": action,
                "call_id": call_id,
                "tool_name": tool_name,
            })

        @self._hooks.hook("state_change")
        async def _on_state_change(ctx, state):
            await self._send_json({"type": "state_change", "state": state})

        @self._hooks.hook("error")
        async def _on_error(ctx, error):
            await self._send_json({"type": "error", "message": str(error)})

        # 超时警告
        async def _on_timeout_warning(ctx, **kw):
            await self._send_json({
                "type": "timeout_warning",
                "stage": kw.get("stage", ""),
                "timeout_seconds": kw.get("timeout_seconds", 0),
                "message": kw.get("message", "操作超时"),
            })
        self._hooks.on("timeout_warning", _on_timeout_warning)

    async def _dispatch_message(self, raw_message: str) -> None:
        """解析并路由 WebSocket 消息。"""
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            await self._send_json({"type": "error", "message": "无效的 JSON 消息"})
            return

        msg_type = data.get("type", "")

        # 尝试 Pydantic 校验
        model_cls = INCOMING_MESSAGE_TYPES.get(msg_type)
        if model_cls:
            try:
                model_cls(**data)
            except Exception as e:
                await self._send_json({"type": "error", "message": f"消息格式错误: {e}"})
                return
        else:
            await self._send_json({"type": "error", "message": f"未知的消息类型: {msg_type}"})
            return

        # 路由到对应处理器
        if msg_type == "chat":
            asyncio.create_task(self._handle_chat(data))
        elif msg_type == "cancel":
            await self._handle_cancel()
        elif msg_type == "hitl_response":
            self._handle_approval_response(data)
        elif msg_type == "session_list":
            await self._handle_session_list()
        elif msg_type == "session_create":
            await self._handle_session_create()
        elif msg_type == "session_switch":
            await self._handle_session_switch(data)
        elif msg_type == "session_delete":
            await self._handle_session_delete(data)
        elif msg_type == "ping":
            await self._send_json({"type": "pong"})

    async def _handle_chat(self, data: dict) -> None:
        """处理聊天消息。使用 Session.chat()。"""
        user_text = data.get("text", "").strip()
        if not user_text:
            await self._send_json({"type": "error", "message": "消息内容不能为空"})
            return

        session_id = self._session_id

        # 获取会话锁
        if self._ws_lock.get_lock(session_id).locked():
            await self._send_json({"type": "error", "message": "上一条消息正在处理中，请等待完成"})
            return

        await self._ws_lock.acquire(session_id)
        try:
            session = self._session
            if not session:
                await self._send_json({"type": "error", "message": "会话不存在"})
                return

            task = asyncio.create_task(session.chat(user_text))
            self._running_tasks[session_id] = task

            try:
                response = await task
            except asyncio.CancelledError:
                logger.info(f"Session cancelled (session={session_id})")
                await self._send_json({
                    "type": "message_end",
                    "text": "操作已取消",
                    "stop_reason": "cancelled",
                })
                return
            finally:
                self._running_tasks.pop(session_id, None)

            # 携带上下文使用量信息
            context_usage = self._build_context_usage(session)
            await self._send_json({
                "type": "message_end",
                "text": response,
                "stop_reason": "completed",
                "context_usage": context_usage,
            })

        except Exception as e:
            logger.error(f"Session chat error (session={session_id}): {e}")
            await self._send_json({"type": "error", "message": f"Agent 执行出错: {e}"})
        finally:
            self._ws_lock.release(session_id)

    async def _handle_cancel(self) -> None:
        """处理取消请求。使用 Session.request_cancel()。"""
        session = self._session
        if session:
            session.request_cancel("user_cancelled", "用户通过 WebSocket 取消")
        else:
            task = self._running_tasks.get(self._session_id)
            if task and not task.done():
                task.cancel()
            else:
                await self._send_json({"type": "error", "message": "当前没有正在运行的任务"})
            return

        # 兜底：session.request_cancel 已调用 task.cancel()
        task = self._running_tasks.get(self._session_id)
        if task and not task.done():
            task.cancel()
        else:
            await self._send_json({"type": "error", "message": "当前没有正在运行的任务"})

    def _handle_approval_response(self, data: dict) -> None:
        """处理人工审批回复。"""
        call_id = data.get("call_id", "")
        approved = data.get("approved", False)
        if self._approval_handler:
            self._approval_handler.handle_response(call_id, approved)

    async def _handle_session_list(self) -> None:
        """处理会话列表请求。"""
        sessions = await self._session_manager.list_sessions()

        for s in sessions:
            try:
                messages = await self._session_manager.get_session_messages(s["session_id"])
                first_user = next((m for m in messages if m.role == "user"), None)
                content = ""
                if first_user and hasattr(first_user, 'content') and first_user.content:
                    if isinstance(first_user.content, str):
                        content = first_user.content
                    elif isinstance(first_user.content, list):
                        content = "".join(
                            (b.get('text') or "") if isinstance(b, dict) else (getattr(b, 'text', None) or "")
                            for b in first_user.content
                            if (b.get('type') if isinstance(b, dict) else getattr(b, 'type', '')) == 'text'
                        )
                    else:
                        content = str(first_user.content)
                s["title"] = content[:50] if content else "新对话"
                s["message_count"] = len(messages)
            except Exception as e:
                logger.warning(f"Failed to load session title: {e}")
                s["title"] = "新对话"
                s["message_count"] = 0

        await self._send_json({"type": "session_list_result", "sessions": sessions})

    async def _handle_session_create(self) -> None:
        """处理创建新会话请求。"""
        new_session_id = uuid4().hex[:16]

        # 取消旧会话的运行任务
        old_task = self._running_tasks.pop(self._session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        self._ws_lock.cleanup(self._session_id)

        # 通过 SessionManager 创建新会话
        user = UserContext(user_id="ws_default", username="WebSocket User")
        factory = self._session_manager._factory
        self._session = self._session_manager.create_session(
            user=user,
            session_id=new_session_id,
            hooks=self._hooks,
            approval_handler=self._approval_handler,
            context_window_size=factory.context_window_size,
        )
        self._session_id = new_session_id

        await self._send_json({"type": "session_created", "session_id": new_session_id})

    async def _handle_session_switch(self, data: dict) -> None:
        """处理切换会话请求。"""
        target_id = data.get("session_id", "")
        if not target_id:
            await self._send_json({"type": "error", "message": "缺少 session_id"})
            return

        try:
            # 先检查内存中是否存在
            existing = self._session_manager.get_session(target_id)
            if existing:
                self._session = existing
            else:
                # 从 StateStore 恢复
                user = UserContext(user_id="ws_default", username="WebSocket User")
                factory = self._session_manager._factory
                self._session = await self._session_manager.restore_session(
                    session_id=target_id,
                    user=user,
                    hooks=self._hooks,
                    approval_handler=self._approval_handler,
                    context_window_size=factory.context_window_size,
                )
        except Exception as e:
            await self._send_json({"type": "error", "message": f"切换会话失败: {e}"})
            return

        # 取消旧会话任务
        old_task = self._running_tasks.pop(self._session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        self._ws_lock.cleanup(self._session_id)
        self._session_id = target_id

        # 加载历史消息
        messages = await self._session_manager.get_session_messages(target_id)
        history = _serialize_messages(messages)

        await self._send_json({
            "type": "session_switched",
            "session_id": target_id,
            "messages": history,
        })

    async def _handle_session_delete(self, data: dict) -> None:
        """处理删除会话请求。"""
        target_id = data.get("session_id", "")
        if not target_id:
            await self._send_json({"type": "error", "message": "缺少 session_id"})
            return

        await self._session_manager.delete_session(target_id)
        self._running_tasks.pop(target_id, None)

        await self._send_json({"type": "session_deleted", "session_id": target_id})

    def _build_context_usage(self, session: Session) -> dict:
        """构建上下文使用量信息，用于前端进度条展示。"""
        ctx = session.context
        used = ctx.last_usage_input_tokens
        window_size = ctx.context_window_size
        percentage = round(used / window_size * 100, 1) if window_size > 0 else 0
        return {
            "used_tokens": used,
            "context_window_size": window_size,
            "percentage": percentage,
        }

    def _cleanup(self) -> None:
        """连接断开时清理资源。"""
        self._running_tasks.pop(self._session_id, None)
        self._ws_lock.cleanup(self._session_id)

    async def _send_json(self, data: dict) -> None:
        """安全发送 JSON 消息到 WebSocket 客户端。"""
        try:
            await self._ws.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.warning("WebSocket send failed (connection may be closed)")