from __future__ import annotations

import asyncio
import base64
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionSource,
)
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.revalidation_platform_claims import (
    EvolutionRevalidationPlatformClaimService,
    EvolutionRevalidationPlatformClaimStore,
    issue_evolution_revalidation_worker_identity,
)
from naumi_agent.evolution.revalidation_platform_dispatches import (
    EvolutionRevalidationPlatformDispatchService,
    EvolutionRevalidationPlatformDispatchStore,
)
from naumi_agent.evolution.revalidation_platform_execution_authorizations import (
    EvolutionRevalidationPlatformExecutionAuthorizationError,
    EvolutionRevalidationPlatformExecutionAuthorizationService,
    EvolutionRevalidationPlatformExecutionAuthorizationStore,
)
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.safety.permissions import PermissionMode
from tests.unit.test_evolution_revalidation_adversarial_matrices import (
    T0,
    T1,
    _healthy_worker,
)
from tests.unit.test_evolution_revalidation_adversarial_matrices import (
    _service as _matrix_service,
)
from tests.unit.test_evolution_revalidation_adversarial_samples import (
    _executor as _sample_executor,
)
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
)

T2 = "2026-07-19T00:00:02+00:00"
T3 = "2026-07-19T00:00:03+00:00"
T4 = "2026-07-19T00:00:04+00:00"
T7 = "2026-07-19T00:00:07+00:00"
WORKER_KEY = b"platform-execution-worker-key-v1!!"
CONTROL_KEY = b"platform-execution-control-key-v1!"


def _public_key(private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(private_key.public_key().public_bytes_raw()).decode()


def _signature(private_key: Ed25519PrivateKey, challenge) -> str:
    return base64.b64encode(
        private_key.sign(challenge.payload.canonical_bytes())
    ).decode()


async def _setup(tmp_path: Path, *, claim_lease_seconds: int = 300):
    base, contract, _old_parent, harness_store, _kernel, _state = await _executor_scenario(
        tmp_path
    )
    sample = _sample_executor(base, harness_store)
    registry = WorkerRegistryStore(tmp_path / ".naumi" / "workers.db")
    matrix = _matrix_service(sample, registry)
    state_db = sample.receipt_store._db_path
    dispatch_store = EvolutionRevalidationPlatformDispatchStore(state_db)
    dispatch_service = EvolutionRevalidationPlatformDispatchService(
        workspace_root=sample.workspace_root,
        contract_service=sample.contract_service,
        matrix_service=matrix,
        worker_registry=registry,
        store=dispatch_store,
    )
    platform = contract.required_platforms[0]
    worker, report = _healthy_worker(platform, contract.max_total_duration_seconds)
    await registry.register(worker, registered_at=T0)
    dispatch = await dispatch_service.queue(
        contract_id=contract.contract_id,
        platform=platform,
        worker_health_reports=(report,),
        queued_at=T1,
    )
    claim_store = EvolutionRevalidationPlatformClaimStore(state_db)
    claim_service = EvolutionRevalidationPlatformClaimService(
        workspace_root=sample.workspace_root,
        contract_service=sample.contract_service,
        dispatch_store=dispatch_store,
        worker_registry=registry,
        store=claim_store,
        supervisor_key_provider=lambda: WORKER_KEY,
    )
    private_key = Ed25519PrivateKey.generate()
    identity = issue_evolution_revalidation_worker_identity(
        contract=worker,
        public_key_base64=_public_key(private_key),
        enrolled_at=T1,
        supervisor_key=WORKER_KEY,
    )
    await claim_service.enroll_identity(identity)
    challenge = await claim_service.prepare_claim(
        contract_id=contract.contract_id,
        platform=platform,
        issued_at=T2,
        lease_seconds=claim_lease_seconds,
    )
    claim = await claim_service.submit(
        challenge_id=challenge.payload.challenge_id,
        signature_base64=_signature(private_key, challenge),
        claimed_at=T2,
    )
    permission_store = base.permission_store
    parent = await permission_store.issue(
        request_id="platform-execution-parent",
        session_id="platform-execution-session",
        run_id="platform-execution-run",
        call_id="platform-execution-parent",
        agent_name="main",
        tool_name="evolution_revalidation_platform_execution",
        tool_family="evolution",
        arguments={"contract_id": contract.contract_id, "platform": platform},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        delegated_tool_names=("bash_run",),
        decided_at=T2,
    )
    store = EvolutionRevalidationPlatformExecutionAuthorizationStore(state_db)
    service = EvolutionRevalidationPlatformExecutionAuthorizationService(
        workspace_root=sample.workspace_root,
        contract_service=sample.contract_service,
        dispatch_store=dispatch_store,
        claim_service=claim_service,
        permission_store=permission_store,
        harness_store=harness_store,
        run_grant_authority=base.run_grant_authority,
        store=store,
        control_plane_key_provider=lambda: CONTROL_KEY,
    )
    return (
        contract,
        worker,
        registry,
        dispatch,
        claim,
        claim_service,
        private_key,
        parent,
        harness_store,
        base.run_grant_authority,
        store,
        service,
    )


@pytest.mark.asyncio
async def test_concurrent_issue_binds_parent_claim_lease_and_run_grant(
    tmp_path: Path,
) -> None:
    (
        contract,
        worker,
        _registry,
        dispatch,
        claim,
        _claim_service,
        _private_key,
        parent,
        harness_store,
        grant_authority,
        store,
        service,
    ) = await _setup(tmp_path)

    views = await asyncio.gather(
        *(
            service.issue(
                claim_id=claim.receipt.claim_id,
                parent_permission_receipt_id=parent.receipt_id,
                issued_at=T3,
                ttl_seconds=60,
            )
            for _ in range(8)
        )
    )
    view = views[0]
    item = view.authorization

    assert all(candidate == view for candidate in views)
    assert view.status == "current" and view.execution_authorized
    assert view.result_submission_authorized
    assert not view.execution_started and not view.result_received
    assert item.dispatch_id == dispatch.dispatch_id
    assert item.claim_receipt_sha256 == claim.receipt.receipt_sha256
    assert item.parent_permission_receipt_sha256 == parent.receipt_sha256
    assert item.sample_indices == tuple(range(contract.requested_samples))
    assert item.phases == ("red", "green")
    assert item.allowed_tool_names == ("bash_run",)
    assert item.max_case_output_bytes == worker.resources.max_output_bytes
    assert item.max_result_bytes == min(
        worker.resources.max_output_bytes * 2 * contract.requested_samples,
        1024**3,
    )
    assert item.run_grant.delegated_tool_names == ("bash_run",)
    assert (await grant_authority.validate(
        grant_id=item.run_grant.grant_id,
        now=T3,
    )).allowed
    lease = await harness_store.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.ACTIVE
    assert lease.owner_id == item.runtime_lease_owner_id
    assert await store.get_by_claim(item.claim_id) == item
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM "
            "evolution_revalidation_platform_execution_authorizations"
        ).fetchone() == (1,)
    with sqlite3.connect(tmp_path / ".naumi" / "run-grants.db") as db:
        assert db.execute("SELECT COUNT(*) FROM run_delegation_grants").fetchone() == (1,)


@pytest.mark.asyncio
async def test_missing_parent_permission_creates_no_execution_side_effects(
    tmp_path: Path,
) -> None:
    (
        _contract,
        _worker,
        _registry,
        _dispatch,
        claim,
        _claim_service,
        _private_key,
        parent,
        harness_store,
        _grant_authority,
        store,
        service,
    ) = await _setup(tmp_path)

    with pytest.raises(EvolutionRevalidationPlatformExecutionAuthorizationError) as captured:
        await service.issue(
            claim_id=claim.receipt.claim_id,
            parent_permission_receipt_id="missing-parent",
            issued_at=T3,
        )

    assert captured.value.code == "platform_execution_parent_permission_invalid"
    assert await store.get_by_claim(claim.receipt.claim_id) is None
    assert await harness_store.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    ) is None


@pytest.mark.asyncio
async def test_expired_claim_cannot_create_execution_authority(tmp_path: Path) -> None:
    (
        _contract,
        _worker,
        _registry,
        _dispatch,
        claim,
        _claim_service,
        _private_key,
        parent,
        _harness_store,
        _grant_authority,
        store,
        service,
    ) = await _setup(tmp_path, claim_lease_seconds=5)

    with pytest.raises(EvolutionRevalidationPlatformExecutionAuthorizationError) as captured:
        await service.issue(
            claim_id=claim.receipt.claim_id,
            parent_permission_receipt_id=parent.receipt_id,
            issued_at=T7,
        )

    assert captured.value.code == "platform_execution_claim_not_current"
    assert await store.get_by_claim(claim.receipt.claim_id) is None


@pytest.mark.asyncio
async def test_claim_renewal_dynamically_stales_old_execution_authority(
    tmp_path: Path,
) -> None:
    (
        _contract,
        _worker,
        _registry,
        _dispatch,
        claim,
        claim_service,
        private_key,
        parent,
        _harness_store,
        _grant_authority,
        _store,
        service,
    ) = await _setup(tmp_path)
    current = await service.issue(
        claim_id=claim.receipt.claim_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at=T3,
        ttl_seconds=60,
    )
    renewal = await claim_service.prepare_renewal(
        claim_id=claim.receipt.claim_id,
        issued_at=T3,
        lease_seconds=60,
    )
    await claim_service.submit(
        challenge_id=renewal.payload.challenge_id,
        signature_base64=_signature(private_key, renewal),
        claimed_at=T4,
    )
    competing_service = EvolutionRevalidationPlatformExecutionAuthorizationService(
        workspace_root=service.workspace_root,
        contract_service=service.contract_service,
        dispatch_store=service.dispatch_store,
        claim_service=service.claim_service,
        permission_store=service.permission_store,
        harness_store=service.harness_store,
        run_grant_authority=service.run_grant_authority,
        store=service.store,
        control_plane_key_provider=lambda: CONTROL_KEY,
    )

    stale = await service.inspect(
        authorization_id=current.authorization.authorization_id,
        assessed_at=T4,
    )
    assert stale.status == "stale"
    assert not stale.execution_authorized
    assert not stale.result_submission_authorized
    replacements = await asyncio.gather(
        *(
            (service if index % 2 == 0 else competing_service).issue(
                claim_id=claim.receipt.claim_id,
                parent_permission_receipt_id=parent.receipt_id,
                issued_at=T4,
                ttl_seconds=60,
            )
            for index in range(8)
        )
    )
    replacement = replacements[0]
    old = await service.inspect(
        authorization_id=current.authorization.authorization_id,
        assessed_at=T4,
    )
    assert old.status == "revoked"
    assert old.revocation is not None
    assert old.revocation.reason_code == "claim_lease_superseded"
    assert replacement.status == "current"
    assert replacement.authorization.authorization_sequence == 2
    assert replacement.authorization.previous_authorization_sha256 == (
        current.authorization.authorization_sha256
    )
    assert replacement.authorization.claim_receipt_sha256 != (
        current.authorization.claim_receipt_sha256
    )
    assert all(candidate == replacement for candidate in replacements)
    with sqlite3.connect(_store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM "
            "evolution_revalidation_platform_execution_authorizations"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT COUNT(*) FROM "
            "evolution_revalidation_platform_execution_revocations"
        ).fetchone() == (1,)
    with sqlite3.connect(tmp_path / ".naumi" / "run-grants.db") as db:
        assert db.execute("SELECT COUNT(*) FROM run_delegation_grants").fetchone() == (
            2,
        )
    lease = await _harness_store.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.ACTIVE
    assert lease.epoch == 2


@pytest.mark.asyncio
async def test_revoke_closes_grant_and_releases_runtime_lease(tmp_path: Path) -> None:
    (
        _contract,
        _worker,
        _registry,
        _dispatch,
        claim,
        _claim_service,
        _private_key,
        parent,
        harness_store,
        grant_authority,
        _store,
        service,
    ) = await _setup(tmp_path)
    issued = await service.issue(
        claim_id=claim.receipt.claim_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at=T3,
        ttl_seconds=60,
    )

    revoked = await service.revoke(
        authorization_id=issued.authorization.authorization_id,
        reason_code="operator_cancelled",
        revoked_at=T4,
    )
    repeated = await service.revoke(
        authorization_id=issued.authorization.authorization_id,
        reason_code="operator_cancelled",
        revoked_at=T4,
    )

    assert repeated == revoked
    assert revoked.status == "revoked"
    assert not revoked.execution_authorized
    validation = await grant_authority.validate(
        grant_id=issued.authorization.run_grant.grant_id,
        now=T4,
    )
    assert not validation.allowed
    lease = await harness_store.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.RELEASED


@pytest.mark.asyncio
async def test_expired_authorization_reissues_as_next_hash_chained_generation(
    tmp_path: Path,
) -> None:
    (
        _contract,
        _worker,
        _registry,
        _dispatch,
        claim,
        _claim_service,
        _private_key,
        parent,
        _harness_store,
        _grant_authority,
        _store,
        service,
    ) = await _setup(tmp_path)
    first = await service.issue(
        claim_id=claim.receipt.claim_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at=T3,
        ttl_seconds=1,
    )
    assert (await service.inspect(
        authorization_id=first.authorization.authorization_id,
        assessed_at=T4,
    )).status == "expired"

    second = await service.issue(
        claim_id=claim.receipt.claim_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at=T4,
        ttl_seconds=60,
    )

    assert second.status == "current"
    assert second.authorization.authorization_sequence == 2
    assert second.authorization.previous_authorization_sha256 == (
        first.authorization.authorization_sha256
    )
    old = await service.inspect(
        authorization_id=first.authorization.authorization_id,
        assessed_at=T4,
    )
    assert old.status == "revoked"
    assert old.revocation is not None
    assert old.revocation.reason_code == "authorization_superseded"


@pytest.mark.asyncio
async def test_operator_revocation_cannot_be_bypassed_by_automatic_reissue(
    tmp_path: Path,
) -> None:
    (
        _contract,
        _worker,
        _registry,
        _dispatch,
        claim,
        _claim_service,
        _private_key,
        parent,
        _harness_store,
        _grant_authority,
        _store,
        service,
    ) = await _setup(tmp_path)
    first = await service.issue(
        claim_id=claim.receipt.claim_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at=T3,
        ttl_seconds=60,
    )
    await service.revoke(
        authorization_id=first.authorization.authorization_id,
        reason_code="operator_cancelled",
        revoked_at=T4,
    )

    with pytest.raises(EvolutionRevalidationPlatformExecutionAuthorizationError) as captured:
        await service.issue(
            claim_id=claim.receipt.claim_id,
            parent_permission_receipt_id=parent.receipt_id,
            issued_at=T4,
            ttl_seconds=60,
        )

    assert captured.value.code == "platform_execution_reauthorization_blocked"


@pytest.mark.asyncio
async def test_store_failure_revokes_grant_and_releases_lease(tmp_path: Path) -> None:
    (
        _contract,
        _worker,
        _registry,
        _dispatch,
        claim,
        claim_service,
        _private_key,
        parent,
        harness_store,
        grant_authority,
        store,
        service,
    ) = await _setup(tmp_path)

    class _FailingStore(EvolutionRevalidationPlatformExecutionAuthorizationStore):
        async def record(self, authorization):
            raise OSError("simulated durable failure")

    failing = EvolutionRevalidationPlatformExecutionAuthorizationService(
        workspace_root=tmp_path,
        contract_service=service.contract_service,
        dispatch_store=service.dispatch_store,
        claim_service=claim_service,
        permission_store=service.permission_store,
        harness_store=harness_store,
        run_grant_authority=grant_authority,
        store=_FailingStore(store.db_path),
        control_plane_key_provider=lambda: CONTROL_KEY,
    )

    with pytest.raises(OSError, match="simulated durable failure"):
        await failing.issue(
            claim_id=claim.receipt.claim_id,
            parent_permission_receipt_id=parent.receipt_id,
            issued_at=T3,
            ttl_seconds=60,
        )

    assert await store.get_by_claim(claim.receipt.claim_id) is None
    with sqlite3.connect(tmp_path / ".naumi" / "run-grants.db") as db:
        assert db.execute("SELECT state FROM run_delegation_grants").fetchall() == [
            ("revoked",)
        ]
    lease = await harness_store.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.RELEASED


@pytest.mark.asyncio
async def test_foreign_control_plane_key_makes_persisted_authority_stale(
    tmp_path: Path,
) -> None:
    (
        _contract,
        _worker,
        _registry,
        _dispatch,
        claim,
        claim_service,
        _private_key,
        parent,
        harness_store,
        grant_authority,
        store,
        service,
    ) = await _setup(tmp_path)
    issued = await service.issue(
        claim_id=claim.receipt.claim_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at=T3,
        ttl_seconds=60,
    )
    foreign = EvolutionRevalidationPlatformExecutionAuthorizationService(
        workspace_root=tmp_path,
        contract_service=service.contract_service,
        dispatch_store=service.dispatch_store,
        claim_service=claim_service,
        permission_store=service.permission_store,
        harness_store=harness_store,
        run_grant_authority=grant_authority,
        store=store,
        control_plane_key_provider=lambda: b"foreign-platform-execution-key!!",
    )

    view = await foreign.inspect(
        authorization_id=issued.authorization.authorization_id,
        assessed_at=T3,
    )
    assert view.status == "stale"
    assert not view.execution_authorized
