"""Private encrypted TCP tunnel for the web interface."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import struct
from dataclasses import dataclass, field
from typing import Any

from myagent.utils.logging import get_logger


logger = get_logger(__name__)

MAGIC = b"MAGT"
PROTOCOL_VERSION = 1
SUITE_ID = 1
SUITE_NAME = "X25519-HKDF-SHA256-CHACHA20POLY1305"

FRAME_DATA = 0x01
FRAME_PING = 0x02
FRAME_PONG = 0x03
FRAME_CLOSE = 0x04
FRAME_ERROR = 0x05

MAX_HANDSHAKE_MESSAGE = 64 * 1024
DEFAULT_MAX_FRAME_SIZE = 1024 * 1024
DEFAULT_READ_CHUNK_SIZE = 64 * 1024


@dataclass
class PrivateTunnelConfig:
    enabled: bool = False
    listen_host: str = "0.0.0.0"
    listen_hosts: list[str] = field(default_factory=lambda: ["0.0.0.0", "::"])
    listen_port: int = 9443
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8000
    upstream_source_host: str = "127.0.0.2"
    protocol_version: int = PROTOCOL_VERSION
    server_key_id: str = "server-v1"
    server_private_key: str = ""
    client_psk_id: str = "client-v1"
    client_psk: str = ""
    max_frame_size: int = DEFAULT_MAX_FRAME_SIZE
    handshake_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 300.0

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "PrivateTunnelConfig":
        data = raw or {}
        raw_listen_hosts = data.get("listen_hosts")
        if isinstance(raw_listen_hosts, (list, tuple)):
            listen_hosts = _dedupe_hosts(raw_listen_hosts)
            if not listen_hosts:
                raise ValueError("private_transport.listen_hosts must not be empty")
        elif "listen_host" in data:
            listen_hosts = [str(data.get("listen_host") or "0.0.0.0")]
        else:
            listen_hosts = ["0.0.0.0", "::"]
        return cls(
            enabled=bool(data.get("enabled", False)),
            listen_host=listen_hosts[0],
            listen_hosts=listen_hosts,
            listen_port=int(data.get("listen_port") or 9443),
            upstream_host=str(data.get("upstream_host") or "127.0.0.1"),
            upstream_port=int(data.get("upstream_port") or 8000),
            upstream_source_host=str(data.get("upstream_source_host") or "127.0.0.2"),
            protocol_version=int(data.get("protocol_version") or PROTOCOL_VERSION),
            server_key_id=str(data.get("server_key_id") or "server-v1"),
            server_private_key=str(data.get("server_private_key") or ""),
            client_psk_id=str(data.get("client_psk_id") or "client-v1"),
            client_psk=str(data.get("client_psk") or ""),
            max_frame_size=int(data.get("max_frame_size") or DEFAULT_MAX_FRAME_SIZE),
            handshake_timeout_seconds=float(data.get("handshake_timeout_seconds") or 10.0),
            idle_timeout_seconds=float(data.get("idle_timeout_seconds") or 300.0),
        )


@dataclass
class _TunnelSession:
    c2s_aead: Any
    s2c_aead: Any
    c2s_nonce_prefix: bytes
    s2c_nonce_prefix: bytes
    max_frame_size: int
    recv_seq: int = 0
    send_seq: int = 0
    send_lock: asyncio.Lock | None = None

    def __post_init__(self) -> None:
        if self.send_lock is None:
            self.send_lock = asyncio.Lock()


class PrivateTunnelError(Exception):
    """Raised for protocol and crypto failures."""


class PrivateTunnelServer:
    """Encrypted TCP tunnel that forwards decrypted bytes to the existing web server."""

    def __init__(self, config: PrivateTunnelConfig):
        self.config = config
        self._server: asyncio.AbstractServer | None = None
        self._server_private_key: Any | None = None
        self._client_psk: bytes = b""

    async def start(self) -> None:
        if not self.config.enabled:
            return
        self._server_private_key = _load_x25519_private_key(self.config.server_private_key)
        self._client_psk = _decode_secret(self.config.client_psk, name="client_psk")
        if len(self._client_psk) < 16:
            raise ValueError("private_transport.client_psk must contain at least 16 bytes")
        if self.config.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported private tunnel protocol_version: {self.config.protocol_version}")
        _validate_upstream_source_host(self.config.upstream_source_host)

        self._server = await asyncio.start_server(
            self._handle_client,
            self.config.listen_hosts,
            self.config.listen_port,
        )
        sockets = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        logger.info(
            "Private tunnel listening on %s -> %s:%s",
            sockets,
            self.config.upstream_host,
            self.config.upstream_port,
        )

    async def stop(self) -> None:
        if not self._server:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("Private tunnel stopped")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        conn_id = secrets.token_hex(4)
        upstream_writer: asyncio.StreamWriter | None = None
        tasks: set[asyncio.Task] = set()
        logger.info(
            "Private tunnel accepted: conn=%s peer=%s expected_server_key_id=%s expected_client_psk_id=%s",
            conn_id,
            peer,
            self.config.server_key_id,
            self.config.client_psk_id,
        )
        try:
            session = await asyncio.wait_for(
                self._handshake(reader, writer, conn_id=conn_id, peer=peer),
                timeout=self.config.handshake_timeout_seconds,
            )
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.config.upstream_host,
                self.config.upstream_port,
                local_addr=(self.config.upstream_source_host, 0),
            )
            logger.info(
                "Private tunnel upstream connected: conn=%s peer=%s upstream=%s:%s",
                conn_id,
                peer,
                self.config.upstream_host,
                self.config.upstream_port,
            )
            tasks = {
                asyncio.create_task(
                    self._client_to_upstream(reader, writer, upstream_writer, session, conn_id=conn_id, peer=peer)
                ),
                asyncio.create_task(
                    self._upstream_to_client(upstream_reader, writer, session, conn_id=conn_id, peer=peer)
                ),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            logger.info("Private tunnel disconnected: conn=%s peer=%s", conn_id, peer)
        except asyncio.TimeoutError:
            logger.warning(
                "Private tunnel timeout: conn=%s peer=%s handshake_timeout=%ss idle_timeout=%ss",
                conn_id,
                peer,
                self.config.handshake_timeout_seconds,
                self.config.idle_timeout_seconds,
            )
        except PrivateTunnelError as exc:
            logger.warning("Private tunnel rejected: conn=%s peer=%s error=%s", conn_id, peer, exc)
        except Exception:
            logger.exception("Private tunnel connection failed: conn=%s peer=%s", conn_id, peer)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if upstream_writer:
                _close_writer(upstream_writer)
            _close_writer(writer)
            logger.info("Private tunnel closed: conn=%s peer=%s", conn_id, peer)

    async def _handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        conn_id: str,
        peer: Any,
    ) -> _TunnelSession:
        if not self._server_private_key:
            raise PrivateTunnelError("server key is not initialized")
        crypto = _crypto()

        client_payload = await _read_plain_message(reader, label="client_hello", conn_id=conn_id, peer=peer)
        logger.info(
            "Private tunnel client_hello received: conn=%s peer=%s payload_len=%s payload_sha256=%s preview=%s",
            conn_id,
            peer,
            len(client_payload),
            hashlib.sha256(client_payload).hexdigest()[:16],
            _payload_preview(client_payload),
        )
        client_hello = _json_loads(client_payload)
        logger.info(
            "Private tunnel client_hello parsed: conn=%s peer=%s type=%s version=%s suite_id=%s "
            "server_key_id=%s client_psk_id=%s client_nonce_len=%s client_ephemeral_len=%s",
            conn_id,
            peer,
            client_hello.get("type"),
            client_hello.get("version"),
            client_hello.get("suite_id"),
            _safe_log_value(client_hello.get("server_key_id")),
            _safe_log_value(client_hello.get("client_psk_id")),
            _b64_field_len(client_hello, "client_nonce"),
            _b64_field_len(client_hello, "client_ephemeral_public"),
        )
        if client_hello.get("type") != "client_hello":
            raise PrivateTunnelError("expected client_hello")
        if client_hello.get("magic") != MAGIC.decode():
            raise PrivateTunnelError("bad magic")
        if int(client_hello.get("version") or 0) != PROTOCOL_VERSION:
            raise PrivateTunnelError("unsupported version")
        if int(client_hello.get("suite_id") or 0) != SUITE_ID:
            raise PrivateTunnelError("unsupported suite")
        if str(client_hello.get("server_key_id") or "") != self.config.server_key_id:
            raise PrivateTunnelError("unknown server_key_id")
        if str(client_hello.get("client_psk_id") or "") != self.config.client_psk_id:
            raise PrivateTunnelError("unknown client_psk_id")

        client_ephemeral_public = crypto.x25519.X25519PublicKey.from_public_bytes(
            _b64decode_field(client_hello, "client_ephemeral_public", expected_len=32)
        )
        client_nonce = _b64decode_field(client_hello, "client_nonce", expected_len=16)
        server_ephemeral_private = crypto.x25519.X25519PrivateKey.generate()
        server_ephemeral_public = server_ephemeral_private.public_key().public_bytes(
            encoding=crypto.serialization.Encoding.Raw,
            format=crypto.serialization.PublicFormat.Raw,
        )
        server_nonce = secrets.token_bytes(16)

        server_core = _json_dumps({
            "type": "server_hello",
            "magic": MAGIC.decode(),
            "version": PROTOCOL_VERSION,
            "suite_id": SUITE_ID,
            "suite": SUITE_NAME,
            "server_key_id": self.config.server_key_id,
            "client_psk_id": self.config.client_psk_id,
            "client_nonce": _b64encode(client_nonce),
            "server_nonce": _b64encode(server_nonce),
            "server_ephemeral_public": _b64encode(server_ephemeral_public),
        })

        dh_static = self._server_private_key.exchange(client_ephemeral_public)
        dh_ephemeral = server_ephemeral_private.exchange(client_ephemeral_public)
        handshake_secret = _hkdf(
            ikm=dh_static + dh_ephemeral + self._client_psk,
            salt=hashlib.sha256(client_payload + server_core).digest(),
            info=b"myagent-private-v1 handshake",
            length=32,
        )
        server_finished = hmac.new(
            handshake_secret,
            b"server finished" + hashlib.sha256(client_payload + server_core).digest(),
            hashlib.sha256,
        ).digest()
        server_payload = _json_dumps({
            **_json_loads(server_core),
            "server_finished": _b64encode(server_finished),
        })
        await _write_plain_message(writer, server_payload)
        logger.info(
            "Private tunnel server_hello sent: conn=%s peer=%s payload_len=%s server_nonce_len=16",
            conn_id,
            peer,
            len(server_payload),
        )

        client_finished_payload = await _read_plain_message(reader, label="client_finished", conn_id=conn_id, peer=peer)
        logger.info(
            "Private tunnel client_finished received: conn=%s peer=%s payload_len=%s payload_sha256=%s preview=%s",
            conn_id,
            peer,
            len(client_finished_payload),
            hashlib.sha256(client_finished_payload).hexdigest()[:16],
            _payload_preview(client_finished_payload),
        )
        client_finished_msg = _json_loads(client_finished_payload)
        if client_finished_msg.get("type") != "client_finished":
            raise PrivateTunnelError("expected client_finished")
        actual_finished = _b64decode_field(client_finished_msg, "client_finished", expected_len=32)
        expected_finished = hmac.new(
            handshake_secret,
            b"client finished" + hashlib.sha256(client_payload + server_payload).digest(),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(actual_finished, expected_finished):
            raise PrivateTunnelError("client_finished verification failed")
        logger.info("Private tunnel handshake complete: conn=%s peer=%s suite=%s", conn_id, peer, SUITE_NAME)

        transcript = client_payload + server_payload + client_finished_payload
        traffic_secret = _hkdf(
            ikm=handshake_secret,
            salt=hashlib.sha256(transcript).digest(),
            info=b"myagent-private-v1 traffic",
            length=32,
        )
        return _TunnelSession(
            c2s_aead=crypto.ChaCha20Poly1305(_hkdf(traffic_secret, b"", b"c2s key", 32)),
            s2c_aead=crypto.ChaCha20Poly1305(_hkdf(traffic_secret, b"", b"s2c key", 32)),
            c2s_nonce_prefix=_hkdf(traffic_secret, b"", b"c2s nonce", 4),
            s2c_nonce_prefix=_hkdf(traffic_secret, b"", b"s2c nonce", 4),
            max_frame_size=self.config.max_frame_size,
        )

    async def _client_to_upstream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream_writer: asyncio.StreamWriter,
        session: _TunnelSession,
        *,
        conn_id: str,
        peer: Any,
    ) -> None:
        total_bytes = 0
        frame_count = 0
        while True:
            frame_type, plaintext = await asyncio.wait_for(
                _read_encrypted_frame(reader, session),
                timeout=self.config.idle_timeout_seconds,
            )
            if frame_type == FRAME_DATA:
                total_bytes += len(plaintext)
                frame_count += 1
                upstream_writer.write(plaintext)
                await upstream_writer.drain()
            elif frame_type == FRAME_PING:
                await _write_encrypted_frame(writer, session, FRAME_PONG, plaintext)
            elif frame_type in {FRAME_CLOSE, FRAME_ERROR}:
                logger.info(
                    "Private tunnel client stream ended: conn=%s peer=%s frame_type=%s frames=%s bytes=%s",
                    conn_id,
                    peer,
                    _frame_type_name(frame_type),
                    frame_count,
                    total_bytes,
                )
                break
            elif frame_type == FRAME_PONG:
                continue
            else:
                raise PrivateTunnelError(f"unknown frame_type: {frame_type}")

    async def _upstream_to_client(
        self,
        upstream_reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: _TunnelSession,
        *,
        conn_id: str,
        peer: Any,
    ) -> None:
        total_bytes = 0
        frame_count = 0
        while True:
            chunk = await asyncio.wait_for(
                upstream_reader.read(DEFAULT_READ_CHUNK_SIZE),
                timeout=self.config.idle_timeout_seconds,
            )
            if not chunk:
                await _write_encrypted_frame(writer, session, FRAME_CLOSE, b"")
                logger.info(
                    "Private tunnel upstream stream ended: conn=%s peer=%s frames=%s bytes=%s",
                    conn_id,
                    peer,
                    frame_count,
                    total_bytes,
                )
                break
            total_bytes += len(chunk)
            frame_count += 1
            await _write_encrypted_frame(writer, session, FRAME_DATA, chunk)


async def _read_plain_message(
    reader: asyncio.StreamReader,
    *,
    label: str,
    conn_id: str,
    peer: Any,
) -> bytes:
    raw_len = await reader.readexactly(4)
    length = struct.unpack(">I", raw_len)[0]
    if length <= 0 or length > MAX_HANDSHAKE_MESSAGE:
        hint = ""
        if raw_len == MAGIC:
            hint = "; first 4 bytes are MAGT, client may be missing uint32 length prefix before JSON"
        raise PrivateTunnelError(
            f"invalid {label} length: first4_hex={raw_len.hex()} interpreted_len={length} "
            f"max={MAX_HANDSHAKE_MESSAGE}{hint}"
        )
    logger.info(
        "Private tunnel plain message length: conn=%s peer=%s label=%s first4_hex=%s length=%s",
        conn_id,
        peer,
        label,
        raw_len.hex(),
        length,
    )
    payload = await reader.readexactly(length)
    if label == "client_hello" and len(payload) < 120:
        logger.warning(
            "Private tunnel short client_hello: conn=%s peer=%s payload_len=%s; expected JSON hello is usually larger",
            conn_id,
            peer,
            len(payload),
        )
    return payload


async def _write_plain_message(writer: asyncio.StreamWriter, payload: bytes) -> None:
    if len(payload) > MAX_HANDSHAKE_MESSAGE:
        raise PrivateTunnelError("handshake message too large")
    writer.write(struct.pack(">I", len(payload)) + payload)
    await writer.drain()


async def _read_encrypted_frame(reader: asyncio.StreamReader, session: _TunnelSession) -> tuple[int, bytes]:
    raw_len = await reader.readexactly(4)
    frame_len = struct.unpack(">I", raw_len)[0]
    if frame_len < 17 or frame_len > session.max_frame_size + 17:
        raise PrivateTunnelError("invalid encrypted frame length")
    payload = await reader.readexactly(frame_len)
    frame_type = payload[0]
    ciphertext = payload[1:]
    seq = session.recv_seq
    associated_data = _associated_data(frame_type, seq, len(ciphertext))
    nonce = session.c2s_nonce_prefix + struct.pack(">Q", seq)
    plaintext = session.c2s_aead.decrypt(nonce, ciphertext, associated_data)
    if len(plaintext) > session.max_frame_size:
        raise PrivateTunnelError("decrypted frame is too large")
    session.recv_seq += 1
    return frame_type, plaintext


async def _write_encrypted_frame(
    writer: asyncio.StreamWriter | None,
    session: _TunnelSession,
    frame_type: int,
    plaintext: bytes,
    **_: Any,
) -> None:
    if writer is None:
        return
    if len(plaintext) > session.max_frame_size:
        raise PrivateTunnelError("plaintext frame is too large")
    assert session.send_lock is not None
    async with session.send_lock:
        seq = session.send_seq
        associated_data = _associated_data(frame_type, seq, len(plaintext) + 16)
        nonce = session.s2c_nonce_prefix + struct.pack(">Q", seq)
        ciphertext = session.s2c_aead.encrypt(nonce, plaintext, associated_data)
        writer.write(struct.pack(">I", 1 + len(ciphertext)) + bytes([frame_type]) + ciphertext)
        await writer.drain()
        session.send_seq += 1


def _associated_data(frame_type: int, sequence_number: int, ciphertext_len: int) -> bytes:
    return (
        MAGIC
        + struct.pack(">H", PROTOCOL_VERSION)
        + bytes([frame_type])
        + struct.pack(">Q", sequence_number)
        + struct.pack(">I", ciphertext_len)
    )


def _payload_preview(payload: bytes, *, limit: int = 96) -> str:
    clipped = payload[:limit]
    try:
        text = clipped.decode("utf-8")
    except UnicodeDecodeError:
        return "hex:" + clipped.hex()
    text = "".join(ch if ch.isprintable() else "." for ch in text)
    if len(payload) > limit:
        text += "..."
    return repr(text)


def _safe_log_value(value: Any, *, limit: int = 80) -> str:
    text = str(value or "")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _b64_field_len(payload: dict[str, Any], field: str) -> int | str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        return "missing"
    try:
        return len(_b64decode(value))
    except Exception:
        return "invalid"


def _frame_type_name(frame_type: int) -> str:
    names = {
        FRAME_DATA: "DATA",
        FRAME_PING: "PING",
        FRAME_PONG: "PONG",
        FRAME_CLOSE: "CLOSE",
        FRAME_ERROR: "ERROR",
    }
    return names.get(frame_type, f"UNKNOWN({frame_type})")


def _load_x25519_private_key(value: str) -> Any:
    crypto = _crypto()
    raw = _decode_secret(value, name="server_private_key", expected_len=32)
    return crypto.x25519.X25519PrivateKey.from_private_bytes(raw)


def _decode_secret(value: str, *, name: str, expected_len: int | None = None) -> bytes:
    raw_value = (value or "").strip()
    if not raw_value:
        raise ValueError(f"private_transport.{name} is required")
    if raw_value.startswith("base64:"):
        raw_value = raw_value[len("base64:"):]
        padded = raw_value + "=" * (-len(raw_value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    elif expected_len is not None:
        padded = raw_value + "=" * (-len(raw_value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    else:
        decoded = raw_value.encode("utf-8")
    if expected_len is not None and len(decoded) != expected_len:
        raise ValueError(f"private_transport.{name} must decode to {expected_len} bytes")
    return decoded


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str, *, expected_len: int | None = None) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    data = base64.urlsafe_b64decode(padded.encode("ascii"))
    if expected_len is not None and len(data) != expected_len:
        raise PrivateTunnelError("invalid base64 field length")
    return data


def _b64decode_field(payload: dict[str, Any], field: str, *, expected_len: int | None = None) -> bytes:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise PrivateTunnelError(f"missing field: {field}")
    return _b64decode(value, expected_len=expected_len)


def _json_dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json_loads(payload: bytes) -> dict[str, Any]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise PrivateTunnelError("invalid json message") from exc
    if not isinstance(data, dict):
        raise PrivateTunnelError("json message must be an object")
    return data


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    crypto = _crypto()
    return crypto.HKDF(
        algorithm=crypto.hashes.SHA256(),
        length=length,
        salt=salt or None,
        info=info,
    ).derive(ikm)


@dataclass(frozen=True)
class _CryptoModule:
    x25519: Any
    ChaCha20Poly1305: Any
    HKDF: Any
    hashes: Any
    serialization: Any


def _crypto() -> _CryptoModule:
    try:
        from cryptography.hazmat.primitives.asymmetric import x25519
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes, serialization
    except Exception as exc:
        raise RuntimeError(
            "private_transport requires working cryptography/cffi dependencies. "
            "Install project dependencies before enabling the private tunnel."
        ) from exc
    return _CryptoModule(
        x25519=x25519,
        ChaCha20Poly1305=ChaCha20Poly1305,
        HKDF=HKDF,
        hashes=hashes,
        serialization=serialization,
    )


def _close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:
        pass


def _dedupe_hosts(values: list[Any] | tuple[Any, ...]) -> list[str]:
    hosts: list[str] = []
    for value in values:
        host = str(value or "").strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _validate_upstream_source_host(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("private_transport.upstream_source_host must be a loopback IP address") from exc
    if not address.is_loopback:
        raise ValueError("private_transport.upstream_source_host must be a loopback IP address")
