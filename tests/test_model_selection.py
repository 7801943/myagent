import asyncio

import pytest

from myagent.core.events import EventBus
from myagent.core.models import UserContext
from myagent.core.session.session import Session
from myagent.interfaces.web.ws_models import ModelSelectMessage
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

    def list_schemas(self):
        return []


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
        thinking_default_level="max",
        thinking_levels=[
            {"id": "high", "label": "High", "extra_body": {"reasoning_effort": "high"}},
            {"id": "max", "label": "Max", "extra_body": {"reasoning_effort": "max"}},
        ],
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


def test_openai_provider_adds_selected_thinking_level_extra_body():
    provider = OpenAIProvider(
        "glm",
        "glm-5.2",
        "test",
        thinking_supported=True,
        thinking_enabled=True,
        thinking_enabled_extra_body={"thinking": {"type": "enabled"}},
        thinking_default_level="max",
        thinking_levels=[
            {"id": "high", "label": "High", "extra_body": {"reasoning_effort": "high"}},
            {"id": "max", "label": "Max", "extra_body": {"reasoning_effort": "max"}},
        ],
    )

    kwargs = provider._build_create_kwargs(
        [{"role": "user", "content": "hi"}],
        kwargs={"extra_body": {"tool_stream": True}},
    )

    assert kwargs["extra_body"] == {
        "tool_stream": True,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }

    provider.thinking_level = "high"
    kwargs = provider._build_create_kwargs([{"role": "user", "content": "hi"}])

    assert kwargs["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


def test_openai_provider_qwen_thinking_does_not_add_reasoning_effort_by_default():
    provider = OpenAIProvider(
        "qwen",
        "qwen3.7-plus",
        "test",
        thinking_supported=True,
        thinking_enabled=True,
        thinking_enabled_extra_body={"enable_thinking": True},
        thinking_disabled_extra_body={"enable_thinking": False},
    )

    kwargs = provider._build_create_kwargs([{"role": "user", "content": "hi"}])

    assert kwargs["extra_body"] == {"enable_thinking": True}
    assert "reasoning_effort" not in kwargs["extra_body"]

    provider.thinking_enabled = False
    kwargs = provider._build_create_kwargs([{"role": "user", "content": "hi"}])

    assert kwargs["extra_body"] == {"enable_thinking": False}


def test_openai_provider_omits_thinking_extra_body_when_unsupported():
    provider = OpenAIProvider("local", "gemma", "test", thinking_supported=False)

    kwargs = provider._build_create_kwargs([{"role": "user", "content": "hi"}])

    assert "extra_body" not in kwargs


def test_session_model_selection_updates_active_model():
    session = make_session()

    asyncio.run(session.set_model_selection("primary", thinking_enabled=True, thinking_level="high"))

    assert session.data.model.active["provider_key"] == "primary"
    assert session.data.model.active["thinking_enabled"] is True
    assert session.data.model.active["thinking_level"] == "high"
    assert session.data.model.active["thinking_default_level"] == "max"
    assert session.data.model.active["thinking_levels"] == [
        {"id": "high", "label": "High"},
        {"id": "max", "label": "Max"},
    ]

    asyncio.run(session.set_model_selection("backup", thinking_enabled=False))

    assert session.data.model.active["provider_key"] == "backup"
    assert session.data.model.active["model_id"] == "gemma"
    assert session.data.model.active["thinking_supported"] is False


def test_session_model_selection_rejects_unknown_provider():
    session = make_session()

    with pytest.raises(ValueError):
        asyncio.run(session.set_model_selection("missing", thinking_enabled=False))


def test_session_model_selection_rejects_unsupported_thinking():
    session = make_session()

    with pytest.raises(ValueError):
        asyncio.run(session.set_model_selection("backup", thinking_enabled=True))


def test_session_model_selection_rejects_unknown_thinking_level():
    session = make_session()

    with pytest.raises(ValueError):
        asyncio.run(session.set_model_selection("primary", thinking_enabled=True, thinking_level="medium"))


def test_model_select_message_accepts_thinking_level():
    msg = ModelSelectMessage(
        type="model_select",
        provider_key="primary",
        thinking_enabled=True,
        thinking_level="max",
    )

    assert msg.thinking_level == "max"


def test_session_model_selection_rejects_while_running():
    session = make_session()

    async def run():
        await session._chat_lock.acquire()
        try:
            with pytest.raises(RuntimeError):
                await session.set_model_selection("backup", thinking_enabled=False)
        finally:
            session._chat_lock.release()

    asyncio.run(run())
