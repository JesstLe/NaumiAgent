"""Trusted target baseline resolution for one remote post-rollback lane."""

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

from naumi_agent.evolution.post_rollback_behavioral_coverage import (
    EvolutionPostRollbackBehavioralCoverageContract,
    EvolutionPostRollbackBehavioralCoverageError,
    EvolutionPostRollbackBehavioralCoverageService,
)
from naumi_agent.evolution.post_rollback_remote_lane_placements import (
    EvolutionPostRollbackRemoteLanePlacement,
    EvolutionPostRollbackRemoteLanePlacementError,
    EvolutionPostRollbackRemoteLanePlacementService,
    EvolutionPostRollbackRemoteLanePlacementStore,
)
from naumi_agent.release.channel_catalog import (
    ReleaseChannelCatalogError,
    ReleaseChannelCatalogStore,
    ReleaseChannelResolution,
)

EVOLUTION_POST_ROLLBACK_TARGET_BASELINE_POLICY = (
    "evolution-post-rollback-target-baseline-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_CHANNEL_RE = r"^[a-z][a-z0-9._-]{0,63}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionPostRollbackTargetBaseline(_StrictModel):
    """Target-specific trusted resolution with no transport or execution authority."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-post-rollback-target-baseline-v1"] = (
        EVOLUTION_POST_ROLLBACK_TARGET_BASELINE_POLICY
    )
    baseline_resolution_id: str = Field(pattern=r"^evpostbaseline_[0-9a-f]{24}$")
    baseline_resolution_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    coverage_contract_id: str = Field(pattern=r"^evpostcoverage_[0-9a-f]{24}$")
    coverage_contract_sha256: str = Field(pattern=_SHA256_RE)
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    workbench_proposal_id: str = Field(min_length=1, max_length=128)
    comparison_id: str = Field(pattern=_SHA256_RE)
    placement_id: str = Field(pattern=r"^evpostplacement_[0-9a-f]{24}$")
    placement_sha256: str = Field(pattern=_SHA256_RE)
    worker_id: str = Field(min_length=1, max_length=128)
    worker_instance_id: str = Field(min_length=1, max_length=128)
    worker_epoch: int = Field(ge=1)
    release_target: str = Field(pattern=r"^(?:macos|linux|windows)-(?:arm64|x64)$")
    local_baseline_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    local_baseline_slot_sha256: str = Field(pattern=_SHA256_RE)
    local_baseline_version: str = Field(min_length=1, max_length=128)
    local_baseline_target: str = Field(min_length=1, max_length=128)
    local_baseline_source_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    local_baseline_source_tree_sha256: str = Field(pattern=_SHA256_RE)
    channel: str = Field(pattern=_CHANNEL_RE)
    channel_resolution: ReleaseChannelResolution
    source_equivalence_verified: Literal[True] = True
    target_build_attestation_verified: Literal[True] = True
    baseline_resolved: Literal[True] = True
    transport_delivered: Literal[False] = False
    installation_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    resolved_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Target Baseline workspace 必须 canonical。")
        _aware(self.resolved_at)
        resolution = self.channel_resolution
        build = resolution.entry.build_attestation.payload
        if not (
            self.channel == resolution.channel
            and self.release_target == resolution.target == resolution.entry.target == build.target
            and self.local_baseline_version == resolution.entry.version == build.version
            and self.local_baseline_source_commit == build.source_commit
            and self.local_baseline_source_tree_sha256 == build.source_tree_sha256
            and resolution.catalog_resolution_authority
            and resolution.download_input_authority
        ):
            raise ValueError("Target Baseline 与本机 source/version 或 target 不等价。")
        if any(
            (
                self.transport_delivered,
                self.installation_authority,
                self.execution_authority,
                self.result_authority,
                self.learning_authority,
                self.promotion_authority,
            )
        ):
            raise ValueError("Target Baseline Resolution 不得扩大权威。")
        digest = _digest(
            self.model_dump(
                mode="json",
                exclude={"baseline_resolution_id", "baseline_resolution_sha256"},
            )
        )
        if not (
            hmac.compare_digest(self.baseline_resolution_sha256, digest)
            and self.baseline_resolution_id == f"evpostbaseline_{digest[:24]}"
        ):
            raise ValueError("Target Baseline Resolution identity 不一致。")
        return self


class EvolutionPostRollbackTargetBaselineView(_StrictModel):
    baseline: EvolutionPostRollbackTargetBaseline
    durable_source_valid: bool
    placement_authority: bool
    catalog_authority: bool
    baseline_equivalence_current: bool
    baseline_resolution_authority: bool
    download_input_authority: bool
    transport_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    result_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        current = bool(
            self.durable_source_valid
            and self.placement_authority
            and self.catalog_authority
            and self.baseline_equivalence_current
        )
        if not (
            self.baseline_resolution_authority is current
            and self.download_input_authority is current
        ):
            raise ValueError("Target Baseline authority 投影不一致。")
        return self


class EvolutionPostRollbackTargetBaselineError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostRollbackTargetBaselineStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(
        self,
        placement_id: str,
    ) -> EvolutionPostRollbackTargetBaseline | None:
        _placement_id(placement_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT baseline_json FROM evolution_post_rollback_target_baselines "
                        "WHERE placement_id = ?",
                        (placement_id,),
                    )
                ).fetchone()
            return None if row is None else _restore(row["baseline_json"])
        except EvolutionPostRollbackTargetBaselineError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackTargetBaselineError(
                "post_rollback_target_baseline_store_corrupt",
                "Target Baseline Resolution 损坏或无法读取。",
            ) from exc

    async def record(
        self,
        baseline: EvolutionPostRollbackTargetBaseline,
    ) -> EvolutionPostRollbackTargetBaseline:
        try:
            item = EvolutionPostRollbackTargetBaseline.model_validate_json(
                baseline.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackTargetBaselineError(
                "post_rollback_target_baseline_invalid",
                "Target Baseline Resolution artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionPostRollbackTargetBaselineError(
                "post_rollback_target_baseline_oversized",
                "Target Baseline Resolution 超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                dependency = await (
                    await db.execute(
                        "SELECT placement_sha256 FROM "
                        "evolution_post_rollback_remote_lane_placements "
                        "WHERE placement_id = ?",
                        (item.placement_id,),
                    )
                ).fetchone()
                if dependency is None or dependency["placement_sha256"] != item.placement_sha256:
                    await db.rollback()
                    raise EvolutionPostRollbackTargetBaselineError(
                        "post_rollback_target_baseline_dependency_mismatch",
                        "Target Baseline 的 Placement durable authority 不一致。",
                    )
                existing = await (
                    await db.execute(
                        "SELECT baseline_json FROM evolution_post_rollback_target_baselines "
                        "WHERE placement_id = ?",
                        (item.placement_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing["baseline_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionPostRollbackTargetBaselineError(
                            "post_rollback_target_baseline_conflict",
                            "同一 Placement 已绑定不同 Target Baseline Resolution。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_post_rollback_target_baselines "
                    "(baseline_resolution_id, baseline_resolution_sha256, placement_id, "
                    "placement_sha256, channel, target, baseline_json, resolved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.baseline_resolution_id,
                        item.baseline_resolution_sha256,
                        item.placement_id,
                        item.placement_sha256,
                        item.channel,
                        item.release_target,
                        encoded,
                        item.resolved_at,
                    ),
                )
                await db.commit()
        except EvolutionPostRollbackTargetBaselineError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionPostRollbackTargetBaselineError(
                "post_rollback_target_baseline_store_error",
                "Target Baseline Resolution 无法持久化。",
            ) from exc
        return item


class EvolutionPostRollbackTargetBaselineService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        coverage_service: EvolutionPostRollbackBehavioralCoverageService,
        placement_store: EvolutionPostRollbackRemoteLanePlacementStore,
        placement_service: EvolutionPostRollbackRemoteLanePlacementService,
        catalog_store: ReleaseChannelCatalogStore,
        store: EvolutionPostRollbackTargetBaselineStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.coverage_service = coverage_service
        self.placement_store = placement_store
        self.placement_service = placement_service
        self.catalog_store = catalog_store
        self.store = store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def resolve(
        self,
        *,
        request_id: str,
        comparison_id: str,
        channel: str,
        resolved_at: str | None = None,
    ) -> EvolutionPostRollbackTargetBaselineView:
        request = _request_id(request_id)
        comparison = _comparison_id(comparison_id)
        release_channel = _channel(channel)
        lock = self._locks.setdefault(f"{request}:{comparison}", asyncio.Lock())
        async with lock:
            coverage = await self.coverage_service.record(request_id=request)
            if coverage.contract.workspace_root != str(self.workspace_root):
                raise EvolutionPostRollbackTargetBaselineError(
                    "post_rollback_target_baseline_workspace_mismatch",
                    "Coverage Contract 不属于当前 workspace。",
                )
            placement = await self.placement_store.get(
                coverage.contract.contract_id,
                comparison,
            )
            if placement is None:
                raise EvolutionPostRollbackTargetBaselineError(
                    "post_rollback_target_baseline_placement_missing",
                    "该远端 lane 尚未建立 Placement。",
                )
            placement_view = await self.placement_service.inspect(placement=placement)
            if not placement_view.placement_authority:
                raise EvolutionPostRollbackTargetBaselineError(
                    "post_rollback_target_baseline_placement_stale",
                    "Remote Lane Placement authority 已失效。",
                )
            existing = await self.store.get(placement.placement_id)
            if existing is not None:
                if existing.channel != release_channel:
                    raise EvolutionPostRollbackTargetBaselineError(
                        "post_rollback_target_baseline_conflict",
                        "该 Placement 已绑定其他 release channel。",
                    )
                view = await self.inspect(baseline=existing)
                if not view.baseline_resolution_authority:
                    raise EvolutionPostRollbackTargetBaselineError(
                        "post_rollback_target_baseline_stale",
                        "既有 Target Baseline Resolution authority 已失效。",
                    )
                return view
            try:
                resolution = await self.catalog_store.resolve(
                    channel=release_channel,
                    target=placement.release_target,
                )
            except ReleaseChannelCatalogError as exc:
                raise EvolutionPostRollbackTargetBaselineError(
                    "post_rollback_target_baseline_catalog_unavailable",
                    "受信 Release Channel Catalog 无法解析该 target。",
                ) from exc
            try:
                artifact = _build_target_baseline(
                    coverage=coverage.contract,
                    placement=placement,
                    resolution=resolution,
                    resolved_at=resolved_at or datetime.now(UTC).isoformat(),
                )
            except (TypeError, ValueError) as exc:
                raise EvolutionPostRollbackTargetBaselineError(
                    "post_rollback_target_baseline_not_equivalent",
                    "目标 artifact 与本机 baseline 的 version/source 不等价。",
                ) from exc
            recorded = await self.store.record(artifact)
            view = await self.inspect(baseline=recorded)
            if not view.baseline_resolution_authority:
                raise EvolutionPostRollbackTargetBaselineError(
                    "post_rollback_target_baseline_authority_changed",
                    "Target Baseline 持久化期间 authority 已变化。",
                )
            return view

    async def inspect(
        self,
        *,
        baseline: EvolutionPostRollbackTargetBaseline,
    ) -> EvolutionPostRollbackTargetBaselineView:
        item = EvolutionPostRollbackTargetBaseline.model_validate_json(
            baseline.model_dump_json()
        )
        flags = {"durable": False, "placement": False, "catalog": False, "equivalent": False}
        try:
            durable = await self.store.get(item.placement_id)
            placement = await self.placement_store.get(
                item.coverage_contract_id,
                item.comparison_id,
            )
            if placement is None:
                raise ValueError("Placement missing")
            _require_placement_lineage(item, placement)
            placement_view = await self.placement_service.inspect(placement=placement)
            current = await self.catalog_store.resolve(
                channel=item.channel,
                target=item.release_target,
            )
            flags["durable"] = durable == item and item.workspace_root == str(self.workspace_root)
            flags["placement"] = placement_view.placement_authority
            flags["catalog"] = _same_catalog_resolution(item.channel_resolution, current)
            flags["equivalent"] = _resolution_matches_local(item, current)
        except (
            EvolutionPostRollbackBehavioralCoverageError,
            EvolutionPostRollbackRemoteLanePlacementError,
            EvolutionPostRollbackTargetBaselineError,
            ReleaseChannelCatalogError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            flags = {key: False for key in flags}
        return EvolutionPostRollbackTargetBaselineView(
            baseline=item,
            durable_source_valid=flags["durable"],
            placement_authority=flags["placement"],
            catalog_authority=flags["catalog"],
            baseline_equivalence_current=flags["equivalent"],
            baseline_resolution_authority=all(flags.values()),
            download_input_authority=all(flags.values()),
        )


def render_post_rollback_target_baseline(
    view: EvolutionPostRollbackTargetBaselineView,
) -> str:
    item = view.baseline
    entry = item.channel_resolution.entry
    return "\n".join(
        (
            "## 回滚后目标 Baseline Resolution",
            "",
            f"- Resolution：`{item.baseline_resolution_id}`",
            f"- Placement：`{item.placement_id}`",
            f"- Worker：`{item.worker_id}` · epoch {item.worker_epoch}",
            f"- Channel / target：`{item.channel}` / `{item.release_target}`",
            f"- Version：`{entry.version}`",
            f"- Source commit：`{entry.build_attestation.payload.source_commit}`",
            f"- Source tree：`{entry.build_attestation.payload.source_tree_sha256}`",
            f"- Build Attestation：`{entry.build_attestation.attestation_id}`",
            f"- Baseline authority：{'有效' if view.baseline_resolution_authority else '无效'}",
            "- 下载输入：已验证" if view.download_input_authority else "- 下载输入：已撤权",
            "",
            "该 Resolution 未下载、未安装、未下发任务，也不授予执行、结果、学习或推广权限。",
        )
    )


def _build_target_baseline(
    *,
    coverage: EvolutionPostRollbackBehavioralCoverageContract,
    placement: EvolutionPostRollbackRemoteLanePlacement,
    resolution: ReleaseChannelResolution,
    resolved_at: str,
) -> EvolutionPostRollbackTargetBaseline:
    _require_coverage_placement(coverage, placement)
    build = resolution.entry.build_attestation.payload
    if not (
        resolution.target == placement.release_target
        and resolution.entry.target == placement.release_target
        and resolution.entry.version == coverage.baseline_version
        and build.version == coverage.baseline_version
        and build.source_commit == coverage.baseline_source_commit
        and build.source_tree_sha256 == coverage.baseline_source_tree_sha256
    ):
        raise ValueError("Target artifact 与 local baseline 不等价。")
    timestamp = _aware(resolved_at).isoformat()
    payload = {
        "schema_version": 1,
        "policy_version": EVOLUTION_POST_ROLLBACK_TARGET_BASELINE_POLICY,
        "workspace_root": coverage.workspace_root,
        "coverage_contract_id": coverage.contract_id,
        "coverage_contract_sha256": coverage.contract_sha256,
        "outcome_id": coverage.outcome_id,
        "outcome_sha256": coverage.outcome_sha256,
        "request_id": coverage.request_id,
        "workbench_proposal_id": coverage.workbench_proposal_id,
        "comparison_id": placement.lane.original_comparison_id,
        "placement_id": placement.placement_id,
        "placement_sha256": placement.placement_sha256,
        "worker_id": placement.worker_id,
        "worker_instance_id": placement.worker_instance_id,
        "worker_epoch": placement.worker_epoch,
        "release_target": placement.release_target,
        "local_baseline_slot_id": coverage.baseline_slot_id,
        "local_baseline_slot_sha256": coverage.baseline_slot_sha256,
        "local_baseline_version": coverage.baseline_version,
        "local_baseline_target": coverage.baseline_target,
        "local_baseline_source_commit": coverage.baseline_source_commit,
        "local_baseline_source_tree_sha256": coverage.baseline_source_tree_sha256,
        "channel": resolution.channel,
        "channel_resolution": resolution.model_dump(mode="json"),
        "source_equivalence_verified": True,
        "target_build_attestation_verified": True,
        "baseline_resolved": True,
        "transport_delivered": False,
        "installation_authority": False,
        "execution_authority": False,
        "result_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "resolved_at": timestamp,
    }
    digest = _digest(payload)
    return EvolutionPostRollbackTargetBaseline.model_validate(
        {
            **payload,
            "baseline_resolution_id": f"evpostbaseline_{digest[:24]}",
            "baseline_resolution_sha256": digest,
        }
    )


def _require_coverage_placement(
    coverage: EvolutionPostRollbackBehavioralCoverageContract,
    placement: EvolutionPostRollbackRemoteLanePlacement,
) -> None:
    if not (
        coverage.contract_id == placement.coverage_contract_id
        and coverage.contract_sha256 == placement.coverage_contract_sha256
        and coverage.outcome_id == placement.outcome_id
        and coverage.outcome_sha256 == placement.outcome_sha256
        and coverage.request_id == placement.request_id
        and coverage.workbench_proposal_id == placement.workbench_proposal_id
        and placement.lane in coverage.lanes
    ):
        raise ValueError("Target Baseline Coverage/Placement lineage 不一致。")


def _require_placement_lineage(
    item: EvolutionPostRollbackTargetBaseline,
    placement: EvolutionPostRollbackRemoteLanePlacement,
) -> None:
    if not (
        item.placement_id == placement.placement_id
        and item.placement_sha256 == placement.placement_sha256
        and item.coverage_contract_id == placement.coverage_contract_id
        and item.coverage_contract_sha256 == placement.coverage_contract_sha256
        and item.outcome_id == placement.outcome_id
        and item.outcome_sha256 == placement.outcome_sha256
        and item.request_id == placement.request_id
        and item.workbench_proposal_id == placement.workbench_proposal_id
        and item.comparison_id == placement.lane.original_comparison_id
        and item.worker_id == placement.worker_id
        and item.worker_instance_id == placement.worker_instance_id
        and item.worker_epoch == placement.worker_epoch
        and item.release_target == placement.release_target
    ):
        raise ValueError("Target Baseline/Placement lineage 不一致。")


def _same_catalog_resolution(
    recorded: ReleaseChannelResolution,
    current: ReleaseChannelResolution,
) -> bool:
    return bool(
        recorded.catalog_id == current.catalog_id
        and recorded.catalog_sha256 == current.catalog_sha256
        and recorded.channel_trust_policy_id == current.channel_trust_policy_id
        and recorded.channel_trust_policy_sha256 == current.channel_trust_policy_sha256
        and recorded.build_trust_policy_id == current.build_trust_policy_id
        and recorded.build_trust_policy_sha256 == current.build_trust_policy_sha256
        and recorded.channel == current.channel
        and recorded.target == current.target
        and recorded.entry == current.entry
        and recorded.archive_urls == current.archive_urls
    )


def _resolution_matches_local(
    item: EvolutionPostRollbackTargetBaseline,
    resolution: ReleaseChannelResolution,
) -> bool:
    build = resolution.entry.build_attestation.payload
    return bool(
        resolution.target == item.release_target
        and resolution.entry.target == item.release_target
        and resolution.entry.version == item.local_baseline_version
        and build.version == item.local_baseline_version
        and build.source_commit == item.local_baseline_source_commit
        and build.source_tree_sha256 == item.local_baseline_source_tree_sha256
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_post_rollback_target_baselines ("
        "baseline_resolution_id TEXT PRIMARY KEY, "
        "baseline_resolution_sha256 TEXT NOT NULL UNIQUE, "
        "placement_id TEXT NOT NULL UNIQUE, placement_sha256 TEXT NOT NULL, "
        "channel TEXT NOT NULL, target TEXT NOT NULL, baseline_json TEXT NOT NULL, "
        "resolved_at TEXT NOT NULL)"
    )
    await db.commit()


def _restore(encoded: str) -> EvolutionPostRollbackTargetBaseline:
    try:
        payload = json.loads(encoded)
        if not isinstance(payload, dict):
            raise ValueError("Target Baseline payload 必须是对象。")
        return EvolutionPostRollbackTargetBaseline.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise EvolutionPostRollbackTargetBaselineError(
            "post_rollback_target_baseline_store_corrupt",
            "Target Baseline Resolution 无法验证。",
        ) from exc


def _request_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"evrerollbackreq_[0-9a-f]{24}", text) is None:
        raise EvolutionPostRollbackTargetBaselineError(
            "post_rollback_target_baseline_request_id_invalid",
            "Rollback Request ID 格式无效。",
        )
    return text


def _comparison_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(_SHA256_RE, text) is None:
        raise EvolutionPostRollbackTargetBaselineError(
            "post_rollback_target_baseline_comparison_id_invalid",
            "Comparison ID 格式无效。",
        )
    return text


def _placement_id(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"evpostplacement_[0-9a-f]{24}", text) is None:
        raise EvolutionPostRollbackTargetBaselineError(
            "post_rollback_target_baseline_placement_id_invalid",
            "Placement ID 格式无效。",
        )
    return text


def _channel(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(_CHANNEL_RE, text) is None:
        raise EvolutionPostRollbackTargetBaselineError(
            "post_rollback_target_baseline_channel_invalid",
            "Release channel 格式无效。",
        )
    return text


def _aware(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_POST_ROLLBACK_TARGET_BASELINE_POLICY",
    "EvolutionPostRollbackTargetBaseline",
    "EvolutionPostRollbackTargetBaselineError",
    "EvolutionPostRollbackTargetBaselineService",
    "EvolutionPostRollbackTargetBaselineStore",
    "EvolutionPostRollbackTargetBaselineView",
    "render_post_rollback_target_baseline",
]
