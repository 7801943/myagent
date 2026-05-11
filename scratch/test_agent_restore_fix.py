import asyncio
from unittest.mock import MagicMock, AsyncMock
from myagent.core.agent import Agent
from myagent.core.session import Session

async def test_agent_restore_fix():
    state_store = MagicMock()
    state_store.load_state = AsyncMock(return_value=("idle", {}))
    state_store.load_messages = AsyncMock(return_value=[])
    
    agent = Agent(
        provider_router=MagicMock(),
        state_store=state_store
    )
    
    print("Restoring session...")
    await agent.restore_session("test_session")
    
    assert state_store.load_state.called, "load_state was not called"
    assert state_store.load_messages.called, "load_messages was not called"
    assert agent.session_id == "test_session", "session_id was not updated"
    print("Test passed!")

if __name__ == "__main__":
    asyncio.run(test_agent_restore_fix())
