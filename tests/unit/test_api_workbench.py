"""Unit tests for workbench API routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from naumi_agent.api.routes.workbench import (
    IssueAttach,
    MissionCreate,
    attach_workbench_issue,
    create_workbench_mission,
    get_workbench_snapshot,
)
from naumi_agent.workbench.models import Mission, ParallelMode, RiskLevel


class _FakeSessionStore:
    def __init__(self, exists: bool) -> None:
        self.exists = exists

    async def load(self, session_id: str):
        if not self.exists:
            return None
        return SimpleNamespace(id=session_id)


class _FakeWorkbenchService:
    def __init__(self) -> None:
        self.created_missions: list[dict] = []
        self.attached_issues: list[dict] = []

    async def dashboard_snapshot(self, session_id: str):
        return {
            "session_id": session_id,
            "missions": [],
            "tasks": [],
            "issues": [],
            "failures": [],
            "events": [],
        }

    async def create_mission(self, *, session_id: str, title: str, goal: str):
        self.created_missions.append(
            {"session_id": session_id, "title": title, "goal": goal}
        )
        return Mission(
            id="mission-1",
            session_id=session_id,
            title=title,
            goal=goal,
        )

    async def attach_issue(
        self,
        *,
        session_id: str,
        mission_id: str,
        task_id: str,
        acceptance_criteria: list[str],
        parallel_mode: ParallelMode = ParallelMode.EXCLUSIVE,
        risk_level: RiskLevel = RiskLevel.MEDIUM,
    ):
        self.attached_issues.append(
            {
                "session_id": session_id,
                "mission_id": mission_id,
                "task_id": task_id,
                "acceptance_criteria": acceptance_criteria,
                "parallel_mode": parallel_mode,
                "risk_level": risk_level,
            }
        )
        return {
            "session_id": session_id,
            "task_id": task_id,
            "mission_id": mission_id,
            "parallel_mode": parallel_mode,
            "risk_level": risk_level,
            "requires_human_approval": True,
            "acceptance_criteria": list(acceptance_criteria),
            "expected_artifacts": [],
            "related_branch": "",
            "related_worktree": "",
            "related_pr": "",
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
        }


class _FakeEngine:
    def __init__(self, exists: bool) -> None:
        self.session_store = _FakeSessionStore(exists)
        self.workbench_service = _FakeWorkbenchService()
        self.loaded: list[str] = []

    async def load_session(self, session_id: str) -> bool:
        self.loaded.append(session_id)
        return self.session_store.exists


def _fake_request(engine: _FakeEngine):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))


@pytest.mark.asyncio
async def test_workbench_snapshot_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_workbench_snapshot("missing", _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_workbench_snapshot_endpoint_returns_service_snapshot() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_workbench_snapshot("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert response["session_id"] == "sess-1"
    assert "missions" in response
    assert "events" in response


@pytest.mark.asyncio
async def test_create_mission_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = MissionCreate(title="Mac 工作台", goal="可视化治理")

    with pytest.raises(HTTPException) as exc:
        await create_workbench_mission("missing", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_create_mission_endpoint_returns_created_mission() -> None:
    engine = _FakeEngine(exists=True)
    body = MissionCreate(title="Mac 工作台", goal="可视化治理多 Agent 研发")

    response = await create_workbench_mission("sess-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.created_missions == [
        {"session_id": "sess-1", "title": "Mac 工作台", "goal": "可视化治理多 Agent 研发"}
    ]
    assert response["session_id"] == "sess-1"
    assert response["title"] == "Mac 工作台"
    assert response["goal"] == "可视化治理多 Agent 研发"
    assert "id" in response
    assert response["status"] == "planning"


@pytest.mark.asyncio
async def test_attach_issue_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = IssueAttach(task_id="task-1", acceptance_criteria=["认领冲突必须被拒绝"])

    with pytest.raises(HTTPException) as exc:
        await attach_workbench_issue(
            "missing", "mission-1", body, _fake_request(engine), auth="test"
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_attach_issue_endpoint_returns_attached_issue() -> None:
    engine = _FakeEngine(exists=True)
    body = IssueAttach(
        task_id="task-1",
        acceptance_criteria=["AC1", "AC2"],
        parallel_mode=ParallelMode.COOPERATIVE,
        risk_level=RiskLevel.HIGH,
    )

    response = await attach_workbench_issue(
        "sess-1", "mission-1", body, _fake_request(engine), auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.attached_issues == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "task_id": "task-1",
            "acceptance_criteria": ["AC1", "AC2"],
            "parallel_mode": ParallelMode.COOPERATIVE,
            "risk_level": RiskLevel.HIGH,
        }
    ]
    assert response["session_id"] == "sess-1"
    assert response["mission_id"] == "mission-1"
    assert response["task_id"] == "task-1"
    assert response["acceptance_criteria"] == ["AC1", "AC2"]
    assert response["parallel_mode"] == ParallelMode.COOPERATIVE
    assert response["risk_level"] == RiskLevel.HIGH
