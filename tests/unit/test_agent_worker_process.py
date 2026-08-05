from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from naumi_agent.daemons.agent_worker_process import (
    AgentWorkerProcessError,
    AgentWorkerProcessState,
    AuthenticatedAgentWorkerProcess,
    _child_message,
    _transport_address,
    _validate_child_message,
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
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.store import HarnessStore
from naumi_agent.ui.doctor import _worker_authority_check


def _process(tmp_path: Path, **overrides: object) -> AuthenticatedAgentWorkerProcess:
    values: dict[str, object] = {
        "worker_registry": WorkerRegistryStore(tmp_path / "worker-registry.db"),
        "heartbeat_store": HarnessStore(tmp_path / "harness.db"),
        "workspace_root": tmp_path / "workspace",
        "runtime_dir": tmp_path / "runtime" / "agent-worker",
        "software_version": "0.1.214",
        "max_concurrent_jobs": 4,
        "heartbeat_interval_seconds": 0.05,
        "heartbeat_timeout_seconds": 3,
        "handshake_timeout_seconds": 5,
        "shutdown_timeout_seconds": 3,
    }
    values.update(overrides)
    return AuthenticatedAgentWorkerProcess(**values)  # type: ignore[arg-type]


def test_constructor_is_lazy_and_rejects_unsafe_configuration(tmp_path: Path) -> None:
    process = _process(tmp_path)

    assert process.snapshot().state is AgentWorkerProcessState.CREATED
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "worker-registry.db").exists()
    assert not (tmp_path / "harness.db").exists()

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
    assert process.contract.capabilities == (WorkerCapability.AGENT_CONTROL_TRANSPORT,)
    registration = await process._registry.get_active(started.worker_id)
    assert registration is not None
    assert registration.contract.contract_sha256 == started.contract_sha256

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
    assert "控制通道就绪、任务调度未开放" in check.detail
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
    assert not tuple((tmp_path / "runtime" / "agent-worker").glob("*.sock"))
    if os.name != "nt":
        assert stat.S_IMODE(
            (tmp_path / "runtime" / "agent-worker").stat().st_mode
        ) == 0o700


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
