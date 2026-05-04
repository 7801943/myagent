"""
统一流事件模型 StreamEvent + BaseProvider 抽象基类。
所有 Provider 必须将原生 SDK 事件转换为 StreamEvent 对上层输出。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

@dataclass
class StreamEvent:
    """所有 Provider 输出的统一事件类型。"""
    type: Literal[
        "text_delta",         # 文本增量片段
        "thinking_delta",     # 推理/思考增量片段
        "tool_call_start",    # 工具调用开始（含 tool_name, call_id）
        "tool_call_delta",    # 工具参数 JSON 增量
        "tool_call_end",      # 工具调用参数完整
        "message_end",        # 本轮消息结束
        "error",              # 错误事件
        "provider_failover",  # Router 发生 failover（基础设施层向上传递的元事件）
    ]
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args_delta: str | None = None
    tool_args: dict | None = None
    stop_reason: str | None = None
    error: Exception | None = None
    usage: dict[str, int] = field(default_factory=dict)  # {"input_tokens": N, "output_tokens": N}
    meta: dict = field(default_factory=dict)             # 通用扩展字段（供 provider_failover 等元事件使用）

@dataclass
class ProviderCapabilities:
    """Provider / 模型的能力描述。"""
    supports_vision: bool = False
    supports_tool_calls: bool = True
    supports_streaming: bool = True
    max_image_size_mb: int = 20

class ProviderError(Exception):
    """Provider 层通用异常基类。"""
    pass

class ProviderRateLimitError(ProviderError):
    """速率限制。"""
    pass

class ProviderTimeoutError(ProviderError):
    """超时。"""
    pass

class ProviderAuthError(ProviderError):
    """认证失败。"""
    pass

class AllProvidersFailedError(ProviderError):
    """所有 Provider 均失败。"""
    def __init__(self, errors: list[tuple[str, Exception]]):
        self.errors = errors
        details = "; ".join(f"{name}: {err}" for name, err in errors)
        super().__init__(f"All providers failed: {details}")

class BaseProvider(ABC):
    """LLM Provider 抽象基类。"""

    def __init__(self, name: str, model: str, api_key: str, api_base: str | None = None):
        self.name = name
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.capabilities: ProviderCapabilities = ProviderCapabilities()

    @abstractmethod
    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 LLM，yield StreamEvent 序列。"""
        ...

    @abstractmethod
    def format_messages(self, messages: list) -> list[dict]:
        """将内部 Message 列表转为该 Provider 的 API 格式。"""
        ...

    @abstractmethod
    def format_tools(self, tools: list) -> list[dict]:
        """将内部 Tool 列表转为该 Provider 的 tools 参数格式。"""
        ...