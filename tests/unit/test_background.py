"""Background task subsystem tests."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from naumi_agent.background import (
    BackgroundRunner,
    BackgroundStatus,
    BackgroundTask,
    BackgroundTaskStore,
)
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
    async def test_idempotency_key_reuses_one_task_across_concurrent_runners(
        self,
        tmp_path: Path,
    ) -> None:
        base_dir = tmp_path / "background"
        first_runner = BackgroundRunner(BackgroundTaskStore(base_dir))
        second_runner = BackgroundRunner(BackgroundTaskStore(base_dir))
        command = f'{sys.executable} -c "import time; time.sleep(0.3)"'

        first, second = await asyncio.gather(
            first_runner.run(command, idempotency_key="pact_concurrent-1"),
            second_runner.run(command, idempotency_key="pact_concurrent-1"),
        )

        assert first.id == second.id == "bg_0001"
        assert len(first_runner.list_tasks()) == 1
        assert (
            len(first_runner._processes) + len(second_runner._processes)
        ) == 1
        owner = first_runner if first.id in first_runner._processes else second_runner
        await _wait_for_finished(owner, first.id)
        await first_runner.shutdown()
        await second_runner.shutdown()

    @pytest.mark.asyncio
    async def test_idempotency_key_survives_store_and_runner_restart(
        self,
        tmp_path: Path,
    ) -> None:
        base_dir = tmp_path / "background"
        first_runner = BackgroundRunner(BackgroundTaskStore(base_dir))
        command = f'{sys.executable} -c "print(\'once\')"'
        first = await first_runner.run(
            command,
            idempotency_key="pact_restart-1",
        )
        await _wait_for_finished(first_runner, first.id)

        reopened = BackgroundRunner(BackgroundTaskStore(base_dir))
        replay = await reopened.run(
            command,
            idempotency_key="pact_restart-1",
        )

        assert replay.id == first.id
        assert replay.status is BackgroundStatus.COMPLETED
        assert reopened._processes == {}
        assert len(reopened.list_tasks()) == 1
        await first_runner.shutdown()
        await reopened.shutdown()

    @pytest.mark.asyncio
    async def test_replay_returns_receipt_when_original_cwd_no_longer_exists(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "ephemeral-workspace"
        workspace.mkdir()
        runner = BackgroundRunner(BackgroundTaskStore(tmp_path / "background"))
        command = f'{sys.executable} -c "print(\'done\')"'
        first = await runner.run(
            command,
            cwd=str(workspace),
            idempotency_key="pact_missing-cwd-1",
        )
        await _wait_for_finished(runner, first.id)
        workspace.rmdir()

        replay = await runner.run(
            command,
            cwd=str(workspace),
            idempotency_key="pact_missing-cwd-1",
        )

        assert replay.id == first.id
        assert replay.status is BackgroundStatus.COMPLETED
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_idempotency_key_rejects_parameter_conflict(
        self,
        runner: BackgroundRunner,
    ) -> None:
        first = await runner.run(
            f'{sys.executable} -c "import time; time.sleep(1)"',
            idempotency_key="pact_conflict-1",
        )
        with pytest.raises(ValueError, match="幂等键已绑定不同"):
            await runner.run(
                f'{sys.executable} -c "print(2)"',
                idempotency_key="pact_conflict-1",
            )
        await runner.cancel(first.id)
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_failed_spawn_keeps_durable_reservation_and_is_not_retried(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spawn = AsyncMock(side_effect=OSError("spawn unavailable"))
        monkeypatch.setattr(
            "naumi_agent.background.runner.create_shell_process",
            spawn,
        )
        runner = BackgroundRunner(BackgroundTaskStore(tmp_path / "background"))

        with pytest.raises(OSError, match="spawn unavailable"):
            await runner.run("echo once", idempotency_key="pact_spawn-1")
        reserved = runner.store.get_by_idempotency_key("pact_spawn-1")
        replay = await runner.run("echo once", idempotency_key="pact_spawn-1")

        assert reserved is not None
        assert reserved.status is BackgroundStatus.FAILED
        assert "OSError" in reserved.error
        assert replay.id == reserved.id
        assert replay.status is BackgroundStatus.FAILED
        assert spawn.await_count == 1

    @pytest.mark.asyncio
    async def test_idempotency_key_validation_is_bounded(
        self,
        runner: BackgroundRunner,
    ) -> None:
        with pytest.raises(ValueError, match="幂等键必须"):
            await runner.run("echo bad", idempotency_key="bad key")
        with pytest.raises(ValueError, match="幂等键必须"):
            await runner.run("echo long", idempotency_key="a" * 129)

    def test_legacy_task_json_loads_new_idempotency_defaults(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "background"
        base_dir.mkdir()
        (base_dir / "tasks.json").write_text(
            json.dumps({
                "bg_0001": {
                    "id": "bg_0001",
                    "command": "echo legacy",
                    "cwd": str(tmp_path),
                    "status": "completed",
                    "output_path": str(base_dir / "artifacts" / "bg_0001.log"),
                }
            }),
            encoding="utf-8",
        )

        restored = BackgroundTaskStore(base_dir).get("bg_0001")

        assert restored is not None
        assert restored.idempotency_key == ""
        assert restored.timeout_seconds == 1800

    def test_reserved_task_idempotent_identity_is_immutable(self, tmp_path: Path) -> None:
        store = BackgroundTaskStore(tmp_path / "background")
        task, created = store.reserve(
            command="echo immutable",
            cwd=str(tmp_path),
            idempotency_key="pact_immutable-1",
            timeout_seconds=30,
            port_hints=[],
            started_at="2026-07-18T12:00:00",
        )
        task.command = "echo changed"

        assert created is True
        with pytest.raises(ValueError, match="幂等身份不可修改"):
            store.save(task)

    def test_corrupted_store_fails_closed_without_overwriting_bytes(
        self,
        tmp_path: Path,
    ) -> None:
        base_dir = tmp_path / "background"
        base_dir.mkdir()
        records_path = base_dir / "tasks.json"
        original = b'{"bg_0001": broken'
        records_path.write_bytes(original)
        store = BackgroundTaskStore(base_dir)

        assert store.list_tasks() == []
        with pytest.raises(ValueError, match="记录损坏"):
            store.reserve(
                command="echo must-not-run",
                cwd=str(tmp_path),
                idempotency_key="pact_corrupt-1",
                timeout_seconds=30,
                port_hints=[],
                started_at="2026-07-18T12:00:00",
            )
        assert records_path.read_bytes() == original


class TestBackgroundRetention:
    def _task(
        self,
        store: BackgroundTaskStore,
        task_id: str,
        *,
        status: BackgroundStatus = BackgroundStatus.COMPLETED,
        completed_at: str,
        output_path: Path | None = None,
    ) -> BackgroundTask:
        path = output_path or store.artifacts_dir / f"{task_id}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task_id, encoding="utf-8")
        task = BackgroundTask(
            id=task_id,
            command="echo done",
            cwd=str(store.base_dir),
            status=status,
            output_path=str(path),
            started_at=completed_at,
            completed_at=completed_at if status != BackgroundStatus.RUNNING else "",
            notified=True,
        )
        store.save(task)
        return task

    def test_prune_removes_expired_terminal_record_and_artifact(self, tmp_path: Path) -> None:
        store = BackgroundTaskStore(tmp_path / "background")
        now = datetime(2026, 7, 13, 12, 0, 0)
        old = self._task(
            store,
            "bg_0001",
            completed_at=(now - timedelta(days=8)).isoformat(),
        )

        result = store.prune(now=now, retention_days=7, max_records=100)

        assert result.records_deleted == 1
        assert result.artifacts_deleted == 1
        assert store.get(old.id) is None
        assert not Path(old.output_path).exists()

    def test_prune_keeps_only_newest_terminal_records(self, tmp_path: Path) -> None:
        store = BackgroundTaskStore(tmp_path / "background")
        now = datetime(2026, 7, 13, 12, 0, 0)
        for index in range(1, 103):
            self._task(
                store,
                f"bg_{index:04d}",
                completed_at=(now - timedelta(minutes=103 - index)).isoformat(),
            )

        result = store.prune(now=now, retention_days=7, max_records=100)

        assert result.records_deleted == 2
        assert store.get("bg_0001") is None
        assert store.get("bg_0002") is None
        assert len(store.list_tasks()) == 100

    def test_prune_never_removes_running_record(self, tmp_path: Path) -> None:
        store = BackgroundTaskStore(tmp_path / "background")
        now = datetime(2026, 7, 13, 12, 0, 0)
        running = self._task(
            store,
            "bg_0001",
            status=BackgroundStatus.RUNNING,
            completed_at=(now - timedelta(days=30)).isoformat(),
        )

        result = store.prune(now=now, retention_days=7, max_records=0)

        assert result.records_deleted == 0
        assert store.get(running.id) is not None
        assert Path(running.output_path).exists()

    def test_count_prune_keeps_unexpired_idempotency_receipt(self, tmp_path: Path) -> None:
        store = BackgroundTaskStore(tmp_path / "background")
        now = datetime(2026, 7, 13, 12, 0, 0)
        receipt = self._task(
            store,
            "bg_0001",
            completed_at=(now - timedelta(hours=1)).isoformat(),
        )
        receipt.idempotency_key = "pact_retained-1"
        store.save(receipt)

        result = store.prune(now=now, retention_days=7, max_records=0)

        assert result.records_deleted == 0
        assert store.get(receipt.id) is not None

    def test_prune_refuses_external_artifact_and_keeps_record(self, tmp_path: Path) -> None:
        store = BackgroundTaskStore(tmp_path / "background")
        now = datetime(2026, 7, 13, 12, 0, 0)
        external = tmp_path / "do-not-delete.log"
        task = self._task(
            store,
            "bg_0001",
            completed_at=(now - timedelta(days=8)).isoformat(),
            output_path=external,
        )

        result = store.prune(now=now, retention_days=7, max_records=100)

        assert result.records_deleted == 0
        assert result.errors
        assert store.get(task.id) is not None
        assert external.exists()

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

    @pytest.mark.asyncio
    async def test_run_rejects_obviously_busy_port(self, runner: BackgroundRunner) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]

            with pytest.raises(ValueError, match="端口已被占用"):
                await runner.run(f"{sys.executable} -m http.server {port}")

    @pytest.mark.asyncio
    async def test_run_does_not_treat_plain_numbers_as_ports(
        self,
        runner: BackgroundRunner,
    ) -> None:
        task = await runner.run(f"{sys.executable} -c \"print(2026)\"")
        await _wait_for_finished(runner, task.id)

        finished = runner.get(task.id)
        assert finished is not None
        assert finished.port_hints == []
        assert finished.status == BackgroundStatus.COMPLETED
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_cleanup_marks_stale_running_records(
        self,
        tmp_path: Path,
        runner: BackgroundRunner,
    ) -> None:
        output_path = tmp_path / "background" / "artifacts" / "bg_0999.log"
        runner.store.save(
            BackgroundTask(
                id="bg_0999",
                command="python -m http.server 8765",
                cwd=str(tmp_path),
                status=BackgroundStatus.RUNNING,
                output_path=str(output_path),
                pid=None,
                process_group_id=None,
                port_hints=[8765],
                started_at="2026-01-01T00:00:00",
            )
        )

        result = await runner.cleanup()
        stored = runner.get("bg_0999")

        assert "标记 1 个陈旧任务" in result
        assert stored is not None
        assert stored.status == BackgroundStatus.FAILED
        assert "进程已不存在" in stored.error


class TestBackgroundLifecycleRelease:
    @pytest.mark.asyncio
    async def test_finished_process_releases_runtime_maps(
        self,
        runner: BackgroundRunner,
    ) -> None:
        task = await runner.run(f'{sys.executable} -c "print(\"done\")"')

        await _wait_for_finished(runner, task.id)

        assert task.id not in runner._processes
        assert task.id not in runner._watchers
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_notification_moves_terminal_task_from_active_to_history(
        self,
        runner: BackgroundRunner,
    ) -> None:
        task = await runner.run(f'{sys.executable} -c "raise SystemExit(3)"')
        await _wait_for_finished(runner, task.id)

        assert [item.id for item in runner.list_active_tasks()] == [task.id]
        assert runner.list_history() == []

        notifications = runner.collect_notifications()

        assert len(notifications) == 1
        assert runner.list_active_tasks() == []
        assert [item.id for item in runner.list_history()] == [task.id]
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_cleanup_reports_expired_history_pruning(
        self,
        tmp_path: Path,
        runner: BackgroundRunner,
    ) -> None:
        output_path = runner.store.artifacts_dir / "bg_0900.log"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("old", encoding="utf-8")
        runner.store.save(BackgroundTask(
            id="bg_0900",
            command="echo old",
            cwd=str(tmp_path),
            status=BackgroundStatus.COMPLETED,
            output_path=str(output_path),
            started_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:00:01",
            notified=True,
        ))

        result = await runner.cleanup()

        assert "清理历史记录 1 个" in result
        assert "删除日志 1 个" in result
        assert runner.get("bg_0900") is None


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
            "background_cleanup",
        }
        run_tool = next(tool for tool in tools if tool.name == "background_run")
        assert "idempotency_key" in run_tool.parameters_schema["properties"]

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
    async def test_cleanup_tool_reports_cleanup_result(self, runner: BackgroundRunner) -> None:
        tools = {tool.name: tool for tool in create_background_tools(runner)}

        result = await tools["background_cleanup"].execute()

        assert "后台清理完成" in result

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
                "background_cleanup",
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

            events: list[tuple[str, dict[str, object]]] = []

            async def on_event(event: str, data: dict[str, object]) -> None:
                events.append((event, data))

            await engine._inject_background_notifications(on_event)
            assert any(
                "background_task_notification" in str(msg.get("content", ""))
                for msg in engine._messages
            )
            assert "injected" in str(engine._messages[-1]["content"])
            assert events
            event, data = events[-1]
            assert event == "runtime_notification"
            assert data["source"] == "background"
            assert data["title"] == "后台任务通知"
            assert data["count"] == 1
            assert "bg_0001" in str(data["preview"])
            assert "injected" in str(data["content"])
        finally:
            await engine.shutdown()


class TestBackgroundPermissions:
    def test_lockdown_allows_readonly_background_tools(self) -> None:
        checker = PermissionChecker(PermissionMode.LOCKDOWN)
        assert checker.check("background_status", {"task_id": "bg_0001"}).allowed
        assert checker.check("background_list", {}).allowed
        assert checker.check("background_read_output", {"task_id": "bg_0001"}).allowed
        assert not checker.check("background_run", {"command": "echo hi"}).allowed
        assert not checker.check("background_cleanup", {}).allowed

    def test_moderate_background_run_requires_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        decision = checker.check("background_run", {"command": "echo hi"})
        assert decision.allowed
        assert decision.requires_confirmation

    def test_moderate_background_cleanup_does_not_require_confirmation(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        decision = checker.check("background_cleanup", {})
        assert decision.allowed
        assert not decision.requires_confirmation

    def test_background_run_blocks_dangerous_commands(self) -> None:
        checker = PermissionChecker(PermissionMode.MODERATE)
        result = checker.check("background_run", {"command": "rm -rf /"})
        assert not result.allowed
