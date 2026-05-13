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
  - SessionMeta 作为唯一状态容器（扁平化嵌套 dict 结构）
  - 初始化时从 Agent 实例采集模型、工具等信息
  - 运行时更新由 Hook 信号驱动（Phase 2 完善）
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from myagent.context.manager import ContextManager
from myagent.context.state import SessionState, AgentRunState
from myagent.context.message import ContentBlock
from myagent.core.hook import HookContext
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.core.agent import Agent, AgentFactory
    from myagent.core.hook import HookManager
    from myagent.core.workspace import WorkspaceManager, WorkspaceState
    from myagent.context.state import StateStore

logger = get_logger(__name__)


# ─── SessionMeta ──────────────────────────────────────────────

@dataclass
class SessionMeta:
    """
    会话状态容器 — 扁平化嵌套 dict 结构。

    分组：
      - user: {"user_id", "username"}
      - model: {"active": {...}, "available": [{...}, ...]}
      - tool:  {"tools": [{"name", "description", "source"}, ...]}
      - context: {"token_usage": {"used", "total"},
                  "agent_run_state", "session_state",
                  "stop_reason", "cancelled"}
      - workspace: {"state": {...} | None}
      - extra: 扩展字段
    """

    user: dict[str, Any] = field(default_factory=lambda: {"user_id": "", "username": ""})
    model: dict[str, Any] = field(default_factory=lambda: {"active": {}, "available": []})
    tool: dict[str, Any] = field(default_factory=lambda: {"tools": []})
    context: dict[str, Any] = field(default_factory=lambda: {
        "token_usage": {"used": 0, "total": 128000},
        "agent_run_state": "idle",
        "session_state": "active",
        "stop_reason": "",
        "cancelled": False,
    })
    workspace: dict[str, Any] = field(default_factory=lambda: {"state": None})
    extra: dict[str, Any] = field(default_factory=dict)

    _GROUPS = ("user", "model", "tool", "context", "workspace", "extra")

    # ── 统一 get / set ──

    def get(self, group: str, key: str | None = None, default=None):
        """
        取值。

        meta.get("user")           → 整个 user dict
        meta.get("user", "username") → "alice"
        meta.get("context", "agent_run_state") → "idle"
        """
        data = getattr(self, group, None)
        if data is None:
            return default
        if key is None:
            return data
        return data.get(key, default)

    def set(self, group: str, data: dict | None = None, **kwargs):
        """
        设值。支持两种方式（可混用）：

        meta.set("user", {"user_id": "1", "username": "alice"})  # dict 整体合并
        meta.set("context", agent_run_state="idle")               # kwargs 合并
        meta.set("model", active={...}, available=[...])           # 多 kwargs
        """
        target = getattr(self, group, None)
        if target is None:
            return
        if data is not None:
            target.update(data)
        if kwargs:
            target.update(kwargs)

    # ── 序列化 ──

    def to_dict(self) -> dict[str, Any]:
        """序列化为持久化 / 前端推送的 dict。"""
        # 计算 token_usage 派生值
        tu = self.context.get("token_usage", {})
        used = tu.get("used", 0)
        total = tu.get("total", 128000)
        percentage = round(used / total * 100, 1) if total > 0 else 0.0
        remaining = max(0, total - used)

        return {
            "user": dict(self.user),
            "model": {
                "active": dict(self.model.get("active", {})),
                "available": [dict(m) for m in self.model.get("available", [])],
            },
            "tool": {
                "tools": [dict(t) for t in self.tool.get("tools", [])],
            },
            "context": {
                **{k: v for k, v in self.context.items() if k != "token_usage"},
                "token_usage": {
                    "used": used,
                    "total": total,
                    "percentage": percentage,
                    "remaining": remaining,
                },
            },
            "workspace": dict(self.workspace),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMeta:
        """
        从 dict 反序列化（用于从 DB 恢复）。
        兼容新旧两种格式。
        """
        # 新格式检测：有 "user" 顶级 key 且为 dict
        if "user" in data and isinstance(data["user"], dict) and "user_id" in data["user"]:
            return cls(
                user=data.get("user", {}),
                model=data.get("model", {}),
                tool=data.get("tool", {}),
                context=data.get("context", {}),
                workspace=data.get("workspace", {}),
                extra=data.get("extra", {}),
            )

        # 旧格式（扁平） — 迁移到新格式
        token_usage_data = data.get("token_usage", {})
        if not isinstance(token_usage_data, dict):
            token_usage_data = {}

        return cls(
            user={
                "user_id": data.get("user_id", ""),
                "username": data.get("username", ""),
            },
            model={
                "active": data.get("active_model", {}) or {},
                "available": data.get("available_models", []) or [],
            },
            tool={
                "tools": data.get("tools", []) or [],
            },
            context={
                "token_usage": {
                    "used": token_usage_data.get("used", 0),
                    "total": token_usage_data.get("total", 128000),
                },
                "agent_run_state": data.get("agent_run_state", "idle"),
                "session_state": data.get("session_state", "active"),
                "stop_reason": data.get("stop_reason", ""),
                "cancelled": data.get("cancelled", False),
            },
            workspace={"state": data.get("workspace_state")},
            extra=data.get("extra", {}),
        )


# ─── UserContext ──────────────────────────────────────────────

@dataclass
class UserContext:
    """用户会话上下文。"""
    user_id: str
    username: str = ""
    credentials: dict = field(default_factory=dict)  # 下载 token 等
    preferences: dict = field(default_factory=dict)   # 用户配置


# ─── Session ─────────────────────────────────────────────────

class Session:
    """
    一等公民 Web 会话容器。

    职责：
    1. 持有 per-session 的 ContextManager
    2. 持有 Agent 引用（共享组件）
    3. 提供 chat(user_input) 执行一轮交互
    4. 管理生命周期：取消、持久化、恢复
    5. 持有 SessionMeta（唯一状态容器）
    6. 持有 WorkspaceManager（工作空间状态容器）

    状态管理：
      - self.meta: SessionMeta — 唯一状态载体（可序列化到 DB / 推送前端）
      - 初始化时从 Agent 实例采集模型列表、工具列表等
      - 运行时通过 meta.get() / meta.set() 方法更新
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        agent: "Agent",
        user: UserContext,
        state_store: "StateStore | None" = None,
        system_prompt: str | None = None,
        max_tokens_budget: int = 100000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
        workspace_root: str | None = None,
    ):
        self.id: str = session_id or uuid4().hex[:16]
        self.created_at: datetime = datetime.now(timezone.utc)
        self.user = user
        self.last_active_at: datetime = datetime.now(timezone.utc)

        # ── 唯一状态容器 ──
        self.meta = SessionMeta(
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

        # 前端通知回调（由 ws_handler 注入）
        self._ws_notify = None

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
        agent.hooks.on("state_change", self._on_state_change)

        # 注册 tool_end hook：文件操作工具执行后自动刷新 workspace
        agent.hooks.on("tool_end", self._on_tool_end)

    # ── 兼容属性 ──

    @property
    def session_state(self) -> SessionState:
        """兼容属性：从 meta 读取 session_state。"""
        try:
            return SessionState(self.meta.get("context", "session_state", "active"))
        except ValueError:
            return SessionState.ACTIVE

    @session_state.setter
    def session_state(self, value: SessionState) -> None:
        self.meta.set("context", session_state=value.value)

    @property
    def agent_run_state(self) -> AgentRunState:
        """兼容属性：从 meta 读取 agent_run_state。"""
        try:
            return AgentRunState(self.meta.get("context", "agent_run_state", "idle"))
        except ValueError:
            return AgentRunState.IDLE

    @agent_run_state.setter
    def agent_run_state(self, value: AgentRunState) -> None:
        self.meta.set("context", agent_run_state=value.value)

    @property
    def metadata(self) -> dict:
        """兼容属性：返回 meta 的 to_dict()（只读视图）。"""
        return self.meta.to_dict()

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
        从 Agent 实例采集初始状态到 meta（模型列表、工具列表等）。
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

        self.meta.set("model", active=active_model, available=available_models)

        # 同步 token_usage.total 为当前 active provider 的 context_window_size
        active_cws = active_model.get("context_window_size", 128000)
        self.meta.get("context", "token_usage")["total"] = active_cws

        # ── 2. 采集工具列表 ──
        tools: list[dict] = []
        tm = self._agent.tool_manager
        if tm:
            for record in tm.list_schemas() or []:
                tools.append({
                    "name": record.name,
                    "description": record.description,
                    "source": record.source,
                })
        self.meta.set("tool", tools=tools)

    # ── Hook 回调 ──

    async def _on_state_change(self, ctx, state: str) -> None:
        """Hook 回调：同步 agent_run_state 到 meta。"""
        self.meta.set("context", agent_run_state=state)

    async def _on_tool_end(self, ctx, tool_name: str, result: Any, call_id: str, latency_ms: float) -> None:
        """Hook 回调：文件操作工具执行后自动刷新 workspace。"""
        if not self.workspace:
            return

        file_tools = {"file_write", "file_read", "cli_execute"}
        if tool_name in file_tools:
            await self.workspace_update("agent", "files_changed", {})

        if tool_name == "file_read" and hasattr(result, 'metadata'):
            file_path = result.metadata.get("path", "") if isinstance(result.metadata, dict) else ""
            if file_path:
                import os
                root = self.workspace.root_path
                if file_path.startswith(root):
                    rel_path = os.path.relpath(file_path, root)
                else:
                    rel_path = file_path
                await self.workspace_update("agent", "mark_llm_read", {"path": rel_path})

    # ── 核心对话 ──

    async def chat(self, user_input: str | list[ContentBlock]) -> str:
        """
        发起一轮对话。

        Args:
            user_input: 用户输入，支持三种形式：
                - str: 纯文本消息
                - list[ContentBlock]: 多模态内容（文本 + 图像混合）
                - 空字符串 "": 跳过添加用户消息（用于已预注入 context 的场景）

        Returns:
            Agent 的最终回复文本
        """
        self.last_active_at = datetime.now(timezone.utc)
        self._running_task = asyncio.current_task()
        self._cancel_reason = ""
        self._cancel_detail = ""

        # 构建 HookContext，注入 workspace 信息
        workspace_root = self.workspace.root_path if self.workspace else None
        active_file_path = self.workspace.get_active_file_path() if self.workspace else None
        ctx = HookContext(
            session_id=self.id,
            workspace_root=workspace_root,
            active_file_path=active_file_path,
        )

        if self.workspace:
            self._agent._workspace_root = self.workspace.root_path
            self._agent._active_file_path = self.workspace.get_active_file_path()

        logger.info(f"Session chat start: {self.id}")

        try:
            if self.workspace:
                workspace_text = self.workspace.get_file_list_text()
                if workspace_text:
                    self._context.add_system_note(workspace_text)

            if isinstance(user_input, list):
                await self._context.add_user_message(user_input)
            elif user_input:
                await self._context.add_user_message(user_input)

            result = await self._agent.run(self._context, ctx)

            final_content = self._agent.hooks.finalize_content(ctx, result.text)

            logger.info(f"Session chat end: {self.id}, reason={result.stop_reason}")

            # 更新 meta 并持久化
            self.meta.set("context", stop_reason=result.stop_reason or "completed")
            await self._persist(AgentRunState.IDLE)

            return final_content or ""

        except asyncio.CancelledError:
            reason = self._cancel_reason or "user_cancelled"
            cancel_msg = f"[系统] 操作已取消 — {reason}"
            if self._cancel_detail:
                cancel_msg += f": {self._cancel_detail}"
            logger.info(f"Session chat cancelled (session-level): {reason}")
            try:
                self.meta.set("context", cancelled=True)
                self.meta.get("extra")["cancel_reason"] = reason
                await asyncio.shield(self._persist(AgentRunState.IDLE))
            except Exception:
                pass
            return cancel_msg

        except Exception as e:
            logger.error(f"Session chat error: {e}")
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
        self.meta.get("extra")[key] = value

    # ── 持久化 ──

    async def _persist(self, state: AgentRunState | None = None):
        """
        内部持久化入口。
        同步运行时状态到 meta，然后序列化到 StateStore。
        """
        # 同步 agent_run_state
        if state is not None:
            self.meta.set("context", agent_run_state=state.value)

        # 同步 token 使用量
        self.meta.get("context", "token_usage")["used"] = self._context.last_usage_input_tokens

        # 同步 workspace 快照
        if self.workspace:
            self.meta.set("workspace", state=self.workspace.snapshot().to_dict())

        if self._state_store:
            # 兼容旧 StateStore 接口：传入 enum + meta dict + enum
            await self._state_store.save_state(
                self.id,
                self.agent_run_state,       # AgentRunState enum（兼容）
                self.meta.to_dict(),         # 完整状态 dict
                self.session_state,          # SessionState enum（兼容）
            )

    async def save(self) -> None:
        """持久化会话状态和消息。"""
        if self._state_store:
            # 同步最新状态到 meta
            self.meta.get("context", "token_usage")["used"] = self._context.last_usage_input_tokens
            if self.workspace:
                self.meta.set("workspace", state=self.workspace.snapshot().to_dict())

            await self._state_store.save_state(
                self.id,
                self.agent_run_state,
                self.meta.to_dict(),
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

    def make_command_handler(self):
        """创建系统指令处理器并注入给 Agent。"""
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
                        available = session.meta.get("model", "available", [])
                        for m in available:
                            m["is_active"] = (m.get("provider_name") == provider_name)
                            if m["is_active"]:
                                session.meta.set("model", active=m)
                        logger.info(f"System command: /model → {provider_name}")
                    except Exception as e:
                        logger.warning(f"Failed to switch model: {e}")
            elif cmd == "workspace":
                root_path = args.strip()
                if root_path:
                    logger.info(f"System command: /workspace {root_path}")
                    await session.set_workspace(root_path)
            else:
                logger.debug(f"Unknown system command: /{cmd} {args}")

        self._agent._system_command_handler = _system_command_handler

    # ── Workspace 相关方法 ──

    async def set_workspace(self, root_path: str) -> None:
        """设置/切换工作空间。"""
        from myagent.core.workspace import WorkspaceManager

        self.workspace = WorkspaceManager(root_path)
        self.workspace.set_on_change(self._on_workspace_change)
        await self.workspace.update("user", "set_root", {"root_path": root_path})
        logger.info(f"Workspace set: {root_path}")

    async def refresh_workspace(self) -> None:
        """刷新工作空间文件列表。"""
        if self.workspace:
            await self.workspace.update("agent", "files_changed", {})

    async def _on_workspace_change(self, state: "WorkspaceState", source: str) -> None:
        """WorkspaceManager 状态变更回调。"""
        await self._persist_workspace(state)
        if source == "agent" and self._ws_notify:
            await self._ws_notify("workspace_state", state.to_dict())

    def set_ws_notify(self, callback) -> None:
        """由 ws_handler 注入的前端通知回调。"""
        self._ws_notify = callback

    async def _persist_workspace(self, state: "WorkspaceState") -> None:
        """持久化 workspace 状态到 DB。"""
        if self._state_store:
            ws_json = json.dumps(state.to_dict(), ensure_ascii=False)
            await self._state_store.save_workspace(self.id, ws_json)

    async def workspace_update(self, source: str, action: str, data: dict) -> Any:
        """统一 workspace 更新入口。"""
        if not self.workspace:
            return None
        return await self.workspace.update(source, action, data)

    # ── 前端推送 ──

    async def push_conversation_state(self) -> None:
        """推送 meta 快照到前端（通过 ws_notify）。"""
        if self._ws_notify:
            await self._ws_notify("conversation_state", self.meta.to_dict())


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
            await session.save()
            logger.info(f"Session evicted (TTL): {sid}")

    def _get_or_create_agent(
        self,
        user_id: str,
        hooks: "HookManager",
        approval_handler=None,
        no_safety: bool = False,
    ) -> "Agent":
        if user_id not in self._user_agents:
            agent = self._factory.create_agent(
                hooks=hooks,
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
        hooks: "HookManager | None" = None,
        approval_handler=None,
        system_prompt: str | None = None,
        max_tokens_budget: int = 100000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
        no_safety: bool = False,
        workspace_root: str | None = None,
    ) -> Session:
        """创建新会话。"""
        if hooks is None:
            from myagent.core.hook import HookManager
            hooks = HookManager()

        agent = self._get_or_create_agent(user.user_id, hooks, approval_handler, no_safety=no_safety)
        effective_prompt = system_prompt or self._factory.system_prompt

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

        if workspace_root and session.workspace:
            from myagent.core.workspace import scan_dir_files
            files = await scan_dir_files(workspace_root)
            session.workspace._state.files = files

        logger.info(f"Session created: {session.id} for user: {user.user_id}")
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def restore_session(
        self,
        session_id: str,
        user: UserContext,
        hooks: "HookManager | None" = None,
        approval_handler=None,
        max_tokens_budget: int = 100000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
    ) -> Session:
        """从 StateStore 恢复会话。"""
        if not self._state_store:
            raise RuntimeError("No StateStore configured")

        if hooks is None:
            from myagent.core.hook import HookManager
            hooks = HookManager()

        agent = self._get_or_create_agent(user.user_id, hooks, approval_handler)

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
            restored_meta = SessionMeta.from_dict(metadata_dict)
            # 保留从 Agent 采集的 available_models / tools（可能已变化）
            restored_meta.set("model", available=session.meta.get("model", "available", []))
            restored_meta.set("tool", tools=session.meta.get("tool", "tools", []))
            session.meta = restored_meta

            # 回填 ContextManager 的 token 使用量
            tu = restored_meta.get("context", "token_usage", {})
            session._context._last_usage_input_tokens = tu.get("used", 0)
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

    async def delete_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session and self._state_store:
            await self._state_store.clear_session(session_id)
        logger.info(f"Session deleted: {session_id}")

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
                "agent_state": session.meta.get("context", "agent_run_state"),
                "session_state": session.meta.get("context", "session_state"),
                "metadata": session.meta.to_dict(),
            })
        return result

    async def get_session_messages(self, session_id: str) -> list:
        session = self._sessions.get(session_id)
        if session:
            return session._context.messages
        if self._state_store:
            return await self._state_store.load_messages(session_id)
        return []