"""Background task tools exposed to the agent."""

from __future__ import annotations

from typing import Any

from naumi_agent.background.runner import BackgroundRunner, format_task, format_task_list
from naumi_agent.tools.base import Tool


def create_background_tools(runner: BackgroundRunner) -> list[Tool]:
    return [
        BackgroundRunTool(runner),
        BackgroundStatusTool(runner),
        BackgroundListTool(runner),
        BackgroundCancelTool(runner),
        BackgroundReadOutputTool(runner),
    ]


class BackgroundRunTool(Tool):
    def __init__(self, runner: BackgroundRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "background_run"

    @property
    def description(self) -> str:
        return "在后台运行一个耗时 shell 命令，立即返回任务 ID，完成后可查询输出。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要后台执行的 shell 命令"},
                "cwd": {"type": "string", "description": "可选工作目录", "default": ""},
                "timeout_seconds": {
                    "type": "integer",
                    "description": "超时时间，默认 1800 秒",
                    "default": 1800,
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        *,
        command: str,
        cwd: str = "",
        timeout_seconds: int = 1800,
        **kwargs: Any,
    ) -> str:
        try:
            task = await self._runner.run(
                command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )
        except ValueError as e:
            return f"错误：{e}"
        return (
            "后台任务已启动。\n\n"
            f"- 任务 ID：`{task.id}`\n"
            f"- 状态：运行中\n"
            f"- PID：{task.pid}\n"
            f"- 输出文件：`{task.output_path}`\n\n"
            "可使用 `background_status` 查询状态，或 `background_read_output` 读取完整输出。"
        )


class BackgroundStatusTool(Tool):
    def __init__(self, runner: BackgroundRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "background_status"

    @property
    def description(self) -> str:
        return "查看指定后台任务的状态、退出码和输出预览。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "后台任务 ID"}},
            "required": ["task_id"],
        }

    async def execute(self, *, task_id: str, **kwargs: Any) -> str:
        task = self._runner.get(task_id)
        if task is None:
            return f"错误：后台任务不存在：{task_id}"
        return format_task(task)


class BackgroundListTool(Tool):
    def __init__(self, runner: BackgroundRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "background_list"

    @property
    def description(self) -> str:
        return "列出所有后台任务。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return format_task_list(self._runner.list_tasks())


class BackgroundCancelTool(Tool):
    def __init__(self, runner: BackgroundRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "background_cancel"

    @property
    def description(self) -> str:
        return "取消一个运行中的后台任务。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "后台任务 ID"}},
            "required": ["task_id"],
        }

    async def execute(self, *, task_id: str, **kwargs: Any) -> str:
        task = await self._runner.cancel(task_id)
        if task is None:
            return f"错误：后台任务不存在：{task_id}"
        return "后台任务已取消。\n\n" + format_task(task)


class BackgroundReadOutputTool(Tool):
    def __init__(self, runner: BackgroundRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "background_read_output"

    @property
    def description(self) -> str:
        return "读取后台任务的完整输出；长输出会被截断并提示输出文件路径。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "后台任务 ID"},
                "max_chars": {
                    "type": "integer",
                    "description": "最多返回字符数，默认 20000",
                    "default": 20000,
                },
            },
            "required": ["task_id"],
        }

    async def execute(
        self,
        *,
        task_id: str,
        max_chars: int = 20000,
        **kwargs: Any,
    ) -> str:
        return self._runner.read_output(task_id, max_chars=max_chars)
