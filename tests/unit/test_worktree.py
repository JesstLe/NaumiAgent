"""Worktree isolation tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tasks.store import TaskStore
from naumi_agent.worktree.manager import WorktreeManager
from naumi_agent.worktree.models import WorktreeStatus
from naumi_agent.worktree.tools import create_worktree_tools


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "naumi@example.test"], repo)
    _run(["git", "config", "user.name", "Naumi Test"], repo)
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _run(["git", "add", "README.md"], repo)
    _run(["git", "commit", "-m", "initial"], repo)
    return repo


@pytest.fixture
def manager(git_repo: Path, tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(
        repo_root=git_repo,
        storage_dir=tmp_path / "worktrees",
    )


class TestWorktreeManager:
    def test_validate_name_rejects_unsafe_values(self, manager: WorktreeManager) -> None:
        assert manager.validate_name("") is not None
        assert manager.validate_name("../escape") is not None
        assert manager.validate_name("bad/name") is not None
        assert manager.validate_name("ok-name_1.2") is None

    @pytest.mark.asyncio
    async def test_create_status_and_remove_clean_worktree(
        self,
        manager: WorktreeManager,
    ) -> None:
        created = await manager.create("feature-a")
        assert "已创建隔离 worktree" in created

        status = await manager.status("feature-a")
        assert not isinstance(status, list)
        assert status.status == WorktreeStatus.CLEAN
        assert Path(status.path).is_dir()
        assert status.removable

        removed = await manager.remove("feature-a")
        assert "已删除 worktree" in removed
        assert not Path(status.path).exists()

    @pytest.mark.asyncio
    async def test_remove_refuses_dirty_worktree(self, manager: WorktreeManager) -> None:
        await manager.create("dirty-case")
        status = await manager.status("dirty-case")
        assert not isinstance(status, list)
        Path(status.path, "new.txt").write_text("changed\n", encoding="utf-8")

        refused = await manager.remove("dirty-case")
        assert "拒绝删除" in refused
        assert Path(status.path).exists()

        kept = await manager.keep("dirty-case", reason="等待人工审查")
        assert "已保留" in kept
        assert "未提交文件数：1" in kept
        kept_status = await manager.status("dirty-case")
        assert not isinstance(kept_status, list)
        assert kept_status.status == WorktreeStatus.KEPT
        assert kept_status.dirty_files == 1

        forced = await manager.remove("dirty-case", discard_changes=True)
        assert "已删除 worktree" in forced

    @pytest.mark.asyncio
    async def test_bind_task_requires_existing_session_task(
        self,
        git_repo: Path,
        tmp_path: Path,
    ) -> None:
        store = TaskStore(str(tmp_path / "tasks.db"))
        store.set_session("session-1")
        task = await store.create_task(subject="隔离实现")
        manager = WorktreeManager(
            repo_root=git_repo,
            storage_dir=tmp_path / "worktrees",
            task_store=store,
        )

        created = await manager.create("task-bound", task_id=task.id)
        assert "绑定任务：#1" in created

        missing = await manager.bind_task("task-bound", "999")
        assert "不存在" in missing

    @pytest.mark.asyncio
    async def test_create_with_task_without_session_returns_clear_error(
        self,
        git_repo: Path,
        tmp_path: Path,
    ) -> None:
        store = TaskStore(str(tmp_path / "tasks.db"))
        manager = WorktreeManager(
            repo_root=git_repo,
            storage_dir=tmp_path / "worktrees",
            task_store=store,
        )

        result = await manager.create("needs-session", task_id="1")
        assert "当前没有活动会话" in result


class TestWorktreeTools:
    @pytest.mark.asyncio
    async def test_engine_registers_worktree_tools(self, tmp_path: Path) -> None:
        from naumi_agent.config.settings import AppConfig, MemoryConfig
        from naumi_agent.orchestrator.engine import AgentEngine

        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        try:
            names = set(engine.tool_registry.names)
            assert {
                "worktree_create",
                "worktree_status",
                "worktree_bind_task",
                "worktree_keep",
                "worktree_remove",
            }.issubset(names)
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_create_tools_expose_expected_names(self, manager: WorktreeManager) -> None:
        tools = create_worktree_tools(manager)
        assert {tool.name for tool in tools} == {
            "worktree_create",
            "worktree_status",
            "worktree_bind_task",
            "worktree_keep",
            "worktree_remove",
        }

    @pytest.mark.asyncio
    async def test_status_tool_handles_missing_worktree(self, manager: WorktreeManager) -> None:
        tool = {tool.name: tool for tool in create_worktree_tools(manager)}["worktree_status"]
        result = await tool.execute(name="missing")
        assert "不存在" in result


class TestWorktreePermissions:
    def test_lockdown_allows_status_only(self) -> None:
        checker = PermissionChecker(PermissionMode.LOCKDOWN)
        assert checker.check("worktree_status", {}).allowed
        assert not checker.check("worktree_create", {"name": "x"}).allowed

    def test_moderate_allows_worktree_lifecycle(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        assert checker.check("worktree_create", {"name": "x"}).allowed
        assert checker.check("worktree_bind_task", {"name": "x", "task_id": "1"}).allowed
        assert checker.check("worktree_keep", {"name": "x"}).allowed
        assert checker.check("worktree_remove", {"name": "x"}).allowed
