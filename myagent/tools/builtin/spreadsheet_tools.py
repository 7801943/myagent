"""Format-specific tools for XLSX workbooks."""

from pathlib import Path
from typing import Any, Literal

from myagent.tools.api import ToolResult, tool
from myagent.tools.builtin._file_common import _check_path_safety
from myagent.tools.builtin._office_common import attach_version, file_version, version_conflict
from myagent.tools.builtin.file_edit import file_edit_table
from myagent.tools.builtin.file_read import file_read


_SPREADSHEET_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "XLSX 文件路径。"},
        "sheet_name": {
            "type": "string",
            "description": "工作表名；单工作表或 table_name 可唯一定位时可省略。",
        },
        "operation": {
            "type": "string",
            "enum": ["set_range", "update_cells", "clear_range", "append_rows"],
            "description": "编辑动作。",
        },
        "payload": {
            "description": "与 operation 对应的结构化参数。",
            "oneOf": [
                {
                    "title": "set_range payload",
                    "type": "object",
                    "properties": {
                        "range": {"type": "string", "description": "A1 区域，如 A2:C3。"},
                        "values": {
                            "type": "array",
                            "items": {"type": "array", "items": {}},
                            "description": "尺寸与 range 一致的矩形二维数组。",
                        },
                        "resize_range": {"type": "boolean", "default": False},
                        "value_input": {
                            "type": "string",
                            "enum": ["auto", "raw", "formula"],
                            "default": "auto",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["text", "number", "bool", "date", "formula", "blank"],
                        },
                    },
                    "required": ["range", "values"],
                    "additionalProperties": False,
                },
                {
                    "title": "update_cells payload",
                    "type": "object",
                    "properties": {
                        "cells": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "cell": {"type": "string", "description": "单元格地址，如 B4。"},
                                    "value": {},
                                    "kind": {
                                        "type": "string",
                                        "enum": ["text", "number", "bool", "date", "formula", "blank"],
                                    },
                                },
                                "required": ["cell"],
                                "additionalProperties": False,
                            },
                        },
                        "value_input": {
                            "type": "string",
                            "enum": ["auto", "raw", "formula"],
                            "default": "auto",
                        },
                    },
                    "required": ["cells"],
                    "additionalProperties": False,
                },
                {
                    "title": "clear_range payload",
                    "type": "object",
                    "properties": {
                        "range": {"type": "string", "description": "要清空值的 A1 区域。"},
                        "clear": {"type": "string", "enum": ["values"], "default": "values"},
                    },
                    "required": ["range"],
                    "additionalProperties": False,
                },
                {
                    "title": "append_rows payload",
                    "type": "object",
                    "properties": {
                        "rows": {
                            "type": "array",
                            "items": {"anyOf": [{"type": "array"}, {"type": "object"}]},
                            "description": "按列顺序排列的数组，或以表头为键的对象。",
                        },
                        "table_name": {"type": "string", "description": "可选 Excel table 名。"},
                        "header_row": {"type": "integer", "minimum": 1, "default": 1},
                        "value_input": {
                            "type": "string",
                            "enum": ["auto", "raw", "formula"],
                            "default": "auto",
                        },
                    },
                    "required": ["rows"],
                    "additionalProperties": False,
                },
            ],
        },
        "dry_run": {
            "type": "boolean",
            "default": True,
            "description": "默认只预览；确认预览后传 false 才落盘。",
        },
        "expected_version": {
            "type": "string",
            "description": "spreadsheet_read 返回的文件版本；不匹配时拒绝写入。",
        },
    },
    "required": ["path", "operation", "payload"],
}


def _spreadsheet_target(path: str) -> tuple[Path | None, ToolResult | None]:
    error = _check_path_safety(path)
    if error:
        return None, ToolResult(content=error, is_error=True)
    target = Path(path)
    if not target.exists():
        return None, ToolResult(content=f"文件不存在: {path}", is_error=True)
    if not target.is_file():
        return None, ToolResult(content=f"不是文件: {path}", is_error=True)
    if target.suffix.lower() == ".xls":
        return None, ToolResult(content="不支持旧版 .xls，请先转换为 .xlsx。", is_error=True)
    if target.suffix.lower() != ".xlsx":
        return None, ToolResult(content="spreadsheet 工具仅支持 .xlsx 文件。", is_error=True)
    return target, None


@tool(
    name="spreadsheet_read",
    description=(
        "读取 XLSX 工作簿或指定工作表/单元格区域。输出带行号的可读表格，"
        "并返回文件版本、结构 token 和内容 token。value_mode 可选择值、公式或两者。"
    ),
)
async def spreadsheet_read(
    path: str,
    sheet_name: str | None = None,
    cell_range: str | None = None,
    value_mode: Literal["values", "formulas", "both"] = "values",
) -> ToolResult:
    """读取 XLSX 工作簿。

    Args:
        path: XLSX 文件路径。
        sheet_name: 工作表名；多工作表且省略时仅列出工作表。
        cell_range: 可选 A1 区域，如 A1:D20。
        value_mode: values 返回缓存值，formulas 返回公式，both 同时返回两者。
    """
    target, error = _spreadsheet_target(path)
    if error:
        return error
    result = await file_read(
        str(target),
        sheet_name=sheet_name,
        xlsx_range=cell_range,
        render_mode=value_mode,
        row_mode="arrays",
        include_tables=True,
        include_merges=True,
    )
    result = attach_version(result, target, include_in_content=False)
    if result.is_error:
        return result

    version_lines = [f"文件版本: {result.metadata['version']}"]
    if result.metadata.get("structure_token"):
        version_lines.append(f"结构 token: {result.metadata['structure_token']}")
    if result.metadata.get("content_token"):
        version_lines.append(f"内容 token: {result.metadata['content_token']}")
    result.content = result.content.rstrip() + "\n\n" + "\n".join(version_lines)
    return result


@tool(
    name="spreadsheet_edit",
    description=(
        "结构化编辑 XLSX。仅提供四个低歧义动作：set_range、update_cells、"
        "clear_range、append_rows。默认 dry_run=true 返回预览；确认后再以 dry_run=false 落盘。"
    ),
    parameters_schema=_SPREADSHEET_EDIT_SCHEMA,
)
async def spreadsheet_edit(
    path: str,
    operation: Literal["set_range", "update_cells", "clear_range", "append_rows"],
    payload: dict[str, Any],
    sheet_name: str | None = None,
    dry_run: bool = True,
    expected_version: str | None = None,
) -> ToolResult:
    """结构化编辑 XLSX，默认只预览。"""
    target, error = _spreadsheet_target(path)
    if error:
        return error
    if operation not in {"set_range", "update_cells", "clear_range", "append_rows"}:
        return ToolResult(content=f"不支持的 spreadsheet operation: {operation}", is_error=True)
    previous_version = file_version(target)
    conflict = version_conflict(target, expected_version, current_version=previous_version)
    if conflict:
        return conflict

    result = await file_edit_table(
        str(target),
        operation=operation,
        sheet_name=sheet_name,
        payload=payload,
        dry_run=dry_run,
        allow_structure_change=(operation == "append_rows"),
    )
    return attach_version(result, target, previous_version=previous_version)
