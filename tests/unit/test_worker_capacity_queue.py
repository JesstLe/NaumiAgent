from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.daemons.worker_contract import (
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
    WorkerResourceEnvelope,
    detect_worker_platform,
    issue_worker_contract,
)
from naumi_agent.daemons.worker_registry import (
    WORKER_REGISTRY_SCHEMA_VERSION,
    WorkerCapacityExhaustedError,
    WorkerCapacityWaiterState,
    WorkerRegistryConflictError,
    WorkerRegistryStore,
    WorkerRegistryStoreError,
)

T0 = "2026-07-24T00:00:00+00:00"
T1 = "2026-07-24T00:00:01+00:00"
T2 = "2026-07-24T00:00:02+00:00"
T3 = "2026-07-24T00:00:03+00:00"
T4 = "2026-07-24T00:00:04+00:00"
T5 = "2026-07-24T00:00:05+00:00"
T6 = "2026-07-24T00:00:06+00:00"


def _contract(epoch: int = 1):
    return issue_worker_contract(
        worker_id="tool-worker-queue",
        instance_id=f"queue-process-{epoch}",
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
        capabilities=tuple(
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
        ),
        resources=WorkerResourceEnvelope(
            max_concurrent_jobs=1,
            max_memory_bytes=512 * 1024 * 1024,
            max_cpu_seconds=60,
            max_wall_seconds=120,
            max_output_bytes=8 * 1024 * 1024,
        ),
        isolation=WorkerIsolationContract(
            ephemeral_workspace=True,
            network_default_deny=True,
            environment_allowlist=True,
            resource_limits_enforced=True,
            process_tree_cancel=True,
            artifact_digest=True,
        ),
        issued_at=T0 if epoch == 1 else T4,
    )


async def _enqueue(
    store: WorkerRegistryStore,
    contract,
    *,
    queue_id: str,
    job_id: str,
    enqueued_at: str = T1,
    deadline_at: str = T6,
    max_waiters: int = 8,
):
    return await store.enqueue_capacity_waiter(
        queue_id=queue_id,
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        job_id=job_id,
        workspace_sha256="a" * 64,
        enqueued_at=enqueued_at,
        deadline_at=deadline_at,
        max_waiters=max_waiters,
    )


@pytest.mark.asyncio
async def test_fifo_claim_and_capacity_reservation_are_one_durable_boundary(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "worker-registry.db"
    store = WorkerRegistryStore(db_path)
    contract = _contract()
    await store.register(contract, registered_at=T1)
    await _enqueue(
        store,
        contract,
        queue_id="queue-first",
        job_id="job-first",
        enqueued_at=T1,
    )
    await _enqueue(
        store,
        contract,
        queue_id="queue-second",
        job_id="job-second",
        enqueued_at=T2,
    )

    first, competing = await asyncio.gather(
        WorkerRegistryStore(db_path).claim_next_capacity_waiter(
            worker_id=contract.worker_id,
            instance_id=contract.instance_id,
            epoch=contract.epoch,
            claimed_at=T3,
        ),
        WorkerRegistryStore(db_path).claim_next_capacity_waiter(
            worker_id=contract.worker_id,
            instance_id=contract.instance_id,
            epoch=contract.epoch,
            claimed_at=T3,
        ),
    )
    claims = [item for item in (first, competing) if item is not None]
    assert len(claims) == 1
    assert claims[0].waiter.queue_id == "queue-first"
    assert claims[0].waiter.state is WorkerCapacityWaiterState.CLAIMED
    assert claims[0].waiter.reservation_id == claims[0].reservation.reservation_id
    snapshot = await store.capacity_snapshot(
        worker_id=contract.worker_id,
        assessed_at=T3,
    )
    assert snapshot is not None
    assert (snapshot.reserved, snapshot.available) == (1, 0)

    await store.release_capacity(
        reservation_id=claims[0].reservation.reservation_id,
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        reason_code="job_finished",
        released_at=T4,
    )
    second = await WorkerRegistryStore(db_path).claim_next_capacity_waiter(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        claimed_at=T4,
    )
    assert second is not None
    assert second.waiter.queue_id == "queue-second"
    catalog = await WorkerRegistryStore(db_path).list_capacity_waiters(
        worker_id=contract.worker_id,
        assessed_at=T4,
    )
    assert [item.queue_id for item in catalog] == ["queue-first", "queue-second"]
    assert all(item.state is WorkerCapacityWaiterState.CLAIMED for item in catalog)
    exact = await WorkerRegistryStore(db_path).get_capacity_waiter("queue-first")
    assert exact == claims[0].waiter
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            UPDATE worker_capacity_reservations
            SET job_id = 'forged-job'
            WHERE reservation_id = ?
            """,
            (claims[0].reservation.reservation_id,),
        )
        db.commit()
    with pytest.raises(WorkerRegistryStoreError, match="无法读取 capacity waiter"):
        await WorkerRegistryStore(db_path).get_capacity_waiter("queue-first")


@pytest.mark.asyncio
async def test_waiter_bound_is_atomic_across_store_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "worker-registry.db"
    store = WorkerRegistryStore(db_path)
    contract = _contract()
    await store.register(contract, registered_at=T1)

    async def enqueue(index: int):
        return await _enqueue(
            WorkerRegistryStore(db_path),
            contract,
            queue_id=f"queue-{index}",
            job_id=f"job-{index}",
            max_waiters=2,
        )

    results = await asyncio.gather(
        *(enqueue(index) for index in range(3)),
        return_exceptions=True,
    )
    accepted = [item for item in results if not isinstance(item, BaseException)]
    rejected = [item for item in results if isinstance(item, BaseException)]
    assert len(accepted) == 2
    assert len(rejected) == 1
    assert isinstance(rejected[0], WorkerCapacityExhaustedError)
    with pytest.raises(WorkerRegistryConflictError, match="durable policy"):
        await _enqueue(
            WorkerRegistryStore(db_path),
            contract,
            queue_id="queue-policy-drift",
            job_id="job-policy-drift",
            max_waiters=3,
        )
    catalog = await store.list_capacity_waiters(
        worker_id=contract.worker_id,
        assessed_at=T2,
    )
    assert len(catalog) == 2
    assert all(item.state is WorkerCapacityWaiterState.WAITING for item in catalog)


@pytest.mark.asyncio
async def test_waiter_identity_zero_bound_and_claimed_cancel_fail_closed(
    tmp_path: Path,
) -> None:
    zero_path = tmp_path / "zero.db"
    zero_store = WorkerRegistryStore(zero_path)
    contract = _contract()
    await zero_store.register(contract, registered_at=T1)
    with pytest.raises(WorkerCapacityExhaustedError, match="上限 0"):
        await _enqueue(
            zero_store,
            contract,
            queue_id="queue-zero",
            job_id="job-zero",
            max_waiters=0,
        )

    store = WorkerRegistryStore(tmp_path / "identity.db")
    await store.register(contract, registered_at=T1)
    first = await _enqueue(
        store,
        contract,
        queue_id="queue-stable",
        job_id="job-stable",
    )
    replay = await _enqueue(
        WorkerRegistryStore(store.db_path),
        contract,
        queue_id="queue-stable",
        job_id="job-stable",
    )
    assert first == replay
    with pytest.raises(WorkerRegistryConflictError, match="不同事实"):
        await _enqueue(
            store,
            contract,
            queue_id="queue-stable",
            job_id="job-changed",
        )
    claim = await store.claim_next_capacity_waiter(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        claimed_at=T2,
    )
    assert claim is not None
    with pytest.raises(WorkerRegistryConflictError, match="已终结"):
        await store.cancel_capacity_waiter(
            queue_id=first.queue_id,
            worker_id=contract.worker_id,
            instance_id=contract.instance_id,
            epoch=contract.epoch,
            job_id=first.job_id,
            reason_code="operator_cancelled",
            cancelled_at=T3,
        )


@pytest.mark.asyncio
async def test_expiry_cancel_and_worker_takeover_are_fail_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "worker-registry.db"
    store = WorkerRegistryStore(db_path)
    first = _contract()
    await store.register(first, registered_at=T1)
    await _enqueue(
        store,
        first,
        queue_id="queue-expired",
        job_id="job-expired",
        deadline_at=T2,
    )
    cancelled_source = await _enqueue(
        store,
        first,
        queue_id="queue-cancelled",
        job_id="job-cancelled",
        deadline_at=T6,
    )
    cancelled = await store.cancel_capacity_waiter(
        queue_id=cancelled_source.queue_id,
        worker_id=first.worker_id,
        instance_id=first.instance_id,
        epoch=first.epoch,
        job_id=cancelled_source.job_id,
        reason_code="operator_cancelled",
        cancelled_at=T2,
    )
    replay = await store.cancel_capacity_waiter(
        queue_id=cancelled_source.queue_id,
        worker_id=first.worker_id,
        instance_id=first.instance_id,
        epoch=first.epoch,
        job_id=cancelled_source.job_id,
        reason_code="operator_cancelled",
        cancelled_at=T3,
    )
    assert cancelled == replay
    assert cancelled.state is WorkerCapacityWaiterState.CANCELLED

    await _enqueue(
        store,
        first,
        queue_id="queue-fenced",
        job_id="job-fenced",
        enqueued_at=T3,
        deadline_at=T6,
    )
    second = _contract(2)
    await store.register(second, registered_at=T4)
    with pytest.raises(WorkerRegistryConflictError, match="fencing"):
        await store.claim_next_capacity_waiter(
            worker_id=first.worker_id,
            instance_id=first.instance_id,
            epoch=first.epoch,
            claimed_at=T5,
        )
    catalog = await store.list_capacity_waiters(
        worker_id=first.worker_id,
        assessed_at=T5,
    )
    states = {item.queue_id: item.state for item in catalog}
    assert states == {
        "queue-expired": WorkerCapacityWaiterState.EXPIRED,
        "queue-cancelled": WorkerCapacityWaiterState.CANCELLED,
        "queue-fenced": WorkerCapacityWaiterState.FENCED,
    }


@pytest.mark.asyncio
async def test_registry_migrates_v2_queue_schema_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "worker-registry.db"
    store = WorkerRegistryStore(db_path)
    contract = _contract()
    await store.register(contract, registered_at=T1)
    with sqlite3.connect(db_path) as db:
        db.execute("DROP INDEX waiting_worker_capacity_fifo")
        db.execute("DROP TABLE worker_capacity_waiters")
        db.execute("DROP TABLE worker_capacity_queue_policies")
        db.execute("PRAGMA user_version = 2")
        db.commit()

    reopened = WorkerRegistryStore(db_path)
    waiter = await _enqueue(
        reopened,
        contract,
        queue_id="queue-after-migration",
        job_id="job-after-migration",
    )
    assert waiter.state is WorkerCapacityWaiterState.WAITING
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == (WORKER_REGISTRY_SCHEMA_VERSION)
        db.execute(
            """
            UPDATE worker_capacity_waiters
            SET workspace_sha256 = ?
                WHERE queue_id = ?
                """,
            ("G" * 64, waiter.queue_id),
        )
        db.commit()
    with pytest.raises(WorkerRegistryStoreError, match="无法读取 capacity waiter"):
        await WorkerRegistryStore(db_path).list_capacity_waiters(
            worker_id=contract.worker_id,
            assessed_at=T2,
        )
