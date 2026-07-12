"""Same-origin reverse proxy for ONLYOFFICE DocumentServer."""
from __future__ import annotations

import asyncio
import inspect
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from myagent.interfaces.web.dependencies import get_document_service
from myagent.utils.logging import get_logger


logger = get_logger(__name__)

router = APIRouter(prefix="/onlyoffice", tags=["onlyoffice-proxy"])

PROXY_PREFIX = "/onlyoffice"
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
WEBSOCKET_HANDSHAKE_HEADERS = {
    "host",
    "connection",
    "upgrade",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
}


@router.api_route("", methods=PROXY_METHODS, include_in_schema=False)
@router.api_route("/{path:path}", methods=PROXY_METHODS, include_in_schema=False)
async def proxy_onlyoffice_http(request: Request, path: str = ""):
    """Proxy browser-side ONLYOFFICE HTTP requests to the internal DocumentServer."""
    service = get_document_service()
    if not service.enabled:
        raise HTTPException(status_code=404, detail="文档预览/编辑未启用")

    upstream_url = build_upstream_url(
        service.config.onlyoffice_internal_url,
        _request_upstream_path(request.scope),
        _request_query_string(request.scope),
    )
    headers = _proxy_request_headers(
        request.headers,
        client_host=request.client.host if request.client else "",
        request_scheme=_request_scheme(request),
        request_host=request.headers.get("host", ""),
        browser_url=service.config.onlyoffice_url,
    )

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=False)
    try:
        request_kwargs = {}
        if request.method not in {"GET", "HEAD"}:
            request_kwargs["content"] = request.stream()
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            headers=headers,
            **request_kwargs,
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("OnlyOffice proxy HTTP upstream failed: url=%s error=%s", upstream_url, exc)
        raise HTTPException(status_code=502, detail="OnlyOffice 服务不可用") from exc

    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=_proxy_response_headers(upstream_response.headers),
        background=BackgroundTask(_close_http_proxy, upstream_response, client),
    )


@router.websocket("")
@router.websocket("/{path:path}")
async def proxy_onlyoffice_websocket(websocket: WebSocket, path: str = ""):
    """Proxy ONLYOFFICE websocket traffic used by collaborative editing."""
    service = get_document_service()
    if not service.enabled:
        await websocket.close(code=1008, reason="documents disabled")
        return

    upstream_url = build_upstream_url(
        service.config.onlyoffice_internal_url,
        _request_upstream_path(websocket.scope),
        _request_query_string(websocket.scope),
        websocket=True,
    )
    headers = _proxy_websocket_headers(
        websocket.headers,
        client_host=websocket.client.host if websocket.client else "",
        request_scheme=_websocket_forwarded_scheme(websocket),
        request_host=websocket.headers.get("host", ""),
        browser_url=service.config.onlyoffice_url,
    )
    subprotocols = _parse_subprotocols(websocket.headers.get("sec-websocket-protocol", ""))

    upstream_context = _connect_upstream_websocket(upstream_url, headers, subprotocols)
    try:
        async with upstream_context as upstream:
            await _proxy_websocket_session(websocket, upstream)
    except Exception as exc:
        logger.warning("OnlyOffice proxy websocket upstream failed: url=%s error=%s", upstream_url, exc)
        if websocket.client_state.name != "DISCONNECTED":
            await websocket.close(code=1011, reason="OnlyOffice upstream unavailable")


async def _proxy_websocket_session(websocket: WebSocket, upstream) -> None:
    await websocket.accept(subprotocol=getattr(upstream, "subprotocol", None))
    client_task = asyncio.create_task(_websocket_client_to_upstream(websocket, upstream))
    upstream_task = asyncio.create_task(_websocket_upstream_to_client(websocket, upstream))
    done, pending = await asyncio.wait({client_task, upstream_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


def build_upstream_url(upstream_base: str, upstream_path: str, query_string: str = "", *, websocket: bool = False) -> str:
    """Build an upstream URL from the configured DocumentServer base and stripped proxy path."""
    base = urlsplit(upstream_base.rstrip("/"))
    if base.scheme not in {"http", "https"} or not base.netloc:
        raise HTTPException(status_code=500, detail="OnlyOffice 内部地址配置无效")

    scheme = base.scheme
    if websocket:
        scheme = "wss" if scheme == "https" else "ws"

    base_path = base.path.rstrip("/")
    path = "/" + upstream_path.lstrip("/")
    if base_path:
        path = f"{base_path}{path}"
    return urlunsplit((scheme, base.netloc, path, query_string, ""))


def forwarded_host(request_host: str, browser_url: str) -> str:
    """Return the X-Forwarded-Host value expected by ONLYOFFICE virtual-path proxying."""
    browser = urlsplit(browser_url)
    host = browser.netloc or request_host
    prefix = browser.path.rstrip("/") if browser.path else PROXY_PREFIX
    if not prefix or prefix == "/":
        return host
    return f"{host}{prefix}"


def _request_upstream_path(scope: dict) -> str:
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, bytes):
        path = raw_path.decode("ascii", "surrogateescape")
    else:
        path = str(scope.get("path") or "")
    if path == PROXY_PREFIX:
        return "/"
    if path.startswith(f"{PROXY_PREFIX}/"):
        return path[len(PROXY_PREFIX):]
    return "/"


def _request_query_string(scope: dict) -> str:
    query = scope.get("query_string", b"")
    if isinstance(query, bytes):
        return query.decode("latin-1")
    return str(query or "")


def _request_scheme(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.url.scheme


def _websocket_forwarded_scheme(websocket: WebSocket) -> str:
    forwarded = websocket.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return "https" if websocket.url.scheme == "wss" else "http"


def _proxy_request_headers(
    source_headers,
    *,
    client_host: str,
    request_scheme: str,
    request_host: str,
    browser_url: str,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in source_headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS or lower == "host":
            continue
        headers[name] = value
    _add_forwarded_headers(headers, client_host, request_scheme, request_host, browser_url)
    return headers


def _proxy_websocket_headers(
    source_headers,
    *,
    client_host: str,
    request_scheme: str,
    request_host: str,
    browser_url: str,
) -> list[tuple[str, str]]:
    headers: dict[str, str] = {}
    for name, value in source_headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in WEBSOCKET_HANDSHAKE_HEADERS:
            continue
        headers[name] = value
    _add_forwarded_headers(headers, client_host, request_scheme, request_host, browser_url)
    return list(headers.items())


def _add_forwarded_headers(
    headers: dict[str, str],
    client_host: str,
    request_scheme: str,
    request_host: str,
    browser_url: str,
) -> None:
    existing_for = _pop_header_case_insensitive(headers, "x-forwarded-for")
    _pop_header_case_insensitive(headers, "x-forwarded-proto")
    _pop_header_case_insensitive(headers, "x-forwarded-host")
    _pop_header_case_insensitive(headers, "x-forwarded-prefix")
    forwarded_for = f"{existing_for}, {client_host}" if existing_for and client_host else client_host or existing_for
    if forwarded_for:
        headers["X-Forwarded-For"] = forwarded_for
    headers["X-Forwarded-Proto"] = request_scheme
    headers["X-Forwarded-Host"] = forwarded_host(request_host, browser_url)
    headers["X-Forwarded-Prefix"] = PROXY_PREFIX


def _pop_header_case_insensitive(headers: dict[str, str], name: str) -> str:
    for key in list(headers.keys()):
        if key.lower() == name:
            return headers.pop(key)
    return ""


def _proxy_response_headers(source_headers) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in source_headers.items():
        if name.lower() in HOP_BY_HOP_HEADERS:
            continue
        headers[name] = value
    return headers


async def _close_http_proxy(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


def _connect_upstream_websocket(upstream_url: str, headers: list[tuple[str, str]], subprotocols: list[str]):
    try:
        import websockets
    except Exception as exc:
        raise RuntimeError("OnlyOffice websocket proxy requires the websockets package") from exc

    kwargs = {
        "max_size": None,
        "ping_interval": None,
        "subprotocols": subprotocols or None,
    }
    signature = inspect.signature(websockets.connect)
    if "additional_headers" in signature.parameters:
        kwargs["additional_headers"] = headers
    else:
        kwargs["extra_headers"] = headers
    return websockets.connect(upstream_url, **kwargs)


async def _websocket_client_to_upstream(websocket: WebSocket, upstream) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                await upstream.close()
                return
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
            elif message.get("text") is not None:
                await upstream.send(message["text"])
    except WebSocketDisconnect:
        await upstream.close()


async def _websocket_upstream_to_client(websocket: WebSocket, upstream) -> None:
    async for message in upstream:
        if isinstance(message, bytes):
            await websocket.send_bytes(message)
        else:
            await websocket.send_text(message)


def _parse_subprotocols(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
