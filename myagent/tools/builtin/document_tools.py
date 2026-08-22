"""Format-specific tools for line-oriented documents."""

from pathlib import Path

from myagent.tools.api import ToolResult, tool
from myagent.tools.builtin._file_common import _check_path_safety, _detect_file_type
from myagent.tools.builtin._office_common import attach_version, file_version, version_conflict
from myagent.tools.builtin.file_edit import file_edit
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
        "DOCX 按正文段落和表格行生成稳定的逻辑行号。返回文件版本，后续编辑时可传给 expected_version。"
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
    return attach_version(result, target)


@tool(
    name="document_edit",
    description=(
        "精确编辑 DOCX 或纯文本文件（如 TXT、MD）。用 target_content 匹配真实原文，"
        "不要包含 document_read 展示的行号。默认拒绝多处匹配；expected_version 可防止覆盖并发修改。"
    ),
)
async def document_edit(
    path: str,
    target_content: str,
    replacement_content: str,
    start_line: int | None = None,
    end_line: int | None = None,
    allow_multiple: bool = False,
    expected_version: str | None = None,
) -> ToolResult:
    """精确替换行式文档中的内容。

    Args:
        path: DOCX、TXT、MD 或其他纯文本文件路径。
        target_content: 要替换的精确原文，不含展示行号。
        replacement_content: 替换后的文本；空字符串表示删除。
        start_line: 可选搜索起始行，1-based。
        end_line: 可选搜索结束行，包含该行。
        allow_multiple: 是否允许替换搜索范围内的全部匹配，默认 False。
        expected_version: document_read 返回的文件版本；不匹配时拒绝写入。
    """
    target, error = _document_target(path)
    if error:
        return error
    previous_version = file_version(target)
    conflict = version_conflict(target, expected_version, current_version=previous_version)
    if conflict:
        return conflict
    result = await file_edit(
        str(target),
        target_content=target_content,
        replacement_content=replacement_content,
        start_line=start_line,
        end_line=end_line,
        allow_multiple=allow_multiple,
    )
    return attach_version(result, target, previous_version=previous_version)
