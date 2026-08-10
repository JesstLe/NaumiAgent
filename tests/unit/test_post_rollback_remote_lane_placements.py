from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.daemons.worker_contract import (
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
    WorkerPlatform,
    WorkerResourceEnvelope,
    issue_worker_contract,
)
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.post_rollback_behavioral_coverage import (
    EvolutionPostRollbackBehavioralCoverageLaneView,
    EvolutionPostRollbackBehavioralCoverageStore,
    EvolutionPostRollbackBehavioralCoverageView,
    _build_contract,
)
from naumi_agent.evolution.post_rollback_remote_lane_placements import (
    EvolutionPostRollbackRemoteLanePlacementError,
    EvolutionPostRollbackRemoteLanePlacementService,
    EvolutionPostRollbackRemoteLanePlacementStore,
    render_post_rollback_remote_lane_placement,
)
from naumi_agent.evolution.proposal_before_after_evidence import (
    EvolutionProposalBeforeAfterCohort,
    EvolutionProposalBeforeAfterLane,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionPostRollbackRemoteLanePlacementTool,
)

NOW = "2026-08-10T08:00:00+00:00"


def _cohort(batch: str, marker: str) -> EvolutionProposalBeforeAfterCohort:
    return EvolutionProposalBeforeAfterCohort(
        batch_id=batch,
        identity_sha256=marker * 64,
        samples=5,
        samples_sha256=marker * 64,
        passed_samples=5,
        failed_samples=0,
        evaluation_error_samples=0,
        passed_cases=5,
        implementation_failures=0,
        evaluation_errors=0,
        skipped_cases=0,
        duration_ms=5.0,
        observed_tokens=None,
        token_samples=0,
        observed_cost_usd=None,
        cost_samples=0,
    )


def _lane(order: int, kind: str, platform: str, marker: str):
    return EvolutionProposalBeforeAfterLane(
        order=order,
        lane_kind=kind,
        platform=platform,
        suite_id="protocol-hello-core",
        comparison_id=marker * 64,
        comparison_receipt_sha256=marker * 64,
        baseline_id=marker * 64,
        decision="passed",
        statistical_verdict="unchanged",
        statistical_code="unchanged",
        before=_cohort(f"baseline-{marker}", marker),
        after=_cohort(f"candidate-{marker}", marker),
    )


def _coverage(workspace: Path):
    outcome = SimpleNamespace(
        workspace_root=str(workspace),
        outcome_id="evrerollbackout_" + "1" * 24,
        outcome_sha256="2" * 64,
        request_id="evrerollbackreq_" + "3" * 24,
        workbench_session_id="session-placement",
        workbench_proposal_id="proposal-placement",
    )
    verification = SimpleNamespace(
        verification_id="evpostrollback_" + "4" * 24,
        verification_sha256="5" * 64,
        baseline_slot_id="relslot_" + "6" * 24,
        baseline_slot_sha256="7" * 64,
        baseline_manifest_sha256="8" * 64,
        baseline_version="1.2.3",
        baseline_target="macos-arm64",
        baseline_source_commit="9" * 40,
        baseline_source_tree_sha256="a" * 64,
    )
    evidence = SimpleNamespace(
        evidence_id="evbeforeafter_" + "b" * 24,
        evidence_sha256="c" * 64,
        final_evaluation_id="evfinal_" + "d" * 24,
        final_evaluation_sha256="e" * 64,
        lanes=(
            _lane(1, "interventional", "macos", "1"),
            _lane(2, "adversarial", "windows", "2"),
        ),
    )
    contract = _build_contract(
        sources={
            "outcome": outcome,
            "verification": verification,
            "before_after": evidence,
            "baseline_platform": "macos",
        },
        recorded_at=NOW,
    )
    lanes = tuple(
        EvolutionPostRollbackBehavioralCoverageLaneView(
            expected=item,
            status="missing",
            lane_authority=False,
            active_baseline_authority=False,
            dispatch_required=item.remote_target_required,
        )
        for item in contract.lanes
    )
    view = EvolutionPostRollbackBehavioralCoverageView(
        contract=contract,
        lanes=lanes,
        durable_dependencies_valid=True,
        outcome_authority=True,
        runtime_verification_authority=True,
        before_after_authority=True,
        active_baseline_authority=True,
        recorded_lane_count=0,
        missing_lane_count=2,
        stale_lane_count=0,
        remote_dispatch_count=1,
        matrix_ready=False,
    )
    return contract, view


class _CoverageService:
    def __init__(self, view) -> None:
        self.view = view

    async def record(self, *, request_id: str):
        assert request_id == self.view.contract.request_id
        return self.view

    async def inspect(self, *, contract):
        assert contract == self.view.contract
        return self.view


def _worker(worker_id: str, *, system: str = "windows", machine: str = "AMD64"):
    capabilities = tuple(
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
    return issue_worker_contract(
        worker_id=worker_id,
        instance_id=f"{worker_id}-instance",
        epoch=1,
        kind=WorkerKind.TOOL,
        protocol_min=1,
        protocol_max=1,
        software_version="1.0.0",
        platform=WorkerPlatform(
            system=system,
            machine=machine,
            python_implementation="cpython",
            python_version="3.13.5",
        ),
        capabilities=capabilities,
        resources=WorkerResourceEnvelope(
            max_concurrent_jobs=2,
            max_memory_bytes=512 * 1024 * 1024,
            max_cpu_seconds=300,
            max_wall_seconds=300,
            max_output_bytes=16 * 1024 * 1024,
        ),
        isolation=WorkerIsolationContract(True, True, True, True, True, True),
        issued_at=NOW,
    )


async def _fixture(tmp_path: Path):
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    contract, coverage_view = _coverage(workspace)
    db_path = tmp_path / "evolution.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE evolution_proposal_before_after_evidence ("
            "evidence_id TEXT PRIMARY KEY, evidence_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_proposal_before_after_evidence VALUES (?, ?)",
            (contract.before_after_evidence_id, contract.before_after_evidence_sha256),
        )
        db.commit()
    coverage_store = EvolutionPostRollbackBehavioralCoverageStore(db_path)
    await coverage_store.record(contract)
    worker_registry = WorkerRegistryStore(tmp_path / "workers.db")
    windows = _worker("worker-windows")
    await worker_registry.register(windows, registered_at=NOW)
    service = EvolutionPostRollbackRemoteLanePlacementService(
        workspace_root=workspace,
        coverage_store=coverage_store,
        coverage_service=_CoverageService(coverage_view),  # type: ignore[arg-type]
        worker_registry=worker_registry,
        store=EvolutionPostRollbackRemoteLanePlacementStore(db_path),
    )
    return service, worker_registry, contract, coverage_view, windows


@pytest.mark.asyncio
async def test_remote_lane_placement_binds_exact_active_worker_target(
    tmp_path: Path,
) -> None:
    service, registry, contract, coverage, worker = await _fixture(tmp_path)
    remote = coverage.contract.lanes[1]

    first = await service.place(
        request_id=contract.request_id,
        comparison_id=remote.original_comparison_id,
        worker_id=worker.worker_id,
        placed_at="2026-08-10T08:01:00+00:00",
    )
    repeated = await service.place(
        request_id=contract.request_id,
        comparison_id=remote.original_comparison_id,
        worker_id=worker.worker_id,
    )

    assert repeated == first
    assert first.placement_authority
    assert first.placement.release_target == "windows-x64"
    assert first.placement.worker_instance_id == worker.instance_id
    assert first.placement.worker_epoch == 1
    assert not first.placement.health_verified
    assert not first.placement.capacity_reserved
    assert not first.placement.baseline_resolved
    assert not first.execution_authority
    rendered = render_post_rollback_remote_lane_placement(first)
    assert "windows-x64" in rendered
    assert "尚未验证、尚未预留" in rendered

    tool = EvolutionPostRollbackRemoteLanePlacementTool(
        SimpleNamespace(evolution_post_rollback_remote_lane_placement_service=service)
    )
    arguments = {
        "request_id": contract.request_id,
        "comparison_id": remote.original_comparison_id,
        "worker_id": worker.worker_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed
        assert not decision.requires_confirmation
    assert await tool.execute(**arguments) == rendered
    tool_registry = ToolRegistry()
    tool_registry.register(tool)

    class _SlashEngine:
        def __init__(self) -> None:
            self.tool_registry = tool_registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            parsed = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**parsed),
            )

    slash = await execute_slash_command(
        _SlashEngine(),
        f"/evolution outcome-place-behavior {contract.request_id} "
        f"{remote.original_comparison_id} {worker.worker_id}",
    )
    assert first.placement.placement_id in slash

    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_remote_lane_placements "
            "SET placement_json = ? WHERE placement_id = ?",
            ("{}", first.placement.placement_id),
        )
        db.commit()
    tampered = await service.inspect(placement=first.placement)
    assert not tampered.durable_source_valid
    assert not tampered.placement_authority
    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_remote_lane_placements "
            "SET placement_json = ? WHERE placement_id = ?",
            (first.placement.model_dump_json(), first.placement.placement_id),
        )
        db.commit()

    await registry.revoke(
        worker_id=worker.worker_id,
        instance_id=worker.instance_id,
        epoch=worker.epoch,
        reason_code="placement-test-revoke",
        revoked_at="2026-08-10T08:02:00+00:00",
    )
    stale = await service.inspect(placement=first.placement)
    assert not stale.worker_registration_active
    assert not stale.placement_authority


@pytest.mark.asyncio
async def test_remote_lane_placement_rejects_local_or_wrong_platform(
    tmp_path: Path,
) -> None:
    service, registry, contract, coverage, _ = await _fixture(tmp_path)
    local = coverage.contract.lanes[0]
    remote = coverage.contract.lanes[1]

    with pytest.raises(EvolutionPostRollbackRemoteLanePlacementError) as local_error:
        await service.place(
            request_id=contract.request_id,
            comparison_id=local.original_comparison_id,
            worker_id="worker-windows",
        )
    assert local_error.value.code == "post_rollback_placement_lane_not_remote_missing"

    linux = _worker("worker-linux", system="linux", machine="aarch64")
    await registry.register(linux, registered_at=NOW)
    with pytest.raises(EvolutionPostRollbackRemoteLanePlacementError) as mismatch:
        await service.place(
            request_id=contract.request_id,
            comparison_id=remote.original_comparison_id,
            worker_id=linux.worker_id,
        )
    assert mismatch.value.code == "post_rollback_placement_worker_incompatible"

    unsupported = _worker("worker-riscv", machine="riscv64")
    await registry.register(unsupported, registered_at=NOW)
    with pytest.raises(EvolutionPostRollbackRemoteLanePlacementError) as architecture:
        await service.place(
            request_id=contract.request_id,
            comparison_id=remote.original_comparison_id,
            worker_id=unsupported.worker_id,
        )
    assert architecture.value.code == "post_rollback_placement_worker_incompatible"
