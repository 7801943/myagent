"""
MyAgent 工具管理器 V3。

唯一对外接口。负责工具发现、注册、调用、热加载。
执行路径统一经过 JsonRpcProxy（JSON-RPC 2.0）。
集成幂等缓存（IdempotencyCache），防止同一 tool_call_id 被重复执行。
"""
import asyncio
import importlib.util
import inspect
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
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
from myagent.tools.json_rpc import JsonRpcProxy
from myagent.tools.transport import try_create_transport
from myagent.tools.mcp_client import MCPClient

logger = logging.getLogger(__name__)


class IdempotencyCache:
    """幂等缓存：防止同一 tool_call_id 被重复执行。

    集成在 ToolManager 中，使工具执行天然具备幂等性。
    调用方通过 tool_call_id 参数（可选）启用缓存。
    """

    def __init__(self, max_size: int = 1000, ttl_seconds: float = 3600):
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[ToolResult, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, tool_call_id: str) -> ToolResult | None:
        async with self._lock:
            if tool_call_id not in self._cache:
                return None
            result, ts = self._cache[tool_call_id]
            if time.monotonic() - ts > self._ttl_seconds:
                del self._cache[tool_call_id]
                return None
            self._cache.move_to_end(tool_call_id)
            return result

    async def store(self, tool_call_id: str, result: ToolResult) -> None:
        async with self._lock:
            self._cache[tool_call_id] = (result, time.monotonic())
            self._cache.move_to_end(tool_call_id)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()


@dataclass
class _ToolRecord:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    meta: ToolMeta
    source: str

    file_path: str | None = None
    fn_name: str | None = None

    mcp_client: MCPClient | None = None
    mcp_tool_name: str | None = None

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


class ToolManager:
    """
    工具管理器。框架唯一对外接口。

    用法:
        manager = ToolManager(tools_dir="myagent/tools/tools_store",
                              runner_config={...})
        await manager.start()
        result = await manager.execute("query_weather", city="Beijing")
        await manager.stop()
    """

    def __init__(self, tools_dir: str = "myagent/tools/tools_store",
                 runner_config: dict | None = None):
        self._tools_dir = Path(tools_dir)
        self._tools: dict[str, _ToolRecord] = {}
        self._mcp_clients: dict[str, MCPClient] = {}
        self._runner_config = runner_config or {}
        self._proxy: JsonRpcProxy | None = None

        self._watch_task: asyncio.Task | None = None
        self._running = False
        self._tool_states: dict[str, float] = {}
        self._poll_interval = 1.0

        self._on_register: list[Callable[[str, str], None]] = []
        self._on_unregister: list[Callable[[str], None]] = []
        self._idempotency = IdempotencyCache()

    def on_register(self, callback: Callable[[str, str], None]) -> None:
        self._on_register.append(callback)

    def on_unregister(self, callback: Callable[[str], None]) -> None:
        self._on_unregister.append(callback)

    def register(self, tool: ToolLike | Callable,
                 meta: ToolMeta | None = None) -> None:
        if callable(tool) and inspect.iscoroutinefunction(tool):
            try:
                file_path = inspect.getfile(tool)
            except (TypeError, OSError):
                raise TypeError(
                    "Cannot determine source file for function. "
                    "Use ToolManager._register_file_tool() with explicit path.")
            self._register_file_tool(file_path, tool, meta)
        else:
            raise TypeError(
                "Only async functions are supported. "
                "Class-based ToolLike instances must be converted to async "
                "functions with @tool decorator before registration.")

    def _register_file_tool(self, file_path: str, func: Callable,
                            meta: ToolMeta | None = None) -> None:
        decorator_meta = getattr(func, "_tool_meta", {})
        name = decorator_meta.get("name") or func.__name__
        description = (decorator_meta.get("description")
                       or extract_description(func) or name)
        schema = generate_schema(func)

        merged_meta = ToolMeta()
        if decorator_meta:
            merged_meta = merged_meta.merge(
                {k: v for k, v in decorator_meta.items()
                 if k not in ("name", "description")})
        if meta:
            merged_meta = merged_meta.merge(meta.model_dump(exclude_none=True))

        record = _ToolRecord(
            name=name, description=description,
            parameters_schema=schema, meta=merged_meta,
            source="builtin",
            file_path=file_path, fn_name=func.__name__,
        )
        self._tools[name] = record
        logger.info(f"Tool registered (file): {name} <- {file_path}")
        for cb in self._on_register:
            try:
                cb(name, "builtin")
            except Exception:
                pass

    def unregister(self, name: str) -> None:
        if name in self._tools:
            del self._tools[name]
            logger.info(f"Tool unregistered: {name}")
            for cb in self._on_unregister:
                try:
                    cb(name)
                except Exception:
                    pass

    def get_schema(self, name: str) -> dict | None:
        return self._tools.get(name)

    def list_schemas(self) -> list | None:
        if not self._tools:
            return None
        return list(self._tools.values())

    def get(self, name: str) -> _ToolRecord | None:
        return self._tools.get(name)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    # ── 执行 ──

    async def execute(self, name: str, tool_call_id: str | None = None,
                      **args: Any) -> ToolResult:
        """执行工具，支持幂等缓存。

        Args:
            name: 工具名称。
            tool_call_id: 可选的工具调用 ID。传入时启用幂等缓存，
                同一 ID 不会重复执行。
            **args: 工具参数。
        """
        # 幂等缓存检查
        if tool_call_id:
            cached = await self._idempotency.get(tool_call_id)
            if cached is not None:
                logger.info(f"IdempotencyCache hit for {name} (call_id={tool_call_id})")
                return cached

        record = self._tools.get(name)
        if record is None:
            return ToolResult(
                content=f"Tool '{name}' not found. "
                        f"Available: {list(self._tools.keys())}",
                is_error=True,
            )

        timeout = record.meta.timeout
        start = time.monotonic()

        try:
            if record.source == "mcp" and record.mcp_client:
                result = await self._execute_mcp(record, args)
            elif self._proxy is None:
                return ToolResult(
                    content="ToolManager not started: proxy unavailable",
                    is_error=True,
                )
            elif record.name == "cli_execute":
                result = await self._execute_proxy_cli(record, args, timeout)
            elif record.file_path and record.fn_name:
                result = await self._execute_proxy_function(
                    record, args, timeout)
            else:
                return ToolResult(
                    content=f"Tool '{name}' has no execution path configured",
                    is_error=True,
                )

            latency_ms = int((time.monotonic() - start) * 1000)
            result.metadata["latency_ms"] = latency_ms
            if tool_call_id:
                result.metadata["tool_call_id"] = tool_call_id

            # 缓存成功执行的结果
            if tool_call_id:
                await self._idempotency.store(tool_call_id, result)

            return result

        except Exception as e:
            logger.error(f"Tool execution error ({name}): {e}")
            latency_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                content=f"Tool execution error: {e}",
                is_error=True,
                metadata={"latency_ms": latency_ms},
            )

    async def _execute_proxy_cli(self, record: _ToolRecord, args: dict,
                                 timeout: float) -> ToolResult:
        result_dict = await self._proxy.execute_cli(
            command=args["command"],
            cwd=args.get("cwd"),
            timeout=timeout,
        )
        return ToolResult(
            content=result_dict["content"],
            is_error=result_dict.get("is_error", False),
            metadata=result_dict.get("metadata", {}),
        )

    async def _execute_proxy_function(self, record: _ToolRecord, args: dict,
                                      timeout: float) -> ToolResult:
        result_dict = await self._proxy.execute_function(
            file_path=record.file_path,
            fn_name=record.fn_name,
            args=args,
            timeout=timeout,
        )
        return ToolResult(
            content=result_dict["content"],
            is_error=result_dict.get("is_error", False),
            metadata=result_dict.get("metadata", {}),
            content_blocks=result_dict.get("content_blocks"),
        )

    async def _execute_mcp(self, record: _ToolRecord,
                           args: dict) -> ToolResult:
        result_dict = await record.mcp_client.call_tool(
            record.mcp_tool_name, args)
        return ToolResult(
            content=result_dict["content"],
            is_error=result_dict.get("is_error", False),
            metadata=result_dict.get("metadata", {}),
        )

    async def execute_batch(self,
                            calls: list[dict[str, Any]]) -> list[ToolResult]:
        tasks = [self.execute(c["name"], **(c.get("arguments", {})))
                 for c in calls]
        return await asyncio.gather(*tasks)

    # ── 生命周期 ──

    async def start(self) -> None:
        if self._running:
            return

        transport, backend_name = await try_create_transport(
            self._runner_config)
        timeout_default = self._runner_config.get("timeout_default", 120.0)
        self._proxy = JsonRpcProxy(transport, default_timeout=timeout_default)
        await self._proxy.start()
        logger.info(f"ToolManager started with backend={backend_name}")

        self._running = True
        self._watch_task = asyncio.create_task(self._watch_loop())
        await self._scan()

    async def stop(self) -> None:
        self._running = False
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass

        if self._proxy:
            await self._proxy.stop()
            self._proxy = None

        logger.info("ToolManager stopped")

    # ── 热加载 ──

    async def _watch_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._scan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ToolManager scan error: {e}")

    async def _scan(self) -> None:
        if not self._tools_dir.exists():
            self._tools_dir.mkdir(parents=True, exist_ok=True)
            return

        discovered = self._discover_tools()
        current_dirs = {d["dir_path_str"]: d for d in discovered}

        for dir_path in list(self._tool_states.keys()):
            if dir_path not in current_dirs:
                removed_names = [
                    r.name for r in self._tools.values()
                    if r.file_path
                    and str(Path(r.file_path).parent) == dir_path
                ]
                for name in removed_names:
                    self.unregister(name)
                self._tool_states.pop(dir_path, None)
                logger.info(f"Tool directory removed: {dir_path}")

        for dir_path_str, info in current_dirs.items():
            try:
                max_mtime = max(f.stat().st_mtime
                                for f in info["entry_files"])
                prev_mtime = self._tool_states.get(dir_path_str, 0)
                if max_mtime > prev_mtime:
                    self._load_tool_from_dir(info)
                    self._tool_states[dir_path_str] = max_mtime
            except Exception as e:
                logger.error(f"Failed to load tool from {info['name']}: {e}")

    def _discover_tools(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if not self._tools_dir.exists():
            return results

        for item in sorted(self._tools_dir.iterdir()):
            if (not item.is_dir() or item.name.startswith("_")
                    or item.name.startswith(".")):
                continue
            py_files = [f for f in item.glob("*.py")
                        if not f.name.startswith("_")]
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
        entry_file = tool_info["entry_files"][0]
        file_path = str(entry_file)

        spec = importlib.util.spec_from_file_location(
            f"_hot_tool_{tool_info['name']}", file_path)
        if spec is None or spec.loader is None:
            logger.error(f"Cannot create module spec: {file_path}")
            return

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        async_funcs = [
            (name, obj)
            for name, obj in inspect.getmembers(module)
            if inspect.iscoroutinefunction(obj) and not name.startswith("_")
        ]
        if not async_funcs:
            logger.warning(f"No async functions found in {file_path}")
            return

        meta = ToolMeta()
        meta_file: Path = tool_info.get("meta_file", Path())
        if meta_file and meta_file.exists():
            with open(meta_file, encoding="utf-8") as f:
                local_meta = yaml.safe_load(f) or {}
            meta = meta.merge(local_meta)

        for fn_name, fn in async_funcs:
            decorator_meta = getattr(fn, "_tool_meta", {})
            tool_name = decorator_meta.get("name") or fn_name
            description = (decorator_meta.get("description")
                           or extract_description(fn) or tool_name)
            schema = generate_schema(fn)
            merged_meta = meta
            if decorator_meta:
                merged_meta = meta.merge(
                    {k: v for k, v in decorator_meta.items()
                     if k not in ("name", "description")})

            self.unregister(tool_name)

            record = _ToolRecord(
                name=tool_name, description=description,
                parameters_schema=schema, meta=merged_meta,
                source="local",
                file_path=file_path, fn_name=fn_name,
            )
            self._tools[tool_name] = record
            logger.info(f"Hot-reloaded tool: {tool_name} <- {file_path}")
            for cb in self._on_register:
                try:
                    cb(tool_name, "local")
                except Exception:
                    pass

    # ── 内置工具注册 ──

    def _register_builtin_tools(self) -> None:
        cli_record = _ToolRecord(
            name="cli_execute",
            description=(
                "在安全沙盒中执行 CLI 命令。"
                "可以执行常见的文件操作、Python 脚本、git 命令等。"
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令行命令",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "工作目录（可选，默认当前目录）",
                    },
                },
                "required": ["command"],
            },
            meta=ToolMeta(timeout=60.0),
            source="builtin",
        )
        self._tools["cli_execute"] = cli_record

        from myagent.tools.builtin.file_edit import file_edit, file_edit_table
        from myagent.tools.builtin.file_read import file_read
        from myagent.tools.builtin.file_query import file_query
        from myagent.tools.builtin.file_write import file_write

        builtin_dir = Path(__file__).parent / "builtin"
        self._register_file_tool(str(builtin_dir / "file_read.py"), file_read)
        self._register_file_tool(str(builtin_dir / "file_query.py"), file_query)
        self._register_file_tool(str(builtin_dir / "file_write.py"), file_write)
        self._register_file_tool(str(builtin_dir / "file_edit.py"), file_edit)
        self._register_file_tool(str(builtin_dir / "file_edit.py"), file_edit_table)

        logger.info(
            "Registered builtin tools: cli_execute, file_read, file_query, file_write, file_edit, file_edit_table")

    # ── MCP ──

    async def connect_mcp(self, name: str, transport: str,
                          url_or_cmd: str) -> None:
        client = MCPClient()
        await client.connect(transport, url_or_cmd, server_name=name)

        tools = await client.list_tools()
        for t in tools:
            mcp_tool_name = t["name"]
            agent_tool_name = f"mcp_{name}_{mcp_tool_name}"
            input_schema = t.get(
                "inputSchema", {"type": "object", "properties": {}})

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
        client = self._mcp_clients.pop(name, None)
        if client:
            await client.disconnect()

        removed = [r for r in list(self._tools.values())
                   if r.source == "mcp" and r.mcp_client is client]
        for r in removed:
            self.unregister(r.name)

        logger.info(
            f"MCP server disconnected: {name} ({len(removed)} tools removed)")

    @property
    def is_running(self) -> bool:
        return self._running
