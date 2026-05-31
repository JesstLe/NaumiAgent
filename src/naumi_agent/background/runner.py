"""Async background command runner."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime
from pathlib import Path

from naumi_agent.background.models import BackgroundStatus, BackgroundTask
from naumi_agent.background.store import BackgroundTaskStore

_PREVIEW_CHARS = 2000


class BackgroundRunner:
    """Launch shell commands in the background and persist their results."""

    def __init__(self, store: BackgroundTaskStore) -> None:
        self._store = store
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}

    @property
    def store(self) -> BackgroundTaskStore:
        return self._store

    async def run(
        self,
        command: str,
        *,
        cwd: str = "",
        timeout_seconds: int = 1800,
    ) -> BackgroundTask:
        """Start a background shell command and return immediately."""
        command = command.strip()
        if not command:
            raise ValueError("后台命令不能为空")
        if timeout_seconds <= 0:
            raise ValueError("超时时间必须大于 0 秒")

        workdir = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        if not workdir.is_dir():
            raise ValueError(f"工作目录不存在：{workdir}")

        task_id = self._store.next_id()
        output_path = self._store.artifacts_dir / f"{task_id}.log"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        now = _now()
        task = BackgroundTask(
            id=task_id,
            command=command,
            cwd=str(workdir),
            status=BackgroundStatus.RUNNING,
            output_path=str(output_path),
            pid=proc.pid,
            started_at=now,
        )
        self._processes[task_id] = proc
        self._store.save(task)
        self._watchers[task_id] = asyncio.create_task(
            self._watch(task_id, proc, output_path, timeout_seconds)
        )
        return task

    async def cancel(self, task_id: str) -> BackgroundTask | None:
        """Cancel a running task."""
        task = self._store.get(task_id)
        if task is None:
            return None
        if task.is_finished:
            return task

        proc = self._processes.get(task_id)
        if proc is not None and proc.returncode is None:
            await _terminate_process(proc)

        task.status = BackgroundStatus.CANCELLED
        task.completed_at = _now()
        task.error = "用户取消了后台任务"
        self._store.save(task)
        return task

    def list_tasks(self) -> list[BackgroundTask]:
        return self._store.list_tasks()

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._store.get(task_id)

    def read_output(self, task_id: str, max_chars: int = 20000) -> str:
        return self._store.read_output(task_id, max_chars=max_chars)

    def collect_notifications(self, limit: int = 5) -> list[str]:
        """Return newly finished task notifications and mark them delivered."""
        notifications: list[str] = []
        for task in self._store.list_tasks():
            if len(notifications) >= limit:
                break
            if not task.is_finished or task.notified:
                continue
            notifications.append(format_notification(task))
            self._store.mark_notified(task.id)
        return notifications

    async def shutdown(self) -> None:
        """Stop all running background commands."""
        for task_id in list(self._processes):
            await self.cancel(task_id)
        for watcher in list(self._watchers.values()):
            if not watcher.done():
                watcher.cancel()
        if self._watchers:
            await asyncio.gather(*list(self._watchers.values()), return_exceptions=True)

    async def _watch(
        self,
        task_id: str,
        proc: asyncio.subprocess.Process,
        output_path: Path,
        timeout_seconds: int,
    ) -> None:
        try:
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
                output = (stdout or b"").decode("utf-8", errors="replace")
                status = (
                    BackgroundStatus.COMPLETED
                    if proc.returncode == 0
                    else BackgroundStatus.FAILED
                )
                error = "" if proc.returncode == 0 else f"进程退出码：{proc.returncode}"
            except TimeoutError:
                await _terminate_process(proc)
                output = ""
                status = BackgroundStatus.TIMED_OUT
                error = f"后台任务超过 {timeout_seconds} 秒未完成，已终止"

            output_path.write_text(output, encoding="utf-8")
            task = self._store.get(task_id)
            if task is None or task.status == BackgroundStatus.CANCELLED:
                return
            task.status = status
            task.exit_code = proc.returncode
            task.completed_at = _now()
            task.output_preview = _preview(output)
            task.error = error
            self._store.save(task)
        finally:
            self._processes.pop(task_id, None)
            self._watchers.pop(task_id, None)


def format_task(task: BackgroundTask) -> str:
    """Format one task for user-facing output."""
    exit_part = "" if task.exit_code is None else f"\n- 退出码：{task.exit_code}"
    error_part = f"\n- 错误：{task.error}" if task.error else ""
    completed = f"\n- 完成时间：{task.completed_at}" if task.completed_at else ""
    preview = f"\n\n输出预览：\n```text\n{task.output_preview}\n```" if task.output_preview else ""
    return (
        f"### 后台任务 {task.id}\n"
        f"- 状态：{_status_label(task.status)}\n"
        f"- 命令：`{task.command}`\n"
        f"- 工作目录：`{task.cwd}`\n"
        f"- PID：{task.pid or '-'}\n"
        f"- 输出文件：`{task.output_path}`\n"
        f"- 开始时间：{task.started_at}"
        f"{completed}{exit_part}{error_part}{preview}"
    )


def format_task_list(tasks: list[BackgroundTask]) -> str:
    if not tasks:
        return "当前没有后台任务。"
    return "\n\n".join(format_task(task) for task in tasks)


def format_notification(task: BackgroundTask) -> str:
    preview = task.output_preview or "（无输出）"
    return (
        "<background_task_notification>\n"
        f"任务ID：{task.id}\n"
        f"状态：{_status_label(task.status)}\n"
        f"命令：{task.command}\n"
        f"退出码：{task.exit_code}\n"
        f"输出文件：{task.output_path}\n"
        f"输出预览：\n{preview}\n"
        "</background_task_notification>"
    )


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if proc.pid is not None:
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        proc.terminate()

    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except TimeoutError:
        try:
            if proc.pid is not None:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


def _preview(output: str) -> str:
    if len(output) <= _PREVIEW_CHARS:
        return output
    return output[:_PREVIEW_CHARS] + "\n...（输出已截断，完整内容见输出文件）"


def _status_label(status: BackgroundStatus) -> str:
    return {
        BackgroundStatus.RUNNING: "运行中",
        BackgroundStatus.COMPLETED: "已完成",
        BackgroundStatus.FAILED: "失败",
        BackgroundStatus.CANCELLED: "已取消",
        BackgroundStatus.TIMED_OUT: "已超时",
    }[status]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
