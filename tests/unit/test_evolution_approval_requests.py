from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.completer import COMMANDS_META
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.approval_requests import (
    EvolutionPromotionApprovalIdentityAssurance,
    EvolutionPromotionApprovalRequestError,
    EvolutionPromotionApprovalRequestService,
    EvolutionPromotionApprovalResponse,
    EvolutionPromotionApprovalResponseBuilder,
    EvolutionPromotionApprovalResponseReceipt,
    EvolutionPromotionApprovalResponseStore,
    _approval_interaction_request,
    _approval_request_id,
    _sha256_payload,
    render_evolution_promotion_approval_response,
)
from naumi_agent.evolution.approval_requirements import (
    EvolutionPromotionApprovalRequirementExecutor,
    EvolutionPromotionApprovalRequirementStore,
    EvolutionPromotionApprovalRole,
)
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import (
    TOOL_PERMISSIONS,
    PermissionMode,
    PermissionRiskLevel,
)
from naumi_agent.tools.evolution_review import EvolutionPromotionApprovalRequestTool
from naumi_agent.user_interaction import normalize_interaction_request
from tests.unit.test_evolution_approval_requirements import _authority_chain, _Clock


async def _setup(
    root: Path,
    *,
    now: datetime | None = None,
):
    timestamp = now or datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
    _reflection_store, package_executor, package_view = await _authority_chain(root)
    db_path = root / ".naumi" / "state.db"
    clock = _Clock(timestamp)
    requirement_executor = EvolutionPromotionApprovalRequirementExecutor(
        package_executor=package_executor,
        requirement_store=EvolutionPromotionApprovalRequirementStore(db_path),
        clock=clock,
    )
    requirement_view = await requirement_executor.execute(
        workspace_root=root,
        package_id=package_view.package.package_id,
    )
    harness_store = HarnessStore(db_path)
    authority = DurableInteractionAuthorityClient(
        store=harness_store,
        workspace_root=root,
        owner_id="approval-test-owner",
    )
    response_store = EvolutionPromotionApprovalResponseStore(
        db_path,
        interaction_store=harness_store,
    )
    return (
        clock,
        requirement_executor,
        requirement_view,
        harness_store,
        authority,
        response_store,
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
            session_id="approval-session",
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
async def test_user_approval_request_is_fenced_singleflight_and_cross_surface(
    tmp_path: Path,
) -> None:
    (
        clock,
        requirement_executor,
        requirement_view,
        harness_store,
        authority,
        response_store,
    ) = await _setup(tmp_path)
    observations: list[tuple[str, str]] = []
    service = EvolutionPromotionApprovalRequestService(
        requirement_executor=requirement_executor,
        interaction_store=harness_store,
        response_store=response_store,
        request_user_input=_answering_callback(
            authority=authority,
            now=clock.value,
            answer_value="approve",
            observations=observations,
        ),
        clock=clock,
    )

    views = await asyncio.gather(
        *(
            service.execute(
                workspace_root=tmp_path,
                requirement_id=requirement_view.requirement.requirement_id,
                role="user",
            )
            for _ in range(8)
        )
    )
    view = views[0]
    receipt = view.receipt
    assert all(item == view for item in views)
    assert len(observations) == 1
    assert observations[0][1] == "Evolution 审批 · user"
    assert receipt.response is EvolutionPromotionApprovalResponse.APPROVE
    assert receipt.role is EvolutionPromotionApprovalRole.USER
    assert (
        receipt.identity_assurance is EvolutionPromotionApprovalIdentityAssurance.LOCAL_SESSION_USER
    )
    assert receipt.role_binding_verified
    assert receipt.counts_toward_role_quorum
    assert view.eligible_for_future_aggregation
    assert not receipt.signature_entry.required
    assert not receipt.overall_approval_decided
    assert not receipt.final_quorum_reached
    assert not receipt.promotion_authority
    assert not receipt.git_write_executed
    assert not receipt.promotion_executed
    assert not receipt.interaction.allow_custom
    assert receipt.interaction.state == "answered"
    assert receipt.interaction.subject_id == requirement_view.requirement.requirement_id
    assert (
        await response_store.get_by_requirement_role(
            requirement_view.requirement.requirement_id,
            "user",
        )
        == receipt
    )
    with sqlite3.connect(tmp_path / ".naumi" / "state.db") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_promotion_approval_responses"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM harness_interactions WHERE subject_id = ?",
            (requirement_view.requirement.requirement_id,),
        ).fetchone() == (1,)

    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_promotion_approval_request_service=service,
    )
    tool_output = await EvolutionPromotionApprovalRequestTool(engine).execute(
        requirement_view.requirement.requirement_id,
        "user",
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution approval-request {requirement_view.requirement.requirement_id} user",
    )
    assert tool_output == render_evolution_promotion_approval_response(view)
    assert receipt.receipt_id in slash_output
    assert "这不是最终 Promotion 审批" in slash_output


@pytest.mark.asyncio
async def test_specialist_response_requires_identity_and_signature_receipt(
    tmp_path: Path,
) -> None:
    (
        clock,
        requirement_executor,
        requirement_view,
        harness_store,
        authority,
        response_store,
    ) = await _setup(tmp_path)
    observations: list[tuple[str, str]] = []
    view = await EvolutionPromotionApprovalRequestService(
        requirement_executor=requirement_executor,
        interaction_store=harness_store,
        response_store=response_store,
        request_user_input=_answering_callback(
            authority=authority,
            now=clock.value,
            answer_value="approve",
            observations=observations,
        ),
        clock=clock,
    ).execute(
        workspace_root=tmp_path,
        requirement_id=requirement_view.requirement.requirement_id,
        role="data_owner",
    )

    receipt = view.receipt
    assert receipt.role is EvolutionPromotionApprovalRole.DATA_OWNER
    assert (
        receipt.identity_assurance
        is EvolutionPromotionApprovalIdentityAssurance.UNVERIFIED_ROLE_CLAIM
    )
    assert not receipt.role_binding_verified
    assert not receipt.counts_toward_role_quorum
    assert not view.eligible_for_future_aggregation
    assert receipt.signature_entry.required
    assert not receipt.signature_entry.signature_collected
    assert receipt.signature_entry.signable_payload_sha256 == (
        requirement_view.requirement.signable_payload_sha256
    )
    assert "角色身份未验证" in render_evolution_promotion_approval_response(view)


@pytest.mark.asyncio
async def test_pending_is_reused_cancelled_retries_and_stale_view_fails_closed(
    tmp_path: Path,
) -> None:
    (
        clock,
        requirement_executor,
        requirement_view,
        harness_store,
        authority,
        response_store,
    ) = await _setup(tmp_path)
    requirement = requirement_view.requirement
    step = requirement.steps[0]
    request = _approval_interaction_request(
        requirement,
        step,
        timeout_seconds=3_600,
    )
    suffix = requirement.requirement_id.removeprefix("evapprovalreq_")
    pending = await authority.create(
        request=request,
        interaction_id=f"ask-evapproval-{suffix}-user-1",
        subject_kind="tool",
        subject_id=requirement.requirement_id,
        session_id="pending-session",
        agent_name="main",
        now=clock.value.isoformat(),
    )

    async def must_not_ask(_payload: dict[str, object]) -> dict[str, str]:
        raise AssertionError("已有 pending authority 时不得重复显示")

    service = EvolutionPromotionApprovalRequestService(
        requirement_executor=requirement_executor,
        interaction_store=harness_store,
        response_store=response_store,
        request_user_input=must_not_ask,
        clock=clock,
    )
    with pytest.raises(EvolutionPromotionApprovalRequestError) as pending_error:
        await service.execute(
            workspace_root=tmp_path,
            requirement_id=requirement.requirement_id,
            role="user",
        )
    assert pending_error.value.code == "approval_request_interaction_pending"
    assert pending.interaction_id in str(pending_error.value)

    await authority.cancel(
        record=pending,
        now=(clock.value + timedelta(seconds=1)).isoformat(),
    )
    observations: list[tuple[str, str]] = []
    retry_service = EvolutionPromotionApprovalRequestService(
        requirement_executor=requirement_executor,
        interaction_store=harness_store,
        response_store=response_store,
        request_user_input=_answering_callback(
            authority=authority,
            now=clock.value + timedelta(seconds=2),
            answer_value="request_changes",
            observations=observations,
        ),
        clock=lambda: clock.value + timedelta(seconds=2),
    )
    changed = await retry_service.execute(
        workspace_root=tmp_path,
        requirement_id=requirement.requirement_id,
        role="user",
    )
    assert observations[0][0].endswith("-user-2")
    assert changed.receipt.response is EvolutionPromotionApprovalResponse.REQUEST_CHANGES
    assert not changed.receipt.counts_toward_role_quorum
    assert not changed.eligible_for_future_aggregation

    (tmp_path / "target.txt").write_text("moved\n", encoding="utf-8")
    from tests.unit.test_evolution_promotion_packages import _git

    _git(tmp_path, "add", "target.txt")
    _git(tmp_path, "commit", "-m", "move approval target")
    stale = await retry_service.execute(
        workspace_root=tmp_path,
        requirement_id=requirement.requirement_id,
        role="user",
    )
    assert not stale.target_current
    assert not stale.eligible_for_future_aggregation


@pytest.mark.asyncio
async def test_expiry_invalid_role_custom_answer_and_store_tampering_fail_closed(
    tmp_path: Path,
) -> None:
    (
        clock,
        requirement_executor,
        requirement_view,
        harness_store,
        authority,
        response_store,
    ) = await _setup(tmp_path)
    requirement = requirement_view.requirement

    async def must_not_ask(_payload: dict[str, object]) -> dict[str, str]:
        raise AssertionError("失效 Requirement 不得显示问题")

    expired_service = EvolutionPromotionApprovalRequestService(
        requirement_executor=requirement_executor,
        interaction_store=harness_store,
        response_store=response_store,
        request_user_input=must_not_ask,
        clock=lambda: datetime.fromisoformat(requirement.expires_at),
    )
    requirement_executor._clock = lambda: datetime.fromisoformat(requirement.expires_at)
    with pytest.raises(EvolutionPromotionApprovalRequestError) as expired:
        await expired_service.execute(
            workspace_root=tmp_path,
            requirement_id=requirement.requirement_id,
            role="user",
        )
    assert expired.value.code == "approval_request_requirement_ineligible"
    with pytest.raises(EvolutionPromotionApprovalRequestError) as invalid_role:
        await expired_service.execute(
            workspace_root=tmp_path,
            requirement_id=requirement.requirement_id,
            role="model",
        )
    assert invalid_role.value.code == "approval_request_role_invalid"

    requirement_executor._clock = clock
    step = requirement.steps[0]
    request = _approval_interaction_request(
        requirement,
        step,
        timeout_seconds=3_600,
    )
    suffix = requirement.requirement_id.removeprefix("evapprovalreq_")
    record = await authority.create(
        request=request,
        interaction_id=f"ask-evapproval-{suffix}-user-1",
        subject_kind="tool",
        subject_id=requirement.requirement_id,
        session_id="custom-session",
        agent_name="main",
        now=clock.value.isoformat(),
    )
    with pytest.raises(ValueError, match="不允许自定义输入"):
        await authority.answer(
            record=record,
            response={"kind": "custom", "custom_text": "批准"},
            now=(clock.value + timedelta(seconds=1)).isoformat(),
        )
    answered, _ = await authority.answer(
        record=record,
        response={"kind": "option", "value": "reject"},
        now=(clock.value + timedelta(seconds=1)).isoformat(),
    )
    forged_actor_payload = answered.model_dump(mode="json")
    forged_actor_payload["answered_by"] = "agent"
    forged_actor = type(answered).model_validate_json(json.dumps(forged_actor_payload))
    with pytest.raises(EvolutionPromotionApprovalRequestError):
        EvolutionPromotionApprovalResponseBuilder().build(
            requirement=requirement,
            step=step,
            interaction=forged_actor,
        )
    receipt = EvolutionPromotionApprovalResponseBuilder().build(
        requirement=requirement,
        step=step,
        interaction=answered,
    )
    stored = await response_store.record(receipt, requirement=requirement)
    tampered = stored.model_dump(mode="json")
    tampered["promotion_authority"] = True
    payload = {
        key: value for key, value in tampered.items() if key not in {"receipt_id", "receipt_sha256"}
    }
    digest = _sha256_payload(payload)
    tampered["receipt_id"] = f"evapprovalresp_{digest[:24]}"
    tampered["receipt_sha256"] = digest
    with pytest.raises(ValueError):
        EvolutionPromotionApprovalResponseReceipt.model_validate(tampered)

    forged_request = stored.model_dump(mode="json")
    forged_request["interaction_request_sha256"] = "f" * 64
    forged_request["approval_request_id"] = _approval_request_id(
        requirement_id=requirement.requirement_id,
        role=EvolutionPromotionApprovalRole.USER,
        interaction_request_sha256="f" * 64,
    )
    forged_payload = {
        key: value
        for key, value in forged_request.items()
        if key not in {"receipt_id", "receipt_sha256"}
    }
    forged_digest = _sha256_payload(forged_payload)
    forged_request["receipt_id"] = f"evapprovalresp_{forged_digest[:24]}"
    forged_request["receipt_sha256"] = forged_digest
    with pytest.raises(ValueError, match="payload 摘要"):
        EvolutionPromotionApprovalResponseReceipt.model_validate_json(json.dumps(forged_request))

    with sqlite3.connect(tmp_path / ".naumi" / "state.db") as db:
        db.execute(
            "UPDATE evolution_promotion_approval_responses SET response = 'approve' "
            "WHERE receipt_id = ?",
            (stored.receipt_id,),
        )
        db.commit()
    with pytest.raises(EvolutionPromotionApprovalRequestError) as corrupt:
        await response_store.get_by_requirement_role(requirement.requirement_id, "user")
    assert corrupt.value.code == "approval_response_store_corrupt"


def test_approval_request_registration_permissions_and_lazy_exports() -> None:
    command = next(item for item in COMMANDS_META if item.name == "/evolution")
    assert "approval-request" in command.arg_hint
    rule = TOOL_PERMISSIONS["evolution_promotion_approval_request"]
    assert rule.allowed_modes == [
        PermissionMode.BYPASS,
        PermissionMode.PERMISSIVE,
        PermissionMode.MODERATE,
        PermissionMode.STRICT,
    ]
    assert not rule.requires_confirmation
    assert rule.max_calls_per_session == 50
    assert rule.risk_level is PermissionRiskLevel.MEDIUM

    from naumi_agent import evolution

    assert (
        evolution.EvolutionPromotionApprovalRequestService
        is EvolutionPromotionApprovalRequestService
    )
