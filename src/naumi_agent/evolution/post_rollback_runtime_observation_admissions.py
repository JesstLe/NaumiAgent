"""Admission of one managed runtime into a post-rollback observation contract."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.post_rollback_long_term_observation_contracts import (
    EvolutionPostRollbackLongTermObservationContract,
    EvolutionPostRollbackLongTermObservationContractError,
    EvolutionPostRollbackLongTermObservationContractService,
    EvolutionPostRollbackLongTermObservationContractStore,
)
from naumi_agent.harness.runtime_release_binding import HarnessRuntimeReleaseBinding
from naumi_agent.harness.runtime_release_observation import (
    HarnessRuntimeReleaseObservation,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

EVOLUTION_POST_ROLLBACK_RUNTIME_OBSERVATION_ADMISSION_POLICY = (
    "evolution-post-rollback-runtime-observation-admission-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_IDENTITY_RE = r"^[a-z][a-z0-9_-]{0,95}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackRuntimeObservationAdmission(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-post-rollback-runtime-observation-admission-v1"
    ] = EVOLUTION_POST_ROLLBACK_RUNTIME_OBSERVATION_ADMISSION_POLICY
    admission_id: str = Field(pattern=r"^evpostobserveadmit_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    observation_contract_id: str = Field(
        pattern=r"^evpostobservecontract_[0-9a-f]{24}$"
    )
    observation_contract_sha256: str = Field(pattern=_SHA256_RE)
    binding_id: str = Field(pattern=r"^hrreleasebinding_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=_SHA256_RE)
    runtime_identity_id: str = Field(pattern=r"^relruntimeidentity_[0-9a-f]{24}$")
    runtime_identity_sha256: str = Field(pattern=_SHA256_RE)
    surface: Literal["new_ui", "tui"]
    subject_id: str = Field(pattern=_IDENTITY_RE)
    instance_id: str = Field(pattern=_IDENTITY_RE)
    epoch: int = Field(ge=1)
    slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    slot_sha256: str = Field(pattern=_SHA256_RE)
    version: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    pointer_sha256: str = Field(pattern=_SHA256_RE)
    pointer_generation: int = Field(ge=1)
    binary_sha256: str = Field(pattern=_SHA256_RE)
    origin_sample_id: str = Field(pattern=r"^hrreleaseobservation_[0-9a-f]{24}$")
    origin_sample_sha256: str = Field(pattern=_SHA256_RE)
    origin_sequence: Literal[1] = 1
    origin_kind: Literal["startup"] = "startup"
    origin_phase: Literal["starting"] = "starting"
    origin_observed_at: str = Field(min_length=1, max_length=100)
    timeout_seconds: int = Field(ge=3, le=86_400)
    exact_baseline_identity_matched: Literal[True] = True
    startup_origin_verified: Literal[True] = True
    runtime_observation_input_recorded: Literal[True] = True
    observation_window_authority: Literal[False] = False
    long_term_metrics_recorded: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    admitted_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Runtime Observation Admission workspace 必须 canonical。")
        if _aware(self.origin_observed_at) > _aware(self.admitted_at):
            raise ValueError("Runtime Observation Admission 不能早于 origin sample。")
        if any(
            (
                self.observation_window_authority,
                self.long_term_metrics_recorded,
                self.learning_authority,
                self.promotion_authority,
                self.execution_authority,
            )
        ):
            raise ValueError("Runtime Observation Admission 不得越权。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"admission_id", "admission_sha256"})
        )
        if not (
            hmac.compare_digest(self.admission_sha256, digest)
            and self.admission_id == f"evpostobserveadmit_{digest[:24]}"
        ):
            raise ValueError("Runtime Observation Admission content identity 不一致。")
        return self


class EvolutionPostRollbackRuntimeObservationAdmissionView(_StrictModel):
    admission: EvolutionPostRollbackRuntimeObservationAdmission
    status: Literal["admitted", "stale"]
    durable_admission_valid: bool
    observation_contract_authority: bool
    runtime_binding_authority: bool
    startup_origin_authority: bool
    exact_baseline_authority: bool
    runtime_observation_input_authority: bool
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_admission_valid
            and self.observation_contract_authority
            and self.runtime_binding_authority
            and self.startup_origin_authority
            and self.exact_baseline_authority
        )
        if not (
            self.runtime_observation_input_authority is expected
            and (self.status == "admitted") is expected
        ):
            raise ValueError("Runtime Observation Admission authority projection 不一致。")
        return self


class EvolutionPostRollbackRuntimeObservationAdmissionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackRuntimeObservationAdmissionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)

    async def get(
        self,
        outcome_id: str,
        subject_id: str,
    ) -> EvolutionPostRollbackRuntimeObservationAdmission | None:
        outcome = _outcome_id(outcome_id)
        subject = _subject_id(subject_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_runtime_admissions "
                        "WHERE outcome_id = ? AND subject_id = ?",
                        (outcome, subject),
                    )
                ).fetchone()
            if row is None:
                return None
            item = _restore(row["admission_json"])
            if not (
                item.admission_id == row["admission_id"]
                and item.admission_sha256 == row["admission_sha256"]
                and item.observation_contract_id == row["observation_contract_id"]
                and item.binding_id == row["binding_id"]
                and item.origin_sample_id == row["origin_sample_id"]
            ):
                raise ValueError("runtime admission row mismatch")
            return item
        except EvolutionPostRollbackRuntimeObservationAdmissionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRuntimeObservationAdmissionError(
                "post_rollback_runtime_admission_store_corrupt",
                "Post-Rollback Runtime Observation Admission 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        admission: EvolutionPostRollbackRuntimeObservationAdmission,
    ) -> EvolutionPostRollbackRuntimeObservationAdmission:
        item = _admission(admission)
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackRuntimeObservationAdmissionError(
                "post_rollback_runtime_admission_oversized",
                "Post-Rollback Runtime Observation Admission 超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                contract = await (
                    await db.execute(
                        "SELECT contract_sha256 FROM "
                        "evolution_post_rollback_observation_contracts "
                        "WHERE contract_id = ? AND outcome_id = ?",
                        (item.observation_contract_id, item.outcome_id),
                    )
                ).fetchone()
                if (
                    contract is None
                    or contract["contract_sha256"]
                    != item.observation_contract_sha256
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackRuntimeObservationAdmissionError(
                        "post_rollback_runtime_admission_contract_mismatch",
                        "Runtime Observation Admission 缺少 exact durable Contract。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT admission_json FROM "
                        "evolution_post_rollback_runtime_admissions "
                        "WHERE outcome_id = ? AND subject_id = ?",
                        (item.outcome_id, item.subject_id),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["admission_json"])
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionPostRollbackRuntimeObservationAdmissionError(
                        "post_rollback_runtime_admission_conflict",
                        "同一 Outcome/runtime subject 已绑定不同 Admission。",
                    )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_runtime_admissions "
                    "(admission_id, admission_sha256, outcome_id, request_id, "
                    "subject_id, observation_contract_id, binding_id, "
                    "origin_sample_id, admission_json, admitted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.admission_id,
                        item.admission_sha256,
                        item.outcome_id,
                        item.request_id,
                        item.subject_id,
                        item.observation_contract_id,
                        item.binding_id,
                        item.origin_sample_id,
                        encoded,
                        item.admitted_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionPostRollbackRuntimeObservationAdmissionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackRuntimeObservationAdmissionError(
                "post_rollback_runtime_admission_store_failed",
                "Post-Rollback Runtime Observation Admission 无法持久化。",
            ) from exc


class EvolutionPostRollbackRuntimeObservationAdmissionService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        contract_store: EvolutionPostRollbackLongTermObservationContractStore,
        contract_service: EvolutionPostRollbackLongTermObservationContractService,
        harness_store: HarnessStore,
        store: EvolutionPostRollbackRuntimeObservationAdmissionStore,
    ) -> None:
        if contract_store.db_path != store.db_path:
            raise ValueError("Runtime Admission 与 Contract 必须共享 session SQLite。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_store = contract_store
        self.contract_service = contract_service
        self.harness_store = harness_store
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(
        self,
        *,
        request_id: str,
        subject_id: str,
    ) -> EvolutionPostRollbackRuntimeObservationAdmissionView:
        request = _request_id(request_id)
        subject = _subject_id(subject_id)
        lock_key = f"{request}:{subject}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            contract_view = await self.contract_service.record(request_id=request)
            contract = contract_view.contract
            if not contract_view.observation_contract_authority:
                raise EvolutionPostRollbackRuntimeObservationAdmissionError(
                    "post_rollback_runtime_admission_contract_stale",
                    "长期观察契约 authority 已失效。",
                )
            binding, origin = await self._runtime_source(subject)
            artifact = _build_admission(contract, binding, origin)
            existing = await self.store.get(contract.outcome_id, subject)
            if existing is None:
                existing = await self.store.record(artifact)
            elif existing != artifact:
                raise EvolutionPostRollbackRuntimeObservationAdmissionError(
                    "post_rollback_runtime_admission_conflict",
                    "既有 Runtime Admission 与当前 contract/binding 不一致。",
                )
            view = await self.inspect(admission=existing)
            if not view.runtime_observation_input_authority:
                raise EvolutionPostRollbackRuntimeObservationAdmissionError(
                    "post_rollback_runtime_admission_authority_changed",
                    "Runtime Admission 持久化期间 authority 已变化。",
                )
            return view

    async def inspect(
        self,
        *,
        admission: EvolutionPostRollbackRuntimeObservationAdmission,
    ) -> EvolutionPostRollbackRuntimeObservationAdmissionView:
        item = _admission(admission)
        durable = contract_authority = binding_authority = False
        origin_authority = baseline_authority = False
        try:
            stored = await self.store.get(item.outcome_id, item.subject_id)
            contract = await self.contract_store.get_by_outcome(item.outcome_id)
            if contract is None:
                raise ValueError("runtime admission contract missing")
            contract_view = await self.contract_service.inspect(contract=contract)
            binding, origin = await self._runtime_source(item.subject_id)
            rebuilt = _build_admission(contract, binding, origin)
            durable = stored == item and rebuilt == item
            contract_authority = contract_view.observation_contract_authority
            binding_authority = binding.binding_id == item.binding_id
            origin_authority = origin.sample_id == item.origin_sample_id
            baseline_authority = _baseline_matches(contract, binding)
        except (
            EvolutionPostRollbackRuntimeObservationAdmissionError,
            EvolutionPostRollbackLongTermObservationContractError,
            HarnessStoreError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            pass
        authority = bool(
            durable
            and contract_authority
            and binding_authority
            and origin_authority
            and baseline_authority
        )
        return EvolutionPostRollbackRuntimeObservationAdmissionView(
            admission=item,
            status="admitted" if authority else "stale",
            durable_admission_valid=durable,
            observation_contract_authority=contract_authority,
            runtime_binding_authority=binding_authority,
            startup_origin_authority=origin_authority,
            exact_baseline_authority=baseline_authority,
            runtime_observation_input_authority=authority,
        )

    async def _runtime_source(
        self,
        subject_id: str,
    ) -> tuple[HarnessRuntimeReleaseBinding, HarnessRuntimeReleaseObservation]:
        binding = await self.harness_store.get_runtime_release_binding(
            workspace_root=self.workspace_root,
            subject_id=subject_id,
        )
        page = await self.harness_store.list_runtime_release_observations(
            workspace_root=self.workspace_root,
            subject_id=subject_id,
            after_sequence=0,
            limit=1,
        )
        if binding is None or page is None or len(page.items) != 1:
            raise EvolutionPostRollbackRuntimeObservationAdmissionError(
                "post_rollback_runtime_admission_source_missing",
                "找不到 exact managed runtime binding/startup origin。",
            )
        origin = page.items[0]
        if not (
            page.binding_id == binding.binding_id
            and page.binding_sha256 == binding.binding_sha256
            and origin.binding_id == binding.binding_id
            and origin.binding_sha256 == binding.binding_sha256
            and origin.runtime_identity_id == binding.runtime_identity.identity_id
            and origin.runtime_identity_sha256
            == binding.runtime_identity.identity_sha256
            and origin.surface == binding.surface
            and origin.subject_id == binding.subject_id
            and origin.instance_id == binding.instance_id
            and origin.epoch == binding.epoch
            and origin.chain_origin_kind == "startup"
            and origin.chain_origin_sequence == 1
            and origin.heartbeat_sequence == 1
            and origin.phase == "starting"
            and origin.previous_sample_sha256 == ""
        ):
            raise EvolutionPostRollbackRuntimeObservationAdmissionError(
                "post_rollback_runtime_admission_origin_invalid",
                "Managed runtime origin 不是 exact startup sequence-1 sample。",
            )
        return binding, origin


def render_post_rollback_runtime_observation_admission(
    view: EvolutionPostRollbackRuntimeObservationAdmissionView,
) -> str:
    item = view.admission
    return "\n".join(
        [
            f"# Post-Rollback Runtime Observation Admission `{item.admission_id}`",
            "",
            f"- 状态：`{view.status}`",
            f"- Observation Contract：`{item.observation_contract_id}`",
            f"- Runtime Binding：`{item.binding_id}`",
            f"- Surface / Subject：`{item.surface}` / `{item.subject_id}`",
            f"- Instance / Epoch：`{item.instance_id}` / `{item.epoch}`",
            f"- Baseline：`{item.slot_id}` · `{item.version}` · `{item.target}`",
            f"- Startup Origin：`{item.origin_sample_id}` · sequence `1`",
            f"- Heartbeat timeout：`{item.timeout_seconds}s`",
            "- Exact baseline identity："
            f"`{str(view.exact_baseline_authority).lower()}`",
            "- Runtime observation input authority："
            f"`{str(view.runtime_observation_input_authority).lower()}`",
            "- Window / Long-term metrics authority：`false / false`",
            "- Learning / Promotion / Execution authority：`false / false / false`",
        ]
    )


def _build_admission(
    contract: EvolutionPostRollbackLongTermObservationContract,
    binding: HarnessRuntimeReleaseBinding,
    origin: HarnessRuntimeReleaseObservation,
) -> EvolutionPostRollbackRuntimeObservationAdmission:
    if not _baseline_matches(contract, binding):
        raise EvolutionPostRollbackRuntimeObservationAdmissionError(
            "post_rollback_runtime_admission_baseline_mismatch",
            "Managed runtime identity 与回滚后的 exact baseline 不一致。",
        )
    if not (
        contract.workspace_root == binding.workspace_root == origin.workspace_root
        and binding.surface in contract.eligible_surfaces
        and origin.binding_id == binding.binding_id
        and origin.binding_sha256 == binding.binding_sha256
        and origin.sample_id
        and origin.chain_origin_kind == contract.required_chain_origin_kind
        and origin.chain_origin_sequence == contract.required_chain_origin_sequence
        and origin.heartbeat_sequence == 1
        and origin.phase == "starting"
        and origin.previous_sample_sha256 == ""
    ):
        raise EvolutionPostRollbackRuntimeObservationAdmissionError(
            "post_rollback_runtime_admission_lineage_invalid",
            "Runtime Binding/startup origin 与长期观察契约不一致。",
        )
    if _aware(origin.observed_at) < _aware(contract.window_not_before_at):
        raise EvolutionPostRollbackRuntimeObservationAdmissionError(
            "post_rollback_runtime_admission_origin_predates_contract",
            "当前 runtime 早于长期观察契约启动；请在契约生成后重启 Naumi，"
            "再准入新的 managed runtime subject。",
        )
    admitted = max(
        _aware(contract.recorded_at),
        _aware(binding.bound_at),
        _aware(origin.observed_at),
    ).isoformat()
    identity = binding.runtime_identity
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_RUNTIME_OBSERVATION_ADMISSION_POLICY,
        "workspace_root": contract.workspace_root,
        "outcome_id": contract.outcome_id,
        "outcome_sha256": contract.outcome_sha256,
        "request_id": contract.request_id,
        "observation_contract_id": contract.contract_id,
        "observation_contract_sha256": contract.contract_sha256,
        "binding_id": binding.binding_id,
        "binding_sha256": binding.binding_sha256,
        "runtime_identity_id": identity.identity_id,
        "runtime_identity_sha256": identity.identity_sha256,
        "surface": binding.surface,
        "subject_id": binding.subject_id,
        "instance_id": binding.instance_id,
        "epoch": binding.epoch,
        "slot_id": identity.slot_id,
        "slot_sha256": identity.slot_sha256,
        "version": identity.version,
        "target": identity.target,
        "pointer_id": identity.pointer_id,
        "pointer_sha256": identity.pointer_sha256,
        "pointer_generation": identity.pointer_generation,
        "binary_sha256": identity.binary_sha256,
        "origin_sample_id": origin.sample_id,
        "origin_sample_sha256": origin.sample_sha256,
        "origin_sequence": 1,
        "origin_kind": "startup",
        "origin_phase": "starting",
        "origin_observed_at": _aware(origin.observed_at).isoformat(),
        "timeout_seconds": origin.timeout_seconds,
        "exact_baseline_identity_matched": True,
        "startup_origin_verified": True,
        "runtime_observation_input_recorded": True,
        "observation_window_authority": False,
        "long_term_metrics_recorded": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
        "admitted_at": admitted,
    }
    digest = _digest(core)
    return EvolutionPostRollbackRuntimeObservationAdmission.model_validate(
        {
            **core,
            "admission_id": f"evpostobserveadmit_{digest[:24]}",
            "admission_sha256": digest,
        }
    )


def _baseline_matches(
    contract: EvolutionPostRollbackLongTermObservationContract,
    binding: HarnessRuntimeReleaseBinding,
) -> bool:
    identity = binding.runtime_identity
    return bool(
        contract.workspace_root == binding.workspace_root
        and identity.slot_id == contract.baseline_slot_id
        and identity.slot_sha256 == contract.baseline_slot_sha256
        and identity.version == contract.baseline_version
        and identity.target == contract.baseline_target
        and identity.pointer_id == contract.rollback_pointer_id
        and identity.pointer_sha256 == contract.rollback_pointer_sha256
        and identity.pointer_generation == contract.rollback_pointer_generation
        and identity.binary_sha256 == contract.baseline_binary_sha256
    )


def _admission(value) -> EvolutionPostRollbackRuntimeObservationAdmission:
    try:
        return EvolutionPostRollbackRuntimeObservationAdmission.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackRuntimeObservationAdmissionError(
            "post_rollback_runtime_admission_invalid",
            "Post-Rollback Runtime Observation Admission 无效。",
        ) from exc


def _restore(raw: str) -> EvolutionPostRollbackRuntimeObservationAdmission:
    if len(str(raw).encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("runtime admission oversized")
    return EvolutionPostRollbackRuntimeObservationAdmission.model_validate_json(raw)


def _request_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evrerollbackreq_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionPostRollbackRuntimeObservationAdmissionError(
            "post_rollback_runtime_admission_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return normalized


def _outcome_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evrerollbackout_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionPostRollbackRuntimeObservationAdmissionError(
            "post_rollback_runtime_admission_outcome_id_invalid",
            "Rollback Outcome ID 格式无效。",
        )
    return normalized


def _subject_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(_IDENTITY_RE, normalized) is None:
        raise EvolutionPostRollbackRuntimeObservationAdmissionError(
            "post_rollback_runtime_admission_subject_id_invalid",
            "Runtime subject ID 格式无效。",
        )
    return normalized


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("Runtime Observation Admission 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_runtime_admissions ("
        "admission_id TEXT PRIMARY KEY, admission_sha256 TEXT NOT NULL UNIQUE, "
        "outcome_id TEXT NOT NULL, request_id TEXT NOT NULL, "
        "subject_id TEXT NOT NULL, observation_contract_id TEXT NOT NULL, "
        "binding_id TEXT NOT NULL UNIQUE, origin_sample_id TEXT NOT NULL UNIQUE, "
        "admission_json TEXT NOT NULL, admitted_at TEXT NOT NULL, "
        "UNIQUE(outcome_id, subject_id));"
        "CREATE INDEX IF NOT EXISTS idx_post_rollback_runtime_admission_request "
        "ON evolution_post_rollback_runtime_admissions(request_id, admitted_at);"
    )


__all__ = [
    "EVOLUTION_POST_ROLLBACK_RUNTIME_OBSERVATION_ADMISSION_POLICY",
    "EvolutionPostRollbackRuntimeObservationAdmission",
    "EvolutionPostRollbackRuntimeObservationAdmissionError",
    "EvolutionPostRollbackRuntimeObservationAdmissionService",
    "EvolutionPostRollbackRuntimeObservationAdmissionStore",
    "EvolutionPostRollbackRuntimeObservationAdmissionView",
    "render_post_rollback_runtime_observation_admission",
]
