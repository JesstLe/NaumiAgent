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
async def test_owner_can_promote_unclaimed_tail_around_its_live_active_claim(
    tmp_path,
) -> None:
    authority = _authority(tmp_path)
    active = await authority.enqueue(
        request_id="active", text="执行中", client_id="terminal-a", now=T0,
    )
    await authority.enqueue(
        request_id="first", text="下一条", client_id="terminal-a", now=T1,
    )
    latest = await authority.enqueue(
        request_id="latest", text="立即发送", client_id="terminal-a", now=T10,
    )
    claim = await authority.claim(active, now=T0)

    promoted = await authority.promote(
        request_id=latest.request_id,
        active_claim=claim,
        now=T10,
    )
    queued = await authority.store.list_queued_conversations(
        workspace_root=authority.workspace_root,
        session_id=authority.session_id,
    )

    assert promoted.position == 1
    assert [item.request_id for item in queued] == ["latest", "active", "first"]
    with pytest.raises(ConversationQueueClaimError, match="尚未派发"):
        await authority.promote(
            request_id=active.request_id,
            active_claim=claim,
            now=T10,
        )


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


@pytest.mark.asyncio
async def test_live_claim_cannot_be_resolved(tmp_path) -> None:
    authority = _authority(tmp_path)
    item = await authority.enqueue(
        request_id="live", text="仍在运行", client_id="terminal-a", now=T0,
    )
    await authority.claim(item, now=T0)

    review = await _authority(tmp_path, owner="reviewer").review(
        request_id="live", now=T10,
    )
    assert review.resolvable is False
    assert review.resolution_code == "claim_live"
    with pytest.raises(ConversationQueueClaimError, match="活跃实例"):
        await _authority(tmp_path, owner="reviewer").resolve(
            request_id="live",
            action="retry",
            reason="用户确认重试",
            now=T10,
        )


@pytest.mark.asyncio
async def test_expired_claim_retry_is_atomic_audited_and_dispatchable(tmp_path) -> None:
    first = _authority(tmp_path)
    item = await first.enqueue(
        request_id="expired", text="需要重试", client_id="terminal-a", now=T0,
    )
    old_claim = await first.claim(item, now=T0)
    reviewer = _authority(tmp_path, owner="reviewer")

    resolution = await reviewer.resolve(
        request_id="expired",
        action="retry",
        reason="用户核对后重试",
        now=T31,
    )

    assert resolution.action == "retry"
    assert resolution.reviewed_epoch == old_claim.lease.epoch
    assert resolution.retry_request_id.startswith("retry-")
    queued = await reviewer.store.list_queued_conversations(
        workspace_root=reviewer.workspace_root,
        session_id=reviewer.session_id,
    )
    assert len(queued) == 1
    assert queued[0].request_id == resolution.retry_request_id
    assert queued[0].text == item.text
    assert (await reviewer.recover()).ready == queued
    new_claim = await reviewer.claim(queued[0], now=T31)
    assert new_claim.lease.epoch == 1
    with sqlite3.connect(reviewer.store.db_path) as db:
        audit = db.execute(
            "SELECT action, request_id, retry_request_id, reviewed_owner_id, "
            "reviewed_epoch, reason FROM harness_conversation_queue_resolutions"
        ).fetchone()
        old_state = db.execute(
            "SELECT state, terminal_reason FROM harness_conversation_queue "
            "WHERE request_id = 'expired'"
        ).fetchone()
    assert audit == (
        "retry",
        "expired",
        resolution.retry_request_id,
        old_claim.lease.owner_id,
        old_claim.lease.epoch,
        "用户核对后重试",
    )
    assert old_state == ("cancelled", "explicit_retry")


@pytest.mark.asyncio
async def test_released_claim_can_be_cancelled_idempotently(tmp_path) -> None:
    first = _authority(tmp_path)
    item = await first.enqueue(
        request_id="released", text="决定放弃", client_id="terminal-a", now=T0,
    )
    claim = await first.claim(item, now=T0)
    released = await first.store.release_run_lease(
        workspace_root=first.workspace_root,
        run_kind="runtime",
        run_id=claim.lease.run_id,
        owner_id=claim.lease.owner_id,
        epoch=claim.lease.epoch,
        now=T1,
    )
    assert released is not None
    reviewer = _authority(tmp_path, owner="reviewer")

    first_result = await reviewer.resolve(
        request_id="released",
        action="cancel",
        reason="用户确认无需执行",
        now=T10,
    )
    second_result = await reviewer.store.resolve_ambiguous_queued_conversation(
        workspace_root=reviewer.workspace_root,
        session_id=reviewer.session_id,
        request_id="released",
        action="cancel",
        retry_request_id="",
        reviewed_run_id=claim.lease.run_id,
        reviewed_owner_id=claim.lease.owner_id,
        reviewed_epoch=claim.lease.epoch,
        actor_id=reviewer.owner_id,
        reason="用户确认无需执行",
        resolved_at=T10,
    )

    assert second_result == first_result
    assert await reviewer.store.list_queued_conversations(
        workspace_root=reviewer.workspace_root,
        session_id=reviewer.session_id,
    ) == ()
