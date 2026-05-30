"""
类型安全事件定义。

核心原则：
  - 有因果依赖、需要返回值的交互走 **直接调用**
  - 无因果关系、发了就忘的通知走 **EventBus**（未来替换 HookManager）

本文件定义的事件类型供 EventBus 发布/订阅使用。
当前阶段（步骤 1）仅为定义，尚未接入实际事件流。
待步骤 9（HookManager → EventBus）时全面启用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ─── 基类 ──────────────────────────────────────────────────

@dataclass
class Event:
    """所有事件的基类。"""
    session_id: str = ""


# ─── LLM 相关事件 ──────────────────────────────────────────

@dataclass
class StreamDelta(Event):
    """LLM 流式文本增量。"""
    delta: str = ""


@dataclass
class ThinkingDelta(Event):
    """LLM 思考过程增量（extended thinking）。"""
    delta: str = ""


@dataclass
class StreamStart(Event):
    """LLM 流式输出开始。"""
    pass


@dataclass
class StreamEnd(Event):
    """LLM 流式输出结束。"""
    resuming: bool = False


# ─── 工具相关事件 ──────────────────────────────────────────

@dataclass
class ToolStart(Event):
    """工具开始执行。"""
    tool_name: str = ""
    args: dict = field(default_factory=dict)
    call_id: str = ""


@dataclass
class ToolEnd(Event):
    """工具执行完成。"""
    tool_name: str = ""
    result: Any = None
    call_id: str = ""
    latency_ms: float = 0.0


@dataclass
class ToolError(Event):
    """工具执行出错。"""
    tool_name: str = ""
    error: str = ""
    call_id: str = ""


# ─── 状态变更事件 ──────────────────────────────────────────

@dataclass
class StateChange(Event):
    """Agent 运行状态变更（thinking / generating / idle 等）。"""
    state: str = ""


@dataclass
class Error(Event):
    """运行时错误。"""
    error: str = ""


# ─── 安全事件 ──────────────────────────────────────────────

@dataclass
class SafetyBlocked(Event):
    """安全规则拦截。"""
    rule: str = ""
    reason: str = ""
    action: str = ""
    call_id: str = ""
    tool_name: str = ""


@dataclass
class TimeoutWarning(Event):
    """超时警告。"""
    message: str = ""
    elapsed_ms: float = 0.0


# ─── 审批事件 ──────────────────────────────────────────────

@dataclass
class ApprovalNeeded(Event):
    """工具需要人工审批。"""
    tool_calls: list = field(default_factory=list)


# ─── 系统事件 ──────────────────────────────────────────────

@dataclass
class SystemCommand(Event):
    """系统指令（/new, /model 等）。"""
    command: str = ""
    args: str = ""