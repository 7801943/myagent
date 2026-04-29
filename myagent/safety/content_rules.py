"""
ContentFilter：输入/输出内容安全过滤。
检查用户输入和模型输出中的敏感/危险内容。
"""
import re

from myagent.safety.base import BaseRule, SafetyContext, GuardResult, PolicyDecision
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class InputContentFilter(BaseRule):
    """用户输入内容过滤。"""
    name = "input_content_filter"
    priority = 50

    # 可通过配置扩展
    _INJECTION_PATTERNS = [
        re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
        re.compile(r"forget\s+your\s+(system\s+)?prompt", re.IGNORECASE),
    ]

    async def check(self, context: SafetyContext) -> GuardResult:
        if not context.user_input:
            return GuardResult()

        for pattern in self._INJECTION_PATTERNS:
            if pattern.search(context.user_input):
                logger.warning("InputContentFilter DENY: injection attempt detected")
                return GuardResult(
                    decision=PolicyDecision.DENY,
                    rule_name=self.name,
                    reason="检测到疑似 prompt 注入",
                )

        return GuardResult()


class OutputContentFilter(BaseRule):
    """模型输出内容过滤（防止信息泄露）。"""
    name = "output_content_filter"
    priority = 50

    _SENSITIVE_PATTERNS = [
        re.compile(r"(sk-[a-zA-Z0-9]{20,})", re.IGNORECASE),           # OpenAI API Key
        re.compile(r"(AKIA[A-Z0-9]{16})", re.IGNORECASE),              # AWS Access Key
    ]

    async def check(self, context: SafetyContext) -> GuardResult:
        if not context.output_content:
            return GuardResult()

        for pattern in self._SENSITIVE_PATTERNS:
            if pattern.search(context.output_content):
                logger.warning("OutputContentFilter: potential secret leak detected")
                return GuardResult(
                    decision=PolicyDecision.REWRITE,
                    rule_name=self.name,
                    reason="输出中检测到疑似密钥信息",
                )

        return GuardResult()