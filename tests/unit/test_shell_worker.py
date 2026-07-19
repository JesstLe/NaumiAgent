from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
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
from naumi_agent.daemons.shell_admission import ShellWorkerAdmissionComposer
from naumi_agent.daemons.shell_worker import (
    AuthenticatedLocalShellTransport,
    ShellCommandRequest,
    ShellCommandSpec,
    ShellSandboxBackend,
    ShellSandboxUnavailableError,
    ShellWorkerCoordinator,
    ShellWorkerStatus,
    detect_shell_sandbox_backend,
)
from naumi_agent.daemons.tool_jobs import (
    ToolJobAuthority,
    ToolJobLifecycleAuthority,
    ToolJobRequest,
    ToolJobState,
    ToolJobStore,
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
from naumi_agent.daemons.worker_registry import (
    WorkerRegistrationState,
    WorkerRegistryStore,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.sandbox_checks import (
    AdmittedSandboxShellJob,
    HarnessSandboxCheckRunner,
    HarnessSandboxCheckStatus,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode

T0 = "2026-07-19T00:00:00+00:00"
T1 = "2026-07-19T00:00:01+00:00"
T2 = "2026-07-19T00:00:02+00:00"
T3 = "2026-07-19T00:00:03+00:00"
T4 = "2026-07-19T00:00:04+00:00"
T5 = "2026-07-19T00:00:05+00:00"
T6 = "2026-07-19T00:00:06+00:00"


def _request(
    tmp_path: Path,
    *,
    code: str,
    job_id: str = "job-a",
    artifact_name: str = "job-a.log",
    timeout_seconds: float = 5,
    max_output_bytes: int = 1024 * 1024,
) -> ShellCommandRequest:
    workspace = tmp_path / "workspace"
    artifact = tmp_path / "artifacts"
    workspace.mkdir(parents=True, exist_ok=True)
    artifact.mkdir(parents=True, exist_ok=True)
    manifest = workspace / ".naumi-sandbox-manifest.json"
    manifest.write_text('{"schema_version":1}', encoding="utf-8")
    spec = ShellCommandSpec(
        argv=(sys.executable, "-c", code),
        workspace_root=str(workspace.resolve()),
        workspace_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        cwd_relative=".",
        artifact_root=str(artifact.resolve()),
        artifact_name=artifact_name,
        environment=(),
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        max_memory_bytes=4 * 1024 * 1024 * 1024,
        max_cpu_seconds=10,
    )
    return ShellCommandRequest(
        job_id=job_id,
        worker_id="tool-worker-a",
        worker_instance_id="process-a",
        worker_epoch=1,
        worker_contract_sha256="a" * 64,
        spec=spec,
    )


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


def _worker_contract():
    return issue_worker_contract(
        worker_id="tool-worker-a",
        instance_id="process-a",
        epoch=1,
        kind=WorkerKind.TOOL,
        protocol_min=1,
        protocol_max=1,
        software_version="0.1.214",
        platform=detect_worker_platform(),
        capabilities=_capabilities(),
        resources=WorkerResourceEnvelope(
            max_concurrent_jobs=2,
            max_memory_bytes=4 * 1024 * 1024 * 1024,
            max_cpu_seconds=60,
            max_wall_seconds=120,
            max_output_bytes=8 * 1024 * 1024,
        ),
        isolation=_isolation(),
        issued_at=T0,
    )


def _requirements(contract) -> WorkerAdmissionRequirements:
    return WorkerAdmissionRequirements(
        kind=WorkerKind.TOOL,
        protocol_version=1,
        capabilities=_capabilities(),
        allowed_platforms=(contract.platform.system,),
        min_memory_bytes=256 * 1024 * 1024,
        min_cpu_seconds=30,
        min_wall_seconds=60,
        min_output_bytes=1024 * 1024,
        isolation=_isolation(),
    )


def _health(contract):
    return issue_worker_health_report(
        contract=contract,
        heartbeat=HarnessHeartbeat(
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
        ),
        active_jobs=0,
        accepting_jobs=True,
    )


async def _admitted_shell_job(tmp_path: Path, spec: ShellCommandSpec):
    runtime = tmp_path / "authority"
    workspace = Path(spec.workspace_root)
    registry = WorkerRegistryStore(runtime / "worker-registry.db")
    harness = HarnessStore(tmp_path / "state" / "harness.db")
    permission_store = PermissionDecisionReceiptStore(
        runtime / "permission-decisions.db"
    )
    grant_store = ExecutionGrantStore(runtime / "execution-grants.db")
    job_store = ToolJobStore(runtime / "tool-jobs.db")
    contract = _worker_contract()
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
    arguments = spec.canonical_payload()
    receipt = await permission_store.issue(
        request_id="call-a",
        session_id="session-a",
        run_id="tool-run-a",
        call_id="call-a",
        agent_name="main",
        tool_name="bash_run",
        tool_family="shell",
        arguments=arguments,
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
        arguments=arguments,
        idempotency_key="job-key-a",
        worker_id=contract.worker_id,
        authorization_reference=receipt.receipt_id,
    )
    grants = ExecutionGrantAuthority(
        store=grant_store,
        worker_registry=registry,
        harness_store=harness,
        permission_decision_store=permission_store,
        workspace_root=workspace,
    )
    grant = await grants.issue(
        grant_request,
        decision=PermissionChecker(PermissionMode.BYPASS).check(
            "bash_run", arguments
        ),
        permission_mode=PermissionMode.BYPASS,
        source=ExecutionGrantSource.BYPASS,
        now=T2,
        ttl_seconds=30,
    )
    tool_request = ToolJobRequest(
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
    jobs = ToolJobAuthority(
        store=job_store,
        execution_grants=grants,
        worker_registry=registry,
    )
    admitted = await jobs.admit(
        tool_request,
        worker_health=_health(contract),
        requirements=_requirements(contract),
        now=T3,
    )
    return admitted, tool_request, jobs, job_store, registry, contract


def _require_real_backend() -> ShellSandboxBackend:
    try:
        return detect_shell_sandbox_backend()
    except ShellSandboxUnavailableError as exc:
        pytest.skip(str(exc))


@pytest.mark.asyncio
async def test_authenticated_worker_runs_non_pty_and_persists_artifact(
    tmp_path: Path,
) -> None:
    backend = _require_real_backend()
    request = _request(
        tmp_path,
        code="from pathlib import Path; Path('inside.txt').write_text('ok'); print('done')",
    )
    transport = AuthenticatedLocalShellTransport(
        runtime_dir=tmp_path / "runtime",
    )
    started = 0

    async def on_started() -> None:
        nonlocal started
        started += 1

    result = await transport.execute(request, on_started=on_started)

    assert started == 1
    assert result.status is ShellWorkerStatus.PASSED
    assert result.exit_code == 0
    assert result.output_tail.strip() == "done"
    assert result.artifact_path.read_text().strip() == "done"
    assert result.output_bytes == len(b"done\n")
    assert result.sandbox_backend is backend
    assert (tmp_path / "workspace" / "inside.txt").read_text() == "ok"
    assert not list((tmp_path / "runtime").glob("*.sock"))
    if os.name != "nt":
        assert (tmp_path / "runtime").stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_coordinator_consumes_tool_job_authority_and_reuses_terminal_receipt(
    tmp_path: Path,
) -> None:
    _require_real_backend()
    provisional = _request(
        tmp_path,
        code="from pathlib import Path; Path('verified.txt').write_text('yes'); print('ok')",
    )
    admitted, tool_request, jobs, store, registry, contract = (
        await _admitted_shell_job(tmp_path, provisional.spec)
    )
    shell_request = ShellCommandRequest(
        job_id=admitted.contract.job_id,
        worker_id=contract.worker_id,
        worker_instance_id=contract.instance_id,
        worker_epoch=contract.epoch,
        worker_contract_sha256=contract.contract_sha256,
        spec=provisional.spec,
    )
    timestamps = iter((T4, T5, T6))
    coordinator = ShellWorkerCoordinator(
        jobs=jobs,
        lifecycle=ToolJobLifecycleAuthority(store, registry),
        worker_registry=registry,
        transport=AuthenticatedLocalShellTransport(
            runtime_dir=tmp_path / "transport"
        ),
        now=lambda: next(timestamps),
    )

    result = await coordinator.execute(
        job_id=admitted.contract.job_id,
        tool_job_request=tool_request,
        shell_request=shell_request,
        worker_health=_health(contract),
        requirements=_requirements(contract),
        dispatch_id="dispatch-a",
    )
    replay = await coordinator.execute(
        job_id=admitted.contract.job_id,
        tool_job_request=tool_request,
        shell_request=shell_request,
        worker_health=_health(contract),
        requirements=_requirements(contract),
        dispatch_id="dispatch-a",
    )

    assert result.payload_sent
    assert not result.reconcile_required
    assert result.command is not None
    assert result.command.status is ShellWorkerStatus.PASSED
    assert result.job.state is ToolJobState.SUCCEEDED
    assert result.job.latest_receipt.sequence == 4
    assert (Path(provisional.spec.workspace_root) / "verified.txt").read_text() == "yes"
    assert not replay.payload_sent
    assert replay.command is None
    assert replay.job == result.job


@pytest.mark.asyncio
async def test_admission_composer_builds_and_releases_exact_authority_chain(
    tmp_path: Path,
) -> None:
    provisional = _request(tmp_path, code="print('admitted')")
    runtime = tmp_path / "authority"
    registry = WorkerRegistryStore(runtime / "worker-registry.db")
    harness = HarnessStore(tmp_path / "state" / "harness.db")
    permissions = PermissionDecisionReceiptStore(runtime / "permission-decisions.db")
    grants = ExecutionGrantStore(runtime / "execution-grants.db")
    jobs = ToolJobStore(runtime / "tool-jobs.db")
    parent = await permissions.issue(
        request_id="parent-check",
        session_id="session-a",
        run_id="tool-run-a",
        call_id="parent-check",
        agent_name="main",
        tool_name="harness_run_check",
        tool_family="harness_run_check",
        arguments={"check_id": "unit", "run_id": "tool-run-a"},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="medium",
        delegated_tool_names=("bash_run",),
        decided_at=T2,
    )
    timestamps = iter((T3, T4))
    composer = ShellWorkerAdmissionComposer(
        worker_registry=registry,
        harness_store=harness,
        permission_store=permissions,
        execution_grant_store=grants,
        tool_job_store=jobs,
        transport=AuthenticatedLocalShellTransport(runtime_dir=runtime / "transport"),
        software_version="0.1.214",
        now=lambda: next(timestamps),
        token=lambda: "a" * 32,
    )

    composed = await composer.compose(
        parent_receipt_id=parent.receipt_id,
        spec=provisional.spec,
    )

    admitted = composed.admitted
    stored = await jobs.get(admitted.job_id)
    active = await registry.get_active(admitted.shell_request.worker_id)
    lease = await harness.get_run_lease(
        workspace_root=provisional.workspace_root,
        run_kind=HarnessRunKind.TOOL,
        run_id=parent.run_id,
    )
    assert stored is not None and stored.state is ToolJobState.ADMITTED
    assert active is not None and active.state is WorkerRegistrationState.ACTIVE
    assert lease is not None and lease.state is HarnessRunLeaseState.ACTIVE
    assert admitted.tool_job_request.arguments == provisional.spec.canonical_payload()
    assert admitted.shell_request.spec == provisional.spec

    await composed.release()
    await composed.release()

    assert await registry.get_active(admitted.shell_request.worker_id) is None
    released = await harness.get_run_lease(
        workspace_root=provisional.workspace_root,
        run_kind=HarnessRunKind.TOOL,
        run_id=parent.run_id,
    )
    assert released is not None and released.state is HarnessRunLeaseState.RELEASED


@pytest.mark.asyncio
async def test_harness_profile_check_runs_in_ephemeral_worker_snapshot(
    tmp_path: Path,
) -> None:
    _require_real_backend()
    workspace = tmp_path / "source-workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Harness Tests"],
        cwd=workspace,
        check=True,
    )
    (workspace / "source.py").write_text("VALUE = 7\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture"],
        cwd=workspace,
        check=True,
    )
    check = HarnessCheckSpec(
        id="compile",
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "assert 'VALUE = 7' in Path('source.py').read_text(); "
            "Path('sandbox-only.txt').write_text('ok'); print('profile check passed')",
        ),
        timeout_seconds=10,
        required_for=("change",),
        provides=("compile",),
    )
    profile_digest = hashlib.sha256(b"trusted-profile").hexdigest()
    runner = HarnessSandboxCheckRunner(
        workspace_root=workspace,
        sandbox_root=(tmp_path / "sandboxes").resolve(),
        artifact_root=(tmp_path / "sandbox-artifacts").resolve(),
    )

    async def profile_is_current() -> bool:
        return True

    async def admit_job(spec: ShellCommandSpec) -> AdmittedSandboxShellJob:
        admitted, tool_request, jobs, store, registry, contract = (
            await _admitted_shell_job(tmp_path / "sandbox-admission", spec)
        )
        shell_request = ShellCommandRequest(
            job_id=admitted.contract.job_id,
            worker_id=contract.worker_id,
            worker_instance_id=contract.instance_id,
            worker_epoch=contract.epoch,
            worker_contract_sha256=contract.contract_sha256,
            spec=spec,
        )
        timestamps = iter((T4, T5, T6))
        coordinator = ShellWorkerCoordinator(
            jobs=jobs,
            lifecycle=ToolJobLifecycleAuthority(store, registry),
            worker_registry=registry,
            transport=AuthenticatedLocalShellTransport(
                runtime_dir=tmp_path / "sandbox-transport"
            ),
            now=lambda: next(timestamps),
        )
        return AdmittedSandboxShellJob(
            job_id=admitted.contract.job_id,
            tool_job_request=tool_request,
            shell_request=shell_request,
            worker_health=_health(contract),
            requirements=_requirements(contract),
            dispatch_id="dispatch-sandbox",
            coordinator=coordinator,
        )

    result = await runner.run(
        run_id="sandbox-run-a",
        check=check,
        profile_digest=profile_digest,
        profile_is_current=profile_is_current,
        admit_job=admit_job,
    )

    assert result.status is HarnessSandboxCheckStatus.PASSED
    assert result.job_id is not None
    assert result.lifecycle_receipt_sha256 is not None
    assert result.artifact_path is not None and result.artifact_path.is_file()
    assert "profile check passed" in result.output
    assert not (workspace / "sandbox-only.txt").exists()
    assert not tuple((tmp_path / "sandboxes").iterdir())


@pytest.mark.asyncio
async def test_harness_sandbox_blocks_untrusted_profile_and_sensitive_snapshot(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Harness Tests"],
        cwd=workspace,
        check=True,
    )
    (workspace / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / ".env").write_text("API_KEY=must-not-copy\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture"],
        cwd=workspace,
        check=True,
    )
    sandbox_root = (tmp_path / "sandboxes").resolve()
    artifact_root = (tmp_path / "artifacts").resolve()
    runner = HarnessSandboxCheckRunner(
        workspace_root=workspace,
        sandbox_root=sandbox_root,
        artifact_root=artifact_root,
    )
    check = HarnessCheckSpec(
        id="compile",
        argv=(sys.executable, "-c", "print('must not run')"),
    )
    digest = hashlib.sha256(b"profile").hexdigest()
    admitted = False

    async def reject_admission(_spec: ShellCommandSpec) -> AdmittedSandboxShellJob:
        nonlocal admitted
        admitted = True
        raise AssertionError("admission must not run")

    async def untrusted() -> bool:
        return False

    blocked = await runner.run(
        run_id="untrusted-run",
        check=check,
        profile_digest=digest,
        profile_is_current=untrusted,
        admit_job=reject_admission,
    )

    assert blocked.status is HarnessSandboxCheckStatus.BLOCKED
    assert not admitted
    assert not sandbox_root.exists()
    assert not artifact_root.exists()

    async def trusted() -> bool:
        return True

    sensitive = await runner.run(
        run_id="sensitive-run",
        check=check,
        profile_digest=digest,
        profile_is_current=trusted,
        admit_job=reject_admission,
    )

    assert sensitive.status is HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR
    assert "敏感路径" in sensitive.message
    assert not admitted
    assert not tuple(sandbox_root.iterdir())


@pytest.mark.asyncio
async def test_worker_denies_host_write_and_network(tmp_path: Path) -> None:
    _require_real_backend()
    outside = tmp_path / "outside.txt"
    write_request = _request(
        tmp_path / "write",
        code=f"from pathlib import Path; Path({str(outside)!r}).write_text('bad')",
    )
    write_result = await AuthenticatedLocalShellTransport(
        runtime_dir=tmp_path / "runtime-write"
    ).execute(write_request, on_started=_noop_started)

    network_code = (
        "import socket; from pathlib import Path; "
        f"\ntry: list(Path({str(Path.home())!r}).iterdir())"
        "\nexcept PermissionError: print('home denied')"
        "\nelse: raise SystemExit('home unexpectedly readable')"
        "\ns=socket.socket(); "
        "\ntry: s.connect(('127.0.0.1', 9))"
        "\nexcept PermissionError: print('network denied')"
        "\nelse: raise SystemExit('network unexpectedly available')"
    )
    network_request = _request(
        tmp_path / "network",
        code=network_code,
    )
    network_result = await AuthenticatedLocalShellTransport(
        runtime_dir=tmp_path / "runtime-network"
    ).execute(network_request, on_started=_noop_started)

    assert write_result.status is ShellWorkerStatus.FAILED
    assert not outside.exists()
    assert network_result.status is ShellWorkerStatus.PASSED, network_result.output_tail
    assert "home denied" in network_result.output_tail
    assert "network denied" in network_result.output_tail


@pytest.mark.asyncio
async def test_cancel_kills_worker_process_tree(tmp_path: Path) -> None:
    _require_real_backend()
    marker = tmp_path / "workspace" / "grandchild-survived"
    child = (
        "import time; from pathlib import Path; "
        f"time.sleep(0.8); Path({str(marker)!r}).write_text('bad')"
    )
    code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    )
    request = _request(tmp_path, code=code, timeout_seconds=30)
    cancel = asyncio.Event()
    transport = AuthenticatedLocalShellTransport(
        runtime_dir=tmp_path / "runtime",
        terminate_grace_seconds=0.1,
    )

    async def on_started() -> None:
        asyncio.get_running_loop().call_later(0.2, cancel.set)

    result = await transport.execute(
        request,
        on_started=on_started,
        cancel_event=cancel,
    )
    await asyncio.sleep(1.0)

    assert result.status is ShellWorkerStatus.CANCELLED
    assert not marker.exists()


@pytest.mark.asyncio
async def test_output_and_wall_limits_are_enforced(tmp_path: Path) -> None:
    _require_real_backend()
    output_request = _request(
        tmp_path / "output",
        code="import sys, time; [print('x'*4096) for _ in range(10000)]; time.sleep(2)",
        max_output_bytes=4096,
    )
    output_result = await AuthenticatedLocalShellTransport(
        runtime_dir=tmp_path / "runtime-output"
    ).execute(output_request, on_started=_noop_started)

    timeout_request = _request(
        tmp_path / "timeout",
        code="import time; time.sleep(30)",
        timeout_seconds=0.2,
    )
    timeout_result = await AuthenticatedLocalShellTransport(
        runtime_dir=tmp_path / "runtime-timeout"
    ).execute(timeout_request, on_started=_noop_started)

    memory_base = _request(
        tmp_path / "memory",
        code="import time; value=bytearray(256*1024*1024); time.sleep(30)",
        timeout_seconds=5,
    )
    memory_request = replace(
        memory_base,
        spec=replace(memory_base.spec, max_memory_bytes=64 * 1024 * 1024),
    )
    memory_result = await AuthenticatedLocalShellTransport(
        runtime_dir=tmp_path / "runtime-memory"
    ).execute(memory_request, on_started=_noop_started)

    assert output_result.status is ShellWorkerStatus.OUTPUT_LIMIT
    assert output_result.output_bytes == output_request.max_output_bytes
    assert output_result.artifact_path.stat().st_size == output_request.max_output_bytes
    assert timeout_result.status is ShellWorkerStatus.TIMED_OUT
    assert memory_result.status is ShellWorkerStatus.RESOURCE_LIMIT


def test_request_rejects_escape_secret_env_and_relative_executable(
    tmp_path: Path,
) -> None:
    base = _request(tmp_path, code="print('ok')")

    with pytest.raises(ValueError, match="cwd_relative"):
        replace(base, spec=replace(base.spec, cwd_relative="../outside"))
    with pytest.raises(ValueError, match="secret"):
        replace(
            base,
            spec=replace(base.spec, environment=(("API_KEY", "do-not-leak"),)),
        )
    with pytest.raises(ValueError, match="绝对可执行文件"):
        replace(base, spec=replace(base.spec, argv=("python3", "-V")))


async def _noop_started() -> None:
    return None
