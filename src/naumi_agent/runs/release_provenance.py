"""Content-addressed managed-release provenance for one real chat run."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.harness.runtime_release_binding import HarnessRuntimeReleaseBinding

RUN_RELEASE_PROVENANCE_POLICY = "run-release-provenance-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class RunReleaseProvenance(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["run-release-provenance-v1"] = (
        RUN_RELEASE_PROVENANCE_POLICY
    )
    provenance_id: str = Field(pattern=r"^runrelease_[0-9a-f]{24}$")
    provenance_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    run_id: str = Field(min_length=1, max_length=128)
    source_kind: Literal["terminal_runtime_context"] = "terminal_runtime_context"
    surface: Literal["new_ui", "tui"]
    binding: HarnessRuntimeReleaseBinding
    run_started_at: str = Field(min_length=1, max_length=100)
    release_binding_source_authority: Literal[True] = True
    completion_receipt_authority: Literal[False] = False
    execution_outcome_authority: Literal[False] = False
    opt_in_stage_completion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Run Release Provenance workspace 必须 canonical。")
        if not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("Run Release Provenance run_id 无效。")
        if not (
            self.workspace_root == self.binding.workspace_root
            and self.surface == self.binding.surface
            and self.binding.release_identity_authority
            and not self.binding.heartbeat_liveness_authority
            and not self.binding.rollout_observation_authority
        ):
            raise ValueError("Run Release Provenance 与 Runtime Binding 不一致。")
        if _aware(self.run_started_at) < _aware(self.binding.bound_at):
            raise ValueError("Run 不能早于 Runtime Release Binding。")
        core = self.model_dump(
            mode="json",
            exclude={"provenance_id", "provenance_sha256"},
        )
        digest = _digest(core)
        if self.provenance_sha256 != digest or self.provenance_id != (
            f"runrelease_{digest[:24]}"
        ):
            raise ValueError("Run Release Provenance identity 不一致。")
        return self


def build_run_release_provenance(
    *,
    workspace_root: str | Path,
    run_id: str,
    binding: HarnessRuntimeReleaseBinding,
    run_started_at: str,
) -> RunReleaseProvenance:
    try:
        source = HarnessRuntimeReleaseBinding.model_validate_json(
            binding.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Runtime Release Binding artifact 无效。") from exc
    core = {
        "schema_version": 1,
        "policy_version": RUN_RELEASE_PROVENANCE_POLICY,
        "workspace_root": str(Path(workspace_root).expanduser().resolve()),
        "run_id": run_id,
        "source_kind": "terminal_runtime_context",
        "surface": source.surface,
        "binding": source.model_dump(mode="json"),
        "run_started_at": _aware(run_started_at).isoformat(),
        "release_binding_source_authority": True,
        "completion_receipt_authority": False,
        "execution_outcome_authority": False,
        "opt_in_stage_completion_authority": False,
    }
    digest = _digest(core)
    return RunReleaseProvenance.model_validate(
        {
            **core,
            "binding": source,
            "provenance_id": f"runrelease_{digest[:24]}",
            "provenance_sha256": digest,
        }
    )


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Run Release Provenance timestamp 必须包含 offset。")
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
    "RUN_RELEASE_PROVENANCE_POLICY",
    "RunReleaseProvenance",
    "build_run_release_provenance",
]
