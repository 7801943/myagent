"""
CLIFence：CLI 命令安全围栏。
白名单 + 黑名单 + 路径限制 的三层防御。
"""
import re
import shlex
from pathlib import Path

from myagent.safety.base import BaseRule, SafetyContext, GuardResult, PolicyDecision
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class CLIFence(BaseRule):
    """
    CLI 命令安全围栏。

    检查逻辑（按顺序执行，短路返回）：
    1. 黑名单模式匹配 -> DENY
    2. 路径检查 -> DENY
    3. 白名单命令检查 -> DENY（如果命令不在白名单中）
    4. 通过所有检查 -> ALLOW
    """
    name = "cli_fence"
    priority = 10  # 高优先级

    def __init__(
        self,
        allowed_commands: list[str] | None = None,
        denied_patterns: list[str] | None = None,
        denied_paths: list[str] | None = None,
    ):
        self._allowed_commands = set(allowed_commands or [])
        self._denied_patterns = [
            re.compile(p, re.IGNORECASE) for p in (denied_patterns or [])
        ]
        self._denied_paths = [Path(p) for p in (denied_paths or [])]

    async def check(self, context: SafetyContext) -> GuardResult:
        """对 CLI 命令执行安全检查。"""
        if context.tool_name != "cli_execute":
            return GuardResult()  # 非 CLI 工具直接放行

        command = context.tool_args.get("command", "")
        if not command:
            return GuardResult(
                decision=PolicyDecision.DENY,
                rule_name=self.name,
                reason="空命令",
            )

        # 1. 黑名单模式匹配
        for pattern in self._denied_patterns:
            if pattern.search(command):
                logger.warning(f"CLIFence DENIED (pattern): {command[:100]}")
                return GuardResult(
                    decision=PolicyDecision.DENY,
                    rule_name=self.name,
                    reason=f"命令匹配危险模式: {pattern.pattern}",
                )

        # 2. 路径检查（先剥离标准重定向，避免 2>/dev/null 误报）
        cmd_for_path_check = self._strip_redirections(command)
        for denied_path in self._denied_paths:
            denied_str = str(denied_path)
            if denied_str in cmd_for_path_check:
                logger.warning(f"CLIFence DENIED (path): {command[:100]}")
                return GuardResult(
                    decision=PolicyDecision.REQUIRE_HITL,
                    rule_name=self.name,
                    reason=f"命令涉及禁止路径: {denied_str}",
                )

        # 3. 白名单命令检查
        if self._allowed_commands:
            base_cmd = self._extract_base_command(command)
            if base_cmd and base_cmd not in self._allowed_commands:
                logger.warning(f"CLIFence DENIED (whitelist): {base_cmd}")
                return GuardResult(
                    decision=PolicyDecision.DENY,
                    rule_name=self.name,
                    reason=f"命令 '{base_cmd}' 不在白名单中。允许的命令: {sorted(self._allowed_commands)}",
                )

        return GuardResult()  # 全部通过

    @staticmethod
    def _strip_redirections(command: str) -> str:
        """剥离标准 shell 重定向，避免 2>/dev/null 等误触发路径检查。"""
        # 白名单：允许重定向到这些特殊文件
        _SAFE_DEV_FILES = {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/zero"}
        # 匹配常见重定向模式：2>/dev/null, >/dev/null, &>/dev/null, 2>>/dev/null 等
        result = re.sub(
            r'[0-9]*>>[>]?\s*/dev/\w+|[0-9]*>\s*/dev/\w+|&>\s*/dev/\w+',
            '', command
        )
        return result

    @staticmethod
    def _extract_base_command(command: str) -> str | None:
        """提取命令的基础程序名（第一个 token）。"""
        try:
            tokens = shlex.split(command)
            if tokens:
                # 处理可能的路径前缀：/usr/bin/python3 -> python3
                return Path(tokens[0]).name
        except ValueError:
            # shlex 解析失败，用空格分割
            parts = command.strip().split()
            if parts:
                return Path(parts[0]).name
        return None