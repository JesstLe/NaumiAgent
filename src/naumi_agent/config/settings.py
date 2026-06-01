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
    temperature: float = 1.0
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
    allowed_dirs: list[str] = Field(default_factory=lambda: ["/workspace", str(Path.cwd())])
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


class HooksConfig(BaseSettings):
    """Shell hook 配置 — 按 hook point 分组.

    Example YAML::

        hooks:
          tool_execute_start:
            - command: "ruff check --fix $NAUMI_TOOL_FILE"
              timeout: 10
          tool_execute_end:
            - command: "notify-send 'done'"
    """

    model_config = SettingsConfigDict(env_prefix="NAUMI_HOOKS__")

    # point_name → list of {command, timeout}
    tool_execute_start: list[dict[str, Any]] = Field(default_factory=list)
    tool_execute_end: list[dict[str, Any]] = Field(default_factory=list)
    llm_call_start: list[dict[str, Any]] = Field(default_factory=list)
    llm_call_end: list[dict[str, Any]] = Field(default_factory=list)
    engine_run_start: list[dict[str, Any]] = Field(default_factory=list)
    engine_run_end: list[dict[str, Any]] = Field(default_factory=list)
    agent_execute_start: list[dict[str, Any]] = Field(default_factory=list)
    agent_execute_end: list[dict[str, Any]] = Field(default_factory=list)
    delegate_start: list[dict[str, Any]] = Field(default_factory=list)
    delegate_end: list[dict[str, Any]] = Field(default_factory=list)
    message_in: list[dict[str, Any]] = Field(default_factory=list)
    message_out: list[dict[str, Any]] = Field(default_factory=list)


class SkillsConfig(BaseSettings):
    """Skill 搜索路径配置.

    Example YAML::

        skills:
          search_paths:
            - .naumi/skills/
            - ~/.naumi/skills/
    """

    model_config = SettingsConfigDict(env_prefix="NAUMI_SKILLS__")

    search_paths: list[str] = Field(default_factory=lambda: [])


class BrowserDaemonConfig(BaseSettings):
    """browser-debugging-daemon HTTP adapter configuration."""

    model_config = SettingsConfigDict(env_prefix="NAUMI_BROWSER_DAEMON__")

    enabled: bool = True
    base_url: str = "http://127.0.0.1:3005"
    token: str | None = None
    project_dir: str = Field(
        default_factory=lambda: str(Path.home() / "Workspace" / "browser-debugging-daemon")
    )
    request_timeout_seconds: float = 20.0
    startup_timeout_seconds: float = 8.0


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
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    browser_daemon: BrowserDaemonConfig = Field(default_factory=BrowserDaemonConfig)
    keybindings: dict[str, str | list[str]] = Field(default_factory=dict)
    workspace_root: str = Field(default_factory=lambda: str(Path.cwd()))
    custom_tools_dir: str | None = None
    log_level: str = "INFO"

    def resolve_workspace_root(self) -> Path:
        """Return the absolute workspace root used by relative file and shell tools."""
        root = Path(self.workspace_root).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        return root.resolve()

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        p = Path(path).resolve()
        if not p.exists():
            logger.warning("Config file not found: %s, using defaults + env vars", p)
            return cls()
        logger.debug("Loading config from %s", p)
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        config = cls(**data)
        config._resolve_runtime_paths(p.parent)
        return config

    def _resolve_runtime_paths(self, base_dir: Path) -> None:
        """Anchor persistent runtime paths to the config file directory.

        The CLI can be launched from any workspace. Persistent data must not
        drift with the process cwd, otherwise `/resume` and debug replay read a
        different SQLite/debug directory depending on where the user started
        the command.
        """
        self.memory.session_db_path = _anchor_path(
            self.memory.session_db_path,
            base_dir,
        )
        self.memory.vector_db_path = _anchor_path(
            self.memory.vector_db_path,
            base_dir,
        )


def _anchor_path(path: str, base_dir: Path) -> str:
    """Return an absolute path, resolving relative values against base_dir."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = base_dir / p
    return str(p.resolve())
