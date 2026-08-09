"""Release-bound runtime exposure evidence for percentage installations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_percentage_deployments import (
    EvolutionRevalidationPercentageDeploymentError,
    EvolutionRevalidationPercentageDeploymentReceipt,
    EvolutionRevalidationPercentageDeploymentService,
    EvolutionRevalidationPercentageDeploymentStore,
)
from naumi_agent.harness.heartbeat import (
    HarnessHeartbeatHealth,
    HarnessHeartbeatPhase,
    assess_heartbeat,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.runtime_release_binding import (
    HarnessRuntimeReleaseBinding,
)
from naumi_agent.harness.runtime_release_observation import (
    HarnessRuntimeReleaseObservation,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

EVOLUTION_REVALIDATION_PERCENTAGE_RUNTIME_EXPOSURE_POLICY = (
    "evolution-revalidation-percentage-runtime-exposure-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_SUBJECT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationPercentageRuntimeExposureReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-revalidation-percentage-runtime-exposure-v1"
    ] = EVOLUTION_REVALIDATION_PERCENTAGE_RUNTIME_EXPOSURE_POLICY
    exposure_id: str = Field(pattern=r"^evrepercentexposure_[0-9a-f]{24}$")
    exposure_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    deployment: EvolutionRevalidationPercentageDeploymentReceipt
    binding: HarnessRuntimeReleaseBinding
    startup_observation: HarnessRuntimeReleaseObservation
    ready_observation: HarnessRuntimeReleaseObservation
    exposure_kind: Literal["managed_terminal_runtime_ready"] = (
        "managed_terminal_runtime_ready"
    )
    surface: Literal["new_ui", "tui"]
    installation_exposure_observed: Literal[True] = True
    runtime_process_started: Literal[True] = True
    terminal_session_process: Literal[True] = True
    user_request_executed: Literal[False] = False
    completed_run_authority: Literal[False] = False
    percentage_observation_input_authority: Literal[True] = True
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    exposed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        deployment = self.deployment
        binding = self.binding
        identity = binding.runtime_identity
        if not (
            self.workspace_root == deployment.workspace_root == binding.workspace_root
            and self.workspace_root
            == str(Path(self.workspace_root).expanduser().resolve())
            and self.surface == binding.surface
            and deployment.population_assignment_enforced
        ):
            raise ValueError("Percentage Runtime Exposure source 投影不一致。")
        if not _identity_matches_deployment(identity, deployment):
            raise ValueError("Runtime Identity 未绑定 exact Percentage Deployment。")
        if not _startup_pair_matches(
            binding,
            self.startup_observation,
            self.ready_observation,
        ):
            raise ValueError("Percentage Runtime Exposure startup chain 无效。")
        if not (
            self.exposed_at == self.ready_observation.observed_at
            and _aware(deployment.activated_at)
            <= _aware(binding.runtime_identity.verified_at)
            <= _aware(binding.bound_at)
            <= _aware(self.startup_observation.observed_at)
            <= _aware(self.exposed_at)
        ):
            raise ValueError("Percentage Runtime Exposure 时间顺序无效。")
        core = self.model_dump(mode="json", exclude={"exposure_id", "exposure_sha256"})
        digest = _digest(core)
        if not (
            self.exposure_sha256 == digest
            and self.exposure_id == f"evrepercentexposure_{digest[:24]}"
        ):
            raise ValueError("Percentage Runtime Exposure identity 不一致。")
        return self


class EvolutionRevalidationPercentageRuntimeExposureView(_StrictModel):
    receipt: EvolutionRevalidationPercentageRuntimeExposureReceipt
    receipt_source_current: bool
    deployment_fact_current: bool
    deployment_launch_input_current: bool
    release_binding_current: bool
    startup_observations_current: bool
    runtime_heartbeat_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    runtime_exposure_fact_authority: bool
    current_runtime_exposure_authority: bool
    percentage_observation_input_authority: bool
    user_request_executed: Literal[False] = False
    completed_run_authority: Literal[False] = False
    percentage_rollout_authority: Literal[False] = False
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        fact = bool(
            self.receipt.installation_exposure_observed
            and self.receipt_source_current
            and self.deployment_fact_current
            and self.release_binding_current
            and self.startup_observations_current
        )
        current = bool(
            fact
            and self.deployment_launch_input_current
            and self.runtime_heartbeat_current
        )
        if not (
            self.runtime_exposure_fact_authority is fact
            and self.current_runtime_exposure_authority is current
            and self.percentage_observation_input_authority is current
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Percentage Runtime Exposure View authority 投影不一致。")
        return self


class EvolutionRevalidationPercentageRuntimeExposureError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationPercentageRuntimeExposureStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        deployment_store: EvolutionRevalidationPercentageDeploymentStore,
        harness_store: HarnessStore,
    ) -> None:
        if not isinstance(
            deployment_store,
            EvolutionRevalidationPercentageDeploymentStore,
        ):
            raise TypeError("Percentage Runtime Exposure Store 需要 Deployment Store。")
        if not isinstance(harness_store, HarnessStore):
            raise TypeError("Percentage Runtime Exposure Store 需要 HarnessStore。")
        self.db_path = Path(db_path).expanduser().resolve()
        if self.db_path != deployment_store.db_path:
            raise ValueError("Percentage Runtime Exposure 必须共用 Evolution evidence DB。")
        self.deployment_store = deployment_store
        self.harness_store = harness_store

    async def get_by_assignment(
        self,
        assignment_id: str,
    ) -> EvolutionRevalidationPercentageRuntimeExposureReceipt | None:
        _require_assignment_id(assignment_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT exposure_json FROM "
                        "evolution_revalidation_percentage_runtime_exposures "
                        "WHERE assignment_id = ?",
                        (assignment_id,),
                    )
                ).fetchone()
            return None if row is None else _restore(str(row["exposure_json"]))
        except EvolutionRevalidationPercentageRuntimeExposureError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageRuntimeExposureError(
                "percentage_runtime_exposure_source_invalid",
                "Percentage Runtime Exposure durable source 无效。",
            ) from exc

    async def record(
        self,
        receipt: EvolutionRevalidationPercentageRuntimeExposureReceipt,
    ) -> EvolutionRevalidationPercentageRuntimeExposureReceipt:
        item = _validated(receipt)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationPercentageRuntimeExposureError(
                "percentage_runtime_exposure_oversized",
                "Percentage Runtime Exposure Receipt 超过 16 MiB。",
            )
        await _require_harness_sources(self.harness_store, item)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_deployment_source(db, item.deployment)
                existing = await (
                    await db.execute(
                        "SELECT exposure_json FROM "
                        "evolution_revalidation_percentage_runtime_exposures "
                        "WHERE deployment_receipt_id = ? OR assignment_id = ?",
                        (
                            item.deployment.receipt_id,
                            item.deployment.preparation.intent.assignment.assignment_id,
                        ),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(str(existing["exposure_json"]))
                    await db.rollback()
                    if restored != item:
                        raise EvolutionRevalidationPercentageRuntimeExposureError(
                            "percentage_runtime_exposure_conflict",
                            "同一 Percentage Deployment 已绑定不同 Runtime Exposure。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_revalidation_percentage_runtime_exposures "
                    "(exposure_id, exposure_sha256, deployment_receipt_id, "
                    "assignment_id, binding_id, subject_id, exposure_json, exposed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.exposure_id,
                        item.exposure_sha256,
                        item.deployment.receipt_id,
                        item.deployment.preparation.intent.assignment.assignment_id,
                        item.binding.binding_id,
                        item.binding.subject_id,
                        encoded,
                        item.exposed_at,
                    ),
                )
                await db.commit()
        except EvolutionRevalidationPercentageRuntimeExposureError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionRevalidationPercentageRuntimeExposureError(
                "percentage_runtime_exposure_store_error",
                "Percentage Runtime Exposure Receipt 无法持久化。",
            ) from exc
        return item


class EvolutionRevalidationPercentageRuntimeExposureService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        deployment_service: EvolutionRevalidationPercentageDeploymentService,
        harness_store: HarnessStore,
        store: EvolutionRevalidationPercentageRuntimeExposureStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            isinstance(
                deployment_service,
                EvolutionRevalidationPercentageDeploymentService,
            )
            and isinstance(harness_store, HarnessStore)
            and isinstance(store, EvolutionRevalidationPercentageRuntimeExposureStore)
            and store.deployment_store is deployment_service.store
            and store.harness_store is harness_store
        ):
            raise ValueError("Percentage Runtime Exposure Service dependency 不一致。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if self.workspace_root != deployment_service.workspace_root:
            raise ValueError("Percentage Runtime Exposure workspace 必须一致。")
        self.deployment_service = deployment_service
        self.harness_store = harness_store
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def record(self, *, assignment_id: str, subject_id: str):
        _require_assignment_id(assignment_id)
        _require_subject_id(subject_id)
        lock = self._locks.setdefault(assignment_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_assignment(assignment_id)
            if existing is not None:
                if existing.binding.subject_id != subject_id:
                    raise EvolutionRevalidationPercentageRuntimeExposureError(
                        "percentage_runtime_exposure_already_recorded",
                        "该 Percentage Deployment 已绑定另一条 Runtime Exposure。",
                    )
                return await self._view(existing)
            try:
                deployment_view = await self.deployment_service.inspect(
                    assignment_id=assignment_id
                )
            except EvolutionRevalidationPercentageDeploymentError as exc:
                raise EvolutionRevalidationPercentageRuntimeExposureError(
                    "percentage_runtime_exposure_deployment_unavailable",
                    "缺少 current Percentage Deployment authority。",
                ) from exc
            if not deployment_view.percentage_runtime_launch_input_authority:
                raise EvolutionRevalidationPercentageRuntimeExposureError(
                    "percentage_runtime_exposure_deployment_denied",
                    "Percentage Deployment 当前不允许记录 runtime exposure。",
                )
            deployment = deployment_view.receipt
            binding, startup, ready = await _load_harness_sources(
                self.harness_store,
                workspace_root=self.workspace_root,
                subject_id=subject_id,
            )
            if not _identity_matches_deployment(binding.runtime_identity, deployment):
                raise EvolutionRevalidationPercentageRuntimeExposureError(
                    "percentage_runtime_exposure_release_mismatch",
                    "Managed runtime 未运行 exact Percentage Deployment。",
                )
            receipt = _build_receipt(
                workspace_root=self.workspace_root,
                deployment=deployment,
                binding=binding,
                startup=startup,
                ready=ready,
            )
            refreshed = await self.deployment_service.inspect(
                assignment_id=assignment_id
            )
            if not (
                refreshed.receipt == deployment
                and refreshed.percentage_runtime_launch_input_authority
            ):
                raise EvolutionRevalidationPercentageRuntimeExposureError(
                    "percentage_runtime_exposure_context_changed",
                    "记录期间 Percentage Deployment authority 已变化。",
                )
            stored = await self.store.record(receipt)
            return await self._view(stored)

    async def inspect(self, *, assignment_id: str):
        receipt = await self.store.get_by_assignment(assignment_id)
        if receipt is None:
            raise EvolutionRevalidationPercentageRuntimeExposureError(
                "percentage_runtime_exposure_missing",
                "指定 Percentage Assignment 尚无 Runtime Exposure Receipt。",
            )
        return await self._view(receipt)

    async def _view(self, receipt):
        assignment_id = receipt.deployment.preparation.intent.assignment.assignment_id
        receipt_source_current = False
        deployment_fact_current = False
        deployment_launch_input_current = False
        release_binding_current = False
        startup_observations_current = False
        runtime_heartbeat_current = False
        try:
            stored = await self.store.get_by_assignment(assignment_id)
            receipt_source_current = stored == receipt
        except EvolutionRevalidationPercentageRuntimeExposureError:
            pass
        try:
            deployment_view = await self.deployment_service.inspect(
                assignment_id=assignment_id
            )
            deployment_fact_current = bool(
                deployment_view.receipt == receipt.deployment
                and deployment_view.deployment_fact_authority
            )
            deployment_launch_input_current = bool(
                deployment_view.receipt == receipt.deployment
                and deployment_view.percentage_runtime_launch_input_authority
            )
        except EvolutionRevalidationPercentageDeploymentError:
            pass
        try:
            binding = await self.harness_store.get_runtime_release_binding(
                workspace_root=self.workspace_root,
                subject_id=receipt.binding.subject_id,
            )
            release_binding_current = binding == receipt.binding
        except (HarnessStoreError, OSError, TypeError, ValueError):
            pass
        try:
            _, startup, ready = await _load_harness_sources(
                self.harness_store,
                workspace_root=self.workspace_root,
                subject_id=receipt.binding.subject_id,
            )
            startup_observations_current = bool(
                startup == receipt.startup_observation
                and ready == receipt.ready_observation
            )
        except EvolutionRevalidationPercentageRuntimeExposureError:
            pass
        try:
            heartbeat = await self.harness_store.get_heartbeat(
                workspace_root=self.workspace_root,
                subject_kind=HarnessRunKind.RUNTIME,
                subject_id=receipt.binding.subject_id,
            )
            runtime_heartbeat_current = _heartbeat_current(
                heartbeat,
                binding=receipt.binding,
                ready=receipt.ready_observation,
                now=_aware(self.clock()),
            )
        except (HarnessStoreError, OSError, TypeError, ValueError):
            pass
        checks = {
            "receipt_source_changed": receipt_source_current,
            "deployment_fact_changed": deployment_fact_current,
            "deployment_launch_input_changed": deployment_launch_input_current,
            "release_binding_changed": release_binding_current,
            "startup_observations_changed": startup_observations_current,
            "runtime_heartbeat_not_current": runtime_heartbeat_current,
        }
        reasons = tuple(sorted(reason for reason, passed in checks.items() if not passed))
        fact = bool(
            receipt_source_current
            and deployment_fact_current
            and release_binding_current
            and startup_observations_current
        )
        current = bool(
            fact
            and deployment_launch_input_current
            and runtime_heartbeat_current
        )
        return EvolutionRevalidationPercentageRuntimeExposureView(
            receipt=receipt,
            receipt_source_current=receipt_source_current,
            deployment_fact_current=deployment_fact_current,
            deployment_launch_input_current=deployment_launch_input_current,
            release_binding_current=release_binding_current,
            startup_observations_current=startup_observations_current,
            runtime_heartbeat_current=runtime_heartbeat_current,
            invalidation_reasons=reasons,
            runtime_exposure_fact_authority=fact,
            current_runtime_exposure_authority=current,
            percentage_observation_input_authority=current,
        )


def _build_receipt(*, workspace_root, deployment, binding, startup, ready):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_PERCENTAGE_RUNTIME_EXPOSURE_POLICY,
        "workspace_root": str(workspace_root),
        "deployment": deployment,
        "binding": binding,
        "startup_observation": startup,
        "ready_observation": ready,
        "exposure_kind": "managed_terminal_runtime_ready",
        "surface": binding.surface,
        "installation_exposure_observed": True,
        "runtime_process_started": True,
        "terminal_session_process": True,
        "user_request_executed": False,
        "completed_run_authority": False,
        "percentage_observation_input_authority": True,
        "percentage_rollout_authority": False,
        "stable_rollout_authority": False,
        "promotion_authority": False,
        "exposed_at": ready.observed_at,
    }
    digest = _digest(core)
    return EvolutionRevalidationPercentageRuntimeExposureReceipt.model_validate(
        {
            **core,
            "exposure_id": f"evrepercentexposure_{digest[:24]}",
            "exposure_sha256": digest,
        }
    )


def _identity_matches_deployment(identity, deployment) -> bool:
    intent = deployment.preparation.intent
    pointer = deployment.activated_pointer
    slot = intent.archive_admission.installed_slot
    runtime_path = (Path(slot.bundle_dir) / slot.backend_path).resolve()
    install_root = Path(slot.bundle_dir).parents[1].resolve()
    return bool(
        identity.pointer_id == pointer.pointer_id
        and identity.pointer_sha256 == pointer.pointer_sha256
        and identity.pointer_generation == pointer.generation
        and identity.slot_id == intent.candidate_slot_id
        and identity.slot_sha256 == intent.candidate_slot_sha256
        and identity.version == intent.candidate_version
        and identity.target == intent.installation_target
        and identity.boot_receipt_id
        == deployment.preparation.boot_receipt.receipt_id
        and identity.boot_receipt_sha256
        == deployment.preparation.boot_receipt.receipt_sha256
        and identity.binary_sha256
        == deployment.preparation.boot_receipt.binary_sha256
        and Path(identity.runtime_path) == runtime_path
        and Path(identity.install_root) == install_root
        and identity.runtime_process_started
        and identity.terminal_session_process
        and not identity.health_probe_process
    )


def _startup_pair_matches(binding, startup, ready) -> bool:
    common = bool(
        startup.workspace_root == ready.workspace_root == binding.workspace_root
        and startup.binding_id == ready.binding_id == binding.binding_id
        and startup.binding_sha256 == ready.binding_sha256 == binding.binding_sha256
        and startup.runtime_identity_id
        == ready.runtime_identity_id
        == binding.runtime_identity.identity_id
        and startup.runtime_identity_sha256
        == ready.runtime_identity_sha256
        == binding.runtime_identity.identity_sha256
        and startup.surface == ready.surface == binding.surface
        and startup.subject_id == ready.subject_id == binding.subject_id
        and startup.instance_id == ready.instance_id == binding.instance_id
        and startup.epoch == ready.epoch == binding.epoch
    )
    return bool(
        common
        and startup.chain_origin_kind == "startup"
        and startup.chain_origin_sequence == 1
        and startup.heartbeat_sequence == 1
        and startup.previous_sample_sha256 == ""
        and startup.phase is HarnessHeartbeatPhase.STARTING
        and ready.chain_origin_kind == "startup"
        and ready.chain_origin_sequence == 1
        and ready.heartbeat_sequence == 2
        and ready.previous_sample_sha256 == startup.sample_sha256
        and ready.phase is HarnessHeartbeatPhase.RUNNING
        and _aware(startup.observed_at) <= _aware(ready.observed_at)
    )


def _heartbeat_current(heartbeat, *, binding, ready, now) -> bool:
    if heartbeat is None:
        return False
    if not (
        heartbeat.workspace_root == binding.workspace_root
        and heartbeat.subject_kind is HarnessRunKind.RUNTIME
        and heartbeat.subject_id == binding.subject_id
        and heartbeat.instance_id == binding.instance_id
        and heartbeat.epoch == binding.epoch
        and heartbeat.sequence >= ready.heartbeat_sequence
        and heartbeat.phase
        in {HarnessHeartbeatPhase.RUNNING, HarnessHeartbeatPhase.WAITING}
    ):
        return False
    return assess_heartbeat(
        heartbeat,
        now=now.isoformat(),
    ).health is HarnessHeartbeatHealth.HEALTHY


async def _load_harness_sources(
    harness_store,
    *,
    workspace_root,
    subject_id,
):
    try:
        binding = await harness_store.get_runtime_release_binding(
            workspace_root=workspace_root,
            subject_id=subject_id,
        )
        page = await harness_store.list_runtime_release_observations(
            workspace_root=workspace_root,
            subject_id=subject_id,
            after_sequence=0,
            limit=2,
        )
    except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationPercentageRuntimeExposureError(
            "percentage_runtime_exposure_harness_invalid",
            "Managed runtime binding/observation 无法验证。",
        ) from exc
    if binding is None:
        raise EvolutionRevalidationPercentageRuntimeExposureError(
            "percentage_runtime_exposure_binding_missing",
            "指定 runtime 没有 managed release binding。",
        )
    if page is None or len(page.items) < 2:
        raise EvolutionRevalidationPercentageRuntimeExposureError(
            "percentage_runtime_exposure_not_ready",
            "Managed runtime 尚未形成 startup → running 证据。",
        )
    startup, ready = page.items[:2]
    if not _startup_pair_matches(binding, startup, ready):
        raise EvolutionRevalidationPercentageRuntimeExposureError(
            "percentage_runtime_exposure_startup_invalid",
            "Managed runtime startup observation chain 无效。",
        )
    return binding, startup, ready


async def _require_harness_sources(harness_store, receipt) -> None:
    binding, startup, ready = await _load_harness_sources(
        harness_store,
        workspace_root=receipt.workspace_root,
        subject_id=receipt.binding.subject_id,
    )
    if not (
        binding == receipt.binding
        and startup == receipt.startup_observation
        and ready == receipt.ready_observation
    ):
        raise EvolutionRevalidationPercentageRuntimeExposureError(
            "percentage_runtime_exposure_harness_changed",
            "Runtime binding 或 startup observations 已变化。",
        )


async def _require_deployment_source(db, deployment) -> None:
    row = await (
        await db.execute(
            "SELECT receipt_json FROM evolution_revalidation_percentage_deployments "
            "WHERE receipt_id = ?",
            (deployment.receipt_id,),
        )
    ).fetchone()
    try:
        durable = (
            None
            if row is None
            else EvolutionRevalidationPercentageDeploymentReceipt.model_validate_json(
                row["receipt_json"]
            )
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionRevalidationPercentageRuntimeExposureError(
            "percentage_runtime_exposure_deployment_invalid",
            "Durable Percentage Deployment source 无效。",
        ) from exc
    if durable != deployment:
        raise EvolutionRevalidationPercentageRuntimeExposureError(
            "percentage_runtime_exposure_deployment_changed",
            "Durable Percentage Deployment source 已变化。",
        )


def _validated(value) -> EvolutionRevalidationPercentageRuntimeExposureReceipt:
    try:
        return EvolutionRevalidationPercentageRuntimeExposureReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionRevalidationPercentageRuntimeExposureError(
            "percentage_runtime_exposure_invalid",
            "Percentage Runtime Exposure Receipt artifact 无效。",
        ) from exc


def _restore(value: str) -> EvolutionRevalidationPercentageRuntimeExposureReceipt:
    if len(value.encode()) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Percentage Runtime Exposure Receipt 超过 16 MiB。")
    return EvolutionRevalidationPercentageRuntimeExposureReceipt.model_validate_json(
        value
    )


def _require_assignment_id(value) -> None:
    if not isinstance(value, str) or re.fullmatch(
        r"^evrepercentassign_[0-9a-f]{24}$",
        value,
    ) is None:
        raise EvolutionRevalidationPercentageRuntimeExposureError(
            "percentage_runtime_exposure_assignment_invalid",
            "Percentage Assignment identifier 无效。",
        )


def _require_subject_id(value) -> None:
    if not isinstance(value, str) or _SUBJECT_RE.fullmatch(value) is None:
        raise EvolutionRevalidationPercentageRuntimeExposureError(
            "percentage_runtime_exposure_subject_invalid",
            "Runtime subject identifier 无效。",
        )


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_revalidation_percentage_runtime_exposures ("
        "exposure_id TEXT PRIMARY KEY, exposure_sha256 TEXT NOT NULL UNIQUE, "
        "deployment_receipt_id TEXT NOT NULL UNIQUE, assignment_id TEXT NOT NULL UNIQUE, "
        "binding_id TEXT NOT NULL UNIQUE, subject_id TEXT NOT NULL, "
        "exposure_json TEXT NOT NULL, exposed_at TEXT NOT NULL)"
    )
    await db.commit()


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Percentage Runtime Exposure timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: item.model_dump(mode="json"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_PERCENTAGE_RUNTIME_EXPOSURE_POLICY",
    "EvolutionRevalidationPercentageRuntimeExposureError",
    "EvolutionRevalidationPercentageRuntimeExposureReceipt",
    "EvolutionRevalidationPercentageRuntimeExposureService",
    "EvolutionRevalidationPercentageRuntimeExposureStore",
    "EvolutionRevalidationPercentageRuntimeExposureView",
]
