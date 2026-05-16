"""MyAgent Core：ReAct 循环引擎 + Hook 体系 + 会话管理。

Phase 1 重构：
  - AgentLoop → Agent（loop 逻辑内联到 Agent）
  - 新增 Session（一等公民会话容器）
  - 新增 SessionManager（会话生命周期管理）
  - 新增 AgentFactory（从 myagent/factory.py 移入）
  - Turn 相关类型从 loop.py 移到 turns.py

Phase 2 增强：
  - 新增 WorkspaceManager（工作空间文件管理）
  - Session 集成 workspace / command_handler / TTL

Phase 3 重构：
  - 删除 permissions（权限管理将通过其他方式实现）
"""
from myagent.core.agent import Agent
from myagent.core.hook import HookContext, HookManager, HookHandle
from myagent.core.session import Session, SessionManager
from myagent.core.models import UserContext, SessionData
from myagent.core.agent import AgentFactory
from myagent.core.turns import BaseTurn, TurnKind, TurnResult, StreamResult
from myagent.core.workspace import WorkspaceManager

__all__ = [
    "Agent",
    "HookContext",
    "HookManager",
    "HookHandle",
    "Session",
    "SessionManager",
    "SessionData",
    "UserContext",
    "AgentFactory",
    "BaseTurn",
    "TurnKind",
    "TurnResult",
    "StreamResult",
    # Phase 2
    "WorkspaceManager",
]
