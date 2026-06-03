"""
FileRead / FileWrite 工具函数。
通过 JsonRpcProxy -> ExecutionEngine.execute_function() 执行。

支持格式：文本(txt/md/py/json/yaml/log 等)、CSV、PDF、XLSX/XLS、DOCX、图片
"""
import base64
import csv
import io
import logging
import mimetypes
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from myagent.tools.api import tool, ToolResult

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 安全策略
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DENIED_PATHS = {
    "/etc", "/root",
    "/sys", "/proc/sys", "/boot", "/dev",
}

_MAX_BASE64_BYTES = 20 * 1024 * 1024  # 20MB base64 输出上限
_PDF_DEFAULT_MAX_PAGES = 3            # 未指定页码范围时，默认最多渲染页数
_PDF_SCAN_FALLBACK_RATIO = 0.5        # 无文本页占比超过此阈值时，自动回退到 base64 渲染


def _check_path_safety(path: str) -> str | None:
    """检查路径是否被安全策略禁止。返回错误信息或 None。"""
    try:
        resolved = str(Path(path).resolve())
    except Exception:
        return f"路径解析失败: {path}"
    for denied_path in _DENIED_PATHS:
        if resolved.startswith(denied_path):
            return f"路径被安全策略禁止: {path} (匹配: {denied_path})"
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 文件类型检测
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".yaml", ".yml", ".toml",
    ".xml", ".html", ".css",
    ".ini", ".cfg", ".conf", ".env", ".gitignore",
    ".log", ".csv", ".tsv",
}

_SPREADSHEET_EXTENSIONS = {".xlsx", ".xls"}
_DOC_EXTENSIONS = {".docx", ".doc"}
_PDF_EXTENSIONS = {".pdf"}

_IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


def _detect_file_type(path: Path) -> str:
    """
    根据扩展名检测文件类型。
    返回: "text" | "csv" | "xlsx" | "docx" | "pdf" | "image" | "binary"
    """
    ext = path.suffix.lower()

    if ext == ".csv":
        return "csv"
    if ext == ".tsv":
        return "csv"
    if ext in _SPREADSHEET_EXTENSIONS:
        return "xlsx"
    if ext in _DOC_EXTENSIONS:
        return "docx"
    if ext in _PDF_EXTENSIONS:
        return "pdf"
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _TEXT_EXTENSIONS:
        return "text"

    # 无扩展名或未知扩展名：尝试检测是否为文本
    if not ext:
        try:
            mime, _ = mimetypes.guess_type(str(path))
            if mime and mime.startswith("text/"):
                return "text"
        except Exception:
            pass

    return "binary"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 编码检测
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _detect_encoding(path: Path, hint: str | None = None) -> str:
    """检测文件编码。hint 优先，然后尝试 chardet，最后回退 utf-8。"""
    if hint:
        return hint

    # 尝试读取前 8KB 用于检测
    try:
        raw = path.open("rb").read(8192)
    except Exception:
        return "utf-8"

    # 检测 BOM
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be"

    # 检测 null 字节（二进制文件）
    if b"\x00" in raw[:1024]:
        return "binary"

    # 尝试 utf-8
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    # chardet 检测
    try:
        import chardet
        result = chardet.detect(raw)
        if result and result.get("encoding"):
            return result["encoding"]
    except ImportError:
        pass

    # 回退 latin-1（永远不会失败）
    return "latin-1"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 文本行号格式化
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _format_lines(
    lines: list[str],
    start_line: int,
    end_line: int | None,
    show_line_numbers: bool,
    max_lines: int,
    total_lines: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    格式化行列表，添加行号，截断处理。
    返回 (格式化后的文本, 元数据)。
    """
    # 确定行号宽度
    display_total = total_lines or len(lines)
    width = len(str(min(display_total, start_line + max_lines - 1)))

    output_parts: list[str] = []
    truncated = False
    actual_count = 0

    for i, line in enumerate(lines):
        line_num = start_line + i
        if end_line is not None and line_num > end_line:
            break
        if actual_count >= max_lines:
            truncated = True
            break

        # 确保行以换行结尾
        if not line.endswith("\n"):
            line += "\n"

        if show_line_numbers:
            output_parts.append(f"{line_num:>{width}} | {line}")
        else:
            output_parts.append(line)
        actual_count += 1

    content = "".join(output_parts)

    # 截断提示
    if truncated:
        remaining = (total_lines or len(lines)) - (start_line + actual_count - 1)
        content += f"\n...[截断：还有 {remaining} 行未显示，使用 start_line/end_line 翻页]\n"

    meta = {
        "lines_output": actual_count,
        "truncated": truncated,
        "start_line": start_line,
        "total_lines": total_lines,
    }

    return content, meta


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 各格式解析器
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _read_text(
    path: Path,
    start_line: int | None,
    end_line: int | None,
    show_line_numbers: bool,
    max_lines: int,
    encoding: str | None,
) -> ToolResult:
    """读取纯文本文件。"""
    enc = _detect_encoding(path, encoding)
    if enc == "binary":
        return ToolResult(
            content=f"文件 {path.name} 似乎是二进制文件，请使用 output_format=\"base64\" 读取。",
            is_error=True,
        )

    try:
        with open(path, "r", encoding=enc, errors="replace") as f:
            all_lines = f.readlines()
    except PermissionError:
        return ToolResult(content=f"权限不足，无法读取: {path}", is_error=True)
    except Exception as e:
        return ToolResult(content=f"读取文件失败: {e}", is_error=True)

    total = len(all_lines)
    s = max(1, start_line or 1)
    e = min(end_line or total, total) if end_line else total

    if s > total:
        return ToolResult(
            content=f"文件共 {total} 行，start_line={s} 超出范围。",
            is_error=True,
            metadata={"total_lines": total},
        )

    sliced = all_lines[s - 1:e]
    content, meta = _format_lines(sliced, s, e, show_line_numbers, max_lines, total)
    meta["path"] = str(path.resolve())
    meta["encoding"] = enc

    logger.info("file_read 纯文本完成: %s, 返回行数=%d", path, meta["lines_output"])
    return ToolResult(content=content, metadata=meta)


async def _read_csv(
    path: Path,
    start_line: int | None,
    end_line: int | None,
    show_line_numbers: bool,
    max_lines: int,
    encoding: str | None,
) -> ToolResult:
    """读取 CSV/TSV 文件。使用 csv 模块解析，按行输出。"""
    enc = _detect_encoding(path, encoding)
    if enc == "binary":
        enc = "utf-8"

    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","

    try:
        with open(path, "r", encoding=enc, errors="replace", newline="") as f:
            # 使用 csv.reader 处理各种引号和转义
            reader = csv.reader(f, delimiter=delimiter)
            rows = list(reader)
    except PermissionError:
        return ToolResult(content=f"权限不足，无法读取: {path}", is_error=True)
    except Exception as e:
        return ToolResult(content=f"读取 CSV 失败: {e}", is_error=True)

    if not rows:
        return ToolResult(content="CSV 文件为空。", metadata={"path": str(path.resolve())})

    total = len(rows)
    s = max(1, start_line or 1)
    e = min(end_line or total, total) if end_line else total

    if s > total:
        return ToolResult(
            content=f"文件共 {total} 行，start_line={s} 超出范围。",
            is_error=True,
            metadata={"total_lines": total},
        )

    # 格式化每行为 | 分隔的列
    formatted_lines: list[str] = []
    for row in rows[s - 1:e]:
        formatted_lines.append(" | ".join(str(cell) for cell in row) + "\n")

    content, meta = _format_lines(formatted_lines, s, e, show_line_numbers, max_lines, total)
    meta["path"] = str(path.resolve())
    meta["encoding"] = enc
    meta["format"] = "csv"

    logger.info("file_read CSV完成: %s, 返回行数=%d", path, meta["lines_output"])
    return ToolResult(content=content, metadata=meta)


async def _read_xlsx(
    path: Path,
    sheet_name: str | None,
    start_line: int | None,
    end_line: int | None,
    show_line_numbers: bool,
    max_lines: int,
) -> ToolResult:
    """读取 XLSX/XLS 文件。多 Sheet 时先列出 Sheet 列表。"""
    try:
        import openpyxl
    except ImportError:
        return ToolResult(
            content="缺少 openpyxl 库，无法解析 XLSX。请安装: pip install openpyxl",
            is_error=True,
        )

    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        return ToolResult(content=f"打开 XLSX 失败: {e}", is_error=True)

    try:
        sheet_names = wb.sheetnames

        # ── 未指定 sheet_name：列出所有工作表 ──
        if sheet_name is None:
            if len(sheet_names) == 1:
                # 只有一个 Sheet，直接读取
                ws = wb[sheet_names[0]]
                return await _format_xlsx_sheet(
                    ws, sheet_names[0], wb, path, start_line, end_line,
                    show_line_numbers, max_lines,
                )

            # 多个 Sheet：列出信息
            lines = [f"文件: {path.name} (XLSX 工作簿)\n"]
            lines.append(f"包含 {len(sheet_names)} 个工作表：\n")
            for i, name in enumerate(sheet_names, 1):
                ws = wb[name]
                row_count = ws.max_row or 0
                col_count = ws.max_column or 0
                lines.append(f"  [{i}] \"{name}\" ({row_count} 行 × {col_count} 列)\n")
            lines.append(
                "\n请指定 sheet_name 参数选择要读取的工作表。\n"
                f"示例: file_read(path=\"{path}\", sheet_name=\"{sheet_names[0]}\")\n"
            )

            wb.close()
            return ToolResult(
                content="".join(lines),
                metadata={
                    "path": str(path.resolve()),
                    "format": "xlsx",
                    "sheet_count": len(sheet_names),
                    "sheet_names": sheet_names,
                },
            )

        # ── 指定了 sheet_name ──
        if sheet_name not in sheet_names:
            wb.close()
            return ToolResult(
                content=(
                    f"工作表 \"{sheet_name}\" 不存在。\n"
                    f"可用工作表: {', '.join(repr(n) for n in sheet_names)}"
                ),
                is_error=True,
                metadata={"sheet_names": sheet_names},
            )

        ws = wb[sheet_name]
        return await _format_xlsx_sheet(
            ws, sheet_name, wb, path, start_line, end_line,
            show_line_numbers, max_lines,
        )
    except Exception as e:
        wb.close()
        return ToolResult(content=f"读取 XLSX 失败: {e}", is_error=True)


async def _format_xlsx_sheet(
    ws, sheet_name: str, wb, path: Path,
    start_line: int | None, end_line: int | None,
    show_line_numbers: bool, max_lines: int,
) -> ToolResult:
    """格式化一个 XLSX 工作表的内容。"""
    total = ws.max_row or 0
    col_count = ws.max_column or 0

    if total == 0:
        wb.close()
        return ToolResult(
            content=f"工作表 \"{sheet_name}\" 为空。",
            metadata={"path": str(path.resolve()), "sheet": sheet_name},
        )

    s = max(1, start_line or 1)
    e = min(end_line or total, total) if end_line else total

    if s > total:
        wb.close()
        return ToolResult(
            content=f"工作表共 {total} 行，start_line={s} 超出范围。",
            is_error=True,
            metadata={"total_lines": total, "sheet": sheet_name},
        )

    # 读取行数据
    formatted_lines: list[str] = []
    for row_idx, row in enumerate(ws.iter_rows(min_row=s, max_row=e, values_only=True), start=s):
        cells = [str(cell) if cell is not None else "" for cell in row]
        formatted_lines.append(" | ".join(cells) + "\n")

    wb.close()

    content, meta = _format_lines(formatted_lines, s, e, show_line_numbers, max_lines, total)
    meta["path"] = str(path.resolve())
    meta["format"] = "xlsx"
    meta["sheet"] = sheet_name
    meta["columns"] = col_count

    logger.info("file_read XLSX完成: %s, sheet=%s, 返回行数=%d", path, sheet_name, meta["lines_output"])
    return ToolResult(content=content, metadata=meta)


async def _read_pdf_text(
    path: Path,
    start_line: int | None,
    end_line: int | None,
    show_line_numbers: bool,
    max_lines: int,
) -> ToolResult:
    """使用 pdfplumber 提取 PDF 文本内容。"""
    # 抑制 pdfminer 的 DEBUG 日志洪水。
    # pdfminer 在解析 PDF 时会为每个 token/操作输出大量 DEBUG 日志
    # （nexttoken / add_results / nextobject / do_keyword / exec 等），
    # 一个普通 PDF 可能产生数千~数万条日志，导致：
    #   1. 子进程 stderr 被海量日志填满，产生严重 I/O 瓶颈
    #   2. 主进程 _drain_stderr 逐行读取并以 INFO 级别转发，进一步加剧延迟
    #   3. PDF 解析看似"卡住"，实际大部分时间花在日志 I/O 上
    # 将 pdfminer 相关 logger 提升到 WARNING 级别，彻底消除底层噪音。
    for _logger_name in ("pdfminer", "pdfminer.psparser", "pdfminer.pdfinterp",
                         "pdfminer.pdfpage", "pdfminer.converter",
                         "pdfminer.layout", "pdfminer.utils"):
        logging.getLogger(_logger_name).setLevel(logging.WARNING)

    try:
        import pdfplumber
    except ImportError:
        return ToolResult(
            content="缺少 pdfplumber 库，无法解析 PDF。请安装: pip install pdfplumber",
            is_error=True,
        )

    try:
        all_lines: list[str] = []
        page_count = 0

        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            start_page = max(1, start_line or 1)
            end_page = min(end_line or page_count, page_count) if end_line else page_count

            if start_page > page_count:
                return ToolResult(
                    content=f"PDF 共 {page_count} 页，start_line={start_page} 超出范围。",
                    is_error=True,
                    metadata={"page_count": page_count},
                )

            for page_num in range(start_page, end_page + 1):
                page = pdf.pages[page_num - 1]
                text = page.extract_text() or ""
                # 添加页码标记
                all_lines.append(f"--- 第 {page_num} 页 ---\n")
                if text.strip():
                    for line in text.split("\n"):
                        all_lines.append(line + "\n")
                else:
                    all_lines.append("[此页无可提取文本，可能是扫描图片]\n")

        if not all_lines:
            return ToolResult(
                content="PDF 未能提取到任何文本。可能是扫描件/图片 PDF，"
                        "请使用 output_format=\"base64\" 以图片形式读取。",
                metadata={"page_count": page_count},
            )

        # ── 扫描页检测：如果大部分页无文本，自动回退到 base64 渲染 ──
        pages_read_count = end_page - start_page + 1
        empty_page_count = sum(
            1 for line in all_lines
            if line.strip() == "[此页无可提取文本，可能是扫描图片]"
        )
        # 每个 empty page 产生 2 行（页码标题 + 提示），实际空页数 = empty_page_count
        if (
            pages_read_count > 0
            and empty_page_count / pages_read_count > _PDF_SCAN_FALLBACK_RATIO
        ):
            logger.debug(
                "PDF 扫描页回退: 读取 %d 页中 %d 页无文本(%.0f%% > 阈值 %.0f%%)，回退到 base64 渲染",
                pages_read_count, empty_page_count,
                empty_page_count / pages_read_count * 100,
                _PDF_SCAN_FALLBACK_RATIO * 100,
            )
            return await _read_pdf_base64(path, start_line, end_line)

        # 把所有行视为一个整体，用页码范围代替行号范围
        total = len(all_lines)
        s = 1  # 已经只取了目标页的内容

        content, meta = _format_lines(all_lines, s, None, show_line_numbers, max_lines, total)
        meta["path"] = str(path.resolve())
        meta["format"] = "pdf"
        meta["page_count"] = page_count
        meta["pages_read"] = f"{start_page}-{end_page}"

        logger.info("file_read PDF文本完成: %s, 读取页数=%d", path, end_page - start_page + 1)
        return ToolResult(content=content, metadata=meta)

    except Exception as e:
        return ToolResult(content=f"解析 PDF 失败: {e}", is_error=True)


async def _read_pdf_base64(
    path: Path,
    start_line: int | None,
    end_line: int | None,
) -> ToolResult:
    """使用 PyMuPDF 将 PDF 页渲染为 JPEG 图片并返回 base64。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return ToolResult(
            content="缺少 PyMuPDF 库，无法渲染 PDF。请安装: pip install PyMuPDF",
            is_error=True,
        )

    try:
        doc = fitz.open(str(path))
        page_count = len(doc)

        # 未指定范围时，默认只渲染前几页，避免生成过大响应
        if start_line is None and end_line is None:
            start_page = 1
            end_page = min(_PDF_DEFAULT_MAX_PAGES, page_count)
        else:
            start_page = max(1, start_line or 1)
            end_page = min(end_line or page_count, page_count) if end_line else page_count

        if start_page > page_count:
            doc.close()
            return ToolResult(
                content=f"PDF 共 {page_count} 页，start_line={start_page} 超出范围。",
                is_error=True,
                metadata={"page_count": page_count},
            )

        content_blocks: list[dict[str, Any]] = []
        total_bytes = 0

        for page_num in range(start_page, end_page + 1):
            page = doc[page_num - 1]
            # 渲染为 150 DPI 的 JPEG
            pix = page.get_pixmap(dpi=150)
            logger.debug(
                "PDF 第 %d/%d 页渲染: %dx%d (dpi=150, quality=75)",
                page_num, page_count, pix.width, pix.height,
            )
            img_bytes = pix.tobytes("jpeg", jpg_quality=75)
            b64_data = base64.b64encode(img_bytes).decode("ascii")
            total_bytes += len(img_bytes)

            content_blocks.append({
                "type": "image_base64",
                "data": b64_data,
                "media_type": "image/jpeg",
                "page": page_num,
            })

            if total_bytes > _MAX_BASE64_BYTES:
                # 超过大小限制，截断
                remaining = end_page - page_num
                desc = (
                    f"PDF 共 {page_count} 页，已渲染第 {start_page}-{page_num} 页为图片。"
                    f"（剩余 {remaining} 页因大小限制未输出，"
                    f"可调整 start_line/end_line 读取其他页）"
                )
                doc.close()
                logger.info("file_read PDF图片完成(截断): %s, 渲染页数=%d", path, page_num - start_page + 1)
                return ToolResult(
                    content=desc,
                    content_blocks=content_blocks,
                    metadata={
                        "path": str(path.resolve()),
                        "format": "pdf",
                        "page_count": page_count,
                        "pages_rendered": list(range(start_page, page_num + 1)),
                        "truncated": True,
                    },
                )

        doc.close()

        pages_hint = ""
        if page_count > end_page:
            pages_hint = (
                f"（仅渲染了前 {end_page - start_page + 1} 页，"
                f"共 {page_count} 页。可指定 start_line/end_line 读取更多页）"
            )
        desc = (
            f"PDF 共 {page_count} 页，已渲染第 {start_page}-{end_page} 页为图片"
            f"（共 {end_page - start_page + 1} 张）。{pages_hint}"
        )
        logger.info("file_read PDF图片完成: %s, 渲染页数=%d", path, end_page - start_page + 1)
        return ToolResult(
            content=desc,
            content_blocks=content_blocks,
            metadata={
                "path": str(path.resolve()),
                "format": "pdf",
                "page_count": page_count,
                "pages_rendered": list(range(start_page, end_page + 1)),
            },
        )

    except Exception as e:
        return ToolResult(content=f"渲染 PDF 失败: {e}", is_error=True)


async def _read_docx(
    path: Path,
    start_line: int | None,
    end_line: int | None,
    show_line_numbers: bool,
    max_lines: int,
) -> ToolResult:
    """读取 DOCX 文件。"""
    ext = path.suffix.lower()

    if ext == ".doc":
        # .doc 格式：python-docx 有限支持，提示用户
        return ToolResult(
            content=(
                "不支持旧版 .doc 格式。请将文件转换为 .docx 格式后重试。\n"
                "转换方法: 使用 LibreOffice 命令 `libreoffice --headless --convert-to docx <file.doc>`"
            ),
            is_error=True,
        )

    try:
        from docx import Document
    except ImportError:
        return ToolResult(
            content="缺少 python-docx 库，无法解析 DOCX。请安装: pip install python-docx",
            is_error=True,
        )

    try:
        doc = Document(str(path))
    except Exception as e:
        return ToolResult(content=f"打开 DOCX 失败: {e}", is_error=True)

    lines: list[str] = []
    for para in doc.paragraphs:
        text = para.text
        if text.strip():
            lines.append(text + "\n")
        else:
            lines.append("\n")  # 保留空行

    total = len(lines)
    if total == 0:
        return ToolResult(
            content="DOCX 文件内容为空。",
            metadata={"path": str(path.resolve()), "format": "docx"},
        )

    s = max(1, start_line or 1)
    e = min(end_line or total, total) if end_line else total

    if s > total:
        return ToolResult(
            content=f"文档共 {total} 行，start_line={s} 超出范围。",
            is_error=True,
            metadata={"total_lines": total},
        )

    sliced = lines[s - 1:e]
    content, meta = _format_lines(sliced, s, e, show_line_numbers, max_lines, total)
    meta["path"] = str(path.resolve())
    meta["format"] = "docx"

    logger.info("file_read DOCX完成: %s, 返回行数=%d", path, meta["lines_output"])
    return ToolResult(content=content, metadata=meta)


# 大图片自动缩放阈值（超过此尺寸则按比例缩小）
_IMAGE_RESIZE_THRESHOLD = 2048  # 像素（长边）
_IMAGE_JPEG_QUALITY = 80


def _resize_image_if_needed(
    data: bytes, media_type: str
) -> tuple[bytes, str, int, int, bool]:
    """
    如果图片尺寸过大，按比例缩小以减少传输体积。

    Returns:
        (处理后的bytes, media_type, width, height, was_resized)
    """
    try:
        from PIL import Image
    except ImportError:
        return data, media_type, 0, 0, False

    try:
        img = Image.open(io.BytesIO(data))
        orig_width, orig_height = img.size
        was_resized = False

        max_dim = max(orig_width, orig_height)
        if max_dim > _IMAGE_RESIZE_THRESHOLD:
            scale = _IMAGE_RESIZE_THRESHOLD / max_dim
            new_width = int(orig_width * scale)
            new_height = int(orig_height * scale)
            logger.debug(
                "图像缩放: %dx%d -> %dx%d (阈值=%d, 缩放比=%.3f)",
                orig_width, orig_height, new_width, new_height,
                _IMAGE_RESIZE_THRESHOLD, scale,
            )
            img = img.resize((new_width, new_height), Image.LANCZOS)
            was_resized = True
        else:
            logger.debug("图像无需缩放: %dx%d (长边=%d, 阈值=%d)",
                         orig_width, orig_height, max_dim, _IMAGE_RESIZE_THRESHOLD)

        buf = io.BytesIO()
        if media_type == "image/jpeg" or was_resized:
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=_IMAGE_JPEG_QUALITY)
            output_media_type = "image/jpeg"
        else:
            img.save(buf, format="PNG")
            output_media_type = "image/png"

        final_width, final_height = img.size
        return buf.getvalue(), output_media_type, final_width, final_height, was_resized

    except Exception as e:
        logger.warning("图像处理异常，返回原始数据: %s", e)
        return data, media_type, 0, 0, False


async def _read_image_base64(path: Path) -> ToolResult:
    """读取图片文件并返回 base64 编码。大图自动缩放以减少传输体积。"""
    ext = path.suffix.lower()
    media_type = _IMAGE_EXTENSIONS.get(ext)

    if not media_type:
        mime, _ = mimetypes.guess_type(str(path))
        media_type = mime or "application/octet-stream"

    try:
        file_size = path.stat().st_size
        data = path.read_bytes()
    except PermissionError:
        return ToolResult(content=f"权限不足，无法读取: {path}", is_error=True)
    except Exception as e:
        return ToolResult(content=f"读取图片失败: {e}", is_error=True)

    # 自动缩放处理
    processed_data, final_media_type, width, height, was_resized = (
        _resize_image_if_needed(data, media_type)
    )

    # 最终大小检查
    if len(processed_data) > _MAX_BASE64_BYTES:
        return ToolResult(
            content=f"图片文件过大 ({file_size / 1024 / 1024:.1f}MB，"
                    f"缩放后仍超过 {_MAX_BASE64_BYTES / 1024 / 1024:.0f}MB 限制)。",
            is_error=True,
            metadata={
                "path": str(path.resolve()),
                "original_size": file_size,
                "dimensions": f"{width}x{height}" if width else "unknown",
            },
        )

    b64_data = base64.b64encode(processed_data).decode("ascii")

    size_info = f"{file_size} 字节"
    if was_resized:
        size_info += f"（已自动缩放，缩放后 {len(processed_data)} 字节）"
    dim_info = f"，尺寸 {width}x{height}" if width else ""

    logger.info("file_read 图片完成: %s, 尺寸=%s", path, f"{width}x{height}" if width else "unknown")
    return ToolResult(
        content=f"图片: {path.name} ({final_media_type}, {size_info}{dim_info})",
        content_blocks=[{
            "type": "image_base64",
            "data": b64_data,
            "media_type": final_media_type,
        }],
        metadata={
            "path": str(path.resolve()),
            "format": "image",
            "media_type": final_media_type,
            "original_size": file_size,
            "encoded_size": len(processed_data),
            "was_resized": was_resized,
            **({"width": width, "height": height} if width else {}),
        },
    )


async def _read_binary_base64(path: Path) -> ToolResult:
    """将任意二进制文件以 base64 编码返回。"""
    try:
        file_size = path.stat().st_size
        if file_size > _MAX_BASE64_BYTES:
            return ToolResult(
                content=f"文件过大 ({file_size / 1024 / 1024:.1f}MB > "
                        f"{_MAX_BASE64_BYTES / 1024 / 1024:.0f}MB 限制)。",
                is_error=True,
            )

        data = path.read_bytes()
    except PermissionError:
        return ToolResult(content=f"权限不足，无法读取: {path}", is_error=True)
    except Exception as e:
        return ToolResult(content=f"读取文件失败: {e}", is_error=True)

    b64_data = base64.b64encode(data).decode("ascii")

    mime, _ = mimetypes.guess_type(str(path))
    media_type = mime or "application/octet-stream"

    logger.info("file_read 二进制完成: %s, size=%d", path, file_size)
    return ToolResult(
        content=f"二进制文件: {path.name} ({media_type}, {file_size} 字节)",
        content_blocks=[{
            "type": "image_base64",
            "data": b64_data,
            "media_type": media_type,
        }],
        metadata={
            "path": str(path.resolve()),
            "format": "binary",
            "media_type": media_type,
            "file_size": file_size,
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口：file_read
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@tool(name="file_read",
      description=(
          "读取指定路径的文件内容。支持多种格式：\n"
          "- 文本文件(txt/md/py/json/yaml/log等): 按行输出，支持行号\n"
          "- CSV/TSV: 解析后按行输出，列用 | 分隔\n"
          "- XLSX/XLS: 多 Sheet 时先列出 Sheet 信息，需指定 sheet_name 读取\n"
          "- PDF: 提取文本(默认) 或渲染为图片(base64模式)\n"
          "- DOCX: 提取段落文本，按行输出\n"
          "- 图片(png/jpg/gif等): 以 base64 编码返回\n"
          "参数 start_line/end_line 对文本/CSV/DOCX 表示行号，对 PDF/XLSX 表示行号/页码"
      ))
async def file_read(
    path: str,
    sheet_name: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    show_line_numbers: bool = True,
    output_format: str = "auto",
    max_lines: int = 2000,
    encoding: str | None = None,
) -> ToolResult:
    """
    读取文件内容，支持多种格式。

    Args:
        path: 文件路径
        sheet_name: XLSX 工作表名（多 Sheet 时必填）
        start_line: 起始行号/页码（1-based，含）
        end_line: 结束行号/页码（含）
        show_line_numbers: 是否在每行前显示行号（默认 True）
        output_format: 输出格式 "auto"|"text"|"base64"
        max_lines: 最大返回行数（防溢出，默认 2000）
        encoding: 强制指定编码（默认自动检测）
    """
    # ── 参数校验 ──
    # 类型强制转换：LLM 可能传字符串形式的数字/布尔值
    if isinstance(start_line, str):
        try:
            start_line = int(start_line)
        except (ValueError, TypeError):
            return ToolResult(content=f"start_line 必须是整数，收到: {start_line!r}", is_error=True)
    if isinstance(end_line, str):
        try:
            end_line = int(end_line)
        except (ValueError, TypeError):
            return ToolResult(content=f"end_line 必须是整数，收到: {end_line!r}", is_error=True)
    if isinstance(show_line_numbers, str):
        show_line_numbers = show_line_numbers.lower() in ("true", "1", "yes")
    if isinstance(max_lines, str):
        try:
            max_lines = int(max_lines)
        except (ValueError, TypeError):
            max_lines = 2000
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

    # start_line / end_line 校验
    if start_line is not None and start_line < 1:
        start_line = 1
    if end_line is not None and start_line is not None and end_line < start_line:
        return ToolResult(
            content=f"end_line ({end_line}) 不能小于 start_line ({start_line})。",
            is_error=True,
        )

    # max_lines 校验
    if max_lines < 1:
        max_lines = 2000

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
            return await _read_text(target, start_line, end_line,
                                    show_line_numbers, max_lines, encoding)

        elif file_type == "csv":
            if use_base64:
                return await _read_binary_base64(target)
            return await _read_csv(target, start_line, end_line,
                                   show_line_numbers, max_lines, encoding)

        elif file_type == "xlsx":
            if use_base64:
                return await _read_binary_base64(target)
            return await _read_xlsx(target, sheet_name, start_line, end_line,
                                    show_line_numbers, max_lines)

        elif file_type == "docx":
            if use_base64:
                return await _read_binary_base64(target)
            return await _read_docx(target, start_line, end_line,
                                    show_line_numbers, max_lines)

        elif file_type == "pdf":
            if use_base64:
                return await _read_pdf_base64(target, start_line, end_line)
            return await _read_pdf_text(target, start_line, end_line,
                                        show_line_numbers, max_lines)

        elif file_type == "image":
            return await _read_image_base64(target)

        else:
            # binary 或未知类型
            return await _read_binary_base64(target)

    except Exception as e:
        return ToolResult(content=f"读取文件异常: {type(e).__name__}: {e}", is_error=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# file_write（保持不变）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@tool(name="file_write",
      description="将内容写入指定路径的文件。不存在则创建，已存在则覆盖。")
async def file_write(path: str, content: str,
                     append: bool = False) -> ToolResult:
    logger.info("file_write 开始: path=%s, content_len=%d, append=%s", path, len(content), append)
    error = _check_path_safety(path)
    if error:
        return ToolResult(content=error, is_error=True)

    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content)

        action = "追加" if append else "写入"
        logger.info("file_write 成功: %s %d 字符到 %s", action, len(content), target.resolve())
        return ToolResult(
            content=f"文件{action}成功: {target.resolve()} ({len(content)} 字符)",
            metadata={"path": str(target.resolve()),
                      "chars_written": len(content)},
        )
    except Exception as e:
        return ToolResult(content=f"写入文件失败: {e}", is_error=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# file_edit — 精确搜索替换编辑
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── 注释风格映射 ──
_COMMENT_STYLES: dict[str, tuple[str, str]] = {
    # (prefix, suffix) — 行注释用 (prefix, "")，块注释用 (prefix, suffix)
    ".css":  ("/* ", " */"),
    ".html": ("<!-- ", " -->"),
    ".xml":  ("<!-- ", " -->"),
    ".md":   ("<!-- ", " -->"),
    ".ini":  ("; ", ""),
    ".toml": ("# ", ""),
    ".yaml": ("# ", ""),
    ".yml":  ("# ", ""),
    ".conf": ("# ", ""),
}

# JSON 等不支持注释的格式
_NO_COMMENT_EXTENSIONS = {".json", ".csv", ".tsv"}


def _get_comment_style(path: Path) -> tuple[str, str] | None:
    """根据扩展名返回注释风格 (prefix, suffix)，不支持则返回 None。"""
    ext = path.suffix.lower()
    if ext in _NO_COMMENT_EXTENSIONS:
        return None
    return _COMMENT_STYLES.get(ext, ("# ", ""))  # 默认用 # 注释


def _build_diff_preview(
    original: str, replaced: str, match_pos: int
) -> str:
    """构建替换位置 ±3 行的 diff 预览。"""
    orig_lines = original.split("\n")
    # 找到匹配所在的行号
    before_text = original[:match_pos]
    match_line = before_text.count("\n")

    # 计算新文本中替换区域的行号（近似）
    repl_lines = replaced.split("\n")

    ctx_start = max(0, match_line - 3)
    ctx_end = min(len(repl_lines), match_line + 4)

    preview_lines = []
    for i in range(ctx_start, ctx_end):
        marker = ">" if match_line <= i < match_line + max(1, 1) else " "
        preview_lines.append(f"  {marker} {i + 1:>{len(str(ctx_end))}} | {repl_lines[i]}")

    return "\n".join(preview_lines)


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写入纯文本文件。"""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8",
        dir=str(path.parent), suffix=".tmp", delete=False,
    )
    try:
        tmp.write(content)
        tmp.close()
        os.replace(tmp.name, str(path))
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _atomic_write_binary(path: Path, data: bytes) -> None:
    """原子写入二进制文件。"""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        dir=str(path.parent), suffix=".tmp", delete=False,
    )
    try:
        tmp.write(data)
        tmp.close()
        os.replace(tmp.name, str(path))
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


# ── 纯文本编辑 ──

async def _edit_text(
    path: Path,
    target_content: str,
    replacement_content: str,
    start_line: int | None,
    end_line: int | None,
    allow_multiple: bool,
    highlight: str | None,
    comment: str | None,
) -> ToolResult:
    """纯文本文件的精确搜索替换。"""
    enc = _detect_encoding(path)
    if enc == "binary":
        return ToolResult(content="文件似乎是二进制格式，无法进行文本编辑。", is_error=True)

    try:
        with open(path, "r", encoding=enc, errors="replace") as f:
            original = f.read()
    except Exception as e:
        return ToolResult(content=f"读取文件失败: {e}", is_error=True)

    # ── 行号范围约束 ──
    search_text = original
    offset = 0  # 原始文本中的字符偏移
    if start_line is not None or end_line is not None:
        lines = original.split("\n")
        total_lines = len(lines)
        s = max(1, start_line or 1)
        e = min(end_line or total_lines, total_lines) if end_line else total_lines
        if s > total_lines:
            return ToolResult(
                content=f"文件共 {total_lines} 行，start_line={s} 超出范围。",
                is_error=True,
            )
        # 计算偏移量
        offset = sum(len(lines[i]) + 1 for i in range(s - 1))
        search_text = "\n".join(lines[s - 1:e])

    # ── 精确匹配 ──
    count = search_text.count(target_content)
    if count == 0:
        hint = ""
        if start_line is not None:
            hint = f"（在行 {start_line}-{end_line or '末尾'} 范围内）"
        return ToolResult(
            content=f"未找到目标内容{hint}。请使用 file_read 确认文件最新内容后重试。",
            is_error=True,
            metadata={"path": str(path.resolve()), "match_count": 0},
        )
    if count > 1 and not allow_multiple:
        return ToolResult(
            content=f"找到 {count} 个匹配，请提供更精确的 target_content，"
                    f"或使用 start_line/end_line 缩小范围，或设置 allow_multiple=True。",
            is_error=True,
            metadata={"path": str(path.resolve()), "match_count": count},
        )

    # ── 执行替换 ──
    if start_line is not None or end_line is not None:
        new_search_text = search_text.replace(target_content, replacement_content)
        lines = original.split("\n")
        total_lines = len(lines)
        s = max(1, start_line or 1)
        e = min(end_line or total_lines, total_lines) if end_line else total_lines
        new_lines = lines[:s - 1] + new_search_text.split("\n") + lines[e:]
        replaced = "\n".join(new_lines)
        actual_count = new_search_text.count(replacement_content) if replacement_content != target_content else count
    else:
        replaced = original.replace(target_content, replacement_content)
        actual_count = count

    # ── 批注（在替换位置上方添加注释）──
    comment_applied = False
    comment_hint = ""
    if comment:
        style = _get_comment_style(path)
        if style is None:
            comment_hint = "（该文件类型不支持注释，已忽略 comment 参数）"
        else:
            prefix, suffix = style
            comment_line = f"{prefix}[AGENT COMMENT] {comment}{suffix}\n"
            # 在第一个替换位置上方插入注释
            idx = replaced.find(replacement_content)
            if idx >= 0:
                # 找到该行的起始位置
                line_start = replaced.rfind("\n", 0, idx)
                line_start = line_start + 1 if line_start >= 0 else 0
                replaced = replaced[:line_start] + comment_line + replaced[line_start:]
                comment_applied = True

    # ── 原子写入 ──
    try:
        _atomic_write_text(path, replaced)
    except Exception as e:
        return ToolResult(content=f"写入文件失败: {e}", is_error=True)

    # ── 构建返回信息 ──
    match_pos = offset if offset else original.find(target_content)
    diff_preview = _build_diff_preview(original, replaced, max(0, match_pos))

    msg = f"替换成功: {path.name}，共 {actual_count} 处替换。"
    if highlight:
        msg += "（纯文本文件不支持标色，已忽略 highlight 参数）"
    if comment_hint:
        msg += comment_hint
    elif comment_applied:
        msg += " 已添加批注注释。"

    logger.info("file_edit 纯文本成功: path=%s, replacements=%d", path, actual_count)
    return ToolResult(
        content=msg + f"\n\n预览:\n{diff_preview}",
        metadata={
            "path": str(path.resolve()),
            "format": "text",
            "replacements": actual_count,
            "highlight": highlight,
            "comment_added": comment_applied,
        },
    )


# ── DOCX 编辑 ──

def _insert_docx_run_after(anchor_run: Any, text: str, template_run: Any | None = None) -> Any:
    """Insert a run after `anchor_run`, copying character formatting from `template_run`."""
    from docx.oxml import OxmlElement
    from docx.text.run import Run

    template_run = template_run or anchor_run
    new_r = OxmlElement("w:r")
    r_pr = template_run._r.rPr
    if r_pr is not None:
        new_r.append(deepcopy(r_pr))
    anchor_run._r.addnext(new_r)

    new_run = Run(new_r, anchor_run._parent)
    new_run.text = text
    return new_run


def _normalize_docx_edit_text(
    target_content: str,
    replacement_content: str,
) -> tuple[str, str]:
    """Tolerate a single trailing newline copied from file_read's paragraph output."""
    if (
        target_content.endswith("\n")
        and replacement_content.endswith("\n")
        and "\n" not in target_content[:-1]
        and "\n" not in replacement_content[:-1]
    ):
        return target_content[:-1], replacement_content[:-1]
    return target_content, replacement_content


async def _edit_docx(
    path: Path,
    target_content: str,
    replacement_content: str,
    allow_multiple: bool,
    highlight: str | None,
    comment: str | None,
) -> ToolResult:
    """DOCX 文件的精确搜索替换，保留格式。"""
    try:
        from docx import Document
    except ImportError:
        return ToolResult(
            content="缺少 python-docx 库。请安装: pip install python-docx",
            is_error=True,
        )

    try:
        doc = Document(str(path))
    except Exception as e:
        return ToolResult(content=f"打开 DOCX 失败: {e}", is_error=True)

    target_content, replacement_content = _normalize_docx_edit_text(
        target_content,
        replacement_content,
    )

    # ── 构建虚拟文本流及 Run 映射 ──
    virtual_parts: list[str] = []
    # 每个 entry: (para_idx, run_idx, char_start, char_end)
    run_map: list[tuple[int, int, int, int]] = []
    char_pos = 0

    for pi, para in enumerate(doc.paragraphs):
        if pi > 0:
            virtual_parts.append("\n")
            char_pos += 1
        for ri, run in enumerate(para.runs):
            text = run.text
            start = char_pos
            end = char_pos + len(text)
            if text:
                run_map.append((pi, ri, start, end))
                virtual_parts.append(text)
                char_pos = end

    virtual_text = "".join(virtual_parts)

    # ── 精确匹配 ──
    count = virtual_text.count(target_content)
    if count == 0:
        return ToolResult(
            content="未找到目标内容。请使用 file_read 确认文档最新内容后重试。",
            is_error=True,
            metadata={"path": str(path.resolve()), "match_count": 0},
        )
    if count > 1 and not allow_multiple:
        return ToolResult(
            content=f"找到 {count} 个匹配，请提供更精确的 target_content 或设置 allow_multiple=True。",
            is_error=True,
            metadata={"path": str(path.resolve()), "match_count": count},
        )

    matches: list[tuple[int, int, list[tuple[int, int, int, int]]]] = []
    search_start = 0
    while search_start < len(virtual_text):
        match_idx = virtual_text.find(target_content, search_start)
        if match_idx < 0:
            break
        match_end = match_idx + len(target_content)
        involved = [
            (pi, ri, cs, ce)
            for pi, ri, cs, ce in run_map
            if cs < match_end and ce > match_idx
        ]
        if involved:
            matches.append((match_idx, match_end, involved))
        search_start = match_end
        if not allow_multiple:
            break

    cross_paragraph = [
        (match_idx, match_end)
        for match_idx, match_end, involved in matches
        if involved[0][0] != involved[-1][0]
    ]
    if cross_paragraph:
        return ToolResult(
            content=(
                "DOCX 编辑被拒绝：当前安全编辑器只支持单个段落内的替换。"
                "这次匹配跨越了多个段落，继续写入可能产生额外空行或破坏段落对齐。"
                "请把 target_content 缩小到一个段落内后重试。"
            ),
            is_error=True,
            metadata={
                "path": str(path.resolve()),
                "match_count": count,
                "cross_paragraph_matches": len(cross_paragraph),
            },
        )

    # ── 执行替换（逐个匹配处理）──
    actual_count = 0
    comment_count = 0
    for match_idx, match_end, involved in reversed(matches):
        first_pi, first_ri, first_cs, first_ce = involved[0]
        last_pi, last_ri, last_cs, last_ce = involved[-1]
        first_para = doc.paragraphs[first_pi]
        first_run = first_para.runs[first_ri]

        # 第一段第一段：保留前缀 + 替换内容
        prefix = target_content[:0]  # 空
        if first_cs < match_idx:
            prefix = first_run.text[:match_idx - first_cs]

        # 最后一段最后一段：保留后缀
        last_para = doc.paragraphs[last_pi]
        last_run = last_para.runs[last_ri]
        suffix = ""
        if last_ce > match_end:
            suffix = last_run.text[match_end - last_cs:]

        runs = first_para.runs
        first_run.text = prefix
        if first_ri != last_ri:
            for idx in range(first_ri + 1, last_ri):
                runs[idx].text = ""
            last_run.text = ""

        replacement_run = _insert_docx_run_after(first_run, replacement_content)
        if suffix:
            _insert_docx_run_after(replacement_run, suffix, last_run)

        # ── 标色 ──
        if highlight:
            try:
                from docx.enum.text import WD_COLOR_INDEX
                _DOCX_COLOR_MAP = {
                    "yellow": WD_COLOR_INDEX.YELLOW,
                    "green": WD_COLOR_INDEX.BRIGHT_GREEN,
                    "red": WD_COLOR_INDEX.RED,
                    "pink": WD_COLOR_INDEX.PINK,
                }
                color = _DOCX_COLOR_MAP.get(highlight)
                if color:
                    replacement_run.font.highlight_color = color
            except Exception as e:
                logger.warning("DOCX 标色失败: %s", e)

        # ── 批注 ──
        if comment:
            try:
                if hasattr(doc, "add_comment"):
                    doc.add_comment(
                        replacement_run,
                        text=comment,
                        author="Agent",
                        initials="AG",
                    )
                    comment_count += 1
                else:  # pragma: no cover - compatibility with older python-docx
                    logger.warning("python-docx 版本不支持批注，需要 >= 1.2.0")
            except Exception as e:
                logger.warning("DOCX 批注添加失败: %s", e)

        actual_count += 1

    # ── 原子写入 ──
    try:
        buf = io.BytesIO()
        doc.save(buf)
        _atomic_write_binary(path, buf.getvalue())
    except Exception as e:
        return ToolResult(content=f"保存 DOCX 失败: {e}", is_error=True)

    comment_status = ""
    if comment and comment_count:
        comment_status = f" 已添加批注 {comment_count} 处。"
    elif comment:
        comment_status = " 批注添加失败，已完成文本替换。"

    logger.info("file_edit DOCX成功: path=%s, replacements=%d", path, actual_count)
    return ToolResult(
        content=f"DOCX 替换成功: {path.name}，共 {actual_count} 处替换。"
                + (" 已标色。" if highlight else "")
                + comment_status,
        metadata={
            "path": str(path.resolve()),
            "format": "docx",
            "replacements": actual_count,
            "highlight": highlight,
            "comment_added": comment_count > 0,
            "comments_added": comment_count,
        },
    )




# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# XLSX 编辑辅助函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _normalize_xlsx_row(formatted: str) -> str:
    """归一化行文本，用于匹配比较。去除首尾空白，压缩 pipe 周围多余空格。"""
    return " | ".join(c.strip() for c in formatted.split(" | "))


def _format_xlsx_row(row) -> str:
    """将 openpyxl 行数据格式化为 pipe 分隔文本（与 file_read 一致）。"""
    cells = [str(cell) if cell is not None else "" for cell in row]
    return " | ".join(cells)


def _infer_cell_value(s: str):
    """尝试将字符串转为合适的 Python 类型。"""
    s = s.strip()
    if s == "":
        return None
    # 整数
    try:
        return int(s)
    except ValueError:
        pass
    # 浮点
    try:
        return float(s)
    except ValueError:
        pass
    # 布尔
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s


def _check_merge_conflicts(ws, affected_rows: list[int], operation: str) -> str | None:
    """
    检查操作是否会影响合并单元格区域。

    Args:
        ws: 工作表
        affected_rows: 受影响的行号列表
        operation: "replace" | "delete" | "insert"

    Returns:
        冲突描述字符串，无冲突返回 None
    """
    conflicts = []
    for mr in ws.merged_cells.ranges:
        merge_rows = set(range(mr.min_row, mr.max_row + 1))
        affected_set = set(affected_rows)

        if operation == "replace":
            overlap = merge_rows & affected_set
            if overlap and overlap != merge_rows:
                conflicts.append(
                    f"合并区域 {mr} 与替换行 {sorted(overlap)} 部分重叠"
                )

        elif operation == "delete":
            overlap = merge_rows & affected_set
            if overlap and overlap != merge_rows:
                conflicts.append(
                    f"删除行 {sorted(overlap)} 会破坏合并区域 {mr}"
                )

        elif operation == "insert":
            insert_row = min(affected_rows)
            if mr.min_row < insert_row <= mr.max_row:
                conflicts.append(
                    f"在行 {insert_row} 插入会破坏合并区域 {mr}"
                )

    if conflicts:
        return "操作被阻止——涉及合并单元格冲突：\n" + "\n".join(f"  - {c}" for c in conflicts)
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# file_edit_table — XLSX 专用编辑工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@tool(name="file_edit_table",
      description=(
          "编辑 XLSX 表格文件。通过行级匹配进行精确替换。\n"
          "target_content 和 replacement_content 均使用 'cell1 | cell2 | cell3' 格式，\n"
          "与 file_read 输出格式一致。\n\n"
          "功能：\n"
          "- 单行替换：匹配一行，替换为新内容\n"
          "- 多行替换：target_content 和 replacement_content 用 \\n 分隔多行\n"
          "- 删除行：replacement_content 设为 '__DELETE__'\n"
          "- 消歧：当多行匹配时，通过 target_rows 指定行号\n\n"
          "注意：\n"
          "- 单元格值中若包含 ' | '，匹配时需注意可能的列错位\n"
          "- 匹配失败时先 file_read 确认最新内容再重试\n"
          "- 不支持 .xls 旧格式"
      ))
async def file_edit_table(
    path: str,
    target_content: str,
    replacement_content: str,
    sheet_name: str | None = None,
    target_rows: list[int] | None = None,
    allow_multiple: bool = False,
    highlight: str | None = None,
    comment: str | None = None,
) -> ToolResult:
    """
    编辑 XLSX 表格文件。

    Args:
        path: 文件路径，仅支持 .xlsx
        target_content: 要被替换的行内容（与 file_read 输出格式一致）
            格式: "cell1 | cell2 | cell3"
            多行: 用 \n 分隔多行
        replacement_content: 替换后的行内容
            格式: "cell1 | cell2 | cell3"
            多行: 用 \n 分隔多行
            删除行: 使用 "__DELETE__"
        sheet_name: 工作表名（单 Sheet 自动选择，多 Sheet 必填）
        target_rows: 可选消歧参数。当 target_content 匹配多行时，
            通过行号（1-based）指定要替换哪些行
        allow_multiple: 是否允许替换多个匹配行（默认 False）
        highlight: 标色颜色: "yellow"/"green"/"red"/"pink"/None
        comment: 批注内容，None=不添加批注
    """
    import difflib

    try:
        import openpyxl
        from openpyxl.comments import Comment as XlComment
    except ImportError:
        return ToolResult(
            content="缺少 openpyxl 库。请安装: pip install openpyxl",
            is_error=True,
        )

    # ── 路径安全检查 ──
    error = _check_path_safety(path)
    if error:
        return ToolResult(content=error, is_error=True)

    target = Path(path)
    if not target.exists():
        return ToolResult(content=f"文件不存在: {path}", is_error=True)
    if not target.is_file():
        return ToolResult(content=f"不是文件: {path}", is_error=True)

    ext = target.suffix.lower()
    if ext == ".xls":
        return ToolResult(
            content="不支持旧版 .xls 格式。请转换为 .xlsx 后重试。",
            is_error=True,
        )
    if ext != ".xlsx":
        return ToolResult(
            content=f"file_edit_table 仅支持 .xlsx 文件，当前文件类型: {ext or '(无扩展名)'}。",
            is_error=True,
        )

    # ── highlight 值校验 ──
    if highlight is not None and highlight not in ("yellow", "green", "red", "pink"):
        return ToolResult(
            content=f"不支持的 highlight 颜色: {highlight!r}。可选: yellow, green, red, pink",
            is_error=True,
        )

    logger.info("file_edit_table 开始: path=%s", path)

    try:
        wb = openpyxl.load_workbook(str(path))
    except Exception as e:
        return ToolResult(content=f"打开 XLSX 失败: {e}", is_error=True)

    # ── 获取目标 Sheet ──
    try:
        sheet_names = wb.sheetnames

        if sheet_name is None:
            if len(sheet_names) == 1:
                ws = wb[sheet_names[0]]
            else:
                wb.close()
                return ToolResult(
                    content=(
                        f"文件包含 {len(sheet_names)} 个工作表，必须指定 sheet_name。\n"
                        f"可用工作表: {', '.join(repr(n) for n in sheet_names)}"
                    ),
                    is_error=True,
                    metadata={"sheet_names": sheet_names},
                )
        else:
            if sheet_name not in sheet_names:
                wb.close()
                return ToolResult(
                    content=(
                        f"工作表 \"{sheet_name}\" 不存在。\n"
                        f"可用工作表: {', '.join(repr(n) for n in sheet_names)}"
                    ),
                    is_error=True,
                    metadata={"sheet_names": sheet_names},
                )
            ws = wb[sheet_name]
    except Exception as e:
        wb.close()
        return ToolResult(content=f"获取工作表失败: {e}", is_error=True)

    # ── 收集格式化行 ──
    max_col = ws.max_column or 0
    formatted_rows: list[str] = []
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True):
        formatted_rows.append(_format_xlsx_row(row))

    total_rows = len(formatted_rows)
    if total_rows == 0:
        wb.close()
        return ToolResult(
            content="工作表为空，无法执行编辑。",
            is_error=True,
        )

    # ── 解析 target_content 为多行 ──
    target_lines = [_normalize_xlsx_row(line) for line in target_content.split("\n")]
    is_delete = replacement_content.strip() == "__DELETE__"

    if is_delete:
        replacement_lines = []
    else:
        replacement_lines = replacement_content.split("\n")

    # ── 多行匹配：在格式化行序列中搜索连续 N 行完全匹配的位置 ──
    normalized_rows = [_normalize_xlsx_row(r) for r in formatted_rows]
    match_count = len(target_lines)
    all_matches: list[int] = []  # 起始行号列表（1-based）

    for i in range(len(normalized_rows) - match_count + 1):
        if all(normalized_rows[i + j] == target_lines[j] for j in range(match_count)):
            all_matches.append(i + 1)  # 1-based

    # ── 匹配结果判断 ──
    if len(all_matches) == 0:
        wb.close()
        # 模糊搜索提示
        close = difflib.get_close_matches(
            _normalize_xlsx_row(target_content.split("\n")[0]),
            normalized_rows,
            n=3, cutoff=0.6,
        )
        hint = ""
        if close:
            hint = "\n最接近的行:\n" + "\n".join(f"  - {r}" for r in close)
        return ToolResult(
            content=f"未找到目标内容。请使用 file_read 确认工作表内容后重试。{hint}",
            is_error=True,
            metadata={"path": str(target.resolve()), "match_count": 0},
        )

    # 确定要操作的匹配
    matches_to_apply: list[int] = []

    if len(all_matches) == 1:
        matches_to_apply = all_matches
    else:
        # 多个匹配
        if target_rows is not None:
            # 验证 target_rows 在匹配结果中
            for row_num in target_rows:
                if row_num not in all_matches:
                    wb.close()
                    return ToolResult(
                        content=(
                            f"行 {row_num} 不在匹配结果中。\n"
                            f"匹配起始行: {all_matches}\n"
                            f"请检查 target_rows 参数。"
                        ),
                        is_error=True,
                        metadata={"matches": all_matches},
                    )
            matches_to_apply = sorted(target_rows)
        elif allow_multiple:
            matches_to_apply = all_matches
        else:
            wb.close()
            return ToolResult(
                content=(
                    f"找到 {len(all_matches)} 处匹配，请通过以下方式消歧：\n"
                    f"1. 使用 target_rows 指定行号（匹配起始行: {all_matches}）\n"
                    f"2. 使用 allow_multiple=True 替换所有匹配\n"
                    f"3. 提供更精确的 target_content"
                ),
                is_error=True,
                metadata={"matches": all_matches},
            )

    # ── 合并单元格安全检查 ──
    all_affected_rows: list[int] = []
    for match_start in matches_to_apply:
        all_affected_rows.extend(range(match_start, match_start + match_count))

    operation = "delete" if is_delete else "replace"
    merge_conflict = _check_merge_conflicts(ws, list(set(all_affected_rows)), operation)
    if merge_conflict:
        wb.close()
        return ToolResult(content=merge_conflict, is_error=True)

    # ── 标色映射 ──
    from openpyxl.styles import PatternFill
    _XLSX_FILL_MAP = {
        "yellow": PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid"),
        "green":  PatternFill(start_color="92D050", end_color="92D050", fill_type="solid"),
        "red":    PatternFill(start_color="FF6B6B", end_color="FF6B6B", fill_type="solid"),
        "pink":   PatternFill(start_color="FFB6C1", end_color="FFB6C1", fill_type="solid"),
    }

    actual_count = 0
    warnings: list[str] = []

    # ── 从后往前执行，避免行号偏移 ──
    for match_start in sorted(matches_to_apply, reverse=True):
        if is_delete:
            # ── Case B: 删除行 ──
            for offset in range(match_count - 1, -1, -1):
                row_idx = match_start + offset
                ws.delete_rows(row_idx, 1)
            actual_count += 1

        elif match_count == 1 and len(replacement_lines) == 1:
            # ── Case A: 单行替换单行（最常见）──
            row_idx = match_start
            new_cells = replacement_lines[0].split(" | ")
            target_cell_count = formatted_rows[row_idx - 1].count(" | ") + 1 if formatted_rows[row_idx - 1] else 1

            if len(new_cells) != target_cell_count:
                warnings.append(
                    f"行 {row_idx}: 替换列数({len(new_cells)})与原列数({target_cell_count})不一致"
                )

            for col_idx, value in enumerate(new_cells, start=1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.value = _infer_cell_value(value)
                if highlight and highlight in _XLSX_FILL_MAP:
                    cell.fill = _XLSX_FILL_MAP[highlight]
                if comment:
                    cell.comment = XlComment(comment, "Agent")

            # 清空多余列
            for col_idx in range(len(new_cells) + 1, max_col + 1):
                ws.cell(row=row_idx, column=col_idx).value = None
            actual_count += 1

        else:
            # ── Case C: 多行替换多行（N→M）──
            # 先处理：替换前 min(N,M) 行
            min_count = min(match_count, len(replacement_lines))
            for i in range(min_count):
                row_idx = match_start + i
                new_cells = replacement_lines[i].split(" | ")
                for col_idx, value in enumerate(new_cells, start=1):
                    cell = ws.cell(row=row_idx, column=col_idx)
                    cell.value = _infer_cell_value(value)
                    if highlight and highlight in _XLSX_FILL_MAP:
                        cell.fill = _XLSX_FILL_MAP[highlight]
                    if comment:
                        cell.comment = XlComment(comment, "Agent")
                # 清空多余列
                for col_idx in range(len(new_cells) + 1, max_col + 1):
                    ws.cell(row=row_idx, column=col_idx).value = None

            if len(replacement_lines) > match_count:
                # 插入额外行
                extra_count = len(replacement_lines) - match_count
                insert_after = match_start + match_count - 1
                ws.insert_rows(insert_after + 1, extra_count)
                # 填入新行数据
                for i, line in enumerate(replacement_lines[match_count:]):
                    row_idx = insert_after + 1 + i
                    cells = line.split(" | ")
                    for col_idx, value in enumerate(cells, start=1):
                        cell = ws.cell(row=row_idx, column=col_idx)
                        cell.value = _infer_cell_value(value)
                        if highlight and highlight in _XLSX_FILL_MAP:
                            cell.fill = _XLSX_FILL_MAP[highlight]
                        if comment:
                            cell.comment = XlComment(comment, "Agent")

            elif len(replacement_lines) < match_count:
                # 删除多余行（从后往前）
                for i in range(match_count - 1, len(replacement_lines) - 1, -1):
                    ws.delete_rows(match_start + i, 1)

            actual_count += 1

    # ── 原子写入 ──
    try:
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        _atomic_write_binary(target, buf.getvalue())
    except Exception as e:
        return ToolResult(content=f"保存 XLSX 失败: {e}", is_error=True)

    msg = f"XLSX 表格编辑成功: {target.name}，共 {actual_count} 处操作。"
    if warnings:
        msg += f" 警告: {'; '.join(warnings[:5])}"
    if highlight:
        msg += " 已标色。"
    if comment:
        msg += " 已添加批注。"

    logger.info("file_edit_table 成功: path=%s, operations=%d", target, actual_count)
    return ToolResult(
        content=msg,
        metadata={
            "path": str(target.resolve()),
            "format": "xlsx",
            "replacements": actual_count,
            "highlight": highlight,
            "comment_added": bool(comment),
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# file_edit — 纯文本 + DOCX 编辑
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@tool(name="file_edit",
      description=(
          "精确编辑已有文件。支持纯文本文件和 DOCX 两种类型。\n"
          "通过 target_content 精确匹配原始内容，替换为 replacement_content。\n"
          "支持对修改位置进行标色（highlight）和添加批注（comment）。\n\n"
          "使用建议：\n"
          "- 修改已有文件优先使用 file_edit，而非 file_write\n"
          "- target_content 必须精确匹配原文（含缩进和空白）\n"
          "- 匹配失败时先 file_read 确认最新内容再重试\n"
          "- 不支持 .doc 旧格式\n"
          "- 编辑 XLSX 请使用 file_edit_table"
      ))
async def file_edit(
    path: str,
    target_content: str,
    replacement_content: str,
    start_line: int | None = None,
    end_line: int | None = None,
    allow_multiple: bool = False,
    highlight: str | None = None,
    comment: str | None = None,
) -> ToolResult:
    """
    精确编辑已有文件（纯文本 / DOCX）。

    Args:
        path: 文件路径，支持文本/DOCX
        target_content: 要被替换的原始文本（精确匹配，必填）
        replacement_content: 替换后的新文本（必填）
        start_line: 纯文本文件搜索范围起始行（1-based）
        end_line: 纯文本文件搜索范围结束行
        allow_multiple: 是否允许替换多个匹配（默认 False）
        highlight: 标色颜色: "yellow"/"green"/"red"/"pink"/None
        comment: 批注内容，None=不添加批注
    """
    # ── highlight 值校验 ──
    if highlight is not None and highlight not in ("yellow", "green", "red", "pink"):
        return ToolResult(
            content=f"不支持的 highlight 颜色: {highlight!r}。可选: yellow, green, red, pink",
            is_error=True,
        )

    logger.info("file_edit 开始: path=%s", path)

    # ── 文件存在性检查 ──
    target = Path(path)
    if not target.exists():
        return ToolResult(content=f"文件不存在: {path}", is_error=True)
    if not target.is_file():
        return ToolResult(content=f"不是文件: {path}", is_error=True)

    # ── 文件类型检测 ──
    ext = target.suffix.lower()

    # 拦截旧格式
    if ext == ".doc":
        return ToolResult(
            content="不支持旧版 .doc 格式。请转换为 .docx 后重试。",
            is_error=True,
        )
    if ext == ".xls":
        return ToolResult(
            content="不支持旧版 .xls 格式。请转换为 .xlsx 后重试。",
            is_error=True,
        )

    # 拦截 XLSX，引导使用 file_edit_table
    if ext in (".xlsx",):
        return ToolResult(
            content="XLSX 文件请使用 file_edit_table 工具编辑。file_edit 仅支持纯文本和 DOCX 格式。",
            is_error=True,
        )

    # ── 路由到对应编辑器 ──
    file_type = _detect_file_type(target)

    if file_type == "text":
        return await _edit_text(
            target, target_content, replacement_content,
            start_line, end_line, allow_multiple,
            highlight, comment,
        )
    elif file_type == "docx":
        return await _edit_docx(
            target, target_content, replacement_content,
            allow_multiple, highlight, comment,
        )
    else:
        return ToolResult(
            content=f"不支持的文件类型: {ext or '(无扩展名)'}。file_edit 支持纯文本、DOCX 格式。编辑 XLSX 请使用 file_edit_table。",
            is_error=True,
        )

