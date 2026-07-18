from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import naumi_agent.evolution.patch_set_writers as patch_set_writer_module
import naumi_agent.evolution.patch_writers as patch_writer_module
import naumi_agent.evolution.postflight_guards as postflight_guard_module
from naumi_agent.evolution.experiment_leases import (
    EvolutionExperimentLeaseManager,
    EvolutionExperimentLeaseStore,
    ExperimentLeaseConflictError,
    ExperimentLeaseState,
)
from naumi_agent.evolution.experiment_snapshots import (
    EvolutionExperimentSourceSnapshot,
    EvolutionExperimentSourceSnapshotBuilder,
)
from naumi_agent.evolution.experiments import (
    EvolutionExperimentContractIssuer,
)
from naumi_agent.evolution.mutation_plans import (
    EvolutionMutationPlan,
    EvolutionMutationPlanner,
)
from naumi_agent.evolution.patch_journals import (
    EvolutionPatchJournalStore,
    PatchJournalState,
)
from naumi_agent.evolution.patch_recovery import (
    EvolutionPatchRecoveryCoordinator,
    EvolutionPatchSetRecoveryCoordinator,
)
from naumi_agent.evolution.patch_set_writers import (
    EvolutionPatchSetWriter,
    EvolutionPatchSetWriteReceipt,
)
from naumi_agent.evolution.patch_sets import (
    EvolutionPatchSetStore,
    PatchSetFilePhase,
    PatchSetState,
)
from naumi_agent.evolution.patch_writers import (
    EvolutionPatchWriteError,
    EvolutionPatchWriter,
    EvolutionPatchWriteReceipt,
)
from naumi_agent.evolution.queue import EvolutionProposalQueueAdapter
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.evolution.static_guards import (
    EvolutionStaticGuard,
    EvolutionStaticGuardPolicy,
    EvolutionStaticGuardReceipt,
)
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import FeedbackIntakeService, build_direct_user_feedback
from naumi_agent.tasks.store import TaskStore
from naumi_agent.tools.base import ToolRegistry
from naumi_agent.tools.builtin import create_builtin_tools
from naumi_agent.workbench.proposal_governance import ProposalAction
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore
from naumi_agent.worktree.manager import WorktreeManager
from naumi_agent.worktree.models import WorktreeStatus

NOW = datetime(2026, 7, 18, 23, 0, tzinfo=UTC)


class _SimulatedProcessCrash(BaseException):
    pass


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip()


async def _lease_fixture(
    tmp_path: Path,
    *,
    clock=None,
    profile_text: str | None = None,
    target_content: bytes | None = None,
    target_symlink: bool = False,
    scope: str = "src/naumi_agent/ui/footer.py:render_footer",
):
    workspace = tmp_path / "workspace"
    target = workspace / "src" / "naumi_agent" / "ui" / "footer.py"
    target.parent.mkdir(parents=True)
    if target_symlink:
        backing = target.with_name("footer-baseline.txt")
        backing.write_text("baseline\n", encoding="utf-8")
        target.symlink_to(backing.name)
    elif target_content is not None:
        target.write_bytes(target_content)
    else:
        target.write_text("def render_footer():\n    return 'baseline'\n", encoding="utf-8")
    target.with_name("header.py").write_text(
        "def render_header():\n    return 'baseline'\n",
        encoding="utf-8",
    )
    if profile_text is not None:
        profile = workspace / ".naumi" / "harness.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text(profile_text, encoding="utf-8")
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "Naumi Test")
    _git(workspace, "config", "user.email", "naumi@example.invalid")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "baseline")

    candidate_store = EvolutionCandidateStore(tmp_path / "evolution.db")
    intake = FeedbackIntakeService(candidate_store)
    intake_result = None
    for offset in range(2):
        intake_result = await intake.ingest(
            workspace,
            build_direct_user_feedback(
                session_id="experiment-lease",
                category="defect",
                scope=scope,
                topic="footer_truncation",
                summary=f"底栏截断 {offset}",
                now=NOW + timedelta(minutes=offset),
            ),
        )
    assert intake_result is not None

    runtime_db = str(tmp_path / "runtime.db")
    task_store = TaskStore(runtime_db)
    workbench_store = WorkbenchStore(runtime_db)
    service = WorkbenchService(
        task_store=task_store,
        workbench_store=workbench_store,
        workspace_root=str(workspace),
    )
    mission = await service.create_mission(
        session_id="session-1",
        title="隔离实验 Lease",
        goal="验证唯一 worktree 与安全清理",
    )
    issue = await service.create_issue(
        session_id="session-1",
        mission_id=mission.id,
        title="审阅 Footer Proposal",
    )
    review_service = EvolutionReviewService(candidate_store)
    queued = await EvolutionProposalQueueAdapter(
        review_service=review_service,
        workbench_service=service,
    ).enqueue(
        workspace,
        session_id="session-1",
        mission_id=mission.id,
        task_id=issue["task"]["id"],
        agent_id="Evolution-Agent",
        candidate_id=intake_result.candidate_id,
    )
    governed = await service.govern_proposal(
        "session-1",
        queued.proposal["id"],
        action=ProposalAction.APPROVE,
        reviewer="Human",
        decision_note="允许创建隔离 worktree",
        now=NOW + timedelta(minutes=5),
    )
    assert governed is not None
    contract = await EvolutionExperimentContractIssuer(
        review_service=review_service,
        workbench_service=service,
    ).issue(
        workspace,
        session_id="session-1",
        proposal_id=queued.proposal["id"],
        seed=42,
    )
    bound_tasks = TaskStore(runtime_db)
    bound_tasks.set_session("session-1")
    worktree_manager = WorktreeManager(
        repo_root=workspace,
        storage_dir=tmp_path / "worktrees",
        task_store=bound_tasks,
    )
    lease_store = EvolutionExperimentLeaseStore(runtime_db)
    lease_manager = EvolutionExperimentLeaseManager(
        store=lease_store,
        worktree_manager=worktree_manager,
        clock=clock,
    )
    return (
        workspace,
        target,
        contract,
        worktree_manager,
        lease_store,
        lease_manager,
        issue["task"]["id"],
    )


@pytest.mark.asyncio
async def test_acquire_uses_contract_baseline_and_preserves_dirty_main_tree(
    tmp_path: Path,
) -> None:
    (
        workspace,
        target,
        contract,
        worktree_manager,
        _store,
        manager,
        task_id,
    ) = await _lease_fixture(tmp_path)
    target.write_text("def render_footer():\n    return 'user-dirty'\n", encoding="utf-8")
    dirty_before = _git(workspace, "diff", "--", "src/naumi_agent/ui/footer.py")

    lease = await manager.acquire(contract, owner="Evolution-Agent")
    record = await worktree_manager.status(lease.worktree_name)
    assert not isinstance(record, list)
    isolated = Path(lease.worktree_path, "src/naumi_agent/ui/footer.py")

    assert lease.state is ExperimentLeaseState.ACTIVE
    assert lease.worktree_ready is True
    assert lease.execution_ready is False
    assert lease.baseline_commit == contract.baseline.commit
    assert record.base_ref == contract.baseline.commit
    assert record.task_id == task_id
    assert record.metadata["experiment_contract_id"] == contract.contract_id
    assert record.metadata["experiment_manifest_sha256"] == contract.manifest_sha256
    assert isolated.read_text(encoding="utf-8").endswith("return 'baseline'\n")
    assert target.read_text(encoding="utf-8").endswith("return 'user-dirty'\n")
    assert _git(workspace, "diff", "--", "src/naumi_agent/ui/footer.py") == dirty_before


@pytest.mark.asyncio
async def test_concurrent_acquire_is_idempotent_and_owner_bound(tmp_path: Path) -> None:
    _, _, contract, worktree_manager, _store, manager, _task_id = await _lease_fixture(tmp_path)

    leases = await asyncio.gather(
        *(manager.acquire(contract, owner="Evolution-Agent") for _ in range(8))
    )

    assert len({lease.lease_id for lease in leases}) == 1
    assert all(lease.state is ExperimentLeaseState.ACTIVE for lease in leases)
    records = await worktree_manager.status()
    assert isinstance(records, list)
    assert len(records) == 1
    with pytest.raises(ExperimentLeaseConflictError, match="owner binding"):
        await manager.acquire(contract, owner="Other-Agent")


@pytest.mark.asyncio
async def test_clean_release_removes_worktree_and_is_idempotent(tmp_path: Path) -> None:
    _, _, contract, _worktrees, store, manager, _task_id = await _lease_fixture(tmp_path)
    active = await manager.acquire(contract, owner="Evolution-Agent")

    released = await manager.release(contract.contract_id, owner="Evolution-Agent")
    duplicate = await manager.release(contract.contract_id, owner="Evolution-Agent")

    assert released.state is ExperimentLeaseState.CLEANED
    assert released.terminal_reason == "clean_removed"
    assert duplicate == released
    assert not Path(active.worktree_path).exists()
    assert (await store.get(contract.contract_id)) == released


@pytest.mark.asyncio
async def test_dirty_release_keeps_worktree_and_writes_tombstone(tmp_path: Path) -> None:
    _, _, contract, worktrees, _store, manager, _task_id = await _lease_fixture(tmp_path)
    active = await manager.acquire(contract, owner="Evolution-Agent")
    Path(active.worktree_path, "unreviewed.txt").write_text("keep me\n", encoding="utf-8")

    released = await manager.release(contract.contract_id, owner="Evolution-Agent")
    record = await worktrees.status(active.worktree_name)

    assert released.state is ExperimentLeaseState.TOMBSTONED
    assert released.terminal_reason == "dirty_or_ahead"
    assert Path(active.worktree_path).is_dir()
    assert not isinstance(record, list)
    assert record.status is WorktreeStatus.KEPT
    assert "禁止自动删除" in record.kept_reason


@pytest.mark.asyncio
async def test_reconcile_recovers_provisioning_and_expires_clean_worktree(
    tmp_path: Path,
) -> None:
    current = [NOW]
    (
        _workspace,
        _target,
        contract,
        worktrees,
        store,
        manager,
        task_id,
    ) = await _lease_fixture(tmp_path, clock=lambda: current[0])
    name = f"experiment-{contract.contract_id.removeprefix('evx_')[:16]}"
    path = str((worktrees.storage_dir / name).resolve())
    branch = f"naumi/worktree-{name}"
    lease, created = await store.reserve(
        contract,
        owner="Evolution-Agent",
        worktree_path=path,
        branch=branch,
        expires_at=(NOW + timedelta(seconds=60)).isoformat(timespec="seconds"),
        now=NOW.isoformat(timespec="seconds"),
    )
    assert created is True
    await worktrees.create_from_ref(
        name,
        base_ref=contract.baseline.commit,
        task_id=task_id,
        metadata={
            "experiment_contract_id": contract.contract_id,
            "experiment_manifest_sha256": contract.manifest_sha256,
            "experiment_lease_id": lease.lease_id,
        },
    )

    recovered = await manager.reconcile()
    assert recovered[0].state is ExperimentLeaseState.ACTIVE

    current[0] = NOW + timedelta(seconds=61)
    expired = await manager.reconcile()

    assert expired[0].state is ExperimentLeaseState.CLEANED
    assert expired[0].terminal_reason == "clean_removed"
    assert not Path(path).exists()


@pytest.mark.asyncio
async def test_reconcile_tombstones_active_lease_when_worktree_disappears(
    tmp_path: Path,
) -> None:
    _, _, contract, worktrees, store, manager, _task_id = await _lease_fixture(tmp_path)
    active = await manager.acquire(contract, owner="Evolution-Agent")
    subprocess.run(
        ["git", "-C", str(worktrees.repo_root), "worktree", "remove", active.worktree_path],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    reconciled = await manager.reconcile()
    persisted = await store.get(contract.contract_id)

    assert reconciled[0].state is ExperimentLeaseState.TOMBSTONED
    assert reconciled[0].terminal_reason == "active_worktree_missing"
    assert persisted == reconciled[0]
    assert reconciled[0].worktree_ready is False
    assert reconciled[0].execution_ready is False


@pytest.mark.asyncio
async def test_acquire_rejects_lease_duration_beyond_contract_budget(
    tmp_path: Path,
) -> None:
    _, _, contract, _worktrees, store, manager, _task_id = await _lease_fixture(tmp_path)

    with pytest.raises(ValueError, match="超出 Contract"):
        await manager.acquire(
            contract,
            owner="Evolution-Agent",
            duration_seconds=contract.budget.max_duration_seconds + 301,
        )

    assert await store.get(contract.contract_id) is None


@pytest.mark.asyncio
async def test_existing_provision_reservation_is_not_stolen_or_released(
    tmp_path: Path,
) -> None:
    _, _, contract, worktrees, store, manager, _task_id = await _lease_fixture(tmp_path)
    name = f"experiment-{contract.contract_id.removeprefix('evx_')[:16]}"
    reserved_at = datetime.now(UTC)
    lease, created = await store.reserve(
        contract,
        owner="Evolution-Agent",
        worktree_path=str((worktrees.storage_dir / name).resolve()),
        branch=f"naumi/worktree-{name}",
        expires_at=(reserved_at + timedelta(minutes=5)).isoformat(timespec="seconds"),
        now=reserved_at.isoformat(timespec="seconds"),
    )
    assert created is True

    with pytest.raises(ExperimentLeaseConflictError, match="另一个进程创建"):
        await manager.acquire(contract, owner="Evolution-Agent")
    with pytest.raises(ExperimentLeaseConflictError, match="provisioning"):
        await manager.release(contract.contract_id, owner="Evolution-Agent")

    records = await worktrees.status()
    persisted = await store.get(contract.contract_id)
    assert records == []
    assert persisted == lease
    assert persisted is not None
    assert persisted.state is ExperimentLeaseState.PROVISIONING


def _snapshot_builder(
    workspace: Path,
    worktree_storage_dir: Path,
) -> EvolutionExperimentSourceSnapshotBuilder:
    registry = ToolRegistry()
    for tool in create_builtin_tools(workspace):
        registry.register(tool)
    return EvolutionExperimentSourceSnapshotBuilder(
        registry,
        worktree_storage_dir=worktree_storage_dir,
    )


@pytest.mark.asyncio
async def test_source_snapshot_binds_clean_tree_profile_config_and_tools(
    tmp_path: Path,
) -> None:
    workspace, _, contract, _worktrees, _store, manager, _task_id = await _lease_fixture(
        tmp_path,
        profile_text="schema_version: 1\n",
    )
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    builder = _snapshot_builder(workspace, Path(lease.worktree_path).parent)

    first = builder.capture(contract, lease)
    second = builder.capture(contract, lease)

    assert first == second
    assert first.snapshot_id == f"evs_{first.snapshot_sha256[:24]}"
    assert first.contract_id == contract.contract_id
    assert first.lease_id == lease.lease_id
    assert first.baseline_commit == contract.baseline.commit
    assert first.baseline_tree == _git(Path(lease.worktree_path), "rev-parse", "HEAD^{tree}")
    assert first.profile_status == "valid"
    assert len(first.profile_sha256) == 64
    assert tuple(tool.name for tool in first.tools) == tuple(sorted(contract.allowed_tools))
    assert all(tool.naumi_version for tool in first.tools)
    assert first.source_ready is True
    assert first.execution_ready is False


@pytest.mark.asyncio
async def test_source_snapshot_rejects_dirty_worktree_and_missing_tool(
    tmp_path: Path,
) -> None:
    workspace, _, contract, _worktrees, _store, manager, _task_id = await _lease_fixture(tmp_path)
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    missing_profile = _snapshot_builder(
        workspace,
        Path(lease.worktree_path).parent,
    ).capture(contract, lease)
    assert missing_profile.profile_status == "missing"
    assert len(missing_profile.profile_sha256) == 64

    empty = ToolRegistry()
    with pytest.raises(ValueError, match="未注册"):
        EvolutionExperimentSourceSnapshotBuilder(
            empty,
            worktree_storage_dir=Path(lease.worktree_path).parent,
        ).capture(contract, lease)

    Path(lease.worktree_path, "unreviewed.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="已存在变更"):
        _snapshot_builder(workspace, Path(lease.worktree_path).parent).capture(
            contract,
            lease,
        )


@pytest.mark.asyncio
async def test_source_snapshot_rejects_branch_binding_drift(tmp_path: Path) -> None:
    workspace, _, contract, _worktrees, _store, manager, _task_id = await _lease_fixture(tmp_path)
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    with pytest.raises(ValueError, match="受管 Lease 存储目录"):
        _snapshot_builder(workspace, tmp_path / "other-worktrees").capture(
            contract,
            lease,
        )

    _git(Path(lease.worktree_path), "checkout", "-b", "unexpected-branch")

    with pytest.raises(ValueError, match="branch"):
        _snapshot_builder(workspace, Path(lease.worktree_path).parent).capture(
            contract,
            lease,
        )


@pytest.mark.asyncio
async def test_source_snapshot_rejects_invalid_profile_and_digest_tampering(
    tmp_path: Path,
) -> None:
    workspace, _, contract, _worktrees, _store, manager, _task_id = await _lease_fixture(
        tmp_path, profile_text="schema_version: nope\n"
    )
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    with pytest.raises(ValueError, match="Profile 无效"):
        _snapshot_builder(workspace, Path(lease.worktree_path).parent).capture(
            contract,
            lease,
        )

    clean_workspace, _, clean_contract, _, _, clean_manager, _ = await _lease_fixture(
        tmp_path / "clean",
        profile_text="schema_version: 1\n",
    )
    clean_lease = await clean_manager.acquire(clean_contract, owner="Evolution-Agent")
    snapshot = _snapshot_builder(
        clean_workspace,
        Path(clean_lease.worktree_path).parent,
    ).capture(clean_contract, clean_lease)
    payload = snapshot.model_dump(mode="json")
    payload["baseline_tree_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="snapshot_sha256"):
        EvolutionExperimentSourceSnapshot.model_validate(payload)

    nested = snapshot.model_dump(mode="json")
    nested["tools"][0]["schema_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="Tool identity_sha256"):
        EvolutionExperimentSourceSnapshot.model_validate(nested)


def _mutation_planner(
    fixture_root: Path,
    workspace: Path,
    lease_path: str,
) -> EvolutionMutationPlanner:
    return EvolutionMutationPlanner(
        review_service=EvolutionReviewService(
            EvolutionCandidateStore(fixture_root / "evolution.db")
        ),
        snapshot_builder=_snapshot_builder(workspace, Path(lease_path).parent),
    )


@pytest.mark.asyncio
async def test_mutation_plan_is_deterministic_bounded_and_test_first(
    tmp_path: Path,
) -> None:
    workspace, _, contract, _worktrees, _store, manager, _task_id = await _lease_fixture(
        tmp_path, profile_text="schema_version: 1\n"
    )
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    snapshot_builder = _snapshot_builder(workspace, Path(lease.worktree_path).parent)
    snapshot = snapshot_builder.capture(contract, lease)
    planner = _mutation_planner(tmp_path, workspace, lease.worktree_path)

    first = await planner.plan(
        workspace,
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
    )
    second = await planner.plan(
        workspace,
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
    )

    assert first == second
    assert first.plan_id == f"evpplan_{first.plan_sha256[:24]}"
    assert first.authorized_files == contract.scope.allowed_files
    assert tuple(item.path for item in first.planned_files) == contract.scope.allowed_files
    assert first.planned_files[0].file_kind == "python"
    assert first.planned_files[0].change_mode == "modify"
    assert first.planned_files[0].baseline_blob
    assert tuple(stage.phase for stage in first.stages) == (
        "inspect",
        "baseline_check",
        "mutation",
        "static_guard",
        "candidate_check",
        "receipt",
    )
    assert first.stages[1].metric_names == first.stages[4].metric_names
    assert first.stages[2].target_files == contract.scope.allowed_files
    assert first.max_changed_files == len(first.planned_files)
    assert first.max_changed_lines <= contract.budget.max_changed_lines
    assert first.max_tool_calls < contract.budget.max_tool_calls
    assert first.baseline_check_required is True
    assert first.static_guard_required is True
    assert first.unrelated_refactor_allowed is False
    assert first.scope_expansion_allowed is False
    assert first.execution_ready is False


@pytest.mark.asyncio
async def test_mutation_plan_rejects_candidate_and_source_drift(tmp_path: Path) -> None:
    workspace, _, contract, _worktrees, _store, manager, _task_id = await _lease_fixture(tmp_path)
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    snapshot = _snapshot_builder(
        workspace,
        Path(lease.worktree_path).parent,
    ).capture(contract, lease)
    planner = _mutation_planner(tmp_path, workspace, lease.worktree_path)

    intake = FeedbackIntakeService(EvolutionCandidateStore(tmp_path / "evolution.db"))
    await intake.ingest(
        workspace,
        build_direct_user_feedback(
            session_id="experiment-lease",
            category="defect",
            scope="src/naumi_agent/ui/footer.py:render_footer",
            topic="footer_truncation",
            summary="底栏截断再次出现",
            now=NOW + timedelta(minutes=20),
        ),
    )
    with pytest.raises(ValueError, match="Candidate/Proposal 已偏离"):
        await planner.plan(
            workspace,
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
        )

    # Restore a planner view over the original DB state is impossible by design;
    # a source drift is independently rejected before Candidate planning.
    Path(lease.worktree_path, "dirty.txt").write_text("drift\n", encoding="utf-8")
    with pytest.raises(ValueError, match="已存在变更"):
        await planner.plan(
            workspace,
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
        )


@pytest.mark.asyncio
async def test_mutation_plan_rejects_binary_and_symlink_targets(tmp_path: Path) -> None:
    for name, options, expected in (
        ("binary", {"target_content": b"\x00binary"}, "二进制"),
        ("symlink", {"target_symlink": True}, "符号链接"),
    ):
        fixture_root = tmp_path / name
        workspace, _, contract, _, _, manager, _ = await _lease_fixture(
            fixture_root,
            **options,
        )
        lease = await manager.acquire(contract, owner="Evolution-Agent")
        snapshot = _snapshot_builder(
            workspace,
            Path(lease.worktree_path).parent,
        ).capture(contract, lease)
        planner = _mutation_planner(fixture_root, workspace, lease.worktree_path)

        with pytest.raises(ValueError, match=expected):
            await planner.plan(
                workspace,
                contract=contract,
                lease=lease,
                source_snapshot=snapshot,
            )


@pytest.mark.asyncio
async def test_mutation_plan_rejects_manifest_tampering(tmp_path: Path) -> None:
    workspace, _, contract, _, _, manager, _ = await _lease_fixture(tmp_path)
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    snapshot = _snapshot_builder(
        workspace,
        Path(lease.worktree_path).parent,
    ).capture(contract, lease)
    plan = await _mutation_planner(tmp_path, workspace, lease.worktree_path).plan(
        workspace,
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
    )
    payload = plan.model_dump(mode="json")
    payload["objective"]["hypothesis"] = "tampered"

    with pytest.raises(ValidationError, match="plan_sha256"):
        EvolutionMutationPlan.model_validate(payload)


@pytest.mark.asyncio
async def test_multi_file_scope_reaches_plan_and_static_guard_without_forgery(
    tmp_path: Path,
) -> None:
    scope = "files:src/naumi_agent/ui/footer.py,src/naumi_agent/ui/header.py"
    workspace, _, contract, _, _, manager, _ = await _lease_fixture(
        tmp_path,
        scope=scope,
        profile_text="schema_version: 1\n",
    )
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    snapshot_builder = _snapshot_builder(workspace, Path(lease.worktree_path).parent)
    snapshot = snapshot_builder.capture(contract, lease)
    plan = await _mutation_planner(tmp_path, workspace, lease.worktree_path).plan(
        workspace,
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
    )

    assert plan.authorized_files == (
        "src/naumi_agent/ui/footer.py",
        "src/naumi_agent/ui/header.py",
    )
    assert len(plan.planned_files) == 2
    assert plan.max_changed_files == 2
    receipt = await EvolutionStaticGuard(
        snapshot_builder=snapshot_builder,
    ).preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={
            "src/naumi_agent/ui/footer.py": (
                "def render_footer():\n    return 'multi-fixed'\n"
            ),
            "src/naumi_agent/ui/header.py": (
                "def render_header():\n    return 'multi-fixed'\n"
            ),
        },
    )
    assert receipt.preflight_passed is True
    assert tuple(change.path for change in receipt.changes) == plan.authorized_files


async def _patch_set_fixture(tmp_path: Path):
    scope = "files:src/naumi_agent/ui/footer.py,src/naumi_agent/ui/header.py"
    workspace, target, contract, _, _, manager, _ = await _lease_fixture(
        tmp_path,
        scope=scope,
        profile_text="schema_version: 1\n",
    )
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    snapshot_builder = _snapshot_builder(workspace, Path(lease.worktree_path).parent)
    snapshot = snapshot_builder.capture(contract, lease)
    plan = await _mutation_planner(tmp_path, workspace, lease.worktree_path).plan(
        workspace,
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
    )
    proposed = {
        "src/naumi_agent/ui/footer.py": (
            "def render_footer():\n    return 'write-set-fixed'\n"
        ),
        "src/naumi_agent/ui/header.py": (
            "def render_header():\n    return 'write-set-fixed'\n"
        ),
    }
    guard = EvolutionStaticGuard(snapshot_builder=snapshot_builder)
    receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=proposed,
    )
    assert receipt.preflight_passed is True
    isolated_root = Path(lease.worktree_path)
    before = {
        path: (isolated_root / path).read_bytes() for path in plan.authorized_files
    }
    modes = {
        path: (isolated_root / path).stat().st_mode & 0o777
        for path in plan.authorized_files
    }
    return (
        workspace,
        target,
        contract,
        lease,
        snapshot,
        plan,
        guard,
        receipt,
        proposed,
        before,
        modes,
        EvolutionPatchSetStore(tmp_path / "runtime.db", clock=lambda: NOW),
    )


@pytest.mark.asyncio
async def test_patch_set_prepare_persists_whole_set_before_any_write(
    tmp_path: Path,
) -> None:
    (
        workspace,
        target,
        contract,
        lease,
        snapshot,
        plan,
        _guard,
        receipt,
        _proposed,
        before,
        modes,
        store,
    ) = await _patch_set_fixture(tmp_path)
    isolated_before = {
        path: Path(lease.worktree_path, path).read_bytes() for path in plan.authorized_files
    }
    main_before = target.read_bytes()

    transaction = store.prepare(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=receipt,
        before_contents=before,
        file_modes=modes,
    )

    assert transaction.state is PatchSetState.PREPARED
    assert transaction.applied_count == 0
    assert transaction.rollback_cursor == -1
    assert tuple(item.path for item in transaction.files) == tuple(sorted(before))
    assert all(item.phase is PatchSetFilePhase.PREPARED for item in transaction.files)
    assert store.load_backups(transaction.transaction_id) == tuple(
        before[item.path] for item in transaction.files
    )
    assert transaction.write_authorized is False
    assert transaction.execution_ready is False
    assert {
        path: Path(lease.worktree_path, path).read_bytes() for path in plan.authorized_files
    } == isolated_before
    assert target.read_bytes() == main_before
    assert _git(workspace, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_patch_set_enforces_forward_order_and_commits_only_after_all_files(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        _,
        receipt,
        _,
        before,
        modes,
        store,
    ) = await _patch_set_fixture(tmp_path)
    transaction = store.prepare(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=receipt,
        before_contents=before,
        file_modes=modes,
    )

    with pytest.raises(ValueError, match="Guard 顺序"):
        store.mark_file_replaced(transaction.transaction_id, file_index=1)
    with pytest.raises(ValueError, match="不允许 commit"):
        store.mark_committed(transaction.transaction_id, receipt_json='{"ok":true}')

    applying = store.mark_file_replaced(transaction.transaction_id, file_index=0)
    assert applying.state is PatchSetState.APPLYING
    assert applying.applied_count == 1
    assert tuple(item.phase for item in applying.files) == (
        PatchSetFilePhase.REPLACED,
        PatchSetFilePhase.PREPARED,
    )
    applied = store.mark_file_replaced(transaction.transaction_id, file_index=1)
    assert applied.state is PatchSetState.APPLIED
    committed = store.mark_committed(
        transaction.transaction_id,
        receipt_json='{"write_set":"verified"}',
    )
    assert committed.state is PatchSetState.COMMITTED
    assert all(not item.backup_retained for item in committed.files)
    assert store.load_backups(transaction.transaction_id) == (None, None)
    assert store.load_receipt_json(transaction.transaction_id) == (
        '{"write_set":"verified"}'
    )


@pytest.mark.asyncio
async def test_patch_set_requires_complete_reverse_rollback_proof(tmp_path: Path) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        _,
        receipt,
        _,
        before,
        modes,
        store,
    ) = await _patch_set_fixture(tmp_path)
    transaction = store.prepare(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=receipt,
        before_contents=before,
        file_modes=modes,
    )
    store.mark_file_replaced(transaction.transaction_id, file_index=0)
    rolling = store.begin_rollback(transaction.transaction_id)
    assert rolling.rollback_cursor == 1

    with pytest.raises(ValueError, match="逆序"):
        store.mark_file_rolled_back(transaction.transaction_id, file_index=0)
    with pytest.raises(ValueError, match="尚未逐文件完成"):
        store.mark_rolled_back(transaction.transaction_id, failure_code="postflight_failed")
    store.mark_file_rolled_back(transaction.transaction_id, file_index=1)
    final_step = store.mark_file_rolled_back(transaction.transaction_id, file_index=0)
    assert final_step.rollback_cursor == -1
    rolled_back = store.mark_rolled_back(
        transaction.transaction_id,
        failure_code="postflight_failed",
    )
    assert rolled_back.state is PatchSetState.ROLLED_BACK
    assert all(item.phase is PatchSetFilePhase.ROLLED_BACK for item in rolled_back.files)
    assert store.load_backups(transaction.transaction_id) == (None, None)


@pytest.mark.asyncio
async def test_patch_set_detects_transaction_and_backup_tampering(tmp_path: Path) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        _,
        receipt,
        _,
        before,
        modes,
        store,
    ) = await _patch_set_fixture(tmp_path)
    transaction = store.prepare(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=receipt,
        before_contents=before,
        file_modes=modes,
    )
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as db:
        db.execute(
            """UPDATE evolution_patch_set_backups SET backup = ?
               WHERE transaction_id = ? AND file_index = 0""",
            (b"tampered-secret-content", transaction.transaction_id),
        )
    with pytest.raises(ValueError, match="backup 摘要") as backup_error:
        store.load_backups(transaction.transaction_id)
    assert "tampered-secret-content" not in str(backup_error.value)
    transactions, failures = store.scan_recoverable()
    assert transactions == ()
    assert len(failures) == 1
    assert failures[0].transaction_id == transaction.transaction_id
    assert failures[0].failure_code == "patch_set_corrupt"

    # Restore the backup, then corrupt only the signed transaction body.
    with sqlite3.connect(database) as db:
        db.execute(
            """UPDATE evolution_patch_set_backups SET backup = ?
               WHERE transaction_id = ? AND file_index = 0""",
            (before[transaction.files[0].path], transaction.transaction_id),
        )
        raw = db.execute(
            "SELECT transaction_json FROM evolution_patch_sets WHERE transaction_id = ?",
            (transaction.transaction_id,),
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["files"][0]["after_sha256"] = "0" * 64
        db.execute(
            "UPDATE evolution_patch_sets SET transaction_json = ? WHERE transaction_id = ?",
            (json.dumps(payload), transaction.transaction_id),
        )
    with pytest.raises(ValidationError, match="fact_sha256"):
        store.get_by_lease(lease.lease_id)


@pytest.mark.asyncio
async def test_patch_set_retry_accepts_revised_guard_within_plan_attempt_budget(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard,
        receipt,
        _,
        before,
        modes,
        store,
    ) = await _patch_set_fixture(tmp_path)
    first = store.prepare(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=receipt,
        before_contents=before,
        file_modes=modes,
    )
    store.begin_rollback(first.transaction_id)
    for index in reversed(range(len(first.files))):
        store.mark_file_rolled_back(first.transaction_id, file_index=index)
    store.mark_rolled_back(first.transaction_id, failure_code="candidate_check_failed")

    revised_contents = {
        path: f"# revised {index}\nvalue = {index}\n"
        for index, path in enumerate(plan.authorized_files)
    }
    revised_guard = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=revised_contents,
    )
    assert revised_guard.preflight_passed is True
    retried = store.prepare(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=revised_guard,
        before_contents=before,
        file_modes=modes,
    )
    assert retried.transaction_id == first.transaction_id
    assert retried.attempt == 2
    assert retried.guard_id == revised_guard.guard_id
    assert tuple(item.after_sha256 for item in retried.files) != tuple(
        item.after_sha256 for item in first.files
    )
    assert store.load_backups(retried.transaction_id) == tuple(
        before[item.path] for item in retried.files
    )

    current = retried
    while current.attempt < current.max_attempts:
        store.begin_rollback(current.transaction_id)
        for index in reversed(range(len(current.files))):
            store.mark_file_rolled_back(current.transaction_id, file_index=index)
        store.mark_rolled_back(current.transaction_id, failure_code="retry_failed")
        next_guard = await guard.preflight(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            proposed_contents={
                path: f"# attempt {current.attempt + 1}\nvalue = {index + 10}\n"
                for index, path in enumerate(plan.authorized_files)
            },
        )
        current = store.prepare(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=next_guard,
            before_contents=before,
            file_modes=modes,
        )

    store.begin_rollback(current.transaction_id)
    for index in reversed(range(len(current.files))):
        store.mark_file_rolled_back(current.transaction_id, file_index=index)
    exhausted = store.mark_rolled_back(
        current.transaction_id,
        failure_code="attempt_budget_exhausted",
    )
    final_guard = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={
            path: f"# forbidden retry\nvalue = {index + 100}\n"
            for index, path in enumerate(plan.authorized_files)
        },
    )
    unchanged = store.prepare(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=final_guard,
        before_contents=before,
        file_modes=modes,
    )
    assert unchanged == exhausted
    assert unchanged.attempt == unchanged.max_attempts
    assert unchanged.state is PatchSetState.ROLLED_BACK


@pytest.mark.asyncio
async def test_patch_set_concurrent_prepare_is_idempotent_per_lease(tmp_path: Path) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        _,
        receipt,
        _,
        before,
        modes,
        store,
    ) = await _patch_set_fixture(tmp_path)

    def prepare():
        return store.prepare(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=receipt,
            before_contents=before,
            file_modes=modes,
        )

    first, second = await asyncio.gather(
        asyncio.to_thread(prepare),
        asyncio.to_thread(prepare),
    )

    assert first == second
    assert first.state is PatchSetState.PREPARED
    with sqlite3.connect(tmp_path / "runtime.db") as db:
        assert db.execute("SELECT COUNT(*) FROM evolution_patch_sets").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM evolution_patch_set_backups").fetchone()[0]
            == 2
        )


@pytest.mark.asyncio
async def test_patch_set_writer_applies_complete_set_and_replays_receipt(
    tmp_path: Path,
) -> None:
    (
        workspace,
        target,
        contract,
        lease,
        snapshot,
        plan,
        guard,
        receipt,
        proposed,
        before,
        _,
        store,
    ) = await _patch_set_fixture(tmp_path)
    writer = EvolutionPatchSetWriter(
        static_guard=guard,
        patch_set_store=store,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )
    main_footer = target.read_bytes()
    main_header = target.with_name("header.py").read_bytes()

    first = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=receipt,
        proposed_contents=proposed,
    )
    second = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=receipt,
        proposed_contents=proposed,
    )

    assert first == second
    assert first.write_id == f"evsw_{first.write_sha256[:24]}"
    assert first.schema_version == 2
    assert first.postflight_guard is not None
    assert all(
        fact.api_change == "unchanged"
        for fact in first.postflight_guard.facts
    )
    assert tuple(change.path for change in first.changes) == plan.authorized_files
    transaction = store.get_by_lease(lease.lease_id)
    assert transaction is not None
    assert transaction.state is PatchSetState.COMMITTED
    assert store.load_backups(transaction.transaction_id) == (None, None)
    assert EvolutionPatchSetWriteReceipt.model_validate_json(
        store.load_receipt_json(transaction.transaction_id)
    ) == first
    for path, content in proposed.items():
        assert Path(lease.worktree_path, path).read_text(encoding="utf-8") == content
    assert target.read_bytes() == main_footer
    assert target.with_name("header.py").read_bytes() == main_header
    assert _git(workspace, "status", "--porcelain") == ""
    assert _git(Path(lease.worktree_path), "status", "--porcelain").splitlines() == [
        "M src/naumi_agent/ui/footer.py",
        " M src/naumi_agent/ui/header.py",
    ]
    payload = first.model_dump(mode="json")
    payload["worktree_status_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="write_sha256"):
        EvolutionPatchSetWriteReceipt.model_validate(payload)
    legacy_payload = first.model_dump(mode="json")
    for field in (
        "postflight_guard_id",
        "postflight_guard_sha256",
        "postflight_guard",
    ):
        legacy_payload.pop(field)
    legacy_payload["schema_version"] = 1
    legacy_payload["policy_version"] = "evolution-multi-file-patch-writer-v1"
    legacy_payload.pop("write_id")
    legacy_payload.pop("write_sha256")
    legacy_digest = patch_set_writer_module._sha256_payload(legacy_payload)
    legacy = EvolutionPatchSetWriteReceipt.model_validate({
        **legacy_payload,
        "write_id": f"evsw_{legacy_digest[:24]}",
        "write_sha256": legacy_digest,
    })
    assert legacy.schema_version == 1
    assert legacy.postflight_guard is None


@pytest.mark.asyncio
async def test_patch_set_writer_rolls_back_all_files_when_second_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard,
        receipt,
        proposed,
        before,
        _,
        store,
    ) = await _patch_set_fixture(tmp_path)
    writer = EvolutionPatchSetWriter(
        static_guard=guard,
        patch_set_store=store,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )
    original_replace = patch_set_writer_module._atomic_replace
    calls = 0

    def fail_second(target: Path, content: bytes, *, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second replace failure")
        original_replace(target, content, mode=mode)

    monkeypatch.setattr(patch_set_writer_module, "_atomic_replace", fail_second)

    with pytest.raises(EvolutionPatchWriteError, match="多文件原子写入") as error:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=receipt,
            proposed_contents=proposed,
        )

    assert error.value.rollback_completed is True
    assert {
        path: Path(lease.worktree_path, path).read_bytes() for path in plan.authorized_files
    } == before
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == ""
    transaction = store.get_by_lease(lease.lease_id)
    assert transaction is not None
    assert transaction.state is PatchSetState.ROLLED_BACK
    assert tuple(item.phase for item in transaction.files) == (
        PatchSetFilePhase.ROLLED_BACK,
        PatchSetFilePhase.ROLLED_BACK,
    )


@pytest.mark.asyncio
async def test_patch_set_postflight_breaking_api_rolls_back_complete_set(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard,
        _,
        proposed,
        before,
        _,
        store,
    ) = await _patch_set_fixture(tmp_path)
    breaking = dict(proposed)
    breaking["src/naumi_agent/ui/header.py"] = (
        "def render_header(width):\n    return width\n"
    )
    guard_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=breaking,
    )
    writer = EvolutionPatchSetWriter(
        static_guard=guard,
        patch_set_store=store,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )

    with pytest.raises(EvolutionPatchWriteError) as error:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents=breaking,
        )

    assert error.value.code == "postflight_breaking_api"
    assert error.value.rollback_completed is True
    assert {
        path: Path(lease.worktree_path, path).read_bytes()
        for path in plan.authorized_files
    } == before
    transaction = store.get_by_lease(lease.lease_id)
    assert transaction is not None
    assert transaction.state is PatchSetState.ROLLED_BACK


@pytest.mark.asyncio
async def test_patch_set_writer_crash_leaves_ordered_recoverable_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard,
        receipt,
        proposed,
        _before,
        _,
        store,
    ) = await _patch_set_fixture(tmp_path)
    writer = EvolutionPatchSetWriter(
        static_guard=guard,
        patch_set_store=store,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )
    original_mark = store.mark_file_replaced

    def crash_before_second_cas(transaction_id: str, *, file_index: int):
        if file_index == 1:
            raise _SimulatedProcessCrash()
        return original_mark(transaction_id, file_index=file_index)

    monkeypatch.setattr(store, "mark_file_replaced", crash_before_second_cas)

    with pytest.raises(_SimulatedProcessCrash):
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=receipt,
            proposed_contents=proposed,
        )

    transaction = store.get_by_lease(lease.lease_id)
    assert transaction is not None
    assert transaction.state is PatchSetState.APPLYING
    assert transaction.applied_count == 1
    assert tuple(item.phase for item in transaction.files) == (
        PatchSetFilePhase.REPLACED,
        PatchSetFilePhase.PREPARED,
    )
    assert all(
        Path(lease.worktree_path, path).read_text(encoding="utf-8") == content
        for path, content in proposed.items()
    )
    recoverable, failures = store.scan_recoverable()
    assert recoverable == (transaction,)
    assert failures == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_state", ["prepared", "applying", "rolling_back"])
async def test_patch_set_recovery_restores_known_crash_windows(
    tmp_path: Path,
    crash_state: str,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        _,
        receipt,
        proposed,
        before,
        modes,
        store,
    ) = await _patch_set_fixture(tmp_path)
    transaction = store.prepare(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=receipt,
        before_contents=before,
        file_modes=modes,
    )
    if crash_state in {"applying", "rolling_back"}:
        for index, item in enumerate(transaction.files):
            content = proposed[item.path].encode("utf-8")
            patch_set_writer_module._atomic_replace(
                Path(lease.worktree_path, item.path),
                content,
                mode=modes[item.path],
            )
            if crash_state == "rolling_back" or index == 0:
                store.mark_file_replaced(transaction.transaction_id, file_index=index)
    if crash_state == "rolling_back":
        store.begin_rollback(transaction.transaction_id)
        last = transaction.files[-1]
        patch_set_writer_module._atomic_replace(
            Path(lease.worktree_path, last.path),
            before[last.path],
            mode=modes[last.path],
        )
        store.mark_file_rolled_back(
            transaction.transaction_id,
            file_index=last.index,
        )

    outcomes = await EvolutionPatchSetRecoveryCoordinator(
        patch_set_store=store,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    ).recover_pending()

    assert len(outcomes) == 1
    assert outcomes[0].status == (
        "already_baseline" if crash_state == "prepared" else "rolled_back"
    )
    assert outcomes[0].filesystem_changed is (crash_state != "prepared")
    assert outcomes[0].recovery_complete is True
    assert outcomes[0].file_count == 2
    assert {
        path: Path(lease.worktree_path, path).read_bytes() for path in plan.authorized_files
    } == before
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == ""
    recovered = store.get_by_lease(lease.lease_id)
    assert recovered is not None
    assert recovered.state is PatchSetState.ROLLED_BACK
    assert store.load_backups(transaction.transaction_id) == (None, None)


@pytest.mark.asyncio
async def test_patch_set_recovery_fails_closed_on_unknown_target_without_partial_restore(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        _,
        receipt,
        _proposed,
        before,
        modes,
        store,
    ) = await _patch_set_fixture(tmp_path)
    transaction = store.prepare(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=receipt,
        before_contents=before,
        file_modes=modes,
    )
    unknown = b"unrecognized third-state content\n"
    first = transaction.files[0]
    patch_set_writer_module._atomic_replace(
        Path(lease.worktree_path, first.path),
        unknown,
        mode=modes[first.path],
    )

    outcomes = await EvolutionPatchSetRecoveryCoordinator(
        patch_set_store=store,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    ).recover_pending()

    assert len(outcomes) == 1
    assert outcomes[0].status == "failed"
    assert outcomes[0].failure_code == "target_digest_unknown"
    assert outcomes[0].filesystem_changed is False
    assert Path(lease.worktree_path, first.path).read_bytes() == unknown
    second = transaction.files[1]
    assert Path(lease.worktree_path, second.path).read_bytes() == before[second.path]
    failed = store.get_by_lease(lease.lease_id)
    assert failed is not None
    assert failed.state is PatchSetState.RECOVERY_FAILED
    assert store.load_backups(transaction.transaction_id) == tuple(
        before[item.path] for item in transaction.files
    )


@pytest.mark.asyncio
async def test_patch_set_recovery_defers_live_writer_lock_without_touching_files(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        _,
        receipt,
        _proposed,
        before,
        modes,
        store,
    ) = await _patch_set_fixture(tmp_path)
    transaction = store.prepare(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=receipt,
        before_contents=before,
        file_modes=modes,
    )
    lock_path = (
        Path(lease.worktree_path).parent
        / f".{lease.worktree_name}.{lease.lease_id}.patch.lock"
    )
    token = patch_writer_module._acquire_lock(lock_path, lease)
    try:
        outcomes = await EvolutionPatchSetRecoveryCoordinator(
            patch_set_store=store,
            journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
        ).recover_pending()
    finally:
        patch_writer_module._release_lock(lock_path, token)

    assert len(outcomes) == 1
    assert outcomes[0].status == "deferred"
    assert outcomes[0].failure_code == "writer_locked"
    assert outcomes[0].recovery_complete is False
    assert {
        path: Path(lease.worktree_path, path).read_bytes() for path in plan.authorized_files
    } == before
    current = store.get_by_lease(lease.lease_id)
    assert current == transaction


async def _guard_fixture(tmp_path: Path):
    workspace, target, contract, _, _, manager, _ = await _lease_fixture(
        tmp_path,
        profile_text="schema_version: 1\n",
    )
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    snapshot_builder = _snapshot_builder(workspace, Path(lease.worktree_path).parent)
    snapshot = snapshot_builder.capture(contract, lease)
    plan = await _mutation_planner(tmp_path, workspace, lease.worktree_path).plan(
        workspace,
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
    )
    guard = EvolutionStaticGuard(snapshot_builder=snapshot_builder)
    return workspace, target, contract, lease, snapshot, plan, guard


@pytest.mark.asyncio
async def test_static_guard_passes_exact_safe_change_without_writing(
    tmp_path: Path,
) -> None:
    workspace, target, contract, lease, snapshot, plan, guard = await _guard_fixture(tmp_path)
    before_main = target.read_bytes()
    isolated = Path(lease.worktree_path, plan.planned_files[0].path)
    before_isolated = isolated.read_bytes()
    proposed = "def render_footer():\n    return 'fixed'\n"

    first, second = await asyncio.gather(
        *(
            guard.preflight(
                contract=contract,
                lease=lease,
                source_snapshot=snapshot,
                mutation_plan=plan,
                proposed_contents={plan.planned_files[0].path: proposed},
            )
            for _ in range(2)
        )
    )

    assert first == second
    assert first.preflight_passed is True
    assert first.violations == ()
    assert first.guard_id == f"evg_{first.receipt_sha256[:24]}"
    assert first.changes[0].operation == "modify"
    assert first.changes[0].changed_lines > 0
    assert first.total_changed_files == 1
    assert first.total_changed_lines <= plan.max_changed_lines
    assert first.bypass_can_override is False
    assert first.write_authorized is False
    assert first.execution_ready is False
    assert target.read_bytes() == before_main
    assert isolated.read_bytes() == before_isolated
    assert _git(workspace, "status", "--porcelain") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "code"),
    [
        ('api_key = "Q7vN2pL9xK4mR8sT6wY1"\n', "hardcoded_secret"),
        ("# @generated - DO NOT EDIT\nvalue = 1\n", "generated_file"),
        (b"\x00binary", "binary_content"),
    ],
)
async def test_static_guard_blocks_secret_generated_and_binary_content(
    tmp_path: Path,
    content: str | bytes,
    code: str,
) -> None:
    _, _, contract, lease, snapshot, plan, guard = await _guard_fixture(tmp_path)
    receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={plan.planned_files[0].path: content},
    )

    assert receipt.preflight_passed is False
    assert code in {item.code for item in receipt.violations}
    serialized = receipt.model_dump_json()
    assert "Q7vN2pL9xK4mR8sT6wY1" not in serialized
    assert "@generated" not in serialized


@pytest.mark.asyncio
async def test_static_guard_rejects_oversized_content_without_diffing(tmp_path: Path) -> None:
    _, _, contract, lease, snapshot, plan, guard = await _guard_fixture(tmp_path)
    receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={
            plan.planned_files[0].path: b"x" * (2 * 1024 * 1024 + 1),
        },
    )

    assert receipt.preflight_passed is False
    assert "file_too_large" in {item.code for item in receipt.violations}
    assert receipt.changes[0].size_bytes == 2 * 1024 * 1024 + 1
    assert receipt.changes[0].changed_lines == 0


@pytest.mark.asyncio
async def test_static_guard_blocks_scope_and_line_budget_expansion(tmp_path: Path) -> None:
    _, _, contract, lease, snapshot, plan, guard = await _guard_fixture(tmp_path)
    oversized = "\n".join(f"line_{index} = {index}" for index in range(260)) + "\n"
    receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={
            plan.planned_files[0].path: oversized,
            "src/naumi_agent/ui/unapproved.py": "value = 1\n",
        },
    )

    codes = {item.code for item in receipt.violations}
    assert receipt.preflight_passed is False
    assert "scope_expansion" in codes
    assert "file_budget_exceeded" in codes
    assert "line_budget_exceeded" in codes


def test_static_guard_policy_blocks_protected_dependency_and_generated_paths(
    tmp_path: Path,
) -> None:
    policy = EvolutionStaticGuardPolicy()
    protected = policy.inspect_path(tmp_path, "src/naumi_agent/safety/permissions.py")
    dependency = policy.inspect_path(tmp_path, "pyproject.toml")
    generated = policy.inspect_path(tmp_path, "frontend/terminal-ui/dist/app.min.js")

    assert "protected_path" in {item.code for item in protected}
    assert "dependency_change" in {item.code for item in dependency}
    assert "generated_file" in {item.code for item in generated}
    env_reference = policy.inspect_content(
        "src/naumi_agent/config/example.py",
        b'api_key_ref = "{env:BRAVE_SEARCH_API_KEY}"\n',
    )
    assert "hardcoded_secret" not in {item.code for item in env_reference}
    assert len(policy.digest) == 64


@pytest.mark.asyncio
async def test_static_guard_reports_symlink_and_source_drift(tmp_path: Path) -> None:
    _, _, contract, lease, snapshot, plan, guard = await _guard_fixture(tmp_path)
    target = Path(lease.worktree_path, plan.planned_files[0].path)
    target.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    target.symlink_to(outside)

    receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={plan.planned_files[0].path: "value = 2\n"},
    )

    codes = {item.code for item in receipt.violations}
    assert receipt.preflight_passed is False
    assert "source_drift" in codes
    assert "path_escape" in codes
    assert "symlink" in codes
    assert "baseline_mismatch" in codes


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_path", ["../outside.py", "/tmp/outside.py", "C:\\outside.py"])
async def test_static_guard_returns_typed_receipt_for_unsafe_paths(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    _, _, contract, lease, snapshot, plan, guard = await _guard_fixture(tmp_path)
    receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={unsafe_path: "value = 2\n"},
    )

    assert receipt.preflight_passed is False
    assert "path_escape" in {item.code for item in receipt.violations}
    assert receipt.changes[0].path.startswith("<invalid-path:")
    assert unsafe_path not in receipt.model_dump_json()


@pytest.mark.asyncio
async def test_static_guard_receipt_rejects_tampering(tmp_path: Path) -> None:
    _, _, contract, lease, snapshot, plan, guard = await _guard_fixture(tmp_path)
    receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={plan.planned_files[0].path: "value = 2\n"},
    )
    payload = receipt.model_dump(mode="json")
    payload["changes"][0]["after_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="changes_sha256"):
        EvolutionStaticGuardReceipt.model_validate(payload)


async def _writer_fixture(tmp_path: Path):
    workspace, target, contract, lease, snapshot, plan, guard = await _guard_fixture(tmp_path)
    proposed = "def render_footer():\n    return 'atomic-fixed'\n"
    guard_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={plan.planned_files[0].path: proposed},
    )
    assert guard_receipt.preflight_passed is True
    return (
        workspace,
        target,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        EvolutionPatchWriter(
            static_guard=guard,
            journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
        ),
    )


@pytest.mark.asyncio
async def test_patch_writer_atomically_writes_only_isolated_worktree(tmp_path: Path) -> None:
    (
        workspace,
        target,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        writer,
    ) = await _writer_fixture(tmp_path)
    main_before = target.read_bytes()

    receipt = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents={plan.planned_files[0].path: proposed},
    )

    isolated = Path(lease.worktree_path, plan.planned_files[0].path)
    assert isolated.read_text(encoding="utf-8") == proposed
    assert target.read_bytes() == main_before
    assert _git(workspace, "status", "--porcelain") == ""
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == (
        f"M {plan.planned_files[0].path}"
    )
    assert receipt.guard_id == guard_receipt.guard_id
    assert receipt.change.after_sha256 == guard_receipt.changes[0].after_sha256
    assert receipt.postflight_passed is True
    assert receipt.rollback_performed is False
    assert receipt.execution_ready is False
    journal = writer._journal_store.get_by_lease(lease.lease_id)
    assert journal is not None
    assert journal.state is PatchJournalState.COMMITTED
    assert journal.backup_present is False
    assert writer._journal_store.load_backup(journal.journal_id) is None
    replay = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents={plan.planned_files[0].path: proposed},
    )
    assert replay == receipt


@pytest.mark.asyncio
async def test_postflight_guard_allows_additive_python_api_and_binds_receipt(
    tmp_path: Path,
) -> None:
    workspace, target, contract, lease, snapshot, plan, guard = await _guard_fixture(
        tmp_path
    )
    proposed = (
        "def render_footer():\n"
        "    return 'fixed'\n\n"
        "def render_footer_width():\n"
        "    return 80\n"
    )
    guard_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={plan.authorized_files[0]: proposed},
    )
    writer = EvolutionPatchWriter(
        static_guard=guard,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )

    receipt = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents={plan.authorized_files[0]: proposed},
    )

    assert receipt.schema_version == 2
    assert receipt.postflight_guard is not None
    fact = receipt.postflight_guard.facts[0]
    assert fact.api_change == "additive"
    assert fact.public_symbols_after == fact.public_symbols_before + 1
    assert fact.added_lines == guard_receipt.changes[0].added_lines
    assert fact.deleted_lines == guard_receipt.changes[0].deleted_lines
    assert target.read_text(encoding="utf-8").endswith("'baseline'\n")
    serialized = receipt.model_dump_json()
    assert "render_footer_width" not in serialized
    assert "return 'fixed'" not in serialized
    payload = receipt.model_dump(mode="json")
    payload["postflight_guard"]["facts"][0]["added_lines"] += 1
    with pytest.raises(ValidationError, match="Postflight Guard"):
        EvolutionPatchWriteReceipt.model_validate(payload)
    legacy_payload = receipt.model_dump(mode="json")
    for field in (
        "postflight_guard_id",
        "postflight_guard_sha256",
        "postflight_guard",
    ):
        legacy_payload.pop(field)
    legacy_payload["schema_version"] = 1
    legacy_payload["policy_version"] = "evolution-single-file-patch-writer-v1"
    legacy_payload.pop("write_id")
    legacy_payload.pop("write_sha256")
    legacy_digest = patch_writer_module._sha256_payload(legacy_payload)
    legacy = EvolutionPatchWriteReceipt.model_validate({
        **legacy_payload,
        "write_id": f"evw_{legacy_digest[:24]}",
        "write_sha256": legacy_digest,
    })
    assert legacy.schema_version == 1
    assert legacy.postflight_guard is None


def test_python_api_fingerprint_honors_all_and_detects_defaults_and_class_values() -> None:
    before = b'''__all__ = ["public"]
def public(value=1):
    return value
def hidden(value=1):
    return value
class Exported:
    limit = 1
'''
    hidden_only = before.replace(b"def hidden(value=1)", b"def hidden(value, extra=2)")
    default_changed = before.replace(b"def public(value=1)", b"def public(value=2)")
    class_value_changed = before.replace(b"limit = 1", b"limit = 2")

    baseline = postflight_guard_module._python_public_api(before, "module.py")
    assert postflight_guard_module._python_public_api(
        hidden_only, "module.py"
    ) == baseline
    assert postflight_guard_module._python_public_api(
        default_changed, "module.py"
    ) != baseline
    # Exported is not in __all__, so its class constant is intentionally private.
    assert postflight_guard_module._python_public_api(
        class_value_changed, "module.py"
    ) == baseline

    class_export_before = before.replace(
        b'__all__ = ["public"]', b'__all__ = ["public", "Exported"]'
    )
    class_export_after = class_export_before.replace(b"limit = 1", b"limit = 2")
    assert postflight_guard_module._python_public_api(
        class_export_before, "module.py"
    ) != postflight_guard_module._python_public_api(
        class_export_after, "module.py"
    )

    added_export = before.replace(
        b'__all__ = ["public"]', b'__all__ = ["public", "new_api"]'
    ) + b"\ndef new_api():\n    return 1\n"
    baseline_items = postflight_guard_module._python_public_api(before, "module.py")
    added_items = postflight_guard_module._python_public_api(added_export, "module.py")
    assert all(added_items.get(name) == value for name, value in baseline_items.items())
    assert set(added_items) > set(baseline_items)
    with pytest.raises(
        postflight_guard_module.EvolutionPostflightGuardError,
        match="AST 解析失败",
    ) as invalid:
        postflight_guard_module._python_public_api(b"def broken(:\n", "module.py")
    assert invalid.value.code == "postflight_api_parse_failed"


def test_postflight_regular_file_reader_never_follows_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(target.name)

    with pytest.raises(
        postflight_guard_module.EvolutionPostflightGuardError,
        match="不是普通文件",
    ) as error:
        postflight_guard_module._read_regular_file(link)

    assert error.value.code == "postflight_file_type"


@pytest.mark.asyncio
async def test_postflight_guard_rejects_breaking_python_api_and_rolls_back(
    tmp_path: Path,
) -> None:
    _, _, contract, lease, snapshot, plan, guard = await _guard_fixture(tmp_path)
    target = Path(lease.worktree_path, plan.authorized_files[0])
    baseline = target.read_bytes()
    proposed = "def render_footer(width):\n    return width\n"
    guard_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={plan.authorized_files[0]: proposed},
    )
    writer = EvolutionPatchWriter(
        static_guard=guard,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )

    with pytest.raises(EvolutionPatchWriteError) as error:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.authorized_files[0]: proposed},
        )

    assert error.value.code == "postflight_breaking_api"
    assert error.value.rollback_completed is True
    assert target.read_bytes() == baseline
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_postflight_guard_rejects_unsupported_source_parser_fail_closed(
    tmp_path: Path,
) -> None:
    scope = "src/naumi_agent/ui/new_component.ts:render"
    workspace, _, contract, _, _, manager, _ = await _lease_fixture(
        tmp_path,
        profile_text="schema_version: 1\n",
        scope=scope,
    )
    lease = await manager.acquire(contract, owner="Evolution-Agent")
    snapshot_builder = _snapshot_builder(workspace, Path(lease.worktree_path).parent)
    snapshot = snapshot_builder.capture(contract, lease)
    plan = await _mutation_planner(tmp_path, workspace, lease.worktree_path).plan(
        workspace,
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
    )
    guard = EvolutionStaticGuard(snapshot_builder=snapshot_builder)
    proposed = "export function render(): string { return 'ok'; }\n"
    guard_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={plan.authorized_files[0]: proposed},
    )
    writer = EvolutionPatchWriter(
        static_guard=guard,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )

    with pytest.raises(EvolutionPatchWriteError) as error:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.authorized_files[0]: proposed},
        )

    assert error.value.code == "postflight_api_unsupported"
    assert error.value.rollback_completed is True
    assert not Path(lease.worktree_path, plan.authorized_files[0]).exists()


@pytest.mark.asyncio
async def test_postflight_guard_rejects_mode_change_and_restores_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, contract, lease, snapshot, plan, guard_receipt, proposed, writer = (
        await _writer_fixture(tmp_path)
    )
    target = Path(lease.worktree_path, plan.authorized_files[0])
    original_replace = patch_writer_module._atomic_replace
    calls = 0

    def replace_and_make_executable(path: Path, content: bytes, *, mode: int) -> None:
        nonlocal calls
        calls += 1
        original_replace(path, content, mode=mode)
        if calls == 1:
            path.chmod(0o755)

    monkeypatch.setattr(patch_writer_module, "_atomic_replace", replace_and_make_executable)

    with pytest.raises(EvolutionPatchWriteError) as error:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.authorized_files[0]: proposed},
        )

    assert error.value.code == "postflight_mode_changed"
    assert error.value.rollback_completed is True
    assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.asyncio
async def test_patch_writer_rejects_content_not_bound_to_guard_receipt(tmp_path: Path) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        _,
        writer,
    ) = await _writer_fixture(tmp_path)
    isolated = Path(lease.worktree_path, plan.planned_files[0].path)
    before = isolated.read_bytes()

    with pytest.raises(EvolutionPatchWriteError) as captured:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.planned_files[0].path: "value = 'different'\n"},
        )

    assert captured.value.code == "guard_receipt_mismatch"
    assert isolated.read_bytes() == before


@pytest.mark.asyncio
async def test_patch_writer_rolls_back_real_bytes_when_postflight_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        writer,
    ) = await _writer_fixture(tmp_path)
    isolated = Path(lease.worktree_path, plan.planned_files[0].path)
    before = isolated.read_bytes()

    def fail_postflight(*_args) -> bytes:
        raise EvolutionPatchWriteError("injected_postflight", "测试故障注入。")

    monkeypatch.setattr(writer, "_postflight", fail_postflight)
    with pytest.raises(EvolutionPatchWriteError) as captured:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.planned_files[0].path: proposed},
        )

    assert captured.value.code == "injected_postflight"
    assert captured.value.rollback_completed is True
    assert isolated.read_bytes() == before
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == ""
    journal = writer._journal_store.get_by_lease(lease.lease_id)
    assert journal is not None
    assert journal.state is PatchJournalState.ROLLED_BACK
    assert journal.backup_present is False


@pytest.mark.asyncio
async def test_patch_writer_accepts_revised_guard_within_attempt_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        writer,
    ) = await _writer_fixture(tmp_path)
    real_postflight = writer._postflight

    def fail_first(*_args) -> bytes:
        raise EvolutionPatchWriteError("first_attempt_failed", "第一次尝试失败。")

    monkeypatch.setattr(writer, "_postflight", fail_first)
    with pytest.raises(EvolutionPatchWriteError):
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.planned_files[0].path: proposed},
        )
    monkeypatch.setattr(writer, "_postflight", real_postflight)
    revised = "def render_footer():\n    return 'revised-fixed'\n"
    revised_guard = await writer._static_guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={plan.planned_files[0].path: revised},
    )
    assert revised_guard.preflight_passed is True

    receipt = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=revised_guard,
        proposed_contents={plan.planned_files[0].path: revised},
    )

    journal = writer._journal_store.get_by_lease(lease.lease_id)
    assert journal is not None
    assert journal.state is PatchJournalState.COMMITTED
    assert journal.attempt == 2
    assert journal.guard_id == revised_guard.guard_id
    assert journal.guard_id != guard_receipt.guard_id
    assert receipt.guard_id == revised_guard.guard_id


@pytest.mark.asyncio
async def test_patch_writer_enforces_plan_attempt_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        _,
        _,
        writer,
    ) = await _writer_fixture(tmp_path)

    def fail_postflight(*_args) -> bytes:
        raise EvolutionPatchWriteError("attempt_failed", "尝试失败。")

    monkeypatch.setattr(writer, "_postflight", fail_postflight)
    for attempt in range(1, plan.max_attempts + 1):
        proposed = f"def render_footer():\n    return 'attempt-{attempt}'\n"
        guard = await writer._static_guard.preflight(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            proposed_contents={plan.planned_files[0].path: proposed},
        )
        with pytest.raises(EvolutionPatchWriteError) as captured:
            await writer.apply(
                contract=contract,
                lease=lease,
                source_snapshot=snapshot,
                mutation_plan=plan,
                guard_receipt=guard,
                proposed_contents={plan.planned_files[0].path: proposed},
            )
        assert captured.value.code == "attempt_failed"

    final_content = "def render_footer():\n    return 'over-budget'\n"
    final_guard = await writer._static_guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents={plan.planned_files[0].path: final_content},
    )
    with pytest.raises(EvolutionPatchWriteError) as captured:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=final_guard,
            proposed_contents={plan.planned_files[0].path: final_content},
        )
    assert captured.value.code == "attempt_budget_exhausted"
    journal = writer._journal_store.get_by_lease(lease.lease_id)
    assert journal is not None
    assert journal.attempt == plan.max_attempts
    assert journal.state is PatchJournalState.ROLLED_BACK


@pytest.mark.asyncio
async def test_patch_writer_rolls_back_when_directory_fsync_fails_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        writer,
    ) = await _writer_fixture(tmp_path)
    isolated = Path(lease.worktree_path, plan.planned_files[0].path)
    before = isolated.read_bytes()
    real_fsync_directory = patch_writer_module._fsync_directory
    calls = 0

    def fail_once(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected directory fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(patch_writer_module, "_fsync_directory", fail_once)
    with pytest.raises(EvolutionPatchWriteError) as captured:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.planned_files[0].path: proposed},
        )

    assert captured.value.code == "write_failed"
    assert captured.value.rollback_completed is True
    assert calls == 2
    assert isolated.read_bytes() == before
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_patch_writer_finalizes_pre_replace_failure_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        writer,
    ) = await _writer_fixture(tmp_path)
    isolated = Path(lease.worktree_path, plan.planned_files[0].path)
    before = isolated.read_bytes()

    def fail_before_replace(*_args, **_kwargs) -> None:
        raise OSError("injected temp write failure")

    monkeypatch.setattr(patch_writer_module, "_atomic_replace", fail_before_replace)
    with pytest.raises(EvolutionPatchWriteError) as captured:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.planned_files[0].path: proposed},
        )

    assert captured.value.code == "write_failed"
    assert captured.value.rollback_completed is True
    assert isolated.read_bytes() == before
    journal = writer._journal_store.get_by_lease(lease.lease_id)
    assert journal is not None
    assert journal.state is PatchJournalState.ROLLED_BACK
    assert journal.backup_present is False


@pytest.mark.asyncio
async def test_patch_writer_serializes_concurrent_replay(tmp_path: Path) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        writer,
    ) = await _writer_fixture(tmp_path)

    results = await asyncio.gather(
        *(
            writer.apply(
                contract=contract,
                lease=lease,
                source_snapshot=snapshot,
                mutation_plan=plan,
                guard_receipt=guard_receipt,
                proposed_contents={plan.planned_files[0].path: proposed},
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )

    receipts = [item for item in results if isinstance(item, EvolutionPatchWriteReceipt)]
    assert len(receipts) >= 1
    assert len({item.write_id for item in receipts}) == 1
    errors = [item for item in results if isinstance(item, EvolutionPatchWriteError)]
    assert all(item.code == "writer_locked" for item in errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_after_replace_mark", [False, True])
async def test_patch_recovery_rolls_back_prepared_and_replaced_crash_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_replace_mark: bool,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        writer,
    ) = await _writer_fixture(tmp_path)
    isolated = Path(lease.worktree_path, plan.planned_files[0].path)
    before = isolated.read_bytes()
    if crash_after_replace_mark:
        def crash_postflight(*_args) -> bytes:
            raise _SimulatedProcessCrash()

        monkeypatch.setattr(writer, "_postflight", crash_postflight)
        expected_state = PatchJournalState.REPLACED
    else:
        def crash_mark_replaced(_journal_id: str):
            raise _SimulatedProcessCrash()

        monkeypatch.setattr(writer._journal_store, "mark_replaced", crash_mark_replaced)
        expected_state = PatchJournalState.PREPARED

    with pytest.raises(_SimulatedProcessCrash):
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.planned_files[0].path: proposed},
        )
    assert isolated.read_text(encoding="utf-8") == proposed
    pending = writer._journal_store.get_by_lease(lease.lease_id)
    assert pending is not None
    assert pending.state is expected_state

    outcomes = await EvolutionPatchRecoveryCoordinator(
        journal_store=writer._journal_store,
    ).recover_pending()

    assert len(outcomes) == 1
    assert outcomes[0].status == "rolled_back"
    assert outcomes[0].recovery_complete is True
    assert isolated.read_bytes() == before
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == ""
    recovered = writer._journal_store.get_by_lease(lease.lease_id)
    assert recovered is not None
    assert recovered.state is PatchJournalState.ROLLED_BACK
    assert recovered.backup_present is False


@pytest.mark.asyncio
async def test_patch_recovery_defers_live_lock_then_reclaims_dead_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        writer,
    ) = await _writer_fixture(tmp_path)

    def crash_mark_replaced(_journal_id: str):
        raise _SimulatedProcessCrash()

    monkeypatch.setattr(writer._journal_store, "mark_replaced", crash_mark_replaced)
    with pytest.raises(_SimulatedProcessCrash):
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.planned_files[0].path: proposed},
        )
    lock_path = (
        Path(lease.worktree_path).parent
        / f".{lease.worktree_name}.{lease.lease_id}.patch.lock"
    )
    token = patch_writer_module._acquire_lock(lock_path, lease)
    coordinator = EvolutionPatchRecoveryCoordinator(journal_store=writer._journal_store)
    deferred = await coordinator.recover_pending()
    assert deferred[0].status == "deferred"
    assert deferred[0].failure_code == "writer_locked"

    monkeypatch.setattr(patch_writer_module, "_pid_alive", lambda _pid: False)
    recovered = await coordinator.recover_pending()
    assert recovered[0].status == "rolled_back"
    assert not lock_path.exists()
    patch_writer_module._release_lock(lock_path, token)


@pytest.mark.asyncio
async def test_patch_recovery_removes_dead_lock_created_before_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        _,
        _,
        lease,
        _,
        _,
        _,
        _,
        writer,
    ) = await _writer_fixture(tmp_path)
    storage = Path(lease.worktree_path).parent
    lock_path = storage / f".{lease.worktree_name}.{lease.lease_id}.patch.lock"
    original_token = patch_writer_module._acquire_lock(lock_path, lease)
    assert writer._journal_store.get_by_lease(lease.lease_id) is None
    monkeypatch.setattr(patch_writer_module, "_pid_alive", lambda _pid: False)

    outcomes = await EvolutionPatchRecoveryCoordinator(
        journal_store=writer._journal_store,
        worktree_storage_dir=storage,
    ).recover_pending()

    assert len(outcomes) == 1
    assert outcomes[0].status == "orphan_lock_removed"
    assert outcomes[0].recovery_complete is True
    assert not lock_path.exists()
    patch_writer_module._release_lock(lock_path, original_token)


@pytest.mark.asyncio
async def test_patch_recovery_reports_corrupt_backup_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        writer,
    ) = await _writer_fixture(tmp_path)

    def crash_mark_replaced(_journal_id: str):
        raise _SimulatedProcessCrash()

    monkeypatch.setattr(writer._journal_store, "mark_replaced", crash_mark_replaced)
    with pytest.raises(_SimulatedProcessCrash):
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=guard_receipt,
            proposed_contents={plan.planned_files[0].path: proposed},
        )
    isolated = Path(lease.worktree_path, plan.planned_files[0].path)
    after_crash = isolated.read_bytes()
    with sqlite3.connect(tmp_path / "runtime.db") as db:
        db.execute(
            "UPDATE evolution_patch_journals SET backup = ? WHERE lease_id = ?",
            (b"tampered-backup", lease.lease_id),
        )

    outcomes = await EvolutionPatchRecoveryCoordinator(
        journal_store=writer._journal_store,
    ).recover_pending()

    assert len(outcomes) == 1
    assert outcomes[0].status == "failed"
    assert outcomes[0].failure_code == "journal_corrupt"
    assert outcomes[0].filesystem_changed is False
    assert isolated.read_bytes() == after_crash


def test_patch_journal_store_migrates_pre_updated_at_schema(tmp_path: Path) -> None:
    database = tmp_path / "old-runtime.db"
    with sqlite3.connect(database) as db:
        db.execute(
            """CREATE TABLE evolution_patch_journals (
                   journal_id TEXT PRIMARY KEY,
                   journal_json TEXT NOT NULL,
                   lease_id TEXT NOT NULL UNIQUE,
                   state TEXT NOT NULL,
                   backup BLOB,
                   receipt_json TEXT
               )"""
        )

    journals, failures = EvolutionPatchJournalStore(database).scan_recoverable()

    assert journals == ()
    assert failures == ()
    with sqlite3.connect(database) as db:
        columns = {
            str(row[1])
            for row in db.execute("PRAGMA table_info(evolution_patch_journals)")
        }
    assert "updated_at" in columns


@pytest.mark.asyncio
async def test_patch_write_receipt_rejects_tampering(tmp_path: Path) -> None:
    (
        _,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard_receipt,
        proposed,
        writer,
    ) = await _writer_fixture(tmp_path)
    receipt = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents={plan.planned_files[0].path: proposed},
    )
    payload = receipt.model_dump(mode="json")
    payload["worktree_status_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="write_sha256"):
        EvolutionPatchWriteReceipt.model_validate(payload)
