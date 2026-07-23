from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.harness.models import (
    HarnessCheckSpec,
    HarnessEvalSpec,
    HarnessProfile,
)
from naumi_agent.harness.sandbox_batch import (
    HarnessSandboxBatchAdmission,
    HarnessSandboxBatchError,
)
from naumi_agent.harness.sandbox_request import (
    HarnessSandboxEvalRequest,
    HarnessSandboxEvalRequestBuilder,
    HarnessSandboxEvalRequestError,
    validate_request_checks,
)
from naumi_agent.harness.store import (
    HarnessSandboxAdmissionCapacityError,
    HarnessSandboxRetryDispatchFenceError,
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoreError,
)


def _git(workspace: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
    ).stdout


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "harness@example.invalid")
    _git(workspace, "config", "user.name", "Harness Test")
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(workspace, "add", "module.py")
    _git(workspace, "commit", "-qm", "initial")
    return workspace


def _profile(
    *,
    check: HarnessCheckSpec | None = None,
    max_duration_seconds: int = 600,
) -> HarnessProfile:
    return HarnessProfile(
        schema_version=1,
        checks=(
            check
            or HarnessCheckSpec(
                id="unit",
                label="定向单测",
                argv=("uv", "run", "pytest", "-q", "tests/unit/test_one.py"),
                timeout_seconds=30,
                provides=("unit",),
            ),
        ),
        evals=HarnessEvalSpec(max_duration_seconds=max_duration_seconds),
    )


def _build(
    workspace: Path,
    *,
    profile: HarnessProfile | None = None,
    profile_digest: str = "a" * 64,
    batch_id: str = "sandbox-batch-1",
    requested_samples: int = 5,
) -> HarnessSandboxEvalRequest:
    return HarnessSandboxEvalRequestBuilder().build(
        workspace_root=workspace,
        profile=profile or _profile(),
        profile_digest=profile_digest,
        profile_trusted=True,
        check_ids=("unit",),
        batch_id=batch_id,
        requested_samples=requested_samples,
    )


async def _cancel_ticket(
    store: HarnessStore,
    workspace: Path,
    *,
    authority_key: str,
    token: str,
    enqueued_at: str = "2026-07-23T01:01:00+00:00",
    now: str = "2026-07-23T01:02:00+00:00",
):
    ticket = await store.enqueue_sandbox_admission(
        workspace_root=workspace,
        ticket_id=f"hsadm_{token * 24}",
        authority_key=authority_key,
        lane="sandbox",
        requested_samples=5,
        owner_id=f"request-test-{token}",
        now=enqueued_at,
        lease_seconds=300,
        max_active=4,
        max_queued=8,
    )
    receipt, cancelled = await store.cancel_sandbox_admission(
        workspace_root=workspace,
        action_id=f"hsac_{token * 24}",
        ticket_id=ticket.ticket_id,
        authority_key=authority_key,
        epoch=ticket.epoch,
        expected_state=ticket.state,
        actor_id="request-test",
        reason="构造 accepted cancel receipt",
        now=now,
    )
    assert receipt.decision == "accepted"
    assert cancelled is not None
    assert cancelled.state == "cancelled"
    return receipt


async def _prepare_retry(
    tmp_path: Path,
    *,
    cancel_token: str = "1",
    retry_token: str = "2",
):
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    request = _build(workspace)
    await store.record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:00:00+00:00",
    )
    cancel = await _cancel_ticket(
        store,
        workspace,
        authority_key=request.request_sha256,
        token=cancel_token,
    )
    retry = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{retry_token * 24}",
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256=cancel.receipt_sha256,
        actor_id="request-test",
        reason="准备 retry dispatch",
        authority_token="a" * 32,
        now="2026-07-23T01:03:00+00:00",
    )
    assert retry.decision == "accepted"
    return workspace, store, request, cancel, retry


def test_sandbox_request_compiles_clean_git_and_profile_authority(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    profile = _profile()

    first = _build(workspace, profile=profile)
    repeated = _build(workspace, profile=profile)
    second_batch = _build(workspace, profile=profile, batch_id="sandbox-batch-2")

    revision = _git(workspace, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    tree_listing = _git(
        workspace,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        revision,
    )
    assert first == repeated
    assert first.request_id.startswith("hseval_")
    assert first.authority_key == first.request_sha256
    assert first.workspace_root == str(workspace.resolve())
    assert first.lane == "sandbox"
    assert first.source_revision == revision
    assert first.source_tree_sha256 == hashlib.sha256(tree_listing).hexdigest()
    assert first.checks[0].check_id == "unit"
    assert first.check_timeout_seconds_per_sample == 30
    assert first.max_total_duration_seconds == 600
    assert first.suite_id == second_batch.suite_id
    assert first.request_sha256 != second_batch.request_sha256
    assert validate_request_checks(first, profile) == profile.checks

    with pytest.raises(ValidationError, match="摘要不一致"):
        HarnessSandboxEvalRequest.model_validate(
            first.model_copy(update={"batch_id": "tampered"}).model_dump(mode="json")
        )


@pytest.mark.asyncio
async def test_request_manifest_survives_restart_and_fences_batch_identity(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    db_path = tmp_path / "harness.db"
    request = _build(workspace)
    first = await HarnessStore(db_path).record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:00:00+00:00",
    )
    repeated = await HarnessStore(db_path).record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:01:00+00:00",
    )
    restored = await HarnessStore(db_path).get_sandbox_eval_request(
        workspace,
        request.request_sha256,
    )

    assert repeated == first
    assert restored == first
    assert restored is not None
    assert restored.created_at == "2026-07-23T01:00:00+00:00"
    assert restored.request == request

    drifted = _build(workspace, profile_digest="b" * 64)
    with pytest.raises(HarnessStoreConflictError, match="batch"):
        await HarnessStore(db_path).record_sandbox_eval_request(
            drifted,
            created_at="2026-07-23T01:02:00+00:00",
        )


@pytest.mark.asyncio
async def test_request_manifest_detects_persisted_content_tampering(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    db_path = tmp_path / "harness.db"
    request = _build(workspace)
    store = HarnessStore(db_path)
    await store.record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:00:00+00:00",
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_eval_requests
            SET request_json = replace(request_json, ?, ?)
            WHERE request_sha256 = ?
            """,
            ("sandbox-batch-1", "sandbox-batch-X", request.request_sha256),
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="损坏"):
        await HarnessStore(db_path).get_sandbox_eval_request(
            workspace,
            request.request_sha256,
        )


@pytest.mark.asyncio
async def test_request_manifest_concurrent_process_facades_converge(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    db_path = tmp_path / "harness.db"
    request = _build(workspace)

    first, second = await asyncio.gather(
        HarnessStore(db_path).record_sandbox_eval_request(
            request,
            created_at="2026-07-23T01:00:00+00:00",
        ),
        HarnessStore(db_path).record_sandbox_eval_request(
            request,
            created_at="2026-07-23T01:00:01+00:00",
        ),
    )

    assert first == second
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM harness_sandbox_eval_requests"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_retry_authority_consumes_cancel_once_and_survives_restart(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    request = _build(workspace)
    await store.record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:00:00+00:00",
    )
    cancel = await _cancel_ticket(
        store,
        workspace,
        authority_key=request.request_sha256,
        token="1",
    )

    accepted = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'2' * 24}",
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256=cancel.receipt_sha256,
        actor_id="request-test",
        reason="用户显式重试",
        authority_token="a" * 32,
        now="2026-07-23T01:03:00+00:00",
    )
    replayed = await HarnessStore(db_path).authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=accepted.action_id,
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256=cancel.receipt_sha256,
        actor_id="request-test",
        reason="用户显式重试",
        authority_token="b" * 32,
        now="2026-07-23T01:04:00+00:00",
    )
    restored = await HarnessStore(db_path).get_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=accepted.action_id,
    )
    consumed = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'3' * 24}",
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256=cancel.receipt_sha256,
        actor_id="request-test",
        reason="不允许重复消费",
        authority_token="c" * 32,
        now="2026-07-23T01:05:00+00:00",
    )

    assert accepted == replayed == restored
    assert accepted.decision == "accepted"
    assert accepted.code == "sandbox_batch_retry_authorized"
    assert accepted.eval_request_sha256 == request.request_sha256
    assert accepted.execution_authority_key != request.request_sha256
    assert consumed.decision == "rejected"
    assert consumed.code == "sandbox_batch_retry_cancel_receipt_consumed"
    assert consumed.execution_authority_key == ""
    with pytest.raises(HarnessStoreConflictError, match="不同请求"):
        await store.authorize_sandbox_admission_retry(
            workspace_root=workspace,
            action_id=accepted.action_id,
            cancel_receipt_id=cancel.receipt_id,
            cancel_receipt_sha256=cancel.receipt_sha256,
            actor_id="request-test",
            reason="改写同 action 的理由",
            authority_token="d" * 32,
            now="2026-07-23T01:06:00+00:00",
        )


@pytest.mark.asyncio
async def test_retry_authority_rejects_untrusted_cancel_or_missing_manifest(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    request = _build(workspace)
    await store.record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:00:00+00:00",
    )
    cancel = await _cancel_ticket(
        store,
        workspace,
        authority_key=request.request_sha256,
        token="4",
    )

    mismatch = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'5' * 24}",
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256="f" * 64,
        actor_id="request-test",
        reason="错误 cancel digest",
        authority_token="d" * 32,
        now="2026-07-23T01:03:00+00:00",
    )
    missing_cancel = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'6' * 24}",
        cancel_receipt_id=f"hsacr_{'0' * 24}",
        cancel_receipt_sha256="0" * 64,
        actor_id="request-test",
        reason="不存在的 cancel receipt",
        authority_token="e" * 32,
        now="2026-07-23T01:03:00+00:00",
    )
    rollback = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'0' * 24}",
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256=cancel.receipt_sha256,
        actor_id="request-test",
        reason="回退时钟",
        authority_token="0" * 32,
        now="2026-07-23T00:59:00+00:00",
    )
    rejected_cancel, _ = await store.cancel_sandbox_admission(
        workspace_root=workspace,
        action_id=f"hsac_{'a' * 24}",
        ticket_id=cancel.ticket_id,
        authority_key=cancel.authority_key,
        epoch=cancel.presented_epoch,
        expected_state=cancel.presented_state,
        actor_id="request-test",
        reason="终态 ticket 的 rejected cancel",
        now="2026-07-23T01:03:00+00:00",
    )
    not_accepted = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'b' * 24}",
        cancel_receipt_id=rejected_cancel.receipt_id,
        cancel_receipt_sha256=rejected_cancel.receipt_sha256,
        actor_id="request-test",
        reason="rejected cancel 不得重试",
        authority_token="b" * 32,
        now="2026-07-23T01:04:00+00:00",
    )
    foreign_cancel = await _cancel_ticket(
        store,
        workspace,
        authority_key="e" * 64,
        token="7",
    )
    missing_manifest = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'8' * 24}",
        cancel_receipt_id=foreign_cancel.receipt_id,
        cancel_receipt_sha256=foreign_cancel.receipt_sha256,
        actor_id="request-test",
        reason="没有 request manifest",
        authority_token="f" * 32,
        now="2026-07-23T01:03:00+00:00",
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_admission_tickets
            SET state = 'completed'
            WHERE ticket_id = ?
            """,
            (cancel.ticket_id,),
        )
        db.commit()
    invalid_source = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'c' * 24}",
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256=cancel.receipt_sha256,
        actor_id="request-test",
        reason="source ticket 已被篡改",
        authority_token="c" * 32,
        now="2026-07-23T01:05:00+00:00",
    )

    assert mismatch.code == "sandbox_batch_retry_cancel_receipt_mismatch"
    assert missing_cancel.code == "sandbox_batch_retry_cancel_receipt_not_found"
    assert rollback.code == "sandbox_batch_retry_clock_rollback"
    assert not_accepted.code == "sandbox_batch_retry_cancel_not_accepted"
    assert missing_manifest.code == "sandbox_batch_retry_request_manifest_missing"
    assert invalid_source.code == "sandbox_batch_retry_source_ticket_invalid"
    assert {
        mismatch.decision,
        missing_cancel.decision,
        rollback.decision,
        not_accepted.decision,
        missing_manifest.decision,
        invalid_source.decision,
    } == {"rejected"}


@pytest.mark.asyncio
async def test_retry_authority_concurrency_and_chain_resolve_original_request(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    request = _build(workspace)
    await store.record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:00:00+00:00",
    )
    cancel = await _cancel_ticket(
        store,
        workspace,
        authority_key=request.request_sha256,
        token="9",
    )

    decisions = await asyncio.gather(
        HarnessStore(db_path).authorize_sandbox_admission_retry(
            workspace_root=workspace,
            action_id=f"hsar_{'a' * 24}",
            cancel_receipt_id=cancel.receipt_id,
            cancel_receipt_sha256=cancel.receipt_sha256,
            actor_id="process-a",
            reason="并发重试 A",
            authority_token="1" * 32,
            now="2026-07-23T01:03:00+00:00",
        ),
        HarnessStore(db_path).authorize_sandbox_admission_retry(
            workspace_root=workspace,
            action_id=f"hsar_{'b' * 24}",
            cancel_receipt_id=cancel.receipt_id,
            cancel_receipt_sha256=cancel.receipt_sha256,
            actor_id="process-b",
            reason="并发重试 B",
            authority_token="2" * 32,
            now="2026-07-23T01:03:00+00:00",
        ),
    )
    accepted = next(item for item in decisions if item.decision == "accepted")
    rejected = next(item for item in decisions if item.decision == "rejected")
    assert rejected.code == "sandbox_batch_retry_cancel_receipt_consumed"

    chained_cancel = await _cancel_ticket(
        store,
        workspace,
        authority_key=accepted.execution_authority_key,
        token="c",
        enqueued_at="2026-07-23T01:05:00+00:00",
        now="2026-07-23T01:06:00+00:00",
    )
    chained = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'d' * 24}",
        cancel_receipt_id=chained_cancel.receipt_id,
        cancel_receipt_sha256=chained_cancel.receipt_sha256,
        actor_id="request-test",
        reason="取消 retry 后再次显式重试",
        authority_token="3" * 32,
        now="2026-07-23T01:07:00+00:00",
    )

    assert chained.decision == "accepted"
    assert chained.eval_request_sha256 == request.request_sha256
    assert chained.source_authority_key == accepted.execution_authority_key
    assert chained.execution_authority_key not in {
        request.request_sha256,
        accepted.execution_authority_key,
    }


@pytest.mark.asyncio
async def test_retry_authority_wrapper_and_tamper_detection(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    request = _build(workspace)
    await store.record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:00:00+00:00",
    )
    cancel = await _cancel_ticket(
        store,
        workspace,
        authority_key=request.request_sha256,
        token="e",
    )
    admission = HarnessSandboxBatchAdmission(
        max_active=4,
        max_queued=8,
        store=store,
        workspace_root=workspace,
        token=lambda: "4" * 32,
        now=lambda: "2026-07-23T01:03:00+00:00",
    )
    accepted = await admission.authorize_retry(
        action_id=f"hsar_{'f' * 24}",
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256=cancel.receipt_sha256,
        actor_id="request-test",
        reason="通过 admission facade 授权",
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_admission_retry_attempts
            SET action_sha256 = ?
            WHERE action_id = ?
            """,
            ("0" * 64, accepted.action_id),
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="action 摘要"):
        await HarnessStore(db_path).get_sandbox_admission_retry(
            workspace_root=workspace,
            action_id=accepted.action_id,
        )


@pytest.mark.asyncio
async def test_retry_dispatch_claims_new_ticket_and_persists_terminal(
    tmp_path: Path,
) -> None:
    workspace, store, request, cancel, retry = await _prepare_retry(tmp_path)
    dispatch, ticket = await store.claim_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
        retry_receipt_id=retry.receipt_id,
        retry_receipt_sha256=retry.receipt_sha256,
        ticket_id=f"hsadm_{'3' * 24}",
        owner_id="retry-worker-a",
        now="2026-07-23T01:04:00+00:00",
        lease_seconds=30,
        max_active=4,
        max_queued=8,
    )

    assert dispatch.state == "claimed"
    assert dispatch.epoch == 1
    assert dispatch.ticket_id == ticket.ticket_id
    assert ticket.ticket_id != cancel.ticket_id
    assert ticket.authority_key == retry.execution_authority_key
    assert ticket.authority_key != request.request_sha256
    assert ticket.requested_samples == request.requested_samples
    with pytest.raises(
        HarnessSandboxRetryDispatchFenceError,
        match="存活 ticket",
    ):
        await HarnessStore(store.db_path).claim_sandbox_retry_dispatch(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
            retry_receipt_id=retry.receipt_id,
            retry_receipt_sha256=retry.receipt_sha256,
            ticket_id=f"hsadm_{'4' * 24}",
            owner_id="retry-worker-b",
            now="2026-07-23T01:04:01+00:00",
            lease_seconds=30,
            max_active=4,
            max_queued=8,
        )

    terminal_ticket = await store.finish_sandbox_admission(
        workspace_root=workspace,
        ticket_id=ticket.ticket_id,
        owner_id=ticket.owner_id,
        epoch=ticket.epoch,
        state="completed",
        terminal_code="",
        now="2026-07-23T01:04:02+00:00",
    )
    terminal = await store.finish_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
        owner_id=dispatch.owner_id,
        dispatch_epoch=dispatch.epoch,
        ticket_id=terminal_ticket.ticket_id,
        ticket_epoch=terminal_ticket.epoch,
        state="completed",
        terminal_code="",
        now="2026-07-23T01:04:02+00:00",
    )
    restored = await HarnessStore(store.db_path).get_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
    )

    assert terminal == restored
    assert terminal.state == "completed"
    with pytest.raises(HarnessSandboxRetryDispatchFenceError, match="终态"):
        await store.claim_sandbox_retry_dispatch(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
            retry_receipt_id=retry.receipt_id,
            retry_receipt_sha256=retry.receipt_sha256,
            ticket_id=f"hsadm_{'5' * 24}",
            owner_id="retry-worker-c",
            now="2026-07-23T01:04:03+00:00",
            lease_seconds=30,
            max_active=4,
            max_queued=8,
        )


@pytest.mark.asyncio
async def test_retry_dispatch_recovers_only_after_ticket_expiry(tmp_path: Path) -> None:
    workspace, store, _request, _cancel, retry = await _prepare_retry(tmp_path)
    first, first_ticket = await store.claim_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
        retry_receipt_id=retry.receipt_id,
        retry_receipt_sha256=retry.receipt_sha256,
        ticket_id=f"hsadm_{'6' * 24}",
        owner_id="retry-crashed-a",
        now="2026-07-23T01:04:00+00:00",
        lease_seconds=2,
        max_active=4,
        max_queued=8,
    )
    recovered, recovered_ticket = await HarnessStore(
        store.db_path
    ).claim_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
        retry_receipt_id=retry.receipt_id,
        retry_receipt_sha256=retry.receipt_sha256,
        ticket_id=f"hsadm_{'7' * 24}",
        owner_id="retry-recovery-b",
        now="2026-07-23T01:04:03+00:00",
        lease_seconds=2,
        max_active=4,
        max_queued=8,
    )
    expired = await store.get_sandbox_admission(
        workspace_root=workspace,
        ticket_id=first_ticket.ticket_id,
        now="2026-07-23T01:04:03+00:00",
    )

    assert expired is not None
    assert expired.state == "expired"
    assert recovered.epoch == first.epoch + 1
    assert recovered.owner_id == "retry-recovery-b"
    assert recovered.ticket_id == recovered_ticket.ticket_id
    assert recovered_ticket.ticket_id != first_ticket.ticket_id
    with pytest.raises(HarnessSandboxRetryDispatchFenceError, match="fence"):
        await store.finish_sandbox_retry_dispatch(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
            owner_id=first.owner_id,
            dispatch_epoch=first.epoch,
            ticket_id=first.ticket_id,
            ticket_epoch=first.ticket_epoch,
            state="failed",
            terminal_code="stale-worker",
            now="2026-07-23T01:04:04+00:00",
        )


@pytest.mark.asyncio
async def test_retry_admission_facade_completes_dispatch_and_detects_tamper(
    tmp_path: Path,
) -> None:
    workspace, store, _request, cancel, retry = await _prepare_retry(tmp_path)
    admission = HarnessSandboxBatchAdmission(
        max_active=4,
        max_queued=8,
        store=store,
        workspace_root=workspace,
        owner_id="retry-facade",
        lease_seconds=30,
        poll_interval_seconds=0.01,
        token=lambda: "8" * 32,
        now=lambda: "2026-07-23T01:04:00+00:00",
    )

    async with admission.admit_retry(
        retry_action_id=retry.action_id,
        retry_receipt_id=retry.receipt_id,
        retry_receipt_sha256=retry.receipt_sha256,
    ) as ticket:
        assert ticket.ticket_id != cancel.ticket_id
        assert ticket.authority_key == retry.execution_authority_key

    dispatch = await store.get_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
    )
    assert dispatch is not None
    assert dispatch.state == "completed"
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_retry_dispatches
            SET request_sha256 = ?
            WHERE retry_action_id = ?
            """,
            ("0" * 64, retry.action_id),
        )
        db.commit()
    with pytest.raises(HarnessStoreError, match="dispatch 摘要"):
        await HarnessStore(store.db_path).get_sandbox_retry_dispatch(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
        )

    non_durable = HarnessSandboxBatchAdmission()
    with pytest.raises(HarnessSandboxBatchError, match="持久化"):
        async with non_durable.admit_retry(
            retry_action_id=retry.action_id,
            retry_receipt_id=retry.receipt_id,
            retry_receipt_sha256=retry.receipt_sha256,
        ):
            pytest.fail("non-durable retry must not enter")


@pytest.mark.asyncio
async def test_retry_dispatch_user_cancel_is_terminal_not_crash_recovery(
    tmp_path: Path,
) -> None:
    workspace, store, _request, _cancel, retry = await _prepare_retry(tmp_path)
    admission = HarnessSandboxBatchAdmission(
        max_active=4,
        max_queued=8,
        store=store,
        workspace_root=workspace,
        owner_id="retry-cancel",
        lease_seconds=30,
        poll_interval_seconds=0.01,
        token=lambda: "9" * 32,
        now=lambda: "2026-07-23T01:04:00+00:00",
    )
    entered = asyncio.Event()
    ticket_holder = []

    async def execute() -> None:
        async with admission.admit_retry(
            retry_action_id=retry.action_id,
            retry_receipt_id=retry.receipt_id,
            retry_receipt_sha256=retry.receipt_sha256,
        ) as ticket:
            ticket_holder.append(ticket)
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(execute())
    await entered.wait()
    ticket = ticket_holder[0]
    cancel, _current = await admission.cancel(
        action_id=f"hsac_{'f' * 24}",
        ticket_id=ticket.ticket_id,
        authority_key=ticket.authority_key,
        epoch=ticket.epoch,
        expected_state=ticket.state,
        actor_id="request-test",
        reason="用户取消 retry dispatch",
    )
    assert cancel.decision == "accepted"
    with pytest.raises(asyncio.CancelledError):
        await task

    dispatch = await store.get_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
    )
    assert dispatch is not None
    assert dispatch.state == "cancelled"
    with pytest.raises(HarnessSandboxRetryDispatchFenceError, match="终态"):
        await store.claim_sandbox_retry_dispatch(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
            retry_receipt_id=retry.receipt_id,
            retry_receipt_sha256=retry.receipt_sha256,
            ticket_id=f"hsadm_{'a' * 24}",
            owner_id="must-not-recover",
            now="2026-07-23T01:04:01+00:00",
            lease_seconds=30,
            max_active=4,
            max_queued=8,
        )


@pytest.mark.asyncio
async def test_retry_dispatch_capacity_failure_is_atomic(tmp_path: Path) -> None:
    workspace, store, _request, _cancel, retry = await _prepare_retry(tmp_path)
    blocker = await store.enqueue_sandbox_admission(
        workspace_root=workspace,
        ticket_id=f"hsadm_{'b' * 24}",
        authority_key="b" * 64,
        lane="sandbox",
        requested_samples=5,
        owner_id="capacity-blocker",
        now="2026-07-23T01:04:00+00:00",
        lease_seconds=30,
        max_active=1,
        max_queued=0,
    )
    with pytest.raises(HarnessSandboxAdmissionCapacityError):
        await store.claim_sandbox_retry_dispatch(
            workspace_root=workspace,
            retry_action_id=retry.action_id,
            retry_receipt_id=retry.receipt_id,
            retry_receipt_sha256=retry.receipt_sha256,
            ticket_id=f"hsadm_{'c' * 24}",
            owner_id="capacity-retry",
            now="2026-07-23T01:04:01+00:00",
            lease_seconds=30,
            max_active=1,
            max_queued=0,
        )
    assert await store.get_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
    ) is None

    await store.finish_sandbox_admission(
        workspace_root=workspace,
        ticket_id=blocker.ticket_id,
        owner_id=blocker.owner_id,
        epoch=blocker.epoch,
        state="completed",
        terminal_code="",
        now="2026-07-23T01:04:02+00:00",
    )
    dispatch, ticket = await store.claim_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
        retry_receipt_id=retry.receipt_id,
        retry_receipt_sha256=retry.receipt_sha256,
        ticket_id=f"hsadm_{'c' * 24}",
        owner_id="capacity-retry",
        now="2026-07-23T01:04:03+00:00",
        lease_seconds=30,
        max_active=1,
        max_queued=0,
    )
    assert dispatch.state == "claimed"
    assert ticket.state == "active"


@pytest.mark.asyncio
async def test_retry_admission_exception_marks_dispatch_failed(tmp_path: Path) -> None:
    workspace, store, _request, _cancel, retry = await _prepare_retry(tmp_path)
    admission = HarnessSandboxBatchAdmission(
        max_active=4,
        max_queued=8,
        store=store,
        workspace_root=workspace,
        owner_id="retry-failure",
        lease_seconds=30,
        poll_interval_seconds=0.01,
        token=lambda: "d" * 32,
        now=lambda: "2026-07-23T01:04:00+00:00",
    )

    with pytest.raises(RuntimeError, match="simulated dispatch body failure"):
        async with admission.admit_retry(
            retry_action_id=retry.action_id,
            retry_receipt_id=retry.receipt_id,
            retry_receipt_sha256=retry.receipt_sha256,
        ):
            raise RuntimeError("simulated dispatch body failure")

    dispatch = await store.get_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
    )
    assert dispatch is not None
    assert dispatch.state == "failed"
    ticket = await store.get_sandbox_admission(
        workspace_root=workspace,
        ticket_id=dispatch.ticket_id,
        now="2026-07-23T01:04:00+00:00",
    )
    assert ticket is not None
    assert ticket.state == "failed"
    assert ticket.terminal_code == "sandbox_batch_execution_failed"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"profile_trusted": False}, "sandbox_request_profile_untrusted"),
        ({"profile_digest": "bad"}, "sandbox_request_profile_digest_invalid"),
        ({"check_ids": ("missing",)}, "sandbox_request_profile_check_missing"),
        ({"check_ids": ("unit", "unit")}, "sandbox_request_check_ids_duplicated"),
        ({"batch_id": "../escape"}, "sandbox_request_batch_id_invalid"),
        ({"requested_samples": True}, "sandbox_request_sample_count_invalid"),
    ],
)
def test_sandbox_request_rejects_invalid_authority_before_git(
    tmp_path: Path,
    kwargs: dict[str, object],
    code: str,
) -> None:
    values = {
        "workspace_root": tmp_path / "not-created",
        "profile": _profile(),
        "profile_digest": "a" * 64,
        "profile_trusted": True,
        "check_ids": ("unit",),
        "batch_id": "batch",
        "requested_samples": 5,
        **kwargs,
    }

    with pytest.raises(HarnessSandboxEvalRequestError) as captured:
        HarnessSandboxEvalRequestBuilder().build(**values)  # type: ignore[arg-type]

    assert captured.value.code == code


def test_sandbox_request_rejects_dirty_or_nested_git_workspace(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "untracked.txt").write_text("not committed\n", encoding="utf-8")

    with pytest.raises(HarnessSandboxEvalRequestError) as dirty:
        _build(workspace)
    assert dirty.value.code == "sandbox_request_worktree_dirty"

    (workspace / "untracked.txt").unlink()
    nested = workspace / "nested"
    nested.mkdir()
    with pytest.raises(HarnessSandboxEvalRequestError) as wrong_root:
        _build(nested)
    assert wrong_root.value.code == "sandbox_request_git_root_mismatch"


def test_sandbox_request_enforces_worst_case_duration_budget(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    profile = _profile(
        check=HarnessCheckSpec(
            id="unit",
            argv=("uv", "run", "pytest"),
            timeout_seconds=121,
        ),
        max_duration_seconds=600,
    )

    with pytest.raises(HarnessSandboxEvalRequestError) as captured:
        _build(workspace, profile=profile)

    assert captured.value.code == "sandbox_request_duration_budget_exceeded"


def test_sandbox_request_detects_profile_check_drift_after_compilation(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    request = _build(workspace)
    drifted = _profile(
        check=HarnessCheckSpec(
            id="unit",
            argv=("uv", "run", "pytest", "-q", "tests/unit/test_other.py"),
            timeout_seconds=30,
        )
    )

    with pytest.raises(HarnessSandboxEvalRequestError) as captured:
        validate_request_checks(request, drifted)

    assert captured.value.code == "sandbox_request_profile_check_drifted"
