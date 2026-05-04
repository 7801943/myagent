"""
AuditLogger：审计日志管理器。
将 AgentHook 生命周期事件转换为 AuditEvent 并写入后端。
"""
from myagent.observability.events import AuditEvent, EventType
from myagent.observability.backends.base import BaseAuditBackend
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class AuditLogger:
    """审计日志管理器：提供简单的 emit() 接口。"""

    def __init__(self, backend: BaseAuditBackend):
        self._backend = backend

    async def emit(
        self,
        event_type: EventType,
        session_id: str = "",
        turn_id: str = "",
        agent_id: str = "",
        trace_id: str = "",
        span_id: str = "",
        iteration: int = 0,
        **extra_data,
    ) -> None:
        """发射一条审计事件。"""
        event = AuditEvent(
            event_type=event_type,
            session_id=session_id,
            turn_id=turn_id,
            agent_id=agent_id,
            trace_id=trace_id,
            span_id=span_id,
            iteration=iteration,
            data=extra_data,
        )
        try:
            await self._backend.write(event)
        except Exception as e:
            logger.warning(f"Audit write failed: {e}")

    # -- Phase 2 便捷方法 --

    async def emit_safety(
        self,
        decision: str,
        tool_name: str = "",
        rule_name: str = "",
        reason: str = "",
        session_id: str = "",
        **extra,
    ) -> None:
        """发射安全事件。"""
        event_type = (
            EventType.SAFETY_REWRITE if decision == "rewrite"
            else EventType.SAFETY_BLOCKED
        )
        # 鲁棒性处理：从 extra 中提取 session_id 避免冲突
        extra_copy = extra.copy()
        s_id = session_id or extra_copy.pop("session_id", "")

        await self.emit(
            event_type=event_type,
            session_id=s_id,
            tool_name=tool_name,
            decision=decision,
            rule_name=rule_name,
            reason=reason,
            **extra_copy,
        )

    async def emit_hitl(
        self,
        action: str,  # "request" | "approved" | "rejected"
        tool_name: str = "",
        reason: str = "",
        session_id: str = "",
        **extra,
    ) -> None:
        """发射 HITL 审批事件。"""
        event_type_map = {
            "request": EventType.HITL_REQUEST,
            "approved": EventType.HITL_APPROVED,
            "rejected": EventType.HITL_REJECTED,
        }
        event_type = event_type_map.get(action, EventType.HITL_REQUEST)
        # 鲁棒性处理
        extra_copy = extra.copy()
        s_id = session_id or extra_copy.pop("session_id", "")

        await self.emit(
            event_type=event_type,
            session_id=s_id,
            tool_name=tool_name,
            reason=reason,
            **extra_copy,
        )

    async def emit_timeout(
        self,
        stage: str,
        timeout_seconds: float,
        session_id: str = "",
        **extra,
    ) -> None:
        """发射超时警告事件。"""
        # 鲁棒性处理
        extra_copy = extra.copy()
        s_id = session_id or extra_copy.pop("session_id", "")

        await self.emit(
            EventType.TIMEOUT_WARNING,
            session_id=s_id,
            stage=stage,
            timeout_seconds=timeout_seconds,
            **extra_copy,
        )

    async def emit_cancelled(
        self,
        reason: str,
        detail: str = "",
        session_id: str = "",
        **extra,
    ) -> None:
        """发射取消事件。"""
        # 鲁棒性处理
        extra_copy = extra.copy()
        s_id = session_id or extra_copy.pop("session_id", "")

        await self.emit(
            EventType.AGENT_CANCELLED,
            session_id=s_id,
            reason=reason,
            detail=detail,
            **extra_copy,
        )

    async def log_event(self, event_type: str, data: dict, session_id: str = "") -> None:
        """
        便捷方法：供 AgentLoop 内联调用。
        event_type 为字符串，自动映射到 EventType。
        """
        # 尝试映射字符串到 EventType
        try:
            et = EventType(event_type)
        except ValueError:
            et = EventType.ERROR  # 未知事件类型回退

        # 从 data 中提取标准字段，避免与显式参数冲突
        data_copy = data.copy()
        # 必须总是执行 pop，否则如果 session_id 参数有值，pop 就不会执行，导致重复传参
        s_id = data_copy.pop("session_id", "")
        if session_id:
            s_id = session_id
            
        t_id = data_copy.pop("turn_id", "")
        a_id = data_copy.pop("agent_id", "")
        tr_id = data_copy.pop("trace_id", "")
        sp_id = data_copy.pop("span_id", "")
        iter_val = data_copy.pop("iteration", 0)

        await self.emit(
            et,
            session_id=s_id,
            turn_id=t_id,
            agent_id=a_id,
            trace_id=tr_id,
            span_id=sp_id,
            iteration=iter_val,
            **data_copy
        )

    async def flush(self) -> None:
        await self._backend.flush()

    async def close(self) -> None:
        await self._backend.close()
