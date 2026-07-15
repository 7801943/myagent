import asyncio
from types import SimpleNamespace

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


def test_app_lifespan_starts_main_private_tunnel(monkeypatch):
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
            }
        },
    )

    async def exercise():
        fake_app = SimpleNamespace(state=SimpleNamespace(config_path="test-config.yaml"))
        async with web_app.lifespan(fake_app):
            assert fake_app.state.private_tunnel.config.listen_port == 9443

    asyncio.run(exercise())

    assert events == [("start", 9443), ("stop", 9443)]


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
