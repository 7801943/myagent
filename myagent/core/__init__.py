"""MyAgent Core：ReAct 调度中枢 + Hook 体系 + 会话管理。

重构后文件结构：
  - core/harness.py: AgentHarness（无状态执行引擎，per-session）
  - core/llm.py: LLMClient（LLM 通信层）
  - core/tools.py: ToolInterface（工具执行适配层）
  - core/models.py: 数据容器（SessionData, SessionState 等）
  - core/session/: 会话包
      session.py: Session（瘦会话容器）
      manager.py: SessionManager（组件构建 + TTL）
      client_bridge.py: ClientBridge（WS 多客户端管理 + 审批桥接）
      serializer.py: 消息序列化纯函数
  - core/hook.py: HookManager + HookContext
  - core/events.py: 类型安全事件定义
  - core/workspace.py: WorkspaceManager
"""
from myagent.core.harness import AgentHarness
from myagent.core.llm import LLMClient, StreamResult
from myagent.core.tools import ToolInterface
from myagent.core.hook import HookContext, HookManager, HookHandle
from myagent.core.session import Session, SessionManager
from myagent.core.session.client_bridge import ClientBridge, ClientHandle
from myagent.core.models import UserContext, SessionData
from myagent.core.workspace import WorkspaceManager
from myagent.core import events

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
    "ClientBridge",
    "ClientHandle",
    "SessionData",
    "UserContext",
    "WorkspaceManager",
    "events",
]
