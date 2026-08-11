"""Durable target execution and Result return for remote stable finalization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_remote_finalization_deliveries import (
    EvolutionStableRemoteFinalizationDeliveryError,
    EvolutionStableRemoteFinalizationDeliveryPackage,
    EvolutionStableRemoteFinalizationDeliveryService,
    EvolutionStableRemoteFinalizationTargetJournal,
)
from naumi_agent.evolution.stable_remote_finalizations import (
    EvolutionStableRemoteFinalizationError,
    EvolutionStableRemoteFinalizationReceipt,
    EvolutionStableRemoteFinalizationSubmission,
    encode_stable_remote_finalization_submission,
)
from naumi_agent.release.installation_keys import ReleaseInstallationKeyService
from naumi_agent.release.population_registry import ReleaseManagedInstallationCredential
from naumi_agent.release.rollout_control_keys import (
    ReleaseRolloutControlTrustPolicyDocument,
)
from naumi_agent.release.slots import ReleaseSlotStore

_POLICY = "evolution-stable-remote-finalization-result-return-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionStableRemoteFinalizationResultReturnEvent(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-remote-finalization-result-return-v1"] = _POLICY
    event_id: str = Field(pattern=r"^evstableresultreturnevent_[0-9a-f]{24}$")
    event_sha256: str = Field(pattern=_SHA256_RE)
    delivery_id: str = Field(pattern=r"^evstableremotedelivery_[0-9a-f]{24}$")
    sequence: int = Field(ge=1, le=1_000_000)
    phase: Literal["execute", "return"]
    state: Literal["queued", "in_flight", "completed", "dead_letter"]
    owner_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    claim_epoch: int = Field(ge=0, le=1_000_000)
    attempt_count: int = Field(ge=0, le=1_000_000)
    lease_expires_at: str | None = Field(default=None, min_length=1, max_length=100)
    next_attempt_at: str = Field(min_length=1, max_length=100)
    submission_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
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
        if self.state == "in_flight" and not (self.owner_sha256 and self.lease_expires_at):
            raise ValueError("in_flight Result Return 缺少 owner/lease。")
        if self.state != "in_flight" and (
            self.owner_sha256 is not None or self.lease_expires_at is not None
        ):
            raise ValueError("非 in_flight Result Return 不得携带 owner/lease。")
        if self.phase == "execute" and (
            self.submission_sha256 is not None or self.receipt_sha256 is not None
        ):
            raise ValueError("execute phase 不得提前绑定 Result/Receipt。")
        if self.phase == "return" and self.submission_sha256 is None:
            raise ValueError("return phase 必须绑定 exact Result。")
        if self.state == "completed" and not (self.phase == "return" and self.receipt_sha256):
            raise ValueError("completed Result Return 必须绑定 Receipt。")
        if self.state != "completed" and self.receipt_sha256 is not None:
            raise ValueError("非 completed Result Return 不得绑定 Receipt。")
        if self.state == "dead_letter" and self.failure_code is None:
            raise ValueError("dead-letter Result Return 必须绑定 failure code。")
        if self.state not in {"queued", "dead_letter"} and self.failure_code is not None:
            raise ValueError("仅 retry/dead-letter 可携带 failure code。")
        core = self.model_dump(mode="json", exclude={"event_id", "event_sha256"})
        digest = _digest(core)
        if self.event_sha256 != digest or self.event_id != (
            f"evstableresultreturnevent_{digest[:24]}"
        ):
            raise ValueError("Result Return event identity 无效。")
        return self


class EvolutionStableRemoteFinalizationResultReturnView(_StrictModel):
    package: EvolutionStableRemoteFinalizationDeliveryPackage
    latest_event: EvolutionStableRemoteFinalizationResultReturnEvent
    submission: EvolutionStableRemoteFinalizationSubmission | None = None
    receipt: EvolutionStableRemoteFinalizationReceipt | None = None

    @model_validator(mode="after")
    def _artifacts(self) -> Self:
        event = self.latest_event
        if event.delivery_id != self.package.delivery_id:
            raise ValueError("Result Return event 与 Delivery Package 不一致。")
        if event.phase == "execute" and self.submission is not None:
            raise ValueError("execute phase 不得保存 submission。")
        if event.phase == "return" and not (
            self.submission is not None
            and event.submission_sha256 == _submission_sha(self.submission)
            and _submission_matches(self.package, self.submission)
        ):
            raise ValueError("Result Return submission 绑定无效。")
        if event.state == "completed" and not (
            self.receipt is not None
            and event.receipt_sha256 == self.receipt.receipt_sha256
            and _receipt_matches(self.package, self.submission, self.receipt)
        ):
            raise ValueError("Result Return Receipt 绑定无效。")
        if event.state != "completed" and self.receipt is not None:
            raise ValueError("未完成 Result Return 不得保存 Receipt。")
        return self


class EvolutionStableRemoteFinalizationResultReturnError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = _failure_code(code)


class EvolutionStableRemoteFinalizationResultTransportError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = _failure_code(code)
        self.retryable = bool(retryable)


@runtime_checkable
class EvolutionStableRemoteFinalizationResultTransport(Protocol):
    """Trusted Control Plane endpoint for one exact installation-signed Result."""

    async def submit(
        self,
        *,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
        submission: EvolutionStableRemoteFinalizationSubmission,
        late_recovery: bool,
    ) -> EvolutionStableRemoteFinalizationReceipt: ...


class LocalStableRemoteFinalizationControlPlaneTransport:
    """In-process adapter; production network authentication is a separate slice."""

    def __init__(self, service: EvolutionStableRemoteFinalizationDeliveryService) -> None:
        self.service = service

    async def submit(
        self,
        *,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
        submission: EvolutionStableRemoteFinalizationSubmission,
        late_recovery: bool,
    ) -> EvolutionStableRemoteFinalizationReceipt:
        if not _submission_matches(package, submission):
            raise EvolutionStableRemoteFinalizationResultTransportError(
                "stable_remote_result_return_submission_mismatch",
                "Result 与 Delivery Package 不一致。",
                retryable=False,
            )
        delivery = await self.service.ingest_result(
            delivery_id=package.delivery_id,
            submission_base64=encode_stable_remote_finalization_submission(submission),
            late_recovery=late_recovery,
        )
        if delivery.receipt is None:
            raise EvolutionStableRemoteFinalizationResultTransportError(
                "stable_remote_result_return_receipt_missing",
                "Control Plane 未返回 Finalization Receipt。",
            )
        return delivery.receipt


class EvolutionStableRemoteFinalizationCredentialResolver(Protocol):
    async def __call__(
        self, package: EvolutionStableRemoteFinalizationDeliveryPackage
    ) -> ReleaseManagedInstallationCredential: ...


class EvolutionStableRemoteFinalizationResultReturnStore:
    """Installation-local durable outbox with append-only fenced events."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def synchronize_from_journal(
        self,
        *,
        journal: EvolutionStableRemoteFinalizationTargetJournal,
        limit: int,
        enqueued_at: str,
    ) -> int:
        """Anti-join the immutable target journal into the durable outbox."""
        if journal.db_path != self.db_path:
            raise ValueError("Result Return Store 与 Target Journal 必须同源。")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("Result Return synchronize limit 必须为 1..1000。")
        _aware(enqueued_at)
        if not self.db_path.is_file():
            return 0
        async with aiosqlite.connect(self.db_path) as db:
            await _ensure_schema(db)
            rows = await (
                await db.execute(
                    "SELECT journal.delivery_id FROM "
                    "stable_finalization_delivery_journal AS journal LEFT JOIN "
                    "stable_finalization_result_returns AS result_return ON "
                    "result_return.delivery_id = journal.delivery_id WHERE "
                    "result_return.delivery_id IS NULL ORDER BY journal.delivery_id "
                    "LIMIT ?",
                    (limit,),
                )
            ).fetchall()
        synchronized = 0
        for row in rows:
            entry = journal.get(str(row[0]))
            if entry is None:
                continue
            _, created = await self._enqueue(
                package=entry.package,
                submission=entry.submission,
                enqueued_at=enqueued_at,
            )
            synchronized += int(created)
        return synchronized

    async def enqueue(
        self,
        *,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
        submission: EvolutionStableRemoteFinalizationSubmission | None,
        enqueued_at: str,
    ) -> EvolutionStableRemoteFinalizationResultReturnView:
        view, _ = await self._enqueue(
            package=package,
            submission=submission,
            enqueued_at=enqueued_at,
        )
        return view

    async def _enqueue(
        self,
        *,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
        submission: EvolutionStableRemoteFinalizationSubmission | None,
        enqueued_at: str,
    ) -> tuple[EvolutionStableRemoteFinalizationResultReturnView, bool]:
        timestamp = _aware(enqueued_at)
        if submission is not None and not _submission_matches(package, submission):
            raise EvolutionStableRemoteFinalizationResultReturnError(
                "stable_remote_result_return_submission_mismatch",
                "Target Result 与 Delivery Package 不一致。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await _row(db, package.delivery_id)
            if row is not None:
                view = _restore_view(row)
                await _verify_chain(db, view)
                if view.package != package:
                    raise EvolutionStableRemoteFinalizationResultReturnError(
                        "stable_remote_result_return_conflict",
                        "Result Return 已绑定不同 Delivery Package。",
                    )
                await db.rollback()
                return view, False
            phase: Literal["execute", "return"] = "return" if submission is not None else "execute"
            event = _event(
                package=package,
                sequence=1,
                phase=phase,
                state="queued",
                claim_epoch=0,
                attempt_count=0,
                next_attempt_at=timestamp,
                occurred_at=timestamp,
                submission_sha256=(None if submission is None else _submission_sha(submission)),
            )
            await db.execute(
                "INSERT INTO stable_finalization_result_returns "
                "(delivery_id, package_json, latest_event_json, submission_json, "
                "receipt_json) VALUES (?, ?, ?, ?, NULL)",
                (
                    package.delivery_id,
                    package.model_dump_json(),
                    event.model_dump_json(),
                    None if submission is None else submission.model_dump_json(),
                ),
            )
            await _insert_event(db, event)
            await db.commit()
            return (
                EvolutionStableRemoteFinalizationResultReturnView(
                    package=package, latest_event=event, submission=submission
                ),
                True,
            )

    async def get(
        self, delivery_id: str
    ) -> EvolutionStableRemoteFinalizationResultReturnView | None:
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await _row(db, _delivery_id(delivery_id))
            if row is None:
                return None
            view = _restore_view(row)
            await _verify_chain(db, view)
            return view

    async def claim(
        self,
        *,
        owner_id: str,
        now: str,
        lease_seconds: int = 60,
    ) -> EvolutionStableRemoteFinalizationResultReturnView | None:
        timestamp = _aware(now)
        owner = _owner_sha256(owner_id)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 3 <= lease_seconds <= 300
        ):
            raise ValueError("Result Return claim lease 必须为 3..300 秒。")
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute(
                    "SELECT * FROM stable_finalization_result_returns "
                    "ORDER BY delivery_id LIMIT 1000"
                )
            ).fetchall()
            for row in rows:
                view = _restore_view(row)
                await _verify_chain(db, view)
                event = view.latest_event
                due = event.state == "queued" and _aware(event.next_attempt_at) <= timestamp
                expired = (
                    event.state == "in_flight"
                    and event.lease_expires_at is not None
                    and _aware(event.lease_expires_at) <= timestamp
                )
                if due or expired:
                    claimed = _event(
                        package=view.package,
                        sequence=event.sequence + 1,
                        phase=event.phase,
                        state="in_flight",
                        owner_sha256=owner,
                        claim_epoch=event.claim_epoch + 1,
                        attempt_count=event.attempt_count + 1,
                        lease_expires_at=timestamp + timedelta(seconds=lease_seconds),
                        next_attempt_at=timestamp,
                        occurred_at=timestamp,
                        previous_event_sha256=event.event_sha256,
                        submission_sha256=event.submission_sha256,
                    )
                    await _update_event(db, view.package.delivery_id, claimed)
                    await db.commit()
                    return view.model_copy(update={"latest_event": claimed})
            await db.rollback()
            return None

    async def record_submission(
        self,
        *,
        delivery_id: str,
        owner_id: str,
        claim_epoch: int,
        submission: EvolutionStableRemoteFinalizationSubmission,
        now: str,
    ) -> EvolutionStableRemoteFinalizationResultReturnView:
        timestamp = _aware(now)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            view = await _claimed_view(
                db, delivery_id, owner_id, claim_epoch, timestamp, phase="execute"
            )
            if not _submission_matches(view.package, submission):
                raise EvolutionStableRemoteFinalizationResultReturnError(
                    "stable_remote_result_return_submission_mismatch",
                    "Result 与 claimed Delivery Package 不一致。",
                )
            digest = _submission_sha(submission)
            queued = _event(
                package=view.package,
                sequence=view.latest_event.sequence + 1,
                phase="return",
                state="queued",
                claim_epoch=view.latest_event.claim_epoch,
                attempt_count=view.latest_event.attempt_count,
                next_attempt_at=timestamp,
                occurred_at=timestamp,
                previous_event_sha256=view.latest_event.event_sha256,
                submission_sha256=digest,
            )
            await db.execute(
                "UPDATE stable_finalization_result_returns SET latest_event_json = ?, "
                "submission_json = ? WHERE delivery_id = ?",
                (queued.model_dump_json(), submission.model_dump_json(), delivery_id),
            )
            await _insert_event(db, queued)
            await db.commit()
            return view.model_copy(update={"latest_event": queued, "submission": submission})

    async def retry(
        self,
        *,
        delivery_id: str,
        owner_id: str,
        claim_epoch: int,
        failure_code: str,
        now: str,
        base_seconds: float,
        max_seconds: float,
    ) -> EvolutionStableRemoteFinalizationResultReturnView:
        timestamp = _aware(now)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            view = await _claimed_view(db, delivery_id, owner_id, claim_epoch, timestamp)
            event = view.latest_event
            queued = _event(
                package=view.package,
                sequence=event.sequence + 1,
                phase=event.phase,
                state="queued",
                claim_epoch=event.claim_epoch,
                attempt_count=event.attempt_count,
                next_attempt_at=timestamp
                + timedelta(
                    seconds=_retry_delay(
                        event.attempt_count,
                        base_seconds=base_seconds,
                        max_seconds=max_seconds,
                    )
                ),
                occurred_at=timestamp,
                previous_event_sha256=event.event_sha256,
                submission_sha256=event.submission_sha256,
                failure_code=_failure_code(failure_code),
            )
            await _update_event(db, view.package.delivery_id, queued)
            await db.commit()
            return view.model_copy(update={"latest_event": queued})

    async def dead_letter(
        self,
        *,
        delivery_id: str,
        owner_id: str,
        claim_epoch: int,
        failure_code: str,
        now: str,
    ) -> EvolutionStableRemoteFinalizationResultReturnView:
        timestamp = _aware(now)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            view = await _claimed_view(db, delivery_id, owner_id, claim_epoch, timestamp)
            event = view.latest_event
            dead = _event(
                package=view.package,
                sequence=event.sequence + 1,
                phase=event.phase,
                state="dead_letter",
                claim_epoch=event.claim_epoch,
                attempt_count=event.attempt_count,
                next_attempt_at=timestamp,
                occurred_at=timestamp,
                previous_event_sha256=event.event_sha256,
                submission_sha256=event.submission_sha256,
                failure_code=_failure_code(failure_code),
            )
            await _update_event(db, view.package.delivery_id, dead)
            await db.commit()
            return view.model_copy(update={"latest_event": dead})

    async def complete(
        self,
        *,
        delivery_id: str,
        owner_id: str,
        claim_epoch: int,
        receipt: EvolutionStableRemoteFinalizationReceipt,
        now: str,
    ) -> EvolutionStableRemoteFinalizationResultReturnView:
        timestamp = _aware(now)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            view = await _claimed_view(
                db, delivery_id, owner_id, claim_epoch, timestamp, phase="return"
            )
            if not _receipt_matches(view.package, view.submission, receipt):
                raise EvolutionStableRemoteFinalizationResultReturnError(
                    "stable_remote_result_return_receipt_mismatch",
                    "Control Plane Receipt 与 exact package/result 不一致。",
                )
            event = view.latest_event
            completed = _event(
                package=view.package,
                sequence=event.sequence + 1,
                phase="return",
                state="completed",
                claim_epoch=event.claim_epoch,
                attempt_count=event.attempt_count,
                next_attempt_at=timestamp,
                occurred_at=timestamp,
                previous_event_sha256=event.event_sha256,
                submission_sha256=event.submission_sha256,
                receipt_sha256=receipt.receipt_sha256,
            )
            await db.execute(
                "UPDATE stable_finalization_result_returns SET latest_event_json = ?, "
                "receipt_json = ? WHERE delivery_id = ?",
                (completed.model_dump_json(), receipt.model_dump_json(), delivery_id),
            )
            await _insert_event(db, completed)
            await db.commit()
            return view.model_copy(update={"latest_event": completed, "receipt": receipt})


class EvolutionStableRemoteFinalizationResultReturnWorkerState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    WAITING = "waiting"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class EvolutionStableRemoteFinalizationResultReturnWorkerPolicy:
    interval_seconds: float = 30.0
    max_empty_backoff_seconds: float = 300.0
    max_failure_backoff_seconds: float = 300.0
    journal_scan_limit: int = 20
    return_scan_limit: int = 20
    claim_lease_seconds: int = 60
    result_timeout_seconds: float = 20.0
    retry_base_seconds: float = 5.0
    retry_max_seconds: float = 300.0
    max_attempts: int = 8
    shutdown_drain_seconds: float = 25.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if not _finite(self.interval_seconds, 0.1, 86_400):
            raise ValueError("Result Return Worker 周期间隔无效。")
        if not _finite(self.max_empty_backoff_seconds, self.interval_seconds, 604_800):
            raise ValueError("Result Return Worker 空轮退避无效。")
        if not _finite(self.max_failure_backoff_seconds, self.interval_seconds, 604_800):
            raise ValueError("Result Return Worker 失败退避无效。")
        for label, value in (
            ("journal_scan_limit", self.journal_scan_limit),
            ("return_scan_limit", self.return_scan_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
                raise ValueError(f"Result Return Worker {label} 无效。")
        if (
            isinstance(self.claim_lease_seconds, bool)
            or not isinstance(self.claim_lease_seconds, int)
            or not 3 <= self.claim_lease_seconds <= 300
        ):
            raise ValueError("Result Return Worker claim lease 无效。")
        if not _finite(
            self.result_timeout_seconds, 0.1, self.claim_lease_seconds - 0.1
        ):
            raise ValueError("Result Return timeout 必须小于 claim lease。")
        if not _finite(self.retry_base_seconds, 0.1, 3600) or not _finite(
            self.retry_max_seconds, self.retry_base_seconds, 3600
        ):
            raise ValueError("Result Return Worker retry backoff 无效。")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 1000
        ):
            raise ValueError("Result Return Worker retry budget 无效。")
        if not _finite(self.shutdown_drain_seconds, 0.1, 3600):
            raise ValueError("Result Return Worker shutdown drain 无效。")
        if not _finite(self.jitter_ratio, 0, 0.5):
            raise ValueError("Result Return Worker jitter 无效。")


@dataclass(frozen=True, slots=True)
class EvolutionStableRemoteFinalizationResultReturnPassResult:
    synchronized: int = 0
    claimed: int = 0
    executed: int = 0
    returned: int = 0
    retry_scheduled: int = 0
    dead_lettered: int = 0
    failures: int = 0
    failure_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvolutionStableRemoteFinalizationResultReturnWorkerSnapshot:
    state: EvolutionStableRemoteFinalizationResultReturnWorkerState
    pass_count: int
    synchronized_count: int
    claimed_count: int
    executed_count: int
    returned_count: int
    retry_scheduled_count: int
    dead_lettered_count: int
    failure_count: int
    forced_shutdown_count: int
    next_delay_seconds: float
    last_failure_codes: tuple[str, ...]
    started_at: str
    last_pass_at: str


class EvolutionStableRemoteFinalizationResultReturnWorker:
    """Execute ACKed target grants and durably return exact signed Results."""

    def __init__(
        self,
        *,
        journal: EvolutionStableRemoteFinalizationTargetJournal,
        store: EvolutionStableRemoteFinalizationResultReturnStore,
        release_slot_store: ReleaseSlotStore,
        installation_key_service: ReleaseInstallationKeyService,
        trust_policy_provider: Callable[[], ReleaseRolloutControlTrustPolicyDocument],
        credential_resolver: EvolutionStableRemoteFinalizationCredentialResolver,
        transport: EvolutionStableRemoteFinalizationResultTransport,
        policy: EvolutionStableRemoteFinalizationResultReturnWorkerPolicy,
        owner_id: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if not (
            journal.db_path == store.db_path
            and journal.release_root == release_slot_store.release_root
            and journal.release_root == installation_key_service.release_root
        ):
            raise ValueError("Result Return Journal/Store/Release/key root 必须一致。")
        if not isinstance(transport, EvolutionStableRemoteFinalizationResultTransport):
            raise TypeError("transport 必须实现 Result Return transport。")
        self.journal = journal
        self.store = store
        self.release_slot_store = release_slot_store
        self.installation_key_service = installation_key_service
        self.trust_policy_provider = trust_policy_provider
        self.credential_resolver = credential_resolver
        self.transport = transport
        self.policy = policy
        self.owner_id = (owner_id or f"stable-result-return-{uuid.uuid4().hex}").strip()
        if not 1 <= len(self.owner_id) <= 256:
            raise ValueError("Result Return Worker owner_id 无效。")
        self.clock = clock
        self.random_value = random_value
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._state = EvolutionStableRemoteFinalizationResultReturnWorkerState.STOPPED
        self._pass_count = self._synchronized_count = self._claimed_count = 0
        self._executed_count = self._returned_count = self._retry_scheduled_count = 0
        self._dead_lettered_count = self._failure_count = self._forced_shutdown_count = 0
        self._empty_passes = self._failure_passes = 0
        self._next_delay_seconds = policy.interval_seconds
        self._last_failure_codes: tuple[str, ...] = ()
        self._started_at = self._last_pass_at = ""

    def start(self) -> bool:
        if self._task is not None and not self._task.done():
            return False
        self._stop_event.clear()
        self._wake_event.clear()
        self._state = EvolutionStableRemoteFinalizationResultReturnWorkerState.WAITING
        self._started_at = self._timestamp()
        self._task = asyncio.create_task(
            self._run_loop(), name="naumi-stable-finalization-result-return"
        )
        return True

    async def stop(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        self._state = EvolutionStableRemoteFinalizationResultReturnWorkerState.STOPPING
        self._stop_event.set()
        self._wake_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.policy.shutdown_drain_seconds)
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

    async def run_once(self) -> EvolutionStableRemoteFinalizationResultReturnPassResult:
        async with self._run_lock:
            return await self._run_once_locked()

    async def _run_once_locked(self) -> EvolutionStableRemoteFinalizationResultReturnPassResult:
        self._state = EvolutionStableRemoteFinalizationResultReturnWorkerState.RUNNING
        self._last_pass_at = self._timestamp()
        synchronized = claimed = executed = returned = retries = dead = failures = 0
        codes: list[str] = []
        try:
            synchronized = await self.store.synchronize_from_journal(
                journal=self.journal,
                limit=self.policy.journal_scan_limit,
                enqueued_at=self._timestamp(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            codes.append("stable_remote_result_return_synchronize_failed")

        for _ in range(self.policy.return_scan_limit):
            if self._stop_event.is_set():
                break
            try:
                view = await self.store.claim(
                    owner_id=self.owner_id,
                    now=self._timestamp(),
                    lease_seconds=self.policy.claim_lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                codes.append("stable_remote_result_return_claim_failed")
                break
            if view is None:
                break
            claimed += 1
            event = view.latest_event
            try:
                if event.phase == "execute":
                    submission = await self._execute(view.package)
                    await self.store.record_submission(
                        delivery_id=view.package.delivery_id,
                        owner_id=self.owner_id,
                        claim_epoch=event.claim_epoch,
                        submission=submission,
                        now=self._timestamp(),
                    )
                    executed += 1
                else:
                    if view.submission is None:
                        raise EvolutionStableRemoteFinalizationResultReturnError(
                            "stable_remote_result_return_submission_missing",
                            "return phase 缺少 signed Result。",
                        )
                    receipt = await asyncio.wait_for(
                        self.transport.submit(
                            package=view.package,
                            submission=view.submission,
                            late_recovery=(
                                self._now()
                                >= _aware(view.package.execution_package.grant.expires_at)
                            ),
                        ),
                        timeout=self.policy.result_timeout_seconds,
                    )
                    await self.store.complete(
                        delivery_id=view.package.delivery_id,
                        owner_id=self.owner_id,
                        claim_epoch=event.claim_epoch,
                        receipt=receipt,
                        now=self._timestamp(),
                    )
                    returned += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code, retryable = _failure(exc, phase=event.phase)
                failures += 1
                codes.append(code)
                try:
                    if not retryable or event.attempt_count >= self.policy.max_attempts:
                        await self.store.dead_letter(
                            delivery_id=view.package.delivery_id,
                            owner_id=self.owner_id,
                            claim_epoch=event.claim_epoch,
                            failure_code=code,
                            now=self._timestamp(),
                        )
                        dead += 1
                    else:
                        await self.store.retry(
                            delivery_id=view.package.delivery_id,
                            owner_id=self.owner_id,
                            claim_epoch=event.claim_epoch,
                            failure_code=code,
                            now=self._timestamp(),
                            base_seconds=self.policy.retry_base_seconds,
                            max_seconds=self.policy.retry_max_seconds,
                        )
                        retries += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failures += 1
                    codes.append("stable_remote_result_return_settlement_failed")
        result = EvolutionStableRemoteFinalizationResultReturnPassResult(
            synchronized=synchronized,
            claimed=claimed,
            executed=executed,
            returned=returned,
            retry_scheduled=retries,
            dead_lettered=dead,
            failures=failures,
            failure_codes=tuple(codes),
        )
        self._record(result)
        return result

    async def _execute(
        self, package: EvolutionStableRemoteFinalizationDeliveryPackage
    ) -> EvolutionStableRemoteFinalizationSubmission:
        credential = await asyncio.wait_for(
            self.credential_resolver(package),
            timeout=self.policy.result_timeout_seconds,
        )
        trust_policy = self.trust_policy_provider()
        entry = self.journal.get(package.delivery_id)
        if entry is None:
            raise EvolutionStableRemoteFinalizationResultReturnError(
                "stable_remote_result_return_journal_missing",
                "Target Journal Delivery 不存在。",
            )
        recover = entry.state == "writer_committed" or (
            self._now() >= _aware(package.execution_package.grant.expires_at)
        )
        # Run in this task: cancellation cannot leave an unobserved background writer.
        return self.journal.execute(
            package=package,
            trust_policy=trust_policy,
            credential=credential,
            release_slot_store=self.release_slot_store,
            installation_key_service=self.installation_key_service,
            recover_existing_only=recover,
            clock=self.clock,
        )

    def snapshot(self) -> EvolutionStableRemoteFinalizationResultReturnWorkerSnapshot:
        return EvolutionStableRemoteFinalizationResultReturnWorkerSnapshot(
            state=self._state,
            pass_count=self._pass_count,
            synchronized_count=self._synchronized_count,
            claimed_count=self._claimed_count,
            executed_count=self._executed_count,
            returned_count=self._returned_count,
            retry_scheduled_count=self._retry_scheduled_count,
            dead_lettered_count=self._dead_lettered_count,
            failure_count=self._failure_count,
            forced_shutdown_count=self._forced_shutdown_count,
            next_delay_seconds=self._next_delay_seconds,
            last_failure_codes=self._last_failure_codes,
            started_at=self._started_at,
            last_pass_at=self._last_pass_at,
        )

    def _record(self, result: EvolutionStableRemoteFinalizationResultReturnPassResult) -> None:
        self._pass_count += 1
        self._synchronized_count += result.synchronized
        self._claimed_count += result.claimed
        self._executed_count += result.executed
        self._returned_count += result.returned
        self._retry_scheduled_count += result.retry_scheduled
        self._dead_lettered_count += result.dead_lettered
        self._failure_count += result.failures
        self._last_failure_codes = result.failure_codes
        if result.failures:
            self._empty_passes = 0
            self._failure_passes += 1
            delay = min(
                self.policy.max_failure_backoff_seconds,
                self.policy.interval_seconds * 2 ** min(20, self._failure_passes - 1),
            )
        elif result.claimed == 0 and result.synchronized == 0:
            self._failure_passes = 0
            self._empty_passes += 1
            delay = min(
                self.policy.max_empty_backoff_seconds,
                self.policy.interval_seconds * 2 ** min(20, self._empty_passes),
            )
        else:
            self._empty_passes = self._failure_passes = 0
            delay = self.policy.interval_seconds
        sample = self.random_value()
        if not _finite(sample, 0, 1):
            raise ValueError("Result Return Worker random source 必须在 0..1。")
        self._next_delay_seconds = max(
            0.0, delay * (1 + self.policy.jitter_ratio * (2 * float(sample) - 1))
        )
        self._state = (
            EvolutionStableRemoteFinalizationResultReturnWorkerState.STOPPING
            if self._stop_event.is_set()
            else EvolutionStableRemoteFinalizationResultReturnWorkerState.WAITING
        )

    async def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._wait(self._next_delay_seconds)
                if not self._stop_event.is_set():
                    await self.run_once()
        finally:
            self._state = EvolutionStableRemoteFinalizationResultReturnWorkerState.STOPPED

    async def _wait(self, delay: float) -> None:
        stop = asyncio.create_task(self._stop_event.wait())
        wake = asyncio.create_task(self._wake_event.wait())
        try:
            done, _ = await asyncio.wait(
                {stop, wake}, timeout=max(0.0, delay), return_when=asyncio.FIRST_COMPLETED
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
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("Result Return Worker 时钟必须包含时区。")
        return value.astimezone(UTC)

    def _timestamp(self) -> str:
        return self._now().isoformat()


def render_stable_remote_finalization_result_return_pass(
    result: EvolutionStableRemoteFinalizationResultReturnPassResult,
) -> str:
    return "\n".join(
        (
            "## Remote Finalization Result Return Worker",
            "",
            "- 状态：**本轮完成**",
            f"- Synchronized：`{result.synchronized}`",
            f"- Claimed：`{result.claimed}`",
            f"- Writer/Result：`{result.executed}`",
            f"- Receipt returned：`{result.returned}`",
            f"- Retry：`{result.retry_scheduled}`",
            f"- Dead letter：`{result.dead_lettered}`",
            f"- Failures：`{result.failures}`",
            f"- Failure codes：`{', '.join(result.failure_codes) or 'none'}`",
        )
    )


def render_stable_remote_finalization_result_return_worker(
    snapshot: EvolutionStableRemoteFinalizationResultReturnWorkerSnapshot,
) -> str:
    return "\n".join(
        (
            "## Remote Finalization Result Return Worker",
            "",
            f"- 状态：**{snapshot.state.value}**",
            f"- Passes：`{snapshot.pass_count}`",
            f"- Synchronized：`{snapshot.synchronized_count}`",
            f"- Claimed：`{snapshot.claimed_count}`",
            f"- Writer/Result：`{snapshot.executed_count}`",
            f"- Receipt returned：`{snapshot.returned_count}`",
            f"- Retry：`{snapshot.retry_scheduled_count}`",
            f"- Dead letter：`{snapshot.dead_lettered_count}`",
            f"- Failures：`{snapshot.failure_count}`",
            f"- Forced shutdown：`{snapshot.forced_shutdown_count}`",
            f"- Next delay：`{snapshot.next_delay_seconds:.3f}s`",
            f"- Failure codes：`{', '.join(snapshot.last_failure_codes) or 'none'}`",
        )
    )


async def _claimed_view(
    db,
    delivery_id: str,
    owner_id: str,
    claim_epoch: int,
    timestamp: datetime,
    *,
    phase: Literal["execute", "return"] | None = None,
) -> EvolutionStableRemoteFinalizationResultReturnView:
    row = await _row(db, _delivery_id(delivery_id))
    if row is None:
        raise EvolutionStableRemoteFinalizationResultReturnError(
            "stable_remote_result_return_missing", "Result Return 不存在。"
        )
    view = _restore_view(row)
    await _verify_chain(db, view)
    event = view.latest_event
    if not (
        event.state == "in_flight"
        and (phase is None or event.phase == phase)
        and event.owner_sha256 == _owner_sha256(owner_id)
        and event.claim_epoch == claim_epoch
        and event.lease_expires_at is not None
        and timestamp < _aware(event.lease_expires_at)
    ):
        raise EvolutionStableRemoteFinalizationResultReturnError(
            "stable_remote_result_return_claim_fenced",
            "Result Return claim 已失效。",
        )
    return view


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS stable_finalization_result_returns ("
        "delivery_id TEXT PRIMARY KEY, package_json TEXT NOT NULL, "
        "latest_event_json TEXT NOT NULL, submission_json TEXT, receipt_json TEXT)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS stable_finalization_result_return_events ("
        "delivery_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_json TEXT NOT NULL, "
        "PRIMARY KEY(delivery_id, sequence))"
    )


async def _row(db, delivery_id: str):
    return await (
        await db.execute(
            "SELECT * FROM stable_finalization_result_returns WHERE delivery_id = ?",
            (delivery_id,),
        )
    ).fetchone()


def _restore_view(row) -> EvolutionStableRemoteFinalizationResultReturnView:
    return EvolutionStableRemoteFinalizationResultReturnView(
        package=EvolutionStableRemoteFinalizationDeliveryPackage.model_validate_json(
            row["package_json"]
        ),
        latest_event=(
            EvolutionStableRemoteFinalizationResultReturnEvent.model_validate_json(
                row["latest_event_json"]
            )
        ),
        submission=None
        if row["submission_json"] is None
        else (
            EvolutionStableRemoteFinalizationSubmission.model_validate_json(row["submission_json"])
        ),
        receipt=None
        if row["receipt_json"] is None
        else (EvolutionStableRemoteFinalizationReceipt.model_validate_json(row["receipt_json"])),
    )


async def _insert_event(db, event) -> None:
    await db.execute(
        "INSERT INTO stable_finalization_result_return_events "
        "(delivery_id, sequence, event_json) VALUES (?, ?, ?)",
        (event.delivery_id, event.sequence, event.model_dump_json()),
    )


async def _update_event(db, delivery_id: str, event) -> None:
    await db.execute(
        "UPDATE stable_finalization_result_returns SET latest_event_json = ? WHERE delivery_id = ?",
        (event.model_dump_json(), delivery_id),
    )
    await _insert_event(db, event)


async def _verify_chain(db, view) -> None:
    rows = await (
        await db.execute(
            "SELECT event_json FROM stable_finalization_result_return_events "
            "WHERE delivery_id = ? ORDER BY sequence",
            (view.package.delivery_id,),
        )
    ).fetchall()
    events = [
        EvolutionStableRemoteFinalizationResultReturnEvent.model_validate_json(row[0])
        for row in rows
    ]
    if not events or events[-1] != view.latest_event:
        raise EvolutionStableRemoteFinalizationResultReturnError(
            "stable_remote_result_return_chain_corrupt",
            "Result Return event chain 与主记录不一致。",
        )
    allowed = {
        ("execute", "queued", "execute", "in_flight"),
        ("execute", "in_flight", "execute", "queued"),
        ("execute", "in_flight", "execute", "in_flight"),
        ("execute", "in_flight", "execute", "dead_letter"),
        ("execute", "in_flight", "return", "queued"),
        ("return", "queued", "return", "in_flight"),
        ("return", "in_flight", "return", "queued"),
        ("return", "in_flight", "return", "in_flight"),
        ("return", "in_flight", "return", "completed"),
        ("return", "in_flight", "return", "dead_letter"),
    }
    for index, event in enumerate(events):
        previous = None if index == 0 else events[index - 1]
        if event.sequence != index + 1 or event.previous_event_sha256 != (
            None if previous is None else previous.event_sha256
        ):
            raise EvolutionStableRemoteFinalizationResultReturnError(
                "stable_remote_result_return_chain_corrupt",
                "Result Return event chain 不连续。",
            )
        if previous is None:
            if not (
                event.state == "queued" and event.claim_epoch == 0 and event.attempt_count == 0
            ):
                raise EvolutionStableRemoteFinalizationResultReturnError(
                    "stable_remote_result_return_chain_corrupt",
                    "Result Return 首事件无效。",
                )
            continue
        if (previous.phase, previous.state, event.phase, event.state) not in allowed:
            raise EvolutionStableRemoteFinalizationResultReturnError(
                "stable_remote_result_return_chain_corrupt",
                "Result Return 状态转换无效。",
            )
        claimed = event.state == "in_flight"
        if claimed and not (
            event.claim_epoch == previous.claim_epoch + 1
            and event.attempt_count == previous.attempt_count + 1
        ):
            raise EvolutionStableRemoteFinalizationResultReturnError(
                "stable_remote_result_return_chain_corrupt",
                "Result Return claim 计数不连续。",
            )
        if not claimed and not (
            event.claim_epoch == previous.claim_epoch
            and event.attempt_count == previous.attempt_count
        ):
            raise EvolutionStableRemoteFinalizationResultReturnError(
                "stable_remote_result_return_chain_corrupt",
                "Result Return 非 claim 计数发生漂移。",
            )


def _event(
    *,
    package,
    sequence,
    phase,
    state,
    claim_epoch,
    attempt_count,
    next_attempt_at,
    occurred_at,
    owner_sha256=None,
    lease_expires_at=None,
    submission_sha256=None,
    receipt_sha256=None,
    failure_code=None,
    previous_event_sha256=None,
):
    core = {
        "schema_version": 1,
        "policy_version": _POLICY,
        "delivery_id": package.delivery_id,
        "sequence": sequence,
        "phase": phase,
        "state": state,
        "owner_sha256": owner_sha256,
        "claim_epoch": claim_epoch,
        "attempt_count": attempt_count,
        "lease_expires_at": (
            None if lease_expires_at is None else _aware(lease_expires_at).isoformat()
        ),
        "next_attempt_at": _aware(next_attempt_at).isoformat(),
        "submission_sha256": submission_sha256,
        "receipt_sha256": receipt_sha256,
        "failure_code": failure_code,
        "occurred_at": _aware(occurred_at).isoformat(),
        "previous_event_sha256": previous_event_sha256,
    }
    digest = _digest(core)
    return EvolutionStableRemoteFinalizationResultReturnEvent.model_validate(
        {
            **core,
            "event_id": f"evstableresultreturnevent_{digest[:24]}",
            "event_sha256": digest,
        }
    )


def _submission_matches(package, submission) -> bool:
    result = submission.result
    execution = package.execution_package
    return bool(
        result.grant_id == execution.grant.grant_id
        and result.grant_sha256 == execution.grant.grant_sha256
        and result.authorization_id == execution.grant.authorization_id
        and result.authorization_sha256 == execution.grant.authorization_sha256
        and result.installation_member_id == package.installation_member_id
    )


def _receipt_matches(package, submission, receipt) -> bool:
    return bool(
        submission is not None
        and receipt.execution_package == package.execution_package
        and receipt.submission == submission
    )


def _submission_sha(submission) -> str:
    return _digest(submission.model_dump(mode="json"))


def _failure(exc: Exception, *, phase: str) -> tuple[str, bool]:
    if isinstance(exc, TimeoutError):
        return f"stable_remote_result_return_{phase}_timeout", True
    if isinstance(exc, EvolutionStableRemoteFinalizationResultTransportError):
        return exc.code, exc.retryable
    if isinstance(exc, EvolutionStableRemoteFinalizationResultReturnError):
        return exc.code, exc.code not in {
            "stable_remote_result_return_submission_mismatch",
            "stable_remote_result_return_receipt_mismatch",
            "stable_remote_result_return_chain_corrupt",
        }
    if isinstance(exc, EvolutionStableRemoteFinalizationDeliveryError):
        return exc.code, exc.code not in {
            "stable_remote_delivery_journal_conflict",
            "stable_remote_delivery_journal_corrupt",
        }
    if isinstance(exc, EvolutionStableRemoteFinalizationError):
        permanent = exc.code in {
            "stable_remote_finalization_grant_expired",
            "stable_remote_finalization_recovery_fact_missing",
            "stable_remote_finalization_recovery_fact_outside_window",
            "stable_remote_finalization_recovery_binding_mismatch",
            "stable_remote_finalization_credential_mismatch",
        }
        return exc.code, not permanent
    if isinstance(exc, (OSError, ConnectionError)):
        return "stable_remote_result_return_transport_unavailable", True
    return f"stable_remote_result_return_{phase}_failed", True


def _delivery_id(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("evstableremotedelivery_"):
        raise ValueError("Delivery ID 无效。")
    return value


def _owner_sha256(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 256:
        raise ValueError("Result Return owner_id 必须为 1..256 个字符。")
    return hashlib.sha256(value.strip().encode()).hexdigest()


def _failure_code(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("Result Return failure_code 无效。")
    return normalized


def _retry_delay(attempt: int, *, base_seconds: float, max_seconds: float) -> float:
    if not _finite(base_seconds, 0.1, 3600) or not _finite(max_seconds, base_seconds, 3600):
        raise ValueError("Result Return retry backoff 无效。")
    return min(max_seconds, base_seconds * 2 ** min(max(attempt - 1, 0), 16))


def _aware(value) -> datetime:
    item = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if item.tzinfo is None or item.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return item.astimezone(UTC)


def _digest(value) -> str:
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
    "EvolutionStableRemoteFinalizationCredentialResolver",
    "EvolutionStableRemoteFinalizationResultReturnError",
    "EvolutionStableRemoteFinalizationResultReturnEvent",
    "EvolutionStableRemoteFinalizationResultReturnPassResult",
    "EvolutionStableRemoteFinalizationResultReturnStore",
    "EvolutionStableRemoteFinalizationResultReturnView",
    "EvolutionStableRemoteFinalizationResultReturnWorker",
    "EvolutionStableRemoteFinalizationResultReturnWorkerPolicy",
    "EvolutionStableRemoteFinalizationResultReturnWorkerSnapshot",
    "EvolutionStableRemoteFinalizationResultReturnWorkerState",
    "EvolutionStableRemoteFinalizationResultTransport",
    "EvolutionStableRemoteFinalizationResultTransportError",
    "LocalStableRemoteFinalizationControlPlaneTransport",
    "render_stable_remote_finalization_result_return_pass",
    "render_stable_remote_finalization_result_return_worker",
]
