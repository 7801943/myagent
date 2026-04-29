"""
Turn 抽象层独立测试。
覆盖：
- ModelTurn：有/无 tool_calls 时的 next_turn 判断、流式处理、context 写入
- ToolTurn：next_turn 固定为 MODEL、结果写入 context、错误/安全事件
- BaseTurn：取消检查传播、看门狗超时
- AgentLoop dispatcher：完整 ReAct 循环、max_iterations、cancelled 处理
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from myagent.core.hook import HookContext, HookManager
from myagent.core.turns import TurnKind, TurnResult, BaseTurn, ModelTurn, ToolTurn
from myagent.core.loop import AgentLoop
from myagent.core.stream import StreamResult
from myagent.core.cancellation import CancellationToken, CancelReason, AgentCancelledError
from myagent.context.manager import ContextManager
from myagent.context.message import ToolCall, ToolResult as MsgToolResult
from myagent.providers.base import StreamEvent
from myagent.tools.base import ToolResult
from myagent.tools.executor import ToolExecutor
from myagent.tools.registry import ToolRegistry


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def hook_ctx():
    """创建测试用 HookContext。"""
    return HookContext(session_id="test-session", agent_id="test-agent")


@pytest.fixture
def hooks():
    return HookManager()


@pytest.fixture
def context():
    return ContextManager()


@pytest.fixture
def cancel_token():
    return CancellationToken()


@pytest.fixture
def tool_registry():
    return ToolRegistry()


@pytest.fixture
def executor(tool_registry):
    return ToolExecutor(registry=tool_registry)


def _make_tool_call(name: str = "test_tool", call_id: str = "tc_001") -> ToolCall:
    """创建测试用 ToolCall。"""
    return ToolCall(id=call_id, name=name, arguments={"arg1": "value1"})


async def _mock_stream_events(events: list[StreamEvent]):
    """将 StreamEvent 列表包装为 async generator。"""
    for event in events:
        yield event


# ═══════════════════════════════════════════════════════════
# TurnResult 测试
# ═══════════════════════════════════════════════════════════

class TestTurnResult:
    """TurnResult.to_dict() 序列化测试。"""

    def test_to_dict_no_tool_calls(self):
        result = TurnResult(kind=TurnKind.MODEL, next_turn=None, stream_result=StreamResult(text="hi"))
        d = result.to_dict()
        assert d["kind"] == "MODEL"
        assert d["next_turn"] is None
        assert d["has_stream_result"] is True
        assert d["has_data"] is False

    def test_to_dict_with_tool_calls(self):
        result = TurnResult(kind=TurnKind.MODEL, next_turn=TurnKind.TOOL, data=["tc1"])
        d = result.to_dict()
        assert d["kind"] == "MODEL"
        assert d["next_turn"] == "TOOL"
        assert d["has_data"] is True

    def test_to_dict_tool_turn(self):
        result = TurnResult(kind=TurnKind.TOOL, next_turn=TurnKind.MODEL)
        d = result.to_dict()
        assert d["kind"] == "TOOL"
        assert d["next_turn"] == "MODEL"


# ═══════════════════════════════════════════════════════════
# BaseTurn 生命周期事件测试
# ═══════════════════════════════════════════════════════════

class TestBaseTurnLifecycle:
    """BaseTurn turn_start / turn_end / turn_error 生命周期测试。"""

    @pytest.mark.asyncio
    async def test_model_turn_emits_start_and_end(self, hooks, context, executor, hook_ctx):
        """正常执行时应 emit model_turn_start 和 model_turn_end。"""
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Hi"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        lifecycle_events = []
        async def capture(ctx, **kwargs):
            lifecycle_events.append(kwargs.get("_event_name", "unknown"))

        hooks.on("model_turn_start", capture)
        hooks.on("model_turn_end", capture)

        turn = ModelTurn(
            provider_router=mock_router, context=context, executor=executor,
            hooks=hooks, cancel_token=None, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx)

        assert len(lifecycle_events) == 2

    @pytest.mark.asyncio
    async def test_tool_turn_emits_start_and_end(self, hooks, context, executor, hook_ctx):
        """正常执行时应 emit tool_turn_start 和 tool_turn_end。"""
        executor.execute_batch = AsyncMock(return_value=[
            ToolResult(content="ok", is_error=False, metadata={})
        ])

        lifecycle_events = []
        async def capture(ctx, **kwargs):
            lifecycle_events.append("event")

        hooks.on("tool_turn_start", capture)
        hooks.on("tool_turn_end", capture)

        turn = ToolTurn(
            context=context, executor=executor,
            hooks=hooks, cancel_token=None, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx, input_data=[_make_tool_call()])

        assert len(lifecycle_events) == 2

    @pytest.mark.asyncio
    async def test_cancelled_before_execute_no_lifecycle_events(self, hooks, context, executor, hook_ctx, cancel_token):
        """在 execute 前取消时，不应触发任何 turn_start/end/error 事件（cancel check 在 turn_start 之前）。"""
        cancel_token.cancel(CancelReason.USER_CANCEL, "test")

        emitted = []
        async def capture(ctx, **kwargs):
            emitted.append("event")

        hooks.on("model_turn_start", capture)
        hooks.on("model_turn_end", capture)
        hooks.on("model_turn_error", capture)

        turn = ModelTurn(
            provider_router=MagicMock(), context=context, executor=executor,
            hooks=hooks, cancel_token=cancel_token, audit=None, watchdog_timeout=10.0,
        )

        with pytest.raises(AgentCancelledError):
            await turn.execute(hook_ctx)

        # cancel check 在 turn_start 之前，所以 0 个事件
        assert len(emitted) == 0

    @pytest.mark.asyncio
    async def test_cancelled_during_stream_no_turn_end(self, hooks, context, executor, hook_ctx, cancel_token):
        """在流式过程中取消时，应触发 turn_start 但不触发 turn_end 或 turn_error。"""
        # 不预先取消，而是在第一个 chunk 后手动取消
        async def cancelling_stream(*args, **kwargs):
            yield StreamEvent(type="text_delta", text="partial...")
            # 第一个 chunk 后取消，第二个 chunk 时 ModelTurn 会检查到
            cancel_token.cancel(CancelReason.USER_CANCEL, "mid-stream")
            yield StreamEvent(type="text_delta", text="should not arrive")

        mock_router = MagicMock()
        mock_router.stream = cancelling_stream

        emitted = []
        async def capture(ctx, **kwargs):
            emitted.append("event")

        hooks.on("model_turn_start", capture)
        hooks.on("model_turn_end", capture)
        hooks.on("model_turn_error", capture)

        turn = ModelTurn(
            provider_router=mock_router, context=context, executor=executor,
            hooks=hooks, cancel_token=cancel_token, audit=None, watchdog_timeout=10.0,
        )

        with pytest.raises(AgentCancelledError):
            await turn.execute(hook_ctx)

        # turn_start 已触发，但 turn_end 和 turn_error 不应触发
        assert len(emitted) == 1

    @pytest.mark.asyncio
    async def test_error_emits_turn_error(self, hooks, context, executor, hook_ctx):
        """异常时应触发 turn_error 事件。"""
        mock_router = MagicMock()

        async def failing_stream(*args, **kwargs):
            raise RuntimeError("Provider crashed")
            yield

        mock_router.stream = failing_stream

        error_events = []
        async def capture_error(ctx, **kwargs):
            error_events.append(kwargs.get("error"))

        hooks.on("model_turn_start", capture_error)
        hooks.on("model_turn_error", capture_error)
        hooks.on("model_turn_end", capture_error)

        turn = ModelTurn(
            provider_router=mock_router, context=context, executor=executor,
            hooks=hooks, cancel_token=None, audit=None, watchdog_timeout=10.0,
        )

        with pytest.raises(RuntimeError):
            await turn.execute(hook_ctx)

        # turn_start + turn_error，没有 turn_end
        assert len(error_events) == 2
        assert isinstance(error_events[1], RuntimeError)

    @pytest.mark.asyncio
    async def test_persist_turn_history_called(self, hooks, context, executor, hook_ctx):
        """_persist_turn_history 应在正常完成时被调用。"""
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Hi"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        persist_calls = []

        class TrackingModelTurn(ModelTurn):
            async def _persist_turn_history(self, ctx, result):
                persist_calls.append(result.to_dict())

        turn = TrackingModelTurn(
            provider_router=mock_router, context=context, executor=executor,
            hooks=hooks, cancel_token=None, audit=None, watchdog_timeout=10.0,
        )
        await turn.execute(hook_ctx)

        assert len(persist_calls) == 1
        assert persist_calls[0]["kind"] == "MODEL"


# ═══════════════════════════════════════════════════════════
# ModelTurn 测试
# ═══════════════════════════════════════════════════════════

class TestModelTurn:
    """ModelTurn 测试套件。"""

    @pytest.mark.asyncio
    async def test_no_tool_calls_returns_none_next(self, hooks, context, executor, hook_ctx, cancel_token):
        """ModelTurn 在没有 tool_calls 时 next_turn 应为 None（表示结束）。"""
        # 模拟 Provider 返回纯文本（无工具调用）
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Hello, "),
            StreamEvent(type="text_delta", text="world!"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        turn = ModelTurn(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=None,
            audit=None,
            watchdog_timeout=10.0,
        )

        result = await turn.execute(hook_ctx)

        assert result.kind == TurnKind.MODEL
        assert result.next_turn is None
        assert result.stream_result is not None
        assert result.stream_result.text == "Hello, world!"
        assert result.stream_result.tool_calls == []

    @pytest.mark.asyncio
    async def test_with_tool_calls_returns_tool_next(self, hooks, context, executor, hook_ctx):
        """ModelTurn 在有 tool_calls 时 next_turn 应为 TOOL。"""
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
            provider_router=mock_router,
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=None,
            audit=None,
            watchdog_timeout=10.0,
        )

        result = await turn.execute(hook_ctx)

        assert result.kind == TurnKind.MODEL
        assert result.next_turn == TurnKind.TOOL
        assert result.data is not None
        assert len(result.data) == 1
        assert result.data[0].name == "read_file"

    @pytest.mark.asyncio
    async def test_writes_assistant_message_to_context(self, hooks, context, executor, hook_ctx):
        """ModelTurn 应将 assistant 消息写入 context。"""
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Response text"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        turn = ModelTurn(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=None,
            audit=None,
            watchdog_timeout=10.0,
        )

        await turn.execute(hook_ctx)

        messages = context.get_messages()
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0].content == "Response text"

    @pytest.mark.asyncio
    async def test_cancellation_during_stream(self, hooks, context, executor, hook_ctx, cancel_token):
        """ModelTurn 在流式过程中取消应抛出 AgentCancelledError。"""
        cancel_token.cancel(CancelReason.USER_CANCEL, "test cancel")

        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="partial..."),
        ])

        turn = ModelTurn(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=cancel_token,
            audit=None,
            watchdog_timeout=10.0,
        )

        with pytest.raises(AgentCancelledError) as exc_info:
            await turn.execute(hook_ctx)

        assert exc_info.value.reason == CancelReason.USER_CANCEL

    @pytest.mark.asyncio
    async def test_cancellation_before_execute(self, hooks, context, executor, hook_ctx, cancel_token):
        """BaseTurn.execute() 在执行前检查取消，应立即抛出。"""
        cancel_token.cancel(CancelReason.USER_CANCEL, "pre-cancel")

        turn = ModelTurn(
            provider_router=MagicMock(),
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=cancel_token,
            audit=None,
            watchdog_timeout=10.0,
        )

        with pytest.raises(AgentCancelledError):
            await turn.execute(hook_ctx)

    @pytest.mark.asyncio
    async def test_hook_events_emitted(self, hooks, context, executor, hook_ctx):
        """ModelTurn 应正确触发 provider_call_start/end, state_change 等 Hook 事件。"""
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Hi"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        emitted_events = []

        async def capture_hook(ctx, **kwargs):
            emitted_events.append(ctx)

        hooks.on("provider_call_start", capture_hook)
        hooks.on("provider_call_end", capture_hook)
        hooks.on("state_change", capture_hook)

        turn = ModelTurn(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=None,
            audit=None,
            watchdog_timeout=10.0,
        )

        await turn.execute(hook_ctx)

        # provider_call_start, state_change(thinking), state_change(running), provider_call_end
        assert len(emitted_events) >= 3

    @pytest.mark.asyncio
    async def test_provider_error_propagates(self, hooks, context, executor, hook_ctx):
        """ModelTurn 在 Provider 抛出异常时应正确传播。"""
        mock_router = MagicMock()

        async def failing_stream(*args, **kwargs):
            raise RuntimeError("Provider crashed")
            yield  # make it an async generator

        mock_router.stream = failing_stream

        turn = ModelTurn(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=None,
            audit=None,
            watchdog_timeout=10.0,
        )

        with pytest.raises(RuntimeError, match="Provider crashed"):
            await turn.execute(hook_ctx)


# ═══════════════════════════════════════════════════════════
# ToolTurn 测试
# ═══════════════════════════════════════════════════════════

class TestToolTurn:
    """ToolTurn 测试套件。"""

    @pytest.mark.asyncio
    async def test_next_turn_always_model(self, hooks, context, executor, hook_ctx):
        """ToolTurn 的 next_turn 应始终为 MODEL。"""
        tool_calls = [_make_tool_call("read_file", "tc_001")]

        # mock execute_batch
        executor.execute_batch = AsyncMock(return_value=[
            ToolResult(content="file content here", is_error=False, metadata={"latency_ms": 50})
        ])

        turn = ToolTurn(
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=None,
            audit=None,
            watchdog_timeout=10.0,
        )

        result = await turn.execute(hook_ctx, input_data=tool_calls)

        assert result.kind == TurnKind.TOOL
        assert result.next_turn == TurnKind.MODEL

    @pytest.mark.asyncio
    async def test_writes_tool_results_to_context(self, hooks, context, executor, hook_ctx):
        """ToolTurn 应将工具执行结果写入 context。"""
        tool_calls = [_make_tool_call("read_file", "tc_001")]

        executor.execute_batch = AsyncMock(return_value=[
            ToolResult(content="file content", is_error=False, metadata={"latency_ms": 30})
        ])

        turn = ToolTurn(
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=None,
            audit=None,
            watchdog_timeout=10.0,
        )

        await turn.execute(hook_ctx, input_data=tool_calls)

        messages = context.get_messages()
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content == "file content"
        assert tool_msgs[0].tool_call_id == "tc_001"

    @pytest.mark.asyncio
    async def test_tool_error_emits_tool_error_event(self, hooks, context, executor, hook_ctx):
        """ToolTurn 在工具执行失败时应触发 tool_error 事件。"""
        tool_calls = [_make_tool_call("bad_tool", "tc_002")]

        executor.execute_batch = AsyncMock(return_value=[
            ToolResult(content="Tool execution failed", is_error=True, metadata={"latency_ms": 10})
        ])

        error_events = []

        async def capture_tool_error(ctx, **kwargs):
            error_events.append(kwargs)

        hooks.on("tool_error", capture_tool_error)

        turn = ToolTurn(
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=None,
            audit=None,
            watchdog_timeout=10.0,
        )

        await turn.execute(hook_ctx, input_data=tool_calls)

        assert len(error_events) == 1
        assert error_events[0]["tool_name"] == "bad_tool"

    @pytest.mark.asyncio
    async def test_safety_blocked_emits_event(self, hooks, context, executor, hook_ctx):
        """ToolTurn 在安全拦截时应触发 safety_blocked 事件。"""
        tool_calls = [_make_tool_call("dangerous_tool", "tc_003")]

        executor.execute_batch = AsyncMock(return_value=[
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
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=None,
            audit=None,
            watchdog_timeout=10.0,
        )

        await turn.execute(hook_ctx, input_data=tool_calls)

        assert len(safety_events) == 1
        assert safety_events[0]["rule"] == "cli_fence"
        assert safety_events[0]["action"] == "deny"

    @pytest.mark.asyncio
    async def test_multiple_tools_all_executed(self, hooks, context, executor, hook_ctx):
        """ToolTurn 应正确处理多个工具调用。"""
        tool_calls = [
            _make_tool_call("read_file", "tc_010"),
            _make_tool_call("write_file", "tc_011"),
        ]

        executor.execute_batch = AsyncMock(return_value=[
            ToolResult(content="read result", is_error=False, metadata={"latency_ms": 20}),
            ToolResult(content="write result", is_error=False, metadata={"latency_ms": 30}),
        ])

        turn = ToolTurn(
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=None,
            audit=None,
            watchdog_timeout=10.0,
        )

        result = await turn.execute(hook_ctx, input_data=tool_calls)

        assert result.next_turn == TurnKind.MODEL
        messages = context.get_messages()
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert len(tool_msgs) == 2

    @pytest.mark.asyncio
    async def test_cancellation_before_execution(self, hooks, context, executor, hook_ctx, cancel_token):
        """ToolTurn 在执行前取消应抛出 AgentCancelledError。"""
        cancel_token.cancel(CancelReason.USER_CANCEL, "pre-tool-cancel")

        turn = ToolTurn(
            context=context,
            executor=executor,
            hooks=hooks,
            cancel_token=cancel_token,
            audit=None,
            watchdog_timeout=10.0,
        )

        with pytest.raises(AgentCancelledError):
            await turn.execute(hook_ctx, input_data=[_make_tool_call()])


# ═══════════════════════════════════════════════════════════
# AgentLoop dispatcher 测试
# ═══════════════════════════════════════════════════════════

class TestAgentLoop:
    """AgentLoop dispatcher 测试套件。"""

    @pytest.mark.asyncio
    async def test_simple_text_response(self, hooks, context, executor, hook_ctx):
        """AgentLoop 在模型直接回复文本时应在第一次迭代结束。"""
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Direct answer"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        loop = AgentLoop(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hook=hooks,
            max_iterations=10,
        )

        result = await loop.run(hook_ctx)

        assert result.text == "Direct answer"
        assert result.stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_tool_then_text(self, hooks, context, executor, hook_ctx):
        """AgentLoop 应正确处理 tool_use → tool_result → text 的两轮循环。"""
        # 第一次调用返回 tool_calls
        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 第一次：返回工具调用
                yield StreamEvent(type="text_delta", text="Let me check.")
                yield StreamEvent(type="tool_call_start", tool_name="read_file", tool_call_id="tc_100")
                yield StreamEvent(type="tool_call_delta", tool_args_delta='{"path": "/tmp/x"}')
                yield StreamEvent(type="tool_call_end", tool_call_id="tc_100", tool_name="read_file",
                                  tool_args={"path": "/tmp/x"})
                yield StreamEvent(type="message_end", stop_reason="tool_use")
            else:
                # 第二次：返回最终文本
                yield StreamEvent(type="text_delta", text="The file contains hello.")
                yield StreamEvent(type="message_end", stop_reason="end_turn")

        mock_router = MagicMock()
        mock_router.stream = mock_stream

        executor.execute_batch = AsyncMock(return_value=[
            ToolResult(content="file content: hello", is_error=False, metadata={"latency_ms": 10})
        ])

        loop = AgentLoop(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hook=hooks,
            max_iterations=10,
        )

        result = await loop.run(hook_ctx)

        assert result.text == "The file contains hello."
        assert result.stop_reason == "end_turn"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_max_iterations(self, hooks, context, executor, hook_ctx):
        """AgentLoop 达到最大迭代次数应返回 max_iterations。"""
        # 每次 Provider 都返回 tool_calls，永不结束
        async def always_tools(*args, **kwargs):
            yield StreamEvent(type="tool_call_start", tool_name="loop_tool", tool_call_id="tc_inf")
            yield StreamEvent(type="tool_call_delta", tool_args_delta='{}')
            yield StreamEvent(type="tool_call_end", tool_call_id="tc_inf", tool_name="loop_tool",
                              tool_args={})
            yield StreamEvent(type="message_end", stop_reason="tool_use")

        mock_router = MagicMock()
        mock_router.stream = always_tools

        executor.execute_batch = AsyncMock(return_value=[
            ToolResult(content="ok", is_error=False, metadata={})
        ])

        loop = AgentLoop(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hook=hooks,
            max_iterations=3,
        )

        result = await loop.run(hook_ctx)

        assert result.stop_reason == "max_iterations"
        assert "最大迭代次数" in result.text

    @pytest.mark.asyncio
    async def test_cancelled_handling(self, hooks, context, executor, hook_ctx, cancel_token):
        """AgentLoop 在取消时应返回 cancelled StreamResult。"""
        cancel_token.cancel(CancelReason.USER_CANCEL, "user pressed Ctrl+C")

        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="partial..."),
        ])

        loop = AgentLoop(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hook=hooks,
            cancel_token=cancel_token,
            max_iterations=10,
        )

        result = await loop.run(hook_ctx)

        assert "cancelled" in result.stop_reason
        assert "操作已取消" in result.text

    @pytest.mark.asyncio
    async def test_iteration_events_emitted(self, hooks, context, executor, hook_ctx):
        """AgentLoop dispatcher 应正确触发 iteration_start/end 事件。"""
        mock_router = MagicMock()
        mock_router.stream.return_value = _mock_stream_events([
            StreamEvent(type="text_delta", text="Done"),
            StreamEvent(type="message_end", stop_reason="end_turn"),
        ])

        events_log = []

        async def log_event(ctx, **kwargs):
            events_log.append("event")

        hooks.on("iteration_start", log_event)
        hooks.on("iteration_end", log_event)
        hooks.on("state_change", log_event)

        loop = AgentLoop(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hook=hooks,
        )

        await loop.run(hook_ctx)

        # iteration_start + (provider events from ModelTurn) + state_change(finished) + iteration_end
        assert any(True for e in events_log)

    @pytest.mark.asyncio
    async def test_create_turn_factory(self, hooks, context, executor):
        """AgentLoop._create_turn 应正确创建不同类型的 Turn。"""
        mock_router = MagicMock()

        loop = AgentLoop(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hook=hooks,
        )

        model_turn = loop._create_turn(TurnKind.MODEL)
        assert isinstance(model_turn, ModelTurn)

        tool_turn = loop._create_turn(TurnKind.TOOL)
        assert isinstance(tool_turn, ToolTurn)

    @pytest.mark.asyncio
    async def test_create_turn_unknown_kind_raises(self, hooks, context, executor):
        """AgentLoop._create_turn 对未知 TurnKind 应抛出 ValueError。"""
        mock_router = MagicMock()

        loop = AgentLoop(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hook=hooks,
        )

        with pytest.raises(ValueError, match="Unknown TurnKind"):
            loop._create_turn("invalid")


# ═══════════════════════════════════════════════════════════
# 集成测试：多轮 tool 调用链
# ═══════════════════════════════════════════════════════════

class TestIntegration:
    """集成测试：模拟真实的 ReAct 多轮交互。"""

    @pytest.mark.asyncio
    async def test_multi_turn_react_loop(self, hooks, context, executor, hook_ctx):
        """模拟：用户提问 → read_file → write_file → 最终回复。"""
        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 第一轮：调用 read_file
                yield StreamEvent(type="text_delta", text="Reading file...")
                yield StreamEvent(type="tool_call_start", tool_name="read_file", tool_call_id="tc_a")
                yield StreamEvent(type="tool_call_delta", tool_args_delta='{"path": "/tmp/a.txt"}')
                yield StreamEvent(type="tool_call_end", tool_call_id="tc_a", tool_name="read_file",
                                  tool_args={"path": "/tmp/a.txt"})
                yield StreamEvent(type="message_end", stop_reason="tool_use")
            elif call_count == 2:
                # 第二轮：调用 write_file
                yield StreamEvent(type="text_delta", text="Writing file...")
                yield StreamEvent(type="tool_call_start", tool_name="write_file", tool_call_id="tc_b")
                yield StreamEvent(type="tool_call_delta", tool_args_delta='{"path": "/tmp/b.txt", "content": "x"}')
                yield StreamEvent(type="tool_call_end", tool_call_id="tc_b", tool_name="write_file",
                                  tool_args={"path": "/tmp/b.txt", "content": "x"})
                yield StreamEvent(type="message_end", stop_reason="tool_use")
            else:
                # 第三轮：最终回复
                yield StreamEvent(type="text_delta", text="Done! File copied successfully.")
                yield StreamEvent(type="message_end", stop_reason="end_turn")

        mock_router = MagicMock()
        mock_router.stream = mock_stream

        executor.execute_batch = AsyncMock(return_value=[
            ToolResult(content="OK", is_error=False, metadata={"latency_ms": 10})
        ])

        loop = AgentLoop(
            provider_router=mock_router,
            context=context,
            executor=executor,
            hook=hooks,
            max_iterations=10,
        )

        result = await loop.run(hook_ctx)

        assert result.text == "Done! File copied successfully."
        assert result.stop_reason == "end_turn"
        assert call_count == 3

        # 验证 context 中有正确的消息序列
        messages = context.get_messages()
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert len(assistant_msgs) == 3  # 3 轮 assistant 消息
        assert len(tool_msgs) == 2  # 2 轮工具结果