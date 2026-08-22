"""Format-specific tools for line-oriented documents."""

from pathlib import Path
from typing import Literal

from myagent.tools.api import ToolResult, tool
from myagent.tools.builtin._file_common import _check_path_safety, _detect_file_type
from myagent.tools.builtin._office_common import attach_version, file_version, version_conflict
from myagent.tools.builtin.file_edit import file_edit, resolve_document_edit_scope
from myagent.tools.builtin.file_read import file_read


def _document_target(path: str) -> tuple[Path | None, ToolResult | None]:
    error = _check_path_safety(path)
    if error:
        return None, ToolResult(content=error, is_error=True)
    target = Path(path)
    if not target.exists():
        return None, ToolResult(content=f"文件不存在: {path}", is_error=True)
    if not target.is_file():
        return None, ToolResult(content=f"不是文件: {path}", is_error=True)
    file_type = _detect_file_type(target)
    if file_type not in {"text", "docx"} or target.suffix.lower() == ".doc":
        return None, ToolResult(
            content="document 工具仅支持 DOCX 和纯文本文件（如 TXT、MD）。",
            is_error=True,
        )
    return target, None


@tool(
    name="document_read",
    description=(
        "按行读取 DOCX 或纯文本文件（如 TXT、MD）。纯文本使用物理行号；"
        "DOCX 按正文段落和表格行生成逻辑行号。"
    ),
)
async def document_read(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> ToolResult:
    """读取行式文档。

    Args:
        path: DOCX、TXT、MD 或其他纯文本文件路径。
        start_line: 可选起始行，1-based 且包含该行。
        end_line: 可选结束行，包含该行；省略则读到末尾。
    """
    target, error = _document_target(path)
    if error:
        return error
    result = await file_read(
        str(target),
        start_line_or_page=start_line,
        end_line_or_page=end_line,
    )
    return attach_version(result, target, include_in_content=False)


@tool(
    name="document_edit",
    description=(
        "精确编辑 DOCX 或纯文本文件（如 TXT、MD）。用 target_content 匹配真实原文，"
        "不要包含 document_read 展示的行号。全文唯一匹配时忽略 line_no；"
        "有多个匹配时，必须由 line_no 与 target_content 共同唯一定位。"
        "DOCX 标色只作用于 replacement_content 对应的字符，不会自动标色整行；支持批注。"
    ),
)
async def document_edit(
    path: str,
    target_content: str,
    replacement_content: str,
    line_no: int | None = None,
    highlight: Literal["yellow", "green", "red", "pink"] | None = None,
    comment: str | None = None,
) -> ToolResult:
    """精确替换行式文档中的内容。

    Args:
        path: DOCX、TXT、MD 或其他纯文本文件路径。
        target_content: 要替换的精确原文，不含展示行号。
        replacement_content: 替换后的文本；空字符串表示删除。
        line_no: 可选逻辑行号。全文唯一匹配时忽略；有多个匹配时用于唯一定位。
        highlight: 可选标色颜色: yellow/green/red/pink。DOCX 只标色替换后的字符；仅当 replacement_content 是整行时才会整行标色。
        comment: 可选批注内容。纯文本将按对应文本语法插入注释，DOCX 添加 Word 批注。
    """
    return await _document_edit_impl(
        path=path,
        target_content=target_content,
        replacement_content=replacement_content,
        line_no=line_no,
        highlight=highlight,
        comment=comment,
    )


async def _document_edit_impl(
    *,
    path: str,
    target_content: str,
    replacement_content: str,
    line_no: int | None = None,
    highlight: str | None = None,
    comment: str | None = None,
    expected_version: str | None = None,
) -> ToolResult:
    """Internal implementation retaining optional optimistic concurrency checks."""
    target, error = _document_target(path)
    if error:
        return error
    if target_content == "":
        return ToolResult(content="target_content 不能为空。", is_error=True)
    previous_version = file_version(target)
    conflict = version_conflict(target, expected_version, current_version=previous_version)
    if conflict:
        return conflict
    resolved_line, scope_error = resolve_document_edit_scope(
        target, target_content, replacement_content, line_no
    )
    if scope_error:
        return scope_error
    result = await file_edit(
        str(target),
        target_content=target_content,
        replacement_content=replacement_content,
        start_line=resolved_line,
        end_line=resolved_line,
        allow_multiple=False,
        highlight=highlight,
        comment=comment,
    )
    return attach_version(
        result,
        target,
        previous_version=previous_version,
        include_in_content=False,
    )
