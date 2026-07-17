"""Authoritative Workbench worktree snapshot projection tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from naumi_agent.tasks.store import TaskStore
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore
from naumi_agent.worktree.manager import WorktreeManager
from naumi_agent.worktree.models import WorktreeRecord


@pytest.mark.asyncio
async def test_dashboard_projects_real_dirty_worktree_lease_and_agent(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    database = tmp_path / "workbench.db"
    session_id = "session-worktrees"
    task_store = TaskStore(database)
    task_store.set_session(session_id)
    store = WorkbenchStore(database)
    manager = WorktreeManager(
        repo_root=repo,
        storage_dir=tmp_path / "managed-worktrees",
        task_store=task_store,
    )
    service = WorkbenchService(
        task_store=task_store,
        workbench_store=store,
        worktree_manager=manager,
    )
    mission = await service.create_mission(
        session_id=session_id,
        title="Worktree 终端页",
        goal="展示真实 Git 状态",
    )
    issue = await service.create_issue(
        session_id=session_id,
        mission_id=mission.id,
        title="实现 Worktrees tab",
    )
    task_id = issue["task_id"]
    assert "已创建" in await manager.create("ui-10-worktrees", task_id)
    await store.set_issue_worktree(
        session_id=session_id,
        task_id=task_id,
        worktree_name="ui-10-worktrees",
    )
    lease = await store.create_lease(
        session_id=session_id,
        task_id=task_id,
        agent_id="Frontend-Agent",
        expires_at="2099-01-01T00:00:00+00:00",
        worktree_name="ui-10-worktrees",
    )
    record = await manager.status("ui-10-worktrees")
    assert not isinstance(record, list)
    (Path(record.path) / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    first = await service.dashboard_snapshot(session_id)
    duplicate = await service.dashboard_snapshot(session_id)

    assert first["revision"] == duplicate["revision"] == 1
    assert first["worktrees_status"] == "ready"
    assert first["worktrees_code"] == ""
    assert first["worktrees_total"] == 1
    assert first["worktrees_truncated"] is False
    assert first["counts"]["worktrees"] == 1
    assert first["active_selection"]["worktree"] == "ui-10-worktrees"
    assert len(first["worktrees"]) == 1
    projected = first["worktrees"][0]
    assert projected["name"] == "ui-10-worktrees"
    assert projected["path"] == record.path
    assert projected["branch"] == "naumi/worktree-ui-10-worktrees"
    assert projected["status"] == "dirty"
    assert projected["dirty_files"] == 1
    assert projected["commits_ahead"] == 0
    assert projected["removable"] is False
    assert projected["task"]["id"] == task_id
    assert projected["lease"]["id"] == lease.id
    assert projected["lease"]["state"] == "active"
    assert projected["agent_id"] == "Frontend-Agent"


@pytest.mark.asyncio
async def test_worktree_projection_failure_preserves_other_dashboard_data(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.db"
    session_id = "session-worktree-failure"
    task_store = TaskStore(database)
    task_store.set_session(session_id)
    store = WorkbenchStore(database)
    service = WorkbenchService(
        task_store=task_store,
        workbench_store=store,
        worktree_manager=_FailingWorktreeManager(),
    )
    await service.create_mission(
        session_id=session_id,
        title="仍可见的目标",
        goal="Worktree 失败不能拖垮 Overview",
    )

    snapshot = await service.dashboard_snapshot(session_id)

    assert snapshot["missions"][0]["title"] == "仍可见的目标"
    assert snapshot["worktrees"] == []
    assert snapshot["worktrees_status"] == "unavailable"
    assert snapshot["worktrees_code"] == "worktree_snapshot_failed"
    assert snapshot["counts"]["worktrees"] == 0


@pytest.mark.asyncio
async def test_unchanged_worktree_refresh_keeps_stable_updated_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    manager = WorktreeManager(
        repo_root=repo,
        storage_dir=tmp_path / "managed-worktrees",
    )
    assert "已创建" in await manager.create("stable")
    first = await manager.status("stable")
    assert not isinstance(first, list)
    monkeypatch.setattr(
        "naumi_agent.worktree.manager._now",
        lambda: "2099-01-01T00:00:00",
    )

    second = await manager.status("stable")

    assert not isinstance(second, list)
    assert second.updated_at == first.updated_at


@pytest.mark.asyncio
async def test_worktree_projection_caps_payload_and_reports_real_total(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.db"
    task_store = TaskStore(database)
    task_store.set_session("session-many-worktrees")
    service = WorkbenchService(
        task_store=task_store,
        workbench_store=WorkbenchStore(database),
        worktree_manager=_BulkWorktreeManager(201),
    )

    snapshot = await service.dashboard_snapshot("session-many-worktrees")

    assert len(snapshot["worktrees"]) == 200
    assert snapshot["worktrees_total"] == 201
    assert snapshot["worktrees_truncated"] is True
    assert snapshot["counts"]["worktrees"] == 201


class _FailingWorktreeManager:
    async def status(self, name: str = "") -> list[object]:
        del name
        raise RuntimeError("PRIVATE_WORKTREE_FAILURE")


class _BulkWorktreeManager:
    def __init__(self, count: int) -> None:
        self._count = count

    async def status(self, name: str = "") -> list[WorktreeRecord]:
        del name
        return [
            WorktreeRecord(
                name=f"worktree-{index:03d}",
                path=f"/tmp/worktree-{index:03d}",
                branch=f"naumi/worktree-{index:03d}",
                base_ref="deadbeef",
            )
            for index in range(self._count)
        ]


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "tests@naumi.local")
    _git(path, "config", "user.name", "Naumi Tests")
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-qm", "initial")
    return path


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()
