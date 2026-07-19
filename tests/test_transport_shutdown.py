import asyncio
import os
import shlex
import signal
import sys
from types import SimpleNamespace

import pytest

from myagent.tools.json_rpc import JsonRpcProxy
from myagent.tools.manager import ToolManager
from myagent.tools.transport import SubprocessTransport


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_json_rpc_start_cancellation_stops_transport_and_reader_task():
    class _Writer:
        def write(self, _data):
            pass

        async def drain(self):
            pass

    class _Transport:
        def __init__(self):
            self.reader = asyncio.StreamReader()
            self.writer = _Writer()
            self.stopped = False

        async def start(self):
            pass

        async def stop(self):
            self.stopped = True
            self.reader.feed_eof()

    transport = _Transport()
    proxy = JsonRpcProxy(transport)
    start_task = asyncio.create_task(proxy.start())
    await asyncio.sleep(0)

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert transport.stopped
    assert proxy._reader_task is None


@pytest.mark.asyncio
async def test_subprocess_transport_does_not_replace_application_signal_handlers(tmp_path):
    original_sigint = signal.getsignal(signal.SIGINT)
    original_sigterm = signal.getsignal(signal.SIGTERM)
    transport = SubprocessTransport(cwd=str(tmp_path))

    try:
        await transport.start()
        assert signal.getsignal(signal.SIGINT) is original_sigint
        assert signal.getsignal(signal.SIGTERM) is original_sigterm
    finally:
        await transport.stop()

    assert transport._proc is None
    assert transport._atexit_cleanup is None
    assert not transport._exit_hooks_registered


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
@pytest.mark.asyncio
async def test_subprocess_transport_stop_reaps_runner_and_active_descendant(tmp_path):
    transport = SubprocessTransport(cwd=os.getcwd())
    proxy = JsonRpcProxy(transport, default_timeout=60.0)
    await proxy.start()
    assert transport._proc is not None
    runner_pid = transport._proc.pid

    child_pid_file = tmp_path / "child.pid"
    child_script = (
        "import os,time,pathlib,signal; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child_script)}"
    execute_task = asyncio.create_task(proxy.execute_cli(command, timeout=60.0))

    await _wait_until(child_pid_file.exists)
    child_pid = int(child_pid_file.read_text())
    assert os.path.exists(f"/proc/{runner_pid}")
    assert os.path.exists(f"/proc/{child_pid}")

    await proxy.stop()
    await asyncio.gather(execute_task, return_exceptions=True)

    await _wait_until(lambda: not os.path.exists(f"/proc/{runner_pid}"))
    await _wait_until(lambda: not os.path.exists(f"/proc/{child_pid}"))
    assert transport._proc is None
    assert transport._atexit_cleanup is None


@pytest.mark.asyncio
async def test_tool_manager_stop_disconnects_mcp_clients_and_clears_tasks(tmp_path):
    class _FakeMCPClient:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    class _FakeProxy:
        stopped = False

        async def stop(self):
            self.stopped = True

    client = _FakeMCPClient()
    proxy = _FakeProxy()
    manager = ToolManager(tools_dir=str(tmp_path))
    manager._running = True
    manager._watch_task = asyncio.create_task(asyncio.Event().wait())
    manager._proxy = proxy
    manager._mcp_clients["example"] = client
    manager._tools["mcp_example_tool"] = SimpleNamespace(
        source="mcp",
        mcp_client=client,
        name="mcp_example_tool",
    )

    await manager.stop()

    assert client.disconnected
    assert proxy.stopped
    assert manager._watch_task is None
    assert manager._proxy is None
    assert manager._mcp_clients == {}
    assert "mcp_example_tool" not in manager._tools
