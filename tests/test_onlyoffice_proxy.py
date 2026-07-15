import pytest

from myagent.interfaces.web.routes.onlyoffice_proxy import (
    _proxy_request_headers,
    build_upstream_url,
    forwarded_host,
)
from myagent.interfaces.web.services.document_service import DocumentService


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
