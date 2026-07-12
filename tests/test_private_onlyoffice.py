from myagent.interfaces.web.private_tunnel import PrivateTunnelConfig
from myagent.interfaces.web.services.document_service import DocumentService, normalize_private_onlyoffice_origin


def test_onlyoffice_tunnel_config_inherits_security_settings():
    config = PrivateTunnelConfig.from_nested_mapping(
        {
            "enabled": True,
            "listen_host": "::",
            "listen_port": 9443,
            "upstream_host": "127.0.0.1",
            "upstream_port": 8001,
            "protocol_version": 1,
            "server_key_id": "server-main",
            "server_private_key": "server-private",
            "client_psk_id": "client-main",
            "client_psk": "client-secret",
            "max_frame_size": 2048,
            "handshake_timeout_seconds": 3,
            "idle_timeout_seconds": 30,
            "onlyoffice": {
                "enabled": True,
                "listen_port": 9444,
            },
        },
        "onlyoffice",
        default_listen_port=9444,
        default_upstream_port=8081,
    )

    assert config.enabled is True
    assert config.listen_host == "::"
    assert config.listen_port == 9444
    assert config.upstream_host == "127.0.0.1"
    assert config.upstream_port == 8081
    assert config.server_key_id == "server-main"
    assert config.server_private_key == "server-private"
    assert config.client_psk_id == "client-main"
    assert config.client_psk == "client-secret"
    assert config.max_frame_size == 2048
    assert config.handshake_timeout_seconds == 3
    assert config.idle_timeout_seconds == 30


def test_private_onlyoffice_origin_accepts_loopback_http_origins():
    assert normalize_private_onlyoffice_origin("http://127.0.0.1:18081") == "http://127.0.0.1:18081"
    assert normalize_private_onlyoffice_origin("http://localhost:18081/") == "http://localhost:18081"
    assert normalize_private_onlyoffice_origin("http://[::1]:18081") == "http://[::1]:18081"


def test_private_onlyoffice_origin_rejects_non_origin_values():
    rejected = [
        "https://127.0.0.1:18081",
        "http://192.168.1.10:18081",
        "http://127.0.0.1:18081/web-apps",
        "http://127.0.0.1:18081?x=1",
        "http://user@127.0.0.1:18081",
        "http://127.0.0.1:not-a-port",
        "",
        None,
    ]

    for value in rejected:
        assert normalize_private_onlyoffice_origin(value) is None


def test_editor_config_uses_private_onlyoffice_origin_override(tmp_path):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"docx")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "http://document-server:8081",
            "myagent_internal_url": "http://host.docker.internal:8001",
            "access_token_secret": "test-secret",
        },
    )

    data = service.build_editor_config(
        "sample.docx",
        username="alice",
        session_id="session-1",
        onlyoffice_url_override="http://127.0.0.1:18081",
    )

    assert data["onlyoffice_url"] == "http://127.0.0.1:18081"
    assert data["config"]["document"]["url"].startswith("http://host.docker.internal:8001/api/documents/download")


def test_editor_config_without_override_uses_configured_onlyoffice_url(tmp_path):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"docx")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "http://document-server:8081",
            "myagent_internal_url": "http://host.docker.internal:8001",
            "access_token_secret": "test-secret",
        },
    )

    data = service.build_editor_config("sample.docx", username="alice", session_id="session-1")

    assert data["onlyoffice_url"] == "http://document-server:8081"
