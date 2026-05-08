"""
MyAgent 工具 API 层 V3。

定义工具的核心数据结构，替代旧的 base.py + schema.py。
- ToolResult: 统一执行结果（合并 base.py:ToolResult 和 message.py:ToolResult）
- ToolMeta: 严格类型的元数据 (Pydantic，替代 setattr 模式)
- ToolLike: 最小化协议接口（兼容现有 BaseTool 子类）
- @tool: 工具声明装饰器
- generate_schema: 函数自省 → JSON Schema
"""
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, Field


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ToolResult — 统一执行结果
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ToolResult:
    """
    工具执行结果封装。
    
    统一替代旧版本中的 base.py:ToolResult 和 message.py:ToolResult，
    提供一个标准化的返回结构供 Agent 和 LLM 使用。
    
    Attributes:
        content (str): 工具执行返回的文本内容，将直接喂给大模型。
        is_error (bool): 标识工具执行是否发生错误，默认为 False。
        metadata (dict): 附加的元数据信息（如执行耗时、原始数据等），不会直接展示给模型。
    """

    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ToolMeta — 严格类型元数据
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ToolMeta(BaseModel):
    """
    工具元数据配置。
    
    使用 Pydantic 实现严格的类型检查和默认值管理，
    替代旧版本中直接使用 getattr/setattr 注入元数据的模式。
    
    Attributes:
        category (str): 工具的分类，默认为 "custom"。
        permission (str): 工具需要的权限级别，默认为 "standard"。
        source (str): 工具的来源，例如 "local" 或 "mcp"。
        timeout (float): 工具执行的超时时间（秒），默认 30.0 秒。
        requires_network (bool): 是否需要网络连接。
        requires_sandbox (bool): 是否需要在安全沙箱中运行。
        extra (dict): 其他动态扩展配置项。
    """

    category: str = "custom"
    permission: str = "standard"
    source: str = "local"
    timeout: float = 30.0
    requires_network: bool = False
    requires_sandbox: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default) if hasattr(self, key) else default

    def merge(self, overrides: dict[str, Any]) -> "ToolMeta":
        """合并覆盖字段，返回新实例。"""
        data = self.model_dump()
        data.update(overrides)
        return ToolMeta(**data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ToolLike — 最小化协议
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@runtime_checkable
class ToolLike(Protocol):
    """
    工具的最小化协议 (Protocol)。
    
    基于“鸭子类型”设计，只要对象实现了这里定义的属性和方法，
    就可以作为工具直接被调度系统注册和使用，无需显式继承 BaseTool。
    这极大提高了框架的灵活性，使得普通的 async 函数或第三方类可以轻松接入。
    
    Attributes:
        name (str): 工具的唯一标识名称（建议仅包含字母、数字、下划线）。
        description (str): 工具的详细描述，大模型严重依赖此描述判断何时及如何调用。
        parameters_schema (dict): 符合 JSON Schema 规范的参数定义（对应 MCP 的 inputSchema）。
        meta (ToolMeta | None): 附加的工具配置元数据。
    """

    name: str
    description: str
    parameters_schema: dict
    meta: ToolMeta | None

    async def execute(self, **kwargs) -> ToolResult: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# @tool — 工具声明装饰器
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def tool(
    name: str | None = None,
    description: str | None = None,
    timeout: float | None = None,
    permission: str | None = None,
) -> Callable:
    """
    工具声明装饰器。

    用于将普通的 async 函数快速包装并打上自定义的工具标签。
    配合 generate_schema，可以将纯函数无缝转变为符合协议的工具。

    Args:
        name: 可选，覆盖函数原本的名称作为工具名。
        description: 可选，覆盖函数原本的 docstring 作为工具描述。
        timeout: 可选，设置该工具的执行超时时间。
        permission: 可选，设置该工具的权限级别。

    用法:
        @tool(name="query_weather", timeout=15)
        async def query_weather(city: str = "Beijing") -> str:
            '''查询指定城市的天气情况'''
            ...

    兼容性: 即使不显式添加 @tool 装饰器，符合规范的纯 async def 函数
    也会被 ToolManager 等管理系统自动发现并注册为工具。
    """

    def decorator(func: Callable) -> Callable:
        meta_overrides: dict[str, Any] = {}
        if name is not None:
            meta_overrides["name"] = name
        if description is not None:
            meta_overrides["description"] = description
        if timeout is not None:
            meta_overrides["timeout"] = timeout
        if permission is not None:
            meta_overrides["permission"] = permission

        func._tool_meta = meta_overrides
        return func

    return decorator


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Schema 生成 — 函数自省 → JSON Schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def generate_schema(func: Callable) -> dict:
    """
    从函数签名自动生成 JSON Schema。
    
    通过自省 (introspect) 传入函数的参数列表、类型注解和默认值，
    自动构建出大模型 API（或 MCP 协议等）所需的参数描述字典。
    
    Args:
        func (Callable): 需要提取 Schema 的目标函数。
        
    Returns:
        dict: 符合 JSON Schema 规范的字典对象。
    """
    sig = inspect.signature(func)
    hints = _safe_get_type_hints(func) or {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        py_type = hints.get(pname)
        prop = _py_type_to_property(py_type)

        if param.default is inspect.Parameter.empty:
            required.append(pname)
        else:
            prop["default"] = param.default

        properties[pname] = prop

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def extract_description(func: Callable) -> str:
    """从 docstring 提取描述"""
    if not func.__doc__:
        return ""
    # return func.__doc__.strip().split("\n")[0].strip()
    # 使用 inspect.cleandoc 可以自动处理掉代码缩进带来的多余空格，并保留所有行
    return inspect.cleandoc(func.__doc__)


def _safe_get_type_hints(func: Callable) -> dict | None:
    """安全获取类型注解。"""
    from typing import get_type_hints

    try:
        if hasattr(func, "__module__") and func.__module__ is not None:
            return get_type_hints(func)
        return getattr(func, "__annotations__", None)
    except Exception:
        return getattr(func, "__annotations__", None)


def _py_type_to_property(py_type: type | None) -> dict:
    """Python 类型 → JSON Schema property。"""
    if py_type is None:
        return {"type": "string"}

    if py_type in _TYPE_MAP:
        return {"type": _TYPE_MAP[py_type]}

    # str 类型注解（如 "int", "str" 等）
    if isinstance(py_type, str):
        name_map = {"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}
        resolved = name_map.get(py_type)
        if resolved:
            return {"type": _TYPE_MAP[resolved]}
        return {"type": "string"}

    # Optional[T] / Union[T, None]
    origin = getattr(py_type, "__origin__", None)
    if origin is not None:
        from typing import get_args

        args = get_args(py_type)
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _py_type_to_property(non_none[0])

    return {"type": "string"}
