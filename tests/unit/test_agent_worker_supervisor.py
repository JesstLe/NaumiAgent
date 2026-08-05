from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.daemons.agent_jobs import AgentJobPayload, AgentJobState, AgentJobStore
from naumi_agent.daemons.agent_worker_contract import issue_agent_worker_request
from naumi_agent.daemons.agent_worker_process import (
    agent_worker_job_owner_id,
    agent_worker_job_reservation_id,
)
from naumi_agent.daemons.agent_worker_supervisor import (
    AgentWorkerSupervisor,
    AgentWorkerSupervisorOutcome,
)
from naumi_agent.daemons.agent_worker_supervisor_contract import (
    AgentWorkerProcessObservationState,
    supervisor_fence_receipt_from_json,
    supervisor_fence_receipt_json,
    verify_agent_worker_supervisor_fence_receipt,
)
from naumi_agent.daemons.worker_authority_health import inspect_worker_authority_health
from naumi_agent.daemons.worker_contract import (
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
    WorkerResourceEnvelope,
    detect_worker_platform,
    issue_worker_contract,
)
from naumi_agent.daemons.worker_registry import (
    WorkerCapacityReservationState,
    WorkerRegistrationState,
    WorkerRegistryStore,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.ui.doctor import _worker_authority_check


@dataclass
class _Clock:
    value: datetime

    def datetime(self) -> datetime:
        return self.value

    def iso(self) -> str:
        return self.value.isoformat()

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _contract(clock: _Clock):
    return issue_worker_contract(
        worker_id="agent-worker-local",
        instance_id="agent-process-supervisor-test",
        epoch=1,
        kind=WorkerKind.AGENT,
        protocol_min=1,
        protocol_max=1,
        software_version="0.1.214",
        platform=detect_worker_platform(),
        capabilities=(
            WorkerCapability.AGENT_CONTROL_TRANSPORT,
            WorkerCapability.AGENT_JOB_OWNER_LEASE,
        ),
        resources=WorkerResourceEnvelope(
            max_concurrent_jobs=1,
            max_memory_bytes=16 * 1024 * 1024,
            max_cpu_seconds=60,
            max_wall_seconds=60,
            max_output_bytes=1024,
        ),
        isolation=WorkerIsolationContract(False, False, False, False, False, False),
        issued_at=clock.iso(),
    )


def _child() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _authority(
    tmp_path: Path,
    clock: _Clock,
) -> tuple[
    WorkerRegistryStore,
    HarnessStore,
    AgentJobStore,
    object,
    object,
]:
    key = b"s" * 32
    registry = WorkerRegistryStore(
        tmp_path / "worker-registry.db",
        supervisor_key_provider=lambda: key,
    )
    heartbeats = HarnessStore(tmp_path / "harness.db")
    jobs = AgentJobStore(
        tmp_path / "agent-jobs.db",
        key_provider=lambda: key,
        clock=clock.datetime,
    )
    contract = _contract(clock)
    process = _child()
    await registry.register(contract, registered_at=clock.iso())
    await registry.register_process_witness(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        process_id=process.pid,
        witnessed_at=clock.iso(),
    )
    await heartbeats.record_heartbeat(
        workspace_root=tmp_path,
        subject_kind=HarnessRunKind.AGENT,
        subject_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at=clock.iso(),
        timeout_seconds=3,
        detail_code="agent_worker_control_alive_dispatch_disabled",
    )
    payload = AgentJobPayload(
        task_id="supervisor-job",
        session_id="supervisor-session",
        task="验证 Supervisor 安全接管",
        context="只允许 pre-start requeue",
        message_topic="agent.result.supervisor",
    )
    request = issue_agent_worker_request(
        task_id=payload.task_id,
        session_id=payload.session_id,
        agent_name="Explore",
        task=payload.task,
        context=payload.context,
        tool_scope=("file_read",),
        permission_mode="moderate",
        model_tier="capable",
        max_turns=50,
        max_budget_usd=None,
        timeout_seconds=60,
        message_topic=payload.message_topic,
        issued_at=clock.iso(),
    )
    job = await jobs.admit(request=request, payload=payload)
    reservation_id = agent_worker_job_reservation_id(contract, job.job_id)
    await registry.reserve_capacity(
        reservation_id=reservation_id,
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        job_id=job.job_id,
        reserved_at=clock.iso(),
        ttl_seconds=3,
    )
    await jobs.claim_admitted_for_worker(
        job.job_id,
        owner_id=agent_worker_job_owner_id(contract),
        expected_request_sha256=job.request_sha256,
        lease_seconds=3,
    )
    return registry, heartbeats, jobs, contract, process


@pytest.mark.asyncio
async def test_supervisor_refuses_stale_heartbeat_while_exact_process_is_alive(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    registry, heartbeats, jobs, contract, process = await _authority(tmp_path, clock)
    try:
        clock.advance(10)
        result = await AgentWorkerSupervisor(
            worker_registry=registry,
            heartbeat_store=heartbeats,
            agent_job_store=jobs,
            workspace_root=tmp_path,
            owner_id="supervisor-live-process",
            now_provider=clock.iso,
        ).reconcile_once()

        assert result.outcome is AgentWorkerSupervisorOutcome.PROCESS_ALIVE
        assert result.process_observation == AgentWorkerProcessObservationState.ALIVE.value
        active = await registry.get_active(contract.worker_id)
        assert active is not None and active.state is WorkerRegistrationState.ACTIVE
        claimed = await jobs.get_worker_prestart_claim(
            owner_id=agent_worker_job_owner_id(contract)
        )
        assert claimed is not None and claimed.state is AgentJobState.CLAIMED
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.asyncio
async def test_supervisor_fences_dead_process_and_requeues_only_expired_prestart_job(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 8, 5, 13, 0, tzinfo=UTC))
    key = b"s" * 32
    registry, heartbeats, jobs, contract, process = await _authority(tmp_path, clock)
    process.terminate()
    process.wait(timeout=5)
    clock.advance(10)

    first = await AgentWorkerSupervisor(
        worker_registry=registry,
        heartbeat_store=heartbeats,
        agent_job_store=jobs,
        workspace_root=tmp_path,
        owner_id="supervisor-dead-process",
        now_provider=clock.iso,
    ).reconcile_once()

    assert first.outcome is AgentWorkerSupervisorOutcome.FENCED
    assert first.job_requeued is True
    assert first.process_observation == AgentWorkerProcessObservationState.DEAD.value
    assert await registry.get_active(contract.worker_id) is None
    receipt = await registry.get_latest_supervisor_fence(worker_id=contract.worker_id)
    assert receipt is not None
    assert verify_agent_worker_supervisor_fence_receipt(
        receipt,
        authentication_key=key,
    )
    assert not verify_agent_worker_supervisor_fence_receipt(
        receipt,
        authentication_key=b"x" * 32,
    )
    unknown_field_payload = json.loads(supervisor_fence_receipt_json(receipt))
    unknown_field_payload["unexpected"] = "must-fail-closed"
    with pytest.raises(ValueError, match="字段集合"):
        supervisor_fence_receipt_from_json(
            json.dumps(unknown_field_payload, ensure_ascii=False)
        )
    job = await jobs.get(receipt.evidence.job_id)
    assert job is not None and job.state is AgentJobState.ADMITTED
    assert job.latest_receipt.reason_code == "agent_worker_supervisor_prestart_requeued"
    reservation = await registry.get_capacity_reservation(
        receipt.evidence.reservation_id,
        assessed_at=clock.iso(),
    )
    assert reservation is not None
    assert reservation.state is WorkerCapacityReservationState.EXPIRED
    receipt_sequence = job.latest_receipt.sequence
    authority_snapshot = inspect_worker_authority_health(
        registry_db_path=registry.db_path,
        harness_db_path=heartbeats.db_path,
        workspace_root=tmp_path,
        now=clock.iso(),
    )
    doctor_check = _worker_authority_check(authority_snapshot)
    assert authority_snapshot.supervisor_fence_count == 1
    assert authority_snapshot.latest_supervisor_job_id == receipt.evidence.job_id
    assert doctor_check.status == "pass"
    assert "Supervisor 已完成 1 次显式 fencing" in doctor_check.detail

    replay = await AgentWorkerSupervisor(
        worker_registry=registry,
        heartbeat_store=heartbeats,
        agent_job_store=jobs,
        workspace_root=tmp_path,
        owner_id="supervisor-reconcile-replay",
        now_provider=clock.iso,
    ).reconcile_once()
    replayed_job = await jobs.get(receipt.evidence.job_id)

    assert replay.outcome is AgentWorkerSupervisorOutcome.RECOVERED
    assert replay.job_requeued is True
    assert replayed_job is not None
    assert replayed_job.latest_receipt.sequence == receipt_sequence


@pytest.mark.asyncio
async def test_supervisor_refuses_running_job_with_unknown_side_effect_boundary(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 8, 5, 14, 0, tzinfo=UTC))
    registry, heartbeats, jobs, contract, process = await _authority(tmp_path, clock)
    claimed = await jobs.get_worker_prestart_claim(
        owner_id=agent_worker_job_owner_id(contract)
    )
    assert claimed is not None
    await jobs.mark_running(
        claimed.job_id,
        owner_id=agent_worker_job_owner_id(contract),
        claim_epoch=claimed.claim_epoch,
    )
    process.terminate()
    process.wait(timeout=5)
    clock.advance(10)

    result = await AgentWorkerSupervisor(
        worker_registry=registry,
        heartbeat_store=heartbeats,
        agent_job_store=jobs,
        workspace_root=tmp_path,
        owner_id="supervisor-running-job",
        now_provider=clock.iso,
    ).reconcile_once()

    assert result.outcome is AgentWorkerSupervisorOutcome.EVIDENCE_INCOMPLETE
    assert result.reason_code == "agent_job_running_side_effect_unknown"
    assert await registry.get_active(contract.worker_id) is not None
    running = await jobs.get(claimed.job_id)
    assert running is not None and running.state is AgentJobState.RUNNING
    assert await registry.get_latest_supervisor_fence(
        worker_id=contract.worker_id
    ) is None


@pytest.mark.asyncio
async def test_supervisor_stands_by_while_another_owner_holds_live_lease(
    tmp_path: Path,
) -> None:
    clock = _Clock(datetime(2026, 8, 5, 15, 0, tzinfo=UTC))
    registry, heartbeats, jobs, contract, process = await _authority(tmp_path, clock)
    try:
        lease = await registry.acquire_supervisor_lease(
            worker_id=contract.worker_id,
            owner_id="supervisor-primary",
            acquired_at=clock.iso(),
            lease_seconds=30,
        )
        assert lease is not None

        result = await AgentWorkerSupervisor(
            worker_registry=registry,
            heartbeat_store=heartbeats,
            agent_job_store=jobs,
            workspace_root=tmp_path,
            owner_id="supervisor-contender",
            now_provider=clock.iso,
        ).reconcile_once()

        assert result.outcome is AgentWorkerSupervisorOutcome.STANDBY
        assert result.reason_code == "supervisor_owner_live"
        assert await registry.get_active(contract.worker_id) is not None
    finally:
        process.terminate()
        process.wait(timeout=5)
