# MyAgent Phase 1 编码实施指南（完整版）

> **基准文档**：`实施方案V3.md` — 全自研 Python Agent 框架架构设计方案（V3版）
> **范围**：Phase 1 聚焦于构建**可运行的核心骨架**——从配置加载、LLM 调用、状态机循环到 CLI 交互、审计日志的完整链路必须跑通。
> **不含**：沙盒安全、文档处理、SubAgent递归、Skill系统、评测引擎（Phase 2–5）。

---

## 一、Phase 1 交付目标（验收标准）

完成后应能运行以下端到端场景：

```bash
# 1. 基本对话
myagent chat "你好，请介绍一下自己"
# → Rich 流式输出，JSONL 审计日志落盘

# 2. 工具调用（使用内置的 echo 测试工具）
myagent chat "请使用 echo 工具输出 Hello World"
# → 工具调用 → 幂等缓存 → 结果返回 → 继续推理

# 3. Provider Failover
# 配置 primary=anthropic, fallback=openai，关闭 primary API Key
# → 自动 failover 到 openai，UI 显示切换提示

# 4. 断点恢复
# 在工具执行期间 Ctrl+C，重新运行 myagent resume <session_id>
# → 从 StateStore 恢复，跳过已完成的工具调用（幂等缓存命中）
```

---

## 二、开发顺序与模块依赖图

严格按照**自底向上**的依赖拓扑排序开发。每个编号代表一个可独立提交、可独立测试的最小工作单元。

```
                              ┌──────────────┐
                              │  ⑧ CLI 入口   │
                              │ interfaces/  │
                              └──────┬───────┘
                                     │ depends on
                              ┌──────┴───────┐
                              │  ⑦ Agent 门面 │
                              │  core/agent  │
                              └──────┬───────┘
                                     │ assembles
                   ┌─────────────────┼────────────────────┐
                   │                 │                     │
            ┌──────┴───────┐  ┌──────┴───────┐  ┌─────────┴──────────┐
            │  ⑥ AgentLoop │  │ ⑥ Hooks体系  │  │ ⑥ 审计系统(Audit)  │
            │  core/loop   │  │ core/hook     │  │ observability/     │
            └──────┬───────┘  └──────────────┘  └────────────────────┘
                   │ depends on
       ┌───────────┼──────────────┐
       │           │              │
┌──────┴──────┐ ┌──┴───────┐ ┌───┴──────────┐
│ ④ Provider  │ │ ④ Context│ │ ⑤ Tool系统   │
│  providers/ │ │ context/ │ │  tools/      │
└──────┬──────┘ └──┬───────┘ └───┬──────────┘
       │           │             │
       └───────────┼─────────────┘
                   │ depends on
            ┌──────┴───────┐
            │ ③ 配置层     │
            │ utils/config │
            └──────┬───────┘
                   │ depends on
       ┌───────────┼──────────┐
       │           │          │
┌──────┴──────┐ ┌──┴───────┐ ┌┴──────────────┐
│ ② retry     │ │ ② logging│ │ ② timeout     │
│ utils/      │ │ utils/   │ │ utils/        │
└─────────────┘ └──────────┘ └───────────────┘
                   │
            ┌──────┴───────┐
            │ ① pyproject  │
            │   .toml      │
            └──────────────┘
```

---

## 三、逐文件编码规范

### ① pyproject.toml — 项目基石

```toml
[project]
name = "myagent"
version = "0.1.0"
description = "全自研生产级异步 Python Agent 框架"
requires-python = ">=3.12"

dependencies = [
    # LLM Provider
    "openai>=1.0",
    "anthropic>=0.40",
    "httpx>=0.27",

    # 数据模型
    "pydantic>=2.0",
    "pydantic-settings>=2.0",

    # CLI 界面
    "rich>=13.0",
    "click>=8.0",

    # WebSocket（Phase 1 仅创建 lock.py 占位，不启动 server）
    "websockets>=12.0",

    # 配置 & 应用日志
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
    "picologging>=0.9",

    # 审计日志
    "aiofiles>=23.0",
    "aiosqlite>=0.20",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "pytest-cov", "ruff"]
postgres = ["asyncpg>=0.29"]

[project.scripts]
myagent = "myagent.interfaces.cli.main:cli"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
target-version = "py312"
line-length = 120
```

> **关键约束**：严禁引入 LangChain / LlamaIndex / Pandas / Datasets / Redis 等过重依赖。所有持久化一律走 SQLite。

---

### ② utils/ — 基础工具层

#### `utils/logging.py` — picologging 高性能日志

```python
"""
应用运行日志（区别于审计日志）。
使用 picologging 替代 stdlib logging，API 完全兼容，性能提升 4-10x。
"""
import picologging as logging
from picologging import StreamHandler, Formatter

_LOG_FORMAT = "%(asctime)s | %(name)-24s | %(levelname)-7s | %(message)s"
_initialized = False

def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """初始化全局日志配置。仅在首次调用时生效。"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console_handler = StreamHandler()
    console_handler.setFormatter(Formatter(_LOG_FORMAT))
    root.addHandler(console_handler)

    if log_file:
        from picologging import FileHandler
        fh = FileHandler(log_file)
        fh.setFormatter(Formatter(_LOG_FORMAT))
        root.addHandler(fh)

def get_logger(name: str) -> logging.Logger:
    """获取命名 Logger。"""
    return logging.getLogger(name)
```

#### `utils/retry.py` — 异步重试装饰器

```python
"""指数退避 + 抖动的异步重试装饰器。"""
import asyncio
import functools
import random
from typing import Type

from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class ExponentialBackoff:
    def __init__(self, base: float = 1.0, max_delay: float = 30.0, jitter: bool = True):
        self.base = base
        self.max_delay = max_delay
        self.jitter = jitter

    def delay(self, attempt: int) -> float:
        d = min(self.base * (2 ** attempt), self.max_delay)
        if self.jitter:
            d = d * (0.5 + random.random() * 0.5)
        return d

def async_retry(
    max_attempts: int = 3,
    backoff: ExponentialBackoff | None = None,
    retry_on: tuple[Type[Exception], ...] = (Exception,),
):
    """
    异步重试装饰器。
    仅对 retry_on 中指定的异常类型重试。
    """
    if backoff is None:
        backoff = ExponentialBackoff()

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except retry_on as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        delay = backoff.delay(attempt)
                        logger.warning(
                            f"Retry {attempt+1}/{max_attempts} for {func.__name__}: {e}, "
                            f"waiting {delay:.1f}s"
                        )
                        await asyncio.sleep(delay)
            raise last_exc
        return wrapper
    return decorator
```

#### `utils/timeout.py` — 超时管理器

```python
"""统一的超时管理，包装 asyncio.wait_for。"""
import asyncio
from dataclasses import dataclass

@dataclass
class TimeoutConfig:
    """可配置的超时参数集合。"""
    provider_first_token_s: float = 15.0
    tool_execution_s: float = 30.0
    cli_command_s: float = 60.0
    subagent_total_s: float = 300.0
    agent_turn_s: float = 600.0

class TimeoutError(Exception):
    """Agent 框架内部超时异常（区分 asyncio.TimeoutError）。"""
    def __init__(self, operation: str, timeout: float):
        self.operation = operation
        self.timeout = timeout
        super().__init__(f"Operation '{operation}' timed out after {timeout}s")

async def with_timeout(coro, timeout: float, operation: str = "unknown"):
    """包装 asyncio.wait_for，抛出自定义 TimeoutError。"""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(operation, timeout)
```

#### `utils/config.py` — YAML + 环境变量配置加载

```python
"""
配置加载：YAML 文件 + 环境变量覆盖。
环境变量用 ${VAR_NAME} 语法在 YAML 中引用。
"""
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

_ENV_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')

def _resolve_env_vars(value: Any) -> Any:
    """递归解析 YAML 中的 ${ENV_VAR} 引用。"""
    if isinstance(value, str):
        def replacer(match):
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))
        return _ENV_VAR_PATTERN.sub(replacer, value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value

def load_yaml_config(path: str | Path) -> dict:
    """加载 YAML 配置文件并解析环境变量。"""
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return _resolve_env_vars(raw)

# ── Pydantic-Settings 结构化配置模型 ──

class ProviderConfig(BaseModel):
    name: str
    type: str                          # "openai" | "anthropic"
    model: str
    priority: int = 1
    api_key: str = ""
    api_base: str | None = None        # 自定义 endpoint

class FailoverConfig(BaseModel):
    strategy: str = "priority"         # priority | round_robin | latency
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_recovery_seconds: int = 60

class AuditConfig(BaseModel):
    enabled: bool = True
    level: str = "standard"
    environment: str = "dev"
    queue_size: int = 2000
    jsonl_log_dir: str = "./audit_logs"
    jsonl_retention_days: int = 90

class AgentConfig(BaseSettings):
    """Agent 全局配置，支持 YAML 文件加载和环境变量覆盖。"""
    providers: list[ProviderConfig] = Field(default_factory=list)
    failover: FailoverConfig = Field(default_factory=FailoverConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    max_iterations: int = 25
    max_tokens_budget: int = 100000
    tool_result_max_chars: int = 4000

    model_config = {"env_prefix": "MYAGENT_"}
```

---

### ③ context/ — 上下文与状态持久化

#### `context/message.py` — Pydantic 数据模型

```python
"""
核心数据模型：Message、ContentBlock、ToolCall。
所有内部消息流转必须使用此模型，不可裸传 dict。
"""
from datetime import datetime, timezone
from typing import Literal, Any
from pydantic import BaseModel, Field
from uuid import uuid4

class ContentBlock(BaseModel):
    """支持文本和多模态内容。"""
    type: Literal["text", "image_url", "image_base64"]
    text: str | None = None
    url: str | None = None
    base64_data: str | None = None
    media_type: str | None = None  # image/jpeg, image/png 等

class ToolCall(BaseModel):
    """LLM 发出的工具调用请求。"""
    id: str = Field(default_factory=lambda: f"tc_{uuid4().hex[:12]}")
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

class ToolResult(BaseModel):
    """工具执行结果。"""
    tool_call_id: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

class Message(BaseModel):
    """统一消息格式。"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentBlock]
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None  # role="tool" 时必填
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    token_estimate: int | None = None

    def to_openai_dict(self) -> dict:
        """转为 OpenAI API 消息格式。"""
        msg: dict[str, Any] = {"role": self.role}
        if isinstance(self.content, str):
            msg["content"] = self.content
        else:
            msg["content"] = [block.model_dump(exclude_none=True) for block in self.content]
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        return msg

    def to_anthropic_dict(self) -> dict:
        """转为 Anthropic API 消息格式。"""
        # Anthropic 格式差异较大，需要特殊处理
        msg: dict[str, Any] = {"role": self.role}
        if isinstance(self.content, str):
            msg["content"] = self.content
        else:
            blocks = []
            for block in self.content:
                if block.type == "text":
                    blocks.append({"type": "text", "text": block.text})
                elif block.type == "image_base64":
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block.media_type,
                            "data": block.base64_data,
                        }
                    })
            msg["content"] = blocks
        return msg
```

#### `context/state.py` — StateStore 持久化（V3 核心）

```python
"""
StateStore：会话状态持久化。
V3 核心变更 —— 显式状态机的持久化保障，使得断线恢复成为可能。

表结构：
  - sessions: session_id, agent_state(枚举), metadata(JSON), updated_at
  - messages: session_id, seq, message_json, created_at
  - pending_tool_calls: session_id, tool_call_id, tool_call_json, status, result_json
"""
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import aiosqlite

from myagent.context.message import Message, ToolCall, ToolResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class AgentState(str, Enum):
    """
    V3 显式状态机的核心枚举。
    AgentLoop 的每一次状态变迁都必须先持久化到 StateStore，
    再执行实际操作——这是断电恢复的基石。
    """
    IDLE = "idle"                    # 等待输入
    RUNNING = "running"              # 正在调用 LLM
    WAITING_TOOL = "waiting_tool"    # LLM 已返回 tool_calls，等待执行
    WAITING_HITL = "waiting_hitl"    # 等待人工审批（Phase 2 实现，Phase 1 预留）
    ERROR = "error"                  # 发生错误
    FINISHED = "finished"            # 当前 Turn 完成

class StateStore(ABC):
    """状态持久化抽象接口。"""

    @abstractmethod
    async def save_state(self, session_id: str, state: AgentState, metadata: dict | None = None) -> None: ...

    @abstractmethod
    async def load_state(self, session_id: str) -> tuple[AgentState, dict]:
        """返回 (当前状态, metadata)。不存在时返回 (IDLE, {})。"""
        ...

    @abstractmethod
    async def save_messages(self, session_id: str, messages: list[Message]) -> None: ...

    @abstractmethod
    async def load_messages(self, session_id: str) -> list[Message]: ...

    @abstractmethod
    async def save_pending_tool_calls(self, session_id: str, tool_calls: list[ToolCall]) -> None: ...

    @abstractmethod
    async def load_pending_tool_calls(self, session_id: str) -> list[ToolCall]: ...

    @abstractmethod
    async def save_tool_result(self, session_id: str, tool_call_id: str, result: ToolResult) -> None: ...

    @abstractmethod
    async def load_tool_results(self, session_id: str) -> dict[str, ToolResult]:
        """返回 {tool_call_id: ToolResult}，用于幂等缓存检查。"""
        ...

    @abstractmethod
    async def clear_session(self, session_id: str) -> None: ...

class SQLiteStateStore(StateStore):
    """
    基于 aiosqlite 的 StateStore 实现。
    Phase 1 的唯一持久化后端，零外部依赖。
    """

    def __init__(self, db_path: str | Path = "myagent_state.db"):
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """创建表结构。Agent 启动时调用。"""
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                agent_state TEXT NOT NULL DEFAULT 'idle',
                metadata TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                message_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, seq)
            );
            CREATE TABLE IF NOT EXISTS pending_tool_calls (
                session_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_call_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                result_json TEXT,
                PRIMARY KEY(session_id, tool_call_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        """)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def save_state(self, session_id: str, state: AgentState, metadata: dict | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        await self._db.execute(
            """INSERT INTO sessions (session_id, agent_state, metadata, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 agent_state = excluded.agent_state,
                 metadata = excluded.metadata,
                 updated_at = excluded.updated_at""",
            (session_id, state.value, meta_json, now),
        )
        await self._db.commit()
        logger.debug(f"State saved: session={session_id}, state={state.value}")

    async def load_state(self, session_id: str) -> tuple[AgentState, dict]:
        async with self._db.execute(
            "SELECT agent_state, metadata FROM sessions WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return AgentState.IDLE, {}
            return AgentState(row[0]), json.loads(row[1])

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # 全量替换策略：先删后插。短会话场景足够高效。
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        for seq, msg in enumerate(messages):
            msg_json = msg.model_dump_json()
            await self._db.execute(
                "INSERT INTO messages (session_id, seq, message_json, created_at) VALUES (?, ?, ?, ?)",
                (session_id, seq, msg_json, now),
            )
        await self._db.commit()

    async def load_messages(self, session_id: str) -> list[Message]:
        async with self._db.execute(
            "SELECT message_json FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [Message.model_validate_json(row[0]) for row in rows]

    async def save_pending_tool_calls(self, session_id: str, tool_calls: list[ToolCall]) -> None:
        for tc in tool_calls:
            await self._db.execute(
                """INSERT OR REPLACE INTO pending_tool_calls
                   (session_id, tool_call_id, tool_call_json, status)
                   VALUES (?, ?, ?, 'pending')""",
                (session_id, tc.id, tc.model_dump_json()),
            )
        await self._db.commit()

    async def load_pending_tool_calls(self, session_id: str) -> list[ToolCall]:
        async with self._db.execute(
            "SELECT tool_call_json FROM pending_tool_calls WHERE session_id = ? AND status = 'pending'",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [ToolCall.model_validate_json(row[0]) for row in rows]

    async def save_tool_result(self, session_id: str, tool_call_id: str, result: ToolResult) -> None:
        await self._db.execute(
            """UPDATE pending_tool_calls SET status = 'completed', result_json = ?
               WHERE session_id = ? AND tool_call_id = ?""",
            (result.model_dump_json(), session_id, tool_call_id),
        )
        await self._db.commit()

    async def load_tool_results(self, session_id: str) -> dict[str, ToolResult]:
        async with self._db.execute(
            "SELECT tool_call_id, result_json FROM pending_tool_calls WHERE session_id = ? AND status = 'completed'",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: ToolResult.model_validate_json(row[1]) for row in rows}

    async def clear_session(self, session_id: str) -> None:
        await self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM pending_tool_calls WHERE session_id = ?", (session_id,))
        await self._db.commit()
```

#### `context/manager.py` — ContextManager（三层控制法）

```python
"""
ContextManager：管理消息历史，预留 TokenBudget 三层控制（V3 核心）。
三层结构：[System Prompt] + [Summary Memory] + [Recent N 轮原话]
Phase 1 实现基础的消息管理 + Token 估算 + 工具结果强截断。
"""
from myagent.context.message import Message, ContentBlock, ToolResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class ContextManager:
    def __init__(
        self,
        max_tokens_budget: int = 100000,
        tool_result_max_chars: int = 4000,
        recent_turns: int = 20,
    ):
        self._messages: list[Message] = []
        self._system_prompt: str | None = None
        self._max_tokens_budget = max_tokens_budget
        self._tool_result_max_chars = tool_result_max_chars
        self._recent_turns = recent_turns

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    def set_system(self, prompt: str) -> None:
        """设置/替换 System Prompt。"""
        self._system_prompt = prompt
        # 确保 system 消息始终在首位
        self._messages = [m for m in self._messages if m.role != "system"]
        self._messages.insert(0, Message(role="system", content=prompt))

    def add_user_message(self, content: str | list[ContentBlock]) -> None:
        self._messages.append(Message(role="user", content=content))

    def add_assistant_message(self, content: str, tool_calls=None) -> None:
        self._messages.append(Message(
            role="assistant", content=content, tool_calls=tool_calls
        ))

    def add_tool_result(self, tool_call_id: str, result: ToolResult) -> None:
        """
        添加工具结果。V3 关键：强制截断超长工具输出，防止上下文爆窗。
        """
        content = result.content
        if len(content) > self._tool_result_max_chars:
            content = content[:self._tool_result_max_chars] + f"\n...[截断：原文 {len(result.content)} 字符，已截断至 {self._tool_result_max_chars} 字符]"
            logger.warning(
                f"Tool result truncated: {result.tool_call_id}, "
                f"{len(result.content)} -> {self._tool_result_max_chars} chars"
            )
        self._messages.append(Message(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
        ))

    def get_messages(self) -> list[Message]:
        """
        返回发送给 LLM 的消息列表。
        Phase 1：直接返回全部消息。
        TODO(Phase 5)：实现三层结构裁剪 —— System + Summary + Recent
        """
        return list(self._messages)

    def estimate_tokens(self) -> int:
        """
        粗略估算总 Token 数（1 中文字 ≈ 2 token，1 英文单词 ≈ 1 token）。
        """
        total = 0
        for msg in self._messages:
            text = msg.content if isinstance(msg.content, str) else " ".join(
                b.text or "" for b in msg.content if b.text
            )
            # 粗略估算：中文按字符数×2，英文按词数
            total += len(text)  # 简化估算
        return total

    def is_over_budget(self) -> bool:
        return self.estimate_tokens() > self._max_tokens_budget

    def restore_from(self, messages: list[Message]) -> None:
        """从 StateStore 恢复消息历史。"""
        self._messages = messages
        # 重新提取 system prompt
        for msg in self._messages:
            if msg.role == "system":
                self._system_prompt = msg.content if isinstance(msg.content, str) else None
                break
```

---

### ④ providers/ — LLM Provider 层

#### `providers/base.py` — 统一流事件模型

```python
"""
统一流事件模型 StreamEvent + BaseProvider 抽象基类。
所有 Provider 必须将原生 SDK 事件转换为 StreamEvent 对上层输出。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Any

@dataclass
class StreamEvent:
    """所有 Provider 输出的统一事件类型。"""
    type: Literal[
        "text_delta",       # 文本增量片段
        "tool_call_start",  # 工具调用开始（含 tool_name, call_id）
        "tool_call_delta",  # 工具参数 JSON 增量
        "tool_call_end",    # 工具调用参数完整
        "message_end",      # 本轮消息结束
        "error",            # 错误事件
    ]
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args_delta: str | None = None
    tool_args: dict | None = None
    stop_reason: str | None = None
    error: Exception | None = None
    usage: dict[str, int] = field(default_factory=dict)  # {"input_tokens": N, "output_tokens": N}

@dataclass
class ProviderCapabilities:
    """Provider / 模型的能力描述。"""
    supports_vision: bool = False
    supports_tool_calls: bool = True
    supports_streaming: bool = True
    max_image_size_mb: int = 20

class ProviderError(Exception):
    """Provider 层通用异常基类。"""
    pass

class ProviderRateLimitError(ProviderError):
    """速率限制。"""
    pass

class ProviderTimeoutError(ProviderError):
    """超时。"""
    pass

class ProviderAuthError(ProviderError):
    """认证失败。"""
    pass

class AllProvidersFailedError(ProviderError):
    """所有 Provider 均失败。"""
    def __init__(self, errors: list[tuple[str, Exception]]):
        self.errors = errors
        details = "; ".join(f"{name}: {err}" for name, err in errors)
        super().__init__(f"All providers failed: {details}")

class BaseProvider(ABC):
    """LLM Provider 抽象基类。"""

    def __init__(self, name: str, model: str, api_key: str, api_base: str | None = None):
        self.name = name
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.capabilities: ProviderCapabilities = ProviderCapabilities()

    @abstractmethod
    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 LLM，yield StreamEvent 序列。"""
        ...

    @abstractmethod
    def format_messages(self, messages: list) -> list[dict]:
        """将内部 Message 列表转为该 Provider 的 API 格式。"""
        ...

    @abstractmethod
    def format_tools(self, tools: list) -> list[dict]:
        """将内部 Tool 列表转为该 Provider 的 tools 参数格式。"""
        ...
```

#### `providers/capability.py` — 模型能力检测

```python
"""
CapabilityDetector：基于模型名探测 vision/tool_call 等能力。
可通过配置覆盖内置的已知模型列表。
"""
import fnmatch
from myagent.providers.base import ProviderCapabilities

class CapabilityDetector:
    _KNOWN_VISION_MODELS = [
        "gpt-4o", "gpt-4o-*", "gpt-4-turbo", "gpt-4-vision-preview",
        "claude-3-*", "claude-opus-*", "claude-sonnet-*",
        "gemini-*",
    ]

    _KNOWN_NO_TOOL_MODELS = [
        "o1-preview", "o1-mini",  # reasoning models may not support tools
    ]

    def detect(self, model: str, provider_type: str) -> ProviderCapabilities:
        supports_vision = any(fnmatch.fnmatch(model, pat) for pat in self._KNOWN_VISION_MODELS)
        supports_tools = not any(fnmatch.fnmatch(model, pat) for pat in self._KNOWN_NO_TOOL_MODELS)
        return ProviderCapabilities(
            supports_vision=supports_vision,
            supports_tool_calls=supports_tools,
        )
```

#### `providers/openai_provider.py` — OpenAI 流式

```python
"""
OpenAI Provider 实现。
将 openai SDK 的 ChatCompletionChunk 流转换为统一的 StreamEvent。
"""
import json
from typing import AsyncIterator

from openai import AsyncOpenAI, RateLimitError, APITimeoutError, AuthenticationError

from myagent.providers.base import (
    BaseProvider, StreamEvent, ProviderCapabilities,
    ProviderRateLimitError, ProviderTimeoutError, ProviderAuthError,
)
from myagent.providers.capability import CapabilityDetector
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class OpenAIProvider(BaseProvider):
    def __init__(self, name: str, model: str, api_key: str, api_base: str | None = None):
        super().__init__(name, model, api_key, api_base)
        self._client = AsyncOpenAI(api_key=api_key, base_url=api_base)
        self.capabilities = CapabilityDetector().detect(model, "openai")

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamEvent]:
        try:
            params = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if tools:
                params["tools"] = tools

            response = await self._client.chat.completions.create(**params)

            # 工具调用状态追踪
            _tool_calls: dict[int, dict] = {}  # index -> {id, name, args_buffer}
            _usage = {}

            async for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                finish_reason = chunk.choices[0].finish_reason if chunk.choices else None

                # Usage 信息（在最后一个 chunk 中）
                if chunk.usage:
                    _usage = {
                        "input_tokens": chunk.usage.prompt_tokens,
                        "output_tokens": chunk.usage.completion_tokens,
                    }

                if delta is None:
                    continue

                # 文本增量
                if delta.content:
                    yield StreamEvent(type="text_delta", text=delta.content)

                # 工具调用
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in _tool_calls:
                            _tool_calls[idx] = {
                                "id": tc_delta.id or "",
                                "name": "",
                                "args_buffer": "",
                            }

                        tc = _tool_calls[idx]
                        if tc_delta.id:
                            tc["id"] = tc_delta.id
                        if tc_delta.function and tc_delta.function.name:
                            tc["name"] = tc_delta.function.name
                            yield StreamEvent(
                                type="tool_call_start",
                                tool_name=tc["name"],
                                tool_call_id=tc["id"],
                            )
                        if tc_delta.function and tc_delta.function.arguments:
                            tc["args_buffer"] += tc_delta.function.arguments
                            yield StreamEvent(
                                type="tool_call_delta",
                                tool_call_id=tc["id"],
                                tool_args_delta=tc_delta.function.arguments,
                            )

                # 结束
                if finish_reason:
                    # 先发送所有工具调用的 end 事件
                    for tc in _tool_calls.values():
                        try:
                            args = json.loads(tc["args_buffer"]) if tc["args_buffer"] else {}
                        except json.JSONDecodeError:
                            args = {"_raw": tc["args_buffer"]}
                            logger.warning(f"Failed to parse tool args for {tc['name']}: {tc['args_buffer'][:100]}")
                        yield StreamEvent(
                            type="tool_call_end",
                            tool_name=tc["name"],
                            tool_call_id=tc["id"],
                            tool_args=args,
                        )
                    yield StreamEvent(
                        type="message_end",
                        stop_reason=finish_reason,
                        usage=_usage,
                    )

        except RateLimitError as e:
            raise ProviderRateLimitError(f"OpenAI rate limit: {e}") from e
        except APITimeoutError as e:
            raise ProviderTimeoutError(f"OpenAI timeout: {e}") from e
        except AuthenticationError as e:
            raise ProviderAuthError(f"OpenAI auth failed: {e}") from e

    def format_messages(self, messages: list) -> list[dict]:
        return [msg.to_openai_dict() for msg in messages]

    def format_tools(self, tools: list) -> list[dict]:
        return [tool.to_openai_schema() for tool in tools]
```

#### `providers/anthropic_provider.py` — Anthropic 流式

```python
"""
Anthropic Provider 实现。
将 anthropic SDK 的 streaming events 转换为统一的 StreamEvent。

Anthropic 的流式事件结构：
  message_start → content_block_start → content_block_delta → content_block_stop → message_delta → message_stop
"""
import json
from typing import AsyncIterator

from anthropic import AsyncAnthropic, RateLimitError, APITimeoutError, AuthenticationError

from myagent.providers.base import (
    BaseProvider, StreamEvent, ProviderCapabilities,
    ProviderRateLimitError, ProviderTimeoutError, ProviderAuthError,
)
from myagent.providers.capability import CapabilityDetector
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class AnthropicProvider(BaseProvider):
    def __init__(self, name: str, model: str, api_key: str, api_base: str | None = None):
        super().__init__(name, model, api_key, api_base)
        kwargs = {"api_key": api_key}
        if api_base:
            kwargs["base_url"] = api_base
        self._client = AsyncAnthropic(**kwargs)
        self.capabilities = CapabilityDetector().detect(model, "anthropic")

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamEvent]:
        try:
            # Anthropic 要求 system 作为独立参数
            system_prompt = None
            api_messages = []
            for msg in messages:
                if msg.get("role") == "system":
                    system_prompt = msg["content"]
                else:
                    api_messages.append(msg)

            params = {
                "model": self.model,
                "messages": api_messages,
                "max_tokens": kwargs.get("max_tokens", 4096),
                "stream": True,
            }
            if system_prompt:
                params["system"] = system_prompt
            if tools:
                params["tools"] = tools

            # 状态追踪
            _current_block_type: str | None = None
            _current_tool_id: str | None = None
            _current_tool_name: str | None = None
            _tool_args_buffer: str = ""
            _usage: dict = {}

            async with self._client.messages.stream(**params) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            _current_block_type = "tool_use"
                            _current_tool_id = block.id
                            _current_tool_name = block.name
                            _tool_args_buffer = ""
                            yield StreamEvent(
                                type="tool_call_start",
                                tool_name=block.name,
                                tool_call_id=block.id,
                            )
                        elif block.type == "text":
                            _current_block_type = "text"

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if _current_block_type == "text" and hasattr(delta, "text"):
                            yield StreamEvent(type="text_delta", text=delta.text)
                        elif _current_block_type == "tool_use" and hasattr(delta, "partial_json"):
                            _tool_args_buffer += delta.partial_json
                            yield StreamEvent(
                                type="tool_call_delta",
                                tool_call_id=_current_tool_id,
                                tool_args_delta=delta.partial_json,
                            )

                    elif event.type == "content_block_stop":
                        if _current_block_type == "tool_use":
                            try:
                                args = json.loads(_tool_args_buffer) if _tool_args_buffer else {}
                            except json.JSONDecodeError:
                                args = {"_raw": _tool_args_buffer}
                            yield StreamEvent(
                                type="tool_call_end",
                                tool_name=_current_tool_name,
                                tool_call_id=_current_tool_id,
                                tool_args=args,
                            )
                        _current_block_type = None

                    elif event.type == "message_delta":
                        stop_reason = getattr(event.delta, "stop_reason", None)
                        if hasattr(event, "usage"):
                            _usage["output_tokens"] = getattr(event.usage, "output_tokens", 0)

                    elif event.type == "message_start":
                        if hasattr(event.message, "usage"):
                            _usage["input_tokens"] = getattr(event.message.usage, "input_tokens", 0)

                    elif event.type == "message_stop":
                        yield StreamEvent(
                            type="message_end",
                            stop_reason=getattr(event, "stop_reason", "end_turn") or "end_turn",
                            usage=_usage,
                        )

        except RateLimitError as e:
            raise ProviderRateLimitError(f"Anthropic rate limit: {e}") from e
        except APITimeoutError as e:
            raise ProviderTimeoutError(f"Anthropic timeout: {e}") from e
        except AuthenticationError as e:
            raise ProviderAuthError(f"Anthropic auth failed: {e}") from e

    def format_messages(self, messages: list) -> list[dict]:
        return [msg.to_anthropic_dict() for msg in messages]

    def format_tools(self, tools: list) -> list[dict]:
        return [tool.to_anthropic_schema() for tool in tools]
```

#### `providers/router.py` — ProviderRouter + 熔断器

```python
"""
ProviderRouter：多路冗余 + Failover + 熔断器。
按优先级排序，遇到可恢复的错误时自动切换到下一个 Provider。
"""
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from myagent.providers.base import (
    BaseProvider, StreamEvent,
    ProviderRateLimitError, ProviderTimeoutError, AllProvidersFailedError,
)
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

@dataclass
class CircuitBreakerState:
    """单个 Provider 的熔断器状态。"""
    failure_count: int = 0
    last_failure_time: float = 0.0
    is_open: bool = False  # True = 熔断中，暂停使用

class ProviderRouter:
    """
    核心路由器。遍历 Provider 列表，遇到限流/超时自动 Failover。
    支持熔断器：连续失败 N 次后暂停该 Provider。
    """
    # 触发 Failover 的异常类型
    RETRIABLE_ERRORS = (ProviderRateLimitError, ProviderTimeoutError)

    def __init__(
        self,
        providers: list[BaseProvider],
        failure_threshold: int = 3,
        recovery_seconds: int = 60,
        on_failover: callable = None,  # Hook 回调：(from_name, to_name, reason) -> None
    ):
        self._providers = sorted(providers, key=lambda p: getattr(p, '_priority', 0))
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._breakers: dict[str, CircuitBreakerState] = {
            p.name: CircuitBreakerState() for p in providers
        }
        self._on_failover = on_failover

    def _is_available(self, provider: BaseProvider) -> bool:
        """检查 Provider 是否可用（未被熔断）。"""
        breaker = self._breakers[provider.name]
        if not breaker.is_open:
            return True
        # 检查恢复时间
        if time.monotonic() - breaker.last_failure_time > self._recovery_seconds:
            breaker.is_open = False
            breaker.failure_count = 0
            logger.info(f"Circuit breaker recovered for provider: {provider.name}")
            return True
        return False

    def _record_failure(self, provider: BaseProvider, error: Exception) -> None:
        breaker = self._breakers[provider.name]
        breaker.failure_count += 1
        breaker.last_failure_time = time.monotonic()
        if breaker.failure_count >= self._failure_threshold:
            breaker.is_open = True
            logger.warning(
                f"Circuit breaker OPEN for provider: {provider.name} "
                f"(failures={breaker.failure_count}, recovery={self._recovery_seconds}s)"
            )

    def _record_success(self, provider: BaseProvider) -> None:
        breaker = self._breakers[provider.name]
        breaker.failure_count = 0
        breaker.is_open = False

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamEvent]:
        """
        尝试按优先级依次调用 Provider。
        成功则 yield 事件流；遇到可重试异常则自动切换。
        """
        errors: list[tuple[str, Exception]] = []
        prev_provider_name: str | None = None

        for provider in self._providers:
            if not self._is_available(provider):
                logger.info(f"Skipping circuit-broken provider: {provider.name}")
                continue

            try:
                logger.info(f"Trying provider: {provider.name} (model={provider.model})")

                if prev_provider_name and self._on_failover:
                    await self._on_failover(prev_provider_name, provider.name, str(errors[-1][1]) if errors else "")

                async for event in provider.stream(messages, tools, **kwargs):
                    yield event

                # 成功完成，重置熔断计数
                self._record_success(provider)
                return

            except self.RETRIABLE_ERRORS as e:
                logger.warning(f"Provider {provider.name} failed: {e}, trying next...")
                self._record_failure(provider, e)
                errors.append((provider.name, e))
                prev_provider_name = provider.name
                continue

        raise AllProvidersFailedError(errors)

    @property
    def current_provider(self) -> BaseProvider | None:
        """返回当前最高优先级可用的 Provider。"""
        for p in self._providers:
            if self._is_available(p):
                return p
        return None
```

---

### ⑤ tools/ — 工具系统

#### `tools/base.py` — BaseTool + ToolResult

```python
"""
工具系统基础抽象。
所有工具必须继承 BaseTool 并实现 execute 方法。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ToolResult:
    """工具执行结果。"""
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

class BaseTool(ABC):
    """工具抽象基类。"""
    name: str = ""
    description: str = ""
    parameters_schema: dict = field(default_factory=dict)  # JSON Schema

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """执行工具。子类必须实现。"""
        ...

    def to_openai_schema(self) -> dict:
        """转为 OpenAI function calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            }
        }

    def to_anthropic_schema(self) -> dict:
        """转为 Anthropic tool_use 格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters_schema,
        }
```

#### `tools/registry.py` — ToolRegistry

```python
"""
ToolRegistry：工具注册中心。
支持按名称注册、查找、列举所有工具。
"""
from myagent.tools.base import BaseTool
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            logger.warning(f"Tool '{tool.name}' already registered, overwriting.")
        self._tools[tool.name] = tool
        logger.info(f"Tool registered: {tool.name}")

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def to_openai_schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def to_anthropic_schemas(self) -> list[dict]:
        return [t.to_anthropic_schema() for t in self._tools.values()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
```

#### `tools/executor.py` — ToolExecutor

```python
"""
ToolExecutor：工具执行引擎。
职责：
1. 从 ToolRegistry 按名称查找工具
2. 通过 IdempotencyCache 拦截重复调用（V3 核心）
3. 执行工具并返回结果
4. 超时控制（asyncio.wait_for）
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

class ToolNotFoundError(Exception):
    pass

class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        idempotency_cache: IdempotencyCache | None = None,
        default_timeout: float = 30.0,
    ):
        self._registry = registry
        self._cache = idempotency_cache
        self._default_timeout = default_timeout

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """
        执行单个工具调用。
        V3 幂等性：先查缓存，命中则直接返回，跳过实际执行。
        """
        tool = self._registry.get(tool_call.name)
        if tool is None:
            return ToolResult(
                content=f"Error: Tool '{tool_call.name}' not found. Available tools: {[t.name for t in self._registry.list_tools()]}",
                is_error=True,
            )

        # V3 幂等缓存检查
        if self._cache:
            cached = await self._cache.get(tool_call.id)
            if cached is not None:
                logger.info(f"Idempotency cache HIT for tool_call_id={tool_call.id}, skipping execution")
                return cached

        # 执行
        start_time = time.monotonic()
        try:
            result = await asyncio.wait_for(
                tool.execute(**tool_call.arguments),
                timeout=self._default_timeout,
            )
        except asyncio.TimeoutError:
            result = ToolResult(
                content=f"Error: Tool '{tool_call.name}' timed out after {self._default_timeout}s",
                is_error=True,
            )
        except Exception as e:
            logger.error(f"Tool '{tool_call.name}' raised exception: {e}", exc_info=True)
            result = ToolResult(
                content=f"Error executing tool '{tool_call.name}': {type(e).__name__}: {e}",
                is_error=True,
            )

        latency_ms = int((time.monotonic() - start_time) * 1000)
        result.metadata["latency_ms"] = latency_ms

        # V3 幂等缓存存储
        if self._cache:
            await self._cache.put(tool_call.id, result)

        return result

    async def execute_batch(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """并行执行多个工具调用。"""
        tasks = [self.execute(tc) for tc in tool_calls]
        return await asyncio.gather(*tasks)
```

#### `tools/idempotency.py` — IdempotencyCache（V3 核心）

```python
"""
IdempotencyCache：工具调用幂等缓存。
V3 核心机制 —— 绑定全局唯一 tool_call_id，
拦截因网络超时/断线恢复导致的重复执行。

实现方式：
- 内存 LRU 缓存 + 可选 SQLite 持久化（联动 StateStore）
- Phase 1 使用内存缓存，断线恢复时通过 StateStore.load_tool_results() 预热
"""
from collections import OrderedDict
from myagent.tools.base import ToolResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class IdempotencyCache:
    """
    基于 tool_call_id 的幂等缓存。
    内存 LRU，容量溢出时淘汰最旧的条目。
    """

    def __init__(self, max_size: int = 1000):
        self._cache: OrderedDict[str, ToolResult] = OrderedDict()
        self._max_size = max_size

    async def get(self, tool_call_id: str) -> ToolResult | None:
        """查询缓存。命中时返回 ToolResult，未命中返回 None。"""
        if tool_call_id in self._cache:
            self._cache.move_to_end(tool_call_id)
            return self._cache[tool_call_id]
        return None

    async def put(self, tool_call_id: str, result: ToolResult) -> None:
        """写入缓存。超出容量则淘汰最旧条目。"""
        self._cache[tool_call_id] = result
        self._cache.move_to_end(tool_call_id)
        while len(self._cache) > self._max_size:
            evicted_id, _ = self._cache.popitem(last=False)
            logger.debug(f"Idempotency cache evicted: {evicted_id}")

    async def has(self, tool_call_id: str) -> bool:
        return tool_call_id in self._cache

    def preload(self, results: dict[str, ToolResult]) -> None:
        """
        从 StateStore 预热缓存（断线恢复时使用）。
        """
        for call_id, result in results.items():
            self._cache[call_id] = result
        logger.info(f"Idempotency cache preloaded with {len(results)} entries")

    def clear(self) -> None:
        self._cache.clear()
```

---

### ⑥ core/ — Agent 核心（状态机 + Hook 体系）

#### `core/hook.py` — 完整生命周期钩子

此文件按 V3 方案逐字实现，包含：
- `HookContext` — 执行上下文快照（新增 `trace_id`, `span_id`）
- `AgentHook` — 所有生命周期钩子方法（no-op 默认实现）
- `CompositeHook` — 组合模式（AgentLoop 唯一持有对象）

```python
"""
核心 Hook 体系。
HookContext 新增 V3 的 trace_id / span_id 字段用于全链路追踪。
"""
from abc import ABC
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

@dataclass
class HookContext:
    """传递给所有 Hook 方法，携带当前 Agent 执行状态。"""
    session_id: str
    agent_id: str = "main"
    turn_id: str = field(default_factory=lambda: uuid4().hex[:12])
    iteration: int = 0
    model: str = ""
    provider: str = ""
    trace_id: str = ""          # V3 新增：全链路追踪 ID
    span_id: str = ""           # V3 新增：当前 span
    tool_calls: list = field(default_factory=list)
    tool_events: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        """生成可序列化的上下文快照。"""
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "iteration": self.iteration,
            "model": self.model,
            "provider": self.provider,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "usage": self.usage,
        }

class AgentHook(ABC):
    """
    Agent 生命周期钩子基类。
    所有方法均有默认空实现（no-op），子类只需覆盖感兴趣的钩子点。
    """
    def wants_streaming(self) -> bool:
        return False

    # ── Session ──
    async def on_session_start(self, ctx: HookContext) -> None: pass
    async def on_session_end(self, ctx: HookContext, *, final_content: str | None, exit_reason: str) -> None: pass

    # ── Turn ──
    async def on_turn_start(self, ctx: HookContext) -> None: pass
    async def on_turn_end(self, ctx: HookContext) -> None: pass

    # ── Iteration ──
    async def on_iteration_start(self, ctx: HookContext) -> None: pass
    async def on_iteration_end(self, ctx: HookContext) -> None: pass

    # ── Provider ──
    async def on_provider_call_start(self, ctx: HookContext) -> None: pass
    async def on_provider_call_end(self, ctx: HookContext, *, stop_reason: str, usage: dict) -> None: pass
    async def on_provider_failover(self, ctx: HookContext, *, from_provider: str, to_provider: str, reason: str) -> None: pass

    # ── Streaming ──
    async def on_stream_start(self, ctx: HookContext) -> None: pass
    async def on_stream(self, ctx: HookContext, delta: str) -> None: pass
    async def on_stream_end(self, ctx: HookContext, *, resuming: bool) -> None: pass

    # ── Tool ──
    async def before_execute_tools(self, ctx: HookContext) -> None: pass
    async def on_tool_start(self, ctx: HookContext, *, tool_name: str, args: dict, call_id: str) -> None: pass
    async def on_tool_end(self, ctx: HookContext, *, tool_name: str, result: Any, call_id: str, latency_ms: int) -> None: pass
    async def on_tool_error(self, ctx: HookContext, *, tool_name: str, error: Exception, call_id: str) -> None: pass
    async def after_execute_tools(self, ctx: HookContext) -> None: pass

    # ── Safety ──
    async def on_safety_blocked(self, ctx: HookContext, *, rule: str, reason: str, action: str) -> None: pass

    # ── SubAgent（Phase 3 使用，Phase 1 预留） ──
    async def on_subagent_start(self, ctx: HookContext, *, spec: Any, depth: int) -> None: pass
    async def on_subagent_end(self, ctx: HookContext, *, spec: Any, result: Any, depth: int) -> None: pass

    # ── Error ──
    async def on_error(self, ctx: HookContext, *, error: Exception) -> None: pass

    # ── 内容后处理（同步） ──
    def finalize_content(self, ctx: HookContext, content: str | None) -> str | None:
        return content


class CompositeHook(AgentHook):
    """组合多个 Hook 实例，AgentLoop 只持有一个 CompositeHook。"""

    def __init__(self, hooks: list[AgentHook] | None = None):
        self._hooks: list[AgentHook] = hooks or []

    def add(self, hook: AgentHook) -> None:
        self._hooks.append(hook)

    def wants_streaming(self) -> bool:
        return any(h.wants_streaming() for h in self._hooks)

    # ── 通用委托宏 ──
    async def _dispatch(self, method_name: str, *args, **kwargs):
        for h in self._hooks:
            method = getattr(h, method_name)
            await method(*args, **kwargs)

    async def _dispatch_streaming(self, method_name: str, *args, **kwargs):
        for h in self._hooks:
            if h.wants_streaming():
                method = getattr(h, method_name)
                await method(*args, **kwargs)

    # ── Session ──
    async def on_session_start(self, ctx): await self._dispatch("on_session_start", ctx)
    async def on_session_end(self, ctx, *, final_content, exit_reason):
        await self._dispatch("on_session_end", ctx, final_content=final_content, exit_reason=exit_reason)

    # ── Turn ──
    async def on_turn_start(self, ctx): await self._dispatch("on_turn_start", ctx)
    async def on_turn_end(self, ctx): await self._dispatch("on_turn_end", ctx)

    # ── Iteration ──
    async def on_iteration_start(self, ctx): await self._dispatch("on_iteration_start", ctx)
    async def on_iteration_end(self, ctx): await self._dispatch("on_iteration_end", ctx)

    # ── Provider ──
    async def on_provider_call_start(self, ctx): await self._dispatch("on_provider_call_start", ctx)
    async def on_provider_call_end(self, ctx, *, stop_reason, usage):
        await self._dispatch("on_provider_call_end", ctx, stop_reason=stop_reason, usage=usage)
    async def on_provider_failover(self, ctx, *, from_provider, to_provider, reason):
        await self._dispatch("on_provider_failover", ctx, from_provider=from_provider, to_provider=to_provider, reason=reason)

    # ── Streaming ──
    async def on_stream_start(self, ctx): await self._dispatch_streaming("on_stream_start", ctx)
    async def on_stream(self, ctx, delta): await self._dispatch_streaming("on_stream", ctx, delta)
    async def on_stream_end(self, ctx, *, resuming): await self._dispatch_streaming("on_stream_end", ctx, resuming=resuming)

    # ── Tool ──
    async def before_execute_tools(self, ctx): await self._dispatch("before_execute_tools", ctx)
    async def on_tool_start(self, ctx, *, tool_name, args, call_id):
        await self._dispatch("on_tool_start", ctx, tool_name=tool_name, args=args, call_id=call_id)
    async def on_tool_end(self, ctx, *, tool_name, result, call_id, latency_ms):
        await self._dispatch("on_tool_end", ctx, tool_name=tool_name, result=result, call_id=call_id, latency_ms=latency_ms)
    async def on_tool_error(self, ctx, *, tool_name, error, call_id):
        await self._dispatch("on_tool_error", ctx, tool_name=tool_name, error=error, call_id=call_id)
    async def after_execute_tools(self, ctx): await self._dispatch("after_execute_tools", ctx)

    # ── Safety ──
    async def on_safety_blocked(self, ctx, *, rule, reason, action):
        await self._dispatch("on_safety_blocked", ctx, rule=rule, reason=reason, action=action)

    # ── SubAgent ──
    async def on_subagent_start(self, ctx, *, spec, depth):
        await self._dispatch("on_subagent_start", ctx, spec=spec, depth=depth)
    async def on_subagent_end(self, ctx, *, spec, result, depth):
        await self._dispatch("on_subagent_end", ctx, spec=spec, result=result, depth=depth)

    # ── Error ──
    async def on_error(self, ctx, *, error): await self._dispatch("on_error", ctx, error=error)

    # ── Content ──
    def finalize_content(self, ctx, content):
        for h in self._hooks:
            content = h.finalize_content(ctx, content)
        return content
```

#### `core/stream.py` — StreamProcessor

```python
"""
StreamProcessor：聚合 Provider 的 StreamEvent 流。
职责：
1. 累积文本片段形成完整的 assistant 响应
2. 累积工具调用的增量 JSON 片段
3. 在流式过程中调用 Hook 回调
"""
from dataclasses import dataclass, field
from myagent.providers.base import StreamEvent
from myagent.context.message import ToolCall

@dataclass
class StreamResult:
    """StreamProcessor 处理完一轮流的结果。"""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)

class StreamProcessor:
    """聚合一次 Provider 流调用的所有事件。"""

    def __init__(self):
        self._text_buffer: str = ""
        self._tool_calls: list[ToolCall] = []
        self._stop_reason: str = ""
        self._usage: dict = {}

    def process_event(self, event: StreamEvent) -> None:
        if event.type == "text_delta" and event.text:
            self._text_buffer += event.text
        elif event.type == "tool_call_end":
            self._tool_calls.append(ToolCall(
                id=event.tool_call_id or "",
                name=event.tool_name or "",
                arguments=event.tool_args or {},
            ))
        elif event.type == "message_end":
            self._stop_reason = event.stop_reason or ""
            if event.usage:
                self._usage = event.usage

    def get_result(self) -> StreamResult:
        return StreamResult(
            text=self._text_buffer,
            tool_calls=list(self._tool_calls),
            stop_reason=self._stop_reason,
            usage=self._usage,
        )

    def reset(self) -> None:
        self._text_buffer = ""
        self._tool_calls = []
        self._stop_reason = ""
        self._usage = {}
```

#### `core/parser.py` — StructuredOutputParser

```python
"""StructuredOutputParser：从 LLM 输出中提取结构化数据（代码块、JSON 等）。"""
import json
import re
from typing import Any, Callable

class StructuredOutputParser:
    """
    支持多种解析策略：
    1. Markdown 代码块提取 (```json ... ```)
    2. 自定义注册解析器
    """

    _CODEBLOCK_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

    def __init__(self):
        self._parsers: dict[str, Callable[[str], Any]] = {}

    def register(self, format_name: str, parser_fn: Callable[[str], Any]) -> None:
        self._parsers[format_name] = parser_fn

    def extract_json(self, text: str) -> dict | list | None:
        """尝试从文本中提取 JSON 内容。"""
        # 先尝试代码块
        for match in self._CODEBLOCK_RE.finditer(text):
            lang, content = match.group(1), match.group(2)
            if lang in ("json", ""):
                try:
                    return json.loads(content.strip())
                except json.JSONDecodeError:
                    continue
        # 再尝试整段文本
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            return None

    def parse(self, text: str, format_name: str) -> Any:
        if format_name in self._parsers:
            return self._parsers[format_name](text)
        if format_name == "json":
            return self.extract_json(text)
        raise ValueError(f"Unknown format: {format_name}")
```

#### `core/loop.py` — AgentLoop（V3 显式状态机）

```python
"""
AgentLoop：V3 显式状态机驱动的 ReAct 循环。
❗核心原则：不写 while True。

状态转移图：
  IDLE → RUNNING → (text end) → FINISHED
                 → (tool calls) → WAITING_TOOL → RUNNING (next iteration)
                 → (error) → ERROR
                 
每次状态转换前先持久化到 StateStore，确保断线可恢复。
"""
import asyncio
from uuid import uuid4

from myagent.context.manager import ContextManager
from myagent.context.state import AgentState, StateStore
from myagent.context.message import ToolCall, ToolResult as MsgToolResult
from myagent.core.hook import AgentHook, HookContext, CompositeHook
from myagent.core.stream import StreamProcessor
from myagent.providers.router import ProviderRouter
from myagent.tools.executor import ToolExecutor
from myagent.tools.base import ToolResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class AgentLoop:
    """
    V3 状态机 AgentLoop。
    
    每次调用 run_turn() 处理一个完整的 Turn（用户输入 → 最终响应）。
    调用 resume() 可从 StateStore 恢复断点继续运行。
    """

    def __init__(
        self,
        provider_router: ProviderRouter,
        context_manager: ContextManager,
        tool_executor: ToolExecutor,
        state_store: StateStore,
        hook: AgentHook | None = None,
        max_iterations: int = 25,
        session_id: str | None = None,
    ):
        self._router = provider_router
        self._ctx_mgr = context_manager
        self._tool_executor = tool_executor
        self._state_store = state_store
        self._hook = hook or CompositeHook()
        self._max_iterations = max_iterations
        self._session_id = session_id or uuid4().hex[:16]

        # 运行时状态
        self._state = AgentState.IDLE
        self._iteration = 0
        self._hook_ctx: HookContext | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    async def run_turn(self, user_input: str) -> str:
        """
        处理一个完整 Turn：用户输入 → (N 轮 ReAct 迭代) → 最终文本响应。
        """
        turn_id = uuid4().hex[:12]
        trace_id = uuid4().hex[:16]

        self._hook_ctx = HookContext(
            session_id=self._session_id,
            turn_id=turn_id,
            trace_id=trace_id,
            span_id=uuid4().hex[:8],
            model=self._router.current_provider.model if self._router.current_provider else "",
            provider=self._router.current_provider.name if self._router.current_provider else "",
        )

        # 添加用户消息
        self._ctx_mgr.add_user_message(user_input)

        await self._hook.on_turn_start(self._hook_ctx)
        await self._transition(AgentState.RUNNING)

        try:
            result = await self._react_loop()
            await self._transition(AgentState.FINISHED)
            # 持久化消息历史
            await self._state_store.save_messages(self._session_id, self._ctx_mgr.messages)
            content = self._hook.finalize_content(self._hook_ctx, result)
            await self._hook.on_turn_end(self._hook_ctx)
            return content or ""
        except Exception as e:
            await self._transition(AgentState.ERROR, metadata={"error": str(e)})
            await self._hook.on_error(self._hook_ctx, error=e)
            raise

    async def resume(self) -> str:
        """
        从 StateStore 恢复断点继续运行。
        V3 核心：状态机恢复模式。
        """
        state, metadata = await self._state_store.load_state(self._session_id)
        logger.info(f"Resuming session {self._session_id}, state={state.value}")

        # 恢复消息历史
        messages = await self._state_store.load_messages(self._session_id)
        if messages:
            self._ctx_mgr.restore_from(messages)

        # 预热幂等缓存
        cached_results = await self._state_store.load_tool_results(self._session_id)
        if cached_results and self._tool_executor._cache:
            self._tool_executor._cache.preload(
                {k: ToolResult(content=v.content, is_error=v.is_error, metadata=v.metadata)
                 for k, v in cached_results.items()}
            )

        self._state = state
        self._hook_ctx = HookContext(
            session_id=self._session_id,
            trace_id=metadata.get("trace_id", uuid4().hex[:16]),
            span_id=uuid4().hex[:8],
            model=self._router.current_provider.model if self._router.current_provider else "",
            provider=self._router.current_provider.name if self._router.current_provider else "",
        )

        if state == AgentState.WAITING_TOOL:
            # 从工具等待状态恢复：加载 pending 工具然后执行
            pending = await self._state_store.load_pending_tool_calls(self._session_id)
            if pending:
                await self._execute_tools(pending)
                await self._transition(AgentState.RUNNING)
            return await self._react_loop()
        elif state == AgentState.RUNNING:
            return await self._react_loop()
        elif state == AgentState.ERROR:
            # 错误恢复：尝试重新开始当前 iteration
            await self._transition(AgentState.RUNNING)
            return await self._react_loop()
        else:
            return ""  # IDLE / FINISHED 无需恢复

    async def _react_loop(self) -> str:
        """
        ReAct 主循环。通过迭代计数控制而非 while True。
        每次迭代：调用 LLM → 处理响应 → (如有工具调用则执行) → 下一轮
        """
        for iteration in range(self._iteration, self._max_iterations):
            self._iteration = iteration + 1
            self._hook_ctx.iteration = self._iteration

            await self._hook.on_iteration_start(self._hook_ctx)
            await self._hook.on_provider_call_start(self._hook_ctx)

            # ── 调用 LLM ──
            provider = self._router.current_provider
            if provider is None:
                raise RuntimeError("No available provider")

            self._hook_ctx.model = provider.model
            self._hook_ctx.provider = provider.name

            messages = [msg.to_openai_dict() for msg in self._ctx_mgr.get_messages()]
            tools_schemas = self._tool_executor._registry.to_openai_schemas() if self._tool_executor._registry else None

            stream_processor = StreamProcessor()
            first_token = True

            async for event in self._router.stream(messages, tools_schemas):
                stream_processor.process_event(event)

                if event.type == "text_delta" and event.text:
                    if first_token:
                        await self._hook.on_stream_start(self._hook_ctx)
                        first_token = False
                    await self._hook.on_stream(self._hook_ctx, event.text)

            if not first_token:
                # 有文本输出，结束流
                result = stream_processor.get_result()
                has_tool_calls = bool(result.tool_calls)
                await self._hook.on_stream_end(self._hook_ctx, resuming=has_tool_calls)

            result = stream_processor.get_result()
            self._hook_ctx.usage = result.usage
            await self._hook.on_provider_call_end(
                self._hook_ctx, stop_reason=result.stop_reason, usage=result.usage
            )

            # ── 保存 assistant 消息 ──
            self._ctx_mgr.add_assistant_message(result.text, result.tool_calls or None)

            # ── 如无工具调用，循环结束 ──
            if not result.tool_calls:
                await self._hook.on_iteration_end(self._hook_ctx)
                return result.text

            # ── 有工具调用：V3 状态机转 WAITING_TOOL ──
            self._hook_ctx.tool_calls = result.tool_calls
            await self._transition(AgentState.WAITING_TOOL)

            # 持久化 pending 工具调用
            await self._state_store.save_pending_tool_calls(self._session_id, result.tool_calls)
            await self._state_store.save_messages(self._session_id, self._ctx_mgr.messages)

            # ── 执行工具 ──
            await self._execute_tools(result.tool_calls)

            # ── 回到 RUNNING，进入下一轮迭代 ──
            await self._transition(AgentState.RUNNING)
            await self._hook.on_iteration_end(self._hook_ctx)

        # 达到最大迭代次数
        logger.warning(f"Max iterations ({self._max_iterations}) reached for session {self._session_id}")
        return self._ctx_mgr.messages[-1].content if self._ctx_mgr.messages else ""

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> None:
        """执行一批工具调用并写入上下文。"""
        await self._hook.before_execute_tools(self._hook_ctx)

        for tc in tool_calls:
            await self._hook.on_tool_start(
                self._hook_ctx, tool_name=tc.name, args=tc.arguments, call_id=tc.id
            )

        # 并行执行
        results = await self._tool_executor.execute_batch(tool_calls)

        # 记录结果
        tool_events = []
        for tc, result in zip(tool_calls, results):
            latency = result.metadata.get("latency_ms", 0)
            if result.is_error:
                await self._hook.on_tool_error(
                    self._hook_ctx, tool_name=tc.name, error=Exception(result.content), call_id=tc.id
                )
            else:
                await self._hook.on_tool_end(
                    self._hook_ctx, tool_name=tc.name, result=result, call_id=tc.id, latency_ms=latency
                )

            tool_events.append({
                "name": tc.name,
                "call_id": tc.id,
                "result": result.content[:200],
                "is_error": result.is_error,
                "latency_ms": latency,
            })

            # 写入上下文
            self._ctx_mgr.add_tool_result(
                tc.id,
                MsgToolResult(tool_call_id=tc.id, content=result.content, is_error=result.is_error),
            )

            # 持久化工具结果（用于断线恢复）
            await self._state_store.save_tool_result(
                self._session_id, tc.id,
                MsgToolResult(tool_call_id=tc.id, content=result.content, is_error=result.is_error),
            )

        self._hook_ctx.tool_events = tool_events
        await self._hook.after_execute_tools(self._hook_ctx)

    async def _transition(self, new_state: AgentState, metadata: dict | None = None) -> None:
        """状态转移 + 持久化。V3 核心：先写后做。"""
        old = self._state
        self._state = new_state
        meta = metadata or {}
        if self._hook_ctx:
            meta["trace_id"] = self._hook_ctx.trace_id
        await self._state_store.save_state(self._session_id, new_state, meta)
        logger.debug(f"State transition: {old.value} → {new_state.value} (session={self._session_id})")
```

#### `core/agent.py` — Agent 门面类

```python
"""
Agent 门面类：唯一的公开入口。
负责组装所有组件（Provider、Context、Tools、Hooks、Audit）并暴露简洁的 API。
"""
from uuid import uuid4

from myagent.core.loop import AgentLoop
from myagent.core.hook import CompositeHook, AgentHook
from myagent.context.manager import ContextManager
from myagent.context.state import SQLiteStateStore
from myagent.providers.router import ProviderRouter
from myagent.providers.openai_provider import OpenAIProvider
from myagent.providers.anthropic_provider import AnthropicProvider
from myagent.tools.executor import ToolExecutor
from myagent.tools.registry import ToolRegistry
from myagent.tools.idempotency import IdempotencyCache
from myagent.utils.config import AgentConfig
from myagent.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)

class Agent:
    """Agent 门面类。Phase 1 的组装入口。"""

    def __init__(self, config: AgentConfig, hooks: list[AgentHook] | None = None):
        self._config = config
        self._composite_hook = CompositeHook(hooks or [])

        # 构建 Providers
        providers = self._build_providers()
        self._router = ProviderRouter(
            providers=providers,
            failure_threshold=config.failover.circuit_breaker_failure_threshold,
            recovery_seconds=config.failover.circuit_breaker_recovery_seconds,
        )

        # 构建工具系统
        self._tool_registry = ToolRegistry()
        self._idempotency_cache = IdempotencyCache()
        self._tool_executor = ToolExecutor(
            registry=self._tool_registry,
            idempotency_cache=self._idempotency_cache,
        )

        # 状态存储
        self._state_store = SQLiteStateStore()

        # 上下文管理器（按会话创建）
        self._context_managers: dict[str, ContextManager] = {}

    def _build_providers(self) -> list:
        providers = []
        for pc in self._config.providers:
            if pc.type == "openai":
                p = OpenAIProvider(name=pc.name, model=pc.model, api_key=pc.api_key, api_base=pc.api_base)
            elif pc.type == "anthropic":
                p = AnthropicProvider(name=pc.name, model=pc.model, api_key=pc.api_key, api_base=pc.api_base)
            else:
                logger.warning(f"Unknown provider type: {pc.type}, skipping")
                continue
            p._priority = pc.priority
            providers.append(p)
        return providers

    async def initialize(self) -> None:
        """初始化所有异步资源。"""
        await self._state_store.initialize()
        logger.info("Agent initialized")

    async def shutdown(self) -> None:
        """清理资源。"""
        await self._state_store.close()
        logger.info("Agent shutdown")

    def register_tool(self, tool) -> None:
        """注册工具。"""
        self._tool_registry.register(tool)

    def add_hook(self, hook: AgentHook) -> None:
        """添加 Hook。"""
        self._composite_hook.add(hook)

    async def chat(self, user_input: str, session_id: str | None = None, system_prompt: str | None = None) -> str:
        """
        单次对话接口。
        """
        session_id = session_id or uuid4().hex[:16]

        # 获取或创建 ContextManager
        if session_id not in self._context_managers:
            self._context_managers[session_id] = ContextManager(
                max_tokens_budget=self._config.max_tokens_budget,
                tool_result_max_chars=self._config.tool_result_max_chars,
            )
            if system_prompt:
                self._context_managers[session_id].set_system(system_prompt)

        ctx_mgr = self._context_managers[session_id]

        loop = AgentLoop(
            provider_router=self._router,
            context_manager=ctx_mgr,
            tool_executor=self._tool_executor,
            state_store=self._state_store,
            hook=self._composite_hook,
            max_iterations=self._config.max_iterations,
            session_id=session_id,
        )

        return await loop.run_turn(user_input)

    async def resume(self, session_id: str) -> str:
        """恢复中断的会话。"""
        if session_id not in self._context_managers:
            self._context_managers[session_id] = ContextManager(
                max_tokens_budget=self._config.max_tokens_budget,
                tool_result_max_chars=self._config.tool_result_max_chars,
            )

        loop = AgentLoop(
            provider_router=self._router,
            context_manager=self._context_managers[session_id],
            tool_executor=self._tool_executor,
            state_store=self._state_store,
            hook=self._composite_hook,
            max_iterations=self._config.max_iterations,
            session_id=session_id,
        )
        return await loop.resume()
```

---

### ⑥ observability/ — 审计系统

> Phase 1 实现：AuditLevel + AuditEvent 模型 + AuditLogger（异步队列）+ JSONLBackend + AuditHook + FieldMasker

#### `observability/level.py`
```python
"""AuditLevel 四级粒度枚举。"""
from enum import IntEnum

class AuditLevel(IntEnum):
    MINIMAL = 1
    STANDARD = 2
    VERBOSE = 3
    DEBUG = 4
```

#### `observability/events.py`
```python
"""
AuditEvent 数据模型族（Pydantic）。
每个事件必带 trace_id（V3 链路追踪）。
"""
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from pydantic import BaseModel, Field
from myagent.observability.level import AuditLevel

class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid4().hex[:16])
    event_type: str
    session_id: str
    agent_id: str = "main"
    trace_id: str = ""           # V3: 全链路追踪
    span_id: str = ""
    level: AuditLevel = AuditLevel.STANDARD
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    environment: str = "dev"
    metadata: dict[str, Any] = Field(default_factory=dict)

class ConversationEvent(AuditEvent):
    event_type: str = "conversation"
    turn_id: str = ""
    sub_type: str = ""          # user_input | assistant_response
    user_input: str | None = None
    user_input_hash: str = ""
    assistant_response: str | None = None
    stop_reason: str = ""
    model: str = ""
    provider: str = ""
    tokens_input: int = 0
    tokens_output: int = 0
    latency_ms: int = 0

class ToolCallEvent(AuditEvent):
    event_type: str = "tool_call"
    turn_id: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    phase: str = ""             # before | after
    args: dict | None = None
    result_summary: str = ""
    is_error: bool = False
    latency_ms: int = 0

class ProviderEvent(AuditEvent):
    event_type: str = "provider"
    sub_type: str = ""          # failover | rate_limit | circuit_break
    from_provider: str | None = None
    to_provider: str | None = None
    error_message: str | None = None

class ErrorEvent(AuditEvent):
    event_type: str = "error"
    error_type: str = ""
    error_message: str = ""
    traceback: str | None = None
    context: dict = Field(default_factory=dict)
```

#### `observability/masker.py`
```python
"""FieldMasker：PII 脱敏 + 字段粒度控制。"""
import hashlib
from myagent.observability.level import AuditLevel
from myagent.observability.events import AuditEvent

class FieldMasker:
    """根据 AuditLevel 控制字段记录深度。"""

    def __init__(self, redact_patterns: list[str] | None = None):
        self._redact_patterns: list[str] = redact_patterns or []

    def add_redact_pattern(self, pattern: str) -> None:
        """供 SecretManager 注册需要脱敏的密文。"""
        if pattern and pattern not in self._redact_patterns:
            self._redact_patterns.append(pattern)

    def apply(self, event: AuditEvent, level: AuditLevel) -> AuditEvent:
        """根据级别控制字段粒度。返回处理后的事件副本。"""
        data = event.model_dump()

        # 对文本字段应用密文脱敏
        for key in ("user_input", "assistant_response", "error_message"):
            if key in data and data[key]:
                data[key] = self._redact_secrets(data[key])

        # 按级别截断/隐藏
        if level <= AuditLevel.MINIMAL:
            if "user_input" in data and data["user_input"]:
                data["user_input_hash"] = hashlib.sha256(data["user_input"].encode()).hexdigest()[:16]
                data["user_input"] = None
            if "assistant_response" in data and data["assistant_response"]:
                data["assistant_response"] = data["assistant_response"][:200]
            if "args" in data:
                data["args"] = None
        elif level <= AuditLevel.STANDARD:
            if "user_input" in data and data["user_input"]:
                data["user_input"] = data["user_input"][:2000]
            if "args" in data:
                data["args"] = None  # standard 级不记录工具参数

        return type(event).model_validate(data)

    def _redact_secrets(self, text: str) -> str:
        for pattern in self._redact_patterns:
            text = text.replace(pattern, "[REDACTED]")
        return text
```

#### `observability/audit_logger.py`
```python
"""AuditLogger：中心门面，异步队列写入。"""
import asyncio
from myagent.observability.events import AuditEvent
from myagent.observability.level import AuditLevel
from myagent.observability.masker import FieldMasker
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class AuditLogger:
    def __init__(
        self,
        level: AuditLevel,
        backends: list,  # list[BaseAuditBackend]
        masker: FieldMasker,
        queue_size: int = 2000,
    ):
        self._level = level
        self._backends = backends
        self._masker = masker
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=queue_size)
        self._writer_task: asyncio.Task | None = None
        self._dropped_count: int = 0

    async def start(self) -> None:
        self._writer_task = asyncio.create_task(self._write_loop())
        logger.info(f"AuditLogger started (level={self._level.name}, backends={len(self._backends)})")

    async def stop(self) -> None:
        await self._queue.join()
        if self._writer_task:
            self._writer_task.cancel()
        for backend in self._backends:
            await backend.close()

    async def log(self, event: AuditEvent) -> None:
        event = self._masker.apply(event, self._level)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_count += 1
            if self._dropped_count % 10 == 0:
                logger.warning(f"Audit queue full, {self._dropped_count} events dropped")

    async def _write_loop(self) -> None:
        while True:
            try:
                event = await self._queue.get()
                for backend in self._backends:
                    try:
                        await backend.write(event)
                    except Exception as e:
                        logger.error(f"Audit backend write failed: {e}")
                self._queue.task_done()
            except asyncio.CancelledError:
                break
```

#### `observability/hook.py` — AuditHook

```python
"""AuditHook：通过 AgentHook 体系挂载审计能力，对 AgentLoop 零侵入。"""
import hashlib
from myagent.core.hook import AgentHook, HookContext
from myagent.observability.audit_logger import AuditLogger
from myagent.observability.events import ConversationEvent, ToolCallEvent, ErrorEvent

class AuditHook(AgentHook):
    def __init__(self, audit_logger: AuditLogger):
        self._audit = audit_logger

    async def on_turn_start(self, ctx: HookContext) -> None:
        # 记录 turn 开始（可作为 user_input 占位）
        pass

    async def on_provider_call_end(self, ctx: HookContext, *, stop_reason: str, usage: dict) -> None:
        await self._audit.log(ConversationEvent(
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            turn_id=ctx.turn_id,
            sub_type="provider_call",
            stop_reason=stop_reason,
            model=ctx.model,
            provider=ctx.provider,
            tokens_input=usage.get("input_tokens", 0),
            tokens_output=usage.get("output_tokens", 0),
        ))

    async def on_tool_start(self, ctx: HookContext, *, tool_name: str, args: dict, call_id: str) -> None:
        await self._audit.log(ToolCallEvent(
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            turn_id=ctx.turn_id,
            tool_name=tool_name,
            tool_call_id=call_id,
            phase="before",
            args=args,
        ))

    async def on_tool_end(self, ctx: HookContext, *, tool_name: str, result, call_id: str, latency_ms: int) -> None:
        await self._audit.log(ToolCallEvent(
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            turn_id=ctx.turn_id,
            tool_name=tool_name,
            tool_call_id=call_id,
            phase="after",
            result_summary=str(result.content)[:200] if hasattr(result, 'content') else "",
            is_error=getattr(result, 'is_error', False),
            latency_ms=latency_ms,
        ))

    async def on_error(self, ctx: HookContext, *, error: Exception) -> None:
        import traceback as tb
        await self._audit.log(ErrorEvent(
            session_id=ctx.session_id,
            trace_id=ctx.trace_id,
            error_type=type(error).__name__,
            error_message=str(error),
            traceback=tb.format_exc(),
            context=ctx.snapshot(),
        ))
```

#### `observability/backends/base.py`
```python
"""审计后端抽象基类。"""
from abc import ABC, abstractmethod
from myagent.observability.events import AuditEvent

class BaseAuditBackend(ABC):
    @abstractmethod
    async def write(self, event: AuditEvent) -> None: ...
    async def close(self) -> None: pass
```

#### `observability/backends/jsonl_backend.py`
```python
"""JSONL 文件后端（默认，零额外依赖）。"""
from datetime import datetime, timezone
from pathlib import Path
import aiofiles
from myagent.observability.backends.base import BaseAuditBackend
from myagent.observability.events import AuditEvent

class JSONLBackend(BaseAuditBackend):
    def __init__(self, log_dir: str | Path = "./audit_logs", retention_days: int = 90):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._retention_days = retention_days

    async def write(self, event: AuditEvent) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._log_dir / f"{today}.jsonl"
        line = event.model_dump_json() + "\n"
        async with aiofiles.open(path, "a", encoding="utf-8") as f:
            await f.write(line)

    async def close(self) -> None:
        pass
```

---

### ⑦ interfaces/ — 接口层

#### `interfaces/websocket/lock.py` — SessionMutex

```python
"""
SessionMutex：基于 session_id 的并发控制锁。
防止同一会话被多个请求并行操作，导致上下文混乱。
"""
import asyncio
from collections import defaultdict

class SessionMutex:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, session_id: str) -> None:
        await self._locks[session_id].acquire()

    def release(self, session_id: str) -> None:
        if session_id in self._locks:
            self._locks[session_id].release()

    async def __aenter__(self, session_id: str):
        await self.acquire(session_id)
        return self

    async def __aexit__(self, *args):
        pass  # 需要显式调用 release
```

#### `interfaces/cli/ui.py` — CLIProgressHook

```python
"""
CLIProgressHook：基于 Rich 的流式 CLI 渲染。
实现 wants_streaming=True，接收 on_stream 回调实时输出文本。
"""
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner

from myagent.core.hook import AgentHook, HookContext

class CLIProgressHook(AgentHook):
    """Rich 流式渲染 Hook。"""

    def __init__(self, console: Console | None = None):
        self._console = console or Console()
        self._text_buffer = ""
        self._live: Live | None = None

    def wants_streaming(self) -> bool:
        return True

    async def on_turn_start(self, ctx: HookContext) -> None:
        self._text_buffer = ""

    async def on_stream_start(self, ctx: HookContext) -> None:
        self._text_buffer = ""
        self._live = Live(console=self._console, refresh_per_second=8)
        self._live.start()

    async def on_stream(self, ctx: HookContext, delta: str) -> None:
        self._text_buffer += delta
        if self._live:
            self._live.update(Markdown(self._text_buffer))

    async def on_stream_end(self, ctx: HookContext, *, resuming: bool) -> None:
        if self._live:
            self._live.update(Markdown(self._text_buffer))
            self._live.stop()
            self._live = None
        if resuming:
            self._console.print()  # 空行

    async def on_tool_start(self, ctx: HookContext, *, tool_name: str, args: dict, call_id: str) -> None:
        self._console.print(
            Panel(
                f"[bold cyan]🔧 {tool_name}[/]\n{_format_args(args)}",
                border_style="cyan",
                title="Tool Call",
                title_align="left",
            )
        )

    async def on_tool_end(self, ctx: HookContext, *, tool_name: str, result, call_id: str, latency_ms: int) -> None:
        content = getattr(result, 'content', str(result))
        is_error = getattr(result, 'is_error', False)
        style = "red" if is_error else "green"
        self._console.print(
            Panel(
                f"[{style}]{content[:500]}[/]",
                border_style=style,
                title=f"✅ {tool_name} ({latency_ms}ms)" if not is_error else f"❌ {tool_name} ({latency_ms}ms)",
                title_align="left",
            )
        )

    async def on_provider_failover(self, ctx: HookContext, *, from_provider: str, to_provider: str, reason: str) -> None:
        self._console.print(
            f"[yellow]⚠ Provider failover: {from_provider} → {to_provider} ({reason})[/]"
        )

    async def on_error(self, ctx: HookContext, *, error: Exception) -> None:
        self._console.print(f"[bold red]Error: {error}[/]")

def _format_args(args: dict) -> str:
    if not args:
        return "[dim]no args[/]"
    lines = [f"  {k}: {v}" for k, v in args.items()]
    return "\n".join(lines)
```

#### `interfaces/cli/main.py` — CLI 入口

```python
"""
CLI 入口：基于 Click 的命令行界面。
支持：chat（交互/单次）、resume（断点恢复）。
"""
import asyncio
import click
from pathlib import Path
from rich.console import Console

console = Console()

@click.group()
@click.option("--config", default="config/config.yaml", help="配置文件路径")
@click.option("--log-level", default="INFO", help="日志级别")
@click.pass_context
def cli(ctx, config, log_level):
    """MyAgent — 全自研 Python Agent 框架"""
    from myagent.utils.logging import setup_logging
    setup_logging(level=log_level)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config

@cli.command()
@click.argument("message", required=False)
@click.option("--session-id", default=None, help="会话 ID（用于多轮对话）")
@click.option("--system-prompt", default=None, help="System Prompt")
@click.option("--show-tools", is_flag=True, help="显示工具调用详情")
@click.pass_context
def chat(ctx, message, session_id, system_prompt, show_tools):
    """与 Agent 对话"""
    asyncio.run(_chat(ctx.obj["config_path"], message, session_id, system_prompt))

async def _chat(config_path: str, message: str | None, session_id: str | None, system_prompt: str | None):
    from myagent.utils.config import load_yaml_config, AgentConfig
    from myagent.core.agent import Agent
    from myagent.interfaces.cli.ui import CLIProgressHook
    from myagent.observability.hook import AuditHook
    from myagent.observability.audit_logger import AuditLogger
    from myagent.observability.masker import FieldMasker
    from myagent.observability.level import AuditLevel
    from myagent.observability.backends.jsonl_backend import JSONLBackend

    # 加载配置
    raw = load_yaml_config(config_path)
    config = AgentConfig(**raw) if raw else AgentConfig()

    # 构建审计系统
    masker = FieldMasker()
    jsonl = JSONLBackend(log_dir=config.audit.jsonl_log_dir)
    audit_logger = AuditLogger(
        level=AuditLevel(config.audit.level) if isinstance(config.audit.level, int) else AuditLevel[config.audit.level.upper()],
        backends=[jsonl],
        masker=masker,
    )

    # 构建 Agent
    agent = Agent(
        config=config,
        hooks=[CLIProgressHook(console), AuditHook(audit_logger)],
    )

    await agent.initialize()
    await audit_logger.start()

    try:
        if message:
            # 单次对话
            result = await agent.chat(message, session_id=session_id, system_prompt=system_prompt)
        else:
            # 交互模式
            console.print("[bold green]MyAgent[/] — 输入 /exit 退出, /clear 清空上下文\n")
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/] ")
                except (EOFError, KeyboardInterrupt):
                    break
                if user_input.strip() == "/exit":
                    break
                if user_input.strip() == "/clear":
                    session_id = None
                    console.print("[dim]上下文已清空[/]")
                    continue
                if not user_input.strip():
                    continue
                console.print()
                result = await agent.chat(user_input, session_id=session_id, system_prompt=system_prompt)
                console.print()
    finally:
        await audit_logger.stop()
        await agent.shutdown()

@cli.command()
@click.argument("session_id")
@click.pass_context
def resume(ctx, session_id):
    """恢复中断的会话"""
    asyncio.run(_resume(ctx.obj["config_path"], session_id))

async def _resume(config_path: str, session_id: str):
    from myagent.utils.config import load_yaml_config, AgentConfig
    from myagent.core.agent import Agent
    from myagent.interfaces.cli.ui import CLIProgressHook

    raw = load_yaml_config(config_path)
    config = AgentConfig(**raw) if raw else AgentConfig()
    agent = Agent(config=config, hooks=[CLIProgressHook(console)])
    await agent.initialize()
    try:
        result = await agent.resume(session_id)
        console.print(f"\n[green]Session {session_id} resumed successfully.[/]")
    finally:
        await agent.shutdown()

if __name__ == "__main__":
    cli()
```

---

## 四、 V3 特性在 Phase 1 中的落地清单

| V3 特性 | Phase 1 落地状态 | 实现要点 |
|---------|-------------|---------|
| **显式状态机 (AgentState)** | ✅ 完整实现 | `core/loop.py` 中 `AgentState` 枚举 + `_transition()` 先写后做 |
| **StateStore 持久化** | ✅ 完整实现 | `context/state.py` SQLite 三表设计 (sessions, messages, pending_tool_calls) |
| **IdempotencyCache** | ✅ 完整实现 | `tools/idempotency.py` 内存 LRU + StateStore 预热 |
| **TokenBudget 三层控制** | ⚠️ 预留骨架 | `context/manager.py` 中 `tool_result_max_chars` 强截断已实现；三层裁剪待 Phase 5 |
| **PolicyEngine (ALLOW/DENY/HITL)** | ⏳ Phase 2 | Phase 1 无安全层，工具直接执行 |
| **Budget Tree 资源隔离** | ⏳ Phase 3 | SubAgent 系统的一部分 |
| **SessionMutex** | ✅ 实现 | `interfaces/websocket/lock.py` |
| **Trace/Span ID 链路追踪** | ✅ 实现 | `HookContext` 中 `trace_id` / `span_id`，所有 AuditEvent 带入 |

---

## 五、配置文件模板

### `config/config.yaml`

```yaml
providers:
  - name: anthropic_primary
    type: anthropic
    model: claude-sonnet-4-20250514
    priority: 1
    api_key: "${ANTHROPIC_API_KEY}"
  - name: openai_fallback
    type: openai
    model: gpt-4o
    priority: 2
    api_key: "${OPENAI_API_KEY}"

failover:
  strategy: priority
  circuit_breaker_failure_threshold: 3
  circuit_breaker_recovery_seconds: 60

max_iterations: 25
max_tokens_budget: 100000
tool_result_max_chars: 4000

audit:
  enabled: true
  level: standard
  environment: dev
  queue_size: 2000
  jsonl_log_dir: ./audit_logs
  jsonl_retention_days: 90
```

---

## 六、Phase 1 测试计划

### 单元测试（最低覆盖要求）

| 模块 | 测试文件 | 关键测试用例 |
|------|---------|------------|
| `utils/retry.py` | `test_retry.py` | 指数退避的延迟值、最大重试次数、仅对指定异常重试 |
| `utils/config.py` | `test_config.py` | YAML 加载 + 环境变量解析 |
| `context/message.py` | `test_message.py` | Message 序列化/反序列化、OpenAI/Anthropic 格式转换 |
| `context/state.py` | `test_state.py` | SQLite CRUD + AgentState 转换 + 幂等缓存预热 |
| `context/manager.py` | `test_context.py` | 工具结果截断、消息列表管理 |
| `providers/router.py` | `test_router.py` | Failover 切换、熔断器触发与恢复 |
| `tools/idempotency.py` | `test_idempotency.py` | LRU 淘汰、缓存命中跳过 |
| `tools/executor.py` | `test_executor.py` | 工具执行 + 超时 + 幂等拦截 |
| `core/stream.py` | `test_stream.py` | StreamEvent 聚合 |
| `observability/masker.py` | `test_masker.py` | 各级别字段截断与哈希 |

### 集成测试

```python
# tests/test_integration.py
async def test_basic_chat():
    """测试基本对话流程（需要 LLM API Key）。"""

async def test_tool_call_and_idempotency():
    """注册 echo 测试工具，验证工具调用 + 幂等缓存。"""

async def test_state_persistence_and_resume():
    """验证状态持久化与断点恢复。"""
```

---

## 七、对编码 AI 的特别提示

1. **强异步控制**：所有 IO（网络、文件写入、数据库查）必须带 `await`。严禁在异步上下文中调用同步阻塞函数。
2. **严防代码臃肿**：严格遵守单一职责原则（SRP）。AgentLoop 不直接写日志，委派给 Hook → AuditLogger。
3. **先写后做**：状态转换必须先 `_transition()` 持久化到 StateStore，再执行实际操作。
4. **分批提交**：建议按以下分组提交代码：
   - 第 1 批：`pyproject.toml` + `utils/` (4文件)
   - 第 2 批：`context/` (3文件)
   - 第 3 批：`providers/` (5文件)
   - 第 4 批：`tools/` (4文件)
   - 第 5 批：`observability/` (7文件)
   - 第 6 批：`core/` (5文件)
   - 第 7 批：`interfaces/` (3文件) + `config/config.yaml`
5. **不引入多余抽象**：Phase 1 不实现 Safety 层、SubAgent、Skill、PluginManager。对应的接口在 Hook 和 BaseTool 中已经预留。
6. **import 保护**：跨层引用严格通过包的 `__init__.py` 导出，避免对内部实现的直接依赖。

祝编码顺利！
