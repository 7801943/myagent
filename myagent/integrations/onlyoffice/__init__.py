"""ONLYOFFICE realtime automation integration."""

from .protocol import ErrorCode, OnlyOfficeProtocolError
from .runtime import OnlyOfficeSessionAutomation

__all__ = ["ErrorCode", "OnlyOfficeProtocolError", "OnlyOfficeSessionAutomation"]
