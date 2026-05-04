"""
BaseSandbox：沙盒执行抽象基类。
定义 run() 接口，由不同后端（subprocess / Docker）实现。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SandboxResult:
    """沙盒执行结果。"""
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    killed: bool = False
    execution_time_ms: int = 0

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.killed

    @property
    def output(self) -> str:
        """合并 stdout 和 stderr 输出。"""
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        if self.timed_out:
            parts.append("[TIMED OUT]")
        if self.killed:
            parts.append("[KILLED]")
        return "\n".join(parts) if parts else "(no output)"


class BaseSandbox(ABC):
    """沙盒抽象基类。"""

    @abstractmethod
    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> SandboxResult:
        """在沙盒中执行命令。"""
        ...