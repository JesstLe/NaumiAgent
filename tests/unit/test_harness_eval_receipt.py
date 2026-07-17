from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalGuardrailStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalGuardrailResult,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_receipt import (
    EvalComparisonDecision,
    EvalReceiptSample,
    HarnessEvalComparisonReceipt,
    build_eval_comparison_receipt,
    eval_result_sha256,
    eval_sample_set_sha256,
)
from naumi_agent.harness.store import (
    HARNESS_STORE_SCHEMA_VERSION,
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoreError,
)

_NOW = "2026-07-18T11:00:00+08:00"
_LATER = "2026-07-18T11:01:00+08:00"


def _identity(
    *,
    commit: str,
    policy: HarnessEvalComparisonPolicy,
    suite_sha256: str = "a" * 64,
):
    return build_eval_baseline_identity(
        Path("."),
        configuration=HarnessEvalConfigurationIdentity.create(
            suite_id="receipt-protocol",
            suite_sha256=suite_sha256,
            profile_sha256="b" * 64,
            policy_sha256=policy.sha256,
            runner_version="protocol_hello@1",
            repetitions=5,
            live=False,
        ),
        source_identity=HarnessEvalSourceIdentity(
            commit=commit * 40,
            tree_sha256=f"sha256:{commit * 64}",
            dirty=False,
        ),
        platform_identity=HarnessEvalPlatformIdentity(
            system="linux",
            release="6.12",
            machine="x86_64",
            python_implementation="CPython",
            python_version="3.13.5",
            naumi_version="0.1.214",
        ),
    )


def _result(
    *,
    commit: str,
    status: EvalCaseStatus = EvalCaseStatus.PASSED,
    policy: HarnessEvalComparisonPolicy | None = None,
    duration_ms: float = 10,
    suite_sha256: str = "a" * 64,
) -> HarnessEvalSuiteResult:
    comparison_policy = policy or HarnessEvalComparisonPolicy()
    return HarnessEvalSuiteResult(
        suite_id="receipt-protocol",
        title="比较回执协议评测",
        suite_path="evals/receipt-protocol.yaml",
        suite_sha256=suite_sha256,
        status=(
            EvalRunStatus.PASSED
            if status is EvalCaseStatus.PASSED
            else EvalRunStatus.FAILED
        ),
        cases=(
            HarnessEvalCaseResult(
                case_id="hello",
                runner="protocol_hello",
                status=status,
                primary_metric="protocol_outcome_match",
                guardrails=(
                    HarnessEvalGuardrailResult(
                        guardrail="no_model",
                        status=EvalGuardrailStatus.PASSED,
                    ),
                    HarnessEvalGuardrailResult(
                        guardrail="no_side_effect",
                        status=EvalGuardrailStatus.PASSED,
                    ),
                ),
            ),
        ),
        comparison_policy=comparison_policy,
        baseline_identity=_identity(
            commit=commit,
            policy=comparison_policy,
            suite_sha256=suite_sha256,
        ),
        duration_ms=duration_ms,
    )


def _samples(results: tuple[HarnessEvalSuiteResult, ...]) -> tuple[EvalReceiptSample, ...]:
    return tuple(
        EvalReceiptSample(
            sample_index=index,
            result_sha256=eval_result_sha256(result),
            result=result,
        )
        for index, result in enumerate(results)
    )


def _stable_results(*, commit: str) -> tuple[HarnessEvalSuiteResult, ...]:
    return tuple(_result(commit=commit, duration_ms=10 + index) for index in range(5))


def _build(
    tmp_path: Path,
    baseline: tuple[EvalReceiptSample, ...],
    current: tuple[EvalReceiptSample, ...],
    *,
    created_at: str = _NOW,
):
    return build_eval_comparison_receipt(
        workspace_root=tmp_path,
        suite_id="receipt-protocol",
        baseline_id="c" * 64,
        baseline_batch_id="baseline-001",
        baseline_samples_sha256=eval_sample_set_sha256(baseline),
        baseline_samples=baseline,
        current_batch_id="candidate-001",
        current_samples=current,
        created_at=created_at,
    )


def test_receipt_preserves_mechanical_policy_and_statistical_evidence(
    tmp_path: Path,
) -> None:
    baseline = _samples(_stable_results(commit="1"))
    current = _samples(_stable_results(commit="2"))

    receipt = _build(tmp_path, baseline, current)

    assert receipt.decision is EvalComparisonDecision.PASSED
    assert receipt.statistical_verdict == "unchanged"
    assert receipt.baseline_samples == receipt.current_samples == 5
    assert {item.mechanical_verdict for item in receipt.sample_evidence} == {
        "unchanged"
    }
    assert {item.policy_verdict for item in receipt.sample_evidence} == {"passed"}
    assert receipt.receipt_sha256 != receipt.id


def test_policy_failure_and_allowed_flakiness_remain_distinct(tmp_path: Path) -> None:
    baseline = _samples(_stable_results(commit="1"))
    failed = _samples(
        tuple(
            _result(commit="2", status=EvalCaseStatus.IMPLEMENTATION_FAILURE)
            for _ in range(5)
        )
    )
    failed_receipt = _build(tmp_path, baseline, failed)

    permissive = HarnessEvalComparisonPolicy(
        min_pass_rate=0,
        max_regressions=1,
        max_implementation_failures=1,
        max_pass_rate_drop=1,
    )
    permissive_baseline = _samples(
        tuple(_result(commit="3", policy=permissive) for _ in range(5))
    )
    mixed_results = [
        _result(commit="4", policy=permissive) for _ in range(5)
    ]
    mixed_results[-1] = _result(
        commit="4",
        status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
        policy=permissive,
    )
    flaky_receipt = _build(
        tmp_path,
        permissive_baseline,
        _samples(tuple(mixed_results)),
    )

    assert failed_receipt.decision is EvalComparisonDecision.FAILED
    assert failed_receipt.statistical_verdict == "regressed"
    assert all(item.violation_codes for item in failed_receipt.sample_evidence)
    assert flaky_receipt.decision is EvalComparisonDecision.FLAKY
    assert flaky_receipt.statistical_verdict == "flaky"
    assert all(item.policy_verdict == "passed" for item in flaky_receipt.sample_evidence)


def test_receipt_rejects_tampered_digest_gaps_and_incompatible_identity(
    tmp_path: Path,
) -> None:
    baseline = _samples(_stable_results(commit="1"))
    current = _samples(_stable_results(commit="2"))
    tampered = _build(tmp_path, baseline, current).model_dump(mode="json")
    tampered["receipt_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        HarnessEvalComparisonReceipt.model_validate(tampered)
    wrong_decision = _build(tmp_path, baseline, current).model_dump(mode="json")
    wrong_decision["decision"] = "failed"
    with pytest.raises(ValueError, match="decision"):
        HarnessEvalComparisonReceipt.model_validate(wrong_decision)
    with pytest.raises(ValueError, match="连续"):
        _build(tmp_path, baseline, (current[0], *current[2:]))

    incompatible = _samples(
        tuple(_result(commit="2", suite_sha256="d" * 64) for _ in range(5))
    )
    receipt = _build(tmp_path, baseline, incompatible)
    assert receipt.decision is EvalComparisonDecision.INCOMPATIBLE
    assert receipt.statistical_verdict == "incompatible"


@pytest.mark.asyncio
async def test_receipt_store_is_immutable_scoped_and_tamper_evident(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    baseline_results = _stable_results(commit="1")
    current_results = _stable_results(commit="2")
    for index, result in enumerate(baseline_results):
        await store.record_eval_result(
            workspace_root=workspace,
            batch_id="baseline-001",
            sample_index=index,
            result=result,
            created_at=_NOW,
        )
    baseline = await store.promote_eval_baseline(
        workspace_root=workspace,
        batch_id="baseline-001",
        suite_id="receipt-protocol",
        promoted_by="Harness-Test",
        promotion_reason="五次稳定全绿样本",
        created_at=_NOW,
    )
    for index, result in enumerate(current_results):
        await store.record_eval_result(
            workspace_root=workspace,
            batch_id="candidate-001",
            sample_index=index,
            result=result,
            created_at=_LATER,
        )
    baseline_records = await store.list_eval_results(
        workspace,
        "baseline-001",
        "receipt-protocol",
    )
    current_records = await store.list_eval_results(
        workspace,
        "candidate-001",
        "receipt-protocol",
    )
    baseline_samples = tuple(
        EvalReceiptSample(
            sample_index=item.sample_index,
            result_sha256=item.result_sha256,
            result=item.result,
        )
        for item in baseline_records
    )
    current_samples = tuple(
        EvalReceiptSample(
            sample_index=item.sample_index,
            result_sha256=item.result_sha256,
            result=item.result,
        )
        for item in current_records
    )
    receipt = build_eval_comparison_receipt(
        workspace_root=workspace,
        suite_id="receipt-protocol",
        baseline_id=baseline.id,
        baseline_batch_id=baseline.batch_id,
        baseline_samples_sha256=baseline.samples_sha256,
        baseline_samples=baseline_samples,
        current_batch_id="candidate-001",
        current_samples=current_samples,
        created_at=_LATER,
    )

    first = await store.record_eval_comparison_receipt(receipt)
    retry = await store.record_eval_comparison_receipt(receipt)
    restored = await HarnessStore(store.db_path).get_eval_comparison_receipt(
        workspace,
        "receipt-protocol",
        baseline.id,
        "candidate-001",
    )
    listed = await store.list_eval_comparison_receipts(
        workspace,
        "receipt-protocol",
    )

    assert retry == first == restored
    assert listed == (first,)
    assert first.receipt.decision is EvalComparisonDecision.PASSED
    assert await store.get_eval_comparison_receipt(
        other,
        "receipt-protocol",
        baseline.id,
        "candidate-001",
    ) is None
    conflicting = build_eval_comparison_receipt(
        workspace_root=workspace,
        suite_id="receipt-protocol",
        baseline_id=baseline.id,
        baseline_batch_id=baseline.batch_id,
        baseline_samples_sha256=baseline.samples_sha256,
        baseline_samples=baseline_samples,
        current_batch_id="candidate-001",
        current_samples=current_samples,
        created_at="2026-07-18T11:02:00+08:00",
    )
    with pytest.raises(HarnessStoreConflictError, match="不可覆盖"):
        await store.record_eval_comparison_receipt(conflicting)

    with sqlite3.connect(store.db_path) as db:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        db.execute(
            "UPDATE harness_eval_comparison_receipts SET decision = 'failed'"
        )
        db.commit()
    assert version == HARNESS_STORE_SCHEMA_VERSION == 10
    with pytest.raises(HarnessStoreError, match="损坏"):
        await HarnessStore(store.db_path).get_eval_comparison_receipt(
            workspace,
            "receipt-protocol",
            baseline.id,
            "candidate-001",
        )
