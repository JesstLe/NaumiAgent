from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.harness.sandbox_retry_prune import (
    render_sandbox_retry_prune_receipt,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.safety.permissions import PermissionMode
from tests.unit.test_harness_sandbox_retry_catalog import (
    _create_dispatch,
    _workspace,
)
from tests.unit.test_harness_surfaces import _engine

ASSESSED = "2026-07-24T01:06:30+00:00"
RUN_ID = "run-prune"


def _retry_facts(path: Path) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    with sqlite3.connect(path) as db:
        tables = tuple(
            row[0]
            for row in db.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name LIKE 'harness_%'
                  AND name <> 'harness_sandbox_retry_prune_attempts'
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


def _arguments(preview, candidate, *, action_id: str) -> dict[str, object]:
    return {
        "action_id": action_id,
        "preview_id": preview.preview_id,
        "preview_sha256": preview.preview_sha256,
        "candidate_id": candidate.candidate_id,
        "candidate_sha256": candidate.candidate_sha256,
        "retry_action_id": candidate.retry_action_id,
        "dispatch_id": candidate.dispatch_id,
        "dispatch_epoch": candidate.dispatch_epoch,
        "dispatch_request_sha256": candidate.dispatch_request_sha256,
        "dispatch_updated_at": candidate.updated_at,
        "protection_refs_sha256": candidate.protection_refs_sha256,
        "preview_assessed_at": preview.assessed_at,
        "retention_days": preview.policy.retention_days,
        "limit": preview.policy.limit,
        "scan_limit": preview.policy.scan_limit,
        "reason": "清理超过保留期的终态 retry cohort",
        "run_id": RUN_ID,
    }


async def _authorized_service(
    tmp_path: Path,
    workspace: Path,
    store: HarnessStore,
    arguments: dict[str, object],
    *,
    token: str,
) -> HarnessService:
    permission_store = PermissionDecisionReceiptStore(
        tmp_path / f"permission-{token}.db"
    )
    parent = await permission_store.issue(
        request_id=f"request-{token}",
        session_id="session-prune",
        run_id=RUN_ID,
        call_id=f"call-{token}",
        agent_name="main",
        tool_name="harness_eval_sandbox_retry_prune_authorize",
        tool_family="harness_eval_governance",
        arguments=arguments,
        outcome=PermissionDecisionOutcome.BYPASS_ENABLED,
        actor=PermissionDecisionActor.USER,
        source=PermissionDecisionSource.BYPASS,
        permission_mode=PermissionMode.BYPASS,
        risk_level="medium",
        decided_at=ASSESSED,
    )
    return HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / f"trust-{token}.db"),
        store=HarnessStore(store.db_path),
        authorization_receipt_provider=lambda: parent,
    )


async def _preview_one(
    tmp_path: Path,
    workspace: Path,
    store: HarnessStore,
):
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "preview-trust.db"),
        store=store,
    )
    preview = await service.sandbox_retry_retention_preview(
        retention_days=1,
        limit=1,
        scan_limit=1,
        assessed_at=ASSESSED,
    )
    assert len(preview.candidates) == 1
    return preview, preview.candidates[0]


@pytest.mark.asyncio
async def test_prune_authorization_is_idempotent_auditable_and_never_deletes(
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
        persisted_samples=1,
    )
    preview, candidate = await _preview_one(tmp_path, workspace, store)
    arguments = _arguments(
        preview,
        candidate,
        action_id=f"hsrpa_{'1' * 24}",
    )
    service = await _authorized_service(
        tmp_path,
        workspace,
        store,
        arguments,
        token="1",
    )
    before = _retry_facts(store.db_path)

    first = await service.authorize_sandbox_retry_prune(**arguments)
    repeated = await service.authorize_sandbox_retry_prune(**arguments)

    assert first == repeated
    assert first.decision == "accepted"
    assert first.code == "sandbox_retry_prune_authorized"
    assert _retry_facts(store.db_path) == before
    assert (
        await store.get_sandbox_retry_prune_receipt(
            workspace_root=workspace,
            action_id=first.action_id,
        )
        == first
    )
    rendered = render_sandbox_retry_prune_receipt(first)
    assert "本轮没有删除任何记录" in rendered
    assert str(workspace.resolve()) not in rendered
    tool = next(
        item
        for item in create_harness_tools(service)
        if item.name == "harness_eval_sandbox_retry_prune_authorize"
    )
    assert not tool.metadata.read_only
    assert not tool.metadata.destructive
    assert not tool.metadata.requires_confirmation
    assert tool.metadata.requires_persistent_authorization
    assert tool.metadata.concurrency_safe
    assert "本轮没有删除任何记录" in await tool.execute(**arguments)


@pytest.mark.asyncio
async def test_prune_authorization_rejects_preview_drift_durably(
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
        terminal="failed",
    )
    preview, candidate = await _preview_one(tmp_path, workspace, store)
    arguments = _arguments(
        preview,
        candidate,
        action_id=f"hsrpa_{'2' * 24}",
    )
    arguments["preview_sha256"] = "f" * 64
    service = await _authorized_service(
        tmp_path,
        workspace,
        store,
        arguments,
        token="2",
    )

    receipt = await service.authorize_sandbox_retry_prune(**arguments)

    assert receipt.decision == "rejected"
    assert receipt.code == "sandbox_retry_prune_preview_mismatch"
    assert (
        await store.get_sandbox_retry_prune_receipt(
            workspace_root=workspace,
            action_id=receipt.action_id,
        )
        == receipt
    )


@pytest.mark.asyncio
async def test_prune_authorization_revalidates_current_dispatch_fence(
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
        terminal="cancelled",
    )
    preview, candidate = await _preview_one(tmp_path, workspace, store)
    arguments = _arguments(
        preview,
        candidate,
        action_id=f"hsrpa_{'3' * 24}",
    )
    service = await _authorized_service(
        tmp_path,
        workspace,
        store,
        arguments,
        token="3",
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_retry_dispatches
            SET updated_at = ?
            WHERE workspace_root = ? AND retry_action_id = ?
            """,
            (
                "2026-07-23T01:04:06+00:00",
                str(workspace.resolve()),
                candidate.retry_action_id,
            ),
        )
        db.commit()

    receipt_provider = service._authorization_receipt_provider  # noqa: SLF001
    assert receipt_provider is not None
    parent = receipt_provider()
    assert parent is not None
    receipt = await store.authorize_sandbox_retry_prune(
        workspace_root=workspace,
        action_id=str(arguments["action_id"]),
        preview_id=str(arguments["preview_id"]),
        preview_sha256=str(arguments["preview_sha256"]),
        candidate_id=str(arguments["candidate_id"]),
        candidate_sha256=str(arguments["candidate_sha256"]),
        retry_action_id=str(arguments["retry_action_id"]),
        dispatch_id=str(arguments["dispatch_id"]),
        dispatch_epoch=int(arguments["dispatch_epoch"]),
        dispatch_request_sha256=str(arguments["dispatch_request_sha256"]),
        dispatch_updated_at=str(arguments["dispatch_updated_at"]),
        protection_refs_sha256=str(arguments["protection_refs_sha256"]),
        parent_permission_receipt_id=parent.receipt_id,
        parent_permission_receipt_sha256=parent.receipt_sha256,
        actor_id="user:main",
        reason=str(arguments["reason"]),
        created_at=ASSESSED,
    )

    assert receipt.decision == "rejected"
    assert receipt.code == "sandbox_retry_prune_candidate_drift"


@pytest.mark.asyncio
async def test_prune_authorization_never_crosses_workspace_scope(
    tmp_path: Path,
) -> None:
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
            minute=4,
            lease_seconds=300,
            terminal="completed",
        )
    second_preview, second_candidate = await _preview_one(
        tmp_path,
        second,
        store,
    )
    arguments = _arguments(
        second_preview,
        second_candidate,
        action_id=f"hsrpa_{'6' * 24}",
    )
    first_service = await _authorized_service(
        tmp_path,
        first,
        store,
        arguments,
        token="6",
    )

    receipt = await first_service.authorize_sandbox_retry_prune(**arguments)

    assert receipt.decision == "rejected"
    assert receipt.code == "sandbox_retry_prune_preview_mismatch"
    assert (
        await store.get_sandbox_retry_prune_receipt(
            workspace_root=second,
            action_id=receipt.action_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_concurrent_prune_intents_accept_exactly_once_and_detect_tampering(
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
    preview, candidate = await _preview_one(tmp_path, workspace, store)
    first_args = _arguments(
        preview,
        candidate,
        action_id=f"hsrpa_{'4' * 24}",
    )
    second_args = _arguments(
        preview,
        candidate,
        action_id=f"hsrpa_{'5' * 24}",
    )
    first_service, second_service = await asyncio.gather(
        _authorized_service(
            tmp_path,
            workspace,
            store,
            first_args,
            token="4",
        ),
        _authorized_service(
            tmp_path,
            workspace,
            store,
            second_args,
            token="5",
        ),
    )

    receipts = await asyncio.gather(
        first_service.authorize_sandbox_retry_prune(**first_args),
        second_service.authorize_sandbox_retry_prune(**second_args),
    )

    assert sorted(receipt.decision for receipt in receipts) == [
        "accepted",
        "rejected",
    ]
    assert {receipt.code for receipt in receipts} == {
        "sandbox_retry_prune_authorized",
        "sandbox_retry_prune_already_authorized",
    }
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_retry_prune_attempts
            SET receipt_sha256 = ?
            WHERE workspace_root = ? AND action_id = ?
            """,
            ("0" * 64, str(workspace.resolve()), receipts[0].action_id),
        )
        db.commit()
    with pytest.raises(HarnessStoreError, match="摘要不一致"):
        await store.get_sandbox_retry_prune_receipt(
            workspace_root=workspace,
            action_id=receipts[0].action_id,
        )


@pytest.mark.asyncio
async def test_shared_slash_authorizes_real_candidate_without_deleting(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        store = engine.harness_service.store
        assert store is not None
        await _create_dispatch(
            store,
            engine.workspace_root,
            source_token="7",
            retry_token="b",
            ticket_token="8",
            minute=4,
            lease_seconds=300,
            terminal="completed",
        )
        preview = await engine.harness_service.sandbox_retry_retention_preview(
            retention_days=1,
            limit=1,
            scan_limit=1,
            assessed_at=ASSESSED,
        )
        candidate = preview.candidates[0]
        before = _retry_facts(store.db_path)
        command = (
            "/harness eval sandbox retry-prune-authorize "
            f"{candidate.candidate_id} "
            f"--preview {preview.preview_id} "
            f"--preview-sha256 {preview.preview_sha256} "
            f"--candidate-sha256 {candidate.candidate_sha256} "
            f"--retry-action {candidate.retry_action_id} "
            f"--dispatch {candidate.dispatch_id} "
            f"--epoch {candidate.dispatch_epoch} "
            f"--dispatch-sha256 {candidate.dispatch_request_sha256} "
            f"--updated-at {candidate.updated_at} "
            f"--refs-sha256 {candidate.protection_refs_sha256} "
            f"--preview-assessed-at {preview.assessed_at} "
            f"--retention-days {preview.policy.retention_days} "
            f"--limit {preview.policy.limit} "
            f"--scan-limit {preview.policy.scan_limit} "
            '--reason "清理超过保留期的终态 retry cohort"'
        )

        rendered = await execute_slash_command(engine, command)

        assert "Sandbox retry prune 回执：已签发" in rendered
        assert "本轮没有删除任何记录" in rendered
        assert _retry_facts(store.db_path) == before
    finally:
        await engine.shutdown()
