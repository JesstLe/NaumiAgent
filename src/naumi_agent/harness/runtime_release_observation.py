"""Append-only observations for release-bound runtime heartbeats."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.runtime_release_binding import (
    HarnessRuntimeReleaseBinding,
)

HARNESS_RUNTIME_RELEASE_OBSERVATION_POLICY = (
    "harness-runtime-release-observation-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class HarnessRuntimeReleaseObservation(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["harness-runtime-release-observation-v1"] = (
        HARNESS_RUNTIME_RELEASE_OBSERVATION_POLICY
    )
    sample_id: str = Field(pattern=r"^hrreleaseobservation_[0-9a-f]{24}$")
    sample_sha256: str = Field(pattern=_SHA256_RE)
    previous_sample_sha256: str = Field(pattern=r"^(|[0-9a-f]{64})$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    binding_id: str = Field(pattern=r"^hrreleasebinding_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=_SHA256_RE)
    runtime_identity_id: str = Field(pattern=r"^relruntimeidentity_[0-9a-f]{24}$")
    runtime_identity_sha256: str = Field(pattern=_SHA256_RE)
    surface: Literal["new_ui", "tui"]
    subject_kind: Literal["runtime"] = "runtime"
    subject_id: str = Field(min_length=1, max_length=96)
    instance_id: str = Field(min_length=1, max_length=96)
    epoch: int = Field(ge=1)
    chain_origin_sequence: int = Field(ge=1)
    chain_origin_kind: Literal["startup", "legacy_snapshot"]
    heartbeat_sequence: int = Field(ge=1)
    phase: HarnessHeartbeatPhase
    observed_at: str = Field(min_length=1, max_length=100)
    timeout_seconds: int = Field(ge=3, le=86_400)
    detail_code: str = Field(min_length=1, max_length=128)
    heartbeat_observation_authority: Literal[True] = True
    current_liveness_authority: Literal[False] = False
    rollout_observation_window_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Runtime Release Observation workspace_root 必须 canonical。")
        if not _IDENTITY_RE.fullmatch(self.subject_id) or not _IDENTITY_RE.fullmatch(
            self.instance_id
        ):
            raise ValueError("Runtime Release Observation heartbeat identity 无效。")
        _aware(self.observed_at)
        if self.heartbeat_sequence < self.chain_origin_sequence:
            raise ValueError("Runtime Release Observation 不能早于 chain origin。")
        if self.chain_origin_kind == "startup" and self.chain_origin_sequence != 1:
            raise ValueError("startup observation chain 必须从 sequence 1 开始。")
        is_origin = self.heartbeat_sequence == self.chain_origin_sequence
        if is_origin and self.previous_sample_sha256:
            raise ValueError("Observation chain origin 不能声明前序摘要。")
        if not is_origin and not self.previous_sample_sha256:
            raise ValueError("非 origin observation 必须声明前序摘要。")
        core = self.model_dump(mode="json", exclude={"sample_id", "sample_sha256"})
        digest = _digest(core)
        if self.sample_sha256 != digest or self.sample_id != (
            f"hrreleaseobservation_{digest[:24]}"
        ):
            raise ValueError("Runtime Release Observation content identity 不一致。")
        return self


class RuntimeReleaseObservationPage(_StrictModel):
    workspace_root: str = Field(min_length=1, max_length=4096)
    subject_id: str = Field(min_length=1, max_length=96)
    binding_id: str = Field(pattern=r"^hrreleasebinding_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=_SHA256_RE)
    after_sequence: int = Field(ge=0)
    items: tuple[HarnessRuntimeReleaseObservation, ...] = Field(max_length=500)
    next_sequence: int = Field(ge=0)

    @property
    def has_more(self) -> bool:
        return self.next_sequence > 0


def build_runtime_release_observation(
    *,
    binding: HarnessRuntimeReleaseBinding,
    heartbeat: HarnessHeartbeat,
    previous_sample_sha256: str,
    chain_origin_sequence: int,
    chain_origin_kind: Literal["startup", "legacy_snapshot"],
) -> HarnessRuntimeReleaseObservation:
    if heartbeat.subject_kind is not HarnessRunKind.RUNTIME:
        raise ValueError("Release-bound observation 只接受 runtime heartbeat。")
    if not (
        heartbeat.workspace_root == binding.workspace_root
        and heartbeat.subject_id == binding.subject_id
        and heartbeat.instance_id == binding.instance_id
        and heartbeat.epoch == binding.epoch
    ):
        raise ValueError("Runtime Release Binding 与 heartbeat observation 不一致。")
    if _aware(heartbeat.observed_at) < _aware(binding.bound_at):
        raise ValueError("Runtime Release Observation 不能早于 release binding。")
    previous = previous_sample_sha256.strip()
    if previous and not re.fullmatch(_SHA256_RE, previous):
        raise ValueError("previous_sample_sha256 必须为空或 64 位小写 SHA-256。")
    core = {
        "schema_version": 1,
        "policy_version": HARNESS_RUNTIME_RELEASE_OBSERVATION_POLICY,
        "previous_sample_sha256": previous,
        "workspace_root": binding.workspace_root,
        "binding_id": binding.binding_id,
        "binding_sha256": binding.binding_sha256,
        "runtime_identity_id": binding.runtime_identity.identity_id,
        "runtime_identity_sha256": binding.runtime_identity.identity_sha256,
        "surface": binding.surface,
        "subject_kind": "runtime",
        "subject_id": heartbeat.subject_id,
        "instance_id": heartbeat.instance_id,
        "epoch": heartbeat.epoch,
        "chain_origin_sequence": chain_origin_sequence,
        "chain_origin_kind": chain_origin_kind,
        "heartbeat_sequence": heartbeat.sequence,
        "phase": heartbeat.phase.value,
        "observed_at": _aware(heartbeat.observed_at).isoformat(),
        "timeout_seconds": heartbeat.timeout_seconds,
        "detail_code": heartbeat.detail_code,
        "heartbeat_observation_authority": True,
        "current_liveness_authority": False,
        "rollout_observation_window_authority": False,
    }
    digest = _digest(core)
    return HarnessRuntimeReleaseObservation.model_validate(
        {
            **core,
            "sample_id": f"hrreleaseobservation_{digest[:24]}",
            "sample_sha256": digest,
        }
    )


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Runtime Release Observation timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "HARNESS_RUNTIME_RELEASE_OBSERVATION_POLICY",
    "HarnessRuntimeReleaseObservation",
    "RuntimeReleaseObservationPage",
    "build_runtime_release_observation",
]
