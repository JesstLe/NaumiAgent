from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from naumi_agent.daemons.execution_grants import (
    ExecutionGrantAuthority,
    ExecutionGrantRequest,
    ExecutionGrantSource,
    ExecutionGrantStore,
)
from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.daemons.tool_jobs import (
    TOOL_JOB_SCHEMA_VERSION,
    ToolJobAuthority,
    ToolJobConflictError,
    ToolJobError,
    ToolJobRequest,
    ToolJobState,
    ToolJobStore,
    ToolJobValidationReason,
)
from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionRequirements,
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
    WorkerResourceEnvelope,
    detect_worker_platform,
    issue_worker_contract,
    issue_worker_health_report,
)
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode

T0 = "2026-07-19T00:00:00+00:00"
T1 = "2026-07-19T00:00:01+00:00"
T2 = "2026-07-19T00:00:02+00:00"
T3 = "2026-07-19T00:00:03+00:00"


def _isolation() -> WorkerIsolationContract:
    return WorkerIsolationContract(True, True, True, True, True, True)


def _capabilities() -> tuple[WorkerCapability, ...]:
    return tuple(
        sorted(
            (
                WorkerCapability.ARTIFACT_DIGEST,
                WorkerCapability.ENVIRONMENT_ALLOWLIST,
                WorkerCapability.NETWORK_POLICY,
                WorkerCapability.PROCESS_TREE_CANCEL,
                WorkerCapability.RESOURCE_LIMITS,
                WorkerCapability.SHELL_NON_PTY,
                WorkerCapability.WORKSPACE_EPHEMERAL,
            ),
            key=str,
        )
    )


def _contract(epoch: int = 1):
    return issue_worker_contract(
        worker_id="tool-worker-a",
        instance_id=f"process-{epoch}",
        epoch=epoch,
        kind=WorkerKind.TOOL,
        protocol_min=1,
        protocol_max=1,
        software_version="0.1.214",
        platform=detect_worker_platform(
            system="Linux",
            machine="x86_64",
            python_implementation="CPython",
            python_version="3.13.5",
        ),
        capabilities=_capabilities(),
        resources=WorkerResourceEnvelope(
            max_concurrent_jobs=2,
            max_memory_bytes=512 * 1024 * 1024,
            max_cpu_seconds=60,
            max_wall_seconds=120,
            max_output_bytes=8 * 1024 * 1024,
        ),
        isolation=_isolation(),
        issued_at=T0 if epoch == 1 else T3,
    )


def _requirements() -> WorkerAdmissionRequirements:
    return WorkerAdmissionRequirements(
        kind=WorkerKind.TOOL,
        protocol_version=1,
        capabilities=_capabilities(),
        allowed_platforms=("linux",),
        min_memory_bytes=256 * 1024 * 1024,
        min_cpu_seconds=30,
        min_wall_seconds=60,
        min_output_bytes=1024 * 1024,
        isolation=_isolation(),
    )


def _health(contract, *, active_jobs: int = 0, accepting_jobs: bool = True):
    heartbeat = HarnessHeartbeat(
        workspace_root="/workspace",
        subject_kind=HarnessRunKind.TOOL,
        subject_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=T2,
        timeout_seconds=30,
        detail_code="ready",
    )
    return issue_worker_health_report(
        contract=contract,
        heartbeat=heartbeat,
        active_jobs=active_jobs,
        accepting_jobs=accepting_jobs,
    )


async def _authority(tmp_path: Path, *, arguments=None):
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    registry = WorkerRegistryStore(runtime / "worker-registry.db")
    harness = HarnessStore(tmp_path / "state" / "harness.db")
    permission_store = PermissionDecisionReceiptStore(
        runtime / "permission-decisions.db"
    )
    grant_store = ExecutionGrantStore(runtime / "execution-grants.db")
    job_store = ToolJobStore(runtime / "tool-jobs.db")
    contract = _contract()
    await registry.register(contract, registered_at=T1)
    lease = await harness.acquire_run_lease(
        workspace_root=workspace,
        run_kind=HarnessRunKind.TOOL,
        run_id="tool-run-a",
        owner_id=contract.instance_id,
        now=T1,
        lease_seconds=60,
    )
    assert lease is not None
    args = arguments or {"command": "printf safe", "cwd": "/workspace"}
    receipt = await permission_store.issue(
        request_id="call-a",
        session_id="session-a",
        run_id="tool-run-a",
        call_id="call-a",
        agent_name="main",
        tool_name="bash_run",
        tool_family="shell",
        arguments=args,
        outcome=PermissionDecisionOutcome.BYPASS_ENABLED,
        actor=PermissionDecisionActor.USER,
        source=PermissionDecisionSource.BYPASS,
        permission_mode=PermissionMode.BYPASS,
        risk_level="high",
        decided_at=T2,
    )
    grant_request = ExecutionGrantRequest(
        session_id="session-a",
        run_id="tool-run-a",
        call_id="call-a",
        tool_name="bash_run",
        arguments=args,
        idempotency_key="job-key-a",
        worker_id=contract.worker_id,
        authorization_reference=receipt.receipt_id,
    )
    grant_authority = ExecutionGrantAuthority(
        store=grant_store,
        worker_registry=registry,
        harness_store=harness,
        permission_decision_store=permission_store,
        workspace_root=workspace,
    )
    grant = await grant_authority.issue(
        grant_request,
        decision=PermissionChecker(PermissionMode.BYPASS).check("bash_run", args),
        permission_mode=PermissionMode.BYPASS,
        source=ExecutionGrantSource.BYPASS,
        now=T2,
        ttl_seconds=30,
    )
    request = ToolJobRequest(
        session_id=grant_request.session_id,
        run_id=grant_request.run_id,
        call_id=grant_request.call_id,
        tool_name=grant_request.tool_name,
        arguments=grant_request.arguments,
        idempotency_key=grant_request.idempotency_key,
        worker_id=grant_request.worker_id,
        authorization_reference=grant_request.authorization_reference,
        execution_grant_id=grant.contract.grant_id,
    )
    authority = ToolJobAuthority(
        store=job_store,
        execution_grants=grant_authority,
        worker_registry=registry,
    )
    return (
        authority,
        request,
        job_store,
        grant_store,
        registry,
        harness,
        permission_store,
        contract,
        lease,
    )


@pytest.mark.asyncio
async def test_admit_reopen_validate_and_never_persist_raw_arguments(
    tmp_path: Path,
) -> None:
    secret = "super-secret-token"
    authority, request, store, *_, contract, lease = await _authority(
        tmp_path,
        arguments={"command": f"printf {secret}"},
    )

    admitted = await authority.admit(
        request,
        worker_health=_health(contract),
        requirements=_requirements(),
        now=T3,
    )
    reopened = await ToolJobStore(store.db_path).get(admitted.contract.job_id)
    validation = await authority.validate_for_dispatch(
        job_id=admitted.contract.job_id,
        request=request,
        worker_health=_health(contract),
        requirements=_requirements(),
        now="2026-07-19T00:00:04+00:00",
    )

    assert reopened == admitted
    assert admitted.state is ToolJobState.ADMITTED
    assert admitted.contract.worker_epoch == contract.epoch
    assert admitted.contract.lease_epoch == lease.epoch
    assert admitted.contract.expires_at == "2026-07-19T00:00:32+00:00"
    assert validation.allowed
    assert validation.reasons == (ToolJobValidationReason.VALID,)
    assert secret.encode() not in store.db_path.read_bytes()
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    if os.name != "nt":
        assert store.db_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_concurrent_retry_converges_on_one_immutable_job(tmp_path: Path) -> None:
    authority, request, store, *_, contract, _ = await _authority(tmp_path)
    reopened_authority = ToolJobAuthority(
        store=ToolJobStore(store.db_path),
        execution_grants=authority._execution_grants,
        worker_registry=authority._worker_registry,
    )

    admitted = await asyncio.gather(
        *(
            (authority if index % 2 == 0 else reopened_authority).admit(
                request,
                worker_health=_health(contract),
                requirements=_requirements(),
                now=T3,
            )
            for index in range(8)
        )
    )

    assert len({item.contract.job_id for item in admitted}) == 1
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM tool_jobs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_admission_rejects_unhealthy_capacity_and_insufficient_isolation(
    tmp_path: Path,
) -> None:
    authority, request, *_, contract, _ = await _authority(tmp_path)

    with pytest.raises(ToolJobConflictError, match="capacity_exhausted"):
        await authority.admit(
            request,
            worker_health=_health(contract, active_jobs=2),
            requirements=_requirements(),
            now=T3,
        )
    impossible = replace(
        _requirements(),
        min_memory_bytes=1024 * 1024 * 1024,
    )
    with pytest.raises(ToolJobConflictError, match="resource_insufficient"):
        await authority.admit(
            request,
            worker_health=_health(contract),
            requirements=impossible,
            now=T3,
        )


@pytest.mark.asyncio
async def test_dispatch_revalidation_detects_request_grant_worker_and_requirement_changes(
    tmp_path: Path,
) -> None:
    authority, request, _, grant_store, registry, _, _, contract, _ = await _authority(
        tmp_path
    )
    admitted = await authority.admit(
        request,
        worker_health=_health(contract),
        requirements=_requirements(),
        now=T3,
    )

    changed = await authority.validate_for_dispatch(
        job_id=admitted.contract.job_id,
        request=replace(request, arguments={"command": "printf changed"}),
        worker_health=_health(contract),
        requirements=replace(_requirements(), min_output_bytes=2 * 1024 * 1024),
        now="2026-07-19T00:00:04+00:00",
    )
    await grant_store.revoke(
        grant_id=request.execution_grant_id,
        reason="operator_stop",
        revoked_at="2026-07-19T00:00:05+00:00",
    )
    revoked = await authority.validate_for_dispatch(
        job_id=admitted.contract.job_id,
        request=request,
        worker_health=_health(contract),
        requirements=_requirements(),
        now="2026-07-19T00:00:06+00:00",
    )
    takeover = _contract(2)
    await registry.register(takeover, registered_at="2026-07-19T00:00:07+00:00")
    fenced = await authority.validate_for_dispatch(
        job_id=admitted.contract.job_id,
        request=request,
        worker_health=_health(contract),
        requirements=_requirements(),
        now="2026-07-19T00:00:08+00:00",
    )
    invalid_requirements = await authority.validate_for_dispatch(
        job_id=admitted.contract.job_id,
        request=request,
        worker_health=_health(takeover),
        requirements=replace(_requirements(), kind=WorkerKind.BROWSER),
        now="2026-07-19T00:00:09+00:00",
    )

    assert ToolJobValidationReason.REQUEST_MISMATCH in changed.reasons
    assert ToolJobValidationReason.REQUIREMENTS_MISMATCH in changed.reasons
    assert ToolJobValidationReason.EXECUTION_GRANT_INVALID in changed.reasons
    assert ToolJobValidationReason.EXECUTION_GRANT_INVALID in revoked.reasons
    assert ToolJobValidationReason.WORKER_NOT_ADMITTED in fenced.reasons
    assert (
        ToolJobValidationReason.REQUIREMENTS_MISMATCH
        in invalid_requirements.reasons
    )
    assert ToolJobValidationReason.WORKER_NOT_ADMITTED in invalid_requirements.reasons


@pytest.mark.asyncio
async def test_store_rejects_tamper_future_schema_wrong_type_and_non_tool_requirements(
    tmp_path: Path,
) -> None:
    authority, request, store, *_, contract, _ = await _authority(tmp_path)
    admitted = await authority.admit(
        request,
        worker_health=_health(contract),
        requirements=_requirements(),
        now=T3,
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE tool_jobs SET contract_json = '{}' WHERE job_id = ?",
            (admitted.contract.job_id,),
        )
        db.commit()
    with pytest.raises(ToolJobError, match="无法读取"):
        await ToolJobStore(store.db_path).get(admitted.contract.job_id)

    future = tmp_path / "future.db"
    with sqlite3.connect(future) as db:
        db.execute(f"PRAGMA user_version = {TOOL_JOB_SCHEMA_VERSION + 1}")
    with pytest.raises(ToolJobError, match="不受支持"):
        await ToolJobStore(future).get("a" * 32)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ToolJobError, match="不是文件"):
        await ToolJobStore(directory).get("a" * 32)

    browser_requirements = replace(_requirements(), kind=WorkerKind.BROWSER)
    with pytest.raises(ValueError, match="必须为 tool"):
        await authority.admit(
            request,
            worker_health=_health(contract),
            requirements=browser_requirements,
            now=T3,
        )


def test_store_is_lazy_and_rejects_relative_path(tmp_path: Path) -> None:
    path = tmp_path / "tool-jobs.db"
    ToolJobStore(path)
    assert not path.exists()
    with pytest.raises(ValueError, match="绝对路径"):
        ToolJobStore("relative.db")
