"""Prompt 模板数据模型。"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class SectionPriority(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def sort_order(self) -> int:
        return {"high": 0, "medium": 1, "low": 2}[self.value]


class Section(BaseModel):
    """单个章节定义。"""
    name: str
    priority: SectionPriority = SectionPriority.MEDIUM
    enabled_when: str | bool = True    # Jinja2 表达式或布尔值
    template: str                       # Jinja2 模板内容


class TemplateSettings(BaseModel):
    """全局模板设置。"""
    engine: str = "jinja2"
    date_format: str = "%Y年%m月%d日 %A %H时%M分%S秒"


class PromptTemplate(BaseModel):
    """完整的提示词模板配置。"""
    version: str
    settings: TemplateSettings = Field(default_factory=TemplateSettings)
    sections: list[Section] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PromptTemplate":
        """从 YAML 文件加载模板配置。"""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**data)

    @classmethod
    def default(cls) -> "PromptTemplate":
        """返回硬编码的默认模板（当 YAML 文件缺失时降级）。"""
        return cls(
            version="2.0",
            settings=TemplateSettings(),
            sections=[
                Section(
                    name="identity",
                    priority=SectionPriority.HIGH,
                    enabled_when=True,
                    template="你是一个智能助手，可以帮助用户完成各种任务。\n"
                             "当前时间: {{ current_datetime }}",
                ),
                Section(
                    name="workspace",
                    priority=SectionPriority.MEDIUM,
                    enabled_when="{{ workspace_root | length > 0 }}",
                    template=(
                        "当前工作目录: {{ workspace_root }}\n"
                        "{% if active_file %}活跃文件: {{ active_file }}{% endif %}\n"
                        "{{ workspace_files }}"
                    ),
                ),
                Section(
                    name="client_state",
                    priority=SectionPriority.MEDIUM,
                    enabled_when="{{ client_state.model or client_state.tools }}",
                    template=(
                        "{% if client_state.model %}前端模型选择状态: {{ client_state.model }}{% endif %}\n"
                        "{% if client_state.tools %}前端工具选择状态: {{ client_state.tools }}{% endif %}"
                    ),
                ),
                Section(
                    name="skills",
                    priority=SectionPriority.MEDIUM,
                    enabled_when="{{ available_skills | length > 0 or active_skills | length > 0 }}",
                    template=(
                        "<skills>\n"
                        "{% if available_skills %}\n"
                        "你可以使用以下专项能力（Skill）处理特定任务。\n"
                        "当用户请求匹配某个 Skill 时，请调用 use_skill 工具加载详细指令。\n"
                        "可用 Skill 目录:\n"
                        "{% for skill in available_skills %}\n"
                        "- {{ skill.name }}: {{ skill.description }}\n"
                        "{% endfor %}\n"
                        "{% endif %}\n"
                        "{% if active_skills %}\n"
                        "已激活的 Skill 指令:\n"
                        "{% for skill in active_skills %}\n"
                        "=== {{ skill.name }} ===\n"
                        "{{ skill.instructions }}\n"
                        "{% endfor %}\n"
                        "{% endif %}\n"
                        "</skills>"
                    ),
                ),
            ],
        )
