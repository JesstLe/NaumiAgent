from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from naumi_agent.daemons.worker_authority_health import (
    WorkerAuthorityHealthError,
    inspect_worker_authority_health,
)
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
    WorkerRegistryStore,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HARNESS_STORE_SCHEMA_VERSION, HarnessStore
from naumi_agent.ui.doctor import DoctorReport, _worker_authority_check
from naumi_agent.ui.doctor_health import build_doctor_health_snapshot

T0 = "2026-07-19T00:00:00+00:00"
T1 = "2026-07-19T00:00:01+00:00"


def _contract():
    return issue_worker_contract(
        worker_id="tool-worker-a",
        instance_id="process-1",
        epoch=3,
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
        capabilities=(WorkerCapability.SHELL_NON_PTY,),
        resources=WorkerResourceEnvelope(
            max_concurrent_jobs=4,
            max_memory_bytes=512 * 1024 * 1024,
            max_cpu_seconds=60,
            max_wall_seconds=120,
            max_output_bytes=8 * 1024 * 1024,
        ),
        isolation=WorkerIsolationContract(False, False, False, False, False, False),
        issued_at=T0,
    )


@pytest.mark.asyncio
async def test_absent_authority_is_lazy_and_user_visible(tmp_path: Path) -> None:
    registry = tmp_path / "runtime" / "worker-registry.db"
    harness = tmp_path / "state" / "harness.db"

    snapshot = inspect_worker_authority_health(
        registry_db_path=registry,
        harness_db_path=harness,
        workspace_root=tmp_path,
        now=T1,
    )
    check = _worker_authority_check(snapshot)

    assert snapshot.registry_health == "absent"
    assert snapshot.heartbeat_store_health == "not_needed"
    assert check.status == "pass"
    assert "按需创建" in check.detail
    assert not registry.exists()
    assert not harness.exists()
    assert not registry.parent.exists()
    assert not harness.parent.exists()


@pytest.mark.asyncio
async def test_active_worker_combines_verified_contract_and_healthy_heartbeat(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "worker-registry.db"
    harness = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    contract = _contract()
    await WorkerRegistryStore(registry).register(contract, registered_at=T1)
    await HarnessStore(harness).record_heartbeat(
        workspace_root=workspace,
        subject_kind=HarnessRunKind.TOOL,
        subject_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at="2026-07-19T00:00:02+00:00",
        timeout_seconds=10,
        detail_code="secret-bearing-internal-code",
    )
    registry_before = registry.read_bytes()
    harness_before = harness.read_bytes()

    snapshot = inspect_worker_authority_health(
        registry_db_path=registry,
        harness_db_path=harness,
        workspace_root=workspace,
        now="2026-07-19T00:00:05+00:00",
    )
    check = _worker_authority_check(snapshot)

    assert snapshot.active_count == 1
    assert snapshot.heartbeat_store_health == "ready"
    assert snapshot.workers[0].heartbeat_health == "healthy"
    assert snapshot.workers[0].heartbeat_age_seconds == 3
    assert check.status == "pass"
    assert "epoch 3" in check.detail
    assert "linux/x86_64" in check.detail
    assert "容量 4" in check.detail
    assert "心跳健康/3.0s" in check.detail
    assert "secret-bearing-internal-code" not in check.detail
    assert registry.read_bytes() == registry_before
    assert harness.read_bytes() == harness_before

    typed = build_doctor_health_snapshot(DoctorReport(checks=(check,)))
    assert typed.items[0].domain == "runtime"
    assert typed.items[0].responsibility == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("instance_id", "observed_at", "now", "expected_health"),
    [
        ("process-1", "2026-07-19T00:00:02+00:00", "2026-07-19T00:00:20+00:00", "stale"),
        (
            "old-process",
            "2026-07-19T00:00:02+00:00",
            "2026-07-19T00:00:05+00:00",
            "identity_mismatch",
        ),
    ],
)
async def test_stale_or_wrong_incarnation_fails_closed(
    tmp_path: Path,
    instance_id: str,
    observed_at: str,
    now: str,
    expected_health: str,
) -> None:
    registry = tmp_path / "worker-registry.db"
    harness = tmp_path / "harness.db"
    contract = _contract()
    await WorkerRegistryStore(registry).register(contract, registered_at=T1)
    await HarnessStore(harness).record_heartbeat(
        workspace_root=tmp_path,
        subject_kind="tool",
        subject_id=contract.worker_id,
        instance_id=instance_id,
        epoch=contract.epoch,
        sequence=1,
        phase="running",
        observed_at=observed_at,
        timeout_seconds=10,
    )

    snapshot = inspect_worker_authority_health(
        registry_db_path=registry,
        harness_db_path=harness,
        workspace_root=tmp_path,
        now=now,
    )
    check = _worker_authority_check(snapshot)

    assert snapshot.workers[0].heartbeat_health == expected_health
    assert check.status == "error"
    assert "暂停新任务派发" in check.suggestion
    typed = build_doctor_health_snapshot(DoctorReport(checks=(check,)))
    assert typed.items[0].responsibility == "product_runtime"


@pytest.mark.asyncio
async def test_active_worker_missing_then_starting_heartbeat_is_explicit(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "worker-registry.db"
    harness = tmp_path / "harness.db"
    contract = _contract()
    await WorkerRegistryStore(registry).register(contract, registered_at=T1)

    missing = inspect_worker_authority_health(
        registry_db_path=registry,
        harness_db_path=harness,
        workspace_root=tmp_path,
        now="2026-07-19T00:00:05+00:00",
    )
    assert missing.workers[0].heartbeat_health == "missing"
    assert _worker_authority_check(missing).status == "error"
    assert not harness.exists()

    await HarnessStore(harness).record_heartbeat(
        workspace_root=tmp_path,
        subject_kind="tool",
        subject_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        sequence=1,
        phase="starting",
        observed_at="2026-07-19T00:00:04+00:00",
        timeout_seconds=10,
    )
    starting = inspect_worker_authority_health(
        registry_db_path=registry,
        harness_db_path=harness,
        workspace_root=tmp_path,
        now="2026-07-19T00:00:05+00:00",
    )

    assert starting.workers[0].heartbeat_health == "starting"
    check = _worker_authority_check(starting)
    assert check.status == "warn"
    assert "心跳启动中" in check.detail


@pytest.mark.parametrize("mode", ["future", "corrupt", "directory"])
def test_registry_damage_is_bounded_and_never_repaired(tmp_path: Path, mode: str) -> None:
    registry = tmp_path / "worker-registry.db"
    harness = tmp_path / "harness.db"
    if mode == "future":
        with sqlite3.connect(registry) as db:
            db.execute(f"PRAGMA user_version = {WORKER_REGISTRY_SCHEMA_VERSION + 1}")
        before = registry.read_bytes()
    elif mode == "corrupt":
        registry.write_bytes(b"not-a-sqlite-database")
        before = registry.read_bytes()
    else:
        registry.mkdir()
        before = None

    with pytest.raises(WorkerAuthorityHealthError) as raised:
        inspect_worker_authority_health(
            registry_db_path=registry,
            harness_db_path=harness,
            workspace_root=tmp_path,
            now=T1,
        )

    assert raised.value.code in {
        "registry_schema_incompatible",
        "registry_unreadable",
        "registry_wrong_type",
    }
    if before is not None:
        assert registry.read_bytes() == before
    assert not harness.exists()


@pytest.mark.asyncio
async def test_future_heartbeat_schema_keeps_registry_facts_but_marks_unavailable(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "worker-registry.db"
    harness = tmp_path / "harness.db"
    await WorkerRegistryStore(registry).register(_contract(), registered_at=T1)
    with sqlite3.connect(harness) as db:
        db.execute(f"PRAGMA user_version = {HARNESS_STORE_SCHEMA_VERSION + 1}")
    before = harness.read_bytes()

    snapshot = inspect_worker_authority_health(
        registry_db_path=registry,
        harness_db_path=harness,
        workspace_root=tmp_path,
        now=T1,
    )
    check = _worker_authority_check(snapshot)

    assert snapshot.heartbeat_store_health == "incompatible"
    assert snapshot.workers[0].heartbeat_health == "unavailable"
    assert check.status == "error"
    assert "版本不兼容" in check.detail
    assert harness.read_bytes() == before
