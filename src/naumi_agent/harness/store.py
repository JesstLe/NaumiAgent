"""Durable Harness profiles, completion runs, checks, and evidence metadata."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite

from naumi_agent.harness.artifact_gc import (
    ArtifactGarbageCollectionError,
    ArtifactGarbageCollector,
)
from naumi_agent.harness.checks import HarnessCheckResult
from naumi_agent.harness.completion import (
    HarnessCompletionReceipt,
    HarnessEvidenceRef,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalGuardrailStatus,
    EvalRunStatus,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_receipt import HarnessEvalComparisonReceipt
from naumi_agent.harness.heartbeat import (
    HarnessHeartbeat,
    HarnessHeartbeatPhase,
)
from naumi_agent.harness.interaction import (
    HarnessInteractionRecord,
    answer_interaction,
    cancel_interaction,
    expire_interaction,
    takeover_interaction,
)
from naumi_agent.harness.models import HarnessCompletionContract
from naumi_agent.harness.reconciliation import (
    ReconciliationArtifactGcStatus,
    ReconciliationArtifactKind,
    ReconciliationArtifactReference,
    SessionDeleteReconciliation,
    SessionReconciliationState,
    SessionReconciliationTerminalOutcome,
    SessionReconciliationTransitionError,
    validate_reconciliation_transition,
)
from naumi_agent.harness.replay_models import HarnessReplayBaselinePayload
from naumi_agent.harness.retention import LifecycleActor, LifecyclePolicy
from naumi_agent.harness.run_lease import (
    HarnessRunFenceDecision,
    HarnessRunFenceReason,
    HarnessRunFenceReceipt,
    HarnessRunKind,
    HarnessRunLease,
    HarnessRunLeaseState,
)
from naumi_agent.harness.tombstone import (
    ReconciliationFailureCode,
    ReconciliationFailureStage,
    ReconciliationTombstone,
    ReconciliationTombstoneStatus,
    compute_retry_delay_seconds,
)
from naumi_agent.harness.trust import resolve_harness_trust_db_path
from naumi_agent.safety.guardrails import OutputGuardrail

HARNESS_STORE_SCHEMA_VERSION = 14
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVAL_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RUN_LEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_INTERACTION_ID_RE = re.compile(r"^ask-[A-Za-z0-9._:-]{1,128}$")
_CONVERSATION_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONVERSATION_QUEUE_TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})
_MAX_DURABLE_CONVERSATION_QUEUE_ITEMS = 20
_MAX_EVAL_RESULT_BYTES = 4 * 1024 * 1024
_SECRET_ARG_NAME_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|authorization|cookie)",
    re.IGNORECASE,
)
_BEARER_VALUE_RE = re.compile(r"^bearer\s+\S+", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[_-]?key|token|secret|password|passwd|authorization|cookie|credential)"
    r"\s*([:=])\s*([^\s,;]+)",
    re.IGNORECASE,
)
_PROFILE_STATUSES = {"missing", "invalid", "untrusted", "trusted"}
_SENSITIVE_SUMMARY_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "token",
    "access_token",
}


class HarnessStoreError(RuntimeError):
    """Raised when the durable Harness state cannot be read or written."""


class HarnessStoreConflictError(HarnessStoreError):
    """Raised when an idempotency key is reused for different immutable data."""


@dataclass(frozen=True, slots=True)
class HarnessStoredProfile:
    workspace_root: str
    profile_digest: str
    schema_version: int
    loaded_at: str
    trusted_at: str
    trust_source: str
    status: str


@dataclass(frozen=True, slots=True)
class HarnessStoredCriterion:
    id: str
    description: str
    source_kind: str
    source_ref: str
    status: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HarnessStoredCheck:
    id: str
    check_key: str
    argv: tuple[str, ...]
    cwd: str
    status: str
    exit_code: int | None
    duration_ms: int
    started_at: str
    completed_at: str
    tree_fingerprint: str
    profile_digest: str
    artifact_path: str


@dataclass(frozen=True, slots=True)
class HarnessStoredEvidence:
    id: str
    kind: str
    uri: str
    sha256: str
    description: str
    summary: dict[str, Any]
    producer: str
    created_at: str
    criterion_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HarnessStoredRun:
    id: str
    workspace_root: str
    session_id: str
    task_id: str | None
    issue_id: str | None
    task_kind: str
    objective: str
    status: str
    profile_digest: str | None
    tree_fingerprint_before: str
    tree_fingerprint_after: str
    started_at: str
    completed_at: str
    contract: HarnessCompletionContract
    receipt: HarnessCompletionReceipt | None
    criteria: tuple[HarnessStoredCriterion, ...]
    checks: tuple[HarnessStoredCheck, ...]
    evidence: tuple[HarnessStoredEvidence, ...]


@dataclass(frozen=True, slots=True)
class HarnessStoredReplayBaseline:
    run_id: str
    manifest_json: str
    manifest_sha256: str
    rule_version: str
    explanation_json: str
    explanation_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class HarnessStoredEvalResult:
    id: str
    workspace_root: str
    batch_id: str
    suite_id: str
    sample_index: int
    identity_sha256: str
    result_sha256: str
    result: HarnessEvalSuiteResult
    created_at: str


@dataclass(frozen=True, slots=True)
class HarnessStoredEvalBaseline:
    id: str
    workspace_root: str
    suite_id: str
    version: int
    batch_id: str
    identity_sha256: str
    sample_count: int
    samples_sha256: str
    baseline_sha256: str
    promoted_by: str
    promotion_reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class HarnessStoredEvalBaselineEvent:
    id: str
    workspace_root: str
    suite_id: str
    baseline_id: str
    previous_baseline_id: str
    actor: str
    reason: str
    created_at: str
    event_sha256: str


@dataclass(frozen=True, slots=True)
class HarnessStoredEvalComparisonReceipt:
    id: str
    workspace_root: str
    suite_id: str
    baseline_id: str
    current_batch_id: str
    decision: str
    receipt_sha256: str
    receipt: HarnessEvalComparisonReceipt
    created_at: str


@dataclass(frozen=True, slots=True)
class HarnessConversationQueueItem:
    """One durable, workspace/session-scoped conversation submission."""

    workspace_root: str
    session_id: str
    request_id: str
    client_id: str
    text: str
    payload_sha256: str
    state: str
    position: int
    enqueued_at: str
    updated_at: str
    terminal_reason: str


@dataclass(frozen=True, slots=True)
class HarnessSessionDeleteImpact:
    """Workspace-scoped Harness records associated with one session."""

    workspace_root: str
    session_id: str
    run_count: int = 0
    criterion_count: int = 0
    check_count: int = 0
    evidence_count: int = 0
    replay_baseline_count: int = 0
    check_artifact_reference_count: int = 0
    evidence_artifact_reference_count: int = 0

    @property
    def artifact_reference_count(self) -> int:
        """Return references, not unique or safely deletable artifact files."""
        return (
            self.check_artifact_reference_count
            + self.evidence_artifact_reference_count
        )


def resolve_harness_db_path() -> Path:
    """Return the user-state Harness DB path without consulting the workspace."""
    return resolve_harness_trust_db_path().with_name("harness.db")


class HarnessStore:
    """SQLite store with workspace isolation and idempotent lifecycle writes."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def record_profile(
        self,
        *,
        workspace_root: str | Path,
        profile_digest: str,
        schema_version: int,
        loaded_at: str,
        trusted_at: str,
        trust_source: str,
        status: str,
    ) -> HarnessStoredProfile:
        workspace = _canonical_workspace(workspace_root)
        digest = _validate_sha256(profile_digest, field="profile_digest")
        if schema_version < 1:
            raise ValueError("schema_version 必须大于或等于 1。")
        loaded = _normalize_timestamp(loaded_at, field="loaded_at")
        trusted = _normalize_optional_timestamp(trusted_at, field="trusted_at")
        source = _normalize_text(trust_source, field="trust_source", max_length=64)
        normalized_status = status.strip() if isinstance(status, str) else ""
        if normalized_status not in _PROFILE_STATUSES:
            raise ValueError("Harness Profile status 无效。")

        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    """
                    INSERT INTO harness_profiles (
                        workspace_root, profile_digest, schema_version, loaded_at,
                        trusted_at, trust_source, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workspace_root, profile_digest) DO UPDATE SET
                        schema_version = excluded.schema_version,
                        loaded_at = excluded.loaded_at,
                        trusted_at = excluded.trusted_at,
                        trust_source = excluded.trust_source,
                        status = excluded.status
                    """,
                    (
                        workspace,
                        digest,
                        schema_version,
                        loaded,
                        trusted,
                        source,
                        normalized_status,
                    ),
                )
                await db.commit()
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError(
                "无法保存 Harness Profile 状态。请检查用户状态目录权限。"
            ) from exc
        return HarnessStoredProfile(
            workspace_root=workspace,
            profile_digest=digest,
            schema_version=schema_version,
            loaded_at=loaded,
            trusted_at=trusted,
            trust_source=source,
            status=normalized_status,
        )

    async def start_run(
        self,
        *,
        workspace_root: str | Path,
        contract: HarnessCompletionContract,
        tree_fingerprint_before: str,
        started_at: str,
    ) -> HarnessStoredRun:
        workspace = _canonical_workspace(workspace_root)
        fingerprint = _normalize_fingerprint(tree_fingerprint_before)
        started = _normalize_timestamp(started_at, field="started_at")
        contract_json = _model_json(contract)
        stored_contract = HarnessCompletionContract.model_validate_json(contract_json)
        identity = (
            workspace,
            stored_contract.session_id,
            stored_contract.task_id or "",
            stored_contract.issue_id or "",
            stored_contract.task_kind.value,
            stored_contract.objective,
            stored_contract.profile_digest or "",
            fingerprint,
            started,
            contract_json,
        )

        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    SELECT workspace_root, session_id, task_id, issue_id, task_kind,
                           objective, profile_digest, tree_fingerprint_before,
                           started_at, contract_json
                    FROM harness_runs WHERE id = ?
                    """,
                    (contract.run_id,),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    existing_identity = tuple(str(value or "") for value in existing)
                    if existing_identity != identity:
                        await db.rollback()
                        raise HarnessStoreConflictError(
                            f"Harness run {contract.run_id} 已存在，且内容与本次写入冲突。"
                        )
                    await self._verify_criteria_match(db, stored_contract)
                    await db.rollback()
                    restored = await self.get_run(contract.run_id)
                    assert restored is not None
                    return restored

                await db.execute(
                    """
                    INSERT INTO harness_runs (
                        id, workspace_root, session_id, task_id, issue_id, task_kind,
                        objective, status, profile_digest, tree_fingerprint_before,
                        tree_fingerprint_after, started_at, completed_at,
                        contract_json, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, '', ?, '', ?, '')
                    """,
                    (
                        contract.run_id,
                        workspace,
                        stored_contract.session_id,
                        stored_contract.task_id or "",
                        stored_contract.issue_id or "",
                        stored_contract.task_kind.value,
                        stored_contract.objective,
                        stored_contract.profile_digest or "",
                        fingerprint,
                        started,
                        contract_json,
                    ),
                )
                source_ref = (
                    stored_contract.source_refs[0]
                    if stored_contract.source_refs
                    else ""
                )
                await db.executemany(
                    """
                    INSERT INTO harness_contract_criteria (
                        run_id, criterion_id, description, source_kind, source_ref,
                        status, evidence_ids_json
                    ) VALUES (?, ?, ?, 'completion_contract', ?, 'pending', '[]')
                    """,
                    [
                        (
                            contract.run_id,
                            criterion.id,
                            criterion.description,
                            source_ref,
                        )
                        for criterion in stored_contract.acceptance_criteria
                    ],
                )
                await db.commit()
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError(
                "无法创建 Harness 运行记录。主任务可继续，但本次回执无法持久化。"
            ) from exc
        restored = await self.get_run(contract.run_id)
        assert restored is not None
        return restored

    async def record_check(
        self,
        *,
        result: HarnessCheckResult,
        argv: Sequence[str],
        cwd: str | Path,
        started_at: str,
        completed_at: str,
        artifact_path: str = "",
    ) -> HarnessStoredCheck:
        normalized_argv = _normalize_argv(argv)
        started = _normalize_timestamp(started_at, field="started_at")
        completed = _normalize_timestamp(completed_at, field="completed_at")
        artifact = _normalize_optional_reference(
            artifact_path,
            field="artifact_path",
        )
        check_key = result.check_id
        record_id = _stable_id(
            result.run_id,
            check_key,
            result.profile_digest,
            result.tree_fingerprint,
            started,
        )

        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                run = await self._require_run(db, result.run_id)
                normalized_cwd = _canonical_cwd(cwd, workspace_root=str(run["workspace_root"]))
                payload = (
                    record_id,
                    result.run_id,
                    check_key,
                    _json_dumps(list(normalized_argv)),
                    normalized_cwd,
                    result.status.value,
                    result.exit_code,
                    result.duration_ms,
                    started,
                    completed,
                    _normalize_fingerprint(result.tree_fingerprint),
                    _validate_sha256(result.profile_digest, field="profile_digest"),
                    artifact,
                )
                cursor = await db.execute(
                    "SELECT * FROM harness_checks WHERE id = ?",
                    (record_id,),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    if _check_row_payload(existing) != payload:
                        await db.rollback()
                        raise HarnessStoreConflictError(
                            f"Harness check {result.check_id} 的幂等记录发生冲突。"
                        )
                    await db.rollback()
                    return _check_from_row(existing)
                await db.execute(
                    """
                    INSERT INTO harness_checks (
                        id, run_id, check_key, argv_json, cwd, status, exit_code,
                        duration_ms, started_at, completed_at, tree_fingerprint,
                        profile_digest, artifact_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                await db.commit()
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError(
                "无法保存 Harness 检查结果。请检查用户状态目录权限。"
            ) from exc
        return HarnessStoredCheck(
            id=record_id,
            check_key=check_key,
            argv=normalized_argv,
            cwd=normalized_cwd,
            status=result.status.value,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            started_at=started,
            completed_at=completed,
            tree_fingerprint=result.tree_fingerprint,
            profile_digest=result.profile_digest,
            artifact_path=artifact,
        )

    async def record_evidence(
        self,
        *,
        run_id: str,
        evidence: HarnessEvidenceRef,
        uri: str,
        sha256: str,
        summary: Mapping[str, Any],
        producer: str,
        created_at: str,
    ) -> HarnessStoredEvidence:
        normalized_uri = _normalize_reference(uri, field="uri")
        digest = _validate_sha256(sha256, field="sha256")
        structured_summary = _normalize_summary(summary)
        normalized_producer = _normalize_text(
            producer,
            field="producer",
            max_length=128,
        )
        created = _normalize_timestamp(created_at, field="created_at")
        description = _redact_sensitive_text(evidence.summary)
        summary_json = _json_dumps({"text": description, "data": structured_summary})
        criterion_ids_json = _json_dumps(list(evidence.criterion_ids))
        payload = (
            evidence.id,
            run_id,
            evidence.kind,
            normalized_uri,
            digest,
            summary_json,
            normalized_producer,
            created,
            criterion_ids_json,
        )

        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await self._require_run(db, run_id)
                await self._verify_evidence_criteria(
                    db,
                    run_id=run_id,
                    criterion_ids=evidence.criterion_ids,
                )
                cursor = await db.execute(
                    "SELECT * FROM harness_evidence WHERE run_id = ? AND id = ?",
                    (run_id, evidence.id),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    if _evidence_row_payload(existing) != payload:
                        await db.rollback()
                        raise HarnessStoreConflictError(
                            f"Harness evidence {evidence.id} 的幂等记录发生冲突。"
                        )
                    await db.rollback()
                    return _evidence_from_row(existing)
                await db.execute(
                    """
                    INSERT INTO harness_evidence (
                        id, run_id, kind, uri, sha256, summary_json, producer,
                        created_at, criterion_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                await db.commit()
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError(
                "无法保存 Harness 证据索引。请检查用户状态目录权限。"
            ) from exc
        return HarnessStoredEvidence(
            id=evidence.id,
            kind=evidence.kind,
            uri=normalized_uri,
            sha256=digest,
            description=description,
            summary=structured_summary,
            producer=normalized_producer,
            created_at=created,
            criterion_ids=evidence.criterion_ids,
        )

    async def finish_run(
        self,
        *,
        run_id: str,
        receipt: HarnessCompletionReceipt,
        completed_at: str,
        contract: HarnessCompletionContract | None = None,
    ) -> HarnessStoredRun:
        if receipt.run_id != run_id:
            raise ValueError("receipt.run_id 与目标 Harness run 不一致。")
        completed = _normalize_timestamp(completed_at, field="completed_at")
        receipt_json = _model_json(receipt)

        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                run = await self._require_run(db, run_id)
                final_contract = await self._validate_final_contract(
                    db,
                    run=run,
                    contract=contract,
                )
                final_contract_json = _model_json(final_contract)
                if receipt.task_kind is not final_contract.task_kind:
                    await db.rollback()
                    raise HarnessStoreConflictError(
                        "完成回执的 task kind 与最终 Harness contract 不一致。"
                    )
                existing_receipt = str(run["receipt_json"])
                if existing_receipt:
                    if (
                        existing_receipt != receipt_json
                        or str(run["contract_json"]) != final_contract_json
                    ):
                        await db.rollback()
                        raise HarnessStoreConflictError(
                            f"Harness run {run_id} 已完成，不能用不同回执覆盖。"
                        )
                    await db.rollback()
                    restored = await self.get_run(run_id)
                    assert restored is not None
                    return restored

                cursor = await db.execute(
                    """
                    SELECT criterion_id FROM harness_contract_criteria
                    WHERE run_id = ? ORDER BY criterion_id
                    """,
                    (run_id,),
                )
                stored_ids = tuple(str(row[0]) for row in await cursor.fetchall())
                receipt_ids = tuple(sorted(item.id for item in receipt.criteria))
                if stored_ids != receipt_ids:
                    await db.rollback()
                    raise HarnessStoreConflictError(
                        "完成回执的验收条件与 Harness contract 不一致。"
                    )
                await db.execute(
                    """
                    UPDATE harness_runs SET
                        status = ?, tree_fingerprint_after = ?, completed_at = ?,
                        task_kind = ?, objective = ?, profile_digest = ?,
                        contract_json = ?, receipt_json = ?
                    WHERE id = ?
                    """,
                    (
                        receipt.status,
                        _normalize_fingerprint(receipt.tree_fingerprint),
                        completed,
                        final_contract.task_kind.value,
                        final_contract.objective,
                        final_contract.profile_digest or "",
                        final_contract_json,
                        receipt_json,
                        run_id,
                    ),
                )
                await db.executemany(
                    """
                    UPDATE harness_contract_criteria
                    SET status = ?, evidence_ids_json = ?
                    WHERE run_id = ? AND criterion_id = ?
                    """,
                    [
                        (
                            item.status,
                            _json_dumps(list(item.evidence_ids)),
                            run_id,
                            item.id,
                        )
                        for item in receipt.criteria
                    ],
                )
                await db.commit()
        except HarnessStoreConflictError:
            raise
        except (
            aiosqlite.Error,
            OSError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise HarnessStoreError(
                "无法完成 Harness 运行记录。主任务结果仍保留，但回执未持久化。"
            ) from exc
        restored = await self.get_run(run_id)
        assert restored is not None
        try:
            from naumi_agent.harness.replay import capture_replay_baseline

            payload = await asyncio.to_thread(
                capture_replay_baseline,
                restored,
                workspace_root=restored.workspace_root,
            )
            await self.record_replay_baseline(payload, created_at=completed)
        except (HarnessStoreError, OSError, RuntimeError, ValueError):
            # The completion receipt is already durable. Replay will surface a
            # missing baseline as a legacy/partial run instead of hiding success.
            pass
        return restored

    async def record_replay_baseline(
        self,
        payload: HarnessReplayBaselinePayload,
        *,
        created_at: str,
    ) -> HarnessStoredReplayBaseline:
        """Insert one immutable replay baseline, or return its identical record."""
        run_id = _normalize_text(payload.run_id, field="run_id", max_length=128)
        manifest_sha256 = _validate_sha256(
            payload.manifest_sha256,
            field="manifest_sha256",
        )
        explanation_sha256 = _validate_sha256(
            payload.explanation_sha256,
            field="explanation_sha256",
        )
        if _stable_digest(payload.manifest_json) != manifest_sha256:
            raise ValueError("Replay manifest digest 与内容不一致。")
        if _stable_digest(payload.explanation_json) != explanation_sha256:
            raise ValueError("Replay explanation digest 与内容不一致。")
        _validate_json_object(payload.manifest_json, field="manifest_json")
        _validate_json_object(payload.explanation_json, field="explanation_json")
        rule_version = _normalize_text(
            payload.rule_version,
            field="rule_version",
            max_length=64,
        )
        created = _normalize_timestamp(created_at, field="created_at")
        values = (
            run_id,
            payload.manifest_json,
            manifest_sha256,
            rule_version,
            payload.explanation_json,
            explanation_sha256,
            created,
        )

        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await self._require_run(db, run_id)
                cursor = await db.execute(
                    "SELECT * FROM harness_replay_baselines WHERE run_id = ?",
                    (run_id,),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    stored = _replay_baseline_from_row(existing)
                    if stored != HarnessStoredReplayBaseline(*values):
                        await db.rollback()
                        raise HarnessStoreConflictError(
                            f"Harness run {run_id} 的 Replay 基线不可变，不能覆盖。"
                        )
                    await db.rollback()
                    return stored
                await db.execute(
                    """
                    INSERT INTO harness_replay_baselines (
                        run_id, manifest_json, manifest_sha256, rule_version,
                        explanation_json, explanation_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                await db.commit()
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError("无法保存 Harness Replay 基线。") from exc
        return HarnessStoredReplayBaseline(*values)

    async def get_replay_baseline(
        self,
        run_id: str,
    ) -> HarnessStoredReplayBaseline | None:
        """Read one replay baseline without creating or changing it."""
        normalized_run_id = _normalize_text(run_id, field="run_id", max_length=128)
        if not self._db_path.is_file():
            return None
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    "SELECT * FROM harness_replay_baselines WHERE run_id = ?",
                    (normalized_run_id,),
                )
                row = await cursor.fetchone()
                return _replay_baseline_from_row(row) if row is not None else None
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessStoreError("无法读取 Harness Replay 基线。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("Harness Replay 基线损坏或无法读取。") from exc

    async def record_eval_result(
        self,
        *,
        workspace_root: str | Path,
        batch_id: str,
        sample_index: int,
        result: HarnessEvalSuiteResult,
        created_at: str,
    ) -> HarnessStoredEvalResult:
        """Insert one immutable typed Eval sample, or return an identical retry."""
        workspace = _canonical_workspace(workspace_root)
        normalized_batch = _normalize_eval_batch_id(batch_id)
        if not isinstance(sample_index, int) or isinstance(sample_index, bool):
            raise ValueError("sample_index 必须是整数。")
        if not 0 <= sample_index <= 9_999:
            raise ValueError("sample_index 必须在 0..9999 之间。")
        if not isinstance(result, HarnessEvalSuiteResult):
            raise ValueError("result 必须是 HarnessEvalSuiteResult。")
        suite_id = _normalize_text(result.suite_id, field="suite_id", max_length=64)
        created = _normalize_timestamp(created_at, field="created_at")
        safe_payload = _redact_json_value(result.model_dump(mode="json"))
        safe_result = HarnessEvalSuiteResult.model_validate(safe_payload)
        result_json = _json_dumps(safe_result.model_dump(mode="json"))
        if len(result_json.encode("utf-8")) > _MAX_EVAL_RESULT_BYTES:
            raise ValueError("Eval Result 不能超过 4 MiB。")
        result_sha256 = _stable_digest(result_json)
        identity_sha256 = (
            safe_result.baseline_identity.identity_sha256
            if safe_result.baseline_identity is not None
            else ""
        )
        record_id = _stable_id(
            workspace,
            normalized_batch,
            suite_id,
            str(sample_index),
        )
        stored = HarnessStoredEvalResult(
            id=record_id,
            workspace_root=workspace,
            batch_id=normalized_batch,
            suite_id=suite_id,
            sample_index=sample_index,
            identity_sha256=identity_sha256,
            result_sha256=result_sha256,
            result=safe_result,
            created_at=created,
        )
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM harness_eval_results WHERE id = ?",
                    (record_id,),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    restored = _eval_result_from_row(existing)
                    if restored.result_sha256 != result_sha256:
                        await db.rollback()
                        raise HarnessStoreConflictError(
                            "同一 Eval batch/suite/sample 不可覆盖为不同结果。"
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    """
                    INSERT INTO harness_eval_results (
                        id, workspace_root, batch_id, suite_id, sample_index,
                        identity_sha256, result_sha256, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        workspace,
                        normalized_batch,
                        suite_id,
                        sample_index,
                        identity_sha256,
                        result_sha256,
                        result_json,
                        created,
                    ),
                )
                await db.commit()
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法保存 Harness Eval Result。") from exc
        return stored

    async def get_eval_result(
        self,
        workspace_root: str | Path,
        batch_id: str,
        suite_id: str,
        sample_index: int,
    ) -> HarnessStoredEvalResult | None:
        """Read one exact workspace-scoped Eval sample without mutation."""
        workspace = _canonical_workspace(workspace_root)
        batch = _normalize_eval_batch_id(batch_id)
        suite = _normalize_text(suite_id, field="suite_id", max_length=64)
        if (
            not isinstance(sample_index, int)
            or isinstance(sample_index, bool)
            or not 0 <= sample_index <= 9_999
        ):
            raise ValueError("sample_index 必须在 0..9999 之间。")
        if not self._db_path.is_file():
            return None
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_results
                    WHERE workspace_root = ? AND batch_id = ?
                      AND suite_id = ? AND sample_index = ?
                    """,
                    (workspace, batch, suite, sample_index),
                )
                row = await cursor.fetchone()
                return _eval_result_from_row(row) if row is not None else None
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessStoreError("无法读取 Harness Eval Result。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("Harness Eval Result 损坏或无法读取。") from exc

    async def list_eval_results(
        self,
        workspace_root: str | Path,
        batch_id: str,
        suite_id: str,
        *,
        limit: int = 100,
    ) -> tuple[HarnessStoredEvalResult, ...]:
        """List one exact cohort in sample order for statistical comparison."""
        workspace = _canonical_workspace(workspace_root)
        batch = _normalize_eval_batch_id(batch_id)
        suite = _normalize_text(suite_id, field="suite_id", max_length=64)
        if not 1 <= limit <= 10_000:
            raise ValueError("limit 必须在 1..10000 之间。")
        if not self._db_path.is_file():
            return ()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_results
                    WHERE workspace_root = ? AND batch_id = ? AND suite_id = ?
                    ORDER BY sample_index ASC
                    LIMIT ?
                    """,
                    (workspace, batch, suite, limit),
                )
                return tuple(
                    _eval_result_from_row(row) for row in await cursor.fetchall()
                )
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return ()
            raise HarnessStoreError("无法列出 Harness Eval Result。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("Harness Eval Result 列表损坏或无法读取。") from exc

    async def promote_eval_baseline(
        self,
        *,
        workspace_root: str | Path,
        batch_id: str,
        suite_id: str,
        promoted_by: str,
        promotion_reason: str,
        created_at: str,
    ) -> HarnessStoredEvalBaseline:
        """Promote one eligible immutable cohort and atomically select it."""
        workspace = _canonical_workspace(workspace_root)
        batch = _normalize_eval_batch_id(batch_id)
        suite = _normalize_text(suite_id, field="suite_id", max_length=64)
        actor = _normalize_text(promoted_by, field="promoted_by", max_length=128)
        reason = _normalize_text(
            promotion_reason,
            field="promotion_reason",
            max_length=2_000,
        )
        created = _normalize_timestamp(created_at, field="created_at")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_results
                    WHERE workspace_root = ? AND batch_id = ? AND suite_id = ?
                    ORDER BY sample_index ASC
                    """,
                    (workspace, batch, suite),
                )
                samples = tuple(
                    _eval_result_from_row(row) for row in await cursor.fetchall()
                )
                identity_sha256, samples_sha256 = _validate_baseline_cohort(samples)
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_baselines
                    WHERE workspace_root = ? AND suite_id = ? AND batch_id = ?
                    """,
                    (workspace, suite, batch),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    baseline = _eval_baseline_from_row(existing)
                    if (
                        baseline.identity_sha256 != identity_sha256
                        or baseline.samples_sha256 != samples_sha256
                    ):
                        await db.rollback()
                        raise HarnessStoreConflictError(
                            "已晋升的 Eval batch 不可覆盖为不同 cohort。"
                        )
                    await db.rollback()
                    return baseline
                cursor = await db.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1
                    FROM harness_eval_baselines
                    WHERE workspace_root = ? AND suite_id = ?
                    """,
                    (workspace, suite),
                )
                version = int((await cursor.fetchone())[0])
                baseline_id = _stable_id(workspace, suite, batch)
                baseline_sha256 = _eval_baseline_digest(
                    baseline_id=baseline_id,
                    workspace_root=workspace,
                    suite_id=suite,
                    version=version,
                    batch_id=batch,
                    identity_sha256=identity_sha256,
                    sample_count=len(samples),
                    samples_sha256=samples_sha256,
                    promoted_by=actor,
                    promotion_reason=reason,
                    created_at=created,
                )
                cursor = await db.execute(
                    """
                    SELECT baseline_id FROM harness_eval_baseline_selectors
                    WHERE workspace_root = ? AND suite_id = ?
                    """,
                    (workspace, suite),
                )
                selector = await cursor.fetchone()
                previous_id = str(selector["baseline_id"]) if selector else ""
                await db.execute(
                    """
                    INSERT INTO harness_eval_baselines (
                        id, workspace_root, suite_id, version, batch_id,
                        identity_sha256, sample_count, samples_sha256,
                        baseline_sha256, promoted_by, promotion_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        baseline_id,
                        workspace,
                        suite,
                        version,
                        batch,
                        identity_sha256,
                        len(samples),
                        samples_sha256,
                        baseline_sha256,
                        actor,
                        reason,
                        created,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO harness_eval_baseline_selectors (
                        workspace_root, suite_id, baseline_id, updated_at,
                        selector_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(workspace_root, suite_id) DO UPDATE SET
                        baseline_id = excluded.baseline_id,
                        updated_at = excluded.updated_at,
                        selector_sha256 = excluded.selector_sha256
                    """,
                    (
                        workspace,
                        suite,
                        baseline_id,
                        created,
                        _eval_baseline_selector_digest(
                            workspace,
                            suite,
                            baseline_id,
                            created,
                        ),
                    ),
                )
                event_id = _stable_id("baseline_promoted", baseline_id)
                event_sha256 = _eval_baseline_event_digest(
                    event_id=event_id,
                    workspace_root=workspace,
                    suite_id=suite,
                    baseline_id=baseline_id,
                    previous_baseline_id=previous_id,
                    actor=actor,
                    reason=reason,
                    created_at=created,
                )
                await db.execute(
                    """
                    INSERT INTO harness_eval_baseline_events (
                        id, workspace_root, suite_id, baseline_id,
                        previous_baseline_id, actor, reason, created_at,
                        event_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        workspace,
                        suite,
                        baseline_id,
                        previous_id,
                        actor,
                        reason,
                        created,
                        event_sha256,
                    ),
                )
                await db.commit()
        except HarnessStoreConflictError:
            raise
        except ValueError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError("无法晋升 Harness Eval Baseline。") from exc
        restored = await self.get_active_eval_baseline(workspace, suite)
        assert restored is not None
        return restored

    async def get_active_eval_baseline(
        self,
        workspace_root: str | Path,
        suite_id: str,
    ) -> HarnessStoredEvalBaseline | None:
        workspace = _canonical_workspace(workspace_root)
        suite = _normalize_text(suite_id, field="suite_id", max_length=64)
        if not self._db_path.is_file():
            return None
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_baseline_selectors
                    WHERE workspace_root = ? AND suite_id = ?
                    """,
                    (workspace, suite),
                )
                selector = await cursor.fetchone()
                if selector is None:
                    return None
                expected_selector_sha256 = _eval_baseline_selector_digest(
                    workspace,
                    suite,
                    str(selector["baseline_id"]),
                    str(selector["updated_at"]),
                )
                if str(selector["selector_sha256"]) != expected_selector_sha256:
                    raise ValueError("active Eval Baseline selector 摘要与内容不一致。")
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_baselines
                    WHERE id = ? AND workspace_root = ? AND suite_id = ?
                    """,
                    (str(selector["baseline_id"]), workspace, suite),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError("active Eval Baseline selector 引用了越界或缺失版本。")
                baseline = _eval_baseline_from_row(row)
                if baseline.workspace_root != workspace or baseline.suite_id != suite:
                    raise ValueError("active Eval Baseline selector 越过工作区边界。")
                return baseline
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessStoreError("无法读取 active Eval Baseline。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("active Eval Baseline 损坏或无法读取。") from exc

    async def list_eval_baselines(
        self,
        workspace_root: str | Path,
        suite_id: str,
        *,
        limit: int = 100,
    ) -> tuple[HarnessStoredEvalBaseline, ...]:
        workspace = _canonical_workspace(workspace_root)
        suite = _normalize_text(suite_id, field="suite_id", max_length=64)
        if not 1 <= limit <= 1_000:
            raise ValueError("limit 必须在 1..1000 之间。")
        if not self._db_path.is_file():
            return ()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_baselines
                    WHERE workspace_root = ? AND suite_id = ?
                    ORDER BY version DESC LIMIT ?
                    """,
                    (workspace, suite, limit),
                )
                return tuple(
                    _eval_baseline_from_row(row) for row in await cursor.fetchall()
                )
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return ()
            raise HarnessStoreError("无法列出 Eval Baseline。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("Eval Baseline 列表损坏或无法读取。") from exc

    async def get_eval_baseline_by_batch(
        self,
        workspace_root: str | Path,
        suite_id: str,
        batch_id: str,
    ) -> HarnessStoredEvalBaseline | None:
        """Read one exact immutable Baseline version by its source cohort."""
        workspace = _canonical_workspace(workspace_root)
        suite = _normalize_text(suite_id, field="suite_id", max_length=64)
        batch = _normalize_eval_batch_id(batch_id)
        if not self._db_path.is_file():
            return None
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_baselines
                    WHERE workspace_root = ? AND suite_id = ? AND batch_id = ?
                    """,
                    (workspace, suite, batch),
                )
                row = await cursor.fetchone()
                return _eval_baseline_from_row(row) if row is not None else None
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessStoreError("无法读取 Eval Baseline batch。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("Eval Baseline batch 损坏或无法读取。") from exc

    async def get_eval_baseline_event(
        self,
        workspace_root: str | Path,
        suite_id: str,
        baseline_id: str,
    ) -> HarnessStoredEvalBaselineEvent | None:
        """Read the one immutable promotion event for an exact Baseline ID."""
        workspace = _canonical_workspace(workspace_root)
        suite = _normalize_text(suite_id, field="suite_id", max_length=64)
        baseline = _validate_sha256(baseline_id, field="baseline_id")
        if not self._db_path.is_file():
            return None
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_baseline_events
                    WHERE workspace_root = ? AND suite_id = ? AND baseline_id = ?
                    """,
                    (workspace, suite, baseline),
                )
                row = await cursor.fetchone()
                return (
                    _eval_baseline_event_from_row(row)
                    if row is not None
                    else None
                )
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessStoreError("无法读取 Eval Baseline 审计事件。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("Eval Baseline 审计事件损坏或无法读取。") from exc

    async def list_eval_baseline_events(
        self,
        workspace_root: str | Path,
        suite_id: str,
        *,
        limit: int = 100,
    ) -> tuple[HarnessStoredEvalBaselineEvent, ...]:
        workspace = _canonical_workspace(workspace_root)
        suite = _normalize_text(suite_id, field="suite_id", max_length=64)
        if not 1 <= limit <= 1_000:
            raise ValueError("limit 必须在 1..1000 之间。")
        if not self._db_path.is_file():
            return ()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_baseline_events
                    WHERE workspace_root = ? AND suite_id = ?
                    ORDER BY created_at ASC, id ASC LIMIT ?
                    """,
                    (workspace, suite, limit),
                )
                return tuple(
                    _eval_baseline_event_from_row(row)
                    for row in await cursor.fetchall()
                )
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return ()
            raise HarnessStoreError("无法列出 Eval Baseline 审计事件。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("Eval Baseline 审计事件损坏或无法读取。") from exc

    async def record_eval_comparison_receipt(
        self,
        receipt: HarnessEvalComparisonReceipt,
    ) -> HarnessStoredEvalComparisonReceipt:
        """Insert one immutable comparison authority, or return an identical retry."""
        if not isinstance(receipt, HarnessEvalComparisonReceipt):
            raise ValueError("receipt 必须是 HarnessEvalComparisonReceipt。")
        workspace = _canonical_workspace(receipt.workspace_root)
        if workspace != receipt.workspace_root:
            raise ValueError("Comparison receipt workspace_root 必须是规范绝对路径。")
        suite = _normalize_text(receipt.suite_id, field="suite_id", max_length=64)
        current_batch = _normalize_eval_batch_id(receipt.current_batch_id)
        receipt_json = _json_dumps(receipt.model_dump(mode="json"))
        created = _normalize_timestamp(receipt.created_at, field="created_at")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                baseline_cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_baselines
                    WHERE id = ? AND workspace_root = ? AND suite_id = ?
                    """,
                    (receipt.baseline_id, workspace, suite),
                )
                baseline_row = await baseline_cursor.fetchone()
                if baseline_row is None:
                    await db.rollback()
                    raise HarnessStoreConflictError(
                        "Comparison receipt 引用了缺失或越界的 Baseline。"
                    )
                baseline = _eval_baseline_from_row(baseline_row)
                if (
                    baseline.batch_id != receipt.baseline_batch_id
                    or baseline.identity_sha256
                    != receipt.baseline_identity_sha256
                    or baseline.sample_count != receipt.baseline_samples
                    or baseline.samples_sha256
                    != receipt.baseline_samples_sha256
                ):
                    await db.rollback()
                    raise HarnessStoreConflictError(
                        "Comparison receipt 的 Baseline 摘要与已晋升版本不一致。"
                    )
                baseline_samples_cursor = await db.execute(
                    """
                    SELECT *
                    FROM harness_eval_results
                    WHERE workspace_root = ? AND batch_id = ? AND suite_id = ?
                    ORDER BY sample_index ASC
                    """,
                    (workspace, baseline.batch_id, suite),
                )
                baseline_samples = tuple(
                    _eval_result_from_row(row)
                    for row in await baseline_samples_cursor.fetchall()
                )
                _validate_receipt_stored_cohort(
                    samples=baseline_samples,
                    expected_count=receipt.baseline_samples,
                    expected_identity_sha256=receipt.baseline_identity_sha256,
                    expected_samples_sha256=receipt.baseline_samples_sha256,
                    cohort_name="Baseline",
                )
                current_cursor = await db.execute(
                    """
                    SELECT *
                    FROM harness_eval_results
                    WHERE workspace_root = ? AND batch_id = ? AND suite_id = ?
                    ORDER BY sample_index ASC
                    """,
                    (workspace, current_batch, suite),
                )
                current_samples = tuple(
                    _eval_result_from_row(row)
                    for row in await current_cursor.fetchall()
                )
                _validate_receipt_stored_cohort(
                    samples=current_samples,
                    expected_count=receipt.current_samples,
                    expected_identity_sha256=receipt.current_identity_sha256,
                    expected_samples_sha256=receipt.current_samples_sha256,
                    cohort_name="Candidate",
                    evidence_sha256=tuple(
                        item.result_sha256 for item in receipt.sample_evidence
                    ),
                )
                cursor = await db.execute(
                    "SELECT * FROM harness_eval_comparison_receipts WHERE id = ?",
                    (receipt.id,),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    restored = _eval_comparison_receipt_from_row(existing)
                    if restored.receipt_sha256 != receipt.receipt_sha256:
                        await db.rollback()
                        raise HarnessStoreConflictError(
                            "同一 Baseline/Candidate 比较不可覆盖为不同回执。"
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    """
                    INSERT INTO harness_eval_comparison_receipts (
                        id, workspace_root, suite_id, baseline_id,
                        current_batch_id, decision, receipt_sha256,
                        receipt_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.id,
                        workspace,
                        suite,
                        receipt.baseline_id,
                        current_batch,
                        receipt.decision.value,
                        receipt.receipt_sha256,
                        receipt_json,
                        created,
                    ),
                )
                await db.commit()
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法保存 Harness Eval Comparison receipt。") from exc
        restored = await self.get_eval_comparison_receipt(
            workspace,
            suite,
            receipt.baseline_id,
            current_batch,
        )
        assert restored is not None
        return restored

    async def get_eval_comparison_receipt(
        self,
        workspace_root: str | Path,
        suite_id: str,
        baseline_id: str,
        current_batch_id: str,
    ) -> HarnessStoredEvalComparisonReceipt | None:
        """Read one exact workspace-scoped comparison receipt without mutation."""
        workspace = _canonical_workspace(workspace_root)
        suite = _normalize_text(suite_id, field="suite_id", max_length=64)
        baseline = _validate_sha256(baseline_id, field="baseline_id")
        batch = _normalize_eval_batch_id(current_batch_id)
        if not self._db_path.is_file():
            return None
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_eval_comparison_receipts
                    WHERE workspace_root = ? AND suite_id = ?
                      AND baseline_id = ? AND current_batch_id = ?
                    """,
                    (workspace, suite, baseline, batch),
                )
                row = await cursor.fetchone()
                return (
                    _eval_comparison_receipt_from_row(row)
                    if row is not None
                    else None
                )
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessStoreError("无法读取 Eval Comparison receipt。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("Eval Comparison receipt 损坏或无法读取。") from exc

    async def list_eval_comparison_receipts(
        self,
        workspace_root: str | Path,
        suite_id: str,
        *,
        baseline_id: str | None = None,
        limit: int = 100,
    ) -> tuple[HarnessStoredEvalComparisonReceipt, ...]:
        workspace = _canonical_workspace(workspace_root)
        suite = _normalize_text(suite_id, field="suite_id", max_length=64)
        baseline = (
            _validate_sha256(baseline_id, field="baseline_id")
            if baseline_id is not None
            else None
        )
        if not 1 <= limit <= 1_000:
            raise ValueError("limit 必须在 1..1000 之间。")
        if not self._db_path.is_file():
            return ()
        try:
            async with self._connection() as db:
                if baseline is None:
                    cursor = await db.execute(
                        """
                        SELECT * FROM harness_eval_comparison_receipts
                        WHERE workspace_root = ? AND suite_id = ?
                        ORDER BY created_at DESC, id DESC LIMIT ?
                        """,
                        (workspace, suite, limit),
                    )
                else:
                    cursor = await db.execute(
                        """
                        SELECT * FROM harness_eval_comparison_receipts
                        WHERE workspace_root = ? AND suite_id = ? AND baseline_id = ?
                        ORDER BY created_at DESC, id DESC LIMIT ?
                        """,
                        (workspace, suite, baseline, limit),
                    )
                return tuple(
                    _eval_comparison_receipt_from_row(row)
                    for row in await cursor.fetchall()
                )
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return ()
            raise HarnessStoreError("无法列出 Eval Comparison receipt。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("Eval Comparison receipt 列表损坏或无法读取。") from exc

    async def get_run(self, run_id: str) -> HarnessStoredRun | None:
        normalized_run_id = _normalize_text(run_id, field="run_id", max_length=128)
        if not self._db_path.is_file():
            return None
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    "SELECT * FROM harness_runs WHERE id = ?",
                    (normalized_run_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                return await self._run_from_row(db, row)
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessStoreError("无法读取 Harness 运行记录。") from exc
        except (aiosqlite.Error, OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessStoreError("Harness 运行记录损坏或无法读取。") from exc

    async def list_runs(
        self,
        workspace_root: str | Path,
        *,
        limit: int = 20,
    ) -> tuple[HarnessStoredRun, ...]:
        workspace = _canonical_workspace(workspace_root)
        if not 1 <= limit <= 1_000:
            raise ValueError("limit 必须在 1 到 1000 之间。")
        if not self._db_path.is_file():
            return ()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_runs
                    WHERE workspace_root = ?
                    ORDER BY started_at DESC, id DESC
                    LIMIT ?
                    """,
                    (workspace, limit),
                )
                rows = await cursor.fetchall()
                return tuple([await self._run_from_row(db, row) for row in rows])
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return ()
            raise HarnessStoreError("无法列出 Harness 运行记录。") from exc
        except (aiosqlite.Error, OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessStoreError("Harness 运行列表损坏或无法读取。") from exc

    async def list_session_runs(
        self,
        workspace_root: str | Path,
        session_id: str,
        *,
        limit: int = 20,
    ) -> tuple[HarnessStoredRun, ...]:
        """List runs owned by one exact workspace/session boundary."""
        workspace = _canonical_workspace(workspace_root)
        normalized_session_id = _normalize_text(
            session_id,
            field="session_id",
            max_length=256,
        )
        if not 1 <= limit <= 1_000:
            raise ValueError("limit 必须在 1 到 1000 之间。")
        if not self._db_path.is_file():
            return ()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT * FROM harness_runs
                    WHERE workspace_root = ? AND session_id = ?
                    ORDER BY started_at DESC, id DESC
                    LIMIT ?
                    """,
                    (workspace, normalized_session_id, limit),
                )
                rows = await cursor.fetchall()
                return tuple([await self._run_from_row(db, row) for row in rows])
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return ()
            raise HarnessStoreError("无法列出会话关联的 Harness 运行记录。") from exc
        except (aiosqlite.Error, OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessStoreError(
                "会话关联的 Harness 运行列表损坏或无法读取。"
            ) from exc

    async def preview_session_delete(
        self,
        workspace_root: str | Path,
        session_id: str,
    ) -> HarnessSessionDeleteImpact:
        """Count exact rows for a workspace/session without loading row content."""
        workspace = _canonical_workspace(workspace_root)
        normalized_session_id = _normalize_text(
            session_id,
            field="session_id",
            max_length=256,
        )
        empty = HarnessSessionDeleteImpact(
            workspace_root=workspace,
            session_id=normalized_session_id,
        )
        if not self._db_path.is_file():
            return empty
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
                tables = {str(row[0]) for row in await cursor.fetchall()}
                if "harness_runs" not in tables:
                    return empty

                run_filter = (
                    "SELECT id FROM harness_runs "
                    "WHERE workspace_root = ? AND session_id = ?"
                )

                async def count(table: str, extra: str = "") -> int:
                    if table not in tables:
                        return 0
                    query = f"SELECT COUNT(*) FROM {table} WHERE run_id IN ({run_filter})"
                    if extra:
                        query += f" AND ({extra})"
                    result = await db.execute(
                        query,
                        (workspace, normalized_session_id),
                    )
                    row = await result.fetchone()
                    return int(row[0]) if row is not None else 0

                run_cursor = await db.execute(
                    "SELECT COUNT(*) FROM harness_runs "
                    "WHERE workspace_root = ? AND session_id = ?",
                    (workspace, normalized_session_id),
                )
                run_row = await run_cursor.fetchone()
                return HarnessSessionDeleteImpact(
                    workspace_root=workspace,
                    session_id=normalized_session_id,
                    run_count=int(run_row[0]) if run_row is not None else 0,
                    criterion_count=await count("harness_contract_criteria"),
                    check_count=await count("harness_checks"),
                    evidence_count=await count("harness_evidence"),
                    replay_baseline_count=await count("harness_replay_baselines"),
                    check_artifact_reference_count=await count(
                        "harness_checks",
                        "TRIM(artifact_path) <> ''",
                    ),
                    evidence_artifact_reference_count=await count(
                        "harness_evidence",
                        "uri LIKE 'artifact://%'",
                    ),
                )
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError(
                "无法预览会话关联的 Harness 记录；状态库可能损坏或正忙。"
            ) from exc

    async def prepare_session_delete_reconciliation(
        self,
        *,
        request_id: str,
        workspace_root: str | Path,
        session_id: str,
        actor: LifecycleActor | str,
        created_at: str,
    ) -> SessionDeleteReconciliation:
        """Persist immutable scope and artifact references before any deletion."""
        normalized_request_id = _normalize_text(
            request_id,
            field="request_id",
            max_length=128,
        )
        workspace = _canonical_workspace(workspace_root)
        normalized_session_id = _normalize_text(
            session_id,
            field="session_id",
            max_length=256,
        )
        try:
            normalized_actor = actor if isinstance(actor, LifecycleActor) else LifecycleActor(actor)
        except (TypeError, ValueError) as exc:
            raise ValueError("actor 包含未知生命周期操作者。") from exc
        timestamp = _normalize_timestamp(created_at, field="created_at")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                existing = await self._reconciliation_from_id(db, normalized_request_id)
                if existing is not None:
                    if (
                        existing.workspace_root != workspace
                        or existing.session_id != normalized_session_id
                        or existing.actor is not normalized_actor
                    ):
                        raise HarnessStoreConflictError(
                            f"协调请求 {normalized_request_id} 的幂等键已用于其他作用域。"
                        )
                    await db.commit()
                    return existing

                run_cursor = await db.execute(
                    "SELECT COUNT(*) FROM harness_runs "
                    "WHERE workspace_root = ? AND session_id = ?",
                    (workspace, normalized_session_id),
                )
                run_row = await run_cursor.fetchone()
                run_count = int(run_row[0]) if run_row is not None else 0
                reference_cursor = await db.execute(
                    """
                    SELECT 'check_path' AS kind, artifact_path AS value
                    FROM harness_checks
                    WHERE run_id IN (
                        SELECT id FROM harness_runs
                        WHERE workspace_root = ? AND session_id = ?
                    ) AND TRIM(artifact_path) <> ''
                    UNION ALL
                    SELECT 'evidence_uri' AS kind, uri AS value
                    FROM harness_evidence
                    WHERE run_id IN (
                        SELECT id FROM harness_runs
                        WHERE workspace_root = ? AND session_id = ?
                    ) AND uri LIKE 'artifact://%'
                    ORDER BY kind, value
                    """,
                    (
                        workspace,
                        normalized_session_id,
                        workspace,
                        normalized_session_id,
                    ),
                )
                references = tuple(
                    ReconciliationArtifactReference(
                        kind=ReconciliationArtifactKind(str(row["kind"])),
                        value=str(row["value"]),
                    )
                    for row in await reference_cursor.fetchall()
                )
                references_json = json.dumps(
                    [
                        {"kind": reference.kind.value, "value": reference.value}
                        for reference in references
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                await db.execute(
                    """
                    INSERT INTO harness_session_reconciliations (
                        request_id, workspace_root, session_id, actor, state,
                        run_count, deleted_run_count, artifact_references_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        normalized_request_id,
                        workspace,
                        normalized_session_id,
                        normalized_actor.value,
                        SessionReconciliationState.PREPARED.value,
                        run_count,
                        references_json,
                        timestamp,
                        timestamp,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO harness_session_artifact_gc (
                        request_id, status, deleted_count, missing_count,
                        shared_count, unsafe_count, non_file_count, candidate_count,
                        blocked_by_unresolved_live_reference, completed_at, updated_at
                    ) VALUES (?, 'pending', 0, 0, 0, 0, 0, 0, 0, '', ?)
                    """,
                    (normalized_request_id, timestamp),
                )
                await db.commit()
                return SessionDeleteReconciliation(
                    request_id=normalized_request_id,
                    workspace_root=workspace,
                    session_id=normalized_session_id,
                    actor=normalized_actor,
                    state=SessionReconciliationState.PREPARED,
                    run_count=run_count,
                    deleted_run_count=0,
                    artifact_references=references,
                    artifact_gc_status=ReconciliationArtifactGcStatus.PENDING,
                    artifact_candidate_count=0,
                    artifact_deleted_count=0,
                    artifact_missing_count=0,
                    artifact_shared_count=0,
                    artifact_unsafe_count=0,
                    artifact_non_file_count=0,
                    artifact_gc_blocked_by_unresolved_live_reference=False,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessStoreError("无法准备 Session 删除协调记录。") from exc

    async def get_session_delete_reconciliation(
        self,
        request_id: str,
    ) -> SessionDeleteReconciliation | None:
        normalized_request_id = _normalize_text(
            request_id,
            field="request_id",
            max_length=128,
        )
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                return await self._reconciliation_from_id(db, normalized_request_id)
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessStoreError("无法读取 Session 删除协调记录。") from exc
        except (aiosqlite.Error, OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessStoreError("Session 删除协调记录损坏或无法读取。") from exc

    async def get_session_reconciliation_terminal_outcome(
        self,
        request_id: str,
    ) -> SessionReconciliationTerminalOutcome | None:
        """Return an explicit non-delete terminal outcome, if one exists."""
        normalized_request_id = _normalize_text(
            request_id,
            field="request_id",
            max_length=128,
        )
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT outcome FROM harness_session_reconciliation_terminals
                    WHERE request_id = ?
                    """,
                    (normalized_request_id,),
                )
                row = await cursor.fetchone()
                return (
                    SessionReconciliationTerminalOutcome(str(row["outcome"]))
                    if row is not None
                    else None
                )
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法读取 Session 协调终态。") from exc

    async def abort_retention_reconciliation(
        self,
        request_id: str,
        *,
        aborted_at: str,
    ) -> SessionReconciliationTerminalOutcome:
        """Stop a prepared retention delete after its policy becomes invalid."""
        normalized_request_id = _normalize_text(
            request_id,
            field="request_id",
            max_length=128,
        )
        timestamp = _normalize_timestamp(aborted_at, field="aborted_at")
        outcome = SessionReconciliationTerminalOutcome.RETENTION_POLICY_BLOCKED
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                current = await self._require_reconciliation(db, normalized_request_id)
                if (
                    current.actor is not LifecycleActor.RETENTION_WORKER
                    or current.state is not SessionReconciliationState.PREPARED
                ):
                    await db.rollback()
                    raise HarnessStoreConflictError(
                        "只有 prepared 状态的 retention 协调可以因策略变化终止。"
                    )
                existing_cursor = await db.execute(
                    """
                    SELECT outcome FROM harness_session_reconciliation_terminals
                    WHERE request_id = ?
                    """,
                    (normalized_request_id,),
                )
                existing = await existing_cursor.fetchone()
                if existing is not None:
                    await db.commit()
                    return SessionReconciliationTerminalOutcome(
                        str(existing["outcome"])
                    )
                await db.execute(
                    """
                    INSERT INTO harness_session_reconciliation_terminals (
                        request_id, outcome, completed_at
                    ) VALUES (?, ?, ?)
                    """,
                    (normalized_request_id, outcome.value, timestamp),
                )
                await db.execute(
                    """
                    UPDATE harness_session_artifact_gc
                    SET status = 'completed', completed_at = ?, updated_at = ?
                    WHERE request_id = ?
                    """,
                    (timestamp, timestamp, normalized_request_id),
                )
                await db.commit()
                return outcome
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法终止失效的 retention 协调请求。") from exc

    async def acquire_retention_worker_lease(
        self,
        *,
        owner_id: str,
        now: str,
        lease_seconds: int,
    ) -> bool:
        """Atomically acquire or take over the singleton retention lease."""
        owner = _normalize_text(owner_id, field="owner_id", max_length=128)
        timestamp = _normalize_utc_timestamp(now, field="now")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds 必须在 1 到 86400 之间。")
        expires_at = _timestamp_plus_seconds(timestamp, lease_seconds)
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    INSERT INTO harness_retention_worker_leases (
                        lease_name, owner_id, lease_expires_at, updated_at
                    ) VALUES ('session_retention', ?, ?, ?)
                    ON CONFLICT(lease_name) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        lease_expires_at = excluded.lease_expires_at,
                        updated_at = excluded.updated_at
                    WHERE harness_retention_worker_leases.owner_id = excluded.owner_id
                       OR harness_retention_worker_leases.lease_expires_at <= excluded.updated_at
                    """,
                    (owner, expires_at, timestamp),
                )
                await db.commit()
                return cursor.rowcount > 0
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError("无法获取 retention worker 租约。") from exc

    async def renew_retention_worker_lease(
        self,
        *,
        owner_id: str,
        now: str,
        lease_seconds: int,
    ) -> bool:
        """Renew only a non-expired lease still owned by this worker."""
        owner = _normalize_text(owner_id, field="owner_id", max_length=128)
        timestamp = _normalize_utc_timestamp(now, field="now")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds 必须在 1 到 86400 之间。")
        expires_at = _timestamp_plus_seconds(timestamp, lease_seconds)
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    UPDATE harness_retention_worker_leases
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE lease_name = 'session_retention'
                      AND owner_id = ? AND lease_expires_at > ?
                    """,
                    (expires_at, timestamp, owner, timestamp),
                )
                await db.commit()
                return cursor.rowcount > 0
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError("无法续租 retention worker。") from exc

    async def release_retention_worker_lease(self, *, owner_id: str) -> bool:
        """Release only the lease currently owned by this worker."""
        owner = _normalize_text(owner_id, field="owner_id", max_length=128)
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    DELETE FROM harness_retention_worker_leases
                    WHERE lease_name = 'session_retention' AND owner_id = ?
                    """,
                    (owner,),
                )
                await db.commit()
                return cursor.rowcount > 0
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError("无法释放 retention worker 租约。") from exc

    async def acquire_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        owner_id: str,
        now: str,
        lease_seconds: int,
    ) -> HarnessRunLease | None:
        """Atomically acquire a run lease or take over an expired/released epoch."""
        workspace = _canonical_workspace(workspace_root)
        kind = _coerce_run_kind(run_kind)
        normalized_run_id = _normalize_run_lease_id(run_id, field="run_id")
        owner = _normalize_run_lease_id(owner_id, field="owner_id")
        timestamp = _normalize_utc_timestamp(now, field="now")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds 必须在 1 到 86400 之间。")
        expires_at = _timestamp_plus_seconds(timestamp, lease_seconds)
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    INSERT INTO harness_run_leases (
                        workspace_root, run_kind, run_id, owner_id, epoch, state,
                        acquired_at, expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, 'active', ?, ?, ?)
                    ON CONFLICT(workspace_root, run_kind, run_id) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        epoch = CASE
                            WHEN harness_run_leases.state = 'active'
                             AND harness_run_leases.owner_id = excluded.owner_id
                             AND harness_run_leases.expires_at > excluded.updated_at
                            THEN harness_run_leases.epoch
                            ELSE harness_run_leases.epoch + 1
                        END,
                        state = 'active',
                        acquired_at = CASE
                            WHEN harness_run_leases.state = 'active'
                             AND harness_run_leases.owner_id = excluded.owner_id
                             AND harness_run_leases.expires_at > excluded.updated_at
                            THEN harness_run_leases.acquired_at
                            ELSE excluded.acquired_at
                        END,
                        expires_at = CASE
                            WHEN harness_run_leases.state = 'active'
                             AND harness_run_leases.owner_id = excluded.owner_id
                             AND harness_run_leases.expires_at > excluded.updated_at
                             AND harness_run_leases.expires_at > excluded.expires_at
                            THEN harness_run_leases.expires_at
                            ELSE excluded.expires_at
                        END,
                        updated_at = excluded.updated_at
                    WHERE excluded.updated_at >= harness_run_leases.updated_at
                      AND (
                          harness_run_leases.state = 'released'
                          OR harness_run_leases.expires_at <= excluded.updated_at
                          OR harness_run_leases.owner_id = excluded.owner_id
                      )
                    """,
                    (
                        workspace,
                        kind.value,
                        normalized_run_id,
                        owner,
                        timestamp,
                        expires_at,
                        timestamp,
                    ),
                )
                if cursor.rowcount <= 0:
                    await db.rollback()
                    return None
                row = await _select_run_lease_row(
                    db,
                    workspace_root=workspace,
                    run_kind=kind,
                    run_id=normalized_run_id,
                )
                await db.commit()
                assert row is not None
                return _run_lease_from_row(row)
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法获取长周期 Run 租约。") from exc

    async def record_heartbeat(
        self,
        *,
        workspace_root: str | Path,
        subject_kind: HarnessRunKind | str,
        subject_id: str,
        instance_id: str,
        epoch: int,
        sequence: int,
        phase: HarnessHeartbeatPhase | str,
        observed_at: str,
        timeout_seconds: int,
        detail_code: str = "ok",
    ) -> HarnessHeartbeat:
        """Persist a monotonic heartbeat snapshot or reject stale ownership."""
        workspace = _canonical_workspace(workspace_root)
        kind = _coerce_run_kind(subject_kind)
        subject = _normalize_run_lease_id(subject_id, field="subject_id")
        instance = _normalize_run_lease_id(instance_id, field="instance_id")
        normalized_epoch = _normalize_run_epoch(epoch)
        normalized_sequence = _normalize_heartbeat_sequence(sequence)
        normalized_phase = _coerce_heartbeat_phase(phase)
        timestamp = _normalize_utc_timestamp(observed_at, field="observed_at")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 3 <= timeout_seconds <= 86_400
        ):
            raise ValueError("timeout_seconds 必须是 3 到 86400 之间的整数。")
        detail = _normalize_run_lease_id(detail_code, field="detail_code")

        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _select_heartbeat_row(
                    db,
                    workspace_root=workspace,
                    subject_kind=kind,
                    subject_id=subject,
                )
                incoming = HarnessHeartbeat(
                    workspace_root=workspace,
                    subject_kind=kind,
                    subject_id=subject,
                    instance_id=instance,
                    epoch=normalized_epoch,
                    sequence=normalized_sequence,
                    phase=normalized_phase,
                    observed_at=timestamp,
                    timeout_seconds=timeout_seconds,
                    detail_code=detail,
                )
                if row is not None:
                    current = _heartbeat_from_row(row)
                    if incoming == current:
                        await db.commit()
                        return current
                    _validate_heartbeat_advance(current, incoming)
                await db.execute(
                    """
                    INSERT INTO harness_heartbeats (
                        workspace_root, subject_kind, subject_id, instance_id,
                        epoch, sequence, phase, observed_at, timeout_seconds,
                        detail_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workspace_root, subject_kind, subject_id)
                    DO UPDATE SET
                        instance_id = excluded.instance_id,
                        epoch = excluded.epoch,
                        sequence = excluded.sequence,
                        phase = excluded.phase,
                        observed_at = excluded.observed_at,
                        timeout_seconds = excluded.timeout_seconds,
                        detail_code = excluded.detail_code
                    """,
                    (
                        workspace,
                        kind.value,
                        subject,
                        instance,
                        normalized_epoch,
                        normalized_sequence,
                        normalized_phase.value,
                        timestamp,
                        timeout_seconds,
                        detail,
                    ),
                )
                await db.commit()
                return incoming
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法保存 Harness 心跳。") from exc

    async def get_heartbeat(
        self,
        *,
        workspace_root: str | Path,
        subject_kind: HarnessRunKind | str,
        subject_id: str,
    ) -> HarnessHeartbeat | None:
        """Read the latest typed heartbeat for one workspace-scoped subject."""
        workspace = _canonical_workspace(workspace_root)
        kind = _coerce_run_kind(subject_kind)
        subject = _normalize_run_lease_id(subject_id, field="subject_id")
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                row = await _select_heartbeat_row(
                    db,
                    workspace_root=workspace,
                    subject_kind=kind,
                    subject_id=subject,
                )
                return _heartbeat_from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法读取 Harness 心跳。") from exc

    async def enqueue_conversation(
        self,
        *,
        workspace_root: str | Path,
        session_id: str,
        request_id: str,
        client_id: str,
        text: str,
        enqueued_at: str,
    ) -> HarnessConversationQueueItem:
        """Idempotently append one conversation to a bounded durable queue."""
        workspace = _canonical_workspace(workspace_root)
        session = _normalize_text(session_id, field="session_id", max_length=256)
        request = _normalize_conversation_request_id(request_id)
        client = _normalize_text(client_id, field="client_id", max_length=128)
        content = _normalize_conversation_text(text)
        timestamp = _normalize_utc_timestamp(enqueued_at, field="enqueued_at")
        digest = _conversation_queue_digest(
            session_id=session,
            request_id=request,
            text=content,
        )
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                existing = await _select_conversation_queue_item(
                    db,
                    workspace_root=workspace,
                    session_id=session,
                    request_id=request,
                )
                if existing is not None:
                    item = _conversation_queue_item_from_row(existing)
                    if item.text != content:
                        raise HarnessStoreConflictError(
                            f"request_id 已绑定不同排队消息：{request}"
                        )
                    await db.rollback()
                    return item
                count_row = await (
                    await db.execute(
                        """
                        SELECT COUNT(*) AS item_count, COALESCE(MAX(position), 0) AS last_position
                        FROM harness_conversation_queue
                        WHERE workspace_root = ? AND session_id = ? AND state = 'queued'
                        """,
                        (workspace, session),
                    )
                ).fetchone()
                assert count_row is not None
                if int(count_row["item_count"]) >= _MAX_DURABLE_CONVERSATION_QUEUE_ITEMS:
                    raise HarnessStoreConflictError(
                        "排队对话已达到 20 条上限，请等待、取消或提升已有消息。"
                    )
                position = int(count_row["last_position"]) + 1
                await db.execute(
                    """
                    INSERT INTO harness_conversation_queue (
                        workspace_root, session_id, request_id, client_id, text,
                        payload_sha256, state, position, enqueued_at, updated_at,
                        terminal_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, '')
                    """,
                    (
                        workspace,
                        session,
                        request,
                        client,
                        content,
                        digest,
                        position,
                        timestamp,
                        timestamp,
                    ),
                )
                await db.commit()
                return HarnessConversationQueueItem(
                    workspace_root=workspace,
                    session_id=session,
                    request_id=request,
                    client_id=client,
                    text=content,
                    payload_sha256=digest,
                    state="queued",
                    position=position,
                    enqueued_at=timestamp,
                    updated_at=timestamp,
                    terminal_reason="",
                )
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法保存排队对话。请检查用户状态目录权限。") from exc

    async def list_queued_conversations(
        self,
        *,
        workspace_root: str | Path,
        session_id: str,
        limit: int = 20,
    ) -> tuple[HarnessConversationQueueItem, ...]:
        """Read a stable, bounded queue without mutating delivery state."""
        workspace = _canonical_workspace(workspace_root)
        session = _normalize_text(session_id, field="session_id", max_length=256)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("queue limit 必须是 1 到 100 之间的整数。")
        if not self._db_path.is_file():
            return ()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                rows = await (
                    await db.execute(
                        """
                        SELECT * FROM harness_conversation_queue
                        WHERE workspace_root = ? AND session_id = ? AND state = 'queued'
                        ORDER BY position ASC, enqueued_at ASC, request_id ASC
                        LIMIT ?
                        """,
                        (workspace, session, limit),
                    )
                ).fetchall()
                return tuple(_conversation_queue_item_from_row(row) for row in rows)
        except HarnessStoreError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法读取排队对话。") from exc

    async def promote_queued_conversation(
        self,
        *,
        workspace_root: str | Path,
        session_id: str,
        request_id: str,
        updated_at: str,
    ) -> HarnessConversationQueueItem:
        """Move one queued item to the next position while preserving peer order."""
        workspace = _canonical_workspace(workspace_root)
        session = _normalize_text(session_id, field="session_id", max_length=256)
        request = _normalize_conversation_request_id(request_id)
        timestamp = _normalize_utc_timestamp(updated_at, field="updated_at")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                rows = await _select_queued_conversation_rows(
                    db, workspace_root=workspace, session_id=session,
                )
                selected = next(
                    (row for row in rows if str(row["request_id"]) == request),
                    None,
                )
                if selected is None:
                    raise HarnessStoreConflictError(
                        f"排队消息不存在或已离开队列：{request}"
                    )
                current = _conversation_queue_item_from_row(selected)
                _ensure_queue_timestamp_forward(current.updated_at, timestamp)
                ordered = [
                    selected,
                    *(
                        row
                        for row in rows
                        if str(row["request_id"]) != request
                    ),
                ]
                for position, row in enumerate(ordered, start=1):
                    await db.execute(
                        """
                        UPDATE harness_conversation_queue
                        SET position = ?, updated_at = ?
                        WHERE workspace_root = ? AND session_id = ? AND request_id = ?
                        """,
                        (
                            position,
                            timestamp,
                            workspace,
                            session,
                            str(row["request_id"]),
                        ),
                    )
                result_row = await _select_conversation_queue_item(
                    db,
                    workspace_root=workspace,
                    session_id=session,
                    request_id=request,
                )
                await db.commit()
                assert result_row is not None
                return _conversation_queue_item_from_row(result_row)
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法提升排队对话。") from exc

    async def finish_queued_conversation(
        self,
        *,
        workspace_root: str | Path,
        session_id: str,
        request_id: str,
        state: str,
        terminal_reason: str,
        updated_at: str,
        claim_run_id: str = "",
        claim_owner_id: str = "",
        claim_epoch: int | None = None,
    ) -> HarnessConversationQueueItem:
        """Terminalize one item, optionally fencing and releasing its claim."""
        workspace = _canonical_workspace(workspace_root)
        session = _normalize_text(session_id, field="session_id", max_length=256)
        request = _normalize_conversation_request_id(request_id)
        normalized_state = state.strip() if isinstance(state, str) else ""
        if normalized_state not in _CONVERSATION_QUEUE_TERMINAL_STATES:
            raise ValueError("queue state 必须是 completed、cancelled 或 failed。")
        reason = _normalize_text(
            terminal_reason, field="terminal_reason", max_length=256,
        )
        timestamp = _normalize_utc_timestamp(updated_at, field="updated_at")
        claim_values = (claim_run_id, claim_owner_id, claim_epoch)
        has_claim = any(value not in {"", None} for value in claim_values)
        if has_claim and not all(value not in {"", None} for value in claim_values):
            raise ValueError("queue claim 必须同时提供 run_id、owner_id 和 epoch。")
        normalized_claim_run_id = (
            _normalize_run_lease_id(claim_run_id, field="claim_run_id")
            if has_claim else ""
        )
        normalized_claim_owner_id = (
            _normalize_run_lease_id(claim_owner_id, field="claim_owner_id")
            if has_claim else ""
        )
        normalized_claim_epoch = (
            _normalize_run_epoch(claim_epoch)
            if has_claim and claim_epoch is not None else 0
        )
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await _select_conversation_queue_item(
                    db,
                    workspace_root=workspace,
                    session_id=session,
                    request_id=request,
                )
                if row is None:
                    raise HarnessStoreConflictError(f"排队消息不存在：{request}")
                current = _conversation_queue_item_from_row(row)
                if current.state != "queued":
                    if (
                        current.state == normalized_state
                        and current.terminal_reason == reason
                    ):
                        await db.rollback()
                        return current
                    raise HarnessStoreConflictError(
                        f"排队消息已经终结为 {current.state}：{request}"
                    )
                _ensure_queue_timestamp_forward(current.updated_at, timestamp)
                if has_claim:
                    lease_row = await _select_run_lease_row(
                        db,
                        workspace_root=workspace,
                        run_kind=HarnessRunKind.RUNTIME,
                        run_id=normalized_claim_run_id,
                    )
                    decision, fence_reason = _decide_run_fence(
                        lease_row,
                        owner_id=normalized_claim_owner_id,
                        epoch=normalized_claim_epoch,
                        checked_at=timestamp,
                    )
                    if decision is not HarnessRunFenceDecision.ACCEPTED:
                        raise HarnessStoreConflictError(
                            "排队消息 claim 已失效，拒绝提交终态："
                            f"{fence_reason.value}"
                        )
                    operation_id = _stable_id(
                        "conversation_queue_finish",
                        session,
                        request,
                        normalized_state,
                        str(normalized_claim_epoch),
                    )
                    await db.execute(
                        """
                        INSERT INTO harness_run_fence_events (
                            workspace_root, run_kind, run_id, operation_id,
                            presented_owner_id, presented_epoch,
                            active_owner_id, active_epoch,
                            decision, reason, checked_at
                        ) VALUES (?, 'runtime', ?, ?, ?, ?, ?, ?, 'accepted', 'current', ?)
                        """,
                        (
                            workspace,
                            normalized_claim_run_id,
                            operation_id,
                            normalized_claim_owner_id,
                            normalized_claim_epoch,
                            normalized_claim_owner_id,
                            normalized_claim_epoch,
                            timestamp,
                        ),
                    )
                await db.execute(
                    """
                    UPDATE harness_conversation_queue
                    SET state = ?, terminal_reason = ?, updated_at = ?
                    WHERE workspace_root = ? AND session_id = ? AND request_id = ?
                      AND state = 'queued'
                    """,
                    (normalized_state, reason, timestamp, workspace, session, request),
                )
                if has_claim:
                    release_cursor = await db.execute(
                        """
                        UPDATE harness_run_leases
                        SET state = 'released', expires_at = ?, updated_at = ?
                        WHERE workspace_root = ? AND run_kind = 'runtime'
                          AND run_id = ? AND state = 'active'
                          AND owner_id = ? AND epoch = ? AND expires_at > ?
                        """,
                        (
                            timestamp,
                            timestamp,
                            workspace,
                            normalized_claim_run_id,
                            normalized_claim_owner_id,
                            normalized_claim_epoch,
                            timestamp,
                        ),
                    )
                    if release_cursor.rowcount != 1:
                        raise HarnessStoreConflictError(
                            "排队消息 claim 在提交终态时发生并发变化。"
                        )
                remaining = await _select_queued_conversation_rows(
                    db, workspace_root=workspace, session_id=session,
                )
                for position, queued_row in enumerate(remaining, start=1):
                    await db.execute(
                        """
                        UPDATE harness_conversation_queue
                        SET position = ?, updated_at = ?
                        WHERE workspace_root = ? AND session_id = ? AND request_id = ?
                        """,
                        (
                            position,
                            timestamp,
                            workspace,
                            session,
                            str(queued_row["request_id"]),
                        ),
                    )
                result_row = await _select_conversation_queue_item(
                    db,
                    workspace_root=workspace,
                    session_id=session,
                    request_id=request,
                )
                await db.commit()
                assert result_row is not None
                return _conversation_queue_item_from_row(result_row)
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法更新排队对话终态。") from exc

    async def create_interaction(
        self,
        *,
        workspace_root: str | Path,
        record: HarnessInteractionRecord,
    ) -> HarnessInteractionRecord:
        """Persist an immutable interaction identity and its first event."""
        workspace = _canonical_workspace(workspace_root)
        if record.sequence != 1 or record.state != "pending":
            raise ValueError("新 interaction 必须从 pending/sequence=1 开始。")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                existing = await self._get_interaction_with_connection(
                    db, workspace, record.interaction_id,
                )
                if existing is not None:
                    if existing.digest() == record.digest():
                        await db.rollback()
                        return existing
                    raise HarnessStoreConflictError(
                        f"interaction_id 已绑定不同问题：{record.interaction_id}"
                    )
                await db.execute(
                    """
                    INSERT INTO harness_interactions (
                        workspace_root, interaction_id, subject_kind, subject_id,
                        latest_sequence, state, owner_id, owner_epoch,
                        owner_lease_expires_at, expires_at, payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace, record.interaction_id, record.subject_kind,
                        record.subject_id, record.sequence, record.state,
                        record.owner_id, record.owner_epoch,
                        record.owner_lease_expires_at, record.expires_at,
                        record.canonical_json(), record.digest(),
                    ),
                )
                await self._append_interaction_event(db, workspace, record, "")
                await db.commit()
                return record
        except (HarnessStoreConflictError, ValueError):
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError("无法创建持久用户交互。") from exc

    async def get_interaction(
        self,
        *,
        workspace_root: str | Path,
        interaction_id: str,
    ) -> HarnessInteractionRecord | None:
        workspace = _canonical_workspace(workspace_root)
        identifier = _normalize_interaction_id(interaction_id)
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                return await self._get_interaction_with_connection(
                    db, workspace, identifier,
                )
        except HarnessStoreError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法读取持久用户交互。") from exc

    async def list_pending_interactions(
        self,
        *,
        workspace_root: str | Path,
        subject_kind: HarnessRunKind | str | None = None,
        subject_id: str = "",
        limit: int = 50,
    ) -> tuple[HarnessInteractionRecord, ...]:
        """Return bounded pending records without silently changing timeout state."""
        workspace = _canonical_workspace(workspace_root)
        if not 1 <= limit <= 100:
            raise ValueError("interaction limit 必须在 1..100 之间。")
        kind = _coerce_run_kind(subject_kind) if subject_kind is not None else None
        subject = (
            _normalize_run_lease_id(subject_id, field="subject_id")
            if subject_id else ""
        )
        if kind is None and subject:
            raise ValueError("按 subject_id 查询时必须同时提供 subject_kind。")
        if not self._db_path.is_file():
            return ()
        await self._ensure_schema()
        query = (
            "SELECT interaction_id FROM harness_interactions "
            "WHERE workspace_root = ? AND state = 'pending'"
        )
        params: list[object] = [workspace]
        if kind is not None:
            query += " AND subject_kind = ?"
            params.append(kind.value)
        if subject:
            query += " AND subject_id = ?"
            params.append(subject)
        query += " ORDER BY rowid ASC LIMIT ?"
        params.append(limit)
        try:
            async with self._connection() as db:
                rows = await (await db.execute(query, tuple(params))).fetchall()
                records = []
                for row in rows:
                    record = await self._get_interaction_with_connection(
                        db, workspace, str(row["interaction_id"]),
                    )
                    if record is not None:
                        records.append(record)
                return tuple(records)
        except HarnessStoreError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法列出待回答用户交互。") from exc

    async def list_interactions(
        self,
        *,
        workspace_root: str | Path,
        subject_kind: HarnessRunKind | str | None = None,
        subject_ids: Sequence[str] = (),
        limit: int = 50,
    ) -> tuple[HarnessInteractionRecord, ...]:
        """Return a bounded newest-first interaction history without mutation."""
        workspace = _canonical_workspace(workspace_root)
        if not 1 <= limit <= 100:
            raise ValueError("interaction limit 必须在 1..100 之间。")
        kind = _coerce_run_kind(subject_kind) if subject_kind is not None else None
        if len(subject_ids) > 50:
            raise ValueError("interaction subject_ids 最多 50 项。")
        subjects = tuple(dict.fromkeys(
            _normalize_run_lease_id(value, field="subject_id")
            for value in subject_ids
        ))
        if subjects and kind is None:
            raise ValueError("按 subject_ids 查询时必须同时提供 subject_kind。")
        if not self._db_path.is_file():
            return ()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                query = (
                    "SELECT interaction_id FROM harness_interactions "
                    "WHERE workspace_root = ?"
                )
                params: list[object] = [workspace]
                if kind is not None:
                    query += " AND subject_kind = ?"
                    params.append(kind.value)
                if subjects:
                    query += f" AND subject_id IN ({','.join('?' for _ in subjects)})"
                    params.extend(subjects)
                query += " ORDER BY rowid DESC LIMIT ?"
                params.append(limit)
                rows = await (await db.execute(query, tuple(params))).fetchall()
                records = []
                for row in rows:
                    record = await self._get_interaction_with_connection(
                        db, workspace, str(row["interaction_id"]),
                    )
                    if record is not None:
                        records.append(record)
                return tuple(records)
        except HarnessStoreError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法列出持久用户交互历史。") from exc

    async def takeover_interaction(
        self,
        *,
        workspace_root: str | Path,
        interaction_id: str,
        expected_sequence: int,
        owner_id: str,
        now: str,
        owner_lease_seconds: int,
    ) -> HarnessInteractionRecord:
        return await self._transition_interaction(
            workspace_root=workspace_root,
            interaction_id=interaction_id,
            expected_sequence=expected_sequence,
            transition=lambda record: takeover_interaction(
                record,
                owner_id=owner_id,
                now=now,
                owner_lease_seconds=owner_lease_seconds,
            ),
        )

    async def answer_interaction(
        self,
        *,
        workspace_root: str | Path,
        interaction_id: str,
        expected_sequence: int,
        owner_id: str,
        owner_epoch: int,
        response: dict[str, object],
        answered_by: str,
        now: str,
    ) -> HarnessInteractionRecord:
        return await self._transition_interaction(
            workspace_root=workspace_root,
            interaction_id=interaction_id,
            expected_sequence=expected_sequence,
            transition=lambda record: answer_interaction(
                record,
                owner_id=owner_id,
                owner_epoch=owner_epoch,
                response=response,
                answered_by=answered_by,
                now=now,
            ),
        )

    async def expire_interaction(
        self,
        *,
        workspace_root: str | Path,
        interaction_id: str,
        expected_sequence: int,
        now: str,
    ) -> HarnessInteractionRecord:
        return await self._transition_interaction(
            workspace_root=workspace_root,
            interaction_id=interaction_id,
            expected_sequence=expected_sequence,
            transition=lambda record: expire_interaction(record, now=now),
        )

    async def cancel_interaction(
        self,
        *,
        workspace_root: str | Path,
        interaction_id: str,
        expected_sequence: int,
        now: str,
    ) -> HarnessInteractionRecord:
        return await self._transition_interaction(
            workspace_root=workspace_root,
            interaction_id=interaction_id,
            expected_sequence=expected_sequence,
            transition=lambda record: cancel_interaction(record, now=now),
        )

    async def _transition_interaction(
        self,
        *,
        workspace_root: str | Path,
        interaction_id: str,
        expected_sequence: int,
        transition: Callable[[HarnessInteractionRecord], HarnessInteractionRecord],
    ) -> HarnessInteractionRecord:
        workspace = _canonical_workspace(workspace_root)
        identifier = _normalize_interaction_id(interaction_id)
        if expected_sequence < 1:
            raise ValueError("expected_sequence 必须大于或等于 1。")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                current = await self._get_interaction_with_connection(
                    db, workspace, identifier,
                )
                if current is None:
                    raise HarnessStoreConflictError("interaction 不存在，拒绝状态迁移。")
                if current.sequence != expected_sequence:
                    raise HarnessStoreConflictError(
                        "interaction sequence 已变化："
                        f"{expected_sequence} != {current.sequence}。"
                    )
                candidate = transition(current)
                if candidate.interaction_id != current.interaction_id:
                    raise HarnessStoreConflictError("interaction transition 改写了稳定 ID。")
                immutable_fields = (
                    "subject_kind", "subject_id", "session_id", "agent_name",
                    "header", "question", "options", "allow_custom",
                    "custom_label", "created_at", "expires_at",
                )
                if any(
                    getattr(candidate, field) != getattr(current, field)
                    for field in immutable_fields
                ):
                    raise HarnessStoreConflictError(
                        "interaction transition 改写了不可变问题字段。"
                    )
                if candidate.sequence != current.sequence + 1:
                    raise HarnessStoreConflictError(
                        "interaction transition 必须恰好推进一个 sequence。"
                    )
                if candidate.owner_epoch < current.owner_epoch:
                    raise HarnessStoreConflictError(
                        "interaction owner epoch 不能倒退。"
                    )
                previous_digest = current.digest()
                cursor = await db.execute(
                    """
                    UPDATE harness_interactions SET
                        latest_sequence = ?, state = ?, owner_id = ?, owner_epoch = ?,
                        owner_lease_expires_at = ?, expires_at = ?,
                        payload_json = ?, payload_sha256 = ?
                    WHERE workspace_root = ? AND interaction_id = ?
                      AND latest_sequence = ?
                    """,
                    (
                        candidate.sequence, candidate.state, candidate.owner_id,
                        candidate.owner_epoch, candidate.owner_lease_expires_at,
                        candidate.expires_at, candidate.canonical_json(),
                        candidate.digest(), workspace, identifier, expected_sequence,
                    ),
                )
                if cursor.rowcount != 1:
                    raise HarnessStoreConflictError(
                        "interaction 被并发更新，拒绝覆盖。"
                    )
                await self._append_interaction_event(
                    db, workspace, candidate, previous_digest,
                )
                await db.commit()
                return candidate
        except (HarnessStoreConflictError, ValueError):
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError("无法更新持久用户交互。") from exc

    async def _get_interaction_with_connection(
        self,
        db: aiosqlite.Connection,
        workspace: str,
        interaction_id: str,
    ) -> HarnessInteractionRecord | None:
        row = await (await db.execute(
            "SELECT * FROM harness_interactions "
            "WHERE workspace_root = ? AND interaction_id = ?",
            (workspace, interaction_id),
        )).fetchone()
        if row is None:
            return None
        event_rows = await (await db.execute(
            "SELECT * FROM harness_interaction_events "
            "WHERE workspace_root = ? AND interaction_id = ? ORDER BY sequence ASC",
            (workspace, interaction_id),
        )).fetchall()
        previous_digest = ""
        latest: HarnessInteractionRecord | None = None
        for expected, event in enumerate(event_rows, start=1):
            payload = str(event["payload_json"])
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if int(event["sequence"]) != expected:
                raise HarnessStoreError("interaction 事件序号不连续，拒绝读取。")
            if not hmac.compare_digest(digest, str(event["payload_sha256"])):
                raise HarnessStoreError("interaction 事件摘要校验失败，拒绝读取。")
            if not hmac.compare_digest(
                previous_digest, str(event["previous_payload_sha256"])
            ):
                raise HarnessStoreError("interaction 事件哈希链断裂，拒绝读取。")
            latest = HarnessInteractionRecord.model_validate_json(payload)
            if (
                latest.interaction_id != interaction_id
                or latest.sequence != expected
                or latest.state != str(event["state"])
            ):
                raise HarnessStoreError("interaction 事件元数据不一致，拒绝读取。")
            previous_digest = digest
        if latest is None:
            raise HarnessStoreError("interaction 快照存在但事件链为空。")
        payload = str(row["payload_json"])
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        snapshot = HarnessInteractionRecord.model_validate_json(payload)
        if (
            not hmac.compare_digest(digest, str(row["payload_sha256"]))
            or snapshot != latest
            or int(row["latest_sequence"]) != latest.sequence
            or str(row["state"]) != latest.state
            or str(row["subject_kind"]) != latest.subject_kind
            or str(row["subject_id"]) != latest.subject_id
            or str(row["owner_id"]) != latest.owner_id
            or int(row["owner_epoch"]) != latest.owner_epoch
            or str(row["owner_lease_expires_at"])
                != latest.owner_lease_expires_at
            or str(row["expires_at"]) != latest.expires_at
        ):
            raise HarnessStoreError("interaction 快照与事件链末端不一致。")
        return latest

    @staticmethod
    async def _append_interaction_event(
        db: aiosqlite.Connection,
        workspace: str,
        record: HarnessInteractionRecord,
        previous_digest: str,
    ) -> None:
        await db.execute(
            """
            INSERT INTO harness_interaction_events (
                workspace_root, interaction_id, sequence, state,
                payload_json, payload_sha256, previous_payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace, record.interaction_id, record.sequence, record.state,
                record.canonical_json(), record.digest(), previous_digest,
                record.updated_at,
            ),
        )

    async def renew_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        owner_id: str,
        epoch: int,
        now: str,
        lease_seconds: int,
    ) -> HarnessRunLease | None:
        """Renew only the live lease matching the presented owner and epoch."""
        workspace = _canonical_workspace(workspace_root)
        kind = _coerce_run_kind(run_kind)
        normalized_run_id = _normalize_run_lease_id(run_id, field="run_id")
        owner = _normalize_run_lease_id(owner_id, field="owner_id")
        normalized_epoch = _normalize_run_epoch(epoch)
        timestamp = _normalize_utc_timestamp(now, field="now")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds 必须在 1 到 86400 之间。")
        expires_at = _timestamp_plus_seconds(timestamp, lease_seconds)
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    UPDATE harness_run_leases
                    SET expires_at = CASE
                            WHEN expires_at > ? THEN expires_at ELSE ?
                        END,
                        updated_at = ?
                    WHERE workspace_root = ? AND run_kind = ? AND run_id = ?
                      AND state = 'active' AND owner_id = ? AND epoch = ?
                      AND expires_at > ? AND updated_at <= ?
                    """,
                    (
                        expires_at,
                        expires_at,
                        timestamp,
                        workspace,
                        kind.value,
                        normalized_run_id,
                        owner,
                        normalized_epoch,
                        timestamp,
                        timestamp,
                    ),
                )
                if cursor.rowcount <= 0:
                    await db.rollback()
                    return None
                row = await _select_run_lease_row(
                    db,
                    workspace_root=workspace,
                    run_kind=kind,
                    run_id=normalized_run_id,
                )
                await db.commit()
                assert row is not None
                return _run_lease_from_row(row)
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法续租长周期 Run。") from exc

    async def release_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        owner_id: str,
        epoch: int,
        now: str,
    ) -> HarnessRunLease | None:
        """Release a live exact epoch while retaining its monotonic fence history."""
        workspace = _canonical_workspace(workspace_root)
        kind = _coerce_run_kind(run_kind)
        normalized_run_id = _normalize_run_lease_id(run_id, field="run_id")
        owner = _normalize_run_lease_id(owner_id, field="owner_id")
        normalized_epoch = _normalize_run_epoch(epoch)
        timestamp = _normalize_utc_timestamp(now, field="now")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    UPDATE harness_run_leases
                    SET state = 'released', expires_at = ?, updated_at = ?
                    WHERE workspace_root = ? AND run_kind = ? AND run_id = ?
                      AND state = 'active' AND owner_id = ? AND epoch = ?
                      AND expires_at > ? AND updated_at <= ?
                    """,
                    (
                        timestamp,
                        timestamp,
                        workspace,
                        kind.value,
                        normalized_run_id,
                        owner,
                        normalized_epoch,
                        timestamp,
                        timestamp,
                    ),
                )
                if cursor.rowcount <= 0:
                    await db.rollback()
                    return None
                row = await _select_run_lease_row(
                    db,
                    workspace_root=workspace,
                    run_kind=kind,
                    run_id=normalized_run_id,
                )
                await db.commit()
                assert row is not None
                return _run_lease_from_row(row)
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法释放长周期 Run 租约。") from exc

    async def get_run_lease(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
    ) -> HarnessRunLease | None:
        """Read the latest lease row, including a released epoch, without mutation."""
        workspace = _canonical_workspace(workspace_root)
        kind = _coerce_run_kind(run_kind)
        normalized_run_id = _normalize_run_lease_id(run_id, field="run_id")
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                row = await _select_run_lease_row(
                    db,
                    workspace_root=workspace,
                    run_kind=kind,
                    run_id=normalized_run_id,
                )
                return _run_lease_from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法读取长周期 Run 租约。") from exc

    async def record_run_fence_decision(
        self,
        *,
        workspace_root: str | Path,
        run_kind: HarnessRunKind | str,
        run_id: str,
        operation_id: str,
        owner_id: str,
        epoch: int,
        checked_at: str,
    ) -> HarnessRunFenceReceipt:
        """Atomically decide and audit whether a result owns the current epoch."""
        workspace = _canonical_workspace(workspace_root)
        kind = _coerce_run_kind(run_kind)
        normalized_run_id = _normalize_run_lease_id(run_id, field="run_id")
        operation = _normalize_run_lease_id(operation_id, field="operation_id")
        owner = _normalize_run_lease_id(owner_id, field="owner_id")
        normalized_epoch = _normalize_run_epoch(epoch)
        timestamp = _normalize_utc_timestamp(checked_at, field="checked_at")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                existing_cursor = await db.execute(
                    """
                    SELECT * FROM harness_run_fence_events
                    WHERE workspace_root = ? AND run_kind = ?
                      AND run_id = ? AND operation_id = ?
                    """,
                    (workspace, kind.value, normalized_run_id, operation),
                )
                existing = await existing_cursor.fetchone()
                if existing is not None:
                    if (
                        str(existing["presented_owner_id"]) != owner
                        or int(existing["presented_epoch"]) != normalized_epoch
                    ):
                        await db.rollback()
                        raise HarnessStoreConflictError(
                            "operation_id 已用于不同的 Run lease fencing 输入。"
                        )
                    await db.commit()
                    return _run_fence_receipt_from_row(existing)

                lease_row = await _select_run_lease_row(
                    db,
                    workspace_root=workspace,
                    run_kind=kind,
                    run_id=normalized_run_id,
                )
                decision, reason = _decide_run_fence(
                    lease_row,
                    owner_id=owner,
                    epoch=normalized_epoch,
                    checked_at=timestamp,
                )
                active_owner = str(lease_row["owner_id"]) if lease_row else ""
                active_epoch = int(lease_row["epoch"]) if lease_row else 0
                await db.execute(
                    """
                    INSERT INTO harness_run_fence_events (
                        workspace_root, run_kind, run_id, operation_id,
                        presented_owner_id, presented_epoch,
                        active_owner_id, active_epoch,
                        decision, reason, checked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace,
                        kind.value,
                        normalized_run_id,
                        operation,
                        owner,
                        normalized_epoch,
                        active_owner,
                        active_epoch,
                        decision.value,
                        reason.value,
                        timestamp,
                    ),
                )
                await db.commit()
                return HarnessRunFenceReceipt(
                    workspace_root=workspace,
                    run_kind=kind,
                    run_id=normalized_run_id,
                    operation_id=operation,
                    presented_owner_id=owner,
                    presented_epoch=normalized_epoch,
                    active_owner_id=active_owner,
                    active_epoch=active_epoch,
                    decision=decision,
                    reason=reason,
                    checked_at=timestamp,
                )
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法记录长周期 Run fencing 决策。") from exc

    async def list_pending_session_reconciliations(
        self,
        *,
        limit: int = 100,
    ) -> tuple[SessionDeleteReconciliation, ...]:
        """List bounded incomplete requests for restart recovery."""
        if not 1 <= limit <= 1_000:
            raise ValueError("limit 必须在 1 到 1000 之间。")
        if not self._db_path.is_file():
            return ()
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT request_id FROM harness_session_reconciliations
                    WHERE (state <> ? OR request_id IN (
                        SELECT request_id FROM harness_session_artifact_gc
                        WHERE status <> 'completed'
                    )) AND request_id NOT IN (
                        SELECT request_id
                        FROM harness_session_reconciliation_terminals
                    )
                    ORDER BY updated_at, request_id
                    LIMIT ?
                    """,
                    (SessionReconciliationState.RECORDS_COMMITTED.value, limit),
                )
                records: list[SessionDeleteReconciliation] = []
                for row in await cursor.fetchall():
                    record = await self._reconciliation_from_id(
                        db,
                        str(row["request_id"]),
                    )
                    if record is not None:
                        records.append(record)
                return tuple(records)
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return ()
            raise HarnessStoreError("无法列出未完成的 Session 协调记录。") from exc
        except (aiosqlite.Error, OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessStoreError("未完成的 Session 协调记录损坏或无法读取。") from exc

    async def mark_session_delete_committed(
        self,
        request_id: str,
        *,
        updated_at: str,
    ) -> SessionDeleteReconciliation:
        """Confirm authoritative Session deletion before Harness cleanup."""
        return await self._advance_session_reconciliation(
            request_id,
            requested=SessionReconciliationState.SESSION_COMMITTED,
            updated_at=updated_at,
        )

    async def reconcile_session_delete_records(
        self,
        request_id: str,
        *,
        updated_at: str,
    ) -> SessionDeleteReconciliation:
        """Atomically delete scoped Harness rows and commit reconciliation state."""
        normalized_request_id = _normalize_text(
            request_id,
            field="request_id",
            max_length=128,
        )
        timestamp = _normalize_timestamp(updated_at, field="updated_at")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                current = await self._require_reconciliation(db, normalized_request_id)
                idempotent = validate_reconciliation_transition(
                    current.state,
                    SessionReconciliationState.RECORDS_COMMITTED,
                )
                if idempotent:
                    await db.commit()
                    return current
                _ensure_reconciliation_time_forward(current.updated_at, timestamp)
                cursor = await db.execute(
                    "DELETE FROM harness_runs WHERE workspace_root = ? AND session_id = ?",
                    (current.workspace_root, current.session_id),
                )
                deleted = max(cursor.rowcount, 0)
                await db.execute(
                    """
                    UPDATE harness_session_reconciliations
                    SET state = ?, deleted_run_count = ?, updated_at = ?
                    WHERE request_id = ?
                    """,
                    (
                        SessionReconciliationState.RECORDS_COMMITTED.value,
                        deleted,
                        timestamp,
                        normalized_request_id,
                    ),
                )
                await db.commit()
                updated = await self.get_session_delete_reconciliation(
                    normalized_request_id
                )
                assert updated is not None
                return updated
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessStoreError("无法协调 Session 关联的 Harness 记录。") from exc

    async def reconcile_session_artifacts(
        self,
        request_id: str,
        *,
        updated_at: str,
        collector: ArtifactGarbageCollector | None = None,
    ) -> SessionDeleteReconciliation:
        """Delete unshared safe Artifact files and durably commit the GC result."""
        normalized_request_id = _normalize_text(
            request_id,
            field="request_id",
            max_length=128,
        )
        timestamp = _normalize_timestamp(updated_at, field="updated_at")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                current = await self._require_reconciliation(db, normalized_request_id)
                if current.state is not SessionReconciliationState.RECORDS_COMMITTED:
                    raise SessionReconciliationTransitionError(
                        "Harness 记录尚未协调完成，不能清理 Artifact。"
                    )
                if current.artifact_gc_status is ReconciliationArtifactGcStatus.COMPLETED:
                    await db.commit()
                    return current
                _ensure_reconciliation_time_forward(current.updated_at, timestamp)
                active_collector = collector or ArtifactGarbageCollector(
                    current.workspace_root
                )
                plan = active_collector.build_plan(current.artifact_references)
                cursor = await db.execute(
                    """
                    SELECT 'check_path' AS kind, c.artifact_path AS value
                    FROM harness_checks AS c
                    JOIN harness_runs AS r ON r.id = c.run_id
                    WHERE r.workspace_root = ? AND r.session_id <> ?
                        AND TRIM(c.artifact_path) <> ''
                    UNION ALL
                    SELECT 'evidence_uri' AS kind, e.uri AS value
                    FROM harness_evidence AS e
                    JOIN harness_runs AS r ON r.id = e.run_id
                    WHERE r.workspace_root = ? AND r.session_id <> ?
                        AND e.uri LIKE 'artifact://%'
                    ORDER BY kind, value
                    """,
                    (
                        current.workspace_root,
                        current.session_id,
                        current.workspace_root,
                        current.session_id,
                    ),
                )
                while True:
                    rows = await cursor.fetchmany(256)
                    if not rows:
                        break
                    active_collector.observe_surviving_references(
                        plan,
                        tuple(
                            ReconciliationArtifactReference(
                                kind=ReconciliationArtifactKind(str(row["kind"])),
                                value=str(row["value"]),
                            )
                            for row in rows
                        ),
                    )
                result = await asyncio.to_thread(active_collector.execute, plan)
                await db.execute(
                    """
                    UPDATE harness_session_artifact_gc
                    SET status = 'completed', deleted_count = ?, missing_count = ?,
                        shared_count = ?, unsafe_count = ?, non_file_count = ?,
                        candidate_count = ?, blocked_by_unresolved_live_reference = ?,
                        completed_at = ?, updated_at = ?
                    WHERE request_id = ? AND status = 'pending'
                    """,
                    (
                        result.deleted_count,
                        result.missing_count,
                        result.shared_count,
                        result.unsafe_reference_count,
                        result.non_file_count,
                        result.candidate_count,
                        int(result.blocked_by_unresolved_live_reference),
                        timestamp,
                        timestamp,
                        normalized_request_id,
                    ),
                )
                await db.commit()
                updated = await self.get_session_delete_reconciliation(
                    normalized_request_id
                )
                assert updated is not None
                return updated
        except (ArtifactGarbageCollectionError, SessionReconciliationTransitionError):
            raise
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessStoreError("无法协调 Session 关联的 Artifact 文件。") from exc

    async def _advance_session_reconciliation(
        self,
        request_id: str,
        *,
        requested: SessionReconciliationState,
        updated_at: str,
    ) -> SessionDeleteReconciliation:
        normalized_request_id = _normalize_text(
            request_id,
            field="request_id",
            max_length=128,
        )
        timestamp = _normalize_timestamp(updated_at, field="updated_at")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                current = await self._require_reconciliation(db, normalized_request_id)
                idempotent = validate_reconciliation_transition(
                    current.state,
                    requested,
                )
                if idempotent:
                    await db.commit()
                    return current
                _ensure_reconciliation_time_forward(current.updated_at, timestamp)
                await db.execute(
                    """
                    UPDATE harness_session_reconciliations
                    SET state = ?, updated_at = ? WHERE request_id = ?
                    """,
                    (requested.value, timestamp, normalized_request_id),
                )
                await db.commit()
                return SessionDeleteReconciliation(
                    request_id=current.request_id,
                    workspace_root=current.workspace_root,
                    session_id=current.session_id,
                    actor=current.actor,
                    state=requested,
                    run_count=current.run_count,
                    deleted_run_count=current.deleted_run_count,
                    artifact_references=current.artifact_references,
                    artifact_gc_status=current.artifact_gc_status,
                    artifact_candidate_count=current.artifact_candidate_count,
                    artifact_deleted_count=current.artifact_deleted_count,
                    artifact_missing_count=current.artifact_missing_count,
                    artifact_shared_count=current.artifact_shared_count,
                    artifact_unsafe_count=current.artifact_unsafe_count,
                    artifact_non_file_count=current.artifact_non_file_count,
                    artifact_gc_blocked_by_unresolved_live_reference=(
                        current.artifact_gc_blocked_by_unresolved_live_reference
                    ),
                    created_at=current.created_at,
                    updated_at=timestamp,
                )
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessStoreError("无法推进 Session 删除协调状态。") from exc

    async def _require_reconciliation(
        self,
        db: aiosqlite.Connection,
        request_id: str,
    ) -> SessionDeleteReconciliation:
        record = await self._reconciliation_from_id(db, request_id)
        if record is None:
            raise HarnessStoreConflictError(f"协调请求 {request_id} 不存在。")
        return record

    async def _reconciliation_from_id(
        self,
        db: aiosqlite.Connection,
        request_id: str,
    ) -> SessionDeleteReconciliation | None:
        cursor = await db.execute(
            "SELECT * FROM harness_session_reconciliations WHERE request_id = ?",
            (request_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        gc_cursor = await db.execute(
            "SELECT * FROM harness_session_artifact_gc WHERE request_id = ?",
            (request_id,),
        )
        gc_row = await gc_cursor.fetchone()
        if gc_row is None:
            raise ValueError("Session 删除协调记录缺少 Artifact GC 状态。")
        raw_references = json.loads(str(row["artifact_references_json"]))
        if not isinstance(raw_references, list):
            raise ValueError("artifact_references_json 必须是数组。")
        references = tuple(
            ReconciliationArtifactReference(
                kind=ReconciliationArtifactKind(str(item["kind"])),
                value=_normalize_text(
                    item["value"],
                    field="artifact_reference",
                    max_length=4_096,
                ),
            )
            for item in raw_references
            if isinstance(item, dict)
        )
        if len(references) != len(raw_references):
            raise ValueError("artifact_references_json 包含无效记录。")
        return SessionDeleteReconciliation(
            request_id=str(row["request_id"]),
            workspace_root=str(row["workspace_root"]),
            session_id=str(row["session_id"]),
            actor=LifecycleActor(str(row["actor"])),
            state=SessionReconciliationState(str(row["state"])),
            run_count=int(row["run_count"]),
            deleted_run_count=int(row["deleted_run_count"]),
            artifact_references=references,
            artifact_gc_status=ReconciliationArtifactGcStatus(str(gc_row["status"])),
            artifact_candidate_count=int(gc_row["candidate_count"]),
            artifact_deleted_count=int(gc_row["deleted_count"]),
            artifact_missing_count=int(gc_row["missing_count"]),
            artifact_shared_count=int(gc_row["shared_count"]),
            artifact_unsafe_count=int(gc_row["unsafe_count"]),
            artifact_non_file_count=int(gc_row["non_file_count"]),
            artifact_gc_blocked_by_unresolved_live_reference=bool(
                int(gc_row["blocked_by_unresolved_live_reference"])
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    async def record_reconciliation_failure(
        self,
        *,
        request_id: str,
        failure_id: str,
        stage: ReconciliationFailureStage | str,
        error_code: ReconciliationFailureCode | str,
        occurred_at: str,
        max_attempts: int = 8,
        worker_id: str = "",
    ) -> ReconciliationTombstone:
        """Record one idempotent sanitized failure and schedule bounded retry."""
        normalized_request_id = _normalize_text(
            request_id,
            field="request_id",
            max_length=128,
        )
        normalized_failure_id = _normalize_text(
            failure_id,
            field="failure_id",
            max_length=128,
        )
        normalized_stage = _coerce_failure_stage(stage)
        normalized_error_code = _coerce_failure_code(error_code)
        timestamp = _normalize_utc_timestamp(occurred_at, field="occurred_at")
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts 必须在 1 到 100 之间。")
        normalized_worker = (
            _normalize_text(worker_id, field="worker_id", max_length=128)
            if worker_id
            else ""
        )
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                reconciliation = await self._require_reconciliation(
                    db,
                    normalized_request_id,
                )
                reconciliation_updated = _normalize_utc_timestamp(
                    reconciliation.updated_at,
                    field="reconciliation.updated_at",
                )
                if timestamp < reconciliation_updated:
                    raise HarnessStoreConflictError(
                        "失败时间不能早于 reconciliation 当前状态时间。"
                    )
                event_cursor = await db.execute(
                    """
                    SELECT request_id, stage, error_code, occurred_at
                    FROM harness_session_reconciliation_failure_events
                    WHERE failure_id = ?
                    """,
                    (normalized_failure_id,),
                )
                event = await event_cursor.fetchone()
                if event is not None:
                    if (
                        str(event["request_id"]) != normalized_request_id
                        or str(event["stage"]) != normalized_stage.value
                        or str(event["error_code"]) != normalized_error_code.value
                        or str(event["occurred_at"]) != timestamp
                    ):
                        raise HarnessStoreConflictError(
                            f"失败事件 {normalized_failure_id} 的幂等键已用于其他事实。"
                        )
                    existing = await self._require_tombstone(db, normalized_request_id)
                    if existing.max_attempts != max_attempts:
                        raise HarnessStoreConflictError(
                            "同一协调请求不能改变 max_attempts。"
                        )
                    await db.commit()
                    return existing

                current = await self._tombstone_from_id(db, normalized_request_id)
                if current is not None:
                    if current.max_attempts != max_attempts:
                        raise HarnessStoreConflictError(
                            "同一协调请求不能改变 max_attempts。"
                        )
                    if current.status in {
                        ReconciliationTombstoneStatus.EXHAUSTED,
                        ReconciliationTombstoneStatus.RESOLVED,
                    }:
                        raise HarnessStoreConflictError(
                            "已耗尽或已解决的 tombstone 不能记录新失败。"
                        )
                    if timestamp < current.updated_at:
                        raise HarnessStoreConflictError(
                            "失败时间不能早于 tombstone 当前状态时间。"
                        )
                    if current.status is ReconciliationTombstoneStatus.LEASED:
                        if not normalized_worker or current.lease_owner != normalized_worker:
                            raise HarnessStoreConflictError(
                                "只有当前租约持有者能报告重试失败。"
                            )
                        if timestamp >= current.lease_expires_at:
                            raise HarnessStoreConflictError(
                                "租约已过期，旧 worker 不能提交失败结果。"
                            )
                    elif normalized_worker:
                        raise HarnessStoreConflictError(
                            "未领取的 tombstone 不能附带 worker_id。"
                        )
                    attempt_count = current.attempt_count + 1
                    created = current.created_at
                else:
                    if normalized_worker:
                        raise HarnessStoreConflictError(
                            "首次失败尚无可供 worker 持有的租约。"
                        )
                    attempt_count = 1
                    created = timestamp

                exhausted = attempt_count >= max_attempts
                status = (
                    ReconciliationTombstoneStatus.EXHAUSTED
                    if exhausted
                    else ReconciliationTombstoneStatus.PENDING
                )
                next_retry_at = (
                    timestamp
                    if exhausted
                    else _timestamp_plus_seconds(
                        timestamp,
                        compute_retry_delay_seconds(
                            normalized_request_id,
                            attempt_count,
                        ),
                    )
                )
                if current is None:
                    await db.execute(
                        """
                        INSERT INTO harness_session_reconciliation_tombstones (
                            request_id, policy, stage, error_code, status,
                            attempt_count, max_attempts, next_retry_at,
                            lease_owner, lease_expires_at, last_failure_id,
                            created_at, updated_at
                        ) VALUES (?, 'delete', ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?)
                        """,
                        (
                            normalized_request_id,
                            normalized_stage.value,
                            normalized_error_code.value,
                            status.value,
                            attempt_count,
                            max_attempts,
                            next_retry_at,
                            normalized_failure_id,
                            created,
                            timestamp,
                        ),
                    )
                else:
                    await db.execute(
                        """
                        UPDATE harness_session_reconciliation_tombstones
                        SET stage = ?, error_code = ?, status = ?, attempt_count = ?,
                            next_retry_at = ?, lease_owner = '', lease_expires_at = '',
                            last_failure_id = ?, updated_at = ?
                        WHERE request_id = ?
                        """,
                        (
                            normalized_stage.value,
                            normalized_error_code.value,
                            status.value,
                            attempt_count,
                            next_retry_at,
                            normalized_failure_id,
                            timestamp,
                            normalized_request_id,
                        ),
                    )
                await db.execute(
                    """
                    INSERT INTO harness_session_reconciliation_failure_events (
                        failure_id, request_id, stage, error_code, occurred_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_failure_id,
                        normalized_request_id,
                        normalized_stage.value,
                        normalized_error_code.value,
                        timestamp,
                    ),
                )
                await db.commit()
                return ReconciliationTombstone(
                    request_id=normalized_request_id,
                    policy=LifecyclePolicy.DELETE,
                    stage=normalized_stage,
                    error_code=normalized_error_code,
                    status=status,
                    attempt_count=attempt_count,
                    max_attempts=max_attempts,
                    next_retry_at=next_retry_at,
                    lease_owner="",
                    lease_expires_at="",
                    last_failure_id=normalized_failure_id,
                    created_at=created,
                    updated_at=timestamp,
                )
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法记录 Session 协调失败 tombstone。") from exc

    async def get_reconciliation_tombstone(
        self,
        request_id: str,
    ) -> ReconciliationTombstone | None:
        normalized_request_id = _normalize_text(
            request_id,
            field="request_id",
            max_length=128,
        )
        if not self._db_path.is_file():
            return None
        try:
            async with self._connection() as db:
                return await self._tombstone_from_id(db, normalized_request_id)
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessStoreError("无法读取协调 tombstone。") from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("协调 tombstone 损坏或无法读取。") from exc

    async def claim_due_reconciliation_tombstones(
        self,
        *,
        worker_id: str,
        now: str,
        lease_seconds: int,
        limit: int = 20,
    ) -> tuple[ReconciliationTombstone, ...]:
        """Atomically lease due or expired tombstones to one worker."""
        normalized_worker = _normalize_text(
            worker_id,
            field="worker_id",
            max_length=128,
        )
        timestamp = _normalize_utc_timestamp(now, field="now")
        if not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds 必须在 1 到 3600 之间。")
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间。")
        lease_expires_at = _timestamp_plus_seconds(timestamp, lease_seconds)
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """
                    SELECT request_id
                    FROM harness_session_reconciliation_tombstones
                    WHERE (
                        status = 'pending' AND next_retry_at <= ?
                    ) OR (
                        status = 'leased' AND lease_expires_at <= ?
                    )
                    ORDER BY next_retry_at, request_id
                    LIMIT ?
                    """,
                    (timestamp, timestamp, limit),
                )
                request_ids = [str(row["request_id"]) for row in await cursor.fetchall()]
                claimed: list[ReconciliationTombstone] = []
                for claimed_request_id in request_ids:
                    await db.execute(
                        """
                        UPDATE harness_session_reconciliation_tombstones
                        SET status = 'leased', lease_owner = ?, lease_expires_at = ?,
                            updated_at = ?
                        WHERE request_id = ?
                        """,
                        (
                            normalized_worker,
                            lease_expires_at,
                            timestamp,
                            claimed_request_id,
                        ),
                    )
                    record = await self._require_tombstone(db, claimed_request_id)
                    claimed.append(record)
                await db.commit()
                return tuple(claimed)
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法领取到期的协调 tombstone。") from exc

    async def resolve_reconciliation_tombstone(
        self,
        request_id: str,
        *,
        worker_id: str,
        resolved_at: str,
    ) -> ReconciliationTombstone:
        """Resolve one leased tombstone while rejecting stale workers."""
        normalized_request_id = _normalize_text(
            request_id,
            field="request_id",
            max_length=128,
        )
        normalized_worker = _normalize_text(
            worker_id,
            field="worker_id",
            max_length=128,
        )
        timestamp = _normalize_utc_timestamp(resolved_at, field="resolved_at")
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                current = await self._require_tombstone(db, normalized_request_id)
                if current.status is ReconciliationTombstoneStatus.RESOLVED:
                    if current.lease_owner != normalized_worker:
                        raise HarnessStoreConflictError(
                            "只有原租约持有者能幂等重放 resolved。"
                        )
                    await db.commit()
                    return current
                if (
                    current.status is not ReconciliationTombstoneStatus.LEASED
                    or current.lease_owner != normalized_worker
                ):
                    raise HarnessStoreConflictError(
                        "只有当前租约持有者能解决 tombstone。"
                    )
                if timestamp < current.updated_at:
                    raise HarnessStoreConflictError(
                        "解决时间不能早于租约领取时间。"
                    )
                if timestamp >= current.lease_expires_at:
                    raise HarnessStoreConflictError(
                        "租约已过期，旧 worker 不能提交成功结果。"
                    )
                await db.execute(
                    """
                    UPDATE harness_session_reconciliation_tombstones
                    SET status = 'resolved', updated_at = ? WHERE request_id = ?
                    """,
                    (timestamp, normalized_request_id),
                )
                await db.commit()
                return ReconciliationTombstone(
                    request_id=current.request_id,
                    policy=current.policy,
                    stage=current.stage,
                    error_code=current.error_code,
                    status=ReconciliationTombstoneStatus.RESOLVED,
                    attempt_count=current.attempt_count,
                    max_attempts=current.max_attempts,
                    next_retry_at=current.next_retry_at,
                    lease_owner=current.lease_owner,
                    lease_expires_at=current.lease_expires_at,
                    last_failure_id=current.last_failure_id,
                    created_at=current.created_at,
                    updated_at=timestamp,
                )
        except HarnessStoreConflictError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise HarnessStoreError("无法解决协调 tombstone。") from exc

    async def _require_tombstone(
        self,
        db: aiosqlite.Connection,
        request_id: str,
    ) -> ReconciliationTombstone:
        tombstone = await self._tombstone_from_id(db, request_id)
        if tombstone is None:
            raise HarnessStoreConflictError(f"协调 tombstone {request_id} 不存在。")
        return tombstone

    async def _tombstone_from_id(
        self,
        db: aiosqlite.Connection,
        request_id: str,
    ) -> ReconciliationTombstone | None:
        cursor = await db.execute(
            """
            SELECT * FROM harness_session_reconciliation_tombstones
            WHERE request_id = ?
            """,
            (request_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ReconciliationTombstone(
            request_id=str(row["request_id"]),
            policy=LifecyclePolicy(str(row["policy"])),
            stage=ReconciliationFailureStage(str(row["stage"])),
            error_code=ReconciliationFailureCode(str(row["error_code"])),
            status=ReconciliationTombstoneStatus(str(row["status"])),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            next_retry_at=str(row["next_retry_at"]),
            lease_owner=str(row["lease_owner"]),
            lease_expires_at=str(row["lease_expires_at"]),
            last_failure_id=str(row["last_failure_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    async def delete_session_records(
        self,
        workspace_root: str | Path,
        session_id: str,
    ) -> int:
        workspace = _canonical_workspace(workspace_root)
        normalized_session_id = _normalize_text(
            session_id,
            field="session_id",
            max_length=256,
        )
        if not self._db_path.is_file():
            return 0
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "DELETE FROM harness_runs WHERE workspace_root = ? AND session_id = ?",
                    (workspace, normalized_session_id),
                )
                await db.commit()
                return max(cursor.rowcount, 0)
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise HarnessStoreError("无法清理会话关联的 Harness 记录。") from exc
        except (aiosqlite.Error, OSError) as exc:
            raise HarnessStoreError("无法清理会话关联的 Harness 记录。") from exc

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _restrict_permissions(self._db_path.parent, 0o700)
                for attempt in range(5):
                    try:
                        async with self._connection() as db:
                            cursor = await db.execute("PRAGMA user_version")
                            version = int((await cursor.fetchone())[0])
                            if version > HARNESS_STORE_SCHEMA_VERSION:
                                raise HarnessStoreError(
                                    "Harness 数据库版本高于当前程序支持范围，"
                                    "请升级 NaumiAgent。"
                                )
                            await db.execute("PRAGMA journal_mode = WAL")
                            await db.executescript(_SCHEMA_V1)
                            await db.executescript(_SCHEMA_V2)
                            await db.executescript(_SCHEMA_V3)
                            await db.executescript(_SCHEMA_V4)
                            await _migrate_tombstone_stage_v5(db)
                            await db.executescript(_SCHEMA_V5)
                            await db.executescript(_SCHEMA_V6)
                            await db.executescript(_SCHEMA_V7)
                            await db.executescript(_SCHEMA_V8)
                            await db.executescript(_SCHEMA_V9)
                            await db.executescript(_SCHEMA_V10)
                            await db.executescript(_SCHEMA_V11)
                            await db.executescript(_SCHEMA_V12)
                            await db.executescript(_SCHEMA_V13)
                            await db.executescript(_SCHEMA_V14)
                            await db.execute(
                                "PRAGMA user_version = "
                                f"{HARNESS_STORE_SCHEMA_VERSION}"
                            )
                            await db.commit()
                        break
                    except aiosqlite.OperationalError as exc:
                        locked = "locked" in str(exc).lower()
                        if not locked or attempt == 4:
                            raise
                        await asyncio.sleep(0.025 * (2**attempt))
            except HarnessStoreError:
                raise
            except (aiosqlite.Error, OSError) as exc:
                raise HarnessStoreError(
                    "无法初始化 Harness 状态库。请检查用户状态目录权限。"
                ) from exc
            _restrict_permissions(self._db_path, 0o600)
            self._schema_ready = True

    async def _validate_final_contract(
        self,
        db: aiosqlite.Connection,
        *,
        run: aiosqlite.Row,
        contract: HarnessCompletionContract | None,
    ) -> HarnessCompletionContract:
        initial = HarnessCompletionContract.model_validate_json(
            str(run["contract_json"])
        )
        if contract is None:
            return initial
        final_json = _model_json(contract)
        final = HarnessCompletionContract.model_validate_json(final_json)
        expected = initial.model_copy(
            update={
                "task_kind": final.task_kind,
                "required_checks": final.required_checks,
            }
        )
        if final != expected:
            raise HarnessStoreConflictError(
                f"Harness run {initial.run_id} 的最终 contract 改写了不可变字段。"
            )
        await self._verify_criteria_match(db, final)
        return final

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

    async def _require_run(
        self,
        db: aiosqlite.Connection,
        run_id: str,
    ) -> aiosqlite.Row:
        cursor = await db.execute(
            "SELECT * FROM harness_runs WHERE id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HarnessStoreConflictError(f"Harness run {run_id} 不存在。")
        return row

    async def _verify_criteria_match(
        self,
        db: aiosqlite.Connection,
        contract: HarnessCompletionContract,
    ) -> None:
        cursor = await db.execute(
            """
            SELECT criterion_id, description FROM harness_contract_criteria
            WHERE run_id = ? ORDER BY criterion_id
            """,
            (contract.run_id,),
        )
        stored = tuple((str(row[0]), str(row[1])) for row in await cursor.fetchall())
        expected = tuple(
            sorted(
                (criterion.id, criterion.description)
                for criterion in contract.acceptance_criteria
            )
        )
        if stored != expected:
            raise HarnessStoreConflictError(
                f"Harness run {contract.run_id} 的验收条件与本次写入冲突。"
            )

    async def _verify_evidence_criteria(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        criterion_ids: tuple[str, ...],
    ) -> None:
        if not criterion_ids:
            return
        cursor = await db.execute(
            """
            SELECT criterion_id FROM harness_contract_criteria
            WHERE run_id = ?
            """,
            (run_id,),
        )
        available = {str(row[0]) for row in await cursor.fetchall()}
        unknown = tuple(item for item in criterion_ids if item not in available)
        if unknown:
            raise HarnessStoreConflictError(
                "Harness evidence 引用了不存在的验收条件：" + "、".join(unknown)
            )

    async def _run_from_row(
        self,
        db: aiosqlite.Connection,
        row: aiosqlite.Row,
    ) -> HarnessStoredRun:
        run_id = str(row["id"])
        criteria_cursor = await db.execute(
            """
            SELECT * FROM harness_contract_criteria
            WHERE run_id = ? ORDER BY criterion_id
            """,
            (run_id,),
        )
        check_cursor = await db.execute(
            """
            SELECT * FROM harness_checks
            WHERE run_id = ? ORDER BY started_at, id
            """,
            (run_id,),
        )
        evidence_cursor = await db.execute(
            """
            SELECT * FROM harness_evidence
            WHERE run_id = ? ORDER BY created_at, id
            """,
            (run_id,),
        )
        criteria = tuple(
            _criterion_from_row(item) for item in await criteria_cursor.fetchall()
        )
        checks = tuple(_check_from_row(item) for item in await check_cursor.fetchall())
        evidence = tuple(
            _evidence_from_row(item) for item in await evidence_cursor.fetchall()
        )
        receipt_json = str(row["receipt_json"])
        return HarnessStoredRun(
            id=run_id,
            workspace_root=str(row["workspace_root"]),
            session_id=str(row["session_id"]),
            task_id=str(row["task_id"]) or None,
            issue_id=str(row["issue_id"]) or None,
            task_kind=str(row["task_kind"]),
            objective=str(row["objective"]),
            status=str(row["status"]),
            profile_digest=str(row["profile_digest"]) or None,
            tree_fingerprint_before=str(row["tree_fingerprint_before"]),
            tree_fingerprint_after=str(row["tree_fingerprint_after"]),
            started_at=str(row["started_at"]),
            completed_at=str(row["completed_at"]),
            contract=HarnessCompletionContract.model_validate_json(
                str(row["contract_json"])
            ),
            receipt=(
                HarnessCompletionReceipt.model_validate_json(receipt_json)
                if receipt_json
                else None
            ),
            criteria=criteria,
            checks=checks,
            evidence=evidence,
        )


def _criterion_from_row(row: aiosqlite.Row) -> HarnessStoredCriterion:
    return HarnessStoredCriterion(
        id=str(row["criterion_id"]),
        description=str(row["description"]),
        source_kind=str(row["source_kind"]),
        source_ref=str(row["source_ref"]),
        status=str(row["status"]),
        evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
    )


def _check_from_row(row: aiosqlite.Row) -> HarnessStoredCheck:
    return HarnessStoredCheck(
        id=str(row["id"]),
        check_key=str(row["check_key"]),
        argv=tuple(json.loads(str(row["argv_json"]))),
        cwd=str(row["cwd"]),
        status=str(row["status"]),
        exit_code=row["exit_code"],
        duration_ms=int(row["duration_ms"]),
        started_at=str(row["started_at"]),
        completed_at=str(row["completed_at"]),
        tree_fingerprint=str(row["tree_fingerprint"]),
        profile_digest=str(row["profile_digest"]),
        artifact_path=str(row["artifact_path"]),
    )


def _evidence_from_row(row: aiosqlite.Row) -> HarnessStoredEvidence:
    payload = json.loads(str(row["summary_json"]))
    return HarnessStoredEvidence(
        id=str(row["id"]),
        kind=str(row["kind"]),
        uri=str(row["uri"]),
        sha256=str(row["sha256"]),
        description=str(payload["text"]),
        summary=dict(payload["data"]),
        producer=str(row["producer"]),
        created_at=str(row["created_at"]),
        criterion_ids=tuple(json.loads(str(row["criterion_ids_json"]))),
    )


def _replay_baseline_from_row(row: aiosqlite.Row) -> HarnessStoredReplayBaseline:
    baseline = HarnessStoredReplayBaseline(
        run_id=str(row["run_id"]),
        manifest_json=str(row["manifest_json"]),
        manifest_sha256=_validate_sha256(
            str(row["manifest_sha256"]),
            field="manifest_sha256",
        ),
        rule_version=_normalize_text(
            str(row["rule_version"]),
            field="rule_version",
            max_length=64,
        ),
        explanation_json=str(row["explanation_json"]),
        explanation_sha256=_validate_sha256(
            str(row["explanation_sha256"]),
            field="explanation_sha256",
        ),
        created_at=_normalize_timestamp(str(row["created_at"]), field="created_at"),
    )
    _validate_json_object(baseline.manifest_json, field="manifest_json")
    _validate_json_object(baseline.explanation_json, field="explanation_json")
    return baseline


def _eval_result_from_row(row: aiosqlite.Row) -> HarnessStoredEvalResult:
    result_json = str(row["result_json"])
    result_sha256 = _validate_sha256(
        str(row["result_sha256"]),
        field="result_sha256",
    )
    if _stable_digest(result_json) != result_sha256:
        raise ValueError("Eval Result digest 与内容不一致。")
    result = HarnessEvalSuiteResult.model_validate_json(result_json)
    identity_sha256 = str(row["identity_sha256"])
    expected_identity = (
        result.baseline_identity.identity_sha256
        if result.baseline_identity is not None
        else ""
    )
    if identity_sha256 != expected_identity:
        raise ValueError("Eval Result identity 与内容不一致。")
    workspace = _canonical_workspace(str(row["workspace_root"]))
    batch_id = _normalize_eval_batch_id(str(row["batch_id"]))
    suite_id = _normalize_text(str(row["suite_id"]), field="suite_id", max_length=64)
    sample_index = int(row["sample_index"])
    expected_id = _stable_id(workspace, batch_id, suite_id, str(sample_index))
    if str(row["id"]) != expected_id or result.suite_id != suite_id:
        raise ValueError("Eval Result immutable key 与内容不一致。")
    return HarnessStoredEvalResult(
        id=expected_id,
        workspace_root=workspace,
        batch_id=batch_id,
        suite_id=suite_id,
        sample_index=sample_index,
        identity_sha256=identity_sha256,
        result_sha256=result_sha256,
        result=result,
        created_at=_normalize_timestamp(str(row["created_at"]), field="created_at"),
    )


def _validate_baseline_cohort(
    samples: tuple[HarnessStoredEvalResult, ...],
) -> tuple[str, str]:
    if not samples:
        raise ValueError("Eval cohort 为空，不能晋升 Baseline。")
    if [item.sample_index for item in samples] != list(range(len(samples))):
        raise ValueError("Eval cohort sample_index 必须从 0 连续递增。")
    identity_values = {item.identity_sha256 for item in samples}
    if "" in identity_values or len(identity_values) != 1:
        raise ValueError("Eval cohort 缺少统一、可验证的 Identity。")
    for sample in samples:
        result = sample.result
        identity = result.baseline_identity
        if identity is None or result.baseline_identity_code:
            raise ValueError("Eval sample Identity 不可用于 Baseline。")
        if not identity.baseline_eligible:
            raise ValueError("Eval sample 未通过 Baseline eligibility gate。")
        if identity.configuration.repetitions != len(samples):
            raise ValueError("Identity repetitions 与 cohort 样本数不一致。")
        if result.status is not EvalRunStatus.PASSED or not result.cases:
            raise ValueError("Baseline cohort 必须包含非空的全绿 Suite Result。")
        if any(case.status is not EvalCaseStatus.PASSED for case in result.cases):
            raise ValueError("Baseline cohort 含未通过 case。")
        if any(
            guardrail.status is not EvalGuardrailStatus.PASSED
            for case in result.cases
            for guardrail in case.guardrails
        ):
            raise ValueError("Baseline cohort 含未通过或未验证 guardrail。")
    samples_sha256 = _stable_digest(
        _json_dumps(
            [
                {
                    "sample_index": item.sample_index,
                    "result_sha256": item.result_sha256,
                }
                for item in samples
            ]
        )
    )
    return next(iter(identity_values)), samples_sha256


def _eval_baseline_from_row(row: aiosqlite.Row) -> HarnessStoredEvalBaseline:
    workspace = _canonical_workspace(str(row["workspace_root"]))
    suite_id = _normalize_text(str(row["suite_id"]), field="suite_id", max_length=64)
    batch_id = _normalize_eval_batch_id(str(row["batch_id"]))
    expected_id = _stable_id(workspace, suite_id, batch_id)
    version = int(row["version"])
    sample_count = int(row["sample_count"])
    if str(row["id"]) != expected_id or version < 1 or not 1 <= sample_count <= 10_000:
        raise ValueError("Eval Baseline immutable key 或计数无效。")
    identity_sha256 = _validate_sha256(
        str(row["identity_sha256"]),
        field="identity_sha256",
    )
    samples_sha256 = _validate_sha256(
        str(row["samples_sha256"]),
        field="samples_sha256",
    )
    baseline_sha256 = _validate_sha256(
        str(row["baseline_sha256"]),
        field="baseline_sha256",
    )
    promoted_by = _normalize_text(
        str(row["promoted_by"]),
        field="promoted_by",
        max_length=128,
    )
    promotion_reason = _normalize_text(
        str(row["promotion_reason"]),
        field="promotion_reason",
        max_length=2_000,
    )
    created_at = _normalize_timestamp(str(row["created_at"]), field="created_at")
    expected_digest = _eval_baseline_digest(
        baseline_id=expected_id,
        workspace_root=workspace,
        suite_id=suite_id,
        version=version,
        batch_id=batch_id,
        identity_sha256=identity_sha256,
        sample_count=sample_count,
        samples_sha256=samples_sha256,
        promoted_by=promoted_by,
        promotion_reason=promotion_reason,
        created_at=created_at,
    )
    if baseline_sha256 != expected_digest:
        raise ValueError("Eval Baseline digest 与内容不一致。")
    return HarnessStoredEvalBaseline(
        id=expected_id,
        workspace_root=workspace,
        suite_id=suite_id,
        version=version,
        batch_id=batch_id,
        identity_sha256=identity_sha256,
        sample_count=sample_count,
        samples_sha256=samples_sha256,
        baseline_sha256=baseline_sha256,
        promoted_by=promoted_by,
        promotion_reason=promotion_reason,
        created_at=created_at,
    )


def _eval_baseline_digest(
    *,
    baseline_id: str,
    workspace_root: str,
    suite_id: str,
    version: int,
    batch_id: str,
    identity_sha256: str,
    sample_count: int,
    samples_sha256: str,
    promoted_by: str,
    promotion_reason: str,
    created_at: str,
) -> str:
    return _stable_digest(
        _json_dumps(
            {
                "id": baseline_id,
                "workspace_root": workspace_root,
                "suite_id": suite_id,
                "version": version,
                "batch_id": batch_id,
                "identity_sha256": identity_sha256,
                "sample_count": sample_count,
                "samples_sha256": samples_sha256,
                "promoted_by": promoted_by,
                "promotion_reason": promotion_reason,
                "created_at": created_at,
            }
        )
    )


def _eval_baseline_event_from_row(
    row: aiosqlite.Row,
) -> HarnessStoredEvalBaselineEvent:
    event = HarnessStoredEvalBaselineEvent(
        id=_normalize_text(str(row["id"]), field="event_id", max_length=64),
        workspace_root=_canonical_workspace(str(row["workspace_root"])),
        suite_id=_normalize_text(
            str(row["suite_id"]), field="suite_id", max_length=64
        ),
        baseline_id=_normalize_text(
            str(row["baseline_id"]), field="baseline_id", max_length=64
        ),
        previous_baseline_id=str(row["previous_baseline_id"]),
        actor=_normalize_text(str(row["actor"]), field="actor", max_length=128),
        reason=_normalize_text(str(row["reason"]), field="reason", max_length=2_000),
        created_at=_normalize_timestamp(str(row["created_at"]), field="created_at"),
        event_sha256=_validate_sha256(
            str(row["event_sha256"]), field="event_sha256"
        ),
    )
    expected_id = _stable_id("baseline_promoted", event.baseline_id)
    expected_digest = _eval_baseline_event_digest(
        event_id=event.id,
        workspace_root=event.workspace_root,
        suite_id=event.suite_id,
        baseline_id=event.baseline_id,
        previous_baseline_id=event.previous_baseline_id,
        actor=event.actor,
        reason=event.reason,
        created_at=event.created_at,
    )
    if event.id != expected_id or event.event_sha256 != expected_digest:
        raise ValueError("Eval Baseline 审计事件摘要与内容不一致。")
    return event


def _eval_baseline_event_digest(
    *,
    event_id: str,
    workspace_root: str,
    suite_id: str,
    baseline_id: str,
    previous_baseline_id: str,
    actor: str,
    reason: str,
    created_at: str,
) -> str:
    return _stable_digest(
        _json_dumps(
            {
                "id": event_id,
                "workspace_root": workspace_root,
                "suite_id": suite_id,
                "baseline_id": baseline_id,
                "previous_baseline_id": previous_baseline_id,
                "actor": actor,
                "reason": reason,
                "created_at": created_at,
            }
        )
    )


def _eval_baseline_selector_digest(
    workspace_root: str,
    suite_id: str,
    baseline_id: str,
    updated_at: str,
) -> str:
    return _stable_digest(
        _json_dumps(
            {
                "workspace_root": workspace_root,
                "suite_id": suite_id,
                "baseline_id": baseline_id,
                "updated_at": updated_at,
            }
        )
    )


def _validate_receipt_stored_cohort(
    *,
    samples: tuple[HarnessStoredEvalResult, ...],
    expected_count: int,
    expected_identity_sha256: str,
    expected_samples_sha256: str,
    cohort_name: str,
    evidence_sha256: tuple[str, ...] | None = None,
) -> None:
    if len(samples) != expected_count:
        raise HarnessStoreConflictError(
            f"Comparison receipt 的 {cohort_name} 样本数与存储 cohort 不一致。"
        )
    indexes = [sample.sample_index for sample in samples]
    if indexes != list(range(len(samples))):
        raise HarnessStoreConflictError(
            f"Comparison receipt 的 {cohort_name} sample_index 不连续。"
        )
    identities = {sample.identity_sha256 for sample in samples}
    if identities != {expected_identity_sha256}:
        raise HarnessStoreConflictError(
            f"Comparison receipt 的 {cohort_name} Identity 与存储 cohort 不一致。"
        )
    samples_sha256 = _stable_digest(
        _json_dumps(
            [
                {
                    "sample_index": sample.sample_index,
                    "result_sha256": sample.result_sha256,
                }
                for sample in samples
            ]
        )
    )
    if samples_sha256 != expected_samples_sha256:
        raise HarnessStoreConflictError(
            f"Comparison receipt 的 {cohort_name} 摘要与存储 cohort 不一致。"
        )
    if evidence_sha256 is not None and evidence_sha256 != tuple(
        sample.result_sha256 for sample in samples
    ):
        raise HarnessStoreConflictError(
            "Comparison receipt 的逐样本证据与存储 cohort 不一致。"
        )


def _eval_comparison_receipt_from_row(
    row: aiosqlite.Row,
) -> HarnessStoredEvalComparisonReceipt:
    receipt_json = str(row["receipt_json"])
    receipt = HarnessEvalComparisonReceipt.model_validate_json(receipt_json)
    workspace = _canonical_workspace(str(row["workspace_root"]))
    suite = _normalize_text(str(row["suite_id"]), field="suite_id", max_length=64)
    baseline_id = _validate_sha256(str(row["baseline_id"]), field="baseline_id")
    current_batch = _normalize_eval_batch_id(str(row["current_batch_id"]))
    receipt_sha256 = _validate_sha256(
        str(row["receipt_sha256"]),
        field="receipt_sha256",
    )
    created_at = _normalize_timestamp(str(row["created_at"]), field="created_at")
    if (
        str(row["id"]) != receipt.id
        or workspace != receipt.workspace_root
        or suite != receipt.suite_id
        or baseline_id != receipt.baseline_id
        or current_batch != receipt.current_batch_id
        or str(row["decision"]) != receipt.decision.value
        or receipt_sha256 != receipt.receipt_sha256
        or created_at != receipt.created_at
    ):
        raise ValueError("Eval Comparison receipt 行摘要与内容不一致。")
    return HarnessStoredEvalComparisonReceipt(
        id=receipt.id,
        workspace_root=workspace,
        suite_id=suite,
        baseline_id=baseline_id,
        current_batch_id=current_batch,
        decision=receipt.decision.value,
        receipt_sha256=receipt_sha256,
        receipt=receipt,
        created_at=created_at,
    )


def _check_row_payload(row: aiosqlite.Row) -> tuple[Any, ...]:
    return tuple(
        row[field]
        for field in (
            "id",
            "run_id",
            "check_key",
            "argv_json",
            "cwd",
            "status",
            "exit_code",
            "duration_ms",
            "started_at",
            "completed_at",
            "tree_fingerprint",
            "profile_digest",
            "artifact_path",
        )
    )


def _evidence_row_payload(row: aiosqlite.Row) -> tuple[Any, ...]:
    return tuple(
        row[field]
        for field in (
            "id",
            "run_id",
            "kind",
            "uri",
            "sha256",
            "summary_json",
            "producer",
            "created_at",
            "criterion_ids_json",
        )
    )


def _canonical_workspace(workspace_root: str | Path) -> str:
    if isinstance(workspace_root, str) and not workspace_root.strip():
        raise ValueError("workspace_root 不能为空。")
    return str(Path(workspace_root).expanduser().resolve())


def _normalize_conversation_request_id(value: str) -> str:
    normalized = _normalize_text(value, field="request_id", max_length=128)
    if not _CONVERSATION_REQUEST_ID_RE.fullmatch(normalized):
        raise ValueError(
            "request_id 只能包含字母、数字、点、下划线、冒号和连字符。"
        )
    return normalized


def _normalize_conversation_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("排队消息不能为空。")
    if len(value) > 100_000:
        raise ValueError("排队消息长度不能超过 100000。")
    if "\x00" in value:
        raise ValueError("排队消息不能包含 NUL 字符。")
    return value


def _conversation_queue_digest(
    *,
    session_id: str,
    request_id: str,
    text: str,
) -> str:
    payload = _json_dumps({
        "request_id": request_id,
        "session_id": session_id,
        "text": text,
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ensure_queue_timestamp_forward(current: str, requested: str) -> None:
    if datetime.fromisoformat(requested) < datetime.fromisoformat(current):
        raise HarnessStoreConflictError("排队消息 updated_at 不能早于当前记录。")


async def _select_conversation_queue_item(
    db: aiosqlite.Connection,
    *,
    workspace_root: str,
    session_id: str,
    request_id: str,
) -> aiosqlite.Row | None:
    return await (
        await db.execute(
            """
            SELECT * FROM harness_conversation_queue
            WHERE workspace_root = ? AND session_id = ? AND request_id = ?
            """,
            (workspace_root, session_id, request_id),
        )
    ).fetchone()


async def _select_queued_conversation_rows(
    db: aiosqlite.Connection,
    *,
    workspace_root: str,
    session_id: str,
) -> list[aiosqlite.Row]:
    rows = await (
        await db.execute(
            """
            SELECT * FROM harness_conversation_queue
            WHERE workspace_root = ? AND session_id = ? AND state = 'queued'
            ORDER BY position ASC, enqueued_at ASC, request_id ASC
            """,
            (workspace_root, session_id),
        )
    ).fetchall()
    return list(rows)


def _conversation_queue_item_from_row(
    row: aiosqlite.Row,
) -> HarnessConversationQueueItem:
    item = HarnessConversationQueueItem(
        workspace_root=str(row["workspace_root"]),
        session_id=str(row["session_id"]),
        request_id=str(row["request_id"]),
        client_id=str(row["client_id"]),
        text=str(row["text"]),
        payload_sha256=str(row["payload_sha256"]),
        state=str(row["state"]),
        position=int(row["position"]),
        enqueued_at=str(row["enqueued_at"]),
        updated_at=str(row["updated_at"]),
        terminal_reason=str(row["terminal_reason"]),
    )
    expected_digest = _conversation_queue_digest(
        session_id=item.session_id,
        request_id=item.request_id,
        text=item.text,
    )
    legacy_digest = _legacy_conversation_queue_digest(item)
    if not (
        hmac.compare_digest(item.payload_sha256, expected_digest)
        or hmac.compare_digest(item.payload_sha256, legacy_digest)
    ):
        raise HarnessStoreError("排队消息摘要校验失败，拒绝读取。")
    if item.state not in {"queued", *_CONVERSATION_QUEUE_TERMINAL_STATES}:
        raise HarnessStoreError("排队消息状态无效，拒绝读取。")
    if item.position < 1:
        raise HarnessStoreError("排队消息位置无效，拒绝读取。")
    return item


def _legacy_conversation_queue_digest(item: HarnessConversationQueueItem) -> str:
    payload = _json_dumps({
        "client_id": item.client_id,
        "request_id": item.request_id,
        "session_id": item.session_id,
        "text": item.text,
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _coerce_run_kind(value: HarnessRunKind | str) -> HarnessRunKind:
    if isinstance(value, HarnessRunKind):
        return value
    try:
        return HarnessRunKind(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("run_kind 包含未知长周期运行类型。") from exc


def _normalize_run_epoch(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("epoch 必须是大于或等于 1 的整数。")
    return value


def _normalize_heartbeat_sequence(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("sequence 必须是大于或等于 1 的整数。")
    return value


def _coerce_heartbeat_phase(
    value: HarnessHeartbeatPhase | str,
) -> HarnessHeartbeatPhase:
    if isinstance(value, HarnessHeartbeatPhase):
        return value
    try:
        return HarnessHeartbeatPhase(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("phase 包含未知 Heartbeat 阶段。") from exc


def _normalize_run_lease_id(value: str, *, field: str) -> str:
    normalized = _normalize_text(value, field=field, max_length=128)
    if not _RUN_LEASE_ID_RE.fullmatch(normalized):
        raise ValueError(
            f"{field} 只能包含字母、数字、点、下划线、冒号和连字符。"
        )
    return normalized


def _normalize_interaction_id(value: str) -> str:
    normalized = _normalize_text(
        value,
        field="interaction_id",
        max_length=132,
    )
    if not _INTERACTION_ID_RE.fullmatch(normalized):
        raise ValueError("interaction_id 必须是 ask- 前缀的稳定 ID。")
    return normalized


async def _select_run_lease_row(
    db: aiosqlite.Connection,
    *,
    workspace_root: str,
    run_kind: HarnessRunKind,
    run_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        """
        SELECT * FROM harness_run_leases
        WHERE workspace_root = ? AND run_kind = ? AND run_id = ?
        """,
        (workspace_root, run_kind.value, run_id),
    )
    return await cursor.fetchone()


async def _select_heartbeat_row(
    db: aiosqlite.Connection,
    *,
    workspace_root: str,
    subject_kind: HarnessRunKind,
    subject_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        """
        SELECT * FROM harness_heartbeats
        WHERE workspace_root = ? AND subject_kind = ? AND subject_id = ?
        """,
        (workspace_root, subject_kind.value, subject_id),
    )
    return await cursor.fetchone()


def _heartbeat_from_row(row: aiosqlite.Row) -> HarnessHeartbeat:
    return HarnessHeartbeat(
        workspace_root=str(row["workspace_root"]),
        subject_kind=HarnessRunKind(str(row["subject_kind"])),
        subject_id=str(row["subject_id"]),
        instance_id=str(row["instance_id"]),
        epoch=int(row["epoch"]),
        sequence=int(row["sequence"]),
        phase=HarnessHeartbeatPhase(str(row["phase"])),
        observed_at=str(row["observed_at"]),
        timeout_seconds=int(row["timeout_seconds"]),
        detail_code=str(row["detail_code"]),
    )


def _validate_heartbeat_advance(
    current: HarnessHeartbeat,
    incoming: HarnessHeartbeat,
) -> None:
    if incoming.epoch < current.epoch:
        raise HarnessStoreConflictError("Heartbeat epoch 已落后，拒绝覆盖新 owner。")
    if incoming.epoch == current.epoch:
        if incoming.instance_id != current.instance_id:
            raise HarnessStoreConflictError("相同 Heartbeat epoch 不能更换 instance。")
        if incoming.sequence <= current.sequence:
            raise HarnessStoreConflictError("Heartbeat sequence 必须单调递增。")
    if datetime.fromisoformat(incoming.observed_at) < datetime.fromisoformat(
        current.observed_at
    ):
        raise HarnessStoreConflictError("Heartbeat observed_at 不能倒退。")


def _run_lease_from_row(row: aiosqlite.Row) -> HarnessRunLease:
    return HarnessRunLease(
        workspace_root=str(row["workspace_root"]),
        run_kind=HarnessRunKind(str(row["run_kind"])),
        run_id=str(row["run_id"]),
        owner_id=str(row["owner_id"]),
        epoch=int(row["epoch"]),
        state=HarnessRunLeaseState(str(row["state"])),
        acquired_at=str(row["acquired_at"]),
        expires_at=str(row["expires_at"]),
        updated_at=str(row["updated_at"]),
    )


def _run_fence_receipt_from_row(row: aiosqlite.Row) -> HarnessRunFenceReceipt:
    return HarnessRunFenceReceipt(
        workspace_root=str(row["workspace_root"]),
        run_kind=HarnessRunKind(str(row["run_kind"])),
        run_id=str(row["run_id"]),
        operation_id=str(row["operation_id"]),
        presented_owner_id=str(row["presented_owner_id"]),
        presented_epoch=int(row["presented_epoch"]),
        active_owner_id=str(row["active_owner_id"]),
        active_epoch=int(row["active_epoch"]),
        decision=HarnessRunFenceDecision(str(row["decision"])),
        reason=HarnessRunFenceReason(str(row["reason"])),
        checked_at=str(row["checked_at"]),
    )


def _decide_run_fence(
    lease_row: aiosqlite.Row | None,
    *,
    owner_id: str,
    epoch: int,
    checked_at: str,
) -> tuple[HarnessRunFenceDecision, HarnessRunFenceReason]:
    if lease_row is None:
        return HarnessRunFenceDecision.REJECTED, HarnessRunFenceReason.MISSING
    if str(lease_row["state"]) != HarnessRunLeaseState.ACTIVE.value:
        return HarnessRunFenceDecision.REJECTED, HarnessRunFenceReason.RELEASED
    if datetime.fromisoformat(checked_at) < datetime.fromisoformat(
        str(lease_row["updated_at"])
    ):
        return HarnessRunFenceDecision.REJECTED, HarnessRunFenceReason.CLOCK_REGRESSION
    if datetime.fromisoformat(str(lease_row["expires_at"])) <= datetime.fromisoformat(
        checked_at
    ):
        return HarnessRunFenceDecision.REJECTED, HarnessRunFenceReason.EXPIRED
    if str(lease_row["owner_id"]) != owner_id:
        return HarnessRunFenceDecision.REJECTED, HarnessRunFenceReason.OWNER_MISMATCH
    if int(lease_row["epoch"]) != epoch:
        return HarnessRunFenceDecision.REJECTED, HarnessRunFenceReason.EPOCH_MISMATCH
    return HarnessRunFenceDecision.ACCEPTED, HarnessRunFenceReason.CURRENT


def _canonical_cwd(cwd: str | Path, *, workspace_root: str) -> str:
    if isinstance(cwd, str) and not cwd.strip():
        raise ValueError("cwd 不能为空。")
    path = Path(cwd).expanduser().resolve()
    workspace = Path(workspace_root)
    if not path.is_relative_to(workspace):
        raise ValueError("Harness check cwd 必须位于当前工作区内。")
    return str(path)


def _normalize_text(value: str, *, field: str, max_length: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError(f"{field} 不能为空。")
    if len(normalized) > max_length:
        raise ValueError(f"{field} 长度不能超过 {max_length}。")
    if "\x00" in normalized:
        raise ValueError(f"{field} 不能包含 NUL 字符。")
    return normalized


def _normalize_eval_batch_id(value: str) -> str:
    normalized = _normalize_text(value, field="batch_id", max_length=128)
    if not _EVAL_BATCH_ID_RE.fullmatch(normalized):
        raise ValueError("batch_id 只能包含字母、数字、点、下划线、冒号和连字符。")
    return normalized


def _normalize_fingerprint(value: str) -> str:
    return _normalize_text(value, field="tree_fingerprint", max_length=256)


def _validate_sha256(value: str, *, field: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field} 必须是 64 位小写 SHA-256。")
    return normalized


def _ensure_reconciliation_time_forward(current: str, requested: str) -> None:
    if datetime.fromisoformat(requested) < datetime.fromisoformat(current):
        raise SessionReconciliationTransitionError(
            "协调状态 updated_at 不能早于当前记录。"
        )


def _normalize_utc_timestamp(value: str, *, field: str) -> str:
    normalized = _normalize_timestamp(value, field=field)
    return datetime.fromisoformat(normalized).astimezone(UTC).isoformat()


def _timestamp_plus_seconds(value: str, seconds: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


def _coerce_failure_stage(
    value: ReconciliationFailureStage | str,
) -> ReconciliationFailureStage:
    if isinstance(value, ReconciliationFailureStage):
        return value
    try:
        return ReconciliationFailureStage(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("stage 包含未知协调失败阶段。") from exc


def _coerce_failure_code(
    value: ReconciliationFailureCode | str,
) -> ReconciliationFailureCode:
    if isinstance(value, ReconciliationFailureCode):
        return value
    try:
        return ReconciliationFailureCode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("error_code 包含未知协调错误码。") from exc


def _normalize_timestamp(value: str, *, field: str) -> str:
    normalized = _normalize_text(value, field=field, max_length=64)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区偏移。")
    return normalized


def _normalize_optional_timestamp(value: str, *, field: str) -> str:
    if not value:
        return ""
    return _normalize_timestamp(value, field=field)


def _normalize_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError("Harness check argv 必须是非空字符串数组。")
    normalized = tuple(item.strip() if isinstance(item, str) else "" for item in argv)
    if any(not item or "\x00" in item for item in normalized):
        raise ValueError("Harness check argv 不能包含空值或 NUL 字符。")
    if len(normalized) > 256 or sum(len(item) for item in normalized) > 32_768:
        raise ValueError("Harness check argv 过长，无法安全持久化。")
    return _redact_argv(normalized)


def _redact_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    redacted: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if token.startswith("-") and _SECRET_ARG_NAME_RE.search(token):
            if "=" in token:
                name, _, _ = token.partition("=")
                redacted.append(f"{name}=<redacted>")
            else:
                redacted.append(token)
                redact_next = True
            continue
        if "=" in token:
            name, _, _ = token.partition("=")
            if _SECRET_ARG_NAME_RE.search(name):
                redacted.append(f"{name}=<redacted>")
                continue
        if ":" in token:
            name, _, _ = token.partition(":")
            if _SECRET_ARG_NAME_RE.search(name):
                redacted.append(f"{name}: <redacted>")
                continue
        if _BEARER_VALUE_RE.fullmatch(token):
            redacted.append("<redacted>")
            continue
        redacted.append(OutputGuardrail.redact(token))
    return tuple(redacted)


def _normalize_reference(value: str, *, field: str) -> str:
    normalized = _normalize_text(value, field=field, max_length=4_096)
    if "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{field} 不能包含换行。")
    parsed = urlsplit(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} 不能包含认证信息。")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} 不能包含 query 或 fragment。")
    return normalized


def _normalize_optional_reference(value: str, *, field: str) -> str:
    if not value:
        return ""
    return _normalize_reference(value, field=field)


def _normalize_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(summary, Mapping):
        raise ValueError("summary 必须是 JSON object。")

    def inspect(value: Any, *, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("summary 的 key 必须是字符串。")
                normalized_key = key.strip().lower().replace("-", "_")
                if normalized_key in _SENSITIVE_SUMMARY_KEYS:
                    raise ValueError(f"summary 包含敏感字段：{path}{key}")
                inspect(item, path=f"{path}{key}.")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                inspect(item, path=f"{path}{index}.")

    inspect(summary, path="")
    try:
        encoded = _json_dumps(dict(summary))
    except (TypeError, ValueError) as exc:
        raise ValueError("summary 必须只包含 JSON 可序列化值。") from exc
    if len(encoded.encode("utf-8")) > 65_536:
        raise ValueError("summary 不能超过 64 KiB。")
    decoded = _redact_json_value(json.loads(encoded))
    if not isinstance(decoded, dict):
        raise ValueError("summary 必须是 JSON object。")
    return decoded


def _model_json(model: HarnessCompletionContract | HarnessCompletionReceipt) -> str:
    return _json_dumps(_redact_json_value(model.model_dump(mode="json")))


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _redact_sensitive_text(value: str) -> str:
    redacted = OutputGuardrail.redact(value)
    redacted = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        redacted,
    )
    if _BEARER_VALUE_RE.fullmatch(redacted):
        return "<redacted>"
    return redacted


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _stable_id(*parts: str) -> str:
    payload = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_json_object(value: str, *, field: str) -> None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} 必须是有效 JSON。") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} 必须是 JSON object。")


def _restrict_permissions(path: Path, mode: int) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


async def _migrate_tombstone_stage_v5(db: aiosqlite.Connection) -> None:
    """Rebuild v4 CHECK constraints to admit the Artifact GC failure stage."""
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN ("
        "'harness_session_reconciliation_tombstones', "
        "'harness_session_reconciliation_failure_events')"
    )
    rows = await cursor.fetchall()
    if len(rows) == 2 and all("artifact_gc" in str(row["sql"]) for row in rows):
        return
    await db.executescript(
        """
        BEGIN IMMEDIATE;
        DROP INDEX IF EXISTS idx_harness_reconciliation_tombstones_due;
        DROP INDEX IF EXISTS idx_harness_reconciliation_failure_events_request;
        ALTER TABLE harness_session_reconciliation_tombstones
            RENAME TO harness_session_reconciliation_tombstones_v4;
        ALTER TABLE harness_session_reconciliation_failure_events
            RENAME TO harness_session_reconciliation_failure_events_v4;

        CREATE TABLE harness_session_reconciliation_tombstones (
            request_id TEXT PRIMARY KEY
                REFERENCES harness_session_reconciliations(request_id)
                ON DELETE RESTRICT,
            policy TEXT NOT NULL CHECK (policy = 'delete'),
            stage TEXT NOT NULL CHECK (
                stage IN ('session_delete', 'harness_records', 'artifact_gc')
            ),
            error_code TEXT NOT NULL CHECK (
                error_code IN (
                    'session_store_error', 'harness_store_error',
                    'cancelled', 'infrastructure_error'
                )
            ),
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'leased', 'exhausted', 'resolved')
            ),
            attempt_count INTEGER NOT NULL CHECK (attempt_count >= 1),
            max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
            next_retry_at TEXT NOT NULL,
            lease_owner TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            last_failure_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO harness_session_reconciliation_tombstones
        SELECT * FROM harness_session_reconciliation_tombstones_v4;

        CREATE TABLE harness_session_reconciliation_failure_events (
            failure_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL
                REFERENCES harness_session_reconciliations(request_id)
                ON DELETE RESTRICT,
            stage TEXT NOT NULL CHECK (
                stage IN ('session_delete', 'harness_records', 'artifact_gc')
            ),
            error_code TEXT NOT NULL CHECK (
                error_code IN (
                    'session_store_error', 'harness_store_error',
                    'cancelled', 'infrastructure_error'
                )
            ),
            occurred_at TEXT NOT NULL
        );
        INSERT INTO harness_session_reconciliation_failure_events
        SELECT * FROM harness_session_reconciliation_failure_events_v4;

        DROP TABLE harness_session_reconciliation_failure_events_v4;
        DROP TABLE harness_session_reconciliation_tombstones_v4;
        CREATE INDEX idx_harness_reconciliation_tombstones_due
        ON harness_session_reconciliation_tombstones (
            status, next_retry_at, lease_expires_at, request_id
        );
        CREATE INDEX idx_harness_reconciliation_failure_events_request
        ON harness_session_reconciliation_failure_events (
            request_id, occurred_at, failure_id
        );
        COMMIT;
        """
    )


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS harness_profiles (
    workspace_root TEXT NOT NULL,
    profile_digest TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    loaded_at TEXT NOT NULL,
    trusted_at TEXT NOT NULL,
    trust_source TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (workspace_root, profile_digest)
);

CREATE TABLE IF NOT EXISTS harness_runs (
    id TEXT PRIMARY KEY,
    workspace_root TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    task_kind TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    profile_digest TEXT NOT NULL,
    tree_fingerprint_before TEXT NOT NULL,
    tree_fingerprint_after TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_runs_workspace_started
ON harness_runs (workspace_root, started_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_harness_runs_session
ON harness_runs (session_id);

CREATE INDEX IF NOT EXISTS idx_harness_runs_workspace_session_started
ON harness_runs (workspace_root, session_id, started_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS harness_contract_criteria (
    run_id TEXT NOT NULL REFERENCES harness_runs(id) ON DELETE CASCADE,
    criterion_id TEXT NOT NULL,
    description TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    PRIMARY KEY (run_id, criterion_id)
);

CREATE TABLE IF NOT EXISTS harness_checks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES harness_runs(id) ON DELETE CASCADE,
    check_key TEXT NOT NULL,
    argv_json TEXT NOT NULL,
    cwd TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    duration_ms INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    tree_fingerprint TEXT NOT NULL,
    profile_digest TEXT NOT NULL,
    artifact_path TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_checks_run_started
ON harness_checks (run_id, started_at, id);

CREATE TABLE IF NOT EXISTS harness_evidence (
    id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES harness_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    producer TEXT NOT NULL,
    created_at TEXT NOT NULL,
    criterion_ids_json TEXT NOT NULL,
    PRIMARY KEY (run_id, id)
);

CREATE INDEX IF NOT EXISTS idx_harness_evidence_run_created
ON harness_evidence (run_id, created_at, id);
"""

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS harness_replay_baselines (
    run_id TEXT PRIMARY KEY REFERENCES harness_runs(id) ON DELETE CASCADE,
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    explanation_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS harness_session_reconciliations (
    request_id TEXT PRIMARY KEY,
    workspace_root TEXT NOT NULL,
    session_id TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (actor IN ('user', 'retention_worker', 'system_recovery')),
    state TEXT NOT NULL CHECK (
        state IN ('prepared', 'session_committed', 'records_committed')
    ),
    run_count INTEGER NOT NULL CHECK (run_count >= 0),
    deleted_run_count INTEGER NOT NULL CHECK (deleted_run_count >= 0),
    artifact_references_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_session_reconciliations_state_updated
ON harness_session_reconciliations (state, updated_at, request_id);
"""

_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS harness_session_reconciliation_tombstones (
    request_id TEXT PRIMARY KEY
        REFERENCES harness_session_reconciliations(request_id) ON DELETE RESTRICT,
    policy TEXT NOT NULL CHECK (policy = 'delete'),
    stage TEXT NOT NULL CHECK (
        stage IN ('session_delete', 'harness_records', 'artifact_gc')
    ),
    error_code TEXT NOT NULL CHECK (
        error_code IN (
            'session_store_error', 'harness_store_error',
            'cancelled', 'infrastructure_error'
        )
    ),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'leased', 'exhausted', 'resolved')
    ),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 1),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    next_retry_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    last_failure_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_reconciliation_tombstones_due
ON harness_session_reconciliation_tombstones (
    status, next_retry_at, lease_expires_at, request_id
);

CREATE TABLE IF NOT EXISTS harness_session_reconciliation_failure_events (
    failure_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL
        REFERENCES harness_session_reconciliations(request_id) ON DELETE RESTRICT,
    stage TEXT NOT NULL CHECK (
        stage IN ('session_delete', 'harness_records', 'artifact_gc')
    ),
    error_code TEXT NOT NULL CHECK (
        error_code IN (
            'session_store_error', 'harness_store_error',
            'cancelled', 'infrastructure_error'
        )
    ),
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_reconciliation_failure_events_request
ON harness_session_reconciliation_failure_events (request_id, occurred_at, failure_id);
"""

_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS harness_session_artifact_gc (
    request_id TEXT PRIMARY KEY
        REFERENCES harness_session_reconciliations(request_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed')),
    deleted_count INTEGER NOT NULL CHECK (deleted_count >= 0),
    missing_count INTEGER NOT NULL CHECK (missing_count >= 0),
    shared_count INTEGER NOT NULL CHECK (shared_count >= 0),
    unsafe_count INTEGER NOT NULL CHECK (unsafe_count >= 0),
    non_file_count INTEGER NOT NULL CHECK (non_file_count >= 0),
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    blocked_by_unresolved_live_reference INTEGER NOT NULL CHECK (
        blocked_by_unresolved_live_reference IN (0, 1)
    ),
    completed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO harness_session_artifact_gc (
    request_id, status, deleted_count, missing_count, shared_count,
    unsafe_count, non_file_count, candidate_count,
    blocked_by_unresolved_live_reference, completed_at, updated_at
)
SELECT request_id, 'pending', 0, 0, 0, 0, 0, 0, 0, '', updated_at
FROM harness_session_reconciliations;

CREATE INDEX IF NOT EXISTS idx_harness_session_artifact_gc_status_updated
ON harness_session_artifact_gc (status, updated_at, request_id);
"""

_SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS harness_session_reconciliation_terminals (
    request_id TEXT PRIMARY KEY
        REFERENCES harness_session_reconciliations(request_id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL CHECK (outcome IN ('retention_policy_blocked')),
    completed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_session_reconciliation_terminals_outcome
ON harness_session_reconciliation_terminals (outcome, completed_at, request_id);
"""

_SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS harness_retention_worker_leases (
    lease_name TEXT PRIMARY KEY CHECK (lease_name = 'session_retention'),
    owner_id TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS harness_eval_results (
    id TEXT PRIMARY KEY,
    workspace_root TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    suite_id TEXT NOT NULL,
    sample_index INTEGER NOT NULL CHECK (sample_index BETWEEN 0 AND 9999),
    identity_sha256 TEXT NOT NULL,
    result_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workspace_root, batch_id, suite_id, sample_index)
);

CREATE INDEX IF NOT EXISTS idx_harness_eval_results_cohort
ON harness_eval_results (
    workspace_root, batch_id, suite_id, sample_index
);
"""

_SCHEMA_V9 = """
CREATE TABLE IF NOT EXISTS harness_eval_baselines (
    id TEXT PRIMARY KEY,
    workspace_root TEXT NOT NULL,
    suite_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    batch_id TEXT NOT NULL,
    identity_sha256 TEXT NOT NULL,
    sample_count INTEGER NOT NULL CHECK (sample_count BETWEEN 1 AND 10000),
    samples_sha256 TEXT NOT NULL,
    baseline_sha256 TEXT NOT NULL,
    promoted_by TEXT NOT NULL,
    promotion_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workspace_root, suite_id, version),
    UNIQUE (workspace_root, suite_id, batch_id)
);

CREATE TABLE IF NOT EXISTS harness_eval_baseline_selectors (
    workspace_root TEXT NOT NULL,
    suite_id TEXT NOT NULL,
    baseline_id TEXT NOT NULL
        REFERENCES harness_eval_baselines(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    selector_sha256 TEXT NOT NULL,
    PRIMARY KEY (workspace_root, suite_id)
);

CREATE TABLE IF NOT EXISTS harness_eval_baseline_events (
    id TEXT PRIMARY KEY,
    workspace_root TEXT NOT NULL,
    suite_id TEXT NOT NULL,
    baseline_id TEXT NOT NULL
        REFERENCES harness_eval_baselines(id) ON DELETE RESTRICT,
    previous_baseline_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    event_sha256 TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harness_eval_baselines_versions
ON harness_eval_baselines (workspace_root, suite_id, version DESC);

CREATE INDEX IF NOT EXISTS idx_harness_eval_baseline_events_suite
ON harness_eval_baseline_events (workspace_root, suite_id, created_at, id);
"""

_SCHEMA_V10 = """
CREATE TABLE IF NOT EXISTS harness_eval_comparison_receipts (
    id TEXT PRIMARY KEY,
    workspace_root TEXT NOT NULL,
    suite_id TEXT NOT NULL,
    baseline_id TEXT NOT NULL
        REFERENCES harness_eval_baselines(id) ON DELETE RESTRICT,
    current_batch_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (
        decision IN ('passed', 'failed', 'flaky', 'inconclusive', 'incompatible')
    ),
    receipt_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workspace_root, suite_id, baseline_id, current_batch_id)
);

CREATE INDEX IF NOT EXISTS idx_harness_eval_comparison_receipts_suite
ON harness_eval_comparison_receipts (
    workspace_root, suite_id, created_at DESC, id DESC
);
"""

_SCHEMA_V11 = """
CREATE TABLE IF NOT EXISTS harness_run_leases (
    workspace_root TEXT NOT NULL,
    run_kind TEXT NOT NULL CHECK (
        run_kind IN ('pursuit', 'tool', 'browser', 'agent', 'runtime')
    ),
    run_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    state TEXT NOT NULL CHECK (state IN ('active', 'released')),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, run_kind, run_id)
);

CREATE INDEX IF NOT EXISTS idx_harness_run_leases_expiry
ON harness_run_leases (state, expires_at, workspace_root, run_kind, run_id);

CREATE TABLE IF NOT EXISTS harness_run_fence_events (
    workspace_root TEXT NOT NULL,
    run_kind TEXT NOT NULL CHECK (
        run_kind IN ('pursuit', 'tool', 'browser', 'agent', 'runtime')
    ),
    run_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    presented_owner_id TEXT NOT NULL,
    presented_epoch INTEGER NOT NULL CHECK (presented_epoch >= 1),
    active_owner_id TEXT NOT NULL,
    active_epoch INTEGER NOT NULL CHECK (active_epoch >= 0),
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    reason TEXT NOT NULL CHECK (
        reason IN (
            'current', 'missing', 'released', 'clock_regression', 'expired',
            'owner_mismatch', 'epoch_mismatch'
        )
    ),
    checked_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, run_kind, run_id, operation_id)
);

CREATE INDEX IF NOT EXISTS idx_harness_run_fence_events_checked
ON harness_run_fence_events (
    workspace_root, run_kind, run_id, checked_at, operation_id
);
"""

_SCHEMA_V12 = """
CREATE TABLE IF NOT EXISTS harness_heartbeats (
    workspace_root TEXT NOT NULL,
    subject_kind TEXT NOT NULL CHECK (
        subject_kind IN ('pursuit', 'tool', 'browser', 'agent', 'runtime')
    ),
    subject_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    phase TEXT NOT NULL CHECK (
        phase IN ('starting', 'running', 'waiting', 'draining', 'stopped', 'failed')
    ),
    observed_at TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL CHECK (
        timeout_seconds BETWEEN 3 AND 86400
    ),
    detail_code TEXT NOT NULL,
    PRIMARY KEY (workspace_root, subject_kind, subject_id)
);

CREATE INDEX IF NOT EXISTS idx_harness_heartbeats_observed
ON harness_heartbeats (workspace_root, subject_kind, observed_at, subject_id);
"""

_SCHEMA_V13 = """
CREATE TABLE IF NOT EXISTS harness_interactions (
    workspace_root TEXT NOT NULL,
    interaction_id TEXT NOT NULL,
    subject_kind TEXT NOT NULL CHECK (
        subject_kind IN ('pursuit', 'tool', 'browser', 'agent', 'runtime')
    ),
    subject_id TEXT NOT NULL,
    latest_sequence INTEGER NOT NULL CHECK (latest_sequence >= 1),
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'answered', 'expired', 'cancelled')
    ),
    owner_id TEXT NOT NULL,
    owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
    owner_lease_expires_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY (workspace_root, interaction_id)
);

CREATE INDEX IF NOT EXISTS idx_harness_interactions_pending
ON harness_interactions (
    workspace_root, state, subject_kind, subject_id, interaction_id
);

CREATE TABLE IF NOT EXISTS harness_interaction_events (
    workspace_root TEXT NOT NULL,
    interaction_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'answered', 'expired', 'cancelled')
    ),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    previous_payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, interaction_id, sequence),
    FOREIGN KEY (workspace_root, interaction_id)
        REFERENCES harness_interactions(workspace_root, interaction_id)
        ON DELETE CASCADE
);
"""

_SCHEMA_V14 = """
CREATE TABLE IF NOT EXISTS harness_conversation_queue (
    workspace_root TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    text TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'completed', 'cancelled', 'failed')
    ),
    position INTEGER NOT NULL CHECK (position >= 1),
    enqueued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    terminal_reason TEXT NOT NULL,
    PRIMARY KEY (workspace_root, session_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_harness_conversation_queue_ready
ON harness_conversation_queue (
    workspace_root, session_id, state, position, enqueued_at, request_id
);
"""
