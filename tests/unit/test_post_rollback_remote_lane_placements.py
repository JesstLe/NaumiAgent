from __future__ import annotations

import asyncio
import base64
import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.daemons.authenticated_worker_identity import (
    AuthenticatedWorkerIdentityAuthority,
    AuthenticatedWorkerIdentityStore,
    issue_authenticated_worker_identity,
)
from naumi_agent.daemons.worker_contract import (
    WorkerCapability,
    WorkerIsolationContract,
    WorkerKind,
    WorkerPlatform,
    WorkerResourceEnvelope,
    issue_worker_contract,
    issue_worker_health_report,
)
from naumi_agent.daemons.worker_registry import (
    WorkerCapacityReservationState,
    WorkerRegistryStore,
)
from naumi_agent.evolution.post_rollback_behavioral_coverage import (
    EvolutionPostRollbackBehavioralCoverageLaneView,
    EvolutionPostRollbackBehavioralCoverageStore,
    EvolutionPostRollbackBehavioralCoverageView,
    _build_contract,
)
from naumi_agent.evolution.post_rollback_remote_claims import (
    EvolutionPostRollbackRemoteClaimError,
    EvolutionPostRollbackRemoteClaimService,
    EvolutionPostRollbackRemoteClaimStore,
    render_post_rollback_remote_claim,
)
from naumi_agent.evolution.post_rollback_remote_dispatches import (
    EvolutionPostRollbackRemoteDispatchError,
    EvolutionPostRollbackRemoteDispatchService,
    EvolutionPostRollbackRemoteDispatchStore,
    render_post_rollback_remote_dispatch,
)
from naumi_agent.evolution.post_rollback_remote_lane_placements import (
    EvolutionPostRollbackRemoteLanePlacementError,
    EvolutionPostRollbackRemoteLanePlacementService,
    EvolutionPostRollbackRemoteLanePlacementStore,
    render_post_rollback_remote_lane_placement,
)
from naumi_agent.evolution.post_rollback_target_baselines import (
    EvolutionPostRollbackTargetBaselineError,
    EvolutionPostRollbackTargetBaselineService,
    EvolutionPostRollbackTargetBaselineStore,
    render_post_rollback_target_baseline,
)
from naumi_agent.evolution.proposal_before_after_evidence import (
    EvolutionProposalBeforeAfterCohort,
    EvolutionProposalBeforeAfterLane,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.release.build_attestations import (
    ReleaseBuildContext,
    ReleaseBuildSigner,
    ReleaseTrustedBuilderKey,
    create_release_build_attestation,
    create_release_build_trust_policy,
)
from naumi_agent.release.channel_catalog import (
    ReleaseChannelCatalogSigner,
    ReleaseChannelCatalogStore,
    ReleaseChannelEntry,
    ReleaseTrustedChannelKey,
    create_release_channel_trust_policy,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionPostRollbackRemoteClaimTool,
    EvolutionPostRollbackRemoteDispatchTool,
    EvolutionPostRollbackRemoteLanePlacementTool,
    EvolutionPostRollbackTargetBaselineTool,
)

NOW = "2026-08-10T08:00:00+00:00"
T0 = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


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


def _worker(
    worker_id: str,
    *,
    system: str = "windows",
    machine: str = "AMD64",
    epoch: int = 1,
    issued_at: str = NOW,
):
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
        instance_id=f"{worker_id}-instance-{epoch}",
        epoch=epoch,
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
        issued_at=issued_at,
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


async def _catalog(
    tmp_path: Path,
    *,
    source_commit: str = "9" * 40,
    source_tree_sha256: str = "a" * 64,
):
    def encode(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    channel_signer = ReleaseChannelCatalogSigner.from_private_key_base64(
        signer_id="naumi-release-channel",
        key_id="channel-2026-q3",
        key_generation=1,
        private_key_base64=encode(bytes(range(32))),
    )
    build_signer = ReleaseBuildSigner.from_private_key_base64(
        builder_id="naumi-github-release",
        key_id="release-2026-q3",
        key_generation=1,
        private_key_base64=encode(b"b" * 32),
    )
    channel_key = ReleaseTrustedChannelKey(
        identity=channel_signer.identity,
        state="active",
        channels=("stable",),
        valid_from=(T0 - timedelta(days=1)).isoformat(),
        valid_until=(T0 + timedelta(days=7)).isoformat(),
    )
    build_key = ReleaseTrustedBuilderKey(
        identity=build_signer.identity,
        state="active",
        valid_from=(T0 - timedelta(days=1)).isoformat(),
        valid_until=(T0 + timedelta(days=7)).isoformat(),
    )
    policies = [
        create_release_channel_trust_policy(
            (channel_key,),
            archive_origins=("https://downloads.naumi.dev",),
        ),
        create_release_build_trust_policy((build_key,)),
    ]
    target = "windows-x64"
    root = tmp_path / "catalog-source"
    root.mkdir()
    archive = root / "naumi-1.2.3-windows-x64.zip"
    archive.write_bytes(b"source-free-windows-x64")
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "NaumiAgent",
                "version": "1.2.3",
                "target": target,
                "source_commit": source_commit,
                "source_tree_sha256": source_tree_sha256,
                "files": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    attestation = create_release_build_attestation(
        signer=build_signer,
        context=ReleaseBuildContext(
            repository="JesstLe/NaumiAgent",
            workflow_ref=(
                "JesstLe/NaumiAgent/.github/workflows/"
                "release-binaries.yml@refs/tags/v1.2.3"
            ),
            run_id="123",
            run_attempt=1,
            built_at=(T0 - timedelta(hours=1)).isoformat(),
        ),
        manifest_path=manifest,
        archive_path=archive,
    )
    entry = ReleaseChannelEntry(
        target=target,
        version="1.2.3",
        release_generation=123,
        archive_path=f"releases/v1.2.3/{archive.name}",
        archive_name=archive.name,
        archive_sha256=attestation.payload.archive_sha256,
        archive_size_bytes=archive.stat().st_size,
        manifest_sha256=attestation.payload.manifest_sha256,
        build_attestation=attestation,
    )
    catalog = channel_signer.issue(
        channel="stable",
        entries=(entry,),
        previous=None,
        generated_at=T0.isoformat(),
        valid_from=(T0 + timedelta(seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=1)).isoformat(),
    )
    store = ReleaseChannelCatalogStore(
        tmp_path / "release-channel.db",
        channel_trust_policy_provider=lambda: policies[0],
        build_trust_policy_provider=lambda: policies[1],
        clock=lambda: T0 + timedelta(seconds=2),
    )
    await store.record(catalog)
    return store, policies, channel_signer, build_signer


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


@pytest.mark.asyncio
async def test_target_baseline_resolves_exact_source_and_revokes_with_catalog(
    tmp_path: Path,
) -> None:
    placement_service, _, contract, coverage, worker = await _fixture(tmp_path)
    remote = coverage.contract.lanes[1]
    placement = await placement_service.place(
        request_id=contract.request_id,
        comparison_id=remote.original_comparison_id,
        worker_id=worker.worker_id,
        placed_at="2026-08-10T08:01:00+00:00",
    )
    catalog_store, policies, channel_signer, build_signer = await _catalog(tmp_path)
    service = EvolutionPostRollbackTargetBaselineService(
        workspace_root=Path(contract.workspace_root),
        coverage_service=placement_service.coverage_service,
        placement_store=placement_service.store,
        placement_service=placement_service,
        catalog_store=catalog_store,
        store=EvolutionPostRollbackTargetBaselineStore(placement_service.store.db_path),
    )

    first = await service.resolve(
        request_id=contract.request_id,
        comparison_id=remote.original_comparison_id,
        channel="stable",
        resolved_at="2026-08-10T08:03:00+00:00",
    )
    repeated = await service.resolve(
        request_id=contract.request_id,
        comparison_id=remote.original_comparison_id,
        channel="stable",
    )

    assert repeated == first
    assert first.baseline_resolution_authority
    assert first.download_input_authority
    assert first.baseline.release_target == "windows-x64"
    assert first.baseline.local_baseline_version == "1.2.3"
    build = first.baseline.channel_resolution.entry.build_attestation.payload
    assert build.source_commit == contract.baseline_source_commit
    assert build.source_tree_sha256 == contract.baseline_source_tree_sha256
    encoded_baseline = first.baseline.model_dump_json()
    assert "private_key_base64" not in encoded_baseline
    assert base64.b64encode(b"b" * 32).decode("ascii") not in encoded_baseline
    assert not first.baseline.transport_delivered
    assert not first.execution_authority
    rendered = render_post_rollback_target_baseline(first)
    assert "windows-x64" in rendered
    assert "未下载、未安装" in rendered

    tool = EvolutionPostRollbackTargetBaselineTool(
        SimpleNamespace(evolution_post_rollback_target_baseline_service=service)
    )
    arguments = {
        "request_id": contract.request_id,
        "comparison_id": remote.original_comparison_id,
        "channel": "stable",
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
        f"/evolution outcome-resolve-behavior {contract.request_id} "
        f"{remote.original_comparison_id} stable",
    )
    assert first.baseline.baseline_resolution_id in slash

    revoked_channel_key = ReleaseTrustedChannelKey(
        identity=channel_signer.identity,
        state="revoked",
        channels=("stable",),
        valid_from=(T0 - timedelta(days=1)).isoformat(),
        valid_until=(T0 + timedelta(days=7)).isoformat(),
        revoked_at=(T0 + timedelta(seconds=3)).isoformat(),
    )
    policies[0] = create_release_channel_trust_policy(
        (revoked_channel_key,),
        archive_origins=("https://downloads.naumi.dev",),
    )
    stale = await service.inspect(baseline=first.baseline)
    assert not stale.catalog_authority
    assert not stale.baseline_resolution_authority
    assert not stale.download_input_authority

    # Keep the builder object live to prove no private key was persisted in the artifact.
    assert build_signer.identity == build.builder
    assert placement.placement.placement_id == first.baseline.placement_id


@pytest.mark.asyncio
async def test_target_baseline_rejects_same_version_with_different_source(
    tmp_path: Path,
) -> None:
    placement_service, _, contract, coverage, worker = await _fixture(tmp_path)
    remote = coverage.contract.lanes[1]
    await placement_service.place(
        request_id=contract.request_id,
        comparison_id=remote.original_comparison_id,
        worker_id=worker.worker_id,
        placed_at="2026-08-10T08:01:00+00:00",
    )
    catalog_store, _, _, _ = await _catalog(
        tmp_path,
        source_commit="f" * 40,
        source_tree_sha256="e" * 64,
    )
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    wrong_workspace_service = EvolutionPostRollbackTargetBaselineService(
        workspace_root=other_workspace,
        coverage_service=placement_service.coverage_service,
        placement_store=placement_service.store,
        placement_service=placement_service,
        catalog_store=catalog_store,
        store=EvolutionPostRollbackTargetBaselineStore(placement_service.store.db_path),
    )
    with pytest.raises(EvolutionPostRollbackTargetBaselineError) as workspace_mismatch:
        await wrong_workspace_service.resolve(
            request_id=contract.request_id,
            comparison_id=remote.original_comparison_id,
            channel="stable",
        )
    assert (
        workspace_mismatch.value.code
        == "post_rollback_target_baseline_workspace_mismatch"
    )

    service = EvolutionPostRollbackTargetBaselineService(
        workspace_root=Path(contract.workspace_root),
        coverage_service=placement_service.coverage_service,
        placement_store=placement_service.store,
        placement_service=placement_service,
        catalog_store=catalog_store,
        store=EvolutionPostRollbackTargetBaselineStore(placement_service.store.db_path),
    )

    with pytest.raises(EvolutionPostRollbackTargetBaselineError) as mismatch:
        await service.resolve(
            request_id=contract.request_id,
            comparison_id=remote.original_comparison_id,
            channel="stable",
        )
    assert mismatch.value.code == "post_rollback_target_baseline_not_equivalent"


class _EvidenceStore:
    def __init__(self, evidence) -> None:
        self.evidence = evidence

    async def get_by_outcome(self, outcome_id: str):
        return self.evidence if outcome_id == self.evidence.outcome_id else None


class _FailingRemoteDispatchStore(EvolutionPostRollbackRemoteDispatchStore):
    async def record(self, dispatch):
        del dispatch
        raise EvolutionPostRollbackRemoteDispatchError(
            "post_rollback_remote_dispatch_test_store_failure",
            "测试注入的 durable store failure。",
        )


async def _dispatch_fixture(tmp_path: Path, *, failing_store: bool = False):
    placement_service, registry, contract, coverage, worker = await _fixture(tmp_path)
    shutil.copytree(
        Path("docs/harness/evals").resolve(),
        Path(contract.workspace_root) / "docs" / "harness" / "evals",
    )
    remote = coverage.contract.lanes[1]
    placement = await placement_service.place(
        request_id=contract.request_id,
        comparison_id=remote.original_comparison_id,
        worker_id=worker.worker_id,
        placed_at="2026-08-10T08:01:00+00:00",
    )
    catalog_store, _, _, _ = await _catalog(tmp_path)
    target_service = EvolutionPostRollbackTargetBaselineService(
        workspace_root=Path(contract.workspace_root),
        coverage_service=placement_service.coverage_service,
        placement_store=placement_service.store,
        placement_service=placement_service,
        catalog_store=catalog_store,
        store=EvolutionPostRollbackTargetBaselineStore(placement_service.store.db_path),
    )
    baseline = await target_service.resolve(
        request_id=contract.request_id,
        comparison_id=remote.original_comparison_id,
        channel="stable",
        resolved_at="2026-08-10T08:03:00+00:00",
    )
    evidence = SimpleNamespace(
        evidence_id=contract.before_after_evidence_id,
        evidence_sha256=contract.before_after_evidence_sha256,
        workspace_root=contract.workspace_root,
        outcome_id=contract.outcome_id,
        outcome_sha256=contract.outcome_sha256,
        request_id=contract.request_id,
        final_evaluation_id=contract.final_evaluation_id,
        final_evaluation_sha256=contract.final_evaluation_sha256,
        lanes=(
            _lane(1, "interventional", "macos", "1"),
            _lane(2, "adversarial", "windows", "2"),
        ),
    )
    heartbeat = HarnessHeartbeat(
        workspace_root=contract.workspace_root,
        subject_kind=HarnessRunKind.TOOL,
        subject_id=worker.worker_id,
        instance_id=worker.instance_id,
        epoch=worker.epoch,
        sequence=1,
        phase=HarnessHeartbeatPhase.RUNNING,
        observed_at="2026-08-10T08:03:01+00:00",
        timeout_seconds=60,
        detail_code="ready",
    )
    health = issue_worker_health_report(
        contract=worker,
        heartbeat=heartbeat,
        active_jobs=0,
        accepting_jobs=True,
    )
    await registry.record_health_report(
        health,
        recorded_at="2026-08-10T08:03:01+00:00",
    )
    store_type = (
        _FailingRemoteDispatchStore
        if failing_store
        else EvolutionPostRollbackRemoteDispatchStore
    )
    service = EvolutionPostRollbackRemoteDispatchService(
        workspace_root=Path(contract.workspace_root),
        target_baseline_service=target_service,
        evidence_store=_EvidenceStore(evidence),  # type: ignore[arg-type]
        worker_registry=registry,
        store=store_type(placement_service.store.db_path),
    )
    return service, registry, contract, remote, worker, placement, baseline


@pytest.mark.asyncio
async def test_remote_dispatch_reserves_exact_capacity_and_revokes_dynamically(
    tmp_path: Path,
) -> None:
    service, registry, contract, remote, worker, placement, baseline = (
        await _dispatch_fixture(tmp_path)
    )
    queued_at = "2026-08-10T08:03:02+00:00"

    peer = EvolutionPostRollbackRemoteDispatchService(
        workspace_root=Path(contract.workspace_root),
        target_baseline_service=service.target_baseline_service,
        evidence_store=service.evidence_store,
        worker_registry=registry,
        store=EvolutionPostRollbackRemoteDispatchStore(service.store.db_path),
    )
    first, repeated = await asyncio.gather(
        service.queue(
            request_id=contract.request_id,
            comparison_id=remote.original_comparison_id,
            channel="stable",
            queued_at=queued_at,
        ),
        peer.queue(
            request_id=contract.request_id,
            comparison_id=remote.original_comparison_id,
            channel="stable",
            queued_at=queued_at,
        ),
    )

    assert repeated == first
    assert first.dispatch_authority
    assert first.dispatch.placement_id == placement.placement.placement_id
    assert first.dispatch.baseline_resolution_id == baseline.baseline.baseline_resolution_id
    assert first.dispatch.suite_id == "protocol-hello-core"
    assert first.dispatch.repetitions == 5
    assert first.dispatch.case_execution_budget_ms == 600
    assert first.dispatch.total_execution_budget_ms == 3_000
    assert first.dispatch.reservation_ttl_seconds == 13
    assert first.dispatch.attempt == 1
    assert not first.claim_authority
    assert not first.execution_authority
    assert not first.result_authority
    reservation = await registry.get_capacity_reservation(
        first.dispatch.reservation_id,
        assessed_at=queued_at,
    )
    assert reservation is not None
    assert reservation.state is WorkerCapacityReservationState.ACTIVE
    assert reservation.worker_id == worker.worker_id
    rendered = render_post_rollback_remote_dispatch(first)
    assert "尚未被 Worker claim" in rendered

    class _DispatchService:
        async def queue(self, **arguments):
            assert arguments == {
                "request_id": contract.request_id,
                "comparison_id": remote.original_comparison_id,
                "channel": "stable",
            }
            return first

    tool = EvolutionPostRollbackRemoteDispatchTool(
        SimpleNamespace(
            evolution_post_rollback_remote_dispatch_service=_DispatchService()
        )
    )
    arguments = {
        "request_id": contract.request_id,
        "comparison_id": remote.original_comparison_id,
        "channel": "stable",
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
        f"/evolution outcome-dispatch-behavior {contract.request_id} "
        f"{remote.original_comparison_id} stable",
    )
    assert first.dispatch.dispatch_id in slash
    with pytest.raises(EvolutionPostRollbackRemoteDispatchError) as regression:
        await service.inspect(
            dispatch=first.dispatch,
            assessed_at="2026-08-10T08:03:01+00:00",
        )
    assert (
        regression.value.code
        == "post_rollback_remote_dispatch_assessment_before_queue"
    )

    draining_heartbeat = HarnessHeartbeat(
        workspace_root=contract.workspace_root,
        subject_kind=HarnessRunKind.TOOL,
        subject_id=worker.worker_id,
        instance_id=worker.instance_id,
        epoch=worker.epoch,
        sequence=2,
        phase=HarnessHeartbeatPhase.DRAINING,
        observed_at="2026-08-10T08:03:03+00:00",
        timeout_seconds=60,
        detail_code="draining",
    )
    await registry.record_health_report(
        issue_worker_health_report(
            contract=worker,
            heartbeat=draining_heartbeat,
            active_jobs=1,
            accepting_jobs=False,
        ),
        recorded_at="2026-08-10T08:03:03+00:00",
    )
    stale = await service.inspect(
        dispatch=first.dispatch,
        assessed_at="2026-08-10T08:03:04+00:00",
    )
    assert not stale.health_authority
    assert not stale.dispatch_authority


@pytest.mark.asyncio
async def test_remote_dispatch_rejects_capacity_exhaustion_without_artifact(
    tmp_path: Path,
) -> None:
    service, registry, contract, remote, worker, _, baseline = await _dispatch_fixture(
        tmp_path
    )
    for index in range(2):
        await registry.reserve_capacity(
            reservation_id=f"occupied-{index}",
            worker_id=worker.worker_id,
            instance_id=worker.instance_id,
            epoch=worker.epoch,
            job_id=f"occupied-job-{index}",
            reserved_at="2026-08-10T08:03:02+00:00",
            ttl_seconds=20,
        )

    with pytest.raises(EvolutionPostRollbackRemoteDispatchError) as exhausted:
        await service.queue(
            request_id=contract.request_id,
            comparison_id=remote.original_comparison_id,
            channel="stable",
            queued_at="2026-08-10T08:03:02+00:00",
        )
    assert exhausted.value.code == "post_rollback_remote_dispatch_capacity_exhausted"
    assert await service.store.get(baseline.baseline.baseline_resolution_id) is None


@pytest.mark.asyncio
async def test_remote_dispatch_compensates_reservation_when_durable_write_fails(
    tmp_path: Path,
) -> None:
    service, registry, contract, remote, _, _, _ = await _dispatch_fixture(
        tmp_path,
        failing_store=True,
    )

    with pytest.raises(EvolutionPostRollbackRemoteDispatchError) as failed:
        await service.queue(
            request_id=contract.request_id,
            comparison_id=remote.original_comparison_id,
            channel="stable",
            queued_at="2026-08-10T08:03:02+00:00",
        )
    assert failed.value.code == "post_rollback_remote_dispatch_test_store_failure"
    capacity = await registry.capacity_snapshot(
        worker_id="worker-windows",
        assessed_at="2026-08-10T08:03:02+00:00",
    )
    assert capacity is not None
    assert capacity.reserved == 0
    assert capacity.available == capacity.maximum


@pytest.mark.asyncio
async def test_remote_dispatch_rejects_tampered_before_after_lineage(
    tmp_path: Path,
) -> None:
    service, registry, contract, remote, worker, _, baseline = await _dispatch_fixture(
        tmp_path
    )
    service.evidence_store.evidence.evidence_sha256 = "f" * 64  # type: ignore[attr-defined]

    with pytest.raises(EvolutionPostRollbackRemoteDispatchError) as stale:
        await service.queue(
            request_id=contract.request_id,
            comparison_id=remote.original_comparison_id,
            channel="stable",
            queued_at="2026-08-10T08:03:02+00:00",
        )
    assert stale.value.code == "post_rollback_remote_dispatch_evidence_stale"
    assert await service.store.get(baseline.baseline.baseline_resolution_id) is None
    capacity = await registry.capacity_snapshot(
        worker_id=worker.worker_id,
        assessed_at="2026-08-10T08:03:02+00:00",
    )
    assert capacity is not None and capacity.reserved == 0


async def _claim_fixture(tmp_path: Path):
    service, registry, contract, remote, worker, _, _ = await _dispatch_fixture(tmp_path)
    dispatch = await service.queue(
        request_id=contract.request_id,
        comparison_id=remote.original_comparison_id,
        channel="stable",
        queued_at="2026-08-10T08:03:02+00:00",
    )
    private_key = Ed25519PrivateKey.from_private_bytes(b"c" * 32)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    supervisor_key = b"post-rollback-worker-supervisor-key"
    identity_store = AuthenticatedWorkerIdentityStore(service.store.db_path)
    identity_authority = AuthenticatedWorkerIdentityAuthority(
        worker_registry=registry,
        store=identity_store,
        supervisor_key_provider=lambda: supervisor_key,
    )
    identity = issue_authenticated_worker_identity(
        contract=worker,
        public_key_base64=base64.b64encode(public_key).decode("ascii"),
        enrolled_at="2026-08-10T08:03:02+00:00",
        supervisor_key=supervisor_key,
    )
    await identity_authority.enroll(identity)
    claim_service = EvolutionPostRollbackRemoteClaimService(
        dispatch_service=service,
        dispatch_store=service.store,
        worker_registry=registry,
        identity_authority=identity_authority,
        store=EvolutionPostRollbackRemoteClaimStore(service.store.db_path),
    )
    return claim_service, registry, dispatch, worker, private_key


def _sign_claim(private_key, challenge) -> str:
    signature = private_key.sign(challenge.payload.canonical_bytes())
    return base64.b64encode(signature).decode("ascii")


@pytest.mark.asyncio
async def test_remote_claim_one_time_signature_renewal_and_fencing(
    tmp_path: Path,
) -> None:
    service, registry, dispatch, worker, private_key = await _claim_fixture(tmp_path)
    challenge = await service.prepare_claim(
        dispatch_id=dispatch.dispatch.dispatch_id,
        issued_at="2026-08-10T08:03:03+00:00",
        challenge_ttl_seconds=2,
        lease_seconds=5,
    )
    repeated_challenge = await service.prepare_claim(
        dispatch_id=dispatch.dispatch.dispatch_id,
        issued_at="2026-08-10T08:03:03+00:00",
        challenge_ttl_seconds=2,
        lease_seconds=5,
    )
    assert repeated_challenge == challenge
    assert challenge.one_time
    assert not challenge.transport_delivered

    class _ClaimService:
        async def prepare_claim(self, **arguments):
            assert arguments == {"dispatch_id": dispatch.dispatch.dispatch_id}
            return challenge

    tool = EvolutionPostRollbackRemoteClaimTool(
        SimpleNamespace(
            evolution_post_rollback_remote_claim_service=_ClaimService()
        )
    )
    arguments = {
        "action": "prepare",
        "dispatch_id": dispatch.dispatch.dispatch_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed
        assert not decision.requires_confirmation
    prepared = await tool.execute(**arguments)
    assert challenge.payload.challenge_id in prepared
    assert challenge.payload.model_dump_json() in prepared
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
        f"/evolution outcome-claim-behavior prepare "
        f"{dispatch.dispatch.dispatch_id}",
    )
    assert challenge.payload.challenge_id in slash
    signature = _sign_claim(private_key, challenge)

    first = await service.submit(
        challenge_id=challenge.payload.challenge_id,
        signature_base64=signature,
        claimed_at="2026-08-10T08:03:04+00:00",
    )
    repeated = await service.submit(
        challenge_id=challenge.payload.challenge_id,
        signature_base64=signature,
        claimed_at="2026-08-10T08:03:04+00:00",
    )
    assert repeated == first
    assert first.status == "current"
    assert first.worker_claimed
    assert first.receipt.sequence == 1
    assert first.receipt.lease_epoch == 1
    assert first.receipt.lease_expires_at == "2026-08-10T08:03:09+00:00"
    assert first.receipt.reservation_deadline == "2026-08-10T08:03:15+00:00"
    assert not first.transport_delivered
    assert not first.execution_authority
    assert "尚未传输 baseline" in render_post_rollback_remote_claim(first)

    renewal = await service.prepare_renewal(
        claim_id=first.receipt.claim_id,
        issued_at="2026-08-10T08:03:05+00:00",
        challenge_ttl_seconds=1,
        lease_seconds=5,
    )
    renewed = await service.submit(
        challenge_id=renewal.payload.challenge_id,
        signature_base64=_sign_claim(private_key, renewal),
        claimed_at="2026-08-10T08:03:05.500000+00:00",
    )
    assert renewed.status == "current"
    assert renewed.receipt.sequence == 2
    assert renewed.receipt.lease_epoch == 2
    assert renewed.receipt.previous_receipt_sha256 == first.receipt.receipt_sha256
    assert renewed.receipt.lease_expires_at == "2026-08-10T08:03:10.500000+00:00"

    takeover = _worker(
        worker.worker_id,
        epoch=2,
        issued_at="2026-08-10T08:03:07+00:00",
    )
    await registry.register(takeover, registered_at="2026-08-10T08:03:07+00:00")
    stale = await service.inspect(
        claim_id=renewed.receipt.claim_id,
        assessed_at="2026-08-10T08:03:08+00:00",
    )
    assert stale.status == "stale"
    assert not stale.lease_active
    assert not stale.worker_claimed


@pytest.mark.asyncio
async def test_remote_claim_rejects_forgery_replay_and_expired_window(
    tmp_path: Path,
) -> None:
    service, _, dispatch, _, private_key = await _claim_fixture(tmp_path)
    challenge = await service.prepare_claim(
        dispatch_id=dispatch.dispatch.dispatch_id,
        issued_at="2026-08-10T08:03:03+00:00",
        challenge_ttl_seconds=2,
        lease_seconds=20,
    )
    forged_key = Ed25519PrivateKey.from_private_bytes(b"d" * 32)
    with pytest.raises(EvolutionPostRollbackRemoteClaimError) as forged:
        await service.submit(
            challenge_id=challenge.payload.challenge_id,
            signature_base64=_sign_claim(forged_key, challenge),
            claimed_at="2026-08-10T08:03:04+00:00",
        )
    assert forged.value.code == "post_rollback_remote_claim_signature_invalid"

    signature = _sign_claim(private_key, challenge)
    claimed = await service.submit(
        challenge_id=challenge.payload.challenge_id,
        signature_base64=signature,
        claimed_at="2026-08-10T08:03:04+00:00",
    )
    assert claimed.receipt.lease_expires_at == dispatch.dispatch.reservation_expires_at
    other_signature = base64.b64encode(b"x" * 64).decode("ascii")
    with pytest.raises(EvolutionPostRollbackRemoteClaimError) as replay:
        await service.submit(
            challenge_id=challenge.payload.challenge_id,
            signature_base64=other_signature,
            claimed_at="2026-08-10T08:03:04+00:00",
        )
    assert replay.value.code == "post_rollback_remote_claim_challenge_closed"

    expired = await service.inspect(
        claim_id=claimed.receipt.claim_id,
        assessed_at=dispatch.dispatch.reservation_expires_at,
    )
    assert expired.status == "expired"
    with pytest.raises(EvolutionPostRollbackRemoteClaimError) as already_claimed:
        await service.prepare_claim(
            dispatch_id=dispatch.dispatch.dispatch_id,
            issued_at="2026-08-10T08:03:06+00:00",
        )
    assert already_claimed.value.code == "post_rollback_remote_claim_already_claimed"


@pytest.mark.asyncio
async def test_remote_claim_challenge_cannot_outlive_reservation(
    tmp_path: Path,
) -> None:
    service, _, dispatch, _, _ = await _claim_fixture(tmp_path)
    with pytest.raises(EvolutionPostRollbackRemoteClaimError) as window:
        await service.prepare_claim(
            dispatch_id=dispatch.dispatch.dispatch_id,
            issued_at="2026-08-10T08:03:03+00:00",
            challenge_ttl_seconds=20,
            lease_seconds=5,
        )
    assert (
        window.value.code
        == "post_rollback_remote_claim_window_exceeds_reservation"
    )


@pytest.mark.asyncio
async def test_remote_claim_pending_challenge_rechecks_health_and_store_authority(
    tmp_path: Path,
) -> None:
    service, registry, dispatch, worker, _ = await _claim_fixture(tmp_path)
    await service.prepare_claim(
        dispatch_id=dispatch.dispatch.dispatch_id,
        issued_at="2026-08-10T08:03:03+00:00",
        challenge_ttl_seconds=2,
        lease_seconds=5,
    )
    draining = HarnessHeartbeat(
        workspace_root=dispatch.dispatch.workspace_root,
        subject_kind=HarnessRunKind.TOOL,
        subject_id=worker.worker_id,
        instance_id=worker.instance_id,
        epoch=worker.epoch,
        sequence=2,
        phase=HarnessHeartbeatPhase.DRAINING,
        observed_at="2026-08-10T08:03:03.500000+00:00",
        timeout_seconds=60,
        detail_code="draining",
    )
    await registry.record_health_report(
        issue_worker_health_report(
            contract=worker,
            heartbeat=draining,
            active_jobs=1,
            accepting_jobs=False,
        ),
        recorded_at="2026-08-10T08:03:03.500000+00:00",
    )
    with pytest.raises(EvolutionPostRollbackRemoteClaimError) as stale:
        await service.prepare_claim(
            dispatch_id=dispatch.dispatch.dispatch_id,
            issued_at="2026-08-10T08:03:04+00:00",
        )
    assert stale.value.code == "post_rollback_remote_claim_authority_stale"

    with pytest.raises(ValueError, match="共享同一 SQLite"):
        EvolutionPostRollbackRemoteClaimService(
            dispatch_service=service.dispatch_service,
            dispatch_store=service.dispatch_store,
            worker_registry=registry,
            identity_authority=AuthenticatedWorkerIdentityAuthority(
                worker_registry=registry,
                store=AuthenticatedWorkerIdentityStore(
                    (tmp_path / "other-identities.db").resolve()
                ),
                supervisor_key_provider=lambda: b"x" * 32,
            ),
            store=service.store,
        )
