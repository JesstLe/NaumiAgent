from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.cli.completer import COMMANDS_META
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.approval_principals import (
    EvolutionApprovalPrincipalAction,
    EvolutionApprovalPrincipalError,
    EvolutionApprovalPrincipalEvent,
    EvolutionApprovalPrincipalService,
    EvolutionApprovalPrincipalState,
    EvolutionApprovalPrincipalStore,
    _governance_request,
    _principal_id,
    render_evolution_approval_principal,
)
from naumi_agent.evolution.approval_requirements import (
    EvolutionPromotionApprovalRole,
)
from naumi_agent.harness.interaction_runtime import DurableInteractionAuthorityClient
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import (
    TOOL_PERMISSIONS,
    PermissionChecker,
    PermissionMode,
    PermissionRiskLevel,
)
from naumi_agent.tools.evolution_review import (
    EvolutionApprovalPrincipalAuthorityTool,
)
from naumi_agent.user_interaction import normalize_interaction_request


def _public_key() -> tuple[str, bytes]:
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
    return base64.b64encode(public_raw).decode("ascii"), private_raw


class _AnsweringCallback:
    def __init__(
        self,
        authority: DurableInteractionAuthorityClient,
        *,
        answer: str = "approve",
    ) -> None:
        self.authority = authority
        self.answer = answer
        self.calls: list[dict[str, object]] = []
        self.now = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)

    async def __call__(self, payload: dict[str, object]) -> dict[str, str]:
        self.calls.append(payload)
        offset = len(self.calls) * 10
        record = await self.authority.create(
            request=normalize_interaction_request(payload),
            interaction_id=str(payload["_interaction_id"]),
            subject_kind=str(payload["_durable_subject_kind"]),
            subject_id=str(payload["_durable_subject_id"]),
            session_id="principal-governance-session",
            agent_name="main",
            now=(self.now + timedelta(seconds=offset)).isoformat(),
        )
        _record, response = await self.authority.answer(
            record=record,
            response={"kind": "option", "value": self.answer},
            now=(self.now + timedelta(seconds=offset + 1)).isoformat(),
        )
        return response


def _setup(root: Path, *, answer: str = "approve"):
    db_path = root / ".naumi" / "state.db"
    harness = HarnessStore(db_path)
    authority = DurableInteractionAuthorityClient(
        store=harness,
        workspace_root=root,
        owner_id="principal-test-owner",
    )
    callback = _AnsweringCallback(authority, answer=answer)
    store = EvolutionApprovalPrincipalStore(db_path, interaction_store=harness)
    service = EvolutionApprovalPrincipalService(
        store=store,
        interaction_store=harness,
        request_user_input=callback,
    )
    return db_path, harness, authority, callback, store, service


@pytest.mark.asyncio
async def test_registration_is_singleflight_durable_and_cross_surface(tmp_path: Path) -> None:
    db_path, _harness, _authority, callback, store, service = _setup(tmp_path)
    public_key, private_raw = _public_key()

    results = await asyncio.gather(
        *(
            service.register(
                workspace_root=tmp_path,
                principal_name="release.owner",
                roles=("release_manager", "security_reviewer"),
                public_key_base64=public_key,
            )
            for _ in range(8)
        )
    )
    first = results[0]
    assert all(item.principal == first.principal for item in results)
    assert sum(item.authority_changed for item in results) == 1
    assert len(callback.calls) == 1
    assert first.approved and first.principal is not None
    event = first.principal.principal
    assert event.action is EvolutionApprovalPrincipalAction.REGISTER
    assert event.state is EvolutionApprovalPrincipalState.ACTIVE
    assert event.roles == (
        EvolutionPromotionApprovalRole.SECURITY_REVIEWER,
        EvolutionPromotionApprovalRole.RELEASE_MANAGER,
    )
    assert event.key_generation == 1
    assert event.role_binding_verified
    assert not event.private_key_stored
    assert not event.private_key_requested
    assert not event.approval_decision_authority
    assert not event.promotion_authority
    assert first.principal.signature_verification_eligible
    assert await store.get(tmp_path, event.principal_id) == event
    assert await store.record(event) == event

    database_bytes = db_path.read_bytes()
    assert private_raw not in database_bytes
    assert base64.b64encode(private_raw) not in database_bytes
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_approval_principal_events"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM harness_interactions WHERE subject_id = ?",
            (event.principal_id,),
        ).fetchone() == (1,)

    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_approval_principal_service=service,
    )
    tool_output = await EvolutionApprovalPrincipalAuthorityTool(engine).execute(
        principal_id=event.principal_id
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution approval-principal show {event.principal_id}",
    )
    assert tool_output == render_evolution_approval_principal(first.principal)
    assert event.principal_id in slash_output
    assert "私钥从未进入 Naumi" in slash_output


@pytest.mark.asyncio
async def test_rotation_role_update_revoke_and_idempotency(tmp_path: Path) -> None:
    db_path, _harness, _authority, callback, store, service = _setup(tmp_path)
    first_key, _ = _public_key()
    second_key, _ = _public_key()
    registered = await service.register(
        workspace_root=tmp_path,
        principal_name="security.owner",
        roles=("security_reviewer",),
        public_key_base64=first_key,
    )
    assert registered.principal is not None
    principal_id = registered.principal.principal.principal_id

    rotated = await service.rotate_key(
        workspace_root=tmp_path,
        principal_id=principal_id,
        public_key_base64=second_key,
    )
    assert rotated.principal is not None
    assert rotated.principal.principal.sequence == 2
    assert rotated.principal.principal.key_generation == 2
    assert rotated.principal.principal.public_key_base64 == second_key
    assert rotated.principal.principal.previous_event_sha256 == (
        registered.principal.principal.event_sha256
    )

    updated = await service.update_roles(
        workspace_root=tmp_path,
        principal_id=principal_id,
        roles=("security_reviewer", "release_manager"),
    )
    assert updated.principal is not None
    assert updated.principal.principal.sequence == 3
    assert updated.principal.principal.key_generation == 2
    assert updated.principal.principal.public_key_base64 == second_key

    revoked = await service.revoke(workspace_root=tmp_path, principal_id=principal_id)
    assert revoked.principal is not None
    assert revoked.principal.principal.sequence == 4
    assert revoked.principal.principal.state is EvolutionApprovalPrincipalState.REVOKED
    assert not revoked.principal.signature_verification_eligible
    repeated = await service.revoke(workspace_root=tmp_path, principal_id=principal_id)
    assert repeated.approved
    assert not repeated.authority_changed
    assert repeated.action is EvolutionApprovalPrincipalAction.REVOKE
    assert len(callback.calls) == 4
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_approval_principal_events"
        ).fetchone() == (4,)

    with pytest.raises(EvolutionApprovalPrincipalError) as rotate_revoked:
        await service.rotate_key(
            workspace_root=tmp_path,
            principal_id=principal_id,
            public_key_base64=first_key,
        )
    assert rotate_revoked.value.code == "approval_principal_revoked"
    stored = await store.get(tmp_path, principal_id)
    assert stored is not None
    assert stored.state is EvolutionApprovalPrincipalState.REVOKED


@pytest.mark.asyncio
async def test_rejection_pending_retry_invalid_keys_and_workspace_isolation(
    tmp_path: Path,
) -> None:
    rejected_root = tmp_path / "rejected"
    rejected_root.mkdir()
    _db, _harness, _authority, callback, store, service = _setup(
        rejected_root,
        answer="reject",
    )
    key, _ = _public_key()
    rejected = await service.register(
        workspace_root=rejected_root,
        principal_name="data.owner",
        roles=("data_owner",),
        public_key_base64=key,
    )
    assert not rejected.approved
    assert not rejected.authority_changed
    assert rejected.principal is None
    assert await store.get_by_name(rejected_root, "data.owner") is None
    replay = await service.register(
        workspace_root=rejected_root,
        principal_name="data.owner",
        roles=("data_owner",),
        public_key_base64=key,
    )
    assert not replay.approved
    assert len(callback.calls) == 1

    pending_root = tmp_path / "pending"
    pending_root.mkdir()
    _db2, harness2, authority2, _callback2, store2, _service2 = _setup(pending_root)
    workspace = str(pending_root.resolve())
    principal_id = _principal_id(workspace, "review.owner")
    request = _governance_request(
        action=EvolutionApprovalPrincipalAction.REGISTER,
        principal_id=principal_id,
        principal_name="review.owner",
        roles=(EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,),
        public_key_base64=key,
        timeout_seconds=3_600,
    )
    pending = await authority2.create(
        request=request,
        interaction_id=(
            f"ask-evprincipal-{principal_id.removeprefix('evprincipal_')}-register-1"
        ),
        subject_kind="tool",
        subject_id=principal_id,
        session_id="pending-principal-session",
        agent_name="main",
        now=datetime(2026, 7, 23, 10, 0, tzinfo=UTC).isoformat(),
    )

    async def must_not_ask(_payload: dict[str, object]) -> dict[str, str]:
        raise AssertionError("pending governance interaction 不得重复创建")

    pending_service = EvolutionApprovalPrincipalService(
        store=store2,
        interaction_store=harness2,
        request_user_input=must_not_ask,
    )
    with pytest.raises(EvolutionApprovalPrincipalError) as pending_error:
        await pending_service.register(
            workspace_root=pending_root,
            principal_name="review.owner",
            roles=("independent_reviewer",),
            public_key_base64=key,
        )
    assert pending_error.value.code == "approval_principal_interaction_pending"
    await authority2.cancel(
        record=pending,
        now=datetime(2026, 7, 23, 10, 0, 1, tzinfo=UTC).isoformat(),
    )
    retry_callback = _AnsweringCallback(authority2)
    retry_service = EvolutionApprovalPrincipalService(
        store=store2,
        interaction_store=harness2,
        request_user_input=retry_callback,
    )
    retried = await retry_service.register(
        workspace_root=pending_root,
        principal_name="review.owner",
        roles=("independent_reviewer",),
        public_key_base64=key,
    )
    assert retried.principal is not None
    assert str(retry_callback.calls[0]["_interaction_id"]).endswith("-register-2")
    assert await store2.get(tmp_path, principal_id) is None

    with pytest.raises(ValueError, match="public key"):
        await retry_service.register(
            workspace_root=pending_root,
            principal_name="invalid.key",
            roles=("user",),
            public_key_base64="not-base64",
        )


@pytest.mark.asyncio
async def test_event_and_index_tampering_fail_closed(tmp_path: Path) -> None:
    db_path, _harness, _authority, _callback, store, service = _setup(tmp_path)
    key, _ = _public_key()
    registered = await service.register(
        workspace_root=tmp_path,
        principal_name="audit.owner",
        roles=("user",),
        public_key_base64=key,
    )
    assert registered.principal is not None
    event = registered.principal.principal

    forged = event.model_dump(mode="json")
    forged["promotion_authority"] = True
    with pytest.raises(ValueError):
        EvolutionApprovalPrincipalEvent.model_validate_json(json.dumps(forged))

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE evolution_approval_principals SET key_generation = 99 "
            "WHERE principal_id = ?",
            (event.principal_id,),
        )
        db.commit()
    with pytest.raises(EvolutionApprovalPrincipalError) as corrupt:
        await store.get(tmp_path, event.principal_id)
    assert corrupt.value.code == "approval_principal_store_corrupt"

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE evolution_approval_principals SET key_generation = 1 "
            "WHERE principal_id = ?",
            (event.principal_id,),
        )
        db.execute(
            "DELETE FROM evolution_approval_principals WHERE principal_id = ?",
            (event.principal_id,),
        )
        db.commit()
    with pytest.raises(EvolutionApprovalPrincipalError) as orphan:
        await store.get(tmp_path, event.principal_id)
    assert orphan.value.code == "approval_principal_store_corrupt"

    event_root = tmp_path / "event-json"
    event_root.mkdir()
    event_db, _harness2, _authority2, _callback2, event_store, event_service = _setup(
        event_root
    )
    event_key, _ = _public_key()
    event_result = await event_service.register(
        workspace_root=event_root,
        principal_name="event.owner",
        roles=("user",),
        public_key_base64=event_key,
    )
    assert event_result.principal is not None
    event_principal = event_result.principal.principal
    with sqlite3.connect(event_db) as db:
        db.execute(
            "UPDATE evolution_approval_principal_events SET event_json = '{}' "
            "WHERE event_id = ?",
            (event_principal.event_id,),
        )
        db.commit()
    with pytest.raises(EvolutionApprovalPrincipalError) as event_corrupt:
        await event_store.get(event_root, event_principal.principal_id)
    assert event_corrupt.value.code == "approval_principal_store_corrupt"


@pytest.mark.asyncio
async def test_answered_interaction_replays_after_callback_crash(tmp_path: Path) -> None:
    db_path = tmp_path / ".naumi" / "state.db"
    harness = HarnessStore(db_path)
    authority = DurableInteractionAuthorityClient(
        store=harness,
        workspace_root=tmp_path,
        owner_id="principal-replay-owner",
    )
    answer_then_crash = _AnsweringCallback(authority)

    async def crashing_callback(payload: dict[str, object]) -> dict[str, str]:
        await answer_then_crash(payload)
        raise RuntimeError("simulated process loss after durable answer")

    store = EvolutionApprovalPrincipalStore(db_path, interaction_store=harness)
    crashed_service = EvolutionApprovalPrincipalService(
        store=store,
        interaction_store=harness,
        request_user_input=crashing_callback,
    )
    key, _ = _public_key()
    with pytest.raises(RuntimeError, match="simulated process loss"):
        await crashed_service.register(
            workspace_root=tmp_path,
            principal_name="replay.owner",
            roles=("release_manager",),
            public_key_base64=key,
        )

    async def must_not_ask(_payload: dict[str, object]) -> dict[str, str]:
        raise AssertionError("answered authority 必须在重启后直接消费")

    recovered = await EvolutionApprovalPrincipalService(
        store=store,
        interaction_store=harness,
        request_user_input=must_not_ask,
    ).register(
        workspace_root=tmp_path,
        principal_name="replay.owner",
        roles=("release_manager",),
        public_key_base64=key,
    )
    assert recovered.approved and recovered.authority_changed
    assert recovered.principal is not None
    assert recovered.principal.principal.interaction.answered_by == "user"
    assert await store.get(tmp_path, recovered.principal.principal.principal_id) == (
        recovered.principal.principal
    )


def test_registration_permissions_command_metadata_and_lazy_exports() -> None:
    command = next(item for item in COMMANDS_META if item.name == "/evolution")
    assert "approval-principal" in command.arg_hint
    rule = TOOL_PERMISSIONS["evolution_approval_principal"]
    assert rule.allowed_modes == [
        PermissionMode.BYPASS,
        PermissionMode.PERMISSIVE,
        PermissionMode.MODERATE,
        PermissionMode.STRICT,
    ]
    assert not rule.requires_confirmation
    assert rule.max_calls_per_session == 20
    assert rule.risk_level is PermissionRiskLevel.HIGH
    assert rule.tool_family == "evolution_approval_identity"
    bypass = PermissionChecker(PermissionMode.BYPASS).check(
        "evolution_approval_principal",
        {},
    )
    assert bypass.allowed and not bypass.requires_confirmation
    moderate = PermissionChecker(PermissionMode.MODERATE).check(
        "evolution_approval_principal",
        {},
    )
    assert moderate.allowed and moderate.requires_confirmation

    from naumi_agent import evolution

    assert evolution.EvolutionApprovalPrincipalService is EvolutionApprovalPrincipalService
