# MyAgent Tools 系统升级方案

## 一、现有代码结构总览

```
myagent/tools/
├── __init__.py          # 导出 14 个符号
├── base.py              # BaseTool 抽象基类 + ToolResult 数据类
├── schema.py            # Python 自省 → JSON Schema 生成器
├── wrapper.py           # FunctionTool 包装器 + make_tool 装饰器
├── registry.py          # ToolRegistry 注册中心（dict 封装）
├── executor.py          # ToolExecutor 执行引擎（Safety → Cache → Secret → Execute）
├── loader.py            # ToolLoader 三通道加载 + HotReloader 热加载
├── cli_tool.py          # CLITool（沙盒命令执行）
├── file_tools.py        # FileReadTool / FileWriteTool
├── idempotency.py       # IdempotencyCache（LRU 幂等缓存）
├── secrets.py           # SecretManager（凭据注入 + 脱敏）
├── sandbox/
│   ├── base.py          # BaseSandbox + SandboxResult
│   ├── subprocess_sandbox.py  # SubprocessSandbox（ulimit 隔离）
│   └── docker_sandbox.py      # DockerSandbox（骨架，NotImplemented）
└── tools_store/
    └── weather_tool.py  # 示例工具（热加载用）
```

### 调用链路

```
AgentFactory._build_tool_registry()
  → ToolRegistry() + register(CLITool/FileReadTool/FileWriteTool)
  → Agent.__init__() 持有 ToolRegistry + 创建 ToolExecutor
  → AgentLoop._create_turn(MODEL) → executor.get_tool_schemas() → 动态获取
  → Provider.format_tools() → LLM API
  → ToolTurn → ToolExecutor.execute_batch() → BaseTool.execute()
```

---

## 二、发现的问题

### 2.1 代码实现问题

#### P1: `BaseTool.__init_subclass__` 逻辑错误

```python
# base.py L36-40
def __init_subclass__(cls, **kwargs):
    super().__init_subclass__(**kwargs)
    if not cls.name and not getattr(cls, 'name', None):
        cls.name = cls.__name__
```

**问题**：`not cls.name` 和 `not getattr(cls, 'name', None)` 是同义的（`cls.name` 本身就是 `getattr` 查找），条件永远等价于 `not cls.name`。当 `cls.name = ""`（父类默认值）时，所有子类都会被覆盖为类名——但 `CLITool` 等已经声明了 `name = "cli_execute"`，所以这个条件实际从不触发（非空字符串 truthy）。**这段代码无害但也无用**，建议移除或修正意图。

#### P2: `ToolRegistry` 声称"线程安全"但实际不是

```python
# registry.py L11
class ToolRegistry:
    """工具注册中心。线程安全的字典封装。"""
```

**问题**：没有任何锁机制。在 asyncio 环境中，同一事件循环内的 `register`/`unregister` 不会有竞态问题（单线程协程），但 docstring "线程安全" 是误导。如果未来涉及多线程（如工具在独立进程中运行），需要加锁。**建议修正 docstring 或添加 `asyncio.Lock`。**

#### P3: `ToolExecutor.get_tool_schemas()` 返回类型不一致

```python
# executor.py L135-138
def get_tool_schemas(self) -> list | None:
    tools = self._registry.list_tools()
    return tools if tools else None
```

**问题**：返回的是 `list[BaseTool]`（工具实例列表），不是 "schemas"。方法名暗示返回 JSON Schema，但实际返回 BaseTool 列表，由 Provider 的 `format_tools()` 再转换。**命名误导**，建议改为 `get_tools()` 或 `list_tools()`。

#### P4: `weather_tool.py` 使用同步阻塞 I/O

```python
# weather_tool.py — 声明为 async def 但内部使用 urllib.request.urlopen（同步阻塞）
async def query_weather(city: str = "Beijing") -> str:
    ...
    with urllib.request.urlopen(req, timeout=10) as resp:  # 阻塞事件循环！
```

**问题**：`urllib.request.urlopen` 是同步阻塞调用，在 async 函数中会阻塞整个事件循环。应使用 `aiohttp` 或 `asyncio.to_thread()`。

#### P5: `HotReloader` 和 `ToolLoader` 职责耦合

`loader.py` 同时包含 `ToolLoader`（加载逻辑，196 行）和 `HotReloader`（文件监控，182 行），共 413 行。两者职责不同但因历史原因在同一文件中。

#### P6: `SecretManager._resolved_secrets` 内存泄漏风险

```python
# secrets.py L37
self._resolved_secrets: dict[str, str] = {}
```

永远只增不减，无 TTL 或 LRU 淘汰。对于长运行 Agent 进程，密钥值会永驻内存。虽然当前密钥量不大，但明文密钥长驻内存本身也是安全隐患。

#### P7: `BaseTool.meta` 使用可变类属性

```python
# base.py L29
meta: dict = {}  # 所有子类共享同一个 dict 实例！
```

**问题**：类级别可变默认值是 Python 经典陷阱。如果某个子类实例修改 `meta`，会影响所有子类。当前未被使用所以无害，但未来使用时必然出 bug。

---

### 2.2 架构层面的问题

| 问题 | 说明 |
|------|------|
| 工具在主进程同步执行 | `BaseTool.execute()` 直接在 Agent 事件循环中运行，IO 密集型工具（网络请求、文件操作）会阻塞整个循环 |
| 无工具元数据结构 | `BaseTool.meta: dict = {}` 只是占位，无标准化字段定义，热注入系统无法分类 |
| MCP 兼容只有注释 | 多处 `# TODO: [MCP]` 但无实际接口预留 |
| `sandbox/` 与 `tools/` 绑定 | 沙盒是执行环境概念，不应该是 tools 的子模块 |
| secrets 职责错位 | 密钥管理属于安全范畴，不应放在 tools 模块 |

---

## 三、升级方案

### 3.1 目标

1. **进程隔离**：工具在独立进程中运行，使用 JSON-RPC 通信，未来可平滑迁移到远程服务器/容器
2. **MCP 兼容**：维持自研 schema 方案，预留 MCP Server 适配层
3. **元数据配置化**：工具元数据通过配置文件动态定义，支持运行时扩展和删改，不硬编码
4. **精简抽象**：合并冗余模块，减少不必要的间接层

### 3.2 新目录结构（精简后）

```
myagent/
├── tools/
│   ├── __init__.py            # 精简导出
│   ├── base.py                # BaseTool + ToolResult + ToolMeta（动态配置驱动）+ FunctionTool + make_tool
│   ├── schema.py              # JSON Schema 生成器（保持不变）
│   ├── registry.py            # ToolRegistry（修正 docstring）
│   ├── executor.py            # ToolExecutor（合并 IdempotencyCache + ProcessRunner 集成）
│   ├── loader.py              # ToolLoader（适配 tools_store 子目录结构）+ HotReloader
│   ├── process_runner.py      # [新] JSON-RPC 进程隔离执行器
│   ├── mcp_compat.py          # [新] MCP 兼容层骨架
│   ├── builtin/               # [新] 内置工具集合
│   │   ├── __init__.py
│   │   ├── cli_tool.py        # CLITool（移入）
│   │   └── file_tools.py      # FileReadTool / FileWriteTool（移入）
│   └── tools_store/           # 热加载工具（每个工具单独一个目录）
│       └── weather/
│           ├── weather_tool.py # 工具实现
│           └── meta.yaml      # 工具元数据（可选，缺失则使用全局默认）
├── safety/
│   ├── __init__.py
│   ├── base.py                # PolicyDecision, SafetyContext, GuardResult, BaseRule
│   ├── guard.py               # SafetyGuard（责任链编排）
│   ├── policy.py              # PolicyEngine
│   ├── cli_fence.py           # CLI 命令围栏
│   ├── content_rules.py       # 内容过滤规则
│   ├── rules/
│   │   └── __init__.py
│   └── secrets.py             # [移入] SecretManager（凭据注入 + 脱敏）
├── runtime/
│   ├── __init__.py
│   └── sandbox/               # [移入] 沙盒执行环境
│       ├── __init__.py
│       ├── base.py            # BaseSandbox + SandboxResult
│       ├── subprocess_sandbox.py  # SubprocessSandbox
│       └── docker_sandbox.py      # DockerSandbox
└── config/
    └── tool_meta.yaml         # [新] 工具元数据配置文件（全局默认 + 各工具覆盖）
```

**精简要点：**
- 删除 `wrapper.py` → `FunctionTool` 和 `make_tool` 移入 `base.py`（它们本质就是 BaseTool 的变体）
- 删除 `idempotency.py` → 合并到 `executor.py`（只有一个使用方，72 行代码无需独立文件）
- 删除 `secrets.py` → 移到 `myagent/safety/secrets.py`（密钥管理属于安全职责，与 SafetyGuard 同模块）
- 移动 `sandbox/` → `myagent/runtime/sandbox/`（沙盒是执行环境概念，不属于 tools）
- 内置工具移入 `builtin/` 子目录，与框架代码分离
- `tools_store/` 改为每个工具一个独立子目录，支持辅助文件共存
- 新增 `config/tool_meta.yaml` → 工具元数据配置化，不硬编码

### 3.3 工具元数据：配置文件驱动方案

**核心思路**：工具元数据（分类、权限、运行时约束等）通过 YAML 配置文件动态定义，代码端不硬编码任何字段。需要新增字段时只改配置文件，不改代码。

#### 配置文件格式 `config/tool_meta.yaml`

```yaml
# ============================================
# MyAgent 工具元数据配置
# 说明：
#   - defaults 下的字段为全局默认值，所有工具自动继承
#   - tools 下按工具 name 覆盖，支持任意字段
#   - 新增字段只需在此文件添加，ToolMeta 会自动读取
#   - 运行时可通过 ToolMeta.merge() 动态覆盖
# ============================================

# 全局默认值（所有工具继承）
defaults:
  category: custom
  permission: standard
  source: local
  timeout: 30.0
  max_retries: 0
  run_in_process: false
  requires_sandbox: false
  requires_network: false

# 各工具覆盖（字段随意扩展，不限于上面列出的 key）
tools:
  # --- 内置工具 ---
  cli_execute:
    category: system
    permission: dangerous
    requires_sandbox: true
    source: builtin
    run_in_process: true
    timeout: 60.0
    process_timeout: 120.0

  file_read:
    category: system
    permission: safe
    source: builtin

  file_write:
    category: system
    permission: standard
    source: builtin

  # --- 热加载工具 ---
  query_weather:
    category: network
    permission: safe
    requires_network: true
    source: hot_reload
    run_in_process: true
    timeout: 15.0

  # --- 未来 MCP 工具 ---
  # mcp_github_create_issue:
  #   category: external
  #   permission: standard
  #   source: mcp
  #   run_in_process: true
  #   server: github
  #   auth_required: true
```

#### 工具目录级元数据 `tools_store/<tool>/meta.yaml`

热加载工具可以在自己的目录下放置 `meta.yaml`，用于覆盖或补充全局配置：

```yaml
# tools_store/weather/meta.yaml
# 此文件会与全局 defaults 和 config/tool_meta.yaml 中的对应条目合并
# 优先级：meta.yaml > config/tool_meta.yaml > defaults

category: network
permission: safe
requires_network: true
timeout: 15.0
# 可以添加任意自定义字段
api_provider: open-meteo
cache_ttl: 300
```

#### 代码端 `ToolMeta` 实现

```python
# base.py — 动态元数据容器

import yaml
from pathlib import Path
from typing import Any

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
        attrs = {k: v for k, v in self.__dict__.items() if not k.startswith('_')}
        return f"ToolMeta({attrs})"
    
    def get(self, key: str, default: Any = None) -> Any:
        """安全获取属性，缺失时返回 default。"""
        return getattr(self, key, default)
    
    def merge(self, overrides: dict) -> "ToolMeta":
        """
        合并覆盖字段，返回新的 ToolMeta 实例。
        用于运行时动态修改元数据。
        """
        current = {k: v for k, v in self.__dict__.items() if not k.startswith('_')}
        current.update(overrides)
        return ToolMeta(**current)
    
    def to_dict(self) -> dict:
        """导出为字典（序列化/调试用）。"""
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}
    
    # ---- 类方法：从配置文件加载 ----
    
    @classmethod
    def load(cls, tool_name: str, 
             global_config_path: str = "config/tool_meta.yaml",
             tool_meta_path: str | None = None) -> "ToolMeta":
        """
        从配置文件加载工具元数据。
        
        Args:
            tool_name: 工具名称（对应 BaseTool.name）
            global_config_path: 全局配置文件路径
            tool_meta_path: 工具目录下的 meta.yaml 路径（可选）
        
        Returns:
            合并后的 ToolMeta 实例
        """
        # 1. 加载全局默认
        defaults = {}
        tool_overrides = {}
        
        global_path = Path(global_config_path)
        if global_path.exists():
            with open(global_path) as f:
                config = yaml.safe_load(f) or {}
            defaults = config.get("defaults", {})
            tool_overrides = config.get("tools", {}).get(tool_name, {})
        
        # 2. 加载工具目录级元数据
        local_meta = {}
        if tool_meta_path:
            local_path = Path(tool_meta_path)
            if local_path.exists():
                with open(local_path) as f:
                    local_meta = yaml.safe_load(f) or {}
        
        # 3. 三层合并：defaults < tool_overrides < local_meta
        merged = {}
        merged.update(defaults)
        merged.update(tool_overrides)
        merged.update(local_meta)
        
        # 确保 tool_name 字段始终存在
        merged["tool_name"] = tool_name
        
        return cls(**merged)
    
    @classmethod
    def load_for_hot_reload(cls, tool_name: str, tool_dir: str,
                            global_config_path: str = "config/tool_meta.yaml") -> "ToolMeta":
        """
        为热加载工具加载元数据的便捷方法。
        自动从 tool_dir 下查找 meta.yaml。
        """
        return cls.load(
            tool_name=tool_name,
            global_config_path=global_config_path,
            tool_meta_path=str(Path(tool_dir) / "meta.yaml"),
        )
```

**BaseTool 集成方式：**

```python
class BaseTool(ABC):
    name: str = ""
    description: str = ""
    parameters_schema: dict = {}
    meta: ToolMeta | None = None
    
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.name:
            cls.name = cls.__name__
    
    def _ensure_meta(self):
        """延迟加载元数据（首次访问时从配置文件读取）。"""
        if self.meta is None:
            self.meta = ToolMeta.load(self.name)
```

**内置工具使用示例（可选覆盖）：**

```python
class CLITool(BaseTool):
    name = "cli_execute"
    description = "在安全沙盒中执行 CLI 命令"
    parameters_schema = {...}
    # meta 不需要手动设置，_ensure_meta() 会从 config/tool_meta.yaml 自动加载
    # 如果需要代码级覆盖：
    # meta = ToolMeta(category="system", permission="dangerous", requires_sandbox=True)
```

**热加载工具使用示例：**

```python
# tools_store/weather/weather_tool.py
# meta 会从 tools_store/weather/meta.yaml + config/tool_meta.yaml 自动合并加载

from myagent.tools.base import make_tool, ToolResult

@make_tool
async def query_weather(city: str = "Beijing") -> str:
    """查询天气信息。"""
    ...
```

### 3.4 进程隔离方案 `process_runner.py` — JSON-RPC

**核心思路**：使用 `asyncio.create_subprocess_exec` 启动子进程，通过 stdin/stdout 传递 JSON-RPC 2.0 消息，实现工具的进程隔离执行。

#### JSON-RPC 协议格式

```json
// ---- 请求（主进程 → 子进程）----
{
  "jsonrpc": "2.0",
  "method": "execute",
  "params": {
    "tool_entry": "myagent.tools.builtin.cli_tool:CLITool",
    "arguments": {"command": "ls -la"},
    "timeout": 60.0,
    "meta": {"category": "system", "permission": "dangerous"}
  },
  "id": 1
}

// ---- 成功响应（子进程 → 主进程）----
{
  "jsonrpc": "2.0",
  "result": {
    "content": "total 8\ndrwxr-xr-x ...",
    "is_error": false,
    "metadata": {}
  },
  "id": 1
}

// ---- 错误响应（子进程 → 主进程）----
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32000,
    "message": "Tool execution failed",
    "data": {"type": "TimeoutError", "detail": "Execution timed out after 60s"}
  },
  "id": 1
}
```

**标准错误码：**

| 错误码 | 含义 |
|--------|------|
| `-32700` | JSON 解析错误 |
| `-32600` | 无效请求 |
| `-32601` | 方法不存在 |
| `-32602` | 无效参数 |
| `-32000` | 工具执行失败 |
| `-32001` | 执行超时 |
| `-32002` | 工具未找到 |

#### 子进程端入口脚本

```python
# process_runner.py 内置的子进程入口（通过 __main__ 或内联代码运行）
# 子进程职责：接收 JSON-RPC 请求 → 动态导入工具类 → 执行 → 返回结果

import sys
import json
import asyncio
import importlib

async def handle_request(request: dict) -> dict:
    """处理单条 JSON-RPC 请求。"""
    req_id = request.get("id")
    params = request.get("params", {})
    
    try:
        # 动态导入工具
        tool_entry = params["tool_entry"]  # "module.path:ClassName"
        module_path, class_name = tool_entry.rsplit(":", 1)
        module = importlib.import_module(module_path)
        tool_cls = getattr(module, class_name)
        
        # 实例化并执行
        tool = tool_cls()
        result = await asyncio.wait_for(
            tool.execute(**params.get("arguments", {})),
            timeout=params.get("timeout", 60.0),
        )
        
        return {
            "jsonrpc": "2.0",
            "result": {"content": result.content, "is_error": result.is_error, 
                       "metadata": result.metadata},
            "id": req_id,
        }
    except asyncio.TimeoutError:
        return {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Execution timeout"}, "id": req_id}
    except Exception as e:
        return {"jsonrpc": "2.0", "error": {"code": -32000, "message": str(e), 
                                              "data": {"type": type(e).__name__}}, "id": req_id}

async def main():
    """子进程主循环：从 stdin 逐行读取 JSON-RPC 请求。"""
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    
    writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_event_loop())
    
    while True:
        line = await reader.readline()
        if not line:
            break
        request = json.loads(line.decode())
        response = await handle_request(request)
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        await writer.drain()

if __name__ == "__main__":
    asyncio.run(main())
```

#### 主进程端 ProcessToolRunner

```python
# process_runner.py — 主进程端接口

import asyncio
import json
import uuid
from typing import Any

class ProcessToolRunner:
    """
    基于 stdin/stdout JSON-RPC 的进程隔离执行器。
    
    特性：
      - 每个 ToolMeta.run_in_process=True 的工具在独立子进程中执行
      - 使用 JSON-RPC 2.0 over stdio 通信
      - 支持超时控制、进程生命周期管理
      - 未来可无缝替换 transport 层（stdio → TCP → HTTP/gRPC）
    
    进程生命周期策略：
      - 模式 A（默认）：每次执行启动新进程（简单安全，无状态污染）
      - 模式 B（可选）：常驻进程复用（减少启动开销，需要手动状态清理）
    """
    
    def __init__(self, persistent: bool = False):
        self._persistent = persistent
        self._processes: dict[str, asyncio.subprocess.Process] = {}  # 常驻进程池
        self._request_id = 0
    
    async def run_tool(
        self,
        tool_entry: str,       # "module.path:ClassName"
        arguments: dict,
        timeout: float = 60.0,
        meta: dict | None = None,
    ) -> dict:  # 返回 {"content": str, "is_error": bool, "metadata": dict}
        """在独立子进程中执行工具，返回结果字典。"""
        
        if self._persistent:
            proc = await self._get_or_create_process(tool_entry)
        else:
            proc = await self._create_process()
        
        try:
            # 构造 JSON-RPC 请求
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "method": "execute",
                "params": {
                    "tool_entry": tool_entry,
                    "arguments": arguments,
                    "timeout": timeout,
                    "meta": meta or {},
                },
                "id": self._request_id,
            }
            
            # 发送请求
            request_line = json.dumps(request, ensure_ascii=False) + "\n"
            proc.stdin.write(request_line.encode())
            await proc.stdin.drain()
            
            # 读取响应（带超时）
            response_line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=timeout + 5.0  # 多给 5 秒余量
            )
            response = json.loads(response_line.decode())
            
            # 检查 JSON-RPC 错误
            if "error" in response:
                error = response["error"]
                return {
                    "content": f"[ProcessRunner Error {error['code']}] {error['message']}",
                    "is_error": True,
                    "metadata": error.get("data", {}),
                }
            
            return response["result"]
        
        except asyncio.TimeoutError:
            return {
                "content": f"[ProcessRunner] 执行超时 ({timeout}s)",
                "is_error": True,
                "metadata": {"type": "TimeoutError"},
            }
        except Exception as e:
            return {
                "content": f"[ProcessRunner] 执行失败: {e}",
                "is_error": True,
                "metadata": {"type": type(e).__name__},
            }
        finally:
            if not self._persistent:
                await self._cleanup_process(proc)
    
    async def _create_process(self) -> asyncio.subprocess.Process:
        """启动子进程。"""
        return await asyncio.create_subprocess_exec(
            sys.executable, "-m", "myagent.tools.process_runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    
    async def _get_or_create_process(self, tool_entry: str) -> asyncio.subprocess.Process:
        """获取或创建常驻进程。"""
        if tool_entry not in self._processes or self._processes[tool_entry].returncode is not None:
            self._processes[tool_entry] = await self._create_process()
        return self._processes[tool_entry]
    
    async def _cleanup_process(self, proc: asyncio.subprocess.Process) -> None:
        """清理子进程。"""
        try:
            proc.stdin.close()
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:
            proc.kill()
    
    async def shutdown(self) -> None:
        """关闭所有常驻进程。"""
        for proc in self._processes.values():
            await self._cleanup_process(proc)
        self._processes.clear()
```

#### 与 ToolExecutor 的集成

```python
# executor.py 修改

async def execute(self, tool_call: ToolCall, ...) -> ToolResult:
    tool = self._registry.get(tool_call.name)
    ...
    meta = tool.meta or ToolMeta()
    
    if meta.get("run_in_process", False):
        result_dict = await self._process_runner.run_tool(
            tool_entry=tool.entry_point,
            arguments=args,
            timeout=meta.get("process_timeout", meta.get("timeout", 60.0)),
            meta=meta.to_dict(),
        )
        result = ToolResult(
            content=result_dict["content"],
            is_error=result_dict.get("is_error", False),
            metadata=result_dict.get("metadata", {}),
        )
    else:
        result = await asyncio.wait_for(
            tool.execute(**args),
            timeout=meta.get("timeout", self._default_timeout),
        )
```

**渐进式迁移路径：**

```
Phase 1（当前）: 所有工具在主进程执行
    ↓ config/tool_meta.yaml 中 run_in_process: true
Phase 2: 标记的工具在本地子进程执行（JSON-RPC over stdio）
    ↓ 替换 ProcessToolRunner transport 层
Phase 3: 工具在远程容器中执行（JSON-RPC over TCP/HTTP）
    ↓ 替换为 MCP Server 协议
Phase 4: 完整 MCP Server 模式
```

### 3.5 MCP 兼容层 `mcp_compat.py`

```python
# mcp_compat.py — 接口预留 + 骨架实现

"""
MCP (Model Context Protocol) 兼容层。
当前为接口预留，未来实现时只需填充此文件，不影响其他模块。

兼容原理：
  MyAgent 的 BaseTool.parameters_schema 与 MCP inputSchema 格式一致（标准 JSON Schema），
  因此 MCP 工具可以直接包装为 BaseTool，走统一的 Registry/Executor 流水线。
  
  MCP 工具的元数据同样通过 config/tool_meta.yaml 配置，无需硬编码。
"""

from myagent.tools.base import BaseTool, ToolResult, ToolMeta

class MCPTool(BaseTool):
    """将 MCP Server 工具适配为 BaseTool。（预留骨架）"""
    
    def __init__(self, server_name: str, tool_name: str, 
                 description: str, input_schema: dict):
        self.name = f"mcp_{server_name}_{tool_name}"
        self.description = f"[MCP:{server_name}] {description}"
        self.parameters_schema = input_schema  # 格式完全一致，零转换
        # 元数据从配置文件加载，MCP 工具在配置中 source=mcp
        self.meta = ToolMeta.load(self.name)
        self._server_name = server_name
        self._tool_name = tool_name
    
    async def execute(self, **kwargs) -> ToolResult:
        # TODO: 实现 MCP tool call（通过 MCP Client 发送请求）
        # 当前 MCP 协议使用 JSON-RPC 2.0，与 ProcessRunner 的通信协议兼容
        # 实现 时只需：
        #   1. 通过 MCPClientManager 发送 tools/call 请求
        #   2. 将 MCP 响应转换为 ToolResult
        raise NotImplementedError(
            "MCP tool execution not yet implemented. "
            "See mcp_compat.py for integration plan."
        )

class MCPClientManager:
    """
    MCP Client 连接管理器。（预留骨架）
    
    未来实现时负责：
      - 管理 MCP Server 连接（stdio / SSE / HTTP）
      - 发现远程工具（tools/list）
      - 转发工具调用（tools/call）
      - 连接生命周期管理（心跳、重连）
    """
    
    async def connect(self, server_url: str, transport: str = "stdio") -> None:
        """连接 MCP Server。transport: stdio | sse | http"""
        raise NotImplementedError("MCP client not yet implemented")
    
    async def list_tools(self, server_name: str) -> list[MCPTool]:
        """发现远程工具，返回 MCPTool 列表。"""
        raise NotImplementedError("MCP client not yet implemented")
    
    async def call_tool(self, server_name: str, tool_name: str, 
                        arguments: dict) -> ToolResult:
        """调用远程工具，返回 ToolResult。"""
        raise NotImplementedError("MCP client not yet implemented")
    
    async def disconnect(self, server_name: str) -> None:
        """断开连接。"""
        raise NotImplementedError("MCP client not yet implemented")
```

### 3.6 精简合并的具体改动

#### ① `wrapper.py` → 合并到 `base.py`

`FunctionTool` 和 `make_tool` 本质是 BaseTool 的工厂方法，不需要独立文件：

```python
# base.py 末尾追加

class FunctionTool(BaseTool):
    """将 async callable 自动包装为 BaseTool。"""
    
    def __init__(self, func, *, name=None, description=None):
        from myagent.tools.schema import generate_schema, extract_description
        self.name = name or func.__name__
        self.description = description or extract_description(func) or self.name
        self.parameters_schema = generate_schema(func)
        # 元数据延迟加载：先尝试从配置文件读取，无则用默认值
        self.meta = None  # _ensure_meta() 在首次需要时加载
        self._func = func
    
    async def execute(self, **kwargs) -> ToolResult:
        result = await self._func(**kwargs)
        return result if isinstance(result, ToolResult) else ToolResult(content=str(result))

def make_tool(func=None, *, name=None, description=None):
    """工具装饰器 / 工厂函数。"""
    if func is not None:
        return FunctionTool(func, name=name, description=description)
    def decorator(f):
        return FunctionTool(f, name=name, description=description)
    return decorator
```

#### ② `idempotency.py` → 合并到 `executor.py`

`IdempotencyCache` 只在 `ToolExecutor` 中使用，且只有 72 行，合并后减少一个文件和一层 import。

#### ③ `secrets.py` → 移到 `myagent/safety/`

密钥管理属于安全职责范畴，与 SafetyGuard、PolicyEngine 同属 `safety` 模块。

移动后影响范围：
- `myagent/factory.py`：import 路径从 `myagent.tools.secrets` 改为 `myagent.safety.secrets`
- `myagent/tools/executor.py`：通过构造注入使用，移动文件不影响接口
- `myagent/tools/__init__.py`：移除 `SecretManager` 的导出

#### ④ `sandbox/` → 移到 `myagent/runtime/sandbox/`

沙盒是执行环境概念，未来不仅 CLITool 使用，ProcessToolRunner 也可能需要。

移动后影响范围：
- `myagent/factory.py`：import 路径从 `myagent.tools.sandbox` 改为 `myagent.runtime.sandbox`
- `myagent/tools/builtin/cli_tool.py`：import 路径相应修改

#### ⑤ 内置工具移入 `builtin/`

`cli_tool.py`、`file_tools.py` 移入 `builtin/` 子目录，与框架基础设施代码分离。

#### ⑥ `tools_store/` 改为子目录结构

从"每个工具一个 .py 文件"改为"每个工具一个目录"：

```
tools_store/
├── weather/
│   ├── weather_tool.py    # 工具入口
│   └── meta.yaml          # 元数据（可选）
└── search/
    ├── search_tool.py     # 工具入口
    ├── helpers.py         # 辅助模块
    └── meta.yaml          # 元数据
```

**`ToolLoader` 适配**：发现逻辑从"扫描 .py 文件"改为"扫描子目录 → 查找入口 .py 文件"：

```python
# loader.py — ToolLoader._discover_tools() 修改

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
        if not item.is_dir() or item.name.startswith('_'):
            continue
        
        # 查找入口 .py 文件
        py_files = [f for f in item.glob("*.py") if not f.name.startswith('_')]
        if not py_files:
            continue
        
        # 检查是否有 meta.yaml
        meta_path = item / "meta.yaml"
        
        tools.append({
            "name": item.name,
            "dir": item,
            "entry_files": py_files,
            "meta_path": meta_path if meta_path.exists() else None,
        })
    
    return tools
```

热加载时元数据加载方式：

```python
# loader.py — 加载工具时自动关联元数据

tool_meta = ToolMeta.load_for_hot_reload(
    tool_name=function_name,
    tool_dir=str(tool_dir),
)
function_tool.meta = tool_meta
```

### 3.7 修正 `BaseTool.meta` 的可变类属性问题

```python
# 修正前
class BaseTool(ABC):
    meta: dict = {}  # 共享可变对象！

# 修正后
class BaseTool(ABC):
    meta: ToolMeta | None = None  # 延迟加载，首次访问时从配置文件读取
    
    def _ensure_meta(self) -> ToolMeta:
        """确保 meta 已加载，未加载时从配置文件读取。"""
        if self.meta is None:
            self.meta = ToolMeta.load(self.name)
        return self.meta
```

---

## 四、实施优先级

| 优先级 | 任务 | 影响范围 | 复杂度 |
|--------|------|----------|--------|
| **P0** | 修复 `BaseTool.meta` 可变类属性 → 改为 `ToolMeta \| None` + 延迟加载 | base.py | 低 |
| **P0** | 修复 `get_tool_schemas()` 命名 | executor.py, loop.py | 低 |
| **P0** | 修复 weather_tool 同步阻塞 | weather_tool.py | 低 |
| **P1** | 创建 `config/tool_meta.yaml` + 实现 `ToolMeta` 配置加载 | base.py, config/ | 中 |
| **P1** | 合并 `wrapper.py` 到 `base.py` | base.py, __init__.py | 低 |
| **P1** | 合并 `idempotency.py` 到 `executor.py` | executor.py | 低 |
| **P1** | 移动 `secrets.py` 到 `myagent/safety/` | factory.py, executor.py, safety/ | 低 |
| **P1** | 移动 `sandbox/` 到 `myagent/runtime/sandbox/` | factory.py, cli_tool.py | 低 |
| **P1** | 内置工具移入 `builtin/` | factory.py, __init__.py | 低 |
| **P1** | `tools_store/` 改为子目录结构 + `ToolLoader` 适配 | loader.py, tools_store/ | 中 |
| **P2** | 实现 `ProcessToolRunner`（JSON-RPC over stdio） | 新文件 + executor.py | 高 |
| **P2** | 添加 `mcp_compat.py` 骨架 | 新文件 + factory.py | 中 |
| **P3** | 修正 ToolRegistry docstring | registry.py | 低 |
| **P3** | SecretManager 内存泄漏修复（TTL + LRU 淘汰） | safety/secrets.py | 低 |

---

## 五、精简前后对比

### 文件数量

| 指标 | 现在 | 精简后 |
|------|------|--------|
| tools/ 下的 .py 文件 | 11 | 9 (-2) |
| 新增文件 | — | 3 (process_runner, mcp_compat, config/tool_meta.yaml) |
| 删除文件 | — | 3 (wrapper, idempotency, secrets移出) |
| 移出文件 | — | 1 (sandbox → runtime/) |
| 新增目录 | — | 2 (builtin/, runtime/sandbox/) |
| __init__.py 导出符号 | 14 | 10 |

### 关键变化

```
删除:
  - wrapper.py        → 合并到 base.py（FunctionTool + make_tool）
  - idempotency.py    → 合并到 executor.py

移出 tools/:
  - secrets.py        → myagent/safety/secrets.py（安全职责归位）
  - sandbox/          → myagent/runtime/sandbox/（执行环境独立）

新增:
  + process_runner.py  — JSON-RPC 进程隔离执行器
  + mcp_compat.py      — MCP 兼容层骨架
  + builtin/           — 内置工具子目录（cli_tool, file_tools）
  + config/tool_meta.yaml — 工具元数据配置文件

重构:
  ~ base.py            — 新增 ToolMeta（配置驱动）, FunctionTool, make_tool
  ~ executor.py        — 合并 IdempotencyCache, 集成 ProcessToolRunner
  ~ registry.py        — 修正 docstring
  ~ loader.py          — 适配 tools_store 子目录结构 + ToolMeta 加载
  ~ tools_store/       — 从单文件改为每工具一个子目录

配置驱动（不硬编码）:
  ● ToolMeta 字段完全由 config/tool_meta.yaml 定义
  ● 新增/删除/修改元数据只需改 YAML，不改 Python 代码
  ● 热加载工具支持目录级 meta.yaml 覆盖
  ● 运行时支持 ToolMeta.merge() 动态覆盖
```

### 架构改进总结

| 维度 | 改进前 | 改进后 |
|------|--------|--------|
| 元数据 | `dict = {}` 硬编码在代码中 | YAML 配置文件驱动，三层合并 |
| 进程隔离 | 无，工具在主进程直接执行 | JSON-RPC over stdio，支持常驻/临时进程 |
| 沙盒位置 | `tools/sandbox/`（职责耦合） | `runtime/sandbox/`（执行环境独立） |
| 密钥管理 | `tools/secrets.py`（职责错位） | `safety/secrets.py`（安全归位） |
| 热加载工具 | 单 .py 文件，无辅助空间 | 独立目录 + meta.yaml + 辅助文件 |
| MCP 兼容 | 仅 TODO 注释 | 接口预留 + 骨架代码 |