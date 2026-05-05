# Core 精简重构方案 — Review 与建议

> 对 `core精简.md` 的重构方案进行系统性审查，基于实际代码仓库的完整分析。

---

## 一、总体评价

方案的整体方向是合理的：
- 精简 core 模块（11 → 7 文件），提升模块内聚性
- 消除 `CancellationToken` 和 `asyncio.Task.cancel()` 并存的双重取消机制
- 将 `SafetyGuard` 从 `ToolExecutor` 移到 `HumanTurn`，实现更清晰的安全/执行分离
- 新状态机 `HUMAN → MODEL → HUMAN → TOOL → MODEL → HUMAN` 将 HumanTurn 作为中枢

但步骤 4 和步骤 5 存在多处细节缺陷，需要修正后再执行。

---

## 二、各步骤逐项审查

### 步骤一：删除 hitl.py — 已过时

**发现**：`hitl.py` 源文件**已经不存在**，只有编译残留 `myagent/core/__pycache__/hitl.cpython-312.pyc`（4,725 字节，日期 May 2）。

`HITLController` / `CLIHITLController` 已在之前的重构中被 `HumanTurn` + `approval_handler` 回调完全替代。

**建议**：
1. 直接从方案中移除步骤一（无需再删）
2. 清理 `__pycache__` 中的残留 `.pyc` 文件：`rm myagent/core/__pycache__/hitl.cpython-312.pyc`
3. 方案验证命令 `grep -r "hitl"` 应改为检查 import 语句而非字符串匹配——当前 `hitl` 作为 HITL 概念词出现在 safety/base.py、tools/executor.py、turns.py 等多处，grep 会误报。正确命令：

```bash
grep -rP "from myagent\.core\.hitl|import.*hitl" myagent/ --include="*.py"
```

---

### 步骤二：移出 parser.py 到 utils/ — 合理但需处理向后兼容

**发现**：

`StructuredOutputParser` 在 `myagent/core/parser.py` 中定义，通过 `core/__init__.py` 导出。在整个业务代码中**无任何实际调用**——仅 `__init__.py` 的 re-export 引用它，无其他 `.py` 文件 import 或使用。

`myagent/utils/` 目录已存在，包含 `__init__.py`、`config.py`、`logging.py`、`retry.py`、`timeout.py`。

**问题**：外部使用者可能通过以下路径导入：
- `from myagent.core import StructuredOutputParser`
- `from myagent.core.parser import StructuredOutputParser`

移动后这两个路径都会断裂。

**建议**：

1. 移动后保留一个 deprecated re-export 在 `core/__init__.py`：

```python
import warnings
from myagent.utils.parser import StructuredOutputParser as _StructuredOutputParser

def __getattr__(name):
    if name == "StructuredOutputParser":
        warnings.warn(
            "StructuredOutputParser 已移至 myagent.utils.parser，"
            "请更新 import 路径。",
            DeprecationWarning, stacklevel=2
        )
        from myagent.utils.parser import StructuredOutputParser
        return StructuredOutputParser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

2. 更新 `core/__init__.py` 的 `__all__` 列表

---

### 步骤三：移出 factory.py 到顶层 — 有遗漏

**发现**：

方案第 88-89 行列的消费者文件中**遗漏了一个**：

| 文件 | 行号 | import 语句 |
|------|------|-------------|
| `myagent/interfaces/cli/main.py` | 14 | `from myagent.core.factory import AgentFactory` |
| `myagent/interfaces/web/ws_handler.py` | 25 | `from myagent.core.factory import AgentFactory` |
| **`myagent/interfaces/web/dependencies.py`** | **9** | **`from myagent.core.factory import AgentFactory`** |

方案原文只提到了前两个，需补充第三个。

**建议**：
1. 将 `myagent/interfaces/web/dependencies.py` 加入步骤三的修改列表
2. 更新验证 grep 命令确认无遗漏

---

### 步骤四：删除 cancellation.py — 核心问题（需重点修正）

#### 问题 4.1：方案自相矛盾 — Session 和 AgentLoop 同时 catch CancelledError

**最严重的问题。**

方案第 4.1 节在 `Session.run()` 中 catch `asyncio.CancelledError`，第 4.2 节又在 `AgentLoop.run()` 中 catch `asyncio.CancelledError`。由于 `Session.run()` 内部调用 `self._loop.run()`，如果 AgentLoop 先 catch 了异常，Session 的 catch 块永远不会被触发。两者只能保留其一。

**当前代码行为**：
- `AgentLoop.run()` (loop.py:155-175) catch `AgentCancelledError` → 写取消消息到 context → 返回 `StreamResult`
- `Session.run()` (session.py:153-167) catch `AgentCancelledError` → persist → re-raise
- `Session.run()` (session.py:161-167) catch `asyncio.CancelledError` → persist → re-raise（当前存在但不经过 AgentLoop 处理）

**建议**：明确取消的**唯一入口**是 `AgentLoop.run()`：

```python
# AgentLoop.run() — 唯一取消处理层
except asyncio.CancelledError:
    cancel_msg = f"[系统] 操作已取消"
    self._context.add_assistant_message(content=cancel_msg, tool_calls=None)
    if self._audit:
        await self._audit.emit_cancelled(...)
    return StreamResult(text=cancel_msg, stop_reason="cancelled")
```

```python
# Session.run() — 不 catch asyncio.CancelledError，让其向上传播
# 但如果取消发生在 Session 层（如 add_user_message），需要 shield persist：
try:
    self._context.add_user_message(user_input)
    result = await self._loop.run(ctx)  # AgentLoop 自己 handle cancel
    ...
except asyncio.CancelledError:
    # 只有 Session 层自身被取消时才到此处
    await asyncio.shield(self._persist(...))
    raise
```

同时**删除**当前 `Session.run()` 中冗余的 `except AgentCancelledError` 块（session.py:153-159）。

#### 问题 4.2：`_running_task` 生命周期管理缺失

方案引入 `session._running_task` 供 `request_cancel()` 使用，但未说明谁负责设置/清理它。

**当前**：WebSocket handler 自管 `_running_tasks` 字典，CLI 无 task 管理。

**建议**：让 `Session.run()` 内部自动管理，避免外部调用者遗忘：

```python
async def run(self, user_input: str) -> str:
    self._running_task = asyncio.current_task()
    try:
        ...
    finally:
        self._running_task = None
```

#### 问题 4.3：取消理由信息丢失

当前 `CancelReason` 携带 `reason`（枚举：`USER_CANCEL`, `TIMEOUT`）和 `detail`（自由文本），取消消息格式为 `[系统] 操作已取消 — {reason}: {detail}`。

方案改为用 `Session._cancel_reason` / `_cancel_detail` 字符串存储，但消息格式中缺少了 `detail` 信息（方案第 142 行只用了 reason）。

**建议**：在 `AgentLoop.run()` 取消处理中保留 reason + detail 双字段，维持与当前一致的取消消息格式。

#### 问题 4.4：`CancelReason.TIMEOUT` 从未被使用

代码搜索确认 `CancelReason.TIMEOUT` 被定义但从未在代码中被设置。看门狗（`BaseTurn._watchdog`）只通过 hook 发射 `timeout_warning`，不触发取消。删除时无需处理。

#### 问题 4.5：Breaking Change — 公开 API 移除

`core/__init__.py` 公开导出以下 3 个符号：

```python
from myagent.core.cancellation import CancellationToken, CancelReason, AgentCancelledError
```

删除后 `__all__` 从 15 减至 12 个符号。任何外部代码 `from myagent.core import AgentCancelledError` 会断裂。

**建议**：同步骤二，保留 deprecated re-export 过渡期。

#### 问题 4.6：受影响文件完整清单

| 文件 | 当前 import | 修改 |
|------|------------|------|
| `core/__init__.py` | Re-export `CancellationToken, CancelReason, AgentCancelledError` | 删除或替换为 deprecated re-export |
| `core/agent.py:22` | `from myagent.core.cancellation import CancelReason` | 删除 |
| `core/loop.py:19` | `from myagent.core.cancellation import CancellationToken, AgentCancelledError, CancelReason` | 删除，改为 catch `asyncio.CancelledError` |
| `core/session.py:18` | `from myagent.core.cancellation import CancellationToken, CancelReason, AgentCancelledError` | 删除，改为 `_cancel_reason: str` |
| `core/stream.py:17` | `from myagent.core.cancellation import CancellationToken, AgentCancelledError, CancelReason` | 删除 cancel_token 参数 |
| `core/turns.py:30` | `from myagent.core.cancellation import CancellationToken, AgentCancelledError, CancelReason` | 删除 cancel_token 参数 |
| `interfaces/cli/main.py:16` | `from myagent.core.cancellation import AgentCancelledError` | 删除，改为 catch `asyncio.CancelledError` |
| `interfaces/web/ws_handler.py:27` | `from myagent.core.cancellation import AgentCancelledError, CancelReason` | 删除，改为 catch `asyncio.CancelledError` |

#### 步骤四总结

| 评估维度 | 结论 |
|---------|------|
| 方向正确性 | 正确 — `CancellationToken` 确实在重新发明 `asyncio` 已有能力 |
| 方案完整性 | 不足 — 需修正 4.1 冲突、4.2 缺失、4.3 信息丢失 |
| 风险等级 | **中高** — 涉及 8 个文件修改，取消是贯穿全链路的核心机制 |
| 测试覆盖 | 方案仅提 `python -m pytest`，需补充具体取消场景测试 |

---

### 步骤五：状态机重构 — 多处分歧

#### 问题 5.1：SafetyGuard 接口调用参数不匹配

方案第 5.1 节伪代码第 273 行：

```python
result = await self._safety_guard.check_tool_call(tc.name, tc.arguments)
```

但实际 `SafetyGuard.check_tool_call()` 签名（`myagent/safety/guard.py`）为：

```python
async def check_tool_call(self, tool_name: str, args: dict, session_id: str) -> GuardResult:
```

**缺少 `session_id` 参数**，执行时会报 `TypeError: check_tool_call() missing 1 required positional argument: 'session_id'`。

**修正**：
```python
result = await self._safety_guard.check_tool_call(tc.name, tc.arguments, ctx.session_id)
```

#### 问题 5.2：枚举值比较方式不安全

方案第 280 行：

```python
if result.decision.value == "rewrite":
```

`PolicyDecision` 是 `str, Enum`，`.value` 返回字符串。用字符串比较：
- 如果枚举定义变为 `REWRITE = "rewrite_args"`，静默失败
- IDE 无法提供自动补全和重构支持

**修正**：直接使用枚举成员比较：
```python
if result.decision == PolicyDecision.REWRITE:
```

#### 问题 5.3：HumanTurn 职责过重（God Object 倾向）

方案中 HumanTurn 承载 4 种角色，根据 `source` 参数切换行为：

| source | 角色 | 职责 |
|--------|------|------|
| `None` | 入口 | 合规检查、系统指令处理、用户输入接收 |
| `MODEL` + tools | 安全围栏 | SafetyGuard 检查 + 审批路由 |
| `MODEL` + no tools | 结束判定 | 决定循环终止 |
| — | 阻塞入口 | 等待用户输入（loop 概念） |

这本质上是一个"根据 source 分发"的大 switch 函数，违反单一职责原则。未来新增角色（如 `source=TOOL` 用于工具执行后的审计）会进一步膨胀。

**建议**：将入口角色拆分：

```
EntryTurn（新增） → MODEL Turn → HumanTurn（审批） → TOOL Turn → MODEL Turn
      ↑                                                              ↓
      └──────────────── 无工具调用时结束 ←─────────────────────────────┘
```

`EntryTurn` 负责：用户输入合规检查、系统指令处理、未来扩展。
`HumanTurn` 只负责：安全围栏 + 审批路由（原有职责不动）。

这样：

| 新结构 | 职责 |
|--------|------|
| `EntryTurn` | 入口：接收用户 query、合规检查、系统指令、路由到 MODEL |
| `HumanTurn` | 审批：安全围栏检查、用户审批、路由到 TOOL 或 MODEL |

#### 问题 5.4：额外循环迭代的开销

**无工具调用的简单消息：**

| 版本 | 迭代路径 | 次数 |
|------|---------|------|
| 当前 | `MODEL → None` | 1 |
| 方案 | `HUMAN → MODEL → HUMAN → None` | 2 |
| 建议 | `ENTRY → MODEL → (结束)` 或 `ENTRY → MODEL → HUMAN → None` | 1 或 2 |

即使 HumanTurn 入口 pass-through 是 trivial 的（仅 `return TurnResult(next_turn=TurnKind.MODEL)`），`BaseTurn.execute()` 模板方法仍需：
1. `await self._cancel.check()` — 取消检查
2. `asyncio.create_task(self._watchdog(ctx))` — 看门狗任务
3. Hook 发射（`state_change` 等）
4. 审计日志 (`iteration_start`/`iteration_end`)

在批量/高频场景下，2x 的开销会累积。

#### 问题 5.5：ToolExecutor 的 `skip_safety` 清理不彻底

方案第 5.5 节伪代码中仍保留 `skip_safety` 参数：

```python
async def execute(self, tool_call, skip_safety=False) -> ToolResult:
    # 删除: 整个安全检查分支（if not skip_safety and self._safety_guard）
```

既然安全逻辑已完全移出 ToolExecutor，`skip_safety` 参数和所有安全相关代码应**彻底删除**，不应保留任何残余。

ToolExecutor 清理后应为纯粹的"查找工具 → 注入凭据 → 执行 → 记录缓存"，不含任何安全检查。

#### 问题 5.6：方案未列出的 Import 变更

| 文件 | 删除 import | 新增 import |
|------|------------|------------|
| `tools/executor.py` | `safety_guard` 参数接收、safety 相关 import | — |
| `core/turns.py` (ToolTurn) | — | — |
| `core/turns.py` (HumanTurn) | — | `SafetyGuard`, `PolicyDecision`, `GuardResult`, `ToolCall`, `MsgToolResult` |
| `core/loop.py` | — | `SafetyGuard`（传给 HumanTurn） |
| `core/agent.py` | — | `SafetyGuard`（存储并传给 Session） |
| `core/session.py` | — | `SafetyGuard`（存储并传给 AgentLoop） |

#### 问题 5.7：`safety_guard` 传递链路延长

| 版本 | 传递链路 | 层数 |
|------|---------|------|
| 当前 | `Agent.__init__` → `ToolExecutor.__init__` | 2 |
| 方案 | `Agent.__init__` → `Session.__init__` → `AgentLoop.__init__` → `HumanTurn.__init__` | 4 |

构造函数参数增多增加了维护成本。如果未来 `safety_guard` 需要同时被 `EntryTurn` 和 `HumanTurn` 使用（合规检查 + 工具安全），链路更长。

#### 问题 5.8：对 `model_turn` 返回值的变更影响

方案要求 `ModelTurn._do_execute()` 将 `next_turn` 从直接路由改为全部经过 HUMAN：

```python
# 原来
next_turn=TurnKind.TOOL      # 有工具 → TOOL
next_turn=None                # 无工具 → 结束

# 方案
next_turn=TurnKind.HUMAN      # 所有情况 → HUMAN
```

这意味着 `TurnResult.data` 的语义变化：
- 无工具时：当前 `data=None`，方案需要 `data=None`（兼容）
- 有工具时：当前 `data=tool_calls`，方案 `data=tool_calls`（兼容）

`HumanTurn._do_execute()` 用 `source` 区分 MODEL 有/无 tool_calls 是正确的。

#### 步骤五总结

| 评估维度 | 结论 |
|---------|------|
| 方向正确性 | 正确 — SafetyGuard 归位 HumanTurn、ToolExecutor 纯化为执行器 |
| 方案完整性 | 不足 — 接口不匹配、枚举比较错误、import 变更遗漏 |
| 风险等级 | **高** — 涉及状态机核心逻辑变更、7+ 文件修改 |
| 架构考量 | HumanTurn 职责过重，建议拆分 EntryTurn |

---

## 三、方案遗漏的全局问题

### 3.1 Breaking Change 无向后兼容策略

步骤二、三、四都会改变公开 API。方案未提及：
- 是否需要 `DeprecationWarning`
- 过渡期的版本号策略
- 对外部使用者（import 这些模块的第三方代码）的迁移指南

### 3.2 验证命令不够精确

方案各步验证用 `grep` 搜索时存在以下问题：

| 步骤 | 方案命令 | 问题 |
|------|---------|------|
| 步骤一 | `grep -r "hitl"` | `hitl` 作为子串出现在 `safety/base.py`、`turns.py` 等多处，误报严重 |
| 步骤二 | `grep -r "from myagent.core.parser"` | 未匹配 `from myagent.core import StructuredOutputParser` 形式 |
| 步骤四 | `grep -r "CancellationToken"` | 遗漏 `asyncio.CancelledError` 的检查 |

**建议**：提供精确的 grep 正则或直接列出需要 grep 的文件列表。

### 3.3 测试策略不足

方案验证栏只写 `python -m pytest`，未列出：
- 哪些具体测试文件覆盖了取消逻辑
- 哪些测试覆盖了状态机转换
- 是否需要新增测试（如：取消发生在不同 Turn 阶段的行为）
- 手动测试场景的具体步骤和预期结果

### 3.4 `max_iterations` 影响

当前 `max_iterations=50`。新状态机下每次交互最少 2 次迭代，实际可用工具调用次数减半（最多 ~25 轮 tool use vs 当前 ~50 轮）。虽然 25 轮通常足够，但应该在方案中明确注明。

---

## 四、修正后的执行顺序建议

```
步骤 1（原步骤二）：移出 parser.py → utils/
步骤 2（原步骤三）：移出 factory.py → myagent/factory.py（含 dependencies.py）
    ↓
步骤 3（原步骤四）：asyncio.CancelledError 替代 CancellationToken
                    （需修正：取消入口唯一化、_running_task 生命周期）
    ↓
步骤 4（原步骤五）：状态机重构
    ├── 4a: 拆分 EntryTurn（新增）+ HumanTurn 简化
    ├── 4b: SafetyGuard 移入 HumanTurn
    ├── 4c: ToolExecutor 纯化（删除所有安全逻辑）
    └── 4d: 更新 Loop 状态机文档
```

原步骤一（删除 hitl.py）已不存在，直接跳过。

---

## 五、总结

| 步骤 | 原风险评级 | 修正后风险 | 主要问题数 |
|------|-----------|-----------|-----------|
| 步骤一（hitl） | 零风险 | N/A（已完成） | 0 |
| 步骤二（parser） | 低风险 | 低风险 | 1（向后兼容） |
| 步骤三（factory） | 低风险 | 低风险 | 1（遗漏文件） |
| 步骤四（取消） | 中等风险 | **中高** | 6 |
| 步骤五（状态机） | 高风险 | **高** | 8 |

方案在架构方向上正确，但在实现细节层面有显著缺陷。建议在正式开始重构前先修正本文列出的问题。
