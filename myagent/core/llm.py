"""
LLM 通信层（LLMClient）

职责：
1. 封装与 Provider 的全部通信细节
2. 流式聚合：将 AsyncIterator[StreamEvent] → StreamResult
3. 实时 Hook 转发：每个 chunk 到达时直接 emit 到前端（不经过 Harness 中转）
4. 取消检查：每个 chunk 后检查 asyncio 取消信号
5. 屏蔽 Provider 路由、参数格式化、多模态内容处理等细节

Harness 只需调用 LLMClient.generate(messages, tools, ctx) 即可获得聚合结果。
"""
import asyncio
from dataclasses import dataclass
from typing import Any

from myagent.providers.base import StreamEvent
from myagent.providers.router import ProviderRouter
from myagent.context.message import ToolCall
from myagent.core.hook import HookContext, HookManager
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
      3. 逐 chunk 实时 emit Hook 事件（stream / thinking_stream 等）
      4. 每个 chunk 后检查 asyncio 取消信号
      5. 流结束后构建 StreamResult 返回

    用法：
        client = LLMClient(router, hooks)
        result = await client.generate(messages, tools, ctx)
    """

    def __init__(self, router: ProviderRouter, hooks: HookManager):
        self._router = router
        self._hooks = hooks

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
        ctx: HookContext,
    ) -> StreamResult:
        """
        流式调用 LLM 并返回聚合结果。
        Hook 事件在 chunk 到达时实时转发到前端。
        每个 chunk 后检查取消信号，被取消时立即抛出 CancelledError。
        """
        self._reset()

        # 通知前端：Agent 正在思考
        await self._hooks.emit("state_change", ctx, state="thinking")

        logger.debug(f"LLMClient generate: session={ctx.session_id}, messages={len(messages)}, tools={bool(tools)}")

        # 如果有流式订阅者，发送 stream_start
        if self._hooks.wants_streaming():
            await self._hooks.emit("stream_start", ctx)

        content_started = False  # 标记是否已收到第一个文本内容

        # 迭代 Provider 流
        async for event in self._router.stream(messages, tools):
            # 累积事件到内部缓冲区
            self._accumulate(event)

            # 首次收到文本时，切换状态为 generating
            if not content_started and event.type == "text_delta" and event.text:
                content_started = True
                await self._hooks.emit("state_change", ctx, state="generating")

            # 实时分发事件给 Hook 订阅者（前端流式展示）
            await self._dispatch_event(event, ctx)

            # 每个 chunk 后检查取消信号
            if asyncio.current_task() is not None and asyncio.current_task().cancelled():
                raise asyncio.CancelledError()

        # 构建最终聚合结果
        result = self._build_result()

        # 流式结束事件
        if self._hooks.wants_streaming():
            await self._hooks.emit("stream_end", ctx, resuming=bool(result.tool_calls))

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

    async def _dispatch_event(self, event: StreamEvent, ctx: HookContext) -> None:
        """将关键事件通过 HookManager 广播给 UI 等订阅方。"""
        try:
            if event.type == "text_delta" and event.text:
                await self._hooks.emit("stream", ctx, delta=event.text)
            elif event.type == "thinking_delta" and event.text:
                await self._hooks.emit("thinking_stream", ctx, delta=event.text)
            elif event.type == "error" and event.error:
                await self._hooks.emit("error", ctx, error=event.error)
        except Exception as e:
            logger.warning(f"LLMClient hook dispatch error: {e}")

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