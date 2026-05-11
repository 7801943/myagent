"""
应用运行日志（区别于审计日志）。

使用 rich.logging.RichHandler 实现控制台彩色输出 + 行号显示。
文件日志保持纯文本格式，不含 ANSI 转义码。

2026-4-15 因为bug问题，退回到标准库
2026-5-11 引入 RichHandler，增加行号 + 彩色日志级别
"""
import logging
from logging import Formatter

from rich.logging import RichHandler

# 文件日志格式（纯文本，包含完整路径和行号）
_FILE_LOG_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s"

_initialized = False


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
    root.addHandler(console_handler)

    # ── 文件：纯文本（无 ANSI 转义码）──
    if log_file:
        from logging import FileHandler

        fh = FileHandler(log_file)
        fh.setFormatter(Formatter(_FILE_LOG_FORMAT))
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    """获取命名 Logger。"""
    return logging.getLogger(name)