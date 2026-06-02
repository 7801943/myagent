"""
ContextManager：管理消息历史，预留 TokenBudget 三层控制（V3 核心）。
三层结构：[System Prompt] + [Summary Memory] + [Recent N 轮原话]
Phase 1 实现基础的消息管理 + Token 估算 + 工具结果强截断。

Phase 1 重构：
  - add_* 方法改为 async，每条消息添加时实时写入数据库
  - 调用端无需考虑批量 flush，吞吐优化由 DB 层负责
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# [FIX] ToolResult → ToolResultMessage，消除与 api.py:ToolResult 的二义性
from myagent.context.message import Message, ContentBlock, ToolResultMessage
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.context.state import StateStore

logger = get_logger(__name__)


class ContextManager:
    def __init__(
        self,
        max_tokens_budget: int = 100000,
        context_window_size: int = 200000,
        tool_result_max_chars: int = 200000,
        recent_turns: int = 20,
        state_store: "StateStore | None" = None,
        session_id: str | None = None,
    ):
        self._messages: list[Message] = []
        self._system_prompt: str | None = None
        self._max_tokens_budget = max_tokens_budget
        self._context_window_size = context_window_size
        self._tool_result_max_chars = tool_result_max_chars
        self._recent_turns = recent_turns
        self._last_usage_input_tokens: int = 0
        # 实时持久化
        self._state_store = state_store
        self._session_id = session_id

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

    @property
    def system_prompt(self) -> str | None:
        """获取当前 System Prompt。"""
        return self._system_prompt

    def set_system(self, prompt: str) -> None:
        """设置/替换 System Prompt。"""
        self._system_prompt = prompt
        # 确保 system 消息始终在首位
        self._messages = [m for m in self._messages if m.role != "system"]
        self._messages.insert(0, Message(role="system", content=prompt))

    def add_system_note(self, text: str) -> None:
        """
        临时注入一条系统注释（workspace 文件列表等上下文信息）。
        不修改 _system_prompt，而是在 system 消息之后、用户消息之前插入。
        每次调用前会清除之前注入的注释，避免重复。
        """
        # 移除之前通过 add_system_note 注入的注释（以特定前缀标识）
        tag = "[工作空间]"
        self._messages = [m for m in self._messages
                          if not (isinstance(m.content, str) and m.content.startswith(tag))]
        # 在 system prompt 之后插入
        insert_idx = 1 if self._messages and self._messages[0].role == "system" else 0
        self._messages.insert(insert_idx, Message(role="system", content=text))

    async def add_user_message(self, content: str | list[ContentBlock]) -> None:
        """添加用户消息 + 实时持久化。"""
        self._messages.append(Message(role="user", content=content))
        await self._persist_messages()

    async def add_assistant_message(self, content: str, tool_calls=None) -> None:
        """添加助手消息 + 实时持久化。"""
        self._messages.append(Message(
            role="assistant", content=content, tool_calls=tool_calls
        ))
        await self._persist_messages()

    async def add_tool_result(self, tool_call_id: str, result: ToolResultMessage) -> None:
        """
        添加工具结果 + 实时持久化。
        V3 关键：强制截断超长工具输出，防止上下文爆窗。
        支持 str 和 list[ContentBlock] 两种 content 类型。
        """
        content = result.content

        if isinstance(content, str):
            if len(content) > self._tool_result_max_chars:
                content = content[:self._tool_result_max_chars] + (
                    f"\n...[截断：原文 {len(result.content)} 字符，"
                    f"已截断至 {self._tool_result_max_chars} 字符]"
                )
                logger.warning(
                    f"Tool result truncated: {result.tool_call_id}, "
                    f"{len(result.content)} -> {self._tool_result_max_chars} chars"
                )
        else:
            # list[ContentBlock] — 截断文本块，保留其他块
            truncated_blocks = []
            for block in content:
                if block.text and len(block.text) > self._tool_result_max_chars:
                    truncated_text = block.text[:self._tool_result_max_chars] + (
                        f"\n...[截断：原文 {len(block.text)} 字符]"
                    )
                    from myagent.context.message import ContentBlock as CB
                    truncated_blocks.append(CB(type="text", text=truncated_text))
                    logger.warning(
                        f"Tool result text block truncated: {result.tool_call_id}"
                    )
                else:
                    truncated_blocks.append(block)
            content = truncated_blocks

        self._messages.append(Message(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
        ))
        await self._persist_messages()

    def get_messages(self) -> list[Message]:
        """
        返回发送给 LLM 的消息列表。
        Phase 1：直接返回全部消息。
        TODO(Phase 5)：实现三层结构裁剪 —— System + Summary + Recent
        """
        return list(self._messages)

    # 每张图像的保守默认 token 估算（约 1024×1024 high detail）
    _IMAGE_TOKEN_ESTIMATE = 765

    def estimate_tokens(self) -> int:
        """
        粗略估算总 Token 数。
        - 文本：1 中文字 ≈ 2 token，1 英文单词 ≈ 1 token（简化为字符数）
        - 图像：每张按保守默认值估算（约 765 tokens）
        """
        total = 0
        for msg in self._messages:
            if isinstance(msg.content, str):
                total += len(msg.content)
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if block.type == "text" and block.text:
                        total += len(block.text)
                    elif block.type in ("image_url", "image_base64"):
                        total += self._IMAGE_TOKEN_ESTIMATE
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

    def clear(self) -> None:
        """清空所有消息和 system prompt（供 /new 系统指令使用）。"""
        self._messages.clear()
        self._system_prompt = None
        self._last_usage_input_tokens = 0

    async def _persist_messages(self) -> None:
        """实时写入数据库。调用端无需关心。"""
        if self._state_store and self._session_id:
            try:
                await self._state_store.save_messages(self._session_id, self._messages)
            except Exception:
                logger.warning(f"Failed to persist messages for session {self._session_id}")

    async def flush(self) -> None:
        """强制刷新（供 Session 在状态变更时调用）。"""
        await self._persist_messages()
