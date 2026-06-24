"""Virtual private/public workspace path resolver."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from myagent.core.workspace import FileInfo, _scan_level_sync

WorkspaceArea = Literal["private", "public"]
WorkspaceActor = Literal["user", "agent", "onlyoffice", "admin"]
WorkspaceOperation = Literal["read", "write", "upload", "rename", "delete", "list"]

_SAFE_USERNAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class ResolvedWorkspacePath:
    virtual_path: str
    real_path: Path
    area: WorkspaceArea
    inner_path: str


def safe_workspace_username(raw: str) -> str:
    value = _SAFE_USERNAME_RE.sub("_", str(raw or "").strip()).strip("._-")
    if not value or value in {".", ".."}:
        raise ValueError("Invalid username for workspace path")
    return value


class WorkspaceResolver:
    """Maps virtual workspace paths to per-user private and shared public roots."""

    def __init__(
        self,
        *,
        username: str,
        group: str = "user",
        private_root: str | Path,
        public_root: str | Path,
    ):
        self.username = username
        self.group = group or "user"
        self.private_root = Path(private_root).expanduser().resolve()
        self.public_root = Path(public_root).expanduser().resolve()
        self.private_root.mkdir(parents=True, exist_ok=True)
        self.public_root.mkdir(parents=True, exist_ok=True)

    @property
    def virtual_root(self) -> str:
        return f"workspace://{safe_workspace_username(self.username)}"

    def resolve(
        self,
        virtual_path: str,
        *,
        operation: WorkspaceOperation = "read",
        actor: WorkspaceActor = "user",
        must_exist: bool = False,
    ) -> ResolvedWorkspacePath:
        area, inner = self._split_virtual_path(virtual_path)
        root = self.private_root if area == "private" else self.public_root
        real = (root / inner).resolve() if inner else root
        if real != root and root not in real.parents:
            raise PermissionError("路径不能越出工作区")
        if must_exist and not real.exists():
            raise FileNotFoundError(str(virtual_path))
        if not self.can(actor, operation, area):
            raise PermissionError("当前用户或 actor 没有该工作区操作权限")
        return ResolvedWorkspacePath(
            virtual_path=self._join_virtual(area, inner),
            real_path=real,
            area=area,
            inner_path=inner,
        )

    def can(self, actor: WorkspaceActor, operation: WorkspaceOperation, area: WorkspaceArea) -> bool:
        if operation in {"read", "list"}:
            return True
        if area == "private":
            return actor in {"user", "agent", "onlyoffice", "admin"}
        # public write surface: users may upload, admin may manage, onlyoffice may save.
        if actor == "agent":
            return False
        if actor == "admin":
            return True
        if actor == "onlyoffice":
            return operation == "write"
        if actor == "user":
            return operation == "upload"
        return False

    def actor_for_user(self) -> WorkspaceActor:
        return "admin" if self.group == "admin" else "user"

    def to_virtual_path(self, real_path: str | Path) -> str | None:
        try:
            path = Path(real_path).expanduser().resolve()
        except Exception:
            return None
        if path == self.private_root:
            return "private"
        if path == self.public_root:
            return "public"
        if self.private_root in path.parents:
            return self._join_virtual("private", path.relative_to(self.private_root).as_posix())
        if self.public_root in path.parents:
            return self._join_virtual("public", path.relative_to(self.public_root).as_posix())
        return None

    def is_under_public(self, path: str | Path) -> bool:
        virtual = self.to_virtual_path(path)
        return bool(virtual == "public" or (virtual and virtual.startswith("public/")))

    def is_under_private(self, path: str | Path) -> bool:
        virtual = self.to_virtual_path(path)
        return bool(virtual == "private" or (virtual and virtual.startswith("private/")))

    async def scan_dir(self, virtual_dir: str | None = None) -> list[FileInfo]:
        virtual_dir = self._normalize_virtual_path(virtual_dir or "")
        if not virtual_dir:
            return [
                self._dir_info("private", "private"),
                self._dir_info("public", "public"),
            ]
        area, inner = self._split_virtual_path(virtual_dir)
        root = self.private_root if area == "private" else self.public_root
        entries = _scan_level_sync(root, inner or None)
        prefix = area if not inner else f"{area}/{inner}"
        result: list[FileInfo] = []
        for entry in entries:
            virtual_path = f"{prefix}/{entry.path}" if entry.path else prefix
            entry.path = virtual_path
            self._apply_permissions(entry, area)
            result.append(entry)
        return result

    def normalize_for_tool(self, path: str, *, operation: WorkspaceOperation) -> ResolvedWorkspacePath:
        raw = str(path or "").replace("\\", "/")
        virtual = self.to_virtual_path(raw)
        if virtual is None:
            if raw.startswith("public/") or raw == "public" or raw.startswith("private/") or raw == "private":
                virtual = raw
            elif Path(raw).is_absolute():
                raise PermissionError("工具路径不在当前工作区内")
            else:
                virtual = f"private/{raw.strip('/')}"
        return self.resolve(
            virtual,
            operation=operation,
            actor="agent",
            must_exist=operation == "read",
        )

    def _dir_info(self, path: str, area: WorkspaceArea) -> FileInfo:
        info = FileInfo(path=path, is_dir=True, area=area)
        self._apply_permissions(info, area)
        return info

    def _apply_permissions(self, info: FileInfo, area: WorkspaceArea) -> None:
        actor = self.actor_for_user()
        info.area = area
        info.can_read = True
        info.can_upload = self.can(actor, "upload", area)
        info.can_rename = self.can(actor, "rename", area)
        info.can_delete = self.can(actor, "delete", area)
        info.can_agent_read = True
        info.can_agent_write = self.can("agent", "write", area)

    def _split_virtual_path(self, path: str) -> tuple[WorkspaceArea, str]:
        normalized = self._normalize_virtual_path(path)
        if not normalized:
            raise ValueError("路径必须以 private/ 或 public/ 开头")
        if normalized == "private" or normalized.startswith("private/"):
            return "private", normalized.removeprefix("private").strip("/")
        if normalized == "public" or normalized.startswith("public/"):
            return "public", normalized.removeprefix("public").strip("/")
        raise ValueError("路径必须以 private/ 或 public/ 开头")

    @staticmethod
    def _join_virtual(area: WorkspaceArea, inner: str) -> str:
        inner = str(inner or "").strip("/")
        return f"{area}/{inner}" if inner else area

    @staticmethod
    def _normalize_virtual_path(path: str | None) -> str:
        raw = str(path or "").replace("\\", "/").strip("/")
        if not raw:
            return ""
        if Path(raw).is_absolute() or re.match(r"^[A-Za-z]:", raw):
            return ""
        normalized = os.path.normpath(raw).replace("\\", "/")
        if normalized == "." or normalized == ".." or normalized.startswith("../"):
            return ""
        return normalized.strip("/")

