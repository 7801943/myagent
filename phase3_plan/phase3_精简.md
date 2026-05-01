# Phase 3 方案对比与精简设计分析

## 1. 两个方案异同对比

### 共同点

| 维度 | 两方案一致之处 |
|------|---------------|
| 文件结构 | 均为 `subagent/` 下 4 文件：`base.py` `runner.py` `manager.py` `spawn_tool.py` |
| 数据模型 | Budget / SubAgentSpec / SubAgentResult 基本一致 |
| Manager 职责 | 深度限制 + 并发 Semaphore + 超时 `wait_for` + 白名单工具过滤 |
| SpawnTool | parameters_schema 完全一致（task/message/tools/model/max_iterations） |
| 防护机制 | 7 层保护：深度、并发、Budget token/tool/iteration、超时、spawn 自排除 |
| 审计 | 复用已有 `SUBAGENT_START` / `SUBAGENT_END` 枚举 |
| 集成方式 | 均修改 `agent.py` 注册 SpawnTool，共享 ProviderRouter |

### 差异点

| 维度 | 方案 A（phase3.md） | 方案 B（phase3实施计划.md） |
|------|---------------------|---------------------------|
| Runner 实现 | **内联 StreamProcessor + ToolExecutor** 调用，不实例化 ModelTurn/ToolTurn 类 | **复用完整 AgentLoop**，Runner 内部创建 AgentLoop.run() |
| 代码详细度 | 672 行，给出完整伪代码实现（`_run_model_turn`、`_run_tool_turn` 等） | 425 行，偏架构描述，实现细节留空（`...` 占位） |
| Budget 设计 | 用 `remaining_*` + `decrease()` + `clone_with_factor()`，有继承削减 | 用 `_used_*` + `consume_*()` + `is_exhausted()` |
| SubAgentResult | 有 `to_summary()` 方法 | 有 `token_usage: dict` 字段 |
| SpawnTool cancel_token | SpawnTool 自身持有 cancel_token，每次 run 时更新 | 通过 SubAgentManager 传递 |
| Hook 处理 | 创建独立 HookManager + progress 回调 | 创建独立 HookManager + 转发到父 HookManager |
| WebSocket 集成 | 未涉及 | 有 server.py 修改（subagent_start/end Hook） |

### 核心区别总结

> **方案 A**：Runner 自己写 ReAct 循环（`for iteration in range(...)`），内联调用 StreamProcessor + ToolExecutor，**不复用 Turn 抽象**。
>
> **方案 B**：Runner 内部创建完整 AgentLoop 实例，**完全复用 Turn + Loop dispatcher**。

---

## 2. 你的改进方案分析

你的核心思路：

> 1. **不复用主 Agent 的 Turn 和 Loop**
> 2. **精简上下文**：只有任务要求、工具 schema、交付标准和结果 schema
> 3. **无流式输出**
> 4. **简化异常处理**：只关注超时和工具失败

### 2.1 合理性分析

#### ✅ 完全合理的部分

**（1）不复用 Turn/Loop — 强烈赞同**

看你的代码库，当前 `ModelTurn` 和 `ToolTurn` 承载了很多 SubAgent **不需要的**横切关注点：

```python
# ModelTurn._do_execute() 里的这些 SubAgent 不需要：
await self._hooks.emit("state_change", ctx, state="thinking")     # UI 状态推送
await self._hooks.emit("stream_end", ctx, resuming=has_tools)      # 流式结束通知
# BaseTurn.execute() 里：
watchdog = asyncio.create_task(self._watchdog(ctx))                # 看门狗超时
```

`AgentLoop.run()` 本身也有 session_start/end、iteration_start/end 审计等 SubAgent 不需要的逻辑。复用它们会引入不必要的复杂度和耦合。

**方案 A 的 Runner（内联 StreamProcessor + ToolExecutor）本质上已经在做这件事**——只是它仍然引入了 HookManager 和 StreamProcessor 的流式分发逻辑。你可以进一步精简。

**（2）无流式输出 — 完全合理**

SubAgent 的输出是作为 `ToolResult.content` 返回给主 Agent 的。主 Agent 的前端 UI 只关心 `tool_start("spawn")` → `tool_end("spawn", result)` 这两个事件。SubAgent 内部的 text_delta 推给谁看？没人看。

这意味着你可以：
- **不需要 HookManager**（SubAgent 内部无流式消费者）
- **不需要 StreamProcessor**（它的核心价值是流式 Hook 分发）
- **直接调用 Provider 的非流式 API**，或者流式消费但只聚合不分发

**（3）精简上下文 — 合理但需注意边界**

你的思路是上下文只有：
- 任务要求（system prompt）
- 可用工具 schema
- 交付标准和结果 schema

这本质上就是一个**极简的 ReAct 循环**，context 结构为：

```
[system: 任务描述 + 交付标准 + 结果schema]
[user: initial_message]
[assistant: ...]        ← LLM 输出
[tool: tool_result]     ← 工具结果
[assistant: ...]        ← 继续推理
...
[assistant: final_answer]  ← 最终输出
```

这和当前 ContextManager 的功能一致，但你可以**不用 ContextManager 类**，直接用一个 `list[dict]` 管理就够了，因为 SubAgent 不需要：
- Session 持久化
- Token budget 三层裁剪
- 消息恢复 (`restore_from`)

#### ⚠️ 需要注意的风险

**（1）不用 StreamProcessor 意味着要自己处理 Provider 返回**

当前你的 Provider（OpenAI / Anthropic）**只有流式接口**（`async for event in provider.stream(...)`）。如果不用 StreamProcessor，你需要一个最简的聚合器来把流事件拼成完整的 text + tool_calls。

建议方案：写一个 30 行的 `_collect_response()` 工具函数，替代 180 行的 StreamProcessor：

```python
async def _collect_response(
    router: ProviderRouter,
    messages: list[dict],
    tools: list[dict] | None,
) -> tuple[str, list[ToolCall], dict]:
    """
    一站式收集 Provider 流式输出，返回 (text, tool_calls, usage)。
    无 Hook 分发、无状态追踪、无流式推送。
    """
    text_parts = []
    tool_call_buffers = {}
    tool_calls = []
    usage = {}

    async for event in router.stream(messages, tools):
        if event.type == "text_delta" and event.text:
            text_parts.append(event.text)
        elif event.type == "tool_call_start":
            tool_call_buffers[event.tool_call_id] = {"name": event.tool_name, "args": ""}
        elif event.type == "tool_call_delta" and event.tool_args_delta:
            buf = tool_call_buffers.get(event.tool_call_id)
            if buf:
                buf["args"] += event.tool_args_delta
        elif event.type == "tool_call_end" and event.tool_args is not None:
            tool_calls.append(ToolCall(
                id=event.tool_call_id, name=event.tool_name, arguments=event.tool_args
            ))
        elif event.type == "message_end":
            usage = event.usage or {}

    return "".join(text_parts), tool_calls, usage
```

**（2）工具执行仍然需要 Safety 检查**

即使精简，SubAgent 的工具执行**仍然应该走 ToolExecutor**（而非直接调用 `tool.execute()`），因为 ToolExecutor 里有：
- SafetyGuard 前置检查（SubAgent 执行 CLI 命令也需要安全围栏）
- IdempotencyCache（防重复执行）
- 超时控制

这是你代码库里已经很成熟的流水线，跳过它会引入安全漏洞。

**（3）异常处理不能只有超时和工具失败**

还需要覆盖：
- `BudgetExceededError`（预算耗尽）
- `AgentCancelledError`（父 Agent 取消传播）
- Provider 错误（AllProvidersFailedError）
- JSON 解析错误（LLM 返回了格式错误的 tool_call）

---

## 3. 推荐方案：精简版 SubAgentRunner

结合你的想法和代码库现状，推荐的 Runner 设计如下：

```python
class SubAgentRunner:
    """
    精简版子 Agent 执行器。
    
    设计理念：
    - 不复用 Turn / Loop / StreamProcessor / HookManager
    - 不做流式输出
    - 上下文用 list[dict] 直接管理（不用 ContextManager）
    - 工具执行仍走 ToolExecutor（保留安全检查）
    - 只关注：LLM 调用 → 工具执行 → 循环 → 最终结果
    """
    
    async def run(self) -> SubAgentResult:
        # 构建初始上下文
        messages = [
            {"role": "system", "content": self._build_system_prompt()},
            {"role": "user", "content": self._spec.initial_message},
        ]
        
        tools_used = []
        
        for iteration in range(self._budget.max_iterations):
            # 1. 取消检查
            if self._cancel_token and self._cancel_token.is_cancelled:
                return SubAgentResult(... success=False, error="cancelled")
            
            # 2. 调用 LLM（无流式分发，纯聚合）
            try:
                text, tool_calls, usage = await asyncio.wait_for(
                    _collect_response(self._router, messages, tools_schema),
                    timeout=60.0,  # 单次 LLM 超时
                )
            except asyncio.TimeoutError:
                return SubAgentResult(... success=False, error="llm_timeout")
            except Exception as e:
                return SubAgentResult(... success=False, error=str(e))
            
            # 3. Budget 扣除
            self._budget.consume_tokens(usage.get("output_tokens", 0))
            
            # 4. 写入 assistant 消息
            messages.append({"role": "assistant", "content": text, ...})
            
            # 5. 无工具调用 → 完成
            if not tool_calls:
                return SubAgentResult(content=text, ... success=True)
            
            # 6. 执行工具（复用 ToolExecutor，保留安全检查）
            results = await self._executor.execute_batch(tool_calls)
            for tc, tr in zip(tool_calls, results):
                tools_used.append(tc.name)
                messages.append({"role": "tool", "content": tr.content, ...})
                self._budget.consume_tool_call()
        
        return SubAgentResult(... success=False, error="max_iterations")
    
    def _build_system_prompt(self) -> str:
        """构建精简的 system prompt：任务 + 交付标准 + 结果 schema。"""
        return (
            f"{self._spec.task}\n\n"
            f"## 交付要求\n"
            f"完成任务后，请直接输出最终结果。不需要额外解释。\n"
            f"如果无法完成，请说明原因。"
        )
```

### 精简前后对比

| 维度 | 复用 Turn/Loop 方案 | 精简方案 |
|------|---------------------|---------|
| 依赖模块 | AgentLoop + ModelTurn + ToolTurn + StreamProcessor + HookManager + ContextManager | ProviderRouter + ToolExecutor + `_collect_response()` |
| Runner 代码量 | ~150 行（创建 Loop + 各种参数传递） | ~80 行（自包含循环） |
| 流式分发 | 有（但无人消费） | 无 |
| HookManager | 需要创建独立实例 | 不需要 |
| ContextManager | 需要创建独立实例 | 直接用 `list[dict]` |
| 上下文管理 | 完整（system/summary/recent） | 极简（system + 对话历史） |
| 看门狗 | 继承 BaseTurn 的 watchdog | 不需要（直接 wait_for） |
| 可测试性 | 依赖多，mock 复杂 | 依赖少，易于 mock |

---

## 4. 最终建议

> [!IMPORTANT]
> **赞同你的精简方向**，但有两个底线不要放弃：
> 1. **工具执行必须走 ToolExecutor**（安全检查 + 幂等缓存）
> 2. **取消传播必须保留** CancellationToken

### 推荐的最终文件结构

```
myagent/subagent/
├── __init__.py          # 导出
├── base.py              # Budget + SubAgentSpec + SubAgentResult（与两方案基本一致）
├── runner.py            # 精简版 Runner（~80行，自包含 ReAct 循环）
├── manager.py           # 与两方案基本一致（并发/深度/超时控制）
├── spawn_tool.py        # 与两方案基本一致
└── collect.py           # _collect_response() 工具函数（~30行，替代 StreamProcessor）
```

### 要不要把 `_collect_response` 单独放文件？

建议**放在 `runner.py` 内部**即可。它只有 ~30 行，专为 SubAgent 设计，没有复用价值。如果将来主 Agent 也需要非流式模式，再提取到 `core/collect.py`。

### 关于 SubAgentSpec 中的"结果 schema"

你提到"交付标准和结果 schema"。这是一个很好的想法，可以在 SubAgentSpec 中增加一个可选字段：

```python
@dataclass
class SubAgentSpec:
    task: str
    initial_message: str
    tools: list[str] | None = None
    model: str | None = None
    budget: Budget | None = None
    # 新增：结构化输出约束
    output_schema: dict | None = None      # JSON Schema，约束最终输出格式
    acceptance_criteria: str | None = None  # 交付标准描述
```

然后在 `_build_system_prompt()` 中自动注入这些约束。
