"""
工作空间状态容器。

核心原则：
  WorkspaceManager 是纯状态容器，不做任何文件 I/O。
  文件扫描由外部工具函数完成，结果注入容器。
  状态变更后通过 _on_change 回调通知 Session。

数据流：
  前端用户操作 ──ws──→ ws_handler ──→ Session ──→ WorkspaceManager.update()
                                                          │
                                                    _on_change(snapshot)
                                                          │
                                                  Session._on_workspace_change()
                                                          │
                                                  推送前端 + 注入 LLM 上下文

  LLM 工具执行 ──→ Session.record_llm_access() ──→ WorkspaceManager.record_llm_access()
                                                          │
                                                    同上通知链路
"""

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Awaitable, Callable


# ══════════════════════════════════════
#  数据模型
# ══════════════════════════════════════

@dataclass
class FileInfo:
    """文件元信息（对应目录树中的一个条目）。"""
    path: str              # 相对于 workspace root 的路径（如 "src/core/agent.py"）
    is_dir: bool = False   # 是否为目录
    size: int = 0          # 文件大小（字节），目录为 0
    modified_at: str = ""  # ISO 格式最后修改时间
    language: str = ""     # 编程语言标识（如 "python"），目录为 ""

    def to_dict(self) -> dict:
        return {
            "path": self.path, "is_dir": self.is_dir,
            "size": self.size, "modified_at": self.modified_at,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileInfo":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items()})


@dataclass
class OpenFileTab:
    """前端打开的文件 Tab 状态。"""
    path: str              # 相对于 workspace root 的路径
    is_dirty: bool = False # 前端编辑器是否有未保存修改（预留）
    cursor_line: int = 0   # 光标行号（预留，当前未实现）
    cursor_column: int = 0 # 光标列号（预留，当前未实现）
    scroll_top: int = 0    # 滚动位置（预留，当前未实现）

    def to_dict(self) -> dict:
        return {
            "path": self.path, "is_dirty": self.is_dirty,
            "cursor_line": self.cursor_line, "cursor_column": self.cursor_column,
            "scroll_top": self.scroll_top,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OpenFileTab":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items()})


@dataclass
class LLMFileRecord:
    """LLM 后端访问文件记录（预留，当前未实现完整功能）。"""
    path: str              # 相对于 workspace root 的路径
    preview: str = ""      # 文件内容预览（前 200 字符）
    accessed_at: str = ""  # ISO 格式访问时间

    def to_dict(self) -> dict:
        return {"path": self.path, "preview": self.preview, "accessed_at": self.accessed_at}

    @classmethod
    def from_dict(cls, d: dict) -> "LLMFileRecord":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items()})


@dataclass
class WorkspaceState:
    """工作空间完整快照，可 JSON 序列化后存入 DB 或发送给前端。"""
    root_path: str = ""
    files: list[FileInfo] = field(default_factory=list)            # 文件列表（扁平，含目录）
    open_files: list[OpenFileTab] = field(default_factory=list)    # 前端打开的文件 Tab
    active_file_index: int | None = None                           # 当前活跃文件索引
    llm_files: list[LLMFileRecord] = field(default_factory=list)   # LLM 访问记录

    def to_dict(self) -> dict:
        return {
            "root_path": self.root_path,
            "files": [f.to_dict() for f in self.files],
            "open_files": [f.to_dict() for f in self.open_files],
            "active_file_index": self.active_file_index,
            "llm_files": [f.to_dict() for f in self.llm_files],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkspaceState":
        return cls(
            root_path=d.get("root_path", ""),
            files=[FileInfo.from_dict(f) for f in d.get("files", [])],
            open_files=[OpenFileTab.from_dict(f) for f in d.get("open_files", [])],
            active_file_index=d.get("active_file_index"),
            llm_files=[LLMFileRecord.from_dict(f) for f in d.get("llm_files", [])],
        )


# ══════════════════════════════════════
#  WorkspaceManager — 纯状态容器
# ══════════════════════════════════════

class WorkspaceManager:
    """
    工作空间状态容器（纯内存状态，不做文件 I/O）。

    职责：
      - 持有当前工作空间的完整状态（文件列表、打开文件、LLM 访问记录）
      - 提供 update 方法修改状态
      - 状态变更后通过 _on_change 回调通知上层（Session）
      - 可导出快照（snapshot）用于持久化/传输/LLM 上下文注入
    """

    def __init__(self, root_path: str = ""):
        self._state = WorkspaceState(root_path=root_path)
        self._on_change: Callable[[WorkspaceState], Awaitable[None]] | None = None

    # ── 回调设置 ──

    def set_on_change(self, callback: Callable[[WorkspaceState], Awaitable[None]]) -> None:
        """由 Session 注入，状态变更时调用。"""
        self._on_change = callback

    # ── 属性访问 ──

    @property
    def root_path(self) -> str:
        return self._state.root_path

    @property
    def state(self) -> WorkspaceState:
        return self._state

    def snapshot(self) -> WorkspaceState:
        """返回深拷贝快照（安全，调用方修改不影响内部状态）。"""
        return copy.deepcopy(self._state)

    def get_active_file_path(self) -> str | None:
        """获取当前活跃文件的相对路径。"""
        idx = self._state.active_file_index
        if idx is not None and 0 <= idx < len(self._state.open_files):
            return self._state.open_files[idx].path
        return None

    def get_file_list_text(self) -> str:
        """生成文件列表的文本摘要，用于注入 LLM 上下文。"""
        if not self._state.files and not self._state.root_path:
            return ""
        lines = [f"[工作空间] {self._state.root_path}"]
        file_count = sum(1 for f in self._state.files if not f.is_dir)
        dir_count = sum(1 for f in self._state.files if f.is_dir)
        lines.append(f"  共 {dir_count} 个目录, {file_count} 个文件")
        # 只列出文件（非目录），按路径排序，限制行数
        files_only = sorted(
            [f for f in self._state.files if not f.is_dir],
            key=lambda f: f.path,
        )
        for f in files_only[:80]:  # 最多 80 个文件，避免上下文过长
            size_str = f"{f.size}B" if f.size < 1024 else f"{f.size // 1024}KB"
            lines.append(f"  {f.path} ({size_str}, {f.language})")
        if len(files_only) > 80:
            lines.append(f"  ... 还有 {len(files_only) - 80} 个文件")
        return "\n".join(lines)

    # ── 状态更新方法（均触发 _on_change）──

    async def _notify(self) -> None:
        """通知 Session 状态变更。"""
        if self._on_change:
            await self._on_change(self.snapshot())

    async def set_root(self, root_path: str) -> None:
        """设置工作空间根目录（清空所有状态）。"""
        self._state = WorkspaceState(root_path=root_path)
        await self._notify()

    async def update_files(self, files: list[FileInfo]) -> None:
        """更新文件列表（由外部扫描后调用）。"""
        self._state.files = list(files)  # 浅拷贝足够，FileInfo 不可变
        await self._notify()

    async def open_file(self, path: str, line: int = 0, column: int = 0) -> int:
        """记录前端打开文件。若已打开则切换活跃，否则新建 Tab。返回索引。"""
        # 检查是否已在 open_files 中
        for i, tab in enumerate(self._state.open_files):
            if tab.path == path:
                self._state.active_file_index = i
                await self._notify()
                return i
        # 新建 Tab
        tab = OpenFileTab(path=path, cursor_line=line, cursor_column=column)
        self._state.open_files.append(tab)
        idx = len(self._state.open_files) - 1
        self._state.active_file_index = idx
        await self._notify()
        return idx

    async def close_file(self, index: int) -> None:
        """记录前端关闭文件 Tab。"""
        if not (0 <= index < len(self._state.open_files)):
            return
        self._state.open_files.pop(index)
        # 修正 active_file_index
        if self._state.active_file_index is not None:
            if index < self._state.active_file_index:
                self._state.active_file_index -= 1
            elif index == self._state.active_file_index:
                if self._state.open_files:
                    self._state.active_file_index = min(
                        index, len(self._state.open_files) - 1
                    )
                else:
                    self._state.active_file_index = None
        await self._notify()

    async def set_active_file(self, index: int) -> None:
        """切换活跃文件。"""
        if 0 <= index < len(self._state.open_files):
            self._state.active_file_index = index
            await self._notify()

    async def mark_dirty(self, index: int, is_dirty: bool) -> None:
        """标记文件脏状态（前端编辑器用，预留）。"""
        if 0 <= index < len(self._state.open_files):
            self._state.open_files[index].is_dirty = is_dirty
            # 脏状态变更暂不通知前端（前端自己知道），仅后端记录

    async def record_llm_access(self, path: str, preview: str = "") -> None:
        """记录 LLM 访问的文件（预留，去重更新）。"""
        now = datetime.now(timezone.utc).isoformat()
        record = LLMFileRecord(path=path, preview=preview[:200], accessed_at=now)
        # 去重：如果已存在则更新
        for i, r in enumerate(self._state.llm_files):
            if r.path == path:
                self._state.llm_files[i] = record
                await self._notify()
                return
        self._state.llm_files.append(record)
        await self._notify()

    # ── 恢复 ──

    def restore_from(self, state: WorkspaceState) -> None:
        """从快照恢复（不触发通知，用于 Session 恢复场景）。"""
        self._state = copy.deepcopy(state)


# ══════════════════════════════════════
#  文件扫描工具函数（外部调用，非 WorkspaceManager 方法）
# ══════════════════════════════════════

# 需要精确匹配的忽略目录
_IGNORE_DIRS_EXACT = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    ".tox", ".eggs",
}

# 需要通配符匹配的忽略模式
_IGNORE_DIRS_PATTERNS = {
    "*.egg-info",
}

_LANGUAGE_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript", ".html": "html",
    ".css": "css", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".md": "markdown", ".txt": "text", ".toml": "toml",
    ".sh": "bash", ".sql": "sql", ".xml": "xml", ".rs": "rust",
    ".go": "go", ".java": "java", ".c": "c", ".cpp": "cpp",
    ".h": "c", ".hpp": "cpp", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".kt": "kotlin", ".scala": "scala",
    ".lua": "lua", ".r": "r", ".zig": "zig",
}


def _detect_language(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return _LANGUAGE_MAP.get(suffix, "text")


def _should_ignore_dir(name: str) -> bool:
    """检查目录是否应该被忽略。"""
    if name in _IGNORE_DIRS_EXACT:
        return True
    return any(fnmatch(name, pat) for pat in _IGNORE_DIRS_PATTERNS)


def _scan_sync(root: Path) -> list[FileInfo]:
    """同步扫描目录，返回扁平文件列表。"""
    result: list[FileInfo] = []

    def _recurse(current: Path) -> None:
        try:
            entries = sorted(current.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except (PermissionError, OSError):
            return
        for entry in entries:
            # 跳过隐藏文件（保留常见配置文件）
            if entry.name.startswith('.') and entry.name not in ('.gitignore', '.env.example'):
                continue
            # 跳过忽略目录
            if entry.is_dir() and _should_ignore_dir(entry.name):
                continue

            rel = str(entry.relative_to(root))
            if entry.is_dir():
                result.append(FileInfo(path=rel, is_dir=True, language=""))
                _recurse(entry)
            else:
                try:
                    stat = entry.stat()
                    result.append(FileInfo(
                        path=rel,
                        is_dir=False,
                        size=stat.st_size,
                        modified_at=datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                        language=_detect_language(entry.name),
                    ))
                except (PermissionError, OSError):
                    result.append(FileInfo(path=rel, is_dir=False, language=_detect_language(entry.name)))

    _recurse(root)
    return result


async def scan_workspace_files(root_path: str) -> list[FileInfo]:
    """
    扫描工作空间目录，返回文件元信息列表。
    在后台线程中执行以避免阻塞事件循环。
    """
    return await asyncio.to_thread(_scan_sync, Path(root_path).resolve())