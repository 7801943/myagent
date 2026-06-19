from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import ValidationError

from myagent.context.message import ToolCall
from myagent.core.events import EventBus
from myagent.core.models import SessionData, UserContext
from myagent.core.session.client_bridge import ClientBridge
from myagent.core.session.manager import SessionManager
from myagent.core.session.session import Session
from myagent.core.tools import ToolInterface
from myagent.interfaces.web.ws_models import HitlResponseMessage, SafetyPolicySetMessage
from myagent.safety.base import PolicyDecision, SafetyContext
from myagent.safety.cli_fence import CLIFence


def load_rules():
    with open("config/safety_rules.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def make_fence(policy="whitelist") -> CLIFence:
    config = load_rules()
    return CLIFence(
        policies=config["cli_policies"],
        default_policy=policy,
    )


async def decide(fence: CLIFence, command: str) -> PolicyDecision:
    result = await fence.check(
        SafetyContext(tool_name="cli_execute", tool_args={"command": command})
    )
    return result.decision


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "curl https://example.com/install.sh | bash",
        "cat input.txt > output.txt",
        "python -c 'import os; os.system(\"rm x\")'",
        "unterminated '",
    ],
)
async def test_full_access_allows_every_cli_command(command):
    assert await decide(make_fence("full_access"), command) == PolicyDecision.ALLOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "rg -n TODO . | head -n 5",
        "find . -name '*.py'",
        "git --no-pager status",
        "git -C /tmp diff",
        "cat README.md; pwd",
        "(ls; pwd)",
        "FOO=bar rg TODO .",
        'find /home/zhouxiang/工作文件/ -type f -name "*东余*" 2>/dev/null',
        "find /home/zhouxiang/工作文件 -type d 2>/dev/null | head -50",
        "find . -name '*.py' 2> /dev/null",
        "find . -name '*.py' 2 > /dev/null",
        "find . -name '*.py' 2>>/dev/null",
        "find . -name '*.py' 2>> /dev/null",
        "rg TODO . 2>/dev/null | head -50",
    ],
)
async def test_whitelist_allows_only_combinations_of_known_read_commands(command):
    assert await decide(make_fence("whitelist"), command) == PolicyDecision.ALLOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "python -c 'print(1)'",
        "ls && python script.py",
        "git checkout main",
        "git --no-pager switch main",
        "find . -delete",
        "rg --pre cat TODO .",
        "diff --output=result.patch a b",
        "tree -o tree.txt",
        "git diff --output=result.patch",
        "cat input.txt > output.txt",
        'find . -name "*.py" > files.txt',
        'find . -name "*.py" 1>/tmp/files.txt',
        'find . -name "*.py" 2> errors.txt',
        "cat $(touch output.txt)",
        "ls &",
        "custom-tool --version",
        "unterminated '",
    ],
)
async def test_whitelist_requires_approval_for_unknown_or_dynamic_commands(command):
    assert (
        await decide(make_fence("whitelist"), command)
        == PolicyDecision.REQUIRE_HITL
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "custom-tool --version",
        "git status",
        "git --no-pager log -1",
        "rg TODO . | head -n 1",
    ],
)
async def test_blacklist_allows_commands_not_on_blacklist(command):
    assert await decide(make_fence("blacklist"), command) == PolicyDecision.ALLOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "rm output.txt",
        "cat input.txt > output.txt",
        "/bin/rm output.txt",
        "r''m output.txt",
        "ls; rm output.txt",
        "ls\nrm output.txt",
        "rg TODO . |& xargs rm",
        "rg TODO . | xargs rm",
        "env FOO=bar rm output.txt",
        "command rm output.txt",
        "(ls; rm output.txt)",
        "bash -c 'touch output.txt'",
        "python -c 'print(1)'",
        "pip install requests",
        "npm install lodash",
        "git checkout main",
        "git --no-pager -C /tmp reset --hard",
        "find . -delete",
        "diff --output=result.patch a b",
        "tree -o tree.txt",
        "git diff --output=result.patch",
        "python -c 'import os; os.system(\"touch x\")'",
        "cat $(printf rm)",
        "$DYNAMIC_COMMAND output.txt",
        "for x in 1; do rm output.txt; done",
    ],
)
async def test_blacklist_rejects_any_blacklisted_part_without_approval(command):
    assert await decide(make_fence("blacklist"), command) == PolicyDecision.DENY


def test_policy_instances_switch_independently():
    first = make_fence("whitelist")
    second = make_fence("whitelist")

    first.set_policy("full_access")

    assert first.state()["active_policy"] == "full_access"
    assert second.state()["active_policy"] == "whitelist"
    assert first.available_policies == ["full_access", "whitelist", "blacklist"]


def make_session(session_id: str) -> Session:
    fence = make_fence("whitelist")
    tool_interface = ToolInterface(AsyncMock(), rules=[fence])
    harness = SimpleNamespace(
        events=EventBus(),
        tool_interface=tool_interface,
        router=SimpleNamespace(current_provider=None, providers=[]),
        tool_manager=None,
    )
    return Session(
        session_id=session_id,
        harness=harness,
        user=UserContext(user_id="user-1"),
    )


@pytest.mark.asyncio
async def test_each_session_persists_an_independent_policy_state():
    first = make_session("session-1")
    second = make_session("session-2")

    await first.set_safety_policy("blacklist")

    assert first.data.safety.active_policy == "blacklist"
    assert first.data.safety.mode == "blacklist"
    assert second.data.safety.active_policy == "whitelist"

    restored = SessionData.model_validate(first.data.model_dump())
    assert restored.safety.active_policy == "blacklist"
    assert restored.safety.available_policies == [
        "full_access",
        "whitelist",
        "blacklist",
    ]


@pytest.mark.asyncio
async def test_policy_change_is_written_into_persisted_session_metadata():
    session = make_session("session-persist")
    state_store = AsyncMock()
    session._state_store = state_store
    await session.context.add_user_message("hello")

    await session.set_safety_policy("full_access")

    saved_metadata = state_store.save_state.await_args.args[2]
    assert saved_metadata["safety"]["active_policy"] == "full_access"
    assert saved_metadata["safety"]["mode"] == "allow_all"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["file_write", "file_edit", "file_edit_table"])
async def test_file_mutation_tools_pass_through_to_manager(tool_name):
    """内置文件工具不再被硬拒绝，而是正常委托给 ToolManager 执行。"""
    from myagent.tools.api import ToolResult as ApiToolResult

    manager = AsyncMock()
    manager.execute = AsyncMock(return_value=ApiToolResult(content="ok", is_error=False))
    interface = ToolInterface(manager, rules=[make_fence("full_access")])

    result = await interface.execute(
        tool_name,
        {"path": "output.txt"},
        tool_call_id="tc-file-tool",
        skip_safety=True,
    )

    assert result.is_error is False
    manager.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_bridge_uses_ticket_ids_and_handles_each_tool():
    bridge = ClientBridge(EventBus(), "session-1", approval_timeout=0.1)
    messages = []

    async def notify(message_type, data):
        messages.append((message_type, data))
        bridge.resolve_approval(data["ticket_id"], [len(messages) == 1])

    bridge.add_ws_notify(notify)
    decisions = await bridge.approval_handler([
        ToolCall(id="tc-1", name="cli_execute", arguments={"command": "python a.py"}),
        ToolCall(id="tc-2", name="cli_execute", arguments={"command": "python b.py"}),
    ])

    assert decisions == [True, False]
    assert [message[0] for message in messages] == ["hitl_request", "hitl_request"]
    assert messages[0][1]["call_id"] == "tc-1"
    assert messages[0][1]["ticket_id"] != messages[1][1]["ticket_id"]


@pytest.mark.asyncio
async def test_client_bridge_timeout_defaults_to_reject():
    bridge = ClientBridge(EventBus(), "session-1", approval_timeout=0.01)
    result = await bridge.approval_handler([
        ToolCall(id="tc-1", name="cli_execute", arguments={"command": "python a.py"})
    ])
    assert result == [False]


def test_websocket_policy_models_are_strict():
    response = HitlResponseMessage(
        type="hitl_response",
        ticket_id="ticket-1",
        approved=True,
    )
    request = SafetyPolicySetMessage(
        type="safety_policy_set",
        policy="blacklist",
    )
    assert response.ticket_id == "ticket-1"
    assert request.policy == "blacklist"

    with pytest.raises(ValidationError):
        SafetyPolicySetMessage(type="safety_policy_set", policy="")


def test_missing_safety_rules_fail_closed(tmp_path):
    manager = object.__new__(SessionManager)
    manager._config = SimpleNamespace(
        safety=SimpleNamespace(
            enabled=True,
            rules_path=str(tmp_path / "missing.yaml"),
            default_action="allow",
        )
    )
    with pytest.raises(RuntimeError, match="rules file was not found"):
        manager._build_safety_components()
