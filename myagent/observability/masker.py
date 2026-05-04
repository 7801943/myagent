"""
DataMasker：审计日志脱敏器。
在写入审计日志前，对敏感字段进行脱敏处理。
"""
import re
from typing import Any

# 常见敏感字段名模式
_SENSITIVE_KEYS = {
    "api_key", "secret", "password", "token", "authorization",
    "credit_card", "ssn", "phone", "email",
}

# 脱敏规则
_API_KEY_PATTERN = re.compile(r"(sk-|key-|token-)([a-zA-Z0-9]{4})[a-zA-Z0-9]*")
_EMAIL_PATTERN = re.compile(r"([a-zA-Z0-9._%+-])(?:[a-zA-Z0-9._%+-]*)@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")

class DataMasker:
    """数据脱敏器。"""

    def __init__(self, sensitive_keys: set[str] | None = None):
        self._sensitive_keys = sensitive_keys or _SENSITIVE_KEYS
        self._custom_patterns: list[re.Pattern] = []

    def mask_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """对字典中的敏感字段进行脱敏。"""
        masked = {}
        for key, value in data.items():
            if self._is_sensitive_key(key):
                masked[key] = self._mask_value(value)
            elif isinstance(value, dict):
                masked[key] = self.mask_dict(value)
            elif isinstance(value, str):
                masked[key] = self._mask_string(value)
            else:
                masked[key] = value
        return masked

    def _is_sensitive_key(self, key: str) -> bool:
        return key.lower() in self._sensitive_keys

    def _mask_value(self, value: Any) -> str:
        if isinstance(value, str):
            if len(value) <= 8:
                return "***"
            return value[:4] + "***" + value[-4:]
        return "***"

    def _mask_string(self, text: str) -> str:
        """对字符串中的敏感模式进行脱敏。"""
        # 脱敏 API Key
        text = _API_KEY_PATTERN.sub(r"\1****", text)
        # 脱敏邮箱
        text = _EMAIL_PATTERN.sub(r"\1***@\2", text)
        # 脱敏自定义模式
        for pattern in self._custom_patterns:
            text = pattern.sub("***", text)
        return text

    def add_redact_pattern(self, value: str) -> None:
        """注册一个需要被脱敏的原文值。"""
        if value and len(value) >= 8:
            escaped = re.escape(value)
            self._custom_patterns.append(re.compile(escaped))