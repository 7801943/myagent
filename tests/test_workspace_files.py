import io
import zipfile

import pytest
from fastapi import HTTPException

from myagent.core.workspace import WorkspaceManager
from myagent.interfaces.web.routes.workspace_files import (
    _archive_magic_label,
    _has_forbidden_archive_suffix,
    _validate_relative_path,
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
