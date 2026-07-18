from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionReason,
    WorkerAdmissionRequirements,
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
    WorkerResourceEnvelope,
    detect_worker_platform,
    issue_worker_contract,
    issue_worker_health_report,
)
from naumi_agent.daemons.worker_registry import (
    WORKER_REGISTRY_SCHEMA_VERSION,
    WorkerRegistrationState,
    WorkerRegistryConflictError,
    WorkerRegistryStore,
    WorkerRegistryStoreError,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind

T0 = "2026-07-19T00:00:00+00:00"
T1 = "2026-07-19T00:00:01+00:00"
T2 = "2026-07-19T00:00:02+00:00"
T3 = "2026-07-19T00:00:03+00:00"
T4 = "2026-07-19T00:00:04+00:00"


def _isolation() -> WorkerIsolationContract:
    return WorkerIsolationContract(True, True, True, True, True, True)


def _capabilities() -> tuple[WorkerCapability, ...]:
    return tuple(
        sorted(
            (
                WorkerCapability.ARTIFACT_DIGEST,
                WorkerCapability.ENVIRONMENT_ALLOWLIST,
                WorkerCapability.NETWORK_POLICY,
                WorkerCapability.PROCESS_TREE_CANCEL,
                WorkerCapability.RESOURCE_LIMITS,
                WorkerCapability.SHELL_NON_PTY,
                WorkerCapability.WORKSPACE_EPHEMERAL,
            ),
            key=str,
        )
    )


def _contract(epoch: int = 1, *, issued_at: str | None = None):
    return issue_worker_contract(
        worker_id="tool-worker-a",
        instance_id=f"process-{epoch}",
        epoch=epoch,
        kind=WorkerKind.TOOL,
        protocol_min=1,
        protocol_max=1,
        software_version="0.1.214",
        platform=detect_worker_platform(
            system="Linux",
            machine="x86_64",
            python_implementation="CPython",
            python_version="3.13.5",
        ),
        capabilities=_capabilities(),
        resources=WorkerResourceEnvelope(
            max_concurrent_jobs=2,
            max_memory_bytes=512 * 1024 * 1024,
            max_cpu_seconds=60,
            max_wall_seconds=120,
            max_output_bytes=8 * 1024 * 1024,
        ),
        isolation=_isolation(),
        issued_at=issued_at or (T0 if epoch == 1 else T1),
    )


def _report(contract, *, observed_at: str = T2):
    heartbeat = HarnessHeartbeat(
        workspace_root="/workspace",
        subject_kind=HarnessRunKind.TOOL,
        subject_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=observed_at,
        timeout_seconds=10,
        detail_code="ready",
    )
    return issue_worker_health_report(
        contract=contract,
        heartbeat=heartbeat,
        active_jobs=0,
        accepting_jobs=True,
    )


def _requirements() -> WorkerAdmissionRequirements:
    return WorkerAdmissionRequirements(
        kind=WorkerKind.TOOL,
        protocol_version=1,
        capabilities=_capabilities(),
        allowed_platforms=("linux",),
        min_memory_bytes=256 * 1024 * 1024,
        min_cpu_seconds=30,
        min_wall_seconds=60,
        min_output_bytes=1024 * 1024,
        isolation=_isolation(),
    )


@pytest.mark.asyncio
async def test_registry_is_lazy_idempotent_and_survives_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "workers.db"
    store = WorkerRegistryStore(db_path)
    contract = _contract()
    assert not db_path.exists()

    first = await store.register(contract, registered_at=T1)
    replay = await store.register(contract, registered_at=T2)
    reopened = await WorkerRegistryStore(db_path).get_active(contract.worker_id)

    assert first == replay == reopened
    assert first.state is WorkerRegistrationState.ACTIVE
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == WORKER_REGISTRY_SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM worker_registrations").fetchone()[0] == 1
    if os.name != "nt":
        assert db_path.stat().st_mode & 0o777 == 0o600
        assert db_path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_concurrent_first_registration_initializes_schema_once(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "workers.db"
    contract = _contract()

    first, second = await asyncio.gather(
        WorkerRegistryStore(db_path).register(contract, registered_at=T1),
        WorkerRegistryStore(db_path).register(contract, registered_at=T1),
    )

    assert first == second
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM worker_registrations").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_higher_epoch_fences_history_and_stale_takeover(tmp_path: Path) -> None:
    store = WorkerRegistryStore(tmp_path / "workers.db")
    first = _contract()
    second = _contract(2)
    await store.register(first, registered_at=T1)
    active = await store.register(second, registered_at=T2)

    assert active.contract == second
    history = await store.list_history(first.worker_id)
    assert [item.state for item in history] == [
        WorkerRegistrationState.SUPERSEDED,
        WorkerRegistrationState.ACTIVE,
    ]
    assert history[0].terminal_at == T2
    assert history[0].reason_code == "higher_epoch_registered"

    with pytest.raises(WorkerRegistryConflictError, match="未高于"):
        await store.register(
            issue_worker_contract(
                worker_id=first.worker_id,
                instance_id="different-process",
                epoch=1,
                kind=first.kind,
                protocol_min=first.protocol_min,
                protocol_max=first.protocol_max,
                software_version=first.software_version,
                platform=first.platform,
                capabilities=first.capabilities,
                resources=first.resources,
                isolation=first.isolation,
                issued_at=T2,
            ),
            registered_at=T3,
        )
    with pytest.raises(WorkerRegistryConflictError, match="新的 instance_id"):
        await store.register(
            issue_worker_contract(
                worker_id=second.worker_id,
                instance_id=second.instance_id,
                epoch=3,
                kind=second.kind,
                protocol_min=second.protocol_min,
                protocol_max=second.protocol_max,
                software_version=second.software_version,
                platform=second.platform,
                capabilities=second.capabilities,
                resources=second.resources,
                isolation=second.isolation,
                issued_at=T3,
            ),
            registered_at=T3,
        )


@pytest.mark.asyncio
async def test_concurrent_registrations_converge_on_highest_epoch(tmp_path: Path) -> None:
    db_path = tmp_path / "workers.db"
    seed = WorkerRegistryStore(db_path)
    await seed.register(_contract(), registered_at=T1)
    second = WorkerRegistryStore(db_path)
    third = WorkerRegistryStore(db_path)

    results = await asyncio.gather(
        second.register(_contract(2, issued_at=T2), registered_at=T2),
        third.register(_contract(3, issued_at=T3), registered_at=T3),
        return_exceptions=True,
    )

    assert all(
        not isinstance(item, BaseException) or isinstance(item, WorkerRegistryConflictError)
        for item in results
    )
    active = await WorkerRegistryStore(db_path).get_active("tool-worker-a")
    assert active is not None
    assert active.contract.epoch == 3
    history = await seed.list_history("tool-worker-a")
    assert [item.contract.epoch for item in history][-1] == 3


@pytest.mark.asyncio
async def test_revoke_is_exact_idempotent_and_blocks_stale_owner(tmp_path: Path) -> None:
    store = WorkerRegistryStore(tmp_path / "workers.db")
    first = _contract()
    await store.register(first, registered_at=T1)
    revoked = await store.revoke(
        worker_id=first.worker_id,
        instance_id=first.instance_id,
        epoch=first.epoch,
        reason_code="operator_stop",
        revoked_at=T2,
    )
    replay = await store.revoke(
        worker_id=first.worker_id,
        instance_id=first.instance_id,
        epoch=first.epoch,
        reason_code="operator_stop",
        revoked_at=T3,
    )

    assert revoked == replay
    assert revoked.state is WorkerRegistrationState.REVOKED
    assert await store.get_active(first.worker_id) is None
    with pytest.raises(WorkerRegistryConflictError, match="不同原因"):
        await store.revoke(
            worker_id=first.worker_id,
            instance_id=first.instance_id,
            epoch=first.epoch,
            reason_code="health_failure",
            revoked_at=T3,
        )

    high_watermark = WorkerRegistryStore(tmp_path / "high-watermark.db")
    fifth = _contract(5, issued_at=T1)
    await high_watermark.register(fifth, registered_at=T1)
    await high_watermark.revoke(
        worker_id=fifth.worker_id,
        instance_id=fifth.instance_id,
        epoch=fifth.epoch,
        reason_code="operator_stop",
        revoked_at=T2,
    )
    with pytest.raises(WorkerRegistryConflictError, match="不能重新激活"):
        await high_watermark.register(fifth, registered_at=T3)
    with pytest.raises(WorkerRegistryConflictError, match="未高于"):
        await high_watermark.register(
            _contract(4, issued_at=T3),
            registered_at=T3,
        )


@pytest.mark.asyncio
async def test_authority_admission_uses_only_active_contract(tmp_path: Path) -> None:
    store = WorkerRegistryStore(tmp_path / "workers.db")
    old = _contract()
    current = _contract(2)
    missing = await store.assess_admission(
        worker_id=old.worker_id,
        report=_report(old),
        requirements=_requirements(),
        now=T3,
    )
    assert missing.reasons == (WorkerAdmissionReason.REGISTRATION_MISSING,)

    await store.register(old, registered_at=T1)
    await store.register(current, registered_at=T2)
    stale = await store.assess_admission(
        worker_id=old.worker_id,
        report=_report(old),
        requirements=_requirements(),
        now=T3,
    )
    admitted = await store.assess_admission(
        worker_id=current.worker_id,
        report=_report(current),
        requirements=_requirements(),
        now=T3,
    )

    assert WorkerAdmissionReason.IDENTITY_MISMATCH in stale.reasons
    assert admitted.admitted


@pytest.mark.asyncio
async def test_registry_rejects_tamper_clock_regression_and_unknown_schema(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workers.db"
    store = WorkerRegistryStore(db_path)
    contract = _contract()
    with pytest.raises(ValueError, match="摘要校验失败"):
        await store.register(replace(contract, software_version="0.1.215"), registered_at=T1)
    with pytest.raises(ValueError, match="不能晚于"):
        await store.register(_contract(issued_at=T2), registered_at=T1)

    await store.register(contract, registered_at=T1)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE worker_registrations SET contract_json = ? WHERE worker_id = ?",
            ("{}", contract.worker_id),
        )
        db.commit()
    with pytest.raises(WorkerRegistryStoreError, match="无法读取"):
        await WorkerRegistryStore(db_path).get_active(contract.worker_id)

    future_path = tmp_path / "future.db"
    with sqlite3.connect(future_path) as db:
        db.execute(f"PRAGMA user_version = {WORKER_REGISTRY_SCHEMA_VERSION + 1}")
    with pytest.raises(WorkerRegistryStoreError, match="不受支持"):
        await WorkerRegistryStore(future_path).get_active(contract.worker_id)

    legacy_path = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_path) as db:
        db.execute("CREATE TABLE user_data (value TEXT NOT NULL)")
        db.execute("INSERT INTO user_data VALUES ('preserve-me')")
    with pytest.raises(WorkerRegistryStoreError, match="未知的未版本化"):
        await WorkerRegistryStore(legacy_path).get_active(contract.worker_id)
    with sqlite3.connect(legacy_path) as db:
        assert db.execute("SELECT value FROM user_data").fetchone()[0] == "preserve-me"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0

    wrong_type = tmp_path / "registry-directory"
    wrong_type.mkdir()
    with pytest.raises(WorkerRegistryStoreError, match="不是文件"):
        await WorkerRegistryStore(wrong_type).get_active(contract.worker_id)

    broken_path = tmp_path / "broken-history.db"
    broken = WorkerRegistryStore(broken_path)
    await broken.register(_contract(), registered_at=T1)
    await broken.register(_contract(2), registered_at=T2)
    with sqlite3.connect(broken_path) as db:
        db.execute("DELETE FROM worker_registrations WHERE epoch = 2")
        db.commit()
    with pytest.raises(WorkerRegistryStoreError, match="历史断裂"):
        await WorkerRegistryStore(broken_path).register(
            _contract(3, issued_at=T3),
            registered_at=T3,
        )
