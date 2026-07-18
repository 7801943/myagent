import asyncio
import base64
import json
import socket
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
    assert config.listen_hosts == ["::"]
    assert config.listen_port == 9443
    assert config.upstream_host == "127.0.0.1"
    assert config.upstream_port == 8001
    assert config.upstream_source_host == "127.0.0.2"
    assert config.server_key_id == "server-main"
    assert config.server_private_key == "server-private"
    assert config.client_psk_id == "client-main"
    assert config.client_psk == "client-secret-value"
    assert config.max_frame_size == 2048
    assert config.handshake_timeout_seconds == 3
    assert config.idle_timeout_seconds == 30


def test_private_tunnel_config_prefers_dual_stack_listen_hosts():
    config = PrivateTunnelConfig.from_mapping(
        {
            "listen_host": "127.0.0.1",
            "listen_hosts": ["0.0.0.0", "::", "0.0.0.0"],
            "upstream_source_host": "127.0.0.2",
        }
    )

    assert config.listen_host == "0.0.0.0"
    assert config.listen_hosts == ["0.0.0.0", "::"]
    assert config.upstream_source_host == "127.0.0.2"


def test_private_tunnel_config_defaults_to_dual_stack():
    config = PrivateTunnelConfig.from_mapping({})

    assert config.listen_hosts == ["0.0.0.0", "::"]


def test_disabled_private_tunnel_does_not_open_listener():
    server = PrivateTunnelServer(PrivateTunnelConfig(enabled=False))

    asyncio.run(server.start())

    assert server._server is None


def test_private_tunnel_opens_and_closes_ipv4_and_ipv6_listeners():
    async def exercise():
        crypto = _crypto()
        private_key = crypto.x25519.X25519PrivateKey.generate()
        private_raw = private_key.private_bytes(
            encoding=crypto.serialization.Encoding.Raw,
            format=crypto.serialization.PrivateFormat.Raw,
            encryption_algorithm=crypto.serialization.NoEncryption(),
        )
        encoded_key = base64.urlsafe_b64encode(private_raw).decode("ascii").rstrip("=")

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        server = PrivateTunnelServer(PrivateTunnelConfig(
            enabled=True,
            listen_hosts=["0.0.0.0", "::"],
            listen_port=port,
            server_private_key=encoded_key,
            client_psk="0123456789abcdef",
        ))
        await server.start()
        assert server._server is not None
        families = {sock.family for sock in server._server.sockets or []}
        assert socket.AF_INET in families
        assert socket.AF_INET6 in families

        for host in ("127.0.0.1", "::1"):
            _reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()

        await asyncio.sleep(0)
        await server.stop()
        assert server._server is None

    asyncio.run(exercise())


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
            assert fake_app.state.encrypted_transport_sources == {"127.0.0.2"}

    asyncio.run(exercise())

    assert events == [("start", 9443), ("stop", 9443)]


def test_websocket_encryption_marker_uses_peer_address_not_headers():
    fake_app = SimpleNamespace(state=SimpleNamespace(
        encrypted_transport_sources={"127.0.0.2"},
    ))
    encrypted_ws = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.2"),
        headers={},
    )
    mapped_ws = SimpleNamespace(
        client=SimpleNamespace(host="::ffff:127.0.0.2"),
        headers={},
    )
    spoofed_plain_ws = SimpleNamespace(
        client=SimpleNamespace(host="192.168.0.84"),
        headers={"x-myagent-encrypted-transport": "true"},
    )

    assert web_app._is_encrypted_websocket(encrypted_ws, fake_app) is True
    assert web_app._is_encrypted_websocket(mapped_ws, fake_app) is True
    assert web_app._is_encrypted_websocket(spoofed_plain_ws, fake_app) is False


def test_private_tunnel_source_address_is_visible_to_upstream():
    async def exercise():
        peers = []

        async def handle(_reader, writer):
            peers.append(writer.get_extra_info("peername"))
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]
        _reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            port,
            local_addr=("127.0.0.2", 0),
        )
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0)
        upstream.close()
        await upstream.wait_closed()
        return peers

    peers = asyncio.run(exercise())
    assert peers
    assert peers[0][0] == "127.0.0.2"


def test_websocket_connected_message_reports_encrypted_transport():
    sent_messages = []

    class _FakeWebSocket:
        state = SimpleNamespace(user=None)

        async def accept(self):
            return None

        async def send_text(self, payload):
            sent_messages.append(json.loads(payload))

        async def iter_text(self):
            if False:
                yield ""

    class _FakeClientHandle:
        def detach(self):
            return None

    class _FakeSession:
        id = "session-1"
        _bridge = SimpleNamespace(has_clients=False)
        workspace = None
        data = SimpleNamespace(model_dump=lambda: {})

        def attach_client(self, _callback):
            return _FakeClientHandle()

        def serialize_messages(self):
            return []

    class _FakeSessionManager:
        context_window_size = 200000

        async def join_session(self, _user, config_override=None):
            return _FakeSession()

    async def exercise():
        handler = web_app.WebSocketHandler(
            _FakeWebSocket(),
            _FakeSessionManager(),
            encrypted_transport=True,
        )
        await handler.run()

    asyncio.run(exercise())
    connected = next(message for message in sent_messages if message.get("type") == "connected")
    assert connected["encrypted_transport"] is True


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
