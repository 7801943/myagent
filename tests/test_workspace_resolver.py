from unittest.mock import AsyncMock

import pytest

from myagent.core.tools import ToolInterface
from myagent.core.workspace_resolver import WorkspaceResolver
from myagent.tools.api import ToolResult


@pytest.mark.asyncio
async def test_resolver_scan_dir_keeps_nested_files_under_virtual_directory(tmp_path):
    private_root = tmp_path / "users" / "admin"
    public_root = tmp_path / "public"
    docs = private_root / "docs"
    docs.mkdir(parents=True)
    public_root.mkdir(parents=True)
    (docs / "report.txt").write_text("hello", encoding="utf-8")

    resolver = WorkspaceResolver(
        username="admin",
        group="admin",
        private_root=private_root,
        public_root=public_root,
    )

    entries = await resolver.scan_dir("admin/docs")

    assert [entry.path for entry in entries] == ["admin/docs/report.txt"]
    assert entries[0].is_dir is False


@pytest.mark.asyncio
async def test_cli_workspace_uri_is_rewritten_to_private_cwd(tmp_path):
    private_root = tmp_path / "users" / "admin"
    public_root = tmp_path / "public"
    resolver = WorkspaceResolver(
        username="admin",
        group="admin",
        private_root=private_root,
        public_root=public_root,
    )
    manager = AsyncMock()
    manager.execute = AsyncMock(return_value=ToolResult(content="ok"))
    interface = ToolInterface(manager, workspace_resolver=resolver)

    result = await interface.execute(
        "cli_execute",
        {"command": "ls -la workspace://admin/"},
        tool_call_id="tc-cli",
        skip_safety=True,
    )

    assert result.is_error is False
    kwargs = manager.execute.await_args.kwargs
    assert kwargs["command"] == f"ls -la {private_root}/"
    assert kwargs["cwd"] == str(private_root)


@pytest.mark.asyncio
async def test_cli_public_virtual_path_still_blocks_mutations(tmp_path):
    private_root = tmp_path / "users" / "admin"
    public_root = tmp_path / "public"
    resolver = WorkspaceResolver(
        username="admin",
        group="admin",
        private_root=private_root,
        public_root=public_root,
    )
    manager = AsyncMock()
    manager.execute = AsyncMock(return_value=ToolResult(content="ok"))
    interface = ToolInterface(manager, workspace_resolver=resolver)

    result = await interface.execute(
        "cli_execute",
        {"command": "touch public/new.txt"},
        tool_call_id="tc-cli",
        skip_safety=True,
    )

    assert result.is_error is True
    assert "公共目录" in result.content
    manager.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["document_read", "pdf_read", "spreadsheet_read", "document_edit", "spreadsheet_edit"],
)
async def test_format_specific_file_tools_resolve_workspace_paths(
    tmp_path, tool_name
):
    private_root = tmp_path / "users" / "admin"
    public_root = tmp_path / "public"
    private_root.mkdir(parents=True)
    public_root.mkdir(parents=True)
    (private_root / "report.txt").write_text("hello", encoding="utf-8")
    resolver = WorkspaceResolver(
        username="admin",
        group="admin",
        private_root=private_root,
        public_root=public_root,
    )
    manager = AsyncMock()
    manager.execute = AsyncMock(return_value=ToolResult(content="ok"))
    interface = ToolInterface(manager, workspace_resolver=resolver)

    result = await interface.execute(
        tool_name,
        {"path": f"{resolver.private_virtual_root}/report.txt"},
        tool_call_id=f"tc-{tool_name}",
        skip_safety=True,
    )

    assert not result.is_error
    kwargs = manager.execute.await_args.kwargs
    assert kwargs["path"] == str(private_root / "report.txt")
