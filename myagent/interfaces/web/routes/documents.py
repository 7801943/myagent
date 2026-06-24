"""OnlyOffice 文档预览/编辑 REST API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from myagent.interfaces.web.dependencies import get_document_service, get_session_manager
from myagent.utils.logging import get_logger


logger = get_logger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("/health")
async def documents_health():
    """返回 OnlyOffice 集成功能是否启用。"""
    service = get_document_service()
    return {
        "enabled": service.enabled,
        "onlyoffice_url": service.config.onlyoffice_url,
    }


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
    return service.build_editor_config(
        path,
        username=username,
        group=group,
        mode=mode,
        workspace_root=session.workspace.root_path if session and session.workspace else None,
        resolver=resolver,
        session_id=session_id,
    )


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
    if result.get("error") == 0 and session and session.workspace and int(payload.get("status") or 0) in {2, 6}:
        if path == "public" or path.startswith("public/"):
            await get_session_manager().notify_public_workspace_changed([path])
        else:
            await session.workspace.update("user", "files_changed", {"changed_paths": [path]})
    return result


def _client_host(request: Request) -> str:
    if not request.client:
        return ""
    return f"{request.client.host}:{request.client.port}"


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
