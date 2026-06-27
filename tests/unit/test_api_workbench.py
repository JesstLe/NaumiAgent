"""Unit tests for workbench API routes."""

from __future__ import annotations

import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from naumi_agent import __version__
from naumi_agent.api.routes.workbench import (
    AgentProfileUpsert,
    ApprovalResolve,
    ClaimIssue,
    ContextHealthRecord,
    DecisionCreate,
    IntentLockCreate,
    IssueAttach,
    MissionCreate,
    ValidationRunCreate,
    attach_workbench_issue,
    claim_workbench_issue,
    create_context_health_snapshot,
    create_decision,
    create_intent_lock,
    create_validation_run,
    create_workbench_mission,
    expire_workbench_leases,
    get_agent_profiles,
    get_approvals,
    get_context_snapshots,
    get_daemon_status,
    get_decisions,
    get_failures,
    get_intent_locks,
    get_issues,
    get_leases,
    get_missions,
    get_validation_runs,
    get_workbench_bootstrap,
    get_workbench_capabilities,
    get_workbench_events,
    get_workbench_snapshot,
    release_workbench_lease,
    resolve_approval,
    upsert_agent_profile,
    websocket_workbench_events,
)
from naumi_agent.api.routes.workbench import (
    router as workbench_router,
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
        self.created_issues: list[dict] = []
        self.attached_issues: list[dict] = []
        self.registered_agent_profiles: list[dict] = []
        self.created_intent_locks: list[dict] = []
        self.created_decisions: list[dict] = []
        self.recorded_context_health: list[dict] = []
        self.resolved_approvals: list[dict] = []
        self.run_validations: list[dict] = []
        self.listed_events: list[dict] = []
        self.listed_validation_runs: list[dict] = []
        self.listed_context_snapshots: list[dict] = []
        self.listed_approvals: list[dict] = []
        self.listed_agent_profiles: list[dict] = []
        self.listed_failures: list[dict] = []
        self.listed_issues: list[dict] = []
        self.listed_missions: list[dict] = []
        self.listed_leases: list[dict] = []
        self.listed_intent_locks: list[dict] = []
        self.listed_decisions: list[dict] = []
        self._run_validation_error: Exception | None = None
        self._intent_lock_error: Exception | None = None
        self._decision_error: Exception | None = None
        self._agent_profile_error: Exception | None = None
        self._context_health_error: Exception | None = None
        self._resolve_approval_error: Exception | None = None
        self._resolve_approval_result: dict | None = None

    def set_run_validation_error(self, error: Exception) -> None:
        self._run_validation_error = error

    def set_intent_lock_error(self, error: Exception) -> None:
        self._intent_lock_error = error

    def set_decision_error(self, error: Exception) -> None:
        self._decision_error = error

    def set_agent_profile_error(self, error: Exception) -> None:
        self._agent_profile_error = error

    def set_context_health_error(self, error: Exception) -> None:
        self._context_health_error = error

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

    async def create_issue(
        self,
        *,
        session_id: str,
        mission_id: str,
        title: str,
        description: str = "",
        blocked_by: list[str] | None = None,
        acceptance_criteria: list[str],
        parallel_mode: ParallelMode = ParallelMode.EXCLUSIVE,
        risk_level: RiskLevel = RiskLevel.MEDIUM,
    ):
        self.created_issues.append(
            {
                "session_id": session_id,
                "mission_id": mission_id,
                "title": title,
                "description": description,
                "blocked_by": list(blocked_by or []),
                "acceptance_criteria": acceptance_criteria,
                "parallel_mode": parallel_mode,
                "risk_level": risk_level,
            }
        )
        return {
            "session_id": session_id,
            "task_id": "task-9",
            "mission_id": mission_id,
            "parallel_mode": parallel_mode.value,
            "risk_level": risk_level.value,
            "requires_human_approval": True,
            "acceptance_criteria": list(acceptance_criteria),
            "expected_artifacts": [],
            "related_branch": "",
            "related_worktree": "",
            "related_pr": "",
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
        }

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

    async def register_agent_profile(
        self,
        *,
        session_id: str,
        agent_id: str,
        name: str,
        role: str,
        capabilities: list[str] | None = None,
        permissions: list[str] | None = None,
        max_parallel_tasks: int = 1,
        status: str = "idle",
        actor: str = "Human",
    ):
        self.registered_agent_profiles.append(
            {
                "session_id": session_id,
                "agent_id": agent_id,
                "name": name,
                "role": role,
                "capabilities": capabilities,
                "permissions": permissions,
                "max_parallel_tasks": max_parallel_tasks,
                "status": status,
                "actor": actor,
            }
        )
        if self._agent_profile_error is not None:
            raise self._agent_profile_error
        return {
            "id": agent_id,
            "session_id": session_id,
            "name": name.strip(),
            "role": role.strip(),
            "capabilities": list(capabilities or []),
            "permissions": list(permissions or []),
            "max_parallel_tasks": max_parallel_tasks,
            "status": status,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
        }

    async def list_agent_profiles(
        self,
        session_id: str,
        status: str | None = None,
        limit: int = 50,
    ):
        self.listed_agent_profiles.append(
            {"session_id": session_id, "status": status, "limit": limit}
        )
        return {
            "agent_profiles": [
                {
                    "id": "agent-1",
                    "session_id": session_id,
                    "name": "Backend Agent",
                    "role": "coder",
                    "capabilities": ["code", "test"],
                    "permissions": ["read", "write"],
                    "max_parallel_tasks": 2,
                    "status": status or "busy",
                    "created_at": "2024-01-01T00:00:00",
                    "updated_at": "2024-01-01T00:00:00",
                }
            ],
            "status": status,
            "limit": limit,
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

    async def list_events(
        self,
        session_id: str,
        event_type: str | None = None,
        subject_id: str | None = None,
        actor: str | None = None,
        limit: int = 50,
    ):
        self.listed_events.append(
            {
                "session_id": session_id,
                "event_type": event_type,
                "subject_id": subject_id,
                "actor": actor,
                "limit": limit,
            }
        )
        return {
            "events": [
                {
                    "id": "evt-1",
                    "session_id": session_id,
                    "type": event_type or "mission.created",
                    "actor": actor or "Human",
                    "subject_id": subject_id or "mission-1",
                    "payload": {"title": "Mac 工作台"},
                    "timestamp": "2024-01-01T00:00:00",
                }
            ],
            "event_type": event_type,
            "subject_id": subject_id,
            "actor": actor,
            "limit": limit,
        }

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

    async def record_context_health(
        self,
        *,
        session_id: str,
        task_id: str,
        agent_id: str,
        minutes_since_sync: int,
        token_load_ratio: float,
        policy_conflict: bool = False,
        actor: str = "Human",
    ):
        self.recorded_context_health.append(
            {
                "session_id": session_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "minutes_since_sync": minutes_since_sync,
                "token_load_ratio": token_load_ratio,
                "policy_conflict": policy_conflict,
                "actor": actor,
            }
        )
        if self._context_health_error is not None:
            raise self._context_health_error
        return {
            "id": "snap-1",
            "session_id": session_id,
            "agent_id": agent_id.strip(),
            "task_id": task_id,
            "health": "stale",
            "reasons": ["超过 60 分钟未同步上下文"],
            "created_at": "2024-01-01T00:00:00",
        }

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

    async def list_failures(
        self,
        session_id: str,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ):
        self.listed_failures.append(
            {
                "session_id": session_id,
                "task_id": task_id,
                "status": status,
                "limit": limit,
            }
        )
        return [
            {
                "id": "failure-1",
                "session_id": session_id,
                "task_id": task_id or "task-1",
                "kind": "test_failed",
                "title": "测试失败",
                "detail": "详情",
                "source_id": "run-1",
                "status": status or "open",
                "created_at": "2024-01-01T00:00:00",
            }
        ]

    async def list_issues(
        self,
        session_id: str,
        mission_id: str | None = None,
        risk_level: str | None = None,
        limit: int = 50,
    ):
        self.listed_issues.append(
            {
                "session_id": session_id,
                "mission_id": mission_id,
                "risk_level": risk_level,
                "limit": limit,
            }
        )
        return {
            "issues": [
                {
                    "session_id": session_id,
                    "task_id": "task-1",
                    "mission_id": mission_id or "mission-1",
                    "parallel_mode": "exclusive",
                    "risk_level": risk_level or "medium",
                    "requires_human_approval": True,
                    "acceptance_criteria": [],
                    "expected_artifacts": [],
                    "related_branch": "",
                    "related_worktree": "",
                    "related_pr": "",
                    "created_at": "2024-01-01T00:00:00",
                    "updated_at": "2024-01-01T00:00:00",
                }
            ],
            "mission_id": mission_id,
            "risk_level": risk_level,
            "limit": limit,
        }

    async def list_leases(
        self,
        session_id: str,
        state: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ):
        self.listed_leases.append(
            {
                "session_id": session_id,
                "state": state,
                "task_id": task_id,
                "agent_id": agent_id,
                "limit": limit,
            }
        )
        return {
            "leases": [
                {
                    "id": "lease-1",
                    "session_id": session_id,
                    "task_id": task_id or "task-1",
                    "agent_id": agent_id or "agent-1",
                    "state": state or "active",
                    "expires_at": "2024-01-01T01:00:00",
                    "worktree_name": "wt-1",
                    "created_at": "2024-01-01T00:00:00",
                    "updated_at": "2024-01-01T00:00:00",
                }
            ],
            "state": state,
            "task_id": task_id,
            "agent_id": agent_id,
            "limit": limit,
        }

    async def list_missions(
        self,
        session_id: str,
        status: str | None = None,
        limit: int = 50,
    ):
        self.listed_missions.append(
            {
                "session_id": session_id,
                "status": status,
                "limit": limit,
            }
        )
        return {
            "missions": [
                {
                    "id": "mission-1",
                    "session_id": session_id,
                    "title": "Mac 工作台",
                    "goal": "补齐 API 调用面",
                    "status": status or "planning",
                    "created_at": "2024-01-01T00:00:00",
                    "updated_at": "2024-01-01T00:00:00",
                }
            ],
            "status": status,
            "limit": limit,
        }

    async def list_intent_locks(self, session_id: str, mission_id: str):
        self.listed_intent_locks.append(
            {"session_id": session_id, "mission_id": mission_id}
        )
        return [
            {
                "id": "lock-1",
                "session_id": session_id,
                "mission_id": mission_id,
                "rule": "禁止修改 core 模块",
                "blocked_paths": ["src/secret"],
                "allowed_paths": ["src/secret/README.md"],
                "require_proposal_for_risk": "high",
                "active": True,
                "created_at": "2024-01-01T00:00:00",
            }
        ]

    async def list_decisions(self, session_id: str, mission_id: str):
        self.listed_decisions.append(
            {"session_id": session_id, "mission_id": mission_id}
        )
        return [
            {
                "id": "decision-1",
                "session_id": session_id,
                "mission_id": mission_id,
                "kind": "architecture",
                "title": "采用 FastAPI",
                "content": "使用 FastAPI 承载 Workbench API",
                "actor": "Planner-Agent",
                "created_at": "2024-01-01T00:00:00",
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

    async def release(self, session_id: str, lease_id: str) -> Lease | None:
        self.released.append({"session_id": session_id, "lease_id": lease_id})
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


class _RecordingWorkbenchWebSocket:
    def __init__(self, engine: _FakeEngine) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(engine=engine))
        self.accepted = False
        self.closed = False
        self.sent_json: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.sent_json.append(payload)

    async def close(self) -> None:
        self.closed = True

    async def receive_json(self) -> dict:
        raise WebSocketDisconnect()


class _FakeSessionStoreWithCount:
    def __init__(self, total: int) -> None:
        self.total = total

    async def list_sessions(
        self, page: int = 1, page_size: int = 20, query: str = ""
    ) -> tuple[list, int]:
        return ([], self.total)


class _FakeSessionStoreWithLatest:
    def __init__(self, sessions: list[SimpleNamespace]) -> None:
        self.sessions = sessions
        self.loaded: list[str] = []

    async def load(self, session_id: str):
        self.loaded.append(session_id)
        for session in self.sessions:
            if session.id == session_id:
                return session
        return None

    async def list_sessions(
        self, page: int = 1, page_size: int = 20, query: str = ""
    ) -> tuple[list[SimpleNamespace], int]:
        return (self.sessions[:page_size], len(self.sessions))


def _fake_status_request(
    engine: _FakeEngine,
    started_at: str = "2026-06-27T10:00:00+00:00",
    hostname: str | None = "127.0.0.1",
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

    response = await get_workbench_events(
        "sess-1", _fake_request(engine), limit=25, auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_events == [
        {
            "session_id": "sess-1",
            "event_type": None,
            "subject_id": None,
            "actor": None,
            "limit": 25,
        }
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
        "event_type": None,
        "subject_id": None,
        "actor": None,
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_get_events_endpoint_forwards_filters_and_returns_them() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_workbench_events(
        "sess-1",
        _fake_request(engine),
        limit=25,
        event_type="issue.created",
        subject_id="task-2",
        actor="Planner-Agent",
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_events == [
        {
            "session_id": "sess-1",
            "event_type": "issue.created",
            "subject_id": "task-2",
            "actor": "Planner-Agent",
            "limit": 25,
        }
    ]
    assert response.model_dump() == {
        "events": [
            {
                "id": "evt-1",
                "session_id": "sess-1",
                "type": "issue.created",
                "actor": "Planner-Agent",
                "subject_id": "task-2",
                "payload": {"title": "Mac 工作台"},
                "timestamp": "2024-01-01T00:00:00",
            }
        ],
        "event_type": "issue.created",
        "subject_id": "task-2",
        "actor": "Planner-Agent",
        "limit": 25,
    }


def test_get_events_route_accepts_type_query_alias() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/events",
        params={
            "type": "issue.created",
            "subject_id": "task-2",
            "actor": "Planner-Agent",
            "limit": "7",
        },
    )

    assert response.status_code == 200
    assert engine.workbench_service.listed_events == [
        {
            "session_id": "sess-1",
            "event_type": "issue.created",
            "subject_id": "task-2",
            "actor": "Planner-Agent",
            "limit": 7,
        }
    ]
    assert response.json()["event_type"] == "issue.created"


def test_workbench_event_stream_rejects_missing_session() -> None:
    engine = _FakeEngine(exists=False)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    with client.websocket_connect("/workbench/sessions/missing/events/stream") as websocket:
        assert websocket.receive_json() == {
            "type": "error",
            "message": "Session not found",
        }
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_text()


def test_workbench_event_stream_refreshes_audit_events() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    with client.websocket_connect("/workbench/sessions/sess-1/events/stream") as websocket:
        assert websocket.receive_json() == {"type": "connected", "session_id": "sess-1"}
        assert websocket.receive_json()["type"] == "workbench.event"
        assert websocket.receive_json() == {"type": "refresh_complete", "count": 1}
        websocket.send_json(
            {
                "type": "refresh",
                "limit": 7,
                "event_type": "issue.created",
                "subject_id": "task-2",
                "actor": "Planner-Agent",
            }
        )

        event_message = websocket.receive_json()
        complete_message = websocket.receive_json()

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_events == [
        {
            "session_id": "sess-1",
            "event_type": None,
            "subject_id": None,
            "actor": None,
            "limit": 50,
        },
        {
            "session_id": "sess-1",
            "event_type": "issue.created",
            "subject_id": "task-2",
            "actor": "Planner-Agent",
            "limit": 7,
        }
    ]
    assert event_message == {
        "type": "workbench.event",
        "event": {
            "id": "evt-1",
            "session_id": "sess-1",
            "type": "issue.created",
            "actor": "Planner-Agent",
            "subject_id": "task-2",
            "payload": {"title": "Mac 工作台"},
            "timestamp": "2024-01-01T00:00:00",
        },
    }
    assert complete_message == {"type": "refresh_complete", "count": 1}


@pytest.mark.asyncio
async def test_workbench_event_stream_sends_initial_audit_events_on_connect() -> None:
    engine = _FakeEngine(exists=True)
    websocket = _RecordingWorkbenchWebSocket(engine)

    await websocket_workbench_events(websocket, "sess-1")

    assert websocket.accepted is True
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_events == [
        {
            "session_id": "sess-1",
            "event_type": None,
            "subject_id": None,
            "actor": None,
            "limit": 50,
        }
    ]
    assert websocket.sent_json == [
        {"type": "connected", "session_id": "sess-1"},
        {
            "type": "workbench.event",
            "event": {
                "id": "evt-1",
                "session_id": "sess-1",
                "type": "mission.created",
                "actor": "Human",
                "subject_id": "mission-1",
                "payload": {"title": "Mac 工作台"},
                "timestamp": "2024-01-01T00:00:00",
            },
        },
        {"type": "refresh_complete", "count": 1},
    ]


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
async def test_create_context_health_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = ContextHealthRecord(
        agent_id="Agent-A",
        minutes_since_sync=75,
        token_load_ratio=0.2,
    )

    with pytest.raises(HTTPException) as exc:
        await create_context_health_snapshot(
            "missing", "task-1", body, _fake_request(engine), auth="test"
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_create_context_health_endpoint_records_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    body = ContextHealthRecord(
        agent_id=" Agent-A ",
        minutes_since_sync=75,
        token_load_ratio=0.2,
        policy_conflict=False,
        actor="Human",
    )

    response = await create_context_health_snapshot(
        "sess-1", "task-2", body, _fake_request(engine), auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.recorded_context_health == [
        {
            "session_id": "sess-1",
            "task_id": "task-2",
            "agent_id": " Agent-A ",
            "minutes_since_sync": 75,
            "token_load_ratio": 0.2,
            "policy_conflict": False,
            "actor": "Human",
        }
    ]
    assert response == {
        "id": "snap-1",
        "session_id": "sess-1",
        "agent_id": "Agent-A",
        "task_id": "task-2",
        "health": "stale",
        "reasons": ["超过 60 分钟未同步上下文"],
        "created_at": "2024-01-01T00:00:00",
    }


@pytest.mark.asyncio
async def test_create_context_health_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_context_health_error(
        ValueError("issue 不存在，无法同步上下文健康度")
    )
    body = ContextHealthRecord(
        agent_id="Agent-A",
        minutes_since_sync=75,
        token_load_ratio=0.2,
    )

    with pytest.raises(HTTPException) as exc:
        await create_context_health_snapshot(
            "sess-1", "task-2", body, _fake_request(engine), auth="test"
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "issue 不存在，无法同步上下文健康度"


def test_create_context_health_route_accepts_json_body() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/issues/task-2/context-health",
        json={
            "agent_id": "Agent-A",
            "minutes_since_sync": 75,
            "token_load_ratio": 0.2,
            "policy_conflict": False,
            "actor": "Human",
        },
    )

    assert response.status_code == 201
    assert engine.workbench_service.recorded_context_health == [
        {
            "session_id": "sess-1",
            "task_id": "task-2",
            "agent_id": "Agent-A",
            "minutes_since_sync": 75,
            "token_load_ratio": 0.2,
            "policy_conflict": False,
            "actor": "Human",
        }
    ]
    assert response.json()["health"] == "stale"


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
async def test_get_missions_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_missions("missing", _fake_request(engine), status=None, limit=10, auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_missions_endpoint_returns_missions_and_params() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_missions(
        "sess-1", _fake_request(engine), status="active", limit=25, auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_missions == [
        {"session_id": "sess-1", "status": "active", "limit": 25}
    ]
    assert response.model_dump() == {
        "missions": [
            {
                "id": "mission-1",
                "session_id": "sess-1",
                "title": "Mac 工作台",
                "goal": "补齐 API 调用面",
                "status": "active",
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
        ],
        "status": "active",
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_get_missions_endpoint_without_status_filter() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_missions(
        "sess-1", _fake_request(engine), status=None, limit=50, auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_missions == [
        {"session_id": "sess-1", "status": None, "limit": 50}
    ]
    assert response.model_dump()["status"] is None
    assert response.model_dump()["limit"] == 50
    assert len(response.model_dump()["missions"]) == 1


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
async def test_get_failures_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_failures(
            "missing",
            _fake_request(engine),
            task_id=None,
            status=None,
            limit=10,
            auth="test",
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_failures_endpoint_returns_failures_and_params() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_failures(
        "sess-1",
        _fake_request(engine),
        task_id="task-2",
        status="resolved",
        limit=25,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_failures == [
        {"session_id": "sess-1", "task_id": "task-2", "status": "resolved", "limit": 25}
    ]
    assert response.model_dump() == {
        "failures": [
            {
                "id": "failure-1",
                "session_id": "sess-1",
                "task_id": "task-2",
                "kind": "test_failed",
                "title": "测试失败",
                "detail": "详情",
                "source_id": "run-1",
                "status": "resolved",
                "created_at": "2024-01-01T00:00:00",
            }
        ],
        "task_id": "task-2",
        "status": "resolved",
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_get_failures_endpoint_without_filters() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_failures(
        "sess-1",
        _fake_request(engine),
        task_id=None,
        status=None,
        limit=50,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_failures == [
        {"session_id": "sess-1", "task_id": None, "status": None, "limit": 50}
    ]
    assert response.model_dump()["task_id"] is None
    assert response.model_dump()["status"] is None
    assert response.model_dump()["limit"] == 50
    assert len(response.model_dump()["failures"]) == 1


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
    assert market.released == [
        {"session_id": "sess-1", "lease_id": "lease-missing"}
    ]
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

    assert market.released == [{"session_id": "sess-1", "lease_id": "lease-2"}]
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
    assert response.host == "127.0.0.1"
    assert response.port == 9876
    assert response.started_at == "2026-06-27T10:00:00+00:00"
    assert response.workspace_count == 7


@pytest.mark.asyncio
async def test_daemon_status_normalizes_missing_host_to_loopback() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store = _FakeSessionStoreWithCount(total=0)
    request = _fake_status_request(engine, hostname=None, port=8765)

    response = await get_daemon_status(request, auth="test")

    assert response.host == "127.0.0.1"
    assert response.port == 8765


@pytest.mark.asyncio
async def test_daemon_status_does_not_echo_request_host() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store = _FakeSessionStoreWithCount(total=0)
    request = _fake_status_request(engine, hostname="example.test", port=8765)

    response = await get_daemon_status(request, auth="test")

    assert response.host == "127.0.0.1"


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
async def test_workbench_bootstrap_returns_latest_session_and_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    latest_session = SimpleNamespace(
        id="sess-latest",
        title="Mac 工作台",
        model="gpt-5",
        created_at=datetime(2026, 6, 27, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 6, 27, 9, 0, tzinfo=UTC),
        messages=[{"role": "user"}, {"role": "assistant"}],
        total_tokens=128,
        total_cost_usd=0.012,
        status="active",
    )
    engine.session_store = _FakeSessionStoreWithLatest([latest_session])
    request = _fake_status_request(engine)

    response = await get_workbench_bootstrap(request, auth="test")

    assert response.selected_session_id == "sess-latest"
    assert response.total_sessions == 1
    assert response.sessions[0]["id"] == "sess-latest"
    assert response.sessions[0]["message_count"] == 2
    assert response.snapshot is not None
    assert response.snapshot["session_id"] == "sess-latest"
    assert response.daemon_status.host == "127.0.0.1"
    assert response.capabilities.protocol_version == 1
    assert engine.session_store.loaded == ["sess-latest"]


@pytest.mark.asyncio
async def test_workbench_bootstrap_keeps_daemon_ready_when_no_sessions_exist() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store = _FakeSessionStoreWithLatest([])
    request = _fake_status_request(engine)

    response = await get_workbench_bootstrap(request, auth="test")

    assert response.selected_session_id is None
    assert response.sessions == []
    assert response.total_sessions == 0
    assert response.snapshot is None
    assert response.daemon_status.status == "running"
    assert response.capabilities.supported_locales == ["zh-CN", "en-US"]


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


@pytest.mark.asyncio
async def test_get_issues_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_issues(
            "missing",
            _fake_request(engine),
            mission_id=None,
            risk_level=None,
            limit=10,
            auth="test",
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_issues_endpoint_returns_issues_and_params() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_issues(
        "sess-1",
        _fake_request(engine),
        mission_id="mission-2",
        risk_level="high",
        limit=25,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_issues == [
        {"session_id": "sess-1", "mission_id": "mission-2", "risk_level": "high", "limit": 25}
    ]
    assert response.model_dump() == {
        "issues": [
            {
                "session_id": "sess-1",
                "task_id": "task-1",
                "mission_id": "mission-2",
                "parallel_mode": "exclusive",
                "risk_level": "high",
                "requires_human_approval": True,
                "acceptance_criteria": [],
                "expected_artifacts": [],
                "related_branch": "",
                "related_worktree": "",
                "related_pr": "",
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
        ],
        "mission_id": "mission-2",
        "risk_level": "high",
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_get_issues_endpoint_without_filters() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_issues(
        "sess-1",
        _fake_request(engine),
        mission_id=None,
        risk_level=None,
        limit=50,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_issues == [
        {"session_id": "sess-1", "mission_id": None, "risk_level": None, "limit": 50}
    ]
    assert response.model_dump()["mission_id"] is None
    assert response.model_dump()["risk_level"] is None
    assert response.model_dump()["limit"] == 50
    assert len(response.model_dump()["issues"]) == 1


@pytest.mark.asyncio
async def test_get_agent_profiles_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_agent_profiles(
            "missing", _fake_request(engine), status=None, limit=10, auth="test"
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_agent_profiles_endpoint_returns_profiles_and_params() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_agent_profiles(
        "sess-1", _fake_request(engine), status="busy", limit=25, auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_agent_profiles == [
        {"session_id": "sess-1", "status": "busy", "limit": 25}
    ]
    assert response.model_dump() == {
        "agent_profiles": [
            {
                "id": "agent-1",
                "session_id": "sess-1",
                "name": "Backend Agent",
                "role": "coder",
                "capabilities": ["code", "test"],
                "permissions": ["read", "write"],
                "max_parallel_tasks": 2,
                "status": "busy",
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
        ],
        "status": "busy",
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_upsert_agent_profile_endpoint_registers_profile() -> None:
    engine = _FakeEngine(exists=True)
    body = AgentProfileUpsert(
        name=" Backend Agent ",
        role=" coder ",
        capabilities=["code", "test"],
        permissions=["read"],
        max_parallel_tasks=2,
        status="busy",
        actor="Human",
    )

    response = await upsert_agent_profile(
        "sess-1", "agent-1", body, _fake_request(engine), auth="test"
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.registered_agent_profiles == [
        {
            "session_id": "sess-1",
            "agent_id": "agent-1",
            "name": " Backend Agent ",
            "role": " coder ",
            "capabilities": ["code", "test"],
            "permissions": ["read"],
            "max_parallel_tasks": 2,
            "status": "busy",
            "actor": "Human",
        }
    ]
    assert response["id"] == "agent-1"
    assert response["name"] == "Backend Agent"
    assert response["role"] == "coder"


@pytest.mark.asyncio
async def test_upsert_agent_profile_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_agent_profile_error(ValueError("Agent 名称不能为空"))
    body = AgentProfileUpsert(name="", role="coder")

    with pytest.raises(HTTPException) as exc:
        await upsert_agent_profile(
            "sess-1", "agent-1", body, _fake_request(engine), auth="test"
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "Agent 名称不能为空"


def test_upsert_agent_profile_route_accepts_json_body() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/agents/agent-1",
        json={
            "name": "Backend Agent",
            "role": "coder",
            "capabilities": ["code"],
            "permissions": ["read"],
            "max_parallel_tasks": 2,
            "status": "busy",
            "actor": "Human",
        },
    )

    assert response.status_code == 201
    assert engine.workbench_service.registered_agent_profiles == [
        {
            "session_id": "sess-1",
            "agent_id": "agent-1",
            "name": "Backend Agent",
            "role": "coder",
            "capabilities": ["code"],
            "permissions": ["read"],
            "max_parallel_tasks": 2,
            "status": "busy",
            "actor": "Human",
        }
    ]
    assert response.json()["status"] == "busy"


def test_create_issue_route_accepts_json_body_without_existing_task_id() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/missions/mission-1/issues",
        json={
            "title": "实现 Issue 创建 API",
            "description": "创建 backing task 并绑定 workbench metadata",
            "blocked_by": ["1"],
            "acceptance_criteria": ["dashboard 刷新后可见", "可被 Agent claim"],
            "parallel_mode": "cooperative",
            "risk_level": "high",
        },
    )

    assert response.status_code == 201
    assert engine.workbench_service.created_issues == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "title": "实现 Issue 创建 API",
            "description": "创建 backing task 并绑定 workbench metadata",
            "blocked_by": ["1"],
            "acceptance_criteria": ["dashboard 刷新后可见", "可被 Agent claim"],
            "parallel_mode": ParallelMode.COOPERATIVE,
            "risk_level": RiskLevel.HIGH,
        }
    ]
    assert response.json()["task_id"] == "task-9"
    assert response.json()["parallel_mode"] == "cooperative"
    assert response.json()["risk_level"] == "high"


@pytest.mark.asyncio
async def test_get_leases_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_leases(
            "missing",
            _fake_request(engine),
            state=None,
            task_id=None,
            agent_id=None,
            limit=10,
            auth="test",
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_leases_endpoint_returns_leases_and_params() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_leases(
        "sess-1",
        _fake_request(engine),
        state="active",
        task_id="task-2",
        agent_id="agent-2",
        limit=25,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_leases == [
        {
            "session_id": "sess-1",
            "state": "active",
            "task_id": "task-2",
            "agent_id": "agent-2",
            "limit": 25,
        }
    ]
    assert response.model_dump() == {
        "leases": [
            {
                "id": "lease-1",
                "session_id": "sess-1",
                "task_id": "task-2",
                "agent_id": "agent-2",
                "state": "active",
                "expires_at": "2024-01-01T01:00:00",
                "worktree_name": "wt-1",
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
        ],
        "state": "active",
        "task_id": "task-2",
        "agent_id": "agent-2",
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_get_leases_endpoint_without_filters() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_leases(
        "sess-1",
        _fake_request(engine),
        state=None,
        task_id=None,
        agent_id=None,
        limit=50,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_leases == [
        {
            "session_id": "sess-1",
            "state": None,
            "task_id": None,
            "agent_id": None,
            "limit": 50,
        }
    ]
    assert response.model_dump()["state"] is None
    assert response.model_dump()["task_id"] is None
    assert response.model_dump()["agent_id"] is None
    assert response.model_dump()["limit"] == 50
    assert len(response.model_dump()["leases"]) == 1


@pytest.mark.asyncio
async def test_get_intent_locks_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_intent_locks("missing", "mission-1", _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_intent_locks_endpoint_returns_locks_and_mission_id() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_intent_locks("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_intent_locks == [
        {"session_id": "sess-1", "mission_id": "mission-2"}
    ]
    assert response.model_dump() == {
        "intent_locks": [
            {
                "id": "lock-1",
                "session_id": "sess-1",
                "mission_id": "mission-2",
                "rule": "禁止修改 core 模块",
                "blocked_paths": ["src/secret"],
                "allowed_paths": ["src/secret/README.md"],
                "require_proposal_for_risk": "high",
                "active": True,
                "created_at": "2024-01-01T00:00:00",
            }
        ],
        "mission_id": "mission-2",
    }


@pytest.mark.asyncio
async def test_get_decisions_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_decisions("missing", "mission-1", _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_decisions_endpoint_returns_decisions_and_mission_id() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_decisions("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_decisions == [
        {"session_id": "sess-1", "mission_id": "mission-2"}
    ]
    assert response.model_dump() == {
        "decisions": [
            {
                "id": "decision-1",
                "session_id": "sess-1",
                "mission_id": "mission-2",
                "kind": "architecture",
                "title": "采用 FastAPI",
                "content": "使用 FastAPI 承载 Workbench API",
                "actor": "Planner-Agent",
                "created_at": "2024-01-01T00:00:00",
            }
        ],
        "mission_id": "mission-2",
    }


def test_get_intent_locks_route_accepts_path_and_returns_array() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/missions/mission-1/intent-locks")

    assert response.status_code == 200
    assert response.json() == {
        "intent_locks": [
            {
                "id": "lock-1",
                "session_id": "sess-1",
                "mission_id": "mission-1",
                "rule": "禁止修改 core 模块",
                "blocked_paths": ["src/secret"],
                "allowed_paths": ["src/secret/README.md"],
                "require_proposal_for_risk": "high",
                "active": True,
                "created_at": "2024-01-01T00:00:00",
            }
        ],
        "mission_id": "mission-1",
    }


def test_get_decisions_route_accepts_path_and_returns_array() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/missions/mission-1/decisions")

    assert response.status_code == 200
    assert response.json() == {
        "decisions": [
            {
                "id": "decision-1",
                "session_id": "sess-1",
                "mission_id": "mission-1",
                "kind": "architecture",
                "title": "采用 FastAPI",
                "content": "使用 FastAPI 承载 Workbench API",
                "actor": "Planner-Agent",
                "created_at": "2024-01-01T00:00:00",
            }
        ],
        "mission_id": "mission-1",
    }
