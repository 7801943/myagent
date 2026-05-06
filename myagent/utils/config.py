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

class TimeoutConfig(BaseModel):
    """超时配置。"""
    llm_generation: float = 120.0      # LLM 流式生成超时（秒）
    tool_batch: float = 60.0           # 工具批量执行超时（秒）
    iteration: float = 300.0           # 单次 ReAct 迭代超时（秒）
    human_approval: float = 300.0      # 人工审批等待超时（秒）

class HotReloadConfig(BaseModel):
    """工具热加载配置。"""
    enabled: bool = False
    watch_dir: str = "myagent/tools/tools_store"
    poll_interval: float = 60.0
    safe_mode: bool = False

class AgentConfig(BaseSettings):
    """Agent 全局配置，支持 YAML 文件加载和环境变量覆盖。"""
    providers: list[ProviderConfig] = Field(default_factory=list)
    failover: FailoverConfig = Field(default_factory=FailoverConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    hot_reload: HotReloadConfig = Field(default_factory=HotReloadConfig)
    max_iterations: int = 25
    max_tokens_budget: int = 100000
    context_window_size: int = 128000
    tool_result_max_chars: int = 4000
    system_prompt: str | None = None
    system_prompt_file: str | None = None

    model_config = {"env_prefix": "MYAGENT_", "extra": "ignore"}
