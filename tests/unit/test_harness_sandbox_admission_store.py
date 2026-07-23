from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.harness.store import (
    HarnessSandboxAdmissionCapacityError,
    HarnessSandboxAdmissionFenceError,
    HarnessSandboxAdmissionPolicyError,
    HarnessStore,
    HarnessStoreError,
)

_T0 = "2026-07-23T00:00:00+00:00"
_T1 = "2026-07-23T00:00:01+00:00"
_T2_5 = "2026-07-23T00:00:02.500000+00:00"
_T3 = "2026-07-23T00:00:03+00:00"


async def _enqueue(
    store: HarnessStore,
    workspace: Path,
    *,
    suffix: str,
    owner: str,
    now: str = _T0,
    lease_seconds: int = 10,
    max_active: int = 1,
    max_queued: int = 1,
):
    return await store.enqueue_sandbox_admission(
        workspace_root=workspace,
        ticket_id=f"hsadm_{suffix * 24}",
        authority_key=suffix * 64,
        lane="sandbox",
        requested_samples=5,
        owner_id=owner,
        now=now,
        lease_seconds=lease_seconds,
        max_active=max_active,
        max_queued=max_queued,
    )


@pytest.mark.asyncio
async def test_durable_admission_bounds_capacity_and_promotes_fifo_across_instances(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "harness.db"
    first_store = HarnessStore(db_path)
    second_store = HarnessStore(db_path)

    first, second = await asyncio.gather(
        _enqueue(first_store, workspace, suffix="a", owner="worker-a"),
        _enqueue(second_store, workspace, suffix="b", owner="worker-b"),
    )
    active = first if first.state == "active" else second
    queued = second if active is first else first

    assert active.active_count == 1
    assert queued.state == "queued"
    assert queued.queue_position == 1
    with pytest.raises(HarnessSandboxAdmissionCapacityError, match="等待队列已满"):
        await _enqueue(
            HarnessStore(db_path),
            workspace,
            suffix="c",
            owner="worker-c",
        )

    await first_store.finish_sandbox_admission(
        workspace_root=workspace,
        ticket_id=active.ticket_id,
        owner_id=active.owner_id,
        epoch=active.epoch,
        state="completed",
        terminal_code="",
        now=_T1,
    )
    promoted = await second_store.poll_sandbox_admission(
        workspace_root=workspace,
        ticket_id=queued.ticket_id,
        owner_id=queued.owner_id,
        epoch=queued.epoch,
        now=_T1,
        lease_seconds=10,
    )

    assert promoted.state == "active"
    assert promoted.queue_position == 0
    assert promoted.active_count == 1
    assert promoted.queued_count == 0


@pytest.mark.asyncio
async def test_expired_active_ticket_is_fenced_and_next_live_waiter_recovers(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    expired = await _enqueue(
        store,
        workspace,
        suffix="d",
        owner="worker-expired",
        lease_seconds=2,
    )
    waiting = await _enqueue(
        HarnessStore(store.db_path),
        workspace,
        suffix="e",
        owner="worker-live",
        now=_T1,
        lease_seconds=2,
    )

    promoted = await HarnessStore(store.db_path).poll_sandbox_admission(
        workspace_root=workspace,
        ticket_id=waiting.ticket_id,
        owner_id=waiting.owner_id,
        epoch=waiting.epoch,
        now=_T2_5,
        lease_seconds=2,
    )
    assert promoted.state == "active"
    assert promoted.active_count == 1
    assert promoted.queued_count == 0

    with pytest.raises(HarnessSandboxAdmissionFenceError, match="已终止"):
        await store.poll_sandbox_admission(
            workspace_root=workspace,
            ticket_id=expired.ticket_id,
            owner_id=expired.owner_id,
            epoch=expired.epoch,
            now=_T2_5,
            lease_seconds=2,
        )
    observed = await store.get_sandbox_admission(
        workspace_root=workspace,
        ticket_id=expired.ticket_id,
        now=_T2_5,
    )
    assert observed is not None
    assert observed.state == "expired"
    assert observed.terminal_code == "sandbox_batch_admission_lease_expired"


@pytest.mark.asyncio
async def test_live_policy_change_fails_closed_then_succeeds_after_terminal(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    active = await _enqueue(
        store,
        workspace,
        suffix="f",
        owner="worker-policy",
    )

    with pytest.raises(HarnessSandboxAdmissionPolicyError, match="配置"):
        await _enqueue(
            HarnessStore(store.db_path),
            workspace,
            suffix="1",
            owner="worker-other-policy",
            max_active=2,
            max_queued=2,
        )

    await store.finish_sandbox_admission(
        workspace_root=workspace,
        ticket_id=active.ticket_id,
        owner_id=active.owner_id,
        epoch=active.epoch,
        state="cancelled",
        terminal_code="sandbox_batch_cancelled",
        now=_T1,
    )
    replacement = await _enqueue(
        HarnessStore(store.db_path),
        workspace,
        suffix="2",
        owner="worker-new-policy",
        now=_T3,
        max_active=2,
        max_queued=2,
    )
    snapshot = await store.sandbox_admission_snapshot(
        workspace_root=workspace,
        now=_T3,
    )

    assert replacement.state == "active"
    assert snapshot is not None
    assert (snapshot.max_active, snapshot.max_queued) == (2, 2)


@pytest.mark.asyncio
async def test_terminal_write_is_idempotent_but_wrong_epoch_is_fenced(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    active = await _enqueue(
        store,
        workspace,
        suffix="3",
        owner="worker-terminal",
    )
    first = await store.finish_sandbox_admission(
        workspace_root=workspace,
        ticket_id=active.ticket_id,
        owner_id=active.owner_id,
        epoch=active.epoch,
        state="failed",
        terminal_code="sample_failed",
        now=_T1,
    )
    replay = await HarnessStore(store.db_path).finish_sandbox_admission(
        workspace_root=workspace,
        ticket_id=active.ticket_id,
        owner_id=active.owner_id,
        epoch=active.epoch,
        state="failed",
        terminal_code="sample_failed",
        now=_T3,
    )

    assert replay == first
    with pytest.raises(HarnessSandboxAdmissionFenceError, match="已终止"):
        await store.finish_sandbox_admission(
            workspace_root=workspace,
            ticket_id=active.ticket_id,
            owner_id=active.owner_id,
            epoch=active.epoch + 1,
            state="failed",
            terminal_code="sample_failed",
            now=_T3,
        )


@pytest.mark.asyncio
async def test_tampered_admission_request_digest_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    active = await _enqueue(
        store,
        workspace,
        suffix="4",
        owner="worker-tamper",
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_admission_tickets
            SET request_sha256 = ?
            WHERE workspace_root = ? AND ticket_id = ?
            """,
            ("0" * 64, str(workspace.resolve()), active.ticket_id),
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="摘要"):
        await store.poll_sandbox_admission(
            workspace_root=workspace,
            ticket_id=active.ticket_id,
            owner_id=active.owner_id,
            epoch=active.epoch,
            now=_T1,
            lease_seconds=10,
        )
