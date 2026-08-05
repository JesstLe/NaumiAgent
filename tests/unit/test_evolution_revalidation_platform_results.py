from __future__ import annotations

import asyncio
import base64
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionSource,
)
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.revalidation_adversarial_samples import (
    EvolutionRevalidationAdversarialSampleStore,
    adversarial_batch_id,
)
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
    EvolutionRevalidationPlatformExecutionAuthorizationService,
    EvolutionRevalidationPlatformExecutionAuthorizationStore,
)
from naumi_agent.evolution.revalidation_platform_results import (
    EvolutionRevalidationPlatformResultArtifact,
    EvolutionRevalidationPlatformResultError,
    EvolutionRevalidationPlatformResultPayload,
    EvolutionRevalidationPlatformResultService,
    EvolutionRevalidationPlatformResultStore,
    issue_evolution_revalidation_platform_result_manifest,
)
from naumi_agent.harness.store import (
    HarnessStoreError,
    harness_eval_result_canonical_json,
    harness_eval_result_sha256,
)
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
    _executor as _adversarial_executor,
)
from tests.unit.test_evolution_revalidation_adversarial_samples import (
    _executor_scenario,
)

T2 = "2026-07-19T00:00:02+00:00"
T3 = "2026-07-19T00:00:03+00:00"
T_EXPIRED = "2026-07-19T00:02:00+00:00"
WORKER_KEY = b"platform-result-worker-key-v1!!!!!!"
CONTROL_KEY = b"platform-result-control-key-v1!!!!!"


def _public_key(private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(private_key.public_key().public_bytes_raw()).decode()


def _claim_signature(private_key, challenge) -> str:
    return base64.b64encode(
        private_key.sign(challenge.payload.canonical_bytes())
    ).decode()


async def _scenario(tmp_path: Path, *, sample_count: int = 1):
    base, contract, _old_parent, harness_store, _kernel, _state = (
        await _executor_scenario(tmp_path)
    )
    sample_executor = _adversarial_executor(base, harness_store)
    registry = WorkerRegistryStore(tmp_path / ".naumi" / "workers.db")
    matrix = _matrix_service(sample_executor, registry)
    state_db = sample_executor.receipt_store._db_path
    dispatch_store = EvolutionRevalidationPlatformDispatchStore(state_db)
    dispatch_service = EvolutionRevalidationPlatformDispatchService(
        workspace_root=sample_executor.workspace_root,
        contract_service=sample_executor.contract_service,
        matrix_service=matrix,
        worker_registry=registry,
        store=dispatch_store,
    )
    platform = contract.required_platforms[0]
    worker, report = _healthy_worker(platform, contract.max_total_duration_seconds)
    await registry.register(worker, registered_at=T0)
    await dispatch_service.queue(
        contract_id=contract.contract_id,
        platform=platform,
        worker_health_reports=(report,),
        queued_at=T1,
    )
    claim_store = EvolutionRevalidationPlatformClaimStore(state_db)
    claim_service = EvolutionRevalidationPlatformClaimService(
        workspace_root=sample_executor.workspace_root,
        contract_service=sample_executor.contract_service,
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
        lease_seconds=300,
    )
    claim = await claim_service.submit(
        challenge_id=challenge.payload.challenge_id,
        signature_base64=_claim_signature(private_key, challenge),
        claimed_at=T2,
    )
    parent = await base.permission_store.issue(
        request_id="platform-result-parent",
        session_id="platform-result-session",
        run_id="platform-result-run",
        call_id="platform-result-parent",
        agent_name="main",
        tool_name="evolution_revalidation_platform_result",
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
    sample_executor.now = lambda: T3
    local_pairs = []
    for sample_index in range(sample_count):
        local_pair = await sample_executor.execute(
            contract_id=contract.contract_id,
            platform=platform,
            parent_receipt_id=parent.receipt_id,
            sample_index=sample_index,
        )
        red = await harness_store.get_eval_result(
            tmp_path,
            local_pair.red_batch_id,
            contract.suite_id,
            sample_index,
        )
        green = await harness_store.get_eval_result(
            tmp_path,
            local_pair.green_batch_id,
            contract.suite_id,
            sample_index,
        )
        assert red is not None and green is not None
        local_pairs.append((local_pair, red, green))
    authorization_store = EvolutionRevalidationPlatformExecutionAuthorizationStore(
        state_db
    )
    authorization_service = EvolutionRevalidationPlatformExecutionAuthorizationService(
        workspace_root=sample_executor.workspace_root,
        contract_service=sample_executor.contract_service,
        dispatch_store=dispatch_store,
        claim_service=claim_service,
        permission_store=base.permission_store,
        harness_store=harness_store,
        run_grant_authority=base.run_grant_authority,
        store=authorization_store,
        control_plane_key_provider=lambda: CONTROL_KEY,
    )
    authorization = await authorization_service.issue(
        claim_id=claim.receipt.claim_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at=T3,
        ttl_seconds=60,
    )
    authority = authorization.authorization
    artifacts = tuple(
        _artifact(
            stored.result,
            phase=phase,
            sample_index=sample_index,
            sample_seed=local_pair.sample_seed,
            grant_sha256=authority.run_grant.grant_sha256,
        )
        for sample_index, (local_pair, red, green) in enumerate(local_pairs)
        for phase, stored in (("red", red), ("green", green))
    )
    first_pair = local_pairs[0][0]
    payload = EvolutionRevalidationPlatformResultPayload(
        authorization_id=authority.authorization_id,
        authorization_sha256=authority.authorization_sha256,
        authorization_sequence=authority.authorization_sequence,
        claim_receipt_id=authority.claim_receipt_id,
        claim_receipt_sha256=authority.claim_receipt_sha256,
        identity_id=authority.identity_id,
        identity_sha256=authority.identity_sha256,
        worker_id=authority.worker_id,
        worker_instance_id=authority.worker_instance_id,
        worker_epoch=authority.worker_epoch,
        contract_id=authority.contract_id,
        contract_sha256=authority.contract_sha256,
        source_snapshot_id=authority.source_snapshot_id,
        source_snapshot_sha256=authority.source_snapshot_sha256,
        platform=authority.platform,
        platform_identity=first_pair.platform_identity,
        platform_identity_sha256=first_pair.platform_sha256,
        suite_id=authority.suite_id,
        requested_samples=authority.requested_samples,
        start_index=0,
        sample_count=sample_count,
        artifacts=artifacts,
        result_bytes=sum(item.result_bytes for item in artifacts),
        run_grant_sha256=authority.run_grant.grant_sha256,
        completed_at=T3,
    )
    manifest = issue_evolution_revalidation_platform_result_manifest(
        payload=payload,
        private_key=private_key,
    )
    clock = {"now": T3}
    result_store = EvolutionRevalidationPlatformResultStore(
        state_db,
        control_plane_key_provider=lambda: CONTROL_KEY,
    )
    service = EvolutionRevalidationPlatformResultService(
        workspace_root=sample_executor.workspace_root,
        authorization_service=authorization_service,
        source_service=sample_executor.source_service,
        profile_service=sample_executor.profile_service,
        harness_store=harness_store,
        sample_store=EvolutionRevalidationAdversarialSampleStore(state_db),
        store=result_store,
        control_plane_key_provider=lambda: CONTROL_KEY,
        now=lambda: clock["now"],
    )
    return (
        contract,
        private_key,
        authority,
        manifest,
        clock,
        harness_store,
        result_store,
        service,
    )


def _artifact(result, *, phase, sample_index, sample_seed, grant_sha256):
    cases = tuple(
        case.model_copy(
            update={
                "message": re.sub(
                    r"run_grant_sha256=[0-9a-f]{64}",
                    f"run_grant_sha256={grant_sha256}",
                    case.message.replace("run_scope=sample", "run_scope=cohort"),
                )
            }
        )
        for case in result.cases
    )
    updated = result.model_copy(update={"cases": cases})
    encoded = harness_eval_result_canonical_json(updated).encode("utf-8")
    return EvolutionRevalidationPlatformResultArtifact(
        sample_index=sample_index,
        sample_seed=sample_seed,
        phase=phase,
        result_sha256=harness_eval_result_sha256(updated),
        result_bytes=len(encoded),
        result=updated,
    )


@pytest.mark.asyncio
async def test_signed_manifest_recovers_partial_h5a_after_authority_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        contract,
        _private_key,
        authority,
        manifest,
        clock,
        harness_store,
        result_store,
        service,
    ) = await _scenario(tmp_path)
    original = harness_store.record_eval_result
    calls = 0

    async def fail_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HarnessStoreError("injected green write failure")
        return await original(**kwargs)

    monkeypatch.setattr(harness_store, "record_eval_result", fail_second)
    with pytest.raises(EvolutionRevalidationPlatformResultError) as interrupted:
        await service.submit(manifest)
    assert interrupted.value.code == "platform_result_h5a_rejected"
    assert await result_store.get_manifest(manifest.manifest_id) == manifest
    assert await result_store.get_receipt(manifest.manifest_id) is None
    red_batch = adversarial_batch_id(contract, authority.platform, "red", "cohort")
    green_batch = adversarial_batch_id(contract, authority.platform, "green", "cohort")
    assert await harness_store.get_eval_result(
        tmp_path, red_batch, contract.suite_id, 0
    ) is not None
    assert await harness_store.get_eval_result(
        tmp_path, green_batch, contract.suite_id, 0
    ) is None

    monkeypatch.setattr(harness_store, "record_eval_result", original)
    clock["now"] = T_EXPIRED
    receipt = await service.submit(manifest)
    repeated = await service.submit(manifest)

    assert repeated == receipt
    assert receipt.result_received and receipt.h5a_ingested
    assert receipt.continuous_prefix and not receipt.full_cohort_received
    assert not receipt.cohort_authority and not receipt.promotion_authority
    assert await result_store.accepted_prefix(contract.contract_id, authority.platform) == 1
    assert await harness_store.get_eval_result(
        tmp_path, green_batch, contract.suite_id, 0
    ) is not None


@pytest.mark.asyncio
async def test_concurrent_exact_manifest_ingestion_is_idempotent(tmp_path: Path) -> None:
    (
        contract,
        _private_key,
        authority,
        manifest,
        _clock,
        _harness_store,
        result_store,
        service,
    ) = await _scenario(tmp_path)
    origin = datetime.fromisoformat(T3).astimezone(UTC)
    ticks = iter(
        (origin + timedelta(microseconds=index)).isoformat() for index in range(8)
    )
    service.now = lambda: next(ticks)

    receipts = await asyncio.gather(*(service.submit(manifest) for _ in range(8)))

    assert all(item == receipts[0] for item in receipts)
    assert await result_store.accepted_prefix(contract.contract_id, authority.platform) == 1


@pytest.mark.asyncio
async def test_invalid_worker_signature_and_wrong_grant_leave_no_remote_evidence(
    tmp_path: Path,
) -> None:
    (
        contract,
        _private_key,
        authority,
        manifest,
        _clock,
        harness_store,
        result_store,
        service,
    ) = await _scenario(tmp_path)
    forged = issue_evolution_revalidation_platform_result_manifest(
        payload=manifest.payload,
        private_key=Ed25519PrivateKey.generate(),
    )
    with pytest.raises(EvolutionRevalidationPlatformResultError) as invalid_signature:
        await service.submit(forged)
    assert invalid_signature.value.code == "platform_result_signature_invalid"

    wrong_grant = "f" * 64
    bad_artifacts = tuple(
        _artifact(
            artifact.result,
            phase=artifact.phase,
            sample_index=artifact.sample_index,
            sample_seed=artifact.sample_seed,
            grant_sha256=wrong_grant,
        )
        for artifact in manifest.payload.artifacts
    )
    bad_payload = manifest.payload.model_copy(
        update={
            "artifacts": bad_artifacts,
            "result_bytes": sum(item.result_bytes for item in bad_artifacts),
        }
    )
    bad_manifest = issue_evolution_revalidation_platform_result_manifest(
        payload=bad_payload,
        private_key=_private_key,
    )
    with pytest.raises(EvolutionRevalidationPlatformResultError) as invalid_grant:
        await service.submit(bad_manifest)
    assert invalid_grant.value.code == "platform_result_grant_mismatch"
    assert await result_store.get_window(authority.authorization_id, 0) is None
    for phase in ("red", "green"):
        assert await harness_store.get_eval_result(
            tmp_path,
            adversarial_batch_id(contract, authority.platform, phase, "cohort"),
            contract.suite_id,
            0,
        ) is None


@pytest.mark.asyncio
async def test_durable_admission_attestation_is_tamper_evident(tmp_path: Path) -> None:
    (
        _contract,
        _private_key,
        _authority,
        manifest,
        _clock,
        _harness_store,
        result_store,
        service,
    ) = await _scenario(tmp_path)
    await service.submit(manifest)
    with sqlite3.connect(result_store.db_path) as db:
        db.execute(
            "UPDATE evolution_revalidation_platform_result_manifests "
            "SET admission_attestation_sha256 = ? WHERE manifest_id = ?",
            ("f" * 64, manifest.manifest_id),
        )
        db.commit()

    with pytest.raises(EvolutionRevalidationPlatformResultError) as tampered:
        await result_store.get_manifest(manifest.manifest_id)
    assert tampered.value.code == "platform_result_admission_tampered"
