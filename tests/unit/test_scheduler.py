"""Scheduler subsystem tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig, SafetyConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.scheduler.cron import CronSchedule
from naumi_agent.scheduler.models import ScheduleStatus
from naumi_agent.scheduler.runner import SchedulerRunner
from naumi_agent.scheduler.store import SchedulerStore
from naumi_agent.scheduler.tools import create_scheduler_tools


@pytest.fixture
def fixed_now() -> list[datetime]:
    return [datetime(2030, 1, 1, 8, 0, tzinfo=UTC)]


@pytest.fixture
def runner(tmp_path: Path, fixed_now: list[datetime]) -> SchedulerRunner:
    return SchedulerRunner(
        SchedulerStore(tmp_path / "scheduler"),
        now_fn=lambda: fixed_now[0],
        poll_seconds=0.01,
    )


class TestCronSchedule:
    def test_parse_step_range_and_weekday_alias(self) -> None:
        cron = CronSchedule.parse("*/15 9-17 * * 1,3,7")

        assert cron.minutes == {0, 15, 30, 45}
        assert 9 in cron.hours
        assert 18 not in cron.hours
        assert cron.weekdays == {0, 1, 3}

    def test_next_after_is_strictly_future(self) -> None:
        cron = CronSchedule.parse("*/30 * * * *")
        after = datetime(2030, 1, 1, 8, 30, tzinfo=UTC)

        assert cron.next_after(after) == datetime(2030, 1, 1, 9, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        "expression",
        ["* * * *", "*/0 * * * *", "61 * * * *", "10-1 * * * *"],
    )
    def test_invalid_cron_expression_raises_clear_error(self, expression: str) -> None:
        with pytest.raises(ValueError):
            CronSchedule.parse(expression)


class TestSchedulerRunner:
    def test_once_schedule_fires_once_and_persists_event(
        self,
        runner: SchedulerRunner,
        fixed_now: list[datetime],
        tmp_path: Path,
    ) -> None:
        job = runner.create(
            kind="once",
            expression="2030-01-01T08:05:00+00:00",
            prompt="检查测试结果",
        )
        assert job.id == "sch_0001"
        assert job.status == ScheduleStatus.ACTIVE

        fixed_now[0] = datetime(2030, 1, 1, 8, 6, tzinfo=UTC)
        notifications = runner.collect_notifications()
        second = runner.collect_notifications()

        assert len(notifications) == 1
        assert "schedule_notification" in notifications[0]
        assert "检查测试结果" in notifications[0]
        assert second == []

        restored = SchedulerRunner(SchedulerStore(tmp_path / "scheduler"))
        stored = restored.get(job.id)
        assert stored is not None
        assert stored.status == ScheduleStatus.COMPLETED
        assert stored.fired_count == 1

    def test_cron_schedule_computes_next_fire(
        self,
        runner: SchedulerRunner,
        fixed_now: list[datetime],
    ) -> None:
        job = runner.create(kind="cron", expression="*/10 * * * *", prompt="轮询自检")
        assert job.next_fire_at == "2030-01-01T08:10:00+00:00"

        fixed_now[0] = datetime(2030, 1, 1, 8, 11, tzinfo=UTC)
        assert runner.tick_due() == 1
        updated = runner.get(job.id)

        assert updated is not None
        assert updated.status == ScheduleStatus.ACTIVE
        assert updated.fired_count == 1
        assert updated.next_fire_at == "2030-01-01T08:20:00+00:00"

    def test_overdue_cron_coalesces_missed_runs(
        self,
        runner: SchedulerRunner,
        fixed_now: list[datetime],
    ) -> None:
        job = runner.create(kind="cron", expression="*/10 * * * *", prompt="避免刷屏")

        fixed_now[0] = datetime(2030, 1, 1, 9, 41, tzinfo=UTC)
        assert runner.tick_due() == 1
        updated = runner.get(job.id)

        assert updated is not None
        assert updated.fired_count == 1
        assert updated.next_fire_at == "2030-01-01T09:50:00+00:00"

    def test_pause_resume_and_cancel(self, runner: SchedulerRunner) -> None:
        job = runner.create(kind="cron", expression="*/5 * * * *", prompt="保持节奏")

        paused = runner.pause(job.id)
        assert paused is not None
        assert paused.status == ScheduleStatus.PAUSED

        resumed = runner.resume(job.id)
        assert resumed is not None
        assert resumed.status == ScheduleStatus.ACTIVE

        cancelled = runner.cancel(job.id)
        assert cancelled is not None
        assert cancelled.status == ScheduleStatus.CANCELLED
        assert cancelled.next_fire_at == ""

    def test_invalid_inputs_are_rejected(self, runner: SchedulerRunner) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            runner.create(kind="once", expression="2030-01-01T08:05:00+00:00", prompt="")
        with pytest.raises(ValueError, match="不能早于"):
            runner.create(kind="once", expression="2029-01-01T08:05:00+00:00", prompt="过期")
        with pytest.raises(ValueError, match="必须是 once 或 cron"):
            runner.create(kind="later", expression="*", prompt="坏类型")
        with pytest.raises(ValueError, match="只支持 session_message"):
            runner.create(
                kind="cron",
                expression="* * * * *",
                prompt="坏目标",
                target="task",
            )


class TestSchedulerTools:
    @pytest.mark.asyncio
    async def test_create_tools_expose_expected_names(self, runner: SchedulerRunner) -> None:
        assert {tool.name for tool in create_scheduler_tools(runner)} == {
            "schedule_create",
            "schedule_list",
            "schedule_cancel",
            "schedule_pause",
            "schedule_resume",
        }

    @pytest.mark.asyncio
    async def test_tool_lifecycle(self, runner: SchedulerRunner) -> None:
        tools = {tool.name: tool for tool in create_scheduler_tools(runner)}

        created = await tools["schedule_create"].execute(
            kind="cron",
            expression="*/15 * * * *",
            prompt="工具链路验证",
        )
        listed = await tools["schedule_list"].execute()
        paused = await tools["schedule_pause"].execute(schedule_id="sch_0001")
        resumed = await tools["schedule_resume"].execute(schedule_id="sch_0001")
        cancelled = await tools["schedule_cancel"].execute(schedule_id="sch_0001")

        assert "调度任务已创建" in created
        assert "工具链路验证" in listed
        assert "已暂停" in paused
        assert "已恢复" in resumed
        assert "已取消" in cancelled

    @pytest.mark.asyncio
    async def test_engine_registers_scheduler_tools(self, tmp_path: Path) -> None:
        engine = AgentEngine(
            AppConfig(memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))
        )
        try:
            assert {
                "schedule_create",
                "schedule_list",
                "schedule_cancel",
                "schedule_pause",
                "schedule_resume",
            }.issubset(set(engine.tool_registry.names))
        finally:
            await engine.shutdown()

    @pytest.mark.asyncio
    async def test_engine_injects_due_schedule_notifications(self, tmp_path: Path) -> None:
        now = [datetime(2030, 1, 1, 8, 0, tzinfo=UTC)]
        engine = AgentEngine(
            AppConfig(
                memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db")),
                safety=SafetyConfig(permission_mode="bypass"),
            )
        )
        try:
            engine.scheduler_runner._now_fn = lambda: now[0]
            engine.scheduler_runner.create(
                kind="once",
                expression=(now[0] + timedelta(minutes=1)).isoformat(),
                prompt="注入验证",
            )
            now[0] += timedelta(minutes=2)

            engine._inject_scheduler_notifications()

            assert any(
                "schedule_notification" in str(msg.get("content", ""))
                for msg in engine._messages
            )
            assert "注入验证" in str(engine._messages[-1]["content"])
        finally:
            await engine.shutdown()


class TestSchedulerPermissions:
    def test_lockdown_allows_readonly_schedule_list(self) -> None:
        checker = PermissionChecker(PermissionMode.LOCKDOWN)

        assert checker.check("schedule_list", {}).allowed
        assert not checker.check(
            "schedule_create",
            {"kind": "once", "expression": "2030-01-01T08:00:00", "prompt": "x"},
        ).allowed

    def test_strict_allows_schedule_lifecycle(self) -> None:
        checker = PermissionChecker(PermissionMode.STRICT)

        assert checker.check(
            "schedule_create",
            {"kind": "once", "expression": "2030-01-01T08:00:00", "prompt": "x"},
        ).allowed
        assert checker.check("schedule_pause", {"schedule_id": "sch_0001"}).allowed
        assert checker.check("schedule_resume", {"schedule_id": "sch_0001"}).allowed
        assert checker.check("schedule_cancel", {"schedule_id": "sch_0001"}).allowed
