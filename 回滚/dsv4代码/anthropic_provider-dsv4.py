"""Anthropic 流式 Provider 实现。将 Anthropic SDK 事件统一转为 StreamEvent。"""
import json
from typing import AsyncIterator

from myagent.providers.base import (
    BaseProvider, StreamEvent, ProviderCapabilities,
    ProviderRateLimitError, ProviderTimeoutError, ProviderAuthError,
)
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class AnthropicProvider(BaseProvider):
    """基于 anthropic SDK 的流式 Provider。"""

    def __init__(self, name: str, model: str, api_key: str, api_base: str | None = None):
        super().__init__(name, model, api_key, api_base)
        self.capabilities = ProviderCapabilities(supports_vision=True, supports_tool_calls=True)
        self._client = None

    def _get_client(self):
        """懒加载 Anthropic AsyncClient。"""
        if self._client is None:
            from anthropic import AsyncAnthropic
            kwargs = {"api_key": self.api_key}
            if self.api_base:
                kwargs["base_url"] = self.api_base
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    def format_messages(self, messages: list) -> list[dict]:
        """
        Anthropic API 要求 system 消息从 messages 中分离。
        返回 (system_prompt, messages_list)。
        """
        return [msg.to_anthropic_dict() for msg in messages]

    def format_tools(self, tools: list) -> list[dict]:
        """将 BaseTool 列表转为 Anthropic tool_use 格式。"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters_schema,  # Anthropic 用 input_schema 而非 parameters
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
        调用 Anthropic Messages API（流式）并 yield StreamEvent。

        Anthropic 的 system prompt 是独立参数，需要从 messages 中提取。
        """
        client = self._get_client()
        try:
            # 分离 system 消息
            system_content = None
            api_messages = []
            for msg in messages:
                if msg.get("role") == "system":
                    system_content = msg.get("content", "")
                else:
                    api_messages.append(msg)

            create_kwargs: dict = {
                "model": self.model,
                "messages": api_messages,
                "max_tokens": kwargs.pop("max_tokens", 4096),
            }
            if system_content:
                create_kwargs["system"] = system_content
            if tools:
                create_kwargs["tools"] = tools
            create_kwargs.update(kwargs)

            async with client.messages.stream(**create_kwargs) as stream:
                # 工具调用参数缓冲
                tool_call_buffers: dict[str, dict] = {}  # tool_use_id -> {id, name, args_json}

                async for event in stream:
                    # 文本增量 / 工具参数增量（合并处理，修复死代码 Bug）
                    if event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            yield StreamEvent(type="text_delta", text=event.delta.text)
                        if hasattr(event.delta, "partial_json"):
                            for buf in tool_call_buffers.values():
                                buf["args_json"] += event.delta.partial_json or ""
                                yield StreamEvent(
                                    type="tool_call_delta",
                                    tool_call_id=buf["id"],
                                    tool_name=buf["name"],
                                    tool_args_delta=event.delta.partial_json or "",
                                )

                    # 工具调用开始
                    elif event.type == "content_block_start":
                        if hasattr(event.content_block, "type") and event.content_block.type == "tool_use":
                            tc_id = event.content_block.id
                            tool_call_buffers[tc_id] = {
                                "id": tc_id,
                                "name": event.content_block.name,
                                "args_json": "",
                            }
                            yield StreamEvent(
                                type="tool_call_start",
                                tool_call_id=tc_id,
                                tool_name=event.content_block.name,
                            )

                    # 消息结束
                    elif event.type == "message_stop":
                        # 输出所有 tool_call_end
                        for buf in tool_call_buffers.values():
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

                    elif event.type == "message_delta":
                        # 获取 stop_reason 和 usage
                        stop_reason = getattr(event.delta, "stop_reason", None)
                        if stop_reason:
                            usage = {}
                            # Anthropic 在 message_delta 中提供 usage
                            if hasattr(event, "usage"):
                                usage = {
                                    "input_tokens": getattr(event.usage, "input_tokens", 0),
                                    "output_tokens": getattr(event.usage, "output_tokens", 0),
                                }
                            yield StreamEvent(
                                type="message_end",
                                stop_reason=stop_reason,
                                usage=usage,
                            )

        except Exception as e:
            mapped = self._map_error(e)
            if mapped is not e:
                raise mapped from e
            raise

    @staticmethod
    def _map_error(e: Exception) -> Exception:
        """将 Anthropic SDK 异常映射为框架异常。"""
        try:
            from anthropic import RateLimitError, APITimeoutError, AuthenticationError
            if isinstance(e, RateLimitError):
                return ProviderRateLimitError(str(e))
            if isinstance(e, APITimeoutError):
                return ProviderTimeoutError(str(e))
            if isinstance(e, AuthenticationError):
                return ProviderAuthError(str(e))
        except ImportError:
            pass
        return e
