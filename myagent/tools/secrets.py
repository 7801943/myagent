"""
SecretManager：统一密钥凭据管理器。
职责：
1. 从环境变量 / 配置文件安全获取凭据
2. 向指定工具注入运行时凭据
3. 向 DataMasker 注册需要脱敏的密文值
"""
import os
from typing import Any

from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class SecretManager:
    """
    统一密钥凭据管理器。

    使用场景：
    - 工具需要 API Key 时，通过 SecretManager 获取，不直接从环境变量读取
    - 获取的值自动注册到 DataMasker 进行脱敏
    - 审计日志中不会出现明文密钥
    """

    def __init__(
        self,
        env_prefix: str = "MYAGENT_SECRET_",
        sensitive_fields: list[str] | None = None,
    ):
        self._env_prefix = env_prefix
        self._sensitive_fields = set(sensitive_fields or [
            "password", "api_key", "token", "secret",
            "access_key", "secret_key", "credential",
        ])
        self._masker = None   # 延迟注入 DataMasker
        self._resolved_secrets: dict[str, str] = {}

    def set_masker(self, masker: Any) -> None:
        """注入 DataMasker 实例，用于自动脱敏。"""
        self._masker = masker

    def resolve(self, key: str) -> str | None:
        """
        解析密钥值。
        查找顺序：缓存 -> 环境变量（带前缀）-> 环境变量（原名）
        """
        if key in self._resolved_secrets:
            return self._resolved_secrets[key]

        # 尝试带前缀的环境变量
        prefixed_key = f"{self._env_prefix}{key.upper()}"
        value = os.environ.get(prefixed_key)

        # 回退到不带前缀的环境变量
        if value is None:
            value = os.environ.get(key.upper())
            if value is None:
                value = os.environ.get(key)

        if value:
            self._resolved_secrets[key] = value
            # 自动注册脱敏
            if self._masker:
                self._masker.add_redact_pattern(value)
            logger.debug(f"Secret resolved: {key} (from env)")

        return value

    def resolve_tool_credentials(self, tool_name: str) -> dict[str, str]:
        """
        为指定工具解析所需凭据。
        凭据以 MYAGENT_SECRET_{TOOL_NAME}_{KEY} 格式存放在环境变量中。
        """
        prefix = f"{self._env_prefix}{tool_name.upper()}_"
        credentials = {}
        for key, value in os.environ.items():
            if key.startswith(prefix):
                cred_name = key[len(prefix):].lower()
                credentials[cred_name] = value
                # 自动脱敏
                if self._masker:
                    self._masker.add_redact_pattern(value)
        return credentials

    def redact_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """
        脱敏工具参数中的敏感字段。
        用于审计日志和 UI 显示。
        """
        redacted = dict(args)
        for key in args:
            if any(field in key.lower() for field in self._sensitive_fields):
                redacted[key] = "[REDACTED]"
        return redacted

    def inject_secrets(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """
        向工具参数中注入必要的凭据。
        凭据字段以 _credentials 键标识。
        """
        credentials = self.resolve_tool_credentials(tool_name)
        if credentials:
            enriched = dict(args)
            enriched["_credentials"] = credentials
            return enriched
        return args