from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.evolution.experiments import (
    EvolutionExperimentContract,
    EvolutionExperimentContractIssuer,
    ExperimentBudget,
    ExperimentScope,
    GitExperimentBaselineReader,
)
from naumi_agent.evolution.queue import EvolutionProposalQueueAdapter
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import FeedbackIntakeService, build_direct_user_feedback
from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.proposal_governance import ProposalAction
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore

NOW = datetime(2026, 7, 18, 22, 0, tzinfo=UTC)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip()


async def _approved_fixture(
    tmp_path: Path,
    *,
    approve: bool = True,
    scope: str = "src/naumi_agent/ui/footer.py:render_footer",
):
    workspace = tmp_path / "workspace"
    target = workspace / "src" / "naumi_agent" / "ui" / "footer.py"
    target.parent.mkdir(parents=True)
    target.write_text("def render_footer():\n    return 'ready'\n", encoding="utf-8")
    target.with_name("header.py").write_text(
        "def render_header():\n    return 'ready'\n",
        encoding="utf-8",
    )
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "Naumi Test")
    _git(workspace, "config", "user.email", "naumi@example.invalid")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "baseline")

    evolution_store = EvolutionCandidateStore(tmp_path / "evolution.db")
    intake = FeedbackIntakeService(evolution_store)
    intake_result = None
    for offset in range(2):
        intake_result = await intake.ingest(
            workspace,
            build_direct_user_feedback(
                session_id="experiment-contract",
                category="defect",
                scope=scope,
                topic="footer_truncation",
                summary=f"底栏截断 {offset}",
                now=NOW + timedelta(minutes=offset),
            ),
        )
    assert intake_result is not None

    runtime_db = str(tmp_path / "runtime.db")
    service = WorkbenchService(
        task_store=TaskStore(runtime_db),
        workbench_store=WorkbenchStore(runtime_db),
        workspace_root=str(workspace),
    )
    mission = await service.create_mission(
        session_id="session-1",
        title="隔离实验",
        goal="先签发不可执行契约",
    )
    issue = await service.create_issue(
        session_id="session-1",
        mission_id=mission.id,
        title="审阅 Footer Proposal",
    )
    review_service = EvolutionReviewService(evolution_store)
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
    if approve:
        governed = await service.govern_proposal(
            "session-1",
            queued.proposal["id"],
            action=ProposalAction.APPROVE,
            reviewer="Human",
            decision_note="允许进入契约阶段",
            now=NOW + timedelta(minutes=5),
        )
        assert governed is not None
    issuer = EvolutionExperimentContractIssuer(
        review_service=review_service,
        workbench_service=service,
    )
    return workspace, evolution_store, service, issuer, queued.proposal["id"]


@pytest.mark.asyncio
async def test_approved_proposal_issues_stable_non_executable_contract(
    tmp_path: Path,
) -> None:
    workspace, _store, _service, issuer, proposal_id = await _approved_fixture(tmp_path)
    baseline = _git(workspace, "rev-parse", "HEAD")
    before = (workspace / "src/naumi_agent/ui/footer.py").read_bytes()

    first = await issuer.issue(
        workspace,
        session_id="session-1",
        proposal_id=proposal_id,
        seed=42,
    )
    second = await issuer.issue(
        workspace,
        session_id="session-1",
        proposal_id=proposal_id,
        seed=42,
    )

    assert first == second
    assert first.contract_id.startswith("evx_")
    assert len(first.manifest_sha256) == 64
    assert first.contract_id == f"evx_{first.manifest_sha256[:24]}"
    assert first.baseline.commit == baseline
    assert first.baseline.workspace_dirty_at_issue is False
    assert first.scope.allowed_files == ("src/naumi_agent/ui/footer.py",)
    assert first.source.workbench_proposal_id == proposal_id
    assert first.source.reviewer == "Human"
    assert first.allowed_tools == ("file_read", "glob", "grep", "file_edit", "file_write")
    assert first.allowed_checks[0].verifier == "feedback_recurrence"
    assert first.network_access is False
    assert first.dependency_installation is False
    assert first.requires_worktree_lease is True
    assert first.requires_source_snapshot is True
    assert first.requires_static_guard is True
    assert first.execution_ready is False
    assert first.state == "contract"
    assert (workspace / "src/naumi_agent/ui/footer.py").read_bytes() == before
    assert _git(workspace, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_contract_issuer_rejects_open_or_stale_proposal(tmp_path: Path) -> None:
    workspace, store, _service, issuer, proposal_id = await _approved_fixture(
        tmp_path / "open",
        approve=False,
    )
    with pytest.raises(ValueError, match="只有 approved"):
        await issuer.issue(
            workspace,
            session_id="session-1",
            proposal_id=proposal_id,
            seed=1,
        )
    workspace, store, _service, issuer, proposal_id = await _approved_fixture(
        tmp_path / "stale"
    )
    intake = FeedbackIntakeService(store)
    await intake.ingest(
        workspace,
        build_direct_user_feedback(
            session_id="experiment-contract",
            category="defect",
            scope="src/naumi_agent/ui/footer.py:render_footer",
            topic="footer_truncation",
            summary="底栏第三次截断",
            now=NOW + timedelta(minutes=10),
        ),
    )
    with pytest.raises(ValueError, match="当前可信 Preview 不一致"):
        await issuer.issue(
            workspace,
            session_id="session-1",
            proposal_id=proposal_id,
            seed=1,
        )


@pytest.mark.asyncio
async def test_approved_multi_file_proposal_issues_bounded_contract(tmp_path: Path) -> None:
    scope = "files:src/naumi_agent/ui/footer.py,src/naumi_agent/ui/header.py"
    workspace, _store, _service, issuer, proposal_id = await _approved_fixture(
        tmp_path,
        scope=scope,
    )

    contract = await issuer.issue(
        workspace,
        session_id="session-1",
        proposal_id=proposal_id,
        seed=42,
    )

    assert contract.scope.impact_scope == scope
    assert contract.scope.allowed_files == (
        "src/naumi_agent/ui/footer.py",
        "src/naumi_agent/ui/header.py",
    )
    assert 2 <= contract.budget.max_changed_files <= 6
    assert contract.execution_ready is False


def test_experiment_scope_rejects_multi_file_display_authority_mismatch() -> None:
    with pytest.raises(ValidationError, match="allowed_files 不一致"):
        ExperimentScope(
            impact_scope=(
                "files:src/naumi_agent/ui/footer.py,src/naumi_agent/ui/header.py"
            ),
            allowed_files=(
                "src/naumi_agent/ui/header.py",
                "src/naumi_agent/ui/footer.py",
            ),
        )

@pytest.mark.asyncio
async def test_contract_budget_cannot_expand_risk_policy(tmp_path: Path) -> None:
    workspace, _store, _service, issuer, proposal_id = await _approved_fixture(tmp_path)
    oversized = ExperimentBudget(
        max_changed_files=16,
        max_changed_lines=2_000,
        max_tool_calls=200,
        max_duration_seconds=3_600,
        max_attempts=3,
    )

    with pytest.raises(ValueError, match="超过风险策略上限"):
        await issuer.issue(
            workspace,
            session_id="session-1",
            proposal_id=proposal_id,
            seed=1,
            budget=oversized,
        )


@pytest.mark.asyncio
async def test_contract_identity_rejects_manifest_tampering(tmp_path: Path) -> None:
    workspace, _store, _service, issuer, proposal_id = await _approved_fixture(tmp_path)
    contract = await issuer.issue(
        workspace,
        session_id="session-1",
        proposal_id=proposal_id,
        seed=7,
    )
    payload = contract.model_dump(mode="json")
    payload["seed"] = 8

    with pytest.raises(ValidationError, match="manifest_sha256"):
        EvolutionExperimentContract.model_validate(payload)


def test_git_baseline_reader_requires_exact_root_and_reports_dirty(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "Naumi Test")
    _git(root, "config", "user.email", "naumi@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    reader = GitExperimentBaselineReader()

    clean = reader.read(root)
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    dirty = reader.read(root)

    assert clean.commit == dirty.commit
    assert clean.workspace_dirty_at_issue is False
    assert dirty.workspace_dirty_at_issue is True
    with pytest.raises(ValueError, match="精确 Git 仓库根目录"):
        reader.read(nested)
