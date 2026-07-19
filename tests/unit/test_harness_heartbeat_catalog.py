from __future__ import annotations

import sqlite3

import pytest

from naumi_agent.harness.heartbeat import HarnessHeartbeatHealth, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore

ASSESSED = "2026-07-20T00:02:00+00:00"


async def _record(
    store: HarnessStore,
    workspace,
    subject_id: str,
    observed_at: str,
    *,
    kind: HarnessRunKind = HarnessRunKind.RUNTIME,
) -> None:
    await store.record_heartbeat(
        workspace_root=workspace,
        subject_kind=kind,
        subject_id=subject_id,
        instance_id=f"instance-{subject_id}",
        epoch=1,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=observed_at,
        timeout_seconds=30,
        detail_code="runtime_alive",
    )


@pytest.mark.asyncio
async def test_runtime_heartbeat_catalog_pages_without_gaps_or_duplicates(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    fixtures = (
        ("runtime-c", "2026-07-20T00:01:50+00:00"),
        ("runtime-a", "2026-07-20T00:01:50+00:00"),
        ("runtime-b", "2026-07-20T00:01:50+00:00"),
        ("runtime-new", "2026-07-20T00:01:55+00:00"),
        ("runtime-old", "2026-07-20T00:00:00+00:00"),
    )
    for subject_id, observed_at in fixtures:
        await _record(store, workspace, subject_id, observed_at)

    seen: list[str] = []
    cursor = ""
    page_count = 0
    while True:
        page = await store.list_runtime_heartbeats(
            workspace_root=workspace,
            assessed_at=ASSESSED,
            limit=2,
            cursor=cursor,
        )
        page_count += 1
        assert page.assessed_at == ASSESSED
        seen.extend(item.heartbeat.subject_id for item in page.items)
        if not page.has_more:
            assert page.next_cursor == ""
            break
        cursor = page.next_cursor
        store = HarnessStore(store.db_path)

    assert page_count == 3
    assert seen == [
        "runtime-new",
        "runtime-a",
        "runtime-b",
        "runtime-c",
        "runtime-old",
    ]
    assert len(seen) == len(set(seen))
    assert page.items[-1].health is HarnessHeartbeatHealth.OFFLINE
    with sqlite3.connect(store.db_path) as db:
        indexes = {
            row[1]
            for row in db.execute("PRAGMA index_list('harness_heartbeats')").fetchall()
        }
        query_plan = " ".join(
            str(row[3])
            for row in db.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM harness_heartbeats
                WHERE workspace_root = ? AND subject_kind = ?
                ORDER BY observed_at DESC, subject_id ASC
                LIMIT ?
                """,
                (str(workspace.resolve()), HarnessRunKind.RUNTIME.value, 2),
            ).fetchall()
        )
    assert "idx_harness_heartbeats_catalog" in indexes
    assert "idx_harness_heartbeats_catalog" in query_plan


@pytest.mark.asyncio
async def test_runtime_heartbeat_catalog_is_workspace_and_kind_isolated(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _record(store, first, "runtime-first", "2026-07-20T00:01:59+00:00")
    await _record(store, second, "runtime-second", "2026-07-20T00:01:59+00:00")
    await _record(
        store,
        first,
        "pursuit-first",
        "2026-07-20T00:01:59+00:00",
        kind=HarnessRunKind.PURSUIT,
    )

    page = await store.list_runtime_heartbeats(
        workspace_root=first,
        assessed_at=ASSESSED,
    )
    assert [item.heartbeat.subject_id for item in page.items] == ["runtime-first"]
    assert page.items[0].health is HarnessHeartbeatHealth.HEALTHY


@pytest.mark.asyncio
async def test_runtime_heartbeat_cursor_rejects_tamper_and_scope_drift(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _record(store, first, "runtime-a", "2026-07-20T00:01:59+00:00")
    await _record(store, first, "runtime-b", "2026-07-20T00:01:58+00:00")
    page = await store.list_runtime_heartbeats(
        workspace_root=first,
        assessed_at=ASSESSED,
        limit=1,
    )
    assert page.next_cursor

    with pytest.raises(ValueError, match="工作区"):
        await store.list_runtime_heartbeats(
            workspace_root=second,
            assessed_at=ASSESSED,
            limit=1,
            cursor=page.next_cursor,
        )
    with pytest.raises(ValueError, match="评估时间"):
        await store.list_runtime_heartbeats(
            workspace_root=first,
            assessed_at="2026-07-20T00:02:01+00:00",
            limit=1,
            cursor=page.next_cursor,
        )
    replacement = "A" if page.next_cursor[-1] != "A" else "B"
    tampered = f"{page.next_cursor[:-1]}{replacement}"
    with pytest.raises(ValueError, match="cursor"):
        await store.list_runtime_heartbeats(
            workspace_root=first,
            assessed_at=ASSESSED,
            limit=1,
            cursor=tampered,
        )


@pytest.mark.asyncio
async def test_runtime_heartbeat_cursor_does_not_backtrack_after_newer_insert(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _record(store, workspace, "runtime-first", "2026-07-20T00:01:59+00:00")
    await _record(store, workspace, "runtime-old", "2026-07-20T00:01:58+00:00")
    first = await store.list_runtime_heartbeats(
        workspace_root=workspace,
        assessed_at=ASSESSED,
        limit=1,
    )
    assert first.next_cursor

    await _record(store, workspace, "runtime-newer", ASSESSED)
    continuation = await store.list_runtime_heartbeats(
        workspace_root=workspace,
        assessed_at=ASSESSED,
        limit=10,
        cursor=first.next_cursor,
    )
    assert [item.heartbeat.subject_id for item in continuation.items] == [
        "runtime-old"
    ]
    refreshed = await store.list_runtime_heartbeats(
        workspace_root=workspace,
        assessed_at=ASSESSED,
        limit=1,
    )
    assert refreshed.items[0].heartbeat.subject_id == "runtime-newer"


@pytest.mark.asyncio
async def test_runtime_heartbeat_catalog_handles_empty_and_invalid_requests(tmp_path) -> None:
    store = HarnessStore(tmp_path / "missing.db")
    empty = await store.list_runtime_heartbeats(
        workspace_root=tmp_path,
        assessed_at=ASSESSED,
    )
    assert empty.items == ()
    assert not empty.has_more

    with pytest.raises(ValueError, match="limit"):
        await store.list_runtime_heartbeats(
            workspace_root=tmp_path,
            assessed_at=ASSESSED,
            limit=201,
        )
    with pytest.raises(ValueError, match="cursor"):
        await store.list_runtime_heartbeats(
            workspace_root=tmp_path,
            assessed_at=ASSESSED,
            cursor="not-a-valid-cursor",
        )
