"""
JSON-RPC 2.0 协议层：客户端代理 + 服务端处理器。
"""
import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


class JsonRpcProxy:
    """
    JSON-RPC 客户端代理（在 agent 主进程内）。

    - 将 execute_cli / execute_function 调用打包为 JSON-RPC 请求
    - 通过 Transport 发送到服务端，接收响应
    - 并发安全：写锁 + 请求 id 路由
    - 超时隔离：单请求超时不影响其他请求
    - 健康检查：启动时 ping
    """

    def __init__(self, transport, default_timeout: float = 120.0):
        self._transport = transport
        self._default_timeout = default_timeout
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self._transport.start()
        self._reader_task = asyncio.create_task(self._read_loop())

        try:
            await self._send("ping", {}, timeout=10.0)
            logger.info("JsonRpcProxy connected and healthy")
        except Exception:
            await self._transport.stop()
            raise RuntimeError("JsonRpcProxy health check failed: server not responding")

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        self._fail_all_pending(RuntimeError("JsonRpcProxy stopped"))
        await self._transport.stop()
        logger.info("JsonRpcProxy stopped")

    async def execute_cli(
        self, command: str, cwd: str | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        return await self._send("execute_cli", {
            "command": command,
            "cwd": cwd or os.getcwd(),
            "timeout": timeout or self._default_timeout,
        }, timeout=timeout or self._default_timeout)

    async def execute_function(
        self, file_path: str, fn_name: str, args: dict,
        timeout: float | None = None, cwd: str | None = None,
    ) -> dict[str, Any]:
        return await self._send("execute", {
            "file_path": file_path,
            "fn_name": fn_name,
            "args": args,
            "timeout": timeout or self._default_timeout,
            "cwd": cwd or os.getcwd(),
        }, timeout=timeout or self._default_timeout)

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _send(self, method: str, params: dict, timeout: float) -> dict[str, Any]:
        req_id = self._next_id()
        request = {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        async with self._write_lock:
            self._transport.writer.write(
                (json.dumps(request, ensure_ascii=False) + "\n").encode())
            await self._transport.writer.drain()

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise JsonRpcError(-32001, f"Request timeout after {timeout}s")
        except Exception:
            self._pending.pop(req_id, None)
            raise

    async def _read_loop(self) -> None:
        consecutive_errors = 0
        max_consecutive_errors = 5

        try:
            while True:
                try:
                    line = await self._transport.reader.readline()
                except ValueError as e:
                    # readline() 缓冲区溢出（单行 JSON 超过 limit）
                    # 跳过此条损坏的响应，尝试继续读取后续数据
                    consecutive_errors += 1
                    logger.error(
                        f"JsonRpcProxy readline buffer overflow ({consecutive_errors}/{max_consecutive_errors}): {e}"
                    )
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error("Too many consecutive read errors, failing all pending requests")
                        self._fail_all_pending(e)
                        break
                    # 尝试排空残留数据直到下一个换行符
                    try:
                        await self._transport.reader.readuntil(b"\n")
                    except Exception:
                        pass
                    continue

                if not line:
                    logger.warning("Transport stream closed (EOF)")
                    self._fail_all_pending(RuntimeError("Transport disconnected"))
                    break

                consecutive_errors = 0  # 重置错误计数

                try:
                    response = json.loads(line.decode())
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON response: {line[:200]}")
                    continue

                req_id = response.get("id")
                if req_id is not None and req_id in self._pending:
                    fut = self._pending.pop(req_id)
                    if not fut.done():
                        if "error" in response:
                            err = response["error"]
                            fut.set_exception(JsonRpcError(
                                err.get("code", -32603),
                                err.get("message", "Unknown error"),
                                err.get("data"),
                            ))
                        else:
                            fut.set_result(response.get("result", {}))
                else:
                    logger.debug(f"Discarding stale response for id={req_id}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"JsonRpcProxy read loop error: {e}")
            self._fail_all_pending(e)

    def _fail_all_pending(self, exc: Exception) -> None:
        for req_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()


class JsonRpcServer:
    """
    JSON-RPC 服务端处理器（在子进程/容器内）。

    - 从 reader 读取 JSON-RPC 请求
    - 每请求创建独立 asyncio.Task
    - 调用 ExecutionEngine 执行
    - 写锁确保响应不交叠
    """

    def __init__(self, engine):
        self._engine = engine
        self._write_lock = asyncio.Lock()

    async def serve(self, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter) -> None:
        pending_tasks: set[asyncio.Task] = set()

        try:
            while True:
                if reader.at_eof():
                    break
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
                    async with self._write_lock:
                        writer.write(
                            (json.dumps(error_response, ensure_ascii=False) + "\n").encode())
                        await writer.drain()
                    continue

                task = asyncio.create_task(
                    self._handle_and_respond(request, writer))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)

        finally:
            for task in pending_tasks:
                task.cancel()

    async def _handle_and_respond(self, request: dict,
                                  writer: asyncio.StreamWriter) -> None:
        try:
            response = await self._dispatch(request)
        except Exception as e:
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": str(e)},
                "id": request.get("id"),
            }

        async with self._write_lock:
            writer.write(
                (json.dumps(response, ensure_ascii=False) + "\n").encode())
            await writer.drain()

    async def _dispatch(self, request: dict) -> dict:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        if method == "ping":
            return {"jsonrpc": "2.0", "result": "pong", "id": req_id}

        if method == "execute":
            return await self._handle_execute(params, req_id)

        if method == "execute_cli":
            return await self._handle_execute_cli(params, req_id)

        return {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": f"Method not found: {method}"},
            "id": req_id,
        }

    async def _handle_execute(self, params: dict, req_id: int) -> dict:
        try:
            result = await self._engine.execute_function(
                file_path=params.get("file_path", ""),
                fn_name=params.get("fn_name", ""),
                args=params.get("args", {}),
                timeout=params.get("timeout", 120.0),
                cwd=params.get("cwd"),
            )
            return {"jsonrpc": "2.0", "result": result, "id": req_id}
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": str(e),
                          "data": {"type": type(e).__name__}},
                "id": req_id,
            }

    async def _handle_execute_cli(self, params: dict, req_id: int) -> dict:
        try:
            result = await self._engine.execute_cli(
                command=params.get("command", ""),
                cwd=params.get("cwd"),
                timeout=params.get("timeout", 120.0),
            )
            return {"jsonrpc": "2.0", "result": result, "id": req_id}
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": str(e),
                          "data": {"type": type(e).__name__}},
                "id": req_id,
            }
