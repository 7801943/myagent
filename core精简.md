# Core 模块精简重构计划

## 目标架构

### 新状态机

```
HUMAN → MODEL → HUMAN → TOOL → MODEL → HUMAN → ...
  ↑                ↓
  入口            结束（无 tool_calls）
```

HumanTurn 成为整个 Loop 的入口和中枢，承载多重角色：
1. **接收用户 query**：未来可做合规检查，不合规则暂停等待用户修改
2. **系统指令处理**：`/new_session`、`/model` 等会话配置命令
3. **安全围栏 + 用户审批**：SafetyGuard 从 ToolExecutor 移入 HumanTurn
4. **等待用户输入**：作为 loop 的阻塞入口

HumanTurn 不做旁路优化——工具执行数量有限，空循环性能影响可忽略。

### 精简后的 core 文件结构（从 11 → 7）

```
core/
├── __init__.py      # 导出
├── agent.py         # Agent（配置持有 + 会话工厂）
├── session.py       # Session（会话容器）
├── loop.py          # AgentLoop（ReAct dispatcher）
├── turns.py         # BaseTurn + ModelTurn + ToolTurn + HumanTurn
├── stream.py        # StreamProcessor + StreamResult
└── hook.py          # HookManager + HookContext + HookHandle
```

删除/移出的文件：
- `hitl.py` → 🗑️ 删除（废弃代码，无引用）
- `parser.py` → 📦 移到 `myagent/utils/parser.py`
- `factory.py` → 📦 移到 `myagent/factory.py`
- `cancellation.py` → 🗑️ 删除（用 `asyncio.Task.cancel()` 替代）

---

## 步骤一：删除废弃的 hitl.py（零风险）

### 背景
`HITLController` 和 `CLIHITLController` 已被 `HumanTurn` + `approval_handler` 回调完全取代，无任何引用。

### 操作
1. 删除 `myagent/core/hitl.py`
2. 确认无文件 import `hitl` 模块（应该没有）
3. 运行测试确认无影响

### 验证
```bash
grep -r "hitl" myagent/ --include="*.py" -l
# 应该只返回 config 相关的 hitl 配置引用，不应有 core/hitl 的 import
```

---

## 步骤二：移出 parser.py 到 utils/（低风险）

### 背景
`StructuredOutputParser` 是纯工具类（从文本提取 JSON/代码块），与 core 的 ReAct 循环引擎无逻辑关联。

### 操作
1. 将 `myagent/core/parser.py` 移动到 `myagent/utils/parser.py`
2. 全局搜索 `from myagent.core.parser` 和 `from myagent.core import.*StructuredOutputParser`，更新 import 路径为 `from myagent.utils.parser`
3. 更新 `myagent/core/__init__.py`：移除 `StructuredOutputParser` 的导出
4. 运行测试

### 验证
```bash
grep -r "from myagent.core.parser\|from myagent.core import.*StructuredOutputParser" myagent/ --include="*.py"
# 应该无结果
```

---

## 步骤三：移出 factory.py 到顶层（低风险）

### 背景
`AgentFactory` 依赖 providers、tools、safety 等所有模块，是 core 的消费者而非组成部分。

### 操作
1. 将 `myagent/core/factory.py` 移动到 `myagent/factory.py`
2. 全局搜索 `from myagent.core.factory`，更新为 `from myagent.factory`
3. 主要涉及的文件：
   - `myagent/interfaces/cli/main.py`
   - `myagent/interfaces/web/ws_handler.py`
4. 运行测试

### 验证
```bash
grep -r "from myagent.core.factory" myagent/ --include="*.py"
# 应该无结果
```

---

## 步骤四：删除 cancellation.py，用 asyncio 原生取消替代（中等风险）

### 背景
`CancellationToken` 本质上在重新发明 `asyncio.Task.cancel()` 的能力，且存在以下问题：
- 需要 4 层参数接力（Session → Loop → Turn → StreamProcessor）
- 需要 4 处手动 `check()` 轮询，可能遗漏
- WebSocket handler 已同时使用 CancellationToken 和 `task.cancel()` 两套机制

用 `asyncio.Task.cancel()` 替代后：
- 不需要传递 token
- 每个 `await` 点自动成为取消检查点
- 零额外代码

### 操作

#### 4.1 修改 Session

```python
# session.py
class Session:
    def __init__(self, ...):
        # 删除: self._cancel_token
        self._running_task: asyncio.Task | None = None
        self._cancel_reason: str = ""
        self._cancel_detail: str = ""

    async def run(self, user_input: str) -> str:
        # 删除: self._cancel_token = CancellationToken()
        # 删除: self._loop._cancel_token = self._cancel_token
        self._cancel_reason = ""
        self._cancel_detail = ""
        
        ctx = HookContext(session_id=self.id)
        try:
            self._context.add_user_message(user_input)
            result = await self._loop.run(ctx)
            final_content = self._hooks.finalize_content(ctx, result.text)
            await self._persist(AgentState.IDLE, {"stop_reason": result.stop_reason or "completed"})
            return final_content or ""
        
        except asyncio.CancelledError:
            reason = self._cancel_reason or "user_cancelled"
            cancel_msg = f"[系统] 操作已取消 — {reason}: {self._cancel_detail}"
            self._context.add_assistant_message(content=cancel_msg, tool_calls=None)
            await self._persist(AgentState.IDLE, {"cancelled": True, "cancel_reason": reason})
            return cancel_msg
    
    def request_cancel(self, reason: str = "user_cancelled", detail: str = "") -> None:
        self._cancel_reason = reason
        self._cancel_detail = detail
        if self._running_task and not self._running_task.done():
            self._running_task.cancel()
```

#### 4.2 简化 AgentLoop

- 删除 `__init__` 中的 `cancel_token` 参数和 `self._cancel_token`
- 删除 `run()` 中的手动 `await self._cancel_token.check()`
- 删除 `_create_turn()` 中传给每个 Turn 的 `cancel_token=self._cancel_token`
- `except AgentCancelledError` 改为 `except asyncio.CancelledError`，逻辑保持不变

#### 4.3 简化 BaseTurn 及子类

- `BaseTurn.__init__` 删除 `cancel_token` 参数和 `self._cancel`
- `BaseTurn.execute()` 删除手动 `await self._cancel.check()`
- `except AgentCancelledError` 改为 `except asyncio.CancelledError`
- `ModelTurn.__init__` 删除 `cancel_token` 参数
- `ToolTurn.__init__` 删除 `cancel_token` 参数，删除 `_do_execute` 中的手动 check
- `HumanTurn.__init__` 删除 `cancel_token` 参数

#### 4.4 简化 StreamProcessor

- `run()` 删除 `cancel_token` 参数
- 删除流式循环中的 `if cancel_token and cancel_token.is_cancelled` 检查
- `asyncio.CancelledError` 会在 `async for event in self._router.stream()` 的 await 点自动抛出

#### 4.5 更新 Agent

- `request_cancel()` 简化签名，`reason` 和 `detail` 改为普通字符串
- 删除 `from myagent.core.cancellation import CancelReason`

#### 4.6 更新接口层

- `CLI main.py`：`except AgentCancelledError` 改为 `except asyncio.CancelledError`
- `WebSocket ws_handler.py`：
  - 删除 `from myagent.core.cancellation import AgentCancelledError, CancelReason`
  - `except AgentCancelledError` 改为 `except asyncio.CancelledError`
  - `_handle_cancel()` 中先设置 reason 再 cancel task：
    ```python
    session._cancel_reason = "user_cancelled"
    session._cancel_detail = "用户通过 WebSocket 取消"
    task.cancel()
    ```

#### 4.7 更新 __init__.py

- 删除 `CancellationToken, CancelReason, AgentCancelledError` 的导入和导出

#### 4.8 删除文件

- 删除 `myagent/core/cancellation.py`

### 验证
```bash
grep -r "CancellationToken\|AgentCancelledError\|CancelReason\|from myagent.core.cancellation" myagent/ --include="*.py"
# 应该无结果
python -m pytest
```

---

## 步骤五：重构状态机 — HumanTurn 移到 ModelTurn 后 + SafetyGuard 集成（高风险）

### 背景
当前：`MODEL → TOOL → HUMAN`
目标：`HUMAN → MODEL → HUMAN → TOOL → MODEL → HUMAN → ...`

HumanTurn 成为 loop 入口，每次 ModelTurn 输出后都经过 HumanTurn。

### 操作

#### 5.1 修改 HumanTurn — 扩展职责

HumanTurn 根据 `source`（上一个 Turn 的类型）决定行为：

```python
class HumanTurn(BaseTurn):
    """
    人机交互 Turn — 整个 Loop 的入口和中枢。
    
    根据 source 承担不同角色：
    - source=None（入口）：接收用户 query，合规检查，系统指令处理
    - source=MODEL + 有 tool_calls：安全围栏检查 + 用户审批
    - source=MODEL + 无 tool_calls：循环结束
    """
    kind = TurnKind.HUMAN
    _stage_name = "human_interaction"

    def __init__(
        self,
        context: ContextManager,
        hooks: HookManager,
        audit: AuditLogger | None,
        watchdog_timeout: float,
        approval_handler=None,
        safety_guard=None,          # ← 新增：从 ToolExecutor 移入
    ):
        super().__init__(hooks, audit, watchdog_timeout)
        self._context = context
        self._approval_handler = approval_handler
        self._safety_guard = safety_guard

    async def _do_execute(self, ctx, input_data=None, source=None) -> TurnResult:
        if source is None:
            # ── 入口：接收用户 query ──
            # 当前阶段：直接透传到 MODEL
            # 未来扩展：合规检查、系统指令处理
            return TurnResult(kind=TurnKind.HUMAN, next_turn=TurnKind.MODEL)
        
        elif source == TurnKind.MODEL:
            tool_calls = input_data  # ModelTurn 传递的 tool_calls（可能为 None）
            
            if not tool_calls:
                # 无工具调用，循环结束
                return TurnResult(kind=TurnKind.HUMAN, next_turn=None)
            
            # ── 安全围栏检查 + 用户审批 ──
            approved = []
            rejected = []
            pending_approval = []
            
            for tc in tool_calls:
                if self._safety_guard:
                    result = await self._safety_guard.check_tool_call(tc.name, tc.arguments)
                    if result.is_denied:
                        rejected.append((tc, f"安全策略拒绝: {result.reason}"))
                        continue
                    if result.requires_hitl:
                        pending_approval.append(tc)
                        continue
                    if result.decision.value == "rewrite" and result.rewritten_args:
                        tc = ToolCall(id=tc.id, name=tc.name, arguments=result.rewritten_args)
                approved.append(tc)
            
            # 需要用户审批的
            if pending_approval and self._approval_handler:
                decisions = await self._approval_handler(pending_approval)
                for tc, ok in zip(pending_approval, decisions):
                    if ok:
                        approved.append(tc)
                    else:
                        rejected.append((tc, f"工具 '{tc.name}' 被用户拒绝执行"))
            elif pending_approval:
                for tc in pending_approval:
                    rejected.append((tc, f"工具 '{tc.name}' 无审批处理器，自动拒绝"))
            
            # 被拒绝的写入 context
            for tc, reason in rejected:
                msg_result = MsgToolResult(
                    tool_call_id=tc.id, tool_name=tc.name, content=reason,
                )
                self._context.add_tool_result(tc.id, msg_result)
            
            if approved:
                return TurnResult(kind=TurnKind.HUMAN, next_turn=TurnKind.TOOL, data=approved)
            else:
                return TurnResult(kind=TurnKind.HUMAN, next_turn=TurnKind.MODEL)
```

#### 5.2 简化 ToolTurn — 去掉安全相关逻辑

```python
class ToolTurn(BaseTurn):
    """
    工具执行 Turn（纯执行，不再做安全分拣）。
    所有安全检查和审批已在 HumanTurn 完成。
    """
    async def _do_execute(self, ctx, input_data=None, source=None) -> TurnResult:
        tool_calls = input_data  # HumanTurn 传递的已批准 tool_calls
        
        # 直接执行（skip_safety=True，因为 HumanTurn 已检查过）
        tool_results = await self._executor.execute_batch(tool_calls, skip_safety=True)
        
        # 写入 context + 发射 hook 事件（保持现有逻辑）
        for tc, tr in zip(tool_calls, tool_results):
            # ... 写入 context, emit hook events（保持现有的 tool_end/tool_error 逻辑）
            pass
        
        # 执行完毕 → 回到 MODEL
        return TurnResult(kind=TurnKind.TOOL, next_turn=TurnKind.MODEL, data=tool_results)
```

#### 5.3 修改 ModelTurn — 输出后路由到 HumanTurn

```python
# ModelTurn._do_execute() 末尾修改
if has_tools:
    # 原来：next_turn=TurnKind.TOOL
    # 现在：next_turn=TurnKind.HUMAN（让 HumanTurn 做安全检查）
    return TurnResult(
        kind=TurnKind.MODEL,
        next_turn=TurnKind.HUMAN,
        data=result.tool_calls,
        stream_result=result,
    )
else:
    # 原来：next_turn=None（直接结束）
    # 现在：next_turn=TurnKind.HUMAN（让 HumanTurn 决定是否结束）
    return TurnResult(
        kind=TurnKind.MODEL,
        next_turn=TurnKind.HUMAN,
        stream_result=result,
    )
```

#### 5.4 修改 AgentLoop — 起始 Turn 改为 HUMAN

```python
class AgentLoop:
    async def run(self, ctx: HookContext) -> StreamResult:
        current_kind = TurnKind.HUMAN  # 原来是 TurnKind.MODEL
        # ... 其余 dispatcher 逻辑不变
```

#### 5.5 简化 ToolExecutor — 去掉 SafetyGuard

```python
class ToolExecutor:
    def __init__(self, registry, idempotency_cache=None, default_timeout=30.0,
                 secret_manager=None):
        # 删除: safety_guard 参数
        # 删除: self._safety_guard
        ...
    
    async def execute(self, tool_call, skip_safety=False) -> ToolResult:
        # 删除: 整个安全检查分支（if not skip_safety and self._safety_guard）
        # 删除: needs_approval 相关逻辑
        # 保留: 幂等缓存 + 凭据注入 + 执行 + 超时
        ...
```

注意：`safety_guard` 实例改为传给 `AgentLoop`（或直接传给 `HumanTurn`），而非 `ToolExecutor`。
同时 `skip_safety` 参数也可以删除，因为安全检查已不在 ToolExecutor 中。

#### 5.6 更新 Agent 和 Session 的构建链

- `Agent.__init__` 中 `safety_guard` 不再传给 `ToolExecutor`，改为存储后传给 `Session`
- `Session.__init__` 将 `safety_guard` 传给 `AgentLoop`
- `AgentLoop._create_turn()` 创建 `HumanTurn` 时传入 `safety_guard`

#### 5.7 更新 Loop 注释中的状态机文档

```python
"""
状态机：
  HUMAN → MODEL → HUMAN（无工具调用，结束）
  HUMAN → MODEL → HUMAN → TOOL → MODEL → HUMAN（有工具调用）
  HUMAN → MODEL → HUMAN → MODEL（工具全部被拒绝，LLM 重新生成）
"""
```

### 验证
```bash
python -m pytest
# 手动测试：
# 1. 正常对话（无工具）：HUMAN → MODEL → HUMAN → 结束
# 2. 工具调用（安全）：HUMAN → MODEL → HUMAN → TOOL → MODEL → HUMAN → 结束
# 3. 工具调用（需审批）：HUMAN → MODEL → HUMAN(审批) → TOOL → MODEL → HUMAN → 结束
# 4. 工具调用（被拒绝）：HUMAN → MODEL → HUMAN(拒绝) → MODEL → HUMAN → 结束
# 5. 取消操作：任意阶段 asyncio.CancelledError 向上传播
```

---

## 依赖关系与执行顺序

```
步骤 1-3（相互独立，可并行）
  ↓
步骤 4（依赖 1-3 完成）
  ↓
步骤 5（依赖步骤 4）
```

- 步骤 1-3 相互独立，可并行执行，均为低风险文件移动/删除
- 步骤 4 依赖 1-3 完成（避免同时改动太多文件）
- 步骤 5 依赖步骤 4（取消机制变更后再改状态机）
- 每步完成后运行测试，确认无回归
