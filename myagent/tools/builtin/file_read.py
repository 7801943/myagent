"""file_read 工具入口。"""
import logging
from pathlib import Path

from myagent.tools.api import tool, ToolResult
from myagent.tools.builtin._file_common import (
    _check_path_safety,
    _detect_file_type,
    _read_binary_base64,
    _read_csv,
    _read_docx,
    _read_image_base64,
    _read_pdf_base64,
    _read_pdf_text,
    _read_text,
    _read_xlsx,
)

logger = logging.getLogger(__name__)

@tool(name="file_read",
      description=(
          "读取指定路径的文件内容。支持多种格式：\n"
          "- 文本文件(txt/md/py/json/yaml/log等): 按行输出，带行号\n"
          "- CSV/TSV: 解析后按行输出，列用 | 分隔\n"
          "- XLSX/XLS: 多 Sheet 时先列出 Sheet 信息，需指定 sheet_name 读取\n"
          "- PDF: 提取文本(默认) 或渲染为图片(base64模式)\n"
          "- DOCX: 按文档内容顺序提取段落和表格，按行输出（不支持按真实 Word 页码定位）\n"
          "- 图片(png/jpg/gif等): 以 base64 编码返回\n"
          "参数 start_line_or_page / end_line_or_page 对文本/CSV/DOCX/XLSX 表示输出行号，对 PDF 表示页码；DOCX 不支持真实 Word 页码定位"
      ))
async def file_read(
    path: str,
    sheet_name: str | None = None,
    start_line_or_page: int | None = None,
    end_line_or_page: int | None = None,
    output_format: str = "auto",
    encoding: str | None = None,
    xlsx_range: str | None = None,
    render_mode: str = "values",
    row_mode: str = "text",
    include_tables: bool = True,
    include_merges: bool = True,
) -> ToolResult:
    """
    读取文件内容，支持多种格式。始终显示行号。

    Args:
        path: 文件路径。可传绝对路径、workspace 可见路径或相对路径；在会话工作区中会由工具层解析到允许的真实路径
        sheet_name: XLSX 工作表名称。仅当读取 XLSX 文件且有多个工作表时需要指定，未指定默认先返回所有表名
        start_line_or_page: 起始位置（从1开始，包含该行/页）。对文本/CSV/DOCX/XLSX 表示起始行号，对 PDF 表示起始页码。未指定则从第1行/页开始
        end_line_or_page: 结束位置（包含该行/页）。对文本/CSV/DOCX/XLSX 表示结束行号，对 PDF 表示结束页码。未指定则返回到文件末尾
        output_format: 输出格式，可选 "auto"（自动判断）、"text"（强制文本）、"base64"（强制二进制编码）。默认 "auto"
        encoding: 强制指定文件编码（如 "utf-8"、"gbk"）。默认自动检测
        xlsx_range: XLSX 专用 A1 区域（如 "A1:D20"）。指定后优先于 start_line_or_page/end_line_or_page
        render_mode: XLSX 专用读取模式，可选 "values"、"formulas"、"both"
        row_mode: XLSX 专用 metadata 行模式，可选 "text"、"arrays"、"objects"
        include_tables: XLSX metadata 是否包含 Excel tables 摘要
        include_merges: XLSX metadata 是否包含合并单元格摘要
    """
    # ── 参数校验 ──
    # 类型强制转换：LLM 可能传字符串形式的数字/布尔值
    if isinstance(start_line_or_page, str):
        try:
            start_line_or_page = int(start_line_or_page)
        except (ValueError, TypeError):
            return ToolResult(content=f"start_line_or_page 必须是整数，收到: {start_line_or_page!r}", is_error=True)
    if isinstance(end_line_or_page, str):
        try:
            end_line_or_page = int(end_line_or_page)
        except (ValueError, TypeError):
            return ToolResult(content=f"end_line_or_page 必须是整数，收到: {end_line_or_page!r}", is_error=True)
    if isinstance(output_format, str):
        output_format = output_format.lower()

    error = _check_path_safety(path)
    if error:
        return ToolResult(content=error, is_error=True)

    target = Path(path)
    if not target.exists():
        return ToolResult(content=f"文件不存在: {path}", is_error=True)
    if not target.is_file():
        return ToolResult(content=f"不是文件: {path}", is_error=True)

    # start_line_or_page / end_line_or_page 校验
    if start_line_or_page is not None and start_line_or_page < 1:
        start_line_or_page = 1
    if end_line_or_page is not None and start_line_or_page is not None and end_line_or_page < start_line_or_page:
        return ToolResult(
            content=f"end_line_or_page ({end_line_or_page}) 不能小于 start_line_or_page ({start_line_or_page})。",
            is_error=True,
        )

    # output_format 校验
    if output_format not in ("auto", "text", "base64"):
        return ToolResult(
            content=f"不支持的 output_format: {output_format}。可选: auto, text, base64",
            is_error=True,
        )

    # ── 文件类型检测 ──
    file_type = _detect_file_type(target)
    logger.info("file_read 开始: path=%s, file_type=%s, output_format=%s", path, file_type, output_format)

    # ── 路由到对应解析器 ──
    try:
        # 确定是否使用 base64 模式
        use_base64 = output_format == "base64"
        if output_format == "auto":
            use_base64 = file_type == "image" or file_type == "binary"

        if file_type == "text":
            if use_base64:
                return await _read_binary_base64(target)
            return await _read_text(target, start_line_or_page, end_line_or_page, encoding)

        elif file_type == "csv":
            if use_base64:
                return await _read_binary_base64(target)
            return await _read_csv(target, start_line_or_page, end_line_or_page, encoding)

        elif file_type == "xlsx":
            if use_base64:
                return await _read_binary_base64(target)
            return await _read_xlsx(
                target, sheet_name, start_line_or_page, end_line_or_page,
                xlsx_range=xlsx_range, render_mode=render_mode, row_mode=row_mode,
                include_tables=include_tables, include_merges=include_merges,
            )

        elif file_type == "docx":
            if use_base64:
                return await _read_binary_base64(target)
            return await _read_docx(target, start_line_or_page, end_line_or_page)

        elif file_type == "pdf":
            if use_base64:
                return await _read_pdf_base64(target, start_line_or_page, end_line_or_page)
            return await _read_pdf_text(target, start_line_or_page, end_line_or_page)

        elif file_type == "image":
            return await _read_image_base64(target)

        else:
            # binary 或未知类型
            return await _read_binary_base64(target)

    except Exception as e:
        return ToolResult(content=f"读取文件异常: {type(e).__name__}: {e}", is_error=True)

