from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.harness.sandbox_retry_retention import (
    HarnessSandboxRetryRetentionPreview,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore
from tests.unit.test_harness_sandbox_retry_catalog import (
    _create_dispatch,
    _workspace,
)

ASSESSED = "2026-07-24T01:06:30+00:00"


def _database_facts(path: Path) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    with sqlite3.connect(path) as db:
        tables = tuple(
            row[0]
            for row in db.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'harness_%'
                ORDER BY name
                """
            ).fetchall()
        )
        return tuple(
            (
                table,
                tuple(
                    db.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    ).fetchall()
                ),
            )
            for table in tables
        )


@pytest.mark.asyncio
async def test_retry_retention_preview_is_bounded_read_only_and_explainable(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    for source, retry, ticket, minute in (
        ("1", "a", "2", 4),
        ("3", "b", "4", 5),
        ("5", "c", "6", 6),
    ):
        await _create_dispatch(
            store,
            workspace,
            source_token=source,
            retry_token=retry,
            ticket_token=ticket,
            minute=minute,
            lease_seconds=300,
            terminal="completed",
            persisted_samples=1,
        )
    await _create_dispatch(
        store,
        workspace,
        source_token="7",
        retry_token="d",
        ticket_token="8",
        minute=6,
        lease_seconds=300,
    )
    before = _database_facts(store.db_path)
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(store.db_path),
    )

    preview = await service.sandbox_retry_retention_preview(
        retention_days=1,
        limit=2,
        scan_limit=2,
        assessed_at=ASSESSED,
    )

    assert _database_facts(store.db_path) == before
    assert preview.status == "ok"
    assert preview.total_dispatch_count == 4
    assert preview.open_dispatch_count == 1
    assert preview.terminal_dispatch_count == 3
    assert preview.eligible_count == 3
    assert preview.scanned_count == 2
    assert preview.selected_count == 2
    assert preview.deferred_eligible_count == 1
    assert preview.scan_truncated
    assert preview.selection_truncated
    assert [item.retry_action_id for item in preview.candidates] == [
        f"hsar_{'a' * 24}",
        f"hsar_{'b' * 24}",
    ]
    assert all(item.terminal_state == "completed" for item in preview.candidates)
    assert all(item.age_seconds >= 86_400 for item in preview.candidates)
    assert all(
        {ref.kind for ref in item.protection_refs}
        == {
            "dispatch",
            "retry_receipt",
            "cancel_receipt",
            "request_manifest",
            "source_ticket",
            "ticket",
            "h5a_sample",
        }
        for item in preview.candidates
    )
    payload = preview.model_dump_json()
    assert str(workspace.resolve()) not in payload
    assert "owner_id" not in payload
    assert "execution_authority" not in payload
    assert "catalog-test" not in payload


@pytest.mark.asyncio
async def test_retry_retention_preview_excludes_open_and_recent_terminal(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    await _create_dispatch(
        store,
        workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=6,
        lease_seconds=300,
        terminal="failed",
    )
    await _create_dispatch(
        store,
        workspace,
        source_token="3",
        retry_token="b",
        ticket_token="4",
        minute=6,
        lease_seconds=300,
        claim=False,
    )

    preview = await HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=store,
    ).sandbox_retry_retention_preview(
        retention_days=30,
        assessed_at=ASSESSED,
    )

    assert preview.terminal_dispatch_count == 1
    assert preview.open_dispatch_count == 1
    assert preview.eligible_count == 0
    assert preview.candidates == ()
    assert not preview.scan_truncated


@pytest.mark.asyncio
async def test_retry_retention_preview_is_workspace_scoped(tmp_path: Path) -> None:
    first = _workspace(tmp_path, "first")
    second = _workspace(tmp_path, "second")
    store = HarnessStore(tmp_path / "harness.db")
    for workspace in (first, second):
        await _create_dispatch(
            store,
            workspace,
            source_token="1",
            retry_token="a",
            ticket_token="2",
            minute=6,
            lease_seconds=300,
            terminal="completed",
        )

    preview = await HarnessService(
        workspace_root=first,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=store,
    ).sandbox_retry_retention_preview(
        retention_days=1,
        assessed_at=ASSESSED,
    )

    assert preview.total_dispatch_count == 1
    assert preview.eligible_count == 1
    assert len(preview.candidates) == 1
    assert preview.workspace_sha256 != "0" * 64
    assert str(second.resolve()) not in preview.model_dump_json()


@pytest.mark.asyncio
async def test_retry_retention_preview_does_not_initialize_empty_database(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    database = tmp_path / "empty.db"
    database.touch()
    preview = await HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(database),
    ).sandbox_retry_retention_preview(
        assessed_at=ASSESSED,
    )

    assert preview.status == "ok"
    assert preview.total_dispatch_count == 0
    assert database.read_bytes() == b""


@pytest.mark.asyncio
async def test_retry_retention_tool_is_read_only_and_preview_is_tamper_evident(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    await _create_dispatch(
        store,
        workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=6,
        lease_seconds=300,
        terminal="cancelled",
    )
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=store,
    )
    tool = next(
        item
        for item in create_harness_tools(service)
        if item.name == "harness_eval_sandbox_retry_retention_preview"
    )

    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert "参数无效" in await tool.execute(limit=True)
    rendered = await tool.execute(
        retention_days=1,
        limit=1,
        scan_limit=1,
        assessed_at=ASSESSED,
    )
    assert "Sandbox retry retention 预览" in rendered
    assert "不会生成 prune receipt" in rendered
    assert "保护引用" in rendered
    assert str(workspace.resolve()) not in rendered

    preview = await service.sandbox_retry_retention_preview(
        retention_days=1,
        limit=1,
        scan_limit=1,
        assessed_at=ASSESSED,
    )
    tampered = preview.model_dump(mode="json")
    tampered["candidates"][0]["protection_refs_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="protection refs"):
        HarnessSandboxRetryRetentionPreview.model_validate(tampered)
