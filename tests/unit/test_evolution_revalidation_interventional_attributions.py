from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.evolution.failure_attribution import EvolutionFailureAttributionStore
from naumi_agent.evolution.revalidation_interventional_attributions import (
    EvolutionRevalidationInterventionalAttributionError,
    EvolutionRevalidationInterventionalAttributionExecutor,
)
from naumi_agent.evolution.revalidation_interventional_comparisons import (
    EvolutionRevalidationInterventionalComparisonExecutor,
)
from tests.unit.test_evolution_revalidation_interventional_cohorts import (
    _cohort_executor,
)
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
)


def _attribution(sample, harness_store, cohort):
    comparison = EvolutionRevalidationInterventionalComparisonExecutor(
        workspace_root=sample.workspace_root,
        harness_store=harness_store,
        cohort_store=cohort.receipt_store,
        contract_service=sample.contract_service,
    )
    return EvolutionRevalidationInterventionalAttributionExecutor(
        workspace_root=sample.workspace_root,
        harness_store=harness_store,
        cohort_store=cohort.receipt_store,
        comparison_executor=comparison,
        contract_service=sample.contract_service,
        attribution_store=EvolutionFailureAttributionStore(
            sample.receipt_store._db_path
        ),
    )


@pytest.mark.asyncio
async def test_fresh_interventional_attribution_requires_cohort(tmp_path: Path):
    sample, contract, _parent, harness_store, _kernel, _state = (
        await _executor_scenario(tmp_path)
    )
    cohort = _cohort_executor(sample, contract, harness_store)

    with pytest.raises(EvolutionRevalidationInterventionalAttributionError) as captured:
        await _attribution(sample, harness_store, cohort).execute(
            contract_id=contract.contract_id
        )
    assert captured.value.code == "fresh_interventional_attribution_cohort_missing"


@pytest.mark.asyncio
async def test_fresh_interventional_attribution_persists_h5c_classification(
    tmp_path: Path,
):
    sample, contract, parent, harness_store, _kernel, _state = (
        await _executor_scenario(tmp_path)
    )
    cohort = _cohort_executor(sample, contract, harness_store)
    receipt = await cohort.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
    )
    executor = _attribution(sample, harness_store, cohort)

    attribution = await executor.execute(contract_id=contract.contract_id)
    repeated = await executor.execute(contract_id=contract.contract_id)

    assert repeated == attribution
    assert attribution.red_receipt_id == receipt.receipt_id
    assert attribution.green_receipt_id == receipt.receipt_id
    assert attribution.validation_plan_id == contract.validation_plan_id
    assert attribution.category == "objective_not_improved"
    assert attribution.action == "revise_candidate"
    assert not attribution.reflection_eligible
