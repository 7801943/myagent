"""
ToolLoader：统一工具加载器。

负责从三个通道加载工具并注册到 ToolRegistry：
  通道 A: Python 模块路径（entry_point 格式 "module.path:attr_name"）
  通道 B: 动态代码文本（exec 加载）
  通道 A 变体: 代码文件路径（读取后走通道 B）

所有通道的输出都是 BaseTool 实例，注册到同一个 ToolRegistry。
支持 AST 安全检查（safe_mode）。
"""
import ast
import asyncio
import importlib
import time
from pathlib import Path
from typing import Callable

from myagent.tools.base import BaseTool
from myagent.tools.registry import ToolRegistry
from myagent.tools.wrapper import FunctionTool
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
            return obj

        if isinstance(obj, type) and issubclass(obj, BaseTool):
            return obj()

        if callable(obj):
            return FunctionTool(obj, **kwargs)

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


# ── 文件热加载 ──


class HotReloader:
    """
    文件热加载器：周期性扫描 tools_store 目录，自动加载/重载工具到 Registry。

    工作流程：
        1. 每隔 poll_interval 秒扫描 watch_dir 下所有 .py 文件
        2. 通过 mtime 检测文件是否新增或变更
        3. 新增文件 → from_file() 加载并注册到 registry
        4. 变更文件 → 重新加载，替换 registry 中的旧工具实例
        5. 支持 on_reload 回调通知上层

    用法：
        registry = ToolRegistry()
        reloader = HotReloader(registry, watch_dir="./tools_store")
        await reloader.start()   # 启动后台扫描
        # ... 随时可以往 tools_store 丢 .py 文件 ...
        await reloader.stop()    # 停止扫描
    """

    def __init__(
        self,
        registry: ToolRegistry,
        watch_dir: str = "myagent/tools/tools_store",
        poll_interval: float = 60.0,
        on_reload: Callable[[BaseTool, str], None] | None = None,
        safe_mode: bool = True,
    ):
        """
        Args:
            registry: 工具注册中心实例
            watch_dir: 监控的工具脚本目录
            poll_interval: 扫描间隔（秒），默认 60 秒
            on_reload: 工具加载/重载后的回调函数 (tool, event_type)
            safe_mode: 是否启用 AST 安全检查
        """
        self._registry = registry
        self._watch_dir = Path(watch_dir)
        self._poll_interval = poll_interval
        self._on_reload = on_reload
        self._safe_mode = safe_mode

        # 文件状态追踪: { 文件路径(str): (mtime, tool_name) }
        self._file_states: dict[str, tuple[float, str]] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    # ── 公开接口 ──

    async def start(self) -> None:
        """启动后台扫描任务。"""
        if self._running:
            logger.warning("HotReloader 已在运行中")
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"HotReloader 已启动: watch_dir={self._watch_dir}, "
            f"interval={self._poll_interval}s"
        )

        # 首次立即扫描一次
        await self._scan()

    async def stop(self) -> None:
        """停止后台扫描任务。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("HotReloader 已停止")

    def reload(self, path: str) -> BaseTool | None:
        """
        手动触发指定文件的重载。

        Args:
            path: 工具文件路径

        Returns:
            加载后的 BaseTool 实例，失败返回 None
        """
        try:
            tool = ToolLoader.from_file(path, safe_mode=self._safe_mode)
            self._registry.register(tool)

            file_path = str(Path(path).resolve())
            self._file_states[file_path] = (time.time(), tool.name)

            logger.info(f"手动重载工具: {tool.name} <- {path}")
            if self._on_reload:
                self._on_reload(tool, "manual_reload")
            return tool
        except Exception as e:
            logger.error(f"手动重载失败 ({path}): {e}")
            return None

    @property
    def watched_files(self) -> list[str]:
        """返回当前已追踪的文件列表。"""
        return list(self._file_states.keys())

    @property
    def is_running(self) -> bool:
        """返回是否正在运行。"""
        return self._running

    # ── 内部实现 ──

    async def _poll_loop(self) -> None:
        """后台轮询循环。"""
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._scan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"HotReloader 扫描异常: {e}")

    async def _scan(self) -> None:
        """扫描目录，检测新增和变更文件。"""
        if not self._watch_dir.exists():
            self._watch_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"创建工具目录: {self._watch_dir}")
            return

        # 获取目录下所有 .py 文件（排除 __init__.py）
        py_files = [
            f for f in self._watch_dir.glob("*.py")
            if f.name != "__init__.py"
        ]

        if not py_files:
            return

        current_files = {str(f.resolve()): f for f in py_files}

        # 检测已删除的文件 → 从 registry 移除
        for tracked_path in list(self._file_states.keys()):
            if tracked_path not in current_files:
                _, tool_name = self._file_states.pop(tracked_path)
                self._registry.unregister(tool_name)
                logger.info(f"工具文件已删除，移除注册: {tool_name} <- {tracked_path}")

        # 检测新增和变更的文件 → 加载/重载
        for file_str, file_path in current_files.items():
            try:
                mtime = file_path.stat().st_mtime
                prev = self._file_states.get(file_str)

                if prev is None:
                    # 新增文件
                    tool = ToolLoader.from_file(
                        str(file_path), safe_mode=self._safe_mode
                    )
                    self._registry.register(tool)
                    self._file_states[file_str] = (mtime, tool.name)
                    logger.info(f"热加载新工具: {tool.name} <- {file_path.name}")
                    if self._on_reload:
                        self._on_reload(tool, "added")

                elif mtime > prev[0]:
                    # 文件已变更
                    old_name = prev[1]
                    tool = ToolLoader.from_file(
                        str(file_path), safe_mode=self._safe_mode
                    )
                    # 如果工具名变了，移除旧名称
                    if old_name != tool.name:
                        self._registry.unregister(old_name)
                    self._registry.register(tool)
                    self._file_states[file_str] = (mtime, tool.name)
                    logger.info(f"热重载工具: {tool.name} <- {file_path.name}")
                    if self._on_reload:
                        self._on_reload(tool, "reloaded")

            except Exception as e:
                logger.error(f"加载工具文件失败 ({file_path.name}): {e}")
