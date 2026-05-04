"""
ContextManager：管理消息历史，预留 TokenBudget 三层控制（V3 核心）。
三层结构：[System Prompt] + [Summary Memory] + [Recent N 轮原话]
Phase 1 实现基础的消息管理 + Token 估算 + 工具结果强截断。
"""
from myagent.context.message import Message, ContentBlock, ToolResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class ContextManager:
    def __init__(
        self,
        max_tokens_budget: int = 200000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
        recent_turns: int = 20,
    ):
        self._messages: list[Message] = []
        self._system_prompt: str | None = None
        self._max_tokens_budget = max_tokens_budget
        self._context_window_size = context_window_size
        self._tool_result_max_chars = tool_result_max_chars
        self._recent_turns = recent_turns
        self._last_usage_input_tokens: int = 0

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    @property
    def context_window_size(self) -> int:
        return self._context_window_size

    @property
    def last_usage_input_tokens(self) -> int:
        """返回最后一次 API 调用的 input_tokens（即当前上下文实际占用量）。"""
        return self._last_usage_input_tokens

    def update_usage(self, usage: dict) -> None:
        """更新来自 API 返回的 token 使用量。取最后一次的 input_tokens。"""
        if usage and usage.get("input_tokens"):
            self._last_usage_input_tokens = usage["input_tokens"]
            logger.debug(f"Context usage updated: input_tokens={self._last_usage_input_tokens}")

    def set_system(self, prompt: str) -> None:
        """设置/替换 System Prompt。"""
        self._system_prompt = prompt
        # 确保 system 消息始终在首位
        self._messages = [m for m in self._messages if m.role != "system"]
        self._messages.insert(0, Message(role="system", content=prompt))

    def add_user_message(self, content: str | list[ContentBlock]) -> None:
        self._messages.append(Message(role="user", content=content))

    def add_assistant_message(self, content: str, tool_calls=None) -> None:
        self._messages.append(Message(
            role="assistant", content=content, tool_calls=tool_calls
        ))

    def add_tool_result(self, tool_call_id: str, result: ToolResult) -> None:
        """
        添加工具结果。V3 关键：强制截断超长工具输出，防止上下文爆窗。
        """
        content = result.content
        if len(content) > self._tool_result_max_chars:
            content = content[:self._tool_result_max_chars] + f"\n...[截断：原文 {len(result.content)} 字符，已截断至 {self._tool_result_max_chars} 字符]"
            logger.warning(
                f"Tool result truncated: {result.tool_call_id}, "
                f"{len(result.content)} -> {self._tool_result_max_chars} chars"
            )
        self._messages.append(Message(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
        ))

    def get_messages(self) -> list[Message]:
        """
        返回发送给 LLM 的消息列表。
        Phase 1：直接返回全部消息。
        TODO(Phase 5)：实现三层结构裁剪 —— System + Summary + Recent
        """
        return list(self._messages)

    def estimate_tokens(self) -> int:
        """
        粗略估算总 Token 数（1 中文字 ≈ 2 token，1 英文单词 ≈ 1 token）。
        """
        total = 0
        for msg in self._messages:
            text = msg.content if isinstance(msg.content, str) else " ".join(
                b.text or "" for b in msg.content if b.text
            )
            # 粗略估算：中文按字符数×2，英文按词数
            total += len(text)  # 简化估算
        return total

    def is_over_budget(self) -> bool:
        return self.estimate_tokens() > self._max_tokens_budget

    def restore_from(self, messages: list[Message]) -> None:
        """从 StateStore 恢复消息历史。"""
        self._messages = messages
        # 重新提取 system prompt
        for msg in self._messages:
            if msg.role == "system":
                self._system_prompt = msg.content if isinstance(msg.content, str) else None
                break