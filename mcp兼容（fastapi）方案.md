# MyAgent × FastMCP 集成兼容方案

## 一、FastMCP 软件包研究

### 1.1 基本信息

| 项目 | 说明 |
|------|------|
| **名称** | FastMCP |
| **维护方** | Prefect 团队 |
| **定位** | MCP (Model Context Protocol) 生态的标准 Python 框架 |
| **版本** | 3.x (主分支) |
| **安装** | `pip install fastmcp` |
| **文档** | https://gofastmcp.com |
| **代码仓库** | https://github.com/jlowin/fastmcp |
| **日下载量** | 100 万+ |
| **市场份额** | 占所有 MCP 服务器的 70%（含各语言） |
| **License** | Apache 2.0 |

### 1.2 核心能力

FastMCP 提供三大支柱能力：

#### Server — 构建 MCP 服务端

```python
from fastmcp import FastMCP

mcp = FastMCP("MyServer")

@mcp.tool
def add(a: int, b: int) -> int:
    """Add two numbers"""
    return a + b

@mcp.resource("data://config")
def get_config() -> dict:
    return {"theme": "dark"}

@mcp.prompt
def analyze_data(data_points: list[float]) -> str:
    return f"Please analyze these data points: {data_points}"

if __name__ == "__main__":
    mcp.run()                          # stdio transport (默认)
    # mcp.run(transport="http", port=8000)  # HTTP transport
```

**关键特性：**
- 装饰器风格，自动生成 JSON Schema
- 支持 Tools / Resources / Prompts 三种组件
- 支持 stdio / HTTP (Streamable HTTP) / SSE 传输
- Pydantic 参数校验（严格/宽松模式可选）
- 认证（OAuth / Token 验证）、中间件、生命周期钩子
- 可通过 `fastmcp run server.py:mcp` CLI 启动
- 可与 FastAPI/Starlette 深度集成（`app.mount("/mcp", mcp.get_asgi_app())`）

#### Client — 连接 MCP 服务端

```python
from fastmcp import Client

# 三种连接方式
client = Client(server_instance)           # 内存模式（测试用）
client = Client("http://example.com/mcp")  # HTTP 远程
client = Client("my_server.py")            # STDIO 子进程

async with client:
    tools = await client.list_tools()
    result = await client.call_tool("greet", {"name": "World"})
    resources = await client.list_resources()
    prompts = await client.list_prompts()
```

**关键特性：**
- 自动推断传输方式（HTTP/STDIO/内存）
- 异步上下文管理器，自动处理协议握手
- 支持回调：日志、进度、采样、用户交互
- 支持多服务器配置（Claude Desktop 风格）
- 工具名自动前缀隔离

#### Apps — 交互式 UI

工具返回 Prefab UI 组件（图表、表格、表单等），在对话中直接渲染可视化界面。

### 1.3 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│                      FastMCP Runtime                         │
│                                                              │
│  Transport Layer:  stdio | HTTP | SSE                       │
│  Protocol Layer:   JSON-RPC 2.0 (MCP spec)                  │
│  Auth Layer:       OAuth | Token | None                     │
│  Middleware:       Request/Response/Notification hooks      │
│  Components:       Tools | Resources | Prompts              │
│  Storage:          SessionStateStore (Key-Value)            │
│  Lifespan:         Startup/Shutdown hooks                   │
└─────────────────────────────────────────────────────────────┘
```

### 1.4 与 MCP Python SDK 的关系

- FastMCP 1.0 在 2024 年被纳入官方 MCP Python SDK
- 当前独立维护的 FastMCP (3.x) 是官方 SDK 的"升级替代品"
- 提供了更简洁的 API、更好的性能、更丰富的功能

---

## 二、MyAgent 项目 MCP 兼容现状

### 2.1 现有设计预留点

代码中已有多处 `[MCP]` 标记，说明 MCP 集成是计划内目标：

| 文件 | 位置 | 预留内容 |
|------|------|----------|
| `myagent/factory.py` | L11, L20 | 注释："MCP Tools 将在此处注册到 ToolRegistry" |
| `myagent/factory.py` | L249 | "MCP 工具的沙盒可能需要不同的隔离策略" |
| `myagent/factory.py` | L272-276 | 伪代码：`from myagent.tools.mcp_tool import MCPTool` |
| `myagent/interfaces/web/app.py` | L18 | "`/mcp` 端点暴露 MCP 协议（SSE transport）" |
| `myagent/interfaces/web/dependencies.py` | L6 | "注入 MCP 客户端管理器" |
| `myagent/interfaces/web/routes/health.py` | L6 | "添加 `/api/mcp/status` 端点" |
| `myagent/interfaces/web/ws_models.py` | L7 | "可添加 MCP 协议相关的消息类型" |

### 2.2 无需引入的架构约束

MyAgent 的现有架构天然适合 MCP 集成：

1. **Tool 抽象层** (`BaseTool` / `ToolRegistry`) — MCP 工具可直接适配为 `BaseTool` 子类，走统一的 `ToolExecutor` 流水线
2. **SafetyGuard 安全体系** — MCP 工具同样受 PolicyEngine + CLIFence + ContentFilter 约束
3. **Hook 事件系统** — MCP 工具的执行可触发相同的生命周期事件
4. **AgentFactory 组合根** — 在 `_build_tool_registry()` 中集中注入 MCP 工具

---

## 三、集成方案设计

### 3.1 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                        MyAgent + FastMCP                         │
│                                                                  │
│   ┌──────────────┐          MCP Client 模式                      │
│   │  外部 MCP    │ ────► MCPClientManager ───► MCPTool           │
│   │  Server #1   │      (连接池 / 生命周期)    (BaseTool 适配)   │
│   │  (天气 API)  │                           │                   │
│   ├──────────────┤                           ▼                   │
│   │  外部 MCP    │ ────►  ┌──────────────────────────────────┐  │
│   │  Server #2   │        │         ToolRegistry             │  │
│   │  (数据库)    │        │   CLITool + FileTools + MCPTools │  │
│   ├──────────────┤        │         (统一注册表)             │  │
│   │  外部 MCP    │        └───────────────┬──────────────────┘  │
│   │  Server #3   │                        │                      │
│   └──────────────┘                        ▼                      │
│                                  ToolExecutor                     │
│                         (Safety → Idempotency → Execute)         │
│                                      │                           │
│                                      ▼                           │
│                                  AgentLoop                        │
│                              (ReAct 循环)                        │
│                                                                  │
│   ┌──────────────────────────────────────────────────────────┐  │
│   │                   MCP Server 模式                        │  │
│   │                                                          │  │
│   │   FastAPI (/ws + /api/*)      FastMCP Server (/mcp)      │  │
│   │        │                            │                    │  │
│   │        ▼                            ▼                    │  │
│   │   MyAgent Web UI            外部 MCP Client               │  │
│   │   (浏览器实时对话)          (Claude Desktop / Cursor)    │  │
│   └──────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 推荐实施路径：Client 优先，Server 增强

| 阶段 | 内容 | 工期 | 优先级 |
|------|------|------|--------|
| **Phase 1** | MCP Client 集成 | 3-4 天 | 🔴 高 — 核心价值 |
| **Phase 2** | MCP Server 集成 | 1-2 天 | 🟡 中 — 生态互操作 |
| **Phase 3** | 动态发现 + 热更新 | 后续 | 🟢 低 — 锦上添花 |

### 3.3 Phase 1: MCP Client 集成（核心价值）

**目标：** 让 MyAgent 的 Agent 能够调用外部 MCP 服务器的工具。

**价值分析：**
- 无需手写工具代码即可接入天气预报、搜索引擎、数据库查询、代码托管等外部能力
- MCP 生态已有数千个可用服务器，一次接入全部可用
- 保持 MyAgent 的安全体系完整覆盖，外部工具与内置工具统一安全管理

#### 3.3.1 新增文件

```
myagent/tools/mcp/
├── __init__.py              # 模块导出
├── config.py                # Pydantic 配置模型
├── client_manager.py        # MCP 客户端连接生命周期管理
└── mcp_tool.py              # MCP 工具 → BaseTool 适配器
```

#### 3.3.2 配置模型 (`config.py`)

```python
from pydantic import BaseModel
from typing import Literal, Optional

class MCPServerConfig(BaseModel):
    """单个 MCP 服务器的配置"""
    name: str                              # 逻辑名称 (用作前缀)
    transport: Literal["http", "stdio"]    # 传输方式
    url: Optional[str] = None              # HTTP: MCP 端点 URL
    command: Optional[str] = None          # STDIO: 启动命令
    args: list[str] = []                   # STDIO: 命令参数
    env: dict[str, str] = {}               # STDIO: 环境变量
    headers: dict[str, str] = {}           # HTTP: 请求头 (支持 ${ENV} 引用)
    timeout: float = 30.0                  # 调用超时（秒）
    enabled: bool = True                   # 是否启用
    tags: list[str] = []                   # 标签过滤

class MCPConfig(BaseModel):
    """MCP 总配置"""
    enabled: bool = False
    servers: list[MCPServerConfig] = []
```

#### 3.3.3 连接管理器 (`client_manager.py`)

```python
from fastmcp import Client
from typing import AsyncIterator

class MCPClientManager:
    """
    管理到多个 MCP 服务器的 FastMCP Client 连接。
    
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
        """并行连接所有已启用的 MCP 服务器"""
        ...

    async def disconnect_all(self) -> None:
        """关闭所有客户端连接"""
        ...

    async def list_all_tools(self) -> list[MCPToolInfo]:
        """列出所有服务器的工具（带 server_name 前缀）"""
        ...

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict
    ) -> ToolResult:
        """调用指定服务器的工具"""
        ...

    def health_check(self) -> dict[str, bool]:
        """检查各服务器连接状态"""
        ...
```

**设计要点：**

1. **连接管理**：每个 MCP 服务器一个独立的 `Client` 实例，并行初始化
2. **命名空间**：工具名自动加 `mcp_{server_name}_` 前缀，避免冲突
3. **容错**：单个服务器连接失败不影响其他服务器和 Agent 整体
4. **安全**：所有 MCP 工具调用走 MyAgent 的 SafetyGuard 流水线
5. **超时**：统一的 `asyncio.wait_for` 超时控制，防止 MCP 调用阻塞对话

#### 3.3.4 工具适配器 (`mcp_tool.py`)

```python
class MCPTool(BaseTool):
    """
    将单个 MCP 工具包装为 MyAgent BaseTool。
    
    关键设计：
    - 动态 name/description/parameters_schema（从 MCP list_tools 获取）
    - execute() 委托给 MCPClientManager.call_tool()
    - 与内置工具走相同的 SafetyGuard → Executor 流水线
    - meta 字段标记来源为 "mcp:{server_name}"
    """

    def __init__(self, client_manager: MCPClientManager, 
                 server_name: str, tool_info: MCPToolInfo):
        self.name = f"mcp_{server_name}_{tool_info.name}"
        self.description = f"[MCP:{server_name}] {tool_info.description}"
        self.parameters_schema = tool_info.inputSchema
        self.meta = {
            "source": "mcp",
            "server": server_name,
            "original_name": tool_info.name,
        }
        # 内部引用
        self._client_manager = client_manager
        self._server_name = server_name
        self._tool_name = tool_info.name

    async def execute(self, **kwargs) -> ToolResult:
        """委托给 MCP 服务器执行"""
        try:
            result = await self._client_manager.call_tool(
                self._server_name, self._tool_name, kwargs
            )
            return ToolResult(content=..., metadata={...})
        except Exception as e:
            return ToolResult(content=..., is_error=True)
```

#### 3.3.5 配置示例 (`config.yaml` 新增)

```yaml
agent:
  # ... 现有配置 ...

  mcp:
    enabled: true
    servers:
      # HTTP transport 示例
      - name: "weather"
        transport: "http"
        url: "http://localhost:9000/mcp"
        tags: ["public"]

      # STDIO transport 示例
      - name: "filesystem"
        transport: "stdio"
        command: "python"
        args: ["./mcp_servers/filesystem_server.py"]
        env:
          ROOT_DIR: "/tmp/mcp_workspace"

      # 带认证的远程服务
      - name: "database"
        transport: "http"
        url: "https://db-mcp.example.com/mcp"
        headers:
          Authorization: "Bearer ${MCP_DB_TOKEN}"
        timeout: 15.0
```

#### 3.3.6 集成到 AgentFactory

在 `AgentFactory._build_tool_registry()` 中（约 L269-283），注册完内置工具后接入 MCP 工具：

```python
async def _build_tool_registry(self, sandbox) -> ToolRegistry:
    tool_registry = ToolRegistry()
    tool_registry.register(CLITool(sandbox))
    tool_registry.register(FileReadTool())
    tool_registry.register(FileWriteTool())

    # ── [MCP] 注册 FastMCP 协议工具 ──
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
            logger.info(f"Registered {len(servers)} MCP server(s) with "
                        f"{len(tool_registry) - 3} external tools")

    return tool_registry
```

#### 3.3.7 安全集成

MCP 工具与内置工具走相同的安全流水线，无需额外开发：

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

### 3.4 Phase 2: MCP Server 集成（生态互操作）

**目标：** 将 MyAgent 的高质量工具集（CLI 沙盒、文件读写）暴露为 MCP 标准工具，供外部 MCP 客户端使用。

**典型使用者：** Claude Desktop、Cursor IDE、Continue.dev、Zed、VS Code Copilot 等。

#### 3.4.1 实现方式

利用 FastMCP 的 FastAPI/Starlette 集成能力，在现有 FastAPI 应用上挂载一个 MCP 端点：

```python
# 在 myagent/interfaces/web/app.py 的 create_app() 中

from fastmcp import FastMCP

def create_app(config_path="config.yaml"):
    app = FastAPI(...)
    # ... 现有路由和中间件 ...

    # ── [MCP] MCP Server 端点 ──
    mcp = FastMCP(
        "MyAgent",
        instructions="由 MyAgent 框架提供的智能体工具集：CLI 沙盒执行、文件读写操作"
    )

    # 注册工具：将 MyAgent 的 BaseTool 包装为 FastMCP tool
    agent_factory = get_agent_factory()
    sandbox = agent_factory._build_sandbox()

    @mcp.tool
    async def cli_execute(command: str) -> str:
        """执行安全的 CLI 命令（受沙盒和安全策略约束）"""
        ...

    @mcp.tool
    async def file_read(path: str) -> str:
        """读取指定文件的内容"""
        ...

    @mcp.tool
    async def file_write(path: str, content: str) -> str:
        """写入内容到指定文件"""
        ...

    # 挂载到 FastAPI
    app.mount("/mcp", mcp.get_asgi_app())

    # ── [MCP] MCP 状态端点 ──
    @app.get("/api/mcp/status")
    async def mcp_status():
        return {"mcp_server": "running", "tools": len(mcp._tool_manager._tools)}

    return app
```

#### 3.4.2 Security 考虑

- MCP Server 暴露时走完整的安全流水线（CLIFence、PolicyEngine）
- 生产环境需要认证中间件保护 MCP 端点
- 可通过 `tags` 控制哪些工具对 MCP 可见

---

### 3.5 Phase 3: 未来增强（可选）

| 特性 | 说明 |
|------|------|
| **MCP Discovery** | 自动发现 Prefect Horizon 上的 MCP 服务 |
| **热更新** | CLI 命令 `/mcp reload` 动态重载 MCP 工具 |
| **MCP 工具可见性控制** | 通过 `tags` + `enable/disable` 精细控制哪些 MCP 工具对 Agent 可见 |
| **MCP 工具缓存** | 首次调用后缓存 schema，避免每次初始化都 list_tools |
| **MCP 服务状态面板** | 在 Web UI 侧边栏显示各 MCP 服务连接状态 |

---

## 四、风险评估与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| **MCP Server 连接不稳定** | Agent 启动失败或工具调用超时 | ① 懒加载 + 健康检查 ② 连接失败不影响其他 Server 和 Agent 整体 ③ 超时机制兜底 |
| **MCP 工具 Schema 不兼容** | 工具注册失败 | ① `MCPTool` 做 schema 格式校验和转换 ② 跳过不兼容的工具并记录日志 ③ 兼容 JSON Schema Draft-07 |
| **MCP 工具执行超时** | 阻塞整个 ReAct 循环 | ① 复用现有的 `asyncio.wait_for(..., timeout)` ② 可在 `MCPServerConfig` 中配置 per-server 超时 |
| **工具名称冲突** | LLM 调错工具 | ① MCP 工具名统一加 `mcp_{server_name}_` 前缀 ② 在 description 中明确标注来源 |
| **安全边界被 MCP 突破** | 外部工具执行危险操作 | ① MCP 工具与内置工具走相同的 SafetyGuard 流水线 ② 可在 PolicyEngine 中为 MCP 工具指定更严格的默认策略 |
| **依赖冲突** | fastmcp 与现有依赖不兼容 | ① `fastmcp` 的核心依赖与 MyAgent 高度重合（pydantic、httpx 等）② 使用 `[mcp]` optional dependency 隔离 |
| **多 Server 并发连接** | 初始化时 IO 风暴 | ① 并行初始化但限制并发数 ② 懒加载：Agent 启动后再逐步连接 |

---

## 五、依赖分析

### 5.1 FastMCP 核心依赖

| 依赖 | MyAgent 现状 | 兼容性 |
|------|-------------|--------|
| `pydantic>=2.0` | ✅ 已安装 (>=2.0) | 完全兼容 |
| `httpx>=0.27` | ✅ 已安装 (>=0.27) | 完全兼容 |
| `starlette` | ✅ 已安装 (FastAPI 依赖) | 完全兼容 |
| `uvicorn` | ✅ 已安装 (>=0.30) | 完全兼容 |
| `rich>=13.0` | ✅ 已安装 (>=13.0) | 完全兼容 |
| `cloudpickle` | ❌ 未安装 | pip install 自动安装 |
| `anyio` | ✅ (FastAPI 间接依赖) | 完全兼容 |

### 5.2 推荐配置

```toml
# pyproject.toml 新增
[project.optional-dependencies]
mcp = ["fastmcp>=3.0.0"]
dev = ["pytest", "pytest-asyncio", "pytest-cov", "ruff"]
```

---

## 六、实施检查清单

### Phase 1: MCP Client

- [ ] 创建 `myagent/tools/mcp/` 模块目录
- [ ] 实现 `MCPServerConfig` / `MCPConfig` Pydantic 模型 (`config.py`)
- [ ] 实现 `MCPClientManager` — 连接池、工具列表、调用路由 (`client_manager.py`)
- [ ] 实现 `MCPTool` — BaseTool 适配器 (`mcp_tool.py`)
- [ ] 扩展 `config.yaml.example` 增加 MCP 配置段说明
- [ ] 修改 `AgentFactory._build_tool_registry()` 接入 MCP 工具
- [ ] 修改 `AgentFactory.create_agent()` 传递 MCP 状态到 Agent
- [ ] 确保 MCP 工具走完整的 SafetyGuard 流水线
- [ ] 添加 `/api/mcp/status` 健康检查端点
- [ ] 添加 `pyproject.toml` 的 `[mcp]` optional dependency

### Phase 2: MCP Server

- [ ] 在 `create_app()` 中创建 FastMCP Server 实例
- [ ] 包装 MyAgent 内置工具为 FastMCP tools
- [ ] 挂载 `/mcp` 端点到 FastAPI
- [ ] 考虑认证中间件保护 MCP 端点
- [ ] 测试与 Claude Desktop 的互通

### Phase 3: 集成测试

- [ ] 单元测试：MCPTool 的 execute / schema 转换
- [ ] 集成测试：MCPClientManager 连接真实 MCP 服务器
- [ ] 端到端测试：Agent 调用 MCP 工具的完整 ReAct 流程
- [ ] 安全测试：MCP 工具被 SafetyGuard 拦截的行为

---

## 七、总结

FastMCP 是 MCP 生态最成熟、最广泛的 Python 框架，与 MyAgent 的架构天然契合：

1. **Client 模式（高优先级）**：3-4 天实现，让 MyAgent 的 Agent 即刻获得数千个 MCP 服务器的能力——这是最大的杠杆效应。通过 `MCPClientManager` + `MCPTool` 适配器，外部工具无缝融入现有的 ToolRegistry、ToolExecutor、SafetyGuard 体系。

2. **Server 模式（中优先级）**：1-2 天实现，将 MyAgent 的 CLI 沙盒和文件操作暴露为 MCP 标准，让 Claude Desktop 等客户端可以调用。利用 FastMCP 的 FastAPI 集成，挂载到现有 Web 服务上。

3. **架构优势**：MyAgent 的 `BaseTool` 抽象、`SafetyGuard` 责任链、`ToolExecutor` 流水线、`HookManager` 事件系统——这些设计天然支持 MCP 工具的接入，无需重构核心逻辑。

4. **风险可控**：通过命名空间前缀、超时控制、失败隔离、安全策略覆盖，MCP 工具的引入不会削弱 MyAgent 的安全性和稳定性。
