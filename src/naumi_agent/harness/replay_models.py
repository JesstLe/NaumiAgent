"""Immutable values returned by deterministic Harness replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

HarnessReplayStatus = Literal["reproduced", "changed", "partial", "corrupt"]
HarnessReplayLookupStatus = Literal["ok", "not_found", "unavailable"]


@dataclass(frozen=True, slots=True)
class HarnessReplayTimelineEvent:
    kind: str
    id: str
    timestamp: str
    status: str


@dataclass(frozen=True, slots=True)
class HarnessReplayArtifact:
    id: str
    kind: str
    reference: str
    status: Literal[
        "verified",
        "missing",
        "digest_mismatch",
        "unsafe_path",
        "unsupported",
        "malformed",
        "unreadable",
    ]
    expected_sha256: str = ""
    actual_sha256: str = ""


@dataclass(frozen=True, slots=True)
class HarnessReplayDifference:
    field: str
    baseline: str
    current: str


@dataclass(frozen=True, slots=True)
class HarnessReplayBaselinePayload:
    run_id: str
    manifest_json: str
    manifest_sha256: str
    rule_version: str
    explanation_json: str
    explanation_sha256: str


@dataclass(frozen=True, slots=True)
class HarnessReplayResult:
    run_id: str
    status: HarnessReplayStatus
    baseline_manifest_sha256: str
    current_manifest_sha256: str
    baseline_rule_version: str
    current_rule_version: str
    baseline_explanation_sha256: str
    current_explanation_sha256: str
    timeline: tuple[HarnessReplayTimelineEvent, ...]
    artifacts: tuple[HarnessReplayArtifact, ...]
    anomalies: tuple[str, ...]
    differences: tuple[HarnessReplayDifference, ...]
    legacy_baseline_created: bool = False


@dataclass(frozen=True, slots=True)
class HarnessReplayLookup:
    status: HarnessReplayLookupStatus
    result: HarnessReplayResult | None = None
    message: str = ""


__all__ = [
    "HarnessReplayArtifact",
    "HarnessReplayBaselinePayload",
    "HarnessReplayDifference",
    "HarnessReplayLookup",
    "HarnessReplayResult",
    "HarnessReplayStatus",
    "HarnessReplayTimelineEvent",
]
