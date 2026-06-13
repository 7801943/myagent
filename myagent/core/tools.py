"""
工具接口层（ToolInterface）

职责：
1. Harness 与 ToolManager 之间的薄适配层
2. 封装工具执行链路：安全守卫 → 密钥注入 → ToolManager.execute
3. 提供批量并行执行
4. 提供 schema 查询
5. 内置安全责任链编排（原 SafetyGuard）
6. 工具执行全流程编排：批量执行 → 安全分拣 → 人工审批 → 批准后重执行

安全检查执行顺序：
  a. PolicyEngine 策略引擎（基于配置的动态规则）
  b. 注册的 BaseRule 规则链（按 priority 排序）
  第一个返回非 ALLOW 的结果会短路返回。

Harness 通过此接口与工具系统交互，不直接依赖 myagent/tools/manager.py。
"""
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from myagent.tools.manager import ToolManager
from myagent.tools.api import ToolResult
from myagent.safety.base import BaseRule, SafetyContext, GuardResult
from myagent.safety.policy import PolicyEngine
from myagent.safety.cli_fence import CLIFence
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


# ── 工具执行结果（含审批状态） ──

@dataclass
class ExecutedTool:
    """
    单个工具在执行管线中的完整结果。
    
    Attributes:
        tool_call: 原始工具调用
        result: 工具执行结果
        status: 执行状态 — "completed" | "approved" | "rejected"
    """
    tool_call: Any  # ToolCall
    result: ToolResult
    status: str = "completed"  # completed / approved / rejected


class ToolInterface:
    """
    Harness 与工具系统之间的适配层。
    封装工具执行的安全守卫、密钥注入、幂等缓存等细节。

    内置安全责任链：按优先级串行执行所有 BaseRule，遇到非 ALLOW 结果立即短路返回。

    用法：
        tools = ToolInterface(
            tool_manager,
            policy_engine=policy_engine,
            rules=[...],
            secret_manager=secret_manager,
        )
        result = await tools.execute("file_read", {"path": "/tmp/test.txt"}, tool_call_id="tc_xxx")
    """

    def __init__(
        self,
        tool_manager: ToolManager,
        policy_engine: PolicyEngine | None = None,
        rules: list[BaseRule] | None = None,
        secret_manager=None,
    ):
        self._tool_manager = tool_manager
        self._policy_engine = policy_engine
        self._rules: list[BaseRule] = sorted(rules or [], key=lambda r: r.priority)
        self._secret_manager = secret_manager
        self._cli_fence = next(
            (rule for rule in self._rules if isinstance(rule, CLIFence)),
            None,
        )

    @property
    def has_safety(self) -> bool:
        """是否启用安全策略。"""
        return self._policy_engine is not None or len(self._rules) > 0

    def add_rule(self, rule: BaseRule) -> None:
        """添加安全规则并重新排序。"""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)
        if isinstance(rule, CLIFence):
            self._cli_fence = rule
        logger.debug(f"Rule added: {rule.name} (priority={rule.priority})")

    def get_cli_policy_state(self) -> dict[str, Any]:
        if not self._cli_fence:
            return {
                "active_policy": "full_access",
                "available_policies": ["full_access"],
                "mode": "allow_all",
            }
        return self._cli_fence.state()

    def set_cli_policy(self, policy_name: str) -> dict[str, Any]:
        if not self._cli_fence:
            raise RuntimeError("CLI safety policy is not configured")
        self._cli_fence.set_policy(policy_name)
        return self._cli_fence.state()

    # ── 安全责任链 ──

    async def _run_safety_chain(self, context: SafetyContext) -> GuardResult:
        """
        执行安全责任链。
        1. 先走策略引擎
        2. 再走规则链（按 priority 排序）
        第一个返回非 ALLOW 的结果会短路返回。
        """
        # 1. 策略引擎
        if self._policy_engine:
            result = await self._policy_engine.decide(context)
            if not result.is_allowed:
                logger.info(
                    f"Safety: {result.decision.value} by policy_engine"
                    f" (tool={context.tool_name}): {result.reason}"
                )
                return result

        # 2. 规则链
        for rule in self._rules:
            result = await rule.check(context)
            if not result.is_allowed:
                logger.info(
                    f"Safety: {result.decision.value} by {rule.name}"
                    f" (tool={context.tool_name}): {result.reason}"
                )
                return result

        return GuardResult()  # 全部通过

    async def check_tool_call(self, tool_name: str, args: dict, session_id: str = "") -> GuardResult:
        """检查工具调用是否安全。"""
        context = SafetyContext(
            tool_name=tool_name,
            tool_args=args,
            session_id=session_id,
        )
        return await self._run_safety_chain(context)

    async def check_input(self, user_input: str, session_id: str = "") -> GuardResult:
        """检查用户输入安全性。"""
        context = SafetyContext(user_input=user_input, session_id=session_id)
        return await self._run_safety_chain(context)

    async def check_output(self, output_content: str, session_id: str = "") -> GuardResult:
        """检查模型输出安全性。"""
        context = SafetyContext(output_content=output_content, session_id=session_id)
        return await self._run_safety_chain(context)

    # ── 单工具执行 ──

    async def execute(
        self,
        name: str,
        args: dict,
        tool_call_id: str,
        skip_safety: bool = False,
    ) -> ToolResult:
        """
        执行单个工具。
        执行链路：安全检查 → 密钥注入 → ToolManager.execute（含幂等缓存）
        """
        # 1. 安全检查
        if not skip_safety and self.has_safety:
            guard_result = await self.check_tool_call(name, args)
            if guard_result.is_denied:
                logger.warning(f"Tool '{name}' denied by safety guard: {guard_result.reason}")
                return ToolResult(
                    content=f"安全策略拒绝执行工具 '{name}': {guard_result.reason}",
                    is_error=True,
                    metadata={"denied_by": guard_result.rule_name, "tool_call_id": tool_call_id},
                )
            if guard_result.requires_hitl:
                logger.info(f"Tool '{name}' requires HITL approval: {guard_result.reason}")
                return ToolResult(
                    content=f"工具 '{name}' 需要人工审批: {guard_result.reason}",
                    is_error=False,
                    metadata={
                        "needs_approval": True,
                        "reason": guard_result.reason,
                        "tool_call_id": tool_call_id,
                    },
                )
            if (
                guard_result.decision
                and hasattr(guard_result.decision, "value")
                and guard_result.decision.value == "rewrite"
                and guard_result.rewritten_args
            ):
                args = guard_result.rewritten_args
                logger.info(f"Tool '{name}' args rewritten by safety guard")

        # 2. 密钥注入
        if self._secret_manager:
            args = self._secret_manager.inject_secrets(name, args)

        # 3. 委托 ToolManager 执行（含幂等缓存）
        return await self._tool_manager.execute(name, tool_call_id=tool_call_id, **args)

    # ── 批量并行执行 ──

    async def execute_batch(
        self,
        tool_calls: list,
        skip_safety: bool = False,
    ) -> list[ToolResult]:
        """
        批量并行执行工具。
        tool_calls 为 list[ToolCall] 对象。
        """
        import asyncio
        tasks = [
            self.execute(
                tc.name if hasattr(tc, "name") else tc.get("name", ""),
                tc.arguments if hasattr(tc, "arguments") else tc.get("arguments", {}),
                tc.id if hasattr(tc, "id") else tc.get("id", ""),
                skip_safety=skip_safety,
            )
            for tc in tool_calls
        ]
        return await asyncio.gather(*tasks)

    # ── 工具执行全流程编排（含审批） ──

    async def execute_with_approval(
        self,
        tool_calls: list,
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
    ) -> list[ExecutedTool]:
        """
        工具执行全流程编排：批量执行 → 安全分拣 → 人工审批 → 批准后重执行。
        
        Harness 只需调用此方法即可获得所有工具的最终执行结果（含审批状态），
        然后负责写入 context 和发射事件。

        Args:
            tool_calls: 待执行的工具调用列表（list[ToolCall]）
            approval_handler: 人工审批回调，接收待审批的 tool_calls，返回每个的批准决定

        Returns:
            list[ExecutedTool]: 所有工具的执行结果（含状态标记）
        """
        # 1. 批量执行（含安全检查）
        tool_results = await self.execute_batch(tool_calls, skip_safety=False)

        # 2. 分拣：需要审批 vs 已完成
        pending = [
            (tc, tr) for tc, tr in zip(tool_calls, tool_results)
            if tr.metadata.get("needs_approval")
        ]
        completed = [
            (tc, tr) for tc, tr in zip(tool_calls, tool_results)
            if not tr.metadata.get("needs_approval")
        ]

        results: list[ExecutedTool] = [
            ExecutedTool(tool_call=tc, result=tr, status="completed")
            for tc, tr in completed
        ]

        # 3. 处理人工审批
        if pending:
            approved_calls, rejected_calls = await self._handle_approval(
                pending, approval_handler,
            )

            # 3a. 被拒绝的工具：构造拒绝结果
            for tc in rejected_calls:
                reject_result = ToolResult(
                    content=f"工具 '{tc.name}' 被用户拒绝执行",
                    is_error=True,
                    metadata={"tool_call_id": tc.id},
                )
                results.append(ExecutedTool(
                    tool_call=tc, result=reject_result, status="rejected",
                ))

            # 3b. 被批准的工具：重新执行（跳过安全检查）
            if approved_calls:
                approved_results = await self.execute_batch(approved_calls, skip_safety=True)
                for tc, tr in zip(approved_calls, approved_results):
                    results.append(ExecutedTool(
                        tool_call=tc, result=tr, status="approved",
                    ))

        return results

    async def _handle_approval(
        self,
        pending: list[tuple[Any, ToolResult]],
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None,
    ) -> tuple[list, list]:
        """
        人工审批流程：提取待审批调用 → 调用 handler → 区分批准/拒绝。
        
        Args:
            pending: [(ToolCall, ToolResult)] 需要审批的工具调用及其初步结果
            approval_handler: 审批回调

        Returns:
            (approved_tool_calls, rejected_tool_calls)
        """
        pending_calls = [tc for tc, _ in pending]

        logger.info(
            f"Approval needed for {len(pending_calls)} tools: "
            f"{[tc.name for tc in pending_calls]}"
        )

        if approval_handler:
            decisions = await approval_handler(pending_calls)
        else:
            decisions = [False] * len(pending_calls)

        approved = [tc for tc, ok in zip(pending_calls, decisions) if ok]
        rejected = [tc for tc, ok in zip(pending_calls, decisions) if not ok]
        return approved, rejected

    # ── Schema 查询 ──

    def list_schemas(self) -> list[dict]:
        """
        获取所有已注册工具的 schema 列表。
        返回与 Provider.format_tools() 兼容的 dict 格式（含热加载发现的工具）。
        """
        if not self._tool_manager:
            return []
        records = self._tool_manager.list_schemas()
        if not records:
            return []
        return [
            {
                "name": r.name,
                "description": r.description,
                "parameters_schema": r.parameters_schema,
                "source": r.source,
            }
            for r in records
        ]

    # ── 生命周期委托 ──

    async def start(self) -> None:
        """启动 ToolManager（创建 JsonRpcProxy + 热加载扫描）。"""
        if self._tool_manager:
            await self._tool_manager.start()

    async def stop(self) -> None:
        """停止 ToolManager（关闭 Proxy + 停止热加载扫描）。"""
        if self._tool_manager:
            await self._tool_manager.stop()
