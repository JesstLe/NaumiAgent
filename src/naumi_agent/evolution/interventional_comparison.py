"""Authority-bound HAR-08 comparison for interventional RED/GREEN cohorts."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal

from naumi_agent.evolution.comparison_kernel import (
    EvolutionComparisonKernel,
    EvolutionComparisonKernelError,
)
from naumi_agent.evolution.interventional_green_cohort import (
    EvolutionInterventionalGreenCohortReceipt,
)
from naumi_agent.evolution.interventional_green_request import (
    EvolutionInterventionalGreenCohortRequest,
)
from naumi_agent.evolution.interventional_red_cohort import (
    EvolutionInterventionalRedCohortReceipt,
)
from naumi_agent.evolution.interventional_red_sample import (
    INTERVENTIONAL_RED_CHECK_RUNNER,
    EvolutionInterventionalRedSampleError,
    build_interventional_configuration,
    validate_interventional_red_authority,
)
from naumi_agent.evolution.interventional_sample_kernel import (
    interventional_lifecycle_digest,
    interventional_run_grant_digest,
    interventional_run_scope,
)
from naumi_agent.evolution.self_review import SELF_REVIEW_STATIC_RUNNER_VERSION
from naumi_agent.evolution.validation_cohorts import EvolutionBaselineCohortRequest
from naumi_agent.evolution.validation_metric_bindings import EvolutionMetricRunnerBinding
from naumi_agent.evolution.validation_plans import (
    EvolutionValidationPlan,
    EvolutionValidationProfileBinding,
)
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredEvalComparisonReceipt,
    HarnessStoredEvalResult,
    HarnessStoreError,
)


class EvolutionInterventionalComparisonError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionInterventionalComparisonExecutor:
    """Persist native H5c evidence after validating both interventional cohorts."""

    def __init__(self, store: HarnessStore) -> None:
        if not isinstance(store, HarnessStore):
            raise TypeError("Interventional Comparison executor 需要 HarnessStore。")
        self._store = store
        self._comparison_kernel = EvolutionComparisonKernel(store)

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        baseline_request: EvolutionBaselineCohortRequest,
        metric_binding: EvolutionMetricRunnerBinding,
        validation_plan: EvolutionValidationPlan,
        profile_binding: EvolutionValidationProfileBinding,
        red_receipt: EvolutionInterventionalRedCohortReceipt,
        green_request: EvolutionInterventionalGreenCohortRequest,
        green_receipt: EvolutionInterventionalGreenCohortReceipt,
    ) -> HarnessStoredEvalComparisonReceipt:
        try:
            workspace = Path(workspace_root).expanduser().resolve(strict=True)
            request, binding, plan, profile = validate_interventional_red_authority(
                baseline_request,
                metric_binding,
                validation_plan,
                profile_binding,
            )
            red = EvolutionInterventionalRedCohortReceipt.model_validate(
                red_receipt.model_dump(mode="json")
            )
            green = EvolutionInterventionalGreenCohortRequest.model_validate(
                green_request.model_dump(mode="json")
            )
            green_run = EvolutionInterventionalGreenCohortReceipt.model_validate(
                green_receipt.model_dump(mode="json")
            )
        except (
            AttributeError,
            OSError,
            TypeError,
            ValueError,
            EvolutionInterventionalRedSampleError,
        ) as exc:
            raise EvolutionInterventionalComparisonError(
                "interventional_comparison_authority_invalid",
                "Interventional Comparison authority 无效或已被篡改。",
            ) from exc
        _require_authority(request, binding, plan, profile, red, green, green_run)
        try:
            red_records = await self._store.list_eval_results(
                workspace,
                request.batch_id,
                request.suite_id,
                limit=request.requested_samples + 1,
            )
            green_records = await self._store.list_eval_results(
                workspace,
                green.batch_id,
                green.suite_id,
                limit=green.requested_samples + 1,
            )
            _require_stored_cohort(
                red_records,
                expected_count=request.requested_samples,
                expected_digests=red.sample_result_sha256,
                phase="red",
            )
            _require_stored_cohort(
                green_records,
                expected_count=green.requested_samples,
                expected_digests=green_run.sample_result_sha256,
                phase="green",
            )
            _require_comparable_records(
                red_records,
                green_records,
                request=request,
                binding=binding,
                plan=plan,
                profile=profile,
                green=green_run,
            )
            _require_summary_evidence(
                red_records,
                red,
                request,
                binding,
                phase="red",
            )
            _require_summary_evidence(
                green_records,
                green_run,
                request,
                binding,
                phase="green",
            )
        except EvolutionInterventionalComparisonError:
            raise
        except (HarnessStoreError, ValueError) as exc:
            raise EvolutionInterventionalComparisonError(
                "interventional_comparison_evidence_unavailable",
                "Interventional Comparison 无法读取可信 H5a evidence。",
            ) from exc
        try:
            return await self._comparison_kernel.execute(
                workspace_root=workspace,
                suite_id=request.suite_id,
                red_batch_id=request.batch_id,
                green_batch_id=green.batch_id,
                red_completed_at=red.completed_at,
                green_completed_at=green_run.completed_at,
                validation_plan_id=plan.validation_plan_id,
                lane_label="Interventional",
                red_records=red_records,
                green_records=green_records,
            )
        except EvolutionComparisonKernelError as exc:
            raise EvolutionInterventionalComparisonError(exc.code, str(exc)) from exc


def _require_authority(request, binding, plan, profile, red, green, green_run) -> None:
    if not (
        red.baseline_request_id == request.request_id
        and red.baseline_request_sha256 == request.request_sha256
        and red.metric_binding_id == binding.binding_id
        and red.metric_binding_sha256 == binding.binding_sha256
        and red.validation_plan_id == plan.validation_plan_id
        and red.validation_plan_sha256 == plan.validation_plan_sha256
        and red.profile_binding_id == profile.binding_id
        and red.profile_binding_sha256 == profile.binding_sha256
        and red.suite_id == request.suite_id
        and red.batch_id == request.batch_id
        and red.baseline_commit == request.baseline_commit
        and red.baseline_tree_sha256 == request.baseline_tree_sha256
        and red.requested_samples == red.persisted_samples == request.requested_samples
        and red.sample_seeds == request.sample_seeds
        and green.baseline_request_id == request.request_id
        and green.baseline_request_sha256 == request.request_sha256
        and green.red_receipt_id == red.receipt_id
        and green.red_receipt_sha256 == red.receipt_sha256
        and green.metric_binding_id == binding.binding_id
        and green.metric_binding_sha256 == binding.binding_sha256
        and green.validation_plan_id == plan.validation_plan_id
        and green.validation_plan_sha256 == plan.validation_plan_sha256
        and green.profile_binding_id == profile.binding_id
        and green.profile_binding_sha256 == profile.binding_sha256
        and green.candidate_id == plan.candidate_id
        and green.candidate_revision == plan.candidate_revision
        and green.candidate_files_sha256 == plan.candidate_files_sha256
        and green.suite_id == request.suite_id
        and green.requested_samples == request.requested_samples
        and green.sample_seeds == request.sample_seeds
        and green_run.green_request_id == green.request_id
        and green_run.green_request_sha256 == green.request_sha256
        and green_run.red_receipt_id == red.receipt_id
        and green_run.red_receipt_sha256 == red.receipt_sha256
        and green_run.metric_binding_id == binding.binding_id
        and green_run.metric_binding_sha256 == binding.binding_sha256
        and green_run.validation_plan_id == plan.validation_plan_id
        and green_run.validation_plan_sha256 == plan.validation_plan_sha256
        and green_run.profile_binding_id == profile.binding_id
        and green_run.profile_binding_sha256 == profile.binding_sha256
        and green_run.lease_id == green.lease_id
        and green_run.candidate_id == plan.candidate_id
        and green_run.candidate_revision == plan.candidate_revision
        and green_run.candidate_files_sha256 == plan.candidate_files_sha256
        and green_run.suite_id == request.suite_id
        and green_run.batch_id == green.batch_id
        and green_run.requested_samples
        == green_run.persisted_samples
        == request.requested_samples
        and green_run.sample_seeds == request.sample_seeds
    ):
        raise EvolutionInterventionalComparisonError(
            "interventional_comparison_authority_mismatch",
            "Interventional RED、GREEN、Plan、Profile 与 Metric authority 不一致。",
        )


def _require_stored_cohort(
    records: tuple[HarnessStoredEvalResult, ...],
    *,
    expected_count: int,
    expected_digests: tuple[str, ...],
    phase: Literal["red", "green"],
) -> None:
    indexes = tuple(item.sample_index for item in records)
    if not (
        len(records) == expected_count
        and indexes == tuple(range(expected_count))
        and tuple(item.result_sha256 for item in records) == expected_digests
    ):
        raise EvolutionInterventionalComparisonError(
            f"interventional_{phase}_cohort_evidence_mismatch",
            f"H5a Interventional {phase.upper()} cohort 与 completion receipt 不一致。",
        )


def _require_comparable_records(
    red_records,
    green_records,
    *,
    request,
    binding,
    plan,
    profile,
    green,
) -> None:
    configuration = build_interventional_configuration(request, binding, plan, profile)
    red_identities = {item.result.baseline_identity for item in red_records}
    green_identities = {item.result.baseline_identity for item in green_records}
    if None in red_identities or None in green_identities:
        raise EvolutionInterventionalComparisonError(
            "interventional_comparison_identity_missing",
            "Interventional RED/GREEN cohort 缺少可比较 Identity。",
        )
    if len(red_identities) != 1 or len(green_identities) != 1:
        raise EvolutionInterventionalComparisonError(
            "interventional_comparison_identity_unstable",
            "Interventional RED/GREEN cohort 内部 Identity 不统一。",
        )
    red_identity = next(iter(red_identities))
    green_identity = next(iter(green_identities))
    if red_identity is None or green_identity is None:
        raise EvolutionInterventionalComparisonError(
            "interventional_comparison_identity_missing",
            "Interventional RED/GREEN cohort 缺少可比较 Identity。",
        )
    if not (
        red_identity.configuration == green_identity.configuration == configuration
        and red_identity.platform == green_identity.platform
        and red_identity.source.commit == request.baseline_commit
        and red_identity.source.tree_sha256 == f"sha256:{request.baseline_tree_sha256}"
        and not red_identity.source.dirty
        and green_identity.source.commit == request.baseline_commit
        and green_identity.source.tree_sha256 == green.candidate_tree_sha256
        and green_identity.identity_sha256 == green.candidate_identity_sha256
        and green_identity.source.dirty
    ):
        raise EvolutionInterventionalComparisonError(
            "interventional_comparison_identity_mismatch",
            "Interventional RED/GREEN configuration、平台或 source identity 不可比较。",
        )
    _require_case_structure(red_records, request, binding, phase="red")
    _require_case_structure(green_records, request, binding, phase="green")


def _require_case_structure(records, request, binding, *, phase: str) -> None:
    expected_checks = tuple(item.check_id for item in request.checks)
    expected_metrics = tuple(item.metric_name for item in binding.entries)
    for record in records:
        checks = tuple(
            item for item in record.result.cases
            if item.runner == INTERVENTIONAL_RED_CHECK_RUNNER
        )
        metric_cases = tuple(
            item for item in record.result.cases
            if item.runner == SELF_REVIEW_STATIC_RUNNER_VERSION
        )
        observed_metrics = tuple(
            observation.metric
            for case in metric_cases
            for observation in case.metric_observations
        )
        if not (
            tuple(item.case_id for item in checks) == expected_checks
            and all(interventional_lifecycle_digest(item.message) for item in checks)
            and all(interventional_run_scope(item.message) == "cohort" for item in checks)
            and all(interventional_run_grant_digest(item.message) for item in checks)
            and observed_metrics == expected_metrics
            and all(len(item.metric_observations) == 1 for item in metric_cases)
            and len(checks) + len(metric_cases) == len(record.result.cases)
        ):
            raise EvolutionInterventionalComparisonError(
                f"interventional_{phase}_case_evidence_mismatch",
                f"Interventional {phase.upper()} check/metric/lifecycle evidence 不完整。",
            )


def _require_summary_evidence(records, receipt, request, binding, *, phase: str) -> None:
    checks = []
    check_ids = sorted(item.check_id for item in request.checks)
    for check_id in check_ids:
        statuses = Counter(
            case.status.value
            for record in records
            for case in record.result.cases
            if case.runner == INTERVENTIONAL_RED_CHECK_RUNNER
            and case.case_id == check_id
        )
        checks.append((
            check_id,
            statuses["passed"],
            statuses["implementation_failure"],
            statuses["evaluation_error"],
        ))
    receipt_checks = tuple(
        (item.check_id, item.passed, item.failed, item.evaluation_errors)
        for item in receipt.checks
    )
    metrics = []
    for entry in sorted(binding.entries, key=lambda item: item.metric_name):
        observations = tuple(
            observation
            for record in records
            for case in record.result.cases
            for observation in case.metric_observations
            if observation.metric == entry.metric_name
        )
        if len(observations) != len(records):
            raise EvolutionInterventionalComparisonError(
                f"interventional_{phase}_summary_mismatch",
                f"Interventional {phase.upper()} metric summary 与 H5a 不一致。",
            )
        metrics.append((
            entry.metric_name,
            observations[0].unit,
            entry.direction,
            entry.target,
            tuple(item.value for item in observations),
        ))
    receipt_metrics = tuple(
        (
            item.metric_name,
            item.unit,
            item.direction,
            item.target,
            item.sample_values,
        )
        for item in receipt.metrics
    )
    run_grants = tuple(sorted({
        digest
        for record in records
        for case in record.result.cases
        if case.runner == INTERVENTIONAL_RED_CHECK_RUNNER
        and (digest := interventional_run_grant_digest(case.message)) is not None
    }))
    if not (
        tuple(checks) == receipt_checks
        and tuple(metrics) == receipt_metrics
        and run_grants == receipt.cohort_run_grant_sha256
        and receipt.completed_at == max(item.created_at for item in records)
    ):
        raise EvolutionInterventionalComparisonError(
            f"interventional_{phase}_summary_mismatch",
            f"Interventional {phase.upper()} completion summary 与 H5a 不一致。",
        )


__all__ = [
    "EvolutionInterventionalComparisonError",
    "EvolutionInterventionalComparisonExecutor",
]
