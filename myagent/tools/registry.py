"""
ToolRegistry：工具注册中心。
支持按名称注册、查找、列举工具。
"""
from myagent.tools.base import BaseTool
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class ToolRegistry:
    """工具注册中心。字典封装。"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册工具。同名工具会被覆盖。"""
        self._tools[tool.name] = tool
        logger.debug(f"Tool registered: {tool.name}")

    def unregister(self, name: str) -> None:
        """移除工具。"""
        self._tools.pop(name, None)

    def get(self, name: str) -> BaseTool | None:
        """按名称获取工具。"""
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        """返回所有已注册工具。"""
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        """返回所有已注册工具名称。"""
        return list(self._tools.keys())

    def has(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)