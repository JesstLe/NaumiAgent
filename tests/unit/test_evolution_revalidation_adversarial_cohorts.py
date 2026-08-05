from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_adversarial_cohorts import (
    EvolutionRevalidationAdversarialCohortExecutor,
    EvolutionRevalidationAdversarialCohortStore,
)
from naumi_agent.harness.eval_identity import capture_eval_platform_identity
from tests.unit.test_evolution_revalidation_adversarial_samples import (
    _executor as _sample_executor,
)
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
)


class _InterruptOnce:
    def __init__(self, delegate, sample_index: int) -> None:
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
            raise RuntimeError("simulated adversarial cohort interruption")
        return await self.delegate.execute(**kwargs)


def _cohort(sample, harness_store, *, token="a" * 32):
    return EvolutionRevalidationAdversarialCohortExecutor(
        workspace_root=sample.workspace_root,
        harness_store=harness_store,
        sample_store=sample.receipt_store,
        receipt_store=EvolutionRevalidationAdversarialCohortStore(
            sample.receipt_store._db_path
        ),
        permission_store=sample.permission_store,
        run_grant_authority=sample.run_grant_authority,
        sample_executor=sample,
        contract_service=sample.contract_service,
        token=lambda: token,
    )


@pytest.mark.asyncio
async def test_fresh_adversarial_cohort_persists_continuous_platform_pair(
    tmp_path: Path,
) -> None:
    base, contract, parent, harness_store, kernel, _state = await _executor_scenario(
        tmp_path
    )
    platform = capture_eval_platform_identity().system
    sample = _sample_executor(base, harness_store)
    executor = _cohort(sample, harness_store)

    receipt = await executor.execute(
        contract_id=contract.contract_id,
        platform=platform,
        parent_receipt_id=parent.receipt_id,
    )
    repeated = await executor.execute(
        contract_id=contract.contract_id,
        platform=platform,
        parent_receipt_id="not-needed-after-completion",
    )

    assert repeated == receipt
    assert receipt.persisted_samples == receipt.requested_samples == 7
    assert receipt.cohort_complete and not receipt.matrix_complete
    assert not receipt.comparison_authority and not receipt.promotion_authority
    assert len(receipt.cohort_run_grant_sha256) == 1
    assert len(receipt.checks[0].red_values) == 7
    assert len(receipt.checks[0].green_values) == 7
    assert kernel.calls == ["adversarial"] * 14
    with sqlite3.connect(tmp_path / ".naumi" / "run-grants.db") as db:
        assert db.execute(
            "SELECT state, revoke_reason FROM run_delegation_grants"
        ).fetchall() == [("revoked", "cohort_finished")]


@pytest.mark.asyncio
async def test_fresh_adversarial_cohort_resumes_only_missing_suffix(
    tmp_path: Path,
) -> None:
    base, contract, parent, harness_store, kernel, _state = await _executor_scenario(
        tmp_path
    )
    platform = capture_eval_platform_identity().system
    sample = _sample_executor(base, harness_store)
    interrupted = _InterruptOnce(sample, 2)

    with pytest.raises(RuntimeError, match="simulated adversarial cohort interruption"):
        await _cohort(interrupted, harness_store).execute(
            contract_id=contract.contract_id,
            platform=platform,
            parent_receipt_id=parent.receipt_id,
        )
    assert kernel.calls == ["adversarial"] * 4

    receipt = await _cohort(sample, harness_store, token="b" * 32).execute(
        contract_id=contract.contract_id,
        platform=platform,
        parent_receipt_id=parent.receipt_id,
    )
    assert receipt.persisted_samples == 7
    assert len(receipt.cohort_run_grant_sha256) == 2
    assert kernel.calls == ["adversarial"] * 14
