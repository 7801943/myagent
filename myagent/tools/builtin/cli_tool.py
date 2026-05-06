"""
CLITool：在安全沙盒中执行 CLI 命令。
集成 CLIFence 安全围栏，在执行前进行白名单/黑名单/路径检查。

从 myagent/tools/cli_tool.py 移入 builtin/。
"""
from myagent.tools.base import BaseTool, ToolResult
from myagent.runtime.sandbox.base import BaseSandbox
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class CLITool(BaseTool):
    """
    CLI 命令执行工具。
    通过 BaseSandbox 执行命令，前置安全检查由 ToolExecutor -> PolicyEngine 处理。
    """
    name = "cli_execute"
    description = (
        "在安全沙盒中执行 CLI 命令。"
        "可以执行常见的文件操作、Python 脚本、git 命令等。"
        "受到安全围栏限制，危险命令会被拦截。"
    )

    parameters_schema = {
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
    }

    def __init__(self, sandbox: BaseSandbox):
        self._sandbox = sandbox

    async def execute(self, command: str, cwd: str | None = None, **kwargs) -> ToolResult:
        """执行 CLI 命令。"""
        logger.info(f"CLITool executing: {command[:100]}")

        result = await self._sandbox.run(command, cwd=cwd)

        if result.timed_out:
            return ToolResult(
                content=f"命令执行超时: {command[:100]}\n{result.output}",
                is_error=True,
                metadata={"execution_time_ms": result.execution_time_ms},
            )

        if result.exit_code != 0:
            return ToolResult(
                content=f"命令执行失败 (退出码: {result.exit_code}):\n{result.output}",
                is_error=True,
                metadata={
                    "exit_code": result.exit_code,
                    "execution_time_ms": result.execution_time_ms,
                },
            )

        return ToolResult(
            content=result.output,
            metadata={"execution_time_ms": result.execution_time_ms},
        )