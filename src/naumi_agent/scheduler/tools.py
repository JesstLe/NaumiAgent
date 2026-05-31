"""Scheduler tools exposed to the agent."""

from __future__ import annotations

from typing import Any

from naumi_agent.scheduler.runner import SchedulerRunner, format_job, format_job_list
from naumi_agent.tools.base import Tool


def create_scheduler_tools(runner: SchedulerRunner) -> list[Tool]:
    return [
        ScheduleCreateTool(runner),
        ScheduleListTool(runner),
        ScheduleCancelTool(runner),
        SchedulePauseTool(runner),
        ScheduleResumeTool(runner),
    ]


class ScheduleCreateTool(Tool):
    def __init__(self, runner: SchedulerRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "schedule_create"

    @property
    def description(self) -> str:
        return "创建一次性提醒或 5 段 cron 周期提醒，触发后注入当前会话上下文。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["once", "cron"],
                    "description": "调度类型：once 或 cron",
                },
                "expression": {
                    "type": "string",
                    "description": "once 使用 ISO 时间；cron 使用 5 段表达式",
                },
                "prompt": {"type": "string", "description": "触发时投递的提醒内容"},
                "target": {
                    "type": "string",
                    "enum": ["session_message"],
                    "description": "触发目标，默认 session_message",
                    "default": "session_message",
                },
            },
            "required": ["kind", "expression", "prompt"],
        }

    async def execute(
        self,
        *,
        kind: str,
        expression: str,
        prompt: str,
        target: str = "session_message",
        **kwargs: Any,
    ) -> str:
        try:
            job = self._runner.create(
                kind=kind,
                expression=expression,
                prompt=prompt,
                target=target,
            )
        except ValueError as e:
            return f"错误：{e}"
        self._runner.start()
        return "调度任务已创建。\n\n" + format_job(job)


class ScheduleListTool(Tool):
    def __init__(self, runner: SchedulerRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "schedule_list"

    @property
    def description(self) -> str:
        return "列出调度任务，可选择只看启用中的任务。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "active_only": {
                    "type": "boolean",
                    "description": "是否只列出启用中的调度",
                    "default": False,
                }
            },
            "required": [],
        }

    async def execute(self, *, active_only: bool = False, **kwargs: Any) -> str:
        return format_job_list(
            self._runner.list_jobs(include_inactive=not active_only)
        )


class ScheduleCancelTool(Tool):
    def __init__(self, runner: SchedulerRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "schedule_cancel"

    @property
    def description(self) -> str:
        return "取消一个调度任务，取消后不会再触发。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"schedule_id": {"type": "string", "description": "调度 ID"}},
            "required": ["schedule_id"],
        }

    async def execute(self, *, schedule_id: str, **kwargs: Any) -> str:
        job = self._runner.cancel(schedule_id)
        if job is None:
            return f"错误：调度任务不存在：{schedule_id}"
        return "调度任务已取消。\n\n" + format_job(job)


class SchedulePauseTool(Tool):
    def __init__(self, runner: SchedulerRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "schedule_pause"

    @property
    def description(self) -> str:
        return "暂停一个启用中的调度任务。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"schedule_id": {"type": "string", "description": "调度 ID"}},
            "required": ["schedule_id"],
        }

    async def execute(self, *, schedule_id: str, **kwargs: Any) -> str:
        job = self._runner.pause(schedule_id)
        if job is None:
            return f"错误：调度任务不存在：{schedule_id}"
        return "调度任务已暂停。\n\n" + format_job(job)


class ScheduleResumeTool(Tool):
    def __init__(self, runner: SchedulerRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "schedule_resume"

    @property
    def description(self) -> str:
        return "恢复一个已暂停的调度任务。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"schedule_id": {"type": "string", "description": "调度 ID"}},
            "required": ["schedule_id"],
        }

    async def execute(self, *, schedule_id: str, **kwargs: Any) -> str:
        job = self._runner.resume(schedule_id)
        if job is None:
            return f"错误：调度任务不存在：{schedule_id}"
        if job.status.value in {"cancelled", "completed"}:
            return "调度任务不能恢复，因为它已经结束。\n\n" + format_job(job)
        self._runner.start()
        return "调度任务已恢复。\n\n" + format_job(job)
