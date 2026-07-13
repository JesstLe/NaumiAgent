"""Authoritative runtime Inspector domain and service tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.inspector import (
    INSPECTOR_SCHEMA_VERSION,
    RuntimeInspectorService,
    RuntimeInspectorSnapshot,
    RuntimeInspectorTracker,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.tasks.models import TaskStatus


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _engine(tmp_path: Path) -> AgentEngine:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.name", "Inspector Test")
    _git(workspace, "config", "user.email", "inspector@example.test")
    (workspace / "base.txt").write_text("base\n", encoding="utf-8")
    _git(workspace, "add", "base.txt")
    _git(workspace, "commit", "-m", "base")
    data_dir = tmp_path / "data"
    return AgentEngine(
        AppConfig(
            workspace_root=str(workspace),
            memory=MemoryConfig(
                session_db_path=str(data_dir / "sessions.db"),
                vector_db_path=str(data_dir / "vectors"),
                long_term_enabled=False,
            ),
        )
    )


def _receipt(run_id: str, *, session_change: str = "src/demo.py") -> CompletionReceipt:
    return CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": f"receipt-{run_id}",
            "run_id": run_id,
            "outcome": "completed",
            "summary": "真实改动和验证已经完成。",
            "changes": [
                {
                    "path": session_change,
                    "status": "modified",
                    "additions": 2,
                    "deletions": 1,
                    "source_tool": "file_edit",
                }
            ],
            "validations": [
                {
                    "command": "python -m pytest tests/unit/test_demo.py -q",
                    "status": "passed",
                    "exit_code": 0,
                    "passed": 3,
                    "failed": 0,
                    "skipped": 1,
                    "scope": "tests/unit/test_demo.py",
                }
            ],
            "approvals": [
                {
                    "call_id": "validation-1",
                    "tool_name": "bash_run",
                    "decision": "allowed_once",
                    "scope": "本次运行",
                }
            ],
            "git_state": {
                "available": True,
                "branch": "main",
                "commit": "abc123",
                "dirty": True,
            },
            "unverified": ["尚未运行跨平台验证。"],
            "next_actions": [
                {
                    "id": "review-changes",
                    "kind": "review_changes",
                    "label": "审查改动",
                }
            ],
        }
    )


def test_snapshot_rejects_unknown_schema_and_invalid_tab_state() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        RuntimeInspectorSnapshot.from_dict({"schema_version": 2})

    payload = RuntimeInspectorSnapshot.empty(session_id="session-1").to_dict()
    payload["plan"]["state"] = "invented"
    with pytest.raises(ValueError, match="plan.state"):
        RuntimeInspectorSnapshot.from_dict(payload)


def test_snapshot_round_trip_uses_five_fixed_tabs() -> None:
    snapshot = RuntimeInspectorSnapshot.empty(session_id="session-1")

    restored = RuntimeInspectorSnapshot.from_dict(snapshot.to_dict())

    assert restored == snapshot
    assert restored.schema_version == INSPECTOR_SCHEMA_VERSION
    assert set(restored.to_dict()) >= {
        "plan",
        "tools",
        "context",
        "changes",
        "tests",
    }
    assert all(
        restored.to_dict()[tab]["state"] == "empty"
        for tab in ("plan", "tools", "context", "changes", "tests")
    )


def test_tracker_bounds_redacts_and_correlates_tools_and_approvals() -> None:
    tracker = RuntimeInspectorTracker(max_tools=2, max_approvals=2)
    tracker.observe(
        "tool_start",
        {
            "name": "bash_run",
            "call_id": "call-0",
            "args": json.dumps({"command": "API_KEY=super-secret-value pytest tests/a.py -q"}),
        },
    )
    tracker.observe(
        "tool_end",
        {
            "name": "bash_run",
            "call_id": "call-0",
            "status": "success",
            "duration_ms": 25,
        },
    )
    for index in range(1, 3):
        tracker.observe(
            "tool_start",
            {
                "name": "file_read",
                "call_id": f"call-{index}",
                "args": json.dumps({"path": f"src/{index}.py", "padding": "x" * 900}),
            },
        )
    tracker.observe(
        "permission_bubble",
        {
            "request_id": "permission-1",
            "tool_name": "bash_run",
            "status": "denied",
            "reason": "Authorization: Bearer private-value",
        },
    )

    assert [item.call_id for item in tracker.tools] == ["call-1", "call-2"]
    assert all(len(item.summary) <= 500 for item in tracker.tools)
    assert tracker.approvals[0].decision == "denied"
    public = json.dumps(tracker.to_dict(), ensure_ascii=False)
    assert "super-secret-value" not in public
    assert "private-value" not in public


def test_tracker_deduplicates_replayed_event_ids_and_tracks_run_state() -> None:
    tracker = RuntimeInspectorTracker()
    event = {"event_id": "event-1", "run_id": "run-1", "name": "file_read", "call_id": "c1"}

    assert tracker.observe("run_started", {"event_id": "run-start", "run_id": "run-1"}) is True
    assert tracker.observe("tool_start", event) is True
    assert tracker.observe("tool_start", event) is False
    assert tracker.active_run_id == "run-1"
    assert len(tracker.tools) == 1

    tracker.observe("completion_receipt", _receipt("run-1").to_dict())
    assert tracker.active_run_id == ""
    assert tracker.latest_receipt_id == "receipt-run-1"


def test_tracker_clears_run_evidence_when_session_changes() -> None:
    tracker = RuntimeInspectorTracker()
    tracker.observe(
        "tool_start",
        {
            "session_id": "session-a",
            "event_id": "tool-a",
            "run_id": "run-a",
            "name": "file_read",
            "call_id": "read-a",
        },
    )
    assert [item.call_id for item in tracker.tools] == ["read-a"]

    tracker.observe(
        "run_started",
        {
            "session_id": "session-b",
            "event_id": "run-b",
            "run_id": "run-b",
        },
    )

    assert tracker.session_id == "session-b"
    assert tracker.tools == ()
    assert tracker.approvals == ()
    assert tracker.latest_receipt_id == ""


@pytest.mark.asyncio
async def test_service_builds_five_tabs_from_real_authoritative_sources(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        session = await engine.get_or_create_session(title="Inspector 验收")
        engine.task_store.set_session(session.id)
        task = await engine.task_store.create_task("补充验证", blocked_by=[])
        await engine.task_store.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            active_form="正在补充验证",
        )
        run = await engine.chat_run_store.start_run(
            session_id=session.id,
            user_message_id="message-1",
            run_id="run-authoritative",
        )
        receipt = _receipt(run.id)
        await engine.chat_run_store.finish_run(
            run.id,
            status="completed",
            receipt=receipt,
        )
        engine.runtime_inspector.observe(
            "tool_start",
            {
                "event_id": "tool-start-1",
                "run_id": run.id,
                "name": "file_edit",
                "call_id": "edit-1",
                "args": json.dumps({"path": "src/demo.py"}),
            },
        )
        engine.runtime_inspector.observe(
            "tool_end",
            {
                "event_id": "tool-end-1",
                "run_id": run.id,
                "name": "file_edit",
                "call_id": "edit-1",
                "status": "success",
                "duration_ms": 17,
            },
        )
        engine.runtime_inspector.observe("completion_receipt", receipt.to_dict())

        snapshot = await engine.runtime_inspector.snapshot()

        assert snapshot.session_id == session.id
        assert snapshot.revision == 1
        assert snapshot.plan.state == "ready"
        assert snapshot.plan.items[0].subject == "补充验证"
        assert snapshot.plan.items[0].status == "in_progress"
        assert snapshot.tools.state == "ready"
        assert snapshot.tools.items[0].name == "file_edit"
        assert snapshot.tools.items[0].status == "success"
        assert snapshot.context.state == "ready"
        assert snapshot.context.workspace_root == str(engine.workspace_root)
        assert snapshot.context.branch == "main"
        assert snapshot.context.model
        assert snapshot.changes.state == "ready"
        assert snapshot.changes.receipt_id == receipt.receipt_id
        assert snapshot.changes.items[0].path == "src/demo.py"
        assert snapshot.tests.state == "ready"
        assert snapshot.tests.validations[0].status == "passed"
        assert snapshot.tests.unverified == ("尚未运行跨平台验证。",)
        assert snapshot.tests.next_actions[0].kind == "review_changes"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_service_revision_changes_only_when_authoritative_content_changes(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        await engine.get_or_create_session(title="Revision")
        first = await engine.runtime_inspector.snapshot()
        same = await engine.runtime_inspector.snapshot()
        assert same.revision == first.revision

        engine.runtime_inspector.observe(
            "tool_start",
            {"event_id": "new-tool", "name": "file_read", "call_id": "read-1"},
        )
        changed = await engine.runtime_inspector.snapshot()
        assert changed.revision == first.revision + 1
        assert RuntimeInspectorService.changed_tabs(first, changed) == ("tools",)
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_service_marks_previous_receipt_stale_while_new_run_is_active(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        session = await engine.get_or_create_session(title="Stale")
        run = await engine.chat_run_store.start_run(
            session_id=session.id,
            user_message_id="message-old",
            run_id="run-old",
        )
        await engine.chat_run_store.finish_run(
            run.id,
            status="completed",
            receipt=_receipt(run.id),
        )
        engine.runtime_inspector.observe(
            "run_started",
            {"event_id": "run-new-start", "run_id": "run-new"},
        )

        snapshot = await engine.runtime_inspector.snapshot()

        assert snapshot.changes.state == "stale"
        assert snapshot.tests.state == "stale"
        assert snapshot.changes.source_run_id == "run-old"
        assert snapshot.active_run_id == "run-new"
        assert snapshot.plan.next_actions == ()
    finally:
        await engine.shutdown()
