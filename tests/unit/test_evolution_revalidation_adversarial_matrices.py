from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.daemons.worker_contract import (
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
    WorkerResourceEnvelope,
    detect_worker_platform,
    issue_worker_contract,
    issue_worker_health_report,
)
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.revalidation_adversarial_matrices import (
    EvolutionRevalidationAdversarialMatrixError,
    EvolutionRevalidationAdversarialMatrixService,
    EvolutionRevalidationAdversarialMatrixStore,
)
from naumi_agent.harness.eval_identity import capture_eval_platform_identity
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from tests.unit.test_evolution_revalidation_adversarial_cohorts import _cohort
from tests.unit.test_evolution_revalidation_adversarial_samples import (
    _executor as _sample_executor,
)
from tests.unit.test_evolution_revalidation_interventional_samples import (
    _executor_scenario,
)

T0 = "2026-07-19T00:00:00+00:00"
T1 = "2026-07-19T00:00:01+00:00"


def _service(sample, worker_registry):
    db_path = sample.receipt_store._db_path
    return EvolutionRevalidationAdversarialMatrixService(
        workspace_root=sample.workspace_root,
        contract_service=sample.contract_service,
        cohort_store=_cohort(sample, sample.harness_store).receipt_store,
        matrix_store=EvolutionRevalidationAdversarialMatrixStore(db_path),
        worker_registry=worker_registry,
    )


def _healthy_worker(platform: str, max_wall_seconds: int, *, active_jobs: int = 0):
    system = "Darwin" if platform == "macos" else platform.title()
    capabilities = tuple(sorted((
        WorkerCapability.ARTIFACT_DIGEST,
        WorkerCapability.ENVIRONMENT_ALLOWLIST,
        WorkerCapability.NETWORK_POLICY,
        WorkerCapability.PROCESS_TREE_CANCEL,
        WorkerCapability.RESOURCE_LIMITS,
        WorkerCapability.SHELL_NON_PTY,
        WorkerCapability.WORKSPACE_EPHEMERAL,
    ), key=str))
    contract = issue_worker_contract(
        worker_id=f"matrix-{platform}-worker",
        instance_id="process-1",
        epoch=1,
        kind=WorkerKind.TOOL,
        protocol_min=1,
        protocol_max=1,
        software_version="0.1.214",
        platform=detect_worker_platform(
            system=system,
            machine="x86_64",
            python_implementation="CPython",
            python_version="3.13.5",
        ),
        capabilities=capabilities,
        resources=WorkerResourceEnvelope(
            max_concurrent_jobs=2,
            max_memory_bytes=512 * 1024 * 1024,
            max_cpu_seconds=7_200,
            max_wall_seconds=max_wall_seconds,
            max_output_bytes=8 * 1024 * 1024,
        ),
        isolation=WorkerIsolationContract(True, True, True, True, True, True),
        issued_at=T0,
    )
    report = issue_worker_health_report(
        contract=contract,
        heartbeat=HarnessHeartbeat(
            workspace_root="/workspace",
            subject_kind=HarnessRunKind.TOOL,
            subject_id=contract.worker_id,
            instance_id=contract.instance_id,
            epoch=contract.epoch,
            sequence=1,
            phase=HarnessHeartbeatPhase.RUNNING,
            observed_at=T1,
            timeout_seconds=30,
            detail_code="matrix_ready",
        ),
        active_jobs=active_jobs,
        accepting_jobs=True,
    )
    return contract, report


@pytest.mark.asyncio
async def test_matrix_distinguishes_pending_and_live_admitted_worker(tmp_path: Path):
    base, contract, _parent, harness_store, _kernel, _state = await _executor_scenario(
        tmp_path
    )
    sample = _sample_executor(base, harness_store)
    registry = WorkerRegistryStore(tmp_path / ".naumi" / "workers.db")
    service = _service(sample, registry)
    platform = contract.required_platforms[0]

    pending = await service.inspect(contract_id=contract.contract_id, assessed_at=T1)
    assert pending.pending_platforms == (platform,)
    assert not pending.matrix_complete
    assert not pending.dispatch_authority

    worker, report = _healthy_worker(platform, contract.max_total_duration_seconds)
    await registry.register(worker, registered_at=T0)
    runnable = await service.inspect(
        contract_id=contract.contract_id,
        worker_health_reports=(report,),
        assessed_at=T1,
    )
    assert runnable.runnable_platforms == (platform,)
    assert runnable.lanes[0].worker_contract_sha256 == worker.contract_sha256
    assert not runnable.matrix_complete
    assert not runnable.dispatch_authority

    _worker, saturated = _healthy_worker(
        platform, contract.max_total_duration_seconds, active_jobs=2
    )
    blocked = await service.inspect(
        contract_id=contract.contract_id,
        worker_health_reports=(saturated,),
        assessed_at=T1,
    )
    assert blocked.pending_platforms == (platform,)
    assert blocked.lanes[0].reason_codes == ("worker_capacity_exhausted",)


@pytest.mark.asyncio
async def test_complete_matrix_is_persisted_and_revalidated(tmp_path: Path):
    base, contract, parent, harness_store, _kernel, _state = await _executor_scenario(
        tmp_path
    )
    sample = _sample_executor(base, harness_store)
    platform = capture_eval_platform_identity().system
    cohort = _cohort(sample, harness_store)
    cohort_receipt = await cohort.execute(
        contract_id=contract.contract_id,
        platform=platform,
        parent_receipt_id=parent.receipt_id,
    )
    service = _service(sample, WorkerRegistryStore(tmp_path / ".naumi" / "workers.db"))

    status = await service.inspect(contract_id=contract.contract_id, assessed_at=T1)
    replay = await service.inspect(
        contract_id=contract.contract_id,
        assessed_at="2026-07-19T00:00:02+00:00",
    )

    assert status == replay
    assert status.matrix_complete
    assert status.completed_platforms == (platform,)
    assert status.lanes[0].cohort_receipt_sha256 == cohort_receipt.receipt_sha256
    assert not status.comparison_authority and not status.promotion_authority


@pytest.mark.asyncio
async def test_matrix_rejects_stale_runtime_contract(tmp_path: Path):
    base, contract, _parent, harness_store, _kernel, state = await _executor_scenario(
        tmp_path
    )
    sample = _sample_executor(base, harness_store)
    state.current = False
    service = _service(sample, WorkerRegistryStore(tmp_path / ".naumi" / "workers.db"))

    with pytest.raises(EvolutionRevalidationAdversarialMatrixError) as captured:
        await service.inspect(contract_id=contract.contract_id, assessed_at=T1)
    assert captured.value.code == "fresh_adversarial_matrix_contract_not_ready"
