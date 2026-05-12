"""
Session + SessionManager：一等公民 Web 会话容器 & 多用户会话管理。

Phase 1 重构：
  - Session：从 Agent 管理的内部对象 → 一等公民会话容器
  - 新增 UserContext 数据类（用户身份+凭证）
  - 新增 SessionManager 管理 Session 的 CRUD
  - 每用户维护一个 Agent 实例
  - 会话持久化（StateStore 集成）
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from myagent.context.manager import ContextManager
from myagent.context.state import SessionState, AgentRunState
from myagent.context.message import ContentBlock
from myagent.core.hook import HookContext
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.core.agent import Agent, AgentFactory
    from myagent.core.hook import HookManager
    from myagent.context.state import StateStore

logger = get_logger(__name__)


# ─── UserContext ──────────────────────────────────────────────

@dataclass
class UserContext:
    """用户会话上下文。"""
    user_id: str
    username: str = ""
    permissions: list[str] = field(default_factory=list)
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
    5. 维护 session_state 和 agent_run_state
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
    ):
        self.id: str = session_id or uuid4().hex[:16]
        self.created_at: datetime = datetime.now(timezone.utc)
        self.user = user
        self.session_state: SessionState = SessionState.ACTIVE
        self.agent_run_state: AgentRunState = AgentRunState.IDLE
        self.metadata: dict = {}

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

        if system_prompt:
            self._context.set_system(system_prompt)

        # 注册状态同步 hook：监听 state_change 事件更新 agent_run_state
        agent.hooks.on("state_change", self._on_state_change)

    @property
    def context(self) -> ContextManager:
        return self._context

    async def _on_state_change(self, ctx, state: str) -> None:
        """Hook 回调：同步 agent_run_state。"""
        try:
            self.agent_run_state = AgentRunState(state)
        except ValueError:
            pass

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
        self._running_task = asyncio.current_task()
        self._cancel_reason = ""
        self._cancel_detail = ""

        ctx = HookContext(session_id=self.id)

        logger.info(f"Session chat start: {self.id}")

        try:
            # 写入用户消息（空字符串跳过，支持预注入场景）
            if isinstance(user_input, list):
                await self._context.add_user_message(user_input)
            elif user_input:
                await self._context.add_user_message(user_input)

            # 执行 ReAct 循环（Agent.run 内部处理 CancelledError）
            result = await self._agent.run(self._context, ctx)

            # 内容后处理
            final_content = self._agent.hooks.finalize_content(ctx, result.text)

            logger.info(f"Session chat end: {self.id}, reason={result.stop_reason}")

            # 持久化 session 状态（消息已由 ContextManager 自动持久化）
            await self._persist(
                AgentRunState.IDLE,
                {"stop_reason": result.stop_reason or "completed"}
            )

            return final_content or ""

        except asyncio.CancelledError:
            # Session 层面取消（Agent 未能捕获的极端情况）
            reason = self._cancel_reason or "user_cancelled"
            cancel_msg = f"[系统] 操作已取消 — {reason}"
            if self._cancel_detail:
                cancel_msg += f": {self._cancel_detail}"
            logger.info(f"Session chat cancelled (session-level): {reason}")
            try:
                await asyncio.shield(
                    self._persist(AgentRunState.IDLE, {
                        "cancelled": True,
                        "cancel_reason": reason,
                    })
                )
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
        """供外部（CLI/WebSocket）调用的取消入口。先设置理由，再取消 task。"""
        self._cancel_reason = reason
        self._cancel_detail = detail
        if self._running_task and not self._running_task.done():
            self._running_task.cancel()
            logger.info(f"Session cancel requested: {reason} — {detail}")

    def update_metadata(self, key: str, value) -> None:
        """更新会话元数据。"""
        self.metadata[key] = value

    async def _persist(self, state=None, metadata=None):
        """内部持久化入口。消息已由 ContextManager 自动持久化，此处只管 session 状态。"""
        if state is not None:
            self.agent_run_state = state
        if metadata is not None:
            self.metadata.update(metadata)
        if self._state_store:
            await self._state_store.save_state(self.id, self.agent_run_state, self.metadata)

    async def save(self) -> None:
        """持久化会话状态和消息。"""
        if self._state_store:
            await self._state_store.save_state(self.id, self.agent_run_state, self.metadata)
            await self._state_store.save_messages(self.id, self._context.messages)

    async def load_messages(self) -> list:
        """加载该会话的全部消息历史。"""
        if not self._state_store:
            return []
        return await self._state_store.load_messages(self.id)


# ─── SessionManager ──────────────────────────────────────────

class SessionManager:
    """
    顶层会话管理器。
    职责：
    1. 管理 Session 的 CRUD（create/get/list/delete）
    2. 每用户维护一个 Agent 实例
    3. 会话持久化（StateStore 集成）
    4. 用户隔离
    """

    def __init__(self, factory: "AgentFactory", state_store: "StateStore | None" = None):
        """
        Args:
            factory: AgentFactory（用于创建 Agent 实例）
            state_store: 可选的 StateStore（用于会话持久化）
        """
        self._factory = factory
        self._state_store = state_store
        self._sessions: dict[str, Session] = {}
        self._user_agents: dict[str, "Agent"] = {}  # user_id → Agent

    def _get_or_create_agent(
        self,
        user_id: str,
        hooks: "HookManager",
        approval_handler=None,
        no_safety: bool = False,
    ) -> "Agent":
        """获取或为用户创建 Agent 实例。"""
        if user_id not in self._user_agents:
            agent = self._factory.create_agent(
                hooks=hooks,
                approval_handler=approval_handler,
                no_safety=no_safety,
            )
            self._user_agents[user_id] = agent
            logger.info(f"Agent created for user: {user_id}")
        return self._user_agents[user_id]

    def create_session(
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
    ) -> Session:
        """创建新会话。"""
        if hooks is None:
            from myagent.core.hook import HookManager
            hooks = HookManager()

        agent = self._get_or_create_agent(user.user_id, hooks, approval_handler, no_safety=no_safety)

        # 如果未指定 system_prompt，使用 factory 的默认值
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
        )
        self._sessions[session.id] = session
        logger.info(f"Session created: {session.id} for user: {user.user_id}")
        return session

    def get_session(self, session_id: str) -> Session | None:
        """获取会话。"""
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
        """从 StateStore 恢复会话。

        流程：
        1. 从 state_store 加载 session 数据
        2. 获取/创建用户的 Agent 实例
        3. 创建 Session 对象
        4. 恢复 ContextManager（消息历史）
        """
        if not self._state_store:
            raise RuntimeError("No StateStore configured")

        if hooks is None:
            from myagent.core.hook import HookManager
            hooks = HookManager()

        agent = self._get_or_create_agent(user.user_id, hooks, approval_handler)

        # 加载会话状态
        agent_run_state, metadata = await self._state_store.load_state(session_id)

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
        session.agent_run_state = agent_run_state
        session.metadata = metadata

        # 恢复消息历史
        messages = await self._state_store.load_messages(session_id)
        if messages:
            session._context.restore_from(messages)

        self._sessions[session.id] = session
        logger.info(f"Session restored: {session_id}")
        return session

    async def delete_session(self, session_id: str) -> None:
        """删除会话。"""
        session = self._sessions.pop(session_id, None)
        if session and self._state_store:
            await self._state_store.clear_session(session_id)
        logger.info(f"Session deleted: {session_id}")

    async def list_sessions(self, user_id: str | None = None) -> list[dict]:
        """列出会话。如果提供了 user_id，只返回该用户的会话。"""
        if self._state_store:
            sessions = await self._state_store.list_all_sessions()
            if user_id:
                # TODO: 按 user_id 过滤（需要 metadata 中存储 user_id）
                pass
            return sessions
        # 无 StateStore 时，从内存返回
        result = []
        for sid, session in self._sessions.items():
            result.append({
                "session_id": sid,
                "agent_state": session.agent_run_state.value if session.agent_run_state else "idle",
                "session_state": session.session_state.value if session.session_state else "active",
                "metadata": session.metadata,
            })
        return result

    async def get_session_messages(self, session_id: str) -> list:
        """获取会话消息列表（供 ws_handler 展示会话预览）。"""
        session = self._sessions.get(session_id)
        if session:
            return session._context.messages
        if self._state_store:
            return await self._state_store.load_messages(session_id)
        return []