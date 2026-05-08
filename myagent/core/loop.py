"""
AgentLoop + Turn 抽象层：ReAct 循环引擎与 Turn 执行单元。

Turn 抽象：
  BaseTurn  — 模板方法（看门狗 + 子类逻辑，取消由 asyncio 自动传播）
  ModelTurn — LLM 流式生成（包含 StreamProcessor + Hook 分发 + context 写入）
  ToolTurn  — 工具批量执行（包含安全分拣 + 结果分发 + context 写入）
  HumanTurn — 人工审批（等待用户决策，继承看门狗 + 取消检查）

状态机路由（由 AgentLoop dispatcher 驱动）：
  MODEL → TOOL → MODEL（全部安全）
  MODEL → TOOL → HUMAN → TOOL → MODEL（部分需审批）
  MODEL → TOOL → HUMAN → MODEL（全部被拒绝）
  MODEL → None（无工具调用，结束）

设计决策：
- Turn 是一次性执行器
- Turn 自己声明下一步（状态机模式）
- Hook 旁路分发（保持现有流式消费模式）

保留在 dispatcher 层的职责：
- cancelled 异常处理（asyncio.CancelledError，跨 Turn 的全局事件）
- max_iterations 控制

取消机制：由 asyncio.Task.cancel() 驱动，CancelledError 在 await 点自动抛出，
AgentLoop 统一 catch 后写入取消消息并返回 StreamResult（不 re-raise），
使 Task 正常完成，支持后续恢复执行。
"""
import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Awaitable

from myagent.providers.router import ProviderRouter
from myagent.context.manager import ContextManager
from myagent.context.message import ToolCall, ToolResult as MsgToolResult
from myagent.core.hook import HookContext, HookManager
from myagent.providers.base import StreamEvent
from myagent.tools.manager import ToolManager
from myagent.observability.audit_logger import AuditLogger
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 流式聚合结果
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class StreamResult:
    """一次 Provider 调用的聚合结果。"""
    text: str = ""
    reasoning_text: str = ""
    tool_calls: list[ToolCall] = None
    stop_reason: str | None = None
    usage: dict = None

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []
        if self.usage is None:
            self.usage = {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Turn 数据结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TurnKind(Enum):
    """Turn 类型枚举。"""
    MODEL = auto()
    TOOL = auto()
    HUMAN = auto()


@dataclass
class TurnResult:
    """Turn 的统一输出。"""
    kind: TurnKind
    next_turn: TurnKind | None = None
    data: Any = None                       # 传递给下一个 Turn 的数据
    stream_result: StreamResult | None = None  # ModelTurn 专用
    meta: dict = field(default_factory=dict)   # 执行元数据（elapsed_seconds, usage 等）

    # NOTE: to_dict() 当前未被消费，暂注释保留。
    # def to_dict(self) -> dict:
    #     """序列化为可持久化的字典。"""
    #     return {
    #         "kind": self.kind.name,
    #         "next_turn": self.next_turn.name if self.next_turn else None,
    #         "has_stream_result": self.stream_result is not None,
    #         "has_data": self.data is not None,
    #     }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Turn 基类与子类
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BaseTurn(ABC):
    """
    Turn 基类，封装横切关注点：
    - 看门狗超时（Turn 执行期间）
    - Turn 生命周期事件（turn_error 保留为调试点）
    - 清理（finally 中取消看门狗）
    - 取消由 asyncio.CancelledError 自动传播，无需手动检查

    子类只需实现 _do_execute() 和 kind / _stage_name 属性。
    """

    def __init__(
        self,
        hooks: HookManager,
        audit: AuditLogger | None,
        watchdog_timeout: float,
    ):
        self._hooks = hooks
        self._audit = audit
        self._timeout = watchdog_timeout

    async def execute(self, ctx: HookContext, input_data: Any = None, source: TurnKind | None = None) -> TurnResult:
        """
        模板方法：看门狗 → 子类逻辑 → 清理。
        source: 上一个 Turn 的类型（用于 ToolTurn 判断是否来自 HumanTurn）。
        异常路径：turn_error → raise。
        取消路径：CancelledError 直接向上传播（由 AgentLoop 统一处理）。
        """
        start = time.monotonic()
        watchdog = asyncio.create_task(self._watchdog(ctx))
        try:
            result = await self._do_execute(ctx, input_data, source)
            result.meta["elapsed_seconds"] = round(time.monotonic() - start, 3)

            return result

        except asyncio.CancelledError:
            # 取消不触发 turn_error，直接向上传播
            raise

        except Exception as e:
            # 统一错误抛出时的基类 Hook 事件
            turn_error_name = f"{self.kind.name.lower()}_turn_error"
            await self._hooks.emit(turn_error_name, ctx, error=e)
            raise

        finally:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass

    async def _watchdog(self, ctx: HookContext):
        """看门狗协程：超时后发出 timeout_warning 事件。"""
        await asyncio.sleep(self._timeout)
        await self._hooks.emit("timeout_warning", ctx,
            stage=self._stage_name,
            timeout_seconds=self._timeout,
            message=f"{self._stage_name} 已超过 {self._timeout}s，您可以选择继续等待或取消操作",
        )
        if self._audit:
            await self._audit.emit_timeout(
                stage=self._stage_name,
                timeout_seconds=self._timeout,
                session_id=ctx.session_id,
                iteration=ctx.iteration,
            )

    @property
    @abstractmethod
    def kind(self) -> TurnKind: ...

    @property
    @abstractmethod
    def _stage_name(self) -> str: ...

    @abstractmethod
    async def _do_execute(self, ctx: HookContext, input_data: Any, source: TurnKind | None) -> TurnResult: ...


class ModelTurn(BaseTurn):
    """
    LLM 流式生成 Turn。
    职责：
    1. 流式调用 Provider → 事件聚合 → Hook 分发（原 StreamProcessor 已溶解到此类）
    2. 写入 assistant 消息到 context
    3. 决定下一步：有 tool_calls → TOOL，无 → None（结束）
    """
    kind = TurnKind.MODEL
    _stage_name = "llm_generation"

    def __init__(
        self,
        provider_router: ProviderRouter,
        context: ContextManager,
        tool_schemas: list | None,
        hooks: HookManager,
        audit: AuditLogger | None,
        watchdog_timeout: float,
    ):
        super().__init__(hooks, audit, watchdog_timeout)
        self._router = provider_router
        self._context = context
        self._tool_schemas = tool_schemas
        # ── 流式聚合状态 ──
        self._text_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_calls: list[ToolCall] = []
        self._tool_call_buffers: dict[str, dict] = {}
        self._stop_reason: str | None = None
        self._usage: dict = {}

    async def _do_execute(self, ctx: HookContext, input_data: Any = None, source: TurnKind | None = None) -> TurnResult:
        self._reset_stream()

        await self._hooks.emit("state_change", ctx, state="thinking")

        if self._audit:
            await self._audit.log_event("provider_call_start", ctx.snapshot(), session_id=ctx.session_id)

        try:
            messages = self._context.get_messages()
            tools = self._tool_schemas

            # stream_start 信号
            if self._hooks.wants_streaming():
                await self._hooks.emit("stream_start", ctx)

            # 流式消费 + 聚合 + Hook 分发
            content_started = False
            async for event in self._router.stream(messages, tools):
                # 聚合事件
                self._accumulate(event)

                # 状态追踪：首次文本输出时切换到 generating
                if not content_started and event.type == "text_delta" and event.text:
                    content_started = True
                    await self._hooks.emit("state_change", ctx, state="generating")

                # Hook 分发
                await self._dispatch_event(event, ctx)

        except asyncio.CancelledError:
            raise  # 向上传递取消
        except Exception as e:
            logger.error(f"Provider error in iteration {ctx.iteration}: {e}")
            await self._hooks.emit("state_change", ctx, state="error")
            await self._hooks.emit("error", ctx, error=e)
            if self._audit:
                await self._audit.log_event("error", {
                    "error": str(e), "error_type": type(e).__name__,
                }, session_id=ctx.session_id)
            raise

        # 构建聚合结果
        result = self._build_result()

        # stream_end 信号（只发射一次，修复原 StreamProcessor + ModelTurn 重复发射的 bug）
        if self._hooks.wants_streaming():
            await self._hooks.emit("stream_end", ctx, resuming=bool(result.tool_calls))

        # Provider 调用结束
        if self._audit:
            await self._audit.log_event("provider_call_end", {
                "stop_reason": result.stop_reason or "",
                "usage": result.usage,
            }, session_id=ctx.session_id)

        # 更新上下文的 token 使用量
        if result.usage:
            self._context.update_usage(result.usage)

        # 写入 assistant 消息到上下文
        self._context.add_assistant_message(
            content=result.text,
            tool_calls=result.tool_calls if result.tool_calls else None,
        )

        # 决定下一步
        has_tools = bool(result.tool_calls)
        meta = {"usage": result.usage} if result.usage else {}

        if has_tools:
            return TurnResult(
                kind=TurnKind.MODEL,
                next_turn=TurnKind.TOOL,
                data=result.tool_calls,
                stream_result=result,
                meta=meta,
            )
        else:
            return TurnResult(
                kind=TurnKind.MODEL,
                next_turn=None,
                stream_result=result,
                meta=meta,
            )

    # ── 流式聚合方法────

    def _accumulate(self, event: StreamEvent) -> None:
        """将 StreamEvent 累积到内部缓冲区。"""
        if event.type == "text_delta" and event.text:
            self._text_parts.append(event.text)

        elif event.type == "thinking_delta" and event.text:
            self._reasoning_parts.append(event.text)

        elif event.type == "tool_call_start":
            self._tool_call_buffers[event.tool_call_id] = {
                "name": event.tool_name,
                "args_json": "",
            }

        elif event.type == "tool_call_delta":
            buf = self._tool_call_buffers.get(event.tool_call_id)
            if buf and event.tool_args_delta:
                buf["args_json"] += event.tool_args_delta

        elif event.type == "tool_call_end" and event.tool_args is not None:
            self._tool_calls.append(ToolCall(
                id=event.tool_call_id,
                name=event.tool_name,
                arguments=event.tool_args,
            ))
            self._tool_call_buffers.pop(event.tool_call_id, None)

        elif event.type == "message_end":
            self._stop_reason = event.stop_reason
            if event.usage:
                self._usage = event.usage

    async def _dispatch_event(self, event: StreamEvent, ctx: HookContext) -> None:
        """将关键事件通过 HookManager 广播给 UI/审计等订阅方。"""
        try:
            if event.type == "text_delta" and event.text:
                await self._hooks.emit("stream", ctx, delta=event.text)
            elif event.type == "thinking_delta" and event.text:
                await self._hooks.emit("thinking_stream", ctx, delta=event.text)
            elif event.type == "error" and event.error:
                await self._hooks.emit("error", ctx, error=event.error)
        except Exception as e:
            logger.warning(f"Hook dispatch error: {e}")

    def _build_result(self) -> StreamResult:
        """从内部缓冲区构建最终的 StreamResult。"""
        return StreamResult(
            text="".join(self._text_parts),
            reasoning_text="".join(self._reasoning_parts),
            tool_calls=list(self._tool_calls),
            stop_reason=self._stop_reason,
            usage=dict(self._usage),
        )

    def _reset_stream(self) -> None:
        """重置所有流式聚合状态（每次 _do_execute 自动调用）。"""
        self._text_parts.clear()
        self._reasoning_parts.clear()
        self._tool_calls.clear()
        self._tool_call_buffers.clear()
        self._stop_reason = None
        self._usage.clear()


class ToolTurn(BaseTurn):
    """
    工具批量执行 Turn。
    职责：
    1. 并行执行 tool_calls（通过 tool_executor 回调）
    2. 分拣结果：已完成 vs 需审批（needs_approval）
    3. 写入已完成的 tool_results 到 context
    4. 决定下一步：
       - 全部完成 → MODEL
       - 有待审批 → HUMAN
    当 source=HUMAN 时（已审批的调用），跳过安全检查。
    """
    kind = TurnKind.TOOL
    _stage_name = "tool_execution"

    def __init__(
        self,
        context: ContextManager,
        tool_executor: Callable | None,
        hooks: HookManager,
        audit: AuditLogger | None,
        watchdog_timeout: float,
    ):
        super().__init__(hooks, audit, watchdog_timeout)
        self._context = context
        self._tool_executor = tool_executor

    async def _do_execute(self, ctx: HookContext, input_data: Any = None, source: TurnKind | None = None) -> TurnResult:
        tool_calls = input_data  # ModelTurn 或 HumanTurn 传递的 tool_calls 列表

        await self._hooks.emit("state_change", ctx, state="waiting_tool")

        # 判断是否来自 HumanTurn（已审批的调用跳过安全检查）
        skip_safety = (source == TurnKind.HUMAN)

        # per-tool: tool_start
        for tc in tool_calls:
            await self._hooks.emit("tool_start",
                ctx, tool_name=tc.name, args=tc.arguments, call_id=tc.id
            )
            if self._audit:
                await self._audit.log_event("tool_start", {
                    "tool_name": tc.name, "call_id": tc.id,
                }, session_id=ctx.session_id)

        # 批量执行
        if self._tool_executor:
            tool_results = await self._tool_executor(tool_calls, skip_safety=skip_safety)
        else:
            # fallback: 直接调用 tool_manager
            tool_results = []
            for tc in tool_calls:
                from myagent.tools.api import ToolResult as Tr
                tool_results.append(Tr(content=f"Tool '{tc.name}' not available", is_error=True))

        # 分拣：已完成 vs 需审批
        pending = [(tc, tr) for tc, tr in zip(tool_calls, tool_results)
                   if tr.metadata.get("needs_approval")]
        completed = [(tc, tr) for tc, tr in zip(tool_calls, tool_results)
                     if not tr.metadata.get("needs_approval")]

        # 已完成的正常写入 context + 发射 hook 事件
        for tc, tr in completed:
            latency = tr.metadata.get("latency_ms", 0)

            msg_result = MsgToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=tr.content,
                metadata={"latency_ms": latency},
            )
            self._context.add_tool_result(tc.id, msg_result)
            if tr.is_error:
                if "denied_by" in tr.metadata:
                    await self._hooks.emit("safety_blocked",
                        ctx,
                        rule=tr.metadata["denied_by"],
                        reason=str(tr.content),
                        action="deny",
                        call_id=tc.id,
                        tool_name=tc.name,
                    )
                    if self._audit:
                        await self._audit.emit_safety(
                            decision="deny",
                            rule_name=tr.metadata["denied_by"],
                            reason=str(tr.content),
                            session_id=ctx.session_id,
                        )
                await self._hooks.emit("tool_error",
                    ctx, tool_name=tc.name,
                    error=Exception(tr.content), call_id=tc.id,
                )
                if self._audit:
                    await self._audit.log_event("tool_error", {
                        "tool_name": tc.name, "call_id": tc.id,
                        "error": str(tr.content),
                    }, session_id=ctx.session_id)
            else:
                await self._hooks.emit("tool_end",
                    ctx, tool_name=tc.name, result=tr,
                    call_id=tc.id, latency_ms=latency,
                )
                if self._audit:
                    await self._audit.log_event("tool_end", {
                        "tool_name": tc.name, "call_id": tc.id,
                        "latency_ms": latency,
                        "is_error": False,
                    }, session_id=ctx.session_id)

        if pending:
            # 有待审批 → 下一步走 HumanTurn
            if self._audit:
                await self._audit.log_event("approval_needed", {
                    "pending_count": len(pending),
                    "tool_names": [tc.name for tc, _ in pending],
                }, session_id=ctx.session_id)
            return TurnResult(
                kind=TurnKind.TOOL,
                next_turn=TurnKind.HUMAN,
                data=[tc for tc, _ in pending],
            )
        else:
            return TurnResult(
                kind=TurnKind.TOOL,
                next_turn=TurnKind.MODEL,
                data=tool_results,
            )


class HumanTurn(BaseTurn):
    """
    人工审批 Turn。继承 BaseTurn 获得看门狗超时保护。
    职责：
    1. 通过 hook 事件 approval_needed 通知 UI 层
    2. 等待 approval_handler 返回审批决策
    3. 被拒绝的写入 context 让 LLM 知道
    4. 决定下一步：有批准 → TOOL（执行），全部拒绝 → MODEL
    """
    kind = TurnKind.HUMAN
    _stage_name = "human_approval"

    def __init__(
        self,
        context: ContextManager,
        hooks: HookManager,
        audit: AuditLogger | None,
        watchdog_timeout: float,
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
    ):
        super().__init__(hooks, audit, watchdog_timeout)
        self._context = context
        self._approval_handler = approval_handler  # async (tool_calls) -> list[bool]

    async def _do_execute(self, ctx: HookContext, input_data: Any = None, source: TurnKind | None = None) -> TurnResult:
        """input_data: list[ToolCall] 需要审批的工具调用"""
        pending_calls = input_data

        # 通过 hook 通知 UI 层
        await self._hooks.emit("approval_needed", ctx, tool_calls=pending_calls)

        if self._audit:
            await self._audit.log_event("human_turn_start", {
                "pending_count": len(pending_calls),
                "tool_names": [tc.name for tc in pending_calls],
            }, session_id=ctx.session_id)

        if self._approval_handler:
            decisions = await self._approval_handler(pending_calls)
        else:
            # 无 handler，默认全部拒绝
            decisions = [False] * len(pending_calls)

        approved = [tc for tc, ok in zip(pending_calls, decisions) if ok]
        rejected = [tc for tc, ok in zip(pending_calls, decisions) if not ok]

        # 被拒绝的写入 context 让 LLM 知道
        if rejected:
            for tc in rejected:
                msg_result = MsgToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=f"工具 '{tc.name}' 被用户拒绝执行",
                )
                self._context.add_tool_result(tc.id, msg_result)

                await self._hooks.emit("tool_error",
                    ctx, tool_name=tc.name,
                    error=Exception(f"工具 '{tc.name}' 被用户拒绝执行"),
                    call_id=tc.id,
                )
                if self._audit:
                    await self._audit.log_event("tool_rejected", {
                        "tool_name": tc.name, "call_id": tc.id,
                    }, session_id=ctx.session_id)

        if self._audit:
            await self._audit.log_event("human_turn_end", {
                "approved_count": len(approved),
                "rejected_count": len(rejected),
            }, session_id=ctx.session_id)

        if approved:
            return TurnResult(
                kind=TurnKind.HUMAN,
                next_turn=TurnKind.TOOL,
                data=approved,
            )
        else:
            return TurnResult(
                kind=TurnKind.HUMAN,
                next_turn=TurnKind.MODEL,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AgentLoop：ReAct 循环引擎（Turn dispatcher）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AgentLoop:
    """
    ReAct 循环引擎（通用 Turn dispatcher 版）。
    
    根据 TurnResult.next_turn 动态路由到下一个 Turn：
    1. MODEL → LLM 生成，决定是否有 tool_calls
    2. TOOL → 执行工具，分拣结果（完成 vs 需审批）
    3. HUMAN → 等待人工审批（仅在有 needs_approval 时触发）
    """

    def __init__(
        self,
        provider_router: ProviderRouter,
        context: ContextManager,
        hook: HookManager,
        tool_manager: ToolManager | None = None,
        tool_executor: Callable | None = None,
        max_iterations: int = 50,
        # ── 超时看门狗参数 ──
        llm_timeout: float = 120.0,
        tool_batch_timeout: float = 60.0,
        human_approval_timeout: float = 300.0,
        # ── 审计日志（内联）──
        audit_logger: AuditLogger | None = None,
        # ── 人工审批 handler ──
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
    ):
        self._router = provider_router
        self._context = context
        self._tool_manager = tool_manager
        self._tool_executor = tool_executor
        self._hook = hook
        self._max_iterations = max_iterations
        self._llm_timeout = llm_timeout
        self._tool_batch_timeout = tool_batch_timeout
        self._human_approval_timeout = human_approval_timeout
        self._audit = audit_logger
        self._approval_handler = approval_handler

    def _create_turn(self, kind: TurnKind):
        """工厂方法：根据 TurnKind 创建对应的 Turn 实例。
        每次动态获取 tool_schemas（通过 tool_manager），支持运行时热加载工具。"""
        if kind == TurnKind.MODEL:
            return ModelTurn(
                provider_router=self._router,
                context=self._context,
                tool_schemas=self._tool_manager.list_schemas() if self._tool_manager else None,
                hooks=self._hook,
                audit=self._audit,
                watchdog_timeout=self._llm_timeout,
            )
        elif kind == TurnKind.TOOL:
            return ToolTurn(
                context=self._context,
                tool_executor=self._tool_executor,
                hooks=self._hook,
                audit=self._audit,
                watchdog_timeout=self._tool_batch_timeout,
            )
        elif kind == TurnKind.HUMAN:
            return HumanTurn(
                context=self._context,
                hooks=self._hook,
                audit=self._audit,
                watchdog_timeout=self._human_approval_timeout,
                approval_handler=self._approval_handler,
            )
        else:
            raise ValueError(f"Unknown TurnKind: {kind}")

    async def run(self, ctx: HookContext) -> StreamResult:
        """
        执行完整的 ReAct 循环，返回最终的 StreamResult。
        通用 dispatcher：根据 TurnResult.next_turn 动态路由。
        """
        final_result: StreamResult | None = None
        current_kind = TurnKind.MODEL
        current_data = None
        previous_kind: TurnKind | None = None

        try:
            for iteration in range(self._max_iterations):
                ctx.iteration = iteration + 1

                # → iteration_start 留在 dispatcher 层
                if self._audit:
                    await self._audit.log_event("iteration_start", ctx.snapshot(), session_id=ctx.session_id)

                # 创建并执行当前 Turn
                turn = self._create_turn(current_kind)
                result: TurnResult = await turn.execute(ctx, current_data, source=previous_kind)

                if result.next_turn is None:
                    # 循环结束
                    final_result = result.stream_result
                    await self._hook.emit("state_change", ctx, state="idle")
                    if self._audit:
                        await self._audit.log_event("iteration_end", ctx.snapshot(), session_id=ctx.session_id)
                    break

                # 路由到下一个 Turn
                previous_kind = result.kind
                current_kind = result.next_turn
                current_data = result.data

                # → iteration_end 留在 dispatcher 层
                if self._audit:
                    await self._audit.log_event("iteration_end", ctx.snapshot(), session_id=ctx.session_id)

            else:
                # for-else: 达到 max_iterations
                logger.warning(f"AgentLoop reached max iterations ({self._max_iterations})")
                final_result = StreamResult(
                    text="达到最大迭代次数限制，终止执行。",
                    stop_reason="max_iterations",
                )

        except asyncio.CancelledError:
            # → cancelled 处理留在 dispatcher 层
            # CancelledError 被 catch 且正常返回，Task 不会进入 cancelled 终态
            cancel_msg = "[系统] 操作已取消"
            logger.info(f"AgentLoop cancelled at iteration {ctx.iteration}")

            self._context.add_assistant_message(
                content=cancel_msg, tool_calls=None
            )

            if self._audit:
                try:
                    await asyncio.shield(
                        self._audit.emit_cancelled(
                            reason="user_cancelled",
                            detail="",
                            session_id=ctx.session_id,
                            iteration=ctx.iteration,
                        )
                    )
                except Exception:
                    pass

            return StreamResult(
                text=cancel_msg,
                stop_reason="cancelled",
            )

        return final_result or StreamResult(stop_reason="unknown")