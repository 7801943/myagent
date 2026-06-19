"""CLI 命令安全策略：完全访问、白名单和黑名单三种会话级 profile。"""
from __future__ import annotations

import re
import shlex
from enum import Enum
from pathlib import Path
from typing import Any

from myagent.safety.base import BaseRule, GuardResult, PolicyDecision, SafetyContext
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class CLIPolicyMode(str, Enum):
    ALLOW_ALL = "allow_all"
    WHITELIST = "whitelist"
    BLACKLIST = "blacklist"


class CLIFence(BaseRule):
    """根据当前 profile 对 CLI 命令及其 shell 组合进行判定。"""

    name = "cli_fence"
    priority = 10

    _CONTROL_OPERATORS = {"|", "|&", "||", "&&", ";", "&", "(", ")", "{", "}"}
    _REDIRECT_OPERATORS = {">", ">>", "<", "<<", "<<<", "<>", ">|", "&>", "&>>"}
    _COMMAND_BOUNDARIES = {"|", "|&", "||", "&&", ";", "&", "(", "{"}
    _WRAPPER_COMMANDS = {
        "env", "command", "builtin", "exec", "nohup", "time", "sudo",
        "nice", "setsid", "stdbuf", "coproc",
    }
    _ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

    def __init__(
        self,
        policies: dict[str, dict[str, Any]] | None = None,
        default_policy: str = "whitelist",
    ):
        self._policies = self._compile_policies(policies or {})
        if not self._policies:
            self._policies = {
                "full_access": {"mode": CLIPolicyMode.ALLOW_ALL},
            }
        if default_policy not in self._policies:
            raise ValueError(f"Unknown default CLI policy: {default_policy}")
        self._active_policy = default_policy

    @property
    def active_policy(self) -> str:
        return self._active_policy

    @property
    def available_policies(self) -> list[str]:
        return list(self._policies.keys())

    def set_policy(self, policy_name: str) -> None:
        if policy_name not in self._policies:
            raise ValueError(
                f"Unknown CLI policy '{policy_name}'. "
                f"Available: {self.available_policies}"
            )
        self._active_policy = policy_name
        logger.info("CLI safety policy switched to: %s", policy_name)

    def state(self) -> dict[str, Any]:
        profile = self._policies[self._active_policy]
        return {
            "active_policy": self._active_policy,
            "available_policies": self.available_policies,
            "mode": profile["mode"].value,
        }

    async def check(self, context: SafetyContext) -> GuardResult:
        if context.tool_name != "cli_execute":
            return GuardResult()

        profile = self._policies[self._active_policy]
        mode = profile["mode"]
        if mode == CLIPolicyMode.ALLOW_ALL:
            return GuardResult()

        command = str(context.tool_args.get("command", "") or "")
        analysis = self._analyze_command(command)

        if mode == CLIPolicyMode.WHITELIST:
            return self._check_whitelist(command, analysis, profile)
        return self._check_blacklist(command, analysis, profile)

    def _check_whitelist(
        self,
        command: str,
        analysis: dict[str, Any],
        profile: dict[str, Any],
    ) -> GuardResult:
        if analysis["parse_error"]:
            return self._require_approval(
                f"命令无法可靠解析: {analysis['parse_error']}"
            )

        for pattern in profile["approval_patterns"]:
            if pattern.search(command):
                return self._require_approval(
                    f"命令匹配需审批模式: {pattern.pattern}"
                )

        risky_features = analysis["features"] & profile["approval_shell_features"]
        if risky_features:
            return self._require_approval(
                "命令包含需审批的 shell 结构: " + ", ".join(sorted(risky_features))
            )

        unknown = [
            command_name
            for command_name in analysis["commands"]
            if command_name not in profile["allowed_commands"]
        ]
        if unknown:
            return self._require_approval(
                "命令不在白名单中: " + ", ".join(dict.fromkeys(unknown))
            )

        for invocation in analysis["invocations"]:
            allowed_subcommands = profile["allowed_subcommands"].get(
                invocation["command"]
            )
            if allowed_subcommands is None:
                continue
            subcommand = self._extract_subcommand(
                invocation["command"],
                invocation["args"],
            )
            if subcommand not in allowed_subcommands:
                return self._require_approval(
                    f"命令 '{invocation['command']}' 的子命令 "
                    f"'{subcommand or '(缺失)'}' 不在白名单中"
                )

        if not analysis["commands"]:
            return self._require_approval("未识别到可验证的白名单命令")
        return GuardResult()

    def _check_blacklist(
        self,
        command: str,
        analysis: dict[str, Any],
        profile: dict[str, Any],
    ) -> GuardResult:
        for pattern in profile["denied_patterns"]:
            if pattern.search(command):
                return self._deny(f"命令匹配黑名单模式: {pattern.pattern}")

        denied_features = analysis["features"] & profile["denied_shell_features"]
        if denied_features:
            return self._deny(
                "命令包含黑名单 shell 结构: "
                + ", ".join(sorted(denied_features))
            )

        denied = [
            command_name
            for command_name in analysis["commands"]
            if command_name in profile["denied_commands"]
        ]
        if denied:
            return self._deny(
                "命令位于黑名单中: " + ", ".join(dict.fromkeys(denied))
            )

        for invocation in analysis["invocations"]:
            denied_subcommands = profile["denied_subcommands"].get(
                invocation["command"]
            )
            if denied_subcommands is None:
                continue
            subcommand = self._extract_subcommand(
                invocation["command"],
                invocation["args"],
            )
            if subcommand in denied_subcommands:
                return self._deny(
                    f"命令 '{invocation['command']}' 的子命令 "
                    f"'{subcommand}' 位于黑名单中"
                )
        return GuardResult()

    @classmethod
    def _analyze_command(cls, command: str) -> dict[str, Any]:
        features: set[str] = set()
        if re.search(r"`|\$\(", command):
            features.add("command_substitution")
        if re.search(r"(?:<|>)\(", command):
            features.add("process_substitution")
        if re.search(r"\$(?:[A-Za-z_]|\{)", command):
            features.add("variable_expansion")
        if re.search(
            r"(?:^|[;&|]\s*)(?:for|while|until|if|case|select|function)\b",
            command,
        ):
            features.add("shell_control_flow")

        normalized_command = command.replace("\r\n", "\n").replace("\n", " ; ")
        try:
            lexer = shlex.shlex(
                normalized_command,
                posix=True,
                punctuation_chars="|&;<>(){}",
            )
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError as exc:
            return {
                "commands": [],
                "invocations": [],
                "features": features,
                "parse_error": str(exc),
            }

        commands: list[str] = []
        invocations: list[dict[str, Any]] = []
        expect_command = True
        wrapper_pending = False
        current_invocation: dict[str, Any] | None = None

        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in cls._CONTROL_OPERATORS:
                if token in {"|", "|&", "||", "&&", ";"}:
                    features.add("command_chain")
                elif token == "&":
                    features.add("background")
                elif token in {"(", ")", "{", "}"}:
                    features.add("subshell")
                if token in cls._COMMAND_BOUNDARIES:
                    expect_command = True
                    wrapper_pending = False
                    current_invocation = None
                index += 1
                continue

            redirect_index = index
            redirect_fd = ""
            if (
                token.isdigit()
                and index + 1 < len(tokens)
                and cls._is_redirect_operator(tokens[index + 1])
            ):
                redirect_fd = token
                redirect_index = index + 1

            if cls._is_redirect_operator(tokens[redirect_index]):
                redirect_operator = tokens[redirect_index]
                target_index = redirect_index + 1
                target = tokens[target_index] if target_index < len(tokens) else ""
                if not cls._is_allowed_stderr_discard(
                    redirect_fd,
                    redirect_operator,
                    target,
                ):
                    features.add("redirection")
                index = target_index + 1 if target else redirect_index + 1
                continue

            if not expect_command:
                if current_invocation is not None:
                    current_invocation["args"].append(token)
                index += 1
                continue
            if token == "$":
                index += 1
                continue
            if cls._ASSIGNMENT_RE.match(token):
                index += 1
                continue
            if token.startswith("-") and wrapper_pending:
                index += 1
                continue

            command_name = Path(token).name
            commands.append(command_name)
            current_invocation = {"command": command_name, "args": []}
            invocations.append(current_invocation)
            if command_name in cls._WRAPPER_COMMANDS:
                wrapper_pending = True
                expect_command = True
            else:
                wrapper_pending = False
                expect_command = False
            index += 1

        return {
            "commands": commands,
            "invocations": invocations,
            "features": features,
            "parse_error": "",
        }

    @classmethod
    def _is_redirect_operator(cls, token: str) -> bool:
        return token in cls._REDIRECT_OPERATORS or (
            token and set(token) <= {"<", ">"}
        )

    @staticmethod
    def _is_allowed_stderr_discard(fd: str, operator: str, target: str) -> bool:
        return fd == "2" and operator in {">", ">>"} and target == "/dev/null"

    @staticmethod
    def _compile_policies(
        policies: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        compiled: dict[str, dict[str, Any]] = {}
        for name, raw in policies.items():
            mode = CLIPolicyMode(raw.get("mode", ""))
            compiled[name] = {
                "mode": mode,
                "allowed_commands": set(raw.get("allowed_commands", [])),
                "allowed_subcommands": {
                    command: set(subcommands)
                    for command, subcommands in raw.get(
                        "allowed_subcommands", {}
                    ).items()
                },
                "denied_commands": set(raw.get("denied_commands", [])),
                "denied_subcommands": {
                    command: set(subcommands)
                    for command, subcommands in raw.get(
                        "denied_subcommands", {}
                    ).items()
                },
                "approval_patterns": [
                    re.compile(pattern, re.IGNORECASE)
                    for pattern in raw.get("approval_patterns", [])
                ],
                "denied_patterns": [
                    re.compile(pattern, re.IGNORECASE)
                    for pattern in raw.get("denied_patterns", [])
                ],
                "approval_shell_features": set(
                    raw.get(
                        "approval_shell_features",
                        [
                            "redirection",
                            "background",
                            "subshell",
                            "command_substitution",
                            "process_substitution",
                        ],
                    )
                ),
                "denied_shell_features": set(
                    raw.get("denied_shell_features", [])
                ),
            }
        return compiled

    @staticmethod
    def _extract_subcommand(command_name: str, args: list[str]) -> str | None:
        if command_name != "git":
            return args[0] if args else None

        index = 0
        while index < len(args):
            token = args[index]
            if token in {"--no-pager", "--paginate", "-P"}:
                index += 1
                continue
            if token == "-C":
                index += 2
                continue
            if token.startswith("-C") and len(token) > 2:
                index += 1
                continue
            if token.startswith("-"):
                return None
            return token
        return None

    def _deny(self, reason: str) -> GuardResult:
        return GuardResult(
            decision=PolicyDecision.DENY,
            rule_name=f"{self.name}:{self._active_policy}",
            reason=reason,
        )

    def _require_approval(self, reason: str) -> GuardResult:
        return GuardResult(
            decision=PolicyDecision.REQUIRE_HITL,
            rule_name=f"{self.name}:{self._active_policy}",
            reason=reason,
        )
