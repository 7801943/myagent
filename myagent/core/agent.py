"""
Agent：无状态 AI 引擎。
Phase 1 重构：合并 AgentLoop，Agent 成为纯 AI 引擎。

职责：
1. 持有共享组件（ProviderRouter、ToolManager、HookManager）
2. 执行 ReAct 循环（原 AgentLoop.run() 逻辑）
3. 工具执行钩子（Safety + Secret + Idempotency）
4. 热加载管理

不再负责：
- Session 管理（→ SessionManager）
- 取消操作（→ Session）
- 审计日志（→ 标准 logger）
"""
import asyncio
import time
from collections import OrderedDict
from typing import Callable, Awaitable

from myagent.providers.router import ProviderRouter
from myagent.tools.manager import ToolManager
from myagent.tools.api import ToolResult
from myagent.core.hook import HookContext, HookManager
from myagent.core.turns import TurnKind, StreamResult
from myagent.context.manager import ContextManager
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class IdempotencyCache:
    """幂等缓存：防止同一 tool_call_id 被重复执行。"""

    def __init__(self, max_size: int = 1000, ttl_seconds: float = 3600):
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[ToolResult, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, tool_call_id: str) -> ToolResult | None:
        async with self._lock:
            if tool_call_id not in self._cache:
                return None
            result, ts = self._cache[tool_call_id]
            if time.monotonic() - ts > self._ttl_seconds:
                del self._cache[tool_call_id]
                return None
            self._cache.move_to_end(tool_call_id)
            return result

    async def store(self, tool_call_id: str, result: ToolResult) -> None:
        async with self._lock:
            self._cache[tool_call_id] = (result, time.monotonic())
            self._cache.move_to_end(tool_call_id)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()


class Agent:
    """
    无状态 AI 引擎：Provider + Tools + Safety + ReAct 循环。
    原 Agent + AgentLoop 合并。

    不再持有 Session 状态，通过 run(context, ctx) 接受外部传入的上下文。
    """

    def __init__(
        self,
        *,
        provider_router: ProviderRouter,
        tool_manager: ToolManager | None = None,
        hooks: HookManager | None = None,
        safety_guard=None,
        secret_manager=None,
        max_iterations: int = 50,
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
        system_command_handler: Callable[[str, str, HookContext], Awaitable[None]] | None = None,
    ):
        self._router = provider_router
        self._tool_manager = tool_manager or ToolManager()
        self._hooks = hooks or HookManager()
        self._safety_guard = safety_guard
        self._secret_manager = secret_manager
        self._idempotency = IdempotencyCache()
        self._max_iterations = max_iterations
        self._approval_handler = approval_handler
        self._system_command_handler = system_command_handler

    @property
    def hooks(self) -> HookManager:
        return self._hooks

    @property
    def tools(self) -> ToolManager:
        return self._tool_manager

    @property
    def tool_manager(self) -> ToolManager:
        return self._tool_manager

    @property
    def router(self) -> ProviderRouter:
        return self._router

    def add_tool(self, tool) -> None:
        self._tool_manager.register(tool)

    def add_hook(self, event: str, callback) -> None:
        """注册 hook 回调（代理到 HookManager.on()）。"""
        self._hooks.on(event, callback)

    async def run(self, context: ContextManager, ctx: HookContext) -> StreamResult:
        """
        执行 ReAct 循环。原 AgentLoop.run() 逻辑。

        Args:
            context: ContextManager（由 Session 提供）
            ctx: HookContext（由 Session 创建）

        Returns:
            最终的 StreamResult
        """
        final_result: StreamResult | None = None
        current_kind = TurnKind.SYSTEM
        current_data = None
        previous_kind: TurnKind | None = None

        try:
            for iteration in range(self._max_iterations):
                ctx.iteration = iteration + 1
                logger.debug(f"Iteration {iteration + 1}, turn={current_kind.name}")

                turn = self._create_turn(current_kind, context)
                from myagent.core.turns import TurnResult
                result: TurnResult = await turn.execute(ctx, current_data, source=previous_kind)

                if result.next_turn is None:
                    final_result = result.stream_result
                    await self._hooks.emit("state_change", ctx, state="idle")
                    break

                previous_kind = result.kind
                current_kind = result.next_turn
                current_data = result.data

            else:
                logger.warning(f"Agent reached max iterations ({self._max_iterations})")
                final_result = StreamResult(
                    text="达到最大迭代次数限制，终止执行。",
                    stop_reason="max_iterations",
                )

        except asyncio.CancelledError:
            cancel_msg = "[系统] 操作已取消"
            logger.info(f"Agent cancelled at iteration {ctx.iteration}")
            await context.add_assistant_message(content=cancel_msg, tool_calls=None)
            return StreamResult(
                text=cancel_msg,
                stop_reason="cancelled",
            )

        return final_result or StreamResult(stop_reason="unknown")

    def _create_turn(self, kind: TurnKind, context: ContextManager):
        """Turn 工厂。每次动态获取 tool_schemas，支持运行时热加载。"""
        if kind == TurnKind.MODEL:
            from myagent.core.turns import ModelTurn
            return ModelTurn(
                provider_router=self._router,
                context=context,
                tool_schemas=self._tool_manager.list_schemas() if self._tool_manager else None,
                hooks=self._hooks,
                timeout=120.0,
            )
        elif kind == TurnKind.TOOL:
            from myagent.core.turns import ToolTurn
            return ToolTurn(
                context=context,
                tool_executor=self._execute_tool_batch,
                hooks=self._hooks,
                timeout=60.0,
                approval_handler=self._approval_handler,
            )
        elif kind == TurnKind.SYSTEM:
            from myagent.core.turns import SystemTurn
            return SystemTurn(
                context=context,
                hooks=self._hooks,
                timeout=30.0,
                system_command_handler=self._system_command_handler,
            )
        else:
            raise ValueError(f"Unknown TurnKind: {kind}")

    async def execute_tool(self, name: str, args: dict, tool_call_id: str, skip_safety: bool = False) -> ToolResult:
        """
        工具执行钩子链：Safety → Idempotency → Secret → Execute。
        保持现有逻辑不变。
        """
        if not skip_safety and self._safety_guard:
            guard_result = await self._safety_guard.check_tool_call(name, args)
            if guard_result.is_denied:
                return ToolResult(
                    content=f"安全策略拒绝执行工具 '{name}': {guard_result.reason}",
                    is_error=True,
                    metadata={"denied_by": guard_result.rule_name, "tool_call_id": tool_call_id},
                )
            if guard_result.requires_hitl:
                return ToolResult(
                    content=f"工具 '{name}' 需要人工审批: {guard_result.reason}",
                    is_error=False,
                    metadata={
                        "needs_approval": True,
                        "reason": guard_result.reason,
                        "tool_call_id": tool_call_id,
                    },
                )
            if guard_result.decision and hasattr(guard_result.decision, 'value') and guard_result.decision.value == "rewrite" and guard_result.rewritten_args:
                args = guard_result.rewritten_args

        cached = await self._idempotency.get(tool_call_id)
        if cached is not None:
            logger.info(f"IdempotencyCache hit for {name} (call_id={tool_call_id})")
            return cached

        if self._secret_manager:
            args = self._secret_manager.inject_secrets(name, args)

        start = time.monotonic()
        result = await self._tool_manager.execute(name, **args)
        latency_ms = int((time.monotonic() - start) * 1000)
        result.metadata["latency_ms"] = latency_ms
        result.metadata["tool_call_id"] = tool_call_id

        await self._idempotency.store(tool_call_id, result)
        return result

    async def _execute_tool_batch(self, tool_calls: list, skip_safety: bool = False) -> list:
        """批量执行工具。tool_calls = list[ToolCall]。"""
        tasks = [self.execute_tool(tc.name, tc.arguments, tc.id, skip_safety) for tc in tool_calls]
        return await asyncio.gather(*tasks)

    async def start_hot_reload(self) -> None:
        if self._tool_manager:
            await self._tool_manager.start()

    async def stop_hot_reload(self) -> None:
        if self._tool_manager:
            await self._tool_manager.stop()