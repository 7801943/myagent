"""
ExecutionEngine: 纯执行逻辑，不包含协议和传输。
在任意环境（subprocess/docker/remote）中行为一致。
"""
import asyncio
import importlib.util
import os
from typing import Any

_MAX_CONCURRENT = 10
_DEFAULT_MAX_OUTPUT_BYTES = 512000


class ExecutionEngine:
    """
    纯执行逻辑：CLI 命令 + 函数工具。

    对于 CLI 命令使用排他锁串行执行，避免状态冲突；
    对于函数工具使用 asyncio.Semaphore 限制最大并发数。
    """

    def __init__(self, sandbox_backend: str = "subprocess",
                 max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES):
        self._backend = sandbox_backend
        self._max_output_bytes = max_output_bytes
        self._tool_cache: dict[str, tuple[Any, float]] = {}
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        self._cli_lock = asyncio.Lock()

    async def execute_cli(
        self, command: str, cwd: str | None = None, timeout: float = 120.0
    ) -> dict[str, Any]:
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
                    cwd=cwd,
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

    async def execute_function(
        self,
        file_path: str,
        fn_name: str,
        args: dict[str, Any],
        timeout: float = 120.0,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        cache_key = f"{file_path}:{fn_name}"

        async with self._semaphore:
            prev_cwd = None
            try:
                if cwd:
                    prev_cwd = os.getcwd()
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
                        pass

    def _load_module(self, cache_key: str, file_path: str):
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

    def _truncate_output(self, data: bytes) -> str:
        max_bytes = self._max_output_bytes
        if len(data) > max_bytes:
            truncated = data[:max_bytes]
            text = truncated.decode("utf-8", errors="replace")
            text += f"\n...[输出截断：原始 {len(data)} 字节]"
            return text
        return data.decode("utf-8", errors="replace")
