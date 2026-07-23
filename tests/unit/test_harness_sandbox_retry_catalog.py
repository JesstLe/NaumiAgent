from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from naumi_agent.harness.eval_models import EvalRunStatus, HarnessEvalSuiteResult
from naumi_agent.harness.models import (
    HarnessCheckSpec,
    HarnessEvalSpec,
    HarnessProfile,
)
from naumi_agent.harness.sandbox_request import HarnessSandboxEvalRequestBuilder
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore

ASSESSED = "2026-07-23T01:06:30+00:00"


def _workspace(tmp_path: Path, name: str = "workspace") -> Path:
    workspace = tmp_path / name
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "harness@example.invalid"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Harness Test"],
        cwd=workspace,
        check=True,
    )
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "initial"],
        cwd=workspace,
        check=True,
    )
    return workspace


def _profile() -> HarnessProfile:
    return HarnessProfile(
        schema_version=1,
        checks=(
            HarnessCheckSpec(
                id="unit",
                label="定向单测",
                argv=("python3", "-c", "print('ok')"),
                timeout_seconds=30,
                provides=("unit",),
            ),
        ),
        evals=HarnessEvalSpec(max_duration_seconds=600),
    )


async def _create_dispatch(
    store: HarnessStore,
    workspace: Path,
    *,
    source_token: str,
    retry_token: str,
    ticket_token: str,
    minute: int,
    lease_seconds: int,
    terminal: str = "",
    persisted_samples: int = 0,
):
    prefix = f"2026-07-23T01:{minute:02d}"
    request = HarnessSandboxEvalRequestBuilder().build(
        workspace_root=workspace,
        profile=_profile(),
        profile_digest="a" * 64,
        profile_trusted=True,
        check_ids=("unit",),
        batch_id=f"sandbox-retry-catalog-{retry_token}",
        requested_samples=5,
    )
    await store.record_sandbox_eval_request(
        request,
        created_at=f"{prefix}:00+00:00",
    )
    source = await store.enqueue_sandbox_admission(
        workspace_root=workspace,
        ticket_id=f"hsadm_{source_token * 24}",
        authority_key=request.request_sha256,
        lane="sandbox",
        requested_samples=5,
        owner_id=f"source-{source_token}",
        now=f"{prefix}:01+00:00",
        lease_seconds=300,
        max_active=10,
        max_queued=10,
    )
    cancel, _ = await store.cancel_sandbox_admission(
        workspace_root=workspace,
        action_id=f"hsac_{source_token * 24}",
        ticket_id=source.ticket_id,
        authority_key=source.authority_key,
        epoch=source.epoch,
        expected_state=source.state,
        actor_id="catalog-test",
        reason="构造 catalog retry",
        now=f"{prefix}:02+00:00",
    )
    retry = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{retry_token * 24}",
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256=cancel.receipt_sha256,
        actor_id="catalog-test",
        reason="构造 catalog dispatch",
        authority_token=retry_token * 32,
        now=f"{prefix}:03+00:00",
    )
    dispatch, ticket = await store.claim_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
        retry_receipt_id=retry.receipt_id,
        retry_receipt_sha256=retry.receipt_sha256,
        ticket_id=f"hsadm_{ticket_token * 24}",
        owner_id=f"retry-{retry_token}",
        now=f"{prefix}:04+00:00",
        lease_seconds=lease_seconds,
        max_active=10,
        max_queued=10,
    )
    for sample_index in range(persisted_samples):
        await store.record_eval_result(
            workspace_root=workspace,
            batch_id=request.batch_id,
            sample_index=sample_index,
            result=HarnessEvalSuiteResult(
                suite_id=request.suite_id,
                title="Catalog fixture",
                suite_path="catalog",
                status=EvalRunStatus.PASSED,
            ),
            created_at=f"{prefix}:04+00:00",
        )
    if terminal:
        finished_ticket = await store.finish_sandbox_admission(
            workspace_root=workspace,
            ticket_id=ticket.ticket_id,
            owner_id=ticket.owner_id,
            epoch=ticket.epoch,
            state=terminal,
            terminal_code="",
            now=f"{prefix}:05+00:00",
        )
        dispatch = await store.finish_sandbox_retry_dispatch(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
            owner_id=dispatch.owner_id,
            dispatch_epoch=dispatch.epoch,
            ticket_id=finished_ticket.ticket_id,
            ticket_epoch=finished_ticket.epoch,
            state=terminal,
            terminal_code="",
            now=f"{prefix}:05+00:00",
        )
    return request, retry, dispatch


@pytest.mark.asyncio
async def test_retry_catalog_pages_and_classifies_durable_recovery(
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
        minute=4,
        lease_seconds=300,
        terminal="completed",
    )
    recovery_request, recovery, _ = await _create_dispatch(
        store,
        workspace,
        source_token="3",
        retry_token="b",
        ticket_token="4",
        minute=5,
        lease_seconds=2,
        persisted_samples=2,
    )
    await _create_dispatch(
        store,
        workspace,
        source_token="5",
        retry_token="c",
        ticket_token="6",
        minute=6,
        lease_seconds=300,
    )

    first = await store.list_sandbox_retry_dispatches(
        workspace_root=workspace,
        assessed_at=ASSESSED,
        limit=2,
    )
    second = await HarnessStore(store.db_path).list_sandbox_retry_dispatches(
        workspace_root=workspace,
        limit=2,
        cursor=first.next_cursor,
    )

    assert first.has_more
    assert [item.recovery_status for item in first.items] == [
        "live",
        "recovery_required",
    ]
    assert first.items[1].dispatch.retry_action_id == recovery.action_id
    assert first.items[1].batch_id == recovery_request.batch_id
    assert first.items[1].persisted_samples == 2
    assert first.items[1].cancel_receipt_id == recovery.cancel_receipt_id
    assert [item.recovery_status for item in second.items] == ["terminal"]
    assert second.assessed_at == first.assessed_at
    assert not second.has_more

    open_page = await store.list_sandbox_retry_dispatches(
        workspace_root=workspace,
        assessed_at=ASSESSED,
        state_filter="open",
    )
    terminal_page = await store.list_sandbox_retry_dispatches(
        workspace_root=workspace,
        assessed_at=ASSESSED,
        state_filter="terminal",
    )
    assert len(open_page.items) == 2
    assert len(terminal_page.items) == 1

    with sqlite3.connect(store.db_path) as db:
        indexes = {
            row[1]
            for row in db.execute(
                "PRAGMA index_list('harness_sandbox_retry_dispatches')"
            ).fetchall()
        }
        plan = " ".join(
            str(row[3])
            for row in db.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM harness_sandbox_retry_dispatches
                WHERE workspace_root = ?
                ORDER BY updated_at DESC, retry_action_id ASC
                LIMIT ?
                """,
                (str(workspace.resolve()), 2),
            ).fetchall()
        )
    assert "idx_harness_sandbox_retry_dispatch_catalog" in indexes
    assert "idx_harness_sandbox_retry_dispatch_state_catalog" in indexes
    assert "idx_harness_sandbox_retry_dispatch_catalog" in plan


@pytest.mark.asyncio
async def test_retry_catalog_is_workspace_scoped_and_cursor_is_bound(
    tmp_path: Path,
) -> None:
    first_workspace = _workspace(tmp_path, "first")
    second_workspace = _workspace(tmp_path, "second")
    store = HarnessStore(tmp_path / "harness.db")
    await _create_dispatch(
        store,
        first_workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=5,
        lease_seconds=300,
    )
    await _create_dispatch(
        store,
        first_workspace,
        source_token="3",
        retry_token="b",
        ticket_token="4",
        minute=6,
        lease_seconds=300,
    )
    await _create_dispatch(
        store,
        second_workspace,
        source_token="5",
        retry_token="c",
        ticket_token="6",
        minute=6,
        lease_seconds=300,
    )
    page = await store.list_sandbox_retry_dispatches(
        workspace_root=first_workspace,
        assessed_at=ASSESSED,
        limit=1,
    )
    assert page.next_cursor

    with pytest.raises(ValueError, match="工作区"):
        await store.list_sandbox_retry_dispatches(
            workspace_root=second_workspace,
            assessed_at=ASSESSED,
            limit=1,
            cursor=page.next_cursor,
        )
    with pytest.raises(ValueError, match="评估时间"):
        await store.list_sandbox_retry_dispatches(
            workspace_root=first_workspace,
            assessed_at="2026-07-23T01:06:31+00:00",
            limit=1,
            cursor=page.next_cursor,
        )
    with pytest.raises(ValueError, match="state filter"):
        await store.list_sandbox_retry_dispatches(
            workspace_root=first_workspace,
            assessed_at=ASSESSED,
            state_filter="open",
            limit=1,
            cursor=page.next_cursor,
        )
    replacement = "A" if page.next_cursor[-1] != "A" else "B"
    with pytest.raises(ValueError, match="cursor"):
        await store.list_sandbox_retry_dispatches(
            workspace_root=first_workspace,
            assessed_at=ASSESSED,
            limit=1,
            cursor=f"{page.next_cursor[:-1]}{replacement}",
        )


@pytest.mark.asyncio
async def test_retry_catalog_fails_closed_on_non_contiguous_h5a(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    request, _retry, _dispatch = await _create_dispatch(
        store,
        workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=6,
        lease_seconds=300,
        persisted_samples=1,
    )
    await store.record_eval_result(
        workspace_root=workspace,
        batch_id=request.batch_id,
        sample_index=4,
        result=HarnessEvalSuiteResult(
            suite_id=request.suite_id,
            title="Non-contiguous fixture",
            suite_path="catalog",
            status=EvalRunStatus.PASSED,
        ),
        created_at="2026-07-23T01:06:05+00:00",
    )

    with pytest.raises(HarnessStoreError, match="H5a"):
        await HarnessStore(store.db_path).list_sandbox_retry_dispatches(
            workspace_root=workspace,
            assessed_at=ASSESSED,
        )


@pytest.mark.asyncio
async def test_retry_catalog_fails_closed_on_terminal_ticket_drift(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    _request, _retry, dispatch = await _create_dispatch(
        store,
        workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=6,
        lease_seconds=300,
        terminal="completed",
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_admission_tickets
            SET state = 'failed', terminal_code = 'tampered'
            WHERE workspace_root = ? AND ticket_id = ?
            """,
            (str(workspace.resolve()), dispatch.ticket_id),
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="终态不一致"):
        await HarnessStore(store.db_path).list_sandbox_retry_dispatches(
            workspace_root=workspace,
            assessed_at=ASSESSED,
        )


@pytest.mark.asyncio
async def test_retry_catalog_tool_is_read_only_and_renders_recovery_facts(
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
        lease_seconds=2,
    )
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=store,
    )
    tool = next(
        item
        for item in create_harness_tools(service)
        if item.name == "harness_eval_sandbox_retries"
    )

    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert set(tool.parameters_schema["properties"]) == {
        "state",
        "limit",
        "cursor",
        "assessed_at",
    }
    assert "参数无效" in await tool.execute(limit=True)
    rendered = await tool.execute(
        state="open",
        limit=20,
        assessed_at=ASSESSED,
    )
    assert "租约已过期，需要恢复" in rendered
    assert "Retry action：`hsar_" in rendered
    assert "Cancel receipt：`hsacr_" in rendered
    assert "当前目录只读" in rendered
    assert "execution_authority" not in rendered
    assert "owner" not in rendered
