"""Fenced automatic delivery for signed stable-promotion observation revisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_promotion_observation_revision_deliveries import (
    EvolutionStablePromotionObservationRevisionDeliveryError,
    EvolutionStablePromotionObservationRevisionDeliveryReceipt,
    EvolutionStablePromotionObservationRevisionDeliveryService,
    EvolutionStablePromotionObservationRevisionSubmission,
    stable_promotion_observation_revision_receipt_matches_submission,
)

_SHA256_RE = r"^[0-9a-f]{64}$"
_ADMISSION_RE = r"^evstablepromadmit_[0-9a-f]{24}$"
_SUBMISSION_RE = r"^evstablepromrevsubmit_[0-9a-f]{24}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionObservationRevisionDispatchEvent(_StrictModel):
    schema_version: Literal[1] = 1
    event_id: str = Field(pattern=r"^evstablepromrevdispatch_[0-9a-f]{24}$")
    event_sha256: str = Field(pattern=_SHA256_RE)
    admission_id: str = Field(pattern=_ADMISSION_RE)
    submission_id: str = Field(pattern=_SUBMISSION_RE)
    submission_sha256: str = Field(pattern=_SHA256_RE)
    first_sequence: int = Field(ge=1, le=5_000)
    last_sequence: int = Field(ge=1, le=5_000)
    sequence: int = Field(ge=1, le=1_000_000)
    state: Literal["queued", "in_flight", "acknowledged", "dead_letter"]
    owner_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    claim_epoch: int = Field(ge=0, le=1_000_000)
    attempt_count: int = Field(ge=0, le=1_000_000)
    lease_expires_at: str | None = Field(default=None, min_length=1, max_length=100)
    next_attempt_at: str = Field(min_length=1, max_length=100)
    receipt_id: str | None = Field(
        default=None, pattern=r"^evstablepromrevreceive_[0-9a-f]{24}$"
    )
    receipt_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    failure_code: str | None = Field(default=None, min_length=1, max_length=128)
    occurred_at: str = Field(min_length=1, max_length=100)
    previous_event_sha256: str | None = Field(default=None, pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.next_attempt_at)
        _aware(self.occurred_at)
        if self.lease_expires_at is not None:
            _aware(self.lease_expires_at)
        in_flight = self.state == "in_flight"
        if in_flight is not bool(self.owner_sha256 and self.lease_expires_at):
            raise ValueError("Observation revision in-flight owner/lease 不一致。")
        has_receipt = self.receipt_id is not None and self.receipt_sha256 is not None
        if (self.state == "acknowledged") is not has_receipt:
            raise ValueError("Observation revision acknowledged event Receipt 不一致。")
        if self.state == "dead_letter" and self.failure_code is None:
            raise ValueError("Observation revision dead-letter 缺少 failure code。")
        if self.state not in {"queued", "dead_letter"} and self.failure_code is not None:
            raise ValueError("仅 retry/dead-letter event 可携带 failure code。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"event_id", "event_sha256"})
        )
        if not (
            self.event_sha256 == digest
            and self.event_id == f"evstablepromrevdispatch_{digest[:24]}"
        ):
            raise ValueError("Observation revision dispatch event identity 不一致。")
        return self


class EvolutionStablePromotionObservationRevisionDispatchView(_StrictModel):
    submission: EvolutionStablePromotionObservationRevisionSubmission
    latest_event: EvolutionStablePromotionObservationRevisionDispatchEvent
    receipt: EvolutionStablePromotionObservationRevisionDeliveryReceipt | None = None

    @model_validator(mode="after")
    def _binding(self) -> Self:
        payload = self.submission.payload
        event = self.latest_event
        if not (
            event.admission_id == payload.admission_id
            and event.submission_id == self.submission.submission_id
            and event.submission_sha256 == self.submission.submission_sha256
            and event.first_sequence == payload.first_sequence
            and event.last_sequence == payload.last_sequence
        ):
            raise ValueError("Observation revision dispatch 与 Submission 不一致。")
        if self.receipt is not None and not (
            event.state == "acknowledged"
            and event.receipt_id == self.receipt.receipt_id
            and event.receipt_sha256 == self.receipt.receipt_sha256
            and stable_promotion_observation_revision_receipt_matches_submission(
                self.receipt, self.submission
            )
        ):
            raise ValueError("Observation revision dispatch Receipt binding 无效。")
        if event.state == "acknowledged" and self.receipt is None:
            raise ValueError("acknowledged observation revision dispatch 缺少 Receipt。")
        return self


class EvolutionStablePromotionObservationRevisionTransportError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        normalized = _failure_code(code)
        self.code = normalized
        self.retryable = bool(retryable)


class EvolutionStablePromotionObservationRevisionDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@runtime_checkable
class EvolutionStablePromotionObservationRevisionControlPlaneTransport(Protocol):
    async def submit(
        self, submission: EvolutionStablePromotionObservationRevisionSubmission
    ) -> EvolutionStablePromotionObservationRevisionDeliveryReceipt: ...


class LocalStablePromotionObservationRevisionControlPlaneTransport:
    """Real in-process adapter for the 7b3b1 Control Plane receive boundary."""

    def __init__(
        self,
        *,
        receiver: EvolutionStablePromotionObservationRevisionDeliveryService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.receiver = receiver
        self.clock = clock

    async def submit(
        self, submission: EvolutionStablePromotionObservationRevisionSubmission
    ) -> EvolutionStablePromotionObservationRevisionDeliveryReceipt:
        try:
            view = await self.receiver.receive(
                submission=submission,
                received_at=_aware(self.clock()),
            )
        except EvolutionStablePromotionObservationRevisionDeliveryError as exc:
            permanent = exc.code in {
                "stable_promotion_observation_revision_admission_mismatch",
                "stable_promotion_observation_revision_signature_untrusted",
                "stable_promotion_observation_revision_remote_head_conflict",
                "stable_promotion_observation_revision_received_before_signed",
                "stable_promotion_observation_revision_submission_invalid",
            }
            raise EvolutionStablePromotionObservationRevisionTransportError(
                exc.code,
                str(exc),
                retryable=not permanent,
            ) from exc
        if not view.remote_revision_delivery_authority:
            raise EvolutionStablePromotionObservationRevisionTransportError(
                "stable_promotion_observation_revision_receipt_stale",
                "Control Plane Receipt 不具备 current revision delivery authority。",
                retryable=False,
            )
        return view.receipt


class EvolutionStablePromotionObservationRevisionDispatchStore:
    """Append-only owner/epoch/lease dispatch journal for every signed batch."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def enqueue(
        self,
        submission: EvolutionStablePromotionObservationRevisionSubmission,
        *,
        enqueued_at: str | datetime,
    ) -> EvolutionStablePromotionObservationRevisionDispatchView:
        item = EvolutionStablePromotionObservationRevisionSubmission.model_validate(submission)
        now = _aware(enqueued_at)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await _require_outbound(db, item)
            existing = await _row(db, item.submission_id)
            if existing is not None:
                view = _restore_view(existing)
                await _verify_event_chain(db, view)
                await db.rollback()
                if view.submission == item:
                    return view
                raise EvolutionStablePromotionObservationRevisionDispatchError(
                    "stable_promotion_observation_revision_dispatch_conflict",
                    "Submission ID 已绑定不同 dispatch artifact。",
                )
            active_rows = await (
                await db.execute(
                    "SELECT * FROM evolution_stable_promotion_observation_revision_dispatches "
                    "WHERE admission_id = ? ORDER BY first_sequence",
                    (item.payload.admission_id,),
                )
            ).fetchall()
            for row in active_rows:
                view = _restore_view(row)
                await _verify_event_chain(db, view)
                if view.latest_event.state in {"queued", "in_flight"}:
                    await db.rollback()
                    raise EvolutionStablePromotionObservationRevisionDispatchError(
                        "stable_promotion_observation_revision_dispatch_active",
                        "同一 Admission 已有 active revision batch dispatch。",
                    )
            acknowledged_sequence, acknowledged_sha = await _acknowledged_head(
                db, item.payload.admission_id
            )
            if not (
                item.payload.prior_remote_head_sequence == acknowledged_sequence
                and item.payload.prior_remote_head_revision_sha256 == acknowledged_sha
            ):
                await db.rollback()
                raise EvolutionStablePromotionObservationRevisionDispatchError(
                    "stable_promotion_observation_revision_dispatch_head_conflict",
                    "Signed batch 前置 head 与 local acknowledged head 不一致。",
                )
            event = _event(
                submission=item,
                sequence=1,
                state="queued",
                claim_epoch=0,
                attempt_count=0,
                next_attempt_at=now,
                occurred_at=now,
            )
            await db.execute(
                "INSERT INTO evolution_stable_promotion_observation_revision_dispatches "
                "(submission_id, admission_id, first_sequence, last_sequence, "
                "submission_json, latest_event_json, receipt_json) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    item.submission_id,
                    item.payload.admission_id,
                    item.payload.first_sequence,
                    item.payload.last_sequence,
                    item.model_dump_json(),
                    event.model_dump_json(),
                ),
            )
            await _insert_event(db, event)
            await db.commit()
            return EvolutionStablePromotionObservationRevisionDispatchView(
                submission=item,
                latest_event=event,
            )

    async def get(
        self, submission_id: str
    ) -> EvolutionStablePromotionObservationRevisionDispatchView | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await _row(db, _submission_id(submission_id))
            if row is None:
                return None
            view = _restore_view(row)
            await _verify_event_chain(db, view)
            return view

    async def latest_for_admission(
        self, admission_id: str
    ) -> EvolutionStablePromotionObservationRevisionDispatchView | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT * FROM evolution_stable_promotion_observation_revision_dispatches "
                    "WHERE admission_id = ? ORDER BY first_sequence DESC LIMIT 1",
                    (_admission_id(admission_id),),
                )
            ).fetchone()
            if row is None:
                return None
            view = _restore_view(row)
            await _verify_event_chain(db, view)
            await _acknowledged_head(db, view.submission.payload.admission_id)
            return view

    async def acknowledged_head(self, admission_id: str) -> tuple[int, str]:
        if not self.db_path.is_file():
            return 0, ""
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            return await _acknowledged_head(db, _admission_id(admission_id))

    async def acknowledged_chain_candidates(self, *, limit: int) -> tuple[str, ...]:
        """Return bounded Admissions whose latest dispatch is ACKed without a successor."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Observation revision chain candidate limit 必须为 1..1000。")
        if not self.db_path.is_file():
            return ()
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            rows = await (
                await db.execute(
                    "SELECT current.* FROM "
                    "evolution_stable_promotion_observation_revision_dispatches current "
                    "WHERE NOT EXISTS (SELECT 1 FROM "
                    "evolution_stable_promotion_observation_revision_dispatches later "
                    "WHERE later.admission_id = current.admission_id "
                    "AND later.first_sequence > current.first_sequence) "
                    "ORDER BY current.rowid LIMIT 1000"
                )
            ).fetchall()
            candidates: list[str] = []
            for row in rows:
                view = _restore_view(row)
                await _verify_event_chain(db, view)
                if view.latest_event.state != "acknowledged":
                    continue
                await _acknowledged_head(db, view.submission.payload.admission_id)
                candidates.append(view.submission.payload.admission_id)
                if len(candidates) >= limit:
                    break
            return tuple(candidates)

    async def claim(
        self,
        *,
        owner_id: str,
        now: str | datetime,
        lease_seconds: int = 60,
    ) -> EvolutionStablePromotionObservationRevisionDispatchView | None:
        owner = _owner_sha256(owner_id)
        timestamp = _aware(now)
        if isinstance(lease_seconds, bool) or not 3 <= lease_seconds <= 300:
            raise ValueError("Observation revision claim lease 必须为 3..300 秒。")
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute(
                    "SELECT * FROM evolution_stable_promotion_observation_revision_dispatches "
                    "ORDER BY rowid LIMIT 1000"
                )
            ).fetchall()
            for row in rows:
                view = _restore_view(row)
                await _verify_event_chain(db, view)
                event = view.latest_event
                due = _aware(event.next_attempt_at) <= timestamp
                expired = event.lease_expires_at is not None and (
                    _aware(event.lease_expires_at) <= timestamp
                )
                if (event.state == "queued" and due) or (
                    event.state == "in_flight" and expired
                ):
                    claimed = _event(
                        submission=view.submission,
                        sequence=event.sequence + 1,
                        state="in_flight",
                        owner_sha256=owner,
                        claim_epoch=event.claim_epoch + 1,
                        attempt_count=event.attempt_count + 1,
                        lease_expires_at=timestamp + timedelta(seconds=lease_seconds),
                        next_attempt_at=timestamp,
                        occurred_at=timestamp,
                        previous_event_sha256=event.event_sha256,
                    )
                    await _update_event(db, view.submission.submission_id, claimed)
                    await db.commit()
                    return view.model_copy(update={"latest_event": claimed})
            await db.rollback()
            return None

    async def retry(
        self,
        *,
        submission_id: str,
        owner_id: str,
        claim_epoch: int,
        failure_code: str,
        now: str | datetime,
        base_seconds: float = 5.0,
        max_seconds: float = 300.0,
    ) -> EvolutionStablePromotionObservationRevisionDispatchView:
        return await self._settle_failure(
            submission_id=submission_id,
            owner_id=owner_id,
            claim_epoch=claim_epoch,
            failure_code=failure_code,
            now=now,
            dead_letter=False,
            base_seconds=base_seconds,
            max_seconds=max_seconds,
        )

    async def dead_letter(
        self,
        *,
        submission_id: str,
        owner_id: str,
        claim_epoch: int,
        failure_code: str,
        now: str | datetime,
    ) -> EvolutionStablePromotionObservationRevisionDispatchView:
        return await self._settle_failure(
            submission_id=submission_id,
            owner_id=owner_id,
            claim_epoch=claim_epoch,
            failure_code=failure_code,
            now=now,
            dead_letter=True,
        )

    async def _settle_failure(
        self,
        *,
        submission_id: str,
        owner_id: str,
        claim_epoch: int,
        failure_code: str,
        now: str | datetime,
        dead_letter: bool,
        base_seconds: float = 5.0,
        max_seconds: float = 300.0,
    ) -> EvolutionStablePromotionObservationRevisionDispatchView:
        timestamp = _aware(now)
        owner = _owner_sha256(owner_id)
        code = _failure_code(failure_code)
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await _row(db, _submission_id(submission_id))
            if row is None:
                raise EvolutionStablePromotionObservationRevisionDispatchError(
                    "stable_promotion_observation_revision_dispatch_missing",
                    "Observation revision dispatch 不存在。",
                )
            view = _restore_view(row)
            await _verify_event_chain(db, view)
            event = view.latest_event
            _require_live_claim(event, owner=owner, claim_epoch=claim_epoch, now=timestamp)
            delay = 0.0 if dead_letter else _retry_delay(
                event.attempt_count,
                base_seconds=base_seconds,
                max_seconds=max_seconds,
            )
            settled = _event(
                submission=view.submission,
                sequence=event.sequence + 1,
                state="dead_letter" if dead_letter else "queued",
                claim_epoch=event.claim_epoch,
                attempt_count=event.attempt_count,
                next_attempt_at=timestamp + timedelta(seconds=delay),
                occurred_at=timestamp,
                previous_event_sha256=event.event_sha256,
                failure_code=code,
            )
            await _update_event(db, submission_id, settled)
            await db.commit()
            return view.model_copy(update={"latest_event": settled})

    async def acknowledge(
        self,
        *,
        submission_id: str,
        owner_id: str,
        claim_epoch: int,
        receipt: EvolutionStablePromotionObservationRevisionDeliveryReceipt,
        now: str | datetime,
    ) -> EvolutionStablePromotionObservationRevisionDispatchView:
        timestamp = _aware(now)
        owner = _owner_sha256(owner_id)
        item = EvolutionStablePromotionObservationRevisionDeliveryReceipt.model_validate(
            receipt
        )
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await _row(db, _submission_id(submission_id))
            if row is None:
                raise EvolutionStablePromotionObservationRevisionDispatchError(
                    "stable_promotion_observation_revision_dispatch_missing",
                    "Observation revision dispatch 不存在。",
                )
            view = _restore_view(row)
            await _verify_event_chain(db, view)
            if view.latest_event.state == "acknowledged":
                await db.rollback()
                if view.receipt == item:
                    return view
                raise EvolutionStablePromotionObservationRevisionDispatchError(
                    "stable_promotion_observation_revision_dispatch_receipt_conflict",
                    "Observation revision dispatch 已绑定不同 Receipt。",
                )
            _require_live_claim(
                view.latest_event,
                owner=owner,
                claim_epoch=claim_epoch,
                now=timestamp,
            )
            if not (
                stable_promotion_observation_revision_receipt_matches_submission(
                    item, view.submission
                )
                and _aware(item.received_at) <= timestamp
            ):
                raise EvolutionStablePromotionObservationRevisionDispatchError(
                    "stable_promotion_observation_revision_dispatch_receipt_invalid",
                    "Control Plane Receipt 与 current in-flight batch 不一致。",
                )
            acknowledged = _event(
                submission=view.submission,
                sequence=view.latest_event.sequence + 1,
                state="acknowledged",
                claim_epoch=view.latest_event.claim_epoch,
                attempt_count=view.latest_event.attempt_count,
                next_attempt_at=timestamp,
                occurred_at=timestamp,
                previous_event_sha256=view.latest_event.event_sha256,
                receipt_id=item.receipt_id,
                receipt_sha256=item.receipt_sha256,
            )
            await db.execute(
                "UPDATE evolution_stable_promotion_observation_revision_dispatches SET "
                "latest_event_json = ?, receipt_json = ? WHERE submission_id = ?",
                (
                    acknowledged.model_dump_json(),
                    item.model_dump_json(),
                    submission_id,
                ),
            )
            await _insert_event(db, acknowledged)
            await db.commit()
            return view.model_copy(
                update={"latest_event": acknowledged, "receipt": item}
            )


class EvolutionStablePromotionObservationRevisionWorkerState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    WAITING = "waiting"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class EvolutionStablePromotionObservationRevisionWorkerPolicy:
    interval_seconds: float = 30.0
    max_empty_backoff_seconds: float = 300.0
    max_failure_backoff_seconds: float = 300.0
    claim_lease_seconds: int = 60
    scan_limit: int = 20
    receipt_timeout_seconds: float = 20.0
    retry_base_seconds: float = 5.0
    retry_max_seconds: float = 300.0
    max_attempts: int = 8
    shutdown_drain_seconds: float = 25.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if not _finite(self.interval_seconds, 0.1, 86_400):
            raise ValueError("Observation revision Worker interval 无效。")
        if not _finite(
            self.max_empty_backoff_seconds, self.interval_seconds, 604_800
        ) or not _finite(
            self.max_failure_backoff_seconds, self.interval_seconds, 604_800
        ):
            raise ValueError("Observation revision Worker backoff 无效。")
        if isinstance(self.claim_lease_seconds, bool) or not (
            isinstance(self.claim_lease_seconds, int)
            and 3 <= self.claim_lease_seconds <= 300
        ):
            raise ValueError("Observation revision Worker claim lease 无效。")
        if isinstance(self.scan_limit, bool) or not (
            isinstance(self.scan_limit, int) and 1 <= self.scan_limit <= 1000
        ):
            raise ValueError("Observation revision Worker scan limit 无效。")
        if not _finite(
            self.receipt_timeout_seconds, 0.1, self.claim_lease_seconds - 0.1
        ):
            raise ValueError("Observation revision Worker Receipt timeout 无效。")
        if not _finite(self.retry_base_seconds, 0.1, 3600) or not _finite(
            self.retry_max_seconds, self.retry_base_seconds, 3600
        ):
            raise ValueError("Observation revision Worker retry policy 无效。")
        if isinstance(self.max_attempts, bool) or not (
            isinstance(self.max_attempts, int) and 1 <= self.max_attempts <= 1000
        ):
            raise ValueError("Observation revision Worker attempt budget 无效。")
        if not _finite(self.shutdown_drain_seconds, 0.1, 3600):
            raise ValueError("Observation revision Worker drain timeout 无效。")
        if not _finite(self.jitter_ratio, 0, 0.5):
            raise ValueError("Observation revision Worker jitter 无效。")


@dataclass(frozen=True, slots=True)
class EvolutionStablePromotionObservationRevisionPassResult:
    claimed: int = 0
    acknowledged: int = 0
    chained: int = 0
    retry_scheduled: int = 0
    dead_lettered: int = 0
    failures: int = 0
    failure_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvolutionStablePromotionObservationRevisionWorkerSnapshot:
    state: EvolutionStablePromotionObservationRevisionWorkerState
    pass_count: int
    claimed_count: int
    acknowledged_count: int
    chained_count: int
    retry_scheduled_count: int
    dead_lettered_count: int
    failure_count: int
    forced_shutdown_count: int
    consecutive_empty_passes: int
    consecutive_failure_passes: int
    next_delay_seconds: float
    last_failure_codes: tuple[str, ...]
    started_at: str
    last_pass_at: str


class EvolutionStablePromotionObservationRevisionDeliveryWorker:
    """Drain a FIFO prefix while fencing every network attempt by owner/epoch/lease."""

    def __init__(
        self,
        *,
        sender: EvolutionStablePromotionObservationRevisionDeliveryService,
        store: EvolutionStablePromotionObservationRevisionDispatchStore,
        transport: EvolutionStablePromotionObservationRevisionControlPlaneTransport,
        policy: EvolutionStablePromotionObservationRevisionWorkerPolicy,
        owner_id: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if sender.store.db_path != store.db_path:
            raise ValueError("Observation revision Worker sender/store 必须同源。")
        if not isinstance(
            transport, EvolutionStablePromotionObservationRevisionControlPlaneTransport
        ):
            raise TypeError("transport 必须实现 observation revision Control Plane port。")
        self.sender = sender
        self.store = store
        self.transport = transport
        self.policy = policy
        self.owner_id = (
            owner_id or f"stable-observation-revision-{uuid.uuid4().hex}"
        ).strip()
        if not 1 <= len(self.owner_id) <= 256:
            raise ValueError("Observation revision Worker owner_id 无效。")
        self.clock = clock
        self.random_value = random_value
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._state = EvolutionStablePromotionObservationRevisionWorkerState.STOPPED
        self._pass_count = self._claimed_count = self._acknowledged_count = 0
        self._chained_count = self._retry_scheduled_count = 0
        self._dead_lettered_count = self._failure_count = 0
        self._forced_shutdown_count = self._consecutive_empty_passes = 0
        self._consecutive_failure_passes = 0
        self._next_delay_seconds = policy.interval_seconds
        self._last_failure_codes: tuple[str, ...] = ()
        self._started_at = self._last_pass_at = ""

    async def enqueue_next(
        self, admission_id: str
    ) -> EvolutionStablePromotionObservationRevisionDispatchView:
        item_id = _admission_id(admission_id)
        after_sequence, _ = await self.store.acknowledged_head(item_id)
        submission = await self.sender.prepare(
            admission_id=item_id,
            after_sequence=after_sequence,
        )
        view = await self.store.enqueue(submission, enqueued_at=self._now())
        self.wake()
        return view

    def start(self) -> bool:
        if self._task is not None and not self._task.done():
            return False
        self._stop_event.clear()
        self._wake_event.clear()
        self._state = EvolutionStablePromotionObservationRevisionWorkerState.WAITING
        self._started_at = self._timestamp()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="naumi-stable-observation-revision-delivery",
        )
        return True

    async def stop(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        self._state = EvolutionStablePromotionObservationRevisionWorkerState.STOPPING
        self._stop_event.set()
        self._wake_event.set()
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self.policy.shutdown_drain_seconds
            )
        except TimeoutError:
            self._forced_shutdown_count += 1
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None
        return True

    def wake(self) -> bool:
        if self._task is None or self._task.done() or self._stop_event.is_set():
            return False
        self._wake_event.set()
        return True

    async def run_once(self) -> EvolutionStablePromotionObservationRevisionPassResult:
        async with self._run_lock:
            return await self._run_once_locked()

    async def _run_once_locked(
        self,
    ) -> EvolutionStablePromotionObservationRevisionPassResult:
        self._state = EvolutionStablePromotionObservationRevisionWorkerState.RUNNING
        self._last_pass_at = self._timestamp()
        claimed = acknowledged = chained = retry_scheduled = 0
        dead_lettered = failures = 0
        codes: list[str] = []
        try:
            chain_candidates = await self.store.acknowledged_chain_candidates(
                limit=self.policy.scan_limit
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            chain_candidates = ()
            failures += 1
            codes.append(
                "stable_promotion_observation_revision_chain_recovery_scan_failed"
            )
        for admission_id in chain_candidates:
            try:
                await self.enqueue_next(admission_id)
            except asyncio.CancelledError:
                raise
            except EvolutionStablePromotionObservationRevisionDeliveryError as exc:
                if exc.code != "stable_promotion_observation_revision_no_new_samples":
                    failures += 1
                    codes.append(exc.code)
            except Exception as exc:
                code, _ = _failure(exc)
                failures += 1
                codes.append(code)
            else:
                chained += 1
        for _ in range(self.policy.scan_limit):
            if self._stop_event.is_set():
                break
            try:
                view = await self.store.claim(
                    owner_id=self.owner_id,
                    now=self._now(),
                    lease_seconds=self.policy.claim_lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                codes.append("stable_promotion_observation_revision_dispatch_claim_failed")
                break
            if view is None:
                break
            claimed += 1
            event = view.latest_event
            delivery_acknowledged = False
            try:
                current = await self.sender.prepare(
                    admission_id=view.submission.payload.admission_id,
                    after_sequence=(
                        view.submission.payload.prior_remote_head_sequence
                    ),
                )
                if current != view.submission:
                    raise EvolutionStablePromotionObservationRevisionTransportError(
                        "stable_promotion_observation_revision_dispatch_submission_stale",
                        "Current signed revision Submission 与 dispatch 不一致。",
                        retryable=False,
                    )
                receipt = await asyncio.wait_for(
                    self.transport.submit(view.submission),
                    timeout=self.policy.receipt_timeout_seconds,
                )
                await self.store.acknowledge(
                    submission_id=view.submission.submission_id,
                    owner_id=self.owner_id,
                    claim_epoch=event.claim_epoch,
                    receipt=receipt,
                    now=self._now(),
                )
                acknowledged += 1
                delivery_acknowledged = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code, retryable = _failure(exc)
                failures += 1
                codes.append(code)
                try:
                    if not retryable or event.attempt_count >= self.policy.max_attempts:
                        await self.store.dead_letter(
                            submission_id=view.submission.submission_id,
                            owner_id=self.owner_id,
                            claim_epoch=event.claim_epoch,
                            failure_code=code,
                            now=self._now(),
                        )
                        dead_lettered += 1
                    else:
                        await self.store.retry(
                            submission_id=view.submission.submission_id,
                            owner_id=self.owner_id,
                            claim_epoch=event.claim_epoch,
                            failure_code=code,
                            now=self._now(),
                            base_seconds=self.policy.retry_base_seconds,
                            max_seconds=self.policy.retry_max_seconds,
                        )
                        retry_scheduled += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failures += 1
                    codes.append(
                        "stable_promotion_observation_revision_dispatch_settlement_failed"
                    )
            if delivery_acknowledged:
                try:
                    await self.enqueue_next(view.submission.payload.admission_id)
                except asyncio.CancelledError:
                    raise
                except EvolutionStablePromotionObservationRevisionDeliveryError as exc:
                    if exc.code != "stable_promotion_observation_revision_no_new_samples":
                        failures += 1
                        codes.append(exc.code)
                except Exception as exc:
                    code, _ = _failure(exc)
                    failures += 1
                    codes.append(code)
                else:
                    chained += 1
        result = EvolutionStablePromotionObservationRevisionPassResult(
            claimed=claimed,
            acknowledged=acknowledged,
            chained=chained,
            retry_scheduled=retry_scheduled,
            dead_lettered=dead_lettered,
            failures=failures,
            failure_codes=tuple(codes),
        )
        self._record(result)
        return result

    def snapshot(self) -> EvolutionStablePromotionObservationRevisionWorkerSnapshot:
        return EvolutionStablePromotionObservationRevisionWorkerSnapshot(
            state=self._state,
            pass_count=self._pass_count,
            claimed_count=self._claimed_count,
            acknowledged_count=self._acknowledged_count,
            chained_count=self._chained_count,
            retry_scheduled_count=self._retry_scheduled_count,
            dead_lettered_count=self._dead_lettered_count,
            failure_count=self._failure_count,
            forced_shutdown_count=self._forced_shutdown_count,
            consecutive_empty_passes=self._consecutive_empty_passes,
            consecutive_failure_passes=self._consecutive_failure_passes,
            next_delay_seconds=self._next_delay_seconds,
            last_failure_codes=self._last_failure_codes,
            started_at=self._started_at,
            last_pass_at=self._last_pass_at,
        )

    def _record(
        self, result: EvolutionStablePromotionObservationRevisionPassResult
    ) -> None:
        self._pass_count += 1
        self._claimed_count += result.claimed
        self._acknowledged_count += result.acknowledged
        self._chained_count += result.chained
        self._retry_scheduled_count += result.retry_scheduled
        self._dead_lettered_count += result.dead_lettered
        self._failure_count += result.failures
        self._last_failure_codes = result.failure_codes
        if result.failures:
            self._consecutive_empty_passes = 0
            self._consecutive_failure_passes += 1
            delay = min(
                self.policy.max_failure_backoff_seconds,
                self.policy.interval_seconds
                * 2 ** min(20, self._consecutive_failure_passes - 1),
            )
        elif result.claimed == 0:
            self._consecutive_failure_passes = 0
            self._consecutive_empty_passes += 1
            delay = min(
                self.policy.max_empty_backoff_seconds,
                self.policy.interval_seconds
                * 2 ** min(20, self._consecutive_empty_passes),
            )
        else:
            self._consecutive_empty_passes = 0
            self._consecutive_failure_passes = 0
            delay = self.policy.interval_seconds
        self._next_delay_seconds = self._jittered(delay)
        self._state = (
            EvolutionStablePromotionObservationRevisionWorkerState.STOPPING
            if self._stop_event.is_set()
            else EvolutionStablePromotionObservationRevisionWorkerState.WAITING
        )

    async def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._wait(self._next_delay_seconds)
                if not self._stop_event.is_set():
                    await self.run_once()
        finally:
            self._state = EvolutionStablePromotionObservationRevisionWorkerState.STOPPED

    async def _wait(self, delay: float) -> None:
        stop = asyncio.create_task(self._stop_event.wait())
        wake = asyncio.create_task(self._wake_event.wait())
        try:
            done, _ = await asyncio.wait(
                {stop, wake},
                timeout=max(0.0, delay),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wake in done and wake.result():
                self._wake_event.clear()
        finally:
            for task in (stop, wake):
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def _now(self) -> datetime:
        return _aware(self.clock())

    def _timestamp(self) -> str:
        return self._now().isoformat()

    def _jittered(self, delay: float) -> float:
        sample = self.random_value()
        if not _finite(sample, 0, 1):
            raise ValueError("Observation revision Worker random source 必须在 0..1。")
        factor = 1 + self.policy.jitter_ratio * (2 * float(sample) - 1)
        return max(0.0, delay * factor)


def render_stable_promotion_observation_revision_dispatch(
    view: EvolutionStablePromotionObservationRevisionDispatchView,
) -> str:
    event = view.latest_event
    return "\n".join((
        "## Stable Promotion Observation Revision Dispatch",
        "",
        f"- Admission：`{event.admission_id}`",
        f"- Submission：`{event.submission_id}`",
        f"- Revision range：`{event.first_sequence}..{event.last_sequence}`",
        f"- 状态：**{event.state}**",
        f"- Claim epoch / attempt：`{event.claim_epoch}` / `{event.attempt_count}`",
        f"- Next attempt：`{event.next_attempt_at}`",
        f"- Receipt：`{event.receipt_id or 'none'}`",
        f"- Failure：`{event.failure_code or 'none'}`",
        "- Population / Promoted Outcome authority：`false`",
    ))


def render_stable_promotion_observation_revision_worker_pass(
    result: EvolutionStablePromotionObservationRevisionPassResult,
) -> str:
    return "\n".join((
        "## Stable Promotion Observation Revision Worker",
        "",
        "- 状态：**本轮完成**",
        f"- Claimed：`{result.claimed}`",
        f"- ACK / Chained：`{result.acknowledged}` / `{result.chained}`",
        f"- Retry / Dead letter：`{result.retry_scheduled}` / `{result.dead_lettered}`",
        f"- Failures：`{result.failures}`",
        f"- Failure codes：`{', '.join(result.failure_codes) or 'none'}`",
    ))


def render_stable_promotion_observation_revision_worker(
    snapshot: EvolutionStablePromotionObservationRevisionWorkerSnapshot,
) -> str:
    return "\n".join((
        "## Stable Promotion Observation Revision Worker",
        "",
        f"- 状态：**{snapshot.state.value}**",
        f"- Passes：`{snapshot.pass_count}`",
        f"- Claimed / ACK：`{snapshot.claimed_count}` / `{snapshot.acknowledged_count}`",
        f"- Chained：`{snapshot.chained_count}`",
        f"- Retry / Dead letter：`{snapshot.retry_scheduled_count}` / "
        f"`{snapshot.dead_lettered_count}`",
        f"- Failures：`{snapshot.failure_count}`",
        f"- Forced shutdown：`{snapshot.forced_shutdown_count}`",
        f"- Next delay：`{snapshot.next_delay_seconds:.3f}s`",
        f"- Failure codes：`{', '.join(snapshot.last_failure_codes) or 'none'}`",
    ))


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_observation_revision_dispatches ("
        "submission_id TEXT PRIMARY KEY, admission_id TEXT NOT NULL, "
        "first_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL, "
        "submission_json TEXT NOT NULL, latest_event_json TEXT NOT NULL, "
        "receipt_json TEXT, UNIQUE(admission_id, first_sequence))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_stable_promotion_observation_revision_dispatch_events ("
        "submission_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_json TEXT NOT NULL, "
        "PRIMARY KEY(submission_id, sequence))"
    )


async def _row(db: aiosqlite.Connection, submission_id: str):
    return await (
        await db.execute(
            "SELECT * FROM evolution_stable_promotion_observation_revision_dispatches "
            "WHERE submission_id = ?",
            (submission_id,),
        )
    ).fetchone()


async def _require_outbound(db, submission) -> None:
    row = await (
        await db.execute(
            "SELECT submission_id, submission_sha256, submission_json FROM "
            "evolution_stable_promotion_observation_revision_outbox "
            "WHERE submission_id = ?",
            (submission.submission_id,),
        )
    ).fetchone()
    try:
        durable = (
            None
            if row is None
            else EvolutionStablePromotionObservationRevisionSubmission.model_validate_json(
                row["submission_json"]
            )
        )
    except ValueError as exc:
        raise EvolutionStablePromotionObservationRevisionDispatchError(
            "stable_promotion_observation_revision_dispatch_outbound_corrupt",
            "Observation revision signed outbound 已损坏。",
        ) from exc
    if row is None or not (
        durable == submission
        and row["submission_id"] == submission.submission_id
        and row["submission_sha256"] == submission.submission_sha256
    ):
        raise EvolutionStablePromotionObservationRevisionDispatchError(
            "stable_promotion_observation_revision_dispatch_outbound_missing",
            "Observation revision dispatch 缺少 exact signed outbound。",
        )


def _restore_view(row) -> EvolutionStablePromotionObservationRevisionDispatchView:
    try:
        view = EvolutionStablePromotionObservationRevisionDispatchView(
            submission=(
                EvolutionStablePromotionObservationRevisionSubmission.model_validate_json(
                    row["submission_json"]
                )
            ),
            latest_event=(
                EvolutionStablePromotionObservationRevisionDispatchEvent.model_validate_json(
                    row["latest_event_json"]
                )
            ),
            receipt=(
                None
                if row["receipt_json"] is None
                else EvolutionStablePromotionObservationRevisionDeliveryReceipt.model_validate_json(
                    row["receipt_json"]
                )
            ),
        )
        if not (
            row["submission_id"] == view.submission.submission_id
            and row["admission_id"] == view.submission.payload.admission_id
            and int(row["first_sequence"]) == view.submission.payload.first_sequence
            and int(row["last_sequence"]) == view.submission.payload.last_sequence
        ):
            raise ValueError("dispatch row identity mismatch")
        return view
    except ValueError as exc:
        raise EvolutionStablePromotionObservationRevisionDispatchError(
            "stable_promotion_observation_revision_dispatch_store_corrupt",
            "Observation revision dispatch durable row 已损坏。",
        ) from exc


async def _insert_event(db, event) -> None:
    await db.execute(
        "INSERT INTO evolution_stable_promotion_observation_revision_dispatch_events "
        "(submission_id, sequence, event_json) VALUES (?, ?, ?)",
        (event.submission_id, event.sequence, event.model_dump_json()),
    )


async def _update_event(db, submission_id, event) -> None:
    await db.execute(
        "UPDATE evolution_stable_promotion_observation_revision_dispatches SET "
        "latest_event_json = ? WHERE submission_id = ?",
        (event.model_dump_json(), submission_id),
    )
    await _insert_event(db, event)


async def _verify_event_chain(db, view) -> None:
    await _require_outbound(db, view.submission)
    rows = await (
        await db.execute(
            "SELECT event_json FROM "
            "evolution_stable_promotion_observation_revision_dispatch_events "
            "WHERE submission_id = ? ORDER BY sequence",
            (view.submission.submission_id,),
        )
    ).fetchall()
    try:
        events = [
            EvolutionStablePromotionObservationRevisionDispatchEvent.model_validate_json(
                row[0]
            )
            for row in rows
        ]
    except ValueError as exc:
        raise EvolutionStablePromotionObservationRevisionDispatchError(
            "stable_promotion_observation_revision_dispatch_chain_corrupt",
            "Observation revision dispatch event chain 无法解析。",
        ) from exc
    if not events or events[-1] != view.latest_event:
        raise EvolutionStablePromotionObservationRevisionDispatchError(
            "stable_promotion_observation_revision_dispatch_chain_corrupt",
            "Observation revision dispatch event chain 与主记录不一致。",
        )
    allowed = {
        ("queued", "in_flight"),
        ("in_flight", "queued"),
        ("in_flight", "in_flight"),
        ("in_flight", "acknowledged"),
        ("in_flight", "dead_letter"),
    }
    for index, event in enumerate(events):
        if not (
            event.sequence == index + 1
            and event.submission_id == view.submission.submission_id
            and event.submission_sha256 == view.submission.submission_sha256
            and event.previous_event_sha256
            == (None if index == 0 else events[index - 1].event_sha256)
        ):
            raise EvolutionStablePromotionObservationRevisionDispatchError(
                "stable_promotion_observation_revision_dispatch_chain_corrupt",
                "Observation revision dispatch event chain 不连续。",
            )
        if index == 0:
            if not (
                event.state == "queued"
                and event.claim_epoch == 0
                and event.attempt_count == 0
            ):
                raise EvolutionStablePromotionObservationRevisionDispatchError(
                    "stable_promotion_observation_revision_dispatch_chain_corrupt",
                    "Observation revision dispatch 首事件无效。",
                )
            continue
        previous = events[index - 1]
        if (previous.state, event.state) not in allowed:
            raise EvolutionStablePromotionObservationRevisionDispatchError(
                "stable_promotion_observation_revision_dispatch_chain_corrupt",
                "Observation revision dispatch 状态转换无效。",
            )
        claiming = event.state == "in_flight"
        if claiming and not (
            event.claim_epoch == previous.claim_epoch + 1
            and event.attempt_count == previous.attempt_count + 1
        ):
            raise EvolutionStablePromotionObservationRevisionDispatchError(
                "stable_promotion_observation_revision_dispatch_chain_corrupt",
                "Observation revision dispatch claim 计数不连续。",
            )
        if not claiming and not (
            event.claim_epoch == previous.claim_epoch
            and event.attempt_count == previous.attempt_count
        ):
            raise EvolutionStablePromotionObservationRevisionDispatchError(
                "stable_promotion_observation_revision_dispatch_chain_corrupt",
                "Observation revision dispatch 非 claim 计数漂移。",
            )


async def _acknowledged_head(db, admission_id: str) -> tuple[int, str]:
    rows = await (
        await db.execute(
            "SELECT * FROM evolution_stable_promotion_observation_revision_dispatches "
            "WHERE admission_id = ? ORDER BY first_sequence",
            (admission_id,),
        )
    ).fetchall()
    sequence = 0
    sha = ""
    for row in rows:
        view = _restore_view(row)
        await _verify_event_chain(db, view)
        if view.latest_event.state == "dead_letter":
            continue
        if view.latest_event.state != "acknowledged":
            break
        payload = view.submission.payload
        if not (
            payload.prior_remote_head_sequence == sequence
            and payload.prior_remote_head_revision_sha256 == sha
            and payload.first_sequence == sequence + 1
            and view.receipt is not None
        ):
            raise EvolutionStablePromotionObservationRevisionDispatchError(
                "stable_promotion_observation_revision_dispatch_ack_chain_corrupt",
                "Observation revision local ACK chain 不连续。",
            )
        sequence = payload.last_sequence
        sha = payload.revisions[-1].revision_sha256
    return sequence, sha


def _event(
    *,
    submission,
    sequence,
    state,
    claim_epoch,
    attempt_count,
    next_attempt_at,
    occurred_at,
    owner_sha256=None,
    lease_expires_at=None,
    receipt_id=None,
    receipt_sha256=None,
    failure_code=None,
    previous_event_sha256=None,
):
    payload = submission.payload
    core = {
        "schema_version": 1,
        "admission_id": payload.admission_id,
        "submission_id": submission.submission_id,
        "submission_sha256": submission.submission_sha256,
        "first_sequence": payload.first_sequence,
        "last_sequence": payload.last_sequence,
        "sequence": sequence,
        "state": state,
        "owner_sha256": owner_sha256,
        "claim_epoch": claim_epoch,
        "attempt_count": attempt_count,
        "lease_expires_at": (
            None if lease_expires_at is None else lease_expires_at.isoformat()
        ),
        "next_attempt_at": next_attempt_at.isoformat(),
        "receipt_id": receipt_id,
        "receipt_sha256": receipt_sha256,
        "failure_code": failure_code,
        "occurred_at": occurred_at.isoformat(),
        "previous_event_sha256": previous_event_sha256,
    }
    digest = _digest(core)
    return EvolutionStablePromotionObservationRevisionDispatchEvent.model_validate({
        **core,
        "event_id": f"evstablepromrevdispatch_{digest[:24]}",
        "event_sha256": digest,
    })


def _require_live_claim(event, *, owner, claim_epoch, now) -> None:
    if not (
        event.state == "in_flight"
        and event.owner_sha256 == owner
        and event.claim_epoch == claim_epoch
        and event.lease_expires_at is not None
        and now < _aware(event.lease_expires_at)
    ):
        raise EvolutionStablePromotionObservationRevisionDispatchError(
            "stable_promotion_observation_revision_dispatch_claim_fenced",
            "Observation revision dispatch claim 已失效。",
        )


def _failure(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, TimeoutError):
        return "stable_promotion_observation_revision_receipt_timeout", True
    if isinstance(exc, EvolutionStablePromotionObservationRevisionTransportError):
        return exc.code, exc.retryable
    if isinstance(exc, EvolutionStablePromotionObservationRevisionDeliveryError):
        permanent = exc.code in {
            "stable_promotion_observation_revision_admission_mismatch",
            "stable_promotion_observation_revision_admission_stale",
            "stable_promotion_observation_revision_credential_stale",
            "stable_promotion_observation_revision_signature_untrusted",
            "stable_promotion_observation_revision_outbound_conflict",
            "stable_promotion_observation_revision_cursor_stale",
        }
        return exc.code, not permanent
    if isinstance(exc, EvolutionStablePromotionObservationRevisionDispatchError):
        permanent = exc.code in {
            "stable_promotion_observation_revision_dispatch_receipt_invalid",
            "stable_promotion_observation_revision_dispatch_receipt_conflict",
            "stable_promotion_observation_revision_dispatch_outbound_missing",
            "stable_promotion_observation_revision_dispatch_chain_corrupt",
            "stable_promotion_observation_revision_dispatch_ack_chain_corrupt",
        }
        return exc.code, not permanent
    if isinstance(exc, (ConnectionError, OSError)):
        return "stable_promotion_observation_revision_transport_unavailable", True
    return "stable_promotion_observation_revision_transport_failed", True


def _admission_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(_ADMISSION_RE, normalized) is None:
        raise ValueError("Runtime Admission ID 无效。")
    return normalized


def _submission_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(_SUBMISSION_RE, normalized) is None:
        raise ValueError("Observation revision Submission ID 无效。")
    return normalized


def _owner_sha256(value: str) -> str:
    normalized = str(value or "").strip()
    if not 1 <= len(normalized) <= 256:
        raise ValueError("Observation revision dispatch owner_id 无效。")
    return hashlib.sha256(normalized.encode()).hexdigest()


def _failure_code(value: str) -> str:
    normalized = str(value or "").strip()
    if not 1 <= len(normalized) <= 128:
        raise ValueError("Observation revision dispatch failure code 无效。")
    return normalized


def _retry_delay(attempt: int, *, base_seconds: float, max_seconds: float) -> float:
    if not (
        _finite(base_seconds, 0.1, 3600)
        and _finite(max_seconds, base_seconds, 3600)
    ):
        raise ValueError("Observation revision dispatch retry backoff 无效。")
    return min(max_seconds, base_seconds * 2 ** min(max(attempt - 1, 0), 16))


def _aware(value: str | datetime) -> datetime:
    item = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if item.tzinfo is None or item.utcoffset() is None:
        raise ValueError("Observation revision dispatch 时间必须包含时区。")
    return item.astimezone(UTC)


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _finite(value: object, minimum: float, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


__all__ = [
    "EvolutionStablePromotionObservationRevisionControlPlaneTransport",
    "EvolutionStablePromotionObservationRevisionDeliveryWorker",
    "EvolutionStablePromotionObservationRevisionDispatchError",
    "EvolutionStablePromotionObservationRevisionDispatchEvent",
    "EvolutionStablePromotionObservationRevisionDispatchStore",
    "EvolutionStablePromotionObservationRevisionDispatchView",
    "EvolutionStablePromotionObservationRevisionPassResult",
    "EvolutionStablePromotionObservationRevisionTransportError",
    "EvolutionStablePromotionObservationRevisionWorkerPolicy",
    "EvolutionStablePromotionObservationRevisionWorkerSnapshot",
    "EvolutionStablePromotionObservationRevisionWorkerState",
    "LocalStablePromotionObservationRevisionControlPlaneTransport",
    "render_stable_promotion_observation_revision_dispatch",
    "render_stable_promotion_observation_revision_worker",
    "render_stable_promotion_observation_revision_worker_pass",
]
