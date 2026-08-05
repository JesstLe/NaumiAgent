from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.daemons.permission_context import current_permission_receipt
from naumi_agent.daemons.permission_decisions import permission_arguments_sha256
from naumi_agent.evolution.revalidation_rebases import (
    EvolutionRevalidationRebaseExecutor,
    EvolutionRevalidationRebaseStore,
)
from naumi_agent.evolution.revalidation_replays import (
    EvolutionRevalidationReplayExecutor,
    EvolutionRevalidationReplayStore,
)
from naumi_agent.evolution.revalidation_validations import (
    EvolutionRevalidationValidationError,
    EvolutionRevalidationValidationService,
    EvolutionRevalidationValidationStatus,
    EvolutionRevalidationValidationStore,
)
from naumi_agent.harness.evolution_revalidation import (
    HarnessEvolutionRevalidationRun,
    HarnessEvolutionRevalidationRunError,
    HarnessEvolutionRevalidationSource,
    build_harness_evolution_revalidation_plan,
)
from naumi_agent.harness.models import HarnessCheckSpec, HarnessProfile
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckResult,
    HarnessSandboxCheckStatus,
    HarnessSandboxSourceOverlay,
)
from naumi_agent.harness.service import HarnessService, HarnessStatusCode
from naumi_agent.memory.session import Session
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import (
    TOOL_PERMISSIONS,
    PermissionMode,
    PermissionRiskLevel,
)
from naumi_agent.tools.evolution_review import EvolutionRevalidationValidationTool
from tests.unit.test_evolution_promotion_packages import _git
from tests.unit.test_evolution_revalidation_rebases import _advance_view
from tests.unit.test_evolution_revalidation_replays import _fixture


class _StaticExecutionService:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    async def execute(self, **_kwargs):
        return self.outcome


class _StaticRequestService:
    def __init__(self, view) -> None:
        self.view = view

    async def inspect(self, **_kwargs):
        return self.view


class _StaticStore:
    def __init__(self, value) -> None:
        self.value = value

    async def get(self, _key):
        return self.value


class _FakeHarness:
    def __init__(self, *, status: HarnessSandboxCheckStatus) -> None:
        self.status = status
        self.calls = 0
        self.sources = []
        self.check = HarnessCheckSpec(
            id="targeted_unit",
            argv=("uv", "run", "pytest", "-q", "tests/unit/test_targeted.py"),
            timeout_seconds=30,
            when_changed=("src/**/*.py",),
            required_for=("change",),
            provides=("unit",),
        )

    async def prepare_evolution_revalidation(self, *, changed_paths):
        return build_harness_evolution_revalidation_plan(
            profile_sha256="9" * 64,
            changed_paths=changed_paths,
            checks=(self.check,),
        )

    async def run_evolution_revalidation(
        self,
        *,
        source,
        source_is_current,
        **_kwargs,
    ):
        assert await source_is_current()
        self.calls += 1
        self.sources.append(source)
        result = HarnessSandboxCheckResult(
            check_id=self.check.id,
            run_id="run-revalidation",
            status=self.status,
            source_revision=source.revision,
            source_tree_sha256=source.overlay_source_sha256,
            snapshot_manifest_sha256="7" * 64,
            profile_digest="9" * 64,
            job_id="job-revalidation",
            lifecycle_receipt_sha256="8" * 64,
            output="focused check output",
            exit_code=0 if self.status is HarnessSandboxCheckStatus.PASSED else 1,
            duration_ms=25,
            artifact_path=None,
            message="done",
        )
        return HarnessEvolutionRevalidationRun(
            run_id="run-revalidation",
            results=(result,),
        )


class _PartialFailureHarness(_FakeHarness):
    async def run_evolution_revalidation(self, **kwargs):
        run = await super().run_evolution_revalidation(**kwargs)
        raise HarnessEvolutionRevalidationRunError(
            "second_check_infrastructure_failed",
            "second check failed",
            run_id=run.run_id,
            partial_results=run.results,
        )


class _FakeComposedJob:
    def __init__(self) -> None:
        self.admitted = object()
        self.released = False

    async def release(self) -> None:
        self.released = True


class _FakeComposer:
    def __init__(self) -> None:
        self.jobs = []

    async def compose(self, **_kwargs):
        job = _FakeComposedJob()
        self.jobs.append(job)
        return job


class _FakeSandboxRunner:
    async def run(self, **kwargs):
        await kwargs["admit_job"](object())
        source_sha = kwargs["overlay_source_sha256"]
        return HarnessSandboxCheckResult(
            check_id=kwargs["check"].id,
            run_id=kwargs["run_id"],
            status=HarnessSandboxCheckStatus.PASSED,
            source_revision=kwargs["source_revision"],
            source_tree_sha256=source_sha,
            snapshot_manifest_sha256="7" * 64,
            profile_digest=kwargs["profile_digest"],
            job_id="job-adapter",
            lifecycle_receipt_sha256="8" * 64,
            output="adapter output",
            exit_code=0,
            duration_ms=10,
            artifact_path=None,
            message="passed",
        )


def _service(
    root: Path,
    *,
    outcome,
    view,
    package,
    lease,
    harness,
    now: datetime,
) -> EvolutionRevalidationValidationService:
    return EvolutionRevalidationValidationService(
        execution_service=_StaticExecutionService(outcome),
        request_service=_StaticRequestService(view),
        package_input_store=_StaticStore(
            SimpleNamespace(
                promotion_review_eligible=True,
                package_input=package,
            )
        ),
        lease_store=_StaticStore(lease),
        harness=harness,
        store=EvolutionRevalidationValidationStore(root / ".naumi" / "state.db"),
        clock=lambda: now,
    )


@pytest.mark.asyncio
async def test_exact_replay_runs_harness_overlays_and_persists_new_evidence(
    tmp_path: Path,
) -> None:
    now, storage, package, lease, view, path, before, after = _fixture(tmp_path)
    replay = await EvolutionRevalidationReplayExecutor(
        store=EvolutionRevalidationReplayStore(tmp_path / ".naumi" / "state.db"),
        worktree_storage_dir=storage,
        clock=lambda: now,
    ).execute(request_view=view, package_input=package, lease=lease)
    harness = _FakeHarness(status=HarnessSandboxCheckStatus.PASSED)
    service = _service(
        tmp_path,
        outcome=replay,
        view=view,
        package=package,
        lease=lease,
        harness=harness,
        now=now,
    )

    receipt = await service.execute(
        workspace_root=tmp_path,
        request_id=view.request.request_id,
    )
    repeated = await service.execute(
        workspace_root=tmp_path,
        request_id=view.request.request_id,
    )

    assert receipt == repeated
    assert receipt.status is EvolutionRevalidationValidationStatus.PASSED
    assert receipt.execution_kind == "exact"
    assert receipt.project_code_executed
    assert receipt.new_validation_evidence_issued
    assert receipt.all_checks_passed
    assert not receipt.old_evidence_invalidated
    assert not receipt.promotion_authority
    assert harness.calls == 1
    assert harness.sources[0].overlays[0].path == path
    assert harness.sources[0].overlays[0].content == after
    assert (tmp_path / path).read_bytes() == before
    assert (Path(lease.worktree_path) / path).read_bytes() == after

    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_revalidation_validation_service=_StaticExecutionService(receipt),
    )
    tool = EvolutionRevalidationValidationTool(engine)
    tool_output = await tool.execute(view.request.request_id)
    slash_output = await execute_slash_command(
        engine,
        f"/evolution revalidation-validate {view.request.request_id}",
    )
    assert receipt.receipt_id in tool_output
    assert receipt.receipt_id in slash_output
    assert tool.metadata.delegated_tool_names == ("bash_run",)
    rule = TOOL_PERMISSIONS[tool.name]
    assert not rule.requires_confirmation
    assert rule.risk_level is PermissionRiskLevel.MEDIUM


@pytest.mark.asyncio
async def test_rebase_result_is_reconstructed_before_harness_execution(
    tmp_path: Path,
) -> None:
    now, storage, package, lease, view, path, _before, after = _fixture(tmp_path)
    target_only = tmp_path / "target-only.txt"
    target_only.write_text("advanced\n", encoding="utf-8")
    _git(tmp_path, "add", "target-only.txt")
    _git(tmp_path, "commit", "-m", "advance target independently")
    advanced = _advance_view(tmp_path, view)
    rebase = await EvolutionRevalidationRebaseExecutor(
        store=EvolutionRevalidationRebaseStore(tmp_path / ".naumi" / "state.db"),
        worktree_storage_dir=storage,
        clock=lambda: now,
    ).execute(request_view=advanced, package_input=package, lease=lease)
    harness = _FakeHarness(status=HarnessSandboxCheckStatus.PASSED)
    service = _service(
        tmp_path,
        outcome=rebase,
        view=advanced,
        package=package,
        lease=lease,
        harness=harness,
        now=now,
    )

    receipt = await service.execute(
        workspace_root=tmp_path,
        request_id=view.request.request_id,
    )

    merged = harness.sources[0].overlays[0].content
    assert receipt.execution_kind == "rebase"
    assert receipt.status is EvolutionRevalidationValidationStatus.PASSED
    assert hashlib.sha256(merged).hexdigest() == rebase.files[0].result_sha256
    assert b"FEATURE = 'replayed'" in merged
    assert (tmp_path / path).read_bytes() != after
    assert target_only.read_text(encoding="utf-8") == "advanced\n"
    assert (Path(lease.worktree_path) / path).read_bytes() == after


@pytest.mark.asyncio
async def test_failed_real_check_is_new_evidence_but_not_promotion_authority(
    tmp_path: Path,
) -> None:
    now, storage, package, lease, view, _path, _before, _after = _fixture(tmp_path)
    replay = await EvolutionRevalidationReplayExecutor(
        store=EvolutionRevalidationReplayStore(tmp_path / ".naumi" / "state.db"),
        worktree_storage_dir=storage,
        clock=lambda: now,
    ).execute(request_view=view, package_input=package, lease=lease)
    harness = _FakeHarness(status=HarnessSandboxCheckStatus.FAILED)
    service = _service(
        tmp_path,
        outcome=replay,
        view=view,
        package=package,
        lease=lease,
        harness=harness,
        now=now,
    )

    receipt = await service.execute(
        workspace_root=tmp_path,
        request_id=view.request.request_id,
    )

    assert receipt.status is EvolutionRevalidationValidationStatus.FAILED
    assert receipt.failure_code == "checks_failed"
    assert receipt.project_code_executed
    assert receipt.new_validation_evidence_issued
    assert not receipt.all_checks_passed
    assert not receipt.promotion_authority


@pytest.mark.asyncio
async def test_harness_adapter_requires_exact_delegated_permission_and_records_result(
    tmp_path: Path,
) -> None:
    check = HarnessCheckSpec(
        id="targeted_unit",
        argv=("uv", "run", "pytest", "-q", "tests/unit/test_targeted.py"),
        timeout_seconds=30,
        when_changed=("src/**/*.py",),
        required_for=("change",),
        provides=("unit",),
    )
    profile = HarnessProfile(schema_version=1, checks=(check,))
    status = SimpleNamespace(
        code=HarnessStatusCode.TRUSTED,
        trusted=True,
        snapshot=SimpleNamespace(profile=profile),
        profile_digest="9" * 64,
    )
    parent = SimpleNamespace(
        authorizes_execution=True,
        tool_name="evolution_revalidation_validate",
        run_id="run-adapter",
        delegated_tool_names=("bash_run",),
        arguments_sha256=permission_arguments_sha256(
            {"request_id": "evrevalidation_" + "1" * 24}
        ),
        receipt_id="permission-adapter",
    )
    composer = _FakeComposer()
    service = object.__new__(HarnessService)
    service._sandbox_check_runner = _FakeSandboxRunner()
    service._shell_admission_composer = composer
    service._authorization_receipt_provider = lambda: parent

    async def current_status():
        return status

    recorded = []

    async def record(result):
        recorded.append(result)

    async def persist(result, *, argv):
        recorded.append((result, argv))

    service.status = current_status
    service._record_check_result = record
    service._persist_check_result = persist
    plan = await service.prepare_evolution_revalidation(
        changed_paths=("src/naumi_agent/example.py",)
    )
    overlay = HarnessSandboxSourceOverlay(
        path="src/naumi_agent/example.py",
        content=b"VALUE = 2\n",
        sha256=hashlib.sha256(b"VALUE = 2\n").hexdigest(),
    )
    source = HarnessEvolutionRevalidationSource(
        revision="a" * 40,
        revision_tree_sha256="6" * 64,
        overlays=(overlay,),
        overlay_source_sha256="5" * 64,
    )

    async def source_is_current():
        return True

    run = await service.run_evolution_revalidation(
        request_id="evrevalidation_" + "1" * 24,
        authority_sha256="4" * 64,
        plan=plan,
        source=source,
        source_is_current=source_is_current,
    )

    assert run.run_id == "run-adapter"
    assert run.results[0].job_id == "job-adapter"
    assert len(recorded) == 2
    assert composer.jobs[0].released


@pytest.mark.asyncio
async def test_partial_worker_evidence_is_preserved_when_later_check_crashes(
    tmp_path: Path,
) -> None:
    now, storage, package, lease, view, _path, _before, _after = _fixture(tmp_path)
    replay = await EvolutionRevalidationReplayExecutor(
        store=EvolutionRevalidationReplayStore(tmp_path / ".naumi" / "state.db"),
        worktree_storage_dir=storage,
        clock=lambda: now,
    ).execute(request_view=view, package_input=package, lease=lease)
    harness = _PartialFailureHarness(status=HarnessSandboxCheckStatus.PASSED)
    service = _service(
        tmp_path,
        outcome=replay,
        view=view,
        package=package,
        lease=lease,
        harness=harness,
        now=now,
    )

    receipt = await service.execute(
        workspace_root=tmp_path,
        request_id=view.request.request_id,
    )

    assert receipt.status is EvolutionRevalidationValidationStatus.FAILED
    assert receipt.failure_code == "second_check_infrastructure_failed"
    assert len(receipt.checks) == 1
    assert receipt.project_code_executed
    assert receipt.new_validation_evidence_issued
    assert not receipt.all_checks_passed


@pytest.mark.asyncio
async def test_slash_path_persists_bypass_parent_and_bash_delegation(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(session_db_path=str(tmp_path / "state.db")),
        )
    )
    engine._session = Session(title="slash revalidation")
    engine._permission_port.set_mode(PermissionMode.BYPASS)
    captured = []

    class _CapturingService:
        async def execute(self, **_kwargs):
            captured.append(current_permission_receipt())
            return "validated"

    engine.evolution_revalidation_validation_service = _CapturingService()
    try:
        result = await engine.run_evolution_revalidation_validation_slash(
            "evrevalidation_" + "1" * 24
        )
    finally:
        await engine.shutdown()

    assert result == "validated"
    assert captured[0] is not None
    assert captured[0].tool_name == "evolution_revalidation_validate"
    assert captured[0].delegated_tool_names == ("bash_run",)
    assert captured[0].permission_mode is PermissionMode.BYPASS


@pytest.mark.asyncio
async def test_validation_claim_blocks_concurrency_and_advances_expired_epoch(
    tmp_path: Path,
) -> None:
    store = EvolutionRevalidationValidationStore(tmp_path / "state.db")
    now = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    request_id = "evrevalidation_" + "1" * 24
    first, terminal = await store.claim(
        authority_sha256="2" * 64,
        request_id=request_id,
        execution_sha256="3" * 64,
        profile_sha256="4" * 64,
        now=now,
    )
    assert first is not None and first.epoch == 1
    assert terminal is None
    with pytest.raises(EvolutionRevalidationValidationError) as busy:
        await store.claim(
            authority_sha256="2" * 64,
            request_id=request_id,
            execution_sha256="3" * 64,
            profile_sha256="4" * 64,
            now=now + timedelta(seconds=1),
        )
    assert busy.value.code == "revalidation_validation_claim_busy"

    recovered, terminal = await store.claim(
        authority_sha256="2" * 64,
        request_id=request_id,
        execution_sha256="3" * 64,
        profile_sha256="4" * 64,
        now=now + timedelta(seconds=301),
    )
    assert recovered is not None and recovered.epoch == 2
    assert terminal is None
