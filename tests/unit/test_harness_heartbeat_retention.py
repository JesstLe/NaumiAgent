from __future__ import annotations

import asyncio

import pytest

from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore

T0 = "2026-07-20T00:00:00+00:00"
T1 = "2026-07-20T00:00:01+00:00"
T50 = "2026-07-20T00:00:50+00:00"
T99 = "2026-07-20T00:01:39+00:00"
T100 = "2026-07-20T00:01:40+00:00"


async def _record(
    store: HarnessStore,
    workspace,
    subject_id: str,
    *,
    kind: HarnessRunKind = HarnessRunKind.RUNTIME,
    phase: HarnessHeartbeatPhase = HarnessHeartbeatPhase.RUNNING,
    observed_at: str = T0,
    sequence: int = 1,
) -> None:
    await store.record_heartbeat(
        workspace_root=workspace,
        subject_kind=kind,
        subject_id=subject_id,
        instance_id=f"instance-{subject_id}",
        epoch=1,
        sequence=sequence,
        phase=phase,
        observed_at=observed_at,
        timeout_seconds=3,
        detail_code="test",
    )


@pytest.mark.asyncio
async def test_prune_runtime_heartbeats_is_scoped_bounded_and_fail_closed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _record(store, workspace, "old-offline")
    await _record(
        store,
        workspace,
        "old-stopped",
        phase=HarnessHeartbeatPhase.STOPPED,
        observed_at=T1,
    )
    await _record(store, workspace, "protected-offline")
    await _record(store, workspace, "fresh-running", observed_at=T99)
    await _record(
        store,
        workspace,
        "pursuit-old",
        kind=HarnessRunKind.PURSUIT,
    )

    first = await store.prune_runtime_heartbeats(
        workspace_root=workspace,
        observed_before=T50,
        assessed_at=T100,
        limit=1,
        protected_subject_ids=("protected-offline", "protected-offline"),
    )
    assert first.scanned_count == 1
    assert first.deleted_count == 1
    assert first.deleted_subject_ids == ("old-offline",)
    assert first.protected_subject_ids == ("protected-offline",)

    second = await store.prune_runtime_heartbeats(
        workspace_root=workspace,
        observed_before=T50,
        assessed_at=T100,
        limit=10,
        protected_subject_ids=("protected-offline",),
    )
    assert second.deleted_subject_ids == ("old-stopped",)
    for subject_id, kind in (
        ("protected-offline", HarnessRunKind.RUNTIME),
        ("fresh-running", HarnessRunKind.RUNTIME),
        ("pursuit-old", HarnessRunKind.PURSUIT),
    ):
        assert await store.get_heartbeat(
            workspace_root=workspace,
            subject_kind=kind,
            subject_id=subject_id,
        ) is not None


@pytest.mark.asyncio
async def test_prune_runtime_heartbeats_preserves_stale_but_not_offline(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _record(store, workspace, "stale-only", observed_at=T99)

    receipt = await store.prune_runtime_heartbeats(
        workspace_root=workspace,
        observed_before=T100,
        assessed_at="2026-07-20T00:01:43+00:00",
    )
    assert receipt.scanned_count == 1
    assert receipt.deleted_count == 0
    assert await store.get_heartbeat(
        workspace_root=workspace,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id="stale-only",
    ) is not None


@pytest.mark.asyncio
async def test_prune_and_live_pulse_cannot_permanently_delete_runtime(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    initial = HarnessStore(db_path)
    await _record(initial, workspace, "racing-runtime")
    pruner = HarnessStore(db_path)
    pulser = HarnessStore(db_path)

    receipt, _ = await asyncio.gather(
        pruner.prune_runtime_heartbeats(
            workspace_root=workspace,
            observed_before=T50,
            assessed_at=T100,
        ),
        pulser.record_heartbeat(
            workspace_root=workspace,
            subject_kind=HarnessRunKind.RUNTIME,
            subject_id="racing-runtime",
            instance_id="instance-racing-runtime",
            epoch=1,
            sequence=2,
            phase=HarnessHeartbeatPhase.RUNNING,
            observed_at=T100,
            timeout_seconds=3,
            detail_code="runtime_alive",
        ),
    )
    assert receipt.deleted_count in {0, 1}
    current = await HarnessStore(db_path).get_heartbeat(
        workspace_root=workspace,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id="racing-runtime",
    )
    assert current is not None
    assert current.sequence == 2
    assert current.observed_at == T100


@pytest.mark.asyncio
async def test_prune_runtime_heartbeats_rejects_unsafe_bounds(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    with pytest.raises(ValueError, match="cutoff"):
        await store.prune_runtime_heartbeats(
            workspace_root=tmp_path,
            observed_before=T100,
            assessed_at=T100,
        )
    with pytest.raises(ValueError, match="limit"):
        await store.prune_runtime_heartbeats(
            workspace_root=tmp_path,
            observed_before=T50,
            assessed_at=T100,
            limit=0,
        )
    with pytest.raises(ValueError, match="ID 序列"):
        await store.prune_runtime_heartbeats(
            workspace_root=tmp_path,
            observed_before=T50,
            assessed_at=T100,
            protected_subject_ids="runtime-1",
        )
