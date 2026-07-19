import asyncio
from types import SimpleNamespace

import pytest

from myagent.core.session.manager import SessionManager


class _FakeToolInterface:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


class _FakeSession:
    def __init__(self, session_id: str, running_task: asyncio.Task | None = None):
        self.id = session_id
        self._running_task = running_task
        self.tool_interface = _FakeToolInterface()
        self._harness = SimpleNamespace(tool_interface=self.tool_interface)
        self.cancelled = False
        self.unregistered = False
        self.saved = False

    def request_cancel(self, reason: str, detail: str):
        self.cancelled = (reason, detail)
        if self._running_task and not self._running_task.done():
            self._running_task.cancel()

    def unregister_events(self):
        self.unregistered = True

    async def save(self):
        self.saved = True


@pytest.mark.asyncio
async def test_session_manager_stop_cleans_every_active_session_and_drops_references():
    async def wait_forever():
        await asyncio.Event().wait()

    running_task = asyncio.create_task(wait_forever())
    first = _FakeSession("first", running_task)
    second = _FakeSession("second")

    manager = object.__new__(SessionManager)
    manager._running = True
    manager._cleanup_task = asyncio.create_task(wait_forever())
    manager._sessions = {("alice", "first"): first, ("bob", "second"): second}

    await manager.stop()

    assert running_task.cancelled()
    assert manager._cleanup_task is None
    assert manager._sessions == {}
    for session in (first, second):
        assert session.cancelled == ("server_shutdown", "服务正在关闭")
        assert session.unregistered
        assert session.tool_interface.stopped
        assert session.saved
