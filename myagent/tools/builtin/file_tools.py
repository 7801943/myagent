"""
FileRead / FileWrite 工具函数。
通过 JsonRpcProxy -> ExecutionEngine.execute_function() 执行。
"""
from pathlib import Path

from myagent.tools.api import tool, ToolResult

_DENIED_PATHS = {
    "/etc", "/root",
    "/sys", "/proc/sys", "/boot", "/dev",
}


def _check_path_safety(path: str) -> str | None:
    resolved = str(Path(path).resolve())
    for denied_path in _DENIED_PATHS:
        if resolved.startswith(denied_path):
            return f"路径被安全策略禁止: {path} (匹配: {denied_path})"
    return None


@tool(name="file_read",
      description="读取指定路径的文件内容。支持文本文件，自动检测编码。")
async def file_read(path: str, max_lines: int = 500) -> ToolResult:
    error = _check_path_safety(path)
    if error:
        return ToolResult(content=error, is_error=True)

    target = Path(path)
    if not target.exists():
        return ToolResult(content=f"文件不存在: {path}", is_error=True)
    if not target.is_file():
        return ToolResult(content=f"不是文件: {path}", is_error=True)

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    lines.append(f"\n...[截断：文件超过 {max_lines} 行]")
                    break
                lines.append(line)
        content = "".join(lines)
        return ToolResult(
            content=content,
            metadata={"path": str(target.resolve()), "lines_read": len(lines)},
        )
    except Exception as e:
        return ToolResult(content=f"读取文件失败: {e}", is_error=True)


@tool(name="file_write",
      description="将内容写入指定路径的文件。不存在则创建，已存在则覆盖。")
async def file_write(path: str, content: str,
                     append: bool = False) -> ToolResult:
    error = _check_path_safety(path)
    if error:
        return ToolResult(content=error, is_error=True)

    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content)

        action = "追加" if append else "写入"
        return ToolResult(
            content=f"文件{action}成功: {target.resolve()} ({len(content)} 字符)",
            metadata={"path": str(target.resolve()),
                      "chars_written": len(content)},
        )
    except Exception as e:
        return ToolResult(content=f"写入文件失败: {e}", is_error=True)
