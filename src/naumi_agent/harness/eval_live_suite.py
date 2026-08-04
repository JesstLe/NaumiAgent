"""Declarative repeated Live Eval suites persisted through the shared H5a store."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
    build_eval_model_identity,
    capture_eval_source_identity,
)
from naumi_agent.harness.eval_live import (
    LIVE_EVAL_PROMPT_VERSION,
    LIVE_EVAL_RUNNER_VERSION,
    HarnessLiveEvalError,
    HarnessLiveEvalRequest,
    HarnessLiveEvalRunner,
    HarnessLiveEvalStatus,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalLiveEvidence,
    HarnessEvalMetricObservation,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.fingerprint import TreeFingerprintError
from naumi_agent.runtime.ports.model import ModelPort

LIVE_SUITE_RUNNER_VERSION = "live_suite@1"
MAX_LIVE_SUITE_BYTES = 256 * 1024
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUEST_ID_RE = re.compile(r"^hlivebatch_[0-9a-f]{24}$")
_SAFE_PROVIDER_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}$")
_LOGGER = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessLiveSuiteExpected(_StrictModel):
    exact_match: Literal[True] = True


class HarnessLiveSuiteMetrics(_StrictModel):
    primary: Literal["live_transport_exact_match"]


class HarnessLiveSuiteCaseBudget(_StrictModel):
    max_duration_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    max_cost_usd: float = Field(default=0.02, gt=0.0, le=10.0)
    max_output_tokens: int = Field(default=32, ge=1, le=64)

    @field_validator("max_duration_seconds", "max_cost_usd", mode="before")
    @classmethod
    def _finite_non_boolean_float(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Live Suite 数值预算不得使用布尔值。")
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            raise ValueError("Live Suite 数值预算必须有限。")
        return value

    @field_validator("max_output_tokens", mode="before")
    @classmethod
    def _integer_not_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("max_output_tokens 不得使用布尔值。")
        return value


class HarnessLiveSuiteCase(_StrictModel):
    id: str
    runner: Literal["live_transport_echo"]
    prompt_version: Literal["naumi_live_echo@1"] = LIVE_EVAL_PROMPT_VERSION
    expected: HarnessLiveSuiteExpected
    metrics: HarnessLiveSuiteMetrics
    budget: HarnessLiveSuiteCaseBudget = Field(default_factory=HarnessLiveSuiteCaseBudget)

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _ID_RE.fullmatch(normalized):
            raise ValueError("Live case id 格式无效。")
        return normalized


class HarnessLiveSuiteBudget(_StrictModel):
    max_duration_seconds_per_sample: float = Field(
        default=30.0,
        ge=1.0,
        le=600.0,
    )
    max_cost_usd_per_sample: float = Field(default=0.05, gt=0.0, le=10.0)

    @field_validator(
        "max_duration_seconds_per_sample",
        "max_cost_usd_per_sample",
        mode="before",
    )
    @classmethod
    def _finite_non_boolean_float(cls, value: object) -> object:
        return HarnessLiveSuiteCaseBudget._finite_non_boolean_float(value)


class HarnessLiveEvalSuite(_StrictModel):
    schema_version: Literal[1]
    kind: Literal["live"]
    id: str
    title: str = Field(min_length=1, max_length=200)
    cases: tuple[HarnessLiveSuiteCase, ...] = Field(min_length=1, max_length=10)
    budget: HarnessLiveSuiteBudget = Field(default_factory=HarnessLiveSuiteBudget)
    comparison_policy: HarnessEvalComparisonPolicy = Field(
        default_factory=HarnessEvalComparisonPolicy
    )

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _ID_RE.fullmatch(normalized):
            raise ValueError("Live Suite id 格式无效。")
        return normalized

    @field_validator("title")
    @classmethod
    def _nonempty_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Live Suite title 不能为空。")
        return normalized

    @model_validator(mode="after")
    def _suite_budget_covers_declared_cases(self) -> HarnessLiveEvalSuite:
        case_ids = tuple(case.id for case in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Live Suite 中存在重复 case id。")
        declared_cost = sum(case.budget.max_cost_usd for case in self.cases)
        declared_duration = sum(case.budget.max_duration_seconds for case in self.cases)
        if declared_cost > self.budget.max_cost_usd_per_sample:
            raise ValueError("Live Suite sample 成本预算小于 case 预算总和。")
        if declared_duration > self.budget.max_duration_seconds_per_sample:
            raise ValueError("Live Suite sample 时长预算小于 case 预算总和。")
        return self


class HarnessLiveBatchRequest(_StrictModel):
    schema_version: Literal[1] = 1
    request_id: str
    created_at: str
    batch_id: str
    suite_id: str
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=512)
    repetitions: int = Field(ge=5, le=20)
    max_total_duration_seconds: float = Field(ge=5.0, le=3_600.0)
    max_total_cost_usd: float = Field(gt=0.0, le=10.0)
    runner_version: Literal["live_suite@1"] = LIVE_SUITE_RUNNER_VERSION
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("request_id")
    @classmethod
    def _safe_request_id(cls, value: str) -> str:
        if not _REQUEST_ID_RE.fullmatch(value):
            raise ValueError("Live batch request_id 格式无效。")
        return value

    @field_validator("created_at")
    @classmethod
    def _canonical_utc_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Live batch created_at 必须是 ISO-8601 时间。") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise ValueError("Live batch created_at 必须使用 UTC 时区。")
        canonical = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if value != canonical:
            raise ValueError("Live batch created_at 必须使用规范 UTC 格式。")
        return value

    @field_validator("batch_id")
    @classmethod
    def _safe_batch_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _BATCH_RE.fullmatch(normalized):
            raise ValueError("Live batch_id 格式无效。")
        return normalized

    @field_validator("suite_id")
    @classmethod
    def _safe_suite_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _ID_RE.fullmatch(normalized):
            raise ValueError("Live suite_id 格式无效。")
        return normalized

    @field_validator("model")
    @classmethod
    def _safe_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("Live batch model 包含控制字符。")
        return normalized

    @field_validator(
        "max_total_duration_seconds",
        "max_total_cost_usd",
        mode="before",
    )
    @classmethod
    def _finite_non_boolean_float(cls, value: object) -> object:
        return HarnessLiveSuiteCaseBudget._finite_non_boolean_float(value)

    @field_validator("repetitions", mode="before")
    @classmethod
    def _integer_not_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("repetitions 不得使用布尔值。")
        return value

    @model_validator(mode="after")
    def _digest_matches(self) -> HarnessLiveBatchRequest:
        expected = _sha256_payload(self.model_dump(mode="json", exclude={"request_sha256"}))
        if not hmac.compare_digest(expected, self.request_sha256):
            raise ValueError("Live batch request_sha256 与请求内容不一致。")
        return self

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        created_at: str,
        batch_id: str,
        suite_id: str,
        suite_sha256: str,
        profile_sha256: str,
        model: str,
        repetitions: int,
        max_total_duration_seconds: float,
        max_total_cost_usd: float,
    ) -> HarnessLiveBatchRequest:
        raw = {
            "schema_version": 1,
            "request_id": request_id,
            "created_at": created_at,
            "batch_id": batch_id,
            "suite_id": suite_id,
            "suite_sha256": suite_sha256,
            "profile_sha256": profile_sha256,
            "model": model,
            "repetitions": repetitions,
            "max_total_duration_seconds": max_total_duration_seconds,
            "max_total_cost_usd": max_total_cost_usd,
            "runner_version": LIVE_SUITE_RUNNER_VERSION,
        }
        validated = _LiveBatchRequestWithoutDigest.model_validate(raw)
        payload = validated.model_dump(mode="json")
        return cls.model_validate({**payload, "request_sha256": _sha256_payload(payload)})


class _LiveBatchRequestWithoutDigest(_StrictModel):
    schema_version: Literal[1]
    request_id: str
    created_at: str
    batch_id: str
    suite_id: str
    suite_sha256: str
    profile_sha256: str
    model: str
    repetitions: int
    max_total_duration_seconds: float
    max_total_cost_usd: float
    runner_version: str

    @model_validator(mode="after")
    def _reuse_request_contract(self) -> Self:
        HarnessLiveBatchRequest.model_validate(
            {
                **self.model_dump(mode="json"),
                "request_sha256": _sha256_payload(self.model_dump(mode="json")),
            }
        )
        return self


class HarnessLiveBatchStatus(_StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["completed", "partial", "error"]
    code: str = Field(default="", pattern=r"^(?:|[a-z][a-z0-9_]{0,127})$")
    message: str = Field(default="", max_length=500)
    request_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_id: str
    suite_id: str
    model: str
    provider_model: str = Field(default="", max_length=512)
    requested: int = Field(ge=5, le=20)
    completed: int = Field(ge=0, le=20)
    persisted: int = Field(ge=0, le=20)
    total_calls: int = Field(ge=0, le=200)
    total_tokens: int = Field(ge=0)
    total_cost_usd: float = Field(ge=0.0, le=10_000.0)
    duration_ms: float = Field(ge=0.0)
    max_total_duration_seconds: float = Field(ge=5.0, le=3_600.0)
    max_total_cost_usd: float = Field(gt=0.0, le=10.0)
    actual_cost_exceeded: bool = False
    identity_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    baseline_eligible: bool = False
    sample_result_sha256: tuple[str, ...] = Field(default=(), max_length=20)
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("sample_result_sha256")
    @classmethod
    def _sample_digests_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values):
            raise ValueError("Live batch sample digest 格式无效。")
        return values

    @model_validator(mode="after")
    def _status_is_self_consistent(self) -> HarnessLiveBatchStatus:
        expected = _sha256_payload(self.model_dump(mode="json", exclude={"receipt_sha256"}))
        if not hmac.compare_digest(expected, self.receipt_sha256):
            raise ValueError("Live batch receipt_sha256 与回执内容不一致。")
        if not 0 <= self.persisted <= self.completed <= self.requested:
            raise ValueError("Live batch requested/completed/persisted 不一致。")
        if len(self.sample_result_sha256) != self.persisted:
            raise ValueError("Live batch sample digest 数量与 persisted 不一致。")
        if self.actual_cost_exceeded != (self.total_cost_usd > self.max_total_cost_usd):
            raise ValueError("Live batch actual_cost_exceeded 与实际成本不一致。")
        if self.status == "completed" and self.actual_cost_exceeded:
            raise ValueError("completed Live batch 不得超过总成本预算。")
        if self.status == "completed" and (
            self.completed != self.requested or self.persisted != self.requested
        ):
            raise ValueError("completed Live batch 必须完成并保存全部样本。")
        if self.baseline_eligible and (self.status != "completed" or not self.identity_sha256):
            raise ValueError("Live batch Baseline 资格缺少完整 cohort identity。")
        return self


class HarnessLiveBatchProgress(_StrictModel):
    """Factual progress for one paid Live Eval batch."""

    schema_version: Literal[1] = 1
    kind: Literal["live"] = "live"
    stage: Literal[
        "preparing",
        "evaluating",
        "persisting",
        "completed",
        "partial",
        "error",
    ]
    request_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_id: str
    suite_id: str
    model: str
    provider_model: str = Field(default="", max_length=512)
    requested: int = Field(ge=5, le=20)
    completed: int = Field(ge=0, le=20)
    persisted: int = Field(ge=0, le=20)
    total_calls: int = Field(default=0, ge=0, le=200)
    total_tokens: int = Field(default=0, ge=0)
    total_cost_usd: float = Field(default=0.0, ge=0.0, le=10_000.0)
    duration_ms: float = Field(default=0.0, ge=0.0)
    max_total_duration_seconds: float = Field(ge=5.0, le=3_600.0)
    max_total_cost_usd: float = Field(gt=0.0, le=10.0)
    actual_cost_exceeded: bool = False
    identity_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    baseline_eligible: bool = False
    code: str = Field(default="", pattern=r"^(?:|[a-z][a-z0-9_]{0,127})$")
    message: str = Field(default="", max_length=500)

    @field_validator("request_id")
    @classmethod
    def _safe_request_id(cls, value: str) -> str:
        if not _REQUEST_ID_RE.fullmatch(value):
            raise ValueError("Live batch progress request_id 格式无效。")
        return value

    @field_validator("batch_id")
    @classmethod
    def _safe_batch_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _BATCH_RE.fullmatch(normalized):
            raise ValueError("Live batch progress batch_id 格式无效。")
        return normalized

    @field_validator("suite_id")
    @classmethod
    def _safe_suite_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _ID_RE.fullmatch(normalized):
            raise ValueError("Live batch progress suite_id 格式无效。")
        return normalized

    @field_validator("model")
    @classmethod
    def _safe_model(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
        ):
            raise ValueError("Live batch progress model 格式无效。")
        return normalized

    @field_validator("provider_model")
    @classmethod
    def _safe_provider_model(cls, value: str) -> str:
        if value and not _SAFE_PROVIDER_MODEL_RE.fullmatch(value):
            raise ValueError("Live batch progress provider_model 格式无效。")
        return value

    @field_validator(
        "total_cost_usd",
        "duration_ms",
        "max_total_duration_seconds",
        "max_total_cost_usd",
        mode="before",
    )
    @classmethod
    def _finite_non_boolean_float(cls, value: object) -> object:
        return HarnessLiveSuiteCaseBudget._finite_non_boolean_float(value)

    @field_validator(
        "requested",
        "completed",
        "persisted",
        "total_calls",
        "total_tokens",
        mode="before",
    )
    @classmethod
    def _integer_not_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Live batch progress 计数不得使用布尔值。")
        return value

    @model_validator(mode="after")
    def _progress_is_coherent(self) -> HarnessLiveBatchProgress:
        if not 0 <= self.persisted <= self.completed <= self.requested:
            raise ValueError("Live batch 进度必须满足 persisted <= completed <= requested。")
        if self.stage == "preparing" and (self.completed or self.persisted):
            raise ValueError("Live batch preparing 阶段不能声明已完成样本。")
        if self.actual_cost_exceeded != (self.total_cost_usd > self.max_total_cost_usd):
            raise ValueError("Live batch progress 超支标记与实际成本不一致。")
        if self.stage == "completed" and (
            self.completed != self.requested or self.persisted != self.requested
        ):
            raise ValueError("Live batch completed 阶段必须完成并保存全部样本。")
        if self.stage == "completed" and self.actual_cost_exceeded:
            raise ValueError("Live batch completed 阶段不得实际超支。")
        if self.baseline_eligible and (
            self.stage != "completed" or not self.identity_sha256
        ):
            raise ValueError("Live batch 只有完整终态可声明 Baseline 资格。")
        return self


@dataclass(frozen=True, slots=True)
class LoadedLiveEvalSuite:
    path: Path
    display_path: str
    suite: HarnessLiveEvalSuite
    sha256: str


@dataclass(frozen=True, slots=True)
class HarnessLiveBatchExecution:
    request: HarnessLiveBatchRequest
    status: Literal["completed", "partial"]
    code: str
    message: str
    results: tuple[HarnessEvalSuiteResult, ...]
    provider_model: str
    total_calls: int
    total_tokens: int
    total_cost_usd: float
    duration_ms: float


LiveBatchProgressCallback = Callable[[HarnessLiveBatchProgress], Awaitable[None]]


class HarnessLiveSuiteRunner:
    """Run a closed declarative Live Suite under one source/model boundary."""

    def __init__(
        self,
        model_port: ModelPort,
        *,
        transport_runner: HarnessLiveEvalRunner | None = None,
        request_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._model_port = model_port
        self._transport_runner = transport_runner or HarnessLiveEvalRunner(model_port)
        self._request_id_factory = request_id_factory or (
            lambda: f"hlivebatch_{secrets.token_hex(12)}"
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.perf_counter

    async def run(
        self,
        *,
        workspace_root: str | Path,
        loaded: LoadedLiveEvalSuite,
        profile_sha256: str,
        profile_trusted: bool,
        model: str,
        repetitions: int,
        batch_id: str,
        max_total_duration_seconds: float,
        max_total_cost_usd: float,
        on_progress: LiveBatchProgressCallback | None = None,
    ) -> HarnessLiveBatchExecution:
        request = HarnessLiveBatchRequest.create(
            request_id=self._request_id_factory(),
            created_at=_utc_timestamp(self._clock()),
            batch_id=batch_id,
            suite_id=loaded.suite.id,
            suite_sha256=loaded.sha256,
            profile_sha256=profile_sha256,
            model=model,
            repetitions=repetitions,
            max_total_duration_seconds=max_total_duration_seconds,
            max_total_cost_usd=max_total_cost_usd,
        )
        workspace = Path(workspace_root).expanduser().resolve()
        started = self._monotonic()
        deadline = started + request.max_total_duration_seconds
        await _notify_live_batch_progress(
            on_progress,
            HarnessLiveBatchProgress(
                stage="preparing",
                request_id=request.request_id,
                request_sha256=request.request_sha256,
                batch_id=request.batch_id,
                suite_id=request.suite_id,
                model=request.model,
                requested=request.repetitions,
                completed=0,
                persisted=0,
                max_total_duration_seconds=request.max_total_duration_seconds,
                max_total_cost_usd=request.max_total_cost_usd,
            ),
        )
        source_before, source_code = _capture_source(workspace)
        try:
            capability_before = self._model_port.get_model_capability_contract(model)
            reasoning_before = self._model_port.get_reasoning_effort_status(model)
            model_before = build_eval_model_identity(capability_before, reasoning_before)
        except Exception as exc:
            raise HarnessLiveEvalError(
                "live_batch_model_contract_invalid",
                "Live batch 无法建立请求前模型身份。",
            ) from exc

        raw_results: list[HarnessEvalSuiteResult] = []
        provider_models: set[str] = set()
        total_calls = 0
        total_tokens = 0
        total_cost = 0.0
        terminal_code = ""
        terminal_message = ""
        for _sample_index in range(request.repetitions):
            remaining_seconds = deadline - self._monotonic()
            remaining_cost = request.max_total_cost_usd - total_cost
            if remaining_seconds < 1.0:
                terminal_code = "live_batch_duration_exhausted"
                terminal_message = "Live batch 总时限不足以安全启动下一个样本。"
                break
            if remaining_cost <= 0.0:
                terminal_code = "live_batch_cost_exhausted"
                terminal_message = "Live batch 总成本预算已耗尽。"
                break
            sample = await self._run_sample(
                loaded,
                batch_request_sha256=request.request_sha256,
                model=model,
                max_duration_seconds=min(
                    remaining_seconds,
                    loaded.suite.budget.max_duration_seconds_per_sample,
                ),
                max_cost_usd=min(
                    remaining_cost,
                    loaded.suite.budget.max_cost_usd_per_sample,
                ),
            )
            raw_results.append(sample.result)
            provider_models.update(sample.provider_models)
            total_calls += sample.calls
            total_tokens += sample.tokens
            total_cost = round(total_cost + sample.cost_usd, 9)
            await _notify_live_batch_progress(
                on_progress,
                HarnessLiveBatchProgress(
                    stage="evaluating",
                    request_id=request.request_id,
                    request_sha256=request.request_sha256,
                    batch_id=request.batch_id,
                    suite_id=request.suite_id,
                    model=request.model,
                    provider_model=(
                        next(iter(provider_models)) if len(provider_models) == 1 else ""
                    ),
                    requested=request.repetitions,
                    completed=len(raw_results),
                    persisted=0,
                    total_calls=total_calls,
                    total_tokens=total_tokens,
                    total_cost_usd=total_cost,
                    duration_ms=_elapsed_ms(self._monotonic(), started),
                    max_total_duration_seconds=request.max_total_duration_seconds,
                    max_total_cost_usd=request.max_total_cost_usd,
                    actual_cost_exceeded=total_cost > request.max_total_cost_usd,
                    code=sample.code if sample.halt_batch else "",
                    message=(
                        "Live batch 遇到评测基础设施错误，未进行隐式重试。"
                        if sample.halt_batch
                        else ""
                    ),
                ),
            )
            if total_cost > request.max_total_cost_usd:
                terminal_code = "actual_cost_exceeded"
                terminal_message = (
                    "Provider 回执成本超过批次剩余预算；已停止后续样本且不会重试。"
                )
                break
            if sample.halt_batch:
                terminal_code = sample.code or "live_batch_sample_unstable"
                terminal_message = "Live batch 遇到评测基础设施错误，未进行隐式重试。"
                break

        source_after, after_code = _capture_source(workspace)
        source_code = source_code or after_code
        identity_code = source_code
        provider_model = next(iter(provider_models)) if len(provider_models) == 1 else ""
        try:
            capability_after = self._model_port.get_model_capability_contract(model)
            reasoning_after = self._model_port.get_reasoning_effort_status(model)
            model_after = build_eval_model_identity(capability_after, reasoning_after)
        except Exception:
            capability_after = None
            reasoning_after = None
            model_after = None
            identity_code = identity_code or "live_batch_model_contract_unavailable"
        if not identity_code and source_before != source_after:
            identity_code = "live_batch_source_changed"
        if not identity_code and model_after != model_before:
            identity_code = "live_batch_model_contract_changed"
        if not identity_code and len(provider_models) > 1:
            identity_code = "live_batch_provider_model_changed"
        if not identity_code and not provider_model:
            identity_code = "live_batch_provider_model_unavailable"

        identity = None
        if (
            not identity_code
            and source_before is not None
            and capability_after is not None
            and reasoning_after is not None
        ):
            configuration = HarnessEvalConfigurationIdentity.create(
                suite_id=loaded.suite.id,
                suite_sha256=loaded.sha256,
                profile_sha256=profile_sha256,
                policy_sha256=loaded.suite.comparison_policy.sha256,
                runner_version=LIVE_SUITE_RUNNER_VERSION,
                repetitions=repetitions,
                live=True,
            )
            identity = build_eval_baseline_identity(
                workspace,
                configuration=configuration,
                capability=capability_after,
                reasoning=reasoning_after,
                provider_model=provider_model,
                profile_trusted=profile_trusted,
                source_identity=source_before,
            )

        results = tuple(
            result.model_copy(
                update={
                    "baseline_identity": identity,
                    "baseline_identity_code": identity_code,
                }
            )
            for result in raw_results
        )
        completed = len(results)
        complete = completed == request.repetitions
        return HarnessLiveBatchExecution(
            request=request,
            status="completed" if complete else "partial",
            code=terminal_code if not complete else "",
            message=terminal_message if not complete else "Live batch 已完成全部样本。",
            results=results,
            provider_model=provider_model,
            total_calls=total_calls,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            duration_ms=_elapsed_ms(self._monotonic(), started),
        )

    async def _run_sample(
        self,
        loaded: LoadedLiveEvalSuite,
        *,
        batch_request_sha256: str,
        model: str,
        max_duration_seconds: float,
        max_cost_usd: float,
    ) -> _LiveSampleExecution:
        started = self._monotonic()
        deadline = started + max_duration_seconds
        remaining_cost = max_cost_usd
        cases: list[HarnessEvalCaseResult] = []
        providers: set[str] = set()
        calls = 0
        tokens = 0
        spent = 0.0
        halt_batch = False
        halt_code = ""
        for case in loaded.suite.cases:
            if halt_batch:
                cases.append(
                    _skipped_live_case(
                        case,
                        "live_sample_halted",
                        batch_request_sha256=batch_request_sha256,
                    )
                )
                continue
            seconds = min(case.budget.max_duration_seconds, deadline - self._monotonic())
            cost = min(case.budget.max_cost_usd, remaining_cost)
            if seconds < 1.0 or cost <= 0.0:
                cases.append(
                    _skipped_live_case(
                        case,
                        "live_sample_budget_exhausted",
                        batch_request_sha256=batch_request_sha256,
                    )
                )
                halt_batch = True
                halt_code = "live_sample_budget_exhausted"
                continue
            receipt = await self._transport_runner.run(
                HarnessLiveEvalRequest(
                    live=True,
                    model=model,
                    max_duration_seconds=seconds,
                    max_cost_usd=cost,
                    max_output_tokens=case.budget.max_output_tokens,
                )
            )
            calls += int(receipt.provider_call_attempted)
            tokens += receipt.total_tokens
            spent = round(spent + receipt.cost_usd, 9)
            remaining_cost = max(0.0, max_cost_usd - spent)
            if receipt.provider_model:
                providers.add(receipt.provider_model)
            cases.append(
                _live_case_result(
                    case,
                    receipt,
                    batch_request_sha256=batch_request_sha256,
                )
            )
            if receipt.status in {
                HarnessLiveEvalStatus.EVALUATION_ERROR,
                HarnessLiveEvalStatus.PARTIAL,
            }:
                halt_batch = True
                halt_code = receipt.code
        status = (
            EvalRunStatus.PASSED
            if cases and all(case.status is EvalCaseStatus.PASSED for case in cases)
            else EvalRunStatus.FAILED
        )
        return _LiveSampleExecution(
            result=HarnessEvalSuiteResult(
                suite_id=loaded.suite.id,
                title=loaded.suite.title,
                suite_path=loaded.display_path,
                suite_sha256=loaded.sha256,
                status=status,
                cases=tuple(cases),
                code=halt_code,
                message=("Live sample 存在评测基础设施错误。" if halt_batch else ""),
                comparison_policy=loaded.suite.comparison_policy,
                duration_ms=_elapsed_ms(self._monotonic(), started),
            ),
            provider_models=frozenset(providers),
            calls=calls,
            tokens=tokens,
            cost_usd=spent,
            halt_batch=halt_batch,
            code=halt_code,
        )


@dataclass(frozen=True, slots=True)
class _LiveSampleExecution:
    result: HarnessEvalSuiteResult
    provider_models: frozenset[str]
    calls: int
    tokens: int
    cost_usd: float
    halt_batch: bool
    code: str


def resolve_declared_live_eval_suite(
    workspace_root: str | Path,
    declared_suites: Sequence[str],
    target: str,
) -> LoadedLiveEvalSuite:
    workspace = Path(workspace_root).expanduser().resolve()
    requested = str(target).strip().replace("\\", "/")
    declared = tuple(str(value).strip().replace("\\", "/") for value in declared_suites)
    if not requested or requested == "all":
        raise HarnessLiveEvalError(
            "single_live_suite_required",
            "Live batch 必须指定一个 Profile 声明的 Suite id 或路径。",
        )
    if requested in declared:
        return load_live_eval_suite(workspace, workspace / requested)
    matches: list[LoadedLiveEvalSuite] = []
    for path_text in declared:
        try:
            loaded = load_live_eval_suite(workspace, workspace / path_text)
        except HarnessLiveEvalError:
            continue
        if loaded.suite.id == requested:
            matches.append(loaded)
    if len(matches) != 1:
        raise HarnessLiveEvalError(
            "live_suite_not_declared",
            "未找到唯一匹配的 Live Suite；只能使用当前 Profile 声明的 id 或路径。",
        )
    return matches[0]


def load_live_eval_suite(
    workspace_root: str | Path,
    suite_path: str | Path,
) -> LoadedLiveEvalSuite:
    workspace = Path(workspace_root).expanduser().resolve()
    candidate = Path(suite_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    path = candidate.resolve(strict=False)
    if not _is_relative_to(path, workspace):
        raise HarnessLiveEvalError(
            "live_suite_outside_workspace",
            "Live Suite 必须位于当前工作区内。",
        )
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_LIVE_SUITE_BYTES + 1)
        if len(raw) > MAX_LIVE_SUITE_BYTES:
            raise HarnessLiveEvalError(
                "live_suite_too_large",
                "Live Suite 超过 256 KiB 上限。",
            )
    except HarnessLiveEvalError:
        raise
    except OSError as exc:
        raise HarnessLiveEvalError(
            "live_suite_unreadable",
            "Live Suite 不存在或无法读取。",
        ) from exc
    try:
        payload: Any = yaml.safe_load(raw.decode("utf-8"))
        suite = HarnessLiveEvalSuite.model_validate(payload)
    except UnicodeDecodeError as exc:
        raise HarnessLiveEvalError(
            "live_suite_invalid_encoding",
            "Live Suite 必须使用 UTF-8 编码。",
        ) from exc
    except yaml.YAMLError as exc:
        raise HarnessLiveEvalError(
            "live_suite_invalid_yaml",
            "Live Suite YAML 语法无效。",
        ) from exc
    except ValidationError as exc:
        fields = sorted({str(item["loc"][0]) for item in exc.errors() if item["loc"]})
        suffix = f"（字段：{', '.join(fields[:8])}）" if fields else ""
        raise HarnessLiveEvalError(
            "live_suite_schema_invalid",
            f"Live Suite schema version 1 校验失败{suffix}。",
        ) from exc
    return LoadedLiveEvalSuite(
        path=path,
        display_path=_display_path(path, workspace),
        suite=suite,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def build_live_batch_status(
    execution: HarnessLiveBatchExecution,
    *,
    persisted_result_sha256: Sequence[str],
    persistence_code: str = "",
    persistence_message: str = "",
) -> HarnessLiveBatchStatus:
    persisted = tuple(persisted_result_sha256)
    identity = execution.results[0].baseline_identity if execution.results else None
    status: Literal["completed", "partial", "error"] = execution.status
    code = execution.code
    message = execution.message
    if persistence_code:
        status = "error"
        code = persistence_code
        message = persistence_message
    elif len(persisted) != len(execution.results):
        status = "partial"
        code = "live_batch_persistence_incomplete"
        message = "Live batch 只保存了不可变连续前缀。"
    raw = {
        "schema_version": 1,
        "status": status,
        "code": code,
        "message": message,
        "request_id": execution.request.request_id,
        "request_sha256": execution.request.request_sha256,
        "batch_id": execution.request.batch_id,
        "suite_id": execution.request.suite_id,
        "model": execution.request.model,
        "provider_model": execution.provider_model,
        "requested": execution.request.repetitions,
        "completed": len(execution.results),
        "persisted": len(persisted),
        "total_calls": execution.total_calls,
        "total_tokens": execution.total_tokens,
        "total_cost_usd": execution.total_cost_usd,
        "duration_ms": execution.duration_ms,
        "max_total_duration_seconds": execution.request.max_total_duration_seconds,
        "max_total_cost_usd": execution.request.max_total_cost_usd,
        "actual_cost_exceeded": (
            execution.total_cost_usd > execution.request.max_total_cost_usd
        ),
        "identity_sha256": identity.identity_sha256 if identity is not None else "",
        "baseline_eligible": bool(
            status == "completed" and identity is not None and identity.baseline_eligible
        ),
        "sample_result_sha256": persisted,
    }
    return HarnessLiveBatchStatus.model_validate({**raw, "receipt_sha256": _sha256_payload(raw)})


def live_batch_terminal_progress(
    status: HarnessLiveBatchStatus,
) -> HarnessLiveBatchProgress:
    """Project the tamper-evident terminal receipt into the progress protocol."""
    return HarnessLiveBatchProgress(
        stage=status.status,
        request_id=status.request_id,
        request_sha256=status.request_sha256,
        batch_id=status.batch_id,
        suite_id=status.suite_id,
        model=status.model,
        provider_model=status.provider_model,
        requested=status.requested,
        completed=status.completed,
        persisted=status.persisted,
        total_calls=status.total_calls,
        total_tokens=status.total_tokens,
        total_cost_usd=status.total_cost_usd,
        duration_ms=status.duration_ms,
        max_total_duration_seconds=status.max_total_duration_seconds,
        max_total_cost_usd=status.max_total_cost_usd,
        actual_cost_exceeded=status.actual_cost_exceeded,
        identity_sha256=status.identity_sha256,
        baseline_eligible=status.baseline_eligible,
        code=status.code,
        message=status.message,
    )


def render_live_batch_status(status: HarnessLiveBatchStatus) -> str:
    tone = {"completed": "已完成", "partial": "部分完成", "error": "错误"}
    lines = [
        "# Harness Live Eval Batch",
        "",
        f"- 状态：{tone[status.status]}" + (f"（`{status.code}`）" if status.code else ""),
        f"- Batch：`{status.batch_id}` · Suite：`{status.suite_id}`",
        f"- 模型：`{status.model}`",
        f"- 样本：完成 {status.completed}/{status.requested} · 已保存 {status.persisted}",
        f"- 调用：{status.total_calls} · Token：{status.total_tokens}",
        f"- 成本：${status.total_cost_usd:.6f} / ${status.max_total_cost_usd:.6f}",
        f"- 耗时：{status.duration_ms:.0f}ms / {status.max_total_duration_seconds:.1f}s",
    ]
    if status.provider_model:
        lines.append(f"- Provider 实际模型：`{status.provider_model}`")
    if status.actual_cost_exceeded:
        lines.append("- 成本保护：Provider 回执显示实际成本已超出上限，后续样本已停止")
    if status.identity_sha256:
        lines.append(
            f"- Identity：`{status.identity_sha256[:12]}…` · "
            + ("可显式晋升" if status.baseline_eligible else "不可晋升")
        )
    else:
        lines.append("- Identity：不可用；本批次不可晋升")
    if status.message:
        lines.append(f"- 说明：{status.message}")
    lines.append(f"- 回执：`{status.receipt_sha256[:12]}…`")
    return "\n".join(lines)


def _live_case_result(
    case: HarnessLiveSuiteCase,
    receipt: Any,
    *,
    batch_request_sha256: str,
) -> HarnessEvalCaseResult:
    if receipt.status is HarnessLiveEvalStatus.PASSED:
        status = EvalCaseStatus.PASSED
    elif receipt.status is HarnessLiveEvalStatus.IMPLEMENTATION_FAILURE:
        status = EvalCaseStatus.IMPLEMENTATION_FAILURE
    else:
        status = EvalCaseStatus.EVALUATION_ERROR
    observations: tuple[HarnessEvalMetricObservation, ...] = ()
    primary_metric = ""
    if status is not EvalCaseStatus.EVALUATION_ERROR:
        primary_metric = "live_transport_exact_match"
        observations = tuple(
            sorted(
                (
                    HarnessEvalMetricObservation(
                        metric="live_cost_usd",
                        value=receipt.cost_usd,
                        unit="usd",
                        direction="decrease",
                        target=case.budget.max_cost_usd,
                    ),
                    HarnessEvalMetricObservation(
                        metric="live_duration_ms",
                        value=receipt.duration_ms,
                        unit="milliseconds",
                        direction="decrease",
                        target=case.budget.max_duration_seconds * 1_000,
                    ),
                    HarnessEvalMetricObservation(
                        metric="live_output_tokens",
                        value=float(receipt.output_tokens),
                        unit="tokens",
                        direction="decrease",
                        target=float(case.budget.max_output_tokens),
                    ),
                    HarnessEvalMetricObservation(
                        metric=primary_metric,
                        value=1.0 if receipt.exact_match else 0.0,
                        unit="ratio",
                        direction="increase",
                        target=1.0,
                        primary=True,
                    ),
                ),
                key=lambda item: item.metric,
            )
        )
    return HarnessEvalCaseResult(
        case_id=case.id,
        runner=LIVE_EVAL_RUNNER_VERSION,
        status=status,
        primary_metric=primary_metric,
        metric_observations=observations,
        code=receipt.code,
        message=receipt.message,
        duration_ms=receipt.duration_ms,
        live_evidence=HarnessEvalLiveEvidence(
            provider_model=receipt.provider_model,
            finish_reason=receipt.finish_reason,
            input_tokens=receipt.input_tokens,
            output_tokens=receipt.output_tokens,
            total_tokens=receipt.total_tokens,
            cost_usd=receipt.cost_usd,
            response_sha256=receipt.response_sha256,
            transport_receipt_sha256=receipt.receipt_sha256,
            batch_request_sha256=batch_request_sha256,
            exact_match=receipt.exact_match,
        ),
    )


def _skipped_live_case(
    case: HarnessLiveSuiteCase,
    code: str,
    *,
    batch_request_sha256: str,
) -> HarnessEvalCaseResult:
    empty_receipt = {
        "provider_model": "",
        "finish_reason": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "response_sha256": "",
        "transport_receipt_sha256": "0" * 64,
        "batch_request_sha256": batch_request_sha256,
        "exact_match": False,
    }
    return HarnessEvalCaseResult(
        case_id=case.id,
        runner=LIVE_EVAL_RUNNER_VERSION,
        status=EvalCaseStatus.EVALUATION_ERROR,
        code=code,
        message="Live sample 预算不足，未发送请求。",
        live_evidence=HarnessEvalLiveEvidence.model_validate(empty_receipt),
    )


def _capture_source(
    workspace: Path,
) -> tuple[HarnessEvalSourceIdentity | None, str]:
    try:
        return capture_eval_source_identity(workspace), ""
    except (TreeFingerprintError, OSError):
        return None, "live_batch_source_unavailable"


def _display_path(path: Path, workspace: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.name


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _elapsed_ms(now: float, started: float) -> float:
    if not math.isfinite(now) or not math.isfinite(started):
        return 0.0
    return max(0.0, (now - started) * 1_000)


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def _notify_live_batch_progress(
    callback: LiveBatchProgressCallback | None,
    progress: HarnessLiveBatchProgress,
) -> None:
    if callback is None:
        return
    try:
        await callback(progress)
    except Exception as exc:
        _LOGGER.warning("Live batch progress callback failed: %s", type(exc).__name__)


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "LIVE_SUITE_RUNNER_VERSION",
    "HarnessLiveBatchExecution",
    "HarnessLiveBatchProgress",
    "HarnessLiveBatchRequest",
    "HarnessLiveBatchStatus",
    "HarnessLiveEvalSuite",
    "HarnessLiveSuiteRunner",
    "LoadedLiveEvalSuite",
    "build_live_batch_status",
    "load_live_eval_suite",
    "live_batch_terminal_progress",
    "render_live_batch_status",
    "resolve_declared_live_eval_suite",
]
