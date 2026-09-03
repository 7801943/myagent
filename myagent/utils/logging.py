"""
应用运行日志（区别于审计日志）。

使用 rich.logging.RichHandler 实现控制台彩色输出 + 行号显示。
文件日志保持纯文本格式，不含 ANSI 转义码。

2026-4-15 因为bug问题，退回到标准库
2026-5-11 引入 RichHandler，增加行号 + 彩色日志级别
"""
import logging
import re
from logging import Formatter

from rich.logging import RichHandler

# 文件日志格式（纯文本，包含完整路径和行号）
_FILE_LOG_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s"

_initialized = False

_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|jwt|access_token)=)[^&\s\"']+"
)
_SENSITIVE_JSON_RE = re.compile(
    r'''(?i)(["'](?:token|jwt|access_token)["']\s*:\s*["'])[^"']+'''
)


def redact_sensitive_text(value: str) -> str:
    """Redact credentials commonly embedded in logged URLs or JSON fragments."""
    text = str(value)
    text = _SENSITIVE_QUERY_RE.sub(r"\1<redacted>", text)
    return _SENSITIVE_JSON_RE.sub(r"\1<redacted>", text)


class SensitiveDataFilter(logging.Filter):
    """Logging filter that preserves record structure while redacting credentials."""

    def filter(self, record: logging.LogRecord) -> bool:
        # AccessFormatter derives client_addr/request_line/status_code from the
        # original five positional arguments, so keep that structure intact.
        if record.name == "uvicorn.access":
            if isinstance(record.msg, str):
                record.msg = redact_sensitive_text(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_log_arg(value) for value in record.args)
            elif isinstance(record.args, dict):
                record.args = {key: _redact_log_arg(value) for key, value in record.args.items()}
            return True

        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        redacted = redact_sensitive_text(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def _redact_log_arg(value):
    if isinstance(value, str):
        return redact_sensitive_text(value)
    rendered = str(value)
    redacted = redact_sensitive_text(rendered)
    return redacted if redacted != rendered else value


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """初始化全局日志配置。仅在首次调用时生效。

    控制台使用 RichHandler（彩色日志级别 + 行号），
    文件输出使用标准 Formatter（纯文本 + 行号）。
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # ── 控制台：RichHandler（彩色 + 行号）──
    console_handler = RichHandler(
        level=numeric_level,
        rich_tracebacks=True,          # 异常追踪也用 Rich 渲染
        show_path=False,               # 我们在 format 中自行控制路径显示
        show_time=True,                # 显示时间戳
        omit_repeated_times=False,     # 不省略重复时间
        markup=True,                   # 支持 Rich markup
    )
    # RichHandler 自身的 format string 只控制 message 之前的部分
    console_handler.setFormatter(
        Formatter(fmt="%(name)s | %(filename)s:%(lineno)d | %(message)s")
    )
    console_handler.addFilter(SensitiveDataFilter())
    root.addHandler(console_handler)

    # ── 文件：纯文本（无 ANSI 转义码）──
    if log_file:
        from logging import FileHandler

        fh = FileHandler(log_file)
        fh.setFormatter(Formatter(_FILE_LOG_FORMAT))
        fh.addFilter(SensitiveDataFilter())
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    """获取命名 Logger。"""
    return logging.getLogger(name)
