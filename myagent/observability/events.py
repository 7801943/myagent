"""
AuditEvent：审计事件数据模型。
所有可观测性事件均通过 AuditEvent 记录。
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time

class EventType(str, Enum):
    """事件类型枚举。"""
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"
    PROVIDER_CALL_START = "provider_call_start"
    PROVIDER_CALL_END = "provider_call_end"
    PROVIDER_FAILOVER = "provider_failover"
    STREAM_START = "stream_start"
    STREAM_DELTA = "stream_delta"
    STREAM_END = "stream_end"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    TOOL_ERROR = "tool_error"
    SAFETY_BLOCKED = "safety_blocked"
    SAFETY_REWRITE = "safety_rewrite"
    HITL_REQUEST = "hitl_request"
    HITL_APPROVED = "hitl_approved"
    HITL_REJECTED = "hitl_rejected"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_END = "subagent_end"
    ERROR = "error"
    TIMEOUT_WARNING = "timeout_warning"
    AGENT_CANCELLED = "agent_cancelled"

@dataclass
class AuditEvent:
    """审计事件。"""
    event_type: EventType
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    turn_id: str = ""
    agent_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    iteration: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "agent_id": self.agent_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "iteration": self.iteration,
            "data": self.data,
        }