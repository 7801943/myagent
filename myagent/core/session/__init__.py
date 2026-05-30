"""
Session 包：会话容器 + 管理器 + WS 桥接 + 序列化。

对外接口（通过 __init__.py 重导出，外部导入不变）：
  from myagent.core.session import Session, SessionManager
  from myagent.core.session import ClientBridge, ClientHandle
"""
from myagent.core.session.session import Session
from myagent.core.session.manager import SessionManager
from myagent.core.session.client_bridge import ClientBridge, ClientHandle

__all__ = [
    "Session",
    "SessionManager",
    "ClientBridge",
    "ClientHandle",
]
