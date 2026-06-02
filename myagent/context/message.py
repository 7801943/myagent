"""
核心数据模型：Message、ContentBlock、ToolCall。
所有内部消息流转必须使用此模型，不可裸传 dict。
"""
import json
from datetime import datetime, timezone
from typing import Literal, Any
from pydantic import BaseModel, Field
from uuid import uuid4

class ContentBlock(BaseModel):
    """支持文本和多模态内容。"""
    type: Literal["text", "image_url", "image_base64"]
    text: str | None = None
    url: str | None = None
    base64_data: str | None = None
    media_type: str | None = None  # image/jpeg, image/png 等

class ToolCall(BaseModel):
    """LLM 发出的工具调用请求。"""
    id: str = Field(default_factory=lambda: f"tc_{uuid4().hex[:12]}")
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

class ToolResultMessage(BaseModel):
    """
    工具执行结果的 Pydantic 消息模型。
    
    [FIX] 原名 ToolResult，与 api.py:ToolResult（dataclass）同名导致二义性。
    重命名为 ToolResultMessage 以明确语义差异：
    - api.py:ToolResult（dataclass）= 工具函数的返回值封装
    - message.py:ToolResultMessage（Pydantic BaseModel）= 存入对话历史的消息记录
    
    Attributes:
        tool_call_id: 对应的 ToolCall.id
        tool_name: 工具名称（可选）
        content: 结果内容（文本或多模态内容块）
        is_error: 是否为错误结果
        metadata: 扩展元数据
    """
    tool_call_id: str
    tool_name: str | None = None
    content: str | list[ContentBlock]
    is_error: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

class Message(BaseModel):
    """统一消息格式。"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentBlock]
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None  # role="tool" 时必填
    tool_name: str | None = None     # role="tool" 时可选，记录工具名
    metadata: dict[str, Any] = Field(default_factory=dict)  # 扩展元数据（如 thinking）
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    token_estimate: int | None = None

    def to_openai_dict(self) -> dict:
        """转为 OpenAI API 消息格式。"""
        msg: dict[str, Any] = {"role": self.role}
        if isinstance(self.content, str):
            msg["content"] = self.content
        else:
            parts = []
            for block in self.content:
                if block.type == "text":
                    parts.append({"type": "text", "text": block.text or ""})
                elif block.type == "image_url":
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": block.url},
                    })
                elif block.type == "image_base64":
                    data_uri = f"data:{block.media_type or 'image/png'};base64,{block.base64_data}"
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": data_uri},
                    })
            msg["content"] = parts
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        return msg

    def to_anthropic_dict(self) -> dict:
        """转为 Anthropic API 消息格式。"""
        msg: dict[str, Any] = {"role": self.role}
        if isinstance(self.content, str):
            msg["content"] = self.content
        else:
            blocks = []
            for block in self.content:
                if block.type == "text":
                    blocks.append({"type": "text", "text": block.text or ""})
                elif block.type == "image_base64":
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block.media_type or "image/png",
                            "data": block.base64_data,
                        }
                    })
                elif block.type == "image_url":
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": block.url,
                        }
                    })
            msg["content"] = blocks
        return msg