"""
Session + SessionManager：一等公民 Web 会话容器 & 多用户会话管理。

Phase 1 重构：
  - Session：从 Agent 管理的内部对象 → 一等公民会话容器
  - 新增 UserContext 数据类（用户身份+凭证）
  - 新增 SessionManager 管理 Session 的 CRUD
  - 每用户维护一个 Agent 实例
  - 会话持久化（StateStore 集成）

Phase 2 重构：
  - WorkspaceManager 集成（工作空间状态容器）
  - system_command_handler 扩展（/workspace 指令）
  - Session TTL 过期清理
  - ws_notify 前端推送回调

Phase 3 重构：
  - WorkspaceManager 统一 update() 入口
  - 删除 permission 检查
  - agent 操作目录同步到前端，user 操作目录同步到 LLM 上下文

Phase 4 重构：
  - SessionData 作为唯一状态容器（扁平化嵌套 dict 结构）
  - 初始化时从 Agent 实例采集模型、工具等信息
  - 运行时更新由 Hook 信号驱动（Phase 2 完善）

Phase 5 重构（多用户 Bug 修复）：
  - Hook 回调保存 HookHandle，Session 销毁时取消注册（Bug #6）
  - Hook 回调增加 session_id 过滤（Bug #5）
  - chat() 不再写入 Agent 属性，通过 HookContext 传递状态（Bug #2）
  - make_command_handler 保存为实例属性，不写入 Agent（Bug #3）
  - ws_notify 改为观察者列表，支持多客户端（Bug #4）
  - SessionManager 销毁/TTL 清理时取消 hook 注册

Phase 6 重构（领域模型解耦）：
  - SessionState / AgentRunState 迁移到 core/models.py
  - UserContext / SessionData 迁移到 core/models.py
  - session.py 仅保留 Session 核心流转 + SessionManager 管理
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from dataclasses import dataclass

from myagent.context.manager import ContextManager
from myagent.context.message import ContentBlock, ToolCall
from myagent.core.hook import HookContext, HookHandle
from myagent.core.models import UserContext, SessionData, SessionState, AgentRunState
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.core.agent import Agent, AgentFactory
    from myagent.core.hook import HookManager
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

@dataclass
class PendingApproval:
    """等待审批的工单。"""
    future: asyncio.Future
    tool_calls: list


class VirtualApprovalHandler:
    """
    会话级审批 handler，与 WebSocket 连接解耦。
    Agent 调用 → Session 广播 hitl_request → 任意客户端响应 → Future 完成。
    """

    def __init__(self, broadcast):
        self._broadcast = broadcast  # session._notify_clients
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

    职责：
    1. 持有 per-session 的 ContextManager
    2. 持有 Agent 引用（共享组件）
    3. 提供 chat(user_input) 执行一轮交互
    4. 管理生命周期：取消、持久化、恢复
    5. 持有 SessionData（唯一状态容器）
    6. 持有 WorkspaceManager（工作空间状态容器）

    状态管理：
      - self.data: SessionData — 唯一状态载体（可序列化到 DB / 推送前端）
      - 初始化时从 Agent 实例采集模型列表、工具列表等
      - 运行时通过 meta 属性直接访问
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        agent: "Agent",
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

        # 共享 Agent 引用
        self._agent = agent
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

        # PromptRenderer（由 SessionManager 注入，SSPT 动态渲染）
        self._prompt_renderer: "PromptRenderer | None" = None

        # WorkspaceManager（工作空间状态容器，不做文件 I/O）
        self.workspace: "WorkspaceManager | None" = None
        if workspace_root:
            from myagent.core.workspace import WorkspaceManager
            self.workspace = WorkspaceManager(workspace_root)
            self.workspace.set_on_change(self._on_workspace_change)

        if system_prompt:
            self._context.set_system(system_prompt)

        # ── 从 Agent 实例采集初始状态 ──
        self._init_meta_from_agent()

        # 注册状态同步 hook：监听 state_change 事件更新 meta
        # 保存 HookHandle，用于 Session 销毁时取消注册
        # topic=self.id 确保只接收本 Session 的事件（Topic 路由）
        self._hook_handles: list[HookHandle] = []
        self._hook_handles.append(
            agent.hooks.on("state_change", self._on_state_change, topic=self.id)
        )

        # 注册 tool_end hook：文件操作工具执行后自动刷新 workspace
        self._hook_handles.append(
            agent.hooks.on("tool_end", self._on_tool_end, topic=self.id)
        )

        # ── 内置审批 handler（与 WebSocket 连接解耦） ──
        self._approval_handler = VirtualApprovalHandler(self._notify_clients)

        # ── 内置并发锁（防止多客户端同时 chat） ──
        self._chat_lock = asyncio.Lock()

        # 系统指令处理器（由 make_command_handler 创建）
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
        """兼容属性：从 meta 读取 session_state。"""
        try:
            return SessionState(self.data.context.session_state)
        except ValueError:
            return SessionState.ACTIVE

    @session_state.setter
    def session_state(self, value: SessionState) -> None:
        self.data.context.session_state = value.value

    @property
    def agent_run_state(self) -> AgentRunState:
        """兼容属性：从 meta 读取 agent_run_state。"""
        try:
            return AgentRunState(self.data.context.agent_run_state)
        except ValueError:
            return AgentRunState.IDLE

    @agent_run_state.setter
    def agent_run_state(self, value: AgentRunState) -> None:
        self.data.context.agent_run_state = value.value

    @property
    def metadata(self) -> dict:
        """返回 meta 的字典形式（只读视图）。"""
        return self.data.model_dump()

    @property
    def context(self) -> ContextManager:
        return self._context

    @property
    def agent(self) -> "Agent":
        """公开 getter：获取关联的 Agent 实例。"""
        return self._agent

    # ── 从 Agent 初始化 meta ──

    def _init_meta_from_agent(self) -> None:
        """
        从 Agent 实例采集初始状态到 data（模型列表、工具列表等）。
        仅在 Session 创建时调用一次。
        """
        router = self._agent.router

        # ── 1. 采集模型信息 ──
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

        # 如果没有匹配到 active_model，取第一个
        if not active_model.get("provider_name") and available_models:
            active_model = available_models[0]
            active_model["is_active"] = True

        self.data.model.active = active_model
        self.data.model.available = available_models

        # 同步 token_usage.total 为当前 active provider 的 context_window_size
        # [Pydantic 迁移] 直接属性赋值，不再通过 get()["key"] = value
        active_cws = active_model.get("context_window_size", 200000)
        self.data.context.token_usage.total = active_cws

        # ── 2. 采集工具列表（含 parameters_schema 供 API 调用和 prompt 渲染使用） ──
        tools: list[dict] = []
        tm = self._agent.tool_manager
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
        """注入 PromptRenderer（由 SessionManager 在创建 Session 后调用）。"""
        self._prompt_renderer = renderer

    # ── 工具统一管理 ──

    async def update_tools(self, source: str = "agent") -> None:
        """从 ToolManager 重新采集工具列表，同步到 meta。

        Args:
            source: "agent" → LLM 触发的变更, "user" → 用户操作触发
        """
        from myagent.prompt.variables import _summarize_parameters

        tools: list[dict] = []
        tm = self._agent.tool_manager
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
        """Hook 回调：同步 agent_run_state 到 meta。Topic 路由已确保仅收到本 session 事件。"""
        self.data.context.agent_run_state = state

    async def _on_tool_end(self, ctx, tool_name: str, result: Any, call_id: str, latency_ms: float) -> None:
        """Hook 回调：文件操作工具执行后自动刷新 workspace。Topic 路由已确保仅收到本 session 事件。"""
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
        """
        发起一轮对话。内置并发锁，多客户端同时调用时排队执行。

        Args:
            user_input: 用户输入，支持三种形式：
                - str: 纯文本消息
                - list[ContentBlock]: 多模态内容（文本 + 图像混合）
                - 空字符串 "": 跳过添加用户消息（用于已预注入 context 的场景）

        Returns:
            Agent 的最终回复文本

        Raises:
            RuntimeError: Session 正忙（另一轮对话进行中）
        """
        if not self._chat_lock.locked():
            async with self._chat_lock:
                return await self._chat_inner(user_input)
        else:
            raise RuntimeError("Session is busy, please wait")

    async def _chat_inner(self, user_input: str | list[ContentBlock]) -> str:
        """chat 的实际实现（在锁内执行）。"""
        # 注入 Session 内置的 VirtualApprovalHandler 到 Agent
        self._agent._approval_handler = self._approval_handler

        self.last_active_at = datetime.now(timezone.utc)
        self._running_task = asyncio.current_task()
        self._cancel_reason = ""
        self._cancel_detail = ""

        # 构建 HookContext，将所有会话级状态注入 ctx（不再写入 Agent）
        ctx = HookContext(
            session_id=self.id,
            session_meta=self.data,
            system_command_handler=self._system_command_handler,
        )

        logger.info(f"Session chat start: {self.id}")

        try:
            # ── 渲染动态 system prompt（SSPT） ──
            if self._prompt_renderer:
                from myagent.prompt.variables import VariableCollector
                variables = await VariableCollector.collect(self)
                rendered_prompt = self._prompt_renderer.render(variables)
                self._context.set_system(rendered_prompt)

            if isinstance(user_input, list):
                await self._context.add_user_message(user_input)
            elif user_input:
                await self._context.add_user_message(user_input)

            result = await self._agent.run(self._context, ctx)

            final_content = self._agent.hooks.finalize_content(ctx, result.text)

            logger.info(f"Session chat end: {self.id}, reason={result.stop_reason}")

            # 更新 meta 并持久化
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
                # [Pydantic 迁移] extra 仍是 dict，直接用属性访问
                self.data.extra["cancel_reason"] = reason
                await asyncio.shield(self._persist(AgentRunState.IDLE))
            except Exception:
                pass
            return cancel_msg

        except Exception as e:
            logger.error(f"Session chat error: {e}", exc_info=True)
            await self._agent.hooks.emit("error", ctx, error=e)
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
        """更新会话扩展元数据（存入 meta.extra）。"""
        # [Pydantic 迁移] extra 仍是 dict，直接用属性访问
        self.data.extra[key] = value

    # ── 持久化 ──

    async def _persist(self, state: AgentRunState | None = None):
        """
        内部持久化入口。
        同步运行时状态到 meta，然后序列化到 StateStore。
        """
        # 同步 agent_run_state
        if state is not None:
            self.data.context.agent_run_state = state.value

        # 同步 token 使用量
        # [Pydantic 迁移] 直接属性赋值
        self.data.context.token_usage.used = self._context.last_usage_input_tokens

        # 同步 workspace 快照
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
        """持久化会话状态和消息。"""
        if self._state_store:
            # [Pydantic 迁移] 直接属性赋值
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
        """加载该会话的全部消息历史。"""
        if not self._state_store:
            return []
        return await self._state_store.load_messages(self.id)

    # 命令系统待迁移到独立文件 command.py，计划中
    def make_command_handler(self):
        """创建系统指令处理器并保存为实例属性（不写入 Agent）。"""
        session = self

        async def _system_command_handler(cmd: str, args: str, ctx: HookContext) -> None:
            if cmd == "new":
                logger.info(f"System command: /new — clearing context for session {session.id}")
                session._context.clear()
            elif cmd == "model":
                provider_name = args.strip()
                if provider_name and hasattr(session._agent, '_router'):
                    try:
                        session._agent._router.set_provider(provider_name)
                        # 更新 meta 中的 active_model
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

        # 保存为 Session 实例属性，不再写入 Agent
        self._system_command_handler = _system_command_handler

    # ── Workspace 回调 ──

    async def _on_workspace_change(self, state: "WorkspaceState", source: str) -> None:
        """WorkspaceManager 状态变更回调。"""
        # 持久化 workspace 状态到 DB（空会话不触发）
        if self.has_user_message() and self._state_store:
            ws_json = json.dumps(state.to_dict(), ensure_ascii=False)
            await self._state_store.save_workspace(self.id, ws_json)
        if source == "agent":
            await self._notify_clients("workspace_state", state.to_dict())

    # ── 多客户端通知 ──

    def add_ws_notify(self, callback) -> None:
        """添加一个客户端通知回调（支持多客户端订阅同一 Session）。"""
        if callback not in self._ws_notifiers:
            self._ws_notifiers.append(callback)

    def remove_ws_notify(self, callback) -> None:
        """移除一个客户端通知回调（客户端断开时调用）。"""
        if callback in self._ws_notifiers:
            self._ws_notifiers.remove(callback)

    def set_ws_notify(self, callback) -> None:
        """向后兼容：内部调用 add_ws_notify。"""
        self.add_ws_notify(callback)

    def attach_client(self, sender) -> ClientHandle:
        """
        将一个客户端（WebSocket）接入本会话。
        内部完成 Hook 绑定 + ws_notify 注册，返回 ClientHandle。

        所有回调通过 topic=self.id 注册到 HookManager，
        emit 时自动按 ctx.session_id 路由，无需手动过滤。

        Args:
            sender: 异步函数，接收 dict 并发给客户端

        Returns:
            ClientHandle，断开时调用 handle.detach()
        """
        hooks = self._agent.hooks
        sid = self.id
        handles: list[HookHandle] = []

        # ── Topic 路由：所有回调注册时传入 topic=sid ──
        # emit 时 HookManager 自动匹配 ctx.session_id == sid 的回调，
        # 无需在每个回调内做 if ctx.session_id == sid 判断。

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

        # ws_notify 回调签名是 (msg_type, data)，需要适配为 sender(dict)
        async def _ws_notify_wrapper(msg_type: str, data: dict) -> None:
            await sender({"type": msg_type, **data})

        self.add_ws_notify(_ws_notify_wrapper)
        return ClientHandle(handles, self, _ws_notify_wrapper)

    async def _notify_clients(self, msg_type: str, data: dict) -> None:
        """向所有订阅的客户端推送通知。"""
        for notify in list(self._ws_notifiers):  # 复制列表防止迭代中修改
            try:
                await notify(msg_type, data)
            except Exception:
                logger.warning("Client notify failed (connection may be closed)")

    # ── 消息序列化 ──

    def serialize_messages(self) -> list[dict]:
        """序列化当前会话消息历史为前端格式。"""
        return _serialize_messages(self._context.messages)

    # ── 前端推送 ──

    async def push_conversation_state(self) -> None:
        """推送 meta 快照到前端（通过 ws_notifiers）。"""
        if self._ws_notifiers:
            await self._notify_clients("conversation_state", self.data.model_dump())


# ─── SessionManager ──────────────────────────────────────────

class SessionManager:
    """
    顶层会话管理器。
    职责：
    1. 管理 Session 的 CRUD（create/get/list/delete）
    2. 每用户维护一个 Agent 实例
    3. 会话持久化（StateStore 集成）
    4. 用户隔离
    5. Session TTL 过期清理
    """

    def __init__(
        self,
        factory: "AgentFactory",
        state_store: "StateStore | None" = None,
        session_ttl_seconds: int = 3600,
    ):
        self._factory = factory
        self._state_store = state_store
        self._sessions: dict[str, Session] = {}
        self._user_agents: dict[str, "Agent"] = {}

        self._session_ttl = session_ttl_seconds
        self._cleanup_interval = 300
        self._running = False
        self._cleanup_task: asyncio.Task | None = None

    @property
    def factory(self) -> "AgentFactory":
        return self._factory

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
            # 空会话（无用户消息）直接丢弃，不持久化；同时清理 DB 中可能存在的残余记录
            if not session.has_user_message():
                if self._state_store:
                    await self._state_store.clear_session(sid)
                logger.info(f"Empty session discarded (TTL): {sid}")
            else:
                await session.save()
                logger.info(f"Session evicted (TTL): {sid}")

    def _get_or_create_agent(
        self,
        user_id: str,
        approval_handler=None,
        no_safety: bool = False,
    ) -> "Agent":
        """
        获取或创建用户的 Agent 实例。
        Agent 始终自建 HookManager（共享广播中心），外部通过 agent.hooks.on() 注册回调。
        同一用户复用同一个 Agent，确保 HookManager 共享。
        """
        if user_id not in self._user_agents:
            agent = self._factory.create_agent(
                approval_handler=approval_handler,
                no_safety=no_safety,
            )
            self._user_agents[user_id] = agent
            logger.info(f"Agent created for user: {user_id}")
        return self._user_agents[user_id]

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
        """
        创建新会话。
        Agent 始终自建 HookManager，不再接受外部 hooks 参数。
        外部（如 ws_handler）通过 agent.hooks.on() 注册自己的回调。
        """
        agent = self._get_or_create_agent(user.user_id, approval_handler, no_safety=no_safety)
        effective_prompt = system_prompt or self._factory.system_prompt

        # 如果没有显式传入 workspace_root，从配置读取 root_dir 作为默认值
        if not workspace_root:
            root_dir = self._factory.config.root_dir
            if root_dir:
                workspace_root = str(Path(root_dir).expanduser())

        session = Session(
            session_id=session_id,
            agent=agent,
            user=user,
            state_store=self._state_store,
            system_prompt=effective_prompt,
            max_tokens_budget=max_tokens_budget,
            context_window_size=context_window_size,
            tool_result_max_chars=tool_result_max_chars,
            workspace_root=workspace_root,
        )
        self._sessions[session.id] = session

        session.make_command_handler()

        # ── 注入 PromptRenderer（SSPT 动态渲染） ──
        try:
            renderer = self._factory.create_prompt_renderer()
            session.set_prompt_renderer(renderer)
        except Exception as e:
            logger.warning(f"Failed to create PromptRenderer, using static prompt: {e}")

        if workspace_root and session.workspace:
            # 使用统一的 update 入口扫描根目录（触发通知链 + 状态同步）
            await session.workspace.update("user", "set_root", {"root_path": workspace_root})
            # 同步 workspace 快照到 meta，确保首次 _push_conversation_state 包含文件列表
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
        """
        从 StateStore 恢复会话。
        复用用户的 Agent（含共享 HookManager），不再接受外部 hooks。
        """
        if not self._state_store:
            raise RuntimeError("No StateStore configured")

        agent = self._get_or_create_agent(user.user_id, approval_handler)

        # 加载会话状态
        agent_run_state, metadata_dict = await self._state_store.load_state(session_id)

        # 创建 Session
        session = Session(
            session_id=session_id,
            agent=agent,
            user=user,
            state_store=self._state_store,
            max_tokens_budget=max_tokens_budget,
            context_window_size=context_window_size,
            tool_result_max_chars=tool_result_max_chars,
        )

        # 从持久化的 dict 恢复 meta
        if isinstance(metadata_dict, dict):
            restored_data = SessionData.model_validate(metadata_dict)
            # 保留从 Agent 采集的 available_models / tools（可能已变化）
            restored_data.model.available = session.data.model.available
            restored_data.tool.tools = session.data.tool.tools
            session.data = restored_data

            # 回填 ContextManager 的 token 使用量
            # [Pydantic 迁移] 直接属性访问，不再通过 get()
            session._context._last_usage_input_tokens = restored_data.context.token_usage.used
        else:
            # 兼容旧格式
            session.agent_run_state = agent_run_state

        # 恢复消息历史
        messages = await self._state_store.load_messages(session_id)
        if messages:
            session._context.restore_from(messages)

        # 恢复 workspace 状态
        workspace_json = await self._state_store.load_workspace(session_id)
        if workspace_json:
            try:
                from myagent.core.workspace import WorkspaceManager, WorkspaceState
                ws_data = json.loads(workspace_json)
                ws_state = WorkspaceState.from_dict(ws_data)
                session.workspace = WorkspaceManager()
                session.workspace.restore_from(ws_state)
                session.workspace.set_on_change(session._on_workspace_change)
                logger.info(f"Workspace restored for session {session_id}: {ws_state.root_path}")
            except Exception as e:
                logger.warning(f"Failed to restore workspace for session {session_id}: {e}")

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
        """
        会话接入统一入口。调用方只需提供 user + 可选 session_id，
        内部按优先级自动决策：内存命中 > DB恢复 > 用户活跃态 > 新建。

        Args:
            user: 用户上下文
            session_id: 客户端指定的会话ID（None 表示不指定）
            config_override: 覆盖默认配置（context_window_size 等）
            approval_handler: 审批 handler（Phase B 后将由 VirtualApprovalHandler 替代）
        """
        cfg = config_override or {}

        # 1. 命中内存
        if session_id and session_id in self._sessions:
            logger.info(f"join_session: memory hit {session_id}")
            return self._sessions[session_id]

        # 2. 命中持久化
        if session_id and self._state_store:
            try:
                session = await self.restore_session(
                    session_id=session_id,
                    user=user,
                    approval_handler=approval_handler,
                    **cfg,
                )
                logger.info(f"join_session: restored {session_id}")
                return session
            except Exception as e:
                logger.warning(f"join_session: restore failed ({e}), fallback to create")

        # 3. 无 session_id → 强制新建
        # (移除查用户活跃态的逻辑，确保刷新页面时默认新建空白会话)
        # if not session_id:
        #     active = self.get_user_active_session(user.user_id)
        #     if active:
        #         logger.info(f"join_session: reuse active {active.id}")
        #         return active

        # 4. 降级新建
        session = await self.create_session(
            user=user,
            session_id=session_id,
            approval_handler=approval_handler,
            **cfg,
        )
        logger.info(f"join_session: created new {session.id}")
        return session

    async def delete_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session.unregister_hooks()
        # 无论是否在内存中，都尝试从 DB 清理
        if self._state_store:
            await self._state_store.clear_session(session_id)
        logger.info(f"Session deleted: {session_id}")

    def get_user_active_session(self, user_id: str) -> Session | None:
        """
        获取用户的活跃会话（供 WS 连接复用）。
        优先返回最近活跃的会话，用于多客户端共享同一 Session。
        """
        # 反向遍历，优先返回最近使用的
        for sid in reversed(list(self._sessions.keys())):
            session = self._sessions.get(sid)
            if session and session.user.user_id == user_id:
                return session
        return None

    async def list_sessions(self, user_id: str | None = None) -> list[dict]:
        if self._state_store:
            sessions = await self._state_store.list_all_sessions()
            if user_id:
                pass  # TODO: 按 user_id 过滤
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