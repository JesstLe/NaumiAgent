from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.revalidation_adversarial_comparisons import (
    EvolutionRevalidationAdversarialComparisonError,
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


@pytest.mark.asyncio
async def test_fresh_adversarial_comparison_requires_complete_matrix(tmp_path: Path):
    base, contract, _parent, harness_store, _kernel, _state = await _executor_scenario(
        tmp_path
    )
    sample = _sample_executor(base, harness_store)
    cohort = _cohort(sample, harness_store)
    matrix = _service(
        sample, WorkerRegistryStore(tmp_path / ".naumi" / "workers.db")
    )
    executor = EvolutionRevalidationAdversarialComparisonExecutor(
        workspace_root=tmp_path,
        harness_store=harness_store,
        cohort_store=cohort.receipt_store,
        matrix_service=matrix,
        contract_service=sample.contract_service,
    )

    with pytest.raises(EvolutionRevalidationAdversarialComparisonError) as captured:
        await executor.execute(contract_id=contract.contract_id)
    assert captured.value.code == "fresh_adversarial_comparison_matrix_incomplete"


@pytest.mark.asyncio
async def test_fresh_adversarial_comparison_recomputes_platform_h5c(tmp_path: Path):
    base, contract, parent, harness_store, kernel, _state = await _executor_scenario(
        tmp_path
    )
    sample = _sample_executor(base, harness_store)
    cohort_executor = _cohort(sample, harness_store)
    platform = capture_eval_platform_identity().system
    cohort = await cohort_executor.execute(
        contract_id=contract.contract_id,
        platform=platform,
        parent_receipt_id=parent.receipt_id,
    )
    matrix = _service(
        sample, WorkerRegistryStore(tmp_path / ".naumi" / "workers.db")
    )
    executor = EvolutionRevalidationAdversarialComparisonExecutor(
        workspace_root=tmp_path,
        harness_store=harness_store,
        cohort_store=cohort_executor.receipt_store,
        matrix_service=matrix,
        contract_service=sample.contract_service,
    )

    receipts = await executor.execute(contract_id=contract.contract_id)
    repeated = await executor.execute(contract_id=contract.contract_id)

    assert repeated == receipts
    assert len(receipts) == 1
    assert receipts[0].receipt.baseline_samples == 7
    assert receipts[0].receipt.current_samples == 7
    assert receipts[0].receipt.statistical_verdict == "unchanged"
    assert receipts[0].receipt.decision == "passed"
    assert receipts[0].receipt.baseline_batch_id == cohort.red_batch_id
    assert receipts[0].receipt.current_batch_id == cohort.green_batch_id
    assert kernel.calls == ["adversarial"] * 14

    async with harness_store._connection() as db:
        await db.execute(
            "DELETE FROM harness_eval_results WHERE batch_id = ? AND sample_index = ?",
            (cohort.green_batch_id, 6),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationAdversarialComparisonError) as missing:
        await executor.execute(contract_id=contract.contract_id)
    assert missing.value.code == (
        "fresh_adversarial_comparison_cohort_evidence_mismatch"
    )
