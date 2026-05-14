"""Jinja2 渲染器。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jinja2 import Environment, BaseLoader, TemplateError

from myagent.prompt.template import PromptTemplate, Section
from myagent.prompt.variables import PromptVariables

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class PromptRenderer:
    """Jinja2 渲染引擎。"""

    def __init__(self, prompt_template: PromptTemplate):
        self._template = prompt_template
        self._settings = prompt_template.settings
        self._jinja = Environment(loader=BaseLoader(), autoescape=False)

    def render(self, variables: PromptVariables) -> str:
        """渲染完整的 system prompt。
        
        流程:
        1. 按 priority 排序 sections
        2. 逐一渲染
        3. 合并结果
        """
        sorted_sections = sorted(
            self._template.sections,
            key=lambda s: s.priority.sort_order,
        )

        variables_dict = variables.to_dict()
        rendered = []

        for section in sorted_sections:
            # 检查 enabled_when 条件
            if not self._evaluate_enabled(section, variables_dict):
                continue

            # 渲染章节
            try:
                jinja_tpl = self._jinja.from_string(section.template)
                section_text = jinja_tpl.render(**variables_dict)
            except TemplateError as e:
                logger.warning(f"Failed to render section '{section.name}': {e}")
                continue

            if not section_text.strip():
                continue

            rendered.append(section_text)

        result = "\n\n".join(rendered)
        logger.info(f"Prompt rendered: {len(result)} chars, sections={len(rendered)}")
        return result

    def _evaluate_enabled(self, section: Section, variables_dict: dict) -> bool:
        """评估 section 的 enabled_when 条件。"""
        if isinstance(section.enabled_when, bool):
            return section.enabled_when
        if isinstance(section.enabled_when, str) and section.enabled_when.strip():
            try:
                expr = section.enabled_when.strip()
                # 去掉 {{ }} 包裹（YAML 中常见写法）
                if expr.startswith("{{") and expr.endswith("}}"):
                    expr = expr[2:-2].strip()
                tpl = self._jinja.from_string(
                    "{% if " + expr + " %}True{% else %}False{% endif %}"
                )
                result = tpl.render(**variables_dict).strip()
                return result == "True"
            except TemplateError:
                return True
        return True
