# Phase 3 — SubAgent 子智能体系统实施方案

## 目标

实现 V3 设计中的 **SubAgent 子智能体系统**，使主 Agent 能够通过 `spawn` 工具派生拥有**独立上下文、独立工具集、受控生命周期**的嵌套 Agent 实例，并支持并行执行、深度/并发限制、Budget 预算控制和审计追踪。

## 当前代码库现状（Phase 1+2 已完成）

### ✅ 已就绪的基础设施

| 模块 | 文件 | 状态 |
|------|------|------|
| AgentLoop（Turn dispatcher） | `core/loop.py` + `core/turns.py` | ✅ ModelTurn + ToolTurn 完整 |
| HookManager（函数式事件分发） | `core/hook.py` | ✅ emit/on/hook 装饰器 |
| StreamProcessor（流式聚合） | `core/stream.py` | ✅ 含 CancellationToken |
| ToolRegistry + ToolExecutor | `tools/registry.py` + `tools/executor.py` | ✅ 含 Safety/Idempotency/HITL |
| ContextManager | `context/manager.py` | ✅ 三层预留、工具结果截断 |
| ProviderRouter（Failover+熔断） | `providers/router.py` | ✅ |
| CancellationToken | `core/cancellation.py` | ✅ 协作式取消 |
| AuditLogger + EventType | `observability/audit_logger.py` + `events.py` | ✅ 含 SUBAGENT_START/END 枚举 |
| SafetyGuard + PolicyEngine | `safety/guard.py` + `safety/policy.py` | ✅ 四态决策 |
| IdempotencyCache | `tools/idempotency.py` | ✅ LRU 内存缓存 |
| Session + SessionManager | `core/session.py` | ✅ |

### 🔑 关键发现

1. `EventType` 枚举中已预留 `SUBAGENT_START` / `SUBAGENT_END`
2. `TimeoutConfig` 已有 `subagent_total_s = 300.0`
3. `AgentLoop` 是纯 dispatcher，核心逻辑在 `ModelTurn._do_execute()` 和 `ToolTurn._do_execute()` 中
4. `ToolExecutor.execute()` 已有完整的 Safety → Idempotency → Secret → Execute 流水线
5. 当前没有任何 `subagent/` 目录或相关代码

---

## 架构设计

### 调用链路

```
主 Agent ToolTurn
  └── ToolExecutor.execute(spawn tool_call)
        └── SpawnTool.execute(task, message, tools, model, ...)
              └── SubAgentManager.spawn(spec, parent_depth)
                    ├── 深度/并发检查
                    ├── Budget 分配
                    └── SubAgentRunner.run(spec)
                          ├── 创建独立 ContextManager
                          ├── 创建独立 ToolRegistry（白名单过滤）
                          ├── 创建独立 HookManager（转发 progress）
                          ├── 复用 ProviderRouter（共享 Provider 池）
                          └── 内部 AgentLoop.run() → StreamResult
                                └── SubAgentResult 返回给主 Agent
```

### 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| SubAgent 的 Loop | 复用现有 `AgentLoop` | 避免重复代码，Turn 抽象已足够通用 |
| Provider | 共享主 Agent 的 `ProviderRouter` | 避免重复初始化，熔断状态共享 |
| ContextManager | 每个 SubAgent 独立创建 | 隔离上下文，避免污染主 Agent |
| ToolRegistry | 从主 Agent 白名单过滤 | 安全隔离，SubAgent 不应访问未授权工具 |
| HookManager | 独立创建，注册 progress 转发回调 | 子 Agent 事件不应直接推送到主 Agent 的 UI |
| SpawnTool 禁止自调用 | SubAgent 的 ToolRegistry 默认排除 `spawn` | 防止死循环，除非显式指定且深度未达上限 |
| Budget | 独立 Budget 实例，从父 Budget 继承削减 | V3 核心：强制限制资源消耗 |

---

## 新增文件清单

### SubAgent 模块（`myagent/subagent/`）

---

#### [NEW] `myagent/subagent/__init__.py`

导出 SubAgentSpec、SubAgentResult、Budget、SubAgentManager、SpawnTool。

---

#### [NEW] `myagent/subagent/base.py`

数据模型定义：

```python
@dataclass
class Budget:
    """资源预算，V3 核心防滥用机制。"""
    max_tokens: int = 50000        # 最大 Token 消耗
    max_tool_calls: int = 20       # 最大工具调用次数
    max_iterations: int = 20       # 最大 ReAct 迭代次数
    max_subagents: int = 0         # 该 SubAgent 允许再派生的子数量（默认 0 = 禁止嵌套）
    
    _used_tokens: int = 0
    _used_tool_calls: int = 0
    
    def consume_tokens(self, n: int) -> None: ...
    def consume_tool_call(self) -> None: ...
    def is_exhausted(self) -> bool: ...

@dataclass
class SubAgentSpec:
    """SubAgent 规格说明。"""
    task: str                              # system prompt
    initial_message: str                   # 第一条 user message
    tools: list[str] | None = None         # 工具白名单（None = 继承父集 - spawn）
    model: str | None = None               # 指定模型（None = 继承父）
    budget: Budget | None = None           # 预算（None = 使用默认）
    max_iterations: int = 20
    timeout_seconds: float = 300.0
    metadata: dict = field(default_factory=dict)

@dataclass  
class SubAgentResult:
    """SubAgent 执行结果。"""
    content: str
    tools_used: list[str]
    iterations: int
    token_usage: dict
    success: bool
    error: str | None = None
    metadata: dict = field(default_factory=dict)
```

---

#### [NEW] `myagent/subagent/runner.py`

轻量执行器，复用 AgentLoop 核心逻辑：

```python
class SubAgentRunner:
    """
    复用核心 AgentLoop 逻辑，但拥有独立上下文。
    关键：创建独立的 ContextManager + ToolRegistry + HookManager，
    但共享 ProviderRouter。
    """
    def __init__(
        self,
        spec: SubAgentSpec,
        provider_router: ProviderRouter,
        tool_registry: ToolRegistry,      # 已过滤的工具集
        parent_hooks: HookManager,         # 用于转发 progress
        depth: int,
        budget: Budget,
        cancel_token: CancellationToken | None,
        audit: AuditLogger | None,
    ): ...

    async def run(self) -> SubAgentResult:
        # 1. 创建独立 ContextManager
        # 2. 设置 system prompt = spec.task
        # 3. 添加 user message = spec.initial_message
        # 4. 创建独立 HookManager，注册 progress 转发
        # 5. 创建 AgentLoop(独立 context, 共享 router, 独立 hooks)
        # 6. loop.run(ctx) → StreamResult
        # 7. 包装为 SubAgentResult
        ...
```

---

#### [NEW] `myagent/subagent/manager.py`

生命周期管理：

```python
class SubAgentManager:
    """
    SubAgent 生命周期管理器。
    职责：并发控制、深度限制、Budget 分配、工具集过滤。
    """
    _MAX_CONCURRENT = 3    # 最大并发 SubAgent
    _MAX_DEPTH = 3         # 最大嵌套深度
    
    def __init__(
        self,
        provider_router: ProviderRouter,
        tool_registry: ToolRegistry,       # 主 Agent 的完整工具集
        hooks: HookManager,                # 主 Agent 的 HookManager
        cancel_token: CancellationToken | None,
        audit: AuditLogger | None,
        default_budget: Budget | None = None,
    ): ...

    async def spawn(
        self,
        spec: SubAgentSpec,
        parent_depth: int = 0,
    ) -> SubAgentResult:
        # 1. 深度检查
        # 2. semaphore 并发控制
        # 3. Budget 分配（继承削减 or 默认）
        # 4. 构建过滤后的 ToolRegistry
        # 5. 审计：SUBAGENT_START
        # 6. asyncio.wait_for(runner.run(), timeout)
        # 7. 审计：SUBAGENT_END
        ...

    def _build_tool_registry(self, spec: SubAgentSpec, depth: int) -> ToolRegistry:
        # 按白名单过滤；默认排除 spawn（depth >= MAX_DEPTH - 1 时）
        ...
```

---

#### [NEW] `myagent/subagent/spawn_tool.py`

LLM 可调用的工具接口：

```python
class SpawnTool(BaseTool):
    """
    派生子智能体的工具。
    LLM 通过调用此工具来 spawn SubAgent。
    """
    name = "spawn"
    description = "派生一个子智能体来完成特定子任务。..."
    parameters_schema = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "子智能体的任务描述（系统提示）"},
            "message": {"type": "string", "description": "发送给子智能体的第一条用户消息"},
            "tools": {"type": "array", "items": {"type": "string"}, "description": "可用工具名列表"},
            "model": {"type": "string", "description": "指定模型"},
            "max_iterations": {"type": "integer", "default": 20},
        },
        "required": ["task", "message"],
    }

    def __init__(self, manager: SubAgentManager, current_depth: int = 0): ...

    async def execute(self, **kwargs) -> ToolResult:
        spec = SubAgentSpec(...)
        result = await self._manager.spawn(spec, parent_depth=self._depth)
        if result.success:
            return ToolResult(content=result.content, metadata={...})
        return ToolResult(content=f"SubAgent failed: {result.error}", is_error=True)
```

---

## 现有文件修改清单

---

#### [MODIFY] `myagent/core/agent.py`

- 新增 `SubAgentManager` 初始化
- 新增 `SpawnTool` 自动注册（可通过配置开关）
- 将 `cancel_token` 传递给 `SubAgentManager`

```diff
+from myagent.subagent.manager import SubAgentManager
+from myagent.subagent.spawn_tool import SpawnTool

 class Agent:
     def __init__(self, ...):
         ...
+        # SubAgent 管理器
+        self._subagent_manager = SubAgentManager(
+            provider_router=self._router,
+            tool_registry=self._tool_registry,
+            hooks=self._hooks,
+            cancel_token=None,  # run() 时更新
+            audit=self._audit,
+        )
+        # 自动注册 spawn 工具
+        spawn_tool = SpawnTool(manager=self._subagent_manager, current_depth=0)
+        self._tool_registry.register(spawn_tool)

     async def run(self, user_input: str) -> str:
         self._cancel_token = CancellationToken()
         self._loop._cancel_token = self._cancel_token
+        self._subagent_manager._cancel_token = self._cancel_token
         ...
```

---

#### [MODIFY] `myagent/observability/audit_logger.py`

新增 SubAgent 专用便捷方法：

```diff
+    async def emit_subagent(
+        self,
+        action: str,  # "spawned" | "completed" | "failed" | "timeout"
+        task_summary: str = "",
+        depth: int = 0,
+        iterations: int | None = None,
+        tools_used: list[str] | None = None,
+        session_id: str = "",
+        **extra,
+    ) -> None:
+        event_type = EventType.SUBAGENT_START if action == "spawned" else EventType.SUBAGENT_END
+        await self.emit(event_type, session_id=session_id, ...)
```

---

#### [MODIFY] `myagent/interfaces/websocket/server.py`

- 在 `_build_agent()` 中注册 SubAgent 事件的 WebSocket 推送 Hook
- 新增 `subagent_start` / `subagent_end` Hook 回调

```diff
+    @hooks.hook("subagent_start")
+    async def _on_subagent_start(ctx, task, depth, **kw):
+        await _send({"type": "subagent_start", "task": task[:100], "depth": depth})
+
+    @hooks.hook("subagent_end") 
+    async def _on_subagent_end(ctx, success, iterations, **kw):
+        await _send({"type": "subagent_end", "success": success, "iterations": iterations})
```

---

#### [MODIFY] `myagent/core/__init__.py`

导出新增的 SubAgent 相关类型。

---

## 实施步骤（按依赖顺序）

### Step 1：数据模型（`subagent/base.py`）
- 实现 Budget、SubAgentSpec、SubAgentResult
- 实现 BudgetExhaustedError 异常
- 无外部依赖，可独立测试

### Step 2：SubAgentRunner（`subagent/runner.py`）
- 依赖：AgentLoop, ContextManager, ToolRegistry, HookManager, ProviderRouter
- 实现独立上下文创建 + AgentLoop 复用
- 实现 Budget 消耗追踪（通过 Hook 监听 token usage）
- 实现 progress 事件转发到父 HookManager

### Step 3：SubAgentManager（`subagent/manager.py`）
- 依赖：SubAgentRunner, Budget
- 实现 asyncio.Semaphore 并发控制
- 实现深度检查
- 实现工具集白名单过滤
- 实现 asyncio.wait_for 超时保护
- 集成审计事件

### Step 4：SpawnTool（`subagent/spawn_tool.py`）
- 依赖：SubAgentManager, BaseTool
- 实现 LLM 可调用的 spawn 工具
- 设计 parameters_schema（精确描述给 LLM）

### Step 5：集成到 Agent（修改 `agent.py`）
- SubAgentManager 初始化
- SpawnTool 自动注册
- CancellationToken 传递

### Step 6：WebSocket 集成（修改 `server.py`）
- SubAgent 事件推送 Hook
- 前端消息协议扩展

### Step 7：审计扩展（修改 `audit_logger.py`）
- SubAgent 专用 emit 方法
- SubAgentEvent 数据格式

---

## 防死循环保护机制

| 保护层 | 机制 | 默认值 |
|--------|------|--------|
| 深度限制 | `SubAgentManager._MAX_DEPTH` | 3 层 |
| 并发限制 | `asyncio.Semaphore(_MAX_CONCURRENT)` | 3 个 |
| Budget 令牌 | `Budget.max_tokens` | 50000 |
| Budget 工具调用 | `Budget.max_tool_calls` | 20 次 |
| Budget 迭代 | `Budget.max_iterations` | 20 轮 |
| 超时 | `asyncio.wait_for(timeout)` | 300s |
| spawn 自排除 | 深度 ≥ MAX_DEPTH-1 时自动移除 spawn 工具 | 自动 |
| CancellationToken | 父取消 → 子取消（共享 token） | 继承 |

---

## 验证计划

### 单元测试
```bash
# 数据模型测试
pytest tests/test_subagent_base.py -v

# Runner 测试（mock Provider）
pytest tests/test_subagent_runner.py -v

# Manager 测试（并发/深度/超时）
pytest tests/test_subagent_manager.py -v

# SpawnTool schema 测试
pytest tests/test_spawn_tool.py -v
```

### 集成测试
1. **基本 spawn**：主 Agent 调用 spawn → SubAgent 执行并返回
2. **并行 spawn**：同时 spawn 3 个 SubAgent，验证并发限制
3. **深度限制**：SubAgent 尝试 spawn 子 SubAgent，验证深度拦截
4. **Budget 耗尽**：设定极小 budget，验证超限终止
5. **超时保护**：设定极短超时，验证超时取消
6. **取消传播**：父 Agent 取消时 SubAgent 也被取消
7. **审计验证**：检查 JSONL 日志中的 subagent_start/end 事件

### 手动验证
通过 WebSocket 前端向 Agent 发送需要子任务拆解的请求，观察：
- SubAgent 事件是否推送到前端
- 最终结果是否正确汇总
- 审计日志是否完整

---

## 风险与缓解

| 风险 | 缓解策略 |
|------|---------|
| SubAgent 无限递归 | 三层防护：深度限制 + spawn 自排除 + Budget |
| SubAgent 占用过多资源 | Budget 令牌/调用次数硬限制 + 超时 |
| 父取消时子未清理 | 共享 CancellationToken，子 Loop 每个 await 点检查 |
| Provider 并发压力 | 共享 ProviderRouter，熔断器保护所有请求 |
| 上下文隔离不完全 | 每个 SubAgent 创建全新 ContextManager 实例 |
| 审计日志关联困难 | SubAgent 继承父 trace_id，生成独立 span_id |
