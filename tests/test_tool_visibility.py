from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from myagent.core.models import UserContext
from myagent.core.tools import ToolInterface
from myagent.tools.api import ToolMeta, ToolResult


def _record(name: str):
    return SimpleNamespace(
        name=name,
        description=f"{name} description",
        parameters_schema={},
        source="local",
        meta=ToolMeta(),
    )


def _manager(tool_names: list[str]):
    return SimpleNamespace(
        list_schemas=lambda: [_record(name) for name in tool_names],
        execute=AsyncMock(return_value=ToolResult(content="ok")),
    )


def _user(group: str, visible_tools=None) -> UserContext:
    return UserContext(
        user_id=f"{group}-1",
        username=f"{group}-1",
        preferences={
            "group": group,
            "visible_tools": visible_tools if visible_tools is not None else ["*"],
        },
    )


@pytest.mark.asyncio
async def test_non_admin_cannot_see_or_execute_admin_only_network_tools():
    manager = _manager(["file_read", "internet_search", "query_weather"])
    interface = ToolInterface(manager, user=_user("user", ["*"]))

    visible_names = [schema["name"] for schema in interface.list_schemas()]

    assert visible_names == ["file_read"]
    result = await interface.execute(
        "internet_search",
        {"query": "news"},
        tool_call_id="tc-search",
    )
    assert result.is_error is True
    assert result.metadata["denied_by"] == "tool_visibility"
    manager.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_can_use_admin_only_network_tools():
    manager = _manager(["internet_search", "query_weather"])
    interface = ToolInterface(manager, user=_user("admin", ["*"]))

    visible_names = [schema["name"] for schema in interface.list_schemas()]
    result = await interface.execute(
        "query_weather",
        {"city": "Shanghai"},
        tool_call_id="tc-weather",
        skip_safety=True,
    )

    assert visible_names == ["internet_search", "query_weather"]
    assert result.is_error is False
    manager.execute.assert_awaited_once_with(
        "query_weather",
        tool_call_id="tc-weather",
        city="Shanghai",
    )


def test_visible_tools_supports_explicit_exclusions():
    manager = _manager(["file_read", "query_weather"])
    interface = ToolInterface(manager, user=_user("admin", ["*", "!query_weather"]))

    visible_names = [schema["name"] for schema in interface.list_schemas()]

    assert visible_names == ["file_read"]
