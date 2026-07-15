from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Naumi Test")
    _git(repo, "config", "user.email", "naumi@example.test")
    for name in (
        "tracked.txt",
        "preexisting-dirty.txt",
        "further-dirty.txt",
        "deleted.txt",
    ):
        (repo / name).write_text(f"base {name}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")


@pytest.mark.asyncio
async def test_git_probe_attributes_only_net_changes_after_run_baseline(tmp_path):
    from naumi_agent.runs.git_probe import GitWorkspaceProbe, diff_run_changes

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "preexisting-dirty.txt").write_text("dirty before\n", encoding="utf-8")
    (repo / "further-dirty.txt").write_text("dirty before\n", encoding="utf-8")
    before = await GitWorkspaceProbe(repo).capture()

    (repo / "tracked.txt").write_text("changed in run\n", encoding="utf-8")
    (repo / "further-dirty.txt").write_text("changed again in run\n", encoding="utf-8")
    (repo / "new file.txt").write_text("new in run\n", encoding="utf-8")
    (repo / "deleted.txt").unlink()
    after = await GitWorkspaceProbe(repo).capture()

    delta = diff_run_changes(before, after)

    assert delta.git_state.available is True
    assert delta.git_state.branch == "main"
    assert delta.git_state.dirty is True
    assert delta.git_state.commit == _git(repo, "rev-parse", "HEAD")
    assert {change.path for change in delta.changes} == {
        "tracked.txt",
        "further-dirty.txt",
        "new file.txt",
        "deleted.txt",
    }
    assert "preexisting-dirty.txt" not in {change.path for change in delta.changes}
    by_path = {change.path: change for change in delta.changes}
    assert by_path["tracked.txt"].status == "modified"
    assert by_path["new file.txt"].status == "untracked"
    assert by_path["deleted.txt"].status == "deleted"
    assert by_path["tracked.txt"].additions == 1
    assert by_path["tracked.txt"].deletions == 1
    assert delta.warnings == ()


@pytest.mark.asyncio
async def test_git_probe_labels_removed_untracked_paths_as_deleted_effects(tmp_path):
    from naumi_agent.runs.git_probe import GitWorkspaceProbe, diff_run_changes

    repo = tmp_path / "repo"
    _init_repo(repo)
    removed = repo / "scratch" / "generated.txt"
    removed.parent.mkdir()
    removed.write_text("temporary\n", encoding="utf-8")
    before = await GitWorkspaceProbe(repo).capture()

    removed.unlink()
    removed.parent.rmdir()
    after = await GitWorkspaceProbe(repo).capture()

    delta = diff_run_changes(before, after)

    assert [(change.path, change.status) for change in delta.changes] == [
        ("scratch/generated.txt", "removed_untracked")
    ]


def test_receipt_change_scope_defaults_to_task_for_schema_v1_compatibility():
    from naumi_agent.runs.models import ReceiptChange

    change = ReceiptChange.from_dict(
        {
            "path": "src/example.py",
            "status": "modified",
        }
    )

    assert change.scope == "task"


@pytest.mark.asyncio
async def test_git_probe_reports_unavailable_without_inventing_changes(tmp_path):
    from naumi_agent.runs.git_probe import GitWorkspaceProbe, diff_run_changes

    workspace = tmp_path / "not-a-repository"
    workspace.mkdir()
    before = await GitWorkspaceProbe(workspace).capture()
    (workspace / "plain.txt").write_text("not tracked\n", encoding="utf-8")
    after = await GitWorkspaceProbe(workspace).capture()

    delta = diff_run_changes(before, after)

    assert delta.git_state.available is False
    assert delta.changes == ()
    assert delta.warnings
    assert all("Git" in warning for warning in delta.warnings)


@pytest.mark.asyncio
async def test_receipt_builder_uses_real_validation_and_redacts_command_secrets(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    repo = tmp_path / "repo"
    _init_repo(repo)
    builder = await RunReceiptBuilder.start(
        workspace_root=repo,
        run_id="run-1",
        started_at="2026-07-13T00:00:00+00:00",
    )
    builder.observe(
        "tool_start",
        {
            "name": "file_edit",
            "call_id": "edit-1",
            "args": json.dumps({"path": "tracked.txt"}),
        },
    )
    (repo / "tracked.txt").write_text("changed by run\n", encoding="utf-8")
    builder.observe(
        "tool_end",
        {"name": "file_edit", "call_id": "edit-1", "status": "success"},
    )
    builder.observe(
        "permission_bubble",
        {
            "call_id": "test-1",
            "tool_name": "bash_run",
            "status": "confirmed",
            "reason": "用户已允许本次工具执行。",
        },
    )
    builder.observe(
        "tool_start",
        {
            "name": "bash_run",
            "call_id": "test-1",
            "args": json.dumps(
                {
                    "command": (
                        "API_KEY=super-secret-value "
                        "python3 -m pytest tests/unit/test_app.py -q"
                    )
                }
            ),
        },
    )
    builder.observe(
        "tool_end",
        {
            "name": "bash_run",
            "call_id": "test-1",
            "status": "success",
            "duration_ms": 250,
            "content": "3 passed, 1 failed, 2 skipped in 0.20s\n[exit code: 1]",
        },
    )

    receipt = await builder.finish("completed", "实现完成。")

    assert receipt.outcome == "partial"
    assert receipt.summary == "实现完成。"
    assert len(receipt.validations) == 1
    validation = receipt.validations[0]
    assert validation.status == "failed"
    assert validation.exit_code == 1
    assert (validation.passed, validation.failed, validation.skipped) == (3, 1, 2)
    assert validation.scope == "tests/unit/test_app.py"
    assert "super-secret-value" not in validation.command
    assert "<redacted>" in validation.command
    assert receipt.approvals[0].decision == "allowed_once"
    assert receipt.changes[0].path == "tracked.txt"
    assert receipt.changes[0].source_tool == "file_edit"
    assert any(risk.code == "validation_failed" for risk in receipt.risks)
    assert receipt.next_actions[0].kind == "retry_validation"


@pytest.mark.asyncio
async def test_receipt_builder_marks_changed_but_unvalidated_run_partial(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    repo = tmp_path / "repo"
    _init_repo(repo)
    builder = await RunReceiptBuilder.start(workspace_root=repo, run_id="run-2")
    (repo / "tracked.txt").write_text("changed without tests\n", encoding="utf-8")

    receipt = await builder.finish("completed", "代码已修改。")

    assert receipt.outcome == "partial"
    assert receipt.validations == ()
    assert "缺少对应的验证证据" in "\n".join(receipt.unverified)
    assert {action.kind for action in receipt.next_actions} >= {
        "run_validation",
        "review_changes",
    }


@pytest.mark.asyncio
async def test_receipt_builder_completes_verified_rm_and_separates_runtime_trace(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    repo = tmp_path / "repo"
    _init_repo(repo)
    target = repo / "scratch folder"
    target.mkdir()
    (target / "one.txt").write_text("one\n", encoding="utf-8")
    (target / "two.txt").write_text("two\n", encoding="utf-8")
    trace = repo / ".naumi" / "terminal-ui-debug.jsonl"
    trace.parent.mkdir()
    trace.write_text('{"event":"before"}\n', encoding="utf-8")
    builder = await RunReceiptBuilder.start(workspace_root=repo, run_id="run-delete")
    command = f"{shlex.join(['rm', '-rf', str(target)])} 2>/dev/null && echo removed"
    builder.observe(
        "tool_start",
        {
            "name": "bash_run",
            "call_id": "delete-1",
            "args": json.dumps({"command": command}),
        },
    )

    for child in target.iterdir():
        child.unlink()
    target.rmdir()
    trace.write_text(
        '{"event":"before"}\n{"event":"after"}\n',
        encoding="utf-8",
    )
    builder.observe(
        "tool_end",
        {
            "name": "bash_run",
            "call_id": "delete-1",
            "status": "success",
            "content": "Shell 命令执行完成。\n退出码: 0\n\nremoved",
        },
    )

    receipt = await builder.finish("completed", f"已删除 {target} 目录及其所有内容。")

    assert receipt.outcome == "completed"
    assert len(receipt.validations) == 1
    assert receipt.validations[0].status == "passed"
    assert f"路径已不存在: {target}" in receipt.validations[0].command
    by_path = {change.path: change for change in receipt.changes}
    assert by_path["scratch folder/one.txt"].status == "removed_untracked"
    assert by_path["scratch folder/one.txt"].source_tool == "bash_run"
    assert by_path["scratch folder/one.txt"].scope == "task"
    assert by_path[".naumi/terminal-ui-debug.jsonl"].scope == "background"
    assert not any(risk.code == "changes_unverified" for risk in receipt.risks)
    assert receipt.next_actions == ()


@pytest.mark.asyncio
async def test_receipt_builder_rejects_failed_or_unproven_rm_postconditions(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    repo = tmp_path / "repo"
    _init_repo(repo)
    target = repo / "still-here"
    target.mkdir()
    builder = await RunReceiptBuilder.start(workspace_root=repo, run_id="run-delete-failed")
    builder.observe(
        "tool_start",
        {
            "name": "bash_run",
            "call_id": "delete-failed",
            "args": json.dumps({"command": f"rm -rf {target}"}),
        },
    )
    builder.observe(
        "tool_end",
        {
            "name": "bash_run",
            "call_id": "delete-failed",
            "status": "success",
            "content": "Shell 命令执行完成。\n退出码: 1\n[exit code: 1]",
        },
    )

    receipt = await builder.finish("completed", "已尝试删除。")

    assert receipt.outcome == "partial"
    assert any(risk.code == "tool_failed" for risk in receipt.risks)
    assert not receipt.validations


@pytest.mark.asyncio
async def test_receipt_builder_fails_rm_postcondition_when_target_remains(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    repo = tmp_path / "repo"
    _init_repo(repo)
    target = repo / "still-here"
    target.mkdir()
    builder = await RunReceiptBuilder.start(workspace_root=repo, run_id="run-delete-residual")
    builder.observe(
        "tool_start",
        {
            "name": "bash_run",
            "call_id": "delete-residual",
            "args": json.dumps({"command": f"rm -rf {target}"}),
        },
    )
    builder.observe(
        "tool_end",
        {
            "name": "bash_run",
            "call_id": "delete-residual",
            "status": "success",
            "content": "Shell 命令执行完成。\n退出码: 0",
        },
    )

    receipt = await builder.finish("completed", "已尝试删除。")

    assert receipt.outcome == "partial"
    assert receipt.validations[0].status == "failed"
    assert any(risk.code == "validation_failed" for risk in receipt.risks)
    assert receipt.next_actions[0].kind == "retry_validation"


@pytest.mark.asyncio
async def test_receipt_builder_verifies_deleting_symlink_without_following_target(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    repo = tmp_path / "repo"
    _init_repo(repo)
    target = repo / "tracked.txt"
    link = repo / "temporary-link"
    link.symlink_to(target)
    builder = await RunReceiptBuilder.start(workspace_root=repo, run_id="run-delete-link")
    builder.observe(
        "tool_start",
        {
            "name": "file_delete",
            "call_id": "delete-link",
            "args": json.dumps({"path": str(link)}),
        },
    )

    link.unlink()
    builder.observe(
        "tool_end",
        {
            "name": "file_delete",
            "call_id": "delete-link",
            "status": "success",
            "content": "文件已删除。",
        },
    )
    receipt = await builder.finish("completed", "符号链接已删除。")

    assert target.exists()
    assert not link.exists()
    assert receipt.outcome == "completed"
    assert receipt.validations[0].status == "passed"
    assert receipt.changes[0].path == "temporary-link"
    assert receipt.changes[0].source_tool == "file_delete"


@pytest.mark.asyncio
async def test_runtime_trace_only_is_background_but_explicit_trace_edit_is_task(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    repo = tmp_path / "repo"
    _init_repo(repo)
    trace = repo / ".naumi" / "terminal-ui-debug.jsonl"
    trace.parent.mkdir()
    trace.write_text("before\n", encoding="utf-8")
    background_builder = await RunReceiptBuilder.start(
        workspace_root=repo,
        run_id="run-background-only",
    )
    trace.write_text("before\nafter\n", encoding="utf-8")

    background_receipt = await background_builder.finish("completed", "读取完成。")

    assert background_receipt.outcome == "completed"
    assert background_receipt.changes[0].scope == "background"
    assert not background_receipt.unverified

    explicit_builder = await RunReceiptBuilder.start(
        workspace_root=repo,
        run_id="run-explicit-trace-edit",
    )
    explicit_builder.observe(
        "tool_start",
        {
            "name": "file_write",
            "call_id": "write-trace",
            "args": json.dumps({"path": str(trace)}),
        },
    )
    trace.write_text("explicit edit\n", encoding="utf-8")
    explicit_builder.observe(
        "tool_end",
        {
            "name": "file_write",
            "call_id": "write-trace",
            "status": "success",
        },
    )

    explicit_receipt = await explicit_builder.finish("completed", "轨迹已编辑。")

    assert explicit_receipt.outcome == "partial"
    assert explicit_receipt.changes[0].scope == "task"
    assert explicit_receipt.changes[0].source_tool == "file_write"
    assert any(risk.code == "changes_unverified" for risk in explicit_receipt.risks)


@pytest.mark.asyncio
async def test_delete_postcondition_does_not_mask_unvalidated_code_change(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    repo = tmp_path / "repo"
    _init_repo(repo)
    disposable = repo / "disposable.txt"
    disposable.write_text("temporary\n", encoding="utf-8")
    builder = await RunReceiptBuilder.start(workspace_root=repo, run_id="run-mixed-change")
    builder.observe(
        "tool_start",
        {
            "name": "file_edit",
            "call_id": "edit-code",
            "args": json.dumps({"path": "tracked.txt"}),
        },
    )
    (repo / "tracked.txt").write_text("code changed\n", encoding="utf-8")
    builder.observe(
        "tool_end",
        {"name": "file_edit", "call_id": "edit-code", "status": "success"},
    )
    builder.observe(
        "tool_start",
        {
            "name": "file_delete",
            "call_id": "delete-temp",
            "args": json.dumps({"path": str(disposable)}),
        },
    )
    disposable.unlink()
    builder.observe(
        "tool_end",
        {
            "name": "file_delete",
            "call_id": "delete-temp",
            "status": "success",
        },
    )

    receipt = await builder.finish("completed", "修改与清理已完成。")

    assert receipt.validations[0].scope == "文件系统"
    assert receipt.validations[0].status == "passed"
    assert receipt.outcome == "partial"
    assert any(risk.code == "changes_unverified" for risk in receipt.risks)
    assert any(action.kind == "run_validation" for action in receipt.next_actions)


@pytest.mark.asyncio
async def test_receipt_builder_preserves_changes_when_run_is_cancelled(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    repo = tmp_path / "repo"
    _init_repo(repo)
    builder = await RunReceiptBuilder.start(workspace_root=repo, run_id="run-3")
    (repo / "tracked.txt").write_text("changed before cancel\n", encoding="utf-8")

    receipt = await builder.finish("cancelled", "运行已取消。")

    assert receipt.outcome == "cancelled"
    assert [change.path for change in receipt.changes] == ["tracked.txt"]
    assert any(action.kind == "continue_run" for action in receipt.next_actions)


@pytest.mark.asyncio
async def test_receipt_builder_reports_git_unavailable_without_claiming_changes(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    workspace = tmp_path / "not-a-repository"
    workspace.mkdir()
    builder = await RunReceiptBuilder.start(
        workspace_root=workspace,
        run_id="run-no-git",
    )
    (workspace / "plain.txt").write_text("not observable by Git\n", encoding="utf-8")

    receipt = await builder.finish("completed", "运行结束。")

    assert receipt.outcome == "partial"
    assert receipt.changes == ()
    assert receipt.git_state.available is False
    assert any(risk.code == "git_unavailable" for risk in receipt.risks)
    assert any("Git" in item for item in receipt.unverified)


@pytest.mark.asyncio
async def test_receipt_builder_preserves_denied_approval_as_blocking_risk(tmp_path):
    from naumi_agent.runs.receipt_builder import RunReceiptBuilder

    repo = tmp_path / "repo"
    _init_repo(repo)
    builder = await RunReceiptBuilder.start(workspace_root=repo, run_id="run-denied")
    builder.observe(
        "permission_bubble",
        {
            "call_id": "dangerous-1",
            "tool_name": "bash_run",
            "status": "denied",
        },
    )

    receipt = await builder.finish("completed", "请求未执行。")

    assert receipt.outcome == "partial"
    assert receipt.approvals[0].decision == "denied"
    assert any(risk.code == "approval_not_granted" for risk in receipt.risks)
    assert receipt.next_actions[0].kind == "request_approval"


@pytest.mark.asyncio
async def test_run_recorder_persists_steps_and_receipt_before_returning(tmp_path):
    from naumi_agent.runs.recorder import ChatRunRecorder
    from naumi_agent.runs.store import ChatRunStore

    repo = tmp_path / "repo"
    _init_repo(repo)
    db_path = tmp_path / "chat-runs.db"
    store = ChatRunStore(db_path)
    recorder = await ChatRunRecorder.start(
        store=store,
        workspace_root=repo,
        session_id="session-1",
        task="修改并验证 tracked.txt",
        run_id="run-recorded",
    )

    await recorder.observe("thinking_start", {"turn": 1})
    await recorder.observe(
        "tool_start",
        {
            "name": "file_edit",
            "call_id": "edit-1",
            "args": json.dumps({"path": "tracked.txt"}),
        },
    )
    (repo / "tracked.txt").write_text("recorded change\n", encoding="utf-8")
    await recorder.observe(
        "tool_end",
        {
            "name": "file_edit",
            "call_id": "edit-1",
            "status": "success",
            "content": "updated",
        },
    )
    await recorder.observe("response_start", {"turn": 1})
    receipt = await recorder.finish("cancelled", "用户取消了本轮运行。")

    restored = await ChatRunStore(db_path).get_run("session-1", "run-recorded")
    assert restored is not None
    assert restored.status == "cancelled"
    assert restored.receipt == receipt
    assert restored.receipt.outcome == "cancelled"
    assert [step.stage for step in restored.steps] == [
        "request",
        "analysis",
        "tool",
        "response",
    ]
    assert restored.steps[2].status == "completed"
    assert restored.user_message_id.startswith("msg-")


@pytest.mark.asyncio
async def test_run_recorder_finish_is_idempotent(tmp_path):
    from naumi_agent.runs.recorder import ChatRunRecorder
    from naumi_agent.runs.store import ChatRunStore

    repo = tmp_path / "repo"
    _init_repo(repo)
    recorder = await ChatRunRecorder.start(
        store=ChatRunStore(tmp_path / "chat-runs.db"),
        workspace_root=repo,
        session_id="session-1",
        task="只回答问题",
        run_id="run-idempotent",
    )

    first = await recorder.finish("completed", "回答完成。")
    second = await recorder.finish("failed", "不应覆盖。")

    assert second is first
    assert second.outcome == "completed"


@pytest.mark.asyncio
async def test_run_recorder_event_sink_persists_runtime_event_identity(tmp_path):
    from datetime import UTC, datetime

    from naumi_agent.runs.recorder import ChatRunRecorder, ChatRunRecorderEventSink
    from naumi_agent.runs.store import ChatRunStore
    from naumi_agent.runtime.ports.events import EventSink, RuntimeEvent, RuntimeEventType

    repo = tmp_path / "repo"
    _init_repo(repo)
    store = ChatRunStore(tmp_path / "chat-runs.db")
    recorder = await ChatRunRecorder.start(
        store=store,
        workspace_root=repo,
        session_id="session-1",
        task="读取 tracked.txt",
        run_id="run-event-sink",
    )
    sink = ChatRunRecorderEventSink(recorder)
    event = RuntimeEvent(
        id="event-tool-start",
        type=RuntimeEventType.TOOL_START,
        data={"name": "file_read", "call_id": "read-1"},
        timestamp=datetime.now(UTC).isoformat(),
        session_id="session-1",
        run_id="run-event-sink",
        sequence=1,
    )

    assert isinstance(sink, EventSink)
    await sink.emit(event)

    restored = await ChatRunStore(store.db_path).get_run("session-1", "run-event-sink")
    assert restored is not None
    assert restored.steps[-1].stage == "tool"
    assert restored.steps[-1].event_id == "event-tool-start"
    assert restored.steps[-1].metadata == {}
