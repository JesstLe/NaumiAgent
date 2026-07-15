"""Deterministic Harness replay without tool, model, check, or session execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from naumi_agent.harness.explain import (
    HARNESS_EXPLAIN_RULE_VERSION,
    HarnessExplainer,
)
from naumi_agent.harness.replay_models import (
    HarnessReplayArtifact,
    HarnessReplayBaselinePayload,
    HarnessReplayDifference,
    HarnessReplayResult,
    HarnessReplayTimelineEvent,
)
from naumi_agent.harness.store import HarnessStoredRun

HARNESS_REPLAY_MANIFEST_VERSION = 1
_KNOWN_EVIDENCE_KINDS = frozenset(
    {"check_output", "file", "note", "source", "test_report", "tool_execution"}
)


class _ReplayBaseline(Protocol):
    manifest_json: str
    manifest_sha256: str
    rule_version: str
    explanation_json: str
    explanation_sha256: str


def capture_replay_baseline(
    run: HarnessStoredRun,
    *,
    workspace_root: str | Path,
    rule_version: str = HARNESS_EXPLAIN_RULE_VERSION,
) -> HarnessReplayBaselinePayload:
    """Capture immutable normalized facts and artifact digests for one run."""
    workspace = Path(workspace_root).expanduser().resolve()
    artifact_baselines = tuple(
        _capture_check_artifact(check.id, check.artifact_path, workspace)
        for check in run.checks
        if check.artifact_path
    )
    manifest = _manifest_value(
        run,
        artifact_baselines=artifact_baselines,
        rule_version=rule_version,
    )
    manifest_json = canonical_json(manifest)
    explanation_json = canonical_json(HarnessExplainer().explain(run))
    return HarnessReplayBaselinePayload(
        run_id=run.id,
        manifest_json=manifest_json,
        manifest_sha256=_sha256_text(manifest_json),
        rule_version=rule_version,
        explanation_json=explanation_json,
        explanation_sha256=_sha256_text(explanation_json),
    )


def replay_stored_run(
    run: HarnessStoredRun,
    *,
    baseline: _ReplayBaseline,
    workspace_root: str | Path,
    rule_version: str = HARNESS_EXPLAIN_RULE_VERSION,
    legacy_baseline_created: bool = False,
) -> HarnessReplayResult:
    """Replay one stored run against an immutable baseline."""
    workspace = Path(workspace_root).expanduser().resolve()
    baseline_integrity = _sha256_text(baseline.manifest_json)
    baseline_manifest: dict[str, Any] | None
    try:
        loaded = json.loads(baseline.manifest_json)
        baseline_manifest = loaded if isinstance(loaded, dict) else None
    except json.JSONDecodeError:
        baseline_manifest = None

    artifacts: tuple[HarnessReplayArtifact, ...] = ()
    anomalies: list[str] = []
    if baseline_manifest is None:
        current_manifest_json = canonical_json(
            _manifest_value(run, artifact_baselines=(), rule_version=rule_version)
        )
        differences = (
            HarnessReplayDifference(
                field="baseline_manifest",
                baseline=baseline.manifest_sha256,
                current="invalid",
            ),
        )
        return HarnessReplayResult(
            run_id=run.id,
            status="corrupt",
            baseline_manifest_sha256=baseline.manifest_sha256,
            current_manifest_sha256=_sha256_text(current_manifest_json),
            baseline_rule_version=baseline.rule_version,
            current_rule_version=rule_version,
            baseline_explanation_sha256=baseline.explanation_sha256,
            current_explanation_sha256=_sha256_text(
                canonical_json(HarnessExplainer().explain(run))
            ),
            timeline=_assemble_timeline(run),
            artifacts=(),
            anomalies=("baseline_manifest_invalid",),
            differences=differences,
            legacy_baseline_created=legacy_baseline_created,
        )

    artifact_baselines = tuple(baseline_manifest.get("check_artifacts") or ())
    baseline_manifest_version = baseline_manifest.get("manifest_version")
    normalized_manifest_version = (
        baseline_manifest_version
        if isinstance(baseline_manifest_version, int)
        and not isinstance(baseline_manifest_version, bool)
        else HARNESS_REPLAY_MANIFEST_VERSION
    )
    current_manifest_json = canonical_json(
        _manifest_value(
            run,
            artifact_baselines=artifact_baselines,
            rule_version=baseline.rule_version,
            manifest_version=normalized_manifest_version,
        )
    )
    current_manifest_sha256 = _sha256_text(current_manifest_json)
    current_explanation_json = canonical_json(HarnessExplainer().explain(run))
    current_explanation_sha256 = _sha256_text(current_explanation_json)

    artifacts = tuple(
        [
            _verify_check_artifact(item, workspace)
            for item in artifact_baselines
            if isinstance(item, dict)
        ]
        + [_verify_evidence(item, run.id, workspace) for item in run.evidence]
    )
    anomalies.extend(_run_anomalies(run))
    if normalized_manifest_version < HARNESS_REPLAY_MANIFEST_VERSION:
        anomalies.append(f"old_manifest_version:{normalized_manifest_version}")
    elif normalized_manifest_version > HARNESS_REPLAY_MANIFEST_VERSION:
        anomalies.append(f"unsupported_manifest_version:{normalized_manifest_version}")
    anomalies.extend(
        f"artifact_unsupported:{item.id}"
        for item in artifacts
        if item.status in {"unsupported", "malformed", "unsafe_path"}
    )
    if legacy_baseline_created:
        anomalies.append("legacy_baseline_created")

    differences_list: list[HarnessReplayDifference] = []
    corrupt = False
    changed = False
    partial = bool(anomalies)
    if normalized_manifest_version > HARNESS_REPLAY_MANIFEST_VERSION:
        corrupt = True
    if baseline_integrity != baseline.manifest_sha256:
        corrupt = True
        differences_list.append(
            HarnessReplayDifference(
                field="baseline_manifest_integrity",
                baseline=baseline.manifest_sha256,
                current=baseline_integrity,
            )
        )
    baseline_explanation_integrity = _sha256_text(baseline.explanation_json)
    if baseline_explanation_integrity != baseline.explanation_sha256:
        corrupt = True
        differences_list.append(
            HarnessReplayDifference(
                field="baseline_explanation_integrity",
                baseline=baseline.explanation_sha256,
                current=baseline_explanation_integrity,
            )
        )
    if current_manifest_sha256 != baseline.manifest_sha256:
        corrupt = True
        differences_list.append(
            HarnessReplayDifference(
                field="manifest_sha256",
                baseline=baseline.manifest_sha256,
                current=current_manifest_sha256,
            )
        )
    if rule_version != baseline.rule_version:
        changed = True
        differences_list.append(
            HarnessReplayDifference(
                field="rule_version",
                baseline=baseline.rule_version,
                current=rule_version,
            )
        )
    if current_explanation_sha256 != baseline.explanation_sha256:
        changed = True
        differences_list.append(
            HarnessReplayDifference(
                field="explanation_sha256",
                baseline=baseline.explanation_sha256,
                current=current_explanation_sha256,
            )
        )
    for artifact in artifacts:
        if artifact.status in {"digest_mismatch", "malformed"}:
            corrupt = True
        elif artifact.status != "verified":
            partial = True

    if corrupt:
        status = "corrupt"
    elif changed:
        status = "changed"
    elif partial:
        status = "partial"
    else:
        status = "reproduced"
    return HarnessReplayResult(
        run_id=run.id,
        status=status,
        baseline_manifest_sha256=baseline.manifest_sha256,
        current_manifest_sha256=current_manifest_sha256,
        baseline_rule_version=baseline.rule_version,
        current_rule_version=rule_version,
        baseline_explanation_sha256=baseline.explanation_sha256,
        current_explanation_sha256=current_explanation_sha256,
        timeline=_assemble_timeline(run),
        artifacts=artifacts,
        anomalies=tuple(dict.fromkeys(anomalies)),
        differences=tuple(differences_list),
        legacy_baseline_created=legacy_baseline_created,
    )


def canonical_json(value: Any) -> str:
    """Encode replay values deterministically."""
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _manifest_value(
    run: HarnessStoredRun,
    *,
    artifact_baselines: tuple[Any, ...],
    rule_version: str,
    manifest_version: int = HARNESS_REPLAY_MANIFEST_VERSION,
) -> dict[str, Any]:
    return {
        "manifest_version": manifest_version,
        "rule_version": rule_version,
        "run": {
            "id": run.id,
            "workspace_root": run.workspace_root,
            "session_id": run.session_id,
            "task_id": run.task_id,
            "issue_id": run.issue_id,
            "task_kind": run.task_kind,
            "objective": run.objective,
            "status": run.status,
            "profile_digest": run.profile_digest,
            "tree_fingerprint_before": run.tree_fingerprint_before,
            "tree_fingerprint_after": run.tree_fingerprint_after,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
        },
        "contract": run.contract,
        "receipt": run.receipt,
        "criteria": run.criteria,
        "checks": run.checks,
        "evidence": run.evidence,
        "timeline": _assemble_timeline(run),
        "check_artifacts": artifact_baselines,
    }


def _assemble_timeline(run: HarnessStoredRun) -> tuple[HarnessReplayTimelineEvent, ...]:
    ranked: list[tuple[str, int, str, HarnessReplayTimelineEvent]] = [
        (
            run.started_at,
            0,
            run.id,
            HarnessReplayTimelineEvent(
                kind="run_started",
                id=run.id,
                timestamp=run.started_at,
                status="running",
            ),
        )
    ]
    ranked.extend(
        (
            check.completed_at or check.started_at,
            1,
            check.id,
            HarnessReplayTimelineEvent(
                kind="check",
                id=check.id,
                timestamp=check.completed_at or check.started_at,
                status=check.status,
            ),
        )
        for check in run.checks
    )
    ranked.extend(
        (
            evidence.created_at,
            2,
            evidence.id,
            HarnessReplayTimelineEvent(
                kind="evidence",
                id=evidence.id,
                timestamp=evidence.created_at,
                status=str(evidence.summary.get("status") or evidence.kind),
            ),
        )
        for evidence in run.evidence
    )
    if run.completed_at:
        ranked.append(
            (
                run.completed_at,
                3,
                run.id,
                HarnessReplayTimelineEvent(
                    kind="run_finished",
                    id=run.id,
                    timestamp=run.completed_at,
                    status=run.status,
                ),
            )
        )
    return tuple(item[3] for item in sorted(ranked, key=lambda item: item[:3]))


def _run_anomalies(run: HarnessStoredRun) -> tuple[str, ...]:
    anomalies: list[str] = []
    if not run.completed_at or run.receipt is None or run.status == "running":
        anomalies.append("run_not_finished")
    anomalies.extend(
        f"tool_start_missing:{item.id}"
        for item in run.evidence
        if item.kind == "tool_execution" and bool(item.summary.get("start_missing"))
    )
    anomalies.extend(
        f"unknown_evidence_kind:{item.id}"
        for item in run.evidence
        if item.kind not in _KNOWN_EVIDENCE_KINDS
    )
    counts: dict[str, int] = {}
    for item in run.evidence:
        counts[item.id] = counts.get(item.id, 0) + 1
    anomalies.extend(
        f"duplicate_evidence:{evidence_id}"
        for evidence_id, count in sorted(counts.items())
        if count > 1
    )
    return tuple(anomalies)


def _capture_check_artifact(
    check_id: str,
    reference: str,
    workspace: Path,
) -> dict[str, str]:
    path = _safe_workspace_path(reference, workspace)
    if path is None:
        return {"id": check_id, "reference": reference, "sha256": "", "status": "unsafe_path"}
    if not path.is_file():
        return {"id": check_id, "reference": reference, "sha256": "", "status": "missing"}
    try:
        digest = _sha256_file(path)
    except OSError:
        return {
            "id": check_id,
            "reference": reference,
            "sha256": "",
            "status": "unreadable",
        }
    return {"id": check_id, "reference": reference, "sha256": digest, "status": "captured"}


def _verify_check_artifact(item: dict[str, Any], workspace: Path) -> HarnessReplayArtifact:
    identifier = str(item.get("id") or "check-artifact")
    reference = str(item.get("reference") or "")
    expected = str(item.get("sha256") or "")
    path = _safe_workspace_path(reference, workspace)
    if path is None:
        status = "unsafe_path"
        actual = ""
    elif not path.is_file():
        status = "missing"
        actual = ""
    else:
        try:
            actual = _sha256_file(path)
        except OSError:
            status = "unreadable"
            actual = ""
        else:
            if not expected:
                status = "unreadable"
            else:
                status = "verified" if actual == expected else "digest_mismatch"
    return HarnessReplayArtifact(
        id=identifier,
        kind="check_artifact",
        reference=reference,
        status=status,
        expected_sha256=expected,
        actual_sha256=actual,
    )


def _verify_evidence(
    evidence: Any,
    run_id: str,
    workspace: Path,
) -> HarnessReplayArtifact:
    parsed = urlsplit(evidence.uri)
    if parsed.scheme == "chat-run":
        expected_path = f"/tool/{evidence.id}"
        valid_uri = unquote(parsed.netloc) == run_id and unquote(parsed.path) == expected_path
        actual = _sha256_text(canonical_json(evidence.summary))
        if not valid_uri:
            status = "malformed"
        else:
            status = "verified" if actual == evidence.sha256 else "digest_mismatch"
        return HarnessReplayArtifact(
            id=evidence.id,
            kind="evidence",
            reference=evidence.uri,
            status=status,
            expected_sha256=evidence.sha256,
            actual_sha256=actual,
        )
    if parsed.scheme == "artifact":
        reference = unquote(
            "/".join(part for part in (parsed.netloc, parsed.path.lstrip("/")) if part)
        )
        path = _safe_workspace_path(reference, workspace)
        if path is None:
            status = "unsafe_path"
            actual = ""
        elif not path.is_file():
            status = "missing"
            actual = ""
        else:
            try:
                actual = _sha256_file(path)
            except OSError:
                status = "unreadable"
                actual = ""
            else:
                status = "verified" if actual == evidence.sha256 else "digest_mismatch"
        return HarnessReplayArtifact(
            id=evidence.id,
            kind="evidence",
            reference=evidence.uri,
            status=status,
            expected_sha256=evidence.sha256,
            actual_sha256=actual,
        )
    return HarnessReplayArtifact(
        id=evidence.id,
        kind="evidence",
        reference=evidence.uri,
        status="unsupported",
        expected_sha256=evidence.sha256,
    )


def _safe_workspace_path(reference: str, workspace: Path) -> Path | None:
    if not reference or "\x00" in reference:
        return None
    candidate = Path(reference).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if not resolved.is_relative_to(workspace):
        return None
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


__all__ = [
    "HARNESS_REPLAY_MANIFEST_VERSION",
    "canonical_json",
    "capture_replay_baseline",
    "replay_stored_run",
]
