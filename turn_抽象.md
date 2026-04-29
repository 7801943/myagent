# Turn 抽象层设计分析

## 当前 `loop.py` 的核心问题

你的 `AgentLoop.run()` 是一个 **321 行的单体方法**，它把三个本质不同的"阶段"硬编码在一个 `for` 循环里：

```
for iteration in range(max_iterations):
    ① LLM 流式调用 → StreamProcessor 聚合 → StreamParser 分发
    ② 工具批量执行 → 结果写入上下文
    ③ （隐式的）"人类等待输入" — 由 Agent.run() 外层处理
```

每个阶段都混杂了：**业务逻辑** + **Hook 分发** + **审计内联** + **看门狗** + **取消检查**。同一个方法里有 **15 次** `self._hook.emit()` 调用和 **9 次** `self._audit` 检查。这就是你想引入 Turn 的直觉来源——**这些阶段有明显不同的生命周期语义，不应该全部扁平化在一个循环体里。**

## Turn 思路的好处

### 1. 状态机显式化

当前的状态流转（`thinking → running → waiting_tool → finished`）是通过散落的 `emit("state_change")` 实现的，**没有任何编译时保障**。Turn 可以把它变成一个显式的状态机：

```
HumanTurn → ModelTurn → ToolTurn → ModelTurn → ... → ModelTurn(stop)
                                                         ↓
                                                      HumanTurn
```

每个 Turn 的状态转换是自描述的，不需要在循环里到处 emit。

### 2. 异步/流式的统一接口

你说的"每个 turn 或流式、或一次性"非常对：

| Turn | 输入 | 输出 | 接口模式 |
|------|------|------|----------|
| `HumanTurn` | 外部输入（用户消息） | 写入 context 的消息 | **一次性**（等待外部注入） |
| `ModelTurn` | context 里的消息历史 | StreamResult（文本 + tool_calls） | **流式** AsyncIterator |
| `ToolTurn` | tool_calls 列表 | tool_results 列表 | **并发一次性**（内部并行，对外一次返回） |

### 3. 可测试性飞跃

当前要测试"工具执行后模型重新生成"这个场景，你必须 mock 整个 `AgentLoop.run()`。有了 Turn，你可以：

```python
# 直接测试单个 Turn
turn = ModelTurn(provider, context, hooks)
result = await turn.execute(ctx)
assert result.next_turn == TurnKind.TOOL
```

### 4. 横切关注点集中化

看门狗、取消检查、审计日志目前在每个阶段都手写一遍。Turn 基类可以通过模板方法 / 装饰器统一处理：

```python
class BaseTurn:
    async def execute(self, ctx):
        await self._check_cancel()      # 统一取消检查
        watchdog = self._start_watchdog() # 统一看门狗
        try:
            result = await self._do_execute(ctx)  # 子类实现
            await self._audit(ctx, result)         # 统一审计
            return result
        finally:
            watchdog.cancel()
```

## 设计上的关键张力

### ⚠️ 张力 1：Turn 是"值对象"还是"有状态执行器"？

两种路线：

**路线 A — Turn 是一次性执行器（推荐）：**
```python
turn = ModelTurn(deps)
result: TurnResult = await turn.execute(ctx)
# turn 用完就丢，不持有跨 turn 的状态
```

**路线 B — Turn 是可恢复的状态对象：**
```python
turn = ModelTurn(deps)
async for chunk in turn.stream(ctx):
    yield chunk  # 可中途暂停/恢复
result = turn.result
```

路线 A 更简单，但路线 B 如果你要做 HITL（Human-in-the-Loop）中断-恢复，可能更合适。

> **建议**：先用路线 A，但让 TurnResult 携带足够信息以便将来做恢复。

### ⚠️ 张力 2：循环编排权在哪？

Turn 抽象化后，**谁来决定下一个 Turn 是什么？**

**方案 1 — Turn 自己声明下一步（状态机模式）：**
```python
class TurnResult:
    next: TurnKind | None  # TOOL / MODEL / HUMAN / None(结束)
    data: Any              # 传递给下一个 Turn 的数据
```

AgentLoop 退化为一个简单的 dispatcher：
```python
while turn_result.next is not None:
    turn = self._create_turn(turn_result.next)
    turn_result = await turn.execute(ctx, turn_result.data)
```

**方案 2 — 循环硬编码在 AgentLoop（当前模式）：**
Turn 只是被调用的"步骤"，编排逻辑还在 loop 里。

> **建议**：方案 1 更优雅，而且让你未来可以轻松加入新 Turn 类型（比如 `ValidationTurn`、`PlanningTurn`）。

### ⚠️ 张力 3：流式输出如何暴露？

`ModelTurn` 是流式的，你的 Hook 系统需要在流式过程中逐 chunk 分发。两个选择：

**选择 A — Turn 内部消费流，通过 Hook 旁路分发：**
```python
class ModelTurn(BaseTurn):
    async def _do_execute(self, ctx):
        async for event in self._provider.stream(...):
            self._processor.process(event)
            await self._parser.dispatch(event, ctx)  # Hook 旁路
        return TurnResult(...)
```
（这就是你现在的模式，只是包了一层。）

**选择 B — Turn 本身就是 AsyncIterator：**
```python
class ModelTurn(BaseTurn):
    async def stream(self, ctx) -> AsyncIterator[StreamEvent]:
        async for event in self._provider.stream(...):
            yield event  # 调用方自己决定怎么处理
        self._result = ...
```

> **建议**：选择 A 更务实。选择 B 听起来纯粹但会让 Loop 重新承担分发职责，得不偿失。

## Strawman API 草案

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

class TurnKind(Enum):
    HUMAN = auto()
    MODEL = auto()
    TOOL  = auto()

@dataclass 
class TurnResult:
    """Turn 的统一输出。"""
    kind: TurnKind                    # 本次是什么 Turn
    next_turn: TurnKind | None        # 下一步是什么（None = 结束）
    data: Any = None                  # 传递给下一个 Turn 的数据
    stream_result: StreamResult | None = None  # ModelTurn 专用


class BaseTurn(ABC):
    """Turn 基类，封装横切关注点。"""
    
    def __init__(self, hooks: HookManager, cancel_token, audit_logger, watchdog_timeout: float):
        self._hooks = hooks
        self._cancel = cancel_token
        self._audit = audit_logger
        self._timeout = watchdog_timeout

    async def execute(self, ctx: HookContext, input_data: Any = None) -> TurnResult:
        """模板方法：取消检查 → 看门狗 → 子类逻辑 → 审计。"""
        if self._cancel:
            await self._cancel.check()
        
        watchdog = asyncio.create_task(self._watchdog(ctx))
        try:
            await self._hooks.emit(f"{self.kind.name.lower()}_turn_start", ctx)
            result = await self._do_execute(ctx, input_data)
            await self._hooks.emit(f"{self.kind.name.lower()}_turn_end", ctx)
            
            if self._audit:
                await self._audit.log_event(f"{self.kind.name.lower()}_turn_end", 
                                            ctx.snapshot(), session_id=ctx.session_id)
            return result
        finally:
            watchdog.cancel()

    @property
    @abstractmethod
    def kind(self) -> TurnKind: ...

    @abstractmethod
    async def _do_execute(self, ctx: HookContext, input_data: Any) -> TurnResult: ...


class ModelTurn(BaseTurn):
    """LLM 流式生成。"""
    kind = TurnKind.MODEL
    
    def __init__(self, provider, context, hooks, cancel_token, audit, timeout):
        super().__init__(hooks, cancel_token, audit, timeout)
        self._provider = provider
        self._context = context

    async def _do_execute(self, ctx, input_data) -> TurnResult:
        processor = StreamProcessor()
        parser = StreamParser(self._hooks)
        
        messages = self._context.get_messages()
        tools = ...  # 从 executor 获取
        
        async for event in self._provider.stream(messages, tools):
            if self._cancel and self._cancel.is_cancelled:
                raise AgentCancelledError(...)
            processor.process(event)
            await parser.dispatch(event, ctx)
        
        result = processor.result()
        
        # 写入 context
        self._context.add_assistant_message(
            content=result.text,
            tool_calls=result.tool_calls or None,
        )
        
        # 决定下一步
        if result.tool_calls:
            return TurnResult(
                kind=TurnKind.MODEL,
                next_turn=TurnKind.TOOL,
                data=result.tool_calls,
                stream_result=result,
            )
        else:
            return TurnResult(
                kind=TurnKind.MODEL,
                next_turn=None,  # 结束
                stream_result=result,
            )


class ToolTurn(BaseTurn):
    """工具批量执行。"""
    kind = TurnKind.TOOL
    
    async def _do_execute(self, ctx, tool_calls) -> TurnResult:
        results = await self._executor.execute_batch(tool_calls)
        # 写入 context...
        return TurnResult(
            kind=TurnKind.TOOL,
            next_turn=TurnKind.MODEL,  # 工具执行完总是回到模型
            data=results,
        )
```

然后 **AgentLoop 变成一个极简的 dispatcher**：

```python
class AgentLoop:
    async def run(self, ctx) -> StreamResult:
        turn_result = TurnResult(kind=TurnKind.HUMAN, next_turn=TurnKind.MODEL)
        
        for _ in range(self._max_iterations):
            turn = self._create_turn(turn_result.next_turn)
            turn_result = await turn.execute(ctx, turn_result.data)
            
            if turn_result.next_turn is None:
                return turn_result.stream_result
        
        return StreamResult(text="达到最大迭代次数", stop_reason="max_iterations")
```

从 321 行变成 ~10 行。

## 需要你决定的问题

1. **Turn 粒度**：`ModelTurn` 是否应该包含"写入 context"的职责？还是让 Loop 来做？（推荐 Turn 自包含）
2. **HumanTurn 是否需要**：当前架构中 `Agent.run()` 已经处理了人类输入，`HumanTurn` 可能只在 multi-turn 对话模式下才有意义。你是否计划让 Loop 管理完整的对话生命周期？
3. **Hook 事件命名是否变更**：引入 Turn 后，是 `turn_start` / `model_turn_start` 还是保持现有的 `provider_call_start` / `before_execute_tools`？（建议渐进式：新事件与旧事件并存一段时间）
4. **流式接口的选择**：你前面说"每个 turn 或流式、或一次性的接口"——你倾向于统一为 `AsyncIterator[TurnEvent]`（选择 B），还是保持 Hook 旁路分发（选择 A）？

## 总结

你的直觉是对的——**Turn 是 loop.py 正确的分解方向**。它解决了：
- ✅ 321 行单体方法难以维护
- ✅ 横切关注点（审计/取消/看门狗）重复 copy-paste
- ✅ 状态流转隐式、脆弱
- ✅ 难以独立测试单个阶段

关键是不要过度设计：先从 `ModelTurn` + `ToolTurn` 两个 Turn 开始，`HumanTurn` 可以后补。让 Turn 自包含（管自己的 context 写入），Loop 只做 dispatch。
