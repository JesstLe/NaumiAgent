from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import naumi_agent.evolution.patch_writers as patch_writer_module
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
                scope="src/naumi_agent/ui/footer.py:render_footer",
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
        EvolutionPatchWriter(static_guard=guard),
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

    assert sum(isinstance(item, EvolutionPatchWriteReceipt) for item in results) == 1
    errors = [item for item in results if isinstance(item, EvolutionPatchWriteError)]
    assert len(errors) == 1
    assert errors[0].code in {"writer_locked", "guard_rejected"}


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
