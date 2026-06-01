import pytest

from myagent.core.events import EventBus, ExecutionContext, StateChange, StreamDelta


@pytest.mark.asyncio
async def test_event_bus_publishes_typed_and_legacy_callbacks():
    bus = EventBus()
    typed = []
    legacy = []

    bus.on(StreamDelta, lambda event: typed.append((event.session_id, event.delta)))
    bus.on("stream", lambda ctx, delta: legacy.append((ctx.session_id, delta)))

    await bus.publish(StreamDelta(session_id="s1", delta="hello"))
    await bus.emit("stream", ExecutionContext(session_id="s2"), delta="world")

    assert typed == [("s1", "hello"), ("s2", "world")]
    assert legacy == [("s1", "hello"), ("s2", "world")]


@pytest.mark.asyncio
async def test_event_bus_routes_by_topic_and_unregisters():
    bus = EventBus()
    seen = []

    handle = bus.on(StateChange, lambda event: seen.append(event.state), topic="s1")
    bus.on(StateChange, lambda event: seen.append(f"global:{event.state}"))

    await bus.publish(StateChange(session_id="s2", state="thinking"))
    await bus.publish(StateChange(session_id="s1", state="idle"))
    handle.unregister()
    await bus.publish(StateChange(session_id="s1", state="done"))

    assert seen == ["global:thinking", "idle", "global:idle", "global:done"]
