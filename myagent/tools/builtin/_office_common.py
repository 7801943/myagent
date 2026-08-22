"""Shared helpers for the format-specific document tools."""

from __future__ import annotations

import hashlib
from pathlib import Path

from myagent.tools.api import ToolResult


def file_version(path: Path) -> str:
    """Return a stable content version suitable for optimistic concurrency checks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def version_conflict(
    path: Path,
    expected_version: str | None,
    *,
    current_version: str | None = None,
) -> ToolResult | None:
    """Reject an edit when the file changed after the caller last read it."""
    if not expected_version:
        return None
    current_version = current_version or file_version(path)
    if expected_version == current_version:
        return None
    return ToolResult(
        content=(
            "文件版本不匹配，文件可能已被其他编辑器或任务修改。"
            "请重新读取文件后再编辑。\n"
            f"期望版本: {expected_version}\n当前版本: {current_version}"
        ),
        is_error=True,
        metadata={
            "path": str(path.resolve()),
            "expected_version": expected_version,
            "current_version": current_version,
        },
    )


def attach_version(
    result: ToolResult,
    path: Path,
    *,
    previous_version: str | None = None,
    include_in_content: bool = True,
) -> ToolResult:
    """Expose file versions to both the runtime and the language model."""
    if result.is_error or not path.exists():
        return result

    current_version = file_version(path)
    result.metadata = dict(result.metadata or {})
    result.metadata["path"] = str(path.resolve())
    result.metadata["version"] = current_version
    if previous_version is not None:
        result.metadata["previous_version"] = previous_version

    if include_in_content:
        version_lines = []
        if previous_version is not None and previous_version != current_version:
            version_lines.append(f"修改前版本: {previous_version}")
        version_lines.append(f"文件版本: {current_version}")
        result.content = result.content.rstrip() + "\n\n" + "\n".join(version_lines)
    return result
