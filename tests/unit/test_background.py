"""Background task subsystem tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from naumi_agent.background import BackgroundRunner, BackgroundStatus, BackgroundTaskStore
from naumi_agent.background.tools import create_background_tools
from naumi_agent.config.settings import AppConfig, MemoryConfig, SafetyConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode


@pytest.fixture
def runner(tmp_path: Path) -> BackgroundRunner:
    return BackgroundRunner(BackgroundTaskStore(tmp_path / "background"))


async def _wait_for_finished(runner: BackgroundRunner, task_id: str) -> None:
    for _ in range(50):
        task = runner.get(task_id)
        if task is not None and task.is_finished:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"background task did not finish: {task_id}")


class TestBackgroundRunner:
    @pytest.mark.asyncio
    async def test_run_returns_immediately_and_persists_output(
        self,
        runner: BackgroundRunner,
    ) -> None:
        command = (
            f"{sys.executable} -c \"import time; "
            "time.sleep(0.2); print('background done')\""
        )
        task = await runner.run(command)
        assert task.id == "bg_0001"
        assert task.status == BackgroundStatus.RUNNING

        await _wait_for_finished(runner, task.id)
        finished = runner.get(task.id)
        assert finished is not None
        assert finished.status == BackgroundStatus.COMPLETED
        assert finished.exit_code == 0
        assert "background done" in runner.read_output(task.id)
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_failed_command_records_exit_code(self, runner: BackgroundRunner) -> None:
        task = await runner.run(f"{sys.executable} -c \"raise SystemExit(7)\"")
        await _wait_for_finished(runner, task.id)

        finished = runner.get(task.id)
        assert finished is not None
        assert finished.status == BackgroundStatus.FAILED
        assert finished.exit_code == 7
        assert "进程退出码" in finished.error
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_cancel_running_task(self, runner: BackgroundRunner) -> None:
        task = await runner.run(f"{sys.executable} -c \"import time; time.sleep(10)\"")
        cancelled = await runner.cancel(task.id)

        assert cancelled is not None
        assert cancelled.status == BackgroundStatus.CANCELLED
        stored = runner.get(task.id)
        assert stored is not None
        assert stored.status == BackgroundStatus.CANCELLED
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_collect_notifications_once(self, runner: BackgroundRunner) -> None:
        task = await runner.run(f"{sys.executable} -c \"print('notify me')\"")
        await _wait_for_finished(runner, task.id)

        first = runner.collect_notifications()
        second = runner.collect_notifications()

        assert len(first) == 1
        assert "notify me" in first[0]
        assert second == []
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_invalid_inputs_return_clear_errors(self, runner: BackgroundRunner) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            await runner.run("")
        with pytest.raises(ValueError, match="工作目录不存在"):
            await runner.run("echo hi", cwd="/definitely/not/a/real/dir")


class TestBackgroundTools:
    @pytest.mark.asyncio
    async def test_create_tools_expose_expected_names(self, runner: BackgroundRunner) -> None:
        tools = create_background_tools(runner)
        assert {tool.name for tool in tools} == {
            "background_run",
            "background_status",
            "background_list",
            "background_cancel",
            "background_read_output",
        }

    @pytest.mark.asyncio
    async def test_run_status_and_output_tools(self, runner: BackgroundRunner) -> None:
        tools = {tool.name: tool for tool in create_background_tools(runner)}
        command = f"{sys.executable} -c \"print('tool output')\""
        started = await tools["background_run"].execute(command=command)
        assert "bg_0001" in started

        await _wait_for_finished(runner, "bg_0001")
        status = await tools["background_status"].execute(task_id="bg_0001")
        output = await tools["background_read_output"].execute(task_id="bg_0001")

        assert "已完成" in status
        assert "tool output" in output
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_engine_registers_background_tools(self, tmp_path: Path) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        try:
            names = set(engine.tool_registry.names)
            assert {
                "background_run",
                "background_status",
                "background_list",
                "background_cancel",
                "background_read_output",
            }.issubset(names)
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_engine_injects_finished_notifications(self, tmp_path: Path) -> None:
        engine = AgentEngine(
            AppConfig(
                memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
                safety=SafetyConfig(permission_mode="bypass"),
            )
        )
        try:
            task = await engine.background_runner.run(f"{sys.executable} -c \"print('injected')\"")
            await _wait_for_finished(engine.background_runner, task.id)

            engine._inject_background_notifications()
            assert any(
                "background_task_notification" in str(msg.get("content", ""))
                for msg in engine._messages
            )
            assert "injected" in str(engine._messages[-1]["content"])
        finally:
            await engine.shutdown()


class TestBackgroundPermissions:
    def test_lockdown_allows_readonly_background_tools(self) -> None:
        checker = PermissionChecker(PermissionMode.LOCKDOWN)
        assert checker.check("background_status", {"task_id": "bg_0001"}).allowed
        assert checker.check("background_list", {}).allowed
        assert checker.check("background_read_output", {"task_id": "bg_0001"}).allowed
        assert not checker.check("background_run", {"command": "echo hi"}).allowed

    def test_moderate_background_run_requires_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        decision = checker.check("background_run", {"command": "echo hi"})
        assert decision.allowed
        assert decision.requires_confirmation

    def test_background_run_blocks_dangerous_commands(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        result = checker.check("background_run", {"command": "rm -rf /"})
        assert not result.allowed
