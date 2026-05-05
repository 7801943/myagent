"""
ToolExecutor：工具执行引擎。
流水线：SafetyGuard -> IdempotencyCache -> SecretManager -> Execute -> Cache。
HITL 不再阻塞 executor，改为返回 needs_approval 标记由上层 HumanTurn 处理。
"""
import asyncio
import time
from typing import Any

from myagent.tools.base import BaseTool, ToolResult
from myagent.tools.registry import ToolRegistry
from myagent.tools.idempotency import IdempotencyCache
from myagent.context.message import ToolCall
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class ToolExecutor:
    """
    工具执行引擎。
    SafetyGuard -> IdempotencyCache -> SecretManager -> Execute -> Cache。
    requires_hitl 时返回 needs_approval 标记，不阻塞等待审批。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        idempotency_cache: IdempotencyCache | None = None,
        default_timeout: float = 30.0,
        safety_guard: Any | None = None,          # SafetyGuard 实例
        secret_manager: Any | None = None,         # SecretManager 实例
    ):
        self._registry = registry
        self._cache = idempotency_cache
        self._default_timeout = default_timeout
        self._safety_guard = safety_guard
        self._secret_manager = secret_manager

    async def execute(self, tool_call: ToolCall, skip_safety: bool = False) -> ToolResult:
        """
        执行单个工具调用。
        流程：Safety -> Idempotency -> Secret -> Execute -> Cache
        skip_safety: 跳过安全检查（用于已通过 HumanTurn 审批的调用）
        """
        tool = self._registry.get(tool_call.name)
        if tool is None:
            return ToolResult(
                content=f"Error: Tool '{tool_call.name}' not found. "
                        f"Available: {[t.name for t in self._registry.list_tools()]}",
                is_error=True,
                metadata={"tool_call_id": tool_call.id},
            )

        # -- 安全检查（必须在幂等缓存之前，已审批的调用可跳过） --
        if not skip_safety and self._safety_guard:
            guard_result = await self._safety_guard.check_tool_call(
                tool_call.name, tool_call.arguments
            )
            if guard_result.is_denied:
                logger.warning(f"Tool DENIED: {tool_call.name} - {guard_result.reason}")
                return ToolResult(
                    content=f"安全策略拒绝执行工具 '{tool_call.name}': {guard_result.reason}",
                    is_error=True,
                    metadata={"denied_by": guard_result.rule_name, "tool_call_id": tool_call.id},
                )
            if guard_result.requires_hitl:
                # 需要人工审批：返回 needs_approval 标记，不阻塞等待
                # 上层 ToolTurn 会分拣并路由到 HumanTurn
                logger.info(f"Tool requires approval: {tool_call.name} - {guard_result.reason}")
                return ToolResult(
                    content=f"工具 '{tool_call.name}' 需要人工审批: {guard_result.reason}",
                    is_error=False,
                    metadata={
                        "needs_approval": True,
                        "reason": guard_result.reason,
                        "rule_name": guard_result.rule_name,
                        "tool_call_id": tool_call.id,
                    },
                )
            if guard_result.decision.value == "rewrite" and guard_result.rewritten_args:
                # 参数重写
                tool_call = ToolCall(
                    id=tool_call.id,
                    name=tool_call.name,
                    arguments=guard_result.rewritten_args,
                )

        # -- 幂等缓存检查 --
        if self._cache:
            cached = await self._cache.get(tool_call.id)
            if cached is not None:
                logger.info(f"IdempotencyCache hit for {tool_call.name} (call_id={tool_call.id})")
                return cached

        # -- Phase 2: 凭据注入 --
        args = dict(tool_call.arguments)
        if self._secret_manager:
            args = self._secret_manager.inject_secrets(tool_call.name, args)

        # -- 执行工具 --
        start_time = time.monotonic()
        try:
            result = await asyncio.wait_for(
                tool.execute(**args),
                timeout=self._default_timeout,
            )
        except asyncio.TimeoutError:
            result = ToolResult(
                content=f"Tool '{tool_call.name}' timed out after {self._default_timeout}s",
                is_error=True,
            )
        except Exception as e:
            logger.error(f"Tool '{tool_call.name}' exception: {e}", exc_info=True)
            result = ToolResult(
                content=f"Error: {type(e).__name__}: {e}",
                is_error=True,
            )

        latency_ms = int((time.monotonic() - start_time) * 1000)
        result.metadata["latency_ms"] = latency_ms
        result.metadata["tool_call_id"] = tool_call.id

        # -- 幂等缓存存储 --
        if self._cache:
            await self._cache.store(tool_call.id, result)

        return result

    async def execute_batch(self, tool_calls: list[ToolCall], skip_safety: bool = False) -> list[ToolResult]:
        """并行执行多个工具调用。skip_safety 用于已通过 HumanTurn 审批的调用。"""
        tasks = [self.execute(tc, skip_safety=skip_safety) for tc in tool_calls]
        return await asyncio.gather(*tasks)

    def get_tool_schemas(self) -> list | None:
        """返回工具的 JSON schema 列表（供 LLM API 调用）。无工具时返回 None。"""
        tools = self._registry.list_tools()
        return tools if tools else None
