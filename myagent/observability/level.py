"""
LogLevel：日志/审计级别枚举。
"""
from enum import IntEnum

class LogLevel(IntEnum):
    """日志级别（兼容标准库 logging）。"""
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50

    @classmethod
    def from_string(cls, level: str) -> "LogLevel":
        """从字符串解析日志级别。"""
        mapping = {
            "debug": cls.DEBUG,
            "info": cls.INFO,
            "warning": cls.WARNING,
            "warn": cls.WARNING,
            "error": cls.ERROR,
            "critical": cls.CRITICAL,
        }
        return mapping.get(level.lower(), cls.INFO)