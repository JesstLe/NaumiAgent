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
from naumi_agent.evolution.revalidation_approval_requests import (
    EvolutionRevalidationApprovalRequestService,
    EvolutionRevalidationApprovalResponseStore,
)
from naumi_agent.evolution.revalidation_approval_signatures import (
    EVOLUTION_REVALIDATION_PROFESSIONAL_SIGNATURE_DOMAIN,
    EvolutionRevalidationApprovalSignatureError,
    EvolutionRevalidationApprovalSignatureService,
    EvolutionRevalidationApprovalSignatureStatus,
    EvolutionRevalidationApprovalSignatureStore,
)
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.harness.store import HarnessStore
from tests.unit.test_evolution_approval_principals import _AnsweringCallback
from tests.unit.test_evolution_revalidation_approval_requests import (
    _answering_callback,
)
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


async def _setup(root: Path):
    requirement_service, _store, contract, _plan, db_path, _prior = await _scenario(root)
    requirement = await requirement_service.issue(contract_id=contract.contract_id)
    clock = _Clock(datetime.fromisoformat(requirement.issued_at) + timedelta(seconds=20))
    harness_store = HarnessStore(root / ".naumi" / "harness.db")
    authority = DurableInteractionAuthorityClient(
        store=harness_store,
        workspace_root=root,
        owner_id="fresh-signature-test",
    )
    response_store = EvolutionRevalidationApprovalResponseStore(
        db_path,
        interaction_store=harness_store,
    )
    response = await EvolutionRevalidationApprovalRequestService(
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
    ).execute(
        contract_id=contract.contract_id,
        requirement_id=requirement.requirement_id,
        role="security_reviewer",
    )
    private, public = _keypair()
    principal_store = EvolutionApprovalPrincipalStore(
        db_path,
        interaction_store=harness_store,
    )
    principal_service = EvolutionApprovalPrincipalService(
        store=principal_store,
        interaction_store=harness_store,
        request_user_input=_AnsweringCallback(authority),
    )
    principal = await principal_service.register(
        workspace_root=root,
        principal_name="security.reviewer",
        roles=("security_reviewer",),
        public_key_base64=public,
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
    return (
        contract,
        requirement,
        response.receipt,
        principal.principal.principal,
        private,
        clock,
        signature_store,
        signature_service,
        principal_service,
    )


@pytest.mark.asyncio
async def test_fresh_professional_signature_is_real_durable_and_singleflight(
    tmp_path: Path,
) -> None:
    (
        contract,
        requirement,
        response,
        principal,
        private,
        _clock,
        store,
        service,
        _principal_service,
    ) = await _setup(tmp_path)

    views = await asyncio.gather(
        *(
            service.prepare(
                workspace_root=tmp_path,
                contract_id=contract.contract_id,
                requirement_id=requirement.requirement_id,
                role="security_reviewer",
                principal_id=principal.principal_id,
            )
            for _ in range(8)
        )
    )
    challenge = views[0].challenge
    assert all(item.challenge == challenge for item in views)
    assert views[0].status is EvolutionRevalidationApprovalSignatureStatus.PENDING
    assert challenge.payload.domain == EVOLUTION_REVALIDATION_PROFESSIONAL_SIGNATURE_DOMAIN
    assert challenge.payload.requirement_id == requirement.requirement_id
    assert challenge.payload.approval_response_id == response.receipt_id
    assert challenge.payload.principal_event_id == principal.event_id
    assert challenge.payload.key_generation == principal.key_generation
    assert challenge.fresh_signature_required
    assert not challenge.prior_signature_reused

    signature = base64.b64encode(private.sign(challenge.payload.canonical_bytes())).decode("ascii")
    receipt_views = await asyncio.gather(
        *(
            service.submit(
                workspace_root=tmp_path,
                contract_id=contract.contract_id,
                challenge_id=challenge.challenge_id,
                signature_base64=signature,
            )
            for _ in range(8)
        )
    )
    receipt_view = receipt_views[0]
    assert all(item == receipt_view for item in receipt_views)
    assert receipt_view.receipt.signature_verified
    assert receipt_view.receipt.fresh_signature_verified
    assert receipt_view.receipt.identity_binding_verified
    assert receipt_view.receipt.role_binding_verified
    assert receipt_view.receipt.counts_toward_role_quorum
    assert receipt_view.eligible_for_decision_aggregation
    assert not receipt_view.receipt.prior_signature_reused
    assert not receipt_view.receipt.decision_authority
    assert not receipt_view.receipt.promotion_authority
    assert await store.get_receipt(receipt_view.receipt.receipt_id) == receipt_view.receipt
    closed = await store.get_challenge(challenge.challenge_id, now=_clock.value)
    assert closed is not None
    assert closed.status is EvolutionRevalidationApprovalSignatureStatus.CONSUMED
    assert closed.closed_by_receipt_id == receipt_view.receipt.receipt_id


@pytest.mark.asyncio
async def test_wrong_key_and_rotated_principal_fail_closed(tmp_path: Path) -> None:
    (
        contract,
        requirement,
        _response,
        principal,
        _private,
        _clock,
        _store,
        service,
        principal_service,
    ) = await _setup(tmp_path)
    challenge = (
        await service.prepare(
            workspace_root=tmp_path,
            contract_id=contract.contract_id,
            requirement_id=requirement.requirement_id,
            role="security_reviewer",
            principal_id=principal.principal_id,
        )
    ).challenge
    wrong_private, rotated_public = _keypair()
    wrong_signature = base64.b64encode(
        wrong_private.sign(challenge.payload.canonical_bytes())
    ).decode("ascii")
    with pytest.raises(EvolutionRevalidationApprovalSignatureError) as wrong:
        await service.submit(
            workspace_root=tmp_path,
            contract_id=contract.contract_id,
            challenge_id=challenge.challenge_id,
            signature_base64=wrong_signature,
        )
    assert wrong.value.code == "fresh_signature_invalid"

    await principal_service.rotate_key(
        workspace_root=tmp_path,
        principal_id=principal.principal_id,
        public_key_base64=rotated_public,
    )
    with pytest.raises(EvolutionRevalidationApprovalSignatureError) as rotated:
        await service.submit(
            workspace_root=tmp_path,
            contract_id=contract.contract_id,
            challenge_id=challenge.challenge_id,
            signature_base64=wrong_signature,
        )
    assert rotated.value.code == "fresh_signature_authority_changed"
