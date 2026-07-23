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
    render_sandbox_retry_prune_execution_receipt,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.orchestrator.engine import AgentRuntimeMode
from naumi_agent.safety.permissions import PermissionMode
from tests.unit.test_harness_sandbox_retry_catalog import (
    _create_dispatch,
    _workspace,
)
from tests.unit.test_harness_sandbox_retry_prune import (
    ASSESSED,
    _arguments,
    _authorized_service,
    _preview_one,
)
from tests.unit.test_harness_surfaces import _engine

RUN_ID = "run-prune-execution"


def _execution_arguments(
    authorization,
    *,
    action_id: str,
) -> dict[str, object]:
    return {
        "action_id": action_id,
        "authorization_action_id": authorization.action_id,
        "authorization_receipt_id": authorization.receipt_id,
        "authorization_receipt_sha256": authorization.receipt_sha256,
        "candidate_id": authorization.candidate_id,
        "candidate_sha256": authorization.candidate_sha256,
        "retry_action_id": authorization.retry_action_id,
        "dispatch_id": authorization.dispatch_id,
        "protection_refs_sha256": authorization.protection_refs_sha256,
        "reason": "执行已授权的终态 retry 原子清理",
        "run_id": RUN_ID,
    }


async def _execution_service(
    tmp_path: Path,
    workspace: Path,
    store: HarnessStore,
    arguments: dict[str, object],
    *,
    token: str,
) -> HarnessService:
    permission_store = PermissionDecisionReceiptStore(
        tmp_path / f"execution-permission-{token}.db"
    )
    parent = await permission_store.issue(
        request_id=f"execution-request-{token}",
        session_id="session-prune-execution",
        run_id=RUN_ID,
        call_id=f"execution-call-{token}",
        agent_name="main",
        tool_name="harness_eval_sandbox_retry_prune_execute",
        tool_family="harness_eval_governance",
        arguments=arguments,
        outcome=PermissionDecisionOutcome.BYPASS_ENABLED,
        actor=PermissionDecisionActor.USER,
        source=PermissionDecisionSource.BYPASS,
        permission_mode=PermissionMode.BYPASS,
        risk_level="high",
        decided_at=ASSESSED,
    )
    return HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / f"execution-trust-{token}.db"),
        store=HarnessStore(store.db_path),
        authorization_receipt_provider=lambda: parent,
    )


async def _authorized_fixture(
    tmp_path: Path,
    *,
    terminal: str = "completed",
    persisted_samples: int = 1,
):
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    request, retry, dispatch = await _create_dispatch(
        store,
        workspace,
        source_token="1",
        retry_token="a",
        ticket_token="2",
        minute=4,
        lease_seconds=300,
        terminal=terminal,
        persisted_samples=persisted_samples,
    )
    preview, candidate = await _preview_one(tmp_path, workspace, store)
    authorization_arguments = _arguments(
        preview,
        candidate,
        action_id=f"hsrpa_{'1' * 24}",
    )
    authorization_service = await _authorized_service(
        tmp_path,
        workspace,
        store,
        authorization_arguments,
        token="authorization",
    )
    authorization = await authorization_service.authorize_sandbox_retry_prune(
        **authorization_arguments
    )
    assert authorization.decision == "accepted"
    return workspace, store, request, retry, dispatch, authorization


def _count(
    path: Path,
    table: str,
    *,
    workspace: Path,
    where: str = "",
    values: tuple[object, ...] = (),
) -> int:
    suffix = f" AND {where}" if where else ""
    with sqlite3.connect(path) as db:
        row = db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE workspace_root = ?{suffix}",
            (str(workspace.resolve()), *values),
        ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.asyncio
async def test_prune_execution_atomically_deletes_only_the_safe_set(
    tmp_path: Path,
) -> None:
    (
        workspace,
        store,
        request,
        retry,
        dispatch,
        authorization,
    ) = await _authorized_fixture(tmp_path)
    arguments = _execution_arguments(
        authorization,
        action_id=f"hsrpe_{'2' * 24}",
    )
    service = await _execution_service(
        tmp_path,
        workspace,
        store,
        arguments,
        token="success",
    )

    receipt = await service.execute_sandbox_retry_prune(**arguments)
    repeated = await service.execute_sandbox_retry_prune(**arguments)

    assert receipt == repeated
    assert receipt.decision == "executed"
    assert receipt.code == "sandbox_retry_prune_executed"
    assert receipt.deleted_dispatch_count == 1
    assert receipt.deleted_retry_attempt_count == 1
    assert receipt.deleted_ticket_count == 1
    assert receipt.retained_reference_count == 4
    assert receipt.retry_receipt_id == retry.receipt_id
    assert receipt.retry_receipt_sha256 == retry.receipt_sha256
    assert receipt.cancel_receipt_id == retry.cancel_receipt_id
    assert (
        await store.get_sandbox_retry_dispatch(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
        )
        is None
    )
    assert (
        await store.get_sandbox_admission_retry(
            workspace_root=workspace,
            action_id=retry.action_id,
        )
        is None
    )
    assert _count(
        store.db_path,
        "harness_sandbox_admission_tickets",
        workspace=workspace,
        where="ticket_id = ?",
        values=(dispatch.ticket_id,),
    ) == 0
    assert _count(
        store.db_path,
        "harness_sandbox_eval_requests",
        workspace=workspace,
        where="request_sha256 = ?",
        values=(request.request_sha256,),
    ) == 1
    assert _count(
        store.db_path,
        "harness_sandbox_admission_cancel_attempts",
        workspace=workspace,
    ) == 1
    assert _count(
        store.db_path,
        "harness_eval_results",
        workspace=workspace,
        where="batch_id = ?",
        values=(request.batch_id,),
    ) == 1
    assert _count(
        store.db_path,
        "harness_sandbox_retry_prune_attempts",
        workspace=workspace,
        where="action_id = ?",
        values=(authorization.action_id,),
    ) == 1
    assert (
        await store.get_sandbox_retry_prune_execution_receipt(
            workspace_root=workspace,
            action_id=receipt.action_id,
        )
        == receipt
    )
    replayed_retry = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'b' * 24}",
        cancel_receipt_id=retry.cancel_receipt_id,
        cancel_receipt_sha256=retry.cancel_receipt_sha256,
        actor_id="prune-replay-test",
        reason="验证 prune 后不能重新消费 cancel receipt",
        authority_token="c" * 32,
        now="2026-07-24T02:00:00+00:00",
    )
    assert replayed_retry.decision == "rejected"
    assert replayed_retry.code == "sandbox_batch_retry_cancel_receipt_consumed"
    rendered = render_sandbox_retry_prune_execution_receipt(receipt)
    assert "原子事务已提交" in rendered
    assert str(workspace.resolve()) not in rendered
    tool = next(
        item
        for item in create_harness_tools(service)
        if item.name == "harness_eval_sandbox_retry_prune_execute"
    )
    assert not tool.metadata.read_only
    assert tool.metadata.destructive
    assert tool.metadata.requires_confirmation
    assert tool.metadata.requires_persistent_authorization
    assert tool.metadata.concurrency_safe


@pytest.mark.asyncio
async def test_prune_execution_rejects_drift_without_partial_delete(
    tmp_path: Path,
) -> None:
    workspace, store, _request, retry, _dispatch, authorization = (
        await _authorized_fixture(tmp_path, terminal="failed")
    )
    arguments = _execution_arguments(
        authorization,
        action_id=f"hsrpe_{'3' * 24}",
    )
    service = await _execution_service(
        tmp_path,
        workspace,
        store,
        arguments,
        token="drift",
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
                retry.action_id,
            ),
        )
        db.commit()

    receipt = await service.execute_sandbox_retry_prune(**arguments)

    assert receipt.decision == "rejected"
    assert receipt.code == "sandbox_retry_prune_candidate_drift"
    assert receipt.deleted_dispatch_count == 0
    assert receipt.deleted_retry_attempt_count == 0
    assert receipt.deleted_ticket_count == 0
    assert (
        await store.get_sandbox_retry_dispatch(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
        )
        is not None
    )
    assert (
        await store.get_sandbox_admission_retry(
            workspace_root=workspace,
            action_id=retry.action_id,
        )
        is not None
    )


@pytest.mark.asyncio
async def test_prune_execution_preserves_a_ticket_with_a_later_reference(
    tmp_path: Path,
) -> None:
    workspace, store, _request, retry, dispatch, authorization = (
        await _authorized_fixture(tmp_path, persisted_samples=0)
    )
    cancel, _ticket = await store.cancel_sandbox_admission(
        workspace_root=workspace,
        action_id=f"hsac_{'3' * 24}",
        ticket_id=dispatch.ticket_id,
        authority_key=dispatch.execution_authority_key,
        epoch=dispatch.ticket_epoch,
        expected_state="active",
        actor_id="shared-reference-test",
        reason="构造终态 ticket 的后续审计引用",
        now="2026-07-23T01:06:00+00:00",
    )
    assert cancel.decision == "rejected"
    arguments = _execution_arguments(
        authorization,
        action_id=f"hsrpe_{'4' * 24}",
    )
    service = await _execution_service(
        tmp_path,
        workspace,
        store,
        arguments,
        token="shared-ticket",
    )

    receipt = await service.execute_sandbox_retry_prune(**arguments)

    assert receipt.decision == "executed"
    assert receipt.deleted_ticket_count == 0
    assert receipt.retained_reference_count == 4
    assert _count(
        store.db_path,
        "harness_sandbox_admission_tickets",
        workspace=workspace,
        where="ticket_id = ?",
        values=(dispatch.ticket_id,),
    ) == 1
    assert (
        await store.get_sandbox_admission_retry(
            workspace_root=workspace,
            action_id=retry.action_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_prune_execution_rolls_back_if_any_delete_fails(
    tmp_path: Path,
) -> None:
    workspace, store, _request, retry, dispatch, authorization = (
        await _authorized_fixture(tmp_path)
    )
    arguments = _execution_arguments(
        authorization,
        action_id=f"hsrpe_{'5' * 24}",
    )
    service = await _execution_service(
        tmp_path,
        workspace,
        store,
        arguments,
        token="rollback",
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            """
            CREATE TRIGGER reject_retry_delete
            BEFORE DELETE ON harness_sandbox_admission_retry_attempts
            BEGIN
                SELECT RAISE(ABORT, 'injected retry delete failure');
            END
            """
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="无法执行"):
        await service.execute_sandbox_retry_prune(**arguments)

    assert (
        await store.get_sandbox_retry_dispatch(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
        )
        is not None
    )
    assert (
        await store.get_sandbox_admission_retry(
            workspace_root=workspace,
            action_id=retry.action_id,
        )
        is not None
    )
    assert _count(
        store.db_path,
        "harness_sandbox_admission_tickets",
        workspace=workspace,
        where="ticket_id = ?",
        values=(dispatch.ticket_id,),
    ) == 1
    assert _count(
        store.db_path,
        "harness_sandbox_retry_prune_executions",
        workspace=workspace,
    ) == 0


@pytest.mark.asyncio
async def test_prune_execution_consumes_once_and_detects_receipt_tampering(
    tmp_path: Path,
) -> None:
    workspace, store, _request, _retry, _dispatch, authorization = (
        await _authorized_fixture(tmp_path)
    )
    first_arguments = _execution_arguments(
        authorization,
        action_id=f"hsrpe_{'6' * 24}",
    )
    second_arguments = _execution_arguments(
        authorization,
        action_id=f"hsrpe_{'7' * 24}",
    )
    first_service, second_service = await asyncio.gather(
        _execution_service(
            tmp_path,
            workspace,
            store,
            first_arguments,
            token="concurrent-first",
        ),
        _execution_service(
            tmp_path,
            workspace,
            store,
            second_arguments,
            token="concurrent-second",
        ),
    )

    receipts = await asyncio.gather(
        first_service.execute_sandbox_retry_prune(**first_arguments),
        second_service.execute_sandbox_retry_prune(**second_arguments),
    )

    assert sorted(receipt.decision for receipt in receipts) == [
        "executed",
        "rejected",
    ]
    assert {receipt.code for receipt in receipts} == {
        "sandbox_retry_prune_executed",
        "sandbox_retry_prune_already_executed",
    }
    executed = next(item for item in receipts if item.decision == "executed")
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_retry_prune_executions
            SET receipt_sha256 = ?
            WHERE workspace_root = ? AND action_id = ?
            """,
            ("0" * 64, str(workspace.resolve()), executed.action_id),
        )
        db.commit()
    with pytest.raises(HarnessStoreError, match="摘要不一致"):
        await store.get_sandbox_retry_prune_execution_receipt(
            workspace_root=workspace,
            action_id=executed.action_id,
        )


@pytest.mark.asyncio
async def test_shared_slash_executes_authorized_prune_in_bypass(
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
        authorize_command = (
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
            '--reason "签发终态 retry 清理授权"'
        )
        authorized = await execute_slash_command(engine, authorize_command)
        assert "Sandbox retry prune 回执：已签发" in authorized
        with sqlite3.connect(store.db_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """
                SELECT action_id FROM harness_sandbox_retry_prune_attempts
                WHERE workspace_root = ? AND decision = 'accepted'
                """,
                (str(engine.workspace_root.resolve()),),
            ).fetchone()
        assert row is not None
        authorization = await store.get_sandbox_retry_prune_receipt(
            workspace_root=engine.workspace_root,
            action_id=str(row["action_id"]),
        )
        assert authorization is not None
        engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
        execute_command = (
            "/harness eval sandbox retry-prune-execute "
            f"{authorization.action_id} "
            f"--receipt {authorization.receipt_id} "
            f"--receipt-sha256 {authorization.receipt_sha256} "
            f"--candidate {authorization.candidate_id} "
            f"--candidate-sha256 {authorization.candidate_sha256} "
            f"--retry-action {authorization.retry_action_id} "
            f"--dispatch {authorization.dispatch_id} "
            f"--refs-sha256 {authorization.protection_refs_sha256} "
            '--reason "执行终态 retry 原子清理"'
        )

        rendered = await execute_slash_command(engine, execute_command)

        assert "Sandbox retry prune 执行回执：已完成" in rendered
        assert "原子事务已提交" in rendered
    finally:
        await engine.shutdown()
