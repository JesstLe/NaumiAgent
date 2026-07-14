"""Trusted Profile check selection, execution, caching, and single-flight control."""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath

from naumi_agent.harness.fingerprint import (
    TreeFingerprint,
    TreeFingerprintError,
    compute_tree_fingerprint,
)
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.validation.executor import (
    CommandExecutionStatus,
    ValidationExecutor,
)
from naumi_agent.validation.policy import (
    ValidationCommandPolicy,
    ValidationPolicyError,
)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class HarnessCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    STALE = "stale"


@dataclass(frozen=True)
class HarnessCheckResult:
    check_id: str
    run_id: str
    status: HarnessCheckStatus
    tree_fingerprint: str
    profile_digest: str
    message: str
    output: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    cached: bool = False


ProfileRevalidator = Callable[[], Awaitable[bool]]


class HarnessCheckRunner:
    """Run exact trusted Profile checks and cache only current successful evidence."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        executor: ValidationExecutor | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self._executor = executor or ValidationExecutor()
        self._lock = asyncio.Lock()
        self._inflight: dict[
            tuple[str, str, str, str],
            asyncio.Task[HarnessCheckResult],
        ] = {}
        self._cache: OrderedDict[
            tuple[str, str, str, str],
            HarnessCheckResult,
        ] = OrderedDict()
        self._max_cache_entries = 256

    async def run(
        self,
        *,
        run_id: str,
        check: HarnessCheckSpec,
        profile_digest: str,
        profile_is_current: ProfileRevalidator,
    ) -> HarnessCheckResult:
        run_id = validate_run_id(run_id)
        try:
            fingerprint = await asyncio.to_thread(
                compute_tree_fingerprint,
                self.workspace_root,
            )
        except TreeFingerprintError as exc:
            return _blocked_result(
                check_id=check.id,
                run_id=run_id,
                profile_digest=profile_digest,
                message=str(exc),
                status=HarnessCheckStatus.INFRASTRUCTURE_ERROR,
            )
        key = (run_id, check.id, fingerprint.digest, profile_digest)
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return replace(cached, cached=True)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._execute_and_finalize(
                        key=key,
                        run_id=run_id,
                        check=check,
                        profile_digest=profile_digest,
                        fingerprint=fingerprint,
                        profile_is_current=profile_is_current,
                    )
                )
                self._inflight[key] = task
        return await asyncio.shield(task)

    async def _execute_and_finalize(
        self,
        *,
        key: tuple[str, str, str, str],
        run_id: str,
        check: HarnessCheckSpec,
        profile_digest: str,
        fingerprint: TreeFingerprint,
        profile_is_current: ProfileRevalidator,
    ) -> HarnessCheckResult:
        try:
            result = await self._execute(
                run_id=run_id,
                check=check,
                profile_digest=profile_digest,
                fingerprint=fingerprint,
                profile_is_current=profile_is_current,
            )
            async with self._lock:
                if result.status is HarnessCheckStatus.PASSED:
                    self._cache[key] = result
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._max_cache_entries:
                        self._cache.popitem(last=False)
            return result
        finally:
            async with self._lock:
                self._inflight.pop(key, None)

    async def _execute(
        self,
        *,
        run_id: str,
        check: HarnessCheckSpec,
        profile_digest: str,
        fingerprint: TreeFingerprint,
        profile_is_current: ProfileRevalidator,
    ) -> HarnessCheckResult:
        if not await profile_is_current():
            return _blocked_result(
                check_id=check.id,
                run_id=run_id,
                profile_digest=profile_digest,
                tree_fingerprint=fingerprint.digest,
                message="Harness Profile 在命令启动前已失去信任，检查未执行。",
            )
        try:
            approved = ValidationCommandPolicy(
                allowed_commands=(check.argv,),
                allowed_roots=(self.workspace_root,),
            ).approve(argv=check.argv, cwd=self.workspace_root)
        except ValidationPolicyError as exc:
            return _blocked_result(
                check_id=check.id,
                run_id=run_id,
                profile_digest=profile_digest,
                tree_fingerprint=fingerprint.digest,
                message=str(exc),
            )
        execution = await self._executor.run(
            argv=approved.argv,
            cwd=approved.cwd,
            timeout_seconds=check.timeout_seconds,
        )
        if not await profile_is_current():
            return _blocked_result(
                check_id=check.id,
                run_id=run_id,
                profile_digest=profile_digest,
                tree_fingerprint=fingerprint.digest,
                message=(
                    "Harness Profile 在检查执行期间发生变化；本次结果已作废，"
                    "请重新信任后再运行。"
                ),
            )
        try:
            final_fingerprint = await asyncio.to_thread(
                compute_tree_fingerprint,
                self.workspace_root,
            )
        except TreeFingerprintError as exc:
            return _blocked_result(
                check_id=check.id,
                run_id=run_id,
                profile_digest=profile_digest,
                tree_fingerprint=fingerprint.digest,
                message=str(exc),
                status=HarnessCheckStatus.INFRASTRUCTURE_ERROR,
            )
        if final_fingerprint.digest != fingerprint.digest:
            return HarnessCheckResult(
                check_id=check.id,
                run_id=run_id,
                status=HarnessCheckStatus.STALE,
                tree_fingerprint=final_fingerprint.digest,
                profile_digest=profile_digest,
                message="检查执行期间工作树发生变化；旧结果不能证明当前代码。",
                output=execution.output,
                exit_code=execution.exit_code,
                duration_ms=execution.duration_ms,
            )
        status = _map_execution_status(execution.status)
        return HarnessCheckResult(
            check_id=check.id,
            run_id=run_id,
            status=status,
            tree_fingerprint=fingerprint.digest,
            profile_digest=profile_digest,
            message=_status_message(check.id, status),
            output=execution.output,
            exit_code=execution.exit_code,
            duration_ms=execution.duration_ms,
        )


def select_required_check_ids(
    checks: Sequence[HarnessCheckSpec],
    *,
    task_kind: str,
    changed_paths: Sequence[str],
) -> tuple[str, ...]:
    selected: list[str] = []
    for check in checks:
        if task_kind not in check.required_for:
            continue
        if check.when_changed and not any(
            _matches_any_pattern(path, check.when_changed) for path in changed_paths
        ):
            continue
        selected.append(check.id)
    return tuple(selected)


def validate_run_id(run_id: str) -> str:
    normalized = run_id.strip()
    if not _RUN_ID_RE.fullmatch(normalized):
        raise ValueError(
            "run_id 必须为 1-128 位字母、数字、点、下划线、冒号或连字符。"
        )
    return normalized


def _matches_any_pattern(path: str, patterns: Sequence[str]) -> bool:
    candidate = PurePosixPath(path.replace("\\", "/"))
    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        if candidate.match(normalized):
            return True
        if "**/" in normalized and candidate.match(normalized.replace("**/", "")):
            return True
    return False


def _map_execution_status(status: CommandExecutionStatus) -> HarnessCheckStatus:
    return {
        CommandExecutionStatus.PASSED: HarnessCheckStatus.PASSED,
        CommandExecutionStatus.FAILED: HarnessCheckStatus.FAILED,
        CommandExecutionStatus.TIMED_OUT: HarnessCheckStatus.TIMED_OUT,
        CommandExecutionStatus.CANCELLED: HarnessCheckStatus.CANCELLED,
        CommandExecutionStatus.INFRASTRUCTURE_ERROR: (
            HarnessCheckStatus.INFRASTRUCTURE_ERROR
        ),
    }[status]


def _status_message(check_id: str, status: HarnessCheckStatus) -> str:
    return {
        HarnessCheckStatus.PASSED: f"Harness 检查 {check_id} 已通过。",
        HarnessCheckStatus.FAILED: f"Harness 检查 {check_id} 未通过。",
        HarnessCheckStatus.TIMED_OUT: f"Harness 检查 {check_id} 已超时。",
        HarnessCheckStatus.CANCELLED: f"Harness 检查 {check_id} 已取消。",
        HarnessCheckStatus.INFRASTRUCTURE_ERROR: (
            f"Harness 检查 {check_id} 未能正常启动。"
        ),
    }.get(status, f"Harness 检查 {check_id} 状态：{status.value}。")


def _blocked_result(
    *,
    check_id: str,
    run_id: str,
    profile_digest: str,
    message: str,
    tree_fingerprint: str = "-",
    status: HarnessCheckStatus = HarnessCheckStatus.BLOCKED_BY_POLICY,
) -> HarnessCheckResult:
    return HarnessCheckResult(
        check_id=check_id,
        run_id=run_id,
        status=status,
        tree_fingerprint=tree_fingerprint,
        profile_digest=profile_digest,
        message=message,
    )
