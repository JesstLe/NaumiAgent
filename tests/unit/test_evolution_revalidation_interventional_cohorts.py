from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_interventional_cohorts import (
    EvolutionRevalidationInterventionalCohortError,
    EvolutionRevalidationInterventionalCohortExecutor,
    EvolutionRevalidationInterventionalCohortStore,
    _validate_red_run_evidence,
)
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
)


class _InterruptOnce:
    def __init__(self, delegate, *, sample_index: int) -> None:
        self.delegate = delegate
        self.sample_index = sample_index
        self.interrupted = False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def execute(self, **kwargs):
        if (
            kwargs["sample_index"] == self.sample_index
            and kwargs.get("run_authority") is not None
            and not self.interrupted
        ):
            self.interrupted = True
            raise RuntimeError("simulated fresh cohort interruption")
        return await self.delegate.execute(**kwargs)


def _cohort_executor(sample_executor, contract, harness_store, *, token="a" * 32):
    state_db = sample_executor.receipt_store._db_path
    return EvolutionRevalidationInterventionalCohortExecutor(
        workspace_root=sample_executor.workspace_root,
        harness_store=harness_store,
        sample_store=sample_executor.receipt_store,
        receipt_store=EvolutionRevalidationInterventionalCohortStore(state_db),
        permission_store=sample_executor.permission_store,
        run_grant_authority=sample_executor.run_grant_authority,
        sample_executor=sample_executor,
        contract_service=sample_executor.contract_service,
        token=lambda: token,
    )


@pytest.mark.asyncio
async def test_fresh_cohort_persists_continuous_paired_samples(tmp_path: Path) -> None:
    sample, contract, parent, harness_store, kernel, _state = await _executor_scenario(
        tmp_path
    )
    executor = _cohort_executor(sample, contract, harness_store)

    receipt = await executor.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
    )
    repeated = await executor.execute(
        contract_id=contract.contract_id,
        parent_receipt_id="not-needed-after-completion",
    )

    assert repeated == receipt
    assert receipt.persisted_samples == receipt.requested_samples == 7
    assert receipt.cohort_complete
    assert not receipt.comparison_authority
    assert not receipt.promotion_authority
    assert len(receipt.red_result_sha256) == len(receipt.green_result_sha256) == 7
    assert len(receipt.platform_sha256) == 1
    assert len(receipt.cohort_run_grant_sha256) == 1
    assert kernel.calls == [lane for _ in range(7) for lane in ("red", "green")]
    assert receipt.metrics[0].metric_name == "self_review.broad_except.count"
    assert len(receipt.metrics[0].red_values) == len(receipt.metrics[0].green_values) == 7
    with sqlite3.connect(tmp_path / ".naumi" / "run-grants.db") as db:
        assert db.execute(
            "SELECT state, revoke_reason FROM run_delegation_grants"
        ).fetchall() == [("revoked", "cohort_finished")]


@pytest.mark.asyncio
async def test_fresh_cohort_resumes_only_missing_continuous_suffix(
    tmp_path: Path,
) -> None:
    sample, contract, parent, harness_store, kernel, _state = await _executor_scenario(
        tmp_path
    )
    interrupted = _InterruptOnce(sample, sample_index=2)
    first = _cohort_executor(interrupted, contract, harness_store, token="a" * 32)

    with pytest.raises(RuntimeError, match="simulated fresh cohort interruption"):
        await first.execute(
            contract_id=contract.contract_id,
            parent_receipt_id=parent.receipt_id,
        )
    assert kernel.calls == ["red", "green", "red", "green"]

    resumed = _cohort_executor(sample, contract, harness_store, token="b" * 32)
    receipt = await resumed.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
    )

    assert receipt.persisted_samples == 7
    assert kernel.calls == [lane for _ in range(7) for lane in ("red", "green")]
    assert len(receipt.cohort_run_grant_sha256) == 2


@pytest.mark.asyncio
async def test_fresh_cohort_rejects_unbound_run_grant_evidence(tmp_path: Path) -> None:
    sample, contract, parent, harness_store, _kernel, _state = await _executor_scenario(
        tmp_path
    )
    executor = _cohort_executor(sample, contract, harness_store)
    await executor.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
    )
    records = await executor._records(contract, "red")
    first = records[0]
    cases = tuple(
        case.model_copy(update={"message": "run_scope=cohort"})
        if case.runner == "evolution_profile_check@1"
        else case
        for case in first.result.cases
    )
    tampered = replace(
        first,
        result=first.result.model_copy(update={"cases": cases}),
    )

    with pytest.raises(
        EvolutionRevalidationInterventionalCohortError,
        match="Run Grant evidence",
    ):
        _validate_red_run_evidence((tampered, *records[1:]), contract)


@pytest.mark.asyncio
async def test_standalone_sample_does_not_block_scoped_cohort(tmp_path: Path) -> None:
    sample, contract, parent, harness_store, kernel, _state = await _executor_scenario(
        tmp_path
    )
    standalone = await sample.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
        sample_index=0,
    )
    cohort = await _cohort_executor(sample, contract, harness_store).execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
    )

    scoped = await sample.receipt_store.get_by_sample(
        contract.contract_id,
        0,
        "cohort",
    )
    assert scoped is not None and scoped.receipt_id != standalone.receipt_id
    assert cohort.sample_receipt_sha256[0] == scoped.receipt_sha256
    assert kernel.calls == ["red", "green"] + [
        lane for _ in range(7) for lane in ("red", "green")
    ]
