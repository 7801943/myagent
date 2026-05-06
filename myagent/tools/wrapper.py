"""
工具包装器：将任意 async callable 包装为 BaseTool 实例。

支持：
1. 直接传入函数对象（FunctionTool 包装）
2. 装饰器用法（@make_tool）
3. 从代码文本动态加载（结合 loader.py 使用）

核心：所有工具来源归一为 BaseTool 实例，走统一的 ToolRegistry / ToolExecutor 流水线。
"""
from typing import Callable

from myagent.tools.base import BaseTool, ToolResult
from myagent.tools.schema import generate_schema, extract_description


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
        self.name = name or func.__name__
        self.description = description or extract_description(func) or self.name
        self.parameters_schema = generate_schema(func)

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
