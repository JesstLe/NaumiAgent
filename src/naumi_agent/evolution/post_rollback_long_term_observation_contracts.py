"""Frozen policy contract for post-rollback long-term observation."""

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

from naumi_agent.evolution.post_rollback_behavioral_matrix import (
    EvolutionPostRollbackBehavioralMatrix,
    EvolutionPostRollbackBehavioralMatrixError,
    EvolutionPostRollbackBehavioralMatrixService,
    EvolutionPostRollbackBehavioralMatrixStore,
)
from naumi_agent.evolution.post_rollback_runtime_verifications import (
    EvolutionPostRollbackRuntimeVerification,
    EvolutionPostRollbackRuntimeVerificationError,
    EvolutionPostRollbackRuntimeVerificationService,
    EvolutionPostRollbackRuntimeVerificationStore,
)

EVOLUTION_POST_ROLLBACK_LONG_TERM_OBSERVATION_CONTRACT_POLICY = (
    "evolution-post-rollback-long-term-observation-contract-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackLongTermObservationContract(_StrictModel):
    """Exact lineage and rules, without observation-window authority."""

    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-post-rollback-long-term-observation-contract-v1"
    ] = EVOLUTION_POST_ROLLBACK_LONG_TERM_OBSERVATION_CONTRACT_POLICY
    contract_id: str = Field(pattern=r"^evpostobservecontract_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    behavioral_matrix_id: str = Field(pattern=r"^evpostmatrix_[0-9a-f]{24}$")
    behavioral_matrix_sha256: str = Field(pattern=_SHA256_RE)
    runtime_verification_id: str = Field(pattern=r"^evpostrollback_[0-9a-f]{24}$")
    runtime_verification_sha256: str = Field(pattern=_SHA256_RE)
    before_after_evidence_id: str = Field(pattern=r"^evbeforeafter_[0-9a-f]{24}$")
    before_after_evidence_sha256: str = Field(pattern=_SHA256_RE)
    final_evaluation_id: str = Field(pattern=r"^evfinal_[0-9a-f]{24}$")
    final_evaluation_sha256: str = Field(pattern=_SHA256_RE)
    baseline_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    baseline_slot_sha256: str = Field(pattern=_SHA256_RE)
    baseline_manifest_sha256: str = Field(pattern=_SHA256_RE)
    baseline_version: str = Field(min_length=1, max_length=128)
    baseline_target: str = Field(min_length=1, max_length=128)
    rollback_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    rollback_pointer_sha256: str = Field(pattern=_SHA256_RE)
    rollback_pointer_generation: int = Field(ge=1)
    baseline_binary_sha256: str = Field(pattern=_SHA256_RE)
    verification_runtime_identity_sha256: str = Field(pattern=_SHA256_RE)
    eligible_surfaces: tuple[Literal["new_ui", "tui"], ...] = ("new_ui", "tui")
    required_chain_origin_kind: Literal["startup"] = "startup"
    required_chain_origin_sequence: Literal[1] = 1
    operational_phases: tuple[Literal["running", "waiting"], ...] = (
        "running",
        "waiting",
    )
    breach_phases: tuple[Literal["failed"], ...] = ("failed",)
    censor_phases: tuple[Literal["draining", "stopped"], ...] = (
        "draining",
        "stopped",
    )
    minimum_observation_seconds: Literal[3600] = 3600
    minimum_operational_samples: Literal[12] = 12
    maximum_sample_count: Literal[5000] = 5000
    gap_limit_rule: Literal["sample_timeout_seconds"] = "sample_timeout_seconds"
    latest_age_limit_rule: Literal["sample_timeout_seconds"] = (
        "sample_timeout_seconds"
    )
    require_constant_runtime_binding: Literal[True] = True
    require_contiguous_sequence_and_hash: Literal[True] = True
    observation_contract_recorded: Literal[True] = True
    long_term_metrics_recorded: Literal[False] = False
    observation_window_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    window_not_before_at: str = Field(min_length=1, max_length=100)
    recorded_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("长期观察契约 workspace 必须 canonical。")
        if self.eligible_surfaces != ("new_ui", "tui"):
            raise ValueError("长期观察契约必须覆盖 New UI 与 TUI runtime surface。")
        if self.operational_phases != ("running", "waiting"):
            raise ValueError("长期观察契约 operational phases 不完整。")
        if self.breach_phases != ("failed",) or self.censor_phases != (
            "draining",
            "stopped",
        ):
            raise ValueError("长期观察契约 breach/censoring 规则不完整。")
        if _aware(self.window_not_before_at) != _aware(self.recorded_at):
            raise ValueError("长期观察契约记录时间必须等于窗口最早取样时间。")
        if any(
            (
                self.long_term_metrics_recorded,
                self.observation_window_authority,
                self.learning_authority,
                self.promotion_authority,
                self.execution_authority,
            )
        ):
            raise ValueError("长期观察契约不得冒充指标、窗口、学习或执行 authority。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"contract_id", "contract_sha256"})
        )
        if not (
            hmac.compare_digest(self.contract_sha256, digest)
            and self.contract_id == f"evpostobservecontract_{digest[:24]}"
        ):
            raise ValueError("长期观察契约 content identity 不一致。")
        return self


class EvolutionPostRollbackLongTermObservationContractView(_StrictModel):
    contract: EvolutionPostRollbackLongTermObservationContract
    status: Literal["recorded", "stale"]
    durable_contract_valid: bool
    behavioral_matrix_authority: bool
    runtime_verification_authority: bool
    observation_contract_authority: bool
    observation_window_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.durable_contract_valid
            and self.behavioral_matrix_authority
            and self.runtime_verification_authority
        )
        if not (
            self.observation_contract_authority is expected
            and (self.status == "recorded") is expected
        ):
            raise ValueError("长期观察契约 authority projection 不一致。")
        return self


class EvolutionPostRollbackLongTermObservationContractError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackLongTermObservationContractStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve(strict=False)

    async def get_by_outcome(
        self,
        outcome_id: str,
    ) -> EvolutionPostRollbackLongTermObservationContract | None:
        outcome = _outcome_id(outcome_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_post_rollback_observation_contracts "
                        "WHERE outcome_id = ?",
                        (outcome,),
                    )
                ).fetchone()
            if row is None:
                return None
            item = _restore(row["contract_json"])
            if not (
                item.contract_id == row["contract_id"]
                and item.contract_sha256 == row["contract_sha256"]
                and item.request_id == row["request_id"]
                and item.behavioral_matrix_id == row["behavioral_matrix_id"]
                and item.runtime_verification_id == row["runtime_verification_id"]
            ):
                raise ValueError("observation contract row mismatch")
            return item
        except EvolutionPostRollbackLongTermObservationContractError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackLongTermObservationContractError(
                "post_rollback_observation_contract_store_corrupt",
                "Post-Rollback 长期观察契约损坏或无法读取。",
            ) from exc

    async def record(
        self,
        contract: EvolutionPostRollbackLongTermObservationContract,
    ) -> EvolutionPostRollbackLongTermObservationContract:
        item = _contract(contract)
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackLongTermObservationContractError(
                "post_rollback_observation_contract_oversized",
                "Post-Rollback 长期观察契约超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path, timeout=5.0) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                matrix = await (
                    await db.execute(
                        "SELECT matrix_sha256 FROM "
                        "evolution_post_rollback_behavioral_matrices "
                        "WHERE matrix_id = ? AND outcome_id = ?",
                        (item.behavioral_matrix_id, item.outcome_id),
                    )
                ).fetchone()
                verification = await (
                    await db.execute(
                        "SELECT verification_sha256 FROM "
                        "evolution_post_rollback_runtime_verifications "
                        "WHERE verification_id = ? AND outcome_id = ?",
                        (item.runtime_verification_id, item.outcome_id),
                    )
                ).fetchone()
                if not (
                    matrix is not None
                    and matrix["matrix_sha256"] == item.behavioral_matrix_sha256
                    and verification is not None
                    and verification["verification_sha256"]
                    == item.runtime_verification_sha256
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackLongTermObservationContractError(
                        "post_rollback_observation_contract_dependency_mismatch",
                        "长期观察契约缺少 exact durable Matrix 或 Runtime Verification。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT contract_json FROM "
                        "evolution_post_rollback_observation_contracts "
                        "WHERE outcome_id = ?",
                        (item.outcome_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["contract_json"])
                    await db.rollback()
                    if restored == item:
                        return restored
                    raise EvolutionPostRollbackLongTermObservationContractError(
                        "post_rollback_observation_contract_conflict",
                        "同一 Outcome 已绑定不同长期观察契约。",
                    )
                await db.execute(
                    "INSERT INTO evolution_post_rollback_observation_contracts "
                    "(contract_id, contract_sha256, outcome_id, request_id, "
                    "behavioral_matrix_id, runtime_verification_id, contract_json, "
                    "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.contract_id,
                        item.contract_sha256,
                        item.outcome_id,
                        item.request_id,
                        item.behavioral_matrix_id,
                        item.runtime_verification_id,
                        encoded,
                        item.recorded_at,
                    ),
                )
                await db.commit()
            return item
        except EvolutionPostRollbackLongTermObservationContractError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackLongTermObservationContractError(
                "post_rollback_observation_contract_store_failed",
                "Post-Rollback 长期观察契约无法持久化。",
            ) from exc


class EvolutionPostRollbackLongTermObservationContractService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        matrix_store: EvolutionPostRollbackBehavioralMatrixStore,
        matrix_service: EvolutionPostRollbackBehavioralMatrixService,
        runtime_verification_store: EvolutionPostRollbackRuntimeVerificationStore,
        runtime_verification_service: EvolutionPostRollbackRuntimeVerificationService,
        store: EvolutionPostRollbackLongTermObservationContractStore,
    ) -> None:
        if len(
            {
                matrix_store.db_path,
                runtime_verification_store.db_path,
                store.db_path,
            }
        ) != 1:
            raise ValueError("长期观察 authority stores 必须共享 session SQLite。")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.matrix_store = matrix_store
        self.matrix_service = matrix_service
        self.runtime_verification_store = runtime_verification_store
        self.runtime_verification_service = runtime_verification_service
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(
        self,
        *,
        request_id: str,
    ) -> EvolutionPostRollbackLongTermObservationContractView:
        request = _request_id(request_id)
        lock = self._locks.setdefault(request, asyncio.Lock())
        async with lock:
            matrix_view = await self.matrix_service.record(request_id=request)
            matrix = matrix_view.matrix
            if matrix.workspace_root != str(self.workspace_root):
                raise EvolutionPostRollbackLongTermObservationContractError(
                    "post_rollback_observation_contract_workspace_mismatch",
                    "Behavioral Matrix 不属于当前 workspace。",
                )
            verification = await self.runtime_verification_store.get_by_outcome(
                matrix.outcome_id
            )
            if verification is None:
                raise EvolutionPostRollbackLongTermObservationContractError(
                    "post_rollback_observation_contract_verification_missing",
                    "长期观察契约缺少 Runtime Verification。",
                )
            verification_view = await self.runtime_verification_service.inspect(
                verification=verification
            )
            if not (
                matrix_view.behavioral_evaluation_authority
                and verification_view.verification_authority
            ):
                raise EvolutionPostRollbackLongTermObservationContractError(
                    "post_rollback_observation_contract_dependency_stale",
                    "Behavioral Matrix 或 Runtime Verification authority 已失效。",
                )
            artifact = _build_contract(matrix, verification)
            existing = await self.store.get_by_outcome(matrix.outcome_id)
            if existing is None:
                existing = await self.store.record(artifact)
            elif existing != artifact:
                raise EvolutionPostRollbackLongTermObservationContractError(
                    "post_rollback_observation_contract_conflict",
                    "既有长期观察契约与当前 exact lineage 不一致。",
                )
            view = await self.inspect(contract=existing)
            if not view.observation_contract_authority:
                raise EvolutionPostRollbackLongTermObservationContractError(
                    "post_rollback_observation_contract_authority_changed",
                    "长期观察契约持久化期间 authority 已变化。",
                )
            return view

    async def inspect(
        self,
        *,
        contract: EvolutionPostRollbackLongTermObservationContract,
    ) -> EvolutionPostRollbackLongTermObservationContractView:
        item = _contract(contract)
        durable = matrix_authority = verification_authority = False
        try:
            stored = await self.store.get_by_outcome(item.outcome_id)
            matrix = await self.matrix_store.get_by_outcome(item.outcome_id)
            verification = await self.runtime_verification_store.get_by_outcome(
                item.outcome_id
            )
            if matrix is None or verification is None:
                raise ValueError("observation contract dependency missing")
            if not (
                matrix.workspace_root == str(self.workspace_root)
                and verification.workspace_root == str(self.workspace_root)
            ):
                raise ValueError("observation contract workspace mismatch")
            matrix_view = await self.matrix_service.inspect(matrix=matrix)
            verification_view = await self.runtime_verification_service.inspect(
                verification=verification
            )
            rebuilt = _build_contract(matrix, verification)
            durable = stored == item and rebuilt == item
            matrix_authority = matrix_view.behavioral_evaluation_authority
            verification_authority = verification_view.verification_authority
        except (
            EvolutionPostRollbackLongTermObservationContractError,
            EvolutionPostRollbackBehavioralMatrixError,
            EvolutionPostRollbackRuntimeVerificationError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            pass
        authority = bool(durable and matrix_authority and verification_authority)
        return EvolutionPostRollbackLongTermObservationContractView(
            contract=item,
            status="recorded" if authority else "stale",
            durable_contract_valid=durable,
            behavioral_matrix_authority=matrix_authority,
            runtime_verification_authority=verification_authority,
            observation_contract_authority=authority,
        )


def render_post_rollback_long_term_observation_contract(
    view: EvolutionPostRollbackLongTermObservationContractView,
) -> str:
    item = view.contract
    return "\n".join(
        [
            f"# Post-Rollback Long-Term Observation Contract `{item.contract_id}`",
            "",
            f"- 状态：`{view.status}`",
            f"- Rollback Outcome：`{item.outcome_id}`",
            f"- Behavioral Matrix：`{item.behavioral_matrix_id}`",
            f"- Runtime Verification：`{item.runtime_verification_id}`",
            f"- Baseline runtime：`{item.baseline_slot_id}` · `{item.baseline_target}`",
            f"- Surface：`{' / '.join(item.eligible_surfaces)}`",
            f"- 最短观察：`{item.minimum_observation_seconds}s`",
            f"- 最少运行样本：`{item.minimum_operational_samples}`",
            "- 最大间隙 / 最新样本年龄：不得超过每条 heartbeat 的 timeout",
            "- Censoring：`draining / stopped`；Breach：`failed`",
            "- Observation contract authority："
            f"`{str(view.observation_contract_authority).lower()}`",
            "- 长期指标 / Window authority：`false / false`",
            "- Learning / Promotion / Execution authority：`false / false / false`",
        ]
    )


def _build_contract(
    matrix: EvolutionPostRollbackBehavioralMatrix,
    verification: EvolutionPostRollbackRuntimeVerification,
) -> EvolutionPostRollbackLongTermObservationContract:
    if not (
        matrix.workspace_root == verification.workspace_root
        and matrix.outcome_id == verification.outcome_id
        and matrix.outcome_sha256 == verification.outcome_sha256
        and matrix.request_id == verification.request_id
        and matrix.runtime_verification_id == verification.verification_id
        and matrix.runtime_verification_sha256 == verification.verification_sha256
        and matrix.behavioral_evaluation_recorded
        and matrix.recovery_verdict == "recovered"
        and verification.runtime_identity_recovered
    ):
        raise EvolutionPostRollbackLongTermObservationContractError(
            "post_rollback_observation_contract_lineage_invalid",
            "长期观察契约要求 exact recovered Matrix/Runtime lineage。",
        )
    anchor = max(_aware(matrix.recorded_at), _aware(verification.verified_at)).isoformat()
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_LONG_TERM_OBSERVATION_CONTRACT_POLICY,
        "workspace_root": matrix.workspace_root,
        "outcome_id": matrix.outcome_id,
        "outcome_sha256": matrix.outcome_sha256,
        "request_id": matrix.request_id,
        "behavioral_matrix_id": matrix.matrix_id,
        "behavioral_matrix_sha256": matrix.matrix_sha256,
        "runtime_verification_id": verification.verification_id,
        "runtime_verification_sha256": verification.verification_sha256,
        "before_after_evidence_id": matrix.before_after_evidence_id,
        "before_after_evidence_sha256": matrix.before_after_evidence_sha256,
        "final_evaluation_id": matrix.final_evaluation_id,
        "final_evaluation_sha256": matrix.final_evaluation_sha256,
        "baseline_slot_id": verification.baseline_slot_id,
        "baseline_slot_sha256": verification.baseline_slot_sha256,
        "baseline_manifest_sha256": verification.baseline_manifest_sha256,
        "baseline_version": verification.baseline_version,
        "baseline_target": verification.baseline_target,
        "rollback_pointer_id": verification.rollback_pointer_id,
        "rollback_pointer_sha256": verification.rollback_pointer_sha256,
        "rollback_pointer_generation": verification.rollback_pointer_generation,
        "baseline_binary_sha256": verification.fresh_boot_receipt.binary_sha256,
        "verification_runtime_identity_sha256": verification.runtime_identity_sha256,
        "eligible_surfaces": ["new_ui", "tui"],
        "required_chain_origin_kind": "startup",
        "required_chain_origin_sequence": 1,
        "operational_phases": ["running", "waiting"],
        "breach_phases": ["failed"],
        "censor_phases": ["draining", "stopped"],
        "minimum_observation_seconds": 3600,
        "minimum_operational_samples": 12,
        "maximum_sample_count": 5000,
        "gap_limit_rule": "sample_timeout_seconds",
        "latest_age_limit_rule": "sample_timeout_seconds",
        "require_constant_runtime_binding": True,
        "require_contiguous_sequence_and_hash": True,
        "observation_contract_recorded": True,
        "long_term_metrics_recorded": False,
        "observation_window_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
        "window_not_before_at": anchor,
        "recorded_at": anchor,
    }
    digest = _digest(core)
    return EvolutionPostRollbackLongTermObservationContract.model_validate(
        {
            **core,
            "contract_id": f"evpostobservecontract_{digest[:24]}",
            "contract_sha256": digest,
        }
    )


def _contract(value) -> EvolutionPostRollbackLongTermObservationContract:
    try:
        return EvolutionPostRollbackLongTermObservationContract.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackLongTermObservationContractError(
            "post_rollback_observation_contract_invalid",
            "Post-Rollback 长期观察契约无效。",
        ) from exc


def _restore(raw: str) -> EvolutionPostRollbackLongTermObservationContract:
    if len(str(raw).encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("observation contract oversized")
    return EvolutionPostRollbackLongTermObservationContract.model_validate_json(raw)


def _request_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evrerollbackreq_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionPostRollbackLongTermObservationContractError(
            "post_rollback_observation_contract_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return normalized


def _outcome_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"^evrerollbackout_[0-9a-f]{24}$", normalized) is None:
        raise EvolutionPostRollbackLongTermObservationContractError(
            "post_rollback_observation_contract_outcome_id_invalid",
            "Rollback Outcome ID 格式无效。",
        )
    return normalized


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("长期观察契约时间必须包含 offset。")
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
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_observation_contracts ("
        "contract_id TEXT PRIMARY KEY, contract_sha256 TEXT NOT NULL UNIQUE, "
        "outcome_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL UNIQUE, "
        "behavioral_matrix_id TEXT NOT NULL UNIQUE, "
        "runtime_verification_id TEXT NOT NULL UNIQUE, "
        "contract_json TEXT NOT NULL, recorded_at TEXT NOT NULL);"
        "CREATE INDEX IF NOT EXISTS idx_post_rollback_observation_contract_request "
        "ON evolution_post_rollback_observation_contracts(request_id, recorded_at);"
    )


__all__ = [
    "EVOLUTION_POST_ROLLBACK_LONG_TERM_OBSERVATION_CONTRACT_POLICY",
    "EvolutionPostRollbackLongTermObservationContract",
    "EvolutionPostRollbackLongTermObservationContractError",
    "EvolutionPostRollbackLongTermObservationContractService",
    "EvolutionPostRollbackLongTermObservationContractStore",
    "EvolutionPostRollbackLongTermObservationContractView",
    "render_post_rollback_long_term_observation_contract",
]
