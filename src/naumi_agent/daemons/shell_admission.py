"""Compose one exact delegated authorization into an admitted Shell ToolJob."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from naumi_agent.daemons.execution_grants import (
    ExecutionGrantAuthority,
    ExecutionGrantRequest,
    ExecutionGrantSource,
    ExecutionGrantStore,
)
from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import RunDelegationGrantAuthority
from naumi_agent.daemons.shell_worker import (
    AuthenticatedLocalShellTransport,
    ShellCommandRequest,
    ShellCommandSpec,
    ShellWorkerCoordinator,
)
from naumi_agent.daemons.tool_jobs import (
    ToolJobAuthority,
    ToolJobLifecycleAuthority,
    ToolJobRequest,
    ToolJobStore,
)
from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionRequirements,
    WorkerCapability,
    WorkerContract,
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
from naumi_agent.harness.sandbox_checks import AdmittedSandboxShellJob
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import (
    PermissionDecision,
    PermissionOutcome,
)

NowProvider = Callable[[], str]
TokenProvider = Callable[[], str]

_CAPABILITIES = (
    WorkerCapability.ARTIFACT_DIGEST,
    WorkerCapability.ENVIRONMENT_ALLOWLIST,
    WorkerCapability.NETWORK_POLICY,
    WorkerCapability.PROCESS_TREE_CANCEL,
    WorkerCapability.RESOURCE_LIMITS,
    WorkerCapability.SHELL_NON_PTY,
    WorkerCapability.WORKSPACE_EPHEMERAL,
)
_ISOLATION = WorkerIsolationContract(True, True, True, True, True, True)


@dataclass(frozen=True, slots=True)
class ComposedSandboxShellJob:
    admitted: AdmittedSandboxShellJob
    lease_epoch: int
    worker_instance_id: str
    worker_epoch: int
    _release: Callable[[], Awaitable[None]]

    async def release(self) -> None:
        await self._release()


class ShellWorkerAdmissionComposer:
    """Create and later fence one ephemeral local Shell worker admission."""

    def __init__(
        self,
        *,
        worker_registry: WorkerRegistryStore,
        harness_store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        execution_grant_store: ExecutionGrantStore,
        tool_job_store: ToolJobStore,
        transport: AuthenticatedLocalShellTransport,
        software_version: str,
        run_delegation_grant_authority: RunDelegationGrantAuthority | None = None,
        worker_id: str = "local-shell-worker",
        now: NowProvider | None = None,
        token: TokenProvider | None = None,
    ) -> None:
        self._worker_registry = worker_registry
        self._harness_store = harness_store
        self._permission_store = permission_store
        self._execution_grant_store = execution_grant_store
        self._tool_job_store = tool_job_store
        self._transport = transport
        self._software_version = software_version
        self._run_delegation_grant_authority = run_delegation_grant_authority
        self._worker_id = worker_id
        self._now = now or (lambda: datetime.now(UTC).isoformat())
        self._token = token or (lambda: uuid4().hex)
        self._registration_lock = asyncio.Lock()

    async def compose(
        self,
        *,
        parent_receipt_id: str,
        spec: ShellCommandSpec,
        run_grant_id: str | None = None,
    ) -> ComposedSandboxShellJob:
        now = self._now()
        parent = await self._permission_store.get(parent_receipt_id)
        if parent is None:
            raise ValueError("Shell admission 父权限回执不存在。")
        run_grant_sha256 = ""
        if run_grant_id is not None:
            if self._run_delegation_grant_authority is None:
                raise ValueError("Shell admission 缺少 Run delegation authority。")
            run_validation = await self._run_delegation_grant_authority.validate(
                grant_id=run_grant_id,
                now=now,
            )
            run_contract = run_validation.contract
            if (
                not run_validation.allowed
                or run_contract is None
                or run_contract.parent_receipt_id != parent.receipt_id
                or run_contract.parent_receipt_sha256 != parent.receipt_sha256
                or run_contract.session_id != parent.session_id
                or run_contract.run_id != parent.run_id
                or "bash_run" not in run_contract.delegated_tool_names
            ):
                raise ValueError("Shell admission 的 Run delegation grant 无效。")
            run_grant_sha256 = run_contract.grant_sha256
        identity = hashlib.sha256(
            f"{parent.receipt_sha256}\0{run_grant_sha256}\0{spec.digest()}".encode()
        ).hexdigest()
        instance_id = f"shell-{self._token()[:32]}"
        async with self._registration_lock:
            history = await self._worker_registry.list_history(self._worker_id)
            worker_epoch = history[-1].contract.epoch + 1 if history else 1
            contract = issue_worker_contract(
                worker_id=self._worker_id,
                instance_id=instance_id,
                epoch=worker_epoch,
                kind=WorkerKind.TOOL,
                protocol_min=1,
                protocol_max=1,
                software_version=self._software_version,
                platform=detect_worker_platform(),
                capabilities=_CAPABILITIES,
                resources=WorkerResourceEnvelope(
                    max_concurrent_jobs=1,
                    max_memory_bytes=spec.max_memory_bytes,
                    max_cpu_seconds=spec.max_cpu_seconds,
                    max_wall_seconds=max(1, int(spec.timeout_seconds + 0.999)),
                    max_output_bytes=spec.max_output_bytes,
                ),
                isolation=_ISOLATION,
                issued_at=now,
            )
            await self._worker_registry.register(contract, registered_at=now)
        lease = await self._harness_store.acquire_run_lease(
            workspace_root=spec.workspace_root,
            run_kind=HarnessRunKind.TOOL,
            run_id=parent.run_id,
            owner_id=instance_id,
            now=now,
            lease_seconds=min(300, max(30, int(spec.timeout_seconds) + 15)),
        )
        if lease is None:
            await self._worker_registry.revoke(
                worker_id=contract.worker_id,
                instance_id=contract.instance_id,
                epoch=contract.epoch,
                reason_code="lease_unavailable",
                revoked_at=self._now(),
            )
            raise RuntimeError("Shell admission 无法取得 Tool run lease。")
        try:
            call_id = f"shell-{identity[:48]}"
            child_ttl = min(120, max(15, int(spec.timeout_seconds) + 10))
            authorization_now = self._now()
            if run_grant_id is None:
                child = await self._permission_store.issue_delegated(
                    parent_receipt_id=parent.receipt_id,
                    request_id=call_id,
                    call_id=call_id,
                    tool_name="bash_run",
                    tool_family="shell",
                    arguments=spec.canonical_payload(),
                    risk_level="high",
                    decided_at=authorization_now,
                    ttl_seconds=child_ttl,
                )
            else:
                assert self._run_delegation_grant_authority is not None
                child = await self._permission_store.issue_run_delegated(
                    run_grant_authority=self._run_delegation_grant_authority,
                    run_grant_id=run_grant_id,
                    request_id=call_id,
                    call_id=call_id,
                    tool_name="bash_run",
                    tool_family="shell",
                    arguments=spec.canonical_payload(),
                    risk_level="high",
                    decided_at=authorization_now,
                    ttl_seconds=child_ttl,
                )
            grant_request = ExecutionGrantRequest(
                session_id=parent.session_id,
                run_id=parent.run_id,
                call_id=call_id,
                tool_name="bash_run",
                arguments=spec.canonical_payload(),
                idempotency_key=f"shelljob-{identity[:48]}",
                worker_id=contract.worker_id,
                authorization_reference=child.receipt_id,
            )
            grant_authority = ExecutionGrantAuthority(
                store=self._execution_grant_store,
                worker_registry=self._worker_registry,
                harness_store=self._harness_store,
                permission_decision_store=self._permission_store,
                workspace_root=spec.workspace_root,
                run_delegation_grant_authority=(
                    self._run_delegation_grant_authority
                    if run_grant_id is not None
                    else None
                ),
            )
            grant = await grant_authority.issue(
                grant_request,
                decision=PermissionDecision(
                    allowed=True,
                    outcome=PermissionOutcome.ALLOW,
                    tool_family="shell",
                ),
                permission_mode=parent.permission_mode,
                source=ExecutionGrantSource.DELEGATED,
                now=authorization_now,
                ttl_seconds=child_ttl,
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
            health = issue_worker_health_report(
                contract=contract,
                heartbeat=HarnessHeartbeat(
                    workspace_root=str(Path(spec.workspace_root).resolve()),
                    subject_kind=HarnessRunKind.TOOL,
                    subject_id=contract.worker_id,
                    instance_id=contract.instance_id,
                    epoch=contract.epoch,
                    sequence=1,
                    phase=HarnessHeartbeatPhase.RUNNING,
                    observed_at=authorization_now,
                    timeout_seconds=30,
                    detail_code="ephemeral_shell_ready",
                ),
                active_jobs=0,
                accepting_jobs=True,
            )
            requirements = WorkerAdmissionRequirements(
                kind=WorkerKind.TOOL,
                protocol_version=1,
                capabilities=_CAPABILITIES,
                allowed_platforms=(contract.platform.system,),
                min_memory_bytes=spec.max_memory_bytes,
                min_cpu_seconds=spec.max_cpu_seconds,
                min_wall_seconds=max(1, int(spec.timeout_seconds + 0.999)),
                min_output_bytes=spec.max_output_bytes,
                isolation=_ISOLATION,
            )
            jobs = ToolJobAuthority(
                store=self._tool_job_store,
                execution_grants=grant_authority,
                worker_registry=self._worker_registry,
            )
            stored = await jobs.admit(
                tool_request,
                worker_health=health,
                requirements=requirements,
                now=authorization_now,
            )
            shell_request = ShellCommandRequest(
                job_id=stored.contract.job_id,
                worker_id=contract.worker_id,
                worker_instance_id=contract.instance_id,
                worker_epoch=contract.epoch,
                worker_contract_sha256=contract.contract_sha256,
                spec=spec,
            )
            coordinator = ShellWorkerCoordinator(
                jobs=jobs,
                lifecycle=ToolJobLifecycleAuthority(
                    self._tool_job_store, self._worker_registry
                ),
                worker_registry=self._worker_registry,
                transport=self._transport,
                now=self._now,
            )
            release_lock = asyncio.Lock()
            released = False

            async def release() -> None:
                nonlocal released
                async with release_lock:
                    if released:
                        return
                    await self._release_authorities(
                        spec=spec,
                        run_id=parent.run_id,
                        contract=contract,
                        lease_epoch=lease.epoch,
                        reason_code="ephemeral_job_finished",
                    )
                    released = True

            return ComposedSandboxShellJob(
                admitted=AdmittedSandboxShellJob(
                    job_id=stored.contract.job_id,
                    tool_job_request=tool_request,
                    shell_request=shell_request,
                    worker_health=health,
                    requirements=requirements,
                    dispatch_id=f"dispatch-{identity[:48]}",
                    coordinator=coordinator,
                ),
                lease_epoch=lease.epoch,
                worker_instance_id=contract.instance_id,
                worker_epoch=contract.epoch,
                _release=release,
            )
        except BaseException as exc:
            try:
                await self._release_authorities(
                    spec=spec,
                    run_id=parent.run_id,
                    contract=contract,
                    lease_epoch=lease.epoch,
                    reason_code="admission_failed",
                )
            except BaseException as cleanup_exc:
                exc.add_note(f"Shell admission 清理失败：{cleanup_exc}")
            raise

    async def _release_authorities(
        self,
        *,
        spec: ShellCommandSpec,
        run_id: str,
        contract: WorkerContract,
        lease_epoch: int,
        reason_code: str,
    ) -> None:
        released_at = self._now()
        errors: list[BaseException] = []
        try:
            released_lease = await self._harness_store.release_run_lease(
                workspace_root=spec.workspace_root,
                run_kind=HarnessRunKind.TOOL,
                run_id=run_id,
                owner_id=contract.instance_id,
                epoch=lease_epoch,
                now=released_at,
            )
            if released_lease is None:
                errors.append(RuntimeError("Tool run lease 已失效或不再属于当前 Worker。"))
        except BaseException as exc:
            errors.append(exc)
        try:
            await self._worker_registry.revoke(
                worker_id=contract.worker_id,
                instance_id=contract.instance_id,
                epoch=contract.epoch,
                reason_code=reason_code,
                revoked_at=released_at,
            )
        except BaseException as exc:
            errors.append(exc)
        if errors:
            detail = "; ".join(f"{type(item).__name__}: {item}" for item in errors)
            raise RuntimeError(f"Shell admission 权限清理不完整：{detail}")


__all__ = ["ComposedSandboxShellJob", "ShellWorkerAdmissionComposer"]
