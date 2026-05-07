"""
MyAgent 工具系统 V3。

核心模块:
- api: ToolResult, ToolMeta, ToolLike, tool, generate_schema
- manager: ToolManager (工具发现/注册/执行/热加载/MCP — 唯一对外接口)
- runner: SubprocessRunner, MCPClient (子进程执行 + MCP 协议)
"""
from myagent.tools.api import (
    ToolResult,
    ToolMeta,
    ToolLike,
    tool,
    generate_schema,
    extract_description,
)
from myagent.tools.manager import ToolManager
from myagent.tools.runner import SubprocessRunner, MCPClient

__all__ = [
    "ToolResult",
    "ToolMeta",
    "ToolLike",
    "tool",
    "generate_schema",
    "extract_description",
    "ToolManager",
    "SubprocessRunner",
    "MCPClient",
]
