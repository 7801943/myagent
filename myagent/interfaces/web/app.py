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
  - /api/sessions — 会话 CRUD REST API
  - / — 静态文件服务（web/ 目录）

未来扩展：
  - [AUTH] JWT 认证中间件，保护 /api/* 和 /ws 端点
  - [MCP] /mcp 端点暴露 MCP 协议（SSE transport）
  - [AUTH] OAuth2 / LDAP 集成
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
    get_agent_factory,
    get_state_store,
)
from myagent.interfaces.web.ws_handler import WebSocketHandler
from myagent.interfaces.web.routes import health, sessions
from myagent.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)


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

    # ── 注册 REST 路由 ──
    app.include_router(health.router)
    app.include_router(sessions.router)

    # ── WebSocket 端点 ──
    # TODO: [AUTH] 连接时校验 token（可通过 query params 或 header）
    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        """WebSocket 端点：Agent 实时交互。"""
        factory = get_agent_factory()
        store = get_state_store()
        handler = WebSocketHandler(ws, factory, store)
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
    )


if __name__ == "__main__":
    main()