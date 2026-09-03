"""OnlyOffice 文档预览/编辑 REST API。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException, Query, Request

from myagent.interfaces.web.dependencies import get_document_service, get_session_manager
from myagent.integrations.onlyoffice.protocol import ErrorCode, OnlyOfficeProtocolError
from myagent.interfaces.web.private_proxy_context import resolve_private_proxy_context
from myagent.utils.logging import get_logger


logger = get_logger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])
tools_router = APIRouter(prefix="/api/tools", tags=["onlyoffice-tools"])


@router.get("/health")
async def documents_health():
    """返回 OnlyOffice 集成功能是否启用。"""
    service = get_document_service()
    return {
        "enabled": service.enabled,
        "onlyoffice_url": service.config.onlyoffice_url,
    }


@router.get("/plugin-config")
async def plugin_config(token: str = Query(...)):
    """Return the signed per-editor hidden plugin manifest."""
    return get_document_service().plugin_config(token)


@router.post("/plugin-runtime")
async def plugin_runtime(
    token: str = Header(..., alias="X-OnlyOffice-Plugin-Token"),
):
    """Return runtime bridge parameters to the hidden plugin iframe."""
    return get_document_service().plugin_runtime(token)


@router.post("/plugin-diagnostic")
async def plugin_diagnostic(
    request: Request,
    token: str = Header(..., alias="X-OnlyOffice-Plugin-Token"),
):
    """Record a signed plugin lifecycle event without exposing the token in access logs."""
    service = get_document_service()
    token_payload = service.verify_plugin_token(token)
    try:
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(status_code=413, detail="OnlyOffice 插件诊断消息过大")
        payload = json.loads(body or b"{}")
    except HTTPException:
        raise
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    phase = _safe_log_text(payload.get("phase"), 80)
    message = _safe_log_text(payload.get("message"), 500)
    logger.info(
        "OnlyOffice plugin diagnostic: session=%s path=%s editor=%s key=%s phase=%s message=%s client=%s",
        _safe_log_text(token_payload.get("session_id"), 128),
        _safe_log_text(token_payload.get("path"), 1000),
        _short_id(token_payload.get("editor_session_id")),
        _short_id(token_payload.get("document_key")),
        phase,
        message,
        _client_host(request),
    )
    return {"ok": True}


@router.get("/editor-config")
async def editor_config(
    request: Request,
    path: str = Query(..., description="工作区相对路径"),
    mode: str = Query("edit", description="edit 或 view"),
    session_id: str = Query("", description="会话 ID，用于定位工作空间根目录"),
):
    """浏览器端获取 OnlyOffice editor config；该端点由 AuthMiddleware 保护。"""
    service = get_document_service()
    user = getattr(request.state, "user", None)
    username = getattr(user, "username", "") if user else ""
    group = getattr(user, "group", "user") if user else "user"
    logger.info(
        "Documents editor-config requested: path=%s mode=%s session=%s user=%s client=%s",
        path,
        mode,
        session_id,
        username,
        _client_host(request),
    )
    session = _session_for_user(session_id, username)
    resolver = getattr(session.workspace, "resolver", None) if session and session.workspace else None
    proxy_context = resolve_private_proxy_context(
        request,
        getattr(request.app.state, "encrypted_transport_sources", set()),
    )
    if proxy_context.trusted_tunnel and not proxy_context.browser_origin:
        logger.warning(
            "Documents editor-config could not resolve private browser origin: client=%s host=%s",
            _client_host(request),
            request.headers.get("host", ""),
        )
    elif proxy_context.browser_origin:
        logger.info(
            "Documents editor-config private browser origin resolved: client=%s origin=%s",
            _client_host(request),
            proxy_context.browser_origin,
        )
    data = service.build_editor_config(
        path,
        username=username,
        group=group,
        mode=mode,
        workspace_root=session.workspace.root_path if session and session.workspace else None,
        resolver=resolver,
        session_id=session_id,
        onlyoffice_proxy_origin=proxy_context.browser_origin,
        browser_origin=proxy_context.browser_origin or _request_browser_origin(request),
    )
    logger.info(
        "Documents editor-config response: path=%s onlyoffice_url=%s",
        path,
        data.get("onlyoffice_url", ""),
    )
    return data


@router.get("/download")
async def download_document(
    request: Request,
    path: str = Query(..., description="工作区相对路径"),
    token: str = Query(..., description="短期文档访问 token"),
):
    """OnlyOffice DocumentServer 下载文档。"""
    logger.info("Documents download requested: path=%s client=%s", path, _client_host(request))
    service = get_document_service()
    payload = service.verify_access_token(token, path)
    session = _session_for_user(
        str(payload.get("session_id") or ""),
        str(payload.get("username") or ""),
        required=False,
    )
    resolver = getattr(session.workspace, "resolver", None) if session and session.workspace else None
    workspace_root = session.workspace.root_path if session and session.workspace else None
    return service.download_file(path, token, workspace_root=workspace_root, resolver=resolver)


@router.post("/callback")
async def document_callback(
    request: Request,
    path: str = Query(..., description="工作区相对路径"),
    token: str = Query(..., description="短期文档访问 token"),
):
    """OnlyOffice DocumentServer 保存回调。"""
    service = get_document_service()
    payload = await request.json()
    logger.info(
        "Documents callback requested: path=%s client=%s payload_keys=%s",
        path,
        _client_host(request),
        sorted(payload.keys()),
    )
    token_payload = service.verify_access_token(token, path)
    session = _session_for_user(
        str(token_payload.get("session_id") or ""),
        str(token_payload.get("username") or ""),
        required=False,
    )
    resolver = getattr(session.workspace, "resolver", None) if session and session.workspace else None
    workspace_root = session.workspace.root_path if session and session.workspace else None
    result = await service.handle_callback(path, token, payload, workspace_root=workspace_root, resolver=resolver)
    status = int(payload.get("status") or 0)
    if result.get("error") == 0 and session and session.workspace and status in {2, 6}:
        if resolver and resolver.virtual_path_area(path) == "public":
            if status == 2:
                await get_session_manager().notify_public_workspace_changed([path])
        else:
            action = "onlyoffice_saved" if status == 6 else "files_changed"
            await session.workspace.update("user", action, {"changed_paths": [path]})
    if status in {2, 4}:
        service.release_document_lease(path, str(payload.get("key") or ""))
    service.notify_save_completed(payload, success=result.get("error") == 0)
    return result


@tools_router.post("/onlyoffice_format")
async def onlyoffice_format(request: Request):
    """Format the current session-bound hidden ONLYOFFICE selection."""
    payload = await _tool_payload(request)
    runtime = _tool_runtime(request, payload)
    properties = payload.get("properties")
    if not isinstance(properties, dict):
        raise HTTPException(status_code=400, detail="properties 必须是对象")
    try:
        return await runtime.http_format(
            str(payload.get("path") or ""),
            properties,
            expected_text=payload.get("expected_text"),
        )
    except OnlyOfficeProtocolError as exc:
        raise _tool_http_error(exc) from exc


@tools_router.post("/onlyoffice_add_comment")
async def onlyoffice_add_comment(request: Request):
    """Add a comment to the current session-bound hidden ONLYOFFICE selection."""
    payload = await _tool_payload(request)
    runtime = _tool_runtime(request, payload)
    try:
        return await runtime.http_add_comment(
            str(payload.get("path") or ""),
            payload.get("text"),
            expected_text=payload.get("expected_text"),
            author=str(payload.get("author") or "Agent"),
            author_user_id=str(payload.get("author_user_id") or "agent"),
        )
    except OnlyOfficeProtocolError as exc:
        raise _tool_http_error(exc) from exc


async def _tool_payload(request: Request) -> dict:
    try:
        body = await request.body()
        if len(body) > 256_000:
            raise HTTPException(status_code=413, detail="OnlyOffice 工具请求过大")
        payload = json.loads(body or b"{}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求必须是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求必须是 JSON 对象")
    return payload


def _tool_runtime(request: Request, payload: dict):
    user = getattr(request.state, "user", None)
    username = getattr(user, "username", "") if user else ""
    session = _session_for_user(str(payload.get("session_id") or ""), username)
    runtime = getattr(session, "onlyoffice_automation", None)
    if not runtime or not runtime.enabled:
        raise HTTPException(status_code=503, detail="OnlyOffice 自动化未启用")
    if not isinstance(payload.get("path"), str) or not payload["path"]:
        raise HTTPException(status_code=400, detail="path 必须是非空字符串")
    return runtime


def _tool_http_error(exc: OnlyOfficeProtocolError) -> HTTPException:
    if exc.code in {ErrorCode.TARGET_NOT_FOUND, ErrorCode.LINE_OUT_OF_RANGE}:
        status = 404
    elif exc.code in {
        ErrorCode.STALE_LINE_INDEX, ErrorCode.NO_ACTIVE_SELECTION, ErrorCode.SELECTION_CHANGED,
        ErrorCode.TARGET_NOT_UNIQUE, ErrorCode.VERIFY_FAILED,
    }:
        status = 409
    elif exc.code == ErrorCode.READ_ONLY:
        status = 423
    elif exc.code in {ErrorCode.NO_CAPABLE_CLIENT, ErrorCode.EDITOR_OPEN_TIMEOUT, ErrorCode.SAVE_FAILED}:
        status = 503
    else:
        status = 400
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


def _client_host(request: Request) -> str:
    if not request.client:
        return ""
    return f"{request.client.host}:{request.client.port}"


def _request_browser_origin(request: Request) -> str | None:
    """Return the authenticated browser's actual HTTP origin for plugin messaging."""
    try:
        scheme = str(request.url.scheme or "").lower()
        host = str(request.headers.get("host", "") or "")
    except Exception:
        return None
    if scheme not in {"http", "https"} or not host or any(ord(char) <= 32 for char in host):
        return None
    return f"{scheme}://{host}"


def _safe_log_text(value, limit: int) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ")[:limit]


def _short_id(value) -> str:
    text = _safe_log_text(value, 128)
    return text[:12] if text else "-"


def _session_for_user(session_id: str, username: str, required: bool = True):
    if not session_id:
        if required:
            raise HTTPException(status_code=400, detail="缺少 session_id")
        return None
    session = get_session_manager().get_session(session_id, user=username)
    if not session or not session.workspace or not session.workspace.root_path:
        if required:
            raise HTTPException(status_code=404, detail="会话工作空间不存在")
        return None
    return session
