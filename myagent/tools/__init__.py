"""
MyAgent 工具系统。

核心模块：
- base: BaseTool, ToolResult, ToolMeta, FunctionTool, make_tool
- registry: ToolRegistry
- executor: ToolExecutor, IdempotencyCache
- loader: ToolLoader, HotReloader
- schema: generate_schema, extract_description

子包：
- builtin/: 内置工具 (CLITool, FileReadTool, FileWriteTool)
- tools_store/: 热加载工具目录

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
from myagent.tools.loader import ToolLoader, HotReloader
from myagent.tools.schema import generate_schema, extract_description

__all__ = [
    # 核心
    "BaseTool", "ToolResult", "ToolMeta", "FunctionTool", "make_tool",
    "ToolRegistry", "ToolExecutor", "IdempotencyCache",
    "ToolLoader", "HotReloader",
    "generate_schema", "extract_description",
]