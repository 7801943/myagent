"""OnlyOffice 文档预览/编辑 REST API。"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from myagent.interfaces.web.dependencies import get_document_service
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
):
    """浏览器端获取 OnlyOffice editor config；该端点由 AuthMiddleware 保护。"""
    service = get_document_service()
    user = getattr(request.state, "user", None)
    username = getattr(user, "username", "") if user else ""
    logger.info(
        "Documents editor-config requested: path=%s mode=%s user=%s client=%s",
        path,
        mode,
        username,
        _client_host(request),
    )
    return service.build_editor_config(path, username=username, mode=mode)


@router.get("/download")
async def download_document(
    request: Request,
    path: str = Query(..., description="工作区相对路径"),
    token: str = Query(..., description="短期文档访问 token"),
):
    """OnlyOffice DocumentServer 下载文档。"""
    logger.info("Documents download requested: path=%s client=%s", path, _client_host(request))
    service = get_document_service()
    return service.download_file(path, token)


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
    return await service.handle_callback(path, token, payload)


def _client_host(request: Request) -> str:
    if not request.client:
        return ""
    return f"{request.client.host}:{request.client.port}"
