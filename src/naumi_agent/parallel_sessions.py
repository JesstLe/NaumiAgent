"""Launch independent interactive NaumiAgent sessions."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_PARALLEL_SESSIONS = 1
MAX_PARALLEL_SESSIONS = 10


class ParallelSessionLaunchError(RuntimeError):
    """Raised when independent terminal sessions cannot be launched safely."""


@dataclass(frozen=True)
class ParallelSessionProcess:
    slot: int
    pid: int
    workspace: Path


@dataclass(frozen=True)
class ParallelSessionLaunchResult:
    workspace: Path
    processes: tuple[ParallelSessionProcess, ...]

    @property
    def count(self) -> int:
        return len(self.processes)


def parse_parallel_request(raw: str, *, default_workspace: str | Path) -> tuple[int, Path]:
    """Parse ``[count] [workspace]`` without losing quoted Windows paths."""
    text = str(raw or "").strip()
    if not text:
        return 1, _resolve_workspace(default_workspace)

    try:
        parts = shlex.split(text, posix=os.name != "nt")
    except ValueError as exc:
        raise ParallelSessionLaunchError(f"并行会话参数无法解析：{exc}") from exc
    parts = [_strip_matching_quotes(part) for part in parts]
    if not parts:
        return 1, _resolve_workspace(default_workspace)

    count = 1
    if parts[0].isdigit():
        count = int(parts.pop(0))
    _validate_count(count)
    if len(parts) > 1:
        raise ParallelSessionLaunchError("工作目录包含空格时请用双引号包裹。")
    workspace = parts[0] if parts else default_workspace
    return count, _resolve_workspace(workspace)


def launch_parallel_sessions(
    *,
    count: int,
    workspace: str | Path,
    config_path: str | Path,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    platform: str | None = None,
) -> ParallelSessionLaunchResult:
    """Launch one OS process and one engine per interactive conversation."""
    _validate_count(count)
    resolved_workspace = _resolve_workspace(workspace)
    resolved_config = Path(config_path).expanduser().resolve()
    if not resolved_config.is_file():
        raise ParallelSessionLaunchError(f"配置文件不存在：{resolved_config}")

    command = _child_command(resolved_config)
    target_platform = os.name if platform is None else platform
    creationflags = 0
    if target_platform == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0)) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )

    launched: list[Any] = []
    processes: list[ParallelSessionProcess] = []
    try:
        for slot in range(1, count + 1):
            environment = os.environ.copy()
            environment["NAUMI_PARALLEL_PARENT_PID"] = str(os.getpid())
            environment["NAUMI_PARALLEL_SLOT"] = str(slot)
            process = popen_factory(
                command,
                cwd=str(resolved_workspace),
                env=environment,
                close_fds=True,
                creationflags=creationflags,
            )
            launched.append(process)
            processes.append(
                ParallelSessionProcess(
                    slot=slot,
                    pid=int(process.pid),
                    workspace=resolved_workspace,
                )
            )
    except (OSError, ValueError) as exc:
        for process in launched:
            try:
                process.terminate()
            except OSError:
                pass
        raise ParallelSessionLaunchError(
            f"并行会话仅启动 {len(launched)}/{count} 个，已停止本次创建：{exc}"
        ) from exc

    return ParallelSessionLaunchResult(
        workspace=resolved_workspace,
        processes=tuple(processes),
    )


def render_parallel_launch_result(result: ParallelSessionLaunchResult) -> str:
    pids = "、".join(str(item.pid) for item in result.processes)
    return (
        f"已打开 {result.count} 个独立并行会话。\n"
        f"- 工作目录：`{result.workspace}`\n"
        f"- 进程 PID：{pids}\n"
        "每个窗口拥有独立 Engine 和会话；同时修改同一文件时请使用不同 Git worktree。"
    )


def _child_command(config_path: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--config", str(config_path)]
    return [sys.executable, "-m", "naumi_agent", "--config", str(config_path)]


def _validate_count(count: int) -> None:
    if isinstance(count, bool) or not MIN_PARALLEL_SESSIONS <= int(count) <= MAX_PARALLEL_SESSIONS:
        raise ParallelSessionLaunchError(
            f"并行会话数量必须在 {MIN_PARALLEL_SESSIONS} 到 {MAX_PARALLEL_SESSIONS} 之间。"
        )


def _resolve_workspace(workspace: str | Path) -> Path:
    resolved = Path(workspace).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ParallelSessionLaunchError(f"工作目录不存在或不是文件夹：{resolved}")
    return resolved


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
