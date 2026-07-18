from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from naumi_agent.harness.heartbeat import (
    HarnessHeartbeat,
    HarnessHeartbeatHealth,
    HarnessHeartbeatPhase,
    assess_heartbeat,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import (
    HARNESS_STORE_SCHEMA_VERSION,
    HarnessStore,
    HarnessStoreConflictError,
)

T0 = "2026-07-18T00:00:00+00:00"
T4 = "2026-07-18T00:00:04+00:00"
T11 = "2026-07-18T00:00:11+00:00"
T31 = "2026-07-18T00:00:31+00:00"


def _heartbeat(
    *,
    phase: HarnessHeartbeatPhase = HarnessHeartbeatPhase.RUNNING,
) -> HarnessHeartbeat:
    return HarnessHeartbeat(
        workspace_root="/workspace",
        subject_kind=HarnessRunKind.PURSUIT,
        subject_id="run-1",
        instance_id="worker-1",
        epoch=1,
        sequence=1,
        phase=phase,
        observed_at=T0,
        timeout_seconds=10,
        detail_code="lease_active",
    )


@pytest.mark.parametrize(
    ("now", "phase", "expected"),
    [
        (T4, HarnessHeartbeatPhase.STARTING, HarnessHeartbeatHealth.STARTING),
        (T4, HarnessHeartbeatPhase.RUNNING, HarnessHeartbeatHealth.HEALTHY),
        (T4, HarnessHeartbeatPhase.DRAINING, HarnessHeartbeatHealth.DRAINING),
        (T11, HarnessHeartbeatPhase.RUNNING, HarnessHeartbeatHealth.STALE),
        (T31, HarnessHeartbeatPhase.RUNNING, HarnessHeartbeatHealth.OFFLINE),
        (T31, HarnessHeartbeatPhase.STOPPED, HarnessHeartbeatHealth.STOPPED),
        (T31, HarnessHeartbeatPhase.FAILED, HarnessHeartbeatHealth.FAILED),
    ],
)
def test_assess_heartbeat_has_mechanical_health_thresholds(
    now: str,
    phase: HarnessHeartbeatPhase,
    expected: HarnessHeartbeatHealth,
) -> None:
    snapshot = assess_heartbeat(_heartbeat(phase=phase), now=now)

    assert snapshot.health is expected
    assert snapshot.age_seconds >= 0


def test_assess_heartbeat_detects_clock_regression_and_invalid_time() -> None:
    regressed = assess_heartbeat(
        _heartbeat(),
        now="2026-07-17T23:59:59+00:00",
    )
    assert regressed.health is HarnessHeartbeatHealth.CLOCK_REGRESSION
    assert regressed.age_seconds == 0

    with pytest.raises(ValueError, match="时区偏移"):
        assess_heartbeat(_heartbeat(), now="2026-07-18T00:00:01")
    with pytest.raises(ValueError, match="offline_multiplier"):
        assess_heartbeat(_heartbeat(), now=T4, offline_multiplier=0.5)
    with pytest.raises(ValueError, match="offline_multiplier"):
        assess_heartbeat(_heartbeat(), now=T4, offline_multiplier=float("nan"))
    with pytest.raises(ValueError, match="timeout_seconds"):
        assess_heartbeat(
            replace(_heartbeat(), timeout_seconds=0),
            now=T4,
        )


@pytest.mark.asyncio
async def test_store_round_trip_is_idempotent_and_survives_reopen(tmp_path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)
    kwargs = {
        "workspace_root": workspace,
        "subject_kind": HarnessRunKind.PURSUIT,
        "subject_id": "run-1",
        "instance_id": "worker-1",
        "epoch": 1,
        "sequence": 1,
        "phase": HarnessHeartbeatPhase.RUNNING,
        "observed_at": T0,
        "timeout_seconds": 10,
        "detail_code": "lease_active",
    }

    first = await store.record_heartbeat(**kwargs)
    replay = await store.record_heartbeat(**kwargs)
    reopened = await HarnessStore(db_path).get_heartbeat(
        workspace_root=workspace,
        subject_kind=HarnessRunKind.PURSUIT,
        subject_id="run-1",
    )

    assert replay == first
    assert reopened == first


@pytest.mark.asyncio
async def test_schema_v11_database_upgrades_to_current_without_reset(tmp_path) -> None:
    db_path = tmp_path / "harness.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        db.execute("INSERT INTO legacy_marker VALUES ('preserved')")
        db.execute("PRAGMA user_version = 11")
        db.commit()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    await HarnessStore(db_path).record_heartbeat(
        workspace_root=workspace,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id="runtime-1",
        instance_id="process-1",
        epoch=1,
        sequence=1,
        phase=HarnessHeartbeatPhase.STARTING,
        observed_at=T0,
        timeout_seconds=10,
        detail_code="booting",
    )

    with sqlite3.connect(db_path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        marker = db.execute("SELECT value FROM legacy_marker").fetchone()[0]
        heartbeat_count = db.execute("SELECT COUNT(*) FROM harness_heartbeats").fetchone()[0]
    assert version == HARNESS_STORE_SCHEMA_VERSION
    assert marker == "preserved"
    assert heartbeat_count == 1


@pytest.mark.asyncio
async def test_store_rejects_stale_sequence_identity_and_clock(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def record(**updates):
        values = {
            "workspace_root": workspace,
            "subject_kind": HarnessRunKind.PURSUIT,
            "subject_id": "run-1",
            "instance_id": "worker-1",
            "epoch": 1,
            "sequence": 1,
            "phase": HarnessHeartbeatPhase.RUNNING,
            "observed_at": T0,
            "timeout_seconds": 10,
            "detail_code": "lease_active",
        }
        values.update(updates)
        return await store.record_heartbeat(**values)

    await record()
    with pytest.raises(HarnessStoreConflictError, match="sequence"):
        await record(sequence=1, observed_at=T4)
    with pytest.raises(HarnessStoreConflictError, match="instance"):
        await record(instance_id="worker-2", sequence=2, observed_at=T4)
    with pytest.raises(HarnessStoreConflictError, match="observed_at"):
        await record(sequence=2, observed_at="2026-07-17T23:59:59+00:00")

    takeover = await record(
        instance_id="worker-2",
        epoch=2,
        sequence=1,
        observed_at=T4,
    )
    assert takeover.instance_id == "worker-2"
    assert takeover.epoch == 2
    with pytest.raises(HarnessStoreConflictError, match="epoch"):
        await record(sequence=3, observed_at=T11)


@pytest.mark.asyncio
async def test_store_validates_bounds_and_workspace_isolation(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    base = {
        "subject_kind": HarnessRunKind.RUNTIME,
        "subject_id": "runtime-1",
        "instance_id": "process-1",
        "epoch": 1,
        "sequence": 1,
        "phase": HarnessHeartbeatPhase.STARTING,
        "observed_at": T0,
        "timeout_seconds": 10,
        "detail_code": "booting",
    }
    await store.record_heartbeat(workspace_root=first, **base)
    assert (
        await store.get_heartbeat(
            workspace_root=second,
            subject_kind=HarnessRunKind.RUNTIME,
            subject_id="runtime-1",
        )
        is None
    )

    with pytest.raises(ValueError, match="timeout_seconds"):
        await store.record_heartbeat(
            workspace_root=first,
            **{**base, "timeout_seconds": 2},
        )
    with pytest.raises(ValueError, match="sequence"):
        await store.record_heartbeat(
            workspace_root=first,
            **{**base, "sequence": 0},
        )
