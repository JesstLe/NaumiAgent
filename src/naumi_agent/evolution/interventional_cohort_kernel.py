"""Shared governed orchestration for continuous interventional cohorts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantRequest,
)
from naumi_agent.evolution.interventional_sample_kernel import (
    EvolutionInterventionalRunAuthority,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore, HarnessStoredEvalResult

_SampleReceiptT = TypeVar("_SampleReceiptT", bound=BaseModel)
_CohortReceiptT = TypeVar("_CohortReceiptT", bound=BaseModel)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

type CohortRecordsLoader = Callable[[], Awaitable[tuple[HarnessStoredEvalResult, ...]]]
type CohortPrefixValidator[ReceiptT: BaseModel] = Callable[
    [tuple[HarnessStoredEvalResult, ...]],
    Awaitable[list[ReceiptT]],
]
type CohortRunEvidenceValidator = Callable[
    [tuple[HarnessStoredEvalResult, ...]],
    None,
]
type CohortSampleExecutor[ReceiptT: BaseModel] = Callable[
    [int, EvolutionInterventionalRunAuthority],
    Awaitable[ReceiptT],
]
type CohortReceiptBuilder[SampleReceiptT: BaseModel, CohortReceiptT: BaseModel] = Callable[
    [tuple[HarnessStoredEvalResult, ...], list[SampleReceiptT]],
    CohortReceiptT,
]


class EvolutionInterventionalCohortKernelError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionInterventionalCohortKernel:
    """Own one cohort Runtime lease/Run Grant and recover continuous H5a prefixes."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        now: Callable[[], str] | None = None,
        token: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.store = store
        self.permission_store = permission_store
        self.run_grant_authority = run_grant_authority
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self.token = token or (lambda: uuid4().hex)

    async def execute(
        self,
        *,
        phase: Literal["red", "green"],
        authority_key: str,
        parent_receipt_id: str,
        requested_samples: int,
        max_total_duration_seconds: int,
        load_records: CohortRecordsLoader,
        validate_existing_prefix: CohortPrefixValidator[_SampleReceiptT],
        validate_run_evidence: CohortRunEvidenceValidator,
        execute_sample: CohortSampleExecutor[_SampleReceiptT],
        build_receipt: CohortReceiptBuilder[_SampleReceiptT, _CohortReceiptT],
    ) -> _CohortReceiptT:
        phase_name = self._phase(phase)
        if _SHA256_RE.fullmatch(authority_key) is None:
            raise EvolutionInterventionalCohortKernelError(
                "cohort_authority_key_invalid",
                f"Interventional {phase_name} cohort authority key 必须是 SHA-256。",
            )
        if (
            isinstance(requested_samples, bool)
            or not 5 <= requested_samples <= 100
            or isinstance(max_total_duration_seconds, bool)
            or not 60 <= max_total_duration_seconds <= 3_600
        ):
            raise EvolutionInterventionalCohortKernelError(
                "cohort_budget_invalid",
                f"Interventional {phase_name} cohort 样本数或总时限无效。",
            )
        records = await load_records()
        self._require_continuous_prefix(records, requested_samples, phase_name)
        receipts = await validate_existing_prefix(records)
        self._require_receipt_prefix(receipts, records, phase_name)
        validate_run_evidence(records)
        if len(records) == requested_samples:
            return build_receipt(records, receipts)

        parent = await self.permission_store.get(parent_receipt_id)
        if (
            parent is None
            or not parent.authorizes_execution
            or not parent.run_id
            or "bash_run" not in parent.delegated_tool_names
        ):
            raise EvolutionInterventionalCohortKernelError(
                "cohort_parent_permission_invalid",
                f"Interventional {phase_name} cohort 缺少可执行的父权限回执。",
            )
        raw_token = self.token()
        token = raw_token.strip() if isinstance(raw_token, str) else ""
        if not token or len(token) > 64 or re.fullmatch(r"[A-Za-z0-9]+", token) is None:
            raise EvolutionInterventionalCohortKernelError(
                "cohort_owner_token_invalid",
                f"Interventional {phase_name} cohort owner token 格式无效。",
            )
        owner_id = f"evo-{phase_name.lower()}-cohort-{token[:32]}"
        lease_seconds = min(3_600, max(60, max_total_duration_seconds))
        lease = await self.store.acquire_run_lease(
            workspace_root=self.workspace_root,
            run_kind=HarnessRunKind.RUNTIME,
            run_id=parent.run_id,
            owner_id=owner_id,
            now=self._now(phase_name),
            lease_seconds=lease_seconds,
        )
        if lease is None:
            raise EvolutionInterventionalCohortKernelError(
                "cohort_runtime_lease_unavailable",
                f"Interventional {phase_name} cohort 无法取得独占 Runtime lease。",
            )
        grant_id: str | None = None
        try:
            grant = await self.run_grant_authority.issue(
                RunDelegationGrantRequest(
                    idempotency_key=(
                        f"evo-{phase_name.lower()}-cohort-{authority_key[:18]}-"
                        f"{lease.epoch}-{hashlib.sha256(owner_id.encode()).hexdigest()[:12]}"
                    ),
                    parent_receipt_id=parent_receipt_id,
                    run_kind=HarnessRunKind.RUNTIME,
                    lease_owner_id=owner_id,
                    lease_epoch=lease.epoch,
                    delegated_tool_names=("bash_run",),
                ),
                now=self._now(phase_name),
                ttl_seconds=lease_seconds,
            )
            grant_id = grant.contract.grant_id
            authority = EvolutionInterventionalRunAuthority(
                parent_receipt_id=parent_receipt_id,
                run_id=parent.run_id,
                grant_id=grant.contract.grant_id,
                grant_sha256=grant.contract.grant_sha256,
            )
            for sample_index in range(len(records), requested_samples):
                receipts.append(await execute_sample(sample_index, authority))
        finally:
            await self._release_authority(
                phase_name=phase_name,
                grant_id=grant_id,
                run_id=parent.run_id,
                owner_id=owner_id,
                lease_epoch=lease.epoch,
            )

        persisted = await load_records()
        self._require_continuous_prefix(persisted, requested_samples, phase_name)
        self._require_receipt_prefix(receipts, persisted, phase_name)
        validate_run_evidence(persisted)
        if len(persisted) != requested_samples:
            raise EvolutionInterventionalCohortKernelError(
                "cohort_persistence_incomplete",
                f"Interventional {phase_name} cohort 未完整写入 H5a。",
            )
        return build_receipt(persisted, receipts)

    @staticmethod
    def _phase(phase: str) -> str:
        normalized = phase.strip().upper() if isinstance(phase, str) else ""
        if normalized not in {"RED", "GREEN"}:
            raise EvolutionInterventionalCohortKernelError(
                "cohort_phase_invalid",
                "Interventional cohort phase 必须是 RED 或 GREEN。",
            )
        return normalized

    @staticmethod
    def _require_continuous_prefix(records, requested_samples, phase_name) -> None:
        indexes = tuple(item.sample_index for item in records)
        if indexes != tuple(range(len(records))) or len(records) > requested_samples:
            raise EvolutionInterventionalCohortKernelError(
                "cohort_sample_prefix_invalid",
                f"Interventional {phase_name} H5a sample index 不连续或越界。",
            )

    @staticmethod
    def _require_receipt_prefix(receipts, records, phase_name) -> None:
        if len(receipts) != len(records) or any(
            getattr(receipt, "sample_index", None) != record.sample_index
            for receipt, record in zip(receipts, records, strict=True)
        ):
            raise EvolutionInterventionalCohortKernelError(
                "cohort_receipt_prefix_invalid",
                f"Interventional {phase_name} sample receipt 前缀不完整。",
            )

    async def _release_authority(
        self,
        *,
        phase_name: str,
        grant_id: str | None,
        run_id: str,
        owner_id: str,
        lease_epoch: int,
    ) -> None:
        cleanup_at = self._now(phase_name)
        errors: list[BaseException] = []
        if grant_id is not None:
            try:
                await self.run_grant_authority.revoke(
                    grant_id=grant_id,
                    reason="cohort_finished",
                    revoked_at=cleanup_at,
                )
            except BaseException as exc:
                errors.append(exc)
        try:
            released = await self.store.release_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=run_id,
                owner_id=owner_id,
                epoch=lease_epoch,
                now=cleanup_at,
            )
            if released is None:
                errors.append(RuntimeError("Cohort Runtime lease 未能释放。"))
        except BaseException as exc:
            errors.append(exc)
        if errors:
            detail = "; ".join(str(item) for item in errors)
            raise EvolutionInterventionalCohortKernelError(
                "cohort_authority_cleanup_failed",
                f"Interventional {phase_name} cohort 权限清理不完整：{detail[:300]}",
            )

    def _now(self, phase_name: str) -> str:
        value = self.now()
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise EvolutionInterventionalCohortKernelError(
                "cohort_clock_invalid",
                f"Interventional {phase_name} cohort 时钟格式无效。",
            ) from exc
        if parsed.utcoffset() is None:
            raise EvolutionInterventionalCohortKernelError(
                "cohort_clock_invalid",
                f"Interventional {phase_name} cohort 时钟必须包含时区。",
            )
        return value


__all__ = [
    "EvolutionInterventionalCohortKernel",
    "EvolutionInterventionalCohortKernelError",
]
