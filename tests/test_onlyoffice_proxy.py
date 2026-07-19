from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace

import pytest

from myagent.interfaces.web.routes import documents as documents_route
from myagent.interfaces.web.services import document_service as document_service_module
from myagent.interfaces.web.routes.onlyoffice_proxy import (
    _proxy_request_headers,
    build_upstream_url,
    forwarded_host,
)
from myagent.interfaces.web.services.document_service import DocumentService


def _editor_config_token(data):
    callback_url = data["config"]["editorConfig"]["callbackUrl"]
    return parse_qs(urlsplit(callback_url).query)["token"][0]


def test_editor_config_defaults_to_same_origin_onlyoffice_proxy(tmp_path):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"docx")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "access_token_secret": "test-secret",
        },
    )

    data = service.build_editor_config("sample.docx", username="alice", session_id="session-1")

    assert service.config.onlyoffice_url == "/onlyoffice"
    assert service.config.onlyoffice_internal_url == "http://localhost:8081"
    assert data["onlyoffice_url"] == "/onlyoffice"


@pytest.mark.asyncio
async def test_editor_config_binds_trusted_tunnel_host_without_changing_browser_proxy_path(tmp_path, monkeypatch):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"docx")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
            "access_token_secret": "test-secret",
        },
    )
    session = SimpleNamespace(
        workspace=SimpleNamespace(root_path=str(tmp_path), resolver=None),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(user=SimpleNamespace(username="alice", group="user")),
        client=SimpleNamespace(host="127.0.0.2", port=42641),
        headers={"host": "127.0.0.1:43171"},
        app=SimpleNamespace(state=SimpleNamespace(encrypted_transport_sources={"127.0.0.2"})),
    )
    monkeypatch.setattr(documents_route, "get_document_service", lambda: service)
    monkeypatch.setattr(documents_route, "_session_for_user", lambda *_args, **_kwargs: session)

    data = await documents_route.editor_config(request, "sample.docx", "edit", "session-1")
    token_payload = service.verify_access_token(_editor_config_token(data), "sample.docx")

    assert data["onlyoffice_url"] == "/onlyoffice"
    assert token_payload["onlyoffice_proxy_origin"] == "http://127.0.0.1:43171"


@pytest.mark.asyncio
async def test_editor_config_plain_request_does_not_bind_forged_loopback_host(tmp_path, monkeypatch):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"docx")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
            "access_token_secret": "test-secret",
        },
    )
    session = SimpleNamespace(
        workspace=SimpleNamespace(root_path=str(tmp_path), resolver=None),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(user=SimpleNamespace(username="alice", group="user")),
        client=SimpleNamespace(host="192.168.0.84", port=42641),
        headers={
            "host": "127.0.0.1:43171",
            "x-myagent-private-onlyoffice-origin": "http://127.0.0.1:43171",
        },
        app=SimpleNamespace(state=SimpleNamespace(encrypted_transport_sources={"127.0.0.2"})),
    )
    monkeypatch.setattr(documents_route, "get_document_service", lambda: service)
    monkeypatch.setattr(documents_route, "_session_for_user", lambda *_args, **_kwargs: session)

    data = await documents_route.editor_config(request, "sample.docx", "edit", "session-1")
    token_payload = service.verify_access_token(_editor_config_token(data), "sample.docx")

    assert data["onlyoffice_url"] == "/onlyoffice"
    assert "onlyoffice_proxy_origin" not in token_payload


def test_legacy_absolute_onlyoffice_url_remains_direct_by_default(tmp_path):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"docx")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "http://document-server:8081",
            "access_token_secret": "test-secret",
        },
    )

    data = service.build_editor_config("sample.docx", username="alice", session_id="session-1")

    assert service.config.onlyoffice_url == "http://document-server:8081"
    assert service.config.onlyoffice_internal_url == "http://document-server:8081"
    assert data["onlyoffice_url"] == "http://document-server:8081"


def test_onlyoffice_callback_download_url_rewrites_proxy_url_to_internal_upstream(tmp_path):
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
            "onlyoffice_internal_url": "http://document-server:80",
            "myagent_public_url": "https://app.example.com",
            "myagent_internal_url": "http://myagent:8000",
            "access_token_secret": "test-secret",
        },
    )

    assert (
        service.rewrite_onlyoffice_download_url("https://app.example.com/onlyoffice/cache/files/abc.docx?token=1")
        == "http://document-server:80/cache/files/abc.docx?token=1"
    )
    assert (
        service.rewrite_onlyoffice_download_url("http://myagent:8000/onlyoffice/cache/files/abc.docx")
        == "http://document-server:80/cache/files/abc.docx"
    )
    assert (
        service.rewrite_onlyoffice_download_url("http://document-server:80/cache/files/abc.docx")
        == "http://document-server:80/cache/files/abc.docx"
    )


def test_onlyoffice_callback_download_url_rejects_untrusted_hosts(tmp_path):
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
            "onlyoffice_internal_url": "http://document-server:80",
            "myagent_public_url": "https://app.example.com",
            "access_token_secret": "test-secret",
        },
    )

    with pytest.raises(ValueError):
        service.rewrite_onlyoffice_download_url("https://evil.example.com/cache/files/abc.docx")


def test_private_local_proxy_origin_is_bound_to_callback_token(tmp_path):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"docx")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
            "onlyoffice_internal_url": "http://document-server:80",
            "myagent_internal_url": "http://myagent:8000",
            "access_token_secret": "test-secret",
        },
    )

    data = service.build_editor_config(
        "sample.docx",
        username="alice",
        session_id="session-1",
        onlyoffice_proxy_origin="http://127.0.0.1:43171",
    )

    assert data["onlyoffice_url"] == "/onlyoffice"
    callback_url = data["config"]["editorConfig"]["callbackUrl"]
    token = parse_qs(urlsplit(callback_url).query)["token"][0]
    token_payload = service.verify_access_token(token, "sample.docx")
    assert token_payload["onlyoffice_proxy_origin"] == "http://127.0.0.1:43171"
    assert (
        service.rewrite_onlyoffice_download_url(
            "http://127.0.0.1:43171/onlyoffice/cache/files/updated.docx?token=one-time",
            trusted_proxy_origin=token_payload["onlyoffice_proxy_origin"],
        )
        == "http://document-server:80/cache/files/updated.docx?token=one-time"
    )


def test_private_local_proxy_origin_rejects_non_loopback_override(tmp_path):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"docx")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
            "onlyoffice_internal_url": "http://document-server:80",
            "access_token_secret": "test-secret",
        },
    )

    data = service.build_editor_config(
        "sample.docx",
        username="alice",
        onlyoffice_proxy_origin="http://attacker.example.com:8081",
    )

    assert data["onlyoffice_url"] == "/onlyoffice"
    with pytest.raises(ValueError):
        service.rewrite_onlyoffice_download_url(
            "http://attacker.example.com:8081/cache/files/updated.docx",
            trusted_proxy_origin="http://attacker.example.com:8081",
        )


@pytest.mark.parametrize(
    "download_url",
    [
        "http://127.0.0.1:43172/onlyoffice/cache/files/updated.docx",
        "http://127.0.0.1:43171/onlyoffice/web-apps/apps/api.js",
        "http://127.0.0.1:43171/onlyoffice/cache/other/updated.docx",
        "https://127.0.0.1:43171/onlyoffice/cache/files/updated.docx",
    ],
)
def test_private_callback_download_url_rejects_mismatch_or_non_cache_path(tmp_path, download_url):
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
            "onlyoffice_internal_url": "http://document-server:80",
            "access_token_secret": "test-secret",
        },
    )

    with pytest.raises(ValueError):
        service.rewrite_onlyoffice_download_url(
            download_url,
            trusted_proxy_origin="http://127.0.0.1:43171",
        )


def test_private_callback_download_url_requires_token_bound_origin(tmp_path):
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
            "onlyoffice_internal_url": "http://document-server:80",
            "access_token_secret": "test-secret",
        },
    )

    with pytest.raises(ValueError):
        service.rewrite_onlyoffice_download_url(
            "http://127.0.0.1:43171/onlyoffice/cache/files/updated.docx",
        )


@pytest.mark.asyncio
async def test_status_6_callback_downloads_and_atomically_replaces_document(tmp_path, monkeypatch):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"old-document")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
            "onlyoffice_internal_url": "http://document-server:80",
            "access_token_secret": "test-secret",
        },
    )
    token = service._create_access_token(
        "sample.docx",
        "alice",
        "edit",
        onlyoffice_proxy_origin="http://127.0.0.1:43171",
    )
    requested_urls = []

    class _Response:
        status_code = 200
        content = b"new-document"
        is_redirect = False

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *, timeout, follow_redirects):
            assert timeout == 60.0
            assert follow_redirects is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            requested_urls.append(url)
            return _Response()

    monkeypatch.setattr(document_service_module.httpx, "AsyncClient", _Client)

    result = await service.handle_callback(
        "sample.docx",
        token,
        {
            "status": 6,
            "url": "http://127.0.0.1:43171/onlyoffice/cache/files/data/key/output.docx?token=one-time",
        },
    )

    assert result == {"error": 0}
    assert requested_urls == [
        "http://document-server:80/cache/files/data/key/output.docx?token=one-time",
    ]
    assert document.read_bytes() == b"new-document"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_status_6_callback_rejects_download_redirect_without_replacing_document(tmp_path, monkeypatch):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"old-document")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
            "onlyoffice_internal_url": "http://document-server:80",
            "access_token_secret": "test-secret",
        },
    )
    token = service._create_access_token(
        "sample.docx",
        "alice",
        "edit",
        onlyoffice_proxy_origin="http://127.0.0.1:43171",
    )

    class _RedirectResponse:
        status_code = 302
        content = b""
        is_redirect = True

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *, timeout, follow_redirects):
            assert follow_redirects is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return _RedirectResponse()

    monkeypatch.setattr(document_service_module.httpx, "AsyncClient", _Client)

    result = await service.handle_callback(
        "sample.docx",
        token,
        {
            "status": 6,
            "url": "http://127.0.0.1:43171/onlyoffice/cache/files/data/key/output.docx",
        },
    )

    assert result == {"error": 1}
    assert document.read_bytes() == b"old-document"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_proxy_url_builder_preserves_base_path_and_websocket_scheme():
    assert (
        build_upstream_url("http://document-server:80", "/web-apps/apps/api/documents/api.js", "v=1")
        == "http://document-server:80/web-apps/apps/api/documents/api.js?v=1"
    )
    assert (
        build_upstream_url("https://document-server/base", "/doc/123/c", "x=1", websocket=True)
        == "wss://document-server/base/doc/123/c?x=1"
    )


def test_proxy_forwarded_headers_use_virtual_onlyoffice_path():
    headers = _proxy_request_headers(
        {
            "host": "app.example.com",
            "connection": "keep-alive",
            "range": "bytes=0-10",
            "x-forwarded-for": "10.0.0.1",
            "x-forwarded-prefix": "/upstream-prefix",
        },
        client_host="10.0.0.2",
        request_scheme="https",
        request_host="app.example.com",
        browser_url="/onlyoffice",
    )

    assert "host" not in {name.lower() for name in headers}
    assert "connection" not in {name.lower() for name in headers}
    assert headers["range"] == "bytes=0-10"
    assert headers["X-Forwarded-For"] == "10.0.0.1, 10.0.0.2"
    assert headers["X-Forwarded-Proto"] == "https"
    assert headers["X-Forwarded-Host"] == "app.example.com/onlyoffice"
    assert "x-forwarded-prefix" not in {name.lower() for name in headers}


def test_forwarded_host_can_use_absolute_browser_url():
    assert forwarded_host("ignored.example.com", "https://docs.example.com/onlyoffice") == "docs.example.com/onlyoffice"
