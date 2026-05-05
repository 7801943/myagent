"""
Schema 生成器：从 Python 函数自省自动生成 JSON Schema。
纯标准库实现，不依赖第三方包。

原理链路：
   inspect.signature()  → 参数列表 + 默认值
   typing.get_type_hints() → 类型注解
   __doc__ → 描述 + 参数说明（正则解析）
   → 组装为标准 JSON Schema（与 MCP inputSchema / OpenAI parameters 一致）
"""
import inspect
import re
from typing import get_type_hints, get_origin, get_args, Union, Optional

# ── 类型映射：Python 基础类型 → JSON Schema type ──
_TYPE_MAP: dict = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}

# 反向映射表（名称 → Python 类型）
_TYPE_NAME_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}


def generate_schema(func, *, include_descriptions: bool = True) -> dict:
    """
    从函数签名自动生成 JSON Schema。

    Args:
        func: 可调用对象（函数/方法）
        include_descriptions: 是否从 docstring 提取参数描述

    Returns:
        标准的 JSON Schema dict，可直接用作:
        - BaseTool.parameters_schema
        - MCP inputSchema
        - OpenAI function calling parameters
    """
    sig = inspect.signature(func)
    hints = _safe_get_type_hints(func) or {}

    arg_docs: dict[str, str] = {}
    if include_descriptions and func.__doc__:
        arg_docs = _parse_docstring_args(func.__doc__)

    properties: dict = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        py_type = hints.get(name)
        prop = _py_type_to_property(py_type)

        if name in arg_docs:
            prop["description"] = arg_docs[name]

        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            prop["default"] = param.default

        properties[name] = prop

    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    return schema


def extract_description(func) -> str:
    """从 docstring 提取第一行作为简短描述。"""
    if not func.__doc__:
        return ""
    lines = func.__doc__.strip().split("\n")
    return lines[0].strip()


# ── 内部辅助函数 ──

def _safe_get_type_hints(func) -> dict | None:
    """安全获取类型注解（函数可能是动态 exec 生成的，get_type_hints 可能失败）。"""
    try:
        module_attr = getattr(func, "__module__", None)
        if module_attr is not None:
            return get_type_hints(func)
        return getattr(func, "__annotations__", None)
    except Exception:
        return getattr(func, "__annotations__", None)


def _py_type_to_property(py_type) -> dict:
    """将 Python 类型转换为 JSON Schema property 定义。"""
    if py_type is None:
        return {"type": "string"}

    origin = get_origin(py_type)
    args = get_args(py_type)

    if origin is Union or (origin is None and py_type is Optional):
        non_none = [a for a in args if a is not type(None)] if args else [py_type]
        if non_none:
            return _py_type_to_property(non_none[0])
        return {"type": "string"}

    if py_type in _TYPE_MAP:
        return {"type": _TYPE_MAP[py_type]}

    if isinstance(py_type, type) and hasattr(py_type, "__origin__"):
        return {"type": "string"}

    if isinstance(py_type, str):
        resolved = _TYPE_NAME_MAP.get(py_type)
        if resolved:
            return {"type": _TYPE_MAP[resolved]}
        return {"type": "string"}

    return {"type": "string"}


def _parse_docstring_args(doc: str) -> dict[str, str]:
    """从 docstring 解析 Args: 段落，提取参数描述（支持 Google 和 Sphinx 风格）。"""
    if not doc:
        return {}

    patterns = [
        re.compile(r"Args:\s*\n((?:\s+\w+.*\n?)+)", re.IGNORECASE),
        re.compile(r"Parameters:\s*\n((?:\s+\w+.*\n?)+)", re.IGNORECASE),
    ]

    for pat in patterns:
        match = pat.search(doc)
        if match:
            result: dict[str, str] = {}
            for line in match.group(1).strip().split("\n"):
                m = re.match(r"\s*(\w+)(?:\s*\([^)]+\))?\s*:\s*(.*)", line)
                if m:
                    result[m.group(1)] = m.group(2).strip()
            if result:
                return result

    return {}
