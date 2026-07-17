from __future__ import annotations

from dataclasses import replace

import pytest

from naumi_agent.harness.eval_surface import (
    HarnessEvalBaselineStatus,
    HarnessEvalBaselineView,
    HarnessEvalBatchProgress,
    HarnessEvalComparisonView,
    HarnessEvalPromotionFlowStatus,
)
from naumi_agent.harness.explain import (
    HarnessExplainCheck,
    HarnessExplainCriterion,
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
    harness_eval_baseline_payload,
    harness_eval_batch_payload,
    harness_eval_promotion_payload,
    harness_explain_payload,
    harness_replay_payload,
)


def test_harness_eval_baseline_payload_is_typed_bounded_and_deterministic() -> None:
    baseline_id = "a" * 64
    status = HarnessEvalBaselineStatus(
        status="ok",
        suite_id="surface-protocol",
        active=HarnessEvalBaselineView(
            id=baseline_id,
            version=2,
            batch_id="candidate-2",
            sample_count=5,
            identity_sha256="b" * 64,
            samples_sha256="c" * 64,
            promoted_by="user",
            promotion_reason="验证新协议" + "x" * 600,
            created_at="2026-07-18T10:00:00+00:00",
        ),
        comparisons=tuple(
            HarnessEvalComparisonView(
                id=f"{index:064x}",
                baseline_id=baseline_id,
                current_batch_id=f"candidate-{index}",
                decision="passed",
                statistical_verdict="unchanged",
                current_samples=5,
                created_at="2026-07-18T10:01:00+00:00",
            )
            for index in range(25)
        ),
    )

    first = harness_eval_baseline_payload(status)
    second = harness_eval_baseline_payload(status)

    assert first == second
    assert set(first) == {
        "schema_version",
        "snapshot_sha256",
        "status",
        "suite_id",
        "message",
        "active",
        "comparisons",
    }
    assert len(first["snapshot_sha256"]) == 64
    assert len(first["active"]["promotion_reason"]) == 500
    assert len(first["comparisons"]) == 20


def test_harness_eval_baseline_payload_rejects_incoherent_status() -> None:
    with pytest.raises(ValueError, match="active"):
        harness_eval_baseline_payload(
            HarnessEvalBaselineStatus(status="ok", suite_id="surface-protocol")
        )


def test_harness_eval_batch_payload_distinguishes_progress_and_terminal() -> None:
    evaluating = harness_eval_batch_payload(
        HarnessEvalBatchProgress(
            stage="evaluating",
            batch_id="candidate-1",
            suite_id="surface-protocol",
            requested=5,
            completed=2,
            persisted=0,
            passed_cases=4,
            duration_ms=12.3456,
        )
    )
    completed = harness_eval_batch_payload(
        HarnessEvalBatchProgress(
            stage="completed",
            batch_id="candidate-1",
            suite_id="surface-protocol",
            requested=5,
            completed=5,
            persisted=5,
            identity_sha256="a" * 64,
            baseline_eligible=True,
        )
    )

    assert evaluating["terminal"] is False
    assert evaluating["duration_ms"] == 12.346
    assert completed["terminal"] is True
    assert completed["baseline_eligible"] is True

    with pytest.raises(ValueError, match="persisted"):
        HarnessEvalBatchProgress(
            stage="persisting",
            batch_id="candidate-1",
            suite_id="surface-protocol",
            requested=5,
            completed=2,
            persisted=3,
        )
    with pytest.raises(ValueError, match="eligible"):
        HarnessEvalBatchProgress(
            stage="partial",
            batch_id="candidate-1",
            suite_id="surface-protocol",
            requested=5,
            completed=2,
            persisted=2,
            identity_sha256="a" * 64,
            baseline_eligible=True,
        )
    with pytest.raises(ValueError, match="comparisons"):
        harness_eval_baseline_payload(
            HarnessEvalBaselineStatus(
                status="empty",
                suite_id="surface-protocol",
                comparisons=(
                    HarnessEvalComparisonView(
                        id="d" * 64,
                        baseline_id="a" * 64,
                        current_batch_id="candidate",
                        decision="passed",
                        statistical_verdict="unchanged",
                        current_samples=5,
                        created_at="2026-07-18T10:01:00+00:00",
                    ),
                ),
            )
        )


def test_harness_eval_promotion_payload_preserves_guided_and_terminal_state() -> None:
    waiting = harness_eval_promotion_payload(
        HarnessEvalPromotionFlowStatus(
            stage="awaiting_confirmation",
            suite_id="surface-protocol",
            batch_id="candidate-1",
            promotion_reason="完整回归已通过",
        )
    )
    promoted = harness_eval_promotion_payload(
        HarnessEvalPromotionFlowStatus(
            stage="promoted",
            suite_id="surface-protocol",
            batch_id="candidate-1",
            baseline_id="a" * 64,
            active_baseline_id="a" * 64,
            version=1,
            sample_count=5,
            promoted_by="user",
            promotion_reason="完整回归已通过",
            created_at="2026-07-18T10:00:00+00:00",
        )
    )

    assert waiting["terminal"] is False
    assert waiting["promotion_reason"] == "完整回归已通过"
    assert promoted["terminal"] is True
    assert promoted["baseline_id"] == "a" * 64

    with pytest.raises(ValueError, match="确认阶段"):
        HarnessEvalPromotionFlowStatus(
            stage="awaiting_confirmation",
            suite_id="surface-protocol",
            batch_id="candidate-1",
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
        criteria=tuple(
            HarnessExplainCriterion(
                id=f"criterion-{index}",
                status="unsatisfied",
                description="必须通过验证" + "d" * 600,
                evidence_ids=tuple(f"evidence-{item}" for item in range(110)),
            )
            for index in range(105)
        ),
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
    assert len(explanation["criteria"]) == 100
    assert len(explanation["criteria"][0]["description"]) == 500
    assert len(explanation["criteria"][0]["evidence_ids"]) == 100
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


def test_successful_harness_lookups_require_authoritative_results() -> None:
    with pytest.raises(ValueError, match="explanation"):
        harness_explain_payload(
            "detail-run",
            HarnessExplainLookup(status="ok", explanation=None),
        )
    with pytest.raises(ValueError, match="result"):
        harness_replay_payload(
            "detail-run",
            HarnessReplayLookup(status="ok", result=None),
        )


def test_harness_payloads_reject_mutable_or_mismatched_run_state() -> None:
    explanation = _explanation()
    with pytest.raises(ValueError, match="run_id"):
        harness_explain_payload(
            "detail-run",
            HarnessExplainLookup(
                status="ok",
                explanation=replace(explanation, run_id="other-run"),
            ),
        )
    with pytest.raises(ValueError, match="尚未完成"):
        harness_explain_payload(
            "detail-run",
            HarnessExplainLookup(
                status="ok",
                explanation=replace(explanation, status="running", running=True),
            ),
        )

    replay = _replay_result()
    with pytest.raises(ValueError, match="run_id"):
        harness_replay_payload(
            "detail-run",
            HarnessReplayLookup(
                status="ok",
                result=replace(replay, run_id="other-run"),
            ),
        )
    with pytest.raises(ValueError, match="尚未完成"):
        harness_replay_payload(
            "detail-run",
            HarnessReplayLookup(
                status="ok",
                result=replace(replay, anomalies=("run_not_finished",)),
            ),
        )


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
