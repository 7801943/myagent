"""
HookManager + HookContext：完整的 Agent 生命周期钩子体系。
HookContext 携带 trace_id / span_id（V3 链路追踪）。
纯函数式回调注册。
"""
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from myagent.utils.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from myagent.tools.api import ToolResult

@dataclass
class HookContext:
    """传递给所有 Hook 方法，携带当前 Agent 执行状态。"""
    session_id: str
    # 用户：待废弃
    # agent_id: str = "main"
    # turn_id: str = field(default_factory=lambda: uuid4().hex[:12])
    # trace_id: str = ""              # V3: 全链路追踪
    # span_id: str = ""
    iteration: int = 0
    # 用户：待废弃
    # model: str = ""
    # provider: str = ""
    # tool_calls: list = field(default_factory=list)
    # tool_events: list = field(default_factory=list)
    # usage: dict = field(default_factory=dict)
    # Phase 2 新增 (用户：待废弃)
    # workspace_root: str | None = None          # 工作空间根目录（绝对路径）
    # active_file_path: str | None = None        # 当前活跃文件相对路径
    # 新增：会话级状态载体（替代 Agent._session_meta）
    session_meta: Any = None
    # 新增：会话级系统指令处理器（替代 Agent._system_command_handler）
    system_command_handler: Callable | None = None

    def snapshot(self) -> dict:
        """生成可序列化的上下文快照（供审计/错误记录使用）。"""
        return {
            "session_id": self.session_id,
            # 用户：待废弃
            # "agent_id": self.agent_id,
            # "turn_id": self.turn_id,
            # "trace_id": self.trace_id,
            # "span_id": self.span_id,
            "iteration": self.iteration,
            # "model": self.model,
            # "provider": self.provider,
            # "usage": self.usage,
            # "workspace_root": self.workspace_root,
            # "active_file_path": self.active_file_path,
        }

class HookHandle:
    """
    由 HookManager.on() 返回的句柄，可用于取消注册。
    用法：
        handle = agent.hooks.on("stream", my_callback, topic="sess_abc")
        handle.unregister()  # 取消注册
    """

    def __init__(self, manager: "HookManager", event: str, callback: Callable, topic: str | None = None):
        self._manager = manager
        self._event = event
        self._callback = callback
        self._topic = topic

    def unregister(self) -> None:
        """取消此回调的注册。"""
        topic_map = self._manager._listeners.get(self._event)
        if topic_map is None:
            return
        listeners = topic_map.get(self._topic)
        if listeners and self._callback in listeners:
            listeners.remove(self._callback)
            # 清理空 list 和空 dict，避免内存泄漏
            if not listeners:
                topic_map.pop(self._topic, None)
            if not topic_map:
                self._manager._listeners.pop(self._event, None)

class HookManager:
    """
    事件分发器。支持函数式回调注册 + Topic 路由。
    绑定到 Agent 实例，非全局单例。

    Topic 机制：
        注册时指定 topic（如 session_id），emit 时自动按 ctx.session_id 路由。
        回调只会收到匹配 topic 或全局（topic=None）的事件，
        彻底消除 _for_this(ctx) 过滤代码。

    用法：
        # 带 topic 注册（per-session 隔离）
        handle = agent.hooks.on("stream", my_callback, topic="sess_abc")

        # 全局注册（topic=None，接收所有事件）
        handle = agent.hooks.on("error", global_logger)

        # 装饰器注册（全局）
        @agent.hooks.hook("stream")
        async def my_stream_handler(ctx, delta):
            print(delta)

        # 取消注册
        handle.unregister()
    """

    def __init__(self):
        # 结构: { event_name: { topic: [callbacks] } }
        # topic 为 None 代表全局监听
        self._listeners: dict[str, dict[str | None, list[Callable]]] = defaultdict(
            lambda: defaultdict(list)
        )

    def on(self, event: str, callback: Callable, topic: str | None = None) -> HookHandle:
        """注册单个事件回调，返回可取消的 Handle。"""
        self._listeners[event][topic].append(callback)
        return HookHandle(self, event, callback, topic)

    def hook(self, event: str, topic: str | None = None) -> Callable:
        """
        装饰器，用于注册事件回调函数。
        用法：
            @agent.hooks.hook("stream")
            async def my_stream_handler(ctx, delta):
                pass
        """
        def decorator(func: Callable) -> Callable:
            self.on(event, func, topic=topic)
            return func
        return decorator

    async def emit(self, event: str, ctx: HookContext, **kwargs) -> None:
        """
        触发事件，按 ctx.session_id 路由到对应 topic 的回调 + 全局回调。
        """
        topic = ctx.session_id
        topic_map = self._listeners.get(event)
        if not topic_map:
            return

        # 1. 匹配当前 topic 的回调
        # 2. 匹配全局（topic=None）的回调
        callbacks = list(topic_map.get(topic, [])) + list(topic_map.get(None, []))

        for cb in callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(ctx, **kwargs)
                else:
                    cb(ctx, **kwargs)
            except Exception:
                logger.exception("Hook callback failed for event '%s'", event)

    def wants_streaming(self) -> bool:
        """检查是否有任何监听者需要流式回调（跨所有 topic）。"""
        stream_map = self._listeners.get("stream")
        thinking_map = self._listeners.get("thinking_stream")
        has_stream = any(bool(cbs) for cbs in stream_map.values()) if stream_map else False
        has_thinking = any(bool(cbs) for cbs in thinking_map.values()) if thinking_map else False
        return has_stream or has_thinking

    def finalize_content(self, ctx: HookContext, content: str | None) -> str | None:
        """对最终输出内容进行后处理（同步）。按 topic 路由。"""
        topic_map = self._listeners.get("finalize_content")
        if not topic_map:
            return content

        topic = ctx.session_id
        callbacks = list(topic_map.get(topic, [])) + list(topic_map.get(None, []))

        for cb in callbacks:
            try:
                res = cb(ctx, content)
                if res is not None:
                    content = res
            except Exception:
                logger.exception("Hook callback failed for event 'finalize_content'")
        return content
