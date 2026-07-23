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

AGENT_JOB_SCHEMA_VERSION = 1
_PAYLOAD_MAGIC = b"NAUMI_AGENT_JOB_PAYLOAD_V1\x00"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TASK_ID_BYTES = 512
_MAX_SESSION_ID_BYTES = 512
_MAX_TASK_BYTES = 2 * 1024**2
_MAX_CONTEXT_BYTES = 16 * 1024**2
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
        if not isinstance(request, AgentWorkerRequest):
            raise TypeError("request 必须是 AgentWorkerRequest。")
        if not isinstance(payload, AgentJobPayload):
            raise TypeError("payload 必须是 AgentJobPayload。")
        _verify_payload_binding(request, payload)
        key = self._runtime_key()
        envelope = seal_runtime_payload(
            _encode_payload(request, payload),
            aad=request.request_sha256.encode("ascii"),
            key=key,
        )
        job_id = _job_id(request.request_sha256)
        admitted_at = self._now().isoformat()
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
            occurred_at=admitted_at,
            previous_receipt_sha256=None,
            authentication_key=key.key_bytes,
        )
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM agent_jobs WHERE request_id = ?",
                    (request.request_id,),
                )
                row = await cursor.fetchone()
                if row is not None:
                    existing = await _stored_from_row(db, row, key=key)
                    if not hmac.compare_digest(
                        existing.request_sha256,
                        request.request_sha256,
                    ):
                        raise AgentJobConflictError(
                            "AgentJob request_id 已绑定其他请求。"
                        )
                    recovered = self._open_payload(existing, key=key)
                    if recovered != payload:
                        raise AgentJobConflictError(
                            "AgentJob request_id 已绑定其他 payload。"
                        )
                    await db.commit()
                    return existing
                envelope_json = _canonical_json(envelope.to_dict())
                receipt_json = _serialize_receipt(receipt)
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
                        job_id,
                        request.request_id,
                        request.request_sha256,
                        AgentJobState.ADMITTED.value,
                        admitted_at,
                        receipt.sequence,
                        receipt.receipt_sha256,
                        receipt_json,
                        envelope_json,
                    ),
                )
                await _insert_receipt(db, receipt)
                await db.commit()
                return StoredAgentJob(
                    job_id=job_id,
                    request=request,
                    payload_envelope=envelope,
                    state=AgentJobState.ADMITTED,
                    claim_owner_id=None,
                    claim_epoch=0,
                    claim_expires_at=None,
                    admitted_at=admitted_at,
                    latest_receipt=receipt,
                    result=None,
                )
        except (AgentJobConflictError, AgentJobError):
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise AgentJobError("无法持久化 AgentJob。") from exc

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
    ) -> AgentJobTransitionResult:
        if not isinstance(result, AgentWorkerResult):
            raise TypeError("result 必须是 AgentWorkerResult。")
        target = AgentJobState(result.status.value)
        return await self._owner_transition(
            job_id,
            owner_id=owner_id,
            claim_epoch=claim_epoch,
            target_state=target,
            result=result,
            reason_code=result.reason_code,
            allowed_states=frozenset({AgentJobState.RUNNING}),
        )

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
        try:
            raw = open_runtime_payload(
                stored.payload_envelope,
                aad=stored.request_sha256.encode("ascii"),
                key=key or self._runtime_key(),
            )
            request, payload = _decode_payload(raw)
            if request != stored.request:
                raise ValueError("AgentJob encrypted request 与主记录不一致。")
            _verify_payload_binding(stored.request, payload)
            return payload
        except (PayloadEnvelopeError, TypeError, ValueError) as exc:
            raise AgentJobError("AgentJob payload 无法认证或恢复。") from exc

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
                        await db.execute(
                            f"PRAGMA user_version = {AGENT_JOB_SCHEMA_VERSION}"
                        )
                    elif version != AGENT_JOB_SCHEMA_VERSION:
                        raise AgentJobError(
                            f"AgentJob schema v{version} 不受支持；"
                            f"当前仅支持 v{AGENT_JOB_SCHEMA_VERSION}。"
                        )
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
    await _insert_receipt(db, receipt)
    cursor = await db.execute(
        """
        UPDATE agent_jobs
        SET state = ?, claim_owner_id = ?, claim_epoch = ?,
            claim_expires_at = ?, latest_sequence = ?,
            latest_receipt_sha256 = ?, latest_receipt_json = ?,
            result_json = COALESCE(?, result_json)
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
        stored = StoredAgentJob(
            job_id=str(row["job_id"]),
            request=request,
            payload_envelope=envelope,
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


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} 标识格式无效。")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} 必须是小写 SHA-256。")


def _require_positive_int(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} 必须是正整数。")


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


_STATE_VALUES = ", ".join(f"'{state.value}'" for state in AgentJobState)
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


__all__ = [
    "AGENT_JOB_SCHEMA_VERSION",
    "AgentJobConflictError",
    "AgentJobError",
    "AgentJobKeyUnavailableError",
    "AgentJobLifecycleConflictError",
    "AgentJobLifecycleReceipt",
    "AgentJobPayload",
    "AgentJobState",
    "AgentJobStore",
    "AgentJobTransitionResult",
    "StoredAgentJob",
    "TERMINAL_AGENT_JOB_STATES",
]
