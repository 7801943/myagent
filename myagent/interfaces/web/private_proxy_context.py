"""可信私有隧道请求的代理上下文解析。

该模块只读取连接元数据，不修改 ASGI scope、请求头或请求路径。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

from myagent.interfaces.web.auth import normalize_ip


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class PrivateProxyContext:
    """从一次 HTTP 连接中提取出的可信私有代理信息。"""

    trusted_tunnel: bool
    peer_ip: str
    browser_origin: str | None = None


def resolve_private_proxy_context(
    request: Any,
    trusted_sources: Iterable[str] | None,
) -> PrivateProxyContext:
    """仅对可信隧道出口解析 LocalProxy 的 loopback HTTP origin。"""
    client = getattr(request, "client", None)
    peer_ip = normalize_ip(str(getattr(client, "host", "") or ""))
    normalized_sources = {normalize_ip(str(source)) for source in trusted_sources or ()}
    if not peer_ip or peer_ip not in normalized_sources:
        return PrivateProxyContext(trusted_tunnel=False, peer_ip=peer_ip)

    raw_host = str(getattr(request, "headers", {}).get("host", "") or "")
    browser_origin = loopback_http_origin_from_host(raw_host)
    return PrivateProxyContext(
        trusted_tunnel=True,
        peer_ip=peer_ip,
        browser_origin=browser_origin,
    )


def normalize_loopback_http_origin(value: str | None) -> str | None:
    """规范化完整 origin；只接受无额外 URL 组件的 HTTP loopback 地址。"""
    if not value or _contains_unsafe_authority_characters(value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "http" or not parsed.netloc:
            return None
        if parsed.username or parsed.password:
            return None
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return None
        port = parsed.port
    except ValueError:
        return None

    hostname = (parsed.hostname or "").lower()
    if hostname not in LOOPBACK_HOSTS:
        return None
    if parsed.netloc.endswith(":") or (port is not None and port < 1):
        return None
    return _build_http_origin(hostname, port)


def loopback_http_origin_from_host(raw_host: str | None) -> str | None:
    """将 HTTP Host authority 转为经过规范化的 loopback origin。"""
    if not raw_host or _contains_unsafe_authority_characters(raw_host):
        return None
    return normalize_loopback_http_origin(f"http://{raw_host}")


def _build_http_origin(hostname: str, port: int | None) -> str:
    authority = f"[{hostname}]" if hostname == "::1" else hostname
    if port is not None:
        authority = f"{authority}:{port}"
    return f"http://{authority}"


def _contains_unsafe_authority_characters(value: str) -> bool:
    return any(ord(char) <= 32 or ord(char) == 127 for char in value)
