import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from myagent.core.workspace import WorkspaceManager
from myagent.core.workspace_resolver import WorkspaceResolver
from myagent.interfaces.web.services.workspace_file_service import (
    WorkspaceFileError,
    WorkspaceFileService,
    WorkspaceLimits,
)


class FakeSessionManager:
    def __init__(self, session):
        self._sessions = {(session.user.username, session.id): session}
        self.changes = []

    async def notify_workspace_files_changed(self, username, change):
        self.changes.append((username, change))
        for session in self._sessions.values():
            if session.user.username == username:
                await session.workspace.update("user", "files_changed", change)


async def make_service(tmp_path, *, username="alice", group="user", limits=None):
    private_root = tmp_path / "users" / username
    public_root = tmp_path / "public"
    private_root.mkdir(parents=True)
    public_root.mkdir(parents=True, exist_ok=True)
    resolver = WorkspaceResolver(
        username=username,
        group=group,
        private_root=private_root,
        public_root=public_root,
    )
    workspace = WorkspaceManager(resolver.virtual_root, resolver=resolver)
    session = SimpleNamespace(
        id=f"session-{username}",
        user=SimpleNamespace(username=username),
        workspace=workspace,
    )
    manager = FakeSessionManager(session)
    workspace.set_on_change(lambda _state, _source: asyncio.sleep(0))
    await workspace.update("user", "set_root", {})
    service = WorkspaceFileService(session, manager, limits or WorkspaceLimits())
    return service, resolver, manager


@pytest.mark.asyncio
async def test_list_exposes_private_public_capabilities(tmp_path):
    service, resolver, _manager = await make_service(tmp_path)
    (resolver.private_root / "notes.txt").write_text("hello", encoding="utf-8")
    (resolver.public_root / "shared.txt").write_text("world", encoding="utf-8")

    private_page = await service.list_dir(resolver.private_virtual_root)
    public_page = await service.list_dir(resolver.public_virtual_root)

    private = private_page["entries"][0]
    public = public_page["entries"][0]
    assert private["capabilities"]["rename"] is True
    assert private["capabilities"]["delete"] is True
    assert private["capabilities"]["replace"] is True
    assert public["capabilities"]["rename"] is False
    assert public["capabilities"]["delete"] is False
    assert public["capabilities"]["replace"] is False
    assert public["capabilities"]["copy"] is True


@pytest.mark.asyncio
async def test_private_delete_is_recoverable_and_restore_handles_conflict(tmp_path):
    service, resolver, manager = await make_service(tmp_path)
    target = resolver.private_root / "report.txt"
    target.write_text("original", encoding="utf-8")

    deleted = await service.delete([f"{resolver.private_virtual_root}/report.txt"])
    assert target.exists() is False
    assert deleted["deleted"][0]["recoverable"] is True

    trash = await service.list_trash()
    assert len(trash["entries"]) == 1
    assert "object_path" not in trash["entries"][0]

    target.write_text("replacement", encoding="utf-8")
    restored = await service.restore_trash(
        [trash["entries"][0]["id"]],
        conflict_policy="keep_both",
    )
    restored_path = restored["restored"][0]["path"]
    assert restored_path.endswith("report (1).txt")
    assert (resolver.private_root / "report (1).txt").read_text(encoding="utf-8") == "original"
    assert manager.changes


@pytest.mark.asyncio
async def test_public_delete_requires_admin_but_upload_permission_is_retained(tmp_path):
    service, resolver, _manager = await make_service(tmp_path, group="user")
    shared = resolver.public_root / "shared.txt"
    shared.write_text("shared", encoding="utf-8")

    page = await service.list_dir(resolver.public_virtual_root)
    assert page["entries"][0]["capabilities"]["delete"] is False

    with pytest.raises(WorkspaceFileError) as exc_info:
        await service.delete([f"{resolver.public_virtual_root}/shared.txt"])
    assert exc_info.value.status_code == 403
    assert shared.exists()

    with pytest.raises(WorkspaceFileError) as overwrite_error:
        await service.init_upload(
            target_dir=resolver.public_virtual_root,
            files=[{"path": "shared.txt", "size": 3}],
            conflict_policy="overwrite",
        )
    assert overwrite_error.value.code == "OVERWRITE_FORBIDDEN"

    private_copy = resolver.private_root / "shared.txt"
    private_copy.write_text("private replacement", encoding="utf-8")
    job = service.start_copy_move(
        kind="copy",
        sources=[f"{resolver.private_virtual_root}/shared.txt"],
        target_dir=resolver.public_virtual_root,
        conflict_policy="overwrite",
    )
    for _ in range(100):
        if job.state in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    assert job.state == "failed"
    assert job.error["code"] == "OVERWRITE_FORBIDDEN"
    assert shared.read_text(encoding="utf-8") == "shared"


@pytest.mark.asyncio
async def test_resumable_chunk_upload_and_duplicate_chunk_are_idempotent(tmp_path):
    limits = WorkspaceLimits(upload_chunk_bytes=4, max_file_bytes=100, max_batch_bytes=100)
    service, resolver, _manager = await make_service(tmp_path, limits=limits)
    payload = b"abcdefghij"

    initialized = await service.init_upload(
        target_dir=resolver.private_virtual_root,
        files=[{"path": "docs/data.txt", "size": len(payload), "last_modified": 1}],
        conflict_policy="fail",
    )
    upload_id = initialized["upload_id"]
    file_id = initialized["files"][0]["id"]

    for index, start in enumerate(range(0, len(payload), 4)):
        chunk = payload[start:start + 4]
        checksum = hashlib.sha256(chunk).hexdigest()
        await service.write_upload_chunk(upload_id, file_id, index, chunk, checksum)
        if index == 0:
            await service.write_upload_chunk(upload_id, file_id, index, chunk, checksum)

    status = await service.upload_status(upload_id)
    assert status["uploaded_bytes"] == len(payload)
    completed = await service.complete_upload(upload_id)
    assert completed["uploaded"][0]["path"].endswith("docs/data.txt")
    assert (resolver.private_root / "docs" / "data.txt").read_bytes() == payload


@pytest.mark.asyncio
async def test_upload_quota_and_archive_magic_are_enforced(tmp_path):
    limits = WorkspaceLimits(
        private_quota_bytes=10,
        max_file_bytes=100,
        max_batch_bytes=100,
        upload_chunk_bytes=100,
    )
    service, resolver, _manager = await make_service(tmp_path, limits=limits)
    (resolver.private_root / "used.txt").write_bytes(b"12345678")

    with pytest.raises(WorkspaceFileError) as quota_error:
        await service.init_upload(
            target_dir=resolver.private_virtual_root,
            files=[{"path": "large.txt", "size": 4}],
            conflict_policy="fail",
        )
    assert quota_error.value.code == "QUOTA_EXCEEDED"

    service.limits = WorkspaceLimits(
        private_quota_bytes=1000,
        max_file_bytes=100,
        max_batch_bytes=100,
        upload_chunk_bytes=100,
    )
    body = b"PK\x03\x04not-an-office-document"
    initialized = await service.init_upload(
        target_dir=resolver.private_virtual_root,
        files=[{"path": "renamed.txt", "size": len(body)}],
        conflict_policy="fail",
    )
    item = initialized["files"][0]
    await service.write_upload_chunk(
        initialized["upload_id"],
        item["id"],
        0,
        body,
        hashlib.sha256(body).hexdigest(),
    )
    with pytest.raises(WorkspaceFileError) as archive_error:
        await service.complete_upload(initialized["upload_id"])
    assert archive_error.value.code == "ARCHIVE_FORBIDDEN"


@pytest.mark.asyncio
async def test_background_copy_reports_completion_and_updates_workspace(tmp_path):
    service, resolver, manager = await make_service(tmp_path)
    docs = resolver.private_root / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("a", encoding="utf-8")
    destination = resolver.private_root / "copies"
    destination.mkdir()

    job = service.start_copy_move(
        kind="copy",
        sources=[f"{resolver.private_virtual_root}/docs"],
        target_dir=f"{resolver.private_virtual_root}/copies",
        conflict_policy="fail",
    )
    for _ in range(100):
        if job.state in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)

    assert job.state == "completed", job.error
    assert (destination / "docs" / "a.txt").read_text(encoding="utf-8") == "a"
    assert manager.changes[-1][1]["changed_paths"] == [f"{resolver.private_virtual_root}/copies/docs"]
