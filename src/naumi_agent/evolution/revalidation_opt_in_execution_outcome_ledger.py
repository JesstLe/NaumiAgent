"""Durable ledger and source revalidation for opt-in execution outcomes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_opt_in_execution_outcomes import (
    EvolutionRevalidationOptInExecutionOutcome,
    EvolutionRevalidationOptInExecutionOutcomeError,
    build_opt_in_execution_outcome,
)
from naumi_agent.evolution.revalidation_opt_in_observation_window_assessments import (
    EvolutionRevalidationOptInObservationAssessmentError,
    EvolutionRevalidationOptInObservationWindowService,
    EvolutionRevalidationOptInObservationWindowStore,
)
from naumi_agent.evolution.revalidation_opt_in_observation_windows import (
    EvolutionRevalidationOptInObservationWindow,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.runs.store import ChatRunRecord, ChatRunStore

EVOLUTION_REVALIDATION_OPT_IN_EXECUTION_OUTCOME_LEDGER_POLICY = (
    "evolution-revalidation-opt-in-execution-outcome-ledger-v1"
)
_MAX_OUTCOME_BYTES = 8 * 1024 * 1024
_MAX_SAMPLES = 5_000
_PAGE_LIMIT = 500


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationOptInExecutionOutcomeView(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-opt-in-execution-outcome-ledger-v1"
    ] = EVOLUTION_REVALIDATION_OPT_IN_EXECUTION_OUTCOME_LEDGER_POLICY
    receipt: EvolutionRevalidationOptInExecutionOutcome
    outcome_source_current: bool
    chat_run_source_current: bool
    liveness_source_current: bool
    release_binding_current: bool
    heartbeat_coverage_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    execution_outcome_authority: bool
    successful_completed_run_authority: bool
    opt_in_stage_completion_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
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
        if self.execution_outcome_authority is not current:
            raise ValueError("Opt-in Execution Outcome View authority projection 不一致。")
        if self.successful_completed_run_authority is not successful:
            raise ValueError("Opt-in Execution Outcome View success projection 不一致。")
        if tuple(sorted(set(self.invalidation_reasons))) != self.invalidation_reasons:
            raise ValueError("Opt-in Execution Outcome invalidation reasons 必须去重排序。")
        return self


class EvolutionRevalidationOptInExecutionOutcomeLedgerStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        window_store: EvolutionRevalidationOptInObservationWindowStore,
        chat_run_store: ChatRunStore,
        harness_store: HarnessStore,
    ) -> None:
        if not isinstance(window_store, EvolutionRevalidationOptInObservationWindowStore):
            raise TypeError("Execution Outcome Store 需要 Observation Window Store。")
        if not isinstance(chat_run_store, ChatRunStore):
            raise TypeError("Execution Outcome Store 需要 ChatRunStore。")
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Execution Outcome Store 需要 HarnessStore。")
        self.db_path = Path(db_path).expanduser().resolve()
        if self.db_path != window_store.db_path:
            raise ValueError("Execution Outcome 与 Observation Window 必须共用证据数据库。")
        if window_store.harness_store is not harness_store:
            raise ValueError("Execution Outcome 与 Window 必须复用同一 HAR source。")
        self.window_store = window_store
        self.chat_run_store = chat_run_store
        self.harness_store = harness_store

    async def get(
        self,
        outcome_id: str,
    ) -> EvolutionRevalidationOptInExecutionOutcome | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT outcome_json FROM "
                        "evolution_revalidation_opt_in_execution_outcomes "
                        "WHERE outcome_id = ?",
                        (outcome_id,),
                    )
                ).fetchone()
            return None if row is None else _restore_outcome(row["outcome_json"])
        except EvolutionRevalidationOptInExecutionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_outcome_source_unavailable",
                "Opt-in Execution Outcome durable source 当前不可读取。",
            ) from exc

    async def get_by_run(
        self,
        run_id: str,
    ) -> EvolutionRevalidationOptInExecutionOutcome | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT outcome_json FROM "
                        "evolution_revalidation_opt_in_execution_outcomes "
                        "WHERE run_id = ?",
                        (run_id,),
                    )
                ).fetchone()
            return None if row is None else _restore_outcome(row["outcome_json"])
        except EvolutionRevalidationOptInExecutionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_outcome_source_unavailable",
                "Opt-in Execution Outcome durable source 当前不可读取。",
            ) from exc

    async def list_for_completion(
        self,
        completion_id: str,
        *,
        limit: int = 100,
    ) -> tuple[EvolutionRevalidationOptInExecutionOutcome, ...]:
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
                        "evolution_revalidation_opt_in_execution_outcomes "
                        "WHERE completion_id = ? "
                        "ORDER BY execution_completed_at, outcome_id LIMIT ?",
                        (completion_id, limit),
                    )
                ).fetchall()
            return tuple(_restore_outcome(row["outcome_json"]) for row in rows)
        except EvolutionRevalidationOptInExecutionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_outcome_source_unavailable",
                "Opt-in Execution Outcome durable source 当前不可读取。",
            ) from exc

    async def record(
        self,
        outcome: EvolutionRevalidationOptInExecutionOutcome,
    ) -> EvolutionRevalidationOptInExecutionOutcome:
        item = _validated_outcome(outcome)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_OUTCOME_BYTES:
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_outcome_oversized",
                "Opt-in Execution Outcome 超过 8 MiB。",
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
                        "evolution_revalidation_opt_in_observation_windows "
                        "WHERE window_id = ?",
                        (item.liveness_source.window_id,),
                    )
                ).fetchone()
                if window_row is None:
                    await db.rollback()
                    raise EvolutionRevalidationOptInExecutionOutcomeError(
                        "opt_in_execution_liveness_source_changed",
                        "Execution Outcome liveness source 已变化或不存在。",
                    )
                try:
                    durable_window = (
                        EvolutionRevalidationOptInObservationWindow.model_validate_json(
                            window_row["window_json"]
                        )
                    )
                except (TypeError, ValueError) as exc:
                    await db.rollback()
                    raise EvolutionRevalidationOptInExecutionOutcomeError(
                        "opt_in_execution_liveness_source_changed",
                        "Execution Outcome liveness source 已损坏。",
                    ) from exc
                if not _window_matches(item, durable_window):
                    await db.rollback()
                    raise EvolutionRevalidationOptInExecutionOutcomeError(
                        "opt_in_execution_liveness_source_changed",
                        "Execution Outcome liveness source 与引用不一致。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT outcome_json FROM "
                        "evolution_revalidation_opt_in_execution_outcomes "
                        "WHERE run_id = ?",
                        (item.run_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore_outcome(existing["outcome_json"])
                    await db.rollback()
                    if not _same_run_evidence(restored, item):
                        raise EvolutionRevalidationOptInExecutionOutcomeError(
                            "opt_in_execution_run_identity_conflict",
                            "同一 chat run 已绑定不同 Execution Outcome evidence。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_opt_in_execution_outcomes "
                    "(outcome_id, outcome_sha256, completion_id, window_id, run_id, "
                    "session_id, result, successful, execution_completed_at, outcome_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.outcome_id,
                        item.outcome_sha256,
                        item.liveness_source.completion_id,
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
        except EvolutionRevalidationOptInExecutionOutcomeError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_outcome_store_error",
                "Opt-in Execution Outcome 无法持久化。",
            ) from exc
        return item

    async def _require_external_sources(
        self,
        item: EvolutionRevalidationOptInExecutionOutcome,
    ) -> None:
        window = await self.window_store.get(item.liveness_source.window_id)
        if window is None or not _window_matches(item, window):
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_liveness_source_changed",
                "Execution Outcome liveness source 已变化或不存在。",
            )
        run = await self.chat_run_store.get_run(item.session_id, item.run_id)
        if run is None or not _run_matches(item, run):
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_chat_run_source_changed",
                "Execution Outcome chat run source 已变化或不存在。",
            )
        binding = await self.harness_store.get_runtime_release_binding(
            workspace_root=item.workspace_root,
            subject_id=item.release_provenance.binding.subject_id,
        )
        if binding != item.release_provenance.binding:
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_binding_source_changed",
                "Execution Outcome release binding 已变化或不存在。",
            )
        actual = await _read_exact_coverage(
            harness_store=self.harness_store,
            outcome=item,
        )
        if actual != item.coverage_samples:
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_coverage_source_changed",
                "Execution Outcome heartbeat coverage source 已变化。",
            )


class EvolutionRevalidationOptInExecutionOutcomeLedgerService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        window_service: EvolutionRevalidationOptInObservationWindowService,
        harness_store: HarnessStore,
        chat_run_store: ChatRunStore,
        store: EvolutionRevalidationOptInExecutionOutcomeLedgerStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(window_service, EvolutionRevalidationOptInObservationWindowService):
            raise TypeError("Execution Outcome Service 需要 Observation Window Service。")
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Execution Outcome Service 需要 HarnessStore。")
        if not isinstance(chat_run_store, ChatRunStore):
            raise TypeError("Execution Outcome Service 需要 ChatRunStore。")
        if not isinstance(store, EvolutionRevalidationOptInExecutionOutcomeLedgerStore):
            raise TypeError("Execution Outcome Service 需要 Outcome Store。")
        if not (
            store.window_store is window_service.store
            and store.harness_store is harness_store
            and store.chat_run_store is chat_run_store
        ):
            raise ValueError("Execution Outcome Store/Service source 必须一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if self.workspace_root != window_service.workspace_root:
            raise ValueError("Execution Outcome 与 Observation Window workspace 必须一致。")
        self.window_service = window_service
        self.harness_store = harness_store
        self.chat_run_store = chat_run_store
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def record(
        self,
        *,
        completion_id: str,
        subject_id: str,
        session_id: str,
        run_id: str,
    ) -> EvolutionRevalidationOptInExecutionOutcomeView:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_run(run_id)
            if existing is not None:
                if existing.liveness_source.completion_id != completion_id:
                    raise EvolutionRevalidationOptInExecutionOutcomeError(
                        "opt_in_execution_run_identity_conflict",
                        "该 chat run 已绑定其他 completion evidence。",
                    )
                return await self._view(existing)
            try:
                window_view = await self.window_service.inspect(
                    completion_id=completion_id,
                    subject_id=subject_id,
                )
            except EvolutionRevalidationOptInObservationAssessmentError as exc:
                raise EvolutionRevalidationOptInExecutionOutcomeError(
                    "opt_in_execution_liveness_unavailable",
                    "缺少当前可用的 Opt-in liveness window。",
                ) from exc
            if not window_view.runtime_liveness_window_authority:
                raise EvolutionRevalidationOptInExecutionOutcomeError(
                    "opt_in_execution_liveness_not_authoritative",
                    "当前 Opt-in liveness window 不具备执行结果准入权限。",
                )
            run = await self.chat_run_store.get_run(session_id, run_id)
            if run is None:
                raise EvolutionRevalidationOptInExecutionOutcomeError(
                    "opt_in_execution_chat_run_missing",
                    "指定 chat run 不存在或不属于该 Session。",
                )
            coverage = await _read_current_coverage(
                harness_store=self.harness_store,
                run=run,
            )
            outcome = build_opt_in_execution_outcome(
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
    ) -> EvolutionRevalidationOptInExecutionOutcomeView:
        receipt = await self.store.get(outcome_id)
        if receipt is None:
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_outcome_missing",
                "指定的 Opt-in Execution Outcome 不存在。",
            )
        return await self._view(receipt)

    async def _view(
        self,
        receipt: EvolutionRevalidationOptInExecutionOutcome,
    ) -> EvolutionRevalidationOptInExecutionOutcomeView:
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
            EvolutionRevalidationOptInObservationAssessmentError,
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
        except (HarnessStoreError, EvolutionRevalidationOptInExecutionOutcomeError):
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
        return EvolutionRevalidationOptInExecutionOutcomeView(
            receipt=receipt,
            outcome_source_current=outcome_source_current,
            chat_run_source_current=chat_run_source_current,
            liveness_source_current=liveness_source_current,
            release_binding_current=release_binding_current,
            heartbeat_coverage_current=heartbeat_coverage_current,
            invalidation_reasons=tuple(sorted(set(invalidation))),
            execution_outcome_authority=current,
            successful_completed_run_authority=(
                current and receipt.successful_completed_run_authority
            ),
        )

    def _now(self) -> datetime:
        return _aware(self.clock())


async def _read_current_coverage(
    *,
    harness_store: HarnessStore,
    run: ChatRunRecord,
):
    if run.release_provenance is None or run.receipt is None:
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_run_evidence_incomplete",
            "Chat run 缺少 release provenance 或 completion receipt。",
        )
    binding = run.release_provenance.binding
    heartbeat = await harness_store.get_heartbeat(
        workspace_root=run.release_provenance.workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=binding.subject_id,
    )
    if heartbeat is None:
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_heartbeat_missing",
            "缺少 Runtime heartbeat head。",
        )
    origin_page = await harness_store.list_runtime_release_observations(
        workspace_root=run.release_provenance.workspace_root,
        subject_id=binding.subject_id,
        after_sequence=0,
        limit=1,
    )
    if origin_page is None or not origin_page.items:
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_coverage_missing",
            "缺少 Runtime Release Observation samples。",
        )
    origin = origin_page.items[0].chain_origin_sequence
    first_sequence = max(origin, heartbeat.sequence - _MAX_SAMPLES + 1)
    after_sequence = 0 if first_sequence == origin else first_sequence - 1
    samples = []
    while after_sequence < heartbeat.sequence:
        page = await harness_store.list_runtime_release_observations(
            workspace_root=run.release_provenance.workspace_root,
            subject_id=binding.subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, heartbeat.sequence - after_sequence),
        )
        if page is None or not (
            page.binding_id == binding.binding_id
            and page.binding_sha256 == binding.binding_sha256
        ):
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_ledger_binding_mismatch",
                "Runtime observation ledger 未绑定该 chat run release。",
            )
        samples.extend(page.items)
        if page.items and page.items[-1].heartbeat_sequence == heartbeat.sequence:
            break
        if not page.items or page.items[-1].heartbeat_sequence <= after_sequence:
            raise EvolutionRevalidationOptInExecutionOutcomeError(
                "opt_in_execution_ledger_cursor_invalid",
                "Runtime observation ledger cursor 未前进。",
            )
        after_sequence = page.items[-1].heartbeat_sequence
    heartbeat_after = await harness_store.get_heartbeat(
        workspace_root=run.release_provenance.workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=binding.subject_id,
    )
    binding_after = await harness_store.get_runtime_release_binding(
        workspace_root=run.release_provenance.workspace_root,
        subject_id=binding.subject_id,
    )
    if (
        heartbeat_after != heartbeat
        or binding_after != binding
        or not samples
        or not _heartbeat_matches_sample(heartbeat, samples[-1])
    ):
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_ledger_head_changed",
            "Runtime observation ledger 在读取期间发生变化。",
        )
    started = _aware(run.receipt.started_at)
    completed = _aware(run.receipt.completed_at)
    predecessor = None
    successor = None
    for index, sample in enumerate(samples):
        observed = _aware(sample.observed_at)
        if observed <= started:
            predecessor = index
        if predecessor is not None and observed >= completed:
            successor = index
            break
    if predecessor is None or successor is None or successor <= predecessor:
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_coverage_pending",
            "Heartbeat 尚未从运行开始前连续覆盖到运行完成后。",
        )
    return tuple(samples[predecessor : successor + 1])


async def _read_exact_coverage(
    *,
    harness_store: HarnessStore,
    outcome: EvolutionRevalidationOptInExecutionOutcome,
):
    expected = outcome.coverage_samples
    first = expected[0]
    after_sequence = first.heartbeat_sequence - 1
    actual = []
    while len(actual) < len(expected):
        page = await harness_store.list_runtime_release_observations(
            workspace_root=outcome.workspace_root,
            subject_id=outcome.release_provenance.binding.subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, len(expected) - len(actual)),
        )
        if page is None or not page.items:
            break
        actual.extend(page.items)
        after_sequence = page.items[-1].heartbeat_sequence
    return tuple(actual)


def _run_matches(
    outcome: EvolutionRevalidationOptInExecutionOutcome,
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
    return bool(
        window.window_id == source.window_id
        and window.window_sha256 == source.window_sha256
        and window.runtime_health.receipt_id == source.runtime_health_receipt_id
        and window.runtime_health.receipt_sha256
        == source.runtime_health_receipt_sha256
        and window.runtime_health.deployment.receipt_id
        == source.deployment_receipt_id
        and window.runtime_health.deployment.receipt_sha256
        == source.deployment_receipt_sha256
        and window.binding.binding_id == source.binding_id
        and window.binding.binding_sha256 == source.binding_sha256
    )


def _same_run_evidence(left, right) -> bool:
    return bool(
        left.workspace_root == right.workspace_root
        and left.session_id == right.session_id
        and left.run_id == right.run_id
        and left.run_status == right.run_status
        and left.liveness_source.completion_id
        == right.liveness_source.completion_id
        and left.release_provenance == right.release_provenance
        and left.completion_receipt == right.completion_receipt
        and left.usage == right.usage
        and left.coverage_samples == right.coverage_samples
    )


def _heartbeat_matches_sample(heartbeat, sample) -> bool:
    return bool(
        heartbeat.workspace_root == sample.workspace_root
        and heartbeat.subject_kind is HarnessRunKind.RUNTIME
        and heartbeat.subject_id == sample.subject_id
        and heartbeat.instance_id == sample.instance_id
        and heartbeat.epoch == sample.epoch
        and heartbeat.sequence == sample.heartbeat_sequence
        and heartbeat.phase is sample.phase
        and _aware(heartbeat.observed_at) == _aware(sample.observed_at)
        and heartbeat.timeout_seconds == sample.timeout_seconds
        and heartbeat.detail_code == sample.detail_code
    )


def _validated_outcome(value) -> EvolutionRevalidationOptInExecutionOutcome:
    try:
        return EvolutionRevalidationOptInExecutionOutcome.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_outcome_invalid",
            "Opt-in Execution Outcome artifact 无效。",
        ) from exc


def _restore_outcome(value: str) -> EvolutionRevalidationOptInExecutionOutcome:
    if len(value.encode()) > _MAX_OUTCOME_BYTES:
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_outcome_source_oversized",
            "Opt-in Execution Outcome durable source 超过上限。",
        )
    try:
        return EvolutionRevalidationOptInExecutionOutcome.model_validate_json(value)
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInExecutionOutcomeError(
            "opt_in_execution_outcome_source_invalid",
            "Opt-in Execution Outcome durable source 无效。",
        ) from exc


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Opt-in Execution Outcome timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_opt_in_execution_outcomes (
            outcome_id TEXT PRIMARY KEY,
            outcome_sha256 TEXT NOT NULL,
            completion_id TEXT NOT NULL,
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
        "idx_evolution_revalidation_opt_in_execution_outcomes_completion "
        "ON evolution_revalidation_opt_in_execution_outcomes "
        "(completion_id, execution_completed_at, outcome_id)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_OPT_IN_EXECUTION_OUTCOME_LEDGER_POLICY",
    "EvolutionRevalidationOptInExecutionOutcomeLedgerService",
    "EvolutionRevalidationOptInExecutionOutcomeLedgerStore",
    "EvolutionRevalidationOptInExecutionOutcomeView",
]
