# MyAgent 自研 MCP Tool 兼容方案

## 一、核心理念

> **一切工具，无论来源（本地函数 / 动态代码文本 / MCP Server），最终都归一为 `BaseTool {name, description, parameters_schema}`，走统一的注册、安全、执行流水线。**

Python 自省生成的 JSON Schema = MCP `inputSchema` = OpenAI `parameters`，三者格式完全一致，零转换。

---

## 二、三层工具来源架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                     工具来源（三通道）                                 │
│                                                                     │
│  通道 A: 本地 Python 函数        通道 B: 动态代码文本                  │
│  ┌─────────────────────┐       ┌─────────────────────┐              │
│  │ # my_tools.py       │       │ tool_code = """      │              │
│  │ async def web_search│       │ async def translate( │              │
│  │   (query: str,      │       │   text: str,         │              │
│  │    max: int = 5     │       │   target: str = "en" │              │
│  │   ) -> ToolResult:  │       │ ) -> ToolResult:     │              │
│  │     """搜索网络"""   │       │     """翻译文本"""    │              │
│  │     ...             │       │     ...              │              │
│  └────────┬────────────┘       └────────┬────────────┘              │
│           │                             │                           │
│           │  inspect.signature()        │  exec(code, namespace)    │
│           │  get_type_hints()           │  → 得到 function object   │
│           │  __doc__                    │  → 同左的自省链路          │
│           │         ↘                   ↙                           │
│           │     make_tool_from_function()                            │
│           │         │                                               │
│           │         ▼                                               │
│           │   BaseTool { name, description, parameters_schema }     │
│           │                                                         │
│  通道 C: MCP Server (远景)                                           │
│  ┌─────────────────────┐                                            │
│  │ MCPClientManager    │                                            │
│  │  → list_tools()     │                                            │
│  │  → 获得 inputSchema │ ← 与自人生成的 schema 完全一致              │
│  │  → 包装为 MCPTool   │                                            │
│  └────────┬────────────┘                                            │
│           │                                                         │
│           ▼                                                         │
│  ┌──────────────────────────────────────────────┐                   │
│  │           ToolRegistry (统一注册表)            │                   │
│  │                                               │                   │
│  │  cli_execute   (内置)                         │                   │
│  │  file_read     (内置)                         │                   │
│  │  file_write    (内置)                         │                   │
│  │  web_search    (通道A: 本地函数)               │                   │
│  │  translate     (通道B: 动态加载)               │                   │
│  │  mcp_weather   (通道C: MCP远程)               │                   │
│  └──────────────────┬───────────────────────────┘                   │
│                      │                                              │
│                      ▼                                              │
│              ToolExecutor → SafetyGuard → Execute                   │
│                      │                                              │
│                      ▼                                              │
│                 AgentLoop (动态获取 schemas)                         │
│                      │                                              │
│                      ▼                                              │
│              ProviderRouter → LLM (OpenAI / Anthropic)              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 三、Python 自省原理链路

### 3.1 函数对象的运行时元数据

Python 函数本质上是对象，解释器在**编译期**就把元信息绑定到函数对象上：

```python
def search_files(path: str, regex: str, file_pattern: str = "*", max_results: int = 10) -> list[str]:
    """搜索文件内容。
    
    Args:
        path: 搜索目录路径
        regex: 正则表达式模式
        file_pattern: 文件名匹配模式
        max_results: 最大返回结果数
    """
    pass

# ① __name__ — 函数名（编译期绑定）
search_files.__name__        # "search_files"

# ② __doc__ — 文档字符串（编译期绑定，函数体第一个字符串字面量）
search_files.__doc__         # "搜索文件内容。\n\nArgs:\n    ..."

# ③ __annotations__ — 类型注解（编译期绑定，Python 3.0+）
search_files.__annotations__  
# {'path': <class 'str'>, 'regex': <class 'str'>, 
#  'file_pattern': <class 'str'>, 'max_results': <class 'int'>, 
#  'return': list[str]}

# ④ __defaults__ — 位置参数默认值（编译期绑定，从右往左对应）
search_files.__defaults__    # ('*', 10)

# ⑤ __kwdefaults__ — keyword-only 参数默认值
```

这些都是**免费的**，不需要任何额外库，任何 Python 函数对象都自带。

### 3.2 `inspect` 模块 — 把原始元数据结构化

```python
import inspect

sig = inspect.signature(search_files)
# Signature(path: str, regex: str, file_pattern: str = '*', max_results: int = 10) -> list[str]

for name, param in sig.parameters.items():
    print(f"  {name}: annotation={param.annotation}, default={param.default}, kind={param.kind}")

# 输出：
#   path:        annotation=<class 'str'>, default=<Parameter.empty>, kind=POSITIONAL_OR_KEYWORD
#   regex:       annotation=<class 'str'>, default=<Parameter.empty>, kind=POSITIONAL_OR_KEYWORD
#   file_pattern: annotation=<class 'str'>, default='*',               kind=POSITIONAL_OR_KEYWORD
#   max_results: annotation=<class 'int'>, default=10,                 kind=POSITIONAL_OR_KEYWORD
```

**关键判断**：`param.default is inspect.Parameter.empty` → 该参数是必填的。

### 3.3 Python 类型 → JSON Schema 映射

```python
_TYPE_MAP = {
    str:    "string",
    int:    "integer",
    float:  "number",
    bool:   "boolean",
    list:   "array",
    dict:   "object",
}
```

生成结果示例：

```json
{
    "type": "object",
    "properties": {
        "path":         {"type": "string", "description": "搜索目录路径"},
        "regex":        {"type": "string", "description": "正则表达式模式"},
        "file_pattern": {"type": "string", "default": "*", "description": "文件名匹配模式"},
        "max_results":  {"type": "integer", "default": 10, "description": "最大返回结果数"}
    },
    "required": ["path", "regex"]
}
```

**这正好是 MCP `inputSchema`、OpenAI `parameters` 的标准格式。**

### 3.4 Schema 一致性对比

```
MCP Tool Schema                     Python 自省 Schema
┌─────────────────────┐             ┌─────────────────────┐
│ name                │             │                     │
│ description         │             │                     │
│ inputSchema:        │             │  ← 这部分完全一致 →  │
│   {                 │             │  {                   │
│     type: "object"  │   ══════    │    type: "object"   │
│     properties: {}  │   一致      │    properties: {}   │
│     required: []    │             │    required: []     │
│   }                 │             │  }                   │
└─────────────────────┘             └─────────────────────┘

OpenAI Function Calling:
┌─────────────────────────────┐
│ type: "function"            │
│ function:                   │
│   name: ...                 │
│   description: ...          │
│   parameters: {JSON Schema} │  ← 同样完全一致
└─────────────────────────────┘
```

### 3.5 动态加载 — 从文本到可调用对象

```python
code_text = '''
async def web_search(query: str, max_results: int = 5) -> str:
    """搜索互联网。
    
    Args:
        query: 搜索关键词
        max_results: 最大返回结果数
    """
    return f"搜索 '{query}' 的前 {max_results} 条结果"
'''

# exec 在给定命名空间中执行代码
namespace = {}
exec(code_text, namespace)

# 函数就在 namespace 里了，完全正常的 Python 函数
func = namespace["web_search"]

# 所有自省属性完整保留
func.__name__              # "web_search"
func.__doc__               # 完整 docstring
inspect.signature(func)    # (query: str, max_results: int = 5) -> str
```

**核心原理**：Python 是解释型语言，`exec()` 让解释器在运行时编译和执行代码。编译后的函数对象和普通函数没有任何区别。

---

## 四、新增/修改文件清单

| 文件 | 操作 | 职责 |
|------|------|------|
| `myagent/tools/schema.py` | **新建** | Python 自省 → JSON Schema 生成器 |
| `myagent/tools/wrapper.py` | **新建** | `make_tool()` / `FunctionTool` — 函数→BaseTool 包装器 |
| `myagent/tools/loader.py` | **新建** | 工具加载器：本地模块扫描 + 动态代码 exec + 配置驱动 |
| `myagent/tools/mcp/` | **新建目录（远景）** | MCP Client 集成 |
| `myagent/tools/base.py` | 微调 | `__init_subclass__` 自动生成 schema（未手写时） |
| `myagent/core/session.py` | 改 1 行 | `_tool_schemas` → 动态属性 |
| `myagent/core/loop.py` | 小改 | AgentLoop 动态获取 schemas |
| `myagent/core/agent.py` | 扩展 | `add_tool()` 支持热加载通知 |
| `myagent/factory.py` | 扩展 | `_build_tool_registry()` 支持三通道加载 |
| `config.yaml.example` | 扩展 | 新增 `tools.extra` 和 `mcp` 配置段 |

---

## 五、核心模块设计

### 5.1 `myagent/tools/schema.py` — Schema 生成器

```python
"""
从 Python 函数自动生成 JSON Schema。
纯标准库实现，不依赖第三方包。

原理链路：
  inspect.signature() → 参数列表 + 默认值
  typing.get_type_hints() → 类型注解
  __doc__ → 描述 + 参数说明（正则解析）
  → 组装为标准 JSON Schema（与 MCP inputSchema / OpenAI parameters 一致）
"""
import inspect
import re
from typing import get_type_hints

# Python type → JSON Schema type 映射
_TYPE_MAP = {
    str:    "string",
    int:    "integer",
    float:  "number",
    bool:   "boolean",
    list:   "array",
    dict:   "object",
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
        - OpenAI parameters
    """
    sig = inspect.signature(func)
    hints = get_type_hints(func) if hasattr(func, '__module__') else {}
    
    # 从 docstring 提取参数描述
    arg_docs = {}
    if include_descriptions and func.__doc__:
        arg_docs = _parse_docstring_args(func.__doc__)
    
    properties = {}
    required = []
    
    for name, param in sig.parameters.items():
        # 跳过 self/cls 和 *args/**kwargs
        if name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, 
                          inspect.Parameter.VAR_KEYWORD):
            continue
        
        # 类型映射
        py_type = hints.get(name)
        if py_type:
            json_type = _TYPE_MAP.get(py_type, "string")
        else:
            json_type = "string"  # 无注解默认 string
        
        prop = {"type": json_type}
        
        # 描述
        if name in arg_docs:
            prop["description"] = arg_docs[name]
        
        # 默认值
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            prop["default"] = param.default
        
        properties[name] = prop
    
    schema = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    
    return schema

def extract_description(func) -> str:
    """从 docstring 提取简短描述（第一行非空文本）"""
    if not func.__doc__:
        return ""
    lines = func.__doc__.strip().split('\n')
    return lines[0].strip()

def _parse_docstring_args(doc: str) -> dict[str, str]:
    """从 docstring 中解析 Args: 段落（支持 Google 风格）"""
    if not doc:
        return {}
    match = re.search(r'Args:\s*\n((?:\s+\w+.*\n?)+)', doc)
    if not match:
        return {}
    result = {}
    for line in match.group(1).strip().split('\n'):
        m = re.match(r'\s+(\w+):\s*(.*)', line)
        if m:
            result[m.group(1)] = m.group(2).strip()
    return result
```

### 5.2 `myagent/tools/wrapper.py` — 函数→BaseTool 包装器

```python
"""
将任意 async 函数包装为 BaseTool 实例。
支持：
1. 直接传入函数对象
2. 从 Python 模块路径加载（如 "my_package.tools:web_search"）
3. 从代码文本动态加载（exec）
"""
from typing import Callable
from myagent.tools.base import BaseTool, ToolResult
from myagent.tools.schema import generate_schema, extract_description

class FunctionTool(BaseTool):
    """
    通用函数工具：将任意 async callable 包装为 BaseTool。
    
    设计要点：
    - name/description/parameters_schema 全部从函数自省自动生成
    - execute() 直接委托给原始函数
    - 无需手写任何 schema
    """
    
    def __init__(
        self,
        func: Callable,
        *,
        name: str | None = None,
        description: str | None = None,
    ):
        # 从函数元信息自动提取
        self.name = name or func.__name__
        self.description = description or extract_description(func) or self.name
        self.parameters_schema = generate_schema(func)
        
        # 保存原始函数引用
        self._func = func
        self.__doc__ = func.__doc__  # 保留原始 docstring
    
    async def execute(self, **kwargs) -> ToolResult:
        """执行原始函数并包装结果。"""
        result = await self._func(**kwargs)
        
        # 如果函数已经返回 ToolResult，直接使用
        if isinstance(result, ToolResult):
            return result
        
        # 否则包装为 ToolResult
        return ToolResult(content=str(result))

def make_tool(
    func: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> FunctionTool | Callable:
    """
    工具创建工厂函数。支持装饰器用法和直接调用用法。
    
    用法 1 — 装饰器:
        @make_tool
        async def web_search(query: str) -> ToolResult:
            '''搜索互联网'''
            ...
    
    用法 2 — 直接包装:
        tool = make_tool(web_search, name="search", description="搜索")
    
    用法 3 — 从代码文本:
        tool = make_tool_from_code(code_text, name="translate")
    """
    if func is not None:
        return FunctionTool(func, name=name, description=description)
    
    def decorator(f):
        return FunctionTool(f, name=name, description=description)
    return decorator
```

### 5.3 `myagent/tools/loader.py` — 三通道工具加载器

```python
"""
ToolLoader：统一工具加载器。
负责从三个通道加载工具并注册到 ToolRegistry：
  通道 A: Python 模块路径（entry_point 格式 "module.path:attr_name"）
  通道 B: 动态代码文本（exec 加载）
  通道 C: MCP Server（通过 MCPClientManager，远景）
  
所有通道的输出都是 BaseTool 实例，注册到同一个 ToolRegistry。
"""
import importlib
import ast
import asyncio
from pathlib import Path

from myagent.tools.base import BaseTool, ToolResult
from myagent.tools.wrapper import FunctionTool
from myagent.tools.registry import ToolRegistry
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class ToolLoader:
    """工具加载器。"""
    
    # ── 通道 A: 从 Python entry_point 加载 ──
    
    @staticmethod
    def load_from_entry(entry: str, **kwargs) -> BaseTool:
        """
        从 Python 模块路径加载工具。
        
        Args:
            entry: 格式 "module.path:function_name" 或 "module.path:ClassName"
                   例如 "myagent.extra.search:web_search"
        
        原理：
            importlib.import_module(module_path) → 得到模块对象
            getattr(module, attr_name) → 得到函数/类对象
            如果是函数 → FunctionTool 包装
            如果是 BaseTool 子类 → 直接实例化
        """
        if ":" not in entry:
            raise ValueError(f"Invalid entry point format: {entry}. Expected 'module:attr'")
        
        module_path, attr_name = entry.rsplit(":", 1)
        
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ImportError(f"Failed to import module '{module_path}': {e}")
        
        obj = getattr(module, attr_name, None)
        if obj is None:
            raise AttributeError(f"'{module_path}' has no attribute '{attr_name}'")
        
        # 已经是 BaseTool 实例
        if isinstance(obj, BaseTool):
            return obj
        
        # BaseTool 子类（未实例化的类）
        if isinstance(obj, type) and issubclass(obj, BaseTool):
            return obj()
        
        # 普通函数 → 自动包装
        if callable(obj):
            return FunctionTool(obj, **kwargs)
        
        raise TypeError(f"Entry point '{entry}' must be a function or BaseTool, got {type(obj)}")
    
    # ── 通道 B: 从代码文本动态加载 ──
    
    @staticmethod
    def load_from_code(
        code: str,
        *,
        function_name: str | None = None,
        name: str | None = None,
        description: str | None = None,
        safe_mode: bool = True,
    ) -> BaseTool:
        """
        从 Python 代码文本动态加载工具。
        
        Args:
            code: Python 代码字符串
            function_name: 要提取的函数名（如果代码中只有一个 async def，可自动推断）
            name: 工具名称（默认用函数名）
            description: 工具描述（默认从 docstring 提取）
            safe_mode: 安全模式（True 时禁止危险内置函数）
        
        原理：
            1. 可选：ast.parse() 语法检查
            2. exec(code, namespace) → 在命名空间中执行代码
            3. 从 namespace 中提取目标函数对象
            4. FunctionTool(func) → 自动自省生成 schema
        """
        # AST 安全检查（safe_mode 时）
        if safe_mode:
            _validate_code_safety(code)
        
        # 执行代码
        namespace = {"__builtins__": __builtins__}
        exec(code, namespace)
        
        # 发现目标函数
        func = _discover_function(namespace, function_name)
        
        return FunctionTool(func, name=name, description=description)
    
    # ── 通道 A+B 统一入口 ──
    
    @staticmethod
    def load_from_config(tool_configs: list[dict]) -> list[BaseTool]:
        """
        从配置列表批量加载工具。
        
        每个配置项格式：
        {
            "type": "entry",                    # 通道A
            "entry": "my_package.module:func",
            "name": "optional_override",
        }
        或：
        {
            "type": "code",                     # 通道B
            "code": "async def translate(...) -> ToolResult: ...",
            "name": "translate",
        }
        或：
        {
            "type": "file",                     # 通道A变体：从文件加载
            "path": "./my_tools/search.py",
            "function": "web_search",
        }
        """
        tools = []
        for cfg in tool_configs:
            tool_type = cfg.get("type", "entry")
            try:
                if tool_type == "entry":
                    tool = ToolLoader.load_from_entry(
                        cfg["entry"],
                        name=cfg.get("name"),
                        description=cfg.get("description"),
                    )
                elif tool_type == "code":
                    tool = ToolLoader.load_from_code(
                        cfg["code"],
                        function_name=cfg.get("function"),
                        name=cfg.get("name"),
                        description=cfg.get("description"),
                    )
                elif tool_type == "file":
                    path = Path(cfg["path"])
                    code = path.read_text(encoding="utf-8")
                    tool = ToolLoader.load_from_code(
                        code,
                        function_name=cfg.get("function"),
                        name=cfg.get("name"),
                        description=cfg.get("description"),
                    )
                else:
                    logger.warning(f"Unknown tool type: {tool_type}")
                    continue
                
                tools.append(tool)
                logger.info(f"Loaded tool: {tool.name} (type={tool_type})")
            except Exception as e:
                logger.error(f"Failed to load tool from config {cfg}: {e}")
        
        return tools


def _discover_function(namespace: dict, function_name: str | None = None):
    """从 exec 的 namespace 中发现目标 async 函数。"""
    if function_name:
        func = namespace.get(function_name)
        if func is None:
            raise ValueError(f"Function '{function_name}' not found in code")
        return func
    
    # 自动发现：找第一个 async def 函数（排除内部名称）
    async_funcs = [
        obj for name, obj in namespace.items()
        if callable(obj) and not name.startswith("_")
        and asyncio.iscoroutinefunction(obj)
    ]
    
    # 备选：找第一个普通 callable（排除类和模块）
    if not async_funcs:
        candidates = [
            obj for name, obj in namespace.items()
            if callable(obj) and not isinstance(obj, type) and not name.startswith("_")
        ]
        if candidates:
            return candidates[0]
    
    if async_funcs:
        return async_funcs[0]
    
    raise ValueError("No callable function found in the provided code")


def _validate_code_safety(code: str):
    """AST 级别的安全检查。"""
    tree = ast.parse(code)
    
    forbidden = {
        ast.Import: lambda n: any(alias.name in ('os', 'subprocess', 'sys') for alias in n.names),
        ast.ImportFrom: lambda n: n.module in ('os', 'subprocess', 'sys'),
    }
    
    for node in ast.walk(tree):
        for node_type, checker in forbidden.items():
            if isinstance(node, node_type) and checker(node):
                raise ValueError(
                    f"Safety check failed: import of restricted module detected "
                    f"at line {node.lineno}"
                )
```

---

## 六、热加载 — 解决 `_tool_schemas` 缓存问题

### 6.1 问题分析

当前 `Session.__init__` 预计算 `_tool_schemas`（`session.py:84`），`AgentLoop` 和 `ModelTurn` 构造时固定 schema，导致运行时注册的新工具不会出现在 LLM 的工具列表中。

### 6.2 `session.py` 修改

```python
# 之前（静态缓存）
self._tool_schemas = executor.get_tool_schemas()

# 之后（动态获取）
@property
def tool_schemas(self):
    """动态获取最新的工具 schema 列表（支持运行时热加载）。"""
    return self._executor.get_tool_schemas()
```

### 6.3 `loop.py` 修改 — AgentLoop 持有 executor 引用

```python
class AgentLoop:
    def __init__(self, ..., executor: ToolExecutor, ...):
        self._executor = executor  # 新增：持有引用
        # self._tool_schemas = tool_schemas  # 移除：不再缓存
    
    def _create_turn(self, kind: TurnKind):
        if kind == TurnKind.MODEL:
            return ModelTurn(
                tool_schemas=self._executor.get_tool_schemas(),  # 每次动态获取
                ...
            )
```

### 6.4 `Agent.add_tool()` 增强

```python
def add_tool(self, tool) -> None:
    """注册工具（支持热加载）。"""
    self._tool_registry.register(tool)
    # 由于 session.tool_schemas 和 loop 现在都是动态获取，
    # 新注册的工具会在下一次 ModelTurn 自动可见
    logger.info(f"Tool hot-loaded: {tool.name}")
```

---

## 七、配置驱动

### `config.yaml` 扩展

```yaml
agent:
  # ... 现有配置 ...
  
  # ── 扩展工具（三通道统一配置）──
  tools:
    default_timeout: 30.0
    extra:
      # 通道 A: entry_point（本地模块）
      - type: "entry"
        entry: "myagent.extra.web_search:web_search"
      
      - type: "entry"
        entry: "myagent.extra.translator:translate"
        name: "translate_text"    # 可选：覆盖工具名称
      
      # 通道 A 变体: 文件路径
      - type: "file"
        path: "./my_tools/analyzer.py"
        function: "analyze_data"
      
      # 通道 B: 内联代码（适合简单工具）
      # - type: "code"
      #   code: |
      #     async def greet(name: str) -> ToolResult:
      #         '''打招呼
      #         Args:
      #             name: 人名
      #         '''
      #         return ToolResult(content=f"Hello, {name}!")
  
  # ── MCP Client（远景）──
  mcp:
    enabled: false
    servers:
      - name: "weather"
        transport: "http"
        url: "http://localhost:9000/mcp"
```

### `AgentConfig` 扩展

```python
# myagent/utils/config.py 新增
class ExtraToolConfig(BaseModel):
    """单个扩展工具配置。"""
    type: str = "entry"          # entry | code | file
    entry: str | None = None     # 通道A: "module.path:function"
    code: str | None = None      # 通道B: 内联代码
    path: str | None = None      # 通道A变体: 文件路径
    function: str | None = None  # 目标函数名（可选）
    name: str | None = None      # 覆盖工具名称（可选）
    description: str | None = None  # 覆盖工具描述（可选）

class ToolsConfig(BaseModel):
    """工具总配置。"""
    default_timeout: float = 30.0
    extra: list[ExtraToolConfig] = []
```

---

## 八、MCP Client 远景（通道 C）

当需要对接 MCP Server 时，新增 `myagent/tools/mcp/` 模块：

```
myagent/tools/mcp/
├── __init__.py
├── config.py          # MCPServerConfig Pydantic 模型
├── client_manager.py  # FastMCP Client 连接管理器
└── mcp_tool.py        # MCPTool(BaseTool) — 将远程工具适配为 BaseTool
```

### `mcp_tool.py` 核心设计

```python
class MCPTool(BaseTool):
    """
    将单个 MCP 工具包装为 MyAgent BaseTool。
    
    关键：parameters_schema 直接使用 MCP Server 返回的 inputSchema，
    与自人生成的 schema 格式完全一致，零转换。
    """
    
    def __init__(self, client_manager, server_name, tool_info):
        self.name = f"mcp_{server_name}_{tool_info.name}"
        self.description = f"[MCP:{server_name}] {tool_info.description}"
        self.parameters_schema = tool_info.inputSchema  # ← 直接使用，格式一致
        self.meta = {
            "source": "mcp",
            "server": server_name,
            "original_name": tool_info.name,
        }
        self._client_manager = client_manager
        self._server_name = server_name
        self._tool_name = tool_info.name
    
    async def execute(self, **kwargs) -> ToolResult:
        """委托给 MCP 服务器执行。"""
        try:
            result = await self._client_manager.call_tool(
                self._server_name, self._tool_name, kwargs
            )
            return ToolResult(content=str(result))
        except Exception as e:
            return ToolResult(content=f"MCP tool error: {e}", is_error=True)
```

### `client_manager.py` 核心设计

```python
class MCPClientManager:
    """
    管理到多个 MCP 服务器的连接。
    
    职责：
    1. 按配置并行建立到所有 MCP 服务器的连接
    2. 聚合所有服务器的工具列表（带命名空间前缀）
    3. 路由工具调用到正确的服务器
    4. 生命周期管理（启动连接 / 健康检查 / 优雅关闭）
    """
    
    def __init__(self, servers: list[MCPServerConfig]):
        self._configs = {s.name: s for s in servers}
        self._clients: dict[str, Client] = {}
    
    async def connect_all(self) -> None:
        """并行连接所有已启用的 MCP 服务器。"""
        ...
    
    async def disconnect_all(self) -> None:
        """关闭所有客户端连接。"""
        ...
    
    async def list_all_tools(self) -> list[MCPToolInfo]:
        """列出所有服务器的工具（带 server_name 前缀）。"""
        ...
    
    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict
    ) -> ToolResult:
        """调用指定服务器的工具。"""
        ...
    
    def health_check(self) -> dict[str, bool]:
        """检查各服务器连接状态。"""
        ...
```

### MCP 集成到 AgentFactory

```python
# AgentFactory._build_tool_registry() 末尾新增
mcp_cfg = self._app_config.get("mcp", {})
if mcp_cfg.get("enabled"):
    servers = [MCPServerConfig(**s) for s in mcp_cfg.get("servers", []) 
               if s.get("enabled", True)]
    if servers:
        self._mcp_manager = MCPClientManager(servers)
        await self._mcp_manager.connect_all()
        for tool_info in await self._mcp_manager.list_all_tools():
            tool_registry.register(
                MCPTool(self._mcp_manager, tool_info.server, tool_info)
            )
```

### 安全集成

MCP 工具与内置工具走相同的安全流水线：

```
用户消息 → AgentLoop
  → ProviderRouter.stream() → LLM 决定调用 mcp_weather_forecast
    → ToolExecutor.execute()
      → SafetyGuard.check_tool_call("mcp_weather_forecast", args)  ← 统一安全入口
        → PolicyEngine.decide("mcp_weather_forecast")               ← 策略匹配
        → CLIFence.check()                                          ← CLI 安全围栏
        → ContentFilter                                             ← 内容过滤
      → IdempotencyCache.get(call_id)                               ← 幂等保护
      → SecretManager.inject_secrets()                              ← 凭据注入
      → MCPTool.execute(**args)                                     ← 实际调用
        → MCPClientManager.call_tool("weather", "forecast", args)   ← 委托给 FastMCP
```

---

## 九、实施步骤

| 步骤 | 内容 | 优先级 | 工期 |
|------|------|--------|------|
| **Step 1** | 新建 `schema.py` — 自省→Schema 生成 | 🔴 高 | 半天 |
| **Step 2** | 新建 `wrapper.py` — `make_tool()` / `FunctionTool` | 🔴 高 | 半天 |
| **Step 3** | 新建 `loader.py` — 三通道加载器 | 🔴 高 | 1 天 |
| **Step 4** | 修改 `session.py` + `loop.py` — 解决 schema 缓存 | 🔴 高 | 半天 |
| **Step 5** | 修改 `factory.py` — 接入 extra_tools 配置加载 | 🔴 高 | 半天 |
| **Step 6** | 扩展 `config.yaml.example` + `AgentConfig` | 🔴 高 | 半天 |
| **Step 7** | 新建 `mcp/` 模块 — MCP Client 集成 | 🟡 中 | 3-4 天 |
| **Step 8** | MCP Server 集成（暴露 MyAgent 工具为 MCP 标准） | 🟢 低 | 1-2 天 |

---

## 十、设计亮点

1. **Schema 一致性**：Python 自省生成的 schema = MCP `inputSchema` = OpenAI `parameters`，三者完全一致，零转换
2. **渐进式接入**：Step 1-6 不依赖 fastmcp，纯标准库实现；Step 7-8 MCP Client/Server 是可选增强
3. **热加载无侵入**：只需把 `_tool_schemas` 从缓存改为动态属性，现有 Session/Loop 架构无需重构
4. **安全一致性**：无论工具来源，都走相同的 SafetyGuard → PolicyEngine → CLIFence 流水线
5. **自研优先**：核心能力（自省、包装、加载、热注册）完全自研，不依赖外部框架；MCP 协议兼容作为远景扩展

---

## 十一、风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| `exec()` 动态加载的安全风险 | 恶意代码执行 | AST 安全检查 + 白名单控制 + SafetyGuard 审查 |
| MCP Server 连接不稳定 | Agent 启动失败或工具调用超时 | 懒加载 + 健康检查 + 超时兜底 + 失败隔离 |
| 工具名称冲突 | LLM 调错工具 | MCP 工具名统一加 `mcp_{server_name}_` 前缀 |
| `get_type_hints()` 对复杂类型支持不足 | Schema 不完整 | 基础类型用标准库映射，复杂类型可选引入 pydantic TypeAdapter |
| 依赖冲突（fastmcp） | 影响现有依赖 | 通过 `[mcp]` optional dependency 隔离 |