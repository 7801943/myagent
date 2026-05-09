# Tool 执行器升级 V3 —— JSON-RPC 统一执行路径

## 一、目标架构

```
ToolManager (manager.py)
    │
    ├── MCP 工具 → MCPClient (不变，独立路径)
    │
    └── 本地执行 → JsonRpcProxy ──json-rpc──→ Transport ──stream──→ 子进程 / Docker 容器
                        ↑                              │
                  并发安全：写锁 + id 路由        JsonRpcServer
                  超时隔离：每请求独立 Future           │
                  健康检查：ping                     ExecutionEngine
                                                    ├── execute_cli → bash
                                                    └── execute_function → importlib → fn()

三种部署形态，代码 100% 复用：

形态 1: backend=subprocess
  ToolManager → JsonRpcProxy → SubprocessTransport → 本地子进程 (_pipe_main)
  ↑ 主进程 spawn 子进程，exit 时注册信号/kill 清理

形态 2: backend=docker（优先）
  ToolManager → JsonRpcProxy → TcpTransport → tcp://127.0.0.1:9876 → Docker 容器 (_tcp_main)
  ↑ 主进程只连 TCP，不主动 spawn 容器。容器由用户独立启动

形态 3: backend=subprocess (Docker 连接失败时 fallback)
  Docker TCP 连接失败 → 自动降级 → SubprocessTransport → 子进程
```

---

## 二、核心设计决策

| 决策 | 说明 |
|------|------|
| 全面启用 JSON-RPC 2.0 | 不再保留开关，所有执行路径统一经过 JsonRpcProxy |
| 长连接复用 | 连接在 ToolManager.start() 时建立，ToolManager.stop() 时断开，不每次请求 spawn/kill |
| 并发安全 | 客户端写锁 + id 路由响应 / 服务端每请求独立 asyncio.Task |
| 超时隔离 | 单请求超时不影响其他请求和 server 端其他 task |
| Docker 优先，连接失败降级 | 优先尝试 TCP 连接 Docker 容器 → 不可用则 spawn 子进程 fallback |
| 主进程不主动启动 Docker | Docker 容器由用户独立启动。主进程只负责 TCP 连接，不管理容器生命周期 |
| Subprocess 进程退出挂钩 | fallback 子进程注册 SIGTERM/SIGINT + atexit，主进程退出时自动 kill |
| 独立部署入口 | runner.py 支持 pipe 模式（Transport spawn）和 TCP 模式（Docker/远程部署） |
| 健康检查 | Proxy 启动后发 ping 验证连通性，运行时 EOF 检测断连 |

---

## 三、新增文件

### 3.1 `myagent/tools/engine.py` — 纯执行逻辑 (~100 行)

```python
"""ExecutionEngine: 纯执行逻辑，不包含协议和传输。"""
import asyncio
import importlib.util
import os
import time
from typing import Any

_MAX_CONCURRENT = 10               # 最大并发执行数
_DEFAULT_MAX_OUTPUT_BYTES = 102400  # 默认最大输出字节数 (100KB)


class ExecutionEngine:
    """
    纯执行逻辑：CLI 命令 + 函数工具。

    在任意环境（subprocess/docker/remote）中行为一致。
    对于 CLI 命令使用排他锁串行执行，避免状态冲突；
    对于函数工具使用 asyncio.Semaphore 限制最大并发数。

    输出截断长度（max_output_bytes）为实例属性，支持运行时动态修改。
    """

    def __init__(self, sandbox_backend: str = "subprocess", max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES):
        self._backend = sandbox_backend
        self._tool_cache: dict[str, tuple[Any, float]] = {}  # key → (module, mtime)
        self._max_output_bytes = max_output_bytes  # 运行时可修改

        # 函数工具允许并发，但限制最大并发数
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        # CLI 工具必须完全串行执行
        self._cli_lock = asyncio.Lock()

    def set_max_output_bytes(self, max_bytes: int) -> None:
        """运行时修改输出截断长度。供 JsonRpcServer 远程调用。"""
        self._max_output_bytes = max_bytes

    @property
    def max_output_bytes(self) -> int:
        """当前输出截断长度。"""
        return self._max_output_bytes

    # ── CLI 命令 ──

    async def execute_cli(
        self, command: str, cwd: str | None = None, timeout: float = 120.0
    ) -> dict[str, Any]:
        """
        执行 CLI 命令。

        - subprocess 模式：使用 ulimit 限制 CPU/内存
        - docker 模式：容器已有 cgroup 限制，直接执行
        - cwd 直接传入 create_subprocess_exec，避免 os.chdir() 竞态

        Returns: {"content": str, "is_error": bool, "metadata": dict}
        """
        if not command:
            return {"content": "Empty command", "is_error": True, "metadata": {}}

        if self._backend == "subprocess":
            shell_command = f"ulimit -t 30 && ulimit -v 524288 && {command}"
        else:
            shell_command = command

        async with self._cli_lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "/bin/bash", "-c", shell_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,                  # 避免 os.chdir() 竞态
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )

                stdout = self._truncate_output(stdout_bytes)
                stderr = self._truncate_output(stderr_bytes)

                output = stdout
                if stderr:
                    output += f"\n[stderr]\n{stderr}"

                is_error = proc.returncode != 0
                if is_error:
                    output = f"命令执行失败 (退出码: {proc.returncode}):\n{output}"

                return {
                    "content": output,
                    "is_error": is_error,
                    "metadata": {"exit_code": proc.returncode},
                }

            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return {
                    "content": f"命令执行超时 ({timeout}s): {command[:100]}",
                    "is_error": True,
                    "metadata": {"timed_out": True},
                }
            except Exception as e:
                return {
                    "content": f"命令执行异常: {e}",
                    "is_error": True,
                    "metadata": {"type": type(e).__name__},
                }

    # ── 函数工具 ──

    async def execute_function(
        self,
        file_path: str,
        fn_name: str,
        args: dict[str, Any],
        timeout: float = 120.0,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """
        执行 Python 函数工具。

        通过 importlib 加载模块并调用函数。
        基于文件的 mtime 自动判断是否需要重新加载（热更新）。

        Returns: {"content": str, "is_error": bool, "metadata": dict}
        """
        cache_key = f"{file_path}:{fn_name}"

        async with self._semaphore:
            try:
                # 切换工作目录（单 task 内安全，只在 fn 调用期间生效）
                prev_cwd = os.getcwd() if cwd else None
                if cwd:
                    os.chdir(cwd)

                module = self._load_module(cache_key, file_path)

                fn = getattr(module, fn_name, None)
                if fn is None:
                    return {
                        "content": f"Function not found: {fn_name} in {file_path}",
                        "is_error": True,
                        "metadata": {},
                    }

                result = await asyncio.wait_for(fn(**args), timeout=timeout)

                if hasattr(result, "content") and hasattr(result, "is_error"):
                    return {
                        "content": str(result.content),
                        "is_error": result.is_error,
                        "metadata": getattr(result, "metadata", {}),
                    }
                else:
                    return {"content": str(result), "is_error": False, "metadata": {}}

            except asyncio.TimeoutError:
                return {
                    "content": f"函数执行超时 ({timeout}s): {fn_name}",
                    "is_error": True,
                    "metadata": {"timed_out": True},
                }
            except Exception as e:
                self._tool_cache.pop(cache_key, None)
                return {
                    "content": f"函数执行异常: {type(e).__name__}: {e}",
                    "is_error": True,
                    "metadata": {"type": type(e).__name__},
                }
            finally:
                if prev_cwd:
                    try:
                        os.chdir(prev_cwd)
                    except OSError:
                        pass  # 目录可能已被删除

    # ── 模块缓存（基于 mtime 热更新） ──

    def _load_module(self, cache_key: str, file_path: str):
        """加载模块，通过比对文件 mtime 实现热加载。"""
        try:
            current_mtime = os.path.getmtime(file_path)
        except OSError:
            current_mtime = 0.0

        if cache_key in self._tool_cache:
            module, cached_mtime = self._tool_cache[cache_key]
            if current_mtime == cached_mtime:
                return module

        spec = importlib.util.spec_from_file_location("_tool_module", file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load module: {file_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._tool_cache[cache_key] = (module, current_mtime)
        return module

    # ── 输出截断 ──

    def _truncate_output(self, data: bytes) -> str:
        """截断超长输出。使用 self._max_output_bytes 作为阈值（运行时可修改）。"""
        max_bytes = self._max_output_bytes
        if len(data) > max_bytes:
            truncated = data[:max_bytes]
            text = truncated.decode("utf-8", errors="replace")
            text += f"\n...[输出截断：原始 {len(data)} 字节]"
            return text
        return data.decode("utf-8", errors="replace")
```

---

### 3.2 `myagent/tools/json_rpc.py` — JSON-RPC 协议层 (~140 行)

```python
"""JSON-RPC 2.0 协议层：客户端代理 + 服务端处理器。"""
import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class JsonRpcError(Exception):
    """JSON-RPC 错误。"""
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JsonRpcProxy —— 客户端代理（在 agent 主进程内）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class JsonRpcProxy:
    """
    JSON-RPC 客户端代理。

    职责：
    - 将 execute_cli / execute_function 调用打包为 JSON-RPC 请求
    - 通过 Transport 发送到服务端，接收响应
    - 并发安全：写锁 + 请求 id 路由
    - 超时隔离：单请求超时不影响其他请求
    - 健康检查：启动时 ping

    用法:
        proxy = JsonRpcProxy(transport, default_timeout=120.0)
        await proxy.start()
        result = await proxy.execute_cli("ls -la", cwd="/tmp")
        await proxy.stop()
    """

    def __init__(self, transport, default_timeout: float = 120.0):
        self._transport = transport
        self._default_timeout = default_timeout
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None

    # ── 生命周期 ──

    async def start(self) -> None:
        """启动 transport 并验证连通性。"""
        await self._transport.start()
        self._reader_task = asyncio.create_task(self._read_loop())

        # 健康检查：发送 ping 验证服务端已就绪
        try:
            await self._send("ping", {}, timeout=10.0)
            logger.info("JsonRpcProxy connected and healthy")
        except Exception:
            await self._transport.stop()
            raise RuntimeError("JsonRpcProxy health check failed: server not responding")

    async def stop(self) -> None:
        """关闭 reader、cancel 所有 pending、停止 transport。"""
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

    # ── 执行入口 ──

    async def execute_cli(
        self, command: str, cwd: str | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        """执行 CLI 命令（JSON-RPC → execute_cli）。"""
        return await self._send("execute_cli", {
            "command": command,
            "cwd": cwd or os.getcwd(),
            "timeout": timeout or self._default_timeout,
        }, timeout=timeout or self._default_timeout)

    async def execute_function(
        self, file_path: str, fn_name: str, args: dict,
        timeout: float | None = None, cwd: str | None = None,
    ) -> dict[str, Any]:
        """执行函数工具（JSON-RPC → execute）。"""
        return await self._send("execute", {
            "file_path": file_path,
            "fn_name": fn_name,
            "args": args,
            "timeout": timeout or self._default_timeout,
            "cwd": cwd or os.getcwd(),
        }, timeout=timeout or self._default_timeout)

    # ── 远程配置 ──

    async def set_truncation(self, max_bytes: int) -> None:
        """远程设置服务端输出截断长度（运行时动态修改）。"""
        await self._send("set_truncation", {"max_bytes": max_bytes}, timeout=10.0)

    # ── 内部：请求/响应管理 ──

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _send(self, method: str, params: dict, timeout: float) -> dict[str, Any]:
        """发送单条 JSON-RPC 请求并等待响应。"""
        req_id = self._next_id()
        request = {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        # 写锁防止多协程写入交叠
        async with self._write_lock:
            self._transport.writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
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
        """后台循环：读取 transport 响应，按 id 路由到对应 Future。"""
        try:
            while True:
                line = await self._transport.reader.readline()
                if not line:
                    logger.warning("Transport stream closed (EOF)")
                    self._fail_all_pending(RuntimeError("Transport disconnected"))
                    break

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
                    # stale response（超时后到达的响应，直接丢弃）
                    logger.debug(f"Discarding stale response for id={req_id}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"JsonRpcProxy read loop error: {e}")
            self._fail_all_pending(e)

    def _fail_all_pending(self, exc: Exception) -> None:
        """将所有 pending Future 标记为失败。"""
        for req_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JsonRpcServer —— 服务端处理器（在子进程/容器内）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class JsonRpcServer:
    """
    JSON-RPC 服务端处理器。

    职责：
    - 从 reader 读取 JSON-RPC 请求
    - 每请求创建独立 asyncio.Task（异常/超时不阻塞 server loop）
    - 调用 ExecutionEngine 执行
    - 写锁确保响应不交叠

    用法:
        engine = ExecutionEngine(sandbox_backend=backend)
        server = JsonRpcServer(engine)
        await server.serve(reader, writer)
    """

    def __init__(self, engine):
        self._engine = engine
        self._write_lock = asyncio.Lock()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """服务主循环。读取请求，分派执行，写入响应。"""
        pending_tasks: set[asyncio.Task] = set()

        try:
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
                    async with self._write_lock:
                        writer.write((json.dumps(error_response, ensure_ascii=False) + "\n").encode())
                        await writer.drain()
                    continue

                # 每请求独立 task，一个卡住不影响其他
                task = asyncio.create_task(self._handle_and_respond(request, writer))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)

        finally:
            # 关闭时取消所有未完成的 task
            for task in pending_tasks:
                task.cancel()

    async def _handle_and_respond(self, request: dict, writer: asyncio.StreamWriter) -> None:
        """处理请求并写回响应（带写锁）。"""
        try:
            response = await self._dispatch(request)
        except Exception as e:
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": str(e)},
                "id": request.get("id"),
            }

        async with self._write_lock:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
            await writer.drain()

    async def _dispatch(self, request: dict) -> dict:
        """分发请求到具体处理器。"""
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        if method == "ping":
            return {"jsonrpc": "2.0", "result": "pong", "id": req_id}

        if method == "set_truncation":
            max_bytes = params.get("max_bytes", 102400)
            self._engine.set_max_output_bytes(max_bytes)
            return {"jsonrpc": "2.0", "result": {"max_output_bytes": max_bytes}, "id": req_id}

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
        """executor 函数工具。"""
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
                "error": {"code": -32000, "message": str(e), "data": {"type": type(e).__name__}},
                "id": req_id,
            }

    async def _handle_execute_cli(self, params: dict, req_id: int) -> dict:
        """执行 CLI 命令。"""
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
                "error": {"code": -32000, "message": str(e), "data": {"type": type(e).__name__}},
                "id": req_id,
            }
```

---

### 3.3 `myagent/tools/transport.py` — Transport 后端 (~180 行)

```python
"""Transport 后端抽象：子进程 / TCP（Docker/远程）连接。"""
import asyncio
import atexit
import logging
import os
import signal
import sys
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Transport(ABC):
    """传输层抽象。只负责双向流的建立和生命周期管理。"""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @property
    @abstractmethod
    def reader(self) -> asyncio.StreamReader: ...

    @property
    @abstractmethod
    def writer(self) -> asyncio.StreamWriter: ...

    @staticmethod
    def build_safe_env() -> dict[str, str]:
        """统一的环境变量安全策略（deny-list）。"""
        env = dict(os.environ)
        for key in (
            "API_KEY", "SECRET_KEY", "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
        ):
            env.pop(key, None)
        return env


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SubprocessTransport —— 本地子进程（fallback 模式）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SubprocessTransport(Transport):
    """
    本地 Python 子进程传输层。

    主进程 spawn 子进程，通过 stdin/stdout 管道通信。
    注册信号和 atexit，确保主进程退出时子进程被 kill。
    """

    def __init__(self, *, cwd: str | None = None, env: dict | None = None):
        self._cwd = cwd or os.getcwd()
        self._env = env or Transport.build_safe_env()
        self._env["MYAGENT_SANDBOX_BACKEND"] = "subprocess"
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._drain_task: asyncio.Task | None = None

    @property
    def reader(self) -> asyncio.StreamReader:
        assert self._reader is not None, "Transport not started"
        return self._reader

    @property
    def writer(self) -> asyncio.StreamWriter:
        assert self._writer is not None, "Transport not started"
        return self._writer

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "myagent.tools.runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )
        logger.info(f"SubprocessTransport started: PID={self._proc.pid}")

        # 注册退出清理：主进程退出时 kill 子进程
        self._register_exit_hooks()

        # 从管道构造 StreamReader / StreamWriter
        loop = asyncio.get_running_loop()
        self._reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(self._reader)
        await loop.connect_read_pipe(
            lambda: protocol, self._proc.stdout)

        transport, _ = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, self._proc.stdin)
        self._writer = asyncio.StreamWriter(
            transport, None, self._reader, loop)

        # 后台消费 stderr（防止缓冲区满死锁）
        self._drain_task = asyncio.create_task(self._drain_stderr())

    def _register_exit_hooks(self) -> None:
        """注册退出钩子：主进程退出时 kill 子进程。"""
        def _cleanup():
            if self._proc and self._proc.returncode is None:
                try:
                    self._proc.kill()
                    logger.info(f"SubprocessTransport cleanup: killed PID={self._proc.pid}")
                except Exception:
                    pass

        atexit.register(_cleanup)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _cleanup)
            except NotImplementedError:
                pass  # Windows 不支持

    async def stop(self) -> None:
        if self._drain_task:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass

        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass

        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass

        logger.info("SubprocessTransport stopped")

    async def _drain_stderr(self) -> None:
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.debug(f"[child stderr] {line.decode().rstrip()}")
        except Exception:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TcpTransport —— TCP 连接（Docker / 远程服务器）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TcpTransport(Transport):
    """
    TCP 连接传输层。

    连接到已运行的 JSON-RPC 服务器（Docker 容器或远程机器）。
    不管理服务器生命周期——容器由用户启动，远程由运维管理。
    stop() 只关闭客户端连接。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9876):
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def reader(self) -> asyncio.StreamReader:
        assert self._reader is not None, "Transport not started"
        return self._reader

    @property
    def writer(self) -> asyncio.StreamWriter:
        assert self._writer is not None, "Transport not started"
        return self._writer

    async def start(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )
        logger.info(f"TcpTransport connected: {self._host}:{self._port}")

    async def stop(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        logger.info(f"TcpTransport disconnected: {self._host}:{self._port}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Transport 工厂：Docker 优先 → fallback subprocess
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def try_create_transport(runner_config: dict) -> tuple[Transport, str]:
    """
    按优先级尝试创建 Transport：

    1. docker:  尝试 TCP 连接 Docker 容器 → 成功则返回 TcpTransport
    2. docker 连接失败 → 记录日志 → 降级到 SubprocessTransport

    Returns:
        (transport, backend_name)  — backend_name ∈ {"docker", "subprocess"}
    """
    backend = runner_config.get("backend", "auto")

    if backend in ("auto", "docker"):
        docker_cfg = runner_config.get("docker", {})
        host = docker_cfg.get("host", "127.0.0.1")
        port = docker_cfg.get("port", 9876)
        connect_timeout = docker_cfg.get("connect_timeout", 3.0)

        try:
            tcp_transport = TcpTransport(host=host, port=port)
            await asyncio.wait_for(tcp_transport.start(), timeout=connect_timeout)
            logger.info(f"Docker executor available at {host}:{port}")
            return tcp_transport, "docker"
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
            logger.warning(
                f"Docker executor not available at {host}:{port} ({e}). "
                f"Falling back to subprocess."
            )

    # Fallback: subprocess
    env = Transport.build_safe_env()
    subprocess_transport = SubprocessTransport(
        cwd=os.getcwd(),
        env=env,
    )
    await subprocess_transport.start()
    logger.info("Using subprocess backend (fallback)")
    return subprocess_transport, "subprocess"
```

---

### 3.4 `myagent/tools/runner.py` — 子进程/服务器入口（重写，~75 行）

```python
"""Tool 执行器子进程/服务器入口。

两种模式：
  python -m myagent.tools.runner              → pipe 模式（SubprocessTransport spawn 用）
  python -m myagent.tools.runner --tcp 9876   → TCP 模式（Docker/独立部署用）

─────────────────────────────────────────────────
Docker 独立部署：
─────────────────────────────────────────────────
1. 构建镜像：
     docker build -t myagent-sandbox:latest -f Dockerfile.sandbox .

2. 启动容器（TCP 模式，容器作为服务器常驻）：
     docker run -d --name myagent-executor \
       -p 9876:9876 \
       -v $(pwd):/workspace:rw \
       -w /workspace \
       --network=none \
       --memory=512m --cpus=1 --pids-limit=50 \
       --security-opt=no-new-privileges \
       myagent-sandbox:latest \
       python -m myagent.tools.runner --tcp --port 9876

3. 容器由用户或 daemon 管理生命周期：
     docker logs myagent-executor    # 查看日志
     docker stop myagent-executor    # 停止
     docker rm myagent-executor      # 删除

4. 主进程连接：config.yaml sandbox.backend=docker，指向 127.0.0.1:9876
─────────────────────────────────────────────────
"""
import argparse
import asyncio
import os
import sys

from myagent.tools.engine import ExecutionEngine
from myagent.tools.json_rpc import JsonRpcServer


async def _pipe_main() -> None:
    """Pipe 模式：从 stdin/stdout 读写 JSON-RPC。
    由 SubprocessTransport spawn 时使用。"""
    backend = os.environ.get("MYAGENT_SANDBOX_BACKEND", "subprocess")
    engine = ExecutionEngine(sandbox_backend=backend)
    server = JsonRpcServer(engine)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    transport, _ = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(transport, None, reader, loop)

    await server.serve(reader, writer)


async def _tcp_main(host: str = "0.0.0.0", port: int = 9876) -> None:
    """TCP 模式：监听端口，接收 JSON-RPC 连接。
    Docker 容器或远程服务器独立部署时使用。"""
    backend = os.environ.get("MYAGENT_SANDBOX_BACKEND", "docker")
    engine = ExecutionEngine(sandbox_backend=backend)
    server = JsonRpcServer(engine)

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await server.serve(reader, writer)

    srv = await asyncio.start_server(handle_client, host, port)
    addr = srv.sockets[0].getsockname()
    print(f"JsonRpcServer listening on tcp://{addr[0]}:{addr[1]}", file=sys.stderr)

    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MyAgent Tool Execution Server")
    parser.add_argument("--tcp", action="store_true", help="Run as TCP server (default: pipe mode)")
    parser.add_argument("--host", default="0.0.0.0", help="TCP bind address")
    parser.add_argument("--port", type=int, default=9876, help="TCP port")
    args = parser.parse_args()

    if args.tcp:
        asyncio.run(_tcp_main(args.host, args.port))
    else:
        asyncio.run(_pipe_main())
```

---

## 四、修改文件

### 4.1 `myagent/tools/manager.py` — ToolManager 改动

**初始化变更** — 注入 JsonRpcProxy：

```python
from myagent.tools.json_rpc import JsonRpcProxy
from myagent.tools.transport import try_create_transport

class ToolManager:
    def __init__(self, tools_dir: str = "myagent/tools/tools_store",
                 runner_config: dict | None = None):
        self._tools_dir = Path(tools_dir)
        self._tools: dict[str, _ToolRecord] = {}
        self._mcp_clients: dict[str, MCPClient] = {}
        self._runner_config = runner_config or {}
        # _proxy 延迟创建（在 start() 中确定后端后再建）
        self._proxy: JsonRpcProxy | None = None

        # 热加载状态
        self._watch_task: asyncio.Task | None = None
        self._running = False
        self._tool_states: dict[str, float] = {}
        self._poll_interval = 1.0
        self._on_register: list = []
        self._on_unregister: list = []
```

**路由变更** — execute() 方法：

```python
async def execute(self, name: str, **args: Any) -> ToolResult:
    record = self._tools.get(name)
    if record is None:
        return ToolResult(
            content=f"Tool '{name}' not found. Available: {list(self._tools.keys())}",
            is_error=True,
        )

    timeout = record.meta.timeout

    try:
        # MCP 路径：独立通道，不经过 proxy
        if record.source == "mcp" and record.mcp_client:
            return await self._execute_mcp(record, args)

        # 代理尚未启动（ToolManager.start() 未调用）
        if self._proxy is None:
            return ToolResult(
                content="ToolManager not started: proxy unavailable",
                is_error=True,
            )

        # CLI 命令 → JsonRpcProxy.execute_cli()
        if record.name == "cli_execute":
            return await self._execute_proxy_cli(record, args, timeout)

        # 所有函数工具（含内置 FileReadTool/FileWriteTool）→ JsonRpcProxy.execute_function()
        if record.file_path and record.fn_name:
            return await self._execute_proxy_function(record, args, timeout)

        return ToolResult(
            content=f"Tool '{name}' has no execution path configured",
            is_error=True,
        )
    except Exception as e:
        logger.error(f"Tool execution error ({name}): {e}")
        return ToolResult(content=f"Tool execution error: {e}", is_error=True)

async def _execute_proxy_cli(self, record, args, timeout) -> ToolResult:
    """通过 JsonRpcProxy 执行 CLI 命令。"""
    if self._proxy is None:
        return ToolResult(content="ToolManager not started (no proxy)", is_error=True)
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

async def _execute_proxy_function(self, record, args, timeout) -> ToolResult:
    """通过 JsonRpcProxy 执行函数工具。"""
    if self._proxy is None:
        return ToolResult(content="ToolManager not started (no proxy)", is_error=True)
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
    )
```

**生命周期变更** — proxy 在 start() 中创建（Docker 优先尝试 → fallback）：

```python
async def start(self) -> None:
    if self._running:
        return

    # 创建 Transport：Docker TCP 优先 → 不可用则 fallback subprocess
    transport, backend_name = await try_create_transport(self._runner_config)
    timeout_default = self._runner_config.get("timeout_default", 120.0)
    self._proxy = JsonRpcProxy(transport, default_timeout=timeout_default)

    # 启动长连接 Proxy（含 ping 健康检查）
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

    # 关闭长连接 Proxy
    if self._proxy:
        await self._proxy.stop()
        self._proxy = None

    logger.info("ToolManager stopped")
```

**CLI 工具注册** — 不再通过 CLITool 类：

```python
def _register_builtin_tools(self) -> None:
    """注册内置工具。"""
    # CLI 执行（虚拟工具，路由到 proxy）
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
        fn=None,  # fn=None → execute() 中路由到 proxy
    )
    self._tools["cli_execute"] = cli_record

    # 文件工具保持不变（in-process）
    from myagent.tools.builtin.file_tools import FileReadTool, FileWriteTool
    self.register(FileReadTool())
    self.register(FileWriteTool())
```

---

### 4.2 `myagent/factory.py` — 框架集成

**删除** `_build_sandbox()` 方法。

**变更** `_build_tool_manager()` 和 `create_agent()`：

```python
# 前:
def create_agent(self, ...):
    ...
    sandbox = self._build_sandbox()          # 删除
    ...
    tool_manager = self._build_tool_manager(sandbox)  # 删除 sandbox 参数

# 后:
def create_agent(self, ...):
    ...
    secret_manager = self._build_secret_manager()
    tool_manager = self._build_tool_manager()       # 无参数
    ...

def _build_tool_manager(self) -> ToolManager:
    hr_cfg = self._config_obj.hot_reload
    tools_dir = hr_cfg.watch_dir if hr_cfg and hr_cfg.enabled else "myagent/tools/tools_store"

    # 从配置加载 runner 参数
    runner_cfg = self._app_config.get("sandbox", {})  # 复用 sandbox 配置段

    manager = ToolManager(tools_dir=tools_dir, runner_config=runner_cfg)
    manager._register_builtin_tools()
    logger.info("Registered builtin tools: cli_execute, file_read, file_write")
    return manager
```

**清理 import**：

```python
# 删除:
from myagent.runtime.sandbox import SubprocessSandbox
from myagent.runtime.sandbox.subprocess_sandbox import ResourceLimits
from myagent.tools.builtin.cli_tool import CLITool
```

---

### 4.3 `config.yaml` — 配置升级

**前**（sandbox 段）：

```yaml
sandbox:
  backend: "subprocess"
  max_cpu_seconds: 30
  max_memory_mb: 512
  max_output_bytes: 102400
  timeout_seconds: 60.0
```

**后**：

```yaml
sandbox:
  backend: auto                              # auto | docker | subprocess
                                             # auto: 尝试 docker TCP → 失败则 fallback subprocess
  timeout_default: 120.0                     # 工具执行默认超时（秒）

  subprocess:                                # fallback 子进程模式参数
    max_cpu_seconds: 30
    max_memory_mb: 512
    max_output_bytes: 102400

  docker:                                    # Docker TCP 连接参数（主进程不 spawn 容器）
    host: "127.0.0.1"
    port: 9876
    connect_timeout: 3.0                     # 连接超时（秒），超时则降级到 subprocess
    # 以下为 Docker 容器启动参考（主进程不使用，仅供独立部署时参考）：
    run_reference:
      image: "myagent-sandbox:latest"
      network: "bridge"                       # none | bridge | host（none=完全断网，bridge=允许出站）
      memory: "512m"
      cpus: "1"
      pids_limit: 50
      security_opt: "no-new-privileges"
```

---

### 4.4 `myagent/tools/__init__.py` — 更新导出

```python
from myagent.tools.manager import ToolManager
from myagent.tools.api import ToolResult, ToolMeta, ToolLike, tool, generate_schema
from myagent.tools.engine import ExecutionEngine
from myagent.tools.json_rpc import JsonRpcProxy, JsonRpcServer
from myagent.tools.transport import Transport, SubprocessTransport, TcpTransport, try_create_transport

__all__ = [
    "ToolManager", "ToolResult", "ToolMeta", "ToolLike", "tool", "generate_schema",
    "ExecutionEngine", "JsonRpcProxy", "JsonRpcServer",
    "Transport", "SubprocessTransport", "TcpTransport", "try_create_transport",
]
```

---

### 4.5 `myagent/tools/builtin/__init__.py` — 更新导出

```python
from myagent.tools.builtin.file_tools import FileReadTool, FileWriteTool

__all__ = ["FileReadTool", "FileWriteTool"]
```

---

## 五、删除文件

| 文件 | 原因 |
|------|------|
| `myagent/tools/builtin/cli_tool.py` | CLI 执行移入 ExecutionEngine，注册逻辑移入 ToolManager._register_builtin_tools() |
| `myagent/runtime/sandbox/__init__.py` | 沙盒概念废弃 |
| `myagent/runtime/sandbox/base.py` | 被 ExecutionEngine + Transport 替代 |
| `myagent/runtime/sandbox/subprocess_sandbox.py` | 被 ExecutionEngine + SubprocessTransport 替代 |
| `myagent/runtime/sandbox/docker_sandbox.py` | 被 TcpTransport + 独立 Docker 部署替代 |

---

## 六、新增文件

| 文件 | 说明 |
|------|------|
| `Dockerfile.sandbox` | Docker 沙盒镜像 |
| `myagent/tools/mcp_client.py` | MCPClient 从 runner.py 抽离为独立文件 |

### 6.1 `Dockerfile.sandbox`

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash coreutils curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY pyproject.toml .
RUN pip install --no-cache-dir -e . 2>/dev/null || true

# 默认 TCP 模式（作为独立服务器运行）
CMD ["python", "-m", "myagent.tools.runner", "--tcp", "--port", "9876"]
```

构建：
```bash
docker build -t myagent-sandbox:latest -f Dockerfile.sandbox .
```

启动（容器作为独立服务器，主进程不管理其生命周期）：
```bash
docker run -d --name myagent-executor \
  -p 9876:9876 \
  -v $(pwd):/workspace:rw \
  -w /workspace \
  --memory=512m --cpus=1 --pids-limit=50 \
  --security-opt=no-new-privileges \
  myagent-sandbox:latest
# 默认 --network=bridge（允许出站访问 API，weather/search 等工具可用）
# 强隔离场景用 --network=none（完全断网）
```
```

容器由用户手动管理：
```bash
docker logs myagent-executor     # 查看日志
docker stop myagent-executor     # 停止
docker rm myagent-executor       # 删除
```

---

## 七、超时与异常处理矩阵

| 场景 | 客户端 (JsonRpcProxy) | 服务端 (JsonRpcServer) | 结果 |
|------|----------------------|----------------------|------|
| 正常执行 | `await future` → 收到响应 | `await engine.execute_xxx(timeout)` → 返回 | 正常 |
| 客户端超时 | `wait_for(future, timeout)` → 抛弃该 Future | 该 task 继续运行（有自己的 timeout） | 客户收到 TimeoutError，服务端 task 最终超时或完成（响应被丢弃） |
| 函数异常 | 收到 error response → `future.set_exception` | `except Exception` → 构造 error response | 客户收到 JsonRpcError |
| 单个请求超时 | 只 pop 该 Future，连接保持 | 对应 task 被 `wait_for` 取消，不影响其他 task | 其他请求继续正常 |
| 连接断开 (EOF) | `_read_loop` 检测 → `_fail_all_pending` | `serve()` 的 while 循环退出 | 所有 pending 收到异常 |
| Docker TCP 不通 | — | — | `try_create_transport` 捕获 ConnectionRefused → 降级 SubprocessTransport，记录 WARNING 日志 |
| 并发写入冲突 | `asyncio.Lock` 序列化 | `asyncio.Lock` 序列化 | 无 |
| 主进程 SIGTERM | — | — | SubprocessTransport 的 atexit/信号处理器 kill 子进程；TcpTransport 只关闭连接 |

---

## 八、Docker 后端连接 & 降级流程

```
ToolManager.start()
    │
    ├── 1. try_create_transport(config)
    │   │
    │   ├── backend=auto 或 docker?
    │   │   ├── 尝试: await TcpTransport(host, port).start()
    │   │   │   ├── 连接成功 → 返回 (TcpTransport, "docker") ✅
    │   │   │   └── ConnectionRefused / Timeout → logger.warning(...)
    │   │   └── 捕获异常后继续 ↓
    │   │
    │   └── fallback: SubprocessTransport.start()
    │       ├── spawn: python -m myagent.tools.runner
    │       ├── 注册 atexit + SIGTERM/SIGINT → kill 子进程
    │       └── 返回 (SubprocessTransport, "subprocess") ⚠️
    │
    └── 2. JsonRpcProxy(transport).start()
        ├── 启动 reader task
        └── 发 ping → 10s 超时 → fail-fast
```

| 层级 | 时机 | 机制 |
|------|------|------|
| Docker TCP 连接尝试 | start() 时 | `asyncio.open_connection(host, port)` + `asyncio.wait_for` |
| 连接失败处理 | Docker 不通 | 捕获异常 → 记录 WARNING 日志 → 降级 subprocess |
| 启动后验证 | Transport 建立后 | Proxy 发 ping，10s 超时 → 异常 |
| 运行时监控 | 长连接持续期间 | `_read_loop` 读到 EOF → `_fail_all_pending` |
| 主进程退出 | SIGTERM / 正常退出 | Subprocess: atexit/signal → kill 子进程。Tcp: 只关闭连接 |

---

## 九、改动量估算

| 文件 | 状态 | 行数 |
|------|------|------|
| `tools/engine.py` | 新增 | ~160 |
| `tools/json_rpc.py` | 新增 | ~230 |
| `tools/transport.py` | 新增 | ~260 |
| `tools/runner.py` | 重写 | 473 → ~75 |
| `tools/mcp_client.py` | 抽离 | ~170（从 runner.py 移出） |
| `tools/manager.py` | 修改 | +55 / -25 |
| `factory.py` | 修改 | +10 / -40 |
| `tools/__init__.py` | 修改 | +5 |
| `tools/builtin/__init__.py` | 修改 | +2 |
| `config.yaml` | 修改 | +10 |
| `Dockerfile.sandbox` | 新增 | ~20 |
| **新代码** | | **~1245 行** |
| `tools/builtin/cli_tool.py` | 删除 | -79 |
| `runtime/sandbox/__init__.py` | 删除 | -5 |
| `runtime/sandbox/base.py` | 删除 | -53 |
| `runtime/sandbox/subprocess_sandbox.py` | 删除 | -141 |
| `runtime/sandbox/docker_sandbox.py` | 删除 | -24 |
| **删除** | | **~302 行** |
| `runner.py` 重写净减少 | | -398 |
| **总计净变化** | | **+~545 行** |

---

## 十、与当前架构的关键差异

| | 当前 (V2) | V3 |
|---|-----------|-----|
| 执行路径数 | 3 条（MCP / CLITool→Sandbox / SubprocessRunner） | 2 条（MCP / JsonRpcProxy） |
| 子进程生命周期 | 每次请求 spawn/kill | 长连接复用 |
| Docker 模式 | 骨架，`docker run` spawn（未实现） | TCP 连接已有容器（主进程不 spawn） |
| Docker 降级 | 无 | Docker 不通 → 自动降级 subprocess + 日志 |
| Subprocess 退出 | 无挂钩 | atexit + SIGTERM/SIGINT → kill 子进程 |
| CLI 隔离 | SubprocessSandbox（ulimit，无目录隔离） | 子进程（ulimit）/ Docker（cgroup + fs 隔离） |
| 环境变量策略 | 3 种不一致实现 | 1 个 `build_safe_env()` |
| drain_stderr | 2 个拷贝 | SubprocessTransport 内 1 个 |
| 并发请求 | 不支持（单请求独占进程） | 支持（写锁 + id 路由，服务端 Semaphore 上限 10） |
| 模块缓存 | 无（每次 spawn 新进程天然刷新） | 带 TTL（3600s），超时自动重新 importlib |
| 服务端独立部署 | 不支持 | `--tcp` 入口 + Docker daemon 模式 |
| Docker 模式 | 骨架，`docker run` spawn（未实现） | TCP 连接已有容器（主进程不启动/不停容器） |
| Docker 网络 | 未实现 | 可选 `bridge`（允许出站）或 `none`（强隔离） |
| 配置驱动 | 硬编码 | `config.yaml` sandbox 段 |
