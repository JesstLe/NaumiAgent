from __future__ import annotations

import asyncio
import base64
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.daemons.permission_context import bind_permission_receipt
from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantStore,
)
from naumi_agent.evolution.post_rollback_remote_execution_authorizations import (
    EvolutionPostRollbackRemoteExecutionAuthorizationError,
    EvolutionPostRollbackRemoteExecutionAuthorizationService,
    EvolutionPostRollbackRemoteExecutionAuthorizationStore,
)
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionPostRollbackRemoteExecutionAuthorizationTool,
)
from tests.unit.test_post_rollback_remote_deliveries import _delivery_fixture
from tests.unit.test_post_rollback_remote_lane_placements import _sign_claim


async def _execution_fixture(tmp_path: Path):
    delivery_service, claim, signing_private, _transport, _archive, _calls = (
        await _delivery_fixture(tmp_path)
    )
    delivery = await delivery_service.prepare(
        claim_id=claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:05+00:00",
        offer_ttl_seconds=15,
    )
    delivery_signature = base64.b64encode(
        signing_private.sign(delivery.offer.ack_payload.canonical_bytes())
    ).decode("ascii")
    delivered = await delivery_service.submit_ack(
        delivery_id=delivery.offer.delivery_id,
        worker_signature_base64=delivery_signature,
        acknowledged_at="2026-08-10T08:03:06+00:00",
    )
    dispatch = await delivery_service.dispatch_store.get_by_id(
        delivered.offer.dispatch_id
    )
    assert dispatch is not None
    permission_store = PermissionDecisionReceiptStore(
        tmp_path / ".naumi" / "permissions.db"
    )
    parent = await permission_store.issue(
        request_id="post-rollback-execution-parent",
        session_id="post-rollback-execution-session",
        run_id="post-rollback-execution-run",
        call_id="post-rollback-execution-call",
        agent_name="main",
        tool_name="evolution_post_rollback_remote_execution",
        tool_family="evolution_evaluation_artifact",
        arguments={"delivery_id": delivered.offer.delivery_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="medium",
        delegated_tool_names=("bash_run",),
        decided_at="2026-08-10T08:03:06+00:00",
    )
    harness_store = HarnessStore(tmp_path / ".naumi" / "harness.db")
    grant_store = RunDelegationGrantStore(tmp_path / ".naumi" / "run-grants.db")
    grant_authority = RunDelegationGrantAuthority(
        store=grant_store,
        permission_store=permission_store,
        harness_store=harness_store,
        workspace_root=dispatch.workspace_root,
    )
    store = EvolutionPostRollbackRemoteExecutionAuthorizationStore(
        delivery_service.store.db_path
    )
    service = EvolutionPostRollbackRemoteExecutionAuthorizationService(
        workspace_root=dispatch.workspace_root,
        delivery_service=delivery_service,
        delivery_store=delivery_service.store,
        claim_service=delivery_service.claim_service,
        claim_store=delivery_service.claim_store,
        dispatch_service=delivery_service.dispatch_service,
        dispatch_store=delivery_service.dispatch_store,
        identity_authority=delivery_service.identity_authority,
        permission_store=permission_store,
        harness_store=harness_store,
        run_grant_authority=grant_authority,
        store=store,
        random_bytes=lambda size: bytes([size + 2]) * size,
        clock=lambda: datetime(2026, 8, 10, 8, 3, 7, tzinfo=UTC),
    )
    return (
        service,
        delivered,
        dispatch,
        claim,
        signing_private,
        parent,
        harness_store,
        grant_authority,
        store,
    )


@pytest.mark.asyncio
async def test_start_signature_atomically_creates_exact_execution_authority(
    tmp_path: Path,
) -> None:
    (
        service,
        delivered,
        dispatch,
        _claim,
        signing_private,
        parent,
        harness_store,
        grant_authority,
        store,
    ) = await _execution_fixture(tmp_path)

    prepared = await service.prepare(
        delivery_id=delivered.offer.delivery_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at="2026-08-10T08:03:07+00:00",
        challenge_ttl_seconds=5,
    )

    assert prepared.status == "awaiting_start"
    assert not prepared.execution_authorized
    assert prepared.authorization is None
    assert prepared.challenge.payload.runtime_eval_request == dispatch.runtime_eval_request
    assert prepared.challenge.payload.total_execution_budget_ms == (
        dispatch.total_execution_budget_ms
    )
    assert prepared.challenge.payload.max_result_bytes == 512 * 1024 * dispatch.repetitions
    assert await harness_store.get_run_lease(
        workspace_root=dispatch.workspace_root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    ) is None

    signature = base64.b64encode(
        signing_private.sign(prepared.challenge.payload.canonical_bytes())
    ).decode("ascii")
    authorized = await service.submit(
        challenge_id=prepared.challenge.challenge_id,
        worker_signature_base64=signature,
        authorized_at="2026-08-10T08:03:08+00:00",
    )
    replay = await service.submit(
        challenge_id=prepared.challenge.challenge_id,
        worker_signature_base64=signature,
        authorized_at="2026-08-10T08:03:08+00:00",
    )

    assert replay == authorized
    assert authorized.status == "current"
    assert authorized.installation_authorized and authorized.execution_authorized
    assert not authorized.execution_started
    assert not authorized.result_authority
    item = authorized.authorization
    assert item is not None
    assert item.start_payload.delivery_receipt_sha256 == (
        delivered.receipt.receipt_sha256
    )
    assert item.run_grant.delegated_tool_names == ("bash_run",)
    assert (
        await grant_authority.validate(
            grant_id=item.run_grant.grant_id,
            now="2026-08-10T08:03:08+00:00",
        )
    ).allowed
    lease = await harness_store.get_run_lease(
        workspace_root=dispatch.workspace_root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.ACTIVE
    assert lease.owner_id == item.runtime_lease_owner_id
    assert await store.get_authorization_for_attempt(item.attempt_id) == item


@pytest.mark.asyncio
async def test_forged_start_signature_creates_no_lease_or_grant(tmp_path: Path) -> None:
    service, delivered, dispatch, _claim, _key, parent, harness, _grants, _store = (
        await _execution_fixture(tmp_path)
    )
    prepared = await service.prepare(
        delivery_id=delivered.offer.delivery_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at="2026-08-10T08:03:07+00:00",
    )

    with pytest.raises(EvolutionPostRollbackRemoteExecutionAuthorizationError) as exc:
        await service.submit(
            challenge_id=prepared.challenge.challenge_id,
            worker_signature_base64=base64.b64encode(b"x" * 64).decode("ascii"),
            authorized_at="2026-08-10T08:03:08+00:00",
        )
    assert exc.value.code == "post_rollback_remote_start_signature_invalid"
    assert await harness.get_run_lease(
        workspace_root=dispatch.workspace_root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    ) is None
    grant_db = tmp_path / ".naumi" / "run-grants.db"
    if grant_db.exists():
        with sqlite3.connect(grant_db) as db:
            assert db.execute("SELECT COUNT(*) FROM run_delegation_grants").fetchone() == (0,)


@pytest.mark.asyncio
async def test_concurrent_prepare_and_submit_converge_to_one_attempt(tmp_path: Path) -> None:
    service, delivered, _dispatch, _claim, key, parent, _harness, _grants, store = (
        await _execution_fixture(tmp_path)
    )
    second = EvolutionPostRollbackRemoteExecutionAuthorizationService(
        workspace_root=service.workspace_root,
        delivery_service=service.delivery_service,
        delivery_store=service.delivery_store,
        claim_service=service.claim_service,
        claim_store=service.claim_store,
        dispatch_service=service.dispatch_service,
        dispatch_store=service.dispatch_store,
        identity_authority=service.identity_authority,
        permission_store=service.permission_store,
        harness_store=service.harness_store,
        run_grant_authority=service.run_grant_authority,
        store=EvolutionPostRollbackRemoteExecutionAuthorizationStore(store.db_path),
        random_bytes=lambda size: bytes([size + 3]) * size,
    )
    left, right = await asyncio.gather(
        service.prepare(
            delivery_id=delivered.offer.delivery_id,
            parent_permission_receipt_id=parent.receipt_id,
            issued_at="2026-08-10T08:03:07+00:00",
        ),
        second.prepare(
            delivery_id=delivered.offer.delivery_id,
            parent_permission_receipt_id=parent.receipt_id,
            issued_at="2026-08-10T08:03:07+00:00",
        ),
    )
    assert left.challenge == right.challenge
    signature = base64.b64encode(
        key.sign(left.challenge.payload.canonical_bytes())
    ).decode("ascii")
    first_auth, second_auth = await asyncio.gather(
        service.submit(
            challenge_id=left.challenge.challenge_id,
            worker_signature_base64=signature,
            authorized_at="2026-08-10T08:03:08+00:00",
        ),
        second.submit(
            challenge_id=left.challenge.challenge_id,
            worker_signature_base64=signature,
            authorized_at="2026-08-10T08:03:08+00:00",
        ),
    )
    assert first_auth == second_auth
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_post_rollback_remote_execution_authorizations"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_missing_parent_permission_cannot_create_start_challenge(
    tmp_path: Path,
) -> None:
    service, delivered, _dispatch, _claim, _key, _parent, _harness, _grants, _store = (
        await _execution_fixture(tmp_path)
    )

    with pytest.raises(EvolutionPostRollbackRemoteExecutionAuthorizationError) as exc:
        await service.prepare(
            delivery_id=delivered.offer.delivery_id,
            parent_permission_receipt_id="missing-parent",
            issued_at="2026-08-10T08:03:07+00:00",
        )
    assert exc.value.code == "post_rollback_remote_execution_authority_stale"

    stale_parent = await service.permission_store.issue(
        request_id="stale-post-rollback-parent",
        session_id="post-rollback-execution-session",
        run_id="stale-post-rollback-run",
        call_id="stale-post-rollback-call",
        agent_name="main",
        tool_name="evolution_post_rollback_remote_execution",
        tool_family="evolution_evaluation_artifact",
        arguments={"delivery_id": delivered.offer.delivery_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="medium",
        delegated_tool_names=("bash_run",),
        decided_at="2026-08-10T07:57:00+00:00",
    )
    with pytest.raises(EvolutionPostRollbackRemoteExecutionAuthorizationError) as old:
        await service.prepare(
            delivery_id=delivered.offer.delivery_id,
            parent_permission_receipt_id=stale_parent.receipt_id,
            issued_at="2026-08-10T08:03:07+00:00",
        )
    assert old.value.code == "post_rollback_remote_execution_authority_stale"


@pytest.mark.asyncio
async def test_claim_renewal_and_grant_revocation_dynamically_fence_authority(
    tmp_path: Path,
) -> None:
    service, delivered, _dispatch, claim, key, parent, _harness, grants, _store = (
        await _execution_fixture(tmp_path)
    )
    prepared = await service.prepare(
        delivery_id=delivered.offer.delivery_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at="2026-08-10T08:03:07+00:00",
    )
    signature = base64.b64encode(
        key.sign(prepared.challenge.payload.canonical_bytes())
    ).decode("ascii")
    authorized = await service.submit(
        challenge_id=prepared.challenge.challenge_id,
        worker_signature_base64=signature,
        authorized_at="2026-08-10T08:03:08+00:00",
    )
    item = authorized.authorization
    assert item is not None
    await grants.revoke(
        grant_id=item.run_grant.grant_id,
        reason="operator_cancelled",
        revoked_at="2026-08-10T08:03:08.500000+00:00",
    )
    revoked = await service.inspect(
        reference_id=item.authorization_id,
        assessed_at="2026-08-10T08:03:08.500000+00:00",
    )
    assert revoked.status == "stale"
    assert revoked.authorization == item
    assert not revoked.execution_authorized

    renewal_root = tmp_path / "renewal"
    renewal_root.mkdir()
    service, delivered, _dispatch, claim, key, parent, _harness, _grants, _store = (
        await _execution_fixture(renewal_root)
    )
    prepared = await service.prepare(
        delivery_id=delivered.offer.delivery_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at="2026-08-10T08:03:07+00:00",
    )
    signature = base64.b64encode(
        key.sign(prepared.challenge.payload.canonical_bytes())
    ).decode("ascii")
    authorized = await service.submit(
        challenge_id=prepared.challenge.challenge_id,
        worker_signature_base64=signature,
        authorized_at="2026-08-10T08:03:08+00:00",
    )
    renewal = await service.claim_service.prepare_renewal(
        claim_id=claim.receipt.claim_id,
        issued_at="2026-08-10T08:03:09+00:00",
        challenge_ttl_seconds=1,
        lease_seconds=5,
    )
    await service.claim_service.submit(
        challenge_id=renewal.payload.challenge_id,
        signature_base64=_sign_claim(key, renewal),
        claimed_at="2026-08-10T08:03:09.500000+00:00",
    )
    stale = await service.inspect(
        reference_id=authorized.authorization.authorization_id,
        assessed_at="2026-08-10T08:03:09.500000+00:00",
    )
    assert stale.status == "stale"
    assert not stale.claim_authority
    assert not stale.execution_authorized


@pytest.mark.asyncio
async def test_slow_grant_crossing_start_deadline_revokes_grant_and_releases_lease(
    tmp_path: Path,
) -> None:
    service, delivered, dispatch, _claim, key, parent, harness, grants, store = (
        await _execution_fixture(tmp_path)
    )
    current = [datetime(2026, 8, 10, 8, 3, 7, tzinfo=UTC)]
    service.clock = lambda: current[0]
    prepared = await service.prepare(
        delivery_id=delivered.offer.delivery_id,
        parent_permission_receipt_id=parent.receipt_id,
        challenge_ttl_seconds=5,
    )
    current[0] = datetime(2026, 8, 10, 8, 3, 8, tzinfo=UTC)
    original_issue = grants.issue

    async def delayed_issue(*args, **kwargs):
        grant = await original_issue(*args, **kwargs)
        current[0] = datetime(2026, 8, 10, 8, 3, 12, tzinfo=UTC)
        return grant

    grants.issue = delayed_issue  # type: ignore[method-assign]
    signature = base64.b64encode(
        key.sign(prepared.challenge.payload.canonical_bytes())
    ).decode("ascii")

    with pytest.raises(EvolutionPostRollbackRemoteExecutionAuthorizationError) as exc:
        await service.submit(
            challenge_id=prepared.challenge.challenge_id,
            worker_signature_base64=signature,
        )
    assert exc.value.code == "post_rollback_remote_start_window_invalid"
    assert await store.get_authorization_for_attempt(
        prepared.challenge.attempt_id
    ) is None
    lease = await harness.get_run_lease(
        workspace_root=dispatch.workspace_root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.RELEASED
    with sqlite3.connect(tmp_path / ".naumi" / "run-grants.db") as db:
        assert db.execute(
            "SELECT state FROM run_delegation_grants"
        ).fetchone() == ("revoked",)


@pytest.mark.asyncio
async def test_authorization_store_failure_compensates_grant_and_lease(
    tmp_path: Path,
) -> None:
    service, delivered, dispatch, _claim, key, parent, harness, _grants, store = (
        await _execution_fixture(tmp_path)
    )
    prepared = await service.prepare(
        delivery_id=delivered.offer.delivery_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at="2026-08-10T08:03:07+00:00",
    )

    class _FailingStore(EvolutionPostRollbackRemoteExecutionAuthorizationStore):
        async def record_authorization(self, authorization, *, assessed_at):
            raise EvolutionPostRollbackRemoteExecutionAuthorizationError(
                "injected_authorization_store_failure",
                "injected",
            )

    service.store = _FailingStore(store.db_path)
    signature = base64.b64encode(
        key.sign(prepared.challenge.payload.canonical_bytes())
    ).decode("ascii")
    with pytest.raises(EvolutionPostRollbackRemoteExecutionAuthorizationError) as exc:
        await service.submit(
            challenge_id=prepared.challenge.challenge_id,
            worker_signature_base64=signature,
            authorized_at="2026-08-10T08:03:08+00:00",
        )
    assert exc.value.code == "injected_authorization_store_failure"
    lease = await harness.get_run_lease(
        workspace_root=dispatch.workspace_root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.RELEASED
    with sqlite3.connect(tmp_path / ".naumi" / "run-grants.db") as db:
        assert db.execute(
            "SELECT state FROM run_delegation_grants"
        ).fetchone() == ("revoked",)


@pytest.mark.asyncio
async def test_execution_tool_and_slash_share_persistent_parent_permission(
    tmp_path: Path,
) -> None:
    service, delivered, _dispatch, _claim, _key, parent, _harness, _grants, _store = (
        await _execution_fixture(tmp_path)
    )
    tool = EvolutionPostRollbackRemoteExecutionAuthorizationTool(
        SimpleNamespace(
            evolution_post_rollback_remote_execution_authorization_service=service
        )
    )
    arguments = {"action": "prepare", "delivery_id": delivered.offer.delivery_id}
    assert tool.metadata.delegated_tool_names == ("bash_run",)
    assert tool.metadata.requires_persistent_authorization
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    with bind_permission_receipt(parent):
        rendered = await tool.execute(**arguments)
    assert "Start Challenge" in rendered

    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            parsed = registered.parse_arguments(call.arguments)
            with bind_permission_receipt(parent):
                content = await registered.execute(**parsed)
            return ToolResult(call_id=call.id, status="success", content=content)

    slash = await execute_slash_command(
        _SlashEngine(),
        "/evolution outcome-authorize-behavior prepare "
        f"{delivered.offer.delivery_id}",
    )
    assert "Start Challenge" in slash


def test_execution_authorization_rejects_split_session_authority_paths(
    tmp_path: Path,
) -> None:
    one = tmp_path / "one.db"
    two = tmp_path / "two.db"
    with pytest.raises(ValueError, match="共享 session SQLite"):
        EvolutionPostRollbackRemoteExecutionAuthorizationService(
            workspace_root=tmp_path,
            delivery_service=object(),  # type: ignore[arg-type]
            delivery_store=SimpleNamespace(db_path=one),  # type: ignore[arg-type]
            claim_service=object(),  # type: ignore[arg-type]
            claim_store=SimpleNamespace(db_path=one),  # type: ignore[arg-type]
            dispatch_service=object(),  # type: ignore[arg-type]
            dispatch_store=SimpleNamespace(db_path=two),  # type: ignore[arg-type]
            identity_authority=SimpleNamespace(
                store=SimpleNamespace(db_path=one)
            ),  # type: ignore[arg-type]
            permission_store=object(),  # type: ignore[arg-type]
            harness_store=object(),  # type: ignore[arg-type]
            run_grant_authority=object(),  # type: ignore[arg-type]
            store=EvolutionPostRollbackRemoteExecutionAuthorizationStore(one),
        )


@pytest.mark.asyncio
async def test_start_challenge_sqlite_tamper_fails_closed(tmp_path: Path) -> None:
    service, delivered, _dispatch, _claim, _key, parent, _harness, _grants, store = (
        await _execution_fixture(tmp_path)
    )
    prepared = await service.prepare(
        delivery_id=delivered.offer.delivery_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at="2026-08-10T08:03:07+00:00",
    )
    encoded = prepared.challenge.model_dump_json()
    suite = prepared.challenge.payload.suite_id
    tampered = encoded.replace(
        f'"suite_id":"{suite}"',
        '"suite_id":"tampered_suite"',
        1,
    )
    assert tampered != encoded
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_remote_start_challenges "
            "SET challenge_json = ? WHERE challenge_id = ?",
            (tampered, prepared.challenge.challenge_id),
        )

    with pytest.raises(EvolutionPostRollbackRemoteExecutionAuthorizationError) as exc:
        await store.get_challenge(prepared.challenge.challenge_id)
    assert exc.value.code == "post_rollback_remote_execution_store_corrupt"
