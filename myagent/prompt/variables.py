"""提示词变量定义与采集器。"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from myagent.core.session import Session


def _get_weekday_cn(dt: datetime) -> str:
    """获取中文星期名称。"""
    days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return days[dt.weekday()]


def _summarize_parameters(schema: dict) -> str:
    """将 JSON Schema 简化为可读的参数摘要。
    
    Example input: {"type":"object","properties":{"city":{"type":"string","description":"城市名"}}}
    Example output: "city(string, [必填]): 城市名"
    """
    if not schema or not isinstance(schema, dict):
        return "(无参数)"
    props = schema.get("properties", {})
    if not props:
        return "(无参数)"
    required = schema.get("required", [])
    parts = []
    for name, info in props.items():
        if not isinstance(info, dict):
            continue
        ptype = info.get("type", "any")
        desc = info.get("description", "")
        is_req = "[必填]" if name in required else "[可选]"
        if desc:
            parts.append(f"{name}({ptype}, {is_req}): {desc}")
        else:
            parts.append(f"{name}({ptype}, {is_req})")
    return "; ".join(parts)


@dataclass
class PromptVariables:
    """系统提示词渲染所需的全部动态变量。"""

    # ── 用户信息 ──
    user_info: dict = field(default_factory=lambda: {
        "name": "", "group": "", "role": ""
    })

    # ── 时间 ──
    current_datetime: str = ""

    # ── 工作空间 ──
    workspace_root: str = ""
    workspace_files: str = ""
    open_files: list[dict] = field(default_factory=list)
    active_file: str = ""

    # ── 前端运行态 ──
    client_state: dict = field(default_factory=dict)

    # ── 工具 ──
    available_tools: list[dict] = field(default_factory=list)

    # ── 模型 ──
    active_model: dict = field(default_factory=lambda: {
        "model_id": "", "provider_type": "", "context_window_size": 200000
    })

    # ── Token ──
    token_usage: dict = field(default_factory=lambda: {
        "used": 0, "total": 200000, "percentage": 0.0, "remaining": 200000
    })

    # ── 平台 ──
    platform_info: dict = field(default_factory=lambda: {
        "os": "", "arch": "", "hostname": "", "python_version": ""
    })

    # ── 安全 ──
    safety_policy: str = ""

    # ── Skills ──
    skills: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转换为 Jinja2 渲染所需的 dict。"""
        return asdict(self)


# 平台信息缓存（静态数据，首次采集后不变）
_platform_cache: dict = {}


def _get_platform_info() -> dict:
    """获取平台信息（带缓存）。"""
    global _platform_cache
    if _platform_cache:
        return _platform_cache
    _platform_cache = {
        "os": platform.system(),
        "arch": platform.machine(),
        "hostname": platform.node(),
        "python_version": sys.version.split()[0],
    }
    return _platform_cache


def _get_safety_summary(session: "Session") -> str:
    """提取当前会话的 CLI 安全策略摘要。"""
    try:
        safety = session.data.safety
        return (
            f"CLI安全策略: {safety.active_policy} ({safety.mode})；"
            "file_write、file_edit、file_edit_table 永久拒绝"
        )
    except Exception:
        return ""


class VariableCollector:
    """从 Session 采集 PromptVariables。

    每轮 chat() 调用一次，确保获取最新运行时状态。
    """

    @staticmethod
    async def collect(session: "Session") -> PromptVariables:
        now = datetime.now()
        current_datetime = (
            f"{now.year}年{now.month}月{now.day}日 "
            f"{_get_weekday_cn(now)} "
            f"{now.hour:02d}时{now.minute:02d}分{now.second:02d}秒"
        )

        # ── 用户信息 ──
        # [Pydantic 迁移] user 已无 "info" 字段，旧代码始终走默认分支
        user_info = {"name": "未设置", "group": "未设置", "role": "未设置"}

        # ── 工具 ──
        tools = session.data.tool.tools
        available_tools = [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters_summary": _summarize_parameters(
                    t.get("parameters_schema", {}) if isinstance(t, dict) else {}
                ),
                "source": t.get("source", "unknown"),
                "category": t.get("category", ""),
            }
            for t in tools
        ]

        # ── 工作空间 ──
        workspace_root = ""
        workspace_files = ""
        open_files = []
        active_file = ""
        if session.workspace:
            workspace_root = session.workspace.root_path
            workspace_files = session.workspace.get_file_list_text()
            open_files = [
                {
                    "path": f.path,
                    "is_dirty": f.is_dirty,
                    "cursor_line": f.cursor_line,
                    "cursor_column": f.cursor_column,
                }
                for f in session.workspace.state.open_files
            ]
            active_file = session.workspace.get_active_file_path() or ""

        # ── 前端运行态 ──
        client_state = session.data.client_state.model_dump()

        # ── 模型 ──
        active_model = dict(session.data.model.active)
        if not active_model.get("model_id"):
            active_model = {"model_id": "unknown", "provider_type": "unknown", "context_window_size": 200000}

        # ── Token ──
        # [Pydantic 迁移] token_usage 现在是 TokenUsage 对象，直接属性访问 + 计算字段
        tu = session.data.context.token_usage
        token_usage = {
            "used": tu.used,
            "total": tu.total,
            "percentage": tu.percentage,
            "remaining": tu.remaining,
        }

        return PromptVariables(
            user_info=user_info,
            current_datetime=current_datetime,
            workspace_root=workspace_root,
            workspace_files=workspace_files,
            open_files=open_files,
            active_file=active_file,
            client_state=client_state,
            available_tools=available_tools,
            active_model=active_model,
            token_usage=token_usage,
            platform_info=_get_platform_info(),
            safety_policy=_get_safety_summary(session),
            skills=[],  # 预留
        )