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

import naumi_agent.api.routes.workbench as workbench_routes
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
    WorkbenchSessionCreate,
    WorktreeKeep,
    attach_workbench_issue,
    claim_workbench_issue,
    create_context_health_snapshot,
    create_decision,
    create_intent_lock,
    create_validation_run,
    create_workbench_mission,
    create_workbench_session,
    delete_worktree,
    expire_workbench_leases,
    get_agent_profile,
    get_agent_profiles,
    get_approval,
    get_approvals,
    get_context_snapshot,
    get_context_snapshots,
    get_daemon_status,
    get_decision,
    get_decisions,
    get_failure,
    get_failures,
    get_intent_lock,
    get_intent_locks,
    get_issue,
    get_issues,
    get_lease,
    get_leases,
    get_mission,
    get_missions,
    get_validation_run,
    get_validation_runs,
    get_workbench_bootstrap,
    get_workbench_capabilities,
    get_workbench_event,
    get_workbench_events,
    get_workbench_snapshot,
    get_worktree,
    get_worktrees,
    keep_worktree,
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
from naumi_agent.worktree.models import WorktreeRecord, WorktreeStatus


class _FakeSessionStore:
    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.load_error: Exception | None = None
        self.list_sessions_error: Exception | None = None
        self.create_session_error: Exception | None = None
        self.created_sessions: list[dict[str, str | None]] = []

    async def load(self, session_id: str):
        if self.load_error is not None:
            raise self.load_error
        if not self.exists:
            return None
        return SimpleNamespace(id=session_id)

    async def create_session(
        self,
        title: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
    ):
        if self.create_session_error is not None:
            raise self.create_session_error
        self.exists = True
        self.created_sessions.append(
            {"title": title, "model": model, "system_prompt": system_prompt}
        )
        return SimpleNamespace(
            id="sess-created",
            title=title or "Mac 工作台",
            model=model or "kimi-for-coding",
            created_at=datetime(2026, 6, 27, 8, 0, tzinfo=UTC),
            updated_at=datetime(2026, 6, 27, 8, 0, tzinfo=UTC),
            messages=[],
            total_tokens=0,
            total_cost_usd=0.0,
            status="active",
        )

    async def list_sessions(
        self, page: int = 1, page_size: int = 20, query: str = ""
    ) -> tuple[list[SimpleNamespace], int]:
        if self.list_sessions_error is not None:
            raise self.list_sessions_error
        if not self.exists:
            return ([], 0)
        return ([SimpleNamespace(id="sess-1")], 1)


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
        self.requested_events: list[dict] = []
        self.listed_validation_runs: list[dict] = []
        self.requested_validation_runs: list[dict] = []
        self.listed_context_snapshots: list[dict] = []
        self.requested_context_snapshots: list[dict] = []
        self.listed_approvals: list[dict] = []
        self.requested_approvals: list[dict] = []
        self.listed_agent_profiles: list[dict] = []
        self.requested_agent_profiles: list[dict] = []
        self.listed_failures: list[dict] = []
        self.requested_failures: list[dict] = []
        self.listed_issues: list[dict] = []
        self.requested_issues: list[dict] = []
        self.requested_leases: list[dict] = []
        self.requested_missions: list[dict] = []
        self.listed_missions: list[dict] = []
        self.listed_leases: list[dict] = []
        self.listed_intent_locks: list[dict] = []
        self.requested_intent_locks: list[dict] = []
        self.listed_decisions: list[dict] = []
        self.requested_decisions: list[dict] = []
        self._create_mission_error: Exception | None = None
        self._list_missions_error: Exception | None = None
        self._get_mission_error: Exception | None = None
        self._issue_error: Exception | None = None
        self._list_issues_error: Exception | None = None
        self._get_issue_error: Exception | None = None
        self._list_leases_error: Exception | None = None
        self._get_lease_error: Exception | None = None
        self._list_approvals_error: Exception | None = None
        self._get_approval_error: Exception | None = None
        self._run_validation_error: Exception | None = None
        self._intent_lock_error: Exception | None = None
        self._list_intent_locks_error: Exception | None = None
        self._get_intent_lock_error: Exception | None = None
        self._decision_error: Exception | None = None
        self._list_decisions_error: Exception | None = None
        self._get_decision_error: Exception | None = None
        self._agent_profile_error: Exception | None = None
        self._get_agent_profile_error: Exception | None = None
        self._list_failures_error: Exception | None = None
        self._get_failure_error: Exception | None = None
        self._context_health_error: Exception | None = None
        self._list_agent_profiles_error: Exception | None = None
        self._resolve_approval_error: Exception | None = None
        self._resolve_approval_result: dict | None = None
        self._dashboard_snapshot_error: Exception | None = None
        self._list_events_error: Exception | None = None
        self._get_event_error: Exception | None = None
        self._list_validation_runs_error: Exception | None = None
        self._get_validation_run_error: Exception | None = None
        self._list_context_snapshots_error: Exception | None = None
        self._get_context_snapshot_error: Exception | None = None

    def set_run_validation_error(self, error: Exception) -> None:
        self._run_validation_error = error

    def set_intent_lock_error(self, error: Exception) -> None:
        self._intent_lock_error = error

    def set_list_intent_locks_error(self, error: Exception) -> None:
        self._list_intent_locks_error = error

    def set_get_intent_lock_error(self, error: Exception) -> None:
        self._get_intent_lock_error = error

    def set_decision_error(self, error: Exception) -> None:
        self._decision_error = error

    def set_list_decisions_error(self, error: Exception) -> None:
        self._list_decisions_error = error

    def set_get_decision_error(self, error: Exception) -> None:
        self._get_decision_error = error

    def set_agent_profile_error(self, error: Exception) -> None:
        self._agent_profile_error = error

    def set_context_health_error(self, error: Exception) -> None:
        self._context_health_error = error

    def set_resolve_approval_error(self, error: Exception) -> None:
        self._resolve_approval_error = error

    def set_resolve_approval_result(self, result: dict | None) -> None:
        self._resolve_approval_result = result

    def set_dashboard_snapshot_error(self, error: Exception) -> None:
        self._dashboard_snapshot_error = error

    def set_list_events_error(self, error: Exception) -> None:
        self._list_events_error = error

    def set_get_event_error(self, error: Exception) -> None:
        self._get_event_error = error

    def set_list_validation_runs_error(self, error: Exception) -> None:
        self._list_validation_runs_error = error

    def set_get_validation_run_error(self, error: Exception) -> None:
        self._get_validation_run_error = error

    def set_list_context_snapshots_error(self, error: Exception) -> None:
        self._list_context_snapshots_error = error

    def set_get_context_snapshot_error(self, error: Exception) -> None:
        self._get_context_snapshot_error = error

    def set_create_mission_error(self, error: Exception) -> None:
        self._create_mission_error = error

    def set_list_missions_error(self, error: Exception) -> None:
        self._list_missions_error = error

    def set_get_mission_error(self, error: Exception) -> None:
        self._get_mission_error = error

    def set_issue_error(self, error: Exception) -> None:
        self._issue_error = error

    def set_list_issues_error(self, error: Exception) -> None:
        self._list_issues_error = error

    def set_get_issue_error(self, error: Exception) -> None:
        self._get_issue_error = error

    def set_list_leases_error(self, error: Exception) -> None:
        self._list_leases_error = error

    def set_get_lease_error(self, error: Exception) -> None:
        self._get_lease_error = error

    def set_list_failures_error(self, error: Exception) -> None:
        self._list_failures_error = error

    def set_get_failure_error(self, error: Exception) -> None:
        self._get_failure_error = error

    def set_list_agent_profiles_error(self, error: Exception) -> None:
        self._list_agent_profiles_error = error

    def set_get_agent_profile_error(self, error: Exception) -> None:
        self._get_agent_profile_error = error

    def set_list_approvals_error(self, error: Exception) -> None:
        self._list_approvals_error = error

    def set_get_approval_error(self, error: Exception) -> None:
        self._get_approval_error = error

    async def dashboard_snapshot(self, session_id: str):
        if self._dashboard_snapshot_error is not None:
            raise self._dashboard_snapshot_error
        return {
            "version": 1,
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
        if self._create_mission_error is not None:
            raise self._create_mission_error
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
        if self._issue_error is not None:
            raise self._issue_error
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
        if self._issue_error is not None:
            raise self._issue_error
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
        if self._list_agent_profiles_error is not None:
            raise self._list_agent_profiles_error
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

    async def get_agent_profile(self, session_id: str, agent_id: str):
        self.requested_agent_profiles.append(
            {"session_id": session_id, "agent_id": agent_id}
        )
        if self._get_agent_profile_error is not None:
            raise self._get_agent_profile_error
        if agent_id == "missing-agent":
            return None
        return {
            "id": agent_id,
            "session_id": session_id,
            "name": "Backend Agent",
            "role": "coder",
            "capabilities": ["code", "test"],
            "permissions": ["read", "write"],
            "max_parallel_tasks": 2,
            "status": "busy",
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

    async def list_events(
        self,
        session_id: str,
        event_type: str | None = None,
        subject_id: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ):
        if self._list_events_error is not None:
            raise self._list_events_error
        self.listed_events.append(
            {
                "session_id": session_id,
                "event_type": event_type,
                "subject_id": subject_id,
                "actor": actor,
                "since": since,
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
            "since": since,
            "limit": limit,
        }

    async def get_event(self, session_id: str, event_id: str):
        self.requested_events.append(
            {
                "session_id": session_id,
                "event_id": event_id,
            }
        )
        if self._get_event_error is not None:
            raise self._get_event_error
        if event_id == "missing-event":
            return None
        return {
            "id": event_id,
            "session_id": session_id,
            "type": "issue.claimed",
            "actor": "Backend-Agent",
            "subject_id": "task-1",
            "payload": {"lease_id": "lease-1"},
            "timestamp": "2024-01-01T00:00:00",
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
        if self._list_validation_runs_error is not None:
            raise self._list_validation_runs_error
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

    async def get_validation_run(self, session_id: str, run_id: str):
        self.requested_validation_runs.append(
            {"session_id": session_id, "run_id": run_id}
        )
        if self._get_validation_run_error is not None:
            raise self._get_validation_run_error
        if run_id == "missing-run":
            return None
        return {
            "id": run_id,
            "session_id": session_id,
            "task_id": "task-1",
            "actor": "ValidationRunner",
            "command": ["pytest", "test.py"],
            "cwd": "/workspace",
            "status": "passed",
            "exit_code": 0,
            "output": "ok",
            "started_at": "2024-01-01T00:00:00",
            "completed_at": "2024-01-01T00:00:01",
        }

    async def list_context_snapshots(
        self,
        session_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ):
        if self._list_context_snapshots_error is not None:
            raise self._list_context_snapshots_error
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

    async def get_context_snapshot(self, session_id: str, snapshot_id: str):
        self.requested_context_snapshots.append(
            {"session_id": session_id, "snapshot_id": snapshot_id}
        )
        if self._get_context_snapshot_error is not None:
            raise self._get_context_snapshot_error
        if snapshot_id == "missing-snapshot":
            return None
        return {
            "id": snapshot_id,
            "session_id": session_id,
            "agent_id": "agent-1",
            "task_id": "task-1",
            "health": "good",
            "reasons": ["上下文健康"],
            "created_at": "2024-01-01T00:00:00",
        }

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
        if self._list_approvals_error is not None:
            raise self._list_approvals_error
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

    async def get_approval(self, session_id: str, approval_id: str):
        self.requested_approvals.append(
            {"session_id": session_id, "approval_id": approval_id}
        )
        if self._get_approval_error is not None:
            raise self._get_approval_error
        if approval_id == "missing-approval":
            return None
        return {
            "id": approval_id,
            "session_id": session_id,
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

    async def list_failures(
        self,
        session_id: str,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ):
        if self._list_failures_error is not None:
            raise self._list_failures_error
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

    async def get_failure(self, session_id: str, failure_id: str):
        self.requested_failures.append(
            {"session_id": session_id, "failure_id": failure_id}
        )
        if self._get_failure_error is not None:
            raise self._get_failure_error
        if failure_id == "missing-failure":
            return None
        return {
            "id": failure_id,
            "session_id": session_id,
            "task_id": "task-1",
            "kind": "test_failed",
            "title": "测试失败",
            "detail": "详情",
            "source_id": "run-1",
            "status": "open",
            "created_at": "2024-01-01T00:00:00",
        }

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
        if self._list_issues_error is not None:
            raise self._list_issues_error
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

    async def get_issue(self, session_id: str, task_id: str):
        self.requested_issues.append({"session_id": session_id, "task_id": task_id})
        if self._get_issue_error is not None:
            raise self._get_issue_error
        if task_id == "missing-task":
            return None
        return {
            "session_id": session_id,
            "task_id": task_id,
            "mission_id": "mission-1",
            "parallel_mode": "cooperative",
            "risk_level": "high",
            "requires_human_approval": True,
            "acceptance_criteria": ["详情页不依赖全量 snapshot"],
            "expected_artifacts": [],
            "related_branch": "issue-detail",
            "related_worktree": "wt-issue-detail",
            "related_pr": "",
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
        }

    async def list_leases(
        self,
        session_id: str,
        state: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ):
        if self._list_leases_error is not None:
            raise self._list_leases_error
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

    async def get_lease(self, session_id: str, lease_id: str):
        self.requested_leases.append({"session_id": session_id, "lease_id": lease_id})
        if self._get_lease_error is not None:
            raise self._get_lease_error
        if lease_id == "missing-lease":
            return None
        return {
            "id": lease_id,
            "session_id": session_id,
            "task_id": "task-1",
            "agent_id": "agent-1",
            "state": "active",
            "expires_at": "2024-01-01T01:00:00",
            "worktree_name": "wt-1",
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
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
        if self._list_missions_error is not None:
            raise self._list_missions_error
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

    async def get_mission(self, session_id: str, mission_id: str):
        self.requested_missions.append(
            {"session_id": session_id, "mission_id": mission_id}
        )
        if self._get_mission_error is not None:
            raise self._get_mission_error
        if mission_id == "missing-mission":
            return None
        return {
            "id": mission_id,
            "session_id": session_id,
            "title": "Mac 工作台",
            "goal": "补齐 Mission 详情 API",
            "status": "planning",
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
        }

    async def list_intent_locks(self, session_id: str, mission_id: str):
        if self._list_intent_locks_error is not None:
            raise self._list_intent_locks_error
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

    async def get_intent_lock(self, session_id: str, mission_id: str, lock_id: str):
        self.requested_intent_locks.append(
            {
                "session_id": session_id,
                "mission_id": mission_id,
                "lock_id": lock_id,
            }
        )
        if self._get_intent_lock_error is not None:
            raise self._get_intent_lock_error
        if lock_id == "missing-lock":
            return None
        return {
            "id": lock_id,
            "session_id": session_id,
            "mission_id": mission_id,
            "rule": "禁止修改 core 模块",
            "blocked_paths": ["src/secret"],
            "allowed_paths": ["src/secret/README.md"],
            "require_proposal_for_risk": "high",
            "active": True,
            "created_at": "2024-01-01T00:00:00",
        }

    async def list_decisions(self, session_id: str, mission_id: str):
        if self._list_decisions_error is not None:
            raise self._list_decisions_error
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

    async def get_decision(self, session_id: str, mission_id: str, decision_id: str):
        self.requested_decisions.append(
            {
                "session_id": session_id,
                "mission_id": mission_id,
                "decision_id": decision_id,
            }
        )
        if self._get_decision_error is not None:
            raise self._get_decision_error
        if decision_id == "missing-decision":
            return None
        return {
            "id": decision_id,
            "session_id": session_id,
            "mission_id": mission_id,
            "kind": "architecture",
            "title": "采用 FastAPI",
            "content": "使用 FastAPI 承载 Workbench API",
            "actor": "Planner-Agent",
            "created_at": "2024-01-01T00:00:00",
        }


class FakeTaskMarket:
    def __init__(self) -> None:
        self.claimed: list[dict] = []
        self.released: list[str] = []
        self.expired_calls = 0
        self.expired_sessions: list[str] = []
        self._lease: Lease | None = None
        self._expired: list[Lease] = []
        self._claim_error: Exception | None = None
        self._release_error: Exception | None = None
        self._expire_error: Exception | None = None

    def set_lease(self, lease: Lease | None) -> None:
        self._lease = lease

    def set_expired(self, leases: list[Lease]) -> None:
        self._expired = leases

    def set_claim_error(self, error: Exception) -> None:
        self._claim_error = error

    def set_release_error(self, error: Exception) -> None:
        self._release_error = error

    def set_expire_error(self, error: Exception) -> None:
        self._expire_error = error

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
        if self._release_error is not None:
            raise self._release_error
        return self._lease

    async def expire_overdue_leases(self, *, session_id: str, now=None) -> list[Lease]:
        self.expired_calls += 1
        self.expired_sessions.append(session_id)
        if self._expire_error is not None:
            raise self._expire_error
        return list(self._expired)


class FakeWorktreeManager:
    def __init__(self, records: list[WorktreeRecord] | None = None) -> None:
        self.records = list(records or [])
        self.status_calls: list[str] = []
        self.keep_calls: list[dict[str, str]] = []
        self.remove_calls: list[dict[str, object]] = []

    async def status(self, name: str = "") -> WorktreeRecord | list[WorktreeRecord]:
        self.status_calls.append(name)
        if "/" in name:
            raise ValueError("worktree 名称不能包含路径分隔符")
        if name:
            for record in self.records:
                if record.name == name:
                    return record
            raise KeyError(name)
        return list(self.records)

    async def keep(self, name: str, reason: str = "") -> str:
        self.keep_calls.append({"name": name, "reason": reason})
        record = await self.status(name)
        if isinstance(record, list):
            raise KeyError(name)
        record.status = WorktreeStatus.KEPT
        record.kept_reason = reason.strip()
        return "已保留 worktree 供审查。\n\n### Worktree"

    async def remove(self, name: str, discard_changes: bool = False) -> str:
        self.remove_calls.append({"name": name, "discard_changes": discard_changes})
        record = await self.status(name)
        if isinstance(record, list):
            raise KeyError(name)
        if not discard_changes and not record.removable:
            return "拒绝删除：worktree 中仍有未保存或未审查的工作。"
        self.records = [item for item in self.records if item.name != name]
        return f"已删除 worktree：{name}"


class FakeWorkbenchStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def append_event(
        self,
        *,
        session_id: str,
        type: str,
        actor: str,
        subject_id: str,
        payload: dict | None = None,
    ):
        self.events.append(
            {
                "session_id": session_id,
                "type": type,
                "actor": actor,
                "subject_id": subject_id,
                "payload": payload or {},
            }
        )


class _FakeEngine:
    def __init__(
        self,
        exists: bool,
        workbench_market=None,
        worktree_manager: FakeWorktreeManager | None = None,
        load_session_result: bool | None = None,
        load_session_results: dict[str, bool] | None = None,
        load_session_error: Exception | None = None,
    ) -> None:
        self.session_store = _FakeSessionStore(exists)
        self.workbench_service = _FakeWorkbenchService()
        self.workbench_store = FakeWorkbenchStore()
        self.workbench_market = workbench_market
        self.worktree_manager = worktree_manager or FakeWorktreeManager()
        self.load_session_result = load_session_result
        self.load_session_results = load_session_results or {}
        self.load_session_error = load_session_error
        self.loaded: list[str] = []

    async def load_session(self, session_id: str) -> bool:
        self.loaded.append(session_id)
        if self.load_session_error is not None:
            raise self.load_session_error
        if session_id in self.load_session_results:
            return self.load_session_results[session_id]
        if self.load_session_result is not None:
            return self.load_session_result
        exists = getattr(self.session_store, "exists", None)
        if exists is not None:
            return bool(exists)
        return await self.session_store.load(session_id) is not None


def _fake_request(engine: _FakeEngine):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))


class _RecordingWorkbenchWebSocket:
    def __init__(
        self,
        engine: _FakeEngine,
        *,
        config=None,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(engine=engine, config=config))
        self.headers = headers or {}
        self.query_params = query_params or {}
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
        self.list_requests: list[dict[str, int | str]] = []

    async def load(self, session_id: str):
        self.loaded.append(session_id)
        for session in self.sessions:
            if session.id == session_id:
                return session
        return None

    async def list_sessions(
        self, page: int = 1, page_size: int = 20, query: str = ""
    ) -> tuple[list[SimpleNamespace], int]:
        self.list_requests.append({"page": page, "page_size": page_size, "query": query})
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
async def test_workbench_snapshot_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_workbench_snapshot("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_workbench_snapshot_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_workbench_snapshot("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_workbench_snapshot_endpoint_includes_worktree_statuses() -> None:
    worktree_manager = FakeWorktreeManager(
        [
            WorktreeRecord(
                name="wt-api",
                path="/repo/.naumi/worktrees/wt-api",
                branch="naumi/worktree-wt-api",
                base_ref="abc123",
                status=WorktreeStatus.DIRTY,
                task_id="task-1",
                dirty_files=2,
                commits_ahead=1,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:04:00",
            )
        ]
    )
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)

    response = await get_workbench_snapshot("sess-1", _fake_request(engine), auth="test")

    assert worktree_manager.status_calls == [""]
    assert response["worktrees"] == [
        {
            "name": "wt-api",
            "path": "/repo/.naumi/worktrees/wt-api",
            "branch": "naumi/worktree-wt-api",
            "base_ref": "abc123",
            "status": "dirty",
            "task_id": "task-1",
            "dirty_files": 2,
            "commits_ahead": 1,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:04:00",
            "kept_reason": "",
            "metadata": {},
            "removable": False,
        }
    ]


@pytest.mark.asyncio
async def test_workbench_snapshot_endpoint_reports_unavailable_snapshot_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_dashboard_snapshot_error(
        RuntimeError("snapshot backend unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_workbench_snapshot("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 503
    assert exc.value.detail == "snapshot backend unavailable"


@pytest.mark.asyncio
async def test_workbench_snapshot_endpoint_reports_invalid_snapshot_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_dashboard_snapshot_error(
        ValueError("snapshot session state is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_workbench_snapshot("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 400
    assert exc.value.detail == "snapshot session state is invalid"


@pytest.mark.asyncio
async def test_get_events_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_workbench_events("missing", _fake_request(engine), limit=10, auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_events_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_workbench_events("sess-1", _fake_request(engine), limit=10, auth="test")

    assert engine.loaded == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_events_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_workbench_events("sess-1", _fake_request(engine), limit=10, auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_events == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
            "since": None,
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
        "since": None,
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_get_events_endpoint_reports_unavailable_event_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_events_error(
        RuntimeError("event store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_workbench_events("sess-1", _fake_request(engine), limit=10, auth="test")

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 503
    assert exc.value.detail == "event store unavailable"


@pytest.mark.asyncio
async def test_get_events_endpoint_reports_invalid_event_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_events_error(
        ValueError("event filter is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_workbench_events("sess-1", _fake_request(engine), limit=10, auth="test")

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 400
    assert exc.value.detail == "event filter is invalid"


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
        since="2026-06-27T10:00:00+00:00",
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_events == [
        {
            "session_id": "sess-1",
            "event_type": "issue.created",
            "subject_id": "task-2",
            "actor": "Planner-Agent",
            "since": "2026-06-27T10:00:00+00:00",
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
        "since": "2026-06-27T10:00:00+00:00",
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
            "since": "2026-06-27T10:00:00+00:00",
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
            "since": "2026-06-27T10:00:00+00:00",
            "limit": 7,
        }
    ]
    assert response.json()["event_type"] == "issue.created"
    assert response.json()["since"] == "2026-06-27T10:00:00+00:00"


def test_get_event_route_returns_single_event() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/events/event-2")

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_events == [
        {"session_id": "sess-1", "event_id": "event-2"}
    ]
    assert response.json() == {
        "id": "event-2",
        "session_id": "sess-1",
        "type": "issue.claimed",
        "actor": "Backend-Agent",
        "subject_id": "task-1",
        "payload": {"lease_id": "lease-1"},
        "timestamp": "2024-01-01T00:00:00",
    }


def test_get_event_route_returns_chinese_404_for_missing_event() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/events/missing-event")

    assert response.status_code == 404
    assert response.json() == {"detail": "审计事件不存在"}


@pytest.mark.asyncio
async def test_get_event_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_workbench_event(
            "sess-1",
            "event-2",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == []
    assert engine.workbench_service.requested_events == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_event_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_workbench_event(
            "sess-1",
            "event-2",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_events == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_event_endpoint_reports_unavailable_event_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_event_error(
        RuntimeError("event detail store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_workbench_event(
            "sess-1",
            "event-2",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_events == [
        {"session_id": "sess-1", "event_id": "event-2"}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "event detail store unavailable"


@pytest.mark.asyncio
async def test_get_event_endpoint_reports_invalid_event_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_event_error(ValueError("event id is invalid"))

    with pytest.raises(HTTPException) as exc:
        await get_workbench_event(
            "sess-1",
            "event-2",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_events == [
        {"session_id": "sess-1", "event_id": "event-2"}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "event id is invalid"


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
                "since": "2026-06-27T10:00:00+00:00",
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
            "since": None,
            "limit": 50,
        },
        {
            "session_id": "sess-1",
            "event_type": "issue.created",
            "subject_id": "task-2",
            "actor": "Planner-Agent",
            "since": "2026-06-27T10:00:00+00:00",
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


def test_workbench_event_stream_reports_refresh_errors_without_abrupt_close() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_events_error(
        RuntimeError("event store unavailable")
    )
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    with client.websocket_connect("/workbench/sessions/sess-1/events/stream") as websocket:
        assert websocket.receive_json() == {"type": "connected", "session_id": "sess-1"}
        assert websocket.receive_json() == {
            "type": "error",
            "message": "event store unavailable",
        }

        websocket.send_json({"type": "ping"})

        assert websocket.receive_json() == {"type": "pong"}


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
            "since": None,
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
async def test_workbench_event_stream_can_send_initial_snapshot_on_connect() -> None:
    engine = _FakeEngine(exists=True)
    websocket = _RecordingWorkbenchWebSocket(
        engine,
        query_params={"include_snapshot": "true"},
    )

    await websocket_workbench_events(websocket, "sess-1")

    assert websocket.accepted is True
    assert engine.loaded == ["sess-1"]
    assert websocket.sent_json[0] == {"type": "connected", "session_id": "sess-1"}
    assert websocket.sent_json[1] == {
        "type": "workbench/snapshot",
        "version": 1,
        "payload": {
            "version": 1,
            "session_id": "sess-1",
            "missions": [],
            "tasks": [],
            "issues": [],
            "leases": [],
            "failures": [],
            "events": [],
            "worktrees": [],
        },
    }
    assert websocket.sent_json[-1] == {"type": "refresh_complete", "count": 1}


@pytest.mark.asyncio
async def test_workbench_event_stream_rejects_invalid_api_key_when_configured() -> None:
    engine = _FakeEngine(exists=True)
    config = SimpleNamespace(api=SimpleNamespace(api_keys=["local-token"]))
    websocket = _RecordingWorkbenchWebSocket(
        engine,
        config=config,
        headers={"Authorization": "Bearer wrong-token"},
    )

    await websocket_workbench_events(websocket, "sess-1")

    assert websocket.accepted is True
    assert websocket.closed is True
    assert engine.loaded == []
    assert engine.workbench_service.listed_events == []
    assert websocket.sent_json == [
        {"type": "error", "message": "Invalid API key"},
    ]


@pytest.mark.asyncio
async def test_workbench_event_stream_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    websocket = _RecordingWorkbenchWebSocket(engine)

    await websocket_workbench_events(websocket, "sess-1")

    assert websocket.accepted is True
    assert websocket.closed is True
    assert engine.loaded == []
    assert engine.workbench_service.listed_events == []
    assert websocket.sent_json == [
        {"type": "error", "message": "会话存储暂不可用"},
    ]


@pytest.mark.asyncio
async def test_workbench_event_stream_reports_unavailable_runtime_session() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )
    websocket = _RecordingWorkbenchWebSocket(engine)

    await websocket_workbench_events(websocket, "sess-1")

    assert websocket.accepted is True
    assert websocket.closed is True
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_events == []
    assert websocket.sent_json == [
        {"type": "error", "message": "运行态会话暂不可用"},
    ]


@pytest.mark.asyncio
async def test_workbench_event_stream_accepts_valid_api_key_when_configured() -> None:
    engine = _FakeEngine(exists=True)
    config = SimpleNamespace(api=SimpleNamespace(api_keys=["local-token"]))
    websocket = _RecordingWorkbenchWebSocket(
        engine,
        config=config,
        headers={"Authorization": "Bearer local-token"},
    )

    await websocket_workbench_events(websocket, "sess-1")

    assert websocket.accepted is True
    assert websocket.closed is False
    assert engine.loaded == ["sess-1"]
    assert websocket.sent_json[0] == {"type": "connected", "session_id": "sess-1"}
    assert websocket.sent_json[-1] == {"type": "refresh_complete", "count": 1}


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
async def test_get_validation_runs_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_validation_runs(
            "sess-1", _fake_request(engine), task_id=None, limit=10, auth="test"
        )

    assert engine.loaded == []
    assert engine.workbench_service.listed_validation_runs == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_validation_runs_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_validation_runs(
            "sess-1", _fake_request(engine), task_id=None, limit=10, auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_validation_runs == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_get_validation_runs_endpoint_reports_unavailable_validation_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_validation_runs_error(
        RuntimeError("validation store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_validation_runs(
            "sess-1", _fake_request(engine), task_id="task-2", limit=25, auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 503
    assert exc.value.detail == "validation store unavailable"


@pytest.mark.asyncio
async def test_get_validation_runs_endpoint_reports_invalid_validation_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_validation_runs_error(
        ValueError("validation filter is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_validation_runs(
            "sess-1", _fake_request(engine), task_id="task-2", limit=25, auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 400
    assert exc.value.detail == "validation filter is invalid"


@pytest.mark.asyncio
async def test_get_validation_run_endpoint_returns_single_run() -> None:
    engine = _FakeEngine(exists=True)

    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/validation-runs/run-2",
        headers={"Authorization": "Bearer local-token"},
    )

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_validation_runs == [
        {"session_id": "sess-1", "run_id": "run-2"}
    ]
    assert response.json() == {
        "id": "run-2",
        "session_id": "sess-1",
        "task_id": "task-1",
        "actor": "ValidationRunner",
        "command": ["pytest", "test.py"],
        "cwd": "/workspace",
        "status": "passed",
        "exit_code": 0,
        "output": "ok",
        "started_at": "2024-01-01T00:00:00",
        "completed_at": "2024-01-01T00:00:01",
    }


@pytest.mark.asyncio
async def test_get_validation_run_endpoint_returns_404_for_missing_run() -> None:
    engine = _FakeEngine(exists=True)

    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/validation-runs/missing-run",
        headers={"Authorization": "Bearer local-token"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "验证运行不存在"


@pytest.mark.asyncio
async def test_get_validation_run_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_validation_run("sess-1", "run-2", _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.requested_validation_runs == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_validation_run_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_validation_run("sess-1", "run-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_validation_runs == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_validation_run_endpoint_reports_unavailable_validation_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_validation_run_error(
        RuntimeError("validation run store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_validation_run("sess-1", "run-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_validation_runs == [
        {"session_id": "sess-1", "run_id": "run-2"}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "validation run store unavailable"


@pytest.mark.asyncio
async def test_get_validation_run_endpoint_reports_invalid_validation_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_validation_run_error(
        ValueError("validation run id is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_validation_run("sess-1", "run-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_validation_runs == [
        {"session_id": "sess-1", "run_id": "run-2"}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "validation run id is invalid"


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
async def test_get_context_snapshots_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_context_snapshots(
            "sess-1",
            _fake_request(engine),
            task_id=None,
            agent_id=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == []
    assert engine.workbench_service.listed_context_snapshots == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_context_snapshots_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_context_snapshots(
            "sess-1",
            _fake_request(engine),
            task_id=None,
            agent_id=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_context_snapshots == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_get_context_snapshots_endpoint_reports_unavailable_context_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_context_snapshots_error(
        RuntimeError("context snapshot store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_context_snapshots(
            "sess-1",
            _fake_request(engine),
            task_id="task-2",
            agent_id="agent-2",
            limit=25,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 503
    assert exc.value.detail == "context snapshot store unavailable"


@pytest.mark.asyncio
async def test_get_context_snapshots_endpoint_reports_invalid_context_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_context_snapshots_error(
        ValueError("context snapshot filter is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_context_snapshots(
            "sess-1",
            _fake_request(engine),
            task_id="task-2",
            agent_id="agent-2",
            limit=25,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 400
    assert exc.value.detail == "context snapshot filter is invalid"


@pytest.mark.asyncio
async def test_get_context_snapshot_endpoint_returns_single_snapshot() -> None:
    engine = _FakeEngine(exists=True)

    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/context-snapshots/snap-2",
        headers={"Authorization": "Bearer local-token"},
    )

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_context_snapshots == [
        {"session_id": "sess-1", "snapshot_id": "snap-2"}
    ]
    assert response.json() == {
        "id": "snap-2",
        "session_id": "sess-1",
        "agent_id": "agent-1",
        "task_id": "task-1",
        "health": "good",
        "reasons": ["上下文健康"],
        "created_at": "2024-01-01T00:00:00",
    }


@pytest.mark.asyncio
async def test_get_context_snapshot_endpoint_returns_404_for_missing_snapshot() -> None:
    engine = _FakeEngine(exists=True)

    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/context-snapshots/missing-snapshot",
        headers={"Authorization": "Bearer local-token"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "上下文快照不存在"


@pytest.mark.asyncio
async def test_get_context_snapshot_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_context_snapshot(
            "sess-1", "snap-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == []
    assert engine.workbench_service.requested_context_snapshots == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_context_snapshot_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_context_snapshot(
            "sess-1", "snap-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_context_snapshots == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_context_snapshot_endpoint_reports_unavailable_context_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_context_snapshot_error(
        RuntimeError("context snapshot store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_context_snapshot(
            "sess-1", "snap-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_context_snapshots == [
        {"session_id": "sess-1", "snapshot_id": "snap-2"}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "context snapshot store unavailable"


@pytest.mark.asyncio
async def test_get_context_snapshot_endpoint_reports_invalid_context_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_context_snapshot_error(
        ValueError("context snapshot id is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_context_snapshot(
            "sess-1", "snap-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_context_snapshots == [
        {"session_id": "sess-1", "snapshot_id": "snap-2"}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "context snapshot id is invalid"


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
async def test_create_context_health_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    body = ContextHealthRecord(
        agent_id="Agent-A",
        minutes_since_sync=75,
        token_load_ratio=0.2,
    )

    with pytest.raises(HTTPException) as exc:
        await create_context_health_snapshot(
            "sess-1", "task-2", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == []
    assert engine.workbench_service.recorded_context_health == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_create_context_health_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
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

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.recorded_context_health == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_create_context_health_endpoint_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    body = ContextHealthRecord(
        agent_id="Agent-A",
        minutes_since_sync=75,
        token_load_ratio=0.2,
        policy_conflict=False,
        actor="Human",
    )

    response = await create_context_health_snapshot(
        "sess-1",
        "task-2",
        body,
        _fake_request(engine),
        include_snapshot=True,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert response["context_snapshot"]["id"] == "snap-1"
    assert response["context_snapshot"]["health"] == "stale"
    assert response["snapshot"]["version"] == 1
    assert response["snapshot"]["session_id"] == "sess-1"


def test_create_context_health_route_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/issues/task-2/context-health",
        params={"include_snapshot": "true"},
        json={
            "agent_id": "Agent-A",
            "minutes_since_sync": 75,
            "token_load_ratio": 0.2,
            "policy_conflict": False,
            "actor": "Human",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["context_snapshot"]["id"] == "snap-1"
    assert body["context_snapshot"]["health"] == "stale"
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


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


@pytest.mark.asyncio
async def test_create_context_health_endpoint_reports_unavailable_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_context_health_error(
        RuntimeError("context health store unavailable")
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

    assert engine.loaded == ["sess-1"]
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
    assert exc.value.status_code == 503
    assert exc.value.detail == "context health store unavailable"


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
async def test_create_mission_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    body = MissionCreate(title="Mac 工作台", goal="可视化治理多 Agent 研发")

    with pytest.raises(HTTPException) as exc:
        await create_workbench_mission("sess-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.created_missions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_create_mission_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )
    body = MissionCreate(title="Mac 工作台", goal="可视化治理多 Agent 研发")

    with pytest.raises(HTTPException) as exc:
        await create_workbench_mission("sess-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.created_missions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_create_mission_endpoint_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    body = MissionCreate(title="Mac 工作台", goal="可视化治理多 Agent 研发")

    response = await create_workbench_mission(
        "sess-1",
        body,
        _fake_request(engine),
        include_snapshot=True,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert response["mission"]["session_id"] == "sess-1"
    assert response["mission"]["title"] == "Mac 工作台"
    assert response["snapshot"]["version"] == 1
    assert response["snapshot"]["session_id"] == "sess-1"


def test_create_mission_route_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/missions",
        params={"include_snapshot": "true"},
        json={"title": "Mac 工作台", "goal": "可视化治理多 Agent 研发"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["mission"]["session_id"] == "sess-1"
    assert body["mission"]["title"] == "Mac 工作台"
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_create_mission_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_create_mission_error(ValueError("Mission 标题不能为空"))
    body = MissionCreate(title="Mac 工作台", goal="可视化治理多 Agent 研发")

    with pytest.raises(HTTPException) as exc:
        await create_workbench_mission("sess-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.created_missions == [
        {"session_id": "sess-1", "title": "Mac 工作台", "goal": "可视化治理多 Agent 研发"}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "Mission 标题不能为空"


@pytest.mark.asyncio
async def test_create_mission_endpoint_reports_unavailable_mission_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_create_mission_error(
        RuntimeError("mission store unavailable")
    )
    body = MissionCreate(title="Mac 工作台", goal="可视化治理多 Agent 研发")

    with pytest.raises(HTTPException) as exc:
        await create_workbench_mission("sess-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.created_missions == [
        {"session_id": "sess-1", "title": "Mac 工作台", "goal": "可视化治理多 Agent 研发"}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "mission store unavailable"


@pytest.mark.asyncio
async def test_get_missions_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_missions("missing", _fake_request(engine), status=None, limit=10, auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_missions_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_missions(
            "sess-1", _fake_request(engine), status=None, limit=10, auth="test"
        )

    assert engine.loaded == []
    assert engine.workbench_service.listed_missions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_missions_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_missions(
            "sess-1", _fake_request(engine), status=None, limit=10, auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_missions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_get_missions_endpoint_reports_invalid_filter() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_missions_error(
        ValueError("mission status filter is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_missions(
            "sess-1", _fake_request(engine), status="bad-status", limit=25, auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_missions == [
        {"session_id": "sess-1", "status": "bad-status", "limit": 25}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "mission status filter is invalid"


@pytest.mark.asyncio
async def test_get_missions_endpoint_reports_unavailable_mission_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_missions_error(
        RuntimeError("mission store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_missions(
            "sess-1", _fake_request(engine), status=None, limit=25, auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_missions == [
        {"session_id": "sess-1", "status": None, "limit": 25}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "mission store unavailable"


def test_get_mission_route_returns_single_mission_detail() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/missions/mission-2")

    assert response.status_code == 200
    assert engine.workbench_service.requested_missions == [
        {"session_id": "sess-1", "mission_id": "mission-2"}
    ]
    assert response.json() == {
        "id": "mission-2",
        "session_id": "sess-1",
        "title": "Mac 工作台",
        "goal": "补齐 Mission 详情 API",
        "status": "planning",
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }


def test_get_mission_route_returns_404_for_missing_mission() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/missions/missing-mission")

    assert response.status_code == 404
    assert response.json()["detail"] == "mission 不存在"
    assert engine.workbench_service.requested_missions == [
        {"session_id": "sess-1", "mission_id": "missing-mission"}
    ]


@pytest.mark.asyncio
async def test_get_mission_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_mission("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.requested_missions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_mission_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_mission("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_missions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_mission_endpoint_reports_invalid_mission_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_mission_error(
        ValueError("mission id is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_mission("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_missions == [
        {"session_id": "sess-1", "mission_id": "mission-2"}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "mission id is invalid"


@pytest.mark.asyncio
async def test_get_mission_endpoint_reports_unavailable_mission_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_mission_error(
        RuntimeError("mission store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_mission("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_missions == [
        {"session_id": "sess-1", "mission_id": "mission-2"}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "mission store unavailable"


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
async def test_attach_issue_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    body = IssueAttach(task_id="task-1", acceptance_criteria=["认领冲突必须被拒绝"])

    with pytest.raises(HTTPException) as exc:
        await attach_workbench_issue(
            "sess-1", "mission-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == []
    assert engine.workbench_service.attached_issues == []
    assert engine.workbench_service.created_issues == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_attach_issue_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )
    body = IssueAttach(task_id="task-1", acceptance_criteria=["认领冲突必须被拒绝"])

    with pytest.raises(HTTPException) as exc:
        await attach_workbench_issue(
            "sess-1", "mission-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.attached_issues == []
    assert engine.workbench_service.created_issues == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_attach_issue_endpoint_reports_unavailable_issue_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_issue_error(RuntimeError("issue store unavailable"))
    body = IssueAttach(
        task_id="task-1",
        acceptance_criteria=["AC1"],
        parallel_mode=ParallelMode.EXCLUSIVE,
        risk_level=RiskLevel.MEDIUM,
    )

    with pytest.raises(HTTPException) as exc:
        await attach_workbench_issue(
            "sess-1", "mission-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.attached_issues == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "task_id": "task-1",
            "acceptance_criteria": ["AC1"],
            "parallel_mode": ParallelMode.EXCLUSIVE,
            "risk_level": RiskLevel.MEDIUM,
        }
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "issue store unavailable"


@pytest.mark.asyncio
async def test_claim_issue_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False, workbench_market=FakeTaskMarket())
    body = ClaimIssue(agent_id="Agent-1")

    with pytest.raises(HTTPException) as exc:
        await claim_workbench_issue("missing", "task-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_claim_issue_endpoint_reports_unavailable_session_store() -> None:
    market = FakeTaskMarket()
    engine = _FakeEngine(exists=True, workbench_market=market)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    body = ClaimIssue(agent_id="Agent-1", duration_minutes=30, worktree_name="wt-1")

    with pytest.raises(HTTPException) as exc:
        await claim_workbench_issue(
            "sess-1", "task-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == []
    assert market.claimed == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_claim_issue_endpoint_reports_runtime_session_load_failure() -> None:
    market = FakeTaskMarket()
    engine = _FakeEngine(
        exists=True,
        workbench_market=market,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )
    body = ClaimIssue(agent_id="Agent-1", duration_minutes=30, worktree_name="wt-1")

    with pytest.raises(HTTPException) as exc:
        await claim_workbench_issue(
            "sess-1", "task-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert market.claimed == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_claim_issue_endpoint_can_return_fresh_snapshot() -> None:
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

    response = await claim_workbench_issue(
        "sess-1",
        "task-1",
        body,
        _fake_request(engine),
        include_snapshot=True,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert response["lease"]["id"] == "lease-1"
    assert response["lease"]["task_id"] == "task-1"
    assert response["snapshot"]["version"] == 1
    assert response["snapshot"]["session_id"] == "sess-1"


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
async def test_claim_issue_endpoint_reports_unavailable_task_market() -> None:
    market = FakeTaskMarket()
    market.set_claim_error(RuntimeError("task market unavailable"))
    engine = _FakeEngine(exists=True, workbench_market=market)
    body = ClaimIssue(agent_id="Agent-1", duration_minutes=30, worktree_name="wt-1")

    with pytest.raises(HTTPException) as exc:
        await claim_workbench_issue(
            "sess-1", "task-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert market.claimed == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "task market unavailable"


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
async def test_get_approvals_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_approvals(
            "sess-1", _fake_request(engine), state=None, limit=10, auth="test"
        )

    assert engine.loaded == []
    assert engine.workbench_service.listed_approvals == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_approvals_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await get_approvals(
            "sess-1", _fake_request(engine), state=None, limit=10, auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_approvals == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_get_approvals_endpoint_reports_unavailable_approval_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_approvals_error(
        RuntimeError("approval store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_approvals(
            "sess-1",
            _fake_request(engine),
            state=ApprovalState.WAITING,
            limit=25,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 503
    assert exc.value.detail == "approval store unavailable"


@pytest.mark.asyncio
async def test_get_approvals_endpoint_reports_invalid_approval_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_approvals_error(
        ValueError("approval filter is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_approvals(
            "sess-1",
            _fake_request(engine),
            state=ApprovalState.WAITING,
            limit=25,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert exc.value.status_code == 400
    assert exc.value.detail == "approval filter is invalid"


def test_get_approval_route_returns_single_approval() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/approvals/approval-2")

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_approvals == [
        {"session_id": "sess-1", "approval_id": "approval-2"}
    ]
    assert response.json() == {
        "id": "approval-2",
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


def test_get_approval_route_returns_404_for_missing_approval() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/approvals/missing-approval")

    assert response.status_code == 404
    assert response.json() == {"detail": "审批请求不存在"}
    assert engine.workbench_service.requested_approvals == [
        {"session_id": "sess-1", "approval_id": "missing-approval"}
    ]


@pytest.mark.asyncio
async def test_get_approval_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_approval("sess-1", "approval-2", _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.requested_approvals == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_approval_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await get_approval("sess-1", "approval-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_approvals == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_approval_endpoint_reports_unavailable_approval_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_approval_error(
        RuntimeError("approval store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_approval("sess-1", "approval-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_approvals == [
        {"session_id": "sess-1", "approval_id": "approval-2"}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "approval store unavailable"


@pytest.mark.asyncio
async def test_get_approval_endpoint_reports_invalid_approval_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_approval_error(
        ValueError("approval id is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_approval("sess-1", "approval-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_approvals == [
        {"session_id": "sess-1", "approval_id": "approval-2"}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "approval id is invalid"


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
async def test_get_failures_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_failures(
            "sess-1",
            _fake_request(engine),
            task_id=None,
            status=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == []
    assert engine.workbench_service.listed_failures == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_failures_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_failures(
            "sess-1",
            _fake_request(engine),
            task_id=None,
            status=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_failures == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_get_failures_endpoint_reports_invalid_failure_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_failures_error(
        ValueError("failure filter is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_failures(
            "sess-1",
            _fake_request(engine),
            task_id=None,
            status="invalid",
            limit=10,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_failures == []
    assert exc.value.status_code == 400
    assert exc.value.detail == "failure filter is invalid"


@pytest.mark.asyncio
async def test_get_failures_endpoint_reports_unavailable_failure_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_failures_error(
        RuntimeError("failure store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_failures(
            "sess-1",
            _fake_request(engine),
            task_id=None,
            status=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_failures == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "failure store unavailable"


@pytest.mark.asyncio
async def test_get_failure_endpoint_returns_single_failure() -> None:
    engine = _FakeEngine(exists=True)

    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/failures/failure-2",
        headers={"Authorization": "Bearer local-token"},
    )

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_failures == [
        {"session_id": "sess-1", "failure_id": "failure-2"}
    ]
    assert response.json() == {
        "id": "failure-2",
        "session_id": "sess-1",
        "task_id": "task-1",
        "kind": "test_failed",
        "title": "测试失败",
        "detail": "详情",
        "source_id": "run-1",
        "status": "open",
        "created_at": "2024-01-01T00:00:00",
    }


@pytest.mark.asyncio
async def test_get_failure_endpoint_returns_404_for_missing_failure() -> None:
    engine = _FakeEngine(exists=True)

    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/failures/missing-failure",
        headers={"Authorization": "Bearer local-token"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "失败卡片不存在"


@pytest.mark.asyncio
async def test_get_failure_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_failure("sess-1", "failure-2", _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.requested_failures == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_failure_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_failure("sess-1", "failure-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_failures == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_failure_endpoint_reports_invalid_failure_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_failure_error(ValueError("failure id is invalid"))

    with pytest.raises(HTTPException) as exc:
        await get_failure("sess-1", "failure-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_failures == [
        {"session_id": "sess-1", "failure_id": "failure-2"}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "failure id is invalid"


@pytest.mark.asyncio
async def test_get_failure_endpoint_reports_unavailable_failure_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_failure_error(
        RuntimeError("failure detail store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_failure("sess-1", "failure-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_failures == [
        {"session_id": "sess-1", "failure_id": "failure-2"}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "failure detail store unavailable"


@pytest.mark.asyncio
async def test_resolve_approval_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = ApprovalResolve(state=ApprovalState.APPROVED)

    with pytest.raises(HTTPException) as exc:
        await resolve_approval("missing", "approval-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_resolve_approval_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    body = ApprovalResolve(actor="Human", state=ApprovalState.APPROVED)

    with pytest.raises(HTTPException) as exc:
        await resolve_approval("sess-1", "approval-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.resolved_approvals == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_resolve_approval_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )
    body = ApprovalResolve(actor="Human", state=ApprovalState.APPROVED)

    with pytest.raises(HTTPException) as exc:
        await resolve_approval("sess-1", "approval-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.resolved_approvals == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_resolve_approval_endpoint_can_return_fresh_snapshot() -> None:
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
        "sess-1",
        "approval-1",
        body,
        _fake_request(engine),
        include_snapshot=True,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert response["approval"]["id"] == "approval-1"
    assert response["approval"]["state"] == "approved"
    assert response["snapshot"]["version"] == 1
    assert response["snapshot"]["session_id"] == "sess-1"


def test_resolve_approval_route_can_return_fresh_snapshot() -> None:
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
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/approvals/approval-1/resolve",
        params={"include_snapshot": "true"},
        json={"actor": "Human", "state": "approved", "decision_note": "同意"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["approval"]["id"] == "approval-1"
    assert body["approval"]["state"] == "approved"
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


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
async def test_resolve_approval_endpoint_reports_unavailable_approval_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_resolve_approval_error(
        RuntimeError("approval store unavailable")
    )
    body = ApprovalResolve(actor="Human", state=ApprovalState.APPROVED)

    with pytest.raises(HTTPException) as exc:
        await resolve_approval("sess-1", "approval-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.resolved_approvals == [
        {
            "session_id": "sess-1",
            "approval_id": "approval-1",
            "actor": "Human",
            "state": ApprovalState.APPROVED,
            "decision_note": "",
        }
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "approval store unavailable"


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
async def test_release_lease_endpoint_reports_unavailable_session_store() -> None:
    market = FakeTaskMarket()
    engine = _FakeEngine(exists=True, workbench_market=market)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await release_workbench_lease(
            "sess-1", "lease-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == []
    assert market.released == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_release_lease_endpoint_reports_runtime_session_load_failure() -> None:
    market = FakeTaskMarket()
    engine = _FakeEngine(
        exists=True,
        workbench_market=market,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await release_workbench_lease(
            "sess-1", "lease-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert market.released == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_release_lease_endpoint_can_return_fresh_snapshot() -> None:
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

    response = await release_workbench_lease(
        "sess-1",
        "lease-2",
        _fake_request(engine),
        include_snapshot=True,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert response["lease"]["id"] == "lease-2"
    assert response["lease"]["state"] == LeaseState.RELEASED
    assert response["snapshot"]["version"] == 1
    assert response["snapshot"]["session_id"] == "sess-1"


def test_release_lease_route_can_return_fresh_snapshot() -> None:
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
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/leases/lease-2/release",
        params={"include_snapshot": "true"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["lease"]["id"] == "lease-2"
    assert body["lease"]["state"] == "released"
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_release_lease_endpoint_reports_unavailable_task_market() -> None:
    market = FakeTaskMarket()
    market.set_release_error(RuntimeError("task market unavailable"))
    engine = _FakeEngine(exists=True, workbench_market=market)

    with pytest.raises(HTTPException) as exc:
        await release_workbench_lease(
            "sess-1", "lease-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert market.released == [{"session_id": "sess-1", "lease_id": "lease-2"}]
    assert exc.value.status_code == 503
    assert exc.value.detail == "task market unavailable"


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
    assert market.expired_sessions == ["sess-1"]
    assert response == {"expired": [asdict(lease)]}


@pytest.mark.asyncio
async def test_expire_leases_endpoint_reports_unavailable_session_store() -> None:
    market = FakeTaskMarket()
    engine = _FakeEngine(exists=True, workbench_market=market)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await expire_workbench_leases("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert market.expired_calls == 0
    assert market.expired_sessions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_expire_leases_endpoint_reports_runtime_session_load_failure() -> None:
    market = FakeTaskMarket()
    engine = _FakeEngine(
        exists=True,
        workbench_market=market,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await expire_workbench_leases("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert market.expired_calls == 0
    assert market.expired_sessions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_expire_leases_endpoint_can_return_fresh_snapshot() -> None:
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

    response = await expire_workbench_leases(
        "sess-1",
        _fake_request(engine),
        include_snapshot=True,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert response["expired"] == [asdict(lease)]
    assert response["snapshot"]["version"] == 1
    assert response["snapshot"]["session_id"] == "sess-1"


def test_expire_leases_route_can_return_fresh_snapshot() -> None:
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
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/leases/expire",
        params={"include_snapshot": "true"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["expired"][0]["id"] == "lease-3"
    assert body["expired"][0]["state"] == "expired"
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_expire_leases_endpoint_reports_unavailable_task_market() -> None:
    market = FakeTaskMarket()
    market.set_expire_error(RuntimeError("task market unavailable"))
    engine = _FakeEngine(exists=True, workbench_market=market)

    with pytest.raises(HTTPException) as exc:
        await expire_workbench_leases("sess-1", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert market.expired_calls == 1
    assert market.expired_sessions == ["sess-1"]
    assert exc.value.status_code == 503
    assert exc.value.detail == "task market unavailable"


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
async def test_daemon_status_persists_generated_started_at(monkeypatch) -> None:
    class FakeDateTime:
        calls = 0

        @classmethod
        def now(cls, tz):
            cls.calls += 1
            seconds = 10 + cls.calls
            return datetime(2026, 6, 27, 10, 0, seconds, tzinfo=tz)

    monkeypatch.setattr(workbench_routes, "datetime", FakeDateTime)
    engine = _FakeEngine(exists=True)
    engine.session_store = _FakeSessionStoreWithCount(total=0)
    state = SimpleNamespace(engine=engine)
    request = SimpleNamespace(
        app=SimpleNamespace(state=state),
        url=SimpleNamespace(hostname="127.0.0.1", port=8765),
    )

    first = await get_daemon_status(request, auth="test")
    second = await get_daemon_status(request, auth="test")

    assert first.started_at == "2026-06-27T10:00:11+00:00"
    assert second.started_at == first.started_at
    assert state.started_at == first.started_at
    assert FakeDateTime.calls == 1


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
    assert engine.loaded == ["sess-latest"]


@pytest.mark.asyncio
async def test_workbench_bootstrap_does_not_select_unloadable_latest_session() -> None:
    engine = _FakeEngine(exists=True, load_session_result=False)
    latest_session = SimpleNamespace(
        id="sess-broken",
        title="损坏会话",
        model="gpt-5",
        created_at=datetime(2026, 6, 27, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 6, 27, 9, 0, tzinfo=UTC),
        messages=[],
        total_tokens=0,
        total_cost_usd=0.0,
        status="active",
    )
    engine.session_store = _FakeSessionStoreWithLatest([latest_session])
    request = _fake_status_request(engine)

    response = await get_workbench_bootstrap(request, auth="test")

    assert response.sessions[0]["id"] == "sess-broken"
    assert response.selected_session_id is None
    assert response.snapshot is None
    assert response.daemon_status.status == "running"
    assert engine.loaded == ["sess-broken"]


@pytest.mark.asyncio
async def test_workbench_bootstrap_degrades_when_session_registry_is_unavailable() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.list_sessions_error = RuntimeError("session registry unavailable")
    request = _fake_status_request(engine)

    response = await get_workbench_bootstrap(request, page_size=5, auth="test")

    assert response.sessions == []
    assert response.total_sessions == 0
    assert response.selected_session_id is None
    assert response.snapshot is None
    assert response.daemon_status.status == "running"
    assert response.capabilities.protocol_version == 1
    assert engine.loaded == []


@pytest.mark.asyncio
async def test_workbench_bootstrap_selects_next_loadable_session() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_results={
            "sess-broken": False,
            "sess-ready": True,
        },
    )
    broken_session = SimpleNamespace(
        id="sess-broken",
        title="损坏会话",
        model="gpt-5",
        created_at=datetime(2026, 6, 27, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 6, 27, 9, 0, tzinfo=UTC),
        messages=[],
        total_tokens=0,
        total_cost_usd=0.0,
        status="active",
    )
    ready_session = SimpleNamespace(
        id="sess-ready",
        title="可用会话",
        model="gpt-5",
        created_at=datetime(2026, 6, 27, 7, 0, tzinfo=UTC),
        updated_at=datetime(2026, 6, 27, 8, 30, tzinfo=UTC),
        messages=[{"role": "user"}],
        total_tokens=64,
        total_cost_usd=0.006,
        status="active",
    )
    engine.session_store = _FakeSessionStoreWithLatest([broken_session, ready_session])
    request = _fake_status_request(engine)

    response = await get_workbench_bootstrap(request, page_size=2, auth="test")

    assert [session["id"] for session in response.sessions] == [
        "sess-broken",
        "sess-ready",
    ]
    assert response.selected_session_id == "sess-ready"
    assert response.snapshot is not None
    assert response.snapshot["session_id"] == "sess-ready"
    assert engine.loaded == ["sess-broken", "sess-ready"]


@pytest.mark.asyncio
async def test_workbench_bootstrap_keeps_daemon_ready_when_snapshot_fails() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_dashboard_snapshot_error(
        RuntimeError("snapshot backend unavailable")
    )
    latest_session = SimpleNamespace(
        id="sess-snapshot-fails",
        title="快照失败会话",
        model="gpt-5",
        created_at=datetime(2026, 6, 27, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 6, 27, 9, 0, tzinfo=UTC),
        messages=[],
        total_tokens=0,
        total_cost_usd=0.0,
        status="active",
    )
    engine.session_store = _FakeSessionStoreWithLatest([latest_session])
    request = _fake_status_request(engine)

    response = await get_workbench_bootstrap(request, auth="test")

    assert response.sessions[0]["id"] == "sess-snapshot-fails"
    assert response.selected_session_id is None
    assert response.snapshot is None
    assert response.daemon_status.status == "running"
    assert engine.loaded == ["sess-snapshot-fails"]


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
async def test_create_workbench_session_returns_selected_bootstrap_snapshot() -> None:
    engine = _FakeEngine(exists=False)
    request = _fake_status_request(engine)
    body = WorkbenchSessionCreate(
        title="Mac 工作台",
        model="kimi-for-coding",
        system_prompt="中文优先",
    )

    response = await create_workbench_session(body, request, auth="test")

    assert engine.session_store.created_sessions == [
        {
            "title": "Mac 工作台",
            "model": "kimi-for-coding",
            "system_prompt": "中文优先",
        }
    ]
    assert response.selected_session_id == "sess-created"
    assert response.total_sessions == 1
    assert response.sessions[0]["id"] == "sess-created"
    assert response.snapshot is not None
    assert response.snapshot["session_id"] == "sess-created"
    assert response.capabilities.supported_locales == ["zh-CN", "en-US"]
    assert engine.loaded == ["sess-created"]


@pytest.mark.asyncio
async def test_create_workbench_session_reports_session_store_failure() -> None:
    engine = _FakeEngine(exists=False)
    engine.session_store.create_session_error = RuntimeError("session store unavailable")

    with pytest.raises(HTTPException) as exc:
        await create_workbench_session(
            WorkbenchSessionCreate(title="Mac 工作台"),
            _fake_status_request(engine),
            auth="test",
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "session store unavailable"
    assert engine.loaded == []


@pytest.mark.asyncio
async def test_create_workbench_session_reports_unloadable_created_session() -> None:
    engine = _FakeEngine(exists=False, load_session_result=False)

    with pytest.raises(HTTPException) as exc:
        await create_workbench_session(
            WorkbenchSessionCreate(title="Mac 工作台"),
            _fake_status_request(engine),
            auth="test",
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "会话创建后无法加载"
    assert engine.loaded == ["sess-created"]


@pytest.mark.asyncio
async def test_create_workbench_session_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=False,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await create_workbench_session(
            WorkbenchSessionCreate(title="Mac 工作台"),
            _fake_status_request(engine),
            auth="test",
        )

    assert engine.session_store.created_sessions == [
        {
            "title": "Mac 工作台",
            "model": None,
            "system_prompt": None,
        }
    ]
    assert engine.loaded == ["sess-created"]
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_create_workbench_session_defaults_blank_title() -> None:
    engine = _FakeEngine(exists=False)

    await create_workbench_session(
        WorkbenchSessionCreate(title="   "),
        _fake_status_request(engine),
        auth="test",
    )

    assert engine.session_store.created_sessions[0]["title"] == "Mac 工作台"


def test_create_workbench_session_route_accepts_json_body() -> None:
    engine = _FakeEngine(exists=False)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions",
        json={
            "title": "Mac 工作台",
            "model": "kimi-for-coding",
            "system_prompt": "中文优先",
        },
    )

    assert response.status_code == 201
    assert engine.session_store.created_sessions == [
        {
            "title": "Mac 工作台",
            "model": "kimi-for-coding",
            "system_prompt": "中文优先",
        }
    ]
    body = response.json()
    assert body["selected_session_id"] == "sess-created"
    assert body["sessions"][0]["id"] == "sess-created"
    assert body["snapshot"]["session_id"] == "sess-created"
    assert body["capabilities"]["supported_locales"] == ["zh-CN", "en-US"]


def test_list_workbench_sessions_route_returns_session_registry() -> None:
    session = SimpleNamespace(
        id="sess-workbench",
        title="Mac 工作台",
        model="gpt-5",
        created_at=datetime(2026, 6, 27, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 6, 27, 9, 0, tzinfo=UTC),
        messages=[{"role": "user"}, {"role": "assistant"}],
        total_tokens=128,
        total_cost_usd=0.012,
        status="active",
    )
    engine = _FakeEngine(exists=True)
    engine.session_store = _FakeSessionStoreWithLatest([session])
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions?page=1&page_size=1")

    assert response.status_code == 200
    assert engine.session_store.list_requests == [
        {"page": 1, "page_size": 1, "query": ""}
    ]
    body = response.json()
    assert body == {
        "sessions": [
            {
                "id": "sess-workbench",
                "title": "Mac 工作台",
                "model": "gpt-5",
                "created_at": "2026-06-27T08:00:00+00:00",
                "updated_at": "2026-06-27T09:00:00+00:00",
                "message_count": 2,
                "total_tokens": 128,
                "total_cost_usd": 0.012,
                "status": "active",
            }
        ],
        "total": 1,
        "page": 1,
        "page_size": 1,
    }


def test_list_workbench_sessions_route_reports_registry_failure() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.list_sessions_error = RuntimeError("session registry unavailable")
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions?page=1&page_size=20")

    assert response.status_code == 503
    assert response.json() == {"detail": "session registry unavailable"}


@pytest.mark.asyncio
async def test_run_validation_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = ValidationRunCreate(task_id="task-1", argv=["pytest"])

    with pytest.raises(HTTPException) as exc:
        await create_validation_run("missing", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_run_validation_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    body = ValidationRunCreate(task_id="task-1", argv=["pytest"])

    with pytest.raises(HTTPException) as exc:
        await create_validation_run("sess-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.run_validations == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_run_validation_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )
    body = ValidationRunCreate(task_id="task-1", argv=["pytest"])

    with pytest.raises(HTTPException) as exc:
        await create_validation_run("sess-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.run_validations == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_run_validation_endpoint_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    body = ValidationRunCreate(
        task_id="task-1",
        actor="Human",
        argv=["pytest", "test.py"],
        cwd="/workspace",
    )

    response = await create_validation_run(
        "sess-1",
        body,
        _fake_request(engine),
        include_snapshot=True,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert response["validation_run"] == {
        "id": "run-1",
        "status": "passed",
        "exit_code": 0,
        "output": "ok",
    }
    assert response["snapshot"]["version"] == 1
    assert response["snapshot"]["session_id"] == "sess-1"


def test_run_validation_route_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/validation-runs",
        params={"include_snapshot": "true"},
        json={
            "task_id": "task-1",
            "actor": "Human",
            "argv": ["pytest", "test.py"],
            "cwd": "/workspace",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["validation_run"] == {
        "id": "run-1",
        "status": "passed",
        "exit_code": 0,
        "output": "ok",
    }
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


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
async def test_create_intent_lock_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    body = IntentLockCreate(rule="禁止修改 core 模块")

    with pytest.raises(HTTPException) as exc:
        await create_intent_lock(
            "sess-1", "mission-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == []
    assert engine.workbench_service.created_intent_locks == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_create_intent_lock_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )
    body = IntentLockCreate(
        actor="Planner-Agent",
        rule="禁止修改 src/secret 下文件",
        blocked_paths=["src/secret"],
        allowed_paths=["src/secret/README.md"],
        require_proposal_for_risk=RiskLevel.HIGH,
    )

    with pytest.raises(HTTPException) as exc:
        await create_intent_lock(
            "sess-1", "mission-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.created_intent_locks == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_create_intent_lock_endpoint_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    body = IntentLockCreate(
        actor="Planner-Agent",
        rule="高风险文件需要人工审批",
        blocked_paths=["src/core"],
        allowed_paths=["src/core/README.md"],
        require_proposal_for_risk=RiskLevel.HIGH,
    )

    response = await create_intent_lock(
        "sess-1",
        "mission-1",
        body,
        _fake_request(engine),
        include_snapshot=True,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert response["intent_lock"]["id"] == "lock-1"
    assert response["intent_lock"]["require_proposal_for_risk"] == "high"
    assert response["snapshot"]["version"] == 1
    assert response["snapshot"]["session_id"] == "sess-1"


def test_create_intent_lock_route_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/missions/mission-1/intent-locks",
        params={"include_snapshot": "true"},
        json={
            "actor": "Planner-Agent",
            "rule": "高风险文件需要人工审批",
            "blocked_paths": ["src/core"],
            "allowed_paths": ["src/core/README.md"],
            "require_proposal_for_risk": "high",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["intent_lock"]["id"] == "lock-1"
    assert body["intent_lock"]["require_proposal_for_risk"] == "high"
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


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
async def test_create_intent_lock_endpoint_maps_runtime_error_to_503() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_intent_lock_error(RuntimeError("意图锁存储暂不可用"))
    body = IntentLockCreate(rule="高风险改动需要人工审批")

    with pytest.raises(HTTPException) as exc:
        await create_intent_lock("sess-1", "mission-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 503
    assert exc.value.detail == "意图锁存储暂不可用"


@pytest.mark.asyncio
async def test_create_decision_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = DecisionCreate(title="采用 FastAPI", content="使用 FastAPI 承载 Workbench API")

    with pytest.raises(HTTPException) as exc:
        await create_decision("missing", "mission-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_create_decision_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    body = DecisionCreate(title="采用 SwiftUI 原生壳", content="保持 Mac 体验")

    with pytest.raises(HTTPException) as exc:
        await create_decision("sess-1", "mission-1", body, _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.created_decisions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_create_decision_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )
    body = DecisionCreate(
        actor="Planner-Agent",
        kind=DecisionKind.ARCHITECTURE,
        title="采用 SwiftUI 原生壳",
        content="保持 Mac 体验",
    )

    with pytest.raises(HTTPException) as exc:
        await create_decision(
            "sess-1", "mission-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.created_decisions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_create_decision_endpoint_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    body = DecisionCreate(
        actor="Reviewer-Agent",
        kind=DecisionKind.POLICY,
        title="采用人工审批闸门",
        content="高风险变更必须进入审批队列",
    )

    response = await create_decision(
        "sess-1",
        "mission-1",
        body,
        _fake_request(engine),
        include_snapshot=True,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert response["decision"]["id"] == "decision-1"
    assert response["decision"]["kind"] == "policy"
    assert response["snapshot"]["version"] == 1
    assert response["snapshot"]["session_id"] == "sess-1"


def test_create_decision_route_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/missions/mission-1/decisions",
        params={"include_snapshot": "true"},
        json={
            "actor": "Reviewer-Agent",
            "kind": "policy",
            "title": "采用人工审批闸门",
            "content": "高风险变更必须进入审批队列",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["decision"]["id"] == "decision-1"
    assert body["decision"]["kind"] == "policy"
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


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
async def test_create_decision_endpoint_maps_runtime_error_to_503() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_decision_error(RuntimeError("决策日志暂不可用"))
    body = DecisionCreate(title="采用 FastAPI", content="使用 FastAPI 承载 Workbench API")

    with pytest.raises(HTTPException) as exc:
        await create_decision("sess-1", "mission-1", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 503
    assert exc.value.detail == "决策日志暂不可用"


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
async def test_get_issues_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_issues(
            "sess-1",
            _fake_request(engine),
            mission_id=None,
            risk_level=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == []
    assert engine.workbench_service.listed_issues == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_issues_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_issues(
            "sess-1",
            _fake_request(engine),
            mission_id=None,
            risk_level=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_issues == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_get_issues_endpoint_reports_invalid_issue_filter() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_issues_error(
        ValueError("issue risk filter is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_issues(
            "sess-1",
            _fake_request(engine),
            mission_id="mission-2",
            risk_level="bad-risk",
            limit=25,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_issues == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-2",
            "risk_level": "bad-risk",
            "limit": 25,
        }
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "issue risk filter is invalid"


@pytest.mark.asyncio
async def test_get_issues_endpoint_reports_unavailable_issue_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_issues_error(
        RuntimeError("issue store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_issues(
            "sess-1",
            _fake_request(engine),
            mission_id=None,
            risk_level=None,
            limit=25,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_issues == [
        {"session_id": "sess-1", "mission_id": None, "risk_level": None, "limit": 25}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "issue store unavailable"


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
async def test_get_agent_profiles_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_agent_profiles(
            "sess-1", _fake_request(engine), status=None, limit=10, auth="test"
        )

    assert engine.loaded == []
    assert engine.workbench_service.listed_agent_profiles == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_agent_profiles_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_agent_profiles(
            "sess-1", _fake_request(engine), status=None, limit=10, auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_agent_profiles == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_get_agent_profiles_endpoint_reports_invalid_profile_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_agent_profiles_error(
        ValueError("agent status filter is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_agent_profiles(
            "sess-1", _fake_request(engine), status="invalid", limit=10, auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_agent_profiles == []
    assert exc.value.status_code == 400
    assert exc.value.detail == "agent status filter is invalid"


@pytest.mark.asyncio
async def test_get_agent_profiles_endpoint_reports_unavailable_profile_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_agent_profiles_error(
        RuntimeError("agent profile store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_agent_profiles(
            "sess-1", _fake_request(engine), status=None, limit=10, auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_agent_profiles == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "agent profile store unavailable"


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
async def test_upsert_agent_profile_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    body = AgentProfileUpsert(name="Backend Agent", role="coder")

    with pytest.raises(HTTPException) as exc:
        await upsert_agent_profile(
            "sess-1", "agent-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == []
    assert engine.workbench_service.registered_agent_profiles == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_upsert_agent_profile_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )
    body = AgentProfileUpsert(name="Backend Agent", role="coder")

    with pytest.raises(HTTPException) as exc:
        await upsert_agent_profile(
            "sess-1", "agent-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.registered_agent_profiles == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_upsert_agent_profile_endpoint_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    body = AgentProfileUpsert(
        name="Backend Agent",
        role="coder",
        capabilities=["code"],
        permissions=["read"],
        max_parallel_tasks=2,
        status="busy",
        actor="Human",
    )

    response = await upsert_agent_profile(
        "sess-1",
        "agent-1",
        body,
        _fake_request(engine),
        include_snapshot=True,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert response["agent_profile"]["id"] == "agent-1"
    assert response["agent_profile"]["status"] == "busy"
    assert response["snapshot"]["version"] == 1
    assert response["snapshot"]["session_id"] == "sess-1"


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


@pytest.mark.asyncio
async def test_upsert_agent_profile_endpoint_reports_unavailable_profile_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_agent_profile_error(
        RuntimeError("agent profile store unavailable")
    )
    body = AgentProfileUpsert(name="Backend Agent", role="coder")

    with pytest.raises(HTTPException) as exc:
        await upsert_agent_profile(
            "sess-1", "agent-1", body, _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.registered_agent_profiles == [
        {
            "session_id": "sess-1",
            "agent_id": "agent-1",
            "name": "Backend Agent",
            "role": "coder",
            "capabilities": [],
            "permissions": [],
            "max_parallel_tasks": 1,
            "status": "idle",
            "actor": "Human",
        }
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "agent profile store unavailable"


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


def test_upsert_agent_profile_route_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/agents/agent-1",
        params={"include_snapshot": "true"},
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
    body = response.json()
    assert body["agent_profile"]["id"] == "agent-1"
    assert body["agent_profile"]["status"] == "busy"
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


def test_get_agent_profile_route_returns_single_profile() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/agents/agent-2")

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_agent_profiles == [
        {"session_id": "sess-1", "agent_id": "agent-2"}
    ]
    assert response.json() == {
        "id": "agent-2",
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


def test_get_agent_profile_route_returns_404_for_missing_profile() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/agents/missing-agent")

    assert response.status_code == 404
    assert response.json() == {"detail": "智能体不存在"}
    assert engine.workbench_service.requested_agent_profiles == [
        {"session_id": "sess-1", "agent_id": "missing-agent"}
    ]


@pytest.mark.asyncio
async def test_get_agent_profile_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_agent_profile(
            "sess-1", "agent-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == []
    assert engine.workbench_service.requested_agent_profiles == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_agent_profile_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_agent_profile(
            "sess-1", "agent-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_agent_profiles == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_agent_profile_endpoint_reports_invalid_profile_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_agent_profile_error(
        ValueError("agent id is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_agent_profile(
            "sess-1", "agent-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_agent_profiles == [
        {"session_id": "sess-1", "agent_id": "agent-2"}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "agent id is invalid"


@pytest.mark.asyncio
async def test_get_agent_profile_endpoint_reports_unavailable_profile_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_agent_profile_error(
        RuntimeError("agent profile detail store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_agent_profile(
            "sess-1", "agent-2", _fake_request(engine), auth="test"
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_agent_profiles == [
        {"session_id": "sess-1", "agent_id": "agent-2"}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "agent profile detail store unavailable"


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


def test_create_issue_route_can_return_fresh_snapshot() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/missions/mission-1/issues",
        params={"include_snapshot": "true"},
        json={
            "title": "实现 Issue 创建 API",
            "acceptance_criteria": ["dashboard 刷新后可见"],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["issue"]["task_id"] == "task-9"
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


def test_get_issue_route_returns_single_issue_detail() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/issues/task-2")

    assert response.status_code == 200
    assert engine.workbench_service.requested_issues == [
        {"session_id": "sess-1", "task_id": "task-2"}
    ]
    assert response.json() == {
        "session_id": "sess-1",
        "task_id": "task-2",
        "mission_id": "mission-1",
        "parallel_mode": "cooperative",
        "risk_level": "high",
        "requires_human_approval": True,
        "acceptance_criteria": ["详情页不依赖全量 snapshot"],
        "expected_artifacts": [],
        "related_branch": "issue-detail",
        "related_worktree": "wt-issue-detail",
        "related_pr": "",
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }


def test_get_issue_route_returns_404_for_missing_issue() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/issues/missing-task")

    assert response.status_code == 404
    assert response.json()["detail"] == "issue 不存在"
    assert engine.workbench_service.requested_issues == [
        {"session_id": "sess-1", "task_id": "missing-task"}
    ]


@pytest.mark.asyncio
async def test_get_issue_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_issue("sess-1", "task-2", _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.requested_issues == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_issue_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_issue("sess-1", "task-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_issues == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_issue_endpoint_reports_invalid_issue_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_issue_error(ValueError("issue id is invalid"))

    with pytest.raises(HTTPException) as exc:
        await get_issue("sess-1", "task-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_issues == [
        {"session_id": "sess-1", "task_id": "task-2"}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "issue id is invalid"


@pytest.mark.asyncio
async def test_get_issue_endpoint_reports_unavailable_issue_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_issue_error(
        RuntimeError("issue store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_issue("sess-1", "task-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_issues == [
        {"session_id": "sess-1", "task_id": "task-2"}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "issue store unavailable"


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
async def test_get_leases_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_leases(
            "sess-1",
            _fake_request(engine),
            state=None,
            task_id=None,
            agent_id=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == []
    assert engine.workbench_service.listed_leases == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_leases_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_leases(
            "sess-1",
            _fake_request(engine),
            state=None,
            task_id=None,
            agent_id=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_leases == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_get_leases_endpoint_reports_invalid_lease_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_leases_error(
        ValueError("lease filter is invalid")
    )

    with pytest.raises(HTTPException) as exc:
        await get_leases(
            "sess-1",
            _fake_request(engine),
            state="invalid",
            task_id=None,
            agent_id=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_leases == []
    assert exc.value.status_code == 400
    assert exc.value.detail == "lease filter is invalid"


@pytest.mark.asyncio
async def test_get_leases_endpoint_reports_unavailable_lease_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_leases_error(
        RuntimeError("lease store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_leases(
            "sess-1",
            _fake_request(engine),
            state=None,
            task_id=None,
            agent_id=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_leases == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "lease store unavailable"


def test_get_lease_route_returns_single_lease() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/leases/lease-2")

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_leases == [
        {"session_id": "sess-1", "lease_id": "lease-2"}
    ]
    assert response.json() == {
        "id": "lease-2",
        "session_id": "sess-1",
        "task_id": "task-1",
        "agent_id": "agent-1",
        "state": "active",
        "expires_at": "2024-01-01T01:00:00",
        "worktree_name": "wt-1",
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }


def test_get_lease_route_returns_404_for_missing_lease() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/leases/missing-lease")

    assert response.status_code == 404
    assert response.json() == {"detail": "租约不存在"}
    assert engine.workbench_service.requested_leases == [
        {"session_id": "sess-1", "lease_id": "missing-lease"}
    ]


@pytest.mark.asyncio
async def test_get_lease_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_lease("sess-1", "lease-2", _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.requested_leases == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_lease_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True, load_session_error=RuntimeError("运行态会话暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_lease("sess-1", "lease-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_leases == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_lease_endpoint_reports_invalid_lease_request() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_lease_error(ValueError("lease id is invalid"))

    with pytest.raises(HTTPException) as exc:
        await get_lease("sess-1", "lease-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_leases == [
        {"session_id": "sess-1", "lease_id": "lease-2"}
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "lease id is invalid"


@pytest.mark.asyncio
async def test_get_lease_endpoint_reports_unavailable_lease_service() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_lease_error(
        RuntimeError("lease detail store unavailable")
    )

    with pytest.raises(HTTPException) as exc:
        await get_lease("sess-1", "lease-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_leases == [
        {"session_id": "sess-1", "lease_id": "lease-2"}
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "lease detail store unavailable"


@pytest.mark.asyncio
async def test_get_worktrees_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_worktrees(
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
async def test_get_worktrees_endpoint_reports_unavailable_session_store() -> None:
    worktree_manager = FakeWorktreeManager()
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_worktrees(
            "sess-1",
            _fake_request(engine),
            task_id=None,
            status=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == []
    assert worktree_manager.status_calls == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_worktrees_endpoint_reports_runtime_session_load_failure() -> None:
    worktree_manager = FakeWorktreeManager()
    engine = _FakeEngine(
        exists=True,
        worktree_manager=worktree_manager,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await get_worktrees(
            "sess-1",
            _fake_request(engine),
            task_id=None,
            status=None,
            limit=10,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert worktree_manager.status_calls == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_worktree_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_worktree(
            "missing",
            "wt-api",
            _fake_request(engine),
            auth="test",
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_worktree_endpoint_reports_unavailable_session_store() -> None:
    worktree_manager = FakeWorktreeManager()
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_worktree(
            "sess-1",
            "wt-api",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == []
    assert worktree_manager.status_calls == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_worktree_endpoint_reports_runtime_session_load_failure() -> None:
    worktree_manager = FakeWorktreeManager()
    engine = _FakeEngine(
        exists=True,
        worktree_manager=worktree_manager,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await get_worktree(
            "sess-1",
            "wt-api",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert worktree_manager.status_calls == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_keep_worktree_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)
    body = WorktreeKeep(actor="Human", reason="等待人工审查")

    with pytest.raises(HTTPException) as exc:
        await keep_worktree("missing", "wt-api", body, _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_keep_worktree_endpoint_reports_unavailable_session_store() -> None:
    worktree_manager = FakeWorktreeManager()
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")
    body = WorktreeKeep(actor="Human", reason="等待人工审查")

    with pytest.raises(HTTPException) as exc:
        await keep_worktree("sess-1", "wt-api", body, _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert worktree_manager.keep_calls == []
    assert worktree_manager.status_calls == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_keep_worktree_endpoint_reports_runtime_session_load_failure() -> None:
    worktree_manager = FakeWorktreeManager()
    engine = _FakeEngine(
        exists=True,
        worktree_manager=worktree_manager,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )
    body = WorktreeKeep(actor="Human", reason="等待人工审查")

    with pytest.raises(HTTPException) as exc:
        await keep_worktree("sess-1", "wt-api", body, _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert worktree_manager.keep_calls == []
    assert worktree_manager.status_calls == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_worktrees_endpoint_filters_and_returns_json_ready_records() -> None:
    worktree_manager = FakeWorktreeManager(
        [
            WorktreeRecord(
                name="wt-api",
                path="/repo/.naumi/worktrees/wt-api",
                branch="naumi/worktree-wt-api",
                base_ref="abc123",
                status=WorktreeStatus.CLEAN,
                task_id="task-1",
                dirty_files=0,
                commits_ahead=0,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:00:00",
                metadata={"owner": "Backend-Agent"},
            ),
            WorktreeRecord(
                name="wt-dirty",
                path="/repo/.naumi/worktrees/wt-dirty",
                branch="naumi/worktree-wt-dirty",
                base_ref="def456",
                status=WorktreeStatus.DIRTY,
                task_id="task-2",
                dirty_files=3,
                commits_ahead=1,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:01:00",
            ),
        ]
    )
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)

    response = await get_worktrees(
        "sess-1",
        _fake_request(engine),
        task_id="task-1",
        status="clean",
        limit=10,
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert worktree_manager.status_calls == [""]
    assert response.model_dump() == {
        "worktrees": [
            {
                "name": "wt-api",
                "path": "/repo/.naumi/worktrees/wt-api",
                "branch": "naumi/worktree-wt-api",
                "base_ref": "abc123",
                "status": "clean",
                "task_id": "task-1",
                "dirty_files": 0,
                "commits_ahead": 0,
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
                "kept_reason": "",
                "metadata": {"owner": "Backend-Agent"},
                "removable": True,
            }
        ],
        "task_id": "task-1",
        "status": "clean",
        "limit": 10,
    }


def test_get_worktrees_route_accepts_filters() -> None:
    worktree_manager = FakeWorktreeManager(
        [
            WorktreeRecord(
                name="wt-kept",
                path="/repo/.naumi/worktrees/wt-kept",
                branch="naumi/worktree-wt-kept",
                base_ref="abc123",
                status=WorktreeStatus.KEPT,
                task_id="task-1",
                dirty_files=0,
                commits_ahead=2,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:03:00",
                kept_reason="等待人工审查",
            ),
            WorktreeRecord(
                name="wt-clean",
                path="/repo/.naumi/worktrees/wt-clean",
                branch="naumi/worktree-wt-clean",
                base_ref="def456",
                status=WorktreeStatus.CLEAN,
                task_id="task-2",
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:02:00",
            ),
        ]
    )
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/worktrees?task_id=task-1&status=kept"
    )

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert response.json() == {
        "worktrees": [
            {
                "name": "wt-kept",
                "path": "/repo/.naumi/worktrees/wt-kept",
                "branch": "naumi/worktree-wt-kept",
                "base_ref": "abc123",
                "status": "kept",
                "task_id": "task-1",
                "dirty_files": 0,
                "commits_ahead": 2,
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:03:00",
                "kept_reason": "等待人工审查",
                "metadata": {},
                "removable": False,
            }
        ],
        "task_id": "task-1",
        "status": "kept",
        "limit": 50,
    }


def test_get_worktree_route_returns_single_worktree() -> None:
    worktree_manager = FakeWorktreeManager(
        [
            WorktreeRecord(
                name="wt-api",
                path="/repo/.naumi/worktrees/wt-api",
                branch="naumi/worktree-wt-api",
                base_ref="abc123",
                status=WorktreeStatus.DIRTY,
                task_id="task-1",
                dirty_files=2,
                commits_ahead=1,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:04:00",
                metadata={"agent_id": "Backend-Agent"},
            )
        ]
    )
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/worktrees/wt-api")

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert worktree_manager.status_calls == ["wt-api"]
    assert response.json() == {
        "name": "wt-api",
        "path": "/repo/.naumi/worktrees/wt-api",
        "branch": "naumi/worktree-wt-api",
        "base_ref": "abc123",
        "status": "dirty",
        "task_id": "task-1",
        "dirty_files": 2,
        "commits_ahead": 1,
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:04:00",
        "kept_reason": "",
        "metadata": {"agent_id": "Backend-Agent"},
        "removable": False,
    }


def test_get_worktree_route_returns_404_for_missing_worktree() -> None:
    engine = _FakeEngine(exists=True, worktree_manager=FakeWorktreeManager())
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/worktrees/missing-worktree")

    assert response.status_code == 404
    assert response.json() == {"detail": "worktree 不存在"}
    assert engine.worktree_manager.status_calls == ["missing-worktree"]


def test_get_worktree_route_maps_invalid_name_to_400() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get("/workbench/sessions/sess-1/worktrees/bad/name")

    assert response.status_code == 400
    assert response.json() == {"detail": "worktree 名称不能包含路径分隔符"}
    assert engine.worktree_manager.status_calls == ["bad/name"]


def test_keep_worktree_route_marks_worktree_kept_and_records_audit_event() -> None:
    worktree_manager = FakeWorktreeManager(
        [
            WorktreeRecord(
                name="wt-api",
                path="/repo/.naumi/worktrees/wt-api",
                branch="naumi/worktree-wt-api",
                base_ref="abc123",
                status=WorktreeStatus.DIRTY,
                task_id="task-1",
                dirty_files=2,
                commits_ahead=1,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:04:00",
            )
        ]
    )
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/worktrees/wt-api/keep",
        json={"actor": "Reviewer-Agent", "reason": "等待人工审查"},
    )

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert worktree_manager.keep_calls == [
        {"name": "wt-api", "reason": "等待人工审查"}
    ]
    assert response.json()["status"] == "kept"
    assert response.json()["kept_reason"] == "等待人工审查"
    assert response.json()["removable"] is False
    assert engine.workbench_store.events == [
        {
            "session_id": "sess-1",
            "type": "worktree.kept",
            "actor": "Reviewer-Agent",
            "subject_id": "wt-api",
            "payload": {"reason": "等待人工审查"},
        }
    ]


def test_keep_worktree_route_can_return_fresh_snapshot() -> None:
    worktree_manager = FakeWorktreeManager(
        [
            WorktreeRecord(
                name="wt-api",
                path="/repo/.naumi/worktrees/wt-api",
                branch="naumi/worktree-wt-api",
                base_ref="abc123",
                status=WorktreeStatus.DIRTY,
                task_id="task-1",
                dirty_files=2,
                commits_ahead=1,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:04:00",
            )
        ]
    )
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/worktrees/wt-api/keep",
        params={"include_snapshot": "true"},
        json={"actor": "Reviewer-Agent", "reason": "等待人工审查"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["worktree"]["name"] == "wt-api"
    assert body["worktree"]["status"] == "kept"
    assert body["worktree"]["kept_reason"] == "等待人工审查"
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


def test_keep_worktree_route_returns_404_for_missing_worktree() -> None:
    engine = _FakeEngine(exists=True, worktree_manager=FakeWorktreeManager())
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.post(
        "/workbench/sessions/sess-1/worktrees/missing-worktree/keep",
        json={"reason": "等待人工审查"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "worktree 不存在"}
    assert engine.worktree_manager.keep_calls == [
        {"name": "missing-worktree", "reason": "等待人工审查"}
    ]


@pytest.mark.asyncio
async def test_delete_worktree_endpoint_reports_unavailable_session_store() -> None:
    worktree_manager = FakeWorktreeManager()
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await delete_worktree(
            "sess-1",
            "wt-clean",
            _fake_request(engine),
            discard_changes=False,
            auth="test",
        )

    assert engine.loaded == []
    assert worktree_manager.remove_calls == []
    assert worktree_manager.status_calls == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_delete_worktree_endpoint_reports_runtime_session_load_failure() -> None:
    worktree_manager = FakeWorktreeManager()
    engine = _FakeEngine(
        exists=True,
        worktree_manager=worktree_manager,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await delete_worktree(
            "sess-1",
            "wt-clean",
            _fake_request(engine),
            discard_changes=False,
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert worktree_manager.remove_calls == []
    assert worktree_manager.status_calls == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


def test_delete_worktree_route_removes_worktree_and_records_audit_event() -> None:
    worktree_manager = FakeWorktreeManager(
        [
            WorktreeRecord(
                name="wt-clean",
                path="/repo/.naumi/worktrees/wt-clean",
                branch="naumi/worktree-wt-clean",
                base_ref="abc123",
                status=WorktreeStatus.CLEAN,
                task_id="task-1",
                dirty_files=0,
                commits_ahead=0,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:04:00",
            )
        ]
    )
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.delete("/workbench/sessions/sess-1/worktrees/wt-clean")

    assert response.status_code == 200
    assert response.json() == {
        "name": "wt-clean",
        "discard_changes": False,
        "message": "已删除 worktree：wt-clean",
    }
    assert worktree_manager.remove_calls == [
        {"name": "wt-clean", "discard_changes": False}
    ]
    assert engine.workbench_store.events == [
        {
            "session_id": "sess-1",
            "type": "worktree.removed",
            "actor": "Human",
            "subject_id": "wt-clean",
            "payload": {"discard_changes": False},
        }
    ]


def test_delete_worktree_route_can_return_fresh_snapshot() -> None:
    worktree_manager = FakeWorktreeManager(
        [
            WorktreeRecord(
                name="wt-clean",
                path="/repo/.naumi/worktrees/wt-clean",
                branch="naumi/worktree-wt-clean",
                base_ref="abc123",
                status=WorktreeStatus.CLEAN,
                task_id="task-1",
                dirty_files=0,
                commits_ahead=0,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:04:00",
            )
        ]
    )
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.delete(
        "/workbench/sessions/sess-1/worktrees/wt-clean",
        params={"include_snapshot": "true"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["removal"] == {
        "name": "wt-clean",
        "discard_changes": False,
        "message": "已删除 worktree：wt-clean",
    }
    assert body["snapshot"]["version"] == 1
    assert body["snapshot"]["session_id"] == "sess-1"


def test_delete_worktree_route_rejects_dirty_without_discard_changes() -> None:
    worktree_manager = FakeWorktreeManager(
        [
            WorktreeRecord(
                name="wt-dirty",
                path="/repo/.naumi/worktrees/wt-dirty",
                branch="naumi/worktree-wt-dirty",
                base_ref="abc123",
                status=WorktreeStatus.DIRTY,
                task_id="task-1",
                dirty_files=2,
                commits_ahead=1,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:04:00",
            )
        ]
    )
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.delete("/workbench/sessions/sess-1/worktrees/wt-dirty")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "拒绝删除：worktree 中仍有未保存或未审查的工作。"
    }
    assert worktree_manager.remove_calls == [
        {"name": "wt-dirty", "discard_changes": False}
    ]
    assert engine.workbench_store.events == []


def test_delete_worktree_route_force_removes_dirty_worktree() -> None:
    worktree_manager = FakeWorktreeManager(
        [
            WorktreeRecord(
                name="wt-dirty",
                path="/repo/.naumi/worktrees/wt-dirty",
                branch="naumi/worktree-wt-dirty",
                base_ref="abc123",
                status=WorktreeStatus.DIRTY,
                task_id="task-1",
                dirty_files=2,
                commits_ahead=1,
                created_at="2024-01-01T00:00:00",
                updated_at="2024-01-01T00:04:00",
            )
        ]
    )
    engine = _FakeEngine(exists=True, worktree_manager=worktree_manager)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.delete(
        "/workbench/sessions/sess-1/worktrees/wt-dirty?discard_changes=true"
    )

    assert response.status_code == 200
    assert response.json() == {
        "name": "wt-dirty",
        "discard_changes": True,
        "message": "已删除 worktree：wt-dirty",
    }
    assert worktree_manager.remove_calls == [
        {"name": "wt-dirty", "discard_changes": True}
    ]


@pytest.mark.asyncio
async def test_get_intent_locks_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_intent_locks("missing", "mission-1", _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_intent_locks_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_intent_locks("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.listed_intent_locks == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_intent_locks_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await get_intent_locks("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_intent_locks == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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
async def test_get_intent_locks_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_intent_locks_error(
        ValueError("意图锁查询参数无效")
    )

    with pytest.raises(HTTPException) as exc:
        await get_intent_locks("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_intent_locks == []
    assert exc.value.status_code == 400
    assert exc.value.detail == "意图锁查询参数无效"


@pytest.mark.asyncio
async def test_get_intent_locks_endpoint_maps_runtime_error_to_503() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_intent_locks_error(
        RuntimeError("意图锁存储暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_intent_locks("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_intent_locks == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "意图锁存储暂不可用"


@pytest.mark.asyncio
async def test_get_intent_lock_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_intent_lock(
            "missing",
            "mission-1",
            "lock-1",
            _fake_request(engine),
            auth="test",
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_intent_lock_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_intent_lock(
            "sess-1",
            "mission-2",
            "lock-2",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == []
    assert engine.workbench_service.requested_intent_locks == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_intent_lock_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await get_intent_lock(
            "sess-1",
            "mission-2",
            "lock-2",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_intent_locks == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_intent_lock_endpoint_returns_lock_detail() -> None:
    engine = _FakeEngine(exists=True)

    response = await get_intent_lock(
        "sess-1",
        "mission-2",
        "lock-2",
        _fake_request(engine),
        auth="test",
    )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_intent_locks == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-2",
            "lock_id": "lock-2",
        }
    ]
    assert response == {
        "id": "lock-2",
        "session_id": "sess-1",
        "mission_id": "mission-2",
        "rule": "禁止修改 core 模块",
        "blocked_paths": ["src/secret"],
        "allowed_paths": ["src/secret/README.md"],
        "require_proposal_for_risk": "high",
        "active": True,
        "created_at": "2024-01-01T00:00:00",
    }


@pytest.mark.asyncio
async def test_get_intent_lock_endpoint_returns_404_when_missing() -> None:
    engine = _FakeEngine(exists=True)

    with pytest.raises(HTTPException) as exc:
        await get_intent_lock(
            "sess-1",
            "mission-2",
            "missing-lock",
            _fake_request(engine),
            auth="test",
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "意图锁不存在"
    assert engine.workbench_service.requested_intent_locks == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-2",
            "lock_id": "missing-lock",
        }
    ]


@pytest.mark.asyncio
async def test_get_intent_lock_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_intent_lock_error(
        ValueError("意图锁标识无效")
    )

    with pytest.raises(HTTPException) as exc:
        await get_intent_lock(
            "sess-1",
            "mission-2",
            "bad-lock",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_intent_locks == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-2",
            "lock_id": "bad-lock",
        }
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "意图锁标识无效"


@pytest.mark.asyncio
async def test_get_intent_lock_endpoint_maps_runtime_error_to_503() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_intent_lock_error(
        RuntimeError("意图锁详情暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_intent_lock(
            "sess-1",
            "mission-2",
            "lock-2",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_intent_locks == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-2",
            "lock_id": "lock-2",
        }
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "意图锁详情暂不可用"


@pytest.mark.asyncio
async def test_get_decisions_endpoint_requires_existing_session() -> None:
    engine = _FakeEngine(exists=False)

    with pytest.raises(HTTPException) as exc:
        await get_decisions("missing", "mission-1", _fake_request(engine), auth="test")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_decisions_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_decisions("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == []
    assert engine.workbench_service.listed_decisions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_decisions_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await get_decisions("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_decisions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


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


@pytest.mark.asyncio
async def test_get_decisions_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_decisions_error(
        ValueError("决策查询参数无效")
    )

    with pytest.raises(HTTPException) as exc:
        await get_decisions("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_decisions == []
    assert exc.value.status_code == 400
    assert exc.value.detail == "决策查询参数无效"


@pytest.mark.asyncio
async def test_get_decisions_endpoint_maps_runtime_error_to_503() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_list_decisions_error(
        RuntimeError("决策日志暂不可用")
    )

    with pytest.raises(HTTPException) as exc:
        await get_decisions("sess-1", "mission-2", _fake_request(engine), auth="test")

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.listed_decisions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "决策日志暂不可用"


@pytest.mark.asyncio
async def test_get_decision_endpoint_reports_unavailable_session_store() -> None:
    engine = _FakeEngine(exists=True)
    engine.session_store.load_error = RuntimeError("会话存储暂不可用")

    with pytest.raises(HTTPException) as exc:
        await get_decision(
            "sess-1",
            "mission-2",
            "decision-2",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == []
    assert engine.workbench_service.requested_decisions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "会话存储暂不可用"


@pytest.mark.asyncio
async def test_get_decision_endpoint_reports_runtime_session_load_failure() -> None:
    engine = _FakeEngine(
        exists=True,
        load_session_error=RuntimeError("运行态会话暂不可用"),
    )

    with pytest.raises(HTTPException) as exc:
        await get_decision(
            "sess-1",
            "mission-2",
            "decision-2",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_decisions == []
    assert exc.value.status_code == 503
    assert exc.value.detail == "运行态会话暂不可用"


@pytest.mark.asyncio
async def test_get_decision_endpoint_maps_value_error_to_400() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_decision_error(ValueError("决策标识无效"))

    with pytest.raises(HTTPException) as exc:
        await get_decision(
            "sess-1",
            "mission-2",
            "bad-decision",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_decisions == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-2",
            "decision_id": "bad-decision",
        }
    ]
    assert exc.value.status_code == 400
    assert exc.value.detail == "决策标识无效"


@pytest.mark.asyncio
async def test_get_decision_endpoint_maps_runtime_error_to_503() -> None:
    engine = _FakeEngine(exists=True)
    engine.workbench_service.set_get_decision_error(RuntimeError("决策详情暂不可用"))

    with pytest.raises(HTTPException) as exc:
        await get_decision(
            "sess-1",
            "mission-2",
            "decision-2",
            _fake_request(engine),
            auth="test",
        )

    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_decisions == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-2",
            "decision_id": "decision-2",
        }
    ]
    assert exc.value.status_code == 503
    assert exc.value.detail == "决策详情暂不可用"


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


def test_get_intent_lock_route_returns_single_lock() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/missions/mission-1/intent-locks/lock-2"
    )

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_intent_locks == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "lock_id": "lock-2",
        }
    ]
    assert response.json() == {
        "id": "lock-2",
        "session_id": "sess-1",
        "mission_id": "mission-1",
        "rule": "禁止修改 core 模块",
        "blocked_paths": ["src/secret"],
        "allowed_paths": ["src/secret/README.md"],
        "require_proposal_for_risk": "high",
        "active": True,
        "created_at": "2024-01-01T00:00:00",
    }


def test_get_intent_lock_route_returns_404_for_missing_lock() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/missions/mission-1/intent-locks/missing-lock"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "意图锁不存在"}
    assert engine.workbench_service.requested_intent_locks == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "lock_id": "missing-lock",
        }
    ]


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


def test_get_decision_route_returns_single_decision() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/missions/mission-1/decisions/decision-2"
    )

    assert response.status_code == 200
    assert engine.loaded == ["sess-1"]
    assert engine.workbench_service.requested_decisions == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "decision_id": "decision-2",
        }
    ]
    assert response.json() == {
        "id": "decision-2",
        "session_id": "sess-1",
        "mission_id": "mission-1",
        "kind": "architecture",
        "title": "采用 FastAPI",
        "content": "使用 FastAPI 承载 Workbench API",
        "actor": "Planner-Agent",
        "created_at": "2024-01-01T00:00:00",
    }


def test_get_decision_route_returns_404_for_missing_decision() -> None:
    engine = _FakeEngine(exists=True)
    app = FastAPI()
    app.state.engine = engine
    app.include_router(workbench_router)
    client = TestClient(app)

    response = client.get(
        "/workbench/sessions/sess-1/missions/mission-1/decisions/missing-decision"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "决策不存在"}
    assert engine.workbench_service.requested_decisions == [
        {
            "session_id": "sess-1",
            "mission_id": "mission-1",
            "decision_id": "missing-decision",
        }
    ]
