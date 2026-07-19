from __future__ import annotations

import hashlib
import json
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
    actor: PermissionDecisionActor = PermissionDecisionActor.USER,
    delegated_tool_names: tuple[str, ...] = (),
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
        actor=actor,
        source=source,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        delegated_tool_names=delegated_tool_names,
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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
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
async def test_policy_receipt_persists_sorted_delegation_scope(tmp_path: Path) -> None:
    store = PermissionDecisionReceiptStore(tmp_path / "decisions.db")
    receipt = await _issue(
        store,
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        source=PermissionDecisionSource.POLICY,
        actor=PermissionDecisionActor.RUNTIME,
        delegated_tool_names=("bash_run",),
    )

    assert receipt.authorizes_execution
    assert receipt.delegated_tool_names == ("bash_run",)
    assert await PermissionDecisionReceiptStore(store.db_path).get(
        receipt.receipt_id
    ) == receipt

    with pytest.raises(ValueError, match="唯一、排序"):
        await _issue(
            store,
            call_id="call-2",
            outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
            source=PermissionDecisionSource.POLICY,
            actor=PermissionDecisionActor.RUNTIME,
            delegated_tool_names=("z_tool", "a_tool"),
        )


@pytest.mark.asyncio
async def test_delegated_receipt_binds_parent_exact_args_expiry_and_no_chaining(
    tmp_path: Path,
) -> None:
    store = PermissionDecisionReceiptStore(tmp_path / "decisions.db")
    parent = await _issue(
        store,
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        source=PermissionDecisionSource.POLICY,
        actor=PermissionDecisionActor.RUNTIME,
        delegated_tool_names=("bash_run",),
    )
    child = await store.issue_delegated(
        parent_receipt_id=parent.receipt_id,
        request_id="shell-child-1",
        call_id="shell-child-1",
        tool_name="bash_run",
        tool_family="shell",
        arguments={"argv": ["/usr/bin/true"]},
        risk_level="high",
        decided_at="2026-07-19T08:00:01+00:00",
        ttl_seconds=30,
    )
    replay = await store.issue_delegated(
        parent_receipt_id=parent.receipt_id,
        request_id="shell-child-1",
        call_id="shell-child-1",
        tool_name="bash_run",
        tool_family="shell",
        arguments={"argv": ["/usr/bin/true"]},
        risk_level="high",
        decided_at="2026-07-19T08:00:02+00:00",
        ttl_seconds=30,
    )

    assert replay == child
    assert child.source is PermissionDecisionSource.DELEGATED
    assert child.outcome is PermissionDecisionOutcome.DELEGATED_ALLOWED
    assert child.actor is PermissionDecisionActor.RUNTIME
    assert child.parent_receipt_id == parent.receipt_id
    assert child.parent_receipt_sha256 == parent.receipt_sha256
    assert child.session_id == parent.session_id
    assert child.run_id == parent.run_id
    assert child.permission_mode is parent.permission_mode
    assert child.expires_at == "2026-07-19T08:00:31+00:00"
    assert child.delegated_tool_names == ()

    with pytest.raises(PermissionDecisionReceiptConflictError, match="不同"):
        await store.issue_delegated(
            parent_receipt_id=parent.receipt_id,
            request_id="shell-child-1",
            call_id="shell-child-1",
            tool_name="bash_run",
            tool_family="shell",
            arguments={"argv": ["/usr/bin/false"]},
            risk_level="high",
            decided_at="2026-07-19T08:00:02+00:00",
        )

    with pytest.raises(PermissionDecisionReceiptConflictError, match="不得继续"):
        await store.issue_delegated(
            parent_receipt_id=child.receipt_id,
            request_id="grandchild",
            call_id="grandchild",
            tool_name="bash_run",
            tool_family="shell",
            arguments={"argv": ["/usr/bin/true"]},
            risk_level="high",
            decided_at="2026-07-19T08:00:02+00:00",
        )
    with pytest.raises(PermissionDecisionReceiptConflictError, match="未授权"):
        await store.issue_delegated(
            parent_receipt_id=parent.receipt_id,
            request_id="browser-child",
            call_id="browser-child",
            tool_name="browser_run",
            tool_family="browser",
            arguments={},
            risk_level="high",
            decided_at="2026-07-19T08:00:02+00:00",
        )


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


@pytest.mark.asyncio
async def test_v1_receipt_is_read_and_store_migrates_without_rewriting_it(
    tmp_path: Path,
) -> None:
    store = PermissionDecisionReceiptStore(tmp_path / "decisions.db")
    receipt = await _issue(store)
    with sqlite3.connect(store.db_path) as db:
        payload = json.loads(
            db.execute(
                "SELECT receipt_json FROM permission_decisions WHERE receipt_id = ?",
                (receipt.receipt_id,),
            ).fetchone()[0]
        )
        payload["schema_version"] = 1
        payload.pop("delegated_tool_names")
        payload.pop("parent_receipt_id")
        payload.pop("parent_receipt_sha256")
        payload.pop("expires_at")
        digest_payload = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        payload["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        db.execute(
            "UPDATE permission_decisions SET receipt_sha256 = ?, receipt_json = ?",
            (payload["receipt_sha256"], raw),
        )
        db.execute("PRAGMA user_version = 1")
        db.commit()

    reopened = PermissionDecisionReceiptStore(store.db_path)
    restored = await reopened.get(receipt.receipt_id)
    assert restored is not None
    assert restored.schema_version == 1
    assert restored.delegated_tool_names == ()

    await _issue(reopened, call_id="call-2")
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_v2_receipt_is_read_and_store_migrates_to_v3(tmp_path: Path) -> None:
    store = PermissionDecisionReceiptStore(tmp_path / "decisions.db")
    receipt = await _issue(
        store,
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        source=PermissionDecisionSource.POLICY,
        actor=PermissionDecisionActor.RUNTIME,
        delegated_tool_names=("bash_run",),
    )
    with sqlite3.connect(store.db_path) as db:
        payload = json.loads(
            db.execute("SELECT receipt_json FROM permission_decisions").fetchone()[0]
        )
        payload["schema_version"] = 2
        for field in ("parent_receipt_id", "parent_receipt_sha256", "expires_at"):
            payload.pop(field)
        digest_payload = {
            key: value for key, value in payload.items() if key != "receipt_sha256"
        }
        payload["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        db.execute(
            "UPDATE permission_decisions SET receipt_sha256 = ?, receipt_json = ?",
            (payload["receipt_sha256"], raw),
        )
        db.execute("PRAGMA user_version = 2")
        db.commit()

    reopened = PermissionDecisionReceiptStore(store.db_path)
    restored = await reopened.get(receipt.receipt_id)
    assert restored is not None
    assert restored.schema_version == 2
    assert restored.delegated_tool_names == ("bash_run",)
    assert restored.parent_receipt_id == ""
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
