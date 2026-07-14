"""Real workspace environment collection for the Workbench chat inspector."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from naumi_agent.api.chat_environment import ChatEnvironmentCollector
from naumi_agent.api.chat_runs import ChatRunStore
from naumi_agent.background.models import BackgroundStatus, BackgroundTask
from naumi_agent.background.store import BackgroundTaskStore


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_collects_real_git_process_and_source_state(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    source = repo / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('one')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    source.write_text("print('one')\nprint('two')\n", encoding="utf-8")
    (repo / "notes.md").write_text("new\n", encoding="utf-8")

    background = BackgroundTaskStore(tmp_path / "background")
    background.save(
        BackgroundTask(
            id="bg_0001",
            command="python -m http.server 8765 --api-key super-secret",
            cwd=str(repo),
            status=BackgroundStatus.RUNNING,
            output_path=str(tmp_path / "background" / "server.log"),
            pid=4321,
            started_at="2026-07-12T10:00:00+00:00",
        )
    )
    background.save(
        BackgroundTask(
            id="bg_0002",
            command="sleep 30",
            cwd=str(tmp_path / "outside"),
            status=BackgroundStatus.RUNNING,
            output_path=str(tmp_path / "background" / "outside.log"),
            pid=4322,
        )
    )

    runs = ChatRunStore(tmp_path / "runs.db")
    run = await runs.start_run(session_id="sess_1", user_message_id="msg_1")
    await runs.append_artifact(
        run.id,
        kind="source",
        title="app.py",
        summary={"path": str(source)},
        status="ready",
        artifact_id="source-1",
    )
    await runs.append_artifact(
        run.id,
        kind="source",
        title="outside.txt",
        summary={"path": str(tmp_path / "outside.txt")},
        status="ready",
        artifact_id="source-2",
    )

    snapshot = await ChatEnvironmentCollector(
        workspace_root=repo,
        background_store=background,
        chat_run_store=runs,
    ).collect(session_id="sess_1")

    assert snapshot.workspace_root == str(repo.resolve())
    assert snapshot.git.branch == "main"
    assert snapshot.git.changed_files == 2
    assert snapshot.git.additions == 1
    assert snapshot.git.deletions == 0
    assert snapshot.git.dirty is True
    assert [process.id for process in snapshot.processes] == ["bg_0001"]
    assert snapshot.processes[0].command == "python -m http.server 8765 --api-key <redacted>"
    assert [source.path for source in snapshot.sources] == ["src/app.py"]


@pytest.mark.asyncio
async def test_non_git_workspace_returns_explicit_empty_git_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    snapshot = await ChatEnvironmentCollector(
        workspace_root=workspace,
        background_store=BackgroundTaskStore(tmp_path / "background"),
        chat_run_store=ChatRunStore(tmp_path / "runs.db"),
    ).collect(session_id="sess_1")

    assert snapshot.git.available is False
    assert snapshot.git.changed_files == 0
    assert snapshot.processes == []
    assert snapshot.sources == []


@pytest.mark.asyncio
async def test_collect_diff_returns_staged_and_unstaged_files(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    source = repo / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('one')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    source.write_text("print('one')\nprint('two')\n", encoding="utf-8")
    (repo / "notes.md").write_text("new\n", encoding="utf-8")
    staged = repo / "staged.txt"
    staged.write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    diff = await ChatEnvironmentCollector(
        workspace_root=repo,
        background_store=BackgroundTaskStore(tmp_path / "background"),
        chat_run_store=ChatRunStore(tmp_path / "runs.db"),
    ).collect_diff()

    assert diff.available is True
    assert diff.branch == "main"
    assert any(
        f.path == "src/app.py" and f.stage == "unstaged" for f in diff.files
    )
    assert any(
        f.path == "notes.md" and f.stage == "unstaged" for f in diff.files
    )
    assert any(
        f.path == "staged.txt" and f.stage == "staged" for f in diff.files
    )
