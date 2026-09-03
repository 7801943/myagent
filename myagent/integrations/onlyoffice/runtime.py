"""Session-scoped realtime ONLYOFFICE automation and agent tools."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from myagent.tools.api import ToolMeta, ToolResult
from myagent.utils.logging import get_logger

from .protocol import (
    BRIDGE_REVISION,
    ErrorCode,
    OnlyOfficeProtocolError,
    PROTOCOL_VERSION,
    now_ms,
    validate_command,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class EditorState:
    path: str
    document_key: str
    editor_session_id: str
    ready: bool = False
    active: bool = False
    writable: bool = False
    error: str = ""


@dataclass(slots=True)
class BrowserClient:
    client_id: str
    sender: Callable[[dict[str, Any]], Awaitable[None]]
    visible: bool = True
    last_active_ms: int = field(default_factory=now_ms)
    last_seen_ms: int = field(default_factory=now_ms)
    bridge_revision: int = BRIDGE_REVISION
    editors: dict[str, EditorState] = field(default_factory=dict)
    state_signature: tuple = field(default_factory=tuple)


@dataclass(slots=True)
class PendingCommand:
    client_id: str
    future: asyncio.Future


@dataclass(slots=True)
class DocumentSnapshot:
    path: str
    file_hash: str
    snapshot_id: str
    client_id: str
    editor_session_id: str
    document_key: str
    lines: list[dict[str, Any]]
    created_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class HiddenSelection:
    path: str
    selected_text: str
    file_hash: str
    snapshot_id: str
    client_id: str
    editor_session_id: str
    document_key: str
    line_no: int | None = None
    page: int | None = None
    created_at: float = field(default_factory=time.monotonic)


class OnlyOfficeSessionAutomation:
    """Routes validated commands to a capable browser belonging to one Session."""

    def __init__(self, session: Any, document_service: Any, config: dict[str, Any] | None = None):
        self.session = session
        self.document_service = document_service
        raw = config or {}
        self.enabled = bool(raw.get("enabled", False))
        self.open_timeout = float(raw.get("open_timeout_seconds", 30))
        self.command_timeout = float(raw.get("command_timeout_seconds", 15))
        self.save_timeout = float(raw.get("save_timeout_seconds", 30))
        self.client_ttl_ms = int(float(raw.get("client_ttl_seconds", 45)) * 1000)
        self.max_text_chars = int(raw.get("max_text_chars", 100_000))
        self.max_html_chars = int(raw.get("max_html_chars", 200_000))
        self.max_cells = int(raw.get("max_cells", 10_000))
        self.selection_ttl = float(raw.get("selection_ttl_seconds", 120))
        self._clients: dict[str, BrowserClient] = {}
        self._pending: dict[str, PendingCommand] = {}
        self._state_changed = asyncio.Condition()
        self._preferred_client_id = ""
        self._snapshots: dict[tuple[str, str], DocumentSnapshot] = {}
        self._latest_snapshots: dict[str, DocumentSnapshot] = {}
        self._selections: dict[tuple[str, str], HiddenSelection] = {}

    @property
    def has_clients(self) -> bool:
        return bool(self._capable_clients())

    def register_client(self, client_id: str, sender: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        current = self._clients.get(client_id)
        if current:
            current.sender = sender
            current.last_seen_ms = now_ms()
        else:
            self._clients[client_id] = BrowserClient(client_id=client_id, sender=sender)
        logger.info(
            "OnlyOffice automation client registered: session=%s client=%s clients=%s enabled=%s",
            self.session.id,
            _short_id(client_id),
            len(self._clients),
            self.enabled,
        )

    def touch_client(self, client_id: str) -> None:
        """Refresh the server-observed liveness of an attached WebSocket client."""
        client = self._clients.get(client_id)
        if client:
            client.last_seen_ms = now_ms()

    def unregister_client(self, client_id: str) -> None:
        self._clients.pop(client_id, None)
        self._snapshots = {key: value for key, value in self._snapshots.items() if key[0] != client_id}
        self._latest_snapshots = {
            key: value for key, value in self._latest_snapshots.items() if value.client_id != client_id
        }
        self._selections = {key: value for key, value in self._selections.items() if key[0] != client_id}
        logger.info(
            "OnlyOffice automation client unregistered: session=%s client=%s clients=%s",
            self.session.id,
            _short_id(client_id),
            len(self._clients),
        )
        for request_id, pending in list(self._pending.items()):
            if pending.client_id == client_id and not pending.future.done():
                pending.future.set_exception(
                    OnlyOfficeProtocolError(ErrorCode.NO_CAPABLE_CLIENT, "执行命令的浏览器客户端已断开")
                )
                self._pending.pop(request_id, None)

    async def update_client_state(self, client_id: str, payload: dict[str, Any]) -> None:
        client = self._clients.get(client_id)
        if not client:
            raise OnlyOfficeProtocolError(ErrorCode.NO_CAPABLE_CLIENT, "未知的浏览器客户端")
        client.visible = bool(payload.get("visible", True))
        client.last_active_ms = int(payload.get("last_active_ms") or now_ms())
        client.last_seen_ms = now_ms()
        client.bridge_revision = int(payload.get("bridge_revision") or 0)
        editors: dict[str, EditorState] = {}
        for item in payload.get("editors") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            key = str(item.get("document_key") or "")
            editor_session_id = str(item.get("editor_session_id") or "")
            if not path or not key or not editor_session_id:
                continue
            editors[path] = EditorState(
                path=path,
                document_key=key,
                editor_session_id=editor_session_id,
                ready=bool(item.get("ready", False)),
                active=bool(item.get("active", False)),
                writable=bool(item.get("writable", False)),
                error=str(item.get("error") or "")[:1000],
            )
        client.editors = editors
        state_signature = tuple(sorted(
            (path, editor.editor_session_id, editor.document_key, editor.ready, editor.active, editor.writable, editor.error)
            for path, editor in editors.items()
        ))
        if state_signature != client.state_signature:
            client.state_signature = state_signature
            logger.info(
                "OnlyOffice client state changed: session=%s client=%s visible=%s bridge_revision=%s editors=%s",
                self.session.id,
                _short_id(client_id),
                client.visible,
                client.bridge_revision,
                [_editor_log_state(editor) for editor in editors.values()],
            )
        async with self._state_changed:
            self._state_changed.notify_all()

    def resolve_response(self, client_id: str, response: dict[str, Any]) -> None:
        request_id = str(response.get("request_id") or "")
        pending = self._pending.get(request_id)
        if not pending or pending.client_id != client_id or pending.future.done():
            logger.warning(
                "OnlyOffice bridge response ignored: session=%s client=%s request=%s pending=%s",
                self.session.id,
                _short_id(client_id),
                _short_id(request_id),
                bool(pending),
            )
            return
        if not isinstance(response.get("ok"), bool):
            pending.future.set_exception(
                OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "浏览器返回的响应格式无效")
            )
            return
        pending.future.set_result(response)
        logger.info(
            "OnlyOffice bridge response accepted: session=%s client=%s request=%s ok=%s",
            self.session.id,
            _short_id(client_id),
            _short_id(request_id),
            response.get("ok"),
        )

    def log_client_diagnostic(self, client_id: str, payload: dict[str, Any]) -> None:
        phase = _log_text(payload.get("phase"), 80)
        message = _log_text(payload.get("message"), 500)
        path = _log_text(payload.get("path"), 1000)
        request_id = _short_id(str(payload.get("request_id") or ""))
        logger.info(
            "OnlyOffice browser diagnostic: session=%s client=%s phase=%s path=%s request=%s message=%s",
            self.session.id,
            _short_id(client_id),
            phase,
            path,
            request_id,
            message,
        )

    def set_preferred_client(self, client_id: str) -> None:
        self._preferred_client_id = client_id if client_id in self._clients else ""

    def clear_preferred_client(self) -> None:
        self._preferred_client_id = ""

    def register_tools(self) -> None:
        if not self.enabled:
            return
        manager = self.session.harness.tool_manager
        meta = ToolMeta(source="runtime", category="document", permission="standard", timeout=90.0)
        for name, description, schema, handler in _tool_definitions(self):
            manager.register_inline(
                name=name,
                description=description,
                parameters_schema=schema,
                handler=handler,
                meta=meta,
            )

    async def dispatch(
        self,
        path: str,
        command: str,
        args: dict[str, Any],
        *,
        write: bool = False,
        preferred_client_id: str = "",
        with_context: bool = False,
    ) -> Any:
        virtual_path, _real_path = self._resolve_path(path, write=write)
        logger.info(
            "OnlyOffice command starting: session=%s command=%s path=%s write=%s clients=%s",
            self.session.id,
            command,
            virtual_path,
            write,
            len(self._capable_clients()),
        )
        client, editor = await self._ensure_editor(virtual_path, preferred_client_id=preferred_client_id)
        if write and not editor.writable:
            raise OnlyOfficeProtocolError(ErrorCode.READ_ONLY, "目标编辑器当前为只读模式")
        request_id = secrets.token_hex(16)
        request = validate_command({
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "editor_session_id": editor.editor_session_id,
            "document": {"path": virtual_path, "document_key": editor.document_key},
            "command": command,
            "args": args,
            "deadline_ms": now_ms() + int(self.command_timeout * 1000),
        })
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = PendingCommand(client_id=client.client_id, future=future)
        try:
            logger.info(
                "OnlyOffice bridge command sending: session=%s client=%s request=%s command=%s path=%s editor=%s key=%s",
                self.session.id,
                _short_id(client.client_id),
                _short_id(request_id),
                command,
                virtual_path,
                _short_id(editor.editor_session_id),
                _short_id(editor.document_key),
            )
            await client.sender({"type": "onlyoffice_bridge_command", "request": request})
            response = await asyncio.wait_for(future, timeout=self.command_timeout)
        except asyncio.TimeoutError as exc:
            logger.warning(
                "OnlyOffice bridge command timed out: session=%s client=%s request=%s command=%s path=%s timeout=%ss",
                self.session.id,
                _short_id(client.client_id),
                _short_id(request_id),
                command,
                virtual_path,
                self.command_timeout,
            )
            raise OnlyOfficeProtocolError(ErrorCode.COMMAND_TIMEOUT, "等待 OnlyOffice 插件响应超时") from exc
        finally:
            self._pending.pop(request_id, None)
        if not response.get("ok"):
            error = response.get("error") or {}
            raise OnlyOfficeProtocolError(
                str(error.get("code") or ErrorCode.INTERNAL_ERROR),
                str(error.get("message") or "OnlyOffice 插件执行失败"),
                metadata=error.get("metadata") if isinstance(error.get("metadata"), dict) else None,
            )
        result = response.get("result")
        logger.info(
            "OnlyOffice command completed: session=%s request=%s command=%s path=%s",
            self.session.id,
            _short_id(request_id),
            command,
            virtual_path,
        )
        clean_result = result if isinstance(result, dict) else {"value": result}
        if with_context:
            return clean_result, client, editor, virtual_path, _real_path
        return clean_result

    async def _ensure_editor(
        self,
        virtual_path: str,
        *,
        preferred_client_id: str = "",
    ) -> tuple[BrowserClient, EditorState]:
        selected = self._choose_client(virtual_path, require_editor=True, preferred=preferred_client_id)
        if selected:
            logger.info(
                "OnlyOffice ready editor reused: session=%s client=%s path=%s editor=%s key=%s",
                self.session.id,
                _short_id(selected[0].client_id),
                virtual_path,
                _short_id(selected[1].editor_session_id),
                _short_id(selected[1].document_key),
            )
            return selected
        client = self._choose_client(virtual_path, require_editor=False, preferred=preferred_client_id)
        if not client:
            logger.warning(
                "OnlyOffice no capable client: session=%s path=%s registered_clients=%s preferred=%s",
                self.session.id,
                virtual_path,
                len(self._clients),
                _short_id(self._preferred_client_id),
            )
            raise OnlyOfficeProtocolError(ErrorCode.NO_CAPABLE_CLIENT, "当前 Session 没有可用的浏览器客户端")

        # Pull a fresh snapshot before opening. This recovers editors that were already
        # open when the WebSocket reconnected or the user switched sessions.
        async with self._state_changed:
            logger.info(
                "OnlyOffice requesting fresh client state: session=%s client=%s path=%s",
                self.session.id,
                _short_id(client.client_id),
                virtual_path,
            )
            await client.sender({"type": "onlyoffice_state_request"})
            try:
                await asyncio.wait_for(self._state_changed.wait(), timeout=min(1.0, self.open_timeout))
            except asyncio.TimeoutError:
                pass
        selected = self._choose_client(virtual_path, require_editor=True, preferred=client.client_id)
        if selected:
            logger.info(
                "OnlyOffice ready editor found after state refresh: session=%s client=%s path=%s editor=%s",
                self.session.id,
                _short_id(selected[0].client_id),
                virtual_path,
                _short_id(selected[1].editor_session_id),
            )
            return selected

        if self.session.workspace:
            await self.session.workspace.update("agent", "open_file", {"path": virtual_path})
        await client.sender({
            "type": "onlyoffice_open_request",
            "path": virtual_path,
            "mode": "edit",
            "deadline_ms": now_ms() + int(self.open_timeout * 1000),
        })
        logger.info(
            "OnlyOffice open request sent: session=%s client=%s path=%s timeout=%ss",
            self.session.id,
            _short_id(client.client_id),
            virtual_path,
            self.open_timeout,
        )
        deadline = time.monotonic() + self.open_timeout
        while True:
            selected = self._choose_client(virtual_path, require_editor=True, preferred=client.client_id)
            if selected:
                logger.info(
                    "OnlyOffice editor became ready: session=%s client=%s path=%s editor=%s key=%s",
                    self.session.id,
                    _short_id(selected[0].client_id),
                    virtual_path,
                    _short_id(selected[1].editor_session_id),
                    _short_id(selected[1].document_key),
                )
                return selected
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._editor_open_timeout(virtual_path, client)
            async with self._state_changed:
                try:
                    await asyncio.wait_for(self._state_changed.wait(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise self._editor_open_timeout(virtual_path, client) from exc

    def _editor_open_timeout(self, path: str, client: BrowserClient) -> OnlyOfficeProtocolError:
        editor = client.editors.get(path)
        metadata: dict[str, Any] = {
            "client_id": client.client_id,
            "path": path,
            "editor_reported": editor is not None,
        }
        if editor:
            metadata.update({
                "document_key": editor.document_key,
                "editor_session_id": editor.editor_session_id,
                "plugin_ready": editor.ready,
            })
            if editor.error:
                metadata["plugin_error"] = editor.error
        logger.warning(
            "OnlyOffice editor open timed out: session=%s client=%s path=%s metadata=%s",
            self.session.id,
            _short_id(client.client_id),
            path,
            metadata,
        )
        return OnlyOfficeProtocolError(
            ErrorCode.EDITOR_OPEN_TIMEOUT,
            "等待目标文档编辑器就绪超时",
            metadata=metadata,
        )

    def _choose_client(
        self,
        path: str,
        *,
        require_editor: bool,
        preferred: str = "",
    ) -> tuple[BrowserClient, EditorState] | BrowserClient | None:
        clients = self._capable_clients()
        preferred_id = preferred or self._preferred_client_id
        if preferred_id and any(item.client_id == preferred_id for item in clients):
            clients = [item for item in clients if item.client_id == preferred_id]
        clients.sort(key=lambda item: (
            1 if item.client_id == preferred_id else 0,
            1 if path in item.editors and item.editors[path].ready else 0,
            1 if path in item.editors and item.editors[path].active else 0,
            1 if item.visible else 0,
            item.last_active_ms,
        ), reverse=True)
        if require_editor:
            for client in clients:
                editor = client.editors.get(path)
                if editor and editor.ready:
                    return client, editor
            return None
        return clients[0] if clients else None

    def _capable_clients(self) -> list[BrowserClient]:
        cutoff = now_ms() - self.client_ttl_ms
        return [
            client for client in self._clients.values()
            if client.bridge_revision >= BRIDGE_REVISION and client.last_seen_ms >= cutoff
        ]

    def _resolve_path(self, path: str, *, write: bool) -> tuple[str, Path]:
        resolver = getattr(self.session.workspace, "resolver", None) if self.session.workspace else None
        if not resolver:
            root = Path(self.session.workspace.root_path).resolve()
            real = (root / str(path).strip("/")).resolve()
            if root not in real.parents and real != root:
                raise OnlyOfficeProtocolError(ErrorCode.PERMISSION_DENIED, "路径不能越出工作区")
            if not real.exists():
                raise OnlyOfficeProtocolError(ErrorCode.TARGET_NOT_FOUND, f"文件不存在: {path}")
            return str(path).replace("\\", "/").strip("/"), real
        try:
            resolved = resolver.normalize_for_tool(path, operation="write" if write else "read")
        except FileNotFoundError as exc:
            raise OnlyOfficeProtocolError(ErrorCode.TARGET_NOT_FOUND, f"文件不存在: {path}") from exc
        except (PermissionError, ValueError) as exc:
            raise OnlyOfficeProtocolError(ErrorCode.PERMISSION_DENIED, str(exc)) from exc
        if resolved.real_path.suffix.lower() not in {".docx", ".pdf", ".xlsx"}:
            raise OnlyOfficeProtocolError(ErrorCode.UNSUPPORTED_FILE_TYPE, "实时自动化仅支持 DOCX、PDF 和 XLSX")
        return resolved.virtual_path, resolved.real_path

    async def tool_create_from_template(
        self,
        template_path: str,
        target_path: str,
        variables: dict[str, Any] | None = None,
    ) -> ToolResult:
        if not self.has_clients:
            return _error_result(ErrorCode.NO_CAPABLE_CLIENT, "当前 Session 没有可用的浏览器客户端")
        try:
            template_virtual, template_real = self._resolve_path(template_path, write=False)
            resolver = getattr(self.session.workspace, "resolver", None) if self.session.workspace else None
            if resolver:
                target = resolver.normalize_for_tool(target_path, operation="write")
                target_virtual, target_real = target.virtual_path, target.real_path
            else:
                root = Path(self.session.workspace.root_path).resolve()
                target_real = (root / str(target_path).strip("/")).resolve()
                if root not in target_real.parents:
                    raise OnlyOfficeProtocolError(ErrorCode.PERMISSION_DENIED, "目标路径不能越出工作区")
                target_virtual = str(target_path).replace("\\", "/").strip("/")
            if template_real.suffix.lower() not in {".docx", ".xlsx"} or template_real.suffix.lower() != target_real.suffix.lower():
                raise OnlyOfficeProtocolError(ErrorCode.UNSUPPORTED_FILE_TYPE, "模板和目标必须是相同的 DOCX 或 XLSX 格式")
            if target_real.exists():
                raise OnlyOfficeProtocolError(ErrorCode.FILE_EXISTS, f"目标文件已存在: {target_path}")
            target_real.parent.mkdir(parents=True, exist_ok=True)
            tmp = target_real.with_name(f".{target_real.name}.{secrets.token_hex(8)}.tmp")
            try:
                shutil.copy2(template_real, tmp)
                os.replace(tmp, target_real)
            finally:
                if tmp.exists():
                    tmp.unlink()
            if self.session.workspace:
                await self.session.workspace.update("agent", "files_changed", {"changed_paths": [target_virtual]})
                await self.session.workspace.update("agent", "open_file", {"path": target_virtual})
            result: dict[str, Any] = {
                "template_path": template_virtual,
                "path": target_virtual,
                "created": True,
            }
            if variables:
                clean_variables = _validate_variables(variables)
                fill = await self.dispatch(target_virtual, "template_fill", {"variables": clean_variables}, write=True)
                editor = self._choose_client(target_virtual, require_editor=True)
                assert isinstance(editor, tuple)
                await self.document_service.force_save_and_wait(
                    editor[1].document_key,
                    request_id=secrets.token_hex(16),
                    timeout=self.save_timeout,
                )
                result["fill"] = fill
            return ToolResult(
                content=json.dumps(result, ensure_ascii=False),
                metadata={"path": str(target_real), "workspace_handled": True},
            )
        except OnlyOfficeProtocolError as exc:
            return _error_result(exc.code, exc.message, exc.metadata)
        except Exception as exc:
            logger.exception("OnlyOffice template creation failed")
            return _error_result(ErrorCode.INTERNAL_ERROR, str(exc))

    async def _refresh_snapshot(self, path: str, *, preferred_client_id: str = "") -> tuple[DocumentSnapshot, Path]:
        virtual_path, real_path = self._resolve_path(path, write=False)
        file_hash = _sha256_file(real_path)
        if real_path.suffix.lower() == ".pdf":
            lines = _extract_pdf_logical_lines(real_path)
            snapshot = DocumentSnapshot(
                path=virtual_path,
                file_hash=file_hash,
                snapshot_id=file_hash,
                client_id=preferred_client_id,
                editor_session_id="",
                document_key="",
                lines=lines,
            )
        else:
            result, client, editor, virtual_path, real_path = await self.dispatch(
                virtual_path,
                "document_snapshot",
                {},
                write=False,
                preferred_client_id=preferred_client_id,
                with_context=True,
            )
            lines = result.get("lines")
            snapshot_id = str(result.get("snapshot_id") or "")
            if not isinstance(lines, list) or not snapshot_id:
                raise OnlyOfficeProtocolError(ErrorCode.VERIFY_FAILED, "OnlyOffice 未返回有效的逻辑行快照")
            snapshot = DocumentSnapshot(
                path=virtual_path,
                file_hash=file_hash,
                snapshot_id=snapshot_id,
                client_id=client.client_id,
                editor_session_id=editor.editor_session_id,
                document_key=editor.document_key,
                lines=_normalize_snapshot_lines(lines),
            )
            self._snapshots[(client.client_id, virtual_path)] = snapshot
        self._latest_snapshots[virtual_path] = snapshot
        return snapshot, real_path

    def _current_snapshot(self, virtual_path: str) -> DocumentSnapshot | None:
        if self._preferred_client_id:
            selected = self._snapshots.get((self._preferred_client_id, virtual_path))
            if selected:
                return selected
        return self._latest_snapshots.get(virtual_path)

    def _assert_file_unchanged(self, snapshot: DocumentSnapshot, real_path: Path) -> None:
        if snapshot.file_hash != _sha256_file(real_path):
            raise OnlyOfficeProtocolError(ErrorCode.STALE_LINE_INDEX, "文档已变化，请重新读取或搜索后再定位")

    async def tool_document_read(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        try:
            snapshot, real_path = await self._refresh_snapshot(path)
            total = len(snapshot.lines)
            start, end = _validate_line_window(start_line, end_line, total)
            body = _render_numbered_lines(snapshot.lines[start - 1:end], start)
            content = (
                f"文档：{snapshot.path}\n"
                f"版本：sha256:{snapshot.file_hash}\n"
                f"总行数：{total}\n\n{body}"
            )
            return ToolResult(
                content=content,
                metadata={"path": str(real_path), "workspace_handled": True, "version": snapshot.file_hash},
            )
        except OnlyOfficeProtocolError as exc:
            return _error_result(exc.code, exc.message, exc.metadata)
        except Exception as exc:
            logger.exception("OnlyOffice logical read failed")
            return _error_result(ErrorCode.INTERNAL_ERROR, str(exc))

    async def tool_search(
        self,
        path: str,
        keyword: str,
        context_lines: int = 2,
        match_case: bool = False,
    ) -> ToolResult:
        try:
            if not isinstance(keyword, str) or not keyword:
                raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "keyword 必须是非空字符串")
            if not isinstance(context_lines, int) or isinstance(context_lines, bool) or not 0 <= context_lines <= 20:
                raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "context_lines 必须是 0 到 20 的整数")
            snapshot, real_path = await self._refresh_snapshot(path)
            needle = keyword if match_case else keyword.casefold()
            matched_indexes = [
                index for index, line in enumerate(snapshot.lines)
                if needle in (line["text"] if match_case else line["text"].casefold())
            ]
            visible_indexes = matched_indexes[:50]
            matches = []
            for index in visible_indexes:
                lower = max(0, index - context_lines)
                upper = min(len(snapshot.lines), index + context_lines + 1)
                matches.append({
                    "line_no": index + 1,
                    "page": snapshot.lines[index].get("page"),
                    "line_text": snapshot.lines[index]["text"],
                    "paragraph_text": snapshot.lines[index].get("paragraph_text", snapshot.lines[index]["text"]),
                    "context": _render_numbered_lines(snapshot.lines[lower:upper], lower + 1),
                })
            payload = {
                "success": True,
                "match_count": len(matched_indexes),
                "matches": matches,
                "truncated": len(matched_indexes) > len(visible_indexes),
            }
            return ToolResult(
                content=json.dumps(payload, ensure_ascii=False),
                metadata={"path": str(real_path), "workspace_handled": True, "version": snapshot.file_hash},
            )
        except OnlyOfficeProtocolError as exc:
            return _error_result(exc.code, exc.message, exc.metadata)
        except Exception as exc:
            logger.exception("OnlyOffice search failed")
            return _error_result(ErrorCode.INTERNAL_ERROR, str(exc))

    async def tool_navigate_and_select(
        self,
        path: str,
        line_no: int | None = None,
        select_text: str | None = None,
        anchor: str | None = None,
        page: int | None = None,
    ) -> ToolResult:
        try:
            result, _selection, real_path = await self._navigate_and_select(
                path,
                line_no=line_no,
                select_text=select_text,
                anchor=anchor,
                page=page,
            )
            return ToolResult(
                content=json.dumps(result, ensure_ascii=False),
                metadata={"path": str(real_path), "workspace_handled": True},
            )
        except OnlyOfficeProtocolError as exc:
            return _error_result(exc.code, exc.message, exc.metadata)
        except Exception as exc:
            logger.exception("OnlyOffice navigate/select failed")
            return _error_result(ErrorCode.INTERNAL_ERROR, str(exc))

    async def _navigate_and_select(
        self,
        path: str,
        *,
        line_no: int | None,
        select_text: str | None,
        anchor: str | None,
        page: int | None,
    ) -> tuple[dict[str, Any], HiddenSelection | None, Path]:
        _validate_selection_target(line_no=line_no, select_text=select_text, anchor=anchor, page=page)
        virtual_path, real_path = self._resolve_path(path, write=False)
        snapshot = self._current_snapshot(virtual_path)
        if snapshot is None:
            snapshot, real_path = await self._refresh_snapshot(virtual_path)
        self._assert_file_unchanged(snapshot, real_path)
        if line_no is not None and not 1 <= line_no <= len(snapshot.lines):
            raise OnlyOfficeProtocolError(ErrorCode.LINE_OUT_OF_RANGE, f"行号必须在 1 到 {len(snapshot.lines)} 之间")
        suffix = real_path.suffix.lower()
        args: dict[str, Any] = {
            "line_no": line_no,
            "select_text": select_text,
            "anchor": anchor,
            "page": page,
        }
        preferred_client_id = snapshot.client_id
        if suffix == ".docx":
            args["snapshot_id"] = snapshot.snapshot_id
        elif line_no is not None:
            line = snapshot.lines[line_no - 1]
            line_text = line["text"]
            if select_text is not None and line_text.count(select_text) != 1:
                code = ErrorCode.TARGET_NOT_FOUND if select_text not in line_text else ErrorCode.TARGET_NOT_UNIQUE
                raise OnlyOfficeProtocolError(code, "指定行中的 select_text 必须精确命中一次")
            args["page"] = line.get("page")
            args["line_no"] = None
        result, client, editor, virtual_path, real_path = await self.dispatch(
            virtual_path,
            "document_select",
            args,
            write=False,
            preferred_client_id=preferred_client_id,
            with_context=True,
        )
        if suffix == ".docx" and (
            editor.editor_session_id != snapshot.editor_session_id or editor.document_key != snapshot.document_key
        ):
            raise OnlyOfficeProtocolError(ErrorCode.STALE_LINE_INDEX, "编辑器实例已变化，请重新读取文档")
        selected_text = str(result.get("selected_text") or "")
        response = {"success": True, "selected_text": selected_text}
        key = (client.client_id, virtual_path)
        if selected_text:
            selection = HiddenSelection(
                path=virtual_path,
                selected_text=selected_text,
                file_hash=snapshot.file_hash,
                snapshot_id=snapshot.snapshot_id,
                client_id=client.client_id,
                editor_session_id=editor.editor_session_id,
                document_key=editor.document_key,
                line_no=line_no,
                page=page or result.get("page"),
            )
            self._selections[key] = selection
        else:
            self._selections.pop(key, None)
            selection = None
        return response, selection, real_path

    def _find_selection(self, virtual_path: str) -> HiddenSelection | None:
        candidates = [item for (client_id, path), item in self._selections.items() if path == virtual_path]
        if self._preferred_client_id:
            candidates.sort(key=lambda item: item.client_id == self._preferred_client_id, reverse=True)
        else:
            candidates.sort(key=lambda item: item.created_at, reverse=True)
        for selection in candidates:
            if time.monotonic() - selection.created_at <= self.selection_ttl:
                return selection
            self._selections.pop((selection.client_id, selection.path), None)
        return None

    async def _selection_or_current(
        self,
        path: str,
        *,
        expected_text: str | None = None,
    ) -> tuple[HiddenSelection, Path]:
        virtual_path, real_path = self._resolve_path(path, write=True)
        if real_path.suffix.lower() == ".pdf":
            raise OnlyOfficeProtocolError(ErrorCode.READ_ONLY, "PDF 第一阶段不支持写入")
        selection = self._find_selection(virtual_path)
        if selection:
            self._assert_file_unchanged(
                DocumentSnapshot(
                    path=selection.path,
                    file_hash=selection.file_hash,
                    snapshot_id=selection.snapshot_id,
                    client_id=selection.client_id,
                    editor_session_id=selection.editor_session_id,
                    document_key=selection.document_key,
                    lines=[],
                ),
                real_path,
            )
            return selection, real_path
        if not expected_text:
            raise OnlyOfficeProtocolError(ErrorCode.NO_ACTIVE_SELECTION, "没有有效选区，请先调用 navigate_and_select")
        result, client, editor, virtual_path, real_path = await self.dispatch(
            virtual_path,
            "document_get_selection",
            {},
            write=False,
            with_context=True,
        )
        selected_text = str(result.get("selected_text") or "")
        if selected_text != expected_text:
            raise OnlyOfficeProtocolError(ErrorCode.SELECTION_CHANGED, "编辑器当前选区与 expected_text 不一致")
        snapshot, real_path = await self._refresh_snapshot(virtual_path, preferred_client_id=client.client_id)
        selection = HiddenSelection(
            path=virtual_path,
            selected_text=selected_text,
            file_hash=snapshot.file_hash,
            snapshot_id=snapshot.snapshot_id,
            client_id=client.client_id,
            editor_session_id=editor.editor_session_id,
            document_key=editor.document_key,
        )
        self._selections[(client.client_id, virtual_path)] = selection
        return selection, real_path

    async def _save_selection_write(
        self,
        selection: HiddenSelection,
        command: str,
        args: dict[str, Any],
    ) -> tuple[dict[str, Any], DocumentSnapshot, DocumentSnapshot, Path]:
        before = self._snapshots.get((selection.client_id, selection.path)) or self._latest_snapshots.get(selection.path)
        if before is None:
            before, _ = await self._refresh_snapshot(selection.path, preferred_client_id=selection.client_id)
        result, client, editor, virtual_path, real_path = await self.dispatch(
            selection.path,
            command,
            {**args, "expected_text": selection.selected_text},
            write=True,
            preferred_client_id=selection.client_id,
            with_context=True,
        )
        if editor.editor_session_id != selection.editor_session_id or editor.document_key != selection.document_key:
            raise OnlyOfficeProtocolError(ErrorCode.SELECTION_CHANGED, "编辑器实例或文档版本已变化")
        try:
            await self.document_service.force_save_and_wait(
                editor.document_key,
                request_id=secrets.token_hex(16),
                timeout=self.save_timeout,
            )
        except Exception as exc:
            raise OnlyOfficeProtocolError(ErrorCode.SAVE_FAILED, "OnlyOffice 保存失败") from exc
        after, real_path = await self._refresh_snapshot(virtual_path, preferred_client_id=client.client_id)
        self._selections.pop((selection.client_id, selection.path), None)
        return result, before, after, real_path

    async def tool_insert_or_replace(self, path: str, operation: str, text: str) -> ToolResult:
        try:
            if operation not in {"replace", "insert_before", "insert_after"}:
                raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "operation 只允许 replace/insert_before/insert_after")
            if not isinstance(text, str):
                raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "text 必须是字符串")
            selection, _ = await self._selection_or_current(path)
            if (operation != "replace" and not text) or (operation == "replace" and text == selection.selected_text):
                raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "修改内容不能是空操作")
            _result, before, after, real_path = await self._save_selection_write(
                selection,
                "document_insert_or_replace",
                {"operation": operation, "text": text},
            )
            if before.snapshot_id == after.snapshot_id:
                raise OnlyOfficeProtocolError(ErrorCode.VERIFY_FAILED, "修改后文档内容没有变化")
            payload = {"success": True, "unified_diff": _snapshot_diff(selection.path, before.lines, after.lines)}
            return ToolResult(content=json.dumps(payload, ensure_ascii=False), metadata={"path": str(real_path), "workspace_handled": True})
        except OnlyOfficeProtocolError as exc:
            return _error_result(exc.code, exc.message, exc.metadata)
        except Exception as exc:
            logger.exception("OnlyOffice insert/replace failed")
            return _error_result(ErrorCode.INTERNAL_ERROR, str(exc))

    async def _format(
        self,
        path: str,
        properties: dict[str, Any],
        *,
        expected_text: str | None = None,
    ) -> tuple[dict[str, Any], Path]:
        clean = _validate_format_properties(properties)
        selection, _ = await self._selection_or_current(path, expected_text=expected_text)
        result, _before, _after, real_path = await self._save_selection_write(
            selection,
            "document_format",
            {"properties": clean},
        )
        if not result.get("applied"):
            raise OnlyOfficeProtocolError(ErrorCode.VERIFY_FAILED, "OnlyOffice 未确认格式已应用")
        diff = _property_diff(selection.path, "format", selection.selected_text, clean)
        return {"success": True, "unified_diff": diff}, real_path

    async def tool_format(self, path: str, properties: dict[str, Any]) -> ToolResult:
        try:
            payload, real_path = await self._format(path, properties)
            return ToolResult(content=json.dumps(payload, ensure_ascii=False), metadata={"path": str(real_path), "workspace_handled": True})
        except OnlyOfficeProtocolError as exc:
            return _error_result(exc.code, exc.message, exc.metadata)
        except Exception as exc:
            logger.exception("OnlyOffice format failed")
            return _error_result(ErrorCode.INTERNAL_ERROR, str(exc))

    async def _add_comment(
        self,
        path: str,
        text: str,
        *,
        expected_text: str | None = None,
        author: str = "Agent",
        author_user_id: str = "agent",
    ) -> tuple[dict[str, Any], Path]:
        if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
            raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "批注必须是 1 到 20000 字符的非空字符串")
        selection, _ = await self._selection_or_current(path, expected_text=expected_text)
        result, _before, _after, real_path = await self._save_selection_write(
            selection,
            "document_add_comment",
            {"text": text, "author": author or "Agent", "author_user_id": author_user_id or "agent"},
        )
        if not result.get("added"):
            raise OnlyOfficeProtocolError(ErrorCode.VERIFY_FAILED, "OnlyOffice 未确认批注已添加")
        return {"success": True}, real_path

    async def tool_add_comment(self, path: str, text: str) -> ToolResult:
        try:
            payload, real_path = await self._add_comment(path, text)
            return ToolResult(content=json.dumps(payload, ensure_ascii=False), metadata={"path": str(real_path), "workspace_handled": True})
        except OnlyOfficeProtocolError as exc:
            return _error_result(exc.code, exc.message, exc.metadata)
        except Exception as exc:
            logger.exception("OnlyOffice add comment failed")
            return _error_result(ErrorCode.INTERNAL_ERROR, str(exc))

    async def http_format(self, path: str, properties: dict[str, Any], *, expected_text: str | None = None) -> dict[str, Any]:
        payload, _ = await self._format(path, properties, expected_text=expected_text)
        return payload

    async def http_add_comment(
        self,
        path: str,
        text: str,
        *,
        expected_text: str | None = None,
        author: str = "Agent",
        author_user_id: str = "agent",
    ) -> dict[str, Any]:
        payload, _ = await self._add_comment(
            path,
            text,
            expected_text=expected_text,
            author=author,
            author_user_id=author_user_id,
        )
        return payload

    async def tool_command(self, path: str, command: str, args: dict[str, Any], *, write: bool) -> ToolResult:
        try:
            result = await self.dispatch(path, command, args, write=write)
            virtual_path, real_path = self._resolve_path(path, write=write)
            if write:
                selected = self._choose_client(virtual_path, require_editor=True)
                assert isinstance(selected, tuple)
                save_request_id = secrets.token_hex(16)
                await self.document_service.force_save_and_wait(
                    selected[1].document_key,
                    request_id=save_request_id,
                    timeout=self.save_timeout,
                )
                result["saved"] = True
            return ToolResult(
                content=json.dumps(result, ensure_ascii=False),
                metadata={"path": str(real_path), "workspace_handled": True},
            )
        except OnlyOfficeProtocolError as exc:
            return _error_result(exc.code, exc.message, exc.metadata)
        except Exception as exc:
            logger.exception("OnlyOffice realtime tool failed: command=%s", command)
            return _error_result(ErrorCode.INTERNAL_ERROR, str(exc))


def _error_result(code: str, message: str, metadata: dict[str, Any] | None = None) -> ToolResult:
    error = {"code": code, "message": message}
    if metadata:
        error["metadata"] = metadata
    payload = {"error": error}
    return ToolResult(content=json.dumps(payload, ensure_ascii=False), is_error=True, metadata=metadata or {})


def _short_id(value: str) -> str:
    text = str(value or "")
    return text[:12] if text else "-"


def _log_text(value: Any, limit: int) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ")[:limit]


def _editor_log_state(editor: EditorState) -> dict[str, Any]:
    return {
        "path": editor.path,
        "editor": _short_id(editor.editor_session_id),
        "key": _short_id(editor.document_key),
        "ready": editor.ready,
        "active": editor.active,
        "writable": editor.writable,
        "error": _log_text(editor.error, 200),
    }


def _validate_variables(variables: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(variables, dict):
        raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "variables 必须是对象")
    result: dict[str, Any] = {}
    for key, value in variables.items():
        if not isinstance(key, str) or not key or len(key) > 128:
            raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, "占位符名称必须是限长非空字符串")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise OnlyOfficeProtocolError(ErrorCode.PROTOCOL_INVALID, f"变量 {key} 不是 JSON 标量")
        result[key] = value
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_snapshot_lines(lines: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in lines:
        if not isinstance(item, dict):
            raise OnlyOfficeProtocolError(ErrorCode.VERIFY_FAILED, "逻辑行快照包含无效条目")
        text = str(item.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        page = item.get("page")
        result.append({
            "text": text,
            "paragraph_text": str(item.get("paragraph_text") or text),
            "page": int(page) if isinstance(page, (int, float)) and not isinstance(page, bool) else None,
            "start": item.get("start"),
            "end": item.get("end"),
        })
    return result


def _extract_pdf_logical_lines(path: Path) -> list[dict[str, Any]]:
    try:
        import fitz
    except ImportError as exc:
        raise OnlyOfficeProtocolError(ErrorCode.UNSUPPORTED_FILE_TYPE, "读取 PDF 需要 PyMuPDF") from exc
    lines: list[dict[str, Any]] = []
    with fitz.open(str(path)) as document:
        for page_index, page in enumerate(document):
            text = str(page.get_text("text", sort=True) or "").replace("\r\n", "\n").replace("\r", "\n")
            page_lines = text.split("\n")
            if page_lines and page_lines[-1] == "":
                page_lines.pop()
            if not page_lines:
                page_lines = [""]
            for line in page_lines:
                lines.append({
                    "text": line,
                    "paragraph_text": line,
                    "page": page_index + 1,
                    "start": None,
                    "end": None,
                })
    if lines and not any(line["text"].strip() for line in lines):
        raise OnlyOfficeProtocolError(
            ErrorCode.UNSUPPORTED_FILE_TYPE,
            "PDF 没有可靠文本层；请使用 pdf_read 查看扫描页，不能按文本行定位",
        )
    return lines


def _validate_line_window(start_line: int | None, end_line: int | None, total: int) -> tuple[int, int]:
    for value, name in ((start_line, "start_line"), (end_line, "end_line")):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
            raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, f"{name} 必须是 1-based 正整数")
    if total == 0:
        if start_line is not None or end_line is not None:
            raise OnlyOfficeProtocolError(ErrorCode.LINE_OUT_OF_RANGE, "文档没有可读取的逻辑行")
        return 1, 0
    start = start_line or 1
    end = end_line or total
    if start > end:
        raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "start_line 不能大于 end_line")
    if start > total or end > total:
        raise OnlyOfficeProtocolError(ErrorCode.LINE_OUT_OF_RANGE, f"行号必须在 1 到 {total} 之间")
    return start, end


def _render_numbered_lines(lines: list[dict[str, Any]], start_line: int) -> str:
    return "\n".join(
        f"{start_line + index} | {line['text']}" if line["text"] else f"{start_line + index} |"
        for index, line in enumerate(lines)
    )


def _validate_selection_target(
    *,
    line_no: int | None,
    select_text: str | None,
    anchor: str | None,
    page: int | None,
) -> None:
    for value, name in ((line_no, "line_no"), (page, "page")):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
            raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, f"{name} 必须是 1-based 正整数")
    if anchor is not None and (not isinstance(anchor, str) or not anchor):
        raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "anchor 必须是非空字符串")
    if select_text is not None and (not isinstance(select_text, str) or not select_text):
        raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "select_text 必须是非空字符串")
    if anchor is not None and select_text is not None:
        raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "anchor 模式不能同时提供 select_text")
    if select_text is not None and line_no is None and page is None:
        raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "select_text 必须配合 line_no 或 page")
    if anchor is None and select_text is None and line_no is None and page is None:
        raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "至少需要 line_no、page、anchor 或 select_text 定位条件")


_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _validate_format_properties(properties: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(properties, dict) or not properties:
        raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "properties 至少需要一个格式属性")
    allowed = {
        "font_family", "font_size", "bold", "italic", "underline", "strikeout",
        "font_color", "background_color", "alignment", "line_spacing",
    }
    unknown = sorted(set(properties) - allowed)
    if unknown:
        raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, f"不支持的格式属性: {', '.join(unknown)}")
    clean: dict[str, Any] = {}
    if "font_family" in properties:
        value = properties["font_family"]
        if not isinstance(value, str) or not value or len(value) > 100:
            raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "font_family 必须是 1 到 100 字符")
        clean["font_family"] = value
    if "font_size" in properties:
        value = properties["font_size"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 1 <= value <= 300:
            raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "font_size 必须在 1 到 300 pt 之间")
        clean["font_size"] = float(value)
    for name in ("bold", "italic", "underline", "strikeout"):
        if name in properties:
            if not isinstance(properties[name], bool):
                raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, f"{name} 必须是布尔值")
            clean[name] = properties[name]
    for name in ("font_color", "background_color"):
        if name in properties:
            value = properties[name]
            if not isinstance(value, str) or not _HEX_COLOR_RE.fullmatch(value):
                raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, f"{name} 必须是 #RRGGBB")
            clean[name] = value.upper()
    if "alignment" in properties:
        value = properties["alignment"]
        if value not in {"left", "center", "right", "justify"}:
            raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "alignment 只允许 left/center/right/justify")
        clean["alignment"] = value
    if "line_spacing" in properties:
        spacing = properties["line_spacing"]
        if not isinstance(spacing, dict) or set(spacing) != {"rule", "value"}:
            raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "line_spacing 必须只包含 rule 和 value")
        rule = spacing.get("rule")
        value = spacing.get("value")
        if rule not in {"multiple", "exact", "at_least"}:
            raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "line_spacing.rule 无效")
        maximum = 10 if rule == "multiple" else 1000
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value <= maximum:
            raise OnlyOfficeProtocolError(ErrorCode.INVALID_ARGUMENT, "line_spacing.value 超出允许范围")
        clean["line_spacing"] = {"rule": rule, "value": float(value)}
    return clean


def _snapshot_diff(path: str, before: list[dict[str, Any]], after: list[dict[str, Any]]) -> str:
    before_text = [line["text"] for line in before]
    after_text = [line["text"] for line in after]
    matcher = difflib.SequenceMatcher(a=before_text, b=after_text, autojunk=False)
    output = [f"--- {path}@before", f"+++ {path}@after"]
    for group in matcher.get_grouped_opcodes(3):
        before_start = group[0][1]
        before_end = group[-1][2]
        after_start = group[0][3]
        after_end = group[-1][4]
        output.append(
            f"@@ -{_diff_range(before_start, before_end)} +{_diff_range(after_start, after_end)} @@"
        )
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for offset, text in enumerate(before_text[i1:i2]):
                    output.append(f" {i1 + offset + 1} | {text}" if text else f" {i1 + offset + 1} |")
            if tag in {"delete", "replace"}:
                for offset, text in enumerate(before_text[i1:i2]):
                    output.append(f"-{i1 + offset + 1} | {text}" if text else f"-{i1 + offset + 1} |")
            if tag in {"insert", "replace"}:
                for offset, text in enumerate(after_text[j1:j2]):
                    output.append(f"+{j1 + offset + 1} | {text}" if text else f"+{j1 + offset + 1} |")
    return "\n".join(output)


def _diff_range(start: int, end: int) -> str:
    count = end - start
    beginning = start + 1
    if count == 0:
        beginning -= 1
    return str(beginning) if count == 1 else f"{beginning},{count}"


def _property_diff(path: str, kind: str, selected_text: str, properties: dict[str, Any]) -> str:
    before = [f"selection: {selected_text}"]
    after = [f"selection: {selected_text}"] + [
        f"{name}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}"
        for name, value in sorted(properties.items())
    ]
    return "\n".join(difflib.unified_diff(
        before,
        after,
        fromfile=f"{path}@{kind}-before",
        tofile=f"{path}@{kind}-after",
        lineterm="",
    ))


def _tool_definitions(runtime: OnlyOfficeSessionAutomation):
    yield (
        "onlyoffice_create_from_template",
        "从工作区模板复制创建新 DOCX/XLSX，在 OnlyOffice 中打开，并可选地实时填充 {{name}} 占位符后自动保存。"
        "模板与目标扩展名必须相同且为 .docx/.xlsx；目标文件不能已存在（否则 FILE_EXISTS）。"
        "variables 中每个占位符都必须在模板里存在（写作 {{name}}，大小写敏感），任一缺失则整体失败"
        "（PLACEHOLDER_NOT_FOUND，metadata.missing 列出缺失项），不会部分填充。"
        "返回 fill.replaced 为每个占位符的实际替换次数。",
        {"type": "object", "properties": {
            "template_path": {
                "type": "string",
                "description": "工作区相对路径的模板文件，必须已存在，.docx 或 .xlsx",
            },
            "target_path": {
                "type": "string",
                "description": "新文件的工作区相对路径；不能已存在，扩展名须与模板相同",
            },
            "variables": {
                "type": "object",
                "description": "占位符名 → 标量值；省略则仅复制模板不填充。DOCX 中值一律转为文本；XLSX 中整格恰为 {{name}} 的单元格保留值原始类型",
                "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
            },
        }, "required": ["template_path", "target_path"]},
        runtime.tool_create_from_template,
    )

    async def document_read(path: str, start_line: int | None = None, end_line: int | None = None):
        return await runtime.tool_document_read(path, start_line=start_line, end_line=end_line)

    yield (
        "onlyoffice_document_read",
        "读取 DOCX/PDF 并建立版本绑定的逻辑行快照。每行输出为 `数字 | 文本`，空行保留。"
        "start_line/end_line 为 1-based 闭区间；都省略时读取全文。该工具不会修改通用 document_read。",
        {"type": "object", "properties": {
            "path": {
                "type": "string",
                "description": "工作区相对路径的 DOCX 或 PDF 文件",
            },
            "start_line": {"type": "integer", "minimum": 1, "description": "可选起始逻辑行，包含该行"},
            "end_line": {"type": "integer", "minimum": 1, "description": "可选结束逻辑行，包含该行"},
        }, "required": ["path"]}, document_read,
    )

    async def document_navigate(path: str, target: dict[str, Any]):
        return await runtime.tool_command(path, "document_navigate", {"target": target}, write=False)

    yield (
        "onlyoffice_document_navigate",
        "兼容旧调用的 DOCX 定位工具；新流程优先使用 onlyoffice_document_navigate_and_select。"
        "按文本锚点或页码定位，成功后匹配文本会被选中。"
        "anchor 必须是从文档逐字复制的连续原文片段（精确子串匹配，不是正则/关键词/改写）；"
        "拿不准原文时先调 onlyoffice_document_read 核对。全文唯一匹配时直接成功；"
        "匹配多次会报 TARGET_NOT_UNIQUE（错误信息含匹配数 N），此时任选其一重试："
        "补 context_before/context_after 消歧，或用 occurrence 指定第几个匹配（1..N）。"
        "无匹配报 TARGET_NOT_FOUND，说明 anchor 与原文不一致，不要原样重试。"
        "选中后旧调用可继续使用 onlyoffice_document_edit；新调用应使用隐藏选区写入工具。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "工作区相对路径的 DOCX 文件（如 reports/demo.docx）。编辑器未打开时自动打开并等待就绪",
                },
                "target": {
                    "type": "object",
                    "description": "定位目标；anchor 与 page 至少提供一个",
                    "properties": {
                        "anchor": {
                            "type": "string",
                            "description": "锚点原文：从文档复制的连续文本片段，按精确子串全文搜索；标点、空格、大小写须与原文一致",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "1-based 页码；仅跳页定位时可只给 page 不给 anchor。与 anchor 同给时先跳页，锚点搜索仍全文进行",
                        },
                        "occurrence": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "选取第几个匹配，1-based，按 context 过滤后的顺序编号；仅在锚点匹配多次时需要",
                        },
                        "context_before": {
                            "type": "string",
                            "description": "紧邻锚点之前的原文片段（匹配条件：锚点前文以它结尾）。用于多匹配消歧",
                        },
                        "context_after": {
                            "type": "string",
                            "description": "紧邻锚点之后的原文片段（匹配条件：锚点后文以它开头）。用于多匹配消歧",
                        },
                        "match_case": {
                            "type": "boolean",
                            "default": True,
                            "description": "是否区分大小写，默认 true",
                        },
                    },
                },
            },
            "required": ["path", "target"],
        },
        document_navigate,
    )

    async def document_edit(path: str, operation: str, text: str = "", expected_text: str | None = None, target: dict[str, Any] | None = None):
        args = {"operation": operation, "text": text}
        if expected_text is not None:
            args["expected_text"] = expected_text
        if target is not None:
            args["target"] = target
        return await runtime.tool_command(path, "document_edit", args, write=True)

    yield (
        "onlyoffice_document_edit",
        "兼容旧调用的 DOCX 编辑工具；新流程优先使用 onlyoffice_document_insert_or_replace。"
        "在活动 OnlyOffice DOCX 中实时写入，成功后自动保存；编辑器需为编辑模式（只读报 READ_ONLY）。"
        "按 operation 分三种用法：① replace_match（替换已知文本，最常用）：给 target 定位、校验原文并替换，一步完成；"
        "② replace_selection / delete_selection：作用于编辑器当前选区，必须给 expected_text 且与选区内容逐字一致，"
        "不一致报 STALE_SELECTION（需要重新定位选区后再重试）；"
        "③ insert_at_cursor：在光标处插入 text，无前置条件。text 中的换行会成为段落分隔、制表符保留。",
        {"type": "object", "properties": {
            "path": {
                "type": "string",
                "description": "工作区相对路径的 DOCX 文件（如 reports/demo.docx）。编辑器未打开时自动打开并等待就绪",
            },
            "operation": {
                "type": "string",
                "enum": ["insert_at_cursor", "replace_selection", "delete_selection", "replace_match"],
                "description": "replace_match=定位+替换一步完成；replace_selection/delete_selection=作用于当前选区；insert_at_cursor=光标处插入",
            },
            "text": {
                "type": "string",
                "description": "要写入的文本：insert_at_cursor 的插入内容、replace_selection/replace_match 的替换内容（空串=删除）；delete_selection 忽略此参数",
            },
            "expected_text": {
                "type": "string",
                "description": "前置条件文本：replace_selection/delete_selection 必填，须与选区当前内容逐字一致（表格单元格以制表符分隔、段落以换行分隔）；replace_match 可省略，默认取 target.anchor",
            },
            "target": {
                "type": "object",
                "description": "仅 replace_match 必填；定位目标，语义同 onlyoffice_document_navigate 的 target",
                "properties": {
                    "anchor": {
                        "type": "string",
                        "description": "锚点原文：从文档复制的连续文本片段，精确子串全文搜索；标点、空格、大小写须与原文一致",
                    },
                    "occurrence": {
                        "type": "integer", "minimum": 1,
                        "description": "锚点匹配多次时指定第几个（1-based，按 context 过滤后顺序）",
                    },
                    "context_before": {
                        "type": "string",
                        "description": "紧邻锚点之前的原文片段（前文以它结尾），多匹配消歧用",
                    },
                    "context_after": {
                        "type": "string",
                        "description": "紧邻锚点之后的原文片段（后文以它开头），多匹配消歧用",
                    },
                    "match_case": {
                        "type": "boolean", "default": True,
                        "description": "是否区分大小写，默认 true",
                    },
                },
            },
        }, "required": ["path", "operation"]}, document_edit,
    )

    async def search(path: str, keyword: str, context_lines: int = 2, match_case: bool = False):
        return await runtime.tool_search(path, keyword, context_lines=context_lines, match_case=match_case)

    yield (
        "onlyoffice_search",
        "在 DOCX/PDF 逻辑行中搜索普通文本并刷新行号快照。返回命中行全文、所属段落、页码及编号上下文；"
        "同一行多次出现只返回一个命中项，最多返回 50 个命中行。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "工作区相对路径的 DOCX 或 PDF 文件"},
            "keyword": {"type": "string", "minLength": 1, "description": "普通文本关键字，不是正则表达式"},
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 20, "default": 2},
            "match_case": {"type": "boolean", "default": False},
        }, "required": ["path", "keyword"]},
        search,
    )

    async def navigate_and_select(
        path: str,
        line_no: int | None = None,
        select_text: str | None = None,
        anchor: str | None = None,
        page: int | None = None,
    ):
        return await runtime.tool_navigate_and_select(
            path, line_no=line_no, select_text=select_text, anchor=anchor, page=page,
        )

    yield (
        "onlyoffice_document_navigate_and_select",
        "使用逻辑行号、唯一锚点或页码在 DOCX/PDF 中定位。line_no+select_text 仅在指定行精确选择；"
        "anchor 全文精确匹配，可用 line_no/page 共同过滤；单独 line_no/page 只移动光标并清空隐藏选区。"
        "成功仅返回 success 与 selected_text，歧义时拒绝猜测。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "工作区相对路径的 DOCX 或 PDF 文件"},
            "line_no": {"type": "integer", "minimum": 1},
            "select_text": {"type": "string", "minLength": 1},
            "anchor": {"type": "string", "minLength": 1},
            "page": {"type": "integer", "minimum": 1},
        }, "required": ["path"]},
        navigate_and_select,
    )

    async def insert_or_replace(path: str, operation: str, text: str):
        return await runtime.tool_insert_or_replace(path, operation, text)

    yield (
        "onlyoffice_document_insert_or_replace",
        "修改由 onlyoffice_document_navigate_and_select 建立的隐藏选区。支持替换、前插、后插；"
        "执行前验证选区和文档版本，保存并复读后仅返回 unified diff。PDF 为只读。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "工作区相对路径的 DOCX 文件"},
            "operation": {"type": "string", "enum": ["replace", "insert_before", "insert_after"]},
            "text": {"type": "string", "description": "替换或插入的文本"},
        }, "required": ["path", "operation", "text"]},
        insert_or_replace,
    )

    format_properties_schema = {
        "type": "object",
        "properties": {
            "font_family": {"type": "string", "minLength": 1, "maxLength": 100},
            "font_size": {"type": "number", "minimum": 1, "maximum": 300},
            "bold": {"type": "boolean"},
            "italic": {"type": "boolean"},
            "underline": {"type": "boolean"},
            "strikeout": {"type": "boolean"},
            "font_color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
            "background_color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
            "alignment": {"type": "string", "enum": ["left", "center", "right", "justify"]},
            "line_spacing": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string", "enum": ["multiple", "exact", "at_least"]},
                    "value": {"type": "number", "exclusiveMinimum": 0},
                },
                "required": ["rule", "value"],
                "additionalProperties": False,
            },
        },
        "minProperties": 1,
        "additionalProperties": False,
    }

    async def format_selection(path: str, properties: dict[str, Any]):
        return await runtime.tool_format(path, properties)

    yield (
        "onlyoffice_format",
        "格式化当前隐藏文本选区，支持字体、字号、字符样式、前景/背景色、段落对齐与行距；"
        "保存验证后返回格式属性 unified diff。PDF 为只读。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "工作区相对路径的 DOCX 文件"},
            "properties": format_properties_schema,
        }, "required": ["path", "properties"]},
        format_selection,
    )

    async def add_comment(path: str, text: str):
        return await runtime.tool_add_comment(path, text)

    yield (
        "onlyoffice_add_comment",
        "向当前隐藏文本选区添加批注，默认作者为 Agent。保存并验证后仅返回 success。PDF 第一阶段不支持。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "工作区相对路径的 DOCX 文件"},
            "text": {"type": "string", "minLength": 1, "maxLength": 20000},
        }, "required": ["path", "text"]},
        add_comment,
    )

    async def spreadsheet_read(path: str, scope: str = "used_range", sheet_name: str | None = None, cell_range: str | None = None, value_mode: str = "values"):
        return await runtime.tool_command(path, "spreadsheet_read", {
            "scope": scope, "sheet_name": sheet_name, "cell_range": cell_range,
            "value_mode": value_mode, "max_cells": runtime.max_cells,
        }, write=False)

    yield (
        "onlyoffice_spreadsheet_read",
        "从活动 OnlyOffice XLSX 编辑器实时读取：当前选区、指定区域或工作表已使用区域。"
        "返回 {sheet_name, address, rows, columns, values?, formulas?}，values/formulas 为行主序二维数组。"
        "写入前应先读取目标区域，把返回的 values 原样作为 expected_values/expected_value 传给 "
        "onlyoffice_spreadsheet_edit 做前置条件校验。",
        {"type": "object", "properties": {
            "path": {
                "type": "string",
                "description": "工作区相对路径的 XLSX 文件（如 reports/demo.xlsx）。编辑器未打开时自动打开并等待就绪",
            },
            "scope": {
                "type": "string", "enum": ["selection", "range", "used_range"], "default": "used_range",
                "description": "selection=编辑器当前选区；range=cell_range 指定的区域（此时 cell_range 必填）；used_range=该工作表已使用区域",
            },
            "sheet_name": {
                "type": "string",
                "description": "工作表名；省略则为当前活动工作表。不存在报 TARGET_NOT_FOUND",
            },
            "cell_range": {
                "type": "string",
                "description": "A1 格式区域（如 \"A1:C3\"），不含工作表名前缀（工作表由 sheet_name 指定）；仅 scope=range 时使用",
            },
            "value_mode": {
                "type": "string", "enum": ["values", "formulas", "both"], "default": "values",
                "description": "values=计算值；formulas=公式文本；both=两者都返回",
            },
        }, "required": ["path"]}, spreadsheet_read,
    )

    async def spreadsheet_edit(path: str, operation: str, payload: dict[str, Any], sheet_name: str | None = None):
        return await runtime.tool_command(path, "spreadsheet_edit", {
            "operation": operation, "payload": payload, "sheet_name": sheet_name,
        }, write=True)

    yield (
        "onlyoffice_spreadsheet_edit",
        "在活动 OnlyOffice XLSX 中实时写入并自动保存；编辑器需为编辑模式（只读报 READ_ONLY）。"
        "覆盖类操作都要求前置条件校验（乐观并发）：expected_values/expected_value 必须与目标区域当前值完全一致，"
        "不一致报 STALE_RANGE（metadata.actual 带当前值）——先用 onlyoffice_spreadsheet_read 读取同一区域，"
        "把返回的 values 原样作为 expected_values 再重试，不要凭记忆构造。"
        "payload 按 operation 取四种结构之一：set_range={range, values, expected_values}；"
        "update_cells={cells:[{cell, value, expected_value}]}（零散单元格，逐格校验）；"
        "clear_range={range, expected_values}（清空内容）；append_rows={rows:[[...]]}（追加行，无需校验）。",
        {"type": "object", "properties": {
            "path": {
                "type": "string",
                "description": "工作区相对路径的 XLSX 文件（如 reports/demo.xlsx）。编辑器未打开时自动打开并等待就绪",
            },
            "sheet_name": {
                "type": "string",
                "description": "工作表名；省略则为当前活动工作表。不存在报 TARGET_NOT_FOUND",
            },
            "operation": {
                "type": "string", "enum": ["set_range", "update_cells", "clear_range", "append_rows"],
                "description": "set_range=整块写入；update_cells=逐格更新；clear_range=清空区域；append_rows=追加行",
            },
            "payload": {
                "type": "object",
                "description": "结构由 operation 决定（见工具描述）；其中的 range 均为 A1 格式（如 \"A1:C3\"），不含工作表名前缀",
                "properties": {
                    "range": {
                        "type": "string",
                        "description": "目标区域（set_range/clear_range 必填），如 \"A1:C3\"",
                    },
                    "values": {
                        "type": "array", "items": {"type": "array"},
                        "description": "set_range 的写入值：行主序二维数组，行列数须与 range 一致",
                    },
                    "expected_values": {
                        "type": "array", "items": {"type": "array"},
                        "description": "set_range/clear_range 必填：目标区域当前值二维数组，须与 onlyoffice_spreadsheet_read 返回的 values 完全一致",
                    },
                    "cells": {
                        "type": "array",
                        "description": "update_cells 必填：零散单元格列表，逐格校验后写入",
                        "items": {
                            "type": "object",
                            "properties": {
                                "cell": {"type": "string", "description": "单元格地址，如 \"B2\""},
                                "value": {"description": "写入值（标量）"},
                                "expected_value": {"description": "该格当前值（必填），须与读取结果一致"},
                            },
                        },
                    },
                    "rows": {
                        "type": "array", "items": {"type": "array"},
                        "description": "append_rows 的追加内容：行主序二维数组；追加在已使用区域下方，起始列对齐已使用区域",
                    },
                },
            },
        }, "required": ["path", "operation", "payload"]}, spreadsheet_edit,
    )
