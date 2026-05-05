"""
WebSocket Handler：处理 WebSocket 连接的完整生命周期。
从 server.py 迁移并适配 FastAPI WebSocket API。

职责：
1. WebSocket 连接管理（accept/close/心跳）
2. 消息路由（基于 Pydantic 模型校验）
3. Agent 生命周期管理（创建/运行/取消）
4. 会话管理（创建/切换/列表/删除）— 通过 Agent 的 Session 工厂
5. 人工审批控制器（approval_handler 模式）
6. Hook 回调 → WebSocket JSON 推送

重构变更：
- 单 Agent 多会话模式（Agent 持有共享组件，Session 管理独立上下文）
- approval_handler 替代旧的 HITLController（批量审批 + 异步 Future）
- 删除对 hitl.py / HITLController 的依赖
"""
import asyncio
import json
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from myagent.core.agent import Agent
from myagent.factory import AgentFactory
from myagent.core.hook import HookManager
from myagent.core.session import Session
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
    
    工作流程：
    1. HumanTurn 调用 __call__(tool_calls)
    2. 向 WS 客户端发送批量审批请求
    3. 等待客户端逐个回复（通过 Future）
    4. 返回决策列表
    """

    def __init__(self, websocket: WebSocket):
        self._ws = websocket
        self._pending_futures: dict[str, asyncio.Future] = {}

    async def __call__(self, tool_calls: list[ToolCall]) -> list[bool]:
        """approval_handler 接口：逐个发送审批请求，等待所有回复。"""
        # 为每个 tool_call 创建 Future
        futures = {}
        for tc in tool_calls:
            fut = asyncio.get_event_loop().create_future()
            self._pending_futures[tc.id] = fut
            futures[tc.id] = fut

        # 逐个发送 hitl_request 消息（匹配前端期望的消息格式）
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
                # 发送失败，该调用拒绝
                self._pending_futures.pop(tc.id, None)
                futures.pop(tc.id, None)

        # 如果全部发送失败，返回全部拒绝
        if not futures:
            return [False] * len(tool_calls)

        # 等待所有回复（带超时）
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
    """
    将消息列表序列化为前端可显示的 dict 列表。
    兼容 str / list / ContentBlock 等多种 content 格式。
    """
    history = []
    for msg in messages:
        entry: dict = {"role": msg.role, "content": ""}

        # 提取文本内容
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

        # 工具调用信息
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
    单 Agent 多会话模式：一个 Agent 实例管理多个 Session。
    """

    def __init__(
        self,
        websocket: WebSocket,
        factory: AgentFactory,
        state_store: StateStore,
    ):
        self._ws = websocket
        self._factory = factory
        self._state_store = state_store
        self._ws_lock = WebSocketLock()
        self._agent: Agent | None = None
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._session_id: str = ""
        self._approval_handler: WebSocketApprovalHandler | None = None

    async def run(self) -> None:
        """WebSocket 连接主循环。"""
        await self._ws.accept()

        # 初始化会话
        self._session_id = uuid4().hex[:16]
        logger.info(f"WebSocket client connected, session: {self._session_id}")

        # 发送连接确认（含上下文窗口大小）
        context_window_size = self._factory.config.context_window_size
        await self._send_json({
            "type": "connected",
            "session_id": self._session_id,
            "context_window_size": context_window_size,
        })

        # 构建 Agent（单 Agent 多会话）
        self._approval_handler = WebSocketApprovalHandler(self._ws)

        try:
            self._agent = self._build_agent()
        except Exception as e:
            logger.error(f"Failed to build agent: {e}")
            await self._send_json({"type": "error", "message": f"Agent 初始化失败: {e}"})
            await self._ws.close()
            return

        # 创建初始会话
        self._agent.create_session(session_id=self._session_id)

        try:
            async for raw_message in self._ws.iter_text():
                await self._dispatch_message(raw_message)

        except WebSocketDisconnect:
            logger.info(f"WebSocket client disconnected, session: {self._session_id}")
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
        finally:
            # 清理：停止热加载器
            if self._agent:
                await self._agent.stop_hot_reload()
            self._cleanup()

    def _build_agent(self) -> Agent:
        """构建 Agent 实例，注册 WebSocket Hook 回调。"""
        hooks = HookManager()

        # ── 注册 Hook 回调：将 Agent 事件推送到 WebSocket ──

        @hooks.hook("stream")
        async def _on_stream(ctx, delta):
            await self._send_json({"type": "text_delta", "text": delta})

        @hooks.hook("thinking_stream")
        async def _on_thinking_stream(ctx, delta):
            await self._send_json({"type": "thinking_delta", "text": delta})

        @hooks.hook("stream_start")
        async def _on_stream_start(ctx):
            await self._send_json({"type": "stream_start"})

        @hooks.hook("stream_end")
        async def _on_stream_end(ctx, resuming=False):
            await self._send_json({"type": "stream_end", "resuming": resuming})

        @hooks.hook("tool_start")
        async def _on_tool_start(ctx, tool_name, args, call_id):
            await self._send_json({
                "type": "tool_start",
                "tool_name": tool_name,
                "args": args,
                "call_id": call_id,
            })

        @hooks.hook("tool_end")
        async def _on_tool_end(ctx, tool_name, result, call_id, latency_ms):
            await self._send_json({
                "type": "tool_end",
                "tool_name": tool_name,
                "result": result.content,
                "latency_ms": latency_ms,
                "call_id": call_id,
            })

        @hooks.hook("tool_error")
        async def _on_tool_error(ctx, tool_name, error, call_id):
            await self._send_json({
                "type": "tool_error",
                "tool_name": tool_name,
                "error": str(error),
                "call_id": call_id,
            })

        @hooks.hook("safety_blocked")
        async def _on_safety_blocked(ctx, rule, reason, action, call_id="", tool_name=""):
            await self._send_json({
                "type": "safety_blocked",
                "rule": rule,
                "reason": reason,
                "action": action,
                "call_id": call_id,
                "tool_name": tool_name,
            })

        @hooks.hook("state_change")
        async def _on_state_change(ctx, state):
            await self._send_json({"type": "state_change", "state": state})

        @hooks.hook("error")
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
        hooks.on("timeout_warning", _on_timeout_warning)

        # 通过 AgentFactory 创建 Agent（单 Agent，多会话由 Agent 内部管理）
        return self._factory.create_agent(
            hooks=hooks,
            approval_handler=self._approval_handler,
        )

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
        """处理聊天消息。"""
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
            # 确保活跃会话正确
            session = self._agent.get_session(session_id)
            if not session:
                session = self._agent.create_session(session_id=session_id)

            self._agent.set_active_session(session_id)

            task = asyncio.create_task(self._agent.run(user_text))
            self._running_tasks[session_id] = task

            try:
                response = await task
            except asyncio.CancelledError:
                logger.info(f"Agent cancelled (session={session_id})")
                # Session/AgentLoop 已处理取消并正常返回，
                # 此处捕获的是极少数 Session 层面的取消
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
            logger.error(f"Agent run error (session={session_id}): {e}")
            await self._send_json({"type": "error", "message": f"Agent 执行出错: {e}"})
        finally:
            self._ws_lock.release(session_id)

    async def _handle_cancel(self) -> None:
        """处理取消请求。先设置 session 取消理由，再取消 task。"""
        session = self._agent.get_session(self._session_id)
        if session:
            # 先设置取消理由，再由 request_cancel 内部调用 task.cancel()
            session.request_cancel("user_cancelled", "用户通过 WebSocket 取消")
        else:
            # 无 session 时直接取消 task
            task = self._running_tasks.get(self._session_id)
            if task and not task.done():
                task.cancel()
            else:
                await self._send_json({"type": "error", "message": "当前没有正在运行的任务"})
            return

        # session.request_cancel 已调用 task.cancel()，此处做兜底
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
        sessions = await self._state_store.list_all_sessions()

        for s in sessions:
            try:
                messages = await self._state_store.load_messages(s["session_id"])
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

        # 通过 Agent 创建新会话
        self._agent.create_session(session_id=new_session_id)
        self._session_id = new_session_id

        await self._send_json({"type": "session_created", "session_id": new_session_id})

    async def _handle_session_switch(self, data: dict) -> None:
        """处理切换会话请求。"""
        target_id = data.get("session_id", "")
        if not target_id:
            await self._send_json({"type": "error", "message": "缺少 session_id"})
            return

        try:
            # 尝试恢复已有会话
            existing = self._agent.get_session(target_id)
            if existing:
                self._agent.set_active_session(target_id)
            else:
                # 从 StateStore 恢复
                await self._agent.restore_session(target_id)
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
        messages = await self._state_store.load_messages(target_id)
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

        await self._state_store.clear_session(target_id)
        self._running_tasks.pop(target_id, None)

        # 从 Agent 的会话列表中移除
        session = self._agent.get_session(target_id)
        if session:
            self._agent._sessions.pop(target_id, None)

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