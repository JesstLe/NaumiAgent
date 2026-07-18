from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.daemons.permission_decisions import (
    PERMISSION_DECISION_SCHEMA_VERSION,
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionReceiptConflictError,
    PermissionDecisionReceiptError,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.safety.permissions import PermissionMode

NOW = "2026-07-19T08:00:00+00:00"


async def _issue(
    store: PermissionDecisionReceiptStore,
    *,
    call_id: str = "call-1",
    outcome: PermissionDecisionOutcome = PermissionDecisionOutcome.ALLOW_ONCE,
    source: PermissionDecisionSource = PermissionDecisionSource.USER_CONFIRMATION,
    arguments: dict[str, object] | None = None,
):
    return await store.issue(
        request_id=call_id,
        session_id="session-1",
        run_id="run-1",
        call_id=call_id,
        agent_name="main",
        tool_name="bash_run",
        tool_family="shell",
        arguments=arguments or {"command": "printf safe"},
        outcome=outcome,
        actor=PermissionDecisionActor.USER,
        source=source,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        decided_at=NOW,
    )


@pytest.mark.asyncio
async def test_issue_reopen_and_list_without_raw_arguments(tmp_path: Path) -> None:
    store = PermissionDecisionReceiptStore(tmp_path / "runtime" / "decisions.db")
    receipt = await _issue(
        store,
        arguments={"command": "printf super-secret-token"},
    )

    reopened = PermissionDecisionReceiptStore(store.db_path)

    assert await reopened.get(receipt.receipt_id) == receipt
    assert reopened.list_session("session-1") == (receipt,)
    assert receipt.authorizes_execution
    assert b"super-secret-token" not in store.db_path.read_bytes()
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    if os.name != "nt":
        assert store.db_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_retry_is_idempotent_and_conflicting_terminal_decision_fails(
    tmp_path: Path,
) -> None:
    store = PermissionDecisionReceiptStore(tmp_path / "decisions.db")
    first = await _issue(store)
    replay = await _issue(store)

    assert replay == first
    with pytest.raises(PermissionDecisionReceiptConflictError, match="不同"):
        await _issue(
            store,
            outcome=PermissionDecisionOutcome.DENIED,
            source=PermissionDecisionSource.USER_CONFIRMATION,
        )


@pytest.mark.asyncio
async def test_denied_receipt_never_authorizes_execution(tmp_path: Path) -> None:
    store = PermissionDecisionReceiptStore(tmp_path / "decisions.db")
    denied = await _issue(store, outcome=PermissionDecisionOutcome.DENIED)

    assert denied.authorizes_execution is False


@pytest.mark.asyncio
async def test_store_rejects_tamper_future_schema_and_wrong_type(tmp_path: Path) -> None:
    store = PermissionDecisionReceiptStore(tmp_path / "decisions.db")
    receipt = await _issue(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE permission_decisions SET receipt_json = '{}' WHERE receipt_id = ?",
            (receipt.receipt_id,),
        )
        db.commit()
    with pytest.raises(PermissionDecisionReceiptError, match="无法读取"):
        await PermissionDecisionReceiptStore(store.db_path).get(receipt.receipt_id)

    future = tmp_path / "future.db"
    with sqlite3.connect(future) as db:
        db.execute(f"PRAGMA user_version = {PERMISSION_DECISION_SCHEMA_VERSION + 1}")
    with pytest.raises(PermissionDecisionReceiptError, match="不受支持"):
        PermissionDecisionReceiptStore(future).list_session("session-1")

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(PermissionDecisionReceiptError, match="不是文件"):
        PermissionDecisionReceiptStore(directory).list_session("session-1")


def test_store_is_lazy_and_rejects_relative_paths(tmp_path: Path) -> None:
    path = tmp_path / "decisions.db"
    store = PermissionDecisionReceiptStore(path)

    assert store.list_session("session-1") == ()
    assert not path.exists()
    with pytest.raises(ValueError, match="绝对路径"):
        PermissionDecisionReceiptStore("relative.db")
