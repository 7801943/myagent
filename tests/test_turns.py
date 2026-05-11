"""
Turn 抽象层独立测试。
覆盖：
- BaseTurn：生命周期钩子 (turn_start/turn_end/turn_error)、看门狗超时
- SystemTurn：系统指令检测
- ModelTurn：有/无 tool_calls 时的 next_turn 判断、流式处理、context 写入
- ToolTurn：结果写入 context、错误/安全事件、内联审批
- AgentLoop dispatcher：完整 ReAct 循环、max_iterations、cancelled 处理
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from myagent.core.hook import HookContext, HookManager
from myagent.core.loop import (
    TurnKind, TurnResult, BaseTurn, SystemTurn, ModelTurn, ToolTurn, AgentLoop, StreamResult,
)
from myagent.context.manager import ContextManager
from myagent.context.message import ToolCall, ToolResult as MsgToolResult
from myagent.providers.base import StreamEvent
from myagent.tools.api import ToolResult


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def hook_ctx():
    return HookContext(session_id="test-session", agent_id="test-agent")


@pytest.fixture
def hooks():
    return HookManager()


@pytest.fixture
def context():
    return ContextManager()


def _make_tool_call(name: str = "test_tool", call_id: str = "tc_001") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments={"arg1": "value1"})


async def _mock_stream_events(events: list[StreamEvent]):
    for event in events:
        yield event


# ═══════════════════════════════════════════════════════════
# TurnResult 测试
# ═══════════════════════════════════════════════════════════

class TestTurnResult:
    """TurnResult 基础测试。"""

    def test_model_turn_result(self):
        result = TurnResult(kind=TurnKind.MODEL, next_turn=None, stream_result=StreamResult(text="hi"))
        assert result.kind == TurnKind.MODEL
        assert result.next_turn is None
        assert result.stream_result.text == "hi"

    def test_tool_turn_result(self):
        result = TurnResult(kind=TurnKind.TOOL, next_turn=TurnKind.MODEL, data=["tc1"])
        assert result.kind == TurnKind.TOOL
        assert result.next_turn == TurnKind.MODEL
        assert result.data == ["tc1"]

    def test_system_turn_result(self):
        result = TurnResult(kind=TurnKind.SYSTEM, next_turn=TurnKind.MODEL)
        assert result.kind == TurnKind.SYSTEM
        assert result.next_turn == TurnKind.MODEL


# ═══════════════════════════════════════════════════════════
# BaseTurn 生命周期事件测试
# ═══════════════════════════════════════════════════════════

class TestBaseTurnLifecycle:
    """BaseTurn turn_start / turn_end / turn_error 生命周期测试。"""

    def _make_event_tracker(self, hooks, event_names):
        """创建事件追踪回调，返回 event -> list 的映射。"""
        tracks = {name: [] for name in event_names}
        async def capture(ctx, **kwargs):
            pass
        for name in event_names:
            def make_cb(name=name):
                async def cb(ctx, **kwargs):
                    tracks[name].append(dict(kwargs, _ctx=ctx))
                return cb
            hooks.on(name, make_cb())
        return tracks

    @pytest.mark.asyncio
    async def test_model_turn_emits_start_and_end(self, hooks, context, hook_ctx):
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Hi"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        tracks = self._make_event_tracker(hooks, ["model_turn_start", "model_turn_end"])

        turn = ModelTurn(
            provider_router=mock_router, context=context,
            tool_schemas=None, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx)

        assert len(tracks["model_turn_start"]) == 1
        assert len(tracks["model_turn_end"]) == 1

    @pytest.mark.asyncio
    async def test_tool_turn_emits_start_and_end(self, hooks, context, hook_ctx):
        tool_executor = AsyncMock(return_value=[
            ToolResult(content="ok", is_error=False, metadata={})
        ])

        tracks = self._make_event_tracker(hooks, ["tool_turn_start", "tool_turn_end"])

        turn = ToolTurn(
            context=context, tool_executor=tool_executor,
            hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx, input_data=[_make_tool_call()])

        assert len(tracks["tool_turn_start"]) == 1
        assert len(tracks["tool_turn_end"]) == 1

    @pytest.mark.asyncio
    async def test_system_turn_emits_start_and_end(self, hooks, context, hook_ctx):
        tracks = self._make_event_tracker(hooks, ["system_turn_start", "system_turn_end"])

        turn = SystemTurn(
            context=context, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx)

        assert len(tracks["system_turn_start"]) == 1
        assert len(tracks["system_turn_end"]) == 1

    @pytest.mark.asyncio
    async def test_error_emits_turn_error_and_state_change(self, hooks, context, hook_ctx):
        mock_router = MagicMock()

        async def failing_stream(*args, **kwargs):
            raise RuntimeError("Provider crashed")
            yield

        mock_router.stream = failing_stream

        tracks = self._make_event_tracker(hooks, [
            "model_turn_start", "model_turn_error", "model_turn_end",
            "state_change", "error",
        ])

        turn = ModelTurn(
            provider_router=mock_router, context=context,
            tool_schemas=None, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )

        with pytest.raises(RuntimeError):
            await turn.execute(hook_ctx)

        # turn_start + state_change(thinking) + state_change(error) + error + turn_error. No turn_end.
        assert len(tracks["model_turn_start"]) == 1
        assert len(tracks["model_turn_error"]) == 1
        assert len(tracks["state_change"]) == 2  # thinking (ModelTurn) + error (BaseTurn)
        assert len(tracks["error"]) == 1
        assert len(tracks["model_turn_end"]) == 0

    @pytest.mark.asyncio
    async def test_cancelled_not_emits_error_hooks(self, hooks, context, hook_ctx):
        """CancelledError 应直接传播，不触发 turn_error/state_change(error)。"""
        mock_router = MagicMock()

        async def cancelled_stream(*args, **kwargs):
            raise asyncio.CancelledError()
            yield

        mock_router.stream = cancelled_stream

        tracks = self._make_event_tracker(hooks, ["model_turn_error", "state_change"])

        turn = ModelTurn(
            provider_router=mock_router, context=context,
            tool_schemas=None, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )

        with pytest.raises(asyncio.CancelledError):
            await turn.execute(hook_ctx)

        assert len(tracks["model_turn_error"]) == 0


# ═══════════════════════════════════════════════════════════
# SystemTurn 测试
# ═══════════════════════════════════════════════════════════

class TestSystemTurn:
    """SystemTurn 测试套件。"""

    @pytest.mark.asyncio
    async def test_no_user_message_returns_model(self, hooks, context, hook_ctx):
        turn = SystemTurn(
            context=context, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        result = await turn.execute(hook_ctx)
        assert result.kind == TurnKind.SYSTEM
        assert result.next_turn == TurnKind.MODEL

    @pytest.mark.asyncio
    async def test_regular_user_message_returns_model(self, hooks, context, hook_ctx):
        context.add_user_message("Hello, how are you?")
        turn = SystemTurn(
            context=context, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        result = await turn.execute(hook_ctx)
        assert result.next_turn == TurnKind.MODEL

    @pytest.mark.asyncio
    async def test_system_command_detected(self, hooks, context, hook_ctx):
        context.add_user_message("/model gpt-4")
        cmd_events = []

        async def capture(ctx, **kwargs):
            cmd_events.append(kwargs)

        hooks.on("system_command", capture)

        handler_calls = []

        async def cmd_handler(cmd, args, ctx):
            handler_calls.append((cmd, args))

        turn = SystemTurn(
            context=context, hooks=hooks, audit=None, watchdog_timeout=10.0,
            system_command_handler=cmd_handler,
        )
        result = await turn.execute(hook_ctx)

        assert result.next_turn == TurnKind.MODEL
        assert len(cmd_events) == 1
        assert cmd_events[0]["command"] == "model"
        assert cmd_events[0]["args"] == "gpt-4"
        assert len(handler_calls) == 1
        assert handler_calls[0] == ("model", "gpt-4")

    @pytest.mark.asyncio
    async def test_system_command_new(self, hooks, context, hook_ctx):
        context.add_user_message("/new")
        cmd_events = []

        async def capture(ctx, **kwargs):
            cmd_events.append(kwargs)

        hooks.on("system_command", capture)

        turn = SystemTurn(
            context=context, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        result = await turn.execute(hook_ctx)

        assert result.next_turn == TurnKind.MODEL
        assert len(cmd_events) == 1
        assert cmd_events[0]["command"] == "new"
        assert cmd_events[0]["args"] == ""

    @pytest.mark.asyncio
    async def test_system_command_without_handler(self, hooks, context, hook_ctx):
        context.add_user_message("/clear all")
        cmd_events = []

        async def capture(ctx, **kwargs):
            cmd_events.append(kwargs)

        hooks.on("system_command", capture)

        turn = SystemTurn(
            context=context, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        result = await turn.execute(hook_ctx)

        assert result.next_turn == TurnKind.MODEL
        assert len(cmd_events) == 1


# ═══════════════════════════════════════════════════════════
# ModelTurn 测试
# ═══════════════════════════════════════════════════════════

class TestModelTurn:
    """ModelTurn 测试套件。"""

    @pytest.mark.asyncio
    async def test_no_tool_calls_returns_none_next(self, hooks, context, hook_ctx):
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Hello, "),
            StreamEvent(type="text_delta", text="world!"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        turn = ModelTurn(
            provider_router=mock_router, context=context,
            tool_schemas=None, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        result = await turn.execute(hook_ctx)

        assert result.kind == TurnKind.MODEL
        assert result.next_turn is None
        assert result.stream_result.text == "Hello, world!"
        assert result.stream_result.tool_calls == []

    @pytest.mark.asyncio
    async def test_with_tool_calls_returns_tool_next(self, hooks, context, hook_ctx):
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="I'll call a tool."),
            StreamEvent(type="tool_call_start", tool_name="read_file", tool_call_id="tc_001"),
            StreamEvent(type="tool_call_delta", tool_args_delta='{"path": "test.py"}'),
            StreamEvent(type="tool_call_end", tool_call_id="tc_001", tool_name="read_file",
                        tool_args={"path": "test.py"}),
            StreamEvent(type="message_end", stop_reason="tool_use"),
        ])

        turn = ModelTurn(
            provider_router=mock_router, context=context,
            tool_schemas=None, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        result = await turn.execute(hook_ctx)

        assert result.kind == TurnKind.MODEL
        assert result.next_turn == TurnKind.TOOL
        assert len(result.data) == 1
        assert result.data[0].name == "read_file"

    @pytest.mark.asyncio
    async def test_writes_assistant_message_to_context(self, hooks, context, hook_ctx):
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Response text"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        turn = ModelTurn(
            provider_router=mock_router, context=context,
            tool_schemas=None, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx)

        messages = context.get_messages()
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0].content == "Response text"

    @pytest.mark.asyncio
    async def test_cancellation_during_stream(self, hooks, context, hook_ctx):
        async def cancelling_stream(*args, **kwargs):
            yield StreamEvent(type="text_delta", text="partial...")
            raise asyncio.CancelledError()

        mock_router = MagicMock()
        mock_router.stream = cancelling_stream

        turn = ModelTurn(
            provider_router=mock_router, context=context,
            tool_schemas=None, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )

        with pytest.raises(asyncio.CancelledError):
            await turn.execute(hook_ctx)

    @pytest.mark.asyncio
    async def test_provider_error_propagates_via_base_turn(self, hooks, context, hook_ctx):
        mock_router = MagicMock()

        async def failing_stream(*args, **kwargs):
            raise RuntimeError("Provider crashed")
            yield

        mock_router.stream = failing_stream

        turn = ModelTurn(
            provider_router=mock_router, context=context,
            tool_schemas=None, hooks=hooks, audit=None, watchdog_timeout=10.0,
        )

        with pytest.raises(RuntimeError, match="Provider crashed"):
            await turn.execute(hook_ctx)


# ═══════════════════════════════════════════════════════════
# ToolTurn 测试
# ═══════════════════════════════════════════════════════════

class TestToolTurn:
    """ToolTurn 测试套件。"""

    @pytest.mark.asyncio
    async def test_next_turn_always_model(self, hooks, context, hook_ctx):
        tool_executor = AsyncMock(return_value=[
            ToolResult(content="file content", is_error=False, metadata={"latency_ms": 50})
        ])

        turn = ToolTurn(
            context=context, tool_executor=tool_executor,
            hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        result = await turn.execute(hook_ctx, input_data=[_make_tool_call("read_file", "tc_001")])

        assert result.kind == TurnKind.TOOL
        assert result.next_turn == TurnKind.MODEL

    @pytest.mark.asyncio
    async def test_writes_tool_results_to_context(self, hooks, context, hook_ctx):
        tool_executor = AsyncMock(return_value=[
            ToolResult(content="file content", is_error=False, metadata={"latency_ms": 30})
        ])

        turn = ToolTurn(
            context=context, tool_executor=tool_executor,
            hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx, input_data=[_make_tool_call("read_file", "tc_001")])

        messages = context.get_messages()
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content == "file content"
        assert tool_msgs[0].tool_call_id == "tc_001"

    @pytest.mark.asyncio
    async def test_tool_error_emits_tool_error_event(self, hooks, context, hook_ctx):
        tool_executor = AsyncMock(return_value=[
            ToolResult(content="Tool execution failed", is_error=True, metadata={"latency_ms": 10})
        ])

        error_events = []

        async def capture_tool_error(ctx, **kwargs):
            error_events.append(kwargs)

        hooks.on("tool_error", capture_tool_error)

        turn = ToolTurn(
            context=context, tool_executor=tool_executor,
            hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx, input_data=[_make_tool_call("bad_tool", "tc_002")])

        assert len(error_events) == 1
        assert error_events[0]["tool_name"] == "bad_tool"

    @pytest.mark.asyncio
    async def test_safety_blocked_emits_event(self, hooks, context, hook_ctx):
        tool_executor = AsyncMock(return_value=[
            ToolResult(
                content="Blocked by safety rule",
                is_error=True,
                metadata={"latency_ms": 5, "denied_by": "cli_fence"},
            )
        ])

        safety_events = []

        async def capture_safety(ctx, **kwargs):
            safety_events.append(kwargs)

        hooks.on("safety_blocked", capture_safety)

        turn = ToolTurn(
            context=context, tool_executor=tool_executor,
            hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx, input_data=[_make_tool_call("dangerous_tool", "tc_003")])

        assert len(safety_events) == 1
        assert safety_events[0]["rule"] == "cli_fence"
        assert safety_events[0]["action"] == "deny"

    @pytest.mark.asyncio
    async def test_multiple_tools_all_executed(self, hooks, context, hook_ctx):
        tool_executor = AsyncMock(return_value=[
            ToolResult(content="read result", is_error=False, metadata={"latency_ms": 20}),
            ToolResult(content="write result", is_error=False, metadata={"latency_ms": 30}),
        ])

        turn = ToolTurn(
            context=context, tool_executor=tool_executor,
            hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        result = await turn.execute(hook_ctx, input_data=[
            _make_tool_call("read_file", "tc_010"),
            _make_tool_call("write_file", "tc_011"),
        ])

        assert result.next_turn == TurnKind.MODEL
        tool_msgs = [m for m in context.get_messages() if m.role == "tool"]
        assert len(tool_msgs) == 2

    @pytest.mark.asyncio
    async def test_inline_approval_approved(self, hooks, context, hook_ctx):
        tool_executor = AsyncMock(side_effect=[
            [ToolResult(content="needs check", is_error=False, metadata={"needs_approval": True, "latency_ms": 10})],
            [ToolResult(content="executed after approval", is_error=False, metadata={"latency_ms": 5})],
        ])

        turn = ToolTurn(
            context=context, tool_executor=tool_executor,
            hooks=hooks, audit=None, watchdog_timeout=10.0,
            approval_handler=AsyncMock(return_value=[True]),
        )
        await turn.execute(hook_ctx, input_data=[_make_tool_call("dangerous_tool", "tc_app")])

        assert tool_executor.call_count == 2
        tool_msgs = [m for m in context.get_messages() if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content == "executed after approval"

    @pytest.mark.asyncio
    async def test_inline_approval_rejected(self, hooks, context, hook_ctx):
        tool_executor = AsyncMock(return_value=[
            ToolResult(content="needs check", is_error=False, metadata={"needs_approval": True, "latency_ms": 10}),
        ])

        error_events = []

        async def capture_tool_error(ctx, **kwargs):
            error_events.append(kwargs)

        hooks.on("tool_error", capture_tool_error)

        turn = ToolTurn(
            context=context, tool_executor=tool_executor,
            hooks=hooks, audit=None, watchdog_timeout=10.0,
            approval_handler=AsyncMock(return_value=[False]),
        )
        await turn.execute(hook_ctx, input_data=[_make_tool_call("dangerous_tool", "tc_rej")])

        assert tool_executor.call_count == 1
        tool_msgs = [m for m in context.get_messages() if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert "被用户拒绝执行" in tool_msgs[0].content
        assert len(error_events) == 1

    @pytest.mark.asyncio
    async def test_inline_approval_no_handler_defaults_reject(self, hooks, context, hook_ctx):
        tool_executor = AsyncMock(return_value=[
            ToolResult(content="needs check", is_error=False, metadata={"needs_approval": True}),
        ])

        turn = ToolTurn(
            context=context, tool_executor=tool_executor,
            hooks=hooks, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx, input_data=[_make_tool_call("dangerous_tool", "tc_no")])

        assert tool_executor.call_count == 1
        tool_msgs = [m for m in context.get_messages() if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert "被用户拒绝执行" in tool_msgs[0].content


# ═══════════════════════════════════════════════════════════
# AgentLoop dispatcher 测试
# ═══════════════════════════════════════════════════════════

class TestAgentLoop:
    """AgentLoop dispatcher 测试套件。"""

    @pytest.mark.asyncio
    async def test_system_then_model_text_response(self, hooks, context, hook_ctx):
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Direct answer"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        loop = AgentLoop(
            provider_router=mock_router, context=context,
            hook=hooks, max_iterations=10,
        )
        result = await loop.run(hook_ctx)

        assert result.text == "Direct answer"
        assert result.stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_system_then_model_tool_loop(self, hooks, context, hook_ctx):
        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamEvent(type="text_delta", text="Let me check.")
                yield StreamEvent(type="tool_call_start", tool_name="read_file", tool_call_id="tc_100")
                yield StreamEvent(type="tool_call_delta", tool_args_delta='{"path": "/tmp/x"}')
                yield StreamEvent(type="tool_call_end", tool_call_id="tc_100", tool_name="read_file",
                                  tool_args={"path": "/tmp/x"})
                yield StreamEvent(type="message_end", stop_reason="tool_use")
            else:
                yield StreamEvent(type="text_delta", text="The file contains hello.")
                yield StreamEvent(type="message_end", stop_reason="end_turn")

        mock_router = MagicMock()
        mock_router.stream = mock_stream

        tool_executor = AsyncMock(return_value=[
            ToolResult(content="file content: hello", is_error=False, metadata={"latency_ms": 10})
        ])

        loop = AgentLoop(
            provider_router=mock_router, context=context,
            tool_executor=tool_executor, hook=hooks, max_iterations=10,
        )
        result = await loop.run(hook_ctx)

        assert result.text == "The file contains hello."
        assert result.stop_reason == "end_turn"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_max_iterations(self, hooks, context, hook_ctx):
        async def always_tools(*args, **kwargs):
            yield StreamEvent(type="tool_call_start", tool_name="loop_tool", tool_call_id="tc_inf")
            yield StreamEvent(type="tool_call_delta", tool_args_delta='{}')
            yield StreamEvent(type="tool_call_end", tool_call_id="tc_inf", tool_name="loop_tool",
                              tool_args={})
            yield StreamEvent(type="message_end", stop_reason="tool_use")

        mock_router = MagicMock()
        mock_router.stream = always_tools

        tool_executor = AsyncMock(return_value=[
            ToolResult(content="ok", is_error=False, metadata={})
        ])

        loop = AgentLoop(
            provider_router=mock_router, context=context,
            tool_executor=tool_executor, hook=hooks, max_iterations=3,
        )
        result = await loop.run(hook_ctx)

        assert result.stop_reason == "max_iterations"
        assert "最大迭代次数" in result.text

    @pytest.mark.asyncio
    async def test_cancelled_handling(self, hooks, context, hook_ctx):
        async def cancellable_stream(*args, **kwargs):
            raise asyncio.CancelledError()
            yield

        mock_router = MagicMock()
        mock_router.stream = cancellable_stream

        loop = AgentLoop(
            provider_router=mock_router, context=context,
            hook=hooks, max_iterations=10,
        )
        result = await loop.run(hook_ctx)

        assert "cancelled" in result.stop_reason
        assert "操作已取消" in result.text

    @pytest.mark.asyncio
    async def test_create_turn_factory(self, hooks, context):
        mock_router = MagicMock()

        loop = AgentLoop(
            provider_router=mock_router, context=context,
            hook=hooks,
        )

        model_turn = loop._create_turn(TurnKind.MODEL)
        assert isinstance(model_turn, ModelTurn)

        tool_turn = loop._create_turn(TurnKind.TOOL)
        assert isinstance(tool_turn, ToolTurn)

        system_turn = loop._create_turn(TurnKind.SYSTEM)
        assert isinstance(system_turn, SystemTurn)

    @pytest.mark.asyncio
    async def test_create_turn_unknown_kind_raises(self, hooks, context):
        mock_router = MagicMock()

        loop = AgentLoop(
            provider_router=mock_router, context=context,
            hook=hooks,
        )

        with pytest.raises(ValueError, match="Unknown TurnKind"):
            loop._create_turn("invalid")


# ═══════════════════════════════════════════════════════════
# 集成测试：多轮 tool 调用链
# ═══════════════════════════════════════════════════════════

class TestIntegration:
    """集成测试：模拟真实 ReAct 多轮交互。"""

    @pytest.mark.asyncio
    async def test_multi_turn_react_loop(self, hooks, context, hook_ctx):
        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield StreamEvent(type="text_delta", text="Reading file...")
                yield StreamEvent(type="tool_call_start", tool_name="read_file", tool_call_id="tc_a")
                yield StreamEvent(type="tool_call_delta", tool_args_delta='{"path": "/tmp/a.txt"}')
                yield StreamEvent(type="tool_call_end", tool_call_id="tc_a", tool_name="read_file",
                                  tool_args={"path": "/tmp/a.txt"})
                yield StreamEvent(type="message_end", stop_reason="tool_use")
            elif call_count == 2:
                yield StreamEvent(type="text_delta", text="Writing file...")
                yield StreamEvent(type="tool_call_start", tool_name="write_file", tool_call_id="tc_b")
                yield StreamEvent(type="tool_call_delta", tool_args_delta='{"path": "/tmp/b.txt", "content": "x"}')
                yield StreamEvent(type="tool_call_end", tool_call_id="tc_b", tool_name="write_file",
                                  tool_args={"path": "/tmp/b.txt", "content": "x"})
                yield StreamEvent(type="message_end", stop_reason="tool_use")
            else:
                yield StreamEvent(type="text_delta", text="Done! File copied successfully.")
                yield StreamEvent(type="message_end", stop_reason="end_turn")

        mock_router = MagicMock()
        mock_router.stream = mock_stream

        tool_executor = AsyncMock(return_value=[
            ToolResult(content="OK", is_error=False, metadata={"latency_ms": 10})
        ])

        loop = AgentLoop(
            provider_router=mock_router, context=context,
            tool_executor=tool_executor, hook=hooks, max_iterations=10,
        )
        result = await loop.run(hook_ctx)

        assert result.text == "Done! File copied successfully."
        assert result.stop_reason == "end_turn"
        assert call_count == 3

        messages = context.get_messages()
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert len(assistant_msgs) == 3
        assert len(tool_msgs) == 2
