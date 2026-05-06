# Hook 体系设计讨论笔记

## 一、Hook 在 Agent 框架中的角色

Hook（钩子）是一种在程序执行流的关键节点"悬挂"逻辑的机制。它不属于主流程，但可以观察、响应、甚至干预主流程的行为。

这是**开闭原则（OCP）**的经典实现：
> 软件实体应该对扩展开放，对修改关闭。

在 myagent 里，`AgentLoop` 的核心职责是 ReAct 循环（发请求→解析工具→执行→再发请求），这个流程是稳定的。审计日志、前端推送、调试打印等"横切关注点"通过 Hook 机制被干净地注入进来，无需修改 AgentLoop 本身。

### 现有 Hook 层级关系

```
AgentHook（抽象基类，定义所有事件点，默认空实现）
├── AuditHook（observability/hook.py）  → 将事件写入审计数据库
├── WebSocketHook                        → 将事件推送到前端
└── CompositeHook（core/hook.py）       → 组合器，对外伪装成单个 Hook
```

---

## 二、CompositeHook 的存在意义

### 核心问题

当有多个独立的观察者（AuditHook + WebSocketHook + DebugHook）想同时监听同一个 Agent 时，如何让 AgentLoop 不需要知道有几个观察者？

**笨方法**：
```python
class AgentLoop:
    def __init__(self, audit_hook, websocket_hook, debug_hook):
        # 每次增加观察者，AgentLoop 就要改
        ...
```

**CompositeHook 方法（Composite 设计模式）**：
```
AgentLoop → CompositeHook（代理人）
                ├── AuditHook
                └── WebSocketHook
```

AgentLoop 只打交道代理人，代理人负责广播给所有人。这是**对扩展开放，对修改关闭**的具体实现。

### 现有实现的痛点

CompositeHook 有 20+ 个广播方法，每个结构完全相同：

```python
async def on_session_start(self, ctx):
    for h in self._hooks:
        await h.on_session_start(ctx)

async def on_turn_start(self, ctx):
    for h in self._hooks:
        await h.on_turn_start(ctx)
# ... 再手写 18 个
```

这是违反 **DRY 原则（Don't Repeat Yourself）**的样板代码。

---

## 三、消除重复的方案

### 方案一：`__getattr__` 动态代理

```python
class CompositeHook(AgentHook):
    def __getattr__(self, name: str):
        # 仅在"找不到属性"时触发（Python 查找失败的最后兜底）
        base_method = getattr(AgentHook, name, None)
        if base_method is None or not inspect.iscoroutinefunction(base_method):
            raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

        async def _broadcast(*args, **kwargs):
            for hook in self._hooks:
                await getattr(hook, name)(*args, **kwargs)

        return _broadcast  # 运行时按需生成，利用闭包捕获 name
```

**原理**：Python 访问属性时有固定查找顺序，找不到才调用 `__getattr__`。  
**优点**：极少代码。  
**缺点**：IDE 无自动补全，方法"不可见"。

### 方案二：`setattr` 类级注入（推荐）

```python
def _make_broadcast_method(method_name: str):
    """工厂函数：固定 method_name，避免循环变量陷阱（闭包问题）"""
    async def _broadcast(self, *args, **kwargs):
        for hook in self._hooks:
            await getattr(hook, method_name)(*args, **kwargs)
    _broadcast.__name__ = method_name
    return _broadcast

# 类定义后，自动把广播方法"贴"上去
_SKIP = {"wants_streaming", "finalize_content"}
for _name, _method in inspect.getmembers(AgentHook, predicate=inspect.isfunction):
    if _name.startswith("_") or _name in _SKIP:
        continue
    if inspect.iscoroutinefunction(_method):
        setattr(CompositeHook, _name, _make_broadcast_method(_name))
```

**原理**：在模块加载阶段就把方法"贴"到类上，行为与手写完全一样。  
**优点**：方法真实存在于类上，调试友好；与手写性能相同。  
**缺点**：需要理解 `setattr` 循环逻辑。

### 方案对比

| 维度 | `__getattr__` | `setattr` 注入 | 手写（现状） |
|---|---|---|---|
| 代码量 | 最少 | 少 | 最多 |
| IDE 支持 | ❌ | ✅ | ✅ |
| 调试难度 | 高 | 中 | 低 |
| 运行时开销 | 每次有查找开销 | 与手写相同 | 与手写相同 |

---

## 四、用户提出的替代设计：事件总线单例

### 设计思路

维护全局单例，为每个事件点维护回调列表。`AgentHook` 子类实例化时，自动将覆盖的方法注册进对应列表。

```python
class HookRegistry:  # 单例
    _on_session_start = []
    _on_turn_start = []
    # ...

    async def fire_session_start(self, ctx):
        for fn in self._on_session_start:
            await fn(ctx)

class AgentHook(HookRegistry):
    def __init__(self):
        registry = HookRegistry()
        if type(self).on_session_start is not AgentHook.on_session_start:
            registry._on_session_start.append(self.on_session_start)
```

### 与 CompositeHook 的本质对比

| | CompositeHook | 事件总线单例 |
|---|---|---|
| **模式名** | Composite 模式 | Event Emitter / Observer 模式 |
| **中心** | 对象中心（管理观察者） | 事件中心（管理回调列表） |
| **著名实现** | Python composites | Node.js EventEmitter, Qt Signal-Slot |

### 事件总线的核心优势

- 全局可注册，不需要持有 composite 引用
- 函数式风格，可直接注册任意 `async def`

### 事件总线的核心挑战

1. **多 Agent 实例冲突**：单例共享导致 Agent A 的事件触发 Agent B 的 Hook
2. **内存泄漏隐患**：单例持有绑定方法 → 间接持有实例 → 阻止 GC，需要显式注销
3. **隔离性差**：调试时难以确认"这个 Agent 被哪些 Hook 监听"

### 结论

两种设计都是成熟模式，各有适用场景：

- **CompositeHook**：适合多 Agent 实例、框架级设计，天然隔离
- **事件总线**：适合单 Agent 应用、插件系统，注册更灵活

---

## 五、共同的改进点：并发执行 Hook

无论哪种设计，当前都是**顺序 await**——慢的 Hook 会阻塞整个链：

```python
# 现状：顺序，AuditHook 慢了，WebSocketHook 就得等
for h in self._hooks:
    await h.on_session_start(ctx)
```

用 `asyncio.gather` 并发执行：

```python
# 改进：并发，互不阻塞
await asyncio.gather(*[h.on_session_start(ctx) for h in self._hooks])
```

**前提**：Hook 之间没有执行顺序依赖（大多数场景满足）。  
**这是两种设计都可以做的实质性性能改进。**

---

## 六、Python 关键概念速查

| 概念 | 一句话说明 |
|---|---|
| `__getattr__` | 属性查找失败时的最后兜底，仅在找不到时触发 |
| 闭包（closure） | 内层函数"记住"外层函数作用域里的变量 |
| `inspect.iscoroutinefunction` | 判断一个函数是否是 `async def` |
| `setattr(cls, name, value)` | 动态给类/对象贴上属性或方法 |
| `asyncio.gather(*coros)` | 并发执行多个协程，全部完成后返回 |
| 单例（Singleton） | 全局唯一实例，现代设计中慎用（测试困难、多实例冲突） |
| DRY 原则 | Don't Repeat Yourself，消除重复逻辑 |
| 开闭原则（OCP） | 对扩展开放，对修改关闭 |
| 组合优于继承 | Favor composition over inheritance（GoF 设计模式核心原则之一） |
