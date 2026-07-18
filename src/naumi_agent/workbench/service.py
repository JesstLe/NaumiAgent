"""Workbench application service used by API routes and UI bridges."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.context_health import (
    ContextHealthInput,
    evaluate_context_health,
)
from naumi_agent.workbench.models import (
    AgentProfile,
    Approval,
    ApprovalState,
    Decision,
    DecisionKind,
    DecisionStrength,
    EventSeverity,
    IntentLock,
    IssueBid,
    IssueMetadata,
    Lease,
    LeaseState,
    Mission,
    ParallelMode,
    ProposalState,
    RiskLevel,
    WorkbenchProposal,
)
from naumi_agent.workbench.review_evidence import ReviewEvidenceCollector
from naumi_agent.workbench.store import WorkbenchStore
from naumi_agent.workbench.validation import ValidationCommand, ValidationRunner
from naumi_agent.worktree.models import WorktreeRecord

logger = logging.getLogger(__name__)

_MAX_SNAPSHOT_WORKTREES = 200
_MAX_SNAPSHOT_REVIEWS = 100


class WorktreeStatusProvider(Protocol):
    async def status(
        self,
        name: str = "",
    ) -> WorktreeRecord | list[WorktreeRecord]: ...


def _status_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _workbench_navigation_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Derive deterministic counts and an initial selection from authoritative data."""
    missions = list(snapshot.get("missions") or [])
    tasks = list(snapshot.get("tasks") or [])
    issues = list(snapshot.get("issues") or [])
    leases = list(snapshot.get("leases") or [])
    approvals = list(snapshot.get("approvals") or [])
    failures = list(snapshot.get("failures") or [])

    active_task = next(
        (task for task in tasks if _status_text(task.get("status")) == "in_progress"),
        None,
    )
    active_task_id = str((active_task or {}).get("id") or "")
    active_issue = next(
        (issue for issue in issues if str(issue.get("task_id") or "") == active_task_id),
        None,
    )
    active_mission = next(
        (
            mission
            for mission in missions
            if _status_text(mission.get("status")) in {"active", "planning"}
        ),
        None,
    )
    mission_id = str(
        (active_issue or {}).get("mission_id")
        or (active_mission or {}).get("id")
        or ""
    )
    active_lease = next(
        (lease for lease in leases if str(lease.get("task_id") or "") == active_task_id),
        None,
    )
    worktree = str(
        (active_issue or {}).get("related_worktree")
        or (active_lease or {}).get("worktree_name")
        or ""
    )
    active_review = next(
        (
            review
            for review in approvals
            if str(review.get("task_id") or "") == active_task_id
        ),
        approvals[0] if approvals else None,
    )
    referenced_worktrees = {
        str(value)
        for value in [
            *(issue.get("related_worktree") for issue in issues),
            *(lease.get("worktree_name") for lease in leases),
        ]
        if str(value or "").strip()
    }
    projected_worktrees = list(snapshot.get("worktrees") or [])
    if snapshot.get("worktrees_status") == "ready":
        worktree_count = max(
            len(projected_worktrees),
            int(snapshot.get("worktrees_total") or 0),
        )
    else:
        worktree_count = len(referenced_worktrees)
    return {
        "counts": {
            "missions": len(missions),
            "tasks": len(tasks),
            "worktrees": worktree_count,
            "reviews": len(approvals),
            "failures": len(failures),
        },
        "active_selection": {
            "mission_id": mission_id,
            "task_id": active_task_id,
            "worktree": worktree,
            "review_id": str((active_review or {}).get("id") or ""),
        },
    }


class WorkbenchService:
    """High-level facade for dashboard operations."""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        workbench_store: WorkbenchStore,
        validation_runner: ValidationRunner | None = None,
        workspace_root: str | None = None,
        review_evidence_collector: ReviewEvidenceCollector | None = None,
        worktree_manager: WorktreeStatusProvider | None = None,
    ) -> None:
        self._task_store = task_store
        self._workbench_store = workbench_store
        self._validation_runner = validation_runner
        self._workspace_root = workspace_root
        self._review_evidence_collector = review_evidence_collector
        self._worktree_manager = worktree_manager
        self._snapshot_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._snapshot_states: OrderedDict[str, tuple[str, str, int]] = OrderedDict()

    def _tasks_for_session(self, session_id: str) -> TaskStore:
        return self._task_store.scoped(session_id)

    async def create_mission(self, *, session_id: str, title: str, goal: str) -> Mission:
        mission = await self._workbench_store.create_mission(session_id, title, goal)
        await self._workbench_store.append_event(
            session_id=session_id,
            type="mission.created",
            actor="Human",
            subject_id=mission.id,
            payload={"title": mission.title},
        )
        return mission

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
    ) -> dict[str, Any]:
        if not rule or not rule.strip():
            raise ValueError("意图锁规则不能为空")

        cleaned_blocked = [p.strip() for p in (blocked_paths or []) if p and p.strip()]
        cleaned_allowed = [p.strip() for p in (allowed_paths or []) if p and p.strip()]

        lock = await self._workbench_store.add_intent_lock(
            session_id=session_id,
            mission_id=mission_id,
            rule=rule.strip(),
            blocked_paths=cleaned_blocked,
            allowed_paths=cleaned_allowed,
            require_proposal_for_risk=require_proposal_for_risk,
            created_by=actor,
        )
        await self._workbench_store.append_event(
            session_id=session_id,
            type="intent_lock.created",
            actor=actor,
            subject_id=lock.id,
            payload={
                "mission_id": mission_id,
                "rule": lock.rule,
                "require_proposal_for_risk": require_proposal_for_risk.value,
            },
        )
        return self._intent_lock_to_dict(lock)

    async def deactivate_intent_lock(
        self, session_id: str, mission_id: str, lock_id: str, actor: str
    ) -> dict[str, Any] | None:
        """Deactivates an intent lock and records an audit event.

        A deactivated lock no longer blocks actions (the policy evaluator skips
        inactive locks). Returns ``None`` when the lock does not exist.
        """
        existing = await self._workbench_store.get_intent_lock(
            session_id, mission_id, lock_id
        )
        if existing is None:
            return None
        lock = await self._workbench_store.deactivate_intent_lock(session_id, lock_id)
        if lock is None:
            return None
        await self._workbench_store.append_event(
            session_id=session_id,
            type="intent_lock.deactivated",
            actor=actor,
            subject_id=lock.id,
            payload={
                "mission_id": mission_id,
                "rule": lock.rule,
            },
        )
        return self._intent_lock_to_dict(lock)

    async def record_policy_hit(
        self,
        *,
        session_id: str,
        mission_id: str,
        lock_id: str,
        rule: str,
        reason: str,
        actor: str,
        changed_paths: list[str],
    ) -> None:
        """Records a policy-hit audit event when an intent lock blocks an action."""
        await self._workbench_store.append_event(
            session_id=session_id,
            type="policy.hit",
            actor=actor,
            subject_id=lock_id,
            payload={
                "mission_id": mission_id,
                "rule": rule,
                "reason": reason,
                "changed_paths": list(changed_paths),
            },
        )

    @staticmethod
    def _intent_lock_to_dict(lock: IntentLock) -> dict[str, Any]:
        data = asdict(lock)
        data["require_proposal_for_risk"] = data["require_proposal_for_risk"].value
        return data

    async def list_intent_locks(
        self,
        session_id: str,
        mission_id: str,
        active: bool | None = None,
    ) -> list[dict[str, Any]]:
        locks = await self._workbench_store.list_intent_locks(
            session_id, mission_id, active=active
        )
        return [self._intent_lock_to_dict(lock) for lock in locks]

    async def get_intent_lock(
        self, session_id: str, mission_id: str, lock_id: str
    ) -> dict[str, Any] | None:
        lock = await self._workbench_store.get_intent_lock(
            session_id, mission_id, lock_id
        )
        if lock is None:
            return None
        return self._intent_lock_to_dict(lock)

    async def create_decision(
        self,
        *,
        session_id: str,
        mission_id: str,
        actor: str,
        kind: DecisionKind,
        title: str,
        content: str,
        strength: DecisionStrength = DecisionStrength.REQUIRED,
    ) -> dict[str, Any]:
        if not title or not title.strip():
            raise ValueError("决策标题不能为空")
        if not content or not content.strip():
            raise ValueError("决策内容不能为空")

        decision = await self._workbench_store.add_decision(
            session_id=session_id,
            mission_id=mission_id,
            kind=kind,
            title=title.strip(),
            content=content.strip(),
            actor=actor.strip(),
            strength=strength,
        )
        await self._workbench_store.append_event(
            session_id=session_id,
            type="decision.created",
            actor=decision.actor,
            subject_id=decision.id,
            payload={
                "mission_id": mission_id,
                "kind": decision.kind.value,
                "title": decision.title,
            },
        )
        return self._decision_to_dict(decision)

    @staticmethod
    def _decision_to_dict(decision: Decision) -> dict[str, Any]:
        data = asdict(decision)
        data["kind"] = data["kind"].value
        data["strength"] = data["strength"].value
        return data

    async def list_decisions(
        self,
        session_id: str,
        mission_id: str,
        kind: DecisionKind | str | None = None,
    ) -> list[dict[str, Any]]:
        decisions = await self._workbench_store.list_decisions(
            session_id, mission_id, kind=kind
        )
        return [self._decision_to_dict(decision) for decision in decisions]

    async def get_decision(
        self, session_id: str, mission_id: str, decision_id: str
    ) -> dict[str, Any] | None:
        decision = await self._workbench_store.get_decision(
            session_id, mission_id, decision_id
        )
        if decision is None:
            return None
        return self._decision_to_dict(decision)

    async def resolve_approval(
        self,
        *,
        session_id: str,
        approval_id: str,
        actor: str,
        state: ApprovalState,
        decision_note: str,
    ) -> dict[str, Any] | None:
        if state not in {ApprovalState.APPROVED, ApprovalState.REJECTED}:
            raise ValueError("审批结果只能是 approved 或 rejected")

        approval = await self._workbench_store.resolve_approval(
            session_id=session_id,
            approval_id=approval_id,
            state=state,
            reviewer=actor.strip(),
            decision_note=decision_note.strip(),
        )
        if approval is None:
            return None

        await self._workbench_store.append_event(
            session_id=session_id,
            type="approval.resolved",
            actor=approval.reviewer,
            subject_id=approval.id,
            payload={
                "state": approval.state.value,
                "mission_id": approval.mission_id,
                "task_id": approval.task_id,
                "title": approval.title,
            },
        )
        task = await self._tasks_for_session(session_id).get_task(approval.task_id)
        return self._approval_to_dict(approval) | {"task": self._task_to_summary(task)}

    @staticmethod
    def _approval_to_dict(approval: Approval) -> dict[str, Any]:
        data = asdict(approval)
        data["state"] = data["state"].value
        return data

    async def list_approvals(
        self,
        session_id: str,
        state: ApprovalState | None = None,
        mission_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        approvals = await self._workbench_store.list_approvals(
            session_id=session_id,
            state=state,
            mission_id=mission_id,
            task_id=task_id,
            limit=limit,
        )
        tasks = await self._tasks_for_session(session_id).list_tasks()
        tasks_by_id = {task.id: task for task in tasks}
        return [
            self._approval_to_dict(approval)
            | {"task": self._task_to_summary(tasks_by_id.get(approval.task_id))}
            for approval in approvals
        ]

    async def get_approval(
        self, session_id: str, approval_id: str
    ) -> dict[str, Any] | None:
        approval = await self._workbench_store.get_approval(session_id, approval_id)
        if approval is None:
            return None
        task = await self._tasks_for_session(session_id).get_task(approval.task_id)
        return self._approval_to_dict(approval) | {"task": self._task_to_summary(task)}

    async def get_review_evidence(
        self, session_id: str, approval_id: str
    ) -> dict[str, Any] | None:
        """Collects real review evidence for an approval (diff, files, runs...).

        Returns ``None`` when the approval does not exist. When the evidence
        collector is not configured (no worktree storage dir), still returns
        the approval with empty diff fields rather than failing.
        """
        if self._review_evidence_collector is None:
            approval = await self._workbench_store.get_approval(session_id, approval_id)
            if approval is None:
                return None
            return {
                "approval": self._approval_to_dict(approval),
                "issue": None,
                "worktree": {"name": "", "path": "", "status": "unbound"},
                "validation_runs": [],
                "changed_files": [],
                "diff_hunks": [],
                "agent_notes": [],
                "events": [],
            }
        return await self._review_evidence_collector.collect(
            session_id=session_id, approval_id=approval_id
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
    ) -> dict[str, Any]:
        mission_id = await self._require_mission(session_id, mission_id)
        task = await self._tasks_for_session(session_id).get_task(task_id)
        if task is None:
            raise ValueError(f"任务 #{task_id} 不存在")

        issue = await self._workbench_store.upsert_issue(
            session_id=session_id,
            task_id=task_id,
            mission_id=mission_id,
            parallel_mode=parallel_mode,
            risk_level=risk_level,
            acceptance_criteria=acceptance_criteria,
        )
        await self._workbench_store.append_event(
            session_id=session_id,
            type="issue.created",
            actor="Planner-Agent",
            subject_id=task_id,
            payload={"mission_id": mission_id, "risk_level": risk_level.value},
        )
        return self._issue_to_dict(issue) | {"task": self._task_to_summary(task)}

    async def create_issue(
        self,
        *,
        session_id: str,
        mission_id: str,
        title: str,
        description: str = "",
        blocked_by: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        parallel_mode: ParallelMode = ParallelMode.EXCLUSIVE,
        risk_level: RiskLevel = RiskLevel.MEDIUM,
    ) -> dict[str, Any]:
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("issue 标题不能为空")
        mission_id = await self._require_mission(session_id, mission_id)

        cleaned_description = description.strip()
        cleaned_blockers = [
            blocker.strip() for blocker in (blocked_by or []) if blocker and blocker.strip()
        ]
        cleaned_acceptance_criteria = [
            item.strip()
            for item in (acceptance_criteria or [])
            if item and item.strip()
        ]

        task = await self._tasks_for_session(session_id).create_task(
            subject=cleaned_title,
            description=cleaned_description,
            blocked_by=cleaned_blockers,
        )
        await self.attach_issue(
            session_id=session_id,
            mission_id=mission_id,
            task_id=task.id,
            acceptance_criteria=cleaned_acceptance_criteria,
            parallel_mode=parallel_mode,
            risk_level=risk_level,
        )
        issue = await self._workbench_store.get_issue(session_id, task.id)
        if issue is None:
            raise RuntimeError("issue 创建后无法读取")
        return self._issue_to_dict(issue) | {"task": self._task_to_summary(task)}

    async def _require_mission(self, session_id: str, mission_id: str) -> str:
        cleaned_mission_id = mission_id.strip()
        if not cleaned_mission_id:
            raise ValueError("mission 不存在或不属于当前会话")

        missions = await self._workbench_store.list_missions(session_id)
        if not any(mission.id == cleaned_mission_id for mission in missions):
            raise ValueError("mission 不存在或不属于当前会话")
        return cleaned_mission_id

    async def list_issues(
        self,
        session_id: str,
        mission_id: str | None = None,
        risk_level: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        normalized_status: str | None = None
        task_status: TaskStatus | None = None
        if status is not None:
            normalized_status = status.strip().lower()
            try:
                task_status = TaskStatus(normalized_status)
            except ValueError as exc:
                raise ValueError(f"任务状态无效: {status}") from exc
        issues = await self._workbench_store.list_issues(
            session_id=session_id,
            mission_id=mission_id,
            risk_level=risk_level,
            limit=limit,
        )
        tasks = await self._tasks_for_session(session_id).list_tasks()
        tasks_by_id = {task.id: task for task in tasks}
        if task_status is not None:
            matching_task_ids = {
                task.id for task in tasks if task.status == task_status
            }
            issues = [
                issue for issue in issues if issue.task_id in matching_task_ids
            ]
        return {
            "issues": [
                self._issue_to_dict(issue) | {
                    "task": self._task_to_summary(tasks_by_id.get(issue.task_id))
                }
                for issue in issues
            ],
            "mission_id": mission_id,
            "risk_level": risk_level,
            "status": normalized_status,
            "limit": limit,
        }

    async def get_issue(self, session_id: str, task_id: str) -> dict[str, Any] | None:
        issue = await self._workbench_store.get_issue(session_id, task_id)
        if issue is None:
            return None
        task = await self._tasks_for_session(session_id).get_task(task_id)
        return self._issue_to_dict(issue) | {"task": self._task_to_summary(task)}

    @staticmethod
    def _issue_to_dict(issue: IssueMetadata) -> dict[str, Any]:
        data = asdict(issue)
        # Ensure enum values are JSON-friendly strings.
        data["parallel_mode"] = data["parallel_mode"].value
        data["risk_level"] = data["risk_level"].value
        return data

    AGENT_STALE_THRESHOLD_SECONDS = 300
    AGENT_OFFLINE_THRESHOLD_SECONDS = 900

    def _derive_agent_status(
        self,
        last_heartbeat_at: str,
        active_lease: Lease | None,
        profile_status: str,
    ) -> str:
        """Derive agent activity status from heartbeat age and active lease.

        Profiles that have never heartbeated keep their persisted status so
        registration-time declarations remain visible until the first heartbeat
        arrives.
        """
        if not last_heartbeat_at:
            return profile_status
        try:
            heartbeat = datetime.fromisoformat(last_heartbeat_at)
        except ValueError:
            return profile_status
        age_seconds = (datetime.now() - heartbeat).total_seconds()
        if age_seconds > self.AGENT_OFFLINE_THRESHOLD_SECONDS:
            return "offline"
        if age_seconds > self.AGENT_STALE_THRESHOLD_SECONDS:
            return "stale"
        return "busy" if active_lease is not None else "idle"

    async def _enrich_agent_profile(
        self, profile: AgentProfile, session_id: str
    ) -> dict[str, Any]:
        """Return the profile dict enriched with derived status, current lease and issue."""
        active_lease = await self._workbench_store.get_agent_active_lease(
            session_id, profile.id
        )
        current_issue: dict[str, Any] | None = None
        task: Any | None = None
        if active_lease is not None:
            issue = await self._workbench_store.get_issue(
                session_id, active_lease.task_id
            )
            task = await self._tasks_for_session(session_id).get_task(active_lease.task_id)
            if issue is not None:
                current_issue = self._issue_to_dict(issue) | {
                    "task": self._task_to_summary(task)
                }
        status = self._derive_agent_status(
            profile.last_heartbeat_at, active_lease, profile.status
        )
        data = asdict(profile)
        data["status"] = status
        data["current_lease"] = (
            self._lease_to_dict(active_lease, task=task) if active_lease else None
        )
        data["current_issue"] = current_issue
        return data

    async def record_agent_heartbeat(
        self, session_id: str, agent_id: str
    ) -> dict[str, Any] | None:
        """Record a heartbeat and return the enriched agent profile."""
        profile = await self._workbench_store.record_agent_heartbeat(
            session_id, agent_id
        )
        if profile is None:
            return None
        await self._workbench_store.append_event(
            session_id=session_id,
            type="agent.heartbeat",
            actor=agent_id,
            subject_id=agent_id,
            payload={"status": profile.status},
        )
        return await self._enrich_agent_profile(profile, session_id)

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
    ) -> dict[str, Any]:
        profile = await self._workbench_store.upsert_agent_profile(
            session_id=session_id,
            agent_id=agent_id,
            name=name,
            role=role,
            capabilities=capabilities,
            permissions=permissions,
            max_parallel_tasks=max_parallel_tasks,
            status=status,
        )
        await self._workbench_store.append_event(
            session_id=session_id,
            type="agent_profile.upserted",
            actor=actor.strip() or "Human",
            subject_id=profile.id,
            payload={
                "name": profile.name,
                "role": profile.role,
                "status": profile.status,
                "capabilities": profile.capabilities,
                "permissions": profile.permissions,
            },
        )
        return await self._enrich_agent_profile(profile, session_id)

    async def list_agent_profiles(
        self,
        session_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        profiles = await self._workbench_store.list_agent_profiles(
            session_id, status=status, limit=limit
        )
        return {
            "agent_profiles": [
                await self._enrich_agent_profile(profile, session_id)
                for profile in profiles
            ],
            "status": status,
            "limit": limit,
        }

    async def get_agent_profile(
        self, session_id: str, agent_id: str
    ) -> dict[str, Any] | None:
        profile = await self._workbench_store.get_agent_profile(session_id, agent_id)
        if profile is None:
            return None
        return await self._enrich_agent_profile(profile, session_id)

    @staticmethod
    def _agent_profile_to_dict(profile: AgentProfile) -> dict[str, Any]:
        return asdict(profile)

    async def dashboard_snapshot(self, session_id: str) -> dict[str, Any]:
        async with self._snapshot_lock_for(session_id):
            return await self._build_dashboard_snapshot(session_id)

    def _snapshot_lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._snapshot_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._snapshot_locks[session_id] = lock
        self._snapshot_locks.move_to_end(session_id)
        while len(self._snapshot_locks) > 100:
            oldest_session, oldest_lock = next(iter(self._snapshot_locks.items()))
            if oldest_lock.locked():
                break
            del self._snapshot_locks[oldest_session]
        return lock

    async def _build_dashboard_snapshot(self, session_id: str) -> dict[str, Any]:
        tasks = await self._tasks_for_session(session_id).list_tasks()
        raw_agent_profiles = await self._workbench_store.list_agent_profiles(
            session_id, limit=50
        )
        agent_profiles = [
            await self._enrich_agent_profile(profile, session_id)
            for profile in raw_agent_profiles
        ]
        events = await self._workbench_store.list_events(session_id, limit=50)
        failures = await self._workbench_store.list_failures(session_id)
        raw_validation_runs = await self._workbench_store.list_validation_runs(
            session_id, limit=50
        )
        context_snapshots = await self._workbench_store.list_context_snapshots(
            session_id, limit=50
        )
        approvals = await self._workbench_store.list_approvals(
            session_id, state=ApprovalState.WAITING, limit=_MAX_SNAPSHOT_REVIEWS
        )
        missions = await self._list_missions_for_snapshot(session_id)
        tasks_by_id = {task.id: task for task in tasks}
        validation_runs = [
            run | {"task": self._task_to_summary(tasks_by_id.get(run["task_id"]))}
            for run in raw_validation_runs
        ]
        intent_locks: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        for mission in missions:
            mission_id = mission["id"]
            intent_locks.extend(
                self._intent_lock_to_dict(lock)
                for lock in await self._workbench_store.list_intent_locks(
                    session_id, mission_id
                )
            )
            decisions.extend(
                self._decision_to_dict(decision)
                for decision in await self._workbench_store.list_decisions(
                    session_id, mission_id
                )
            )
        issues = []
        leases = []
        for task in tasks:
            issue = await self._workbench_store.get_issue(session_id, task.id)
            if issue is not None:
                issues.append(
                    self._issue_to_dict(issue) | {"task": self._task_to_summary(task)}
                )
            lease = await self._workbench_store.get_active_lease(session_id, task.id)
            if lease is not None:
                leases.append(self._lease_to_dict(lease, task=task))
        bids = await self._workbench_store.list_bids_for_snapshot(
            session_id, [task.id for task in tasks]
        )
        proposals = await self._workbench_store.list_proposals_for_snapshot(session_id)
        worktrees, worktrees_status, worktrees_code, worktrees_total = (
            await self._worktree_snapshot(tasks_by_id=tasks_by_id, leases=leases)
        )
        snapshot = {
            "version": 1,
            "session_id": session_id,
            "summary": self._snapshot_summary(
                missions=missions,
                agent_profiles=agent_profiles,
                tasks=tasks,
                issues=issues,
                validation_runs=raw_validation_runs,
                approvals=approvals,
            ),
            "missions": missions,
            "agent_profiles": agent_profiles,
            "intent_locks": intent_locks,
            "decisions": decisions,
            "tasks": [asdict(task) for task in tasks],
            "issues": issues,
            "leases": leases,
            "bids": [self._bid_to_dict(bid) for bid in bids],
            "proposals": [self._proposal_to_dict(p) for p in proposals],
            "failures": failures,
            "events": [self._event_to_dict(event, tasks_by_id) for event in events],
            "validation_runs": validation_runs,
            "context_snapshots": context_snapshots,
            "approvals": [self._approval_to_dict(approval) for approval in approvals],
            "worktrees": worktrees,
            "worktrees_status": worktrees_status,
            "worktrees_code": worktrees_code,
            "worktrees_total": worktrees_total,
            "worktrees_truncated": worktrees_total > len(worktrees),
        }
        return self._version_dashboard_snapshot(snapshot)

    async def _worktree_snapshot(
        self,
        *,
        tasks_by_id: dict[str, Any],
        leases: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str, str, int]:
        manager = self._worktree_manager
        if manager is None:
            return [], "unavailable", "worktree_manager_unavailable", 0
        try:
            raw_records = await manager.status()
            records = raw_records if isinstance(raw_records, list) else [raw_records]
            if any(not isinstance(record, WorktreeRecord) for record in records):
                raise TypeError("worktree manager returned invalid record")
        except Exception as exc:
            logger.warning(
                "Workbench worktree snapshot failed (%s)",
                type(exc).__name__,
            )
            return [], "unavailable", "worktree_snapshot_failed", 0

        active_task_id = next(
            (
                task.id
                for task in tasks_by_id.values()
                if _status_text(getattr(task, "status", "")) == "in_progress"
            ),
            "",
        )
        ordered = sorted(
            records,
            key=lambda record: (record.task_id != active_task_id, record.name),
        )
        lease_by_worktree = {
            str(lease.get("worktree_name") or ""): lease
            for lease in leases
            if str(lease.get("worktree_name") or "")
        }
        lease_by_task = {
            str(lease.get("task_id") or ""): lease
            for lease in leases
            if str(lease.get("task_id") or "")
        }
        projected = []
        for record in ordered[:_MAX_SNAPSHOT_WORKTREES]:
            lease = lease_by_worktree.get(record.name) or lease_by_task.get(
                record.task_id
            )
            data = asdict(record)
            data["status"] = record.status.value
            data["removable"] = record.removable
            data["task"] = self._task_to_summary(tasks_by_id.get(record.task_id))
            data["lease"] = lease
            data["agent_id"] = str((lease or {}).get("agent_id") or "")
            projected.append(data)
        return projected, "ready", "", len(ordered)

    def _version_dashboard_snapshot(
        self,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach bounded, session-scoped consistency metadata to a full snapshot."""
        session_id = str(snapshot.get("session_id") or "")
        summary = _workbench_navigation_summary(snapshot)
        comparable = {
            **snapshot,
            "counts": summary["counts"],
            "active_selection": summary["active_selection"],
        }
        encoded = json.dumps(
            comparable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        previous_state = self._snapshot_states.get(session_id)
        if previous_state is None:
            previous_state = (uuid.uuid4().hex[:16], "", 0)
        previous_stream_id, previous_fingerprint, previous_revision = previous_state
        revision = (
            previous_revision
            if fingerprint == previous_fingerprint
            else previous_revision + 1
        )
        self._snapshot_states[session_id] = (
            previous_stream_id,
            fingerprint,
            revision,
        )
        self._snapshot_states.move_to_end(session_id)
        while len(self._snapshot_states) > 100:
            self._snapshot_states.popitem(last=False)
        return {
            **comparable,
            "schema_version": 1,
            "stream_id": previous_stream_id,
            "revision": revision,
            "generated_at": datetime.now().astimezone().isoformat(),
            "full": True,
        }

    @staticmethod
    def _snapshot_summary(
        *,
        missions: list[dict[str, Any]],
        agent_profiles: list[dict[str, Any]],
        tasks: list[Any],
        issues: list[dict[str, Any]],
        validation_runs: list[dict[str, Any]],
        approvals: list[Approval],
    ) -> dict[str, Any]:
        task_status_by_id = {task.id: task.status for task in tasks}
        issue_task_ids = {issue["task_id"] for issue in issues}
        return {
            "current_mission_title": missions[0]["title"] if missions else "",
            "active_agents": sum(
                1 for profile in agent_profiles if profile.get("status") != "idle"
            ),
            "open_issues": sum(
                1
                for task_id in issue_task_ids
                if str(task_status_by_id.get(task_id, "")) != "completed"
            ),
            "blocked_issues": sum(
                1
                for task_id in issue_task_ids
                if str(task_status_by_id.get(task_id, "")) == "blocked"
            ),
            "pending_approvals": len(approvals),
            "failed_validations": sum(
                1 for run in validation_runs if run.get("status") == "failed"
            ),
        }

    @staticmethod
    def _task_to_summary(task: Any | None) -> dict[str, Any] | None:
        if task is None:
            return None
        data = asdict(task)
        status = data.get("status")
        if hasattr(status, "value"):
            data["status"] = status.value
        return data

    @staticmethod
    def _event_task_id(event: Any, tasks_by_id: dict[str, Any]) -> str | None:
        payload_task_id = event.payload.get("task_id")
        if isinstance(payload_task_id, str) and payload_task_id:
            return payload_task_id
        if event.subject_id in tasks_by_id:
            return event.subject_id
        return None

    @classmethod
    def _event_to_dict(cls, event: Any, tasks_by_id: dict[str, Any]) -> dict[str, Any]:
        data = event.to_dict()
        data["severity"] = event.severity.value
        task_id = cls._event_task_id(event, tasks_by_id)
        if task_id is not None:
            data["task"] = cls._task_to_summary(tasks_by_id.get(task_id))
        return data

    @staticmethod
    def _lease_to_dict(lease: Lease, task: Any | None = None) -> dict[str, Any]:
        data = asdict(lease)
        # Ensure enum values are JSON-friendly strings.
        data["state"] = data["state"].value
        data["task"] = WorkbenchService._task_to_summary(task)
        return data

    @staticmethod
    def _bid_to_dict(bid: IssueBid) -> dict[str, Any]:
        return {
            "id": bid.id,
            "session_id": bid.session_id,
            "task_id": bid.task_id,
            "agent_id": bid.agent_id,
            "confidence": bid.confidence,
            "estimate_minutes": bid.estimate_minutes,
            "eta": bid.eta,
            "note": bid.note,
            "created_at": bid.created_at,
            "updated_at": bid.updated_at,
        }

    async def create_bid(
        self,
        session_id: str,
        task_id: str,
        agent_id: str,
        confidence: float,
        estimate_minutes: int,
        eta: str,
        note: str,
    ) -> dict[str, Any]:
        """Persist a new agent bid for an issue and return it as a dict."""
        bid = await self._workbench_store.create_bid(
            session_id=session_id,
            task_id=task_id,
            agent_id=agent_id,
            confidence=confidence,
            estimate_minutes=estimate_minutes,
            eta=eta,
            note=note,
        )
        await self._workbench_store.append_event(
            session_id=session_id,
            type="bid.created",
            actor=agent_id,
            subject_id=task_id,
            payload={"bid_id": bid.id},
        )
        return self._bid_to_dict(bid)

    async def list_bids(
        self,
        session_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        bids = await self._workbench_store.list_bids(
            session_id, task_id=task_id, agent_id=agent_id, limit=limit
        )
        return {
            "bids": [self._bid_to_dict(bid) for bid in bids],
            "task_id": task_id,
            "agent_id": agent_id,
            "limit": limit,
        }

    @staticmethod
    def _proposal_to_dict(proposal: WorkbenchProposal) -> dict[str, Any]:
        return {
            "id": proposal.id,
            "session_id": proposal.session_id,
            "mission_id": proposal.mission_id,
            "task_id": proposal.task_id,
            "agent_id": proposal.agent_id,
            "title": proposal.title,
            "impact_scope": proposal.impact_scope,
            "intended_files": list(proposal.intended_files),
            "validation_plan": list(proposal.validation_plan),
            "risk_level": proposal.risk_level.value,
            "questions": list(proposal.questions),
            "state": proposal.state.value,
            "decision_note": proposal.decision_note,
            "converted_issue_id": proposal.converted_issue_id,
            "created_at": proposal.created_at,
            "updated_at": proposal.updated_at,
        }

    async def create_proposal(
        self,
        *,
        session_id: str,
        mission_id: str,
        task_id: str,
        agent_id: str,
        title: str,
        impact_scope: str,
        intended_files: list[str] | None = None,
        validation_plan: list[str] | None = None,
        risk_level: RiskLevel = RiskLevel.MEDIUM,
        questions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Persist a new proposal and emit a ``proposal.created`` audit event."""
        proposal = await self._workbench_store.create_proposal(
            session_id=session_id,
            mission_id=mission_id,
            task_id=task_id,
            agent_id=agent_id,
            title=title,
            impact_scope=impact_scope,
            intended_files=intended_files,
            validation_plan=validation_plan,
            risk_level=risk_level,
            questions=questions,
        )
        await self._workbench_store.append_event(
            session_id=session_id,
            type="proposal.created",
            actor=agent_id,
            subject_id=proposal.id,
            payload={
                "mission_id": mission_id,
                "task_id": task_id,
                "title": proposal.title,
                "risk_level": proposal.risk_level.value,
            },
            severity=self._severity_for_risk(proposal.risk_level),
        )
        return self._proposal_to_dict(proposal)

    async def list_proposals(
        self,
        session_id: str,
        *,
        mission_id: str | None = None,
        task_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        proposals = await self._workbench_store.list_proposals(
            session_id,
            mission_id=mission_id,
            task_id=task_id,
            state=state,
            limit=limit,
        )
        return {
            "proposals": [self._proposal_to_dict(p) for p in proposals],
            "mission_id": mission_id,
            "task_id": task_id,
            "state": state,
            "limit": limit,
        }

    async def get_proposal(
        self, session_id: str, proposal_id: str
    ) -> dict[str, Any] | None:
        proposal = await self._workbench_store.get_proposal(session_id, proposal_id)
        return self._proposal_to_dict(proposal) if proposal else None

    async def resolve_proposal(
        self,
        session_id: str,
        proposal_id: str,
        *,
        approved: bool,
        reviewer: str,
        decision_note: str = "",
    ) -> dict[str, Any] | None:
        """Approve or reject an OPEN proposal, emitting an audit event.

        Returns the updated proposal dict, or ``None`` if no such proposal
        exists. Resolving an already-decided proposal is idempotent: the
        existing record is returned without a new event.
        """
        existing = await self._workbench_store.get_proposal(session_id, proposal_id)
        if existing is None:
            return None
        target_state = ProposalState.APPROVED if approved else ProposalState.REJECTED
        if existing.state is not ProposalState.OPEN:
            return self._proposal_to_dict(existing)
        proposal = await self._workbench_store.update_proposal_state(
            session_id,
            proposal_id,
            state=target_state,
            decision_note=decision_note,
        )
        event_type = "proposal.approved" if approved else "proposal.rejected"
        await self._workbench_store.append_event(
            session_id=session_id,
            type=event_type,
            actor=reviewer,
            subject_id=proposal_id,
            payload={
                "mission_id": existing.mission_id,
                "task_id": existing.task_id,
                "decision_note": decision_note,
            },
            severity=EventSeverity.WARNING,
        )
        return self._proposal_to_dict(proposal) if proposal else None

    async def convert_proposal(
        self,
        session_id: str,
        proposal_id: str,
        *,
        reviewer: str,
        decision_note: str = "",
    ) -> dict[str, Any] | None:
        """Convert an OPEN proposal into a tracked issue.

        Creates an issue metadata record, marks the proposal as CONVERTED, and
        emits a ``proposal.converted`` audit event. Returns the updated proposal
        dict (including ``converted_issue_id``), or ``None`` if not found.
        """
        existing = await self._workbench_store.get_proposal(session_id, proposal_id)
        if existing is None:
            return None
        if existing.state is not ProposalState.OPEN:
            return self._proposal_to_dict(existing)
        issue = await self._workbench_store.upsert_issue(
            IssueMetadata(
                session_id=session_id,
                task_id=existing.task_id,
                mission_id=existing.mission_id,
                risk_level=existing.risk_level,
                requires_human_approval=True,
                acceptance_criteria=list(existing.validation_plan),
                expected_artifacts=list(existing.intended_files),
            )
        )
        proposal = await self._workbench_store.update_proposal_state(
            session_id,
            proposal_id,
            state=ProposalState.CONVERTED,
            decision_note=decision_note,
            converted_issue_id=issue.task_id,
        )
        await self._workbench_store.append_event(
            session_id=session_id,
            type="proposal.converted",
            actor=reviewer,
            subject_id=proposal_id,
            payload={
                "mission_id": existing.mission_id,
                "task_id": existing.task_id,
                "issue_task_id": issue.task_id,
                "decision_note": decision_note,
            },
            severity=EventSeverity.WARNING,
        )
        return self._proposal_to_dict(proposal) if proposal else None

    @staticmethod
    def _severity_for_risk(risk_level: RiskLevel) -> Any:
        """Map a risk level to an audit-event severity."""
        high_severity = {RiskLevel.HIGH, RiskLevel.CRITICAL}
        if risk_level in high_severity:
            return EventSeverity.WARNING
        return EventSeverity.INFO

    async def list_events(
        self,
        session_id: str,
        event_type: str | None = None,
        subject_id: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        severity: str | None = None,
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        events = await self._workbench_store.list_events(
            session_id,
            event_type=event_type,
            subject_id=subject_id,
            actor=actor,
            since=since,
            severity=severity,
            correlation_id=correlation_id,
            parent_event_id=parent_event_id,
            limit=limit,
        )
        tasks = await self._tasks_for_session(session_id).list_tasks()
        tasks_by_id = {task.id: task for task in tasks}
        return {
            "events": [self._event_to_dict(event, tasks_by_id) for event in events],
            "event_type": event_type,
            "subject_id": subject_id,
            "actor": actor,
            "since": since,
            "severity": severity,
            "correlation_id": correlation_id,
            "parent_event_id": parent_event_id,
            "limit": limit,
        }

    async def get_event(self, session_id: str, event_id: str) -> dict[str, Any] | None:
        event = await self._workbench_store.get_event(session_id, event_id)
        if event is None:
            return None
        tasks = await self._tasks_for_session(session_id).list_tasks()
        return self._event_to_dict(event, {task.id: task for task in tasks})

    async def list_validation_runs(
        self,
        session_id: str,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        runs = await self._workbench_store.list_validation_runs(
            session_id, task_id=task_id, status=status, limit=limit
        )
        tasks = await self._tasks_for_session(session_id).list_tasks()
        tasks_by_id = {task.id: task for task in tasks}
        return [
            run | {"task": self._task_to_summary(tasks_by_id.get(run["task_id"]))}
            for run in runs
        ]

    async def get_validation_run(
        self,
        session_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        run = await self._workbench_store.get_validation_run(session_id, run_id)
        if run is None:
            return None
        task = await self._tasks_for_session(session_id).get_task(run["task_id"])
        return run | {"task": self._task_to_summary(task)}

    async def list_context_snapshots(
        self,
        session_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        health: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        snapshots = await self._workbench_store.list_context_snapshots(
            session_id, task_id=task_id, agent_id=agent_id, health=health, limit=limit
        )
        tasks = await self._tasks_for_session(session_id).list_tasks()
        tasks_by_id = {task.id: task for task in tasks}
        return [
            snapshot | {
                "task": self._task_to_summary(tasks_by_id.get(snapshot["task_id"]))
            }
            for snapshot in snapshots
        ]

    async def get_context_snapshot(
        self,
        session_id: str,
        snapshot_id: str,
    ) -> dict[str, Any] | None:
        snapshot = await self._workbench_store.get_context_snapshot(
            session_id,
            snapshot_id,
        )
        if snapshot is None:
            return None
        task = await self._tasks_for_session(session_id).get_task(snapshot["task_id"])
        return snapshot | {"task": self._task_to_summary(task)}

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
    ) -> dict[str, Any]:
        cleaned_agent_id = agent_id.strip()
        if not cleaned_agent_id:
            raise ValueError("agent_id 不能为空")
        if minutes_since_sync < 0:
            raise ValueError("minutes_since_sync 不能为负数")
        if token_load_ratio < 0:
            raise ValueError("token_load_ratio 不能为负数")

        issue = await self._workbench_store.get_issue(session_id, task_id)
        if issue is None:
            raise ValueError("issue 不存在，无法同步上下文健康度")

        has_acceptance_criteria = bool(issue.acceptance_criteria)
        missions = await self._workbench_store.list_missions(session_id)
        mission = next(
            (m for m in missions if m.id == issue.mission_id),
            None,
        )
        has_goal = mission is not None and mission.goal.strip() != ""

        result = evaluate_context_health(
            ContextHealthInput(
                has_goal=has_goal,
                has_acceptance_criteria=has_acceptance_criteria,
                minutes_since_sync=minutes_since_sync,
                token_load_ratio=token_load_ratio,
                policy_conflict=policy_conflict,
            )
        )

        snapshot = await self._workbench_store.record_context_snapshot(
            session_id=session_id,
            agent_id=cleaned_agent_id,
            task_id=task_id,
            health=result.health,
            reasons=result.reasons,
        )

        await self._workbench_store.append_event(
            session_id=session_id,
            type="context_health.recorded",
            actor=actor.strip() or "Human",
            subject_id=task_id,
            payload={
                "agent_id": cleaned_agent_id,
                "health": snapshot["health"],
                "reasons": result.reasons,
                "mission_id": issue.mission_id,
            },
        )

        task = await self._tasks_for_session(session_id).get_task(task_id)
        return snapshot | {"task": self._task_to_summary(task)}

    async def list_failures(
        self,
        session_id: str,
        task_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        failures = await self._workbench_store.list_failures(
            session_id, task_id=task_id, status=status, kind=kind, limit=limit
        )
        tasks = await self._tasks_for_session(session_id).list_tasks()
        tasks_by_id = {task.id: task for task in tasks}
        return [
            failure | {"task": self._task_to_summary(tasks_by_id.get(failure["task_id"]))}
            for failure in failures
        ]

    async def get_failure(
        self,
        session_id: str,
        failure_id: str,
    ) -> dict[str, Any] | None:
        failure = await self._workbench_store.get_failure(session_id, failure_id)
        if failure is None:
            return None
        task = await self._tasks_for_session(session_id).get_task(failure["task_id"])
        return failure | {"task": self._task_to_summary(task)}

    async def list_leases(
        self,
        session_id: str,
        state: LeaseState | str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        leases = await self._workbench_store.list_leases(
            session_id, state=state, task_id=task_id, agent_id=agent_id, limit=limit
        )
        state_value = state.value if isinstance(state, LeaseState) else state
        tasks = await self._tasks_for_session(session_id).list_tasks()
        tasks_by_id = {task.id: task for task in tasks}
        return {
            "leases": [
                self._lease_to_dict(lease, task=tasks_by_id.get(lease.task_id))
                for lease in leases
            ],
            "state": state_value,
            "task_id": task_id,
            "agent_id": agent_id,
            "limit": limit,
        }

    async def get_lease(self, session_id: str, lease_id: str) -> dict[str, Any] | None:
        lease = await self._workbench_store.get_lease(session_id, lease_id)
        if lease is None:
            return None
        task = await self._tasks_for_session(session_id).get_task(lease.task_id)
        return self._lease_to_dict(lease, task=task)

    async def list_missions(
        self,
        session_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        missions = await self._workbench_store.list_missions(
            session_id, status=status, limit=limit
        )
        return {
            "missions": [asdict(mission) for mission in missions],
            "status": status,
            "limit": limit,
        }

    async def get_mission(self, session_id: str, mission_id: str) -> dict[str, Any] | None:
        missions = await self._workbench_store.list_missions(session_id)
        for mission in missions:
            if mission.id == mission_id:
                return asdict(mission)
        return None

    async def _list_missions_for_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        # Keep the original snapshot behavior: no status filter, no limit, oldest first.
        missions = await self._workbench_store.list_missions(session_id)
        return [asdict(mission) for mission in missions]

    async def run_validation(
        self,
        *,
        session_id: str,
        task_id: str,
        actor: str,
        argv: list[str],
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Run an allowlisted validation command and record the result."""
        if not argv:
            raise ValueError("验证命令不能为空")

        if await self._workbench_store.get_issue(session_id, task_id) is None:
            raise ValueError("issue 不存在，无法运行验证")

        if self._validation_runner is None:
            raise RuntimeError("ValidationRunner 未配置")

        resolved_cwd = self._resolve_cwd(cwd)
        result = await self._validation_runner.run(
            session_id=session_id,
            task_id=task_id,
            actor=actor,
            command=ValidationCommand(argv=argv, cwd=resolved_cwd),
        )
        await self._workbench_store.append_event(
            session_id=session_id,
            type="validation.completed",
            actor=actor,
            subject_id=task_id,
            payload={
                "run_id": result.id,
                "status": result.status,
                "exit_code": result.exit_code,
                "command": argv,
            },
        )
        run = await self.get_validation_run(session_id, result.id)
        if run is None:
            raise RuntimeError("验证记录已完成但无法读取结果")
        return run

    def _resolve_cwd(self, cwd: str | None) -> str:
        """Resolve and validate the working directory for a validation run."""
        if cwd is None:
            if self._workspace_root is None:
                raise ValueError("未配置 workspace_root，必须显式指定 cwd")
            resolved = Path(self._workspace_root).resolve()
            if not resolved.is_dir():
                raise ValueError(f"workspace_root 不存在或不是目录：{self._workspace_root}")
            return str(resolved)

        resolved = Path(cwd).resolve()
        if not resolved.is_dir():
            raise ValueError(f"工作目录不存在或不是目录：{cwd}")

        if self._workspace_root is not None:
            root = Path(self._workspace_root).resolve()
            if root not in resolved.parents and resolved != root:
                raise ValueError("工作目录必须在 workspace_root 内")

        return str(resolved)
