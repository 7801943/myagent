import asyncio

from myagent.providers.base import BaseProvider, ProviderTimeoutError, StreamEvent
from myagent.providers.router import ProviderRouter


class FakeProvider(BaseProvider):
    def __init__(self, name: str, model: str, *, fail: bool = False):
        super().__init__(name=name, model=model, api_key="test")
        self.fail = fail
        self.calls = 0

    def format_messages(self, messages: list) -> list[dict]:
        return messages

    def format_tools(self, tools: list) -> list[dict]:
        return tools

    async def stream(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        self.calls += 1
        if self.fail:
            raise ProviderTimeoutError("timeout")
        yield StreamEvent(type="text_delta", text=self.model)


def test_provider_router_tries_duplicate_provider_names_independently():
    first = FakeProvider("same-name", "first", fail=True)
    second = FakeProvider("same-name", "second", fail=False)
    router = ProviderRouter([first, second])

    async def collect():
        return [event async for event in router.stream([{"role": "user", "content": "hi"}])]

    events = asyncio.run(collect())

    assert first.calls == 1
    assert second.calls == 1
    assert any(event.type == "provider_failover" for event in events)
    assert any(event.type == "text_delta" and event.text == "second" for event in events)
