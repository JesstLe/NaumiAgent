"""Recoverable coordinator for Session deletion and Harness reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from naumi_agent.harness.reconciliation import (
    SessionDeleteReconciliation,
    SessionReconciliationState,
)
from naumi_agent.harness.retention import (
    LifecycleActor,
    LifecyclePolicy,
    decide_lifecycle_transition,
    policy_from_session_status,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.tombstone import (
    ReconciliationFailureCode,
    ReconciliationFailureStage,
    ReconciliationTombstoneStatus,
)


class SessionDeletionPort(Protocol):
    async def load(self, session_id: str) -> Any | None: ...

    async def delete(self, session_id: str) -> bool: ...


class ReconciliationCoordinatorOutcome(StrEnum):
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    RETRY_EXHAUSTED = "retry_exhausted"
    NOT_FOUND = "not_found"
    POLICY_BLOCKED = "policy_blocked"


@dataclass(frozen=True, slots=True)
class ReconciliationCoordinatorResult:
    session_id: str
    request_id: str
    outcome: ReconciliationCoordinatorOutcome
    reconciliation_state: SessionReconciliationState | None
    tombstone_status: ReconciliationTombstoneStatus | None
    message: str


class SessionReconciliationCoordinator:
    """Drive durable delete stages without depending on AgentEngine."""

    def __init__(
        self,
        *,
        session_port: SessionDeletionPort,
        harness_store: HarnessStore,
        fallback_workspace: str | Path,
        max_attempts: int = 8,
    ) -> None:
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts 必须在 1 到 100 之间。")
        self._session_port = session_port
        self._harness_store = harness_store
        self._fallback_workspace = Path(fallback_workspace).expanduser().resolve()
        self._max_attempts = max_attempts

    async def delete_session(
        self,
        session_id: str,
        *,
        now: str | None = None,
    ) -> ReconciliationCoordinatorResult:
        """Prepare and execute one user-authorized Session delete request."""
        normalized_session_id = session_id.strip() if isinstance(session_id, str) else ""
        if not normalized_session_id:
            raise ValueError("session_id 不能为空。")
        timestamp = now or _utc_now()
        try:
            session = await self._session_port.load(normalized_session_id)
        except Exception as exc:
            raise RuntimeError("无法读取待删除 Session，协调尚未开始。") from exc
        if session is None:
            return ReconciliationCoordinatorResult(
                session_id=normalized_session_id,
                request_id="",
                outcome=ReconciliationCoordinatorOutcome.NOT_FOUND,
                reconciliation_state=None,
                tombstone_status=None,
                message="Session 不存在，未创建删除协调请求。",
            )

        try:
            current_policy = policy_from_session_status(str(session.status))
        except ValueError:
            return ReconciliationCoordinatorResult(
                session_id=normalized_session_id,
                request_id="",
                outcome=ReconciliationCoordinatorOutcome.POLICY_BLOCKED,
                reconciliation_state=None,
                tombstone_status=None,
                message="Session 生命周期状态未知，已阻止删除。",
            )
        decision = decide_lifecycle_transition(
            current_policy,
            LifecyclePolicy.DELETE,
            actor=LifecycleActor.USER,
        )
        if not decision.allowed:
            return ReconciliationCoordinatorResult(
                session_id=normalized_session_id,
                request_id="",
                outcome=ReconciliationCoordinatorOutcome.POLICY_BLOCKED,
                reconciliation_state=None,
                tombstone_status=None,
                message="Session 生命周期策略阻止删除。",
            )

        workspace = _session_workspace(session, self._fallback_workspace)
        request_id = build_session_delete_request_id(
            session,
            fallback_workspace=self._fallback_workspace,
        )
        record = await self._harness_store.prepare_session_delete_reconciliation(
            request_id=request_id,
            workspace_root=workspace,
            session_id=normalized_session_id,
            actor=LifecycleActor.USER,
            created_at=timestamp,
        )
        existing_tombstone = await self._harness_store.get_reconciliation_tombstone(
            request_id
        )
        if existing_tombstone is not None:
            exhausted = (
                existing_tombstone.status
                is ReconciliationTombstoneStatus.EXHAUSTED
            )
            return ReconciliationCoordinatorResult(
                session_id=normalized_session_id,
                request_id=request_id,
                outcome=(
                    ReconciliationCoordinatorOutcome.RETRY_EXHAUSTED
                    if exhausted
                    else ReconciliationCoordinatorOutcome.RETRY_SCHEDULED
                ),
                reconciliation_state=record.state,
                tombstone_status=existing_tombstone.status,
                message=(
                    "协调重试次数已耗尽，需要人工检查。"
                    if exhausted
                    else "协调已进入持久重试队列，请等待 worker 继续。"
                ),
            )
        return await self._advance(record, now=timestamp, worker_id="")

    async def seed_incomplete_reconciliations(
        self,
        *,
        now: str,
        limit: int = 100,
    ) -> int:
        """Create tombstones for crash gaps that predate failure recording."""
        records = await self._harness_store.list_pending_session_reconciliations(
            limit=limit
        )
        seeded = 0
        for record in records:
            existing = await self._harness_store.get_reconciliation_tombstone(
                record.request_id
            )
            if existing is not None:
                continue
            stage = _stage_for_state(record.state)
            failure_id = _stable_id(
                "discovered",
                record.request_id,
                record.state.value,
                record.updated_at,
            )
            await self._harness_store.record_reconciliation_failure(
                request_id=record.request_id,
                failure_id=failure_id,
                stage=stage,
                error_code=ReconciliationFailureCode.INFRASTRUCTURE_ERROR,
                occurred_at=now,
                max_attempts=self._max_attempts,
            )
            seeded += 1
        return seeded

    async def recover_due(
        self,
        *,
        worker_id: str,
        now: str,
        lease_seconds: int,
        limit: int = 20,
    ) -> tuple[ReconciliationCoordinatorResult, ...]:
        """Discover crash gaps, lease due tombstones, and resume exact states."""
        await self.seed_incomplete_reconciliations(now=now, limit=min(limit, 100))
        tombstones = await self._harness_store.claim_due_reconciliation_tombstones(
            worker_id=worker_id,
            now=now,
            lease_seconds=lease_seconds,
            limit=limit,
        )
        results: list[ReconciliationCoordinatorResult] = []
        for tombstone in tombstones:
            record = await self._harness_store.get_session_delete_reconciliation(
                tombstone.request_id
            )
            if record is None:
                raise RuntimeError("协调 tombstone 缺少对应 reconciliation 记录。")
            results.append(await self._advance(record, now=now, worker_id=worker_id))
        return tuple(results)

    async def _advance(
        self,
        record: SessionDeleteReconciliation,
        *,
        now: str,
        worker_id: str,
    ) -> ReconciliationCoordinatorResult:
        current = record
        if current.state is SessionReconciliationState.PREPARED:
            try:
                deleted = await self._session_port.delete(current.session_id)
                if not deleted:
                    remaining = await self._session_port.load(current.session_id)
                    if remaining is not None:
                        return await self._failure(
                            current,
                            stage=ReconciliationFailureStage.SESSION_DELETE,
                            error_code=ReconciliationFailureCode.INFRASTRUCTURE_ERROR,
                            now=now,
                            worker_id=worker_id,
                        )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._failure(
                        current,
                        stage=ReconciliationFailureStage.SESSION_DELETE,
                        error_code=ReconciliationFailureCode.CANCELLED,
                        now=now,
                        worker_id=worker_id,
                    )
                )
                raise
            except Exception:
                return await self._failure(
                    current,
                    stage=ReconciliationFailureStage.SESSION_DELETE,
                    error_code=ReconciliationFailureCode.SESSION_STORE_ERROR,
                    now=now,
                    worker_id=worker_id,
                )
            try:
                current = await self._harness_store.mark_session_delete_committed(
                    current.request_id,
                    updated_at=now,
                )
            except Exception:
                return await self._failure(
                    current,
                    stage=ReconciliationFailureStage.SESSION_DELETE,
                    error_code=ReconciliationFailureCode.HARNESS_STORE_ERROR,
                    now=now,
                    worker_id=worker_id,
                )

        if current.state is SessionReconciliationState.SESSION_COMMITTED:
            try:
                current = await self._harness_store.reconcile_session_delete_records(
                    current.request_id,
                    updated_at=now,
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._failure(
                        current,
                        stage=ReconciliationFailureStage.HARNESS_RECORDS,
                        error_code=ReconciliationFailureCode.CANCELLED,
                        now=now,
                        worker_id=worker_id,
                    )
                )
                raise
            except Exception:
                return await self._failure(
                    current,
                    stage=ReconciliationFailureStage.HARNESS_RECORDS,
                    error_code=ReconciliationFailureCode.HARNESS_STORE_ERROR,
                    now=now,
                    worker_id=worker_id,
                )

        tombstone_status: ReconciliationTombstoneStatus | None = None
        if worker_id:
            tombstone = await self._harness_store.resolve_reconciliation_tombstone(
                current.request_id,
                worker_id=worker_id,
                resolved_at=now,
            )
            tombstone_status = tombstone.status
        return ReconciliationCoordinatorResult(
            session_id=current.session_id,
            request_id=current.request_id,
            outcome=ReconciliationCoordinatorOutcome.COMPLETED,
            reconciliation_state=current.state,
            tombstone_status=tombstone_status,
            message="Session 与 Harness 记录协调完成。",
        )

    async def _failure(
        self,
        record: SessionDeleteReconciliation,
        *,
        stage: ReconciliationFailureStage,
        error_code: ReconciliationFailureCode,
        now: str,
        worker_id: str,
    ) -> ReconciliationCoordinatorResult:
        previous = await self._harness_store.get_reconciliation_tombstone(
            record.request_id
        )
        failure_id = _stable_id(
            "failure",
            record.request_id,
            stage.value,
            error_code.value,
            now,
            previous.last_failure_id if previous is not None else "",
        )
        tombstone = await self._harness_store.record_reconciliation_failure(
            request_id=record.request_id,
            failure_id=failure_id,
            stage=stage,
            error_code=error_code,
            occurred_at=now,
            max_attempts=self._max_attempts,
            worker_id=worker_id,
        )
        outcome = (
            ReconciliationCoordinatorOutcome.RETRY_EXHAUSTED
            if tombstone.status is ReconciliationTombstoneStatus.EXHAUSTED
            else ReconciliationCoordinatorOutcome.RETRY_SCHEDULED
        )
        return ReconciliationCoordinatorResult(
            session_id=record.session_id,
            request_id=record.request_id,
            outcome=outcome,
            reconciliation_state=record.state,
            tombstone_status=tombstone.status,
            message=(
                "协调重试次数已耗尽，需要人工检查。"
                if outcome is ReconciliationCoordinatorOutcome.RETRY_EXHAUSTED
                else "协调暂未完成，已安全安排重试。"
            ),
        )


def build_session_delete_request_id(
    session: Any,
    *,
    fallback_workspace: str | Path,
) -> str:
    """Build a deterministic id scoped to one persisted Session instance."""
    session_id = str(getattr(session, "id", "") or "").strip()
    if not session_id:
        raise ValueError("Session id 不能为空。")
    workspace = _session_workspace(session, Path(fallback_workspace))
    created_at = getattr(session, "created_at", None)
    if isinstance(created_at, datetime):
        created = created_at.isoformat()
    else:
        created = str(created_at or "").strip()
    if not created:
        raise ValueError("Session created_at 不能为空。")
    payload = json.dumps(
        [str(workspace), session_id, created],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
    return f"session-delete-{digest}"


def _session_workspace(session: Any, fallback: str | Path) -> Path:
    saved = str(getattr(session, "workspace_root", "") or "").strip()
    return Path(saved or fallback).expanduser().resolve()


def _stage_for_state(
    state: SessionReconciliationState,
) -> ReconciliationFailureStage:
    if state is SessionReconciliationState.PREPARED:
        return ReconciliationFailureStage.SESSION_DELETE
    return ReconciliationFailureStage.HARNESS_RECORDS


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:32]
    return f"reconciliation-{digest}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
