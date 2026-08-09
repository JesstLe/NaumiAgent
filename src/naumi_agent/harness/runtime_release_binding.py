"""Exact release identity binding for one durable runtime heartbeat subject."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.release.runtime_identity import ReleaseRuntimeIdentity

HARNESS_RUNTIME_RELEASE_BINDING_POLICY = "harness-runtime-release-binding-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class HarnessRuntimeReleaseBinding(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["harness-runtime-release-binding-v1"] = (
        HARNESS_RUNTIME_RELEASE_BINDING_POLICY
    )
    binding_id: str = Field(pattern=r"^hrreleasebinding_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    surface: Literal["new_ui", "tui"]
    subject_kind: Literal["runtime"] = "runtime"
    subject_id: str = Field(min_length=1, max_length=96)
    instance_id: str = Field(min_length=1, max_length=96)
    epoch: int = Field(ge=1)
    heartbeat_start_sequence: Literal[1] = 1
    runtime_identity: ReleaseRuntimeIdentity
    release_identity_authority: Literal[True] = True
    heartbeat_liveness_authority: Literal[False] = False
    rollout_observation_authority: Literal[False] = False
    bound_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Runtime Release Binding workspace_root 必须 canonical。")
        if not _IDENTITY_RE.fullmatch(self.subject_id) or not _IDENTITY_RE.fullmatch(
            self.instance_id
        ):
            raise ValueError("Runtime Release Binding heartbeat identity 无效。")
        bound = _aware(self.bound_at)
        if bound < _aware(self.runtime_identity.verified_at):
            raise ValueError("Runtime Release Binding 早于 runtime identity verification。")
        core = self.model_dump(mode="json", exclude={"binding_id", "binding_sha256"})
        digest = _digest(core)
        if self.binding_sha256 != digest or self.binding_id != (
            f"hrreleasebinding_{digest[:24]}"
        ):
            raise ValueError("Runtime Release Binding content identity 不一致。")
        return self


def build_runtime_release_binding(
    *,
    workspace_root: str | Path,
    surface: Literal["new_ui", "tui"],
    subject_id: str,
    instance_id: str,
    epoch: int,
    runtime_identity: ReleaseRuntimeIdentity,
    bound_at: str,
) -> HarnessRuntimeReleaseBinding:
    core = {
        "schema_version": 1,
        "policy_version": HARNESS_RUNTIME_RELEASE_BINDING_POLICY,
        "workspace_root": str(Path(workspace_root).expanduser().resolve()),
        "surface": surface,
        "subject_kind": "runtime",
        "subject_id": subject_id,
        "instance_id": instance_id,
        "epoch": epoch,
        "heartbeat_start_sequence": 1,
        "runtime_identity": runtime_identity.model_dump(mode="json"),
        "release_identity_authority": True,
        "heartbeat_liveness_authority": False,
        "rollout_observation_authority": False,
        "bound_at": _aware(bound_at).isoformat(),
    }
    digest = _digest(core)
    return HarnessRuntimeReleaseBinding.model_validate(
        {
            **core,
            "runtime_identity": runtime_identity,
            "binding_id": f"hrreleasebinding_{digest[:24]}",
            "binding_sha256": digest,
        }
    )


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Runtime Release Binding timestamp 必须包含时区。")
    return parsed


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
    "HARNESS_RUNTIME_RELEASE_BINDING_POLICY",
    "HarnessRuntimeReleaseBinding",
    "build_runtime_release_binding",
]
