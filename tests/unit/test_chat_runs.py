from __future__ import annotations

import pytest

from naumi_agent.api.chat_runs import ChatRunStore


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
