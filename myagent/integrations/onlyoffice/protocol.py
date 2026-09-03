"""Validated protocol shared by the agent, browser host and ONLYOFFICE plugin."""
from __future__ import annotations

import time
from pathlib import PurePosixPath
from typing import Any


PROTOCOL_VERSION = "1.1"
PLUGIN_GUID = "asc.{D8E4A1C7-6F2B-4C3D-9A10-7E5B2F8C4D61}"
BRIDGE_REVISION = 2


class ErrorCode:
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    LINE_OUT_OF_RANGE = "LINE_OUT_OF_RANGE"
    NO_CAPABLE_CLIENT = "NO_CAPABLE_CLIENT"
    EDITOR_OPEN_TIMEOUT = "EDITOR_OPEN_TIMEOUT"
    PLUGIN_NOT_READY = "PLUGIN_NOT_READY"
    DOCUMENT_MISMATCH = "DOCUMENT_MISMATCH"
    READ_ONLY = "READ_ONLY"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_NOT_UNIQUE = "TARGET_NOT_UNIQUE"
    STALE_LINE_INDEX = "STALE_LINE_INDEX"
    NO_ACTIVE_SELECTION = "NO_ACTIVE_SELECTION"
    SELECTION_CHANGED = "SELECTION_CHANGED"
    STALE_SELECTION = "STALE_SELECTION"
    STALE_RANGE = "STALE_RANGE"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    SAVE_TIMEOUT = "SAVE_TIMEOUT"
    SAVE_FAILED = "SAVE_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"
    PROTOCOL_INVALID = "PROTOCOL_INVALID"
    NONCE_INVALID = "NONCE_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_EXISTS = "FILE_EXISTS"
    PLACEHOLDER_NOT_FOUND = "PLACEHOLDER_NOT_FOUND"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class OnlyOfficeProtocolError(ValueError):
    def __init__(self, code: str, message: str, *, metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.metadata = metadata or {}


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_document_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "document.path 必须是非空字符串")
    raw = value.replace("\\", "/").strip("/")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "document.path 不是安全的工作区路径")
    if path.suffix.lower() not in {".docx", ".xlsx", ".pdf"}:
        raise OnlyOfficeProtocolError(ErrorCode.UNSUPPORTED_FILE_TYPE, "实时自动化仅支持 DOCX、PDF 和 XLSX")
    return str(path)


def validate_command(payload: Any, *, at_ms: int | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "请求必须是对象")
    if payload.get("version") != PROTOCOL_VERSION:
        raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "协议版本不匹配")
    request_id = _string(payload.get("request_id"), "request_id", 128)
    editor_session_id = _string(payload.get("editor_session_id"), "editor_session_id", 128)
    document = payload.get("document")
    if not isinstance(document, dict):
        raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "document 必须是对象")
    path = normalize_document_path(document.get("path"))
    document_key = _string(document.get("document_key"), "document.document_key", 128)
    deadline_ms = payload.get("deadline_ms")
    if not isinstance(deadline_ms, int) or isinstance(deadline_ms, bool):
        raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "deadline_ms 必须是整数")
    if deadline_ms <= (now_ms() if at_ms is None else at_ms):
        raise OnlyOfficeProtocolError(ErrorCode.COMMAND_TIMEOUT, "命令已超时")
    command = payload.get("command")
    allowed = {
        "document_read", "document_navigate", "document_edit",
        "document_snapshot", "document_select", "document_get_selection",
        "document_insert_or_replace", "document_format", "document_add_comment",
        "spreadsheet_read", "spreadsheet_edit", "template_fill",
    }
    if command not in allowed:
        raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "命令不在白名单中")
    args = payload.get("args")
    if not isinstance(args, dict):
        raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "args 必须是对象")
    suffix = PurePosixPath(path).suffix.lower()
    docx_only = {
        "document_read", "document_navigate", "document_edit", "document_snapshot",
        "document_insert_or_replace", "document_format", "document_add_comment",
    }
    if command in docx_only and suffix != ".docx":
        if suffix == ".pdf" and command in {
            "document_insert_or_replace", "document_format", "document_add_comment",
        }:
            raise OnlyOfficeProtocolError(ErrorCode.READ_ONLY, "PDF 第一阶段不支持写入")
        raise OnlyOfficeProtocolError(ErrorCode.UNSUPPORTED_FILE_TYPE, "该 document 命令只支持 DOCX")
    if command in {"document_select", "document_get_selection"} and suffix not in {".docx", ".pdf"}:
        raise OnlyOfficeProtocolError(ErrorCode.UNSUPPORTED_FILE_TYPE, "选择命令只支持 DOCX 和 PDF")
    if command == "template_fill" and suffix not in {".docx", ".xlsx"}:
        raise OnlyOfficeProtocolError(ErrorCode.UNSUPPORTED_FILE_TYPE, "模板填充只支持 DOCX 和 XLSX")
    if command.startswith("spreadsheet_") and suffix != ".xlsx":
        raise OnlyOfficeProtocolError(ErrorCode.UNSUPPORTED_FILE_TYPE, "spreadsheet 命令只支持 XLSX")
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "editor_session_id": editor_session_id,
        "document": {"path": path, "document_key": document_key},
        "command": command,
        "args": args,
        "deadline_ms": deadline_ms,
    }


def success_response(request_id: str, result: Any = None) -> dict[str, Any]:
    return {"request_id": request_id, "ok": True, "result": result, "error": None}


def error_response(request_id: str, code: str, message: str, metadata: dict | None = None) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if metadata:
        error["metadata"] = metadata
    return {"request_id": request_id, "ok": False, "result": None, "error": error}


def _string(value: Any, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, f"{name} 必须是限长非空字符串")
    return value
