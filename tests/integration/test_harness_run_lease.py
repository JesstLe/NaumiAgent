"""Real cross-instance SQLite tests for HAR-10.1a run fencing leases."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from naumi_agent.harness.run_lease import (
    HarnessRunFenceDecision,
    HarnessRunFenceReason,
    HarnessRunKind,
    HarnessRunLeaseState,
)
from naumi_agent.harness.store import (
    HARNESS_STORE_SCHEMA_VERSION,
    HarnessStore,
    HarnessStoreConflictError,
)

T0 = "2026-07-18T00:00:00+00:00"
T1 = "2026-07-18T00:00:01+00:00"
T5 = "2026-07-18T00:00:05+00:00"
T31 = "2026-07-18T00:00:31+00:00"
T32 = "2026-07-18T00:00:32+00:00"
T33 = "2026-07-18T00:00:33+00:00"
T34 = "2026-07-18T00:00:34+00:00"


@pytest.mark.asyncio
async def test_three_instances_get_one_owner_and_takeover_fences_old_epoch(
    tmp_path,
) -> None:
    db_path = tmp_path / "harness.db"
    bootstrap = HarnessStore(db_path)
    await bootstrap.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id="bootstrap",
        owner_id="bootstrap",
        now=T0,
        lease_seconds=1,
    )
    stores = [HarnessStore(db_path) for _ in range(3)]
    owners = ["worker-a", "worker-b", "worker-c"]

    claims = await asyncio.gather(*[
        store.acquire_run_lease(
            workspace_root=tmp_path,
            run_kind=HarnessRunKind.PURSUIT,
            run_id="pursuit-1",
            owner_id=owner,
            now=T0,
            lease_seconds=30,
        )
        for store, owner in zip(stores, owners, strict=True)
    ])

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    first = winners[0]
    assert first.epoch == 1
    assert first.state is HarnessRunLeaseState.ACTIVE
    winner_store = stores[owners.index(first.owner_id)]

    idempotent = await winner_store.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind="pursuit",
        run_id="pursuit-1",
        owner_id=first.owner_id,
        now=T5,
        lease_seconds=1,
    )
    assert idempotent is not None
    assert idempotent.epoch == first.epoch
    assert idempotent.acquired_at == first.acquired_at
    assert idempotent.expires_at == first.expires_at

    loser = next(owner for owner in owners if owner != first.owner_id)
    takeover = await HarnessStore(db_path).acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-1",
        owner_id=loser,
        now=T31,
        lease_seconds=30,
    )
    assert takeover is not None
    assert takeover.owner_id == loser
    assert takeover.epoch == 2

    stale = await HarnessStore(db_path).record_run_fence_decision(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-1",
        operation_id="commit-old-result",
        owner_id=first.owner_id,
        epoch=first.epoch,
        checked_at=T32,
    )
    assert stale.decision is HarnessRunFenceDecision.REJECTED
    assert stale.reason is HarnessRunFenceReason.OWNER_MISMATCH
    assert stale.active_owner_id == loser
    assert stale.active_epoch == 2

    accepted = await HarnessStore(db_path).record_run_fence_decision(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-1",
        operation_id="commit-current-result",
        owner_id=loser,
        epoch=takeover.epoch,
        checked_at=T32,
    )
    assert accepted.accepted is True
    assert accepted.reason is HarnessRunFenceReason.CURRENT

    repeated = await HarnessStore(db_path).record_run_fence_decision(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.PURSUIT,
        run_id="pursuit-1",
        operation_id="commit-old-result",
        owner_id=first.owner_id,
        epoch=first.epoch,
        checked_at=T34,
    )
    assert repeated == stale
    with pytest.raises(HarnessStoreConflictError, match="operation_id"):
        await HarnessStore(db_path).record_run_fence_decision(
            workspace_root=tmp_path,
            run_kind=HarnessRunKind.PURSUIT,
            run_id="pursuit-1",
            operation_id="commit-old-result",
            owner_id=loser,
            epoch=takeover.epoch,
            checked_at=T34,
        )

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT decision, reason FROM harness_run_fence_events "
            "WHERE run_id = 'pursuit-1' ORDER BY operation_id"
        ).fetchall()
    assert rows == [("accepted", "current"), ("rejected", "owner_mismatch")]


@pytest.mark.asyncio
async def test_release_preserves_epoch_and_rejects_stale_same_owner(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    first = await store.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.BROWSER,
        run_id="browser-job",
        owner_id="browser-worker",
        now=T0,
        lease_seconds=30,
    )
    assert first is not None
    renewed = await store.renew_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.BROWSER,
        run_id="browser-job",
        owner_id=first.owner_id,
        epoch=first.epoch,
        now=T1,
        lease_seconds=1,
    )
    assert renewed is not None
    assert renewed.epoch == first.epoch
    assert renewed.expires_at == first.expires_at
    released = await store.release_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.BROWSER,
        run_id="browser-job",
        owner_id=first.owner_id,
        epoch=first.epoch,
        now=T1,
    )
    assert released is not None
    assert released.state is HarnessRunLeaseState.RELEASED
    released_result = await store.record_run_fence_decision(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.BROWSER,
        run_id="browser-job",
        operation_id="released-browser-output",
        owner_id=first.owner_id,
        epoch=first.epoch,
        checked_at=T1,
    )
    assert released_result.reason is HarnessRunFenceReason.RELEASED

    second = await store.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.BROWSER,
        run_id="browser-job",
        owner_id=first.owner_id,
        now=T1,
        lease_seconds=30,
    )
    assert second is not None
    assert second.epoch == first.epoch + 1
    stale = await store.record_run_fence_decision(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.BROWSER,
        run_id="browser-job",
        operation_id="late-browser-output",
        owner_id=first.owner_id,
        epoch=first.epoch,
        checked_at=T5,
    )
    assert stale.reason is HarnessRunFenceReason.EPOCH_MISMATCH


@pytest.mark.asyncio
async def test_renewal_rejects_wrong_epoch_expiry_and_clock_regression(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    lease = await store.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.AGENT,
        run_id="agent-run",
        owner_id="agent-a",
        now=T5,
        lease_seconds=25,
    )
    assert lease is not None
    assert await store.renew_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.AGENT,
        run_id="agent-run",
        owner_id="agent-a",
        epoch=lease.epoch + 1,
        now=T31,
        lease_seconds=30,
    ) is None
    assert await store.renew_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.AGENT,
        run_id="agent-run",
        owner_id="agent-a",
        epoch=lease.epoch,
        now=T1,
        lease_seconds=30,
    ) is None
    regressed = await store.record_run_fence_decision(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.AGENT,
        run_id="agent-run",
        operation_id="regressed-result",
        owner_id="agent-a",
        epoch=lease.epoch,
        checked_at=T1,
    )
    assert regressed.reason is HarnessRunFenceReason.CLOCK_REGRESSION
    expired = await store.record_run_fence_decision(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.AGENT,
        run_id="agent-run",
        operation_id="expired-result",
        owner_id="agent-a",
        epoch=lease.epoch,
        checked_at=T31,
    )
    assert expired.reason is HarnessRunFenceReason.EXPIRED


@pytest.mark.asyncio
async def test_same_run_id_is_isolated_by_workspace_and_kind(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"

    leases = await asyncio.gather(
        store.acquire_run_lease(
            workspace_root=workspace_a,
            run_kind=HarnessRunKind.TOOL,
            run_id="shared-id",
            owner_id="tool-a",
            now=T0,
            lease_seconds=30,
        ),
        store.acquire_run_lease(
            workspace_root=workspace_b,
            run_kind=HarnessRunKind.TOOL,
            run_id="shared-id",
            owner_id="tool-b",
            now=T0,
            lease_seconds=30,
        ),
        store.acquire_run_lease(
            workspace_root=workspace_a,
            run_kind=HarnessRunKind.RUNTIME,
            run_id="shared-id",
            owner_id="runtime-a",
            now=T0,
            lease_seconds=30,
        ),
    )

    assert all(item is not None and item.epoch == 1 for item in leases)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("run_kind", "unknown", "run_kind"),
        ("run_id", "bad/run", "run_id"),
        ("owner_id", "bad owner", "owner_id"),
    ],
)
async def test_run_lease_rejects_invalid_identity_fields(
    tmp_path,
    field: str,
    value: str,
    message: str,
) -> None:
    kwargs = {
        "workspace_root": tmp_path,
        "run_kind": HarnessRunKind.RUNTIME,
        "run_id": "runtime-1",
        "owner_id": "runtime-a",
        "now": T0,
        "lease_seconds": 30,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=message):
        await HarnessStore(tmp_path / "harness.db").acquire_run_lease(**kwargs)


@pytest.mark.asyncio
async def test_run_lease_rejects_boolean_epoch(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    lease = await store.acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id="runtime-1",
        owner_id="runtime-a",
        now=T0,
        lease_seconds=30,
    )
    assert lease is not None

    with pytest.raises(ValueError, match="epoch"):
        await store.renew_run_lease(
            workspace_root=tmp_path,
            run_kind=HarnessRunKind.RUNTIME,
            run_id="runtime-1",
            owner_id="runtime-a",
            epoch=True,
            now=T1,
            lease_seconds=30,
        )


@pytest.mark.asyncio
async def test_schema_v10_migrates_additively_to_run_leases(tmp_path) -> None:
    db_path = tmp_path / "harness.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        db.execute("INSERT INTO legacy_marker (value) VALUES ('preserved')")
        db.execute("PRAGMA user_version = 10")
        db.commit()

    lease = await HarnessStore(db_path).acquire_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id="runtime-migrated",
        owner_id="runtime-a",
        now=T0,
        lease_seconds=30,
    )

    assert lease is not None
    with sqlite3.connect(db_path) as db:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        marker = db.execute("SELECT value FROM legacy_marker").fetchone()[0]
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert version == HARNESS_STORE_SCHEMA_VERSION == 16
    assert marker == "preserved"
    assert {"harness_run_leases", "harness_run_fence_events"} <= tables
