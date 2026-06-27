"""Workbench application service used by API routes and UI bridges."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.models import (
    Approval,
    ApprovalState,
    Decision,
    DecisionKind,
    IntentLock,
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
        return self._approval_to_dict(approval)

    @staticmethod
    def _approval_to_dict(approval: Approval) -> dict[str, Any]:
        data = asdict(approval)
        data["state"] = data["state"].value
        return data

    async def list_approvals(
        self,
        session_id: str,
        state: ApprovalState | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        approvals = await self._workbench_store.list_approvals(
            session_id=session_id,
            state=state,
            limit=limit,
        )
        return [self._approval_to_dict(approval) for approval in approvals]

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
        return asdict(issue)

    async def list_issues(
        self,
        session_id: str,
        mission_id: str | None = None,
        risk_level: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        issues = await self._workbench_store.list_issues(
            session_id=session_id,
            mission_id=mission_id,
            risk_level=risk_level,
            limit=limit,
        )
        return {
            "issues": [self._issue_to_dict(issue) for issue in issues],
            "mission_id": mission_id,
            "risk_level": risk_level,
            "limit": limit,
        }

    @staticmethod
    def _issue_to_dict(issue: IssueMetadata) -> dict[str, Any]:
        data = asdict(issue)
        # Ensure enum values are JSON-friendly strings.
        data["parallel_mode"] = data["parallel_mode"].value
        data["risk_level"] = data["risk_level"].value
        return data

    async def dashboard_snapshot(self, session_id: str) -> dict[str, Any]:
        tasks = await self._task_store.list_tasks()
        events = await self._workbench_store.list_events(session_id, limit=50)
        failures = await self._workbench_store.list_failures(session_id)
        validation_runs = await self._workbench_store.list_validation_runs(
            session_id, limit=50
        )
        context_snapshots = await self._workbench_store.list_context_snapshots(
            session_id, limit=50
        )
        approvals = await self._workbench_store.list_approvals(
            session_id, state=ApprovalState.WAITING, limit=50
        )
        issues = []
        leases = []
        for task in tasks:
            issue = await self._workbench_store.get_issue(session_id, task.id)
            if issue is not None:
                issues.append(asdict(issue))
            lease = await self._workbench_store.get_active_lease(session_id, task.id)
            if lease is not None:
                leases.append(self._lease_to_dict(lease))
        return {
            "session_id": session_id,
            "missions": await self._list_missions_for_snapshot(session_id),
            "tasks": [asdict(task) for task in tasks],
            "issues": issues,
            "leases": leases,
            "failures": failures,
            "events": [event.to_dict() for event in events],
            "validation_runs": validation_runs,
            "context_snapshots": context_snapshots,
            "approvals": [self._approval_to_dict(approval) for approval in approvals],
        }

    @staticmethod
    def _lease_to_dict(lease: Lease) -> dict[str, Any]:
        data = asdict(lease)
        # Ensure enum values are JSON-friendly strings.
        data["state"] = data["state"].value
        return data

    async def list_events(
        self,
        session_id: str,
        event_type: str | None = None,
        subject_id: str | None = None,
        actor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        events = await self._workbench_store.list_events(
            session_id,
            event_type=event_type,
            subject_id=subject_id,
            actor=actor,
            limit=limit,
        )
        return {
            "events": [event.to_dict() for event in events],
            "event_type": event_type,
            "subject_id": subject_id,
            "actor": actor,
            "limit": limit,
        }

    async def list_validation_runs(
        self,
        session_id: str,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._workbench_store.list_validation_runs(
            session_id, task_id=task_id, limit=limit
        )

    async def list_context_snapshots(
        self,
        session_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._workbench_store.list_context_snapshots(
            session_id, task_id=task_id, agent_id=agent_id, limit=limit
        )

    async def list_failures(
        self,
        session_id: str,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._workbench_store.list_failures(
            session_id, task_id=task_id, status=status, limit=limit
        )

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
        return {
            "leases": [self._lease_to_dict(lease) for lease in leases],
            "state": state_value,
            "task_id": task_id,
            "agent_id": agent_id,
            "limit": limit,
        }

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
        return {
            "id": result.id,
            "status": result.status,
            "exit_code": result.exit_code,
            "output": result.output,
        }

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
