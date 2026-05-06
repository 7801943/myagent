# MyAgent — 全自研 Python Agent 框架架构设计方案（终版）

## 概述

本文档描述一个**完全自研、生产级、全异步**的 Python Agent 框架，不依赖 LangChain、LlamaIndex 等第三方 Agent 框架。从 OpenAI / Anthropic 原始 HTTP 接口出发，构建完整的智能体能力栈。

### 已确认设计决策

| 决策项 | 选择 | 备注 |
|--------|------|------|
| CLI 沙盒 | `subprocess + ulimit` | 预留 Docker 接口，通过 `--sandbox-backend` 参数切换 |
| 图像输入 | 多模态 LLM 原生支持 | Provider 能力检测决定是否启用；不集成 OCR |
| WebSocket 框架 | 纯 `websockets` 库 | 轻量，无额外 Web 框架依赖 |
| SubAgent | 完整设计，轻量实现 | 复用 AgentLoop，通过 SpawnTool 触发，独立上下文 |
| 日志与审计 | 分级 + 多后端 + 异步队列 | AuditLevel 四级粒度，JSONL/SQLite/Postgres 后端，通过 AuditHook 无侵入集成 |

---

## 设计原则

| 原则 | 说明 |
|------|------|
| **零框架依赖** | 只依赖 LLM 官方 SDK、异步 HTTP 库、基础工具库 |
| **层次清晰** | 严格分层，层间通过抽象接口通信，禁止跨层直接调用 |
| **全异步** | 所有 I/O 操作使用 `asyncio`，无阻塞 |
| **可插拔** | Provider、Tool、Skill、Safety Rule、Interface 均支持插件化扩展 |
| **渐进增强** | 核心功能稳健，高级功能（上下文压缩、RAG、IM接口）以预留接口方式占位 |

---

## 整体架构分层

```
┌──────────────────────────────────────────────────────────────────┐
│                        Interface Layer                            │
│     CLI (Rich streaming)  │  WebSocket (websockets)  │  IM(预留) │
├──────────────────────────────────────────────────────────────────┤
│                        Agent Core                                 │
│   AgentLoop  │  StreamProcessor  │  StructuredOutputParser        │
│   AgentHook(生命周期钩子) │ CompositeHook │ HookContext           │
│   SubAgentManager  │  SpawnTool  │  CLIProgressHook               │
├───────────────────────────┬──────────────────────────────────────┤
│      Context Manager      │           Skill Engine                │
│  (messages, multimodal,   │  (YAML/MD skills, SkillRegistry,      │
│   future compression)     │   composed workflows)                 │
├───────────────────────────┴──────────────────────────────────────┤
│                        Tool System                                │
│  ToolRegistry │ ToolExecutor │ CLITool │ PySandbox │ FileTools    │
│  DocumentTools │ VectorRAGTool(预留)                              │
├──────────────────────────────┬───────────────────────────────────┤
│        Safety System         │      Document Processor            │
│  GuardChain │ CLIFence(ulimit│  DOCX │ XLSX │ PDF(pdfplumber)    │
│  /subprocess) │ ContentFilter│  ImageHandler(多模态 base64/URL)  │
├──────────────────────────────┴───────────────────────────────────┤
│                       Provider Layer                              │
│  ProviderRouter(failover) │ OpenAIProvider │ AnthropicProvider    │
│  UnifiedStreamEvent │ RetryPolicy │ TimeoutManager                │
│  CapabilityDetector(多模态/工具能力检测)                          │
├──────────────────────────────────────────────────────────────────┤
│               Vector / RAG Interface（预留）                      │
│         BaseVectorStore │ RAGSkill │ EmbeddingProvider            │
├──────────────────────────────────────────────────────────────────┤
│          Observability Layer — 日志与审计（横切所有层）            │
│  AuditLogger │ AuditLevel(4级粒度) │ AuditHook(非侵入集成)        │
│  JSONLBackend │ SQLiteBackend │ PostgresBackend(预留)             │
│  AsyncEventQueue │ FieldMasker(PII脱敏) │ RetentionPolicy         │
└──────────────────────────────────────────────────────────────────┘
```

---

## 目录结构

```
myagent/
├── pyproject.toml
├── config/
│   ├── config.yaml              # 主配置（providers, models, timeouts, sandbox）
│   └── safety_rules.yaml        # 安全规则配置
├── myagent/
│   ├── __init__.py
│   │
│   ├── providers/               # ① LLM Provider 层
│   │   ├── base.py              #   BaseProvider, StreamEvent（统一流事件）
│   │   ├── openai_provider.py   #   OpenAI 流式实现
│   │   ├── anthropic_provider.py#   Anthropic 流式实现
│   │   ├── capability.py        #   CapabilityDetector（多模态/工具检测）
│   │   └── router.py            #   ProviderRouter（多路冗余 + Failover + 熔断）
│   │
│   ├── core/                    # ② Agent 核心
│   │   ├── agent.py             #   Agent 门面类（唯一公开入口）
│   │   ├── loop.py              #   AgentLoop（ReAct 执行循环）
│   │   ├── hook.py              #   AgentHook + CompositeHook + HookContext（完整生命周期钩子）
│   │   ├── stream.py            #   StreamProcessor（流事件聚合）
│   │   └── parser.py            #   StructuredOutputParser, ToolCallParser
│   │
│   ├── subagent/                # ③ SubAgent 子智能体系统
│   │   ├── __init__.py
│   │   ├── base.py              #   SubAgentSpec, SubAgentResult 数据模型
│   │   ├── manager.py           #   SubAgentManager（生命周期管理）
│   │   ├── runner.py            #   SubAgentRunner（复用 AgentLoop）
│   │   └── spawn_tool.py        #   SpawnTool（LLM 可调用的 spawn 工具）
│   │
│   ├── context/                 # ④ 上下文管理
│   │   ├── manager.py           #   ContextManager
│   │   ├── message.py           #   Message 数据模型（Pydantic，支持多模态）
│   │   └── compressor.py        #   ContextCompressor（预留接口）
│   │
│   ├── tools/                   # ⑤ 工具系统
│   │   ├── base.py              #   BaseTool, ToolResult
│   │   ├── registry.py          #   ToolRegistry
│   │   ├── executor.py          #   ToolExecutor（带安全前置检查）
│   │   ├── cli_tool.py          #   CLI 执行工具（安全围栏集成）
│   │   ├── sandbox/             #   沙盒抽象
│   │   │   ├── base.py          #     BaseSandbox（抽象接口，预留 Docker）
│   │   │   ├── subprocess_sandbox.py # subprocess + ulimit 实现
│   │   │   └── docker_sandbox.py     # Docker 实现（预留骨架）
│   │   └── file_tools.py        #   文件读写工具
│   │
│   ├── skills/                  # ⑥ Skill 系统
│   │   ├── base.py              #   BaseSkill, SkillResult
│   │   ├── registry.py          #   SkillRegistry（从 skills/ 目录加载）
│   │   ├── loader.py            #   YAML/MD skill 定义加载器
│   │   └── builtin/             #   内置 Skill
│   │       └── summarize.py
│   │
│   ├── safety/                  # ⑦ 安全系统（责任链）
│   │   ├── base.py              #   BaseRule, SafetyGuard
│   │   ├── cli_fence.py         #   CLI 安全围栏（白黑名单/路径限制）
│   │   ├── content_rules.py     #   内容安全规则（输入/输出过滤）
│   │   └── rules/               #   自定义规则插件目录
│   │
│   ├── documents/               # ⑧ 文档处理
│   │   ├── base.py              #   BaseDocumentProcessor
│   │   ├── docx_processor.py    #   Word 文档（python-docx）
│   │   ├── xlsx_processor.py    #   Excel 表格（openpyxl）
│   │   └── pdf_processor.py     #   PDF（pdfplumber / pymupdf）
│   │
│   ├── vision/                  # ⑨ 图像输入（多模态）
│   │   └── image_handler.py     #   ImageHandler（本地/URL/bytes → content block）
│   │
│   ├── vector/                  # ⑩ 向量检索接口（预留）
│   │   ├── base.py              #   BaseVectorStore, BaseEmbeddingProvider
│   │   └── rag_skill.py         #   RAG Skill（预留）
│   │
│   ├── interfaces/              # ⑪ 接口层
│   │   ├── base.py              #   BaseInterface
│   │   ├── cli/
│   │   │   ├── ui.py            #   Rich 流式 CLI UI
│   │   │   └── main.py          #   CLI 入口（Click）
│   │   ├── websocket/
│   │   │   └── server.py        #   WebSocket Server（纯 websockets 库）
│   │   └── im/                  #   IM 接口预留目录
│   │       └── base.py
│   │
│   ├── observability/           # ⑫ 日志与审计系统
│   │   ├── __init__.py
│   │   ├── audit_logger.py      #   AuditLogger（中心门面，异步队列写入）
│   │   ├── events.py            #   AuditEvent 数据模型族（Pydantic）
│   │   ├── level.py             #   AuditLevel 枚举（minimal/standard/verbose/debug）
│   │   ├── hook.py              #   AuditHook（挂载到 AgentHook，无侵入集成）
│   │   ├── masker.py            #   FieldMasker（PII脱敏 / 内容截断）
│   │   └── backends/
│   │       ├── base.py          #     BaseAuditBackend 抽象接口
│   │       ├── jsonl_backend.py #     JSONL 文件后端（默认，零依赖，日期轮转）
│   │       ├── sqlite_backend.py#     SQLite 后端（开发/测试，支持结构化查询）
│   │       └── postgres_backend.py # PostgreSQL 后端（生产，预留骨架）
│   │
│   ├── plugins/                 # ⑬ 插件系统
│   │   ├── base.py              #   BasePlugin
│   │   └── manager.py           #   PluginManager（动态加载）
│   │
│   └── utils/
│       ├── logging.py           #   应用运行日志（structlog，区别于审计日志）
│       ├── retry.py             #   异步重试装饰器（exponential backoff）
│       ├── timeout.py           #   超时管理器
│       └── config.py            #   配置加载（YAML + 环境变量）
│
├── skills/                      # Skill 定义目录（SKILL.md 风格）
│   └── example/
│       └── SKILL.md
├── audit_logs/                  # 默认 JSONL 审计日志目录（可配置）
│   └── .gitkeep
└── tests/
```

---

## 各层详细设计

---

### ① Provider 层 — 多路冗余 LLM 接口

**核心目标**：屏蔽 OpenAI / Anthropic 流格式差异，对上层统一输出 `StreamEvent`。

#### 统一流事件模型

```python
# providers/base.py
@dataclass
class StreamEvent:
    type: Literal[
        "text_delta",        # 文本增量片段
        "tool_call_start",   # 工具调用开始（含 tool_name, call_id）
        "tool_call_delta",   # 工具参数 JSON 增量
        "tool_call_end",     # 工具调用参数完整（tool_args 已填充）
        "message_end",       # 本轮消息结束
        "error",             # 错误事件
    ]
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args_delta: str | None = None
    tool_args: dict | None = None
    stop_reason: str | None = None
    error: Exception | None = None

class BaseProvider(ABC):
    capabilities: ProviderCapabilities   # 是否支持多模态、工具调用等

    @abstractmethod
    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        model: str,
        **kwargs,
    ) -> AsyncIterator[StreamEvent]: ...
```

#### CapabilityDetector — 多模态能力检测

```python
# providers/capability.py
@dataclass
class ProviderCapabilities:
    supports_vision: bool = False       # 支持图像输入
    supports_tool_calls: bool = True
    supports_streaming: bool = True
    max_image_size_mb: int = 20

class CapabilityDetector:
    # 内置模型能力注册表（可通过配置覆盖）
    _KNOWN_VISION_MODELS = {
        "gpt-4o", "gpt-4-turbo", "gpt-4-vision-preview",
        "claude-3-*", "claude-opus-4-5", "claude-sonnet-*",
        "gemini-*",
    }
    def detect(self, model: str, provider_type: str) -> ProviderCapabilities: ...
```

#### ProviderRouter — 多路冗余 + Failover + 熔断

```yaml
# config/config.yaml（示例）
providers:
  - name: anthropic_primary
    type: anthropic
    model: claude-opus-4-5
    priority: 1
    api_key: "${ANTHROPIC_API_KEY}"
  - name: openai_fallback
    type: openai
    model: gpt-4o
    priority: 2
    api_key: "${OPENAI_API_KEY}"
  - name: openai_mini_backup
    type: openai
    model: gpt-4o-mini
    priority: 3
    api_key: "${OPENAI_API_KEY}"

failover:
  strategy: priority          # priority | round_robin | latency
  circuit_breaker:
    failure_threshold: 3      # 连续失败 N 次触发熔断
    recovery_seconds: 60      # 熔断恢复等待时间
```

Router 执行逻辑：
1. 按优先级排序，从最高优先级开始
2. 遇到 `ProviderRateLimitError` / `ProviderTimeoutError` 自动切换
3. 熔断器：连续失败 N 次后暂停该 Provider，`recovery_seconds` 后自动恢复
4. 所有 Provider 均失败时抛出 `AllProvidersFailedError`

---

### ② Agent Core — ReAct 循环

```
用户输入
  │
  ▼
ContextManager.add_user_message()  ←── 含图像的消息由 ImageHandler 处理
  │
  ▼
ProviderRouter.stream(messages, tools)
  │   ┌──────────────────────────────┐
  │   │      StreamProcessor          │
  │   │  accumulates StreamEvents     │
  │   └──────────────────────────────┘
  │
  ├── text_delta ──► AgentHook.on_stream(delta) ──► UI 即时输出
  │
  └── tool_call_end
        │
        ▼
      ToolCallParser.parse()
        │
        ▼
      SafetyGuard.check_tool_call()     ← 安全前置检查
        │
        ▼
      asyncio.gather(*[               ← 并行执行多个工具调用
          ToolExecutor.execute(tc)
          for tc in tool_calls
      ])
        │
        ▼
      ContextManager.add_tool_results()
        │
        ▼
      AgentHook.after_iteration()     ← 结果回调（日志、UI展示）
        │
        ▼
      (循环，直到 stop_reason == "end_turn" 或达到 max_iterations)
```

**StructuredOutputParser** 支持：
- Markdown 代码块提取（` ```json ... ``` `）
- Anthropic `tool_use` 块作为结构化输出载体
- 注册自定义解析器：`parser.register("my_format", fn)`

---

### ② Agent Core — Hook 体系（生命周期钩子接口）

#### 设计定位

Hook 体系是 Agent 循环的**可观测性与扩展性骨干**，将 UI 渲染、审计记录、业务监控等关切点与核心逻辑彻底解耦。

- `AgentHook`：抽象基类，所有方法有默认空实现（no-op），子类只覆盖感兴趣的钩子点
- `CompositeHook`：组合多个 Hook 实例，是 AgentLoop 实际使用的运行时对象
- `HookContext`：携带当前执行状态快照的上下文数据类，传递给所有钩子方法

#### HookContext — 执行上下文快照

```python
# core/hook.py
@dataclass
class HookContext:
    """传递给所有 Hook 方法，携带当前 Agent 执行状态。"""
    session_id: str
    agent_id: str
    turn_id: str                          # 每次 user→response 循环的唯一 ID
    iteration: int                        # 当前 ReAct 迭代次数（从 1 开始）
    model: str
    provider: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_events: list[dict] = field(default_factory=list)
    response: LLMResponse | None = None
    usage: dict = field(default_factory=dict)  # token 用量

    def snapshot(self) -> dict:
        """生成可序列化的上下文快照（供审计/错误记录使用）。"""
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "iteration": self.iteration,
            "model": self.model,
            "provider": self.provider,
            "usage": self.usage,
        }
```

#### AgentHook — 完整生命周期钩子接口

```python
# core/hook.py
class AgentHook(ABC):
    """
    Agent 生命周期钩子基类。
    所有方法均有默认空实现（no-op），子类只需覆盖感兴趣的钩子点。
    """

    def wants_streaming(self) -> bool:
        """返回 True 表示此 Hook 需要 on_stream 回调（影响流式路由）。"""
        return False

    # ── Session 生命周期 ────────────────────────────────────────────
    async def on_session_start(self, ctx: HookContext) -> None: pass
    async def on_session_end(
        self, ctx: HookContext, *, final_content: str | None, exit_reason: str
    ) -> None: pass

    # ── Turn 生命周期（用户输入 → 最终响应为一个 Turn） ─────────────
    async def on_turn_start(self, ctx: HookContext) -> None: pass
    async def on_turn_end(self, ctx: HookContext) -> None: pass

    # ── Iteration 生命周期（单次 ReAct 循环为一个 Iteration） ────────
    async def on_iteration_start(self, ctx: HookContext) -> None: pass
    async def on_iteration_end(self, ctx: HookContext) -> None: pass

    # ── Provider / 流式调用 ─────────────────────────────────────────
    async def on_provider_call_start(self, ctx: HookContext) -> None: pass
    async def on_provider_call_end(
        self, ctx: HookContext, *, stop_reason: str, usage: dict
    ) -> None: pass
    async def on_provider_failover(
        self, ctx: HookContext, *, from_provider: str, to_provider: str, reason: str
    ) -> None: pass

    # 以下三个仅在 wants_streaming() == True 时被调用
    async def on_stream_start(self, ctx: HookContext) -> None: pass
    async def on_stream(self, ctx: HookContext, delta: str) -> None: pass
    async def on_stream_end(self, ctx: HookContext, *, resuming: bool) -> None: pass

    # ── 工具调用 ────────────────────────────────────────────────────
    async def before_execute_tools(self, ctx: HookContext) -> None: pass
    async def on_tool_start(
        self, ctx: HookContext, *, tool_name: str, args: dict, call_id: str
    ) -> None: pass
    async def on_tool_end(
        self, ctx: HookContext,
        *, tool_name: str, result: ToolResult, call_id: str, latency_ms: int
    ) -> None: pass
    async def on_tool_error(
        self, ctx: HookContext, *, tool_name: str, error: Exception, call_id: str
    ) -> None: pass
    async def after_execute_tools(self, ctx: HookContext) -> None: pass
    async def after_iteration(self, ctx: HookContext) -> None: pass   # 兼容旧接口

    # ── 安全系统 ────────────────────────────────────────────────────
    async def on_safety_blocked(
        self, ctx: HookContext, *, rule: str, reason: str, action: str
    ) -> None: pass

    # ── SubAgent ────────────────────────────────────────────────────
    async def on_subagent_start(
        self, ctx: HookContext, *, spec: "SubAgentSpec", depth: int
    ) -> None: pass
    async def on_subagent_end(
        self, ctx: HookContext,
        *, spec: "SubAgentSpec", result: "SubAgentResult", depth: int
    ) -> None: pass

    # ── 错误与内容后处理 ─────────────────────────────────────────────
    async def on_error(self, ctx: HookContext, *, error: Exception) -> None: pass

    def finalize_content(
        self, ctx: HookContext, content: str | None
    ) -> str | None:
        """
        对最终输出内容进行后处理（同步）。
        例如：去除 <think>...</think> 标签、格式化签名等。
        """
        return content
```

#### CompositeHook — 组合多个 Hook

```python
# core/hook.py
class CompositeHook(AgentHook):
    """
    将多个 Hook 组合为一个，使它们接收相同的生命周期事件。
    AgentLoop 只持有一个 CompositeHook 实例，无需关心内部 Hook 数量。

    典型组合（由 Agent 门面类在初始化时装配）：
        hook = CompositeHook([
            CLIProgressHook(on_stream=..., on_progress=...),  # UI 层
            AuditHook(audit_logger=..., session_id=...),      # 审计层
            CustomBusinessHook(...),                           # 自定义业务钩子
        ])
    """
    def __init__(self, hooks: list[AgentHook]):
        self._hooks = hooks

    def wants_streaming(self) -> bool:
        return any(h.wants_streaming() for h in self._hooks)

    # 通用委托模式（所有方法结构相同）
    async def on_session_start(self, ctx) -> None:
        for h in self._hooks:
            await h.on_session_start(ctx)

    async def on_stream(self, ctx, delta) -> None:
        for h in self._hooks:
            if h.wants_streaming():
                await h.on_stream(ctx, delta)

    async def on_tool_end(self, ctx, *, tool_name, result, call_id, latency_ms) -> None:
        for h in self._hooks:
            await h.on_tool_end(
                ctx, tool_name=tool_name, result=result,
                call_id=call_id, latency_ms=latency_ms
            )

    def finalize_content(self, ctx, content) -> str | None:
        for h in self._hooks:
            content = h.finalize_content(ctx, content)
        return content

    # ... 其余方法均以相同方式委托
```

#### Hook 调用点在 AgentLoop 中的完整映射

```
AgentLoop._process_message()
  │
  ├── hook.on_session_start()          ← [会话级] 首次建立 Session 时
  ├── hook.on_turn_start()             ← [轮次级] 每次接收用户消息
  │
  └── _run_agent_loop():              ← ReAct 主循环
        │
        ├─ [每次 Iteration]
        │   ├── hook.on_iteration_start()
        │   │
        │   ├── hook.on_provider_call_start()
        │   │     ├── hook.on_stream_start()          ← 首个 token 到达前
        │   │     ├── hook.on_stream(delta)            ← 每个文本片段
        │   │     └── hook.on_stream_end(resuming)     ← 本段流结束
        │   ├── hook.on_provider_call_end(stop_reason, usage)
        │   │
        │   ├── hook.before_execute_tools()            ← 所有工具开始前
        │   │     ├── hook.on_tool_start(name, args, id)
        │   │     ├── [asyncio.gather 并行执行]
        │   │     ├── hook.on_tool_end(name, result, id, ms)  ← 每个工具完成
        │   │     └── hook.on_tool_error(name, err, id)       ← 工具出错
        │   ├── hook.after_execute_tools()
        │   │
        │   └── hook.on_iteration_end()
        │
        └── hook.finalize_content(content)   ← 输出内容后处理

  ├── hook.on_turn_end()               ← [轮次级] 最终响应已生成
  └── hook.on_session_end(final, reason) ← [会话级] Agent 关闭时

跨层触发点（非 AgentLoop 内部）：
  ProviderRouter   → hook.on_provider_failover(from, to, reason)
  SafetyGuard      → hook.on_safety_blocked(rule, reason, action)
  SubAgentManager  → hook.on_subagent_start(spec, depth)
  SubAgentRunner   → hook.on_subagent_end(spec, result, depth)
  任意位置          → hook.on_error(error)
```

#### 内置 Hook 实现对照

| Hook 实现 | 位置 | 职责 |
|-----------|------|------|
| `CLIProgressHook` | `interfaces/cli/ui.py` | Rich 流式渲染、Spinner、工具提示 |
| `AuditHook` | `observability/hook.py` | 异步写入所有 AuditEvent |
| `WebSocketHook` | `interfaces/websocket/server.py` | 将事件序列化推送给 WS 客户端 |
| `CustomHook` | 用户自定义 | 继承 `AgentHook`，实现任意钩子点 |

---

### ③ SubAgent 子智能体系统（重点设计）

#### 设计理念

SubAgent 是**拥有独立上下文、独立工具集、受控生命周期**的嵌套 Agent 实例。
主 Agent 通过调用 `spawn` 工具触发 SubAgent，收集其输出作为工具结果继续推理。

```
主 Agent Loop
  │
  ├── 正常推理 ...
  │
  └── tool_call: spawn(task="分析这份报告", tools=["read_file", "web_search"])
        │
        ▼
      SpawnTool.execute()
        │
        ▼
      SubAgentManager.create(spec)       ← 创建 SubAgent 实例
        │
        ▼
      SubAgentRunner.run(spec)           ← 复用 AgentLoop 的核心循环
        │  (独立 ContextManager, 独立 ToolRegistry)
        │  (可使用不同 model / 不同 system_prompt)
        │  (输出通过 on_progress 回调实时传给主 Agent UI)
        │
        ▼
      SubAgentResult(content, tools_used, iterations)
        │
        ▼
      作为 tool_result 返回给主 Agent
        │
        ▼
  主 Agent 继续推理（拥有子智能体的完整输出）
```

#### 数据模型

```python
# subagent/base.py
@dataclass
class SubAgentSpec:
    task: str                          # 子任务描述（系统提示）
    tools: list[str] | None = None     # 允许的工具名列表，None = 继承父 Agent 工具集
    model: str | None = None           # 可使用不同模型，None = 继承父 Agent 模型
    max_iterations: int = 20           # 子 Agent 最大迭代次数
    timeout_seconds: int = 300         # 子 Agent 超时（秒）
    initial_message: str | None = None # 子 Agent 第一条用户消息
    allow_spawn: bool = False          # 子 Agent 是否可以再次 spawn（防止无限递归）
    metadata: dict = field(default_factory=dict)

@dataclass
class SubAgentResult:
    content: str                       # 最终输出
    tools_used: list[str]             # 使用过的工具列表
    iterations: int                   # 实际迭代次数
    success: bool
    error: str | None = None
    metadata: dict = field(default_factory=dict)
```

#### SubAgentManager

```python
# subagent/manager.py
class SubAgentManager:
    _MAX_CONCURRENT = 3      # 最大并发 SubAgent 数量
    _MAX_DEPTH = 3           # 最大嵌套深度（防止递归爆炸）

    def __init__(self, provider_router, tool_registry, config): ...

    async def spawn(
        self,
        spec: SubAgentSpec,
        parent_depth: int = 0,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> SubAgentResult:
        """创建并运行 SubAgent，返回结果。"""
        if parent_depth >= self._MAX_DEPTH:
            raise SubAgentDepthError(f"Max subagent depth ({self._MAX_DEPTH}) exceeded")

        async with self._semaphore:   # asyncio.Semaphore(_MAX_CONCURRENT)
            runner = SubAgentRunner(
                spec=spec,
                provider_router=self.provider_router,
                tool_registry=self._build_tool_registry(spec),
                on_progress=on_progress,
                depth=parent_depth + 1,
            )
            return await asyncio.wait_for(
                runner.run(),
                timeout=spec.timeout_seconds,
            )

    def _build_tool_registry(self, spec: SubAgentSpec) -> ToolRegistry:
        """根据 spec.tools 白名单过滤工具集。"""
        if spec.tools is None:
            return self.tool_registry   # 继承全集
        registry = ToolRegistry()
        for name in spec.tools:
            if tool := self.tool_registry.get(name):
                registry.register(tool)
        return registry
```

#### SpawnTool — LLM 可调用的接口

```python
# subagent/spawn_tool.py
class SpawnTool(BaseTool):
    name = "spawn"
    description = """
    派生一个子智能体来完成特定子任务。
    适合用于：并行处理、隔离执行、专项分析等场景。
    子智能体拥有独立上下文，完成任务后返回最终输出。
    """
    parameters_schema = {
        "type": "object",
        "properties": {
            "task":        {"type": "string", "description": "子智能体的任务描述（系统提示）"},
            "message":     {"type": "string", "description": "发送给子智能体的第一条用户消息"},
            "tools":       {"type": "array",  "items": {"type": "string"},
                           "description": "子智能体可使用的工具名列表，不填则继承父智能体工具集"},
            "model":       {"type": "string", "description": "指定子智能体使用的模型，不填则继承"},
            "max_iterations": {"type": "integer", "default": 20},
        },
        "required": ["task", "message"],
    }

    async def execute(self, task, message, tools=None, model=None, max_iterations=20) -> ToolResult:
        spec = SubAgentSpec(
            task=task,
            initial_message=message,
            tools=tools,
            model=model,
            max_iterations=max_iterations,
        )
        result = await self.manager.spawn(spec, parent_depth=self._current_depth)
        if result.success:
            return ToolResult(content=result.content, metadata={"iterations": result.iterations})
        return ToolResult(content=f"SubAgent failed: {result.error}", is_error=True)
```

#### SubAgentRunner — 轻量执行器

```python
# subagent/runner.py
class SubAgentRunner:
    """复用核心 AgentLoop 逻辑，但拥有独立上下文。"""

    def __init__(self, spec, provider_router, tool_registry, on_progress, depth): ...

    async def run(self) -> SubAgentResult:
        ctx = ContextManager()
        ctx.set_system(self.spec.task)
        ctx.add_user_message(self.spec.initial_message)

        iterations = 0
        tools_used = []

        async for event in self._react_loop(ctx):
            if event.type == "tool_used":
                tools_used.append(event.tool_name)
            elif event.type == "progress" and self.on_progress:
                await self.on_progress(f"[SubAgent] {event.text}")
            elif event.type == "done":
                return SubAgentResult(
                    content=event.text,
                    tools_used=tools_used,
                    iterations=iterations,
                    success=True,
                )
            iterations += 1

    async def _react_loop(self, ctx):
        """与 AgentLoop._run_agent_loop 相同逻辑，提取为独立可复用方法。"""
        ...
```

#### SubAgent 并行模式（高级用法）

主 Agent 可以在一次 ReAct 轮次中同时 spawn 多个 SubAgent：

```
主 Agent 决策：需要同时分析 3 份报告

tool_calls: [
  spawn(task="分析报告A", message="请分析..."),
  spawn(task="分析报告B", message="请分析..."),
  spawn(task="分析报告C", message="请分析..."),
]

asyncio.gather(*[executor.execute(tc) for tc in tool_calls])
  │── SubAgent A（并行运行）
  │── SubAgent B（并行运行）
  └── SubAgent C（并行运行）

全部完成后，结果统一返回主 Agent 继续推理。
```

---

### ④ 上下文管理

```python
# context/message.py（Pydantic 模型）
class ContentBlock(BaseModel):
    type: Literal["text", "image_url", "image_base64"]
    text: str | None = None
    url: str | None = None              # 图像 URL
    base64_data: str | None = None      # base64 图像数据
    media_type: str | None = None       # image/jpeg, image/png 等

class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentBlock]
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    token_estimate: int | None = None

class ContextManager:
    def add_user_message(self, content: str | list[ContentBlock]) -> None
    def add_assistant_message(self, content, tool_calls=None) -> None
    def add_tool_result(self, tool_call_id, result: ToolResult) -> None
    def get_messages(self) -> list[Message]
    def estimate_tokens(self) -> int
    async def maybe_compress(self) -> None   # 预留：触发压缩
```

**ContextCompressor 预留接口**（未来实现）：
- 策略1：滑动窗口（保留最近 N 轮）
- 策略2：LLM 摘要压缩（对早期消息生成摘要）
- 策略3：重要性评分（保留关键工具调用结果）

---

### ⑤ 工具系统

#### BaseTool 接口

```python
class BaseTool(ABC):
    name: str
    description: str
    parameters_schema: dict    # JSON Schema

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult: ...

    def to_openai_schema(self) -> dict:
        """生成 OpenAI function calling 格式"""

    def to_anthropic_schema(self) -> dict:
        """生成 Anthropic tool_use 格式"""
```

#### CLI 沙盒（subprocess + ulimit）

```python
# tools/sandbox/subprocess_sandbox.py
class SubprocessSandbox(BaseSandbox):
    async def run(self, command: str, **kwargs) -> SandboxResult:
        # 1. CLIFence 安全检查（白黑名单、路径限制）
        # 2. 构建受限 subprocess
        # 3. ulimit 资源限制（CPU时间、内存、文件大小）
        # 4. asyncio.wait_for 超时控制
        # 5. kill 子进程
        ...

# tools/sandbox/docker_sandbox.py（预留骨架）
class DockerSandbox(BaseSandbox):
    """Docker 容器沙盒（通过 --sandbox-backend=docker 启用）"""
    async def run(self, command: str, **kwargs) -> SandboxResult:
        raise NotImplementedError("Docker sandbox coming soon")
```

启动参数控制：
```bash
myagent --sandbox-backend subprocess   # 默认
myagent --sandbox-backend docker       # 未来支持
```

CLI 安全围栏配置：
```yaml
# config/safety_rules.yaml
cli_fence:
  allowed_commands:
    - ls, cat, grep, find, echo, pwd, env
    - python3, pip, git, curl, wget
    - head, tail, wc, sort, uniq, diff
  denied_patterns:
    - "rm -rf /"
    - "sudo"
    - "mkfs"
  denied_paths:
    - /etc/shadow
    - /root
    - /sys
    - /proc/sys
  resource_limits:
    max_cpu_seconds: 30
    max_memory_mb: 512
    max_output_bytes: 102400
    timeout_seconds: 60     # asyncio.wait_for 超时
```

#### Python 代码沙盒

```python
# tools/sandbox/subprocess_sandbox.py（Python 代码执行）
class PythonSubprocessSandbox:
    """
    在独立子进程中执行 Python 代码：
    - 获取受限 builtins（可配置禁用 open/exec/eval 等）
    - subprocess 配合 ulimit 资源限制
    - 捕获 stdout/stderr，返回结构化结果
    """
    DENIED_BUILTINS = {"open", "exec", "eval", "__import__", "compile"}
```

---

### ⑥ Skill 系统

Skill vs Tool 对比：

| | Tool | Skill |
|--|--|--|
| 粒度 | 原子操作（单次函数调用） | 组合工作流（可含多步推理） |
| 定义方式 | Python 类 | YAML/Markdown 描述 + 可选 Python |
| 可组合 | 否 | 是（Skill 可调用 Tool 和 SubAgent） |
| 上下文 | 无（无状态） | 有专属系统提示和工具集 |

**SKILL.md 格式**：
```markdown
---
name: deep_analysis
description: 对复杂问题进行深度分析，自动拆解子任务并用子智能体并行处理
version: "1.0"
parameters:
  - name: topic
    type: string
    required: true
  - name: depth
    type: integer
    default: 2
tools_allowed: [read_file, web_search, spawn]
model: claude-opus-4-5       # 可为 skill 指定专属模型
---

## 系统指令
你是一个专业的深度分析专家...
```

Skill 加载逻辑：
1. `SkillRegistry` 扫描 `skills/` 目录，加载所有 `SKILL.md`
2. 每个 Skill 注册为一个特殊 Tool（`use_skill` 或直接以 skill 名注册）
3. 调用时由 `SkillEngine` 实例化独立 AgentLoop（等价于 SubAgent）

---

### ⑦ 安全系统 — 责任链模式

```
InputGuard → CLIFence → ContentFilter → OutputGuard
    │               │            │             │
  检查输入       CLI命令/路径    敏感词/        防止
  注入/越权      白黑名单限制    内容合规       信息泄露
```

```python
class SafetyGuard:
    def __init__(self, rules: list[BaseRule]): ...
    async def check_input(self, msg: str) -> GuardResult: ...
    async def check_tool_call(self, tool: str, args: dict) -> GuardResult: ...
    async def check_output(self, content: str) -> GuardResult: ...

# 自定义规则：继承 BaseRule，放入 safety/rules/ 目录，在 config 中引用即可
class BaseRule(ABC):
    priority: int = 100    # 数字越小越先执行
    @abstractmethod
    async def check(self, context: SafetyContext) -> GuardResult: ...
```

---

### ⑧ 文档处理

```python
class BaseDocumentProcessor(ABC):
    supported_extensions: list[str]
    @abstractmethod
    async def extract_text(self, path: Path) -> str: ...
    @abstractmethod
    async def extract_structured(self, path: Path) -> DocumentContent: ...
```

| 格式 | 库 | 备注 |
|------|-----|------|
| DOCX | `python-docx` | 含表格提取 |
| XLSX | `openpyxl` | 支持多 Sheet |
| PDF（文字版） | `pdfplumber` | 高精度文本提取 |
| PDF（扫描版） | `pymupdf`（图像提取）→ 多模态 LLM | 扫描版页面转图像，送入支持 vision 的 LLM |

> **扫描版 PDF 策略**：不使用传统 OCR 引擎，而是将每页渲染为图像，通过多模态 LLM（如 `claude-opus-4-5` / `gpt-4o`）理解内容，精度更高。

---

### ⑨ 图像输入（多模态）

```python
# vision/image_handler.py
class ImageHandler:
    def __init__(self, provider_capabilities: ProviderCapabilities): ...

    async def prepare(
        self,
        source: str | Path | bytes,   # URL / 本地路径 / 字节流
        provider_type: str,            # "openai" | "anthropic"
    ) -> ContentBlock:
        """
        返回适配对应 Provider 的多模态 content block：
        - OpenAI:    {"type": "image_url", "image_url": {"url": "data:image/..."}}
        - Anthropic: {"type": "image", "source": {"type": "base64", ...}}
        """
        if not self.provider_capabilities.supports_vision:
            return ContentBlock(type="text", text="[图像输入：当前模型不支持多模态，已忽略]")
        ...

    def _check_size(self, data: bytes) -> None:
        """检查图像大小是否超过 Provider 限制"""
```

---

### ⑩ 接口层

#### CLI（Rich 流式）

```
功能：
  - 流式文本用 rich.Live + Markdown 实时渲染
  - 工具调用用进度面板显示（Panel + Spinner）
  - Provider failover 时显示切换提示
  - --show-tools 参数展示工具调用详情
  - --sandbox-backend 参数选择沙盒模式
  - 内建命令：/clear /exit /history /model
  - 多轮对话、session 持久化
```

#### WebSocket Server（纯 websockets 库）

```python
# interfaces/websocket/server.py
import websockets

# 消息协议（JSON）
# Client → Server:
{
  "type": "message",
  "content": "帮我分析这份文档",
  "images": ["data:image/png;base64,..."],  # 可选
  "session_id": "abc123"                    # 可选，用于会话延续
}

# Server → Client（流式）:
{"type": "text_delta",   "text": "好的，我来..."}
{"type": "tool_start",   "name": "read_file", "args": {"path": "..."}}
{"type": "tool_result",  "name": "read_file", "content": "..."}
{"type": "subagent",     "status": "started", "task": "..."}
{"type": "done",         "session_id": "abc123"}
{"type": "error",        "message": "..."}

async def handler(websocket):
    async for raw in websocket:
        msg = parse_message(raw)
        async for event in agent.stream_process(msg):
            await websocket.send(json.dumps(event))

async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        await asyncio.Future()   # 永远运行
```

#### IM 接口预留

`BaseIMInterface` 定义统一抽象，未来适配：企业微信 / 钉钉 / 飞书 / Telegram / Slack

---

### ⑪ 异步、重试、超时机制

```python
# utils/retry.py
@async_retry(
    max_attempts=3,
    backoff=ExponentialBackoff(base=1.0, max=30.0, jitter=True),
    retry_on=(ProviderRateLimitError, ProviderTimeoutError, httpx.ConnectError),
)
async def _call_provider_with_retry(...): ...
```

| 场景 | 超时 | 行为 |
|------|------|------|
| Provider 首个 token | 15s | 切换 Failover Provider |
| 单次工具执行 | 30s（可配置） | 抛出 ToolTimeoutError |
| CLI 命令（subprocess） | 60s（可配置） | kill 子进程 |
| Python 沙盒执行 | 30s（可配置） | kill 子进程 |
| SubAgent 整体 | 300s（可配置） | 取消 Task，返回部分结果 |
| 完整 Agent 轮次 | 600s（可配置） | 终止并返回部分结果 |

---

### ⑫ 观测性 — 日志与审计系统

#### 设计定位

日志与审计系统是**横切所有层的基础设施**，不同于 `utils/logging.py`（应用运行日志），它专注于：
- **完整记录**用户输入、模型输出、工具调用、SubAgent 执行的全链路轨迹
- **全生命周期覆盖**：开发调试 → 测试验证 → 生产审计，通过粒度配置无缝切换
- **不阻塞主链路**：写入操作通过异步队列在后台完成
- **合规友好**：支持 PII 脱敏、字段截断、保留策略，适配隐私保护要求

#### AuditLevel — 四级粒度

| 级别 | 适用场景 | 记录内容 |
|------|---------|----------|
| `minimal` | 生产（成本敏感） | 时间戳、session_id、模型名、输入哈希、响应摘要（前100字）、token用量 |
| `standard` | 生产（常规） | + 完整用户输入、完整模型响应、工具名列表（不含参数）、Failover事件 |
| `verbose` | 测试 / 预发 | + 工具完整参数与结果、安全拦截事件、SubAgent生命周期、Provider切换详情 |
| `debug` | 开发 / 排错 | + 完整消息上下文列表、流式delta序列、Provider请求头、token级别计时 |

#### AuditEvent 数据模型族

```python
# observability/events.py
class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str                  # 见下方事件类型
    session_id: str
    agent_id: str                    # 支持多 Agent 实例区分
    level: AuditLevel
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    environment: str                 # dev | test | prod（从配置读取）
    metadata: dict = Field(default_factory=dict)

# 具体事件子类
class ConversationEvent(AuditEvent):
    event_type: str = "conversation"
    turn_id: str
    user_input: str | None           # minimal 下为 None（只存 hash）
    user_input_hash: str             # SHA256，始终记录
    user_input_has_image: bool
    assistant_response: str | None   # minimal 下为摘要
    stop_reason: str
    model: str
    provider: str
    tokens_input: int
    tokens_output: int
    latency_ms: int

class ToolCallEvent(AuditEvent):
    event_type: str = "tool_call"
    turn_id: str
    tool_name: str
    tool_call_id: str
    args: dict | None                # verbose+ 才记录
    result_summary: str              # 结果前 200 字
    result_full: str | None          # verbose+ 才记录完整结果
    is_error: bool
    latency_ms: int

class ProviderEvent(AuditEvent):
    event_type: str = "provider"     # failover, rate_limit, circuit_break
    sub_type: str
    from_provider: str | None
    to_provider: str | None
    error_message: str | None

class SubAgentEvent(AuditEvent):
    event_type: str = "subagent"
    sub_type: str                    # spawned | completed | failed | timeout
    parent_session_id: str
    task_summary: str                # 任务描述前 100 字
    depth: int
    iterations: int | None
    tools_used: list[str] | None

class SafetyEvent(AuditEvent):
    event_type: str = "safety"
    rule_name: str
    action: str                      # blocked | warned | sanitized
    input_hash: str
    reason: str

class ErrorEvent(AuditEvent):
    event_type: str = "error"
    error_type: str
    error_message: str
    traceback: str | None            # verbose+ 才记录
    context: dict                    # 错误发生时的上下文快照
```

#### AuditLogger — 中心门面

```python
# observability/audit_logger.py
class AuditLogger:
    """
    中心审计日志门面，所有组件通过它写入审计事件。
    写入操作通过 asyncio.Queue 异步化，不阻塞 Agent 主链路。
    """
    def __init__(
        self,
        level: AuditLevel,
        backends: list[BaseAuditBackend],
        masker: FieldMasker,
        queue_size: int = 1000,
    ):
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=queue_size)
        self._writer_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动后台写入 Task。Agent 启动时调用。"""
        self._writer_task = asyncio.create_task(self._write_loop())

    async def stop(self) -> None:
        """等待队列清空后停止。Agent 关闭时调用。"""
        await self._queue.join()
        if self._writer_task:
            self._writer_task.cancel()

    async def log(self, event: AuditEvent) -> None:
        """非阻塞写入（队列满时丢弃并计数，不阻塞主链路）。"""
        event = self.masker.apply(event, self.level)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_count += 1    # 监控指标

    async def _write_loop(self) -> None:
        while True:
            event = await self._queue.get()
            for backend in self._backends:
                await backend.write(event)
            self._queue.task_done()

    # 便捷方法（供各层调用）
    async def log_conversation(self, ...) -> None: ...
    async def log_tool_call(self, ...) -> None: ...
    async def log_provider_event(self, ...) -> None: ...
    async def log_subagent(self, ...) -> None: ...
    async def log_safety(self, ...) -> None: ...
    async def log_error(self, ...) -> None: ...
```

#### AuditHook — 无侵入集成

```python
# observability/hook.py
class AuditHook(AgentHook):
    """
    通过 AgentHook 体系挂载审计能力，对 AgentLoop 零侵入。
    业务逻辑与审计逻辑完全解耦。
    """
    def __init__(self, audit_logger: AuditLogger, session_id: str): ...

    async def on_user_message(self, ctx, content) -> None:
        await self.audit.log_conversation(
            sub_type="user_input", content=content, session_id=self.session_id
        )

    async def before_execute_tools(self, ctx) -> None:
        for tc in ctx.tool_calls:
            await self.audit.log_tool_call(
                tool_name=tc.name, args=tc.arguments, phase="before"
            )

    async def after_iteration(self, ctx) -> None:
        for event in ctx.tool_events:
            await self.audit.log_tool_call(
                tool_name=event["name"], result=event["result"], phase="after"
            )

    async def on_message_end(self, ctx, content, stop_reason) -> None:
        await self.audit.log_conversation(
            sub_type="assistant_response",
            content=content,
            stop_reason=stop_reason,
            usage=ctx.usage,
        )

    async def on_error(self, ctx, error) -> None:
        await self.audit.log_error(error=error, context=ctx.snapshot())
```

#### FieldMasker — PII 脱敏与内容缩放

```python
# observability/masker.py
class FieldMasker:
    """
    根据 AuditLevel 控制每个字段的记录深度：
    - hash_only:   只记录哈希（隐私输入保护）
    - truncate(n): 保留前 n 个字符
    - redact:      替换为 "[REDACTED]"
    - full:        完整记录
    """
    def __init__(self, rules: dict[str, FieldRule]): ...

    def apply(self, event: AuditEvent, level: AuditLevel) -> AuditEvent:
        """根据当前级别对事件字段应用脱敏规则。"""
        ...

# 配置示例（config/config.yaml）
# audit:
#   level: standard            # minimal | standard | verbose | debug
#   masking:
#     user_input: truncate_500 # minimal 下 hash_only，standard+ 下 truncate_500
#     tool_args: redact        # 特定工具参数永远脱敏（如包含密码的命令）
#     assistant_response: full
```

#### 存储后端

```python
# observability/backends/base.py
class BaseAuditBackend(ABC):
    @abstractmethod
    async def write(self, event: AuditEvent) -> None: ...
    async def query(self, filters: AuditQuery) -> list[AuditEvent]: ...
    async def close(self) -> None: ...
```

```python
# observability/backends/jsonl_backend.py
class JSONLBackend(BaseAuditBackend):
    """
    默认后端，零额外依赖。
    - 每行一个 JSON 对象（JSON Lines 格式）
    - 按日期自动轮转：audit_logs/2026-04-10.jsonl
    - 支持配置保留天数（超期文件自动清理）
    - 适合开发、中小规模生产
    """
    def __init__(self, log_dir: Path, retention_days: int = 90): ...

    async def write(self, event: AuditEvent) -> None:
        line = event.model_dump_json() + "\n"
        async with aiofiles.open(self._today_path(), "a") as f:
            await f.write(line)
```

```python
# observability/backends/sqlite_backend.py
class SQLiteBackend(BaseAuditBackend):
    """
    SQLite 后端，适合开发测试阶段的结构化查询分析。
    - 按 event_type 建索引
    - 支持按 session_id / 时间范围 / 事件类型过滤查询
    - 推荐配合 JSONL 后端同时使用（JSONL 作主、SQLite 作查询）
    """
```

```python
# observability/backends/postgres_backend.py  （预留骨架）
class PostgresBackend(BaseAuditBackend):
    """
    PostgreSQL 后端（生产级，预留）：
    - asyncpg 连接池
    - 分表或 TimescaleDB 时序分区
    - 支持合规导出（CSV / GDPR 清除请求）
    """
    async def write(self, event: AuditEvent) -> None:
        raise NotImplementedError("PostgreSQL backend coming soon")
```

#### 配置示例

```yaml
# config/config.yaml（审计部分）
audit:
  enabled: true
  level: standard              # minimal | standard | verbose | debug
  environment: prod            # 注入到所有事件的 environment 字段

  backends:
    - type: jsonl
      log_dir: ./audit_logs
      retention_days: 90
    # - type: sqlite             # 可同时启用多个后端
    #   db_path: ./audit.db
    # - type: postgres
    #   dsn: "postgresql://..."

  masking:
    user_input:
      minimal: hash_only       # minimal 级：只记录哈希
      standard: truncate_2000  # standard+：保留前 2000 字
      verbose: full
      debug: full
    tool_args:
      minimal: redact          # 工具参数 minimal 下不记录
      standard: truncate_500
      verbose: full
      debug: full
    assistant_response:
      minimal: truncate_200
      standard: full
      verbose: full
      debug: full

  queue:
    size: 2000                 # 事件队列大小（满时丢弃并计数）
    drop_alert_threshold: 10   # 丢弃超过 N 条时打印警告
```

#### 与各层的集成点

```
用户输入          ──► AuditHook.on_user_message()          [ConversationEvent]
 Provider 调用    ──► AuditHook.on_stream_start/end()       [ProviderEvent]
 Failover 切换   ──► ProviderRouter → AuditLogger.log()   [ProviderEvent]
 工具调用前      ──► AuditHook.before_execute_tools()      [ToolCallEvent]
 工具调用后      ──► AuditHook.after_iteration()           [ToolCallEvent]
 安全拦截        ──► SafetyGuard → AuditLogger.log()       [SafetyEvent]
 SubAgent 启动   ──► SubAgentManager → AuditLogger.log()  [SubAgentEvent]
 SubAgent 完成   ──► SubAgentRunner → AuditLogger.log()   [SubAgentEvent]
 模型最终响应   ──► AuditHook.on_message_end()             [ConversationEvent]
 任何异常        ──► AuditHook.on_error()                  [ErrorEvent]
```

---

## 依赖库清单

```toml
[tool.poetry.dependencies]
python = "^3.11"

# LLM Provider
openai = "^1.0"
anthropic = "^0.40"
httpx = "^0.27"

# 数据模型
pydantic = "^2.0"
pydantic-settings = "^2.0"

# CLI 界面
rich = "^13.0"
click = "^8.0"

# WebSocket
websockets = "^12.0"

# 文档处理
python-docx = "^1.0"
openpyxl = "^3.0"
pdfplumber = "^0.11"
pymupdf = "^1.24"        # PDF 渲染（扫描版图像化）
Pillow = "^10.0"         # 图像处理

# 配置 & 应用日志
pyyaml = "^6.0"
python-dotenv = "^1.0"
picologging = "^0.9"    # 高性能应用日志（兼容 stdlib logging API，速度快 4-10x）

# 审计日志
aiofiles = "^23.0"      # 异步文件写入（JSONL 后端）
aiosqlite = "^0.20"     # 异步 SQLite（SQLite 后端，可选）
asyncpg = {optional = true, version = "^0.29"}  # PostgreSQL（预留）
```

---

## 分阶段实施计划

### Phase 1 — 核心骨架（1-2 周）
- [ ] Provider 层：OpenAI + Anthropic 流式，统一 StreamEvent
- [ ] CapabilityDetector：模型多模态能力检测
- [ ] ProviderRouter：多路冗余 + Failover + 熔断器
- [ ] AgentLoop：基础 ReAct 循环（含工具并行执行）
- [ ] ContextManager + Message 数据模型（含多模态 ContentBlock）
- [ ] BaseTool + ToolRegistry + ToolExecutor
- [ ] 重试 / 超时工具类
- [ ] **HookContext + AgentHook + CompositeHook**：完整生命周期钩子体系
- [ ] **CLIProgressHook**：Rich 流式渲染（实现 `wants_streaming=True`）
- [ ] picologging 初始化（`utils/logging.py` 封装，兼容 stdlib logging API）
- [ ] CLI 基础 UI（Rich 流式输出）
- [ ] **审计系统基础**：AuditLevel + AuditEvent 模型 + AuditLogger（异步队列）
- [ ] **JSONLBackend**：日志文件写入 + 日期轮转
- [ ] **AuditHook**（继承 AgentHook）：挂载到 CompositeHook，覆盖 ConversationEvent + ErrorEvent
- [ ] **FieldMasker**：按 AuditLevel 控制字段粒度

### Phase 2 — 工具、沙盒、安全（1 周）
- [ ] SubprocessSandbox（ulimit）+ DockerSandbox（预留骨架）
- [ ] CLITool（集成 CLIFence）
- [ ] PythonSandboxTool
- [ ] SafetyGuard 责任链 + CLIFence + ContentFilter
- [ ] FileReadTool / FileWriteTool
- [ ] StructuredOutputParser + ToolCallParser
- [ ] ImageHandler（多模态图像处理，适配 OpenAI/Anthropic 格式）
- [ ] **审计扩展**：ToolCallEvent + SafetyEvent 覆盖；ProviderEvent（Failover）覆盖

### Phase 3 — SubAgent 系统（1 周）
- [ ] SubAgentSpec + SubAgentResult 数据模型
- [ ] SubAgentRunner（复用 AgentLoop 核心逻辑）
- [ ] SubAgentManager（并发控制 + 深度限制）
- [ ] SpawnTool（LLM 可调用接口）
- [ ] 并行 SubAgent 模式验证
- [ ] 防死循环保护（深度/并发上限）
- [ ] **审计扩展**：SubAgentEvent（spawned/completed/failed/timeout）覆盖

### Phase 4 — Skill、文档、接口（1 周）
- [ ] Skill 系统（YAML/MD 加载 + SkillRegistry + SkillEngine）
- [ ] DOCX / XLSX / PDF 文档处理器
- [ ] 扫描版 PDF（pymupdf 图像化 → 多模态 LLM）
- [ ] WebSocket Server（纯 websockets）
- [ ] PluginManager（动态模块加载）
- [ ] IM BaseInterface（预留骨架）
- [ ] **审计扩展**：SQLiteBackend（开发/测试结构化查询）；WebSocket 接口级事件覆盖

### Phase 5 — 完善与测试
- [ ] 向量检索接口（BaseVectorStore + RAGSkill 预留实现）
- [ ] ContextCompressor 预留接口（滑动窗口策略）
- [ ] 单元测试（Provider Mock、Tool 测试、Safety 测试）
- [ ] 集成测试（End-to-End Agent 对话）
- [ ] README + 配置文档
- [ ] **审计完善**：PostgresBackend 骨架；保留策略（自动清理过期 JSONL）；审计事件查询 CLI 工具（`myagent audit query --session-id xxx --level verbose`）

---

## 关键风险与缓解

| 风险 | 缓解策略 |
|------|---------|
| SubAgent 无限递归 | 深度限制（默认 3 层）+ 并发限制（默认 3 个） |
| Provider 全部失败 | AllProvidersFailedError 明确上报，支持优雅降级提示 |
| CLI 沙盒逃逸 | 多层防御：白名单 → 路径限制 → ulimit → 超时 kill |
| 上下文超出 token 限制 | ContextManager 估算 token，超阈值自动触发压缩（Phase 5） |
| 流式 WebSocket 断连 | 心跳检测 + session_id 支持断线重连续传 |
| 审计队列积压/丢失 | 队列满时计数丢弃（不阻塞主链路），监控 `dropped_count` 指标并告警 |
| 审计日志含敏感信息 | FieldMasker + 默认 `standard` 级别，敏感工具参数配置为 `redact` |
| 审计存储空间膨胀 | JSONL 按天轮转 + 可配置保留天数（默认 90 天）自动清理 |
