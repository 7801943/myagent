"""
SafetyGuard：安全责任链编排器。
按优先级串行执行所有 BaseRule，遇到非 ALLOW 结果立即短路返回。
"""
from myagent.safety.base import BaseRule, SafetyContext, GuardResult, PolicyDecision
from myagent.safety.policy import PolicyEngine
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class SafetyGuard:
    """
    安全守卫：责任链模式。

    执行顺序：
    1. PolicyEngine 策略引擎（基于配置的动态规则）
    2. 注册的 BaseRule 规则链（按 priority 排序）

    第一个返回非 ALLOW 的结果会短路返回。
    """

    def __init__(
        self,
        policy_engine: PolicyEngine | None = None,
        rules: list[BaseRule] | None = None,
    ):
        self._policy_engine = policy_engine
        self._rules = sorted(rules or [], key=lambda r: r.priority)

    def add_rule(self, rule: BaseRule) -> None:
        """添加安全规则并重新排序。"""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)

    async def check_tool_call(self, tool_name: str, args: dict, session_id: str = "") -> GuardResult:
        """
        检查工具调用是否安全。
        返回最终决策结果。
        """
        context = SafetyContext(
            tool_name=tool_name,
            tool_args=args,
            session_id=session_id,
        )

        # 1. 先走策略引擎
        if self._policy_engine:
            result = await self._policy_engine.decide(context)
            if not result.is_allowed:
                return result

        # 2. 再走规则链
        for rule in self._rules:
            result = await rule.check(context)
            if not result.is_allowed:
                logger.info(f"SafetyGuard: {result.decision.value} by {rule.name}: {result.reason}")
                return result

        return GuardResult()  # 全部通过

    async def check_input(self, user_input: str, session_id: str = "") -> GuardResult:
        """检查用户输入安全性。"""
        context = SafetyContext(user_input=user_input, session_id=session_id)
        for rule in self._rules:
            result = await rule.check(context)
            if not result.is_allowed:
                return result
        return GuardResult()

    async def check_output(self, output_content: str, session_id: str = "") -> GuardResult:
        """检查模型输出安全性。"""
        context = SafetyContext(output_content=output_content, session_id=session_id)
        for rule in self._rules:
            result = await rule.check(context)
            if not result.is_allowed:
                return result
        return GuardResult()