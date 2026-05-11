import asyncio
from unittest.mock import MagicMock, AsyncMock
from myagent.observability.audit_logger import AuditLogger
from myagent.observability.events import EventType

async def test_log_event_duplicate_fix():
    backend = MagicMock()
    backend.write = AsyncMock()
    logger = AuditLogger(backend)
    
    # Simulate ctx.snapshot() which includes session_id
    data = {
        "session_id": "session_123",
        "agent_id": "agent_456",
        "other": "value"
    }
    
    print("Calling log_event with potentially duplicate session_id...")
    # This should NOT raise TypeError: emit() got multiple values for keyword argument 'session_id'
    await logger.log_event("session_start", data, session_id="session_123")
    
    assert backend.write.called
    event = backend.write.call_args[0][0]
    assert event.session_id == "session_123"
    assert event.agent_id == "agent_456"
    assert event.data == {"other": "value"}
    print("Test passed!")

if __name__ == "__main__":
    asyncio.run(test_log_event_duplicate_fix())
