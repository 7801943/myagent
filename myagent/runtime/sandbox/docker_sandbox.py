"""
DockerSandbox：Docker 容器沙盒（预留骨架）。
通过 --sandbox-backend=docker 启用。

从 myagent/tools/sandbox/docker_sandbox.py 移入。
"""
from myagent.runtime.sandbox.base import BaseSandbox, SandboxResult


class DockerSandbox(BaseSandbox):
    """Docker 容器沙盒。Phase 2 预留骨架，不实现。"""

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> SandboxResult:
        raise NotImplementedError(
            "Docker sandbox is not yet implemented. "
            "Use --sandbox-backend=subprocess (default) instead."
        )