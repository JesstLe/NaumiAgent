"""Exact lane coverage contract for post-rollback behavioral evaluation."""

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

from naumi_agent.evolution.post_rollback_behavioral_lanes import (
    EvolutionPostRollbackBehavioralLaneError,
    EvolutionPostRollbackBehavioralLaneService,
    EvolutionPostRollbackBehavioralLaneStore,
)
from naumi_agent.evolution.post_rollback_runtime_verifications import (
    EvolutionPostRollbackRuntimeVerificationService,
)
from naumi_agent.evolution.proposal_before_after_evidence import (
    EvolutionProposalBeforeAfterEvidenceService,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcomeService,
)

EVOLUTION_POST_ROLLBACK_BEHAVIORAL_COVERAGE_POLICY = (
    "evolution-post-rollback-behavioral-coverage-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_BINDING_RE = r"^[^\x00\r\n]{1,128}$"
_MAX_ARTIFACT_BYTES = 512 * 1024
type Platform = Literal["linux", "macos", "windows"]
type ExecutionScope = Literal["local_installed_baseline", "remote_target_required"]
type LaneStatus = Literal["recorded", "missing", "stale"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackBehavioralCoverageLane(_StrictModel):
    order: int = Field(ge=1, le=4)
    lane_kind: Literal["interventional", "adversarial"]
    platform: Platform
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    original_comparison_id: str = Field(pattern=_SHA256_RE)
    original_comparison_sha256: str = Field(pattern=_SHA256_RE)
    original_baseline_id: str = Field(pattern=_SHA256_RE)
    original_baseline_samples_sha256: str = Field(pattern=_SHA256_RE)
    execution_scope: ExecutionScope
    local_execution_eligible: bool
    remote_target_required: bool

    @model_validator(mode="after")
    def _scope_is_exact(self) -> Self:
        local = self.execution_scope == "local_installed_baseline"
        if not (
            self.local_execution_eligible is local and self.remote_target_required is (not local)
        ):
            raise ValueError("Post-Rollback Coverage lane execution scope 不一致。")
        return self


class EvolutionPostRollbackBehavioralCoverageContract(_StrictModel):
    """Immutable expected lane set; it is not a completed evaluation matrix."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-behavioral-coverage-v1"] = (
        EVOLUTION_POST_ROLLBACK_BEHAVIORAL_COVERAGE_POLICY
    )
    contract_id: str = Field(pattern=r"^evpostcoverage_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    workbench_session_id: str = Field(pattern=_SAFE_BINDING_RE)
    workbench_proposal_id: str = Field(pattern=_SAFE_BINDING_RE)
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
    baseline_platform: Platform
    baseline_source_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    baseline_source_tree_sha256: str = Field(pattern=_SHA256_RE)
    lane_count: int = Field(ge=2, le=4)
    local_lane_count: int = Field(ge=0, le=4)
    remote_lane_count: int = Field(ge=0, le=4)
    required_platforms: tuple[Platform, ...] = Field(min_length=1, max_length=3)
    lanes: tuple[EvolutionPostRollbackBehavioralCoverageLane, ...] = Field(
        min_length=2,
        max_length=4,
    )
    coverage_contract_recorded: Literal[True] = True
    behavioral_evaluation_recorded: Literal[False] = False
    execution_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    recorded_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Post-Rollback Coverage workspace 必须 canonical。")
        _aware(self.recorded_at)
        if not (
            self.lane_count == len(self.lanes)
            and tuple(item.order for item in self.lanes) == tuple(range(1, self.lane_count + 1))
            and self.lanes[0].lane_kind == "interventional"
            and all(item.lane_kind == "adversarial" for item in self.lanes[1:])
        ):
            raise ValueError("Post-Rollback Coverage lane 集不完整。")
        local_count = sum(item.local_execution_eligible for item in self.lanes)
        if not (
            self.local_lane_count == local_count
            and self.remote_lane_count == self.lane_count - local_count
            and self.required_platforms == tuple(item.platform for item in self.lanes[1:])
            and self.required_platforms == tuple(sorted(set(self.required_platforms)))
            and all(
                (item.platform == self.baseline_platform) is item.local_execution_eligible
                for item in self.lanes
            )
        ):
            raise ValueError("Post-Rollback Coverage platform projection 不一致。")
        if any(
            (
                self.behavioral_evaluation_recorded,
                self.execution_authority,
                self.learning_authority,
                self.promotion_authority,
            )
        ):
            raise ValueError("Coverage Contract 不得授予执行、学习或推广权限。")
        digest = _digest(self.model_dump(mode="json", exclude={"contract_id", "contract_sha256"}))
        if not (
            hmac.compare_digest(self.contract_sha256, digest)
            and self.contract_id == f"evpostcoverage_{digest[:24]}"
        ):
            raise ValueError("Post-Rollback Coverage Contract identity 不一致。")
        return self


class EvolutionPostRollbackBehavioralCoverageLaneView(_StrictModel):
    expected: EvolutionPostRollbackBehavioralCoverageLane
    status: LaneStatus
    lane_id: str | None = Field(default=None, pattern=r"^evpostbehavior_[0-9a-f]{24}$")
    lane_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    lane_authority: bool
    active_baseline_authority: bool
    dispatch_required: bool

    @model_validator(mode="after")
    def _projection(self) -> Self:
        recorded = self.status == "recorded"
        if not (
            (self.lane_id is not None) is (self.status != "missing")
            and (self.lane_sha256 is not None) is (self.status != "missing")
            and self.lane_authority is recorded
            and (not self.active_baseline_authority or recorded)
            and self.dispatch_required
            is (self.expected.remote_target_required and self.status == "missing")
        ):
            raise ValueError("Post-Rollback Coverage lane view 投影不一致。")
        return self


class EvolutionPostRollbackBehavioralCoverageView(_StrictModel):
    contract: EvolutionPostRollbackBehavioralCoverageContract
    lanes: tuple[EvolutionPostRollbackBehavioralCoverageLaneView, ...]
    durable_dependencies_valid: bool
    outcome_authority: bool
    runtime_verification_authority: bool
    before_after_authority: bool
    active_baseline_authority: bool
    recorded_lane_count: int = Field(ge=0, le=4)
    missing_lane_count: int = Field(ge=0, le=4)
    stale_lane_count: int = Field(ge=0, le=4)
    remote_dispatch_count: int = Field(ge=0, le=4)
    matrix_ready: bool
    behavioral_evaluation_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        statuses = tuple(item.status for item in self.lanes)
        authoritative = bool(
            self.durable_dependencies_valid
            and self.outcome_authority
            and self.runtime_verification_authority
            and self.before_after_authority
            and self.active_baseline_authority
        )
        expected_ready = bool(
            authoritative
            and len(self.lanes) == self.contract.lane_count
            and all(item.lane_authority for item in self.lanes)
        )
        if not (
            self.recorded_lane_count == statuses.count("recorded")
            and self.missing_lane_count == statuses.count("missing")
            and self.stale_lane_count == statuses.count("stale")
            and self.remote_dispatch_count == sum(item.dispatch_required for item in self.lanes)
            and self.matrix_ready is expected_ready
        ):
            raise ValueError("Post-Rollback Coverage View 聚合不一致。")
        return self


class EvolutionPostRollbackBehavioralCoverageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackBehavioralCoverageStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_by_outcome(
        self, outcome_id: str
    ) -> EvolutionPostRollbackBehavioralCoverageContract | None:
        _outcome_id(outcome_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT contract_json FROM "
                        "evolution_post_rollback_behavioral_coverage "
                        "WHERE outcome_id = ?",
                        (outcome_id,),
                    )
                ).fetchone()
            return None if row is None else _restore(row["contract_json"])
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackBehavioralCoverageError(
                "post_rollback_coverage_store_corrupt",
                "Post-Rollback Coverage Contract 损坏或无法读取。",
            ) from exc

    async def record(
        self, contract: EvolutionPostRollbackBehavioralCoverageContract
    ) -> EvolutionPostRollbackBehavioralCoverageContract:
        try:
            item = EvolutionPostRollbackBehavioralCoverageContract.model_validate_json(
                contract.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackBehavioralCoverageError(
                "post_rollback_coverage_invalid",
                "Post-Rollback Coverage Contract 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackBehavioralCoverageError(
                "post_rollback_coverage_oversized",
                "Post-Rollback Coverage Contract 超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                dependency = await (
                    await db.execute(
                        "SELECT evidence_sha256 FROM "
                        "evolution_proposal_before_after_evidence WHERE evidence_id = ?",
                        (item.before_after_evidence_id,),
                    )
                ).fetchone()
                if (
                    dependency is None
                    or dependency["evidence_sha256"] != item.before_after_evidence_sha256
                ):
                    await db.rollback()
                    raise EvolutionPostRollbackBehavioralCoverageError(
                        "post_rollback_coverage_dependency_mismatch",
                        "Coverage Contract 的 Before/After durable authority 不一致。",
                    )
                row = await (
                    await db.execute(
                        "SELECT contract_json FROM "
                        "evolution_post_rollback_behavioral_coverage "
                        "WHERE outcome_id = ?",
                        (item.outcome_id,),
                    )
                ).fetchone()
                if row is not None:
                    existing = _restore(row["contract_json"])
                    await db.rollback()
                    if existing != item:
                        raise EvolutionPostRollbackBehavioralCoverageError(
                            "post_rollback_coverage_conflict",
                            "同一 Outcome 已绑定不同 Coverage Contract。",
                        )
                    return existing
                await db.execute(
                    "INSERT INTO evolution_post_rollback_behavioral_coverage "
                    "(contract_id, contract_sha256, outcome_id, request_id, "
                    "contract_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.contract_id,
                        item.contract_sha256,
                        item.outcome_id,
                        item.request_id,
                        encoded,
                        item.recorded_at,
                    ),
                )
                await db.commit()
        except EvolutionPostRollbackBehavioralCoverageError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackBehavioralCoverageError(
                "post_rollback_coverage_store_error",
                "Post-Rollback Coverage Contract 无法持久化。",
            ) from exc
        return item


class EvolutionPostRollbackBehavioralCoverageService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        outcome_service: EvolutionRevalidationRollbackOutcomeService,
        runtime_verification_service: EvolutionPostRollbackRuntimeVerificationService,
        before_after_service: EvolutionProposalBeforeAfterEvidenceService,
        lane_store: EvolutionPostRollbackBehavioralLaneStore,
        lane_service: EvolutionPostRollbackBehavioralLaneService,
        store: EvolutionPostRollbackBehavioralCoverageStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.outcome_service = outcome_service
        self.runtime_verification_service = runtime_verification_service
        self.before_after_service = before_after_service
        self.lane_store = lane_store
        self.lane_service = lane_service
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def record(self, *, request_id: str) -> EvolutionPostRollbackBehavioralCoverageView:
        request = _request_id(request_id)
        lock = self._locks.setdefault(request, asyncio.Lock())
        async with lock:
            sources = await self._sources(request, create=True)
            existing = await self.store.get_by_outcome(sources["outcome"].outcome_id)
            if existing is None:
                artifact = _build_contract(sources=sources)
                existing = await self.store.record(artifact)
            view = await self.inspect(contract=existing)
            if not (
                view.durable_dependencies_valid
                and view.outcome_authority
                and view.runtime_verification_authority
                and view.before_after_authority
            ):
                raise EvolutionPostRollbackBehavioralCoverageError(
                    "post_rollback_coverage_authority_changed",
                    "Coverage Contract 持久化期间 authority 已变化。",
                )
            return view

    async def inspect(
        self,
        *,
        contract: EvolutionPostRollbackBehavioralCoverageContract,
    ) -> EvolutionPostRollbackBehavioralCoverageView:
        item = EvolutionPostRollbackBehavioralCoverageContract.model_validate_json(
            contract.model_dump_json()
        )
        flags = {
            "durable": False,
            "outcome": False,
            "verification": False,
            "before_after": False,
            "active": False,
        }
        try:
            durable = await self.store.get_by_outcome(item.outcome_id)
            sources = await self._sources(item.request_id, create=False)
            flags["durable"] = durable == item
            flags["outcome"] = bool(sources["outcome_authority"])
            flags["verification"] = bool(sources["verification_authority"])
            flags["before_after"] = bool(sources["before_after_authority"])
            flags["active"] = bool(sources["active"])
            if _build_contract(sources=sources, recorded_at=item.recorded_at) != item:
                flags = {key: False for key in flags}
        except (
            EvolutionPostRollbackBehavioralCoverageError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            flags = {key: False for key in flags}
        authoritative = all(flags.values())
        lane_views = tuple(
            [await self._lane_view(expected, outcome_id=item.outcome_id) for expected in item.lanes]
        )
        if not authoritative:
            lane_views = tuple(_stale_projection(value) for value in lane_views)
        statuses = tuple(value.status for value in lane_views)
        return EvolutionPostRollbackBehavioralCoverageView(
            contract=item,
            lanes=lane_views,
            durable_dependencies_valid=flags["durable"],
            outcome_authority=flags["outcome"],
            runtime_verification_authority=flags["verification"],
            before_after_authority=flags["before_after"],
            active_baseline_authority=flags["active"],
            recorded_lane_count=statuses.count("recorded"),
            missing_lane_count=statuses.count("missing"),
            stale_lane_count=statuses.count("stale"),
            remote_dispatch_count=sum(value.dispatch_required for value in lane_views),
            matrix_ready=bool(authoritative and all(value.lane_authority for value in lane_views)),
        )

    async def _lane_view(
        self,
        expected: EvolutionPostRollbackBehavioralCoverageLane,
        *,
        outcome_id: str,
    ) -> EvolutionPostRollbackBehavioralCoverageLaneView:
        try:
            lane = await self.lane_store.get(outcome_id, expected.original_comparison_id)
            if lane is None:
                return _missing_projection(expected)
            view = await self.lane_service.inspect(lane=lane)
            matches = bool(
                lane.lane_order == expected.order
                and lane.lane_kind == expected.lane_kind
                and lane.platform == expected.platform
                and lane.suite_id == expected.suite_id
                and lane.original_comparison_sha256 == expected.original_comparison_sha256
                and lane.original_baseline_id == expected.original_baseline_id
                and lane.original_baseline_samples_sha256
                == expected.original_baseline_samples_sha256
            )
            authority = bool(matches and view.lane_authority)
            return EvolutionPostRollbackBehavioralCoverageLaneView(
                expected=expected,
                status="recorded" if authority else "stale",
                lane_id=lane.lane_id,
                lane_sha256=lane.lane_sha256,
                lane_authority=authority,
                active_baseline_authority=bool(authority and view.active_baseline_authority),
                dispatch_required=False,
            )
        except (EvolutionPostRollbackBehavioralLaneError, OSError, TypeError, ValueError):
            if "lane" not in locals() or lane is None:
                return _missing_projection(expected)
            return EvolutionPostRollbackBehavioralCoverageLaneView(
                expected=expected,
                status="stale",
                lane_id=lane.lane_id,
                lane_sha256=lane.lane_sha256,
                lane_authority=False,
                active_baseline_authority=False,
                dispatch_required=False,
            )

    async def _sources(self, request_id: str, *, create: bool) -> dict[str, object]:
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
                    raise ValueError("Coverage prerequisite missing")
                verification_view = await self.runtime_verification_service.inspect(
                    verification=verification
                )
                before_after_view = await self.before_after_service.inspect(evidence=before_after)
            verification = verification_view.verification
            before_after = before_after_view.evidence
            if not (
                outcome.outcome_id == verification.outcome_id == before_after.outcome_id
                and outcome.outcome_sha256
                == verification.outcome_sha256
                == before_after.outcome_sha256
                and outcome.workbench_proposal_id
                == verification.workbench_proposal_id
                == before_after.workbench_proposal_id
            ):
                raise ValueError("Coverage lineage mismatch")
            platform = _target_platform(verification.baseline_target)
            if any(lane.platform == "unknown" for lane in before_after.lanes):
                raise EvolutionPostRollbackBehavioralCoverageError(
                    "post_rollback_coverage_platform_unknown",
                    "Final Evaluation 含 unknown platform，不能建立目标主机覆盖契约。",
                )
        except EvolutionPostRollbackBehavioralCoverageError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackBehavioralCoverageError(
                "post_rollback_coverage_source_invalid",
                "Post-Rollback Coverage source 不完整或已损坏。",
            ) from exc
        return {
            "outcome": outcome,
            "verification": verification,
            "before_after": before_after,
            "baseline_platform": platform,
            "outcome_authority": outcome_view.outcome_authority,
            "verification_authority": verification_view.verification_authority,
            "before_after_authority": before_after_view.before_after_authority,
            "active": bool(
                outcome_view.active_baseline_authority
                and verification_view.active_baseline_authority
            ),
        }


def render_post_rollback_behavioral_coverage(
    view: EvolutionPostRollbackBehavioralCoverageView,
) -> str:
    item = view.contract
    authority = all(
        (
            view.durable_dependencies_valid,
            view.outcome_authority,
            view.runtime_verification_authority,
            view.before_after_authority,
            view.active_baseline_authority,
        )
    )
    lines = [
        "## 回滚后行为覆盖契约",
        "",
        f"- Contract：`{item.contract_id}`",
        f"- Proposal：`{item.workbench_proposal_id}`",
        f"- Outcome：`{item.outcome_id}`",
        f"- 本机基线：`{item.baseline_version}` · `{item.baseline_target}`",
        f"- Lane：{view.recorded_lane_count}/{item.lane_count} 已记录 · "
        f"缺失 {view.missing_lane_count} · 失效 {view.stale_lane_count}",
        f"- 需要目标主机调度：{view.remote_dispatch_count}",
        f"- Coverage authority：{'有效' if authority else '无效'}",
        f"- Matrix ready：{'是' if view.matrix_ready else '否'}",
        "",
    ]
    for lane in view.lanes:
        location = "本机" if lane.expected.local_execution_eligible else "目标主机"
        lines.append(
            f"- L{lane.expected.order} `{lane.expected.lane_kind}` / "
            f"`{lane.expected.platform}` / `{lane.expected.suite_id}`："
            f"{lane.status} · {location}"
        )
    lines.extend(
        (
            "",
            "该契约只冻结完整覆盖要求，不授予远程执行、行为总体评测、学习或推广权限。",
        )
    )
    return "\n".join(lines)


def _build_contract(
    *, sources: dict[str, object], recorded_at: str | None = None
) -> EvolutionPostRollbackBehavioralCoverageContract:
    outcome = sources["outcome"]
    verification = sources["verification"]
    before_after = sources["before_after"]
    baseline_platform = sources["baseline_platform"]
    assert isinstance(baseline_platform, str)
    lanes = tuple(
        EvolutionPostRollbackBehavioralCoverageLane(
            order=lane.order,
            lane_kind=lane.lane_kind,
            platform=lane.platform,
            suite_id=lane.suite_id,
            original_comparison_id=lane.comparison_id,
            original_comparison_sha256=lane.comparison_receipt_sha256,
            original_baseline_id=lane.baseline_id,
            original_baseline_samples_sha256=lane.before.samples_sha256,
            execution_scope=(
                "local_installed_baseline"
                if lane.platform == baseline_platform
                else "remote_target_required"
            ),
            local_execution_eligible=lane.platform == baseline_platform,
            remote_target_required=lane.platform != baseline_platform,
        )
        for lane in before_after.lanes
    )
    now = _aware(recorded_at or datetime.now(UTC).isoformat()).isoformat()
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_BEHAVIORAL_COVERAGE_POLICY,
        "workspace_root": outcome.workspace_root,
        "outcome_id": outcome.outcome_id,
        "outcome_sha256": outcome.outcome_sha256,
        "request_id": outcome.request_id,
        "workbench_session_id": outcome.workbench_session_id,
        "workbench_proposal_id": outcome.workbench_proposal_id,
        "runtime_verification_id": verification.verification_id,
        "runtime_verification_sha256": verification.verification_sha256,
        "before_after_evidence_id": before_after.evidence_id,
        "before_after_evidence_sha256": before_after.evidence_sha256,
        "final_evaluation_id": before_after.final_evaluation_id,
        "final_evaluation_sha256": before_after.final_evaluation_sha256,
        "baseline_slot_id": verification.baseline_slot_id,
        "baseline_slot_sha256": verification.baseline_slot_sha256,
        "baseline_manifest_sha256": verification.baseline_manifest_sha256,
        "baseline_version": verification.baseline_version,
        "baseline_target": verification.baseline_target,
        "baseline_platform": baseline_platform,
        "baseline_source_commit": verification.baseline_source_commit,
        "baseline_source_tree_sha256": verification.baseline_source_tree_sha256,
        "lane_count": len(lanes),
        "local_lane_count": sum(item.local_execution_eligible for item in lanes),
        "remote_lane_count": sum(item.remote_target_required for item in lanes),
        "required_platforms": [item.platform for item in lanes[1:]],
        "lanes": [item.model_dump(mode="json") for item in lanes],
        "coverage_contract_recorded": True,
        "behavioral_evaluation_recorded": False,
        "execution_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "recorded_at": now,
    }
    digest = _digest(payload)
    return EvolutionPostRollbackBehavioralCoverageContract.model_validate(
        {
            **payload,
            "contract_id": f"evpostcoverage_{digest[:24]}",
            "contract_sha256": digest,
        }
    )


def _missing_projection(
    expected: EvolutionPostRollbackBehavioralCoverageLane,
) -> EvolutionPostRollbackBehavioralCoverageLaneView:
    return EvolutionPostRollbackBehavioralCoverageLaneView(
        expected=expected,
        status="missing",
        lane_authority=False,
        active_baseline_authority=False,
        dispatch_required=expected.remote_target_required,
    )


def _stale_projection(
    value: EvolutionPostRollbackBehavioralCoverageLaneView,
) -> EvolutionPostRollbackBehavioralCoverageLaneView:
    if value.status == "missing":
        return value
    return EvolutionPostRollbackBehavioralCoverageLaneView(
        expected=value.expected,
        status="stale",
        lane_id=value.lane_id,
        lane_sha256=value.lane_sha256,
        lane_authority=False,
        active_baseline_authority=False,
        dispatch_required=False,
    )


def _target_platform(target: str) -> Platform:
    prefix = str(target).split("-", 1)[0].casefold()
    mapping: dict[str, Platform] = {
        "darwin": "macos",
        "macos": "macos",
        "linux": "linux",
        "windows": "windows",
    }
    try:
        return mapping[prefix]
    except KeyError as exc:
        raise ValueError("Baseline target platform 不受支持。") from exc


def _request_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"evrerollbackreq_[0-9a-f]{24}", text) is None:
        raise EvolutionPostRollbackBehavioralCoverageError(
            "post_rollback_coverage_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return text


def _outcome_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"evrerollbackout_[0-9a-f]{24}", text) is None:
        raise ValueError("Rollback Outcome ID 格式无效。")
    return text


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_behavioral_coverage ("
        "contract_id TEXT PRIMARY KEY, contract_sha256 TEXT NOT NULL UNIQUE, "
        "outcome_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL UNIQUE, "
        "contract_json TEXT NOT NULL, recorded_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_post_rollback_coverage_request "
        "ON evolution_post_rollback_behavioral_coverage(request_id, recorded_at)"
    )
    await db.commit()


def _restore(encoded: str) -> EvolutionPostRollbackBehavioralCoverageContract:
    try:
        payload = json.loads(encoded)
        if not isinstance(payload, dict):
            raise ValueError("Coverage payload 必须是对象。")
        return EvolutionPostRollbackBehavioralCoverageContract.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackBehavioralCoverageError(
            "post_rollback_coverage_store_corrupt",
            "Post-Rollback Coverage Contract 无法验证。",
        ) from exc


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_POST_ROLLBACK_BEHAVIORAL_COVERAGE_POLICY",
    "EvolutionPostRollbackBehavioralCoverageContract",
    "EvolutionPostRollbackBehavioralCoverageError",
    "EvolutionPostRollbackBehavioralCoverageLane",
    "EvolutionPostRollbackBehavioralCoverageLaneView",
    "EvolutionPostRollbackBehavioralCoverageService",
    "EvolutionPostRollbackBehavioralCoverageStore",
    "EvolutionPostRollbackBehavioralCoverageView",
    "render_post_rollback_behavioral_coverage",
]
