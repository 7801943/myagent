"""
BaseTool 抽象基类 + ToolResult 数据类 + ToolMeta 元数据容器 + FunctionTool 包装器。

设计原则：
- BaseTool 只描述工具"是什么"（name / description / parameters_schema）
- 不包含任何 Provider 格式转换逻辑，格式转换由各 Provider 的 format_tools() 负责
- ToolMeta 元数据完全由配置文件驱动，不硬编码
- FunctionTool / make_tool 将任意 async callable 包装为 BaseTool 实例
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


@dataclass
class ToolResult:
    """工具执行结果。"""
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolMeta:
    """
    工具元数据容器。

    字段完全由配置文件驱动，不硬编码。
    读取优先级（高→低）：
      1. 运行时 merge() 动态覆盖
      2. tools_store/<tool>/meta.yaml（工具目录级）
      3. config/tool_meta.yaml 中 tools.<name> 覆盖
      4. config/tool_meta.yaml 中 defaults 全局默认
    """

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self) -> str:
        attrs = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        return f"ToolMeta({attrs})"

    def get(self, key: str, default: Any = None) -> Any:
        """安全获取属性，缺失时返回 default。"""
        return getattr(self, key, default)

    def merge(self, overrides: dict) -> "ToolMeta":
        """
        合并覆盖字段，返回新的 ToolMeta 实例。
        用于运行时动态修改元数据。
        """
        current = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        current.update(overrides)
        return ToolMeta(**current)

    def to_dict(self) -> dict:
        """导出为字典（序列化/调试用）。"""
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    # ---- 类方法：从配置文件加载 ----

    @classmethod
    def load(
        cls,
        tool_name: str,
        global_config_path: str | None = None,
        tool_meta_path: str | None = None,
    ) -> "ToolMeta":
        """
        从配置文件加载工具元数据。

        Args:
            tool_name: 工具名称（对应 BaseTool.name）
            global_config_path: 全局配置文件路径
            tool_meta_path: 工具目录下的 meta.yaml 路径（可选）

        Returns:
            合并后的 ToolMeta 实例
        """
        if global_config_path is None:
            # 基于包根目录推算默认路径
            global_config_path = str(
                Path(__file__).parent.parent.parent / "config" / "tool_meta.yaml"
            )

        # 1. 加载全局默认
        defaults: dict = {}
        tool_overrides: dict = {}

        global_path = Path(global_config_path)
        if global_path.exists():
            with open(global_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            defaults = config.get("defaults", {})
            tool_overrides = config.get("tools", {}).get(tool_name, {})

        # 2. 加载工具目录级元数据
        local_meta: dict = {}
        if tool_meta_path:
            local_path = Path(tool_meta_path)
            if local_path.exists():
                with open(local_path, encoding="utf-8") as f:
                    local_meta = yaml.safe_load(f) or {}

        # 3. 三层合并：defaults < tool_overrides < local_meta
        merged: dict = {}
        merged.update(defaults)
        merged.update(tool_overrides)
        merged.update(local_meta)

        # 确保 tool_name 字段始终存在
        merged["tool_name"] = tool_name

        return cls(**merged)

    @classmethod
    def load_for_hot_reload(
        cls,
        tool_name: str,
        tool_dir: str,
        global_config_path: str | None = None,
    ) -> "ToolMeta":
        """
        为热加载工具加载元数据的便捷方法。
        自动从 tool_dir 下查找 meta.yaml。
        """
        return cls.load(
            tool_name=tool_name,
            global_config_path=global_config_path,
            tool_meta_path=str(Path(tool_dir) / "meta.yaml"),
        )


class BaseTool(ABC):
    """
    工具抽象基类。
    子类只需声明三个类属性 + 实现 execute()，无需关心任何 Provider 格式。
    """

    name: str = ""
    description: str = ""
    parameters_schema: dict = {}
    meta: ToolMeta | None = None  # 延迟加载，首次访问时从配置文件读取

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """执行工具。"""
        ...

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # 确保子类有 name：如果未显式声明非空 name，则使用类名
        if not cls.name:
            cls.name = cls.__name__

    def _ensure_meta(self) -> ToolMeta:
        """延迟加载元数据（首次访问时从配置文件读取）。"""
        if self.meta is None:
            self.meta = ToolMeta.load(self.name)
        return self.meta


class FunctionTool(BaseTool):
    """
    通用函数工具：将任意 async callable 自动包装为 BaseTool。

    设计要点：
    - name / description / parameters_schema 全部从函数自省自动生成
    - execute() 直接委托给原始函数
    - 无需手写任何 schema
    - 如果原始函数返回 ToolResult，直接使用；否则包装为 ToolResult(content=str(result))
    """

    def __init__(
        self,
        func: Callable,
        *,
        name: str | None = None,
        description: str | None = None,
    ):
        from myagent.tools.schema import generate_schema, extract_description

        self.name = name or func.__name__
        self.description = description or extract_description(func) or self.name
        self.parameters_schema = generate_schema(func)
        # 元数据延迟加载：先尝试从配置文件读取
        self.meta = None  # _ensure_meta() 在首次需要时加载
        self._func = func
        self.__doc__ = func.__doc__

    async def execute(self, **kwargs) -> ToolResult:
        result = await self._func(**kwargs)

        if isinstance(result, ToolResult):
            return result

        return ToolResult(content=str(result))


def make_tool(
    func: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> FunctionTool | Callable:
    """
    工具创建工厂。支持装饰器用法和直接调用用法。

    用法 1 — 装饰器:
        @make_tool
        async def web_search(query: str, max_results: int = 5) -> str:
            '''搜索互联网。'''
            ...

    用法 2 — 直接包装:
        tool = make_tool(web_search, name="search", description="搜索")

    用法 3 — 带覆盖名:
        @make_tool(name="translate_text")
        async def translate(text: str, target: str = "en") -> str:
            '''翻译文本。'''
            ...
    """
    if func is not None:
        return FunctionTool(func, name=name, description=description)

    def decorator(f):
        return FunctionTool(f, name=name, description=description)

    return decorator