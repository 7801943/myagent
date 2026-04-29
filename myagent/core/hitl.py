"""
HITLController：人在回路控制器。
Phase 2 实现 CLI 模式下的同步审批（用户输入 y/n）。
预留 WebSocket 异步审批接口供 Phase 4 使用。
"""
import asyncio
from typing import Any

from myagent.context.message import ToolCall
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class HITLController:
    """
    HITL 控制器基类。
    提供审批请求接口，具体实现由不同的 Interface 层提供。
    """

    async def request_approval(
        self,
        tool_name: str,
        reason: str,
        tool_call: ToolCall,
    ) -> bool:
        """
        请求人工审批。
        返回 True 表示批准，False 表示拒绝。
        默认实现：自动拒绝（安全第一）。
        """
        logger.warning(f"HITL: auto-rejecting {tool_name} (no approval handler)")
        return False


class CLIHITLController(HITLController):
    """
    CLI 模式下的 HITL 控制器。
    通过 Rich Console 向用户展示审批请求，等待输入。
    """

    def __init__(self, console: Any = None, timeout: int = 120):
        self._console = console
        self._timeout = timeout

    async def request_approval(
        self,
        tool_name: str,
        reason: str,
        tool_call: ToolCall,
    ) -> bool:
        """在 CLI 中请求用户审批。"""
        if self._console is None:
            from rich.console import Console
            self._console = Console()

        self._console.print()
        self._console.print("[bold yellow]========== 需要人工审批 ==========[/]")
        self._console.print(f"  工具: [bold]{tool_name}[/]")
        self._console.print(f"  原因: {reason}")
        self._console.print(f"  参数: {tool_call.arguments}")
        self._console.print("[bold yellow]=================================[/]")
        self._console.print()

        # 使用 run_in_executor 包装阻塞的 console.input
        loop = asyncio.get_running_loop()
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._console.input(
                        "[bold yellow]是否批准执行？[/] ([green]y[/]es / [red]n[/]o): "
                    ).strip().lower()
                ),
                timeout=self._timeout
            )
        except asyncio.TimeoutError:
            self._console.print("[red]审批超时，自动拒绝[/]")
            return False
        except (EOFError, KeyboardInterrupt):
            self._console.print("[dim]审批被取消[/]")
            return False

        if response in ("y", "yes", "approve"):
            self._console.print("[green]已批准执行[/]")
            return True
        elif response in ("n", "no", "reject"):
            self._console.print("[red]已拒绝执行[/]")
            return False
        else:
            self._console.print("[red]未识别的输入，自动拒绝[/]")
            return False