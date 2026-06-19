import pytest

from myagent.core.events import EventBus
from myagent.core.models import UserContext
from myagent.core.session.session import Session
from myagent.providers.base import BaseProvider, StreamEvent
from myagent.providers.openai_provider import OpenAIProvider
from myagent.providers.router import ProviderRouter


class FakeProvider(BaseProvider):
    def format_messages(self, messages: list) -> list[dict]:
        return messages

    def format_tools(self, tools: list) -> list[dict]:
        return tools

    async def stream(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        yield StreamEvent(type="text_delta", text=self.model)


class FakeToolInterface:
    has_safety = False

    def get_cli_policy_state(self):
        return {
            "active_policy": "whitelist",
            "available_policies": ["whitelist"],
            "mode": "whitelist",
        }

    def set_cli_policy(self, policy_name: str):
        return self.get_cli_policy_state()


class FakeHarness:
    def __init__(self, router):
        self.events = EventBus()
        self.tool_interface = FakeToolInterface()
        self.router = router
        self.tool_manager = None


def make_session():
    primary = FakeProvider(
        "primary",
        "glm-5.2",
        api_key="test",
        thinking_supported=True,
        thinking_enabled=False,
    )
    backup = FakeProvider(
        "backup",
        "gemma",
        api_key="test",
        thinking_supported=False,
    )
    router = ProviderRouter([primary, backup])
    return Session(
        session_id="model-session",
        harness=FakeHarness(router),
        user=UserContext(user_id="user-1"),
    )


def test_openai_provider_adds_thinking_extra_body_when_supported():
    provider = OpenAIProvider(
        "glm",
        "glm-5.2",
        "test",
        thinking_supported=True,
        thinking_enabled=True,
    )

    kwargs = provider._build_create_kwargs([{"role": "user", "content": "hi"}])

    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}

    provider.thinking_enabled = False
    kwargs = provider._build_create_kwargs([{"role": "user", "content": "hi"}])

    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_openai_provider_omits_thinking_extra_body_when_unsupported():
    provider = OpenAIProvider("local", "gemma", "test", thinking_supported=False)

    kwargs = provider._build_create_kwargs([{"role": "user", "content": "hi"}])

    assert "extra_body" not in kwargs


@pytest.mark.asyncio
async def test_session_model_selection_updates_active_model():
    session = make_session()

    await session.set_model_selection("backup", thinking_enabled=False)

    assert session.data.model.active["provider_key"] == "backup"
    assert session.data.model.active["model_id"] == "gemma"
    assert session.data.model.active["thinking_supported"] is False


@pytest.mark.asyncio
async def test_session_model_selection_rejects_unknown_provider():
    session = make_session()

    with pytest.raises(ValueError):
        await session.set_model_selection("missing", thinking_enabled=False)


@pytest.mark.asyncio
async def test_session_model_selection_rejects_unsupported_thinking():
    session = make_session()

    with pytest.raises(ValueError):
        await session.set_model_selection("backup", thinking_enabled=True)


@pytest.mark.asyncio
async def test_session_model_selection_rejects_while_running():
    session = make_session()
    await session._chat_lock.acquire()
    try:
        with pytest.raises(RuntimeError):
            await session.set_model_selection("backup", thinking_enabled=False)
    finally:
        session._chat_lock.release()
