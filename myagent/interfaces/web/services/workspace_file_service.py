"""Secure, resumable workspace file-management services.

The service keeps filesystem policy out of the HTTP route layer.  It builds on
``WorkspaceResolver`` so every operation is checked against the existing
private/public permission model before touching disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import posixpath
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from myagent.core.workspace import IGNORED_DIR_NAMES
from myagent.utils.logging import get_logger


logger = get_logger(__name__)

ConflictPolicy = Literal["fail", "skip", "overwrite", "keep_both"]

ARCHIVE_SUFFIXES = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst",
    ".cab", ".iso", ".jar", ".war", ".ear", ".apk", ".dmg",
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",
}
ZIP_OFFICE_SUFFIXES = {
    ".docx", ".docm", ".dotx", ".dotm",
    ".xlsx", ".xlsm", ".xlsb", ".xltx", ".xltm",
    ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm", ".sldx",
    ".odt", ".ods", ".odp", ".odg", ".odf", ".ott", ".ots", ".otp", ".otg",
    ".vsdx", ".vssx", ".vstx", ".vstm", ".vdx",
}
ARCHIVE_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"PK\x07\x08", "zip"),
    (b"Rar!\x1a\x07\x00", "rar"),
    (b"Rar!\x1a\x07\x01\x00", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"MSCF", "cab"),
)

VISIBLE_DOTFILES = {".gitignore", ".env.example"}
INTERNAL_DIRS = {".myagent_uploads", ".myagent_trash"}
COPY_BUFFER_SIZE = 1024 * 1024


class WorkspaceFileError(Exception):
    """Structured domain error translated to an HTTP response by the route."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        path: str = "",
        details: Any = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.path = path
        self.details = details
        self.retryable = retryable

    def to_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "details": self.details,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class WorkspaceLimits:
    private_quota_bytes: int = 10 * 1024**3
    max_file_bytes: int = 1024**3
    max_batch_bytes: int = 2 * 1024**3
    max_batch_files: int = 1000
    upload_chunk_bytes: int = 8 * 1024**2
    max_concurrent_uploads: int = 3
    upload_expiry_hours: int = 24
    trash_retention_days: int = 30
    list_page_size: int = 200
    search_result_limit: int = 200

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "WorkspaceLimits":
        data = dict(raw or {})
        defaults = cls()

        def positive(name: str, default: int) -> int:
            try:
                value = int(data.get(name, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        return cls(**{
            field_name: positive(field_name, getattr(defaults, field_name))
            for field_name in defaults.__dataclass_fields__
        })

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class WorkspaceJob:
    id: str
    owner: str
    session_id: str
    kind: str
    state: str = "queued"
    processed_bytes: int = 0
    total_bytes: int = 0
    processed_items: int = 0
    total_items: int = 0
    current_path: str = ""
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    result_path: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 3600)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def touch(self) -> None:
        self.updated_at = time.time()

    def ensure_active(self) -> None:
        if self.cancel_event.is_set():
            raise WorkspaceFileError(409, "JOB_CANCELLED", "任务已取消")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "processed_bytes": self.processed_bytes,
            "total_bytes": self.total_bytes,
            "processed_items": self.processed_items,
            "total_items": self.total_items,
            "current_path": self.current_path,
            "error": self.error,
            "result": self.result,
            "created_at": _iso_from_timestamp(self.created_at),
            "updated_at": _iso_from_timestamp(self.updated_at),
            "expires_at": _iso_from_timestamp(self.expires_at),
        }


class WorkspaceJobRegistry:
    """In-process background jobs with stable IDs for UI polling."""

    def __init__(self) -> None:
        self._jobs: dict[str, WorkspaceJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def create(
        self,
        *,
        owner: str,
        session_id: str,
        kind: str,
        runner: Callable[[WorkspaceJob], Awaitable[dict[str, Any] | None]],
    ) -> WorkspaceJob:
        self.cleanup_expired()
        job = WorkspaceJob(id=uuid.uuid4().hex, owner=owner, session_id=session_id, kind=kind)
        self._jobs[job.id] = job

        async def _run() -> None:
            job.state = "running"
            job.touch()
            try:
                result = await runner(job)
                if job.cancel_event.is_set():
                    job.state = "cancelled"
                else:
                    job.result = result or {}
                    job.state = "completed"
            except WorkspaceFileError as exc:
                job.error = exc.to_detail()
                job.state = "cancelled" if exc.code == "JOB_CANCELLED" else "failed"
            except asyncio.CancelledError:
                job.cancel_event.set()
                job.state = "cancelled"
                raise
            except Exception as exc:  # pragma: no cover - defensive boundary
                logger.exception("Workspace job failed: id=%s kind=%s", job.id, job.kind)
                job.error = {
                    "code": "JOB_FAILED",
                    "message": str(exc) or "后台任务失败",
                    "path": job.current_path,
                    "details": None,
                    "retryable": False,
                }
                job.state = "failed"
            finally:
                job.touch()
                job.expires_at = time.time() + 3600
                self._tasks.pop(job.id, None)

        self._tasks[job.id] = asyncio.create_task(_run(), name=f"workspace-{kind}-{job.id}")
        return job

    def get(self, job_id: str, owner: str) -> WorkspaceJob:
        self.cleanup_expired()
        job = self._jobs.get(job_id)
        if not job or job.owner != owner:
            raise WorkspaceFileError(404, "JOB_NOT_FOUND", "任务不存在")
        return job

    def cancel(self, job_id: str, owner: str) -> WorkspaceJob:
        job = self.get(job_id, owner)
        if job.state in {"completed", "failed", "cancelled"}:
            return job
        job.cancel_event.set()
        job.state = "cancelling"
        job.touch()
        return job

    def cleanup_expired(self) -> None:
        now = time.time()
        for job_id, job in list(self._jobs.items()):
            if job.expires_at > now or job.state in {"queued", "running", "cancelling"}:
                continue
            if job.result_path:
                try:
                    Path(job.result_path).unlink(missing_ok=True)
                except OSError:
                    pass
            self._jobs.pop(job_id, None)


workspace_jobs = WorkspaceJobRegistry()
_upload_locks: dict[str, asyncio.Lock] = {}


def has_forbidden_archive_suffix(path: str) -> bool:
    lower = str(path or "").lower()
    return any(lower.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def has_zip_office_suffix(path: str) -> bool:
    lower = str(path or "").lower()
    return any(lower.endswith(suffix) for suffix in ZIP_OFFICE_SUFFIXES)


def archive_magic_label(header: bytes) -> str:
    for magic, label in ARCHIVE_MAGICS:
        if header.startswith(magic):
            return label
    if len(header) >= 263 and header[257:263] in {b"ustar\x00", b"ustar "}:
        return "tar"
    return ""


def validate_entry_name(name: str) -> str:
    raw = unicodedata.normalize("NFC", str(name or "").strip())
    if not raw:
        raise WorkspaceFileError(400, "INVALID_NAME", "名称不能为空")
    if raw in {".", ".."} or "/" in raw or "\\" in raw or "\x00" in raw:
        raise WorkspaceFileError(400, "INVALID_NAME", "名称包含非法字符")
    if any(ord(char) < 32 for char in raw):
        raise WorkspaceFileError(400, "INVALID_NAME", "名称不能包含控制字符")
    if len(raw.encode("utf-8")) > 255:
        raise WorkspaceFileError(400, "INVALID_NAME", "名称过长")
    if raw in INTERNAL_DIRS or (raw.startswith(".") and raw not in VISIBLE_DOTFILES):
        raise WorkspaceFileError(400, "RESERVED_NAME", "该名称属于隐藏或系统保留名称")
    if has_forbidden_archive_suffix(raw):
        raise WorkspaceFileError(415, "ARCHIVE_FORBIDDEN", "不允许使用压缩或归档文件名")
    return raw


def validate_upload_path(path: str) -> str:
    raw = unicodedata.normalize("NFC", str(path or "")).replace("\\", "/").strip("/")
    if not raw:
        raise WorkspaceFileError(400, "INVALID_PATH", "文件路径不能为空")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceFileError(400, "INVALID_PATH", "路径不能包含空片段、. 或 ..", path=raw)
    for part in parts:
        validate_entry_name(part)
    return "/".join(parts)


class WorkspaceFileService:
    """Filesystem operations for one authenticated workspace session."""

    def __init__(self, session, session_manager, limits: WorkspaceLimits):
        self.session = session
        self.session_manager = session_manager
        self.limits = limits
        self.resolver = getattr(session.workspace, "resolver", None) if session.workspace else None
        if not self.resolver:
            raise WorkspaceFileError(404, "WORKSPACE_NOT_FOUND", "会话工作空间不存在")
        self.username = session.user.username
        self.actor = self.resolver.actor_for_user()

    # ------------------------------------------------------------------
    # Read surface
    # ------------------------------------------------------------------

    async def list_dir(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        sort_by: str = "name",
        order: str = "asc",
    ) -> dict[str, Any]:
        page_size = min(max(int(limit or self.limits.list_page_size), 1), 500)
        clean_path = str(path or "").strip("/")
        entries = await self.resolver.scan_dir(clean_path or None)
        records = await asyncio.to_thread(self._entry_records, entries)
        reverse = str(order).lower() == "desc"
        key = {
            "size": lambda item: (not item["is_dir"], item["size"], item["name"].casefold()),
            "modified": lambda item: (not item["is_dir"], item["modified_at"], item["name"].casefold()),
            "type": lambda item: (not item["is_dir"], item["extension"], item["name"].casefold()),
        }.get(sort_by, lambda item: (not item["is_dir"], item["name"].casefold()))
        records.sort(key=key, reverse=reverse)
        offset = max(int(offset), 0)
        page = records[offset:offset + page_size]
        next_offset = offset + len(page)
        return {
            "path": clean_path,
            "entries": page,
            "offset": offset,
            "next_offset": next_offset if next_offset < len(records) else None,
            "total": len(records),
            "quota": await self.quota(),
            "limits": self.limits.to_dict(),
        }

    async def details(self, path: str) -> dict[str, Any]:
        resolved = self._resolve(path, "read", must_exist=True)
        self._reject_symlink(resolved.real_path, path)
        return await asyncio.to_thread(self._details_sync, resolved)

    async def search(self, query: str, *, areas: list[str] | None = None, limit: int | None = None) -> dict[str, Any]:
        needle = str(query or "").strip().casefold()
        if not needle:
            return {"query": "", "entries": [], "truncated": False}
        max_results = min(max(int(limit or self.limits.search_result_limit), 1), 500)
        selected = set(areas or ["private", "public"])
        entries, truncated = await asyncio.to_thread(self._search_sync, needle, selected, max_results)
        return {"query": query, "entries": entries, "truncated": truncated}

    async def quota(self) -> dict[str, Any]:
        used = await asyncio.to_thread(self._tree_size, self.resolver.private_root, False)
        reserved = await asyncio.to_thread(self._reserved_upload_bytes)
        total = self.limits.private_quota_bytes
        return {
            "used_bytes": used,
            "reserved_bytes": reserved,
            "total_bytes": total,
            "available_bytes": max(0, total - used - reserved),
        }

    # ------------------------------------------------------------------
    # Immediate mutations
    # ------------------------------------------------------------------

    async def create_folder(self, parent: str, name: str) -> dict[str, Any]:
        safe_name = validate_entry_name(name)
        parent_resolved = self._resolve(parent, "write", must_exist=True)
        if not parent_resolved.real_path.is_dir():
            raise WorkspaceFileError(400, "NOT_A_DIRECTORY", "目标路径不是目录", path=parent)
        target_virtual = _join_virtual(parent_resolved.virtual_path, safe_name)
        target = self._resolve(target_virtual, "write", must_exist=False)
        try:
            await asyncio.to_thread(target.real_path.mkdir, parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise WorkspaceFileError(409, "NAME_CONFLICT", "同名文件或目录已存在", path=target_virtual) from exc
        await self._notify({"changed_paths": [target_virtual]})
        return {"ok": True, "entry": await self.details(target_virtual)}

    async def rename(self, path: str, new_name: str, expected_version: str = "") -> dict[str, Any]:
        safe_name = validate_entry_name(new_name)
        source = self._resolve(path, "rename", must_exist=True)
        self._ensure_not_root(source.virtual_path)
        self._reject_symlink(source.real_path, path)
        self._check_version(source.real_path, expected_version, path)
        self._ensure_paths_not_open([source.virtual_path])
        target_virtual = _join_virtual(posixpath.dirname(source.virtual_path), safe_name)
        target = self._resolve(target_virtual, "rename", must_exist=False)
        if target.real_path.exists():
            raise WorkspaceFileError(409, "NAME_CONFLICT", "目标名称已存在", path=target_virtual)
        try:
            await asyncio.to_thread(os.replace, source.real_path, target.real_path)
        except OSError as exc:
            raise WorkspaceFileError(400, "RENAME_FAILED", f"重命名失败: {exc}", path=path) from exc
        await self._notify({
            "changed_paths": [target_virtual],
            "renamed_paths": [{"from": source.virtual_path, "to": target_virtual}],
        })
        return {"ok": True, "from": source.virtual_path, "to": target_virtual}

    async def delete(self, paths: list[str], *, expected_versions: dict[str, str] | None = None) -> dict[str, Any]:
        normalized = self._dedupe_nested_paths(paths)
        if not normalized:
            raise WorkspaceFileError(400, "EMPTY_SELECTION", "未选择要删除的文件")
        self._ensure_paths_not_open(normalized)
        result = await asyncio.to_thread(self._delete_sync, normalized, expected_versions or {})
        await self._notify({"deleted_paths": result["deleted_paths"]})
        return {"ok": True, "deleted": result["deleted"], "rejected": []}

    async def list_trash(self) -> dict[str, Any]:
        await asyncio.to_thread(self._cleanup_expired_trash)
        entries = await asyncio.to_thread(self._read_trash_entries)
        public_entries = [
            {key: value for key, value in item.items() if key != "object_path"}
            for item in entries
        ]
        return {"entries": public_entries, "retention_days": self.limits.trash_retention_days}

    async def restore_trash(
        self,
        trash_ids: list[str],
        *,
        target_dir: str = "",
        conflict_policy: ConflictPolicy = "fail",
    ) -> dict[str, Any]:
        result = await asyncio.to_thread(
            self._restore_trash_sync,
            trash_ids,
            target_dir,
            conflict_policy,
        )
        await self._notify({"changed_paths": result["restored_paths"]})
        return {"ok": True, "restored": result["restored"]}

    async def purge_trash(self, trash_ids: list[str] | None = None) -> dict[str, Any]:
        purged = await asyncio.to_thread(self._purge_trash_sync, trash_ids)
        return {"ok": True, "purged": purged}

    # ------------------------------------------------------------------
    # Background operations
    # ------------------------------------------------------------------

    def start_copy_move(
        self,
        *,
        kind: Literal["copy", "move"],
        sources: list[str],
        target_dir: str,
        conflict_policy: ConflictPolicy,
        expected_versions: dict[str, str] | None = None,
    ) -> WorkspaceJob:
        if kind == "move":
            self._ensure_paths_not_open(sources)

        async def runner(job: WorkspaceJob) -> dict[str, Any]:
            result = await asyncio.to_thread(
                self._copy_move_sync,
                job,
                kind,
                sources,
                target_dir,
                conflict_policy,
                expected_versions or {},
            )
            await self._notify(result["change"])
            return {key: value for key, value in result.items() if key != "change"}

        return workspace_jobs.create(
            owner=self.username,
            session_id=self.session.id,
            kind=kind,
            runner=runner,
        )

    def start_archive(self, paths: list[str]) -> WorkspaceJob:
        async def runner(job: WorkspaceJob) -> dict[str, Any]:
            return await asyncio.to_thread(self._archive_sync, job, paths)

        return workspace_jobs.create(
            owner=self.username,
            session_id=self.session.id,
            kind="archive",
            runner=runner,
        )

    # ------------------------------------------------------------------
    # Resumable uploads
    # ------------------------------------------------------------------

    async def init_upload(
        self,
        *,
        target_dir: str,
        files: list[dict[str, Any]],
        conflict_policy: ConflictPolicy,
    ) -> dict[str, Any]:
        self._cleanup_expired_uploads()
        if not files:
            raise WorkspaceFileError(400, "EMPTY_UPLOAD", "未选择上传文件")
        if len(files) > self.limits.max_batch_files:
            raise WorkspaceFileError(413, "TOO_MANY_FILES", f"单批最多上传 {self.limits.max_batch_files} 个文件")

        target = self._resolve(target_dir, "upload", must_exist=True)
        if not target.real_path.is_dir():
            raise WorkspaceFileError(400, "NOT_A_DIRECTORY", "上传目标不是目录", path=target_dir)

        normalized: list[dict[str, Any]] = []
        total_size = 0
        conflicts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(files):
            rel = validate_upload_path(str(item.get("path") or ""))
            size = int(item.get("size") or 0)
            if size < 0 or size > self.limits.max_file_bytes:
                raise WorkspaceFileError(413, "FILE_TOO_LARGE", "文件超过单文件大小限制", path=rel)
            total_size += size
            target_virtual = _join_virtual(target.virtual_path, rel)
            if target_virtual in seen:
                raise WorkspaceFileError(400, "DUPLICATE_TARGET", "上传列表包含重复目标", path=target_virtual)
            seen.add(target_virtual)
            target_path = self._resolve(target_virtual, "upload", must_exist=False).real_path
            if target_path.exists():
                if target_path.is_dir():
                    raise WorkspaceFileError(409, "TARGET_IS_DIRECTORY", "上传目标是目录", path=target_virtual)
                conflicts.append({"path": rel, "target_path": target_virtual, "version": _entry_version(target_path)})
            normalized.append({
                "id": f"f{index + 1}",
                "path": rel,
                "target_path": target_virtual,
                "size": size,
                "last_modified": int(item.get("last_modified") or 0),
                "chunk_count": math.ceil(size / self.limits.upload_chunk_bytes) if size else 0,
                "completed_chunks": [],
            })

        if total_size > self.limits.max_batch_bytes:
            raise WorkspaceFileError(413, "BATCH_TOO_LARGE", "上传批次超过大小限制")
        if conflicts and conflict_policy == "fail":
            raise WorkspaceFileError(409, "UPLOAD_CONFLICT", "上传目标存在同名文件", details={"conflicts": conflicts})
        if conflicts and conflict_policy == "overwrite" and not self.resolver.can(self.actor, "write", target.area):
            raise WorkspaceFileError(403, "OVERWRITE_FORBIDDEN", "当前用户不能覆盖该区域中的现有文件")
        if conflict_policy == "skip" and conflicts:
            conflict_paths = {item["target_path"] for item in conflicts}
            normalized = [item for item in normalized if item["target_path"] not in conflict_paths]
            total_size = sum(item["size"] for item in normalized)
        if conflict_policy == "keep_both" and conflicts:
            conflict_paths = {item["target_path"] for item in conflicts}
            for item in normalized:
                if item["target_path"] not in conflict_paths:
                    continue
                existing = self._resolve(item["target_path"], "upload", must_exist=True)
                unique = _unique_path(existing.real_path)
                item["target_path"] = _join_virtual(posixpath.dirname(item["target_path"]), unique.name)
        if not normalized:
            return {"ok": True, "upload_id": None, "files": [], "skipped": conflicts}

        if target.area == "private":
            quota = await self.quota()
            if total_size > quota["available_bytes"]:
                raise WorkspaceFileError(413, "QUOTA_EXCEEDED", "private 工作区可用容量不足", details=quota)

        upload_id = uuid.uuid4().hex
        upload_dir = self._uploads_root() / upload_id
        await asyncio.to_thread(upload_dir.mkdir, parents=True, exist_ok=False)
        manifest = {
            "version": 1,
            "id": upload_id,
            "owner": self.username,
            "session_id": self.session.id,
            "target_dir": target.virtual_path,
            "area": target.area,
            "conflict_policy": conflict_policy,
            "created_at": time.time(),
            "expires_at": time.time() + self.limits.upload_expiry_hours * 3600,
            "chunk_bytes": self.limits.upload_chunk_bytes,
            "total_size": total_size,
            "files": normalized,
        }
        await asyncio.to_thread(self._write_manifest, upload_dir, manifest)
        return self._upload_status(manifest, conflicts=conflicts)

    async def upload_status(self, upload_id: str) -> dict[str, Any]:
        _upload_dir, manifest = await asyncio.to_thread(self._load_upload, upload_id)
        return self._upload_status(manifest)

    async def write_upload_chunk(
        self,
        upload_id: str,
        file_id: str,
        chunk_index: int,
        body: bytes,
        checksum: str = "",
    ) -> dict[str, Any]:
        lock = _upload_locks.setdefault(upload_id, asyncio.Lock())
        async with lock:
            upload_dir, manifest = await asyncio.to_thread(self._load_upload, upload_id)
            file_item = next((item for item in manifest["files"] if item["id"] == file_id), None)
            if not file_item:
                raise WorkspaceFileError(404, "UPLOAD_FILE_NOT_FOUND", "上传文件不存在")
            chunk_count = int(file_item["chunk_count"])
            if chunk_index < 0 or chunk_index >= chunk_count:
                raise WorkspaceFileError(400, "INVALID_CHUNK", "分片编号无效")
            expected_size = min(
                int(manifest["chunk_bytes"]),
                int(file_item["size"]) - chunk_index * int(manifest["chunk_bytes"]),
            )
            if len(body) != expected_size:
                raise WorkspaceFileError(400, "INVALID_CHUNK_SIZE", "分片大小不正确")
            digest = hashlib.sha256(body).hexdigest()
            if checksum and checksum.lower() != digest:
                raise WorkspaceFileError(400, "CHUNK_CHECKSUM_MISMATCH", "分片校验失败", retryable=True)

            chunks_dir = upload_dir / "chunks" / file_id
            await asyncio.to_thread(chunks_dir.mkdir, parents=True, exist_ok=True)
            destination = chunks_dir / f"{chunk_index:08d}.part"
            await asyncio.to_thread(_atomic_write_bytes, destination, body)
            completed = set(int(value) for value in file_item.get("completed_chunks", []))
            completed.add(chunk_index)
            file_item["completed_chunks"] = sorted(completed)
            manifest["expires_at"] = time.time() + self.limits.upload_expiry_hours * 3600
            await asyncio.to_thread(self._write_manifest, upload_dir, manifest)
            return {
                "ok": True,
                "upload_id": upload_id,
                "file_id": file_id,
                "chunk_index": chunk_index,
                "checksum": digest,
                "completed_chunks": file_item["completed_chunks"],
            }

    async def complete_upload(self, upload_id: str) -> dict[str, Any]:
        lock = _upload_locks.setdefault(upload_id, asyncio.Lock())
        async with lock:
            upload_dir, manifest = await asyncio.to_thread(self._load_upload, upload_id)
            changed = await asyncio.to_thread(self._complete_upload_sync, upload_dir, manifest)
        _upload_locks.pop(upload_id, None)
        await self._notify({"changed_paths": changed})
        return {"ok": True, "uploaded": [{"path": path} for path in changed], "rejected": []}

    async def cancel_upload(self, upload_id: str) -> dict[str, Any]:
        lock = _upload_locks.setdefault(upload_id, asyncio.Lock())
        async with lock:
            upload_dir, _manifest = await asyncio.to_thread(self._load_upload, upload_id)
            await asyncio.to_thread(shutil.rmtree, upload_dir, True)
        _upload_locks.pop(upload_id, None)
        return {"ok": True, "upload_id": upload_id, "state": "cancelled"}

    # ------------------------------------------------------------------
    # Internal read helpers
    # ------------------------------------------------------------------

    def _entry_records(self, entries) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for info in entries:
            try:
                resolved = self._resolve(info.path, "read", must_exist=True)
                if resolved.real_path.is_symlink():
                    continue
                stat = resolved.real_path.stat()
            except (OSError, WorkspaceFileError):
                continue
            name = posixpath.basename(info.path)
            result.append({
                **info.to_dict(),
                "name": name,
                "extension": "" if info.is_dir else Path(name).suffix.lower(),
                "version": _entry_version_from_stat(stat),
                "capabilities": self._capabilities(info.path, info.area, info.is_dir),
            })
        return result

    def _details_sync(self, resolved) -> dict[str, Any]:
        stat = resolved.real_path.stat()
        is_dir = resolved.real_path.is_dir()
        child_count = 0
        descendant_count = 0
        size = stat.st_size if not is_dir else 0
        if is_dir:
            for root, dirs, files in os.walk(resolved.real_path, followlinks=False):
                dirs[:] = [name for name in dirs if self._visible_name(name) and not (Path(root) / name).is_symlink()]
                visible_files = [name for name in files if self._visible_name(name) and not (Path(root) / name).is_symlink()]
                if Path(root) == resolved.real_path:
                    child_count = len(dirs) + len(visible_files)
                descendant_count += len(dirs) + len(visible_files)
                for name in visible_files:
                    try:
                        size += (Path(root) / name).stat().st_size
                    except OSError:
                        pass
        name = resolved.real_path.name
        return {
            "path": resolved.virtual_path,
            "name": name,
            "is_dir": is_dir,
            "size": size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "extension": "" if is_dir else resolved.real_path.suffix.lower(),
            "area": resolved.area,
            "version": _entry_version_from_stat(stat),
            "child_count": child_count,
            "descendant_count": descendant_count,
            "capabilities": self._capabilities(resolved.virtual_path, resolved.area, is_dir),
        }

    def _search_sync(self, needle: str, areas: set[str], limit: int) -> tuple[list[dict[str, Any]], bool]:
        result: list[dict[str, Any]] = []
        truncated = False
        roots = (("private", self.resolver.private_root), ("public", self.resolver.public_root))
        for area, root in roots:
            if area not in areas:
                continue
            for current, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [name for name in dirs if self._visible_name(name) and not (Path(current) / name).is_symlink()]
                for name in [*dirs, *files]:
                    path = Path(current) / name
                    if path.is_symlink() or not self._visible_name(name):
                        continue
                    inner = path.relative_to(root).as_posix()
                    virtual = self.resolver._join_virtual(area, inner)
                    if needle not in virtual.casefold():
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    is_dir = path.is_dir()
                    result.append({
                        "path": virtual,
                        "name": name,
                        "is_dir": is_dir,
                        "size": 0 if is_dir else stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        "extension": "" if is_dir else path.suffix.lower(),
                        "area": area,
                        "version": _entry_version_from_stat(stat),
                        "capabilities": self._capabilities(virtual, area, is_dir),
                    })
                    if len(result) >= limit:
                        truncated = True
                        return result, truncated
        return result, truncated

    def _capabilities(self, path: str, area: str, is_dir: bool) -> dict[str, bool]:
        is_root = path in self.resolver.root_virtual_paths
        return {
            "read": True,
            "download": not is_root,
            "upload": is_dir and self.resolver.can(self.actor, "upload", area),
            "create_folder": is_dir and self.resolver.can(self.actor, "write", area),
            "rename": not is_root and self.resolver.can(self.actor, "rename", area),
            "copy": not is_root,
            "move": not is_root and self.resolver.can(self.actor, "delete", area),
            "delete": not is_root and self.resolver.can(self.actor, "delete", area),
            "replace": not is_root and not is_dir and self.resolver.can(self.actor, "write", area),
        }

    # ------------------------------------------------------------------
    # Internal mutation helpers
    # ------------------------------------------------------------------

    def _delete_sync(self, paths: list[str], expected_versions: dict[str, str]) -> dict[str, Any]:
        deleted: list[dict[str, Any]] = []
        deleted_paths: list[str] = []
        for path in paths:
            resolved = self._resolve(path, "delete", must_exist=True)
            self._ensure_not_root(resolved.virtual_path)
            self._reject_symlink(resolved.real_path, path)
            self._check_version(resolved.real_path, expected_versions.get(path, ""), path)
            kind = "dir" if resolved.real_path.is_dir() else "file"
            if resolved.area == "private":
                trash_id = uuid.uuid4().hex
                trash_root = self._trash_root()
                object_dir = trash_root / "objects" / trash_id
                object_dir.mkdir(parents=True, exist_ok=False)
                destination = object_dir / resolved.real_path.name
                size = self._tree_size(resolved.real_path, True)
                os.replace(resolved.real_path, destination)
                deleted_at = datetime.now(timezone.utc)
                metadata = {
                    "id": trash_id,
                    "original_path": resolved.virtual_path,
                    "name": resolved.real_path.name,
                    "kind": kind,
                    "size": size,
                    "deleted_at": deleted_at.isoformat(),
                    "expires_at": (deleted_at + timedelta(days=self.limits.trash_retention_days)).isoformat(),
                }
                self._write_json_atomic(trash_root / "meta" / f"{trash_id}.json", metadata)
                deleted.append({**metadata, "recoverable": True})
            else:
                if resolved.real_path.is_dir():
                    shutil.rmtree(resolved.real_path)
                else:
                    resolved.real_path.unlink()
                deleted.append({"path": resolved.virtual_path, "kind": kind, "recoverable": False})
            deleted_paths.append(resolved.virtual_path)
        return {"deleted": deleted, "deleted_paths": deleted_paths}

    def _restore_trash_sync(
        self,
        trash_ids: list[str],
        target_dir: str,
        conflict_policy: ConflictPolicy,
    ) -> dict[str, Any]:
        restored: list[dict[str, Any]] = []
        restored_paths: list[str] = []
        entries = {item["id"]: item for item in self._read_trash_entries()}
        for trash_id in trash_ids:
            item = entries.get(trash_id)
            if not item:
                raise WorkspaceFileError(404, "TRASH_NOT_FOUND", "回收站项目不存在")
            source = Path(item["object_path"])
            original = str(item["original_path"])
            destination_virtual = (
                _join_virtual(target_dir, item["name"])
                if target_dir
                else original
            )
            destination = self._resolve(destination_virtual, "write", must_exist=False)
            if destination.area != "private":
                raise WorkspaceFileError(403, "TRASH_PRIVATE_ONLY", "回收站项目只能恢复到 private 目录")
            destination.real_path.parent.mkdir(parents=True, exist_ok=True)
            if destination.real_path.exists():
                destination_virtual, destination_path = self._resolve_conflict(
                    destination_virtual,
                    destination.real_path,
                    conflict_policy,
                )
                if destination_path is None:
                    continue
                destination = self._resolve(destination_virtual, "write", must_exist=False)
                if conflict_policy == "overwrite":
                    _remove_path(destination.real_path)
            os.replace(source, destination.real_path)
            meta_path = self._trash_root() / "meta" / f"{trash_id}.json"
            meta_path.unlink(missing_ok=True)
            shutil.rmtree(source.parent, ignore_errors=True)
            restored.append({"id": trash_id, "path": destination.virtual_path})
            restored_paths.append(destination.virtual_path)
        return {"restored": restored, "restored_paths": restored_paths}

    def _purge_trash_sync(self, trash_ids: list[str] | None) -> list[str]:
        entries = self._read_trash_entries()
        wanted = set(trash_ids or [item["id"] for item in entries])
        purged: list[str] = []
        for item in entries:
            if item["id"] not in wanted:
                continue
            shutil.rmtree(Path(item["object_path"]).parent, ignore_errors=True)
            (self._trash_root() / "meta" / f"{item['id']}.json").unlink(missing_ok=True)
            purged.append(item["id"])
        return purged

    def _copy_move_sync(
        self,
        job: WorkspaceJob,
        kind: str,
        sources: list[str],
        target_dir: str,
        conflict_policy: ConflictPolicy,
        expected_versions: dict[str, str],
    ) -> dict[str, Any]:
        target = self._resolve(target_dir, "upload", must_exist=True)
        if not target.real_path.is_dir():
            raise WorkspaceFileError(400, "NOT_A_DIRECTORY", "目标路径不是目录", path=target_dir)
        resolved_sources = []
        for path in self._dedupe_nested_paths(sources):
            resolved = self._resolve(path, "read", must_exist=True)
            self._ensure_not_root(resolved.virtual_path)
            self._reject_symlink(resolved.real_path, path)
            self._check_version(resolved.real_path, expected_versions.get(path, ""), path)
            if kind == "move":
                self._resolve(path, "delete", must_exist=True)
            if target.real_path == resolved.real_path or resolved.real_path in target.real_path.parents:
                raise WorkspaceFileError(400, "INVALID_DESTINATION", "不能将目录移动或复制到自身内部", path=path)
            resolved_sources.append(resolved)

        job.total_bytes = sum(self._tree_size(item.real_path, True) for item in resolved_sources)
        job.total_items = sum(self._tree_items(item.real_path) for item in resolved_sources)
        private_growth = 0
        if target.area == "private":
            for source in resolved_sources:
                if kind == "copy" or source.area != "private":
                    private_growth += self._tree_size(source.real_path, True)
        if private_growth:
            used = self._tree_size(self.resolver.private_root, False)
            reserved = self._reserved_upload_bytes()
            if used + reserved + private_growth > self.limits.private_quota_bytes:
                raise WorkspaceFileError(
                    413,
                    "QUOTA_EXCEEDED",
                    "private 工作区可用容量不足",
                    details={
                        "used_bytes": used,
                        "reserved_bytes": reserved,
                        "required_bytes": private_growth,
                        "total_bytes": self.limits.private_quota_bytes,
                    },
                )
        job.touch()
        changed_paths: list[str] = []
        renamed_paths: list[dict[str, str]] = []

        for source in resolved_sources:
            job.ensure_active()
            destination_virtual = _join_virtual(target.virtual_path, source.real_path.name)
            destination = self._resolve(destination_virtual, "upload", must_exist=False)
            if destination.real_path == source.real_path:
                if kind == "move":
                    continue
                if conflict_policy != "keep_both":
                    raise WorkspaceFileError(
                        409,
                        "NAME_CONFLICT",
                        "复制到当前目录时请选择保留两份",
                        path=source.virtual_path,
                    )
            if destination.real_path.exists():
                if conflict_policy == "overwrite" and not self.resolver.can(self.actor, "write", destination.area):
                    raise WorkspaceFileError(
                        403,
                        "OVERWRITE_FORBIDDEN",
                        "当前用户不能覆盖该区域中的现有文件",
                        path=destination_virtual,
                    )
                if conflict_policy == "overwrite":
                    self._ensure_paths_not_open([destination_virtual])
                destination_virtual, destination_path = self._resolve_conflict(
                    destination_virtual,
                    destination.real_path,
                    conflict_policy,
                )
                if destination_path is None:
                    continue
                destination = self._resolve(destination_virtual, "upload", must_exist=False)

            job.current_path = source.virtual_path
            job.touch()
            if kind == "move" and not destination.real_path.exists():
                try:
                    os.replace(source.real_path, destination.real_path)
                    job.processed_bytes += self._tree_size(destination.real_path, True)
                    job.processed_items += self._tree_items(destination.real_path)
                except OSError:
                    self._copy_path(job, source.real_path, destination.real_path, overwrite=False)
                    _remove_path(source.real_path)
            else:
                self._copy_path(
                    job,
                    source.real_path,
                    destination.real_path,
                    overwrite=conflict_policy == "overwrite",
                )
                if kind == "move":
                    _remove_path(source.real_path)
            changed_paths.append(destination.virtual_path)
            if kind == "move":
                renamed_paths.append({"from": source.virtual_path, "to": destination.virtual_path})

        change: dict[str, Any] = {"changed_paths": changed_paths}
        if renamed_paths:
            change["renamed_paths"] = renamed_paths
        job.current_path = ""
        job.touch()
        return {"paths": changed_paths, "change": change}

    def _copy_path(self, job: WorkspaceJob, source: Path, destination: Path, *, overwrite: bool) -> None:
        job.ensure_active()
        if source.is_dir():
            if destination.exists() and not destination.is_dir():
                if not overwrite:
                    raise WorkspaceFileError(409, "NAME_CONFLICT", "目标类型冲突", path=str(destination))
                _remove_path(destination)
            destination.mkdir(parents=True, exist_ok=True)
            job.processed_items += 1
            for child in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
                if child.is_symlink():
                    raise WorkspaceFileError(400, "SYMLINK_UNSUPPORTED", "不支持复制符号链接", path=str(child))
                self._copy_path(job, child, destination / child.name, overwrite=overwrite)
            return
        if destination.exists() and destination.is_dir():
            if not overwrite:
                raise WorkspaceFileError(409, "NAME_CONFLICT", "目标类型冲突", path=str(destination))
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.copying"
        try:
            with source.open("rb") as src, temp_path.open("wb") as dst:
                while True:
                    job.ensure_active()
                    chunk = src.read(COPY_BUFFER_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
                    job.processed_bytes += len(chunk)
                    job.touch()
            shutil.copystat(source, temp_path)
            if destination.exists() and overwrite:
                _remove_path(destination)
            os.replace(temp_path, destination)
            job.processed_items += 1
        finally:
            temp_path.unlink(missing_ok=True)

    def _archive_sync(self, job: WorkspaceJob, paths: list[str]) -> dict[str, Any]:
        sources = []
        for path in self._dedupe_nested_paths(paths):
            resolved = self._resolve(path, "read", must_exist=True)
            self._ensure_not_root(resolved.virtual_path)
            self._reject_symlink(resolved.real_path, path)
            sources.append(resolved)
        if not sources:
            raise WorkspaceFileError(400, "EMPTY_SELECTION", "未选择要下载的文件")
        job.total_bytes = sum(self._tree_size(item.real_path, True) for item in sources)
        job.total_items = sum(self._tree_items(item.real_path) for item in sources)
        fd, archive_name = tempfile.mkstemp(prefix="workspace-download-", suffix=".zip")
        os.close(fd)
        archive_path = Path(archive_name)
        try:
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for source in sources:
                    base = source.real_path.parent
                    if source.real_path.is_file():
                        job.ensure_active()
                        zf.write(source.real_path, source.real_path.relative_to(base).as_posix())
                        job.processed_bytes += source.real_path.stat().st_size
                        job.processed_items += 1
                        continue
                    for current, dirs, files in os.walk(source.real_path, followlinks=False):
                        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
                        for name in files:
                            job.ensure_active()
                            child = Path(current) / name
                            if child.is_symlink():
                                continue
                            job.current_path = child.relative_to(base).as_posix()
                            zf.write(child, child.relative_to(base).as_posix())
                            job.processed_bytes += child.stat().st_size
                            job.processed_items += 1
                            job.touch()
            job.result_path = str(archive_path)
            return {"filename": "workspace-download.zip", "size": archive_path.stat().st_size}
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # Upload internals
    # ------------------------------------------------------------------

    def _upload_status(self, manifest: dict[str, Any], *, conflicts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        uploaded_bytes = 0
        chunk_bytes = int(manifest["chunk_bytes"])
        for item in manifest["files"]:
            for index in item.get("completed_chunks", []):
                uploaded_bytes += min(chunk_bytes, int(item["size"]) - int(index) * chunk_bytes)
        return {
            "ok": True,
            "upload_id": manifest["id"],
            "state": "uploading",
            "chunk_bytes": chunk_bytes,
            "total_bytes": int(manifest["total_size"]),
            "uploaded_bytes": uploaded_bytes,
            "expires_at": _iso_from_timestamp(float(manifest["expires_at"])),
            "files": manifest["files"],
            "conflicts": conflicts or [],
        }

    def _complete_upload_sync(self, upload_dir: Path, manifest: dict[str, Any]) -> list[str]:
        changed: list[str] = []
        prepared: list[tuple[dict[str, Any], Any, Path]] = []
        try:
            for item in manifest["files"]:
                expected = set(range(int(item["chunk_count"])))
                completed = set(int(index) for index in item.get("completed_chunks", []))
                if expected != completed:
                    raise WorkspaceFileError(
                        409,
                        "UPLOAD_INCOMPLETE",
                        "仍有分片未上传",
                        path=item["path"],
                        details={"missing_chunks": sorted(expected - completed)},
                        retryable=True,
                    )
                target = self._resolve(item["target_path"], "upload", must_exist=False)
                target.real_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = target.real_path.parent / f".{target.real_path.name}.{manifest['id']}.uploading"
                with temp_path.open("wb") as output:
                    for index in range(int(item["chunk_count"])):
                        chunk_path = upload_dir / "chunks" / item["id"] / f"{index:08d}.part"
                        with chunk_path.open("rb") as chunk_file:
                            shutil.copyfileobj(chunk_file, output, COPY_BUFFER_SIZE)
                if temp_path.stat().st_size != int(item["size"]):
                    temp_path.unlink(missing_ok=True)
                    raise WorkspaceFileError(400, "UPLOAD_SIZE_MISMATCH", "上传文件大小校验失败", path=item["path"])
                with temp_path.open("rb") as file_obj:
                    header = file_obj.read(4096)
                magic = archive_magic_label(header)
                if magic and not (magic == "zip" and has_zip_office_suffix(target.real_path.name)):
                    temp_path.unlink(missing_ok=True)
                    raise WorkspaceFileError(415, "ARCHIVE_FORBIDDEN", f"不允许上传压缩或归档文件: {magic}", path=item["path"])
                if target.real_path.exists():
                    if manifest["conflict_policy"] != "overwrite":
                        temp_path.unlink(missing_ok=True)
                        raise WorkspaceFileError(409, "UPLOAD_CONFLICT", "完成上传时目标已存在", path=item["target_path"])
                    self._ensure_paths_not_open([item["target_path"]])
                prepared.append((item, target, temp_path))

            for item, target, temp_path in prepared:
                os.replace(temp_path, target.real_path)
                changed.append(target.virtual_path)
            shutil.rmtree(upload_dir, ignore_errors=True)
            return changed
        except Exception:
            for _item, _target, temp_path in prepared:
                temp_path.unlink(missing_ok=True)
            raise

    def _uploads_root(self) -> Path:
        root = self.resolver.private_root / ".myagent_uploads"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _load_upload(self, upload_id: str) -> tuple[Path, dict[str, Any]]:
        if not upload_id or any(char not in "0123456789abcdef" for char in upload_id.lower()):
            raise WorkspaceFileError(404, "UPLOAD_NOT_FOUND", "上传任务不存在")
        upload_dir = self._uploads_root() / upload_id
        manifest_path = upload_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceFileError(404, "UPLOAD_NOT_FOUND", "上传任务不存在") from exc
        if manifest.get("owner") != self.username:
            raise WorkspaceFileError(404, "UPLOAD_NOT_FOUND", "上传任务不存在")
        if float(manifest.get("expires_at") or 0) < time.time():
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise WorkspaceFileError(410, "UPLOAD_EXPIRED", "上传任务已过期")
        return upload_dir, manifest

    def _write_manifest(self, upload_dir: Path, manifest: dict[str, Any]) -> None:
        self._write_json_atomic(upload_dir / "manifest.json", manifest)

    def _reserved_upload_bytes(self) -> int:
        total = 0
        root = self._uploads_root()
        for manifest_path in root.glob("*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("owner") == self.username and manifest.get("area") == "private":
                    total += int(manifest.get("total_size") or 0)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return total

    def _cleanup_expired_uploads(self) -> None:
        root = self._uploads_root()
        now = time.time()
        for manifest_path in root.glob("*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if float(manifest.get("expires_at") or 0) < now:
                    shutil.rmtree(manifest_path.parent, ignore_errors=True)
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    # ------------------------------------------------------------------
    # Trash and shared helpers
    # ------------------------------------------------------------------

    def _trash_root(self) -> Path:
        root = self.resolver.private_root / ".myagent_trash"
        (root / "objects").mkdir(parents=True, exist_ok=True)
        (root / "meta").mkdir(parents=True, exist_ok=True)
        return root

    def _read_trash_entries(self) -> list[dict[str, Any]]:
        result = []
        trash_root = self._trash_root()
        for meta_path in trash_root.joinpath("meta").glob("*.json"):
            try:
                item = json.loads(meta_path.read_text(encoding="utf-8"))
                object_dir = trash_root / "objects" / item["id"]
                candidates = list(object_dir.iterdir())
                if not candidates:
                    continue
                item["object_path"] = str(candidates[0])
                result.append(item)
            except (OSError, KeyError, json.JSONDecodeError):
                continue
        result.sort(key=lambda item: item.get("deleted_at", ""), reverse=True)
        return result

    def _cleanup_expired_trash(self) -> None:
        now = datetime.now(timezone.utc)
        expired = []
        for item in self._read_trash_entries():
            try:
                expires_at = datetime.fromisoformat(str(item["expires_at"]))
            except (KeyError, ValueError):
                continue
            if expires_at <= now:
                expired.append(item["id"])
        if expired:
            self._purge_trash_sync(expired)

    def _resolve(self, path: str, operation: str, *, must_exist: bool):
        try:
            return self.resolver.resolve(
                str(path or "").strip("/"),
                operation=operation,
                actor=self.actor,
                must_exist=must_exist,
            )
        except FileNotFoundError as exc:
            raise WorkspaceFileError(404, "PATH_NOT_FOUND", "路径不存在", path=path) from exc
        except PermissionError as exc:
            raise WorkspaceFileError(403, "PERMISSION_DENIED", str(exc), path=path) from exc
        except ValueError as exc:
            raise WorkspaceFileError(400, "INVALID_PATH", str(exc), path=path) from exc

    async def _notify(self, change: dict[str, Any]) -> None:
        notifier = getattr(self.session_manager, "notify_workspace_files_changed", None)
        if notifier:
            await notifier(self.username, change)
        else:
            await self.session.workspace.update("user", "files_changed", change)

    def _ensure_not_root(self, path: str) -> None:
        if path in self.resolver.root_virtual_paths:
            raise WorkspaceFileError(403, "ROOT_IMMUTABLE", "工作区根目录不能被修改", path=path)

    def _reject_symlink(self, path: Path, virtual_path: str) -> None:
        if path.is_symlink():
            raise WorkspaceFileError(400, "SYMLINK_UNSUPPORTED", "不支持操作符号链接", path=virtual_path)

    def _check_version(self, path: Path, expected: str, virtual_path: str) -> None:
        if expected and _entry_version(path) != expected:
            raise WorkspaceFileError(
                409,
                "STALE_ENTRY",
                "文件已被其他操作修改，请刷新后重试",
                path=virtual_path,
                retryable=True,
            )

    def _ensure_paths_not_open(self, paths: list[str]) -> None:
        requested = [str(path).strip("/") for path in paths if path]
        if not requested:
            return
        for session in list(getattr(self.session_manager, "_sessions", {}).values()):
            resolver = getattr(session.workspace, "resolver", None) if session.workspace else None
            if not resolver:
                continue
            for tab in session.workspace.state.open_files:
                for requested_path in requested:
                    area = self.resolver.virtual_path_area(requested_path)
                    if area == "private" and session.user.username != self.username:
                        continue
                    if tab.path == requested_path or tab.path.startswith(requested_path + "/"):
                        raise WorkspaceFileError(
                            409,
                            "FILE_OPEN",
                            "文件正在文档标签中打开，请先关闭后再操作",
                            path=tab.path,
                        )

    def _resolve_conflict(
        self,
        virtual_path: str,
        real_path: Path,
        policy: ConflictPolicy,
    ) -> tuple[str, Path | None]:
        if not real_path.exists():
            return virtual_path, real_path
        if policy == "skip":
            return virtual_path, None
        if policy == "keep_both":
            candidate = _unique_path(real_path)
            parent_virtual = posixpath.dirname(virtual_path)
            return _join_virtual(parent_virtual, candidate.name), candidate
        if policy == "overwrite":
            return virtual_path, real_path
        raise WorkspaceFileError(409, "NAME_CONFLICT", "目标名称已存在", path=virtual_path)

    def _dedupe_nested_paths(self, paths: list[str]) -> list[str]:
        normalized = []
        for path in paths[:1000]:
            clean = str(path or "").replace("\\", "/").strip("/")
            if clean and clean not in normalized:
                normalized.append(clean)
        normalized.sort(key=lambda item: (item.count("/"), item.casefold()))
        result: list[str] = []
        for path in normalized:
            if any(path == parent or path.startswith(parent + "/") for parent in result):
                continue
            result.append(path)
        return result

    def _tree_size(self, path: Path, include_root: bool) -> int:
        if path.is_symlink():
            return 0
        if path.is_file():
            try:
                return path.stat().st_size
            except OSError:
                return 0
        total = 0
        for current, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [
                name for name in dirs
                if name != ".myagent_uploads" and not (Path(current) / name).is_symlink()
            ]
            for name in files:
                child = Path(current) / name
                if child.is_symlink():
                    continue
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
        return total

    @staticmethod
    def _tree_items(path: Path) -> int:
        if path.is_file():
            return 1
        total = 1
        for _current, dirs, files in os.walk(path, followlinks=False):
            total += len(dirs) + len(files)
        return total

    @staticmethod
    def _visible_name(name: str) -> bool:
        if name in INTERNAL_DIRS or name in IGNORED_DIR_NAMES:
            return False
        return not name.startswith(".") or name in VISIBLE_DOTFILES

    @staticmethod
    def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            temp_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def _join_virtual(parent: str, child: str) -> str:
    return f"{str(parent or '').strip('/')}/{str(child or '').strip('/')}".strip("/")


def _entry_version(path: Path) -> str:
    return _entry_version_from_stat(path.stat())


def _entry_version_from_stat(stat: os.stat_result) -> str:
    return f"{stat.st_mtime_ns:x}-{stat.st_size:x}-{getattr(stat, 'st_ino', 0):x}"


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _unique_path(path: Path) -> Path:
    stem = path.stem if path.suffix else path.name
    suffix = path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise WorkspaceFileError(409, "NAME_CONFLICT", "无法生成不冲突的名称", path=str(path))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp_path.open("wb") as file_obj:
            file_obj.write(data)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
