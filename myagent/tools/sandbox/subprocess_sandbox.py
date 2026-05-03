"""
SubprocessSandbox：基于 subprocess + ulimit 的轻量级沙盒。
最小化系统依赖，支持 CPU/内存/输出大小限制。
"""
import asyncio
import os
import time
from dataclasses import dataclass

from myagent.tools.sandbox.base import BaseSandbox, SandboxResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ResourceLimits:
    """沙盒资源限制配置。"""
    max_cpu_seconds: int = 30
    max_memory_mb: int = 512
    max_output_bytes: int = 102400   # 100KB
    timeout_seconds: float = 60.0


class SubprocessSandbox(BaseSandbox):
    """
    基于 subprocess + ulimit 的沙盒实现。

    安全措施：
    1. ulimit 限制 CPU 时间和虚拟内存
    2. asyncio.wait_for 控制总超时
    3. 输出截断（防止 /dev/urandom 等攻击）
    4. 不继承父进程的环境变量（可选）
    """

    def __init__(self, limits: ResourceLimits | None = None):
        self._limits = limits or ResourceLimits()

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> SandboxResult:
        """在受限子进程中执行命令。"""
        timeout = timeout or self._limits.timeout_seconds
        start_time = time.monotonic()

        # 构建 ulimit 前缀命令
        # macOS (darwin) 不支持 ulimit -v，会报 Invalid argument 错误
        import sys
        if sys.platform == "darwin":
            ulimit_prefix = f"ulimit -t {self._limits.max_cpu_seconds} && "
        else:
            ulimit_prefix = (
                f"ulimit -t {self._limits.max_cpu_seconds} && "
                f"ulimit -v {self._limits.max_memory_mb * 1024} && "
            )

        # 构建环境变量
        proc_env = self._build_env(env)

        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/bash", "-c", f"{ulimit_prefix}{command}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=proc_env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # 超时 - 杀死进程
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
                elapsed = int((time.monotonic() - start_time) * 1000)
                logger.warning(f"Sandbox command timed out after {timeout}s: {command[:100]}")
                return SandboxResult(
                    stdout="",
                    stderr="",
                    exit_code=-1,
                    timed_out=True,
                    execution_time_ms=elapsed,
                )

            elapsed = int((time.monotonic() - start_time) * 1000)

            # 截断输出
            stdout = self._truncate_output(stdout_bytes)
            stderr = self._truncate_output(stderr_bytes)

            return SandboxResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode if process.returncode is not None else -1,
                execution_time_ms=elapsed,
            )

        except Exception as e:
            elapsed = int((time.monotonic() - start_time) * 1000)
            logger.error(f"Sandbox execution error: {e}")
            return SandboxResult(
                stdout="",
                stderr=f"Sandbox error: {type(e).__name__}: {e}",
                exit_code=-1,
                execution_time_ms=elapsed,
            )

    def _build_env(self, extra_env: dict[str, str] | None) -> dict[str, str]:
        """构建子进程环境变量（继承必要的 PATH 等，隔离敏感变量）。"""
        safe_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TERM": "xterm",
        }
        if extra_env:
            safe_env.update(extra_env)
        return safe_env

    def _truncate_output(self, data: bytes) -> str:
        """截断超长输出。"""
        max_bytes = self._limits.max_output_bytes
        if len(data) > max_bytes:
            truncated = data[:max_bytes]
            text = truncated.decode("utf-8", errors="replace")
            text += f"\n...[输出截断：原始 {len(data)} 字节，截断至 {max_bytes} 字节]"
            return text
        return data.decode("utf-8", errors="replace")