from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlanError,
    EvolutionRevalidationRolloutPlanService,
    EvolutionRevalidationRolloutPlanStore,
    EvolutionRevalidationRolloutStageName,
)
from tests.unit.test_evolution_revalidation_approval_decisions import (
    _authorities,
    _keypair,
)


async def _approved(root: Path):
    (
        contract,
        requirement,
        _clock,
        _authority,
        response_service,
        principal_service,
        signature_service,
        decision_store,
        decision_service,
    ) = await _authorities(root)
    for step in requirement.steps:
        await response_service.execute(
            contract_id=contract.contract_id,
            requirement_id=requirement.requirement_id,
            role=step.role,
        )
    principals = []
    for step in requirement.steps:
        if not step.signature_required:
            continue
        private, public = _keypair()
        governed = await principal_service.register(
            workspace_root=root,
            principal_name=f"rollout.{step.role.value}",
            roles=(step.role,),
            public_key_base64=public,
        )
        principal = governed.principal.principal
        principals.append(principal)
        challenge = (
            await signature_service.prepare(
                workspace_root=root,
                contract_id=contract.contract_id,
                requirement_id=requirement.requirement_id,
                role=step.role,
                principal_id=principal.principal_id,
            )
        ).challenge
        await signature_service.submit(
            workspace_root=root,
            contract_id=contract.contract_id,
            challenge_id=challenge.challenge_id,
            signature_base64=base64.b64encode(
                private.sign(challenge.payload.canonical_bytes())
            ).decode(),
        )
    approved = await decision_service.execute(
        workspace_root=root,
        contract_id=contract.contract_id,
        requirement_id=requirement.requirement_id,
    )
    service = EvolutionRevalidationRolloutPlanService(
        workspace_root=root,
        decision_service=decision_service,
        promotion_input_service=decision_service.requirement_service.promotion_input_service,
        store=EvolutionRevalidationRolloutPlanStore(decision_store.db_path),
    )
    return contract, approved, principals, principal_service, service


@pytest.mark.asyncio
async def test_approved_decision_creates_immutable_non_executing_rollout_plan(
    tmp_path: Path,
) -> None:
    contract, approved, principals, principal_service, service = await _approved(tmp_path)

    services = tuple(
        EvolutionRevalidationRolloutPlanService(
            workspace_root=tmp_path,
            decision_service=service.decision_service,
            promotion_input_service=service.promotion_input_service,
            store=EvolutionRevalidationRolloutPlanStore(service.store.db_path),
        )
        for _ in range(8)
    )
    views = await asyncio.gather(*(
        candidate.issue(
            contract_id=contract.contract_id,
            decision_id=approved.receipt.decision_id,
        )
        for candidate in services
    ))
    view = views[0]

    assert all(item == view for item in views)
    assert tuple(item.name for item in view.plan.stages) == tuple(
        EvolutionRevalidationRolloutStageName
    )
    assert tuple(item.exposure_percent for item in view.plan.stages)[-1] == 100
    assert all(item.automatic_pause_on_breach for item in view.plan.stages)
    assert all(item.automatic_rollback_on_breach for item in view.plan.stages)
    assert view.plan.rollback_required
    assert view.plan.monitor_required
    assert view.plan.rollout_plan_authority
    assert not view.plan.stage_entry_authority
    assert not view.plan.execution_authority
    assert not view.execution_authorized
    assert view.next_stage is EvolutionRevalidationRolloutStageName.LOCAL_CANARY

    _new_private, new_public = _keypair()
    await principal_service.rotate_key(
        workspace_root=tmp_path,
        principal_id=principals[0].principal_id,
        public_key_base64=new_public,
    )
    stale = await service.inspect(plan_id=view.plan.plan_id)
    assert not stale.decision_current
    assert not stale.current_rollout_eligible
    assert stale.next_stage is None
    assert not stale.execution_authorized


@pytest.mark.asyncio
async def test_pending_decision_cannot_create_rollout_plan(tmp_path: Path) -> None:
    (
        contract,
        requirement,
        _clock,
        _authority,
        response_service,
        _principal_service,
        _signature_service,
        decision_store,
        decision_service,
    ) = await _authorities(tmp_path)
    await response_service.execute(
        contract_id=contract.contract_id,
        requirement_id=requirement.requirement_id,
        role=requirement.steps[0].role,
    )
    pending = await decision_service.execute(
        workspace_root=tmp_path,
        contract_id=contract.contract_id,
        requirement_id=requirement.requirement_id,
    )
    service = EvolutionRevalidationRolloutPlanService(
        workspace_root=tmp_path,
        decision_service=decision_service,
        promotion_input_service=decision_service.requirement_service.promotion_input_service,
        store=EvolutionRevalidationRolloutPlanStore(decision_store.db_path),
    )

    with pytest.raises(EvolutionRevalidationRolloutPlanError) as blocked:
        await service.issue(
            contract_id=contract.contract_id,
            decision_id=pending.receipt.decision_id,
        )

    assert blocked.value.code == "rollout_plan_decision_not_eligible"
    assert await service.store.get_by_decision(pending.receipt.decision_id) is None
