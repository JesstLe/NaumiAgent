from __future__ import annotations

import asyncio
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import naumi_agent.daemons.agent_worker_process as agent_worker_process_module
from naumi_agent.daemons.agent_jobs import AgentJobPayload, AgentJobState, AgentJobStore
from naumi_agent.daemons.agent_worker_contract import issue_agent_worker_request
from naumi_agent.daemons.agent_worker_process import (
    AgentWorkerProcessError,
    AgentWorkerProcessState,
    AuthenticatedAgentWorkerProcess,
    _child_message,
    _transport_address,
    _validate_child_message,
)
from naumi_agent.daemons.agent_worker_supervisor_contract import (
    AgentWorkerProcessObservationState,
)
from naumi_agent.daemons.worker_authority_health import (
    inspect_worker_authority_health,
)
from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionReason,
    WorkerAdmissionRequirements,
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
)
from naumi_agent.daemons.worker_registry import (
    WorkerCapacityReservationState,
    WorkerRegistryStore,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.store import HarnessStore
from naumi_agent.ui.doctor import _worker_authority_check


def _process(tmp_path: Path, **overrides: object) -> AuthenticatedAgentWorkerProcess:
    values: dict[str, object] = {
        "worker_registry": WorkerRegistryStore(tmp_path / "worker-registry.db"),
        "heartbeat_store": HarnessStore(tmp_path / "harness.db"),
        "agent_job_store": AgentJobStore(
            tmp_path / "agent-jobs.db",
            key_provider=lambda: b"j" * 32,
        ),
        "workspace_root": tmp_path / "workspace",
        "runtime_dir": tmp_path / "runtime" / "agent-worker",
        "software_version": "0.1.214",
        "max_concurrent_jobs": 4,
        "heartbeat_interval_seconds": 0.05,
        "heartbeat_timeout_seconds": 3,
        "handshake_timeout_seconds": 5,
        "shutdown_timeout_seconds": 3,
        "job_claim_lease_seconds": 3,
        "job_claim_renewal_interval_seconds": 0.2,
    }
    values.update(overrides)
    return AuthenticatedAgentWorkerProcess(**values)  # type: ignore[arg-type]


async def _admit_job(process: AuthenticatedAgentWorkerProcess) -> str:
    payload = AgentJobPayload(
        task_id="task-independent-worker",
        session_id="session-independent-worker",
        task="审查一个真实模块",
        context="仅在认证子进程内暂存",
        message_topic="agent.result.independent-worker",
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
        issued_at=datetime.now(UTC).isoformat(),
    )
    job = await process._agent_jobs.admit(request=request, payload=payload)
    return job.job_id


def test_constructor_is_lazy_and_rejects_unsafe_configuration(tmp_path: Path) -> None:
    process = _process(tmp_path)

    assert process.snapshot().state is AgentWorkerProcessState.CREATED
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "worker-registry.db").exists()
    assert not (tmp_path / "harness.db").exists()
    assert not (tmp_path / "agent-jobs.db").exists()

    with pytest.raises(ValueError, match="interval 必须小于 timeout"):
        _process(
            tmp_path,
            heartbeat_interval_seconds=3,
            heartbeat_timeout_seconds=3,
        )
    with pytest.raises(ValueError, match="runtime_dir 必须是绝对路径"):
        _process(tmp_path, runtime_dir=Path("relative"))


@pytest.mark.asyncio
async def test_runtime_path_wrong_type_fails_terminal_without_spawning(
    tmp_path: Path,
) -> None:
    runtime_path = tmp_path / "runtime-path"
    runtime_path.write_text("not a directory", encoding="utf-8")
    process = _process(tmp_path, runtime_dir=runtime_path)

    with pytest.raises(AgentWorkerProcessError):
        await process.start()

    assert process.snapshot().state is AgentWorkerProcessState.FAILED
    assert process.snapshot().process_id is None
    assert not (tmp_path / "worker-registry.db").exists()
    assert not (tmp_path / "harness.db").exists()


def test_windows_transport_uses_authenticated_named_pipe_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr("naumi_agent.daemons.agent_worker_process.os.name", "nt")

    address, family = _transport_address(runtime_dir)

    assert family == "AF_PIPE"
    assert isinstance(address, str)
    assert address.startswith(r"\\.\pipe\naumi-agent-")


@pytest.mark.asyncio
async def test_real_process_registers_pulses_stays_dispatch_disabled_and_revokes(
    tmp_path: Path,
) -> None:
    process = _process(tmp_path)
    started = await process.start()

    assert started.state is AgentWorkerProcessState.RUNNING
    assert started.process_id is not None and started.process_id != os.getpid()
    assert started.accepting_jobs is False
    assert process.contract is not None
    assert process.contract.kind is WorkerKind.AGENT
    assert process.contract.capabilities == (
        WorkerCapability.AGENT_CONTROL_TRANSPORT,
        WorkerCapability.AGENT_JOB_OWNER_LEASE,
    )
    registration = await process._registry.get_active(started.worker_id)
    assert registration is not None
    assert registration.contract.contract_sha256 == started.contract_sha256
    witness = await process._registry.get_process_witness(
        worker_id=started.worker_id,
        epoch=started.epoch,
    )
    observation = await process._registry.observe_process_witness(
        worker_id=started.worker_id,
        epoch=started.epoch,
        assessed_at=datetime.now(UTC).isoformat(),
    )
    assert witness is not None and witness.process_id == started.process_id
    assert observation is not None
    assert observation.state is AgentWorkerProcessObservationState.ALIVE

    await asyncio.sleep(0.14)
    pulse = await process._heartbeats.get_heartbeat(
        workspace_root=tmp_path / "workspace",
        subject_kind=WorkerKind.AGENT.value,
        subject_id=started.worker_id,
    )
    assert pulse is not None
    assert pulse.phase is HarnessHeartbeatPhase.RUNNING
    assert pulse.sequence >= 3
    assert process.health_report().accepting_jobs is False

    forged = _child_message(
        "pulse",
        nonce="forged-nonce",
        worker_id=process.contract.worker_id,
        instance_id=process.contract.instance_id,
        epoch=process.contract.epoch,
        contract_sha256=process.contract.contract_sha256,
        process_id=started.process_id,
        sequence=pulse.sequence + 1,
    )
    with pytest.raises(AgentWorkerProcessError, match="认证失败"):
        _validate_child_message(
            forged,
            expected_type="pulse",
            nonce=process._nonce,
            contract=process.contract,
            process_id=started.process_id,
            expected_sequence=pulse.sequence + 1,
        )

    admission = await process._registry.assess_admission(
        worker_id=started.worker_id,
        report=process.health_report(),
        requirements=WorkerAdmissionRequirements(
            kind=WorkerKind.AGENT,
            protocol_version=1,
            capabilities=(WorkerCapability.AGENT_CONTEXT_SCOPE,),
            isolation=WorkerIsolationContract(False, False, False, False, False, False),
        ),
        now=pulse.observed_at,
    )
    assert admission.admitted is False
    assert WorkerAdmissionReason.CAPABILITY_MISSING in admission.reasons
    assert WorkerAdmissionReason.NOT_ACCEPTING_JOBS in admission.reasons

    authority = inspect_worker_authority_health(
        registry_db_path=tmp_path / "worker-registry.db",
        harness_db_path=tmp_path / "harness.db",
        workspace_root=tmp_path / "workspace",
        now=pulse.observed_at,
    )
    check = _worker_authority_check(authority)
    assert check.status == "warn"
    assert "Job owner lease 就绪、模型执行未开放" in check.detail
    assert "embedded Runtime" in check.suggestion

    stopped = await process.close()
    assert stopped.state is AgentWorkerProcessState.STOPPED
    assert await process._registry.get_active(started.worker_id) is None
    terminal = await process._heartbeats.get_heartbeat(
        workspace_root=tmp_path / "workspace",
        subject_kind=WorkerKind.AGENT.value,
        subject_id=started.worker_id,
    )
    assert terminal is not None
    assert terminal.phase is HarnessHeartbeatPhase.STOPPED
    assert terminal.sequence > pulse.sequence
    assert process._process is not None and not process._process.is_alive()
    stopped_observation = await process._registry.observe_process_witness(
        worker_id=started.worker_id,
        epoch=started.epoch,
        assessed_at=datetime.now(UTC).isoformat(),
    )
    assert stopped_observation is not None
    assert stopped_observation.state in {
        AgentWorkerProcessObservationState.DEAD,
        AgentWorkerProcessObservationState.ZOMBIE,
    }
    assert not tuple((tmp_path / "runtime" / "agent-worker").glob("*.sock"))
    if os.name != "nt":
        assert stat.S_IMODE(
            (tmp_path / "runtime" / "agent-worker").stat().st_mode
        ) == 0o700


@pytest.mark.asyncio
async def test_real_process_binds_renews_and_releases_exact_prestart_job(
    tmp_path: Path,
) -> None:
    process = _process(tmp_path)
    await process.start()
    job_id = await _admit_job(process)

    binding = await process.bind_job(job_id)

    assert binding.job_id == job_id
    assert binding.claim_epoch == 1
    assert process.snapshot().bound_job_id == job_id
    assert process.health_report().active_jobs == 1
    claimed = await process._agent_jobs.get(job_id)
    assert claimed is not None
    assert claimed.state is AgentJobState.CLAIMED
    assert claimed.claim_owner_id == binding.owner_id
    assert claimed.claim_epoch == binding.claim_epoch
    reservation = await process._registry.get_capacity_reservation(
        binding.reservation_id,
        assessed_at=datetime.now(UTC).isoformat(),
    )
    assert reservation is not None
    assert reservation.state is WorkerCapacityReservationState.ACTIVE
    initial_expiry = reservation.expires_at

    await asyncio.sleep(0.35)

    renewed = await process._agent_jobs.get(job_id)
    assert renewed is not None
    assert renewed.latest_receipt.reason_code == "agent_job_claim_renewed"
    reservation = await process._registry.get_capacity_reservation(
        binding.reservation_id,
        assessed_at=datetime.now(UTC).isoformat(),
    )
    assert reservation is not None
    assert reservation.expires_at > initial_expiry

    await process.release_job()

    released = await process._agent_jobs.get(job_id)
    assert released is not None
    assert released.state is AgentJobState.ADMITTED
    assert released.claim_owner_id is None
    assert released.latest_receipt.reason_code == "agent_worker_job_released"
    reservation = await process._registry.get_capacity_reservation(
        binding.reservation_id,
        assessed_at=datetime.now(UTC).isoformat(),
    )
    assert reservation is not None
    assert reservation.state is WorkerCapacityReservationState.RELEASED
    assert process.health_report().active_jobs == 0
    await process.close()


@pytest.mark.asyncio
async def test_bound_child_loss_requeues_only_control_only_prestart_job(
    tmp_path: Path,
) -> None:
    process = _process(tmp_path)
    await process.start()
    job_id = await _admit_job(process)
    binding = await process.bind_job(job_id)
    assert process._process is not None

    process._process.terminate()
    process._process.join(timeout=2)
    await asyncio.wait_for(process._terminal_event.wait(), timeout=3)

    assert process.snapshot().state is AgentWorkerProcessState.FAILED
    recovered = await process._agent_jobs.get(job_id)
    assert recovered is not None
    assert recovered.state is AgentJobState.ADMITTED
    reservation = await process._registry.get_capacity_reservation(
        binding.reservation_id,
        assessed_at=datetime.now(UTC).isoformat(),
    )
    assert reservation is not None
    assert reservation.state is WorkerCapacityReservationState.RELEASED


@pytest.mark.asyncio
async def test_tampered_encrypted_dispatch_fails_child_and_requeues_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _process(tmp_path, handshake_timeout_seconds=2)
    await process.start()
    job_id = await _admit_job(process)
    original = agent_worker_process_module._parent_job_message

    def tampered_message(*args: object, **kwargs: object) -> dict[str, object]:
        message = original(*args, **kwargs)  # type: ignore[arg-type]
        if message.get("type") == "bind_job":
            envelope = dict(message["dispatch_envelope"])  # type: ignore[arg-type]
            ciphertext = str(envelope["ciphertext_base64"])
            envelope["ciphertext_base64"] = (
                ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B")
            )
            message["dispatch_envelope"] = envelope
        return message

    monkeypatch.setattr(
        agent_worker_process_module,
        "_parent_job_message",
        tampered_message,
    )

    with pytest.raises(AgentWorkerProcessError):
        await process.bind_job(job_id)
    await asyncio.wait_for(process._terminal_event.wait(), timeout=3)

    assert process.snapshot().state is AgentWorkerProcessState.FAILED
    recovered = await process._agent_jobs.get(job_id)
    assert recovered is not None
    assert recovered.state is AgentJobState.ADMITTED


@pytest.mark.asyncio
async def test_incarnation_fence_breaks_dual_renewal_and_requeues_prestart_job(
    tmp_path: Path,
) -> None:
    process = _process(tmp_path)
    started = await process.start()
    job_id = await _admit_job(process)
    binding = await process.bind_job(job_id)

    await process._registry.revoke(
        worker_id=started.worker_id,
        instance_id=started.instance_id,
        epoch=started.epoch,
        reason_code="test_incarnation_fenced",
        revoked_at=datetime.now(UTC).isoformat(),
    )
    await asyncio.wait_for(process._terminal_event.wait(), timeout=3)

    assert process.snapshot().state is AgentWorkerProcessState.FAILED
    assert (
        process.snapshot().failure_code
        == "agent_worker_job_owner_lease_renewal_failed"
    )
    recovered = await process._agent_jobs.get(job_id)
    assert recovered is not None
    assert recovered.state is AgentJobState.ADMITTED
    reservation = await process._registry.get_capacity_reservation(
        binding.reservation_id,
        assessed_at=datetime.now(UTC).isoformat(),
    )
    assert reservation is not None
    assert reservation.state is WorkerCapacityReservationState.FENCED


@pytest.mark.asyncio
async def test_active_worker_blocks_implicit_takeover_then_next_graceful_epoch_advances(
    tmp_path: Path,
) -> None:
    first = _process(tmp_path)
    first_started = await first.start()
    second = _process(tmp_path)

    with pytest.raises(AgentWorkerProcessError, match="不允许隐式接管"):
        await second.start()
    assert second.snapshot().state is AgentWorkerProcessState.FAILED
    active = await first._registry.get_active(first_started.worker_id)
    assert active is not None
    assert active.contract.instance_id == first_started.instance_id

    await first.close()
    third = _process(tmp_path)
    third_started = await third.start()
    assert third_started.epoch == first_started.epoch + 1
    await third.close()


@pytest.mark.asyncio
async def test_child_loss_records_failed_terminal_and_revokes(tmp_path: Path) -> None:
    process = _process(tmp_path)
    started = await process.start()
    assert process._process is not None

    process._process.terminate()
    process._process.join(timeout=2)
    await asyncio.wait_for(process._terminal_event.wait(), timeout=3)

    assert process.snapshot().state is AgentWorkerProcessState.FAILED
    assert process.snapshot().failure_code == "agent_worker_transport_closed"
    assert await process._registry.get_active(started.worker_id) is None
    heartbeat = await process._heartbeats.get_heartbeat(
        workspace_root=tmp_path / "workspace",
        subject_kind=WorkerKind.AGENT.value,
        subject_id=started.worker_id,
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.FAILED
    assert process._connection is None
