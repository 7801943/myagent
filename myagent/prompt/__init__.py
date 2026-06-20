"""
Structured System Prompt Template (SSPT) 模块。

提供:
  - PromptTemplate:  从 YAML 配置加载模板
  - PromptRenderer:  Jinja2 渲染引擎
  - VariableCollector: 从 Session 采集动态变量
  - SkillRegistry:    Skill 文件加载与注册
"""

from myagent.prompt.template import PromptTemplate, Section
from myagent.prompt.renderer import PromptRenderer
from myagent.prompt.variables import PromptVariables, VariableCollector
from myagent.prompt.skills import SkillContext, SkillDefinition, SkillRegistry, load_skill_from_dir

__all__ = [
    "PromptTemplate", "Section",
    "PromptRenderer",
    "PromptVariables", "VariableCollector",
    "SkillContext", "SkillDefinition", "SkillRegistry", "load_skill_from_dir",
]
