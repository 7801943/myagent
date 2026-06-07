"""
MyAgent 工具 API 层 V3。

定义工具的核心数据结构，替代旧的 base.py + schema.py。
- ToolResult: 统一执行结果（合并 base.py:ToolResult 和 message.py:ToolResult）
- ToolMeta: 严格类型的元数据 (Pydantic，替代 setattr 模式)
- ToolLike: 最小化协议接口（兼容现有 BaseTool 子类）
- @tool: 工具声明装饰器
- generate_schema: 函数自省 → JSON Schema
"""
import enum
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
        content_blocks (list | None): 多模态内容块列表，如图片 base64 等。
            每个元素为 dict: {"type": "image_base64", "data": "...", "media_type": "image/png"}
            当此字段非空时，下游会将 content 和 content_blocks 一起组装为多模态消息。
    """

    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    content_blocks: list[dict[str, Any]] | None = None


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
    
    同时解析 docstring 中 Google-style 的 Args: 段落，将参数描述
    注入到每个 property 的 "description" 字段，帮助 LLM 正确理解参数含义。
    
    Args:
        func (Callable): 需要提取 Schema 的目标函数。
        
    Returns:
        dict: 符合 JSON Schema 规范的字典对象。
    """
    sig = inspect.signature(func)
    hints = _safe_get_type_hints(func) or {}

    # 从 docstring 提取参数描述
    arg_descriptions = _parse_args_from_docstring(func)

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

        # 注入 docstring 中的参数描述
        if pname in arg_descriptions:
            prop["description"] = arg_descriptions[pname]

        properties[pname] = prop

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _parse_args_from_docstring(func: Callable) -> dict[str, str]:
    """
    从函数 docstring 中解析 Google-style Args: 段落，提取参数描述。
    
    支持的格式:
        Args:
            param_name: 描述文本
            param_name (type): 描述文本
            param_name: 多行描述文本
                续行缩进
    
    Returns:
        dict[str, str]: 参数名 → 描述文本 的映射
    """
    doc = func.__doc__
    if not doc:
        return {}

    # 使用 inspect.cleandoc 统一缩进
    cleaned = inspect.cleandoc(doc)
    lines = cleaned.split("\n")

    # 找到 Args: 段落的起始位置
    args_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "Args:" or stripped == "Arguments:":
            args_start = i + 1
            break

    if args_start is None:
        return {}

    # 找到 Args: 段落的结束位置（遇到下一个顶级段落关键字）
    section_keywords = {
        "Returns:", "Return:", "Raises:", "Raise:", "Yields:", "Yield:",
        "Examples:", "Example:", "Note:", "Notes:", "Warning:", "Warnings:",
        "See Also:", "References:", "Todo:", "Attributes:", "用法:", "返回:",
        "示例:", "注意:", "警告:",
    }
    args_end = len(lines)
    for i in range(args_start, len(lines)):
        stripped = lines[i].strip()
        # 检查是否是新段落的开始（非空行、不缩进、以冒号结尾或匹配关键字）
        if stripped and not lines[i].startswith(" ") and (
            stripped in section_keywords or (stripped.endswith(":") and not stripped.startswith("-"))
        ):
            args_end = i
            break

    # 解析每个参数
    result: dict[str, str] = {}
    current_param: str | None = None
    current_desc: list[str] = []

    for i in range(args_start, args_end):
        line = lines[i]
        stripped = line.strip()

        # 空行可能是参数之间的分隔
        if not stripped:
            if current_param:
                current_desc.append("")
            continue

        # 检查是否是新参数行（顶格或缩进较少，且以 "name:" 或 "name (type):" 开头）
        # Google-style: 参数行通常为 "    param_name: description" 或 "    param_name (type): description"
        is_new_param = False
        if line and not line[0].isspace():
            # 顶格的行，可能是新参数（在 cleandoc 之后缩进被保留）
            is_new_param = True
        elif stripped and ":" in stripped:
            # 检查是否匹配 "param_name:" 或 "param_name (type):" 的模式
            import re
            if re.match(r'^(\w+)\s*(\([^)]*\))?\s*:', stripped):
                is_new_param = True

        if is_new_param:
            # 保存上一个参数
            if current_param:
                desc_text = " ".join(current_desc).strip()
                # 清理多余空白
                desc_text = " ".join(desc_text.split())
                if desc_text:
                    result[current_param] = desc_text

            # 解析新参数名和描述
            import re
            match = re.match(r'^(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)', stripped)
            if match:
                current_param = match.group(1)
                desc_part = match.group(2).strip()
                current_desc = [desc_part] if desc_part else []
            else:
                current_param = None
                current_desc = []
        else:
            # 续行，追加到当前参数描述
            if current_param:
                current_desc.append(stripped)

    # 保存最后一个参数
    if current_param:
        desc_text = " ".join(current_desc).strip()
        desc_text = " ".join(desc_text.split())
        if desc_text:
            result[current_param] = desc_text

    return result


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
    """
    Python 类型 → JSON Schema property。

    [FIX] 扩展支持以下复合类型：
    - Literal["a", "b"] → {"enum": ["a", "b"]}
    - Enum 子类 → {"type": "string", "enum": [e.value, ...]}
    - list[str] → {"type": "array", "items": {"type": "string"}}
    - dict[str, int] → {"type": "object", "additionalProperties": {"type": "integer"}}
    """
    if py_type is None:
        return {"type": "string"}

    # ── 基本类型直接映射 ──
    if py_type in _TYPE_MAP:
        return {"type": _TYPE_MAP[py_type]}

    # ── Enum 子类 → {"type": "string", "enum": [...]} ──
    if isinstance(py_type, type) and issubclass(py_type, enum.Enum):
        enum_values = [e.value for e in py_type]
        # 推断 value 的 JSON 类型
        if enum_values and isinstance(enum_values[0], int):
            return {"type": "integer", "enum": enum_values}
        return {"type": "string", "enum": enum_values}

    # ── 字符串形式的类型注解（前向引用） ──
    if isinstance(py_type, str):
        name_map = {"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}
        resolved = name_map.get(py_type)
        if resolved:
            return {"type": _TYPE_MAP[resolved]}
        return {"type": "string"}

    # ── 泛型origin处理：list[str], dict[str, T], Optional[T], Literal 等 ──
    origin = getattr(py_type, "__origin__", None)
    if origin is not None:
        from typing import get_args

        args = get_args(py_type)

        # Literal["a", "b", 3] → {"enum": ["a", "b", 3]}
        # typing.Literal 的 origin 在 3.8+ 是 Literal 本身（非 types.UnionType）
        # 需要检查 origin 是否为 Literal
        try:
            from typing import Literal as _Literal
            if origin is _Literal:
                return {"enum": list(args)}
        except ImportError:
            pass

        # list[T] → {"type": "array", "items": {...}}
        if origin is list:
            if args:
                item_prop = _py_type_to_property(args[0])
                return {"type": "array", "items": item_prop}
            return {"type": "array"}

        # dict[K, V] → {"type": "object", "additionalProperties": {...}}
        if origin is dict:
            if len(args) >= 2:
                val_prop = _py_type_to_property(args[1])
                return {"type": "object", "additionalProperties": val_prop}
            return {"type": "object"}

        # tuple[T, ...] → {"type": "array", "items": {...}}
        if origin is tuple:
            if args:
                # 不定长 tuple[T, ...] 只有一个元素类型
                if len(args) == 2 and args[1] is Ellipsis:
                    return {"type": "array", "items": _py_type_to_property(args[0])}
                # 定长 tuple[T1, T2, ...] → prefixItems
                return {
                    "type": "array",
                    "prefixItems": [_py_type_to_property(a) for a in args],
                    "minItems": len(args),
                    "maxItems": len(args),
                }
            return {"type": "array"}

        # Optional[T] / Union[T, None] / T | None → 递归处理非 None 分支
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            # 如果有多个非 None 类型（纯 Union），用 anyOf
            if len(non_none) > 1:
                return {"anyOf": [_py_type_to_property(a) for a in non_none]}
            return _py_type_to_property(non_none[0])

    return {"type": "string"}
