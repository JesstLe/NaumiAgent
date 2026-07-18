"""Durable Runtime authority for worker incarnation registration and fencing."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import aiosqlite

from naumi_agent.daemons.worker_contract import (
    WorkerAdmissionDecision,
    WorkerAdmissionReason,
    WorkerAdmissionRequirements,
    WorkerAdmissionResult,
    WorkerCapability,
    WorkerContract,
    WorkerHealthReport,
    WorkerIsolationContract,
    WorkerKind,
    WorkerPlatform,
    WorkerResourceEnvelope,
    assess_worker_admission,
    normalize_worker_timestamp,
    verify_worker_contract,
)

WORKER_REGISTRY_SCHEMA_VERSION = 1
_MAX_CONTRACT_JSON_BYTES = 64 * 1024


class WorkerRegistryStoreError(RuntimeError):
    """Raised when the worker authority cannot provide trustworthy state."""


class WorkerRegistryConflictError(WorkerRegistryStoreError):
    """Raised when a registration violates incarnation fencing."""


class WorkerRegistrationState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class WorkerRegistration:
    contract: WorkerContract
    state: WorkerRegistrationState
    registered_at: str
    terminal_at: str | None
    reason_code: str | None


class WorkerRegistryStore:
    """SQLite-backed source of truth for the active incarnation of each worker."""

    def __init__(self, db_path: str | Path) -> None:
        unresolved = Path(db_path).expanduser()
        if not unresolved.is_absolute():
            raise ValueError("Worker registry 路径必须是绝对路径。")
        path = unresolved.resolve(strict=False)
        self._db_path = path
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def register(
        self,
        contract: WorkerContract,
        *,
        registered_at: str,
    ) -> WorkerRegistration:
        """Register a higher incarnation or idempotently replay the active one."""
        if not isinstance(contract, WorkerContract):
            raise TypeError("contract 必须是 WorkerContract。")
        if not verify_worker_contract(contract):
            raise ValueError("Worker contract 摘要校验失败，拒绝注册。")
        timestamp = normalize_worker_timestamp(registered_at, field="registered_at")
        if datetime.fromisoformat(contract.issued_at) > datetime.fromisoformat(timestamp):
            raise ValueError("Worker contract issued_at 不能晚于 registered_at。")
        contract_json = _serialize_contract(contract)

        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                latest_row = await _select_latest(db, contract.worker_id)
                if latest_row is not None:
                    latest = _registration_from_row(latest_row)
                    if latest.state is WorkerRegistrationState.SUPERSEDED:
                        raise WorkerRegistryStoreError(
                            "Worker registry 历史断裂：最高 epoch 不应为 superseded。"
                        )
                    if latest.contract.contract_sha256 == contract.contract_sha256:
                        if latest.state is not WorkerRegistrationState.ACTIVE:
                            raise WorkerRegistryConflictError(
                                "已终结的 Worker incarnation 不能重新激活。"
                            )
                        await db.commit()
                        return latest
                    _validate_takeover(latest.contract, contract)
                    boundary = latest.terminal_at or latest.registered_at
                    if datetime.fromisoformat(timestamp) < datetime.fromisoformat(boundary):
                        raise WorkerRegistryConflictError("新 Worker registered_at 发生回退。")
                    if latest.state is WorkerRegistrationState.ACTIVE:
                        await db.execute(
                            """
                            UPDATE worker_registrations
                            SET state = ?, terminal_at = ?, reason_code = ?
                            WHERE worker_id = ? AND epoch = ? AND state = ?
                            """,
                            (
                                WorkerRegistrationState.SUPERSEDED.value,
                                timestamp,
                                "higher_epoch_registered",
                                latest.contract.worker_id,
                                latest.contract.epoch,
                                WorkerRegistrationState.ACTIVE.value,
                            ),
                        )
                await db.execute(
                    """
                    INSERT INTO worker_registrations (
                        worker_id, epoch, instance_id, contract_sha256,
                        contract_json, state, registered_at, terminal_at,
                        reason_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        contract.worker_id,
                        contract.epoch,
                        contract.instance_id,
                        contract.contract_sha256,
                        contract_json,
                        WorkerRegistrationState.ACTIVE.value,
                        timestamp,
                    ),
                )
                row = await _select_registration(db, contract.worker_id, contract.epoch)
                await db.commit()
                assert row is not None
                return _registration_from_row(row)
        except WorkerRegistryConflictError:
            raise
        except aiosqlite.IntegrityError as exc:
            raise WorkerRegistryConflictError("Worker 注册与现有 incarnation 冲突。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法持久化 Worker 注册。") from exc

    async def revoke(
        self,
        *,
        worker_id: str,
        instance_id: str,
        epoch: int,
        reason_code: str,
        revoked_at: str,
    ) -> WorkerRegistration:
        """Revoke exactly one active generation; stale callers cannot revoke newer work."""
        _validate_identifier(worker_id, field="worker_id")
        _validate_identifier(instance_id, field="instance_id")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch 必须是正整数。")
        _validate_reason(reason_code)
        timestamp = normalize_worker_timestamp(revoked_at, field="revoked_at")

        await self._ensure_schema()
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _select_registration(db, worker_id, epoch)
                if row is None:
                    raise WorkerRegistryConflictError("Worker incarnation 不存在。")
                registration = _registration_from_row(row)
                if registration.contract.instance_id != instance_id:
                    raise WorkerRegistryConflictError("Worker instance 不匹配。")
                if registration.state is WorkerRegistrationState.REVOKED:
                    if registration.reason_code != reason_code:
                        raise WorkerRegistryConflictError("Worker 已由不同原因撤销。")
                    await db.commit()
                    return registration
                if registration.state is not WorkerRegistrationState.ACTIVE:
                    raise WorkerRegistryConflictError("旧 Worker incarnation 已被 fencing。")
                if datetime.fromisoformat(timestamp) < datetime.fromisoformat(
                    registration.registered_at
                ):
                    raise WorkerRegistryConflictError("revoked_at 早于 registered_at。")
                await db.execute(
                    """
                    UPDATE worker_registrations
                    SET state = ?, terminal_at = ?, reason_code = ?
                    WHERE worker_id = ? AND epoch = ? AND state = ?
                    """,
                    (
                        WorkerRegistrationState.REVOKED.value,
                        timestamp,
                        reason_code,
                        worker_id,
                        epoch,
                        WorkerRegistrationState.ACTIVE.value,
                    ),
                )
                updated = await _select_registration(db, worker_id, epoch)
                await db.commit()
                assert updated is not None
                return _registration_from_row(updated)
        except WorkerRegistryConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法撤销 Worker 注册。") from exc

    async def get_active(self, worker_id: str) -> WorkerRegistration | None:
        """Read the current authority snapshot without accepting caller contract state."""
        _validate_identifier(worker_id, field="worker_id")
        if not _registry_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                row = await _select_active(db, worker_id)
                return _registration_from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法读取当前 Worker 注册。") from exc

    async def list_history(self, worker_id: str) -> tuple[WorkerRegistration, ...]:
        _validate_identifier(worker_id, field="worker_id")
        if not _registry_file_exists(self._db_path):
            return ()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM worker_registrations
                    WHERE worker_id = ? ORDER BY epoch ASC
                    """,
                    (worker_id,),
                )
                return tuple(_registration_from_row(row) for row in await cursor.fetchall())
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise WorkerRegistryStoreError("无法读取 Worker 注册历史。") from exc

    async def assess_admission(
        self,
        *,
        worker_id: str,
        report: WorkerHealthReport,
        requirements: WorkerAdmissionRequirements,
        now: str,
    ) -> WorkerAdmissionResult:
        """Assess only the contract selected by this authority's active pointer."""
        checked_at = normalize_worker_timestamp(now, field="now")
        registration = await self.get_active(worker_id)
        if registration is None:
            return WorkerAdmissionResult(
                decision=WorkerAdmissionDecision.BLOCKED,
                reasons=(WorkerAdmissionReason.REGISTRATION_MISSING,),
                checked_at=checked_at,
                heartbeat_health=None,
            )
        return assess_worker_admission(
            registration.contract,
            report,
            requirements,
            now=checked_at,
        )

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
                            raise WorkerRegistryStoreError(
                                "Worker registry 是未知的未版本化数据库。"
                            )
                        for statement in _SCHEMA_V1_STATEMENTS:
                            await db.execute(statement)
                        await db.execute(f"PRAGMA user_version = {WORKER_REGISTRY_SCHEMA_VERSION}")
                    elif version != WORKER_REGISTRY_SCHEMA_VERSION:
                        raise WorkerRegistryStoreError(
                            f"Worker registry schema v{version} 不受支持；"
                            f"当前仅支持 v{WORKER_REGISTRY_SCHEMA_VERSION}。"
                        )
                    await db.commit()
                if not existed and os.name != "nt":
                    self._db_path.chmod(0o600)
                self._schema_ready = True
            except WorkerRegistryStoreError:
                raise
            except (aiosqlite.Error, OSError, ValueError) as exc:
                raise WorkerRegistryStoreError("无法初始化 Worker registry。") from exc

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


def _validate_takeover(current: WorkerContract, incoming: WorkerContract) -> None:
    if incoming.epoch <= current.epoch:
        raise WorkerRegistryConflictError(
            f"Worker epoch {incoming.epoch} 未高于当前 epoch {current.epoch}。"
        )
    if incoming.instance_id == current.instance_id:
        raise WorkerRegistryConflictError("新 epoch 必须使用新的 instance_id。")
    if datetime.fromisoformat(incoming.issued_at) < datetime.fromisoformat(current.issued_at):
        raise WorkerRegistryConflictError("新 Worker contract issued_at 发生回退。")


def _serialize_contract(contract: WorkerContract) -> str:
    payload = _json_value(asdict(contract))
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > _MAX_CONTRACT_JSON_BYTES:
        raise ValueError("Worker contract 超过持久化大小上限。")
    return encoded


def _deserialize_contract(raw: str) -> WorkerContract:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_CONTRACT_JSON_BYTES:
        raise ValueError("持久化 Worker contract 大小无效。")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "worker_id",
        "instance_id",
        "epoch",
        "kind",
        "protocol_min",
        "protocol_max",
        "software_version",
        "platform",
        "capabilities",
        "resources",
        "isolation",
        "issued_at",
        "contract_sha256",
    }:
        raise ValueError("持久化 Worker contract 字段集合无效。")
    platform = payload["platform"]
    resources = payload["resources"]
    isolation = payload["isolation"]
    capabilities = payload["capabilities"]
    if not isinstance(platform, dict) or not isinstance(resources, dict):
        raise ValueError("持久化 Worker contract 结构无效。")
    if not isinstance(isolation, dict) or not isinstance(capabilities, list):
        raise ValueError("持久化 Worker contract 能力结构无效。")
    if set(platform) != {
        "system",
        "machine",
        "python_implementation",
        "python_version",
    } or set(resources) != {
        "max_concurrent_jobs",
        "max_memory_bytes",
        "max_cpu_seconds",
        "max_wall_seconds",
        "max_output_bytes",
    }:
        raise ValueError("持久化 Worker contract 平台或资源字段无效。")
    if set(isolation) != {
        "ephemeral_workspace",
        "network_default_deny",
        "environment_allowlist",
        "resource_limits_enforced",
        "process_tree_cancel",
        "artifact_digest",
    }:
        raise ValueError("持久化 Worker contract 隔离字段无效。")
    try:
        contract = WorkerContract(
            schema_version=payload["schema_version"],
            worker_id=payload["worker_id"],
            instance_id=payload["instance_id"],
            epoch=payload["epoch"],
            kind=WorkerKind(payload["kind"]),
            protocol_min=payload["protocol_min"],
            protocol_max=payload["protocol_max"],
            software_version=payload["software_version"],
            platform=WorkerPlatform(**platform),
            capabilities=tuple(WorkerCapability(item) for item in capabilities),
            resources=WorkerResourceEnvelope(**resources),
            isolation=WorkerIsolationContract(**isolation),
            issued_at=payload["issued_at"],
            contract_sha256=payload["contract_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("持久化 Worker contract 内容无效。") from exc
    if not verify_worker_contract(contract):
        raise ValueError("持久化 Worker contract 摘要校验失败。")
    return contract


def _registration_from_row(row: aiosqlite.Row) -> WorkerRegistration:
    contract = _deserialize_contract(str(row["contract_json"]))
    if (
        contract.worker_id != str(row["worker_id"])
        or contract.instance_id != str(row["instance_id"])
        or contract.epoch != int(row["epoch"])
        or contract.contract_sha256 != str(row["contract_sha256"])
    ):
        raise ValueError("Worker registry 索引列与合同不一致。")
    registered_at = normalize_worker_timestamp(str(row["registered_at"]), field="registered_at")
    terminal_at = (
        normalize_worker_timestamp(str(row["terminal_at"]), field="terminal_at")
        if row["terminal_at"] is not None
        else None
    )
    reason_code = str(row["reason_code"]) if row["reason_code"] is not None else None
    if datetime.fromisoformat(contract.issued_at) > datetime.fromisoformat(registered_at):
        raise ValueError("Worker registry registered_at 早于合同 issued_at。")
    if terminal_at is not None and datetime.fromisoformat(terminal_at) < datetime.fromisoformat(
        registered_at
    ):
        raise ValueError("Worker registry terminal_at 早于 registered_at。")
    if reason_code is not None:
        _validate_reason(reason_code)
    return WorkerRegistration(
        contract=contract,
        state=WorkerRegistrationState(str(row["state"])),
        registered_at=registered_at,
        terminal_at=terminal_at,
        reason_code=reason_code,
    )


async def _select_active(
    db: aiosqlite.Connection,
    worker_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM worker_registrations WHERE worker_id = ? AND state = ?",
        (worker_id, WorkerRegistrationState.ACTIVE.value),
    )
    return await cursor.fetchone()


async def _select_registration(
    db: aiosqlite.Connection,
    worker_id: str,
    epoch: int,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM worker_registrations WHERE worker_id = ? AND epoch = ?",
        (worker_id, epoch),
    )
    return await cursor.fetchone()


async def _select_latest(
    db: aiosqlite.Connection,
    worker_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        """
        SELECT * FROM worker_registrations
        WHERE worker_id = ? ORDER BY epoch DESC LIMIT 1
        """,
        (worker_id,),
    )
    return await cursor.fetchone()


async def _user_tables(db: aiosqlite.Connection) -> tuple[str, ...]:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return tuple(str(row[0]) for row in await cursor.fetchall())


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _validate_identifier(value: str, *, field: str) -> None:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not value[0].isalnum()
        or any(character not in allowed for character in value)
    ):
        raise ValueError(f"{field} 格式无效。")


def _validate_reason(value: str) -> None:
    _validate_identifier(value, field="reason_code")


def _registry_file_exists(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WorkerRegistryStoreError("无法检查 Worker registry 路径。") from exc
    if not stat.S_ISREG(mode):
        raise WorkerRegistryStoreError("Worker registry 路径不是文件。")
    return True


_SCHEMA_V1_STATEMENTS = (
    """
CREATE TABLE worker_registrations (
    worker_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    instance_id TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    contract_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'superseded', 'revoked')),
    registered_at TEXT NOT NULL,
    terminal_at TEXT,
    reason_code TEXT,
    PRIMARY KEY (worker_id, epoch),
    UNIQUE (contract_sha256),
    CHECK (
        (state = 'active' AND terminal_at IS NULL AND reason_code IS NULL)
        OR (state != 'active' AND terminal_at IS NOT NULL AND reason_code IS NOT NULL)
    )
)
""",
    """
CREATE UNIQUE INDEX one_active_worker_incarnation
ON worker_registrations (worker_id) WHERE state = 'active'
""",
    """
CREATE INDEX worker_registration_history
ON worker_registrations (worker_id, epoch DESC)
""",
)


__all__ = [
    "WORKER_REGISTRY_SCHEMA_VERSION",
    "WorkerRegistration",
    "WorkerRegistrationState",
    "WorkerRegistryConflictError",
    "WorkerRegistryStore",
    "WorkerRegistryStoreError",
]
