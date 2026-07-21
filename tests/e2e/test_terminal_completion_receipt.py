"""Real-process acceptance coverage for terminal completion receipts."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runs.receipt_builder import RunReceiptBuilder
from naumi_agent.runs.recorder import ChatRunRecorder
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.tools.builtin import BashRunTool
from naumi_agent.tui.completion_receipt import format_completion_receipt_markdown
from naumi_agent.ui.bridge import JsonlEngineBridge


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.mark.asyncio
async def test_real_delete_receipt_is_verified_compact_and_background_aware(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "delete-workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.name", "Naumi E2E")
    _git(workspace, "config", "user.email", "naumi-e2e@example.test")
    (workspace / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "base")

    target = workspace / "test artifacts"
    target.mkdir()
    for index in range(6):
        (target / f"file-{index}.txt").write_text(f"{index}\n", encoding="utf-8")
    trace = workspace / ".naumi" / "terminal-ui-debug.jsonl"
    trace.parent.mkdir()
    trace.write_text('{"event":"before"}\n', encoding="utf-8")

    builder = await RunReceiptBuilder.start(workspace_root=workspace, run_id="run-real-delete")
    command = shlex.join(["rm", "-rf", str(target)])
    builder.observe(
        "tool_start",
        {
            "name": "bash_run",
            "call_id": "delete-real",
            "args": json.dumps({"command": command}),
        },
    )
    result = await BashRunTool(
        workspace_root=workspace,
        output_dir=tmp_path / "shell-output",
    ).execute(command=command)
    trace.write_text(
        '{"event":"before"}\n{"event":"after"}\n',
        encoding="utf-8",
    )
    builder.observe(
        "tool_end",
        {
            "name": "bash_run",
            "call_id": "delete-real",
            "status": "success",
            "content": result,
        },
    )
    receipt = await builder.finish("completed", f"已删除 {target} 目录及其所有内容。")

    assert not target.exists()
    assert receipt.outcome == "completed"
    assert [(item.status, item.exit_code) for item in receipt.validations] == [
        ("passed", 0)
    ]
    task_changes = [item for item in receipt.changes if item.scope == "task"]
    background_changes = [item for item in receipt.changes if item.scope == "background"]
    assert len(task_changes) == 6
    assert {item.status for item in task_changes} == {"removed_untracked"}
    assert {item.source_tool for item in task_changes} == {"bash_run"}
    assert [item.path for item in background_changes] == [
        ".naumi/terminal-ui-debug.jsonl"
    ]
    assert receipt.next_actions == ()

    textual = format_completion_receipt_markdown(receipt)
    assert "完成回执 · 已完成" in textual
    assert "验证通过" in textual
    assert "影响：删除 6 个文件" in textual
    assert "工作区另有 1 项运行时变化" in textual
    assert "file-0.txt" not in textual

    node_script = r"""
import { stripAnsi } from './frontend/terminal-ui/src/ansi.js';
import {
  CompletionReceiptCard,
} from './frontend/terminal-ui/src/components/completion-receipt-card.js';
import { renderComponent } from './frontend/terminal-ui/src/components/core.js';
import { normalizeServerRecord } from './frontend/terminal-ui/src/protocol.js';
const payload = JSON.parse(process.env.NAUMI_RECEIPT_JSON);
const receipt = normalizeServerRecord({type: 'completion/receipt', payload}).payload;
const rendered = renderComponent(CompletionReceiptCard({receipt}), {width: 80});
process.stdout.write(rendered.map(stripAnsi).join('\n'));
"""
    node = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        cwd=project_root,
        env={
            **os.environ,
            "NAUMI_RECEIPT_JSON": json.dumps(receipt.to_dict(), ensure_ascii=False),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert node.returncode == 0, node.stderr
    assert "已完成" in node.stdout
    assert "影响 · 删除 6 个文件" in node.stdout
    assert "工作区另有 1 项运行时变化" in node.stdout
    assert "file-0.txt" not in node.stdout


@pytest.mark.asyncio
async def test_real_git_pytest_sqlite_bridge_and_both_terminal_renderers(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "real-workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.name", "Naumi E2E")
    _git(workspace, "config", "user.email", "naumi-e2e@example.test")
    (workspace / "tracked.txt").write_text("before\n", encoding="utf-8")
    (workspace / "test_real.py").write_text(
        "def test_real_workspace_value():\n    assert 6 * 7 == 42\n",
        encoding="utf-8",
    )
    (workspace / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "base")

    state_dir = tmp_path / "state"
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(workspace),
            memory=MemoryConfig(
                session_db_path=str(state_dir / "sessions.db"),
                vector_db_path=str(state_dir / "vectors"),
                long_term_enabled=False,
            ),
        )
    )
    try:
        session = await engine.session_store.create_session(title="真实回执验收")
        session.add_message("user", "修改 tracked.txt 并运行真实 pytest")
        await engine.session_store.save(session)
        engine._session = session
        recorder = await ChatRunRecorder.start(
            store=engine.chat_run_store,
            workspace_root=workspace,
            session_id=session.id,
            task="修改 tracked.txt 并运行真实 pytest",
            run_id="run-real-e2e",
        )

        await recorder.observe(
            "tool_start",
            {
                "name": "file_edit",
                "call_id": "edit-real",
                "args": json.dumps({"path": "tracked.txt"}),
            },
        )
        (workspace / "tracked.txt").write_text("after\n", encoding="utf-8")
        await recorder.observe(
            "tool_end",
            {
                "name": "file_edit",
                "call_id": "edit-real",
                "status": "success",
            },
        )

        command = f"{sys.executable} -m pytest test_real.py -q"
        await recorder.observe(
            "permission_bubble",
            {
                "call_id": "pytest-real",
                "tool_name": "bash_run",
                "status": "confirmed",
            },
        )
        await recorder.observe(
            "tool_start",
            {
                "name": "bash_run",
                "call_id": "pytest-real",
                "args": json.dumps({"command": command}),
            },
        )
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "test_real.py", "-q"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        await recorder.observe(
            "tool_end",
            {
                "name": "bash_run",
                "call_id": "pytest-real",
                "status": "success",
                "content": (
                    completed.stdout
                    + completed.stderr
                    + f"\n[exit code: {completed.returncode}]"
                ),
            },
        )
        receipt = await recorder.finish("completed", "真实文件修改与验证均已完成。")

        assert receipt.outcome == "completed"
        assert [change.path for change in receipt.changes] == ["tracked.txt"]
        assert receipt.changes[0].source_tool == "file_edit"
        assert len(receipt.validations) == 1
        assert receipt.validations[0].status == "passed"
        assert receipt.validations[0].passed == 1
        assert receipt.validations[0].scope == "test_real.py"
        assert receipt.approvals[0].decision == "allowed_once"

        reopened = ChatRunStore(engine.chat_run_store.db_path)
        stored = await reopened.get_receipt(session.id, receipt.receipt_id)
        assert stored == receipt

        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)
        await bridge.resume_session(
            {"session_id": session.id, "clear": True},
            request_id="resume-real-e2e",
        )
        records = [json.loads(line) for line in writer.getvalue().splitlines()]
        replayed = [
            record
            for record in records
            if record["type"] == "completion/receipt"
        ]
        assert len(replayed) == 1
        assert replayed[0]["payload"]["receipt_id"] == receipt.receipt_id

        textual = format_completion_receipt_markdown(receipt)
        assert "完成回执 · 已完成" in textual
        assert "影响：修改 1 个文件" in textual
        assert "pytest test_real.py -q" in textual

        node_script = r"""
import { stripAnsi } from './frontend/terminal-ui/src/ansi.js';
import { normalizeServerRecord } from './frontend/terminal-ui/src/protocol.js';
import { createInitialState, reduceServerEvent } from './frontend/terminal-ui/src/state.js';
import { renderBody } from './frontend/terminal-ui/src/render.js';
const receipt = JSON.parse(process.env.NAUMI_RECEIPT_JSON);
const record = normalizeServerRecord({type: 'completion/receipt', payload: receipt});
const state = createInitialState();
reduceServerEvent(state, record);
process.stdout.write(renderBody(state, 80).map(stripAnsi).join('\n'));
"""
        node = subprocess.run(
            ["node", "--input-type=module", "-e", node_script],
            cwd=project_root,
            env={
                **os.environ,
                "NAUMI_RECEIPT_JSON": json.dumps(
                    receipt.to_dict(),
                    ensure_ascii=False,
                ),
            },
            check=False,
            capture_output=True,
            text=True,
        )
        assert node.returncode == 0, node.stderr
        assert "完成回执" in node.stdout
        assert "真实文件修改与验证均已完成" in node.stdout
        assert "影响 · 修改 1 个文件" in node.stdout
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_real_failure_cancel_read_only_and_no_git_receipts(tmp_path: Path) -> None:
    workspace = tmp_path / "edge-workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.name", "Naumi E2E")
    _git(workspace, "config", "user.email", "naumi-e2e@example.test")
    (workspace / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (workspace / "tracked.txt").write_text("before\n", encoding="utf-8")
    (workspace / "test_fail.py").write_text(
        "def test_real_failure():\n    assert 'actual' == 'expected'\n",
        encoding="utf-8",
    )
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "base")
    store = ChatRunStore(tmp_path / "edge-runs.db")

    failing = await ChatRunRecorder.start(
        store=store,
        workspace_root=workspace,
        session_id="edge-session",
        task="运行真实失败测试",
        run_id="run-real-failure",
    )
    fail_command = f"{sys.executable} -m pytest test_fail.py -q"
    await failing.observe(
        "tool_start",
        {
            "name": "bash_run",
            "call_id": "fail-real",
            "args": json.dumps({"command": fail_command}),
        },
    )
    failed_process = subprocess.run(
        [sys.executable, "-m", "pytest", "test_fail.py", "-q"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed_process.returncode == 1
    await failing.observe(
        "tool_end",
        {
            "name": "bash_run",
            "call_id": "fail-real",
            "status": "success",
            "content": (
                failed_process.stdout
                + failed_process.stderr
                + f"\n[exit code: {failed_process.returncode}]"
            ),
        },
    )
    failed_receipt = await failing.finish("completed", "测试运行结束。")
    assert failed_receipt.outcome == "partial"
    assert failed_receipt.validations[0].failed == 1
    assert failed_receipt.next_actions[0].kind == "retry_validation"

    cancelled = await ChatRunRecorder.start(
        store=store,
        workspace_root=workspace,
        session_id="edge-session",
        task="修改后取消",
        run_id="run-real-cancelled",
    )
    (workspace / "tracked.txt").write_text("changed before cancel\n", encoding="utf-8")
    cancelled_receipt = await cancelled.finish("cancelled", "用户取消。")
    assert cancelled_receipt.outcome == "cancelled"
    assert [change.path for change in cancelled_receipt.changes] == ["tracked.txt"]
    assert any(action.kind == "continue_run" for action in cancelled_receipt.next_actions)

    _git(workspace, "restore", "tracked.txt")
    read_only = await ChatRunRecorder.start(
        store=store,
        workspace_root=workspace,
        session_id="edge-session",
        task="只读取状态",
        run_id="run-real-read-only",
    )
    read_only_receipt = await read_only.finish("completed", "读取完成。")
    assert read_only_receipt.outcome == "completed"
    assert read_only_receipt.changes == ()

    plain_workspace = tmp_path / "plain-workspace"
    plain_workspace.mkdir()
    no_git = await ChatRunRecorder.start(
        store=store,
        workspace_root=plain_workspace,
        session_id="edge-session",
        task="在无 Git 目录检查状态",
        run_id="run-real-no-git",
    )
    no_git_receipt = await no_git.finish("completed", "检查结束。")
    assert no_git_receipt.outcome == "partial"
    assert no_git_receipt.changes == ()
    assert any(risk.code == "git_unavailable" for risk in no_git_receipt.risks)
