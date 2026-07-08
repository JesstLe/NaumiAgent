"""Workbench application service used by API routes and UI bridges."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

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
    IntentLock,
    IssueBid,
    IssueMetadata,
    Lease,
    LeaseState,
    Mission,
    ParallelMode,
    RiskLevel,
)
from naumi_agent.workbench.store import WorkbenchStore
from naumi_agent.workbench.validation import ValidationCommand, ValidationRunner


class WorkbenchService:
    """High-level facade for dashboard operations."""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        workbench_store: WorkbenchStore,
        validation_runner: ValidationRunner | None = None,
        workspace_root: str | None = None,
    ) -> None:
        self._task_store = task_store
        self._workbench_store = workbench_store
        self._validation_runner = validation_runner
        self._workspace_root = workspace_root

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
        task = await self._task_store.get_task(approval.task_id)
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
        tasks = await self._task_store.list_tasks()
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
        task = await self._task_store.get_task(approval.task_id)
        return self._approval_to_dict(approval) | {"task": self._task_to_summary(task)}

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
        task = await self._task_store.get_task(task_id)
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

        task = await self._task_store.create_task(
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
        tasks = await self._task_store.list_tasks()
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
        task = await self._task_store.get_task(task_id)
        return self._issue_to_dict(issue) | {"task": self._task_to_summary(task)}

    @staticmethod
    def _issue_to_dict(issue: IssueMetadata) -> dict[str, Any]:
        data = asdict(issue)
        # Ensure enum values are JSON-friendly strings.
        data["parallel_mode"] = data["parallel_mode"].value
        data["risk_level"] = data["risk_level"].value
        return data

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
        return self._agent_profile_to_dict(profile)

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
                self._agent_profile_to_dict(profile) for profile in profiles
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
        return self._agent_profile_to_dict(profile)

    @staticmethod
    def _agent_profile_to_dict(profile: AgentProfile) -> dict[str, Any]:
        return asdict(profile)

    async def dashboard_snapshot(self, session_id: str) -> dict[str, Any]:
        tasks = await self._task_store.list_tasks()
        agent_profiles = await self._workbench_store.list_agent_profiles(
            session_id, limit=50
        )
        events = await self._workbench_store.list_events(session_id, limit=50)
        failures = await self._workbench_store.list_failures(session_id)
        raw_validation_runs = await self._workbench_store.list_validation_runs(
            session_id, limit=50
        )
        context_snapshots = await self._workbench_store.list_context_snapshots(
            session_id, limit=50
        )
        approvals = await self._workbench_store.list_approvals(
            session_id, state=ApprovalState.WAITING, limit=50
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
        return {
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
            "agent_profiles": [
                self._agent_profile_to_dict(profile) for profile in agent_profiles
            ],
            "intent_locks": intent_locks,
            "decisions": decisions,
            "tasks": [asdict(task) for task in tasks],
            "issues": issues,
            "leases": leases,
            "bids": [self._bid_to_dict(bid) for bid in bids],
            "failures": failures,
            "events": [self._event_to_dict(event, tasks_by_id) for event in events],
            "validation_runs": validation_runs,
            "context_snapshots": context_snapshots,
            "approvals": [self._approval_to_dict(approval) for approval in approvals],
        }

    @staticmethod
    def _snapshot_summary(
        *,
        missions: list[dict[str, Any]],
        agent_profiles: list[AgentProfile],
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
                1 for profile in agent_profiles if profile.status != "idle"
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

    async def list_events(
        self,
        session_id: str,
        event_type: str | None = None,
        subject_id: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        events = await self._workbench_store.list_events(
            session_id,
            event_type=event_type,
            subject_id=subject_id,
            actor=actor,
            since=since,
            limit=limit,
        )
        tasks = await self._task_store.list_tasks()
        tasks_by_id = {task.id: task for task in tasks}
        return {
            "events": [self._event_to_dict(event, tasks_by_id) for event in events],
            "event_type": event_type,
            "subject_id": subject_id,
            "actor": actor,
            "since": since,
            "limit": limit,
        }

    async def get_event(self, session_id: str, event_id: str) -> dict[str, Any] | None:
        event = await self._workbench_store.get_event(session_id, event_id)
        if event is None:
            return None
        tasks = await self._task_store.list_tasks()
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
        tasks = await self._task_store.list_tasks()
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
        task = await self._task_store.get_task(run["task_id"])
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
        tasks = await self._task_store.list_tasks()
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
        task = await self._task_store.get_task(snapshot["task_id"])
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

        task = await self._task_store.get_task(task_id)
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
        tasks = await self._task_store.list_tasks()
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
        task = await self._task_store.get_task(failure["task_id"])
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
        tasks = await self._task_store.list_tasks()
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
        task = await self._task_store.get_task(lease.task_id)
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
