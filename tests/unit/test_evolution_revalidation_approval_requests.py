from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.approval_requests import (
    EvolutionPromotionApprovalIdentityAssurance,
    EvolutionPromotionApprovalResponse,
)
from naumi_agent.evolution.approval_requirements import EvolutionPromotionApprovalRole
from naumi_agent.evolution.revalidation_approval_requests import (
    EvolutionRevalidationApprovalRequestError,
    EvolutionRevalidationApprovalRequestService,
    EvolutionRevalidationApprovalResponseStore,
)
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.harness.store import HarnessStore
from naumi_agent.user_interaction import normalize_interaction_request
from tests.unit.test_evolution_revalidation_approval_requirements import (
    _scenario,
)


def _answering_callback(
    *,
    authority: DurableInteractionAuthorityClient,
    now: datetime,
    answer_value: str,
    observations: list[tuple[str, str]],
):
    async def answer(payload: dict[str, object]) -> dict[str, str]:
        request = normalize_interaction_request(payload)
        observations.append((str(payload["_interaction_id"]), request.header))
        record = await authority.create(
            request=request,
            interaction_id=str(payload["_interaction_id"]),
            subject_kind=str(payload["_durable_subject_kind"]),
            subject_id=str(payload["_durable_subject_id"]),
            session_id="fresh-approval-session",
            agent_name="main",
            now=(now + timedelta(seconds=1)).isoformat(),
        )
        record, response = await authority.answer(
            record=record,
            response={"kind": "option", "value": answer_value},
            now=(now + timedelta(seconds=2)).isoformat(),
        )
        assert record.state == "answered"
        return response

    return answer


@pytest.mark.asyncio
async def test_fresh_user_response_is_singleflight_and_counts_only_new_interaction(
    tmp_path: Path,
) -> None:
    requirement_service, _store, contract, _plan, db_path, _prior = await _scenario(
        tmp_path
    )
    requirement = await requirement_service.issue(contract_id=contract.contract_id)
    now = datetime.fromisoformat(requirement.issued_at) + timedelta(seconds=10)
    harness_store = HarnessStore(tmp_path / ".naumi" / "harness.db")
    authority = DurableInteractionAuthorityClient(
        store=harness_store,
        workspace_root=tmp_path,
        owner_id="fresh-approval-test",
    )
    response_store = EvolutionRevalidationApprovalResponseStore(
        db_path,
        interaction_store=harness_store,
    )
    observations: list[tuple[str, str]] = []
    service = EvolutionRevalidationApprovalRequestService(
        requirement_service=requirement_service,
        interaction_store=harness_store,
        response_store=response_store,
        request_user_input=_answering_callback(
            authority=authority,
            now=now,
            answer_value="approve",
            observations=observations,
        ),
        clock=lambda: now,
    )

    views = await asyncio.gather(*(
        service.execute(
            contract_id=contract.contract_id,
            requirement_id=requirement.requirement_id,
            role="user",
        )
        for _ in range(8)
    ))
    view = views[0]
    receipt = view.receipt

    assert all(item == view for item in views)
    assert len(observations) == 1
    assert observations[0][0].startswith("ask-evreapproval-")
    assert observations[0][1] == "Fresh Evolution 审批 · user"
    assert receipt.response is EvolutionPromotionApprovalResponse.APPROVE
    assert receipt.role is EvolutionPromotionApprovalRole.USER
    assert (
        receipt.identity_assurance
        is EvolutionPromotionApprovalIdentityAssurance.LOCAL_SESSION_USER
    )
    assert receipt.role_binding_verified
    assert receipt.counts_toward_role_quorum
    assert view.eligible_for_future_aggregation
    assert not view.eligible_for_fresh_signature
    assert not receipt.signature_entry.required
    assert not receipt.prior_response_reused
    assert not receipt.prior_signature_reused
    assert not receipt.promotion_authority
    assert not receipt.interaction.allow_custom
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_revalidation_approval_responses"
        ).fetchone() == (1,)
    with sqlite3.connect(tmp_path / ".naumi" / "harness.db") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM harness_interactions WHERE subject_id = ?",
            (requirement.requirement_id,),
        ).fetchone() == (1,)
        db.execute(
            "DELETE FROM harness_interactions WHERE interaction_id = ?",
            (receipt.interaction.interaction_id,),
        )
    with pytest.raises(EvolutionRevalidationApprovalRequestError) as missing:
        await response_store.get_by_requirement_role(
            requirement.requirement_id,
            "user",
        )
    assert missing.value.code == "fresh_approval_response_store_corrupt"


@pytest.mark.asyncio
async def test_fresh_professional_response_requires_new_signature_and_identity(
    tmp_path: Path,
) -> None:
    requirement_service, _store, contract, _plan, db_path, _prior = await _scenario(
        tmp_path
    )
    requirement = await requirement_service.issue(contract_id=contract.contract_id)
    now = datetime.fromisoformat(requirement.issued_at) + timedelta(seconds=10)
    harness_store = HarnessStore(tmp_path / ".naumi" / "harness.db")
    authority = DurableInteractionAuthorityClient(
        store=harness_store,
        workspace_root=tmp_path,
        owner_id="fresh-professional-test",
    )
    response_store = EvolutionRevalidationApprovalResponseStore(
        db_path,
        interaction_store=harness_store,
    )
    observations: list[tuple[str, str]] = []

    view = await EvolutionRevalidationApprovalRequestService(
        requirement_service=requirement_service,
        interaction_store=harness_store,
        response_store=response_store,
        request_user_input=_answering_callback(
            authority=authority,
            now=now,
            answer_value="approve",
            observations=observations,
        ),
        clock=lambda: now,
    ).execute(
        contract_id=contract.contract_id,
        requirement_id=requirement.requirement_id,
        role="security_reviewer",
    )

    receipt = view.receipt
    assert receipt.role is EvolutionPromotionApprovalRole.SECURITY_REVIEWER
    assert (
        receipt.identity_assurance
        is EvolutionPromotionApprovalIdentityAssurance.UNVERIFIED_ROLE_CLAIM
    )
    assert not receipt.role_binding_verified
    assert not receipt.counts_toward_role_quorum
    assert not view.eligible_for_future_aggregation
    assert view.eligible_for_fresh_signature
    assert receipt.signature_entry.required
    assert not receipt.signature_entry.signature_collected
    assert receipt.signature_entry.signable_payload_sha256 == (
        requirement.signable_payload_sha256
    )


@pytest.mark.asyncio
async def test_expired_fresh_requirement_never_creates_interaction(
    tmp_path: Path,
) -> None:
    requirement_service, _store, contract, _plan, db_path, _prior = await _scenario(
        tmp_path
    )
    requirement = await requirement_service.issue(contract_id=contract.contract_id)
    harness_store = HarnessStore(tmp_path / ".naumi" / "harness.db")

    async def must_not_ask(_payload):
        raise AssertionError("expired requirement must not ask")

    service = EvolutionRevalidationApprovalRequestService(
        requirement_service=requirement_service,
        interaction_store=harness_store,
        response_store=EvolutionRevalidationApprovalResponseStore(
            db_path,
            interaction_store=harness_store,
        ),
        request_user_input=must_not_ask,
        clock=lambda: datetime.fromisoformat(requirement.expires_at),
    )

    with pytest.raises(EvolutionRevalidationApprovalRequestError) as expired:
        await service.execute(
            contract_id=contract.contract_id,
            requirement_id=requirement.requirement_id,
            role="user",
        )
    assert expired.value.code == "fresh_approval_request_requirement_expired"
    assert await harness_store.list_interactions(
        workspace_root=tmp_path,
        subject_kind="tool",
        subject_ids=(requirement.requirement_id,),
        limit=10,
    ) == ()
