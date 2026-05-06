"""
CapabilityDetector：基于模型名称自动检测 Provider 能力。
内置模型能力注册表，可通过配置覆盖。
"""
import fnmatch

from myagent.providers.base import ProviderCapabilities
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

# 已知支持视觉的模型（支持通配符匹配）
_KNOWN_VISION_MODELS: list[str] = [
    "gpt-4o*",
    "gpt-4-turbo*",
    "gpt-4-vision*",
    "claude-3*",
    "claude-opus-4*",
    "claude-sonnet-4*",
    "gemini*",
]

# 已知支持工具调用的模型（几乎全部，但某些小模型可能不支持）
_KNOWN_NO_TOOL_MODELS: list[str] = [
    "gpt-3.5-turbo-instruct*",
    "text-*",
]

class CapabilityDetector:
    """根据模型名和 provider 类型检测能力。"""

    def __init__(self, overrides: dict | None = None):
        """
        Args:
            overrides: 自定义能力覆盖，格式 {"model_name": ProviderCapabilities}
        """
        self._overrides = overrides or {}

    def detect(self, model: str, provider_type: str) -> ProviderCapabilities:
        """检测指定模型的能力。"""
        # 先检查自定义覆盖
        if model in self._overrides:
            return self._overrides[model]

        supports_vision = any(fnmatch.fnmatch(model, pattern) for pattern in _KNOWN_VISION_MODELS)
        supports_tools = not any(fnmatch.fnmatch(model, pattern) for pattern in _KNOWN_NO_TOOL_MODELS)

        caps = ProviderCapabilities(
            supports_vision=supports_vision,
            supports_tool_calls=supports_tools,
            supports_streaming=True,
        )

        logger.debug(f"Detected capabilities for {model} ({provider_type}): vision={caps.supports_vision}, tools={caps.supports_tool_calls}")
        return caps