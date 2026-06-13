"""
LLM 通信层（LLMClient）

职责：
1. 封装与 Provider 的全部通信细节
2. 流式聚合：将 AsyncIterator[StreamEvent] → StreamResult
3. 实时 EventBus 转发：每个 chunk 到达时直接 publish 到前端（不经过 Harness 中转）
4. 取消检查：每个 chunk 后检查 asyncio 取消信号
5. 屏蔽 Provider 路由、参数格式化、多模态内容处理等细节

Harness 只需调用 LLMClient.generate(messages, tools, ctx) 即可获得聚合结果。
"""
import asyncio
from dataclasses import dataclass

from myagent.providers.base import StreamEvent
from myagent.providers.router import ProviderRouter
from myagent.context.message import ToolCall
from myagent.core.events import (
    Error,
    EventBus,
    ExecutionContext,
    StateChange,
    StreamDelta,
    StreamEnd,
    StreamStart,
    ThinkingDelta,
)
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


# ── 流式聚合结果（最终将移至 core/models.py，turns.py 删除后此处为规范位置）──

@dataclass
class StreamResult:
    """一次 LLM 调用的聚合结果。"""
    text: str = ""
    reasoning_text: str = ""
    tool_calls: list[ToolCall] | None = None
    stop_reason: str | None = None
    usage: dict | None = None

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []
        if self.usage is None:
            self.usage = {}


# ── LLMClient ──

class LLMClient:
    """
    与 LLM Provider 通信的唯一接口。

    内部完成：
      1. 调用 ProviderRouter.stream() 获取流
      2. 逐 chunk 累积到内部缓冲区（text / reasoning / tool_calls）
      3. 逐 chunk 实时 publish 事件（StreamDelta / ThinkingDelta 等）
      4. 每个 chunk 后检查 asyncio 取消信号
      5. 流结束后构建 StreamResult 返回

    用法：
        client = LLMClient(router, events)
        result = await client.generate(messages, tools, ctx)
    """

    def __init__(
        self,
        router: ProviderRouter,
        events: EventBus | None = None,
        hooks: EventBus | None = None,
    ):
        self._router = router
        self._events = events or hooks
        if self._events is None:
            raise ValueError("LLMClient requires an EventBus")

        # ── 流式聚合缓冲区 ──
        self._text_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_calls: list[ToolCall] = []
        self._tool_call_buffers: dict[int | str, dict] = {}  # id/index → {name, args_json}
        self._stop_reason: str | None = None
        self._usage: dict = {}

    @property
    def router(self) -> ProviderRouter:
        """暴露 ProviderRouter 供 Session 采集模型信息等。"""
        return self._router

    # ── 主入口 ──

    async def generate(
        self,
        messages: list,
        tools: list | None,
        ctx: ExecutionContext,
    ) -> StreamResult:
        """
        流式调用 LLM 并返回聚合结果。
        EventBus 事件在 chunk 到达时实时转发到前端。
        每个 chunk 后检查取消信号，被取消时立即抛出 CancelledError。
        """
        self._reset()

        # 通知前端：Agent 正在思考
        await self._events.publish(ctx.event(StateChange, state="thinking"))

        logger.debug(f"LLMClient generate: session={ctx.session_id}, messages={len(messages)}, tools={bool(tools)}")

        # 如果有流式订阅者，发送 stream_start
        if self._events.wants_streaming():
            await self._events.publish(ctx.event(StreamStart))

        content_started = False  # 标记是否已收到第一个文本内容

        # 迭代 Provider 流
        async for event in self._router.stream(messages, tools):

            # [BUG-FIX] Provider failover：重置缓冲区，避免 Provider A 的残片
            # 与 Provider B 的输出拼接成乱码。
            if event.type == "provider_failover":
                logger.warning(
                    f"Provider failover: {event.meta.get('from_provider')} → "
                    f"{event.meta.get('to_provider')}, reason: {event.meta.get('reason')}"
                )
                self._reset()
                content_started = False
                continue

            # 累积事件到内部缓冲区
            self._accumulate(event)

            # 首次收到文本时，切换状态为 generating
            if not content_started and event.type == "text_delta" and event.text:
                content_started = True
                await self._events.publish(ctx.event(StateChange, state="generating"))

            # 实时分发事件给 EventBus 订阅者（前端流式展示）
            await self._dispatch_event(event, ctx)

            # 每个 chunk 后检查取消信号
            # [BUG-FIX] task.cancelled() 仅在任务已结束后才返回 True，
            # 无法检测挂起的取消。改用 cancelling()（3.11+）+ sleep(0) checkpoint。
            task = asyncio.current_task()
            if task is not None and hasattr(task, "cancelling") and task.cancelling() > 0:
                raise asyncio.CancelledError()
            await asyncio.sleep(0)

        # 构建最终聚合结果
        result = self._build_result()

        # 流式结束事件
        if self._events.wants_streaming():
            await self._events.publish(ctx.event(StreamEnd, resuming=bool(result.tool_calls)))

        logger.debug(f"LLMClient generate done: stop_reason={result.stop_reason}, usage={result.usage}")

        return result

    # ── 流式聚合方法 ──

    def _accumulate(self, event: StreamEvent) -> None:
        """将 StreamEvent 累积到内部缓冲区。"""
        if event.type == "text_delta" and event.text:
            self._text_parts.append(event.text)

        elif event.type == "thinking_delta" and event.text:
            self._reasoning_parts.append(event.text)

        elif event.type == "tool_call_start":
            key = event.tool_call_id or str(len(self._tool_call_buffers))
            self._tool_call_buffers[key] = {
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
            # 清理缓冲区
            key_to_pop = event.tool_call_id
            if key_to_pop:
                self._tool_call_buffers.pop(key_to_pop, None)

        elif event.type == "message_end":
            if event.stop_reason:
                self._stop_reason = event.stop_reason
            if event.usage:
                self._usage = event.usage

    async def _dispatch_event(self, event: StreamEvent, ctx: ExecutionContext) -> None:
        """将关键事件通过 EventBus 广播给 UI 等订阅方。"""
        try:
            if event.type == "text_delta" and event.text:
                await self._events.publish(ctx.event(StreamDelta, delta=event.text))
            elif event.type == "thinking_delta" and event.text:
                await self._events.publish(ctx.event(ThinkingDelta, delta=event.text))
            elif event.type == "error" and event.error:
                await self._events.publish(ctx.event(Error, error=event.error))
        except Exception as e:
            logger.warning(f"LLMClient event dispatch error: {e}")

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
        """重置所有流式聚合状态（每次 generate 自动调用）。"""
        self._text_parts.clear()
        self._reasoning_parts.clear()
        self._tool_calls.clear()
        self._tool_call_buffers.clear()
        self._stop_reason = None
        self._usage.clear()