import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from myagent.integrations.onlyoffice.protocol import (
    BRIDGE_REVISION,
    ErrorCode,
    OnlyOfficeProtocolError,
    PLUGIN_GUID,
    PROTOCOL_VERSION,
    validate_command,
)
from myagent.integrations.onlyoffice.runtime import (
    DocumentSnapshot,
    OnlyOfficeSessionAutomation,
    _snapshot_diff,
    _tool_definitions,
    _validate_format_properties,
)
from myagent.interfaces.web.routes import documents as documents_route
from myagent.interfaces.web.services.document_service import DocumentService


def make_runtime(**config):
    session = SimpleNamespace(id="session-test", workspace=None)
    return OnlyOfficeSessionAutomation(
        session,
        SimpleNamespace(),
        {"enabled": True, **config},
    )


def editor_snapshot(path="admin/report.docx"):
    return {
        "visible": True,
        "last_active_ms": 1,
        "bridge_revision": BRIDGE_REVISION,
        "editors": [{
            "path": path,
            "document_key": "document-key",
            "editor_session_id": "editor-session",
            "ready": True,
            "active": True,
            "writable": True,
        }],
    }


def test_attached_websocket_liveness_is_based_on_server_observation():
    runtime = make_runtime(client_ttl_seconds=1)

    async def sender(_message):
        return None

    runtime.register_client("client-1", sender)
    runtime._clients["client-1"].last_seen_ms = 0
    runtime._clients["client-1"].last_active_ms = 0
    assert not runtime.has_clients

    runtime.touch_client("client-1")
    assert runtime.has_clients


@pytest.mark.asyncio
async def test_editor_state_is_pulled_before_duplicate_open_request():
    runtime = make_runtime(open_timeout_seconds=0.5)
    sent = []

    async def sender(message):
        sent.append(message)
        if message["type"] == "onlyoffice_state_request":
            asyncio.create_task(runtime.update_client_state("client-1", editor_snapshot()))

    runtime.register_client("client-1", sender)

    client, editor = await runtime._ensure_editor("admin/report.docx")

    assert client.client_id == "client-1"
    assert editor.ready is True
    assert [message["type"] for message in sent] == ["onlyoffice_state_request"]


@pytest.mark.asyncio
async def test_open_timeout_reports_whether_editor_or_plugin_was_seen():
    runtime = make_runtime(open_timeout_seconds=0.02)

    async def sender(_message):
        return None

    runtime.register_client("client-1", sender)
    await runtime.update_client_state("client-1", {
        **editor_snapshot(),
        "editors": [{
            **editor_snapshot()["editors"][0],
            "ready": False,
            "error": "Plugin SDK 初始化超时",
        }],
    })

    with pytest.raises(OnlyOfficeProtocolError) as raised:
        await runtime._ensure_editor("admin/report.docx")

    assert raised.value.code == ErrorCode.EDITOR_OPEN_TIMEOUT
    assert raised.value.metadata["editor_reported"] is True
    assert raised.value.metadata["plugin_ready"] is False
    assert raised.value.metadata["plugin_error"] == "Plugin SDK 初始化超时"


def test_editor_config_autostarts_signed_hidden_plugin(tmp_path: Path):
    (tmp_path / "sample.docx").write_bytes(b"docx")
    service = DocumentService(str(tmp_path), {
        "enabled": True,
        "myagent_public_url": "http://app.example.test",
        "access_token_secret": "test-secret",
        "automation": {"enabled": True},
    })

    data = service.build_editor_config(
        "sample.docx",
        username="alice",
        session_id="session-1",
    )

    plugins = data["config"]["editorConfig"]["plugins"]
    assert plugins["autostart"] == [PLUGIN_GUID]
    assert data["automation"]["plugin_guid"] == PLUGIN_GUID
    config_url = plugins["pluginsData"][0]
    assert config_url == "http://app.example.test/onlyoffice-agent-plugin/config.json"
    token = plugins["options"][PLUGIN_GUID]["token"]
    manifest = service.plugin_config(token)
    assert manifest["guid"] == PLUGIN_GUID
    assert manifest["baseUrl"] == "http://app.example.test/onlyoffice-agent-plugin/"
    assert manifest["variations"][0]["url"] == "index.html"
    assert manifest["variations"][0]["type"] == "unvisible"
    assert manifest["variations"][0]["isViewer"] is True
    assert manifest["variations"][0]["EditorsSupport"] == ["word", "cell", "pdf"]

    static_manifest_path = Path(__file__).parents[1] / "web" / "onlyoffice-agent-plugin" / "config.json"
    static_manifest = json.loads(static_manifest_path.read_text(encoding="utf-8"))
    assert static_manifest["guid"] == PLUGIN_GUID
    assert static_manifest["variations"][0]["url"] == "index.html"
    assert static_manifest["variations"][0]["EditorsSupport"] == ["word", "cell", "pdf"]


def test_simplified_onlyoffice_tool_schemas_are_registered():
    definitions = {name: schema for name, _description, schema, _handler in _tool_definitions(make_runtime())}

    assert set(definitions["onlyoffice_document_read"]["properties"]) == {"path", "start_line", "end_line"}
    assert "onlyoffice_search" in definitions
    assert "onlyoffice_document_navigate_and_select" in definitions
    assert "onlyoffice_document_insert_or_replace" in definitions
    assert "onlyoffice_format" in definitions
    assert "onlyoffice_add_comment" in definitions
    # Legacy ONLYOFFICE entry points remain during the compatibility window.
    assert "onlyoffice_document_navigate" in definitions
    assert "onlyoffice_document_edit" in definitions


@pytest.mark.asyncio
async def test_logical_read_and_search_render_plain_numbered_lines(tmp_path: Path):
    runtime = make_runtime()
    document = tmp_path / "report.docx"
    document.write_bytes(b"docx")
    snapshot = DocumentSnapshot(
        path="private/report.docx",
        file_hash="abc123",
        snapshot_id="snapshot-1",
        client_id="client-1",
        editor_session_id="editor-1",
        document_key="key-1",
        lines=[
            {"text": "项目概况", "paragraph_text": "项目概况", "page": 1},
            {"text": "", "paragraph_text": "", "page": 1},
            {"text": "服务器服务器", "paragraph_text": "服务器服务器", "page": 2},
        ],
    )

    async def refresh(_path, *, preferred_client_id=""):
        return snapshot, document

    runtime._refresh_snapshot = refresh
    read = await runtime.tool_document_read("private/report.docx")
    assert read.is_error is False
    assert "1 | 项目概况\n2 |\n3 | 服务器服务器" in read.content
    assert "[paragraph]" not in read.content
    assert "L000001" not in read.content

    search = await runtime.tool_search("private/report.docx", "服务器", context_lines=1)
    payload = json.loads(search.content)
    assert payload["match_count"] == 1
    assert payload["matches"][0]["line_no"] == 3
    assert payload["matches"][0]["context"] == "2 |\n3 | 服务器服务器"


def test_format_validation_uses_public_property_names_and_conversions_are_plugin_owned():
    clean = _validate_format_properties({
        "font_family": "宋体",
        "font_size": 12,
        "background_color": "#ffff00",
        "alignment": "justify",
        "line_spacing": {"rule": "multiple", "value": 1.5},
    })

    assert clean["font_size"] == 12.0
    assert clean["background_color"] == "#FFFF00"
    assert clean["alignment"] == "justify"

    with pytest.raises(OnlyOfficeProtocolError) as raised:
        _validate_format_properties({"font_size_pt": 12})
    assert raised.value.code == ErrorCode.INVALID_ARGUMENT


def test_unified_diff_uses_logical_line_numbers_without_renumbering_unchanged_tail():
    before = [{"text": "标题"}, {"text": "旧内容"}, {"text": "结尾"}]
    after = [{"text": "标题"}, {"text": "新内容"}, {"text": "补充"}, {"text": "结尾"}]

    diff = _snapshot_diff("private/report.docx", before, after)

    assert "--- private/report.docx@before" in diff
    assert "-2 | 旧内容" in diff
    assert "+2 | 新内容" in diff
    assert "+3 | 补充" in diff
    assert "-3 | 结尾" not in diff


def test_protocol_accepts_new_docx_and_pdf_selection_commands():
    base = {
        "version": PROTOCOL_VERSION,
        "request_id": "request-1",
        "editor_session_id": "editor-1",
        "document": {"path": "private/report.docx", "document_key": "key-1"},
        "command": "document_snapshot",
        "args": {},
        "deadline_ms": int(time.time() * 1000) + 10_000,
    }
    assert validate_command(base)["command"] == "document_snapshot"
    pdf = {
        **base,
        "document": {"path": "private/report.pdf", "document_key": "key-2"},
        "command": "document_select",
    }
    assert validate_command(pdf)["document"]["path"].endswith(".pdf")


def test_plugin_bridge_uses_the_authenticated_browser_request_origin(tmp_path: Path):
    (tmp_path / "sample.docx").write_bytes(b"docx")
    service = DocumentService(str(tmp_path), {
        "enabled": True,
        "myagent_public_url": "http://configured.example.test",
        "access_token_secret": "test-secret",
        "automation": {"enabled": True},
    })

    data = service.build_editor_config(
        "sample.docx",
        username="alice",
        session_id="session-1",
        browser_origin="https://actual.example.test:8443",
    )

    plugins = data["config"]["editorConfig"]["plugins"]
    config_url = plugins["pluginsData"][0]
    assert config_url.startswith("https://actual.example.test:8443/")
    assert urlsplit(config_url).query == ""
    token = plugins["options"][PLUGIN_GUID]["token"]
    assert service.plugin_runtime(token)["host_origin"] == "https://actual.example.test:8443"


def test_long_unicode_path_is_kept_out_of_plugin_urls(tmp_path: Path):
    relative_path = "省信通OTN收口资料/（草稿）国网江苏省级OTN光传输网建设工程初步设计评审意见.docx"
    document = tmp_path / relative_path
    document.parent.mkdir(parents=True)
    document.write_bytes(b"docx")
    service = DocumentService(str(tmp_path), {
        "enabled": True,
        "myagent_public_url": "http://app.example.test",
        "access_token_secret": "test-secret",
        "automation": {"enabled": True},
    })

    data = service.build_editor_config(relative_path, username="alice", session_id="session-1")

    plugins = data["config"]["editorConfig"]["plugins"]
    assert plugins["pluginsData"] == ["http://app.example.test/onlyoffice-agent-plugin/config.json"]
    token = plugins["options"][PLUGIN_GUID]["token"]
    runtime = service.plugin_runtime(token)
    assert runtime["path"] == relative_path
    assert token not in plugins["pluginsData"][0]


def test_client_diagnostics_are_sanitized_before_logging(caplog):
    runtime = make_runtime()

    with caplog.at_level("INFO"):
        runtime.log_client_diagnostic("client-123456789", {
            "phase": "plugin_ready\nforged",
            "path": "admin/report.docx",
            "request_id": "request-123456789",
            "message": "line one\nline two",
        })

    assert "plugin_ready forged" in caplog.text
    assert "line one line two" in caplog.text
    assert "nonce" not in caplog.text


@pytest.mark.asyncio
async def test_signed_plugin_diagnostic_is_logged_without_token(tmp_path: Path, monkeypatch, caplog):
    (tmp_path / "sample.docx").write_bytes(b"docx")
    service = DocumentService(str(tmp_path), {
        "enabled": True,
        "access_token_secret": "test-secret",
        "automation": {"enabled": True},
    })
    data = service.build_editor_config("sample.docx", username="alice", session_id="session-1")
    plugins = data["config"]["editorConfig"]["plugins"]
    token = plugins["options"][PLUGIN_GUID]["token"]

    class Request:
        client = SimpleNamespace(host="127.0.0.1", port=1234)

        async def body(self):
            return b'{"phase":"sdk_timeout","message":"executeMethod unavailable"}'

    monkeypatch.setattr(documents_route, "get_document_service", lambda: service)
    with caplog.at_level("INFO"):
        result = await documents_route.plugin_diagnostic(Request(), token)

    assert result == {"ok": True}
    assert "phase=sdk_timeout" in caplog.text
    assert "executeMethod unavailable" in caplog.text
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_authenticated_onlyoffice_tool_routes_use_session_runtime(monkeypatch):
    calls = []

    class Runtime:
        enabled = True

        async def http_format(self, path, properties, *, expected_text=None):
            calls.append(("format", path, properties, expected_text))
            return {"success": True, "unified_diff": "diff"}

        async def http_add_comment(self, path, text, *, expected_text=None, author="Agent", author_user_id="agent"):
            calls.append(("comment", path, text, expected_text, author, author_user_id))
            return {"success": True}

    class Request:
        state = SimpleNamespace(user=SimpleNamespace(username="alice"))

        def __init__(self, payload):
            self.payload = payload

        async def body(self):
            return json.dumps(self.payload, ensure_ascii=False).encode()

    session = SimpleNamespace(onlyoffice_automation=Runtime())
    monkeypatch.setattr(documents_route, "_session_for_user", lambda session_id, username: session)

    formatted = await documents_route.onlyoffice_format(Request({
        "session_id": "session-1",
        "path": "private/report.docx",
        "properties": {"bold": True},
        "expected_text": "服务器",
    }))
    commented = await documents_route.onlyoffice_add_comment(Request({
        "session_id": "session-1",
        "path": "private/report.docx",
        "text": "请确认",
        "expected_text": "服务器",
        "author": "Reviewer",
        "author_user_id": "reviewer-1",
    }))

    assert formatted["success"] is True
    assert commented == {"success": True}
    assert calls == [
        ("format", "private/report.docx", {"bold": True}, "服务器"),
        ("comment", "private/report.docx", "请确认", "服务器", "Reviewer", "reviewer-1"),
    ]
