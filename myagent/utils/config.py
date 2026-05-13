"""
配置加载：YAML 文件 + 环境变量覆盖。
环境变量用 ${VAR_NAME} 语法在 YAML 中引用。

Phase 1 重构：
  - 删除 AuditConfig（审计功能由标准 logger 替代）
  - 删除 TimeoutConfig（超时简化为模块级常量）
  - AgentConfig 中删除 audit 字段
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
    context_window_size: int = 128000  # 该模型的上下文窗口大小

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

class AgentConfig(BaseSettings):
    """Agent 全局配置，支持 YAML 文件加载和环境变量覆盖。"""
    providers: list[ProviderConfig] = Field(default_factory=list)
    failover: FailoverConfig = Field(default_factory=FailoverConfig)
    hot_reload: HotReloadConfig = Field(default_factory=HotReloadConfig)
    max_iterations: int = 50
    # ── 超时参数 ──
    llm_timeout: float = 120.0          # LLM 流式生成超时（秒）
    # ── 上下文 ──
    max_tokens_budget: int = 100000
    tool_result_max_chars: int = 4000
    system_prompt: str | None = None
    system_prompt_file: str | None = None
    # ── 用户根目录（CLI 工具不能越过此目录操作） ──
    root_dir: str = ""

    model_config = {"env_prefix": "MYAGENT_", "extra": "ignore"}