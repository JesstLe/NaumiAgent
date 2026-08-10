"""Read-only, fail-closed preview of durable stable population candidates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_stable_stage_completions import (
    EvolutionRevalidationStableStageCompletion,
    EvolutionRevalidationStableStageCompletionView,
)
from naumi_agent.release.population_registry import (
    ReleasePopulationRegistryError,
    ReleasePopulationSnapshotStore,
    ReleasePopulationSnapshotView,
)

EVOLUTION_STABLE_POPULATION_CANDIDATE_PREVIEW_POLICY = (
    "evolution-stable-population-candidate-preview-v3"
)
_SNAPSHOT_RE = re.compile(r"^relpopsnapshot_[0-9a-f]{24}$")
_MAX_POPULATION = 10_000
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_DISPLAY_ITEMS = 100
_MAX_CONFLICTS = 20
_MAX_DYNAMIC_INSPECTIONS = 16


@runtime_checkable
class EvolutionStableStageCompletionInspectionPort(Protocol):
    """Read-only port implemented by the existing 5f5r Service."""

    async def inspect(
        self,
        *,
        evidence_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationStableStageCompletionView: ...


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStablePopulationCandidateStatus(StrEnum):
    EMPTY = "empty"
    PARTIAL = "partial"
    BREACHED = "breached"
    CONFLICTED = "conflicted"
    CANDIDATE_COMPLETE = "candidate_complete"


class EvolutionStablePopulationCandidateItem(_StrictModel):
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    evidence_id: str = Field(pattern=r"^evrestablecomplete_[0-9a-f]{24}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_status: Literal["passing", "breached", "insufficient"]
    observed_runs: int = Field(ge=0, le=100)
    successful_runs: int = Field(ge=0, le=100)
    assessed_at: str = Field(min_length=1, max_length=100)
    duplicate_intents: int = Field(ge=0, le=_MAX_POPULATION)
    recorded_stage_completion: bool
    dynamically_revalidated: bool
    dynamic_stage_completion_authority: bool
    dynamic_invalidation_reasons: tuple[str, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def _project(self) -> Self:
        _aware(self.assessed_at)
        expected_stage_completion = self.recorded_status == "passing"
        if self.recorded_stage_completion is not expected_stage_completion:
            raise ValueError("Stable Population candidate status projection 不一致。")
        if not (
            self.dynamic_invalidation_reasons
            == tuple(sorted(set(self.dynamic_invalidation_reasons)))
            and (
                self.dynamically_revalidated
                or not self.dynamic_stage_completion_authority
            )
        ):
            raise ValueError("Stable Population candidate dynamic projection 不一致。")
        return self


class EvolutionStablePopulationCandidatePreview(_StrictModel):
    schema_version: Literal[3] = 3
    policy_version: Literal[
        "evolution-stable-population-candidate-preview-v3"
    ] = EVOLUTION_STABLE_POPULATION_CANDIDATE_PREVIEW_POLICY
    preview_id: str = Field(pattern=r"^evstablepoppreview_[0-9a-f]{24}$")
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    requested_snapshot_id: str = Field(
        default="", pattern=r"^(?:|relpopsnapshot_[0-9a-f]{24})$"
    )
    population_snapshot_id: str = Field(
        default="", pattern=r"^(?:|relpopsnapshot_[0-9a-f]{24})$"
    )
    population_snapshot_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    population_snapshot_sequence: int = Field(ge=0, le=1_000_000)
    population_denominator: int = Field(ge=0, le=_MAX_POPULATION)
    candidate_version: str = Field(default="", max_length=128)
    candidate_target: str = Field(default="", max_length=255)
    plan_id: str = Field(default="", pattern=r"^(?:|evrerolloutplan_[0-9a-f]{24})$")
    plan_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    status: EvolutionStablePopulationCandidateStatus
    durable_receipts: int = Field(ge=0, le=_MAX_POPULATION)
    observed_members: int = Field(ge=0, le=_MAX_POPULATION)
    passing_members: int = Field(ge=0, le=_MAX_POPULATION)
    breached_members: int = Field(ge=0, le=_MAX_POPULATION)
    insufficient_members: int = Field(ge=0, le=_MAX_POPULATION)
    conflicting_members: int = Field(ge=0, le=_MAX_POPULATION)
    missing_members: int = Field(ge=0, le=_MAX_POPULATION)
    hidden_items: int = Field(ge=0, le=_MAX_POPULATION)
    integrity_conflicts: tuple[str, ...] = Field(max_length=_MAX_CONFLICTS)
    population_source_configured: bool
    population_source_current: bool
    population_latest_for_channel: bool
    population_trust_current: bool
    population_not_yet_valid: bool
    population_expired: bool
    population_membership_consistent: bool
    population_invalidation_reasons: tuple[str, ...] = Field(max_length=16)
    population_snapshot_authority: bool
    dynamic_inspector_configured: bool
    dynamically_revalidated_members: int = Field(ge=0, le=_MAX_POPULATION)
    dynamic_authoritative_members: int = Field(ge=0, le=_MAX_POPULATION)
    dynamic_non_authoritative_members: int = Field(ge=0, le=_MAX_POPULATION)
    items: tuple[EvolutionStablePopulationCandidateItem, ...] = Field(
        max_length=_MAX_DISPLAY_ITEMS
    )
    candidate_complete: bool
    dynamic_revalidation_authority: bool
    stable_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    generated_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stable Population Preview workspace 必须 canonical。")
        _aware(self.generated_at)
        counts = self.passing_members + self.breached_members + self.insufficient_members
        if not (
            self.observed_members == counts + self.conflicting_members
            and self.durable_receipts >= self.observed_members
            and self.missing_members
            == max(0, self.population_denominator - self.observed_members)
            and self.hidden_items == max(0, self.observed_members - len(self.items))
            and self.integrity_conflicts == tuple(sorted(set(self.integrity_conflicts)))
        ):
            raise ValueError("Stable Population Preview count projection 不一致。")
        population_authority = bool(
            self.population_source_configured
            and bool(self.population_snapshot_id)
            and self.population_source_current
            and self.population_latest_for_channel
            and self.population_trust_current
            and not self.population_not_yet_valid
            and not self.population_expired
            and self.population_membership_consistent
        )
        if not (
            self.population_snapshot_authority is population_authority
            and self.population_invalidation_reasons
            == tuple(sorted(set(self.population_invalidation_reasons)))
        ):
            raise ValueError("Stable Population Preview trust projection 不一致。")
        dynamic_authority = bool(
            self.dynamic_inspector_configured
            and self.population_snapshot_authority
            and self.candidate_complete
            and self.dynamically_revalidated_members == self.population_denominator
            and self.dynamic_authoritative_members == self.population_denominator
            and self.dynamic_non_authoritative_members == 0
        )
        if not (
            self.dynamically_revalidated_members
            == self.dynamic_authoritative_members
            + self.dynamic_non_authoritative_members
            and self.dynamically_revalidated_members <= self.observed_members
            and self.dynamic_revalidation_authority is dynamic_authority
        ):
            raise ValueError("Stable Population Preview dynamic authority 投影不一致。")
        complete = bool(
            self.population_denominator > 0
            and self.observed_members == self.population_denominator
            and self.passing_members == self.population_denominator
            and self.missing_members == 0
            and self.conflicting_members == 0
            and not self.integrity_conflicts
        )
        expected_status = _preview_status(
            durable_receipts=self.durable_receipts,
            candidate_complete=complete,
            breached_members=self.breached_members,
            conflicting_members=self.conflicting_members,
            integrity_conflicts=self.integrity_conflicts,
        )
        if self.candidate_complete is not complete or self.status is not expected_status:
            raise ValueError("Stable Population Preview authority projection 不一致。")
        core = self.model_dump(mode="json", exclude={"preview_id", "preview_sha256"})
        digest = _digest(core)
        if self.preview_sha256 != digest or self.preview_id != (
            f"evstablepoppreview_{digest[:24]}"
        ):
            raise ValueError("Stable Population Preview identity 不一致。")
        return self


class EvolutionStablePopulationCandidatePreviewError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStablePopulationCandidatePreviewService:
    """Project bounded durable candidates without granting rollout authority."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        db_path: str | Path,
        population_store: ReleasePopulationSnapshotStore | None = None,
        stage_completion_inspector: EvolutionStableStageCompletionInspectionPort
        | None = None,
        clock=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.db_path = Path(db_path).expanduser().resolve()
        if population_store is not None and not isinstance(
            population_store, ReleasePopulationSnapshotStore
        ):
            raise TypeError("Stable Population Preview 需要 Population Snapshot Store。")
        self.population_store = population_store
        if stage_completion_inspector is not None and not isinstance(
            stage_completion_inspector,
            EvolutionStableStageCompletionInspectionPort,
        ):
            raise TypeError("Stable Population Preview 需要 5f5r 只读 inspection port。")
        self.stage_completion_inspector = stage_completion_inspector
        self.clock = clock or (lambda: datetime.now(UTC))

    async def preview(
        self,
        *,
        snapshot_id: str | None = None,
        limit: int = 50,
    ) -> EvolutionStablePopulationCandidatePreview:
        requested = _snapshot_id(snapshot_id)
        bounded_limit = _limit(limit)
        rows = await self._read_current_rows(requested)
        receipts = tuple(_restore_row(row) for row in rows)
        selected = requested or (
            "" if not receipts else max(receipts, key=_receipt_order).population_snapshot_id
        )
        selected_receipts = tuple(
            item for item in receipts if item.population_snapshot_id == selected
        )
        dynamic_results = await self._inspect_stage_completions(
            _current_member_receipts(selected_receipts)
        )
        population_view, population_error = await self._inspect_population(selected)
        return _build_preview(
            workspace_root=self.workspace_root,
            requested_snapshot_id=requested,
            snapshot_id=selected,
            receipts=selected_receipts,
            population_store_configured=self.population_store is not None,
            population_view=population_view,
            population_error=population_error,
            dynamic_inspector_configured=self.stage_completion_inspector is not None,
            dynamic_results=dynamic_results,
            limit=bounded_limit,
            generated_at=_aware(self.clock()).isoformat(),
        )

    async def _inspect_stage_completions(
        self,
        receipts: tuple[EvolutionRevalidationStableStageCompletion, ...],
    ) -> dict[str, tuple[bool, bool, tuple[str, ...]]]:
        inspector = self.stage_completion_inspector
        if inspector is None:
            return {
                item.evidence_id: (
                    False,
                    False,
                    ("dynamic_inspector_not_configured",),
                )
                for item in receipts
            }

        async def inspect_one(item):
            try:
                view = await inspector.inspect(
                    evidence_id=item.evidence_id,
                    subject_id=item.subject_id,
                )
                if not isinstance(
                    view, EvolutionRevalidationStableStageCompletionView
                ) or view.receipt != item:
                    return False, False, ("dynamic_inspection_identity_mismatch",)
                return (
                    True,
                    view.stable_stage_completion_authority,
                    view.invalidation_reasons,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                code = getattr(exc, "code", "dynamic_inspection_failed")
                return False, False, (_safe_reason_code(code),)

        results: dict[str, tuple[bool, bool, tuple[str, ...]]] = {}
        for offset in range(0, len(receipts), _MAX_DYNAMIC_INSPECTIONS):
            batch = receipts[offset : offset + _MAX_DYNAMIC_INSPECTIONS]
            inspected = await asyncio.gather(*(inspect_one(item) for item in batch))
            results.update(
                (item.evidence_id, result)
                for item, result in zip(batch, inspected, strict=True)
            )
        return results

    async def _inspect_population(
        self,
        snapshot_id: str,
    ) -> tuple[ReleasePopulationSnapshotView | None, str]:
        if not snapshot_id or self.population_store is None:
            return None, "" if not snapshot_id else "population_store_not_configured"
        try:
            return await self.population_store.inspect(snapshot_id=snapshot_id), ""
        except ReleasePopulationRegistryError as exc:
            return None, exc.code
        except (OSError, TypeError, ValueError):
            return None, "population_source_unavailable"

    async def _read_current_rows(self, requested_snapshot_id: str) -> tuple[tuple, ...]:
        if not self.db_path.is_file():
            return ()
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        try:
            async with aiosqlite.connect(uri, uri=True) as db:
                await db.execute("PRAGMA query_only = ON")
                await db.execute("PRAGMA busy_timeout = 2000")
                exists = await (
                    await db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                        "name = 'evolution_revalidation_stable_stage_completions'"
                    )
                ).fetchone()
                if exists is None:
                    return ()
                where = ""
                params: tuple[object, ...] = ()
                if requested_snapshot_id:
                    where = "WHERE json_extract(evidence_json, '$.population_snapshot_id') = ?"
                    params = (requested_snapshot_id,)
                cte = (
                    "WITH latest AS (SELECT intent_id, MAX(rowid) AS latest_rowid "
                    "FROM evolution_revalidation_stable_stage_completions "
                    "GROUP BY intent_id), current AS (SELECT e.evidence_id, "
                    "e.evidence_sha256, e.intent_id, e.evidence_json, e.assessed_at "
                    "FROM evolution_revalidation_stable_stage_completions e "
                    "JOIN latest l ON e.rowid = l.latest_rowid) "
                )
                count_row = await (
                    await db.execute(
                        cte
                        + "SELECT COUNT(*), COALESCE(SUM(length(evidence_json)), 0) "
                        + "FROM current "
                        + where,
                        params,
                    )
                ).fetchone()
                row_count = 0 if count_row is None else int(count_row[0])
                source_bytes = 0 if count_row is None else int(count_row[1])
                if row_count > _MAX_POPULATION:
                    raise EvolutionStablePopulationCandidatePreviewError(
                        "stable_population_candidate_limit_exceeded",
                        "Stable Population candidate 超过 10000 个成员上限。",
                    )
                if source_bytes > _MAX_SOURCE_BYTES:
                    raise EvolutionStablePopulationCandidatePreviewError(
                        "stable_population_candidate_source_oversized",
                        "Stable Population candidate durable source 超过 64 MiB 上限。",
                    )
                cursor = await db.execute(
                    cte
                    + "SELECT evidence_id, evidence_sha256, intent_id, evidence_json, "
                    "assessed_at FROM current "
                    + where
                    + " ORDER BY assessed_at DESC, evidence_id ASC",
                    params,
                )
                return tuple(await cursor.fetchall())
        except EvolutionStablePopulationCandidatePreviewError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionStablePopulationCandidatePreviewError(
                "stable_population_candidate_source_unavailable",
                "Stable Population candidate durable source 当前不可读取。",
            ) from exc


def _build_preview(
    *,
    workspace_root: Path,
    requested_snapshot_id: str,
    snapshot_id: str,
    receipts: tuple[EvolutionRevalidationStableStageCompletion, ...],
    population_store_configured: bool,
    population_view: ReleasePopulationSnapshotView | None,
    population_error: str,
    dynamic_inspector_configured: bool,
    dynamic_results: dict[str, tuple[bool, bool, tuple[str, ...]]],
    limit: int,
    generated_at: str,
) -> EvolutionStablePopulationCandidatePreview:
    source_set_sha256 = _digest(
        sorted((item.evidence_id, item.evidence_sha256) for item in receipts)
    )
    if not receipts:
        snapshot = None if population_view is None else population_view.snapshot
        population_denominator = (
            0 if snapshot is None else snapshot.payload.population_denominator
        )
        population_reasons = (
            set() if population_view is None else set(population_view.invalidation_reasons)
        )
        if population_error:
            population_reasons.add(population_error)
        population_membership_consistent = bool(
            snapshot is not None and snapshot.snapshot_id == snapshot_id
        )
        population_authority = bool(
            population_store_configured
            and population_view is not None
            and population_view.population_snapshot_authority
            and population_membership_consistent
        )
        return _finalize_preview(
            {
                "source_set_sha256": source_set_sha256,
                "workspace_root": str(workspace_root),
                "requested_snapshot_id": requested_snapshot_id,
                "population_snapshot_id": snapshot_id,
                "population_snapshot_sha256": (
                    "" if snapshot is None else snapshot.snapshot_sha256
                ),
                "population_snapshot_sequence": (
                    0 if snapshot is None else snapshot.payload.sequence
                ),
                "population_denominator": population_denominator,
                "candidate_version": "",
                "candidate_target": "",
                "plan_id": "",
                "plan_sha256": "",
                "status": "empty",
                "durable_receipts": 0,
                "observed_members": 0,
                "passing_members": 0,
                "breached_members": 0,
                "insufficient_members": 0,
                "conflicting_members": 0,
                "missing_members": population_denominator,
                "hidden_items": 0,
                "integrity_conflicts": [],
                "population_source_configured": population_store_configured,
                "population_source_current": bool(
                    population_view and population_view.source_current
                ),
                "population_latest_for_channel": bool(
                    population_view and population_view.latest_for_channel
                ),
                "population_trust_current": bool(
                    population_view and population_view.trust_current
                ),
                "population_not_yet_valid": bool(
                    population_view and population_view.not_yet_valid
                ),
                "population_expired": bool(
                    population_view and population_view.expired
                ),
                "population_membership_consistent": (
                    population_membership_consistent
                ),
                "population_invalidation_reasons": sorted(population_reasons),
                "population_snapshot_authority": population_authority,
                "dynamic_inspector_configured": dynamic_inspector_configured,
                "dynamically_revalidated_members": 0,
                "dynamic_authoritative_members": 0,
                "dynamic_non_authoritative_members": 0,
                "items": [],
                "candidate_complete": False,
                "dynamic_revalidation_authority": False,
                "stable_rollout_authority": False,
                "promotion_authority": False,
                "generated_at": generated_at,
            }
        )
    anchor = max(receipts, key=_receipt_order)
    lineage_fields = (
        "population_snapshot_sha256",
        "population_snapshot_sequence",
        "population_denominator",
        "candidate_version",
        "candidate_target",
        "plan_id",
        "plan_sha256",
    )
    conflicts = {
        f"{field}_conflict"
        for field in lineage_fields
        if any(getattr(item, field) != getattr(anchor, field) for item in receipts)
    }
    if any(item.workspace_root != str(workspace_root) for item in receipts):
        conflicts.add("workspace_root_conflict")
    grouped: dict[str, list[EvolutionRevalidationStableStageCompletion]] = defaultdict(list)
    for receipt in receipts:
        grouped[receipt.installation_member_id].append(receipt)
    population_reasons = set()
    if population_error:
        population_reasons.add(population_error)
    population_source_current = False
    population_latest = False
    population_trust = False
    population_not_yet_valid = False
    population_expired = False
    membership_consistent = False
    if population_view is not None:
        population_source_current = population_view.source_current
        population_latest = population_view.latest_for_channel
        population_trust = population_view.trust_current
        population_not_yet_valid = population_view.not_yet_valid
        population_expired = population_view.expired
        population_reasons.update(population_view.invalidation_reasons)
        snapshot = population_view.snapshot
        population_members = {
            item.payload.member_id for item in snapshot.payload.credentials
        }
        membership_consistent = bool(
            snapshot.snapshot_id == anchor.population_snapshot_id
            and snapshot.snapshot_sha256 == anchor.population_snapshot_sha256
            and snapshot.payload.sequence == anchor.population_snapshot_sequence
            and snapshot.payload.population_denominator == anchor.population_denominator
            and set(grouped).issubset(population_members)
        )
        if not membership_consistent:
            population_reasons.add("population_lineage_or_membership_mismatch")
    elif population_store_configured and not population_error:
        population_reasons.add("population_snapshot_unavailable")
    population_authority = bool(
        population_store_configured
        and population_view is not None
        and population_source_current
        and population_latest
        and population_trust
        and not population_not_yet_valid
        and not population_expired
        and membership_consistent
    )
    items: list[EvolutionStablePopulationCandidateItem] = []
    conflicting_members = 0
    counts: Counter[str] = Counter()
    dynamic_revalidated_members = 0
    dynamic_authoritative_members = 0
    dynamic_non_authoritative_members = 0
    for member_id, member_receipts in grouped.items():
        current = max(member_receipts, key=_receipt_order)
        dynamically_revalidated, dynamic_authority, dynamic_reasons = (
            dynamic_results.get(
                current.evidence_id,
                (False, False, ("dynamic_inspection_result_missing",)),
            )
        )
        duplicate_intents = len({item.intent_id for item in member_receipts}) - 1
        if duplicate_intents:
            conflicting_members += 1
            conflicts.add("duplicate_member_intents")
        else:
            counts[current.metrics.status.value] += 1
        if dynamically_revalidated:
            dynamic_revalidated_members += 1
            if dynamic_authority:
                dynamic_authoritative_members += 1
            else:
                dynamic_non_authoritative_members += 1
        items.append(
            EvolutionStablePopulationCandidateItem(
                installation_member_id=member_id,
                intent_id=current.intent_id,
                evidence_id=current.evidence_id,
                evidence_sha256=current.evidence_sha256,
                recorded_status=current.metrics.status.value,
                observed_runs=current.metrics.observed_runs,
                successful_runs=current.metrics.successful_runs,
                assessed_at=current.assessed_at,
                duplicate_intents=max(0, duplicate_intents),
                recorded_stage_completion=(current.metrics.status.value == "passing"),
                dynamically_revalidated=dynamically_revalidated,
                dynamic_stage_completion_authority=dynamic_authority,
                dynamic_invalidation_reasons=tuple(sorted(set(dynamic_reasons))),
            )
        )
    status_rank = {"breached": 0, "insufficient": 1, "passing": 2}
    items.sort(
        key=lambda item: (
            0 if item.duplicate_intents else 1,
            status_rank[item.recorded_status],
            item.installation_member_id,
        )
    )
    observed = len(grouped)
    denominator = anchor.population_denominator
    if observed > denominator:
        conflicts.add("observed_members_exceed_denominator")
    missing = max(0, denominator - observed)
    passing = counts["passing"]
    breached = counts["breached"]
    insufficient = counts["insufficient"]
    candidate_complete = bool(
        denominator > 0
        and observed == denominator
        and passing == denominator
        and conflicting_members == 0
        and not conflicts
    )
    dynamic_revalidation_authority = bool(
        dynamic_inspector_configured
        and population_authority
        and candidate_complete
        and dynamic_revalidated_members == denominator
        and dynamic_authoritative_members == denominator
        and dynamic_non_authoritative_members == 0
    )
    preview_status = _preview_status(
        durable_receipts=len(receipts),
        candidate_complete=candidate_complete,
        breached_members=breached,
        conflicting_members=conflicting_members,
        integrity_conflicts=tuple(conflicts),
    )
    visible = items[:limit]
    return _finalize_preview(
        {
            "source_set_sha256": source_set_sha256,
            "workspace_root": str(workspace_root),
            "requested_snapshot_id": requested_snapshot_id,
            "population_snapshot_id": snapshot_id,
            "population_snapshot_sha256": anchor.population_snapshot_sha256,
            "population_snapshot_sequence": anchor.population_snapshot_sequence,
            "population_denominator": denominator,
            "candidate_version": anchor.candidate_version,
            "candidate_target": anchor.candidate_target,
            "plan_id": anchor.plan_id,
            "plan_sha256": anchor.plan_sha256,
            "status": preview_status.value,
            "durable_receipts": len(receipts),
            "observed_members": observed,
            "passing_members": passing,
            "breached_members": breached,
            "insufficient_members": insufficient,
            "conflicting_members": conflicting_members,
            "missing_members": missing,
            "hidden_items": max(0, observed - len(visible)),
            "integrity_conflicts": sorted(conflicts)[:_MAX_CONFLICTS],
            "population_source_configured": population_store_configured,
            "population_source_current": population_source_current,
            "population_latest_for_channel": population_latest,
            "population_trust_current": population_trust,
            "population_not_yet_valid": population_not_yet_valid,
            "population_expired": population_expired,
            "population_membership_consistent": membership_consistent,
            "population_invalidation_reasons": sorted(population_reasons),
            "population_snapshot_authority": population_authority,
            "dynamic_inspector_configured": dynamic_inspector_configured,
            "dynamically_revalidated_members": dynamic_revalidated_members,
            "dynamic_authoritative_members": dynamic_authoritative_members,
            "dynamic_non_authoritative_members": dynamic_non_authoritative_members,
            "items": [item.model_dump(mode="json") for item in visible],
            "candidate_complete": candidate_complete,
            "dynamic_revalidation_authority": dynamic_revalidation_authority,
            "stable_rollout_authority": False,
            "promotion_authority": False,
            "generated_at": generated_at,
        }
    )


def render_stable_population_candidate_preview(
    preview: EvolutionStablePopulationCandidatePreview,
) -> str:
    labels = {
        EvolutionStablePopulationCandidateStatus.EMPTY: "暂无候选",
        EvolutionStablePopulationCandidateStatus.PARTIAL: "候选不完整",
        EvolutionStablePopulationCandidateStatus.BREACHED: "检测到越界",
        EvolutionStablePopulationCandidateStatus.CONFLICTED: "证据冲突",
        EvolutionStablePopulationCandidateStatus.CANDIDATE_COMPLETE: "候选已覆盖",
    }
    lines = [
        "## Stable Population 候选预演",
        "",
        f"- 状态：**{labels[preview.status]}**",
        f"- Population Snapshot：`{preview.population_snapshot_id or '尚无'}`",
        (
            "- 成员："
            f"{preview.observed_members}/{preview.population_denominator or 0}；"
            f"passing {preview.passing_members}、breached {preview.breached_members}、"
            f"insufficient {preview.insufficient_members}、冲突 {preview.conflicting_members}、"
            f"缺失 {preview.missing_members}"
        ),
        (
            "- 动态 5f5r 重验："
            f"{preview.dynamic_authoritative_members}/"
            f"{preview.population_denominator or 0} authoritative；"
            f"端口 `{'configured' if preview.dynamic_inspector_configured else 'missing'}`"
        ),
        (
            "- Current Population："
            f"`{'authoritative' if preview.population_snapshot_authority else 'unavailable'}`"
        ),
        "- Stable rollout authority：`false`",
        "- Promotion authority：`false`",
        f"- Preview receipt：`{preview.preview_id}`",
    ]
    if preview.integrity_conflicts:
        lines.extend(
            ["", "### 冲突", *[f"- `{item}`" for item in preview.integrity_conflicts]]
        )
    if preview.population_invalidation_reasons:
        lines.extend(
            [
                "",
                "### Population 撤权原因",
                *[
                    f"- `{item}`" for item in preview.population_invalidation_reasons
                ],
            ]
        )
    if preview.items:
        lines.extend(["", "### 成员候选"])
        for item in preview.items:
            duplicate = f"；重复 intent {item.duplicate_intents}" if item.duplicate_intents else ""
            lines.append(
                f"- `{item.installation_member_id}`：**{item.recorded_status}**；"
                f"运行 {item.successful_runs}/{item.observed_runs}{duplicate}；"
                "动态 `"
                f"{'authoritative' if item.dynamic_stage_completion_authority else 'denied'}"
                "`；"
                f"Evidence `{item.evidence_id}`"
            )
    if preview.hidden_items:
        lines.extend(["", f"另有 {preview.hidden_items} 个成员未在本页展开。"])
    lines.extend(
        [
            "",
            "> 这是 durable candidate 的只读预演，不会把历史 receipt 冒充当前 Stable rollout。",
        ]
    )
    return "\n".join(lines)


def _restore_row(row: tuple) -> EvolutionRevalidationStableStageCompletion:
    evidence_id, evidence_sha256, intent_id, evidence_json, assessed_at = row
    if not isinstance(evidence_json, str) or len(evidence_json.encode()) > 512 * 1024:
        raise EvolutionStablePopulationCandidatePreviewError(
            "stable_population_candidate_receipt_oversized",
            "Stable Population candidate receipt 超过单条上限。",
        )
    try:
        receipt = EvolutionRevalidationStableStageCompletion.model_validate_json(evidence_json)
    except (TypeError, ValueError) as exc:
        raise EvolutionStablePopulationCandidatePreviewError(
            "stable_population_candidate_receipt_invalid",
            "Stable Population candidate receipt 内容身份无效。",
        ) from exc
    if not (
        receipt.evidence_id == evidence_id
        and receipt.evidence_sha256 == evidence_sha256
        and receipt.intent_id == intent_id
        and receipt.assessed_at == assessed_at
    ):
        raise EvolutionStablePopulationCandidatePreviewError(
            "stable_population_candidate_row_mismatch",
            "Stable Population candidate row projection 与 receipt 不一致。",
        )
    return receipt


def _finalize_preview(payload: dict) -> EvolutionStablePopulationCandidatePreview:
    core = {
        "schema_version": 3,
        "policy_version": EVOLUTION_STABLE_POPULATION_CANDIDATE_PREVIEW_POLICY,
        **payload,
    }
    digest = _digest(core)
    return EvolutionStablePopulationCandidatePreview.model_validate(
        {
            **core,
            "preview_id": f"evstablepoppreview_{digest[:24]}",
            "preview_sha256": digest,
        }
    )


def _preview_status(
    *,
    durable_receipts: int,
    candidate_complete: bool,
    breached_members: int,
    conflicting_members: int,
    integrity_conflicts: tuple[str, ...] | list[str] | set[str],
) -> EvolutionStablePopulationCandidateStatus:
    if durable_receipts == 0:
        return EvolutionStablePopulationCandidateStatus.EMPTY
    if conflicting_members or integrity_conflicts:
        return EvolutionStablePopulationCandidateStatus.CONFLICTED
    if breached_members:
        return EvolutionStablePopulationCandidateStatus.BREACHED
    if candidate_complete:
        return EvolutionStablePopulationCandidateStatus.CANDIDATE_COMPLETE
    return EvolutionStablePopulationCandidateStatus.PARTIAL


def _snapshot_id(value: str | None) -> str:
    normalized = str(value or "").strip()
    if normalized and not _SNAPSHOT_RE.fullmatch(normalized):
        raise ValueError("snapshot_id 必须是 relpopsnapshot_ 加 24 位小写十六进制。")
    return normalized


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("limit 必须是 1 到 100 的整数。")
    return value


def _receipt_order(item: EvolutionRevalidationStableStageCompletion) -> tuple:
    return (_aware(item.assessed_at), item.evidence_id)


def _current_member_receipts(
    receipts: tuple[EvolutionRevalidationStableStageCompletion, ...],
) -> tuple[EvolutionRevalidationStableStageCompletion, ...]:
    grouped: dict[str, list[EvolutionRevalidationStableStageCompletion]] = defaultdict(
        list
    )
    for item in receipts:
        grouped[item.installation_member_id].append(item)
    return tuple(
        max(grouped[member_id], key=_receipt_order) for member_id in sorted(grouped)
    )


def _safe_reason_code(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "_", str(value).strip().lower())
    return normalized[:128] or "dynamic_inspection_failed"


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_POPULATION_CANDIDATE_PREVIEW_POLICY",
    "EvolutionStablePopulationCandidateItem",
    "EvolutionStablePopulationCandidatePreview",
    "EvolutionStablePopulationCandidatePreviewError",
    "EvolutionStablePopulationCandidatePreviewService",
    "EvolutionStablePopulationCandidateStatus",
    "EvolutionStableStageCompletionInspectionPort",
    "render_stable_population_candidate_preview",
]
