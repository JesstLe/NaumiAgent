"""Unit tests for workbench API routes."""

from __future__ import annotations

import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from naumi_agent import __version__
from naumi_agent.api.routes.workbench import (
    ApprovalResolve,
    ClaimIssue,
    DecisionCreate,
    IntentLockCreate,
    IssueAttach,
    MissionCreate,
    ValidationRunCreate,
    attach_workbench_issue,
    claim_workbench_issue,
    create_decision,
    create_intent_lock,
    create_validation_run,
    create_workbench_mission,
    expire_workbench_leases,
    get_approvals,
    get_context_snapshots,
    get_daemon_status,
    get_validation_runs,
    get_workbench_capabilities,
    get_workbench_events,
    get_workbench_snapshot,
    release_workbench_lease,
    resolve_approval,
)
from naumi_agent.workbench.models import (
    ApprovalState,
    ContextHealth,
    DecisionKind,
    Lease,
    LeaseState,
    Mission,
    ParallelMode,
    RiskLevel,
)


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
        self.created_intent_locks: list[dict] = []
        self.created_decisions: list[dict] = []
        self.resolved_approvals: list[dict] = []
        self.run_validations: list[dict] = []
        self.listed_events: list[dict] = []
        self.listed_validation_runs: list[dict] = []
        self.listed_context_snapshots: list[dict] = []
        self.listed_approvals: list[dict] = []
        self._run_validation_error: Exception | None = None
        self._intent_lock_error: Exception | None = None
        self._decision_error: Exception | None = None
        self._resolve_approval_error: Exception | None = None
        self._resolve_approval_result: dict | None = None

    def set_run_validation_error(self, error: Exception) -> None:
        self._run_validation_error = error

    def set_intent_lock_error(self, error: Exception) -> None:
        self._intent_lock_error = error

    def set_decision_error(self, error: Exception) -> None:
        self._decision_error = error

    def set_resolve_approval_error(self, error: Exception) -> None:
        self._resolve_approval_error = error

    def set_resolve_approval_result(self, result: dict | None) -> None:
        self._resolve_approval_result = result

    async def dashboard_snapshot(self, session_id: str):
        return {
            "session_id": session_id,
            "missions": [],
            "tasks": [],
            "issues": [],
            "leases": [],
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

    async def create_intent_lock(
        self,
        *,
        session_id: str,
        mission_id: str,
        actor: str,
        rule: str,
        blocked_paths: list[str] | None = None,
        allowed_paths: list[str] | None = None,
        require_proposal_for_risk: RiskLevel = RiskLevel.HIGH,
    ):
        self.created_intent_locks.append(
            {
                "session_id": session_id,
                "mission_id": mission_id,
                "actor": actor,
                "rule": rule,
                "blocked_paths": blocked_paths,
                "allowed_paths": allowed_paths,
                "require_proposal_for_risk": require_proposal_for_risk,
            }
        )
        if self._intent_lock_error is not None:
            raise self._intent_lock_error
        return {
            "id": "lock-1",
            "session_id": session_id,
            "mission_id": mission_id,
            "rule": rule,
            "blocked_paths": list(blocked_paths or []),
            "allowed_paths": list(allowed_paths or []),
            "require_proposal_for_risk": require_proposal_for_risk.value,
            "active": True,
            "created_at": "2024-01-01T00:00:00",
        }

    async def create_decision(
        self,
        *,
        session_id: str,
        mission_id: str,
        actor: str,
        kind: DecisionKind,
        title: str,
        content: str,
    ):
        self.created_decisions.append(
            {
                "session_id": session_id,
                "mission_id": mission_id,
                "actor": actor,
                "kind": kind,
                "title": title,
                "content": content,
            }
        )
        if self._decision_error is not None:
            raise self._decision_error
        return {
            "id": "decision-1",
            "session_id": session_id,
            "mission_id": mission_id,
            "kind": kind.value,
            "title": title,
            "content": content,
            "actor": actor,
            "created_at": "2024-01-01T00:00:00",
        }

    async def resolve_approval(
        self,
        *,
        session_id: str,
        approval_id: str,
        actor: str,
        state: ApprovalState,
        decision_note: str,
    ):
        self.resolved_approvals.append(
            {
                "session_id": session_id,
                "approval_id": approval_id,
                "actor": actor,
                "state": state,
                "decision_note": decision_note,
            }
        )
        if self._resolve_approval_error is not None:
            raise self._resolve_approval_error
        if self._resolve_approval_result is None:
            return None
        return self._resolve_approval_result

    async def list_events(self, session_id: str, limit: int = 50):
        self.listed_events.append({"session_id": session_id, "limit": limit})
        return [
            {
                "id": "evt-1",
                "session_id": session_id,
                "type": "mission.created",
                "actor": "Human",
                "subject_id": "mission-1",
                "payload": {"title": "Mac 工作台"},
                "timestamp": "2024-01-01T00:00:00",
            }
        ]

    async def run_validation(
        self,
        *,
        session_id: str,
        task_id: str,
        actor: str,
        argv: list[str],
        cwd: str | None = None,
    ):
        self.run_validations.append(
            {
                "session_id": session_id,
                "task_id": task_id,
                "actor": actor,
                "argv": argv,
                "cwd": cwd,
            }
        )
        if self._run_validation_error is not None:
            raise self._run_validation_error
        return {
            "id": "run-1",
            "status": "passed",
            "exit_code": 0,
            "output": "ok",
        }

    async def list_validation_runs(
        self, session_id: str, task_id: str | None = None, limit: int = 50
    ):
        self.listed_validation_runs.append(
            {"session_id": session_id, "task_id": task_id, "limit": limit}
        )
        return [
            {
                "id": "run-1",
                "session_id": session_id,
                "task_id": task_id or "task-1",
                "actor": "ValidationRunner",
                "command": ["pytest", "test.py"],
                "cwd": "/workspace",
                "status": "passed",
                "exit_code": 0,
                "output": "ok",
                "started_at": "2024-01-01T00:00:00",
                "completed_at": "2024-01-01T00:00:01",
            }
        ]

    async def list_context_snapshots(
        self,
        session_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ):
        self.listed_context_snapshots.append(
            {
                "session_id": session_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "limit": limit,
            }
        )
        return [
            {
                "id": "snap-1",
                "session_id": session_id,
                "agent_id": agent_id or "agent-1",
                "task_id": task_id or "task-1",
                "health": ContextHealth.GOOD,
                "reasons": ["上下文健康"],
                "created_at": "2024-01-01T00:00:00",
            }
        ]

    async def list_approvals(
        self,
        session_id: str,
        state: ApprovalState | None = None,
        limit: int = 50,
    ):
        self.listed_approvals.append(
            {"session_id": session_id, "state": state, "limit": limit}
        )
        return [
            {
                "id": "approval-1",
                "session_id": session_id,
                "mission_id": "mission-1",
                "task_id": "task-1",
                "state": (state.value if state else "waiting"),
                "title": "请求审批",
                "detail": "详情",
                "requester": "Agent-A",
                "reviewer": "",
                "decision_note": "",
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
        ]


class FakeTaskMarket:
    def __init__(self) -> None:
        self.claimed: list[dict] = []
        self.released: list[str] = []
        self.expired_calls = 0
        self._lease: Lease | None = None
        self._expired: list[Lease] = []
        self._claim_error: ValueError | None = None

    def set_lease(self, lease: Lease | None) -> None:
        self._lease = lease

    def set_expired(self, leases: list[Lease]) -> None:
        self._expired = leases

    def set_claim_error(self, error: ValueError) -> None:
        self._claim_error = error

    async def claim(
        self,
        *,
        task_id: str,
        agent_id: str,
        duration_minutes: int = 45,
        worktree_name: str = "",
    ) -> Lease:
        if self._claim_error is not None:
            raise self._claim_error
        self.claimed.append(
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "duration_minutes": duration_minutes,
                "worktree_name": worktree_name,
            }
        )
        if self._lease is None:
            raise RuntimeError("FakeTaskMarket: lease not configured")
        return self._lease

    async def release(self, lease_id: str) -> Lease | None:
        self.released.append(lease_id)
        return self._lease

    async def expire_overdue_leases(self, *, now=None) -> list[Lease]:
        self.expired_calls += 1
        return list(self._expired)


class _FakeEngine:
    def __init__(self, exists: bool, workbench_market=None) -> None:
        self.session_store = _FakeSessionStore(exists)
        self.workbench_service = _FakeWorkbenchService()
        self.workbench_market = workbench_market
        self.loaded: list[str] = []

    async def load_session(self, session_id: str) -> bool:
        self.loaded.append(session_id)
        return self.session_store.exists


def _fake_request(engine: _FakeEngine):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))


class _FakeSessionStoreWithCount:
    def __init__(self, total: int) -> None:
        self.total = total

    async def list_sessions(
        self, page: int = 1, page_size: int = 20, query: str = ""
    ) -> tuple[list, int]:
        return ([], self.total)


def _fake_status_request(
    engine: _FakeEngine,
    started_at: str = "2026-06-27T10:00:00+00:00",
    hostname: str = "127.0.0.1",
    port: int = 8765,
):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(engine=engine, started_at=started_at)
        ),
        url=SimpleNamespace(hostname=hostname, port=port),
    )


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
    assert "leases" in response
    assert response["leases"] == []
    assert "events" in response


@pytest.mark.asyncio
async def test_get_events_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_workbench_events("missing", _fake_request(engine), limit=10, auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_events_endpoint_returns_events_and_limit() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_workbench_events("sess-1", _fake_request(engine), limit=25, auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_events == [
        {"session_id": "sess-1", "limit": 25}
    ]
    assert response.model_dump() == {
        "events": [
            {
                "id": "evt-1",
                "session_id": "sess-1",
                "type": "mission.created",
                "actor": "Human",
                "subject_id": "mission-1",
                "payload": {"title": "Mac 工作台"},
                "timestamp": "2024-01-01T00:00:00",
            }
        ],
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_get_validation_runs_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_validation_runs(
            "missing", _fake_request(engine), task_id=None, limit=10, auth="test"
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_validation_runs_endpoint_returns_runs_and_params() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_validation_runs(
        "sess-1", _fake_request(engine), task_id="task-2", limit=25, auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_validation_runs == [
        {"session_id": "sess-1", "task_id": "task-2", "limit": 25}
    ]
    assert response.model_dump() == {
        "validation_runs": [
            {
                "id": "run-1",
                "session_id": "sess-1",
                "task_id": "task-2",
                "actor": "ValidationRunner",
                "command": ["pytest", "test.py"],
                "cwd": "/workspace",
                "status": "passed",
                "exit_code": 0,
                "output": "ok",
                "started_at": "2024-01-01T00:00:00",
                "completed_at": "2024-01-01T00:00:01",
            }
        ],
        "task_id": "task-2",
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_get_context_snapshots_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_context_snapshots(
            "missing",
            _fake_request(engine),
            task_id=None,
            agent_id=None,
            limit=10,
            auth="test",
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_context_snapshots_endpoint_returns_snapshots_and_params() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_context_snapshots(
        "sess-1",
        _fake_request(engine),
        task_id="task-2",
        agent_id="agent-2",
        limit=25,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_context_snapshots == [
        {
            "session_id": "sess-1",
            "task_id": "task-2",
            "agent_id": "agent-2",
            "limit": 25,
        }
    ]
    assert response.model_dump() == {
        "context_snapshots": [
            {
                "id": "snap-1",
                "session_id": "sess-1",
                "agent_id": "agent-2",
                "task_id": "task-2",
                "health": ContextHealth.GOOD,
                "reasons": ["上下文健康"],
                "created_at": "2024-01-01T00:00:00",
            }
        ],
        "task_id": "task-2",
        "agent_id": "agent-2",
        "limit": 25,
    }


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


@pytest.mark.asyncio
async def test_claim_issue_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False, workbench_market=FakeTaskMarket())
    body = ClaimIssue(agent_id="Agent-1")

    with pytest.raises(HTTPException) as exc:
        await claim_workbench_issue("missing", "task-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_claim_issue_endpoint_returns_created_lease() -> None:
    market = FakeTaskMarket()
    lease = Lease(
        id="lease-1",
        session_id="sess-1",
        task_id="task-1",
        agent_id="Agent-1",
        state=LeaseState.ACTIVE,
        expires_at="2024-01-01T01:00:00",
        worktree_name="wt-1",
    )
    market.set_lease(lease)
    engine = _FakeEngine(exists=True, workbench_market=market)
    body = ClaimIssue(agent_id="Agent-1", duration_minutes=30, worktree_name="wt-1")

    request = _fake_request(engine)
    response = await claim_workbench_issue(
        "sess-1", "task-1", body, request, auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert market.claimed == [
        {
            "task_id": "task-1",
            "agent_id": "Agent-1",
            "duration_minutes": 30,
            "worktree_name": "wt-1",
        }
    ]
    assert response["id"] == "lease-1"
    assert response["task_id"] == "task-1"
    assert response["agent_id"] == "Agent-1"
    assert response["state"] == LeaseState.ACTIVE
    assert response["worktree_name"] == "wt-1"


@pytest.mark.asyncio
async def test_claim_issue_endpoint_maps_value_error_to_400() -> None:
    market = FakeTaskMarket()
    market.set_claim_error(ValueError("任务 #task-1 不存在"))
    engine = _FakeEngine(exists=True, workbench_market=market)
    body = ClaimIssue(agent_id="Agent-1")

    with pytest.raises(HTTPException) as exc:
        await claim_workbench_issue("sess-1", "task-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_get_approvals_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_approvals(
            "missing", _fake_request(engine), state=None, limit=10, auth="test"
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_approvals_endpoint_returns_approvals_and_params() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_approvals(
        "sess-1",
        _fake_request(engine),
        state=ApprovalState.WAITING,
        limit=25,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_approvals == [
        {"session_id": "sess-1", "state": ApprovalState.WAITING, "limit": 25}
    ]
    assert response.model_dump() == {
        "approvals": [
            {
                "id": "approval-1",
                "session_id": "sess-1",
                "mission_id": "mission-1",
                "task_id": "task-1",
                "state": "waiting",
                "title": "请求审批",
                "detail": "详情",
                "requester": "Agent-A",
                "reviewer": "",
                "decision_note": "",
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
        ],
        "state": "waiting",
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_get_approvals_endpoint_without_state_filter() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_approvals(
        "sess-1", _fake_request(engine), state=None, limit=50, auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_approvals == [
        {"session_id": "sess-1", "state": None, "limit": 50}
    ]
    assert response.model_dump()["state"] is None
    assert response.model_dump()["limit"] == 50
    assert len(response.model_dump()["approvals"]) == 1


@pytest.mark.asyncio
async def test_resolve_approval_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = ApprovalResolve(state=ApprovalState.APPROVED)

    with pytest.raises(HTTPException) as exc:
        await resolve_approval("missing", "approval-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_resolve_approval_endpoint_returns_resolved_approval() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_resolve_approval_result(
        {
            "id": "approval-1",
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "task_id": "task-1",
            "state": "approved",
            "title": "允许重构",
            "detail": "保持测试通过",
            "requester": "Agent-A",
            "reviewer": "Human",
            "decision_note": "同意",
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:01",
        }
    )
    body = ApprovalResolve(actor="Human", state=ApprovalState.APPROVED, decision_note="同意")

    response = await resolve_approval(
        "sess-1", "approval-1", body, _fake_request(engine), auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.resolved_approvals == [
        {
            "session_id": "sess-1",
            "approval_id": "approval-1",
            "actor": "Human",
            "state": ApprovalState.APPROVED,
            "decision_note": "同意",
        }
    ]
    assert response["id"] == "approval-1"
    assert response["state"] == "approved"


@pytest.mark.asyncio
async def test_resolve_approval_endpoint_returns_404_when_missing() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_resolve_approval_result(None)
    body = ApprovalResolve(state=ApprovalState.REJECTED)

    with pytest.raises(HTTPException) as exc:
        await resolve_approval(
            "sess-1", "approval-missing", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 404
    assert exc.value.detail == "审批请求不存在"


@pytest.mark.asyncio
async def test_resolve_approval_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_resolve_approval_error(
        ValueError("审批结果只能是 approved 或 rejected")
    )
    body = ApprovalResolve(state=ApprovalState.WAITING)

    with pytest.raises(HTTPException) as exc:
        await resolve_approval("sess-1", "approval-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 400
    assert exc.value.detail == "审批结果只能是 approved 或 rejected"


@pytest.mark.asyncio
async def test_release_lease_endpoint_returns_404_when_missing() -> None:
    market = FakeTaskMarket()
    market.set_lease(None)
    engine = _FakeEngine(exists=True, workbench_market=market)

    with pytest.raises(HTTPException) as exc:
        await release_workbench_lease("sess-1", "lease-missing", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert market.released == ["lease-missing"]
    assert exc.value.status_code == 404
    assert exc.value.detail == "租约不存在"


@pytest.mark.asyncio
async def test_release_lease_endpoint_returns_released_lease() -> None:
    market = FakeTaskMarket()
    lease = Lease(
        id="lease-2",
        session_id="sess-1",
        task_id="task-2",
        agent_id="Agent-2",
        state=LeaseState.RELEASED,
        expires_at="2024-01-01T02:00:00",
    )
    market.set_lease(lease)
    engine = _FakeEngine(exists=True, workbench_market=market)

    request = _fake_request(engine)
    response = await release_workbench_lease(
        "sess-1", "lease-2", request, auth="test"
    )

    assert market.released == ["lease-2"]
    assert response["id"] == "lease-2"
    assert response["state"] == LeaseState.RELEASED


@pytest.mark.asyncio
async def test_expire_leases_endpoint_returns_expired_list() -> None:
    market = FakeTaskMarket()
    lease = Lease(
        id="lease-3",
        session_id="sess-1",
        task_id="task-3",
        agent_id="Agent-3",
        state=LeaseState.EXPIRED,
        expires_at="2024-01-01T00:00:00",
    )
    market.set_expired([lease])
    engine = _FakeEngine(exists=True, workbench_market=market)

    response = await expire_workbench_leases("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert market.expired_calls == 1
    assert response == {"expired": [asdict(lease)]}


@pytest.mark.asyncio
async def test_daemon_status_returns_expected_fields() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store = _FakeSessionStoreWithCount(total=7)
    request = _fake_status_request(
        engine,
        started_at="2026-06-27T10:00:00+00:00",
        hostname="localhost",
        port=9876,
    )

    response = await get_daemon_status(request, auth="test")

    assert response.status == "running"
    assert response.version == __version__
    assert response.pid == os.getpid()
    assert response.host == "localhost"
    assert response.port == 9876
    assert response.started_at == "2026-06-27T10:00:00+00:00"
    assert response.workspace_count == 7


@pytest.mark.asyncio
async def test_daemon_status_uses_current_time_when_started_at_missing() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store = _FakeSessionStoreWithCount(total=0)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(engine=engine)),
        url=SimpleNamespace(hostname="127.0.0.1", port=8765),
    )

    before = datetime.now(UTC).replace(microsecond=0)
    response = await get_daemon_status(request, auth="test")
    after = datetime.now(UTC).replace(microsecond=0)

    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", response.started_at)
    parsed = datetime.fromisoformat(response.started_at)
    assert parsed.tzinfo is not None
    assert before <= parsed <= after


@pytest.mark.asyncio
async def test_daemon_status_workspace_count_zero_when_session_store_missing() -> None:
    engine = _FakeEngine(exists=True)
    del engine.session_store
    request = _fake_status_request(engine)

    response = await get_daemon_status(request, auth="test")

    assert response.status == "running"
    assert response.workspace_count == 0


@pytest.mark.asyncio
async def test_workbench_capabilities_returns_expected_values() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_workbench_capabilities(_fake_request(engine), auth="test")

    assert response.supports_daemon_management is False
    assert response.supports_workspace_registry is False
    assert response.supports_validation_runner is True
    assert response.supports_cloud_sync is False
    assert response.supported_locales == ["zh-CN", "en-US"]
    assert response.protocol_version == 1


@pytest.mark.asyncio
async def test_run_validation_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = ValidationRunCreate(task_id="task-1", argv=["pytest"])

    with pytest.raises(HTTPException) as exc:
        await create_validation_run("missing", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_run_validation_endpoint_returns_result() -> None:
    engine = _FakeEngine(exists=True)
    body = ValidationRunCreate(
        task_id="task-1",
        actor="Human",
        argv=["pytest", "test.py"],
        cwd="/workspace",
    )

    response = await create_validation_run("sess-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.run_validations == [
        {
            "session_id": "sess-1",
            "task_id": "task-1",
            "actor": "Human",
            "argv": ["pytest", "test.py"],
            "cwd": "/workspace",
        }
    ]
    assert response.model_dump() == {
        "id": "run-1",
        "status": "passed",
        "exit_code": 0,
        "output": "ok",
    }


@pytest.mark.asyncio
async def test_run_validation_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_run_validation_error(
        ValueError("验证命令不在允许列表：rm -rf /")
    )
    body = ValidationRunCreate(task_id="task-1", argv=["rm", "-rf", "/"])

    with pytest.raises(HTTPException) as exc:
        await create_validation_run("sess-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 400
    assert exc.value.detail == "验证命令不在允许列表：rm -rf /"


@pytest.mark.asyncio
async def test_run_validation_endpoint_maps_runtime_error_to_503() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_run_validation_error(RuntimeError("ValidationRunner 未配置"))
    body = ValidationRunCreate(task_id="task-1", argv=["pytest"])

    with pytest.raises(HTTPException) as exc:
        await create_validation_run("sess-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 503
    assert exc.value.detail == "ValidationRunner 未配置"


@pytest.mark.asyncio
async def test_create_intent_lock_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = IntentLockCreate(rule="禁止修改 core 模块")

    with pytest.raises(HTTPException) as exc:
        await create_intent_lock("missing", "mission-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_create_intent_lock_endpoint_returns_created_lock() -> None:
    engine = _FakeEngine(exists=True)
    body = IntentLockCreate(
        actor="Planner-Agent",
        rule="禁止修改 src/secret 下文件",
        blocked_paths=["src/secret"],
        allowed_paths=["src/secret/README.md"],
        require_proposal_for_risk=RiskLevel.HIGH,
    )

    response = await create_intent_lock(
        "sess-1", "mission-1", body, _fake_request(engine), auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.created_intent_locks == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "actor": "Planner-Agent",
            "rule": "禁止修改 src/secret 下文件",
            "blocked_paths": ["src/secret"],
            "allowed_paths": ["src/secret/README.md"],
            "require_proposal_for_risk": RiskLevel.HIGH,
        }
    ]
    assert response["id"] == "lock-1"
    assert response["session_id"] == "sess-1"
    assert response["mission_id"] == "mission-1"
    assert response["rule"] == "禁止修改 src/secret 下文件"
    assert response["blocked_paths"] == ["src/secret"]
    assert response["allowed_paths"] == ["src/secret/README.md"]
    assert response["require_proposal_for_risk"] == "high"
    assert response["active"] is True


@pytest.mark.asyncio
async def test_create_intent_lock_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_intent_lock_error(ValueError("意图锁规则不能为空"))
    body = IntentLockCreate(rule="   ")

    with pytest.raises(HTTPException) as exc:
        await create_intent_lock("sess-1", "mission-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 400
    assert exc.value.detail == "意图锁规则不能为空"


@pytest.mark.asyncio
async def test_create_decision_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = DecisionCreate(title="采用 FastAPI", content="使用 FastAPI 承载 Workbench API")

    with pytest.raises(HTTPException) as exc:
        await create_decision("missing", "mission-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_create_decision_endpoint_returns_created_decision() -> None:
    engine = _FakeEngine(exists=True)
    body = DecisionCreate(
        actor="Planner-Agent",
        kind=DecisionKind.ARCHITECTURE,
        title="采用 FastAPI",
        content="使用 FastAPI 承载 Workbench API",
    )

    response = await create_decision(
        "sess-1", "mission-1", body, _fake_request(engine), auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.created_decisions == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "actor": "Planner-Agent",
            "kind": DecisionKind.ARCHITECTURE,
            "title": "采用 FastAPI",
            "content": "使用 FastAPI 承载 Workbench API",
        }
    ]
    assert response["id"] == "decision-1"
    assert response["session_id"] == "sess-1"
    assert response["mission_id"] == "mission-1"
    assert response["kind"] == "architecture"
    assert response["title"] == "采用 FastAPI"
    assert response["content"] == "使用 FastAPI 承载 Workbench API"
    assert response["actor"] == "Planner-Agent"


@pytest.mark.asyncio
async def test_create_decision_endpoint_uses_default_actor_and_kind() -> None:
    engine = _FakeEngine(exists=True)
    body = DecisionCreate(
        title="默认治理决策",
        content="未显式传 actor/kind 时使用产品默认值",
    )

    response = await create_decision(
        "sess-1", "mission-1", body, _fake_request(engine), auth="test"
    )

    assert engine.workbench_service.created_decisions == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "actor": "Human",
            "kind": DecisionKind.ARCHITECTURE,
            "title": "默认治理决策",
            "content": "未显式传 actor/kind 时使用产品默认值",
        }
    ]
    assert response["actor"] == "Human"
    assert response["kind"] == "architecture"


@pytest.mark.asyncio
async def test_create_decision_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_decision_error(ValueError("决策标题不能为空"))
    body = DecisionCreate(title="   ", content="内容")

    with pytest.raises(HTTPException) as exc:
        await create_decision("sess-1", "mission-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 400
    assert exc.value.detail == "决策标题不能为空"
