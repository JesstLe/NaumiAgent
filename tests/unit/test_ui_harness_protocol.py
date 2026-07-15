from __future__ import annotations

import pytest

from naumi_agent.harness.explain import (
    HarnessExplainCheck,
    HarnessExplainEvidence,
    HarnessExplainFinding,
    HarnessExplainLookup,
    HarnessFailureClass,
    HarnessRunExplanation,
)
from naumi_agent.harness.replay_models import (
    HarnessReplayArtifact,
    HarnessReplayDifference,
    HarnessReplayLookup,
    HarnessReplayResult,
    HarnessReplayTimelineEvent,
)
from naumi_agent.ui.harness_protocol import (
    harness_explain_payload,
    harness_replay_payload,
)


def _explanation() -> HarnessRunExplanation:
    finding = HarnessExplainFinding(
        failure_class=HarnessFailureClass.VERIFICATION_FAILURE,
        source="check:unit" + "x" * 600,
        message="验证失败" + "m" * 600,
        next_step="重新运行" + "n" * 600,
        check_ids=tuple(f"check-{index}" for index in range(60)),
        evidence_ids=tuple(f"evidence-{index}" for index in range(110)),
    )
    return HarnessRunExplanation(
        run_id="detail-run",
        status="completed_unverified",
        objective="修复并验证" + "o" * 600,
        started_at="2026-07-15T10:00:00+00:00",
        completed_at="2026-07-15T10:01:00+00:00",
        verified=False,
        running=False,
        summary="发现验证问题",
        failure_classes=tuple(
            HarnessFailureClass.VERIFICATION_FAILURE for _ in range(25)
        ),
        findings=tuple(finding for _ in range(25)),
        checks=tuple(
            HarnessExplainCheck(id=f"check-{index}", status="failed", duration_ms=-5)
            for index in range(55)
        ),
        evidence=tuple(
            HarnessExplainEvidence(
                id=f"evidence-{index}",
                kind="test_report",
                status="missing",
                digest_prefix="a" * 64,
                uri="artifact://" + "u" * 600,
            )
            for index in range(105)
        ),
    )


def _replay_result() -> HarnessReplayResult:
    return HarnessReplayResult(
        run_id="detail-run",
        status="partial",
        baseline_manifest_sha256="a" * 64,
        current_manifest_sha256="b" * 64,
        baseline_rule_version="1",
        current_rule_version="1",
        baseline_explanation_sha256="c" * 64,
        current_explanation_sha256="d" * 64,
        timeline=tuple(
            HarnessReplayTimelineEvent(
                kind="check",
                id=f"timeline-{index}",
                timestamp="2026-07-15T10:00:00+00:00",
                status="passed",
            )
            for index in range(205)
        ),
        artifacts=tuple(
            HarnessReplayArtifact(
                id=f"artifact-{index}",
                kind="test_report",
                reference="artifact://" + "r" * 600,
                status="verified",
                expected_sha256="e" * 64,
                actual_sha256="e" * 64,
            )
            for index in range(105)
        ),
        anomalies=tuple("anomaly" + "a" * 600 for _ in range(55)),
        differences=tuple(
            HarnessReplayDifference(
                field=f"field-{index}",
                baseline="before" + "b" * 600,
                current="after" + "c" * 600,
            )
            for index in range(55)
        ),
        legacy_baseline_created=True,
    )


def test_harness_explain_payload_is_strict_and_bounded() -> None:
    payload = harness_explain_payload(
        "detail-run",
        HarnessExplainLookup(status="ok", explanation=_explanation()),
    )

    assert set(payload) == {
        "schema_version",
        "revision",
        "run_id",
        "lookup_status",
        "message",
        "explanation",
    }
    assert payload["revision"] == 1
    explanation = payload["explanation"]
    assert len(explanation["objective"]) == 500
    assert len(explanation["failure_classes"]) == 20
    assert len(explanation["findings"]) == 20
    assert len(explanation["findings"][0]["check_ids"]) == 50
    assert len(explanation["findings"][0]["evidence_ids"]) == 100
    assert len(explanation["checks"]) == 50
    assert explanation["checks"][0]["duration_ms"] == 0
    assert len(explanation["evidence"]) == 100
    assert len(explanation["evidence"][0]["uri"]) == 500
    assert explanation["verified"] is False


def test_harness_replay_payload_is_strict_and_bounded() -> None:
    payload = harness_replay_payload(
        "detail-run",
        HarnessReplayLookup(status="ok", result=_replay_result()),
    )

    assert set(payload) == {
        "schema_version",
        "revision",
        "run_id",
        "lookup_status",
        "message",
        "result",
    }
    result = payload["result"]
    assert result["status"] == "partial"
    assert len(result["timeline"]) == 200
    assert len(result["artifacts"]) == 100
    assert len(result["artifacts"][0]["reference"]) == 500
    assert len(result["anomalies"]) == 50
    assert len(result["anomalies"][0]) == 500
    assert len(result["differences"]) == 50
    assert len(result["differences"][0]["baseline"]) == 500
    assert result["legacy_baseline_created"] is True


@pytest.mark.parametrize("status", ["not_found", "unavailable"])
def test_harness_lookup_failures_do_not_fabricate_results(status: str) -> None:
    explain = harness_explain_payload(
        "detail-run",
        HarnessExplainLookup(status=status, message="暂不可用" + "x" * 600),  # type: ignore[arg-type]
    )
    replay = harness_replay_payload(
        "detail-run",
        HarnessReplayLookup(status=status, message="暂不可用" + "x" * 600),  # type: ignore[arg-type]
    )

    assert "explanation" not in explain
    assert "result" not in replay
    assert len(explain["message"]) == 500
    assert len(replay["message"]) == 500


@pytest.mark.parametrize(
    ("run_id", "revision"),
    [("../outside", 1), ("detail-run", 0), ("detail-run", True)],
)
def test_harness_payload_header_rejects_invalid_identity(
    run_id: str,
    revision: object,
) -> None:
    with pytest.raises(ValueError):
        harness_explain_payload(
            run_id,
            HarnessExplainLookup(status="not_found"),
            revision=revision,  # type: ignore[arg-type]
        )
