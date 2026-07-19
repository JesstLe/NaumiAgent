"""Durable authority for terminal permission decisions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
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

from naumi_agent.safety.permissions import PermissionMode

PERMISSION_DECISION_SCHEMA_VERSION = 4
_MAX_RECEIPT_BYTES = 32 * 1024
_MAX_ARGUMENT_BYTES = 256 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PermissionDecisionOutcome(StrEnum):
    ALLOW_ONCE = "allow_once"
    SESSION_GRANTED = "session_granted"
    BYPASS_ENABLED = "bypass_enabled"
    POLICY_ALLOWED = "policy_allowed"
    DELEGATED_ALLOWED = "delegated_allowed"
    DENIED = "denied"


class PermissionDecisionActor(StrEnum):
    USER = "user"
    RUNTIME = "runtime"


class PermissionDecisionSource(StrEnum):
    USER_CONFIRMATION = "user_confirmation"
    SESSION_GRANT = "session_grant"
    BYPASS = "bypass"
    POLICY = "policy"
    DELEGATED = "delegated"


class PermissionDecisionReceiptError(RuntimeError):
    """Raised when durable permission history cannot be trusted."""


class PermissionDecisionReceiptConflictError(PermissionDecisionReceiptError):
    """Raised when one call is assigned conflicting terminal decisions."""


@dataclass(frozen=True, slots=True)
class PermissionDecisionReceipt:
    schema_version: int
    receipt_id: str
    request_id: str
    session_id: str
    run_id: str
    call_id: str
    agent_name: str
    tool_name: str
    tool_family: str
    arguments_sha256: str
    outcome: PermissionDecisionOutcome
    actor: PermissionDecisionActor
    source: PermissionDecisionSource
    permission_mode: PermissionMode
    risk_level: str
    source_grant_id: str
    delegated_tool_names: tuple[str, ...]
    parent_receipt_id: str
    parent_receipt_sha256: str
    run_delegation_grant_id: str
    run_delegation_grant_sha256: str
    expires_at: str
    decided_at: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2, 3, 4}:
            raise ValueError("权限决定回执 schema_version 必须为 1、2、3 或 4。")
        for field in (
            "receipt_id",
            "request_id",
            "session_id",
            "call_id",
            "agent_name",
            "tool_name",
            "tool_family",
            "risk_level",
        ):
            _require_identifier(getattr(self, field), field=field)
        for field in ("run_id", "source_grant_id"):
            value = getattr(self, field)
            if value:
                _require_identifier(value, field=field)
        if not _SHA256.fullmatch(self.arguments_sha256):
            raise ValueError("arguments_sha256 必须是 SHA-256。")
        if not isinstance(self.outcome, PermissionDecisionOutcome):
            raise TypeError("outcome 必须是 PermissionDecisionOutcome。")
        if not isinstance(self.actor, PermissionDecisionActor):
            raise TypeError("actor 必须是 PermissionDecisionActor。")
        if not isinstance(self.source, PermissionDecisionSource):
            raise TypeError("source 必须是 PermissionDecisionSource。")
        if not isinstance(self.permission_mode, PermissionMode):
            raise TypeError("permission_mode 必须是 PermissionMode。")
        _validate_delegated_tool_names(self.delegated_tool_names)
        if self.schema_version == 1 and self.delegated_tool_names:
            raise ValueError("权限决定回执 v1 不支持委托范围。")
        for field in ("parent_receipt_id", "parent_receipt_sha256", "expires_at"):
            if self.schema_version < 3 and getattr(self, field):
                raise ValueError("权限决定回执 v1/v2 不支持子授权字段。")
        for field in ("run_delegation_grant_id", "run_delegation_grant_sha256"):
            if self.schema_version < 4 and getattr(self, field):
                raise ValueError("权限决定回执 v1/v2/v3 不支持 Run grant 字段。")
        _aware_time(self.decided_at, field="decided_at")
        if not _SHA256.fullmatch(self.receipt_sha256):
            raise ValueError("receipt_sha256 必须是 SHA-256。")
        if self.source is PermissionDecisionSource.USER_CONFIRMATION and self.outcome not in {
            PermissionDecisionOutcome.ALLOW_ONCE,
            PermissionDecisionOutcome.DENIED,
        }:
            raise ValueError("user_confirmation 来源与决定不匹配。")
        if (
            self.source is PermissionDecisionSource.SESSION_GRANT
            and self.outcome is not PermissionDecisionOutcome.SESSION_GRANTED
        ):
            raise ValueError("session_grant 来源与决定不匹配。")
        if self.source is PermissionDecisionSource.SESSION_GRANT:
            if not self.source_grant_id:
                raise ValueError("session_grant 回执必须绑定 source_grant_id。")
        elif self.source_grant_id:
            raise ValueError("非 session_grant 回执不得绑定 source_grant_id。")
        if (
            self.source is PermissionDecisionSource.BYPASS
            and self.outcome is not PermissionDecisionOutcome.BYPASS_ENABLED
        ):
            raise ValueError("bypass 来源与决定不匹配。")
        if self.source is PermissionDecisionSource.POLICY:
            if self.outcome is not PermissionDecisionOutcome.POLICY_ALLOWED:
                raise ValueError("policy 来源与决定不匹配。")
            if self.actor is not PermissionDecisionActor.RUNTIME:
                raise ValueError("policy 决定必须由 Runtime 记录。")
        if self.source is PermissionDecisionSource.DELEGATED:
            if self.outcome is not PermissionDecisionOutcome.DELEGATED_ALLOWED:
                raise ValueError("delegated 来源与决定不匹配。")
            if self.actor is not PermissionDecisionActor.RUNTIME:
                raise ValueError("delegated 决定必须由 Runtime 记录。")
            _require_identifier(self.parent_receipt_id, field="parent_receipt_id")
            _require_sha256(self.parent_receipt_sha256, field="parent_receipt_sha256")
            expires = _aware_time(self.expires_at, field="expires_at")
            if expires <= _aware_time(self.decided_at, field="decided_at"):
                raise ValueError("子授权 expires_at 必须晚于 decided_at。")
            if self.delegated_tool_names:
                raise ValueError("子授权不得继续委托。")
            if bool(self.run_delegation_grant_id) != bool(
                self.run_delegation_grant_sha256
            ):
                raise ValueError("子授权 Run grant id/digest 必须同时存在或同时为空。")
            if self.run_delegation_grant_id:
                _require_identifier(
                    self.run_delegation_grant_id,
                    field="run_delegation_grant_id",
                )
                _require_sha256(
                    self.run_delegation_grant_sha256,
                    field="run_delegation_grant_sha256",
                )
        elif (
            self.parent_receipt_id
            or self.parent_receipt_sha256
            or self.run_delegation_grant_id
            or self.run_delegation_grant_sha256
            or self.expires_at
        ):
            raise ValueError("非 delegated 回执不得包含子授权字段。")

    @property
    def authorizes_execution(self) -> bool:
        return self.outcome is not PermissionDecisionOutcome.DENIED


class PermissionDecisionReceiptStore:
    """SQLite source of truth for immutable terminal permission decisions."""

    def __init__(self, db_path: str | Path) -> None:
        unresolved = Path(db_path).expanduser()
        if not unresolved.is_absolute():
            raise ValueError("权限决定回执路径必须是绝对路径。")
        self._db_path = unresolved.resolve(strict=False)
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def issue(
        self,
        *,
        request_id: str,
        session_id: str,
        run_id: str,
        call_id: str,
        agent_name: str,
        tool_name: str,
        tool_family: str,
        arguments: Mapping[str, object],
        outcome: PermissionDecisionOutcome,
        actor: PermissionDecisionActor,
        source: PermissionDecisionSource,
        permission_mode: PermissionMode,
        risk_level: str,
        source_grant_id: str = "",
        delegated_tool_names: tuple[str, ...] = (),
        decided_at: str,
    ) -> PermissionDecisionReceipt:
        if source is PermissionDecisionSource.DELEGATED:
            raise ValueError("delegated 回执必须通过 issue_delegated() 签发。")
        return await self._issue(
            request_id=request_id,
            session_id=session_id,
            run_id=run_id,
            call_id=call_id,
            agent_name=agent_name,
            tool_name=tool_name,
            tool_family=tool_family,
            arguments=arguments,
            outcome=outcome,
            actor=actor,
            source=source,
            permission_mode=permission_mode,
            risk_level=risk_level,
            source_grant_id=source_grant_id,
            delegated_tool_names=delegated_tool_names,
            parent_receipt_id="",
            parent_receipt_sha256="",
            run_delegation_grant_id="",
            run_delegation_grant_sha256="",
            expires_at="",
            decided_at=decided_at,
        )

    async def issue_delegated(
        self,
        *,
        parent_receipt_id: str,
        request_id: str,
        call_id: str,
        tool_name: str,
        tool_family: str,
        arguments: Mapping[str, object],
        risk_level: str,
        decided_at: str,
        ttl_seconds: int = 60,
    ) -> PermissionDecisionReceipt:
        """Derive one exact, non-transitive child authorization from a parent receipt."""
        parent = await self.get(parent_receipt_id)
        if parent is None or not parent.authorizes_execution:
            raise PermissionDecisionReceiptConflictError("父权限回执不存在或不允许执行。")
        if parent.source is PermissionDecisionSource.DELEGATED:
            raise PermissionDecisionReceiptConflictError("子授权不得继续派生子授权。")
        if parent.source not in {
            PermissionDecisionSource.POLICY,
            PermissionDecisionSource.BYPASS,
            PermissionDecisionSource.USER_CONFIRMATION,
        }:
            raise PermissionDecisionReceiptConflictError(
                "当前父权限来源不支持派生子授权。"
            )
        if tool_name not in parent.delegated_tool_names:
            raise PermissionDecisionReceiptConflictError("父权限回执未授权该下游工具。")
        if not parent.run_id:
            raise PermissionDecisionReceiptConflictError("父权限回执未绑定 run_id。")
        decided = _aware_time(decided_at, field="decided_at")
        parent_time = _aware_time(parent.decided_at, field="parent.decided_at")
        if decided < parent_time or decided - parent_time > timedelta(seconds=300):
            raise PermissionDecisionReceiptConflictError("父权限回执已过期或来自未来。")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise TypeError("ttl_seconds 必须是整数。")
        if not 1 <= ttl_seconds <= 120:
            raise ValueError("ttl_seconds 必须在 1 到 120 之间。")
        return await self._issue(
            request_id=request_id,
            session_id=parent.session_id,
            run_id=parent.run_id,
            call_id=call_id,
            agent_name=parent.agent_name,
            tool_name=tool_name,
            tool_family=tool_family,
            arguments=arguments,
            outcome=PermissionDecisionOutcome.DELEGATED_ALLOWED,
            actor=PermissionDecisionActor.RUNTIME,
            source=PermissionDecisionSource.DELEGATED,
            permission_mode=parent.permission_mode,
            risk_level=risk_level,
            source_grant_id="",
            delegated_tool_names=(),
            parent_receipt_id=parent.receipt_id,
            parent_receipt_sha256=parent.receipt_sha256,
            run_delegation_grant_id="",
            run_delegation_grant_sha256="",
            expires_at=(decided + timedelta(seconds=ttl_seconds)).isoformat(),
            decided_at=decided.isoformat(),
        )

    async def issue_run_delegated(
        self,
        *,
        run_grant_authority: object,
        run_grant_id: str,
        request_id: str,
        call_id: str,
        tool_name: str,
        tool_family: str,
        arguments: Mapping[str, object],
        risk_level: str,
        decided_at: str,
        ttl_seconds: int = 60,
    ) -> PermissionDecisionReceipt:
        """Derive one short-lived child from a currently valid bounded run grant."""
        from naumi_agent.daemons.run_delegation_grants import (
            RunDelegationGrantAuthority,
        )

        if not isinstance(run_grant_authority, RunDelegationGrantAuthority):
            raise TypeError("run_grant_authority 必须是 RunDelegationGrantAuthority。")
        _require_identifier(run_grant_id, field="run_grant_id")
        decided = _aware_time(decided_at, field="decided_at")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise TypeError("ttl_seconds 必须是整数。")
        if not 1 <= ttl_seconds <= 120:
            raise ValueError("ttl_seconds 必须在 1 到 120 之间。")
        validation = await run_grant_authority.validate(
            grant_id=run_grant_id,
            now=decided.isoformat(),
        )
        if not validation.allowed or validation.contract is None:
            raise PermissionDecisionReceiptConflictError(
                "Run delegation grant 当前无效。"
            )
        run_grant = validation.contract
        parent = await self.get(run_grant.parent_receipt_id)
        if (
            parent is None
            or not parent.authorizes_execution
            or parent.receipt_sha256 != run_grant.parent_receipt_sha256
            or parent.session_id != run_grant.session_id
            or parent.run_id != run_grant.run_id
        ):
            raise PermissionDecisionReceiptConflictError(
                "Run delegation grant 的父权限链无效。"
            )
        if tool_name not in run_grant.delegated_tool_names:
            raise PermissionDecisionReceiptConflictError(
                "Run delegation grant 未授权该下游工具。"
            )
        grant_expiry = _aware_time(run_grant.expires_at, field="run_grant.expires_at")
        child_expiry = min(decided + timedelta(seconds=ttl_seconds), grant_expiry)
        if child_expiry <= decided:
            raise PermissionDecisionReceiptConflictError(
                "Run delegation grant 已无可用授权时间。"
            )
        return await self._issue(
            request_id=request_id,
            session_id=parent.session_id,
            run_id=parent.run_id,
            call_id=call_id,
            agent_name=parent.agent_name,
            tool_name=tool_name,
            tool_family=tool_family,
            arguments=arguments,
            outcome=PermissionDecisionOutcome.DELEGATED_ALLOWED,
            actor=PermissionDecisionActor.RUNTIME,
            source=PermissionDecisionSource.DELEGATED,
            permission_mode=parent.permission_mode,
            risk_level=risk_level,
            source_grant_id="",
            delegated_tool_names=(),
            parent_receipt_id=parent.receipt_id,
            parent_receipt_sha256=parent.receipt_sha256,
            run_delegation_grant_id=run_grant.grant_id,
            run_delegation_grant_sha256=run_grant.grant_sha256,
            expires_at=child_expiry.isoformat(),
            decided_at=decided.isoformat(),
        )

    async def _issue(
        self,
        *,
        request_id: str,
        session_id: str,
        run_id: str,
        call_id: str,
        agent_name: str,
        tool_name: str,
        tool_family: str,
        arguments: Mapping[str, object],
        outcome: PermissionDecisionOutcome,
        actor: PermissionDecisionActor,
        source: PermissionDecisionSource,
        permission_mode: PermissionMode,
        risk_level: str,
        source_grant_id: str,
        delegated_tool_names: tuple[str, ...],
        parent_receipt_id: str,
        parent_receipt_sha256: str,
        run_delegation_grant_id: str,
        run_delegation_grant_sha256: str,
        expires_at: str,
        decided_at: str,
    ) -> PermissionDecisionReceipt:
        _validate_delegated_tool_names(delegated_tool_names)
        draft = PermissionDecisionReceipt(
            schema_version=4,
            receipt_id=uuid4().hex,
            request_id=request_id,
            session_id=session_id,
            run_id=run_id,
            call_id=call_id,
            agent_name=agent_name,
            tool_name=tool_name,
            tool_family=tool_family,
            arguments_sha256=permission_arguments_sha256(arguments),
            outcome=outcome,
            actor=actor,
            source=source,
            permission_mode=permission_mode,
            risk_level=risk_level,
            source_grant_id=source_grant_id,
            delegated_tool_names=delegated_tool_names,
            parent_receipt_id=parent_receipt_id,
            parent_receipt_sha256=parent_receipt_sha256,
            run_delegation_grant_id=run_delegation_grant_id,
            run_delegation_grant_sha256=run_delegation_grant_sha256,
            expires_at=expires_at,
            decided_at=_canonical_time(decided_at, field="decided_at"),
            receipt_sha256="0" * 64,
        )
        receipt = replace(draft, receipt_sha256=_receipt_digest(draft))
        await self._ensure_schema()
        raw = _serialize_receipt(receipt)
        try:
            async with self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    SELECT receipt_json FROM permission_decisions
                    WHERE session_id = ? AND call_id = ?
                    """,
                    (session_id, call_id),
                )
                row = await cursor.fetchone()
                if row is not None:
                    existing = _deserialize_receipt(str(row[0]))
                    if _same_decision(existing, receipt):
                        await db.commit()
                        return existing
                    raise PermissionDecisionReceiptConflictError(
                        "同一工具调用已存在不同的终态权限决定。"
                    )
                await db.execute(
                    """
                    INSERT INTO permission_decisions
                    (receipt_id, session_id, call_id, decided_at, receipt_sha256, receipt_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receipt_id,
                        session_id,
                        call_id,
                        receipt.decided_at,
                        receipt.receipt_sha256,
                        raw,
                    ),
                )
                await db.commit()
                return receipt
        except PermissionDecisionReceiptConflictError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise PermissionDecisionReceiptError("无法持久化权限决定回执。") from exc

    async def get(self, receipt_id: str) -> PermissionDecisionReceipt | None:
        _require_identifier(receipt_id, field="receipt_id")
        if not _regular_file_exists(self._db_path):
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    "SELECT * FROM permission_decisions WHERE receipt_id = ?",
                    (receipt_id,),
                )
                row = await cursor.fetchone()
                return _receipt_from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise PermissionDecisionReceiptError("无法读取权限决定回执。") from exc

    def list_session(
        self, session_id: str, *, limit: int = 50
    ) -> tuple[PermissionDecisionReceipt, ...]:
        _require_identifier(session_id, field="session_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间。")
        if not _regular_file_exists(self._db_path):
            return ()
        try:
            with sqlite3.connect(self._db_path) as db:
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if version not in {1, 2, 3, PERMISSION_DECISION_SCHEMA_VERSION}:
                    raise PermissionDecisionReceiptError(
                        f"权限决定回执 schema v{version} 不受支持。"
                    )
                rows = db.execute(
                    """
                    SELECT * FROM permission_decisions
                    WHERE session_id = ?
                    ORDER BY decided_at DESC, receipt_id DESC LIMIT ?
                    """,
                    (session_id, limit),
                ).fetchall()
            return tuple(reversed([_receipt_from_sqlite_row(row) for row in rows]))
        except PermissionDecisionReceiptError:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise PermissionDecisionReceiptError("无法读取权限决定历史。") from exc

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
                    version = int((await (await db.execute("PRAGMA user_version")).fetchone())[0])
                    if version == 0:
                        tables = await _user_tables(db)
                        if tables:
                            raise PermissionDecisionReceiptError(
                                "权限决定回执是未知的未版本化数据库。"
                            )
                        await db.execute(_SCHEMA_V1)
                        await db.execute(
                            f"PRAGMA user_version = {PERMISSION_DECISION_SCHEMA_VERSION}"
                        )
                    elif version in {1, 2, 3}:
                        await db.execute(
                            f"PRAGMA user_version = {PERMISSION_DECISION_SCHEMA_VERSION}"
                        )
                    elif version != PERMISSION_DECISION_SCHEMA_VERSION:
                        raise PermissionDecisionReceiptError(
                            f"权限决定回执 schema v{version} 不受支持。"
                        )
                    await db.commit()
                if not existed and os.name != "nt":
                    self._db_path.chmod(0o600)
                self._schema_ready = True
            except PermissionDecisionReceiptError:
                raise
            except (aiosqlite.Error, OSError, ValueError) as exc:
                raise PermissionDecisionReceiptError("无法初始化权限决定回执 Store。") from exc

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self._db_path, timeout=5.0)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA busy_timeout = 5000")
            yield db
        finally:
            await db.close()


def permission_arguments_sha256(arguments: Mapping[str, object]) -> str:
    if not isinstance(arguments, Mapping) or any(not isinstance(key, str) for key in arguments):
        raise TypeError("权限参数必须是字符串键 Mapping。")
    encoded = json.dumps(
        _json_value(dict(arguments)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_ARGUMENT_BYTES:
        raise ValueError("权限参数超过摘要大小上限。")
    return hashlib.sha256(encoded).hexdigest()


def verify_permission_decision_receipt(receipt: PermissionDecisionReceipt) -> bool:
    return hmac.compare_digest(receipt.receipt_sha256, _receipt_digest(receipt))


def _same_decision(left: PermissionDecisionReceipt, right: PermissionDecisionReceipt) -> bool:
    ignored = {"receipt_id", "decided_at", "expires_at", "receipt_sha256"}
    return all(
        getattr(left, field) == getattr(right, field)
        for field in left.__dataclass_fields__
        if field not in ignored
    )


def _receipt_digest(receipt: PermissionDecisionReceipt) -> str:
    payload = asdict(receipt)
    payload.pop("receipt_sha256")
    if receipt.schema_version == 1:
        payload.pop("delegated_tool_names")
    if receipt.schema_version < 3:
        payload.pop("parent_receipt_id")
        payload.pop("parent_receipt_sha256")
        payload.pop("expires_at")
    if receipt.schema_version < 4:
        payload.pop("run_delegation_grant_id")
        payload.pop("run_delegation_grant_sha256")
    return hashlib.sha256(
        json.dumps(
            _json_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _serialize_receipt(receipt: PermissionDecisionReceipt) -> str:
    raw = json.dumps(
        _json_value(asdict(receipt)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(raw.encode()) > _MAX_RECEIPT_BYTES:
        raise ValueError("权限决定回执超过持久化上限。")
    return raw


def _deserialize_receipt(raw: str) -> PermissionDecisionReceipt:
    if not isinstance(raw, str) or len(raw.encode()) > _MAX_RECEIPT_BYTES:
        raise ValueError("持久化权限决定回执大小无效。")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("持久化权限决定回执字段集合无效。")
    schema_version = payload.get("schema_version")
    expected = set(PermissionDecisionReceipt.__dataclass_fields__)
    child_fields = {"parent_receipt_id", "parent_receipt_sha256", "expires_at"}
    run_grant_fields = {
        "run_delegation_grant_id",
        "run_delegation_grant_sha256",
    }
    if schema_version == 1:
        expected.remove("delegated_tool_names")
        expected.difference_update(child_fields)
        expected.difference_update(run_grant_fields)
        if set(payload) != expected:
            raise ValueError("持久化权限决定回执字段集合无效。")
        payload["delegated_tool_names"] = ()
        payload.update({field: "" for field in child_fields})
        payload.update({field: "" for field in run_grant_fields})
    elif schema_version == 2:
        expected.difference_update(child_fields)
        expected.difference_update(run_grant_fields)
        if set(payload) != expected:
            raise ValueError("持久化权限决定回执字段集合无效。")
        delegated_tool_names = payload["delegated_tool_names"]
        if not isinstance(delegated_tool_names, list):
            raise ValueError("持久化权限决定回执委托范围无效。")
        payload["delegated_tool_names"] = tuple(delegated_tool_names)
        payload.update({field: "" for field in child_fields})
        payload.update({field: "" for field in run_grant_fields})
    elif schema_version == 3:
        expected.difference_update(run_grant_fields)
        if set(payload) != expected:
            raise ValueError("持久化权限决定回执字段集合无效。")
        delegated_tool_names = payload["delegated_tool_names"]
        if not isinstance(delegated_tool_names, list):
            raise ValueError("持久化权限决定回执委托范围无效。")
        payload["delegated_tool_names"] = tuple(delegated_tool_names)
        payload.update({field: "" for field in run_grant_fields})
    elif schema_version == 4:
        if set(payload) != expected:
            raise ValueError("持久化权限决定回执字段集合无效。")
        delegated_tool_names = payload["delegated_tool_names"]
        if not isinstance(delegated_tool_names, list):
            raise ValueError("持久化权限决定回执委托范围无效。")
        payload["delegated_tool_names"] = tuple(delegated_tool_names)
    else:
        raise ValueError("持久化权限决定回执 schema_version 无效。")
    receipt = PermissionDecisionReceipt(
        **{
            **payload,
            "outcome": PermissionDecisionOutcome(payload["outcome"]),
            "actor": PermissionDecisionActor(payload["actor"]),
            "source": PermissionDecisionSource(payload["source"]),
            "permission_mode": PermissionMode(payload["permission_mode"]),
        }
    )
    if not verify_permission_decision_receipt(receipt):
        raise ValueError("持久化权限决定回执摘要校验失败。")
    return receipt


def _receipt_from_row(row: aiosqlite.Row) -> PermissionDecisionReceipt:
    receipt = _deserialize_receipt(str(row["receipt_json"]))
    if (
        receipt.receipt_id,
        receipt.session_id,
        receipt.call_id,
        receipt.decided_at,
        receipt.receipt_sha256,
    ) != (
        str(row["receipt_id"]),
        str(row["session_id"]),
        str(row["call_id"]),
        str(row["decided_at"]),
        str(row["receipt_sha256"]),
    ):
        raise ValueError("权限决定回执索引列与内容不一致。")
    return receipt


def _receipt_from_sqlite_row(row: tuple[Any, ...]) -> PermissionDecisionReceipt:
    receipt = _deserialize_receipt(str(row[5]))
    if (
        receipt.receipt_id,
        receipt.session_id,
        receipt.call_id,
        receipt.decided_at,
        receipt.receipt_sha256,
    ) != tuple(str(value) for value in row[:5]):
        raise ValueError("权限决定回执索引列与内容不一致。")
    return receipt


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} 必须是安全标识符。")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} 必须是 SHA-256。")


def _validate_delegated_tool_names(value: tuple[str, ...]) -> None:
    if not isinstance(value, tuple):
        raise TypeError("delegated_tool_names 必须是 tuple。")
    if len(value) > 16 or value != tuple(sorted(set(value))):
        raise ValueError("delegated_tool_names 必须唯一、排序且不超过 16 项。")
    for tool_name in value:
        _require_identifier(tool_name, field="delegated_tool_names")


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
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return tuple(str(row[0]) for row in await cursor.fetchall())


def _regular_file_exists(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PermissionDecisionReceiptError("无法检查权限决定回执路径。") from exc
    if not stat.S_ISREG(mode):
        raise PermissionDecisionReceiptError("权限决定回执路径不是文件。")
    return True


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


_SCHEMA_V1 = """
CREATE TABLE permission_decisions (
    receipt_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    UNIQUE (session_id, call_id)
)
"""


__all__ = [
    "PERMISSION_DECISION_SCHEMA_VERSION",
    "PermissionDecisionActor",
    "PermissionDecisionOutcome",
    "PermissionDecisionReceipt",
    "PermissionDecisionReceiptConflictError",
    "PermissionDecisionReceiptError",
    "PermissionDecisionReceiptStore",
    "PermissionDecisionSource",
    "permission_arguments_sha256",
    "verify_permission_decision_receipt",
]
