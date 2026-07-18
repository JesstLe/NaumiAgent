from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import pytest

from naumi_agent.harness.conversation_queue_runtime import (
    ConversationQueueClaim,
    ConversationQueueClaimError,
    DurableConversationQueueAuthority,
)
from naumi_agent.harness.store import HarnessStore

T0 = "2026-07-18T00:00:00+00:00"
T1 = "2026-07-18T00:00:01+00:00"
T10 = "2026-07-18T00:00:10+00:00"
T31 = "2026-07-18T00:00:31+00:00"


def _authority(tmp_path, *, owner: str = "bridge-a"):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return DurableConversationQueueAuthority(
        store=HarnessStore(tmp_path / "harness.db"),
        workspace_root=workspace,
        session_id="session-1",
        owner_id=owner,
        lease_seconds=30,
    )


@pytest.mark.asyncio
async def test_claim_finish_is_fenced_atomic_and_audited(tmp_path) -> None:
    authority = _authority(tmp_path)
    item = await authority.enqueue(
        request_id="submit-1",
        text="持久消息",
        client_id="terminal-a",
        now=T0,
    )

    claim = await authority.claim(item, now=T0)
    renewed = await authority.renew(claim, now=T10)
    finished = await authority.finish(
        renewed,
        state="completed",
        terminal_reason="run_completed",
        now=T10,
    )

    assert finished.state == "completed"
    recovery = await authority.recover()
    assert recovery.ready == ()
    assert recovery.blocked == ()
    assert recovery.blocker_code == ""
    lease = await authority.store.get_run_lease(
        workspace_root=authority.workspace_root,
        run_kind="runtime",
        run_id=renewed.lease.run_id,
    )
    assert lease is not None and lease.state.value == "released"
    with sqlite3.connect(authority.store.db_path) as db:
        fence = db.execute(
            "SELECT decision, reason FROM harness_run_fence_events "
            "WHERE run_id = ?",
            (renewed.lease.run_id,),
        ).fetchone()
    assert fence == ("accepted", "current")


@pytest.mark.asyncio
async def test_existing_or_expired_claim_blocks_automatic_recovery(tmp_path) -> None:
    authority = _authority(tmp_path)
    first = await authority.enqueue(
        request_id="first", text="第一条", client_id="terminal-a", now=T0,
    )
    second = await authority.enqueue(
        request_id="second", text="第二条", client_id="terminal-a", now=T1,
    )
    await authority.claim(first, now=T0)

    recovery = await _authority(tmp_path, owner="bridge-b").recover()

    assert recovery.ready == ()
    assert recovery.blocked == (first, second)
    assert recovery.blocker_code == "queue_claim_ambiguous"
    with pytest.raises(ConversationQueueClaimError, match="历史 claim"):
        await _authority(tmp_path, owner="bridge-b").claim(first, now=T31)


@pytest.mark.asyncio
async def test_only_one_authority_can_claim_one_item(tmp_path) -> None:
    first = _authority(tmp_path, owner="bridge-a")
    second = _authority(tmp_path, owner="bridge-b")
    item = await first.enqueue(
        request_id="submit-1", text="并发领取", client_id="terminal-a", now=T0,
    )

    results = await asyncio.gather(
        first.claim(item, now=T0),
        second.claim(item, now=T0),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, ConversationQueueClaimError) for result in results) == 1


@pytest.mark.asyncio
async def test_stale_owner_cannot_finish_after_lease_expiry(tmp_path) -> None:
    authority = _authority(tmp_path)
    item = await authority.enqueue(
        request_id="submit-1", text="过期领取", client_id="terminal-a", now=T0,
    )
    claim = await authority.claim(item, now=T0)

    with pytest.raises(ConversationQueueClaimError, match="expired"):
        await authority.finish(
            claim,
            state="failed",
            terminal_reason="runtime_failed",
            now=T31,
        )
    recovery = await authority.recover()
    assert recovery.ready == ()
    assert recovery.blocked == (item,)


@pytest.mark.asyncio
async def test_wrong_claim_epoch_cannot_terminalize_or_release_item(tmp_path) -> None:
    authority = _authority(tmp_path)
    item = await authority.enqueue(
        request_id="submit-1", text="旧 epoch", client_id="terminal-a", now=T0,
    )
    claim = await authority.claim(item, now=T0)
    forged = ConversationQueueClaim(
        item=item,
        lease=replace(claim.lease, epoch=claim.lease.epoch + 1),
    )

    with pytest.raises(ConversationQueueClaimError, match="epoch_mismatch"):
        await authority.finish(
            forged,
            state="completed",
            terminal_reason="run_completed",
            now=T1,
        )
    assert await authority.store.list_queued_conversations(
        workspace_root=authority.workspace_root,
        session_id=authority.session_id,
    ) == (item,)
    lease = await authority.store.get_run_lease(
        workspace_root=authority.workspace_root,
        run_kind="runtime",
        run_id=claim.lease.run_id,
    )
    assert lease is not None and lease.state.value == "active"


@pytest.mark.asyncio
async def test_shutdown_cancel_only_accepts_never_claimed_item(tmp_path) -> None:
    authority = _authority(tmp_path)
    first = await authority.enqueue(
        request_id="first", text="未派发", client_id="terminal-a", now=T0,
    )
    cancelled = await authority.cancel_unclaimed(
        first, reason="ui_shutdown", now=T1,
    )
    assert cancelled.state == "cancelled"

    second = await authority.enqueue(
        request_id="second", text="已派发", client_id="terminal-a", now=T1,
    )
    await authority.claim(second, now=T1)
    with pytest.raises(ConversationQueueClaimError, match="派发边界"):
        await authority.cancel_unclaimed(second, reason="ui_shutdown", now=T10)
