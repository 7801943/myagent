import asyncio
import os
from myagent.providers.openai_provider import OpenAIProvider
from myagent.context.message import Message, ContentBlock, ToolCall

async def main():
    api_key = os.environ.get("GEMINI_API_KEY", "")
    provider = OpenAIProvider(
        name="test",
        model="gemini-2.5-pro", # adjust model name
        api_key=api_key,
        api_base="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    # fake tool call
    tc = ToolCall(id="tc_123", name="test_tool", arguments={})
    
    # tool result with image
    msgs = [
        Message(role="user", content="call test_tool").to_openai_dict(),
        Message(role="assistant", content="", tool_calls=[tc]).to_openai_dict(),
        Message(role="tool", tool_call_id="tc_123", content=[
            ContentBlock(type="text", text="here is the image"),
            # fake 1x1 png base64
            ContentBlock(type="image_base64", base64_data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", media_type="image/png")
        ]).to_openai_dict(),
        Message(role="user", content="What color is the image?").to_openai_dict()
    ]
    print("Messages:", msgs)
    
    try:
        async for event in provider.stream(msgs):
            if event.type == "text_delta":
                print(event.text, end="", flush=True)
        print("\nSuccess")
    except Exception as e:
        print("\nError:", e)

asyncio.run(main())
