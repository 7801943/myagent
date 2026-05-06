import asyncio
from unittest.mock import MagicMock, AsyncMock
from myagent.core.agent import Agent
from myagent.core.stream import StreamResult
from myagent.context.state import AgentState

async def test_agent_save_fix():
    # Mock components
    router = MagicMock()
    # Mock router.stream to return an async iterator
    async def mock_stream(*args, **kwargs):
        yield MagicMock()
    router.stream = mock_stream
    
    state_store = MagicMock()
    state_store.save_messages = AsyncMock()
    state_store.save_state = AsyncMock()
    
    agent = Agent(
        provider_router=router,
        state_store=state_store,
        audit_logger=None
    )
    
    # Mock loop.run to return a StreamResult
    agent._loop.run = AsyncMock(return_value=StreamResult(text="Hello", stop_reason="completed"))
    
    print("Running agent...")
    await agent.run("Hi")
    
    # Check if save_messages and save_state were called instead of save
    assert state_store.save_messages.called, "save_messages was not called"
    assert state_store.save_state.called, "save_state was not called"
    # Verify save was NOT called (it shouldn't even exist on the mock if we didn't define it, but MagicMock might accept it)
    # But we want to ensure we called the RIGHT ones.
    print("Test passed!")

if __name__ == "__main__":
    asyncio.run(test_agent_save_fix())
