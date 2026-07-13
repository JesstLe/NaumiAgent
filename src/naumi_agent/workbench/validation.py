"""Validation run recording for workbench merge gates."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from naumi_agent.workbench.models import FailureKind, now_iso
from naumi_agent.workbench.store import WorkbenchStore


@dataclass(frozen=True)
class ValidationCommand:
    argv: list[str]
    cwd: str


@dataclass(frozen=True)
class ValidationResult:
    id: str
    status: str
    exit_code: int
    output: str


class ValidationRunner:
    """Run allowlisted validation commands and record their result."""

    def __init__(
        self,
        *,
        store: WorkbenchStore,
        allowed_commands: list[list[str]],
        timeout_seconds: int = 120,
    ) -> None:
        self._store = store
        self._allowed_commands = allowed_commands
        self._timeout_seconds = timeout_seconds

    async def run(
        self,
        *,
        session_id: str,
        task_id: str,
        actor: str,
        command: ValidationCommand,
    ) -> ValidationResult:
        self._ensure_allowed(command.argv)
        started = now_iso()
        runtime_argv = list(command.argv)
        if sys.platform == "win32" and runtime_argv[0] == "python3":
            runtime_argv[0] = sys.executable
        proc = await asyncio.create_subprocess_exec(
            *runtime_argv,
            cwd=command.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_seconds)
            output = stdout.decode("utf-8", errors="replace")
            exit_code = proc.returncode or 0
        except TimeoutError:
            proc.kill()
            await proc.wait()
            output = "验证命令超时"
            exit_code = 124
        completed = now_iso()
        status = "passed" if exit_code == 0 else "failed"
        run = await self._store.record_validation_run(
            session_id=session_id,
            task_id=task_id,
            actor=actor,
            command=command.argv,
            cwd=command.cwd,
            status=status,
            exit_code=exit_code,
            output=output[-6000:],
            started_at=started,
            completed_at=completed,
        )
        if status == "failed":
            await self._store.create_failure(
                session_id=session_id,
                task_id=task_id,
                kind=FailureKind.TEST_FAILED,
                title="验证命令失败",
                detail=output[-6000:],
                source_id=run["id"],
            )
        return ValidationResult(
            id=str(run["id"]),
            status=status,
            exit_code=exit_code,
            output=output,
        )

    def _ensure_allowed(self, argv: list[str]) -> None:
        for prefix in self._allowed_commands:
            if argv[: len(prefix)] == prefix:
                return
        raise ValueError(f"验证命令不在允许列表：{' '.join(argv)}")
