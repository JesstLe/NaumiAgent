from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from naumi_agent.orchestrator.pursuit import PursuitRun, PursuitRunStatus
from naumi_agent.orchestrator.pursuit_recovery_attempt import (
    PursuitRecoveryAttempt,
    PursuitRecoveryAttemptState,
    format_recovery_attempts,
    new_recovery_attempt,
)
from naumi_agent.orchestrator.pursuit_store import (
    PursuitStore,
    PursuitStoreConflictError,
    PursuitStoreError,
)


def _store_with_run(tmp_path) -> PursuitStore:
    store = PursuitStore(tmp_path / "pursuit")
    now = time.time()
    store.save_run(PursuitRun(
        id="pursuit-recovery-attempt",
        goal="验证恢复请求账本",
        status=PursuitRunStatus.WAITING,
        phase="waiting",
        started_at=now,
        updated_at=now,
    ))
    return store


def _attempt(*, requested_at: float = 10.0) -> PursuitRecoveryAttempt:
    return new_recovery_attempt(
        run_id="pursuit-recovery-attempt",
        source_request_id="tool-call-private-request",
        requested_at=requested_at,
    )


def test_recovery_attempt_identity_is_stable_and_does_not_retain_request() -> None:
    first = _attempt(requested_at=10.0)
    retry = _attempt(requested_at=99.0)

    assert first.attempt_id == retry.attempt_id
    assert first.source_request_sha256 == retry.source_request_sha256
    assert "tool-call-private-request" not in first.canonical_json()
    assert len(first.digest()) == 64


@pytest.mark.parametrize(
    "updates",
    [
        {"attempt_id": "recovery-bad"},
        {"attempt_id": "recovery-" + "f" * 64},
        {"run_id": "../foreign"},
        {"updated_at": float("inf")},
        {"updated_at": 253_402_300_800.0},
        {"state": "admitted", "sequence": 2},
        {
            "state": "resolved",
            "sequence": 2,
            "resolved_at": 11.0,
            "result_code": "",
        },
        {
            "state": "failed",
            "sequence": 2,
            "resolved_at": 11.0,
            "result_code": "internal_error",
            "boundary_decision_id": "a" * 64,
        },
    ],
)
def test_recovery_attempt_rejects_incoherent_state(updates) -> None:
    payload = _attempt().model_dump(mode="json")
    payload.update(updates)
    with pytest.raises(ValidationError):
        PursuitRecoveryAttempt.model_validate(payload)


def test_recovery_attempt_rejects_unbounded_source_identity() -> None:
    with pytest.raises(ValueError, match="最多 512"):
        new_recovery_attempt(
            run_id="pursuit-recovery-attempt",
            source_request_id="x" * 513,
            requested_at=10.0,
        )


def test_store_persists_hash_chained_recovery_lifecycle_and_reopens(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    requested, created = store.prepare_recovery_attempt(_attempt())
    admitted = store.mark_recovery_attempt_admitted(
        requested.attempt_id,
        admitted_at=11.0,
        lease_epoch=4,
        checkpoint_id="checkpoint-safe",
    )
    resolved = store.resolve_recovery_attempt(
        requested.attempt_id,
        resolved_at=12.0,
        result_code="waiting_for_background",
        boundary_decision_id="b" * 64,
    )

    assert created is True
    assert admitted.state is PursuitRecoveryAttemptState.ADMITTED
    assert admitted.sequence == 2
    assert resolved.state is PursuitRecoveryAttemptState.RESOLVED
    assert resolved.sequence == 3
    reopened = PursuitStore(store.base_dir)
    assert reopened.get_recovery_attempt(requested.attempt_id) == resolved
    assert reopened.list_recovery_attempts(requested.run_id) == [resolved]


def test_public_formatter_localizes_result_without_exposing_source_digest() -> None:
    attempt = _attempt()
    rendered = format_recovery_attempts([
        attempt.model_copy(update={
            "sequence": 2,
            "state": PursuitRecoveryAttemptState.RESOLVED,
            "updated_at": 11.0,
            "resolved_at": 11.0,
            "result_code": "checkpoint_required",
        })
    ])

    assert "缺少可恢复 checkpoint（checkpoint_required）" in rendered
    assert attempt.source_request_sha256 not in rendered
    assert "tool-call-private-request" not in rendered


def test_duplicate_request_is_idempotent_even_with_later_observed_time(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    first, created = store.prepare_recovery_attempt(_attempt(requested_at=10.0))
    duplicate, duplicate_created = store.prepare_recovery_attempt(
        _attempt(requested_at=100.0)
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate == first


def test_parallel_prepare_creates_exactly_one_attempt(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    attempt = _attempt()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: store.prepare_recovery_attempt(attempt),
            range(16),
        ))

    assert sum(created for _, created in results) == 1
    assert {item.attempt_id for item, _ in results} == {attempt.attempt_id}


def test_terminal_attempt_rejects_conflicting_rewrite(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    requested, _ = store.prepare_recovery_attempt(_attempt())
    store.resolve_recovery_attempt(
        requested.attempt_id,
        resolved_at=11.0,
        result_code="checkpoint_required",
    )

    with pytest.raises(PursuitStoreConflictError):
        store.resolve_recovery_attempt(
            requested.attempt_id,
            resolved_at=12.0,
            result_code="checkpoint_inconsistent",
        )


def test_store_rejects_tampered_snapshot_and_event_chain(tmp_path) -> None:
    store = _store_with_run(tmp_path)
    requested, _ = store.prepare_recovery_attempt(_attempt())
    store.mark_recovery_attempt_admitted(
        requested.attempt_id,
        admitted_at=11.0,
        lease_epoch=1,
        checkpoint_id="checkpoint-safe",
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            UPDATE pursuit_recovery_attempt_events
            SET previous_payload_sha256 = ?
            WHERE attempt_id = ? AND sequence = 2
            """,
            ("f" * 64, requested.attempt_id),
        )

    with pytest.raises(PursuitStoreError, match="哈希链断裂"):
        store.get_recovery_attempt(requested.attempt_id)


def test_attempt_requires_existing_run(tmp_path) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    with pytest.raises(PursuitStoreConflictError, match="PursuitRun 不存在"):
        store.prepare_recovery_attempt(_attempt())
