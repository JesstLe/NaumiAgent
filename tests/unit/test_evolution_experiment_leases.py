from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import naumi_agent.evolution.adversarial_samples as adversarial_sample_module
import naumi_agent.evolution.mutation_receipts as mutation_receipt_module
import naumi_agent.evolution.patch_set_writers as patch_set_writer_module
import naumi_agent.evolution.patch_writers as patch_writer_module
import naumi_agent.evolution.postflight_guards as postflight_guard_module
import naumi_agent.evolution.validation_metric_bindings as metric_binding_module
from naumi_agent.daemons.execution_grants import ExecutionGrantStore
from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantRequest,
    RunDelegationGrantStore,
)
from naumi_agent.daemons.shell_admission import ShellWorkerAdmissionComposer
from naumi_agent.daemons.shell_worker import (
    AuthenticatedLocalShellTransport,
    ShellSandboxUnavailableError,
    detect_shell_sandbox_backend,
)
from naumi_agent.daemons.tool_jobs import ToolJobStore
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.adversarial_batch_requests import (
    EvolutionAdversarialBatchRequest,
    EvolutionAdversarialBatchRequestBuilder,
    EvolutionAdversarialBatchRequestError,
)
from naumi_agent.evolution.adversarial_probe_contracts import (
    EvolutionAdversarialProbeContract,
    EvolutionAdversarialProbeContractBuilder,
    EvolutionAdversarialProbeContractError,
    EvolutionAdversarialProbeRegistry,
)
from naumi_agent.evolution.adversarial_samples import (
    EvolutionAdversarialSampleError,
    EvolutionAdversarialSampleExecutor,
    EvolutionAdversarialSampleReceipt,
    adversarial_lane_authority_key,
)
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
from naumi_agent.evolution.mutation_generation import (
    EvolutionMutationGenerationError,
    EvolutionMutationGenerationResult,
    EvolutionMutationGenerationService,
    EvolutionMutationGenerationTrace,
    EvolutionMutationGenerationTraceStore,
)
from naumi_agent.evolution.mutation_plans import (
    EvolutionMutationPlan,
    EvolutionMutationPlanner,
)
from naumi_agent.evolution.mutation_receipts import (
    EvolutionMutationReceipt,
    EvolutionMutationReceiptError,
    EvolutionMutationReceiptService,
    EvolutionMutationReceiptStore,
)
from naumi_agent.evolution.mutation_turns import (
    EvolutionMutationTurnError,
    EvolutionMutationTurnRunner,
    MutationTurnBudget,
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
from naumi_agent.evolution.self_review import scan_self_review_files
from naumi_agent.evolution.static_guards import (
    EvolutionStaticGuard,
    EvolutionStaticGuardPolicy,
    EvolutionStaticGuardReceipt,
)
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.evolution.validation_cohorts import (
    BaselineCohortMetricCase,
    EvolutionBaselineCohortRequest,
    EvolutionBaselineCohortRequestBuilder,
    EvolutionCohortRequestError,
)
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricBindingError,
    EvolutionMetricRunnerBinding,
    EvolutionMetricRunnerBindingBuilder,
    EvolutionMetricRunnerRegistry,
    MetricRunnerBindingEntry,
)
from naumi_agent.evolution.validation_plans import (
    EvolutionValidationBindingError,
    EvolutionValidationPlan,
    EvolutionValidationPlanner,
    EvolutionValidationProfileBinder,
    EvolutionValidationProfileBinding,
    validation_requirements_for_path,
)
from naumi_agent.harness.eval_identity import capture_eval_platform_identity
from naumi_agent.harness.eval_replay import SAFE_REPLAY_EVAL_RUNNER_VERSION
from naumi_agent.harness.feedback import FeedbackIntakeService, build_direct_user_feedback
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckRunner
from naumi_agent.harness.sandbox_eval import (
    HarnessSandboxEvalExecutionKernel,
    HarnessSandboxEvalRunAuthority,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.model.router import ModelResponse, TokenUsage
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType, thaw_event_data
from naumi_agent.safety.permissions import PermissionMode
from naumi_agent.streaming.publisher import RuntimeEventPublisher
from naumi_agent.tasks.store import TaskStore
from naumi_agent.tools.base import ToolCall, ToolRegistry
from naumi_agent.tools.builtin import create_builtin_tools
from naumi_agent.workbench.proposal_governance import ProposalAction
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore
from naumi_agent.worktree.manager import WorktreeManager
from naumi_agent.worktree.models import WorktreeStatus

NOW = datetime(2026, 7, 18, 23, 0, tzinfo=UTC)


class _SimulatedProcessCrash(BaseException):
    pass


class _MutationEventSink:
    def __init__(self, *, fail_on: RuntimeEventType | None = None) -> None:
        self.events: list[RuntimeEvent] = []
        self.fail_on = fail_on

    async def emit(self, event: RuntimeEvent) -> None:
        if event.type is self.fail_on:
            raise RuntimeError("simulated event sink failure")
        self.events.append(event)


class _ScriptedMutationModel:
    def __init__(
        self,
        responses: list[ModelResponse],
        *,
        context_window: int = 124_000,
        max_output: int = 16_384,
    ) -> None:
        self.responses = list(responses)
        self.context_window = context_window
        self.max_output = max_output
        self.calls: list[dict[str, object]] = []

    def resolve_model(self, _tier) -> str:
        return "scripted/mutation"

    def get_context_window(self, _model: str) -> int:
        return self.context_window

    def get_max_output(self, _model: str) -> int:
        return self.max_output

    async def call(self, messages, **kwargs) -> ModelResponse:
        self.calls.append({
            "messages": json.loads(json.dumps(messages)),
            "tools": json.loads(json.dumps(kwargs.get("tools"))),
            "model": kwargs.get("model"),
            "max_tokens": kwargs.get("max_tokens"),
            "temperature": kwargs.get("temperature"),
        })
        if not self.responses:
            raise RuntimeError("script exhausted")
        return self.responses.pop(0)


class _BlockingMutationModel(_ScriptedMutationModel):
    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def call(self, messages, **kwargs) -> ModelResponse:
        self.calls.append({
            "messages": json.loads(json.dumps(messages)),
            "tools": json.loads(json.dumps(kwargs.get("tools"))),
        })
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip()


def _model_tool_call(
    call_id: str,
    name: str,
    arguments: dict[str, str],
) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


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
    assert adversarial_lane_authority_key(first, 1) != (
        adversarial_lane_authority_key(first, 2)
    )


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
        "mutation_generation_trace_id",
        "mutation_generation_trace_sha256",
        "mutation_generation_attempt",
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


async def _guard_fixture(
    tmp_path: Path,
    *,
    target_content: bytes | None = None,
    profile_text: str = "schema_version: 1\n",
):
    workspace, target, contract, _, _, manager, _ = await _lease_fixture(
        tmp_path,
        profile_text=profile_text,
        target_content=target_content,
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


async def _generated_writer_fixture(tmp_path: Path):
    workspace, target, contract, lease, snapshot, plan, guard = await _guard_fixture(
        tmp_path
    )
    proposed = "def render_footer():\n    return 'atomic-fixed'\n"
    session = EvolutionMutationGenerationService(
        trace_store=EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db"),
        clock=lambda: NOW,
    ).begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="generated-writer-fixture",
        attempt=1,
    )
    result = await session.execute(ToolCall(
        id="generated-write-1",
        name="file_write",
        arguments=json.dumps({
            "path": plan.authorized_files[0],
            "content": proposed,
        }),
    ))
    assert result.status == "success"
    generated = await session.finalize()
    guard_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=generated.proposed_contents,
        generation_trace=generated.trace,
    )
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
        generated.trace,
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
        "mutation_generation_trace_id",
        "mutation_generation_trace_sha256",
        "mutation_generation_attempt",
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
async def test_mutation_receipt_finalizes_committed_single_write_and_is_idempotent(
    tmp_path: Path,
) -> None:
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
        generation_trace,
    ) = await _generated_writer_fixture(tmp_path)
    main_before = target.read_bytes()
    write_receipt = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents={plan.authorized_files[0]: proposed},
        generation_trace=generation_trace,
    )
    store = EvolutionMutationReceiptStore(tmp_path / "runtime.db")
    service = EvolutionMutationReceiptService(
        journal_store=writer._journal_store,
        patch_set_store=EvolutionPatchSetStore(tmp_path / "runtime.db"),
        receipt_store=store,
    )

    first = service.finalize(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        static_guard=guard_receipt,
        generation_trace=generation_trace,
    )
    second = service.finalize(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        static_guard=guard_receipt,
        generation_trace=generation_trace,
    )

    assert first == second
    assert first.writer_kind == "single_file"
    assert first.write_receipt_id == write_receipt.write_id
    assert first.attempt == 1
    assert first.max_attempts == plan.max_attempts
    assert first.validation_status == "pending"
    assert first.validation_ready is True
    assert first.promotion_ready is False
    assert first.execution_ready is False
    assert tuple(item.path for item in first.files) == plan.authorized_files
    assert tuple(item.phase for item in first.tool_evidence) == (
        "mutation_generation",
        "static_guard",
        "patch_write",
        "postflight_guard",
    )
    assert store.get(first.mutation_receipt_id) == first
    assert store.get_by_lease(lease.lease_id) == first
    assert store.list_recent(limit=1) == (first,)
    assert target.read_bytes() == main_before
    assert _git(workspace, "status", "--porcelain") == ""
    serialized = first.model_dump_json()
    assert proposed not in serialized
    assert str(Path(lease.worktree_path)) not in serialized

    payload = first.model_dump(mode="json")
    payload["files"][0]["added_lines"] += 1
    with pytest.raises(ValidationError, match="file fact"):
        EvolutionMutationReceipt.model_validate(payload)
    evidence_payload = first.model_dump(mode="json")
    evidence_payload["tool_evidence"][2]["tool_name"] = (
        "evolution_patch_set_writer"
    )
    with pytest.raises(ValidationError, match="tool evidence"):
        EvolutionMutationReceipt.model_validate(evidence_payload)
    trace_payload = first.model_dump(mode="json")
    trace_payload["mutation_generation_trace_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="Generation Trace identity"):
        EvolutionMutationReceipt.model_validate(trace_payload)

    legacy_payload = first.model_dump(
        mode="json",
        exclude={"mutation_receipt_id", "receipt_sha256"},
    )
    legacy_payload.pop("mutation_generation_trace_id")
    legacy_payload.pop("mutation_generation_trace_sha256")
    legacy_payload["schema_version"] = 1
    legacy_payload["policy_version"] = "evolution-mutation-receipt-v1"
    legacy_payload["tool_evidence"] = legacy_payload["tool_evidence"][1:]
    for order, evidence in enumerate(legacy_payload["tool_evidence"], start=1):
        evidence["order"] = order
    legacy_digest = mutation_receipt_module._sha256_payload(legacy_payload)
    legacy = EvolutionMutationReceipt.model_validate({
        **legacy_payload,
        "mutation_receipt_id": f"evmr_{legacy_digest[:24]}",
        "receipt_sha256": legacy_digest,
    })
    assert legacy.schema_version == 1
    assert legacy.mutation_generation_trace_id is None


@pytest.mark.asyncio
async def test_mutation_receipt_rejects_uncommitted_write_and_post_commit_drift(
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
        generation_trace,
    ) = await _generated_writer_fixture(tmp_path)
    service = EvolutionMutationReceiptService(
        journal_store=writer._journal_store,
        patch_set_store=EvolutionPatchSetStore(tmp_path / "runtime.db"),
        receipt_store=EvolutionMutationReceiptStore(tmp_path / "runtime.db"),
    )

    with pytest.raises(EvolutionMutationReceiptError) as unsafe:
        mutation_receipt_module._require_safe_rationale(
            "api_key = " + "sk-" + ("x" * 24)
        )
    assert unsafe.value.code == "mutation_rationale_secret"

    unsafe_plan = plan.model_copy(update={
        "objective": plan.objective.model_copy(update={"hypothesis": "unbound"}),
    })
    with pytest.raises(EvolutionMutationReceiptError) as invalid_authority:
        service.finalize(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=unsafe_plan,
            static_guard=guard_receipt,
            generation_trace=generation_trace,
        )
    assert invalid_authority.value.code == "mutation_authority_invalid"

    with pytest.raises(EvolutionMutationReceiptError) as missing:
        service.finalize(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            static_guard=guard_receipt,
            generation_trace=generation_trace,
        )
    assert missing.value.code == "mutation_write_not_committed"

    write_receipt = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents={plan.authorized_files[0]: proposed},
        generation_trace=generation_trace,
    )
    legacy_payload = write_receipt.model_dump(mode="json")
    for field in (
        "postflight_guard_id",
        "postflight_guard_sha256",
        "postflight_guard",
        "mutation_generation_trace_id",
        "mutation_generation_trace_sha256",
        "mutation_generation_attempt",
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
    load_receipt = writer._journal_store.load_receipt_json
    monkeypatch.setattr(
        writer._journal_store,
        "load_receipt_json",
        lambda _journal_id: legacy.model_dump_json(),
    )
    with pytest.raises(EvolutionMutationReceiptError) as legacy_error:
        service.finalize(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            static_guard=guard_receipt,
            generation_trace=generation_trace,
        )
    assert legacy_error.value.code == "mutation_write_receipt_legacy"
    monkeypatch.setattr(writer._journal_store, "load_receipt_json", load_receipt)

    Path(lease.worktree_path, plan.authorized_files[0]).write_text(
        "def render_footer():\n    return 'drifted'\n",
        encoding="utf-8",
    )
    with pytest.raises(EvolutionMutationReceiptError) as drifted:
        service.finalize(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            static_guard=guard_receipt,
            generation_trace=generation_trace,
        )
    assert drifted.value.code == "postflight_digest_mismatch"


@pytest.mark.asyncio
async def test_mutation_receipt_finalizes_multi_file_write_and_store_is_concurrent(
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
        guard_receipt,
        proposed,
        _,
        _,
        patch_set_store,
    ) = await _patch_set_fixture(tmp_path)
    generation_session = EvolutionMutationGenerationService(
        trace_store=EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db"),
        clock=lambda: NOW,
    ).begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-receipt-multi",
        attempt=1,
    )
    generation_results = await asyncio.gather(*(
        generation_session.execute(ToolCall(
            id=f"mutation-receipt-multi-{index}",
            name="file_write",
            arguments=json.dumps({"path": path, "content": proposed[path]}),
        ))
        for index, path in enumerate(plan.authorized_files, start=1)
    ))
    assert all(item.status == "success" for item in generation_results)
    generated = await generation_session.finalize()
    guard_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=generated.proposed_contents,
        generation_trace=generated.trace,
    )
    journal_store = EvolutionPatchJournalStore(tmp_path / "runtime.db")
    writer = EvolutionPatchSetWriter(
        static_guard=guard,
        patch_set_store=patch_set_store,
        journal_store=journal_store,
    )
    write_receipt = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents=proposed,
        generation_trace=generated.trace,
    )
    store = EvolutionMutationReceiptStore(tmp_path / "runtime.db")
    service = EvolutionMutationReceiptService(
        journal_store=journal_store,
        patch_set_store=patch_set_store,
        receipt_store=store,
    )
    receipt = service.finalize(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        static_guard=guard_receipt,
        generation_trace=generated.trace,
    )

    assert receipt.writer_kind == "multi_file"
    assert receipt.write_receipt_id == write_receipt.write_id
    assert len(receipt.files) == 2
    assert receipt.files_sha256 == postflight_guard_module._sha256_payload(
        [item.model_dump(mode="json") for item in receipt.files]
    )
    copy_store = EvolutionMutationReceiptStore(tmp_path / "receipt-copy.db")
    copies = await asyncio.gather(
        asyncio.to_thread(copy_store.put, receipt),
        asyncio.to_thread(copy_store.put, receipt),
    )
    assert copies == [receipt, receipt]
    with sqlite3.connect(tmp_path / "receipt-copy.db") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_mutation_receipts"
        ).fetchone()[0] == 1
    alternate_payload = receipt.model_dump(
        mode="json",
        exclude={"mutation_receipt_id", "receipt_sha256"},
    )
    alternate_payload["created_at"] = (NOW + timedelta(seconds=1)).isoformat()
    alternate_digest = mutation_receipt_module._sha256_payload(alternate_payload)
    alternate = EvolutionMutationReceipt.model_validate({
        **alternate_payload,
        "mutation_receipt_id": f"evmr_{alternate_digest[:24]}",
        "receipt_sha256": alternate_digest,
    })
    with pytest.raises(EvolutionMutationReceiptError) as conflict:
        copy_store.put(alternate)
    assert conflict.value.code == "mutation_receipt_conflict"


@pytest.mark.asyncio
async def test_mutation_receipt_store_rejects_persisted_tampering(tmp_path: Path) -> None:
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
        generation_trace,
    ) = await _generated_writer_fixture(tmp_path)
    await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents={plan.authorized_files[0]: proposed},
        generation_trace=generation_trace,
    )
    store = EvolutionMutationReceiptStore(tmp_path / "runtime.db")
    receipt = EvolutionMutationReceiptService(
        journal_store=writer._journal_store,
        patch_set_store=EvolutionPatchSetStore(tmp_path / "runtime.db"),
        receipt_store=store,
    ).finalize(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        static_guard=guard_receipt,
        generation_trace=generation_trace,
    )
    payload = receipt.model_dump(mode="json")
    payload["attempt"] = 2
    with sqlite3.connect(tmp_path / "runtime.db") as db:
        db.execute(
            """UPDATE evolution_mutation_receipts SET receipt_json = ?
               WHERE mutation_receipt_id = ?""",
            (json.dumps(payload), receipt.mutation_receipt_id),
        )

    with pytest.raises(EvolutionMutationReceiptError) as corrupted:
        store.get(receipt.mutation_receipt_id)
    assert corrupted.value.code == "mutation_receipt_corrupt"


@pytest.mark.asyncio
async def test_mutation_generation_trace_executes_virtual_edit_without_disk_write(
    tmp_path: Path,
) -> None:
    workspace, target, contract, lease, snapshot, plan, guard = await _guard_fixture(
        tmp_path
    )
    isolated = Path(lease.worktree_path, plan.authorized_files[0])
    isolated_before = isolated.read_bytes()
    main_before = target.read_bytes()
    trace_store = EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db")
    generation_service = EvolutionMutationGenerationService(
        trace_store=trace_store,
        clock=lambda: NOW,
    )
    session = generation_service.begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-run-1",
        attempt=1,
    )
    call = ToolCall(
        id="mutation-call-1",
        name="file_edit",
        arguments=json.dumps({
            "path": plan.authorized_files[0],
            "old_text": "return 'baseline'",
            "new_text": "return 'generated'",
        }),
    )

    first_result, replay_result = await asyncio.gather(
        session.execute(call),
        session.execute(call),
    )
    generated = await session.finalize()
    replayed_generation = await session.finalize()

    assert first_result == replay_result
    assert replayed_generation == generated
    assert first_result.status == "success"
    assert len(generated.trace.calls) == 1
    assert generated.trace.total_tool_calls == 1
    assert generated.trace.successful_tool_calls == 1
    assert generated.trace.failed_tool_calls == 0
    assert generated.trace.final_files[0].after_sha256 == hashlib.sha256(
        generated.proposed_contents[plan.authorized_files[0]].encode("utf-8")
    ).hexdigest()
    assert isolated.read_bytes() == isolated_before
    assert target.read_bytes() == main_before
    assert _git(workspace, "status", "--porcelain") == ""
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == ""
    serialized = generated.trace.model_dump_json()
    assert "mutation-call-1" not in serialized
    assert "return 'generated'" not in serialized
    assert str(Path(lease.worktree_path)) not in serialized
    assert trace_store.get(generated.trace.trace_id) == generated.trace
    assert trace_store.get_for_attempt(plan.plan_id, 1) == generated.trace
    copy_store = EvolutionMutationGenerationTraceStore(tmp_path / "trace-copy.db")
    copies = await asyncio.gather(
        asyncio.to_thread(copy_store.put, generated.trace),
        asyncio.to_thread(copy_store.put, generated.trace),
    )
    assert copies == [generated.trace, generated.trace]
    with sqlite3.connect(tmp_path / "trace-copy.db") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_mutation_generation_traces"
        ).fetchone()[0] == 1
    with pytest.raises(EvolutionMutationGenerationError) as duplicate_attempt:
        generation_service.begin(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            run_id="mutation-run-replacement",
            attempt=1,
        )
    assert duplicate_attempt.value.code == "mutation_trace_attempt_exists"

    guard_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=generated.proposed_contents,
        generation_trace=generated.trace,
    )
    writer = EvolutionPatchWriter(
        static_guard=guard,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )
    write_receipt = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents=generated.proposed_contents,
        generation_trace=generated.trace,
    )
    mutation_receipt = EvolutionMutationReceiptService(
        journal_store=writer._journal_store,
        patch_set_store=EvolutionPatchSetStore(tmp_path / "runtime.db"),
        receipt_store=EvolutionMutationReceiptStore(tmp_path / "runtime.db"),
    ).finalize(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        static_guard=guard_receipt,
        generation_trace=generated.trace,
    )
    assert guard_receipt.schema_version == 2
    assert write_receipt.schema_version == 3
    assert mutation_receipt.schema_version == 2
    assert mutation_receipt.mutation_generation_trace_id == generated.trace.trace_id
    assert tuple(item.phase for item in mutation_receipt.tool_evidence) == (
        "mutation_generation",
        "static_guard",
        "patch_write",
        "postflight_guard",
    )
    assert write_receipt.change.after_sha256 == (
        generated.trace.final_files[0].after_sha256
    )
    assert target.read_bytes() == main_before


@pytest.mark.asyncio
async def test_mutation_generation_trace_records_failed_retry_and_detects_tampering(
    tmp_path: Path,
) -> None:
    _, _, contract, lease, snapshot, plan, _ = await _guard_fixture(tmp_path)
    store = EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db")
    session = EvolutionMutationGenerationService(
        trace_store=store,
        clock=lambda: NOW,
    ).begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-retry",
        attempt=1,
    )
    failed = await session.execute(ToolCall(
        id="retry-1",
        name="file_edit",
        arguments=json.dumps({
            "path": plan.authorized_files[0],
            "old_text": "not present",
            "new_text": "ignored",
        }),
    ))
    succeeded = await session.execute(ToolCall(
        id="retry-2",
        name="file_edit",
        arguments=json.dumps({
            "path": plan.authorized_files[0],
            "old_text": "return 'baseline'",
            "new_text": "return 'retry-fixed'",
        }),
    ))
    generated = await session.finalize()

    assert failed.status == "error"
    assert succeeded.status == "success"
    assert generated.trace.failed_tool_calls == 1
    assert generated.trace.successful_tool_calls == 1
    assert tuple(item.error_code for item in generated.trace.calls) == (
        "mutation_edit_target_missing",
        "",
    )
    assert generated.trace.calls[0].before_sha256 == (
        generated.trace.calls[0].after_sha256
    )
    assert generated.trace.calls[1].before_sha256 == (
        generated.trace.calls[0].after_sha256
    )
    payload = generated.trace.model_dump(mode="json")
    payload["calls"][1]["after_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="call fact"):
        EvolutionMutationGenerationTrace.model_validate(payload)

    with sqlite3.connect(tmp_path / "runtime.db") as db:
        stored = json.loads(db.execute(
            """SELECT trace_json FROM evolution_mutation_generation_traces
               WHERE trace_id = ?""",
            (generated.trace.trace_id,),
        ).fetchone()[0])
        stored["total_tool_calls"] = 3
        db.execute(
            """UPDATE evolution_mutation_generation_traces SET trace_json = ?
               WHERE trace_id = ?""",
            (json.dumps(stored), generated.trace.trace_id),
        )
    with pytest.raises(EvolutionMutationGenerationError) as corrupted:
        store.get(generated.trace.trace_id)
    assert corrupted.value.code == "mutation_trace_corrupt"


@pytest.mark.asyncio
async def test_mutation_generation_trace_requires_complete_scope_and_blocks_escape(
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
        _,
        _,
        _,
        _,
        _,
    ) = await _patch_set_fixture(tmp_path)
    service = EvolutionMutationGenerationService(
        trace_store=EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db"),
        clock=lambda: NOW,
    )
    incomplete = service.begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-incomplete",
        attempt=1,
    )
    first_path = plan.authorized_files[0]
    result = await incomplete.execute(ToolCall(
        id="multi-1",
        name="file_write",
        arguments=json.dumps({
            "path": first_path,
            "content": "def generated():\n    return 1\n",
        }),
    ))
    assert result.status == "success"
    with pytest.raises(EvolutionMutationGenerationError) as missing:
        await incomplete.finalize()
    assert missing.value.code == "mutation_scope_incomplete"

    escaped = service.begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-escape",
        attempt=2,
    )
    blocked = await escaped.execute(ToolCall(
        id="escape-1",
        name="file_write",
        arguments=json.dumps({
            "path": "../outside.py",
            "content": "value = 1\n",
        }),
    ))
    assert blocked.status == "error"
    with pytest.raises(EvolutionMutationGenerationError) as escaped_error:
        await escaped.finalize()
    assert escaped_error.value.code == "mutation_path_invalid"


@pytest.mark.asyncio
async def test_mutation_generation_trace_finalizes_multi_file_proposal(tmp_path: Path) -> None:
    (
        workspace,
        _,
        contract,
        lease,
        snapshot,
        plan,
        guard,
        _,
        _,
        before,
        _,
        patch_set_store,
    ) = await _patch_set_fixture(tmp_path)
    session = EvolutionMutationGenerationService(
        trace_store=EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db"),
        clock=lambda: NOW,
    ).begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-multi",
        attempt=1,
    )
    calls = tuple(
        ToolCall(
            id=f"multi-{index}",
            name="file_write",
            arguments=json.dumps({
                "path": path,
                "content": (
                    f"def render_{Path(path).stem}():\n"
                    f"    return 'generated-{index}'\n"
                ),
            }),
        )
        for index, path in enumerate(plan.authorized_files, start=1)
    )

    results = await asyncio.gather(*(session.execute(call) for call in calls))
    generated = await session.finalize()

    assert all(result.status == "success" for result in results)
    assert tuple(item.order for item in generated.trace.calls) == (1, 2)
    assert tuple(item.path for item in generated.trace.final_files) == tuple(
        sorted(plan.authorized_files)
    )
    assert set(generated.proposed_contents) == set(plan.authorized_files)
    assert {
        path: Path(lease.worktree_path, path).read_bytes()
        for path in plan.authorized_files
    } == before
    assert _git(workspace, "status", "--porcelain") == ""

    guard_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=generated.proposed_contents,
        generation_trace=generated.trace,
    )
    writer = EvolutionPatchSetWriter(
        static_guard=guard,
        patch_set_store=patch_set_store,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )
    write_receipt = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents=generated.proposed_contents,
        generation_trace=generated.trace,
    )
    mutation_receipt = EvolutionMutationReceiptService(
        journal_store=writer._journal_store,
        patch_set_store=patch_set_store,
        receipt_store=EvolutionMutationReceiptStore(tmp_path / "runtime.db"),
    ).finalize(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        static_guard=guard_receipt,
        generation_trace=generated.trace,
    )
    assert write_receipt.schema_version == 3
    assert mutation_receipt.schema_version == 2
    assert mutation_receipt.attempt == generated.trace.attempt == 1
    assert tuple(item.after_sha256 for item in mutation_receipt.files) == tuple(
        item.after_sha256 for item in generated.trace.final_files
    )


@pytest.mark.asyncio
async def test_mutation_generation_trace_fails_closed_on_tool_call_protocol(
    tmp_path: Path,
) -> None:
    _, _, contract, lease, snapshot, plan, _ = await _guard_fixture(tmp_path)
    service = EvolutionMutationGenerationService(
        trace_store=EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db"),
        clock=lambda: NOW,
    )
    unknown = service.begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-unknown-tool",
        attempt=1,
    )
    unknown_result = await unknown.execute(ToolCall(
        id="unknown-1",
        name="bash_run",
        arguments=json.dumps({"command": "true"}),
    ))
    assert unknown_result.status == "error"
    with pytest.raises(EvolutionMutationGenerationError) as unknown_error:
        await unknown.finalize()
    assert unknown_error.value.code == "mutation_tool_not_allowed"

    collision = service.begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-call-collision",
        attempt=1,
    )
    first = ToolCall(
        id="same-call",
        name="file_edit",
        arguments=json.dumps({
            "path": plan.authorized_files[0],
            "old_text": "return 'baseline'",
            "new_text": "return 'first'",
        }),
    )
    changed = ToolCall(
        id="same-call",
        name="file_edit",
        arguments=json.dumps({
            "path": plan.authorized_files[0],
            "old_text": "return 'first'",
            "new_text": "return 'second'",
        }),
    )
    assert (await collision.execute(first)).status == "success"
    assert (await collision.execute(changed)).status == "error"
    with pytest.raises(EvolutionMutationGenerationError) as collision_error:
        await collision.finalize()
    assert collision_error.value.code == "mutation_call_id_collision"

    tool_collision = service.begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-tool-collision",
        attempt=1,
    )
    shared_arguments = json.dumps({
        "path": plan.authorized_files[0],
        "content": "value = 2\n",
    })
    assert (
        await tool_collision.execute(ToolCall(
            id="same-tool-call",
            name="file_write",
            arguments=shared_arguments,
        ))
    ).status == "success"
    changed_tool = await tool_collision.execute(ToolCall(
        id="same-tool-call",
        name="file_edit",
        arguments=shared_arguments,
    ))
    assert changed_tool.status == "error"
    assert "mutation_call_id_collision" in changed_tool.content

    budget = service.begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-budget",
        attempt=1,
    )
    for index in range(plan.max_tool_calls):
        result = await budget.execute(ToolCall(
            id=f"budget-{index}",
            name="file_edit",
            arguments=json.dumps({
                "path": plan.authorized_files[0],
                "old_text": f"missing-{index}",
                "new_text": "ignored",
            }),
        ))
        assert result.status == "error"
    overflow = await budget.execute(ToolCall(
        id="budget-overflow",
        name="file_write",
        arguments=json.dumps({
            "path": plan.authorized_files[0],
            "content": "value = 1\n",
        }),
    ))
    assert overflow.status == "error"
    with pytest.raises(EvolutionMutationGenerationError) as budget_error:
        await budget.finalize()
    assert budget_error.value.code == "mutation_tool_budget_exceeded"


@pytest.mark.asyncio
async def test_mutation_generation_binding_rejects_wrong_digest_and_attempt(
    tmp_path: Path,
) -> None:
    _, _, contract, lease, snapshot, plan, guard = await _guard_fixture(tmp_path)
    trace_store = EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db")
    generation_service = EvolutionMutationGenerationService(
        trace_store=trace_store,
        clock=lambda: NOW,
    )
    proposed = "def render_footer():\n    return 'trace-bound'\n"

    async def generate(attempt: int) -> EvolutionMutationGenerationResult:
        session = generation_service.begin(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            run_id=f"binding-attempt-{attempt}",
            attempt=attempt,
        )
        result = await session.execute(ToolCall(
            id=f"binding-call-{attempt}",
            name="file_write",
            arguments=json.dumps({
                "path": plan.authorized_files[0],
                "content": proposed,
            }),
        ))
        assert result.status == "success"
        return await session.finalize()

    first = await generate(1)
    with pytest.raises(ValueError, match="Generation Trace 绑定不一致"):
        await guard.preflight(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            proposed_contents={
                plan.authorized_files[0]: proposed.replace("trace-bound", "drifted")
            },
            generation_trace=first.trace,
        )
    first_guard = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=first.proposed_contents,
        generation_trace=first.trace,
    )
    writer = EvolutionPatchWriter(
        static_guard=guard,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )
    with pytest.raises(EvolutionPatchWriteError) as missing_trace:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=first_guard,
            proposed_contents=first.proposed_contents,
        )
    assert missing_trace.value.code == "generation_trace_required"

    second = await generate(2)
    with pytest.raises(EvolutionPatchWriteError) as wrong_trace:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=first_guard,
            proposed_contents=first.proposed_contents,
            generation_trace=second.trace,
        )
    assert wrong_trace.value.code == "generation_trace_guard_mismatch"

    second_guard = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=second.proposed_contents,
        generation_trace=second.trace,
    )
    target = Path(lease.worktree_path, plan.authorized_files[0])
    before = target.read_bytes()
    with pytest.raises(EvolutionPatchWriteError) as wrong_attempt:
        await writer.apply(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            guard_receipt=second_guard,
            proposed_contents=second.proposed_contents,
            generation_trace=second.trace,
        )
    assert wrong_attempt.value.code == "mutation_generation_attempt_mismatch"
    assert wrong_attempt.value.rollback_completed is True
    assert target.read_bytes() == before
    journal = writer._journal_store.get_by_lease(lease.lease_id)
    assert journal is not None
    assert journal.attempt == 1
    assert journal.state is PatchJournalState.ROLLED_BACK

    committed = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=second_guard,
        proposed_contents=second.proposed_contents,
        generation_trace=second.trace,
    )
    assert committed.schema_version == 3
    journal = writer._journal_store.get_by_lease(lease.lease_id)
    assert journal is not None
    assert journal.attempt == second.trace.attempt == 2
    assert journal.state is PatchJournalState.COMMITTED


@pytest.mark.asyncio
async def test_mutation_turn_runner_generates_real_trace_and_typed_events(
    tmp_path: Path,
) -> None:
    workspace, target, contract, lease, snapshot, plan, guard = await _guard_fixture(
        tmp_path
    )
    proposed = "def render_footer():\n    return 'turn-runner'\n"
    model = _ScriptedMutationModel([
        ModelResponse(
            content="",
            tool_calls=[_model_tool_call(
                "turn-call-raw-id",
                "file_write",
                {"path": plan.authorized_files[0], "content": proposed},
            )],
            usage=TokenUsage(
                input_tokens=120,
                output_tokens=40,
                total_tokens=160,
                cost_usd=0.001,
            ),
            model="scripted/mutation",
            finish_reason="tool_calls",
        )
    ])
    trace_store = EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db")
    runner = EvolutionMutationTurnRunner(
        model_port=model,  # type: ignore[arg-type]
        generation_service=EvolutionMutationGenerationService(
            trace_store=trace_store,
            clock=lambda: NOW,
        ),
    )
    sink = _MutationEventSink()
    publisher = RuntimeEventPublisher(
        sink,
        session_id="mutation-session",
        run_id="mutation-turn-run",
    )
    main_before = target.read_bytes()
    isolated = Path(lease.worktree_path, plan.authorized_files[0])
    isolated_before = isolated.read_bytes()

    result = await runner.run(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-turn-run",
        attempt=1,
        events=publisher,
    )

    assert result.turns == result.model_calls == result.tool_calls == 1
    assert result.usage.total_tokens == 160
    assert result.models == ("scripted/mutation",)
    assert result.event_delivery_failed is False
    assert result.generation.proposed_contents[plan.authorized_files[0]] == proposed
    assert trace_store.get(result.generation.trace.trace_id) == result.generation.trace
    assert target.read_bytes() == main_before
    assert isolated.read_bytes() == isolated_before
    assert _git(workspace, "status", "--porcelain") == ""
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == ""
    assert [event.type for event in sink.events] == [
        RuntimeEventType.TURN_START,
        RuntimeEventType.TOOL_START,
        RuntimeEventType.TOOL_END,
        RuntimeEventType.RESPONSE_END,
    ]
    assert [event.sequence for event in sink.events] == [1, 2, 3, 4]
    event_json = json.dumps([
        thaw_event_data(event.data) for event in sink.events
    ])
    assert "turn-call-raw-id" not in event_json
    assert "return 'turn-runner'" not in event_json
    first_messages = model.calls[0]["messages"]
    assert isinstance(first_messages, list)
    assert "return 'baseline'" in first_messages[1]["content"]
    tool_schemas = model.calls[0]["tools"]
    assert isinstance(tool_schemas, list)
    assert tool_schemas[0]["function"]["parameters"]["properties"]["path"][
        "enum"
    ] == [plan.authorized_files[0]]

    guard_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=result.generation.proposed_contents,
        generation_trace=result.generation.trace,
    )
    writer = EvolutionPatchWriter(
        static_guard=guard,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )
    write_receipt = await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=guard_receipt,
        proposed_contents=result.generation.proposed_contents,
        generation_trace=result.generation.trace,
    )
    mutation_receipt = EvolutionMutationReceiptService(
        journal_store=writer._journal_store,
        patch_set_store=EvolutionPatchSetStore(tmp_path / "runtime.db"),
        receipt_store=EvolutionMutationReceiptStore(tmp_path / "runtime.db"),
    ).finalize(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        static_guard=guard_receipt,
        generation_trace=result.generation.trace,
    )
    assert write_receipt.schema_version == 3
    assert mutation_receipt.schema_version == 2
    assert mutation_receipt.mutation_generation_trace_id == (
        result.generation.trace.trace_id
    )


@pytest.mark.asyncio
async def test_mutation_turn_runner_retries_recoverable_edit_and_bounds_usage(
    tmp_path: Path,
) -> None:
    _, _, contract, lease, snapshot, plan, _ = await _guard_fixture(tmp_path)
    path = plan.authorized_files[0]
    model = _ScriptedMutationModel([
        ModelResponse(
            content="",
            tool_calls=[_model_tool_call(
                "turn-retry-1",
                "file_edit",
                {"path": path, "old_text": "missing", "new_text": "ignored"},
            )],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="scripted/mutation",
            finish_reason="tool_calls",
            reasoning_content="bounded mutation reasoning",
        ),
        ModelResponse(
            content="",
            tool_calls=[_model_tool_call(
                "turn-retry-2",
                "file_edit",
                {
                    "path": path,
                    "old_text": "return 'baseline'",
                    "new_text": "return 'recovered'",
                },
            )],
            usage=TokenUsage(input_tokens=20, output_tokens=5, total_tokens=25),
            model="scripted/mutation",
            finish_reason="tool_calls",
        ),
    ])
    runner = EvolutionMutationTurnRunner(
        model_port=model,  # type: ignore[arg-type]
        generation_service=EvolutionMutationGenerationService(
            trace_store=EvolutionMutationGenerationTraceStore(
                tmp_path / "runtime.db"
            ),
            clock=lambda: NOW,
        ),
    )

    result = await runner.run(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-turn-retry",
        attempt=1,
        budget=MutationTurnBudget(max_turns=2, max_total_tokens=1_024),
    )

    assert result.turns == result.model_calls == result.tool_calls == 2
    assert result.usage.total_tokens == 40
    assert tuple(item.status for item in result.generation.trace.calls) == (
        "error",
        "success",
    )
    second_messages = model.calls[1]["messages"]
    assert isinstance(second_messages, list)
    assert second_messages[-1]["role"] == "tool"
    assert "mutation_edit_target_missing" in second_messages[-1]["content"]
    assert second_messages[-2]["reasoning_content"] == "bounded mutation reasoning"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["event", "timeout", "caller"])
async def test_mutation_turn_runner_stops_blocked_model_on_all_cancellation_paths(
    tmp_path: Path,
    mode: str,
) -> None:
    _, target, contract, lease, snapshot, plan, _ = await _guard_fixture(tmp_path)
    model = _BlockingMutationModel()
    store = EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db")
    runner = EvolutionMutationTurnRunner(
        model_port=model,  # type: ignore[arg-type]
        generation_service=EvolutionMutationGenerationService(
            trace_store=store,
            clock=lambda: NOW,
        ),
    )
    cancel_event = asyncio.Event()
    task = asyncio.create_task(runner.run(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id=f"mutation-turn-{mode}",
        attempt=1,
        cancel_event=cancel_event,
        budget=MutationTurnBudget(timeout_seconds=0.05),
    ))
    await asyncio.wait_for(model.started.wait(), timeout=1)
    if mode == "event":
        cancel_event.set()
    elif mode == "caller":
        task.cancel()

    if mode == "caller":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(EvolutionMutationTurnError) as stopped:
            await task
        assert stopped.value.code == (
            "mutation_turn_cancelled" if mode == "event" else "mutation_turn_timeout"
        )
    await asyncio.wait_for(model.cancelled.wait(), timeout=1)
    assert store.get_for_attempt(plan.plan_id, 1) is None
    assert target.read_text(encoding="utf-8").endswith("'baseline'\n")
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_mutation_turn_runner_fails_closed_on_protocol_budget_and_final_event(
    tmp_path: Path,
) -> None:
    _, _, contract, lease, snapshot, plan, _ = await _guard_fixture(tmp_path)
    malformed = _ScriptedMutationModel([
        ModelResponse(
            content="",
            tool_calls=[{"id": "bad", "type": "function"}],
            model="scripted/mutation",
        )
    ])
    malformed_runner = EvolutionMutationTurnRunner(
        model_port=malformed,  # type: ignore[arg-type]
        generation_service=EvolutionMutationGenerationService(
            trace_store=EvolutionMutationGenerationTraceStore(
                tmp_path / "malformed.db"
            ),
            clock=lambda: NOW,
        ),
    )
    with pytest.raises(EvolutionMutationTurnError) as protocol_error:
        await malformed_runner.run(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            run_id="mutation-turn-malformed",
            attempt=1,
        )
    assert protocol_error.value.code == "mutation_turn_tool_protocol_invalid"

    over_budget = _ScriptedMutationModel([
        ModelResponse(
            content="",
            tool_calls=[_model_tool_call(
                "not-executed",
                "file_write",
                {"path": plan.authorized_files[0], "content": "value = 1\n"},
            )],
            usage=TokenUsage(
                input_tokens=1_024,
                output_tokens=1,
                total_tokens=1_025,
            ),
            model="scripted/mutation",
        )
    ])
    budget_runner = EvolutionMutationTurnRunner(
        model_port=over_budget,  # type: ignore[arg-type]
        generation_service=EvolutionMutationGenerationService(
            trace_store=EvolutionMutationGenerationTraceStore(tmp_path / "budget.db"),
            clock=lambda: NOW,
        ),
    )
    with pytest.raises(EvolutionMutationTurnError) as budget_error:
        await budget_runner.run(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            run_id="mutation-turn-budget",
            attempt=1,
            budget=MutationTurnBudget(max_total_tokens=1_024),
        )
    assert budget_error.value.code == "mutation_turn_token_budget_exceeded"

    failed_call = _model_tool_call(
        "limit-call",
        "file_edit",
        {
            "path": plan.authorized_files[0],
            "old_text": "never-present",
            "new_text": "ignored",
        },
    )
    turn_limited_model = _ScriptedMutationModel([
        ModelResponse(content="", tool_calls=[failed_call], model="scripted/mutation"),
        ModelResponse(
            content="",
            tool_calls=[{
                **failed_call,
                "id": "limit-call-2",
            }],
            model="scripted/mutation",
        ),
    ])
    turn_limited_runner = EvolutionMutationTurnRunner(
        model_port=turn_limited_model,  # type: ignore[arg-type]
        generation_service=EvolutionMutationGenerationService(
            trace_store=EvolutionMutationGenerationTraceStore(tmp_path / "limit.db"),
            clock=lambda: NOW,
        ),
    )
    with pytest.raises(EvolutionMutationTurnError) as turn_limit_error:
        await turn_limited_runner.run(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            run_id="mutation-turn-limit",
            attempt=1,
            budget=MutationTurnBudget(max_turns=2),
        )
    assert turn_limit_error.value.code == "mutation_turn_limit_exceeded"
    assert len(turn_limited_model.calls) == 2

    proposed = "def render_footer():\n    return 'event-safe'\n"
    completed_model = _ScriptedMutationModel([
        ModelResponse(
            content="",
            tool_calls=[_model_tool_call(
                "event-call",
                "file_write",
                {"path": plan.authorized_files[0], "content": proposed},
            )],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="scripted/mutation",
        )
    ])
    completed_runner = EvolutionMutationTurnRunner(
        model_port=completed_model,  # type: ignore[arg-type]
        generation_service=EvolutionMutationGenerationService(
            trace_store=EvolutionMutationGenerationTraceStore(
                tmp_path / "event.db"
            ),
            clock=lambda: NOW,
        ),
    )
    failing_sink = _MutationEventSink(fail_on=RuntimeEventType.RESPONSE_END)
    completed = await completed_runner.run(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="mutation-turn-event-safe",
        attempt=1,
        events=RuntimeEventPublisher(
            failing_sink,
            session_id="mutation-session",
            run_id="mutation-turn-event-safe",
        ),
    )
    assert completed.event_delivery_failed is True
    assert completed.generation.proposed_contents[plan.authorized_files[0]] == proposed


@pytest.mark.asyncio
async def test_mutation_turn_runner_refuses_source_truncation_when_prompt_is_oversized(
    tmp_path: Path,
) -> None:
    large_source = (
        b"def render_footer():\n    return 'baseline'\n"
        + (b"# approved source context\n" * 1_000)
    )
    _, _, contract, lease, snapshot, plan, _ = await _guard_fixture(
        tmp_path,
        target_content=large_source,
    )
    model = _ScriptedMutationModel([])
    store = EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db")
    runner = EvolutionMutationTurnRunner(
        model_port=model,  # type: ignore[arg-type]
        generation_service=EvolutionMutationGenerationService(
            trace_store=store,
            clock=lambda: NOW,
        ),
    )

    with pytest.raises(EvolutionMutationTurnError) as oversized:
        await runner.run(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_plan=plan,
            run_id="mutation-turn-prompt-budget",
            attempt=1,
            budget=MutationTurnBudget(max_prompt_bytes=16_384),
        )

    assert oversized.value.code == "mutation_turn_prompt_oversized"
    assert model.calls == []
    assert store.get_for_attempt(plan.plan_id, 1) is None
    assert Path(lease.worktree_path, plan.authorized_files[0]).read_bytes() == large_source


async def _validation_receipt_fixture(
    tmp_path: Path,
    *,
    profile_text: str = "schema_version: 1\n",
):
    workspace, _, contract, lease, snapshot, plan, guard = await _guard_fixture(
        tmp_path,
        profile_text=profile_text,
    )
    generation_session = EvolutionMutationGenerationService(
        trace_store=EvolutionMutationGenerationTraceStore(tmp_path / "runtime.db"),
        clock=lambda: NOW,
    ).begin(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        run_id="validation-plan-fixture",
        attempt=1,
    )
    proposed = "def render_footer():\n    return 'validation-plan'\n"
    result = await generation_session.execute(ToolCall(
        id="validation-plan-write",
        name="file_write",
        arguments=json.dumps({
            "path": plan.authorized_files[0],
            "content": proposed,
        }),
    ))
    assert result.status == "success"
    generation = await generation_session.finalize()
    static_receipt = await guard.preflight(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        proposed_contents=generation.proposed_contents,
        generation_trace=generation.trace,
    )
    writer = EvolutionPatchWriter(
        static_guard=guard,
        journal_store=EvolutionPatchJournalStore(tmp_path / "runtime.db"),
    )
    await writer.apply(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        guard_receipt=static_receipt,
        proposed_contents=generation.proposed_contents,
        generation_trace=generation.trace,
    )
    receipt = EvolutionMutationReceiptService(
        journal_store=writer._journal_store,
        patch_set_store=EvolutionPatchSetStore(tmp_path / "runtime.db"),
        receipt_store=EvolutionMutationReceiptStore(tmp_path / "runtime.db"),
    ).finalize(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_plan=plan,
        static_guard=static_receipt,
        generation_trace=generation.trace,
    )
    return workspace, contract, lease, snapshot, receipt


@pytest.mark.asyncio
async def test_validation_plan_binds_real_receipt_to_symmetric_red_green(
    tmp_path: Path,
) -> None:
    workspace, contract, lease, snapshot, receipt = await _validation_receipt_fixture(
        tmp_path
    )
    planner = EvolutionValidationPlanner()
    isolated_before = _git(Path(lease.worktree_path), "status", "--porcelain")

    first = planner.plan(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_receipt=receipt,
    )
    second = planner.plan(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_receipt=receipt,
    )

    assert first == second
    assert first.validation_plan_id == f"evvplan_{first.validation_plan_sha256[:24]}"
    assert first.mutation_receipt_sha256 == receipt.receipt_sha256
    assert first.baseline_commit == contract.baseline.commit
    assert first.candidate_files_sha256 == receipt.files_sha256
    assert first.metrics[0].metric_name == receipt.required_metrics[0]
    assert first.metrics[0].baseline_phase == "red"
    assert first.metrics[0].candidate_phase == "green"
    assert first.metrics[0].same_fixture_required is True
    assert first.metrics[0].same_seed_required is True
    assert first.files[0].file_kind == "python"
    assert first.files[0].required_checks == ("lint", "compile", "unit", "contract")
    assert first.schema_version == 2
    assert first.files[0].operation == receipt.files[0].operation == "modify"
    assert first.files[0].baseline_sha256 == receipt.files[0].before_sha256
    assert first.files[0].candidate_sha256 == receipt.files[0].after_sha256
    assert first.required_check_kinds == ("compile", "contract", "lint", "unit")
    assert first.har08_comparison_receipt_required is True
    assert first.runner_binding_status == "required"
    assert first.execution_ready is False
    assert _git(workspace, "status", "--porcelain") == ""
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == isolated_before


@pytest.mark.asyncio
async def test_validation_plan_rejects_authority_metric_and_profile_drift(
    tmp_path: Path,
) -> None:
    _, contract, lease, snapshot, receipt = await _validation_receipt_fixture(tmp_path)
    planner = EvolutionValidationPlanner()

    with pytest.raises(ValueError, match="metrics"):
        planner.plan(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_receipt=receipt.model_copy(
                update={"required_metrics": ("unexpected.metric",)}
            ),
        )
    with pytest.raises(ValidationError, match="Mutation Receipt 摘要"):
        planner.plan(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot,
            mutation_receipt=receipt.model_copy(
                update={"receipt_sha256": "0" * 64}
            ),
        )
    with pytest.raises(ValueError, match="Profile"):
        planner.plan(
            contract=contract,
            lease=lease,
            source_snapshot=snapshot.model_copy(update={"profile_status": "missing"}),
            mutation_receipt=receipt,
        )
    inactive = lease.model_copy(
        update={"state": ExperimentLeaseState.RELEASED, "worktree_ready": False}
    )
    with pytest.raises(ValueError, match="active"):
        planner.plan(
            contract=contract,
            lease=inactive,
            source_snapshot=snapshot,
            mutation_receipt=receipt,
        )


@pytest.mark.asyncio
async def test_validation_plan_detects_nested_requirement_tampering(
    tmp_path: Path,
) -> None:
    _, contract, lease, snapshot, receipt = await _validation_receipt_fixture(tmp_path)
    plan = EvolutionValidationPlanner().plan(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_receipt=receipt,
    )
    payload = plan.model_dump(mode="json")
    payload["files"][0]["required_checks"] = ["smoke"]

    with pytest.raises(ValidationError, match="required_check_kinds|摘要"):
        EvolutionValidationPlan.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "kind", "checks"),
    [
        ("src/agent.py", "python", ("lint", "compile", "unit", "contract")),
        ("ui/view.js", "javascript", ("lint", "unit", "contract")),
        ("ui/view.tsx", "typescript", ("lint", "compile", "unit", "contract")),
        ("App/View.swift", "swift", ("compile", "unit", "contract")),
        ("core/lib.rs", "rust", ("compile", "unit", "contract")),
        ("cmd/main.go", "go", ("compile", "unit", "contract")),
        ("config/app.yaml", "yaml", ("lint", "contract")),
        ("assets/blob.bin", "other", ("contract", "smoke")),
    ],
)
def test_validation_requirements_cover_supported_language_groups(
    path: str,
    kind: str,
    checks: tuple[str, ...],
) -> None:
    requirement = validation_requirements_for_path(path)

    assert requirement.file_kind == kind
    assert requirement.required_checks == checks


VALIDATION_BINDING_PROFILE = """\
schema_version: 1
checks:
  - id: python_lint
    argv: [python, -m, ruff, check, src]
    timeout_seconds: 60
    when_changed: ['src/**/*.py']
    required_for: [change]
    provides: [lint]
  - id: python_compile
    argv: [python, -m, compileall, -q, src]
    timeout_seconds: 60
    when_changed: ['src/**/*.py']
    required_for: [change]
    provides: [compile]
  - id: python_contract
    argv: [python, -m, pytest, -q, tests/unit/test_footer.py]
    timeout_seconds: 120
    when_changed: ['src/**/*.py']
    required_for: [change]
    provides: [unit, contract]
"""

ADVERSARIAL_BINDING_PROFILE = VALIDATION_BINDING_PROFILE + """\
  - id: adversarial_boundary
    argv:
      - python3
      - -c
      - >-
        import runpy; ns = runpy.run_path('src/naumi_agent/ui/footer.py');
        assert isinstance(ns['render_footer'](), str)
    timeout_seconds: 10
    when_changed: ['src/**/*.py']
    required_for: [change]
    adversarial_probes: [boundary]
  - id: adversarial_platform
    argv:
      - python3
      - -c
      - >-
        import sys;
        assert sys.platform in {'darwin', 'linux', 'win32'}
    timeout_seconds: 10
    when_changed: ['src/**/ui/**/*.py']
    required_for: [change]
    adversarial_probes: [cross_platform]
"""


def test_adversarial_probe_registry_mechanically_covers_all_risk_dimensions() -> None:
    requirements = EvolutionAdversarialProbeRegistry().requirements_for(
        "src/naumi_agent/evolution/runtime/security_store_terminal.py"
    )

    assert tuple(item.kind for item in requirements) == (
        "boundary",
        "concurrency",
        "cross_platform",
        "recovery",
        "reward_hacking",
        "security",
    )
    assert next(
        item for item in requirements if item.kind == "cross_platform"
    ).platform_scope == "matrix"


@pytest.mark.asyncio
async def test_validation_profile_binding_requires_current_trust_and_unique_coverage(
    tmp_path: Path,
) -> None:
    workspace, contract, lease, snapshot, receipt = await _validation_receipt_fixture(
        tmp_path,
        profile_text=VALIDATION_BINDING_PROFILE,
    )
    plan = EvolutionValidationPlanner().plan(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_receipt=receipt,
    )
    trust_store = HarnessTrustStore(tmp_path / "harness-trust.db")
    binder = EvolutionValidationProfileBinder(trust_store)
    with pytest.raises(EvolutionValidationBindingError) as untrusted:
        await binder.bind(plan, workspace_root=workspace)
    assert untrusted.value.code == "validation_profile_untrusted"

    await trust_store.trust(workspace, snapshot.profile_sha256, source="user_slash")
    isolated_before = _git(Path(lease.worktree_path), "status", "--porcelain")
    first = await binder.bind(plan, workspace_root=workspace)
    second = await binder.bind(plan, workspace_root=workspace)

    assert first == second
    assert first.binding_id == f"evvbind_{first.binding_sha256[:24]}"
    assert first.validation_plan_sha256 == plan.validation_plan_sha256
    assert first.profile_sha256 == snapshot.profile_sha256
    assert first.profile_path == ".naumi/harness.yaml"
    assert tuple(item.check_id for item in first.checks) == (
        "python_compile",
        "python_contract",
        "python_lint",
    )
    assert tuple((item.path, item.check_kind, item.check_id) for item in first.coverage) == (
        ("src/naumi_agent/ui/footer.py", "compile", "python_compile"),
        ("src/naumi_agent/ui/footer.py", "contract", "python_contract"),
        ("src/naumi_agent/ui/footer.py", "lint", "python_lint"),
        ("src/naumi_agent/ui/footer.py", "unit", "python_contract"),
    )
    serialized = first.model_dump_json()
    assert "tests/unit/test_footer.py" not in serialized
    assert first.profile_trust_must_be_revalidated is True
    assert first.arc04_worker_required is True
    assert first.execution_ready is False
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == isolated_before

    tampered = first.model_dump(mode="json")
    tampered["checks"][0]["argv_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="Binding 摘要"):
        EvolutionValidationProfileBinding.model_validate(tampered)

    assert await trust_store.untrust(workspace) is True
    with pytest.raises(EvolutionValidationBindingError) as revoked:
        await binder.bind(plan, workspace_root=workspace)
    assert revoked.value.code == "validation_profile_untrusted"
    await trust_store.trust(workspace, snapshot.profile_sha256, source="user_slash")

    profile_path = workspace / ".naumi" / "harness.yaml"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(EvolutionValidationBindingError) as drifted:
        await binder.bind(plan, workspace_root=workspace)
    assert drifted.value.code == "validation_profile_drifted"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "ambiguous"])
async def test_validation_profile_binding_rejects_incomplete_or_ambiguous_capability(
    tmp_path: Path,
    mode: str,
) -> None:
    if mode == "missing":
        profile = VALIDATION_BINDING_PROFILE.replace("    provides: [compile]\n", "")
        expected = "validation_check_missing"
    else:
        profile = VALIDATION_BINDING_PROFILE + """\
  - id: second_lint
    argv: [python, -m, ruff, check, src]
    when_changed: ['src/**/*.py']
    required_for: [change]
    provides: [lint]
"""
        expected = "validation_check_ambiguous"
    workspace, contract, lease, snapshot, receipt = await _validation_receipt_fixture(
        tmp_path,
        profile_text=profile,
    )
    plan = EvolutionValidationPlanner().plan(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_receipt=receipt,
    )
    trust_store = HarnessTrustStore(tmp_path / "harness-trust.db")
    await trust_store.trust(workspace, snapshot.profile_sha256, source="user_slash")

    with pytest.raises(EvolutionValidationBindingError) as blocked:
        await EvolutionValidationProfileBinder(trust_store).bind(
            plan,
            workspace_root=workspace,
        )
    assert blocked.value.code == expected


async def _validation_binding_fixture(tmp_path: Path):
    workspace, contract, lease, snapshot, receipt = await _validation_receipt_fixture(
        tmp_path,
        profile_text=VALIDATION_BINDING_PROFILE,
    )
    plan = EvolutionValidationPlanner().plan(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_receipt=receipt,
    )
    trust_store = HarnessTrustStore(tmp_path / "harness-trust.db")
    await trust_store.trust(workspace, snapshot.profile_sha256, source="user_slash")
    binding = await EvolutionValidationProfileBinder(trust_store).bind(
        plan,
        workspace_root=workspace,
    )
    return workspace, contract, lease, plan, binding


@pytest.mark.asyncio
async def test_adversarial_probe_contract_binds_real_trusted_profile_without_execution(
    tmp_path: Path,
) -> None:
    workspace, contract, lease, snapshot, receipt = await _validation_receipt_fixture(
        tmp_path,
        profile_text=ADVERSARIAL_BINDING_PROFILE,
    )
    plan = EvolutionValidationPlanner().plan(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_receipt=receipt,
    )
    trust_store = HarnessTrustStore(tmp_path / "harness-trust.db")
    await trust_store.trust(workspace, snapshot.profile_sha256, source="user_slash")
    binding = await EvolutionValidationProfileBinder(trust_store).bind(
        plan,
        workspace_root=workspace,
    )
    builder = EvolutionAdversarialProbeContractBuilder(trust_store)

    first = await builder.build(
        validation_plan=plan,
        profile_binding=binding,
        workspace_root=workspace,
    )
    second = await builder.build(
        validation_plan=plan,
        profile_binding=binding,
        workspace_root=workspace,
        platform_identity=first.platform_identity,
    )

    assert first == second
    assert first.probe_contract_id == f"evapc_{first.probe_contract_sha256[:24]}"
    assert first.validation_plan_sha256 == plan.validation_plan_sha256
    assert first.profile_binding_sha256 == binding.binding_sha256
    assert first.candidate_files_sha256 == plan.candidate_files_sha256
    assert tuple((item.kind, item.platform_scope) for item in first.requirements) == (
        ("boundary", "current"),
        ("cross_platform", "matrix"),
    )
    assert tuple(item.check_id for item in first.checks) == (
        "adversarial_boundary",
        "adversarial_platform",
    )
    assert first.coverage_complete is True
    assert first.blockers == ()
    assert first.har08_batch_required is True
    assert first.runner_binding_status == "required"
    assert first.execution_ready is False
    assert "tests/adversarial" not in first.model_dump_json()

    tampered = first.model_dump(mode="json")
    tampered["coverage"][0]["check_id"] = "adversarial_platform"
    with pytest.raises(ValidationError, match="capability|摘要"):
        EvolutionAdversarialProbeContract.model_validate(tampered)


@pytest.mark.asyncio
async def test_adversarial_probe_contract_reports_missing_and_ambiguous_checks(
    tmp_path: Path,
) -> None:
    missing_workspace, contract, lease, snapshot, receipt = (
        await _validation_receipt_fixture(
            tmp_path / "missing",
            profile_text=VALIDATION_BINDING_PROFILE,
        )
    )
    missing_plan = EvolutionValidationPlanner().plan(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_receipt=receipt,
    )
    missing_trust = HarnessTrustStore(tmp_path / "missing-trust.db")
    await missing_trust.trust(
        missing_workspace,
        snapshot.profile_sha256,
        source="user_slash",
    )
    missing_binding = await EvolutionValidationProfileBinder(missing_trust).bind(
        missing_plan,
        workspace_root=missing_workspace,
    )
    missing = await EvolutionAdversarialProbeContractBuilder(missing_trust).build(
        validation_plan=missing_plan,
        profile_binding=missing_binding,
        workspace_root=missing_workspace,
    )

    assert missing.coverage_complete is False
    assert missing.coverage == ()
    assert tuple((item.kind, item.code) for item in missing.blockers) == (
        ("boundary", "probe_check_missing"),
        ("cross_platform", "probe_check_missing"),
    )

    ambiguous_profile = ADVERSARIAL_BINDING_PROFILE + """\
  - id: adversarial_boundary_second
    argv: [python, -m, pytest, -q, tests/adversarial/test_footer_boundary_2.py]
    when_changed: ['src/**/*.py']
    required_for: [change]
    adversarial_probes: [boundary]
"""
    workspace, contract, lease, snapshot, receipt = await _validation_receipt_fixture(
        tmp_path / "ambiguous",
        profile_text=ambiguous_profile,
    )
    plan = EvolutionValidationPlanner().plan(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_receipt=receipt,
    )
    trust_store = HarnessTrustStore(tmp_path / "ambiguous-trust.db")
    await trust_store.trust(workspace, snapshot.profile_sha256, source="user_slash")
    binding = await EvolutionValidationProfileBinder(trust_store).bind(
        plan,
        workspace_root=workspace,
    )
    ambiguous = await EvolutionAdversarialProbeContractBuilder(trust_store).build(
        validation_plan=plan,
        profile_binding=binding,
        workspace_root=workspace,
    )

    assert ambiguous.coverage_complete is False
    assert ambiguous.blockers[0].code == "probe_check_ambiguous"
    assert ambiguous.blockers[0].candidate_check_ids == (
        "adversarial_boundary",
        "adversarial_boundary_second",
    )

    profile_path = workspace / ".naumi" / "harness.yaml"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(EvolutionAdversarialProbeContractError) as drifted:
        await EvolutionAdversarialProbeContractBuilder(trust_store).build(
            validation_plan=plan,
            profile_binding=binding,
            workspace_root=workspace,
        )
    assert drifted.value.code == "probe_profile_drifted"


async def _adversarial_probe_fixture(
    tmp_path: Path,
    *,
    profile_text: str = ADVERSARIAL_BINDING_PROFILE,
):
    workspace, contract, lease, snapshot, receipt = await _validation_receipt_fixture(
        tmp_path,
        profile_text=profile_text,
    )
    plan = EvolutionValidationPlanner().plan(
        contract=contract,
        lease=lease,
        source_snapshot=snapshot,
        mutation_receipt=receipt,
    )
    trust_store = HarnessTrustStore(tmp_path / "harness-trust.db")
    await trust_store.trust(workspace, snapshot.profile_sha256, source="user_slash")
    binding = await EvolutionValidationProfileBinder(trust_store).bind(
        plan,
        workspace_root=workspace,
    )
    probe_contract = await EvolutionAdversarialProbeContractBuilder(
        trust_store
    ).build(
        validation_plan=plan,
        profile_binding=binding,
        workspace_root=workspace,
    )
    return workspace, contract, lease, plan, probe_contract, trust_store


@pytest.mark.asyncio
async def test_adversarial_batch_request_freezes_real_red_green_platform_matrix(
    tmp_path: Path,
) -> None:
    workspace, contract, _, plan, probe_contract, _ = (
        await _adversarial_probe_fixture(tmp_path)
    )
    builder = EvolutionAdversarialBatchRequestBuilder()

    first = builder.build(
        experiment_contract=contract,
        validation_plan=plan,
        probe_contract=probe_contract,
    )
    second = builder.build(
        experiment_contract=contract,
        validation_plan=plan,
        probe_contract=probe_contract,
    )

    assert first == second
    assert first.request_id == f"evadvreq_{first.request_sha256[:24]}"
    assert first.probe_contract_sha256 == probe_contract.probe_contract_sha256
    assert first.lease_id == plan.lease_id
    assert first.probe_platform_sha256 == probe_contract.platform_sha256
    assert first.origin_platform == probe_contract.platform_identity.system
    assert first.required_platforms == ("linux", "macos", "windows")
    assert first.phases == ("red", "green")
    assert tuple((item.platform, item.phase) for item in first.lanes) == (
        ("linux", "red"),
        ("linux", "green"),
        ("macos", "red"),
        ("macos", "green"),
        ("windows", "red"),
        ("windows", "green"),
    )
    assert tuple(item.order for item in first.probes) == (1, 2)
    assert tuple(item.check_id for item in first.checks) == (
        "adversarial_boundary",
        "adversarial_platform",
    )
    assert first.requested_samples == 5
    assert len(set(first.sample_seeds)) == 5
    assert first.check_timeout_seconds_per_sample == 20
    assert first.lane_budget_seconds == 100
    assert first.matrix_budget_seconds == 600
    assert first.matrix_budget_seconds <= contract.budget.max_duration_seconds
    assert first.project_code_execution_allowed is True
    assert first.request_ready is True
    assert first.execution_ready is False
    assert "tests/adversarial" not in first.model_dump_json()
    assert _git(workspace, "status", "--porcelain") == ""

    tampered = first.model_dump(mode="json")
    tampered["lanes"][0]["phase"] = "green"
    with pytest.raises(ValidationError, match="lanes|摘要"):
        EvolutionAdversarialBatchRequest.model_validate(tampered)

    tampered_platforms = first.model_dump(mode="json")
    tampered_platforms["required_platforms"] = ["macos"]
    with pytest.raises(ValidationError, match="三平台"):
        EvolutionAdversarialBatchRequest.model_validate(tampered_platforms)

    tampered_path = first.model_dump(mode="json")
    tampered_path["probes"][0]["path"] = "/etc/passwd"
    with pytest.raises(ValidationError, match="安全相对路径"):
        EvolutionAdversarialBatchRequest.model_validate(tampered_path)


@pytest.mark.asyncio
async def test_adversarial_batch_request_rejects_incomplete_coverage_and_budget(
    tmp_path: Path,
) -> None:
    _, contract, _, plan, incomplete, _ = await _adversarial_probe_fixture(
        tmp_path / "incomplete",
        profile_text=VALIDATION_BINDING_PROFILE,
    )
    builder = EvolutionAdversarialBatchRequestBuilder()

    with pytest.raises(EvolutionAdversarialBatchRequestError) as blocked:
        builder.build(
            experiment_contract=contract,
            validation_plan=plan,
            probe_contract=incomplete,
        )
    assert blocked.value.code == "adversarial_probe_coverage_incomplete"

    _, contract, _, plan, complete, _ = await _adversarial_probe_fixture(
        tmp_path / "budget"
    )
    with pytest.raises(EvolutionAdversarialBatchRequestError) as exceeded:
        builder.build(
            experiment_contract=contract,
            validation_plan=plan,
            probe_contract=complete,
            requested_samples=100,
        )
    assert exceeded.value.code == "adversarial_duration_budget_exceeded"

    with pytest.raises(EvolutionAdversarialBatchRequestError) as invalid_count:
        builder.build(
            experiment_contract=contract,
            validation_plan=plan,
            probe_contract=complete,
            requested_samples=True,
        )
    assert invalid_count.value.code == "adversarial_sample_count_invalid"


def _require_real_shell_backend() -> None:
    try:
        detect_shell_sandbox_backend()
    except ShellSandboxUnavailableError as exc:
        pytest.skip(str(exc))


async def _adversarial_run_authority(
    *,
    workspace: Path,
    store: HarnessStore,
    permissions: PermissionDecisionReceiptStore,
    authority: RunDelegationGrantAuthority,
    parent_receipt_id: str,
    parent_run_id: str,
    lane_order: int,
):
    now = datetime.now(UTC).isoformat()
    owner_id = f"evo-adversarial-lane-{lane_order}"
    run_lease = await store.acquire_run_lease(
        workspace_root=workspace,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent_run_id,
        owner_id=owner_id,
        now=now,
        lease_seconds=300,
    )
    assert run_lease is not None
    grant = await authority.issue(
        RunDelegationGrantRequest(
            idempotency_key=f"adversarial-lane-{lane_order}-{run_lease.epoch}",
            parent_receipt_id=parent_receipt_id,
            run_kind=HarnessRunKind.RUNTIME,
            lease_owner_id=owner_id,
            lease_epoch=run_lease.epoch,
            delegated_tool_names=("bash_run",),
        ),
        now=now,
        ttl_seconds=300,
    )
    return run_lease, grant


async def _release_adversarial_run_authority(
    *,
    workspace: Path,
    store: HarnessStore,
    authority: RunDelegationGrantAuthority,
    run_id: str,
    run_lease,
    grant_id: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    await authority.revoke(
        grant_id=grant_id,
        reason="adversarial_lane_finished",
        revoked_at=now,
    )
    released = await store.release_run_lease(
        workspace_root=workspace,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=run_id,
        owner_id=run_lease.owner_id,
        epoch=run_lease.epoch,
        now=now,
    )
    assert released is not None


@pytest.mark.asyncio
async def test_adversarial_sample_executes_real_red_and_green_lane_with_batch_authority(
    tmp_path: Path,
) -> None:
    _require_real_shell_backend()
    workspace, contract, lease, plan, probes, trust = (
        await _adversarial_probe_fixture(tmp_path)
    )
    request = EvolutionAdversarialBatchRequestBuilder().build(
        experiment_contract=contract,
        validation_plan=plan,
        probe_contract=probes,
    )
    platform = capture_eval_platform_identity().system
    if platform == "unknown":
        pytest.skip("当前平台无法映射到 adversarial matrix。")
    red_lane = next(
        item for item in request.lanes
        if item.platform == platform and item.phase == "red"
    )
    green_lane = next(
        item for item in request.lanes
        if item.platform == platform and item.phase == "green"
    )
    runtime = tmp_path / "adversarial-runtime"
    store = HarnessStore(tmp_path / "adversarial-harness.db")
    permissions = PermissionDecisionReceiptStore(runtime / "permissions.db")
    run_authority = RunDelegationGrantAuthority(
        store=RunDelegationGrantStore(runtime / "run-grants.db"),
        permission_store=permissions,
        harness_store=store,
        workspace_root=workspace,
    )
    parent = await permissions.issue(
        request_id="adversarial-parent",
        session_id="session-adversarial",
        run_id="run-adversarial",
        call_id="adversarial-parent",
        agent_name="main",
        tool_name="evolution_run_adversarial",
        tool_family="evolution",
        arguments={"request_id": request.request_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.BYPASS,
        risk_level="high",
        delegated_tool_names=("bash_run",),
        decided_at=datetime.now(UTC).isoformat(),
    )
    composer = ShellWorkerAdmissionComposer(
        worker_registry=WorkerRegistryStore(runtime / "workers.db"),
        harness_store=store,
        permission_store=permissions,
        execution_grant_store=ExecutionGrantStore(runtime / "execution-grants.db"),
        tool_job_store=ToolJobStore(runtime / "tool-jobs.db"),
        transport=AuthenticatedLocalShellTransport(runtime_dir=runtime / "transport"),
        software_version="test",
        run_delegation_grant_authority=run_authority,
    )
    sandbox_kernel = HarnessSandboxEvalExecutionKernel(
        workspace_root=workspace,
        permission_store=permissions,
        run_grant_authority=run_authority,
        sandbox_runner=HarnessSandboxCheckRunner(
            workspace_root=workspace,
            sandbox_root=tmp_path / "adversarial-sandboxes",
            artifact_root=tmp_path / "adversarial-artifacts",
        ),
        shell_admission_composer=composer,
        now=lambda: datetime.now(UTC).isoformat(),
    )
    executor = EvolutionAdversarialSampleExecutor(
        workspace_root=workspace,
        store=store,
        lease_store=EvolutionExperimentLeaseStore(tmp_path / "runtime.db"),
        worktree_storage_dir=Path(lease.worktree_path).parent,
        profile_service=HarnessService(workspace_root=workspace, trust_store=trust),
        sandbox_eval_kernel=sandbox_kernel,
    )

    receipts: list[EvolutionAdversarialSampleReceipt] = []
    for lane in (red_lane, green_lane):
        run_lease, grant = await _adversarial_run_authority(
            workspace=workspace,
            store=store,
            permissions=permissions,
            authority=run_authority,
            parent_receipt_id=parent.receipt_id,
            parent_run_id=parent.run_id,
            lane_order=lane.order,
        )
        run_evidence = HarnessSandboxEvalRunAuthority(
            parent_receipt_id=parent.receipt_id,
            run_id=parent.run_id,
            grant_id=grant.contract.grant_id,
            grant_sha256=grant.contract.grant_sha256,
        )
        receipt = await executor.execute(
            parent_receipt_id=parent.receipt_id,
            lane_order=lane.order,
            sample_index=0,
            batch_request=request,
            probe_contract=probes,
            validation_plan=plan,
            lease=lease,
            run_authority=run_evidence,
        )
        await _release_adversarial_run_authority(
            workspace=workspace,
            store=store,
            authority=run_authority,
            run_id=parent.run_id,
            run_lease=run_lease,
            grant_id=grant.contract.grant_id,
        )
        repeated = await executor.execute(
            parent_receipt_id=parent.receipt_id,
            lane_order=lane.order,
            sample_index=0,
            batch_request=request,
            probe_contract=probes,
            validation_plan=plan,
            lease=lease,
            run_authority=run_evidence,
        )
        assert repeated == receipt
        receipts.append(receipt)

    red, green = receipts
    assert red.phase == "red" and red.overlay_source_sha256 is None
    assert red.candidate_snapshot_revalidated is False
    assert green.phase == "green" and green.overlay_source_sha256 is not None
    assert green.candidate_snapshot_revalidated is True
    assert red.platform_identity == green.platform_identity
    assert red.check_ids == green.check_ids == (
        "adversarial_boundary",
        "adversarial_platform",
    )
    assert red.check_statuses == green.check_statuses == ("passed", "passed")
    assert red.run_grant_sha256 != green.run_grant_sha256
    assert red.source_tree_sha256 != green.source_tree_sha256
    assert red.harness_result_persisted and green.harness_result_persisted
    assert red.success_rule_revalidated and green.success_rule_revalidated
    for lane in (red_lane, green_lane):
        stored = await store.get_eval_result(
            workspace,
            lane.batch_id,
            request.suite_id,
            0,
        )
        assert stored is not None and stored.result.passed == 2
        assert all(
            case.metric_observations[0].metric.endswith(".exit_zero")
            and case.metric_observations[0].value == 1.0
            for case in stored.result.cases
        )
    case = stored.result.cases[0]
    forged_observation = case.metric_observations[0].model_copy(update={"value": 0.0})
    assert not adversarial_sample_module._case_evidence_is_valid(  # noqa: SLF001
        case.model_copy(update={"metric_observations": (forged_observation,)}),
        request,
    )
    final_lease = await store.get_run_lease(
        workspace_root=workspace,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    )
    assert final_lease is not None
    assert final_lease.state is HarnessRunLeaseState.RELEASED

    tampered = green.model_dump(mode="json")
    tampered["platform"] = "linux" if platform != "linux" else "macos"
    with pytest.raises(ValidationError, match="platform identity"):
        EvolutionAdversarialSampleReceipt.model_validate(tampered)

    candidate_path = Path(lease.worktree_path) / "src/naumi_agent/ui/footer.py"
    candidate_path.write_text(
        "def render_footer():\n    return 'drifted-after-green'\n",
        encoding="utf-8",
    )
    with pytest.raises(EvolutionAdversarialSampleError) as drifted:
        await executor.execute(
            parent_receipt_id=parent.receipt_id,
            lane_order=green_lane.order,
            sample_index=0,
            batch_request=request,
            probe_contract=probes,
            validation_plan=plan,
            lease=lease,
            run_authority=run_evidence,
        )
    assert drifted.value.code == "candidate_file_digest_mismatch"


@pytest.mark.asyncio
async def test_adversarial_sample_rejects_wrong_platform_before_permission_read(
    tmp_path: Path,
) -> None:
    workspace, contract, lease, plan, probes, trust = (
        await _adversarial_probe_fixture(tmp_path)
    )
    request = EvolutionAdversarialBatchRequestBuilder().build(
        experiment_contract=contract,
        validation_plan=plan,
        probe_contract=probes,
    )
    current = capture_eval_platform_identity().system
    if current == "unknown":
        pytest.skip("当前平台无法映射到 adversarial matrix。")
    wrong_lane = next(item for item in request.lanes if item.platform != current)
    permission_store = object()
    run_authority = type("RunAuthority", (), {
        "_workspace_root": workspace,
        "_permission_store": permission_store,
    })()
    kernel = HarnessSandboxEvalExecutionKernel(
        workspace_root=workspace,
        permission_store=permission_store,  # type: ignore[arg-type]
        run_grant_authority=run_authority,  # type: ignore[arg-type]
        sandbox_runner=type("Runner", (), {"workspace_root": workspace})(),  # type: ignore[arg-type]
        shell_admission_composer=type("Composer", (), {
            "_permission_store": permission_store,
            "_run_delegation_grant_authority": run_authority,
        })(),  # type: ignore[arg-type]
        now=lambda: datetime.now(UTC).isoformat(),
    )
    executor = EvolutionAdversarialSampleExecutor(
        workspace_root=workspace,
        store=HarnessStore(tmp_path / "platform-harness.db"),
        lease_store=EvolutionExperimentLeaseStore(tmp_path / "runtime.db"),
        worktree_storage_dir=Path(lease.worktree_path).parent,
        profile_service=HarnessService(workspace_root=workspace, trust_store=trust),
        sandbox_eval_kernel=kernel,
    )

    with pytest.raises(EvolutionAdversarialSampleError) as invalid_index:
        await executor.execute(
            parent_receipt_id="never-read",
            lane_order=wrong_lane.order,
            sample_index=True,
            batch_request=request,
            probe_contract=probes,
            validation_plan=plan,
            lease=lease,
            run_authority=HarnessSandboxEvalRunAuthority(
                parent_receipt_id="never-read",
                run_id="never-read",
                grant_id="never-read",
                grant_sha256="a" * 64,
            ),
        )
    assert invalid_index.value.code == "adversarial_sample_index_invalid"

    with pytest.raises(EvolutionAdversarialSampleError) as blocked:
        await executor.execute(
            parent_receipt_id="never-read",
            lane_order=wrong_lane.order,
            sample_index=0,
            batch_request=request,
            probe_contract=probes,
            validation_plan=plan,
            lease=lease,
            run_authority=HarnessSandboxEvalRunAuthority(
                parent_receipt_id="never-read",
                run_id="never-read",
                grant_id="never-read",
                grant_sha256="a" * 64,
            ),
        )
    assert blocked.value.code == "adversarial_platform_mismatch"


@pytest.mark.asyncio
async def test_baseline_cohort_request_is_deterministic_bounded_and_har08_ready(
    tmp_path: Path,
) -> None:
    workspace, contract, lease, plan, binding = await _validation_binding_fixture(
        tmp_path
    )
    builder = EvolutionBaselineCohortRequestBuilder()
    isolated_before = _git(Path(lease.worktree_path), "status", "--porcelain")

    first = builder.build(
        contract=contract,
        validation_plan=plan,
        profile_binding=binding,
    )
    second = builder.build(
        contract=contract,
        validation_plan=plan,
        profile_binding=binding,
    )

    assert first == second
    assert first.request_id == f"evvred_{first.request_sha256[:24]}"
    assert first.phase == "red"
    assert first.suite_id == f"evo_{plan.validation_plan_sha256[:24]}"
    assert first.batch_id == f"evo:red:{plan.validation_plan_sha256[:24]}"
    assert first.requested_samples == len(first.sample_seeds) == 5
    assert len(set(first.sample_seeds)) == 5
    assert first.baseline_commit == contract.baseline.commit
    assert first.source_materialization == "arc04_ephemeral_git_worktree"
    assert tuple(item.check_id for item in first.checks) == (
        "python_compile",
        "python_contract",
        "python_lint",
    )
    assert first.check_timeout_seconds_per_sample == 240
    assert first.max_total_duration_seconds == contract.budget.max_duration_seconds
    assert first.metrics[0].metric_name == plan.metrics[0].metric_name
    assert first.metrics[0].baseline_operation == "measure"
    assert first.runtime_identity_required is True
    assert first.profile_trust_revalidation_required is True
    assert first.metric_timeout_binding_required is True
    assert first.harness_result_store_required is True
    assert first.har08_comparison_receipt_required is True
    assert first.candidate_request_allowed is False
    assert first.arc04_worker_required is True
    assert first.execution_ready is False
    serialized = first.model_dump_json()
    assert "tests/unit/test_footer.py" not in serialized
    assert lease.worktree_path not in serialized
    assert _git(workspace, "status", "--porcelain") == ""
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == isolated_before


@pytest.mark.asyncio
async def test_baseline_cohort_request_rejects_sample_budget_and_nested_tampering(
    tmp_path: Path,
) -> None:
    _, contract, _, plan, binding = await _validation_binding_fixture(tmp_path)
    builder = EvolutionBaselineCohortRequestBuilder()
    with pytest.raises(EvolutionCohortRequestError) as too_few:
        builder.build(
            contract=contract,
            validation_plan=plan,
            profile_binding=binding,
            requested_samples=4,
        )
    assert too_few.value.code == "baseline_sample_count_invalid"

    with pytest.raises(EvolutionCohortRequestError) as over_budget:
        builder.build(
            contract=contract,
            validation_plan=plan,
            profile_binding=binding,
            requested_samples=6,
        )
    assert over_budget.value.code == "baseline_duration_budget_exceeded"

    request = builder.build(
        contract=contract,
        validation_plan=plan,
        profile_binding=binding,
    )
    tampered = request.model_dump(mode="json")
    tampered["sample_seeds"][0] += 1
    with pytest.raises(ValidationError, match="sample seeds"):
        EvolutionBaselineCohortRequest.model_validate(tampered)

    missing_coverage = request.model_dump(mode="json")
    missing_coverage["checks"][1]["coverage"] = missing_coverage["checks"][1][
        "coverage"
    ][:1]
    with pytest.raises(ValidationError, match="Binding requirements"):
        EvolutionBaselineCohortRequest.model_validate(missing_coverage)


def _metric_case(
    *,
    metric_name: str,
    verifier: str,
) -> BaselineCohortMetricCase:
    return BaselineCohortMetricCase(
        order=1,
        metric_name=metric_name,
        direction="decrease",
        target=0,
        verifier=verifier,
        procedure_sha256="a" * 64,
    )


def test_metric_runner_registry_binds_only_real_mechanical_runners(
    tmp_path: Path,
) -> None:
    registry = EvolutionMetricRunnerRegistry()
    source = tmp_path / "src" / "naumi_agent" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def risky() -> None:\n    try:\n        pass\n    except Exception:\n        pass\n",
        encoding="utf-8",
    )
    validation_paths = ("src/naumi_agent/example.py",)

    static = registry.resolve(
        _metric_case(
            metric_name="self_review.broad_except.count",
            verifier="self_review_static",
        ),
        validation_paths=validation_paths,
    )
    replay = registry.resolve(
        _metric_case(metric_name="replay.error_rate", verifier="harness_replay"),
        validation_paths=validation_paths,
    )
    feedback = registry.resolve(
        _metric_case(
            metric_name="feedback.same_root.recurrence",
            verifier="feedback_recurrence",
        ),
        validation_paths=validation_paths,
    )

    assert static.status == "ready"
    assert static.runner_version == "self_review_static@1"
    assert static.timeout_seconds_per_sample == 30
    assert static.finding_code == "broad_except"
    assert static.fixture_sha256 is not None
    assert static.model_access is static.network_access is False
    assert static.side_effect_free is True
    scan = scan_self_review_files([source], workspace_root=tmp_path)
    assert sum(
        finding.code.value == static.finding_code for finding in scan.findings
    ) == 1
    assert replay.status == "blocked"
    assert replay.runner_version == SAFE_REPLAY_EVAL_RUNNER_VERSION
    assert replay.blocking_code == "replay_fixture_required"
    assert feedback.status == "blocked"
    assert feedback.runner_version is None
    assert feedback.blocking_code == "feedback_window_runner_unavailable"


def test_metric_runner_registry_rejects_unsupported_self_review_metric() -> None:
    resolution = EvolutionMetricRunnerRegistry().resolve(
        _metric_case(
            metric_name="self_review.imaginary.count",
            verifier="self_review_static",
        ),
        validation_paths=("src/naumi_agent/example.py",),
    )

    assert resolution.status == "blocked"
    assert resolution.blocking_code == "self_review_metric_unsupported"

    with pytest.raises(EvolutionMetricBindingError) as unsafe:
        EvolutionMetricRunnerRegistry().resolve(
            _metric_case(
                metric_name="self_review.broad_except.count",
                verifier="self_review_static",
            ),
            validation_paths=("../outside.py",),
        )
    assert unsafe.value.code == "validation_paths_invalid"


def test_metric_runner_budget_blocks_an_otherwise_ready_runner() -> None:
    metric = _metric_case(
        metric_name="self_review.broad_except.count",
        verifier="self_review_static",
    )
    resolution = EvolutionMetricRunnerRegistry().resolve(
        metric,
        validation_paths=("src/naumi_agent/example.py",),
    )
    entry = MetricRunnerBindingEntry(
        order=metric.order,
        metric_name=metric.metric_name,
        direction=metric.direction,
        target=metric.target,
        procedure_sha256=metric.procedure_sha256,
        resolution=resolution,
    )

    blocked = metric_binding_module._apply_duration_budget(
        (entry,),
        required_duration_seconds=1_350,
        max_total_duration_seconds=1_200,
    )

    assert blocked[0].resolution.runner_version == "self_review_static@1"
    assert blocked[0].resolution.timeout_seconds_per_sample == 30
    assert blocked[0].resolution.status == "blocked"
    assert blocked[0].resolution.blocking_code == "metric_duration_budget_exceeded"


@pytest.mark.asyncio
async def test_metric_runner_binding_is_deterministic_blocked_and_tamper_evident(
    tmp_path: Path,
) -> None:
    workspace, contract, lease, plan, profile_binding = (
        await _validation_binding_fixture(tmp_path)
    )
    baseline_request = EvolutionBaselineCohortRequestBuilder().build(
        contract=contract,
        validation_plan=plan,
        profile_binding=profile_binding,
    )
    builder = EvolutionMetricRunnerBindingBuilder()
    main_before = _git(workspace, "status", "--porcelain")
    isolated_before = _git(Path(lease.worktree_path), "status", "--porcelain")

    first = builder.build(
        baseline_request=baseline_request,
        validation_plan=plan,
    )
    second = builder.build(
        baseline_request=baseline_request,
        validation_plan=plan,
    )

    assert first == second
    assert first.binding_id == f"evvmetric_{first.binding_sha256[:24]}"
    assert first.binding_status == "blocked"
    assert first.blocking_codes == ("feedback_window_runner_unavailable",)
    assert first.metric_binding_complete is False
    assert first.profile_timeout_seconds_total == 1_200
    assert first.metric_timeout_seconds_total == 0
    assert first.required_duration_seconds == 1_200
    assert first.budget_headroom_seconds == 0
    assert first.entries[0].resolution.status == "blocked"
    assert first.execution_ready is False
    assert _git(workspace, "status", "--porcelain") == main_before
    assert _git(Path(lease.worktree_path), "status", "--porcelain") == isolated_before

    tampered = first.model_dump(mode="json")
    tampered["entries"][0]["resolution"]["blocking_code"] = "invented_success"
    with pytest.raises(ValidationError, match="blocking codes|摘要"):
        EvolutionMetricRunnerBinding.model_validate(tampered)


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
