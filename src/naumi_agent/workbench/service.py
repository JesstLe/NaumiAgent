"""Workbench application service used by API routes and UI bridges."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.models import Mission, ParallelMode, RiskLevel
from naumi_agent.workbench.store import WorkbenchStore


class WorkbenchService:
    """High-level facade for dashboard operations."""

    def __init__(self, *, task_store: TaskStore, workbench_store: WorkbenchStore) -> None:
        self._task_store = task_store
        self._workbench_store = workbench_store

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

    async def dashboard_snapshot(self, session_id: str) -> dict[str, Any]:
        tasks = await self._task_store.list_tasks()
        events = await self._workbench_store.list_events(session_id, limit=50)
        failures = await self._workbench_store.list_failures(session_id)
        issues = []
        for task in tasks:
            issue = await self._workbench_store.get_issue(session_id, task.id)
            if issue is not None:
                issues.append(asdict(issue))
        return {
            "session_id": session_id,
            "missions": await self._list_missions_for_snapshot(session_id),
            "tasks": [asdict(task) for task in tasks],
            "issues": issues,
            "failures": failures,
            "events": [event.to_dict() for event in events],
        }

    async def list_events(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        events = await self._workbench_store.list_events(session_id, limit=limit)
        return [event.to_dict() for event in events]

    async def list_validation_runs(
        self,
        session_id: str,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._workbench_store.list_validation_runs(
            session_id, task_id=task_id, limit=limit
        )

    async def _list_missions_for_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        missions = await self._workbench_store.list_missions(session_id)
        return [asdict(mission) for mission in missions]
