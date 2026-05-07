"""
ToolLoader：统一工具加载器。

负责从三个通道加载工具并注册到 ToolRegistry：
  通道 A: Python 模块路径（entry_point 格式 "module.path:attr_name"）
  通道 B: 动态代码文本（exec 加载）
  通道 A 变体: 代码文件路径（读取后走通道 B）

所有通道的输出都是 BaseTool 实例，注册到同一个 ToolRegistry。
支持 AST 安全检查（safe_mode）。

HotReloader 已拆分到 myagent/tools/hot_reloader.py。
"""
import ast
import asyncio
import importlib
from pathlib import Path
from typing import Callable

from myagent.tools.base import BaseTool, FunctionTool
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class ToolLoader:
    """工具加载器：支持 entry_point / code_text / file_path 三种来源。"""

    @staticmethod
    def from_entry(entry: str, **kwargs) -> BaseTool:
        """
        从 Python 模块路径加载工具。

        entry 格式: "module.path:function_name" 或 "module.path:ClassName"

        原理：
            importlib.import_module(module_path) → getattr(module, attr_name)
            函数 → FunctionTool 包装
            BaseTool 子类 → 直接实例化
        """
        if ":" not in entry:
            raise ValueError(f"无效 entry 格式: {entry}。应为 'module:attr'。")

        module_path, attr_name = entry.rsplit(":", 1)

        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ImportError(f"无法导入模块 '{module_path}': {e}")

        obj = getattr(module, attr_name, None)
        if obj is None:
            raise AttributeError(f"'{module_path}' 中没有 '{attr_name}'")

        if isinstance(obj, BaseTool):
            obj._entry_point = entry
            return obj

        if isinstance(obj, type) and issubclass(obj, BaseTool):
            instance = obj()
            instance._entry_point = entry
            return instance

        if callable(obj):
            tool = FunctionTool(obj, **kwargs)
            tool._entry_point = entry
            return tool

        raise TypeError(f"entry 指向的对象必须是函数或 BaseTool，实际是 {type(obj)}")

    @staticmethod
    def from_code(
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
            function_name: 目标函数名（代码中只有一个 async def 时可自动推断）
            name: 覆盖工具名称（默认取自函数名）
            description: 覆盖工具描述（默认取自 docstring）
            safe_mode: 启用 AST 安全检查（拦截危险 import）
        """
        if safe_mode:
            _validate_code_safety(code)

        namespace: dict = {"__builtins__": __builtins__}
        exec(code, namespace)

        func = _discover_function(namespace, function_name)
        return FunctionTool(func, name=name, description=description)

    @staticmethod
    def from_file(
        path: str,
        *,
        function_name: str | None = None,
        name: str | None = None,
        description: str | None = None,
        safe_mode: bool = True,
    ) -> BaseTool:
        """
        从代码文件加载工具（读取文件后走 from_code 通道）。

        path: 代码文件路径
        """
        code = Path(path).read_text(encoding="utf-8")
        return ToolLoader.from_code(
            code,
            function_name=function_name,
            name=name,
            description=description,
            safe_mode=safe_mode,
        )

    @staticmethod
    def from_config(tool_configs: list[dict]) -> list[BaseTool]:
        """
        从配置列表批量加载工具。

        每个配置项格式：
            {"type": "entry", "entry": "my_package.module:func"}
            {"type": "code",  "code": "async def foo(...) -> ..."}
            {"type": "file",  "path": "./my_tools/search.py", "function": "web_search"}

        返回值是所有成功加载的 BaseTool 实例列表。
        """
        methods = {
            "entry": lambda cfg: ToolLoader.from_entry(
                cfg["entry"],
                name=cfg.get("name"),
                description=cfg.get("description"),
            ),
            "code": lambda cfg: ToolLoader.from_code(
                cfg["code"],
                function_name=cfg.get("function"),
                name=cfg.get("name"),
                description=cfg.get("description"),
            ),
            "file": lambda cfg: ToolLoader.from_file(
                cfg["path"],
                function_name=cfg.get("function"),
                name=cfg.get("name"),
                description=cfg.get("description"),
            ),
        }

        tools: list[BaseTool] = []
        for cfg in tool_configs:
            tool_type = cfg.get("type", "entry")
            loader = methods.get(tool_type)
            if loader is None:
                logger.warning(f"未知工具类型: {tool_type}，跳过")
                continue
            try:
                tool = loader(cfg)
                tools.append(tool)
                logger.info(f"加载工具: {tool.name} (来源={tool_type})")
            except Exception as e:
                logger.error(f"无法从配置加载工具 ({cfg}): {e}")

        return tools


def _discover_function(namespace: dict, function_name: str | None = None) -> Callable:
    """从 exec 的 namespace 中发现目标函数。"""
    if function_name:
        func = namespace.get(function_name)
        if func is None:
            raise ValueError(f"代码中未找到函数 '{function_name}'")
        if not callable(func):
            raise TypeError(f"'{function_name}' 不是可调用对象")
        return func

    async_funcs = [
        obj
        for name, obj in namespace.items()
        if callable(obj)
        and not name.startswith("_")
        and asyncio.iscoroutinefunction(obj)
    ]

    if async_funcs:
        return async_funcs[0]

    candidates = [
        obj
        for name, obj in namespace.items()
        if callable(obj) and not isinstance(obj, type) and not name.startswith("_")
    ]
    if candidates:
        return candidates[0]

    raise ValueError("代码中未找到可调用的函数")


# ── AST 安全检查 ──

_RESTRICTED_MODULES = frozenset({
    "os", "subprocess", "sys", "shutil",
    "socket", "http.server", "pickle", "ctypes",
})


def _validate_code_safety(code: str) -> None:
    """AST 级别的代码安全检查：拦截危险模块导入。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"代码语法错误: {e}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _RESTRICTED_MODULES:
                    raise ValueError(
                        f"安全检查拒绝: 禁止导入模块 '{alias.name}' (行 {node.lineno})"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _RESTRICTED_MODULES:
                raise ValueError(
                    f"安全检查拒绝: 禁止导入模块 '{node.module}' (行 {node.lineno})"
                )


