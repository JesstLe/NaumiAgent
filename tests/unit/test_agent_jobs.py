from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from naumi_agent.daemons.agent_jobs import (
    AgentJobConflictError,
    AgentJobError,
    AgentJobLifecycleConflictError,
    AgentJobPayload,
    AgentJobState,
    AgentJobStore,
)
from naumi_agent.daemons.agent_worker_contract import (
    issue_agent_worker_request,
    issue_agent_worker_result,
)


@dataclass
class _Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _facts(
    clock: _Clock,
    *,
    task_id: str = "agent-task-1",
    task: str = "修复持久队列竞争",
) -> tuple[object, AgentJobPayload]:
    payload = AgentJobPayload(
        task_id=task_id,
        session_id="session-1",
        task=task,
        context="只处理给定工作区，并保留验证证据。",
        message_topic=f"task.{task_id}.completed",
    )
    request = issue_agent_worker_request(
        task_id=payload.task_id,
        session_id=payload.session_id,
        agent_name="coder",
        task=payload.task,
        context=payload.context,
        tool_scope=("file_edit", "file_read"),
        permission_mode="bypass",
        model_tier="capable",
        max_turns=50,
        max_budget_usd=2.5,
        timeout_seconds=600,
        message_topic=payload.message_topic,
        issued_at=clock().isoformat(),
    )
    return request, payload


@pytest.mark.asyncio
async def test_admit_restart_claim_and_recover_without_plaintext_on_disk(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    key = bytes(range(32))
    request, payload = _facts(clock)
    path = tmp_path / "agent-jobs.db"
    key_calls = 0

    def key_provider() -> bytes:
        nonlocal key_calls
        key_calls += 1
        return key

    store = AgentJobStore(path, key_provider=key_provider, clock=clock)
    assert key_calls == 0
    admitted = await store.admit(request=request, payload=payload)
    assert admitted.state is AgentJobState.ADMITTED
    assert key_calls == 1
    assert "修复持久队列竞争" not in repr(payload)
    raw = path.read_bytes()
    assert payload.task.encode() not in raw
    assert payload.context.encode() not in raw
    assert payload.session_id.encode() not in raw
    assert request.task_sha256.encode() not in raw
    assert request.context_sha256.encode() not in raw

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    restored = await reopened.get(admitted.job_id)
    assert restored is not None
    assert restored.request == request
    with pytest.raises(AgentJobLifecycleConflictError, match="状态"):
        await reopened.recover_payload(
            admitted.job_id,
            owner_id="worker-a",
            claim_epoch=1,
        )

    claim = await reopened.claim(
        admitted.job_id,
        owner_id="worker-a",
        lease_seconds=30,
    )
    assert claim.applied and claim.should_dispatch
    assert claim.job.claim_epoch == 1
    recovered = await reopened.recover_payload(
        admitted.job_id,
        owner_id="worker-a",
        claim_epoch=1,
    )
    assert recovered == payload


@pytest.mark.asyncio
async def test_admission_is_idempotent_and_conflicting_request_id_fails(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    store = AgentJobStore(
        tmp_path / "agent-jobs.db",
        key_provider=lambda: bytes(range(32)),
        clock=clock,
    )
    request, payload = _facts(clock)
    first = await store.admit(request=request, payload=payload)
    replay = await store.admit(request=request, payload=payload)
    assert replay == first

    conflicting_request, conflicting_payload = _facts(
        clock,
        task="同一业务身份下的其他任务",
    )
    assert conflicting_request.request_id == request.request_id
    with pytest.raises(AgentJobConflictError, match="其他请求"):
        await store.admit(
            request=conflicting_request,
            payload=conflicting_payload,
        )
    with pytest.raises(ValueError, match="task 与 request"):
        await store.admit(
            request=request,
            payload=conflicting_payload,
        )


@pytest.mark.asyncio
async def test_expired_prestart_claim_can_take_over_but_running_cannot_retry(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    store = AgentJobStore(
        tmp_path / "agent-jobs.db",
        key_provider=lambda: bytes(range(32)),
        clock=clock,
    )
    request, payload = _facts(clock)
    admitted = await store.admit(request=request, payload=payload)
    first = await store.claim(
        admitted.job_id,
        owner_id="worker-a",
        lease_seconds=10,
    )
    with pytest.raises(AgentJobLifecycleConflictError, match="owner"):
        await store.recover_payload(
            admitted.job_id,
            owner_id="worker-b",
            claim_epoch=first.job.claim_epoch,
        )

    clock.advance(seconds=11)
    takeover = await store.claim(
        admitted.job_id,
        owner_id="worker-b",
        lease_seconds=10,
    )
    assert takeover.job.claim_epoch == 2
    assert takeover.job.latest_receipt.reason_code == "agent_job_claim_taken_over"
    with pytest.raises(AgentJobLifecycleConflictError, match="owner"):
        await store.mark_running(
            admitted.job_id,
            owner_id="worker-a",
            claim_epoch=1,
        )
    assert await store.recover_payload(
        admitted.job_id,
        owner_id="worker-b",
        claim_epoch=2,
    ) == payload
    running = await store.mark_running(
        admitted.job_id,
        owner_id="worker-b",
        claim_epoch=2,
    )
    clock.advance(seconds=11)
    with pytest.raises(AgentJobLifecycleConflictError, match="running"):
        await store.claim(
            admitted.job_id,
            owner_id="worker-c",
            lease_seconds=10,
        )
    recovery = await store.list_recovery_required()
    assert [item.job_id for item in recovery] == [admitted.job_id]
    unknown = await store.mark_recovery_unknown(
        admitted.job_id,
        expected_latest_receipt_sha256=running.job.latest_receipt.receipt_sha256,
    )
    assert unknown.job.state is AgentJobState.UNKNOWN
    replay = await store.mark_recovery_unknown(
        admitted.job_id,
        expected_latest_receipt_sha256=running.job.latest_receipt.receipt_sha256,
    )
    assert not replay.applied


@pytest.mark.asyncio
async def test_renew_running_finish_and_terminal_retry_are_fenced(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock)
    job = await store.admit(request=request, payload=payload)
    claim = await store.claim(
        job.job_id,
        owner_id="worker-a",
        lease_seconds=10,
    )
    await store.mark_running(
        job.job_id,
        owner_id="worker-a",
        claim_epoch=claim.job.claim_epoch,
    )
    clock.advance(seconds=5)
    renewed = await store.renew_claim(
        job.job_id,
        owner_id="worker-a",
        claim_epoch=1,
        lease_seconds=30,
    )
    result = issue_agent_worker_result(
        request=request,
        status="completed",
        response="敏感模型输出",
        error=None,
        total_tokens=123,
        total_cost_usd=0.5,
        turns=4,
        completed_at=clock().isoformat(),
    )
    finished = await store.finish(
        job.job_id,
        owner_id="worker-a",
        claim_epoch=1,
        result=result,
    )
    assert finished.job.state is AgentJobState.COMPLETED
    assert finished.job.result == result
    assert finished.job.latest_receipt.previous_receipt_sha256 == (
        renewed.job.latest_receipt.receipt_sha256
    )

    clock.advance(seconds=60)
    replay = await store.finish(
        job.job_id,
        owner_id="worker-a",
        claim_epoch=1,
        result=result,
    )
    assert not replay.applied
    with pytest.raises(AgentJobLifecycleConflictError, match="owner"):
        await store.finish(
            job.job_id,
            owner_id="worker-b",
            claim_epoch=1,
            result=result,
        )
    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    persisted = await reopened.get(job.job_id)
    assert persisted is not None
    assert persisted.result == result
    assert persisted.state is AgentJobState.COMPLETED


@pytest.mark.asyncio
async def test_concurrent_claim_has_exactly_one_owner(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    first_store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    second_store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock)
    job = await first_store.admit(request=request, payload=payload)

    outcomes = await asyncio.gather(
        first_store.claim(job.job_id, owner_id="worker-a", lease_seconds=30),
        second_store.claim(job.job_id, owner_id="worker-b", lease_seconds=30),
        return_exceptions=True,
    )
    winners = [item for item in outcomes if not isinstance(item, Exception)]
    losers = [item for item in outcomes if isinstance(item, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], AgentJobLifecycleConflictError)
    assert await first_store.claim_next(
        owner_id="worker-c",
        lease_seconds=30,
    ) is None


@pytest.mark.asyncio
async def test_claim_next_is_fifo_and_preclaim_cancel_is_idempotent(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    store = AgentJobStore(
        tmp_path / "agent-jobs.db",
        key_provider=lambda: bytes(range(32)),
        clock=clock,
    )
    first_request, first_payload = _facts(clock, task_id="agent-task-first")
    first = await store.admit(
        request=first_request,
        payload=first_payload,
    )
    clock.advance(seconds=1)
    second_request, second_payload = _facts(clock, task_id="agent-task-second")
    second = await store.admit(
        request=second_request,
        payload=second_payload,
    )

    claimed = await store.claim_next(
        owner_id="worker-a",
        lease_seconds=30,
    )
    assert claimed is not None
    assert claimed.job.job_id == first.job_id
    cancelled = await store.cancel_before_claim(second.job_id)
    assert cancelled.applied
    assert cancelled.job.state is AgentJobState.CANCELLED
    replay = await store.cancel_before_claim(second.job_id)
    assert not replay.applied
    assert await store.claim_next(
        owner_id="worker-b",
        lease_seconds=30,
    ) is None


@pytest.mark.asyncio
async def test_ciphertext_and_event_chain_tampering_fail_closed(tmp_path) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock)
    job = await store.admit(request=request, payload=payload)
    await store.claim(
        job.job_id,
        owner_id="worker-a",
        lease_seconds=30,
    )

    with sqlite3.connect(path) as db:
        original_envelope_json = db.execute(
            "SELECT payload_envelope_json FROM agent_jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
        envelope = json.loads(original_envelope_json)
        ciphertext = bytearray(base64.b64decode(envelope["ciphertext_base64"]))
        ciphertext[0] ^= 1
        envelope["ciphertext_base64"] = base64.b64encode(ciphertext).decode()
        public = {
            key: value
            for key, value in envelope.items()
            if key != "envelope_sha256"
        }
        envelope["envelope_sha256"] = hashlib.sha256(
            json.dumps(
                public,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        db.execute(
            "UPDATE agent_jobs SET payload_envelope_json = ? WHERE job_id = ?",
            (
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                job.job_id,
            ),
        )

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="持久记录无效"):
        await reopened.get(job.job_id)

    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE agent_jobs SET payload_envelope_json = ? WHERE job_id = ?",
            (
                original_envelope_json,
                job.job_id,
            ),
        )
        receipt = json.loads(
            db.execute(
                """
                SELECT receipt_json FROM agent_job_lifecycle_events
                WHERE job_id = ? AND sequence = 2
                """,
                (job.job_id,),
            ).fetchone()[0]
        )
        receipt["owner_id"] = "worker-tampered"
        transition = {
            field: receipt[field]
            for field in (
                "job_id",
                "previous_state",
                "state",
                "owner_id",
                "claim_epoch",
                "claim_expires_at",
                "result_sha256",
                "reason_code",
            )
        }
        receipt["transition_sha256"] = hashlib.sha256(
            json.dumps(
                transition,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        public_receipt = {
            key: value
            for key, value in receipt.items()
            if key not in {"receipt_sha256", "authentication_sha256"}
        }
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                public_receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        db.execute(
            """
            UPDATE agent_job_lifecycle_events
            SET transition_sha256 = ?, receipt_sha256 = ?, receipt_json = ?
            WHERE job_id = ? AND sequence = 2
            """,
            (
                receipt["transition_sha256"],
                receipt["receipt_sha256"],
                json.dumps(
                    receipt,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                job.job_id,
            ),
        )
    with pytest.raises(AgentJobError, match="authentication"):
        await reopened.get(job.job_id)


@pytest.mark.asyncio
async def test_key_failure_is_sanitized_and_does_not_create_partial_db(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    request, payload = _facts(clock)

    def unavailable_key() -> bytes:
        raise RuntimeError("secret-backend-detail")

    store = AgentJobStore(path, key_provider=unavailable_key, clock=clock)
    with pytest.raises(AgentJobError) as exc_info:
        await store.admit(request=request, payload=payload)

    assert "secret-backend-detail" not in str(exc_info.value)
    assert not path.exists()
