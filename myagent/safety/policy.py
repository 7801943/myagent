"""
PolicyEngine：V3 升级版策略引擎。
支持 ALLOW / DENY / REQUIRE_HITL / REWRITE 四态决策。
从 safety_rules.yaml 加载规则，动态匹配工具调用。
"""
import re
from typing import Any

from myagent.safety.base import PolicyDecision, SafetyContext, GuardResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class PolicyEngine:
    """
    策略引擎。
    职责：根据配置的策略规则，对工具调用进行决策。
    """

    def __init__(self, tool_policies: list[dict] | None = None, default_action: str = "allow"):
        self._tool_policies = tool_policies or []
        self._default_action = PolicyDecision(default_action)
        # 预编译正则
        self._compiled_policies = self._compile_policies()

    def _compile_policies(self) -> dict[str, list[dict]]:
        """按工具名预编译策略规则。"""
        compiled: dict[str, list[dict]] = {}
        for policy in self._tool_policies:
            tool_name = policy.get("tool_name", "")
            conditions = []
            for cond in policy.get("conditions", []):
                conditions.append({
                    "pattern": re.compile(cond["pattern"], re.IGNORECASE),
                    "match_field": cond.get("match_field", "command"),  # 默认匹配 command 字段
                    "action": PolicyDecision(cond["action"]),
                    "reason": cond.get("reason", "policy rule matched"),
                })
            compiled[tool_name] = conditions
        return compiled

    async def decide(self, context: SafetyContext) -> GuardResult:
        """
        对工具调用做出决策。
        逐条匹配策略规则，第一个命中的规则决定结果。
        """
        tool_name = context.tool_name
        conditions = self._compiled_policies.get(tool_name, [])

        for cond in conditions:
            match_field = cond["match_field"]
            text_to_check = self._get_match_text(context, match_field)

            if text_to_check and cond["pattern"].search(text_to_check):
                decision = cond["action"]
                reason = cond["reason"]
                logger.info(
                    f"PolicyEngine: {decision.value} for tool={tool_name}, "
                    f"reason={reason}"
                )
                return GuardResult(
                    decision=decision,
                    rule_name=f"policy:{tool_name}",
                    reason=reason,
                )

        return GuardResult(decision=self._default_action)

    @staticmethod
    def _get_match_text(context: SafetyContext, field: str) -> str:
        """从上下文中提取要匹配的文本。"""
        if field == "command":
            return context.tool_args.get("command", "")
        elif field == "path":
            return context.tool_args.get("path", "")
        elif field == "content":
            return context.tool_args.get("content", "")
        else:
            return str(context.tool_args.get(field, ""))