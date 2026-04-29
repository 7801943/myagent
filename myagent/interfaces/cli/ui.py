"""
CLI 界面：Rich 库驱动的终端 UI。
提供流式 Markdown 渲染 + 工具调用面板。
"""
import sys
from typing import TextIO

from myagent.utils.logging import get_logger

logger = get_logger(__name__)


def print_warning(msg: str) -> None:
    """打印黄色警告信息到终端。"""
    YELLOW = "\033[33m"
    RESET = "\033[0m"
    print(f"{YELLOW}⚠ {msg}{RESET}")


class CliUI:
    """CLI 界面管理器。使用 Rich 库进行终端渲染。"""

    def __init__(self, output: TextIO | None = None, show_tools: bool = False):
        self._output = output or sys.stdout
        self._show_tools = show_tools
        self._use_rich = self._detect_rich()

    def _detect_rich(self) -> bool:
        """检测是否可以使用 Rich。"""
        try:
            from rich.console import Console
            return True
        except ImportError:
            return False

    def print(self, text: str) -> None:
        """输出文本。"""
        if self._use_rich:
            self._print_rich(text)
        else:
            print(text, file=self._output)

    def print_stream_delta(self, delta: str) -> None:
        """输出流式增量文本。"""
        self._output.write(delta)
        self._output.flush()

    def print_thinking_delta(self, delta: str) -> None:
        """输出推理增量文本（暗色显示）。"""
        # 使用 ANSI 控制码将文本变灰
        self._output.write(f"\033[90m{delta}\033[0m")
        self._output.flush()

    def print_tool_call(self, tool_name: str, args: dict, call_id: str) -> None:
        """输出工具调用信息。"""
        if not self._show_tools:
            return
        if self._use_rich:
            self._print_tool_rich(tool_name, args, call_id)
        else:
            print(f"\n🔧 调用工具: {tool_name} (id={call_id[:8]}...)", file=self._output)

    def print_tool_result(self, tool_name: str, result: str, latency_ms: int) -> None:
        """输出工具执行结果。"""
        if not self._show_tools:
            if self._use_rich:
                self._print_result_rich(tool_name, "", latency_ms)
            else:
                print(f"  ✅ {tool_name} ({latency_ms}ms)", file=self._output)
            return

        if self._use_rich:
            self._print_result_full_rich(tool_name, str(result), latency_ms)
        else:
            print(f"  ✅ {tool_name} ({latency_ms}ms):\n{str(result)[:500]}", file=self._output)

    def print_error(self, message: str) -> None:
        """输出错误信息。"""
        if self._use_rich:
            try:
                from rich.console import Console
                Console(file=self._output).print(f"[red]❌ {message}[/red]")
            except Exception:
                print(f"❌ {message}", file=self._output)
        else:
            print(f"❌ {message}", file=self._output)

    def _print_rich(self, text: str) -> None:
        try:
            from rich.console import Console
            from rich.markdown import Markdown
            Console(file=self._output).print(Markdown(text))
        except Exception:
            print(text, file=self._output)

    def _print_tool_rich(self, tool_name: str, args: dict, call_id: str) -> None:
        try:
            from rich.console import Console
            from rich.panel import Panel
            import json
            Console(file=self._output).print(
                Panel(
                    json.dumps(args, ensure_ascii=False, indent=2),
                    title=f"🔧 {tool_name}",
                    border_style="blue",
                )
            )
        except Exception:
            print(f"\n🔧 调用工具: {tool_name}", file=self._output)

    def _print_result_rich(self, tool_name: str, result: str, latency_ms: int) -> None:
        try:
            from rich.console import Console
            Console(file=self._output).print(
                f"  [green]✅ {tool_name}[/green] ({latency_ms}ms)"
            )
        except Exception:
            print(f"  ✅ {tool_name} ({latency_ms}ms)", file=self._output)

    def _print_result_full_rich(self, tool_name: str, result: str, latency_ms: int) -> None:
        try:
            from rich.console import Console
            from rich.panel import Panel
            Console(file=self._output).print(
                Panel(
                    result[:1000] + ("..." if len(result) > 1000 else ""),
                    title=f"✅ {tool_name} ({latency_ms}ms)",
                    border_style="green",
                )
            )
        except Exception:
            print(f"  ✅ {tool_name} ({latency_ms}ms):\n{result[:500]}", file=self._output)