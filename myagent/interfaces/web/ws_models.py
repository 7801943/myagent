"""
WebSocket 消息模型：类型安全的 Pydantic 消息协议。
用于自动校验客户端发来的 JSON 消息格式。

未来扩展：
  - [AUTH] 可在消息模型中携带 auth token / user_id 字段
  - [MCP] 可添加 MCP 协议相关的消息类型
"""
from typing import Any, Literal
from pydantic import BaseModel, Field


# ── 客户端 → 服务端消息 ──

class ChatMessage(BaseModel):
    """聊天消息。"""
    type: Literal["chat"] = "chat"
    text: str = Field(..., min_length=1, description="用户输入文本")


class CancelMessage(BaseModel):
    """取消当前执行。"""
    type: Literal["cancel"] = "cancel"


class HitlResponseMessage(BaseModel):
    """HITL 审批回复。"""
    type: Literal["hitl_response"] = "hitl_response"
    call_id: str = Field(..., description="工具调用 ID")
    approved: bool = Field(False, description="是否批准")


class SessionCreateMessage(BaseModel):
    """创建新会话。"""
    type: Literal["session_create"] = "session_create"


class SessionSwitchMessage(BaseModel):
    """切换到指定会话。"""
    type: Literal["session_switch"] = "session_switch"
    session_id: str = Field(..., description="目标会话 ID")


class SessionDeleteMessage(BaseModel):
    """删除指定会话。"""
    type: Literal["session_delete"] = "session_delete"
    session_id: str = Field(..., description="要删除的会话 ID")


class SessionListMessage(BaseModel):
    """请求会话列表。"""
    type: Literal["session_list"] = "session_list"


class PingMessage(BaseModel):
    """心跳检测。"""
    type: Literal["ping"] = "ping"


# ── 服务端 → 客户端消息（用于类型参考，不强制校验）──

class ServerMessage(BaseModel):
    """服务端消息基类。"""
    type: str


class TextDeltaMessage(ServerMessage):
    type: str = "text_delta"
    text: str = ""


class ThinkingDeltaMessage(ServerMessage):
    type: str = "thinking_delta"
    text: str = ""


class StreamStartMessage(ServerMessage):
    type: str = "stream_start"


class StreamEndMessage(ServerMessage):
    type: str = "stream_end"
    resuming: bool = False


class ToolStartMessage(ServerMessage):
    type: str = "tool_start"
    tool_name: str = ""
    args: Any = None
    call_id: str = ""


class ToolEndMessage(ServerMessage):
    type: str = "tool_end"
    tool_name: str = ""
    result: Any = None
    latency_ms: float = 0
    call_id: str = ""


class ToolErrorMessage(ServerMessage):
    type: str = "tool_error"
    tool_name: str = ""
    error: str = ""
    call_id: str = ""


class SafetyBlockedMessage(ServerMessage):
    type: str = "safety_blocked"
    rule: str = ""
    reason: str = ""
    action: str = ""
    call_id: str = ""
    tool_name: str = ""


class StateChangeMessage(ServerMessage):
    type: str = "state_change"
    state: str = ""


class ErrorMessage(ServerMessage):
    type: str = "error"
    message: str = ""


class MessageEndMessage(ServerMessage):
    type: str = "message_end"
    text: str = ""
    stop_reason: str = "completed"


class ConnectedMessage(ServerMessage):
    type: str = "connected"
    session_id: str = ""


class SessionListResultMessage(ServerMessage):
    type: str = "session_list_result"
    sessions: list[dict] = []


class SessionCreatedMessage(ServerMessage):
    type: str = "session_created"
    session_id: str = ""


class SessionSwitchedMessage(ServerMessage):
    type: str = "session_switched"
    session_id: str = ""
    messages: list[dict] = []


class SessionDeletedMessage(ServerMessage):
    type: str = "session_deleted"
    session_id: str = ""


class HitlRequestMessage(ServerMessage):
    type: str = "hitl_request"
    tool_name: str = ""
    reason: str = ""
    args: Any = None
    call_id: str = ""


class TimeoutWarningMessage(ServerMessage):
    type: str = "timeout_warning"
    stage: str = ""
    timeout_seconds: float = 0
    message: str = ""


class PongMessage(ServerMessage):
    type: str = "pong"


# ── 消息类型映射（用于解析）──

INCOMING_MESSAGE_TYPES: dict[str, type[BaseModel]] = {
    "chat": ChatMessage,
    "cancel": CancelMessage,
    "hitl_response": HitlResponseMessage,
    "session_create": SessionCreateMessage,
    "session_switch": SessionSwitchMessage,
    "session_delete": SessionDeleteMessage,
    "session_list": SessionListMessage,
    "ping": PingMessage,
}