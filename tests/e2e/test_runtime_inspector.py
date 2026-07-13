"""Real-process acceptance coverage for the authoritative Runtime Inspector."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runs.recorder import ChatRunRecorder
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore
from naumi_agent.tui.runtime_inspector import format_runtime_inspector_markdown
from naumi_agent.ui.bridge import JsonlEngineBridge


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _engine(workspace: Path, state_dir: Path) -> AgentEngine:
    return AgentEngine(
        AppConfig(
            workspace_root=str(workspace),
            memory=MemoryConfig(
                session_db_path=str(state_dir / "sessions.db"),
                vector_db_path=str(state_dir / "vectors"),
                long_term_enabled=False,
            ),
        )
    )


async def _observe_tool(
    engine: AgentEngine,
    recorder: ChatRunRecorder,
    event: str,
    payload: dict[str, object],
) -> None:
    await recorder.observe(event, payload)
    engine.runtime_inspector.observe(event, payload)


def _bridge_records(writer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in writer.getvalue().splitlines() if line]


def _render_node_inspector(
    project_root: Path,
    snapshot: dict[str, object],
) -> dict[str, dict[str, str]]:
    script = r"""
import fs from 'node:fs';
import { stripAnsi } from './frontend/terminal-ui/src/ansi.js';
import { createInitialState, reduceServerEvent } from './frontend/terminal-ui/src/state.js';
import { renderScreen } from './frontend/terminal-ui/src/render.js';
const snapshot = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = {};
for (const width of [140, 110, 80]) {
  result[String(width)] = {};
  for (const tab of ['plan', 'tools', 'context', 'changes', 'tests']) {
    const state = createInitialState();
    state.currentSessionId = snapshot.session_id;
    state.inspector.open = true;
    state.inspector.selectedTab = tab;
    reduceServerEvent(state, { type: 'inspector/snapshot', payload: snapshot });
    result[String(width)][tab] = renderScreen(state, width, 32)
      .map(stripAnsi)
      .join('\n');
  }
}
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=project_root,
        env=os.environ.copy(),
        input=json.dumps(snapshot, ensure_ascii=False),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.asyncio
async def test_real_git_todo_pytest_sqlite_bridge_node_and_textual_inspector(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "inspector-workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.name", "Naumi Inspector E2E")
    _git(workspace, "config", "user.email", "inspector-e2e@example.test")
    (workspace / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (workspace / "tracked.txt").write_text("before\n", encoding="utf-8")
    (workspace / "test_real.py").write_text(
        "def test_real_inspector_value():\n    assert 6 * 7 == 42\n",
        encoding="utf-8",
    )
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "baseline")

    state_dir = tmp_path / "state"
    engine = _engine(workspace, state_dir)
    try:
        session = await engine.get_or_create_session(title="Runtime Inspector 真实验收")
        task_store = engine.task_store.scoped(session.id)
        task = await task_store.create_task("完成 Runtime Inspector 真实验收")
        await task_store.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            active_form="正在执行真实 Git 与 pytest 验收",
        )
        recorder = await ChatRunRecorder.start(
            store=engine.chat_run_store,
            workspace_root=workspace,
            session_id=session.id,
            task="修改 tracked.txt 并执行真实 pytest",
            run_id="run-inspector-real",
        )

        edit_start = {
            "event_id": "edit-start-real",
            "session_id": session.id,
            "run_id": "run-inspector-real",
            "name": "file_edit",
            "call_id": "edit-real",
            "args": json.dumps({"path": "tracked.txt"}),
        }
        await _observe_tool(engine, recorder, "tool_start", edit_start)
        (workspace / "tracked.txt").write_text("after\n", encoding="utf-8")
        await _observe_tool(
            engine,
            recorder,
            "tool_end",
            {
                **edit_start,
                "event_id": "edit-end-real",
                "status": "success",
                "duration_ms": 7,
            },
        )

        command = f"{sys.executable} -m pytest test_real.py -q"
        test_start = {
            "event_id": "pytest-start-real",
            "session_id": session.id,
            "run_id": "run-inspector-real",
            "name": "bash_run",
            "call_id": "pytest-real",
            "args": json.dumps({"command": command}),
        }
        await _observe_tool(engine, recorder, "tool_start", test_start)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "test_real.py", "-q"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        await _observe_tool(
            engine,
            recorder,
            "tool_end",
            {
                **test_start,
                "event_id": "pytest-end-real",
                "status": "success",
                "duration_ms": 12,
                "content": (
                    completed.stdout
                    + completed.stderr
                    + f"\n[exit code: {completed.returncode}]"
                ),
            },
        )
        receipt = await recorder.finish(
            "completed",
            "Runtime Inspector 真实文件修改与验证均已完成。",
        )
        engine.runtime_inspector.observe("completion_receipt", receipt.to_dict())

        reopened_runs = ChatRunStore(engine.chat_run_store.db_path)
        assert await reopened_runs.get_receipt(session.id, receipt.receipt_id) == receipt
        reopened_tasks = TaskStore(str(state_dir / "sessions.db")).scoped(session.id)
        assert [item.subject for item in await reopened_tasks.list_tasks()] == [
            "完成 Runtime Inspector 真实验收"
        ]

        snapshot = await engine.runtime_inspector.snapshot()
        assert snapshot.context.branch == "main"
        assert snapshot.context.git_available is True
        assert snapshot.plan.items[0].subject == "完成 Runtime Inspector 真实验收"
        assert snapshot.tools.items[0].name == "file_edit"
        assert snapshot.changes.items[0].path == "tracked.txt"
        assert snapshot.tests.validations[0].status == "passed"
        assert snapshot.tests.validations[0].passed == 1

        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)
        await bridge.show_inspector(
            {
                "open": True,
                "known_revision": 0,
                "session_id": session.id,
            },
            request_id="inspector-real-open",
        )
        bridge_snapshot = next(
            record["payload"]
            for record in _bridge_records(writer)
            if record["type"] == "inspector/snapshot"
        )
        assert bridge_snapshot["session_id"] == session.id
        assert bridge_snapshot["changes"]["items"][0]["path"] == "tracked.txt"

        node_rendered = _render_node_inspector(project_root, bridge_snapshot)
        for width in ("140", "110", "80"):
            combined = "\n".join(node_rendered[width].values())
            compact = "".join(
                char
                for char in combined
                if not char.isspace() and char not in "│┌┐└┘─"
            )
            assert "Runtime Inspector" in combined
            assert "完成RuntimeInspector真实验收" in compact
            assert "tracked.txt" in compact
            assert "test_real.py" in compact
            assert "main" in compact

        textual = "\n".join(
            format_runtime_inspector_markdown(snapshot, tab)
            for tab in ("plan", "tools", "context", "changes", "tests")
        )
        assert "完成 Runtime Inspector 真实验收" in textual
        assert "tracked.txt" in textual
        assert "test_real.py" in textual
        assert "Git：main" in textual
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_real_no_git_failed_validation_gap_recovery_and_session_isolation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()
    (workspace / "test_fail.py").write_text(
        "def test_real_failure():\n    assert 'actual' == 'expected'\n",
        encoding="utf-8",
    )
    engine = _engine(workspace, tmp_path / "plain-state")
    try:
        session = await engine.get_or_create_session(title="Inspector 无 Git 失败验收")
        recorder = await ChatRunRecorder.start(
            store=engine.chat_run_store,
            workspace_root=workspace,
            session_id=session.id,
            task="执行真实失败测试",
            run_id="run-inspector-failed",
        )
        command = f"{sys.executable} -m pytest test_fail.py -q"
        start = {
            "event_id": "failed-start-real",
            "session_id": session.id,
            "run_id": "run-inspector-failed",
            "name": "bash_run",
            "call_id": "pytest-failed",
            "args": json.dumps({"command": command}),
        }
        await _observe_tool(engine, recorder, "tool_start", start)
        failed = subprocess.run(
            [sys.executable, "-m", "pytest", "test_fail.py", "-q"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        assert failed.returncode == 1
        await _observe_tool(
            engine,
            recorder,
            "tool_end",
            {
                **start,
                "event_id": "failed-end-real",
                "status": "success",
                "content": (
                    failed.stdout
                    + failed.stderr
                    + f"\n[exit code: {failed.returncode}]"
                ),
            },
        )
        receipt = await recorder.finish("completed", "真实失败测试已执行。")
        engine.runtime_inspector.observe("completion_receipt", receipt.to_dict())

        first = await engine.runtime_inspector.snapshot()
        assert first.context.git_available is False
        assert first.plan.items == ()
        assert first.tests.validations[0].status == "failed"
        assert first.tests.validations[0].failed == 1

        engine.runtime_inspector.observe(
            "tool_start",
            {
                "event_id": "gap-tool-start",
                "session_id": session.id,
                "run_id": "run-gap",
                "name": "file_read",
                "call_id": "gap-read",
            },
        )
        await engine.runtime_inspector.snapshot()
        engine.runtime_inspector.observe(
            "tool_end",
            {
                "event_id": "gap-tool-end",
                "session_id": session.id,
                "run_id": "run-gap",
                "name": "file_read",
                "call_id": "gap-read",
                "status": "success",
            },
        )
        current = await engine.runtime_inspector.snapshot()
        assert current.revision >= first.revision + 2

        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)
        await bridge.show_inspector(
            {
                "open": True,
                "known_revision": first.revision,
                "session_id": session.id,
            },
            request_id="inspector-gap-recovery",
        )
        recovered = _bridge_records(writer)[-1]
        assert recovered["type"] == "inspector/snapshot"
        assert recovered["payload"]["revision"] == current.revision

        await bridge.show_inspector(
            {
                "open": True,
                "known_revision": current.revision,
                "session_id": "another-session",
            },
            request_id="inspector-cross-session",
        )
        rejected = _bridge_records(writer)[-1]
        assert rejected["type"] == "error"
        assert rejected["payload"]["code"] == "inspector_session_mismatch"
    finally:
        await engine.shutdown()
