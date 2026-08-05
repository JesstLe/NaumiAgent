from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.revalidation_final_evaluations import (
    EvolutionRevalidationFinalEvaluationError,
    EvolutionRevalidationFinalEvaluationExecutor,
    EvolutionRevalidationFinalEvaluationStore,
)
from naumi_agent.harness.eval_identity import capture_eval_platform_identity
from tests.unit.test_evolution_revalidation_adversarial_attributions import (
    _executor as _adversarial_attribution,
)
from tests.unit.test_evolution_revalidation_adversarial_cohorts import (
    _cohort as _adversarial_cohort,
)
from tests.unit.test_evolution_revalidation_adversarial_matrices import _service
from tests.unit.test_evolution_revalidation_adversarial_samples import (
    _executor as _adversarial_sample,
)
from tests.unit.test_evolution_revalidation_interventional_attributions import (
    _attribution as _interventional_attribution,
)
from tests.unit.test_evolution_revalidation_interventional_cohorts import (
    _cohort_executor,
)
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
)


def _final(sample, harness_store, interventional, adversarial, matrix):
    interventional_attribution = _interventional_attribution(
        sample, harness_store, interventional
    )
    adversarial_attribution = _adversarial_attribution(
        adversarial, harness_store, matrix, adversarial
    )
    return EvolutionRevalidationFinalEvaluationExecutor(
        workspace_root=sample.workspace_root,
        contract_service=sample.contract_service,
        matrix_service=matrix,
        interventional_cohort_store=interventional.receipt_store,
        adversarial_cohort_store=adversarial.receipt_store,
        interventional_comparison_executor=(
            interventional_attribution.comparison_executor
        ),
        adversarial_comparison_executor=adversarial_attribution.comparison_executor,
        interventional_attribution_executor=interventional_attribution,
        adversarial_attribution_executor=adversarial_attribution,
        receipt_store=EvolutionRevalidationFinalEvaluationStore(
            sample.receipt_store._db_path
        ),
    )


@pytest.mark.asyncio
async def test_fresh_final_evaluation_requires_complete_matrix(tmp_path: Path):
    sample, contract, _parent, harness_store, _kernel, _state = (
        await _executor_scenario(tmp_path)
    )
    adversarial = _adversarial_sample(sample, harness_store)
    adversarial_cohort = _adversarial_cohort(adversarial, harness_store)
    interventional = _cohort_executor(sample, contract, harness_store)
    matrix = _service(
        adversarial, WorkerRegistryStore(tmp_path / ".naumi" / "workers.db")
    )

    with pytest.raises(EvolutionRevalidationFinalEvaluationError) as captured:
        await _final(
            sample, harness_store, interventional, adversarial_cohort, matrix
        ).execute(contract_id=contract.contract_id)
    assert captured.value.code == "fresh_final_evaluation_matrix_incomplete"


@pytest.mark.asyncio
async def test_fresh_final_evaluation_binds_all_current_evidence(tmp_path: Path):
    sample, contract, parent, harness_store, _kernel, _state = (
        await _executor_scenario(tmp_path)
    )
    interventional = _cohort_executor(sample, contract, harness_store)
    await interventional.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
    )
    adversarial = _adversarial_sample(sample, harness_store)
    adversarial_cohort = _adversarial_cohort(adversarial, harness_store)
    await adversarial_cohort.execute(
        contract_id=contract.contract_id,
        platform=capture_eval_platform_identity().system,
        parent_receipt_id=parent.receipt_id,
    )
    matrix = _service(
        adversarial, WorkerRegistryStore(tmp_path / ".naumi" / "workers.db")
    )
    executor = _final(
        sample, harness_store, interventional, adversarial_cohort, matrix
    )

    receipt = await executor.execute(contract_id=contract.contract_id)
    repeated = await executor.execute(contract_id=contract.contract_id)

    assert repeated == receipt
    assert receipt.evaluation_complete and receipt.source_current_at_issue
    assert len(receipt.comparison_ids) == len(receipt.attribution_ids) == 2
    assert receipt.contract == contract
    assert tuple(item.platform for item in receipt.adversarial) == (
        contract.required_platforms
    )
    assert not receipt.reapproval_eligible
    assert not receipt.promotion_authority
    assert receipt.interventional.attribution.category == "objective_not_improved"
    assert receipt.adversarial[0].attribution.reason_code == (
        "adversarial_guardrail_preserved"
    )

    with sqlite3.connect(executor.receipt_store._db_path) as db:
        db.execute(
            "DELETE FROM evolution_failure_attributions WHERE comparison_id = ?",
            (receipt.interventional.comparison_id,),
        )
    with pytest.raises(EvolutionRevalidationFinalEvaluationError) as missing:
        await executor.receipt_store.record(receipt)
    assert missing.value.code == (
        "fresh_final_evaluation_attribution_dependency_mismatch"
    )
