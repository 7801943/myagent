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
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.datastructures import UploadFile
from pydantic import BaseModel, Field

from myagent.interfaces.web.dependencies import get_session_manager
from myagent.utils.logging import get_logger


logger = get_logger(__name__)

router = APIRouter(prefix="/api/workspace/files", tags=["workspace-files"])

_FORBIDDEN_ARCHIVE_SUFFIXES = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst",
    ".cab", ".iso", ".jar", ".war", ".ear", ".apk", ".dmg",
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",
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


class RenameRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    new_name: str = Field(..., min_length=1)


@dataclass
class PathValidation:
    raw_path: str
    relative_path: str = ""
    error: str = ""


@router.post("/preflight")
async def preflight_upload(payload: PreflightRequest):
    """上传前校验路径、压缩扩展名与同名冲突。"""
    session = _session_for_request(payload.session_id)
    root = _workspace_root(session)
    target_dir = _validate_relative_path(payload.target_dir, allow_empty=True)
    _validate_target_directory(root, target_dir)

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
        target_path = _resolve_under_root(root, target_rel)
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

    session = _session_for_request(session_id)
    root = _workspace_root(session)
    clean_target_dir = _validate_relative_path(target_dir, allow_empty=True)
    _validate_target_directory(root, clean_target_dir)

    if len(files) != len(paths):
        raise HTTPException(status_code=400, detail="files 与 paths 数量不一致")
    if not files:
        raise HTTPException(status_code=400, detail="未选择上传文件")

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

        target_path = _resolve_under_root(root, target_rel)
        if target_path.exists():
            if target_path.is_dir():
                rejected.append({"path": raw_path, "target_path": target_rel, "reason": "目标路径是目录"})
                await upload.close()
                continue
            if not overwrite:
                rejected.append({"path": raw_path, "target_path": target_rel, "reason": "目标文件已存在"})
                await upload.close()
                continue

        try:
            bytes_written = await _save_upload_atomically(upload, target_path)
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
async def rename_workspace_path(payload: RenameRequest):
    """重命名 workspace 内的文件或目录，不支持跨目录移动。"""
    session = _session_for_request(payload.session_id)
    root = _workspace_root(session)
    rel_path = _validate_relative_path(payload.path, allow_empty=False)
    new_name = _validate_entry_name(payload.new_name)

    source_path = _resolve_under_root(root, rel_path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="路径不存在")

    target_rel = _join_workspace_relative(posixpath.dirname(rel_path), new_name)
    target_path = _resolve_under_root(root, target_rel)
    if target_path.exists():
        raise HTTPException(status_code=409, detail="目标名称已存在")
    if _has_forbidden_archive_suffix(target_rel):
        raise HTTPException(status_code=415, detail="不允许重命名为压缩或归档文件")

    try:
        os.replace(source_path, target_path)
    except OSError as exc:
        logger.warning("Workspace rename failed: from=%s to=%s error=%s", rel_path, target_rel, exc)
        raise HTTPException(status_code=400, detail=f"重命名失败: {exc}") from exc

    await session.workspace.update(
        "user",
        "files_changed",
        {"renamed_paths": [{"from": rel_path, "to": target_rel}], "changed_paths": [target_rel]},
    )
    return {"ok": True, "from": rel_path, "to": target_rel}


@router.get("/download")
async def download_workspace_path(
    session_id: str = Query(..., description="会话 ID"),
    path: str = Query(..., description="工作区相对路径"),
):
    """下载 workspace 内的文件；目录会打包为 zip。"""
    session = _session_for_request(session_id)
    root = _workspace_root(session)
    rel_path = _validate_relative_path(path, allow_empty=False)
    target_path = _resolve_under_root(root, rel_path)
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
async def delete_workspace_paths(payload: DeleteRequest):
    """删除 workspace 内的文件或目录。"""
    session = _session_for_request(payload.session_id)
    root = _workspace_root(session)

    deleted: list[dict] = []
    rejected: list[dict] = []
    deleted_paths: list[str] = []

    for raw_path in payload.paths[:200]:
        try:
            rel_path = _validate_relative_path(raw_path, allow_empty=False)
        except HTTPException as exc:
            rejected.append({"path": raw_path, "reason": str(exc.detail)})
            continue

        target_path = _resolve_under_root(root, rel_path)
        if not target_path.exists():
            rejected.append({"path": raw_path, "reason": "路径不存在"})
            continue
        try:
            if target_path.is_dir():
                if not payload.recursive:
                    rejected.append({"path": raw_path, "reason": "删除目录需要 recursive=true"})
                    continue
                shutil.rmtree(target_path)
                kind = "dir"
            else:
                target_path.unlink()
                kind = "file"
        except OSError as exc:
            logger.warning("Workspace delete failed: path=%s error=%s", rel_path, exc)
            rejected.append({"path": raw_path, "reason": f"删除失败: {exc}"})
            continue

        deleted.append({"path": rel_path, "kind": kind})
        deleted_paths.append(rel_path)

    if deleted_paths:
        await session.workspace.update("user", "files_changed", {"deleted_paths": deleted_paths})

    if rejected and not deleted:
        raise HTTPException(status_code=400, detail={"deleted": deleted, "rejected": rejected})
    return {"ok": not rejected, "deleted": deleted, "rejected": rejected}


def _session_for_request(session_id: str):
    session = get_session_manager().get_session(session_id)
    if not session or not session.workspace:
        raise HTTPException(status_code=404, detail="会话工作空间不存在")
    return session


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


def _validate_target_directory(root: Path, target_dir: str) -> None:
    if not target_dir:
        return
    path = _resolve_under_root(root, target_dir)
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


def _archive_magic_label(header: bytes) -> str:
    for magic, label in _ARCHIVE_MAGIC_LABELS:
        if header.startswith(magic):
            return label
    if len(header) >= 263 and header[257:263] in {b"ustar\x00", b"ustar "}:
        return "tar"
    return ""


async def _save_upload_atomically(upload: UploadFile, target_path: Path) -> int:
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
    if magic_label:
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
        os.replace(tmp_name, target_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return bytes_written
