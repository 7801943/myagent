"""
WebSocket Handler：处理 WebSocket 连接的完整生命周期。

═══════════════════════════════════════════════════════════════
  架构（V2 共享 HookManager + 多客户端共享 Session）
═══════════════════════════════════════════════════════════════

  三层职责划分：

  ┌─ ws_handler.py ─ "入口层 / 传输层" ─────────────────────┐
  │  1. 初始化 Session（获取现有的 or 创建新的）              │
  │  2. 将本连接的 WS 回调注册到 Agent 共享 HookManager       │
  │  3. 断开时 unregister 本连接的回调                       │
  │  4. 路由前端消息到 Session 方法                          │
  └────────────────────────────────────────────────────────┘

  ┌─ session.py ─ "会话层" ───────────────────────────────┐
  │  1. 管理会话生命周期（创建/恢复/删除/TTL 清理）         │
  │  2. 用户 → Session 映射                                 │
  │  3. 消息持久化（StateStore）                             │
  └────────────────────────────────────────────────────────┘

  ┌─ agent.py ─ "引擎层" ────────────────────────────────┐
  │  1. LLM 调用（run / _create_turn）                    │
  │  2. HookManager 广播（emit 事件 → 所有 WS 连接）       │
  │  3. ToolManager 管理（注册/热加载）                    │
  │  4. SafetyGuard 安全检查                               │
  └────────────────────────────────────────────────────────┘

  数据流：
    Agent.emit("stream", delta="你好")
      → HookManager 广播 → [_on_stream_A, _on_stream_B, ...]
      → 所有 WS 连接都收到推送

  多客户端共享：
    电脑 A 和电脑 B 使用同一用户登录 → 共享同一个 Session + Agent
    任一客户端发消息，所有客户端都看到响应

═══════════════════════════════════════════════════════════════
"""
import asyncio
import json
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from myagent.core.agent import Agent, AgentFactory
from myagent.core.hook import HookManager, HookHandle
from myagent.core.session import Session, SessionManager, UserContext
from myagent.context.message import ToolCall
from myagent.context.state import StateStore
from myagent.interfaces.websocket.lock import WebSocketLock
from myagent.interfaces.web.ws_models import INCOMING_MESSAGE_TYPES
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


# ─── WebSocketApprovalHandler ────────────────────────────────

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


# ─── 消息序列化工具 ──────────────────────────────────────────

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


# ─── WebSocketHandler ────────────────────────────────────────

class WebSocketHandler:
    """
    WebSocket 连接处理器。

    ┌────────────────────────────────────────────────────────┐
    │  V2 架构核心：                                          │
    │  - 每个 WS 连接不再自建 HookManager                     │
    │  - 回调注册到 Session.agent.hooks（共享广播中心）        │
    │  - Agent emit 时所有连接都收到推送                       │
    │  - 断开时通过 HookHandle.unregister() 取消注册          │
    │  - 多客户端共享同一个 Session + Agent                    │
    └────────────────────────────────────────────────────────┘
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

        # 当前活跃的 Session（可能与其他 WS 连接共享）
        self._session: Session | None = None
        self._session_id: str = ""

        # 本连接注册到共享 HookManager 的回调句柄
        self._hook_handles: list[HookHandle] = []

        # 运行中的 chat 任务
        self._running_tasks: dict[str, asyncio.Task] = {}

        # 人工审批 handler（per-WS-connection）
        self._approval_handler: WebSocketApprovalHandler | None = None

    # ═══════════════════════════════════════════════════════
    #  连接主循环
    # ═══════════════════════════════════════════════════════

    async def run(self) -> None:
        """
        WebSocket 连接主循环。

        流程：
        1. 接受连接，认证用户
        2. 获取已有 Session（多客户端共享）或创建新 Session
        3. 注册本连接的 WS 回调到 Agent 共享 HookManager
        4. 推送初始状态
        5. 进入消息循环
        6. 断开时清理（不删除 Session，其他连接可能还在使用）
        """
        await self._ws.accept()

        # ── 1. 用户认证 ──
        user = self._authenticate_user()

        # ── 2. 初始化 approval_handler ──
        self._approval_handler = WebSocketApprovalHandler(self._ws)

        # ── 3. 获取或创建 Session ──
        # 优先查找用户的活跃 Session（多客户端共享）
        existing_session = self._session_manager.get_user_active_session(user.user_id)
        if existing_session:
            # 复用已有 Session（多客户端共享同一个 Agent + Context + Tools）
            self._session = existing_session
            self._session_id = existing_session.id
            logger.info(f"WebSocket client connected, reusing session: {self._session_id}")
        else:
            # 首次连接 → 创建新会话
            self._session_id = uuid4().hex[:16]
            factory = self._session_manager.factory
            try:
                self._session = await self._session_manager.create_session(
                    user=user,
                    session_id=self._session_id,
                    approval_handler=self._approval_handler,
                    context_window_size=factory.context_window_size,
                )
            except Exception as e:
                logger.error(f"Failed to create session: {e}")
                await self._send_json({"type": "error", "message": f"会话初始化失败: {e}"})
                await self._ws.close()
                return

            # 启动工具热加载（仅首次创建 Agent 时）
            try:
                await self._session.agent.start_hot_reload()
            except Exception as e:
                logger.warning(f"Hot reload start failed (non-fatal): {e}")

            logger.info(f"WebSocket client connected, new session: {self._session_id}")

        # ── 4. 注册本连接的 WS 回调到 Agent 共享 HookManager ──
        shared_hooks = self._session.agent.hooks
        self._register_ws_hooks(shared_hooks)

        # ── 5. 注入 ws_notify 回调（workspace 状态变更推送到前端）──
        self._session.add_ws_notify(self._send_notification)

        # ── 6. 发送连接确认 ──
        factory = self._session_manager.factory
        context_window_size = factory.context_window_size
        await self._send_json({
            "type": "connected",
            "session_id": self._session_id,
            "context_window_size": context_window_size,
        })

        # ── 7. 推送初始 conversation_state ──
        await self._push_conversation_state()

        # ── 8. 消息循环 ──
        try:
            async for raw_message in self._ws.iter_text():
                await self._dispatch_message(raw_message)

        except WebSocketDisconnect:
            logger.info(f"WebSocket client disconnected, session: {self._session_id}")
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
        finally:
            # 停止热加载（仅当不再有其他连接使用同一 Agent 时）
            # TODO: 引用计数后再停止，暂时保留不停止
            self._cleanup()

    # ═══════════════════════════════════════════════════════
    #  Hook 注册（核心改动：注册到共享 HookManager）
    # ═══════════════════════════════════════════════════════

    def _register_ws_hooks(self, shared_hooks: HookManager) -> None:
        """
        注册本连接的 WebSocket 回调到 Agent 共享 HookManager。

        每个 WS 连接注册自己的回调，Agent emit 时所有连接都收到推送。
        断开时通过 HookHandle.unregister() 取消注册。

        Args:
            shared_hooks: Agent 的共享 HookManager（广播中心）
        """
        # 保存所有 HookHandle，用于断开时清理
        self._hook_handles = []

        # ── 流式输出 ──
        async def _on_stream(ctx, delta):
            await self._send_json({"type": "text_delta", "text": delta})

        async def _on_thinking_stream(ctx, delta):
            await self._send_json({"type": "thinking_delta", "text": delta})

        async def _on_stream_start(ctx):
            await self._send_json({"type": "stream_start"})

        async def _on_stream_end(ctx, resuming=False):
            await self._send_json({"type": "stream_end", "resuming": resuming})

        # ── 工具事件 ──
        async def _on_tool_start(ctx, tool_name, args, call_id):
            await self._send_json({
                "type": "tool_start",
                "tool_name": tool_name,
                "args": args,
                "call_id": call_id,
            })

        async def _on_tool_end(ctx, tool_name, result, call_id, latency_ms):
            await self._send_json({
                "type": "tool_end",
                "tool_name": tool_name,
                "result": result.content,
                "latency_ms": latency_ms,
                "call_id": call_id,
            })

        async def _on_tool_error(ctx, tool_name, error, call_id):
            await self._send_json({
                "type": "tool_error",
                "tool_name": tool_name,
                "error": str(error),
                "call_id": call_id,
            })

        # ── 安全事件 ──
        async def _on_safety_blocked(ctx, rule, reason, action, call_id="", tool_name=""):
            await self._send_json({
                "type": "safety_blocked",
                "rule": rule,
                "reason": reason,
                "action": action,
                "call_id": call_id,
                "tool_name": tool_name,
            })

        # ── 状态变更 ──
        async def _on_state_change(ctx, state):
            await self._send_json({"type": "state_change", "state": state})

        # ── 错误 ──
        async def _on_error(ctx, error):
            await self._send_json({"type": "error", "message": str(error)})

        # ── 超时警告 ──
        async def _on_timeout_warning(ctx, **kw):
            await self._send_json({
                "type": "timeout_warning",
                "stage": kw.get("stage", ""),
                "timeout_seconds": kw.get("timeout_seconds", 0),
                "message": kw.get("message", "操作超时"),
            })

        # 注册到共享 HookManager，保存 Handle
        self._hook_handles.append(shared_hooks.on("stream", _on_stream))
        self._hook_handles.append(shared_hooks.on("thinking_stream", _on_thinking_stream))
        self._hook_handles.append(shared_hooks.on("stream_start", _on_stream_start))
        self._hook_handles.append(shared_hooks.on("stream_end", _on_stream_end))
        self._hook_handles.append(shared_hooks.on("tool_start", _on_tool_start))
        self._hook_handles.append(shared_hooks.on("tool_end", _on_tool_end))
        self._hook_handles.append(shared_hooks.on("tool_error", _on_tool_error))
        self._hook_handles.append(shared_hooks.on("safety_blocked", _on_safety_blocked))
        self._hook_handles.append(shared_hooks.on("state_change", _on_state_change))
        self._hook_handles.append(shared_hooks.on("error", _on_error))
        self._hook_handles.append(shared_hooks.on("timeout_warning", _on_timeout_warning))

    # ═══════════════════════════════════════════════════════
    #  连接断开清理
    # ═══════════════════════════════════════════════════════

    def _cleanup(self) -> None:
        """
        连接断开时清理资源。

        1. 从共享 HookManager 取消注册本连接的所有回调
        2. 从 Session 的 ws_notifiers 移除本连接的推送回调
        3. 释放任务和锁

        注意：不删除 Session 本身，其他连接可能还在使用。
        """
        # 1. 取消 hook 回调注册（从共享 HookManager 中移除）
        for handle in self._hook_handles:
            handle.unregister()
        self._hook_handles.clear()

        # 2. 移除 ws_notify 回调
        if self._session:
            self._session.remove_ws_notify(self._send_notification)

        # 3. 释放任务和锁
        self._running_tasks.pop(self._session_id, None)
        self._ws_lock.cleanup(self._session_id)

    # ═══════════════════════════════════════════════════════
    #  消息路由
    # ═══════════════════════════════════════════════════════

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
        # Workspace 消息路由（统一使用 workspace_update）
        elif msg_type == "workspace_open_file":
            await self._handle_workspace_action("open_file", data)
        elif msg_type == "workspace_close_file":
            await self._handle_workspace_action("close_file", data)
        elif msg_type == "workspace_set_active_file":
            await self._handle_workspace_action("set_active_file", data)
        elif msg_type == "workspace_set_root":
            await self._handle_workspace_action("set_root", data)
        elif msg_type == "workspace_refresh":
            await self._handle_workspace_action("files_changed", data)
        elif msg_type == "workspace_scan_dir":
            await self._handle_workspace_action("scan_dir", data)

    # ═══════════════════════════════════════════════════════
    #  消息处理器
    # ═══════════════════════════════════════════════════════

    async def _handle_chat(self, data: dict) -> None:
        """
        处理聊天消息。使用 Session.chat()。
        所有订阅同一 Session 的 WS 连接都会收到流式推送。
        """
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
            # 推送 conversation_state（Token 使用量已更新）
            await self._push_conversation_state()

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
        """
        创建新会话。
        复用当前 Agent（共享 HookManager），新建 Context。
        先取消本连接的旧 hook 回调，再重新注册。
        """
        new_session_id = uuid4().hex[:16]

        # 取消旧会话的运行任务
        old_task = self._running_tasks.pop(self._session_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        self._ws_lock.cleanup(self._session_id)

        # 先取消本连接在旧 Session 的 hook 和 ws_notify
        self._unregister_all()

        # 通过 SessionManager 创建新会话（复用已有 Agent + 共享 HookManager）
        user = self._authenticate_user()
        factory = self._session_manager.factory
        self._session = await self._session_manager.create_session(
            user=user,
            session_id=new_session_id,
            approval_handler=self._approval_handler,
            context_window_size=factory.context_window_size,
        )
        self._session_id = new_session_id

        # 重新注册本连接的 hook 回调到共享 HookManager
        self._register_ws_hooks(self._session.agent.hooks)
        self._session.add_ws_notify(self._send_notification)

        await self._send_json({"type": "session_created", "session_id": new_session_id})
        # 推送新会话的 conversation_state
        await self._push_conversation_state()

    async def _handle_session_switch(self, data: dict) -> None:
        """
        切换会话。先取消旧 hook，再注册到新 Session 的 Agent hooks。
        """
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
                user = self._authenticate_user()
                factory = self._session_manager.factory
                self._session = await self._session_manager.restore_session(
                    session_id=target_id,
                    user=user,
                    approval_handler=self._approval_handler,
                    context_window_size=factory.context_window_size,
                )

            # 取消旧 Session 的 hook 和 ws_notify
            self._unregister_all()

            # 注册到新 Session 的 Agent 共享 hooks
            self._register_ws_hooks(self._session.agent.hooks)
            self._session.add_ws_notify(self._send_notification)

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
        # 推送切换后会话的 conversation_state
        await self._push_conversation_state()

    async def _handle_session_delete(self, data: dict) -> None:
        """处理删除会话请求。"""
        target_id = data.get("session_id", "")
        if not target_id:
            await self._send_json({"type": "error", "message": "缺少 session_id"})
            return

        await self._session_manager.delete_session(target_id)
        self._running_tasks.pop(target_id, None)

        await self._send_json({"type": "session_deleted", "session_id": target_id})

    # ═══════════════════════════════════════════════════════
    #  Hook 管理辅助
    # ═══════════════════════════════════════════════════════

    def _unregister_all(self) -> None:
        """
        取消本连接在当前 Session 的所有注册：
        1. 从共享 HookManager 取消 hook 回调
        2. 从 Session 的 ws_notifiers 移除推送回调
        """
        for handle in self._hook_handles:
            handle.unregister()
        self._hook_handles.clear()

        if self._session:
            self._session.remove_ws_notify(self._send_notification)

    # ═══════════════════════════════════════════════════════
    #  认证
    # ═══════════════════════════════════════════════════════

    def _authenticate_user(self) -> UserContext:
        """
        从 WebSocket 连接信息提取用户身份（认证桩）。
        TODO: 接入真实认证系统后替换（从 cookie / header / token 解析）
        """
        return UserContext(user_id="ws_default", username="WebSocket User")

    # ═══════════════════════════════════════════════════════
    #  辅助方法
    # ═══════════════════════════════════════════════════════

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

    # ── Workspace 消息处理（统一入口） ──

    async def _send_notification(self, msg_type: str, data: dict) -> None:
        """Session 的 ws_notify 回调：将 workspace 状态变更推送到前端。"""
        await self._send_json({"type": msg_type, **data})

    async def _handle_workspace_action(self, action: str, data: dict) -> None:
        """
        统一 workspace 操作处理。
        所有前端 workspace 操作直接通过 WorkspaceManager.update() 处理。
        """
        if not self._session or not self._session.workspace:
            return
        await self._session.workspace.update("user", action, data)
        logger.debug(f"Workspace action: {action}, data={data}")

    async def _push_conversation_state(self) -> None:
        """采集并推送 conversation_state 到前端。"""
        if not self._session:
            return
        try:
            state_dict = self._session.meta.to_dict()
            await self._send_json({"type": "conversation_state", **state_dict})
        except Exception as e:
            logger.warning(f"Failed to push conversation_state: {e}")

    async def _send_json(self, data: dict) -> None:
        """安全发送 JSON 消息到 WebSocket 客户端。"""
        try:
            await self._ws.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.warning("WebSocket send failed (connection may be closed)")