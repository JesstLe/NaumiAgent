from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.failure_attribution import EvolutionFailureAttributionStore
from naumi_agent.evolution.revalidation_adversarial_attributions import (
    EvolutionRevalidationAdversarialAttributionError,
    EvolutionRevalidationAdversarialAttributionExecutor,
)
from naumi_agent.evolution.revalidation_adversarial_comparisons import (
    EvolutionRevalidationAdversarialComparisonExecutor,
)
from naumi_agent.harness.eval_identity import capture_eval_platform_identity
from tests.unit.test_evolution_revalidation_adversarial_cohorts import _cohort
from tests.unit.test_evolution_revalidation_adversarial_matrices import _service
from tests.unit.test_evolution_revalidation_adversarial_samples import (
    _executor as _sample_executor,
)
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
)


def _executor(sample, harness_store, matrix, cohort):
    comparison = EvolutionRevalidationAdversarialComparisonExecutor(
        workspace_root=sample.workspace_root,
        harness_store=harness_store,
        cohort_store=cohort.receipt_store,
        matrix_service=matrix,
        contract_service=sample.contract_service,
    )
    return EvolutionRevalidationAdversarialAttributionExecutor(
        workspace_root=sample.workspace_root,
        harness_store=harness_store,
        cohort_store=cohort.receipt_store,
        matrix_service=matrix,
        comparison_executor=comparison,
        contract_service=sample.contract_service,
        attribution_store=EvolutionFailureAttributionStore(
            sample.receipt_store._db_path
        ),
    )


@pytest.mark.asyncio
async def test_fresh_attribution_requires_complete_matrix(tmp_path: Path):
    base, contract, _parent, harness_store, _kernel, _state = await _executor_scenario(
        tmp_path
    )
    sample = _sample_executor(base, harness_store)
    cohort = _cohort(sample, harness_store)
    matrix = _service(
        sample, WorkerRegistryStore(tmp_path / ".naumi" / "workers.db")
    )

    with pytest.raises(EvolutionRevalidationAdversarialAttributionError) as captured:
        await _executor(sample, harness_store, matrix, cohort).execute(
            contract_id=contract.contract_id
        )
    assert captured.value.code == "fresh_adversarial_attribution_matrix_incomplete"


@pytest.mark.asyncio
async def test_fresh_attribution_persists_each_platform_h5c_classification(
    tmp_path: Path,
):
    base, contract, parent, harness_store, _kernel, _state = await _executor_scenario(
        tmp_path
    )
    sample = _sample_executor(base, harness_store)
    cohort = _cohort(sample, harness_store)
    await cohort.execute(
        contract_id=contract.contract_id,
        platform=capture_eval_platform_identity().system,
        parent_receipt_id=parent.receipt_id,
    )
    matrix = _service(
        sample, WorkerRegistryStore(tmp_path / ".naumi" / "workers.db")
    )
    executor = _executor(sample, harness_store, matrix, cohort)

    receipts = await executor.execute(contract_id=contract.contract_id)
    repeated = await executor.execute(contract_id=contract.contract_id)

    assert repeated == receipts
    assert len(receipts) == 1
    assert receipts[0].category == "none"
    assert receipts[0].action == "continue_to_reflection"
    assert not receipts[0].candidate_fault
    assert receipts[0].reflection_eligible
    assert receipts[0].validation_plan_id == contract.validation_plan_id
