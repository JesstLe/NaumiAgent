"""Minimal owner-fenced Supervisor for control-only independent Agent Workers."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from naumi_agent.daemons.agent_jobs import (
    AgentJobState,
    AgentJobStore,
    StoredAgentJob,
)
from naumi_agent.daemons.agent_worker_process import (
    agent_worker_job_owner_id,
    agent_worker_job_reservation_id,
)
from naumi_agent.daemons.agent_worker_supervisor_contract import (
    AgentWorkerProcessObservationState,
    AgentWorkerSupervisorFenceEvidence,
    AgentWorkerSupervisorLease,
)
from naumi_agent.daemons.worker_contract import (
    WorkerCapability,
    WorkerContract,
    WorkerKind,
)
from naumi_agent.daemons.worker_registry import (
    WorkerCapacityReservation,
    WorkerCapacityReservationState,
    WorkerRegistryStore,
)
from naumi_agent.harness.heartbeat import (
    HarnessHeartbeatHealth,
    HarnessHeartbeatSnapshot,
    assess_heartbeat,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore

_ACTIONABLE_HEALTH = frozenset(
    {
        HarnessHeartbeatHealth.STALE,
        HarnessHeartbeatHealth.OFFLINE,
        HarnessHeartbeatHealth.STOPPED,
        HarnessHeartbeatHealth.FAILED,
    }
)


class AgentWorkerSupervisorError(RuntimeError):
    """Raised when Supervisor facts cannot be trusted."""


class AgentWorkerSupervisorOutcome(StrEnum):
    STANDBY = "standby"
    IDLE = "idle"
    HEALTHY = "healthy"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    AUTHORITY_LIVE = "authority_live"
    PROCESS_ALIVE = "process_alive"
    FENCED = "fenced"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class AgentWorkerSupervisorResult:
    worker_id: str
    outcome: AgentWorkerSupervisorOutcome
    reason_code: str
    supervisor_epoch: int
    worker_epoch: int
    heartbeat_health: str
    process_observation: str
    job_id: str
    job_requeued: bool
    fence_receipt_sha256: str
    assessed_at: str


NowProvider = Callable[[], str]


class AgentWorkerSupervisor:
    """Reconcile one control-only Worker without executing model or tool work."""

    def __init__(
        self,
        *,
        worker_registry: WorkerRegistryStore,
        heartbeat_store: HarnessStore,
        agent_job_store: AgentJobStore,
        workspace_root: str | Path,
        worker_id: str = "agent-worker-local",
        owner_id: str | None = None,
        lease_seconds: int = 30,
        now_provider: NowProvider = lambda: datetime.now(UTC).isoformat(),
    ) -> None:
        if not isinstance(worker_registry, WorkerRegistryStore):
            raise TypeError("worker_registry 必须是 WorkerRegistryStore。")
        if not isinstance(heartbeat_store, HarnessStore):
            raise TypeError("heartbeat_store 必须是 HarnessStore。")
        if not isinstance(agent_job_store, AgentJobStore):
            raise TypeError("agent_job_store 必须是 AgentJobStore。")
        workspace = Path(workspace_root).expanduser()
        if not workspace.is_absolute():
            raise ValueError("Supervisor workspace_root 必须是绝对路径。")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("Supervisor worker_id 不能为空。")
        resolved_owner = owner_id or f"agent-supervisor:{uuid.uuid4().hex}"
        if not isinstance(resolved_owner, str) or not resolved_owner.strip():
            raise ValueError("Supervisor owner_id 不能为空。")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 3 <= lease_seconds <= 86_400
        ):
            raise ValueError("Supervisor lease_seconds 必须在 3 到 86400 之间。")
        if not callable(now_provider):
            raise TypeError("now_provider 必须可调用。")
        self._registry = worker_registry
        self._heartbeats = heartbeat_store
        self._agent_jobs = agent_job_store
        self._workspace_root = workspace.resolve(strict=False)
        self._worker_id = worker_id
        self._owner_id = resolved_owner
        self._lease_seconds = lease_seconds
        self._now = now_provider

    async def reconcile_once(self) -> AgentWorkerSupervisorResult:
        """Perform one bounded decision under an exact Supervisor owner lease."""
        assessed_at = self._timestamp("assessed_at")
        lease = await self._registry.acquire_supervisor_lease(
            worker_id=self._worker_id,
            owner_id=self._owner_id,
            acquired_at=assessed_at,
            lease_seconds=self._lease_seconds,
        )
        if lease is None:
            return self._result(
                AgentWorkerSupervisorOutcome.STANDBY,
                "supervisor_owner_live",
                assessed_at=assessed_at,
            )
        try:
            active = await self._registry.get_active(self._worker_id)
            if active is None:
                return await self._resume_or_idle(lease, assessed_at=assessed_at)
            contract = active.contract
            if (
                contract.kind is not WorkerKind.AGENT
                or WorkerCapability.AGENT_CONTROL_TRANSPORT not in contract.capabilities
                or WorkerCapability.AGENT_JOB_OWNER_LEASE not in contract.capabilities
                or WorkerCapability.AGENT_CONTEXT_SCOPE in contract.capabilities
            ):
                return self._result(
                    AgentWorkerSupervisorOutcome.EVIDENCE_INCOMPLETE,
                    "worker_contract_not_control_only",
                    lease=lease,
                    worker_epoch=contract.epoch,
                    assessed_at=assessed_at,
                )
            heartbeat = await self._heartbeats.get_heartbeat(
                workspace_root=self._workspace_root,
                subject_kind=HarnessRunKind.AGENT,
                subject_id=self._worker_id,
            )
            if heartbeat is None:
                return self._result(
                    AgentWorkerSupervisorOutcome.EVIDENCE_INCOMPLETE,
                    "worker_heartbeat_missing",
                    lease=lease,
                    worker_epoch=contract.epoch,
                    assessed_at=assessed_at,
                )
            if (
                heartbeat.instance_id != contract.instance_id
                or heartbeat.epoch != contract.epoch
            ):
                return self._result(
                    AgentWorkerSupervisorOutcome.EVIDENCE_INCOMPLETE,
                    "worker_heartbeat_identity_mismatch",
                    lease=lease,
                    worker_epoch=contract.epoch,
                    assessed_at=assessed_at,
                )
            heartbeat_snapshot = assess_heartbeat(heartbeat, now=assessed_at)
            if heartbeat_snapshot.health not in _ACTIONABLE_HEALTH:
                return self._result(
                    AgentWorkerSupervisorOutcome.HEALTHY,
                    "worker_heartbeat_not_actionable",
                    lease=lease,
                    worker_epoch=contract.epoch,
                    heartbeat_health=heartbeat_snapshot.health.value,
                    assessed_at=assessed_at,
                )
            observation = await self._registry.observe_process_witness(
                worker_id=contract.worker_id,
                epoch=contract.epoch,
                assessed_at=assessed_at,
            )
            if observation is None:
                return self._result(
                    AgentWorkerSupervisorOutcome.EVIDENCE_INCOMPLETE,
                    "worker_process_witness_missing",
                    lease=lease,
                    worker_epoch=contract.epoch,
                    heartbeat_health=heartbeat_snapshot.health.value,
                    assessed_at=assessed_at,
                )
            if observation.state is AgentWorkerProcessObservationState.ALIVE:
                return self._result(
                    AgentWorkerSupervisorOutcome.PROCESS_ALIVE,
                    "worker_process_still_alive",
                    lease=lease,
                    worker_epoch=contract.epoch,
                    heartbeat_health=heartbeat_snapshot.health.value,
                    process_observation=observation.state.value,
                    assessed_at=assessed_at,
                )
            if observation.state is AgentWorkerProcessObservationState.UNVERIFIABLE:
                return self._result(
                    AgentWorkerSupervisorOutcome.EVIDENCE_INCOMPLETE,
                    "worker_process_unverifiable",
                    lease=lease,
                    worker_epoch=contract.epoch,
                    heartbeat_health=heartbeat_snapshot.health.value,
                    process_observation=observation.state.value,
                    assessed_at=assessed_at,
                )

            job = await self._agent_jobs.get_worker_prestart_claim(
                owner_id=agent_worker_job_owner_id(contract),
            )
            reservation = None
            if job is not None:
                if job.state is AgentJobState.RUNNING:
                    return self._result(
                        AgentWorkerSupervisorOutcome.EVIDENCE_INCOMPLETE,
                        "agent_job_running_side_effect_unknown",
                        lease=lease,
                        worker_epoch=contract.epoch,
                        heartbeat_health=heartbeat_snapshot.health.value,
                        process_observation=observation.state.value,
                        job_id=job.job_id,
                        assessed_at=assessed_at,
                    )
                if job.state is not AgentJobState.CLAIMED or not job.claim_expires_at:
                    return self._result(
                        AgentWorkerSupervisorOutcome.EVIDENCE_INCOMPLETE,
                        "agent_job_prestart_evidence_invalid",
                        lease=lease,
                        worker_epoch=contract.epoch,
                        heartbeat_health=heartbeat_snapshot.health.value,
                        process_observation=observation.state.value,
                        job_id=job.job_id,
                        assessed_at=assessed_at,
                    )
                if datetime.fromisoformat(job.claim_expires_at) > datetime.fromisoformat(
                    assessed_at
                ):
                    return self._result(
                        AgentWorkerSupervisorOutcome.AUTHORITY_LIVE,
                        "agent_job_claim_live",
                        lease=lease,
                        worker_epoch=contract.epoch,
                        heartbeat_health=heartbeat_snapshot.health.value,
                        process_observation=observation.state.value,
                        job_id=job.job_id,
                        assessed_at=assessed_at,
                    )
                reservation_id = agent_worker_job_reservation_id(contract, job.job_id)
                reservation = await self._registry.get_capacity_reservation(
                    reservation_id,
                    assessed_at=assessed_at,
                )
                if reservation is None:
                    return self._result(
                        AgentWorkerSupervisorOutcome.EVIDENCE_INCOMPLETE,
                        "worker_capacity_reservation_missing",
                        lease=lease,
                        worker_epoch=contract.epoch,
                        heartbeat_health=heartbeat_snapshot.health.value,
                        process_observation=observation.state.value,
                        job_id=job.job_id,
                        assessed_at=assessed_at,
                    )
                if reservation.state is WorkerCapacityReservationState.ACTIVE:
                    return self._result(
                        AgentWorkerSupervisorOutcome.AUTHORITY_LIVE,
                        "worker_capacity_reservation_live",
                        lease=lease,
                        worker_epoch=contract.epoch,
                        heartbeat_health=heartbeat_snapshot.health.value,
                        process_observation=observation.state.value,
                        job_id=job.job_id,
                        assessed_at=assessed_at,
                    )
                if reservation.state not in {
                    WorkerCapacityReservationState.EXPIRED,
                    WorkerCapacityReservationState.FENCED,
                }:
                    return self._result(
                        AgentWorkerSupervisorOutcome.EVIDENCE_INCOMPLETE,
                        "worker_capacity_reservation_terminal_mismatch",
                        lease=lease,
                        worker_epoch=contract.epoch,
                        heartbeat_health=heartbeat_snapshot.health.value,
                        process_observation=observation.state.value,
                        job_id=job.job_id,
                        assessed_at=assessed_at,
                    )

            lease = await self._registry.renew_supervisor_lease(
                worker_id=self._worker_id,
                owner_id=self._owner_id,
                epoch=lease.epoch,
                renewed_at=assessed_at,
                lease_seconds=self._lease_seconds,
            )
            if lease is None:
                raise AgentWorkerSupervisorError(
                    "Supervisor lease 在 fencing 前已失效。"
                )
            evidence = _build_fence_evidence(
                contract=contract,
                heartbeat_snapshot=heartbeat_snapshot,
                job=job,
                reservation=reservation,
                decided_at=assessed_at,
            )
            receipt = await self._registry.fence_agent_worker_for_supervisor(
                evidence=evidence,
                supervisor_owner_id=self._owner_id,
                supervisor_epoch=lease.epoch,
            )
            requeued = False
            if receipt.has_job:
                transition = await self._agent_jobs.requeue_expired_prestart_worker_claim(
                    receipt
                )
                requeued = transition.job.state is AgentJobState.ADMITTED
            return self._result(
                AgentWorkerSupervisorOutcome.FENCED,
                "agent_worker_supervisor_fenced",
                lease=lease,
                worker_epoch=contract.epoch,
                heartbeat_health=heartbeat_snapshot.health.value,
                process_observation=observation.state.value,
                job_id=job.job_id if job is not None else "",
                job_requeued=requeued,
                fence_receipt_sha256=receipt.receipt_sha256,
                assessed_at=assessed_at,
            )
        finally:
            await self._registry.release_supervisor_lease(
                worker_id=self._worker_id,
                owner_id=self._owner_id,
                epoch=lease.epoch,
                released_at=self._timestamp("released_at"),
            )

    async def _resume_or_idle(
        self,
        lease: AgentWorkerSupervisorLease,
        *,
        assessed_at: str,
    ) -> AgentWorkerSupervisorResult:
        receipt = await self._registry.get_latest_supervisor_fence(
            worker_id=self._worker_id,
        )
        if receipt is None:
            return self._result(
                AgentWorkerSupervisorOutcome.IDLE,
                "worker_registration_absent",
                lease=lease,
                assessed_at=assessed_at,
            )
        requeued = False
        if receipt.has_job:
            transition = await self._agent_jobs.requeue_expired_prestart_worker_claim(
                receipt
            )
            requeued = transition.job.state is AgentJobState.ADMITTED
        return self._result(
            AgentWorkerSupervisorOutcome.RECOVERED,
            "supervisor_fence_reconciled",
            lease=lease,
            worker_epoch=receipt.evidence.worker_epoch,
            heartbeat_health=receipt.evidence.heartbeat_health,
            process_observation=receipt.process_observation.value,
            job_id=receipt.evidence.job_id,
            job_requeued=requeued,
            fence_receipt_sha256=receipt.receipt_sha256,
            assessed_at=assessed_at,
        )

    def _result(
        self,
        outcome: AgentWorkerSupervisorOutcome,
        reason_code: str,
        *,
        lease: AgentWorkerSupervisorLease | None = None,
        worker_epoch: int = 0,
        heartbeat_health: str = "",
        process_observation: str = "",
        job_id: str = "",
        job_requeued: bool = False,
        fence_receipt_sha256: str = "",
        assessed_at: str,
    ) -> AgentWorkerSupervisorResult:
        return AgentWorkerSupervisorResult(
            worker_id=self._worker_id,
            outcome=outcome,
            reason_code=reason_code,
            supervisor_epoch=lease.epoch if lease is not None else 0,
            worker_epoch=worker_epoch,
            heartbeat_health=heartbeat_health,
            process_observation=process_observation,
            job_id=job_id,
            job_requeued=job_requeued,
            fence_receipt_sha256=fence_receipt_sha256,
            assessed_at=assessed_at,
        )

    def _timestamp(self, field: str) -> str:
        value = self._now()
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise AgentWorkerSupervisorError(
                f"Supervisor {field} 时间无效。"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise AgentWorkerSupervisorError(
                f"Supervisor {field} 时间必须包含时区。"
            )
        return parsed.isoformat()


class AgentWorkerSupervisorFactory:
    """Create bounded Supervisor reconcilers from Runtime-owned authorities."""

    def __init__(
        self,
        *,
        worker_registry: WorkerRegistryStore,
        heartbeat_store: HarnessStore,
        agent_job_store: AgentJobStore,
        workspace_root: str | Path,
    ) -> None:
        self.worker_registry = worker_registry
        self.heartbeat_store = heartbeat_store
        self.agent_job_store = agent_job_store
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        AgentWorkerSupervisor(
            worker_registry=worker_registry,
            heartbeat_store=heartbeat_store,
            agent_job_store=agent_job_store,
            workspace_root=self.workspace_root,
        )

    def create(
        self,
        *,
        worker_id: str = "agent-worker-local",
        owner_id: str | None = None,
        lease_seconds: int = 30,
        now_provider: NowProvider = lambda: datetime.now(UTC).isoformat(),
    ) -> AgentWorkerSupervisor:
        return AgentWorkerSupervisor(
            worker_registry=self.worker_registry,
            heartbeat_store=self.heartbeat_store,
            agent_job_store=self.agent_job_store,
            workspace_root=self.workspace_root,
            worker_id=worker_id,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            now_provider=now_provider,
        )


def _build_fence_evidence(
    *,
    contract: WorkerContract,
    heartbeat_snapshot: HarnessHeartbeatSnapshot,
    job: StoredAgentJob | None,
    reservation: WorkerCapacityReservation | None,
    decided_at: str,
) -> AgentWorkerSupervisorFenceEvidence:
    # Runtime type checks above keep this helper small while the canonical
    # operation id still binds every persisted authority fact.
    heartbeat = heartbeat_snapshot.heartbeat
    payload = {
        "worker_id": contract.worker_id,
        "instance_id": contract.instance_id,
        "worker_epoch": contract.epoch,
        "contract_sha256": contract.contract_sha256,
        "heartbeat_sequence": heartbeat.sequence,
        "heartbeat_phase": heartbeat.phase.value,
        "heartbeat_health": heartbeat_snapshot.health.value,
        "heartbeat_observed_at": heartbeat.observed_at,
        "job_id": job.job_id if job is not None else "",
        "request_sha256": job.request_sha256 if job is not None else "",
        "job_owner_id": (job.claim_owner_id or "") if job is not None else "",
        "claim_epoch": job.claim_epoch if job is not None else 0,
        "claim_expires_at": (job.claim_expires_at or "") if job is not None else "",
        "latest_job_receipt_sha256": (
            job.latest_receipt.receipt_sha256 if job is not None else ""
        ),
        "reservation_id": reservation.reservation_id if reservation is not None else "",
        "reservation_state": reservation.state.value if reservation is not None else "",
        "reservation_expires_at": reservation.expires_at if reservation is not None else "",
        "decided_at": decided_at,
    }
    operation_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AgentWorkerSupervisorFenceEvidence(
        operation_id=f"agent-supervisor-fence:{operation_digest}",
        **payload,
    )


__all__ = [
    "AgentWorkerSupervisor",
    "AgentWorkerSupervisorError",
    "AgentWorkerSupervisorFactory",
    "AgentWorkerSupervisorOutcome",
    "AgentWorkerSupervisorResult",
]
