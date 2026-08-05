from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
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
from naumi_agent.evolution.revalidation_interventional_samples import (
    EvolutionRevalidationInterventionalSampleError,
    EvolutionRevalidationInterventionalSampleExecutor,
    EvolutionRevalidationInterventionalSampleStore,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractBuilder,
    EvolutionRevalidationRuntimeContractStore,
    EvolutionRevalidationRuntimeContractView,
)
from naumi_agent.evolution.revalidation_runtime_sources import (
    EvolutionRevalidationRuntimeSourceService,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlanBuilder,
    EvolutionRevalidationValidationPlanStore,
    EvolutionRevalidationValidationPlanView,
)
from naumi_agent.harness.evolution_revalidation import (
    build_harness_evolution_revalidation_plan,
)
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckResult,
    HarnessSandboxCheckRunner,
    HarnessSandboxCheckStatus,
)
from naumi_agent.harness.sandbox_eval import HarnessSandboxEvalExecutionKernel
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.safety.permissions import PermissionMode
from tests.unit.test_evolution_revalidation_runtime_sources import (
    _runtime_scenario,
)
from tests.unit.test_evolution_revalidation_validation_plans import (
    _builder_scenario,
    _rehash_source,
)


class _SandboxKernel:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_green_once = False

    async def execute(self, **kwargs):
        assert await kwargs["profile_is_current"]()
        assert await kwargs["source"].source_is_current()
        self.calls.append(kwargs["lane"])
        if kwargs["lane"] == "green" and self.fail_green_once:
            self.fail_green_once = False
            raise RuntimeError("simulated GREEN interruption")
        return tuple(
            HarnessSandboxCheckResult(
                check_id=check.id,
                run_id=f"run-{kwargs['lane']}-{check.id}",
                status=HarnessSandboxCheckStatus.PASSED,
                source_revision=kwargs["source"].revision,
                source_tree_sha256=kwargs["source"].revision_tree_sha256,
                snapshot_manifest_sha256="1" * 64,
                profile_digest=kwargs["profile_digest"],
                job_id=f"job-{kwargs['lane']}-{check.id}",
                lifecycle_receipt_sha256="2" * 64,
                output="ok",
                exit_code=0,
                duration_ms=1,
                artifact_path=None,
                message="检查通过。",
            )
            for check in kwargs["checks"]
        )


def _require_real_backend() -> None:
    try:
        detect_shell_sandbox_backend()
    except ShellSandboxUnavailableError as exc:
        pytest.skip(str(exc))


async def _executor_scenario(tmp_path: Path):
    plan, _source, _source_store, state, source_service = await _runtime_scenario(
        tmp_path,
        metric_name="self_review.broad_except.count",
        metric_direction="decrease",
        metric_target=0,
        metric_verifier="self_review_static",
        include_boundary=True,
    )
    contract = EvolutionRevalidationRuntimeContractBuilder().build(
        plan=plan,
        now=datetime.fromisoformat(plan.created_at),
    )
    assert contract.binding_status == "ready"
    state_db = tmp_path / ".naumi" / "state.db"
    await EvolutionRevalidationValidationPlanStore(state_db).record(plan)
    await EvolutionRevalidationRuntimeContractStore(state_db).record(contract)

    class _ContractService:
        async def inspect(self, **_kwargs):
            return EvolutionRevalidationRuntimeContractView(
                contract=contract,
                source_current=state.current,
                current_status="ready" if state.current else "stale",
                execution_eligible=state.current,
            )

    class _ProfileService:
        async def status(self):
            return SimpleNamespace(
                trusted=True,
                profile_digest=plan.profile_sha256,
                snapshot=SimpleNamespace(
                    profile=SimpleNamespace(checks=plan.checks),
                ),
            )

    harness_store = HarnessStore(tmp_path / ".naumi" / "harness.db")
    permission_store = PermissionDecisionReceiptStore(tmp_path / ".naumi" / "permissions.db")
    run_grant_authority = RunDelegationGrantAuthority(
        store=RunDelegationGrantStore(tmp_path / ".naumi" / "run-grants.db"),
        permission_store=permission_store,
        harness_store=harness_store,
        workspace_root=tmp_path,
    )
    parent = await permission_store.issue(
        request_id="fresh-sample-parent",
        session_id="fresh-sample-session",
        run_id="fresh-sample-run",
        call_id="fresh-sample-parent",
        agent_name="main",
        tool_name="evolution_revalidation_interventional_sample",
        tool_family="evolution",
        arguments={"contract_id": contract.contract_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        delegated_tool_names=("bash_run",),
        decided_at=datetime.now(UTC).isoformat(),
    )
    kernel = _SandboxKernel()
    executor = EvolutionRevalidationInterventionalSampleExecutor(
        workspace_root=tmp_path,
        harness_store=harness_store,
        receipt_store=EvolutionRevalidationInterventionalSampleStore(state_db),
        permission_store=permission_store,
        run_grant_authority=run_grant_authority,
        profile_service=_ProfileService(),
        sandbox_eval_kernel=kernel,
        contract_service=_ContractService(),
        source_service=source_service,
    )
    return executor, contract, parent, harness_store, kernel, state


@pytest.mark.asyncio
async def test_fresh_sample_executes_real_red_green_metrics_and_persists_pair(
    tmp_path: Path,
) -> None:
    executor, contract, parent, harness_store, kernel, _state = await _executor_scenario(tmp_path)

    receipt = await executor.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
        sample_index=0,
    )
    repeated = await executor.execute(
        contract_id=contract.contract_id,
        parent_receipt_id="not-needed-after-completion",
        sample_index=0,
    )

    assert repeated == receipt
    assert kernel.calls == ["red", "green"]
    assert receipt.pair_complete and not receipt.cohort_complete
    assert receipt.project_code_executed and receipt.metrics_executed
    assert not receipt.promotion_authority
    red = await harness_store.get_eval_result(tmp_path, receipt.red_batch_id, contract.suite_id, 0)
    green = await harness_store.get_eval_result(
        tmp_path, receipt.green_batch_id, contract.suite_id, 0
    )
    assert red is not None and green is not None
    assert red.result.baseline_identity.source.dirty is False
    assert green.result.baseline_identity.source.dirty is True
    red_metric = red.result.cases[-1].metric_observations[0]
    green_metric = green.result.cases[-1].metric_observations[0]
    assert red_metric.metric == green_metric.metric == "self_review.broad_except.count"
    assert red.result.baseline_identity.platform == green.result.baseline_identity.platform
    lease = await harness_store.get_run_lease(
        workspace_root=tmp_path,
        run_kind=HarnessRunKind.RUNTIME,
        run_id=parent.run_id,
    )
    assert lease is not None and lease.state is HarnessRunLeaseState.RELEASED
    with sqlite3.connect(tmp_path / ".naumi" / "run-grants.db") as db:
        assert db.execute("SELECT state, revoke_reason FROM run_delegation_grants").fetchall() == [
            ("revoked", "fresh_sample_finished")
        ]


@pytest.mark.asyncio
async def test_fresh_sample_fails_closed_on_stale_contract_or_missing_evidence(
    tmp_path: Path,
) -> None:
    executor, contract, parent, harness_store, _kernel, state = await _executor_scenario(tmp_path)
    state.current = False
    with pytest.raises(EvolutionRevalidationInterventionalSampleError) as stale:
        await executor.execute(
            contract_id=contract.contract_id,
            parent_receipt_id=parent.receipt_id,
            sample_index=0,
        )
    assert stale.value.code == "fresh_sample_runtime_contract_not_ready"

    state.current = True
    receipt = await executor.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
        sample_index=0,
    )
    async with harness_store._connection() as db:
        await db.execute("DELETE FROM harness_eval_results WHERE id = ?", (receipt.red_result_id,))
        await db.commit()
    with pytest.raises(EvolutionRevalidationInterventionalSampleError) as missing:
        await executor.execute(
            contract_id=contract.contract_id,
            parent_receipt_id="not-needed",
            sample_index=0,
        )
    assert missing.value.code == "fresh_sample_existing_evidence_missing"


@pytest.mark.asyncio
async def test_fresh_sample_resumes_red_prefix_with_distinct_phase_grants(
    tmp_path: Path,
) -> None:
    executor, contract, parent, _store, kernel, _state = await _executor_scenario(tmp_path)
    kernel.fail_green_once = True

    with pytest.raises(RuntimeError, match="simulated GREEN interruption"):
        await executor.execute(
            contract_id=contract.contract_id,
            parent_receipt_id=parent.receipt_id,
            sample_index=0,
        )
    receipt = await executor.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
        sample_index=0,
    )

    assert kernel.calls == ["red", "green", "green"]
    assert receipt.red_run_grant_sha256 != receipt.green_run_grant_sha256


@pytest.mark.asyncio
async def test_fresh_sample_runs_through_real_arc04_worker(tmp_path: Path) -> None:
    _require_real_backend()
    (
        package,
        fresh,
        original_source,
        experiment,
        _harness,
        target_files,
        now,
    ) = await _builder_scenario(
        tmp_path,
        metric_name="self_review.broad_except.count",
        metric_direction="decrease",
        metric_target=0,
        metric_verifier="self_review_static",
        include_boundary=True,
    )
    path = package.patch.files[0].path
    check = HarnessCheckSpec(
        id="fresh_real_check",
        argv=(
            "python3",
            "-c",
            f'import ast; from pathlib import Path; ast.parse(Path("{path}").read_text())',
        ),
        timeout_seconds=10,
        when_changed=(path,),
        required_for=("change",),
        provides=("lint", "compile", "unit", "contract"),
        adversarial_probes=("boundary",),
    )
    profile_raw = (
        "schema_version: 1\nchecks:\n"
        "  - id: fresh_real_check\n"
        f"    argv: {json.dumps(list(check.argv))}\n"
        "    timeout_seconds: 10\n"
        f"    when_changed: {json.dumps([path])}\n"
        "    required_for: [change]\n"
        "    provides: [lint, compile, unit, contract]\n"
        "    adversarial_probes: [boundary]\n"
    )
    (tmp_path / ".naumi" / "harness.yaml").write_text(profile_raw)
    profile_sha256 = hashlib.sha256(profile_raw.encode()).hexdigest()
    harness = build_harness_evolution_revalidation_plan(
        profile_sha256=profile_sha256,
        changed_paths=(path,),
        checks=(check,),
    )
    source = _rehash_source(
        original_source,
        profile_sha256=profile_sha256,
        plan_sha256=harness.plan_sha256,
    )
    plan = EvolutionRevalidationValidationPlanBuilder().build(
        workspace_root=tmp_path,
        fresh_plan=fresh,
        source=source,
        package=package,
        contract=experiment,
        suite_id="core_revalidation",
        requested_samples=7,
        harness_plan=harness,
        target_files=target_files,
        now=now,
    )
    contract = EvolutionRevalidationRuntimeContractBuilder().build(plan=plan, now=now)
    assert contract.binding_status == "ready"
    state_db = tmp_path / ".naumi" / "state.db"
    async with aiosqlite.connect(state_db) as db:
        await db.execute(
            "UPDATE evolution_revalidation_evaluation_sources SET "
            "snapshot_id = ?, snapshot_sha256 = ?, snapshot_json = ?",
            (source.snapshot_id, source.snapshot_sha256, source.model_dump_json()),
        )
        await db.commit()
    await EvolutionRevalidationValidationPlanStore(state_db).record(plan)
    await EvolutionRevalidationRuntimeContractStore(state_db).record(contract)

    class _PlanService:
        async def inspect(self, **_kwargs):
            return EvolutionRevalidationValidationPlanView(
                plan=plan,
                source_current=True,
                current_status="ready",
                execution_eligible=True,
            )

    class _ContractService:
        async def inspect(self, **_kwargs):
            return EvolutionRevalidationRuntimeContractView(
                contract=contract,
                source_current=True,
                current_status="ready",
                execution_eligible=True,
            )

    source_service = EvolutionRevalidationRuntimeSourceService(
        validation_plan_service=_PlanService(),
        source_store=EvolutionRevalidationEvaluationSourceStore(
            state_db,
            storage_dir=tmp_path / ".naumi" / "evolution" / "evaluation-sources",
        ),
    )
    trust = HarnessTrustStore(tmp_path / ".naumi" / "trust.db")
    await trust.trust(tmp_path, profile_sha256, source="test")
    harness_store = HarnessStore(tmp_path / ".naumi" / "harness.db")
    permissions = PermissionDecisionReceiptStore(tmp_path / ".naumi" / "permissions.db")
    run_authority = RunDelegationGrantAuthority(
        store=RunDelegationGrantStore(tmp_path / ".naumi" / "run-grants.db"),
        permission_store=permissions,
        harness_store=harness_store,
        workspace_root=tmp_path,
    )
    parent = await permissions.issue(
        request_id="fresh-real-parent",
        session_id="fresh-real-session",
        run_id="fresh-real-run",
        call_id="fresh-real-parent",
        agent_name="main",
        tool_name="evolution_revalidation_interventional_sample",
        tool_family="evolution",
        arguments={"contract_id": contract.contract_id},
        outcome=PermissionDecisionOutcome.POLICY_ALLOWED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.POLICY,
        permission_mode=PermissionMode.MODERATE,
        risk_level="high",
        delegated_tool_names=("bash_run",),
        decided_at=datetime.now(UTC).isoformat(),
    )
    composer = ShellWorkerAdmissionComposer(
        worker_registry=WorkerRegistryStore(tmp_path / ".naumi" / "workers.db"),
        harness_store=harness_store,
        permission_store=permissions,
        execution_grant_store=ExecutionGrantStore(tmp_path / ".naumi" / "execution-grants.db"),
        tool_job_store=ToolJobStore(tmp_path / ".naumi" / "tool-jobs.db"),
        transport=AuthenticatedLocalShellTransport(runtime_dir=tmp_path / ".naumi" / "transport"),
        software_version="test",
        run_delegation_grant_authority=run_authority,
    )
    sandbox_runner = HarnessSandboxCheckRunner(
        workspace_root=tmp_path,
        sandbox_root=tmp_path / ".naumi" / "sandboxes",
        artifact_root=tmp_path / ".naumi" / "artifacts",
    )
    executor = EvolutionRevalidationInterventionalSampleExecutor(
        workspace_root=tmp_path,
        harness_store=harness_store,
        receipt_store=EvolutionRevalidationInterventionalSampleStore(state_db),
        permission_store=permissions,
        run_grant_authority=run_authority,
        profile_service=HarnessService(workspace_root=tmp_path, trust_store=trust),
        sandbox_eval_kernel=HarnessSandboxEvalExecutionKernel(
            workspace_root=tmp_path,
            permission_store=permissions,
            run_grant_authority=run_authority,
            sandbox_runner=sandbox_runner,
            shell_admission_composer=composer,
            now=lambda: datetime.now(UTC).isoformat(),
        ),
        contract_service=_ContractService(),
        source_service=source_service,
    )

    receipt = await executor.execute(
        contract_id=contract.contract_id,
        parent_receipt_id=parent.receipt_id,
        sample_index=0,
    )

    assert receipt.arc04_worker_used and receipt.project_code_executed
    assert receipt.red_check_statuses == receipt.green_check_statuses == ("passed",)
    assert receipt.red_lifecycle_receipt_sha256[0] != "2" * 64
