from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.post_rollback_behavioral_coverage import (
    EvolutionPostRollbackBehavioralCoverageError,
    EvolutionPostRollbackBehavioralCoverageService,
    EvolutionPostRollbackBehavioralCoverageStore,
    render_post_rollback_behavioral_coverage,
)
from naumi_agent.evolution.post_rollback_behavioral_lanes import (
    EvolutionPostRollbackBehavioralLaneStore,
)
from naumi_agent.evolution.proposal_before_after_evidence import (
    EvolutionProposalBeforeAfterCohort,
    EvolutionProposalBeforeAfterLane,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionPostRollbackBehavioralCoverageTool,
)


class _OutcomeService:
    def __init__(self, view) -> None:
        self.view = view

    async def inspect(self, *, request_id: str):
        assert request_id == self.view.outcome.request_id
        return self.view


class _VerificationStore:
    def __init__(self, item) -> None:
        self.item = item

    async def get_by_outcome(self, outcome_id: str):
        assert outcome_id == self.item.outcome_id
        return self.item


class _VerificationService:
    def __init__(self, view) -> None:
        self.view = view
        self.store = _VerificationStore(view.verification)

    async def record(self, *, request_id: str):
        assert request_id == self.view.verification.request_id
        return self.view

    async def inspect(self, *, verification):
        assert verification == self.view.verification
        return self.view


class _EvidenceStore:
    def __init__(self, item) -> None:
        self.item = item

    async def get_by_outcome(self, outcome_id: str):
        assert outcome_id == self.item.outcome_id
        return self.item


class _BeforeAfterService:
    def __init__(self, view) -> None:
        self.view = view
        self.evidence_store = _EvidenceStore(view.evidence)

    async def record(self, *, request_id: str):
        assert request_id == self.view.evidence.request_id
        return self.view

    async def inspect(self, *, evidence):
        assert evidence == self.view.evidence
        return self.view


class _LaneService:
    async def inspect(self, *, lane):  # pragma: no cover - no lane in base fixture
        raise AssertionError(f"unexpected lane: {lane}")


def _cohort(batch_id: str, digest: str) -> EvolutionProposalBeforeAfterCohort:
    return EvolutionProposalBeforeAfterCohort(
        batch_id=batch_id,
        identity_sha256=digest,
        samples=5,
        samples_sha256=digest,
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
    before = _cohort(f"baseline-{marker}", marker * 64)
    after = _cohort(f"candidate-{marker}", marker * 64)
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
        before=before,
        after=after,
    )


def _fixture(tmp_path: Path, *, unknown: bool = False):
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    outcome = SimpleNamespace(
        workspace_root=str(workspace),
        outcome_id="evrerollbackout_" + "1" * 24,
        outcome_sha256="2" * 64,
        request_id="evrerollbackreq_" + "3" * 24,
        workbench_session_id="session-coverage",
        workbench_proposal_id="proposal-coverage",
    )
    verification = SimpleNamespace(
        verification_id="evpostrollback_" + "4" * 24,
        verification_sha256="5" * 64,
        outcome_id=outcome.outcome_id,
        outcome_sha256=outcome.outcome_sha256,
        request_id=outcome.request_id,
        workbench_proposal_id=outcome.workbench_proposal_id,
        baseline_slot_id="relslot_" + "6" * 24,
        baseline_slot_sha256="7" * 64,
        baseline_manifest_sha256="8" * 64,
        baseline_version="1.2.3",
        baseline_target="darwin-arm64",
        baseline_source_commit="9" * 40,
        baseline_source_tree_sha256="a" * 64,
    )
    evidence = SimpleNamespace(
        evidence_id="evbeforeafter_" + "b" * 24,
        evidence_sha256="c" * 64,
        outcome_id=outcome.outcome_id,
        outcome_sha256=outcome.outcome_sha256,
        request_id=outcome.request_id,
        workbench_proposal_id=outcome.workbench_proposal_id,
        final_evaluation_id="evfinal_" + "d" * 24,
        final_evaluation_sha256="e" * 64,
        lanes=(
            _lane(1, "interventional", "unknown" if unknown else "macos", "1"),
            _lane(2, "adversarial", "windows", "2"),
        ),
    )
    outcome_view = SimpleNamespace(
        outcome=outcome,
        outcome_authority=True,
        active_baseline_authority=True,
    )
    verification_view = SimpleNamespace(
        verification=verification,
        verification_authority=True,
        active_baseline_authority=True,
    )
    evidence_view = SimpleNamespace(evidence=evidence, before_after_authority=True)
    db_path = tmp_path / "evolution.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE evolution_proposal_before_after_evidence ("
            "evidence_id TEXT PRIMARY KEY, evidence_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_proposal_before_after_evidence VALUES (?, ?)",
            (evidence.evidence_id, evidence.evidence_sha256),
        )
        db.commit()
    store = EvolutionPostRollbackBehavioralCoverageStore(db_path)
    service = EvolutionPostRollbackBehavioralCoverageService(
        workspace_root=workspace,
        outcome_service=_OutcomeService(outcome_view),  # type: ignore[arg-type]
        runtime_verification_service=_VerificationService(  # type: ignore[arg-type]
            verification_view
        ),
        before_after_service=_BeforeAfterService(evidence_view),  # type: ignore[arg-type]
        lane_store=EvolutionPostRollbackBehavioralLaneStore(db_path),
        lane_service=_LaneService(),  # type: ignore[arg-type]
        store=store,
    )
    return service, store, outcome, evidence_view


@pytest.mark.asyncio
async def test_coverage_contract_freezes_local_and_remote_lane_requirements(
    tmp_path: Path,
) -> None:
    service, store, outcome, evidence_view = _fixture(tmp_path)

    first = await service.record(request_id=outcome.request_id)
    repeated = await service.record(request_id=outcome.request_id)
    restored = await store.get_by_outcome(outcome.outcome_id)

    assert repeated == first
    assert restored == first.contract
    assert first.contract.baseline_platform == "macos"
    assert first.contract.required_platforms == ("windows",)
    assert first.contract.local_lane_count == 1
    assert first.contract.remote_lane_count == 1
    assert first.recorded_lane_count == 0
    assert first.missing_lane_count == 2
    assert first.remote_dispatch_count == 1
    assert not first.matrix_ready
    assert first.lanes[0].expected.execution_scope == "local_installed_baseline"
    assert not first.lanes[0].dispatch_required
    assert first.lanes[1].expected.execution_scope == "remote_target_required"
    assert first.lanes[1].dispatch_required
    assert not first.contract.execution_authority
    assert not first.contract.behavioral_evaluation_recorded
    rendered = render_post_rollback_behavioral_coverage(first)
    assert "Lane：0/2 已记录" in rendered
    assert "需要目标主机调度：1" in rendered

    tool = EvolutionPostRollbackBehavioralCoverageTool(
        SimpleNamespace(evolution_post_rollback_behavioral_coverage_service=service)
    )
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(
            tool.name,
            {"request_id": outcome.request_id},
            tool=tool,
        )
        assert decision.allowed
        assert not decision.requires_confirmation
    assert await tool.execute(outcome.request_id) == rendered
    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            arguments = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**arguments),
            )

    slash = await execute_slash_command(
        _SlashEngine(),
        f"/evolution outcome-behavior-coverage {outcome.request_id}",
    )
    assert first.contract.contract_id in slash

    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_behavioral_coverage "
            "SET contract_json = ? WHERE contract_id = ?",
            ("{}", first.contract.contract_id),
        )
        db.commit()
    tampered = await service.inspect(contract=first.contract)
    assert not tampered.durable_dependencies_valid
    assert not tampered.matrix_ready
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_behavioral_coverage "
            "SET contract_json = ? WHERE contract_id = ?",
            (first.contract.model_dump_json(), first.contract.contract_id),
        )
        db.commit()

    evidence_view.before_after_authority = False
    stale = await service.inspect(contract=first.contract)
    assert not stale.before_after_authority
    assert not stale.matrix_ready


@pytest.mark.asyncio
async def test_coverage_contract_rejects_unknown_platform(tmp_path: Path) -> None:
    service, _, outcome, _ = _fixture(tmp_path, unknown=True)

    with pytest.raises(EvolutionPostRollbackBehavioralCoverageError) as exc_info:
        await service.record(request_id=outcome.request_id)

    assert exc_info.value.code == "post_rollback_coverage_platform_unknown"
