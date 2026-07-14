"""Validation run recording for workbench merge gates."""

from __future__ import annotations

from dataclasses import dataclass

from naumi_agent.validation.executor import (
    CommandExecutionStatus,
    ValidationExecutor,
)
from naumi_agent.validation.policy import ValidationCommandPolicy
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
        executor: ValidationExecutor | None = None,
    ) -> None:
        self._store = store
        self._policy = ValidationCommandPolicy(allowed_commands=allowed_commands)
        self._timeout_seconds = timeout_seconds
        self._executor = executor or ValidationExecutor()

    async def run(
        self,
        *,
        session_id: str,
        task_id: str,
        actor: str,
        command: ValidationCommand,
    ) -> ValidationResult:
        approved = self._policy.approve(argv=command.argv, cwd=command.cwd)
        started = now_iso()
        execution = await self._executor.run(
            argv=approved.argv,
            cwd=approved.cwd,
            timeout_seconds=self._timeout_seconds,
        )
        output = execution.output or _execution_status_message(execution.status)
        exit_code = _workbench_exit_code(execution.status, execution.exit_code)
        completed = now_iso()
        status = (
            "passed"
            if execution.status is CommandExecutionStatus.PASSED
            else "failed"
        )
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


def _workbench_exit_code(
    status: CommandExecutionStatus,
    exit_code: int | None,
) -> int:
    if exit_code is not None:
        return exit_code
    return {
        CommandExecutionStatus.TIMED_OUT: 124,
        CommandExecutionStatus.INFRASTRUCTURE_ERROR: 125,
        CommandExecutionStatus.CANCELLED: 130,
    }.get(status, 1)


def _execution_status_message(status: CommandExecutionStatus) -> str:
    return {
        CommandExecutionStatus.TIMED_OUT: "验证命令超时，进程组已终止。",
        CommandExecutionStatus.CANCELLED: "验证命令已取消，进程组已终止。",
        CommandExecutionStatus.INFRASTRUCTURE_ERROR: "验证命令未能启动。",
    }.get(status, "验证命令失败。")
