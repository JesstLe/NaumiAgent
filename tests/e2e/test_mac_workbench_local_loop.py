"""End-to-end local-loop smoke test for the Mac Agent Workbench.

This drives the full Mac Workbench HTTP contract through a real (shim) engine and
``TestClient``, exercising every major page's data path against a temp workspace:

    create session
      -> create mission
      -> create issue
      -> claim lease
      -> record context health
      -> run validation
      -> create + approve proposal (M16)
      -> refresh dashboard snapshot
      -> list approvals / events / proposals

It runs entirely against ``127.0.0.1``-local state and needs no network access
beyond the in-process TestClient. No LLM API key is required because the
workbench routes never invoke the model loop.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from naumi_agent.api.routes.workbench import router as workbench_router
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore
from naumi_agent.workbench.validation import ValidationRunner
from naumi_agent.worktree.manager import WorktreeManager


class _LocalLoopEngine:
    """Minimal engine shim satisfying the workbench route surface."""

    def __init__(self, *, db_path: Path, workspace_root: Path) -> None:
        self.workspace_root = str(workspace_root)
        self.session_store = SessionStore(
            MemoryConfig(
                session_db_path=str(db_path),
                vector_db_path=str(workspace_root / "chroma"),
            )
        )
        self.task_store = TaskStore(str(db_path))
        self.workbench_store = WorkbenchStore(str(db_path))
        self.worktree_manager = WorktreeManager(
            repo_root=workspace_root,
            storage_dir=workspace_root / "worktrees",
            task_store=self.task_store,
        )
        self.validation_runner = ValidationRunner(
            store=self.workbench_store,
            allowed_commands=[[sys.executable, "-c"]],
            timeout_seconds=10,
        )
        self.workbench_service = WorkbenchService(
            task_store=self.task_store,
            workbench_store=self.workbench_store,
            validation_runner=self.validation_runner,
            workspace_root=str(workspace_root),
        )

    async def load_session(self, session_id: str) -> bool:
        session = await self.session_store.load(session_id)
        if session is None:
            return False
        self.task_store.set_session(session_id)
        return True

    async def close(self) -> None:
        await self.session_store.close()


def _build_client(tmp_path: Path) -> tuple[TestClient, _LocalLoopEngine, str]:
    async def _prepare() -> tuple[_LocalLoopEngine, str]:
        engine = _LocalLoopEngine(
            db_path=tmp_path / "local-loop.db",
            workspace_root=tmp_path,
        )
        session = await engine.session_store.create_session(
            title="Mac Workbench 本地闭环冒烟"
        )
        engine.task_store.set_session(session.id)
        return engine, session.id

    engine, session_id = asyncio.run(_prepare())
    app = FastAPI()
    app.state.engine = engine
    app.state.config = AppConfig(
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "local-loop.db"),
            vector_db_path=str(tmp_path / "chroma"),
        )
    )
    app.state.started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    app.include_router(workbench_router, prefix="/api/v1")
    client = TestClient(app)
    return client, engine, session_id


def test_mac_workbench_local_loop_creates_visible_real_data(tmp_path: Path) -> None:
    """A real local flow creates visible real data for all major pages."""
    client, engine, session_id = _build_client(tmp_path)

    try:
        # 1. Create a mission.
        mission_resp = client.post(
            f"/api/v1/workbench/sessions/{session_id}/missions",
            json={
                "title": "Workbench 本地闭环",
                "goal": "验证 mission/issue/lease/validation/proposal/snapshot 全链路",
            },
        )
        assert mission_resp.status_code == 201
        mission_id = mission_resp.json()["id"]

        # 2. Create an issue under the mission.
        issue_resp = client.post(
            f"/api/v1/workbench/sessions/{session_id}/missions/{mission_id}/issues",
            json={
                "title": "实现 Dashboard 实时刷新",
                "parallel_mode": "exclusive",
                "risk_level": "high",
                "requires_human_approval": True,
                "acceptance_criteria": ["snapshot 包含 mission", "空状态不显示假数据"],
                "expected_artifacts": ["src/dashboard.py"],
            },
        )
        assert issue_resp.status_code == 201
        task_id = issue_resp.json()["task_id"]

        # 3. Claim a lease for that issue.
        claim_resp = client.post(
            f"/api/v1/workbench/sessions/{session_id}/issues/{task_id}/claim",
            json={"agent_id": "backend-agent", "duration_minutes": 45, "worktree_name": ""},
        )
        assert claim_resp.status_code == 201
        assert claim_resp.json()["state"] == "active"

        # 4. Record context health for the active lease.
        context_resp = client.post(
            f"/api/v1/workbench/sessions/{session_id}/issues/{task_id}/context-health",
            json={
                "agent_id": "backend-agent",
                "minutes_since_sync": 2,
                "token_load_ratio": 0.3,
                "policy_conflict": False,
            },
        )
        assert context_resp.status_code == 201
        assert context_resp.json()["health"] == "good"

        # 5. Run validation (allowed command: python -c).
        validation_resp = client.post(
            f"/api/v1/workbench/sessions/{session_id}/validation-runs",
            json={
                "task_id": task_id,
                "actor": "backend-agent",
                "argv": [sys.executable, "-c", "print('ok')"],
            },
        )
        assert validation_resp.status_code == 201
        assert validation_resp.json()["status"] == "passed"

        # 6. Create a proposal (M16) — direct execution is gated by the high
        #    risk level, so the agent submits a proposal instead.
        proposal_resp = client.post(
            f"/api/v1/workbench/sessions/{session_id}/proposals",
            json={
                "mission_id": mission_id,
                "task_id": task_id,
                "agent_id": "backend-agent",
                "title": "高风险改动提案",
                "impact_scope": "核心调度与认证模块",
                "intended_files": ["src/auth.py"],
                "validation_plan": ["pytest tests/auth"],
                "risk_level": "high",
                "questions": ["是否兼容旧令牌?"],
            },
        )
        assert proposal_resp.status_code == 201
        proposal_id = proposal_resp.json()["id"]
        assert proposal_resp.json()["state"] == "open"

        # 7. Approve the proposal so the gated work may proceed.
        approve_resp = client.post(
            f"/api/v1/workbench/sessions/{session_id}/proposals/{proposal_id}/approve",
            json={"reviewer": "Human", "decision_note": "同意执行"},
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["state"] == "approved"

        # 8. Refresh the dashboard snapshot — it must surface all the real data.
        snapshot_resp = client.get(
            f"/api/v1/workbench/sessions/{session_id}/snapshot"
        )
        assert snapshot_resp.status_code == 200
        snapshot = snapshot_resp.json()
        assert snapshot["session_id"] == session_id
        assert any(m["id"] == mission_id for m in snapshot["missions"])
        assert any(i["task_id"] == task_id for i in snapshot["issues"])
        assert any(lease["task_id"] == task_id for lease in snapshot["leases"])
        assert any(v["task_id"] == task_id for v in snapshot["validation_runs"])
        # Proposals are part of the authoritative snapshot (M16).
        assert any(
            p["id"] == proposal_id and p["state"] == "approved"
            for p in snapshot["proposals"]
        )
        # Audit events must have been emitted for each lifecycle step.
        assert any(
            e["type"] == "proposal.created" for e in snapshot["events"]
        )
        assert any(
            e["type"] == "proposal.approved" for e in snapshot["events"]
        )

        # 9. List approvals, events, and proposals via their dedicated endpoints.
        approvals_resp = client.get(
            f"/api/v1/workbench/sessions/{session_id}/approvals"
        )
        assert approvals_resp.status_code == 200

        events_resp = client.get(
            f"/api/v1/workbench/sessions/{session_id}/events",
            params={"limit": 50},
        )
        assert events_resp.status_code == 200
        event_types = {e["type"] for e in events_resp.json()["events"]}
        assert "mission.created" in event_types
        assert "issue.claimed" in event_types
        assert "proposal.created" in event_types

        proposals_resp = client.get(
            f"/api/v1/workbench/sessions/{session_id}/proposals",
            params={"state": "approved"},
        )
        assert proposals_resp.status_code == 200
        assert len(proposals_resp.json()["proposals"]) == 1
        assert proposals_resp.json()["proposals"][0]["id"] == proposal_id
    finally:
        asyncio.run(engine.close())


def test_mac_workbench_local_loop_rejects_non_allowlisted_validation(
    tmp_path: Path,
) -> None:
    """A non-allowlisted validation command is rejected with a Chinese error."""
    client, engine, session_id = _build_client(tmp_path)

    try:
        mission_resp = client.post(
            f"/api/v1/workbench/sessions/{session_id}/missions",
            json={"title": "校验测试", "goal": "验证命令白名单"},
        )
        mission_id = mission_resp.json()["id"]
        issue_resp = client.post(
            f"/api/v1/workbench/sessions/{session_id}/missions/{mission_id}/issues",
            json={"title": "非法校验命令", "risk_level": "low"},
        )
        task_id = issue_resp.json()["task_id"]

        rejected = client.post(
            f"/api/v1/workbench/sessions/{session_id}/validation-runs",
            json={
                "task_id": task_id,
                "actor": "rogue-agent",
                "argv": ["rm", "-rf", "/"],
            },
        )
        assert rejected.status_code in (400, 422)
    finally:
        asyncio.run(engine.close())
