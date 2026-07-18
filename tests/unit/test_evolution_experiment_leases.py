from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

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
from naumi_agent.evolution.queue import EvolutionProposalQueueAdapter
from naumi_agent.evolution.review import EvolutionReviewService
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


async def _lease_fixture(tmp_path: Path, *, clock=None, profile_text: str | None = None):
    workspace = tmp_path / "workspace"
    target = workspace / "src" / "naumi_agent" / "ui" / "footer.py"
    target.parent.mkdir(parents=True)
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
    workspace, _, contract, _worktrees, _store, manager, _task_id = (
        await _lease_fixture(
            tmp_path,
            profile_text="schema_version: 1\n",
        )
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
    workspace, _, contract, _worktrees, _store, manager, _task_id = (
        await _lease_fixture(tmp_path)
    )
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
    workspace, _, contract, _worktrees, _store, manager, _task_id = (
        await _lease_fixture(tmp_path)
    )
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
    workspace, _, contract, _worktrees, _store, manager, _task_id = (
        await _lease_fixture(tmp_path, profile_text="schema_version: nope\n")
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
