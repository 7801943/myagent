import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.exceptions import InvalidTag

from myagent.interfaces.web.private_tunnel import (
    FRAME_DATA,
    PrivateTunnelConfig,
    PrivateTunnelServer,
    _TunnelSession,
    _crypto,
    _read_encrypted_frame,
    _write_encrypted_frame,
)
from myagent.interfaces.web.services.document_service import (
    DocumentService,
    normalize_private_onlyoffice_origin,
)
from myagent.interfaces.web import app as web_app
from myagent.interfaces.web.routes import onlyoffice_proxy


class _BufferWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None


def test_private_tunnel_config_from_mapping_preserves_client_protocol_settings():
    config = PrivateTunnelConfig.from_mapping(
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
            "client_psk": "client-secret-value",
            "max_frame_size": 2048,
            "handshake_timeout_seconds": 3,
            "idle_timeout_seconds": 30,
        }
    )

    assert config.enabled is True
    assert config.listen_host == "::"
    assert config.listen_port == 9443
    assert config.upstream_host == "127.0.0.1"
    assert config.upstream_port == 8001
    assert config.server_key_id == "server-main"
    assert config.server_private_key == "server-private"
    assert config.client_psk_id == "client-main"
    assert config.client_psk == "client-secret-value"
    assert config.max_frame_size == 2048
    assert config.handshake_timeout_seconds == 3
    assert config.idle_timeout_seconds == 30


def test_onlyoffice_tunnel_inherits_existing_client_security_settings():
    config = PrivateTunnelConfig.from_nested_mapping(
        {
            "listen_host": "::",
            "server_key_id": "server-main",
            "server_private_key": "server-private",
            "client_psk_id": "client-main",
            "client_psk": "client-secret-value",
            "onlyoffice": {
                "enabled": True,
                "listen_port": 9444,
                "upstream_port": 8081,
            },
        },
        "onlyoffice",
        default_listen_port=9444,
        default_upstream_port=8081,
    )

    assert config.enabled is True
    assert config.listen_host == "::"
    assert config.listen_port == 9444
    assert config.upstream_port == 8081
    assert config.server_key_id == "server-main"
    assert config.server_private_key == "server-private"
    assert config.client_psk_id == "client-main"
    assert config.client_psk == "client-secret-value"


def test_private_onlyoffice_origin_is_limited_to_loopback_http():
    assert normalize_private_onlyoffice_origin("http://127.0.0.1:18081") == "http://127.0.0.1:18081"
    assert normalize_private_onlyoffice_origin("http://[::1]:18081") == "http://[::1]:18081"
    assert normalize_private_onlyoffice_origin("https://127.0.0.1:18081") is None
    assert normalize_private_onlyoffice_origin("http://192.168.1.10:18081") is None
    assert normalize_private_onlyoffice_origin("http://127.0.0.1:18081/web-apps") is None


def test_editor_config_can_target_legacy_private_onlyoffice_local_proxy(tmp_path):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"docx")
    service = DocumentService(
        str(tmp_path),
        {
            "enabled": True,
            "onlyoffice_url": "/onlyoffice",
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
    token = parse_qs(urlsplit(data["config"]["document"]["url"]).query)["token"][0]
    token_payload = service.verify_access_token(token, "sample.docx")
    assert token_payload["onlyoffice_proxy_origin"] == "http://127.0.0.1:18081"

    rewritten = service.rewrite_onlyoffice_download_url(
        "http://127.0.0.1:18081/cache/files/updated.docx?token=one-time",
        trusted_proxy_base=token_payload["onlyoffice_proxy_origin"],
    )
    assert rewritten == "http://localhost:8081/cache/files/updated.docx?token=one-time"


def test_editor_config_rejects_untrusted_onlyoffice_override(tmp_path):
    document = tmp_path / "sample.docx"
    document.write_bytes(b"docx")
    service = DocumentService(str(tmp_path), {"enabled": True, "access_token_secret": "test-secret"})

    data = service.build_editor_config(
        "sample.docx",
        username="alice",
        onlyoffice_url_override="http://attacker.example.com:8081",
    )

    assert data["onlyoffice_url"] == "/onlyoffice"


def test_disabled_private_tunnel_does_not_open_listener():
    server = PrivateTunnelServer(PrivateTunnelConfig(enabled=False))

    asyncio.run(server.start())

    assert server._server is None


def test_web_app_keeps_onlyoffice_proxy_routes():
    included_routers = [route.original_router for route in web_app.app.routes if hasattr(route, "original_router")]
    route_paths = {route.path for route in onlyoffice_proxy.router.routes}

    assert any(router is onlyoffice_proxy.router for router in included_routers)
    assert "/onlyoffice" in route_paths
    assert "/onlyoffice/{path:path}" in route_paths


def test_app_lifespan_starts_main_and_legacy_onlyoffice_tunnels(monkeypatch):
    events = []

    class _FakeTunnelServer:
        def __init__(self, config):
            self.config = config

        async def start(self):
            events.append(("start", self.config.listen_port))

        async def stop(self):
            events.append(("stop", self.config.listen_port))

    async def _no_op():
        return None

    monkeypatch.setattr(web_app, "init_services", lambda config_path: None)
    monkeypatch.setattr(web_app, "startup", _no_op)
    monkeypatch.setattr(web_app, "shutdown", _no_op)
    monkeypatch.setattr(web_app, "PrivateTunnelServer", _FakeTunnelServer)
    monkeypatch.setattr(
        web_app,
        "load_yaml_config",
        lambda _path: {
            "private_transport": {
                "enabled": True,
                "listen_port": 9443,
                "server_private_key": "server-private",
                "client_psk": "client-secret-value",
                "onlyoffice": {"enabled": True, "listen_port": 9444},
            }
        },
    )

    async def exercise():
        fake_app = SimpleNamespace(state=SimpleNamespace(config_path="test-config.yaml"))
        async with web_app.lifespan(fake_app):
            assert fake_app.state.private_tunnel.config.listen_port == 9443
            assert fake_app.state.onlyoffice_private_tunnel.config.listen_port == 9444
            assert len(fake_app.state.private_tunnels) == 2

    asyncio.run(exercise())

    assert events == [("start", 9443), ("start", 9444), ("stop", 9444), ("stop", 9443)]


def test_encrypted_frame_round_trip():
    async def exercise() -> bytes:
        crypto = _crypto()
        key = bytes(range(32))
        nonce_prefix = b"test"
        writer_session = _TunnelSession(
            c2s_aead=crypto.ChaCha20Poly1305(bytes(reversed(key))),
            s2c_aead=crypto.ChaCha20Poly1305(key),
            c2s_nonce_prefix=b"read",
            s2c_nonce_prefix=nonce_prefix,
            max_frame_size=1024,
        )
        reader_session = _TunnelSession(
            c2s_aead=crypto.ChaCha20Poly1305(key),
            s2c_aead=crypto.ChaCha20Poly1305(bytes(reversed(key))),
            c2s_nonce_prefix=nonce_prefix,
            s2c_nonce_prefix=b"send",
            max_frame_size=1024,
        )
        writer = _BufferWriter()
        await _write_encrypted_frame(writer, writer_session, FRAME_DATA, b"onlyoffice-over-main-tunnel")

        reader = asyncio.StreamReader()
        reader.feed_data(bytes(writer.data))
        reader.feed_eof()
        frame_type, plaintext = await _read_encrypted_frame(reader, reader_session)
        assert frame_type == FRAME_DATA
        return plaintext

    assert asyncio.run(exercise()) == b"onlyoffice-over-main-tunnel"


def test_encrypted_frame_rejects_tampering():
    async def exercise() -> None:
        crypto = _crypto()
        key = b"k" * 32
        nonce_prefix = b"test"
        writer_session = _TunnelSession(
            c2s_aead=crypto.ChaCha20Poly1305(key),
            s2c_aead=crypto.ChaCha20Poly1305(key),
            c2s_nonce_prefix=nonce_prefix,
            s2c_nonce_prefix=nonce_prefix,
            max_frame_size=1024,
        )
        reader_session = _TunnelSession(
            c2s_aead=crypto.ChaCha20Poly1305(key),
            s2c_aead=crypto.ChaCha20Poly1305(key),
            c2s_nonce_prefix=nonce_prefix,
            s2c_nonce_prefix=nonce_prefix,
            max_frame_size=1024,
        )
        writer = _BufferWriter()
        await _write_encrypted_frame(writer, writer_session, FRAME_DATA, b"protected")
        writer.data[-1] ^= 1

        reader = asyncio.StreamReader()
        reader.feed_data(bytes(writer.data))
        reader.feed_eof()
        with pytest.raises(InvalidTag):
            await _read_encrypted_frame(reader, reader_session)

    asyncio.run(exercise())
