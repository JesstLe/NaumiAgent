from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.pursuit_store import PursuitStore, PursuitStoreError
from naumi_agent.orchestrator.pursuit_terminal_outbox import (
    PursuitTerminalOutboxRunStatus,
    new_terminal_outbox_run_receipt,
)
from naumi_agent.orchestrator.pursuit_terminal_outbox_worker import (
    PursuitTerminalOutboxPassResult,
)


def _receipt(source_request_id: str = "request-1"):
    return new_terminal_outbox_run_receipt(
        source_request_id=source_request_id,
        status=PursuitTerminalOutboxRunStatus.COMPLETED,
        pending_before=2,
        pending_after=1,
        claimed=1,
        delivered=1,
        retry_scheduled=0,
        failures=0,
        failure_codes=(),
        created_at=10.0,
    )


def test_terminal_outbox_run_receipt_is_authenticated_and_identity_free() -> None:
    receipt = _receipt()

    assert receipt.receipt_id.startswith("ptorun_")
    assert "request-1" not in receipt.model_dump_json()
    with pytest.raises(ValidationError, match="digest"):
        receipt.model_copy(update={"pending_after": 0}).model_validate(
            receipt.model_copy(update={"pending_after": 0}).model_dump(mode="json")
        )


def test_terminal_outbox_run_receipt_store_is_first_write_wins(tmp_path) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    first, created = store.save_terminal_outbox_run_receipt(_receipt())
    assert created is True

    conflicting = new_terminal_outbox_run_receipt(
        source_request_id="request-1",
        status=PursuitTerminalOutboxRunStatus.NO_DUE,
        pending_before=0,
        pending_after=0,
        claimed=0,
        delivered=0,
        retry_scheduled=0,
        failures=0,
        failure_codes=(),
        created_at=11.0,
    )
    persisted, created = store.save_terminal_outbox_run_receipt(conflicting)

    assert created is False
    assert persisted == first
    assert store.get_terminal_outbox_run_receipt(first.source_request_sha256) == first

    with store._connect() as conn:
        conn.execute(
            "UPDATE pursuit_terminal_outbox_run_receipts "
            "SET receipt_id = ? WHERE source_request_sha256 = ?",
            ("ptorun_" + "f" * 24, first.source_request_sha256),
        )
    with pytest.raises(PursuitStoreError, match="存储摘要"):
        store.get_terminal_outbox_run_receipt(first.source_request_sha256)


@pytest.mark.asyncio
async def test_explicit_terminal_outbox_run_is_due_only_and_idempotent(tmp_path) -> None:
    store = PursuitStore(tmp_path / "pursuit")
    worker = SimpleNamespace(run_once=AsyncMock(return_value=PursuitTerminalOutboxPassResult()))
    engine = SimpleNamespace(
        pursuit_store=store,
        _pursuit_terminal_outbox_worker=worker,
        _config=SimpleNamespace(
            harness=SimpleNamespace(
                pursuit_terminal_outbox=SimpleNamespace(enabled=True),
            ),
        ),
    )

    first = await AgentEngine.run_pursuit_terminal_outbox_now(engine, "stable-request")
    second = await AgentEngine.run_pursuit_terminal_outbox_now(engine, "stable-request")

    assert first == second
    assert first.status is PursuitTerminalOutboxRunStatus.NO_DUE
    assert first.pending_before == first.pending_after == 0
    worker.run_once.assert_awaited_once()
