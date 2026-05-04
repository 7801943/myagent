"""
健康检查与配置查询路由。

未来扩展：
  - [AUTH] 添加 /api/auth/status 端点
  - [MCP] 添加 /api/mcp/status 端点
"""
from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """健康检查端点。"""
    return {"status": "ok", "service": "myagent"}


@router.get("/api/config")
async def get_config():
    """
    获取前端所需的公开配置信息。
    注意：不暴露敏感信息（如 API key）。
    """
    from myagent.interfaces.web.dependencies import get_agent_factory

    factory = get_agent_factory()
    config = factory.config

    return {
        "max_iterations": config.max_iterations,
        "providers": [
            {"name": p.name, "type": p.type, "model": p.model}
            for p in config.providers
        ],
    }