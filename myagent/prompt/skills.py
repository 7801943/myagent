"""Skill 系统接口定义 (预留，暂不实现)。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class SkillContext:
    """单个 Skill 的上下文信息。"""
    name: str
    description: str
    instructions: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSkill(ABC):
    """Skill 抽象基类。

    未来每种 Skill 实现此基类，例如:
      - OfficeAutomationSkill
      - WebSearchSkill
      - CodeReviewSkill
    """

    @abstractmethod
    async def get_context(self, variables: dict) -> SkillContext | None:
        """根据运行时变量判断是否激活，返回 Skill 上下文。
        
        返回 None 表示该 Skill 在当前场景下不激活。
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Skill 唯一名称。"""
        ...


class SkillRegistry:
    """Skill 注册中心。

    用法:
        registry = SkillRegistry()
        registry.register(OfficeAutomationSkill())
        registry.register(WebSearchSkill())
        
        active = await registry.activate(variables)
        # → [SkillContext("office-automation", ...), ...]
    """

    def __init__(self):
        self._skills: dict[str, BaseSkill] = {}

    def register(self, skill: BaseSkill) -> None:
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> None:
        self._skills.pop(name, None)

    async def activate(self, variables: dict) -> list[SkillContext]:
        """根据运行时变量激活合适的 Skills。"""
        results = []
        for skill in self._skills.values():
            ctx = await skill.get_context(variables)
            if ctx:
                results.append(ctx)
        return results

    @property
    def registered_names(self) -> list[str]:
        return list(self._skills.keys())