from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from myagent.core.events import EventBus, ToolEnd
from myagent.core.models import UserContext
from myagent.core.session.session import Session
from myagent.core.workspace import WorkspaceManager
from myagent.core.workspace_resolver import WorkspaceResolver
from myagent.interfaces.web.routes.workspace_files import (
    _archive_magic_label,
    _has_forbidden_archive_suffix,
    _has_zip_based_office_suffix,
    _validate_relative_path,
)
from myagent.tools.api import ToolResult


class FakeToolInterface:
    def list_schemas(self):
        return []

    def get_cli_policy_state(self):
        return {
            "active_policy": "whitelist",
            "available_policies": ["whitelist"],
            "mode": "whitelist",
        }

    def set_cli_policy(self, policy_name: str):
        return self.get_cli_policy_state()


def make_workspace_session(*, workspace_root=None, workspace_resolver=None) -> Session:
    harness = SimpleNamespace(
        events=EventBus(),
        tool_interface=FakeToolInterface(),
        router=SimpleNamespace(providers=[], selected_provider_key=""),
        tool_manager=None,
    )
    return Session(
        session_id="workspace-refresh-session",
        harness=harness,
        user=UserContext(user_id="user-1", username="admin"),
        workspace_root=str(workspace_root) if workspace_root else None,
        workspace_resolver=workspace_resolver,
    )


def test_workspace_upload_path_validation_rejects_unsafe_paths():
    unsafe = ["", "/tmp/a.txt", "../a.txt", "a/../b.txt", "a//b.txt", "C:/tmp/a.txt", "bad\x00name.txt"]
    for path in unsafe:
        with pytest.raises(HTTPException):
            _validate_relative_path(path, allow_empty=False)


def test_workspace_upload_path_validation_normalizes_safe_paths():
    assert _validate_relative_path("docs/report.txt", allow_empty=False) == "docs/report.txt"
    assert _validate_relative_path("", allow_empty=True) == ""


def test_archive_suffix_and_magic_are_rejected():
    assert _has_forbidden_archive_suffix("docs/a.zip")
    assert _has_forbidden_archive_suffix("docs/a.tar.gz")
    assert _has_forbidden_archive_suffix("docs/a.JAR")
    assert not _has_forbidden_archive_suffix("docs/a.txt")
    assert _archive_magic_label(b"PK\x03\x04anything") == "zip"
    assert _archive_magic_label(b"7z\xbc\xaf\x27\x1canything") == "7z"
    assert _archive_magic_label((b"x" * 257) + b"ustar\x00") == "tar"


def test_zip_based_office_documents_are_allowlisted():
    """docx/xlsx/pptx/odt 等 OOXML 与 ODF 文档本质是 zip，需按扩展名放行。"""
    assert _has_zip_based_office_suffix("docs/report.docx")
    assert _has_zip_based_office_suffix("docs/report.DOCX")
    assert _has_zip_based_office_suffix("sheets/data.xlsx")
    assert _has_zip_based_office_suffix("decks/slide.pptx")
    assert _has_zip_based_office_suffix("notes/letter.odt")
    assert _has_zip_based_office_suffix("diagram.vsdx")
    # 普通文件、被禁的 jar/zip 扩展名不应被白名单放行
    assert not _has_zip_based_office_suffix("docs/a.txt")
    assert not _has_zip_based_office_suffix("docs/a.zip")
    assert not _has_zip_based_office_suffix("lib/a.jar")
    # 魔数仍为 zip：白名单内放行，白名单外（如改名 .txt）依然拦截
    assert _archive_magic_label(b"PK\x03\x04" + b"\x00" * 100) == "zip"


@pytest.mark.asyncio
async def test_workspace_file_list_text_includes_visible_directories_and_permissions(tmp_path):
    private_root = tmp_path / "users" / "admin"
    public_root = tmp_path / "public"
    (private_root / "reports").mkdir(parents=True)
    public_root.mkdir(parents=True)
    resolver = WorkspaceResolver(
        username="admin",
        group="admin",
        private_root=private_root,
        public_root=public_root,
    )
    manager = WorkspaceManager(resolver.virtual_root, resolver=resolver)

    await manager.update("user", "set_root", {})

    text = manager.get_file_list_text()

    assert "admin/" in text
    assert "admin/reports/ [目录] [私有可写]" in text
    assert f"{resolver.public_virtual_root}/ [目录] [公共只读]" in text


@pytest.mark.asyncio
async def test_files_changed_increments_changed_open_tab_revision(tmp_path):
    target = tmp_path / "report.txt"
    target.write_text("v1")
    manager = WorkspaceManager(str(tmp_path))
    await manager.update("user", "set_root", {"root_path": str(tmp_path)})
    await manager.update("user", "open_file", {"path": "report.txt"})

    assert manager.state.open_files[0].revision == 0
    target.write_text("v2")
    await manager.update("user", "files_changed", {"changed_paths": ["report.txt"]})

    assert manager.state.open_files[0].revision == 1
    assert manager.state.files[0].size == 2


@pytest.mark.asyncio
async def test_file_edit_tool_end_marks_changed_open_docx_for_onlyoffice_refresh(tmp_path):
    private_root = tmp_path / "admin"
    public_root = tmp_path / "public"
    private_root.mkdir()
    public_root.mkdir()
    target = private_root / "report.docx"
    target.write_bytes(b"v1")
    resolver = WorkspaceResolver(
        username="admin",
        group="admin",
        private_root=private_root,
        public_root=public_root,
    )
    session = make_workspace_session(workspace_resolver=resolver)
    await session.workspace.update("user", "set_root", {})
    await session.workspace.update("user", "scan_dir", {"path": resolver.private_virtual_root})
    await session.workspace.update("user", "open_file", {"path": f"{resolver.private_virtual_root}/report.docx"})

    assert session.workspace.state.open_files[0].revision == 0

    target.write_bytes(b"v2")
    await session._on_tool_end(ToolEnd(
        tool_name="file_edit",
        result=ToolResult(content="ok", metadata={"path": str(target.resolve())}),
    ))

    assert [tab.path for tab in session.workspace.state.open_files] == [f"{resolver.private_virtual_root}/report.docx"]
    assert session.workspace.state.open_files[0].revision == 1
    assert session.workspace.state.active_file_index == 0
    info = next(file for file in session.workspace.state.files if file.path == f"{resolver.private_virtual_root}/report.docx")
    assert info.size == 2


@pytest.mark.asyncio
async def test_file_write_tool_end_opens_written_file_without_double_revision(tmp_path):
    target = tmp_path / "new.docx"
    target.write_bytes(b"v1")
    session = make_workspace_session(workspace_root=tmp_path)
    await session.workspace.update("user", "set_root", {"root_path": str(tmp_path)})

    await session._on_tool_end(ToolEnd(
        tool_name="file_write",
        result=ToolResult(content="ok", metadata={"path": str(target.resolve())}),
    ))

    assert [tab.path for tab in session.workspace.state.open_files] == ["new.docx"]
    assert session.workspace.state.open_files[0].revision == 0
    assert session.workspace.state.active_file_index == 0


@pytest.mark.asyncio
async def test_files_changed_closes_deleted_file_and_directory_tabs(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    manager = WorkspaceManager(str(tmp_path))
    await manager.update("user", "set_root", {"root_path": str(tmp_path)})
    await manager.update("user", "scan_dir", {"path": "docs"})
    await manager.update("user", "open_file", {"path": "docs/a.txt"})
    await manager.update("user", "open_file", {"path": "b.txt"})

    assert [tab.path for tab in manager.state.open_files] == ["docs/a.txt", "b.txt"]
    (docs / "a.txt").unlink()
    docs.rmdir()
    await manager.update("user", "files_changed", {"deleted_paths": ["docs"]})

    assert [tab.path for tab in manager.state.open_files] == ["b.txt"]
    assert manager.get_active_file_path() == "b.txt"


@pytest.mark.asyncio
async def test_files_changed_renames_open_file_and_expanded_directory(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("a")
    manager = WorkspaceManager(str(tmp_path))
    await manager.update("user", "set_root", {"root_path": str(tmp_path)})
    await manager.update("user", "scan_dir", {"path": "docs"})
    await manager.update("user", "open_file", {"path": "docs/a.txt"})

    docs.rename(tmp_path / "notes")
    await manager.update("user", "files_changed", {"renamed_paths": [{"from": "docs", "to": "notes"}]})

    assert manager.state.open_files[0].path == "notes/a.txt"
    assert manager.state.open_files[0].revision == 1
    assert "notes" in manager.state.expanded_dirs
    assert "docs" not in manager.state.expanded_dirs


def test_document_service_supports_markdown_as_word_type():
    from myagent.interfaces.web.services.document_service import DEFAULT_SUPPORTED_EXTENSIONS, DocumentService

    assert ".md" in DEFAULT_SUPPORTED_EXTENSIONS
    assert ".markdown" in DEFAULT_SUPPORTED_EXTENSIONS
    assert DocumentService._document_type(".md") == "word"
    assert DocumentService._document_type(".markdown") == "word"
