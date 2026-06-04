"""
FastAPI 依赖注入：管理共享服务实例。

Harness 重构：
  - 移除 AgentFactory 引用
  - SessionManager 直接构建所有组件（ProviderRouter / ToolManager / SafetyGuard）
"""
from pathlib import Path

from myagent.context.state import SQLiteStateStore
from myagent.core.session import SessionManager
from myagent.core.models import UserContext
from myagent.interfaces.web.auth import AuthService
from myagent.interfaces.web.services.document_service import DocumentService
from myagent.utils.config import load_yaml_config


# ── 全局单例（由 app.py lifespan 管理生命周期）──

_state_store: SQLiteStateStore | None = None
_session_manager: SessionManager | None = None
_auth_service: AuthService | None = None
_document_service: DocumentService | None = None


def _load_full_config(config_path: str = "config.yaml") -> dict:
    """加载完整 YAML 配置，并复用项目既有的环境变量解析规则。"""
    return load_yaml_config(config_path)

def init_services(config_path: str = "config.yaml") -> None:
    """初始化全局服务实例（在 lifespan startup 时调用）。"""
    global _state_store, _session_manager, _auth_service, _document_service
    _state_store = SQLiteStateStore()
    _session_manager = SessionManager(config_path=config_path, state_store=_state_store)

    full_config = _load_full_config(config_path)

    # 初始化 AuthService
    auth_config = full_config.get("auth", {})
    _auth_service = AuthService(
        users_file=auth_config.get("users_file", "data/users.json"),
        token_ttl=auth_config.get("token_ttl_seconds", 86400),
        max_ips=auth_config.get("max_ips_per_user", 2),
        trusted_proxies=auth_config.get("trusted_proxies"),
    )
    _auth_service.load_users()

    # 初始化 DocumentService。工作区根目录与 SessionManager 默认 root_dir 保持一致。
    agent_config = full_config.get("agent", {})
    root_dir = agent_config.get("root_dir") or "."
    _document_service = DocumentService(
        root_dir=root_dir,
        config=full_config.get("documents", {}),
    )


async def startup() -> None:
    """异步初始化（需要 await 的部分）。"""
    global _state_store
    if _state_store:
        await _state_store.initialize()
    if _session_manager:
        await _session_manager.start()


async def shutdown() -> None:
    """清理资源。"""
    global _state_store
    if _session_manager:
        await _session_manager.stop()
    if _state_store:
        await _state_store.close()
        _state_store = None


def get_state_store() -> SQLiteStateStore:
    """获取 StateStore 实例。"""
    if _state_store is None:
        raise RuntimeError("StateStore not initialized. Call init_services() first.")
    return _state_store


def get_session_manager() -> SessionManager:
    """获取 SessionManager 实例。"""
    if _session_manager is None:
        raise RuntimeError("SessionManager not initialized. Call init_services() first.")
    return _session_manager


def get_auth_service() -> AuthService:
    """获取 AuthService 实例。"""
    if _auth_service is None:
        raise RuntimeError("AuthService not initialized. Call init_services() first.")
    return _auth_service

def get_document_service() -> DocumentService:
    """获取 DocumentService 实例。"""
    if _document_service is None:
        raise RuntimeError("DocumentService not initialized. Call init_services() first.")
    return _document_service
