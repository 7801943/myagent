import asyncio
from myagent.context.state import SQLiteStateStore
async def main():
    store = SQLiteStateStore()
    await store.initialize()
    sessions = await store.list_all_sessions()
    print("Found sessions:", len(sessions))
    for s in sessions:
        sid = s["session_id"]
        print(f"Loading session {sid}")
        try:
            msgs = await store.load_messages(sid)
            print(f"  Loaded {len(msgs)} messages")
        except Exception as e:
            print(f"  Error loading {sid}: {e}")
if __name__ == "__main__":
    asyncio.run(main())
