"""Durable installation-side cursors for stable-promotion observations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_promotion_runtime_admission_deliveries import (
    EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
    EvolutionStablePromotionRuntimeAdmissionDeliveryService,
    EvolutionStablePromotionRuntimeAdmissionSubmission,
    stable_promotion_runtime_admission_receipt_matches_submission,
)
from naumi_agent.evolution.stable_promotion_runtime_admission_delivery_worker import (
    EvolutionStablePromotionRuntimeAdmissionDispatchEvent,
    EvolutionStablePromotionRuntimeAdmissionDispatchStore,
)
from naumi_agent.evolution.stable_promotion_runtime_observation_admissions import (
    EvolutionStablePromotionRuntimeObservationAdmission,
    EvolutionStablePromotionRuntimeObservationAdmissionService,
)
from naumi_agent.harness.runtime_release_observation import (
    HarnessRuntimeReleaseObservation,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

EVOLUTION_STABLE_PROMOTION_OBSERVATION_CHAIN_CURSOR_POLICY = (
    "evolution-stable-promotion-observation-chain-cursor-v1"
)
_ADMISSION_RE = re.compile(r"^evstablepromadmit_[0-9a-f]{24}$")
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_REVISIONS = 5_000
_PAGE_LIMIT = 500
_MAX_CURSOR_BYTES = 2 * 1024 * 1024
_MAX_REVISION_BYTES = 512 * 1024
_ALLOWED_PHASES = {
    "starting",
    "running",
    "waiting",
    "failed",
    "draining",
    "stopped",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionObservationChainRevision(_StrictModel):
    """One deterministic cursor step for one exact HAR observation."""

    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-promotion-observation-chain-cursor-v1"
    ] = EVOLUTION_STABLE_PROMOTION_OBSERVATION_CHAIN_CURSOR_POLICY
    revision_id: str = Field(pattern=r"^evstablepromcursorrev_[0-9a-f]{24}$")
    revision_sha256: str = Field(pattern=_SHA256_RE)
    admission_id: str = Field(pattern=r"^evstablepromadmit_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=_SHA256_RE)
    delivery_receipt_id: str = Field(pattern=r"^evstablepromreceive_[0-9a-f]{24}$")
    delivery_receipt_sha256: str = Field(pattern=_SHA256_RE)
    observation_contract_id: str = Field(pattern=r"^evstablepromobserve_[0-9a-f]{24}$")
    observation_contract_sha256: str = Field(pattern=_SHA256_RE)
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    binding_id: str = Field(pattern=r"^hrreleasebinding_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=_SHA256_RE)
    subject_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,95}$")
    revision_sequence: int = Field(ge=1, le=_MAX_REVISIONS)
    observation: HarnessRuntimeReleaseObservation
    previous_revision_sha256: str = Field(pattern=r"^(|[0-9a-f]{64})$")
    chain_cursor_recorded: Literal[True] = True
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    population_observation_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        sample = self.observation
        if not (
            self.revision_sequence == sample.heartbeat_sequence
            and sample.chain_origin_kind == "startup"
            and sample.chain_origin_sequence == 1
            and sample.binding_id == self.binding_id
            and sample.binding_sha256 == self.binding_sha256
            and sample.subject_id == self.subject_id
            and sample.phase.value in _ALLOWED_PHASES
            and (self.revision_sequence == 1) is (not self.previous_revision_sha256)
        ):
            raise ValueError("稳定推广 observation cursor revision binding 不一致。")
        if self.revision_sequence == 1 and not (
            sample.phase.value == "starting" and not sample.previous_sample_sha256
        ):
            raise ValueError("稳定推广 observation cursor 必须从 startup origin 开始。")
        if self.revision_sequence > 1 and not sample.previous_sample_sha256:
            raise ValueError("稳定推广 observation cursor 非 origin sample 缺少前序摘要。")
        if any(
            (
                self.observation_window_authority,
                self.long_term_metrics_authority,
                self.population_observation_authority,
                self.promoted_outcome_authority,
                self.learning_authority,
                self.promotion_authority,
                self.execution_authority,
            )
        ):
            raise ValueError("稳定推广 observation cursor revision 不得扩张 authority。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"revision_id", "revision_sha256"})
        )
        if not (
            hmac.compare_digest(self.revision_sha256, digest)
            and self.revision_id == f"evstablepromcursorrev_{digest[:24]}"
        ):
            raise ValueError("稳定推广 observation cursor revision identity 不一致。")
        return self


class EvolutionStablePromotionObservationChainCursor(_StrictModel):
    """Latest recoverable position for one acknowledged Runtime Admission."""

    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-stable-promotion-observation-chain-cursor-v1"
    ] = EVOLUTION_STABLE_PROMOTION_OBSERVATION_CHAIN_CURSOR_POLICY
    cursor_id: str = Field(pattern=r"^evstablepromcursor_[0-9a-f]{24}$")
    cursor_sha256: str = Field(pattern=_SHA256_RE)
    admission: EvolutionStablePromotionRuntimeObservationAdmission
    delivery_receipt: EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt
    latest_revision: EvolutionStablePromotionObservationChainRevision
    window_not_before_at: str = Field(min_length=1, max_length=100)
    revision_count: int = Field(ge=1, le=_MAX_REVISIONS)
    after_sequence: int = Field(ge=1, le=_MAX_REVISIONS)
    latest_sample_id: str = Field(pattern=r"^hrreleaseobservation_[0-9a-f]{24}$")
    latest_sample_sha256: str = Field(pattern=_SHA256_RE)
    maximum_sample_count: Literal[5000] = _MAX_REVISIONS
    maximum_page_size: Literal[500] = _PAGE_LIMIT
    chain_cursor_recorded: Literal[True] = True
    signed_page_delivery_authority: Literal[False] = False
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    population_observation_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        admission = self.admission
        receipt = self.delivery_receipt
        revision = self.latest_revision
        _aware(self.window_not_before_at)
        if not (
            receipt.admission_id == admission.admission_id == revision.admission_id
            and receipt.admission_sha256
            == admission.admission_sha256
            == revision.admission_sha256
            and receipt.receipt_id == revision.delivery_receipt_id
            and receipt.receipt_sha256 == revision.delivery_receipt_sha256
            and admission.observation_contract_id == revision.observation_contract_id
            and admission.observation_contract_sha256
            == revision.observation_contract_sha256
            and admission.installation_member_id == revision.installation_member_id
            and admission.binding_id == revision.binding_id
            and admission.binding_sha256 == revision.binding_sha256
            and admission.subject_id == revision.subject_id
            and self.revision_count == self.after_sequence == revision.revision_sequence
            and self.latest_sample_id == revision.observation.sample_id
            and self.latest_sample_sha256 == revision.observation.sample_sha256
            and _aware(self.window_not_before_at)
            <= _aware(revision.observation.observed_at)
        ):
            raise ValueError("稳定推广 observation cursor head binding 不一致。")
        if any(
            (
                self.signed_page_delivery_authority,
                self.observation_window_authority,
                self.long_term_metrics_authority,
                self.population_observation_authority,
                self.promoted_outcome_authority,
                self.learning_authority,
                self.promotion_authority,
                self.execution_authority,
            )
        ):
            raise ValueError("稳定推广 observation cursor 不得扩张 authority。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"cursor_id", "cursor_sha256"})
        )
        if not (
            hmac.compare_digest(self.cursor_sha256, digest)
            and self.cursor_id == f"evstablepromcursor_{digest[:24]}"
        ):
            raise ValueError("稳定推广 observation cursor identity 不一致。")
        return self


class EvolutionStablePromotionObservationChainCursorView(_StrictModel):
    cursor: EvolutionStablePromotionObservationChainCursor
    status: Literal["ready", "stale"]
    durable_cursor_valid: bool
    admission_delivery_acknowledged: bool
    remote_admission_delivery_authority: bool
    runtime_admission_authority: bool
    observation_contract_authority: bool
    release_binding_current: bool
    observation_chain_source_current: bool
    cursor_delivery_input_authority: bool
    signed_page_delivery_authority: Literal[False] = False
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    population_observation_authority: Literal[False] = False
    promoted_outcome_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_cursor_valid
            and self.admission_delivery_acknowledged
            and self.remote_admission_delivery_authority
            and self.runtime_admission_authority
            and self.observation_contract_authority
            and self.release_binding_current
            and self.observation_chain_source_current
        )
        if not (
            self.cursor_delivery_input_authority is expected
            and (self.status == "ready") is expected
        ):
            raise ValueError("稳定推广 observation cursor authority projection 不一致。")
        return self


class EvolutionStablePromotionObservationChainCursorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePromotionObservationChainCursorStore:
    """Append-only revisions plus one content-addressed latest cursor head."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        admission_service: EvolutionStablePromotionRuntimeObservationAdmissionService,
        dispatch_store: EvolutionStablePromotionRuntimeAdmissionDispatchStore,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not (
            admission_service.store.db_path == self.db_path
            and dispatch_store.db_path == self.db_path
        ):
            raise ValueError("稳定推广 observation cursor 必须与 Admission/Dispatch 共用 SQLite。")
        self.admission_service = admission_service
        self.dispatch_store = dispatch_store

    async def get(
        self, admission_id: str
    ) -> EvolutionStablePromotionObservationChainCursor | None:
        item_id = _admission_id(admission_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await _cursor_row(db, item_id)
                if row is None:
                    return None
                cursor = _cursor_from_row(row)
                revisions = await _revision_rows(db, item_id)
                _verify_cursor_chain(cursor, revisions)
                return cursor
        except EvolutionStablePromotionObservationChainCursorError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionObservationChainCursorError(
                "stable_promotion_observation_cursor_store_corrupt",
                "稳定推广 observation cursor 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        *,
        submission: EvolutionStablePromotionRuntimeAdmissionSubmission,
        receipt: EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
        observations: tuple[HarnessRuntimeReleaseObservation, ...],
        window_not_before_at: str,
    ) -> EvolutionStablePromotionObservationChainCursor:
        item = EvolutionStablePromotionRuntimeAdmissionSubmission.model_validate(submission)
        ack = EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt.model_validate(receipt)
        samples = tuple(
            HarnessRuntimeReleaseObservation.model_validate(value)
            for value in observations
        )
        not_before = _aware(window_not_before_at).isoformat()
        if not stable_promotion_runtime_admission_receipt_matches_submission(ack, item):
            raise EvolutionStablePromotionObservationChainCursorError(
                "stable_promotion_observation_cursor_receipt_mismatch",
                "Runtime Admission Receipt 与 Submission 不匹配。",
            )
        if not 1 <= len(samples) <= _PAGE_LIMIT:
            raise EvolutionStablePromotionObservationChainCursorError(
                "stable_promotion_observation_cursor_page_invalid",
                "observation cursor 每次必须接受 1–500 个 sample。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_acknowledged_sources(db, item, ack)
                row = await _cursor_row(db, item.payload.admission.admission_id)
                existing = None if row is None else _cursor_from_row(row)
                revision_rows = await _revision_rows(
                    db, item.payload.admission.admission_id
                )
                if existing is not None:
                    _verify_cursor_chain(existing, revision_rows)
                revisions = [
                    _restore_revision(str(value["revision_json"]))
                    for value in revision_rows
                ]
                by_sequence = {value.revision_sequence: value for value in revisions}
                latest = revisions[-1] if revisions else None
                for sample in samples:
                    expected = _build_revision(
                        admission=item.payload.admission,
                        receipt=ack,
                        observation=sample,
                        previous_revision=(
                            by_sequence.get(sample.heartbeat_sequence - 1)
                            if sample.heartbeat_sequence > 1
                            else None
                        ),
                        window_not_before_at=not_before,
                    )
                    known = by_sequence.get(sample.heartbeat_sequence)
                    if known is not None:
                        if known != expected:
                            await db.rollback()
                            raise EvolutionStablePromotionObservationChainCursorError(
                                "stable_promotion_observation_cursor_revision_conflict",
                                "同一 heartbeat sequence 已绑定不同 cursor revision。",
                            )
                        continue
                    next_sequence = 1 if latest is None else latest.revision_sequence + 1
                    if sample.heartbeat_sequence != next_sequence:
                        await db.rollback()
                        raise EvolutionStablePromotionObservationChainCursorError(
                            "stable_promotion_observation_cursor_gap",
                            "observation cursor 不能跳过 heartbeat sequence。",
                        )
                    if next_sequence > _MAX_REVISIONS:
                        await db.rollback()
                        raise EvolutionStablePromotionObservationChainCursorError(
                            "stable_promotion_observation_cursor_limit_reached",
                            "observation cursor 已达到 5000 sample 上限。",
                        )
                    encoded = expected.model_dump_json()
                    if len(encoded.encode()) > _MAX_REVISION_BYTES:
                        raise EvolutionStablePromotionObservationChainCursorError(
                            "stable_promotion_observation_cursor_revision_oversized",
                            "observation cursor revision 超过 512 KiB。",
                        )
                    await db.execute(
                        "INSERT INTO evolution_stable_promotion_observation_cursor_revisions "
                        "(revision_id, admission_id, heartbeat_sequence, revision_json) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            expected.revision_id,
                            expected.admission_id,
                            expected.revision_sequence,
                            encoded,
                        ),
                    )
                    by_sequence[expected.revision_sequence] = expected
                    latest = expected
                if latest is None:
                    await db.rollback()
                    raise EvolutionStablePromotionObservationChainCursorError(
                        "stable_promotion_observation_cursor_samples_missing",
                        "observation cursor 缺少可记录 sample。",
                    )
                cursor = _build_cursor(
                    admission=item.payload.admission,
                    receipt=ack,
                    latest_revision=latest,
                    window_not_before_at=not_before,
                )
                encoded_cursor = cursor.model_dump_json()
                if len(encoded_cursor.encode()) > _MAX_CURSOR_BYTES:
                    raise EvolutionStablePromotionObservationChainCursorError(
                        "stable_promotion_observation_cursor_oversized",
                        "observation cursor 超过 2 MiB。",
                    )
                await db.execute(
                    "INSERT INTO evolution_stable_promotion_observation_cursors "
                    "(admission_id, cursor_id, cursor_json) VALUES (?, ?, ?) "
                    "ON CONFLICT(admission_id) DO UPDATE SET "
                    "cursor_id = excluded.cursor_id, cursor_json = excluded.cursor_json",
                    (cursor.admission.admission_id, cursor.cursor_id, encoded_cursor),
                )
                await db.commit()
                return cursor
        except EvolutionStablePromotionObservationChainCursorError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePromotionObservationChainCursorError(
                "stable_promotion_observation_cursor_store_failed",
                "稳定推广 observation cursor 无法持久化。",
            ) from exc

    async def revisions(
        self, admission_id: str
    ) -> tuple[EvolutionStablePromotionObservationChainRevision, ...]:
        cursor = await self.get(admission_id)
        if cursor is None:
            return ()
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            rows = await _revision_rows(db, cursor.admission.admission_id)
        revisions = tuple(_restore_revision(str(row["revision_json"])) for row in rows)
        _verify_cursor_chain(cursor, rows)
        return revisions


class EvolutionStablePromotionObservationChainCursorService:
    """Advance a cursor only from the installation's authoritative HAR ledger."""

    def __init__(
        self,
        *,
        admission_service: EvolutionStablePromotionRuntimeObservationAdmissionService,
        delivery_service: EvolutionStablePromotionRuntimeAdmissionDeliveryService,
        dispatch_store: EvolutionStablePromotionRuntimeAdmissionDispatchStore,
        harness_store: HarnessStore,
        store: EvolutionStablePromotionObservationChainCursorStore,
    ) -> None:
        if not (
            store.admission_service is admission_service
            and store.dispatch_store is dispatch_store
            and admission_service.harness_store is harness_store
            and delivery_service.admission_service is admission_service
            and delivery_service.store.db_path == store.db_path
        ):
            raise ValueError("稳定推广 observation cursor authority composition 不一致。")
        self.admission_service = admission_service
        self.delivery_service = delivery_service
        self.dispatch_store = dispatch_store
        self.harness_store = harness_store
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def advance(
        self, *, admission_id: str
    ) -> EvolutionStablePromotionObservationChainCursorView:
        item_id = _admission_id(admission_id)
        lock = self._locks.setdefault(item_id, asyncio.Lock())
        async with lock:
            dispatch = await self.dispatch_store.get(item_id)
            if dispatch is None or dispatch.receipt is None or not (
                dispatch.latest_event.state == "acknowledged"
                and stable_promotion_runtime_admission_receipt_matches_submission(
                    dispatch.receipt, dispatch.submission
                )
            ):
                raise EvolutionStablePromotionObservationChainCursorError(
                    "stable_promotion_observation_cursor_admission_not_acknowledged",
                    "Runtime Admission 尚未获得 Control Plane Receipt ACK。",
                )
            admission = dispatch.submission.payload.admission
            delivery_view = await self.delivery_service.inspect(
                submission=dispatch.submission,
                receipt=dispatch.receipt,
            )
            if not delivery_view.remote_admission_delivery_authority:
                raise EvolutionStablePromotionObservationChainCursorError(
                    "stable_promotion_observation_cursor_delivery_stale",
                    "Control Plane Runtime Admission Receipt authority 已失效。",
                )
            admission_view = await self.admission_service.inspect(admission=admission)
            if not admission_view.runtime_observation_input_authority:
                raise EvolutionStablePromotionObservationChainCursorError(
                    "stable_promotion_observation_cursor_admission_stale",
                    "Runtime Admission authority 已失效，不能推进 observation cursor。",
                )
            contract = await self.admission_service.contract_store.get_by_finalization(
                admission.population_finalization_receipt_id
            )
            if contract is None or not (
                contract.contract_id == admission.observation_contract_id
                and contract.contract_sha256 == admission.observation_contract_sha256
            ):
                raise EvolutionStablePromotionObservationChainCursorError(
                    "stable_promotion_observation_cursor_contract_changed",
                    "稳定推广观察契约已变化或不存在。",
                )
            current = await self.store.get(item_id)
            after_sequence = 0 if current is None else current.after_sequence
            if after_sequence >= contract.maximum_sample_count:
                return await self.inspect(admission_id=item_id)
            page = await self.harness_store.list_runtime_release_observations(
                workspace_root=admission.workspace_root,
                subject_id=admission.subject_id,
                after_sequence=after_sequence,
                limit=min(
                    _PAGE_LIMIT,
                    contract.maximum_sample_count - after_sequence,
                ),
            )
            if page is None or not (
                page.binding_id == admission.binding_id
                and page.binding_sha256 == admission.binding_sha256
            ):
                raise EvolutionStablePromotionObservationChainCursorError(
                    "stable_promotion_observation_cursor_binding_mismatch",
                    "HAR observation page 未绑定 exact Runtime Binding。",
                )
            if not page.items:
                if current is None:
                    raise EvolutionStablePromotionObservationChainCursorError(
                        "stable_promotion_observation_cursor_origin_missing",
                        "HAR observation ledger 缺少 startup origin。",
                    )
                return await self.inspect(admission_id=item_id)
            _validate_source_page(
                admission=admission,
                observations=page.items,
                after_sequence=after_sequence,
                window_not_before_at=contract.window_not_before_at,
                previous_sample=(
                    None if current is None else current.latest_revision.observation
                ),
            )
            await self.store.record(
                submission=dispatch.submission,
                receipt=dispatch.receipt,
                observations=page.items,
                window_not_before_at=contract.window_not_before_at,
            )
            return await self.inspect(admission_id=item_id)

    async def inspect(
        self, *, admission_id: str
    ) -> EvolutionStablePromotionObservationChainCursorView:
        item_id = _admission_id(admission_id)
        cursor = await self.store.get(item_id)
        if cursor is None:
            raise EvolutionStablePromotionObservationChainCursorError(
                "stable_promotion_observation_cursor_missing",
                "稳定推广 observation cursor 不存在。",
            )
        durable = acknowledged = delivery_authority = False
        admission_authority = contract_authority = False
        binding_current = chain_current = False
        try:
            revisions = await self.store.revisions(item_id)
            durable = bool(revisions and revisions[-1] == cursor.latest_revision)
        except (OSError, RuntimeError, TypeError, ValueError):
            revisions = ()
        try:
            dispatch = await self.dispatch_store.get(item_id)
            acknowledged = bool(
                dispatch is not None
                and dispatch.receipt == cursor.delivery_receipt
                and dispatch.submission.payload.admission == cursor.admission
                and dispatch.latest_event.state == "acknowledged"
            )
            if acknowledged:
                delivery_view = await self.delivery_service.inspect(
                    submission=dispatch.submission,
                    receipt=dispatch.receipt,
                )
                delivery_authority = (
                    delivery_view.remote_admission_delivery_authority
                )
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        try:
            view = await self.admission_service.inspect(admission=cursor.admission)
            admission_authority = view.runtime_observation_input_authority
            contract = await self.admission_service.contract_store.get_by_finalization(
                cursor.admission.population_finalization_receipt_id
            )
            contract_authority = bool(
                contract is not None
                and contract.contract_id == cursor.admission.observation_contract_id
                and contract.contract_sha256
                == cursor.admission.observation_contract_sha256
                and _aware(contract.window_not_before_at).isoformat()
                == cursor.window_not_before_at
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        try:
            binding = await self.harness_store.get_runtime_release_binding(
                workspace_root=cursor.admission.workspace_root,
                subject_id=cursor.admission.subject_id,
            )
            binding_current = bool(
                binding is not None
                and binding.binding_id == cursor.admission.binding_id
                and binding.binding_sha256 == cursor.admission.binding_sha256
            )
            actual = await _read_exact_revisions(
                harness_store=self.harness_store,
                cursor=cursor,
                expected=revisions,
            )
            binding_after = await self.harness_store.get_runtime_release_binding(
                workspace_root=cursor.admission.workspace_root,
                subject_id=cursor.admission.subject_id,
            )
            chain_current = bool(
                actual == tuple(value.observation for value in revisions)
                and binding_after == binding
            )
        except (HarnessStoreError, OSError, RuntimeError, TypeError, ValueError):
            pass
        ready = bool(
            durable
            and acknowledged
            and delivery_authority
            and admission_authority
            and contract_authority
            and binding_current
            and chain_current
        )
        return EvolutionStablePromotionObservationChainCursorView(
            cursor=cursor,
            status="ready" if ready else "stale",
            durable_cursor_valid=durable,
            admission_delivery_acknowledged=acknowledged,
            remote_admission_delivery_authority=delivery_authority,
            runtime_admission_authority=admission_authority,
            observation_contract_authority=contract_authority,
            release_binding_current=binding_current,
            observation_chain_source_current=chain_current,
            cursor_delivery_input_authority=ready,
        )


def render_stable_promotion_observation_chain_cursor(
    view: EvolutionStablePromotionObservationChainCursorView,
) -> str:
    item = EvolutionStablePromotionObservationChainCursorView.model_validate(view)
    cursor = item.cursor
    sample = cursor.latest_revision.observation
    lines = [
        "### 稳定推广 Observation Chain Cursor",
        "",
        f"- 状态：`{item.status}`",
        f"- Cursor：`{cursor.cursor_id}`",
        f"- Runtime Admission：`{cursor.admission.admission_id}`",
        f"- Control Plane Receipt：`{cursor.delivery_receipt.receipt_id}`",
        f"- Installation member：`{cursor.admission.installation_member_id}`",
        f"- Runtime subject：`{cursor.admission.subject_id}` / `{cursor.admission.surface}`",
        f"- 已收集：`{cursor.revision_count}/{cursor.maximum_sample_count}` samples",
        f"- Cursor sequence：`{cursor.after_sequence}`",
        f"- 最新 phase：`{sample.phase.value}` @ `{sample.observed_at}`",
        f"- ACK authority：`{str(item.admission_delivery_acknowledged).lower()}`",
        f"- Remote Admission current：`{str(item.remote_admission_delivery_authority).lower()}`",
        f"- HAR chain current：`{str(item.observation_chain_source_current).lower()}`",
        f"- Cursor delivery input：`{str(item.cursor_delivery_input_authority).lower()}`",
        "- Signed page delivery authority：`false`",
        "- Observation window / Population / Promoted Outcome authority：`false`",
        "",
        "该 Cursor 只冻结 installation 本地 HAR 账本进度；尚未把 sample "
        "签名发送到 Control Plane，也不形成长期健康结论。",
    ]
    return "\n".join(lines)


def _build_revision(
    *,
    admission: EvolutionStablePromotionRuntimeObservationAdmission,
    receipt: EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
    observation: HarnessRuntimeReleaseObservation,
    previous_revision: EvolutionStablePromotionObservationChainRevision | None,
    window_not_before_at: str,
) -> EvolutionStablePromotionObservationChainRevision:
    if _aware(observation.observed_at) < _aware(window_not_before_at):
        raise EvolutionStablePromotionObservationChainCursorError(
            "stable_promotion_observation_cursor_predates_window",
            "Runtime observation 早于观察契约 window_not_before_at。",
        )
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_PROMOTION_OBSERVATION_CHAIN_CURSOR_POLICY,
        "admission_id": admission.admission_id,
        "admission_sha256": admission.admission_sha256,
        "delivery_receipt_id": receipt.receipt_id,
        "delivery_receipt_sha256": receipt.receipt_sha256,
        "observation_contract_id": admission.observation_contract_id,
        "observation_contract_sha256": admission.observation_contract_sha256,
        "installation_member_id": admission.installation_member_id,
        "binding_id": admission.binding_id,
        "binding_sha256": admission.binding_sha256,
        "subject_id": admission.subject_id,
        "revision_sequence": observation.heartbeat_sequence,
        "observation": observation,
        "previous_revision_sha256": (
            "" if previous_revision is None else previous_revision.revision_sha256
        ),
        "chain_cursor_recorded": True,
        "observation_window_authority": False,
        "long_term_metrics_authority": False,
        "population_observation_authority": False,
        "promoted_outcome_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionStablePromotionObservationChainRevision.model_validate(
        {
            **core,
            "revision_id": f"evstablepromcursorrev_{digest[:24]}",
            "revision_sha256": digest,
        }
    )


def _build_cursor(
    *,
    admission: EvolutionStablePromotionRuntimeObservationAdmission,
    receipt: EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
    latest_revision: EvolutionStablePromotionObservationChainRevision,
    window_not_before_at: str,
) -> EvolutionStablePromotionObservationChainCursor:
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_PROMOTION_OBSERVATION_CHAIN_CURSOR_POLICY,
        "admission": admission,
        "delivery_receipt": receipt,
        "latest_revision": latest_revision,
        "window_not_before_at": _aware(window_not_before_at).isoformat(),
        "revision_count": latest_revision.revision_sequence,
        "after_sequence": latest_revision.revision_sequence,
        "latest_sample_id": latest_revision.observation.sample_id,
        "latest_sample_sha256": latest_revision.observation.sample_sha256,
        "maximum_sample_count": _MAX_REVISIONS,
        "maximum_page_size": _PAGE_LIMIT,
        "chain_cursor_recorded": True,
        "signed_page_delivery_authority": False,
        "observation_window_authority": False,
        "long_term_metrics_authority": False,
        "population_observation_authority": False,
        "promoted_outcome_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
    }
    digest = _digest(core)
    return EvolutionStablePromotionObservationChainCursor.model_validate(
        {
            **core,
            "cursor_id": f"evstablepromcursor_{digest[:24]}",
            "cursor_sha256": digest,
        }
    )


def _validate_source_page(
    *,
    admission: EvolutionStablePromotionRuntimeObservationAdmission,
    observations: tuple[HarnessRuntimeReleaseObservation, ...],
    after_sequence: int,
    window_not_before_at: str,
    previous_sample: HarnessRuntimeReleaseObservation | None,
) -> None:
    previous = previous_sample
    for offset, sample in enumerate(observations, start=1):
        expected_sequence = after_sequence + offset
        if not (
            sample.workspace_root == admission.workspace_root
            and sample.binding_id == admission.binding_id
            and sample.binding_sha256 == admission.binding_sha256
            and sample.runtime_identity_id == admission.runtime_identity_id
            and sample.runtime_identity_sha256 == admission.runtime_identity_sha256
            and sample.surface == admission.surface
            and sample.subject_id == admission.subject_id
            and sample.instance_id == admission.instance_id
            and sample.epoch == admission.epoch
            and sample.chain_origin_kind == "startup"
            and sample.chain_origin_sequence == 1
            and sample.heartbeat_sequence == expected_sequence
            and sample.phase.value in _ALLOWED_PHASES
            and (expected_sequence == 1 or sample.phase.value != "starting")
            and _aware(sample.observed_at) >= _aware(window_not_before_at)
        ):
            raise EvolutionStablePromotionObservationChainCursorError(
                "stable_promotion_observation_cursor_sample_mismatch",
                "HAR observation sample 与 Runtime Admission/Contract 不匹配。",
            )
        if expected_sequence == 1 and not (
            sample.sample_id == admission.origin_sample_id
            and sample.sample_sha256 == admission.origin_sample_sha256
            and sample.phase.value == admission.origin_phase
            and sample.observed_at == admission.origin_observed_at
            and not sample.previous_sample_sha256
        ):
            raise EvolutionStablePromotionObservationChainCursorError(
                "stable_promotion_observation_cursor_origin_mismatch",
                "HAR observation chain origin 与 Runtime Admission 不一致。",
            )
        if previous is not None and not (
            sample.previous_sample_sha256 == previous.sample_sha256
            and _aware(sample.observed_at) >= _aware(previous.observed_at)
        ):
            raise EvolutionStablePromotionObservationChainCursorError(
                "stable_promotion_observation_cursor_chain_mismatch",
                "HAR observation sequence、hash 或时间链不连续。",
            )
        previous = sample


async def _read_exact_revisions(
    *,
    harness_store: HarnessStore,
    cursor: EvolutionStablePromotionObservationChainCursor,
    expected: tuple[EvolutionStablePromotionObservationChainRevision, ...],
) -> tuple[HarnessRuntimeReleaseObservation, ...]:
    actual: list[HarnessRuntimeReleaseObservation] = []
    after_sequence = 0
    while len(actual) < len(expected):
        page = await harness_store.list_runtime_release_observations(
            workspace_root=cursor.admission.workspace_root,
            subject_id=cursor.admission.subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, len(expected) - len(actual)),
        )
        if page is None or not page.items:
            break
        actual.extend(page.items)
        new_after = page.items[-1].heartbeat_sequence
        if new_after <= after_sequence:
            raise EvolutionStablePromotionObservationChainCursorError(
                "stable_promotion_observation_cursor_source_stalled",
                "HAR observation cursor 未前进。",
            )
        after_sequence = new_after
    return tuple(actual)


async def _require_acknowledged_sources(
    db: aiosqlite.Connection,
    submission: EvolutionStablePromotionRuntimeAdmissionSubmission,
    receipt: EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
) -> None:
    admission = submission.payload.admission
    admission_row = await (
        await db.execute(
            "SELECT admission_json FROM evolution_stable_promotion_runtime_admissions "
            "WHERE admission_id = ?",
            (admission.admission_id,),
        )
    ).fetchone()
    dispatch_row = await (
        await db.execute(
            "SELECT submission_json, latest_event_json, receipt_json FROM "
            "evolution_stable_promotion_runtime_admission_dispatches "
            "WHERE admission_id = ?",
            (admission.admission_id,),
        )
    ).fetchone()
    if admission_row is None or dispatch_row is None:
        raise EvolutionStablePromotionObservationChainCursorError(
            "stable_promotion_observation_cursor_source_missing",
            "Runtime Admission 或 acknowledged dispatch durable source 不存在。",
        )
    try:
        durable_admission = EvolutionStablePromotionRuntimeObservationAdmission.model_validate_json(
            admission_row["admission_json"]
        )
        durable_submission = EvolutionStablePromotionRuntimeAdmissionSubmission.model_validate_json(
            dispatch_row["submission_json"]
        )
        event = EvolutionStablePromotionRuntimeAdmissionDispatchEvent.model_validate_json(
            dispatch_row["latest_event_json"]
        )
        durable_receipt = (
            EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt.model_validate_json(
                dispatch_row["receipt_json"]
            )
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationChainCursorError(
            "stable_promotion_observation_cursor_source_corrupt",
            "Runtime Admission acknowledged durable source 损坏。",
        ) from exc
    if not (
        durable_admission == admission
        and durable_submission == submission
        and durable_receipt == receipt
        and event.state == "acknowledged"
        and event.receipt_id == receipt.receipt_id
        and event.receipt_sha256 == receipt.receipt_sha256
    ):
        raise EvolutionStablePromotionObservationChainCursorError(
            "stable_promotion_observation_cursor_source_changed",
            "Runtime Admission acknowledged durable source 已变化。",
        )


def _verify_cursor_chain(
    cursor: EvolutionStablePromotionObservationChainCursor,
    rows: list[aiosqlite.Row],
) -> None:
    revisions = [_restore_revision(str(row["revision_json"])) for row in rows]
    if not (
        len(revisions) == cursor.revision_count
        and revisions
        and revisions[-1] == cursor.latest_revision
    ):
        raise EvolutionStablePromotionObservationChainCursorError(
            "stable_promotion_observation_cursor_chain_corrupt",
            "observation cursor revision 数量或 head 不一致。",
        )
    previous_revision = None
    previous_sample = None
    for sequence, revision in enumerate(revisions, start=1):
        if not (
            rows[sequence - 1]["revision_id"] == revision.revision_id
            and rows[sequence - 1]["admission_id"] == revision.admission_id
            and rows[sequence - 1]["heartbeat_sequence"]
            == revision.revision_sequence
            and revision.revision_sequence == sequence
            and revision.admission_id == cursor.admission.admission_id
            and revision.delivery_receipt_id == cursor.delivery_receipt.receipt_id
            and revision.previous_revision_sha256
            == ("" if previous_revision is None else previous_revision.revision_sha256)
            and revision.observation.previous_sample_sha256
            == ("" if previous_sample is None else previous_sample.sample_sha256)
        ):
            raise EvolutionStablePromotionObservationChainCursorError(
                "stable_promotion_observation_cursor_chain_corrupt",
                "observation cursor revision/hash chain 断裂。",
            )
        previous_revision = revision
        previous_sample = revision.observation


def _cursor_from_row(
    row: aiosqlite.Row,
) -> EvolutionStablePromotionObservationChainCursor:
    cursor = _restore_cursor(str(row["cursor_json"]))
    if not (
        row["admission_id"] == cursor.admission.admission_id
        and row["cursor_id"] == cursor.cursor_id
    ):
        raise EvolutionStablePromotionObservationChainCursorError(
            "stable_promotion_observation_cursor_store_corrupt",
            "稳定推广 observation cursor row identity 不一致。",
        )
    return cursor


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_observation_cursors ("
        "admission_id TEXT PRIMARY KEY, cursor_id TEXT NOT NULL UNIQUE, "
        "cursor_json TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_stable_promotion_observation_cursor_revisions ("
        "revision_id TEXT PRIMARY KEY, admission_id TEXT NOT NULL, "
        "heartbeat_sequence INTEGER NOT NULL, revision_json TEXT NOT NULL, "
        "UNIQUE(admission_id, heartbeat_sequence))"
    )


async def _cursor_row(db: aiosqlite.Connection, admission_id: str):
    return await (
        await db.execute(
            "SELECT * FROM evolution_stable_promotion_observation_cursors "
            "WHERE admission_id = ?",
            (admission_id,),
        )
    ).fetchone()


async def _revision_rows(db: aiosqlite.Connection, admission_id: str):
    return await (
        await db.execute(
            "SELECT * FROM evolution_stable_promotion_observation_cursor_revisions "
            "WHERE admission_id = ? ORDER BY heartbeat_sequence",
            (admission_id,),
        )
    ).fetchall()


def _restore_cursor(raw: str) -> EvolutionStablePromotionObservationChainCursor:
    try:
        if len(raw.encode()) > _MAX_CURSOR_BYTES:
            raise ValueError("cursor oversized")
        return EvolutionStablePromotionObservationChainCursor.model_validate_json(raw)
    except (TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationChainCursorError(
            "stable_promotion_observation_cursor_store_corrupt",
            "稳定推广 observation cursor artifact 损坏。",
        ) from exc


def _restore_revision(raw: str) -> EvolutionStablePromotionObservationChainRevision:
    try:
        if len(raw.encode()) > _MAX_REVISION_BYTES:
            raise ValueError("revision oversized")
        return EvolutionStablePromotionObservationChainRevision.model_validate_json(raw)
    except (TypeError, ValueError) as exc:
        raise EvolutionStablePromotionObservationChainCursorError(
            "stable_promotion_observation_cursor_store_corrupt",
            "稳定推广 observation cursor revision 损坏。",
        ) from exc


def _admission_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _ADMISSION_RE.fullmatch(normalized) is None:
        raise ValueError("稳定推广 Runtime Admission ID 无效。")
    return normalized


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not (
        isinstance(parsed, datetime)
        and parsed.tzinfo is not None
        and parsed.utcoffset() is not None
    ):
        raise ValueError("稳定推广 observation cursor timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: item.model_dump(mode="json"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_PROMOTION_OBSERVATION_CHAIN_CURSOR_POLICY",
    "EvolutionStablePromotionObservationChainCursor",
    "EvolutionStablePromotionObservationChainCursorError",
    "EvolutionStablePromotionObservationChainCursorService",
    "EvolutionStablePromotionObservationChainCursorStore",
    "EvolutionStablePromotionObservationChainCursorView",
    "EvolutionStablePromotionObservationChainRevision",
    "render_stable_promotion_observation_chain_cursor",
]
