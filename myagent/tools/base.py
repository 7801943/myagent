"""
BaseTool 抽象基类 + ToolResult 数据类。
所有工具必须继承 BaseTool 并实现 execute 方法。

设计原则：
- BaseTool 只描述工具"是什么"（name / description / parameters_schema）
- 不包含任何 Provider 格式转换逻辑，格式转换由各 Provider 的 format_tools() 负责
- meta 字段供未来扩展（如超时覆盖、权限标签、重试策略、multi-modal 能力声明等）
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ToolResult:
    """工具执行结果。"""
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

class BaseTool(ABC):
    """
    工具抽象基类。
    子类只需声明三个类属性 + 实现 execute()，无需关心任何 Provider 格式。
    """
    name: str = ""
    description: str = ""
    parameters_schema: dict = {}
    meta: dict = {}  # 扩展字段：超时覆盖、权限标签、重试策略等未来需求

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """执行工具。"""
        ...

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # 确保子类有必要的类属性
        if not cls.name and not getattr(cls, 'name', None):
            cls.name = cls.__name__