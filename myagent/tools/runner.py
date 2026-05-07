"""
MyAgent 工具执行层 V3。

统一执行入口 + MCP Client。
- SubprocessRunner: 本地子进程 JSON-RPC 执行器（替代 process_runner.py）
- MCPClient: MCP 协议客户端（替代 mcp_compat.py 骨架）
- _child_main: 子进程入口点
"""
import asyncio
import importlib
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 子进程端工具实例缓存
_tool_cache: dict[str, Any] = {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SubprocessRunner — 本地子进程执行器
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class SubprocessRunner:
    """
    本地子进程 JSON-RPC 执行器。

    每工具按需创建子进程（不复用进程池，保持极简）。
    子进程异常自动返回错误，不影响主进程。

    用法:
        runner = SubprocessRunner()
        result = await runner.execute(
            file_path="/path/to/tools_store/weather/weather_tool.py",
            fn_name="query_weather",
            args={"city": "Beijing"},
            timeout=15.0,
        )
        # -> ToolResult
    """

    _request_id = 0

    async def execute(
        self,
        file_path: str,
        fn_name: str,
        args: dict[str, Any],
        timeout: float = 30.0,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """
        在子进程中执行工具函数。

        Returns:
            {"content": str, "is_error": bool, "metadata": dict}
        """
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "method": "execute",
            "params": {
                "file_path": file_path,
                "fn_name": fn_name,
                "args": args,
                "timeout": timeout,
                "cwd": cwd or os.getcwd(),
            },
            "id": self._request_id,
        }

        try:
            proc = await self._spawn()
            try:
                request_line = json.dumps(request, ensure_ascii=False) + "\n"
                proc.stdin.write(request_line.encode())
                await proc.stdin.drain()

                response_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=timeout + 5.0
                )
                response = json.loads(response_line.decode())

                if proc.returncode is not None:
                    return {
                        "content": f"[SubprocessRunner] 子进程意外退出 (code={proc.returncode})",
                        "is_error": True,
                        "metadata": {"type": "ProcessExit", "exit_code": proc.returncode},
                    }

                if "error" in response:
                    err = response["error"]
                    return {
                        "content": f"[Error {err['code']}] {err['message']}",
                        "is_error": True,
                        "metadata": err.get("data", {}),
                    }

                return response["result"]

            finally:
                self._kill(proc)

        except asyncio.TimeoutError:
            return {
                "content": f"[SubprocessRunner] 执行超时 ({timeout}s)",
                "is_error": True,
                "metadata": {"type": "TimeoutError"},
            }
        except json.JSONDecodeError as e:
            return {
                "content": f"[SubprocessRunner] 响应解析失败: {e}",
                "is_error": True,
                "metadata": {"type": "JSONDecodeError"},
            }
        except Exception as e:
            logger.error(f"SubprocessRunner execute failed: {e}")
            return {
                "content": f"[SubprocessRunner] 执行失败: {e}",
                "is_error": True,
                "metadata": {"type": type(e).__name__},
            }

    async def _spawn(self) -> asyncio.subprocess.Process:
        """启动子进程。"""
        env = dict(os.environ)
        for key in (
            "API_KEY",
            "SECRET_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
        ):
            env.pop(key, None)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "myagent.tools.runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
            env=env,
        )
        logger.debug(f"Subprocess spawned PID={proc.pid}")

        # 后台消费 stderr 防止缓冲区满导致死锁
        asyncio.create_task(self._drain_stderr(proc))

        return proc

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """消费 stderr，防止缓冲区满导致子进程阻塞。"""
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.debug(f"[subprocess stderr] {line.decode().rstrip()}")
        except Exception:
            pass

    def _kill(self, proc: asyncio.subprocess.Process) -> None:
        """终止子进程。"""
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MCPClient — MCP 协议客户端
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class MCPClient:
    """
    MCP (Model Context Protocol) 客户端。

    连接远程 MCP Server，统一为 ToolManager 接口。
    支持 stdio transport（覆盖 90% MCP Server）。

    用法:
        client = MCPClient()
        await client.connect("stdio", "npx -y @modelcontextprotocol/server-github")
        tools = await client.list_tools()
        result = await client.call_tool("create_issue", {"title": "bug"})
        await client.disconnect()
    """

    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._server_name = ""

    async def connect(self, transport: str, url_or_cmd: str, server_name: str = "") -> None:
        """
        连接 MCP Server。

        Args:
            transport: 传输协议，当前仅支持 "stdio"
            url_or_cmd: 启动命令或 URL
            server_name: 服务名称（用于日志和命名空间）
        """
        if transport != "stdio":
            raise NotImplementedError(f"MCP transport '{transport}' not supported. Only 'stdio' is available.")

        self._server_name = server_name

        env = dict(os.environ)
        env.pop("API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        env.pop("ANTHROPIC_API_KEY", None)

        parts = url_or_cmd.split()
        self._proc = await asyncio.create_subprocess_exec(
            *parts,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
            env=env,
        )
        logger.info(f"MCP connected: {server_name or url_or_cmd} (PID={self._proc.pid})")

        # 后台消费 stderr
        asyncio.create_task(self._drain_stderr())

        # 发送 initialize 握手
        init_result = await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "myagent", "version": "3.0"},
        })
        logger.debug(f"MCP initialize result: {init_result}")

        # 发送 initialized 通知
        self._request_id += 1
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._proc.stdin.write((json.dumps(notification) + "\n").encode())
        await self._proc.stdin.drain()

    async def list_tools(self) -> list[dict[str, Any]]:
        """
        发现远程工具，返回 schema 列表。
        对应 MCP tools/list 方法。
        """
        result = await self._send_request("tools/list")
        tools = result.get("tools", [])
        for t in tools:
            t["_mcp_source"] = self._server_name
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        调用远程工具。
        对应 MCP tools/call 方法。

        Returns:
            {"content": str, "is_error": bool, "metadata": dict}
        """
        result = await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })

        mcp_content = result.get("content", [])
        text_parts = []
        is_error = result.get("isError", False)

        for item in mcp_content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif isinstance(item, str):
                text_parts.append(item)

        return {
            "content": "\n".join(text_parts) if text_parts else str(result),
            "is_error": is_error,
            "metadata": {"mcp_server": self._server_name},
        }

    async def disconnect(self) -> None:
        """断开连接。"""
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.stdin.close()
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        self._proc = None
        logger.info(f"MCP disconnected: {self._server_name}")

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送 JSON-RPC 请求并等待响应。"""
        async with self._lock:
            if not self._proc or self._proc.returncode is not None:
                raise RuntimeError("MCP server not connected")

            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
                "id": self._request_id,
            }
            request_line = json.dumps(request, ensure_ascii=False) + "\n"
            self._proc.stdin.write(request_line.encode())
            await self._proc.stdin.drain()

            response_line = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=30.0
            )
            response = json.loads(response_line.decode())

            if "error" in response:
                err = response["error"]
                raise RuntimeError(f"MCP error [{err.get('code')}]: {err.get('message')}")

            return response.get("result", {})

    async def _drain_stderr(self) -> None:
        """消费 stderr。"""
        if not self._proc:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.debug(f"[MCP stderr] {line.decode().rstrip()}")
        except Exception:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子进程端 — stdin/stdout JSON-RPC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """处理单条 JSON-RPC 请求（在子进程中执行）。"""
    req_id = request.get("id")
    params = request.get("params", {})
    method = request.get("method", "")

    if method == "ping":
        return {"jsonrpc": "2.0", "result": "pong", "id": req_id}

    if method != "execute":
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": f"Method not found: {method}"},
            "id": req_id,
        }

    file_path = params.get("file_path", "")
    fn_name = params.get("fn_name", "")
    args = params.get("args", {})
    timeout = params.get("timeout", 30.0)

    if request_cwd := params.get("cwd"):
        os.chdir(request_cwd)

    try:
        cache_key = f"{file_path}:{fn_name}"
        if cache_key not in _tool_cache:
            spec = importlib.util.spec_from_file_location("_tool_module", file_path)
            if spec is None or spec.loader is None:
                return {
                    "jsonrpc": "2.0",
                    "error": {"code": -32002, "message": f"Cannot load module: {file_path}"},
                    "id": req_id,
                }
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _tool_cache[cache_key] = module
        else:
            module = _tool_cache[cache_key]

        fn = getattr(module, fn_name, None)
        if fn is None:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32002, "message": f"Function not found: {fn_name} in {file_path}"},
                "id": req_id,
            }

        result = await asyncio.wait_for(fn(**args), timeout=timeout)

        if hasattr(result, "content") and hasattr(result, "is_error"):
            output = {
                "content": str(result.content),
                "is_error": result.is_error,
                "metadata": getattr(result, "metadata", {}),
            }
        else:
            output = {"content": str(result), "is_error": False, "metadata": {}}

        return {"jsonrpc": "2.0", "result": output, "id": req_id}

    except asyncio.TimeoutError:
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32001, "message": "Execution timeout"},
            "id": req_id,
        }
    except ImportError as e:
        _tool_cache.pop(cache_key, None)
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32002, "message": f"Import failed: {e}"},
            "id": req_id,
        }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": str(e), "data": {"type": type(e).__name__}},
            "id": req_id,
        }


async def _child_main() -> None:
    """子进程主循环：stdin 逐行读 JSON-RPC，处理，写回 stdout。"""
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    transport, _ = await asyncio.get_event_loop().connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(transport, None, reader, asyncio.get_event_loop())

    while True:
        line = await reader.readline()
        if not line:
            break

        try:
            request = json.loads(line.decode())
        except json.JSONDecodeError:
            error_response = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }
            writer.write((json.dumps(error_response, ensure_ascii=False) + "\n").encode())
            await writer.drain()
            continue

        response = await _handle_request(request)
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        await writer.drain()


if __name__ == "__main__":
    asyncio.run(_child_main())
