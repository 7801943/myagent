"""OpenAI 流式 Provider 实现。将 OpenAI SDK 事件统一转为 StreamEvent。"""
import asyncio
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
        formatted = []
        for msg in messages:
            msg_dict = msg.to_openai_dict()

            # OpenAI API 规范要求 role="tool" 的 content 必须是纯文本，
            # 直接发多模态数组会被丢弃或报错。
            # Hack 方案：在这里进行拦截，如果工具结果里有图片，就把它"一分为二"
            if msg_dict.get("role") == "tool" and isinstance(msg_dict.get("content"), list):
                # 检查是否存在图片
                has_image = any(
                    part.get("type") == "image_url"
                    for part in msg_dict["content"]
                )

                if has_image:
                    # 获取原本的纯文本总结（例如："已渲染第 1-3 页..."）
                    text_parts = [
                        part.get("text", "")
                        for part in msg_dict["content"]
                        if part.get("type") == "text"
                    ]
                    tool_text = "\n".join(text_parts) if text_parts else "图片已生成。"

                    # 第 1 步：构造一条纯文本的 tool 消息
                    tool_msg = {
                        "role": "tool",
                        "content": tool_text,
                    }
                    if "tool_call_id" in msg_dict:
                        tool_msg["tool_call_id"] = msg_dict["tool_call_id"]
                    formatted.append(tool_msg)

                    # 第 2 步：构造一条紧随其后的 user 消息，专门用来提交图片
                    user_parts = []
                    tool_name = getattr(msg, "tool_name", None)
                    prompt_text = (
                        f"[系统提示: 以下是工具 '{tool_name}' 渲染出的图片内容]"
                        if tool_name
                        else "[系统提示: 以下是工具产生的图片内容]"
                    )
                    user_parts.append({"type": "text", "text": prompt_text})

                    # 把原本在 tool message 里的图片全都挪到这里来
                    for part in msg_dict["content"]:
                        if part.get("type") == "image_url":
                            user_parts.append(part)

                    formatted.append({"role": "user", "content": user_parts})
                    continue
                else:
                    # 如果工具结果里没有图片，只是个纯文本的 list，
                    # 为了符合 OpenAI 规范也要把它拍平成普通的 string
                    text_parts = [
                        part.get("text", "")
                        for part in msg_dict["content"]
                        if part.get("type") == "text"
                    ]
                    msg_dict["content"] = "\n".join(text_parts)

            formatted.append(msg_dict)

        return formatted

    def format_tools(self, tools: list) -> list[dict]:
        """将工具列表转为 OpenAI function calling 格式。
        
        支持两种输入格式：
        1. BaseTool 对象列表（旧格式，ToolManager 直接调用）
        2. dict 列表（新格式，来自 SessionData）
        """
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                # dict 格式（来自 SessionData）
                result.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters_schema", {}),
                    },
                })
            else:
                # BaseTool 对象格式（旧格式兼容）
                result.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters_schema,
                    },
                })
        return result

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
                "stream_options": {"include_usage": True},  # ✅ 修复：请求返回 token 用量
            }
            if tools:
                create_kwargs["tools"] = tools
            create_kwargs.update(kwargs)

            stream = await client.chat.completions.create(**create_kwargs)

            # 用于累积 tool_call 参数
            tool_call_buffers: dict[int, dict] = {}  # index -> {id, name, args_delta}

            async for chunk in stream:
                # 每个 chunk 后检查取消信号
                if asyncio.current_task() is not None and asyncio.current_task().cancelled():
                    raise asyncio.CancelledError()

                if not chunk.choices:
                    # OpenAI stream_options={"include_usage": True} 时，
                    # 最后一个 chunk 没有 choices 但携带 usage 数据
                    if hasattr(chunk, "usage") and chunk.usage:
                        yield StreamEvent(
                            type="message_end",
                            stop_reason=None,
                            usage={
                                "input_tokens": chunk.usage.prompt_tokens or 0,
                                "output_tokens": chunk.usage.completion_tokens or 0,
                            },
                        )
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