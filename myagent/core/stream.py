"""
StreamProcessor：流式调用的统一门面（Facade）。
封装 Provider 调用 + 事件聚合 + Hook 分发。
对外只暴露 run() → StreamResult，消费方无需了解流式细节。

设计决策：
- 组合 ProviderRouter（不继承 Provider）
- 内部吸收原 StreamParser 的 Hook 分发逻辑
- 内部保留原 StreamProcessor 的事件聚合逻辑
"""
from dataclasses import dataclass

from myagent.providers.base import StreamEvent
from myagent.providers.router import ProviderRouter
from myagent.context.message import ToolCall
from myagent.core.hook import HookContext, HookManager
from myagent.core.cancellation import CancellationToken, AgentCancelledError, CancelReason
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


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


class StreamProcessor:
    """
    流式调用门面。
    将 Provider 流式调用 + 事件聚合 + Hook 分发封装为一个统一的 run() 方法。
    
    用法：
        stream = StreamProcessor(router=self._router, hook=self._hooks)
        result: StreamResult = await stream.run(messages, tools, ctx=ctx, cancel_token=self._cancel)
    """

    def __init__(self, router: ProviderRouter, hook: HookManager):
        self._router = router
        self._hook = hook
        # ── 聚合状态 ──
        self._text_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_calls: list[ToolCall] = []
        self._tool_call_buffers: dict[str, dict] = {}  # call_id -> {name, args_json}
        self._stop_reason: str | None = None
        self._usage: dict = {}

    async def run(
        self,
        messages: list,
        tools: list | None = None,
        ctx: HookContext | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> StreamResult:
        """
        一站式流式调用。
        
        Args:
            messages: 格式化后的消息列表
            tools: 格式化后的工具列表
            ctx: Hook 上下文（用于事件分发）
            cancel_token: 取消令牌
            
        Returns:
            StreamResult: 聚合后的完整结果
        """
        self._reset()

        # 触发 stream_start
        if ctx and self._hook.wants_streaming():
            await self._hook.emit("stream_start", ctx)

        content_started = False

        async for event in self._router.stream(messages, tools):
            # 取消检查
            if cancel_token and cancel_token.is_cancelled:
                raise AgentCancelledError(
                    cancel_token.reason or CancelReason.USER_CANCEL,
                    "LLM generation cancelled"
                )

            # 状态追踪：首次文本输出时切换到 generating
            if not content_started and event.type == "text_delta" and event.text:
                content_started = True
                if ctx:
                    await self._hook.emit("state_change", ctx, state="generating")

            # 聚合事件
            self._accumulate(event)

            # 分发到 Hook
            if ctx:
                await self._dispatch(event, ctx)

        # 构建结果
        result = self._build_result()

        # 触发 stream_end
        if ctx and self._hook.wants_streaming():
            has_tools = bool(result.tool_calls)
            await self._hook.emit("stream_end", ctx, resuming=has_tools)

        return result

    # ── 内部方法 ──────────────────────────────────────────────

    def _accumulate(self, event: StreamEvent) -> None:
        """
        事件聚合（原 StreamProcessor.process）。
        将 StreamEvent 累积到内部缓冲区。
        """
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

    async def _dispatch(self, event: StreamEvent, ctx: HookContext) -> None:
        """
        Hook 分发（原 StreamParser.dispatch）。
        将关键事件通过 HookManager 广播给 UI/审计等订阅方。
        """
        try:
            if event.type == "text_delta" and event.text:
                await self._hook.emit("stream", ctx, delta=event.text)
            elif event.type == "thinking_delta" and event.text:
                await self._hook.emit("thinking_stream", ctx, delta=event.text)
            elif event.type == "error" and event.error:
                await self._hook.emit("error", ctx, error=event.error)
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

    def _reset(self) -> None:
        """重置所有内部状态（每次 run 自动调用）。"""
        self._text_parts.clear()
        self._reasoning_parts.clear()
        self._tool_calls.clear()
        self._tool_call_buffers.clear()
        self._stop_reason = None
        self._usage.clear()