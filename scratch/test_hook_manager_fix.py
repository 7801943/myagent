import asyncio
from myagent.core.hook import HookManager, HookContext

class TestState:
    def __init__(self):
        self.before_called = False
        self.after_called = False
        self.on_stream_called = False

async def test_hook_manager():
    manager = HookManager()
    state = TestState()
    
    @manager.hook("stream")
    async def on_stream(ctx: HookContext, delta: str) -> None:
        print(f"Hook: on_stream called with {delta}")
        state.on_stream_called = True

    @manager.hook("before_execute_tools")
    async def before_execute_tools(ctx: HookContext) -> None:
        print("Hook: before_execute_tools called")
        state.before_called = True

    @manager.hook("after_execute_tools")
    async def after_execute_tools(ctx: HookContext) -> None:
        print("Hook: after_execute_tools called")
        state.after_called = True
    
    ctx = HookContext(session_id="test")
    
    print("Emitting stream...")
    await manager.emit("stream", ctx, delta="hello")
    
    print("Emitting before_execute_tools...")
    await manager.emit("before_execute_tools", ctx)
    
    print("Emitting after_execute_tools...")
    await manager.emit("after_execute_tools", ctx)
    
    assert state.on_stream_called, "on_stream was not called"
    assert state.before_called, "before_execute_tools was not called"
    assert state.after_called, "after_execute_tools was not called"
    print("Test passed!")

if __name__ == "__main__":
    asyncio.run(test_hook_manager())
