from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.revalidation_platform_dispatches import (
    EvolutionRevalidationPlatformDispatchError,
    EvolutionRevalidationPlatformDispatchService,
    EvolutionRevalidationPlatformDispatchStore,
)
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


async def _setup(root: Path):
    base, contract, _parent, harness_store, _kernel, _state = await _executor_scenario(root)
    sample = _sample_executor(base, harness_store)
    registry = WorkerRegistryStore(root / ".naumi" / "workers.db")
    matrix = _matrix_service(sample, registry)
    store = EvolutionRevalidationPlatformDispatchStore(sample.receipt_store._db_path)
    service = EvolutionRevalidationPlatformDispatchService(
        workspace_root=sample.workspace_root,
        contract_service=sample.contract_service,
        matrix_service=matrix,
        worker_registry=registry,
        store=store,
    )
    return contract, registry, store, service


@pytest.mark.asyncio
async def test_runnable_lane_queues_one_fenced_capacity_dispatch(tmp_path: Path) -> None:
    contract, registry, store, service = await _setup(tmp_path)
    platform = contract.required_platforms[0]
    worker, report = _healthy_worker(platform, contract.max_total_duration_seconds)
    await registry.register(worker, registered_at=T0)

    queued = await asyncio.gather(
        *(
            service.queue(
                contract_id=contract.contract_id,
                platform=platform,
                worker_health_reports=(report,),
                queued_at=T1,
            )
            for _ in range(8)
        )
    )
    dispatch = queued[0]

    assert all(item == dispatch for item in queued)
    assert dispatch.state == "queued"
    assert dispatch.worker_id == worker.worker_id
    assert dispatch.worker_instance_id == worker.instance_id
    assert dispatch.worker_epoch == worker.epoch
    assert dispatch.worker_contract_sha256 == worker.contract_sha256
    assert dispatch.contract_sha256 == contract.contract_sha256
    assert dispatch.source_snapshot_sha256 == contract.source_snapshot_sha256
    assert dispatch.capacity_reserved
    assert not dispatch.worker_claimed
    assert not dispatch.transport_delivered
    assert not dispatch.result_received
    assert not dispatch.cohort_authority
    assert not dispatch.promotion_authority
    assert await store.get(contract.contract_id, platform) == dispatch
    reservation = await registry.get_capacity_reservation(
        dispatch.reservation_id,
        assessed_at=T1,
    )
    assert reservation is not None
    assert reservation.job_id == dispatch.job_id
    assert reservation.worker_id == dispatch.worker_id
    assert reservation.state.value == "active"
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_revalidation_platform_dispatches"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_pending_lane_never_reserves_or_queues(tmp_path: Path) -> None:
    contract, registry, store, service = await _setup(tmp_path)
    platform = contract.required_platforms[0]

    with pytest.raises(EvolutionRevalidationPlatformDispatchError) as captured:
        await service.queue(
            contract_id=contract.contract_id,
            platform=platform,
            worker_health_reports=(),
            queued_at=T1,
        )

    assert captured.value.code == "platform_dispatch_lane_not_runnable"
    assert await store.get(contract.contract_id, platform) is None
    snapshot = await registry.capacity_snapshot(
        worker_id="missing-worker",
        assessed_at=T1,
    )
    assert snapshot is None
