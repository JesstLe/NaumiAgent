from __future__ import annotations

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
    ) -> None:
        self.workspace_root = workspace
        self.permission_store = permission_store
        self.run_grant_authority = run_grant_authority
        self.fail_once_at = fail_once_at
        self.drift_after_sample = drift_after_sample
        self.wrong_source_at = wrong_source_at
        self.failed = False
        self.calls: list[SimpleNamespace] = []

    async def execute(self, **kwargs):
        sample_index = kwargs["sample_index"]
        authority = kwargs["run_authority"]
        validation = await self.run_grant_authority.validate(
            grant_id=authority.grant_id,
            now=NOW,
        )
        assert validation.allowed
        assert await kwargs["profile_is_current"]()
        self.calls.append(SimpleNamespace(**kwargs))
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
    )
    executor = HarnessSandboxEvalExecutor(
        workspace_root=workspace,
        store=harness_store,
        permission_store=permission_store,
        run_grant_authority=grant_authority,
        execution_kernel=kernel,  # type: ignore[arg-type]
        admission=HarnessSandboxBatchAdmission(max_active=1, max_queued=1),
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
    partial = await store.list_eval_results(
        service.workspace_root,
        "batch-1",
        request.suite_id,
    )
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
