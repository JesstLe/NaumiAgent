"""Finalize one remote platform lane from locally accepted H5a evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.config.credentials import resolve_runtime_payload_key
from naumi_agent.daemons.worker_registry import (
    WorkerCapacityReservationState,
    WorkerRegistryConflictError,
    WorkerRegistryStore,
    WorkerRegistryStoreError,
)
from naumi_agent.evolution.revalidation_adversarial_cohorts import (
    EvolutionRevalidationAdversarialCohortExecutor,
    EvolutionRevalidationAdversarialCohortReceipt,
)
from naumi_agent.evolution.revalidation_adversarial_matrices import (
    EvolutionRevalidationAdversarialMatrixService,
    EvolutionRevalidationAdversarialMatrixStatus,
)
from naumi_agent.evolution.revalidation_platform_claims import (
    EvolutionRevalidationPlatformClaimReceipt,
    EvolutionRevalidationPlatformClaimStore,
)
from naumi_agent.evolution.revalidation_platform_dispatches import (
    EvolutionRevalidationPlatformDispatch,
    EvolutionRevalidationPlatformDispatchStore,
)
from naumi_agent.evolution.revalidation_platform_execution_authorizations import (
    EvolutionRevalidationPlatformExecutionAuthorizationService,
)
from naumi_agent.evolution.revalidation_platform_results import (
    EvolutionRevalidationPlatformResultIngestionReceipt,
    EvolutionRevalidationPlatformResultStore,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)

EVOLUTION_REVALIDATION_PLATFORM_COMPLETION_POLICY = (
    "evolution-revalidation-platform-completion-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationPlatformCompletionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-platform-completion-v1"] = (
        EVOLUTION_REVALIDATION_PLATFORM_COMPLETION_POLICY
    )
    receipt_id: str = Field(pattern=r"^evrevalplatcomplete_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    platform: Literal["linux", "macos", "windows"]
    dispatch_id: str = Field(pattern=r"^evrevalplatdispatch_[0-9a-f]{24}$")
    dispatch_sha256: str = Field(pattern=_SHA256_RE)
    claim_id: str = Field(pattern=r"^evrevalplatclaim_[0-9a-f]{24}$")
    final_claim_receipt_id: str = Field(pattern=r"^evrevalclaimreceipt_[0-9a-f]{24}$")
    final_claim_receipt_sha256: str = Field(pattern=_SHA256_RE)
    identity_id: str = Field(pattern=r"^evrevalworkerid_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    reservation_id: str = Field(pattern=r"^evrevalplatres_[0-9a-f]{24}$")
    reservation_terminal_state: Literal["released", "expired", "fenced"]
    reservation_terminal_reason: str = Field(min_length=1, max_length=64)
    authorization_id: tuple[str, ...] = Field(min_length=1, max_length=100)
    authorization_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    authorization_revocation_sha256: tuple[str, ...] = Field(
        min_length=1, max_length=100
    )
    result_receipt_id: tuple[str, ...] = Field(min_length=1, max_length=100)
    result_receipt_sha256: tuple[str, ...] = Field(min_length=1, max_length=100)
    requested_samples: int = Field(ge=5, le=100)
    accepted_samples: int = Field(ge=5, le=100)
    sample_receipt_sha256: tuple[str, ...] = Field(min_length=5, max_length=100)
    cohort_receipt_id: str = Field(pattern=r"^evrevaladvcohort_[0-9a-f]{24}$")
    cohort_receipt_sha256: str = Field(pattern=_SHA256_RE)
    control_plane_attestation_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    control_plane_attestation_sha256: str = Field(pattern=_SHA256_RE)
    result_prefix_verified: Literal[True] = True
    run_grants_terminal: Literal[True] = True
    capacity_terminal: Literal[True] = True
    dispatch_complete: Literal[True] = True
    cohort_complete: Literal[True] = True
    matrix_authority: Literal[False] = False
    comparison_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    completed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Platform completion workspace 必须 canonical。")
        if not (
            len(self.authorization_id)
            == len(self.authorization_sha256)
            == len(self.authorization_revocation_sha256)
            and len(self.result_receipt_id) == len(self.result_receipt_sha256)
            and self.accepted_samples
            == self.requested_samples
            == len(self.sample_receipt_sha256)
            and self.authorization_id == tuple(dict.fromkeys(self.authorization_id))
        ):
            raise ValueError("Platform completion evidence 数量或顺序无效。")
        _aware(self.completed_at)
        core = self.model_dump(
            mode="json",
            exclude={
                "receipt_id",
                "receipt_sha256",
                "control_plane_attestation_sha256",
            },
        )
        digest = _digest(core)
        if self.receipt_sha256 != digest:
            raise ValueError("Platform completion receipt digest 不一致。")
        if self.receipt_id != f"evrevalplatcomplete_{digest[:24]}":
            raise ValueError("Platform completion receipt id 不一致。")
        return self


class EvolutionRevalidationPlatformCompletionView(_StrictModel):
    receipt: EvolutionRevalidationPlatformCompletionReceipt
    matrix: EvolutionRevalidationAdversarialMatrixStatus
    platform_complete: Literal[True] = True
    matrix_complete: bool
    comparison_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        if self.matrix_complete is not self.matrix.matrix_complete:
            raise ValueError("Platform completion matrix 投影不一致。")
        lane = next(
            (item for item in self.matrix.lanes if item.platform == self.receipt.platform),
            None,
        )
        if lane is None or lane.status != "completed" or (
            lane.cohort_receipt_id != self.receipt.cohort_receipt_id
            or lane.cohort_receipt_sha256 != self.receipt.cohort_receipt_sha256
        ):
            raise ValueError("Platform completion view 未绑定 completed Matrix lane。")
        return self


class EvolutionRevalidationPlatformCompletionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationPlatformCompletionStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        control_plane_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._control_plane_key_provider = control_plane_key_provider

    async def get(self, contract_id: str, platform: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_platform_completions "
                    "WHERE contract_id = ? AND platform = ?",
                    (contract_id, platform),
                )
            ).fetchone()
        if row is None:
            return None
        item = _completion(row["receipt_json"])
        self._verify_attestation(item)
        return item

    async def record(self, receipt):
        item = EvolutionRevalidationPlatformCompletionReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        self._verify_attestation(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            dispatch_row = await (
                await db.execute(
                    "SELECT dispatch_json FROM evolution_revalidation_platform_dispatches "
                    "WHERE dispatch_id = ?",
                    (item.dispatch_id,),
                )
            ).fetchone()
            claim_row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_platform_claim_receipts "
                    "WHERE claim_id = ? ORDER BY sequence DESC LIMIT 1",
                    (item.claim_id,),
                )
            ).fetchone()
            cohort_row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_adversarial_cohorts "
                    "WHERE receipt_id = ?",
                    (item.cohort_receipt_id,),
                )
            ).fetchone()
            if not all((dispatch_row, claim_row, cohort_row)):
                await db.rollback()
                raise EvolutionRevalidationPlatformCompletionError(
                    "platform_completion_dependency_missing",
                    "Platform completion 的 Dispatch/Claim/Cohort 依赖缺失。",
                )
            dispatch = EvolutionRevalidationPlatformDispatch.model_validate_json(
                dispatch_row["dispatch_json"]
            )
            cohort = EvolutionRevalidationAdversarialCohortReceipt.model_validate_json(
                cohort_row["receipt_json"]
            )
            claim = EvolutionRevalidationPlatformClaimReceipt.model_validate_json(
                claim_row["receipt_json"]
            )
            if not _base_dependencies_match(item, dispatch, claim, cohort):
                await db.rollback()
                raise EvolutionRevalidationPlatformCompletionError(
                    "platform_completion_dependency_mismatch",
                    "Platform completion 未绑定 exact Dispatch/Claim/Cohort。",
                )
            result_rows = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_platform_result_receipts "
                    "WHERE contract_id = ? AND platform = ? ORDER BY start_index",
                    (item.contract_id, item.platform),
                )
            ).fetchall()
            results = tuple(
                EvolutionRevalidationPlatformResultIngestionReceipt.model_validate_json(
                    row["receipt_json"]
                )
                for row in result_rows
            )
            if not _result_dependencies_match(item, results, cohort):
                await db.rollback()
                raise EvolutionRevalidationPlatformCompletionError(
                    "platform_completion_result_mismatch",
                    "Platform completion result prefix 与 Cohort 不一致。",
                )
            for auth_id, auth_sha, revocation_sha in zip(
                item.authorization_id,
                item.authorization_sha256,
                item.authorization_revocation_sha256,
                strict=True,
            ):
                auth = await (
                    await db.execute(
                        "SELECT authorization_sha256 FROM "
                        "evolution_revalidation_platform_execution_authorizations "
                        "WHERE authorization_id = ?",
                        (auth_id,),
                    )
                ).fetchone()
                revocation = await (
                    await db.execute(
                        "SELECT revocation_sha256 FROM "
                        "evolution_revalidation_platform_execution_revocations "
                        "WHERE authorization_id = ?",
                        (auth_id,),
                    )
                ).fetchone()
                if not (
                    auth is not None
                    and revocation is not None
                    and auth["authorization_sha256"] == auth_sha
                    and revocation["revocation_sha256"] == revocation_sha
                ):
                    await db.rollback()
                    raise EvolutionRevalidationPlatformCompletionError(
                        "platform_completion_authorization_not_terminal",
                        "Platform completion 存在未收口 execution authorization。",
                    )
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_revalidation_platform_completions "
                    "WHERE contract_id = ? AND platform = ?",
                    (item.contract_id, item.platform),
                )
            ).fetchone()
            if existing is not None:
                restored = _completion(existing["receipt_json"])
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationPlatformCompletionError(
                        "platform_completion_conflict",
                        "同一 platform lane 已绑定不同 completion。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_platform_completions "
                "(receipt_id, receipt_sha256, contract_id, platform, dispatch_id, "
                "cohort_receipt_id, cohort_receipt_sha256, receipt_json, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.contract_id,
                    item.platform,
                    item.dispatch_id,
                    item.cohort_receipt_id,
                    item.cohort_receipt_sha256,
                    item.model_dump_json(),
                    item.completed_at,
                ),
            )
            await db.commit()
        return item

    def _verify_attestation(self, item):
        core = item.model_dump(
            mode="json",
            exclude={
                "receipt_id",
                "receipt_sha256",
                "control_plane_attestation_sha256",
            },
        )
        expected = hmac.new(self._key(), _canonical(core), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, item.control_plane_attestation_sha256):
            raise EvolutionRevalidationPlatformCompletionError(
                "platform_completion_attestation_invalid",
                "Platform completion control-plane attestation 无效。",
            )

    def _key(self):
        key = self._control_plane_key_provider()
        if not isinstance(key, bytes) or len(key) < 32:
            raise EvolutionRevalidationPlatformCompletionError(
                "platform_completion_control_plane_key_invalid",
                "Platform completion control-plane key 不可用。",
            )
        return key


class EvolutionRevalidationPlatformCompletionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_service: EvolutionRevalidationRuntimeContractService,
        dispatch_store: EvolutionRevalidationPlatformDispatchStore,
        claim_store: EvolutionRevalidationPlatformClaimStore,
        authorization_service: EvolutionRevalidationPlatformExecutionAuthorizationService,
        result_store: EvolutionRevalidationPlatformResultStore,
        cohort_executor: EvolutionRevalidationAdversarialCohortExecutor,
        matrix_service: EvolutionRevalidationAdversarialMatrixService,
        worker_registry: WorkerRegistryStore,
        store: EvolutionRevalidationPlatformCompletionStore,
        control_plane_key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_service = contract_service
        self.dispatch_store = dispatch_store
        self.claim_store = claim_store
        self.authorization_service = authorization_service
        self.result_store = result_store
        self.cohort_executor = cohort_executor
        self.matrix_service = matrix_service
        self.worker_registry = worker_registry
        self.store = store
        self._control_plane_key_provider = control_plane_key_provider
        self.now = now or (lambda: datetime.now(UTC).isoformat())

    async def finalize(self, *, contract_id: str, platform: str):
        now = _aware(self.now())
        existing = await self.store.get(contract_id, platform)
        if existing is not None:
            matrix = await self.matrix_service.inspect(
                contract_id=contract_id,
                assessed_at=existing.completed_at,
            )
            return _view(existing, matrix)
        contract_view = await self.contract_service.inspect(
            workspace_root=self.workspace_root,
            contract_id=contract_id,
        )
        if not contract_view.execution_eligible or platform not in (
            contract_view.contract.required_platforms
        ):
            raise EvolutionRevalidationPlatformCompletionError(
                "platform_completion_contract_not_ready",
                "Platform completion 对应 Runtime Contract/platform 不可用。",
            )
        contract = contract_view.contract
        dispatch = await self.dispatch_store.get(contract_id, platform)
        if dispatch is None:
            raise EvolutionRevalidationPlatformCompletionError(
                "platform_completion_dispatch_missing",
                "Platform completion 缺少 durable Dispatch。",
            )
        result_receipts = await self.result_store.list_receipts(contract_id, platform)
        _require_complete_result_prefix(result_receipts, contract.requested_samples)
        claim = await self.claim_store.get_latest_for_dispatch(dispatch.dispatch_id)
        if claim is None:
            raise EvolutionRevalidationPlatformCompletionError(
                "platform_completion_claim_missing",
                "Platform completion 缺少 authenticated Claim history。",
            )
        cohort = await self.cohort_executor.execute(
            contract_id=contract_id,
            platform=platform,
            parent_receipt_id="remote-platform-result-prefix-complete",
        )
        auth_ids = tuple(dict.fromkeys(item.authorization_id for item in result_receipts))
        auths = []
        revocations = []
        for auth_id in auth_ids:
            auth = await self.authorization_service.store.get(auth_id)
            if auth is None or not (
                auth.contract_id == contract_id
                and auth.dispatch_id == dispatch.dispatch_id
                and auth.platform == platform
            ):
                raise EvolutionRevalidationPlatformCompletionError(
                    "platform_completion_authorization_mismatch",
                    "Platform result prefix 引用了错误 execution authorization。",
                )
            revocation = await self.authorization_service.store.get_revocation(auth_id)
            if revocation is None:
                view = await self.authorization_service.revoke(
                    authorization_id=auth_id,
                    reason_code="platform_result_completed",
                    revoked_at=now.isoformat(),
                )
                revocation = view.revocation
            if revocation is None:
                raise EvolutionRevalidationPlatformCompletionError(
                    "platform_completion_authorization_not_terminal",
                    "Execution authorization 未能持久化终态。",
                )
            auths.append(auth)
            revocations.append(revocation)
        reservation = await self.worker_registry.get_capacity_reservation(
            dispatch.reservation_id,
            assessed_at=now.isoformat(),
        )
        if reservation is None:
            raise EvolutionRevalidationPlatformCompletionError(
                "platform_completion_reservation_missing",
                "Platform completion 的 capacity reservation 缺失。",
            )
        if reservation.state is WorkerCapacityReservationState.ACTIVE:
            try:
                reservation = await self.worker_registry.release_capacity(
                    reservation_id=dispatch.reservation_id,
                    worker_id=dispatch.worker_id,
                    instance_id=dispatch.worker_instance_id,
                    epoch=dispatch.worker_epoch,
                    reason_code="platform_result_completed",
                    released_at=now.isoformat(),
                    accept_terminal=True,
                )
            except (WorkerRegistryConflictError, WorkerRegistryStoreError) as exc:
                raise EvolutionRevalidationPlatformCompletionError(
                    "platform_completion_capacity_release_failed",
                    "Platform completion 无法释放 Worker capacity。",
                ) from exc
        if reservation.state not in {
            WorkerCapacityReservationState.RELEASED,
            WorkerCapacityReservationState.EXPIRED,
            WorkerCapacityReservationState.FENCED,
        }:
            raise EvolutionRevalidationPlatformCompletionError(
                "platform_completion_capacity_not_terminal",
                "Platform completion 的 Worker capacity 未进入终态。",
            )
        completed_at = max(
            cohort.completed_at,
            *(item.revoked_at for item in revocations),
            reservation.terminal_at or now.isoformat(),
            key=_aware,
        )
        item = _build_completion(
            contract=contract,
            dispatch=dispatch,
            claim=claim,
            authorizations=tuple(auths),
            revocations=tuple(revocations),
            result_receipts=result_receipts,
            cohort=cohort,
            reservation=reservation,
            completed_at=_aware(completed_at),
            key=self._key(),
        )
        stored = await self.store.record(item)
        matrix = await self.matrix_service.inspect(
            contract_id=contract_id,
            assessed_at=stored.completed_at,
        )
        return _view(stored, matrix)

    def _key(self):
        key = self._control_plane_key_provider()
        if not isinstance(key, bytes) or len(key) < 32:
            raise EvolutionRevalidationPlatformCompletionError(
                "platform_completion_control_plane_key_invalid",
                "Platform completion control-plane key 不可用。",
            )
        return key


def _require_complete_result_prefix(receipts, requested_samples):
    cursor = 0
    for item in receipts:
        if item.start_index != cursor:
            break
        cursor += item.sample_count
    if cursor != requested_samples or not receipts or not receipts[-1].full_cohort_received:
        raise EvolutionRevalidationPlatformCompletionError(
            "platform_completion_result_prefix_incomplete",
            f"Platform result prefix 仅完成 {cursor}/{requested_samples}。",
        )


def _base_dependencies_match(item, dispatch, claim, cohort):
    return bool(
        dispatch.dispatch_sha256 == item.dispatch_sha256
        and dispatch.contract_id == item.contract_id
        and dispatch.platform == item.platform
        and dispatch.worker_id == item.worker_id
        and dispatch.worker_instance_id == item.worker_instance_id
        and dispatch.worker_epoch == item.worker_epoch
        and dispatch.reservation_id == item.reservation_id
        and claim.receipt_id == item.final_claim_receipt_id
        and claim.receipt_sha256 == item.final_claim_receipt_sha256
        and claim.identity_id == item.identity_id
        and claim.identity_sha256 == item.identity_sha256
        and cohort.receipt_sha256 == item.cohort_receipt_sha256
        and cohort.contract_sha256 == item.contract_sha256
        and cohort.platform == item.platform
        and cohort.requested_samples == item.requested_samples
        and cohort.sample_receipt_sha256 == item.sample_receipt_sha256
    )


def _result_dependencies_match(item, results, cohort):
    cursor = 0
    pairs = []
    for result in results:
        if result.start_index != cursor:
            return False
        cursor += result.sample_count
        pairs.extend(result.pair_receipt_sha256)
    return bool(
        cursor == item.requested_samples
        and tuple(result.receipt_id for result in results) == item.result_receipt_id
        and tuple(result.receipt_sha256 for result in results)
        == item.result_receipt_sha256
        and tuple(pairs) == item.sample_receipt_sha256
        and tuple(pairs) == cohort.sample_receipt_sha256
    )


def _build_completion(
    *,
    contract,
    dispatch,
    claim,
    authorizations,
    revocations,
    result_receipts,
    cohort,
    reservation,
    completed_at,
    key,
):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_PLATFORM_COMPLETION_POLICY,
        "workspace_root": contract.workspace_root,
        "contract_id": contract.contract_id,
        "contract_sha256": contract.contract_sha256,
        "platform": dispatch.platform,
        "dispatch_id": dispatch.dispatch_id,
        "dispatch_sha256": dispatch.dispatch_sha256,
        "claim_id": claim.claim_id,
        "final_claim_receipt_id": claim.receipt_id,
        "final_claim_receipt_sha256": claim.receipt_sha256,
        "identity_id": claim.identity_id,
        "identity_sha256": claim.identity_sha256,
        "worker_id": dispatch.worker_id,
        "worker_instance_id": dispatch.worker_instance_id,
        "worker_epoch": dispatch.worker_epoch,
        "reservation_id": dispatch.reservation_id,
        "reservation_terminal_state": reservation.state.value,
        "reservation_terminal_reason": reservation.reason_code or reservation.state.value,
        "authorization_id": [item.authorization_id for item in authorizations],
        "authorization_sha256": [item.authorization_sha256 for item in authorizations],
        "authorization_revocation_sha256": [
            item.revocation_sha256 for item in revocations
        ],
        "result_receipt_id": [item.receipt_id for item in result_receipts],
        "result_receipt_sha256": [item.receipt_sha256 for item in result_receipts],
        "requested_samples": contract.requested_samples,
        "accepted_samples": contract.requested_samples,
        "sample_receipt_sha256": list(cohort.sample_receipt_sha256),
        "cohort_receipt_id": cohort.receipt_id,
        "cohort_receipt_sha256": cohort.receipt_sha256,
        "control_plane_attestation_algorithm": "hmac-sha256",
        "result_prefix_verified": True,
        "run_grants_terminal": True,
        "capacity_terminal": True,
        "dispatch_complete": True,
        "cohort_complete": True,
        "matrix_authority": False,
        "comparison_authority": False,
        "promotion_authority": False,
        "completed_at": completed_at.isoformat(),
    }
    digest = _digest(core)
    attestation = hmac.new(key, _canonical(core), hashlib.sha256).hexdigest()
    return EvolutionRevalidationPlatformCompletionReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evrevalplatcomplete_{digest[:24]}",
            "receipt_sha256": digest,
            "control_plane_attestation_sha256": attestation,
        }
    )


def _view(receipt, matrix):
    return EvolutionRevalidationPlatformCompletionView(
        receipt=receipt,
        matrix=matrix,
        platform_complete=True,
        matrix_complete=matrix.matrix_complete,
    )


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Platform completion 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _canonical(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(payload):
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _completion(value):
    return EvolutionRevalidationPlatformCompletionReceipt.model_validate_json(value)


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_platform_completions ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "contract_id TEXT NOT NULL, platform TEXT NOT NULL, dispatch_id TEXT NOT NULL UNIQUE, "
        "cohort_receipt_id TEXT NOT NULL, cohort_receipt_sha256 TEXT NOT NULL, "
        "receipt_json TEXT NOT NULL, completed_at TEXT NOT NULL, "
        "UNIQUE(contract_id, platform))"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_PLATFORM_COMPLETION_POLICY",
    "EvolutionRevalidationPlatformCompletionError",
    "EvolutionRevalidationPlatformCompletionReceipt",
    "EvolutionRevalidationPlatformCompletionService",
    "EvolutionRevalidationPlatformCompletionStore",
    "EvolutionRevalidationPlatformCompletionView",
]
