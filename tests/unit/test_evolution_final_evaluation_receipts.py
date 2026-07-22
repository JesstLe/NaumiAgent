from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
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
from naumi_agent.evolution.counterfactual_evidence import (
    EvolutionCounterfactualEvidence,
    EvolutionCounterfactualEvidenceError,
    EvolutionCounterfactualEvidenceExecutor,
    EvolutionCounterfactualEvidenceStore,
    render_counterfactual_evidence,
)
from naumi_agent.evolution.decision_inputs import (
    EvolutionDecisionInput,
    EvolutionDecisionInputBuilder,
    EvolutionDecisionInputError,
    EvolutionDecisionInputExecutor,
    EvolutionDecisionInputStore,
    render_decision_input,
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
from naumi_agent.evolution.experiment_leases import EvolutionExperimentLeaseStore
from naumi_agent.evolution.experiments import EvolutionExperimentContractStore
from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionAuthority,
    EvolutionFailureAttributionKernel,
)
from naumi_agent.evolution.final_evaluation_receipts import (
    EvolutionFinalAdversarialLaneEvidence,
    EvolutionFinalEvaluationReceipt,
    EvolutionFinalEvaluationReceiptBuilder,
    EvolutionFinalEvaluationReceiptError,
    EvolutionFinalEvaluationReceiptExecutor,
    EvolutionFinalEvaluationReceiptStore,
    render_final_evaluation_receipt,
)
from naumi_agent.evolution.independent_reviews import (
    EvolutionIndependentReviewBuilder,
    EvolutionIndependentReviewError,
    EvolutionIndependentReviewExecutor,
    EvolutionIndependentReviewStore,
    IndependentReviewerBudget,
    IndependentReviewerIdentity,
    IndependentReviewOpinion,
    IndependentReviewRecommendation,
    IndependentReviewStatus,
    render_independent_review,
)
from naumi_agent.evolution.mechanical_gates import (
    EvolutionMechanicalGate,
    EvolutionMechanicalGateError,
    EvolutionMechanicalGateExecutor,
    EvolutionMechanicalGateStore,
    MechanicalGateRequiredAction,
    MechanicalGateRule,
    render_mechanical_gate,
)
from naumi_agent.evolution.mutation_author_receipts import (
    EvolutionMutationAuthorReceiptBuilder,
    EvolutionMutationAuthorReceiptStore,
)
from naumi_agent.evolution.mutation_generation import (
    EvolutionMutationGenerationTraceStore,
)
from naumi_agent.evolution.mutation_receipts import EvolutionMutationReceiptStore
from naumi_agent.evolution.store import EvolutionCandidateStore
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
from naumi_agent.model.router import (
    ModelCapabilityContract,
    ModelContractStatus,
    ModelResponse,
    ModelRuntimeIdentity,
    TokenUsage,
)
from naumi_agent.tools.evolution_review import (
    EvolutionCounterfactualEvidenceTool,
    EvolutionDecisionInputTool,
    EvolutionFinalEvaluationReceiptTool,
    EvolutionIndependentReviewTool,
    EvolutionMechanicalGateTool,
)
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


class _ScriptedReviewerModel:
    def __init__(
        self,
        responses: list[ModelResponse],
        *,
        canonical_model: str = "reviewer/judge",
        provider: str = "reviewer",
        delay_seconds: float = 0.0,
        supports_structured_output: bool = True,
        max_output: int = 8_192,
    ) -> None:
        self.responses = list(responses)
        self.canonical_model = canonical_model
        self.provider = provider
        self.delay_seconds = delay_seconds
        self.supports_structured_output = supports_structured_output
        self.max_output = max_output
        self.calls: list[dict[str, object]] = []

    def resolve_model(self, _tier) -> str:
        return self.canonical_model

    def get_runtime_identity(self, model: str) -> ModelRuntimeIdentity:
        return ModelRuntimeIdentity(
            requested_model=model,
            canonical_model=self.canonical_model,
            upstream_model=self.canonical_model.partition("/")[2] or self.canonical_model,
            provider=self.provider,
            api_format="test-json",
            source="unit-test",
        )

    def get_model_capability_contract(self, model: str) -> ModelCapabilityContract:
        return ModelCapabilityContract(
            requested_model=model,
            canonical_model=self.canonical_model,
            upstream_model=self.canonical_model.partition("/")[2] or self.canonical_model,
            provider=self.provider,
            api_format="test-json",
            max_context=124_000,
            max_output=self.max_output,
            request_max_tokens=8_192,
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            supports_tools=True,
            supports_streaming=True,
            supports_parallel_tools=True,
            supports_structured_output=self.supports_structured_output,
            supports_reasoning=True,
            supports_vision=False,
            input_modalities=("text",),
            output_modalities=("text",),
            field_sources={},
            status=ModelContractStatus.VERIFIED,
        )

    async def call(self, messages, **kwargs) -> ModelResponse:
        self.calls.append({
            "messages": json.loads(json.dumps(messages)),
            "model": kwargs.get("model"),
            "tier": kwargs.get("tier"),
            "max_tokens": kwargs.get("max_tokens"),
            "temperature": kwargs.get("temperature"),
            "response_format": kwargs.get("response_format"),
            "thinking": kwargs.get("thinking"),
        })
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if not self.responses:
            raise RuntimeError("reviewer script exhausted")
        return self.responses.pop(0)


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
    candidate_status: EvalCaseStatus = EvalCaseStatus.PASSED,
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
            status=candidate_status,
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
    workspace, experiment, lease, plan, probes, _ = await _adversarial_probe_fixture(
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
    assert await final_store.get_by_receipt_id(receipt.receipt_id) == receipt

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

    decision_store = EvolutionDecisionInputStore(tmp_path / "evolution.db")
    decision_executor = EvolutionDecisionInputExecutor(
        candidate_store=EvolutionCandidateStore(tmp_path / "evolution.db"),
        mutation_store=EvolutionMutationReceiptStore(tmp_path / "runtime.db"),
        experiment_store=EvolutionExperimentContractStore(tmp_path / "runtime.db"),
        final_store=final_store,
        decision_store=decision_store,
    )
    decisions = await asyncio.gather(*(
        decision_executor.execute(
            workspace_root=workspace,
            final_evaluation_receipt_id=receipt.receipt_id,
        )
        for _ in range(4)
    ))
    decision = decisions[0]
    assert all(item == decision for item in decisions)
    assert decision.decision_input_id == f"evdin_{decision.decision_input_sha256[:24]}"
    assert decision.candidate_id == receipt.candidate_id
    assert decision.candidate_sha256 == experiment.source.candidate_sha256
    assert decision.mutation_receipt_id == request.mutation_receipt_id
    assert decision.experiment_contract_id == experiment.contract_id
    assert decision.constraints.allowed_files == experiment.scope.allowed_files
    assert decision.constraints.required_metrics == tuple(
        item.metric_name for item in experiment.allowed_checks
    )
    assert decision.decision_input_complete is True
    assert decision.mechanical_gate_ready is True
    assert decision.mechanical_gate_decided is False
    assert decision.candidate_acceptance_decided is False
    assert decision.promotion_ready is False
    assert await decision_store.get(decision.decision_input_id) == decision
    assert (
        await decision_store.get_by_final_receipt(receipt.receipt_id) == decision
    )

    decision_engine = SimpleNamespace(
        workspace_root=workspace,
        evolution_decision_input_executor=decision_executor,
    )
    tool_output = await EvolutionDecisionInputTool(decision_engine).execute(
        receipt.receipt_id
    )
    slash_output = await execute_slash_command(
        decision_engine,
        f"/evolution decision-input {receipt.receipt_id}",
    )
    assert tool_output == render_decision_input(decision)
    assert decision.decision_input_id in slash_output
    assert "尚未执行 mechanical gate" in slash_output

    gate_store = EvolutionMechanicalGateStore(tmp_path / "evolution.db")
    gate_executor = EvolutionMechanicalGateExecutor(
        decision_store=decision_store,
        trace_store=EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db"),
        gate_store=gate_store,
    )
    gates = await asyncio.gather(*(
        gate_executor.execute(
            workspace_root=workspace,
            decision_input_id=decision.decision_input_id,
        )
        for _ in range(4)
    ))
    gate = gates[0]
    assert all(item == gate for item in gates)
    assert gate.gate_id == f"evgate_{gate.gate_sha256[:24]}"
    assert gate.outcome == "pass"
    assert gate.mechanical_gate_decided is True
    assert gate.mechanical_gate_passed is True
    assert gate.mechanical_veto_applied is False
    assert gate.independent_review_ready is True
    assert gate.llm_override_allowed is False
    assert gate.candidate_acceptance_decided is False
    assert gate.promotion_ready is False
    assert gate.veto_codes == ()
    assert gate.required_actions == (
        MechanicalGateRequiredAction.CONTINUE_TO_INDEPENDENT_REVIEW,
    )
    assert all(check.passed for check in gate.checks)
    assert gate.observed_tool_calls == 1
    assert await gate_store.get(gate.gate_id) == gate
    assert await gate_store.get_by_decision_input(decision.decision_input_id) == gate

    gate_engine = SimpleNamespace(
        workspace_root=workspace,
        evolution_mechanical_gate_executor=gate_executor,
    )
    tool_output = await EvolutionMechanicalGateTool(gate_engine).execute(
        decision.decision_input_id
    )
    slash_output = await execute_slash_command(
        gate_engine,
        f"/evolution mechanical-gate {decision.decision_input_id}",
    )
    assert tool_output == render_mechanical_gate(gate)
    assert gate.gate_id in slash_output
    assert "仍不是 Candidate 接受或发布决定" in slash_output

    trace_store = EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db")
    trace = trace_store.get(gate.mutation_trace_id)
    assert trace is not None
    author_builder = EvolutionMutationAuthorReceiptBuilder()
    author_response = ModelResponse(
        content="",
        tool_calls=[{} for _ in range(trace.total_tool_calls)],
        usage=TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30),
        model="author/writer",
        finish_reason="tool_calls",
    )
    author_call = author_builder.build_call_fact(
        order=1,
        input_messages=(
            {"role": "system", "content": "test author prompt"},
            {"role": "user", "content": "test author authority"},
        ),
        response=author_response,
        response_model="author/writer",
    )
    author = author_builder.build(
        trace=trace,
        identity=ModelRuntimeIdentity(
            requested_model="author/writer",
            canonical_model="author/writer",
            upstream_model="writer",
            provider="author",
            api_format="test-json",
            source="unit-test",
        ),
        initial_messages=(
            {"role": "system", "content": "test author prompt"},
            {"role": "user", "content": "test author authority"},
        ),
        tool_schemas=({"type": "function", "name": "virtual_write"},),
        model_calls=(author_call,),
    )
    author_store = EvolutionMutationAuthorReceiptStore(tmp_path / "runtime.db")
    assert author_store.put(author) == author

    opinion_payload = {
        "schema_version": 1,
        "summary": "机械证据完整，建议进入反事实检查。",
        "strengths": ["全部固定门禁规则通过。"],
        "concerns": ["仍需确认是否存在更小改动。"],
        "evidence_refs": [gate.gate_id],
        "recommendation": "continue_to_counterfactual",
        "confidence": "high",
        "mechanical_gate_override_requested": False,
        "candidate_acceptance_decided": False,
    }
    reviewer_response = ModelResponse(
        content=json.dumps(opinion_payload, ensure_ascii=False),
        usage=TokenUsage(
            input_tokens=200,
            output_tokens=80,
            total_tokens=280,
            cost_usd=0.002,
        ),
        model="reviewer/judge",
        finish_reason="stop",
    )
    reviewer_model = _ScriptedReviewerModel(
        [reviewer_response],
        delay_seconds=0.05,
    )
    review_store = EvolutionIndependentReviewStore(tmp_path / "review.db")
    review_executor = EvolutionIndependentReviewExecutor(
        model_port=reviewer_model,  # type: ignore[arg-type]
        gate_store=gate_store,
        author_store=author_store,
        review_store=review_store,
        clock=lambda: datetime(2026, 7, 22, 3, 0, tzinfo=UTC),
    )
    reviews = await asyncio.gather(*(
        review_executor.execute(
            workspace_root=workspace,
            gate_id=gate.gate_id,
        )
        for _ in range(4)
    ))
    review = reviews[0]
    assert all(item == review for item in reviews)
    assert len(reviewer_model.calls) == 1
    assert review.review_id == f"evreview_{review.review_sha256[:24]}"
    assert review.status is IndependentReviewStatus.COMPLETED
    assert review.gate_outcome == "pass"
    assert review.gate_outcome_preserved is True
    assert review.model_called is True
    assert review.reviewer_author_isolated is True
    assert review.reviewer is not None
    assert review.reviewer.canonical_model == "reviewer/judge"
    assert review.author_canonical_model == "author/writer"
    assert review.opinion is not None
    assert review.opinion.recommendation is (
        IndependentReviewRecommendation.CONTINUE_TO_COUNTERFACTUAL
    )
    assert review.llm_override_allowed is False
    assert review.candidate_acceptance_decided is False
    assert review.counterfactual_review_ready is True
    assert review.promotion_ready is False
    assert await review_store.get(review.review_id) == review
    assert await review_store.get_by_gate(gate.gate_id) == review
    call = reviewer_model.calls[0]
    assert call["response_format"] == "json"
    assert call["thinking"] == {"type": "disabled"}
    assert call["temperature"] == 0.0
    serialized_review = review.model_dump_json()
    assert "test author prompt" not in serialized_review
    assert "test author authority" not in serialized_review

    with pytest.raises(ValueError):
        IndependentReviewerIdentity(
            requested_model="reviewer/judge\nforged",
            canonical_model="reviewer/judge",
            upstream_model="judge",
            provider="reviewer",
            api_format="test-json",
            identity_source="unit-test",
        )
    with pytest.raises(ValueError):
        await review_store.acquire(
            gate_id=gate.gate_id,
            owner_token="0" * 32,
            now=datetime(2026, 7, 22, 3, 0, tzinfo=UTC),
            lease_seconds=float("inf"),
        )
    assert review.opinion is not None and review.reviewer is not None
    mismatched_opinion = review.opinion.model_copy(
        update={"summary": "这不是模型原始响应中的意见。"}
    )
    with pytest.raises(EvolutionIndependentReviewError) as response_mismatch:
        EvolutionIndependentReviewBuilder().build_completed(
            gate=gate,
            author_receipt=author,
            reviewer=review.reviewer,
            messages=call["messages"],
            response=reviewer_response,
            opinion=mismatched_opinion,
            reviewed_at="2026-07-22T03:00:00+00:00",
        )
    assert response_mismatch.value.code == "independent_review_response_mismatch"
    with pytest.raises(EvolutionIndependentReviewError) as invalid_prompt:
        EvolutionIndependentReviewBuilder().build_completed(
            gate=gate,
            author_receipt=author,
            reviewer=review.reviewer,
            messages=({"role": "system", "content": "missing authority"},),
            response=reviewer_response,
            opinion=IndependentReviewOpinion.model_validate(opinion_payload),
            reviewed_at="2026-07-22T03:00:00+00:00",
        )
    assert invalid_prompt.value.code == "independent_review_prompt_invalid"

    review_engine = SimpleNamespace(
        workspace_root=workspace,
        evolution_independent_review_executor=review_executor,
    )
    tool_output = await EvolutionIndependentReviewTool(review_engine).execute(
        gate.gate_id
    )
    slash_output = await execute_slash_command(
        review_engine,
        f"/evolution independent-review {gate.gate_id}",
    )
    assert tool_output == render_independent_review(review)
    assert review.review_id in slash_output
    assert "不接受 Candidate" in slash_output
    assert len(reviewer_model.calls) == 1

    counterfactual_store = EvolutionCounterfactualEvidenceStore(
        tmp_path / "counterfactual.db"
    )
    counterfactual_executor = EvolutionCounterfactualEvidenceExecutor(
        review_store=review_store,
        gate_store=gate_store,
        decision_store=decision_store,
        mutation_store=EvolutionMutationReceiptStore(tmp_path / "runtime.db"),
        experiment_store=EvolutionExperimentContractStore(tmp_path / "runtime.db"),
        lease_store=EvolutionExperimentLeaseStore(tmp_path / "runtime.db"),
        evidence_store=counterfactual_store,
        worktree_storage_dir=Path(lease.worktree_path).parent,
    )
    counterfactuals = await asyncio.gather(*(
        counterfactual_executor.execute(
            workspace_root=workspace,
            review_id=review.review_id,
        )
        for _ in range(4)
    ))
    counterfactual = counterfactuals[0]
    assert all(item == counterfactual for item in counterfactuals)
    assert counterfactual.evidence_id == (
        f"evcounter_{counterfactual.evidence_sha256[:24]}"
    )
    assert counterfactual.review == review
    assert counterfactual.outcome == "clear"
    assert counterfactual.findings == ()
    assert counterfactual.smaller_scope_found is False
    assert counterfactual.alternative_explanation_found is False
    assert counterfactual.mechanical_gate_outcome_preserved is True
    assert counterfactual.reviewer_advisory_only is True
    assert counterfactual.llm_used is False
    assert counterfactual.candidate_acceptance_decided is False
    assert counterfactual.reward_hacking_review_ready is True
    assert counterfactual.promotion_ready is False
    assert all(check.passed for check in counterfactual.checks)
    assert await counterfactual_store.get(counterfactual.evidence_id) == counterfactual
    assert (
        await counterfactual_store.get_by_review(review.review_id) == counterfactual
    )
    serialized_counterfactual = counterfactual.model_dump_json()
    assert lease.worktree_path not in serialized_counterfactual
    assert "test author prompt" not in serialized_counterfactual

    counterfactual_engine = SimpleNamespace(
        workspace_root=workspace,
        evolution_counterfactual_evidence_executor=counterfactual_executor,
    )
    tool_output = await EvolutionCounterfactualEvidenceTool(
        counterfactual_engine
    ).execute(review.review_id)
    slash_output = await execute_slash_command(
        counterfactual_engine,
        f"/evolution counterfactual {review.review_id}",
    )
    assert tool_output == render_counterfactual_evidence(counterfactual)
    assert counterfactual.evidence_id in slash_output
    assert "不接受 Candidate" in slash_output

    tampered_counterfactual = counterfactual.model_dump(mode="json")
    tampered_counterfactual["candidate_acceptance_decided"] = True
    with pytest.raises(ValueError):
        EvolutionCounterfactualEvidence.model_validate(tampered_counterfactual)

    same_author_model = _ScriptedReviewerModel(
        [],
        canonical_model="author/writer",
        provider="author",
    )
    identity_conflict_executor = EvolutionIndependentReviewExecutor(
        model_port=same_author_model,  # type: ignore[arg-type]
        gate_store=gate_store,
        author_store=author_store,
        review_store=EvolutionIndependentReviewStore(
            tmp_path / "identity-conflict-review.db"
        ),
    )
    with pytest.raises(EvolutionIndependentReviewError) as identity_conflict:
        await identity_conflict_executor.execute(
            workspace_root=workspace,
            gate_id=gate.gate_id,
        )
    assert identity_conflict.value.code == "independent_review_identity_conflict"
    assert same_author_model.calls == []

    unverified_model = _ScriptedReviewerModel(
        [],
        supports_structured_output=False,
    )
    unverified_executor = EvolutionIndependentReviewExecutor(
        model_port=unverified_model,  # type: ignore[arg-type]
        gate_store=gate_store,
        author_store=author_store,
        review_store=EvolutionIndependentReviewStore(
            tmp_path / "unverified-review.db"
        ),
    )
    with pytest.raises(EvolutionIndependentReviewError) as unverified:
        await unverified_executor.execute(
            workspace_root=workspace,
            gate_id=gate.gate_id,
        )
    assert unverified.value.code == (
        "independent_review_model_capability_unverified"
    )
    assert unverified_model.calls == []

    undersized_output_model = _ScriptedReviewerModel([], max_output=256)
    undersized_output_executor = EvolutionIndependentReviewExecutor(
        model_port=undersized_output_model,  # type: ignore[arg-type]
        gate_store=gate_store,
        author_store=author_store,
        review_store=EvolutionIndependentReviewStore(
            tmp_path / "undersized-output-review.db"
        ),
    )
    with pytest.raises(EvolutionIndependentReviewError) as undersized_output:
        await undersized_output_executor.execute(
            workspace_root=workspace,
            gate_id=gate.gate_id,
        )
    assert undersized_output.value.code == (
        "independent_review_model_capability_unverified"
    )
    assert undersized_output_model.calls == []

    timeout_store = EvolutionIndependentReviewStore(tmp_path / "timeout-review.db")
    slow_model = _ScriptedReviewerModel(
        [reviewer_response],
        delay_seconds=2.0,
    )
    timeout_executor = EvolutionIndependentReviewExecutor(
        model_port=slow_model,  # type: ignore[arg-type]
        gate_store=gate_store,
        author_store=author_store,
        review_store=timeout_store,
    )
    with pytest.raises(EvolutionIndependentReviewError) as timed_out:
        await timeout_executor.execute(
            workspace_root=workspace,
            gate_id=gate.gate_id,
            budget=IndependentReviewerBudget(timeout_seconds=1),
        )
    assert timed_out.value.code == "independent_review_timeout"
    assert len(slow_model.calls) == 1
    recovered_timeout = await EvolutionIndependentReviewExecutor(
        model_port=_ScriptedReviewerModel([reviewer_response]),  # type: ignore[arg-type]
        gate_store=gate_store,
        author_store=author_store,
        review_store=timeout_store,
    ).execute(
        workspace_root=workspace,
        gate_id=gate.gate_id,
    )
    assert recovered_timeout.status is IndependentReviewStatus.COMPLETED

    retry_model = _ScriptedReviewerModel([
        ModelResponse(content="{}", model="reviewer/judge"),
        reviewer_response,
    ])
    retry_store = EvolutionIndependentReviewStore(tmp_path / "retry-review.db")
    retry_executor = EvolutionIndependentReviewExecutor(
        model_port=retry_model,  # type: ignore[arg-type]
        gate_store=gate_store,
        author_store=author_store,
        review_store=retry_store,
        clock=lambda: datetime(2026, 7, 22, 3, 1, tzinfo=UTC),
    )
    with pytest.raises(EvolutionIndependentReviewError) as invalid_json:
        await retry_executor.execute(
            workspace_root=workspace,
            gate_id=gate.gate_id,
        )
    assert invalid_json.value.code == "independent_review_response_invalid"
    retried_review = await retry_executor.execute(
        workspace_root=workspace,
        gate_id=gate.gate_id,
    )
    assert retried_review.status is IndependentReviewStatus.COMPLETED
    assert len(retry_model.calls) == 2

    missing_author_executor = EvolutionIndependentReviewExecutor(
        model_port=_ScriptedReviewerModel([]),  # type: ignore[arg-type]
        gate_store=gate_store,
        author_store=EvolutionMutationAuthorReceiptStore(
            tmp_path / "missing-author.db"
        ),
        review_store=EvolutionIndependentReviewStore(
            tmp_path / "missing-author-review.db"
        ),
    )
    with pytest.raises(EvolutionIndependentReviewError) as missing_author:
        await missing_author_executor.execute(
            workspace_root=workspace,
            gate_id=gate.gate_id,
        )
    assert missing_author.value.code == "independent_review_author_missing"

    missing_trace_executor = EvolutionMechanicalGateExecutor(
        decision_store=decision_store,
        trace_store=EvolutionMutationGenerationTraceStore(
            tmp_path / "missing-trace.db"
        ),
        gate_store=EvolutionMechanicalGateStore(tmp_path / "missing-gate.db"),
    )
    with pytest.raises(EvolutionMechanicalGateError) as missing_trace:
        await missing_trace_executor.execute(
            workspace_root=workspace,
            decision_input_id=decision.decision_input_id,
        )
    assert missing_trace.value.code == "mechanical_gate_trace_missing"

    with pytest.raises(EvolutionMechanicalGateError) as missing_gate_input:
        await gate_executor.execute(
            workspace_root=workspace,
            decision_input_id=f"evdin_{'0' * 24}",
        )
    assert missing_gate_input.value.code == "mechanical_gate_input_missing"

    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    with pytest.raises(EvolutionMechanicalGateError) as gate_wrong_workspace:
        await gate_executor.execute(
            workspace_root=other_workspace,
            decision_input_id=decision.decision_input_id,
        )
    assert gate_wrong_workspace.value.code == "mechanical_gate_workspace_mismatch"

    tampered_gate = gate.model_dump(mode="json")
    tampered_gate["llm_override_allowed"] = True
    with pytest.raises(ValueError):
        EvolutionMechanicalGate.model_validate(tampered_gate)

    # Build a second, fully signed evaluation chain whose GREEN cohorts still fail.
    veto_harness = HarnessStore(tmp_path / "veto-harness.db")
    veto_adversarial = await _lane(
        store=veto_harness,
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
        candidate_status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
    )
    veto_red_records = await veto_harness.list_eval_results(
        workspace, red_lane.batch_id, request.suite_id
    )
    veto_green_records = await veto_harness.list_eval_results(
        workspace, green_lane.batch_id, request.suite_id
    )
    veto_red_completion = completion(red_lane, "red", veto_red_records)
    veto_green_completion = completion(green_lane, "green", veto_green_records)
    veto_authority = EvolutionFailureAttributionAuthority(
        validation_plan_id=plan.validation_plan_id,
        validation_plan_sha256=plan.validation_plan_sha256,
        red_receipt_id=veto_red_completion.receipt_id,
        red_receipt_sha256=veto_red_completion.receipt_sha256,
        green_receipt_id=veto_green_completion.receipt_id,
        green_receipt_sha256=veto_green_completion.receipt_sha256,
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
        suite_id=request.suite_id,
        red_batch_id=red_lane.batch_id,
        green_batch_id=green_lane.batch_id,
        red_samples=5,
        green_samples=5,
        red_result_sha256=tuple(item.result_sha256 for item in veto_red_records),
        green_result_sha256=tuple(item.result_sha256 for item in veto_green_records),
    )
    veto_comparison = await veto_harness.get_eval_comparison_receipt_by_id(
        workspace, veto_adversarial.comparison_id
    )
    assert veto_comparison is not None
    veto_attribution = EvolutionFailureAttributionKernel().build(
        authority=veto_authority,
        comparison=veto_comparison,
    )
    veto_adversarial = EvolutionEvaluationLaneReceiptBuilder().build(
        comparison=veto_comparison,
        attribution=veto_attribution,
        baseline_records=veto_red_records,
        candidate_records=veto_green_records,
    )
    veto_interventional = await _lane(
        store=veto_harness,
        workspace=workspace,
        suite_id="veto_interventional",
        suite_sha256="e" * 64,
        platform=platform,
        red_batch_id="veto:interventional:red",
        green_batch_id="veto:interventional:green",
        red_completion_id=f"evvredcohort_{'5' * 24}",
        red_completion_sha256="6" * 64,
        green_completion_id=f"evvgreencohort_{'7' * 24}",
        green_completion_sha256="8" * 64,
        validation_plan_id=plan.validation_plan_id,
        validation_plan_sha256=plan.validation_plan_sha256,
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
        candidate_status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
    )
    veto_final = EvolutionFinalEvaluationReceiptBuilder().build(
        contract=contract,
        interventional_lane=veto_interventional,
        adversarial_evidence=(EvolutionFinalAdversarialLaneEvidence(
            order=1,
            platform=platform.system,
            lane_receipt=veto_adversarial,
            red_completion=veto_red_completion,
            green_completion=veto_green_completion,
        ),),
    )
    veto_final_store = EvolutionFinalEvaluationReceiptStore(
        tmp_path / "veto-evolution.db"
    )
    await veto_final_store.record(veto_final)
    veto_decision_store = EvolutionDecisionInputStore(tmp_path / "veto-evolution.db")
    veto_decision = await EvolutionDecisionInputExecutor(
        candidate_store=EvolutionCandidateStore(tmp_path / "evolution.db"),
        mutation_store=EvolutionMutationReceiptStore(tmp_path / "runtime.db"),
        experiment_store=EvolutionExperimentContractStore(tmp_path / "runtime.db"),
        final_store=veto_final_store,
        decision_store=veto_decision_store,
    ).execute(
        workspace_root=workspace,
        final_evaluation_receipt_id=veto_final.receipt_id,
    )
    veto_gate = await EvolutionMechanicalGateExecutor(
        decision_store=veto_decision_store,
        trace_store=EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db"),
        gate_store=EvolutionMechanicalGateStore(tmp_path / "veto-evolution.db"),
    ).execute(
        workspace_root=workspace,
        decision_input_id=veto_decision.decision_input_id,
    )
    assert veto_gate.outcome == "veto"
    assert veto_gate.mechanical_gate_passed is False
    assert veto_gate.mechanical_veto_applied is True
    assert veto_gate.independent_review_ready is False
    assert veto_gate.llm_override_allowed is False
    assert MechanicalGateRule.ALL_LANES_REFLECTION_ELIGIBLE in veto_gate.veto_codes
    assert MechanicalGateRule.FAILURE_CATEGORIES_CLEAR in veto_gate.veto_codes
    assert MechanicalGateRule.FAILURE_ACTIONS_CONTINUE in veto_gate.veto_codes
    assert MechanicalGateRequiredAction.REVISE_CANDIDATE in veto_gate.required_actions

    veto_model = _ScriptedReviewerModel([])
    veto_review_store = EvolutionIndependentReviewStore(
        tmp_path / "veto-independent-review.db"
    )
    veto_review = await EvolutionIndependentReviewExecutor(
        model_port=veto_model,  # type: ignore[arg-type]
        gate_store=EvolutionMechanicalGateStore(tmp_path / "veto-evolution.db"),
        author_store=author_store,
        review_store=veto_review_store,
        clock=lambda: datetime(2026, 7, 22, 3, 2, tzinfo=UTC),
    ).execute(
        workspace_root=workspace,
        gate_id=veto_gate.gate_id,
    )
    assert veto_review.status is (
        IndependentReviewStatus.BLOCKED_BY_MECHANICAL_VETO
    )
    assert veto_review.gate_outcome == "veto"
    assert veto_review.model_called is False
    assert veto_review.reviewer is None
    assert veto_review.opinion is None
    assert veto_review.llm_override_allowed is False
    assert veto_review.counterfactual_review_ready is False
    assert veto_model.calls == []
    assert "未调用任何 Reviewer 模型" in render_independent_review(veto_review)
    with pytest.raises(EvolutionCounterfactualEvidenceError) as veto_counterfactual:
        await EvolutionCounterfactualEvidenceExecutor(
            review_store=veto_review_store,
            gate_store=EvolutionMechanicalGateStore(tmp_path / "veto-evolution.db"),
            decision_store=veto_decision_store,
            mutation_store=EvolutionMutationReceiptStore(tmp_path / "runtime.db"),
            experiment_store=EvolutionExperimentContractStore(tmp_path / "runtime.db"),
            lease_store=EvolutionExperimentLeaseStore(tmp_path / "runtime.db"),
            evidence_store=EvolutionCounterfactualEvidenceStore(
                tmp_path / "veto-counterfactual.db"
            ),
            worktree_storage_dir=Path(lease.worktree_path).parent,
        ).execute(
            workspace_root=workspace,
            review_id=veto_review.review_id,
        )
    assert veto_counterfactual.value.code == "counterfactual_review_not_ready"

    missing_executor = EvolutionDecisionInputExecutor(
        candidate_store=EvolutionCandidateStore(tmp_path / "evolution.db"),
        mutation_store=EvolutionMutationReceiptStore(tmp_path / "missing-runtime.db"),
        experiment_store=EvolutionExperimentContractStore(tmp_path / "runtime.db"),
        final_store=final_store,
        decision_store=EvolutionDecisionInputStore(tmp_path / "missing-decision.db"),
    )
    with pytest.raises(EvolutionDecisionInputError) as missing_authority:
        await missing_executor.execute(
            workspace_root=workspace,
            final_evaluation_receipt_id=receipt.receipt_id,
        )
    assert missing_authority.value.code == "decision_input_authority_missing"
    assert "Mutation Receipt" in str(missing_authority.value)

    with pytest.raises(EvolutionDecisionInputError) as wrong_workspace:
        await decision_executor.execute(
            workspace_root=other_workspace,
            final_evaluation_receipt_id=receipt.receipt_id,
        )
    assert wrong_workspace.value.code == "decision_input_workspace_mismatch"

    with pytest.raises(EvolutionDecisionInputError) as missing_final:
        await decision_executor.execute(
            workspace_root=workspace,
            final_evaluation_receipt_id=f"evfinal_{'0' * 24}",
        )
    assert missing_final.value.code == "decision_input_final_missing"

    stored_candidate = await EvolutionCandidateStore(
        tmp_path / "evolution.db"
    ).get_candidate(workspace, receipt.candidate_id)
    stored_mutation = EvolutionMutationReceiptStore(tmp_path / "runtime.db").get(
        request.mutation_receipt_id
    )
    stored_experiment = await EvolutionExperimentContractStore(
        tmp_path / "runtime.db"
    ).get(workspace, experiment.contract_id)
    assert stored_candidate is not None
    assert stored_mutation is not None
    assert stored_experiment is not None
    with pytest.raises(EvolutionDecisionInputError) as revision_drift:
        EvolutionDecisionInputBuilder().build(
            stored_candidate=replace(
                stored_candidate,
                revision=stored_candidate.revision + 1,
            ),
            mutation=stored_mutation,
            experiment=stored_experiment,
            final_evaluation=receipt,
        )
    assert revision_drift.value.code == "decision_input_authority_mismatch"

    tampered_decision = decision.model_dump(mode="json")
    tampered_decision["mechanical_gate_decided"] = True
    with pytest.raises(ValueError):
        EvolutionDecisionInput.model_validate(tampered_decision)

    with pytest.raises(EvolutionFinalEvaluationReceiptError) as missing:
        await executor.execute(
            aggregation_contract_id=contract.contract_id,
            interventional_comparison_id=interventional_lane.comparison_id,
            adversarial_comparison_ids=(),
        )
    assert missing.value.code == "final_evaluation_platform_coverage_incomplete"

    with sqlite3.connect(tmp_path / "evolution.db") as db:
        db.execute(
            "UPDATE evolution_mechanical_gates SET outcome = ? WHERE gate_id = ?",
            ("veto", gate.gate_id),
        )
        db.commit()
    with pytest.raises(EvolutionMechanicalGateError) as corrupt_gate:
        await gate_store.get(gate.gate_id)
    assert corrupt_gate.value.code == "mechanical_gate_store_corrupt"

    with sqlite3.connect(tmp_path / "review.db") as db:
        db.execute(
            "UPDATE evolution_independent_reviews SET review_status = ? "
            "WHERE review_id = ?",
            ("blocked_by_mechanical_veto", review.review_id),
        )
        db.commit()
    with pytest.raises(EvolutionIndependentReviewError) as corrupt_review:
        await review_store.get(review.review_id)
    assert corrupt_review.value.code == "independent_review_store_corrupt"

    with sqlite3.connect(tmp_path / "counterfactual.db") as db:
        db.execute(
            "UPDATE evolution_counterfactual_evidence SET outcome = ? "
            "WHERE evidence_id = ?",
            ("concern", counterfactual.evidence_id),
        )
        db.commit()
    with pytest.raises(EvolutionCounterfactualEvidenceError) as corrupt_counterfactual:
        await counterfactual_store.get(counterfactual.evidence_id)
    assert corrupt_counterfactual.value.code == "counterfactual_store_corrupt"

    with sqlite3.connect(tmp_path / "evolution.db") as db:
        db.execute(
            "UPDATE evolution_decision_inputs SET decision_input_sha256 = ? "
            "WHERE decision_input_id = ?",
            ("0" * 64, decision.decision_input_id),
        )
        db.commit()
    with pytest.raises(EvolutionDecisionInputError) as corrupt_decision:
        await decision_store.get(decision.decision_input_id)
    assert corrupt_decision.value.code == "decision_input_store_corrupt"

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
