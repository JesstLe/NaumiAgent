from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.evolution.approval_principals import (
    EvolutionApprovalPrincipalService,
    EvolutionApprovalPrincipalStore,
)
from naumi_agent.evolution.revalidation_approval_decisions import (
    EvolutionRevalidationApprovalDecisionService,
    EvolutionRevalidationApprovalDecisionStatus,
    EvolutionRevalidationApprovalDecisionStore,
    EvolutionRevalidationApprovalRoleOutcome,
)
from naumi_agent.evolution.revalidation_approval_requests import (
    EvolutionRevalidationApprovalRequestService,
    EvolutionRevalidationApprovalResponseStore,
)
from naumi_agent.evolution.revalidation_approval_signatures import (
    EvolutionRevalidationApprovalSignatureService,
    EvolutionRevalidationApprovalSignatureStore,
)
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.harness.store import HarnessStore
from tests.unit.test_evolution_approval_principals import _AnsweringCallback
from tests.unit.test_evolution_revalidation_approval_requests import _answering_callback
from tests.unit.test_evolution_revalidation_approval_requirements import _scenario


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(public).decode("ascii")


async def _authorities(root: Path):
    requirement_service, _store, contract, _plan, db_path, _prior = await _scenario(root)
    requirement = await requirement_service.issue(contract_id=contract.contract_id)
    clock = _Clock(datetime.fromisoformat(requirement.issued_at) + timedelta(seconds=20))
    harness_store = HarnessStore(root / ".naumi" / "harness.db")
    authority = DurableInteractionAuthorityClient(
        store=harness_store,
        workspace_root=root,
        owner_id="fresh-decision-test",
    )
    response_store = EvolutionRevalidationApprovalResponseStore(
        db_path,
        interaction_store=harness_store,
    )
    response_service = EvolutionRevalidationApprovalRequestService(
        requirement_service=requirement_service,
        interaction_store=harness_store,
        response_store=response_store,
        request_user_input=_answering_callback(
            authority=authority,
            now=clock.value,
            answer_value="approve",
            observations=[],
        ),
        clock=clock,
    )
    principal_service = EvolutionApprovalPrincipalService(
        store=EvolutionApprovalPrincipalStore(
            db_path,
            interaction_store=harness_store,
        ),
        interaction_store=harness_store,
        request_user_input=_AnsweringCallback(authority),
    )
    signature_store = EvolutionRevalidationApprovalSignatureStore(db_path)
    signature_service = EvolutionRevalidationApprovalSignatureService(
        response_store=response_store,
        requirement_service=requirement_service,
        principal_service=principal_service,
        signature_store=signature_store,
        clock=clock,
        challenge_ttl_seconds=300,
    )
    decision_store = EvolutionRevalidationApprovalDecisionStore(db_path)
    decision_service = EvolutionRevalidationApprovalDecisionService(
        requirement_service=requirement_service,
        response_store=response_store,
        signature_store=signature_store,
        signature_service=signature_service,
        decision_store=decision_store,
        clock=clock,
    )
    return (
        contract,
        requirement,
        clock,
        authority,
        response_service,
        principal_service,
        signature_service,
        decision_store,
        decision_service,
    )


@pytest.mark.asyncio
async def test_fresh_decision_chains_pending_to_approved_singleflight(
    tmp_path: Path,
) -> None:
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
    ) = await _authorities(tmp_path)
    responses = {}
    governed_principals = []
    for step in requirement.steps:
        view = await response_service.execute(
            contract_id=contract.contract_id,
            requirement_id=requirement.requirement_id,
            role=step.role,
        )
        responses[step.role] = view.receipt

    pending = await decision_service.execute(
        workspace_root=tmp_path,
        contract_id=contract.contract_id,
        requirement_id=requirement.requirement_id,
    )
    assert pending.receipt.sequence == 1
    assert pending.receipt.status is EvolutionRevalidationApprovalDecisionStatus.PENDING
    assert pending.receipt.approvals_collected == 1
    assert pending.receipt.signatures_collected == 0
    assert not pending.receipt.staged_rollout_eligible
    assert not pending.receipt.promotion_authority

    for step in requirement.steps:
        if not step.signature_required:
            continue
        private, public = _keypair()
        governed = await principal_service.register(
            workspace_root=tmp_path,
            principal_name=f"fresh.{step.role.value}",
            roles=(step.role,),
            public_key_base64=public,
        )
        principal = governed.principal.principal
        governed_principals.append(principal)
        challenge = (
            await signature_service.prepare(
                workspace_root=tmp_path,
                contract_id=contract.contract_id,
                requirement_id=requirement.requirement_id,
                role=step.role,
                principal_id=principal.principal_id,
            )
        ).challenge
        signature = base64.b64encode(private.sign(challenge.payload.canonical_bytes())).decode(
            "ascii"
        )
        await signature_service.submit(
            workspace_root=tmp_path,
            contract_id=contract.contract_id,
            challenge_id=challenge.challenge_id,
            signature_base64=signature,
        )

    approved_views = await asyncio.gather(
        *(
            decision_service.execute(
                workspace_root=tmp_path,
                contract_id=contract.contract_id,
                requirement_id=requirement.requirement_id,
            )
            for _ in range(8)
        )
    )
    approved = approved_views[0]
    assert all(item == approved for item in approved_views)
    assert approved.receipt.sequence == 2
    assert approved.receipt.previous_decision_id == pending.receipt.decision_id
    assert approved.receipt.status is EvolutionRevalidationApprovalDecisionStatus.APPROVED
    assert approved.receipt.approvals_collected == requirement.minimum_approvals
    assert approved.receipt.signatures_collected == requirement.minimum_signatures
    assert not approved.receipt.missing_roles
    assert not approved.receipt.blocking_gates
    assert approved.receipt.final_quorum_reached
    assert approved.receipt.staged_rollout_eligible
    assert approved.current_staged_rollout_eligible
    assert await decision_store.get(approved.receipt.decision_id) == approved.receipt

    _rotated_private, rotated_public = _keypair()
    await principal_service.rotate_key(
        workspace_root=tmp_path,
        principal_id=governed_principals[0].principal_id,
        public_key_base64=rotated_public,
    )
    stale = await decision_service.inspect(
        workspace_root=tmp_path,
        contract_id=contract.contract_id,
        decision_id=approved.receipt.decision_id,
    )
    assert not stale.source_current
    assert stale.current_status is EvolutionRevalidationApprovalDecisionStatus.STALE
    assert not stale.current_staged_rollout_eligible


@pytest.mark.asyncio
async def test_explicit_reject_wins_over_missing_roles(tmp_path: Path) -> None:
    (
        contract,
        requirement,
        clock,
        authority,
        _response_service,
        _principal_service,
        _signature_service,
        _decision_store,
        decision_service,
    ) = await _authorities(tmp_path)
    harness_store = HarnessStore(tmp_path / ".naumi" / "harness.db")
    response_store = EvolutionRevalidationApprovalResponseStore(
        tmp_path / ".naumi" / "state.db",
        interaction_store=harness_store,
    )
    reject_service = EvolutionRevalidationApprovalRequestService(
        requirement_service=decision_service.requirement_service,
        interaction_store=harness_store,
        response_store=response_store,
        request_user_input=_answering_callback(
            authority=authority,
            now=clock.value,
            answer_value="reject",
            observations=[],
        ),
        clock=clock,
    )
    role = requirement.steps[1].role
    await reject_service.execute(
        contract_id=contract.contract_id,
        requirement_id=requirement.requirement_id,
        role=role,
    )

    rejected = await decision_service.execute(
        workspace_root=tmp_path,
        contract_id=contract.contract_id,
        requirement_id=requirement.requirement_id,
    )

    assert rejected.receipt.status is EvolutionRevalidationApprovalDecisionStatus.REJECTED
    role_result = next(item for item in rejected.receipt.role_decisions if item.role is role)
    assert role_result.outcome is EvolutionRevalidationApprovalRoleOutcome.REJECTED
    assert rejected.receipt.missing_roles
    assert not rejected.receipt.staged_rollout_eligible
