"""
应用运行日志（区别于审计日志）。
使用 picologging 替代 stdlib logging，API 完全兼容，性能提升 4-10x。

2026-4-15 因为bug问题，退回到标准库
"""
import logging
from logging import StreamHandler, Formatter

_LOG_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
_initialized = False

def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """初始化全局日志配置。仅在首次调用时生效。"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console_handler = StreamHandler()
    console_handler.setFormatter(Formatter(_LOG_FORMAT))
    root.addHandler(console_handler)

    if log_file:
        from logging import FileHandler
        fh = FileHandler(log_file)
        fh.setFormatter(Formatter(_LOG_FORMAT))
        root.addHandler(fh)

def get_logger(name: str) -> logging.Logger:
    """获取命名 Logger。"""
    return logging.getLogger(name)