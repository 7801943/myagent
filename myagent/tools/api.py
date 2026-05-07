"""
MyAgent 工具 API 层 V3。

定义工具的核心数据结构，替代旧的 base.py + schema.py。
- ToolResult: 统一执行结果（合并 base.py:ToolResult 和 message.py:ToolResult）
- ToolMeta: 严格类型的元数据 (Pydantic，替代 setattr 模式)
- ToolLike: 最小化协议接口（兼容现有 BaseTool 子类）
- @tool: 工具声明装饰器
- generate_schema: 函数自省 → JSON Schema
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, Field


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ToolResult — 统一执行结果
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ToolResult:
    """工具执行结果。统一替代 base.py:ToolResult 和 message.py:ToolResult。"""

    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ToolMeta — 严格类型元数据
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ToolMeta(BaseModel):
    """工具元数据。Pydantic 严格类型，替代旧 setattr 模式。"""

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
    """工具的最小化协议。兼容现有 BaseTool 子类直接注册。"""

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

    用法:
        @tool(name="query_weather", timeout=15)
        async def query_weather(city: str = "Beijing") -> str:
            '''查询天气'''
            ...

    兼容性: 不加 @tool 的纯 async def 函数也会被 ToolManager 自动发现注册。
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
    """从函数签名自动生成 JSON Schema。"""
    import inspect

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
    """从 docstring 提取第一行作为简短描述。"""
    if not func.__doc__:
        return ""
    return func.__doc__.strip().split("\n")[0].strip()


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
