from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.daemons.execution_grants import ExecutionGrantStore
from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
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
from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.failure_attribution import (
    EvolutionFailureAttributionError,
    EvolutionFailureAttributionStore,
    FailureAttributionAction,
    FailureAttributionCategory,
)
from naumi_agent.evolution.interventional_comparison import (
    EvolutionInterventionalComparisonError,
    EvolutionInterventionalComparisonExecutor,
)
from naumi_agent.evolution.interventional_failure_attribution import (
    EvolutionInterventionalFailureAttributionExecutor,
)
from naumi_agent.evolution.interventional_green_cohort import (
    EvolutionInterventionalGreenCohortError,
    EvolutionInterventionalGreenCohortExecutor,
)
from naumi_agent.evolution.interventional_green_request import (
    EvolutionInterventionalGreenCohortRequestBuilder,
    EvolutionInterventionalGreenRequestError,
)
from naumi_agent.evolution.interventional_green_sample import (
    EvolutionInterventionalGreenSampleError,
    EvolutionInterventionalGreenSampleExecutor,
)
from naumi_agent.evolution.interventional_red_cohort import (
    EvolutionInterventionalRedCohortError,
    EvolutionInterventionalRedCohortExecutor,
)
from naumi_agent.evolution.interventional_red_sample import (
    EvolutionInterventionalRedSampleError,
    EvolutionInterventionalRedSampleExecutor,
)
from naumi_agent.evolution.validation_cohorts import (
    BASELINE_COHORT_REQUEST_POLICY,
    BaselineCohortCheckCase,
    BaselineCohortMetricCase,
    EvolutionBaselineCohortRequest,
    _sample_seeds,
)
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricRunnerBindingBuilder,
)
from naumi_agent.evolution.validation_plans import (
    VALIDATION_PLAN_POLICY,
    EvolutionValidationPlan,
    EvolutionValidationProfileBinder,
    ValidationCheckCoverage,
    ValidationFileRequirement,
    ValidationMetricPair,
)
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckRunner
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.safety.permissions import PermissionMode


def _sha256(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    ).stdout


def _require_real_backend() -> None:
    try:
        detect_shell_sandbox_backend()
    except ShellSandboxUnavailableError as exc:
        pytest.skip(str(exc))


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value.isoformat()


class _AdvancingSampleExecutor:
    def __init__(self, delegate, clock: _MutableClock, *, fail_on_call: int | None = None) -> None:
        self.delegate = delegate
        self.clock = clock
        self.calls = 0
        self.fail_on_call = fail_on_call

    async def execute(self, **kwargs):
        if self.fail_on_call == self.calls + 1:
            raise RuntimeError("simulated cohort interruption")
        result = await self.delegate.execute(**kwargs)
        self.calls += 1
        if self.calls == 1:
            self.clock.value += timedelta(seconds=301)
        return result

    async def revalidate_cohort_authority(self, **kwargs):
        return await self.delegate.revalidate_cohort_authority(**kwargs)

    async def validate_cohort_prefix(self, **kwargs):
        return await self.delegate.validate_cohort_prefix(**kwargs)


class _LeaseStore:
    def __init__(self, lease: ExperimentWorktreeLease) -> None:
        self.lease = lease

    async def get(self, contract_id: str) -> ExperimentWorktreeLease | None:
        return self.lease if contract_id == self.lease.contract_id else None


async def _authority(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".naumi").mkdir()
    profile_raw = (
        "schema_version: 1\n"
        "checks:\n"
        "  - id: unit\n"
        "    argv:\n"
        "      - python3\n"
        "      - -c\n"
        "      - 'from pathlib import Path; "
        "assert Path(\"sample.py\").read_text() == \"baseline\\n\"; "
        "print(\"baseline-ok\")'\n"
        "    timeout_seconds: 10\n"
        "    when_changed: ['**/*.py']\n"
        "    required_for: [change]\n"
        "    provides: [unit]\n"
    )
    (root / ".naumi" / "harness.yaml").write_text(profile_raw)
    (root / "sample.py").write_text("baseline\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    commit = _git(root, "rev-parse", "HEAD").decode().strip()
    tree_sha256 = hashlib.sha256(
        _git(root, "ls-tree", "-r", "-z", "--full-tree", commit)
    ).hexdigest()
    profile_sha256 = hashlib.sha256(profile_raw.encode()).hexdigest()
    metric = ValidationMetricPair(
        order=1,
        metric_name="self_review.broad_except.count",
        direction="decrease",
        target=0,
        verifier="self_review_static",
        procedure="统计 broad_except。",
    )
    candidate_source = (
        b"try:\n"
        b"    raise RuntimeError('candidate')\n"
        b"except Exception:\n"
        b"    pass\n"
    )
    file = ValidationFileRequirement(
        path="sample.py",
        file_kind="python",
        required_checks=("unit",),
        operation="modify",
        baseline_sha256=hashlib.sha256(b"baseline\n").hexdigest(),
        candidate_sha256=hashlib.sha256(candidate_source).hexdigest(),
    )
    contract_id = f"evx_{'1' * 24}"
    contract_manifest_sha256 = "2" * 64
    lease_digest = hashlib.sha256(
        f"{contract_id}:{contract_manifest_sha256}".encode()
    ).hexdigest()
    plan_payload = {
        "schema_version": 2,
        "policy_version": VALIDATION_PLAN_POLICY,
        "contract_id": contract_id,
        "contract_manifest_sha256": contract_manifest_sha256,
        "lease_id": f"evl_{lease_digest[:24]}",
        "source_snapshot_id": f"evs_{'4' * 24}",
        "source_snapshot_sha256": "5" * 64,
        "mutation_receipt_id": f"evmr_{'6' * 24}",
        "mutation_receipt_sha256": "7" * 64,
        "candidate_id": f"evc_{'8' * 24}",
        "candidate_revision": 1,
        "seed": 42,
        "baseline_commit": commit,
        "baseline_tree_sha256": tree_sha256,
        "profile_sha256": profile_sha256,
        "experiment_config_sha256": "a" * 64,
        "toolset_sha256": "b" * 64,
        "candidate_files_sha256": "c" * 64,
        "files": [file.model_dump(mode="json")],
        "metrics": [metric.model_dump(mode="json")],
        "required_check_kinds": ["unit"],
        "baseline_first": True,
        "identical_environment_required": True,
        "har08_comparison_receipt_required": True,
        "validation_ready": True,
        "runner_binding_status": "required",
        "execution_ready": False,
        "promotion_ready": False,
    }
    plan_digest = _sha256(plan_payload)
    plan = EvolutionValidationPlan.model_validate({
        **plan_payload,
        "validation_plan_id": f"evvplan_{plan_digest[:24]}",
        "validation_plan_sha256": plan_digest,
    })
    trust = HarnessTrustStore(tmp_path / "trust.db")
    await trust.trust(root, profile_sha256, source="test")
    profile_binding = await EvolutionValidationProfileBinder(trust).bind(
        plan,
        workspace_root=root,
    )
    snapshot_check = HarnessCheckSpec(
        id="unit",
        argv=(
            "python3",
            "-c",
            'from pathlib import Path; '
            'assert Path("sample.py").read_text() == "baseline\\n"; '
            'print("baseline-ok")',
        ),
        timeout_seconds=10,
        when_changed=("**/*.py",),
        required_for=("change",),
        provides=("unit",),
    )
    coverage = ValidationCheckCoverage(
        path="sample.py", check_kind="unit", check_id="unit"
    )
    bound = profile_binding.checks[0]
    check = BaselineCohortCheckCase(
        order=1,
        check_id="unit",
        spec_sha256=_sha256(snapshot_check.model_dump(mode="json")),
        argv_sha256=_sha256(list(snapshot_check.argv)),
        timeout_seconds=10,
        coverage=(coverage,),
    )
    baseline_metric = BaselineCohortMetricCase(
        order=1,
        metric_name=metric.metric_name,
        direction=metric.direction,
        target=metric.target,
        verifier=metric.verifier,
        procedure_sha256=hashlib.sha256(metric.procedure.encode()).hexdigest(),
    )
    assert check.spec_sha256 == bound.spec_sha256
    request_payload = {
        "schema_version": 1,
        "policy_version": BASELINE_COHORT_REQUEST_POLICY,
        "contract_id": plan.contract_id,
        "contract_manifest_sha256": plan.contract_manifest_sha256,
        "validation_plan_id": plan.validation_plan_id,
        "validation_plan_sha256": plan.validation_plan_sha256,
        "profile_binding_id": profile_binding.binding_id,
        "profile_binding_sha256": profile_binding.binding_sha256,
        "profile_binding_requirements_sha256": profile_binding.plan_requirements_sha256,
        "candidate_id": plan.candidate_id,
        "candidate_revision": plan.candidate_revision,
        "phase": "red",
        "suite_id": f"evo_{plan.validation_plan_sha256[:24]}",
        "batch_id": f"evo:red:{plan.validation_plan_sha256[:24]}",
        "requested_samples": 5,
        "base_seed": plan.seed,
        "sample_seeds": list(_sample_seeds(plan.seed, plan.validation_plan_sha256, 5)),
        "baseline_commit": commit,
        "baseline_tree_sha256": tree_sha256,
        "profile_sha256": profile_sha256,
        "experiment_config_sha256": plan.experiment_config_sha256,
        "toolset_sha256": plan.toolset_sha256,
        "source_materialization": "arc04_ephemeral_git_worktree",
        "checks": [check.model_dump(mode="json")],
        "metrics": [baseline_metric.model_dump(mode="json")],
        "check_timeout_seconds_per_sample": 10,
        "max_total_duration_seconds": 600,
        "network_access": False,
        "dependency_installation": False,
        "runtime_identity_required": True,
        "profile_trust_revalidation_required": True,
        "metric_timeout_binding_required": True,
        "continuous_sample_indexes_required": True,
        "harness_result_store_required": True,
        "har08_comparison_receipt_required": True,
        "candidate_request_allowed": False,
        "request_ready": True,
        "arc04_worker_required": True,
        "execution_ready": False,
    }
    request_digest = _sha256(request_payload)
    request = EvolutionBaselineCohortRequest.model_validate({
        **request_payload,
        "request_id": f"evvred_{request_digest[:24]}",
        "request_sha256": request_digest,
    })
    metrics = EvolutionMetricRunnerBindingBuilder().build(
        baseline_request=request,
        validation_plan=plan,
    )
    return root, trust, plan, profile_binding, request, metrics


@pytest.mark.asyncio
async def test_interventional_red_executes_exact_revision_and_releases_authority(
    tmp_path: Path,
) -> None:
    _require_real_backend()
    root, trust, plan, profile_binding, request, metrics = await _authority(tmp_path)
    (root / "sample.py").write_text(
        "try:\n"
        "    raise RuntimeError('candidate')\n"
        "except Exception:\n"
        "    pass\n"
    )
    runtime = tmp_path / "runtime"
    store = HarnessStore(tmp_path / "harness.db")
    permissions = PermissionDecisionReceiptStore(runtime / "permissions.db")
    run_grants = RunDelegationGrantStore(runtime / "run-grants.db")
    run_authority = RunDelegationGrantAuthority(
        store=run_grants,
        permission_store=permissions,
        harness_store=store,
        workspace_root=root,
    )
    parent = await permissions.issue(
        request_id="red-parent",
        session_id="session-red",
        run_id="run-red",
        call_id="red-parent",
        agent_name="main",
        tool_name="evolution_run_baseline",
        tool_family="evolution",
        arguments={"request_id": request.request_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
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
    executor = EvolutionInterventionalRedSampleExecutor(
        workspace_root=root,
        store=store,
        permission_store=permissions,
        run_grant_authority=run_authority,
        profile_service=HarnessService(workspace_root=root, trust_store=trust),
        sandbox_runner=HarnessSandboxCheckRunner(
            workspace_root=root,
            sandbox_root=tmp_path / "sandboxes",
            artifact_root=tmp_path / "artifacts",
        ),
        shell_admission_composer=composer,
    )

    receipt = await executor.execute(
        parent_receipt_id=parent.receipt_id,
        sample_index=0,
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
    )
    repeated = await executor.execute(
        parent_receipt_id=parent.receipt_id,
        sample_index=0,
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
    )

    assert receipt == repeated
    assert receipt.check_statuses == ("passed",)
    assert len(receipt.lifecycle_receipt_sha256) == 1
    assert receipt.metrics_executed is True
    stored = await store.get_eval_result(root, request.batch_id, request.suite_id, 0)
    assert stored is not None and stored.result.passed == 2
    metric_case = stored.result.cases[1]
    assert metric_case.metric_observations[0].metric == (
        "self_review.broad_except.count"
    )
    assert metric_case.metric_observations[0].value == 0
    lease = await store.get_run_lease(
        workspace_root=root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.RELEASED
    with sqlite3.connect(runtime / "run-grants.db") as db:
        states = db.execute(
            "SELECT state, revoke_reason FROM run_delegation_grants"
        ).fetchall()
    assert states == [("revoked", "sample_finished")]
    cohort = EvolutionInterventionalRedCohortExecutor(
        workspace_root=root,
        store=store,
        permission_store=permissions,
        run_grant_authority=run_authority,
        sample_executor=executor,
    )
    with pytest.raises(EvolutionInterventionalRedCohortError) as captured:
        await cohort.execute(
            parent_receipt_id=parent.receipt_id,
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
        )
    assert captured.value.code == "cohort_run_authority_evidence_incomplete"


@pytest.mark.asyncio
async def test_interventional_cohorts_reuse_authority_and_execute_candidate_overlay(
    tmp_path: Path,
) -> None:
    _require_real_backend()
    root, trust, plan, profile_binding, request, metrics = await _authority(tmp_path)
    runtime = tmp_path / "runtime"
    store = HarnessStore(tmp_path / "harness.db")
    permissions = PermissionDecisionReceiptStore(runtime / "permissions.db")
    run_grants = RunDelegationGrantStore(runtime / "run-grants.db")
    clock = _MutableClock(datetime(2026, 7, 19, tzinfo=UTC))
    run_authority = RunDelegationGrantAuthority(
        store=run_grants,
        permission_store=permissions,
        harness_store=store,
        workspace_root=root,
    )
    parent = await permissions.issue(
        request_id="cohort-parent",
        session_id="session-cohort",
        run_id="run-cohort",
        call_id="cohort-parent",
        agent_name="main",
        tool_name="evolution_run_baseline",
        tool_family="evolution",
        arguments={"request_id": request.request_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        delegated_tool_names=("bash_run",),
        decided_at=clock(),
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
        now=clock,
    )
    sample = EvolutionInterventionalRedSampleExecutor(
        workspace_root=root,
        store=store,
        permission_store=permissions,
        run_grant_authority=run_authority,
        profile_service=HarnessService(workspace_root=root, trust_store=trust),
        sandbox_runner=HarnessSandboxCheckRunner(
            workspace_root=root,
            sandbox_root=tmp_path / "sandboxes",
            artifact_root=tmp_path / "artifacts",
        ),
        shell_admission_composer=composer,
        now=clock,
    )
    advancing = _AdvancingSampleExecutor(sample, clock, fail_on_call=3)
    cohort = EvolutionInterventionalRedCohortExecutor(
        workspace_root=root,
        store=store,
        permission_store=permissions,
        run_grant_authority=run_authority,
        sample_executor=advancing,
        now=clock,
        token=lambda: "a" * 32,
    )

    with pytest.raises(RuntimeError, match="simulated cohort interruption"):
        await cohort.execute(
            parent_receipt_id=parent.receipt_id,
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
        )
    prefix = await store.list_eval_results(
        root,
        request.batch_id,
        request.suite_id,
        limit=6,
    )
    assert tuple(item.sample_index for item in prefix) == (0, 1)
    resumed_parent = await permissions.issue(
        request_id="resumed-cohort-parent",
        session_id="session-cohort",
        run_id="run-cohort-resumed",
        call_id="resumed-cohort-parent",
        agent_name="main",
        tool_name="evolution_run_baseline",
        tool_family="evolution",
        arguments={"request_id": request.request_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        delegated_tool_names=("bash_run",),
        decided_at=clock(),
    )
    resumed = EvolutionInterventionalRedCohortExecutor(
        workspace_root=root,
        store=store,
        permission_store=permissions,
        run_grant_authority=run_authority,
        sample_executor=sample,
        now=clock,
        token=lambda: "b" * 32,
    )
    receipt = await resumed.execute(
        parent_receipt_id=resumed_parent.receipt_id,
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
    )
    repeated = await resumed.execute(
        parent_receipt_id="not-needed-after-completion",
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
    )

    assert receipt == repeated
    assert receipt.persisted_samples == receipt.requested_samples == 5
    assert receipt.metrics[0].sample_values == (0, 0, 0, 0, 0)
    assert receipt.checks[0].passed == 5
    assert len(set(receipt.sample_receipt_sha256)) == 5
    assert len(receipt.cohort_run_grant_sha256) == 2
    assert clock.value == datetime(2026, 7, 19, tzinfo=UTC) + timedelta(seconds=301)
    records = await store.list_eval_results(
        root,
        request.batch_id,
        request.suite_id,
        limit=6,
    )
    assert tuple(item.sample_index for item in records) == (0, 1, 2, 3, 4)
    lease = await store.get_run_lease(
        workspace_root=root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=resumed_parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.RELEASED
    with sqlite3.connect(runtime / "run-grants.db") as db:
        states = db.execute(
            "SELECT state, revoke_reason FROM run_delegation_grants"
        ).fetchall()
    assert states == [
        ("revoked", "cohort_finished"),
        ("revoked", "cohort_finished"),
    ]
    worktree_storage = tmp_path / "worktrees"
    worktree_storage.mkdir()
    worktree_name = f"experiment-{plan.contract_id.removeprefix('evx_')[:16]}"
    candidate_root = worktree_storage / worktree_name
    candidate_branch = "experiment/candidate"
    _git(
        root,
        "worktree",
        "add",
        "-q",
        "-b",
        candidate_branch,
        str(candidate_root),
        plan.baseline_commit,
    )
    candidate_source = (
        "try:\n"
        "    raise RuntimeError('candidate')\n"
        "except Exception:\n"
        "    pass\n"
    )
    (candidate_root / "sample.py").write_text(candidate_source)
    lease = ExperimentWorktreeLease(
        lease_id=plan.lease_id,
        contract_id=plan.contract_id,
        manifest_sha256=plan.contract_manifest_sha256,
        session_id="session-cohort",
        mission_id="mission-cohort",
        task_id="task-cohort",
        owner="owner-cohort",
        state=ExperimentLeaseState.ACTIVE,
        worktree_name=worktree_name,
        worktree_path=str(candidate_root),
        branch=candidate_branch,
        baseline_commit=plan.baseline_commit,
        expires_at="2026-07-19T01:00:00+00:00",
        created_at="2026-07-19T00:00:00+00:00",
        updated_at="2026-07-19T00:00:00+00:00",
        worktree_ready=True,
    )
    green = EvolutionInterventionalGreenCohortRequestBuilder().build(
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
        red_receipt=receipt,
        lease=lease,
    )
    repeated_green = EvolutionInterventionalGreenCohortRequestBuilder().build(
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
        red_receipt=receipt,
        lease=lease,
    )
    assert green == repeated_green
    assert green.sample_seeds == request.sample_seeds
    assert green.suite_id == request.suite_id
    assert green.lease_id == plan.lease_id
    assert green.project_code_execution_allowed
    assert green.arc04_worker_required
    assert not green.execution_ready
    with pytest.raises(ValueError):
        type(green).model_validate(
            green.model_copy(update={"candidate_revision": 2}).model_dump(mode="json")
        )

    stale_lease = lease.model_copy(update={
        "state": ExperimentLeaseState.RELEASED,
        "worktree_ready": False,
    })
    with pytest.raises(EvolutionInterventionalGreenRequestError) as captured:
        EvolutionInterventionalGreenCohortRequestBuilder().build(
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
            red_receipt=receipt,
            lease=stale_lease,
        )
    assert captured.value.code == "interventional_green_authority_mismatch"

    tampered_red = receipt.model_copy(update={"persisted_samples": 4})
    with pytest.raises(EvolutionInterventionalGreenRequestError) as captured:
        EvolutionInterventionalGreenCohortRequestBuilder().build(
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
            red_receipt=tampered_red,
            lease=lease,
        )
    assert captured.value.code == "interventional_green_authority_invalid"

    green_parent = await permissions.issue(
        request_id="green-parent",
        session_id="session-green",
        run_id="run-green",
        call_id="green-parent",
        agent_name="main",
        tool_name="evolution_run_candidate",
        tool_family="evolution",
        arguments={"request_id": green.request_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        delegated_tool_names=("bash_run",),
        decided_at=clock(),
    )
    green_executor = EvolutionInterventionalGreenSampleExecutor(
        workspace_root=root,
        store=store,
        lease_store=_LeaseStore(lease),  # type: ignore[arg-type]
        worktree_storage_dir=worktree_storage,
        sample_kernel=sample.sample_kernel,
        clock=lambda: clock.value,
    )
    with pytest.raises(EvolutionInterventionalGreenSampleError) as captured:
        await green_executor.execute(
            parent_receipt_id="must-not-acquire-authority",
            sample_index=0,
            green_request=green.model_copy(update={"candidate_revision": 2}),
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
            red_receipt=receipt,
            lease=lease,
        )
    assert captured.value.code == "green_sample_authority_invalid"
    advancing_green = _AdvancingSampleExecutor(
        green_executor,
        clock,
        fail_on_call=2,
    )
    green_cohort = EvolutionInterventionalGreenCohortExecutor(
        workspace_root=root,
        store=store,
        permission_store=permissions,
        run_grant_authority=run_authority,
        sample_executor=advancing_green,  # type: ignore[arg-type]
        now=clock,
        token=lambda: "c" * 32,
    )
    with pytest.raises(EvolutionInterventionalGreenCohortError) as captured:
        await green_cohort.execute(
            parent_receipt_id="must-not-acquire-cohort-authority",
            green_request=green.model_copy(update={"candidate_revision": 2}),
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
            red_receipt=receipt,
            lease=lease,
        )
    assert captured.value.code == "green_cohort_authority_invalid"
    with pytest.raises(RuntimeError, match="simulated cohort interruption"):
        await green_cohort.execute(
            parent_receipt_id=green_parent.receipt_id,
            green_request=green,
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
            red_receipt=receipt,
            lease=lease,
        )
    green_prefix = await store.list_eval_results(
        root,
        green.batch_id,
        green.suite_id,
        limit=6,
    )
    assert tuple(item.sample_index for item in green_prefix) == (0,)
    green_receipt = await green_executor.execute(
        parent_receipt_id="not-needed-for-existing-green",
        sample_index=0,
        green_request=green,
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
        red_receipt=receipt,
        lease=lease,
    )
    repeated_receipt = await green_executor.execute(
        parent_receipt_id="not-needed-for-existing-green",
        sample_index=0,
        green_request=green,
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
        red_receipt=receipt,
        lease=lease,
    )

    assert green_receipt == repeated_receipt
    assert green_receipt.check_statuses == ("failed",)
    assert green_receipt.candidate_snapshot_revalidated
    green_stored = await store.get_eval_result(
        root,
        green.batch_id,
        green.suite_id,
        0,
    )
    assert green_stored is not None
    assert green_stored.result.baseline_identity is not None
    assert green_stored.result.baseline_identity.source.dirty
    assert green_stored.result.cases[0].status.value == "implementation_failure"
    assert green_stored.result.cases[1].metric_observations[0].value == 1
    assert (root / "sample.py").read_text() == "baseline\n"
    green_runtime_lease = await store.get_run_lease(
        workspace_root=root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=green_parent.run_id,
    )
    assert (
        green_runtime_lease is not None
        and green_runtime_lease.state is HarnessRunLeaseState.RELEASED
    )
    with sqlite3.connect(runtime / "run-grants.db") as db:
        final_grant = db.execute(
            "SELECT state, revoke_reason FROM run_delegation_grants ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert final_grant == ("revoked", "cohort_finished")

    resumed_green_parent = await permissions.issue(
        request_id="green-parent-resumed",
        session_id="session-green",
        run_id="run-green-resumed",
        call_id="green-parent-resumed",
        agent_name="main",
        tool_name="evolution_run_candidate",
        tool_family="evolution",
        arguments={"request_id": green.request_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        delegated_tool_names=("bash_run",),
        decided_at=clock(),
    )
    resumed_green = EvolutionInterventionalGreenCohortExecutor(
        workspace_root=root,
        store=store,
        permission_store=permissions,
        run_grant_authority=run_authority,
        sample_executor=green_executor,
        now=clock,
        token=lambda: "d" * 32,
    )
    green_cohort_receipt = await resumed_green.execute(
        parent_receipt_id=resumed_green_parent.receipt_id,
        green_request=green,
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
        red_receipt=receipt,
        lease=lease,
    )
    repeated_green_cohort = await resumed_green.execute(
        parent_receipt_id="not-needed-after-green-completion",
        green_request=green,
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
        red_receipt=receipt,
        lease=lease,
    )
    assert green_cohort_receipt == repeated_green_cohort
    assert green_cohort_receipt.persisted_samples == 5
    assert green_cohort_receipt.metrics[0].sample_values == (1, 1, 1, 1, 1)
    assert green_cohort_receipt.checks[0].failed == 5
    assert len(green_cohort_receipt.cohort_run_grant_sha256) == 2
    assert len(set(green_cohort_receipt.sample_receipt_sha256)) == 5
    resumed_green_lease = await store.get_run_lease(
        workspace_root=root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=resumed_green_parent.run_id,
    )
    assert (
        resumed_green_lease is not None
        and resumed_green_lease.state is HarnessRunLeaseState.RELEASED
    )
    with sqlite3.connect(runtime / "run-grants.db") as db:
        all_grants = db.execute(
            "SELECT state, revoke_reason FROM run_delegation_grants ORDER BY rowid"
        ).fetchall()
    assert all_grants == [("revoked", "cohort_finished")] * 4
    with pytest.raises(ValueError):
        type(green_cohort_receipt).model_validate(
            green_cohort_receipt.model_copy(
                update={"persisted_samples": 4}
            ).model_dump(mode="json")
        )
    comparison_executor = EvolutionInterventionalComparisonExecutor(store)
    with pytest.raises(EvolutionInterventionalComparisonError) as captured:
        await comparison_executor.execute(
            workspace_root=root,
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
            red_receipt=receipt,
            green_request=green,
            green_receipt=green_cohort_receipt.model_copy(
                update={"candidate_revision": 2}
            ),
        )
    assert captured.value.code == "interventional_comparison_authority_invalid"
    assert await store.get_eval_baseline_by_batch(
        root,
        request.suite_id,
        request.batch_id,
    ) is None
    comparison = await comparison_executor.execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
        red_receipt=receipt,
        green_request=green,
        green_receipt=green_cohort_receipt,
    )
    repeated_comparison = await comparison_executor.execute(
        workspace_root=root,
        baseline_request=request,
        metric_binding=metrics,
        validation_plan=plan,
        profile_binding=profile_binding,
        red_receipt=receipt,
        green_request=green,
        green_receipt=green_cohort_receipt,
    )
    reference = await store.get_eval_baseline_by_batch(
        root,
        request.suite_id,
        request.batch_id,
    )
    assert comparison == repeated_comparison
    assert reference is not None and reference.purpose == "comparison_reference"
    assert await store.get_active_eval_baseline(root, request.suite_id) is None
    assert comparison.receipt.statistical_verdict == "regressed"
    assert comparison.receipt.decision == "failed"
    assert len(comparison.receipt.sample_evidence) == 5
    assert all(
        item.policy_verdict == "failed"
        for item in comparison.receipt.sample_evidence
    )
    attribution_store = EvolutionFailureAttributionStore(
        runtime / "failure-attributions.db"
    )
    attribution_executor = EvolutionInterventionalFailureAttributionExecutor(
        harness_store=store,
        attribution_store=attribution_store,
    )
    with pytest.raises(EvolutionFailureAttributionError) as captured:
        await attribution_executor.execute(
            validation_plan=plan,
            red_receipt=receipt,
            green_receipt=green_cohort_receipt,
            comparison=replace(comparison, decision="passed"),
        )
    assert (
        captured.value.code
        == "interventional_attribution_comparison_not_authoritative"
    )
    with pytest.raises(EvolutionFailureAttributionError) as captured:
        await attribution_executor.execute(
            validation_plan=plan,
            red_receipt=receipt,
            green_receipt=green_cohort_receipt.model_copy(
                update={"candidate_revision": 2}
            ),
            comparison=comparison,
        )
    assert captured.value.code == "interventional_attribution_authority_invalid"
    attribution = await attribution_executor.execute(
        validation_plan=plan,
        red_receipt=receipt,
        green_receipt=green_cohort_receipt,
        comparison=comparison,
    )
    repeated_attribution = await attribution_executor.execute(
        validation_plan=plan,
        red_receipt=receipt,
        green_receipt=green_cohort_receipt,
        comparison=comparison,
    )
    restored_attribution = await EvolutionFailureAttributionStore(
        runtime / "failure-attributions.db"
    ).get(comparison.id)
    assert attribution == repeated_attribution == restored_attribution
    assert attribution.category is FailureAttributionCategory.CANDIDATE_DEFECT
    assert attribution.action is FailureAttributionAction.REVISE_CANDIDATE
    assert attribution.reason_code == "candidate_policy_failed"
    assert attribution.candidate_fault
    assert not attribution.retryable
    assert not attribution.requires_rerun
    assert not attribution.reflection_eligible
    assert attribution.red_receipt_id == receipt.receipt_id
    assert attribution.green_receipt_id == green_cohort_receipt.receipt_id

    (candidate_root / "sample.py").write_text(candidate_source + "# drift\n")
    with pytest.raises(EvolutionInterventionalGreenCohortError) as captured:
        await resumed_green.execute(
            parent_receipt_id="not-needed-for-stale-green-cohort",
            green_request=green,
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
            red_receipt=receipt,
            lease=lease,
        )
    assert captured.value.code == "candidate_file_digest_mismatch"
    with pytest.raises(EvolutionInterventionalGreenSampleError) as captured:
        await green_executor.execute(
            parent_receipt_id="not-needed-for-stale-green",
            sample_index=0,
            green_request=green,
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
            red_receipt=receipt,
            lease=lease,
        )
    assert captured.value.code == "candidate_file_digest_mismatch"


@pytest.mark.asyncio
async def test_interventional_red_rejects_profile_drift_before_authority_acquisition(
    tmp_path: Path,
) -> None:
    root, trust, plan, profile_binding, request, metrics = await _authority(tmp_path)
    profile_path = root / ".naumi" / "harness.yaml"
    profile_path.write_text(
        profile_path.read_text().replace(
            "timeout_seconds: 10",
            "timeout_seconds: 11",
        )
    )
    store = HarnessStore(tmp_path / "harness.db")
    runtime = tmp_path / "runtime"
    permissions = PermissionDecisionReceiptStore(runtime / "permissions.db")
    run_authority = RunDelegationGrantAuthority(
        store=RunDelegationGrantStore(runtime / "run-grants.db"),
        permission_store=permissions,
        harness_store=store,
        workspace_root=root,
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
    executor = EvolutionInterventionalRedSampleExecutor(
        workspace_root=root,
        store=store,
        permission_store=permissions,
        run_grant_authority=run_authority,
        profile_service=HarnessService(workspace_root=root, trust_store=trust),
        sandbox_runner=HarnessSandboxCheckRunner(
            workspace_root=root,
            sandbox_root=tmp_path / "sandboxes",
            artifact_root=tmp_path / "artifacts",
        ),
        shell_admission_composer=composer,
    )

    with pytest.raises(
        EvolutionInterventionalRedSampleError,
        match="Profile 信任已失效",
    ) as captured:
        await executor.execute(
            parent_receipt_id="must-not-read",
            sample_index=0,
            baseline_request=request,
            metric_binding=metrics,
            validation_plan=plan,
            profile_binding=profile_binding,
        )

    assert captured.value.code == "profile_trust_revalidation_failed"
    assert await store.get_run_lease(
        workspace_root=root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id="must-not-exist",
    ) is None


@pytest.mark.asyncio
async def test_interventional_red_cohort_rejects_tampering_before_lease(
    tmp_path: Path,
) -> None:
    root, _, plan, profile_binding, request, metrics = await _authority(tmp_path)
    store = HarnessStore(tmp_path / "harness.db")
    cohort = EvolutionInterventionalRedCohortExecutor(
        workspace_root=root,
        store=store,
        permission_store=None,  # type: ignore[arg-type]
        run_grant_authority=None,  # type: ignore[arg-type]
        sample_executor=None,  # type: ignore[arg-type]
    )
    tampered = metrics.model_copy(update={"baseline_request_sha256": "f" * 64})

    with pytest.raises(EvolutionInterventionalRedCohortError) as captured:
        await cohort.execute(
            parent_receipt_id="must-not-read",
            baseline_request=request,
            metric_binding=tampered,
            validation_plan=plan,
            profile_binding=profile_binding,
        )

    assert captured.value.code == "sample_authority_invalid"
    assert await store.get_run_lease(
        workspace_root=root,
        run_kind=HarnessRunKind.RUNTIME,
        run_id="must-not-exist",
    ) is None
