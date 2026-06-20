from myagent.context.message import ContentBlock, Message
from myagent.core.session.serializer import TOOL_DISPLAY_MAX_CHARS, serialize_messages


def test_serialize_messages_truncates_tool_content_for_display():
    content = "x" * (TOOL_DISPLAY_MAX_CHARS + 500)
    history = serialize_messages([
        Message(role="tool", content=content, tool_call_id="tc_1", tool_name="file_read")
    ])

    rendered = history[0]["content"]
    assert len(rendered) < len(content)
    assert rendered.startswith("x" * TOOL_DISPLAY_MAX_CHARS)
    assert f"原文 {len(content)} 字符" in rendered
    assert history[0]["tool_call_id"] == "tc_1"
    assert history[0]["tool_name"] == "file_read"


def test_serialize_messages_does_not_truncate_assistant_content():
    content = "x" * (TOOL_DISPLAY_MAX_CHARS + 500)
    history = serialize_messages([Message(role="assistant", content=content)])

    assert history[0]["content"] == content


def test_serialize_messages_truncates_tool_text_blocks_for_display():
    content = "x" * (TOOL_DISPLAY_MAX_CHARS + 500)
    history = serialize_messages([
        Message(
            role="tool",
            content=[ContentBlock(type="text", text=content)],
            tool_call_id="tc_1",
        )
    ])

    rendered = history[0]["content"]
    assert len(rendered) < len(content)
    assert rendered.startswith("x" * TOOL_DISPLAY_MAX_CHARS)
    assert f"原文 {len(content)} 字符" in rendered
