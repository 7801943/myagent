"""
MyAgent 工具系统。

核心模块：
- base: BaseTool, ToolResult, ToolMeta, FunctionTool, make_tool
- registry: ToolRegistry
- executor: ToolExecutor, IdempotencyCache
- loader: ToolLoader
- hot_reloader: HotReloader
- schema: generate_schema, extract_description
- process_runner: ProcessToolRunner（JSON-RPC 进程隔离执行器）
- mcp_compat: MCPTool, MCPClientManager（MCP 兼容层骨架）

子包：
- builtin/: 内置工具 (CLITool, FileReadTool, FileWriteTool)
- tools_store/: 热加载工具目录（每个工具一个子目录）

已移动（旧路径保留兼容重导出）：
- sandbox/ → runtime/sandbox/
- secrets.py → safety/secrets.py
- wrapper.py → 合并入 base.py
- idempotency.py → 合并入 executor.py
"""
# 核心类
from myagent.tools.base import BaseTool, ToolResult, ToolMeta, FunctionTool, make_tool
from myagent.tools.registry import ToolRegistry
from myagent.tools.executor import ToolExecutor, IdempotencyCache
from myagent.tools.loader import ToolLoader
from myagent.tools.hot_reloader import HotReloader
from myagent.tools.schema import generate_schema, extract_description
from myagent.tools.process_runner import ProcessToolRunner
from myagent.tools.mcp_compat import MCPTool, MCPClientManager

__all__ = [
    # 核心
    "BaseTool", "ToolResult", "ToolMeta", "FunctionTool", "make_tool",
    "ToolRegistry", "ToolExecutor", "IdempotencyCache",
    "ToolLoader", "HotReloader",
    "generate_schema", "extract_description",
    # 进程隔离
    "ProcessToolRunner",
    # MCP 兼容层
    "MCPTool", "MCPClientManager",
]
