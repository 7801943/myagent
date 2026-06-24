"""
认证 REST API：登录、登出、用户信息查询。

端点：
  - POST /api/auth/login   — 用户登录
  - POST /api/auth/logout  — 用户登出
  - GET  /api/auth/me      — 获取当前用户信息
"""
from fastapi import APIRouter, Request, Response

from pydantic import BaseModel

from myagent.interfaces.web.auth import get_client_ip
from myagent.interfaces.web.dependencies import get_auth_service
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── 请求/响应模型 ──

class LoginRequest(BaseModel):
    """登录请求。"""
    username: str
    password: str


class LoginResponse(BaseModel):
    """登录响应。"""
    ok: bool
    token: str | None = None
    username: str | None = None
    group: str | None = None
    error: str | None = None


class LogoutResponse(BaseModel):
    """登出响应。"""
    ok: bool


class UserInfoResponse(BaseModel):
    """用户信息响应。"""
    username: str
    group: str = "user"
    disabled: bool = False
    active_ips: list[str]
    visible_tools: list[str] = []
    visible_skills: list[str] = []
    workspace: dict = {}


# ── 路由 ──

@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request):
    """用户登录：校验密码 + IP 限制检查。"""
    auth_service = get_auth_service()
    client_ip = get_client_ip(request, auth_service.trusted_proxies)

    logger.info(f"Login attempt: username={req.username}, ip={client_ip}")

    token_info, error = await auth_service.login(req.username, req.password, client_ip)

    if token_info is None:
        logger.warning(f"Login failed: username={req.username}, ip={client_ip}, error={error}")
        return LoginResponse(ok=False, error=error)

    logger.info(f"Login successful: username={req.username}, ip={client_ip}")
    return LoginResponse(
        ok=True,
        token=token_info.token,
        username=token_info.username,
        group=token_info.group,
    )


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request):
    """用户登出：移除 Token 和 IP 绑定。"""
    token = _extract_token(request)
    if not token:
        logger.warning("Logout attempt without token")
        return LogoutResponse(ok=False)

    auth_service = get_auth_service()
    success = await auth_service.logout(token)
    if success:
        logger.info("Logout successful")
    else:
        logger.warning("Logout failed: invalid or expired token")
    return LogoutResponse(ok=success)


@router.get("/me", response_model=UserInfoResponse)
async def get_me(request: Request):
    """获取当前登录用户信息。"""
    token = _extract_token(request)
    if not token:
        return Response(status_code=401, content='{"error": "未认证"}')

    auth_service = get_auth_service()
    token_info = auth_service.validate_token(token)
    if not token_info:
        return Response(status_code=401, content='{"error": "Token 无效或已过期"}')

    user_info = auth_service.get_user_info(token_info.username)
    if not user_info:
        return Response(status_code=401, content='{"error": "用户不存在"}')

    return UserInfoResponse(**user_info)


# ── 辅助函数 ──

def _extract_token(request: Request) -> str | None:
    """从请求头中提取 Bearer Token。"""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return None
