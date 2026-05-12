"""
会话管理 REST API。

Phase 2 变更：
  - 通过 SessionManager 统一管理会话（不再直调 StateStore）

未来扩展：
  - [AUTH] 所有端点需要鉴权中间件，按用户隔离会话
"""
from fastapi import APIRouter

from myagent.interfaces.web.dependencies import get_session_manager

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def list_sessions():
    """列出所有会话。"""
    session_manager = get_session_manager()
    sessions = await session_manager.list_sessions()

    # 为每个会话获取标题和消息数
    for s in sessions:
        try:
            messages = await session_manager.get_session_messages(s["session_id"])
            first_user = next((m for m in messages if m.role == "user"), None)
            content = _extract_text_content(first_user) if first_user else ""
            s["title"] = content[:50] if content else "新对话"
            s["message_count"] = len(messages)
        except Exception:
            s["title"] = "新对话"
            s["message_count"] = 0

    return {"sessions": sessions}


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    """删除指定会话。"""
    session_manager = get_session_manager()
    await session_manager.delete_session(session_id)
    return {"deleted": True, "session_id": session_id}


def _extract_text_content(msg) -> str:
    """从消息对象中提取文本内容（兼容 str/list/ContentBlock）。"""
    if not hasattr(msg, 'content') or not msg.content:
        return ""

    if isinstance(msg.content, str):
        return msg.content

    if isinstance(msg.content, list):
        parts = []
        for block in msg.content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text") or "")
            elif hasattr(block, 'type') and block.type == "text":
                parts.append(getattr(block, 'text', '') or "")
        return "".join(parts)

    return str(msg.content)