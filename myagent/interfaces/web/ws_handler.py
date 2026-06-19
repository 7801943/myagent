"""
WebSocket Handler：处理 WebSocket 连接的完整生命周期。

═══════════════════════════════════════════════════════════════
  架构（V2 共享 EventBus + 多客户端共享 Session）
═══════════════════════════════════════════════════════════════

  三层职责划分：

  ┌─ ws_handler.py ─ "入口层 / 传输层" ─────────────────────┐
  │  1. 初始化 Session（获取现有的 or 创建新的）              │
  │  2. 将本连接的 WS 回调注册到 Session 共享 EventBus        │
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
  │  2. EventBus 广播（publish 事件 → 所有 WS 连接）       │
  │  3. ToolManager 管理（注册/热加载）                    │
  │  4. SafetyGuard 安全检查                               │
  └────────────────────────────────────────────────────────┘

  数据流：
    EventBus.publish(StreamDelta(delta="你好"))
      → EventBus 广播 → [_on_stream_A, _on_stream_B, ...]
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

from myagent.core.session import Session, SessionManager
from myagent.core.session.client_bridge import ClientHandle
from myagent.core.models import UserContext
from myagent.context.state import StateStore
from myagent.interfaces.web.dependencies import get_auth_service
from myagent.interfaces.web.ws_models import INCOMING_MESSAGE_TYPES
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


# ─── WebSocketHandler ────────────────────────────────────────

class WebSocketHandler:
    """
    WebSocket 连接处理器。

    ┌────────────────────────────────────────────────────────┐
    │  V2 架构核心：                                          │
    │  - 每个 WS 连接不再自建 EventBus                        │
    │  - 回调注册到 Session.harness.events（共享广播中心）     │
    │  - Agent emit 时所有连接都收到推送                       │
    │  - 断开时通过 EventHandle.unregister() 取消注册         │
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
        # 当前活跃的 Session（可能与其他 WS 连接共享）
        self._session: Session | None = None
        self._session_id: str = ""

        # 本连接的客户端句柄（Hook + ws_notify 统一管理）
        self._client_handle: ClientHandle | None = None

    # ═══════════════════════════════════════════════════════
    #  连接主循环
    # ═══════════════════════════════════════════════════════

    async def run(self) -> None:
        """
        WebSocket 连接主循环。

        流程：
        1. 接受连接，认证用户
        2. 获取已有 Session（多客户端共享）或创建新 Session
        3. 注册本连接的 WS 回调到 Session 共享 EventBus
        4. 推送初始状态
        5. 进入消息循环
        6. 断开时清理（不删除 Session，其他连接可能还在使用）
        """
        await self._ws.accept()

        # ── 1. 用户认证 ──
        user = self._authenticate_user()

        # ── 2. 获取或创建 Session（通过 join_session 统一入口） ──
        cfg = {"context_window_size": self._session_manager.context_window_size}
        try:
            self._session = await self._session_manager.join_session(
                user, config_override=cfg,
            )
        except Exception as e:
            logger.error(f"Failed to join session: {e}")
            await self._send_json({"type": "error", "message": f"会话初始化失败: {e}"})
            await self._ws.close()
            return

        self._session_id = self._session.id
        is_new = not self._session._bridge.has_clients
        logger.info(f"WebSocket joined session: {self._session_id} (new={is_new})")

        # 工具热加载已在 SessionManager.create_session() 中通过
        # harness.tool_interface.start() 启动，无需重复调用

        # ── 3. 通过 attach_client 统一注册 Hook + ws_notify ──
        self._client_handle = self._session.attach_client(self._send_json)

        # ── 4. 发送连接确认（含历史消息，用于恢复会话） ──
        context_window_size = self._session_manager.context_window_size
        history = self._session.serialize_messages()
        await self._send_json({
            "type": "connected",
            "session_id": self._session_id,
            "context_window_size": context_window_size,
            "messages": history,
        })

        # ── 5. 推送初始状态。workspace_state 单独推送，方便前端恢复编辑器。
        await self._push_conversation_state()
        await self._push_workspace_state()

        # ── 6. 消息循环 ──
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
            await self._cleanup()

    # ═══════════════════════════════════════════════════════
    #  连接断开清理
    # ═══════════════════════════════════════════════════════

    async def _cleanup(self) -> None:
        """
        连接断开时清理资源：detach 客户端句柄 + 注销 token 清理 IP 绑定。
        注意：不删除 Session 本身，其他连接可能还在使用。
        """
        if self._client_handle:
            self._client_handle.detach()
            self._client_handle = None

        # 注销该连接的 token，清理 IP 绑定（如果没有其他 token 使用同一 IP）
        token_info = getattr(self._ws.state, "user", None)
        if token_info and hasattr(token_info, "token"):
            try:
                auth_service = get_auth_service()
                await auth_service.logout(token_info.token)
                logger.info(f"WebSocket disconnect: cleaned up token for user '{token_info.username}'")
            except Exception as e:
                logger.warning(f"Failed to cleanup token on WebSocket disconnect: {e}")

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
        elif msg_type == "safety_policy_set":
            await self._handle_safety_policy_set(data)
        elif msg_type == "model_select":
            await self._handle_model_select(data)
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
        elif msg_type == "workspace_collapse_dir":
            await self._handle_workspace_action("collapse_dir", data)

    # ═══════════════════════════════════════════════════════
    #  消息处理器
    # ═══════════════════════════════════════════════════════

    async def _handle_chat(self, data: dict) -> None:
        """
        处理聊天消息。使用 Session.chat()（内置并发锁）。
        所有订阅同一 Session 的 WS 连接都会收到流式推送。
        """
        user_text = data.get("text", "").strip()
        if not user_text:
            await self._send_json({"type": "error", "message": "消息内容不能为空"})
            return

        session_id = self._session_id
        session = self._session
        if not session:
            await self._send_json({"type": "error", "message": "会话不存在"})
            return

        try:
            response = await session.chat(
                user_text,
                client_state=data.get("client_state"),
            )
        except RuntimeError as e:
            # Session busy（_chat_lock 被占用）
            await self._send_json({"type": "error", "message": str(e)})
            return
        except Exception as e:
            # chat() 内部已 emit error hook，此处补 ws 层兜底通知
            logger.error(f"Session chat error (session={session_id}): {e}", exc_info=True)
            await self._send_json({"type": "error", "message": f"Agent 执行出错: {e}"})
            return

        # chat() 内部已将 CancelledError 转为取消提示字符串返回，
        # stop_reason 由 session.data.context.stop_reason 判断
        stop_reason = session.data.context.stop_reason or "completed"
        context_usage = self._build_context_usage(session)
        # 本轮耗时和 token 消耗（已写入最后一条 assistant 消息的 metadata）
        turn_meta = self._build_turn_metadata(session)
        await self._send_json({
            "type": "message_end",
            "text": response,
            "stop_reason": stop_reason,
            "context_usage": context_usage,
            **turn_meta,
        })
        await self._push_conversation_state()

    async def _handle_cancel(self) -> None:
        """处理取消请求。使用 Session.request_cancel()。"""
        session = self._session
        if not session:
            await self._send_json({"type": "error", "message": "当前没有活跃会话"})
            return
        session.request_cancel("user_cancelled", "用户通过 WebSocket 取消")

    def _handle_approval_response(self, data: dict) -> None:
        """处理人工审批回复（通过 ClientBridge）。"""
        ticket_id = data.get("ticket_id", "")
        decisions = [bool(data.get("approved", False))]
        if self._session and ticket_id:
            self._session._bridge.resolve_approval(ticket_id, decisions)

    async def _handle_safety_policy_set(self, data: dict) -> None:
        """切换当前会话独立的 CLI 安全策略。"""
        if not self._session:
            await self._send_json({"type": "error", "message": "会话不存在"})
            return
        try:
            await self._session.set_safety_policy(data.get("policy", ""))
        except (RuntimeError, ValueError) as exc:
            await self._send_json({"type": "error", "message": str(exc)})

    async def _handle_model_select(self, data: dict) -> None:
        """切换当前会话独立的模型和 Thinking 设置。"""
        if not self._session:
            await self._send_json({"type": "error", "message": "会话不存在"})
            return
        try:
            await self._session.set_model_selection(
                data.get("provider_key", ""),
                thinking_enabled=data.get("thinking_enabled"),
            )
        except (RuntimeError, ValueError) as exc:
            await self._send_json({"type": "error", "message": str(exc)})

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
        创建新会话。通过 detach + attach_client 切换到新 Session。
        """
        new_session_id = uuid4().hex[:16]

        # detach 旧客户端句柄（Session._chat_lock 会自动拦截并发请求）
        if self._client_handle:
            self._client_handle.detach()
            self._client_handle = None

        # 通过 SessionManager 创建新会话
        user = self._authenticate_user()
        self._session = await self._session_manager.create_session(
            user=user,
            session_id=new_session_id,
            context_window_size=self._session_manager.context_window_size,
        )
        self._session_id = new_session_id

        # attach 到新 Session
        self._client_handle = self._session.attach_client(self._send_json)

        await self._send_json({"type": "session_created", "session_id": new_session_id})
        await self._push_conversation_state()
        await self._push_workspace_state()

    async def _handle_session_switch(self, data: dict) -> None:
        """
        切换会话。通过 join_session 统一入口获取目标会话，
        然后通过 detach + attach_client 切换。
        """
        target_id = data.get("session_id", "")
        if not target_id:
            await self._send_json({"type": "error", "message": "缺少 session_id"})
            return

        try:
            user = self._authenticate_user()
            cfg = {"context_window_size": self._session_manager.context_window_size}
            new_session = await self._session_manager.join_session(
                user, target_id, cfg,
            )
        except Exception as e:
            await self._send_json({"type": "error", "message": f"切换会话失败: {e}"})
            return

        # 取消旧客户端句柄
        if self._client_handle:
            self._client_handle.detach()
            self._client_handle = None

        # 切换到新 Session
        self._session = new_session
        self._session_id = target_id

        # attach 到新 Session
        self._client_handle = self._session.attach_client(self._send_json)

        # 加载历史消息（通过 Session 的 serialize_messages 方法）
        history = self._session.serialize_messages()

        await self._send_json({
            "type": "session_switched",
            "session_id": target_id,
            "messages": history,
        })
        await self._push_conversation_state()
        await self._push_workspace_state()

    async def _handle_session_delete(self, data: dict) -> None:
        """处理删除会话请求。"""
        target_id = data.get("session_id", "")
        if not target_id:
            await self._send_json({"type": "error", "message": "缺少 session_id"})
            return

        await self._session_manager.delete_session(target_id)

        await self._send_json({"type": "session_deleted", "session_id": target_id})

    # ═══════════════════════════════════════════════════════
    #  Hook 管理辅助
    # ═══════════════════════════════════════════════════════

    def _unregister_all(self) -> None:
        """取消本连接在当前 Session 的所有注册（detach 客户端句柄）。"""
        if self._client_handle:
            self._client_handle.detach()
            self._client_handle = None

    # ═══════════════════════════════════════════════════════
    #  认证
    # ═══════════════════════════════════════════════════════

    def _authenticate_user(self) -> UserContext:
        """
        从 WebSocket 连接信息提取用户身份。

        通过 app.py 注入的 ws.state.user（TokenInfo）获取已认证的用户信息。
        """
        token_info = getattr(self._ws.state, "user", None)
        if token_info:
            return UserContext(user_id=token_info.username, username=token_info.username)
        # 兜底：不应该走到这里（app.py 已拦截无 token 连接）
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

    def _build_turn_metadata(self, session: Session) -> dict:
        """从最后一条 assistant 消息的 metadata 中提取本轮耗时和 token 消耗。

        Session._chat_inner() 已在对话结束时将这些数据写入 metadata，
        此处仅做读取和扁平化，供 message_end 消息即时展示。
        """
        ctx = session.context
        messages = ctx.messages
        for msg in reversed(messages):
            if msg.role == "assistant" and msg.metadata:
                return {
                    "elapsed_ms": msg.metadata.get("elapsed_ms", 0),
                    "input_tokens": msg.metadata.get("input_tokens", 0),
                    "output_tokens": msg.metadata.get("output_tokens", 0),
                }
        return {"elapsed_ms": 0, "input_tokens": 0, "output_tokens": 0}

    # ── Workspace 消息处理（统一入口） ──

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
            state_dict = self._session.data.model_dump()
            await self._send_json({"type": "conversation_state", **state_dict})
        except Exception as e:
            logger.warning(f"Failed to push conversation_state: {e}")

    async def _push_workspace_state(self) -> None:
        """推送当前 workspace 快照，供前端恢复文件树和文档编辑器。"""
        if not self._session or not self._session.workspace:
            return
        try:
            await self._send_json({
                "type": "workspace_state",
                **self._session.workspace.snapshot().to_dict(),
            })
        except Exception as e:
            logger.warning(f"Failed to push workspace_state: {e}")

    async def _send_json(self, data: dict) -> None:
        """安全发送 JSON 消息到 WebSocket 客户端。"""
        try:
            await self._ws.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.warning("WebSocket send failed (connection may be closed)")
