"""Tests for browser task orchestrator: TaskRunner, stores, templates."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from naumi_agent.tools.browser.orchestrator.task_runner import (
    TaskRunner,
    build_templated_instruction,
    evaluate_rule,
    evaluate_template,
    _normalize_positive_int,
    _normalize_template_input,
    _normalize_template_rule,
    _normalize_template_rules,
    _format_timeout_duration,
)
from naumi_agent.tools.browser.orchestrator.task_run_store import TaskRunStore
from naumi_agent.tools.browser.orchestrator.run_template_store import (
    RunTemplateStore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_runtime() -> MagicMock:
    rt = MagicMock()
    rt.ensure_started = AsyncMock()
    rt.observe = AsyncMock(return_value={"elements": [], "tabs": []})
    rt.get_page_metadata = AsyncMock(return_value={
        "url": "https://example.com",
        "title": "Example",
    })
    rt.get_debug_state = MagicMock(return_value={
        "recentConsole": [],
        "recentNetwork": [],
        "recentErrors": [],
        "capabilities": None,
        "counts": {},
    })
    rt.record_event = MagicMock()
    rt.stop = AsyncMock(return_value={})
    rt.enter_manual_control = AsyncMock(return_value={})
    rt.exit_manual_control = AsyncMock(return_value={})
    rt.write_artifact_json = MagicMock(return_value="/tmp/report.json")
    rt.write_artifact_text = MagicMock(return_value="/tmp/walkthrough.md")
    return rt


def _make_mock_subagent():
    subagent = MagicMock()
    subagent.delegate_task = AsyncMock()
    subagent.planner = MagicMock()
    return subagent


def _make_runner_options(rt):
    planner = MagicMock()
    return {"runtime": rt, "planner": planner}


def _completed_result() -> dict:
    return {
        "status": "completed",
        "step": 1,
        "summary": "Task done",
        "history": [],
        "artifacts": {"sessionDir": "/tmp"},
        "page": {"url": "https://example.com", "title": "Done"},
        "verification": None,
        "operatorMessages": [],
        "pendingInput": None,
        "debug": {},
        "reports": {"reportJsonPath": "/tmp/r.json"},
    }


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


class TestNormalizePositiveInt:
    def test_valid(self):
        assert _normalize_positive_int(5) == 5

    def test_string(self):
        assert _normalize_positive_int("10") == 10

    def test_below_minimum(self):
        assert _normalize_positive_int(0, fallback=3) == 3

    def test_none_returns_fallback(self):
        assert _normalize_positive_int(None, fallback=7) == 7

    def test_none_no_fallback(self):
        assert _normalize_positive_int(None) is None

    def test_negative_returns_fallback(self):
        assert _normalize_positive_int(-5, fallback=2) == 2


class TestFormatTimeoutDuration:
    def test_milliseconds(self):
        assert _format_timeout_duration(500) == "500ms"

    def test_seconds(self):
        assert _format_timeout_duration(3000) == "3 seconds"

    def test_exact_second(self):
        assert _format_timeout_duration(1000) == "1 seconds"


class TestNormalizeTemplateRule:
    def test_valid_rule(self):
        result = _normalize_template_rule({
            "kind": "url_includes",
            "expected": "/dashboard",
            "name": "URL Check",
        })
        assert result["kind"] == "url_includes"
        assert result["expected"] == "/dashboard"
        assert result["name"] == "URL Check"
        assert result["required"] is True

    def test_invalid_kind(self):
        assert _normalize_template_rule({"kind": "bad", "expected": "x"}) is None

    def test_empty_expected(self):
        assert _normalize_template_rule({"kind": "url_includes", "expected": ""}) is None

    def test_value_alias(self):
        result = _normalize_template_rule({
            "kind": "text_includes",
            "value": "Welcome",
        })
        assert result["expected"] == "Welcome"

    def test_optional_false(self):
        result = _normalize_template_rule({
            "kind": "url_includes",
            "expected": "/x",
            "required": False,
        })
        assert result["required"] is False

    def test_auto_id(self):
        result = _normalize_template_rule(
            {"kind": "url_includes", "expected": "/y"}, index=3
        )
        assert result["id"] == "rule-4"

    def test_auto_name(self):
        result = _normalize_template_rule(
            {"kind": "url_includes", "expected": "/z"},
            default_prefix="Login Check",
        )
        assert result["name"] == "Login Check 1"


class TestNormalizeTemplateRules:
    def test_filters_none(self):
        result = _normalize_template_rules([
            {"kind": "url_includes", "expected": "/a"},
            {"kind": "bad", "expected": "x"},
            {"kind": "title_includes", "expected": "Welcome"},
        ])
        assert len(result) == 2

    def test_non_list(self):
        assert _normalize_template_rules("not a list") == []


class TestNormalizeTemplateInput:
    def test_minimal_valid(self):
        result = _normalize_template_input({
            "name": "Test",
            "taskInstruction": "Do something",
        })
        assert result["name"] == "Test"
        assert result["taskInstruction"] == "Do something"
        assert result["browserSource"] == "auto"
        assert "id" in result
        assert "createdAt" in result

    def test_requires_name(self):
        with pytest.raises(ValueError, match="name is required"):
            _normalize_template_input({"taskInstruction": "x"})

    def test_requires_task_or_url(self):
        with pytest.raises(ValueError, match="taskInstruction or startUrl"):
            _normalize_template_input({"name": "No Task"})

    def test_with_start_url(self):
        result = _normalize_template_input({
            "name": "URL Only",
            "startUrl": "https://example.com",
        })
        assert result["startUrl"] == "https://example.com"

    def test_update_existing(self):
        original = _normalize_template_input({
            "name": "Original",
            "taskInstruction": "First version",
        })
        updated = _normalize_template_input(
            {"name": "Updated", "description": "New desc"},
            current=original,
        )
        assert updated["name"] == "Updated"
        assert updated["id"] == original["id"]
        assert updated["taskInstruction"] == "First version"
        assert updated["description"] == "New desc"


class TestBuildTemplatedInstruction:
    def test_no_template(self):
        assert build_templated_instruction("Go there", None) == "Go there"

    def test_with_start_url(self):
        result = build_templated_instruction("Search", {
            "startUrl": "https://google.com",
        })
        assert "Search" in result
        assert "https://google.com" in result

    def test_with_assertions(self):
        result = build_templated_instruction("Do thing", {
            "assertionRules": [
                {"kind": "url_includes", "expected": "/done", "name": "URL Check", "id": "r1"},
            ],
        })
        assert "assertions" in result.lower() or "url_includes" in result

    def test_with_pre_login_checks(self):
        result = build_templated_instruction("Login", {
            "preLoginChecks": [
                {"kind": "url_includes", "expected": "/login", "name": "Check", "id": "r1"},
            ],
        })
        assert "login check" in result.lower()


class TestEvaluateRule:
    def test_url_includes_pass(self):
        result = evaluate_rule(
            {"id": "r1", "name": "URL", "kind": "url_includes", "expected": "/dashboard", "required": True},
            {"url": "https://example.com/dashboard"},
        )
        assert result["passed"] is True

    def test_url_includes_fail(self):
        result = evaluate_rule(
            {"id": "r1", "name": "URL", "kind": "url_includes", "expected": "/dashboard", "required": True},
            {"url": "https://example.com/home"},
        )
        assert result["passed"] is False

    def test_title_includes(self):
        result = evaluate_rule(
            {"id": "r1", "name": "Title", "kind": "title_includes", "expected": "Welcome", "required": True},
            {"title": "Welcome to the app"},
        )
        assert result["passed"] is True

    def test_text_includes(self):
        result = evaluate_rule(
            {"id": "r1", "name": "Text", "kind": "text_includes", "expected": "success", "required": True},
            {"textPreview": "Operation was a success!"},
        )
        assert result["passed"] is True

    def test_case_insensitive(self):
        result = evaluate_rule(
            {"id": "r1", "name": "X", "kind": "url_includes", "expected": "DASHBOARD", "required": True},
            {"url": "https://example.com/dashboard"},
        )
        assert result["passed"] is True

    def test_none_page(self):
        result = evaluate_rule(
            {"id": "r1", "name": "X", "kind": "url_includes", "expected": "/x", "required": True},
            None,
        )
        assert result["passed"] is False


class TestEvaluateTemplate:
    def test_no_template(self):
        assert evaluate_template(None, {}) is None

    def test_all_pass(self):
        result = evaluate_template(
            {
                "id": "t1",
                "name": "Test",
                "assertionRules": [
                    {"id": "r1", "name": "URL", "kind": "url_includes", "expected": "/done", "required": True},
                ],
            },
            {"page": {"url": "https://example.com/done"}},
        )
        assert result["passed"] is True
        assert len(result["assertions"]) == 1

    def test_failure(self):
        result = evaluate_template(
            {
                "id": "t1",
                "name": "Test",
                "assertionRules": [
                    {"id": "r1", "name": "URL", "kind": "url_includes", "expected": "/done", "required": True},
                ],
            },
            {"page": {"url": "https://example.com/other"}},
        )
        assert result["passed"] is False
        assert len(result["failureMessages"]) == 1

    def test_optional_rule_failure_still_passes(self):
        result = evaluate_template(
            {
                "id": "t1",
                "name": "Test",
                "assertionRules": [
                    {"id": "r1", "name": "Optional", "kind": "url_includes", "expected": "/x", "required": False},
                ],
            },
            {"page": {"url": "https://example.com/other"}},
        )
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# TaskRunStore
# ---------------------------------------------------------------------------


class TestTaskRunStore:
    def test_persist_and_load(self, tmp_path):
        store = TaskRunStore(tmp_path)
        runs = [{"id": "run-1", "status": "completed", "summary": "Done"}]
        store.persist(runs)

        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0]["id"] == "run-1"
        assert loaded[0]["status"] == "completed"

    def test_load_empty(self, tmp_path):
        store = TaskRunStore(tmp_path)
        assert store.load() == []

    def test_delete_run(self, tmp_path):
        store = TaskRunStore(tmp_path)
        store.persist([
            {"id": "run-1", "status": "completed"},
            {"id": "run-2", "status": "failed"},
        ])
        store.delete_run("run-1")

        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0]["id"] == "run-2"

    def test_strips_promise_key(self, tmp_path):
        store = TaskRunStore(tmp_path)
        store.persist([
            {"id": "run-1", "status": "running", "promise": "should-drop"},
        ])
        loaded = store.load()
        assert "promise" not in loaded[0]

    def test_index_file_created(self, tmp_path):
        store = TaskRunStore(tmp_path)
        store.persist([
            {"id": "run-1", "status": "completed", "taskInstruction": "Do thing"},
        ])
        index_path = tmp_path / "task-runs" / "index.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text())
        assert index[0]["id"] == "run-1"

    def test_corrupt_file_skipped(self, tmp_path):
        store = TaskRunStore(tmp_path)
        runs_dir = tmp_path / "task-runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "bad.json").write_text("not json{{{")

        loaded = store.load()
        assert loaded == []


# ---------------------------------------------------------------------------
# RunTemplateStore
# ---------------------------------------------------------------------------


class TestRunTemplateStore:
    def test_persist_and_load(self, tmp_path):
        store = RunTemplateStore(tmp_path)
        templates = [{"id": "t1", "name": "Test"}]
        store.persist(templates)
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0]["id"] == "t1"

    def test_load_empty(self, tmp_path):
        store = RunTemplateStore(tmp_path)
        assert store.load() == []

    def test_persist_none(self, tmp_path):
        store = RunTemplateStore(tmp_path)
        store.persist(None)
        assert store.load() == []


# ---------------------------------------------------------------------------
# TaskRunner — state machine
# ---------------------------------------------------------------------------


class TestTaskRunnerCreateRun:
    def test_creates_queued_run(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Open example.com")

        assert run["status"] == "queued"
        assert run["taskInstruction"] == "Open example.com"
        assert run["id"]
        assert run["createdAt"]
        assert runner.get_run(run["id"]) == run

    def test_requires_task_instruction(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        with pytest.raises(ValueError, match="taskInstruction is required"):
            runner.create_run("")

    def test_list_runs(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        runner.create_run("Task 1")
        runner.create_run("Task 2")

        runs = runner.list_runs()
        assert len(runs) == 2
        assert runs[0]["taskInstruction"] == "Task 2"

    def test_list_runs_limit(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        for i in range(5):
            runner.create_run(f"Task {i}")

        runs = runner.list_runs(limit=2)
        assert len(runs) == 2

    def test_get_run_not_found(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        assert runner.get_run("nonexistent") is None

    def test_custom_max_steps(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Task", options={"maxSteps": 5})
        assert run["maxSteps"] == 5

    def test_custom_handoff_timeout(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run(
            "Task", options={"handoffTimeoutMs": 10000}
        )
        assert run["handoffTimeoutMs"] == 10000


class TestTaskRunnerAbortRun:
    def test_abort_queued_run(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Task")
        result = runner.abort_run(run["id"], "User cancelled")

        assert result["status"] == "aborted"
        assert result["summary"] == "User cancelled"
        assert result["finishedAt"]

    def test_abort_finished_run_raises(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Task")
        runner.abort_run(run["id"])

        with pytest.raises(ValueError, match="already finished"):
            runner.abort_run(run["id"])

    def test_abort_nonexistent_raises(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        with pytest.raises(ValueError, match="not found"):
            runner.abort_run("no-such-run")


class TestTaskRunnerManualControl:
    @pytest.mark.asyncio
    async def test_manual_control_queued_raises(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Task")

        with pytest.raises(ValueError, match="not started yet"):
            await runner.request_manual_control(run["id"])

    @pytest.mark.asyncio
    async def test_manual_control_finished_raises(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Task")
        runner.abort_run(run["id"])

        with pytest.raises(ValueError, match="already finished"):
            await runner.request_manual_control(run["id"])

    @pytest.mark.asyncio
    async def test_manual_control_waiting_state(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Task")
        run["status"] = "waiting_for_instruction"
        run["pendingInput"] = {"mode": "guidance", "question": "Continue?"}

        result = await runner.request_manual_control(
            run["id"], "Need manual access"
        )
        assert result["status"] == "manual_control"
        rt.enter_manual_control.assert_called_once()


class TestTaskRunnerResumeRun:
    @pytest.mark.asyncio
    async def test_resume_non_waiting_raises(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Task")

        with pytest.raises(ValueError, match="not waiting"):
            await runner.resume_run(run["id"])

    @pytest.mark.asyncio
    async def test_resume_no_channel_raises(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Task")
        run["status"] = "waiting_for_instruction"
        run["pendingInput"] = {"mode": "guidance"}

        with pytest.raises(ValueError, match="pending reply channel"):
            await runner.resume_run(run["id"])


# ---------------------------------------------------------------------------
# TaskRunner — templates
# ---------------------------------------------------------------------------


class TestTaskRunnerTemplates:
    def test_save_template(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        tmpl = runner.save_template({
            "name": "Login Check",
            "taskInstruction": "Navigate to login",
        })
        assert tmpl["name"] == "Login Check"
        assert tmpl["id"]
        assert runner.get_template(tmpl["id"])["name"] == "Login Check"

    def test_list_templates(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        runner.save_template({"name": "T1", "taskInstruction": "Do A"})
        runner.save_template({"name": "T2", "taskInstruction": "Do B"})

        templates = runner.list_templates()
        assert len(templates) == 2

    def test_update_template(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        tmpl = runner.save_template({
            "name": "Original",
            "taskInstruction": "First",
        })
        updated = runner.save_template({
            "id": tmpl["id"],
            "name": "Updated",
        })
        assert updated["name"] == "Updated"
        assert updated["taskInstruction"] == "First"

    def test_delete_template(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        tmpl = runner.save_template({
            "name": "ToDelete",
            "taskInstruction": "Remove me",
        })
        deleted = runner.delete_template(tmpl["id"])
        assert deleted["name"] == "ToDelete"
        assert runner.get_template(tmpl["id"]) is None

    def test_delete_nonexistent_raises(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        with pytest.raises(ValueError, match="not found"):
            runner.delete_template("no-such-template")

    def test_create_run_from_template(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        tmpl = runner.save_template({
            "name": "Login Flow",
            "taskInstruction": "Login to site",
            "startUrl": "https://example.com/login",
            "timeoutPolicy": {"maxSteps": 5},
        })
        run = runner.create_run_from_template(tmpl["id"])
        assert run["template"]["id"] == tmpl["id"]
        assert run["maxSteps"] == 5

    def test_create_run_from_template_not_found(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        with pytest.raises(ValueError, match="not found"):
            runner.create_run_from_template("no-such")

    def test_compare_template_runs(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        tmpl = runner.save_template({
            "name": "Compare Test",
            "taskInstruction": "Do task",
        })

        # Simulate two runs with same template
        run1 = runner.create_run("Task A")
        run1["template"] = {"id": tmpl["id"]}
        run1["status"] = "completed"
        run1["summary"] = "First run"
        run1["templateEvaluation"] = {"passed": True}

        run2 = runner.create_run("Task B")
        run2["template"] = {"id": tmpl["id"]}
        run2["status"] = "failed"
        run2["summary"] = "Second run"
        run2["templateEvaluation"] = {"passed": False}

        result = runner.compare_template_runs(tmpl["id"])
        assert result["template"]["id"] == tmpl["id"]
        assert len(result["comparisons"]) >= 1


# ---------------------------------------------------------------------------
# TaskRunner — recovery
# ---------------------------------------------------------------------------


class TestTaskRunnerRecovery:
    def test_interrupted_runs_marked_failed(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Interrupted task")
        run["status"] = "running"
        runner._persist_runs()

        # Simulate restart by creating new runner with same dir
        rt2 = _make_mock_runtime()
        runner2 = TaskRunner(str(tmp_path), options=_make_runner_options(rt2))

        recovered = runner2.get_run(run["id"])
        assert recovered["status"] == "failed"
        assert "interrupted" in recovered["summary"].lower()

    def test_queued_runs_preserved(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Queued task")

        rt2 = _make_mock_runtime()
        runner2 = TaskRunner(str(tmp_path), options=_make_runner_options(rt2))
        recovered = runner2.get_run(run["id"])
        assert recovered["status"] == "queued"


# ---------------------------------------------------------------------------
# TaskRunner — history trimming
# ---------------------------------------------------------------------------


class TestTaskRunnerTrimHistory:
    def test_trims_terminal_runs(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(
            str(tmp_path),
            options=_make_runner_options(rt),
        )
        runner._run_history_limit = 3

        for i in range(5):
            run = runner.create_run(f"Task {i}")
            run["status"] = "completed"

        runner._trim_run_history()
        terminal = [r for r in runner.runs if r["status"] == "completed"]
        assert len(terminal) <= 3


# ---------------------------------------------------------------------------
# TaskRunner — events
# ---------------------------------------------------------------------------


class TestTaskRunnerEvents:
    def test_subscribe_receives_events(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))

        events = []
        unsub = runner.subscribe(events.append)

        runner.create_run("Event test")
        assert len(events) >= 1
        assert events[0]["type"] == "run_created"

        unsub()

    def test_unsubscribe_stops_events(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))

        events = []
        unsub = runner.subscribe(events.append)
        unsub()

        runner.create_run("After unsub")
        assert all(e["type"] != "run_created" for e in events)


# ---------------------------------------------------------------------------
# TaskRunner — persistence round-trip
# ---------------------------------------------------------------------------


class TestTaskRunnerPersistence:
    def test_runs_persist_across_instances(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        run = runner.create_run("Persist test")
        run["status"] = "completed"
        runner._persist_runs()

        rt2 = _make_mock_runtime()
        runner2 = TaskRunner(str(tmp_path), options=_make_runner_options(rt2))
        recovered = runner2.get_run(run["id"])
        assert recovered is not None
        assert recovered["taskInstruction"] == "Persist test"
        assert recovered["status"] == "completed"

    def test_templates_persist_across_instances(self, tmp_path):
        rt = _make_mock_runtime()
        runner = TaskRunner(str(tmp_path), options=_make_runner_options(rt))
        runner.save_template({
            "name": "Persistent",
            "taskInstruction": "Keep me",
        })

        rt2 = _make_mock_runtime()
        runner2 = TaskRunner(str(tmp_path), options=_make_runner_options(rt2))
        tmpl = runner2.get_template(
            runner.templates[0]["id"]
        )
        assert tmpl is not None
        assert tmpl["name"] == "Persistent"
