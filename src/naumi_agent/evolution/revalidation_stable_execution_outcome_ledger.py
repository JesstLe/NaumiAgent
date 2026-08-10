"""Durable ledger and source revalidation for stable execution outcomes."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.release_bound_execution_coverage import (
    ReleaseBoundExecutionCoverageError,
    read_current_execution_coverage,
    read_exact_execution_coverage,
)
from naumi_agent.evolution.revalidation_stable_execution_outcomes import (
    EvolutionRevalidationStableExecutionOutcome,
    EvolutionRevalidationStableExecutionOutcomeError,
    build_stable_execution_outcome,
)
from naumi_agent.evolution.revalidation_stable_observation_window_assessments import (
    EvolutionRevalidationStableObservationAssessmentError,
    EvolutionRevalidationStableObservationWindowService,
    EvolutionRevalidationStableObservationWindowStore,
)
from naumi_agent.evolution.revalidation_stable_observation_windows import (
    EvolutionRevalidationStableObservationWindow,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.runs.store import ChatRunRecord, ChatRunStore

EVOLUTION_REVALIDATION_STABLE_EXECUTION_OUTCOME_LEDGER_POLICY = (
    "evolution-revalidation-stable-execution-outcome-ledger-v1"
)
_MAX_OUTCOME_BYTES = 8 * 1024 * 1024
_INTENT_RE = re.compile(r"^evrestableintent_[0-9a-f]{24}$")
_OUTCOME_RE = re.compile(r"^evrestableoutcome_[0-9a-f]{24}$")
_SUBJECT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationStableExecutionOutcomeView(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-stable-execution-outcome-ledger-v1"
    ] = EVOLUTION_REVALIDATION_STABLE_EXECUTION_OUTCOME_LEDGER_POLICY
    receipt: EvolutionRevalidationStableExecutionOutcome
    outcome_source_current: bool
    chat_run_source_current: bool
    liveness_source_current: bool
    release_binding_current: bool
    heartbeat_coverage_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    execution_outcome_authority: bool
    stable_population_observation_input_authority: bool
    successful_completed_run_authority: bool
    stable_completed_run_authority: bool
    stable_stage_completion_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.outcome_source_current
            and self.chat_run_source_current
            and self.liveness_source_current
            and self.release_binding_current
            and self.heartbeat_coverage_current
        )
        successful = bool(
            current and self.receipt.successful_completed_run_authority
        )
        if not (
            self.execution_outcome_authority is current
            and self.stable_population_observation_input_authority is current
            and self.successful_completed_run_authority is successful
            and self.stable_completed_run_authority is successful
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Stable Execution Outcome View authority 投影不一致。")
        return self


class EvolutionRevalidationStableExecutionOutcomeLedgerStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        window_store: EvolutionRevalidationStableObservationWindowStore,
        chat_run_store: ChatRunStore,
        harness_store: HarnessStore,
    ) -> None:
        if not isinstance(
            window_store,
            EvolutionRevalidationStableObservationWindowStore,
        ):
            raise TypeError("Stable Outcome Store 需要 Observation Window Store。")
        if not isinstance(chat_run_store, ChatRunStore):
            raise TypeError("Stable Outcome Store 需要 ChatRunStore。")
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Stable Outcome Store 需要 HarnessStore。")
        self.db_path = Path(db_path).expanduser().resolve()
        if self.db_path != window_store.db_path:
            raise ValueError("Stable Outcome 与 Window 必须共用证据数据库。")
        if window_store.harness_store is not harness_store:
            raise ValueError("Stable Outcome 与 Window 必须复用同一 HAR source。")
        self.window_store = window_store
        self.chat_run_store = chat_run_store
        self.harness_store = harness_store

    async def get(
        self,
        outcome_id: str,
    ) -> EvolutionRevalidationStableExecutionOutcome | None:
        normalized_outcome_id = _outcome_id(outcome_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT outcome_json FROM "
                        "evolution_revalidation_stable_execution_outcomes "
                        "WHERE outcome_id = ?",
                        (normalized_outcome_id,),
                    )
                ).fetchone()
            return None if row is None else _restore_outcome(str(row["outcome_json"]))
        except EvolutionRevalidationStableExecutionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableExecutionOutcomeError(
                "stable_execution_outcome_source_unavailable",
                "Stable Execution Outcome durable source 当前不可读取。",
            ) from exc

    async def get_by_run(
        self,
        run_id: str,
    ) -> EvolutionRevalidationStableExecutionOutcome | None:
        normalized_run_id = _identifier(run_id, field="run_id")
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT outcome_json FROM "
                        "evolution_revalidation_stable_execution_outcomes "
                        "WHERE run_id = ?",
                        (normalized_run_id,),
                    )
                ).fetchone()
            return None if row is None else _restore_outcome(str(row["outcome_json"]))
        except EvolutionRevalidationStableExecutionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableExecutionOutcomeError(
                "stable_execution_outcome_source_unavailable",
                "Stable Execution Outcome durable source 当前不可读取。",
            ) from exc

    async def list_for_intent(
        self,
        intent_id: str,
        *,
        limit: int = 100,
    ) -> tuple[EvolutionRevalidationStableExecutionOutcome, ...]:
        normalized_intent_id = _intent(intent_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须是 1 到 100 之间的整数。")
        if not self.db_path.is_file():
            return ()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                rows = await (
                    await db.execute(
                        "SELECT outcome_json FROM "
                        "evolution_revalidation_stable_execution_outcomes "
                        "WHERE intent_id = ? "
                        "ORDER BY execution_completed_at, outcome_id LIMIT ?",
                        (normalized_intent_id, limit),
                    )
                ).fetchall()
            return tuple(
                _restore_outcome(str(row["outcome_json"])) for row in rows
            )
        except EvolutionRevalidationStableExecutionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableExecutionOutcomeError(
                "stable_execution_outcome_source_unavailable",
                "Stable Execution Outcome durable source 当前不可读取。",
            ) from exc

    async def record(
        self,
        outcome: EvolutionRevalidationStableExecutionOutcome,
    ) -> EvolutionRevalidationStableExecutionOutcome:
        item = _validated_outcome(outcome)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_OUTCOME_BYTES:
            raise EvolutionRevalidationStableExecutionOutcomeError(
                "stable_execution_outcome_oversized",
                "Stable Execution Outcome 超过 8 MiB。",
            )
        await self._require_external_sources(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                window_row = await (
                    await db.execute(
                        "SELECT window_json FROM "
                        "evolution_revalidation_stable_observation_windows "
                        "WHERE window_id = ?",
                        (item.liveness_source.window_id,),
                    )
                ).fetchone()
                if window_row is None:
                    await db.rollback()
                    raise EvolutionRevalidationStableExecutionOutcomeError(
                        "stable_execution_liveness_source_changed",
                        "Stable Outcome liveness source 已变化或不存在。",
                    )
                try:
                    durable_window = (
                        EvolutionRevalidationStableObservationWindow.model_validate_json(
                            window_row["window_json"]
                        )
                    )
                except (TypeError, ValueError) as exc:
                    await db.rollback()
                    raise EvolutionRevalidationStableExecutionOutcomeError(
                        "stable_execution_liveness_source_changed",
                        "Stable Outcome liveness source 已损坏。",
                    ) from exc
                if not _window_matches(item, durable_window):
                    await db.rollback()
                    raise EvolutionRevalidationStableExecutionOutcomeError(
                        "stable_execution_liveness_source_changed",
                        "Stable Outcome liveness source 与引用不一致。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT outcome_json FROM "
                        "evolution_revalidation_stable_execution_outcomes "
                        "WHERE run_id = ?",
                        (item.run_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore_outcome(str(existing["outcome_json"]))
                    await db.rollback()
                    if not _same_run_evidence(restored, item):
                        raise EvolutionRevalidationStableExecutionOutcomeError(
                            "stable_execution_run_identity_conflict",
                            "同一 chat run 已绑定不同 Stable Outcome evidence。",
                        )
                    return restored
                if await _run_exists_in_prior_stage_ledger(db, item.run_id):
                    await db.rollback()
                    raise EvolutionRevalidationStableExecutionOutcomeError(
                        "stable_execution_cross_stage_duplicate",
                        "该 chat run 已被 Opt-in 或 Percentage Outcome 使用。",
                    )
                await db.execute(
                    "INSERT INTO evolution_revalidation_stable_execution_outcomes "
                    "(outcome_id, outcome_sha256, intent_id, window_id, run_id, "
                    "session_id, result, successful, execution_completed_at, outcome_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.outcome_id,
                        item.outcome_sha256,
                        item.liveness_source.intent_id,
                        item.liveness_source.window_id,
                        item.run_id,
                        item.session_id,
                        item.result,
                        int(item.successful_completed_run_authority),
                        item.execution_completed_at,
                        encoded,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationStableExecutionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableExecutionOutcomeError(
                "stable_execution_outcome_store_error",
                "Stable Execution Outcome 无法持久化。",
            ) from exc
        return item

    async def _require_external_sources(
        self,
        item: EvolutionRevalidationStableExecutionOutcome,
    ) -> None:
        window = await self.window_store.get(item.liveness_source.window_id)
        if window is None or not _window_matches(item, window):
            raise EvolutionRevalidationStableExecutionOutcomeError(
                "stable_execution_liveness_source_changed",
                "Stable Outcome liveness source 已变化或不存在。",
            )
        run = await self.chat_run_store.get_run(item.session_id, item.run_id)
        if run is None or not _run_matches(item, run):
            raise EvolutionRevalidationStableExecutionOutcomeError(
                "stable_execution_chat_run_source_changed",
                "Stable Outcome chat run source 已变化或不存在。",
            )
        binding = await self.harness_store.get_runtime_release_binding(
            workspace_root=item.workspace_root,
            subject_id=item.release_provenance.binding.subject_id,
        )
        if binding != item.release_provenance.binding:
            raise EvolutionRevalidationStableExecutionOutcomeError(
                "stable_execution_binding_source_changed",
                "Stable Outcome release binding 已变化或不存在。",
            )
        actual = await _read_exact_coverage(
            harness_store=self.harness_store,
            outcome=item,
        )
        if actual != item.coverage_samples:
            raise EvolutionRevalidationStableExecutionOutcomeError(
                "stable_execution_coverage_source_changed",
                "Stable Outcome heartbeat coverage source 已变化。",
            )


class EvolutionRevalidationStableExecutionOutcomeLedgerService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        window_service: EvolutionRevalidationStableObservationWindowService,
        harness_store: HarnessStore,
        chat_run_store: ChatRunStore,
        store: EvolutionRevalidationStableExecutionOutcomeLedgerStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            isinstance(
                window_service,
                EvolutionRevalidationStableObservationWindowService,
            )
            and isinstance(harness_store, HarnessStore)
            and isinstance(chat_run_store, ChatRunStore)
            and isinstance(
                store,
                EvolutionRevalidationStableExecutionOutcomeLedgerStore,
            )
            and store.window_store is window_service.store
            and store.harness_store is harness_store
            and store.chat_run_store is chat_run_store
        ):
            raise ValueError("Stable Outcome Store/Service source 必须一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if self.workspace_root != window_service.workspace_root:
            raise ValueError("Stable Outcome 与 Window workspace 必须一致。")
        self.window_service = window_service
        self.harness_store = harness_store
        self.chat_run_store = chat_run_store
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def record(
        self,
        *,
        intent_id: str,
        subject_id: str,
        session_id: str,
        run_id: str,
    ) -> EvolutionRevalidationStableExecutionOutcomeView:
        normalized_intent_id = _intent(intent_id)
        normalized_subject_id = _subject(subject_id)
        normalized_run_id = _identifier(run_id, field="run_id")
        normalized_session_id = _identifier(session_id, field="session_id")
        lock = self._locks.setdefault(normalized_run_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_run(normalized_run_id)
            if existing is not None:
                if existing.liveness_source.intent_id != normalized_intent_id:
                    raise EvolutionRevalidationStableExecutionOutcomeError(
                        "stable_execution_run_identity_conflict",
                        "该 chat run 已绑定其他 Stable Intent。",
                    )
                return await self._view(existing)
            try:
                window_view = await self.window_service.inspect(
                    intent_id=normalized_intent_id,
                    subject_id=normalized_subject_id,
                )
            except EvolutionRevalidationStableObservationAssessmentError as exc:
                raise EvolutionRevalidationStableExecutionOutcomeError(
                    "stable_execution_liveness_unavailable",
                    "缺少当前可用的 Stable runtime window。",
                ) from exc
            if not window_view.stable_runtime_window_authority:
                raise EvolutionRevalidationStableExecutionOutcomeError(
                    "stable_execution_liveness_not_authoritative",
                    "当前 Stable runtime window 不具备执行结果准入权限。",
                )
            run = await self.chat_run_store.get_run(
                normalized_session_id,
                normalized_run_id,
            )
            if run is None:
                raise EvolutionRevalidationStableExecutionOutcomeError(
                    "stable_execution_chat_run_missing",
                    "指定 chat run 不存在或不属于该 Session。",
                )
            try:
                coverage = await read_current_execution_coverage(
                    harness_store=self.harness_store,
                    run=run,
                )
            except (ReleaseBoundExecutionCoverageError, HarnessStoreError) as exc:
                code = getattr(exc, "code", "ledger_unavailable")
                raise EvolutionRevalidationStableExecutionOutcomeError(
                    f"stable_execution_{code}",
                    str(exc),
                ) from exc
            outcome = build_stable_execution_outcome(
                window=window_view.receipt,
                run=run,
                coverage_samples=coverage,
                recorded_at=self._now().isoformat(),
            )
            stored = await self.store.record(outcome)
            return await self._view(stored)

    async def inspect(
        self,
        *,
        outcome_id: str,
    ) -> EvolutionRevalidationStableExecutionOutcomeView:
        receipt = await self.store.get(outcome_id)
        if receipt is None:
            raise EvolutionRevalidationStableExecutionOutcomeError(
                "stable_execution_outcome_missing",
                "指定的 Stable Execution Outcome 不存在。",
            )
        return await self._view(receipt)

    async def _view(
        self,
        receipt: EvolutionRevalidationStableExecutionOutcome,
    ) -> EvolutionRevalidationStableExecutionOutcomeView:
        invalidation: list[str] = []
        source = await self.store.get(receipt.outcome_id)
        outcome_source_current = source == receipt
        if not outcome_source_current:
            invalidation.append("outcome_source_changed")
        try:
            run = await self.chat_run_store.get_run(receipt.session_id, receipt.run_id)
            chat_run_source_current = run is not None and _run_matches(receipt, run)
        except (aiosqlite.Error, OSError, TypeError, ValueError):
            chat_run_source_current = False
        if not chat_run_source_current:
            invalidation.append("chat_run_source_changed")
        try:
            window = await self.store.window_store.get(
                receipt.liveness_source.window_id
            )
            liveness_source_current = window is not None and _window_matches(
                receipt,
                window,
            )
        except (
            EvolutionRevalidationStableObservationAssessmentError,
            OSError,
            TypeError,
            ValueError,
        ):
            liveness_source_current = False
        if not liveness_source_current:
            invalidation.append("liveness_source_changed")
        try:
            binding = await self.harness_store.get_runtime_release_binding(
                workspace_root=receipt.workspace_root,
                subject_id=receipt.release_provenance.binding.subject_id,
            )
            release_binding_current = binding == receipt.release_provenance.binding
        except HarnessStoreError:
            release_binding_current = False
        if not release_binding_current:
            invalidation.append("release_binding_changed")
        try:
            actual = await _read_exact_coverage(
                harness_store=self.harness_store,
                outcome=receipt,
            )
            heartbeat_coverage_current = actual == receipt.coverage_samples
        except (HarnessStoreError, ReleaseBoundExecutionCoverageError):
            heartbeat_coverage_current = False
        if not heartbeat_coverage_current:
            invalidation.append("heartbeat_coverage_changed")
        current = bool(
            outcome_source_current
            and chat_run_source_current
            and liveness_source_current
            and release_binding_current
            and heartbeat_coverage_current
        )
        successful = bool(current and receipt.successful_completed_run_authority)
        return EvolutionRevalidationStableExecutionOutcomeView(
            receipt=receipt,
            outcome_source_current=outcome_source_current,
            chat_run_source_current=chat_run_source_current,
            liveness_source_current=liveness_source_current,
            release_binding_current=release_binding_current,
            heartbeat_coverage_current=heartbeat_coverage_current,
            invalidation_reasons=tuple(sorted(set(invalidation))),
            execution_outcome_authority=current,
            stable_population_observation_input_authority=current,
            successful_completed_run_authority=successful,
            stable_completed_run_authority=successful,
        )

    def _now(self) -> datetime:
        return _aware(self.clock())


async def _read_exact_coverage(*, harness_store, outcome):
    return await read_exact_execution_coverage(
        harness_store=harness_store,
        workspace_root=outcome.workspace_root,
        subject_id=outcome.release_provenance.binding.subject_id,
        binding_id=outcome.release_provenance.binding.binding_id,
        binding_sha256=outcome.release_provenance.binding.binding_sha256,
        expected=outcome.coverage_samples,
    )


def _run_matches(
    outcome: EvolutionRevalidationStableExecutionOutcome,
    run: ChatRunRecord,
) -> bool:
    return bool(
        run.id == outcome.run_id
        and run.session_id == outcome.session_id
        and run.status == outcome.run_status
        and run.release_provenance == outcome.release_provenance
        and run.receipt == outcome.completion_receipt
        and run.usage == outcome.usage
        and _aware(run.started_at) == _aware(outcome.execution_started_at)
        and _aware(run.completed_at) >= _aware(outcome.execution_completed_at)
    )


def _window_matches(outcome, window) -> bool:
    source = outcome.liveness_source
    exposure = window.exposure
    deployment = exposure.deployment
    return bool(
        window.window_id == source.window_id
        and window.window_sha256 == source.window_sha256
        and exposure.exposure_id == source.exposure_id
        and exposure.exposure_sha256 == source.exposure_sha256
        and deployment.receipt_id == source.deployment_receipt_id
        and deployment.receipt_sha256 == source.deployment_receipt_sha256
        and exposure.binding.binding_id == source.binding_id
        and exposure.binding.binding_sha256 == source.binding_sha256
        and deployment.preparation.intent.intent_id == source.intent_id
        and deployment.preparation.intent.candidate_version
        == source.candidate_version
        and deployment.preparation.intent.installation_target
        == source.candidate_target
        and source.stage_order == 4
        and source.stage_name == "stable"
        and source.exposure_percent == 100
        and window.population_snapshot_id == source.population_snapshot_id
        and window.population_snapshot_sha256
        == source.population_snapshot_sha256
        and window.population_snapshot_sequence
        == source.population_snapshot_sequence
        and window.population_denominator == source.population_denominator
        and window.installation_member_id == source.installation_member_id
    )


def _same_run_evidence(left, right) -> bool:
    return bool(
        left.workspace_root == right.workspace_root
        and left.session_id == right.session_id
        and left.run_id == right.run_id
        and left.run_status == right.run_status
        and left.liveness_source == right.liveness_source
        and left.release_provenance == right.release_provenance
        and left.completion_receipt == right.completion_receipt
        and left.usage == right.usage
        and left.coverage_samples == right.coverage_samples
    )


def _validated_outcome(value) -> EvolutionRevalidationStableExecutionOutcome:
    try:
        return EvolutionRevalidationStableExecutionOutcome.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableExecutionOutcomeError(
            "stable_execution_outcome_invalid",
            "Stable Execution Outcome artifact 无效。",
        ) from exc


def _restore_outcome(value: str) -> EvolutionRevalidationStableExecutionOutcome:
    if len(value.encode()) > _MAX_OUTCOME_BYTES:
        raise EvolutionRevalidationStableExecutionOutcomeError(
            "stable_execution_outcome_source_oversized",
            "Stable Execution Outcome durable source 超过上限。",
        )
    try:
        return EvolutionRevalidationStableExecutionOutcome.model_validate_json(
            value
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableExecutionOutcomeError(
            "stable_execution_outcome_source_invalid",
            "Stable Execution Outcome durable source 无效。",
        ) from exc


def _intent(value: str) -> str:
    if not isinstance(value, str) or _INTENT_RE.fullmatch(value) is None:
        raise ValueError("intent_id 必须是稳定的 Stable Deployment Intent 标识。")
    return value


def _outcome_id(value: str) -> str:
    if not isinstance(value, str) or _OUTCOME_RE.fullmatch(value) is None:
        raise ValueError("outcome_id 必须是稳定的 Stable Outcome 标识。")
    return value


def _subject(value: str) -> str:
    if not isinstance(value, str) or _SUBJECT_RE.fullmatch(value) is None:
        raise ValueError("subject_id 必须是稳定的 runtime 标识。")
    return value


def _identifier(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or value.strip() != value:
        raise ValueError(f"{field} 必须是 1 到 128 字符的稳定标识。")
    return value


async def _run_exists_in_prior_stage_ledger(
    db: aiosqlite.Connection,
    run_id: str,
) -> bool:
    tables = (
        "evolution_revalidation_opt_in_execution_outcomes",
        "evolution_revalidation_percentage_execution_outcomes",
    )
    for table_name in tables:
        table = await (
            await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            )
        ).fetchone()
        if table is None:
            continue
        sql = (
            "SELECT 1 FROM evolution_revalidation_opt_in_execution_outcomes "
            "WHERE run_id = ? LIMIT 1"
            if table_name == "evolution_revalidation_opt_in_execution_outcomes"
            else "SELECT 1 FROM evolution_revalidation_percentage_execution_outcomes "
            "WHERE run_id = ? LIMIT 1"
        )
        row = await (await db.execute(sql, (run_id,))).fetchone()
        if row is not None:
            return True
    return False


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stable Execution Outcome timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_stable_execution_outcomes (
            outcome_id TEXT PRIMARY KEY,
            outcome_sha256 TEXT NOT NULL,
            intent_id TEXT NOT NULL,
            window_id TEXT NOT NULL,
            run_id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL,
            result TEXT NOT NULL,
            successful INTEGER NOT NULL,
            execution_completed_at TEXT NOT NULL,
            outcome_json TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_evolution_revalidation_stable_execution_outcomes_intent "
        "ON evolution_revalidation_stable_execution_outcomes "
        "(intent_id, execution_completed_at, outcome_id)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_STABLE_EXECUTION_OUTCOME_LEDGER_POLICY",
    "EvolutionRevalidationStableExecutionOutcomeLedgerService",
    "EvolutionRevalidationStableExecutionOutcomeLedgerStore",
    "EvolutionRevalidationStableExecutionOutcomeView",
]
