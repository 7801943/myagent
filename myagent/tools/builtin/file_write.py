"""file_write 工具入口。"""
import logging
from pathlib import Path

from myagent.tools.api import tool, ToolResult
from myagent.tools.builtin._file_common import _check_path_safety
from myagent.tools.builtin.file_edit import _atomic_write_text

logger = logging.getLogger(__name__)

@tool(name="file_write",
      description="将内容写入指定路径的文件。不存在则创建，已存在则覆盖。")
async def file_write(path: str, content: str,
                     append: bool = False) -> ToolResult:
    """
    将内容写入指定路径的文件。

    Args:
        path: 文件路径。可传绝对路径、workspace 可见路径或相对路径；在会话工作区中会由工具层解析到允许的真实路径。不存在则创建，已存在则覆盖
        content: 要写入的文本内容
        append: 是否追加到文件末尾。默认为 False（覆盖写入）
    """
    logger.info("file_write 开始: path=%s, content_len=%d, append=%s", path, len(content), append)
    error = _check_path_safety(path)
    if error:
        return ToolResult(content=error, is_error=True)

    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        action = "追加" if append else "写入"
        if append:
            # 追加模式不是覆盖写，不使用原子替换（否则会丢失原文件内容）
            with open(target, "a", encoding="utf-8") as f:
                f.write(content)
        else:
            # 覆盖模式改用原子写入（临时文件 + os.replace），与 file_edit 保持一致，
            # 避免写入中途崩溃/断电损坏原文件（Issue 6）。
            _atomic_write_text(target, content)

        logger.info("file_write 成功: %s %d 字符到 %s", action, len(content), target.resolve())
        return ToolResult(
            content=f"文件{action}成功: {target.resolve()} ({len(content)} 字符)",
            metadata={"path": str(target.resolve()),
                      "chars_written": len(content)},
        )
    except Exception as e:
        return ToolResult(content=f"写入文件失败: {e}", is_error=True)
