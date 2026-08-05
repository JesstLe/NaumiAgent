from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
from naumi_agent.evolution.revalidation_evaluation_sources import (
    EvolutionRevalidationEvaluationSourceStore,
)
from naumi_agent.evolution.revalidation_local_canary_runs import (
    EvolutionRevalidationLocalCanaryExecutor,
    EvolutionRevalidationLocalCanaryJournalStore,
    EvolutionRevalidationLocalCanaryState,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
)
from naumi_agent.evolution.revalidation_runtime_sources import (
    EvolutionRevalidationRuntimeSourceService,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlanView,
)
from naumi_agent.harness.run_lease import HarnessRunLeaseState
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckRunner, HarnessSandboxCheckStatus
from naumi_agent.harness.sandbox_eval import HarnessSandboxEvalExecutionKernel
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import PermissionMode
from tests.unit.test_evolution_revalidation_interventional_samples import _SandboxKernel
from tests.unit.test_evolution_revalidation_rollout_plans import _approved
from tests.unit.test_evolution_revalidation_rollout_stage_entries import _services

T0 = datetime.fromisoformat("2026-07-19T01:00:00+00:00")


class _FailTerminalOnceStore(EvolutionRevalidationLocalCanaryJournalStore):
    def __init__(self, db_path):
        super().__init__(db_path)
        self.failed = False

    async def transition(self, event):
        if event.terminal and not self.failed:
            self.failed = True
            raise RuntimeError("simulated process loss before terminal journal")
        return await super().transition(event)


async def _scenario(root: Path, *, journal_store=None, kernel=None, real_kernel=False):
    contract, approved, _principals, _principal_service, plan_service = await _approved(
        root
    )
    plan_view = await plan_service.issue(
        contract_id=contract.contract_id,
        decision_id=approved.receipt.decision_id,
    )
    control_service, entry_service = _services(
        root,
        plan_service,
        now=T0.isoformat(),
    )
    entry = await entry_service.issue(plan_id=plan_view.plan.plan_id)
    validation_plan = await (
        plan_service.promotion_input_service.validation_plan_store.get(
            contract.validation_plan_id
        )
    )
    assert validation_plan is not None

    class _PlanService:
        async def inspect(self, **_kwargs):
            return EvolutionRevalidationValidationPlanView(
                plan=validation_plan,
                source_current=True,
                current_status="ready",
                execution_eligible=True,
            )

    source_service = EvolutionRevalidationRuntimeSourceService(
        validation_plan_service=_PlanService(),
        source_store=EvolutionRevalidationEvaluationSourceStore(
            plan_service.store.db_path,
            storage_dir=root / ".naumi" / "evolution" / "evaluation-sources",
        ),
    )

    class _ProfileService:
        async def status(self):
            return SimpleNamespace(
                trusted=True,
                profile_digest=validation_plan.profile_sha256,
                snapshot=SimpleNamespace(
                    profile=SimpleNamespace(checks=validation_plan.checks),
                ),
            )

    permissions = PermissionDecisionReceiptStore(root / ".naumi" / "permissions.db")
    harness_store = HarnessStore(root / ".naumi" / "canary-harness.db")
    run_authority = RunDelegationGrantAuthority(
        store=RunDelegationGrantStore(root / ".naumi" / "canary-grants.db"),
        permission_store=permissions,
        harness_store=harness_store,
        workspace_root=root,
    )
    parent = await permissions.issue(
        request_id="local-canary-parent",
        session_id="local-canary-session",
        run_id="local-canary-parent-run",
        call_id="local-canary-parent",
        agent_name="main",
        tool_name="evolution_local_canary",
        tool_family="evolution",
        arguments={"entry_receipt_id": entry.receipt.receipt_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        delegated_tool_names=("bash_run",),
        decided_at=T0.isoformat(),
    )
    journal = journal_store or EvolutionRevalidationLocalCanaryJournalStore(
        plan_service.store.db_path
    )
    if real_kernel:
        composer = ShellWorkerAdmissionComposer(
            worker_registry=WorkerRegistryStore(root / ".naumi" / "canary-workers.db"),
            harness_store=harness_store,
            permission_store=permissions,
            execution_grant_store=ExecutionGrantStore(
                root / ".naumi" / "canary-execution-grants.db"
            ),
            tool_job_store=ToolJobStore(root / ".naumi" / "canary-tool-jobs.db"),
            transport=AuthenticatedLocalShellTransport(
                runtime_dir=root / ".naumi" / "canary-transport"
            ),
            software_version="test",
            run_delegation_grant_authority=run_authority,
            now=lambda: T0.isoformat(),
        )
        sandbox = HarnessSandboxEvalExecutionKernel(
            workspace_root=root,
            permission_store=permissions,
            run_grant_authority=run_authority,
            sandbox_runner=HarnessSandboxCheckRunner(
                workspace_root=root,
                sandbox_root=root / ".naumi" / "canary-sandboxes",
                artifact_root=root / ".naumi" / "canary-artifacts",
            ),
            shell_admission_composer=composer,
            now=lambda: T0.isoformat(),
        )
    else:
        sandbox = kernel or _SandboxKernel()
    executor = EvolutionRevalidationLocalCanaryExecutor(
        workspace_root=root,
        entry_service=entry_service,
        contract_service=plan_service.promotion_input_service.contract_service,
        source_service=source_service,
        profile_service=_ProfileService(),
        permission_store=permissions,
        run_grant_authority=run_authority,
        harness_store=harness_store,
        sandbox_eval_kernel=sandbox,
        journal_store=journal,
        now=lambda: T0.isoformat(),
        runner_id="local-canary-test-runner",
    )
    return (
        executor,
        entry,
        parent,
        sandbox,
        journal,
        run_authority,
        harness_store,
        control_service,
    )


def _require_real_backend() -> None:
    try:
        detect_shell_sandbox_backend()
    except ShellSandboxUnavailableError as exc:
        pytest.skip(str(exc))


def _install_real_verify_command(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("真实 local-canary worker 测试需要 C 编译器。")
    bin_dir = root / ".naumi" / "test-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    source = bin_dir / "verify.c"
    executable = bin_dir / "verify"
    source.write_text(
        "#include <stdio.h>\n"
        "#include <string.h>\n"
        "int main(int argc, char **argv) {\n"
        "  const char *allowed[] = {\"lint\", \"compile\", \"unit\", "
        "\"contract\", \"boundary\"};\n"
        "  int known = 0;\n"
        "  if (argc != 3) return 64;\n"
        "  for (unsigned i = 0; i < sizeof(allowed) / sizeof(allowed[0]); i++) "
        "known |= strcmp(argv[1], allowed[i]) == 0;\n"
        "  if (!known) return 65;\n"
        "  FILE *source = fopen(argv[2], \"rb\");\n"
        "  if (source == NULL) return 66;\n"
        "  int first = fgetc(source);\n"
        "  if (fclose(source) != 0) return 67;\n"
        "  return first == EOF ? 68 : 0;\n"
        "}\n",
        encoding="utf-8",
    )
    try:
        subprocess.run(
            (compiler, "-O2", "-std=c11", "-o", str(executable), str(source)),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"无法编译真实 verify fixture：{exc.stderr[-300:]}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.mark.asyncio
async def test_local_canary_executes_green_and_persists_terminal_evidence(
    tmp_path: Path,
) -> None:
    (
        executor,
        entry,
        parent,
        kernel,
        journal,
        run_authority,
        harness_store,
        _control,
    ) = await _scenario(tmp_path)

    view = await executor.execute(
        entry_receipt_id=entry.receipt.receipt_id,
        parent_receipt_id=parent.receipt_id,
    )

    assert kernel.calls == ["sandbox"]
    assert view.event.state is EvolutionRevalidationLocalCanaryState.PASSED, (
        view.event.error_code,
        view.event.error_message,
        view.event.checks,
    )
    assert view.run_complete and view.passed and view.monitor_authority
    assert not view.later_stage_authority
    assert view.event.sequence == 4
    assert view.event.source_snapshot_sha256
    assert view.event.source_tree_sha256
    assert view.event.run_grant_sha256
    assert view.event.checks
    assert all(item.lifecycle_receipt_sha256 for item in view.event.checks)
    assert await journal.latest_for_run(view.event.run_id) == view.event
    grant = await run_authority.validate(
        grant_id=view.event.run_grant_id,
        now=(T0 + timedelta(seconds=1)).isoformat(),
    )
    assert not grant.allowed
    lease = await harness_store.get_run_lease(
        workspace_root=tmp_path,
        run_kind="runtime",
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.RELEASED


@pytest.mark.asyncio
async def test_local_canary_runs_through_real_arc04_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_real_backend()
    _install_real_verify_command(tmp_path, monkeypatch)
    (
        executor,
        entry,
        parent,
        _kernel,
        _journal,
        _authority,
        _harness,
        _control,
    ) = await _scenario(tmp_path, real_kernel=True)

    view = await executor.execute(
        entry_receipt_id=entry.receipt.receipt_id,
        parent_receipt_id=parent.receipt_id,
    )

    assert view.event.state is EvolutionRevalidationLocalCanaryState.PASSED, (
        view.event.error_code,
        view.event.error_message,
        view.event.checks,
    )
    assert view.event.checks
    assert all(item.job_id for item in view.event.checks)
    assert all(item.lifecycle_receipt_sha256 for item in view.event.checks)
    assert all(item.snapshot_manifest_sha256 for item in view.event.checks)


@pytest.mark.asyncio
async def test_running_journal_recovers_after_terminal_write_loss(
    tmp_path: Path,
) -> None:
    journal = _FailTerminalOnceStore(tmp_path / ".naumi" / "state.db")
    (
        executor,
        entry,
        parent,
        kernel,
        _journal,
        _authority,
        _harness,
        _control,
    ) = await _scenario(tmp_path, journal_store=journal)

    with pytest.raises(RuntimeError, match="process loss"):
        await executor.execute(
            entry_receipt_id=entry.receipt.receipt_id,
            parent_receipt_id=parent.receipt_id,
        )
    running = await journal.latest_for_entry(entry.receipt.receipt_id)
    assert running is not None
    assert running.state is EvolutionRevalidationLocalCanaryState.RUNNING

    recovered = await executor.execute(
        entry_receipt_id=entry.receipt.receipt_id,
        parent_receipt_id=parent.receipt_id,
    )
    assert recovered.event.state is EvolutionRevalidationLocalCanaryState.PASSED
    assert recovered.event.sequence == 5
    assert kernel.calls == ["sandbox", "sandbox"]


@pytest.mark.asyncio
async def test_arc04_nonpassing_status_is_preserved_as_terminal_evidence(
    tmp_path: Path,
) -> None:
    class _InfrastructureKernel(_SandboxKernel):
        async def execute(self, **kwargs):
            results = await super().execute(**kwargs)
            return (
                replace(
                    results[0],
                    status=HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR,
                    exit_code=70,
                    message="隔离后端不可用。",
                ),
                *results[1:],
            )

    executor, entry, parent, *_rest = await _scenario(
        tmp_path,
        kernel=_InfrastructureKernel(),
    )

    view = await executor.execute(
        entry_receipt_id=entry.receipt.receipt_id,
        parent_receipt_id=parent.receipt_id,
    )

    assert view.event.state is EvolutionRevalidationLocalCanaryState.FAILED
    assert view.event.error_code == "local_canary_check_failed"
    assert view.event.checks[0].status is (
        HarnessSandboxCheckStatus.INFRASTRUCTURE_ERROR
    )
    assert view.event.checks[0].exit_code == 70


@pytest.mark.asyncio
async def test_kill_switch_after_worker_run_records_cancelled_terminal(
    tmp_path: Path,
) -> None:
    holder = {}

    class _PausingKernel(_SandboxKernel):
        async def execute(self, **kwargs):
            results = await super().execute(**kwargs)
            await holder["control"].pause(
                reason_code="monitor_guardrail_breach",
                actor=EvolutionRevalidationRolloutControlActor.MONITOR,
                changed_at=T0.isoformat(),
            )
            return results

    (
        executor,
        entry,
        parent,
        _kernel,
        _journal,
        _authority,
        _harness,
        control,
    ) = await _scenario(tmp_path, kernel=_PausingKernel())
    holder["control"] = control

    view = await executor.execute(
        entry_receipt_id=entry.receipt.receipt_id,
        parent_receipt_id=parent.receipt_id,
    )

    assert view.event.state is EvolutionRevalidationLocalCanaryState.CANCELLED
    assert view.event.error_code == "entry_fenced_after_run"
    assert view.event.checks
    assert view.monitor_authority
    assert not view.passed
    assert not view.later_stage_authority


@pytest.mark.asyncio
async def test_task_cancellation_is_journaled_and_releases_authority(
    tmp_path: Path,
) -> None:
    class _CancellingKernel(_SandboxKernel):
        async def execute(self, **kwargs):
            self.calls.append(kwargs["lane"])
            raise asyncio.CancelledError

    (
        executor,
        entry,
        parent,
        _kernel,
        journal,
        run_authority,
        harness_store,
        _control,
    ) = await _scenario(tmp_path, kernel=_CancellingKernel())

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(
            entry_receipt_id=entry.receipt.receipt_id,
            parent_receipt_id=parent.receipt_id,
        )

    event = await journal.latest_for_entry(entry.receipt.receipt_id)
    assert event is not None
    assert event.state is EvolutionRevalidationLocalCanaryState.CANCELLED
    assert event.error_code == "local_canary_cancelled"
    grant = await run_authority.validate(
        grant_id=event.run_grant_id,
        now=(T0 + timedelta(seconds=1)).isoformat(),
    )
    assert not grant.allowed
    lease = await harness_store.get_run_lease(
        workspace_root=tmp_path,
        run_kind="runtime",
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.RELEASED
