"""HTTP smoke tests for the Mac Workbench API contract."""

from __future__ import annotations

import asyncio
import json
import subprocess
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

            decision_response = client.post(
                f"/api/v1/workbench/sessions/{session_id}/missions/{mission_id}/decisions",
                json={
                    "title": "采用本地 FastAPI 合同",
                    "content": "SwiftUI 通过本地 HTTP 和事件流访问 Workbench。",
                    "actor": "Planner-Agent",
                    "kind": "architecture",
                },
            )
            assert decision_response.status_code == 201
            decision = decision_response.json()

            decision_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/missions/{mission_id}/decisions/{decision['id']}"
            )
            assert decision_detail_response.status_code == 200
            decision_detail = decision_detail_response.json()
            assert decision_detail["id"] == decision["id"]
            assert decision_detail["mission_id"] == mission_id
            assert decision_detail["kind"] == "architecture"
            assert decision_detail["title"] == "采用本地 FastAPI 合同"
            assert (
                decision_detail["content"]
                == "SwiftUI 通过本地 HTTP 和事件流访问 Workbench。"
            )
            assert decision_detail["actor"] == "Planner-Agent"

            intent_lock_response = client.post(
                f"/api/v1/workbench/sessions/{session_id}/missions/{mission_id}/intent-locks",
                json={
                    "actor": "Planner-Agent",
                    "rule": "高风险变更先提交 proposal",
                    "blocked_paths": ["src/naumi_agent/core"],
                    "allowed_paths": ["src/naumi_agent/core/README.md"],
                    "require_proposal_for_risk": "high",
                },
            )
            assert intent_lock_response.status_code == 201
            intent_lock = intent_lock_response.json()

            intent_lock_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/missions/{mission_id}/intent-locks/{intent_lock['id']}"
            )
            assert intent_lock_detail_response.status_code == 200
            intent_lock_detail = intent_lock_detail_response.json()
            assert intent_lock_detail["id"] == intent_lock["id"]
            assert intent_lock_detail["mission_id"] == mission_id
            assert intent_lock_detail["rule"] == "高风险变更先提交 proposal"
            assert intent_lock_detail["blocked_paths"] == ["src/naumi_agent/core"]
            assert intent_lock_detail["allowed_paths"] == [
                "src/naumi_agent/core/README.md"
            ]
            assert intent_lock_detail["require_proposal_for_risk"] == "high"
            assert intent_lock_detail["active"] is True

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

            approval = asyncio.run(
                engine.workbench_store.add_approval(
                    session_id=session_id,
                    mission_id=mission_id,
                    task_id=task_id,
                    title="允许执行高风险验证",
                    detail="验证通过后允许进入人工审查",
                    requester="Backend-Agent",
                )
            )
            approval_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/approvals/{approval.id}"
            )
            assert approval_detail_response.status_code == 200
            approval_detail = approval_detail_response.json()
            assert approval_detail["id"] == approval.id
            assert approval_detail["mission_id"] == mission_id
            assert approval_detail["task_id"] == task_id
            assert approval_detail["state"] == "waiting"
            assert approval_detail["title"] == "允许执行高风险验证"
            assert approval_detail["requester"] == "Backend-Agent"

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

            agent_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/agents/Backend-Agent"
            )
            assert agent_detail_response.status_code == 200
            agent_detail = agent_detail_response.json()
            assert agent_detail["id"] == "Backend-Agent"
            assert agent_detail["name"] == "Backend Agent"
            assert agent_detail["role"] == "api"
            assert agent_detail["capabilities"] == ["python", "fastapi"]
            assert agent_detail["permissions"] == ["read", "test"]
            assert agent_detail["status"] == "busy"

            claim_response = client.post(
                f"/api/v1/workbench/sessions/{session_id}/issues/{task_id}/claim",
                json={
                    "agent_id": "Backend-Agent",
                    "duration_minutes": 30,
                    "worktree_name": "wt-api-smoke",
                },
            )
            assert claim_response.status_code == 201
            claimed_lease = claim_response.json()

            lease_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/leases/{claimed_lease['id']}"
            )
            assert lease_detail_response.status_code == 200
            lease_detail = lease_detail_response.json()
            assert lease_detail["id"] == claimed_lease["id"]
            assert lease_detail["task_id"] == task_id
            assert lease_detail["agent_id"] == "Backend-Agent"
            assert lease_detail["state"] == "active"
            assert lease_detail["worktree_name"] == "wt-api-smoke"

            engine.worktree_manager.storage_dir.mkdir(parents=True, exist_ok=True)
            worktree_state_path = engine.worktree_manager.storage_dir / "worktrees.json"
            worktree_state_path.write_text(
                json.dumps(
                    {
                        "wt-api-smoke": {
                            "name": "wt-api-smoke",
                            "path": str(tmp_path / "missing-wt-api-smoke"),
                            "branch": "naumi/worktree-wt-api-smoke",
                            "base_ref": "abc123",
                            "status": "clean",
                            "task_id": task_id,
                            "dirty_files": 0,
                            "commits_ahead": 0,
                            "created_at": "2024-01-01T00:00:00",
                            "updated_at": "2024-01-01T00:00:00",
                            "kept_reason": "",
                            "metadata": {"agent_id": "Backend-Agent"},
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            worktrees_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/worktrees?task_id={task_id}&status=missing"
            )
            assert worktrees_response.status_code == 200
            worktrees = worktrees_response.json()
            assert worktrees["task_id"] == task_id
            assert worktrees["status"] == "missing"
            assert worktrees["limit"] == 50
            assert len(worktrees["worktrees"]) == 1
            worktree = worktrees["worktrees"][0]
            assert worktree["name"] == "wt-api-smoke"
            assert worktree["task_id"] == task_id
            assert worktree["status"] == "missing"
            assert worktree["metadata"] == {"agent_id": "Backend-Agent"}
            assert worktree["removable"] is False

            worktree_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/worktrees/wt-api-smoke"
            )
            assert worktree_detail_response.status_code == 200
            worktree_detail = worktree_detail_response.json()
            assert worktree_detail["name"] == "wt-api-smoke"
            assert worktree_detail["branch"] == "naumi/worktree-wt-api-smoke"
            assert worktree_detail["task_id"] == task_id
            assert worktree_detail["status"] == "missing"
            assert worktree_detail["metadata"] == {"agent_id": "Backend-Agent"}
            assert worktree_detail["removable"] is False

            worktree_path = tmp_path / "missing-wt-api-smoke"
            worktree_path.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=worktree_path,
                capture_output=True,
                check=True,
                text=True,
            )

            keep_worktree_response = client.post(
                f"/api/v1/workbench/sessions/{session_id}/worktrees/wt-api-smoke/keep",
                json={"actor": "Reviewer-Agent", "reason": "等待人工审查"},
            )
            assert keep_worktree_response.status_code == 200
            kept_worktree = keep_worktree_response.json()
            assert kept_worktree["name"] == "wt-api-smoke"
            assert kept_worktree["task_id"] == task_id
            assert kept_worktree["status"] == "kept"
            assert kept_worktree["kept_reason"] == "等待人工审查"
            assert kept_worktree["removable"] is False

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
            context_snapshot = context_response.json()

            context_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/context-snapshots/{context_snapshot['id']}"
            )
            assert context_detail_response.status_code == 200
            context_detail = context_detail_response.json()
            assert context_detail["id"] == context_snapshot["id"]
            assert context_detail["task_id"] == task_id
            assert context_detail["agent_id"] == "Backend-Agent"
            assert context_detail["health"] == "good"
            assert context_detail["reasons"] == ["上下文健康"]

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
                "worktree.kept",
                "issue.claimed",
                "agent_profile.upserted",
                "issue.created",
                "intent_lock.created",
                "decision.created",
                "mission.created",
            ]

            claimed_event = next(
                event for event in snapshot["events"] if event["type"] == "issue.claimed"
            )
            event_detail_response = client.get(
                f"/api/v1/workbench/sessions/{session_id}/events/{claimed_event['id']}"
            )
            assert event_detail_response.status_code == 200
            event_detail = event_detail_response.json()
            assert event_detail["id"] == claimed_event["id"]
            assert event_detail["type"] == "issue.claimed"
            assert event_detail["actor"] == "Backend-Agent"
            assert event_detail["subject_id"] == task_id
            assert event_detail["payload"]["lease_id"] == claimed_lease["id"]
    finally:
        asyncio.run(engine.close())
