"""Tamper-evident identity contracts for comparable Harness eval baselines."""

from __future__ import annotations

import hashlib
import hmac
import json
import platform
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent import __version__
from naumi_agent.harness.fingerprint import compute_tree_fingerprint
from naumi_agent.model.reasoning import ReasoningEffortStatus
from naumi_agent.model.router import ModelCapabilityContract, ModelContractStatus

_SHA256_RE = r"^[0-9a-f]{64}$"
_TREE_SHA256_RE = r"^sha256:[0-9a-f]{64}$"
_COMMIT_RE = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_RUNNER_VERSION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}@[1-9][0-9]{0,8}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessEvalConfigurationIdentity(_StrictModel):
    """Exact safe configuration dimensions that affect one eval result."""

    suite_id: str
    suite_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    runner_version: str
    repetitions: int = Field(ge=1, le=10_000)
    live: bool
    digest: str = Field(pattern=_SHA256_RE)

    @field_validator("suite_id")
    @classmethod
    def _valid_suite_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _ID_RE.fullmatch(normalized):
            raise ValueError("suite_id 格式无效。")
        return normalized

    @field_validator("runner_version")
    @classmethod
    def _valid_runner_version(cls, value: str) -> str:
        normalized = value.strip()
        if not _RUNNER_VERSION_RE.fullmatch(normalized):
            raise ValueError("runner_version 必须使用 runner@正整数 格式。")
        return normalized

    @model_validator(mode="after")
    def _digest_matches_payload(self) -> Self:
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"digest"})
        )
        if not hmac.compare_digest(self.digest, expected):
            raise ValueError("configuration digest 与身份字段不匹配。")
        return self

    @classmethod
    def create(
        cls,
        *,
        suite_id: str,
        suite_sha256: str,
        profile_sha256: str,
        runner_version: str,
        repetitions: int,
        live: bool,
    ) -> HarnessEvalConfigurationIdentity:
        """Validate configuration fields and bind them to a canonical digest."""
        raw = {
            "suite_id": suite_id,
            "suite_sha256": suite_sha256,
            "profile_sha256": profile_sha256,
            "runner_version": runner_version,
            "repetitions": repetitions,
            "live": live,
        }
        validated = _ConfigurationWithoutDigest.model_validate(raw)
        payload = validated.model_dump(mode="json")
        return cls.model_validate({**payload, "digest": _sha256_payload(payload)})


class _ConfigurationWithoutDigest(_StrictModel):
    suite_id: str
    suite_sha256: str = Field(pattern=_SHA256_RE)
    profile_sha256: str = Field(pattern=_SHA256_RE)
    runner_version: str
    repetitions: int = Field(ge=1, le=10_000)
    live: bool

    @field_validator("suite_id")
    @classmethod
    def _valid_suite_id(cls, value: str) -> str:
        return HarnessEvalConfigurationIdentity._valid_suite_id(value)

    @field_validator("runner_version")
    @classmethod
    def _valid_runner_version(cls, value: str) -> str:
        return HarnessEvalConfigurationIdentity._valid_runner_version(value)


class HarnessEvalSourceIdentity(_StrictModel):
    """Source revision and exact worktree state without exposing changed paths."""

    commit: str = Field(pattern=_COMMIT_RE)
    tree_sha256: str = Field(pattern=_TREE_SHA256_RE)
    dirty: bool


class HarnessEvalModelIdentity(_StrictModel):
    """Display-safe model routing, capability, and reasoning facts."""

    requested_model: str = Field(max_length=512)
    canonical_model: str = Field(max_length=512)
    upstream_model: str = Field(max_length=512)
    provider: str = Field(max_length=128)
    api_format: str = Field(max_length=128)
    capability_sha256: str = Field(pattern=_SHA256_RE)
    capability_status: Literal["verified", "partial", "unverified", "incompatible"]
    reasoning_effort: str = Field(min_length=1, max_length=32)
    reasoning_source: Literal["runtime", "model", "global", "auto"]
    reasoning_supported: tuple[str, ...] = Field(max_length=16)
    reasoning_default: str | None = Field(default=None, max_length=32)
    reasoning_warning: bool = False

    @field_validator(
        "requested_model",
        "canonical_model",
        "upstream_model",
        "provider",
        "api_format",
    )
    @classmethod
    def _strip_identity_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("reasoning_supported")
    @classmethod
    def _unique_reasoning_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("reasoning_supported 不能重复。")
        return values


class HarnessEvalPlatformIdentity(_StrictModel):
    """Runtime platform dimensions required for cross-platform comparison."""

    system: Literal["macos", "linux", "windows", "unknown"]
    release: str = Field(min_length=1, max_length=256)
    machine: str = Field(min_length=1, max_length=128)
    python_implementation: str = Field(min_length=1, max_length=64)
    python_version: str = Field(min_length=1, max_length=64)
    naumi_version: str = Field(min_length=1, max_length=64)

    @field_validator(
        "release",
        "machine",
        "python_implementation",
        "python_version",
        "naumi_version",
    )
    @classmethod
    def _strip_platform_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("平台身份字段不能为空。")
        return normalized


class HarnessEvalBaselineIdentity(_StrictModel):
    """Canonical baseline identity with an independently verifiable digest."""

    schema_version: Literal[1] = 1
    source: HarnessEvalSourceIdentity
    configuration: HarnessEvalConfigurationIdentity
    model: HarnessEvalModelIdentity | None
    platform: HarnessEvalPlatformIdentity
    profile_trusted: bool
    baseline_eligible: bool
    warnings: tuple[str, ...] = Field(default=(), max_length=8)
    identity_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator("warnings")
    @classmethod
    def _unique_warnings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Baseline identity warnings 不能重复。")
        return values

    @model_validator(mode="after")
    def _identity_digest_matches(self) -> Self:
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"identity_sha256"})
        )
        if not hmac.compare_digest(self.identity_sha256, expected):
            raise ValueError("identity_sha256 与 Baseline identity 字段不匹配。")
        return self


def capture_eval_platform_identity() -> HarnessEvalPlatformIdentity:
    """Capture bounded, display-safe host facts without environment variables."""
    system_name = platform.system().lower()
    system: Literal["macos", "linux", "windows", "unknown"]
    if system_name == "darwin":
        system = "macos"
    elif system_name == "linux":
        system = "linux"
    elif system_name == "windows":
        system = "windows"
    else:
        system = "unknown"
    return HarnessEvalPlatformIdentity(
        system=system,
        release=platform.release() or "unknown",
        machine=platform.machine() or "unknown",
        python_implementation=platform.python_implementation() or "unknown",
        python_version=platform.python_version() or "unknown",
        naumi_version=__version__,
    )


def capture_eval_source_identity(
    workspace_root: str | Path,
) -> HarnessEvalSourceIdentity:
    """Capture the authoritative Git source state without retaining path names."""
    fingerprint = compute_tree_fingerprint(workspace_root)
    return HarnessEvalSourceIdentity(
        commit=fingerprint.head.lower(),
        tree_sha256=fingerprint.digest,
        dirty=bool(fingerprint.dirty_paths),
    )


def build_eval_baseline_identity(
    workspace_root: str | Path,
    *,
    configuration: HarnessEvalConfigurationIdentity,
    capability: ModelCapabilityContract | None = None,
    reasoning: ReasoningEffortStatus | None = None,
    platform_identity: HarnessEvalPlatformIdentity | None = None,
    profile_trusted: bool = True,
    source_identity: HarnessEvalSourceIdentity | None = None,
) -> HarnessEvalBaselineIdentity:
    """Build one exact identity from authoritative source/model/runtime facts."""
    source = source_identity or capture_eval_source_identity(workspace_root)
    if (capability is None) != (reasoning is None):
        raise ValueError("模型能力合同与思考强度必须同时提供或同时省略。")
    model = (
        _model_identity(capability, reasoning)
        if capability is not None and reasoning is not None
        else None
    )
    runtime_platform = platform_identity or capture_eval_platform_identity()

    warnings: list[str] = []
    eligible = True
    if not profile_trusted:
        warnings.append("Harness Profile 尚未受信任；本次结果不可晋升为 Baseline。")
        eligible = False
    if source.dirty:
        warnings.append("工作区存在未提交改动；本次结果不可晋升为 Baseline。")
        eligible = False
    if capability is not None and capability.status is ModelContractStatus.PARTIAL:
        warnings.append("模型能力合同仅部分验证；Baseline 比较需保持完全相同的能力摘要。")
    elif capability is not None and capability.status is ModelContractStatus.UNVERIFIED:
        warnings.append("模型关键能力尚未验证；本次结果不可晋升为 Baseline。")
        eligible = False
    elif capability is not None and capability.status is ModelContractStatus.INCOMPATIBLE:
        warnings.append("模型能力与 Agent Harness 不兼容；本次结果不可晋升为 Baseline。")
        eligible = False
    if reasoning is not None and reasoning.warning is not None:
        warnings.append("当前思考强度与模型能力声明不一致；本次结果不可晋升为 Baseline。")
        eligible = False

    raw = {
        "schema_version": 1,
        "source": source.model_dump(mode="json"),
        "configuration": configuration.model_dump(mode="json"),
        "model": model.model_dump(mode="json") if model is not None else None,
        "platform": runtime_platform.model_dump(mode="json"),
        "profile_trusted": profile_trusted,
        "baseline_eligible": eligible,
        "warnings": warnings,
    }
    return HarnessEvalBaselineIdentity.model_validate(
        {**raw, "identity_sha256": _sha256_payload(raw)}
    )


def _model_identity(
    capability: ModelCapabilityContract,
    reasoning: ReasoningEffortStatus,
) -> HarnessEvalModelIdentity:
    if reasoning.model.strip() != capability.requested_model.strip():
        raise ValueError("模型能力合同与思考强度状态不属于同一个 requested model。")
    capability_payload = capability.to_dict()
    capability_payload.pop("warnings", None)
    capability_payload.pop("errors", None)
    return HarnessEvalModelIdentity(
        requested_model=capability.requested_model,
        canonical_model=capability.canonical_model,
        upstream_model=capability.upstream_model,
        provider=capability.provider,
        api_format=capability.api_format,
        capability_sha256=_sha256_payload(capability_payload),
        capability_status=capability.status.value,
        reasoning_effort=reasoning.effective.value,
        reasoning_source=reasoning.source,
        reasoning_supported=tuple(sorted(value.value for value in reasoning.supported)),
        reasoning_default=(
            reasoning.default.value if reasoning.default is not None else None
        ),
        reasoning_warning=reasoning.warning is not None,
    )


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
    "HarnessEvalBaselineIdentity",
    "HarnessEvalConfigurationIdentity",
    "HarnessEvalModelIdentity",
    "HarnessEvalPlatformIdentity",
    "HarnessEvalSourceIdentity",
    "build_eval_baseline_identity",
    "capture_eval_platform_identity",
    "capture_eval_source_identity",
]
