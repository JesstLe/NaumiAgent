from __future__ import annotations

import importlib
import importlib.util
import json

import aiosqlite
import pytest

from naumi_agent.api.chat_runs import ChatRunStore
from naumi_agent.runs.models import CompletionReceipt, ReceiptChange


def _minimal_receipt(run_id: str, *, receipt_id: str = "receipt-1") -> CompletionReceipt:
    return CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": receipt_id,
            "run_id": run_id,
            "outcome": "completed",
            "summary": "已完成并保存证据。",
            "git_state": {"available": False, "dirty": False},
            "started_at": "2026-07-13T00:00:00+00:00",
            "completed_at": "2026-07-13T00:00:01+00:00",
            "duration_ms": 1000,
        }
    )


def test_neutral_run_store_is_the_canonical_implementation() -> None:
    from naumi_agent.api.chat_runs import ChatRunStore as ApiChatRunStore
    from naumi_agent.runs.store import ChatRunStore as NeutralChatRunStore

    assert NeutralChatRunStore.__module__ == "naumi_agent.runs.store"
    assert ApiChatRunStore is NeutralChatRunStore


def test_completion_receipt_round_trips_all_public_evidence() -> None:
    spec = importlib.util.find_spec("naumi_agent.runs.models")
    assert spec is not None, "neutral run receipt models must exist"
    models = importlib.import_module("naumi_agent.runs.models")

    receipt = models.CompletionReceipt(
        schema_version=1,
        receipt_id="receipt-1",
        run_id="run-1",
        outcome="partial",
        summary="完成实现，但验证失败。",
        changes=(
            models.ReceiptChange(
                path="src/app.py",
                status="modified",
                source_tool="file_edit",
                additions=4,
                deletions=1,
            ),
        ),
        validations=(
            models.ReceiptValidation(
                command="python3 -m pytest tests/unit/test_app.py -q",
                scope="tests/unit/test_app.py",
                status="failed",
                exit_code=1,
                passed=3,
                failed=1,
                skipped=0,
                log_ref="run:run-1:tool:call-2",
            ),
        ),
        unverified=("未运行完整测试套件",),
        approvals=(
            models.ReceiptApproval(
                call_id="call-1",
                tool_name="bash_run",
                decision="allowed_once",
                scope="本次调用",
            ),
        ),
        risks=(
            models.ReceiptRisk(
                code="validation_failed",
                level="high",
                message="1 项验证失败",
            ),
        ),
        git_state=models.ReceiptGitState(
            available=True,
            branch="codex/test",
            dirty=True,
            commit="abc123",
            ahead=1,
            behind=0,
        ),
        next_actions=(
            models.ReceiptAction(
                id="retry-validation",
                label="重试失败验证",
                kind="retry_validation",
            ),
        ),
        evidence_refs=("run:run-1:tool:call-2",),
        started_at="2026-07-13T00:00:00+00:00",
        completed_at="2026-07-13T00:00:02+00:00",
        duration_ms=2000,
    )

    restored = models.CompletionReceipt.from_dict(receipt.to_dict())

    assert restored == receipt


def test_completion_receipt_rejects_unsupported_schema_version() -> None:
    spec = importlib.util.find_spec("naumi_agent.runs.models")
    assert spec is not None, "neutral run receipt models must exist"
    models = importlib.import_module("naumi_agent.runs.models")

    with pytest.raises(ValueError, match="schema_version"):
        models.CompletionReceipt.from_dict(
            {
                "schema_version": 2,
                "receipt_id": "receipt-1",
                "run_id": "run-1",
                "outcome": "completed",
            }
        )


@pytest.mark.asyncio
async def test_run_store_restores_ordered_steps_and_artifacts_after_restart(tmp_path):
    db_path = tmp_path / "chat-runs.db"
    store = ChatRunStore(db_path)
    run = await store.start_run(session_id="s1", user_message_id="m1")
    await store.append_step(
        run.id,
        sequence=2,
        stage="tool",
        status="completed",
        summary="运行测试",
    )
    await store.append_step(
        run.id,
        sequence=1,
        stage="analysis",
        status="completed",
        summary="分析请求",
    )
    await store.append_artifact(
        run.id,
        kind="validation",
        title="pytest",
        summary={"passed": 2, "failed": 0},
        status="success",
    )
    await store.finish_run(run.id, status="completed", assistant_message_id="m2")

    restored = await ChatRunStore(db_path).get_run("s1", run.id)

    assert restored is not None
    assert restored.status == "completed"
    assert restored.assistant_message_id == "m2"
    assert [step.sequence for step in restored.steps] == [1, 2]
    assert restored.artifacts[0].kind == "validation"
    assert restored.artifacts[0].summary == {"passed": 2, "failed": 0}


@pytest.mark.asyncio
async def test_run_store_isolates_sessions(tmp_path):
    store = ChatRunStore(tmp_path / "chat-runs.db")
    run = await store.start_run(session_id="s1", user_message_id="m1")

    assert await store.get_run("s2", run.id) is None
    assert await store.list_runs("s2") == []


@pytest.mark.asyncio
async def test_run_store_upserts_same_step_sequence(tmp_path):
    store = ChatRunStore(tmp_path / "chat-runs.db")
    run = await store.start_run(session_id="s1", user_message_id="m1")
    await store.append_step(
        run.id,
        sequence=1,
        stage="tool",
        status="running",
        summary="bash_run",
    )
    await store.append_step(
        run.id,
        sequence=1,
        stage="approval",
        status="awaiting_approval",
        summary="等待确认",
    )

    restored = await store.get_run("s1", run.id)

    assert restored is not None
    assert len(restored.steps) == 1
    assert restored.steps[0].stage == "approval"
    assert restored.steps[0].status == "awaiting_approval"


@pytest.mark.asyncio
async def test_source_references_persist_and_remain_session_isolated(tmp_path):
    db_path = tmp_path / "chat-runs.db"
    store = ChatRunStore(db_path)
    source = await store.add_source(
        session_id="s1",
        kind="file",
        title="spec.md",
        path="docs/spec.md",
    )

    restored = await ChatRunStore(db_path).list_sources("s1")

    assert [item.id for item in restored] == [source.id]
    assert restored[0].path == "docs/spec.md"
    assert await store.list_sources("s2") == []


@pytest.mark.asyncio
async def test_run_store_persists_receipt_and_isolates_receipt_lookup(tmp_path):
    db_path = tmp_path / "chat-runs.db"
    store = ChatRunStore(db_path)
    run = await store.start_run(session_id="s1", user_message_id="m1")
    receipt = _minimal_receipt(run.id)

    await store.finish_run(run.id, status="completed", receipt=receipt)

    reopened = ChatRunStore(db_path)
    restored_run = await reopened.get_run("s1", run.id)
    assert restored_run is not None
    assert restored_run.receipt == receipt
    assert await reopened.get_receipt("s1", receipt.receipt_id) == receipt
    assert await reopened.get_receipt("s2", receipt.receipt_id) is None


@pytest.mark.asyncio
async def test_run_store_migrates_old_chat_runs_table_without_data_loss(tmp_path):
    db_path = tmp_path / "chat-runs.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE chat_runs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_message_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                assistant_message_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        await db.execute(
            """
            INSERT INTO chat_runs (
                id, session_id, user_message_id, status, started_at, updated_at
            ) VALUES ('run-old', 's1', 'm1', 'completed', 'start', 'end')
            """
        )
        await db.commit()

    restored = await ChatRunStore(db_path).get_run("s1", "run-old")

    assert restored is not None
    assert restored.id == "run-old"
    assert restored.receipt is None
    async with aiosqlite.connect(db_path) as db:
        columns = await (await db.execute("PRAGMA table_info(chat_runs)")).fetchall()
    column_names = {column[1] for column in columns}
    assert {"receipt_id", "receipt_json"}.issubset(column_names)


@pytest.mark.asyncio
async def test_run_store_rejects_receipt_for_another_run(tmp_path):
    store = ChatRunStore(tmp_path / "chat-runs.db")
    run = await store.start_run(session_id="s1", user_message_id="m1")

    with pytest.raises(ValueError, match="run_id"):
        await store.finish_run(
            run.id,
            status="completed",
            receipt=_minimal_receipt("different-run"),
        )


@pytest.mark.asyncio
async def test_run_store_keeps_run_readable_when_receipt_json_is_corrupt(tmp_path):
    db_path = tmp_path / "chat-runs.db"
    store = ChatRunStore(db_path)
    run = await store.start_run(session_id="s1", user_message_id="m1")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE chat_runs SET receipt_json = ? WHERE id = ?",
            ("{not-json", run.id),
        )
        await db.commit()

    restored = await ChatRunStore(db_path).get_run("s1", run.id)

    assert restored is not None
    assert restored.receipt is None


@pytest.mark.asyncio
async def test_run_store_finds_old_receipt_beyond_recent_run_window(tmp_path):
    store = ChatRunStore(tmp_path / "chat-runs.db")
    oldest_receipt: CompletionReceipt | None = None
    for index in range(201):
        run = await store.start_run(
            session_id="s1",
            user_message_id=f"m-{index}",
        )
        receipt = _minimal_receipt(run.id, receipt_id=f"receipt-{index}")
        await store.finish_run(run.id, status="completed", receipt=receipt)
        if index == 0:
            oldest_receipt = receipt

    assert oldest_receipt is not None
    assert await store.get_receipt("s1", oldest_receipt.receipt_id) == oldest_receipt


@pytest.mark.asyncio
async def test_run_store_normalizes_receipt_before_writing_sqlite(tmp_path):
    db_path = tmp_path / "chat-runs.db"
    store = ChatRunStore(db_path)
    run = await store.start_run(session_id="s1", user_message_id="m1")
    oversized = CompletionReceipt(
        schema_version=1,
        receipt_id="receipt-bounded",
        run_id=run.id,
        outcome="completed",
        summary="证" * 2_100,
        changes=tuple(
            ReceiptChange(path=f"file-{index}.txt", status="modified")
            for index in range(101)
        ),
    )

    await store.finish_run(run.id, status="completed", receipt=oversized)

    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT receipt_json FROM chat_runs WHERE id = ?",
                (run.id,),
            )
        ).fetchone()
    assert row is not None
    raw_receipt = json.loads(row[0])
    assert len(raw_receipt["summary"]) == 2_000
    assert len(raw_receipt["changes"]) == 100
