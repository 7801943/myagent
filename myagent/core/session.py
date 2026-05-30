"""
Session + SessionManager：一等公民 Web 会话容器 & 多用户会话管理。

Harness 重构：
  - Session._agent → Session._harness (AgentHarness)
  - SessionManager 不再依赖 AgentFactory，直接构建 ProviderRouter / ToolManager / SafetyGuard
  - 删除 AgentFactory

保留：
  - VirtualApprovalHandler / ClientHandle
  - Session TTL 过期清理
  - Hook Topic 路由（per-session 隔离，多客户端共享）
  - 消息序列化 / 持久化
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Awaitable
from uuid import uuid4

import yaml

from myagent.context.manager import ContextManager
from myagent.context.message import ContentBlock, ToolCall
from myagent.core.hook import HookContext, HookHandle, HookManager
from myagent.core.harness import AgentHarness
from myagent.core.llm import LLMClient, StreamResult
from myagent.core.tools import ToolInterface
from myagent.core.models import UserContext, SessionData, SessionState, AgentRunState
from myagent.providers.router import ProviderRouter
from myagent.providers.openai_provider import OpenAIProvider
from myagent.providers.anthropic_provider import AnthropicProvider
from myagent.tools.manager import ToolManager
from myagent.safety.policy import PolicyEngine
from myagent.safety.cli_fence import CLIFence
from myagent.safety.content_rules import InputContentFilter, OutputContentFilter
from myagent.safety.secrets import SecretManager
from myagent.utils.config import load_yaml_config, AgentConfig
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.core.workspace import WorkspaceManager, WorkspaceState
    from myagent.context.state import StateStore
    from myagent.prompt.renderer import PromptRenderer

logger = get_logger(__name__)


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


# ─── VirtualApprovalHandler + ClientHandle ────────────────────

class PendingApproval:
    """等待审批的工单。"""
    def __init__(self, future: asyncio.Future, tool_calls: list):
        self.future = future
        self.tool_calls = tool_calls


class VirtualApprovalHandler:
    """
    会话级审批 handler，与 WebSocket 连接解耦。
    Agent 调用 → Session 广播 hitl_request → 任意客户端响应 → Future 完成。
    """

    def __init__(self, broadcast):
        self._broadcast = broadcast
        self._pending: dict[str, PendingApproval] = {}

    async def __call__(self, tool_calls: list[ToolCall]) -> list[bool]:
        ticket_id = uuid4().hex[:8]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[ticket_id] = PendingApproval(future=fut, tool_calls=tool_calls)

        await self._broadcast("hitl_request", {
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
            self._pending.pop(ticket_id, None)

    def resolve(self, ticket_id: str, decisions: list[bool]) -> None:
        """任意客户端调用此方法完成审批。"""
        pa = self._pending.get(ticket_id)
        if pa and not pa.future.done():
            pa.future.set_result(decisions)


class ClientHandle:
    """代表一个客户端的连接句柄，断开时调用 detach() 清理。"""

    def __init__(self, hook_handles: list[HookHandle], session: "Session", sender):
        self._hook_handles = hook_handles
        self._session = session
        self._sender = sender

    def detach(self) -> None:
        """断开连接时清理所有注册。"""
        for h in self._hook_handles:
            h.unregister()
        self._hook_handles.clear()
        self._session.remove_ws_notify(self._sender)


# ─── Session ─────────────────────────────────────────────────

class Session:
    """
    一等公民 Web 会话容器。

    持有 AgentHarness 引用（调度中枢），通过 harness.run() 执行交互。
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        harness: AgentHarness,
        user: UserContext,
        state_store: "StateStore | None" = None,
        system_prompt: str | None = None,
        max_tokens_budget: int = 200000,
        context_window_size: int = 200000,
        tool_result_max_chars: int = 200000,
        workspace_root: str | None = None,
        name: str | None = "新会话",
    ):
        self.id: str = session_id or uuid4().hex[:16]
        self.created_at: datetime = datetime.now(timezone.utc)
        self.user = user
        self.last_active_at: datetime = datetime.now(timezone.utc)
        self.name = name
        self.created_at: datetime = datetime.now(timezone.utc)

        # ── 唯一状态容器 ──
        self.data = SessionData(
            user={"user_id": user.user_id, "username": user.username},
            context={
                "token_usage": {"used": 0, "total": context_window_size},
                "agent_run_state": AgentRunState.IDLE.value,
                "session_state": SessionState.ACTIVE.value,
                "stop_reason": "",
                "cancelled": False,
            },
        )

        # 共享 Harness 引用
        self._harness = harness
        self._state_store = state_store

        # Per-session ContextManager（带实时持久化）
        self._context = ContextManager(
            max_tokens_budget=max_tokens_budget,
            context_window_size=context_window_size,
            tool_result_max_chars=tool_result_max_chars,
            state_store=state_store,
            session_id=self.id,
        )

        self._running_task: asyncio.Task | None = None
        self._cancel_reason: str = ""
        self._cancel_detail: str = ""

        # 前端通知回调（支持多客户端订阅同一 Session）
        self._ws_notifiers: list = []

        # PromptRenderer（由 SessionManager 注入）
        self._prompt_renderer: "PromptRenderer | None" = None

        # WorkspaceManager
        self.workspace: "WorkspaceManager | None" = None
        if workspace_root:
            from myagent.core.workspace import WorkspaceManager
            self.workspace = WorkspaceManager(workspace_root)
            self.workspace.set_on_change(self._on_workspace_change)

        if system_prompt:
            self._context.set_system(system_prompt)

        # ── 从 Harness 采集初始状态 ──
        self._init_meta_from_harness()

        # 注册状态同步 hook
        self._hook_handles: list[HookHandle] = []
        self._hook_handles.append(
            harness.hooks.on("state_change", self._on_state_change, topic=self.id)
        )
        self._hook_handles.append(
            harness.hooks.on("tool_end", self._on_tool_end, topic=self.id)
        )

        # ── 内置审批 handler ──
        self._approval_handler = VirtualApprovalHandler(self._notify_clients)

        # ── 内置并发锁 ──
        self._chat_lock = asyncio.Lock()

        # 系统指令处理器
        self._system_command_handler = None

    # ── Hook 取消注册 ──

    def unregister_hooks(self) -> None:
        """取消注册所有 hook 回调（Session 销毁时调用）。"""
        for handle in self._hook_handles:
            handle.unregister()
        self._hook_handles.clear()

    # ── 空会话判定 ──

    def has_user_message(self) -> bool:
        """判断会话中是否存在用户消息（用于空会话过滤）。"""
        return any(m.role == "user" for m in self._context.messages)

    # ── 兼容属性 ──

    @property
    def session_state(self) -> SessionState:
        try:
            return SessionState(self.data.context.session_state)
        except ValueError:
            return SessionState.ACTIVE

    @session_state.setter
    def session_state(self, value: SessionState) -> None:
        self.data.context.session_state = value.value

    @property
    def agent_run_state(self) -> AgentRunState:
        try:
            return AgentRunState(self.data.context.agent_run_state)
        except ValueError:
            return AgentRunState.IDLE

    @agent_run_state.setter
    def agent_run_state(self, value: AgentRunState) -> None:
        self.data.context.agent_run_state = value.value

    @property
    def metadata(self) -> dict:
        return self.data.model_dump()

    @property
    def context(self) -> ContextManager:
        return self._context

    @property
    def harness(self) -> AgentHarness:
        """公开 getter：获取关联的 AgentHarness 实例。"""
        return self._harness

    # ── 从 Harness 初始化 meta ──

    def _init_meta_from_harness(self) -> None:
        """从 AgentHarness 采集初始状态到 data（模型列表、工具列表等）。"""
        router = self._harness.router

        # 采集模型信息
        current_provider = router.current_provider
        available_models: list[dict] = []
        active_model: dict = {}

        for p in router.providers:
            ptype = type(p).__name__.replace("Provider", "").lower()
            cws = getattr(p, "_context_window_size", 128000)
            is_current = (current_provider is not None and p.name == current_provider.name)
            info = {
                "provider_name": p.name,
                "model_id": p.model,
                "provider_type": ptype,
                "context_window_size": cws,
                "is_active": is_current,
            }
            available_models.append(info)
            if is_current:
                active_model = info

        if not active_model.get("provider_name") and available_models:
            active_model = available_models[0]
            active_model["is_active"] = True

        self.data.model.active = active_model
        self.data.model.available = available_models

        active_cws = active_model.get("context_window_size", 200000)
        self.data.context.token_usage.total = active_cws

        # 采集工具列表
        tools: list[dict] = []
        tm = self._harness.tool_manager
        if tm:
            for record in tm.list_schemas() or []:
                tools.append({
                    "name": record.name,
                    "description": record.description,
                    "parameters_schema": getattr(record, "parameters_schema", {}),
                    "source": record.source,
                })
        self.data.tool.tools = tools

    # ── PromptRenderer 注入 ──

    def set_prompt_renderer(self, renderer: "PromptRenderer") -> None:
        self._prompt_renderer = renderer

    # ── 工具统一管理 ──

    async def update_tools(self, source: str = "agent") -> None:
        """从 ToolManager 重新采集工具列表，同步到 meta。"""
        from myagent.prompt.variables import _summarize_parameters

        tools: list[dict] = []
        tm = self._harness.tool_manager
        if tm:
            for record in tm.list_schemas() or []:
                tools.append({
                    "name": record.name,
                    "description": record.description,
                    "parameters_schema": getattr(record, "parameters_schema", {}),
                    "parameters_summary": _summarize_parameters(
                        getattr(record, "parameters_schema", {})
                    ),
                    "source": record.source,
                    "category": getattr(record.meta, "category", "") if hasattr(record, "meta") and record.meta else "",
                })
        self.data.tool.tools = tools

    # ── Hook 回调 ──

    async def _on_state_change(self, ctx, state: str) -> None:
        self.data.context.agent_run_state = state

    async def _on_tool_end(self, ctx, tool_name: str, result: Any, call_id: str, latency_ms: float) -> None:
        if not self.workspace:
            return
        file_tools = {"file_write", "file_read", "cli_execute"}
        if tool_name in file_tools:
            await self.workspace.update("agent", "files_changed", {})
        if tool_name == "file_read" and hasattr(result, 'metadata'):
            file_path = result.metadata.get("path", "") if isinstance(result.metadata, dict) else ""
            if file_path:
                import os
                root = self.workspace.root_path
                if file_path.startswith(root):
                    rel_path = os.path.relpath(file_path, root)
                else:
                    rel_path = file_path
                await self.workspace.update("agent", "mark_llm_read", {"path": rel_path})

    # ── 核心对话 ──

    async def chat(self, user_input: str | list[ContentBlock]) -> str:
        """发起一轮对话。内置并发锁。"""
        if not self._chat_lock.locked():
            async with self._chat_lock:
                return await self._chat_inner(user_input)
        else:
            raise RuntimeError("Session is busy, please wait")

    async def _chat_inner(self, user_input: str | list[ContentBlock]) -> str:
        """chat 的实际实现（在锁内执行）。"""
        # 注入 VirtualApprovalHandler 到 Harness
        self._harness._approval_handler = self._approval_handler

        self.last_active_at = datetime.now(timezone.utc)
        self._running_task = asyncio.current_task()
        self._cancel_reason = ""
        self._cancel_detail = ""

        ctx = HookContext(
            session_id=self.id,
            session_meta=self.data,
            system_command_handler=self._system_command_handler,
        )

        logger.info(f"Session chat start: {self.id}")

        try:
            # 渲染动态 system prompt（SSPT）
            if self._prompt_renderer:
                from myagent.prompt.variables import VariableCollector
                variables = await VariableCollector.collect(self)
                rendered_prompt = self._prompt_renderer.render(variables)
                self._context.set_system(rendered_prompt)

            if isinstance(user_input, list):
                await self._context.add_user_message(user_input)
            elif user_input:
                await self._context.add_user_message(user_input)

            result = await self._harness.run(self._context, ctx)

            final_content = self._harness.hooks.finalize_content(ctx, result.text)

            logger.info(f"Session chat end: {self.id}, reason={result.stop_reason}")

            self.data.context.stop_reason = result.stop_reason or "completed"
            await self._persist(AgentRunState.IDLE)

            return final_content or ""

        except asyncio.CancelledError:
            reason = self._cancel_reason or "user_cancelled"
            cancel_msg = f"[系统] 操作已取消 — {reason}"
            if self._cancel_detail:
                cancel_msg += f": {self._cancel_detail}"
            logger.info(f"Session chat cancelled (session-level): {reason}")
            try:
                self.data.context.cancelled = True
                self.data.extra["cancel_reason"] = reason
                await asyncio.shield(self._persist(AgentRunState.IDLE))
            except Exception:
                pass
            return cancel_msg

        except Exception as e:
            logger.error(f"Session chat error: {e}", exc_info=True)
            await self._harness.hooks.emit("error", ctx, error=e)
            await self._persist(AgentRunState.ERROR)
            raise

        finally:
            self._running_task = None

    def request_cancel(
        self,
        reason: str = "user_cancelled",
        detail: str = "",
    ) -> None:
        """供外部（CLI/WebSocket）调用的取消入口。"""
        self._cancel_reason = reason
        self._cancel_detail = detail
        if self._running_task and not self._running_task.done():
            self._running_task.cancel()
            logger.info(f"Session cancel requested: {reason} — {detail}")

    def update_metadata(self, key: str, value) -> None:
        self.data.extra[key] = value

    # ── 持久化 ──

    async def _persist(self, state: AgentRunState | None = None):
        if state is not None:
            self.data.context.agent_run_state = state.value
        self.data.context.token_usage.used = self._context.last_usage_input_tokens
        if self.workspace:
            self.data.workspace.state = self.workspace.snapshot().to_dict()
        if self._state_store:
            await self._state_store.save_state(
                self.id,
                self.agent_run_state,
                self.data.model_dump(),
                self.session_state,
            )

    async def save(self) -> None:
        if self._state_store:
            self.data.context.token_usage.used = self._context.last_usage_input_tokens
            if self.workspace:
                self.data.workspace.state = self.workspace.snapshot().to_dict()
            await self._state_store.save_state(
                self.id,
                self.agent_run_state,
                self.data.model_dump(),
                self.session_state,
            )
            await self._state_store.save_messages(self.id, self._context.messages)
            if self.workspace:
                ws_json = json.dumps(self.workspace.snapshot().to_dict(), ensure_ascii=False)
                await self._state_store.save_workspace(self.id, ws_json)

    async def load_messages(self) -> list:
        if not self._state_store:
            return []
        return await self._state_store.load_messages(self.id)

    def make_command_handler(self):
        """创建系统指令处理器并保存为实例属性。"""
        session = self

        async def _system_command_handler(cmd: str, args: str, ctx: HookContext) -> None:
            if cmd == "new":
                logger.info(f"System command: /new — clearing context for session {session.id}")
                session._context.clear()
            elif cmd == "model":
                provider_name = args.strip()
                if provider_name and hasattr(session._harness, 'router'):
                    try:
                        session._harness.router.set_provider(provider_name)
                        available = session.data.model.available
                        for m in available:
                            m["is_active"] = (m.get("provider_name") == provider_name)
                            if m["is_active"]:
                                session.data.model.active = m
                        logger.info(f"System command: /model → {provider_name}")
                    except Exception as e:
                        logger.warning(f"Failed to switch model: {e}")
            else:
                logger.debug(f"Unknown system command: /{cmd} {args}")

        self._system_command_handler = _system_command_handler

    # ── Workspace 回调 ──

    async def _on_workspace_change(self, state: "WorkspaceState", source: str) -> None:
        if self.has_user_message() and self._state_store:
            ws_json = json.dumps(state.to_dict(), ensure_ascii=False)
            await self._state_store.save_workspace(self.id, ws_json)
        if source == "agent":
            await self._notify_clients("workspace_state", state.to_dict())

    # ── 多客户端通知 ──

    def add_ws_notify(self, callback) -> None:
        if callback not in self._ws_notifiers:
            self._ws_notifiers.append(callback)

    def remove_ws_notify(self, callback) -> None:
        if callback in self._ws_notifiers:
            self._ws_notifiers.remove(callback)

    def set_ws_notify(self, callback) -> None:
        self.add_ws_notify(callback)

    def attach_client(self, sender) -> ClientHandle:
        """
        将一个客户端（WebSocket）接入本会话。
        所有回调通过 topic=self.id 注册到 Harness.hooks。
        """
        hooks = self._harness.hooks
        sid = self.id
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

    async def _notify_clients(self, msg_type: str, data: dict) -> None:
        for notify in list(self._ws_notifiers):
            try:
                await notify(msg_type, data)
            except Exception:
                logger.warning("Client notify failed (connection may be closed)")

    # ── 消息序列化 ──

    def serialize_messages(self) -> list[dict]:
        return _serialize_messages(self._context.messages)

    # ── 前端推送 ──

    async def push_conversation_state(self) -> None:
        if self._ws_notifiers:
            await self._notify_clients("conversation_state", self.data.model_dump())


# ─── SessionManager ──────────────────────────────────────────

class SessionManager:
    """
    顶层会话管理器。
    不再依赖 AgentFactory，直接构建 ProviderRouter / ToolManager / SafetyGuard 等组件。
    """

    def __init__(
        self,
        *,
        config_path: str = "config.yaml",
        state_store: "StateStore | None" = None,
        session_ttl_seconds: int = 3600,
    ):
        self._config_path = config_path
        self._state_store = state_store
        self._sessions: dict[str, Session] = {}
        self._user_harnesses: dict[str, AgentHarness] = {}

        self._session_ttl = session_ttl_seconds
        self._cleanup_interval = 300
        self._running = False
        self._cleanup_task: asyncio.Task | None = None

        # ── 加载配置并缓存 ──
        self._raw = load_yaml_config(config_path)
        app_config = self._raw.get("agent", self._raw) if self._raw else {}
        self._config = AgentConfig(**app_config)

        # 预加载系统提示词
        self._system_prompt: str = self._load_system_prompt()

    @property
    def context_window_size(self) -> int:
        if self._config.providers:
            for p in self._config.providers:
                if p.priority == 1:
                    return p.context_window_size
            return self._config.providers[0].context_window_size
        return 128000

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def config(self) -> AgentConfig:
        return self._config

    async def start(self) -> None:
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(f"SessionManager TTL cleanup started (TTL={self._session_ttl}s)")

    async def stop(self) -> None:
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("SessionManager TTL cleanup stopped")

    async def _cleanup_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._cleanup_interval)
            await self._evict_expired()

    async def _evict_expired(self) -> None:
        now = datetime.now(timezone.utc)
        to_evict = []
        for sid, session in self._sessions.items():
            if (now - session.last_active_at).total_seconds() > self._session_ttl:
                to_evict.append(sid)
        for sid in to_evict:
            session = self._sessions.pop(sid)
            session.unregister_hooks()
            if not session.has_user_message():
                if self._state_store:
                    await self._state_store.clear_session(sid)
                logger.info(f"Empty session discarded (TTL): {sid}")
            else:
                await session.save()
                logger.info(f"Session evicted (TTL): {sid}")

    # ── 组件构建（替代 AgentFactory） ──

    def _build_router(self) -> ProviderRouter:
        providers = []
        for p_cfg in self._config.providers:
            if p_cfg.type.lower() == "openai":
                p = OpenAIProvider(
                    name=p_cfg.name,
                    model=p_cfg.model,
                    api_key=p_cfg.api_key or "sk-dummy",
                    api_base=p_cfg.api_base,
                )
            elif p_cfg.type.lower() == "anthropic":
                p = AnthropicProvider(
                    name=p_cfg.name,
                    model=p_cfg.model,
                    api_key=p_cfg.api_key or "sk-dummy",
                )
            else:
                continue
            p._context_window_size = p_cfg.context_window_size
            providers.append(p)
        if not providers:
            raise RuntimeError("未配置任何 Provider，请检查 config.yaml")
        return ProviderRouter(providers)

    def _load_system_prompt(self) -> str:
        sys_prompt = self._config.system_prompt or "你是一个智能助手，可以帮助用户完成各种任务。"
        if self._config.system_prompt_file:
            prompt_path = Path(self._config.system_prompt_file)
            if prompt_path.exists():
                lines = []
                with open(prompt_path, "r", encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                            continue
                        lines.append(line.rstrip('\n'))
                sys_prompt = "\n".join(lines)
            else:
                logger.warning(f"system_prompt_file {self._config.system_prompt_file} not found.")
        return sys_prompt

    def _build_safety_components(self, no_safety: bool = False) -> tuple[PolicyEngine | None, list] | None:
        """构建安全策略引擎和规则链，返回 (policy_engine, rules) 或 None。"""
        safety_cfg = self._config.safety
        if no_safety or not safety_cfg.enabled:
            logger.info("Safety disabled by config or flag")
            return None
        rules_path = safety_cfg.rules_path
        rules_cfg = {}
        if Path(rules_path).exists():
            with open(rules_path) as f:
                rules_cfg = yaml.safe_load(f) or {}
        else:
            logger.warning(f"Safety rules file not found: {rules_path}.")
        policy_cfg = rules_cfg.get("policy_engine", {})
        policy_engine = PolicyEngine(
            tool_policies=policy_cfg.get("tool_policies", []),
            default_action=policy_cfg.get("default_action", safety_cfg.default_action),
        )
        cli_fence_cfg = rules_cfg.get("cli_fence", {})
        rules = [
            CLIFence(
                allowed_commands=cli_fence_cfg.get("allowed_commands"),
                approval_commands=cli_fence_cfg.get("approval_commands"),
                denied_patterns=cli_fence_cfg.get("denied_patterns"),
                denied_paths=cli_fence_cfg.get("denied_paths"),
            ),
            InputContentFilter(),
            OutputContentFilter(),
        ]
        logger.info(f"Safety enabled: policy_engine + {len(rules)} rules loaded")
        return policy_engine, rules

    def _build_secret_manager(self) -> SecretManager:
        secrets_cfg = self._config.secrets
        return SecretManager(
            env_prefix=secrets_cfg.env_prefix,
            sensitive_fields=secrets_cfg.sensitive_fields or None,
        )

    def _build_tool_manager(self) -> ToolManager:
        hr_cfg = self._config.hot_reload
        tools_dir = (hr_cfg.watch_dir if hr_cfg and hr_cfg.enabled else "myagent/tools/tools_store")
        runner_cfg = self._config.sandbox.model_dump()
        manager = ToolManager(tools_dir=tools_dir, runner_config=runner_cfg)
        manager._register_builtin_tools()
        return manager

    def _get_or_create_harness(
        self,
        user_id: str,
        no_safety: bool = False,
    ) -> AgentHarness:
        """获取或创建用户的 AgentHarness 实例。"""
        if user_id not in self._user_harnesses:
            router = self._build_router()
            safety_parts = self._build_safety_components(no_safety=no_safety)
            secret_manager = self._build_secret_manager()
            tool_manager = self._build_tool_manager()
            hooks = HookManager()

            llm_client = LLMClient(router=router, hooks=hooks)
            tool_interface = ToolInterface(
                tool_manager=tool_manager,
                policy_engine=safety_parts[0] if safety_parts else None,
                rules=safety_parts[1] if safety_parts else None,
                secret_manager=secret_manager,
            )
            harness = AgentHarness(
                llm_client=llm_client,
                tool_interface=tool_interface,
                hooks=hooks,
                max_iterations=self._config.max_iterations,
            )
            self._user_harnesses[user_id] = harness
            logger.info(f"Harness created for user: {user_id}")
        return self._user_harnesses[user_id]

    async def create_session(
        self,
        user: UserContext,
        session_id: str | None = None,
        approval_handler=None,
        system_prompt: str | None = None,
        max_tokens_budget: int = 100000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
        no_safety: bool = False,
        workspace_root: str | None = None,
    ) -> Session:
        harness = self._get_or_create_harness(user.user_id, no_safety=no_safety)
        effective_prompt = system_prompt or self._system_prompt

        if not workspace_root:
            root_dir = self._config.root_dir
            if root_dir:
                workspace_root = str(Path(root_dir).expanduser())

        session = Session(
            session_id=session_id,
            harness=harness,
            user=user,
            state_store=self._state_store,
            system_prompt=effective_prompt,
            max_tokens_budget=max_tokens_budget,
            context_window_size=context_window_size,
            tool_result_max_chars=tool_result_max_chars,
            workspace_root=workspace_root,
        )
        self._sessions[session.id] = session

        # 如果提供了外部审批 handler（如 CLI），覆盖默认的 VirtualApprovalHandler
        if approval_handler:
            session._approval_handler = approval_handler

        session.make_command_handler()

        # 注入 PromptRenderer（SSPT 动态渲染）
        try:
            renderer = self.create_prompt_renderer()
            session.set_prompt_renderer(renderer)
        except Exception as e:
            logger.warning(f"Failed to create PromptRenderer: {e}")

        if workspace_root and session.workspace:
            await session.workspace.update("user", "set_root", {"root_path": workspace_root})
            session.data.workspace.state = session.workspace.snapshot().to_dict()

        logger.info(f"Session created: {session.id} for user: {user.user_id}")
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def restore_session(
        self,
        session_id: str,
        user: UserContext,
        approval_handler=None,
        max_tokens_budget: int = 100000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
    ) -> Session:
        if not self._state_store:
            raise RuntimeError("No StateStore configured")

        harness = self._get_or_create_harness(user.user_id)

        agent_run_state, metadata_dict = await self._state_store.load_state(session_id)

        session = Session(
            session_id=session_id,
            harness=harness,
            user=user,
            state_store=self._state_store,
            max_tokens_budget=max_tokens_budget,
            context_window_size=context_window_size,
            tool_result_max_chars=tool_result_max_chars,
        )

        if isinstance(metadata_dict, dict):
            restored_data = SessionData.model_validate(metadata_dict)
            restored_data.model.available = session.data.model.available
            restored_data.tool.tools = session.data.tool.tools
            session.data = restored_data
            session._context._last_usage_input_tokens = restored_data.context.token_usage.used
        else:
            session.agent_run_state = agent_run_state

        messages = await self._state_store.load_messages(session_id)
        if messages:
            session._context.restore_from(messages)

        workspace_json = await self._state_store.load_workspace(session_id)
        if workspace_json:
            try:
                from myagent.core.workspace import WorkspaceManager, WorkspaceState
                ws_data = json.loads(workspace_json)
                ws_state = WorkspaceState.from_dict(ws_data)
                session.workspace = WorkspaceManager()
                session.workspace.restore_from(ws_state)
                session.workspace.set_on_change(session._on_workspace_change)
            except Exception as e:
                logger.warning(f"Failed to restore workspace: {e}")

        session.make_command_handler()

        self._sessions[session.id] = session
        logger.info(f"Session restored: {session_id}")
        return session

    async def join_session(
        self,
        user: UserContext,
        session_id: str | None = None,
        config_override: dict | None = None,
        approval_handler=None,
    ) -> Session:
        cfg = config_override or {}

        if session_id and session_id in self._sessions:
            return self._sessions[session_id]

        if session_id and self._state_store:
            try:
                session = await self.restore_session(
                    session_id=session_id,
                    user=user,
                    approval_handler=approval_handler,
                    **cfg,
                )
                return session
            except Exception as e:
                logger.warning(f"join_session: restore failed ({e}), fallback to create")

        session = await self.create_session(
            user=user,
            session_id=session_id,
            approval_handler=approval_handler,
            **cfg,
        )
        return session

    async def delete_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session.unregister_hooks()
        if self._state_store:
            await self._state_store.clear_session(session_id)
        logger.info(f"Session deleted: {session_id}")

    def get_user_active_session(self, user_id: str) -> Session | None:
        for sid in reversed(list(self._sessions.keys())):
            session = self._sessions.get(sid)
            if session and session.user.user_id == user_id:
                return session
        return None

    async def list_sessions(self, user_id: str | None = None) -> list[dict]:
        if self._state_store:
            sessions = await self._state_store.list_all_sessions()
            return sessions
        result = []
        for sid, session in self._sessions.items():
            result.append({
                "session_id": sid,
                "agent_state": session.data.context.agent_run_state,
                "session_state": session.data.context.session_state,
                "metadata": session.data.model_dump(),
            })
        return result

    async def get_session_messages(self, session_id: str) -> list:
        session = self._sessions.get(session_id)
        if session:
            return session._context.messages
        if self._state_store:
            return await self._state_store.load_messages(session_id)
        return []

    # ── SSPT: Prompt 模板 ──

    def load_prompt_template(self):
        from myagent.prompt.template import PromptTemplate
        template_path = self._config.prompt_template_path
        if Path(template_path).exists():
            return PromptTemplate.from_yaml(template_path)
        logger.warning(f"prompt_template.yaml not found at {template_path}, using default")
        return PromptTemplate.default()

    def create_prompt_renderer(self):
        from myagent.prompt.renderer import PromptRenderer
        template = self.load_prompt_template()
        return PromptRenderer(template)