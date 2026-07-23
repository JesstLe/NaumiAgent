from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantStore,
)
from naumi_agent.harness.eval_models import EvalRunStatus, HarnessEvalSuiteResult
from naumi_agent.harness.sandbox_batch import (
    HarnessSandboxBatchAdmission,
    HarnessSandboxBatchCheckpoint,
    HarnessSandboxBatchError,
)
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckResult,
    HarnessSandboxCheckStatus,
)
from naumi_agent.harness.sandbox_request import HarnessSandboxEvalRequestBuilder
from naumi_agent.harness.sandbox_service import (
    HarnessSandboxEvalExecutor,
    HarnessSandboxEvalServiceError,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.safety.permissions import PermissionMode

NOW = "2026-07-23T00:00:00+00:00"
PROFILE = """\
schema_version: 1
checks:
  - id: unit
    label: 定向单测
    argv: [python3, -c, "print('sandbox service ok')"]
    timeout_seconds: 10
evals:
  max_duration_seconds: 300
"""


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
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir()
    profile.write_text(PROFILE, encoding="utf-8")
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "harness@example.invalid")
    _git(workspace, "config", "user.name", "Harness Test")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "initial")
    return workspace


class _ExecutionKernel:
    def __init__(
        self,
        workspace: Path,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        *,
        fail_once_at: int | None = None,
        drift_after_sample: int | None = None,
        wrong_source_at: int | None = None,
        pause_once_at: int | None = None,
    ) -> None:
        self.workspace_root = workspace
        self.permission_store = permission_store
        self.run_grant_authority = run_grant_authority
        self.fail_once_at = fail_once_at
        self.drift_after_sample = drift_after_sample
        self.wrong_source_at = wrong_source_at
        self.pause_once_at = pause_once_at
        self.failed = False
        self.paused = False
        self.pause_reached = asyncio.Event()
        self.pause_release = asyncio.Event()
        self.calls: list[SimpleNamespace] = []
        self.validation_now = NOW

    async def execute(self, **kwargs):
        sample_index = kwargs["sample_index"]
        authority = kwargs["run_authority"]
        validation = await self.run_grant_authority.validate(
            grant_id=authority.grant_id,
            now=self.validation_now,
        )
        assert validation.allowed
        assert await kwargs["profile_is_current"]()
        self.calls.append(SimpleNamespace(**kwargs))
        if self.pause_once_at == sample_index and not self.paused:
            self.paused = True
            self.pause_reached.set()
            await self.pause_release.wait()
        if (
            self.fail_once_at == sample_index
            and not self.failed
        ):
            self.failed = True
            raise RuntimeError("simulated sample interruption")
        source = kwargs["source"]
        results = tuple(
            HarnessSandboxCheckResult(
                check_id=check.id,
                run_id=f"sandbox-{sample_index}-{check.id}",
                status=HarnessSandboxCheckStatus.PASSED,
                source_revision=source.revision,
                source_tree_sha256=(
                    "f" * 64
                    if self.wrong_source_at == sample_index
                    else source.revision_tree_sha256
                ),
                snapshot_manifest_sha256=hashlib.sha256(
                    f"manifest:{sample_index}:{check.id}".encode()
                ).hexdigest(),
                profile_digest=kwargs["profile_digest"],
                job_id=f"job-{sample_index}-{check.id}",
                lifecycle_receipt_sha256=hashlib.sha256(
                    f"lifecycle:{sample_index}:{check.id}".encode()
                ).hexdigest(),
                output="sandbox service ok",
                exit_code=0,
                duration_ms=10,
                artifact_path=None,
                message="检查通过。",
            )
            for check in kwargs["checks"]
        )
        if self.drift_after_sample == sample_index:
            profile = self.workspace_root / ".naumi" / "harness.yaml"
            profile.write_text(PROFILE + "\n", encoding="utf-8")
        return results


async def _runtime(
    tmp_path: Path,
    *,
    fail_once_at: int | None = None,
    drift_after_sample: int | None = None,
    wrong_source_at: int | None = None,
    pause_once_at: int | None = None,
    durable_admission: bool = False,
):
    workspace = _workspace(tmp_path)
    permission_store = PermissionDecisionReceiptStore(tmp_path / "permissions.db")
    harness_store = HarnessStore(tmp_path / "harness.db")
    grant_authority = RunDelegationGrantAuthority(
        store=RunDelegationGrantStore(tmp_path / "run-grants.db"),
        permission_store=permission_store,
        harness_store=harness_store,
        workspace_root=workspace,
    )
    kernel = _ExecutionKernel(
        workspace,
        permission_store,
        grant_authority,
        fail_once_at=fail_once_at,
        drift_after_sample=drift_after_sample,
        wrong_source_at=wrong_source_at,
        pause_once_at=pause_once_at,
    )
    admission_tokens = iter(
        hashlib.sha256(f"admission:{index}".encode()).hexdigest()
        for index in range(1, 100)
    )
    admission = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=1,
        store=harness_store if durable_admission else None,
        workspace_root=workspace if durable_admission else None,
        owner_id="sandbox-service-test-owner" if durable_admission else None,
        lease_seconds=30,
        poll_interval_seconds=0.01,
        now=lambda: NOW,
        token=lambda: next(admission_tokens),
    )
    executor = HarnessSandboxEvalExecutor(
        workspace_root=workspace,
        store=harness_store,
        permission_store=permission_store,
        run_grant_authority=grant_authority,
        execution_kernel=kernel,  # type: ignore[arg-type]
        admission=admission,
        now=lambda: NOW,
        token=lambda: "servicebatch",
    )
    parent = await permission_store.issue(
        request_id="request-1",
        session_id="session-1",
        run_id="run-1",
        call_id="call-1",
        agent_name="main",
        tool_name="harness_eval_sandbox",
        tool_family="harness_eval_execution",
        arguments={
            "batch_id": "batch-1",
            "check_ids": ["unit"],
            "run_id": "run-1",
            "samples": 5,
        },
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.BYPASS,
        risk_level="medium",
        delegated_tool_names=("bash_run",),
        decided_at=NOW,
    )
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=harness_store,
        authorization_receipt_provider=lambda: parent,
        sandbox_eval_executor=executor,
    )
    await service.trust(source="test")
    return service, harness_store, grant_authority, kernel, parent


async def _request(service: HarnessService):
    status = await service.status()
    assert status.snapshot.profile is not None
    assert status.profile_digest is not None
    return HarnessSandboxEvalRequestBuilder().build(
        workspace_root=service.workspace_root,
        profile=status.snapshot.profile,
        profile_digest=status.profile_digest,
        profile_trusted=status.trusted,
        check_ids=("unit",),
        batch_id="batch-1",
        requested_samples=5,
    )


@pytest.mark.asyncio
async def test_service_executes_native_batch_persists_h5a_and_reuses_receipt(
    tmp_path: Path,
) -> None:
    service, store, grant_authority, kernel, _parent = await _runtime(tmp_path)
    checkpoints: list[HarnessSandboxBatchCheckpoint] = []

    async def capture(checkpoint: HarnessSandboxBatchCheckpoint) -> None:
        checkpoints.append(checkpoint)

    first = await service.eval_sandbox(
        check_ids=("unit",),
        samples=5,
        batch_id="batch-1",
        on_progress=capture,
    )
    repeated = await service.eval_sandbox(
        check_ids=("unit",),
        samples=5,
        batch_id="batch-1",
    )
    records = await store.list_eval_results(
        service.workspace_root,
        "batch-1",
        first.suite_id,
    )

    assert first == repeated
    assert first.persisted_samples == 5
    assert first.check_ids == ("unit",)
    assert len(first.sample_result_sha256) == 5
    assert len(first.run_grant_sha256) == 1
    assert len(records) == 5
    assert [item.sample_index for item in records] == list(range(5))
    assert all(item.result.baseline_identity is not None for item in records)
    assert all(
        item.result.baseline_identity.source.commit
        == _git(service.workspace_root, "rev-parse", "HEAD").decode().strip()
        for item in records
        if item.result.baseline_identity is not None
    )
    assert len(kernel.calls) == 5
    assert [item.stage for item in checkpoints] == [
        "recovering",
        "acquiring",
        "executing",
        "executing",
        "executing",
        "executing",
        "executing",
        "completed",
    ]
    validation = await grant_authority.validate(
        grant_id=kernel.calls[0].run_authority.grant_id,
        now=NOW,
    )
    assert not validation.allowed


@pytest.mark.asyncio
async def test_service_resumes_continuous_h5a_prefix_with_new_batch_grant(
    tmp_path: Path,
) -> None:
    service, store, _grant_authority, kernel, _parent = await _runtime(
        tmp_path,
        fail_once_at=2,
    )

    with pytest.raises(RuntimeError, match="simulated sample interruption"):
        await service.eval_sandbox(
            check_ids=("unit",),
            samples=5,
            batch_id="batch-1",
        )

    request = await _request(service)
    manifest = await HarnessStore(tmp_path / "harness.db").get_sandbox_eval_request(
        service.workspace_root,
        request.request_sha256,
    )
    partial = await store.list_eval_results(
        service.workspace_root,
        "batch-1",
        request.suite_id,
    )
    assert manifest is not None
    assert manifest.request == request
    assert len(partial) == 2
    assert len(kernel.calls) == 3

    receipt = await service.eval_sandbox(
        check_ids=("unit",),
        samples=5,
        batch_id="batch-1",
    )
    records = await store.list_eval_results(
        service.workspace_root,
        "batch-1",
        receipt.suite_id,
    )

    assert receipt.persisted_samples == 5
    assert len(receipt.run_grant_sha256) == 2
    assert len(records) == 5
    assert len(kernel.calls) == 6


@pytest.mark.asyncio
async def test_service_rejects_permission_not_bound_to_exact_batch_arguments(
    tmp_path: Path,
) -> None:
    service, store, _grant_authority, kernel, _parent = await _runtime(tmp_path)
    wrong = await kernel.permission_store.issue(
        request_id="request-2",
        session_id="session-1",
        run_id="run-1",
        call_id="call-2",
        agent_name="main",
        tool_name="harness_eval_sandbox",
        tool_family="harness_eval_execution",
        arguments={
            "batch_id": "other-batch",
            "check_ids": ["unit"],
            "run_id": "run-1",
            "samples": 5,
        },
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.BYPASS,
        risk_level="medium",
        delegated_tool_names=("bash_run",),
        decided_at=NOW,
    )
    service._authorization_receipt_provider = lambda: wrong

    with pytest.raises(HarnessSandboxEvalServiceError) as captured:
        await service.eval_sandbox(
            check_ids=("unit",),
            samples=5,
            batch_id="batch-1",
        )

    assert captured.value.code == "sandbox_eval_service_parent_permission_invalid"
    assert not kernel.calls
    request = await _request(service)
    assert await store.get_sandbox_eval_request(
        service.workspace_root,
        request.request_sha256,
    ) is None
    assert await store.list_eval_results(
        service.workspace_root,
        "batch-1",
        request.suite_id,
    ) == ()


@pytest.mark.asyncio
async def test_service_rejects_foreign_h5a_prefix_before_grant_or_execution(
    tmp_path: Path,
) -> None:
    service, store, _grant_authority, kernel, _parent = await _runtime(tmp_path)
    request = await _request(service)
    await store.record_eval_result(
        workspace_root=service.workspace_root,
        batch_id=request.batch_id,
        sample_index=0,
        result=HarnessEvalSuiteResult(
            suite_id=request.suite_id,
            title="伪造结果",
            suite_path="foreign",
            status=EvalRunStatus.PASSED,
        ),
        created_at=NOW,
    )

    with pytest.raises(HarnessSandboxEvalServiceError) as captured:
        await service.eval_sandbox(
            check_ids=("unit",),
            samples=5,
            batch_id="batch-1",
        )

    assert captured.value.code == "sandbox_eval_service_h5a_authority_mismatch"
    assert not kernel.calls


@pytest.mark.asyncio
async def test_service_revalidates_profile_before_persisting_sample(
    tmp_path: Path,
) -> None:
    service, store, grant_authority, kernel, _parent = await _runtime(
        tmp_path,
        drift_after_sample=0,
    )
    request = await _request(service)

    with pytest.raises(HarnessSandboxEvalServiceError) as captured:
        await service.eval_sandbox(
            check_ids=("unit",),
            samples=5,
            batch_id="batch-1",
        )

    assert captured.value.code == (
        "sandbox_eval_service_profile_trust_revalidation_failed"
    )
    assert await store.list_eval_results(
        service.workspace_root,
        request.batch_id,
        request.suite_id,
    ) == ()
    validation = await grant_authority.validate(
        grant_id=kernel.calls[0].run_authority.grant_id,
        now=NOW,
    )
    assert not validation.allowed


@pytest.mark.asyncio
async def test_service_rejects_kernel_source_evidence_before_h5a_persistence(
    tmp_path: Path,
) -> None:
    service, store, grant_authority, kernel, _parent = await _runtime(
        tmp_path,
        wrong_source_at=0,
    )
    request = await _request(service)

    with pytest.raises(HarnessSandboxEvalServiceError) as captured:
        await service.eval_sandbox(
            check_ids=("unit",),
            samples=5,
            batch_id="batch-1",
        )

    assert captured.value.code == "sandbox_eval_service_kernel_evidence_invalid"
    assert await store.list_eval_results(
        service.workspace_root,
        request.batch_id,
        request.suite_id,
    ) == ()
    validation = await grant_authority.validate(
        grant_id=kernel.calls[0].run_authority.grant_id,
        now=NOW,
    )
    assert not validation.allowed


@pytest.mark.asyncio
async def test_retry_uses_new_dispatch_authority_and_resumes_original_h5a_prefix(
    tmp_path: Path,
) -> None:
    service, store, _grant_authority, kernel, _parent = await _runtime(
        tmp_path,
        pause_once_at=2,
        durable_admission=True,
    )
    admission = service._sandbox_eval_executor.coordinator.admission
    checkpoints: list[HarnessSandboxBatchCheckpoint] = []

    async def capture(checkpoint: HarnessSandboxBatchCheckpoint) -> None:
        checkpoints.append(checkpoint)

    original_task = asyncio.create_task(
        service.eval_sandbox(
            check_ids=("unit",),
            samples=5,
            batch_id="batch-1",
            on_progress=capture,
        )
    )
    await asyncio.wait_for(kernel.pause_reached.wait(), timeout=2)
    live = checkpoints[-1]
    assert live.stage == "executing"
    assert live.persisted_samples == 2
    assert live.admission_ticket_id is not None
    assert live.admission_epoch is not None
    assert live.admission_state == "active"

    cancel_receipt, cancelled_ticket = await admission.cancel(
        action_id=f"hsac_{'a' * 24}",
        ticket_id=live.admission_ticket_id,
        authority_key=live.authority_key,
        epoch=live.admission_epoch,
        expected_state="active",
        actor_id="test-user",
        reason="验证持久化 retry 恢复",
    )
    assert cancel_receipt.decision == "accepted"
    assert cancelled_ticket is not None
    assert cancelled_ticket.state == "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await original_task

    request = await _request(service)
    prefix = await store.list_eval_results(
        service.workspace_root,
        request.batch_id,
        request.suite_id,
    )
    assert [item.sample_index for item in prefix] == [0, 1]

    retry_arguments = {
        "cancel_receipt_id": cancel_receipt.receipt_id,
        "cancel_receipt_sha256": cancel_receipt.receipt_sha256,
        "reason": "用户确认恢复原 Sandbox Eval",
        "retry_action_id": f"hsar_{'b' * 24}",
        "run_id": "run-1",
    }
    retry_parent = await kernel.permission_store.issue(
        request_id="request-retry-1",
        session_id="session-1",
        run_id="run-1",
        call_id="call-retry-1",
        agent_name="main",
        tool_name="harness_eval_sandbox_retry",
        tool_family="harness_eval_execution",
        arguments=retry_arguments,
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.BYPASS,
        risk_level="medium",
        delegated_tool_names=("bash_run",),
        decided_at=NOW,
    )
    service._authorization_receipt_provider = lambda: retry_parent
    retry_checkpoints: list[HarnessSandboxBatchCheckpoint] = []

    async def capture_retry(checkpoint: HarnessSandboxBatchCheckpoint) -> None:
        retry_checkpoints.append(checkpoint)

    completed = await service.retry_sandbox(
        retry_action_id=retry_arguments["retry_action_id"],
        cancel_receipt_id=retry_arguments["cancel_receipt_id"],
        cancel_receipt_sha256=retry_arguments["cancel_receipt_sha256"],
        reason=retry_arguments["reason"],
        on_progress=capture_retry,
    )
    retry_authority = await store.get_sandbox_admission_retry(
        workspace_root=service.workspace_root,
        action_id=retry_arguments["retry_action_id"],
    )
    dispatch = await store.get_sandbox_retry_dispatch(
        workspace_root=service.workspace_root,
        retry_action_id=retry_arguments["retry_action_id"],
    )
    records = await store.list_eval_results(
        service.workspace_root,
        request.batch_id,
        request.suite_id,
    )

    assert retry_authority is not None
    assert retry_authority.decision == "accepted"
    assert retry_authority.eval_request_sha256 == request.request_sha256
    assert retry_authority.execution_authority_key != request.request_sha256
    assert dispatch is not None
    assert dispatch.state == "completed"
    assert dispatch.ticket_id != cancel_receipt.ticket_id
    assert dispatch.execution_authority_key == retry_authority.execution_authority_key
    assert completed.persisted_samples == 5
    assert len(completed.run_grant_sha256) == 2
    assert [item.sample_index for item in records] == list(range(5))
    assert len(kernel.calls) == 6
    assert all(
        item.authority_key == request.request_sha256
        for item in kernel.calls
    )
    assert retry_checkpoints[0].stage == "admitted"
    assert retry_checkpoints[0].authority_key == (
        retry_authority.execution_authority_key
    )
    assert retry_checkpoints[-1].stage == "completed"
    assert retry_checkpoints[-1].admission_state == "completed"


@pytest.mark.asyncio
async def test_retry_permission_mismatch_is_rejected_before_consuming_intent(
    tmp_path: Path,
) -> None:
    service, store, _grant_authority, kernel, _parent = await _runtime(
        tmp_path,
        durable_admission=True,
    )
    action_id = f"hsar_{'d' * 24}"
    wrong_parent = await kernel.permission_store.issue(
        request_id="request-retry-wrong",
        session_id="session-1",
        run_id="run-1",
        call_id="call-retry-wrong",
        agent_name="main",
        tool_name="harness_eval_sandbox_retry",
        tool_family="harness_eval_execution",
        arguments={
            "cancel_receipt_id": f"hsacr_{'e' * 24}",
            "cancel_receipt_sha256": "e" * 64,
            "reason": "不同的理由",
            "retry_action_id": action_id,
            "run_id": "run-1",
        },
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.BYPASS,
        risk_level="medium",
        delegated_tool_names=("bash_run",),
        decided_at=NOW,
    )
    service._authorization_receipt_provider = lambda: wrong_parent

    with pytest.raises(HarnessSandboxEvalServiceError) as captured:
        await service.retry_sandbox(
            retry_action_id=action_id,
            cancel_receipt_id=f"hsacr_{'e' * 24}",
            cancel_receipt_sha256="e" * 64,
            reason="用户实际提交的理由",
        )

    assert captured.value.code == (
        "sandbox_eval_service_retry_parent_permission_invalid"
    )
    assert await store.get_sandbox_admission_retry(
        workspace_root=service.workspace_root,
        action_id=action_id,
    ) is None


@pytest.mark.asyncio
async def test_resume_reuses_existing_retry_receipt_after_dispatch_lease_expires(
    tmp_path: Path,
) -> None:
    service, store, _grant_authority, kernel, _parent = await _runtime(
        tmp_path,
        pause_once_at=2,
        durable_admission=True,
    )
    executor = service._sandbox_eval_executor
    assert executor is not None
    admission = executor.coordinator.admission
    checkpoints: list[HarnessSandboxBatchCheckpoint] = []

    async def capture(checkpoint: HarnessSandboxBatchCheckpoint) -> None:
        checkpoints.append(checkpoint)

    original_task = asyncio.create_task(
        service.eval_sandbox(
            check_ids=("unit",),
            samples=5,
            batch_id="batch-1",
            on_progress=capture,
        )
    )
    await asyncio.wait_for(kernel.pause_reached.wait(), timeout=2)
    live = checkpoints[-1]
    cancel_receipt, cancelled_ticket = await admission.cancel(
        action_id=f"hsac_{'1' * 24}",
        ticket_id=str(live.admission_ticket_id),
        authority_key=live.authority_key,
        epoch=int(live.admission_epoch or 0),
        expected_state="active",
        actor_id="test-user",
        reason="构造 durable retry crash recovery",
    )
    assert cancel_receipt.decision == "accepted"
    assert cancelled_ticket is not None
    with pytest.raises(asyncio.CancelledError):
        await original_task

    retry_receipt = await store.authorize_sandbox_admission_retry(
        workspace_root=service.workspace_root,
        action_id=f"hsar_{'2' * 24}",
        cancel_receipt_id=cancel_receipt.receipt_id,
        cancel_receipt_sha256=cancel_receipt.receipt_sha256,
        actor_id="original-process",
        reason="用户首次批准 retry",
        authority_token="3" * 32,
        now=NOW,
    )
    pending = await store.get_sandbox_retry_dispatch(
        workspace_root=service.workspace_root,
        retry_action_id=retry_receipt.action_id,
    )
    assert pending is not None
    assert pending.state == "pending"
    claimed, first_ticket = await store.claim_sandbox_retry_dispatch(
        workspace_root=service.workspace_root,
        retry_action_id=retry_receipt.action_id,
        retry_receipt_id=retry_receipt.receipt_id,
        retry_receipt_sha256=retry_receipt.receipt_sha256,
        ticket_id=f"hsadm_{'4' * 24}",
        owner_id="crashed-process",
        now=NOW,
        lease_seconds=2,
        max_active=admission.max_active,
        max_queued=admission.max_queued,
    )
    assert claimed.epoch == 1
    assert first_ticket.state == "active"

    live_resume_arguments = {
        "dispatch_id": claimed.dispatch_id,
        "retry_action_id": retry_receipt.action_id,
        "retry_receipt_id": retry_receipt.receipt_id,
        "retry_receipt_sha256": retry_receipt.receipt_sha256,
        "run_id": "run-resume-live",
    }
    live_resume_parent = await kernel.permission_store.issue(
        request_id="request-resume-live",
        session_id="session-live",
        run_id="run-resume-live",
        call_id="call-resume-live",
        agent_name="recovery-operator",
        tool_name="harness_eval_sandbox_resume",
        tool_family="harness_eval_execution",
        arguments=live_resume_arguments,
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.BYPASS,
        risk_level="medium",
        delegated_tool_names=("bash_run",),
        decided_at=NOW,
    )
    service._authorization_receipt_provider = lambda: live_resume_parent
    with pytest.raises(HarnessSandboxBatchError) as live_error:
        await service.resume_sandbox_retry(
            retry_action_id=retry_receipt.action_id,
            dispatch_id=claimed.dispatch_id,
            retry_receipt_id=retry_receipt.receipt_id,
            retry_receipt_sha256=retry_receipt.receipt_sha256,
        )
    assert live_error.value.code == "sandbox_batch_retry_dispatch_fenced"
    still_live = await store.get_sandbox_retry_dispatch(
        workspace_root=service.workspace_root,
        retry_action_id=retry_receipt.action_id,
    )
    assert still_live == claimed

    resumed_at = "2026-07-23T00:00:03+00:00"
    admission._now = lambda: resumed_at
    executor.now = lambda: resumed_at
    executor.coordinator.now = lambda: resumed_at
    kernel.validation_now = resumed_at
    resume_arguments = {
        "dispatch_id": claimed.dispatch_id,
        "retry_action_id": retry_receipt.action_id,
        "retry_receipt_id": retry_receipt.receipt_id,
        "retry_receipt_sha256": retry_receipt.receipt_sha256,
        "run_id": "run-resume-1",
    }
    resume_parent = await kernel.permission_store.issue(
        request_id="request-resume-1",
        session_id="session-2",
        run_id="run-resume-1",
        call_id="call-resume-1",
        agent_name="recovery-operator",
        tool_name="harness_eval_sandbox_resume",
        tool_family="harness_eval_execution",
        arguments=resume_arguments,
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.BYPASS,
        risk_level="medium",
        delegated_tool_names=("bash_run",),
        decided_at=resumed_at,
    )
    service._authorization_receipt_provider = lambda: resume_parent
    progress: list[HarnessSandboxBatchCheckpoint] = []

    async def capture_resume(checkpoint: HarnessSandboxBatchCheckpoint) -> None:
        progress.append(checkpoint)

    completed = await service.resume_sandbox_retry(
        retry_action_id=retry_receipt.action_id,
        dispatch_id=claimed.dispatch_id,
        retry_receipt_id=retry_receipt.receipt_id,
        retry_receipt_sha256=retry_receipt.receipt_sha256,
        on_progress=capture_resume,
    )

    restored_receipt = await store.get_sandbox_admission_retry(
        workspace_root=service.workspace_root,
        action_id=retry_receipt.action_id,
    )
    restored_dispatch = await store.get_sandbox_retry_dispatch(
        workspace_root=service.workspace_root,
        retry_action_id=retry_receipt.action_id,
    )
    request = await _request(service)
    records = await store.list_eval_results(
        service.workspace_root,
        request.batch_id,
        request.suite_id,
    )

    assert restored_receipt == retry_receipt
    assert restored_dispatch is not None
    assert restored_dispatch.state == "completed"
    assert restored_dispatch.epoch == 2
    assert restored_dispatch.ticket_id != first_ticket.ticket_id
    assert completed.persisted_samples == 5
    assert [item.sample_index for item in records] == list(range(5))
    assert progress[0].stage == "admitted"
    assert progress[0].persisted_samples == 2
    assert progress[-1].stage == "completed"


@pytest.mark.asyncio
async def test_retry_terminalizes_pending_dispatch_when_h5a_is_already_complete(
    tmp_path: Path,
) -> None:
    service, store, _grant_authority, kernel, _parent = await _runtime(
        tmp_path,
        durable_admission=True,
    )
    completed_original = await service.eval_sandbox(
        check_ids=("unit",),
        samples=5,
        batch_id="batch-1",
    )
    assert completed_original.persisted_samples == 5
    assert len(kernel.calls) == 5
    request = await _request(service)
    admission = service._sandbox_eval_executor.coordinator.admission
    source = await store.enqueue_sandbox_admission(
        workspace_root=service.workspace_root,
        ticket_id=f"hsadm_{'5' * 24}",
        authority_key=request.request_sha256,
        lane="sandbox",
        requested_samples=5,
        owner_id="complete-prefix-source",
        now=NOW,
        lease_seconds=30,
        max_active=admission.max_active,
        max_queued=admission.max_queued,
    )
    cancel, _ = await store.cancel_sandbox_admission(
        workspace_root=service.workspace_root,
        action_id=f"hsac_{'6' * 24}",
        ticket_id=source.ticket_id,
        authority_key=source.authority_key,
        epoch=source.epoch,
        expected_state=source.state,
        actor_id="complete-prefix-user",
        reason="验证完整 H5a 不重复执行",
        now=NOW,
    )
    retry_arguments = {
        "cancel_receipt_id": cancel.receipt_id,
        "cancel_receipt_sha256": cancel.receipt_sha256,
        "reason": "完整 H5a 仅结束 dispatch",
        "retry_action_id": f"hsar_{'7' * 24}",
        "run_id": "run-complete-prefix",
    }
    retry_parent = await kernel.permission_store.issue(
        request_id="request-complete-prefix",
        session_id="session-complete-prefix",
        run_id="run-complete-prefix",
        call_id="call-complete-prefix",
        agent_name="main",
        tool_name="harness_eval_sandbox_retry",
        tool_family="harness_eval_execution",
        arguments=retry_arguments,
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.BYPASS,
        risk_level="medium",
        delegated_tool_names=("bash_run",),
        decided_at=NOW,
    )
    service._authorization_receipt_provider = lambda: retry_parent

    completed_retry = await service.retry_sandbox(
        retry_action_id=retry_arguments["retry_action_id"],
        cancel_receipt_id=retry_arguments["cancel_receipt_id"],
        cancel_receipt_sha256=retry_arguments["cancel_receipt_sha256"],
        reason=retry_arguments["reason"],
    )
    dispatch = await store.get_sandbox_retry_dispatch(
        workspace_root=service.workspace_root,
        retry_action_id=retry_arguments["retry_action_id"],
    )

    assert completed_retry == completed_original
    assert len(kernel.calls) == 5
    assert dispatch is not None
    assert dispatch.state == "completed"
    assert dispatch.epoch == 1
    assert dispatch.ticket_id
