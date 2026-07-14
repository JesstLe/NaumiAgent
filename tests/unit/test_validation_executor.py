from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from naumi_agent.validation.executor import (
    CommandExecutionStatus,
    ValidationExecutor,
)
from naumi_agent.validation.policy import (
    ValidationCommandPolicy,
    ValidationPolicyError,
)


def test_policy_accepts_only_structured_allowlisted_argv(tmp_path: Path) -> None:
    policy = ValidationCommandPolicy(
        allowed_commands=((sys.executable, "-c"),),
        allowed_roots=(tmp_path,),
    )

    approved = policy.approve(
        argv=(sys.executable, "-c", "print('ok')"),
        cwd=tmp_path,
    )

    assert approved.argv == (sys.executable, "-c", "print('ok')")
    assert approved.cwd == tmp_path.resolve()

    with pytest.raises(ValidationPolicyError, match="非空 argv"):
        policy.approve(argv=(), cwd=tmp_path)
    with pytest.raises(ValidationPolicyError, match="不在允许列表"):
        policy.approve(argv=("rm", "-rf", "x"), cwd=tmp_path)
    with pytest.raises(ValidationPolicyError, match="不能包含 NUL"):
        policy.approve(argv=(sys.executable, "-c", "bad\x00arg"), cwd=tmp_path)


def test_policy_rejects_cwd_and_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    escape = workspace / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    policy = ValidationCommandPolicy(
        allowed_commands=((sys.executable,),),
        allowed_roots=(workspace,),
    )

    with pytest.raises(ValidationPolicyError, match="允许的工作区"):
        policy.approve(argv=(sys.executable, "--version"), cwd=outside)
    with pytest.raises(ValidationPolicyError, match="允许的工作区"):
        policy.approve(argv=(sys.executable, "--version"), cwd=escape)


@pytest.mark.asyncio
async def test_executor_distinguishes_success_failure_and_infrastructure_error(
    tmp_path: Path,
) -> None:
    executor = ValidationExecutor()

    success = await executor.run(
        argv=(sys.executable, "-c", "print('ok')"),
        cwd=tmp_path,
        timeout_seconds=5,
    )
    failure = await executor.run(
        argv=(sys.executable, "-c", "raise SystemExit(7)"),
        cwd=tmp_path,
        timeout_seconds=5,
    )
    missing = await executor.run(
        argv=(str(tmp_path / "missing-command"),),
        cwd=tmp_path,
        timeout_seconds=5,
    )

    assert success.status is CommandExecutionStatus.PASSED
    assert success.exit_code == 0
    assert success.output.strip() == "ok"
    assert failure.status is CommandExecutionStatus.FAILED
    assert failure.exit_code == 7
    assert missing.status is CommandExecutionStatus.INFRASTRUCTURE_ERROR
    assert missing.exit_code is None
    assert "无法启动验证命令" in missing.output


@pytest.mark.asyncio
async def test_executor_timeout_kills_the_entire_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-survived"
    code = _parent_with_delayed_grandchild(marker)
    executor = ValidationExecutor(terminate_grace_seconds=0.1)

    result = await executor.run(
        argv=(sys.executable, "-c", code),
        cwd=tmp_path,
        timeout_seconds=0.2,
    )
    await asyncio.sleep(1.2)

    assert result.status is CommandExecutionStatus.TIMED_OUT
    assert result.exit_code is None
    assert not marker.exists()


@pytest.mark.asyncio
async def test_executor_cancellation_kills_the_entire_process_group(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cancelled-grandchild-survived"
    code = _parent_with_delayed_grandchild(marker)
    executor = ValidationExecutor(terminate_grace_seconds=0.1)
    pending = asyncio.create_task(
        executor.run(
            argv=(sys.executable, "-c", code),
            cwd=tmp_path,
            timeout_seconds=30,
        )
    )
    await asyncio.sleep(0.2)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await asyncio.sleep(1.2)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_executor_cancel_event_returns_distinct_status_and_kills_group(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "event-cancelled-grandchild-survived"
    cancel_event = asyncio.Event()
    executor = ValidationExecutor(terminate_grace_seconds=0.1)
    pending = asyncio.create_task(
        executor.run(
            argv=(sys.executable, "-c", _parent_with_delayed_grandchild(marker)),
            cwd=tmp_path,
            timeout_seconds=30,
            cancel_event=cancel_event,
        )
    )
    await asyncio.sleep(0.2)

    cancel_event.set()
    result = await pending
    await asyncio.sleep(1.2)

    assert result.status is CommandExecutionStatus.CANCELLED
    assert result.exit_code is None
    assert not marker.exists()


@pytest.mark.asyncio
async def test_executor_keeps_full_artifact_and_bounded_output_tail(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "full.log"
    executor = ValidationExecutor(output_limit_bytes=16)

    result = await executor.run(
        argv=(sys.executable, "-c", "print('0123456789' * 10)"),
        cwd=tmp_path,
        timeout_seconds=5,
        artifact_path=artifact,
    )

    assert result.status is CommandExecutionStatus.PASSED
    assert result.output_truncated
    assert len(result.output.encode()) <= 16
    assert result.output.endswith("6789\n")
    assert len(artifact.read_bytes()) == result.output_bytes == 101


@pytest.mark.asyncio
async def test_executor_artifact_failure_is_infrastructure_error_and_kills_group(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "artifact-failure-grandchild-survived"
    executor = ValidationExecutor(terminate_grace_seconds=0.1)

    result = await executor.run(
        argv=(sys.executable, "-c", _parent_with_delayed_grandchild(marker)),
        cwd=tmp_path,
        timeout_seconds=5,
        artifact_path=tmp_path,
    )
    await asyncio.sleep(1.2)

    assert result.status is CommandExecutionStatus.INFRASTRUCTURE_ERROR
    assert "无法保存验证输出" in result.output
    assert not marker.exists()


def _parent_with_delayed_grandchild(marker: Path) -> str:
    child_code = (
        "import time; from pathlib import Path; "
        f"time.sleep(0.8); Path({str(marker)!r}).write_text('survived')"
    )
    return (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
