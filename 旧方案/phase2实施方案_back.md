# MyAgent Phase 2 编码实施指南（完整版）

> **基准文档**：`实施方案V3.md` — 全自研 Python Agent 框架架构设计方案（V3版）
> **前序依赖**：`phase1实施方案.md` — Phase 1 核心骨架已完成并可运行
> **范围**：Phase 2 聚焦于构建**工具执行沙盒、安全策略引擎、HITL 审批机制、密钥管理器、多模态图像处理**——使框架具备生产级安全能力和实用工具集。
> **不含**：SubAgent 系统（Phase 3）、Skill 系统/文档处理/WebSocket Server（Phase 4）、评测引擎/RAG（Phase 5）。

---

## 一、Phase 2 交付目标（验收标准）

完成后应能运行以下端到端场景：

```bash
# 1. CLI 工具调用（安全沙盒内执行）
myagent chat "请执行 ls -la 命令查看当前目录"
# → CLIFence 安全检查 → SubprocessSandbox 执行 → 结果返回

# 2. 危险命令拦截
myagent chat "请执行 rm -rf / 命令"
# → PolicyEngine 返回 DENY → SafetyGuard 拦截 → 返回错误信息 → 审计日志记录 SafetyEvent

# 3. HITL 审批流程（CLI 模式下的降级实现）
myagent chat "请执行 DROP TABLE users 命令"
# → PolicyEngine 返回 REQUIRE_HITL → CLI 提示用户审批 → 用户输入 approve/reject → 继续/中止

# 4. 文件工具
myagent chat "请读取 config.yaml 文件的内容"
# → FileReadTool 执行 → 结果返回（路径安全检查）

# 5. 多模态图像输入
myagent chat --image ./screenshot.png "请描述这张图片的内容"
# → ImageHandler 处理 → 根据 Provider 能力检测转换格式 → LLM 多模态处理

# 6. 结构化输出解析
myagent chat "请以 JSON 格式返回分析结果"
# → StructuredOutputParser 提取 JSON → 格式化输出

# 7. 审计日志覆盖
# 查看 audit_logs/ 目录，验证 ToolCallEvent、SafetyEvent、ProviderEvent 均正确记录
```

---

## 二、Phase 1 已有代码现状分析

> **重要**：Phase 2 开始前，需要理解 Phase 1 代码的实际状态与 Phase 1 方案的差异，避免在错误的基础上编码。

### Phase 1 代码与方案的已知差异

| 模块 | Phase 1 方案设计 | 实际代码现状 | Phase 2 处理策略 |
|------|---------------|------------|----------------|
| `core/loop.py` | V3 显式状态机 `AgentState` + `_transition()` | **简化版 while 循环**，无状态机、无 StateStore 集成 | Phase 2 不做重构，在 ToolExecutor 层集成安全检查 |
| `core/agent.py` | `AgentConfig` 驱动组装 | **手动依赖注入**（`provider_router`, `context` 等作为参数） | Phase 2 沿用现有接口，Safety/HITL 在 ToolExecutor 层插入 |
| `core/stream.py` | `StreamProcessor.process_event()` | `StreamProcessor.process()` + 支持 `thinking_delta` | 沿用现有实现 |
| `core/parser.py` | `StructuredOutputParser` | **已实现为 `StreamParser`**（负责流事件分发） | Phase 2 新建独立的 `StructuredOutputParser` |
| `config.yaml` | 扁平结构 `providers`/`failover`/`audit` | **嵌套在 `agent:` 下** | Phase 2 沿用现有配置结构，新增 `safety` 配置块 |
| `context/state.py` | `SQLiteStateStore` 完整实现 | 已实现 `StateStore` 抽象 + `SQLiteStateStore` + `JSONLStateStore` | 沿用现有 |

### 修正原则

Phase 2 **不回退重写 Phase 1 组件**，而是在现有代码架构上**增量扩展**：
- Safety/PolicyEngine 通过 `ToolExecutor` 的前置检查集成
- HITL 通过 Hook 体系反向传播到 CLI
- 新模块一律放入各自独立目录，不改动已稳定的模块

---

## 三、开发顺序与模块依赖图

严格按照**自底向上**的依赖拓扑排序开发。

```
                          ┌─────────────────────────┐
                          │ ⑧ CLI 增强 + 集成测试     │
                          │ interfaces/cli/main.py  │
                          └───────────┬─────────────┘
                                      │ depends on
                    ┌─────────────────┼──────────────────┐
                    │                 │                    │
             ┌──────┴──────┐  ┌───────┴────────┐  ┌──────┴──────────┐
             │ ⑦ HITL      │  │ ⑦ 审计扩展     │  │ ⑦ ImageHandler  │
             │ Controller  │  │ (Event覆盖)    │  │  vision/        │
             │ core/hitl   │  │ observability/ │  │                 │
             └──────┬──────┘  └───────┬────────┘  └──────┬──────────┘
                    │                 │                    │
                    └─────────────────┼────────────────────┘
                                      │ depends on
                          ┌───────────┴──────────────┐
                          │ ⑥ ToolExecutor 增强       │
                          │ (Safety 前置 + Secret注入) │
                          │ tools/executor.py 修改     │
                          └───────────┬──────────────┘
                                      │ depends on
              ┌───────────────────────┼───────────────────┐
              │                       │                    │
       ┌──────┴──────┐       ┌───────┴────────┐    ┌──────┴──────────┐
       │ ④ Safety    │       │ ⑤ SecretMgr    │    │ ⑤ Parser增强    │
       │ System      │       │ tools/secrets  │    │ core/parser     │
       │ safety/     │       └────────────────┘    └─────────────────┘
       └──────┬──────┘
              │ depends on
       ┌──────┴───────────────────┐
       │ ③ Sandbox 系统           │
       │ tools/sandbox/           │
       │ tools/cli_tool.py        │
       │ tools/file_tools.py      │
       └──────┬───────────────────┘
              │ depends on
       ┌──────┴──────┐
       │ ② 配置扩展   │
       │ config.yaml │
       │ safety_rules│
       └──────┬──────┘
              │ depends on
       ┌──────┴──────┐
       │ ① Phase 1   │
       │ 已有代码基础 │
       └─────────────┘
```

---

## 四、逐文件编码规范

---

### ① 配置扩展 — 安全规则 + 配置增强

#### [NEW] `config/safety_rules.yaml` — 安全规则配置

```yaml
# 安全规则配置文件
# 定义 CLI 围栏、策略引擎规则、HITL 审批条件

cli_fence:
  # 允许执行的命令（白名单）
  allowed_commands:
    - ls
    - cat
    - grep
    - find
    - echo
    - pwd
    - env
    - python3
    - pip
    - git
    - curl
    - wget
    - head
    - tail
    - wc
    - sort
    - uniq
    - diff
    - mkdir
    - cp
    - mv
    - touch
    - chmod

  # 拒绝执行的模式（黑名单，正则匹配）
  denied_patterns:
    - "rm\\s+-rf\\s+/"
    - "sudo\\s+"
    - "mkfs"
    - "dd\\s+if="
    - ":(){ :|:& };:"
    - "\\|\\s*mail\\b"
    - ">\\/dev\\/sd"
    - "chmod\\s+777\\s+/"

  # 禁止访问的路径
  denied_paths:
    - /etc/shadow
    - /etc/passwd
    - /root
    - /sys
    - /proc/sys
    - /boot
    - /dev

  # 资源限制
  resource_limits:
    max_cpu_seconds: 30
    max_memory_mb: 512
    max_output_bytes: 102400
    timeout_seconds: 60

# 策略引擎规则
policy_engine:
  # 默认策略（当无规则匹配时）
  default_action: allow

  # 工具级策略
  tool_policies:
    # 高危工具 - 需要人工审批
    - tool_name: "cli_execute"
      conditions:
        - pattern: "DROP\\s+TABLE"
          action: require_hitl
          reason: "检测到数据库删表操作"
        - pattern: "DELETE\\s+FROM"
          action: require_hitl
          reason: "检测到数据库删除操作"
        - pattern: "rm\\s+-rf"
          action: deny
          reason: "禁止递归删除操作"
        - pattern: "sudo"
          action: deny
          reason: "禁止使用 sudo 提权"

    # 文件写入 - 特定路径需审批
    - tool_name: "file_write"
      conditions:
        - pattern: "\\.(sh|bash|py)$"
          match_field: "path"
          action: require_hitl
          reason: "写入可执行脚本文件需要审批"
        - pattern: "^/etc/"
          match_field: "path"
          action: deny
          reason: "禁止写入系统配置目录"

# HITL 配置
hitl:
  # CLI 模式下的审批超时（秒）
  cli_approval_timeout: 120
  # 默认自动拒绝（超时后的行为）
  timeout_action: reject
```

#### [MODIFY] `config.yaml` — 新增 safety 配置块

在现有的 `config.yaml` 的 `agent:` 块下追加 safety 配置：

```yaml
  # === Phase 2 新增 ===

  # 安全系统
  safety:
    enabled: true
    rules_path: "config/safety_rules.yaml"

  # 密钥管理
  secrets:
    # 环境变量前缀映射
    env_prefix: "MYAGENT_SECRET_"
    # 需要脱敏的工具参数字段名列表
    sensitive_fields:
      - password
      - api_key
      - token
      - secret
```

---

### ② Sandbox 系统 — CLI 沙盒执行环境

#### [NEW] `myagent/tools/sandbox/__init__.py`

```python
"""沙盒执行模块。"""
from myagent.tools.sandbox.base import BaseSandbox, SandboxResult
from myagent.tools.sandbox.subprocess_sandbox import SubprocessSandbox

__all__ = ["BaseSandbox", "SandboxResult", "SubprocessSandbox"]
```

#### [NEW] `myagent/tools/sandbox/base.py` — 沙盒抽象接口

```python
"""
BaseSandbox：沙盒执行抽象基类。
定义 run() 接口，由不同后端（subprocess / Docker）实现。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

@dataclass
class SandboxResult:
    """沙盒执行结果。"""
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    killed: bool = False
    execution_time_ms: int = 0

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.killed

    @property
    def output(self) -> str:
        """合并 stdout 和 stderr 输出。"""
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        if self.timed_out:
            parts.append("[TIMED OUT]")
        if self.killed:
            parts.append("[KILLED]")
        return "\n".join(parts) if parts else "(no output)"

class BaseSandbox(ABC):
    """沙盒抽象基类。"""

    @abstractmethod
    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> SandboxResult:
        """在沙盒中执行命令。"""
        ...
```

#### [NEW] `myagent/tools/sandbox/subprocess_sandbox.py` — subprocess + ulimit 实现

```python
"""
SubprocessSandbox：基于 subprocess + ulimit 的轻量级沙盒。
最小化系统依赖，支持 CPU/内存/输出大小限制。
"""
import asyncio
import os
import time
from dataclasses import dataclass

from myagent.tools.sandbox.base import BaseSandbox, SandboxResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

@dataclass
class ResourceLimits:
    """沙盒资源限制配置。"""
    max_cpu_seconds: int = 30
    max_memory_mb: int = 512
    max_output_bytes: int = 102400   # 100KB
    timeout_seconds: float = 60.0

class SubprocessSandbox(BaseSandbox):
    """
    基于 subprocess + ulimit 的沙盒实现。

    安全措施：
    1. ulimit 限制 CPU 时间和虚拟内存
    2. asyncio.wait_for 控制总超时
    3. 输出截断（防止 /dev/urandom 等攻击）
    4. 不继承父进程的环境变量（可选）
    """

    def __init__(self, limits: ResourceLimits | None = None):
        self._limits = limits or ResourceLimits()

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> SandboxResult:
        """在受限子进程中执行命令。"""
        timeout = timeout or self._limits.timeout_seconds
        start_time = time.monotonic()

        # 构建 ulimit 前缀命令
        ulimit_prefix = (
            f"ulimit -t {self._limits.max_cpu_seconds} && "
            f"ulimit -v {self._limits.max_memory_mb * 1024} && "
        )
        wrapped_cmd = f"/bin/bash -c '{ulimit_prefix}{self._escape_for_bash(command)}'"

        # 构建环境变量
        proc_env = self._build_env(env)

        try:
            process = await asyncio.create_subprocess_shell(
                wrapped_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=proc_env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # 超时 - 杀死进程
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
                elapsed = int((time.monotonic() - start_time) * 1000)
                logger.warning(f"Sandbox command timed out after {timeout}s: {command[:100]}")
                return SandboxResult(
                    stdout="",
                    stderr="",
                    exit_code=-1,
                    timed_out=True,
                    execution_time_ms=elapsed,
                )

            elapsed = int((time.monotonic() - start_time) * 1000)

            # 截断输出
            stdout = self._truncate_output(stdout_bytes)
            stderr = self._truncate_output(stderr_bytes)

            return SandboxResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode or 0,
                execution_time_ms=elapsed,
            )

        except Exception as e:
            elapsed = int((time.monotonic() - start_time) * 1000)
            logger.error(f"Sandbox execution error: {e}")
            return SandboxResult(
                stdout="",
                stderr=f"Sandbox error: {type(e).__name__}: {e}",
                exit_code=-1,
                execution_time_ms=elapsed,
            )

    def _escape_for_bash(self, command: str) -> str:
        """转义命令中的单引号，防止注入。"""
        return command.replace("'", "'\\''")

    def _build_env(self, extra_env: dict[str, str] | None) -> dict[str, str]:
        """构建子进程环境变量（继承必要的 PATH 等，隔离敏感变量）。"""
        safe_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TERM": "xterm",
        }
        if extra_env:
            safe_env.update(extra_env)
        return safe_env

    def _truncate_output(self, data: bytes) -> str:
        """截断超长输出。"""
        max_bytes = self._limits.max_output_bytes
        if len(data) > max_bytes:
            truncated = data[:max_bytes]
            text = truncated.decode("utf-8", errors="replace")
            text += f"\n...[输出截断：原始 {len(data)} 字节，截断至 {max_bytes} 字节]"
            return text
        return data.decode("utf-8", errors="replace")
```

#### [NEW] `myagent/tools/sandbox/docker_sandbox.py` — Docker 预留骨架

```python
"""
DockerSandbox：Docker 容器沙盒（预留骨架）。
通过 --sandbox-backend=docker 启用。
"""
from myagent.tools.sandbox.base import BaseSandbox, SandboxResult

class DockerSandbox(BaseSandbox):
    """Docker 容器沙盒。Phase 2 预留骨架，不实现。"""

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> SandboxResult:
        raise NotImplementedError(
            "Docker sandbox is not yet implemented. "
            "Use --sandbox-backend=subprocess (default) instead."
        )
```

---

### ③ 工具实现 — CLITool + FileTools

#### [NEW] `myagent/tools/cli_tool.py` — CLI 执行工具

```python
"""
CLITool：在安全沙盒中执行 CLI 命令。
集成 CLIFence 安全围栏，在执行前进行白名单/黑名单/路径检查。
"""
from myagent.tools.base import BaseTool, ToolResult
from myagent.tools.sandbox.base import BaseSandbox
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class CLITool(BaseTool):
    """
    CLI 命令执行工具。
    通过 BaseSandbox 执行命令，前置安全检查由 ToolExecutor -> PolicyEngine 处理。
    """
    name = "cli_execute"
    description = (
        "在安全沙盒中执行 CLI 命令。"
        "可以执行常见的文件操作、Python 脚本、git 命令等。"
        "受到安全围栏限制，危险命令会被拦截。"
    )

    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的命令行命令",
            },
            "cwd": {
                "type": "string",
                "description": "工作目录（可选，默认当前目录）",
            },
        },
        "required": ["command"],
    }

    def __init__(self, sandbox: BaseSandbox):
        self._sandbox = sandbox

    async def execute(self, command: str, cwd: str | None = None, **kwargs) -> ToolResult:
        """执行 CLI 命令。"""
        logger.info(f"CLITool executing: {command[:100]}")

        result = await self._sandbox.run(command, cwd=cwd)

        if result.timed_out:
            return ToolResult(
                content=f"命令执行超时: {command[:100]}\n{result.output}",
                is_error=True,
                metadata={"execution_time_ms": result.execution_time_ms},
            )

        if result.exit_code != 0:
            return ToolResult(
                content=f"命令执行失败 (退出码: {result.exit_code}):\n{result.output}",
                is_error=True,
                metadata={
                    "exit_code": result.exit_code,
                    "execution_time_ms": result.execution_time_ms,
                },
            )

        return ToolResult(
            content=result.output,
            metadata={"execution_time_ms": result.execution_time_ms},
        )
```

#### [NEW] `myagent/tools/file_tools.py` — 文件读写工具

```python
"""
FileReadTool / FileWriteTool：文件读写工具。
包含路径安全检查，防止访问敏感系统目录。
"""
import os
from pathlib import Path

from myagent.tools.base import BaseTool, ToolResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

# 默认禁止访问的路径
_DENIED_PATHS = {
    "/etc/shadow", "/etc/passwd", "/root",
    "/sys", "/proc/sys", "/boot", "/dev",
}

def _check_path_safety(path: str, denied_paths: set[str] | None = None) -> str | None:
    """
    路径安全检查。
    返回 None 表示安全，返回错误消息表示不安全。
    """
    denied = denied_paths or _DENIED_PATHS
    resolved = str(Path(path).resolve())
    for denied_path in denied:
        if resolved.startswith(denied_path):
            return f"路径被安全策略禁止: {path} (匹配: {denied_path})"
    return None


class FileReadTool(BaseTool):
    """读取文件内容。"""
    name = "file_read"
    description = "读取指定路径的文件内容。支持文本文件，自动检测编码。"

    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径",
            },
            "max_lines": {
                "type": "integer",
                "description": "最多读取的行数（可选，默认 500）",
                "default": 500,
            },
        },
        "required": ["path"],
    }

    def __init__(self, denied_paths: set[str] | None = None):
        self._denied_paths = denied_paths or _DENIED_PATHS

    async def execute(self, path: str, max_lines: int = 500, **kwargs) -> ToolResult:
        # 路径安全检查
        error = _check_path_safety(path, self._denied_paths)
        if error:
            return ToolResult(content=error, is_error=True)

        target = Path(path)
        if not target.exists():
            return ToolResult(content=f"文件不存在: {path}", is_error=True)
        if not target.is_file():
            return ToolResult(content=f"不是文件: {path}", is_error=True)

        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        lines.append(f"\n...[截断：文件超过 {max_lines} 行]")
                        break
                    lines.append(line)
            content = "".join(lines)
            return ToolResult(
                content=content,
                metadata={"path": str(target.resolve()), "lines_read": len(lines)},
            )
        except Exception as e:
            return ToolResult(content=f"读取文件失败: {e}", is_error=True)


class FileWriteTool(BaseTool):
    """写入文件内容。"""
    name = "file_write"
    description = "将内容写入指定路径的文件。如果文件不存在则创建，如果文件已存在则覆盖。"

    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要写入的文件路径",
            },
            "content": {
                "type": "string",
                "description": "要写入的文件内容",
            },
            "append": {
                "type": "boolean",
                "description": "是否追加模式（默认覆盖写入）",
                "default": False,
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, denied_paths: set[str] | None = None):
        self._denied_paths = denied_paths or _DENIED_PATHS

    async def execute(self, path: str, content: str, append: bool = False, **kwargs) -> ToolResult:
        # 路径安全检查
        error = _check_path_safety(path, self._denied_paths)
        if error:
            return ToolResult(content=error, is_error=True)

        target = Path(path)
        try:
            # 确保父目录存在
            target.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if append else "w"
            with open(target, mode, encoding="utf-8") as f:
                f.write(content)

            action = "追加" if append else "写入"
            return ToolResult(
                content=f"文件{action}成功: {target.resolve()} ({len(content)} 字符)",
                metadata={"path": str(target.resolve()), "chars_written": len(content)},
            )
        except Exception as e:
            return ToolResult(content=f"写入文件失败: {e}", is_error=True)
```

---

### ④ Safety 系统 — 策略引擎 + 安全责任链

#### [NEW] `myagent/safety/__init__.py`

```python
"""安全系统包。"""
from myagent.safety.base import BaseRule, GuardResult, SafetyContext, PolicyDecision
from myagent.safety.guard import SafetyGuard
from myagent.safety.policy import PolicyEngine
from myagent.safety.cli_fence import CLIFence

__all__ = [
    "BaseRule", "GuardResult", "SafetyContext", "PolicyDecision",
    "SafetyGuard", "PolicyEngine",
    "CLIFence",
]
```

#### [NEW] `myagent/safety/base.py` — 安全基类 + 数据模型

```python
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
```

#### [NEW] `myagent/safety/cli_fence.py` — CLI 安全围栏

```python
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

        # 2. 路径检查
        for denied_path in self._denied_paths:
            denied_str = str(denied_path)
            if denied_str in command:
                logger.warning(f"CLIFence DENIED (path): {command[:100]}")
                return GuardResult(
                    decision=PolicyDecision.DENY,
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
```

#### [NEW] `myagent/safety/content_rules.py` — 内容安全规则

```python
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
                logger.warning(f"InputContentFilter DENY: injection attempt detected")
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
```

#### [NEW] `myagent/safety/policy.py` — PolicyEngine 策略引擎

```python
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
```

#### [NEW] `myagent/safety/guard.py` — SafetyGuard 安全守卫（责任链编排）

```python
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
```

#### [NEW] `myagent/safety/rules/__init__.py`

```python
"""自定义安全规则插件目录。放入此目录的 BaseRule 子类将被自动加载。"""
```

---

### ⑤ SecretManager — 统一密钥凭据管理

#### [NEW] `myagent/tools/secrets.py`

```python
"""
SecretManager：统一密钥凭据管理器。
职责：
1. 从环境变量 / 配置文件安全获取凭据
2. 向指定工具注入运行时凭据
3. 向 FieldMasker 注册需要脱敏的密文值
"""
import os
from typing import Any

from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class SecretManager:
    """
    统一密钥凭据管理器。

    使用场景：
    - 工具需要 API Key 时，通过 SecretManager 获取，不直接从环境变量读取
    - 获取的值自动注册到 FieldMasker 进行脱敏
    - 审计日志中不会出现明文密钥
    """

    def __init__(
        self,
        env_prefix: str = "MYAGENT_SECRET_",
        sensitive_fields: list[str] | None = None,
    ):
        self._env_prefix = env_prefix
        self._sensitive_fields = set(sensitive_fields or [
            "password", "api_key", "token", "secret",
            "access_key", "secret_key", "credential",
        ])
        self._masker = None   # 延迟注入 FieldMasker
        self._resolved_secrets: dict[str, str] = {}

    def set_masker(self, masker: Any) -> None:
        """注入 FieldMasker 实例，用于自动脱敏。"""
        self._masker = masker

    def resolve(self, key: str) -> str | None:
        """
        解析密钥值。
        查找顺序：缓存 -> 环境变量（带前缀）-> 环境变量（原名）
        """
        if key in self._resolved_secrets:
            return self._resolved_secrets[key]

        # 尝试带前缀的环境变量
        prefixed_key = f"{self._env_prefix}{key.upper()}"
        value = os.environ.get(prefixed_key)

        # 回退到不带前缀的环境变量
        if value is None:
            value = os.environ.get(key.upper())
            if value is None:
                value = os.environ.get(key)

        if value:
            self._resolved_secrets[key] = value
            # 自动注册脱敏
            if self._masker:
                self._masker.add_redact_pattern(value)
            logger.debug(f"Secret resolved: {key} (from env)")

        return value

    def resolve_tool_credentials(self, tool_name: str) -> dict[str, str]:
        """
        为指定工具解析所需凭据。
        凭据以 MYAGENT_SECRET_{TOOL_NAME}_{KEY} 格式存放在环境变量中。
        """
        prefix = f"{self._env_prefix}{tool_name.upper()}_"
        credentials = {}
        for key, value in os.environ.items():
            if key.startswith(prefix):
                cred_name = key[len(prefix):].lower()
                credentials[cred_name] = value
                # 自动脱敏
                if self._masker:
                    self._masker.add_redact_pattern(value)
        return credentials

    def redact_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """
        脱敏工具参数中的敏感字段。
        用于审计日志和 UI 显示。
        """
        redacted = dict(args)
        for key in args:
            if any(field in key.lower() for field in self._sensitive_fields):
                redacted[key] = "[REDACTED]"
        return redacted

    def inject_secrets(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """
        向工具参数中注入必要的凭据。
        凭据字段以 _credentials 键标识。
        """
        credentials = self.resolve_tool_credentials(tool_name)
        if credentials:
            enriched = dict(args)
            enriched["_credentials"] = credentials
            return enriched
        return args
```

---

### ⑤-2 StructuredOutputParser 增强

#### [MODIFY] `myagent/core/parser.py` — 追加 StructuredOutputParser

> 注意：Phase 1 的 `parser.py` 实际实现了 `StreamParser`（流事件分发器）。Phase 2 在同一文件末尾追加 `StructuredOutputParser` 类，不修改已有的 `StreamParser`。

```python
# ── 在 parser.py 文件末尾追加以下内容 ──

import json
import re
from typing import Any, Callable

class StructuredOutputParser:
    """
    从 LLM 文本输出中提取结构化数据。
    支持：
    1. Markdown 代码块提取 (```json ... ```)
    2. 纯 JSON 文本解析
    3. 自定义格式注册
    """

    _CODEBLOCK_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

    def __init__(self):
        self._parsers: dict[str, Callable[[str], Any]] = {}

    def register(self, format_name: str, parser_fn: Callable[[str], Any]) -> None:
        """注册自定义解析器。"""
        self._parsers[format_name] = parser_fn

    def extract_json(self, text: str) -> dict | list | None:
        """
        尝试从文本中提取 JSON 内容。
        优先匹配代码块，回退到整段文本。
        """
        # 先尝试代码块
        for match in self._CODEBLOCK_RE.finditer(text):
            lang, content = match.group(1), match.group(2)
            if lang in ("json", ""):
                try:
                    return json.loads(content.strip())
                except json.JSONDecodeError:
                    continue

        # 再尝试整段文本
        text_stripped = text.strip()
        for start_char in ["{", "["]:
            idx = text_stripped.find(start_char)
            if idx >= 0:
                try:
                    return json.loads(text_stripped[idx:])
                except json.JSONDecodeError:
                    continue

        return None

    def extract_codeblocks(self, text: str, language: str | None = None) -> list[str]:
        """
        提取所有代码块的内容。
        language: 指定语言时只提取该语言的代码块，None 则提取全部。
        """
        blocks = []
        for match in self._CODEBLOCK_RE.finditer(text):
            lang, content = match.group(1), match.group(2)
            if language is None or lang == language:
                blocks.append(content.strip())
        return blocks

    def parse(self, text: str, format_name: str) -> Any:
        """使用注册的解析器解析文本。"""
        if format_name in self._parsers:
            return self._parsers[format_name](text)
        if format_name == "json":
            return self.extract_json(text)
        raise ValueError(f"Unknown format: {format_name}")
```

---

### ⑥ ToolExecutor 增强 — 集成 Safety 前置检查 + Secret 注入

#### [MODIFY] `myagent/tools/executor.py`

> **重要**：这是 Phase 2 的核心集成点。ToolExecutor 在执行工具前先经过 SafetyGuard 安全检查，根据 PolicyDecision 决定执行、拒绝或挂起。

替换整个 `ToolExecutor` 类，增加以下逻辑：

```python
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

class ToolNotFoundError(Exception):
    pass

class ToolDeniedError(Exception):
    """安全策略拒绝执行工具。"""
    def __init__(self, tool_name: str, reason: str):
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"Tool '{tool_name}' denied: {reason}")

class HITLRequiredError(Exception):
    """需要人工审批才能执行。"""
    def __init__(self, tool_name: str, reason: str, tool_call: ToolCall):
        self.tool_name = tool_name
        self.reason = reason
        self.tool_call = tool_call
        super().__init__(f"Tool '{tool_name}' requires approval: {reason}")

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
            )

        # -- Phase 2: 安全检查 --
        if self._safety_guard:
            guard_result = await self._safety_guard.check_tool_call(
                tool_call.name, tool_call.arguments
            )
            if guard_result.is_denied:
                logger.warning(f"Tool DENIED: {tool_call.name} - {guard_result.reason}")
                return ToolResult(
                    content=f"安全策略拒绝执行工具 '{tool_call.name}': {guard_result.reason}",
                    is_error=True,
                    metadata={"denied_by": guard_result.rule_name},
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
                            metadata={"rejected_by": "hitl"},
                        )
                    logger.info(f"Tool APPROVED by user: {tool_call.name}")
                else:
                    # 无 HITL 回调时，默认拒绝
                    logger.warning(f"Tool requires HITL but no callback: {tool_call.name}")
                    return ToolResult(
                        content=f"工具 '{tool_call.name}' 需要人工审批但未配置审批通道: {guard_result.reason}",
                        is_error=True,
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
                logger.info(f"Idempotency HIT: {tool_call.id}")
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
                content=f"Error: Tool '{tool_call.name}' timed out after {self._default_timeout}s",
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

        # -- 幂等缓存存储 --
        if self._cache:
            await self._cache.put(tool_call.id, result)

        return result

    async def execute_batch(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """并行执行多个工具调用。"""
        tasks = [self.execute(tc) for tc in tool_calls]
        return await asyncio.gather(*tasks)
```

---

### ⑦ HITL 控制器

#### [NEW] `myagent/core/hitl.py` — 人在回路控制

```python
"""
HITLController：人在回路控制器。
Phase 2 实现 CLI 模式下的同步审批（用户输入 y/n）。
预留 WebSocket 异步审批接口供 Phase 4 使用。
"""
from typing import Any

from myagent.context.message import ToolCall
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class HITLController:
    """
    HITL 控制器基类。
    提供审批请求接口，具体实现由不同的 Interface 层提供。
    """

    async def request_approval(
        self,
        tool_name: str,
        reason: str,
        tool_call: ToolCall,
    ) -> bool:
        """
        请求人工审批。
        返回 True 表示批准，False 表示拒绝。
        默认实现：自动拒绝（安全第一）。
        """
        logger.warning(f"HITL: auto-rejecting {tool_name} (no approval handler)")
        return False


class CLIHITLController(HITLController):
    """
    CLI 模式下的 HITL 控制器。
    通过 Rich Console 向用户展示审批请求，等待输入。
    """

    def __init__(self, console: Any = None, timeout: int = 120):
        self._console = console
        self._timeout = timeout

    async def request_approval(
        self,
        tool_name: str,
        reason: str,
        tool_call: ToolCall,
    ) -> bool:
        """在 CLI 中请求用户审批。"""
        if self._console is None:
            from rich.console import Console
            self._console = Console()

        self._console.print()
        self._console.print("[bold yellow]========== 需要人工审批 ==========[/]")
        self._console.print(f"  工具: [bold]{tool_name}[/]")
        self._console.print(f"  原因: {reason}")
        self._console.print(f"  参数: {tool_call.arguments}")
        self._console.print("[bold yellow]=================================[/]")
        self._console.print()

        try:
            response = self._console.input(
                "[bold yellow]是否批准执行？[/] ([green]y[/]es / [red]n[/]o): "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            self._console.print("[dim]审批被取消[/]")
            return False

        if response in ("y", "yes", "approve"):
            self._console.print("[green]已批准执行[/]")
            return True
        elif response in ("n", "no", "reject"):
            self._console.print("[red]已拒绝执行[/]")
            return False
        else:
            self._console.print("[red]未识别的输入，自动拒绝[/]")
            return False
```

---

### ⑦-2 ImageHandler — 多模态图像处理

#### [NEW] `myagent/vision/__init__.py`

```python
"""多模态图像处理模块。"""
from myagent.vision.image_handler import ImageHandler

__all__ = ["ImageHandler"]
```

#### [NEW] `myagent/vision/image_handler.py`

```python
"""
ImageHandler：多模态图像输入处理。
支持本地文件、URL、bytes 输入，根据 Provider 类型转换为对应的 content block。
"""
import base64
from pathlib import Path

from myagent.providers.base import ProviderCapabilities
from myagent.context.message import ContentBlock
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

# 支持的图像格式
SUPPORTED_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

class ImageHandler:
    """
    多模态图像输入处理器。

    根据 Provider 能力检测决定是否启用图像输入：
    - 支持 vision -> 转换为 base64 content block
    - 不支持 vision -> 返回文本替代说明
    """

    def __init__(self, capabilities: ProviderCapabilities | None = None):
        self._capabilities = capabilities or ProviderCapabilities()

    async def prepare(
        self,
        source: str | Path | bytes,
        provider_type: str = "openai",
    ) -> ContentBlock:
        """
        处理图像输入，返回适配 Provider 的 ContentBlock。

        Args:
            source: 图像来源 -- URL 字符串、本地文件路径、或 bytes
            provider_type: "openai" | "anthropic"
        """
        if not self._capabilities.supports_vision:
            return ContentBlock(
                type="text",
                text="[图像输入：当前模型不支持多模态，已忽略]",
            )

        # 判断来源类型
        if isinstance(source, bytes):
            return self._from_bytes(source, provider_type)
        elif isinstance(source, Path) or (isinstance(source, str) and not source.startswith(("http://", "https://"))):
            return await self._from_file(Path(source), provider_type)
        elif isinstance(source, str) and source.startswith(("http://", "https://")):
            return self._from_url(source, provider_type)
        else:
            return ContentBlock(type="text", text=f"[不支持的图像来源: {type(source)}]")

    async def _from_file(self, path: Path, provider_type: str) -> ContentBlock:
        """从本地文件加载图像。"""
        if not path.exists():
            return ContentBlock(type="text", text=f"[图像文件不存在: {path}]")

        media_type = SUPPORTED_MEDIA_TYPES.get(path.suffix.lower())
        if not media_type:
            return ContentBlock(
                type="text",
                text=f"[不支持的图像格式: {path.suffix}]",
            )

        # 检查文件大小
        file_size_mb = path.stat().st_size / (1024 * 1024)
        if file_size_mb > self._capabilities.max_image_size_mb:
            return ContentBlock(
                type="text",
                text=f"[图像文件过大: {file_size_mb:.1f}MB > {self._capabilities.max_image_size_mb}MB]",
            )

        with open(path, "rb") as f:
            data = f.read()

        return self._from_bytes(data, provider_type, media_type=media_type)

    def _from_bytes(
        self, data: bytes, provider_type: str, media_type: str = "image/png"
    ) -> ContentBlock:
        """从 bytes 构建 ContentBlock。"""
        b64_data = base64.b64encode(data).decode("ascii")

        if provider_type == "openai":
            # OpenAI 格式: data URI
            data_uri = f"data:{media_type};base64,{b64_data}"
            return ContentBlock(
                type="image_url",
                url=data_uri,
                media_type=media_type,
            )
        else:
            # Anthropic 格式: base64 数据
            return ContentBlock(
                type="image_base64",
                base64_data=b64_data,
                media_type=media_type,
            )

    def _from_url(self, url: str, provider_type: str) -> ContentBlock:
        """从 URL 构建 ContentBlock。"""
        return ContentBlock(
            type="image_url",
            url=url,
        )
```

---

### ⑦-3 审计系统扩展 — SafetyEvent + ProviderEvent 覆盖

#### [MODIFY] `myagent/observability/events.py` — 新增 SafetyEvent

Phase 1 已有 `ConversationEvent`、`ToolCallEvent`、`ProviderEvent`、`ErrorEvent`。Phase 2 需在文件末尾追加 `SafetyEvent`：

```python
class SafetyEvent(AuditEvent):
    """安全系统事件。"""
    event_type: str = "safety"
    rule_name: str = ""
    action: str = ""              # denied | require_hitl | rewrite | warned
    tool_name: str = ""
    tool_args_summary: str = ""   # 工具参数摘要（脱敏后）
    input_hash: str = ""
    reason: str = ""
    user_decision: str | None = None   # HITL 决策结果: approved | rejected | None
```

#### [MODIFY] `myagent/observability/hook.py` — AuditHook 扩展

在现有的 `AuditHook` 中追加安全事件和 Provider Failover 事件的钩子方法：

```python
    # -- 新增：安全拦截事件 --
    async def on_safety_blocked(
        self, ctx: HookContext, *, rule: str, reason: str, action: str
    ) -> None:
        from myagent.observability.events import SafetyEvent
        await self._audit.log(SafetyEvent(
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            rule_name=rule,
            action=action,
            reason=reason,
        ))

    # -- 新增：Provider Failover 事件 --
    async def on_provider_failover(
        self, ctx: HookContext, *, from_provider: str, to_provider: str, reason: str
    ) -> None:
        from myagent.observability.events import ProviderEvent
        await self._audit.log(ProviderEvent(
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            sub_type="failover",
            from_provider=from_provider,
            to_provider=to_provider,
            error_message=reason,
        ))
```

---

### ⑧ CLI 增强 — 集成 Safety + HITL + Image + 配置加载

#### [MODIFY] `myagent/interfaces/cli/main.py`

在现有的 CLI 入口中扩展以下内容：

1. **加载安全规则配置**并初始化 SafetyGuard
2. **注册 CLITool、FileReadTool、FileWriteTool**
3. **初始化 CLIHITLController** 并传入 ToolExecutor
4. **支持 `--image` 参数**进行多模态输入
5. **支持 `--sandbox-backend` 参数**选择沙盒

CLI 命令行新增选项：

```python
@cli.command()
@click.argument("message", required=False)
@click.option("--session-id", default=None, help="会话 ID")
@click.option("--system-prompt", default=None, help="System Prompt")
@click.option("--show-tools", is_flag=True, help="显示工具调用详情")
@click.option("--image", multiple=True, help="附带图像文件路径（可多次指定）")
@click.option("--sandbox-backend", default="subprocess",
              type=click.Choice(["subprocess", "docker"]),
              help="沙盒后端选择")
@click.option("--no-safety", is_flag=True, help="禁用安全检查（仅开发调试）")
@click.pass_context
def chat(ctx, message, session_id, system_prompt, show_tools, image, sandbox_backend, no_safety):
    ...
```

在 `_chat()` 中的完整构建逻辑伪代码：

```python
async def _chat(config_path, message, session_id, system_prompt, images, sandbox_backend, no_safety):
    # ... 已有的配置加载与 Agent 构建 ...

    # == Phase 2 新增：安全系统 ==
    safety_guard = None
    if not no_safety:
        from myagent.safety.guard import SafetyGuard
        from myagent.safety.policy import PolicyEngine
        from myagent.safety.cli_fence import CLIFence

        # 加载安全规则
        safety_rules_path = raw.get("agent", {}).get("safety", {}).get(
            "rules_path", "config/safety_rules.yaml"
        )
        safety_config = load_yaml_config(safety_rules_path)

        # 构建 CLI 围栏
        cli_fence_config = safety_config.get("cli_fence", {})
        cli_fence = CLIFence(
            allowed_commands=cli_fence_config.get("allowed_commands"),
            denied_patterns=cli_fence_config.get("denied_patterns"),
            denied_paths=cli_fence_config.get("denied_paths"),
        )

        # 构建策略引擎
        policy_config = safety_config.get("policy_engine", {})
        policy_engine = PolicyEngine(
            tool_policies=policy_config.get("tool_policies", []),
            default_action=policy_config.get("default_action", "allow"),
        )

        safety_guard = SafetyGuard(
            policy_engine=policy_engine,
            rules=[cli_fence],
        )

    # == Phase 2 新增：HITL 控制器 ==
    from myagent.core.hitl import CLIHITLController
    hitl = CLIHITLController(console=console)

    # == Phase 2 新增：SecretManager ==
    from myagent.tools.secrets import SecretManager
    secret_mgr = SecretManager()
    secret_mgr.set_masker(masker)

    # == Phase 2 新增：沙盒 + 工具注册 ==
    from myagent.tools.sandbox.subprocess_sandbox import SubprocessSandbox, ResourceLimits
    from myagent.tools.cli_tool import CLITool
    from myagent.tools.file_tools import FileReadTool, FileWriteTool

    resource_limits = ResourceLimits(
        **cli_fence_config.get("resource_limits", {})
    ) if cli_fence_config.get("resource_limits") else ResourceLimits()

    sandbox = SubprocessSandbox(limits=resource_limits)

    # 注册工具
    agent.add_tool(CLITool(sandbox=sandbox))
    agent.add_tool(FileReadTool())
    agent.add_tool(FileWriteTool())

    # == Phase 2 新增：将 SafetyGuard 和 HITL 注入 ToolExecutor ==
    agent._executor._safety_guard = safety_guard
    agent._executor._secret_manager = secret_mgr
    agent._executor._hitl_callback = hitl.request_approval

    # == Phase 2 新增：图像处理 ==
    if images:
        from myagent.vision.image_handler import ImageHandler
        from myagent.context.message import ContentBlock

        provider = agent._router.current_provider
        provider_type = "anthropic" if "anthropic" in (provider.name if provider else "") else "openai"
        handler = ImageHandler(capabilities=provider.capabilities if provider else None)

        content_blocks = []
        if message:
            content_blocks.append(ContentBlock(type="text", text=message))
        for img_path in images:
            block = await handler.prepare(img_path, provider_type=provider_type)
            content_blocks.append(block)
        # 使用多模态内容发送消息（替代纯文本）
        # agent._context.add_user_message(content_blocks)
        # 然后触发 agent.run() 流程

    # ... 其余交互逻辑保持不变 ...
```

---

## 五、数据流与执行路径

### 工具调用安全检查完整时序

```
用户输入
  |
  v
AgentLoop._react_loop()
  |
  v
Provider.stream() -> tool_call(cli_execute, {"command":"ls -la"})
  |
  v
AgentLoop._execute_tools()
  |
  v
ToolExecutor.execute(tool_call)
  |
  +-- 1. SafetyGuard.check_tool_call("cli_execute", args)
  |     +-- PolicyEngine.decide(context)
  |     |     +-- 匹配规则 -> ALLOW | DENY | REQUIRE_HITL | REWRITE
  |     +-- CLIFence.check(context)
  |           +-- 黑名单检查 -> DENY?
  |           +-- 路径检查   -> DENY?
  |           +-- 白名单检查 -> DENY?
  |
  +-- 2. 决策处理
  |     +-- ALLOW -> 继续执行
  |     +-- DENY  -> 返回 ToolResult(is_error=True)
  |     |           -> Hook.on_safety_blocked()
  |     |           -> AuditHook -> SafetyEvent
  |     +-- REQUIRE_HITL -> hitl_callback(tool_name, reason, tool_call)
  |     |     +-- approved  -> 继续执行
  |     |     +-- rejected  -> 返回 ToolResult(is_error=True)
  |     +-- REWRITE -> 替换参数 -> 继续执行
  |
  +-- 3. IdempotencyCache.get(tool_call_id) 幂等缓存
  |
  +-- 4. SecretManager.inject_secrets() 凭据注入
  |
  +-- 5. CLITool.execute(command="ls -la")
  |     +-- SubprocessSandbox.run(command, cwd, env)
  |           +-- ulimit 限制
  |           +-- asyncio.wait_for(timeout)
  |           +-- SandboxResult
  |
  +-- 6. IdempotencyCache.put(tool_call_id, result)
  |
  +-- 7. 返回 ToolResult -> Hook 通知 -> 写入 Context -> 下一轮 LLM 调用
```

---

## 六、配置文件完整模板

### `config/safety_rules.yaml`

（见第四章 ①[NEW] 中的完整内容）

### `config.yaml` 增量部分

```yaml
  # === Phase 2 新增部分（追加到 agent: 块下） ===

  safety:
    enabled: true
    rules_path: "config/safety_rules.yaml"

  secrets:
    env_prefix: "MYAGENT_SECRET_"
    sensitive_fields:
      - password
      - api_key
      - token
      - secret
```

---

## 七、Phase 2 新增文件清单

| 序号 | 文件路径 | 类型 | 描述 |
|------|---------|------|------|
| 1 | `config/safety_rules.yaml` | 新建 | 安全规则配置文件 |
| 2 | `myagent/tools/sandbox/__init__.py` | 新建 | 沙盒模块初始化 |
| 3 | `myagent/tools/sandbox/base.py` | 新建 | 沙盒抽象接口 + SandboxResult |
| 4 | `myagent/tools/sandbox/subprocess_sandbox.py` | 新建 | subprocess + ulimit 沙盒实现 |
| 5 | `myagent/tools/sandbox/docker_sandbox.py` | 新建 | Docker 沙盒预留骨架 |
| 6 | `myagent/tools/cli_tool.py` | 新建 | CLI 命令执行工具 |
| 7 | `myagent/tools/file_tools.py` | 新建 | FileReadTool + FileWriteTool |
| 8 | `myagent/tools/secrets.py` | 新建 | SecretManager 统一凭据管理 |
| 9 | `myagent/safety/__init__.py` | 新建 | 安全系统包初始化 |
| 10 | `myagent/safety/base.py` | 新建 | PolicyDecision + SafetyContext + GuardResult + BaseRule |
| 11 | `myagent/safety/cli_fence.py` | 新建 | CLI 安全围栏（白/黑名单 + 路径） |
| 12 | `myagent/safety/content_rules.py` | 新建 | 输入/输出内容安全规则 |
| 13 | `myagent/safety/policy.py` | 新建 | PolicyEngine 策略引擎 |
| 14 | `myagent/safety/guard.py` | 新建 | SafetyGuard 安全责任链编排 |
| 15 | `myagent/safety/rules/__init__.py` | 新建 | 自定义规则插件目录 |
| 16 | `myagent/core/hitl.py` | 新建 | HITLController + CLIHITLController |
| 17 | `myagent/vision/__init__.py` | 新建 | 多模态图像处理包 |
| 18 | `myagent/vision/image_handler.py` | 新建 | ImageHandler 图像处理器 |

### 修改文件清单

| 序号 | 文件路径 | 修改内容 |
|------|---------|---------|
| 1 | `myagent/tools/executor.py` | 新增 SafetyGuard 前置检查 + SecretManager 注入 + HITL 回调 |
| 2 | `myagent/core/parser.py` | 追加 StructuredOutputParser 类 |
| 3 | `myagent/observability/events.py` | 追加 SafetyEvent 数据模型 |
| 4 | `myagent/observability/hook.py` | AuditHook 新增 on_safety_blocked + on_provider_failover 覆盖 |
| 5 | `myagent/interfaces/cli/main.py` | 集成 Safety/HITL/Sandbox/Image/工具注册 |
| 6 | `config.yaml` | 追加 safety + secrets 配置块 |

---

## 八、Phase 2 测试计划

### 单元测试

| 模块 | 测试文件 | 关键测试用例 |
|------|---------|------------|
| `tools/sandbox/subprocess_sandbox.py` | `test_sandbox.py` | 命令执行成功/失败、超时 kill、输出截断、环境变量隔离 |
| `tools/cli_tool.py` | `test_cli_tool.py` | CLITool 正常执行、错误处理、超时处理 |
| `tools/file_tools.py` | `test_file_tools.py` | 文件读取/写入/追加、路径安全检查、不存在文件处理 |
| `tools/secrets.py` | `test_secrets.py` | 环境变量解析、凭据注入、参数脱敏 |
| `safety/cli_fence.py` | `test_cli_fence.py` | 白名单通过/拦截、黑名单模式匹配、路径拒绝 |
| `safety/policy.py` | `test_policy.py` | ALLOW/DENY/REQUIRE_HITL 决策、条件匹配、默认策略 |
| `safety/guard.py` | `test_guard.py` | 责任链串行执行、PolicyEngine+Rule 组合、短路返回 |
| `safety/content_rules.py` | `test_content_rules.py` | 输入注入检测、输出密钥泄露检测 |
| `core/parser.py` | `test_parser.py` | JSON 代码块提取、纯 JSON 解析、自定义解析器注册 |
| `vision/image_handler.py` | `test_image_handler.py` | 本地文件/URL/bytes 处理、格式检测、大小限制、不支持 vision 降级 |
| `tools/executor.py` | `test_executor_phase2.py` | Safety -> Idempotency -> Execute 全流程、DENY/HITL/REWRITE 分支 |

### 集成测试

```python
# tests/test_integration_phase2.py

async def test_cli_tool_in_sandbox():
    """测试 CLITool 在 SubprocessSandbox 中正常执行。"""

async def test_dangerous_command_blocked():
    """测试危险命令被 CLIFence + PolicyEngine 拦截。"""

async def test_hitl_approval_flow():
    """测试 HITL 审批流程（模拟用户输入）。"""

async def test_file_read_write_with_safety():
    """测试文件工具的安全路径检查。"""

async def test_image_input_multimodal():
    """测试多模态图像输入处理（需要 vision 模型）。"""

async def test_safety_audit_events():
    """验证 SafetyEvent 正确记录到审计日志。"""

async def test_secret_redaction_in_audit():
    """测试密钥在审计日志中被正确脱敏。"""
```

---

## 九、开发批次建议

建议按以下顺序分批提交代码：

| 批次 | 文件 | 预计工作量 |
|------|------|----------|
| **第 1 批** | `config/safety_rules.yaml` + `safety/base.py` + `safety/cli_fence.py` + `safety/content_rules.py` | 安全基础设施（~200 行） |
| **第 2 批** | `safety/policy.py` + `safety/guard.py` + `safety/__init__.py` + `safety/rules/__init__.py` | 策略引擎（~200 行） |
| **第 3 批** | `tools/sandbox/base.py` + `tools/sandbox/subprocess_sandbox.py` + `tools/sandbox/docker_sandbox.py` | 沙盒系统（~200 行） |
| **第 4 批** | `tools/cli_tool.py` + `tools/file_tools.py` + `tools/secrets.py` | 工具实现（~300 行） |
| **第 5 批** | `core/hitl.py` + `core/parser.py`（追加）+ `tools/executor.py`（修改） | HITL + 解析器 + 执行器增强（~300 行） |
| **第 6 批** | `vision/image_handler.py` + `vision/__init__.py` | 多模态处理（~150 行） |
| **第 7 批** | `observability/events.py`（追加）+ `observability/hook.py`（追加）| 审计扩展（~60 行） |
| **第 8 批** | `interfaces/cli/main.py`（修改）+ `config.yaml`（修改）| CLI 集成 + 配置更新（~200 行） |
| **第 9 批** | 全部测试文件 | 测试覆盖 |

---

## 十、V3 特性在 Phase 2 中的落地清单

| V3 特性 | Phase 2 落地状态 | 实现要点 |
|---------|-------------|---------| 
| **PolicyEngine (ALLOW/DENY/HITL/REWRITE)** | ✅ 完整实现 | `safety/policy.py` 四态决策 + `safety/guard.py` 责任链编排 |
| **CLIFence 安全围栏** | ✅ 完整实现 | `safety/cli_fence.py` 三层防御（白名单+黑名单+路径） |
| **HITL Controller** | ✅ CLI 实现 | `core/hitl.py` CLI 同步审批，预留 WebSocket 异步接口 |
| **SecretManager** | ✅ 完整实现 | `tools/secrets.py` 凭据获取+自动脱敏注册 |
| **SubprocessSandbox** | ✅ 完整实现 | `tools/sandbox/subprocess_sandbox.py` ulimit+超时+输出截断 |
| **DockerSandbox** | ⏳ 预留骨架 | `tools/sandbox/docker_sandbox.py` NotImplementedError |
| **FileReadTool / FileWriteTool** | ✅ 完整实现 | `tools/file_tools.py` 含路径安全检查 |
| **CLITool** | ✅ 完整实现 | `tools/cli_tool.py` 集成沙盒执行 |
| **ImageHandler** | ✅ 完整实现 | `vision/image_handler.py` 多格式输入+Provider 适配 |
| **StructuredOutputParser** | ✅ 完整实现 | `core/parser.py` JSON/代码块提取 |
| **SafetyEvent 审计** | ✅ 完整实现 | `observability/events.py` + `observability/hook.py` |
| **ProviderEvent (Failover) 审计** | ✅ 完整实现 | `observability/hook.py` AuditHook 扩展 |

---

## 十一、对编码 AI 的特别提示

1. **Phase 1 代码不动**：除了 `executor.py`、`parser.py`、`events.py`、`hook.py`、`cli/main.py` 需要增量修改外，Phase 1 已有代码**只追加不删改**。
2. **Safety 前置不后置**：安全检查必须在 `ToolExecutor.execute()` 的最前面执行，在 IdempotencyCache 之前。如果被 DENY 的调用进了幂等缓存，后续重试会永远返回错误结果。
3. **现有 Agent 构造方式**：Phase 1 的 `Agent` 类使用 `provider_router`、`context` 等作为构造参数（不是 `AgentConfig`）。Phase 2 的集成代码要针对这个接口编写，不要按 Phase 1 方案中的 `AgentConfig` 驱动方式。
4. **HITL 要同步阻塞 CLI**：`CLIHITLController.request_approval()` 使用 `console.input()` 等待用户输入，这在 asyncio 中是阻塞调用。建议使用 `asyncio.get_event_loop().run_in_executor(None, console.input, prompt)` 包装为非阻塞。
5. **不引入新依赖**：Phase 2 所有新文件只使用 Python stdlib + Phase 1 已有依赖。沙盒用 `asyncio.subprocess`，不引入额外库。
6. **路径一致性**：所有文件路径使用 `Path` 对象处理，不用裸字符串拼接。确保跨平台兼容。
7. **测试先行**：每个新模块完成后先跑单元测试。Safety 模块尤其需要覆盖边界 case（空命令、Unicode 命令、路径遍历攻击等）。

祝编码顺利！
