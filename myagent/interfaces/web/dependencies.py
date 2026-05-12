"""
FastAPI 依赖注入：管理共享服务实例。

Phase 1 变更：
  - 导入路径 myagent.factory → myagent.core.factory
  - AgentFactory 不再接受 state_store 参数
"""
from myagent.context.state import SQLiteStateStore
from myagent.core.agent import AgentFactory
from myagent.core.session import SessionManager, UserContext


# ── 全局单例（由 app.py lifespan 管理生命周期）──

_state_store: SQLiteStateStore | None = None
_agent_factory: AgentFactory | None = None
_session_manager: SessionManager | None = None


def init_services(config_path: str = "config.yaml") -> None:
    """初始化全局服务实例（在 lifespan startup 时调用）。"""
    global _state_store, _agent_factory, _session_manager
    _state_store = SQLiteStateStore()
    _agent_factory = AgentFactory(config_path=config_path)
    _session_manager = SessionManager(factory=_agent_factory, state_store=_state_store)


async def startup() -> None:
    """异步初始化（需要 await 的部分）。"""
    global _state_store
    if _state_store:
        await _state_store.initialize()


async def shutdown() -> None:
    """清理资源。"""
    global _state_store
    if _state_store:
        await _state_store.close()
        _state_store = None


def get_state_store() -> SQLiteStateStore:
    """获取 StateStore 实例。"""
    if _state_store is None:
        raise RuntimeError("StateStore not initialized. Call init_services() first.")
    return _state_store


def get_agent_factory() -> AgentFactory:
    """获取 AgentFactory 实例。"""
    if _agent_factory is None:
        raise RuntimeError("AgentFactory not initialized. Call init_services() first.")
    return _agent_factory


def get_session_manager() -> SessionManager:
    """获取 SessionManager 实例。"""
    if _session_manager is None:
        raise RuntimeError("SessionManager not initialized. Call init_services() first.")
    return _session_manager