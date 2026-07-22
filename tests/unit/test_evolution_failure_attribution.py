from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import naumi_agent.evolution.evaluation_lane_receipts as evaluation_lane_module
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.evaluation_lane_receipts import (
    EvaluationLaneKind,
    EvolutionEvaluationLaneReceiptBuilder,
    EvolutionEvaluationLaneReceiptError,
    EvolutionEvaluationLaneReceiptExecutor,
    EvolutionEvaluationLaneReceiptStore,
    render_evaluation_lane_receipt,
)
from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionAuthority,
    EvolutionFailureAttributionKernel,
    EvolutionFailureAttributionStore,
    FailureAttributionCategory,
    _classify,
)
from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalMetricObservation,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_receipt import (
    EvalReceiptSample,
    build_eval_comparison_receipt,
    eval_result_sha256,
    eval_sample_set_sha256,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.tools.evolution_review import EvolutionEvaluationReceiptTool


def _result(
    *,
    commit: str,
    status: EvalCaseStatus,
    repetitions: int,
    policy: HarnessEvalComparisonPolicy | None = None,
    suite_sha256: str = "a" * 64,
    observed_tokens: int | None = None,
    observed_cost_usd: float | None = None,
    token_target: int | None = None,
    cost_target_usd: float | None = None,
) -> HarnessEvalSuiteResult:
    comparison_policy = policy or HarnessEvalComparisonPolicy()
    configuration = HarnessEvalConfigurationIdentity.create(
        suite_id="attribution",
        suite_sha256=suite_sha256,
        profile_sha256="b" * 64,
        policy_sha256=comparison_policy.sha256,
        runner_version="attribution@1",
        repetitions=repetitions,
        live=False,
    )
    identity = build_eval_baseline_identity(
        Path("."),
        configuration=configuration,
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
    observations = tuple(
        item
        for item in (
            HarnessEvalMetricObservation(
                metric="cost_usd",
                value=observed_cost_usd,
                unit="usd",
                direction="decrease",
                target=(
                    cost_target_usd
                    if cost_target_usd is not None
                    else observed_cost_usd + 1
                ),
                primary=observed_tokens is None,
            )
            if observed_cost_usd is not None
            else None,
            HarnessEvalMetricObservation(
                metric="tokens_used",
                value=observed_tokens,
                unit="tokens",
                direction="decrease",
                target=(
                    token_target
                    if token_target is not None
                    else observed_tokens + 1
                ),
                primary=True,
            )
            if observed_tokens is not None
            else None,
        )
        if item is not None
    )
    primary_metric = (
        "tokens_used"
        if observed_tokens is not None
        else "cost_usd"
        if observed_cost_usd is not None
        else "outcome"
    )
    return HarnessEvalSuiteResult(
        suite_id="attribution",
        title="Failure attribution fixture",
        suite_path="evals/attribution.yaml",
        suite_sha256=suite_sha256,
        status=(
            EvalRunStatus.PASSED
            if status is EvalCaseStatus.PASSED
            else EvalRunStatus.FAILED
        ),
        cases=(HarnessEvalCaseResult(
            case_id="target",
            runner="attribution@1",
            status=status,
            primary_metric=primary_metric,
            metric_observations=observations,
        ),),
        comparison_policy=comparison_policy,
        baseline_identity=identity,
        duration_ms=10,
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


def _receipt(
    tmp_path: Path,
    baseline: tuple[HarnessEvalSuiteResult, ...],
    current: tuple[HarnessEvalSuiteResult, ...],
):
    baseline_samples = _samples(baseline)
    return build_eval_comparison_receipt(
        workspace_root=tmp_path,
        suite_id="attribution",
        baseline_id="c" * 64,
        baseline_batch_id="red",
        baseline_samples_sha256=eval_sample_set_sha256(baseline_samples),
        baseline_samples=baseline_samples,
        current_batch_id="green",
        current_samples=_samples(current),
        created_at="2026-07-19T04:00:00+08:00",
    )


def test_failure_attribution_classifies_candidate_flaky_and_infrastructure(
    tmp_path: Path,
) -> None:
    baseline = tuple(
        _result(commit="1", status=EvalCaseStatus.PASSED, repetitions=5)
        for _ in range(5)
    )
    failed = tuple(
        _result(
            commit="2",
            status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
            repetitions=5,
        )
        for _ in range(5)
    )
    permissive = HarnessEvalComparisonPolicy(
        min_pass_rate=0,
        max_implementation_failures=1,
        max_regressions=1,
        max_pass_rate_drop=1,
    )
    permissive_baseline = tuple(
        _result(
            commit="3",
            status=EvalCaseStatus.PASSED,
            repetitions=5,
            policy=permissive,
        )
        for _ in range(5)
    )
    mixed = [
        _result(
            commit="4",
            status=EvalCaseStatus.PASSED,
            repetitions=5,
            policy=permissive,
        )
        for _ in range(5)
    ]
    mixed[-1] = _result(
        commit="4",
        status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
        repetitions=5,
        policy=permissive,
    )
    unstable = tuple(
        _result(
            commit="5",
            status=EvalCaseStatus.EVALUATION_ERROR,
            repetitions=5,
        )
        for _ in range(5)
    )

    candidate = _classify(_receipt(tmp_path, baseline, failed))
    flaky = _classify(_receipt(tmp_path, permissive_baseline, tuple(mixed)))
    infrastructure = _classify(_receipt(tmp_path, baseline, unstable))

    assert candidate["category"] == FailureAttributionCategory.CANDIDATE_DEFECT
    assert candidate["candidate_fault"] is True
    assert flaky["category"] == FailureAttributionCategory.FLAKY_EVIDENCE
    assert flaky["requires_rerun"] is True
    assert infrastructure["category"] == (
        FailureAttributionCategory.EVALUATION_INFRASTRUCTURE
    )
    assert infrastructure["retryable"] is True


def test_failure_attribution_classifies_incomplete_and_incompatible(
    tmp_path: Path,
) -> None:
    short_baseline = tuple(
        _result(commit="1", status=EvalCaseStatus.PASSED, repetitions=4)
        for _ in range(4)
    )
    short_current = tuple(
        _result(commit="2", status=EvalCaseStatus.PASSED, repetitions=4)
        for _ in range(4)
    )
    baseline = tuple(
        _result(commit="3", status=EvalCaseStatus.PASSED, repetitions=5)
        for _ in range(5)
    )
    incompatible = tuple(
        _result(
            commit="4",
            status=EvalCaseStatus.PASSED,
            repetitions=5,
            suite_sha256="d" * 64,
        )
        for _ in range(5)
    )

    incomplete = _classify(_receipt(tmp_path, short_baseline, short_current))
    environment = _classify(_receipt(tmp_path, baseline, incompatible))

    assert incomplete["category"] == FailureAttributionCategory.EVIDENCE_INCOMPLETE
    assert incomplete["reason_code"] == "sample_count_insufficient"
    assert environment["category"] == (
        FailureAttributionCategory.ENVIRONMENT_INCOMPATIBLE
    )
    assert environment["action"] == "rebuild_environment"


@pytest.mark.asyncio
async def test_evaluation_lane_receipt_reloads_and_summarizes_real_h5_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness_store = HarnessStore(tmp_path / "harness.db")
    baseline = tuple(
        _result(
            commit="1",
            status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
            repetitions=5,
            observed_tokens=200,
            observed_cost_usd=0.5,
            token_target=150,
            cost_target_usd=1,
        )
        for _ in range(5)
    )
    candidate = tuple(
        _result(
            commit="2",
            status=EvalCaseStatus.PASSED,
            repetitions=5,
            observed_tokens=100,
            observed_cost_usd=0.25,
            token_target=150,
            cost_target_usd=1,
        )
        for _ in range(5)
    )
    for batch_id, results, created_at in (
        ("red", baseline, "2026-07-19T03:58:00+08:00"),
        ("green", candidate, "2026-07-19T03:59:00+08:00"),
    ):
        for index, result in enumerate(results):
            await harness_store.record_eval_result(
                workspace_root=workspace,
                batch_id=batch_id,
                sample_index=index,
                result=result,
                created_at=created_at,
            )
    reference = await harness_store.register_eval_comparison_reference(
        workspace_root=workspace,
        batch_id="red",
        suite_id="attribution",
        registered_by="evolution-test",
        registration_reason="Evaluation Lane RED reference",
        created_at="2026-07-19T03:58:30+08:00",
    )
    red_records = await harness_store.list_eval_results(
        workspace, "red", "attribution"
    )
    green_records = await harness_store.list_eval_results(
        workspace, "green", "attribution"
    )
    comparison_receipt = build_eval_comparison_receipt(
        workspace_root=workspace,
        suite_id="attribution",
        baseline_id=reference.id,
        baseline_batch_id="red",
        baseline_samples_sha256=reference.samples_sha256,
        baseline_samples=tuple(
            EvalReceiptSample(
                sample_index=item.sample_index,
                result_sha256=item.result_sha256,
                result=item.result,
            )
            for item in red_records
        ),
        current_batch_id="green",
        current_samples=tuple(
            EvalReceiptSample(
                sample_index=item.sample_index,
                result_sha256=item.result_sha256,
                result=item.result,
            )
            for item in green_records
        ),
        created_at="2026-07-19T04:00:00+08:00",
    )
    comparison = await harness_store.record_eval_comparison_receipt(
        comparison_receipt
    )
    authority = EvolutionFailureAttributionAuthority(
        validation_plan_id=f"evvplan_{'a' * 24}",
        validation_plan_sha256="b" * 64,
        red_receipt_id=f"evvredcohort_{'1' * 24}",
        red_receipt_sha256="2" * 64,
        green_receipt_id=f"evvgreencohort_{'3' * 24}",
        green_receipt_sha256="4" * 64,
        candidate_id=f"evc_{'5' * 24}",
        candidate_revision=3,
        suite_id="attribution",
        red_batch_id="red",
        green_batch_id="green",
        red_samples=5,
        green_samples=5,
        red_result_sha256=tuple(item.result_sha256 for item in red_records),
        green_result_sha256=tuple(item.result_sha256 for item in green_records),
    )
    attribution_store = EvolutionFailureAttributionStore(tmp_path / "evolution.db")
    attribution = await attribution_store.record(
        EvolutionFailureAttributionKernel().build(
            authority=authority,
            comparison=comparison,
        )
    )
    receipt_store = EvolutionEvaluationLaneReceiptStore(tmp_path / "evolution.db")
    executor = EvolutionEvaluationLaneReceiptExecutor(
        harness_store=harness_store,
        attribution_store=attribution_store,
        receipt_store=receipt_store,
    )

    with pytest.raises(EvolutionEvaluationLaneReceiptError) as forged_h5c:
        await executor.execute(
            comparison=replace(comparison, decision="failed"),
            attribution=attribution,
        )
    assert forged_h5c.value.code == "evaluation_lane_comparison_not_authoritative"
    with pytest.raises(EvolutionEvaluationLaneReceiptError) as forged_attribution:
        await executor.execute(
            comparison=comparison,
            attribution=attribution.model_copy(update={"reason_code": "forged"}),
        )
    assert (
        forged_attribution.value.code
        == "evaluation_lane_attribution_not_authoritative"
    )
    with pytest.raises(EvolutionEvaluationLaneReceiptError) as partial_candidate:
        EvolutionEvaluationLaneReceiptBuilder().build(
            comparison=comparison,
            attribution=attribution,
            baseline_records=red_records,
            candidate_records=green_records[:-1],
        )
    assert partial_candidate.value.code == "evaluation_lane_cohort_mismatch"

    receipt = await executor.execute(
        comparison=comparison,
        attribution=attribution,
    )
    repeated = await executor.execute(
        comparison=comparison,
        attribution=attribution,
    )
    restored = await EvolutionEvaluationLaneReceiptStore(
        tmp_path / "evolution.db"
    ).get(comparison.id)

    assert receipt == repeated == restored
    assert receipt.lane_kind is EvaluationLaneKind.INTERVENTIONAL
    assert receipt.platform == "linux"
    assert receipt.candidate_revision == 3
    assert receipt.comparison_decision == "passed"
    assert receipt.statistical_verdict == "improved"
    assert receipt.failure_category is FailureAttributionCategory.NONE
    assert receipt.reflection_eligible
    assert receipt.baseline.samples == receipt.candidate.samples == 5
    assert receipt.baseline.failed_samples == 5
    assert receipt.candidate.passed_samples == 5
    assert receipt.baseline.duration_ms == receipt.candidate.duration_ms == 50
    assert receipt.baseline.observed_tokens == 1_000
    assert receipt.baseline.observed_cost_usd == 2.5
    assert receipt.baseline.token_samples == receipt.baseline.cost_samples == 5
    assert receipt.candidate.observed_tokens == 500
    assert receipt.candidate.observed_cost_usd == 1.25
    assert receipt.candidate.token_samples == receipt.candidate.cost_samples == 5
    assert tuple(item.kind for item in receipt.artifacts) == (
        "red_completion",
        "green_completion",
        "baseline_samples",
        "candidate_samples",
        "comparison",
        "failure_attribution",
    )
    assert receipt.candidate_evaluation_complete is False
    assert receipt.aggregation_required is True
    assert receipt.comparison_receipt == comparison.receipt
    assert receipt.failure_attribution_receipt == attribution
    forged_payload = receipt.model_dump(mode="json")
    forged_payload["failure_category"] = "candidate_defect"
    forged_digest = evaluation_lane_module._sha256_payload({  # noqa: SLF001
        key: value
        for key, value in forged_payload.items()
        if key not in {"receipt_id", "receipt_sha256"}
    })
    forged_payload["receipt_id"] = f"evlane_{forged_digest[:24]}"
    forged_payload["receipt_sha256"] = forged_digest
    with pytest.raises(ValueError, match="typed source receipts"):
        evaluation_lane_module.EvolutionEvaluationLaneReceipt.model_validate(
            forged_payload
        )
    engine = SimpleNamespace(
        workspace_root=workspace,
        evolution_evaluation_lane_receipt_executor=executor,
    )
    tool_rendered = await EvolutionEvaluationReceiptTool(engine).execute(
        comparison.id
    )
    slash_rendered = await execute_slash_command(
        engine,
        f"/evolution evaluation {comparison.id}",
    )
    assert tool_rendered == render_evaluation_lane_receipt(receipt)
    assert receipt.receipt_id in slash_rendered
    golden = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures/ui17/evaluation-lane-receipt-golden.json"
        ).read_text(encoding="utf-8")
    )
    for marker in golden["tui_markers"]:
        assert marker in tool_rendered
    assert "cost $2.5" in tool_rendered
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    with pytest.raises(EvolutionEvaluationLaneReceiptError) as crossed_workspace:
        await executor.execute_by_id(
            workspace_root=other_workspace,
            comparison_id=comparison.id,
        )
    assert crossed_workspace.value.code == "evaluation_lane_comparison_missing"

    with sqlite3.connect(tmp_path / "evolution.db") as db:
        db.execute(
            "UPDATE evolution_evaluation_lane_receipts "
            "SET receipt_sha256 = ? WHERE comparison_id = ?",
            ("0" * 64, comparison.id),
        )
        db.commit()
    with pytest.raises(EvolutionEvaluationLaneReceiptError) as corrupt:
        await EvolutionEvaluationLaneReceiptStore(tmp_path / "evolution.db").get(
            comparison.id
        )
    assert corrupt.value.code == "evaluation_lane_store_corrupt"
