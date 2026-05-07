"""
HotReloader：文件热加载器，支持 tools_store 子目录结构。

从 myagent/tools/loader.py 拆分出来（职责分离：ToolLoader 负责加载，HotReloader 负责监控）。
"""
import asyncio
import time
from pathlib import Path
from typing import Callable

from myagent.tools.base import BaseTool, ToolMeta, FunctionTool
from myagent.tools.registry import ToolRegistry
from myagent.tools.loader import ToolLoader
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class HotReloader:
    """
    文件热加载器：周期性扫描 tools_store 目录，自动加载/重载工具到 Registry。

    工作流程：
        1. 每隔 poll_interval 秒扫描 watch_dir 下所有子目录
        2. 每个子目录视为一个工具，查找入口 .py 文件
        3. 通过 mtime 检测文件是否新增或变更
        4. 新增工具 → from_file() 加载 + ToolMeta 关联 → 注册到 registry
        5. 变更工具 → 重新加载，替换 registry 中的旧工具实例
        6. 支持 on_reload 回调通知上层

    目录结构（每个工具一个子目录）：
        tools_store/
        ├── weather/
        │   ├── weather_tool.py    # 工具入口
        │   └── meta.yaml          # 元数据（可选）
        └── search/
            ├── search_tool.py     # 工具入口
            ├── helpers.py         # 辅助模块
            └── meta.yaml          # 元数据

    用法：
        registry = ToolRegistry()
        reloader = HotReloader(registry, watch_dir="./tools_store")
        await reloader.start()   # 启动后台扫描
        # ... 随时可以往 tools_store 丢工具目录 ...
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

        # 工具状态追踪: { 目录路径(str): (mtime, tool_name) }
        self._tool_states: dict[str, tuple[float, str]] = {}
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

            # 为子进程 JSON-RPC 设置可导入的入口点
            if getattr(tool, "_entry_point", None) is None:
                tool._entry_point = self._compute_entry_point(Path(path), tool)

            self._registry.register(tool)

            file_path = str(Path(path).resolve())
            self._tool_states[file_path] = (time.time(), tool.name)

            # 加载元数据
            tool_dir = str(Path(path).parent)
            tool.meta = ToolMeta.load_for_hot_reload(tool.name, tool_dir)

            logger.info(f"手动重载工具: {tool.name} <- {path}")
            if self._on_reload:
                self._on_reload(tool, "manual_reload")
            return tool
        except Exception as e:
            logger.error(f"手动重载失败 ({path}): {e}")
            return None

    @property
    def watched_files(self) -> list[str]:
        """返回当前已追踪的工具目录列表。"""
        return list(self._tool_states.keys())

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
        """扫描目录，检测新增和变更的工具子目录。"""
        if not self._watch_dir.exists():
            self._watch_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"创建工具目录: {self._watch_dir}")
            return

        # 发现所有工具子目录
        discovered_tools = self._discover_tools(self._watch_dir)

        if not discovered_tools:
            return

        current_dirs = {t["dir_path_str"]: t for t in discovered_tools}

        # 检测已删除的工具目录 → 从 registry 移除
        for tracked_dir in list(self._tool_states.keys()):
            if tracked_dir not in current_dirs:
                _, tool_name = self._tool_states.pop(tracked_dir)
                self._registry.unregister(tool_name)
                logger.info(f"工具目录已删除，移除注册: {tool_name} <- {tracked_dir}")

        # 检测新增和变更的工具 → 加载/重载
        for dir_str, tool_info in current_dirs.items():
            try:
                mtime = max(f.stat().st_mtime for f in tool_info["entry_files"])
                prev = self._tool_states.get(dir_str)

                if prev is None:
                    # 新增工具
                    tool = self._load_tool_from_dir(tool_info)
                    if tool:
                        # 加载元数据
                        tool.meta = ToolMeta.load_for_hot_reload(
                            tool.name, tool_info["dir"]
                        )
                        self._registry.register(tool)
                        self._tool_states[dir_str] = (mtime, tool.name)
                        logger.info(f"热加载新工具: {tool.name} <- {tool_info['name']}")
                        if self._on_reload:
                            self._on_reload(tool, "added")

                elif mtime > prev[0]:
                    # 工具已变更
                    old_name = prev[1]
                    tool = self._load_tool_from_dir(tool_info)
                    if tool:
                        # 加载元数据
                        tool.meta = ToolMeta.load_for_hot_reload(
                            tool.name, tool_info["dir"]
                        )
                        # 如果工具名变了，移除旧名称
                        if old_name != tool.name:
                            self._registry.unregister(old_name)
                        self._registry.register(tool)
                        self._tool_states[dir_str] = (mtime, tool.name)
                        logger.info(f"热重载工具: {tool.name} <- {tool_info['name']}")
                        if self._on_reload:
                            self._on_reload(tool, "reloaded")

            except Exception as e:
                logger.error(f"加载工具失败 ({tool_info['name']}): {e}")

    def _discover_tools(self, tools_dir: Path) -> list[dict]:
        """
        发现 tools_store 下的所有工具。

        新结构：每个工具一个子目录，子目录下可以有：
          - *.py 文件（工具实现，至少一个）
          - meta.yaml（工具元数据，可选）
          - 其他辅助文件
        """
        tools = []
        if not tools_dir.exists():
            return tools

        for item in sorted(tools_dir.iterdir()):
            if not item.is_dir() or item.name.startswith("_"):
                continue

            # 查找入口 .py 文件
            py_files = [f for f in item.glob("*.py") if not f.name.startswith("_")]
            if not py_files:
                continue

            tools.append({
                "name": item.name,
                "dir": item,
                "dir_path_str": str(item.resolve()),
                "entry_files": py_files,
                "meta_path": item / "meta.yaml",
            })

        return tools

    def _load_tool_from_dir(self, tool_info: dict) -> BaseTool | None:
        """从工具目录加载工具（取第一个 .py 文件作为入口）。"""
        # 取第一个 .py 文件作为入口
        entry_file = tool_info["entry_files"][0]
        try:
            tool = ToolLoader.from_file(str(entry_file), safe_mode=self._safe_mode)
            # 为子进程 JSON-RPC 设置可导入的入口点
            if getattr(tool, "_entry_point", None) is None:
                tool._entry_point = self._compute_entry_point(entry_file, tool)
            return tool
        except Exception as e:
            logger.error(f"加载工具文件失败 ({entry_file}): {e}")
            return None

    def _compute_entry_point(self, file_path: Path, tool: BaseTool) -> str:
        """
        将文件路径转换为子进程可导入的 "module.path:attr_name" 入口点。

        例如:
            myagent/tools/tools_store/search/search_tool.py
            → myagent.tools.tools_store.search.search_tool:internet_search
        """
        # 将文件路径（去除 .py 后缀）转为模块路径
        parts = file_path.with_suffix("").parts

        # 从项目根开始查找模块路径起点（通常为 "myagent"）
        module_path = None
        for i, part in enumerate(parts):
            if part == "myagent":
                module_path = ".".join(parts[i:])
                break

        if module_path is None:
            # 回退：直接用文件名（不含扩展名）
            module_path = file_path.stem

        # 获取原始函数名
        func = getattr(tool, "_func", None)
        attr_name = func.__name__ if func else tool.name

        return f"{module_path}:{attr_name}"
