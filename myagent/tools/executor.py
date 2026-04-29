"""
ToolExecutor：工具执行引擎（Phase 2 增强版）。
新增：
1. SafetyGuard 前置安全检查
2. SecretManager 凭据注入
3. HITL 挂起支持（通过回调通知上层）
"""
import asyncio
import time
from typing import Any, Callable, Awaitable

from myagent.tools.base import BaseTool, ToolResult
from myagent.tools.registry import ToolRegistry
from myagent.tools.idempotency import IdempotencyCache
from myagent.context.message import ToolCall
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class ToolExecutor:
    """
    工具执行引擎。
    Phase 2 增强：SafetyGuard -> IdempotencyCache -> SecretManager -> execute。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        idempotency_cache: IdempotencyCache | None = None,
        default_timeout: float = 30.0,
        safety_guard: Any | None = None,          # SafetyGuard 实例
        secret_manager: Any | None = None,         # SecretManager 实例
        hitl_callback: Callable[[str, str, ToolCall], Awaitable[bool]] | None = None,
    ):
        self._registry = registry
        self._cache = idempotency_cache
        self._default_timeout = default_timeout
        self._safety_guard = safety_guard
        self._secret_manager = secret_manager
        self._hitl_callback = hitl_callback  # async fn(tool_name, reason, tool_call) -> approved: bool

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """
        执行单个工具调用。
        流程：Safety -> Idempotency -> Secret -> Execute -> Cache
        """
        tool = self._registry.get(tool_call.name)
        if tool is None:
            return ToolResult(
                content=f"Error: Tool '{tool_call.name}' not found. "
                        f"Available: {[t.name for t in self._registry.list_tools()]}",
                is_error=True,
                metadata={"tool_call_id": tool_call.id},
            )

        # -- Phase 2: 安全检查（必须在幂等缓存之前） --
        if self._safety_guard:
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
                # 需要人工审批
                if self._hitl_callback:
                    approved = await self._hitl_callback(
                        tool_call.name, guard_result.reason, tool_call
                    )
                    if not approved:
                        logger.info(f"Tool REJECTED by user: {tool_call.name}")
                        return ToolResult(
                            content=f"工具 '{tool_call.name}' 被用户拒绝执行: {guard_result.reason}",
                            is_error=True,
                            metadata={"rejected_by": "hitl", "tool_call_id": tool_call.id},
                        )
                    logger.info(f"Tool APPROVED by user: {tool_call.name}")
                else:
                    # 无 HITL 回调时，默认拒绝
                    logger.warning(f"Tool requires HITL but no callback: {tool_call.name}")
                    return ToolResult(
                        content=f"工具 '{tool_call.name}' 需要人工审批但未配置审批通道: {guard_result.reason}",
                        is_error=True,
                        metadata={"tool_call_id": tool_call.id},
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

    async def execute_batch(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """并行执行多个工具调用。"""
        tasks = [self.execute(tc) for tc in tool_calls]
        return await asyncio.gather(*tasks)