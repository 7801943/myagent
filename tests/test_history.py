import asyncio
from myagent.context.state import SQLiteStateStore

async def main():
    store = SQLiteStateStore()
    await store.initialize()
    sessions = await store.list_all_sessions()
    print("Found sessions:", len(sessions))
    for s in sessions:
        sid = s["session_id"]
        msgs = await store.load_messages(sid)
        history = []
        for msg in msgs:
            entry = {"role": msg.role, "content": ""}
            if hasattr(msg, 'content') and msg.content:
                if isinstance(msg.content, str):
                    entry["content"] = msg.content
                elif isinstance(msg.content, list):
                    entry["content"] = "".join((b.get('text') or "") if isinstance(b, dict) else (getattr(b, 'text', None) or "") for b in msg.content if (b.get('type') if isinstance(b, dict) else getattr(b, 'type', '')) == 'text')
            history.append(entry)
        print(f"Session {sid}: Loaded {len(history)} messages. First msg content length: {len(history[0]['content']) if history else 0}")
if __name__ == "__main__":
    asyncio.run(main())
