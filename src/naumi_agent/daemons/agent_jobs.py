"""Durable encrypted admission and fenced lifecycle authority for Agent jobs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import stat
import struct
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import aiosqlite

from naumi_agent.config.credentials import resolve_runtime_payload_key
from naumi_agent.daemons.agent_worker_contract import (
    AgentWorkerRequest,
    AgentWorkerResult,
    AgentWorkerResultStatus,
)
from naumi_agent.safety.payload_envelope import (
    PayloadEnvelope,
    PayloadEnvelopeError,
    RuntimePayloadKey,
    open_runtime_payload,
    seal_runtime_payload,
)

AGENT_JOB_SCHEMA_VERSION = 4
_PAYLOAD_MAGIC = b"NAUMI_AGENT_JOB_PAYLOAD_V1\x00"
_TERMINAL_PAYLOAD_MAGIC = b"NAUMI_AGENT_JOB_TERMINAL_PAYLOAD_V1\x00"
_TERMINAL_PAYLOAD_AAD = b"NAUMI_AGENT_JOB_TERMINAL_AAD_V1\x00"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TASK_ID_BYTES = 512
_MAX_SESSION_ID_BYTES = 512
_MAX_TASK_BYTES = 2 * 1024**2
_MAX_CONTEXT_BYTES = 16 * 1024**2
_MAX_RESPONSE_BYTES = 16 * 1024**2
_MAX_ERROR_BYTES = 1024**2
_MAX_TOPIC_BYTES = 768
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_LEASE_SECONDS = 24 * 60 * 60
_PAYLOAD_FIELDS = (
    ("task_id", _MAX_TASK_ID_BYTES, False),
    ("session_id", _MAX_SESSION_ID_BYTES, True),
    ("task", _MAX_TASK_BYTES, False),
    ("context", _MAX_CONTEXT_BYTES, True),
    ("message_topic", _MAX_TOPIC_BYTES, False),
)


class AgentJobState(StrEnum):
    ADMITTED = "admitted"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"
    MAX_TURNS = "max_turns"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class AgentJobPublicationState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PUBLISHED = "published"


TERMINAL_AGENT_JOB_STATES = frozenset(
    {
        AgentJobState.COMPLETED,
        AgentJobState.ERROR,
        AgentJobState.TIMEOUT,
        AgentJobState.MAX_TURNS,
        AgentJobState.CANCELLED,
        AgentJobState.UNKNOWN,
    }
)


class AgentJobError(RuntimeError):
    """Raised when durable Agent job state cannot be trusted."""


class AgentJobConflictError(AgentJobError):
    """Raised when an immutable request identity is reused for other facts."""


class AgentJobKeyUnavailableError(AgentJobError):
    """Raised when encrypted Agent job state cannot resolve its Runtime key."""


class AgentJobLifecycleConflictError(AgentJobError):
    """Raised when a stale owner or invalid state attempts a transition."""


class AgentJobCapacityExhaustedError(AgentJobError):
    """Raised when durable Agent active or waiting capacity is exhausted."""


@dataclass(frozen=True, slots=True)
class AgentJobPublicationReceipt:
    schema_version: int
    publication_id: str
    job_id: str
    sequence: int
    request_sha256: str
    result_sha256: str
    state: AgentJobPublicationState
    owner_id: str | None
    claim_epoch: int
    claim_expires_at: str | None
    attempt_count: int
    delivery_sha256: str | None
    published_at: str | None
    reason_code: str
    occurred_at: str
    previous_receipt_sha256: str | None
    receipt_sha256: str
    authentication_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("AgentJob publication schema_version 必须为 1。")
        _require_identifier(self.publication_id, field="publication_id")
        _require_identifier(self.job_id, field="job_id")
        _require_positive_int(self.sequence, field="sequence")
        _require_sha256(self.request_sha256, field="request_sha256")
        _require_sha256(self.result_sha256, field="result_sha256")
        if not isinstance(self.state, AgentJobPublicationState):
            raise TypeError("AgentJob publication state 类型无效。")
        if self.owner_id is not None:
            _require_identifier(self.owner_id, field="owner_id")
        if (
            isinstance(self.claim_epoch, bool)
            or not isinstance(self.claim_epoch, int)
            or self.claim_epoch < 0
        ):
            raise ValueError("AgentJob publication claim_epoch 必须是非负整数。")
        if self.claim_expires_at is not None:
            _aware_time(self.claim_expires_at, field="claim_expires_at")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count < 0
        ):
            raise ValueError("AgentJob publication attempt_count 必须是非负整数。")
        if self.delivery_sha256 is not None:
            _require_sha256(self.delivery_sha256, field="delivery_sha256")
        if self.published_at is not None:
            _aware_time(self.published_at, field="published_at")
        _require_identifier(self.reason_code, field="reason_code")
        _aware_time(self.occurred_at, field="occurred_at")
        if self.previous_receipt_sha256 is not None:
            _require_sha256(
                self.previous_receipt_sha256,
                field="previous_receipt_sha256",
            )
        _require_sha256(self.receipt_sha256, field="receipt_sha256")
        _require_sha256(
            self.authentication_sha256,
            field="authentication_sha256",
        )
        if not hmac.compare_digest(
            self.receipt_sha256,
            _digest(_publication_receipt_payload(self)),
        ):
            raise ValueError("AgentJob publication receipt 摘要校验失败。")
        _validate_publication_receipt_semantics(self)


@dataclass(frozen=True, slots=True)
class StoredAgentJobPublication:
    publication_id: str
    job_id: str
    request_sha256: str
    result_sha256: str
    state: AgentJobPublicationState
    owner_id: str | None
    claim_epoch: int
    claim_expires_at: str | None
    attempt_count: int
    created_at: str
    published_at: str | None
    latest_receipt: AgentJobPublicationReceipt


@dataclass(frozen=True, slots=True)
class AgentJobPublicationTransition:
    publication: StoredAgentJobPublication
    applied: bool


@dataclass(frozen=True, slots=True)
class AgentJobPublicationBacklog:
    pending: int
    live_claimed: int
    expired_claims: int
    assessed_at: str

    def __post_init__(self) -> None:
        for name in ("pending", "live_claimed", "expired_claims"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数。")
        _aware_time(self.assessed_at, field="assessed_at")


@dataclass(frozen=True, slots=True)
class AgentJobPayload:
    """Raw dispatch payload returned only after a live claim is authenticated."""

    task_id: str = field(repr=False)
    session_id: str = field(repr=False)
    task: str = field(repr=False)
    context: str = field(repr=False)
    message_topic: str = field(repr=False)

    def __post_init__(self) -> None:
        for name, maximum, allow_empty in _PAYLOAD_FIELDS:
            _require_text(
                getattr(self, name),
                field=name,
                maximum=maximum,
                allow_empty=allow_empty,
            )


@dataclass(frozen=True, slots=True)
class AgentJobTerminalPayload:
    """Raw terminal content encrypted at the same commit as its result receipt."""

    response: str = field(repr=False)
    error: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_text(
            self.response,
            field="response",
            maximum=_MAX_RESPONSE_BYTES,
            allow_empty=True,
        )
        _require_text(
            self.error,
            field="error",
            maximum=_MAX_ERROR_BYTES,
            allow_empty=True,
        )


@dataclass(frozen=True, slots=True)
class AgentJobLifecycleReceipt:
    schema_version: int
    receipt_id: str
    job_id: str
    sequence: int
    previous_state: AgentJobState | None
    state: AgentJobState
    owner_id: str | None
    claim_epoch: int
    claim_expires_at: str | None
    result_sha256: str | None
    reason_code: str
    occurred_at: str
    previous_receipt_sha256: str | None
    transition_sha256: str
    receipt_sha256: str
    authentication_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("AgentJob lifecycle schema_version 必须为 1。")
        _require_identifier(self.receipt_id, field="receipt_id")
        _require_identifier(self.job_id, field="job_id")
        _require_positive_int(self.sequence, field="sequence")
        if self.previous_state is not None and not isinstance(
            self.previous_state,
            AgentJobState,
        ):
            raise TypeError("AgentJob previous_state 类型无效。")
        if not isinstance(self.state, AgentJobState):
            raise TypeError("AgentJob state 类型无效。")
        if self.owner_id is not None:
            _require_identifier(self.owner_id, field="owner_id")
        if isinstance(self.claim_epoch, bool) or not isinstance(self.claim_epoch, int):
            raise TypeError("AgentJob claim_epoch 必须是整数。")
        if self.claim_epoch < 0:
            raise ValueError("AgentJob claim_epoch 不能为负数。")
        if self.claim_expires_at is not None:
            _aware_time(self.claim_expires_at, field="claim_expires_at")
        if self.result_sha256 is not None:
            _require_sha256(self.result_sha256, field="result_sha256")
        _require_identifier(self.reason_code, field="reason_code")
        _aware_time(self.occurred_at, field="occurred_at")
        if self.previous_receipt_sha256 is not None:
            _require_sha256(
                self.previous_receipt_sha256,
                field="previous_receipt_sha256",
            )
        _require_sha256(self.transition_sha256, field="transition_sha256")
        _require_sha256(self.receipt_sha256, field="receipt_sha256")
        _require_sha256(
            self.authentication_sha256,
            field="authentication_sha256",
        )
        if not hmac.compare_digest(
            self.receipt_sha256,
            _digest(_receipt_payload(self)),
        ):
            raise ValueError("AgentJob lifecycle receipt 摘要校验失败。")
        _validate_receipt_semantics(self)


@dataclass(frozen=True, slots=True)
class StoredAgentJob:
    job_id: str
    request: AgentWorkerRequest
    payload_envelope: PayloadEnvelope
    terminal_payload_envelope: PayloadEnvelope | None
    state: AgentJobState
    claim_owner_id: str | None
    claim_epoch: int
    claim_expires_at: str | None
    admitted_at: str
    latest_receipt: AgentJobLifecycleReceipt
    result: AgentWorkerResult | None

    @property
    def request_sha256(self) -> str:
        return self.request.request_sha256


@dataclass(frozen=True, slots=True)
class AgentJobTransitionResult:
    job: StoredAgentJob
    applied: bool

    @property
    def should_dispatch(self) -> bool:
        return self.applied and self.job.state is AgentJobState.CLAIMED


@dataclass(frozen=True, slots=True)
class AgentJobCapacityPolicy:
    max_active_jobs: int
    max_waiters: int
    configured_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _require_capacity_limit(
            self.max_active_jobs,
            field="max_active_jobs",
            minimum=1,
        )
        _require_capacity_limit(
            self.max_waiters,
            field="max_waiters",
            minimum=0,
        )
        configured = _aware_time(self.configured_at, field="configured_at")
        updated = _aware_time(self.updated_at, field="updated_at")
        if updated < configured:
            raise ValueError("AgentJob capacity updated_at 早于 configured_at。")


@dataclass(frozen=True, slots=True)
class AgentJobCapacitySnapshot:
    policy: AgentJobCapacityPolicy
    active_jobs: int
    waiting_jobs: int
    reclaimable_prestart_jobs: int
    recovery_required_jobs: int
    available_jobs: int
    assessed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.policy, AgentJobCapacityPolicy):
            raise TypeError("policy 必须是 AgentJobCapacityPolicy。")
        for field_name in (
            "active_jobs",
            "waiting_jobs",
            "reclaimable_prestart_jobs",
            "recovery_required_jobs",
            "available_jobs",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须是非负整数。")
        if self.available_jobs != max(
            0,
            self.policy.max_active_jobs - self.active_jobs,
        ):
            raise ValueError("AgentJob capacity available_jobs 不一致。")
        _aware_time(self.assessed_at, field="assessed_at")


@dataclass(frozen=True, slots=True)
class _PreparedAdmittedJob:
    job: StoredAgentJob
    payload: AgentJobPayload = field(repr=False)


@dataclass(frozen=True, slots=True)
class _CapacityCounts:
    active_jobs: int
    waiting_jobs: int
    reclaimable_prestart_jobs: int
    recovery_required_jobs: int


class AgentJobStore:
    """SQLite authority that never persists raw Agent task or context."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        key_provider: Callable[[], bytes] = resolve_runtime_payload_key,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        unresolved = Path(db_path).expanduser()
        if not unresolved.is_absolute():
            raise ValueError("AgentJob 路径必须是绝对路径。")
        if not callable(key_provider):
            raise TypeError("AgentJob key_provider 必须可调用。")
        if clock is not None and not callable(clock):
            raise TypeError("AgentJob clock 必须可调用。")
        self._db_path = unresolved.resolve(strict=False)
        self._key_provider = key_provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def admit(
        self,
        *,
        request: AgentWorkerRequest,
        payload: AgentJobPayload,
    ) -> StoredAgentJob:
        key = self._runtime_key()
        prepared = _prepare_admitted_job(
            request=request,
            payload=payload,
            key=key,
            admitted_at=self._now().isoformat(),
        )
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                existing = await _existing_admission(
                    db,
                    request=request,
                    payload=payload,
                    key=key,
                )
                if existing is not None:
                    await db.commit()
                    return existing
                policy = await _capacity_policy_locked(db)
                if policy is not None:
                    counts = await _capacity_counts_locked(db, now=self._now())
                    if counts.waiting_jobs >= policy.max_waiters:
                        raise AgentJobCapacityExhaustedError(
                            "Agent 持久等待队列已满；请使用 capacity admission "
                            "原子取得执行槽位。"
                        )
                stored = await _insert_admitted_job(db, prepared)
                await db.commit()
                return stored
        except (AgentJobConflictError, AgentJobError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法持久化 AgentJob。") from exc

    async def admit_for_capacity(
        self,
        *,
        request: AgentWorkerRequest,
        payload: AgentJobPayload,
        owner_id: str,
        lease_seconds: int,
        max_active_jobs: int,
        max_waiters: int,
    ) -> AgentJobTransitionResult:
        """Atomically admit and claim, or enter the bounded durable FIFO."""
        _require_identifier(owner_id, field="owner_id")
        _require_lease_seconds(lease_seconds)
        _require_capacity_limit(
            max_active_jobs,
            field="max_active_jobs",
            minimum=1,
        )
        _require_capacity_limit(max_waiters, field="max_waiters", minimum=0)
        key = self._runtime_key()
        now = self._now()
        prepared = _prepare_admitted_job(
            request=request,
            payload=payload,
            key=key,
            admitted_at=now.isoformat(),
        )
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                policy = await _ensure_capacity_policy_locked(
                    db,
                    max_active_jobs=max_active_jobs,
                    max_waiters=max_waiters,
                    now=now,
                )
                existing = await _existing_admission(
                    db,
                    request=request,
                    payload=payload,
                    key=key,
                )
                if existing is None:
                    counts = await _capacity_counts_locked(db, now=now)
                    can_claim_now = (
                        counts.active_jobs < policy.max_active_jobs
                        and counts.waiting_jobs == 0
                    )
                    if not can_claim_now and counts.waiting_jobs >= policy.max_waiters:
                        raise AgentJobCapacityExhaustedError(
                            "Agent 持久等待队列已满"
                            f"（上限 {policy.max_waiters}）。"
                        )
                    existing = await _insert_admitted_job(db, prepared)
                transition = await self._claim_for_capacity_locked(
                    db,
                    stored=existing,
                    owner_id=owner_id,
                    lease_seconds=lease_seconds,
                    now=now,
                    key=key,
                    policy=policy,
                )
                await db.commit()
                return transition
        except (AgentJobCapacityExhaustedError, AgentJobConflictError, AgentJobError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法执行 AgentJob capacity admission。") from exc

    async def get(self, job_id: str) -> StoredAgentJob | None:
        _require_identifier(job_id, field="job_id")
        if not _regular_file_exists(self._db_path):
            return None
        key = self._runtime_key()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN")
                cursor = await db.execute(
                    "SELECT * FROM agent_jobs WHERE job_id = ?",
                    (job_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await db.commit()
                    return None
                stored = await _stored_from_row(db, row, key=key)
                await db.commit()
                return stored
        except AgentJobError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法读取 AgentJob。") from exc

    async def capacity_snapshot(self) -> AgentJobCapacitySnapshot | None:
        """Return the current shared embedded Agent capacity authority."""
        if not _regular_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN")
                policy = await _capacity_policy_locked(db)
                if policy is None:
                    await db.commit()
                    return None
                counts = await _capacity_counts_locked(db, now=now)
                await db.commit()
                return AgentJobCapacitySnapshot(
                    policy=policy,
                    active_jobs=counts.active_jobs,
                    waiting_jobs=counts.waiting_jobs,
                    reclaimable_prestart_jobs=(
                        counts.reclaimable_prestart_jobs
                    ),
                    recovery_required_jobs=counts.recovery_required_jobs,
                    available_jobs=max(
                        0,
                        policy.max_active_jobs - counts.active_jobs,
                    ),
                    assessed_at=now.isoformat(),
                )
        except AgentJobError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法读取 AgentJob capacity。") from exc

    async def claim_next(
        self,
        *,
        owner_id: str,
        lease_seconds: int,
    ) -> AgentJobTransitionResult | None:
        _require_identifier(owner_id, field="owner_id")
        _require_lease_seconds(lease_seconds)
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                policy = await _capacity_policy_locked(db)
                if policy is not None:
                    counts = await _capacity_counts_locked(db, now=now)
                    if counts.active_jobs >= policy.max_active_jobs:
                        await db.commit()
                        return None
                cursor = await db.execute(
                    """
                    SELECT * FROM agent_jobs
                    WHERE state = ?
                       OR (state = ? AND claim_expires_at <= ?)
                    ORDER BY admitted_at, job_id
                    LIMIT 1
                    """,
                    (
                        AgentJobState.ADMITTED.value,
                        AgentJobState.CLAIMED.value,
                        now.isoformat(),
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await db.commit()
                    return None
                stored = await _stored_from_row(db, row, key=key)
                result = await self._claim_locked(
                    db,
                    stored=stored,
                    owner_id=owner_id,
                    lease_seconds=lease_seconds,
                    now=now,
                    key=key,
                )
                await db.commit()
                return result
        except (AgentJobError, AgentJobLifecycleConflictError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法 claim AgentJob。") from exc

    async def claim(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_seconds: int,
    ) -> AgentJobTransitionResult:
        _require_identifier(job_id, field="job_id")
        _require_identifier(owner_id, field="owner_id")
        _require_lease_seconds(lease_seconds)
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                stored = await _require_stored(db, job_id, key=key)
                policy = await _capacity_policy_locked(db)
                if policy is None:
                    result = await self._claim_locked(
                        db,
                        stored=stored,
                        owner_id=owner_id,
                        lease_seconds=lease_seconds,
                        now=now,
                        key=key,
                    )
                else:
                    result = await self._claim_for_capacity_locked(
                        db,
                        stored=stored,
                        owner_id=owner_id,
                        lease_seconds=lease_seconds,
                        now=now,
                        key=key,
                        policy=policy,
                    )
                    if (
                        result.job.state is AgentJobState.ADMITTED
                        and not result.applied
                    ):
                        raise AgentJobCapacityExhaustedError(
                            "AgentJob 尚未轮到持久 FIFO 或共享容量已满。"
                        )
                await db.commit()
                return result
        except (AgentJobError, AgentJobLifecycleConflictError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法 claim AgentJob。") from exc

    async def claim_for_capacity(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_seconds: int,
        max_active_jobs: int,
        max_waiters: int,
    ) -> AgentJobTransitionResult:
        """Try to claim this exact FIFO head without bypassing shared capacity."""
        _require_identifier(job_id, field="job_id")
        _require_identifier(owner_id, field="owner_id")
        _require_lease_seconds(lease_seconds)
        _require_capacity_limit(
            max_active_jobs,
            field="max_active_jobs",
            minimum=1,
        )
        _require_capacity_limit(max_waiters, field="max_waiters", minimum=0)
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                policy = await _ensure_capacity_policy_locked(
                    db,
                    max_active_jobs=max_active_jobs,
                    max_waiters=max_waiters,
                    now=now,
                )
                stored = await _require_stored(db, job_id, key=key)
                result = await self._claim_for_capacity_locked(
                    db,
                    stored=stored,
                    owner_id=owner_id,
                    lease_seconds=lease_seconds,
                    now=now,
                    key=key,
                    policy=policy,
                )
                await db.commit()
                return result
        except (AgentJobError, AgentJobLifecycleConflictError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法 claim AgentJob capacity。") from exc

    async def recover_payload(
        self,
        job_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
    ) -> AgentJobPayload:
        key = self._runtime_key()
        stored = await self._require_live_claim(
            job_id,
            owner_id=owner_id,
            claim_epoch=claim_epoch,
            allowed_states=frozenset({AgentJobState.CLAIMED}),
            key=key,
        )
        return self._open_payload(stored, key=key)

    async def mark_running(
        self,
        job_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
    ) -> AgentJobTransitionResult:
        return await self._owner_transition(
            job_id,
            owner_id=owner_id,
            claim_epoch=claim_epoch,
            target_state=AgentJobState.RUNNING,
            result=None,
            reason_code="agent_job_running",
            allowed_states=frozenset({AgentJobState.CLAIMED}),
        )

    async def renew_claim(
        self,
        job_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
        lease_seconds: int,
    ) -> AgentJobTransitionResult:
        _require_lease_seconds(lease_seconds)
        _require_identifier(job_id, field="job_id")
        _require_identifier(owner_id, field="owner_id")
        _require_positive_int(claim_epoch, field="claim_epoch")
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                stored = await _require_stored(db, job_id, key=key)
                _require_live_owner(
                    stored,
                    owner_id=owner_id,
                    claim_epoch=claim_epoch,
                    now=now,
                    allowed_states=frozenset(
                        {AgentJobState.CLAIMED, AgentJobState.RUNNING}
                    ),
                )
                result = await _append_transition(
                    db,
                    stored=stored,
                    target_state=stored.state,
                    owner_id=owner_id,
                    claim_epoch=claim_epoch,
                    claim_expires_at=expires_at,
                    result=None,
                    reason_code="agent_job_claim_renewed",
                    occurred_at=now.isoformat(),
                    key=key,
                )
                await db.commit()
                return result
        except (AgentJobError, AgentJobLifecycleConflictError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法续期 AgentJob claim。") from exc

    async def finish(
        self,
        job_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
        result: AgentWorkerResult,
        terminal_payload: AgentJobTerminalPayload,
    ) -> AgentJobTransitionResult:
        if not isinstance(result, AgentWorkerResult):
            raise TypeError("result 必须是 AgentWorkerResult。")
        if not isinstance(terminal_payload, AgentJobTerminalPayload):
            raise TypeError("terminal_payload 必须是 AgentJobTerminalPayload。")
        _verify_terminal_payload_binding(result, terminal_payload)
        target = AgentJobState(result.status.value)
        return await self._owner_transition(
            job_id,
            owner_id=owner_id,
            claim_epoch=claim_epoch,
            target_state=target,
            result=result,
            terminal_payload=terminal_payload,
            reason_code=result.reason_code,
            allowed_states=frozenset({AgentJobState.RUNNING}),
        )

    async def recover_terminal_payload(
        self,
        job_id: str,
        *,
        expected_result_sha256: str,
    ) -> AgentJobTerminalPayload:
        """Recover exact terminal content only when its receipt digest matches."""
        _require_identifier(job_id, field="job_id")
        _require_sha256(
            expected_result_sha256,
            field="expected_result_sha256",
        )
        if not _regular_file_exists(self._db_path):
            raise AgentJobLifecycleConflictError("AgentJob 不存在。")
        key = self._runtime_key()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN")
                stored = await _require_stored(db, job_id, key=key)
                if stored.state not in TERMINAL_AGENT_JOB_STATES:
                    raise AgentJobLifecycleConflictError(
                        "AgentJob 尚未进入终态，不能恢复结果 payload。"
                    )
                if (
                    stored.result is None
                    or not hmac.compare_digest(
                        stored.result.result_sha256,
                        expected_result_sha256,
                    )
                ):
                    raise AgentJobLifecycleConflictError(
                        "AgentJob terminal result fence 已变化或缺失。"
                    )
                payload = _open_terminal_payload(stored, key=key)
                _verify_terminal_payload_binding(stored.result, payload)
                await db.commit()
                return payload
        except (AgentJobError, AgentJobLifecycleConflictError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法恢复 AgentJob terminal payload。") from exc

    async def get_publication(
        self,
        publication_id: str,
    ) -> StoredAgentJobPublication | None:
        _require_identifier(publication_id, field="publication_id")
        if not _regular_file_exists(self._db_path):
            return None
        key = self._runtime_key()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN")
                publication = await _find_publication(
                    db,
                    publication_id,
                    key=key,
                )
                if publication is not None:
                    job = await _require_stored(
                        db,
                        publication.job_id,
                        key=key,
                    )
                    _validate_publication_job_binding(publication, job)
                await db.commit()
                return publication
        except AgentJobError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法读取 AgentJob publication。") from exc

    async def claim_next_publication(
        self,
        *,
        owner_id: str,
        lease_seconds: int,
    ) -> AgentJobPublicationTransition | None:
        _require_identifier(owner_id, field="owner_id")
        _require_lease_seconds(lease_seconds)
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    SELECT publication_id
                    FROM agent_job_publications
                    WHERE state = ?
                       OR (
                           state = ?
                           AND (
                               claim_expires_at IS NULL
                               OR claim_expires_at <= ?
                           )
                       )
                    ORDER BY created_at, publication_id
                    LIMIT 1
                    """,
                    (
                        AgentJobPublicationState.PENDING.value,
                        AgentJobPublicationState.CLAIMED.value,
                        now.isoformat(),
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await db.commit()
                    return None
                publication = await _require_publication(
                    db,
                    str(row["publication_id"]),
                    key=key,
                )
                job = await _require_stored(db, publication.job_id, key=key)
                _validate_publication_job_binding(publication, job)
                transition = await _claim_publication_locked(
                    db,
                    publication=publication,
                    owner_id=owner_id,
                    lease_seconds=lease_seconds,
                    now=now,
                    key=key,
                )
                await db.commit()
                return transition
        except AgentJobError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法 claim AgentJob publication。") from exc

    async def renew_publication_claim(
        self,
        publication_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
        lease_seconds: int,
    ) -> AgentJobPublicationTransition:
        _require_identifier(publication_id, field="publication_id")
        _require_identifier(owner_id, field="owner_id")
        _require_positive_int(claim_epoch, field="claim_epoch")
        _require_lease_seconds(lease_seconds)
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                publication = await _require_publication(
                    db,
                    publication_id,
                    key=key,
                )
                _require_live_publication_owner(
                    publication,
                    owner_id=owner_id,
                    claim_epoch=claim_epoch,
                    now=now,
                )
                transition = await _append_publication_transition(
                    db,
                    publication=publication,
                    state=AgentJobPublicationState.CLAIMED,
                    owner_id=owner_id,
                    claim_epoch=claim_epoch,
                    claim_expires_at=(
                        max(
                            now,
                            _required_publication_expiry(publication),
                        )
                        + timedelta(seconds=lease_seconds)
                    ).isoformat(),
                    attempt_count=publication.attempt_count,
                    delivery_sha256=None,
                    published_at=None,
                    reason_code="agent_publication_claim_renewed",
                    occurred_at=now.isoformat(),
                    key=key,
                )
                await db.commit()
                return transition
        except AgentJobError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法续期 AgentJob publication claim。") from exc

    async def release_publication_claim(
        self,
        publication_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
    ) -> AgentJobPublicationTransition:
        return await self._publication_owner_transition(
            publication_id,
            owner_id=owner_id,
            claim_epoch=claim_epoch,
            target_state=AgentJobPublicationState.PENDING,
            delivery_sha256=None,
            reason_code="agent_publication_released",
        )

    async def acknowledge_publication(
        self,
        publication_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
        delivery_sha256: str,
    ) -> AgentJobPublicationTransition:
        _require_sha256(delivery_sha256, field="delivery_sha256")
        return await self._publication_owner_transition(
            publication_id,
            owner_id=owner_id,
            claim_epoch=claim_epoch,
            target_state=AgentJobPublicationState.PUBLISHED,
            delivery_sha256=delivery_sha256,
            reason_code="agent_publication_published",
        )

    async def list_publication_recovery(
        self,
        *,
        limit: int = 100,
    ) -> tuple[StoredAgentJobPublication, ...]:
        _require_bounded_limit(limit, maximum=1000)
        if not _regular_file_exists(self._db_path):
            return ()
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN")
                cursor = await db.execute(
                    """
                    SELECT publication_id
                    FROM agent_job_publications
                    WHERE state = ?
                       OR (
                           state = ?
                           AND (
                               claim_expires_at IS NULL
                               OR claim_expires_at <= ?
                           )
                       )
                    ORDER BY created_at, publication_id
                    LIMIT ?
                    """,
                    (
                        AgentJobPublicationState.PENDING.value,
                        AgentJobPublicationState.CLAIMED.value,
                        now.isoformat(),
                        limit,
                    ),
                )
                publications: list[StoredAgentJobPublication] = []
                for row in await cursor.fetchall():
                    publication = await _require_publication(
                        db,
                        str(row["publication_id"]),
                        key=key,
                    )
                    job = await _require_stored(
                        db,
                        publication.job_id,
                        key=key,
                    )
                    _validate_publication_job_binding(publication, job)
                    publications.append(publication)
                await db.commit()
                return tuple(publications)
        except AgentJobError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法读取 AgentJob publication 恢复目录。") from exc

    async def publication_backlog(self) -> AgentJobPublicationBacklog:
        if not _regular_file_exists(self._db_path):
            return AgentJobPublicationBacklog(
                pending=0,
                live_claimed=0,
                expired_claims=0,
                assessed_at=self._now().isoformat(),
            )
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN")
                cursor = await db.execute(
                    """
                    SELECT publication_id
                    FROM agent_job_publications
                    WHERE state IN (?, ?)
                    ORDER BY created_at, publication_id
                    LIMIT 10001
                    """,
                    (
                        AgentJobPublicationState.PENDING.value,
                        AgentJobPublicationState.CLAIMED.value,
                    ),
                )
                rows = await cursor.fetchall()
                if len(rows) > 10000:
                    raise AgentJobError(
                        "AgentJob publication backlog 超过 10000 项安全上限。"
                    )
                pending = 0
                live_claimed = 0
                expired_claims = 0
                for row in rows:
                    publication = await _require_publication(
                        db,
                        str(row["publication_id"]),
                        key=key,
                    )
                    job = await _require_stored(
                        db,
                        publication.job_id,
                        key=key,
                    )
                    _validate_publication_job_binding(publication, job)
                    if publication.state is AgentJobPublicationState.PENDING:
                        pending += 1
                    elif _required_publication_expiry(publication) <= now:
                        expired_claims += 1
                    else:
                        live_claimed += 1
                await db.commit()
                return AgentJobPublicationBacklog(
                    pending=pending,
                    live_claimed=live_claimed,
                    expired_claims=expired_claims,
                    assessed_at=now.isoformat(),
                )
        except AgentJobError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法读取 AgentJob publication backlog。") from exc

    async def _publication_owner_transition(
        self,
        publication_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
        target_state: AgentJobPublicationState,
        delivery_sha256: str | None,
        reason_code: str,
    ) -> AgentJobPublicationTransition:
        _require_identifier(publication_id, field="publication_id")
        _require_identifier(owner_id, field="owner_id")
        _require_positive_int(claim_epoch, field="claim_epoch")
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                publication = await _require_publication(
                    db,
                    publication_id,
                    key=key,
                )
                if publication.state is target_state:
                    if target_state is AgentJobPublicationState.PUBLISHED:
                        if (
                            publication.owner_id != owner_id
                            or publication.claim_epoch != claim_epoch
                            or publication.latest_receipt.delivery_sha256
                            != delivery_sha256
                        ):
                            raise AgentJobLifecycleConflictError(
                                "AgentJob publication ack 幂等事实不一致。"
                            )
                        await db.commit()
                        return AgentJobPublicationTransition(
                            publication,
                            False,
                        )
                _require_live_publication_owner(
                    publication,
                    owner_id=owner_id,
                    claim_epoch=claim_epoch,
                    now=now,
                )
                published_at = (
                    now.isoformat()
                    if target_state is AgentJobPublicationState.PUBLISHED
                    else None
                )
                transition = await _append_publication_transition(
                    db,
                    publication=publication,
                    state=target_state,
                    owner_id=(
                        owner_id
                        if target_state is AgentJobPublicationState.PUBLISHED
                        else None
                    ),
                    claim_epoch=claim_epoch,
                    claim_expires_at=None,
                    attempt_count=publication.attempt_count,
                    delivery_sha256=delivery_sha256,
                    published_at=published_at,
                    reason_code=reason_code,
                    occurred_at=now.isoformat(),
                    key=key,
                )
                await db.commit()
                return transition
        except AgentJobError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法更新 AgentJob publication。") from exc

    async def cancel_before_claim(self, job_id: str) -> AgentJobTransitionResult:
        _require_identifier(job_id, field="job_id")
        key = self._runtime_key()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                stored = await _require_stored(db, job_id, key=key)
                if stored.state is AgentJobState.CANCELLED:
                    await db.commit()
                    return AgentJobTransitionResult(stored, False)
                if stored.state is not AgentJobState.ADMITTED:
                    raise AgentJobLifecycleConflictError(
                        "AgentJob 已被 claim，不能执行未派发取消。"
                    )
                result = await _append_transition(
                    db,
                    stored=stored,
                    target_state=AgentJobState.CANCELLED,
                    owner_id=None,
                    claim_epoch=stored.claim_epoch,
                    claim_expires_at=None,
                    result=None,
                    reason_code="agent_job_cancelled_before_claim",
                    occurred_at=self._now().isoformat(),
                    key=key,
                )
                await db.commit()
                return result
        except (AgentJobError, AgentJobLifecycleConflictError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法取消 AgentJob。") from exc

    async def list_recovery_required(self) -> tuple[StoredAgentJob, ...]:
        if not _regular_file_exists(self._db_path):
            return ()
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now().isoformat()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN")
                cursor = await db.execute(
                    """
                    SELECT * FROM agent_jobs
                    WHERE state = ? AND claim_expires_at <= ?
                    ORDER BY claim_expires_at, job_id
                    """,
                    (AgentJobState.RUNNING.value, now),
                )
                jobs = tuple(
                    [
                        await _stored_from_row(db, row, key=key)
                        for row in await cursor.fetchall()
                    ]
                )
                await db.commit()
                return jobs
        except AgentJobError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法读取待恢复 AgentJob。") from exc

    async def mark_recovery_unknown(
        self,
        job_id: str,
        *,
        expected_latest_receipt_sha256: str,
    ) -> AgentJobTransitionResult:
        _require_identifier(job_id, field="job_id")
        _require_sha256(
            expected_latest_receipt_sha256,
            field="expected_latest_receipt_sha256",
        )
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                stored = await _require_stored(db, job_id, key=key)
                if stored.state is AgentJobState.UNKNOWN:
                    if (
                        stored.latest_receipt.previous_receipt_sha256
                        != expected_latest_receipt_sha256
                    ):
                        raise AgentJobLifecycleConflictError(
                            "AgentJob recovery fence 已变化。"
                        )
                    await db.commit()
                    return AgentJobTransitionResult(stored, False)
                if stored.state is not AgentJobState.RUNNING:
                    raise AgentJobLifecycleConflictError(
                        "只有 running AgentJob 可执行 recovery unknown 收口。"
                    )
                if stored.latest_receipt.receipt_sha256 != (
                    expected_latest_receipt_sha256
                ):
                    raise AgentJobLifecycleConflictError(
                        "AgentJob recovery fence 已变化。"
                    )
                expiry = _required_claim_expiry(stored)
                if expiry > now:
                    raise AgentJobLifecycleConflictError(
                        "AgentJob claim 仍有效，不能收口为 unknown。"
                    )
                result = await _append_transition(
                    db,
                    stored=stored,
                    target_state=AgentJobState.UNKNOWN,
                    owner_id=stored.claim_owner_id,
                    claim_epoch=stored.claim_epoch,
                    claim_expires_at=stored.claim_expires_at,
                    result=None,
                    reason_code="agent_job_recovery_unknown",
                    occurred_at=now.isoformat(),
                    key=key,
                )
                await db.commit()
                return result
        except (AgentJobError, AgentJobLifecycleConflictError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法收口待恢复 AgentJob。") from exc

    async def _claim_locked(
        self,
        db: aiosqlite.Connection,
        *,
        stored: StoredAgentJob,
        owner_id: str,
        lease_seconds: int,
        now: datetime,
        key: RuntimePayloadKey,
    ) -> AgentJobTransitionResult:
        if stored.state is AgentJobState.CLAIMED:
            expiry = _required_claim_expiry(stored)
            if expiry > now:
                if stored.claim_owner_id == owner_id:
                    return AgentJobTransitionResult(stored, False)
                raise AgentJobLifecycleConflictError(
                    "AgentJob 已由其他 live owner claim。"
                )
        elif stored.state is not AgentJobState.ADMITTED:
            raise AgentJobLifecycleConflictError(
                f"AgentJob 状态 {stored.state.value} 不允许 claim。"
            )
        epoch = stored.claim_epoch + 1
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        return await _append_transition(
            db,
            stored=stored,
            target_state=AgentJobState.CLAIMED,
            owner_id=owner_id,
            claim_epoch=epoch,
            claim_expires_at=expires_at,
            result=None,
            reason_code=(
                "agent_job_claimed"
                if stored.state is AgentJobState.ADMITTED
                else "agent_job_claim_taken_over"
            ),
            occurred_at=now.isoformat(),
            key=key,
        )

    async def _claim_for_capacity_locked(
        self,
        db: aiosqlite.Connection,
        *,
        stored: StoredAgentJob,
        owner_id: str,
        lease_seconds: int,
        now: datetime,
        key: RuntimePayloadKey,
        policy: AgentJobCapacityPolicy,
    ) -> AgentJobTransitionResult:
        if not isinstance(policy, AgentJobCapacityPolicy):
            raise TypeError("policy 必须是 AgentJobCapacityPolicy。")
        if stored.state is AgentJobState.CLAIMED:
            expiry = _required_claim_expiry(stored)
            if expiry > now:
                if stored.claim_owner_id == owner_id:
                    return AgentJobTransitionResult(stored, False)
                raise AgentJobLifecycleConflictError(
                    "AgentJob 已由其他 live owner claim。"
                )
        elif stored.state is AgentJobState.ADMITTED:
            cursor = await db.execute(
                """
                SELECT job_id FROM agent_jobs
                WHERE state = ?
                ORDER BY admitted_at, job_id
                LIMIT 1
                """,
                (AgentJobState.ADMITTED.value,),
            )
            row = await cursor.fetchone()
            if row is None or str(row["job_id"]) != stored.job_id:
                return AgentJobTransitionResult(stored, False)
        else:
            raise AgentJobLifecycleConflictError(
                f"AgentJob 状态 {stored.state.value} 不允许 capacity claim。"
            )

        counts = await _capacity_counts_locked(db, now=now)
        if counts.active_jobs >= policy.max_active_jobs:
            return AgentJobTransitionResult(stored, False)
        return await self._claim_locked(
            db,
            stored=stored,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            now=now,
            key=key,
        )

    async def _owner_transition(
        self,
        job_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
        target_state: AgentJobState,
        result: AgentWorkerResult | None,
        reason_code: str,
        allowed_states: frozenset[AgentJobState],
        terminal_payload: AgentJobTerminalPayload | None = None,
    ) -> AgentJobTransitionResult:
        _require_identifier(job_id, field="job_id")
        _require_identifier(owner_id, field="owner_id")
        _require_positive_int(claim_epoch, field="claim_epoch")
        key = self._runtime_key()
        await self._ensure_schema()
        now = self._now()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                stored = await _require_stored(db, job_id, key=key)
                if stored.state is target_state:
                    if (
                        stored.claim_owner_id != owner_id
                        or stored.claim_epoch != claim_epoch
                    ):
                        raise AgentJobLifecycleConflictError(
                            "AgentJob owner 或 claim epoch 已变化。"
                        )
                    if result is None or stored.result == result:
                        if terminal_payload is not None:
                            persisted_payload = _open_terminal_payload(
                                stored,
                                key=key,
                            )
                            if persisted_payload != terminal_payload:
                                raise AgentJobLifecycleConflictError(
                                    "AgentJob terminal payload 幂等重放不一致。"
                                )
                            await _ensure_publication_locked(
                                db,
                                job=stored,
                                occurred_at=now.isoformat(),
                                key=key,
                            )
                        await db.commit()
                        return AgentJobTransitionResult(stored, False)
                _require_live_owner(
                    stored,
                    owner_id=owner_id,
                    claim_epoch=claim_epoch,
                    now=now,
                    allowed_states=allowed_states,
                )
                if result is not None and not hmac.compare_digest(
                    result.request_sha256,
                    stored.request_sha256,
                ):
                    raise AgentJobLifecycleConflictError(
                        "AgentJob result 未绑定当前 request。"
                    )
                terminal_payload_envelope = None
                if result is not None:
                    if terminal_payload is None:
                        raise AgentJobLifecycleConflictError(
                            "AgentJob terminal transition 缺少结果 payload。"
                        )
                    _verify_terminal_payload_binding(result, terminal_payload)
                    terminal_payload_envelope = seal_runtime_payload(
                        _encode_terminal_payload(terminal_payload),
                        aad=_terminal_payload_aad(
                            stored.request_sha256,
                            result.result_sha256,
                        ),
                        key=key,
                    )
                elif terminal_payload is not None:
                    raise AgentJobLifecycleConflictError(
                        "非结果 transition 不得包含 terminal payload。"
                    )
                transition = await _append_transition(
                    db,
                    stored=stored,
                    target_state=target_state,
                    owner_id=owner_id,
                    claim_epoch=claim_epoch,
                    claim_expires_at=stored.claim_expires_at,
                    result=result,
                    reason_code=reason_code,
                    occurred_at=now.isoformat(),
                    key=key,
                    terminal_payload_envelope=terminal_payload_envelope,
                )
                if result is not None:
                    await _ensure_publication_locked(
                        db,
                        job=transition.job,
                        occurred_at=now.isoformat(),
                        key=key,
                    )
                await db.commit()
                return transition
        except (AgentJobError, AgentJobLifecycleConflictError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法更新 AgentJob 生命周期。") from exc

    async def _require_live_claim(
        self,
        job_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
        allowed_states: frozenset[AgentJobState],
        key: RuntimePayloadKey,
    ) -> StoredAgentJob:
        _require_identifier(job_id, field="job_id")
        _require_identifier(owner_id, field="owner_id")
        _require_positive_int(claim_epoch, field="claim_epoch")
        if not _regular_file_exists(self._db_path):
            raise AgentJobLifecycleConflictError("AgentJob 不存在。")
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN")
                stored = await _require_stored(db, job_id, key=key)
                _require_live_owner(
                    stored,
                    owner_id=owner_id,
                    claim_epoch=claim_epoch,
                    now=self._now(),
                    allowed_states=allowed_states,
                )
                await db.commit()
                return stored
        except (AgentJobError, AgentJobLifecycleConflictError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法验证 AgentJob claim。") from exc

    def _open_payload(
        self,
        stored: StoredAgentJob,
        *,
        key: RuntimePayloadKey | None = None,
    ) -> AgentJobPayload:
        return _open_stored_payload(
            stored,
            key=key or self._runtime_key(),
        )

    def _runtime_key(self) -> RuntimePayloadKey:
        try:
            return RuntimePayloadKey.from_bytes(self._key_provider())
        except Exception as exc:
            raise AgentJobKeyUnavailableError(
                "AgentJob Runtime payload 密钥不可用。"
            ) from exc

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise AgentJobError("AgentJob clock 返回值无效。")
        if value.tzinfo is None or value.utcoffset() is None:
            raise AgentJobError("AgentJob clock 必须返回 aware datetime。")
        return value.astimezone(UTC)

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            created_parent = not self._db_path.parent.exists()
            self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if created_parent and os.name != "nt":
                self._db_path.parent.chmod(0o700)
            existed = self._db_path.exists()
            try:
                async with self._connection() as db:
                    await db.execute("BEGIN IMMEDIATE")
                    cursor = await db.execute("PRAGMA user_version")
                    row = await cursor.fetchone()
                    version = int(row[0]) if row is not None else 0
                    if version == 0:
                        tables = await _user_tables(db)
                        if tables:
                            raise AgentJobError(
                                "AgentJob 是未知的未版本化数据库。"
                            )
                        for statement in _SCHEMA_V1:
                            await db.execute(statement)
                        for statement in _SCHEMA_V2:
                            await db.execute(statement)
                        for statement in _SCHEMA_V3:
                            await db.execute(statement)
                        for statement in _SCHEMA_V4:
                            await db.execute(statement)
                        await db.execute(
                            f"PRAGMA user_version = {AGENT_JOB_SCHEMA_VERSION}"
                        )
                    elif version == 1:
                        for statement in _SCHEMA_V2:
                            await db.execute(statement)
                        await _apply_schema_v3(db)
                        await _apply_schema_v4(db, allow_create=True)
                        await db.execute(
                            f"PRAGMA user_version = {AGENT_JOB_SCHEMA_VERSION}"
                        )
                    elif version == 2:
                        await _apply_schema_v3(db)
                        await _apply_schema_v4(db, allow_create=True)
                        await db.execute(
                            f"PRAGMA user_version = {AGENT_JOB_SCHEMA_VERSION}"
                        )
                    elif version == 3:
                        await _apply_schema_v4(db, allow_create=True)
                        await db.execute(
                            f"PRAGMA user_version = {AGENT_JOB_SCHEMA_VERSION}"
                        )
                    elif version != AGENT_JOB_SCHEMA_VERSION:
                        raise AgentJobError(
                            f"AgentJob schema v{version} 不受支持；"
                            f"当前仅支持 v{AGENT_JOB_SCHEMA_VERSION}。"
                        )
                    else:
                        await _apply_schema_v4(db, allow_create=False)
                    await db.commit()
                if not existed and os.name != "nt":
                    self._db_path.chmod(0o600)
                self._schema_ready = True
            except AgentJobError:
                raise
            except (aiosqlite.Error, OSError, ValueError) as exc:
                raise AgentJobError("无法初始化 AgentJob Store。") from exc

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self._db_path, timeout=5.0)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            await db.close()


def _prepare_admitted_job(
    *,
    request: AgentWorkerRequest,
    payload: AgentJobPayload,
    key: RuntimePayloadKey,
    admitted_at: str,
) -> _PreparedAdmittedJob:
    if not isinstance(request, AgentWorkerRequest):
        raise TypeError("request 必须是 AgentWorkerRequest。")
    if not isinstance(payload, AgentJobPayload):
        raise TypeError("payload 必须是 AgentJobPayload。")
    if not isinstance(key, RuntimePayloadKey):
        raise TypeError("key 必须是 RuntimePayloadKey。")
    _verify_payload_binding(request, payload)
    timestamp = _aware_time(admitted_at, field="admitted_at").isoformat()
    envelope = seal_runtime_payload(
        _encode_payload(request, payload),
        aad=request.request_sha256.encode("ascii"),
        key=key,
    )
    job_id = _job_id(request.request_sha256)
    receipt = _issue_receipt(
        job_id=job_id,
        sequence=1,
        previous_state=None,
        state=AgentJobState.ADMITTED,
        owner_id=None,
        claim_epoch=0,
        claim_expires_at=None,
        result_sha256=None,
        reason_code="agent_job_admitted",
        occurred_at=timestamp,
        previous_receipt_sha256=None,
        authentication_key=key.key_bytes,
    )
    return _PreparedAdmittedJob(
        job=StoredAgentJob(
            job_id=job_id,
            request=request,
            payload_envelope=envelope,
            terminal_payload_envelope=None,
            state=AgentJobState.ADMITTED,
            claim_owner_id=None,
            claim_epoch=0,
            claim_expires_at=None,
            admitted_at=timestamp,
            latest_receipt=receipt,
            result=None,
        ),
        payload=payload,
    )


async def _existing_admission(
    db: aiosqlite.Connection,
    *,
    request: AgentWorkerRequest,
    payload: AgentJobPayload,
    key: RuntimePayloadKey,
) -> StoredAgentJob | None:
    cursor = await db.execute(
        "SELECT * FROM agent_jobs WHERE request_id = ?",
        (request.request_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    existing = await _stored_from_row(db, row, key=key)
    if not hmac.compare_digest(
        existing.request_sha256,
        request.request_sha256,
    ):
        raise AgentJobConflictError(
            "AgentJob request_id 已绑定其他请求。"
        )
    if _open_stored_payload(existing, key=key) != payload:
        raise AgentJobConflictError(
            "AgentJob request_id 已绑定其他 payload。"
        )
    return existing


async def _insert_admitted_job(
    db: aiosqlite.Connection,
    prepared: _PreparedAdmittedJob,
) -> StoredAgentJob:
    if not isinstance(prepared, _PreparedAdmittedJob):
        raise TypeError("prepared 必须是 _PreparedAdmittedJob。")
    job = prepared.job
    await db.execute(
        """
        INSERT INTO agent_jobs (
            job_id, request_id, request_sha256, state,
            claim_owner_id, claim_epoch, claim_expires_at,
            admitted_at, latest_sequence, latest_receipt_sha256,
            latest_receipt_json, payload_envelope_json, result_json
        ) VALUES (?, ?, ?, ?, NULL, 0, NULL, ?, ?, ?, ?, ?, NULL)
        """,
        (
            job.job_id,
            job.request.request_id,
            job.request_sha256,
            AgentJobState.ADMITTED.value,
            job.admitted_at,
            job.latest_receipt.sequence,
            job.latest_receipt.receipt_sha256,
            _serialize_receipt(job.latest_receipt),
            _canonical_json(job.payload_envelope.to_dict()),
        ),
    )
    await _insert_receipt(db, job.latest_receipt)
    return job


def _open_stored_payload(
    stored: StoredAgentJob,
    *,
    key: RuntimePayloadKey,
) -> AgentJobPayload:
    try:
        raw = open_runtime_payload(
            stored.payload_envelope,
            aad=stored.request_sha256.encode("ascii"),
            key=key,
        )
        request, payload = _decode_payload(raw)
        if request != stored.request:
            raise ValueError("AgentJob encrypted request 与主记录不一致。")
        _verify_payload_binding(stored.request, payload)
        return payload
    except (PayloadEnvelopeError, TypeError, ValueError) as exc:
        raise AgentJobError("AgentJob payload 无法认证或恢复。") from exc


def _open_terminal_payload(
    stored: StoredAgentJob,
    *,
    key: RuntimePayloadKey,
) -> AgentJobTerminalPayload:
    envelope = stored.terminal_payload_envelope
    result = stored.result
    if envelope is None or result is None:
        raise AgentJobError("AgentJob 没有可恢复的 terminal payload。")
    try:
        raw = open_runtime_payload(
            envelope,
            aad=_terminal_payload_aad(
                stored.request_sha256,
                result.result_sha256,
            ),
            key=key,
        )
        payload = _decode_terminal_payload(raw)
        _verify_terminal_payload_binding(result, payload)
        return payload
    except (PayloadEnvelopeError, TypeError, ValueError) as exc:
        raise AgentJobError(
            "AgentJob terminal payload 无法认证或恢复。"
        ) from exc


async def _ensure_publication_locked(
    db: aiosqlite.Connection,
    *,
    job: StoredAgentJob,
    occurred_at: str,
    key: RuntimePayloadKey,
) -> StoredAgentJobPublication:
    if job.result is None or job.terminal_payload_envelope is None:
        raise AgentJobError(
            "AgentJob terminal publication 缺少 result 或加密 payload。"
        )
    publication_id = _publication_id(job.job_id, job.result.result_sha256)
    existing = await _find_publication(db, publication_id, key=key)
    if existing is not None:
        _validate_publication_job_binding(existing, job)
        return existing
    receipt = _issue_publication_receipt(
        publication_id=publication_id,
        job_id=job.job_id,
        sequence=1,
        request_sha256=job.request_sha256,
        result_sha256=job.result.result_sha256,
        state=AgentJobPublicationState.PENDING,
        owner_id=None,
        claim_epoch=0,
        claim_expires_at=None,
        attempt_count=0,
        delivery_sha256=None,
        published_at=None,
        reason_code="agent_publication_pending",
        occurred_at=occurred_at,
        previous_receipt_sha256=None,
        key=key.key_bytes,
    )
    await db.execute(
        """
        INSERT INTO agent_job_publications (
            publication_id, job_id, request_sha256, result_sha256,
            state, owner_id, claim_epoch, claim_expires_at,
            attempt_count, created_at, published_at,
            latest_sequence, latest_receipt_sha256, latest_receipt_json
        ) VALUES (?, ?, ?, ?, ?, NULL, 0, NULL, 0, ?, NULL, 1, ?, ?)
        """,
        (
            publication_id,
            job.job_id,
            job.request_sha256,
            job.result.result_sha256,
            AgentJobPublicationState.PENDING.value,
            occurred_at,
            receipt.receipt_sha256,
            _serialize_publication_receipt(receipt),
        ),
    )
    await _insert_publication_receipt(db, receipt)
    return StoredAgentJobPublication(
        publication_id=publication_id,
        job_id=job.job_id,
        request_sha256=job.request_sha256,
        result_sha256=job.result.result_sha256,
        state=AgentJobPublicationState.PENDING,
        owner_id=None,
        claim_epoch=0,
        claim_expires_at=None,
        attempt_count=0,
        created_at=occurred_at,
        published_at=None,
        latest_receipt=receipt,
    )


async def _find_publication(
    db: aiosqlite.Connection,
    publication_id: str,
    *,
    key: RuntimePayloadKey,
) -> StoredAgentJobPublication | None:
    cursor = await db.execute(
        """
        SELECT * FROM agent_job_publications
        WHERE publication_id = ?
        """,
        (publication_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return await _stored_publication_from_row(db, row, key=key)


async def _require_publication(
    db: aiosqlite.Connection,
    publication_id: str,
    *,
    key: RuntimePayloadKey,
) -> StoredAgentJobPublication:
    publication = await _find_publication(db, publication_id, key=key)
    if publication is None:
        raise AgentJobLifecycleConflictError(
            "AgentJob publication 不存在。"
        )
    return publication


async def _stored_publication_from_row(
    db: aiosqlite.Connection,
    row: aiosqlite.Row,
    *,
    key: RuntimePayloadKey,
) -> StoredAgentJobPublication:
    try:
        latest = _deserialize_publication_receipt(
            str(row["latest_receipt_json"])
        )
        publication = StoredAgentJobPublication(
            publication_id=str(row["publication_id"]),
            job_id=str(row["job_id"]),
            request_sha256=str(row["request_sha256"]),
            result_sha256=str(row["result_sha256"]),
            state=AgentJobPublicationState(str(row["state"])),
            owner_id=(
                str(row["owner_id"])
                if row["owner_id"] is not None
                else None
            ),
            claim_epoch=int(row["claim_epoch"]),
            claim_expires_at=(
                str(row["claim_expires_at"])
                if row["claim_expires_at"] is not None
                else None
            ),
            attempt_count=int(row["attempt_count"]),
            created_at=str(row["created_at"]),
            published_at=(
                str(row["published_at"])
                if row["published_at"] is not None
                else None
            ),
            latest_receipt=latest,
        )
        _validate_stored_publication(publication, row)
        await _validate_publication_event_chain(db, publication, key=key)
        return publication
    except AgentJobError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentJobError(
            "AgentJob publication 持久记录无效。"
        ) from exc


def _validate_stored_publication(
    publication: StoredAgentJobPublication,
    row: aiosqlite.Row,
) -> None:
    _require_identifier(publication.publication_id, field="publication_id")
    _require_identifier(publication.job_id, field="job_id")
    _require_sha256(publication.request_sha256, field="request_sha256")
    _require_sha256(publication.result_sha256, field="result_sha256")
    _aware_time(publication.created_at, field="created_at")
    if publication.published_at is not None:
        _aware_time(publication.published_at, field="published_at")
    if int(row["latest_sequence"]) != publication.latest_receipt.sequence:
        raise AgentJobError("AgentJob publication latest sequence 不一致。")
    if str(row["latest_receipt_sha256"]) != (
        publication.latest_receipt.receipt_sha256
    ):
        raise AgentJobError("AgentJob publication latest receipt 不一致。")
    receipt = publication.latest_receipt
    comparisons = (
        (receipt.publication_id, publication.publication_id, "publication_id"),
        (receipt.job_id, publication.job_id, "job_id"),
        (receipt.request_sha256, publication.request_sha256, "request"),
        (receipt.result_sha256, publication.result_sha256, "result"),
        (receipt.state, publication.state, "state"),
        (receipt.owner_id, publication.owner_id, "owner"),
        (receipt.claim_epoch, publication.claim_epoch, "claim epoch"),
        (receipt.claim_expires_at, publication.claim_expires_at, "claim expiry"),
        (receipt.attempt_count, publication.attempt_count, "attempt count"),
        (receipt.published_at, publication.published_at, "published time"),
    )
    for receipt_value, stored_value, name in comparisons:
        if receipt_value != stored_value:
            raise AgentJobError(f"AgentJob publication {name} 不一致。")


async def _validate_publication_event_chain(
    db: aiosqlite.Connection,
    publication: StoredAgentJobPublication,
    *,
    key: RuntimePayloadKey,
) -> None:
    cursor = await db.execute(
        """
        SELECT sequence, receipt_sha256, occurred_at, state, receipt_json
        FROM agent_job_publication_events
        WHERE publication_id = ?
        ORDER BY sequence
        """,
        (publication.publication_id,),
    )
    rows = await cursor.fetchall()
    if len(rows) != publication.latest_receipt.sequence:
        raise AgentJobError("AgentJob publication event 数量不一致。")
    previous: AgentJobPublicationReceipt | None = None
    for index, row in enumerate(rows, start=1):
        receipt = _deserialize_publication_receipt(str(row["receipt_json"]))
        _verify_publication_receipt_authentication(receipt, key=key.key_bytes)
        if int(row["sequence"]) != index or receipt.sequence != index:
            raise AgentJobError("AgentJob publication sequence 不连续。")
        if (
            str(row["receipt_sha256"]) != receipt.receipt_sha256
            or str(row["occurred_at"]) != receipt.occurred_at
            or str(row["state"]) != receipt.state.value
        ):
            raise AgentJobError(
                "AgentJob publication event 投影与 receipt 不一致。"
            )
        if receipt.publication_id != publication.publication_id:
            raise AgentJobError("AgentJob publication event identity 不一致。")
        if previous is None:
            if receipt.previous_receipt_sha256 is not None:
                raise AgentJobError("AgentJob publication 首事件链接无效。")
            if publication.created_at != receipt.occurred_at:
                raise AgentJobError(
                    "AgentJob publication created time 不一致。"
                )
        else:
            if receipt.previous_receipt_sha256 != previous.receipt_sha256:
                raise AgentJobError("AgentJob publication receipt chain 断裂。")
            _validate_publication_transition(previous, receipt)
        if (
            receipt.job_id != publication.job_id
            or receipt.request_sha256 != publication.request_sha256
            or receipt.result_sha256 != publication.result_sha256
        ):
            raise AgentJobError("AgentJob publication event 绑定发生漂移。")
        previous = receipt
    if previous != publication.latest_receipt:
        raise AgentJobError("AgentJob publication latest event 不一致。")


def _validate_publication_transition(
    previous: AgentJobPublicationReceipt,
    current: AgentJobPublicationReceipt,
) -> None:
    previous_time = _aware_time(
        previous.occurred_at,
        field="previous occurred_at",
    )
    current_time = _aware_time(current.occurred_at, field="occurred_at")
    if current_time < previous_time:
        raise AgentJobError(
            "AgentJob publication occurred time 发生倒退。"
        )
    transition = (
        previous.state,
        current.state,
        current.reason_code,
    )
    if transition == (
        AgentJobPublicationState.PENDING,
        AgentJobPublicationState.CLAIMED,
        "agent_publication_claimed",
    ):
        valid = (
            current.claim_epoch == previous.claim_epoch + 1
            and current.attempt_count == previous.attempt_count + 1
        )
    elif transition == (
        AgentJobPublicationState.CLAIMED,
        AgentJobPublicationState.CLAIMED,
        "agent_publication_claim_renewed",
    ):
        valid = (
            current.owner_id == previous.owner_id
            and current.claim_epoch == previous.claim_epoch
            and current.attempt_count == previous.attempt_count
            and _required_receipt_claim_expiry(current)
            > _required_receipt_claim_expiry(previous)
        )
    elif transition == (
        AgentJobPublicationState.CLAIMED,
        AgentJobPublicationState.CLAIMED,
        "agent_publication_claim_taken_over",
    ):
        previous_expiry = _aware_time(
            previous.claim_expires_at or "",
            field="previous claim_expires_at",
        )
        valid = (
            current_time >= previous_expiry
            and current.claim_epoch == previous.claim_epoch + 1
            and current.attempt_count == previous.attempt_count + 1
        )
    elif transition == (
        AgentJobPublicationState.CLAIMED,
        AgentJobPublicationState.PENDING,
        "agent_publication_released",
    ):
        valid = (
            current.claim_epoch == previous.claim_epoch
            and current.attempt_count == previous.attempt_count
            and current_time < _required_receipt_claim_expiry(previous)
        )
    elif transition == (
        AgentJobPublicationState.CLAIMED,
        AgentJobPublicationState.PUBLISHED,
        "agent_publication_published",
    ):
        valid = (
            current.owner_id == previous.owner_id
            and current.claim_epoch == previous.claim_epoch
            and current.attempt_count == previous.attempt_count
            and current_time < _required_receipt_claim_expiry(previous)
        )
    else:
        valid = False
    if not valid:
        raise AgentJobError("AgentJob publication 状态转换无效。")


def _required_receipt_claim_expiry(
    receipt: AgentJobPublicationReceipt,
) -> datetime:
    if receipt.claim_expires_at is None:
        raise AgentJobError(
            "AgentJob publication receipt claim expiry 缺失。"
        )
    return _aware_time(
        receipt.claim_expires_at,
        field="receipt claim_expires_at",
    )


def _validate_publication_job_binding(
    publication: StoredAgentJobPublication,
    job: StoredAgentJob,
) -> None:
    if publication.job_id != job.job_id:
        raise AgentJobError("AgentJob publication job 绑定不一致。")
    if not hmac.compare_digest(
        publication.request_sha256,
        job.request_sha256,
    ):
        raise AgentJobError("AgentJob publication request 绑定不一致。")
    if (
        job.result is None
        or job.terminal_payload_envelope is None
        or not hmac.compare_digest(
            publication.result_sha256,
            job.result.result_sha256,
        )
    ):
        raise AgentJobError("AgentJob publication result 绑定不一致。")
    if publication.publication_id != _publication_id(
        job.job_id,
        job.result.result_sha256,
    ):
        raise AgentJobError("AgentJob publication identity 不一致。")


async def _claim_publication_locked(
    db: aiosqlite.Connection,
    *,
    publication: StoredAgentJobPublication,
    owner_id: str,
    lease_seconds: int,
    now: datetime,
    key: RuntimePayloadKey,
) -> AgentJobPublicationTransition:
    if publication.state is AgentJobPublicationState.CLAIMED:
        if _required_publication_expiry(publication) > now:
            if publication.owner_id == owner_id:
                return AgentJobPublicationTransition(publication, False)
            raise AgentJobLifecycleConflictError(
                "AgentJob publication 已由其他 live owner claim。"
            )
    elif publication.state is not AgentJobPublicationState.PENDING:
        raise AgentJobLifecycleConflictError(
            f"AgentJob publication 状态 {publication.state.value} 不允许 claim。"
        )
    return await _append_publication_transition(
        db,
        publication=publication,
        state=AgentJobPublicationState.CLAIMED,
        owner_id=owner_id,
        claim_epoch=publication.claim_epoch + 1,
        claim_expires_at=(
            now + timedelta(seconds=lease_seconds)
        ).isoformat(),
        attempt_count=publication.attempt_count + 1,
        delivery_sha256=None,
        published_at=None,
        reason_code=(
            "agent_publication_claimed"
            if publication.state is AgentJobPublicationState.PENDING
            else "agent_publication_claim_taken_over"
        ),
        occurred_at=now.isoformat(),
        key=key,
    )


def _require_live_publication_owner(
    publication: StoredAgentJobPublication,
    *,
    owner_id: str,
    claim_epoch: int,
    now: datetime,
) -> None:
    if publication.state is not AgentJobPublicationState.CLAIMED:
        raise AgentJobLifecycleConflictError(
            "AgentJob publication 不处于 claimed 状态。"
        )
    if (
        publication.owner_id != owner_id
        or publication.claim_epoch != claim_epoch
    ):
        raise AgentJobLifecycleConflictError(
            "AgentJob publication owner 或 epoch 已变化。"
        )
    if _required_publication_expiry(publication) <= now:
        raise AgentJobLifecycleConflictError(
            "AgentJob publication claim 已过期。"
        )


def _required_publication_expiry(
    publication: StoredAgentJobPublication,
) -> datetime:
    if publication.claim_expires_at is None:
        raise AgentJobError("AgentJob publication claim expiry 缺失。")
    return _aware_time(
        publication.claim_expires_at,
        field="claim_expires_at",
    )


async def _append_publication_transition(
    db: aiosqlite.Connection,
    *,
    publication: StoredAgentJobPublication,
    state: AgentJobPublicationState,
    owner_id: str | None,
    claim_epoch: int,
    claim_expires_at: str | None,
    attempt_count: int,
    delivery_sha256: str | None,
    published_at: str | None,
    reason_code: str,
    occurred_at: str,
    key: RuntimePayloadKey,
) -> AgentJobPublicationTransition:
    receipt = _issue_publication_receipt(
        publication_id=publication.publication_id,
        job_id=publication.job_id,
        sequence=publication.latest_receipt.sequence + 1,
        request_sha256=publication.request_sha256,
        result_sha256=publication.result_sha256,
        state=state,
        owner_id=owner_id,
        claim_epoch=claim_epoch,
        claim_expires_at=claim_expires_at,
        attempt_count=attempt_count,
        delivery_sha256=delivery_sha256,
        published_at=published_at,
        reason_code=reason_code,
        occurred_at=occurred_at,
        previous_receipt_sha256=(
            publication.latest_receipt.receipt_sha256
        ),
        key=key.key_bytes,
    )
    await _insert_publication_receipt(db, receipt)
    cursor = await db.execute(
        """
        UPDATE agent_job_publications
        SET state = ?, owner_id = ?, claim_epoch = ?,
            claim_expires_at = ?, attempt_count = ?, published_at = ?,
            latest_sequence = ?, latest_receipt_sha256 = ?,
            latest_receipt_json = ?
        WHERE publication_id = ? AND latest_sequence = ?
        """,
        (
            state.value,
            owner_id,
            claim_epoch,
            claim_expires_at,
            attempt_count,
            published_at,
            receipt.sequence,
            receipt.receipt_sha256,
            _serialize_publication_receipt(receipt),
            publication.publication_id,
            publication.latest_receipt.sequence,
        ),
    )
    if cursor.rowcount != 1:
        raise AgentJobLifecycleConflictError(
            "AgentJob publication 被并发修改。"
        )
    return AgentJobPublicationTransition(
        publication=StoredAgentJobPublication(
            publication_id=publication.publication_id,
            job_id=publication.job_id,
            request_sha256=publication.request_sha256,
            result_sha256=publication.result_sha256,
            state=state,
            owner_id=owner_id,
            claim_epoch=claim_epoch,
            claim_expires_at=claim_expires_at,
            attempt_count=attempt_count,
            created_at=publication.created_at,
            published_at=published_at,
            latest_receipt=receipt,
        ),
        applied=True,
    )


async def _insert_publication_receipt(
    db: aiosqlite.Connection,
    receipt: AgentJobPublicationReceipt,
) -> None:
    await db.execute(
        """
        INSERT INTO agent_job_publication_events (
            publication_id, sequence, receipt_sha256,
            occurred_at, state, receipt_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            receipt.publication_id,
            receipt.sequence,
            receipt.receipt_sha256,
            receipt.occurred_at,
            receipt.state.value,
            _serialize_publication_receipt(receipt),
        ),
    )


async def _capacity_policy_locked(
    db: aiosqlite.Connection,
) -> AgentJobCapacityPolicy | None:
    cursor = await db.execute(
        """
        SELECT max_active_jobs, max_waiters, configured_at, updated_at
        FROM agent_job_capacity_policy
        WHERE policy_id = 1
        """
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return AgentJobCapacityPolicy(
            max_active_jobs=int(row["max_active_jobs"]),
            max_waiters=int(row["max_waiters"]),
            configured_at=str(row["configured_at"]),
            updated_at=str(row["updated_at"]),
        )
    except (TypeError, ValueError) as exc:
        raise AgentJobError("AgentJob capacity policy 记录无效。") from exc


async def _ensure_capacity_policy_locked(
    db: aiosqlite.Connection,
    *,
    max_active_jobs: int,
    max_waiters: int,
    now: datetime,
) -> AgentJobCapacityPolicy:
    _require_capacity_limit(
        max_active_jobs,
        field="max_active_jobs",
        minimum=1,
    )
    _require_capacity_limit(max_waiters, field="max_waiters", minimum=0)
    existing = await _capacity_policy_locked(db)
    timestamp = now.isoformat()
    if existing is None:
        await db.execute(
            """
            INSERT INTO agent_job_capacity_policy (
                policy_id, max_active_jobs, max_waiters,
                configured_at, updated_at
            ) VALUES (1, ?, ?, ?, ?)
            """,
            (max_active_jobs, max_waiters, timestamp, timestamp),
        )
        return AgentJobCapacityPolicy(
            max_active_jobs=max_active_jobs,
            max_waiters=max_waiters,
            configured_at=timestamp,
            updated_at=timestamp,
        )
    if (
        existing.max_active_jobs == max_active_jobs
        and existing.max_waiters == max_waiters
    ):
        return existing
    counts = await _capacity_counts_locked(db, now=now)
    if (
        counts.active_jobs
        or counts.waiting_jobs
        or counts.reclaimable_prestart_jobs
    ):
        raise AgentJobConflictError(
            "AgentJob capacity policy 与当前 Runtime 配置不一致；"
            "存在未终结任务时不能改写共享容量。"
        )
    await db.execute(
        """
        UPDATE agent_job_capacity_policy
        SET max_active_jobs = ?, max_waiters = ?, updated_at = ?
        WHERE policy_id = 1
        """,
        (max_active_jobs, max_waiters, timestamp),
    )
    return AgentJobCapacityPolicy(
        max_active_jobs=max_active_jobs,
        max_waiters=max_waiters,
        configured_at=existing.configured_at,
        updated_at=timestamp,
    )


async def _capacity_counts_locked(
    db: aiosqlite.Connection,
    *,
    now: datetime,
) -> _CapacityCounts:
    cursor = await db.execute(
        """
        SELECT state, claim_expires_at
        FROM agent_jobs
        WHERE state IN (?, ?, ?)
        """,
        (
            AgentJobState.ADMITTED.value,
            AgentJobState.CLAIMED.value,
            AgentJobState.RUNNING.value,
        ),
    )
    active = 0
    waiting = 0
    reclaimable = 0
    recovery_required = 0
    for row in await cursor.fetchall():
        try:
            state = AgentJobState(str(row["state"]))
            if state is AgentJobState.ADMITTED:
                if row["claim_expires_at"] is not None:
                    raise ValueError("admitted job 包含 claim expiry")
                waiting += 1
                continue
            raw_expiry = row["claim_expires_at"]
            if raw_expiry is None:
                raise ValueError("active job 缺少 claim expiry")
            expiry = _aware_time(
                str(raw_expiry),
                field="claim_expires_at",
            )
            if state is AgentJobState.CLAIMED:
                if expiry <= now:
                    reclaimable += 1
                else:
                    active += 1
            elif state is AgentJobState.RUNNING:
                active += 1
                if expiry <= now:
                    recovery_required += 1
        except (TypeError, ValueError) as exc:
            raise AgentJobError("AgentJob capacity 状态记录无效。") from exc
    return _CapacityCounts(
        active_jobs=active,
        waiting_jobs=waiting,
        reclaimable_prestart_jobs=reclaimable,
        recovery_required_jobs=recovery_required,
    )


async def _append_transition(
    db: aiosqlite.Connection,
    *,
    stored: StoredAgentJob,
    target_state: AgentJobState,
    owner_id: str | None,
    claim_epoch: int,
    claim_expires_at: str | None,
    result: AgentWorkerResult | None,
    reason_code: str,
    occurred_at: str,
    key: RuntimePayloadKey,
    terminal_payload_envelope: PayloadEnvelope | None = None,
) -> AgentJobTransitionResult:
    receipt = _issue_receipt(
        job_id=stored.job_id,
        sequence=stored.latest_receipt.sequence + 1,
        previous_state=stored.state,
        state=target_state,
        owner_id=owner_id,
        claim_epoch=claim_epoch,
        claim_expires_at=claim_expires_at,
        result_sha256=result.result_sha256 if result is not None else None,
        reason_code=reason_code,
        occurred_at=occurred_at,
        previous_receipt_sha256=stored.latest_receipt.receipt_sha256,
        authentication_key=key.key_bytes,
    )
    receipt_json = _serialize_receipt(receipt)
    result_json = _serialize_result(result) if result is not None else None
    terminal_payload_envelope_json = (
        _canonical_json(terminal_payload_envelope.to_dict())
        if terminal_payload_envelope is not None
        else None
    )
    await _insert_receipt(db, receipt)
    cursor = await db.execute(
        """
        UPDATE agent_jobs
        SET state = ?, claim_owner_id = ?, claim_epoch = ?,
            claim_expires_at = ?, latest_sequence = ?,
            latest_receipt_sha256 = ?, latest_receipt_json = ?,
            result_json = COALESCE(?, result_json),
            terminal_payload_envelope_json =
                COALESCE(?, terminal_payload_envelope_json)
        WHERE job_id = ? AND latest_sequence = ?
        """,
        (
            target_state.value,
            owner_id,
            claim_epoch,
            claim_expires_at,
            receipt.sequence,
            receipt.receipt_sha256,
            receipt_json,
            result_json,
            terminal_payload_envelope_json,
            stored.job_id,
            stored.latest_receipt.sequence,
        ),
    )
    if cursor.rowcount != 1:
        raise AgentJobLifecycleConflictError(
            "AgentJob 生命周期被并发修改。"
        )
    return AgentJobTransitionResult(
        StoredAgentJob(
            job_id=stored.job_id,
            request=stored.request,
            payload_envelope=stored.payload_envelope,
            terminal_payload_envelope=(
                terminal_payload_envelope
                if terminal_payload_envelope is not None
                else stored.terminal_payload_envelope
            ),
            state=target_state,
            claim_owner_id=owner_id,
            claim_epoch=claim_epoch,
            claim_expires_at=claim_expires_at,
            admitted_at=stored.admitted_at,
            latest_receipt=receipt,
            result=result if result is not None else stored.result,
        ),
        True,
    )


async def _insert_receipt(
    db: aiosqlite.Connection,
    receipt: AgentJobLifecycleReceipt,
) -> None:
    await db.execute(
        """
        INSERT INTO agent_job_lifecycle_events (
            job_id, sequence, transition_sha256, receipt_sha256,
            occurred_at, state, receipt_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt.job_id,
            receipt.sequence,
            receipt.transition_sha256,
            receipt.receipt_sha256,
            receipt.occurred_at,
            receipt.state.value,
            _serialize_receipt(receipt),
        ),
    )


async def _require_stored(
    db: aiosqlite.Connection,
    job_id: str,
    *,
    key: RuntimePayloadKey,
) -> StoredAgentJob:
    cursor = await db.execute(
        "SELECT * FROM agent_jobs WHERE job_id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise AgentJobLifecycleConflictError("AgentJob 不存在。")
    return await _stored_from_row(db, row, key=key)


async def _stored_from_row(
    db: aiosqlite.Connection,
    row: aiosqlite.Row,
    *,
    key: RuntimePayloadKey,
) -> StoredAgentJob:
    try:
        envelope = PayloadEnvelope.from_dict(
            _load_json_object(str(row["payload_envelope_json"]))
        )
        request_sha256 = str(row["request_sha256"])
        raw = open_runtime_payload(
            envelope,
            aad=request_sha256.encode("ascii"),
            key=key,
        )
        request, payload = _decode_payload(raw)
        _verify_payload_binding(request, payload)
        state = AgentJobState(str(row["state"]))
        latest = _deserialize_receipt(str(row["latest_receipt_json"]))
        result_raw = row["result_json"]
        result = (
            _deserialize_result(str(result_raw))
            if result_raw is not None
            else None
        )
        terminal_envelope_raw = row["terminal_payload_envelope_json"]
        terminal_envelope = (
            PayloadEnvelope.from_dict(
                _load_json_object(str(terminal_envelope_raw))
            )
            if terminal_envelope_raw is not None
            else None
        )
        stored = StoredAgentJob(
            job_id=str(row["job_id"]),
            request=request,
            payload_envelope=envelope,
            terminal_payload_envelope=terminal_envelope,
            state=state,
            claim_owner_id=(
                str(row["claim_owner_id"])
                if row["claim_owner_id"] is not None
                else None
            ),
            claim_epoch=int(row["claim_epoch"]),
            claim_expires_at=(
                str(row["claim_expires_at"])
                if row["claim_expires_at"] is not None
                else None
            ),
            admitted_at=str(row["admitted_at"]),
            latest_receipt=latest,
            result=result,
        )
        _validate_stored(stored, row)
        if terminal_envelope is not None:
            terminal_payload = _open_terminal_payload(stored, key=key)
            if result is None:
                raise AgentJobError(
                    "AgentJob terminal payload 缺少 result 绑定。"
                )
            _verify_terminal_payload_binding(result, terminal_payload)
        await _validate_event_chain(db, stored, key=key)
        return stored
    except AgentJobError:
        raise
    except (PayloadEnvelopeError, KeyError, TypeError, ValueError) as exc:
        raise AgentJobError("AgentJob 持久记录无效。") from exc


def _validate_stored(stored: StoredAgentJob, row: aiosqlite.Row) -> None:
    _require_identifier(stored.job_id, field="job_id")
    if stored.job_id != _job_id(stored.request_sha256):
        raise AgentJobError("AgentJob job_id 与 request 不一致。")
    if str(row["request_id"]) != stored.request.request_id:
        raise AgentJobError("AgentJob request_id 不一致。")
    if str(row["request_sha256"]) != stored.request_sha256:
        raise AgentJobError("AgentJob request_sha256 不一致。")
    _aware_time(stored.admitted_at, field="admitted_at")
    if int(row["latest_sequence"]) != stored.latest_receipt.sequence:
        raise AgentJobError("AgentJob latest sequence 不一致。")
    if str(row["latest_receipt_sha256"]) != stored.latest_receipt.receipt_sha256:
        raise AgentJobError("AgentJob latest receipt 不一致。")
    if stored.latest_receipt.job_id != stored.job_id:
        raise AgentJobError("AgentJob latest receipt job_id 不一致。")
    if stored.latest_receipt.state is not stored.state:
        raise AgentJobError("AgentJob latest receipt state 不一致。")
    if stored.latest_receipt.owner_id != stored.claim_owner_id:
        raise AgentJobError("AgentJob claim owner 不一致。")
    if stored.latest_receipt.claim_epoch != stored.claim_epoch:
        raise AgentJobError("AgentJob claim epoch 不一致。")
    if stored.latest_receipt.claim_expires_at != stored.claim_expires_at:
        raise AgentJobError("AgentJob claim expiry 不一致。")
    if stored.result is None:
        if stored.state in TERMINAL_AGENT_JOB_STATES - {
            AgentJobState.CANCELLED,
            AgentJobState.UNKNOWN,
        }:
            raise AgentJobError("AgentJob 终态缺少 result。")
    else:
        if stored.state is not AgentJobState(stored.result.status.value):
            raise AgentJobError("AgentJob result 与终态不一致。")
        if stored.result.request_sha256 != stored.request_sha256:
            raise AgentJobError("AgentJob result request 不一致。")
        if stored.latest_receipt.result_sha256 != stored.result.result_sha256:
            raise AgentJobError("AgentJob result receipt 不一致。")


async def _validate_event_chain(
    db: aiosqlite.Connection,
    stored: StoredAgentJob,
    *,
    key: RuntimePayloadKey,
) -> None:
    cursor = await db.execute(
        """
        SELECT receipt_json FROM agent_job_lifecycle_events
        WHERE job_id = ? ORDER BY sequence
        """,
        (stored.job_id,),
    )
    rows = await cursor.fetchall()
    if len(rows) != stored.latest_receipt.sequence:
        raise AgentJobError("AgentJob lifecycle event 数量不一致。")
    previous: AgentJobLifecycleReceipt | None = None
    for index, row in enumerate(rows, start=1):
        receipt = _deserialize_receipt(str(row["receipt_json"]))
        _verify_receipt_authentication(receipt, key=key.key_bytes)
        if receipt.sequence != index:
            raise AgentJobError("AgentJob lifecycle sequence 不连续。")
        if receipt.job_id != stored.job_id:
            raise AgentJobError("AgentJob lifecycle job_id 不一致。")
        if previous is None:
            if receipt.previous_state is not None:
                raise AgentJobError("AgentJob 首事件 previous_state 无效。")
            if receipt.previous_receipt_sha256 is not None:
                raise AgentJobError("AgentJob 首事件 previous receipt 无效。")
        else:
            if receipt.previous_state is not previous.state:
                raise AgentJobError("AgentJob lifecycle previous_state 断裂。")
            if receipt.previous_receipt_sha256 != previous.receipt_sha256:
                raise AgentJobError("AgentJob lifecycle receipt chain 断裂。")
        previous = receipt
    if previous != stored.latest_receipt:
        raise AgentJobError("AgentJob lifecycle latest event 不一致。")


def _require_live_owner(
    stored: StoredAgentJob,
    *,
    owner_id: str,
    claim_epoch: int,
    now: datetime,
    allowed_states: frozenset[AgentJobState],
) -> None:
    if stored.state not in allowed_states:
        allowed = ",".join(sorted(state.value for state in allowed_states))
        raise AgentJobLifecycleConflictError(
            f"AgentJob 状态 {stored.state.value} 不允许操作（允许：{allowed}）。"
        )
    if stored.claim_owner_id != owner_id or stored.claim_epoch != claim_epoch:
        raise AgentJobLifecycleConflictError(
            "AgentJob owner 或 claim epoch 已变化。"
        )
    if _required_claim_expiry(stored) <= now:
        raise AgentJobLifecycleConflictError("AgentJob claim 已过期。")


def _required_claim_expiry(stored: StoredAgentJob) -> datetime:
    if stored.claim_expires_at is None:
        raise AgentJobError("AgentJob claim expiry 缺失。")
    return _aware_time(stored.claim_expires_at, field="claim_expires_at")


def _verify_payload_binding(
    request: AgentWorkerRequest,
    payload: AgentJobPayload,
) -> None:
    checks = (
        (request.task_id_sha256, _text_digest(payload.task_id), "task_id"),
        (request.session_id_sha256, _text_digest(payload.session_id), "session_id"),
        (request.task_sha256, _text_digest(payload.task), "task"),
        (request.context_sha256, _text_digest(payload.context), "context"),
        (
            request.message_topic_sha256,
            _text_digest(payload.message_topic),
            "message_topic",
        ),
    )
    for expected, actual, field_name in checks:
        if not hmac.compare_digest(expected, actual):
            raise ValueError(
                f"AgentJob payload {field_name} 与 request 不一致。"
            )
    if request.task_bytes != len(payload.task.encode("utf-8")):
        raise ValueError("AgentJob payload task 长度与 request 不一致。")
    if request.context_bytes != len(payload.context.encode("utf-8")):
        raise ValueError("AgentJob payload context 长度与 request 不一致。")


def _encode_payload(
    request: AgentWorkerRequest,
    payload: AgentJobPayload,
) -> bytes:
    request_raw = _serialize_request(request).encode("utf-8")
    if len(request_raw) > _MAX_REQUEST_BYTES:
        raise ValueError("AgentJob encrypted request 超过上限。")
    parts = [_PAYLOAD_MAGIC]
    parts.append(struct.pack(">I", len(request_raw)))
    parts.append(request_raw)
    for name, _maximum, _allow_empty in _PAYLOAD_FIELDS:
        encoded = getattr(payload, name).encode("utf-8")
        parts.append(struct.pack(">I", len(encoded)))
        parts.append(encoded)
    return b"".join(parts)


def _decode_payload(
    value: bytes,
) -> tuple[AgentWorkerRequest, AgentJobPayload]:
    if not isinstance(value, bytes) or not value.startswith(_PAYLOAD_MAGIC):
        raise ValueError("AgentJob payload header 无效。")
    offset = len(_PAYLOAD_MAGIC)
    if len(value) - offset < 4:
        raise ValueError("AgentJob encrypted request framing 截断。")
    request_length = struct.unpack(">I", value[offset : offset + 4])[0]
    offset += 4
    if request_length > _MAX_REQUEST_BYTES or len(value) - offset < request_length:
        raise ValueError("AgentJob encrypted request 长度无效。")
    try:
        request_json = value[offset : offset + request_length].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("AgentJob encrypted request 不是 UTF-8。") from exc
    offset += request_length
    request = _deserialize_request(request_json)
    decoded: dict[str, str] = {}
    for name, maximum, allow_empty in _PAYLOAD_FIELDS:
        if len(value) - offset < 4:
            raise ValueError("AgentJob payload framing 截断。")
        length = struct.unpack(">I", value[offset : offset + 4])[0]
        offset += 4
        if length > maximum or len(value) - offset < length:
            raise ValueError(f"AgentJob payload {name} 长度无效。")
        raw = value[offset : offset + length]
        offset += length
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"AgentJob payload {name} 不是 UTF-8。") from exc
        _require_text(
            text,
            field=name,
            maximum=maximum,
            allow_empty=allow_empty,
        )
        decoded[name] = text
    if offset != len(value):
        raise ValueError("AgentJob payload 包含尾随数据。")
    return request, AgentJobPayload(**decoded)


def _verify_terminal_payload_binding(
    result: AgentWorkerResult,
    payload: AgentJobTerminalPayload,
) -> None:
    if not isinstance(result, AgentWorkerResult):
        raise TypeError("result 必须是 AgentWorkerResult。")
    if not isinstance(payload, AgentJobTerminalPayload):
        raise TypeError("payload 必须是 AgentJobTerminalPayload。")
    response_bytes = payload.response.encode("utf-8")
    if result.response_bytes != len(response_bytes):
        raise ValueError("AgentJob terminal response 长度与 result 不一致。")
    if not hmac.compare_digest(
        result.response_sha256,
        hashlib.sha256(response_bytes).hexdigest(),
    ):
        raise ValueError("AgentJob terminal response 与 result 不一致。")
    if not hmac.compare_digest(
        result.error_sha256,
        _text_digest(payload.error),
    ):
        raise ValueError("AgentJob terminal error 与 result 不一致。")


def _encode_terminal_payload(payload: AgentJobTerminalPayload) -> bytes:
    if not isinstance(payload, AgentJobTerminalPayload):
        raise TypeError("payload 必须是 AgentJobTerminalPayload。")
    parts = [_TERMINAL_PAYLOAD_MAGIC]
    for value in (payload.response, payload.error):
        encoded = value.encode("utf-8")
        parts.append(struct.pack(">I", len(encoded)))
        parts.append(encoded)
    return b"".join(parts)


def _decode_terminal_payload(value: bytes) -> AgentJobTerminalPayload:
    if not isinstance(value, bytes) or not value.startswith(
        _TERMINAL_PAYLOAD_MAGIC
    ):
        raise ValueError("AgentJob terminal payload header 无效。")
    offset = len(_TERMINAL_PAYLOAD_MAGIC)
    decoded: list[str] = []
    for field_name, maximum in (
        ("response", _MAX_RESPONSE_BYTES),
        ("error", _MAX_ERROR_BYTES),
    ):
        if len(value) - offset < 4:
            raise ValueError("AgentJob terminal payload framing 截断。")
        length = struct.unpack(">I", value[offset : offset + 4])[0]
        offset += 4
        if length > maximum or len(value) - offset < length:
            raise ValueError(
                f"AgentJob terminal payload {field_name} 长度无效。"
            )
        raw = value[offset : offset + length]
        offset += length
        try:
            decoded.append(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"AgentJob terminal payload {field_name} 不是 UTF-8。"
            ) from exc
    if offset != len(value):
        raise ValueError("AgentJob terminal payload 包含尾随数据。")
    return AgentJobTerminalPayload(response=decoded[0], error=decoded[1])


def _terminal_payload_aad(
    request_sha256: str,
    result_sha256: str,
) -> bytes:
    _require_sha256(request_sha256, field="request_sha256")
    _require_sha256(result_sha256, field="result_sha256")
    return (
        _TERMINAL_PAYLOAD_AAD
        + request_sha256.encode("ascii")
        + b"\x00"
        + result_sha256.encode("ascii")
    )


def _issue_receipt(
    *,
    job_id: str,
    sequence: int,
    previous_state: AgentJobState | None,
    state: AgentJobState,
    owner_id: str | None,
    claim_epoch: int,
    claim_expires_at: str | None,
    result_sha256: str | None,
    reason_code: str,
    occurred_at: str,
    previous_receipt_sha256: str | None,
    authentication_key: bytes,
) -> AgentJobLifecycleReceipt:
    transition = {
        "job_id": job_id,
        "previous_state": previous_state,
        "state": state,
        "owner_id": owner_id,
        "claim_epoch": claim_epoch,
        "claim_expires_at": claim_expires_at,
        "result_sha256": result_sha256,
        "reason_code": reason_code,
    }
    transition_sha256 = _digest(transition)
    receipt_identity = hashlib.sha256(
        f"{job_id}:{sequence}".encode()
    ).hexdigest()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "receipt_id": f"agent-job-receipt-{receipt_identity}",
        "job_id": job_id,
        "sequence": sequence,
        **transition,
        "occurred_at": occurred_at,
        "previous_receipt_sha256": previous_receipt_sha256,
        "transition_sha256": transition_sha256,
    }
    receipt_sha256 = _digest(payload)
    return AgentJobLifecycleReceipt(
        **payload,
        receipt_sha256=receipt_sha256,
        authentication_sha256=_receipt_authentication(
            payload,
            receipt_sha256=receipt_sha256,
            key=authentication_key,
        ),
    )


def _validate_receipt_semantics(receipt: AgentJobLifecycleReceipt) -> None:
    if receipt.sequence == 1:
        if (
            receipt.previous_state is not None
            or receipt.state is not AgentJobState.ADMITTED
            or receipt.owner_id is not None
            or receipt.claim_epoch != 0
            or receipt.claim_expires_at is not None
            or receipt.reason_code != "agent_job_admitted"
        ):
            raise ValueError("AgentJob admission receipt 语义无效。")
        return
    if receipt.previous_state is None:
        raise ValueError("AgentJob lifecycle previous_state 缺失。")
    if receipt.state in {AgentJobState.CLAIMED, AgentJobState.RUNNING}:
        if (
            receipt.owner_id is None
            or receipt.claim_epoch < 1
            or receipt.claim_expires_at is None
        ):
            raise ValueError("AgentJob active receipt claim 事实缺失。")
    transition = (
        receipt.previous_state,
        receipt.state,
        receipt.reason_code,
    )
    active_transitions = {
        (
            AgentJobState.ADMITTED,
            AgentJobState.CLAIMED,
            "agent_job_claimed",
        ),
        (
            AgentJobState.CLAIMED,
            AgentJobState.CLAIMED,
            "agent_job_claim_taken_over",
        ),
        (
            AgentJobState.CLAIMED,
            AgentJobState.CLAIMED,
            "agent_job_claim_renewed",
        ),
        (
            AgentJobState.CLAIMED,
            AgentJobState.RUNNING,
            "agent_job_running",
        ),
        (
            AgentJobState.RUNNING,
            AgentJobState.RUNNING,
            "agent_job_claim_renewed",
        ),
    }
    result_transitions = {
        (
            AgentJobState.RUNNING,
            AgentJobState.COMPLETED,
            "agent_completed",
        ),
        (
            AgentJobState.RUNNING,
            AgentJobState.ERROR,
            "agent_failed",
        ),
        (
            AgentJobState.RUNNING,
            AgentJobState.TIMEOUT,
            "agent_timeout",
        ),
        (
            AgentJobState.RUNNING,
            AgentJobState.MAX_TURNS,
            "agent_max_turns",
        ),
        (
            AgentJobState.RUNNING,
            AgentJobState.CANCELLED,
            "agent_cancelled",
        ),
    }
    non_result_transitions = {
        (
            AgentJobState.ADMITTED,
            AgentJobState.CANCELLED,
            "agent_job_cancelled_before_claim",
        ),
        (
            AgentJobState.RUNNING,
            AgentJobState.UNKNOWN,
            "agent_job_recovery_unknown",
        ),
    }
    if transition in active_transitions:
        if receipt.result_sha256 is not None:
            raise ValueError("AgentJob active receipt 不能绑定 result。")
        return
    if transition in result_transitions:
        if receipt.result_sha256 is None:
            raise ValueError("AgentJob result terminal receipt 缺少 result。")
        return
    if transition in non_result_transitions:
        if receipt.result_sha256 is not None:
            raise ValueError("AgentJob recovery/cancel receipt 不能绑定 result。")
        return
    raise ValueError("AgentJob lifecycle transition 语义无效。")


def _receipt_payload(receipt: AgentJobLifecycleReceipt) -> dict[str, Any]:
    return {
        "schema_version": receipt.schema_version,
        "receipt_id": receipt.receipt_id,
        "job_id": receipt.job_id,
        "sequence": receipt.sequence,
        "previous_state": receipt.previous_state,
        "state": receipt.state,
        "owner_id": receipt.owner_id,
        "claim_epoch": receipt.claim_epoch,
        "claim_expires_at": receipt.claim_expires_at,
        "result_sha256": receipt.result_sha256,
        "reason_code": receipt.reason_code,
        "occurred_at": receipt.occurred_at,
        "previous_receipt_sha256": receipt.previous_receipt_sha256,
        "transition_sha256": receipt.transition_sha256,
    }


def _verify_receipt_authentication(
    receipt: AgentJobLifecycleReceipt,
    *,
    key: bytes,
) -> None:
    expected = _receipt_authentication(
        _receipt_payload(receipt),
        receipt_sha256=receipt.receipt_sha256,
        key=key,
    )
    if not hmac.compare_digest(receipt.authentication_sha256, expected):
        raise AgentJobError("AgentJob lifecycle authentication 无效。")


def _receipt_authentication(
    payload: Mapping[str, Any],
    *,
    receipt_sha256: str,
    key: bytes,
) -> str:
    derived = hmac.new(
        key,
        b"naumi-agent-job-lifecycle-authentication-v1",
        hashlib.sha256,
    ).digest()
    authenticated = {
        **payload,
        "receipt_sha256": receipt_sha256,
    }
    return hmac.new(
        derived,
        _canonical_json(authenticated).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _issue_publication_receipt(
    *,
    publication_id: str,
    job_id: str,
    sequence: int,
    request_sha256: str,
    result_sha256: str,
    state: AgentJobPublicationState,
    owner_id: str | None,
    claim_epoch: int,
    claim_expires_at: str | None,
    attempt_count: int,
    delivery_sha256: str | None,
    published_at: str | None,
    reason_code: str,
    occurred_at: str,
    previous_receipt_sha256: str | None,
    key: bytes,
) -> AgentJobPublicationReceipt:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "publication_id": publication_id,
        "job_id": job_id,
        "sequence": sequence,
        "request_sha256": request_sha256,
        "result_sha256": result_sha256,
        "state": state,
        "owner_id": owner_id,
        "claim_epoch": claim_epoch,
        "claim_expires_at": claim_expires_at,
        "attempt_count": attempt_count,
        "delivery_sha256": delivery_sha256,
        "published_at": published_at,
        "reason_code": reason_code,
        "occurred_at": occurred_at,
        "previous_receipt_sha256": previous_receipt_sha256,
    }
    receipt_sha256 = _digest(payload)
    return AgentJobPublicationReceipt(
        **payload,
        receipt_sha256=receipt_sha256,
        authentication_sha256=_publication_receipt_authentication(
            payload,
            receipt_sha256=receipt_sha256,
            key=key,
        ),
    )


def _validate_publication_receipt_semantics(
    receipt: AgentJobPublicationReceipt,
) -> None:
    if receipt.sequence == 1:
        if (
            receipt.state is not AgentJobPublicationState.PENDING
            or receipt.owner_id is not None
            or receipt.claim_epoch != 0
            or receipt.claim_expires_at is not None
            or receipt.attempt_count != 0
            or receipt.delivery_sha256 is not None
            or receipt.published_at is not None
            or receipt.reason_code != "agent_publication_pending"
            or receipt.previous_receipt_sha256 is not None
        ):
            raise ValueError("AgentJob publication initial receipt 语义无效。")
        return
    if receipt.previous_receipt_sha256 is None:
        raise ValueError("AgentJob publication previous receipt 缺失。")
    if receipt.state is AgentJobPublicationState.CLAIMED:
        if (
            receipt.owner_id is None
            or receipt.claim_epoch < 1
            or receipt.claim_expires_at is None
            or _aware_time(
                receipt.claim_expires_at,
                field="claim_expires_at",
            )
            <= _aware_time(receipt.occurred_at, field="occurred_at")
            or receipt.attempt_count < 1
            or receipt.delivery_sha256 is not None
            or receipt.published_at is not None
            or receipt.reason_code not in {
                "agent_publication_claimed",
                "agent_publication_claim_taken_over",
                "agent_publication_claim_renewed",
            }
        ):
            raise ValueError("AgentJob publication claimed receipt 语义无效。")
        return
    if receipt.state is AgentJobPublicationState.PENDING:
        if (
            receipt.owner_id is not None
            or receipt.claim_epoch < 1
            or receipt.claim_expires_at is not None
            or receipt.attempt_count < 1
            or receipt.delivery_sha256 is not None
            or receipt.published_at is not None
            or receipt.reason_code != "agent_publication_released"
        ):
            raise ValueError("AgentJob publication release receipt 语义无效。")
        return
    if (
        receipt.state is not AgentJobPublicationState.PUBLISHED
        or receipt.owner_id is None
        or receipt.claim_epoch < 1
        or receipt.claim_expires_at is not None
        or receipt.attempt_count < 1
        or receipt.delivery_sha256 is None
        or receipt.published_at is None
        or receipt.published_at != receipt.occurred_at
        or receipt.reason_code != "agent_publication_published"
    ):
        raise ValueError("AgentJob publication published receipt 语义无效。")


def _publication_receipt_payload(
    receipt: AgentJobPublicationReceipt,
) -> dict[str, Any]:
    return {
        "schema_version": receipt.schema_version,
        "publication_id": receipt.publication_id,
        "job_id": receipt.job_id,
        "sequence": receipt.sequence,
        "request_sha256": receipt.request_sha256,
        "result_sha256": receipt.result_sha256,
        "state": receipt.state,
        "owner_id": receipt.owner_id,
        "claim_epoch": receipt.claim_epoch,
        "claim_expires_at": receipt.claim_expires_at,
        "attempt_count": receipt.attempt_count,
        "delivery_sha256": receipt.delivery_sha256,
        "published_at": receipt.published_at,
        "reason_code": receipt.reason_code,
        "occurred_at": receipt.occurred_at,
        "previous_receipt_sha256": receipt.previous_receipt_sha256,
    }


def _verify_publication_receipt_authentication(
    receipt: AgentJobPublicationReceipt,
    *,
    key: bytes,
) -> None:
    expected = _publication_receipt_authentication(
        _publication_receipt_payload(receipt),
        receipt_sha256=receipt.receipt_sha256,
        key=key,
    )
    if not hmac.compare_digest(receipt.authentication_sha256, expected):
        raise AgentJobError(
            "AgentJob publication receipt authentication 无效。"
        )


def _publication_receipt_authentication(
    payload: Mapping[str, Any],
    *,
    receipt_sha256: str,
    key: bytes,
) -> str:
    derived = hmac.new(
        key,
        b"naumi-agent-job-publication-authentication-v1",
        hashlib.sha256,
    ).digest()
    return hmac.new(
        derived,
        _canonical_json({
            **payload,
            "receipt_sha256": receipt_sha256,
        }).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _serialize_request(request: AgentWorkerRequest) -> str:
    return _canonical_json(asdict(request))


def _deserialize_request(value: str) -> AgentWorkerRequest:
    payload = _load_json_object(value)
    expected = set(AgentWorkerRequest.__dataclass_fields__)
    if set(payload) != expected:
        raise ValueError("AgentJob request 字段集合无效。")
    payload["tool_scope"] = tuple(payload["tool_scope"])
    return AgentWorkerRequest(**payload)


def _serialize_result(result: AgentWorkerResult) -> str:
    return _canonical_json(asdict(result))


def _deserialize_result(value: str) -> AgentWorkerResult:
    payload = _load_json_object(value)
    expected = set(AgentWorkerResult.__dataclass_fields__)
    if set(payload) != expected:
        raise ValueError("AgentJob result 字段集合无效。")
    payload["status"] = AgentWorkerResultStatus(payload["status"])
    return AgentWorkerResult(**payload)


def _serialize_receipt(receipt: AgentJobLifecycleReceipt) -> str:
    return _canonical_json(asdict(receipt))


def _deserialize_receipt(value: str) -> AgentJobLifecycleReceipt:
    payload = _load_json_object(value)
    expected = set(AgentJobLifecycleReceipt.__dataclass_fields__)
    if set(payload) != expected:
        raise ValueError("AgentJob lifecycle 字段集合无效。")
    if payload["previous_state"] is not None:
        payload["previous_state"] = AgentJobState(payload["previous_state"])
    payload["state"] = AgentJobState(payload["state"])
    return AgentJobLifecycleReceipt(**payload)


def _serialize_publication_receipt(
    receipt: AgentJobPublicationReceipt,
) -> str:
    return _canonical_json(asdict(receipt))


def _deserialize_publication_receipt(
    value: str,
) -> AgentJobPublicationReceipt:
    payload = _load_json_object(value)
    expected = set(AgentJobPublicationReceipt.__dataclass_fields__)
    if set(payload) != expected:
        raise ValueError("AgentJob publication receipt 字段集合无效。")
    payload["state"] = AgentJobPublicationState(payload["state"])
    return AgentJobPublicationReceipt(**payload)


def _load_json_object(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise TypeError("AgentJob JSON 必须是对象。")
    return decoded


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _job_id(request_sha256: str) -> str:
    _require_sha256(request_sha256, field="request_sha256")
    return f"agent-job-{request_sha256}"


def _publication_id(job_id: str, result_sha256: str) -> str:
    _require_identifier(job_id, field="job_id")
    _require_sha256(result_sha256, field="result_sha256")
    identity = hashlib.sha256(
        f"{job_id}:{result_sha256}".encode("ascii")
    ).hexdigest()
    return f"agent-publication-{identity}"


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} 标识格式无效。")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} 必须是小写 SHA-256。")


def _require_positive_int(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} 必须是正整数。")


def _require_bounded_limit(value: int, *, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"limit 必须是 1 到 {maximum} 之间的整数。")


def _require_capacity_limit(
    value: int,
    *,
    field: str,
    minimum: int,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 10_000
    ):
        raise ValueError(
            f"{field} 必须是 {minimum} 到 10000 之间的整数。"
        )


def _require_lease_seconds(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_LEASE_SECONDS
    ):
        raise ValueError(
            f"AgentJob lease_seconds 必须在 1 到 {_MAX_LEASE_SECONDS} 之间。"
        )


def _require_text(
    value: str,
    *,
    field: str,
    maximum: int,
    allow_empty: bool,
) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是字符串。")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field} 不能为空。")
    if "\x00" in value:
        raise ValueError(f"{field} 不能包含 NUL。")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} 超过 {maximum} 字节上限。")


def _aware_time(value: str, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是 ISO 时间字符串。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是有效 ISO 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return parsed


def _regular_file_exists(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise AgentJobError("AgentJob 数据库路径不是普通文件。")
    return True


async def _user_tables(db: aiosqlite.Connection) -> tuple[str, ...]:
    cursor = await db.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    )
    return tuple(str(row[0]) for row in await cursor.fetchall())


async def _apply_schema_v3(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(agent_jobs)")
    columns = {
        str(row[1]): (str(row[2]).upper(), int(row[3]))
        for row in await cursor.fetchall()
    }
    existing = columns.get("terminal_payload_envelope_json")
    if existing is not None:
        if existing != ("TEXT", 0):
            raise AgentJobError(
                "AgentJob schema v3 terminal payload 列定义无效。"
            )
        return
    for statement in _SCHEMA_V3:
        await db.execute(statement)


async def _apply_schema_v4(
    db: aiosqlite.Connection,
    *,
    allow_create: bool,
) -> None:
    expected = {
        "agent_job_publications": {
            "publication_id", "job_id", "request_sha256", "result_sha256",
            "state", "owner_id", "claim_epoch", "claim_expires_at",
            "attempt_count", "created_at", "published_at",
            "latest_sequence", "latest_receipt_sha256",
            "latest_receipt_json",
        },
        "agent_job_publication_events": {
            "publication_id", "sequence", "receipt_sha256",
            "occurred_at", "state", "receipt_json",
        },
    }
    tables = set(await _user_tables(db))
    present = set(expected) & tables
    if not present:
        if not allow_create:
            raise AgentJobError(
                "AgentJob schema v4 publication 表缺失。"
            )
        for statement in _SCHEMA_V4:
            await db.execute(statement)
        return
    if present != set(expected):
        raise AgentJobError(
            "AgentJob schema v4 publication 表处于不完整状态。"
        )
    for table, expected_columns in expected.items():
        cursor = await db.execute(f"PRAGMA table_info({table})")
        actual_columns = {str(row[1]) for row in await cursor.fetchall()}
        if actual_columns != expected_columns:
            raise AgentJobError(
                f"AgentJob schema v4 {table} 列定义无效。"
            )
    cursor = await db.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'index'
          AND name = 'agent_job_publications_recoverable'
          AND tbl_name = 'agent_job_publications'
        """
    )
    if await cursor.fetchone() is None:
        raise AgentJobError(
            "AgentJob schema v4 publication 恢复索引缺失。"
        )


_STATE_VALUES = ", ".join(f"'{state.value}'" for state in AgentJobState)
_PUBLICATION_STATE_VALUES = ", ".join(
    f"'{state.value}'" for state in AgentJobPublicationState
)
_SCHEMA_V1 = (
    f"""
    CREATE TABLE agent_jobs (
        job_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ({_STATE_VALUES})),
        claim_owner_id TEXT,
        claim_epoch INTEGER NOT NULL CHECK (claim_epoch >= 0),
        claim_expires_at TEXT,
        admitted_at TEXT NOT NULL,
        latest_sequence INTEGER NOT NULL CHECK (latest_sequence >= 1),
        latest_receipt_sha256 TEXT NOT NULL,
        latest_receipt_json TEXT NOT NULL,
        payload_envelope_json TEXT NOT NULL,
        result_json TEXT
    )
    """,
    """
    CREATE INDEX agent_jobs_claimable
    ON agent_jobs (state, claim_expires_at, admitted_at, job_id)
    """,
    f"""
    CREATE TABLE agent_job_lifecycle_events (
        job_id TEXT NOT NULL REFERENCES agent_jobs(job_id) ON DELETE RESTRICT,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        transition_sha256 TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL UNIQUE,
        occurred_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ({_STATE_VALUES})),
        receipt_json TEXT NOT NULL,
        PRIMARY KEY (job_id, sequence),
        UNIQUE (job_id, transition_sha256)
    )
    """,
)

_SCHEMA_V2 = (
    """
    CREATE TABLE agent_job_capacity_policy (
        policy_id INTEGER PRIMARY KEY CHECK (policy_id = 1),
        max_active_jobs INTEGER NOT NULL
            CHECK (max_active_jobs BETWEEN 1 AND 10000),
        max_waiters INTEGER NOT NULL
            CHECK (max_waiters BETWEEN 0 AND 10000),
        configured_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

_SCHEMA_V3 = (
    """
    ALTER TABLE agent_jobs
    ADD COLUMN terminal_payload_envelope_json TEXT
    """,
)

_SCHEMA_V4 = (
    f"""
    CREATE TABLE agent_job_publications (
        publication_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL UNIQUE
            REFERENCES agent_jobs(job_id) ON DELETE RESTRICT,
        request_sha256 TEXT NOT NULL,
        result_sha256 TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ({_PUBLICATION_STATE_VALUES})),
        owner_id TEXT,
        claim_epoch INTEGER NOT NULL CHECK (claim_epoch >= 0),
        claim_expires_at TEXT,
        attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
        created_at TEXT NOT NULL,
        published_at TEXT,
        latest_sequence INTEGER NOT NULL CHECK (latest_sequence >= 1),
        latest_receipt_sha256 TEXT NOT NULL,
        latest_receipt_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX agent_job_publications_recoverable
    ON agent_job_publications (state, claim_expires_at, created_at, publication_id)
    """,
    f"""
    CREATE TABLE agent_job_publication_events (
        publication_id TEXT NOT NULL
            REFERENCES agent_job_publications(publication_id) ON DELETE RESTRICT,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        receipt_sha256 TEXT NOT NULL UNIQUE,
        occurred_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ({_PUBLICATION_STATE_VALUES})),
        receipt_json TEXT NOT NULL,
        PRIMARY KEY (publication_id, sequence)
    )
    """,
)


__all__ = [
    "AGENT_JOB_SCHEMA_VERSION",
    "AgentJobCapacityExhaustedError",
    "AgentJobCapacityPolicy",
    "AgentJobCapacitySnapshot",
    "AgentJobConflictError",
    "AgentJobError",
    "AgentJobKeyUnavailableError",
    "AgentJobLifecycleConflictError",
    "AgentJobLifecycleReceipt",
    "AgentJobPayload",
    "AgentJobPublicationBacklog",
    "AgentJobPublicationReceipt",
    "AgentJobPublicationState",
    "AgentJobPublicationTransition",
    "AgentJobState",
    "AgentJobStore",
    "AgentJobTerminalPayload",
    "AgentJobTransitionResult",
    "StoredAgentJob",
    "StoredAgentJobPublication",
    "TERMINAL_AGENT_JOB_STATES",
]
