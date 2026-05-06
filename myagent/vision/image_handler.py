"""
ImageHandler：多模态图像输入处理。
支持本地文件、URL、bytes 输入，根据 Provider 类型转换为对应的 content block。
"""
import base64
from pathlib import Path

from myagent.providers.base import ProviderCapabilities
from myagent.context.message import ContentBlock
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

# 支持的图像格式
SUPPORTED_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class ImageHandler:
    """
    多模态图像输入处理器。

    根据 Provider 能力检测决定是否启用图像输入：
    - 支持 vision -> 转换为 base64 content block
    - 不支持 vision -> 返回文本替代说明
    """

    def __init__(self, capabilities: ProviderCapabilities | None = None):
        self._capabilities = capabilities or ProviderCapabilities()

    async def prepare(
        self,
        source: str | Path | bytes,
        provider_type: str = "openai",
    ) -> ContentBlock:
        """
        处理图像输入，返回适配 Provider 的 ContentBlock。

        Args:
            source: 图像来源 -- URL 字符串、本地文件路径、或 bytes
            provider_type: "openai" | "anthropic"
        """
        if not self._capabilities.supports_vision:
            return ContentBlock(
                type="text",
                text="[图像输入：当前模型不支持多模态，已忽略]",
            )

        # 判断来源类型
        if isinstance(source, bytes):
            return self._from_bytes(source, provider_type)
        elif isinstance(source, Path) or (isinstance(source, str) and not source.startswith(("http://", "https://"))):
            return await self._from_file(Path(source), provider_type)
        elif isinstance(source, str) and source.startswith(("http://", "https://")):
            return self._from_url(source, provider_type)
        else:
            return ContentBlock(type="text", text=f"[不支持的图像来源: {type(source)}]")

    async def _from_file(self, path: Path, provider_type: str) -> ContentBlock:
        """从本地文件加载图像。"""
        if not path.exists():
            return ContentBlock(type="text", text=f"[图像文件不存在: {path}]")

        media_type = SUPPORTED_MEDIA_TYPES.get(path.suffix.lower())
        if not media_type:
            return ContentBlock(
                type="text",
                text=f"[不支持的图像格式: {path.suffix}]",
            )

        # 检查文件大小
        file_size_mb = path.stat().st_size / (1024 * 1024)
        if file_size_mb > self._capabilities.max_image_size_mb:
            return ContentBlock(
                type="text",
                text=f"[图像文件过大: {file_size_mb:.1f}MB > {self._capabilities.max_image_size_mb}MB]",
            )

        with open(path, "rb") as f:
            data = f.read()

        return self._from_bytes(data, provider_type, media_type=media_type)

    def _from_bytes(
        self, data: bytes, provider_type: str, media_type: str = "image/png"
    ) -> ContentBlock:
        """从 bytes 构建 ContentBlock。"""
        b64_data = base64.b64encode(data).decode("ascii")

        if provider_type == "openai":
            # OpenAI 格式: data URI
            data_uri = f"data:{media_type};base64,{b64_data}"
            return ContentBlock(
                type="image_url",
                url=data_uri,
                media_type=media_type,
            )
        else:
            # Anthropic 格式: base64 数据
            return ContentBlock(
                type="image_base64",
                base64_data=b64_data,
                media_type=media_type,
            )

    def _from_url(self, url: str, provider_type: str) -> ContentBlock:
        """从 URL 构建 ContentBlock。"""
        return ContentBlock(
            type="image_url",
            url=url,
        )