"""
FileReadTool / FileWriteTool：文件读写工具。
包含路径安全检查，防止访问敏感系统目录。

从 myagent/tools/file_tools.py 移入 builtin/。
"""
from pathlib import Path

from myagent.tools.api import ToolResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

# 默认禁止访问的路径
_DENIED_PATHS = {
    "/etc", "/root",
    "/sys", "/proc/sys", "/boot", "/dev",
}


def _check_path_safety(path: str, denied_paths: set[str] | None = None) -> str | None:
    """
    路径安全检查。
    返回 None 表示安全，返回错误消息表示不安全。
    """
    denied = denied_paths or _DENIED_PATHS
    resolved = str(Path(path).resolve())
    for denied_path in denied:
        if resolved.startswith(denied_path):
            return f"路径被安全策略禁止: {path} (匹配: {denied_path})"
    return None


class FileReadTool:
    """读取文件内容。"""
    name = "file_read"
    description = "读取指定路径的文件内容。支持文本文件，自动检测编码。"
    meta = None

    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径",
            },
            "max_lines": {
                "type": "integer",
                "description": "最多读取的行数（可选，默认 500）",
                "default": 500,
            },
        },
        "required": ["path"],
    }

    def __init__(self, denied_paths: set[str] | None = None):
        self._denied_paths = denied_paths or _DENIED_PATHS

    async def execute(self, path: str, max_lines: int = 500, **kwargs) -> ToolResult:
        # 路径安全检查
        error = _check_path_safety(path, self._denied_paths)
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


class FileWriteTool:
    """写入文件内容。"""
    name = "file_write"
    description = "将内容写入指定路径的文件。如果文件不存在则创建，如果文件已存在则覆盖。"
    meta = None

    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要写入的文件路径",
            },
            "content": {
                "type": "string",
                "description": "要写入的文件内容",
            },
            "append": {
                "type": "boolean",
                "description": "是否追加模式（默认覆盖写入）",
                "default": False,
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, denied_paths: set[str] | None = None):
        self._denied_paths = denied_paths or _DENIED_PATHS

    async def execute(self, path: str, content: str, append: bool = False, **kwargs) -> ToolResult:
        # 路径安全检查
        error = _check_path_safety(path, self._denied_paths)
        if error:
            return ToolResult(content=error, is_error=True)

        target = Path(path)
        try:
            # 确保父目录存在
            target.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if append else "w"
            with open(target, mode, encoding="utf-8") as f:
                f.write(content)

            action = "追加" if append else "写入"
            return ToolResult(
                content=f"文件{action}成功: {target.resolve()} ({len(content)} 字符)",
                metadata={"path": str(target.resolve()), "chars_written": len(content)},
            )
        except Exception as e:
            return ToolResult(content=f"写入文件失败: {e}", is_error=True)