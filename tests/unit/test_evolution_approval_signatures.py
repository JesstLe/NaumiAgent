from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.cli.completer import COMMANDS_META
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.approval_principals import (
    EvolutionApprovalPrincipalService,
    EvolutionApprovalPrincipalStore,
)
from naumi_agent.evolution.approval_requests import (
    EvolutionPromotionApprovalRequestService,
)
from naumi_agent.evolution.approval_signatures import (
    EVOLUTION_APPROVAL_SIGNATURE_DOMAIN,
    EvolutionApprovalSignatureChallenge,
    EvolutionApprovalSignatureChallengeStatus,
    EvolutionApprovalSignatureError,
    EvolutionApprovalSignatureReceipt,
    EvolutionApprovalSignatureService,
    EvolutionApprovalSignatureStore,
    render_evolution_approval_signature,
)
from naumi_agent.safety.permissions import (
    TOOL_PERMISSIONS,
    PermissionChecker,
    PermissionMode,
    PermissionRiskLevel,
)
from naumi_agent.tools.evolution_review import (
    EvolutionApprovalSignatureAuthorityTool,
    EvolutionApprovalSignatureTool,
)
from tests.unit.test_evolution_approval_principals import _AnsweringCallback
from tests.unit.test_evolution_approval_requests import (
    _answering_callback,
)
from tests.unit.test_evolution_approval_requests import (
    _setup as _approval_setup,
)


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _keypair() -> tuple[Ed25519PrivateKey, str, bytes]:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(public_raw).decode("ascii"), private_raw


@dataclass
class _SignatureAuthorities:
    db_path: Path
    requirement_executor: object
    requirement_view: object
    response_store: object
    response_view: object
    principal_store: EvolutionApprovalPrincipalStore
    principal_service: EvolutionApprovalPrincipalService
    principal_result: object
    principal_callback: _AnsweringCallback
    signature_store: EvolutionApprovalSignatureStore
    signature_service: EvolutionApprovalSignatureService
    signature_clock: _MutableClock
    private_key: Ed25519PrivateKey
    private_raw: bytes


async def _setup_signature_authorities(
    root: Path,
    *,
    response_value: str = "approve",
) -> _SignatureAuthorities:
    (
        approval_clock,
        requirement_executor,
        requirement_view,
        harness_store,
        authority,
        response_store,
    ) = await _approval_setup(root)
    response_observations: list[tuple[str, str]] = []
    response_service = EvolutionPromotionApprovalRequestService(
        requirement_executor=requirement_executor,
        interaction_store=harness_store,
        response_store=response_store,
        request_user_input=_answering_callback(
            authority=authority,
            now=approval_clock.value,
            answer_value=response_value,
            observations=response_observations,
        ),
        clock=approval_clock,
    )
    response_view = await response_service.execute(
        workspace_root=root,
        requirement_id=requirement_view.requirement.requirement_id,
        role="data_owner",
    )
    private_key, public_key, private_raw = _keypair()
    principal_callback = _AnsweringCallback(authority)
    principal_store = EvolutionApprovalPrincipalStore(
        root / ".naumi" / "state.db",
        interaction_store=harness_store,
    )
    principal_service = EvolutionApprovalPrincipalService(
        store=principal_store,
        interaction_store=harness_store,
        request_user_input=principal_callback,
    )
    principal_result = await principal_service.register(
        workspace_root=root,
        principal_name="data.owner",
        roles=("data_owner",),
        public_key_base64=public_key,
    )
    signature_clock = _MutableClock(datetime(2026, 7, 23, 9, 10, tzinfo=UTC))
    signature_store = EvolutionApprovalSignatureStore(root / ".naumi" / "state.db")
    signature_service = EvolutionApprovalSignatureService(
        response_store=response_store,
        requirement_executor=requirement_executor,
        principal_service=principal_service,
        signature_store=signature_store,
        clock=signature_clock,
        challenge_ttl_seconds=300,
    )
    return _SignatureAuthorities(
        db_path=root / ".naumi" / "state.db",
        requirement_executor=requirement_executor,
        requirement_view=requirement_view,
        response_store=response_store,
        response_view=response_view,
        principal_store=principal_store,
        principal_service=principal_service,
        principal_result=principal_result,
        principal_callback=principal_callback,
        signature_store=signature_store,
        signature_service=signature_service,
        signature_clock=signature_clock,
        private_key=private_key,
        private_raw=private_raw,
    )


def _signature(private: Ed25519PrivateKey, challenge: EvolutionApprovalSignatureChallenge) -> str:
    return base64.b64encode(private.sign(challenge.payload.canonical_bytes())).decode("ascii")


@pytest.mark.asyncio
async def test_real_signature_is_singleflight_durable_and_cross_surface(tmp_path: Path) -> None:
    authority = await _setup_signature_authorities(tmp_path)
    response = authority.response_view.receipt
    principal = authority.principal_result.principal.principal
    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_approval_signature_service=authority.signature_service,
    )

    slash_prepare = await execute_slash_command(
        engine,
        "/evolution approval-signature prepare "
        f"{response.receipt_id} {principal.principal_id}",
    )

    challenges = await asyncio.gather(
        *(
            authority.signature_service.prepare(
                workspace_root=tmp_path,
                approval_response_id=response.receipt_id,
                principal_id=principal.principal_id,
            )
            for _ in range(8)
        )
    )
    challenge_view = challenges[0]
    challenge = challenge_view.challenge
    assert all(item.challenge == challenge for item in challenges)
    assert challenge_view.status is EvolutionApprovalSignatureChallengeStatus.PENDING
    assert challenge_view.eligible_for_submission
    assert challenge.payload.domain == EVOLUTION_APPROVAL_SIGNATURE_DOMAIN
    assert challenge.payload.role.value == "data_owner"
    assert challenge.payload.approval_response_id == response.receipt_id
    assert challenge.payload.requirement_id == response.requirement_id
    assert challenge.payload.principal_event_id == principal.event_id
    assert challenge.payload.key_generation == 1
    assert base64.b64decode(challenge.signable_payload_base64, validate=True) == (
        challenge.payload.canonical_bytes()
    )
    assert challenge.challenge_id in slash_prepare

    signature = _signature(authority.private_key, challenge)
    write_tool_output = await EvolutionApprovalSignatureTool(engine).execute(
        action="submit",
        challenge_id=challenge.challenge_id,
        signature_base64=signature,
    )
    receipts = await asyncio.gather(
        *(
            authority.signature_service.submit(
                workspace_root=tmp_path,
                challenge_id=challenge.challenge_id,
                signature_base64=signature,
            )
            for _ in range(8)
        )
    )
    receipt_view = receipts[0]
    receipt = receipt_view.receipt
    assert all(item.receipt == receipt for item in receipts)
    assert receipt.signature_verified
    assert receipt.identity_binding_verified
    assert receipt.role_binding_verified
    assert receipt.counts_toward_role_quorum
    assert not receipt.overall_approval_decided
    assert not receipt.final_quorum_reached
    assert not receipt.promotion_authority
    assert not receipt.git_write_executed
    assert not receipt.private_key_requested
    assert not receipt.private_key_stored
    assert receipt_view.eligible_for_future_aggregation
    assert receipt.receipt_id in write_tool_output
    consumed = await authority.signature_store.get_challenge(challenge.challenge_id)
    assert consumed is not None
    assert consumed.status is EvolutionApprovalSignatureChallengeStatus.CONSUMED
    assert consumed.closed_by_receipt_id == receipt.receipt_id

    database = authority.db_path.read_bytes()
    assert authority.private_raw not in database
    assert base64.b64encode(authority.private_raw) not in database
    with sqlite3.connect(authority.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_approval_signature_challenges"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_approval_signature_receipts"
        ).fetchone() == (1,)

    tool_output = await EvolutionApprovalSignatureAuthorityTool(engine).execute(
        receipt.receipt_id
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution approval-signature show {receipt.receipt_id}",
    )
    assert tool_output == render_evolution_approval_signature(receipt_view)
    assert receipt.receipt_id in slash_output
    assert "仍不是最终 Promotion 决定" in slash_output


@pytest.mark.asyncio
async def test_wrong_domain_signature_and_consumed_conflict_fail_closed(
    tmp_path: Path,
) -> None:
    authority = await _setup_signature_authorities(tmp_path)
    response = authority.response_view.receipt
    principal = authority.principal_result.principal.principal
    challenge_view = await authority.signature_service.prepare(
        workspace_root=tmp_path,
        approval_response_id=response.receipt_id,
        principal_id=principal.principal_id,
    )
    challenge = challenge_view.challenge
    wrong_key = Ed25519PrivateKey.generate()
    wrong_signature = base64.b64encode(
        wrong_key.sign(challenge.payload.canonical_bytes())
    ).decode("ascii")
    with pytest.raises(EvolutionApprovalSignatureError) as invalid:
        await authority.signature_service.submit(
            workspace_root=tmp_path,
            challenge_id=challenge.challenge_id,
            signature_base64=wrong_signature,
        )
    assert invalid.value.code == "approval_signature_invalid"

    package_digest_signature = base64.b64encode(
        authority.private_key.sign(
            response.signature_entry.signable_payload_sha256.encode("ascii")
        )
    ).decode("ascii")
    with pytest.raises(EvolutionApprovalSignatureError) as wrong_domain:
        await authority.signature_service.submit(
            workspace_root=tmp_path,
            challenge_id=challenge.challenge_id,
            signature_base64=package_digest_signature,
        )
    assert wrong_domain.value.code == "approval_signature_invalid"

    valid_signature = _signature(authority.private_key, challenge)
    receipt = await authority.signature_service.submit(
        workspace_root=tmp_path,
        challenge_id=challenge.challenge_id,
        signature_base64=valid_signature,
    )
    replay = await authority.signature_service.submit(
        workspace_root=tmp_path,
        challenge_id=challenge.challenge_id,
        signature_base64=valid_signature,
    )
    assert replay.receipt == receipt.receipt
    with pytest.raises(EvolutionApprovalSignatureError) as conflict:
        await authority.signature_service.submit(
            workspace_root=tmp_path,
            challenge_id=challenge.challenge_id,
            signature_base64=wrong_signature,
        )
    assert conflict.value.code == "approval_signature_receipt_conflict"


@pytest.mark.asyncio
async def test_rotation_supersedes_challenge_and_revocation_invalidates_receipt(
    tmp_path: Path,
) -> None:
    authority = await _setup_signature_authorities(tmp_path)
    response = authority.response_view.receipt
    principal = authority.principal_result.principal.principal
    first = await authority.signature_service.prepare(
        workspace_root=tmp_path,
        approval_response_id=response.receipt_id,
        principal_id=principal.principal_id,
    )
    first_signature = _signature(authority.private_key, first.challenge)

    second_private, second_public, _second_private_raw = _keypair()
    rotated = await authority.principal_service.rotate_key(
        workspace_root=tmp_path,
        principal_id=principal.principal_id,
        public_key_base64=second_public,
    )
    assert rotated.principal is not None
    assert rotated.principal.principal.key_generation == 2
    with pytest.raises(EvolutionApprovalSignatureError) as stale:
        await authority.signature_service.submit(
            workspace_root=tmp_path,
            challenge_id=first.challenge.challenge_id,
            signature_base64=first_signature,
        )
    assert stale.value.code == "approval_signature_challenge_authority_mismatch"

    second = await authority.signature_service.prepare(
        workspace_root=tmp_path,
        approval_response_id=response.receipt_id,
        principal_id=principal.principal_id,
    )
    assert second.challenge.attempt == 2
    assert second.challenge.payload.key_generation == 2
    old = await authority.signature_store.get_challenge(first.challenge.challenge_id)
    assert old is not None
    assert old.status is EvolutionApprovalSignatureChallengeStatus.SUPERSEDED
    second_signature = _signature(second_private, second.challenge)
    verified = await authority.signature_service.submit(
        workspace_root=tmp_path,
        challenge_id=second.challenge.challenge_id,
        signature_base64=second_signature,
    )
    assert verified.eligible_for_future_aggregation

    revoked = await authority.principal_service.revoke(
        workspace_root=tmp_path,
        principal_id=principal.principal_id,
    )
    assert revoked.principal is not None
    after_revoke = await authority.signature_service.inspect(
        workspace_root=tmp_path,
        receipt_id=verified.receipt.receipt_id,
    )
    assert not after_revoke.principal_active
    assert not after_revoke.principal_key_current
    assert not after_revoke.eligible_for_future_aggregation


@pytest.mark.asyncio
async def test_expiry_workspace_role_and_nonapprove_boundaries(tmp_path: Path) -> None:
    authority = await _setup_signature_authorities(tmp_path)
    response = authority.response_view.receipt
    principal = authority.principal_result.principal.principal
    first = await authority.signature_service.prepare(
        workspace_root=tmp_path,
        approval_response_id=response.receipt_id,
        principal_id=principal.principal_id,
    )
    signature = _signature(authority.private_key, first.challenge)
    authority.signature_clock.value += timedelta(seconds=301)
    with pytest.raises(EvolutionApprovalSignatureError) as expired:
        await authority.signature_service.submit(
            workspace_root=tmp_path,
            challenge_id=first.challenge.challenge_id,
            signature_base64=signature,
        )
    assert expired.value.code == "approval_signature_challenge_closed"
    second = await authority.signature_service.prepare(
        workspace_root=tmp_path,
        approval_response_id=response.receipt_id,
        principal_id=principal.principal_id,
    )
    assert second.challenge.attempt == 2

    other_root = tmp_path / "other"
    other_root.mkdir()
    with pytest.raises(EvolutionApprovalSignatureError) as workspace_mismatch:
        await authority.signature_service.submit(
            workspace_root=other_root,
            challenge_id=second.challenge.challenge_id,
            signature_base64=_signature(authority.private_key, second.challenge),
        )
    assert workspace_mismatch.value.code == "approval_signature_workspace_mismatch"

    rejected_root = tmp_path / "rejected"
    rejected_root.mkdir()
    rejected = await _setup_signature_authorities(rejected_root, response_value="reject")
    rejected_response = rejected.response_view.receipt
    rejected_principal = rejected.principal_result.principal.principal
    with pytest.raises(EvolutionApprovalSignatureError) as nonapprove:
        await rejected.signature_service.prepare(
            workspace_root=rejected_root,
            approval_response_id=rejected_response.receipt_id,
            principal_id=rejected_principal.principal_id,
        )
    assert nonapprove.value.code == "approval_signature_response_requirement_mismatch"


@pytest.mark.asyncio
async def test_artifact_and_store_tampering_fail_closed(tmp_path: Path) -> None:
    authority = await _setup_signature_authorities(tmp_path)
    response = authority.response_view.receipt
    principal = authority.principal_result.principal.principal
    challenge_view = await authority.signature_service.prepare(
        workspace_root=tmp_path,
        approval_response_id=response.receipt_id,
        principal_id=principal.principal_id,
    )
    receipt_view = await authority.signature_service.submit(
        workspace_root=tmp_path,
        challenge_id=challenge_view.challenge.challenge_id,
        signature_base64=_signature(authority.private_key, challenge_view.challenge),
    )
    forged = receipt_view.receipt.model_dump(mode="json")
    forged["promotion_authority"] = True
    with pytest.raises(ValueError):
        EvolutionApprovalSignatureReceipt.model_validate_json(json.dumps(forged))

    with sqlite3.connect(authority.db_path) as db:
        db.execute(
            "UPDATE evolution_approval_signature_receipts SET key_id = ? "
            "WHERE receipt_id = ?",
            ("evapprovalkey_" + "0" * 24, receipt_view.receipt.receipt_id),
        )
        db.commit()
    with pytest.raises(EvolutionApprovalSignatureError) as corrupt:
        await authority.signature_store.get_receipt(receipt_view.receipt.receipt_id)
    assert corrupt.value.code == "approval_signature_receipt_store_corrupt"


def test_registration_permissions_metadata_and_lazy_exports() -> None:
    command = next(item for item in COMMANDS_META if item.name == "/evolution")
    assert "approval-signature" in command.arg_hint
    rule = TOOL_PERMISSIONS["evolution_approval_signature"]
    assert rule.allowed_modes == [
        PermissionMode.BYPASS,
        PermissionMode.PERMISSIVE,
        PermissionMode.MODERATE,
        PermissionMode.STRICT,
    ]
    assert not rule.requires_confirmation
    assert rule.max_calls_per_session == 50
    assert rule.risk_level is PermissionRiskLevel.MEDIUM
    assert rule.tool_family == "evolution_approval_signature"
    moderate = PermissionChecker(PermissionMode.MODERATE).check(
        "evolution_approval_signature",
        {},
    )
    assert moderate.allowed and not moderate.requires_confirmation

    from naumi_agent import evolution

    assert evolution.EvolutionApprovalSignatureService is EvolutionApprovalSignatureService
    assert EvolutionApprovalSignatureTool(SimpleNamespace()).metadata.read_only is False
    assert EvolutionApprovalSignatureAuthorityTool(SimpleNamespace()).metadata.read_only is True
