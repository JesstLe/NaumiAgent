"""Authoritative runtime Inspector snapshot assembly."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from naumi_agent.inspector.models import (
    INSPECTOR_SCHEMA_VERSION,
    INSPECTOR_TAB_NAMES,
    InspectorChanges,
    InspectorContext,
    InspectorPlan,
    InspectorTests,
    InspectorTodo,
    InspectorTools,
    RuntimeInspectorSnapshot,
)
from naumi_agent.inspector.tracker import RuntimeInspectorTracker
from naumi_agent.runs.git_probe import GitWorkspaceProbe
from naumi_agent.runs.models import CompletionReceipt


class RuntimeInspectorService:
    """Build monotonic snapshots from engine-owned and durable evidence."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._tracker = RuntimeInspectorTracker()
        self._lock = asyncio.Lock()
        self._fingerprint = ""
        self._revision = 0

    @property
    def tracker(self) -> RuntimeInspectorTracker:
        return self._tracker

    def observe(self, event: str, data: dict[str, Any]) -> bool:
        payload = dict(data)
        if not payload.get("session_id"):
            session = getattr(self._engine, "_session", None)
            payload["session_id"] = str(getattr(session, "id", "") or "")
        return self._tracker.observe(event, payload)

    async def snapshot(self) -> RuntimeInspectorSnapshot:
        async with self._lock:
            snapshot = await self._build_snapshot()
            fingerprint = _fingerprint(snapshot)
            if fingerprint != self._fingerprint:
                self._revision += 1
                self._fingerprint = fingerprint
            return snapshot.with_revision(self._revision, _now_iso())

    @staticmethod
    def changed_tabs(
        previous: RuntimeInspectorSnapshot,
        current: RuntimeInspectorSnapshot,
    ) -> tuple[str, ...]:
        return tuple(
            name
            for name in INSPECTOR_TAB_NAMES
            if getattr(previous, name) != getattr(current, name)
        )

    async def _build_snapshot(self) -> RuntimeInspectorSnapshot:
        session = getattr(self._engine, "_session", None)
        session_id = str(getattr(session, "id", "") or "")
        self._tracker.bind_session(session_id)
        receipt = await self._latest_receipt(session_id)
        active_run_id = self._tracker.active_run_id
        plan = await self._build_plan(session_id, receipt, active_run_id)
        tools = InspectorTools(
            state=(
                "ready"
                if self._tracker.tools or self._tracker.approvals
                else "loading"
                if active_run_id
                else "empty"
            ),
            items=self._tracker.tools,
            approvals=self._tracker.approvals,
        )
        context = await self._build_context()
        changes, tests = self._receipt_tabs(receipt, active_run_id)
        return RuntimeInspectorSnapshot(
            schema_version=INSPECTOR_SCHEMA_VERSION,
            session_id=session_id,
            revision=0,
            generated_at="",
            active_run_id=active_run_id,
            plan=plan,
            tools=tools,
            context=context,
            changes=changes,
            tests=tests,
        )

    async def _build_plan(
        self,
        session_id: str,
        receipt: CompletionReceipt | None,
        active_run_id: str,
    ) -> InspectorPlan:
        if not session_id:
            return InspectorPlan()
        try:
            tasks = await self._engine.task_store.scoped(session_id).list_tasks()
        except Exception as exc:
            return InspectorPlan(
                state="error",
                warnings=(f"Todo 读取失败：{type(exc).__name__}",),
            )
        items = tuple(
            InspectorTodo(
                id=str(task.id)[:500],
                subject=str(task.subject)[:500],
                status=str(getattr(task.status, "value", task.status))[:500],
                active_form=str(task.active_form or "")[:500],
                owner=str(task.owner or "")[:500],
                blocked_by=tuple(str(item)[:500] for item in task.blocked_by[:50]),
            )
            for task in tasks[:50]
        )
        actions = (
            receipt.next_actions
            if receipt is not None
            and (not active_run_id or active_run_id == receipt.run_id)
            else ()
        )
        return InspectorPlan(
            state="ready" if items or actions else "empty",
            items=items,
            next_actions=actions,
        )

    async def _build_context(self) -> InspectorContext:
        warnings: list[str] = []
        workspace_root = self._engine.workspace_root
        git = await GitWorkspaceProbe(workspace_root).capture()
        warnings.extend(git.warnings)
        try:
            model = str(self._engine.router.resolve_model("capable"))[:500]
        except Exception as exc:
            model = ""
            warnings.append(f"模型信息读取失败：{type(exc).__name__}")
        try:
            context = self._engine.get_context_info()
        except Exception as exc:
            context = {}
            warnings.append(f"上下文信息读取失败：{type(exc).__name__}")
        try:
            budget = self._engine.get_budget_info()
        except Exception as exc:
            budget = {}
            warnings.append(f"预算信息读取失败：{type(exc).__name__}")
        usage = self._engine.usage
        return InspectorContext(
            state="ready",
            workspace_root=str(workspace_root)[:500],
            branch=git.branch,
            commit=git.commit,
            git_available=git.available,
            git_dirty=git.dirty,
            model=model,
            runtime_mode=str(
                getattr(self._engine.runtime_mode, "value", self._engine.runtime_mode)
            )[:500],
            permission_mode=str(
                getattr(self._engine.permission_mode, "value", self._engine.permission_mode)
            )[:500],
            context_used=_int(context.get("used")),
            context_window=_int(context.get("window")),
            context_percentage=_float(context.get("percentage")),
            budget_used_usd=_float(budget.get("used_usd")),
            budget_max_usd=_float(budget.get("max_usd")),
            budget_percentage=_float(budget.get("percentage")),
            input_tokens=_int(usage.total_input_tokens),
            output_tokens=_int(usage.total_output_tokens),
            turns=_int(usage.turns),
            warnings=tuple(dict.fromkeys(warnings))[:20],
        )

    async def _latest_receipt(self, session_id: str) -> CompletionReceipt | None:
        if not session_id:
            return None
        try:
            runs = await self._engine.chat_run_store.list_runs(session_id, limit=20)
        except Exception:
            return None
        return next((run.receipt for run in runs if run.receipt is not None), None)

    @staticmethod
    def _receipt_tabs(
        receipt: CompletionReceipt | None,
        active_run_id: str,
    ) -> tuple[InspectorChanges, InspectorTests]:
        if receipt is None:
            state = "loading" if active_run_id else "empty"
            return InspectorChanges(state=state), InspectorTests(state=state)
        state = "stale" if active_run_id and active_run_id != receipt.run_id else "ready"
        return (
            InspectorChanges(
                state=state,
                source_run_id=receipt.run_id,
                receipt_id=receipt.receipt_id,
                summary=receipt.summary[:500],
                items=receipt.changes,
                git_state=receipt.git_state,
            ),
            InspectorTests(
                state=state,
                source_run_id=receipt.run_id,
                receipt_id=receipt.receipt_id,
                validations=receipt.validations,
                unverified=receipt.unverified,
                next_actions=receipt.next_actions,
            ),
        )


def _fingerprint(snapshot: RuntimeInspectorSnapshot) -> str:
    comparable = snapshot.to_dict()
    comparable["revision"] = 0
    comparable["generated_at"] = ""
    encoded = json.dumps(
        comparable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["RuntimeInspectorService"]
