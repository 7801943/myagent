"""MyAgent Core：ReAct 循环引擎 + Hook 体系 + 会话管理。

Phase 1 重构：
  - AgentLoop → Agent（loop 逻辑内联到 Agent）
  - 新增 Session（一等公民会话容器）
  - 新增 SessionManager（会话生命周期管理）
  - 新增 AgentFactory（从 myagent/factory.py 移入）
  - Turn 相关类型从 loop.py 移到 turns.py
"""
from myagent.core.agent import Agent
from myagent.core.hook import HookContext, HookManager, HookHandle
from myagent.core.session import Session
from myagent.core.session_manager import SessionManager, UserContext
from myagent.core.factory import AgentFactory
from myagent.core.turns import BaseTurn, TurnKind, TurnResult, StreamResult

__all__ = [
    "Agent",
    "HookContext",
    "HookManager",
    "HookHandle",
    "Session",
    "SessionManager",
    "UserContext",
    "AgentFactory",
    "BaseTurn",
    "TurnKind",
    "TurnResult",
    "StreamResult",
]
