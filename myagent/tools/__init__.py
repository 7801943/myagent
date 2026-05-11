"""
MyAgent 工具系统 V3。

核心模块:
- api: ToolResult, ToolMeta, ToolLike, tool, generate_schema
- manager: ToolManager (唯一对外接口)
- engine: ExecutionEngine (纯执行逻辑)
- json_rpc: JsonRpcProxy + JsonRpcServer (JSON-RPC 协议层)
- transport: SubprocessTransport + TcpTransport (传输层)
- mcp_client: MCPClient (MCP 协议客户端)
- runner: 子进程/服务器入口点
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
from myagent.tools.engine import ExecutionEngine
from myagent.tools.json_rpc import JsonRpcProxy, JsonRpcServer, JsonRpcError
from myagent.tools.transport import (
    Transport, SubprocessTransport, TcpTransport, try_create_transport,
)
from myagent.tools.mcp_client import MCPClient

__all__ = [
    "ToolResult",
    "ToolMeta",
    "ToolLike",
    "tool",
    "generate_schema",
    "extract_description",
    "ToolManager",
    "ExecutionEngine",
    "JsonRpcProxy",
    "JsonRpcServer",
    "JsonRpcError",
    "Transport",
    "SubprocessTransport",
    "TcpTransport",
    "try_create_transport",
    "MCPClient",
]
