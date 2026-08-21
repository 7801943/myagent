"""Workspace 文件上传、覆盖与删除 REST API。"""
from __future__ import annotations

import io
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.datastructures import UploadFile
from pydantic import BaseModel, Field

from myagent.interfaces.web.dependencies import get_session_manager
from myagent.interfaces.web.services.workspace_file_service import (
    WorkspaceFileError,
    WorkspaceFileService,
    WorkspaceLimits,
    workspace_jobs,
)
from myagent.utils.logging import get_logger


logger = get_logger(__name__)

router = APIRouter(prefix="/api/workspace/files", tags=["workspace-files"])

_FORBIDDEN_ARCHIVE_SUFFIXES = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst",
    ".cab", ".iso", ".jar", ".war", ".ear", ".apk", ".dmg",
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",
}

# OOXML 与 ODF 文档本质上是 zip 容器，文件头与普通 zip 同为 PK\x03\x04。
# 这里按扩展名建立白名单，使 docx/xlsx/pptx/odt 等文档不会被魔数检测当成压缩包拦截。
_ZIP_BASED_OFFICE_SUFFIXES = {
    ".docx", ".docm", ".dotx", ".dotm",
    ".xlsx", ".xlsm", ".xlsb", ".xltx", ".xltm",
    ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm", ".sldx",
    ".odt", ".ods", ".odp", ".odg", ".odf", ".ott", ".ots", ".otp", ".otg",
    ".vsdx", ".vssx", ".vstx", ".vstm", ".vdx",
}

_ARCHIVE_MAGIC_LABELS: tuple[tuple[bytes, str], ...] = (
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

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PreflightRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    target_dir: str = ""
    paths: list[str] = Field(default_factory=list)


class DeleteRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    paths: list[str] = Field(default_factory=list)
    recursive: bool = False
    expected_versions: dict[str, str] = Field(default_factory=dict)


class RenameRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    new_name: str = Field(..., min_length=1)
    expected_version: str = ""


class FolderRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    parent: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)


class CopyMoveRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    sources: list[str] = Field(default_factory=list)
    target_dir: str = Field(..., min_length=1)
    conflict_policy: Literal["fail", "skip", "overwrite", "keep_both"] = "fail"
    expected_versions: dict[str, str] = Field(default_factory=dict)


class TrashRestoreRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    trash_ids: list[str] = Field(default_factory=list)
    target_dir: str = ""
    conflict_policy: Literal["fail", "skip", "overwrite", "keep_both"] = "fail"


class TrashPurgeRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    trash_ids: list[str] | None = None


class UploadFileSpec(BaseModel):
    path: str = Field(..., min_length=1)
    size: int = Field(..., ge=0)
    last_modified: int = 0


class UploadInitRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    target_dir: str = Field(..., min_length=1)
    files: list[UploadFileSpec] = Field(default_factory=list)
    conflict_policy: Literal["fail", "skip", "overwrite", "keep_both"] = "fail"


class UploadCompleteRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class DownloadJobRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    paths: list[str] = Field(default_factory=list)


@dataclass
class PathValidation:
    raw_path: str
    relative_path: str = ""
    error: str = ""


@router.get("/list")
async def list_workspace_directory(
    request: Request,
    session_id: str = Query(...),
    path: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    sort_by: Literal["name", "size", "modified", "type"] = Query("name"),
    order: Literal["asc", "desc"] = Query("asc"),
):
    """List one workspace directory with permissions and quota information."""
    service = _file_service(request, session_id)
    try:
        return await service.list_dir(path, offset=offset, limit=limit, sort_by=sort_by, order=order)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.get("/details")
async def workspace_path_details(
    request: Request,
    session_id: str = Query(...),
    path: str = Query(...),
):
    service = _file_service(request, session_id)
    try:
        return await service.details(path)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.get("/search")
async def search_workspace_paths(
    request: Request,
    session_id: str = Query(...),
    query: str = Query(..., min_length=1),
    areas: str = Query("private,public"),
    limit: int = Query(200, ge=1, le=500),
):
    service = _file_service(request, session_id)
    selected_areas = [value.strip() for value in areas.split(",") if value.strip() in {"private", "public"}]
    try:
        return await service.search(query, areas=selected_areas, limit=limit)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.post("/folders")
async def create_workspace_folder(payload: FolderRequest, request: Request):
    service = _file_service(request, payload.session_id)
    try:
        return await service.create_folder(payload.parent, payload.name)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.post("/copy")
async def copy_workspace_paths(payload: CopyMoveRequest, request: Request):
    service = _file_service(request, payload.session_id)
    try:
        job = service.start_copy_move(
            kind="copy",
            sources=payload.sources,
            target_dir=payload.target_dir,
            conflict_policy=payload.conflict_policy,
            expected_versions=payload.expected_versions,
        )
        return {"ok": True, "job": job.to_dict()}
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.post("/move")
async def move_workspace_paths(payload: CopyMoveRequest, request: Request):
    service = _file_service(request, payload.session_id)
    try:
        job = service.start_copy_move(
            kind="move",
            sources=payload.sources,
            target_dir=payload.target_dir,
            conflict_policy=payload.conflict_policy,
            expected_versions=payload.expected_versions,
        )
        return {"ok": True, "job": job.to_dict()}
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.get("/trash")
async def list_workspace_trash(request: Request, session_id: str = Query(...)):
    service = _file_service(request, session_id)
    try:
        return await service.list_trash()
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.post("/trash/restore")
async def restore_workspace_trash(payload: TrashRestoreRequest, request: Request):
    service = _file_service(request, payload.session_id)
    try:
        return await service.restore_trash(
            payload.trash_ids,
            target_dir=payload.target_dir,
            conflict_policy=payload.conflict_policy,
        )
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.post("/trash/purge")
async def purge_workspace_trash(payload: TrashPurgeRequest, request: Request):
    service = _file_service(request, payload.session_id)
    try:
        return await service.purge_trash(payload.trash_ids)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.post("/uploads")
async def initialize_workspace_upload(payload: UploadInitRequest, request: Request):
    service = _file_service(request, payload.session_id)
    try:
        return await service.init_upload(
            target_dir=payload.target_dir,
            files=[item.model_dump() for item in payload.files],
            conflict_policy=payload.conflict_policy,
        )
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.get("/uploads/{upload_id}")
async def get_workspace_upload(upload_id: str, request: Request, session_id: str = Query(...)):
    service = _file_service(request, session_id)
    try:
        return await service.upload_status(upload_id)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.put("/uploads/{upload_id}/chunks/{file_id}/{chunk_index}")
async def upload_workspace_chunk(
    upload_id: str,
    file_id: str,
    chunk_index: int,
    request: Request,
    session_id: str = Query(...),
):
    service = _file_service(request, session_id)
    body = await request.body()
    checksum = request.headers.get("X-Chunk-SHA256", "")
    try:
        return await service.write_upload_chunk(upload_id, file_id, chunk_index, body, checksum)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.post("/uploads/{upload_id}/complete")
async def complete_workspace_upload(
    upload_id: str,
    payload: UploadCompleteRequest,
    request: Request,
):
    service = _file_service(request, payload.session_id)
    try:
        return await service.complete_upload(upload_id)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.delete("/uploads/{upload_id}")
async def cancel_workspace_upload(upload_id: str, request: Request, session_id: str = Query(...)):
    service = _file_service(request, session_id)
    try:
        return await service.cancel_upload(upload_id)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.post("/downloads")
async def create_workspace_download(payload: DownloadJobRequest, request: Request):
    service = _file_service(request, payload.session_id)
    try:
        job = service.start_archive(payload.paths)
        return {"ok": True, "job": job.to_dict()}
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.get("/jobs/{job_id}")
async def get_workspace_job(job_id: str, request: Request):
    username = _request_username(request)
    try:
        return {"job": workspace_jobs.get(job_id, username).to_dict()}
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.delete("/jobs/{job_id}")
async def cancel_workspace_job(job_id: str, request: Request):
    username = _request_username(request)
    try:
        return {"job": workspace_jobs.cancel(job_id, username).to_dict()}
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.get("/jobs/{job_id}/result")
async def download_workspace_job_result(job_id: str, request: Request):
    username = _request_username(request)
    try:
        job = workspace_jobs.get(job_id, username)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)
    if job.state != "completed" or not job.result_path:
        raise HTTPException(status_code=409, detail={
            "code": "JOB_NOT_READY",
            "message": "下载任务尚未完成",
            "path": "",
            "details": None,
            "retryable": True,
        })
    filename = str((job.result or {}).get("filename") or "workspace-download.zip")
    return FileResponse(job.result_path, filename=filename, media_type="application/zip")


@router.post("/preflight")
async def preflight_upload(payload: PreflightRequest, request: Request):
    """上传前校验路径、压缩扩展名与同名冲突。"""
    session = _session_for_request(request, payload.session_id)
    target_dir = _normalize_workspace_target_dir(payload.target_dir, session)
    _validate_target_directory(session, target_dir)

    conflicts: list[dict] = []
    rejected: list[dict] = []
    seen_targets: set[str] = set()

    for raw_path in payload.paths[:1000]:
        validation = _validate_upload_relative_path(raw_path)
        if validation.error:
            rejected.append({"path": raw_path, "reason": validation.error})
            continue
        if _has_forbidden_archive_suffix(validation.relative_path):
            rejected.append({"path": raw_path, "reason": "不允许上传压缩或归档文件"})
            continue
        target_rel = _join_workspace_relative(target_dir, validation.relative_path)
        if target_rel in seen_targets:
            rejected.append({"path": raw_path, "reason": "上传列表中存在重复目标路径"})
            continue
        seen_targets.add(target_rel)
        target_path = _resolve_workspace_path(session, target_rel, operation="upload")
        if target_path.exists():
            if target_path.is_dir():
                rejected.append({"path": raw_path, "target_path": target_rel, "reason": "目标路径是目录"})
            else:
                conflicts.append({"path": raw_path, "target_path": target_rel})

    return {
        "ok": not conflicts and not rejected,
        "conflicts": conflicts,
        "rejected": rejected,
    }


@router.post("/upload")
async def upload_files(request: Request):
    """上传本地文件或文件夹到当前会话 workspace。"""
    try:
        form = await request.form()
    except (AssertionError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail="服务端缺少 python-multipart，无法处理文件上传") from exc

    session_id = str(form.get("session_id") or "")
    target_dir = str(form.get("target_dir") or "")
    overwrite = str(form.get("overwrite") or "false").lower() in {"1", "true", "yes", "on"}
    files = [item for item in form.getlist("files[]") if isinstance(item, UploadFile)]
    paths = [str(item) for item in form.getlist("paths[]")]

    session = _session_for_request(request, session_id)
    file_service = _file_service(request, session_id)
    clean_target_dir = _normalize_workspace_target_dir(target_dir, session)
    _validate_target_directory(session, clean_target_dir)

    if len(files) != len(paths):
        raise HTTPException(status_code=400, detail="files 与 paths 数量不一致")
    if not files:
        raise HTTPException(status_code=400, detail="未选择上传文件")
    if len(files) > file_service.limits.max_batch_files:
        raise HTTPException(status_code=413, detail={
            "code": "TOO_MANY_FILES",
            "message": f"单批最多上传 {file_service.limits.max_batch_files} 个文件",
        })
    known_sizes = [int(getattr(upload, "size", 0) or 0) for upload in files]
    if any(size > file_service.limits.max_file_bytes for size in known_sizes):
        raise HTTPException(status_code=413, detail={"code": "FILE_TOO_LARGE", "message": "文件超过单文件大小限制"})
    if sum(known_sizes) > file_service.limits.max_batch_bytes:
        raise HTTPException(status_code=413, detail={"code": "BATCH_TOO_LARGE", "message": "上传批次超过大小限制"})
    resolver = getattr(session.workspace, "resolver", None)
    if resolver and resolver.virtual_path_area(clean_target_dir) == "private":
        quota = await file_service.quota()
        if sum(known_sizes) > quota["available_bytes"]:
            raise HTTPException(status_code=413, detail={
                "code": "QUOTA_EXCEEDED",
                "message": "private 工作区可用容量不足",
                "details": quota,
            })

    uploaded: list[dict] = []
    rejected: list[dict] = []
    changed_paths: list[str] = []
    seen_targets: set[str] = set()

    for index, upload in enumerate(files[:1000]):
        raw_path = paths[index]
        validation = _validate_upload_relative_path(raw_path)
        if validation.error:
            rejected.append({"path": raw_path, "reason": validation.error})
            await upload.close()
            continue
        if _has_forbidden_archive_suffix(validation.relative_path):
            rejected.append({"path": raw_path, "reason": "不允许上传压缩或归档文件"})
            await upload.close()
            continue

        target_rel = _join_workspace_relative(clean_target_dir, validation.relative_path)
        if target_rel in seen_targets:
            rejected.append({"path": raw_path, "reason": "上传列表中存在重复目标路径"})
            await upload.close()
            continue
        seen_targets.add(target_rel)

        target_path = _resolve_workspace_path(session, target_rel, operation="upload")
        if target_path.exists():
            if target_path.is_dir():
                rejected.append({"path": raw_path, "target_path": target_rel, "reason": "目标路径是目录"})
                await upload.close()
                continue
            if not overwrite:
                rejected.append({"path": raw_path, "target_path": target_rel, "reason": "目标文件已存在"})
                await upload.close()
                continue
            # Public upload permission allows creating new objects, not
            # replacing existing ones. Overwrite requires write permission.
            _resolve_workspace_path(session, target_rel, operation="write", must_exist=True)

        try:
            bytes_written = await _save_upload_atomically(
                upload,
                target_path,
                max_bytes=file_service.limits.max_file_bytes,
            )
        except HTTPException as exc:
            rejected.append({"path": raw_path, "target_path": target_rel, "reason": str(exc.detail)})
            continue
        finally:
            await upload.close()

        uploaded.append({"path": target_rel, "size": bytes_written})
        changed_paths.append(target_rel)

    if changed_paths:
        await session.workspace.update("user", "files_changed", {"changed_paths": changed_paths})

    status = 200 if not rejected else (207 if uploaded else 400)
    if status == 400:
        raise HTTPException(status_code=400, detail={"uploaded": uploaded, "rejected": rejected})
    return {"ok": not rejected, "uploaded": uploaded, "rejected": rejected}


@router.post("/rename")
async def rename_workspace_path(payload: RenameRequest, request: Request):
    """重命名 workspace 内的文件或目录，不支持跨目录移动。"""
    service = _file_service(request, payload.session_id)
    try:
        return await service.rename(payload.path, payload.new_name, payload.expected_version)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


@router.get("/download")
async def download_workspace_path(
    request: Request,
    session_id: str = Query(..., description="会话 ID"),
    path: str = Query(..., description="工作区相对路径"),
):
    """下载 workspace 内的文件；目录会打包为 zip。"""
    session = _session_for_request(request, session_id)
    rel_path = _validate_relative_path(path, allow_empty=False)
    target_path = _resolve_workspace_path(session, rel_path, operation="read", must_exist=True)
    if not target_path.exists():
        raise HTTPException(status_code=404, detail="路径不存在")

    if target_path.is_file():
        return FileResponse(str(target_path), filename=target_path.name)

    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail="路径不是文件或目录")

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for child in sorted(target_path.rglob("*")):
            if not child.is_file():
                continue
            arcname = Path(target_path.name) / child.relative_to(target_path)
            zf.write(child, arcname.as_posix())
    archive.seek(0)

    filename = f"{target_path.name or 'workspace'}.zip"
    encoded = quote(filename)
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


@router.post("/delete")
async def delete_workspace_paths(payload: DeleteRequest, request: Request):
    """private 路径移入回收站；public 管理员删除仍为永久删除。"""
    service = _file_service(request, payload.session_id)
    try:
        return await service.delete(payload.paths, expected_versions=payload.expected_versions)
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


def _session_for_request(request: Request, session_id: str):
    token_info = getattr(request.state, "user", None)
    username = getattr(token_info, "username", "")
    if not username:
        raise HTTPException(status_code=401, detail="未认证")
    session = get_session_manager().get_session(session_id, user=username)
    if not session or not session.workspace:
        raise HTTPException(status_code=404, detail="会话工作空间不存在")
    if session.user.username != username:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return session


def _request_username(request: Request) -> str:
    token_info = getattr(request.state, "user", None)
    username = getattr(token_info, "username", "")
    if not username:
        raise HTTPException(status_code=401, detail="未认证")
    return username


def _file_service(request: Request, session_id: str) -> WorkspaceFileService:
    session = _session_for_request(request, session_id)
    manager = get_session_manager()
    raw_workspace = getattr(manager, "_raw", {}).get("workspace", {})
    raw_limits = raw_workspace.get("file_manager", {}) if isinstance(raw_workspace, dict) else {}
    try:
        return WorkspaceFileService(session, manager, WorkspaceLimits.from_mapping(raw_limits))
    except WorkspaceFileError as exc:
        _raise_workspace_error(exc)


def _raise_workspace_error(exc: WorkspaceFileError):
    raise HTTPException(status_code=exc.status_code, detail=exc.to_detail()) from exc


def _normalize_workspace_target_dir(target_dir: str, session=None) -> str:
    clean = _validate_relative_path(target_dir, allow_empty=True)
    if clean:
        return clean
    resolver = getattr(session.workspace, "resolver", None) if session and session.workspace else None
    return resolver.private_virtual_root if resolver else ""


def _resolve_workspace_path(session, relative_path: str, *, operation: str, must_exist: bool = False) -> Path:
    resolver = getattr(session.workspace, "resolver", None) if session.workspace else None
    if resolver:
        try:
            resolved = resolver.resolve(
                relative_path,
                operation=operation,
                actor=resolver.actor_for_user(),
                must_exist=must_exist,
            )
            return resolved.real_path
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="路径不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    root = _workspace_root(session)
    return _resolve_under_root(root, relative_path)


def _workspace_root(session) -> Path:
    root_path = session.workspace.root_path if session.workspace else ""
    if not root_path:
        raise HTTPException(status_code=404, detail="会话工作空间不存在")
    root = Path(root_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=404, detail="工作空间目录不存在")
    return root


def _validate_upload_relative_path(path: str) -> PathValidation:
    try:
        rel_path = _validate_relative_path(path, allow_empty=False)
    except HTTPException as exc:
        return PathValidation(raw_path=path, error=str(exc.detail))
    if rel_path.endswith("/"):
        return PathValidation(raw_path=path, error="文件路径不能为空")
    return PathValidation(raw_path=path, relative_path=rel_path)


def _validate_entry_name(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="名称不能为空")
    if "\x00" in raw or "/" in raw or "\\" in raw:
        raise HTTPException(status_code=400, detail="名称包含非法字符")
    if raw in {".", ".."}:
        raise HTTPException(status_code=400, detail="名称不能是 . 或 ..")
    return raw


def _validate_relative_path(path: str, *, allow_empty: bool) -> str:
    raw = str(path or "").replace("\\", "/")
    if "\x00" in raw:
        raise HTTPException(status_code=400, detail="路径包含非法字符")
    if not raw.strip():
        if allow_empty:
            return ""
        raise HTTPException(status_code=400, detail="路径不能为空")
    if raw.startswith("/") or _WINDOWS_DRIVE_RE.match(raw):
        raise HTTPException(status_code=400, detail="路径必须是工作区相对路径")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="路径不能包含空片段、. 或 ..")
    normalized = posixpath.normpath(raw).replace("\\", "/")
    if normalized == "." or normalized.startswith("../") or normalized == "..":
        raise HTTPException(status_code=400, detail="路径不能越出工作区")
    return normalized.strip("/")


def _join_workspace_relative(target_dir: str, rel_path: str) -> str:
    return f"{target_dir}/{rel_path}" if target_dir else rel_path


def _validate_target_directory(session, target_dir: str) -> None:
    if not target_dir:
        return
    path = _resolve_workspace_path(session, target_dir, operation="upload")
    if path.exists() and not path.is_dir():
        raise HTTPException(status_code=400, detail="上传目标路径不是目录")


def _resolve_under_root(root: Path, relative_path: str) -> Path:
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=403, detail="路径不在工作区内")
    return path


def _has_forbidden_archive_suffix(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(suffix) for suffix in _FORBIDDEN_ARCHIVE_SUFFIXES)


def _has_zip_based_office_suffix(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(suffix) for suffix in _ZIP_BASED_OFFICE_SUFFIXES)


def _archive_magic_label(header: bytes) -> str:
    for magic, label in _ARCHIVE_MAGIC_LABELS:
        if header.startswith(magic):
            return label
    if len(header) >= 263 and header[257:263] in {b"ustar\x00", b"ustar "}:
        return "tar"
    return ""


async def _save_upload_atomically(upload: UploadFile, target_path: Path, max_bytes: int | None = None) -> int:
    try:
        if target_path.parent.exists() and not target_path.parent.is_dir():
            raise HTTPException(status_code=400, detail="目标父路径不是目录")
        target_path.parent.mkdir(parents=True, exist_ok=True)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法创建目标目录: {exc}") from exc

    header = await upload.read(4096)
    magic_label = _archive_magic_label(header)
    # docx/xlsx/pptx 等 OOXML 及 ODF 文档本身就是 zip 容器，按扩展名白名单放行，
    # 其余命中压缩包魔数（含改名的 .txt 等）一律拦截。
    if magic_label and not (magic_label == "zip" and _has_zip_based_office_suffix(target_path.name)):
        raise HTTPException(status_code=415, detail=f"不允许上传压缩或归档文件: {magic_label}")

    fd, tmp_name = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".tmp", dir=str(target_path.parent))
    bytes_written = 0
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            if header:
                tmp_file.write(header)
                bytes_written += len(header)
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                tmp_file.write(chunk)
                bytes_written += len(chunk)
                if max_bytes is not None and bytes_written > max_bytes:
                    raise HTTPException(status_code=413, detail="文件超过单文件大小限制")
        os.replace(tmp_name, target_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return bytes_written
