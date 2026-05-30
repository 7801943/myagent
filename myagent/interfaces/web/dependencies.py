"""
FastAPI 依赖注入：管理共享服务实例。

Harness 重构：
  - 移除 AgentFactory 引用
  - SessionManager 直接构建所有组件（ProviderRouter / ToolManager / SafetyGuard）
"""
from myagent.context.state import SQLiteStateStore
from myagent.core.session import SessionManager
from myagent.core.models import UserContext


# ── 全局单例（由 app.py lifespan 管理生命周期）──

_state_store: SQLiteStateStore | None = None
_session_manager: SessionManager | None = None


def init_services(config_path: str = "config.yaml") -> None:
    """初始化全局服务实例（在 lifespan startup 时调用）。"""
    global _state_store, _session_manager
    _state_store = SQLiteStateStore()
    _session_manager = SessionManager(config_path=config_path, state_store=_state_store)


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