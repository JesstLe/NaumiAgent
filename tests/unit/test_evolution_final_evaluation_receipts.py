from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import naumi_agent.evolution.adversarial_cohort as adversarial_cohort_module
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.adversarial_batch_requests import (
    EvolutionAdversarialBatchRequest,
    EvolutionAdversarialBatchRequestBuilder,
)
from naumi_agent.evolution.adversarial_cohort import (
    AdversarialCohortCheckSummary,
    EvolutionAdversarialCohortReceipt,
)
from naumi_agent.evolution.adversarial_cohort_receipts import (
    EvolutionAdversarialCohortReceiptStore,
    EvolutionAdversarialCohortReceiptStoreError,
)
from naumi_agent.evolution.adversarial_probe_contracts import (
    AdversarialProbeDefinition,
    EvolutionAdversarialProbeRegistry,
)
from naumi_agent.evolution.evaluation_aggregation_contracts import (
    EvolutionEvaluationAggregationContractIssuer,
    EvolutionEvaluationAggregationContractStore,
)
from naumi_agent.evolution.evaluation_lane_receipts import (
    EvolutionEvaluationLaneReceipt,
    EvolutionEvaluationLaneReceiptBuilder,
    EvolutionEvaluationLaneReceiptStore,
)
from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionAuthority,
    EvolutionFailureAttributionKernel,
)
from naumi_agent.evolution.final_evaluation_receipts import (
    EvolutionFinalEvaluationReceipt,
    EvolutionFinalEvaluationReceiptError,
    EvolutionFinalEvaluationReceiptExecutor,
    EvolutionFinalEvaluationReceiptStore,
    render_final_evaluation_receipt,
)
from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
    capture_eval_platform_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_receipt import (
    EvalReceiptSample,
    build_eval_comparison_receipt,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.tools.evolution_review import EvolutionFinalEvaluationReceiptTool
from tests.unit.test_evolution_experiment_leases import (
    _adversarial_probe_fixture,
)


def _current_only_registry() -> EvolutionAdversarialProbeRegistry:
    return EvolutionAdversarialProbeRegistry((
        AdversarialProbeDefinition(
            probe_id="boundary-v1",
            kind="boundary",
            version=1,
            path_patterns=(),
            always_required=True,
            platform_scope="current",
        ),
    ))


def _result(
    workspace: Path,
    *,
    suite_id: str,
    suite_sha256: str,
    platform: HarnessEvalPlatformIdentity,
    commit_digit: str,
    status: EvalCaseStatus,
) -> HarnessEvalSuiteResult:
    policy = HarnessEvalComparisonPolicy()
    configuration = HarnessEvalConfigurationIdentity.create(
        suite_id=suite_id,
        suite_sha256=suite_sha256,
        profile_sha256="b" * 64,
        policy_sha256=policy.sha256,
        runner_version="final-evaluation-fixture@1",
        repetitions=5,
        live=False,
    )
    identity = build_eval_baseline_identity(
        workspace,
        configuration=configuration,
        source_identity=HarnessEvalSourceIdentity(
            commit=commit_digit * 40,
            tree_sha256=f"sha256:{commit_digit * 64}",
            dirty=status is EvalCaseStatus.PASSED,
        ),
        platform_identity=platform,
    )
    run_status = (
        EvalRunStatus.PASSED
        if status is EvalCaseStatus.PASSED
        else EvalRunStatus.FAILED
    )
    return HarnessEvalSuiteResult(
        suite_id=suite_id,
        title="Final Evaluation fixture",
        suite_path=f"evals/{suite_id}.yaml",
        suite_sha256=suite_sha256,
        status=run_status,
        cases=(HarnessEvalCaseResult(
            case_id="target",
            runner="final-evaluation-fixture@1",
            status=status,
        ),),
        comparison_policy=policy,
        baseline_identity=identity,
        duration_ms=10,
    )


async def _lane(
    *,
    store: HarnessStore,
    workspace: Path,
    suite_id: str,
    suite_sha256: str,
    platform: HarnessEvalPlatformIdentity,
    red_batch_id: str,
    green_batch_id: str,
    red_completion_id: str,
    red_completion_sha256: str,
    green_completion_id: str,
    green_completion_sha256: str,
    validation_plan_id: str,
    validation_plan_sha256: str,
    candidate_id: str,
    candidate_revision: int,
) -> EvolutionEvaluationLaneReceipt:
    baseline = tuple(
        _result(
            workspace,
            suite_id=suite_id,
            suite_sha256=suite_sha256,
            platform=platform,
            commit_digit="1",
            status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
        )
        for _ in range(5)
    )
    candidate = tuple(
        _result(
            workspace,
            suite_id=suite_id,
            suite_sha256=suite_sha256,
            platform=platform,
            commit_digit="2",
            status=EvalCaseStatus.PASSED,
        )
        for _ in range(5)
    )
    for batch_id, results, created_at in (
        (red_batch_id, baseline, "2026-07-22T10:00:00+08:00"),
        (green_batch_id, candidate, "2026-07-22T10:01:00+08:00"),
    ):
        for index, result in enumerate(results):
            await store.record_eval_result(
                workspace_root=workspace,
                batch_id=batch_id,
                sample_index=index,
                result=result,
                created_at=created_at,
            )
    reference = await store.register_eval_comparison_reference(
        workspace_root=workspace,
        batch_id=red_batch_id,
        suite_id=suite_id,
        registered_by="final-evaluation-test",
        registration_reason="Final Evaluation fixture RED reference",
        created_at="2026-07-22T10:00:30+08:00",
    )
    red_records = await store.list_eval_results(workspace, red_batch_id, suite_id)
    green_records = await store.list_eval_results(workspace, green_batch_id, suite_id)
    comparison = await store.record_eval_comparison_receipt(
        build_eval_comparison_receipt(
            workspace_root=workspace,
            suite_id=suite_id,
            baseline_id=reference.id,
            baseline_batch_id=red_batch_id,
            baseline_samples_sha256=reference.samples_sha256,
            baseline_samples=tuple(
                EvalReceiptSample(
                    sample_index=item.sample_index,
                    result_sha256=item.result_sha256,
                    result=item.result,
                )
                for item in red_records
            ),
            current_batch_id=green_batch_id,
            current_samples=tuple(
                EvalReceiptSample(
                    sample_index=item.sample_index,
                    result_sha256=item.result_sha256,
                    result=item.result,
                )
                for item in green_records
            ),
            created_at="2026-07-22T10:02:00+08:00",
        )
    )
    attribution = EvolutionFailureAttributionKernel().build(
        authority=EvolutionFailureAttributionAuthority(
            validation_plan_id=validation_plan_id,
            validation_plan_sha256=validation_plan_sha256,
            red_receipt_id=red_completion_id,
            red_receipt_sha256=red_completion_sha256,
            green_receipt_id=green_completion_id,
            green_receipt_sha256=green_completion_sha256,
            candidate_id=candidate_id,
            candidate_revision=candidate_revision,
            suite_id=suite_id,
            red_batch_id=red_batch_id,
            green_batch_id=green_batch_id,
            red_samples=5,
            green_samples=5,
            red_result_sha256=tuple(item.result_sha256 for item in red_records),
            green_result_sha256=tuple(item.result_sha256 for item in green_records),
        ),
        comparison=comparison,
    )
    return EvolutionEvaluationLaneReceiptBuilder().build(
        comparison=comparison,
        attribution=attribution,
        baseline_records=red_records,
        candidate_records=green_records,
    )


def _cohort(
    request: EvolutionAdversarialBatchRequest,
    lane,
    lane_receipt: EvolutionEvaluationLaneReceipt | None,
    *,
    phase: str,
    platform: HarnessEvalPlatformIdentity,
) -> EvolutionAdversarialCohortReceipt:
    is_red = phase == "red"
    result_digests = (
        tuple(item.result_sha256 for item in lane_receipt.comparison_receipt.sample_evidence)
        if lane_receipt is not None and not is_red
        else tuple(f"{index + 1:064x}" for index in range(5))
    )
    if lane_receipt is not None and is_red:
        # H5c stores the aggregate digest for RED but not each RED sample. The caller
        # replaces this provisional cohort after the lane is built.
        result_digests = tuple(f"{index + 11:064x}" for index in range(5))
    summary = AdversarialCohortCheckSummary(
        check_id=request.checks[0].check_id,
        metric_name=f"adversarial.{request.checks[0].check_id}.exit_zero",
        passed=0 if is_red else 5,
        failed=5 if is_red else 0,
        evaluation_errors=0,
        sample_values=(0.0,) * 5 if is_red else (1.0,) * 5,
    )
    overlay = None if is_red else "c" * 64
    payload = {
        "schema_version": 1,
        "policy_version": "evolution-adversarial-cohort-v1",
        "request_id": request.request_id,
        "request_sha256": request.request_sha256,
        "probe_contract_id": request.probe_contract_id,
        "probe_contract_sha256": request.probe_contract_sha256,
        "validation_plan_id": request.validation_plan_id,
        "validation_plan_sha256": request.validation_plan_sha256,
        "lease_id": request.lease_id,
        "candidate_id": request.candidate_id,
        "candidate_revision": request.candidate_revision,
        "candidate_files_sha256": request.candidate_files_sha256,
        "lane_order": lane.order,
        "platform": lane.platform,
        "phase": phase,
        "batch_id": lane.batch_id,
        "suite_id": request.suite_id,
        "authority_key": hashlib.sha256(
            f"{request.request_sha256}:{lane.order}:{lane.batch_id}".encode("ascii")
        ).hexdigest(),
        "requested_samples": 5,
        "persisted_samples": 5,
        "sample_seeds": list(request.sample_seeds),
        "sample_receipt_sha256": [f"{index + 21:064x}" for index in range(5)],
        "sample_result_sha256": list(result_digests),
        "run_grant_sha256": ["d" * 64],
        "baseline_identity_sha256": "e" * 64,
        "source_tree_sha256": (
            f"sha256:{request.baseline_tree_sha256}" if is_red else f"sha256:{overlay}"
        ),
        "overlay_source_sha256": overlay,
        "checks": [summary.model_dump(mode="json")],
        "continuous_sample_indexes_verified": True,
        "harness_batch_coordinator_used": True,
        "cohort_scoped_run_grant_used": True,
        "profile_trust_revalidated": True,
        "lease_revalidated": True,
        "platform_revalidated": True,
        "source_revalidated": True,
        "arc04_worker_used": True,
        "project_code_executed": True,
        "cohort_complete": True,
        "completed_at": "2026-07-22T10:02:00+08:00",
    }
    digest = adversarial_cohort_module._sha256_payload(payload)  # noqa: SLF001
    return EvolutionAdversarialCohortReceipt.model_validate({
        **payload,
        "receipt_id": f"evadvcohort_{digest[:24]}",
        "receipt_sha256": digest,
    })


@pytest.mark.asyncio
async def test_final_evaluation_receipt_reloads_exact_complete_authority(
    tmp_path: Path,
) -> None:
    workspace, experiment, _, plan, probes, _ = await _adversarial_probe_fixture(
        tmp_path,
        registry=_current_only_registry(),
    )
    request = EvolutionAdversarialBatchRequestBuilder().build(
        experiment_contract=experiment,
        validation_plan=plan,
        probe_contract=probes,
    )
    assert request.required_platforms == (capture_eval_platform_identity().system,)
    contract_store = EvolutionEvaluationAggregationContractStore(tmp_path / "evolution.db")
    contract = await EvolutionEvaluationAggregationContractIssuer(
        store=contract_store
    ).issue(workspace_root=workspace, batch_request=request)
    harness_store = HarnessStore(tmp_path / "harness.db")
    lane_store = EvolutionEvaluationLaneReceiptStore(tmp_path / "evolution.db")
    cohort_store = EvolutionAdversarialCohortReceiptStore(tmp_path / "evolution.db")
    platform = capture_eval_platform_identity()
    red_lane, green_lane = request.lanes

    provisional_red = _cohort(request, red_lane, None, phase="red", platform=platform)
    provisional_green = _cohort(
        request,
        green_lane,
        None,
        phase="green",
        platform=platform,
    )
    adversarial_lane = await _lane(
        store=harness_store,
        workspace=workspace,
        suite_id=request.suite_id,
        suite_sha256=request.probe_contract_sha256,
        platform=platform,
        red_batch_id=red_lane.batch_id,
        green_batch_id=green_lane.batch_id,
        red_completion_id=provisional_red.receipt_id,
        red_completion_sha256=provisional_red.receipt_sha256,
        green_completion_id=provisional_green.receipt_id,
        green_completion_sha256=provisional_green.receipt_sha256,
        validation_plan_id=plan.validation_plan_id,
        validation_plan_sha256=plan.validation_plan_sha256,
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
    )
    red_records = await harness_store.list_eval_results(
        workspace, red_lane.batch_id, request.suite_id
    )
    green_records = await harness_store.list_eval_results(
        workspace, green_lane.batch_id, request.suite_id
    )

    def completion(lane, phase, records):
        base = _cohort(request, lane, None, phase=phase, platform=platform)
        payload = base.model_dump(
            mode="json", exclude={"receipt_id", "receipt_sha256"}
        )
        payload["sample_result_sha256"] = [item.result_sha256 for item in records]
        digest = adversarial_cohort_module._sha256_payload(payload)  # noqa: SLF001
        return EvolutionAdversarialCohortReceipt.model_validate({
            **payload,
            "receipt_id": f"evadvcohort_{digest[:24]}",
            "receipt_sha256": digest,
        })

    red_completion = completion(red_lane, "red", red_records)
    green_completion = completion(green_lane, "green", green_records)
    # Rebuild through the signed H5b authority so H5c binds exact durable completions.
    authority = EvolutionFailureAttributionAuthority(
        validation_plan_id=plan.validation_plan_id,
        validation_plan_sha256=plan.validation_plan_sha256,
        red_receipt_id=red_completion.receipt_id,
        red_receipt_sha256=red_completion.receipt_sha256,
        green_receipt_id=green_completion.receipt_id,
        green_receipt_sha256=green_completion.receipt_sha256,
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
        suite_id=request.suite_id,
        red_batch_id=red_lane.batch_id,
        green_batch_id=green_lane.batch_id,
        red_samples=5,
        green_samples=5,
        red_result_sha256=tuple(item.result_sha256 for item in red_records),
        green_result_sha256=tuple(item.result_sha256 for item in green_records),
    )
    comparison = await harness_store.get_eval_comparison_receipt_by_id(
        workspace, adversarial_lane.comparison_id
    )
    assert comparison is not None
    attribution = EvolutionFailureAttributionKernel().build(
        authority=authority,
        comparison=comparison,
    )
    adversarial_lane = EvolutionEvaluationLaneReceiptBuilder().build(
        comparison=comparison,
        attribution=attribution,
        baseline_records=red_records,
        candidate_records=green_records,
    )
    interventional_lane = await _lane(
        store=harness_store,
        workspace=workspace,
        suite_id="final_interventional",
        suite_sha256="f" * 64,
        platform=platform,
        red_batch_id="final:interventional:red",
        green_batch_id="final:interventional:green",
        red_completion_id=f"evvredcohort_{'1' * 24}",
        red_completion_sha256="2" * 64,
        green_completion_id=f"evvgreencohort_{'3' * 24}",
        green_completion_sha256="4" * 64,
        validation_plan_id=plan.validation_plan_id,
        validation_plan_sha256=plan.validation_plan_sha256,
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
    )
    await lane_store.record(interventional_lane)
    await lane_store.record(adversarial_lane)
    final_store = EvolutionFinalEvaluationReceiptStore(tmp_path / "evolution.db")
    executor = EvolutionFinalEvaluationReceiptExecutor(
        contract_store=contract_store,
        lane_store=lane_store,
        cohort_store=cohort_store,
        receipt_store=final_store,
    )

    with pytest.raises(EvolutionFinalEvaluationReceiptError) as missing_completion:
        await executor.execute(
            aggregation_contract_id=contract.contract_id,
            interventional_comparison_id=interventional_lane.comparison_id,
            adversarial_comparison_ids=(adversarial_lane.comparison_id,),
        )
    assert missing_completion.value.code == "final_evaluation_completion_missing"

    await cohort_store.record(red_completion)
    await cohort_store.record(green_completion)
    conflicting_payload = red_completion.model_dump(
        mode="json", exclude={"receipt_id", "receipt_sha256"}
    )
    conflicting_payload["completed_at"] = "2026-07-22T10:03:00+08:00"
    conflicting_digest = adversarial_cohort_module._sha256_payload(  # noqa: SLF001
        conflicting_payload
    )
    conflicting_completion = EvolutionAdversarialCohortReceipt.model_validate({
        **conflicting_payload,
        "receipt_id": f"evadvcohort_{conflicting_digest[:24]}",
        "receipt_sha256": conflicting_digest,
    })
    with pytest.raises(EvolutionAdversarialCohortReceiptStoreError) as conflict:
        await cohort_store.record(conflicting_completion)
    assert conflict.value.code == "adversarial_cohort_receipt_conflict"

    receipt = await executor.execute(
        aggregation_contract_id=contract.contract_id,
        interventional_comparison_id=interventional_lane.comparison_id,
        adversarial_comparison_ids=(adversarial_lane.comparison_id,),
    )
    repeated = await asyncio.gather(*(
        executor.execute(
            aggregation_contract_id=contract.contract_id,
            interventional_comparison_id=interventional_lane.comparison_id,
            adversarial_comparison_ids=(adversarial_lane.comparison_id,),
        )
        for _ in range(4)
    ))
    assert all(item == receipt for item in repeated)
    assert receipt.candidate_evaluation_complete is True
    assert receipt.aggregation_required is False
    assert receipt.mechanical_gate_input_ready is True
    assert receipt.candidate_acceptance_decided is False
    assert receipt.promotion_ready is False
    assert receipt.required_platforms == request.required_platforms
    assert receipt.lane_count == 2
    assert receipt.receipt_id == f"evfinal_{receipt.receipt_sha256[:24]}"
    assert await final_store.get(contract.contract_id) == receipt

    engine = SimpleNamespace(evolution_final_evaluation_receipt_executor=executor)
    tool_output = await EvolutionFinalEvaluationReceiptTool(engine).execute(
        contract.contract_id,
        interventional_lane.comparison_id,
        [adversarial_lane.comparison_id],
    )
    slash_output = await execute_slash_command(
        engine,
        "/evolution evaluation-final "
        f"{contract.contract_id} {interventional_lane.comparison_id} "
        f"{adversarial_lane.comparison_id}",
    )
    assert tool_output == render_final_evaluation_receipt(receipt)
    assert receipt.receipt_id in slash_output
    assert "不是 Candidate 接受或发布决定" in slash_output

    with pytest.raises(EvolutionFinalEvaluationReceiptError) as missing:
        await executor.execute(
            aggregation_contract_id=contract.contract_id,
            interventional_comparison_id=interventional_lane.comparison_id,
            adversarial_comparison_ids=(),
        )
    assert missing.value.code == "final_evaluation_platform_coverage_incomplete"

    tampered = receipt.model_dump(mode="json")
    tampered["promotion_ready"] = True
    with pytest.raises(ValueError):
        EvolutionFinalEvaluationReceipt.model_validate(tampered)
    with sqlite3.connect(tmp_path / "evolution.db") as db:
        db.execute(
            "UPDATE evolution_final_evaluation_receipts SET receipt_sha256 = ? "
            "WHERE aggregation_contract_id = ?",
            ("0" * 64, contract.contract_id),
        )
        db.commit()
    with pytest.raises(EvolutionFinalEvaluationReceiptError) as corrupt:
        await final_store.get(contract.contract_id)
    assert corrupt.value.code == "final_evaluation_store_corrupt"
