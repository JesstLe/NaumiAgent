from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_interventional_comparisons import (
    EvolutionRevalidationInterventionalComparisonError,
    EvolutionRevalidationInterventionalComparisonExecutor,
)
from tests.unit.test_evolution_revalidation_interventional_cohorts import (
    _cohort_executor,
)
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
)


async def _scenario(tmp_path: Path):
    sample, contract, parent, harness_store, kernel, state = await _executor_scenario(
        tmp_path
    )
    cohort_executor = _cohort_executor(sample, contract, harness_store)
    cohort = await cohort_executor.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
    )
    comparison = EvolutionRevalidationInterventionalComparisonExecutor(
        workspace_root=tmp_path,
        harness_store=harness_store,
        cohort_store=cohort_executor.receipt_store,
        contract_service=sample.contract_service,
    )
    return comparison, cohort, contract, harness_store, state, kernel


@pytest.mark.asyncio
async def test_fresh_comparison_recomputes_native_h5c_from_paired_h5a(
    tmp_path: Path,
) -> None:
    executor, cohort, contract, store, _state, kernel = await _scenario(tmp_path)

    receipt = await executor.execute(contract_id=contract.contract_id)
    repeated = await executor.execute(contract_id=contract.contract_id)
    reference = await store.get_eval_baseline_by_batch(
        tmp_path,
        contract.suite_id,
        cohort.red_batch_id,
    )

    assert repeated == receipt
    assert reference is not None and reference.purpose == "comparison_reference"
    assert await store.get_active_eval_baseline(tmp_path, contract.suite_id) is None
    assert receipt.receipt.baseline_samples == receipt.receipt.current_samples == 7
    assert len(receipt.receipt.sample_evidence) == 7
    assert receipt.receipt.statistical_verdict == "unchanged"
    assert receipt.receipt.decision == "passed"
    assert all(
        item.policy_verdict == "passed"
        for item in receipt.receipt.sample_evidence
    )
    assert receipt.receipt.baseline_batch_id == cohort.red_batch_id
    assert receipt.receipt.current_batch_id == cohort.green_batch_id
    assert kernel.calls == [lane for _ in range(7) for lane in ("red", "green")]


@pytest.mark.asyncio
async def test_fresh_comparison_fails_before_h5c_on_stale_or_missing_h5a(
    tmp_path: Path,
) -> None:
    executor, cohort, contract, store, state, _kernel = await _scenario(tmp_path)
    state.current = False
    with pytest.raises(EvolutionRevalidationInterventionalComparisonError) as stale:
        await executor.execute(contract_id=contract.contract_id)
    assert stale.value.code == "fresh_comparison_runtime_contract_not_ready"

    state.current = True
    async with store._connection() as db:
        await db.execute(
            "DELETE FROM harness_eval_results WHERE batch_id = ? AND sample_index = ?",
            (cohort.green_batch_id, 6),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationInterventionalComparisonError) as missing:
        await executor.execute(contract_id=contract.contract_id)
    assert missing.value.code == "fresh_comparison_cohort_evidence_mismatch"
    assert await store.get_eval_baseline_by_batch(
        tmp_path,
        contract.suite_id,
        cohort.red_batch_id,
    ) is None
