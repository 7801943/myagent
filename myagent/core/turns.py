"""
Turn 抽象层：ReAct 循环的执行单元。

从 loop.py 提取，Phase 1 简化：
  - 删除 audit 参数，改用标准 logger
  - 看门狗简化为模块级常量
  - context.add_* 调用加 await（ContextManager 异步化）

Turn 抽象：
  BaseTurn   — 模板方法（看门狗 + 生命周期钩子 + 统一异常处理）
  SystemTurn — 系统指令检测（切换模型、新对话等）
  ModelTurn  — LLM 流式生成（包含 StreamProcessor + Hook 分发 + context 写入）
  ToolTurn   — 工具批量执行（包含安全分拣 + 人工审批 + 结果分发 + context 写入）

状态机路由（由 Agent.run() dispatcher 驱动）：
  SYSTEM → MODEL → TOOL → MODEL（系统指令 → LLM → 工具含审批 → LLM）
  MODEL → None（无工具调用，结束）
"""
import asyncio
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Awaitable

from myagent.providers.router import ProviderRouter
from myagent.context.manager import ContextManager
from myagent.context.message import ToolCall, ToolResult as MsgToolResult, ContentBlock
from myagent.core.hook import HookContext, HookManager
from myagent.core.permissions import check_permission
from myagent.providers.base import StreamEvent
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

# ── 看门狗默认超时常量（秒）──
_DEFAULT_TIMEOUT = 120.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 流式聚合结果
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class StreamResult:
    """一次 Provider 调用的聚合结果。"""
    text: str = ""
    reasoning_text: str = ""
    tool_calls: list[ToolCall] = None
    stop_reason: str | None = None
    usage: dict = None

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []
        if self.usage is None:
            self.usage = {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Turn 数据结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TurnKind(Enum):
    """Turn 类型枚举。"""
    MODEL = auto()
    TOOL = auto()
    SYSTEM = auto()
    HUMAN = auto()  # 已废弃，保留以兼容序列化


@dataclass
class TurnResult:
    """Turn 的统一输出。"""
    kind: TurnKind
    next_turn: TurnKind | None = None           # 下一个要执行的 Turn 类型，None 表示循环结束
    data: Any = None                            # 传递给下一个 Turn 的数据（如 tool_calls 列表）
    stream_result: StreamResult | None = None   # ModelTurn 专用的流式聚合结果
    meta: dict = field(default_factory=dict)    # 执行元数据（elapsed_seconds, usage 等）


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Turn 基类与子类
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BaseTurn(ABC):
    """
    Turn 基类，封装横切关注点：
    - 生命周期钩子（turn_start / turn_end / turn_error）
    - 统一异常处理（state_change→error, error hook, logger）
    - 看门狗超时（Turn 执行期间）
    - 清理（finally 中取消看门狗）
    - 取消由 asyncio.CancelledError 自动传播，无需手动检查

    子类只需实现 _do_execute() 和 kind / _stage_name 属性。
    """

    def __init__(
        self,
        hooks: HookManager,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._hooks = hooks
        self._timeout = timeout

    async def execute(self, ctx: HookContext, input_data: Any = None, source: TurnKind | None = None) -> TurnResult:
        """
        模板方法：turn_start → 子类逻辑 → turn_end → 清理。
        source: 上一个 Turn 的类型（保留接口兼容性，当前不再使用）。
        异常路径：统一 emit state_change(error), error, turn_error → raise。
        取消路径：CancelledError 直接向上传播（由 Agent 统一处理）。
        """
        start = time.monotonic()                          # 记录开始时间，用于计算耗时
        watchdog = asyncio.create_task(self._watchdog(ctx))  # 启动看门狗协程，超时后发出警告
        try:
            # 发射 Turn 开始事件（如 model_turn_start / tool_turn_start）
            await self._hooks.emit(f"{self.kind.name.lower()}_turn_start", ctx)

            # 调用子类实现的具体逻辑
            result = await self._do_execute(ctx, input_data, source)
            # 记录本 Turn 的执行耗时到 meta
            result.meta["elapsed_seconds"] = round(time.monotonic() - start, 3)

            # 发射 Turn 结束事件
            await self._hooks.emit(f"{self.kind.name.lower()}_turn_end", ctx, result=result)
            return result

        except asyncio.CancelledError:
            # 取消操作：直接向上传播，不做任何额外处理
            raise

        except Exception as e:
            # 异常处理：发射错误事件 + 记录日志，然后向上抛出
            await self._hooks.emit("state_change", ctx, state="error")
            await self._hooks.emit("error", ctx, error=e)
            await self._hooks.emit(f"{self.kind.name.lower()}_turn_error", ctx, error=e)
            logger.error(f"{self._stage_name} error: {e}", exc_info=True)
            raise

        finally:
            # 清理看门狗协程，防止资源泄漏
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass

    async def _watchdog(self, ctx: HookContext):
        """看门狗协程：超时后发出 timeout_warning 事件。"""
        await asyncio.sleep(self._timeout)   # 等待超时时间
        # 超时后发射警告事件，通知前端用户可以选择继续等待或取消
        await self._hooks.emit("timeout_warning", ctx,
            stage=self._stage_name,
            timeout_seconds=self._timeout,
            message=f"{self._stage_name} 已超过 {self._timeout}s，您可以选择继续等待或取消操作",
        )
        logger.warning(f"Timeout warning: {self._stage_name} exceeded {self._timeout}s")

    @property
    @abstractmethod
    def kind(self) -> TurnKind: ...

    @property
    @abstractmethod
    def _stage_name(self) -> str: ...

    @abstractmethod
    async def _do_execute(self, ctx: HookContext, input_data: Any, source: TurnKind | None) -> TurnResult: ...


class SystemTurn(BaseTurn):
    """
    系统指令检测 Turn。
    职责：
    1. 检查用户最新消息中的系统控制指令（/model, /new, /clear 等）
    2. 通过 system_command_handler 回调处理指令
    3. 无指令时透传至 MODEL
    4. 未来预留检查query的合法性
    5. 若有多模态输入或文件上传，启动相应检查处理后，透传至 MODEL
    """
    kind = TurnKind.SYSTEM
    _stage_name = "system_check"

    _COMMAND_RE = re.compile(r"^\s*/(\S+)(?:\s+(.*))?")   # 匹配斜杠命令：/command [args]

    def __init__(
        self,
        context: ContextManager,
        hooks: HookManager,
        timeout: float = 30.0,
        system_command_handler: Callable[[str, str, HookContext], Awaitable[None]] | None = None,
    ):
        super().__init__(hooks, timeout)
        self._context = context
        self._handler = system_command_handler

    async def _do_execute(self, ctx: HookContext, input_data: Any = None, source: TurnKind | None = None) -> TurnResult:
        await self._hooks.emit("state_change", ctx, state="system_check")

        messages = self._context.get_messages()
        user_msgs = [m for m in messages if getattr(m, "role", None) == "user"]  # 筛选用户消息
        if user_msgs:
            last_msg = user_msgs[-1]                            # 取最后一条用户消息
            content = (getattr(last_msg, "content", None) or "").strip()
            m = self._COMMAND_RE.match(content)                 # 尝试匹配 /command 格式
            if m:
                cmd = m.group(1)                                # 命令名（如 model / new / clear）
                args = m.group(2) or ""                         # 命令参数
                await self._hooks.emit("system_command", ctx, command=cmd, args=args)  # 发射钩子事件
                if self._handler:
                    await self._handler(cmd, args, ctx)         # 调用实际处理器（如切换模型）
                logger.info(f"System command: /{cmd} {args}")

        # 无论是否处理了系统指令，都进入 MODEL 阶段
        return TurnResult(kind=TurnKind.SYSTEM, next_turn=TurnKind.MODEL)


class ModelTurn(BaseTurn):
    """
    LLM 流式生成 Turn。
    职责：
    1. 流式调用 Provider → 事件聚合 → Hook 分发（原 StreamProcessor 已溶解到此类）
    2. 写入 assistant 消息到 context（await）
    3. 决定下一步：有 tool_calls → TOOL，无 → None（结束）
    """
    kind = TurnKind.MODEL
    _stage_name = "llm_generation"

    def __init__(
        self,
        provider_router: ProviderRouter,
        context: ContextManager,
        tool_schemas: list | None,
        hooks: HookManager,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        super().__init__(hooks, timeout)
        self._router = provider_router             # 多模型路由器（决定用哪个 Provider）
        self._context = context                     # 消息上下文管理器
        self._tool_schemas = tool_schemas           # 工具 JSON Schema 列表（传给 LLM 的 function definitions）
        # ── 流式聚合状态 ──
        self._text_parts: list[str] = []            # 文本增量碎片缓冲区
        self._reasoning_parts: list[str] = []       # 思维链增量碎片缓冲区
        self._tool_calls: list[ToolCall] = []       # 已完成的工具调用列表
        self._tool_call_buffers: dict[str, dict] = {}   # 正在构建的工具调用（id → {name, args_json}）
        self._stop_reason: str | None = None        # 停止原因（end_turn / tool_use / max_tokens）
        self._usage: dict = {}                      # Token 使用量（input_tokens / output_tokens）

    async def _do_execute(self, ctx: HookContext, input_data: Any = None, source: TurnKind | None = None) -> TurnResult:
        # 重置流式状态，防止上次调用的残留数据
        self._reset_stream()

        # 通知前端：Agent 正在思考
        await self._hooks.emit("state_change", ctx, state="thinking")

        logger.debug(f"Provider call start: session={ctx.session_id}")

        # 获取当前对话历史和工具定义
        messages = self._context.get_messages()
        tools = self._tool_schemas

        # 如果有流式订阅者（如 WebSocket/CLI），发送 stream_start 事件
        if self._hooks.wants_streaming():
            await self._hooks.emit("stream_start", ctx)

        # 迭代 Provider 的流式事件，逐个处理
        content_started = False                     # 标记是否已收到第一个文本内容
        async for event in self._router.stream(messages, tools):
            # 累积事件到内部缓冲区
            self._accumulate(event)

            # 首次收到文本时，切换状态为 generating（通知前端开始渲染）
            if not content_started and event.type == "text_delta" and event.text:
                content_started = True
                await self._hooks.emit("state_change", ctx, state="generating")

            # 分发事件给 Hook 订阅者（如 CLI 的流式打印、WebSocket 的实时推送）
            await self._dispatch_event(event, ctx)

        # 从缓冲区构建最终的聚合结果
        result = self._build_result()

        # 流式结束事件（resuming=True 表示后面还有工具调用，前端应保持等待）
        if self._hooks.wants_streaming():
            await self._hooks.emit("stream_end", ctx, resuming=bool(result.tool_calls))

        logger.debug(f"Provider call end: stop_reason={result.stop_reason}, usage={result.usage}")

        # 更新 Token 使用量到 context（用于上下文窗口预算控制）
        if result.usage:
            self._context.update_usage(result.usage)

        # 将 assistant 消息写入 context（包含文本和工具调用），并异步持久化
        # context.add_* 方法现在是 async
        await self._context.add_assistant_message(
            content=result.text,
            tool_calls=result.tool_calls if result.tool_calls else None,
        )

        # 根据是否有工具调用，决定下一步
        has_tools = bool(result.tool_calls)
        meta = {"usage": result.usage} if result.usage else {}

        if has_tools:
            # 有工具调用 → 进入 TOOL 阶段，携带 tool_calls 数据
            return TurnResult(
                kind=TurnKind.MODEL,
                next_turn=TurnKind.TOOL,
                data=result.tool_calls,
                stream_result=result,
                meta=meta,
            )
        else:
            # 无工具调用 → ReAct 循环结束
            return TurnResult(
                kind=TurnKind.MODEL,
                next_turn=None,
                stream_result=result,
                meta=meta,
            )

    # ── 流式聚合方法 ──

    def _accumulate(self, event: StreamEvent) -> None:
        """将 StreamEvent 累积到内部缓冲区。"""
        if event.type == "text_delta" and event.text:
            # 文本增量碎片，追加到缓冲区
            self._text_parts.append(event.text)

        elif event.type == "thinking_delta" and event.text:
            # 思维链增量碎片（如 Claude 的 extended thinking）
            self._reasoning_parts.append(event.text)

        elif event.type == "tool_call_start":
            # 工具调用开始：创建缓冲区，记录名称和空的参数 JSON
            self._tool_call_buffers[event.tool_call_id] = {
                "name": event.tool_name,
                "args_json": "",                     # 参数 JSON 字符串，后续通过 delta 增量拼接
            }

        elif event.type == "tool_call_delta":
            # 工具参数增量：拼接 JSON 字符串
            buf = self._tool_call_buffers.get(event.tool_call_id)
            if buf and event.tool_args_delta:
                buf["args_json"] += event.tool_args_delta

        elif event.type == "tool_call_end" and event.tool_args is not None:
            # 工具调用结束：参数已完整，创建 ToolCall 对象并加入列表
            self._tool_calls.append(ToolCall(
                id=event.tool_call_id,
                name=event.tool_name,
                arguments=event.tool_args,           # 已解析的完整参数 dict
            ))
            # 清理该工具的缓冲区
            self._tool_call_buffers.pop(event.tool_call_id, None)

        elif event.type == "message_end":
            # 消息结束：记录停止原因和 Token 使用量
            if event.stop_reason:
                self._stop_reason = event.stop_reason
            if event.usage:
                self._usage = event.usage

    async def _dispatch_event(self, event: StreamEvent, ctx: HookContext) -> None:
        """将关键事件通过 HookManager 广播给 UI 等订阅方。

        只分发三类用户直接关心的事件：
        - text_delta → stream 钩子（终端/前端实时打印）
        - thinking_delta → thinking_stream 钩子（思维链展示）
        - error → error 钩子（错误提示）
        """
        try:
            if event.type == "text_delta" and event.text:
                await self._hooks.emit("stream", ctx, delta=event.text)
            elif event.type == "thinking_delta" and event.text:
                await self._hooks.emit("thinking_stream", ctx, delta=event.text)
            elif event.type == "error" and event.error:
                await self._hooks.emit("error", ctx, error=event.error)
        except Exception as e:
            # Hook 分发失败不应中断 LLM 调用，仅记录警告
            logger.warning(f"Hook dispatch error: {e}")

    def _build_result(self) -> StreamResult:
        """从内部缓冲区构建最终的 StreamResult。"""
        return StreamResult(
            text="".join(self._text_parts),                 # 拼接所有文本碎片
            reasoning_text="".join(self._reasoning_parts),   # 拼接所有思维链碎片
            tool_calls=list(self._tool_calls),               # 复制工具调用列表
            stop_reason=self._stop_reason,
            usage=dict(self._usage),
        )

    def _reset_stream(self) -> None:
        """重置所有流式聚合状态（每次 _do_execute 自动调用）。"""
        self._text_parts.clear()
        self._reasoning_parts.clear()
        self._tool_calls.clear()
        self._tool_call_buffers.clear()
        self._stop_reason = None
        self._usage.clear()


class ToolTurn(BaseTurn):
    """
    工具批量执行 Turn（含安全分拣 + 人工审批内联）。
    职责：
    1. 并行执行 tool_calls（通过 tool_executor 回调）
    2. 分拣结果：已完成 vs 需审批（needs_approval）
    3. 内联人工审批：emit approval_needed → 等待 approval_handler → 执行/拒绝
    4. 写入已完成/被拒绝的 tool_results 到 context（await）
    5. 下一步始终为 MODEL
    """
    kind = TurnKind.TOOL
    _stage_name = "tool_execution"

    def __init__(
        self,
        context: ContextManager,
        tool_executor: Callable | None,
        hooks: HookManager,
        timeout: float = 60.0,
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
    ):
        super().__init__(hooks, timeout)
        self._context = context                     # 消息上下文
        self._tool_executor = tool_executor         # 工具执行器（通常是 Agent._execute_tool_batch）
        self._approval_handler = approval_handler   # 人工审批回调（None 时自动拒绝）

    async def _do_execute(self, ctx: HookContext, input_data: Any = None, source: TurnKind | None = None) -> TurnResult:
        tool_calls = input_data  # ModelTurn 通过 TurnResult.data 传递的 tool_calls 列表

        # Phase 2: 权限检查 — 逐个验证用户是否有权执行该工具
        for tc in tool_calls:
            perm = f"tool:{tc.name}"
            if not check_permission(ctx.user_permissions, perm):
                from myagent.tools.api import ToolResult as Tr
                logger.warning(f"Permission denied for tool '{tc.name}' (user permissions: {ctx.user_permissions})")
                # 替换为权限拒绝结果
                tc._permission_denied = True

        # 分离被权限拒绝的 tool_calls
        denied_calls = [tc for tc in tool_calls if getattr(tc, '_permission_denied', False)]
        allowed_calls = [tc for tc in tool_calls if not getattr(tc, '_permission_denied', False)]

        # 写入权限拒绝结果
        for tc in denied_calls:
            msg_result = MsgToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=f"权限不足：您没有执行工具 '{tc.name}' 的权限",
            )
            await self._context.add_tool_result(tc.id, msg_result)
            await self._hooks.emit("tool_error",
                ctx, tool_name=tc.name,
                error=Exception(f"Permission denied for tool '{tc.name}'"),
                call_id=tc.id,
            )

        # 只执行有权限的工具
        tool_calls = allowed_calls
        if not tool_calls:
            return TurnResult(kind=TurnKind.TOOL, next_turn=TurnKind.MODEL)

        # 通知前端：正在等待工具执行
        await self._hooks.emit("state_change", ctx, state="waiting_tool")

        # 为每个工具调用发射 tool_start 事件（前端可展示"正在执行 xxx"）
        for tc in tool_calls:
            await self._hooks.emit("tool_start",
                ctx, tool_name=tc.name, args=tc.arguments, call_id=tc.id
            )
            logger.debug(f"Tool start: {tc.name} (call_id={tc.id})")

        # 批量执行所有工具（内部会进行安全检查）
        if self._tool_executor:
            tool_results = await self._tool_executor(tool_calls, skip_safety=False)
        else:
            # 没有执行器时，返回错误结果
            logger.error(f"Tool executor is not available")
            from myagent.tools.api import ToolResult as Tr
            tool_results = [Tr(content=f"Tool '{tc.name}' not available", is_error=True) for tc in tool_calls]

        # 分拣：需要人工审批的 vs 直接完成的
        pending = [(tc, tr) for tc, tr in zip(tool_calls, tool_results)
                   if tr.metadata.get("needs_approval")]
        completed = [(tc, tr) for tc, tr in zip(tool_calls, tool_results)
                     if not tr.metadata.get("needs_approval")]

        # 写入已完成的工具结果到 context
        for tc, tr in completed:
            await self._write_result(ctx, tc, tr)

        # 处理需要人工审批的工具
        if pending:
            approved, rejected = await self._handle_approval(ctx, pending)

            # 处理被拒绝的工具：写入拒绝消息
            for tc in rejected:
                msg_result = MsgToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=f"工具 '{tc.name}' 被用户拒绝执行",
                )
                # context.add_tool_result 现在是 async
                await self._context.add_tool_result(tc.id, msg_result)
                await self._hooks.emit("tool_error",
                    ctx, tool_name=tc.name,
                    error=Exception(f"工具 '{tc.name}' 被用户拒绝执行"),
                    call_id=tc.id,
                )
                logger.info(f"Tool rejected: {tc.name} (call_id={tc.id})")

            # 处理被批准的工具：跳过安全检查再执行一次
            if approved:
                approved_results = await self._tool_executor(approved, skip_safety=True)
                for tc, tr in zip(approved, approved_results):
                    await self._write_result(ctx, tc, tr)

        # 工具执行完毕，返回 MODEL 让 LLM 继续推理
        return TurnResult(kind=TurnKind.TOOL, next_turn=TurnKind.MODEL)

    async def _handle_approval(self, ctx: HookContext, pending: list[tuple[ToolCall, Any]]) -> tuple[list[ToolCall], list[ToolCall]]:
        """内联人工审批：emit hook → 等待 handler → 区分批准/拒绝。"""
        pending_calls = [tc for tc, _ in pending]

        # 通知前端有工具需要审批（展示审批对话框）
        await self._hooks.emit("approval_needed", ctx, tool_calls=pending_calls)
        logger.info(f"Approval needed for {len(pending_calls)} tools: {[tc.name for tc in pending_calls]}")

        # 调用审批回调（CLI 为终端交互输入，WS 为 WebSocket 等待客户端回复）
        if self._approval_handler:
            decisions = await self._approval_handler(pending_calls)
        else:
            # 没有审批回调时，默认全部拒绝
            decisions = [False] * len(pending_calls)

        # 分拣：批准的 vs 拒绝的
        approved = [tc for tc, ok in zip(pending_calls, decisions) if ok]
        rejected = [tc for tc, ok in zip(pending_calls, decisions) if not ok]

        return approved, rejected

    async def _write_result(self, ctx: HookContext, tc: ToolCall, tr: Any) -> None:
        """将单个工具执行结果写入 context 并发射 hook 事件。"""
        latency = tr.metadata.get("latency_ms", 0)

        # 构建 content：如果有 content_blocks（如图片 base64），组装为多模态消息
        content_blocks = getattr(tr, "content_blocks", None)
        if content_blocks:
            # 多模态结果：文本 + 图片（如 vision 工具的输出）
            blocks: list[ContentBlock] = [ContentBlock(type="text", text=tr.content)]
            for cb in content_blocks:
                if cb.get("type") == "image_base64":
                    blocks.append(ContentBlock(
                        type="image_base64",
                        base64_data=cb["data"],
                        media_type=cb.get("media_type", "image/png"),
                    ))
            msg_result = MsgToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=blocks,
                metadata={"latency_ms": latency},
            )
        else:
            # 纯文本结果
            msg_result = MsgToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=tr.content,
                metadata={"latency_ms": latency},
            )
        # context.add_tool_result 现在是 async
        await self._context.add_tool_result(tc.id, msg_result)

        # 根据结果类型发射不同的 hook 事件
        if tr.is_error:
            # 被安全策略拦截的工具调用
            if "denied_by" in tr.metadata:
                await self._hooks.emit("safety_blocked",
                    ctx,
                    rule=tr.metadata["denied_by"],
                    reason=str(tr.content),
                    action="deny",
                    call_id=tc.id,
                    tool_name=tc.name,
                )
                logger.info(f"Safety blocked: {tc.name} by {tr.metadata['denied_by']}")
            # 工具执行错误
            await self._hooks.emit("tool_error",
                ctx, tool_name=tc.name,
                error=Exception(tr.content), call_id=tc.id,
            )
            logger.debug(f"Tool error: {tc.name} (call_id={tc.id}): {tr.content}")
        else:
            # 工具执行成功
            await self._hooks.emit("tool_end",
                ctx, tool_name=tc.name, result=tr,
                call_id=tc.id, latency_ms=latency,
            )
            logger.debug(f"Tool end: {tc.name} (call_id={tc.id}), latency={latency}ms")