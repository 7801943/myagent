"""
安全系统基础类型定义。
BaseRule 为最小粒度规则单元，GuardResult 为检查结果。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PolicyDecision(str, Enum):
    """V3 策略引擎四态决策。"""
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HITL = "require_hitl"
    REWRITE = "rewrite"


@dataclass
class SafetyContext:
    """安全检查的上下文信息。"""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    user_input: str = ""
    output_content: str = ""
    session_id: str = ""


@dataclass
class GuardResult:
    """安全检查结果。"""
    decision: PolicyDecision = PolicyDecision.ALLOW
    rule_name: str = ""
    reason: str = ""
    rewritten_args: dict[str, Any] | None = None   # REWRITE 时的修改后参数

    @property
    def is_allowed(self) -> bool:
        return self.decision == PolicyDecision.ALLOW

    @property
    def is_denied(self) -> bool:
        return self.decision == PolicyDecision.DENY

    @property
    def requires_hitl(self) -> bool:
        return self.decision == PolicyDecision.REQUIRE_HITL


class BaseRule(ABC):
    """
    安全规则抽象基类。
    子类实现 check() 方法，返回 GuardResult。
    priority 越小越先执行。
    """
    name: str = "base_rule"
    priority: int = 100

    @abstractmethod
    async def check(self, context: SafetyContext) -> GuardResult:
        """执行安全检查。"""
        ...