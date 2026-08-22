"""Read-only PDF tool."""

from pathlib import Path

from myagent.tools.api import ToolResult, tool
from myagent.tools.builtin._file_common import _check_path_safety
from myagent.tools.builtin._office_common import attach_version
from myagent.tools.builtin.file_read import file_read


@tool(
    name="pdf_read",
    description=(
        "按页读取 PDF。自动识别文本页、扫描页和混合 PDF：文本页返回可读文本，"
        "图片页渲染为 base64 图像内容块供多模态模型理解。PDF 只读，不提供编辑。"
    ),
)
async def pdf_read(
    path: str,
    start_page: int | None = None,
    end_page: int | None = None,
) -> ToolResult:
    """自动路由并读取 PDF。

    Args:
        path: PDF 文件路径。
        start_page: 可选起始页，1-based 且包含该页。
        end_page: 可选结束页，包含该页；省略则读到末页。
    """
    error = _check_path_safety(path)
    if error:
        return ToolResult(content=error, is_error=True)
    target = Path(path)
    if not target.exists():
        return ToolResult(content=f"文件不存在: {path}", is_error=True)
    if not target.is_file():
        return ToolResult(content=f"不是文件: {path}", is_error=True)
    if target.suffix.lower() != ".pdf":
        return ToolResult(content="pdf_read 仅支持 .pdf 文件。", is_error=True)

    result = await file_read(
        str(target),
        start_line_or_page=start_page,
        end_line_or_page=end_page,
    )
    return attach_version(result, target, include_in_content=False)
