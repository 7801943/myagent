"""类型安全事件系统。

核心原则：
  - 有因果依赖、需要返回值的交互走直接调用
  - 无因果关系、发了就忘的通知走 EventBus

EventBus 替代旧 HookManager，负责 Agent 生命周期通知的发布/订阅。
它仍保留少量旧字符串 hook 兼容能力，方便外部代码平滑迁移。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, Callable, TypeVar

from myagent.utils.logging import get_logger

logger = get_logger(__name__)

E = TypeVar("E", bound="Event")


# ─── 执行上下文 ──────────────────────────────────────────────

@dataclass
class ExecutionContext:
    """单次 Agent 执行的轻量上下文。"""
    session_id: str
    iteration: int = 0
    # 兼容旧 HookContext 字段；新代码不依赖它们。
    agent_id: str = "main"
    session_meta: Any = None
    system_command_handler: Callable | None = None

    def snapshot(self) -> dict:
        """生成可序列化的上下文快照。"""
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "iteration": self.iteration,
        }

    def event(self, event_type: type[E], **kwargs: Any) -> E:
        """基于当前上下文创建事件对象。"""
        return event_type(
            session_id=self.session_id,
            iteration=self.iteration,
            **kwargs,
        )


# ─── 事件基类 ────────────────────────────────────────────────

@dataclass
class Event:
    """所有事件的基类。"""
    session_id: str = ""
    iteration: int = 0
    _context: ExecutionContext | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def context(self) -> ExecutionContext | None:
        return self._context

    def payload(self) -> dict[str, Any]:
        """转换为旧 hook 回调可消费的 kwargs。"""
        return {
            key: value
            for key, value in self.__dict__.items()
            if not key.startswith("_") and key not in {"session_id", "iteration"}
        }


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
    error: Any = ""
    call_id: str = ""


# ─── 状态变更事件 ──────────────────────────────────────────

@dataclass
class StateChange(Event):
    """Agent 运行状态变更（thinking / generating / idle 等）。"""
    state: str = ""


@dataclass
class Error(Event):
    """运行时错误。"""
    error: Any = ""


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


# ─── EventBus ────────────────────────────────────────────────

EVENT_NAME_TO_TYPE: dict[str, type[Event]] = {
    "stream": StreamDelta,
    "thinking_stream": ThinkingDelta,
    "stream_start": StreamStart,
    "stream_end": StreamEnd,
    "tool_start": ToolStart,
    "tool_end": ToolEnd,
    "tool_error": ToolError,
    "state_change": StateChange,
    "error": Error,
    "safety_blocked": SafetyBlocked,
    "timeout_warning": TimeoutWarning,
    "approval_needed": ApprovalNeeded,
    "system_command": SystemCommand,
}

EVENT_TYPE_TO_NAME: dict[type[Event], str] = {
    event_type: name for name, event_type in EVENT_NAME_TO_TYPE.items()
}


class EventHandle:
    """订阅句柄，可用于取消注册。"""

    def __init__(
        self,
        bus: "EventBus",
        key: type[Event] | str,
        callback: Callable,
        topic: str | None = None,
        *,
        legacy: bool = False,
    ):
        self._bus = bus
        self._key = key
        self._callback = callback
        self._topic = topic
        self._legacy = legacy

    def unregister(self) -> None:
        """取消此回调的注册。"""
        listeners = self._bus._legacy_listeners if self._legacy else self._bus._listeners
        topic_map = listeners.get(self._key)
        if topic_map is None:
            return
        callbacks = topic_map.get(self._topic)
        if callbacks and self._callback in callbacks:
            callbacks.remove(self._callback)
            if not callbacks:
                topic_map.pop(self._topic, None)
            if not topic_map:
                listeners.pop(self._key, None)


class EventBus:
    """
    类型化事件分发器，支持 topic=session_id 路由。

    新代码应使用：
        bus.on(StreamDelta, callback)
        await bus.publish(StreamDelta(session_id="...", delta="hi"))

    为迁移期保留：
        bus.on("stream", old_hook_callback)
        await bus.emit("stream", ctx, delta="hi")
    """

    def __init__(self):
        self._listeners: dict[type[Event], dict[str | None, list[Callable]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._legacy_listeners: dict[str, dict[str | None, list[Callable]]] = defaultdict(
            lambda: defaultdict(list)
        )

    def on(
        self,
        event_type: type[E] | str,
        callback: Callable[[E], Any] | Callable,
        topic: str | None = None,
    ) -> EventHandle:
        """注册事件回调，返回可取消的句柄。"""
        if isinstance(event_type, str):
            self._legacy_listeners[event_type][topic].append(callback)
            return EventHandle(self, event_type, callback, topic, legacy=True)

        self._listeners[event_type][topic].append(callback)
        return EventHandle(self, event_type, callback, topic)

    def hook(self, event_type: type[E] | str, topic: str | None = None) -> Callable:
        """装饰器形式的 on()。"""
        def decorator(func: Callable) -> Callable:
            self.on(event_type, func, topic=topic)
            return func
        return decorator

    async def publish(self, event: Event, ctx: ExecutionContext | None = None) -> None:
        """发布类型化事件。"""
        if ctx is not None:
            event._context = ctx
            if not event.session_id:
                event.session_id = ctx.session_id
            if not event.iteration:
                event.iteration = ctx.iteration

        topic = event.session_id
        for callback in self._typed_callbacks(type(event), topic):
            await self._invoke(callback, event)

        legacy_name = EVENT_TYPE_TO_NAME.get(type(event))
        if legacy_name:
            legacy_ctx = event.context or ExecutionContext(
                session_id=event.session_id,
                iteration=event.iteration,
            )
            payload = event.payload()
            for callback in self._legacy_callbacks(legacy_name, topic):
                await self._invoke_legacy(callback, legacy_ctx, payload, legacy_name)

    async def emit(
        self,
        event: Event | str,
        ctx: ExecutionContext | None = None,
        **kwargs: Any,
    ) -> None:
        """兼容旧 HookManager.emit，同时也接受事件对象。"""
        if isinstance(event, Event):
            await self.publish(event, ctx=ctx)
            return

        event_type = EVENT_NAME_TO_TYPE.get(event)
        if event_type is None:
            if ctx is None:
                return
            for callback in self._legacy_callbacks(event, ctx.session_id):
                await self._invoke_legacy(callback, ctx, kwargs, event)
            return

        event_obj = event_type(**kwargs)
        await self.publish(event_obj, ctx=ctx)

    def wants_streaming(self) -> bool:
        """检查是否存在流式内容监听者。"""
        return (
            self.has_listeners(StreamDelta)
            or self.has_listeners(ThinkingDelta)
            or self.has_legacy_listeners("stream")
            or self.has_legacy_listeners("thinking_stream")
        )

    def has_listeners(self, event_type: type[Event]) -> bool:
        topic_map = self._listeners.get(event_type)
        return any(bool(cbs) for cbs in topic_map.values()) if topic_map else False

    def has_legacy_listeners(self, name: str) -> bool:
        topic_map = self._legacy_listeners.get(name)
        return any(bool(cbs) for cbs in topic_map.values()) if topic_map else False

    def finalize_content(self, ctx: ExecutionContext, content: str | None) -> str | None:
        """迁移期兼容旧 finalize_content hook；新逻辑应使用直接调用。"""
        callbacks = self._legacy_callbacks("finalize_content", ctx.session_id)
        for callback in callbacks:
            try:
                result = callback(ctx, content)
                if result is not None:
                    content = result
            except Exception:
                logger.exception("Legacy finalize_content callback failed")
        return content

    def _typed_callbacks(self, event_type: type[Event], topic: str | None) -> list[Callable]:
        callbacks: list[Callable] = []
        for cls in (*event_type.__mro__,):
            if not isinstance(cls, type) or not issubclass(cls, Event):
                continue
            topic_map = self._listeners.get(cls)
            if topic_map:
                callbacks.extend(topic_map.get(topic, []))
                callbacks.extend(topic_map.get(None, []))
            if cls is Event:
                break
        return callbacks

    def _legacy_callbacks(self, name: str, topic: str | None) -> list[Callable]:
        topic_map = self._legacy_listeners.get(name)
        if not topic_map:
            return []
        return list(topic_map.get(topic, [])) + list(topic_map.get(None, []))

    async def _invoke(self, callback: Callable, event: Event) -> None:
        try:
            result = callback(event)
            if isawaitable(result):
                await result
        except Exception:
            logger.exception("Event callback failed for %s", type(event).__name__)

    async def _invoke_legacy(
        self,
        callback: Callable,
        ctx: ExecutionContext,
        payload: dict[str, Any],
        event_name: str,
    ) -> None:
        try:
            result = callback(ctx, **payload)
            if isawaitable(result):
                await result
        except Exception:
            logger.exception("Legacy hook callback failed for event '%s'", event_name)


# 兼容旧命名。新代码使用 ExecutionContext / EventBus / EventHandle。
HookContext = ExecutionContext
HookManager = EventBus
HookHandle = EventHandle
