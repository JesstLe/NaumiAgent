"""Governed, resumable Sandbox Eval batch orchestration over immutable H5a facts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantRequest,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.sandbox_eval import HarnessSandboxEvalRunAuthority
from naumi_agent.harness.store import HarnessStore, HarnessStoredEvalResult

logger = logging.getLogger(__name__)
SANDBOX_BATCH_POLICY = "harness-sandbox-batch-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SampleReceiptT = TypeVar("_SampleReceiptT", bound=BaseModel)
_BatchReceiptT = TypeVar("_BatchReceiptT", bound=BaseModel)

type BatchRecordsLoader = Callable[[], Awaitable[tuple[HarnessStoredEvalResult, ...]]]
type BatchPrefixValidator[ReceiptT: BaseModel] = Callable[
    [tuple[HarnessStoredEvalResult, ...]],
    Awaitable[list[ReceiptT]],
]
type BatchRunEvidenceValidator = Callable[
    [tuple[HarnessStoredEvalResult, ...]],
    None,
]
type BatchSampleExecutor[ReceiptT: BaseModel] = Callable[
    [int, HarnessSandboxEvalRunAuthority],
    Awaitable[ReceiptT],
]
type BatchReceiptBuilder[SampleReceiptT: BaseModel, BatchReceiptT: BaseModel] = Callable[
    [tuple[HarnessStoredEvalResult, ...], list[SampleReceiptT]],
    BatchReceiptT,
]
type BatchProgressCallback = Callable[["HarnessSandboxBatchCheckpoint"], Awaitable[None]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class HarnessSandboxBatchCheckpoint(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["harness-sandbox-batch-v1"] = SANDBOX_BATCH_POLICY
    checkpoint_id: str = Field(pattern=r"^hsbatch_[0-9a-f]{24}$")
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    lane: Literal["red", "green", "adversarial"]
    stage: Literal["recovering", "acquiring", "executing", "completed", "failed"]
    requested_samples: int = Field(ge=5, le=100)
    persisted_samples: int = Field(ge=0, le=100)
    sample_result_sha256: tuple[str, ...] = Field(max_length=100)
    run_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    run_grant_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    code: str = Field(default="", pattern=r"^(?:|[a-z][a-z0-9_]{0,127})$")
    updated_at: str

    @field_validator("sample_result_sha256")
    @classmethod
    def _valid_result_digests(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(_SHA256_RE.fullmatch(value) is None for value in values):
            raise ValueError("Sandbox Batch sample result digest 无效。")
        return values

    @field_validator("updated_at")
    @classmethod
    def _aware_time(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("Sandbox Batch checkpoint 时间必须包含时区。")
        return parsed.isoformat()

    @model_validator(mode="after")
    def _consistent_and_tamper_evident(self) -> Self:
        if not (
            self.persisted_samples == len(self.sample_result_sha256)
            and self.persisted_samples <= self.requested_samples
        ):
            raise ValueError("Sandbox Batch checkpoint 样本前缀不完整。")
        if self.stage == "completed" and self.persisted_samples != self.requested_samples:
            raise ValueError("completed Sandbox Batch 必须持久化全部样本。")
        if (self.stage == "failed") != bool(self.code):
            raise ValueError("Sandbox Batch failed stage 与 code 不一致。")
        if (self.run_grant_sha256 is None) != (self.run_id is None):
            raise ValueError("Sandbox Batch run/grant authority 必须同时存在。")
        expected = _sha256_payload(
            self.model_dump(
                mode="json",
                exclude={"checkpoint_id", "checkpoint_sha256"},
            )
        )
        if not hmac.compare_digest(self.checkpoint_sha256, expected):
            raise ValueError("Sandbox Batch checkpoint digest 不一致。")
        if self.checkpoint_id != f"hsbatch_{expected[:24]}":
            raise ValueError("Sandbox Batch checkpoint identity 不一致。")
        return self


class HarnessSandboxBatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HarnessSandboxBatchCoordinator:
    """Own one batch Runtime lease/Run Grant and recover continuous H5a prefixes."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        now: Callable[[], str] | None = None,
        token: Callable[[], str] | None = None,
        compatibility_scope: Literal["harness", "evolution"] = "harness",
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if compatibility_scope not in {"harness", "evolution"}:
            raise ValueError("Sandbox Batch compatibility_scope 无效。")
        if now is not None and not callable(now):
            raise TypeError("Sandbox Batch now 必须可调用。")
        if token is not None and not callable(token):
            raise TypeError("Sandbox Batch token 必须可调用。")
        if Path(run_grant_authority._workspace_root) != self.workspace_root:
            raise ValueError("Sandbox Batch Run Grant 与 workspace 不一致。")
        if run_grant_authority._permission_store is not permission_store:
            raise ValueError("Sandbox Batch Run Grant 与 Permission Store 不一致。")
        self.store = store
        self.permission_store = permission_store
        self.run_grant_authority = run_grant_authority
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self.token = token or (lambda: uuid4().hex)
        self.compatibility_scope = compatibility_scope

    async def execute(
        self,
        *,
        phase: Literal["red", "green", "adversarial"],
        authority_key: str,
        parent_receipt_id: str,
        requested_samples: int,
        max_total_duration_seconds: int,
        load_records: BatchRecordsLoader,
        validate_existing_prefix: BatchPrefixValidator[_SampleReceiptT],
        validate_run_evidence: BatchRunEvidenceValidator,
        execute_sample: BatchSampleExecutor[_SampleReceiptT],
        build_receipt: BatchReceiptBuilder[_SampleReceiptT, _BatchReceiptT],
        on_progress: BatchProgressCallback | None = None,
    ) -> _BatchReceiptT:
        lane = self._lane(phase)
        lane_name = lane.upper()
        if _SHA256_RE.fullmatch(authority_key) is None:
            raise self._error(
                "authority_key_invalid",
                f"{self._label(lane_name)} authority key 必须是 SHA-256。",
            )
        if (
            isinstance(requested_samples, bool)
            or not 5 <= requested_samples <= 100
            or isinstance(max_total_duration_seconds, bool)
            or not 60 <= max_total_duration_seconds <= 3_600
        ):
            raise self._error(
                "budget_invalid",
                f"{self._label(lane_name)} 样本数或总时限无效。",
            )
        records = await load_records()
        self._require_continuous_prefix(records, requested_samples, lane_name)
        receipts = await validate_existing_prefix(records)
        self._require_receipt_prefix(receipts, records, lane_name)
        validate_run_evidence(records)
        if len(records) == requested_samples:
            await self._emit(
                on_progress,
                authority_key=authority_key,
                lane=lane,
                stage="completed",
                requested_samples=requested_samples,
                records=records,
            )
            return build_receipt(records, receipts)
        await self._emit(
            on_progress,
            authority_key=authority_key,
            lane=lane,
            stage="recovering",
            requested_samples=requested_samples,
            records=records,
        )

        parent = await self.permission_store.get(parent_receipt_id)
        if (
            parent is None
            or not parent.authorizes_execution
            or not parent.run_id
            or "bash_run" not in parent.delegated_tool_names
        ):
            raise self._error(
                "parent_permission_invalid",
                f"{self._label(lane_name)} 缺少可执行的父权限回执。",
            )
        raw_token = self.token()
        token = raw_token.strip() if isinstance(raw_token, str) else ""
        if not token or len(token) > 64 or re.fullmatch(r"[A-Za-z0-9]+", token) is None:
            raise self._error(
                "owner_token_invalid",
                f"{self._label(lane_name)} owner token 格式无效。",
            )
        unit = "cohort" if self.compatibility_scope == "evolution" else "batch"
        owner_id = f"{self._owner_prefix()}-{lane}-{unit}-{token[:32]}"
        lease_seconds = min(3_600, max(60, max_total_duration_seconds))
        lease = await self.store.acquire_run_lease(
            workspace_root=self.workspace_root,
            run_kind=HarnessRunKind.RUNTIME,
            run_id=parent.run_id,
            owner_id=owner_id,
            now=self._now(lane_name),
            lease_seconds=lease_seconds,
        )
        if lease is None:
            raise self._error(
                "runtime_lease_unavailable",
                f"{self._label(lane_name)} 无法取得独占 Runtime lease。",
            )
        await self._emit(
            on_progress,
            authority_key=authority_key,
            lane=lane,
            stage="acquiring",
            requested_samples=requested_samples,
            records=records,
        )
        grant_id: str | None = None
        grant_sha256: str | None = None
        sample_failure: BaseException | None = None
        failed_records: tuple[HarnessStoredEvalResult, ...] = ()
        try:
            grant = await self.run_grant_authority.issue(
                RunDelegationGrantRequest(
                    idempotency_key=(
                        f"{self._owner_prefix()}-{lane}-{unit}-{authority_key[:18]}-"
                        f"{lease.epoch}-{hashlib.sha256(owner_id.encode()).hexdigest()[:12]}"
                    ),
                    parent_receipt_id=parent_receipt_id,
                    run_kind=HarnessRunKind.RUNTIME,
                    lease_owner_id=owner_id,
                    lease_epoch=lease.epoch,
                    delegated_tool_names=("bash_run",),
                ),
                now=self._now(lane_name),
                ttl_seconds=lease_seconds,
            )
            grant_id = grant.contract.grant_id
            grant_sha256 = grant.contract.grant_sha256
            authority = HarnessSandboxEvalRunAuthority(
                parent_receipt_id=parent_receipt_id,
                run_id=parent.run_id,
                grant_id=grant_id,
                grant_sha256=grant_sha256,
            )
            for sample_index in range(len(records), requested_samples):
                try:
                    receipts.append(await execute_sample(sample_index, authority))
                except BaseException as exc:
                    failed_records = await load_records()
                    self._require_continuous_prefix(
                        failed_records,
                        requested_samples,
                        lane_name,
                    )
                    sample_failure = exc
                    break
                persisted = await load_records()
                self._require_continuous_prefix(
                    persisted,
                    requested_samples,
                    lane_name,
                )
                self._require_receipt_prefix(receipts, persisted, lane_name)
                validate_run_evidence(persisted)
                await self._emit(
                    on_progress,
                    authority_key=authority_key,
                    lane=lane,
                    stage="executing",
                    requested_samples=requested_samples,
                    records=persisted,
                    run_id=parent.run_id,
                    run_grant_sha256=grant_sha256,
                )
        finally:
            await self._release_authority(
                lane_name=lane_name,
                grant_id=grant_id,
                run_id=parent.run_id,
                owner_id=owner_id,
                lease_epoch=lease.epoch,
            )

        if sample_failure is not None:
            await self._emit(
                on_progress,
                authority_key=authority_key,
                lane=lane,
                stage="failed",
                requested_samples=requested_samples,
                records=failed_records,
                run_id=parent.run_id,
                run_grant_sha256=grant_sha256,
                code="sample_execution_interrupted",
            )
            raise sample_failure.with_traceback(sample_failure.__traceback__)

        persisted = await load_records()
        self._require_continuous_prefix(persisted, requested_samples, lane_name)
        self._require_receipt_prefix(receipts, persisted, lane_name)
        validate_run_evidence(persisted)
        if len(persisted) != requested_samples:
            raise self._error(
                "persistence_incomplete",
                f"{self._label(lane_name)} 未完整写入 H5a。",
            )
        await self._emit(
            on_progress,
            authority_key=authority_key,
            lane=lane,
            stage="completed",
            requested_samples=requested_samples,
            records=persisted,
        )
        return build_receipt(persisted, receipts)

    def _lane(self, phase: str) -> Literal["red", "green", "adversarial"]:
        normalized = phase.strip().lower() if isinstance(phase, str) else ""
        if normalized not in {"red", "green", "adversarial"}:
            raise self._error(
                "phase_invalid",
                "Sandbox Batch phase 必须是 RED、GREEN 或 ADVERSARIAL。",
            )
        return normalized  # type: ignore[return-value]

    def _require_continuous_prefix(self, records, requested_samples, lane_name) -> None:
        indexes = tuple(item.sample_index for item in records)
        if indexes != tuple(range(len(records))) or len(records) > requested_samples:
            raise self._error(
                "sample_prefix_invalid",
                f"{self._label(lane_name)} H5a sample index 不连续或越界。",
            )

    def _require_receipt_prefix(self, receipts, records, lane_name) -> None:
        if len(receipts) != len(records) or any(
            getattr(receipt, "sample_index", None) != record.sample_index
            for receipt, record in zip(receipts, records, strict=True)
        ):
            raise self._error(
                "receipt_prefix_invalid",
                f"{self._label(lane_name)} sample receipt 前缀不完整。",
            )

    async def _release_authority(
        self,
        *,
        lane_name: str,
        grant_id: str | None,
        run_id: str,
        owner_id: str,
        lease_epoch: int,
    ) -> None:
        cleanup_at = self._now(lane_name)
        errors: list[BaseException] = []
        if grant_id is not None:
            try:
                await self.run_grant_authority.revoke(
                    grant_id=grant_id,
                    reason=(
                        "cohort_finished"
                        if self.compatibility_scope == "evolution"
                        else "sandbox_batch_finished"
                    ),
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
                errors.append(RuntimeError("Sandbox Batch Runtime lease 未能释放。"))
        except BaseException as exc:
            errors.append(exc)
        if errors:
            detail = "; ".join(str(item) for item in errors)
            raise self._error(
                "authority_cleanup_failed",
                f"{self._label(lane_name)} 权限清理不完整：{detail[:300]}",
            )

    async def _emit(
        self,
        callback: BatchProgressCallback | None,
        *,
        authority_key: str,
        lane: Literal["red", "green", "adversarial"],
        stage: Literal["recovering", "acquiring", "executing", "completed", "failed"],
        requested_samples: int,
        records: tuple[HarnessStoredEvalResult, ...],
        run_id: str | None = None,
        run_grant_sha256: str | None = None,
        code: str = "",
    ) -> None:
        if callback is None:
            return
        payload = {
            "schema_version": 1,
            "policy_version": SANDBOX_BATCH_POLICY,
            "authority_key": authority_key,
            "lane": lane,
            "stage": stage,
            "requested_samples": requested_samples,
            "persisted_samples": len(records),
            "sample_result_sha256": [item.result_sha256 for item in records],
            "run_id": run_id,
            "run_grant_sha256": run_grant_sha256,
            "code": code,
            "updated_at": self._now(lane.upper()),
        }
        digest = _sha256_payload(payload)
        checkpoint = HarnessSandboxBatchCheckpoint.model_validate({
            **payload,
            "checkpoint_id": f"hsbatch_{digest[:24]}",
            "checkpoint_sha256": digest,
        })
        try:
            await asyncio.wait_for(callback(checkpoint), timeout=1.0)
        except Exception as exc:
            logger.warning(
                "Sandbox Batch progress delivery failed (%s)",
                type(exc).__name__,
            )

    def _now(self, lane_name: str) -> str:
        value = self.now()
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise self._error(
                "clock_invalid",
                f"{self._label(lane_name)} 时钟格式无效。",
            ) from exc
        if parsed.utcoffset() is None:
            raise self._error(
                "clock_invalid",
                f"{self._label(lane_name)} 时钟必须包含时区。",
            )
        return parsed.isoformat()

    def _error(self, suffix: str, message: str) -> HarnessSandboxBatchError:
        prefix = "cohort" if self.compatibility_scope == "evolution" else "sandbox_batch"
        return HarnessSandboxBatchError(f"{prefix}_{suffix}", message)

    def _owner_prefix(self) -> str:
        return "evo" if self.compatibility_scope == "evolution" else "harness"

    def _label(self, lane_name: str) -> str:
        return (
            f"Interventional {lane_name} cohort"
            if self.compatibility_scope == "evolution"
            else f"Sandbox {lane_name} batch"
        )


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


__all__ = [
    "HarnessSandboxBatchCheckpoint",
    "HarnessSandboxBatchCoordinator",
    "HarnessSandboxBatchError",
    "SANDBOX_BATCH_POLICY",
]
