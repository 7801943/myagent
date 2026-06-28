"""Workspace path resolver with private/public permission areas."""

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
    """Maps user-visible workspace paths to per-user private and shared public roots."""

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
        self.private_label, self.public_label = self._build_root_labels()

    @property
    def virtual_root(self) -> str:
        return f"workspace://{safe_workspace_username(self.username)}"

    @property
    def private_virtual_root(self) -> str:
        return self.private_label

    @property
    def public_virtual_root(self) -> str:
        return self.public_label

    @property
    def root_virtual_paths(self) -> tuple[str, str]:
        return (self.private_virtual_root, self.public_virtual_root)

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
            return self.private_virtual_root
        if path == self.public_root:
            return self.public_virtual_root
        if self.private_root in path.parents:
            return self._join_virtual("private", path.relative_to(self.private_root).as_posix())
        if self.public_root in path.parents:
            return self._join_virtual("public", path.relative_to(self.public_root).as_posix())
        return None

    def is_under_public(self, path: str | Path) -> bool:
        virtual = self.to_virtual_path(path)
        return self.virtual_path_area(virtual) == "public" if virtual else False

    def is_under_private(self, path: str | Path) -> bool:
        virtual = self.to_virtual_path(path)
        return self.virtual_path_area(virtual) == "private" if virtual else False

    def virtual_path_area(self, path: str | None) -> WorkspaceArea | None:
        try:
            area, _inner = self._split_virtual_path(path or "")
            return area
        except ValueError:
            return None

    async def scan_dir(self, virtual_dir: str | None = None) -> list[FileInfo]:
        virtual_dir = self._normalize_virtual_path(virtual_dir or "")
        if not virtual_dir:
            return [
                self._dir_info(self.private_virtual_root, "private"),
                self._dir_info(self.public_virtual_root, "public"),
            ]
        area, inner = self._split_virtual_path(virtual_dir)
        root = self.private_root if area == "private" else self.public_root
        entries = _scan_level_sync(root, inner or None)
        result: list[FileInfo] = []
        for entry in entries:
            entry.path = self._join_virtual(area, entry.path)
            self._apply_permissions(entry, area)
            result.append(entry)
        return result

    def real_cwd_for_agent(self) -> Path:
        """Return the concrete cwd CLI tools should use for this workspace."""
        return self.private_root

    def normalize_for_tool(self, path: str, *, operation: WorkspaceOperation) -> ResolvedWorkspacePath:
        raw = str(path or "").replace("\\", "/")
        virtual = self.to_virtual_path(raw)
        if virtual is None:
            if self.virtual_path_area(raw):
                virtual = raw
            elif Path(raw).is_absolute():
                raise PermissionError("工具路径不在当前工作区内")
            else:
                virtual = self._join_virtual("private", raw.strip("/"))
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
            raise ValueError(self._path_error())
        for area, label in (("private", self.private_virtual_root), ("public", self.public_virtual_root)):
            if normalized == label or normalized.startswith(label + "/"):
                return area, normalized.removeprefix(label).strip("/")
        # Backward compatibility for persisted snapshots and old prompts.
        if normalized == "private" or normalized.startswith("private/"):
            return "private", normalized.removeprefix("private").strip("/")
        if normalized == "public" or normalized.startswith("public/"):
            return "public", normalized.removeprefix("public").strip("/")
        raise ValueError(self._path_error())

    def _join_virtual(self, area: WorkspaceArea, inner: str) -> str:
        label = self.private_virtual_root if area == "private" else self.public_virtual_root
        inner = str(inner or "").strip("/")
        return f"{label}/{inner}" if inner else label

    def _path_error(self) -> str:
        return f"路径必须以 {self.private_virtual_root}/ 或 {self.public_virtual_root}/ 开头"

    def _build_root_labels(self) -> tuple[str, str]:
        private_label = self._safe_label(self.private_root.name or "private")
        public_label = self._safe_label(self.public_root.name or "public")
        if private_label == public_label:
            private_label = self._safe_label(self.private_root.parent.name + "_" + private_label)
        if private_label == public_label:
            private_label = f"{private_label}_private"
        return private_label, public_label

    @staticmethod
    def _safe_label(label: str) -> str:
        cleaned = str(label or "").replace("\\", "/").strip("/")
        cleaned = cleaned.replace("/", "_")
        if cleaned in {"", ".", ".."}:
            return "workspace"
        if cleaned in {"private", "public"}:
            return f"{cleaned}_dir"
        return cleaned

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
