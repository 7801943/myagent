"""
ProcessToolRunner：基于 stdin/stdout JSON-RPC 的统一进程隔离执行器。

V2 设计：所有工具统一走子进程 JSON-RPC，一条路径，零 if/else 分支。

双角色模块：
  1. 主进程端：ProcessToolRunner 类 — 进程池管理、请求发送、响应接收
  2. 子进程端：__main__ 入口 — 从 stdin 读取请求、动态导入工具、执行、返回结果

特性：
  - 所有工具都在独立子进程中执行，主进程永不崩溃
  - 常驻进程池：复用子进程减少启动开销（首次 ~200ms，后续 ~2ms）
  - 继承主进程虚拟环境和工作目录（cwd/env）
  - JSON-RPC 2.0 over stdio 通信
  - 空闲超时自动回收 + 异常进程自动重建

JSON-RPC 标准错误码：
  -32700  JSON 解析错误
  -32600  无效请求
  -32601  方法不存在
  -32602  无效参数
  -32000  工具执行失败
  -32001  执行超时
  -32002  工具未找到
"""
import asyncio
import importlib
import json
import os
import sys
from typing import Any

from myagent.tools.base import BaseTool
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主进程端：ProcessToolRunner（V2 常驻进程池）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ProcessToolRunner:
    """
    基于 stdin/stdout JSON-RPC 的统一进程隔离执行器。

    V2 核心变更：
      - 常驻进程池（Pool 模式），不再每次执行启动新进程
      - 继承主进程 cwd 和虚拟环境
      - 空闲进程超时自动回收

    用法：
        runner = ProcessToolRunner(pool_size=3)
        result = await runner.run_tool(
            tool_entry="myagent.tools.builtin.cli_tool:CLITool",
            arguments={"command": "ls -la"},
            timeout=60.0,
        )
        await runner.shutdown()  # 清理所有常驻进程
    """

    def __init__(
        self,
        pool_size: int = 3,
        idle_timeout: float = 300.0,
    ):
        """
        Args:
            pool_size: 常驻进程池大小（最多同时保持的空闲进程数）
            idle_timeout: 空闲进程超时回收时间（秒），默认 5 分钟
        """
        self._pool_size = pool_size
        self._idle_timeout = idle_timeout
        self._pool: list[asyncio.subprocess.Process] = []
        self._all_processes: set[asyncio.subprocess.Process] = set()
        self._lock = asyncio.Lock()
        self._request_id = 0
        self._shutdown = False
        self._child_env: dict[str, str] | None = None  # lazy init

    async def run_tool(
        self,
        tool_entry: str,  # "module.path:ClassName"
        arguments: dict,
        timeout: float = 60.0,
        meta: dict | None = None,
        cwd: str | None = None,
    ) -> dict:
        """
        在子进程中执行工具，返回结果字典。

        Args:
            tool_entry: 工具入口，格式 "module.path:ClassName"
            arguments: 工具参数
            timeout: 执行超时（秒）
            meta: 工具元数据
            cwd: 工作目录（None 则继承主进程 cwd）

        Returns:
            {"content": str, "is_error": bool, "metadata": dict}
        """
        proc = await self._acquire()
        try:
            # 构造 JSON-RPC 请求
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "method": "execute",
                "params": {
                    "tool_entry": tool_entry,
                    "arguments": arguments,
                    "timeout": timeout,
                    "meta": meta or {},
                    "cwd": cwd or os.getcwd(),
                },
                "id": self._request_id,
            }

            # 发送请求
            request_line = json.dumps(request, ensure_ascii=False) + "\n"
            proc.stdin.write(request_line.encode())
            await proc.stdin.drain()

            # 读取响应（带超时，多给 5 秒余量）
            response_line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=timeout + 5.0
            )
            response = json.loads(response_line.decode())

            # 检查进程是否仍然存活
            if proc.returncode is not None:
                # 进程已退出，不归还到池中
                self._all_processes.discard(proc)
                return {
                    "content": f"[ProcessRunner] 子进程意外退出 (code={proc.returncode})",
                    "is_error": True,
                    "metadata": {"type": "ProcessExit"},
                }

            # 检查 JSON-RPC 错误
            if "error" in response:
                error = response["error"]
                # 如果是严重错误（导入失败等），不归还进程
                if error.get("code", 0) in (-32002,):
                    await self._destroy_process(proc)
                else:
                    await self._release(proc)
                return {
                    "content": f"[ProcessRunner Error {error['code']}] {error['message']}",
                    "is_error": True,
                    "metadata": error.get("data", {}),
                }

            # 成功 → 归还进程到池
            await self._release(proc)
            return response["result"]

        except asyncio.TimeoutError:
            # 超时 → 销毁进程（可能卡死）
            await self._destroy_process(proc)
            return {
                "content": f"[ProcessRunner] 执行超时 ({timeout}s)",
                "is_error": True,
                "metadata": {"type": "TimeoutError"},
            }
        except json.JSONDecodeError as e:
            # 响应损坏 → 销毁进程
            await self._destroy_process(proc)
            return {
                "content": f"[ProcessRunner] 响应解析失败: {e}",
                "is_error": True,
                "metadata": {"type": "JSONDecodeError"},
            }
        except Exception as e:
            # 其他异常 → 销毁进程
            await self._destroy_process(proc)
            return {
                "content": f"[ProcessRunner] 执行失败: {e}",
                "is_error": True,
                "metadata": {"type": type(e).__name__},
            }

    # ── 进程池管理 ──

    async def _acquire(self) -> asyncio.subprocess.Process:
        """从池中获取一个可用进程，池空则创建新进程。"""
        async with self._lock:
            # 尝试从池中取一个存活进程
            while self._pool:
                proc = self._pool.pop(0)
                if proc.returncode is None:
                    logger.debug("从进程池取出空闲进程")
                    return proc
                else:
                    # 已死亡，清理
                    self._all_processes.discard(proc)

            # 池空，创建新进程
            proc = await self._create_process()
            self._all_processes.add(proc)
            return proc

    async def _release(self, proc: asyncio.subprocess.Process) -> None:
        """将进程归还到池中。"""
        if self._shutdown or proc.returncode is not None:
            await self._destroy_process(proc)
            return
        async with self._lock:
            if len(self._pool) < self._pool_size:
                self._pool.append(proc)
                logger.debug(f"进程归还到池 (池大小: {len(self._pool)})")
            else:
                # 池满，销毁
                await self._destroy_process(proc)

    async def _create_process(self) -> asyncio.subprocess.Process:
        """启动子进程，继承主进程的虚拟环境和工作目录。"""
        if self._child_env is None:
            self._child_env = self._build_child_env()

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "myagent.tools.process_runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
            env=self._child_env,
        )
        logger.debug(f"创建子进程 PID={proc.pid}")
        return proc

    async def _destroy_process(self, proc: asyncio.subprocess.Process) -> None:
        """销毁一个子进程。"""
        self._all_processes.discard(proc)
        # 从池中也移除（如果在池中的话）
        if proc in self._pool:
            self._pool.remove(proc)
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _build_child_env(self) -> dict[str, str]:
        """构建子进程环境变量：完整继承主进程环境，移除敏感变量。"""
        env = dict(os.environ)
        # 移除可能存在的敏感变量
        for key in ["API_KEY", "SECRET_KEY", "AWS_SECRET_ACCESS_KEY",
                     "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]:
            env.pop(key, None)
        return env

    async def shutdown(self) -> None:
        """关闭所有常驻进程。"""
        self._shutdown = True
        async with self._lock:
            for proc in self._pool:
                await self._destroy_process(proc)
            self._pool.clear()
            for proc in list(self._all_processes):
                await self._destroy_process(proc)
            self._all_processes.clear()
        logger.info(f"ProcessToolRunner 已关闭所有子进程")

    @property
    def pool_size(self) -> int:
        """当前池中空闲进程数。"""
        return len(self._pool)

    @property
    def total_processes(self) -> int:
        """总管理的进程数（空闲 + 执行中）。"""
        return len(self._all_processes)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 子进程端：JSON-RPC 请求处理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# 工具实例缓存（常驻进程内复用，避免重复导入）
_tool_cache: dict[str, Any] = {}


async def _handle_request(request: dict) -> dict:
    """
    处理单条 JSON-RPC 请求（在子进程中执行）。

    Args:
        request: JSON-RPC 2.0 请求字典

    Returns:
        JSON-RPC 2.0 响应字典
    """
    req_id = request.get("id")
    params = request.get("params", {})
    method = request.get("method")

    # ── 健康检查 ──
    if method == "ping":
        return {"jsonrpc": "2.0", "result": "pong", "id": req_id}

    # ── 方法检查 ──
    if method != "execute":
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": f"Method not found: {method}"},
            "id": req_id,
        }

    try:
        # ── 应用 cwd ──
        request_cwd = params.get("cwd")
        if request_cwd:
            os.chdir(request_cwd)

        # ── 动态导入工具（带缓存） ──
        tool_entry = params.get("tool_entry", "")
        if ":" not in tool_entry:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32602, "message": f"Invalid tool_entry format: {tool_entry}"},
                "id": req_id,
            }

        # 从缓存获取或导入工具类
        if tool_entry not in _tool_cache:
            module_path, class_name = tool_entry.rsplit(":", 1)
            module = importlib.import_module(module_path)
            tool_cls = getattr(module, class_name, None)
            if tool_cls is None:
                return {
                    "jsonrpc": "2.0",
                    "error": {"code": -32002, "message": f"Tool not found: {tool_entry}"},
                    "id": req_id,
                }
            _tool_cache[tool_entry] = tool_cls

        tool_cls = _tool_cache[tool_entry]

        # 实例化并执行
        if isinstance(tool_cls, type):
            # 类 → 实例化
            tool = tool_cls()
        elif isinstance(tool_cls, BaseTool):
            # 已是 BaseTool 实例（罕见但安全）
            tool = tool_cls
        else:
            # 普通函数/协程 → 需要用 FunctionTool 包装
            from myagent.tools.base import FunctionTool
            tool = FunctionTool(tool_cls)

        result = await asyncio.wait_for(
            tool.execute(**params.get("arguments", {})),
            timeout=params.get("timeout", 60.0),
        )

        return {
            "jsonrpc": "2.0",
            "result": {
                "content": result.content,
                "is_error": result.is_error,
                "metadata": result.metadata,
            },
            "id": req_id,
        }

    except asyncio.TimeoutError:
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32001, "message": "Execution timeout"},
            "id": req_id,
        }
    except ImportError as e:
        # 导入失败，从缓存中移除
        _tool_cache.pop(tool_entry, None)
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32002, "message": f"Import failed: {e}"},
            "id": req_id,
        }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "error": {
                "code": -32000,
                "message": str(e),
                "data": {"type": type(e).__name__},
            },
            "id": req_id,
        }


async def _child_main() -> None:
    """
    子进程主循环：从 stdin 逐行读取 JSON-RPC 请求，处理后写回 stdout。

    通信协议：每行一条 JSON-RPC 2.0 消息，以换行符分隔。
    stdin 关闭（EOF）时退出循环。
    """
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_event_loop())

    while True:
        line = await reader.readline()
        if not line:
            break  # EOF

        try:
            request = json.loads(line.decode())
        except json.JSONDecodeError as e:
            error_response = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": f"Parse error: {e}"},
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