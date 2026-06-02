"""MyAgent Context：消息管理与状态存储。"""
# [FIX] ToolResult → ToolResultMessage，消除与 api.py:ToolResult 的二义性
from myagent.context.message import Message, ContentBlock, ToolCall, ToolResultMessage
from myagent.context.manager import ContextManager
from myagent.context.state import StateStore

__all__ = [
    "Message", "ContentBlock", "ToolCall", "ToolResultMessage",
    "ContextManager", "StateStore",
]
