"""
ImageHandler：多模态图像输入处理。
支持本地文件、URL、bytes 输入，根据 Provider 类型转换为对应的 content block。

功能：
- 单张 / 批量图像处理
- 图像 Token 估算（OpenAI / Anthropic 计算规则）
- 批量验证（张数限制、总 token 预算、请求体大小）
- 自动降分辨率 / detail 模式控制
"""
import base64
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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

# Provider 硬性限制
_PROVIDER_LIMITS = {
    "anthropic": {"max_images": 20},
    "openai": {"max_images": 300},  # 无硬性限制，设保守值
}


@dataclass
class ImageTokenEstimate:
    """图像 Token 估算结果。"""
    tokens: int
    width: int
    height: int
    detail: str
    tile_count: int = 1


@dataclass
class BatchValidation:
    """批量图像验证结果。"""
    is_valid: bool
    total_tokens: int
    image_count: int
    warnings: list[str]
    errors: list[str]


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
        detail: Literal["auto", "low", "high"] = "auto",
    ) -> ContentBlock:
        """
        处理单张图像输入，返回适配 Provider 的 ContentBlock。

        Args:
            source: 图像来源 -- URL 字符串、本地文件路径、或 bytes
            provider_type: "openai" | "anthropic"
            detail: OpenAI 图像细节级别 ("auto" | "low" | "high")
        """
        if not self._capabilities.supports_vision:
            return ContentBlock(
                type="text",
                text="[图像输入：当前模型不支持多模态，已忽略]",
            )

        # 判断来源类型
        if isinstance(source, bytes):
            return self._from_bytes(source, provider_type, detail=detail)
        elif isinstance(source, Path) or (isinstance(source, str) and not source.startswith(("http://", "https://"))):
            return await self._from_file(Path(source), provider_type, detail=detail)
        elif isinstance(source, str) and source.startswith(("http://", "https://")):
            return self._from_url(source, provider_type, detail=detail)
        else:
            return ContentBlock(type="text", text=f"[不支持的图像来源: {type(source)}]")

    async def prepare_batch(
        self,
        sources: list[str | Path | bytes],
        provider_type: str = "openai",
        detail: Literal["auto", "low", "high"] = "auto",
        text: str | None = None,
    ) -> list[ContentBlock]:
        """
        批量处理多个图像源，返回 ContentBlock 列表（含可选文本）。

        Args:
            sources: 图像来源列表
            provider_type: "openai" | "anthropic"
            detail: 图像细节级别
            text: 可选的文本消息，放在图像前面

        Returns:
            ContentBlock 列表，可直接传入 Agent.run()
        """
        blocks: list[ContentBlock] = []

        if text:
            blocks.append(ContentBlock(type="text", text=text))

        for source in sources:
            block = await self.prepare(source, provider_type, detail=detail)
            blocks.append(block)

        return blocks

    def validate_batch(
        self,
        sources: list[str | Path | bytes],
        provider_type: str = "openai",
        detail: Literal["auto", "low", "high"] = "auto",
        max_tokens_budget: int | None = None,
    ) -> BatchValidation:
        """
        验证批量图像是否超出 Provider 限制。

        检查项：
        1. 张数限制（Anthropic ≤ 20）
        2. 总 token 预算
        3. 文件大小

        Args:
            sources: 图像来源列表
            provider_type: "openai" | "anthropic"
            detail: 图像细节级别
            max_tokens_budget: 图像 token 预算（默认不限）

        Returns:
            BatchValidation 验证结果
        """
        warnings: list[str] = []
        errors: list[str] = []
        image_count = len(sources)

        # 1. 检查张数限制
        limits = _PROVIDER_LIMITS.get(provider_type, _PROVIDER_LIMITS["openai"])
        max_images = limits["max_images"]
        if image_count > max_images:
            errors.append(
                f"{provider_type} 最多支持 {max_images} 张图像，当前 {image_count} 张"
            )

        # 2. 估算总 token
        total_tokens = 0
        total_size_bytes = 0
        for source in sources:
            if isinstance(source, Path) or (isinstance(source, str) and not source.startswith(("http://", "https://"))):
                path = Path(source)
                if path.exists():
                    file_size = path.stat().st_size
                    total_size_bytes += file_size

                    # 单文件大小检查
                    file_size_mb = file_size / (1024 * 1024)
                    if file_size_mb > self._capabilities.max_image_size_mb:
                        errors.append(
                            f"图像 {path.name} 过大: {file_size_mb:.1f}MB > {self._capabilities.max_image_size_mb}MB"
                        )
            elif isinstance(source, bytes):
                total_size_bytes += len(source)

            # 使用保守默认估算（不读取实际分辨率以避免 I/O）
            total_tokens += estimate_image_tokens(
                1024, 1024, detail=detail, provider_type=provider_type
            ).tokens

        # 3. 检查 token 预算
        if max_tokens_budget and total_tokens > max_tokens_budget:
            errors.append(
                f"图像总 token 估算 {total_tokens} 超出预算 {max_tokens_budget}"
            )
        elif max_tokens_budget and total_tokens > max_tokens_budget * 0.7:
            warnings.append(
                f"图像总 token 估算 {total_tokens} 接近预算 {max_tokens_budget} (>70%)"
            )

        # 4. 请求体大小警告
        total_size_mb = total_size_bytes / (1024 * 1024)
        if total_size_mb > 15:
            warnings.append(
                f"图像总大小 {total_size_mb:.1f}MB，可能接近 API 请求体限制"
            )

        if image_count > 20:
            warnings.append(
                f"输入 {image_count} 张图像，模型注意力可能在 20 张以上显著稀释"
            )

        return BatchValidation(
            is_valid=len(errors) == 0,
            total_tokens=total_tokens,
            image_count=image_count,
            warnings=warnings,
            errors=errors,
        )

    async def _from_file(
        self, path: Path, provider_type: str,
        detail: str = "auto",
    ) -> ContentBlock:
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

        logger.info(f"Loaded image: {path.name} ({file_size_mb:.1f}MB)")
        return self._from_bytes(data, provider_type, media_type=media_type, detail=detail)

    def _from_bytes(
        self, data: bytes, provider_type: str,
        media_type: str = "image/png",
        detail: str = "auto",
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

    def _from_url(
        self, url: str, provider_type: str,
        detail: str = "auto",
    ) -> ContentBlock:
        """从 URL 构建 ContentBlock。"""
        return ContentBlock(
            type="image_url",
            url=url,
        )


def estimate_image_tokens(
    width: int,
    height: int,
    detail: Literal["auto", "low", "high"] = "auto",
    provider_type: str = "openai",
) -> ImageTokenEstimate:
    """
    估算图像的 Token 消耗。

    OpenAI 计算规则 (GPT-4o):
      - low:  固定 85 tokens
      - high: 先将最长边缩至 2048px，再缩至最短边 768px，
              然后按 512×512 切片，每个 tile 170 tokens + 85 基础

    Anthropic 计算规则 (Claude):
      - 缩放至最大边 1568px
      - tokens = ceil(width * height / 750)

    Args:
        width: 图像宽度（像素）
        height: 图像高度（像素）
        detail: "auto" | "low" | "high"
        provider_type: "openai" | "anthropic"
    """
    if provider_type == "anthropic":
        return _estimate_anthropic(width, height)
    else:
        return _estimate_openai(width, height, detail)


def _estimate_openai(
    width: int, height: int,
    detail: str = "auto",
) -> ImageTokenEstimate:
    """OpenAI token 估算。"""
    if detail == "low":
        return ImageTokenEstimate(
            tokens=85, width=width, height=height,
            detail="low", tile_count=0,
        )

    # high / auto → 按 high 计算
    # Step 1: 缩放最长边至 2048
    if max(width, height) > 2048:
        scale = 2048 / max(width, height)
        width = int(width * scale)
        height = int(height * scale)

    # Step 2: 缩放最短边至 768
    if min(width, height) > 768:
        scale = 768 / min(width, height)
        width = int(width * scale)
        height = int(height * scale)

    # Step 3: 按 512×512 切片
    tiles_x = math.ceil(width / 512)
    tiles_y = math.ceil(height / 512)
    tile_count = tiles_x * tiles_y

    tokens = 85 + 170 * tile_count

    return ImageTokenEstimate(
        tokens=tokens, width=width, height=height,
        detail="high", tile_count=tile_count,
    )


def _estimate_anthropic(width: int, height: int) -> ImageTokenEstimate:
    """Anthropic token 估算。"""
    # 缩放至最大边 1568px
    if max(width, height) > 1568:
        scale = 1568 / max(width, height)
        width = int(width * scale)
        height = int(height * scale)

    tokens = math.ceil(width * height / 750)

    return ImageTokenEstimate(
        tokens=tokens, width=width, height=height,
        detail="standard", tile_count=1,
    )