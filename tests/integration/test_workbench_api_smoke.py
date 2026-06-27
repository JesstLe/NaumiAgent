"""HTTP smoke tests for the Mac Workbench API contract."""

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


class _SmokeEngine:
    def __init__(self, *, db_path: Path, workspace_root: Path) -> None:
        self.session_store = SessionStore(
            MemoryConfig(
                session_db_path=str(db_path),
                vector_db_path=str(workspace_root / "chroma"),
            )
        )
        self.task_store = TaskStore(str(db_path))
        self.workbench_store = WorkbenchStore(str(db_path))
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


def test_mac_workbench_http_flow_refreshes_dashboard_snapshot(tmp_path: Path) -> None:
    """Create the local Mac App flow through HTTP and verify snapshot freshness."""

    async def _prepare_engine() -> tuple[_SmokeEngine, str]:
        engine = _SmokeEngine(
            db_path=tmp_path / "workbench-smoke.db",
            workspace_root=tmp_path,
        )
        session = await engine.session_store.create_session(title="Mac Workbench Smoke")
        engine.task_store.set_session(session.id)
        return engine, session.id

    engine, session_id = asyncio.run(_prepare_engine())
    app = FastAPI()
    app.state.engine = engine
    app.state.config = AppConfig(
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "workbench-smoke.db"),
            vector_db_path=str(tmp_path / "chroma"),
        )
    )
    app.state.started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    app.include_router(workbench_router, prefix="/api/v1")

    try:
        with TestClient(app) as client:
            mission_response = client.post(
                f"/api/v1/workbench/sessions/{session_id}/missions",
                json={
                    "title": "实现 SwiftUI 工作台闭环",
                    "goal": "验证 mission、issue、claim、context、validation 和 snapshot",
                },
            )
            assert mission_response.status_code == 201
            mission_id = mission_response.json()["id"]

            mission_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/missions/{mission_id}"
            )
            assert mission_detail_response.status_code == 200
            mission_detail = mission_detail_response.json()
            assert mission_detail["id"] == mission_id
            assert mission_detail["title"] == "实现 SwiftUI 工作台闭环"
            assert (
                mission_detail["goal"]
                == "验证 mission、issue、claim、context、validation 和 snapshot"
            )

            issue_response = client.post(
                f"/api/v1/workbench/sessions/{session_id}/missions/{mission_id}/issues",
                json={
                    "title": "实现 API smoke",
                    "description": "从 Mac App HTTP 合同验证 dashboard 刷新",
                    "blocked_by": [],
                    "acceptance_criteria": ["dashboard snapshot 必须刷新"],
                    "parallel_mode": "exclusive",
                    "risk_level": "medium",
                },
            )
            assert issue_response.status_code == 201
            task_id = issue_response.json()["task_id"]

            issue_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/issues/{task_id}"
            )
            assert issue_detail_response.status_code == 200
            issue_detail = issue_detail_response.json()
            assert issue_detail["task_id"] == task_id
            assert issue_detail["mission_id"] == mission_id
            assert issue_detail["acceptance_criteria"] == ["dashboard snapshot 必须刷新"]

            agent_response = client.post(
                f"/api/v1/workbench/sessions/{session_id}/agents/Backend-Agent",
                json={
                    "name": "Backend Agent",
                    "role": "api",
                    "capabilities": ["python", "fastapi"],
                    "permissions": ["read", "test"],
                    "max_parallel_tasks": 1,
                    "status": "busy",
                    "actor": "Human",
                },
            )
            assert agent_response.status_code == 201

            claim_response = client.post(
                f"/api/v1/workbench/sessions/{session_id}/issues/{task_id}/claim",
                json={
                    "agent_id": "Backend-Agent",
                    "duration_minutes": 30,
                    "worktree_name": "wt-api-smoke",
                },
            )
            assert claim_response.status_code == 201

            context_response = client.post(
                f"/api/v1/workbench/sessions/{session_id}/issues/{task_id}/context-health",
                json={
                    "agent_id": "Backend-Agent",
                    "minutes_since_sync": 5,
                    "token_load_ratio": 0.25,
                    "policy_conflict": False,
                    "actor": "Backend-Agent",
                },
            )
            assert context_response.status_code == 201

            validation_response = client.post(
                f"/api/v1/workbench/sessions/{session_id}/validation-runs",
                json={
                    "task_id": task_id,
                    "actor": "Backend-Agent",
                    "argv": [sys.executable, "-c", "print('validation ok')"],
                    "cwd": str(tmp_path),
                },
            )
            assert validation_response.status_code == 201
            validation_run = validation_response.json()
            assert validation_run["status"] == "passed"

            validation_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/validation-runs/{validation_run['id']}"
            )
            assert validation_detail_response.status_code == 200
            validation_detail = validation_detail_response.json()
            assert validation_detail["id"] == validation_run["id"]
            assert validation_detail["task_id"] == task_id
            assert validation_detail["command"] == [
                sys.executable,
                "-c",
                "print('validation ok')",
            ]
            assert validation_detail["status"] == "passed"

            snapshot_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/snapshot"
            )
            assert snapshot_response.status_code == 200
            snapshot = snapshot_response.json()

            assert snapshot["session_id"] == session_id
            assert [mission["id"] for mission in snapshot["missions"]] == [mission_id]
            assert [task["subject"] for task in snapshot["tasks"]] == ["实现 API smoke"]
            assert [issue["task_id"] for issue in snapshot["issues"]] == [task_id]
            assert snapshot["issues"][0]["related_worktree"] == "wt-api-smoke"
            assert [lease["agent_id"] for lease in snapshot["leases"]] == ["Backend-Agent"]
            assert [profile["id"] for profile in snapshot["agent_profiles"]] == [
                "Backend-Agent"
            ]
            assert [run["status"] for run in snapshot["validation_runs"]] == ["passed"]
            assert [health["health"] for health in snapshot["context_snapshots"]] == ["good"]

            event_types = [event["type"] for event in snapshot["events"]]
            assert event_types == [
                "validation.completed",
                "context_health.recorded",
                "issue.claimed",
                "agent_profile.upserted",
                "issue.created",
                "mission.created",
            ]
    finally:
        asyncio.run(engine.close())
