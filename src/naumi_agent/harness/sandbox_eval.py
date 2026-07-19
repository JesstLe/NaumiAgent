"""Governed ARC-04 execution for one ordered Sandbox Eval check group."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import RunDelegationGrantAuthority
from naumi_agent.daemons.shell_admission import ShellWorkerAdmissionComposer
from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.sandbox_checks import (
    HarnessSandboxCheckResult,
    HarnessSandboxCheckRunner,
    HarnessSandboxSourceOverlay,
)

_SHA256_RE = r"^[0-9a-f]{64}$"
type AuthorityRevalidator = Callable[[], Awaitable[bool]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class HarnessSandboxEvalRunAuthority(_StrictModel):
    parent_receipt_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    grant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    grant_sha256: str = Field(pattern=_SHA256_RE)


@dataclass(frozen=True, slots=True)
class HarnessSandboxEvalSource:
    revision: str
    revision_tree_sha256: str
    overlays: tuple[HarnessSandboxSourceOverlay, ...] = ()
    overlay_source_sha256: str | None = None
    source_is_current: AuthorityRevalidator | None = None


class HarnessSandboxEvalExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HarnessSandboxEvalExecutionKernel:
    """Execute exact ordered checks under one already-issued Run Grant."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        sandbox_runner: HarnessSandboxCheckRunner,
        shell_admission_composer: ShellWorkerAdmissionComposer,
        now: Callable[[], str],
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not callable(now):
            raise TypeError("now 必须可调用。")
        if Path(sandbox_runner.workspace_root) != self.workspace_root:
            raise ValueError("Sandbox Eval Runner 与 kernel workspace 不一致。")
        if Path(run_grant_authority._workspace_root) != self.workspace_root:
            raise ValueError("Sandbox Eval Run Grant 与 kernel workspace 不一致。")
        if run_grant_authority._permission_store is not permission_store:
            raise ValueError("Sandbox Eval Run Grant 与 Permission Store 不一致。")
        if (
            shell_admission_composer._permission_store is not permission_store
            or shell_admission_composer._run_delegation_grant_authority
            is not run_grant_authority
        ):
            raise ValueError("Sandbox Eval Composer authority 与 kernel 不一致。")
        self.permission_store = permission_store
        self.run_grant_authority = run_grant_authority
        self.sandbox_runner = sandbox_runner
        self.shell_admission_composer = shell_admission_composer
        self.now = now

    async def execute(
        self,
        *,
        lane: Literal["red", "green", "adversarial"],
        authority_key: str,
        parent_receipt_id: str,
        sample_index: int,
        checks: tuple[HarnessCheckSpec, ...],
        profile_digest: str,
        profile_is_current: AuthorityRevalidator,
        source: HarnessSandboxEvalSource,
        run_authority: HarnessSandboxEvalRunAuthority,
    ) -> tuple[HarnessSandboxCheckResult, ...]:
        if re.fullmatch(_SHA256_RE, authority_key) is None:
            raise HarnessSandboxEvalExecutionError(
                "sandbox_eval_authority_key_invalid",
                "Sandbox Eval authority key 必须是 SHA-256。",
            )
        if isinstance(sample_index, bool) or not 0 <= sample_index < 100:
            raise HarnessSandboxEvalExecutionError(
                "sandbox_eval_sample_index_invalid",
                "Sandbox Eval sample_index 超出允许范围。",
            )
        if re.fullmatch(_SHA256_RE, profile_digest) is None:
            raise HarnessSandboxEvalExecutionError(
                "sandbox_eval_profile_digest_invalid",
                "Sandbox Eval profile digest 必须是 SHA-256。",
            )
        if not isinstance(source, HarnessSandboxEvalSource):
            raise TypeError("source 必须是 HarnessSandboxEvalSource。")
        if not callable(profile_is_current):
            raise TypeError("profile_is_current 必须可调用。")
        if not 1 <= len(checks) <= 80 or any(
            not isinstance(item, HarnessCheckSpec) for item in checks
        ):
            raise HarnessSandboxEvalExecutionError(
                "sandbox_eval_checks_invalid",
                "Sandbox Eval 必须包含 1..80 个 typed Profile checks。",
            )
        check_ids = tuple(item.id for item in checks)
        if len(check_ids) != len(set(check_ids)):
            raise HarnessSandboxEvalExecutionError(
                "sandbox_eval_checks_duplicated",
                "Sandbox Eval Profile checks 不得重复。",
            )
        try:
            authority = HarnessSandboxEvalRunAuthority.model_validate(
                run_authority.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise HarnessSandboxEvalExecutionError(
                "sandbox_eval_run_authority_invalid",
                "Sandbox Eval Run authority 无效或已被篡改。",
            ) from exc
        parent = await self.permission_store.get(parent_receipt_id)
        if parent is None or not parent.authorizes_execution or not parent.run_id:
            raise HarnessSandboxEvalExecutionError(
                "parent_permission_invalid",
                "Sandbox Eval 缺少可执行的父权限回执。",
            )
        if "bash_run" not in parent.delegated_tool_names:
            raise HarnessSandboxEvalExecutionError(
                "parent_delegation_scope_missing",
                "父权限回执未授权 bash_run 运行委托。",
            )
        validation = await self.run_grant_authority.validate(
            grant_id=authority.grant_id,
            now=self.now(),
        )
        contract = validation.contract
        if not (
            validation.allowed
            and contract is not None
            and authority.parent_receipt_id == parent_receipt_id
            and authority.run_id == parent.run_id == contract.run_id
            and authority.grant_id == contract.grant_id
            and authority.grant_sha256 == contract.grant_sha256
            and contract.parent_receipt_id == parent_receipt_id
            and "bash_run" in contract.delegated_tool_names
        ):
            raise HarnessSandboxEvalExecutionError(
                "cohort_run_authority_invalid",
                "Sandbox Eval Run authority 已失效或不匹配。",
            )
        results: list[HarnessSandboxCheckResult] = []
        for check in checks:
            composed = None

            async def admit(spec, *, _grant_id=authority.grant_id):
                nonlocal composed
                if composed is not None:
                    raise RuntimeError("Sandbox Eval 单项检查不得重复 admission。")
                composed = await self.shell_admission_composer.compose(
                    parent_receipt_id=parent_receipt_id,
                    spec=spec,
                    run_grant_id=_grant_id,
                )
                return composed.admitted

            try:
                result = await self.sandbox_runner.run(
                    run_id=_check_run_id(
                        parent.run_id,
                        lane,
                        sample_index,
                        check.id,
                        authority_key=authority_key,
                    ),
                    check=check,
                    profile_digest=profile_digest,
                    profile_is_current=profile_is_current,
                    admit_job=admit,
                    source_revision=source.revision,
                    expected_source_tree_sha256=source.revision_tree_sha256,
                    source_overlays=source.overlays,
                    overlay_source_sha256=source.overlay_source_sha256,
                    source_is_current=source.source_is_current,
                )
                results.append(result)
            finally:
                if composed is not None:
                    await composed.release()
        if not results or not all(
            item.job_id and item.lifecycle_receipt_sha256 for item in results
        ):
            raise HarnessSandboxEvalExecutionError(
                "project_code_not_executed",
                "Sandbox Eval 未形成完整 ARC-04 Worker 执行证据。",
            )
        return tuple(results)


def _check_run_id(
    parent_run_id: str,
    lane: Literal["red", "green", "adversarial"],
    sample_index: int,
    check_id: str,
    *,
    authority_key: str,
) -> str:
    if lane == "red":
        material = f"{parent_run_id}:{sample_index}:{check_id}"
    elif lane == "adversarial":
        material = (
            f"{parent_run_id}:{lane}:{authority_key}:{sample_index}:{check_id}"
        )
    else:
        material = f"{parent_run_id}:{lane}:{sample_index}:{check_id}"
    digest = hashlib.sha256(material.encode()).hexdigest()
    prefix = "evo" if lane in {"red", "green"} else "heval"
    return f"{prefix}{lane}-{digest[:32]}"


__all__ = [
    "HarnessSandboxEvalExecutionError",
    "HarnessSandboxEvalExecutionKernel",
    "HarnessSandboxEvalRunAuthority",
    "HarnessSandboxEvalSource",
]
