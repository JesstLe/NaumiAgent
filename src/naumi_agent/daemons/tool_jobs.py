"""Durable admission authority for immutable Tool daemon jobs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import stat
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from naumi_agent.daemons.execution_grants import (
    ExecutionGrantAuthority,
    ExecutionGrantContract,
    ExecutionGrantRequest,
    execution_arguments_sha256,
)
from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionRequirements,
    WorkerAdmissionResult,
    WorkerHealthReport,
    WorkerKind,
)
from naumi_agent.daemons.worker_registry import WorkerRegistryStore

TOOL_JOB_SCHEMA_VERSION = 2
_MAX_CONTRACT_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ToolJobState(StrEnum):
    ADMITTED = "admitted"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TERMINAL_TOOL_JOB_STATES = frozenset(
    {
        ToolJobState.SUCCEEDED,
        ToolJobState.FAILED,
        ToolJobState.CANCELLED,
        ToolJobState.UNKNOWN,
    }
)


class ToolJobSideEffect(StrEnum):
    NONE = "none"
    POSSIBLE = "possible"
    OBSERVED = "observed"


class ToolJobValidationReason(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    EXPIRED = "expired"
    REQUEST_MISMATCH = "request_mismatch"
    REQUIREMENTS_MISMATCH = "requirements_mismatch"
    EXECUTION_GRANT_INVALID = "execution_grant_invalid"
    WORKER_NOT_ADMITTED = "worker_not_admitted"


class ToolJobError(RuntimeError):
    """Raised when ToolJob authority cannot provide trustworthy state."""


class ToolJobConflictError(ToolJobError):
    """Raised when an idempotency key is reused for different job facts."""


class ToolJobLifecycleConflictError(ToolJobError):
    """Raised when a lifecycle retry conflicts with durable job facts."""


@dataclass(frozen=True, slots=True)
class ToolJobRequest:
    session_id: str
    run_id: str
    call_id: str
    tool_name: str
    arguments: Mapping[str, object]
    idempotency_key: str
    worker_id: str
    authorization_reference: str
    execution_grant_id: str

    def execution_grant_request(self) -> ExecutionGrantRequest:
        return ExecutionGrantRequest(
            session_id=self.session_id,
            run_id=self.run_id,
            call_id=self.call_id,
            tool_name=self.tool_name,
            arguments=self.arguments,
            idempotency_key=self.idempotency_key,
            worker_id=self.worker_id,
            authorization_reference=self.authorization_reference,
        )


@dataclass(frozen=True, slots=True)
class ImmutableToolJob:
    schema_version: int
    job_id: str
    session_id: str
    run_id: str
    call_id: str
    tool_name: str
    tool_family: str
    arguments_sha256: str
    idempotency_key: str
    workspace_sha256: str
    execution_grant_id: str
    execution_grant_sha256: str
    authorization_reference: str
    worker_id: str
    worker_instance_id: str
    worker_epoch: int
    worker_contract_sha256: str
    lease_owner_id: str
    lease_epoch: int
    requirements_sha256: str
    admitted_at: str
    expires_at: str
    request_sha256: str
    job_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("ToolJob schema_version 必须为 1。")
        for field in (
            "job_id",
            "session_id",
            "run_id",
            "call_id",
            "tool_name",
            "tool_family",
            "idempotency_key",
            "execution_grant_id",
            "authorization_reference",
            "worker_id",
            "worker_instance_id",
            "lease_owner_id",
        ):
            _require_identifier(getattr(self, field), field=field)
        for field in (
            "arguments_sha256",
            "workspace_sha256",
            "execution_grant_sha256",
            "worker_contract_sha256",
            "requirements_sha256",
            "request_sha256",
            "job_sha256",
        ):
            _require_sha256(getattr(self, field), field=field)
        _require_positive_int(self.worker_epoch, field="worker_epoch")
        _require_positive_int(self.lease_epoch, field="lease_epoch")
        admitted = _aware_time(self.admitted_at, field="admitted_at")
        expires = _aware_time(self.expires_at, field="expires_at")
        if expires <= admitted:
            raise ValueError("ToolJob expires_at 必须晚于 admitted_at。")


@dataclass(frozen=True, slots=True)
class StoredToolJob:
    contract: ImmutableToolJob
    state: ToolJobState
    latest_receipt: ToolJobLifecycleReceipt


@dataclass(frozen=True, slots=True)
class ToolJobTransitionResult:
    job: StoredToolJob
    applied: bool

    @property
    def should_send_payload(self) -> bool:
        """Only a newly committed dispatch transition authorizes transport send."""
        return self.applied and self.job.state is ToolJobState.DISPATCHED


@dataclass(frozen=True, slots=True)
class ToolJobLifecycleReceipt:
    schema_version: int
    receipt_id: str
    job_id: str
    sequence: int
    previous_state: ToolJobState | None
    state: ToolJobState
    worker_id: str
    worker_instance_id: str
    worker_epoch: int
    dispatch_id: str | None
    side_effect: ToolJobSideEffect
    result_code: str
    exit_code: int | None
    output_sha256: str | None
    artifact_manifest_sha256: str | None
    occurred_at: str
    previous_receipt_sha256: str | None
    transition_sha256: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("ToolJob lifecycle schema_version 必须为 1。")
        for field in (
            "receipt_id",
            "job_id",
            "worker_id",
            "worker_instance_id",
            "result_code",
        ):
            _require_identifier(getattr(self, field), field=field)
        if self.dispatch_id is not None:
            _require_identifier(self.dispatch_id, field="dispatch_id")
        _require_positive_int(self.sequence, field="sequence")
        _require_positive_int(self.worker_epoch, field="worker_epoch")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ValueError("exit_code 必须是整数或 null。")
        for field in (
            "output_sha256",
            "artifact_manifest_sha256",
            "previous_receipt_sha256",
        ):
            value = getattr(self, field)
            if value is not None:
                _require_sha256(value, field=field)
        _require_sha256(self.transition_sha256, field="transition_sha256")
        _require_sha256(self.receipt_sha256, field="receipt_sha256")
        _aware_time(self.occurred_at, field="occurred_at")
        _validate_lifecycle_receipt_semantics(self)


@dataclass(frozen=True, slots=True)
class ToolJobValidation:
    allowed: bool
    reasons: tuple[ToolJobValidationReason, ...]
    checked_at: str
    contract: ImmutableToolJob | None
    worker_admission: WorkerAdmissionResult | None


class ToolJobStore:
    """SQLite source of truth for immutable admitted ToolJob contracts."""

    def __init__(self, db_path: str | Path) -> None:
        unresolved = Path(db_path).expanduser()
        if not unresolved.is_absolute():
            raise ValueError("ToolJob 路径必须是绝对路径。")
        self._db_path = unresolved.resolve(strict=False)
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def admit(self, contract: ImmutableToolJob) -> StoredToolJob:
        if not isinstance(contract, ImmutableToolJob):
            raise TypeError("contract 必须是 ImmutableToolJob。")
        if not verify_tool_job(contract):
            raise ValueError("ToolJob 摘要校验失败。")
        raw = _serialize_contract(contract)
        receipt = _issue_admission_receipt(contract)
        receipt_raw = _serialize_lifecycle_receipt(receipt)
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM tool_jobs WHERE idempotency_key = ?",
                    (contract.idempotency_key,),
                )
                row = await cursor.fetchone()
                if row is not None:
                    existing = _stored_job_from_row(row)
                    await _validate_event_chain(db, existing)
                    if existing.contract.request_sha256 != contract.request_sha256:
                        raise ToolJobConflictError(
                            "ToolJob idempotency key 已绑定其他请求。"
                        )
                    await db.commit()
                    return existing
                await db.execute(
                    """
                    INSERT INTO tool_jobs (
                        job_id, idempotency_key, request_sha256, job_sha256,
                        admitted_at, expires_at, state, latest_sequence,
                        latest_receipt_sha256, latest_receipt_json, contract_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contract.job_id,
                        contract.idempotency_key,
                        contract.request_sha256,
                        contract.job_sha256,
                        contract.admitted_at,
                        contract.expires_at,
                        ToolJobState.ADMITTED.value,
                        receipt.sequence,
                        receipt.receipt_sha256,
                        receipt_raw,
                        raw,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO tool_job_lifecycle_events (
                        job_id, sequence, transition_sha256, receipt_sha256,
                        occurred_at, state, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contract.job_id,
                        receipt.sequence,
                        receipt.transition_sha256,
                        receipt.receipt_sha256,
                        receipt.occurred_at,
                        receipt.state.value,
                        receipt_raw,
                    ),
                )
                await db.commit()
                return StoredToolJob(contract, ToolJobState.ADMITTED, receipt)
        except ToolJobConflictError:
            raise
        except aiosqlite.IntegrityError as exc:
            raise ToolJobConflictError("ToolJob 与现有记录冲突。") from exc
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ToolJobError("无法持久化 ToolJob。") from exc

    async def get(self, job_id: str) -> StoredToolJob | None:
        _require_identifier(job_id, field="job_id")
        if not _regular_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    "SELECT * FROM tool_jobs WHERE job_id = ?",
                    (job_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                stored = _stored_job_from_row(row)
                await _validate_event_chain(db, stored)
                return stored
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ToolJobError("无法读取 ToolJob。") from exc

    async def _transition(
        self,
        *,
        job_id: str,
        target_state: ToolJobState,
        dispatch_id: str | None,
        side_effect: ToolJobSideEffect,
        result_code: str,
        occurred_at: str,
        exit_code: int | None = None,
        output_sha256: str | None = None,
        artifact_manifest_sha256: str | None = None,
        expected_latest_receipt_sha256: str | None = None,
    ) -> ToolJobTransitionResult:
        """Append one monotonic lifecycle fact and atomically advance latest state."""
        _require_identifier(job_id, field="job_id")
        if not isinstance(target_state, ToolJobState):
            raise TypeError("target_state 必须是 ToolJobState。")
        if not isinstance(side_effect, ToolJobSideEffect):
            raise TypeError("side_effect 必须是 ToolJobSideEffect。")
        _require_identifier(result_code, field="result_code")
        timestamp = _canonical_time(occurred_at, field="occurred_at")
        if dispatch_id is not None:
            _require_identifier(dispatch_id, field="dispatch_id")
        for field, value in (
            ("output_sha256", output_sha256),
            ("artifact_manifest_sha256", artifact_manifest_sha256),
        ):
            if value is not None:
                _require_sha256(value, field=field)
        if expected_latest_receipt_sha256 is not None:
            _require_sha256(
                expected_latest_receipt_sha256,
                field="expected_latest_receipt_sha256",
            )
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM tool_jobs WHERE job_id = ?",
                    (job_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ToolJobLifecycleConflictError("ToolJob 不存在。")
                stored = _stored_job_from_row(row)
                await _validate_event_chain(db, stored)
                if (
                    expected_latest_receipt_sha256 is not None
                    and stored.latest_receipt.receipt_sha256
                    != expected_latest_receipt_sha256
                ):
                    raise ToolJobLifecycleConflictError(
                        "ToolJob recovery fence 已变化。"
                    )
                transition_sha256 = _lifecycle_transition_digest(
                    job_id=stored.contract.job_id,
                    target_state=target_state,
                    dispatch_id=dispatch_id,
                    side_effect=side_effect,
                    result_code=result_code,
                    exit_code=exit_code,
                    output_sha256=output_sha256,
                    artifact_manifest_sha256=artifact_manifest_sha256,
                )
                if stored.state is target_state:
                    if hmac.compare_digest(
                        stored.latest_receipt.transition_sha256,
                        transition_sha256,
                    ):
                        await db.commit()
                        return ToolJobTransitionResult(stored, False)
                    raise ToolJobLifecycleConflictError(
                        "ToolJob 状态已由不同生命周期事实占用。"
                    )
                _require_transition(stored.state, target_state)
                receipt = _issue_lifecycle_receipt(
                    stored=stored,
                    target_state=target_state,
                    dispatch_id=dispatch_id,
                    side_effect=side_effect,
                    result_code=result_code,
                    occurred_at=timestamp,
                    exit_code=exit_code,
                    output_sha256=output_sha256,
                    artifact_manifest_sha256=artifact_manifest_sha256,
                )
                raw = _serialize_lifecycle_receipt(receipt)
                await db.execute(
                    """
                    INSERT INTO tool_job_lifecycle_events (
                        job_id, sequence, transition_sha256, receipt_sha256,
                        occurred_at, state, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        receipt.sequence,
                        receipt.transition_sha256,
                        receipt.receipt_sha256,
                        receipt.occurred_at,
                        receipt.state.value,
                        raw,
                    ),
                )
                result = await db.execute(
                    """
                    UPDATE tool_jobs
                    SET state = ?, latest_sequence = ?,
                        latest_receipt_sha256 = ?, latest_receipt_json = ?
                    WHERE job_id = ? AND state = ? AND latest_sequence = ?
                    """,
                    (
                        target_state.value,
                        receipt.sequence,
                        receipt.receipt_sha256,
                        raw,
                        job_id,
                        stored.state.value,
                        stored.latest_receipt.sequence,
                    ),
                )
                if result.rowcount != 1:
                    raise ToolJobLifecycleConflictError(
                        "ToolJob 生命周期被并发修改。"
                    )
                await db.commit()
                return ToolJobTransitionResult(
                    StoredToolJob(stored.contract, target_state, receipt),
                    True,
                )
        except ToolJobLifecycleConflictError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ToolJobError("无法更新 ToolJob 生命周期。") from exc

    async def list_recovery_required(self) -> tuple[StoredToolJob, ...]:
        """Return dispatched/running jobs that must never be retried blindly."""
        if not _regular_file_exists(self._db_path):
            return ()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    "SELECT * FROM tool_jobs WHERE state IN (?, ?) "
                    "ORDER BY admitted_at, job_id",
                    (ToolJobState.DISPATCHED.value, ToolJobState.RUNNING.value),
                )
                stored_jobs: list[StoredToolJob] = []
                for row in await cursor.fetchall():
                    stored = _stored_job_from_row(row)
                    await _validate_event_chain(db, stored)
                    stored_jobs.append(stored)
                return tuple(stored_jobs)
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ToolJobError("无法读取待恢复 ToolJob。") from exc

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
                            raise ToolJobError("ToolJob 是未知的未版本化数据库。")
                        for statement in _SCHEMA_V2:
                            await db.execute(statement)
                        await db.execute(
                            f"PRAGMA user_version = {TOOL_JOB_SCHEMA_VERSION}"
                        )
                    elif version == 1:
                        await _migrate_v1_to_v2(db)
                    elif version != TOOL_JOB_SCHEMA_VERSION:
                        raise ToolJobError(
                            f"ToolJob schema v{version} 不受支持；"
                            f"当前仅支持 v{TOOL_JOB_SCHEMA_VERSION}。"
                        )
                    await db.commit()
                if not existed and os.name != "nt":
                    self._db_path.chmod(0o600)
                self._schema_ready = True
            except ToolJobError:
                raise
            except (aiosqlite.Error, OSError, ValueError) as exc:
                raise ToolJobError("无法初始化 ToolJob Store。") from exc

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


class ToolJobAuthority:
    """Admit immutable jobs only after every current execution authority agrees."""

    def __init__(
        self,
        *,
        store: ToolJobStore,
        execution_grants: ExecutionGrantAuthority,
        worker_registry: WorkerRegistryStore,
    ) -> None:
        self._store = store
        self._execution_grants = execution_grants
        self._worker_registry = worker_registry

    async def admit(
        self,
        request: ToolJobRequest,
        *,
        worker_health: WorkerHealthReport,
        requirements: WorkerAdmissionRequirements,
        now: str,
    ) -> StoredToolJob:
        _validate_request(request)
        _validate_requirements(requirements)
        admitted_at = _canonical_time(now, field="now")
        grant_request = request.execution_grant_request()
        validation = await self._execution_grants.validate(
            grant_id=request.execution_grant_id,
            request=grant_request,
            now=admitted_at,
        )
        if not validation.allowed or validation.contract is None:
            reasons = ",".join(reason.value for reason in validation.reasons)
            raise ToolJobConflictError(f"Execution grant 无效：{reasons}")
        grant = validation.contract
        worker_admission = await self._worker_registry.assess_admission(
            worker_id=request.worker_id,
            report=worker_health,
            requirements=requirements,
            now=admitted_at,
        )
        if not worker_admission.admitted:
            reasons = ",".join(reason.value for reason in worker_admission.reasons)
            raise ToolJobConflictError(f"Worker admission 被拒绝：{reasons}")
        contract = _issue_job_contract(
            request=request,
            grant=grant,
            requirements=requirements,
            admitted_at=admitted_at,
        )
        return await self._store.admit(contract)

    async def validate_for_dispatch(
        self,
        *,
        job_id: str,
        request: ToolJobRequest,
        worker_health: WorkerHealthReport,
        requirements: WorkerAdmissionRequirements,
        now: str,
    ) -> ToolJobValidation:
        checked_at = _canonical_time(now, field="now")
        stored = await self._store.get(job_id)
        if stored is None:
            return _validation(
                checked_at,
                None,
                None,
                ToolJobValidationReason.MISSING,
            )
        contract = stored.contract
        reasons: list[ToolJobValidationReason] = []
        if datetime.fromisoformat(contract.expires_at) <= datetime.fromisoformat(
            checked_at
        ):
            reasons.append(ToolJobValidationReason.EXPIRED)
        if not _request_matches(contract, request):
            reasons.append(ToolJobValidationReason.REQUEST_MISMATCH)
        try:
            requirements_sha256 = tool_job_requirements_sha256(requirements)
        except (TypeError, ValueError):
            requirements_sha256 = ""
        if contract.requirements_sha256 != requirements_sha256:
            reasons.append(ToolJobValidationReason.REQUIREMENTS_MISMATCH)
        grant_validation = await self._execution_grants.validate(
            grant_id=request.execution_grant_id,
            request=request.execution_grant_request(),
            now=checked_at,
        )
        if (
            not grant_validation.allowed
            or grant_validation.contract is None
            or grant_validation.contract.grant_sha256
            != contract.execution_grant_sha256
        ):
            reasons.append(ToolJobValidationReason.EXECUTION_GRANT_INVALID)
        worker_admission: WorkerAdmissionResult | None = None
        if requirements_sha256:
            worker_admission = await self._worker_registry.assess_admission(
                worker_id=request.worker_id,
                report=worker_health,
                requirements=requirements,
                now=checked_at,
            )
        if worker_admission is None or not worker_admission.admitted:
            reasons.append(ToolJobValidationReason.WORKER_NOT_ADMITTED)
        unique = tuple(dict.fromkeys(reasons))
        return ToolJobValidation(
            allowed=not unique,
            reasons=unique or (ToolJobValidationReason.VALID,),
            checked_at=checked_at,
            contract=contract,
            worker_admission=worker_admission,
        )

    async def dispatch(
        self,
        *,
        job_id: str,
        request: ToolJobRequest,
        worker_health: WorkerHealthReport,
        requirements: WorkerAdmissionRequirements,
        dispatch_id: str,
        now: str,
    ) -> ToolJobTransitionResult:
        """Return applied=True only when a producer may cross the transport boundary."""
        validation = await self.validate_for_dispatch(
            job_id=job_id,
            request=request,
            worker_health=worker_health,
            requirements=requirements,
            now=now,
        )
        if not validation.allowed:
            reasons = ",".join(reason.value for reason in validation.reasons)
            raise ToolJobLifecycleConflictError(
                f"ToolJob dispatch authority 被拒绝：{reasons}"
            )
        return await self._store._transition(
            job_id=job_id,
            target_state=ToolJobState.DISPATCHED,
            dispatch_id=dispatch_id,
            side_effect=ToolJobSideEffect.POSSIBLE,
            result_code="dispatch_committed",
            occurred_at=now,
        )


class ToolJobLifecycleAuthority:
    """Fence Worker lifecycle updates against one immutable ToolJob incarnation."""

    def __init__(
        self,
        store: ToolJobStore,
        worker_registry: WorkerRegistryStore,
    ) -> None:
        if not isinstance(store, ToolJobStore):
            raise TypeError("store 必须是 ToolJobStore。")
        if not isinstance(worker_registry, WorkerRegistryStore):
            raise TypeError("worker_registry 必须是 WorkerRegistryStore。")
        self._store = store
        self._worker_registry = worker_registry

    async def mark_running(
        self,
        *,
        job_id: str,
        dispatch_id: str,
        worker_id: str,
        worker_instance_id: str,
        worker_epoch: int,
        now: str,
    ) -> StoredToolJob:
        await self._require_dispatch_identity(
            job_id=job_id,
            dispatch_id=dispatch_id,
            worker_id=worker_id,
            worker_instance_id=worker_instance_id,
            worker_epoch=worker_epoch,
        )
        transition = await self._store._transition(
            job_id=job_id,
            target_state=ToolJobState.RUNNING,
            dispatch_id=dispatch_id,
            side_effect=ToolJobSideEffect.POSSIBLE,
            result_code="worker_started",
            occurred_at=now,
        )
        return transition.job

    async def finish(
        self,
        *,
        job_id: str,
        dispatch_id: str,
        worker_id: str,
        worker_instance_id: str,
        worker_epoch: int,
        state: ToolJobState,
        side_effect: ToolJobSideEffect,
        result_code: str,
        now: str,
        exit_code: int | None = None,
        output_sha256: str | None = None,
        artifact_manifest_sha256: str | None = None,
    ) -> StoredToolJob:
        if state not in {
            ToolJobState.SUCCEEDED,
            ToolJobState.FAILED,
            ToolJobState.CANCELLED,
            ToolJobState.UNKNOWN,
        }:
            raise ValueError("finish state 必须是终态。")
        if side_effect is ToolJobSideEffect.NONE:
            raise ValueError("dispatch 后终态不能声明无副作用。")
        if state is ToolJobState.UNKNOWN and side_effect is not ToolJobSideEffect.POSSIBLE:
            raise ValueError("unknown 终态必须声明 possible 副作用。")
        await self._require_dispatch_identity(
            job_id=job_id,
            dispatch_id=dispatch_id,
            worker_id=worker_id,
            worker_instance_id=worker_instance_id,
            worker_epoch=worker_epoch,
        )
        transition = await self._store._transition(
            job_id=job_id,
            target_state=state,
            dispatch_id=dispatch_id,
            side_effect=side_effect,
            result_code=result_code,
            occurred_at=now,
            exit_code=exit_code,
            output_sha256=output_sha256,
            artifact_manifest_sha256=artifact_manifest_sha256,
        )
        return transition.job

    async def cancel_before_dispatch(
        self,
        *,
        job_id: str,
        reason_code: str,
        now: str,
    ) -> StoredToolJob:
        transition = await self._store._transition(
            job_id=job_id,
            target_state=ToolJobState.CANCELLED,
            dispatch_id=None,
            side_effect=ToolJobSideEffect.NONE,
            result_code=reason_code,
            occurred_at=now,
        )
        return transition.job

    async def mark_recovery_unknown(
        self,
        *,
        job_id: str,
        expected_latest_receipt_sha256: str,
        reason_code: str,
        now: str,
    ) -> StoredToolJob:
        """Fence an ambiguous recovered job without trusting a stale Worker."""
        _require_sha256(
            expected_latest_receipt_sha256,
            field="expected_latest_receipt_sha256",
        )
        transition = await self._store._transition(
            job_id=job_id,
            target_state=ToolJobState.UNKNOWN,
            dispatch_id=(await self._require_job(job_id)).latest_receipt.dispatch_id,
            side_effect=ToolJobSideEffect.POSSIBLE,
            result_code=reason_code,
            occurred_at=now,
            expected_latest_receipt_sha256=expected_latest_receipt_sha256,
        )
        return transition.job

    async def _require_dispatch_identity(
        self,
        *,
        job_id: str,
        dispatch_id: str,
        worker_id: str,
        worker_instance_id: str,
        worker_epoch: int,
    ) -> StoredToolJob:
        stored = await self._require_job(job_id)
        contract = stored.contract
        if (
            contract.worker_id != worker_id
            or contract.worker_instance_id != worker_instance_id
            or contract.worker_epoch != worker_epoch
        ):
            raise ToolJobLifecycleConflictError("Worker incarnation 与 ToolJob 不一致。")
        active = await self._worker_registry.get_active(worker_id)
        if active is None or (
            active.contract.instance_id != worker_instance_id
            or active.contract.epoch != worker_epoch
            or active.contract.contract_sha256 != contract.worker_contract_sha256
        ):
            raise ToolJobLifecycleConflictError(
                "Worker incarnation 已被 Registry fencing。"
            )
        durable_dispatch_id = stored.latest_receipt.dispatch_id
        if durable_dispatch_id is None or durable_dispatch_id != dispatch_id:
            raise ToolJobLifecycleConflictError("dispatch_id 与 ToolJob 不一致。")
        return stored

    async def _require_job(self, job_id: str) -> StoredToolJob:
        stored = await self._store.get(job_id)
        if stored is None:
            raise ToolJobLifecycleConflictError("ToolJob 不存在。")
        return stored


def tool_job_requirements_sha256(
    requirements: WorkerAdmissionRequirements,
) -> str:
    _validate_requirements(requirements)
    return _canonical_sha256(asdict(requirements))


def verify_tool_job(contract: ImmutableToolJob) -> bool:
    return hmac.compare_digest(contract.job_sha256, _job_digest(contract))


def _issue_job_contract(
    *,
    request: ToolJobRequest,
    grant: ExecutionGrantContract,
    requirements: WorkerAdmissionRequirements,
    admitted_at: str,
) -> ImmutableToolJob:
    request_payload = {
        "session_id": request.session_id,
        "run_id": request.run_id,
        "call_id": request.call_id,
        "tool_name": request.tool_name,
        "tool_family": grant.tool_family,
        "arguments_sha256": execution_arguments_sha256(request.arguments),
        "idempotency_key": request.idempotency_key,
        "workspace_sha256": grant.workspace_sha256,
        "execution_grant_id": grant.grant_id,
        "execution_grant_sha256": grant.grant_sha256,
        "authorization_reference": request.authorization_reference,
        "worker_id": grant.worker_id,
        "worker_instance_id": grant.worker_instance_id,
        "worker_epoch": grant.worker_epoch,
        "worker_contract_sha256": grant.worker_contract_sha256,
        "lease_owner_id": grant.lease_owner_id,
        "lease_epoch": grant.lease_epoch,
        "requirements_sha256": tool_job_requirements_sha256(requirements),
    }
    draft = ImmutableToolJob(
        schema_version=1,
        job_id=uuid4().hex,
        admitted_at=admitted_at,
        expires_at=grant.expires_at,
        request_sha256=_canonical_sha256(request_payload),
        job_sha256="0" * 64,
        **request_payload,
    )
    return replace(draft, job_sha256=_job_digest(draft))


def _request_matches(contract: ImmutableToolJob, request: ToolJobRequest) -> bool:
    try:
        _validate_request(request)
    except (TypeError, ValueError):
        return False
    return (
        contract.session_id == request.session_id
        and contract.run_id == request.run_id
        and contract.call_id == request.call_id
        and contract.tool_name == request.tool_name
        and contract.arguments_sha256
        == execution_arguments_sha256(request.arguments)
        and contract.idempotency_key == request.idempotency_key
        and contract.worker_id == request.worker_id
        and contract.authorization_reference == request.authorization_reference
        and contract.execution_grant_id == request.execution_grant_id
    )


def _validate_request(request: ToolJobRequest) -> None:
    if not isinstance(request, ToolJobRequest):
        raise TypeError("request 必须是 ToolJobRequest。")
    for field in (
        "session_id",
        "run_id",
        "call_id",
        "tool_name",
        "idempotency_key",
        "worker_id",
        "authorization_reference",
        "execution_grant_id",
    ):
        _require_identifier(getattr(request, field), field=field)
    execution_arguments_sha256(request.arguments)


def _validate_requirements(requirements: WorkerAdmissionRequirements) -> None:
    if not isinstance(requirements, WorkerAdmissionRequirements):
        raise TypeError("requirements 必须是 WorkerAdmissionRequirements。")
    if requirements.kind is not WorkerKind.TOOL:
        raise ValueError("ToolJob requirements.kind 必须为 tool。")


def verify_tool_job_lifecycle_receipt(receipt: ToolJobLifecycleReceipt) -> bool:
    expected_transition = _lifecycle_transition_digest(
        job_id=receipt.job_id,
        target_state=receipt.state,
        dispatch_id=receipt.dispatch_id,
        side_effect=receipt.side_effect,
        result_code=receipt.result_code,
        exit_code=receipt.exit_code,
        output_sha256=receipt.output_sha256,
        artifact_manifest_sha256=receipt.artifact_manifest_sha256,
    )
    return (
        receipt.receipt_id == f"tjr_{expected_transition[:24]}"
        and hmac.compare_digest(receipt.transition_sha256, expected_transition)
        and hmac.compare_digest(receipt.receipt_sha256, _receipt_digest(receipt))
    )


def _issue_admission_receipt(contract: ImmutableToolJob) -> ToolJobLifecycleReceipt:
    return _build_lifecycle_receipt(
        contract=contract,
        sequence=1,
        previous_state=None,
        state=ToolJobState.ADMITTED,
        dispatch_id=None,
        side_effect=ToolJobSideEffect.NONE,
        result_code="admitted",
        occurred_at=contract.admitted_at,
        previous_receipt_sha256=None,
    )


def _issue_lifecycle_receipt(
    *,
    stored: StoredToolJob,
    target_state: ToolJobState,
    dispatch_id: str | None,
    side_effect: ToolJobSideEffect,
    result_code: str,
    occurred_at: str,
    exit_code: int | None,
    output_sha256: str | None,
    artifact_manifest_sha256: str | None,
) -> ToolJobLifecycleReceipt:
    if datetime.fromisoformat(occurred_at) < datetime.fromisoformat(
        stored.latest_receipt.occurred_at
    ):
        raise ToolJobLifecycleConflictError(
            "ToolJob lifecycle occurred_at 不能早于上一条回执。"
        )
    return _build_lifecycle_receipt(
        contract=stored.contract,
        sequence=stored.latest_receipt.sequence + 1,
        previous_state=stored.state,
        state=target_state,
        dispatch_id=dispatch_id,
        side_effect=side_effect,
        result_code=result_code,
        occurred_at=occurred_at,
        previous_receipt_sha256=stored.latest_receipt.receipt_sha256,
        exit_code=exit_code,
        output_sha256=output_sha256,
        artifact_manifest_sha256=artifact_manifest_sha256,
    )


def _build_lifecycle_receipt(
    *,
    contract: ImmutableToolJob,
    sequence: int,
    previous_state: ToolJobState | None,
    state: ToolJobState,
    dispatch_id: str | None,
    side_effect: ToolJobSideEffect,
    result_code: str,
    occurred_at: str,
    previous_receipt_sha256: str | None,
    exit_code: int | None = None,
    output_sha256: str | None = None,
    artifact_manifest_sha256: str | None = None,
) -> ToolJobLifecycleReceipt:
    transition_sha256 = _lifecycle_transition_digest(
        job_id=contract.job_id,
        target_state=state,
        dispatch_id=dispatch_id,
        side_effect=side_effect,
        result_code=result_code,
        exit_code=exit_code,
        output_sha256=output_sha256,
        artifact_manifest_sha256=artifact_manifest_sha256,
    )
    draft = ToolJobLifecycleReceipt(
        schema_version=1,
        receipt_id=f"tjr_{transition_sha256[:24]}",
        job_id=contract.job_id,
        sequence=sequence,
        previous_state=previous_state,
        state=state,
        worker_id=contract.worker_id,
        worker_instance_id=contract.worker_instance_id,
        worker_epoch=contract.worker_epoch,
        dispatch_id=dispatch_id,
        side_effect=side_effect,
        result_code=result_code,
        exit_code=exit_code,
        output_sha256=output_sha256,
        artifact_manifest_sha256=artifact_manifest_sha256,
        occurred_at=occurred_at,
        previous_receipt_sha256=previous_receipt_sha256,
        transition_sha256=transition_sha256,
        receipt_sha256="0" * 64,
    )
    return replace(draft, receipt_sha256=_receipt_digest(draft))


def _lifecycle_transition_digest(
    *,
    job_id: str,
    target_state: ToolJobState,
    dispatch_id: str | None,
    side_effect: ToolJobSideEffect,
    result_code: str,
    exit_code: int | None,
    output_sha256: str | None,
    artifact_manifest_sha256: str | None,
) -> str:
    return _canonical_sha256(
        {
            "job_id": job_id,
            "state": target_state,
            "dispatch_id": dispatch_id,
            "side_effect": side_effect,
            "result_code": result_code,
            "exit_code": exit_code,
            "output_sha256": output_sha256,
            "artifact_manifest_sha256": artifact_manifest_sha256,
        }
    )


def _validate_lifecycle_receipt_semantics(receipt: ToolJobLifecycleReceipt) -> None:
    if receipt.sequence == 1:
        if (
            receipt.previous_state is not None
            or receipt.state is not ToolJobState.ADMITTED
            or receipt.dispatch_id is not None
            or receipt.side_effect is not ToolJobSideEffect.NONE
            or receipt.previous_receipt_sha256 is not None
        ):
            raise ValueError("ToolJob admission lifecycle receipt 无效。")
        return
    if receipt.previous_state is None or receipt.previous_receipt_sha256 is None:
        raise ValueError("ToolJob lifecycle receipt 缺少前序绑定。")
    try:
        _require_transition(receipt.previous_state, receipt.state)
    except ToolJobLifecycleConflictError as exc:
        raise ValueError("ToolJob lifecycle 状态转换无效。") from exc
    if receipt.state is ToolJobState.CANCELLED and receipt.previous_state is ToolJobState.ADMITTED:
        if receipt.dispatch_id is not None or receipt.side_effect is not ToolJobSideEffect.NONE:
            raise ValueError("dispatch 前取消不得声明副作用。")
        return
    if receipt.dispatch_id is None:
        raise ValueError("dispatch 后生命周期必须绑定 dispatch_id。")
    if receipt.side_effect is ToolJobSideEffect.NONE:
        raise ValueError("dispatch 后不能声明无副作用。")
    if (
        receipt.state is ToolJobState.UNKNOWN
        and receipt.side_effect is not ToolJobSideEffect.POSSIBLE
    ):
        raise ValueError("unknown 终态必须声明 possible 副作用。")
    if receipt.state in {ToolJobState.DISPATCHED, ToolJobState.RUNNING} and (
        receipt.exit_code is not None
        or receipt.output_sha256 is not None
        or receipt.artifact_manifest_sha256 is not None
    ):
        raise ValueError("非终态不得携带结果摘要。")


def _require_transition(previous: ToolJobState, target: ToolJobState) -> None:
    allowed = {
        ToolJobState.ADMITTED: {
            ToolJobState.DISPATCHED,
            ToolJobState.CANCELLED,
        },
        ToolJobState.DISPATCHED: {
            ToolJobState.RUNNING,
            ToolJobState.SUCCEEDED,
            ToolJobState.FAILED,
            ToolJobState.CANCELLED,
            ToolJobState.UNKNOWN,
        },
        ToolJobState.RUNNING: {
            ToolJobState.SUCCEEDED,
            ToolJobState.FAILED,
            ToolJobState.CANCELLED,
            ToolJobState.UNKNOWN,
        },
    }
    if target not in allowed.get(previous, set()):
        raise ToolJobLifecycleConflictError(
            f"ToolJob 生命周期不允许 {previous.value} -> {target.value}。"
        )


def _validation(
    checked_at: str,
    contract: ImmutableToolJob | None,
    worker_admission: WorkerAdmissionResult | None,
    *reasons: ToolJobValidationReason,
) -> ToolJobValidation:
    return ToolJobValidation(
        False,
        tuple(reasons),
        checked_at,
        contract,
        worker_admission,
    )


def _job_digest(contract: ImmutableToolJob) -> str:
    payload = asdict(contract)
    payload.pop("job_sha256")
    return _canonical_sha256(payload)


def _receipt_digest(receipt: ToolJobLifecycleReceipt) -> str:
    payload = asdict(receipt)
    payload.pop("receipt_sha256")
    return _canonical_sha256(payload)


def _serialize_contract(contract: ImmutableToolJob) -> str:
    encoded = json.dumps(
        _json_value(asdict(contract)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise ValueError("ToolJob contract 超过持久化上限。")
    return encoded


def _serialize_lifecycle_receipt(receipt: ToolJobLifecycleReceipt) -> str:
    if not verify_tool_job_lifecycle_receipt(receipt):
        raise ValueError("ToolJob lifecycle receipt 摘要校验失败。")
    encoded = json.dumps(
        _json_value(asdict(receipt)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise ValueError("ToolJob lifecycle receipt 超过持久化上限。")
    return encoded


def _deserialize_contract(raw: str) -> ImmutableToolJob:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise ValueError("持久化 ToolJob 大小无效。")
    payload = json.loads(raw)
    expected = set(ImmutableToolJob.__dataclass_fields__)
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("持久化 ToolJob 字段集合无效。")
    try:
        contract = ImmutableToolJob(**payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("持久化 ToolJob 内容无效。") from exc
    if not verify_tool_job(contract):
        raise ValueError("持久化 ToolJob 摘要校验失败。")
    return contract


def _deserialize_lifecycle_receipt(raw: str) -> ToolJobLifecycleReceipt:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise ValueError("持久化 ToolJob lifecycle receipt 大小无效。")
    payload = json.loads(raw)
    expected = set(ToolJobLifecycleReceipt.__dataclass_fields__)
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("持久化 ToolJob lifecycle receipt 字段集合无效。")
    try:
        if payload["previous_state"] is not None:
            payload["previous_state"] = ToolJobState(payload["previous_state"])
        payload["state"] = ToolJobState(payload["state"])
        payload["side_effect"] = ToolJobSideEffect(payload["side_effect"])
        receipt = ToolJobLifecycleReceipt(**payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("持久化 ToolJob lifecycle receipt 内容无效。") from exc
    if not verify_tool_job_lifecycle_receipt(receipt):
        raise ValueError("持久化 ToolJob lifecycle receipt 摘要校验失败。")
    return receipt


def _stored_job_from_row(row: aiosqlite.Row) -> StoredToolJob:
    contract = _deserialize_contract(str(row["contract_json"]))
    receipt = _deserialize_lifecycle_receipt(str(row["latest_receipt_json"]))
    if (
        contract.job_id != str(row["job_id"])
        or contract.idempotency_key != str(row["idempotency_key"])
        or contract.request_sha256 != str(row["request_sha256"])
        or contract.job_sha256 != str(row["job_sha256"])
        or contract.admitted_at != str(row["admitted_at"])
        or contract.expires_at != str(row["expires_at"])
        or receipt.job_id != contract.job_id
        or receipt.state.value != str(row["state"])
        or receipt.sequence != int(row["latest_sequence"])
        or receipt.receipt_sha256 != str(row["latest_receipt_sha256"])
    ):
        raise ValueError("ToolJob 索引列与合同不一致。")
    state = ToolJobState(str(row["state"]))
    return StoredToolJob(contract, state, receipt)


async def _validate_event_chain(
    db: aiosqlite.Connection,
    stored: StoredToolJob,
) -> None:
    cursor = await db.execute(
        "SELECT * FROM tool_job_lifecycle_events WHERE job_id = ? "
        "ORDER BY sequence",
        (stored.contract.job_id,),
    )
    rows = await cursor.fetchall()
    if len(rows) != stored.latest_receipt.sequence:
        raise ValueError("ToolJob lifecycle event 数量与 latest sequence 不一致。")
    previous: ToolJobLifecycleReceipt | None = None
    for expected_sequence, row in enumerate(rows, start=1):
        receipt = _deserialize_lifecycle_receipt(str(row["receipt_json"]))
        if (
            receipt.job_id != stored.contract.job_id
            or receipt.sequence != expected_sequence
            or receipt.sequence != int(row["sequence"])
            or receipt.transition_sha256 != str(row["transition_sha256"])
            or receipt.receipt_sha256 != str(row["receipt_sha256"])
            or receipt.occurred_at != str(row["occurred_at"])
            or receipt.state.value != str(row["state"])
        ):
            raise ValueError("ToolJob lifecycle event 索引与回执不一致。")
        if previous is None:
            if (
                receipt.previous_receipt_sha256 is not None
                or receipt.occurred_at != stored.contract.admitted_at
            ):
                raise ValueError("ToolJob lifecycle genesis 前序摘要无效。")
        elif receipt.previous_receipt_sha256 != previous.receipt_sha256:
            raise ValueError("ToolJob lifecycle event 摘要链断裂。")
        if previous is not None and (
            receipt.previous_state is not previous.state
            or datetime.fromisoformat(receipt.occurred_at)
            < datetime.fromisoformat(previous.occurred_at)
        ):
            raise ValueError("ToolJob lifecycle event 前序状态或时间倒退。")
        if (
            receipt.worker_id != stored.contract.worker_id
            or receipt.worker_instance_id != stored.contract.worker_instance_id
            or receipt.worker_epoch != stored.contract.worker_epoch
        ):
            raise ValueError("ToolJob lifecycle event Worker 绑定不一致。")
        previous = receipt
    if previous != stored.latest_receipt:
        raise ValueError("ToolJob latest lifecycle receipt 与事件链不一致。")


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        _json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON 对象键必须是字符串。")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON 数值必须是有限值。")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"不支持的 JSON 值：{type(value).__name__}")


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} 必须是安全标识符。")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} 必须是 SHA-256。")


def _require_positive_int(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} 必须是正整数。")


def _aware_time(value: str, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是 ISO 时间字符串。")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return parsed


def _canonical_time(value: str, *, field: str) -> str:
    return _aware_time(value, field=field).isoformat()


async def _user_tables(db: aiosqlite.Connection) -> tuple[str, ...]:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return tuple(str(row[0]) for row in await cursor.fetchall())


def _regular_file_exists(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ToolJobError("无法检查 ToolJob 路径。") from exc
    if not stat.S_ISREG(mode):
        raise ToolJobError("ToolJob 路径不是文件。")
    return True


async def _migrate_v1_to_v2(db: aiosqlite.Connection) -> None:
    tables = set(await _user_tables(db))
    if tables != {"tool_jobs"}:
        raise ToolJobError("ToolJob schema v1 表集合无效。")
    cursor = await db.execute("SELECT * FROM tool_jobs ORDER BY admitted_at, job_id")
    legacy_rows = await cursor.fetchall()
    contracts: list[ImmutableToolJob] = []
    for row in legacy_rows:
        if str(row["state"]) != ToolJobState.ADMITTED.value:
            raise ToolJobError("ToolJob schema v1 包含未知状态。")
        contract = _deserialize_contract(str(row["contract_json"]))
        if (
            contract.job_id != str(row["job_id"])
            or contract.idempotency_key != str(row["idempotency_key"])
            or contract.request_sha256 != str(row["request_sha256"])
            or contract.job_sha256 != str(row["job_sha256"])
            or contract.admitted_at != str(row["admitted_at"])
            or contract.expires_at != str(row["expires_at"])
        ):
            raise ToolJobError("ToolJob schema v1 索引列与合同不一致。")
        contracts.append(contract)
    await db.execute("DROP INDEX IF EXISTS tool_jobs_expiry")
    await db.execute("ALTER TABLE tool_jobs RENAME TO tool_jobs_v1")
    for statement in _SCHEMA_V2:
        await db.execute(statement)
    for contract in contracts:
        receipt = _issue_admission_receipt(contract)
        receipt_raw = _serialize_lifecycle_receipt(receipt)
        await db.execute(
            """
            INSERT INTO tool_jobs (
                job_id, idempotency_key, request_sha256, job_sha256,
                admitted_at, expires_at, state, latest_sequence,
                latest_receipt_sha256, latest_receipt_json, contract_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract.job_id,
                contract.idempotency_key,
                contract.request_sha256,
                contract.job_sha256,
                contract.admitted_at,
                contract.expires_at,
                ToolJobState.ADMITTED.value,
                receipt.sequence,
                receipt.receipt_sha256,
                receipt_raw,
                _serialize_contract(contract),
            ),
        )
        await db.execute(
            """
            INSERT INTO tool_job_lifecycle_events (
                job_id, sequence, transition_sha256, receipt_sha256,
                occurred_at, state, receipt_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract.job_id,
                receipt.sequence,
                receipt.transition_sha256,
                receipt.receipt_sha256,
                receipt.occurred_at,
                receipt.state.value,
                receipt_raw,
            ),
        )
    await db.execute("DROP TABLE tool_jobs_v1")
    await db.execute(f"PRAGMA user_version = {TOOL_JOB_SCHEMA_VERSION}")


_SCHEMA_V2 = (
    """
    CREATE TABLE tool_jobs (
        job_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        job_sha256 TEXT NOT NULL,
        admitted_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN (
                'admitted', 'dispatched', 'running', 'succeeded',
                'failed', 'cancelled', 'unknown'
            )
        ),
        latest_sequence INTEGER NOT NULL CHECK (latest_sequence >= 1),
        latest_receipt_sha256 TEXT NOT NULL,
        latest_receipt_json TEXT NOT NULL,
        contract_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX tool_jobs_expiry ON tool_jobs (expires_at, job_id)",
    """
    CREATE TABLE tool_job_lifecycle_events (
        job_id TEXT NOT NULL REFERENCES tool_jobs(job_id) ON DELETE RESTRICT,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        transition_sha256 TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL UNIQUE,
        occurred_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN (
                'admitted', 'dispatched', 'running', 'succeeded',
                'failed', 'cancelled', 'unknown'
            )
        ),
        receipt_json TEXT NOT NULL,
        PRIMARY KEY (job_id, sequence),
        UNIQUE (job_id, transition_sha256)
    )
    """,
    """
    CREATE INDEX tool_job_lifecycle_recovery
    ON tool_job_lifecycle_events (state, occurred_at, job_id)
    """,
)


__all__ = [
    "TOOL_JOB_SCHEMA_VERSION",
    "ImmutableToolJob",
    "StoredToolJob",
    "TERMINAL_TOOL_JOB_STATES",
    "ToolJobAuthority",
    "ToolJobConflictError",
    "ToolJobError",
    "ToolJobLifecycleAuthority",
    "ToolJobLifecycleConflictError",
    "ToolJobLifecycleReceipt",
    "ToolJobRequest",
    "ToolJobSideEffect",
    "ToolJobState",
    "ToolJobStore",
    "ToolJobTransitionResult",
    "ToolJobValidation",
    "ToolJobValidationReason",
    "tool_job_requirements_sha256",
    "verify_tool_job",
    "verify_tool_job_lifecycle_receipt",
]
