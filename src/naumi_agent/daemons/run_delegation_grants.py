"""Durable, revocable authority for bounded long-running tool delegation."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import aiosqlite

from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLeaseState
from naumi_agent.harness.store import HarnessStore

RUN_DELEGATION_GRANT_SCHEMA_VERSION = 1
_MAX_CONTRACT_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RunDelegationGrantState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class RunDelegationValidationReason(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    REVOKED = "revoked"
    EXPIRED = "expired"
    CLOCK_REGRESSION = "clock_regression"
    PARENT_MISSING = "parent_missing"
    PARENT_MISMATCH = "parent_mismatch"
    LEASE_MISSING = "lease_missing"
    LEASE_RELEASED = "lease_released"
    LEASE_EXPIRED = "lease_expired"
    LEASE_MISMATCH = "lease_mismatch"
    WORKSPACE_MISMATCH = "workspace_mismatch"


class RunDelegationGrantError(RuntimeError):
    """Raised when durable delegation authority cannot be trusted."""


class RunDelegationGrantConflictError(RunDelegationGrantError):
    """Raised when issuance would conflict with an existing authority fact."""


@dataclass(frozen=True, slots=True)
class RunDelegationGrantContract:
    schema_version: int
    grant_id: str
    idempotency_key: str
    parent_receipt_id: str
    parent_receipt_sha256: str
    session_id: str
    run_id: str
    workspace_sha256: str
    run_kind: HarnessRunKind
    lease_owner_id: str
    lease_epoch: int
    delegated_tool_names: tuple[str, ...]
    issued_at: str
    expires_at: str
    request_sha256: str
    grant_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != RUN_DELEGATION_GRANT_SCHEMA_VERSION:
            raise ValueError("Run delegation grant schema_version 必须为 1。")
        for field in (
            "grant_id",
            "idempotency_key",
            "parent_receipt_id",
            "session_id",
            "run_id",
            "lease_owner_id",
        ):
            _require_identifier(getattr(self, field), field=field)
        for field in (
            "parent_receipt_sha256",
            "workspace_sha256",
            "request_sha256",
            "grant_sha256",
        ):
            _require_sha256(getattr(self, field), field=field)
        if not isinstance(self.run_kind, HarnessRunKind):
            raise TypeError("run_kind 必须是 HarnessRunKind。")
        if (
            isinstance(self.lease_epoch, bool)
            or not isinstance(self.lease_epoch, int)
            or self.lease_epoch < 1
        ):
            raise ValueError("lease_epoch 必须是正整数。")
        _validate_tool_names(self.delegated_tool_names)
        issued = _aware_time(self.issued_at, field="issued_at")
        expires = _aware_time(self.expires_at, field="expires_at")
        if expires <= issued:
            raise ValueError("Run delegation grant expires_at 必须晚于 issued_at。")


@dataclass(frozen=True, slots=True)
class StoredRunDelegationGrant:
    contract: RunDelegationGrantContract
    state: RunDelegationGrantState
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True, slots=True)
class RunDelegationGrantRequest:
    idempotency_key: str
    parent_receipt_id: str
    run_kind: HarnessRunKind
    lease_owner_id: str
    lease_epoch: int
    delegated_tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunDelegationGrantValidation:
    allowed: bool
    reasons: tuple[RunDelegationValidationReason, ...]
    checked_at: str
    contract: RunDelegationGrantContract | None


class RunDelegationGrantStore:
    """SQLite source of truth for immutable and revocable run grants."""

    def __init__(self, db_path: str | Path) -> None:
        unresolved = Path(db_path).expanduser()
        if not unresolved.is_absolute():
            raise ValueError("Run delegation grant 路径必须是绝对路径。")
        self._db_path = unresolved.resolve(strict=False)
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def issue(
        self, contract: RunDelegationGrantContract
    ) -> StoredRunDelegationGrant:
        if not verify_run_delegation_grant(contract):
            raise ValueError("Run delegation grant 摘要校验失败。")
        encoded = _serialize_contract(contract)
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM run_delegation_grants WHERE idempotency_key = ?",
                    (contract.idempotency_key,),
                )
                row = await cursor.fetchone()
                if row is not None:
                    existing = _stored_from_row(row)
                    if existing.contract.request_sha256 != contract.request_sha256:
                        raise RunDelegationGrantConflictError(
                            "Run delegation grant idempotency key 已绑定其他请求。"
                        )
                    await db.commit()
                    return existing
                await db.execute(
                    """
                    INSERT INTO run_delegation_grants (
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
                return _stored_from_row(row)
        except RunDelegationGrantConflictError:
            raise
        except aiosqlite.IntegrityError as exc:
            raise RunDelegationGrantConflictError(
                "Run delegation grant 与现有记录冲突。"
            ) from exc
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise RunDelegationGrantError(
                "无法持久化 Run delegation grant。"
            ) from exc

    async def get(self, grant_id: str) -> StoredRunDelegationGrant | None:
        _require_identifier(grant_id, field="grant_id")
        if not _regular_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                row = await _select_grant(db, grant_id)
                return _stored_from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise RunDelegationGrantError("无法读取 Run delegation grant。") from exc

    async def revoke(
        self, *, grant_id: str, reason: str, revoked_at: str
    ) -> StoredRunDelegationGrant:
        _require_identifier(grant_id, field="grant_id")
        _require_identifier(reason, field="reason")
        timestamp = _canonical_time(revoked_at, field="revoked_at")
        if not _regular_file_exists(self._db_path):
            raise RunDelegationGrantConflictError("Run delegation grant 不存在。")
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _select_grant(db, grant_id)
                if row is None:
                    raise RunDelegationGrantConflictError(
                        "Run delegation grant 不存在。"
                    )
                stored = _stored_from_row(row)
                if stored.state is RunDelegationGrantState.REVOKED:
                    if (
                        stored.revoked_at == timestamp
                        and stored.revoke_reason == reason
                    ):
                        await db.commit()
                        return stored
                    raise RunDelegationGrantConflictError(
                        "Run delegation grant 已按其他事实撤销。"
                    )
                if datetime.fromisoformat(timestamp) < datetime.fromisoformat(
                    stored.contract.issued_at
                ):
                    raise RunDelegationGrantConflictError(
                        "Run delegation grant 撤销时间早于签发时间。"
                    )
                await db.execute(
                    """
                    UPDATE run_delegation_grants
                    SET state = 'revoked', revoked_at = ?, revoke_reason = ?
                    WHERE grant_id = ? AND state = 'active'
                    """,
                    (timestamp, reason, grant_id),
                )
                row = await _select_grant(db, grant_id)
                await db.commit()
                assert row is not None
                return _stored_from_row(row)
        except RunDelegationGrantConflictError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise RunDelegationGrantError("无法撤销 Run delegation grant。") from exc

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
                            raise RunDelegationGrantError(
                                "Run delegation grant 是未知的未版本化数据库。"
                            )
                        for statement in _SCHEMA_V1:
                            await db.execute(statement)
                        await db.execute(
                            f"PRAGMA user_version = {RUN_DELEGATION_GRANT_SCHEMA_VERSION}"
                        )
                    elif version != RUN_DELEGATION_GRANT_SCHEMA_VERSION:
                        raise RunDelegationGrantError(
                            f"Run delegation grant schema v{version} 不受支持。"
                        )
                    await db.commit()
                if not existed and os.name != "nt":
                    self._db_path.chmod(0o600)
                self._schema_ready = True
            except RunDelegationGrantError:
                raise
            except (aiosqlite.Error, OSError, ValueError) as exc:
                raise RunDelegationGrantError(
                    "无法初始化 Run delegation grant Store。"
                ) from exc

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


class RunDelegationGrantAuthority:
    """Issue and revalidate a run grant against parent and lease authorities."""

    def __init__(
        self,
        *,
        store: RunDelegationGrantStore,
        permission_store: PermissionDecisionReceiptStore,
        harness_store: HarnessStore,
        workspace_root: str | Path,
    ) -> None:
        self._store = store
        self._permission_store = permission_store
        self._harness_store = harness_store
        self._workspace_root = Path(workspace_root).expanduser().resolve(strict=False)

    async def issue(
        self,
        request: RunDelegationGrantRequest,
        *,
        now: str,
        ttl_seconds: int,
    ) -> StoredRunDelegationGrant:
        _validate_request(request)
        issued_at = _canonical_time(now, field="now")
        issued = datetime.fromisoformat(issued_at)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise TypeError("ttl_seconds 必须是整数。")
        if not 1 <= ttl_seconds <= 3_600:
            raise ValueError("ttl_seconds 必须在 1 到 3600 之间。")
        parent = await self._permission_store.get(request.parent_receipt_id)
        if parent is None or not parent.authorizes_execution:
            raise RunDelegationGrantConflictError("父权限回执不存在或不允许执行。")
        if parent.source is PermissionDecisionSource.DELEGATED:
            raise RunDelegationGrantConflictError("子权限回执不得创建运行委托。")
        if parent.source not in {
            PermissionDecisionSource.POLICY,
            PermissionDecisionSource.BYPASS,
            PermissionDecisionSource.USER_CONFIRMATION,
        }:
            raise RunDelegationGrantConflictError("当前父权限来源不支持运行委托。")
        if not parent.run_id:
            raise RunDelegationGrantConflictError("父权限回执未绑定 run_id。")
        parent_time = datetime.fromisoformat(parent.decided_at)
        if issued < parent_time or issued - parent_time > timedelta(seconds=300):
            raise RunDelegationGrantConflictError("父权限回执已过期或来自未来。")
        if not set(request.delegated_tool_names).issubset(parent.delegated_tool_names):
            raise RunDelegationGrantConflictError("运行委托超出父权限工具范围。")
        lease = await self._harness_store.get_run_lease(
            workspace_root=self._workspace_root,
            run_kind=request.run_kind,
            run_id=parent.run_id,
        )
        if lease is None:
            raise RunDelegationGrantConflictError("运行委托缺少 Run lease。")
        if lease.state is not HarnessRunLeaseState.ACTIVE:
            raise RunDelegationGrantConflictError("运行委托的 Run lease 已释放。")
        lease_expiry = datetime.fromisoformat(lease.expires_at)
        if lease_expiry <= issued:
            raise RunDelegationGrantConflictError("运行委托的 Run lease 已过期。")
        if lease.owner_id != request.lease_owner_id or lease.epoch != request.lease_epoch:
            raise RunDelegationGrantConflictError("运行委托的 Run lease fence 不匹配。")
        expires = min(issued + timedelta(seconds=ttl_seconds), lease_expiry)
        workspace_sha256 = _workspace_sha256(self._workspace_root)
        request_payload = {
            "idempotency_key": request.idempotency_key,
            "parent_receipt_id": parent.receipt_id,
            "parent_receipt_sha256": parent.receipt_sha256,
            "session_id": parent.session_id,
            "run_id": parent.run_id,
            "workspace_sha256": workspace_sha256,
            "run_kind": request.run_kind.value,
            "lease_owner_id": request.lease_owner_id,
            "lease_epoch": request.lease_epoch,
            "delegated_tool_names": list(request.delegated_tool_names),
        }
        request_sha256 = _canonical_sha256(request_payload)
        draft = RunDelegationGrantContract(
            schema_version=RUN_DELEGATION_GRANT_SCHEMA_VERSION,
            grant_id=uuid4().hex,
            idempotency_key=request.idempotency_key,
            parent_receipt_id=parent.receipt_id,
            parent_receipt_sha256=parent.receipt_sha256,
            session_id=parent.session_id,
            run_id=parent.run_id,
            workspace_sha256=workspace_sha256,
            run_kind=request.run_kind,
            lease_owner_id=request.lease_owner_id,
            lease_epoch=request.lease_epoch,
            delegated_tool_names=request.delegated_tool_names,
            issued_at=issued_at,
            expires_at=expires.isoformat(),
            request_sha256=request_sha256,
            grant_sha256="0" * 64,
        )
        contract = replace(draft, grant_sha256=_grant_digest(draft))
        return await self._store.issue(contract)

    async def validate(
        self, *, grant_id: str, now: str
    ) -> RunDelegationGrantValidation:
        checked_at = _canonical_time(now, field="now")
        current = datetime.fromisoformat(checked_at)
        stored = await self._store.get(grant_id)
        if stored is None:
            return _validation(
                checked_at, None, RunDelegationValidationReason.MISSING
            )
        contract = stored.contract
        reasons: list[RunDelegationValidationReason] = []
        if stored.state is RunDelegationGrantState.REVOKED:
            reasons.append(RunDelegationValidationReason.REVOKED)
        if datetime.fromisoformat(contract.expires_at) <= current:
            reasons.append(RunDelegationValidationReason.EXPIRED)
        if current < datetime.fromisoformat(contract.issued_at):
            reasons.append(RunDelegationValidationReason.CLOCK_REGRESSION)
        if contract.workspace_sha256 != _workspace_sha256(self._workspace_root):
            reasons.append(RunDelegationValidationReason.WORKSPACE_MISMATCH)
        parent = await self._permission_store.get(contract.parent_receipt_id)
        if parent is None:
            reasons.append(RunDelegationValidationReason.PARENT_MISSING)
        elif (
            not parent.authorizes_execution
            or parent.receipt_sha256 != contract.parent_receipt_sha256
            or parent.session_id != contract.session_id
            or parent.run_id != contract.run_id
            or not set(contract.delegated_tool_names).issubset(
                parent.delegated_tool_names
            )
        ):
            reasons.append(RunDelegationValidationReason.PARENT_MISMATCH)
        lease = await self._harness_store.get_run_lease(
            workspace_root=self._workspace_root,
            run_kind=contract.run_kind,
            run_id=contract.run_id,
        )
        if lease is None:
            reasons.append(RunDelegationValidationReason.LEASE_MISSING)
        else:
            if lease.state is not HarnessRunLeaseState.ACTIVE:
                reasons.append(RunDelegationValidationReason.LEASE_RELEASED)
            if datetime.fromisoformat(lease.expires_at) <= current:
                reasons.append(RunDelegationValidationReason.LEASE_EXPIRED)
            if (
                lease.owner_id != contract.lease_owner_id
                or lease.epoch != contract.lease_epoch
            ):
                reasons.append(RunDelegationValidationReason.LEASE_MISMATCH)
        unique = tuple(dict.fromkeys(reasons))
        return RunDelegationGrantValidation(
            allowed=not unique,
            reasons=unique or (RunDelegationValidationReason.VALID,),
            checked_at=checked_at,
            contract=contract,
        )


def verify_run_delegation_grant(contract: RunDelegationGrantContract) -> bool:
    return hmac.compare_digest(contract.grant_sha256, _grant_digest(contract))


def _validate_request(request: RunDelegationGrantRequest) -> None:
    if not isinstance(request, RunDelegationGrantRequest):
        raise TypeError("request 必须是 RunDelegationGrantRequest。")
    for field in ("idempotency_key", "parent_receipt_id", "lease_owner_id"):
        _require_identifier(getattr(request, field), field=field)
    if not isinstance(request.run_kind, HarnessRunKind):
        raise TypeError("run_kind 必须是 HarnessRunKind。")
    if (
        isinstance(request.lease_epoch, bool)
        or not isinstance(request.lease_epoch, int)
        or request.lease_epoch < 1
    ):
        raise ValueError("lease_epoch 必须是正整数。")
    _validate_tool_names(request.delegated_tool_names)


def _validate_tool_names(value: tuple[str, ...]) -> None:
    if not isinstance(value, tuple):
        raise TypeError("delegated_tool_names 必须是 tuple。")
    if not value:
        raise ValueError("delegated_tool_names 不得为空。")
    if tuple(sorted(set(value))) != value or len(value) > 16:
        raise ValueError("delegated_tool_names 必须唯一、排序且不超过 16 项。")
    for tool_name in value:
        _require_identifier(tool_name, field="delegated_tool_names")


def _validation(
    checked_at: str,
    contract: RunDelegationGrantContract | None,
    *reasons: RunDelegationValidationReason,
) -> RunDelegationGrantValidation:
    return RunDelegationGrantValidation(False, tuple(reasons), checked_at, contract)


def _grant_digest(contract: RunDelegationGrantContract) -> str:
    payload = asdict(contract)
    payload["run_kind"] = contract.run_kind.value
    payload["delegated_tool_names"] = list(contract.delegated_tool_names)
    payload["grant_sha256"] = ""
    return _canonical_sha256(payload)


def _serialize_contract(contract: RunDelegationGrantContract) -> str:
    payload = asdict(contract)
    payload["run_kind"] = contract.run_kind.value
    payload["delegated_tool_names"] = list(contract.delegated_tool_names)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise ValueError("Run delegation grant contract 超过大小上限。")
    return encoded


def _deserialize_contract(raw: str) -> RunDelegationGrantContract:
    if len(raw.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise ValueError("Run delegation grant contract 超过大小上限。")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "grant_id",
        "idempotency_key",
        "parent_receipt_id",
        "parent_receipt_sha256",
        "session_id",
        "run_id",
        "workspace_sha256",
        "run_kind",
        "lease_owner_id",
        "lease_epoch",
        "delegated_tool_names",
        "issued_at",
        "expires_at",
        "request_sha256",
        "grant_sha256",
    }:
        raise ValueError("Run delegation grant contract 字段不匹配。")
    tools = payload["delegated_tool_names"]
    if not isinstance(tools, list):
        raise ValueError("delegated_tool_names 格式无效。")
    payload["delegated_tool_names"] = tuple(tools)
    payload["run_kind"] = HarnessRunKind(payload["run_kind"])
    contract = RunDelegationGrantContract(**payload)
    if not verify_run_delegation_grant(contract):
        raise ValueError("Run delegation grant 摘要校验失败。")
    return contract


def _stored_from_row(row: aiosqlite.Row) -> StoredRunDelegationGrant:
    contract = _deserialize_contract(str(row["contract_json"]))
    if (
        contract.grant_id != str(row["grant_id"])
        or contract.request_sha256 != str(row["request_sha256"])
        or contract.grant_sha256 != str(row["grant_sha256"])
        or contract.issued_at != str(row["issued_at"])
        or contract.expires_at != str(row["expires_at"])
    ):
        raise ValueError("Run delegation grant 索引与合同不一致。")
    state = RunDelegationGrantState(str(row["state"]))
    revoked_at = str(row["revoked_at"]) if row["revoked_at"] is not None else None
    revoke_reason = (
        str(row["revoke_reason"]) if row["revoke_reason"] is not None else None
    )
    if state is RunDelegationGrantState.ACTIVE and (
        revoked_at is not None or revoke_reason is not None
    ):
        raise ValueError("Active Run delegation grant 不得包含撤销事实。")
    if state is RunDelegationGrantState.REVOKED and (
        revoked_at is None or revoke_reason is None
    ):
        raise ValueError("Revoked Run delegation grant 缺少撤销事实。")
    if revoked_at is not None:
        revoked = _aware_time(revoked_at, field="revoked_at")
        if revoked < _aware_time(contract.issued_at, field="issued_at"):
            raise ValueError("Run delegation grant 撤销时间早于签发时间。")
    return StoredRunDelegationGrant(
        contract=contract,
        state=state,
        revoked_at=revoked_at,
        revoke_reason=revoke_reason,
    )


async def _select_grant(
    db: aiosqlite.Connection, grant_id: str
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM run_delegation_grants WHERE grant_id = ?", (grant_id,)
    )
    return await cursor.fetchone()


async def _user_tables(db: aiosqlite.Connection) -> tuple[str, ...]:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return tuple(str(row[0]) for row in await cursor.fetchall())


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _workspace_sha256(workspace_root: Path) -> str:
    return hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()


def _canonical_time(value: str, *, field: str) -> str:
    return _aware_time(value, field=field).isoformat()


def _aware_time(value: str, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是 ISO-8601 时间。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 ISO-8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return parsed


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} 必须是安全标识符。")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} 必须是 SHA-256。")


def _regular_file_exists(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise RunDelegationGrantError("Run delegation grant 路径不是普通文件。")
    return True


_SCHEMA_V1 = (
    """
    CREATE TABLE run_delegation_grants (
        grant_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL,
        grant_sha256 TEXT NOT NULL,
        contract_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        revoke_reason TEXT
    )
    """,
    """
    CREATE INDEX idx_run_delegation_grants_state_expiry
    ON run_delegation_grants (state, expires_at, grant_id)
    """,
)


__all__ = [
    "RUN_DELEGATION_GRANT_SCHEMA_VERSION",
    "RunDelegationGrantAuthority",
    "RunDelegationGrantConflictError",
    "RunDelegationGrantContract",
    "RunDelegationGrantError",
    "RunDelegationGrantRequest",
    "RunDelegationGrantState",
    "RunDelegationGrantStore",
    "RunDelegationGrantValidation",
    "RunDelegationValidationReason",
    "StoredRunDelegationGrant",
    "verify_run_delegation_grant",
]
