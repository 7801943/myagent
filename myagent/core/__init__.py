"""MyAgent Core：ReAct 调度中枢 + Hook 体系 + 会话管理。

Harness 重构后：
  - core/harness.py: AgentHarness（ReAct 调度中枢）
  - core/llm.py: LLMClient（LLM 通信层，屏蔽流式/路由/参数格式化细节）
  - core/tools.py: ToolInterface（工具执行适配层）
  - core/models.py: 数据容器（StreamResult, SessionState 等）
  - core/session.py: Session + SessionManager（会话管理，不再依赖 AgentFactory）
  - core/hook.py: HookManager + HookContext
  - core/workspace.py: WorkspaceManager

已删除：
  - agent.py（逻辑迁移至 harness.py + llm.py + tools.py）
  - turns.py（Turn 抽象扁平化到 harness.py 中）
"""
from myagent.core.harness import AgentHarness
from myagent.core.llm import LLMClient, StreamResult
from myagent.core.tools import ToolInterface
from myagent.core.hook import HookContext, HookManager, HookHandle
from myagent.core.session import Session, SessionManager
from myagent.core.models import UserContext, SessionData
from myagent.core.workspace import WorkspaceManager

__all__ = [
    "AgentHarness",
    "LLMClient",
    "StreamResult",
    "ToolInterface",
    "HookContext",
    "HookManager",
    "HookHandle",
    "Session",
    "SessionManager",
    "SessionData",
    "UserContext",
    "WorkspaceManager",
]