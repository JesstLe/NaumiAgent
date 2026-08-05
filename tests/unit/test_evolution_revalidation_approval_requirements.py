from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.approval_requirements import EvolutionPromotionApprovalRole
from naumi_agent.evolution.failure_attribution import EvolutionFailureAttributionStore
from naumi_agent.evolution.revalidation_approval_requirements import (
    EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_DOMAIN,
    EvolutionRevalidationApprovalReason,
    EvolutionRevalidationApprovalRequirementError,
    EvolutionRevalidationApprovalRequirementService,
    EvolutionRevalidationApprovalRequirementStore,
)
from naumi_agent.evolution.revalidation_outcomes import (
    EvolutionRevalidationOutcomeStore,
)
from naumi_agent.evolution.revalidation_promotion_inputs import (
    EvolutionRevalidationPromotionInputService,
    EvolutionRevalidationPromotionInputStore,
)
from naumi_agent.evolution.revalidation_reapproval_authorities import (
    EvolutionRevalidationReapprovalAuthorityService,
    EvolutionRevalidationReapprovalAuthorityStore,
)
from naumi_agent.evolution.revalidation_requests import (
    EvolutionRevalidationRequestStore,
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
from tests.unit.test_evolution_revalidation_promotion_inputs import (
    _persist_prior_dependency,
    _PriorInputStore,
)
from tests.unit.test_evolution_revalidation_reapproval_authorities import (
    _eligible_attribution,
    _eligible_final,
)


async def _scenario(tmp_path: Path):
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
    request = state.request
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS evolution_revalidation_requests ("
            "request_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, "
            "decision_id TEXT NOT NULL UNIQUE, decision_sha256 TEXT NOT NULL, "
            "package_id TEXT NOT NULL, package_sha256 TEXT NOT NULL, "
            "workspace_root TEXT NOT NULL, target_branch TEXT NOT NULL, "
            "target_head TEXT NOT NULL, request_json TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR REPLACE INTO evolution_revalidation_requests "
            "(request_id, request_sha256, decision_id, decision_sha256, "
            "package_id, package_sha256, workspace_root, target_branch, "
            "target_head, request_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.request_id,
                request.request_sha256,
                request.decision_id,
                request.decision_sha256,
                request.package_id,
                request.package_sha256,
                request.workspace_root,
                request.target_branch,
                request.target_head,
                request.model_dump_json(),
            ),
        )
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
    reapproval_store = EvolutionRevalidationReapprovalAuthorityStore(db_path)
    input_service = EvolutionRevalidationPromotionInputService(
        workspace_root=tmp_path,
        contract_service=sample.contract_service,
        validation_plan_store=EvolutionRevalidationValidationPlanStore(db_path),
        final_store=final_executor.receipt_store,
        reapproval_service=EvolutionRevalidationReapprovalAuthorityService(
            workspace_root=tmp_path,
            contract_service=sample.contract_service,
            final_store=final_executor.receipt_store,
            store=reapproval_store,
        ),
        prior_input_store=_PriorInputStore(state.package),
        store=EvolutionRevalidationPromotionInputStore(db_path),
    )
    store = EvolutionRevalidationApprovalRequirementStore(db_path)
    service = EvolutionRevalidationApprovalRequirementService(
        workspace_root=tmp_path,
        promotion_input_service=input_service,
        contract_service=sample.contract_service,
        validation_plan_store=EvolutionRevalidationValidationPlanStore(db_path),
        outcome_store=EvolutionRevalidationOutcomeStore(db_path),
        request_store=EvolutionRevalidationRequestStore(db_path),
        reapproval_store=reapproval_store,
        store=store,
    )
    plan = await EvolutionRevalidationValidationPlanStore(db_path).get(
        contract.validation_plan_id
    )
    assert plan is not None
    return service, store, contract, plan, db_path, state.package


@pytest.mark.asyncio
async def test_fresh_requirement_binds_current_target_and_requires_new_role_chain(
    tmp_path: Path,
) -> None:
    service, store, contract, plan, _db_path, prior = await _scenario(tmp_path)

    issued = await service.issue(contract_id=contract.contract_id)
    repeated = await service.issue(contract_id=contract.contract_id)
    restored = await store.get(issued.promotion_input_id)

    assert issued == repeated == restored
    assert issued.target_branch == "main"
    assert issued.target_head == plan.red_revision
    assert issued.target_tree_sha256 == plan.red_tree_sha256
    assert issued.patch_manifest_sha256 == prior.patch.manifest_sha256
    assert issued.migration_assessment_sha256 == prior.migration.assessment_sha256
    assert issued.rollback_plan_sha256 == prior.rollback.plan_sha256
    assert issued.required_roles == (
        EvolutionPromotionApprovalRole.USER,
        EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,
        EvolutionPromotionApprovalRole.SECURITY_REVIEWER,
        EvolutionPromotionApprovalRole.DATA_OWNER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )
    assert issued.signature_required_roles == issued.required_roles[1:]
    assert issued.minimum_approvals == 5
    assert issued.minimum_signatures == 4
    assert all(step.fresh_interaction_required for step in issued.steps)
    assert all(not step.prior_signature_reusable for step in issued.steps)
    data_owner = next(
        step
        for step in issued.steps
        if step.role is EvolutionPromotionApprovalRole.DATA_OWNER
    )
    assert EvolutionRevalidationApprovalReason.MIGRATION_REVIEW in data_owner.reasons
    assert EvolutionRevalidationApprovalReason.DATA_BACKUP in data_owner.reasons
    assert issued.signable_payload_sha256
    assert EVOLUTION_REVALIDATION_APPROVAL_SIGNATURE_DOMAIN.endswith("review.v1")
    assert issued.approval_request_ready
    assert not issued.interaction_created
    assert not issued.approval_decided
    assert not issued.signatures_collected
    assert not issued.promotion_authority


@pytest.mark.asyncio
async def test_fresh_requirement_store_rejects_removed_current_target_dependency(
    tmp_path: Path,
) -> None:
    service, store, contract, _plan, db_path, _prior = await _scenario(tmp_path)
    issued = await service.issue(contract_id=contract.contract_id)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "DELETE FROM evolution_revalidation_approval_requirements "
            "WHERE promotion_input_id = ?",
            (issued.promotion_input_id,),
        )
        db.execute(
            "DELETE FROM evolution_revalidation_requests WHERE request_id = ?",
            (issued.request_id,),
        )

    with pytest.raises(EvolutionRevalidationApprovalRequirementError) as blocked:
        await store.record(issued)
    assert blocked.value.code == "fresh_approval_requirement_dependency_mismatch"
