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
    ".txt", ".md", ".py", ".json", ".yaml", ".yml", ".toml",
    ".xml", ".html", ".css", ".js", ".ts", ".jsx", ".tsx",
    ".sh", ".bash", ".zsh", ".fish",
    ".c", ".cpp", ".h", ".hpp", ".java", ".go", ".rs", ".rb",
    ".php", ".swift", ".kt", ".scala",
    ".sql", ".r", ".m",
    ".ini", ".cfg", ".conf", ".env", ".gitignore",
    ".dockerfile", ".makefile",
    ".log", ".csv", ".tsv",
    ".lua", ".vim",
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
    logger.debug("file_read: path=%s, file_type=%s, output_format=%s", path, file_type, output_format)

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
    logger.debug("file_write: path=%s, content_len=%d, append=%s", path, len(content), append)
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
        logger.debug("file_write 成功: %s %d 字符到 %s", action, len(content), target.resolve())
        return ToolResult(
            content=f"文件{action}成功: {target.resolve()} ({len(content)} 字符)",
            metadata={"path": str(target.resolve()),
                      "chars_written": len(content)},
        )
    except Exception as e:
        return ToolResult(content=f"写入文件失败: {e}", is_error=True)