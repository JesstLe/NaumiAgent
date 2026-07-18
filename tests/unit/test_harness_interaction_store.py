from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.interaction import (
    HarnessInteractionRecord,
    new_interaction_record,
)
from naumi_agent.harness.interaction_runtime import (
    DurableInteractionAuthorityClient,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoreError,
)
from naumi_agent.user_interaction import normalize_interaction_request

T0 = "2026-07-18T00:00:00+00:00"
T4 = "2026-07-18T00:00:04+00:00"
T11 = "2026-07-18T00:00:11+00:00"
T20 = "2026-07-18T00:00:20+00:00"


def _record(*, interaction_id: str = "ask-durable-1", timeout: int = 20):
    request = normalize_interaction_request({
        "header": "执行策略",
        "question": "请选择恢复方式",
        "options": [
            {"value": "safe", "label": "安全恢复", "description": "先核对状态"},
            {"value": "restart", "label": "重新开始", "description": "放弃旧运行"},
        ],
        "allow_custom": True,
        "custom_label": "其他方案",
    })
    return new_interaction_record(
        request=request,
        subject_kind="pursuit",
        subject_id="pursuit-1",
        session_id="session-1",
        agent_name="main",
        owner_id="bridge-a",
        created_at=T0,
        owner_lease_seconds=10,
        timeout_seconds=timeout,
        interaction_id=interaction_id,
    )


def test_interaction_record_rejects_unredacted_durable_secret() -> None:
    record = _record()
    payload = record.model_dump(mode="python")
    payload["question"] = "api_key=must-not-persist"

    with pytest.raises(ValidationError, match="脱敏"):
        HarnessInteractionRecord.model_validate(payload)


def test_interaction_record_rejects_noncanonical_timeout() -> None:
    payload = _record().model_dump(mode="python")
    payload["expires_at"] = "2026-07-28T00:00:01+00:00"

    with pytest.raises(ValidationError, match="3..604800"):
        HarnessInteractionRecord.model_validate(payload)


@pytest.mark.asyncio
async def test_interaction_survives_new_store_and_create_is_idempotent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = _record()
    first = HarnessStore(db_path)

    assert await first.create_interaction(
        workspace_root=workspace,
        record=record,
    ) == record
    assert await first.create_interaction(
        workspace_root=workspace,
        record=record,
    ) == record

    reopened = HarnessStore(db_path)
    assert await reopened.get_interaction(
        workspace_root=workspace,
        interaction_id=record.interaction_id,
    ) == record
    assert await reopened.list_pending_interactions(
        workspace_root=workspace,
        subject_kind="pursuit",
        subject_id="pursuit-1",
    ) == (record,)

    with sqlite3.connect(db_path) as db:
        event_count = db.execute(
            "SELECT COUNT(*) FROM harness_interaction_events"
        ).fetchone()[0]
    assert event_count == 1

    changed_payload = record.model_dump(mode="python")
    changed_payload["question"] = "请选择不同的恢复方式"
    changed = HarnessInteractionRecord.model_validate(changed_payload)
    with pytest.raises(HarnessStoreConflictError, match="不同问题"):
        await reopened.create_interaction(
            workspace_root=workspace,
            record=changed,
        )


@pytest.mark.asyncio
async def test_answer_is_owner_epoch_and_sequence_fenced(tmp_path: Path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = await store.create_interaction(
        workspace_root=workspace,
        record=_record(),
    )

    with pytest.raises(ValueError, match="owner/epoch"):
        await store.answer_interaction(
            workspace_root=workspace,
            interaction_id=record.interaction_id,
            expected_sequence=1,
            owner_id="bridge-b",
            owner_epoch=1,
            response={"kind": "option", "value": "safe"},
            answered_by="user",
            now=T4,
        )

    answered = await store.answer_interaction(
        workspace_root=workspace,
        interaction_id=record.interaction_id,
        expected_sequence=1,
        owner_id="bridge-a",
        owner_epoch=1,
        response={"kind": "option", "value": "safe"},
        answered_by="user",
        now=T4,
    )
    assert answered.state == "answered"
    assert answered.sequence == 2
    assert answered.answer_value == "safe"
    assert answered.answer_label == "安全恢复"

    with pytest.raises(HarnessStoreConflictError, match="sequence"):
        await store.answer_interaction(
            workspace_root=workspace,
            interaction_id=record.interaction_id,
            expected_sequence=1,
            owner_id="bridge-a",
            owner_epoch=1,
            response={"kind": "option", "value": "restart"},
            answered_by="user",
            now=T4,
        )


@pytest.mark.asyncio
async def test_takeover_requires_expired_owner_lease_and_fences_old_owner(
    tmp_path: Path,
) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = await store.create_interaction(
        workspace_root=workspace,
        record=_record(),
    )
    with pytest.raises(ValueError, match="仍有效"):
        await store.takeover_interaction(
            workspace_root=workspace,
            interaction_id=record.interaction_id,
            expected_sequence=1,
            owner_id="bridge-b",
            now=T4,
            owner_lease_seconds=10,
        )

    claimed = await store.takeover_interaction(
        workspace_root=workspace,
        interaction_id=record.interaction_id,
        expected_sequence=1,
        owner_id="bridge-b",
        now=T11,
        owner_lease_seconds=10,
    )
    assert claimed.sequence == 2
    assert claimed.owner_id == "bridge-b"
    assert claimed.owner_epoch == 2

    with pytest.raises(ValueError, match="owner/epoch"):
        await store.answer_interaction(
            workspace_root=workspace,
            interaction_id=record.interaction_id,
            expected_sequence=2,
            owner_id="bridge-a",
            owner_epoch=1,
            response={"kind": "option", "value": "safe"},
            answered_by="user",
            now="2026-07-18T00:00:12+00:00",
        )


@pytest.mark.asyncio
async def test_runtime_client_renews_expired_same_owner_before_answer(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    client = DurableInteractionAuthorityClient(
        store=store,
        workspace_root=workspace,
        owner_id="bridge-a",
        owner_lease_seconds=10,
    )
    record = await client.create(
        request=_record().request(),
        interaction_id="ask-runtime-renew",
        subject_kind="pursuit",
        subject_id="pursuit-1",
        session_id="session-1",
        agent_name="main",
        now=T0,
    )

    recovery = await client.recover_pending(now=T11)

    assert len(recovery.claimed) == 1
    renewed = recovery.claimed[0]
    assert renewed.sequence == record.sequence + 1
    assert renewed.owner_id == record.owner_id
    assert renewed.owner_epoch == record.owner_epoch
    answered, response = await client.answer(
        record=renewed,
        response={"kind": "option", "value": "safe"},
        now="2026-07-18T00:00:12+00:00",
    )
    assert answered.state == "answered"
    assert response["label"] == "安全恢复"


@pytest.mark.asyncio
async def test_timeout_is_explicit_transition_and_removes_pending(tmp_path: Path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = await store.create_interaction(
        workspace_root=workspace,
        record=_record(timeout=20),
    )
    with pytest.raises(ValueError, match="尚未"):
        await store.expire_interaction(
            workspace_root=workspace,
            interaction_id=record.interaction_id,
            expected_sequence=1,
            now=T11,
        )
    expired = await store.expire_interaction(
        workspace_root=workspace,
        interaction_id=record.interaction_id,
        expected_sequence=1,
        now=T20,
    )
    assert expired.state == "expired"
    assert expired.sequence == 2
    assert await store.list_pending_interactions(
        workspace_root=workspace,
    ) == ()


@pytest.mark.asyncio
async def test_cancel_is_sequence_fenced_and_visible_in_bounded_history(
    tmp_path: Path,
) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = await store.create_interaction(
        workspace_root=workspace,
        record=_record(interaction_id="ask-cancel-first"),
    )
    second = await store.create_interaction(
        workspace_root=workspace,
        record=_record(interaction_id="ask-cancel-second"),
    )

    cancelled = await store.cancel_interaction(
        workspace_root=workspace,
        interaction_id=first.interaction_id,
        expected_sequence=first.sequence,
        now=T4,
    )

    assert cancelled.state == "cancelled"
    assert cancelled.sequence == 2
    assert await store.list_pending_interactions(workspace_root=workspace) == (second,)
    assert await store.list_interactions(
        workspace_root=workspace,
        subject_kind="pursuit",
        subject_ids=("pursuit-1",),
        limit=2,
    ) == (
        second,
        cancelled,
    )
    with pytest.raises(HarnessStoreConflictError, match="sequence"):
        await store.cancel_interaction(
            workspace_root=workspace,
            interaction_id=first.interaction_id,
            expected_sequence=first.sequence,
            now=T11,
        )


@pytest.mark.asyncio
async def test_tampered_event_chain_is_rejected_without_payload_leak(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = _record()
    await HarnessStore(db_path).create_interaction(
        workspace_root=workspace,
        record=record,
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE harness_interaction_events SET payload_json = ?",
            ('{"private":"secret"}',),
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="摘要校验失败") as error:
        await HarnessStore(db_path).get_interaction(
            workspace_root=workspace,
            interaction_id=record.interaction_id,
        )
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_tampered_snapshot_metadata_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = _record()
    await HarnessStore(db_path).create_interaction(
        workspace_root=workspace,
        record=record,
    )
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE harness_interactions SET owner_id = 'bridge-tampered'")
        db.commit()

    with pytest.raises(HarnessStoreError, match="快照与事件链"):
        await HarnessStore(db_path).get_interaction(
            workspace_root=workspace,
            interaction_id=record.interaction_id,
        )


@pytest.mark.asyncio
async def test_parallel_answers_commit_exactly_one_terminal_event(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = _record()
    await HarnessStore(db_path).create_interaction(
        workspace_root=workspace,
        record=record,
    )

    async def answer(value: str):
        return await HarnessStore(db_path).answer_interaction(
            workspace_root=workspace,
            interaction_id=record.interaction_id,
            expected_sequence=1,
            owner_id="bridge-a",
            owner_epoch=1,
            response={"kind": "option", "value": value},
            answered_by="user",
            now=T4,
        )

    results = await asyncio.gather(
        answer("safe"),
        answer("restart"),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, HarnessStoreConflictError) for item in results) == 1
    with sqlite3.connect(db_path) as db:
        events = db.execute(
            "SELECT sequence, state FROM harness_interaction_events ORDER BY sequence"
        ).fetchall()
    assert events == [(1, "pending"), (2, "answered")]


@pytest.mark.asyncio
async def test_v12_database_adds_interaction_tables_without_losing_heartbeat(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = HarnessStore(db_path)
    await initial.record_heartbeat(
        workspace_root=workspace,
        subject_kind=HarnessRunKind.PURSUIT,
        subject_id="pursuit-1",
        instance_id="worker-a",
        epoch=1,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=T0,
        timeout_seconds=30,
        detail_code="lease_active",
    )
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE harness_interaction_events")
        db.execute("DROP TABLE harness_interactions")
        db.execute("PRAGMA user_version = 12")
        db.commit()

    upgraded = HarnessStore(db_path)
    await upgraded.create_interaction(
        workspace_root=workspace,
        record=_record(),
    )

    heartbeat = await upgraded.get_heartbeat(
        workspace_root=workspace,
        subject_kind=HarnessRunKind.PURSUIT,
        subject_id="pursuit-1",
    )
    assert heartbeat is not None
    assert heartbeat.instance_id == "worker-a"
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 13
        assert db.execute(
            "SELECT COUNT(*) FROM harness_interactions"
        ).fetchone()[0] == 1
