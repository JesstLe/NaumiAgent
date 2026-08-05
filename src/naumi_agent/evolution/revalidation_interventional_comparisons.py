"""Fresh authority gate for native paired Interventional H5c comparison."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from naumi_agent.evolution.comparison_kernel import (
    EvolutionComparisonKernel,
    EvolutionComparisonKernelError,
)
from naumi_agent.evolution.revalidation_interventional_cohorts import (
    EvolutionRevalidationInterventionalCheckSummary,
    EvolutionRevalidationInterventionalCohortStore,
    EvolutionRevalidationInterventionalMetricSummary,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)
from naumi_agent.harness.eval_models import EvalCaseStatus
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredEvalComparisonReceipt,
    HarnessStoreError,
)

_PROFILE_RUNNER = "evolution_profile_check@1"


class EvolutionRevalidationInterventionalComparisonError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationInterventionalComparisonExecutor:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        harness_store: HarnessStore,
        cohort_store: EvolutionRevalidationInterventionalCohortStore,
        contract_service: EvolutionRevalidationRuntimeContractService,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.harness_store = harness_store
        self.cohort_store = cohort_store
        self.contract_service = contract_service
        self.kernel = EvolutionComparisonKernel(harness_store)

    async def execute(
        self,
        *,
        contract_id: str,
    ) -> HarnessStoredEvalComparisonReceipt:
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=contract_id,
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationInterventionalComparisonError(
                "fresh_comparison_runtime_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}。",
            )
        contract = view.contract
        try:
            cohort = await self.cohort_store.get_by_contract(contract_id)
        except ValueError as exc:
            raise EvolutionRevalidationInterventionalComparisonError(
                "fresh_comparison_cohort_corrupt",
                "Fresh Interventional cohort receipt 已损坏。",
            ) from exc
        if cohort is None:
            raise EvolutionRevalidationInterventionalComparisonError(
                "fresh_comparison_cohort_missing",
                "Fresh Interventional cohort 尚未完成。",
            )
        _require_authority(contract, cohort)
        try:
            red = await self.harness_store.list_eval_results(
                self.workspace_root,
                cohort.red_batch_id,
                cohort.suite_id,
                limit=cohort.requested_samples + 1,
            )
            green = await self.harness_store.list_eval_results(
                self.workspace_root,
                cohort.green_batch_id,
                cohort.suite_id,
                limit=cohort.requested_samples + 1,
            )
            _require_records(cohort, red, green)
            return await self.kernel.execute(
                workspace_root=self.workspace_root,
                suite_id=cohort.suite_id,
                red_batch_id=cohort.red_batch_id,
                green_batch_id=cohort.green_batch_id,
                red_completed_at=cohort.completed_at,
                green_completed_at=cohort.completed_at,
                validation_plan_id=cohort.validation_plan_id,
                lane_label="Interventional",
                red_records=red,
                green_records=green,
            )
        except EvolutionRevalidationInterventionalComparisonError:
            raise
        except EvolutionComparisonKernelError as exc:
            raise EvolutionRevalidationInterventionalComparisonError(
                exc.code,
                str(exc),
            ) from exc
        except (HarnessStoreError, ValueError) as exc:
            raise EvolutionRevalidationInterventionalComparisonError(
                "fresh_comparison_evidence_unavailable",
                "Fresh Interventional H5a evidence 不可用。",
            ) from exc


def _require_authority(contract, cohort) -> None:
    if not (
        cohort.contract_id == contract.contract_id
        and cohort.contract_sha256 == contract.contract_sha256
        and cohort.validation_plan_id == contract.validation_plan_id
        and cohort.validation_plan_sha256 == contract.validation_plan_sha256
        and cohort.source_snapshot_id == contract.source_snapshot_id
        and cohort.source_snapshot_sha256 == contract.source_snapshot_sha256
        and cohort.suite_id == contract.suite_id
        and cohort.requested_samples == contract.requested_samples
        and cohort.persisted_samples == contract.requested_samples
        and cohort.cohort_complete
        and not cohort.comparison_authority
        and not cohort.promotion_authority
    ):
        raise EvolutionRevalidationInterventionalComparisonError(
            "fresh_comparison_authority_mismatch",
            "Fresh Contract 与 Interventional cohort authority 不一致。",
        )


def _require_records(cohort, red, green) -> None:
    count = cohort.requested_samples
    indexes = tuple(range(count))
    if not (
        len(red) == len(green) == count
        and tuple(item.sample_index for item in red) == indexes
        and tuple(item.sample_index for item in green) == indexes
        and tuple(item.result_sha256 for item in red) == cohort.red_result_sha256
        and tuple(item.result_sha256 for item in green) == cohort.green_result_sha256
    ):
        raise EvolutionRevalidationInterventionalComparisonError(
            "fresh_comparison_cohort_evidence_mismatch",
            "Fresh RED/GREEN H5a 与 cohort receipt 不一致。",
        )
    red_identities = {item.result.baseline_identity for item in red}
    green_identities = {item.result.baseline_identity for item in green}
    if None in red_identities or None in green_identities or (
        len(red_identities) != 1 or len(green_identities) != 1
    ):
        raise EvolutionRevalidationInterventionalComparisonError(
            "fresh_comparison_identity_unstable",
            "Fresh RED/GREEN cohort identity 不统一。",
        )
    red_identity = next(iter(red_identities))
    green_identity = next(iter(green_identities))
    if not (
        red_identity.configuration == green_identity.configuration
        and red_identity.platform == green_identity.platform
        and red_identity.source.commit == green_identity.source.commit
        and not red_identity.source.dirty
        and green_identity.source.dirty
        and red_identity.identity_sha256 != green_identity.identity_sha256
    ):
        raise EvolutionRevalidationInterventionalComparisonError(
            "fresh_comparison_identity_mismatch",
            "Fresh RED/GREEN configuration、平台或 source identity 不可比较。",
        )
    if _metrics(cohort, red, green) != cohort.metrics or _checks(
        cohort, red, green
    ) != cohort.checks:
        raise EvolutionRevalidationInterventionalComparisonError(
            "fresh_comparison_summary_mismatch",
            "Fresh cohort summary 无法由 H5a 机械重算。",
        )


def _metrics(cohort, red, green):
    summaries = []
    for expected in cohort.metrics:
        red_observations = _observations(red, expected.metric_name)
        green_observations = _observations(green, expected.metric_name)
        if len(red_observations) != len(red) or len(green_observations) != len(green):
            return ()
        summaries.append(EvolutionRevalidationInterventionalMetricSummary(
            metric_name=expected.metric_name,
            unit=red_observations[0].unit,
            direction=expected.direction,
            target=expected.target,
            red_values=tuple(item.value for item in red_observations),
            green_values=tuple(item.value for item in green_observations),
        ))
    return tuple(summaries)


def _observations(records, metric_name):
    return tuple(
        observation
        for record in records
        for case in record.result.cases
        for observation in case.metric_observations
        if observation.metric == metric_name
    )


def _checks(cohort, red, green):
    summaries = []
    for item in cohort.checks:
        red_statuses = _statuses(red, item.check_id)
        green_statuses = _statuses(green, item.check_id)
        summaries.append(EvolutionRevalidationInterventionalCheckSummary(
            check_id=item.check_id,
            red_passed=red_statuses[EvalCaseStatus.PASSED.value],
            red_failed=red_statuses[EvalCaseStatus.IMPLEMENTATION_FAILURE.value],
            red_evaluation_errors=red_statuses[EvalCaseStatus.EVALUATION_ERROR.value],
            green_passed=green_statuses[EvalCaseStatus.PASSED.value],
            green_failed=green_statuses[EvalCaseStatus.IMPLEMENTATION_FAILURE.value],
            green_evaluation_errors=green_statuses[EvalCaseStatus.EVALUATION_ERROR.value],
        ))
    return tuple(summaries)


def _statuses(records, check_id):
    return Counter(
        case.status.value
        for record in records
        for case in record.result.cases
        if case.runner == _PROFILE_RUNNER and case.case_id == check_id
    )


__all__ = [
    "EvolutionRevalidationInterventionalComparisonError",
    "EvolutionRevalidationInterventionalComparisonExecutor",
]
