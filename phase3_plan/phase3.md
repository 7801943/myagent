# MyAgent Phase 3 — SubAgent 系统实施计划

## 概述

Phase 3 目标：实现完整的 SubAgent（子智能体）系统，使主 Agent 能通过 `spawn` 工具派生拥有独立上下文、独立工具集、受控生命周期的子 Agent，并行处理复杂子任务。

---

## 1. 新建文件清单

```
myagent/subagent/
├── __init__.py          # 导出全部公共接口
├── base.py              # Budget / SubAgentSpec / SubAgentResult 数据模型
├── runner.py            # SubAgentRunner（封装独立 ReAct 循环）
├── manager.py           # SubAgentManager（并发控制 + 深度限制 + 生命周期）
└── spawn_tool.py        # SpawnTool（继承 BaseTool，LLM 可调用）
```

---

## 2. 各组件详细设计

### 2.1 `base.py` — 数据模型

#### BudgetExceededError

```python
class BudgetExceededError(Exception):
    """预算耗尽异常，SubAgentRunner 捕获后终止循环。"""
    def __init__(self, resource: str, limit: int):
        self.resource = resource  # "tokens" | "tool_calls" | "subagents"
        self.limit = limit
        super().__init__(f"Budget exceeded: {resource} (limit={limit})")
```

#### Budget

```python
@dataclass
class Budget:
    max_tokens: int = 50000
    max_tool_calls: int = 20
    max_subagents: int = 3

    remaining_tokens: int = 50000
    remaining_tool_calls: int = 20
    remaining_subagents: int = 3

    def decrease(self, tokens: int = 0, tool_calls: int = 0, subagents: int = 0) -> bool:
        """
        扣除预算配额。
        任何资源降至 0 以下时抛出 BudgetExceededError。
        返回 True 表示扣除成功。
        """
        if tokens:
            self.remaining_tokens -= tokens
            if self.remaining_tokens < 0:
                raise BudgetExceededError("tokens", self.max_tokens)
        if tool_calls:
            self.remaining_tool_calls -= tool_calls
            if self.remaining_tool_calls < 0:
                raise BudgetExceededError("tool_calls", self.max_tool_calls)
        if subagents:
            self.remaining_subagents -= subagents
            if self.remaining_subagents < 0:
                raise BudgetExceededError("subagents", self.max_subagents)
        return True

    def clone_with_factor(self, factor: float = 0.5) -> "Budget":
        """生成继承/削减的预算副本（子 Agent 从父 Agent 继承时使用）。"""
        return Budget(
            max_tokens=int(self.max_tokens * factor),
            max_tool_calls=int(self.max_tool_calls * factor),
            max_subagents=max(1, int(self.max_subagents * factor)),
            remaining_tokens=int(self.max_tokens * factor),
            remaining_tool_calls=int(self.max_tool_calls * factor),
            remaining_subagents=max(1, int(self.max_subagents * factor)),
        )
```

#### SubAgentSpec

```python
@dataclass
class SubAgentSpec:
    task: str                          # 子智能体的系统提示（任务描述）
    tools: list[str] | None = None     # 允许使用的工具名列表，None = 继承父全部工具
    model: str | None = None           # 指定模型，None = 继承父模型
    budget: Budget | None = None       # 预算限制，None = 创建默认 Budget
    initial_message: str | None = None # 第一条用户消息
    metadata: dict = field(default_factory=dict)
```

#### SubAgentResult

```python
@dataclass
class SubAgentResult:
    content: str                       # 最终输出
    tools_used: list[str]              # 使用过的工具名列表
    iterations: int                    # 实际迭代次数
    success: bool                      # 是否成功完成
    error: str | None = None           # 错误信息
    metadata: dict = field(default_factory=dict)

    def to_summary(self) -> str:
        """生成可嵌入父 Agent 上下文的摘要。"""
        if self.success:
            return (
                f"[SubAgent 完成] 迭代 {self.iterations} 次，"
                f"使用工具: {', '.join(self.tools_used) if self.tools_used else '无'}\n"
                f"{self.content}"
            )
        else:
            return f"[SubAgent 失败] {self.error or '未知错误'}"
```

---

### 2.2 `runner.py` — SubAgentRunner

**设计原则**：直接复用现有 `ModelTurn` / `ToolTurn` / `StreamProcessor` 的 ReAct 模式，不重新发明轮子。

#### 类定义

```python
class SubAgentRunner:
    """
    轻量级子 Agent 执行器。
    复用核心 AgentLoop 的 ReAct 逻辑（ModelTurn → ToolTurn → ModelTurn），
    但拥有独立的 ContextManager、ToolRegistry、Budget。

    与 AgentLoop 的差异：
    - 无 SessionManager（不持久化子会话）
    - 有 Budget 控制（每轮扣除 token/工具配额）
    - 有 progress 回调（实时向父 Agent UI 报告进度）
    - 无 User Turn（初始消息一次性注入）
    """
```

#### 构造函数参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `spec` | `SubAgentSpec` | 子 Agent 规格 |
| `provider_router` | `ProviderRouter` | 复用父 Agent 的路由器 |
| `tool_registry` | `ToolRegistry` | 已按 spec.tools 过滤的工具注册表 |
| `hooks` | `HookManager` | 独立的 HookManager 实例 |
| `cancel_token` | `CancellationToken` | 链接到父 Agent 的取消令牌 |
| `audit_logger` | `AuditLogger` | 复用父 Agent 的审计日志 |
| `audit_parent_session_id` | `str` | 父会话 ID（用于审计关联） |
| `depth` | `int` | 当前嵌套深度 |
| `on_progress` | `Callable` | 进度回调 `async fn(delta: str)` |
| `timeout_config` | `TimeoutConfig` | 超时配置 |

#### 核心执行流程 `run() → SubAgentResult`

```
SubAgentRunner.run()
  │
  ├─ 1. 创建独立 ContextManager，注入 task 作为 system prompt
  ├─ 2. 注入 initial_message 作为首条 user 消息
  │
  └─ 3. ReAct 循环（复用 AgentLoop 的 Turn 模式）:
        │
        for iteration in range(spec.max_iterations):
          │
          ├─ 取消检查 (cancel_token.check())
          ├─ Budget 检查（剩余 token / tool_call 是否耗尽）
          │
          ├─ [ModelTurn] 创建并执行
          │    内部复用 StreamProcessor.run() 调用 Provider
          │    获取 text + tool_calls
          │
          ├─ 无 tool_calls → 循环结束，返回 SubAgentResult(success=True)
          │
          ├─ [ToolTurn] 批量执行 tool_calls
          │    内部复用 ToolExecutor.execute_batch()
          │    写入 tool_results 到 context
          │    扣除 budget.remaining_tool_calls
          │
          └─ 回到循环顶部
        │
        └─ 达到 max_iterations → SubAgentResult(success=False, error="max_iterations")
```

#### 关键实现细节

**ModelTurn 创建**（无需 ModelTurn 类的完整实例化，直接内联 StreamProcessor 调用）：

```python
async def _run_model_turn(self, ctx: HookContext) -> StreamResult:
    stream = StreamProcessor(router=self._router, hook=self._hooks)
    messages = self._context.get_messages()
    # 只传递子 Agent 允许的工具
    tools = (
        self._tool_registry.list_tools()
        if len(self._tool_registry) > 0
        else None
    )
    result = await stream.run(
        messages=messages, tools=tools, ctx=ctx,
        cancel_token=self._cancel_token,
    )
    # 扣除 token 预算
    token_count = self._context.estimate_tokens()
    self._budget.decrease(tokens=token_count)
    return result
```

**ToolTurn 执行**（复用 ToolExecutor）：

```python
async def _run_tool_turn(self, ctx: HookContext, tool_calls: list[ToolCall]):
    results = await self._executor.execute_batch(tool_calls)
    for tc, tr in zip(tool_calls, results):
        self._context.add_tool_result(tc.id, MsgToolResult(
            tool_call_id=tc.id, tool_name=tc.name, content=tr.content
        ))
    # 扣除工具调用预算
    self._budget.decrease(tool_calls=len(tool_calls))
```

**审计覆盖**：

```python
# run() 开始时:
await self._audit.emit(EventType.SUBAGENT_START,
    session_id=self._audit_parent_session_id,
    task=self._spec.task, depth=self._depth,
    agent_id=f"subagent_{uuid4().hex[:8]}",
)

# run() 结束时 (成功):
await self._audit.emit(EventType.SUBAGENT_END,
    session_id=self._audit_parent_session_id,
    sub_type="completed",
    depth=self._depth, iterations=iterations,
    tools_used=tools_used,
)

# run() 异常时 (失败/超时):
await self._audit.emit(EventType.SUBAGENT_END,
    session_id=self._audit_parent_session_id,
    sub_type="failed" | "timeout",
    error=str(e),
)
```

**取消传播**：SubAgentRunner 的 `_cancel_token` 直接链接到父 Agent 的 `CancellationToken`，父 Agent 取消时子 Agent 同步终止。

**并发安全**：每个 SubAgentRunner 拥有独立的 `ContextManager` 和 `ToolRegistry`，无共享可变状态，天然并发安全。

---

### 2.3 `manager.py` — SubAgentManager

#### 类定义

```python
class SubAgentManager:
    """
    子智能体管理器。
    职责：
    1. 并发控制（asyncio.Semaphore）
    2. 深度限制（防递归爆炸）
    3. 工具白名单过滤
    4. 审计事件发射
    """

    _MAX_DEPTH = 3       # 最大嵌套深度
    _MAX_CONCURRENT = 3  # 最大并发 SubAgent
```

#### 构造函数

```python
def __init__(
    self,
    provider_router: ProviderRouter,
    tool_registry: ToolRegistry,
    audit_logger: AuditLogger | None = None,
    timeout_config: TimeoutConfig | None = None,
    max_depth: int = 3,
    max_concurrent: int = 3,
):
    self._router = provider_router
    self._base_tool_registry = tool_registry  # 父 Agent 的完整工具集
    self._audit = audit_logger
    self._timeout_config = timeout_config or TimeoutConfig()
    self._MAX_DEPTH = max_depth
    self._MAX_CONCURRENT = max_concurrent
    self._semaphore = asyncio.Semaphore(max_concurrent)
```

#### spawn() 方法

```python
async def spawn(
    self,
    spec: SubAgentSpec,
    parent_depth: int = 0,
    parent_cancel_token: CancellationToken | None = None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    parent_session_id: str = "",
) -> SubAgentResult:
    """
    创建并运行 SubAgent，返回结果。

    流程：
    1. 深度检查（parent_depth >= MAX_DEPTH → 拒绝）
    2. 构建过滤后的 ToolRegistry
    3. 获取 Semaphore（并发控制）
    4. 创建 SubAgentRunner 并执行
    5. 带整体超时控制（asyncio.wait_for）
    """
    # 1. 深度检查
    current_depth = parent_depth + 1
    if current_depth > self._MAX_DEPTH:
        return SubAgentResult(
            content="", tools_used=[], iterations=0,
            success=False,
            error=f"Max subagent depth ({self._MAX_DEPTH}) exceeded",
        )

    # 2. 构建过滤后的工具注册表
    filtered_registry = self._build_tool_registry(spec.tools)

    # 3. 获取并发信号量
    async with self._semaphore:
        # 4. 创建并执行 SubAgentRunner
        runner = SubAgentRunner(
            spec=spec,
            provider_router=self._router,
            tool_registry=filtered_registry,
            hooks=HookManager(),
            cancel_token=parent_cancel_token,
            audit_logger=self._audit,
            audit_parent_session_id=parent_session_id,
            depth=current_depth,
            on_progress=on_progress,
            timeout_config=self._timeout_config,
        )

        try:
            result = await asyncio.wait_for(
                runner.run(),
                timeout=self._timeout_config.subagent,
            )
        except asyncio.TimeoutError:
            # 审计: timeout
            if self._audit:
                await self._audit.emit(EventType.SUBAGENT_END,
                    session_id=parent_session_id,
                    sub_type="timeout",
                    depth=current_depth,
                )
            return SubAgentResult(
                content="", tools_used=[], iterations=0,
                success=False, error=f"SubAgent timed out",
            )

    return result
```

#### _build_tool_registry() — 工具白名单过滤

```python
def _build_tool_registry(self, tools: list[str] | None) -> ToolRegistry:
    """根据 spec.tools 白名单过滤工具集。"""
    if tools is None:
        # 继承父 Agent 全集
        return self._base_tool_registry

    registry = ToolRegistry()
    for name in tools:
        tool = self._base_tool_registry.get(name)
        if tool:
            registry.register(tool)
        else:
            logger.warning(f"SubAgent requested unknown tool: {name}")
    return registry
```

#### 并行 SubAgent 支持

主 Agent 可以在同一轮 ReAct 中 spawn 多个 SubAgent（通过 `execute_batch` 并行执行多个 `spawn` tool_call）：

```
主 Agent 决策 → tool_calls: [spawn(A), spawn(B), spawn(C)]
                          ↓
            ToolExecutor.execute_batch()
                          ↓
          asyncio.gather(*[spawn_A, spawn_B, spawn_C])
                          ↓
             SubAgentManager.spawn() ×3
             (Semaphore 控制并发 ≤ MAX_CONCURRENT)
```

`asyncio.Semaphore` 确保相同深度的 SubAgent 不会超过 `MAX_CONCURRENT` 个同时运行。

---

### 2.4 `spawn_tool.py` — SpawnTool

```python
class SpawnTool(BaseTool):
    """
    LLM 可调用的 spawn 工具。
    派生一个子智能体来完成特定子任务，收集其输出作为工具结果。

    使用场景：
    - 并行分析多份文档
    - 隔离执行高风险操作
    - 专项问题深度推理
    """
    name = "spawn"
    description = (
        "派生一个子智能体来完成特定子任务。"
        "适合用于：并行处理、隔离执行、专项分析等场景。"
        "子智能体拥有独立上下文，完成任务后返回最终输出。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "子智能体的任务描述（作为系统提示）",
            },
            "message": {
                "type": "string",
                "description": "发送给子智能体的第一条用户消息",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "子智能体可使用的工具名列表。不填则继承父智能体全部工具",
            },
            "model": {
                "type": "string",
                "description": "指定子智能体使用的模型名。不填则继承父智能体模型",
            },
            "max_iterations": {
                "type": "integer",
                "default": 20,
                "description": "最大迭代次数限制",
            },
        },
        "required": ["task", "message"],
    }

    def __init__(self, manager: SubAgentManager, depth: int = 0, cancel_token=None):
        self._manager = manager
        self._depth = depth
        self._cancel_token = cancel_token

    async def execute(
        self, task: str, message: str, tools=None, model=None, max_iterations=20
    ) -> ToolResult:
        spec = SubAgentSpec(
            task=task,
            initial_message=message,
            tools=tools,
            model=model,
            budget=Budget(max_tool_calls=max_iterations),
        )
        result = await self._manager.spawn(
            spec=spec,
            parent_depth=self._depth,
            parent_cancel_token=self._cancel_token,
        )
        if result.success:
            return ToolResult(
                content=result.content,
                metadata={
                    "iterations": result.iterations,
                    "tools_used": result.tools_used,
                    "depth": self._depth + 1,
                },
            )
        return ToolResult(
            content=f"SubAgent failed: {result.error}",
            is_error=True,
            metadata={"error": result.error},
        )
```

---

### 2.5 `__init__.py` — 公共导出

```python
"""MyAgent SubAgent：子智能体系统。"""
from myagent.subagent.base import Budget, BudgetExceededError, SubAgentSpec, SubAgentResult
from myagent.subagent.runner import SubAgentRunner
from myagent.subagent.manager import SubAgentManager
from myagent.subagent.spawn_tool import SpawnTool

__all__ = [
    "Budget", "BudgetExceededError",
    "SubAgentSpec", "SubAgentResult",
    "SubAgentRunner",
    "SubAgentManager",
    "SpawnTool",
]
```

---

## 3. 现有文件修改清单

### 3.1 `myagent/core/agent.py` — Agent 集成

在 `Agent.__init__` 中新增 SubAgentManager 的创建和 SpawnTool 的注册：

```python
# 在 Agent.__init__ 的末尾、系统 prompt 设置之前添加

# --- Phase 3: SubAgent 系统集成 ---
if subagent_enabled:
    self._subagent_manager = SubAgentManager(
        provider_router=self._router,
        tool_registry=self._tool_registry,
        audit_logger=self._audit,
        timeout_config=self._timeout_config,
    )
    # 注册 spawn 工具
    self._tool_registry.register(
        SpawnTool(
            manager=self._subagent_manager,
            depth=0,
            cancel_token=None,  # 每次 run 时更新
        )
    )
```

每次 `Agent.run()` 调用时，更新 SpawnTool 的 cancel_token：

```python
async def run(self, user_input: str) -> str:
    self._cancel_token = CancellationToken()
    self._loop._cancel_token = self._cancel_token

    # 更新 SpawnTool 的 cancel_token
    spawn_tool = self._tool_registry.get("spawn")
    if spawn_tool and isinstance(spawn_tool, SpawnTool):
        spawn_tool._cancel_token = self._cancel_token
    ...
```

### 3.2 `myagent/tools/executor.py` — 无需修改

`execute_batch()` 已通过 `asyncio.gather` 并行执行，天然支持多个 `spawn` tool_call 并行。无需额外改动。

### 3.3 `myagent/utils/config.py` — 超时配置扩展

在 `TimeoutConfig` 中新增 subagent 超时字段：

```python
@dataclass
class TimeoutConfig:
    llm_generation: float = 120.0
    tool_batch: float = 60.0
    iteration: float = 300.0
    subagent: float = 300.0     # Phase 3 新增：SubAgent 整体超时
```

### 3.4 `myagent/observability/events.py` — 无需修改

`EventType.SUBAGENT_START` 和 `EventType.SUBAGENT_END` 已存在（V2 预留），直接使用。

### 3.5 `myagent/observability/audit_logger.py` — 便捷方法扩展

新增两个便捷方法：

```python
async def emit_subagent_start(
    self, session_id: str, task: str, depth: int, **extra
) -> None:
    """发射 SubAgent 启动事件。"""
    await self.emit(
        EventType.SUBAGENT_START,
        session_id=session_id,
        task=task, depth=depth, **extra,
    )

async def emit_subagent_end(
    self, session_id: str, sub_type: str, depth: int, **extra
) -> None:
    """发射 SubAgent 结束事件（completed / failed / timeout）。"""
    await self.emit(
        EventType.SUBAGENT_END,
        session_id=session_id,
        sub_type=sub_type, depth=depth, **extra,
    )
```

### 3.6 `myagent/__init__.py` — 顶层导出（可选）

---

## 4. 防死循环保护汇总

| 机制 | 位置 | 行为 |
|------|------|------|
| **深度限制** | `SubAgentManager._MAX_DEPTH = 3` | 超过最大嵌套深度时拒绝 spawn，返回错误 ToolResult |
| **并发限制** | `SubAgentManager._semaphore` | `asyncio.Semaphore(3)` 限制同深度并发 SubAgent 数量 |
| **Budget Token 控制** | `Budget.decrease(tokens=...)` | 每轮 LLM 调用后扣除 token 估算值，耗尽抛 `BudgetExceededError` |
| **Budget 工具控制** | `Budget.decrease(tool_calls=...)` | 每次工具调用后扣除计数，耗尽抛 `BudgetExceededError` |
| **Budget 递归控制** | `Budget.decrease(subagents=...)` | spawn 时扣除子 Agent 配额，耗尽抛 `BudgetExceededError`（限制孙 Agent 数量） |
| **迭代上限** | SubAgentSpec → `max_iterations` | 默认 20 轮，防止无限制 ReAct 循环 |
| **整体超时** | `asyncio.wait_for(runner.run(), timeout=...)` | 默认 300s，超时后返回失败 SubAgentResult |
| **取消传播** | `CancellationToken` | 父 Agent 取消时，通过共享的 cancel_token 同步终止所有子 Agent |

---

## 5. 测试计划

### 单元测试

| 测试项 | 测试内容 |
|--------|---------|
| `test_base.py::test_budget_decrease` | Budget.decrease 正常扣除及边界检查 |
| `test_base.py::test_budget_exceeded` | 各种资源配置耗尽时正确抛出 BudgetExceededError |
| `test_base.py::test_budget_clone` | clone_with_factor 正确按比例削减 |
| `test_base.py::test_subagent_result_summary` | to_summary() 格式化正确 |

### 集成测试

| 测试项 | 测试内容 |
|--------|---------|
| `test_subagent.py::test_spawn_success` | 简单 spawn 成功返回结果 |
| `test_subagent.py::test_spawn_no_tools` | SubAgent 无工具时的纯文本推理 |
| `test_subagent.py::test_spawn_with_tools` | SubAgent 使用指定工具完成任务 |
| `test_subagent.py::test_parallel_spawn` | 并行 spawn 3 个子 Agent |
| `test_subagent.py::test_max_concurrent` | Semaphore 正确限制并发数 |
| `test_subagent.py::test_max_depth` | 超过深度限制时正确拒绝 |
| `test_subagent.py::test_budget_exceeded` | Budget 耗尽后子 Agent 正确终止 |
| `test_subagent.py::test_cancel_propagation` | 父 Agent 取消时子 Agent 同步取消 |
| `test_subagent.py::test_spawn_timeout` | 子 Agent 超时后返回失败 |
| `test_subagent.py::test_audit_events` | 审计事件正确记录 SUBAGENT_START / END |

---

## 6. 实施顺序

```
Step 1: base.py         ← 数据模型（无依赖）
Step 2: runner.py       ← 执行器（依赖 base + 现有 core 模块）
Step 3: manager.py      ← 管理器（依赖 base + runner）
Step 4: spawn_tool.py   ← 工具接口（依赖 base + manager）
Step 5: 修改 agent.py   ← 集成到 Agent 构造函数
Step 6: 修改 config.py  ← 新增 subagent timeout
Step 7: 修改 audit_logger.py ← 新增便捷方法
Step 8: 编写单元测试    ← test_base.py / test_subagent.py
Step 9: 编写集成测试    ← 端到端 spawn 流程验证
```

---

## 7. 关键设计决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| Runner 是否创建完整 Agent | **否**，直接内联 ModelTurn/ToolTurn 逻辑 | 避免引入 SessionManager / 持久化等不必要的复杂度；V3 规格要求"复用 AgentLoop 核心逻辑" |
| 工具过滤方式 | `ToolRegistry` 按名称白名单过滤 | 简单、高效、无歧义；子 Agent 不应访问未经授权的工具 |
| Budget 与父 Agent 关系 | 独立 Budget，通过 `clone_with_factor` 继承削减 | 防止子 Agent 耗尽父 Agent 的 token 配额 |
| 取消链路 | 共享 `CancellationToken` 实例 | 父 Agent 取消时必须立即终止所有子 Agent |
| 审计日志 | 使用 `audit_parent_session_id` 关联父会话 | 所有 SubAgent 事件归属到父会话的上下文中 |
| Hook 体系 | SubAgent 拥有独立 HookManager | 子 Agent 不应干扰父 Agent 的 UI / 事件流，progress 通过专用回调传递 |
