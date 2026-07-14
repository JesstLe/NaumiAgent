"""全局配置 — YAML + 环境变量."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from naumi_agent.config.credentials import CredentialStoreError, load_model_api_key
from naumi_agent.model.reasoning import ReasoningEffort, ReasoningEffortSetting

logger = logging.getLogger(__name__)

DEFAULT_RUNTIME_MAX_TURNS = 50
_ENV_SECRET_REF = re.compile(r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")
_FRESHNESS = re.compile(
    r"^(?:pd|pw|pm|py|\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2})$"
)


class ModelMeta(BaseSettings):
    """单个模型的元数据覆盖（上下文窗口、价格等）."""

    max_context: int | None = Field(default=None, gt=0)
    max_output: int | None = Field(default=None, gt=0)
    input_cost_per_million: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    output_cost_per_million: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    supports_parallel_tools: bool | None = None
    supports_structured_output: bool | None = None
    supports_reasoning: bool | None = None
    supports_vision: bool | None = None
    input_modalities: tuple[str, ...] | None = None
    output_modalities: tuple[str, ...] | None = None
    reasoning_effort: ReasoningEffortSetting | None = None
    reasoning_efforts: tuple[ReasoningEffort, ...] | None = None
    default_reasoning_effort: ReasoningEffort | None = None

    @field_validator("reasoning_efforts", mode="before")
    @classmethod
    def _reasoning_efforts_must_be_non_empty(
        cls,
        value: object,
    ) -> object:
        if value is None:
            return value
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("reasoning_efforts 必须是非空数组")
        return value

    @model_validator(mode="after")
    def _validate_reasoning_capability(self) -> ModelMeta:
        if (
            self.max_context is not None
            and self.max_output is not None
            and self.max_output > self.max_context
        ):
            raise ValueError("max_output 不能大于 max_context")
        for name, modalities in (
            ("input_modalities", self.input_modalities),
            ("output_modalities", self.output_modalities),
        ):
            if modalities is None:
                continue
            if not modalities or any(not value.strip() for value in modalities):
                raise ValueError(f"{name} 必须是非空字符串数组")
            if len(set(modalities)) != len(modalities):
                raise ValueError(f"{name} 不能包含重复值")
        if self.supports_vision is False and self.input_modalities is not None:
            if "image" in self.input_modalities:
                raise ValueError(
                    "supports_vision=false 时 input_modalities 不能声明 image"
                )
        if self.supports_tools is False and self.supports_parallel_tools is True:
            raise ValueError(
                "supports_tools=false 时 supports_parallel_tools 不能为 true"
            )
        if (
            self.supports_reasoning is False
            and self.reasoning_effort is not None
            and self.reasoning_effort is not ReasoningEffortSetting.AUTO
        ):
            raise ValueError(
                "supports_reasoning=false 时不能设置 reasoning_effort"
            )
        efforts = self.reasoning_efforts
        if efforts is None:
            if self.default_reasoning_effort is not None:
                raise ValueError(
                    "default_reasoning_effort 需要同时声明 reasoning_efforts"
                )
            return self
        if self.supports_reasoning is False:
            raise ValueError(
                "supports_reasoning=false 时不能声明 reasoning_efforts"
            )
        if len(set(efforts)) != len(efforts):
            raise ValueError("reasoning_efforts 不能包含重复值")
        if (
            self.default_reasoning_effort is not None
            and self.default_reasoning_effort not in efforts
        ):
            raise ValueError(
                "default_reasoning_effort 必须出现在 reasoning_efforts 中"
            )
        return self


class ModelConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_MODEL__")

    provider: str | None = None
    catalog_path: str | None = None
    default_model: str = "claude-sonnet-4-6"
    fast_model: str = "claude-haiku-4-5"
    reasoning_model: str = "claude-opus-4-7"
    reasoning_effort: ReasoningEffortSetting = ReasoningEffortSetting.AUTO
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
    compaction_reserved_tokens: int = 20_000
    long_term_enabled: bool = True


class SafetyConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_SAFETY__")

    permission_mode: str = "moderate"
    allowed_dirs: list[str] = Field(default_factory=lambda: ["/workspace", str(Path.cwd())])
    max_budget_usd: float | None = Field(default=None, ge=0)
    max_turns: int = Field(default=DEFAULT_RUNTIME_MAX_TURNS, ge=1)
    max_parallel_tools: int = Field(default=4, ge=1, le=16)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)


class MCPConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_MCP__")

    servers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class APIConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_API__")

    host: str = "127.0.0.1"
    port: int = 8765
    workers: int = 1
    api_keys: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    rate_limit_rpm: int = 300


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


class BraveSearchConfig(BaseSettings):
    """Brave Web Search options with an environment-only secret reference."""

    model_config = SettingsConfigDict(hide_input_in_errors=True)

    enabled: bool = True
    api_key_ref: str = "{env:BRAVE_SEARCH_API_KEY}"
    country: str | None = None
    search_lang: str | None = None
    ui_lang: str | None = None
    safesearch: Literal["off", "moderate", "strict"] = "moderate"
    spellcheck: bool = True
    freshness: str | None = None
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)

    @field_validator("api_key_ref")
    @classmethod
    def _validate_api_key_ref(cls, value: str) -> str:
        normalized = value.strip()
        if not _ENV_SECRET_REF.fullmatch(normalized):
            raise ValueError("api_key_ref 仅允许 {env:VARIABLE_NAME} 环境变量引用")
        return normalized

    @field_validator("country")
    @classmethod
    def _validate_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized != "ALL" and not re.fullmatch(r"[A-Z]{2}", normalized):
            raise ValueError("country 必须是两位国家代码或 ALL")
        return normalized

    @field_validator("search_lang")
    @classmethod
    def _validate_search_lang(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]{2,8})?", normalized):
            raise ValueError("search_lang 必须是有效语言代码")
        return normalized

    @field_validator("ui_lang")
    @classmethod
    def _validate_ui_lang(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})?", normalized):
            raise ValueError("ui_lang 必须是有效 locale")
        return normalized

    @field_validator("freshness")
    @classmethod
    def _validate_freshness(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not _FRESHNESS.fullmatch(normalized):
            raise ValueError("freshness 必须是 pd/pw/pm/py 或 YYYY-MM-DDtoYYYY-MM-DD")
        if "to" in normalized:
            start_text, end_text = normalized.split("to", 1)
            try:
                start = date.fromisoformat(start_text)
                end = date.fromisoformat(end_text)
            except ValueError as exc:
                raise ValueError("freshness 日期范围包含无效日期") from exc
            if start > end:
                raise ValueError("freshness 日期范围起始日期不能晚于结束日期")
        return normalized

    def resolve_api_key(self, environ: Mapping[str, str] | None = None) -> str | None:
        """Resolve the configured environment reference without retaining the secret."""
        match = _ENV_SECRET_REF.fullmatch(self.api_key_ref)
        if not self.enabled or match is None:
            return None
        source = os.environ if environ is None else environ
        value = source.get(match.group(1), "").strip()
        return value or None


class SearchConfig(BaseSettings):
    """Ordered web-search routing and provider options."""

    model_config = SettingsConfigDict(env_prefix="NAUMI_SEARCH__")

    provider_order: tuple[Literal["brave", "duckduckgo", "browser"], ...] = (
        "brave",
        "duckduckgo",
        "browser",
    )
    brave: BraveSearchConfig = Field(default_factory=BraveSearchConfig)

    @field_validator("provider_order", mode="before")
    @classmethod
    def _provider_order_must_be_non_empty(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("provider_order 必须是非空数组")
        return value

    @model_validator(mode="after")
    def _provider_order_must_be_unique(self) -> SearchConfig:
        if len(set(self.provider_order)) != len(self.provider_order):
            raise ValueError("provider_order 不能包含重复提供方")
        return self


class UIConfig(BaseSettings):
    """CLI/TUI theme and output verbosity configuration."""

    model_config = SettingsConfigDict(env_prefix="NAUMI_UI__")

    theme: str = "dark"
    output_style: str = "detailed"
    show_reasoning: bool = True


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NAUMI_",
        env_nested_delimiter="__",
        env_file=str(Path(__file__).resolve().parents[3] / ".env"),
        env_file_encoding="utf-8",
        hide_input_in_errors=True,
    )

    models: ModelConfig = Field(default_factory=ModelConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    browser_daemon: BrowserDaemonConfig = Field(default_factory=BrowserDaemonConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
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

    def bind_runtime_workspace(self, launch_dir: str | Path | None = None) -> Path:
        """Bind an interactive run to its launch directory without rewriting YAML."""
        requested = Path.cwd() if launch_dir is None else Path(launch_dir).expanduser()
        if not requested.exists() or not requested.is_dir():
            raise ValueError(f"启动工作区不存在或不是目录：{requested}")

        launch = requested.resolve()
        previous = self.resolve_workspace_root()
        allowed_dirs: list[str] = []
        for raw in self.safety.allowed_dirs:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = launch / candidate
            resolved = candidate.resolve()
            value = str(launch if resolved == previous else resolved)
            if value not in allowed_dirs:
                allowed_dirs.append(value)

        launch_value = str(launch)
        if launch_value not in allowed_dirs:
            allowed_dirs.insert(0, launch_value)
        self.workspace_root = launch_value
        self.safety.allowed_dirs = allowed_dirs
        return launch

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
        if not config.models.api_key:
            try:
                config.models.api_key = load_model_api_key(
                    provider=config.models.provider,
                )
            except CredentialStoreError as exc:
                logger.warning("System credential store unavailable: %s", exc)
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
        if self.models.catalog_path:
            self.models.catalog_path = _anchor_path(
                self.models.catalog_path,
                base_dir,
            )


def _anchor_path(path: str, base_dir: Path) -> str:
    """Return an absolute path, resolving relative values against base_dir."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = base_dir / p
    return str(p.resolve())
