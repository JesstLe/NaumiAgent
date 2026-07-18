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

TOOL_JOB_SCHEMA_VERSION = 1
_MAX_CONTRACT_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ToolJobState(StrEnum):
    ADMITTED = "admitted"


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
                        admitted_at, expires_at, state, contract_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contract.job_id,
                        contract.idempotency_key,
                        contract.request_sha256,
                        contract.job_sha256,
                        contract.admitted_at,
                        contract.expires_at,
                        ToolJobState.ADMITTED.value,
                        raw,
                    ),
                )
                await db.commit()
                return StoredToolJob(contract, ToolJobState.ADMITTED)
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
                return _stored_job_from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ToolJobError("无法读取 ToolJob。") from exc

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
                        for statement in _SCHEMA_V1:
                            await db.execute(statement)
                        await db.execute(
                            f"PRAGMA user_version = {TOOL_JOB_SCHEMA_VERSION}"
                        )
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


def _stored_job_from_row(row: aiosqlite.Row) -> StoredToolJob:
    contract = _deserialize_contract(str(row["contract_json"]))
    if (
        contract.job_id != str(row["job_id"])
        or contract.idempotency_key != str(row["idempotency_key"])
        or contract.request_sha256 != str(row["request_sha256"])
        or contract.job_sha256 != str(row["job_sha256"])
        or contract.admitted_at != str(row["admitted_at"])
        or contract.expires_at != str(row["expires_at"])
    ):
        raise ValueError("ToolJob 索引列与合同不一致。")
    state = ToolJobState(str(row["state"]))
    return StoredToolJob(contract, state)


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


_SCHEMA_V1 = (
    """
    CREATE TABLE tool_jobs (
        job_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        job_sha256 TEXT NOT NULL,
        admitted_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state = 'admitted'),
        contract_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX tool_jobs_expiry ON tool_jobs (expires_at, job_id)",
)


__all__ = [
    "TOOL_JOB_SCHEMA_VERSION",
    "ImmutableToolJob",
    "StoredToolJob",
    "ToolJobAuthority",
    "ToolJobConflictError",
    "ToolJobError",
    "ToolJobRequest",
    "ToolJobState",
    "ToolJobStore",
    "ToolJobValidation",
    "ToolJobValidationReason",
    "tool_job_requirements_sha256",
    "verify_tool_job",
]
