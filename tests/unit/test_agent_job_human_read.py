"""Same-JobStore human-read receipts preserve legacy acknowledgement authority."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest

from naumi_agent.daemons.agent_jobs import (
    AgentJobError,
    AgentJobLifecycleConflictError,
    AgentJobPayload,
    AgentJobState,
    AgentJobStore,
    AgentJobTerminalPayload,
)
from naumi_agent.daemons.agent_worker_contract import (
    issue_agent_worker_request,
    issue_agent_worker_result,
)


async def _running(tmp_path, session="execution-one"):
    jobs = AgentJobStore(tmp_path / "jobs.db", key_provider=lambda: b"x" * 32)
    payload = AgentJobPayload(
        task_id="task-one",
        session_id=session,
        task="整理资料",
        context="",
        message_topic="item-one",
    )
    request = issue_agent_worker_request(
        **{
            "task_id": payload.task_id,
            "session_id": payload.session_id,
            "task": payload.task,
            "context": payload.context,
            "message_topic": payload.message_topic,
        },
        agent_name="main",
        tool_scope=[],
        permission_mode="strict",
        model_tier="capable",
        max_turns=2,
        max_budget_usd=1,
        timeout_seconds=30,
        issued_at=datetime.now(UTC).isoformat(),
    )
    admitted = await jobs.admit(request=request, payload=payload)
    claimed = await jobs.claim(admitted.job_id, owner_id="owner", lease_seconds=90)
    await jobs.mark_running(admitted.job_id, owner_id="owner", claim_epoch=claimed.job.claim_epoch)
    return jobs, claimed.job, payload


async def _delivered(tmp_path):
    jobs, job, payload = await _running(tmp_path)
    terminal = AgentJobTerminalPayload(response="真实结果", error="")
    result = issue_agent_worker_result(
        request=job.request,
        status="completed",
        response=terminal.response,
        error="",
        total_tokens=17,
        total_cost_usd=0.001,
        turns=1,
        completed_at=datetime.now(UTC).isoformat(),
    )
    await jobs.finish(
        job.job_id,
        owner_id="owner",
        claim_epoch=job.claim_epoch,
        result=result,
        terminal_payload=terminal,
    )
    publication = await jobs.get_job_publication(job.job_id)
    claim = await jobs.claim_publication(
        publication.publication_id, owner_id="publisher", lease_seconds=90
    )
    transition = await jobs.deliver_publication_to_inbox(
        publication.publication_id, owner_id="publisher", claim_epoch=claim.publication.claim_epoch
    )
    return jobs, payload.session_id, transition.delivery


async def test_legacy_ack_never_implies_human_read_and_original_hash_is_unchanged(tmp_path):
    jobs, session, delivery = await _delivered(tmp_path)
    legacy = await jobs.acknowledge_result_inbox_delivery(
        session, delivery.delivery_id, expected_delivery_sha256=delivery.delivery_sha256
    )
    before = legacy.acknowledgement.receipt
    assert (
        await jobs.get_human_result_inbox_read(
            session, delivery.delivery_id, expected_delivery_sha256=delivery.delivery_sha256
        )
        is None
    )
    result = await jobs.acknowledge_human_result_inbox_delivery(
        session,
        delivery.delivery_id,
        expected_delivery_sha256=delivery.delivery_sha256,
        trusted_channel="management_web",
    )
    assert result["applied"] is True
    assert result["receipt"]["legacy_ack_receipt_sha256"] == before.receipt_sha256
    assert result["receipt"]["trusted_channel"] == "management_web"
    again = await jobs.acknowledge_result_inbox_delivery(
        session, delivery.delivery_id, expected_delivery_sha256=delivery.delivery_sha256
    )
    assert again.acknowledgement.receipt == before


async def test_concurrent_human_ack_has_one_immutable_winner_and_reopens(tmp_path):
    jobs, session, delivery = await _delivered(tmp_path)
    values = await asyncio.gather(
        *(
            jobs.acknowledge_human_result_inbox_delivery(
                session,
                delivery.delivery_id,
                expected_delivery_sha256=delivery.delivery_sha256,
                trusted_channel="manual_cli",
            )
            for _ in range(8)
        )
    )
    assert sum(value["applied"] for value in values) == 1
    assert all(value["receipt"] == values[0]["receipt"] for value in values)
    reopened = AgentJobStore(jobs.db_path, key_provider=lambda: b"x" * 32)
    assert (
        await reopened.get_human_result_inbox_read(
            session, delivery.delivery_id, expected_delivery_sha256=delivery.delivery_sha256
        )
        == values[0]["receipt"]
    )


@pytest.mark.parametrize("channel", ["agent", "user", "web", "", None])
async def test_self_reported_or_unknown_channel_does_not_create_ack(tmp_path, channel):
    jobs, session, delivery = await _delivered(tmp_path)
    with pytest.raises(AgentJobLifecycleConflictError):
        await jobs.acknowledge_human_result_inbox_delivery(
            session,
            delivery.delivery_id,
            expected_delivery_sha256=delivery.delivery_sha256,
            trusted_channel=channel,
        )
    assert (await jobs.list_result_inbox_records(session))[0].acknowledgement is None


@pytest.mark.parametrize(
    "session,bad_hash", [("another-execution", False), ("execution-one", True)]
)
async def test_human_read_rejects_wrong_session_and_delivery_hash(tmp_path, session, bad_hash):
    jobs, _, delivery = await _delivered(tmp_path)
    with pytest.raises(AgentJobLifecycleConflictError):
        await jobs.acknowledge_human_result_inbox_delivery(
            session,
            delivery.delivery_id,
            expected_delivery_sha256="a" * 64 if bad_hash else delivery.delivery_sha256,
            trusted_channel="manual_cli",
        )


async def test_human_receipts_are_append_only_and_authenticated(tmp_path):
    jobs, session, delivery = await _delivered(tmp_path)
    await jobs.acknowledge_human_result_inbox_delivery(
        session,
        delivery.delivery_id,
        expected_delivery_sha256=delivery.delivery_sha256,
        trusted_channel="manual_cli",
    )
    with sqlite3.connect(jobs.db_path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE agent_job_human_reads SET receipt_json='{}'")
        db.execute("DROP TRIGGER agent_job_human_reads_no_update")
        db.execute("UPDATE agent_job_human_reads SET receipt_json='{}'")
    with pytest.raises(AgentJobError):
        await jobs.get_human_result_inbox_read(
            session, delivery.delivery_id, expected_delivery_sha256=delivery.delivery_sha256
        )
    with pytest.raises(AgentJobError):
        await AgentJobStore(jobs.db_path, key_provider=lambda: b"x" * 32).list_result_inbox_records(
            session
        )


async def test_v7_upgrade_preserves_legacy_receipt_and_v8_missing_table_fails_closed(tmp_path):
    jobs, session, delivery = await _delivered(tmp_path)
    legacy = await jobs.acknowledge_result_inbox_delivery(
        session, delivery.delivery_id, expected_delivery_sha256=delivery.delivery_sha256
    )
    with sqlite3.connect(jobs.db_path) as db:
        db.execute("DROP TRIGGER agent_job_human_reads_no_update")
        db.execute("DROP TRIGGER agent_job_human_reads_no_delete")
        db.execute("DROP TABLE agent_job_human_reads")
        db.execute("PRAGMA user_version=7")
    upgraded = AgentJobStore(jobs.db_path, key_provider=lambda: b"x" * 32)
    assert (await upgraded.list_result_inbox_records(session))[
        0
    ].acknowledgement == legacy.acknowledgement
    with sqlite3.connect(jobs.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
        db.execute("DROP TRIGGER agent_job_human_reads_no_update")
        db.execute("DROP TRIGGER agent_job_human_reads_no_delete")
        db.execute("DROP TABLE agent_job_human_reads")
    with pytest.raises(AgentJobError):
        await AgentJobStore(jobs.db_path, key_provider=lambda: b"x" * 32).list_result_inbox_records(
            session
        )


async def test_unstarted_claim_cancellation_has_no_zero_result_or_publication(tmp_path):
    jobs, job, _ = await _running(tmp_path)
    # A separate admitted request exercises the real CLAIMED -> CANCELLED path.
    request = issue_agent_worker_request(
        task_id="second",
        session_id="second-session",
        agent_name="main",
        task="第二项",
        context="",
        message_topic="second",
        tool_scope=[],
        permission_mode="strict",
        model_tier="capable",
        max_turns=1,
        max_budget_usd=1,
        timeout_seconds=30,
        issued_at=datetime.now(UTC).isoformat(),
    )
    payload = AgentJobPayload(
        task_id="second",
        session_id="second-session",
        task="第二项",
        context="",
        message_topic="second",
    )
    admitted = await jobs.admit(request=request, payload=payload)
    claim = await jobs.claim(admitted.job_id, owner_id="prestart", lease_seconds=90)
    await jobs.cancel_claimed_before_start(
        admitted.job_id, owner_id="prestart", claim_epoch=claim.job.claim_epoch
    )
    cancelled = await jobs.get(admitted.job_id)
    assert cancelled.state is AgentJobState.CANCELLED and cancelled.result is None
    assert await jobs.get_job_publication(admitted.job_id) is None
    current = await jobs.get(job.job_id)
    unknown = await jobs.mark_execution_unknown(
        job.job_id,
        owner_id="owner",
        claim_epoch=job.claim_epoch,
        expected_request_sha256=job.request_sha256,
        expected_session_id_sha256=job.request.session_id_sha256,
        expected_latest_receipt_sha256=current.latest_receipt.receipt_sha256,
    )
    assert unknown.job.state is AgentJobState.UNKNOWN and unknown.job.result is None
    assert await jobs.get_job_publication(job.job_id) is None
