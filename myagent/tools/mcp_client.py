"""
MCP (Model Context Protocol) 客户端。
独立于 JSON-RPC 执行路径，处理第三方 MCP Server 连接。
"""
import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class MCPClient:
    """
    MCP (Model Context Protocol) 客户端。

    连接远程 MCP Server，统一为 ToolManager 接口。
    支持 stdio transport。

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

    async def connect(self, transport: str, url_or_cmd: str,
                      server_name: str = "") -> None:
        if transport != "stdio":
            raise NotImplementedError(
                f"MCP transport '{transport}' not supported. "
                f"Only 'stdio' is available.")

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
        logger.info(f"MCP connected: {server_name or url_or_cmd} "
                     f"(PID={self._proc.pid})")

        asyncio.create_task(self._drain_stderr())

        init_result = await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "myagent", "version": "3.0"},
        })
        logger.debug(f"MCP initialize result: {init_result}")

        self._request_id += 1
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._proc.stdin.write((json.dumps(notification) + "\n").encode())
        await self._proc.stdin.drain()

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._send_request("tools/list")
        tools = result.get("tools", [])
        for t in tools:
            t["_mcp_source"] = self._server_name
        return tools

    async def call_tool(self, name: str,
                        arguments: dict[str, Any]) -> dict[str, Any]:
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

    async def _send_request(self, method: str,
                            params: dict[str, Any] | None = None
                            ) -> dict[str, Any]:
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
                self._proc.stdout.readline(), timeout=30.0)
            response = json.loads(response_line.decode())

            if "error" in response:
                err = response["error"]
                raise RuntimeError(
                    f"MCP error [{err.get('code')}]: {err.get('message')}")

            return response.get("result", {})

    async def _drain_stderr(self) -> None:
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
