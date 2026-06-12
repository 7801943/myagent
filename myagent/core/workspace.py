"""
工作空间状态容器。

核心原则：
  WorkspaceManager 是纯状态容器，不做任何文件 I/O。
  文件扫描由外部工具函数完成，结果注入容器。
  状态变更后通过 _on_change 回调通知 Session。

统一入口：
  WorkspaceManager.update(source, action, data)
    source: "user" | "agent"
    action: "set_root" | "open_file" | "close_file" | "set_active_file"
            "scan_dir" | "files_changed" | "mark_dirty" | "mark_llm_read"

数据流：
  前端用户操作 ──ws──→ ws_handler ──→ workspace.update("user", action, data)
                                              │
                                    WorkspaceManager.update("user", action, data)
                                              │
                                        _on_change(snapshot, source)
                                              │
                                    Session._on_workspace_change()
                                              ├─ 持久化
                                              └─ workspace 状态自动注入 LLM 上下文

  LLM 工具执行 ──→ tool_end hook ──→ workspace.update("agent", action, data)
                                              │
                                    WorkspaceManager.update("agent", action, data)
                                              │
                                        _on_change(snapshot, source)
                                              │
                                    Session._on_workspace_change()
                                              ├─ 持久化
                                              └─ ws_notify → 推送前端更新
"""

import asyncio
import copy
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


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
    is_user_opened: bool = False  # 用户在前端打开过此文件
    is_llm_read: bool = False     # LLM 后端读取过此文件

    def to_dict(self) -> dict:
        return {
            "path": self.path, "is_dir": self.is_dir,
            "size": self.size, "modified_at": self.modified_at,
            "is_user_opened": self.is_user_opened,
            "is_llm_read": self.is_llm_read,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileInfo":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items()})


@dataclass
class OpenFileTab:
    """前端打开的文件 Tab 状态。"""
    path: str              # 相对于 workspace root 的路径
    is_dirty: bool = False # 前端编辑器是否有未保存修改（预留）
    revision: int = 0      # 文件被 agent 写入/编辑后递增，用于前端刷新预览
    cursor_line: int = 0   # 光标行号（预留，当前未实现）
    cursor_column: int = 0 # 光标列号（预留，当前未实现）
    scroll_top: int = 0    # 滚动位置（预留，当前未实现）

    def to_dict(self) -> dict:
        return {
            "path": self.path, "is_dirty": self.is_dirty,
            "revision": self.revision,
            "cursor_line": self.cursor_line, "cursor_column": self.cursor_column,
            "scroll_top": self.scroll_top,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OpenFileTab":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items()})


@dataclass
class WorkspaceState:
    """工作空间完整快照，可 JSON 序列化后存入 DB 或发送给前端。"""
    root_path: str = ""
    files: list[FileInfo] = field(default_factory=list)            # 文件列表（扁平，含目录）
    open_files: list[OpenFileTab] = field(default_factory=list)    # 前端打开的文件 Tab
    active_file_index: int | None = None                           # 当前活跃文件索引
    expanded_dirs: list[str] = field(default_factory=list)         # 已展开扫描的目录路径

    def to_dict(self) -> dict:
        return {
            "root_path": self.root_path,
            "files": [f.to_dict() for f in self.files],
            "open_files": [f.to_dict() for f in self.open_files],
            "active_file_index": self.active_file_index,
            "expanded_dirs": self.expanded_dirs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkspaceState":
        return cls(
            root_path=d.get("root_path", ""),
            files=[FileInfo.from_dict(f) for f in d.get("files", [])],
            open_files=[OpenFileTab.from_dict(f) for f in d.get("open_files", [])],
            active_file_index=d.get("active_file_index"),
            expanded_dirs=d.get("expanded_dirs", []),
        )


# ══════════════════════════════════════
#  WorkspaceManager — 纯状态容器
# ══════════════════════════════════════

class WorkspaceManager:
    """
    工作空间状态容器（纯内存状态，不做文件 I/O）。

    职责：
      - 持有当前工作空间的完整状态（文件列表、打开文件、已展开目录）
      - 提供统一的 update(source, action, data) 方法修改状态
      - 状态变更后通过 _on_change 回调通知上层（Session）
      - 可导出快照（snapshot）用于持久化/传输/LLM 上下文注入
    """

    def __init__(self, root_path: str = ""):
        self._state = WorkspaceState(root_path=root_path)
        self._on_change: Callable[[WorkspaceState, str], Awaitable[None]] | None = None

    # ── 回调设置 ──

    def set_on_change(self, callback: Callable[[WorkspaceState, str], Awaitable[None]]) -> None:
        """由 Session 注入，状态变更时调用。callback(state, source)。"""
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
        for f in files_only[:80]:
            size_str = f"{f.size}B" if f.size < 1024 else f"{f.size // 1024}KB"
            flags = ""
            if f.is_user_opened:
                flags += " [用户已打开]"
            if f.is_llm_read:
                flags += " [LLM已读取]"
            lines.append(f"  {f.path} ({size_str}){flags}")
        if len(files_only) > 80:
            lines.append(f"  ... 还有 {len(files_only) - 80} 个文件")
        return "\n".join(lines)

    # ── 统一状态更新入口 ──

    async def _notify(self, source: str) -> None:
        """通知 Session 状态变更。"""
        if self._on_change:
            await self._on_change(self.snapshot(), source)

    async def update(self, source: str, action: str, data: dict) -> Any:
        """
        统一状态更新入口。

        Args:
            source: "user" | "agent" — 谁触发的变更
            action: 具体操作类型
            data: 操作数据 dict
        """
        handler = {
            "set_root": self._handle_set_root,
            "open_file": self._handle_open_file,
            "close_file": self._handle_close_file,
            "set_active_file": self._handle_set_active_file,
            "scan_dir": self._handle_scan_dir,
            "collapse_dir": self._handle_collapse_dir,
            "files_changed": self._handle_files_changed,
            "sync_client_state": self._handle_sync_client_state,
            "mark_dirty": self._handle_mark_dirty,
            "mark_llm_read": self._handle_mark_llm_read,
        }.get(action)

        if handler:
            return await handler(source, data)
        else:
            from myagent.utils.logging import get_logger
            get_logger(__name__).warning(f"Unknown workspace action: {action}")

    # ── action 处理器 ──

    async def _handle_set_root(self, source: str, data: dict) -> None:
        """设置工作空间根目录（清空所有状态，扫描根目录一层）。"""
        root_path = data.get("root_path", "") or data.get("path", "")
        if root_path:
            root_path = str(Path(root_path).expanduser().resolve())
        self._state = WorkspaceState(root_path=root_path)
        if root_path:
            files = await scan_dir_files(root_path)
            self._state.files = files
        await self._notify(source)

    async def _handle_open_file(self, source: str, data: dict) -> None:
        """打开文件。source=user 时标记 is_user_opened，source=agent 时标记 is_llm_read。"""
        path = data.get("path", "")
        line = data.get("line", 0)
        column = data.get("column", 0)
        if not path:
            return
        path = self._normalize_relative_path(path)
        if not path or self._is_known_dir(path):
            return

        # 标记 FileInfo flag
        if source == "user":
            self._set_file_flag(path, "is_user_opened", True)
        elif source == "agent":
            self._set_file_flag(path, "is_llm_read", True)

        # 检查是否已在 open_files 中
        for i, tab in enumerate(self._state.open_files):
            if tab.path == path:
                if source == "agent":
                    tab.revision += 1
                self._state.active_file_index = i
                await self._notify(source)
                return i
        # 新建 Tab
        tab = OpenFileTab(path=path, cursor_line=line, cursor_column=column)
        self._state.open_files.append(tab)
        idx = len(self._state.open_files) - 1
        self._state.active_file_index = idx
        await self._notify(source)
        return idx

    async def _handle_close_file(self, source: str, data: dict) -> None:
        """关闭文件 Tab。"""
        index = data.get("index", -1)
        if not (0 <= index < len(self._state.open_files)):
            return
        closed_path = self._state.open_files[index].path
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
        # 取消 is_user_opened 标记（如果没有其他 Tab 打开同一文件）
        still_open = any(t.path == closed_path for t in self._state.open_files)
        if not still_open:
            self._set_file_flag(closed_path, "is_user_opened", False)
        await self._notify(source)

    async def _handle_set_active_file(self, source: str, data: dict) -> None:
        """切换活跃文件。"""
        index = data.get("index", -1)
        if 0 <= index < len(self._state.open_files):
            self._state.active_file_index = index
            await self._notify(source)

    async def _handle_scan_dir(self, source: str, data: dict) -> None:
        """扫描指定子目录（一层），合并结果到文件列表。"""
        sub_path = data.get("path", "")
        if not self._state.root_path:
            return
        sub_path = self._normalize_relative_path(sub_path)
        if sub_path and not self._is_known_dir(sub_path):
            return

        # 扫描子目录
        new_entries = await scan_dir_files(self._state.root_path, sub_path=sub_path or None)

        # 移除该目录的旧直接子条目，替换为新的
        self._remove_direct_children(sub_path)
        self._state.files.extend(new_entries)

        # 记录已展开目录
        if sub_path and sub_path not in self._state.expanded_dirs:
            self._state.expanded_dirs.append(sub_path)

        await self._notify(source)

    async def _handle_collapse_dir(self, source: str, data: dict) -> None:
        """折叠目录：从 expanded_dirs 中移除，保持前后端同步。"""
        path = data.get("path", "")
        if not path:
            return
        path = self._normalize_relative_path(path)
        if path and path in self._state.expanded_dirs:
            self._state.expanded_dirs.remove(path)
            await self._notify(source)

    async def _handle_files_changed(self, source: str, data: dict) -> None:
        """文件变更后刷新：重新扫描所有已展开的目录。"""
        if not self._state.root_path:
            return

        changed_paths = self._normalize_path_list(data.get("changed_paths"))
        deleted_paths = self._normalize_path_list(data.get("deleted_paths"))
        renamed_paths = self._normalize_rename_list(data.get("renamed_paths"))

        if renamed_paths:
            self._apply_renamed_paths(renamed_paths)

        if changed_paths:
            changed_set = set(changed_paths)
            for tab in self._state.open_files:
                if tab.path in changed_set:
                    tab.revision += 1

        if deleted_paths:
            self._remove_deleted_open_tabs(deleted_paths)

        # 保存 flag 状态
        flag_map = {f.path: (f.is_user_opened, f.is_llm_read) for f in self._state.files}

        # 重新扫描根目录
        all_files = await scan_dir_files(self._state.root_path)

        # 重新扫描所有已展开的子目录
        for dir_path in list(self._state.expanded_dirs):
            sub_entries = await scan_dir_files(self._state.root_path, sub_path=dir_path)
            all_files.extend(sub_entries)

        # 恢复 flag 状态
        for f in all_files:
            if f.path in flag_map:
                f.is_user_opened, f.is_llm_read = flag_map[f.path]

        self._state.files = all_files
        await self._notify(source)

    async def _handle_sync_client_state(self, source: str, data: dict) -> None:
        """同步前端 workspace UI 状态，不接受客户端覆盖文件树/root。"""
        if not isinstance(data, dict):
            return

        existing_by_path = {tab.path: tab for tab in self._state.open_files}

        if "open_files" in data:
            next_tabs: list[OpenFileTab] = []
            seen: set[str] = set()
            raw_open_files = data.get("open_files") or []
            if not isinstance(raw_open_files, list):
                raw_open_files = []

            for item in raw_open_files[:50]:
                if isinstance(item, dict):
                    raw_path = item.get("path", "")
                else:
                    raw_path = str(item or "")
                path = self._normalize_relative_path(raw_path)
                if not path or path in seen or self._is_known_dir(path):
                    continue

                existing = existing_by_path.get(path)
                tab = copy.deepcopy(existing) if existing else OpenFileTab(path=path)
                if isinstance(item, dict):
                    tab.is_dirty = bool(item.get("is_dirty", tab.is_dirty))
                    tab.cursor_line = self._coerce_non_negative_int(
                        item.get("cursor_line", tab.cursor_line)
                    )
                    tab.cursor_column = self._coerce_non_negative_int(
                        item.get("cursor_column", tab.cursor_column)
                    )
                    tab.scroll_top = self._coerce_non_negative_int(
                        item.get("scroll_top", tab.scroll_top)
                    )
                next_tabs.append(tab)
                seen.add(path)

            self._state.open_files = next_tabs
            opened_paths = {tab.path for tab in next_tabs}
            for f in self._state.files:
                f.is_user_opened = f.path in opened_paths

        if "active_file_index" in data:
            index = data.get("active_file_index")
            if isinstance(index, int) and 0 <= index < len(self._state.open_files):
                self._state.active_file_index = index
            else:
                self._state.active_file_index = None

        if "expanded_dirs" in data:
            expanded_dirs: list[str] = []
            raw_dirs = data.get("expanded_dirs") or []
            if isinstance(raw_dirs, list):
                for raw_dir in raw_dirs[:200]:
                    path = self._normalize_relative_path(str(raw_dir or ""))
                    if path and path not in expanded_dirs and self._is_known_dir(path):
                        expanded_dirs.append(path)
            self._state.expanded_dirs = expanded_dirs

        await self._notify(source)

    async def _handle_mark_dirty(self, source: str, data: dict) -> None:
        """标记文件脏状态（前端编辑器用，预留）。"""
        index = data.get("index", -1)
        is_dirty = data.get("is_dirty", False)
        if 0 <= index < len(self._state.open_files):
            self._state.open_files[index].is_dirty = is_dirty

    async def _handle_mark_llm_read(self, source: str, data: dict) -> None:
        """标记文件被 LLM 读取。"""
        path = data.get("path", "")
        if path:
            self._set_file_flag(path, "is_llm_read", True)
            await self._notify(source)

    # ── 辅助方法 ──

    def _set_file_flag(self, path: str, flag: str, value: bool) -> None:
        """设置指定文件的 flag（is_user_opened 或 is_llm_read）。"""
        for f in self._state.files:
            if f.path == path:
                setattr(f, flag, value)
                return

    def _is_known_dir(self, path: str) -> bool:
        """判断路径是否是当前文件列表中的目录。"""
        return any(f.path == path and f.is_dir for f in self._state.files)

    @staticmethod
    def _coerce_non_negative_int(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return number if number >= 0 else 0

    @staticmethod
    def _normalize_relative_path(path: str) -> str:
        """规范化前端传入的工作区相对路径，拒绝绝对路径和越界路径。"""
        raw = str(path or "").replace("\\", "/").strip("/")
        if not raw:
            return ""
        if Path(raw).is_absolute():
            return ""
        normalized = os.path.normpath(raw).replace("\\", "/")
        if normalized == "." or normalized.startswith("../") or normalized == "..":
            return ""
        return normalized

    def _remove_direct_children(self, parent: str) -> None:
        """移除指定目录的直接子条目（用于重新扫描时替换）。"""
        prefix = (parent + "/") if parent else ""
        self._state.files = [
            f for f in self._state.files
            if not self._is_direct_child(f.path, parent)
        ]

    def _remove_deleted_open_tabs(self, deleted_paths: list[str]) -> None:
        """关闭已删除文件或删除目录下的打开 Tab。"""
        if not deleted_paths:
            return

        def is_deleted(path: str) -> bool:
            return any(path == deleted or path.startswith(deleted + "/") for deleted in deleted_paths)

        active_path = self.get_active_file_path()
        self._state.open_files = [
            tab for tab in self._state.open_files
            if not is_deleted(tab.path)
        ]
        if not self._state.open_files:
            self._state.active_file_index = None
            return
        if active_path and not is_deleted(active_path):
            for index, tab in enumerate(self._state.open_files):
                if tab.path == active_path:
                    self._state.active_file_index = index
                    return
        self._state.active_file_index = min(
            self._state.active_file_index or 0,
            len(self._state.open_files) - 1,
        )

    @classmethod
    def _normalize_path_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value[:500]:
            path = cls._normalize_relative_path(str(item or ""))
            if path and path not in result:
                result.append(path)
        return result

    @classmethod
    def _normalize_rename_list(cls, value: Any) -> list[tuple[str, str]]:
        if not isinstance(value, list):
            return []
        result: list[tuple[str, str]] = []
        for item in value[:200]:
            if not isinstance(item, dict):
                continue
            from_path = cls._normalize_relative_path(str(item.get("from") or ""))
            to_path = cls._normalize_relative_path(str(item.get("to") or ""))
            if from_path and to_path:
                result.append((from_path, to_path))
        return result

    def _apply_renamed_paths(self, renamed_paths: list[tuple[str, str]]) -> None:
        for from_path, to_path in renamed_paths:
            for tab in self._state.open_files:
                if tab.path == from_path:
                    tab.path = to_path
                    tab.revision += 1
                elif tab.path.startswith(from_path + "/"):
                    tab.path = to_path + tab.path[len(from_path):]
                    tab.revision += 1

            next_expanded: list[str] = []
            for dir_path in self._state.expanded_dirs:
                if dir_path == from_path:
                    next_path = to_path
                elif dir_path.startswith(from_path + "/"):
                    next_path = to_path + dir_path[len(from_path):]
                else:
                    next_path = dir_path
                if next_path not in next_expanded:
                    next_expanded.append(next_path)
            self._state.expanded_dirs = next_expanded

    @staticmethod
    def _is_direct_child(path: str, parent: str) -> bool:
        """判断 path 是否是 parent 的直接子条目。"""
        prefix = (parent + "/") if parent else ""
        if not path.startswith(prefix):
            return False
        remainder = path[len(prefix):]
        return "/" not in remainder and remainder != ""

    # ── 恢复 ──

    def restore_from(self, state: WorkspaceState) -> None:
        """从快照恢复（不触发通知，用于 Session 恢复场景）。"""
        self._state = copy.deepcopy(state)


# ══════════════════════════════════════
#  文件扫描工具函数（外部调用，非 WorkspaceManager 方法）
# ══════════════════════════════════════

IGNORED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
}


def _scan_level_sync(root: Path, sub_path: str | None = None) -> list[FileInfo]:
    """同步扫描目录（一层，非递归），返回直接子条目列表。"""
    root = root.expanduser().resolve()
    if sub_path:
        normalized = WorkspaceManager._normalize_relative_path(sub_path)
        if not normalized:
            return []
        target = (root / normalized).resolve()
    else:
        target = root
    if target != root and root not in target.parents:
        return []
    if not target.exists() or not target.is_dir():
        return []
    result: list[FileInfo] = []
    try:
        entries = sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except (PermissionError, OSError):
        return result
    for entry in entries:
        # 跳过隐藏文件（保留常见配置文件）
        if entry.name.startswith('.') and entry.name not in ('.gitignore', '.env.example'):
            continue
        if entry.is_dir() and entry.name in IGNORED_DIR_NAMES:
            continue

        rel = str(entry.relative_to(root))
        if entry.is_dir():
            result.append(FileInfo(path=rel, is_dir=True))
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
                ))
            except (PermissionError, OSError):
                result.append(FileInfo(path=rel, is_dir=False))
    return result


async def scan_dir_files(root_path: str, sub_path: str | None = None) -> list[FileInfo]:
    """
    扫描工作空间目录（一层，非递归），返回文件元信息列表。
    在后台线程中执行以避免阻塞事件循环。
    """
    return await asyncio.to_thread(_scan_level_sync, Path(root_path).expanduser().resolve(), sub_path)
