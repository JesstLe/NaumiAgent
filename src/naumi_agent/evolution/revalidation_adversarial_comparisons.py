"""Native H5b2/H5c comparison for a completed Fresh Adversarial matrix."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from naumi_agent.evolution.comparison_kernel import (
    EvolutionComparisonKernel,
    EvolutionComparisonKernelError,
)
from naumi_agent.evolution.revalidation_adversarial_cohorts import (
    EvolutionRevalidationAdversarialCheckSummary,
    EvolutionRevalidationAdversarialCohortStore,
)
from naumi_agent.evolution.revalidation_adversarial_matrices import (
    EvolutionRevalidationAdversarialMatrixService,
)
from naumi_agent.evolution.revalidation_adversarial_samples import (
    EVOLUTION_REVALIDATION_ADVERSARIAL_RUNNER,
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


class EvolutionRevalidationAdversarialComparisonError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationAdversarialComparisonExecutor:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        harness_store: HarnessStore,
        cohort_store: EvolutionRevalidationAdversarialCohortStore,
        matrix_service: EvolutionRevalidationAdversarialMatrixService,
        contract_service: EvolutionRevalidationRuntimeContractService,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.harness_store = harness_store
        self.cohort_store = cohort_store
        self.matrix_service = matrix_service
        self.contract_service = contract_service
        self.kernel = EvolutionComparisonKernel(harness_store)

    async def execute(
        self, *, contract_id: str
    ) -> tuple[HarnessStoredEvalComparisonReceipt, ...]:
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root, contract_id=contract_id
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationAdversarialComparisonError(
                "fresh_adversarial_comparison_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}。",
            )
        matrix = await self.matrix_service.inspect(contract_id=contract_id)
        if not matrix.matrix_complete:
            raise EvolutionRevalidationAdversarialComparisonError(
                "fresh_adversarial_comparison_matrix_incomplete",
                "Fresh Adversarial required-platform matrix 尚未完成。",
            )
        contract = view.contract
        comparisons = []
        for lane in matrix.lanes:
            cohort = await self.cohort_store.get(contract_id, lane.platform)
            if cohort is None:
                raise EvolutionRevalidationAdversarialComparisonError(
                    "fresh_adversarial_comparison_cohort_missing",
                    f"平台 {lane.platform} 的 Fresh Adversarial cohort 不存在。",
                )
            _require_authority(contract, matrix, lane, cohort)
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
                _require_records(contract, cohort, red, green)
                comparisons.append(await self.kernel.execute(
                    workspace_root=self.workspace_root,
                    suite_id=cohort.suite_id,
                    red_batch_id=cohort.red_batch_id,
                    green_batch_id=cohort.green_batch_id,
                    red_completed_at=cohort.completed_at,
                    green_completed_at=cohort.completed_at,
                    validation_plan_id=cohort.validation_plan_id,
                    lane_label=f"Adversarial[{lane.platform}]",
                    red_records=red,
                    green_records=green,
                ))
            except EvolutionRevalidationAdversarialComparisonError:
                raise
            except EvolutionComparisonKernelError as exc:
                raise EvolutionRevalidationAdversarialComparisonError(
                    exc.code, str(exc)
                ) from exc
            except (HarnessStoreError, ValueError) as exc:
                raise EvolutionRevalidationAdversarialComparisonError(
                    "fresh_adversarial_comparison_evidence_unavailable",
                    f"平台 {lane.platform} 的 Fresh Adversarial H5a evidence 不可用。",
                ) from exc
        return tuple(comparisons)


def _require_authority(contract, matrix, lane, cohort):
    if not (
        matrix.contract_id == contract.contract_id
        and matrix.contract_sha256 == contract.contract_sha256
        and lane.status == "completed"
        and lane.cohort_receipt_id == cohort.receipt_id
        and lane.cohort_receipt_sha256 == cohort.receipt_sha256
        and cohort.contract_id == contract.contract_id
        and cohort.contract_sha256 == contract.contract_sha256
        and cohort.validation_plan_id == contract.validation_plan_id
        and cohort.validation_plan_sha256 == contract.validation_plan_sha256
        and cohort.source_snapshot_id == contract.source_snapshot_id
        and cohort.source_snapshot_sha256 == contract.source_snapshot_sha256
        and cohort.platform == lane.platform
        and cohort.suite_id == contract.suite_id
        and cohort.requested_samples == cohort.persisted_samples
        == contract.requested_samples
    ):
        raise EvolutionRevalidationAdversarialComparisonError(
            "fresh_adversarial_comparison_authority_mismatch",
            "Fresh Contract、matrix 与 platform cohort authority 不一致。",
        )


def _require_records(contract, cohort, red, green):
    count = cohort.requested_samples
    indexes = tuple(range(count))
    if not (
        len(red) == len(green) == count
        and tuple(item.sample_index for item in red) == indexes
        and tuple(item.sample_index for item in green) == indexes
        and tuple(item.result_sha256 for item in red) == cohort.red_result_sha256
        and tuple(item.result_sha256 for item in green) == cohort.green_result_sha256
    ):
        raise EvolutionRevalidationAdversarialComparisonError(
            "fresh_adversarial_comparison_cohort_evidence_mismatch",
            "Fresh Adversarial RED/GREEN H5a 与 cohort receipt 不一致。",
        )
    red_identities = {item.result.baseline_identity for item in red}
    green_identities = {item.result.baseline_identity for item in green}
    if (
        None in red_identities
        or None in green_identities
        or len(red_identities) != 1
        or len(green_identities) != 1
    ):
        raise EvolutionRevalidationAdversarialComparisonError(
            "fresh_adversarial_comparison_identity_unstable",
            "Fresh Adversarial cohort identity 不统一。",
        )
    red_identity = next(iter(red_identities))
    green_identity = next(iter(green_identities))
    if not (
        red_identity.configuration == green_identity.configuration
        and red_identity.platform == green_identity.platform
        and red_identity.platform.system == cohort.platform
        and red_identity.source.commit == green_identity.source.commit
        and not red_identity.source.dirty
        and green_identity.source.dirty
        and red_identity.identity_sha256 != green_identity.identity_sha256
    ):
        raise EvolutionRevalidationAdversarialComparisonError(
            "fresh_adversarial_comparison_identity_mismatch",
            "Fresh Adversarial RED/GREEN configuration、平台或 source 不可比较。",
        )
    if _summaries(contract, cohort, red, green) != cohort.checks:
        raise EvolutionRevalidationAdversarialComparisonError(
            "fresh_adversarial_comparison_summary_mismatch",
            "Fresh Adversarial cohort summary 无法由原始 H5a 机械重算。",
        )


def _summaries(contract, cohort, red, green):
    expected = {item.check_id: item for item in contract.probe_checks}
    grants: set[str] = set()
    for records in (red, green):
        for record in records:
            cases = record.result.cases
            found = set()
            for case in cases:
                match = re.search(
                    r"(?:^| )run_grant_sha256=([0-9a-f]{64})(?:$| )",
                    case.message,
                )
                binding = expected.get(case.case_id)
                if not (
                    match
                    and binding is not None
                    and case.runner == EVOLUTION_REVALIDATION_ADVERSARIAL_RUNNER
                    and "run_scope=cohort" in case.message
                    and f"probe_kinds={','.join(binding.probes)}" in case.message
                ):
                    return ()
                found.add(match.group(1))
            if tuple(item.case_id for item in cases) != tuple(expected) or len(found) != 1:
                return ()
            grants.update(found)
    if tuple(sorted(grants)) != cohort.cohort_run_grant_sha256:
        return ()
    summaries = []
    for check_id in expected:
        red_cases = _cases(red, check_id)
        green_cases = _cases(green, check_id)
        summaries.append(EvolutionRevalidationAdversarialCheckSummary(
            check_id=check_id,
            metric_name=f"adversarial.{check_id}.exit_zero",
            red_passed=_count(red_cases, EvalCaseStatus.PASSED),
            red_failed=_count(red_cases, EvalCaseStatus.IMPLEMENTATION_FAILURE),
            red_evaluation_errors=_count(red_cases, EvalCaseStatus.EVALUATION_ERROR),
            green_passed=_count(green_cases, EvalCaseStatus.PASSED),
            green_failed=_count(green_cases, EvalCaseStatus.IMPLEMENTATION_FAILURE),
            green_evaluation_errors=_count(green_cases, EvalCaseStatus.EVALUATION_ERROR),
            red_values=tuple(_value(item, check_id) for item in red_cases),
            green_values=tuple(_value(item, check_id) for item in green_cases),
        ))
    return tuple(summaries)


def _cases(records, check_id):
    return tuple(
        case for record in records for case in record.result.cases
        if case.case_id == check_id
    )


def _count(cases, status):
    return Counter(item.status for item in cases)[status]


def _value(case, check_id):
    if case.status is EvalCaseStatus.EVALUATION_ERROR:
        if case.primary_metric or case.metric_observations:
            raise EvolutionRevalidationAdversarialComparisonError(
                "fresh_adversarial_comparison_metric_invalid",
                f"Fresh Adversarial case {check_id} evaluation error 伪造了 metric。",
            )
        return None
    metric = f"adversarial.{check_id}.exit_zero"
    if len(case.metric_observations) != 1:
        raise EvolutionRevalidationAdversarialComparisonError(
            "fresh_adversarial_comparison_metric_invalid",
            f"Fresh Adversarial case {check_id} metric 数量无效。",
        )
    observation = case.metric_observations[0]
    expected = 1.0 if case.status is EvalCaseStatus.PASSED else 0.0
    if not (
        case.primary_metric == observation.metric == metric
        and observation.value == expected
        and observation.unit == "scalar"
        and observation.direction == "increase"
        and observation.target == 1.0
        and observation.primary
    ):
        raise EvolutionRevalidationAdversarialComparisonError(
            "fresh_adversarial_comparison_metric_invalid",
            f"Fresh Adversarial case {check_id} metric evidence 无效。",
        )
    return observation.value


__all__ = [
    "EvolutionRevalidationAdversarialComparisonError",
    "EvolutionRevalidationAdversarialComparisonExecutor",
]
