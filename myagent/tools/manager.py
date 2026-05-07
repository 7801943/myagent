"""
MyAgent 工具管理器 V3。

唯一对外接口。负责工具发现、注册、调用、热加载。
替代旧 registry.py + executor.py + loader.py + hot_reloader.py。

架构：
    tools_store/*.py  ──watchdog──→  ToolManager  ──→  SubprocessRunner (本地)
                                                           └──  MCPClient (远程)
"""
import asyncio
import importlib
import importlib.util
import inspect
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from myagent.tools.api import (
    ToolResult,
    ToolMeta,
    ToolLike,
    generate_schema,
    extract_description,
)
from myagent.tools.runner import SubprocessRunner, MCPClient

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ToolInfo — 内部工具注册记录
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class _ToolRecord:
    """工具注册记录。"""

    name: str
    description: str
    parameters_schema: dict[str, Any]
    meta: ToolMeta
    source: str  # "local", "mcp", "builtin"

    # 本地函数工具
    file_path: str | None = None
    fn_name: str | None = None
    fn: Callable | None = None  # 进程中直接调用（class-based tools only）

    # MCP 远程工具
    mcp_client: MCPClient | None = None
    mcp_tool_name: str | None = None  # 原始 MCP 工具名

    def to_schema(self) -> dict[str, Any]:
        """转为 Provider format_tools 所需的字典格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    @property
    def tool_entry(self) -> str | None:
        """返回子进程加载所需的入口字符串。"""
        if self.file_path and self.fn_name:
            return f"{self.file_path}:{self.fn_name}"
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ToolManager — 唯一对外接口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ToolManager:
    """
    工具管理器。框架唯一对外接口。

    用法:
        manager = ToolManager(tools_dir="myagent/tools/tools_store")
        await manager.start()
        schemas = manager.list_schemas()
        result = await manager.execute("query_weather", city="Beijing")
        await manager.stop()
    """

    def __init__(self, tools_dir: str = "myagent/tools/tools_store"):
        self._tools_dir = Path(tools_dir)
        self._tools: dict[str, _ToolRecord] = {}
        self._runner = SubprocessRunner()
        self._mcp_clients: dict[str, MCPClient] = {}

        # 热加载状态
        self._watch_task: asyncio.Task | None = None
        self._running = False
        self._tool_states: dict[str, float] = {}  # dir_path → mtime
        self._poll_interval = 1.0  # 1s 轮询（轻量替代 watchdog 库依赖）

        # 事件回调
        self._on_register: list[Callable[[str, str], None]] = []
        self._on_unregister: list[Callable[[str], None]] = []

    # ── 事件 ──

    def on_register(self, callback: Callable[[str, str], None]) -> None:
        """注册事件：callback(tool_name, source)。"""
        self._on_register.append(callback)

    def on_unregister(self, callback: Callable[[str], None]) -> None:
        """注销事件：callback(tool_name)。"""
        self._on_unregister.append(callback)

    # ── 注册 / 注销 ──

    def register(
        self,
        tool: ToolLike | Callable,
        meta: ToolMeta | None = None,
    ) -> None:
        """
        注册工具。支持 ToolLike 实例（class-based）或 async 函数。

        用法:
            # Class-based 内置工具
            manager.register(CLITool(sandbox))

            # 函数工具（自动推断 schema）
            manager.register(query_weather, meta=ToolMeta(timeout=15))
        """
        if isinstance(tool, ToolLike):
            self._register_class_tool(tool, meta)
        elif callable(tool):
            self._register_func_tool(tool, meta)
        else:
            raise TypeError(f"tool 必须为 ToolLike 实例或 async callable，实际为 {type(tool)}")

    def _register_class_tool(self, tool: ToolLike, meta: ToolMeta | None = None) -> None:
        """注册 class-based 工具（如 CLITool, FileReadTool）。"""
        record = _ToolRecord(
            name=tool.name,
            description=tool.description,
            parameters_schema=tool.parameters_schema,
            meta=meta or getattr(tool, "meta", None) or ToolMeta(),
            source="builtin",
            fn=tool.execute,  # 进程中直接调用
        )
        self._tools[tool.name] = record
        logger.info(f"Tool registered (class): {tool.name}")
        for cb in self._on_register:
            try:
                cb(tool.name, "builtin")
            except Exception:
                pass

    def _register_func_tool(self, func: Callable, meta: ToolMeta | None = None) -> None:
        """注册函数工具（进程中执行，不走子进程）。"""
        decorator_meta = getattr(func, "_tool_meta", {})
        name = decorator_meta.get("name") or func.__name__
        description = decorator_meta.get("description") or extract_description(func) or name
        schema = generate_schema(func)

        merged_meta = ToolMeta()
        if decorator_meta:
            merged_meta = merged_meta.merge({k: v for k, v in decorator_meta.items() if k not in ("name", "description")})
        if meta:
            merged_meta = merged_meta.merge(meta.model_dump(exclude_none=True))

        record = _ToolRecord(
            name=name,
            description=description,
            parameters_schema=schema,
            meta=merged_meta,
            source="builtin",
            fn=func,
        )
        self._tools[name] = record
        logger.info(f"Tool registered (func): {name}")
        for cb in self._on_register:
            try:
                cb(name, "builtin")
            except Exception:
                pass

    def unregister(self, name: str) -> None:
        """注销工具。"""
        if name in self._tools:
            del self._tools[name]
            logger.info(f"Tool unregistered: {name}")
            for cb in self._on_unregister:
                try:
                    cb(name)
                except Exception:
                    pass

    # ── Schema 查询 ──

    def get_schema(self, name: str) -> dict | None:
        """返回单个工具的 Schema 对象（供 format_tools 使用）。"""
        return self._tools.get(name)

    def list_schemas(self) -> list | None:
        """返回所有工具的 Schema 对象列表（供 Provider format_tools 使用）。无工具时返回 None。"""
        if not self._tools:
            return None
        return list(self._tools.values())

    def get(self, name: str) -> _ToolRecord | None:
        """获取工具记录。"""
        return self._tools.get(name)

    @property
    def tool_names(self) -> list[str]:
        """所有已注册工具名称。"""
        return list(self._tools.keys())

    # ── 执行 ──

    async def execute(self, name: str, **args: Any) -> ToolResult:
        """
        执行单个工具。自动路由到本地子进程 / 直接调用 / MCP 远程。

        Args:
            name: 工具名称
            **args: 工具参数
        """
        record = self._tools.get(name)
        if record is None:
            return ToolResult(
                content=f"Tool '{name}' not found. Available: {list(self._tools.keys())}",
                is_error=True,
            )

        timeout = record.meta.timeout
        start = time.monotonic()

        try:
            if record.source == "mcp" and record.mcp_client:
                return await self._execute_mcp(record, args)

            if record.fn is not None:
                return await self._execute_inprocess(record, args, timeout)

            if record.file_path and record.fn_name:
                return await self._execute_subprocess(record, args, timeout)

            return ToolResult(
                content=f"Tool '{name}' has no execution path configured",
                is_error=True,
            )

        except Exception as e:
            logger.error(f"Tool execution error ({name}): {e}")
            latency_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                content=f"Tool execution error: {e}",
                is_error=True,
                metadata={"latency_ms": latency_ms},
            )

    async def _execute_inprocess(self, record: _ToolRecord, args: dict, timeout: float) -> ToolResult:
        """进程中直接执行（class-based 工具的 execute 方法）。"""
        result = await asyncio.wait_for(record.fn(**args), timeout=timeout)
        if isinstance(result, ToolResult):
            return result
        return ToolResult(content=str(result))

    async def _execute_subprocess(self, record: _ToolRecord, args: dict, timeout: float) -> ToolResult:
        """通过子进程 JSON-RPC 执行（函数工具，隔离安全）。"""
        result_dict = await self._runner.execute(
            file_path=record.file_path,
            fn_name=record.fn_name,
            args=args,
            timeout=timeout,
        )
        return ToolResult(
            content=result_dict["content"],
            is_error=result_dict.get("is_error", False),
            metadata=result_dict.get("metadata", {}),
        )

    async def _execute_mcp(self, record: _ToolRecord, args: dict) -> ToolResult:
        """通过 MCP Client 执行远程工具。"""
        result_dict = await record.mcp_client.call_tool(record.mcp_tool_name, args)
        return ToolResult(
            content=result_dict["content"],
            is_error=result_dict.get("is_error", False),
            metadata=result_dict.get("metadata", {}),
        )

    async def execute_batch(self, calls: list[dict[str, Any]]) -> list[ToolResult]:
        """
        批量并行执行工具。

        Args:
            calls: [{"name": "x", "arguments": {...}}, ...]
        """
        tasks = [self.execute(c["name"], **(c.get("arguments", {}))) for c in calls]
        return await asyncio.gather(*tasks)

    # ── 热加载 ──

    async def start(self) -> None:
        """启动热加载（后台扫描 tools_store/）。"""
        if self._running:
            return

        self._running = True
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info(f"ToolManager started: watch_dir={self._tools_dir}")

        # 首次立即扫描
        await self._scan()

    async def stop(self) -> None:
        """停止热加载。"""
        self._running = False
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        logger.info("ToolManager stopped")

    async def _watch_loop(self) -> None:
        """后台扫描循环。"""
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._scan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ToolManager scan error: {e}")

    async def _scan(self) -> None:
        """扫描 tools_store/，检测新增/变更/删除的工具。"""
        if not self._tools_dir.exists():
            self._tools_dir.mkdir(parents=True, exist_ok=True)
            return

        discovered = self._discover_tools()
        current_dirs = {d["dir_path_str"]: d for d in discovered}

        # 检测删除的目录 → 注销
        for dir_path in list(self._tool_states.keys()):
            if dir_path not in current_dirs:
                removed_names = [r.name for r in self._tools.values() if r.file_path and str(Path(r.file_path).parent) == dir_path]
                for name in removed_names:
                    self.unregister(name)
                self._tool_states.pop(dir_path, None)
                logger.info(f"Tool directory removed: {dir_path}")

        # 检测新增/变更
        for dir_path_str, info in current_dirs.items():
            try:
                max_mtime = max(f.stat().st_mtime for f in info["entry_files"])
                prev_mtime = self._tool_states.get(dir_path_str, 0)

                if max_mtime > prev_mtime:
                    self._load_tool_from_dir(info)
                    self._tool_states[dir_path_str] = max_mtime

            except Exception as e:
                logger.error(f"Failed to load tool from {info['name']}: {e}")

    def _discover_tools(self) -> list[dict[str, Any]]:
        """发现 tools_store/ 下的所有工具子目录。"""
        results: list[dict[str, Any]] = []
        if not self._tools_dir.exists():
            return results

        for item in sorted(self._tools_dir.iterdir()):
            if not item.is_dir() or item.name.startswith("_") or item.name.startswith("."):
                continue

            py_files = [f for f in item.glob("*.py") if not f.name.startswith("_")]
            if not py_files:
                continue

            results.append({
                "name": item.name,
                "dir": item,
                "dir_path_str": str(item.resolve()),
                "entry_files": py_files,
                "meta_file": item / "meta.yaml",
            })

        return results

    def _load_tool_from_dir(self, tool_info: dict[str, Any]) -> None:
        """从工具目录加载函数并注册。"""
        entry_file = tool_info["entry_files"][0]
        file_path = str(entry_file)

        # 用 importlib 加载模块（替代 exec）
        spec = importlib.util.spec_from_file_location(
            f"_hot_tool_{tool_info['name']}", file_path
        )
        if spec is None or spec.loader is None:
            logger.error(f"Cannot create module spec: {file_path}")
            return

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # 查找 async 函数
        async_funcs = [
            (name, obj)
            for name, obj in inspect.getmembers(module)
            if inspect.iscoroutinefunction(obj) and not name.startswith("_")
        ]

        if not async_funcs:
            logger.warning(f"No async functions found in {file_path}")
            return

        # 加载 meta.yaml (可选)
        meta = ToolMeta()
        meta_file: Path = tool_info.get("meta_file", Path())
        if meta_file and meta_file.exists():
            with open(meta_file, encoding="utf-8") as f:
                local_meta = yaml.safe_load(f) or {}
            meta = meta.merge(local_meta)

        for fn_name, fn in async_funcs:
            # @tool 装饰器覆盖
            decorator_meta = getattr(fn, "_tool_meta", {})
            tool_name = decorator_meta.get("name") or fn_name
            description = decorator_meta.get("description") or extract_description(fn) or tool_name
            schema = generate_schema(fn)
            merged_meta = meta
            if decorator_meta:
                merged_meta = meta.merge({k: v for k, v in decorator_meta.items() if k not in ("name", "description")})

            # 注销旧同名工具
            self.unregister(tool_name)

            record = _ToolRecord(
                name=tool_name,
                description=description,
                parameters_schema=schema,
                meta=merged_meta,
                source="local",
                file_path=file_path,
                fn_name=fn_name,
            )
            self._tools[tool_name] = record
            logger.info(f"Hot-reloaded tool: {tool_name} <- {file_path}")
            for cb in self._on_register:
                try:
                    cb(tool_name, "local")
                except Exception:
                    pass

    # ── MCP ──

    async def connect_mcp(self, name: str, transport: str, url_or_cmd: str) -> None:
        """
        连接 MCP Server，自动发现并注册其工具。

        Args:
            name: MCP Server 名称
            transport: 传输协议（当前仅 "stdio"）
            url_or_cmd: 启动命令或 URL
        """
        client = MCPClient()
        await client.connect(transport, url_or_cmd, server_name=name)

        tools = await client.list_tools()
        for t in tools:
            mcp_tool_name = t["name"]
            agent_tool_name = f"mcp_{name}_{mcp_tool_name}"
            input_schema = t.get("inputSchema", {"type": "object", "properties": {}})

            record = _ToolRecord(
                name=agent_tool_name,
                description=f"[MCP:{name}] {t.get('description', mcp_tool_name)}",
                parameters_schema=input_schema,
                meta=ToolMeta(source="mcp", category="external", timeout=30.0),
                source="mcp",
                mcp_client=client,
                mcp_tool_name=mcp_tool_name,
            )
            self._tools[agent_tool_name] = record
            logger.info(f"MCP tool registered: {agent_tool_name} <- {name}")

        self._mcp_clients[name] = client
        logger.info(f"MCP server connected: {name} ({len(tools)} tools)")

    async def disconnect_mcp(self, name: str) -> None:
        """断开 MCP Server 并注销其工具。"""
        client = self._mcp_clients.pop(name, None)
        if client:
            await client.disconnect()

        removed = [r for r in list(self._tools.values()) if r.source == "mcp" and r.mcp_client is client]
        for r in removed:
            self.unregister(r.name)

        logger.info(f"MCP server disconnected: {name} ({len(removed)} tools removed)")

    @property
    def is_running(self) -> bool:
        """是否正在运行热加载。"""
        return self._running
