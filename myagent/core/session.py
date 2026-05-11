"""
Session：交互过程的容器。
每个 Session 拥有独立的 ContextManager、AgentLoop。
共享组件（ProviderRouter、ToolExecutor、HookManager、AuditLogger）由 Agent 注入。
取消机制：由 asyncio.Task.cancel() 驱动，Session 通过 _running_task 自动管理。
"""
import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Awaitable
from uuid import uuid4

from myagent.providers.router import ProviderRouter
from myagent.context.manager import ContextManager
from myagent.context.state import AgentState
from myagent.context.message import Message, ContentBlock
from myagent.core.hook import HookContext, HookManager
from myagent.core.loop import AgentLoop, StreamResult
from myagent.tools.manager import ToolManager
from myagent.observability.audit_logger import AuditLogger
from myagent.utils.config import TimeoutConfig
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.context.state import StateStore

logger = get_logger(__name__)


class Session:
    """
    一个完整的交互会话：拥有自己的上下文、循环和状态。
    
    职责：
    1. 持有 per-session 的 ContextManager 和 AgentLoop
    2. 提供 run(user_input) 执行一轮交互
    3. 管理生命周期：取消、持久化、恢复
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        router: ProviderRouter,
        tool_manager: ToolManager,
        tool_executor: Callable | None = None,
        hooks: HookManager,
        audit: AuditLogger | None = None,
        timeout_config: TimeoutConfig | None = None,
        max_iterations: int = 50,
        system_prompt: str | None = None,
        state_store: "StateStore | None" = None,
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
        # ── ContextManager 配置 ──
        max_tokens_budget: int = 200000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
    ):
        self.id: str = session_id or uuid4().hex[:16]
        self.created_at: datetime = datetime.now(timezone.utc)
        self.state: AgentState = AgentState.IDLE
        self.metadata: dict = {}

        # 共享组件（由 Agent 注入）
        self._router = router
        self._tool_manager = tool_manager
        self._tool_executor = tool_executor
        self._hooks = hooks
        self._audit = audit
        self._state_store = state_store

        # Per-session 组件
        self._context = ContextManager(
            max_tokens_budget=max_tokens_budget,
            context_window_size=context_window_size,
            tool_result_max_chars=tool_result_max_chars,
        )
        self._running_task: asyncio.Task | None = None
        self._cancel_reason: str = ""
        self._cancel_detail: str = ""
        self._approval_handler = approval_handler

        # 超时配置
        tc = timeout_config or TimeoutConfig()

        # Loop（每会话独立，但引用共享组件）
        # tool_schemas 由 AgentLoop 每次创建 ModelTurn 时通过 executor 动态获取，支持热加载
        self._loop = AgentLoop(
            provider_router=router,
            context=self._context,
            hook=hooks,
            tool_manager=tool_manager,
            tool_executor=tool_executor,
            max_iterations=max_iterations,
            llm_timeout=tc.llm_generation,
            tool_batch_timeout=tc.tool_batch,
            human_approval_timeout=tc.human_approval,
            audit_logger=audit,
            approval_handler=approval_handler,
        )

        if system_prompt:
            self._context.set_system(system_prompt)

        # 注册状态同步 hook
        self._hooks.on("state_change", self._on_state_change)

    @property
    def context(self) -> ContextManager:
        return self._context

    async def _on_state_change(self, ctx, state):
        """Hook 回调：同步 Session 状态。"""
        try:
            self.state = AgentState(state)
        except ValueError:
            pass

    async def run(self, user_input: str | list[ContentBlock]) -> str:
        """在此会话中执行一轮用户交互。

        Args:
            user_input: 用户输入，支持三种形式：
                - str: 纯文本消息
                - list[ContentBlock]: 多模态内容（文本 + 图像混合）
                - 空字符串 "": 跳过添加用户消息（用于已预注入 context 的场景）
        """
        self._running_task = asyncio.current_task()
        self._cancel_reason = ""
        self._cancel_detail = ""

        ctx = HookContext(session_id=self.id)

        # 审计
        if self._audit:
            await self._audit.log_event("session_start", ctx.snapshot(), session_id=ctx.session_id)

        try:
            # 写入用户消息（空字符串跳过，支持预注入场景）
            if isinstance(user_input, list):
                self._context.add_user_message(user_input)
            elif user_input:
                self._context.add_user_message(user_input)

            if self._audit:
                await self._audit.log_event("turn_start", ctx.snapshot(), session_id=ctx.session_id)

            # 执行 ReAct 循环（AgentLoop 内部处理 CancelledError）
            result: StreamResult = await self._loop.run(ctx)

            # 内容后处理
            final_content = self._hooks.finalize_content(ctx, result.text)

            if self._audit:
                await self._audit.log_event("turn_end", ctx.snapshot(), session_id=ctx.session_id)
                await self._audit.log_event("session_end", {
                    "exit_reason": result.stop_reason or "completed",
                }, session_id=ctx.session_id)

            # 持久化
            await self._persist(
                AgentState.IDLE,
                {"stop_reason": result.stop_reason or "completed"}
            )

            return final_content or ""

        except asyncio.CancelledError:
            # Session 层面取消（AgentLoop 未能捕获的极端情况）
            reason = self._cancel_reason or "user_cancelled"
            cancel_msg = f"[系统] 操作已取消 — {reason}"
            if self._cancel_detail:
                cancel_msg += f": {self._cancel_detail}"
            logger.info(f"Session run cancelled (session-level): {reason}")
            try:
                await asyncio.shield(
                    self._persist(AgentState.IDLE, {
                        "cancelled": True,
                        "cancel_reason": reason,
                    })
                )
            except Exception:
                pass
            return cancel_msg

        except Exception as e:
            logger.error(f"Session run error: {e}")
            await self._hooks.emit("error", ctx, error=e)
            if self._audit:
                await self._audit.log_event("error", {
                    "error": str(e), "error_type": type(e).__name__,
                }, session_id=ctx.session_id)
            await self._persist(AgentState.ERROR)
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

    async def save(self, messages=None) -> None:
        """持久化会话状态和消息。"""
        if self._state_store:
            await self._state_store.save_state(
                self.id, self.state, self.metadata
            )
            if messages is not None:
                await self._state_store.save_messages(self.id, messages)

    async def persist(self, messages: list[Message] | None = None, state: AgentState | None = None, metadata: dict | None = None) -> None:
        """一站式持久化：更新内存状态并写入持久层。"""
        if state is not None:
            self.state = state
        if metadata is not None:
            self.metadata.update(metadata)
        if messages is not None:
            await self.save(messages)
        else:
            await self.save()

    async def _persist(self, state=None, metadata=None):
        """内部持久化入口。"""
        await self.persist(self._context.messages, state, metadata)

    async def load_messages(self) -> list[Message]:
        """加载该会话的全部消息历史。"""
        if not self._state_store:
            return []
        return await self._state_store.load_messages(self.id)

    @classmethod
    async def restore(cls, session_id: str, state_store: "StateStore", **kwargs) -> "Session":
        """从 StateStore 恢复会话。需要传入共享组件（router, executor, hooks 等）。"""
        state, metadata = await state_store.load_state(session_id)
        session = cls(session_id=session_id, state_store=state_store, **kwargs)
        session.state = state
        session.metadata = metadata
        return session