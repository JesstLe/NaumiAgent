"""Typed bridge for running Evolution revalidation through Harness Sandbox."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckResult,
    HarnessSandboxSourceOverlay,
)

_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class HarnessEvolutionRevalidationPlan(_StrictModel):
    profile_sha256: str = Field(pattern=_SHA256_RE)
    changed_paths: tuple[str, ...] = Field(min_length=1, max_length=16)
    checks: tuple[HarnessCheckSpec, ...] = Field(min_length=1, max_length=80)
    plan_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _plan_is_exact(self):
        if self.changed_paths != tuple(sorted(set(self.changed_paths))):
            raise ValueError("Evolution Revalidation changed paths 必须排序且不得重复。")
        check_ids = tuple(item.id for item in self.checks)
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("Evolution Revalidation checks 不得重复。")
        expected = _sha256_payload(
            {
                "profile_sha256": self.profile_sha256,
                "changed_paths": self.changed_paths,
                "checks": [item.model_dump(mode="json") for item in self.checks],
            }
        )
        if self.plan_sha256 != expected:
            raise ValueError("Evolution Revalidation plan 摘要不一致。")
        return self


@dataclass(frozen=True, slots=True)
class HarnessEvolutionRevalidationSource:
    revision: str
    revision_tree_sha256: str
    overlays: tuple[HarnessSandboxSourceOverlay, ...]
    overlay_source_sha256: str

    def __post_init__(self) -> None:
        if not self.overlays:
            raise ValueError("Evolution Revalidation source 至少需要一个 overlay。")


@dataclass(frozen=True, slots=True)
class HarnessEvolutionRevalidationRun:
    run_id: str
    results: tuple[HarnessSandboxCheckResult, ...]


class HarnessEvolutionRevalidationRunError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        run_id: str,
        partial_results: tuple[HarnessSandboxCheckResult, ...],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.run_id = run_id
        self.partial_results = partial_results


def build_harness_evolution_revalidation_plan(
    *,
    profile_sha256: str,
    changed_paths: tuple[str, ...],
    checks: tuple[HarnessCheckSpec, ...],
) -> HarnessEvolutionRevalidationPlan:
    normalized_paths = tuple(sorted(set(changed_paths)))
    payload = {
        "profile_sha256": profile_sha256,
        "changed_paths": normalized_paths,
        "checks": [item.model_dump(mode="json") for item in checks],
    }
    return HarnessEvolutionRevalidationPlan(
        **payload,
        plan_sha256=_sha256_payload(payload),
    )


def git_revision_tree_sha256(workspace_root: str | Path, revision: str) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--full-tree", revision],
        cwd=Path(workspace_root),
        check=True,
        capture_output=True,
        timeout=20,
    )
    return hashlib.sha256(completed.stdout).hexdigest()


def overlay_set_sha256(overlays: tuple[HarnessSandboxSourceOverlay, ...]) -> str:
    return _sha256_payload(
        [
            {
                "path": item.path,
                "sha256": item.sha256,
                "executable": item.executable,
            }
            for item in overlays
        ]
    )


def _sha256_payload(payload: object) -> str:
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
    "HarnessEvolutionRevalidationPlan",
    "HarnessEvolutionRevalidationRun",
    "HarnessEvolutionRevalidationRunError",
    "HarnessEvolutionRevalidationSource",
    "build_harness_evolution_revalidation_plan",
    "git_revision_tree_sha256",
    "overlay_set_sha256",
]
