"""
StreamProcessor：将 Provider 的 StreamEvent 流聚合为结构化结果。
累积文本、提取 ToolCall、汇总 Token usage。
"""
from dataclasses import dataclass

from myagent.providers.base import StreamEvent
from myagent.context.message import ToolCall
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

@dataclass
class StreamResult:
    """一次 Provider 调用的聚合结果。"""
    text: str = ""
    reasoning_text: str = ""
    tool_calls: list[ToolCall] = None
    stop_reason: str | None = None
    usage: dict = None

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []
        if self.usage is None:
            self.usage = {}

class StreamProcessor:
    """
    流事件聚合器。
    消费 StreamEvent 序列，产出 StreamResult。
    """

    def __init__(self):
        self._text_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_calls: list[ToolCall] = []
        self._tool_call_buffers: dict[str, dict] = {}  # call_id -> {name, args_json}
        self._stop_reason: str | None = None
        self._usage: dict = {}

    def process(self, event: StreamEvent) -> None:
        """处理单个 StreamEvent。"""
        if event.type == "text_delta" and event.text:
            self._text_parts.append(event.text)

        elif event.type == "thinking_delta" and event.text:
            self._reasoning_parts.append(event.text)

        elif event.type == "tool_call_start":
            self._tool_call_buffers[event.tool_call_id] = {
                "name": event.tool_name,
                "args_json": "",
            }

        elif event.type == "tool_call_delta":
            buf = self._tool_call_buffers.get(event.tool_call_id)
            if buf and event.tool_args_delta:
                buf["args_json"] += event.tool_args_delta

        elif event.type == "tool_call_end" and event.tool_args is not None:
            self._tool_calls.append(ToolCall(
                id=event.tool_call_id,
                name=event.tool_name,
                arguments=event.tool_args,
            ))
            self._tool_call_buffers.pop(event.tool_call_id, None)

        elif event.type == "message_end":
            self._stop_reason = event.stop_reason
            if event.usage:
                self._usage = event.usage

    def result(self) -> StreamResult:
        """返回聚合后的 StreamResult。"""
        return StreamResult(
            text="".join(self._text_parts),
            reasoning_text="".join(self._reasoning_parts),
            tool_calls=list(self._tool_calls),
            stop_reason=self._stop_reason,
            usage=dict(self._usage),
        )

    def reset(self) -> None:
        """重置处理器状态。"""
        self._text_parts.clear()
        self._reasoning_parts.clear()
        self._tool_calls.clear()
        self._tool_call_buffers.clear()
        self._stop_reason = None
        self._usage.clear()


