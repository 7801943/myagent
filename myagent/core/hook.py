"""
HookManager + HookContext：完整的 Agent 生命周期钩子体系。
HookContext 携带 trace_id / span_id（V3 链路追踪）。
纯函数式回调注册。
"""
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable
from uuid import uuid4

from myagent.utils.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from myagent.tools.api import ToolResult

@dataclass
class HookContext:
    """传递给所有 Hook 方法，携带当前 Agent 执行状态。"""
    session_id: str
    agent_id: str = "main"
    turn_id: str = field(default_factory=lambda: uuid4().hex[:12])
    trace_id: str = ""              # V3: 全链路追踪
    span_id: str = ""
    iteration: int = 0
    model: str = ""
    provider: str = ""
    tool_calls: list = field(default_factory=list)
    tool_events: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    # Phase 2 新增
    workspace_root: str | None = None          # 工作空间根目录（绝对路径）
    active_file_path: str | None = None        # 当前活跃文件相对路径
    user_permissions: list[str] = field(default_factory=list)

    def snapshot(self) -> dict:
        """生成可序列化的上下文快照（供审计/错误记录使用）。"""
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "turn_id": self.turn_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "iteration": self.iteration,
            "model": self.model,
            "provider": self.provider,
            "usage": self.usage,
            "workspace_root": self.workspace_root,
            "active_file_path": self.active_file_path,
        }

class HookHandle:
    """
    由 HookManager.on() 返回的句柄，可用于取消注册。
    用法：
        handle = agent.hooks.on("stream", my_callback)
        handle.unregister()  # 取消注册
    """

    def __init__(self, manager: "HookManager", event: str, callback: Callable):
        self._manager = manager
        self._event = event
        self._callback = callback

    def unregister(self) -> None:
        """取消此回调的注册。"""
        listeners = self._manager._listeners.get(self._event, [])
        if self._callback in listeners:
            listeners.remove(self._callback)

class HookManager:
    """
    事件分发器。支持函数式回调注册。
    绑定到 Agent 实例，非全局单例。

    用法：
        # 方式 1：函数式注册（轻量、灵活）
        agent.hooks.on("stream", lambda ctx, delta: print(delta))

        # 方式 2：装饰器注册
        @agent.hooks.hook("stream")
        async def my_stream_handler(ctx, delta):
            print(delta)

        # 方式 3：运行时动态增减
        handle = agent.hooks.on("error", alert_callback)
        handle.unregister()  # 取消注册
    """

    def __init__(self):
        self._listeners: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str, callback: Callable) -> HookHandle:
        """注册单个事件回调，返回可取消的 Handle。"""
        self._listeners[event].append(callback)
        return HookHandle(self, event, callback)

    def hook(self, event: str) -> Callable:
        """
        装饰器，用于注册事件回调函数。
        用法：
            @agent.hooks.hook("stream")
            async def my_stream_handler(ctx, delta):
                pass
        """
        def decorator(func: Callable) -> Callable:
            self.on(event, func)
            return func
        return decorator

    async def emit(self, event: str, ctx: HookContext, **kwargs) -> None:
        """
        触发事件，广播给所有监听者。
        """
        for cb in list(self._listeners.get(event, [])):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(ctx, **kwargs)
                else:
                    cb(ctx, **kwargs)
            except Exception:
                logger.exception("Hook callback failed for event '%s'", event)

    def wants_streaming(self) -> bool:
        """检查是否有任何监听者需要流式回调。"""
        return bool(self._listeners.get("stream")) or bool(self._listeners.get("thinking_stream"))

    def finalize_content(self, ctx: HookContext, content: str | None) -> str | None:
        """对最终输出内容进行后处理（同步）。触发 finalize_content 事件。"""
        for cb in list(self._listeners.get("finalize_content", [])):
            try:
                res = cb(ctx, content)
                if res is not None:
                    content = res
            except Exception:
                logger.exception("Hook callback failed for event 'finalize_content'")
        return content
