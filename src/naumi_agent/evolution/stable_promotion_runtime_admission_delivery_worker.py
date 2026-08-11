"""Fenced delivery worker for signed stable-promotion runtime admissions."""

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

from naumi_agent.evolution.stable_promotion_runtime_admission_deliveries import (
    EvolutionStablePromotionRuntimeAdmissionDeliveryError,
    EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
    EvolutionStablePromotionRuntimeAdmissionDeliveryService,
    EvolutionStablePromotionRuntimeAdmissionSubmission,
    stable_promotion_runtime_admission_receipt_matches_submission,
)

_SHA256_RE = r"^[0-9a-f]{64}$"
_ADMISSION_ID_RE = r"^evstablepromadmit_[0-9a-f]{24}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePromotionRuntimeAdmissionDispatchEvent(_StrictModel):
    """Content-addressed append-only transition for one outbound Admission."""

    schema_version: Literal[1] = 1
    event_id: str = Field(pattern=r"^evstablepromdispatch_[0-9a-f]{24}$")
    event_sha256: str = Field(pattern=_SHA256_RE)
    admission_id: str = Field(pattern=_ADMISSION_ID_RE)
    submission_id: str = Field(pattern=r"^evstablepromsubmit_[0-9a-f]{24}$")
    submission_sha256: str = Field(pattern=_SHA256_RE)
    sequence: int = Field(ge=1, le=1_000_000)
    state: Literal["queued", "in_flight", "acknowledged", "dead_letter"]
    owner_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    claim_epoch: int = Field(ge=0, le=1_000_000)
    attempt_count: int = Field(ge=0, le=1_000_000)
    lease_expires_at: str | None = Field(default=None, min_length=1, max_length=100)
    next_attempt_at: str = Field(min_length=1, max_length=100)
    receipt_id: str | None = Field(
        default=None, pattern=r"^evstablepromreceive_[0-9a-f]{24}$"
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
        if self.state == "in_flight" and not (
            self.owner_sha256 and self.lease_expires_at
        ):
            raise ValueError("Runtime Admission in-flight event 缺少 owner/lease。")
        if self.state != "in_flight" and (
            self.owner_sha256 is not None or self.lease_expires_at is not None
        ):
            raise ValueError("非 in-flight Runtime Admission event 不得携带 owner/lease。")
        has_receipt = self.receipt_id is not None and self.receipt_sha256 is not None
        if (self.state == "acknowledged") is not has_receipt:
            raise ValueError("Runtime Admission acknowledged event 必须且只能绑定 Receipt。")
        if self.state == "dead_letter" and self.failure_code is None:
            raise ValueError("Runtime Admission dead-letter event 缺少 failure code。")
        if self.state not in {"queued", "dead_letter"} and self.failure_code is not None:
            raise ValueError("仅 retry/dead-letter event 可绑定 failure code。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"event_id", "event_sha256"})
        )
        if not (
            self.event_sha256 == digest
            and self.event_id == f"evstablepromdispatch_{digest[:24]}"
        ):
            raise ValueError("Runtime Admission dispatch event identity 不一致。")
        return self


class EvolutionStablePromotionRuntimeAdmissionDispatchView(_StrictModel):
    submission: EvolutionStablePromotionRuntimeAdmissionSubmission
    latest_event: EvolutionStablePromotionRuntimeAdmissionDispatchEvent
    receipt: EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt | None = None

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if not (
            self.latest_event.admission_id == self.submission.payload.admission.admission_id
            and self.latest_event.submission_id == self.submission.submission_id
            and self.latest_event.submission_sha256 == self.submission.submission_sha256
        ):
            raise ValueError("Runtime Admission dispatch 与 Submission 不一致。")
        if self.receipt is not None and not (
            self.latest_event.state == "acknowledged"
            and self.latest_event.receipt_id == self.receipt.receipt_id
            and self.latest_event.receipt_sha256 == self.receipt.receipt_sha256
            and stable_promotion_runtime_admission_receipt_matches_submission(
                self.receipt, self.submission
            )
        ):
            raise ValueError("Runtime Admission dispatch Receipt binding 无效。")
        if self.latest_event.state == "acknowledged" and self.receipt is None:
            raise ValueError("acknowledged Runtime Admission dispatch 缺少 Receipt。")
        return self


class EvolutionStablePromotionRuntimeAdmissionTransportError(RuntimeError):
    """Typed failure returned by an authenticated Control Plane transport."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        normalized = str(code or "").strip()
        if not 1 <= len(normalized) <= 128:
            raise ValueError("Runtime Admission transport failure code 无效。")
        self.code = normalized
        self.retryable = bool(retryable)


class EvolutionStablePromotionRuntimeAdmissionDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@runtime_checkable
class EvolutionStablePromotionRuntimeAdmissionControlPlaneTransport(Protocol):
    """Authenticated installation-to-Control-Plane Submission transport."""

    async def submit(
        self, submission: EvolutionStablePromotionRuntimeAdmissionSubmission
    ) -> EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt: ...


class LocalStablePromotionRuntimeAdmissionControlPlaneTransport:
    """Real in-process adapter that executes the Control Plane receive boundary."""

    def __init__(
        self,
        *,
        receiver: EvolutionStablePromotionRuntimeAdmissionDeliveryService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.receiver = receiver
        self.clock = clock

    async def submit(
        self, submission: EvolutionStablePromotionRuntimeAdmissionSubmission
    ) -> EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt:
        try:
            view = await self.receiver.receive(
                submission=submission,
                received_at=_aware(self.clock()),
            )
        except EvolutionStablePromotionRuntimeAdmissionDeliveryError as exc:
            permanent = exc.code in {
                "stable_promotion_admission_delivery_lineage_mismatch",
                "stable_promotion_admission_delivery_signature_untrusted",
                "stable_promotion_admission_delivery_receive_conflict",
                "stable_promotion_admission_delivery_received_before_signed",
            }
            raise EvolutionStablePromotionRuntimeAdmissionTransportError(
                exc.code,
                str(exc),
                retryable=not permanent,
            ) from exc
        if not view.remote_admission_delivery_authority:
            raise EvolutionStablePromotionRuntimeAdmissionTransportError(
                "stable_promotion_admission_control_plane_receipt_stale",
                "Control Plane 返回的 Runtime Admission Receipt 不具备 current authority。",
                retryable=False,
            )
        return view.receipt


class EvolutionStablePromotionRuntimeAdmissionDispatchStore:
    """Durable owner-fenced outbox state layered over the signed 7b2a1 outbox."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def enqueue(
        self,
        submission: EvolutionStablePromotionRuntimeAdmissionSubmission,
        *,
        enqueued_at: str | datetime,
    ) -> EvolutionStablePromotionRuntimeAdmissionDispatchView:
        item = EvolutionStablePromotionRuntimeAdmissionSubmission.model_validate(submission)
        now = _aware(enqueued_at)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await _require_outbound(db, item)
            row = await _row(db, item.payload.admission.admission_id)
            if row is not None:
                view = _restore_view(row)
                await _verify_chain(db, view)
                await db.rollback()
                if view.submission == item:
                    return view
                raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
                    "stable_promotion_admission_dispatch_conflict",
                    "同一 Runtime Admission 已绑定不同 dispatch Submission。",
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
                "INSERT INTO evolution_stable_promotion_runtime_admission_dispatches "
                "(admission_id, submission_id, submission_json, latest_event_json, "
                "receipt_json) VALUES (?, ?, ?, ?, NULL)",
                (
                    item.payload.admission.admission_id,
                    item.submission_id,
                    item.model_dump_json(),
                    event.model_dump_json(),
                ),
            )
            await _insert_event(db, event)
            await db.commit()
            return EvolutionStablePromotionRuntimeAdmissionDispatchView(
                submission=item,
                latest_event=event,
            )

    async def get(
        self, admission_id: str
    ) -> EvolutionStablePromotionRuntimeAdmissionDispatchView | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await _row(db, _admission_id(admission_id))
            if row is None:
                return None
            view = _restore_view(row)
            await _verify_chain(db, view)
            return view

    async def claim(
        self,
        *,
        owner_id: str,
        now: str | datetime,
        lease_seconds: int = 60,
    ) -> EvolutionStablePromotionRuntimeAdmissionDispatchView | None:
        owner = _owner_sha256(owner_id)
        timestamp = _aware(now)
        if isinstance(lease_seconds, bool) or not 3 <= lease_seconds <= 300:
            raise ValueError("Runtime Admission claim lease 必须为 3..300 秒。")
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute(
                    "SELECT * FROM evolution_stable_promotion_runtime_admission_dispatches "
                    "ORDER BY rowid LIMIT 1000"
                )
            ).fetchall()
            for row in rows:
                view = _restore_view(row)
                await _verify_chain(db, view)
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
                    await _update_event(db, view.submission.payload.admission.admission_id, claimed)
                    await db.commit()
                    return view.model_copy(update={"latest_event": claimed})
            await db.rollback()
            return None

    async def retry(
        self,
        *,
        admission_id: str,
        owner_id: str,
        claim_epoch: int,
        failure_code: str,
        now: str | datetime,
        base_seconds: float = 5.0,
        max_seconds: float = 300.0,
    ) -> EvolutionStablePromotionRuntimeAdmissionDispatchView:
        return await self._settle_failure(
            admission_id=admission_id,
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
        admission_id: str,
        owner_id: str,
        claim_epoch: int,
        failure_code: str,
        now: str | datetime,
    ) -> EvolutionStablePromotionRuntimeAdmissionDispatchView:
        return await self._settle_failure(
            admission_id=admission_id,
            owner_id=owner_id,
            claim_epoch=claim_epoch,
            failure_code=failure_code,
            now=now,
            dead_letter=True,
        )

    async def _settle_failure(
        self,
        *,
        admission_id: str,
        owner_id: str,
        claim_epoch: int,
        failure_code: str,
        now: str | datetime,
        dead_letter: bool,
        base_seconds: float = 5.0,
        max_seconds: float = 300.0,
    ) -> EvolutionStablePromotionRuntimeAdmissionDispatchView:
        timestamp = _aware(now)
        owner = _owner_sha256(owner_id)
        code = _failure_code(failure_code)
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await _row(db, _admission_id(admission_id))
            if row is None:
                raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
                    "stable_promotion_admission_dispatch_missing",
                    "Runtime Admission dispatch 不存在。",
                )
            view = _restore_view(row)
            await _verify_chain(db, view)
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
            await _update_event(db, admission_id, settled)
            await db.commit()
            return view.model_copy(update={"latest_event": settled})

    async def acknowledge(
        self,
        *,
        admission_id: str,
        owner_id: str,
        claim_epoch: int,
        receipt: EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt,
        now: str | datetime,
    ) -> EvolutionStablePromotionRuntimeAdmissionDispatchView:
        timestamp = _aware(now)
        owner = _owner_sha256(owner_id)
        item = EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt.model_validate(receipt)
        async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await _row(db, _admission_id(admission_id))
            if row is None:
                raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
                    "stable_promotion_admission_dispatch_missing",
                    "Runtime Admission dispatch 不存在。",
                )
            view = _restore_view(row)
            await _verify_chain(db, view)
            if view.latest_event.state == "acknowledged":
                await db.rollback()
                if view.receipt == item:
                    return view
                raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
                    "stable_promotion_admission_dispatch_receipt_conflict",
                    "Runtime Admission dispatch 已绑定不同 Receipt。",
                )
            _require_live_claim(
                view.latest_event,
                owner=owner,
                claim_epoch=claim_epoch,
                now=timestamp,
            )
            if not (
                stable_promotion_runtime_admission_receipt_matches_submission(
                    item, view.submission
                )
                and _aware(item.received_at) <= timestamp
            ):
                raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
                    "stable_promotion_admission_dispatch_receipt_invalid",
                    "Control Plane Receipt 与 current in-flight Submission 不一致。",
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
                "UPDATE evolution_stable_promotion_runtime_admission_dispatches SET "
                "latest_event_json = ?, receipt_json = ? WHERE admission_id = ?",
                (acknowledged.model_dump_json(), item.model_dump_json(), admission_id),
            )
            await _insert_event(db, acknowledged)
            await db.commit()
            return view.model_copy(
                update={"latest_event": acknowledged, "receipt": item}
            )


class EvolutionStablePromotionRuntimeAdmissionWorkerState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    WAITING = "waiting"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class EvolutionStablePromotionRuntimeAdmissionWorkerPolicy:
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
            raise ValueError("Runtime Admission Worker 周期间隔无效。")
        if not _finite(self.max_empty_backoff_seconds, self.interval_seconds, 604_800):
            raise ValueError("Runtime Admission Worker 空轮退避无效。")
        if not _finite(self.max_failure_backoff_seconds, self.interval_seconds, 604_800):
            raise ValueError("Runtime Admission Worker 失败退避无效。")
        if isinstance(self.claim_lease_seconds, bool) or not (
            isinstance(self.claim_lease_seconds, int)
            and 3 <= self.claim_lease_seconds <= 300
        ):
            raise ValueError("Runtime Admission Worker claim lease 无效。")
        if isinstance(self.scan_limit, bool) or not (
            isinstance(self.scan_limit, int) and 1 <= self.scan_limit <= 1000
        ):
            raise ValueError("Runtime Admission Worker scan limit 无效。")
        if not _finite(
            self.receipt_timeout_seconds, 0.1, self.claim_lease_seconds - 0.1
        ):
            raise ValueError("Runtime Admission Worker Receipt timeout 必须小于 lease。")
        if not _finite(self.retry_base_seconds, 0.1, 3600) or not _finite(
            self.retry_max_seconds, self.retry_base_seconds, 3600
        ):
            raise ValueError("Runtime Admission Worker retry backoff 无效。")
        if isinstance(self.max_attempts, bool) or not (
            isinstance(self.max_attempts, int) and 1 <= self.max_attempts <= 1000
        ):
            raise ValueError("Runtime Admission Worker retry budget 无效。")
        if not _finite(self.shutdown_drain_seconds, 0.1, 3600):
            raise ValueError("Runtime Admission Worker shutdown drain 无效。")
        if not _finite(self.jitter_ratio, 0, 0.5):
            raise ValueError("Runtime Admission Worker jitter 无效。")


@dataclass(frozen=True, slots=True)
class EvolutionStablePromotionRuntimeAdmissionPassResult:
    claimed: int = 0
    acknowledged: int = 0
    retry_scheduled: int = 0
    dead_lettered: int = 0
    failures: int = 0
    failure_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvolutionStablePromotionRuntimeAdmissionWorkerSnapshot:
    state: EvolutionStablePromotionRuntimeAdmissionWorkerState
    pass_count: int
    claimed_count: int
    acknowledged_count: int
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


class EvolutionStablePromotionRuntimeAdmissionDeliveryWorker:
    """Drain a bounded FIFO prefix through an authenticated Control Plane port."""

    def __init__(
        self,
        *,
        sender: EvolutionStablePromotionRuntimeAdmissionDeliveryService,
        store: EvolutionStablePromotionRuntimeAdmissionDispatchStore,
        transport: EvolutionStablePromotionRuntimeAdmissionControlPlaneTransport,
        policy: EvolutionStablePromotionRuntimeAdmissionWorkerPolicy,
        owner_id: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if sender.store.db_path != store.db_path:
            raise ValueError("Runtime Admission Worker sender/store 必须同源。")
        if not isinstance(
            transport, EvolutionStablePromotionRuntimeAdmissionControlPlaneTransport
        ):
            raise TypeError("transport 必须实现 authenticated Control Plane transport。")
        self.sender = sender
        self.store = store
        self.transport = transport
        self.policy = policy
        self.owner_id = (owner_id or f"stable-runtime-admission-{uuid.uuid4().hex}").strip()
        if not 1 <= len(self.owner_id) <= 256:
            raise ValueError("Runtime Admission Worker owner_id 无效。")
        self.clock = clock
        self.random_value = random_value
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._state = EvolutionStablePromotionRuntimeAdmissionWorkerState.STOPPED
        self._pass_count = self._claimed_count = self._acknowledged_count = 0
        self._retry_scheduled_count = self._dead_lettered_count = 0
        self._failure_count = self._forced_shutdown_count = 0
        self._consecutive_empty_passes = self._consecutive_failure_passes = 0
        self._next_delay_seconds = policy.interval_seconds
        self._last_failure_codes: tuple[str, ...] = ()
        self._started_at = self._last_pass_at = ""

    async def enqueue(
        self, admission_id: str
    ) -> EvolutionStablePromotionRuntimeAdmissionDispatchView:
        submission = await self.sender.prepare(admission_id=admission_id)
        view = await self.store.enqueue(submission, enqueued_at=self._now())
        self.wake()
        return view

    def start(self) -> bool:
        if self._task is not None and not self._task.done():
            return False
        self._stop_event.clear()
        self._wake_event.clear()
        self._state = EvolutionStablePromotionRuntimeAdmissionWorkerState.WAITING
        self._started_at = self._timestamp()
        self._task = asyncio.create_task(
            self._run_loop(), name="naumi-stable-runtime-admission-delivery"
        )
        return True

    async def stop(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        self._state = EvolutionStablePromotionRuntimeAdmissionWorkerState.STOPPING
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

    async def run_once(self) -> EvolutionStablePromotionRuntimeAdmissionPassResult:
        async with self._run_lock:
            return await self._run_once_locked()

    async def _run_once_locked(self) -> EvolutionStablePromotionRuntimeAdmissionPassResult:
        self._state = EvolutionStablePromotionRuntimeAdmissionWorkerState.RUNNING
        self._last_pass_at = self._timestamp()
        claimed = acknowledged = retry_scheduled = dead_lettered = failures = 0
        codes: list[str] = []
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
                codes.append("stable_promotion_admission_dispatch_claim_failed")
                break
            if view is None:
                break
            claimed += 1
            event = view.latest_event
            try:
                current = await self.sender.prepare(
                    admission_id=view.submission.payload.admission.admission_id
                )
                if current != view.submission:
                    raise EvolutionStablePromotionRuntimeAdmissionTransportError(
                        "stable_promotion_admission_dispatch_submission_stale",
                        "Current signed Submission 与 dispatch 不一致。",
                        retryable=False,
                    )
                receipt = await asyncio.wait_for(
                    self.transport.submit(view.submission),
                    timeout=self.policy.receipt_timeout_seconds,
                )
                await self.store.acknowledge(
                    admission_id=view.submission.payload.admission.admission_id,
                    owner_id=self.owner_id,
                    claim_epoch=event.claim_epoch,
                    receipt=receipt,
                    now=self._now(),
                )
                acknowledged += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code, retryable = _failure(exc)
                failures += 1
                codes.append(code)
                try:
                    if not retryable or event.attempt_count >= self.policy.max_attempts:
                        await self.store.dead_letter(
                            admission_id=view.submission.payload.admission.admission_id,
                            owner_id=self.owner_id,
                            claim_epoch=event.claim_epoch,
                            failure_code=code,
                            now=self._now(),
                        )
                        dead_lettered += 1
                    else:
                        await self.store.retry(
                            admission_id=view.submission.payload.admission.admission_id,
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
                    codes.append("stable_promotion_admission_dispatch_settlement_failed")
        result = EvolutionStablePromotionRuntimeAdmissionPassResult(
            claimed=claimed,
            acknowledged=acknowledged,
            retry_scheduled=retry_scheduled,
            dead_lettered=dead_lettered,
            failures=failures,
            failure_codes=tuple(codes),
        )
        self._record(result)
        return result

    def snapshot(self) -> EvolutionStablePromotionRuntimeAdmissionWorkerSnapshot:
        return EvolutionStablePromotionRuntimeAdmissionWorkerSnapshot(
            state=self._state,
            pass_count=self._pass_count,
            claimed_count=self._claimed_count,
            acknowledged_count=self._acknowledged_count,
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

    def _record(self, result: EvolutionStablePromotionRuntimeAdmissionPassResult) -> None:
        self._pass_count += 1
        self._claimed_count += result.claimed
        self._acknowledged_count += result.acknowledged
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
            EvolutionStablePromotionRuntimeAdmissionWorkerState.STOPPING
            if self._stop_event.is_set()
            else EvolutionStablePromotionRuntimeAdmissionWorkerState.WAITING
        )

    async def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._wait(self._next_delay_seconds)
                if not self._stop_event.is_set():
                    await self.run_once()
        finally:
            self._state = EvolutionStablePromotionRuntimeAdmissionWorkerState.STOPPED

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
            raise ValueError("Runtime Admission Worker random source 必须在 0..1。")
        factor = 1 + self.policy.jitter_ratio * (2 * float(sample) - 1)
        return max(0.0, delay * factor)


def render_stable_promotion_runtime_admission_dispatch(
    view: EvolutionStablePromotionRuntimeAdmissionDispatchView,
) -> str:
    event = view.latest_event
    return "\n".join(
        (
            "## Stable Promotion Runtime Admission Dispatch",
            "",
            f"- Admission：`{event.admission_id}`",
            f"- Submission：`{event.submission_id}`",
            f"- 状态：**{event.state}**",
            f"- Claim epoch / attempt：`{event.claim_epoch}` / `{event.attempt_count}`",
            f"- Next attempt：`{event.next_attempt_at}`",
            f"- Receipt：`{event.receipt_id or 'none'}`",
            f"- Failure：`{event.failure_code or 'none'}`",
            "- Observation Window authority：`false`",
            "- Promoted Outcome authority：`false`",
        )
    )


def render_stable_promotion_runtime_admission_worker_pass(
    result: EvolutionStablePromotionRuntimeAdmissionPassResult,
) -> str:
    return "\n".join(
        (
            "## Stable Promotion Runtime Admission Worker",
            "",
            "- 状态：**本轮完成**",
            f"- Claimed：`{result.claimed}`",
            f"- ACK：`{result.acknowledged}`",
            f"- Retry：`{result.retry_scheduled}`",
            f"- Dead letter：`{result.dead_lettered}`",
            f"- Failures：`{result.failures}`",
            f"- Failure codes：`{', '.join(result.failure_codes) or 'none'}`",
        )
    )


def render_stable_promotion_runtime_admission_worker(
    snapshot: EvolutionStablePromotionRuntimeAdmissionWorkerSnapshot,
) -> str:
    return "\n".join(
        (
            "## Stable Promotion Runtime Admission Worker",
            "",
            f"- 状态：**{snapshot.state.value}**",
            f"- Passes：`{snapshot.pass_count}`",
            f"- Claimed / ACK：`{snapshot.claimed_count}` / `{snapshot.acknowledged_count}`",
            f"- Retry / Dead letter：`{snapshot.retry_scheduled_count}` / "
            f"`{snapshot.dead_lettered_count}`",
            f"- Failures：`{snapshot.failure_count}`",
            f"- Forced shutdown：`{snapshot.forced_shutdown_count}`",
            f"- Next delay：`{snapshot.next_delay_seconds:.3f}s`",
            f"- Failure codes：`{', '.join(snapshot.last_failure_codes) or 'none'}`",
        )
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_runtime_admission_dispatches ("
        "admission_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE, "
        "submission_json TEXT NOT NULL, latest_event_json TEXT NOT NULL, receipt_json TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_promotion_runtime_admission_dispatch_events ("
        "admission_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_json TEXT NOT NULL, "
        "PRIMARY KEY(admission_id, sequence))"
    )


async def _row(db: aiosqlite.Connection, admission_id: str):
    return await (
        await db.execute(
            "SELECT * FROM evolution_stable_promotion_runtime_admission_dispatches "
            "WHERE admission_id = ?",
            (admission_id,),
        )
    ).fetchone()


async def _require_outbound(
    db: aiosqlite.Connection,
    submission: EvolutionStablePromotionRuntimeAdmissionSubmission,
) -> None:
    row = await (
        await db.execute(
            "SELECT submission_id, submission_sha256, submission_json FROM "
            "evolution_stable_promotion_runtime_admission_outbox WHERE admission_id = ?",
            (submission.payload.admission.admission_id,),
        )
    ).fetchone()
    if row is None:
        raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
            "stable_promotion_admission_dispatch_outbound_missing",
            "Runtime Admission dispatch 缺少 durable signed outbound。",
        )
    try:
        durable = EvolutionStablePromotionRuntimeAdmissionSubmission.model_validate_json(
            row["submission_json"]
        )
    except ValueError as exc:
        raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
            "stable_promotion_admission_dispatch_outbound_corrupt",
            "Runtime Admission signed outbound 已损坏。",
        ) from exc
    if not (
        durable == submission
        and row["submission_id"] == submission.submission_id
        and row["submission_sha256"] == submission.submission_sha256
    ):
        raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
            "stable_promotion_admission_dispatch_outbound_corrupt",
            "Runtime Admission signed outbound identity 不一致。",
        )


def _restore_view(row: aiosqlite.Row) -> EvolutionStablePromotionRuntimeAdmissionDispatchView:
    try:
        view = EvolutionStablePromotionRuntimeAdmissionDispatchView(
            submission=EvolutionStablePromotionRuntimeAdmissionSubmission.model_validate_json(
                row["submission_json"]
            ),
            latest_event=EvolutionStablePromotionRuntimeAdmissionDispatchEvent.model_validate_json(
                row["latest_event_json"]
            ),
            receipt=(
                None
                if row["receipt_json"] is None
                else EvolutionStablePromotionRuntimeAdmissionDeliveryReceipt.model_validate_json(
                    row["receipt_json"]
                )
            ),
        )
        if not (
            row["admission_id"] == view.submission.payload.admission.admission_id
            and row["submission_id"] == view.submission.submission_id
        ):
            raise ValueError("dispatch row identity mismatch")
        return view
    except ValueError as exc:
        raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
            "stable_promotion_admission_dispatch_store_corrupt",
            "Runtime Admission dispatch durable row 已损坏。",
        ) from exc


async def _insert_event(
    db: aiosqlite.Connection,
    event: EvolutionStablePromotionRuntimeAdmissionDispatchEvent,
) -> None:
    await db.execute(
        "INSERT INTO evolution_stable_promotion_runtime_admission_dispatch_events "
        "(admission_id, sequence, event_json) VALUES (?, ?, ?)",
        (event.admission_id, event.sequence, event.model_dump_json()),
    )


async def _update_event(
    db: aiosqlite.Connection,
    admission_id: str,
    event: EvolutionStablePromotionRuntimeAdmissionDispatchEvent,
) -> None:
    await db.execute(
        "UPDATE evolution_stable_promotion_runtime_admission_dispatches SET "
        "latest_event_json = ? WHERE admission_id = ?",
        (event.model_dump_json(), admission_id),
    )
    await _insert_event(db, event)


async def _verify_chain(
    db: aiosqlite.Connection,
    view: EvolutionStablePromotionRuntimeAdmissionDispatchView,
) -> None:
    await _require_outbound(db, view.submission)
    rows = await (
        await db.execute(
            "SELECT event_json FROM "
            "evolution_stable_promotion_runtime_admission_dispatch_events "
            "WHERE admission_id = ? ORDER BY sequence",
            (view.submission.payload.admission.admission_id,),
        )
    ).fetchall()
    try:
        events = [
            EvolutionStablePromotionRuntimeAdmissionDispatchEvent.model_validate_json(
                row[0]
            )
            for row in rows
        ]
    except ValueError as exc:
        raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
            "stable_promotion_admission_dispatch_chain_corrupt",
            "Runtime Admission dispatch event chain 无法解析。",
        ) from exc
    if not events or events[-1] != view.latest_event:
        raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
            "stable_promotion_admission_dispatch_chain_corrupt",
            "Runtime Admission dispatch event chain 与主记录不一致。",
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
            and event.admission_id == view.submission.payload.admission.admission_id
            and event.submission_id == view.submission.submission_id
            and event.submission_sha256 == view.submission.submission_sha256
            and event.previous_event_sha256
            == (None if index == 0 else events[index - 1].event_sha256)
        ):
            raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
                "stable_promotion_admission_dispatch_chain_corrupt",
                "Runtime Admission dispatch event chain 不连续。",
            )
        if index == 0:
            if not (
                event.state == "queued"
                and event.claim_epoch == 0
                and event.attempt_count == 0
            ):
                raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
                    "stable_promotion_admission_dispatch_chain_corrupt",
                    "Runtime Admission dispatch 首事件无效。",
                )
            continue
        previous = events[index - 1]
        if (previous.state, event.state) not in allowed:
            raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
                "stable_promotion_admission_dispatch_chain_corrupt",
                "Runtime Admission dispatch 状态转换无效。",
            )
        claiming = event.state == "in_flight"
        if claiming and not (
            event.claim_epoch == previous.claim_epoch + 1
            and event.attempt_count == previous.attempt_count + 1
        ):
            raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
                "stable_promotion_admission_dispatch_chain_corrupt",
                "Runtime Admission dispatch claim 计数不连续。",
            )
        if not claiming and not (
            event.claim_epoch == previous.claim_epoch
            and event.attempt_count == previous.attempt_count
        ):
            raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
                "stable_promotion_admission_dispatch_chain_corrupt",
                "Runtime Admission dispatch 非 claim 计数发生漂移。",
            )


def _event(
    *,
    submission: EvolutionStablePromotionRuntimeAdmissionSubmission,
    sequence: int,
    state: str,
    claim_epoch: int,
    attempt_count: int,
    next_attempt_at: datetime,
    occurred_at: datetime,
    owner_sha256: str | None = None,
    lease_expires_at: datetime | None = None,
    receipt_id: str | None = None,
    receipt_sha256: str | None = None,
    failure_code: str | None = None,
    previous_event_sha256: str | None = None,
) -> EvolutionStablePromotionRuntimeAdmissionDispatchEvent:
    core = {
        "schema_version": 1,
        "admission_id": submission.payload.admission.admission_id,
        "submission_id": submission.submission_id,
        "submission_sha256": submission.submission_sha256,
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
    return EvolutionStablePromotionRuntimeAdmissionDispatchEvent.model_validate(
        {
            **core,
            "event_id": f"evstablepromdispatch_{digest[:24]}",
            "event_sha256": digest,
        }
    )


def _require_live_claim(
    event: EvolutionStablePromotionRuntimeAdmissionDispatchEvent,
    *,
    owner: str,
    claim_epoch: int,
    now: datetime,
) -> None:
    if not (
        event.state == "in_flight"
        and event.owner_sha256 == owner
        and event.claim_epoch == claim_epoch
        and event.lease_expires_at is not None
        and now < _aware(event.lease_expires_at)
    ):
        raise EvolutionStablePromotionRuntimeAdmissionDispatchError(
            "stable_promotion_admission_dispatch_claim_fenced",
            "Runtime Admission dispatch claim 已失效。",
        )


def _failure(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, TimeoutError):
        return "stable_promotion_admission_dispatch_receipt_timeout", True
    if isinstance(exc, EvolutionStablePromotionRuntimeAdmissionTransportError):
        return exc.code, exc.retryable
    if isinstance(exc, EvolutionStablePromotionRuntimeAdmissionDeliveryError):
        permanent = exc.code in {
            "stable_promotion_admission_delivery_admission_missing",
            "stable_promotion_admission_delivery_admission_stale",
            "stable_promotion_admission_delivery_sources_stale",
            "stable_promotion_admission_delivery_credential_missing",
            "stable_promotion_admission_delivery_signature_untrusted",
            "stable_promotion_admission_delivery_lineage_mismatch",
            "stable_promotion_admission_delivery_outbound_conflict",
        }
        return exc.code, not permanent
    if isinstance(exc, EvolutionStablePromotionRuntimeAdmissionDispatchError):
        permanent = exc.code in {
            "stable_promotion_admission_dispatch_receipt_invalid",
            "stable_promotion_admission_dispatch_receipt_conflict",
            "stable_promotion_admission_dispatch_outbound_corrupt",
            "stable_promotion_admission_dispatch_chain_corrupt",
        }
        return exc.code, not permanent
    if isinstance(exc, (ConnectionError, OSError)):
        return "stable_promotion_admission_transport_unavailable", True
    return "stable_promotion_admission_transport_failed", True


def _admission_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(_ADMISSION_ID_RE, normalized) is None:
        raise ValueError("Runtime Admission ID 无效。")
    return normalized


def _owner_sha256(value: str) -> str:
    normalized = str(value or "").strip()
    if not 1 <= len(normalized) <= 256:
        raise ValueError("Runtime Admission dispatch owner_id 必须为 1..256 字符。")
    return hashlib.sha256(normalized.encode()).hexdigest()


def _failure_code(value: str) -> str:
    normalized = str(value or "").strip()
    if not 1 <= len(normalized) <= 128:
        raise ValueError("Runtime Admission dispatch failure code 无效。")
    return normalized


def _retry_delay(attempt: int, *, base_seconds: float, max_seconds: float) -> float:
    if not (
        _finite(base_seconds, 0.1, 3600)
        and _finite(max_seconds, base_seconds, 3600)
    ):
        raise ValueError("Runtime Admission dispatch retry backoff 无效。")
    return min(max_seconds, base_seconds * 2 ** min(max(attempt - 1, 0), 16))


def _aware(value: str | datetime) -> datetime:
    item = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if item.tzinfo is None or item.utcoffset() is None:
        raise ValueError("Runtime Admission dispatch 时间必须包含时区。")
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
    "EvolutionStablePromotionRuntimeAdmissionControlPlaneTransport",
    "EvolutionStablePromotionRuntimeAdmissionDeliveryWorker",
    "EvolutionStablePromotionRuntimeAdmissionDispatchError",
    "EvolutionStablePromotionRuntimeAdmissionDispatchEvent",
    "EvolutionStablePromotionRuntimeAdmissionDispatchStore",
    "EvolutionStablePromotionRuntimeAdmissionDispatchView",
    "EvolutionStablePromotionRuntimeAdmissionPassResult",
    "EvolutionStablePromotionRuntimeAdmissionTransportError",
    "EvolutionStablePromotionRuntimeAdmissionWorkerPolicy",
    "EvolutionStablePromotionRuntimeAdmissionWorkerSnapshot",
    "EvolutionStablePromotionRuntimeAdmissionWorkerState",
    "LocalStablePromotionRuntimeAdmissionControlPlaneTransport",
    "render_stable_promotion_runtime_admission_dispatch",
    "render_stable_promotion_runtime_admission_worker",
    "render_stable_promotion_runtime_admission_worker_pass",
]
