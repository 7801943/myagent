"""
核心领域模型层 — 纯数据容器与枚举定义。

本文件不依赖任何业务逻辑类，保证纯净性。
所有模块均可安全导入此文件，形成单向依赖图（DAG）。

包含：
  - SessionState：会话生命周期状态枚举
  - AgentRunState：Agent 运行时状态枚举
  - UserContext：用户身份及上下文数据
  - SessionData：会话状态容器（Pydantic BaseModel，强类型嵌套子模型）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, computed_field


# ─── 状态枚举 ──────────────────────────────────────────────

class SessionState(str, Enum):
    """会话生命周期状态。"""
    ACTIVE = "active"           # 活跃
    SUSPENDED = "suspended"     # 挂起（用户离线但保留状态）
    CLOSED = "closed"           # 已关闭


class AgentRunState(str, Enum):
    """
    Agent 运行时状态（原 AgentState，重命名以区分 SessionState）。
    AgentLoop 的每一次状态变迁都必须先持久化到 StateStore，
    再执行实际操作——这是断电恢复的基石。
    """
    IDLE = "idle"                    # 空闲。run() 未执行，或刚结束回到此状态
    THINKING = "thinking"            # LLM 推理阶段（对应 extended thinking）
    GENERATING = "generating"        # LLM 流式输出中（首次 text_delta 后进入）
    WAITING_TOOL = "waiting_tool"    # LLM 已返回 tool_calls，等待执行
    WAITING_HITL = "waiting_hitl"    # 等待人工审批（Phase 2 预留）
    ERROR = "error"                  # 发生错误


# 向后兼容别名
AgentState = AgentRunState

# ── 旧状态值迁移映射 ──
# 精简重构时移除了 RUNNING / FINISHED，但旧数据库中可能仍存储了这些值。
# 加载时通过此映射自动转换，避免 ValueError。
# 用户：未来需要废弃
_LEGACY_STATE_MAP: dict[str, str] = {
    "running": "generating",   # RUNNING → GENERATING
    "finished": "idle",        # FINISHED → IDLE（会话结束即回到空闲）
}


# ─── Pydantic 子模型 ─────────────────────────────────────────
# 每个 SessionData 分组对应一个强类型子模型，
# 替代原来的 dict[str, Any]，获得自动校验、序列化和 IDE 补全。

class TokenUsage(BaseModel):
    """Token 用量统计。"""
    used: int = 0
    total: int = 128000

    @computed_field  # type: ignore[prop-decorator]
    @property
    def percentage(self) -> float:
        """已使用百分比。"""
        return round(self.used / self.total * 100, 1) if self.total > 0 else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remaining(self) -> int:
        """剩余可用 Token。"""
        return max(0, self.total - self.used)


class UserInfo(BaseModel):
    """用户身份信息。"""
    user_id: str = ""
    username: str = ""


class ModelInfo(BaseModel):
    """模型选择与可用列表。"""
    active: dict[str, Any] = {}
    available: list[dict[str, Any]] = []


class ToolInfo(BaseModel):
    """工具注册信息。"""
    tools: list[dict[str, Any]] = []


class SessionContext(BaseModel):
    """会话运行时上下文状态。"""
    token_usage: TokenUsage = TokenUsage()
    agent_run_state: str = "idle"
    session_state: str = "active"
    stop_reason: str = ""
    cancelled: bool = False


class AgentInfo(BaseModel):
    """Agent 运行时信息快照（从 AgentConfig + Agent 实例采集）。"""
    max_iterations: int = 50
    safety_enabled: bool = False
    hot_reload_enabled: bool = False
    llm_timeout: float = 120.0


class WorkspaceInfo(BaseModel):
    """工作区快照。"""
    state: dict[str, Any] | None = None


class SafetyInfo(BaseModel):
    """当前会话独立的 CLI 安全策略状态。"""
    active_policy: str = "whitelist"
    available_policies: list[str] = []
    mode: str = "whitelist"


class ClientStateInfo(BaseModel):
    """前端运行态快照。

    workspace 保存打开文件、活跃 tab 等 UI 状态；model/tools 预留给
    前端模型选择和工具开关，后续可在动态提示词中统一注入。
    """
    workspace: dict[str, Any] | None = None
    model: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    extra: dict[str, Any] = {}


# ─── SessionData（Pydantic BaseModel）──────────────────────────

class SessionData(BaseModel):
    """
    会话状态容器 — Pydantic BaseModel 实现。

    重构自原 dataclass + 手写 get/set/序列化，
    现在由 Pydantic 自动处理校验与序列化。

    新代码推荐直接使用属性访问：
        meta.context.agent_run_state = "idle"
        meta.user.username
        meta.model_dump()
        SessionData.model_validate(d)
    """

    user: UserInfo = UserInfo()
    model: ModelInfo = ModelInfo()
    tool: ToolInfo = ToolInfo()
    context: SessionContext = SessionContext()
    agent: AgentInfo = AgentInfo()
    workspace: WorkspaceInfo = WorkspaceInfo()
    safety: SafetyInfo = SafetyInfo()
    client_state: ClientStateInfo = ClientStateInfo()
    extra: dict[str, Any] = {}

    model_config = {"arbitrary_types_allowed": True}


# ─── UserContext ──────────────────────────────────────────

@dataclass
class UserContext:
    """用户会话上下文。"""
    user_id: str
    username: str = ""
    credentials: dict = field(default_factory=dict)  # 下载 token 等
    preferences: dict = field(default_factory=dict)   # 用户配置