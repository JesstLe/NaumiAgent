"""Durable Harness profiles, completion runs, checks, and evidence metadata."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite

from naumi_agent.harness.checks import HarnessCheckResult
from naumi_agent.harness.completion import (
    HarnessCompletionReceipt,
    HarnessEvidenceRef,
)
from naumi_agent.harness.models import HarnessCompletionContract
from naumi_agent.harness.reconciliation import (
    ReconciliationArtifactKind,
    ReconciliationArtifactReference,
    SessionDeleteReconciliation,
    SessionReconciliationState,
    SessionReconciliationTransitionError,
    validate_reconciliation_transition,
)
from naumi_agent.harness.replay_models import HarnessReplayBaselinePayload
from naumi_agent.harness.retention import LifecycleActor
from naumi_agent.harness.trust import resolve_harness_trust_db_path
from naumi_agent.safety.guardrails import OutputGuardrail

HARNESS_STORE_SCHEMA_VERSION = 3
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
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
        try:
            async with self._connection() as db:
                return await self._reconciliation_from_id(db, normalized_request_id)
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise HarnessStoreError("无法读取 Session 删除协调记录。") from exc
        except (aiosqlite.Error, OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessStoreError("Session 删除协调记录损坏或无法读取。") from exc

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
        try:
            async with self._connection() as db:
                cursor = await db.execute(
                    """
                    SELECT request_id FROM harness_session_reconciliations
                    WHERE state <> ?
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
                async with self._connection() as db:
                    cursor = await db.execute("PRAGMA user_version")
                    version = int((await cursor.fetchone())[0])
                    if version > HARNESS_STORE_SCHEMA_VERSION:
                        raise HarnessStoreError(
                            "Harness 数据库版本高于当前程序支持范围，请升级 NaumiAgent。"
                        )
                    await db.execute("PRAGMA journal_mode = WAL")
                    await db.executescript(_SCHEMA_V1)
                    await db.executescript(_SCHEMA_V2)
                    await db.executescript(_SCHEMA_V3)
                    await db.execute(
                        f"PRAGMA user_version = {HARNESS_STORE_SCHEMA_VERSION}"
                    )
                    await db.commit()
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
