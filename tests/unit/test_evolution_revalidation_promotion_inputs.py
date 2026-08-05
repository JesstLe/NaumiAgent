from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.failure_attribution import EvolutionFailureAttributionStore
from naumi_agent.evolution.revalidation_promotion_inputs import (
    EvolutionRevalidationPromotionInputError,
    EvolutionRevalidationPromotionInputService,
    EvolutionRevalidationPromotionInputStore,
)
from naumi_agent.evolution.revalidation_reapproval_authorities import (
    EvolutionRevalidationReapprovalAuthorityService,
    EvolutionRevalidationReapprovalAuthorityStore,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlanStore,
)
from naumi_agent.harness.eval_identity import capture_eval_platform_identity
from tests.unit.test_evolution_revalidation_adversarial_cohorts import (
    _cohort as _adversarial_cohort,
)
from tests.unit.test_evolution_revalidation_adversarial_matrices import _service
from tests.unit.test_evolution_revalidation_adversarial_samples import (
    _executor as _adversarial_sample,
)
from tests.unit.test_evolution_revalidation_final_evaluations import _final
from tests.unit.test_evolution_revalidation_interventional_cohorts import (
    _cohort_executor,
)
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
)
from tests.unit.test_evolution_revalidation_reapproval_authorities import (
    _eligible_attribution,
    _eligible_final,
)


class _PriorInputStore:
    def __init__(self, package) -> None:
        self._view = type("PriorInputView", (), {"package_input": package})()

    async def get(self, input_id: str):
        if input_id == self._view.package_input.input_id:
            return self._view
        return None


def _persist_prior_dependency(db_path: Path, package) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS evolution_promotion_package_inputs ("
            "input_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, "
            "reflection_id TEXT NOT NULL UNIQUE, reflection_sha256 TEXT NOT NULL, "
            "decision_state_id TEXT NOT NULL UNIQUE, workspace_root TEXT NOT NULL, "
            "candidate_id TEXT NOT NULL, candidate_revision INTEGER NOT NULL, "
            "input_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR REPLACE INTO evolution_promotion_package_inputs "
            "(input_id, input_sha256, reflection_id, reflection_sha256, "
            "decision_state_id, workspace_root, candidate_id, candidate_revision, "
            "input_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                package.input_id,
                package.input_sha256,
                package.reflection_id,
                package.reflection_sha256,
                package.decision_state_id,
                package.workspace_root,
                package.candidate_id,
                package.candidate_revision,
                package.model_dump_json(),
                package.created_at,
            ),
        )


@pytest.mark.asyncio
async def test_fresh_promotion_input_rejects_negative_final_then_binds_fresh_authority(
    tmp_path: Path,
) -> None:
    sample, contract, parent, harness_store, _kernel, state = (
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
        adversarial,
        WorkerRegistryStore(tmp_path / ".naumi" / "workers.db"),
    )
    final_executor = _final(
        sample,
        harness_store,
        interventional,
        adversarial_cohort,
        matrix,
    )
    final = await final_executor.execute(contract_id=contract.contract_id)
    db_path = sample.receipt_store._db_path
    _persist_prior_dependency(db_path, state.package)
    reapproval_service = EvolutionRevalidationReapprovalAuthorityService(
        workspace_root=tmp_path,
        contract_service=sample.contract_service,
        final_store=final_executor.receipt_store,
        store=EvolutionRevalidationReapprovalAuthorityStore(db_path),
    )
    input_store = EvolutionRevalidationPromotionInputStore(db_path)
    input_service = EvolutionRevalidationPromotionInputService(
        workspace_root=tmp_path,
        contract_service=sample.contract_service,
        validation_plan_store=EvolutionRevalidationValidationPlanStore(db_path),
        final_store=final_executor.receipt_store,
        reapproval_service=reapproval_service,
        prior_input_store=_PriorInputStore(state.package),
        store=input_store,
    )

    with pytest.raises(EvolutionRevalidationPromotionInputError) as blocked:
        await input_service.issue(contract_id=contract.contract_id)
    assert blocked.value.code == "fresh_promotion_input_reapproval_blocked"

    eligible_attribution = _eligible_attribution(final.interventional.attribution)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "DELETE FROM evolution_failure_attributions WHERE comparison_id = ?",
            (eligible_attribution.comparison_id,),
        )
        db.execute(
            "DELETE FROM evolution_revalidation_final_evaluations "
            "WHERE contract_id = ?",
            (contract.contract_id,),
        )
    await EvolutionFailureAttributionStore(db_path).record(eligible_attribution)
    eligible_final = _eligible_final(final, eligible_attribution)
    await final_executor.receipt_store.record(eligible_final)

    issued = await input_service.issue(contract_id=contract.contract_id)
    repeated = await input_service.issue(contract_id=contract.contract_id)
    restored = await input_store.get(contract.contract_id)

    assert repeated == issued == restored
    assert issued.final_evaluation_id == eligible_final.receipt_id
    assert issued.reapproval_authority_id.startswith("evreapproval_")
    assert issued.prior_input_id == issued.prior_input.input_id
    assert issued.required_platforms == contract.required_platforms
    assert issued.patch_and_rollback_carried_forward
    assert not issued.prior_decision_reusable
    assert not issued.prior_approval_reusable
    assert not issued.prior_signature_reusable
    assert issued.approval_requirement_ready
    assert not issued.approval_decided
    assert not issued.promotion_authority


@pytest.mark.asyncio
async def test_fresh_promotion_input_store_rejects_missing_prior_dependency(
    tmp_path: Path,
) -> None:
    sample, contract, parent, harness_store, _kernel, state = (
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
        adversarial,
        WorkerRegistryStore(tmp_path / ".naumi" / "workers.db"),
    )
    final_executor = _final(
        sample,
        harness_store,
        interventional,
        adversarial_cohort,
        matrix,
    )
    final = await final_executor.execute(contract_id=contract.contract_id)
    db_path = sample.receipt_store._db_path
    _persist_prior_dependency(db_path, state.package)
    eligible_attribution = _eligible_attribution(final.interventional.attribution)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "DELETE FROM evolution_failure_attributions WHERE comparison_id = ?",
            (eligible_attribution.comparison_id,),
        )
        db.execute(
            "DELETE FROM evolution_revalidation_final_evaluations "
            "WHERE contract_id = ?",
            (contract.contract_id,),
        )
    await EvolutionFailureAttributionStore(db_path).record(eligible_attribution)
    await final_executor.receipt_store.record(
        _eligible_final(final, eligible_attribution)
    )
    service = EvolutionRevalidationPromotionInputService(
        workspace_root=tmp_path,
        contract_service=sample.contract_service,
        validation_plan_store=EvolutionRevalidationValidationPlanStore(db_path),
        final_store=final_executor.receipt_store,
        reapproval_service=EvolutionRevalidationReapprovalAuthorityService(
            workspace_root=tmp_path,
            contract_service=sample.contract_service,
            final_store=final_executor.receipt_store,
            store=EvolutionRevalidationReapprovalAuthorityStore(db_path),
        ),
        prior_input_store=_PriorInputStore(state.package),
        store=EvolutionRevalidationPromotionInputStore(db_path),
    )
    issued = await service.issue(contract_id=contract.contract_id)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "DELETE FROM evolution_revalidation_promotion_inputs WHERE contract_id = ?",
            (contract.contract_id,),
        )
        db.execute(
            "DELETE FROM evolution_promotion_package_inputs WHERE input_id = ?",
            (issued.prior_input_id,),
        )

    with pytest.raises(EvolutionRevalidationPromotionInputError) as blocked:
        await EvolutionRevalidationPromotionInputStore(db_path).record(issued)
    assert blocked.value.code == "fresh_promotion_input_dependency_mismatch"
