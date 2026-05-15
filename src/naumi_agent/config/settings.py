"""全局配置 — YAML + 环境变量."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class ModelMeta(BaseSettings):
    """单个模型的元数据覆盖（上下文窗口、价格等）."""

    max_context: int | None = None
    max_output: int | None = None
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None


class ModelConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_MODEL__")

    default_model: str = "claude-sonnet-4-6"
    fast_model: str = "claude-haiku-4-5"
    reasoning_model: str = "claude-opus-4-7"
    max_tokens: int = 4096
    temperature: float = 0.7
    api_base: str | None = None
    api_key: str | None = None
    model_info: dict[str, ModelMeta] = Field(default_factory=dict)


class MemoryConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_MEMORY__")

    session_db_path: str = "data/sessions.db"
    vector_db_path: str = "data/chroma"
    compaction_threshold: float = 0.75


class SafetyConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_SAFETY__")

    permission_mode: str = "moderate"
    allowed_dirs: list[str] = Field(default_factory=lambda: ["/workspace"])
    max_budget_usd: float = 5.0
    max_turns: int = 30
    max_input_tokens: int = 500_000


class MCPConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_MCP__")

    servers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class APIConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_API__")

    host: str = "0.0.0.0"
    port: int = 8080
    workers: int = 1
    api_keys: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    rate_limit_rpm: int = 60


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NAUMI_",
        env_nested_delimiter="__",
        env_file=str(Path(__file__).resolve().parents[3] / ".env"),
        env_file_encoding="utf-8",
    )

    models: ModelConfig = Field(default_factory=ModelConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    custom_tools_dir: str | None = None
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        p = Path(path).resolve()
        if not p.exists():
            logger.warning("Config file not found: %s, using defaults + env vars", p)
            return cls()
        logger.debug("Loading config from %s", p)
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)
