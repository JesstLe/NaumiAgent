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


class SessionRetentionConfig(BaseSettings):
    """Archived Session retention preview limits."""

    delete_archived_after_days: int = Field(default=30, ge=1)
    max_archived_session_bytes: int = Field(default=0, ge=0)
    max_sessions_per_pass: int = Field(default=20, ge=1, le=10_000)
    max_bytes_per_pass: int = Field(default=256 * 1024 * 1024, ge=1)
    scan_limit: int = Field(default=10_000, ge=1, le=10_000)
    max_runtime_seconds: float = Field(default=10.0, gt=0, le=300)
    periodic_enabled: bool = False
    interval_seconds: float = Field(default=300.0, gt=0, le=86_400)
    max_empty_backoff_seconds: float = Field(default=1800.0, gt=0, le=604_800)
    worker_lease_seconds: int = Field(default=60, ge=1, le=86_400)
    standby_retry_seconds: float = Field(default=15.0, gt=0, le=3600)
    jitter_ratio: float = Field(default=0.1, ge=0, le=0.5)

    @model_validator(mode="after")
    def _validate_periodic_worker(self) -> SessionRetentionConfig:
        if self.max_empty_backoff_seconds < self.interval_seconds:
            raise ValueError("max_empty_backoff_seconds 不能小于 interval_seconds")
        if self.worker_lease_seconds <= self.max_runtime_seconds:
            raise ValueError("worker_lease_seconds 必须大于 max_runtime_seconds")
        return self


class MemoryConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_MEMORY__")

    session_db_path: str = "data/sessions.db"
    vector_db_path: str = "data/chroma"
    compaction_threshold: float = 0.75
    compaction_reserved_tokens: int = 20_000
    long_term_enabled: bool = True
    session_retention: SessionRetentionConfig = Field(
        default_factory=SessionRetentionConfig
    )


class SafetyConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_SAFETY__")

    permission_mode: str = "moderate"
    allowed_dirs: list[str] = Field(default_factory=lambda: ["/workspace", str(Path.cwd())])
    max_budget_usd: float | None = Field(default=None, ge=0)
    max_turns: int = Field(default=DEFAULT_RUNTIME_MAX_TURNS, ge=1)
    max_parallel_tools: int = Field(default=4, ge=1, le=16)
    max_parallel_agents: int = Field(default=4, ge=1, le=32)
    max_queued_agents: int = Field(default=64, ge=0, le=10_000)
    max_parallel_sandbox_batches: int = Field(default=2, ge=1, le=32)
    max_queued_sandbox_batches: int = Field(default=8, ge=0, le=10_000)
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


class BrowserAutomationConfig(BaseSettings):
    """In-process browser task queue and isolation limits."""

    model_config = SettingsConfigDict(env_prefix="NAUMI_BROWSER__")

    replay_recording_enabled: bool = False
    max_concurrent_runs: int = Field(default=2, ge=1, le=8)
    run_history_limit: int = Field(default=200, ge=20, le=5000)


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


class PiEngineConfig(BaseSettings):
    """Process-level settings for the external pi coding agent engine."""

    model_config = SettingsConfigDict(env_prefix="NAUMI_ENGINE__PI__")

    binary: str = "pi"
    provider: str | None = None
    model: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    # Identity block appended to pi's system prompt: None uses the
    # NaumiAgent default, "" disables injection, custom text is verbatim.
    system_prompt_append: str | None = None
    # A run whose pi event stream is silent for this many seconds is
    # aborted with an explicit error instead of hanging forever.
    stall_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    # Extra environment variables for the pi child process. Values may
    # reference an existing variable with "{env:NAME}" so keys never land in
    # the YAML file itself.
    env: dict[str, str] = Field(default_factory=dict)


class EngineConfig(BaseSettings):
    """Which agent engine powers interactive terminal sessions.

    ``naumi`` keeps the built-in AgentEngine as the conversation backend;
    ``pi`` drives the external pi coding agent through its RPC protocol while
    the NaumiAgent frontends stay unchanged.
    """

    model_config = SettingsConfigDict(env_prefix="NAUMI_ENGINE__")

    provider: Literal["naumi", "pi"] = "naumi"
    pi: PiEngineConfig = Field(default_factory=PiEngineConfig)


class RuntimeHeartbeatRetentionConfig(BaseSettings):
    """Safe bounded retention for terminal runtime heartbeat records."""

    enabled: bool = True
    retention_days: int = Field(default=7, ge=3, le=365)
    interval_seconds: float = Field(default=21_600, gt=0, le=604_800)
    standby_retry_seconds: float = Field(default=60, gt=0, le=3600)
    lease_seconds: int = Field(default=60, ge=3, le=86_400)
    scan_limit: int = Field(default=100, ge=1, le=1000)
    catalog_limit: int = Field(default=200, ge=1, le=200)


class PursuitTerminalOutboxConfig(BaseSettings):
    """Bounded automatic recovery for admitted Pursuit terminal outbox."""

    enabled: bool = True
    interval_seconds: float = Field(default=30.0, ge=0.1, le=86_400)
    max_empty_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    claim_lease_seconds: int = Field(default=60, ge=3, le=300)
    scan_limit: int = Field(default=20, ge=1, le=1000)
    reconcile_grace_seconds: float = Field(default=30.0, ge=1, le=86_400)
    retry_base_seconds: float = Field(default=5.0, ge=1, le=3600)
    retry_max_seconds: float = Field(default=300.0, ge=1, le=3600)
    max_attempts: int = Field(default=8, ge=1, le=1000)
    jitter_ratio: float = Field(default=0.1, ge=0, le=0.5)

    @model_validator(mode="after")
    def _validate_terminal_outbox(self) -> PursuitTerminalOutboxConfig:
        if self.max_empty_backoff_seconds < self.interval_seconds:
            raise ValueError(
                "terminal outbox max_empty_backoff_seconds 不能小于 interval_seconds"
            )
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError(
                "terminal outbox retry_max_seconds 不能小于 retry_base_seconds"
            )
        return self


class AgentPublicationRecoveryConfig(BaseSettings):
    """Bounded periodic recovery for durable Agent result publications."""

    enabled: bool = True
    interval_seconds: float = Field(default=30.0, ge=0.1, le=86_400)
    max_empty_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    max_failure_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    scan_limit: int = Field(default=100, ge=1, le=1000)
    max_attempts: int = Field(default=5, ge=1, le=1000)
    jitter_ratio: float = Field(default=0.1, ge=0, le=0.5)

    @model_validator(mode="after")
    def _validate_agent_publication_recovery(
        self,
    ) -> AgentPublicationRecoveryConfig:
        if self.max_empty_backoff_seconds < self.interval_seconds:
            raise ValueError(
                "Agent publication max_empty_backoff_seconds 不能小于 interval_seconds"
            )
        if self.max_failure_backoff_seconds < self.interval_seconds:
            raise ValueError(
                "Agent publication max_failure_backoff_seconds 不能小于 interval_seconds"
            )
        return self


class StableRemoteFinalizationDeliveryWorkerConfig(BaseSettings):
    """Bounded delivery worker policy; transport is injected by runtime composition."""

    enabled: bool = True
    interval_seconds: float = Field(default=30.0, ge=0.1, le=86_400)
    max_empty_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    max_failure_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    claim_lease_seconds: int = Field(default=60, ge=3, le=300)
    scan_limit: int = Field(default=20, ge=1, le=1000)
    ack_timeout_seconds: float = Field(default=20.0, ge=0.1, le=299.9)
    retry_base_seconds: float = Field(default=5.0, ge=0.1, le=3600)
    retry_max_seconds: float = Field(default=300.0, ge=0.1, le=3600)
    max_attempts: int = Field(default=8, ge=1, le=1000)
    shutdown_drain_seconds: float = Field(default=25.0, ge=0.1, le=3600)
    jitter_ratio: float = Field(default=0.1, ge=0, le=0.5)

    @model_validator(mode="after")
    def _validate_stable_remote_delivery(
        self,
    ) -> StableRemoteFinalizationDeliveryWorkerConfig:
        if self.max_empty_backoff_seconds < self.interval_seconds:
            raise ValueError("Remote Finalization 空轮退避不能小于 interval")
        if self.max_failure_backoff_seconds < self.interval_seconds:
            raise ValueError("Remote Finalization 失败退避不能小于 interval")
        if self.ack_timeout_seconds >= self.claim_lease_seconds:
            raise ValueError("Remote Finalization ACK timeout 必须小于 claim lease")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("Remote Finalization retry max 不能小于 retry base")
        return self


class StableRemoteFinalizationResultReturnWorkerConfig(BaseSettings):
    """Target execution and durable Result return worker policy."""

    enabled: bool = True
    interval_seconds: float = Field(default=30.0, ge=0.1, le=86_400)
    max_empty_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    max_failure_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    journal_scan_limit: int = Field(default=20, ge=1, le=1000)
    return_scan_limit: int = Field(default=20, ge=1, le=1000)
    claim_lease_seconds: int = Field(default=60, ge=3, le=300)
    result_timeout_seconds: float = Field(default=20.0, ge=0.1, le=299.9)
    retry_base_seconds: float = Field(default=5.0, ge=0.1, le=3600)
    retry_max_seconds: float = Field(default=300.0, ge=0.1, le=3600)
    max_attempts: int = Field(default=8, ge=1, le=1000)
    shutdown_drain_seconds: float = Field(default=25.0, ge=0.1, le=3600)
    jitter_ratio: float = Field(default=0.1, ge=0, le=0.5)

    @model_validator(mode="after")
    def _validate_result_return(
        self,
    ) -> StableRemoteFinalizationResultReturnWorkerConfig:
        if self.max_empty_backoff_seconds < self.interval_seconds:
            raise ValueError("Result Return 空轮退避不能小于 interval")
        if self.max_failure_backoff_seconds < self.interval_seconds:
            raise ValueError("Result Return 失败退避不能小于 interval")
        if self.result_timeout_seconds >= self.claim_lease_seconds:
            raise ValueError("Result Return timeout 必须小于 claim lease")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("Result Return retry max 不能小于 retry base")
        return self


class StablePromotionRuntimeAdmissionDeliveryWorkerConfig(BaseSettings):
    """Bounded signed Runtime Admission delivery worker policy."""

    enabled: bool = True
    interval_seconds: float = Field(default=30.0, ge=0.1, le=86_400)
    max_empty_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    max_failure_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    claim_lease_seconds: int = Field(default=60, ge=3, le=300)
    scan_limit: int = Field(default=20, ge=1, le=1000)
    receipt_timeout_seconds: float = Field(default=20.0, ge=0.1, le=299.9)
    retry_base_seconds: float = Field(default=5.0, ge=0.1, le=3600)
    retry_max_seconds: float = Field(default=300.0, ge=0.1, le=3600)
    max_attempts: int = Field(default=8, ge=1, le=1000)
    shutdown_drain_seconds: float = Field(default=25.0, ge=0.1, le=3600)
    jitter_ratio: float = Field(default=0.1, ge=0, le=0.5)

    @model_validator(mode="after")
    def _validate_runtime_admission_delivery(
        self,
    ) -> StablePromotionRuntimeAdmissionDeliveryWorkerConfig:
        if self.max_empty_backoff_seconds < self.interval_seconds:
            raise ValueError("Runtime Admission 空轮退避不能小于 interval")
        if self.max_failure_backoff_seconds < self.interval_seconds:
            raise ValueError("Runtime Admission 失败退避不能小于 interval")
        if self.receipt_timeout_seconds >= self.claim_lease_seconds:
            raise ValueError("Runtime Admission Receipt timeout 必须小于 claim lease")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("Runtime Admission retry max 不能小于 retry base")
        return self


class StablePromotionObservationRevisionDeliveryWorkerConfig(BaseSettings):
    """Bounded fenced Observation Revision delivery worker policy."""

    enabled: bool = True
    interval_seconds: float = Field(default=30.0, ge=0.1, le=86_400)
    max_empty_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    max_failure_backoff_seconds: float = Field(default=300.0, ge=0.1, le=604_800)
    claim_lease_seconds: int = Field(default=60, ge=3, le=300)
    scan_limit: int = Field(default=20, ge=1, le=1000)
    receipt_timeout_seconds: float = Field(default=20.0, ge=0.1, le=299.9)
    retry_base_seconds: float = Field(default=5.0, ge=0.1, le=3600)
    retry_max_seconds: float = Field(default=300.0, ge=0.1, le=3600)
    max_attempts: int = Field(default=8, ge=1, le=1000)
    shutdown_drain_seconds: float = Field(default=25.0, ge=0.1, le=3600)
    jitter_ratio: float = Field(default=0.1, ge=0, le=0.5)

    @model_validator(mode="after")
    def _validate_observation_revision_delivery(
        self,
    ) -> StablePromotionObservationRevisionDeliveryWorkerConfig:
        if self.max_empty_backoff_seconds < self.interval_seconds:
            raise ValueError("Observation Revision 空轮退避不能小于 interval")
        if self.max_failure_backoff_seconds < self.interval_seconds:
            raise ValueError("Observation Revision 失败退避不能小于 interval")
        if self.receipt_timeout_seconds >= self.claim_lease_seconds:
            raise ValueError("Observation Revision Receipt timeout 必须小于 claim lease")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("Observation Revision retry max 不能小于 retry base")
        return self


class StableRemoteFinalizationHTTPTransportConfig(BaseSettings):
    """Authenticated control-plane to installation HTTPS transport."""

    enabled: bool = False
    endpoint_url: str = ""
    server_ca_path: str = ""
    client_certificate_path: str = ""
    client_private_key_path: str = ""
    server_certificate_sha256_pins: list[str] = Field(default_factory=list)
    connect_timeout_seconds: float = Field(default=5.0, ge=0.1, le=120)
    request_timeout_seconds: float = Field(default=15.0, ge=0.1, le=600)
    max_response_bytes: int = Field(default=512 * 1024, ge=1, le=512 * 1024)

    @model_validator(mode="after")
    def _validate_stable_remote_http(
        self,
    ) -> StableRemoteFinalizationHTTPTransportConfig:
        configured = bool(
            self.endpoint_url
            or self.server_ca_path
            or self.client_certificate_path
            or self.client_private_key_path
            or self.server_certificate_sha256_pins
        )
        if configured and not self.enabled:
            raise ValueError("Remote Finalization HTTP 已配置证书或端点，但未显式 enabled")
        if self.enabled and not all((
            self.endpoint_url,
            self.server_ca_path,
            self.client_certificate_path,
            self.client_private_key_path,
        )):
            raise ValueError("Remote Finalization HTTP 启用时必须完整配置端点与 mTLS 文件")
        if self.enabled and not 1 <= len(self.server_certificate_sha256_pins) <= 2:
            raise ValueError("Remote Finalization HTTP 必须配置 1–2 个服务端证书 pin")
        if self.enabled and any(
            re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in self.server_certificate_sha256_pins
        ):
            raise ValueError("Remote Finalization HTTP 服务端证书 pin 必须是小写 SHA-256")
        if self.request_timeout_seconds < self.connect_timeout_seconds:
            raise ValueError("Remote Finalization HTTP request timeout 不能小于 connect timeout")
        return self


class StableRemoteFinalizationResultHTTPTransportConfig(BaseSettings):
    """Authenticated installation-to-control-plane Result HTTPS transport."""

    enabled: bool = False
    endpoint_url: str = ""
    server_ca_path: str = ""
    client_certificate_path: str = ""
    client_private_key_path: str = ""
    server_certificate_sha256_pins: list[str] = Field(default_factory=list)
    connect_timeout_seconds: float = Field(default=5.0, ge=0.1, le=120)
    request_timeout_seconds: float = Field(default=15.0, ge=0.1, le=600)
    max_response_bytes: int = Field(default=512 * 1024, ge=1, le=512 * 1024)

    @model_validator(mode="after")
    def _validate_result_http(
        self,
    ) -> StableRemoteFinalizationResultHTTPTransportConfig:
        configured = bool(
            self.endpoint_url
            or self.server_ca_path
            or self.client_certificate_path
            or self.client_private_key_path
            or self.server_certificate_sha256_pins
        )
        if configured and not self.enabled:
            raise ValueError("Result HTTP 已配置证书或端点，但未显式 enabled")
        if self.enabled and not all((
            self.endpoint_url,
            self.server_ca_path,
            self.client_certificate_path,
            self.client_private_key_path,
        )):
            raise ValueError("Result HTTP 启用时必须完整配置端点与 mTLS 文件")
        if self.enabled and not 1 <= len(self.server_certificate_sha256_pins) <= 2:
            raise ValueError("Result HTTP 必须配置 1–2 个控制平面证书 pin")
        if self.enabled and any(
            re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in self.server_certificate_sha256_pins
        ):
            raise ValueError("Result HTTP 控制平面证书 pin 必须是小写 SHA-256")
        if self.request_timeout_seconds < self.connect_timeout_seconds:
            raise ValueError("Result HTTP request timeout 不能小于 connect timeout")
        return self


class StableRemoteFinalizationInstallationDaemonConfig(BaseSettings):
    """Owner-fenced installation daemon and inbound mTLS endpoint policy."""

    enabled: bool = False
    installation_member_id: str = ""
    bind_host: str = "127.0.0.1"
    advertise_host: str = "localhost"
    port: int = Field(default=0, ge=0, le=65_535)
    server_certificate_path: str = ""
    server_private_key_path: str = ""
    control_plane_ca_path: str = ""
    authorized_control_plane_certificate_sha256: list[str] = Field(
        default_factory=list
    )
    max_request_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=2 * 1024 * 1024)
    tls_handshake_timeout_seconds: float = Field(default=5.0, ge=0.1, le=120)
    request_timeout_seconds: float = Field(default=20.0, ge=0.1, le=600)
    requests_per_minute: int = Field(default=120, ge=1, le=100_000)
    max_concurrent_requests: int = Field(default=32, ge=1, le=1024)
    lease_seconds: int = Field(default=60, ge=10, le=86_400)
    renew_interval_seconds: float = Field(default=10.0, ge=0.1, le=28_800)
    heartbeat_interval_seconds: float = Field(default=10.0, ge=0.1, le=86_399.9)
    heartbeat_timeout_seconds: int = Field(default=30, ge=3, le=86_400)

    @model_validator(mode="after")
    def _validate_installation_daemon(
        self,
    ) -> StableRemoteFinalizationInstallationDaemonConfig:
        configured = bool(
            self.installation_member_id
            or self.server_certificate_path
            or self.server_private_key_path
            or self.control_plane_ca_path
            or self.authorized_control_plane_certificate_sha256
        )
        if configured and not self.enabled:
            raise ValueError("Installation daemon 已配置身份或证书，但未显式 enabled")
        if self.enabled and not all((
            self.installation_member_id,
            self.server_certificate_path,
            self.server_private_key_path,
            self.control_plane_ca_path,
        )):
            raise ValueError("Installation daemon 启用时必须完整配置成员与 mTLS 文件")
        if self.enabled and re.fullmatch(
            r"relpopmember_[0-9a-f]{24}", self.installation_member_id
        ) is None:
            raise ValueError("Installation daemon member ID 无效")
        if self.enabled and not 1 <= len(
            self.authorized_control_plane_certificate_sha256
        ) <= 2:
            raise ValueError("Installation daemon 必须配置 1–2 个 Control Plane 证书 pin")
        if self.enabled and any(
            re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in self.authorized_control_plane_certificate_sha256
        ):
            raise ValueError("Installation daemon Control Plane 证书 pin 必须是小写 SHA-256")
        if self.renew_interval_seconds > self.lease_seconds / 3:
            raise ValueError("Installation daemon renew interval 不能大于 lease 的三分之一")
        if self.heartbeat_interval_seconds >= self.heartbeat_timeout_seconds:
            raise ValueError("Installation daemon heartbeat interval 必须小于 timeout")
        return self


class StablePromotionRuntimeAdmissionHTTPTransportConfig(BaseSettings):
    """Authenticated installation-to-Control-Plane Runtime Admission HTTPS."""

    enabled: bool = False
    endpoint_url: str = ""
    server_ca_path: str = ""
    client_certificate_path: str = ""
    client_private_key_path: str = ""
    server_certificate_sha256_pins: list[str] = Field(default_factory=list)
    connect_timeout_seconds: float = Field(default=5.0, ge=0.1, le=120)
    request_timeout_seconds: float = Field(default=15.0, ge=0.1, le=600)
    max_response_bytes: int = Field(default=128 * 1024, ge=1, le=128 * 1024)

    @model_validator(mode="after")
    def _validate_runtime_admission_http(
        self,
    ) -> StablePromotionRuntimeAdmissionHTTPTransportConfig:
        configured = bool(
            self.endpoint_url
            or self.server_ca_path
            or self.client_certificate_path
            or self.client_private_key_path
            or self.server_certificate_sha256_pins
        )
        if configured and not self.enabled:
            raise ValueError(
                "Runtime Admission HTTP 已配置证书或端点，但未显式 enabled"
            )
        if self.enabled and not all((
            self.endpoint_url,
            self.server_ca_path,
            self.client_certificate_path,
            self.client_private_key_path,
        )):
            raise ValueError("Runtime Admission HTTP 启用时必须完整配置端点与 mTLS 文件")
        if self.enabled and not 1 <= len(self.server_certificate_sha256_pins) <= 2:
            raise ValueError("Runtime Admission HTTP 必须配置 1–2 个 Control Plane pin")
        if self.enabled and any(
            re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in self.server_certificate_sha256_pins
        ):
            raise ValueError("Runtime Admission HTTP Control Plane pin 必须是小写 SHA-256")
        if self.request_timeout_seconds < self.connect_timeout_seconds:
            raise ValueError(
                "Runtime Admission HTTP request timeout 不能小于 connect timeout"
            )
        return self


class StablePromotionObservationRevisionHTTPTransportConfig(BaseSettings):
    """Authenticated installation-to-Control-Plane revision HTTPS."""

    enabled: bool = False
    endpoint_url: str = ""
    server_ca_path: str = ""
    client_certificate_path: str = ""
    client_private_key_path: str = ""
    server_certificate_sha256_pins: list[str] = Field(default_factory=list)
    connect_timeout_seconds: float = Field(default=5.0, ge=0.1, le=120)
    request_timeout_seconds: float = Field(default=15.0, ge=0.1, le=600)
    max_response_bytes: int = Field(default=128 * 1024, ge=1, le=128 * 1024)

    @model_validator(mode="after")
    def _validate_observation_revision_http(
        self,
    ) -> StablePromotionObservationRevisionHTTPTransportConfig:
        configured = bool(
            self.endpoint_url
            or self.server_ca_path
            or self.client_certificate_path
            or self.client_private_key_path
            or self.server_certificate_sha256_pins
        )
        if configured and not self.enabled:
            raise ValueError(
                "Observation Revision HTTP 已配置证书或端点，但未显式 enabled"
            )
        if self.enabled and not all((
            self.endpoint_url,
            self.server_ca_path,
            self.client_certificate_path,
            self.client_private_key_path,
        )):
            raise ValueError(
                "Observation Revision HTTP 启用时必须完整配置端点与 mTLS 文件"
            )
        if self.enabled and not 1 <= len(self.server_certificate_sha256_pins) <= 2:
            raise ValueError(
                "Observation Revision HTTP 必须配置 1–2 个 Control Plane pin"
            )
        if self.enabled and any(
            re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in self.server_certificate_sha256_pins
        ):
            raise ValueError(
                "Observation Revision HTTP Control Plane pin 必须是小写 SHA-256"
            )
        if self.request_timeout_seconds < self.connect_timeout_seconds:
            raise ValueError(
                "Observation Revision HTTP request timeout 不能小于 connect timeout"
            )
        return self


class HarnessConfig(BaseSettings):
    """Harness runtime policy configuration."""

    model_config = SettingsConfigDict(env_prefix="NAUMI_HARNESS__")

    runtime_heartbeat_retention: RuntimeHeartbeatRetentionConfig = Field(
        default_factory=RuntimeHeartbeatRetentionConfig
    )
    pursuit_terminal_outbox: PursuitTerminalOutboxConfig = Field(
        default_factory=PursuitTerminalOutboxConfig
    )
    agent_publication_recovery: AgentPublicationRecoveryConfig = Field(
        default_factory=AgentPublicationRecoveryConfig
    )
    stable_remote_finalization_delivery: (
        StableRemoteFinalizationDeliveryWorkerConfig
    ) = Field(default_factory=StableRemoteFinalizationDeliveryWorkerConfig)
    stable_remote_finalization_http_transport: (
        StableRemoteFinalizationHTTPTransportConfig
    ) = Field(default_factory=StableRemoteFinalizationHTTPTransportConfig)
    stable_remote_finalization_result_return: (
        StableRemoteFinalizationResultReturnWorkerConfig
    ) = Field(default_factory=StableRemoteFinalizationResultReturnWorkerConfig)
    stable_promotion_runtime_admission_delivery: (
        StablePromotionRuntimeAdmissionDeliveryWorkerConfig
    ) = Field(default_factory=StablePromotionRuntimeAdmissionDeliveryWorkerConfig)
    stable_promotion_observation_revision_delivery: (
        StablePromotionObservationRevisionDeliveryWorkerConfig
    ) = Field(
        default_factory=StablePromotionObservationRevisionDeliveryWorkerConfig
    )
    stable_promotion_runtime_admission_http_transport: (
        StablePromotionRuntimeAdmissionHTTPTransportConfig
    ) = Field(default_factory=StablePromotionRuntimeAdmissionHTTPTransportConfig)
    stable_promotion_observation_revision_http_transport: (
        StablePromotionObservationRevisionHTTPTransportConfig
    ) = Field(default_factory=StablePromotionObservationRevisionHTTPTransportConfig)
    stable_remote_finalization_result_http_transport: (
        StableRemoteFinalizationResultHTTPTransportConfig
    ) = Field(default_factory=StableRemoteFinalizationResultHTTPTransportConfig)
    stable_remote_finalization_installation_daemon: (
        StableRemoteFinalizationInstallationDaemonConfig
    ) = Field(default_factory=StableRemoteFinalizationInstallationDaemonConfig)

    @model_validator(mode="after")
    def _validate_stable_remote_transport_timeouts(self) -> HarnessConfig:
        http = self.stable_remote_finalization_http_transport
        delivery = self.stable_remote_finalization_delivery
        if http.enabled and http.request_timeout_seconds >= delivery.ack_timeout_seconds:
            raise ValueError(
                "Remote Finalization HTTP request timeout 必须小于 Worker ACK timeout"
            )
        result_http = self.stable_remote_finalization_result_http_transport
        result_return = self.stable_remote_finalization_result_return
        if (
            result_http.enabled
            and result_http.request_timeout_seconds
            >= result_return.result_timeout_seconds
        ):
            raise ValueError(
                "Result HTTP request timeout 必须小于 Result Worker timeout"
            )
        admission_http = self.stable_promotion_runtime_admission_http_transport
        admission_delivery = self.stable_promotion_runtime_admission_delivery
        if (
            admission_http.enabled
            and admission_http.request_timeout_seconds
            >= admission_delivery.receipt_timeout_seconds
        ):
            raise ValueError(
                "Runtime Admission HTTP request timeout 必须小于 Worker Receipt timeout"
            )
        revision_http = self.stable_promotion_observation_revision_http_transport
        revision_delivery = self.stable_promotion_observation_revision_delivery
        if (
            revision_http.enabled
            and revision_http.request_timeout_seconds
            >= revision_delivery.receipt_timeout_seconds
        ):
            raise ValueError(
                "Observation Revision HTTP request timeout 必须小于 Worker Receipt timeout"
            )
        daemon = self.stable_remote_finalization_installation_daemon
        if daemon.enabled and not (
            result_return.enabled and result_http.enabled
        ):
            raise ValueError(
                "Installation daemon 必须同时启用 Result Worker 与认证 Result HTTP"
            )
        if daemon.enabled and daemon.lease_seconds <= (
            result_return.shutdown_drain_seconds
            + 2 * daemon.renew_interval_seconds
        ):
            raise ValueError(
                "Installation daemon lease 必须覆盖 Worker drain 与两个续租周期"
            )
        return self


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
    browser: BrowserAutomationConfig = Field(default_factory=BrowserAutomationConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
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
