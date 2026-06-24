"""
配置加载：YAML 文件 + 环境变量覆盖。
环境变量用 ${VAR_NAME} 语法在 YAML 中引用。

Phase 1 重构：
  - 删除 AuditConfig（审计功能由标准 logger 替代）
  - 删除 TimeoutConfig（超时简化为模块级常量）
  - AgentConfig 中删除 audit 字段

Phase 2 重构（完整建模）：
  - 新增 SafetyConfig / SecretConfig / SandboxConfig / HITLConfig 等子模型
  - AgentConfig 覆盖 config.yaml 中所有配置段，消除散装字典依赖
  - 所有组件通过 self._config.xxx 强类型访问，不再使用 .get()
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
            return os.environ.get(var_name, "")
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

class ThinkingConfig(BaseModel):
    supported: bool | None = None
    default_enabled: bool = False
    enabled_extra_body: dict = Field(
        default_factory=lambda: {"thinking": {"type": "enabled"}}
    )
    disabled_extra_body: dict = Field(
        default_factory=lambda: {"thinking": {"type": "disabled"}}
    )


class ProviderConfig(BaseModel):
    name: str
    type: str                          # "openai" | "anthropic"
    model: str
    priority: int = 1
    api_key: str = ""
    api_base: str | None = None        # 自定义 endpoint
    context_window_size: int = 200000  # 该模型的上下文窗口大小
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)

class FailoverConfig(BaseModel):
    strategy: str = "priority"         # priority | round_robin | latency
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_recovery_seconds: int = 60

class HotReloadConfig(BaseModel):
    """工具热加载配置。"""
    enabled: bool = False
    watch_dir: str = "myagent/tools/tools_store"
    poll_interval: float = 60.0
    safe_mode: bool = False

class SafetyConfig(BaseModel):
    """安全系统配置。"""
    enabled: bool = False
    rules_path: str = "./config/safety_rules.yaml"
    default_action: str = "allow"      # allow | deny

class SecretConfig(BaseModel):
    """密钥管理配置。"""
    env_prefix: str = "MYAGENT_SECRET_"
    sensitive_fields: list[str] = Field(default_factory=lambda: [
        "password", "api_key", "token", "secret", "access_key", "secret_key",
    ])

class SandboxConfig(BaseModel):
    """沙盒执行环境配置。"""
    backend: str = "subprocess"        # subprocess | docker
    max_cpu_seconds: int = 30
    max_memory_mb: int = 512
    max_output_bytes: int = 102400     # 100KB
    timeout_seconds: float = 60.0

class HITLConfig(BaseModel):
    """人在回路（Human-in-the-Loop）审批配置。"""
    enabled: bool = True
    timeout: int = 120                 # 审批超时（秒）
    approval_timeout: float = 300.0    # 人工审批等待超时（秒）

class ContextConfig(BaseModel):
    """上下文管理配置。"""
    max_tokens_budget: int = 200000
    tool_result_max_chars: int = 200000
    recent_turns: int = 20

class ToolsConfig(BaseModel):
    """工具执行配置。"""
    default_timeout: float = 30.0
    batch_timeout: float = 60.0

class SkillConfig(BaseModel):
    """Skill 系统配置。"""
    enabled: bool = True
    active: list[str] = Field(default_factory=list)
    common_dir: str = "prompts/skills/common"

class AgentConfig(BaseSettings):
    """
    Agent 全局配置，支持 YAML 文件加载和环境变量覆盖。

    覆盖 config.yaml 中 agent 段的所有配置项，每个配置段对应一个强类型子模型。
    外部通过 self._config.safety.enabled / self._config.secrets.env_prefix 等访问，
    不再使用 .get() 散装字典。
    """
    # ── Provider 配置 ──
    providers: list[ProviderConfig] = Field(default_factory=list)
    failover: FailoverConfig = Field(default_factory=FailoverConfig)
    hot_reload: HotReloadConfig = Field(default_factory=HotReloadConfig)

    # ── 运行参数 ──
    max_iterations: int = 50
    llm_timeout: float = 120.0          # LLM 流式生成超时（秒）

    # ── 上下文 ──
    context: ContextConfig = Field(default_factory=ContextConfig)
    # 兼容：顶层字段也保留，以支持 max_tokens_budget / tool_result_max_chars 写在根级的旧配置
    max_tokens_budget: int = 200000
    tool_result_max_chars: int = 200000

    # ── 提示词 ──
    system_prompt: str | None = None
    system_prompt_file: str | None = None
    prompt_template_path: str = "config/prompt_template.yaml"

    # ── 用户根目录（CLI 工具不能越过此目录操作） ──
    root_dir: str = ""

    # ── 安全系统 ──
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    secrets: SecretConfig = Field(default_factory=SecretConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    # ── 人在回路 ──
    hitl: HITLConfig = Field(default_factory=HITLConfig)

    # ── 工具执行 ──
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    # ── Skills 系统 ──
    skills: SkillConfig = Field(default_factory=SkillConfig)

    model_config = {"env_prefix": "MYAGENT_", "extra": "ignore"}
