"""Async background command runner."""

from __future__ import annotations

import asyncio
import os
import re
import socket
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from naumi_agent.background.models import BackgroundStatus, BackgroundTask
from naumi_agent.background.store import BackgroundTaskStore
from naumi_agent.runtime.shell import (
    create_shell_process,
    pid_exists,
    terminate_pid_tree,
    terminate_process_tree,
)

_PREVIEW_CHARS = 2000
_OUTPUT_CHUNK_BYTES = 64 * 1024
_OUTPUT_PREVIEW_BYTES = _PREVIEW_CHARS * 4
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PORT_PATTERNS = (
    re.compile(r"\bhttp\.server\s+(?P<port>\d{2,5})(?:\s|$)"),
    re.compile(r"(?:^|\s)(?:--port|-p|--bind-port)\s+(?P<port>\d{2,5})(?:\s|$)"),
    re.compile(r"(?:^|\s)PORT=(?P<port>\d{2,5})(?:\s|$)"),
    re.compile(r"(?::)(?P<port>\d{2,5})(?:/|\s|$)"),
)


class BackgroundRunner:
    """Launch shell commands in the background and persist their results."""

    def __init__(self, store: BackgroundTaskStore) -> None:
        self._store = store
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._dispatch_lock = asyncio.Lock()
        self._store.prune()

    @property
    def store(self) -> BackgroundTaskStore:
        return self._store

    async def run(
        self,
        command: str,
        *,
        cwd: str = "",
        timeout_seconds: int = 1800,
        idempotency_key: str = "",
    ) -> BackgroundTask:
        """Start a background shell command and return immediately."""
        command = command.strip()
        if not command:
            raise ValueError("后台命令不能为空")
        if timeout_seconds <= 0:
            raise ValueError("超时时间必须大于 0 秒")
        idempotency_key = idempotency_key.strip()
        if idempotency_key and not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            raise ValueError(
                "幂等键必须为 1-128 位字母、数字、点、下划线、冒号或连字符"
            )

        workdir = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()

        async with self._dispatch_lock:
            if idempotency_key:
                existing = self._store.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    _validate_idempotent_replay(
                        existing,
                        command=command,
                        cwd=str(workdir),
                        timeout_seconds=timeout_seconds,
                    )
                    return existing

            if not workdir.is_dir():
                raise ValueError(f"工作目录不存在：{workdir}")

            port_hints = _extract_port_hints(command)
            busy_ports = [port for port in port_hints if _is_port_listening(port)]
            if busy_ports:
                ports = ", ".join(str(port) for port in busy_ports)
                raise ValueError(
                    f"端口已被占用：{ports}。请换端口，"
                    "或先用 /background cleanup 清理遗留服务。"
                )

            now = _now()
            if idempotency_key:
                task, created = self._store.reserve(
                    command=command,
                    cwd=str(workdir),
                    idempotency_key=idempotency_key,
                    timeout_seconds=timeout_seconds,
                    port_hints=port_hints,
                    started_at=now,
                )
                if not created:
                    return task
            else:
                task_id = self._store.next_id()
                task = BackgroundTask(
                    id=task_id,
                    command=command,
                    cwd=str(workdir),
                    status=BackgroundStatus.PREPARING,
                    output_path=str(self._store.artifacts_dir / f"{task_id}.log"),
                    port_hints=port_hints,
                    started_at=now,
                    timeout_seconds=timeout_seconds,
                )

            output_path = Path(task.output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                proc = await create_shell_process(
                    command,
                    cwd=str(workdir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except BaseException as exc:
                if idempotency_key:
                    task.status = BackgroundStatus.FAILED
                    task.completed_at = _now()
                    task.error = f"后台进程启动失败：{type(exc).__name__}"
                    self._store.save(task)
                raise

            task.status = BackgroundStatus.RUNNING
            task.pid = proc.pid
            task.process_group_id = proc.pid
            self._processes[task.id] = proc
            self._store.save(task)
            self._watchers[task.id] = asyncio.create_task(
                self._watch(task.id, proc, output_path, timeout_seconds)
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
            await terminate_process_tree(proc)

        task.status = BackgroundStatus.CANCELLED
        task.completed_at = _now()
        task.error = "用户取消了后台任务"
        self._store.save(task)
        self._store.prune()
        return task

    def list_tasks(self) -> list[BackgroundTask]:
        return self._store.list_tasks()

    def list_active_tasks(self) -> list[BackgroundTask]:
        """Return running tasks and terminal results not yet delivered to the agent."""
        return [
            task
            for task in self._store.list_tasks()
            if not task.is_finished or not task.notified
        ]

    def list_history(self) -> list[BackgroundTask]:
        """Return acknowledged terminal task history."""
        return [
            task
            for task in self._store.list_tasks()
            if task.is_finished and task.notified
        ]

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._store.get(task_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> BackgroundTask | None:
        return self._store.get_by_idempotency_key(idempotency_key)

    def is_managed_active(self, task_id: str) -> bool:
        """Return whether this runner owns a live process and its watcher."""
        process = self._processes.get(task_id)
        watcher = self._watchers.get(task_id)
        return (
            process is not None
            and process.returncode is None
            and watcher is not None
            and not watcher.done()
        )

    def read_output(self, task_id: str, max_chars: int = 20000) -> str:
        return self._store.read_output(task_id, max_chars=max_chars)

    async def cleanup(self) -> str:
        """Stop tracked running tasks and mark stale records."""
        cancelled = 0
        stale = 0
        for task in self._store.list_tasks():
            if task.is_finished:
                continue
            proc = self._processes.get(task.id)
            if proc is not None and proc.returncode is None:
                await self.cancel(task.id)
                cancelled += 1
                continue
            if task.pid and pid_exists(task.pid):
                terminate_pid_tree(task.process_group_id or task.pid, force=True)
                task.status = BackgroundStatus.CANCELLED
                task.completed_at = _now()
                task.error = "cleanup 已终止遗留后台进程"
                self._store.save(task)
                cancelled += 1
                continue
            task.status = BackgroundStatus.FAILED
            task.completed_at = task.completed_at or _now()
            task.error = "cleanup 标记：任务记录仍为运行中，但进程已不存在"
            self._store.save(task)
            stale += 1
        pruned = self._store.prune()
        return (
            f"后台清理完成：终止 {cancelled} 个运行任务，标记 {stale} 个陈旧任务，"
            f"清理历史记录 {pruned.records_deleted} 个，删除日志 "
            f"{pruned.artifacts_deleted} 个。"
        )

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
        self._store.prune()
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
        self._store.prune()

    async def _watch(
        self,
        task_id: str,
        proc: asyncio.subprocess.Process,
        output_path: Path,
        timeout_seconds: int,
    ) -> None:
        output_task = asyncio.create_task(
            self._stream_process_output(proc, output_path)
        )
        try:
            try:
                output_preview = await asyncio.wait_for(
                    asyncio.shield(output_task),
                    timeout=timeout_seconds,
                )
                await proc.wait()
                status = (
                    BackgroundStatus.COMPLETED
                    if proc.returncode == 0
                    else BackgroundStatus.FAILED
                )
                error = "" if proc.returncode == 0 else f"进程退出码：{proc.returncode}"
            except TimeoutError:
                await terminate_process_tree(proc)
                try:
                    output_preview = await asyncio.wait_for(
                        asyncio.shield(output_task),
                        timeout=5,
                    )
                except TimeoutError:
                    output_task.cancel()
                    await asyncio.gather(output_task, return_exceptions=True)
                    output_preview = ""
                status = BackgroundStatus.TIMED_OUT
                error = f"后台任务超过 {timeout_seconds} 秒未完成，已终止"
            except Exception as exc:
                if proc.returncode is None:
                    await terminate_process_tree(proc)
                output_preview = ""
                status = BackgroundStatus.FAILED
                error = f"保存后台任务输出失败：{type(exc).__name__}: {exc}"

            task = self._store.get(task_id)
            if task is None or task.status == BackgroundStatus.CANCELLED:
                return
            task.status = status
            task.exit_code = proc.returncode
            task.completed_at = _now()
            task.output_preview = output_preview
            task.error = error
            self._store.save(task)
            self._store.prune()
        finally:
            if not output_task.done():
                output_task.cancel()
                await asyncio.gather(output_task, return_exceptions=True)
            self._processes.pop(task_id, None)
            self._watchers.pop(task_id, None)

    async def _stream_process_output(
        self,
        proc: asyncio.subprocess.Process,
        output_path: Path,
    ) -> str:
        stdout = proc.stdout
        if stdout is None:
            raise RuntimeError("后台进程没有可读取的标准输出管道")
        output_file = await _run_output_io(output_path.open, "wb")
        preview = bytearray()
        truncated = False
        try:
            while chunk := await stdout.read(_OUTPUT_CHUNK_BYTES):
                if len(preview) < _OUTPUT_PREVIEW_BYTES:
                    remaining = _OUTPUT_PREVIEW_BYTES - len(preview)
                    preview.extend(chunk[:remaining])
                    truncated = truncated or len(chunk) > remaining
                else:
                    truncated = True
                await _run_output_io(output_file.write, chunk)
        finally:
            await asyncio.shield(
                _run_output_io(_finalize_output_file, output_file)
            )
        text = bytes(preview).decode("utf-8", errors="replace")
        if truncated or len(text) > _PREVIEW_CHARS:
            return text[:_PREVIEW_CHARS] + (
                "\n...（输出已截断，完整内容见输出文件）"
            )
        return text


def format_task(task: BackgroundTask) -> str:
    """Format one task for user-facing output."""
    exit_part = "" if task.exit_code is None else f"\n- 退出码：{task.exit_code}"
    error_part = f"\n- 错误：{task.error}" if task.error else ""
    completed = f"\n- 完成时间：{task.completed_at}" if task.completed_at else ""
    detail_lines: list[str] = []
    if task.process_group_id:
        detail_lines.append(f"- 进程组：{task.process_group_id}")
    if task.idempotency_key:
        detail_lines.append(f"- 幂等键：{task.idempotency_key}")
    if task.port_hints:
        detail_lines.append(f"- 端口提示：{', '.join(str(port) for port in task.port_hints)}")
    details = ("\n" + "\n".join(detail_lines)) if detail_lines else ""
    preview = f"\n\n输出预览：\n```text\n{task.output_preview}\n```" if task.output_preview else ""
    return (
        f"### 后台任务 {task.id}\n"
        f"- 状态：{_status_label(task.status)}\n"
        f"- 命令：`{task.command}`\n"
        f"- 工作目录：`{task.cwd}`\n"
        f"- PID：{task.pid or '-'}"
        f"{details}\n"
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


def _extract_port_hints(command: str) -> list[int]:
    ports: list[int] = []
    for pattern in _PORT_PATTERNS:
        for match in pattern.finditer(command):
            try:
                port = int(match.group("port"))
            except ValueError:
                continue
            if 1024 <= port <= 65535 and port not in ports:
                ports.append(port)
    return ports


def _is_port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _finalize_output_file(output_file) -> None:
    try:
        output_file.flush()
        os.fsync(output_file.fileno())
    finally:
        output_file.close()


async def _run_output_io(operation: Callable[..., Any], *args: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


def _status_label(status: BackgroundStatus) -> str:
    return {
        BackgroundStatus.PREPARING: "准备中",
        BackgroundStatus.RUNNING: "运行中",
        BackgroundStatus.COMPLETED: "已完成",
        BackgroundStatus.FAILED: "失败",
        BackgroundStatus.CANCELLED: "已取消",
        BackgroundStatus.TIMED_OUT: "已超时",
    }[status]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _validate_idempotent_replay(
    task: BackgroundTask,
    *,
    command: str,
    cwd: str,
    timeout_seconds: int,
) -> None:
    if (
        task.command != command
        or task.cwd != cwd
        or task.timeout_seconds != timeout_seconds
    ):
        raise ValueError("幂等键已绑定不同的后台命令、工作目录或超时时间")
