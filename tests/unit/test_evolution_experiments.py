from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.experiments import (
    EvolutionExperimentContract,
    EvolutionExperimentContractIssuer,
    EvolutionExperimentContractStore,
    EvolutionExperimentContractStoreError,
    ExperimentBudget,
    ExperimentScope,
    GitExperimentBaselineReader,
    default_experiment_seed,
    render_experiment_contract_authority,
)
from naumi_agent.evolution.queue import EvolutionProposalQueueAdapter
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import FeedbackIntakeService, build_direct_user_feedback
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tasks.store import TaskStore
from naumi_agent.tools.evolution_review import (
    EvolutionExperimentContractAuthorityTool,
    EvolutionExperimentContractIssueTool,
)
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.protocol import ClientEventType
from naumi_agent.workbench.proposal_governance import ProposalAction
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore

NOW = datetime(2026, 7, 18, 22, 0, tzinfo=UTC)


class _ProposalOutcomeReader:
    def __init__(self, projections=None, *, error: Exception | None = None) -> None:
        self.projections = projections or {}
        self.error = error

    async def project_session(self, session_id: str):
        if self.error is not None:
            raise self.error
        return self.projections


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
    contract_store = EvolutionExperimentContractStore(runtime_db)
    issuer = EvolutionExperimentContractIssuer(
        review_service=review_service,
        workbench_service=service,
        store=contract_store,
    )
    issuer.bind_proposal_outcome_reader(_ProposalOutcomeReader())
    return (
        workspace,
        evolution_store,
        service,
        contract_store,
        issuer,
        queued.proposal["id"],
    )


class _ExperimentContractBridgeEngine:
    def __init__(
        self,
        *,
        workspace: Path,
        service: WorkbenchService,
        store: EvolutionExperimentContractStore,
        issuer: EvolutionExperimentContractIssuer,
        mode: PermissionMode,
    ) -> None:
        self.workspace_root = workspace
        self.workbench_service = service
        self.evolution_experiment_contract_store = store
        self.evolution_experiment_contract_issuer = issuer
        self._permission_checker = PermissionChecker(
            mode,
            workspace_root=str(workspace),
        )
        self._session = SimpleNamespace(id="session-1")

    def set_permission_confirmer(self, confirmer) -> None:
        self.permission_confirmer = confirmer

    def set_user_interaction_handler(self, handler) -> None:
        self.user_interaction_handler = handler

    async def get_or_create_session(self):
        return self._session


@pytest.mark.asyncio
async def test_approved_proposal_issues_stable_non_executable_contract(
    tmp_path: Path,
) -> None:
    workspace, _store, _service, contract_store, issuer, proposal_id = (
        await _approved_fixture(tmp_path)
    )
    baseline = _git(workspace, "rev-parse", "HEAD")
    before = (workspace / "src/naumi_agent/ui/footer.py").read_bytes()

    repeated = await asyncio.gather(*(
        issuer.issue(
            workspace,
            session_id="session-1",
            proposal_id=proposal_id,
            seed=default_experiment_seed(proposal_id) + index,
        )
        for index in range(5)
    ))
    first = repeated[0]
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
    assert all(item == first for item in repeated)
    authority = await contract_store.get(workspace, first.contract_id)
    assert authority is not None
    assert authority.contract == first
    assert authority.workspace_root == str(workspace.resolve())
    assert authority.authority_id == f"evxauth_{authority.authority_sha256[:24]}"
    assert await contract_store.get_by_proposal(
        workspace,
        session_id="session-1",
        proposal_id=proposal_id,
    ) == authority
    assert await contract_store.get(tmp_path / "other-workspace", first.contract_id) is None
    engine = SimpleNamespace(
        workspace_root=workspace,
        evolution_experiment_contract_store=contract_store,
        evolution_experiment_contract_issuer=issuer,
        _session=SimpleNamespace(id="session-1"),
    )
    tool_output = await EvolutionExperimentContractAuthorityTool(engine).execute(
        first.contract_id
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution experiment-contract {first.contract_id}",
    )
    assert tool_output == render_experiment_contract_authority(authority)
    issue_output = await EvolutionExperimentContractIssueTool(engine).execute(proposal_id)
    assert issue_output == render_experiment_contract_authority(authority)
    assert "不是执行或推广许可" in issue_output
    assert authority.authority_id in slash_output
    assert "不是执行或推广许可" in slash_output
    tampered_authority = authority.model_dump(mode="json")
    tampered_authority["workspace_root"] = str((tmp_path / "forged").resolve())
    with pytest.raises(ValidationError, match="Authority 摘要不一致"):
        type(authority).model_validate(tampered_authority)
    assert (workspace / "src/naumi_agent/ui/footer.py").read_bytes() == before
    assert _git(workspace, "status", "--porcelain") == ""

    with sqlite3.connect(tmp_path / "runtime.db") as db:
        db.execute(
            "UPDATE evolution_experiment_contracts SET manifest_sha256 = ? "
            "WHERE workspace_root = ? AND contract_id = ?",
            ("0" * 64, str(workspace.resolve()), first.contract_id),
        )
        db.commit()
    with pytest.raises(EvolutionExperimentContractStoreError) as corrupt:
        await contract_store.get(workspace, first.contract_id)
    assert corrupt.value.code == "experiment_contract_authority_store_corrupt"


@pytest.mark.asyncio
async def test_contract_issuer_fails_closed_for_terminal_or_unavailable_outcome(
    tmp_path: Path,
) -> None:
    workspace, _store, _service, _contract_store, issuer, proposal_id = (
        await _approved_fixture(tmp_path)
    )
    terminal = {
        "workbench_session_id": "session-1",
        "workbench_proposal_id": proposal_id,
        "status": "rolled_back",
        "contract_issue_allowed": False,
    }
    issuer.bind_proposal_outcome_reader(
        _ProposalOutcomeReader({proposal_id: terminal})
    )

    with pytest.raises(EvolutionExperimentContractStoreError) as blocked:
        await issuer.issue(
            workspace,
            session_id="session-1",
            proposal_id=proposal_id,
            seed=42,
        )
    assert blocked.value.code == "experiment_contract_outcome_terminal"

    issuer.bind_proposal_outcome_reader(
        _ProposalOutcomeReader(error=OSError("private storage detail"))
    )
    with pytest.raises(EvolutionExperimentContractStoreError) as unavailable:
        await issuer.issue(
            workspace,
            session_id="session-1",
            proposal_id=proposal_id,
            seed=42,
        )
    assert unavailable.value.code == "experiment_contract_outcome_source_unavailable"
    assert "private storage detail" not in str(unavailable.value)


@pytest.mark.asyncio
async def test_bridge_requires_confirmation_then_returns_contract_authority(
    tmp_path: Path,
) -> None:
    workspace, _candidate_store, service, store, issuer, proposal_id = (
        await _approved_fixture(tmp_path)
    )
    before = (workspace / "src/naumi_agent/ui/footer.py").read_bytes()
    engine = _ExperimentContractBridgeEngine(
        workspace=workspace,
        service=service,
        store=store,
        issuer=issuer,
        mode=PermissionMode.MODERATE,
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")  # type: ignore[arg-type]
    bridge.bind_writer(writer)
    base = {
        "session_id": "session-1",
        "proposal_id": proposal_id,
        "action": "issue_contract",
        "decision_note": "",
    }

    await bridge.handle_client_record(
        {
            "id": "contract-preview",
            "type": ClientEventType.WORKBENCH_PROPOSAL_ACTION,
            "payload": {**base, "confirmed": False},
        }
    )
    preview = [
        json.loads(line)
        for line in writer.getvalue().splitlines()
        if line.strip()
    ][-1]
    assert preview["payload"]["status"] == "needs_confirmation"
    assert await store.get_by_proposal(
        workspace,
        session_id="session-1",
        proposal_id=proposal_id,
    ) is None

    await bridge.handle_client_record(
        {
            "id": "contract-confirm",
            "type": ClientEventType.WORKBENCH_PROPOSAL_ACTION,
            "payload": {**base, "confirmed": True},
        }
    )
    records = [
        json.loads(line)
        for line in writer.getvalue().splitlines()
        if line.strip()
    ]
    completed = [
        record
        for record in records
        if record["type"] == "workbench/proposal/action_result"
    ][-1]
    summary = completed["payload"]["experiment_contract"]
    assert completed["payload"]["status"] == "completed"
    assert summary["proposal_id"] == proposal_id
    assert summary["contract_id"].startswith("evx_")
    assert summary["authority_id"].startswith("evxauth_")
    assert summary["execution_ready"] is False
    assert summary["promotion_ready"] is False
    assert completed["payload"]["workbench_snapshot"]["counts"]["reviews"] == 1
    assert (workspace / "src/naumi_agent/ui/footer.py").read_bytes() == before
    assert _git(workspace, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_contract_store_migrates_legacy_authority_projection(tmp_path: Path) -> None:
    workspace, _candidate_store, _service, source_store, issuer, proposal_id = (
        await _approved_fixture(tmp_path / "source")
    )
    contract = await issuer.issue(
        workspace,
        session_id="session-1",
        proposal_id=proposal_id,
        seed=default_experiment_seed(proposal_id),
    )
    authority = await source_store.get(workspace, contract.contract_id)
    assert authority is not None
    legacy_path = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_path) as db:
        db.execute(
            """
            CREATE TABLE evolution_experiment_contracts (
                workspace_root TEXT NOT NULL,
                contract_id TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                authority_id TEXT NOT NULL,
                authority_sha256 TEXT NOT NULL,
                authority_json TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                PRIMARY KEY (workspace_root, contract_id)
            )
            """
        )
        db.execute(
            "INSERT INTO evolution_experiment_contracts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                authority.workspace_root,
                authority.contract_id,
                authority.contract_manifest_sha256,
                authority.authority_id,
                authority.authority_sha256,
                json.dumps(
                    authority.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                authority.approved_at,
            ),
        )
        db.commit()

    restored = await EvolutionExperimentContractStore(legacy_path).get_by_proposal(
        workspace,
        session_id="session-1",
        proposal_id=proposal_id,
    )

    assert restored == authority
    with sqlite3.connect(legacy_path) as db:
        columns = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(evolution_experiment_contracts)"
            ).fetchall()
        }
        projection = db.execute(
            "SELECT source_session_id, workbench_proposal_id "
            "FROM evolution_experiment_contracts"
        ).fetchone()
    assert {"source_session_id", "workbench_proposal_id"} <= columns
    assert projection == ("session-1", proposal_id)


@pytest.mark.asyncio
async def test_contract_issuer_rejects_open_or_stale_proposal(tmp_path: Path) -> None:
    workspace, store, _service, _contract_store, issuer, proposal_id = await _approved_fixture(
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
    workspace, store, _service, _contract_store, issuer, proposal_id = await _approved_fixture(
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
    workspace, _store, _service, _contract_store, issuer, proposal_id = await _approved_fixture(
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
    workspace, _store, _service, _contract_store, issuer, proposal_id = (
        await _approved_fixture(tmp_path)
    )
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
    workspace, _store, _service, _contract_store, issuer, proposal_id = (
        await _approved_fixture(tmp_path)
    )
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
