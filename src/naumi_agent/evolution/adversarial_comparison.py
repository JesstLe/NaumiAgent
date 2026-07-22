"""Authority-bound HAR-08 comparison for adversarial RED/GREEN cohorts."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from naumi_agent.evolution.adversarial_batch_requests import (
    EvolutionAdversarialBatchRequest,
)
from naumi_agent.evolution.adversarial_cohort import (
    EvolutionAdversarialCohortReceipt,
)
from naumi_agent.evolution.adversarial_probe_contracts import (
    EvolutionAdversarialProbeContract,
)
from naumi_agent.evolution.adversarial_samples import (
    ADVERSARIAL_SAMPLE_RUNNER,
    build_adversarial_configuration,
)
from naumi_agent.evolution.comparison_kernel import (
    EvolutionComparisonKernel,
    EvolutionComparisonKernelError,
)
from naumi_agent.evolution.validation_plans import EvolutionValidationPlan
from naumi_agent.harness.eval_models import EvalCaseStatus
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredEvalComparisonReceipt,
    HarnessStoreError,
)


class EvolutionAdversarialComparisonError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionAdversarialComparisonExecutor:
    """Validate one same-platform lane pair and persist native H5c evidence."""

    def __init__(self, store: HarnessStore) -> None:
        if not isinstance(store, HarnessStore):
            raise TypeError("Adversarial Comparison executor 需要 HarnessStore。")
        self._store = store
        self._comparison_kernel = EvolutionComparisonKernel(store)

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        batch_request: EvolutionAdversarialBatchRequest,
        probe_contract: EvolutionAdversarialProbeContract,
        validation_plan: EvolutionValidationPlan,
        red_receipt: EvolutionAdversarialCohortReceipt,
        green_receipt: EvolutionAdversarialCohortReceipt,
    ) -> HarnessStoredEvalComparisonReceipt:
        try:
            workspace = Path(workspace_root).expanduser().resolve(strict=True)
            request = EvolutionAdversarialBatchRequest.model_validate(
                batch_request.model_dump(mode="json")
            )
            probes = EvolutionAdversarialProbeContract.model_validate(
                probe_contract.model_dump(mode="json")
            )
            plan = EvolutionValidationPlan.model_validate(
                validation_plan.model_dump(mode="json")
            )
            red = EvolutionAdversarialCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            green = EvolutionAdversarialCohortReceipt.model_validate(
                green_receipt.model_dump(mode="json")
            )
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise EvolutionAdversarialComparisonError(
                "adversarial_comparison_authority_invalid",
                "Adversarial Comparison authority 无效或已被篡改。",
            ) from exc
        _require_authority(request, probes, plan, red, green)
        try:
            red_records = await self._store.list_eval_results(
                workspace, red.batch_id, request.suite_id,
                limit=request.requested_samples + 1,
            )
            green_records = await self._store.list_eval_results(
                workspace, green.batch_id, request.suite_id,
                limit=request.requested_samples + 1,
            )
            _require_stored_cohort(red_records, red, request, phase="red")
            _require_stored_cohort(green_records, green, request, phase="green")
            _require_comparable_records(red_records, green_records, request, red, green)
        except EvolutionAdversarialComparisonError:
            raise
        except (HarnessStoreError, ValueError) as exc:
            raise EvolutionAdversarialComparisonError(
                "adversarial_comparison_evidence_unavailable",
                "Adversarial Comparison 无法读取可信 H5a evidence。",
            ) from exc
        try:
            return await self._comparison_kernel.execute(
                workspace_root=workspace,
                suite_id=request.suite_id,
                red_batch_id=red.batch_id,
                green_batch_id=green.batch_id,
                red_completed_at=red.completed_at,
                green_completed_at=green.completed_at,
                validation_plan_id=plan.validation_plan_id,
                lane_label="Adversarial",
                red_records=red_records,
                green_records=green_records,
            )
        except EvolutionComparisonKernelError as exc:
            raise EvolutionAdversarialComparisonError(exc.code, str(exc)) from exc


def _require_authority(request, probes, plan, red, green) -> None:
    try:
        red_lane = request.lanes[red.lane_order - 1]
        green_lane = request.lanes[green.lane_order - 1]
    except IndexError as exc:
        raise EvolutionAdversarialComparisonError(
            "adversarial_comparison_lane_invalid",
            "Adversarial Comparison lane 不在 Batch Request 中。",
        ) from exc
    shared = (
        request.probe_contract_id == probes.probe_contract_id
        and request.probe_contract_sha256 == probes.probe_contract_sha256
        and request.validation_plan_id == plan.validation_plan_id
        and request.validation_plan_sha256 == plan.validation_plan_sha256
        and request.profile_binding_id == probes.profile_binding_id
        and request.profile_binding_sha256 == probes.profile_binding_sha256
        and request.profile_sha256 == probes.profile_sha256 == plan.profile_sha256
        and request.candidate_id == probes.candidate_id == plan.candidate_id
        and request.candidate_revision == probes.candidate_revision == plan.candidate_revision
        and request.candidate_files_sha256
        == probes.candidate_files_sha256
        == plan.candidate_files_sha256
        and probes.coverage_complete
        and not probes.blockers
    )
    receipt_fields = (
        "request_id", "request_sha256", "probe_contract_id",
        "probe_contract_sha256", "validation_plan_id", "validation_plan_sha256",
        "lease_id", "candidate_id", "candidate_revision", "candidate_files_sha256",
        "suite_id", "requested_samples", "sample_seeds",
    )
    receipts_match = all(
        getattr(red, field) == getattr(green, field) for field in receipt_fields
    ) and all(
        getattr(red, field) == getattr(request, field)
        for field in receipt_fields
        if hasattr(request, field)
    )
    if not (
        shared and receipts_match
        and red_lane.order == red.lane_order and green_lane.order == green.lane_order
        and red_lane.batch_id == red.batch_id and green_lane.batch_id == green.batch_id
        and red_lane.phase == red.phase == "red"
        and green_lane.phase == green.phase == "green"
        and red_lane.platform == red.platform == green_lane.platform == green.platform
        and red.persisted_samples == green.persisted_samples == request.requested_samples
        and red.overlay_source_sha256 is None
        and green.overlay_source_sha256 is not None
        and tuple(item.check_id for item in red.checks)
        == tuple(item.check_id for item in green.checks)
    ):
        raise EvolutionAdversarialComparisonError(
            "adversarial_comparison_authority_mismatch",
            "Adversarial RED、GREEN、Probe、Plan 与 Batch authority 不一致。",
        )


def _require_stored_cohort(records, receipt, request, *, phase: str) -> None:
    if not (
        len(records) == request.requested_samples
        and tuple(item.sample_index for item in records)
        == tuple(range(request.requested_samples))
        and tuple(item.result_sha256 for item in records)
        == receipt.sample_result_sha256
    ):
        raise EvolutionAdversarialComparisonError(
            f"adversarial_{phase}_cohort_evidence_mismatch",
            f"H5a Adversarial {phase.upper()} cohort 与 completion receipt 不一致。",
        )
    _require_summary(records, receipt, request, phase=phase)


def _require_comparable_records(red_records, green_records, request, red, green) -> None:
    expected_configuration = build_adversarial_configuration(request)
    red_identities = {item.result.baseline_identity for item in red_records}
    green_identities = {item.result.baseline_identity for item in green_records}
    if (
        None in red_identities
        or None in green_identities
        or len(red_identities) != 1
        or len(green_identities) != 1
    ):
        raise EvolutionAdversarialComparisonError(
            "adversarial_comparison_identity_unstable",
            "Adversarial RED/GREEN cohort 缺少统一 Identity。",
        )
    red_identity = next(iter(red_identities))
    green_identity = next(iter(green_identities))
    assert red_identity is not None and green_identity is not None
    if not (
        red_identity.configuration == green_identity.configuration == expected_configuration
        and red_identity.platform == green_identity.platform
        and red_identity.platform.system == red.platform == green.platform
        and red_identity.identity_sha256 == red.baseline_identity_sha256
        and green_identity.identity_sha256 == green.baseline_identity_sha256
        and red_identity.source.commit == green_identity.source.commit == request.baseline_commit
        and red_identity.source.tree_sha256 == f"sha256:{request.baseline_tree_sha256}"
        and red_identity.source.tree_sha256 == red.source_tree_sha256
        and not red_identity.source.dirty
        and green_identity.source.tree_sha256 == green.source_tree_sha256
        and green_identity.source.dirty
        and green.source_tree_sha256
        == f"sha256:{green.overlay_source_sha256}"
    ):
        raise EvolutionAdversarialComparisonError(
            "adversarial_comparison_identity_mismatch",
            "Adversarial RED/GREEN configuration、平台或 source identity 不可比较。",
        )


def _require_summary(records, receipt, request, *, phase: str) -> None:
    expected_ids = tuple(item.check_id for item in request.checks)
    expected_probes = {
        item.check_id: f"probe_kinds={','.join(item.probes)}"
        for item in request.checks
    }
    observed_grants: set[str] = set()
    for record in records:
        cases = record.result.cases
        evidence = tuple(_run_evidence(case.message) for case in cases)
        grants = {item[1] for item in evidence if item is not None}
        if not (
            tuple(case.case_id for case in cases) == expected_ids
            and all(case.runner == ADVERSARIAL_SAMPLE_RUNNER for case in cases)
            and all(item is not None for item in evidence)
            and len(grants) == 1
            and all(expected_probes[case.case_id] in case.message for case in cases)
        ):
            raise EvolutionAdversarialComparisonError(
                f"adversarial_{phase}_case_evidence_mismatch",
                f"Adversarial {phase.upper()} probe/lifecycle/Run Grant evidence 不完整。",
            )
        observed_grants.update(grants)
    summaries = []
    for check_id in expected_ids:
        cases = tuple(
            case for record in records for case in record.result.cases
            if case.case_id == check_id and case.runner == ADVERSARIAL_SAMPLE_RUNNER
        )
        statuses = Counter(case.status for case in cases)
        values = tuple(_case_value(case, check_id, phase) for case in cases)
        summaries.append((
            check_id,
            statuses[EvalCaseStatus.PASSED],
            statuses[EvalCaseStatus.IMPLEMENTATION_FAILURE],
            statuses[EvalCaseStatus.EVALUATION_ERROR],
            values,
        ))
    actual = tuple(
        (item.check_id, item.passed, item.failed, item.evaluation_errors, item.sample_values)
        for item in receipt.checks
    )
    if (
        tuple(summaries) != actual
        or tuple(sorted(observed_grants)) != receipt.run_grant_sha256
    ):
        raise EvolutionAdversarialComparisonError(
            f"adversarial_{phase}_summary_mismatch",
            f"Adversarial {phase.upper()} summary/check/lifecycle evidence 与 H5a 不一致。",
        )


def _run_evidence(message: str) -> tuple[str, str] | None:
    lifecycle = re.search(r"(?:^| )lifecycle_sha256=([0-9a-f]{64})(?:$| )", message)
    grant = re.search(r"(?:^| )run_grant_sha256=([0-9a-f]{64})(?:$| )", message)
    return (lifecycle.group(1), grant.group(1)) if lifecycle and grant else None


def _case_value(case, check_id: str, phase: str) -> float | None:
    metric = f"adversarial.{check_id}.exit_zero"
    if case.status is EvalCaseStatus.EVALUATION_ERROR:
        if case.primary_metric or case.metric_observations:
            raise EvolutionAdversarialComparisonError(
                f"adversarial_{phase}_case_evidence_mismatch",
                f"Adversarial {phase.upper()} evaluation error 伪造了 metric。",
            )
        return None
    if len(case.metric_observations) != 1:
        raise EvolutionAdversarialComparisonError(
            f"adversarial_{phase}_case_evidence_mismatch",
            f"Adversarial {phase.upper()} case metric evidence 不完整。",
        )
    observation = case.metric_observations[0]
    expected = 1.0 if case.status is EvalCaseStatus.PASSED else 0.0
    if not (
        case.primary_metric == metric
        and observation.metric == metric
        and observation.value == expected
        and observation.unit == "scalar"
        and observation.direction == "increase"
        and observation.target == 1.0
        and observation.primary
    ):
        raise EvolutionAdversarialComparisonError(
            f"adversarial_{phase}_case_evidence_mismatch",
            f"Adversarial {phase.upper()} case mechanical evidence 不一致。",
        )
    return observation.value


__all__ = [
    "EvolutionAdversarialComparisonError",
    "EvolutionAdversarialComparisonExecutor",
]
