"""Tests for the review evidence collector (real git diff collection)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from naumi_agent.workbench.models import (
    ApprovalState,
    ParallelMode,
    RiskLevel,
)
from naumi_agent.workbench.review_evidence import (
    ReviewEvidenceCollector,
    _git_status_label,
    _parse_diff_hunks,
)
from naumi_agent.workbench.store import WorkbenchStore


def _run_git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.check_output(
        ["git", "-C", str(cwd), *args],
        env=env,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.name", "Test")
    _run_git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# initial\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-q", "-m", "initial")
    return repo


async def _seed_approval_and_issue(
    store: WorkbenchStore, session_id: str, task_id: str, worktree_name: str
) -> str:
    await store.create_mission(session_id, "Mission", "Goal")
    missions = await store.list_missions(session_id)
    mission_id = missions[0].id
    await store.upsert_issue(
        session_id=session_id,
        task_id=task_id,
        mission_id=mission_id,
        parallel_mode=ParallelMode.EXCLUSIVE,
        risk_level=RiskLevel.HIGH,
        related_worktree=worktree_name,
    )
    approval = await store.add_approval(
        session_id=session_id,
        mission_id=mission_id,
        task_id=task_id,
        state=ApprovalState.WAITING,
        title="Approve me",
        detail="Please",
        requester="Agent-A",
    )
    return approval.id


@pytest.mark.asyncio
async def test_collect_returns_none_for_missing_approval(tmp_path) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    collector = ReviewEvidenceCollector(
        store=store, task_store=None, worktree_storage_dir=tmp_path
    )
    result = await collector.collect(session_id="sess", approval_id="missing")
    assert result is None


@pytest.mark.asyncio
async def test_collect_gathers_real_git_diff(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    storage = tmp_path / "worktrees"
    storage.mkdir()
    worktree = storage / "wt-feature"
    _run_git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature")
    # Make a dirty change in the worktree.
    (worktree / "README.md").write_text("# changed\n")
    (worktree / "new_file.txt").write_text("new\n")

    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    approval_id = await _seed_approval_and_issue(store, "sess", "task-1", "wt-feature")

    collector = ReviewEvidenceCollector(
        store=store, task_store=None, worktree_storage_dir=storage
    )
    evidence = await collector.collect(session_id="sess", approval_id=approval_id)
    assert evidence is not None
    assert evidence["approval"]["id"] == approval_id
    assert evidence["worktree"]["name"] == "wt-feature"
    assert evidence["worktree"]["status"] == "present"
    changed_paths = {f["path"] for f in evidence["changed_files"]}
    assert "README.md" in changed_paths
    assert "new_file.txt" in changed_paths
    # Diff hunks exist for the tracked modified file.
    assert any(h["path"] == "README.md" for h in evidence["diff_hunks"])
    assert evidence["issue"] is not None
    assert evidence["issue"]["related_worktree"] == "wt-feature"


@pytest.mark.asyncio
async def test_collect_marks_missing_worktree_when_path_absent(tmp_path) -> None:
    storage = tmp_path / "worktrees"
    storage.mkdir()
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    approval_id = await _seed_approval_and_issue(store, "sess", "task-1", "wt-gone")

    collector = ReviewEvidenceCollector(
        store=store, task_store=None, worktree_storage_dir=storage
    )
    evidence = await collector.collect(session_id="sess", approval_id=approval_id)
    assert evidence is not None
    assert evidence["worktree"]["status"] == "missing"
    assert evidence["changed_files"] == []
    assert evidence["diff_hunks"] == []


def test_git_status_label_maps_known_codes() -> None:
    assert _git_status_label("??") == "untracked"
    assert _git_status_label("A ") == "added"
    assert _git_status_label("D ") == "deleted"
    assert _git_status_label("M ") == "modified"
    assert _git_status_label("R ") == "renamed"
    assert _git_status_label("  ") == "modified"


def test_parse_diff_hunks_splits_by_file_and_caps_patch() -> None:
    diff = (
        "diff --git a/one.py b/one.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/two.py b/two.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    hunks = _parse_diff_hunks(diff)
    assert [h["path"] for h in hunks] == ["one.py", "two.py"]
    assert "+new" in hunks[0]["patch"]
    assert "+y" in hunks[1]["patch"]


def test_parse_diff_hunks_empty_for_no_diff() -> None:
    assert _parse_diff_hunks("") == []
