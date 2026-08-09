"""Durable and dynamically revalidated opt-in liveness-window assessments."""

from __future__ import annotations

import asyncio
import hmac
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_opt_in_observation_windows import (
    EvolutionRevalidationOptInObservationWindow,
    EvolutionRevalidationOptInObservationWindowError,
    EvolutionRevalidationOptInObservationWindowStatus,
    build_opt_in_observation_window,
)
from naumi_agent.evolution.revalidation_opt_in_runtime_health import (
    EvolutionRevalidationOptInRuntimeHealthError,
    EvolutionRevalidationOptInRuntimeHealthService,
    EvolutionRevalidationOptInRuntimeHealthStore,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

EVOLUTION_REVALIDATION_OPT_IN_OBSERVATION_ASSESSMENT_POLICY = (
    "evolution-revalidation-opt-in-observation-assessment-v1"
)
_MAX_WINDOW_BYTES = 8 * 1024 * 1024
_MAX_SAMPLES = 5_000
_PAGE_LIMIT = 500
_SUBJECT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationOptInObservationWindowView(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-opt-in-observation-assessment-v1"
    ] = EVOLUTION_REVALIDATION_OPT_IN_OBSERVATION_ASSESSMENT_POLICY
    receipt: EvolutionRevalidationOptInObservationWindow
    current_assessment: EvolutionRevalidationOptInObservationWindow | None = None
    window_source_current: bool
    latest_receipt_current: bool
    runtime_health_source_current: bool
    runtime_health_authority: bool
    active_deployment_authority: bool
    release_binding_current: bool
    observation_ledger_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    runtime_liveness_window_authority: bool
    pause_input_authority: bool
    rollback_input_authority: bool
    completed_run_evidence_authority: Literal[False] = False
    opt_in_stage_completion_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        dependencies_current = bool(
            self.window_source_current
            and self.latest_receipt_current
            and self.runtime_health_source_current
            and self.runtime_health_authority
            and self.active_deployment_authority
            and self.release_binding_current
            and self.observation_ledger_current
            and self.current_assessment is not None
        )
        passing = bool(
            dependencies_current
            and self.current_assessment is not None
            and self.current_assessment.status
            is EvolutionRevalidationOptInObservationWindowStatus.PASSING
            and self.current_assessment.runtime_liveness_window_authority
        )
        breached = bool(
            dependencies_current
            and self.current_assessment is not None
            and self.current_assessment.status
            is EvolutionRevalidationOptInObservationWindowStatus.BREACHED
            and self.current_assessment.pause_input_authority
            and self.current_assessment.rollback_input_authority
        )
        if self.runtime_liveness_window_authority is not passing:
            raise ValueError("Opt-in Observation Window View authority projection 不一致。")
        if self.pause_input_authority is not breached:
            raise ValueError("Opt-in Observation Window View pause projection 不一致。")
        if self.rollback_input_authority is not breached:
            raise ValueError("Opt-in Observation Window View rollback projection 不一致。")
        if tuple(sorted(set(self.invalidation_reasons))) != self.invalidation_reasons:
            raise ValueError("Opt-in Observation Window invalidation reasons 必须去重排序。")
        return self


class EvolutionRevalidationOptInObservationAssessmentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationOptInObservationWindowStore:
    """Append-only window receipts backed by exact Health and HAR sources."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        runtime_health_store: EvolutionRevalidationOptInRuntimeHealthStore,
        harness_store: HarnessStore,
    ) -> None:
        if not isinstance(
            runtime_health_store,
            EvolutionRevalidationOptInRuntimeHealthStore,
        ):
            raise TypeError("Observation Window Store 需要 Runtime Health Store。")
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Observation Window Store 需要 HarnessStore。")
        self.db_path = Path(db_path).expanduser().resolve()
        if self.db_path != runtime_health_store.db_path:
            raise ValueError("Observation Window 与 Runtime Health 必须共用证据数据库。")
        self.runtime_health_store = runtime_health_store
        self.harness_store = harness_store

    async def get(
        self,
        window_id: str,
    ) -> EvolutionRevalidationOptInObservationWindow | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT window_json FROM "
                        "evolution_revalidation_opt_in_observation_windows "
                        "WHERE window_id = ?",
                        (window_id,),
                    )
                ).fetchone()
            return None if row is None else _restore_window(row["window_json"])
        except EvolutionRevalidationOptInObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInObservationAssessmentError(
                "opt_in_observation_window_source_unavailable",
                "Opt-in Observation Window durable source 当前不可读取。",
            ) from exc

    async def latest(
        self,
        *,
        completion_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationOptInObservationWindow | None:
        subject = _subject(subject_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT window_json FROM "
                        "evolution_revalidation_opt_in_observation_windows "
                        "WHERE completion_id = ? AND subject_id = ? "
                        "ORDER BY assessed_at DESC, window_id DESC LIMIT 1",
                        (completion_id, subject),
                    )
                ).fetchone()
            return None if row is None else _restore_window(row["window_json"])
        except EvolutionRevalidationOptInObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInObservationAssessmentError(
                "opt_in_observation_window_source_unavailable",
                "Opt-in Observation Window durable source 当前不可读取。",
            ) from exc

    async def record(
        self,
        window: EvolutionRevalidationOptInObservationWindow,
    ) -> EvolutionRevalidationOptInObservationWindow:
        item = _validated_window(window)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_WINDOW_BYTES:
            raise EvolutionRevalidationOptInObservationAssessmentError(
                "opt_in_observation_window_oversized",
                "Opt-in Observation Window 超过 8 MiB。",
            )
        await self._require_external_sources(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                health_row = await (
                    await db.execute(
                        "SELECT receipt_json FROM "
                        "evolution_revalidation_opt_in_runtime_health_attempts "
                        "WHERE deployment_receipt_id = ? AND receipt_json != ''",
                        (item.runtime_health.deployment.receipt_id,),
                    )
                ).fetchone()
                if health_row is None or not hmac.compare_digest(
                    str(health_row["receipt_json"]).encode(),
                    item.runtime_health.model_dump_json().encode(),
                ):
                    await db.rollback()
                    raise EvolutionRevalidationOptInObservationAssessmentError(
                        "opt_in_observation_health_source_changed",
                        "Runtime Health durable source 已变化或不存在。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT window_json FROM "
                        "evolution_revalidation_opt_in_observation_windows "
                        "WHERE window_id = ?",
                        (item.window_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore_window(existing["window_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationOptInObservationAssessmentError(
                            "opt_in_observation_window_identity_conflict",
                            "同一 Observation Window identity 已绑定不同内容。",
                        )
                    return restored
                completion_id = (
                    item.runtime_health.deployment.intent.admission.completion_id
                )
                await db.execute(
                    "INSERT INTO evolution_revalidation_opt_in_observation_windows "
                    "(window_id, window_sha256, completion_id, health_receipt_id, "
                    "binding_id, subject_id, status, assessed_at, window_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.window_id,
                        item.window_sha256,
                        completion_id,
                        item.runtime_health.receipt_id,
                        item.binding.binding_id,
                        item.binding.subject_id,
                        item.status.value,
                        item.assessed_at,
                        encoded,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationOptInObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInObservationAssessmentError(
                "opt_in_observation_window_store_error",
                "Opt-in Observation Window 无法持久化。",
            ) from exc
        return item

    async def _require_external_sources(
        self,
        item: EvolutionRevalidationOptInObservationWindow,
    ) -> None:
        try:
            health = await self.runtime_health_store.get_by_deployment(
                item.runtime_health.deployment.receipt_id
            )
            if health != item.runtime_health:
                raise EvolutionRevalidationOptInObservationAssessmentError(
                    "opt_in_observation_health_source_changed",
                    "Runtime Health durable source 已变化或不存在。",
                )
            binding = await self.harness_store.get_runtime_release_binding(
                workspace_root=item.workspace_root,
                subject_id=item.binding.subject_id,
            )
            if binding != item.binding:
                raise EvolutionRevalidationOptInObservationAssessmentError(
                    "opt_in_observation_binding_source_changed",
                    "Runtime Release Binding durable source 已变化或不存在。",
                )
            actual = await _read_exact_sample_slice(
                harness_store=self.harness_store,
                window=item,
            )
            if actual != item.samples:
                raise EvolutionRevalidationOptInObservationAssessmentError(
                    "opt_in_observation_sample_source_changed",
                    "Runtime Release Observation durable source 与窗口不一致。",
                )
        except EvolutionRevalidationOptInObservationAssessmentError:
            raise
        except (aiosqlite.Error, HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationOptInObservationAssessmentError(
                "opt_in_observation_source_unavailable",
                "Opt-in Observation Window durable source 当前不可验证。",
            ) from exc


class EvolutionRevalidationOptInObservationWindowService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        runtime_health_service: EvolutionRevalidationOptInRuntimeHealthService,
        harness_store: HarnessStore,
        store: EvolutionRevalidationOptInObservationWindowStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(
            runtime_health_service,
            EvolutionRevalidationOptInRuntimeHealthService,
        ):
            raise TypeError("Observation Window Service 需要 Runtime Health Service。")
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Observation Window Service 需要 HarnessStore。")
        if not isinstance(store, EvolutionRevalidationOptInObservationWindowStore):
            raise TypeError("Observation Window Service 需要 Observation Window Store。")
        if store.runtime_health_store is not runtime_health_service.store:
            raise ValueError("Observation Window Store/Service 的 Health source 必须一致。")
        if store.harness_store is not harness_store:
            raise ValueError("Observation Window Store/Service 的 HAR source 必须一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if self.workspace_root != runtime_health_service.workspace_root:
            raise ValueError("Observation Window 与 Runtime Health workspace 必须一致。")
        self.runtime_health_service = runtime_health_service
        self.harness_store = harness_store
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def assess(
        self,
        *,
        completion_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationOptInObservationWindowView:
        subject = _subject(subject_id)
        lock = self._locks.setdefault((completion_id, subject), asyncio.Lock())
        async with lock:
            try:
                health_view = await self.runtime_health_service.inspect(
                    completion_id=completion_id
                )
            except EvolutionRevalidationOptInRuntimeHealthError as exc:
                raise EvolutionRevalidationOptInObservationAssessmentError(
                    "opt_in_observation_runtime_health_missing",
                    "缺少可供窗口评估的 Runtime Health Receipt。",
                ) from exc
            if not health_view.runtime_health_authority:
                raise EvolutionRevalidationOptInObservationAssessmentError(
                    "opt_in_observation_runtime_health_stale",
                    "Runtime Health 当前已失效，不能形成新窗口回执。",
                )
            try:
                binding, samples = await _read_current_ledger(
                    harness_store=self.harness_store,
                    workspace_root=self.workspace_root,
                    subject_id=subject,
                )
            except HarnessStoreError as exc:
                raise EvolutionRevalidationOptInObservationAssessmentError(
                    "opt_in_observation_ledger_unavailable",
                    "Runtime Release Observation ledger 当前不可读取。",
                ) from exc
            assessed_at = self._now().isoformat()
            try:
                window = build_opt_in_observation_window(
                    runtime_health=health_view.receipt,
                    binding=binding,
                    samples=samples,
                    assessed_at=assessed_at,
                )
            except EvolutionRevalidationOptInObservationWindowError as exc:
                raise EvolutionRevalidationOptInObservationAssessmentError(
                    exc.code,
                    str(exc),
                ) from exc
            latest = await self.store.latest(
                completion_id=completion_id,
                subject_id=subject,
            )
            stored = (
                latest
                if latest is not None and _same_material_assessment(latest, window)
                else await self.store.record(window)
            )
            return await self._view(stored, assessed_at=assessed_at)

    async def inspect(
        self,
        *,
        completion_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationOptInObservationWindowView:
        subject = _subject(subject_id)
        receipt = await self.store.latest(
            completion_id=completion_id,
            subject_id=subject,
        )
        if receipt is None:
            raise EvolutionRevalidationOptInObservationAssessmentError(
                "opt_in_observation_window_missing",
                "尚未形成 Opt-in Observation Window Receipt。",
            )
        return await self._view(receipt, assessed_at=self._now().isoformat())

    async def inspect_receipt(
        self,
        *,
        window_id: str,
    ) -> EvolutionRevalidationOptInObservationWindowView:
        receipt = await self.store.get(window_id)
        if receipt is None:
            raise EvolutionRevalidationOptInObservationAssessmentError(
                "opt_in_observation_window_missing",
                "指定的 Opt-in Observation Window Receipt 不存在。",
            )
        return await self._view(receipt, assessed_at=self._now().isoformat())

    async def _view(
        self,
        receipt: EvolutionRevalidationOptInObservationWindow,
        *,
        assessed_at: str,
    ) -> EvolutionRevalidationOptInObservationWindowView:
        completion_id = receipt.runtime_health.deployment.intent.admission.completion_id
        subject_id = receipt.binding.subject_id
        source = await self.store.get(receipt.window_id)
        latest = await self.store.latest(
            completion_id=completion_id,
            subject_id=subject_id,
        )
        window_source_current = source == receipt
        latest_receipt_current = latest == receipt
        try:
            health_source = await self.store.runtime_health_store.get_by_deployment(
                receipt.runtime_health.deployment.receipt_id
            )
            runtime_health_source_current = health_source == receipt.runtime_health
        except (aiosqlite.Error, OSError, TypeError, ValueError):
            runtime_health_source_current = False
        runtime_health_authority = False
        active_deployment_authority = False
        invalidation: list[str] = []
        try:
            health_view = await self.runtime_health_service.inspect(
                completion_id=completion_id
            )
            runtime_health_authority = bool(
                health_view.receipt == receipt.runtime_health
                and health_view.runtime_health_authority
            )
            active_deployment_authority = bool(
                health_view.receipt == receipt.runtime_health
                and health_view.active_deployment_authority
            )
        except EvolutionRevalidationOptInRuntimeHealthError:
            invalidation.append("runtime_health_unavailable")
        if not window_source_current:
            invalidation.append("window_source_changed")
        if not latest_receipt_current:
            invalidation.append("newer_window_exists")
        if not runtime_health_source_current:
            invalidation.append("runtime_health_source_changed")
        if not runtime_health_authority:
            invalidation.append("runtime_health_stale")
        if not active_deployment_authority:
            invalidation.append("active_deployment_stale")

        current_assessment = None
        release_binding_current = False
        observation_ledger_current = False
        try:
            binding, samples = await _read_current_ledger(
                harness_store=self.harness_store,
                workspace_root=self.workspace_root,
                subject_id=subject_id,
            )
            release_binding_current = binding == receipt.binding
            if not release_binding_current:
                invalidation.append("release_binding_changed")
            elif runtime_health_source_current:
                current_assessment = build_opt_in_observation_window(
                    runtime_health=receipt.runtime_health,
                    binding=binding,
                    samples=samples,
                    assessed_at=assessed_at,
                )
                observation_ledger_current = True
        except (
            HarnessStoreError,
            EvolutionRevalidationOptInObservationAssessmentError,
            EvolutionRevalidationOptInObservationWindowError,
        ):
            invalidation.append("observation_ledger_invalid")
        if not release_binding_current and "release_binding_changed" not in invalidation:
            invalidation.append("release_binding_changed")
        if not observation_ledger_current:
            invalidation.append("observation_ledger_stale")

        dependencies_current = bool(
            window_source_current
            and latest_receipt_current
            and runtime_health_source_current
            and runtime_health_authority
            and active_deployment_authority
            and release_binding_current
            and observation_ledger_current
            and current_assessment is not None
        )
        passing = bool(
            dependencies_current
            and current_assessment is not None
            and current_assessment.status
            is EvolutionRevalidationOptInObservationWindowStatus.PASSING
        )
        breached = bool(
            dependencies_current
            and current_assessment is not None
            and current_assessment.status
            is EvolutionRevalidationOptInObservationWindowStatus.BREACHED
        )
        return EvolutionRevalidationOptInObservationWindowView(
            receipt=receipt,
            current_assessment=current_assessment,
            window_source_current=window_source_current,
            latest_receipt_current=latest_receipt_current,
            runtime_health_source_current=runtime_health_source_current,
            runtime_health_authority=runtime_health_authority,
            active_deployment_authority=active_deployment_authority,
            release_binding_current=release_binding_current,
            observation_ledger_current=observation_ledger_current,
            invalidation_reasons=tuple(sorted(set(invalidation))),
            runtime_liveness_window_authority=passing,
            pause_input_authority=breached,
            rollback_input_authority=breached,
        )

    def _now(self) -> datetime:
        return _aware(self.clock())


async def _read_current_ledger(
    *,
    harness_store: HarnessStore,
    workspace_root: str | Path,
    subject_id: str,
):
    binding = await harness_store.get_runtime_release_binding(
        workspace_root=workspace_root,
        subject_id=subject_id,
    )
    if binding is None:
        raise EvolutionRevalidationOptInObservationAssessmentError(
            "opt_in_observation_binding_missing",
            "缺少 Runtime Release Binding。",
        )
    heartbeat = await harness_store.get_heartbeat(
        workspace_root=workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=subject_id,
    )
    if heartbeat is None:
        raise EvolutionRevalidationOptInObservationAssessmentError(
            "opt_in_observation_heartbeat_missing",
            "缺少 Runtime heartbeat head。",
        )
    origin_page = await harness_store.list_runtime_release_observations(
        workspace_root=workspace_root,
        subject_id=subject_id,
        after_sequence=0,
        limit=1,
    )
    if origin_page is None or not origin_page.items:
        raise EvolutionRevalidationOptInObservationAssessmentError(
            "opt_in_observation_samples_missing",
            "缺少 Runtime Release Observation samples。",
        )
    if not (
        origin_page.binding_id == binding.binding_id
        and origin_page.binding_sha256 == binding.binding_sha256
    ):
        raise EvolutionRevalidationOptInObservationAssessmentError(
            "opt_in_observation_ledger_binding_mismatch",
            "Runtime Release Observation origin 未绑定 exact Binding。",
        )
    origin = origin_page.items[0].chain_origin_sequence
    first_sequence = max(origin, heartbeat.sequence - _MAX_SAMPLES + 1)
    after_sequence = 0 if first_sequence == origin else first_sequence - 1
    samples = []
    while after_sequence < heartbeat.sequence:
        remaining_to_head = heartbeat.sequence - after_sequence
        page = await harness_store.list_runtime_release_observations(
            workspace_root=workspace_root,
            subject_id=subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, remaining_to_head),
        )
        if page is None or not (
            page.binding_id == binding.binding_id
            and page.binding_sha256 == binding.binding_sha256
        ):
            raise EvolutionRevalidationOptInObservationAssessmentError(
                "opt_in_observation_ledger_binding_mismatch",
                "Runtime Release Observation page 未绑定 exact Binding。",
            )
        samples.extend(page.items)
        if page.items and page.items[-1].heartbeat_sequence == heartbeat.sequence:
            break
        if not page.items or page.items[-1].heartbeat_sequence <= after_sequence:
            raise EvolutionRevalidationOptInObservationAssessmentError(
                "opt_in_observation_ledger_cursor_invalid",
                "Runtime Release Observation page cursor 未前进。",
            )
        after_sequence = page.items[-1].heartbeat_sequence
    if not samples:
        raise EvolutionRevalidationOptInObservationAssessmentError(
            "opt_in_observation_samples_missing",
            "缺少 Runtime Release Observation samples。",
        )
    heartbeat_after = await harness_store.get_heartbeat(
        workspace_root=workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=subject_id,
    )
    if heartbeat_after != heartbeat or not _heartbeat_matches_sample(
        heartbeat,
        samples[-1],
    ):
        raise EvolutionRevalidationOptInObservationAssessmentError(
            "opt_in_observation_ledger_head_changed",
            "Heartbeat head 与 Observation ledger 不一致。",
        )
    binding_after = await harness_store.get_runtime_release_binding(
        workspace_root=workspace_root,
        subject_id=subject_id,
    )
    if binding_after != binding:
        raise EvolutionRevalidationOptInObservationAssessmentError(
            "opt_in_observation_binding_changed",
            "Runtime Release Binding 在分页期间发生变化。",
        )
    return binding, tuple(samples)


async def _read_exact_sample_slice(
    *,
    harness_store: HarnessStore,
    window: EvolutionRevalidationOptInObservationWindow,
):
    expected = window.samples
    first = expected[0]
    after_sequence = (
        0
        if first.heartbeat_sequence == first.chain_origin_sequence
        else first.heartbeat_sequence - 1
    )
    actual = []
    while len(actual) < len(expected):
        page = await harness_store.list_runtime_release_observations(
            workspace_root=window.workspace_root,
            subject_id=window.binding.subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, len(expected) - len(actual)),
        )
        if page is None or not page.items:
            break
        actual.extend(page.items)
        after_sequence = page.items[-1].heartbeat_sequence
    return tuple(actual)


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


def _same_material_assessment(left, right) -> bool:
    return bool(
        left.runtime_health == right.runtime_health
        and left.binding == right.binding
        and left.samples == right.samples
        and left.status is right.status
        and left.insufficient_reasons == right.insufficient_reasons
        and left.breach_reasons == right.breach_reasons
    )


def _validated_window(value) -> EvolutionRevalidationOptInObservationWindow:
    try:
        return EvolutionRevalidationOptInObservationWindow.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInObservationAssessmentError(
            "opt_in_observation_window_invalid",
            "Opt-in Observation Window artifact 无效。",
        ) from exc


def _restore_window(value: str) -> EvolutionRevalidationOptInObservationWindow:
    try:
        return EvolutionRevalidationOptInObservationWindow.model_validate_json(value)
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationOptInObservationAssessmentError(
            "opt_in_observation_window_source_invalid",
            "Opt-in Observation Window durable source 无效。",
        ) from exc


def _subject(value: str) -> str:
    subject = value.strip() if isinstance(value, str) else ""
    if not _SUBJECT_RE.fullmatch(subject):
        raise ValueError("subject_id 必须是稳定的 runtime 标识。")
    return subject


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Observation Window assessment timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_opt_in_observation_windows (
            window_id TEXT PRIMARY KEY,
            window_sha256 TEXT NOT NULL,
            completion_id TEXT NOT NULL,
            health_receipt_id TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            status TEXT NOT NULL,
            assessed_at TEXT NOT NULL,
            window_json TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS "
        "idx_evolution_revalidation_opt_in_observation_windows_latest "
        "ON evolution_revalidation_opt_in_observation_windows "
        "(completion_id, subject_id, assessed_at DESC, window_id DESC)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_OPT_IN_OBSERVATION_ASSESSMENT_POLICY",
    "EvolutionRevalidationOptInObservationAssessmentError",
    "EvolutionRevalidationOptInObservationWindowService",
    "EvolutionRevalidationOptInObservationWindowStore",
    "EvolutionRevalidationOptInObservationWindowView",
]
