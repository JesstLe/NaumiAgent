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
    AGENT_JOB_SCHEMA_VERSION,
    AgentJobCapacityExhaustedError,
    AgentJobConflictError,
    AgentJobError,
    AgentJobLifecycleConflictError,
    AgentJobPayload,
    AgentJobPublicationState,
    AgentJobState,
    AgentJobStore,
    AgentJobTerminalPayload,
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


async def _complete_job(
    store: AgentJobStore,
    clock: _Clock,
    *,
    task_id: str,
    response: str = "durable publication result",
) -> tuple[object, object]:
    request, payload = _facts(clock, task_id=task_id)
    admitted = await store.admit(request=request, payload=payload)
    claimed = await store.claim(
        admitted.job_id,
        owner_id="worker-a",
        lease_seconds=30,
    )
    await store.mark_running(
        admitted.job_id,
        owner_id="worker-a",
        claim_epoch=claimed.job.claim_epoch,
    )
    result = issue_agent_worker_result(
        request=request,
        status="completed",
        response=response,
        error=None,
        total_tokens=3,
        total_cost_usd=0.0,
        turns=1,
        completed_at=clock().isoformat(),
    )
    await store.finish(
        admitted.job_id,
        owner_id="worker-a",
        claim_epoch=claimed.job.claim_epoch,
        result=result,
        terminal_payload=AgentJobTerminalPayload(
            response=response,
            error="",
        ),
    )
    return admitted, result


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
    recovery_kwargs = {
        "expected_request_sha256": running.job.request_sha256,
        "expected_session_id_sha256": running.job.request.session_id_sha256,
        "expected_claim_epoch": running.job.claim_epoch,
        "expected_latest_receipt_sha256": (
            running.job.latest_receipt.receipt_sha256
        ),
    }
    with pytest.raises(AgentJobLifecycleConflictError, match="request fence"):
        await store.mark_recovery_unknown(
            admitted.job_id,
            **{**recovery_kwargs, "expected_request_sha256": "f" * 64},
        )
    with pytest.raises(AgentJobLifecycleConflictError, match="session fence"):
        await store.mark_recovery_unknown(
            admitted.job_id,
            **{**recovery_kwargs, "expected_session_id_sha256": "e" * 64},
        )
    with pytest.raises(AgentJobLifecycleConflictError, match="epoch fence"):
        await store.mark_recovery_unknown(
            admitted.job_id,
            **{**recovery_kwargs, "expected_claim_epoch": 999},
        )
    concurrent = await asyncio.gather(
        store.mark_recovery_unknown(admitted.job_id, **recovery_kwargs),
        store.mark_recovery_unknown(admitted.job_id, **recovery_kwargs),
    )
    assert sum(item.applied for item in concurrent) == 1
    assert all(item.job.state is AgentJobState.UNKNOWN for item in concurrent)


@pytest.mark.asyncio
async def test_recovery_catalog_is_bounded_authenticated_and_content_free(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)

    live_request, live_payload = _facts(
        clock,
        task_id="catalog-live-claim",
        task="目录不得泄露 live claim 正文",
    )
    live = await store.admit(request=live_request, payload=live_payload)
    await store.claim(live.job_id, owner_id="worker-live", lease_seconds=30)

    expired_request, expired_payload = _facts(
        clock,
        task_id="catalog-expired-claim",
    )
    expired = await store.admit(
        request=expired_request,
        payload=expired_payload,
    )
    await store.claim(
        expired.job_id,
        owner_id="worker-expired",
        lease_seconds=10,
    )

    running_request, running_payload = _facts(
        clock,
        task_id="catalog-running",
    )
    running = await store.admit(
        request=running_request,
        payload=running_payload,
    )
    running_claim = await store.claim(
        running.job_id,
        owner_id="worker-running",
        lease_seconds=10,
    )
    await store.mark_running(
        running.job_id,
        owner_id="worker-running",
        claim_epoch=running_claim.job.claim_epoch,
    )

    unknown_request, unknown_payload = _facts(
        clock,
        task_id="catalog-unknown",
    )
    unknown = await store.admit(
        request=unknown_request,
        payload=unknown_payload,
    )
    unknown_claim = await store.claim(
        unknown.job_id,
        owner_id="worker-unknown",
        lease_seconds=10,
    )
    unknown_running = await store.mark_running(
        unknown.job_id,
        owner_id="worker-unknown",
        claim_epoch=unknown_claim.job.claim_epoch,
    )
    clock.advance(seconds=11)
    await store.mark_recovery_unknown(
        unknown.job_id,
        expected_request_sha256=unknown_running.job.request_sha256,
        expected_session_id_sha256=(
            unknown_running.job.request.session_id_sha256
        ),
        expected_claim_epoch=unknown_running.job.claim_epoch,
        expected_latest_receipt_sha256=(
            unknown_running.job.latest_receipt.receipt_sha256
        ),
    )

    terminal, _result = await _complete_job(
        store,
        clock,
        task_id="catalog-publication",
    )

    bounded = await store.recovery_catalog(limit=2)
    assert len(bounded.jobs) == 2
    assert bounded.jobs_truncated is True
    assert len(bounded.publications) == 1
    assert bounded.publications_truncated is False

    catalog = await store.recovery_catalog(limit=10)
    assert {item.state for item in catalog.jobs} == {
        AgentJobState.CLAIMED,
        AgentJobState.RUNNING,
        AgentJobState.UNKNOWN,
    }
    assert {item.job_id for item in catalog.jobs} == {
        live.job_id,
        expired.job_id,
        running.job_id,
        unknown.job_id,
    }
    assert catalog.publications[0].job.job_id == terminal.job_id
    assert catalog.publications[0].job.state is AgentJobState.COMPLETED
    assert catalog.publications[0].publication.state is (
        AgentJobPublicationState.PENDING
    )
    assert live_payload.task not in repr(catalog)
    assert live_payload.context not in repr(catalog)

    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE agent_jobs SET latest_receipt_json = ? WHERE job_id = ?",
            ("{}", live.job_id),
        )
    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="持久记录"):
        await reopened.recovery_catalog(limit=10)


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
        terminal_payload=AgentJobTerminalPayload(
            response="敏感模型输出",
            error="",
        ),
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
        terminal_payload=AgentJobTerminalPayload(
            response="敏感模型输出",
            error="",
        ),
    )
    assert not replay.applied
    with pytest.raises(AgentJobLifecycleConflictError, match="owner"):
        await store.finish(
            job.job_id,
            owner_id="worker-b",
            claim_epoch=1,
            result=result,
            terminal_payload=AgentJobTerminalPayload(
                response="敏感模型输出",
                error="",
            ),
        )
    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    persisted = await reopened.get(job.job_id)
    assert persisted is not None
    assert persisted.result == result
    assert persisted.state is AgentJobState.COMPLETED
    assert persisted.terminal_payload_envelope is not None
    recovered_terminal = await reopened.recover_terminal_payload(
        job.job_id,
        expected_result_sha256=result.result_sha256,
    )
    assert recovered_terminal == AgentJobTerminalPayload(
        response="敏感模型输出",
        error="",
    )
    raw_store = path.read_bytes()
    assert "敏感模型输出".encode() not in raw_store
    with pytest.raises(
        AgentJobLifecycleConflictError,
        match="fence",
    ):
        await reopened.recover_terminal_payload(
            job.job_id,
            expected_result_sha256="0" * 64,
        )


@pytest.mark.asyncio
async def test_terminal_payload_mismatch_is_rejected_before_terminal_commit(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    store = AgentJobStore(
        tmp_path / "agent-jobs.db",
        key_provider=lambda: bytes(range(32)),
        clock=clock,
    )
    request, payload = _facts(clock, task_id="terminal-mismatch")
    admitted = await store.admit(request=request, payload=payload)
    claimed = await store.claim(
        admitted.job_id,
        owner_id="worker-a",
        lease_seconds=30,
    )
    await store.mark_running(
        admitted.job_id,
        owner_id="worker-a",
        claim_epoch=claimed.job.claim_epoch,
    )
    result = issue_agent_worker_result(
        request=request,
        status="completed",
        response="expected response",
        error=None,
        total_tokens=1,
        total_cost_usd=0.0,
        turns=1,
        completed_at=clock().isoformat(),
    )

    with pytest.raises(ValueError, match="response"):
        await store.finish(
            admitted.job_id,
            owner_id="worker-a",
            claim_epoch=claimed.job.claim_epoch,
            result=result,
            terminal_payload=AgentJobTerminalPayload(
                response="different response",
                error="",
            ),
        )

    persisted = await store.get(admitted.job_id)
    assert persisted is not None
    assert persisted.state is AgentJobState.RUNNING
    assert persisted.result is None
    assert persisted.terminal_payload_envelope is None


@pytest.mark.asyncio
async def test_error_terminal_payload_recovers_without_plaintext_on_disk(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    store = AgentJobStore(
        path,
        key_provider=lambda: bytes(range(32)),
        clock=clock,
    )
    request, payload = _facts(clock, task_id="terminal-error")
    admitted = await store.admit(request=request, payload=payload)
    claimed = await store.claim(
        admitted.job_id,
        owner_id="worker-a",
        lease_seconds=30,
    )
    await store.mark_running(
        admitted.job_id,
        owner_id="worker-a",
        claim_epoch=claimed.job.claim_epoch,
    )
    result = issue_agent_worker_result(
        request=request,
        status="error",
        response="bounded partial response",
        error="private provider failure",
        total_tokens=2,
        total_cost_usd=0.0,
        turns=1,
        completed_at=clock().isoformat(),
    )
    await store.finish(
        admitted.job_id,
        owner_id="worker-a",
        claim_epoch=claimed.job.claim_epoch,
        result=result,
        terminal_payload=AgentJobTerminalPayload(
            response="bounded partial response",
            error="private provider failure",
        ),
    )

    reopened = AgentJobStore(
        path,
        key_provider=lambda: bytes(range(32)),
        clock=clock,
    )
    recovered = await reopened.recover_terminal_payload(
        admitted.job_id,
        expected_result_sha256=result.result_sha256,
    )
    assert recovered.response == "bounded partial response"
    assert recovered.error == "private provider failure"
    raw_store = path.read_bytes()
    assert recovered.response.encode() not in raw_store
    assert recovered.error.encode() not in raw_store


@pytest.mark.asyncio
async def test_terminal_payload_ciphertext_tamper_fails_closed(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock, task_id="terminal-tamper")
    admitted = await store.admit(request=request, payload=payload)
    claimed = await store.claim(
        admitted.job_id,
        owner_id="worker-a",
        lease_seconds=30,
    )
    await store.mark_running(
        admitted.job_id,
        owner_id="worker-a",
        claim_epoch=claimed.job.claim_epoch,
    )
    result = issue_agent_worker_result(
        request=request,
        status="completed",
        response="recoverable private response",
        error=None,
        total_tokens=1,
        total_cost_usd=0.0,
        turns=1,
        completed_at=clock().isoformat(),
    )
    await store.finish(
        admitted.job_id,
        owner_id="worker-a",
        claim_epoch=claimed.job.claim_epoch,
        result=result,
        terminal_payload=AgentJobTerminalPayload(
            response="recoverable private response",
            error="",
        ),
    )

    with sqlite3.connect(path) as db:
        raw_envelope = db.execute(
            """
            SELECT terminal_payload_envelope_json
            FROM agent_jobs WHERE job_id = ?
            """,
            (admitted.job_id,),
        ).fetchone()[0]
        envelope = json.loads(raw_envelope)
        ciphertext = bytearray(
            base64.b64decode(envelope["ciphertext_base64"])
        )
        ciphertext[-1] ^= 1
        envelope["ciphertext_base64"] = base64.b64encode(ciphertext).decode()
        public = {
            name: value
            for name, value in envelope.items()
            if name != "envelope_sha256"
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
            """
            UPDATE agent_jobs
            SET terminal_payload_envelope_json = ?
            WHERE job_id = ?
            """,
            (
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                admitted.job_id,
            ),
        )

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="terminal payload"):
        await reopened.get(admitted.job_id)


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


@pytest.mark.asyncio
async def test_capacity_admission_is_cross_store_bounded_and_reusable(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    first_store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    second_store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    first_request, first_payload = _facts(clock, task_id="capacity-first")
    first = await first_store.admit_for_capacity(
        request=first_request,
        payload=first_payload,
        owner_id="runtime-a",
        lease_seconds=30,
        max_active_jobs=1,
        max_waiters=1,
    )
    assert first.should_dispatch

    clock.advance(seconds=1)
    second_request, second_payload = _facts(clock, task_id="capacity-second")
    second = await second_store.admit_for_capacity(
        request=second_request,
        payload=second_payload,
        owner_id="runtime-b",
        lease_seconds=30,
        max_active_jobs=1,
        max_waiters=1,
    )
    assert not second.should_dispatch
    assert second.job.state is AgentJobState.ADMITTED
    snapshot = await first_store.capacity_snapshot()
    assert snapshot is not None
    assert snapshot.active_jobs == 1
    assert snapshot.waiting_jobs == 1
    assert snapshot.available_jobs == 0

    await first_store.mark_running(
        first.job.job_id,
        owner_id="runtime-a",
        claim_epoch=first.job.claim_epoch,
    )
    result = issue_agent_worker_result(
        request=first_request,
        status="completed",
        response="完成",
        error=None,
        total_tokens=1,
        total_cost_usd=0.0,
        turns=1,
        completed_at=clock().isoformat(),
    )
    await first_store.finish(
        first.job.job_id,
        owner_id="runtime-a",
        claim_epoch=first.job.claim_epoch,
        result=result,
        terminal_payload=AgentJobTerminalPayload(
            response="完成",
            error="",
        ),
    )
    claimed_second = await second_store.claim_for_capacity(
        second.job.job_id,
        owner_id="runtime-b",
        lease_seconds=30,
        max_active_jobs=1,
        max_waiters=1,
    )
    assert claimed_second.should_dispatch
    snapshot = await second_store.capacity_snapshot()
    assert snapshot is not None
    assert snapshot.active_jobs == 1
    assert snapshot.waiting_jobs == 0


@pytest.mark.asyncio
async def test_capacity_queue_hard_limit_is_atomic_across_stores(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    stores = [
        AgentJobStore(path, key_provider=lambda: key, clock=clock)
        for _ in range(3)
    ]
    facts = [
        _facts(clock, task_id=f"capacity-race-{index}")
        for index in range(3)
    ]

    outcomes = await asyncio.gather(
        *[
            store.admit_for_capacity(
                request=request,
                payload=payload,
                owner_id=f"runtime-{index}",
                lease_seconds=30,
                max_active_jobs=1,
                max_waiters=1,
            )
            for index, (store, (request, payload)) in enumerate(
                zip(stores, facts, strict=True)
            )
        ],
        return_exceptions=True,
    )
    accepted = [
        item for item in outcomes
        if not isinstance(item, Exception)
    ]
    rejected = [
        item for item in outcomes
        if isinstance(item, Exception)
    ]
    assert len(accepted) == 2
    assert sum(item.should_dispatch for item in accepted) == 1
    assert len(rejected) == 1
    assert isinstance(rejected[0], AgentJobCapacityExhaustedError)
    snapshot = await stores[0].capacity_snapshot()
    assert snapshot is not None
    assert (snapshot.active_jobs, snapshot.waiting_jobs) == (1, 1)


@pytest.mark.asyncio
async def test_capacity_fifo_and_raw_claim_cannot_skip_oldest_waiter(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    store = AgentJobStore(
        tmp_path / "agent-jobs.db",
        key_provider=lambda: bytes(range(32)),
        clock=clock,
    )
    active_request, active_payload = _facts(clock, task_id="capacity-active")
    active = await store.admit_for_capacity(
        request=active_request,
        payload=active_payload,
        owner_id="runtime-active",
        lease_seconds=30,
        max_active_jobs=1,
        max_waiters=2,
    )
    clock.advance(seconds=1)
    first_request, first_payload = _facts(clock, task_id="capacity-wait-first")
    first = await store.admit_for_capacity(
        request=first_request,
        payload=first_payload,
        owner_id="runtime-first",
        lease_seconds=30,
        max_active_jobs=1,
        max_waiters=2,
    )
    clock.advance(seconds=1)
    second_request, second_payload = _facts(clock, task_id="capacity-wait-second")
    second = await store.admit_for_capacity(
        request=second_request,
        payload=second_payload,
        owner_id="runtime-second",
        lease_seconds=30,
        max_active_jobs=1,
        max_waiters=2,
    )
    with pytest.raises(AgentJobCapacityExhaustedError, match="FIFO"):
        await store.claim(
            second.job.job_id,
            owner_id="runtime-second",
            lease_seconds=30,
        )
    await store.cancel_before_claim(first.job.job_id)
    await store.mark_running(
        active.job.job_id,
        owner_id="runtime-active",
        claim_epoch=active.job.claim_epoch,
    )
    active_result = issue_agent_worker_result(
        request=active_request,
        status="completed",
        response="done",
        error=None,
        total_tokens=1,
        total_cost_usd=0.0,
        turns=1,
        completed_at=clock().isoformat(),
    )
    await store.finish(
        active.job.job_id,
        owner_id="runtime-active",
        claim_epoch=active.job.claim_epoch,
        result=active_result,
        terminal_payload=AgentJobTerminalPayload(
            response="done",
            error="",
        ),
    )
    claimed = await store.claim_for_capacity(
        second.job.job_id,
        owner_id="runtime-second",
        lease_seconds=30,
        max_active_jobs=1,
        max_waiters=2,
    )
    assert claimed.should_dispatch


@pytest.mark.asyncio
async def test_capacity_policy_reconfiguration_requires_empty_nonterminal_set(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    store = AgentJobStore(
        tmp_path / "agent-jobs.db",
        key_provider=lambda: bytes(range(32)),
        clock=clock,
    )
    request, payload = _facts(clock, task_id="capacity-policy")
    admitted = await store.admit_for_capacity(
        request=request,
        payload=payload,
        owner_id="runtime-a",
        lease_seconds=30,
        max_active_jobs=1,
        max_waiters=1,
    )
    with pytest.raises(AgentJobConflictError, match="配置不一致"):
        await store.claim_for_capacity(
            admitted.job.job_id,
            owner_id="runtime-a",
            lease_seconds=30,
            max_active_jobs=2,
            max_waiters=2,
        )
    await store.mark_running(
        admitted.job.job_id,
        owner_id="runtime-a",
        claim_epoch=admitted.job.claim_epoch,
    )
    result = issue_agent_worker_result(
        request=request,
        status="completed",
        response="done",
        error=None,
        total_tokens=1,
        total_cost_usd=0.0,
        turns=1,
        completed_at=clock().isoformat(),
    )
    await store.finish(
        admitted.job.job_id,
        owner_id="runtime-a",
        claim_epoch=admitted.job.claim_epoch,
        result=result,
        terminal_payload=AgentJobTerminalPayload(
            response="done",
            error="",
        ),
    )
    next_request, next_payload = _facts(clock, task_id="capacity-policy-next")
    changed = await store.admit_for_capacity(
        request=next_request,
        payload=next_payload,
        owner_id="runtime-b",
        lease_seconds=30,
        max_active_jobs=2,
        max_waiters=2,
    )
    assert changed.should_dispatch
    snapshot = await store.capacity_snapshot()
    assert snapshot is not None
    assert snapshot.policy.max_active_jobs == 2
    assert snapshot.policy.max_waiters == 2


@pytest.mark.asyncio
async def test_expired_running_job_blocks_capacity_until_recovery_unknown(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    store = AgentJobStore(
        tmp_path / "agent-jobs.db",
        key_provider=lambda: bytes(range(32)),
        clock=clock,
    )
    request, payload = _facts(clock, task_id="capacity-recovery")
    running = await store.admit_for_capacity(
        request=request,
        payload=payload,
        owner_id="runtime-a",
        lease_seconds=10,
        max_active_jobs=1,
        max_waiters=1,
    )
    started = await store.mark_running(
        running.job.job_id,
        owner_id="runtime-a",
        claim_epoch=running.job.claim_epoch,
    )
    clock.advance(seconds=11)
    waiting_request, waiting_payload = _facts(
        clock,
        task_id="capacity-after-crash",
    )
    waiting = await store.admit_for_capacity(
        request=waiting_request,
        payload=waiting_payload,
        owner_id="runtime-b",
        lease_seconds=10,
        max_active_jobs=1,
        max_waiters=1,
    )
    assert waiting.job.state is AgentJobState.ADMITTED
    snapshot = await store.capacity_snapshot()
    assert snapshot is not None
    assert snapshot.active_jobs == 1
    assert snapshot.recovery_required_jobs == 1
    assert snapshot.available_jobs == 0
    await store.mark_recovery_unknown(
        running.job.job_id,
        expected_request_sha256=started.job.request_sha256,
        expected_session_id_sha256=started.job.request.session_id_sha256,
        expected_claim_epoch=started.job.claim_epoch,
        expected_latest_receipt_sha256=(
            started.job.latest_receipt.receipt_sha256
        ),
    )
    claimed = await store.claim_for_capacity(
        waiting.job.job_id,
        owner_id="runtime-b",
        lease_seconds=10,
        max_active_jobs=1,
        max_waiters=1,
    )
    assert claimed.should_dispatch


@pytest.mark.asyncio
async def test_schema_v1_migrates_capacity_policy_without_losing_jobs(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock, task_id="capacity-migration")
    admitted = await store.admit(request=request, payload=payload)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_job_capacity_policy")
        db.execute("PRAGMA user_version = 1")

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    assert await reopened.capacity_snapshot() is None
    restored = await reopened.get(admitted.job_id)
    assert restored is not None
    assert restored.request == request
    with sqlite3.connect(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        table = db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'agent_job_capacity_policy'
            """
        ).fetchone()
    assert version == AGENT_JOB_SCHEMA_VERSION == 6
    assert table == ("agent_job_capacity_policy",)


@pytest.mark.asyncio
async def test_schema_v2_adds_terminal_payload_without_losing_jobs(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock, task_id="terminal-migration")
    admitted = await store.admit(request=request, payload=payload)
    with sqlite3.connect(path) as db:
        db.execute(
            "ALTER TABLE agent_jobs DROP COLUMN terminal_payload_envelope_json"
        )
        db.execute("PRAGMA user_version = 2")

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    restored = await reopened.get(admitted.job_id)
    assert restored is not None
    assert restored.request == request
    assert restored.terminal_payload_envelope is None
    with sqlite3.connect(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(agent_jobs)").fetchall()
        }
    assert version == AGENT_JOB_SCHEMA_VERSION == 6
    assert "terminal_payload_envelope_json" in columns


@pytest.mark.asyncio
async def test_publication_outbox_claim_renew_ack_and_restart(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    admitted, _result = await _complete_job(
        store,
        clock,
        task_id="publication-lifecycle",
    )

    backlog = await store.publication_backlog()
    assert (backlog.pending, backlog.live_claimed, backlog.expired_claims) == (
        1,
        0,
        0,
    )
    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    recovery = await reopened.list_publication_recovery()
    assert len(recovery) == 1
    pending = recovery[0]
    assert pending.job_id == admitted.job_id
    assert pending.state is AgentJobPublicationState.PENDING
    assert pending.latest_receipt.sequence == 1
    with pytest.raises(AgentJobLifecycleConflictError, match="claimed"):
        await reopened.release_publication_claim(
            pending.publication_id,
            owner_id="publisher-a",
            claim_epoch=1,
        )

    claimed = await reopened.claim_next_publication(
        owner_id="publisher-a",
        lease_seconds=10,
    )
    assert claimed is not None and claimed.applied
    assert claimed.publication.state is AgentJobPublicationState.CLAIMED
    assert claimed.publication.claim_epoch == 1
    assert claimed.publication.attempt_count == 1
    clock.advance(seconds=5)
    renewed = await reopened.renew_publication_claim(
        claimed.publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=1,
        lease_seconds=30,
    )
    assert renewed.publication.latest_receipt.sequence == 3
    delivery_sha256 = hashlib.sha256(b"terminal-event-and-message").hexdigest()
    published = await reopened.acknowledge_publication(
        claimed.publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=1,
        delivery_sha256=delivery_sha256,
    )
    assert published.publication.state is AgentJobPublicationState.PUBLISHED
    assert published.publication.latest_receipt.delivery_sha256 == delivery_sha256
    replay = await reopened.acknowledge_publication(
        claimed.publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=1,
        delivery_sha256=delivery_sha256,
    )
    assert not replay.applied
    with pytest.raises(AgentJobLifecycleConflictError, match="幂等"):
        await reopened.acknowledge_publication(
            claimed.publication.publication_id,
            owner_id="publisher-a",
            claim_epoch=1,
            delivery_sha256="0" * 64,
        )
    assert await reopened.list_publication_recovery() == ()
    assert await reopened.claim_next_publication(
        owner_id="publisher-b",
        lease_seconds=10,
    ) is None
    backlog = await reopened.publication_backlog()
    assert (backlog.pending, backlog.live_claimed, backlog.expired_claims) == (
        0,
        0,
        0,
    )


@pytest.mark.asyncio
async def test_publication_expiry_takeover_release_and_old_owner_fence(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    first_store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    second_store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    await _complete_job(
        first_store,
        clock,
        task_id="publication-takeover",
    )
    first = await first_store.claim_next_publication(
        owner_id="publisher-a",
        lease_seconds=10,
    )
    assert first is not None
    clock.advance(seconds=11)
    backlog = await second_store.publication_backlog()
    assert backlog.expired_claims == 1
    takeover = await second_store.claim_next_publication(
        owner_id="publisher-b",
        lease_seconds=20,
    )
    assert takeover is not None
    assert takeover.publication.claim_epoch == 2
    assert takeover.publication.attempt_count == 2
    with pytest.raises(AgentJobLifecycleConflictError, match="owner"):
        await first_store.release_publication_claim(
            first.publication.publication_id,
            owner_id="publisher-a",
            claim_epoch=1,
        )
    released = await second_store.release_publication_claim(
        takeover.publication.publication_id,
        owner_id="publisher-b",
        claim_epoch=2,
    )
    assert released.publication.state is AgentJobPublicationState.PENDING
    assert released.publication.owner_id is None
    third = await first_store.claim_next_publication(
        owner_id="publisher-c",
        lease_seconds=20,
    )
    assert third is not None
    assert third.publication.claim_epoch == 3
    assert third.publication.attempt_count == 3


@pytest.mark.asyncio
async def test_publication_quarantine_is_fenced_and_unblocks_fifo(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    await _complete_job(store, clock, task_id="publication-poison")
    clock.advance(seconds=1)
    await _complete_job(store, clock, task_id="publication-after-poison")

    first = await store.claim_next_publication(
        owner_id="publisher-a",
        lease_seconds=30,
    )
    assert first is not None
    with pytest.raises(AgentJobLifecycleConflictError, match="尚未耗尽"):
        await store.quarantine_publication(
            first.publication.publication_id,
            owner_id="publisher-a",
            claim_epoch=first.publication.claim_epoch,
            max_attempts=2,
            failure_code="agent_publication_recovery_delivery_failed",
        )
    await store.release_publication_claim(
        first.publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=first.publication.claim_epoch,
    )
    second_attempt = await store.claim_next_publication(
        owner_id="publisher-a",
        lease_seconds=30,
    )
    assert second_attempt is not None
    assert second_attempt.publication.publication_id == first.publication.publication_id
    assert second_attempt.publication.attempt_count == 2
    with pytest.raises(AgentJobLifecycleConflictError, match="owner"):
        await store.quarantine_publication(
            second_attempt.publication.publication_id,
            owner_id="publisher-b",
            claim_epoch=second_attempt.publication.claim_epoch,
            max_attempts=2,
            failure_code="agent_publication_recovery_delivery_failed",
        )
    with pytest.raises(AgentJobLifecycleConflictError, match="owner"):
        await store.quarantine_publication(
            second_attempt.publication.publication_id,
            owner_id="publisher-a",
            claim_epoch=second_attempt.publication.claim_epoch + 1,
            max_attempts=2,
            failure_code="agent_publication_recovery_delivery_failed",
        )

    isolated = await store.quarantine_publication(
        second_attempt.publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=second_attempt.publication.claim_epoch,
        max_attempts=2,
        failure_code="agent_publication_recovery_delivery_failed",
    )
    assert isolated.applied is True
    assert isolated.publication.state is AgentJobPublicationState.PENDING
    assert isolated.publication.latest_receipt.reason_code == (
        "agent_publication_quarantined"
    )
    assert isolated.quarantine.attempt_count == 2
    replay = await store.quarantine_publication(
        second_attempt.publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=second_attempt.publication.claim_epoch,
        max_attempts=2,
        failure_code="agent_publication_recovery_delivery_failed",
    )
    assert replay.applied is False
    with pytest.raises(AgentJobLifecycleConflictError, match="幂等事实"):
        await store.quarantine_publication(
            second_attempt.publication.publication_id,
            owner_id="publisher-a",
            claim_epoch=second_attempt.publication.claim_epoch,
            max_attempts=2,
            failure_code="different_failure",
        )
    with pytest.raises(AgentJobLifecycleConflictError, match="已隔离"):
        await store.claim_publication(
            second_attempt.publication.publication_id,
            owner_id="publisher-b",
            lease_seconds=30,
        )

    backlog = await store.publication_backlog()
    assert (
        backlog.pending,
        backlog.live_claimed,
        backlog.expired_claims,
        backlog.quarantined,
    ) == (1, 0, 0, 1)
    recovery = await store.list_publication_recovery()
    assert len(recovery) == 1
    assert recovery[0].publication_id != isolated.publication.publication_id
    next_claim = await store.claim_next_publication(
        owner_id="publisher-b",
        lease_seconds=30,
    )
    assert next_claim is not None
    assert next_claim.publication.publication_id == recovery[0].publication_id

    catalog = await store.recovery_catalog(limit=10)
    quarantined = [
        entry for entry in catalog.publications
        if entry.quarantine is not None
    ]
    assert len(quarantined) == 1
    assert quarantined[0].quarantine == isolated.quarantine

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    assert await reopened.get_publication_quarantine(
        isolated.publication.publication_id
    ) == isolated.quarantine


@pytest.mark.asyncio
async def test_publication_quarantine_tampering_fails_authentication(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    await _complete_job(store, clock, task_id="publication-quarantine-tamper")
    claimed = await store.claim_next_publication(
        owner_id="publisher-a",
        lease_seconds=30,
    )
    assert claimed is not None
    isolated = await store.quarantine_publication(
        claimed.publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=claimed.publication.claim_epoch,
        max_attempts=1,
        failure_code="agent_publication_recovery_delivery_failed",
    )
    with sqlite3.connect(path) as db:
        payload = json.loads(
            db.execute(
                """
                SELECT receipt_json
                FROM agent_job_publication_quarantines
                WHERE publication_id = ?
                """,
                (isolated.publication.publication_id,),
            ).fetchone()[0]
        )
        payload["failure_code"] = "forged_failure"
        payload["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"receipt_sha256", "authentication_sha256"}
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        db.execute(
            """
            UPDATE agent_job_publication_quarantines
            SET failure_code = ?, receipt_sha256 = ?, receipt_json = ?
            WHERE publication_id = ?
            """,
            (
                payload["failure_code"],
                payload["receipt_sha256"],
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                isolated.publication.publication_id,
            ),
        )

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="authentication"):
        await reopened.get_publication_quarantine(
            isolated.publication.publication_id
        )


@pytest.mark.asyncio
async def test_publication_quarantine_rolls_back_publication_on_insert_failure(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    await _complete_job(store, clock, task_id="publication-quarantine-rollback")
    claimed = await store.claim_next_publication(
        owner_id="publisher-a",
        lease_seconds=30,
    )
    assert claimed is not None
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TRIGGER fail_publication_quarantine_insert
            BEFORE INSERT ON agent_job_publication_quarantines
            BEGIN
                SELECT RAISE(ABORT, 'injected quarantine failure');
            END
            """
        )

    with pytest.raises(AgentJobError, match="无法隔离"):
        await store.quarantine_publication(
            claimed.publication.publication_id,
            owner_id="publisher-a",
            claim_epoch=claimed.publication.claim_epoch,
            max_attempts=1,
            failure_code="agent_publication_recovery_delivery_failed",
        )

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    publication = await reopened.get_publication(
        claimed.publication.publication_id
    )
    assert publication == claimed.publication
    assert await reopened.get_publication_quarantine(
        claimed.publication.publication_id
    ) is None


@pytest.mark.asyncio
async def test_publication_receipt_tampering_fails_hmac_validation(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    await _complete_job(store, clock, task_id="publication-tamper")
    claimed = await store.claim_next_publication(
        owner_id="publisher-a",
        lease_seconds=30,
    )
    assert claimed is not None
    publication_id = claimed.publication.publication_id

    with sqlite3.connect(path) as db:
        receipt = json.loads(
            db.execute(
                """
                SELECT latest_receipt_json
                FROM agent_job_publications
                WHERE publication_id = ?
                """,
                (publication_id,),
            ).fetchone()[0]
        )
        receipt["attempt_count"] = 2
        public = {
            name: value
            for name, value in receipt.items()
            if name not in {"receipt_sha256", "authentication_sha256"}
        }
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                public,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        serialized = json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        db.execute(
            """
            UPDATE agent_job_publications
            SET attempt_count = 2, latest_receipt_sha256 = ?,
                latest_receipt_json = ?
            WHERE publication_id = ?
            """,
            (receipt["receipt_sha256"], serialized, publication_id),
        )
        db.execute(
            """
            UPDATE agent_job_publication_events
            SET receipt_sha256 = ?, receipt_json = ?
            WHERE publication_id = ? AND sequence = 2
            """,
            (
                receipt["receipt_sha256"],
                serialized,
                publication_id,
            ),
        )

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="authentication"):
        await reopened.get_publication(publication_id)


@pytest.mark.asyncio
async def test_publication_event_projection_tampering_fails_closed(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    await _complete_job(store, clock, task_id="publication-event-projection")
    pending = (await store.list_publication_recovery())[0]

    with sqlite3.connect(path) as db:
        db.execute(
            """
            UPDATE agent_job_publication_events
            SET state = ?
            WHERE publication_id = ? AND sequence = 1
            """,
            (
                AgentJobPublicationState.CLAIMED.value,
                pending.publication_id,
            ),
        )

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="投影与 receipt 不一致"):
        await reopened.get_publication(pending.publication_id)


@pytest.mark.asyncio
async def test_publication_delivery_is_atomic_restart_safe_and_routable(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    admitted, result = await _complete_job(
        store,
        clock,
        task_id="publication-delivery",
        response="跨重启可恢复的结果",
    )
    publication = await store.get_job_publication(admitted.job_id)
    assert publication is not None
    claimed = await store.claim_publication(
        publication.publication_id,
        owner_id="publisher-a",
        lease_seconds=30,
    )
    content = await store.recover_publication_content(
        publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=claimed.publication.claim_epoch,
    )
    assert content.payload.task_id == "publication-delivery"
    assert content.request.agent_name == "coder"
    assert content.result == result
    assert content.terminal_payload.response == "跨重启可恢复的结果"

    delivered = await store.deliver_publication_to_inbox(
        publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=claimed.publication.claim_epoch,
    )
    assert delivered.applied
    assert delivered.publication.state is AgentJobPublicationState.PUBLISHED
    assert (
        delivered.publication.latest_receipt.delivery_sha256
        == delivered.delivery.delivery_sha256
    )
    replay = await store.deliver_publication_to_inbox(
        publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=claimed.publication.claim_epoch,
    )
    assert not replay.applied
    assert replay.delivery == delivered.delivery

    clock.advance(seconds=1)
    second_admitted, _second_result = await _complete_job(
        store,
        clock,
        task_id="publication-delivery-newest",
        response="更新的持久结果",
    )
    second_publication = await store.get_job_publication(second_admitted.job_id)
    assert second_publication is not None
    second_claimed = await store.claim_publication(
        second_publication.publication_id,
        owner_id="publisher-a",
        lease_seconds=30,
    )
    second_delivered = await store.deliver_publication_to_inbox(
        second_publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=second_claimed.publication.claim_epoch,
    )

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    assert await reopened.list_result_inbox("other-session") == ()
    inbox = await reopened.list_result_inbox("session-1")
    assert inbox == (delivered.delivery, second_delivered.delivery)
    latest = await reopened.list_result_inbox(
        "session-1",
        limit=1,
        newest_first=True,
    )
    assert latest == (second_delivered.delivery,)
    with pytest.raises(TypeError, match="newest_first"):
        await reopened.list_result_inbox(
            "session-1",
            newest_first=1,  # type: ignore[arg-type]
        )
    recovered = await reopened.recover_delivered_result(
        delivered.delivery.delivery_id,
        expected_delivery_sha256=delivered.delivery.delivery_sha256,
    )
    assert recovered.payload.task_id == "publication-delivery"
    assert recovered.terminal_payload.response == "跨重启可恢复的结果"
    with pytest.raises(AgentJobLifecycleConflictError, match="fence"):
        await reopened.recover_delivered_result(
            delivered.delivery.delivery_id,
            expected_delivery_sha256="0" * 64,
        )
    raw_store = path.read_bytes()
    assert b"session-1" not in raw_store
    assert "跨重启可恢复的结果".encode() not in raw_store


@pytest.mark.asyncio
async def test_publication_delivery_rolls_back_inbox_when_publish_fails(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    admitted, _result = await _complete_job(
        store,
        clock,
        task_id="publication-delivery-rollback",
    )
    publication = await store.get_job_publication(admitted.job_id)
    assert publication is not None
    claimed = await store.claim_publication(
        publication.publication_id,
        owner_id="publisher-a",
        lease_seconds=30,
    )
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TRIGGER fail_publication_publish
            BEFORE UPDATE OF state ON agent_job_publications
            WHEN NEW.state = 'published'
            BEGIN
                SELECT RAISE(ABORT, 'injected publication failure');
            END
            """
        )

    with pytest.raises(AgentJobError, match="结果收件箱"):
        await store.deliver_publication_to_inbox(
            publication.publication_id,
            owner_id="publisher-a",
            claim_epoch=claimed.publication.claim_epoch,
        )
    with sqlite3.connect(path) as db:
        delivery_count = db.execute(
            "SELECT COUNT(*) FROM agent_job_publication_deliveries"
        ).fetchone()[0]
        db.execute("DROP TRIGGER fail_publication_publish")
    assert delivery_count == 0
    persisted = await store.get_publication(publication.publication_id)
    assert persisted is not None
    assert persisted.state is AgentJobPublicationState.CLAIMED


@pytest.mark.asyncio
async def test_publication_delivery_receipt_tampering_fails_authentication(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    admitted, _result = await _complete_job(
        store,
        clock,
        task_id="publication-delivery-tamper",
    )
    publication = await store.get_job_publication(admitted.job_id)
    assert publication is not None
    claimed = await store.claim_publication(
        publication.publication_id,
        owner_id="publisher-a",
        lease_seconds=30,
    )
    delivered = await store.deliver_publication_to_inbox(
        publication.publication_id,
        owner_id="publisher-a",
        claim_epoch=claimed.publication.claim_epoch,
    )
    with sqlite3.connect(path) as db:
        receipt = json.loads(
            db.execute(
                """
                SELECT receipt_json
                FROM agent_job_publication_deliveries
                WHERE delivery_id = ?
                """,
                (delivered.delivery.delivery_id,),
            ).fetchone()[0]
        )
        receipt["session_routing_hmac"] = "0" * 64
        public = {
            name: value
            for name, value in receipt.items()
            if name not in {"receipt_sha256", "authentication_sha256"}
        }
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                public,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        db.execute(
            """
            UPDATE agent_job_publication_deliveries
            SET session_routing_hmac = ?, receipt_sha256 = ?, receipt_json = ?
            WHERE delivery_id = ?
            """,
            (
                receipt["session_routing_hmac"],
                receipt["receipt_sha256"],
                json.dumps(
                    receipt,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                delivered.delivery.delivery_id,
            ),
        )

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="authentication"):
        await reopened.recover_delivered_result(
            delivered.delivery.delivery_id,
            expected_delivery_sha256=delivered.delivery.delivery_sha256,
        )


@pytest.mark.asyncio
async def test_schema_v3_adds_publication_outbox_and_replay_backfills(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    admitted, result = await _complete_job(
        store,
        clock,
        task_id="publication-migration",
        response="legacy v3 terminal response",
    )
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_job_publication_events")
        db.execute("DROP TABLE agent_job_publications")
        db.execute("PRAGMA user_version = 3")

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    restored = await reopened.get(admitted.job_id)
    assert restored is not None
    assert restored.result == result
    assert await reopened.list_publication_recovery() == ()
    replay = await reopened.finish(
        admitted.job_id,
        owner_id="worker-a",
        claim_epoch=1,
        result=result,
        terminal_payload=AgentJobTerminalPayload(
            response="legacy v3 terminal response",
            error="",
        ),
    )
    assert not replay.applied
    recovery = await reopened.list_publication_recovery()
    assert len(recovery) == 1
    assert recovery[0].job_id == admitted.job_id
    with sqlite3.connect(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in db.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }
    assert version == AGENT_JOB_SCHEMA_VERSION == 6
    assert {
        "agent_job_publications",
        "agent_job_publication_events",
    } <= tables


@pytest.mark.asyncio
async def test_schema_v4_rejects_missing_publication_recovery_index(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock, task_id="publication-index-integrity")
    admitted = await store.admit(request=request, payload=payload)
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX agent_job_publications_recoverable")

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="恢复索引缺失"):
        await reopened.get(admitted.job_id)


@pytest.mark.asyncio
async def test_schema_v4_rejects_missing_publication_tables(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock, task_id="publication-table-integrity")
    admitted = await store.admit(request=request, payload=payload)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_job_publication_events")
        db.execute("DROP TABLE agent_job_publications")

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="publication 表缺失"):
        await reopened.get(admitted.job_id)


@pytest.mark.asyncio
async def test_schema_v4_adds_result_inbox_without_losing_publication(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    admitted, _result = await _complete_job(
        store,
        clock,
        task_id="delivery-schema-migration",
    )
    publication = await store.get_job_publication(admitted.job_id)
    assert publication is not None
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_job_publication_deliveries")
        db.execute("PRAGMA user_version = 4")

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    restored = await reopened.get_publication(publication.publication_id)
    assert restored == publication
    assert await reopened.list_result_inbox("session-1") == ()
    with sqlite3.connect(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        table = db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name = 'agent_job_publication_deliveries'
            """
        ).fetchone()
    assert version == AGENT_JOB_SCHEMA_VERSION == 6
    assert table == ("agent_job_publication_deliveries",)


@pytest.mark.asyncio
async def test_schema_v5_rejects_missing_result_inbox_table(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock, task_id="delivery-schema-integrity")
    admitted = await store.admit(request=request, payload=payload)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_job_publication_deliveries")

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="delivery 表缺失"):
        await reopened.get(admitted.job_id)


@pytest.mark.asyncio
async def test_schema_v5_rejects_weakened_delivery_constraints(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock, task_id="delivery-schema-constraints")
    admitted = await store.admit(request=request, payload=payload)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_job_publication_deliveries")
        db.execute(
            """
            CREATE TABLE agent_job_publication_deliveries (
                delivery_id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                sink TEXT NOT NULL,
                session_routing_hmac TEXT NOT NULL,
                delivery_sha256 TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL,
                receipt_json TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE INDEX agent_job_publication_deliveries_inbox
            ON agent_job_publication_deliveries (
                session_routing_hmac, delivered_at, delivery_id
            )
            """
        )

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="唯一约束无效"):
        await reopened.get(admitted.job_id)


@pytest.mark.asyncio
async def test_schema_v5_adds_quarantine_authority_without_losing_publication(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    admitted, _result = await _complete_job(
        store,
        clock,
        task_id="quarantine-schema-migration",
    )
    publication = await store.get_job_publication(admitted.job_id)
    assert publication is not None
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_job_publication_quarantines")
        db.execute("PRAGMA user_version = 5")

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    assert await reopened.get_publication(publication.publication_id) == publication
    assert await reopened.get_publication_quarantine(
        publication.publication_id
    ) is None
    with sqlite3.connect(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        table = db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name = 'agent_job_publication_quarantines'
            """
        ).fetchone()
    assert version == AGENT_JOB_SCHEMA_VERSION == 6
    assert table == ("agent_job_publication_quarantines",)


@pytest.mark.asyncio
async def test_schema_v6_rejects_missing_quarantine_authority(tmp_path) -> None:
    clock = _Clock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock, task_id="quarantine-schema-integrity")
    admitted = await store.admit(request=request, payload=payload)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_job_publication_quarantines")

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="quarantine 表缺失"):
        await reopened.get(admitted.job_id)


@pytest.mark.asyncio
async def test_schema_v6_rejects_weakened_quarantine_constraints(
    tmp_path,
) -> None:
    clock = _Clock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    path = tmp_path / "agent-jobs.db"
    key = bytes(range(32))
    store = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    request, payload = _facts(clock, task_id="quarantine-schema-constraints")
    admitted = await store.admit(request=request, payload=payload)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_job_publication_quarantines")
        db.execute(
            """
            CREATE TABLE agent_job_publication_quarantines (
                publication_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                claim_epoch INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                failure_code TEXT NOT NULL,
                quarantined_at TEXT NOT NULL,
                publication_receipt_sha256 TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL,
                receipt_json TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE INDEX agent_job_publication_quarantines_catalog
            ON agent_job_publication_quarantines (
                quarantined_at, publication_id
            )
            """
        )

    reopened = AgentJobStore(path, key_provider=lambda: key, clock=clock)
    with pytest.raises(AgentJobError, match="唯一约束无效"):
        await reopened.get(admitted.job_id)
