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
    client_state: dict[str, Any] | None = Field(
        None,
        description="前端运行态快照，如 workspace/model/tools。",
    )


class CancelMessage(BaseModel):
    """取消当前执行。"""
    type: Literal["cancel"] = "cancel"


class HitlResponseMessage(BaseModel):
    """HITL 审批回复。"""
    type: Literal["hitl_response"] = "hitl_response"
    ticket_id: str = Field(..., min_length=1, description="审批工单 ID")
    approved: bool = Field(False, description="是否批准")


class SafetyPolicySetMessage(BaseModel):
    """切换当前会话的 CLI 安全策略。"""
    type: Literal["safety_policy_set"] = "safety_policy_set"
    policy: str = Field(..., min_length=1, description="CLI 安全策略名称")


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
    ticket_id: str = ""
    tool_name: str = ""
    reason: str = ""
    args: Any = None
    call_id: str = ""
    timeout_seconds: float = 0


class TimeoutWarningMessage(ServerMessage):
    type: str = "timeout_warning"
    stage: str = ""
    timeout_seconds: float = 0
    message: str = ""


class PongMessage(ServerMessage):
    type: str = "pong"


class WorkspaceStateMessage(ServerMessage):
    """工作空间状态推送（完整快照）。"""
    type: str = "workspace_state"
    root_path: str = ""
    files: list[dict] = []
    open_files: list[dict] = []
    active_file_index: int | None = None
    expanded_dirs: list[str] = []


class ConversationStateMessage(ServerMessage):
    """会话全局状态推送（聚合快照：模型、Token、工具、Agent 状态等）。"""
    type: str = "conversation_state"
    user_id: str = ""
    username: str = ""
    active_model: dict = {}
    available_models: list[dict] = []
    token_usage: dict = {}
    tools: list[dict] = []
    agent_run_state: str = "idle"
    session_state: str = "active"
    workspace_state: dict | None = None


# ── 客户端 → 服务端 workspace 消息 ──

class WorkspaceOpenFileMessage(BaseModel):
    """前端打开文件。"""
    type: Literal["workspace_open_file"] = "workspace_open_file"
    path: str = Field(..., description="文件相对路径")


class WorkspaceCloseFileMessage(BaseModel):
    """前端关闭文件 Tab。"""
    type: Literal["workspace_close_file"] = "workspace_close_file"
    index: int = Field(..., description="Tab 索引")


class WorkspaceSetActiveFileMessage(BaseModel):
    """前端切换活跃文件。"""
    type: Literal["workspace_set_active_file"] = "workspace_set_active_file"
    index: int = Field(..., description="Tab 索引")


class WorkspaceSetRootMessage(BaseModel):
    """前端设置工作空间根目录。"""
    type: Literal["workspace_set_root"] = "workspace_set_root"
    path: str = Field(..., description="工作空间根目录绝对路径")


class WorkspaceRefreshMessage(BaseModel):
    """前端请求刷新工作空间文件列表。"""
    type: Literal["workspace_refresh"] = "workspace_refresh"


class WorkspaceScanDirMessage(BaseModel):
    """前端请求扫描指定子目录（一层）。"""
    type: Literal["workspace_scan_dir"] = "workspace_scan_dir"
    path: str = Field(..., description="要扫描的子目录相对路径")


class WorkspaceCollapseDirMessage(BaseModel):
    """前端折叠目录（从 expanded_dirs 移除）。"""
    type: Literal["workspace_collapse_dir"] = "workspace_collapse_dir"
    path: str = Field(..., description="要折叠的子目录相对路径")


# ── 消息类型映射（用于解析）──

INCOMING_MESSAGE_TYPES: dict[str, type[BaseModel]] = {
    "chat": ChatMessage,
    "cancel": CancelMessage,
    "hitl_response": HitlResponseMessage,
    "safety_policy_set": SafetyPolicySetMessage,
    "session_create": SessionCreateMessage,
    "session_switch": SessionSwitchMessage,
    "session_delete": SessionDeleteMessage,
    "session_list": SessionListMessage,
    "ping": PingMessage,
    # Phase 2: workspace 消息
    "workspace_open_file": WorkspaceOpenFileMessage,
    "workspace_close_file": WorkspaceCloseFileMessage,
    "workspace_set_active_file": WorkspaceSetActiveFileMessage,
    "workspace_set_root": WorkspaceSetRootMessage,
    "workspace_refresh": WorkspaceRefreshMessage,
    "workspace_scan_dir": WorkspaceScanDirMessage,
    "workspace_collapse_dir": WorkspaceCollapseDirMessage,
}
