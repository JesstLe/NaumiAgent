from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.harness.sandbox_retry_detail import (
    HarnessSandboxRetryDetailSnapshot,
    render_sandbox_retry_detail,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore
from tests.unit.test_harness_sandbox_retry_catalog import (
    ASSESSED,
    _create_dispatch,
    _workspace,
)


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
async def test_retry_detail_reads_exact_authority_chain_without_mutation(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    request, retry, dispatch = await _create_dispatch(
        store,
        workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=6,
        lease_seconds=2,
        persisted_samples=2,
    )
    before = _database_facts(store.db_path)

    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(store.db_path),
    )
    snapshot = await service.sandbox_retry_detail(
        retry_action_id=retry.action_id,
        dispatch_id=dispatch.dispatch_id,
        assessed_at=ASSESSED,
    )

    assert _database_facts(store.db_path) == before
    assert snapshot.status == "ok"
    assert snapshot.detail is not None
    assert snapshot.detail.recovery_status == "recovery_required"
    assert snapshot.detail.request_id == request.request_id
    assert snapshot.detail.persisted_samples == 2
    assert [sample.sample_index for sample in snapshot.detail.samples] == [0, 1]
    assert snapshot.detail.can_resume
    assert snapshot.detail.resume_command.startswith(
        f"/harness eval sandbox resume {retry.action_id}"
    )
    assert {ref.kind for ref in snapshot.detail.protection_refs} == {
        "dispatch",
        "retry_receipt",
        "cancel_receipt",
        "request_manifest",
        "ticket",
        "h5a_sample",
    }
    payload = snapshot.model_dump_json()
    assert str(workspace.resolve()) not in payload
    assert "owner_id" not in payload
    assert "execution_authority" not in payload
    assert "actor_id" not in payload
    assert "catalog-test" not in payload
    assert "构造 catalog" not in payload


@pytest.mark.asyncio
async def test_retry_detail_is_workspace_and_identity_scoped(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "first")
    other = _workspace(tmp_path, "second")
    store = HarnessStore(tmp_path / "harness.db")
    _request, retry, dispatch = await _create_dispatch(
        store,
        workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=6,
        lease_seconds=300,
        claim=False,
    )

    wrong_workspace = HarnessService(
        workspace_root=other,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=store,
    )
    missing = await wrong_workspace.sandbox_retry_detail(
        retry_action_id=retry.action_id,
        dispatch_id=dispatch.dispatch_id,
        assessed_at=ASSESSED,
    )
    mismatch = await HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=store,
    ).sandbox_retry_detail(
        retry_action_id=retry.action_id,
        dispatch_id=f"hsard_{'f' * 24}",
        assessed_at=ASSESSED,
    )

    assert missing.status == "not_found"
    assert mismatch.status == "not_found"
    assert missing.detail is None
    assert mismatch.detail is None


@pytest.mark.asyncio
async def test_retry_detail_projects_pending_and_terminal_states(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    _pending_request, pending_retry, pending_dispatch = await _create_dispatch(
        store,
        workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=5,
        lease_seconds=300,
        claim=False,
    )
    _terminal_request, terminal_retry, terminal_dispatch = await _create_dispatch(
        store,
        workspace,
        source_token="3",
        retry_token="b",
        ticket_token="4",
        minute=6,
        lease_seconds=300,
        terminal="completed",
    )
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(store.db_path),
    )

    pending = await service.sandbox_retry_detail(
        retry_action_id=pending_retry.action_id,
        dispatch_id=pending_dispatch.dispatch_id,
        assessed_at=ASSESSED,
    )
    terminal = await service.sandbox_retry_detail(
        retry_action_id=terminal_retry.action_id,
        dispatch_id=terminal_dispatch.dispatch_id,
        assessed_at=ASSESSED,
    )

    assert pending.detail is not None
    assert pending.detail.recovery_status == "pending"
    assert pending.detail.ticket is None
    assert pending.detail.can_resume
    assert terminal.detail is not None
    assert terminal.detail.recovery_status == "terminal"
    assert terminal.detail.ticket is not None
    assert terminal.detail.ticket.state == "completed"
    assert not terminal.detail.can_resume
    assert terminal.detail.resume_command == ""


@pytest.mark.asyncio
async def test_retry_detail_does_not_initialize_an_empty_database(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    database = tmp_path / "empty.db"
    database.touch()
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(database),
    )

    snapshot = await service.sandbox_retry_detail(
        retry_action_id=f"hsar_{'a' * 24}",
        dispatch_id=f"hsard_{'b' * 24}",
        assessed_at=ASSESSED,
    )

    assert snapshot.status == "not_found"
    assert database.read_bytes() == b""


@pytest.mark.asyncio
async def test_retry_detail_fails_closed_on_cancel_receipt_chain_drift(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    _request, retry, dispatch = await _create_dispatch(
        store,
        workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=6,
        lease_seconds=300,
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_admission_cancel_attempts
            SET receipt_sha256 = ?
            WHERE workspace_root = ? AND receipt_id = ?
            """,
            ("f" * 64, str(workspace.resolve()), retry.cancel_receipt_id),
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="摘要"):
        await HarnessStore(store.db_path).get_sandbox_retry_detail(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
            dispatch_id=dispatch.dispatch_id,
            assessed_at=ASSESSED,
        )


@pytest.mark.asyncio
async def test_retry_detail_accepts_durably_expired_generation(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    _request, retry, dispatch = await _create_dispatch(
        store,
        workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=6,
        lease_seconds=2,
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_admission_tickets
            SET state = 'expired',
                terminal_code = 'sandbox_batch_admission_lease_expired'
            WHERE workspace_root = ? AND ticket_id = ?
            """,
            (str(workspace.resolve()), dispatch.ticket_id),
        )
        db.commit()

    snapshot = await HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(store.db_path),
    ).sandbox_retry_detail(
        retry_action_id=retry.action_id,
        dispatch_id=dispatch.dispatch_id,
        assessed_at=ASSESSED,
    )

    assert snapshot.detail is not None
    assert snapshot.detail.recovery_status == "recovery_required"
    assert snapshot.detail.ticket is not None
    assert snapshot.detail.ticket.state == "expired"
    assert snapshot.detail.ticket.terminal_code == (
        "sandbox_batch_admission_lease_expired"
    )


@pytest.mark.asyncio
async def test_retry_detail_tool_is_read_only_and_tamper_evident(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    _request, retry, dispatch = await _create_dispatch(
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
        if item.name == "harness_eval_sandbox_retry_detail"
    )

    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert set(tool.parameters_schema["required"]) == {
        "retry_action_id",
        "dispatch_id",
    }
    assert "参数无效" in await tool.execute(retry_action_id="bad", dispatch_id="bad")
    rendered = await tool.execute(
        retry_action_id=retry.action_id,
        dispatch_id=dispatch.dispatch_id,
        assessed_at=ASSESSED,
    )
    assert "Sandbox retry dispatch 详情" in rendered
    assert "Retention 保护集合（只读）" in rendered
    assert "显式恢复命令" in rendered
    assert str(workspace.resolve()) not in rendered
    assert "owner" not in rendered

    snapshot = await service.sandbox_retry_detail(
        retry_action_id=retry.action_id,
        dispatch_id=dispatch.dispatch_id,
        assessed_at=ASSESSED,
    )
    tampered = snapshot.model_dump(mode="json")
    tampered["detail"]["resume_command"] = "/harness eval sandbox resume tampered"
    with pytest.raises(ValidationError, match="resume"):
        HarnessSandboxRetryDetailSnapshot.model_validate(tampered)

    assert "Snapshot：" in render_sandbox_retry_detail(snapshot)
