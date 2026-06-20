"""消息序列化纯函数：将内部消息列表转换为前端可显示的 dict 列表。"""

TOOL_DISPLAY_MAX_CHARS = 2000


def truncate_tool_display_content(content: str) -> str:
    """截断前端 tool chip 展示内容，不影响持久化或 LLM 上下文。"""
    if len(content) <= TOOL_DISPLAY_MAX_CHARS:
        return content
    return content[:TOOL_DISPLAY_MAX_CHARS] + (
        f"\n...[截断：原文 {len(content)} 字符]"
    )


def serialize_messages(messages: list) -> list[dict]:
    """将消息列表序列化为前端可显示的 dict 列表。"""
    history = []
    for msg in messages:
        entry: dict = {"role": msg.role, "content": ""}

        if hasattr(msg, 'content') and msg.content:
            if isinstance(msg.content, str):
                entry["content"] = msg.content
            elif isinstance(msg.content, list):
                parts = []
                for block in msg.content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(block.get("text") or "")
                    elif hasattr(block, 'type') and block.type == "text":
                        parts.append(getattr(block, 'text', '') or "")
                entry["content"] = "".join(parts)
            else:
                entry["content"] = str(msg.content)

        if msg.role == "tool" and isinstance(entry["content"], str):
            entry["content"] = truncate_tool_display_content(entry["content"])

        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": getattr(tc, 'id', None) if not isinstance(tc, dict) else tc.get('id'),
                    "name": getattr(tc, 'name', None) if not isinstance(tc, dict) else tc.get('name'),
                    "arguments": getattr(tc, 'arguments', {}) if not isinstance(tc, dict) else tc.get('arguments', {}),
                }
                for tc in msg.tool_calls
            ]

        if hasattr(msg, 'tool_call_id') and msg.tool_call_id:
            entry["tool_call_id"] = msg.tool_call_id
        if hasattr(msg, 'tool_name') and msg.tool_name:
            entry["tool_name"] = msg.tool_name
        if hasattr(msg, 'metadata') and msg.metadata:
            entry["metadata"] = msg.metadata

        history.append(entry)

    return history
