"""Bounded public JSON payloads for Harness detail lookups."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from naumi_agent.harness.checks import validate_run_id
from naumi_agent.harness.explain import HarnessExplainLookup, HarnessRunExplanation
from naumi_agent.harness.replay_models import HarnessReplayLookup, HarnessReplayResult

HARNESS_DETAIL_SCHEMA_VERSION = 1
HARNESS_DETAIL_REVISION = 1
HARNESS_DETAIL_TEXT_LIMIT = 500

_LOOKUP_STATUSES = {"ok", "not_found", "unavailable"}


def harness_explain_payload(
    run_id: str,
    lookup: HarnessExplainLookup,
    *,
    revision: int = HARNESS_DETAIL_REVISION,
) -> dict[str, Any]:
    """Serialize one Explain lookup without leaking unbounded Store values."""
    payload = _lookup_header(run_id, lookup.status, lookup.message, revision)
    if lookup.status == "ok":
        if lookup.explanation is None:
            raise ValueError("Harness Explain 成功结果缺少 explanation。")
        if lookup.explanation.run_id != payload["run_id"]:
            raise ValueError("Harness Explain explanation.run_id 与请求不一致。")
        if lookup.explanation.running or lookup.explanation.status == "running":
            raise ValueError("Harness Explain 运行尚未完成，不能固定为 revision 1。")
        payload["explanation"] = _serialize_explanation(lookup.explanation)
    return payload


def harness_replay_payload(
    run_id: str,
    lookup: HarnessReplayLookup,
    *,
    revision: int = HARNESS_DETAIL_REVISION,
) -> dict[str, Any]:
    """Serialize one Replay lookup without executing or reading artifact bodies."""
    payload = _lookup_header(run_id, lookup.status, lookup.message, revision)
    if lookup.status == "ok":
        if lookup.result is None:
            raise ValueError("Harness Replay 成功结果缺少 result。")
        if lookup.result.run_id != payload["run_id"]:
            raise ValueError("Harness Replay result.run_id 与请求不一致。")
        if "run_not_finished" in lookup.result.anomalies:
            raise ValueError("Harness Replay 运行尚未完成，不能固定为 revision 1。")
        payload["result"] = _serialize_replay(lookup.result)
    return payload


def _lookup_header(
    run_id: str,
    lookup_status: str,
    message: str,
    revision: int,
) -> dict[str, Any]:
    normalized_run_id = validate_run_id(run_id)
    normalized_status = str(lookup_status)
    if normalized_status not in _LOOKUP_STATUSES:
        raise ValueError(f"未知 Harness lookup_status: {normalized_status}")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("Harness detail revision 必须是正整数。")
    return {
        "schema_version": HARNESS_DETAIL_SCHEMA_VERSION,
        "revision": revision,
        "run_id": normalized_run_id,
        "lookup_status": normalized_status,
        "message": _text(message),
    }


def _serialize_explanation(value: HarnessRunExplanation) -> dict[str, Any]:
    return {
        "status": _text(value.status),
        "objective": _text(value.objective),
        "started_at": _text(value.started_at),
        "completed_at": _text(value.completed_at),
        "verified": bool(value.verified),
        "running": bool(value.running),
        "summary": _text(value.summary),
        "failure_classes": _texts(value.failure_classes, limit=20),
        "findings": [
            {
                "failure_class": _text(item.failure_class),
                "source": _text(item.source),
                "message": _text(item.message),
                "next_step": _text(item.next_step),
                "check_ids": _texts(item.check_ids, limit=50),
                "evidence_ids": _texts(item.evidence_ids, limit=100),
            }
            for item in value.findings[:20]
        ],
        "checks": [
            {
                "id": _text(item.id),
                "status": _text(item.status),
                "duration_ms": _nonnegative_int(item.duration_ms),
            }
            for item in value.checks[:50]
        ],
        "evidence": [
            {
                "id": _text(item.id),
                "kind": _text(item.kind),
                "status": _text(item.status),
                "digest_prefix": _text(item.digest_prefix),
                "uri": _text(item.uri),
            }
            for item in value.evidence[:100]
        ],
    }


def _serialize_replay(value: HarnessReplayResult) -> dict[str, Any]:
    return {
        "status": _text(value.status),
        "baseline_manifest_sha256": _text(value.baseline_manifest_sha256),
        "current_manifest_sha256": _text(value.current_manifest_sha256),
        "baseline_rule_version": _text(value.baseline_rule_version),
        "current_rule_version": _text(value.current_rule_version),
        "baseline_explanation_sha256": _text(value.baseline_explanation_sha256),
        "current_explanation_sha256": _text(value.current_explanation_sha256),
        "timeline": [
            {
                "kind": _text(item.kind),
                "id": _text(item.id),
                "timestamp": _text(item.timestamp),
                "status": _text(item.status),
            }
            for item in value.timeline[:200]
        ],
        "artifacts": [
            {
                "id": _text(item.id),
                "kind": _text(item.kind),
                "reference": _text(item.reference),
                "status": _text(item.status),
                "expected_sha256": _text(item.expected_sha256),
                "actual_sha256": _text(item.actual_sha256),
            }
            for item in value.artifacts[:100]
        ],
        "anomalies": _texts(value.anomalies, limit=50),
        "differences": [
            {
                "field": _text(item.field),
                "baseline": _text(item.baseline),
                "current": _text(item.current),
            }
            for item in value.differences[:50]
        ],
        "legacy_baseline_created": bool(value.legacy_baseline_created),
    }


def _text(value: object) -> str:
    return str(value or "").strip()[:HARNESS_DETAIL_TEXT_LIMIT]


def _texts(values: Iterable[object], *, limit: int) -> list[str]:
    result: list[str] = []
    for value in values:
        if len(result) >= limit:
            break
        result.append(_text(value))
    return result


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


__all__ = [
    "HARNESS_DETAIL_REVISION",
    "HARNESS_DETAIL_SCHEMA_VERSION",
    "harness_explain_payload",
    "harness_replay_payload",
]
