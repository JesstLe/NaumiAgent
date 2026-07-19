"""Runtime authority for durable, execution-scoped isolated-worker grants."""

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
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionOutcome,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
    permission_arguments_sha256,
)
from naumi_agent.daemons.worker_contract import WorkerKind
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.permissions import (
    PermissionDecision,
    PermissionMode,
    PermissionOutcome,
)

EXECUTION_GRANT_SCHEMA_VERSION = 1
_MAX_CONTRACT_BYTES = 64 * 1024
_MAX_ARGUMENT_BYTES = 256 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExecutionGrantSource(StrEnum):
    POLICY = "policy"
    USER_CONFIRMATION = "user_confirmation"
    SESSION_GRANT = "session_grant"
    BYPASS = "bypass"


class ExecutionGrantState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ExecutionGrantValidationReason(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    REVOKED = "revoked"
    EXPIRED = "expired"
    REQUEST_MISMATCH = "request_mismatch"
    WORKER_FENCED = "worker_fenced"
    LEASE_MISSING = "lease_missing"
    LEASE_RELEASED = "lease_released"
    LEASE_EXPIRED = "lease_expired"
    LEASE_MISMATCH = "lease_mismatch"


class ExecutionGrantError(RuntimeError):
    """Raised when a grant authority cannot provide trustworthy state."""


class ExecutionGrantConflictError(ExecutionGrantError):
    """Raised when one idempotency key is reused for different authority facts."""


@dataclass(frozen=True, slots=True)
class ExecutionGrantContract:
    schema_version: int
    grant_id: str
    session_id: str
    run_id: str
    call_id: str
    tool_name: str
    tool_family: str
    arguments_sha256: str
    idempotency_key: str
    workspace_sha256: str
    lease_owner_id: str
    lease_epoch: int
    worker_id: str
    worker_instance_id: str
    worker_epoch: int
    worker_contract_sha256: str
    permission_mode: PermissionMode
    source: ExecutionGrantSource
    authorization_reference: str
    issued_at: str
    expires_at: str
    request_sha256: str
    grant_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Execution grant schema_version 必须为 1。")
        for field in (
            "grant_id",
            "session_id",
            "run_id",
            "call_id",
            "tool_name",
            "tool_family",
            "idempotency_key",
            "lease_owner_id",
            "worker_id",
            "worker_instance_id",
            "authorization_reference",
        ):
            _require_identifier(getattr(self, field), field=field)
        for field in (
            "arguments_sha256",
            "workspace_sha256",
            "worker_contract_sha256",
            "request_sha256",
            "grant_sha256",
        ):
            _require_sha256(getattr(self, field), field=field)
        _require_positive_int(self.lease_epoch, field="lease_epoch")
        _require_positive_int(self.worker_epoch, field="worker_epoch")
        if not isinstance(self.permission_mode, PermissionMode):
            raise TypeError("permission_mode 必须是 PermissionMode。")
        if not isinstance(self.source, ExecutionGrantSource):
            raise TypeError("source 必须是 ExecutionGrantSource。")
        issued = _aware_time(self.issued_at, field="issued_at")
        expires = _aware_time(self.expires_at, field="expires_at")
        if expires <= issued:
            raise ValueError("Execution grant expires_at 必须晚于 issued_at。")


@dataclass(frozen=True, slots=True)
class StoredExecutionGrant:
    contract: ExecutionGrantContract
    state: ExecutionGrantState
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True, slots=True)
class ExecutionGrantRequest:
    session_id: str
    run_id: str
    call_id: str
    tool_name: str
    arguments: Mapping[str, object]
    idempotency_key: str
    worker_id: str
    authorization_reference: str


@dataclass(frozen=True, slots=True)
class ExecutionGrantValidation:
    allowed: bool
    reasons: tuple[ExecutionGrantValidationReason, ...]
    checked_at: str
    contract: ExecutionGrantContract | None


class ExecutionGrantStore:
    """SQLite source of truth for immutable, revocable execution grants."""

    def __init__(self, db_path: str | Path) -> None:
        unresolved = Path(db_path).expanduser()
        if not unresolved.is_absolute():
            raise ValueError("Execution grant 路径必须是绝对路径。")
        self._db_path = unresolved.resolve(strict=False)
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def issue(self, contract: ExecutionGrantContract) -> StoredExecutionGrant:
        if not verify_execution_grant(contract):
            raise ValueError("Execution grant 摘要校验失败。")
        encoded = _serialize_contract(contract)
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM execution_grants WHERE idempotency_key = ?",
                    (contract.idempotency_key,),
                )
                existing_row = await cursor.fetchone()
                if existing_row is not None:
                    existing = _stored_grant_from_row(existing_row)
                    if existing.contract.request_sha256 != contract.request_sha256:
                        raise ExecutionGrantConflictError(
                            "Execution grant idempotency key 已绑定其他请求。"
                        )
                    await db.commit()
                    return existing
                await db.execute(
                    """
                    INSERT INTO execution_grants (
                        grant_id, idempotency_key, request_sha256, grant_sha256,
                        contract_json, state, issued_at, expires_at,
                        revoked_at, revoke_reason
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL)
                    """,
                    (
                        contract.grant_id,
                        contract.idempotency_key,
                        contract.request_sha256,
                        contract.grant_sha256,
                        encoded,
                        contract.issued_at,
                        contract.expires_at,
                    ),
                )
                row = await _select_grant(db, contract.grant_id)
                await db.commit()
                assert row is not None
                return _stored_grant_from_row(row)
        except ExecutionGrantConflictError:
            raise
        except aiosqlite.IntegrityError as exc:
            raise ExecutionGrantConflictError("Execution grant 与现有记录冲突。") from exc
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ExecutionGrantError("无法持久化 Execution grant。") from exc

    async def get(self, grant_id: str) -> StoredExecutionGrant | None:
        _require_identifier(grant_id, field="grant_id")
        if not _regular_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                row = await _select_grant(db, grant_id)
                return _stored_grant_from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ExecutionGrantError("无法读取 Execution grant。") from exc

    async def revoke(
        self,
        *,
        grant_id: str,
        reason: str,
        revoked_at: str,
    ) -> StoredExecutionGrant:
        _require_identifier(grant_id, field="grant_id")
        _require_identifier(reason, field="reason")
        timestamp = _canonical_time(revoked_at, field="revoked_at")
        if not _regular_file_exists(self._db_path):
            raise ExecutionGrantConflictError("Execution grant 不存在。")
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _select_grant(db, grant_id)
                if row is None:
                    raise ExecutionGrantConflictError("Execution grant 不存在。")
                current = _stored_grant_from_row(row)
                if current.state is ExecutionGrantState.REVOKED:
                    if current.revoke_reason != reason:
                        raise ExecutionGrantConflictError("Execution grant 已由不同原因撤销。")
                    await db.commit()
                    return current
                if datetime.fromisoformat(timestamp) < datetime.fromisoformat(
                    current.contract.issued_at
                ):
                    raise ExecutionGrantConflictError("revoked_at 早于 issued_at。")
                await db.execute(
                    """
                    UPDATE execution_grants
                    SET state = 'revoked', revoked_at = ?, revoke_reason = ?
                    WHERE grant_id = ? AND state = 'active'
                    """,
                    (timestamp, reason, grant_id),
                )
                updated = await _select_grant(db, grant_id)
                await db.commit()
                assert updated is not None
                return _stored_grant_from_row(updated)
        except ExecutionGrantConflictError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ExecutionGrantError("无法撤销 Execution grant。") from exc

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
                            raise ExecutionGrantError(
                                "Execution grant 是未知的未版本化数据库。"
                            )
                        for statement in _SCHEMA_V1:
                            await db.execute(statement)
                        await db.execute(
                            f"PRAGMA user_version = {EXECUTION_GRANT_SCHEMA_VERSION}"
                        )
                    elif version != EXECUTION_GRANT_SCHEMA_VERSION:
                        raise ExecutionGrantError(
                            f"Execution grant schema v{version} 不受支持；"
                            f"当前仅支持 v{EXECUTION_GRANT_SCHEMA_VERSION}。"
                        )
                    await db.commit()
                if not existed and os.name != "nt":
                    self._db_path.chmod(0o600)
                self._schema_ready = True
            except ExecutionGrantError:
                raise
            except (aiosqlite.Error, OSError, ValueError) as exc:
                raise ExecutionGrantError("无法初始化 Execution grant Store。") from exc

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self._db_path, timeout=5.0)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 5000")
            yield db
        finally:
            await db.close()


class ExecutionGrantAuthority:
    """Mint and validate grants against current Worker and Harness authorities."""

    def __init__(
        self,
        *,
        store: ExecutionGrantStore,
        worker_registry: WorkerRegistryStore,
        harness_store: HarnessStore,
        permission_decision_store: PermissionDecisionReceiptStore,
        workspace_root: str | Path,
    ) -> None:
        self._store = store
        self._worker_registry = worker_registry
        self._harness_store = harness_store
        self._permission_decision_store = permission_decision_store
        self._workspace_root = Path(workspace_root).expanduser().resolve(strict=False)

    async def issue(
        self,
        request: ExecutionGrantRequest,
        *,
        decision: PermissionDecision,
        permission_mode: PermissionMode,
        source: ExecutionGrantSource,
        now: str,
        ttl_seconds: int = 120,
    ) -> StoredExecutionGrant:
        _validate_request(request)
        _validate_authorization(decision, permission_mode=permission_mode, source=source)
        issued_at = _canonical_time(now, field="now")
        issued = datetime.fromisoformat(issued_at)
        receipt = await self._permission_decision_store.get(request.authorization_reference)
        if receipt is None:
            raise ExecutionGrantConflictError("权限决定回执不存在。")
        expected_receipt_source = {
            ExecutionGrantSource.USER_CONFIRMATION: (
                PermissionDecisionSource.USER_CONFIRMATION,
                PermissionDecisionOutcome.ALLOW_ONCE,
            ),
            ExecutionGrantSource.SESSION_GRANT: (
                PermissionDecisionSource.SESSION_GRANT,
                PermissionDecisionOutcome.SESSION_GRANTED,
            ),
            ExecutionGrantSource.BYPASS: (
                PermissionDecisionSource.BYPASS,
                PermissionDecisionOutcome.BYPASS_ENABLED,
            ),
            ExecutionGrantSource.POLICY: (
                PermissionDecisionSource.POLICY,
                PermissionDecisionOutcome.POLICY_ALLOWED,
            ),
        }.get(source)
        if expected_receipt_source is None:
            raise ExecutionGrantConflictError("execution grant 来源不受支持。")
        if (
            not receipt.authorizes_execution
            or (receipt.source, receipt.outcome) != expected_receipt_source
            or receipt.session_id != request.session_id
            or receipt.run_id != request.run_id
            or receipt.call_id != request.call_id
            or receipt.tool_name != request.tool_name
            or receipt.permission_mode is not permission_mode
            or receipt.arguments_sha256 != permission_arguments_sha256(request.arguments)
        ):
            raise ExecutionGrantConflictError("权限决定回执与执行请求不匹配。")
        receipt_age = issued - datetime.fromisoformat(receipt.decided_at)
        if receipt_age < timedelta(0) or receipt_age > timedelta(seconds=300):
            raise ExecutionGrantConflictError("权限决定回执已过期或来自未来时间。")
        if not decision.tool_family:
            raise ValueError("Permission decision 缺少 tool_family。")
        tool_family = decision.tool_family or request.tool_name
        _require_identifier(tool_family, field="tool_family")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise TypeError("ttl_seconds 必须是整数。")
        if not 1 <= ttl_seconds <= 300:
            raise ValueError("ttl_seconds 必须在 1 到 300 之间。")
        registration = await self._worker_registry.get_active(request.worker_id)
        if registration is None:
            raise ExecutionGrantConflictError("Worker registration 不存在。")
        worker = registration.contract
        if worker.kind is not WorkerKind.TOOL:
            raise ExecutionGrantConflictError("Execution grant 只能签发给 Tool Worker。")
        lease = await self._harness_store.get_run_lease(
            workspace_root=self._workspace_root,
            run_kind=HarnessRunKind.TOOL,
            run_id=request.run_id,
        )
        if lease is None:
            raise ExecutionGrantConflictError("Tool run lease 不存在。")
        if lease.state is not HarnessRunLeaseState.ACTIVE:
            raise ExecutionGrantConflictError("Tool run lease 已释放。")
        if lease.owner_id != worker.instance_id:
            raise ExecutionGrantConflictError("Tool run lease owner 与 Worker instance 不一致。")
        lease_expiry = datetime.fromisoformat(lease.expires_at)
        if lease_expiry <= issued:
            raise ExecutionGrantConflictError("Tool run lease 已过期。")
        expires = min(issued + timedelta(seconds=ttl_seconds), lease_expiry)
        arguments_sha256 = execution_arguments_sha256(request.arguments)
        workspace_sha256 = hashlib.sha256(
            str(self._workspace_root).encode("utf-8")
        ).hexdigest()
        request_payload = {
            "session_id": request.session_id,
            "run_id": request.run_id,
            "call_id": request.call_id,
            "tool_name": request.tool_name,
            "tool_family": tool_family,
            "arguments_sha256": arguments_sha256,
            "idempotency_key": request.idempotency_key,
            "workspace_sha256": workspace_sha256,
            "lease_owner_id": lease.owner_id,
            "lease_epoch": lease.epoch,
            "worker_id": worker.worker_id,
            "worker_instance_id": worker.instance_id,
            "worker_epoch": worker.epoch,
            "worker_contract_sha256": worker.contract_sha256,
            "permission_mode": permission_mode.value,
            "source": source.value,
            "authorization_reference": request.authorization_reference,
        }
        request_sha256 = _canonical_sha256(request_payload)
        draft = ExecutionGrantContract(
            schema_version=1,
            grant_id=uuid4().hex,
            session_id=request.session_id,
            run_id=request.run_id,
            call_id=request.call_id,
            tool_name=request.tool_name,
            tool_family=tool_family,
            arguments_sha256=arguments_sha256,
            idempotency_key=request.idempotency_key,
            workspace_sha256=workspace_sha256,
            lease_owner_id=lease.owner_id,
            lease_epoch=lease.epoch,
            worker_id=worker.worker_id,
            worker_instance_id=worker.instance_id,
            worker_epoch=worker.epoch,
            worker_contract_sha256=worker.contract_sha256,
            permission_mode=permission_mode,
            source=source,
            authorization_reference=request.authorization_reference,
            issued_at=issued_at,
            expires_at=expires.isoformat(),
            request_sha256=request_sha256,
            grant_sha256="0" * 64,
        )
        contract = replace(draft, grant_sha256=_grant_digest(draft))
        return await self._store.issue(contract)

    async def validate(
        self,
        *,
        grant_id: str,
        request: ExecutionGrantRequest,
        now: str,
    ) -> ExecutionGrantValidation:
        checked_at = _canonical_time(now, field="now")
        stored = await self._store.get(grant_id)
        if stored is None:
            return _validation(checked_at, None, ExecutionGrantValidationReason.MISSING)
        contract = stored.contract
        reasons: list[ExecutionGrantValidationReason] = []
        if stored.state is ExecutionGrantState.REVOKED:
            reasons.append(ExecutionGrantValidationReason.REVOKED)
        if datetime.fromisoformat(contract.expires_at) <= datetime.fromisoformat(checked_at):
            reasons.append(ExecutionGrantValidationReason.EXPIRED)
        if not _request_matches(contract, request, workspace_root=self._workspace_root):
            reasons.append(ExecutionGrantValidationReason.REQUEST_MISMATCH)

        registration = await self._worker_registry.get_active(contract.worker_id)
        if (
            registration is None
            or registration.contract.instance_id != contract.worker_instance_id
            or registration.contract.epoch != contract.worker_epoch
            or registration.contract.contract_sha256 != contract.worker_contract_sha256
        ):
            reasons.append(ExecutionGrantValidationReason.WORKER_FENCED)
        lease = await self._harness_store.get_run_lease(
            workspace_root=self._workspace_root,
            run_kind=HarnessRunKind.TOOL,
            run_id=contract.run_id,
        )
        if lease is None:
            reasons.append(ExecutionGrantValidationReason.LEASE_MISSING)
        else:
            if lease.state is not HarnessRunLeaseState.ACTIVE:
                reasons.append(ExecutionGrantValidationReason.LEASE_RELEASED)
            if datetime.fromisoformat(lease.expires_at) <= datetime.fromisoformat(checked_at):
                reasons.append(ExecutionGrantValidationReason.LEASE_EXPIRED)
            if lease.owner_id != contract.lease_owner_id or lease.epoch != contract.lease_epoch:
                reasons.append(ExecutionGrantValidationReason.LEASE_MISMATCH)
        unique = tuple(dict.fromkeys(reasons))
        return ExecutionGrantValidation(
            allowed=not unique,
            reasons=unique or (ExecutionGrantValidationReason.VALID,),
            checked_at=checked_at,
            contract=contract,
        )


def execution_arguments_sha256(arguments: Mapping[str, object]) -> str:
    if not isinstance(arguments, Mapping) or any(
        not isinstance(key, str) for key in arguments
    ):
        raise TypeError("Execution arguments 必须是字符串键 Mapping。")
    encoded = json.dumps(
        _json_value(dict(arguments)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_ARGUMENT_BYTES:
        raise ValueError("Execution arguments 超过摘要大小上限。")
    return hashlib.sha256(encoded).hexdigest()


def verify_execution_grant(contract: ExecutionGrantContract) -> bool:
    return hmac.compare_digest(contract.grant_sha256, _grant_digest(contract))


def _request_matches(
    contract: ExecutionGrantContract,
    request: ExecutionGrantRequest,
    *,
    workspace_root: Path,
) -> bool:
    try:
        _validate_request(request)
        arguments_sha256 = execution_arguments_sha256(request.arguments)
    except (TypeError, ValueError):
        return False
    workspace_sha256 = hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()
    return (
        contract.session_id == request.session_id
        and contract.run_id == request.run_id
        and contract.call_id == request.call_id
        and contract.tool_name == request.tool_name
        and contract.arguments_sha256 == arguments_sha256
        and contract.idempotency_key == request.idempotency_key
        and contract.worker_id == request.worker_id
        and contract.authorization_reference == request.authorization_reference
        and contract.workspace_sha256 == workspace_sha256
    )


def _validate_authorization(
    decision: PermissionDecision,
    *,
    permission_mode: PermissionMode,
    source: ExecutionGrantSource,
) -> None:
    if not isinstance(decision, PermissionDecision) or not decision.allowed:
        raise ValueError("Execution grant 只能由已允许的 PermissionDecision 签发。")
    if not isinstance(permission_mode, PermissionMode):
        raise TypeError("permission_mode 必须是 PermissionMode。")
    if not isinstance(source, ExecutionGrantSource):
        raise TypeError("source 必须是 ExecutionGrantSource。")
    if source is ExecutionGrantSource.BYPASS:
        if (
            permission_mode is not PermissionMode.BYPASS
            or decision.outcome is not PermissionOutcome.ALLOW
            or decision.requires_confirmation
        ):
            raise ValueError("bypass grant 必须来自 bypass 模式的直接允许决定。")
    elif source is ExecutionGrantSource.POLICY:
        if (
            permission_mode is PermissionMode.BYPASS
            or decision.outcome is not PermissionOutcome.ALLOW
            or decision.requires_confirmation
        ):
            raise ValueError("policy grant 必须来自非 bypass 的直接允许决定。")
    elif source is ExecutionGrantSource.USER_CONFIRMATION:
        if decision.outcome is not PermissionOutcome.CONFIRM or not decision.requires_confirmation:
            raise ValueError("用户确认 grant 必须绑定待确认决定。")
    elif source is ExecutionGrantSource.SESSION_GRANT and (
        decision.outcome is not PermissionOutcome.CONFIRM
        or not decision.requires_confirmation
        or not decision.allow_session_grant
    ):
        raise ValueError("session grant 必须绑定允许会话授权的待确认决定。")


def _validate_request(request: ExecutionGrantRequest) -> None:
    if not isinstance(request, ExecutionGrantRequest):
        raise TypeError("request 必须是 ExecutionGrantRequest。")
    for field in (
        "session_id",
        "run_id",
        "call_id",
        "tool_name",
        "idempotency_key",
        "worker_id",
        "authorization_reference",
    ):
        _require_identifier(getattr(request, field), field=field)
    execution_arguments_sha256(request.arguments)


def _validation(
    checked_at: str,
    contract: ExecutionGrantContract | None,
    *reasons: ExecutionGrantValidationReason,
) -> ExecutionGrantValidation:
    return ExecutionGrantValidation(False, tuple(reasons), checked_at, contract)


def _grant_digest(contract: ExecutionGrantContract) -> str:
    payload = asdict(contract)
    payload.pop("grant_sha256")
    return _canonical_sha256(payload)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        _json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _serialize_contract(contract: ExecutionGrantContract) -> str:
    encoded = json.dumps(
        _json_value(asdict(contract)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise ValueError("Execution grant contract 超过持久化上限。")
    return encoded


def _deserialize_contract(raw: str) -> ExecutionGrantContract:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise ValueError("持久化 Execution grant 大小无效。")
    payload = json.loads(raw)
    expected = {field.name for field in ExecutionGrantContract.__dataclass_fields__.values()}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("持久化 Execution grant 字段集合无效。")
    try:
        contract = ExecutionGrantContract(
            **{
                **payload,
                "permission_mode": PermissionMode(payload["permission_mode"]),
                "source": ExecutionGrantSource(payload["source"]),
            }
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("持久化 Execution grant 内容无效。") from exc
    if not verify_execution_grant(contract):
        raise ValueError("持久化 Execution grant 摘要校验失败。")
    return contract


def _stored_grant_from_row(row: aiosqlite.Row) -> StoredExecutionGrant:
    contract = _deserialize_contract(str(row["contract_json"]))
    if (
        contract.grant_id != str(row["grant_id"])
        or contract.idempotency_key != str(row["idempotency_key"])
        or contract.request_sha256 != str(row["request_sha256"])
        or contract.grant_sha256 != str(row["grant_sha256"])
        or contract.issued_at != str(row["issued_at"])
        or contract.expires_at != str(row["expires_at"])
    ):
        raise ValueError("Execution grant 索引列与合同不一致。")
    state = ExecutionGrantState(str(row["state"]))
    revoked_at = str(row["revoked_at"]) if row["revoked_at"] is not None else None
    reason = str(row["revoke_reason"]) if row["revoke_reason"] is not None else None
    if state is ExecutionGrantState.ACTIVE and (revoked_at is not None or reason is not None):
        raise ValueError("active Execution grant 包含撤销字段。")
    if state is ExecutionGrantState.REVOKED:
        if revoked_at is None or reason is None:
            raise ValueError("revoked Execution grant 缺少撤销字段。")
        _canonical_time(revoked_at, field="revoked_at")
        _require_identifier(reason, field="revoke_reason")
    return StoredExecutionGrant(contract, state, revoked_at, reason)


async def _select_grant(
    db: aiosqlite.Connection,
    grant_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM execution_grants WHERE grant_id = ?",
        (grant_id,),
    )
    return await cursor.fetchone()


async def _user_tables(db: aiosqlite.Connection) -> tuple[str, ...]:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return tuple(str(row[0]) for row in await cursor.fetchall())


def _regular_file_exists(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ExecutionGrantError("无法检查 Execution grant 路径。") from exc
    if not stat.S_ISREG(mode):
        raise ExecutionGrantError("Execution grant 路径不是文件。")
    return True


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Execution grant Mapping 的键必须是字符串。")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Execution grant 不允许非有限浮点数。")
    return value


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} 格式无效。")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} 必须是小写 SHA-256。")


def _require_positive_int(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise ValueError(f"{field} 必须是正整数。")


def _aware_time(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区偏移。")
    return parsed


def _canonical_time(value: str, *, field: str) -> str:
    return _aware_time(value, field=field).isoformat()


_SCHEMA_V1 = (
    """
CREATE TABLE execution_grants (
    grant_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    grant_sha256 TEXT NOT NULL UNIQUE CHECK (length(grant_sha256) = 64),
    contract_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    CHECK (
        (state = 'active' AND revoked_at IS NULL AND revoke_reason IS NULL)
        OR (state = 'revoked' AND revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)
    )
)
""",
    """
CREATE INDEX execution_grants_active_expiry
ON execution_grants (state, expires_at, grant_id)
""",
)


__all__ = [
    "EXECUTION_GRANT_SCHEMA_VERSION",
    "ExecutionGrantAuthority",
    "ExecutionGrantConflictError",
    "ExecutionGrantContract",
    "ExecutionGrantError",
    "ExecutionGrantRequest",
    "ExecutionGrantSource",
    "ExecutionGrantState",
    "ExecutionGrantStore",
    "ExecutionGrantValidation",
    "ExecutionGrantValidationReason",
    "StoredExecutionGrant",
    "execution_arguments_sha256",
    "verify_execution_grant",
]
