"""Durable, dynamically revalidated stable runtime-window assessments."""

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

from naumi_agent.evolution.revalidation_stable_observation_windows import (
    EvolutionRevalidationStableObservationWindow,
    EvolutionRevalidationStableObservationWindowError,
    EvolutionRevalidationStableObservationWindowStatus,
    build_stable_observation_window,
)
from naumi_agent.evolution.revalidation_stable_runtime_exposures import (
    EvolutionRevalidationStableRuntimeExposureError,
    EvolutionRevalidationStableRuntimeExposureService,
    EvolutionRevalidationStableRuntimeExposureStore,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

EVOLUTION_REVALIDATION_STABLE_OBSERVATION_ASSESSMENT_POLICY = (
    "evolution-revalidation-stable-observation-assessment-v1"
)
_MAX_WINDOW_BYTES = 8 * 1024 * 1024
_MAX_SAMPLES = 5_000
_PAGE_LIMIT = 500
_INTENT_RE = re.compile(r"^evrestableintent_[0-9a-f]{24}$")
_SUBJECT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationStableObservationWindowView(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-stable-observation-assessment-v1"
    ] = EVOLUTION_REVALIDATION_STABLE_OBSERVATION_ASSESSMENT_POLICY
    receipt: EvolutionRevalidationStableObservationWindow
    current_assessment: EvolutionRevalidationStableObservationWindow | None = None
    window_source_current: bool
    latest_receipt_current: bool
    exposure_source_current: bool
    exposure_fact_authority: bool
    active_deployment_authority: bool
    deployment_launch_input_authority: bool
    current_runtime_exposure_authority: bool
    release_binding_current: bool
    observation_ledger_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=12)
    stable_runtime_window_authority: bool
    pause_input_authority: bool
    rollback_input_authority: bool
    completed_run_evidence_authority: Literal[False] = False
    population_observation_authority: Literal[False] = False
    stable_stage_completion_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        durable_dependencies = bool(
            self.window_source_current
            and self.latest_receipt_current
            and self.exposure_source_current
            and self.exposure_fact_authority
            and self.active_deployment_authority
            and self.release_binding_current
            and self.observation_ledger_current
            and self.current_assessment is not None
        )
        passing = bool(
            durable_dependencies
            and self.current_runtime_exposure_authority
            and self.current_assessment is not None
            and self.current_assessment.status
            is EvolutionRevalidationStableObservationWindowStatus.PASSING
            and self.current_assessment.stable_runtime_window_authority
        )
        breached = bool(
            durable_dependencies
            and self.current_assessment is not None
            and self.current_assessment.status
            is EvolutionRevalidationStableObservationWindowStatus.BREACHED
            and self.current_assessment.pause_input_authority
            and self.current_assessment.rollback_input_authority
        )
        if not (
            self.stable_runtime_window_authority is passing
            and self.pause_input_authority is breached
            and self.rollback_input_authority is breached
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Stable Observation Window View authority 投影不一致。")
        return self


class EvolutionRevalidationStableObservationAssessmentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationStableObservationWindowStore:
    """Append-only stable windows backed by exact Exposure and HAR facts."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        exposure_store: EvolutionRevalidationStableRuntimeExposureStore,
        harness_store: HarnessStore,
    ) -> None:
        if not isinstance(
            exposure_store,
            EvolutionRevalidationStableRuntimeExposureStore,
        ):
            raise TypeError("Stable Observation Store 需要 Runtime Exposure Store。")
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Stable Observation Store 需要 HarnessStore。")
        self.db_path = Path(db_path).expanduser().resolve()
        if self.db_path != exposure_store.db_path:
            raise ValueError("Stable Observation 与 Exposure 必须共用证据数据库。")
        self.exposure_store = exposure_store
        self.harness_store = harness_store

    async def get(
        self,
        window_id: str,
    ) -> EvolutionRevalidationStableObservationWindow | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT window_json FROM "
                        "evolution_revalidation_stable_observation_windows "
                        "WHERE window_id = ?",
                        (window_id,),
                    )
                ).fetchone()
            return None if row is None else _restore_window(str(row["window_json"]))
        except EvolutionRevalidationStableObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableObservationAssessmentError(
                "stable_observation_window_source_unavailable",
                "Stable Observation Window durable source 当前不可读取。",
            ) from exc

    async def latest(
        self,
        *,
        intent_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationStableObservationWindow | None:
        intent = _intent(intent_id)
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
                        "evolution_revalidation_stable_observation_windows "
                        "WHERE intent_id = ? AND subject_id = ? "
                        "ORDER BY assessed_at DESC, window_id DESC LIMIT 1",
                        (intent, subject),
                    )
                ).fetchone()
            return None if row is None else _restore_window(str(row["window_json"]))
        except EvolutionRevalidationStableObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableObservationAssessmentError(
                "stable_observation_window_source_unavailable",
                "Stable Observation Window durable source 当前不可读取。",
            ) from exc

    async def record(
        self,
        window: EvolutionRevalidationStableObservationWindow,
    ) -> EvolutionRevalidationStableObservationWindow:
        item = _validated_window(window)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_WINDOW_BYTES:
            raise EvolutionRevalidationStableObservationAssessmentError(
                "stable_observation_window_oversized",
                "Stable Observation Window 超过 8 MiB。",
            )
        await self._require_external_sources(item)
        intent_id = _window_intent_id(item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                exposure_row = await (
                    await db.execute(
                        "SELECT exposure_json FROM "
                        "evolution_revalidation_stable_runtime_exposures "
                        "WHERE intent_id = ?",
                        (intent_id,),
                    )
                ).fetchone()
                if exposure_row is None or not hmac.compare_digest(
                    str(exposure_row["exposure_json"]).encode(),
                    item.exposure.model_dump_json().encode(),
                ):
                    await db.rollback()
                    raise EvolutionRevalidationStableObservationAssessmentError(
                        "stable_observation_exposure_source_changed",
                        "Stable Runtime Exposure durable source 已变化或不存在。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT window_json FROM "
                        "evolution_revalidation_stable_observation_windows "
                        "WHERE window_id = ?",
                        (item.window_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore_window(str(existing["window_json"]))
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationStableObservationAssessmentError(
                            "stable_observation_window_identity_conflict",
                            "同一 Stable Window identity 已绑定不同内容。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_stable_observation_windows "
                    "(window_id, window_sha256, intent_id, exposure_id, "
                    "binding_id, subject_id, status, assessed_at, window_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.window_id,
                        item.window_sha256,
                        intent_id,
                        item.exposure.exposure_id,
                        item.exposure.binding.binding_id,
                        item.exposure.binding.subject_id,
                        item.status.value,
                        item.assessed_at,
                        encoded,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationStableObservationAssessmentError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationStableObservationAssessmentError(
                "stable_observation_window_store_error",
                "Stable Observation Window 无法持久化。",
            ) from exc
        return item

    async def _require_external_sources(
        self,
        item: EvolutionRevalidationStableObservationWindow,
    ) -> None:
        try:
            exposure = await self.exposure_store.get_by_intent(
                _window_intent_id(item)
            )
            if exposure != item.exposure:
                raise EvolutionRevalidationStableObservationAssessmentError(
                    "stable_observation_exposure_source_changed",
                    "Stable Runtime Exposure durable source 已变化或不存在。",
                )
            binding = await self.harness_store.get_runtime_release_binding(
                workspace_root=item.workspace_root,
                subject_id=item.exposure.binding.subject_id,
            )
            if binding != item.exposure.binding:
                raise EvolutionRevalidationStableObservationAssessmentError(
                    "stable_observation_binding_source_changed",
                    "Runtime Release Binding durable source 已变化或不存在。",
                )
            actual = await _read_exact_sample_slice(
                harness_store=self.harness_store,
                window=item,
            )
            if actual != item.samples:
                raise EvolutionRevalidationStableObservationAssessmentError(
                    "stable_observation_sample_source_changed",
                    "Runtime Release Observation durable source 与窗口不一致。",
                )
        except EvolutionRevalidationStableObservationAssessmentError:
            raise
        except (
            EvolutionRevalidationStableRuntimeExposureError,
            HarnessStoreError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionRevalidationStableObservationAssessmentError(
                "stable_observation_source_unavailable",
                "Stable Observation Window durable source 当前不可验证。",
            ) from exc


class EvolutionRevalidationStableObservationWindowService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        exposure_service: EvolutionRevalidationStableRuntimeExposureService,
        harness_store: HarnessStore,
        store: EvolutionRevalidationStableObservationWindowStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            isinstance(
                exposure_service,
                EvolutionRevalidationStableRuntimeExposureService,
            )
            and isinstance(harness_store, HarnessStore)
            and isinstance(store, EvolutionRevalidationStableObservationWindowStore)
            and store.exposure_store is exposure_service.store
            and store.harness_store is harness_store
        ):
            raise ValueError("Stable Observation Window Service dependency 不一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if self.workspace_root != exposure_service.workspace_root:
            raise ValueError("Stable Observation Window workspace 必须一致。")
        self.exposure_service = exposure_service
        self.harness_store = harness_store
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def assess(
        self,
        *,
        intent_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationStableObservationWindowView:
        intent = _intent(intent_id)
        subject = _subject(subject_id)
        lock = self._locks.setdefault((intent, subject), asyncio.Lock())
        async with lock:
            exposure_view = await self._inspect_exposure(intent)
            if not (
                exposure_view.runtime_exposure_fact_authority
                and exposure_view.active_deployment_current
            ):
                raise EvolutionRevalidationStableObservationAssessmentError(
                    "stable_observation_exposure_stale",
                    "Stable Runtime Exposure 当前不能形成新窗口回执。",
                )
            if exposure_view.receipt.binding.subject_id != subject:
                raise EvolutionRevalidationStableObservationAssessmentError(
                    "stable_observation_subject_mismatch",
                    "subject_id 未绑定 exact Stable Runtime Exposure。",
                )
            try:
                binding, samples = await _read_current_ledger(
                    harness_store=self.harness_store,
                    workspace_root=self.workspace_root,
                    subject_id=subject,
                )
            except HarnessStoreError as exc:
                raise EvolutionRevalidationStableObservationAssessmentError(
                    "stable_observation_ledger_unavailable",
                    "Runtime Release Observation ledger 当前不可读取。",
                ) from exc
            if binding != exposure_view.receipt.binding:
                raise EvolutionRevalidationStableObservationAssessmentError(
                    "stable_observation_binding_changed",
                    "Runtime Release Binding 已变化。",
                )
            assessed_at = self._now().isoformat()
            window = _build_window(
                exposure=exposure_view.receipt,
                samples=samples,
                assessed_at=assessed_at,
            )
            latest = await self.store.latest(
                intent_id=intent,
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
        intent_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationStableObservationWindowView:
        intent = _intent(intent_id)
        subject = _subject(subject_id)
        receipt = await self.store.latest(
            intent_id=intent,
            subject_id=subject,
        )
        if receipt is None:
            raise EvolutionRevalidationStableObservationAssessmentError(
                "stable_observation_window_missing",
                "尚未形成 Stable Observation Window Receipt。",
            )
        return await self._view(receipt, assessed_at=self._now().isoformat())

    async def inspect_receipt(
        self,
        *,
        window_id: str,
    ) -> EvolutionRevalidationStableObservationWindowView:
        receipt = await self.store.get(window_id)
        if receipt is None:
            raise EvolutionRevalidationStableObservationAssessmentError(
                "stable_observation_window_missing",
                "指定的 Stable Observation Window Receipt 不存在。",
            )
        return await self._view(receipt, assessed_at=self._now().isoformat())

    async def _inspect_exposure(self, intent_id: str):
        try:
            return await self.exposure_service.inspect(intent_id=intent_id)
        except EvolutionRevalidationStableRuntimeExposureError as exc:
            raise EvolutionRevalidationStableObservationAssessmentError(
                "stable_observation_exposure_missing",
                "缺少可供窗口评估的 Stable Runtime Exposure。",
            ) from exc

    async def _view(
        self,
        receipt: EvolutionRevalidationStableObservationWindow,
        *,
        assessed_at: str,
    ) -> EvolutionRevalidationStableObservationWindowView:
        intent_id = _window_intent_id(receipt)
        subject_id = receipt.exposure.binding.subject_id
        source = await self.store.get(receipt.window_id)
        latest = await self.store.latest(
            intent_id=intent_id,
            subject_id=subject_id,
        )
        window_source_current = source == receipt
        latest_receipt_current = latest == receipt
        try:
            exposure_source = await self.store.exposure_store.get_by_intent(
                intent_id
            )
            exposure_source_current = exposure_source == receipt.exposure
        except EvolutionRevalidationStableRuntimeExposureError:
            exposure_source_current = False
        exposure_fact_authority = False
        active_deployment_authority = False
        deployment_launch_input_authority = False
        current_runtime_exposure_authority = False
        invalidation: list[str] = []
        try:
            exposure_view = await self._inspect_exposure(intent_id)
            exact = exposure_view.receipt == receipt.exposure
            exposure_fact_authority = bool(
                exact and exposure_view.runtime_exposure_fact_authority
            )
            active_deployment_authority = bool(
                exact and exposure_view.active_deployment_current
            )
            deployment_launch_input_authority = bool(
                exact and exposure_view.deployment_launch_input_current
            )
            current_runtime_exposure_authority = bool(
                exact and exposure_view.current_runtime_exposure_authority
            )
        except EvolutionRevalidationStableObservationAssessmentError:
            invalidation.append("runtime_exposure_unavailable")
        checks = {
            "window_source_changed": window_source_current,
            "newer_window_exists": latest_receipt_current,
            "runtime_exposure_source_changed": exposure_source_current,
            "runtime_exposure_fact_stale": exposure_fact_authority,
            "active_deployment_stale": active_deployment_authority,
            "runtime_exposure_not_current": current_runtime_exposure_authority,
        }
        invalidation.extend(reason for reason, passed in checks.items() if not passed)

        current_assessment = None
        release_binding_current = False
        observation_ledger_current = False
        try:
            binding, samples = await _read_current_ledger(
                harness_store=self.harness_store,
                workspace_root=self.workspace_root,
                subject_id=subject_id,
            )
            release_binding_current = binding == receipt.exposure.binding
            if release_binding_current and exposure_source_current:
                current_assessment = _build_window(
                    exposure=receipt.exposure,
                    samples=samples,
                    assessed_at=assessed_at,
                )
                observation_ledger_current = True
        except (
            HarnessStoreError,
            EvolutionRevalidationStableObservationAssessmentError,
        ):
            invalidation.append("observation_ledger_invalid")
        if not release_binding_current:
            invalidation.append("release_binding_changed")
        if not observation_ledger_current:
            invalidation.append("observation_ledger_stale")

        durable_dependencies = bool(
            window_source_current
            and latest_receipt_current
            and exposure_source_current
            and exposure_fact_authority
            and active_deployment_authority
            and release_binding_current
            and observation_ledger_current
            and current_assessment is not None
        )
        passing = bool(
            durable_dependencies
            and current_runtime_exposure_authority
            and current_assessment is not None
            and current_assessment.status
            is EvolutionRevalidationStableObservationWindowStatus.PASSING
        )
        breached = bool(
            durable_dependencies
            and current_assessment is not None
            and current_assessment.status
            is EvolutionRevalidationStableObservationWindowStatus.BREACHED
        )
        return EvolutionRevalidationStableObservationWindowView(
            receipt=receipt,
            current_assessment=current_assessment,
            window_source_current=window_source_current,
            latest_receipt_current=latest_receipt_current,
            exposure_source_current=exposure_source_current,
            exposure_fact_authority=exposure_fact_authority,
            active_deployment_authority=active_deployment_authority,
            deployment_launch_input_authority=deployment_launch_input_authority,
            current_runtime_exposure_authority=current_runtime_exposure_authority,
            release_binding_current=release_binding_current,
            observation_ledger_current=observation_ledger_current,
            invalidation_reasons=tuple(sorted(set(invalidation))),
            stable_runtime_window_authority=passing,
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
        raise EvolutionRevalidationStableObservationAssessmentError(
            "stable_observation_binding_missing",
            "缺少 Runtime Release Binding。",
        )
    heartbeat = await harness_store.get_heartbeat(
        workspace_root=workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=subject_id,
    )
    if heartbeat is None:
        raise EvolutionRevalidationStableObservationAssessmentError(
            "stable_observation_heartbeat_missing",
            "缺少 Runtime heartbeat head。",
        )
    origin_page = await harness_store.list_runtime_release_observations(
        workspace_root=workspace_root,
        subject_id=subject_id,
        after_sequence=0,
        limit=1,
    )
    if origin_page is None or not origin_page.items:
        raise EvolutionRevalidationStableObservationAssessmentError(
            "stable_observation_samples_missing",
            "缺少 Runtime Release Observation samples。",
        )
    if not (
        origin_page.binding_id == binding.binding_id
        and origin_page.binding_sha256 == binding.binding_sha256
    ):
        raise EvolutionRevalidationStableObservationAssessmentError(
            "stable_observation_ledger_binding_mismatch",
            "Runtime Release Observation origin 未绑定 exact Binding。",
        )
    origin = origin_page.items[0].chain_origin_sequence
    first_sequence = max(origin, heartbeat.sequence - _MAX_SAMPLES + 1)
    after_sequence = 0 if first_sequence == origin else first_sequence - 1
    samples = []
    while after_sequence < heartbeat.sequence:
        page = await harness_store.list_runtime_release_observations(
            workspace_root=workspace_root,
            subject_id=subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, heartbeat.sequence - after_sequence),
        )
        if page is None or not (
            page.binding_id == binding.binding_id
            and page.binding_sha256 == binding.binding_sha256
        ):
            raise EvolutionRevalidationStableObservationAssessmentError(
                "stable_observation_ledger_binding_mismatch",
                "Runtime Release Observation page 未绑定 exact Binding。",
            )
        samples.extend(page.items)
        if page.items and page.items[-1].heartbeat_sequence == heartbeat.sequence:
            break
        if not page.items or page.items[-1].heartbeat_sequence <= after_sequence:
            raise EvolutionRevalidationStableObservationAssessmentError(
                "stable_observation_ledger_cursor_invalid",
                "Runtime Release Observation page cursor 未前进。",
            )
        after_sequence = page.items[-1].heartbeat_sequence
    if not samples:
        raise EvolutionRevalidationStableObservationAssessmentError(
            "stable_observation_samples_missing",
            "缺少 Runtime Release Observation samples。",
        )
    heartbeat_after = await harness_store.get_heartbeat(
        workspace_root=workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=subject_id,
    )
    binding_after = await harness_store.get_runtime_release_binding(
        workspace_root=workspace_root,
        subject_id=subject_id,
    )
    if heartbeat_after != heartbeat or not _heartbeat_matches_sample(
        heartbeat,
        samples[-1],
    ):
        raise EvolutionRevalidationStableObservationAssessmentError(
            "stable_observation_ledger_head_changed",
            "Heartbeat head 与 Observation ledger 不一致。",
        )
    if binding_after != binding:
        raise EvolutionRevalidationStableObservationAssessmentError(
            "stable_observation_binding_changed",
            "Runtime Release Binding 在分页期间发生变化。",
        )
    return binding, tuple(samples)


async def _read_exact_sample_slice(
    *,
    harness_store: HarnessStore,
    window: EvolutionRevalidationStableObservationWindow,
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
            subject_id=window.exposure.binding.subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, len(expected) - len(actual)),
        )
        if page is None or not page.items:
            break
        actual.extend(page.items)
        after_sequence = page.items[-1].heartbeat_sequence
    return tuple(actual)


def _build_window(*, exposure, samples, assessed_at):
    try:
        return build_stable_observation_window(
            exposure=exposure,
            samples=samples,
            assessed_at=assessed_at,
        )
    except EvolutionRevalidationStableObservationWindowError as exc:
        raise EvolutionRevalidationStableObservationAssessmentError(
            exc.code,
            str(exc),
        ) from exc


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
        left.exposure == right.exposure
        and left.samples == right.samples
        and left.status is right.status
        and left.insufficient_reasons == right.insufficient_reasons
        and left.breach_reasons == right.breach_reasons
    )


def _validated_window(value) -> EvolutionRevalidationStableObservationWindow:
    try:
        return EvolutionRevalidationStableObservationWindow.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableObservationAssessmentError(
            "stable_observation_window_invalid",
            "Stable Observation Window artifact 无效。",
        ) from exc


def _restore_window(value: str) -> EvolutionRevalidationStableObservationWindow:
    if len(value.encode()) > _MAX_WINDOW_BYTES:
        raise EvolutionRevalidationStableObservationAssessmentError(
            "stable_observation_window_source_oversized",
            "Stable Observation Window durable source 超过 8 MiB。",
        )
    try:
        return EvolutionRevalidationStableObservationWindow.model_validate_json(
            value
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationStableObservationAssessmentError(
            "stable_observation_window_source_invalid",
            "Stable Observation Window durable source 无效。",
        ) from exc


def _window_intent_id(window) -> str:
    return window.exposure.deployment.preparation.intent.intent_id


def _intent(value: str) -> str:
    if not isinstance(value, str) or _INTENT_RE.fullmatch(value) is None:
        raise ValueError("intent_id 必须是稳定的 Stable Deployment Intent 标识。")
    return value


def _subject(value: str) -> str:
    if not isinstance(value, str) or _SUBJECT_RE.fullmatch(value) is None:
        raise ValueError("subject_id 必须是稳定的 runtime 标识。")
    return value


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stable Observation timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_revalidation_stable_observation_windows (
            window_id TEXT PRIMARY KEY,
            window_sha256 TEXT NOT NULL,
            intent_id TEXT NOT NULL,
            exposure_id TEXT NOT NULL,
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
        "idx_evolution_revalidation_stable_observation_windows_latest "
        "ON evolution_revalidation_stable_observation_windows "
        "(intent_id, subject_id, assessed_at DESC, window_id DESC)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_STABLE_OBSERVATION_ASSESSMENT_POLICY",
    "EvolutionRevalidationStableObservationAssessmentError",
    "EvolutionRevalidationStableObservationWindowService",
    "EvolutionRevalidationStableObservationWindowStore",
    "EvolutionRevalidationStableObservationWindowView",
]
