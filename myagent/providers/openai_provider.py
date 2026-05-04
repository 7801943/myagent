"""OpenAI 流式 Provider 实现。将 OpenAI SDK 事件统一转为 StreamEvent。"""
import json
from typing import AsyncIterator

from myagent.providers.base import (
    BaseProvider, StreamEvent, ProviderCapabilities,
    ProviderRateLimitError, ProviderTimeoutError, ProviderAuthError,
)
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class OpenAIProvider(BaseProvider):
    """基于 openai SDK 的流式 Provider。"""

    def __init__(self, name: str, model: str, api_key: str, api_base: str | None = None):
        super().__init__(name, model, api_key, api_base)
        self.capabilities = ProviderCapabilities(supports_vision=True, supports_tool_calls=True)
        self._client = None

    def _get_client(self):
        """懒加载 OpenAI AsyncClient。"""
        if self._client is None:
            from openai import AsyncOpenAI
            kwargs = {"api_key": self.api_key}
            if self.api_base:
                kwargs["base_url"] = self.api_base
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    def format_messages(self, messages: list) -> list[dict]:
        return [msg.to_openai_dict() for msg in messages]

    def format_tools(self, tools: list) -> list[dict]:
        """将 BaseTool 列表转为 OpenAI function calling 格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema,
                },
            }
            for tool in tools
        ]

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamEvent]:
        """
        调用 OpenAI Chat Completions API（流式）并 yield StreamEvent。

        重试策略：由 ProviderRouter 统一管理，此处不内嵌重试。
        """
        client = self._get_client()
        try:
            create_kwargs: dict = {
                "model": self.model,
                "messages": messages,
                "stream": True,
            }
            if tools:
                create_kwargs["tools"] = tools
            create_kwargs.update(kwargs)

            stream = await client.chat.completions.create(**create_kwargs)

            # 用于累积 tool_call 参数
            tool_call_buffers: dict[int, dict] = {}  # index -> {id, name, args_delta}

            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                # 推理链增量
                if getattr(delta, "reasoning_content", None):
                    yield StreamEvent(type="thinking_delta", text=delta.reasoning_content)

                # 文本增量
                if hasattr(delta, "content") and delta.content:
                    yield StreamEvent(type="text_delta", text=delta.content)

                # 工具调用增量
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_call_buffers:
                            tool_call_buffers[idx] = {
                                "id": tc_delta.id or "",
                                "name": tc_delta.function.name if tc_delta.function and tc_delta.function.name else "",
                                "args_json": "",
                            }
                        buf = tool_call_buffers[idx]
                        if tc_delta.id:
                            buf["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                buf["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                buf["args_json"] += tc_delta.function.arguments
                                yield StreamEvent(
                                    type="tool_call_delta",
                                    tool_call_id=buf["id"],
                                    tool_name=buf["name"],
                                    tool_args_delta=tc_delta.function.arguments,
                                )

                # 结束
                if choice.finish_reason:
                    # 先输出所有完整的 tool_call_end
                    for idx, buf in tool_call_buffers.items():
                        try:
                            args = json.loads(buf["args_json"]) if buf["args_json"] else {}
                        except json.JSONDecodeError:
                            args = {}
                        yield StreamEvent(
                            type="tool_call_end",
                            tool_call_id=buf["id"],
                            tool_name=buf["name"],
                            tool_args=args,
                        )

                    # Token 使用量
                    usage = {}
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = {
                            "input_tokens": chunk.usage.prompt_tokens or 0,
                            "output_tokens": chunk.usage.completion_tokens or 0,
                        }

                    yield StreamEvent(
                        type="message_end",
                        stop_reason=choice.finish_reason,
                        usage=usage,
                    )

        except Exception as e:
            mapped = self._map_error(e)
            if mapped is not e:
                raise mapped from e
            raise

    @staticmethod
    def _map_error(e: Exception) -> Exception:
        """将 OpenAI SDK 异常映射为框架异常。"""
        try:
            from openai import RateLimitError, APITimeoutError, AuthenticationError
            if isinstance(e, RateLimitError):
                return ProviderRateLimitError(str(e))
            if isinstance(e, APITimeoutError):
                return ProviderTimeoutError(str(e))
            if isinstance(e, AuthenticationError):
                return ProviderAuthError(str(e))
        except ImportError:
            pass
        return e