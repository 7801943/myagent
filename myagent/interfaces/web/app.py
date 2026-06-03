"""
FastAPI 应用入口：统一 HTTP + WebSocket + 静态文件服务。

启动方式：
    uvicorn myagent.interfaces.web.app:app --host 0.0.0.0 --port 8000 --reload
    或
    python -m myagent.interfaces.web.app

架构：
  - lifespan 管理 StateStore 初始化/清理
  - /ws — WebSocket 端点（Agent 实时交互）
  - /health, /api/config — 健康检查与配置查询
  - /api/auth/* — 认证 API（登录/登出/用户信息）
  - /api/sessions — 会话 CRUD REST API
  - / — 静态文件服务（web/ 目录）
"""
import argparse
import asyncio

import uvicorn
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from myagent.interfaces.web.dependencies import (
    init_services,
    startup,
    shutdown,
    get_session_manager,
    get_state_store,
    get_auth_service,
)
from myagent.interfaces.web.ws_handler import WebSocketHandler
from myagent.interfaces.web.routes import health, sessions, auth
from myagent.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)

# ── Uvicorn 日志配置：毫秒级时间戳 ──
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "with_timestamp": {
            "format": "[%(asctime)s.%(msecs)03d] %(levelprefix)s %(message)s",
            "datefmt": "%m/%d/%y %H:%M:%S",
            "class": "uvicorn.logging.DefaultFormatter",
        },
        "access_with_timestamp": {
            "format": "[%(asctime)s.%(msecs)03d] %(levelprefix)s %(client_addr)s - \"%(request_line)s\" %(status_code)s",
            "datefmt": "%m/%d/%y %H:%M:%S",
            "class": "uvicorn.logging.AccessFormatter",
        },
    },
    "handlers": {
        "default": {
            "formatter": "with_timestamp",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access_with_timestamp",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}

# ── 不需要认证的白名单路径 ──
PUBLIC_PATHS = {
    "/health",
    "/api/auth/login",
    "/api/config",
    "/docs",
    "/openapi.json",
}


class AuthMiddleware:
    """
    纯 ASGI 认证中间件：保护 /api/* HTTP 端点（登录接口除外）。

    使用纯 ASGI 实现（而非 BaseHTTPMiddleware），避免拦截 WebSocket 连接。
    WebSocket 连接在 /ws 端点内部自行验证 token。

    白名单：
      - /health, /api/auth/login, /api/config
      - 静态文件（非 /api/ 路径）
      - WebSocket 连接（/ws 端点内部自行验证）
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # WebSocket 连接直接透传（token 验证在 /ws 端点内部完成）
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        # 非HTTP类型直接透传
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # HTTP 请求：提取路径
        path = scope.get("path", "")

        # 白名单路径直接放行
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        # 非 API 路径（静态文件等）直接放行
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # API 路由需要认证：从 headers 提取 Bearer token
        token = None
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header_val = value.decode("latin-1")
                if header_val.startswith("Bearer "):
                    token = header_val[7:].strip()
                break

        if not token:
            body = '{"error": "未认证，请先登录"}'.encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        auth_service = get_auth_service()
        token_info = auth_service.validate_token(token)
        if not token_info:
            body = '{"error": "Token 无效或已过期"}'.encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        # 认证通过：注入用户信息到 scope["state"]
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["user"] = token_info

        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：初始化和清理全局资源。"""
    config_path = getattr(app.state, "config_path", "config.yaml")
    setup_logging(level="INFO")

    # Startup：初始化服务
    init_services(config_path=config_path)
    await startup()
    logger.info("MyAgent FastAPI server started")

    yield

    # Shutdown：清理资源
    await shutdown()
    logger.info("MyAgent FastAPI server stopped")


def create_app(config_path: str = "config.yaml") -> FastAPI:
    """创建 FastAPI 应用实例。"""
    app = FastAPI(
        title="MyAgent",
        description="全自研生产级异步 Python Agent 框架",
        version="0.1.0",
        lifespan=lifespan,
    )

    # 保存配置路径供 lifespan 使用
    app.state.config_path = config_path

    # ── CORS 中间件 ──
    # TODO: [AUTH] 生产环境应限制 allow_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 认证中间件（纯 ASGI，不拦截 WebSocket）──
    app.add_middleware(AuthMiddleware)

    # ── 注册 REST 路由 ──
    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(auth.router)

    # ── WebSocket 端点 ──
    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        """WebSocket 端点：Agent 实时交互。需要 token 参数。"""
        token = ws.query_params.get("token", "")

        if not token:
            await ws.close(code=4001, reason="未提供认证 Token")
            return

        auth_service = get_auth_service()
        token_info = auth_service.validate_ws_token(token)
        if not token_info:
            await ws.close(code=4001, reason="Token 无效或已过期，请重新登录")
            return

        # 将用户信息注入 WebSocket state
        ws.state.user = token_info

        session_manager = get_session_manager()
        store = get_state_store()
        handler = WebSocketHandler(ws, session_manager, store)
        await handler.run()

    # ── 静态文件挂载（必须放在最后，否则会拦截所有路由）──
    web_dir = Path(__file__).resolve().parent.parent.parent.parent / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")
    else:
        logger.warning(f"Web directory not found: {web_dir}")

    return app


# 默认应用实例（uvicorn myagent.interfaces.web.app:app 会使用这个）
app = create_app()


def main():
    """CLI 入口：python -m myagent.interfaces.web.app"""
    parser = argparse.ArgumentParser(description="MyAgent FastAPI Server")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="监听端口 (默认 8000)")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--reload", action="store_true", help="启用热重载（开发模式）")
    args = parser.parse_args()

    # 如果指定了非默认配置，需要重新创建 app
    if args.config != "config.yaml":
        global app
        app = create_app(config_path=args.config)

    uvicorn.run(
        "myagent.interfaces.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=LOGGING_CONFIG,
    )


if __name__ == "__main__":
    main()