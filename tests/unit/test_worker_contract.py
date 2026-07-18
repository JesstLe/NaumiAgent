from __future__ import annotations

from dataclasses import replace

import pytest

from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionDecision,
    WorkerAdmissionReason,
    WorkerAdmissionRequirements,
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
    WorkerResourceEnvelope,
    assess_worker_admission,
    detect_worker_platform,
    issue_worker_contract,
    issue_worker_health_report,
    verify_worker_contract,
    verify_worker_health_report,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind

T0 = "2026-07-19T00:00:00+00:00"
T4 = "2026-07-19T00:00:04+00:00"
T20 = "2026-07-19T00:00:20+00:00"


def _isolation(*, strict: bool = True) -> WorkerIsolationContract:
    return WorkerIsolationContract(
        ephemeral_workspace=strict,
        network_default_deny=strict,
        environment_allowlist=strict,
        resource_limits_enforced=strict,
        process_tree_cancel=strict,
        artifact_digest=strict,
    )


def _resources(*, concurrency: int = 2) -> WorkerResourceEnvelope:
    return WorkerResourceEnvelope(
        max_concurrent_jobs=concurrency,
        max_memory_bytes=512 * 1024 * 1024,
        max_cpu_seconds=60,
        max_wall_seconds=120,
        max_output_bytes=8 * 1024 * 1024,
    )


def _contract():
    return issue_worker_contract(
        worker_id="tool-worker-a",
        instance_id="process-a",
        epoch=3,
        kind=WorkerKind.TOOL,
        protocol_min=1,
        protocol_max=2,
        software_version="0.1.214",
        platform=detect_worker_platform(
            system="Darwin",
            machine="ARM64",
            python_implementation="CPython",
            python_version="3.13.5",
        ),
        capabilities=(
            WorkerCapability.ARTIFACT_DIGEST,
            WorkerCapability.ENVIRONMENT_ALLOWLIST,
            WorkerCapability.NETWORK_POLICY,
            WorkerCapability.PROCESS_TREE_CANCEL,
            WorkerCapability.RESOURCE_LIMITS,
            WorkerCapability.SHELL_NON_PTY,
            WorkerCapability.WORKSPACE_EPHEMERAL,
        ),
        resources=_resources(),
        isolation=_isolation(),
        issued_at=T0,
    )


def _heartbeat(
    *,
    phase: HarnessHeartbeatPhase = HarnessHeartbeatPhase.RUNNING,
    observed_at: str = T0,
    kind: HarnessRunKind = HarnessRunKind.TOOL,
) -> HarnessHeartbeat:
    return HarnessHeartbeat(
        workspace_root="/workspace",
        subject_kind=kind,
        subject_id="tool-worker-a",
        instance_id="process-a",
        epoch=3,
        sequence=7,
        phase=phase,
        observed_at=observed_at,
        timeout_seconds=10,
        detail_code="ready",
    )


def _requirements(**updates):
    values = {
        "kind": WorkerKind.TOOL,
        "protocol_version": 2,
        "capabilities": (
            WorkerCapability.ARTIFACT_DIGEST,
            WorkerCapability.PROCESS_TREE_CANCEL,
            WorkerCapability.SHELL_NON_PTY,
        ),
        "allowed_platforms": ("darwin", "linux"),
        "min_memory_bytes": 256 * 1024 * 1024,
        "min_cpu_seconds": 30,
        "min_wall_seconds": 60,
        "min_output_bytes": 1024 * 1024,
        "isolation": _isolation(),
    }
    values.update(updates)
    return WorkerAdmissionRequirements(**values)


def test_contract_and_health_report_are_canonical_and_tamper_evident() -> None:
    contract = _contract()
    report = issue_worker_health_report(
        contract=contract,
        heartbeat=_heartbeat(),
        active_jobs=1,
        accepting_jobs=True,
    )

    assert contract.capabilities == tuple(sorted(contract.capabilities, key=str))
    assert verify_worker_contract(contract)
    assert verify_worker_health_report(report)
    assert not verify_worker_contract(replace(contract, software_version="0.1.215"))
    assert not verify_worker_health_report(replace(report, active_jobs=0))


def test_admission_accepts_only_matching_healthy_worker_with_capacity() -> None:
    contract = _contract()
    report = issue_worker_health_report(
        contract=contract,
        heartbeat=_heartbeat(),
        active_jobs=1,
        accepting_jobs=True,
    )

    result = assess_worker_admission(contract, report, _requirements(), now=T4)

    assert result.decision is WorkerAdmissionDecision.ADMITTED
    assert result.reasons == (WorkerAdmissionReason.ADMITTED,)
    assert result.admitted is True
    assert result.checked_at == T4


def test_admission_reports_all_contract_requirement_failures() -> None:
    contract = replace(_contract(), software_version="0.1.215")
    report = issue_worker_health_report(
        contract=contract,
        heartbeat=_heartbeat(kind=HarnessRunKind.AGENT),
        active_jobs=0,
        accepting_jobs=True,
    )
    requirements = _requirements(
        kind=WorkerKind.AGENT,
        protocol_version=3,
        capabilities=(WorkerCapability.BROWSER_PROFILE_ISOLATION,),
        allowed_platforms=("linux",),
        min_memory_bytes=1024 * 1024 * 1024,
        isolation=replace(_isolation(), ephemeral_workspace=False),
    )

    result = assess_worker_admission(contract, report, requirements, now=T4)

    assert result.admitted is False
    assert set(result.reasons) >= {
        WorkerAdmissionReason.CONTRACT_TAMPERED,
        WorkerAdmissionReason.IDENTITY_MISMATCH,
        WorkerAdmissionReason.KIND_MISMATCH,
        WorkerAdmissionReason.PROTOCOL_INCOMPATIBLE,
        WorkerAdmissionReason.PLATFORM_MISMATCH,
        WorkerAdmissionReason.CAPABILITY_MISSING,
        WorkerAdmissionReason.RESOURCE_INSUFFICIENT,
    }


@pytest.mark.parametrize(
    ("report_updates", "heartbeat_updates", "now", "reason"),
    [
        ({"active_jobs": 2}, {}, T4, WorkerAdmissionReason.CAPACITY_EXHAUSTED),
        (
            {"accepting_jobs": False},
            {},
            T4,
            WorkerAdmissionReason.NOT_ACCEPTING_JOBS,
        ),
        ({}, {"phase": HarnessHeartbeatPhase.DRAINING}, T4, WorkerAdmissionReason.HEALTH_NOT_READY),
        ({}, {}, T20, WorkerAdmissionReason.HEALTH_NOT_READY),
    ],
)
def test_admission_blocks_capacity_drain_and_stale_health(
    report_updates,
    heartbeat_updates,
    now,
    reason,
) -> None:
    contract = _contract()
    heartbeat = replace(_heartbeat(), **heartbeat_updates)
    report = issue_worker_health_report(
        contract=contract,
        heartbeat=heartbeat,
        active_jobs=report_updates.get("active_jobs", 0),
        accepting_jobs=report_updates.get("accepting_jobs", True),
    )

    result = assess_worker_admission(contract, report, _requirements(), now=now)

    assert result.admitted is False
    assert reason in result.reasons


def test_admission_rejects_health_from_another_contract_generation() -> None:
    contract = _contract()
    prior = replace(contract, epoch=2, contract_sha256="0" * 64)
    prior = issue_worker_contract(
        worker_id=prior.worker_id,
        instance_id="process-old",
        epoch=2,
        kind=prior.kind,
        protocol_min=prior.protocol_min,
        protocol_max=prior.protocol_max,
        software_version=prior.software_version,
        platform=prior.platform,
        capabilities=prior.capabilities,
        resources=prior.resources,
        isolation=prior.isolation,
        issued_at=prior.issued_at,
    )
    report = issue_worker_health_report(
        contract=prior,
        heartbeat=replace(_heartbeat(), instance_id="process-old", epoch=2),
        active_jobs=0,
        accepting_jobs=True,
    )

    result = assess_worker_admission(contract, report, _requirements(), now=T4)

    assert WorkerAdmissionReason.IDENTITY_MISMATCH in result.reasons


def test_admission_blocks_tampered_capacity_report() -> None:
    contract = _contract()
    report = issue_worker_health_report(
        contract=contract,
        heartbeat=_heartbeat(),
        active_jobs=1,
        accepting_jobs=True,
    )

    result = assess_worker_admission(
        contract,
        replace(report, active_jobs=0),
        _requirements(),
        now=T4,
    )

    assert result.reasons == (WorkerAdmissionReason.HEALTH_TAMPERED,)


def test_platform_and_resource_contracts_validate_cross_platform_boundaries() -> None:
    windows = detect_worker_platform(
        system="Win32",
        machine="AMD64",
        python_implementation="CPython",
        python_version="3.13.5",
    )
    assert windows.system == "windows"
    assert windows.machine == "amd64"
    linux = detect_worker_platform(
        system="Linux",
        machine="x86_64",
        python_implementation="PyPy",
        python_version="3.11.9",
    )
    assert linux.system == "linux"
    assert linux.python_implementation == "pypy"

    with pytest.raises(ValueError, match="仅支持"):
        detect_worker_platform(
            system="FreeBSD",
            machine="x86_64",
            python_implementation="CPython",
            python_version="3.13.5",
        )
    with pytest.raises(ValueError, match="max_concurrent_jobs"):
        _resources(concurrency=0)
    with pytest.raises(ValueError, match="allowed_platforms"):
        _requirements(allowed_platforms=("windows", "linux"))
    with pytest.raises(ValueError, match="capabilities"):
        _requirements(
            capabilities=(
                WorkerCapability.SHELL_NON_PTY,
                WorkerCapability.SHELL_NON_PTY,
            )
        )


def test_isolation_claims_are_explicit_and_fail_closed() -> None:
    contract = _contract()
    weak_contract = issue_worker_contract(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        kind=contract.kind,
        protocol_min=contract.protocol_min,
        protocol_max=contract.protocol_max,
        software_version=contract.software_version,
        platform=contract.platform,
        capabilities=contract.capabilities,
        resources=contract.resources,
        isolation=replace(contract.isolation, network_default_deny=False),
        issued_at=contract.issued_at,
    )
    report = issue_worker_health_report(
        contract=weak_contract,
        heartbeat=_heartbeat(),
        active_jobs=0,
        accepting_jobs=True,
    )

    result = assess_worker_admission(weak_contract, report, _requirements(), now=T4)

    assert result.reasons == (WorkerAdmissionReason.ISOLATION_INSUFFICIENT,)

    with pytest.raises(ValueError, match="缺少对应能力"):
        replace(
            contract,
            capabilities=(WorkerCapability.SHELL_NON_PTY,),
            contract_sha256="0" * 64,
        )
