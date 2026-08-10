"""One exact installed-runtime behavioral lane after a governed rollback."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.post_rollback_runtime_verifications import (
    EvolutionPostRollbackRuntimeVerificationService,
)
from naumi_agent.evolution.proposal_before_after_evidence import (
    EvolutionProposalBeforeAfterEvidenceService,
    EvolutionProposalBeforeAfterLane,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcomeService,
)
from naumi_agent.harness.eval_identity import (
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalGuardrailStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalGuardrailResult,
    HarnessEvalSuiteResult,
    HarnessProtocolActual,
    HarnessProtocolExpected,
)
from naumi_agent.harness.eval_receipt import (
    EvalReceiptSample,
    HarnessEvalComparisonReceipt,
    build_eval_comparison_receipt,
    eval_sample_set_sha256,
)
from naumi_agent.harness.eval_statistics import EvalStatisticalVerdict
from naumi_agent.harness.eval_suite_compare import EvalMechanicalVerdict
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredEvalResult,
    HarnessStoreError,
)
from naumi_agent.release.runtime_eval import (
    ReleaseRuntimeEvalError,
    ReleaseRuntimeEvalReceipt,
    ReleaseRuntimeEvalRequest,
    build_protocol_hello_request,
)
from naumi_agent.release.slots import ReleaseInstalledSlot, ReleaseSlotError, ReleaseSlotStore

EVOLUTION_POST_ROLLBACK_BEHAVIORAL_LANE_POLICY = "evolution-post-rollback-behavioral-lane-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_BINDING_RE = r"^[^\x00\r\n]{1,128}$"
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackBehavioralLane(_StrictModel):
    """A fresh H5c lane; it is not the complete behavioral evaluation matrix."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-behavioral-lane-v1"] = (
        EVOLUTION_POST_ROLLBACK_BEHAVIORAL_LANE_POLICY
    )
    lane_id: str = Field(pattern=r"^evpostbehavior_[0-9a-f]{24}$")
    lane_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    workbench_session_id: str = Field(pattern=_SAFE_BINDING_RE)
    workbench_proposal_id: str = Field(pattern=_SAFE_BINDING_RE)
    runtime_verification_id: str = Field(pattern=r"^evpostrollback_[0-9a-f]{24}$")
    runtime_verification_sha256: str = Field(pattern=_SHA256_RE)
    runtime_verified_at: str = Field(min_length=1, max_length=100)
    before_after_evidence_id: str = Field(pattern=r"^evbeforeafter_[0-9a-f]{24}$")
    before_after_evidence_sha256: str = Field(pattern=_SHA256_RE)
    baseline_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    baseline_slot_sha256: str = Field(pattern=_SHA256_RE)
    baseline_manifest_sha256: str = Field(pattern=_SHA256_RE)
    baseline_source_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    baseline_source_tree_sha256: str = Field(pattern=_SHA256_RE)
    baseline_version: str = Field(min_length=1, max_length=128)
    baseline_target: str = Field(min_length=1, max_length=128)
    baseline_binary_sha256: str = Field(pattern=_SHA256_RE)
    lane_order: int = Field(ge=1, le=4)
    lane_kind: Literal["interventional", "adversarial"]
    platform: Literal["linux", "macos", "windows"]
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    suite_sha256: str = Field(pattern=_SHA256_RE)
    runner_version: Literal["protocol_hello@1"] = "protocol_hello@1"
    original_comparison_id: str = Field(pattern=_SHA256_RE)
    original_comparison_sha256: str = Field(pattern=_SHA256_RE)
    original_baseline_id: str = Field(pattern=_SHA256_RE)
    original_baseline_batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    original_baseline_samples_sha256: str = Field(pattern=_SHA256_RE)
    repetitions: int = Field(ge=5, le=100)
    runtime_eval_request: ReleaseRuntimeEvalRequest
    runtime_eval_receipts: tuple[ReleaseRuntimeEvalReceipt, ...] = Field(
        min_length=5,
        max_length=100,
    )
    fresh_batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    fresh_samples_sha256: str = Field(pattern=_SHA256_RE)
    fresh_comparison: HarnessEvalComparisonReceipt
    recovery_status: Literal["recovered", "changed", "inconclusive", "incompatible"]
    lane_evaluation_recorded: Literal[True] = True
    behavioral_evaluation_recorded: Literal[False] = False
    long_term_metrics_recorded: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    evaluated_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Post-Rollback Behavioral Lane workspace 必须 canonical。")
        if len(self.runtime_eval_receipts) != self.repetitions:
            raise ValueError("Post-Rollback Behavioral Lane repetitions 不完整。")
        receipt_ids = tuple(item.receipt_id for item in self.runtime_eval_receipts)
        if len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("Post-Rollback Behavioral Lane Runtime Receipt 不得重复。")
        if any(
            not (
                item.slot_id == self.baseline_slot_id
                and item.slot_sha256 == self.baseline_slot_sha256
                and item.manifest_sha256 == self.baseline_manifest_sha256
                and item.binary_sha256 == self.baseline_binary_sha256
                and item.request == self.runtime_eval_request
                and item.request.suite_id == self.suite_id
                and item.request.suite_sha256 == self.suite_sha256
            )
            for item in self.runtime_eval_receipts
        ):
            raise ValueError("Post-Rollback Runtime Receipt 与 lane/slot 不一致。")
        if any(
            _aware(item.evaluated_at) < _aware(self.runtime_verified_at)
            for item in self.runtime_eval_receipts
        ):
            raise ValueError("Post-Rollback Runtime Receipt 早于 6c1 Verification。")
        comparison = self.fresh_comparison
        if not (
            comparison.workspace_root == self.workspace_root
            and comparison.suite_id == self.suite_id
            and comparison.baseline_id == self.original_baseline_id
            and comparison.baseline_batch_id == self.original_baseline_batch_id
            and comparison.baseline_samples_sha256 == self.original_baseline_samples_sha256
            and comparison.current_batch_id == self.fresh_batch_id
            and comparison.current_samples == self.repetitions
            and comparison.current_samples_sha256 == self.fresh_samples_sha256
        ):
            raise ValueError("Post-Rollback fresh H5c 与原 baseline/fresh cohort 不一致。")
        expected_status = _recovery_status(comparison)
        if self.recovery_status != expected_status:
            raise ValueError("Post-Rollback recovery status 与 fresh H5c 不一致。")
        latest = max(_aware(item.evaluated_at) for item in self.runtime_eval_receipts)
        if _aware(self.evaluated_at) != max(latest, _aware(comparison.created_at)):
            raise ValueError("Post-Rollback Behavioral Lane evaluated_at 不一致。")
        if any(
            (
                self.behavioral_evaluation_recorded,
                self.long_term_metrics_recorded,
                self.learning_authority,
                self.promotion_authority,
            )
        ):
            raise ValueError("单个 Post-Rollback lane 不得越权。")
        digest = _digest(self.model_dump(mode="json", exclude={"lane_id", "lane_sha256"}))
        if not hmac.compare_digest(self.lane_sha256, digest):
            raise ValueError("Post-Rollback Behavioral Lane 摘要不一致。")
        if self.lane_id != f"evpostbehavior_{digest[:24]}":
            raise ValueError("Post-Rollback Behavioral Lane identity 不一致。")
        return self


class EvolutionPostRollbackBehavioralLaneView(_StrictModel):
    lane: EvolutionPostRollbackBehavioralLane
    durable_dependencies_valid: bool
    outcome_authority: bool
    runtime_verification_authority: bool
    before_after_authority: bool
    original_h5c_authority: bool
    fresh_runtime_authority: bool
    fresh_h5c_authority: bool
    lane_authority: bool
    active_baseline_authority: bool
    behavioral_evaluation_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _authority(self) -> Self:
        expected = all(
            (
                self.durable_dependencies_valid,
                self.outcome_authority,
                self.runtime_verification_authority,
                self.before_after_authority,
                self.original_h5c_authority,
                self.fresh_runtime_authority,
                self.fresh_h5c_authority,
            )
        )
        if self.lane_authority is not expected:
            raise ValueError("Post-Rollback Behavioral Lane authority 投影不一致。")
        if self.active_baseline_authority and not expected:
            raise ValueError("Post-Rollback Behavioral Lane active authority 越界。")
        return self


class EvolutionPostRollbackBehavioralLaneError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackBehavioralLaneStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        lane: EvolutionPostRollbackBehavioralLane,
    ) -> EvolutionPostRollbackBehavioralLane:
        try:
            item = EvolutionPostRollbackBehavioralLane.model_validate_json(lane.model_dump_json())
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_behavioral_lane_invalid",
                "Post-Rollback Behavioral Lane artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_behavioral_lane_oversized",
                "Post-Rollback Behavioral Lane 超过 2 MiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT lane_json FROM evolution_post_rollback_behavioral_lanes "
                        "WHERE outcome_id = ? AND original_comparison_id = ?",
                        (item.outcome_id, item.original_comparison_id),
                    )
                ).fetchone()
                if row is not None:
                    existing = _restore(row["lane_json"])
                    await db.rollback()
                    if not _same_lineage(existing, item):
                        raise EvolutionPostRollbackBehavioralLaneError(
                            "post_rollback_behavioral_lane_conflict",
                            "同一 Outcome/H5c 已绑定不同 Behavioral Lane lineage。",
                        )
                    return existing
                await db.execute(
                    "INSERT INTO evolution_post_rollback_behavioral_lanes "
                    "(lane_id, lane_sha256, outcome_id, request_id, "
                    "original_comparison_id, lane_json, evaluated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.lane_id,
                        item.lane_sha256,
                        item.outcome_id,
                        item.request_id,
                        item.original_comparison_id,
                        encoded,
                        item.evaluated_at,
                    ),
                )
                await db.commit()
        except EvolutionPostRollbackBehavioralLaneError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_behavioral_lane_store_error",
                "Post-Rollback Behavioral Lane 无法持久化。",
            ) from exc
        restored = await self.get(item.outcome_id, item.original_comparison_id)
        assert restored is not None
        return restored

    async def get(
        self,
        outcome_id: str,
        comparison_id: str,
    ) -> EvolutionPostRollbackBehavioralLane | None:
        if re.fullmatch(r"evrerollbackout_[0-9a-f]{24}", str(outcome_id)) is None:
            raise ValueError("Outcome ID 格式无效。")
        if re.fullmatch(_SHA256_RE, str(comparison_id)) is None:
            raise ValueError("Comparison ID 格式无效。")
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT lane_json FROM evolution_post_rollback_behavioral_lanes "
                        "WHERE outcome_id = ? AND original_comparison_id = ?",
                        (outcome_id, comparison_id),
                    )
                ).fetchone()
            return None if row is None else _restore(row["lane_json"])
        except EvolutionPostRollbackBehavioralLaneError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_behavioral_lane_store_corrupt",
                "Post-Rollback Behavioral Lane 损坏或无法读取。",
            ) from exc


class EvolutionPostRollbackBehavioralLaneService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        outcome_service: EvolutionRevalidationRollbackOutcomeService,
        runtime_verification_service: EvolutionPostRollbackRuntimeVerificationService,
        before_after_service: EvolutionProposalBeforeAfterEvidenceService,
        release_slot_store: ReleaseSlotStore,
        harness_store: HarnessStore,
        store: EvolutionPostRollbackBehavioralLaneStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.outcome_service = outcome_service
        self.runtime_verification_service = runtime_verification_service
        self.before_after_service = before_after_service
        self.release_slot_store = release_slot_store
        self.harness_store = harness_store
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(
        self,
        *,
        request_id: str,
        comparison_id: str,
    ) -> EvolutionPostRollbackBehavioralLaneView:
        request = _request_id(request_id)
        comparison = _comparison_id(comparison_id)
        lock = self._locks.setdefault(f"{request}:{comparison}", asyncio.Lock())
        async with lock:
            sources = await self._sources(
                request,
                comparison,
                require_active=True,
                create=True,
            )
            existing = await self.store.get(sources["outcome"].outcome_id, comparison)
            if existing is not None:
                view = await self.inspect(lane=existing)
                if not view.lane_authority:
                    raise EvolutionPostRollbackBehavioralLaneError(
                        "post_rollback_behavioral_lane_stale",
                        "既有 Post-Rollback Behavioral Lane authority 已失效。",
                    )
                return view
            lane = sources["selected_lane"]
            request_artifact = load_post_rollback_runtime_eval_request(
                self.workspace_root,
                lane.suite_id,
            )
            original, baseline, baseline_records = await self._original_h5c(lane)
            _validate_original_suite(request_artifact, original, baseline_records)
            receipts, fresh_records, fresh_comparison = await self._execute_fresh_h5c(
                sources=sources,
                lane=lane,
                request=request_artifact,
                original=original,
                baseline=baseline,
                baseline_records=baseline_records,
            )
            refreshed = await self._sources(
                request,
                comparison,
                require_active=True,
                create=True,
            )
            if not _source_identity_matches(sources, refreshed):
                raise EvolutionPostRollbackBehavioralLaneError(
                    "post_rollback_behavioral_authority_changed",
                    "Post-Rollback authority 在 fresh H5c 执行期间发生变化。",
                )
            artifact = _build_artifact(
                sources=sources,
                slot=sources["slot"],
                lane=lane,
                request=request_artifact,
                original=original,
                baseline_records=baseline_records,
                receipts=receipts,
                fresh_records=fresh_records,
                fresh_comparison=fresh_comparison,
            )
            recorded = await self.store.record(artifact)
            view = await self.inspect(lane=recorded)
            if not view.lane_authority:
                raise EvolutionPostRollbackBehavioralLaneError(
                    "post_rollback_behavioral_authority_changed",
                    "Post-Rollback Behavioral Lane 持久化后 authority 已变化。",
                )
            return view

    async def inspect(
        self,
        *,
        lane: EvolutionPostRollbackBehavioralLane,
    ) -> EvolutionPostRollbackBehavioralLaneView:
        item = EvolutionPostRollbackBehavioralLane.model_validate_json(lane.model_dump_json())
        flags = {
            "durable": False,
            "outcome": False,
            "verification": False,
            "before_after": False,
            "original": False,
            "runtime": False,
            "fresh": False,
            "active": False,
        }
        try:
            durable = await self.store.get(
                item.outcome_id,
                item.original_comparison_id,
            )
            sources = await self._sources(
                item.request_id,
                item.original_comparison_id,
                require_active=False,
                create=False,
            )
            original, baseline, baseline_records = await self._original_h5c(
                sources["selected_lane"]
            )
            _validate_original_suite(item.runtime_eval_request, original, baseline_records)
            runtime_valid = all(
                self.release_slot_store.get_runtime_eval_receipt(receipt.receipt_id) == receipt
                for receipt in item.runtime_eval_receipts
            )
            fresh = await self.harness_store.get_eval_comparison_receipt_by_id(
                self.workspace_root,
                item.fresh_comparison.id,
            )
            fresh_records = await self.harness_store.list_eval_results(
                self.workspace_root,
                item.fresh_batch_id,
                item.suite_id,
                limit=item.repetitions + 1,
            )
            rebuilt = _build_artifact(
                sources=sources,
                slot=sources["slot"],
                lane=sources["selected_lane"],
                request=item.runtime_eval_request,
                original=original,
                baseline_records=baseline_records,
                receipts=item.runtime_eval_receipts,
                fresh_records=fresh_records,
                fresh_comparison=item.fresh_comparison,
            )
            flags.update(
                {
                    "durable": durable == item and rebuilt == item,
                    "outcome": True,
                    "verification": True,
                    "before_after": True,
                    "original": bool(
                        original.receipt.id == item.original_comparison_id
                        and original.receipt.receipt_sha256 == item.original_comparison_sha256
                        and original.receipt.baseline_id == item.original_baseline_id
                        and original.receipt.baseline_samples_sha256
                        == item.original_baseline_samples_sha256
                    ),
                    "runtime": runtime_valid,
                    "fresh": bool(fresh is not None and fresh.receipt == item.fresh_comparison),
                    "active": bool(sources["active"]),
                }
            )
        except (
            EvolutionPostRollbackBehavioralLaneError,
            HarnessStoreError,
            ReleaseSlotError,
            OSError,
            TypeError,
            ValueError,
        ):
            pass
        authority = all(
            flags[key]
            for key in (
                "durable",
                "outcome",
                "verification",
                "before_after",
                "original",
                "runtime",
                "fresh",
            )
        )
        return EvolutionPostRollbackBehavioralLaneView(
            lane=item,
            durable_dependencies_valid=flags["durable"],
            outcome_authority=flags["outcome"],
            runtime_verification_authority=flags["verification"],
            before_after_authority=flags["before_after"],
            original_h5c_authority=flags["original"],
            fresh_runtime_authority=flags["runtime"],
            fresh_h5c_authority=flags["fresh"],
            lane_authority=authority,
            active_baseline_authority=bool(authority and flags["active"]),
            behavioral_evaluation_authority=False,
            learning_authority=False,
            promotion_authority=False,
        )

    async def _sources(
        self,
        request_id: str,
        comparison_id: str,
        *,
        require_active: bool,
        create: bool,
    ) -> dict[str, Any]:
        try:
            outcome_view = await self.outcome_service.inspect(request_id=request_id)
            outcome = outcome_view.outcome
            if create:
                verification_view = await self.runtime_verification_service.record(
                    request_id=request_id
                )
                before_after_view = await self.before_after_service.record(request_id=request_id)
            else:
                verification = await self.runtime_verification_service.store.get_by_outcome(
                    outcome.outcome_id
                )
                before_after = await self.before_after_service.evidence_store.get_by_outcome(
                    outcome.outcome_id
                )
                if verification is None or before_after is None:
                    raise ValueError("Behavioral Lane prerequisite missing")
                verification_view = await self.runtime_verification_service.inspect(
                    verification=verification
                )
                before_after_view = await self.before_after_service.inspect(evidence=before_after)
            verification = verification_view.verification
            before_after = before_after_view.evidence
            selected = tuple(
                item for item in before_after.lanes if item.comparison_id == comparison_id
            )
            if len(selected) != 1:
                raise ValueError("comparison 不属于 Proposal Before/After lanes")
            lane = selected[0]
            if lane.platform == "unknown":
                raise ValueError("unknown platform 不可执行 installed-runtime lane")
            if not (
                outcome.outcome_id == verification.outcome_id == before_after.outcome_id
                and outcome.outcome_sha256
                == verification.outcome_sha256
                == before_after.outcome_sha256
                and outcome.workbench_proposal_id
                == verification.workbench_proposal_id
                == before_after.workbench_proposal_id
            ):
                raise ValueError("Outcome/Verification/BeforeAfter lineage 不一致")
            slot = self.release_slot_store.inspect_installed_slot(verification.baseline_slot_id)
            if not (
                slot.slot_sha256 == verification.baseline_slot_sha256
                and slot.manifest_sha256 == verification.baseline_manifest_sha256
                and slot.version == verification.baseline_version
                and slot.target == verification.baseline_target
                and lane.platform == slot.target.split("-", 1)[0]
            ):
                raise ValueError("Verification/slot identity 不一致")
        except EvolutionPostRollbackBehavioralLaneError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, ReleaseSlotError) as exc:
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_behavioral_source_invalid",
                "Post-Rollback Behavioral Lane source 不完整或已损坏。",
            ) from exc
        if not all(
            (
                outcome_view.outcome_authority,
                verification_view.verification_authority,
                before_after_view.before_after_authority,
            )
        ):
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_behavioral_source_stale",
                "Outcome、Runtime Verification 或 Before/After authority 无效。",
            )
        active = bool(
            outcome_view.active_baseline_authority and verification_view.active_baseline_authority
        )
        if require_active and not active:
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_behavioral_baseline_not_active",
                "Baseline slot 当前不是 active runtime。",
            )
        return {
            "outcome": outcome,
            "verification": verification,
            "before_after": before_after,
            "selected_lane": lane,
            "slot": slot,
            "active": active,
        }

    async def _original_h5c(self, lane: EvolutionProposalBeforeAfterLane):
        original = await self.harness_store.get_eval_comparison_receipt_by_id(
            self.workspace_root,
            lane.comparison_id,
        )
        if original is None or original.receipt_sha256 != lane.comparison_receipt_sha256:
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_original_h5c_missing",
                "原 H5c Comparison 缺失或摘要不一致。",
            )
        receipt = original.receipt
        baseline = await self.harness_store.get_eval_baseline_by_batch(
            self.workspace_root,
            lane.suite_id,
            receipt.baseline_batch_id,
        )
        records = await self.harness_store.list_eval_results(
            self.workspace_root,
            receipt.baseline_batch_id,
            lane.suite_id,
            limit=receipt.baseline_samples + 1,
        )
        if baseline is None or not (
            baseline.id == receipt.baseline_id
            and baseline.samples_sha256 == receipt.baseline_samples_sha256
            and len(records) == receipt.baseline_samples
            and eval_sample_set_sha256(_samples(records)) == receipt.baseline_samples_sha256
        ):
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_original_baseline_invalid",
                "原 H5c baseline cohort 缺失、越界或已漂移。",
            )
        return original, baseline, records

    async def _execute_fresh_h5c(
        self,
        *,
        sources: dict[str, Any],
        lane: EvolutionProposalBeforeAfterLane,
        request: ReleaseRuntimeEvalRequest,
        original,
        baseline,
        baseline_records: tuple[HarnessStoredEvalResult, ...],
    ):
        repetitions = len(baseline_records)
        if not 5 <= repetitions <= 100:
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_repetitions_unsupported",
                "Post-Rollback H5c repetitions 必须在 5..100。",
            )
        receipts: list[ReleaseRuntimeEvalReceipt] = []
        results: list[HarnessEvalSuiteResult] = []
        first = baseline_records[0].result
        for _ in range(repetitions):
            try:
                runtime_receipt = await asyncio.to_thread(
                    self.release_slot_store.evaluate_runtime_protocol,
                    sources["slot"].slot_id,
                    request,
                )
                result = _runtime_result(
                    slot=sources["slot"],
                    request=request,
                    receipt=runtime_receipt,
                    baseline_result=first,
                    repetitions=repetitions,
                )
            except (ReleaseSlotError, OSError, TypeError, ValueError) as exc:
                raise EvolutionPostRollbackBehavioralLaneError(
                    "post_rollback_fresh_h5c_execution_failed",
                    "Installed runtime fresh H5c sample 执行失败。",
                ) from exc
            receipts.append(runtime_receipt)
            results.append(result)
        batch_id = _fresh_batch_id(
            sources["verification"].verification_id,
            lane.order,
            tuple(item.receipt_id for item in receipts),
        )
        records: list[HarnessStoredEvalResult] = []
        for index, (runtime_receipt, result) in enumerate(zip(receipts, results, strict=True)):
            try:
                stored = await self.harness_store.record_eval_result(
                    workspace_root=self.workspace_root,
                    batch_id=batch_id,
                    sample_index=index,
                    result=result,
                    created_at=runtime_receipt.evaluated_at,
                )
            except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
                raise EvolutionPostRollbackBehavioralLaneError(
                    "post_rollback_fresh_h5c_store_failed",
                    "Installed runtime fresh H5c sample 无法持久化。",
                ) from exc
            records.append(stored)
        built = build_eval_comparison_receipt(
            workspace_root=self.workspace_root,
            suite_id=lane.suite_id,
            baseline_id=baseline.id,
            baseline_batch_id=baseline.batch_id,
            baseline_samples_sha256=baseline.samples_sha256,
            baseline_samples=_samples(baseline_records),
            current_batch_id=batch_id,
            current_samples=_samples(tuple(records)),
            created_at=max(_aware(item.created_at) for item in records).isoformat(),
        )
        try:
            fresh = await self.harness_store.record_eval_comparison_receipt(built)
        except (HarnessStoreError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_fresh_h5c_store_failed",
                "Fresh post-rollback H5c Comparison 无法持久化。",
            ) from exc
        return tuple(receipts), tuple(records), fresh.receipt


def render_post_rollback_behavioral_lane(
    view: EvolutionPostRollbackBehavioralLaneView,
) -> str:
    item = view.lane
    return "\n".join(
        [
            f"# Post-Rollback Behavioral Lane `{item.lane_id}`",
            "",
            "**已在 exact installed baseline 上按原 H5c repetitions 重新执行单个平台 lane。**",
            "",
            f"- Proposal：`{item.workbench_proposal_id}`",
            f"- Outcome：`{item.outcome_id}`",
            f"- Suite：`{item.suite_id}` · {item.platform}",
            f"- 原 H5c：`{item.original_comparison_id}`",
            f"- Fresh H5c：`{item.fresh_comparison.id}`",
            f"- Samples：{item.repetitions}",
            f"- Recovery：{item.recovery_status}",
            f"- Lane authority：{'有效' if view.lane_authority else '无效'}",
            "",
            "- 行为级总体评测：尚未完成（仍需聚合全部 Final Evaluation lanes）",
            "- 长期指标：尚未记录",
            "- 权限：不授予 policy learning 或 promotion authority",
        ]
    )


def _runtime_result(
    *,
    slot: ReleaseInstalledSlot,
    request: ReleaseRuntimeEvalRequest,
    receipt: ReleaseRuntimeEvalReceipt,
    baseline_result: HarnessEvalSuiteResult,
    repetitions: int,
) -> HarnessEvalSuiteResult:
    baseline_identity = baseline_result.baseline_identity
    if baseline_identity is None or baseline_identity.model is not None:
        raise ValueError("原 H5c baseline 不是 no-model typed identity。")
    configuration = baseline_identity.configuration
    if not (
        configuration.suite_id == request.suite_id
        and configuration.suite_sha256 == request.suite_sha256
        and configuration.runner_version == request.runner_version
        and configuration.repetitions == repetitions
        and not configuration.live
        and baseline_result.policy_sha256 == configuration.policy_sha256
    ):
        raise ValueError("原 H5c configuration 与 Runtime Eval Request 不兼容。")
    source = HarnessEvalSourceIdentity(
        commit=slot.source_commit,
        tree_sha256=f"sha256:{slot.source_tree_sha256}",
        dirty=False,
    )
    identity = build_eval_baseline_identity(
        Path(slot.bundle_dir),
        configuration=configuration,
        platform_identity=receipt.response.runtime_platform,
        profile_trusted=baseline_identity.profile_trusted,
        source_identity=source,
    )
    baseline_cases = {item.case_id: item for item in baseline_result.cases}
    request_case_ids = tuple(item.case_id for item in request.cases)
    response_case_ids = tuple(item.case_id for item in receipt.response.results)
    if (
        len(baseline_cases) != len(baseline_result.cases)
        or set(baseline_cases) != set(request_case_ids)
        or request_case_ids != response_case_ids
    ):
        raise ValueError("原 H5c、Runtime Eval Request 与 Response case 集合不一致。")
    cases: list[HarnessEvalCaseResult] = []
    for expected_case, result in zip(
        request.cases,
        receipt.response.results,
        strict=True,
    ):
        baseline_case = baseline_cases[expected_case.case_id]
        guardrail_names = {item.guardrail for item in baseline_case.guardrails}
        if not (
            baseline_case.runner == "protocol_hello"
            and baseline_case.expected == expected_case.expected
            and baseline_case.primary_metric == "protocol_outcome_match"
            and not baseline_case.metric_observations
            and guardrail_names == {"no_model", "no_side_effect"}
        ):
            raise ValueError("原 H5c case 合同不是受支持的 protocol_hello@1 形态。")
        matched = _matches(expected_case.expected, result.actual)
        within_budget = result.duration_ms <= expected_case.max_duration_ms
        passed = matched and within_budget
        cases.append(
            HarnessEvalCaseResult(
                case_id=expected_case.case_id,
                runner=baseline_case.runner,
                status=(EvalCaseStatus.PASSED if passed else EvalCaseStatus.IMPLEMENTATION_FAILURE),
                expected=expected_case.expected,
                actual=result.actual,
                primary_metric=baseline_case.primary_metric,
                metric_observations=baseline_case.metric_observations,
                guardrails=(
                    HarnessEvalGuardrailResult(
                        guardrail="no_model",
                        status=(
                            EvalGuardrailStatus.PASSED
                            if result.no_model
                            else EvalGuardrailStatus.FAILED
                        ),
                    ),
                    HarnessEvalGuardrailResult(
                        guardrail="no_side_effect",
                        status=(
                            EvalGuardrailStatus.PASSED
                            if result.no_side_effect
                            else EvalGuardrailStatus.FAILED
                        ),
                    ),
                ),
                code=("" if passed else "installed_runtime_behavior_mismatch"),
                message=("" if passed else "Installed runtime 行为或 case budget 不匹配。"),
                duration_ms=result.duration_ms,
            )
        )
    status = (
        EvalRunStatus.PASSED
        if all(item.status is EvalCaseStatus.PASSED for item in cases)
        else EvalRunStatus.FAILED
    )
    return HarnessEvalSuiteResult(
        suite_id=request.suite_id,
        title=baseline_result.title,
        suite_path=baseline_result.suite_path,
        suite_sha256=request.suite_sha256,
        status=status,
        cases=tuple(cases),
        comparison_policy=baseline_result.comparison_policy,
        baseline_identity=identity,
        duration_ms=sum(item.duration_ms for item in cases),
    )


def _build_artifact(
    *,
    sources: dict[str, Any],
    slot: ReleaseInstalledSlot,
    lane: EvolutionProposalBeforeAfterLane,
    request: ReleaseRuntimeEvalRequest,
    original,
    baseline_records: tuple[HarnessStoredEvalResult, ...],
    receipts: tuple[ReleaseRuntimeEvalReceipt, ...],
    fresh_records: tuple[HarnessStoredEvalResult, ...],
    fresh_comparison: HarnessEvalComparisonReceipt,
) -> EvolutionPostRollbackBehavioralLane:
    verification = sources["verification"]
    outcome = sources["outcome"]
    before_after = sources["before_after"]
    if not (
        len(receipts) == len(fresh_records) == len(baseline_records) and receipts and fresh_records
    ):
        raise ValueError("Post-Rollback fresh receipt/sample coverage 不完整。")
    expected_batch = fresh_comparison.current_batch_id
    for index, (receipt, record) in enumerate(zip(receipts, fresh_records, strict=True)):
        expected_result = _runtime_result(
            slot=slot,
            request=request,
            receipt=receipt,
            baseline_result=baseline_records[0].result,
            repetitions=len(baseline_records),
        )
        if not (
            record.workspace_root == str(Path(outcome.workspace_root).resolve())
            and record.batch_id == expected_batch
            and record.suite_id == lane.suite_id
            and record.sample_index == index
            and record.result == expected_result
        ):
            raise ValueError("Post-Rollback Runtime Receipt 与 fresh H5a sample 不一致。")
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_BEHAVIORAL_LANE_POLICY,
        "workspace_root": str(Path(outcome.workspace_root).resolve()),
        "outcome_id": outcome.outcome_id,
        "outcome_sha256": outcome.outcome_sha256,
        "request_id": outcome.request_id,
        "workbench_session_id": outcome.workbench_session_id,
        "workbench_proposal_id": outcome.workbench_proposal_id,
        "runtime_verification_id": verification.verification_id,
        "runtime_verification_sha256": verification.verification_sha256,
        "runtime_verified_at": verification.verified_at,
        "before_after_evidence_id": before_after.evidence_id,
        "before_after_evidence_sha256": before_after.evidence_sha256,
        "baseline_slot_id": slot.slot_id,
        "baseline_slot_sha256": slot.slot_sha256,
        "baseline_manifest_sha256": slot.manifest_sha256,
        "baseline_source_commit": slot.source_commit,
        "baseline_source_tree_sha256": slot.source_tree_sha256,
        "baseline_version": slot.version,
        "baseline_target": slot.target,
        "baseline_binary_sha256": receipts[0].binary_sha256,
        "lane_order": lane.order,
        "lane_kind": lane.lane_kind,
        "platform": lane.platform,
        "suite_id": lane.suite_id,
        "suite_sha256": request.suite_sha256,
        "runner_version": request.runner_version,
        "original_comparison_id": original.receipt.id,
        "original_comparison_sha256": original.receipt.receipt_sha256,
        "original_baseline_id": original.receipt.baseline_id,
        "original_baseline_batch_id": original.receipt.baseline_batch_id,
        "original_baseline_samples_sha256": original.receipt.baseline_samples_sha256,
        "repetitions": len(baseline_records),
        "runtime_eval_request": request.model_dump(mode="json"),
        "runtime_eval_receipts": [item.model_dump(mode="json") for item in receipts],
        "fresh_batch_id": fresh_comparison.current_batch_id,
        "fresh_samples_sha256": eval_sample_set_sha256(_samples(fresh_records)),
        "fresh_comparison": fresh_comparison.model_dump(mode="json"),
        "recovery_status": _recovery_status(fresh_comparison),
        "lane_evaluation_recorded": True,
        "behavioral_evaluation_recorded": False,
        "long_term_metrics_recorded": False,
        "learning_authority": False,
        "promotion_authority": False,
        "evaluated_at": max(
            max(_aware(item.evaluated_at) for item in receipts),
            _aware(fresh_comparison.created_at),
        ).isoformat(),
    }
    digest = _digest(payload)
    return EvolutionPostRollbackBehavioralLane.model_validate(
        {
            **payload,
            "lane_id": f"evpostbehavior_{digest[:24]}",
            "lane_sha256": digest,
        }
    )


def _validate_original_suite(
    request: ReleaseRuntimeEvalRequest,
    original,
    baseline_records: tuple[HarnessStoredEvalResult, ...],
) -> None:
    first = baseline_records[0].result
    identity = first.baseline_identity
    if identity is None or not (
        original.receipt.suite_id == request.suite_id == first.suite_id
        and first.suite_sha256 == request.suite_sha256
        and identity.configuration.suite_sha256 == request.suite_sha256
        and identity.configuration.runner_version == request.runner_version
        and identity.configuration.repetitions == len(baseline_records)
        and identity.model is None
        and not identity.configuration.live
    ):
        raise EvolutionPostRollbackBehavioralLaneError(
            "post_rollback_suite_identity_incompatible",
            "原 H5c baseline identity 与 installed Runtime Eval Suite 不兼容。",
        )
    reference_cases = {item.case_id: item for item in first.cases}
    request_cases = {item.case_id: item for item in request.cases}
    if (
        len(reference_cases) != len(first.cases)
        or len(request_cases) != len(request.cases)
        or reference_cases.keys() != request_cases.keys()
    ):
        raise EvolutionPostRollbackBehavioralLaneError(
            "post_rollback_suite_case_contract_incompatible",
            "原 H5c baseline 与 installed Runtime Eval case 集合不兼容。",
        )
    for case_id, reference in reference_cases.items():
        request_case = request_cases[case_id]
        if not (
            reference.runner == "protocol_hello"
            and reference.expected == request_case.expected
            and reference.primary_metric == "protocol_outcome_match"
            and not reference.metric_observations
            and {item.guardrail for item in reference.guardrails} == {"no_model", "no_side_effect"}
        ):
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_suite_case_contract_incompatible",
                "原 H5c case 合同不是受支持的 protocol_hello@1 形态。",
            )
    for expected_index, record in enumerate(baseline_records):
        result = record.result
        cases = {item.case_id: item for item in result.cases}
        if not (
            record.sample_index == expected_index
            and result.suite_id == first.suite_id
            and result.suite_sha256 == first.suite_sha256
            and result.policy_sha256 == first.policy_sha256
            and result.comparison_policy == first.comparison_policy
            and result.baseline_identity == identity
            and not result.baseline_identity_code
            and cases.keys() == reference_cases.keys()
        ):
            raise EvolutionPostRollbackBehavioralLaneError(
                "post_rollback_original_baseline_contract_drift",
                "原 H5c baseline cohort 内部合同发生漂移。",
            )
        for case_id, case in cases.items():
            reference = reference_cases[case_id]
            if not (
                case.runner == reference.runner
                and case.expected == reference.expected
                and case.primary_metric == reference.primary_metric
                and case.metric_observations == reference.metric_observations
                and tuple(item.guardrail for item in case.guardrails)
                == tuple(item.guardrail for item in reference.guardrails)
            ):
                raise EvolutionPostRollbackBehavioralLaneError(
                    "post_rollback_original_baseline_contract_drift",
                    "原 H5c baseline sample case 合同发生漂移。",
                )


def load_post_rollback_runtime_eval_request(
    workspace: Path,
    suite_id: str,
) -> ReleaseRuntimeEvalRequest:
    root = workspace / "docs" / "harness" / "evals"
    if not root.is_dir():
        raise EvolutionPostRollbackBehavioralLaneError(
            "post_rollback_suite_registry_missing",
            "Workspace 缺少受信任 Eval Suite 目录。",
        )
    matches: list[ReleaseRuntimeEvalRequest] = []
    paths = sorted(root.glob("*.yaml"))
    if len(paths) > 100:
        raise EvolutionPostRollbackBehavioralLaneError(
            "post_rollback_suite_registry_oversized",
            "受信任 Eval Suite 数量超过 100。",
        )
    for path in paths:
        try:
            request = build_protocol_hello_request(workspace, path)
        except ReleaseRuntimeEvalError:
            continue
        if request.suite_id == suite_id:
            matches.append(request)
    if len(matches) != 1:
        raise EvolutionPostRollbackBehavioralLaneError(
            "post_rollback_suite_unsupported",
            "未找到唯一受支持的 protocol_hello Suite；禁止降级执行。",
        )
    return matches[0]


def _recovery_status(
    comparison: HarnessEvalComparisonReceipt,
) -> Literal["recovered", "changed", "inconclusive", "incompatible"]:
    mechanical = tuple(item.mechanical_verdict for item in comparison.sample_evidence)
    if (
        comparison.statistical_verdict is EvalStatisticalVerdict.INCOMPATIBLE
        or EvalMechanicalVerdict.INCOMPATIBLE in mechanical
    ):
        return "incompatible"
    if (
        comparison.statistical_verdict
        in {EvalStatisticalVerdict.INCONCLUSIVE, EvalStatisticalVerdict.FLAKY}
        or EvalMechanicalVerdict.INCONCLUSIVE in mechanical
    ):
        return "inconclusive"
    if comparison.statistical_verdict is EvalStatisticalVerdict.UNCHANGED and set(mechanical) == {
        EvalMechanicalVerdict.UNCHANGED
    }:
        return "recovered"
    return "changed"


def _samples(
    records: tuple[HarnessStoredEvalResult, ...],
) -> tuple[EvalReceiptSample, ...]:
    return tuple(
        EvalReceiptSample(
            sample_index=item.sample_index,
            result_sha256=item.result_sha256,
            result=item.result,
        )
        for item in records
    )


def _matches(expected: HarnessProtocolExpected, actual: HarnessProtocolActual) -> bool:
    return (
        actual.outcome == expected.outcome
        and actual.error_code == expected.error_code
        and actual.selected_version == expected.selected_version
        and actual.capabilities == expected.capabilities
    )


def _fresh_batch_id(
    verification_id: str,
    order: int,
    receipt_ids: tuple[str, ...],
) -> str:
    run_sha = _digest(receipt_ids)[:16]
    return f"postrollback:{verification_id.removeprefix('evpostrollback_')}:{order}:{run_sha}"


def _source_identity_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        (
            left["outcome"] == right["outcome"],
            left["verification"] == right["verification"],
            left["before_after"] == right["before_after"],
            left["selected_lane"] == right["selected_lane"],
            left["slot"] == right["slot"],
        )
    )


def _same_lineage(
    left: EvolutionPostRollbackBehavioralLane,
    right: EvolutionPostRollbackBehavioralLane,
) -> bool:
    return all(
        (
            left.workspace_root == right.workspace_root,
            left.outcome_id == right.outcome_id,
            left.outcome_sha256 == right.outcome_sha256,
            left.runtime_verification_id == right.runtime_verification_id,
            left.runtime_verification_sha256 == right.runtime_verification_sha256,
            left.before_after_evidence_id == right.before_after_evidence_id,
            left.before_after_evidence_sha256 == right.before_after_evidence_sha256,
            left.baseline_slot_id == right.baseline_slot_id,
            left.baseline_slot_sha256 == right.baseline_slot_sha256,
            left.original_comparison_id == right.original_comparison_id,
            left.original_comparison_sha256 == right.original_comparison_sha256,
            left.fresh_batch_id == right.fresh_batch_id,
        )
    )


def _request_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"evrerollbackreq_[0-9a-f]{24}", normalized) is None:
        raise EvolutionPostRollbackBehavioralLaneError(
            "post_rollback_behavioral_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return normalized


def _comparison_id(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(_SHA256_RE, normalized) is None:
        raise EvolutionPostRollbackBehavioralLaneError(
            "post_rollback_behavioral_comparison_id_invalid",
            "H5c Comparison ID 格式无效。",
        )
    return normalized


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_behavioral_lanes ("
        "lane_id TEXT PRIMARY KEY, lane_sha256 TEXT NOT NULL UNIQUE, "
        "outcome_id TEXT NOT NULL, request_id TEXT NOT NULL, "
        "original_comparison_id TEXT NOT NULL, lane_json TEXT NOT NULL, "
        "evaluated_at TEXT NOT NULL, "
        "UNIQUE(outcome_id, original_comparison_id));"
        "CREATE INDEX IF NOT EXISTS idx_post_rollback_behavioral_request "
        "ON evolution_post_rollback_behavioral_lanes(request_id, evaluated_at);"
    )


def _restore(raw: str) -> EvolutionPostRollbackBehavioralLane:
    if len(str(raw).encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise EvolutionPostRollbackBehavioralLaneError(
            "post_rollback_behavioral_lane_store_corrupt",
            "Post-Rollback Behavioral Lane 超过读取上限。",
        )
    try:
        return EvolutionPostRollbackBehavioralLane.model_validate_json(raw)
    except ValueError as exc:
        raise EvolutionPostRollbackBehavioralLaneError(
            "post_rollback_behavioral_lane_store_corrupt",
            "Post-Rollback Behavioral Lane 损坏或无法验证。",
        ) from exc


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EVOLUTION_POST_ROLLBACK_BEHAVIORAL_LANE_POLICY",
    "EvolutionPostRollbackBehavioralLane",
    "EvolutionPostRollbackBehavioralLaneError",
    "EvolutionPostRollbackBehavioralLaneService",
    "EvolutionPostRollbackBehavioralLaneStore",
    "EvolutionPostRollbackBehavioralLaneView",
    "load_post_rollback_runtime_eval_request",
    "render_post_rollback_behavioral_lane",
]
