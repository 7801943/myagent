"""
Turn 抽象层：将 AgentLoop.run() 的单体方法拆分为独立的 Turn 执行单元。

ModelTurn — LLM 流式生成（包含 StreamProcessor + StreamParser + context 写入）
ToolTurn  — 工具批量执行（包含结果分发 + context 写入）
BaseTurn  — 模板方法（取消检查 + 看门狗 + 子类逻辑）

设计决策：
- 路线 A：Turn 是一次性执行器
- 方案 1：Turn 自己声明下一步（状态机模式）
- 选择 A：Hook 旁路分发（保持现有流式消费模式）
"""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from myagent.providers.router import ProviderRouter
from myagent.context.manager import ContextManager
from myagent.context.message import ToolResult as MsgToolResult
from myagent.core.hook import HookContext, HookManager
from myagent.core.stream import StreamProcessor, StreamResult
from myagent.core.cancellation import CancellationToken, AgentCancelledError, CancelReason
from myagent.tools.executor import ToolExecutor
from myagent.observability.audit_logger import AuditLogger
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class TurnKind(Enum):
    """Turn 类型枚举。"""
    MODEL = auto()
    TOOL = auto()
    # HUMAN = auto()  # 预留，当前架构中 Agent.run() 已处理用户输入


@dataclass
class TurnResult:
    """Turn 的统一输出。"""
    kind: TurnKind
    next_turn: TurnKind | None = None
    data: Any = None                       # 传递给下一个 Turn 的数据
    stream_result: StreamResult | None = None  # ModelTurn 专用

    def to_dict(self) -> dict:
        """序列化为可持久化的字典。"""
        return {
            "kind": self.kind.name,
            "next_turn": self.next_turn.name if self.next_turn else None,
            "has_stream_result": self.stream_result is not None,
            "has_data": self.data is not None,
        }


class BaseTurn(ABC):
    """
    Turn 基类，封装横切关注点：
    - 取消检查（每个 Turn 开始前）
    - 看门狗超时（Turn 执行期间）
    - Turn 生命周期事件（turn_error 保留为调试点）
    - 清理（finally 中取消看门狗）

    子类只需实现 _do_execute() 和 kind / _stage_name 属性。
    """

    def __init__(
        self,
        hooks: HookManager,
        cancel_token: CancellationToken | None,
        audit: AuditLogger | None,
        watchdog_timeout: float,
    ):
        self._hooks = hooks
        self._cancel = cancel_token
        self._audit = audit
        self._timeout = watchdog_timeout

    async def execute(self, ctx: HookContext, input_data: Any = None) -> TurnResult:
        """
        模板方法：取消检查 → turn_start → 看门狗 → 子类逻辑 → 持久化 → turn_end。
        异常路径：turn_error → raise。
        取消路径：直接 raise（由 Loop dispatcher 统一处理）。
        """
        if self._cancel:
            await self._cancel.check()

        watchdog = asyncio.create_task(self._watchdog(ctx))
        try:
            result = await self._do_execute(ctx, input_data)

            return result

        except AgentCancelledError:
            # 取消不触发 turn_end / turn_error，直接向上传播
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
    async def _do_execute(self, ctx: HookContext, input_data: Any) -> TurnResult: ...


class ModelTurn(BaseTurn):
    """
    LLM 流式生成 Turn。
    职责：
    1. 流式调用 Provider → StreamProcessor 聚合 → StreamParser 分发
    2. 写入 assistant 消息到 context
    3. 决定下一步：有 tool_calls → TOOL，无 → None（结束）
    """
    kind = TurnKind.MODEL
    _stage_name = "llm_generation"

    def __init__(
        self,
        provider_router: ProviderRouter,
        context: ContextManager,
        executor: ToolExecutor,
        hooks: HookManager,
        cancel_token: CancellationToken | None,
        audit: AuditLogger | None,
        watchdog_timeout: float,
    ):
        super().__init__(hooks, cancel_token, audit, watchdog_timeout)
        self._router = provider_router
        self._context = context
        self._executor = executor

    async def _do_execute(self, ctx: HookContext, input_data: Any = None) -> TurnResult:
        stream = StreamProcessor(router=self._router, hook=self._hooks)

        await self._hooks.emit("state_change", ctx, state="thinking")

        if self._audit:
            await self._audit.log_event("provider_call_start", ctx.snapshot(), session_id=ctx.session_id)

        try:
            messages = self._context.get_messages()
            tools = self._executor._registry.list_tools() if len(self._executor._registry) > 0 else None

            result = await stream.run(
                messages=messages,
                tools=tools,
                ctx=ctx,
                cancel_token=self._cancel,
            )

        except AgentCancelledError:
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

        # Provider 调用结束
        if self._audit:
            await self._audit.log_event("provider_call_end", {
                "stop_reason": result.stop_reason or "",
                "usage": result.usage,
            }, session_id=ctx.session_id)

        # 更新上下文的 token 使用量（来自 API 返回的真实数据）
        if result.usage:
            self._context.update_usage(result.usage)

        # 写入 assistant 消息到上下文
        self._context.add_assistant_message(
            content=result.text,
            tool_calls=result.tool_calls if result.tool_calls else None,
        )

        # 检查是否有工具调用
        has_tools = bool(result.tool_calls)
        if self._hooks.wants_streaming():
            await self._hooks.emit("stream_end", ctx, resuming=has_tools)

        if has_tools:
            return TurnResult(
                kind=TurnKind.MODEL,
                next_turn=TurnKind.TOOL,
                data=result.tool_calls,
                stream_result=result,
            )
        else:
            return TurnResult(
                kind=TurnKind.MODEL,
                next_turn=None,
                stream_result=result,
            )


class ToolTurn(BaseTurn):
    """
    工具批量执行 Turn。
    职责：
    1. 并行执行 tool_calls
    2. 写入 tool_results 到 context
    3. 分发 tool_start / tool_end / tool_error / safety_blocked 事件
    4. 决定下一步：固定为 MODEL（工具执行完总是回到模型）
    """
    kind = TurnKind.TOOL
    _stage_name = "tool_execution"

    def __init__(
        self,
        context: ContextManager,
        executor: ToolExecutor,
        hooks: HookManager,
        cancel_token: CancellationToken | None,
        audit: AuditLogger | None,
        watchdog_timeout: float,
    ):
        super().__init__(hooks, cancel_token, audit, watchdog_timeout)
        self._context = context
        self._executor = executor

    async def _do_execute(self, ctx: HookContext, input_data: Any = None) -> TurnResult:
        tool_calls = input_data  # ModelTurn 传递的 tool_calls 列表

        # 工具执行前检查取消
        if self._cancel:
            await self._cancel.check()

        await self._hooks.emit("state_change", ctx, state="waiting_tool")

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
        tool_results = await self._executor.execute_batch(tool_calls)

        # 写入工具结果到上下文 + per-tool 事件
        for tc, tr in zip(tool_calls, tool_results):
            msg_result = MsgToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=tr.content,
            )
            self._context.add_tool_result(tc.id, msg_result)

            latency = tr.metadata.get("latency_ms", 0)
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

        return TurnResult(
            kind=TurnKind.TOOL,
            next_turn=TurnKind.MODEL,  # 工具执行完总是回到模型
            data=tool_results,
        )