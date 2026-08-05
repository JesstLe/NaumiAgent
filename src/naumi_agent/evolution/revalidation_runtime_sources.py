"""Shared current-target RED and immutable-overlay GREEN runtime sources."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from naumi_agent.evolution.revalidation_evaluation_sources import (
    EvolutionRevalidationEvaluationSourceSnapshot,
    EvolutionRevalidationEvaluationSourceStore,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlan,
    EvolutionRevalidationValidationPlanService,
)
from naumi_agent.harness.eval_identity import HarnessEvalSourceIdentity
from naumi_agent.harness.sandbox_checks import HarnessSandboxSourceOverlay
from naumi_agent.harness.sandbox_eval import HarnessSandboxEvalSource


class EvolutionRevalidationRuntimeSourceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvolutionRevalidationRuntimeSourcePair:
    validation_plan: EvolutionRevalidationValidationPlan
    source_snapshot: EvolutionRevalidationEvaluationSourceSnapshot
    red: HarnessSandboxEvalSource
    green: HarnessSandboxEvalSource
    red_identity: HarnessEvalSourceIdentity
    green_identity: HarnessEvalSourceIdentity
    overlays: tuple[HarnessSandboxSourceOverlay, ...]
    green_materialized_tree_sha256: str

    def __post_init__(self) -> None:
        plan = self.validation_plan
        snapshot = self.source_snapshot
        if not (
            self.red.revision == self.green.revision == plan.red_revision
            and self.red.revision_tree_sha256
            == self.green.revision_tree_sha256
            == plan.red_tree_sha256
            and not self.red.overlays
            and self.red.overlay_source_sha256 is None
            and self.green.overlays == self.overlays
            and self.green.overlay_source_sha256
            == plan.green_overlay_source_sha256
            and snapshot.snapshot_id == plan.source_snapshot_id
            and snapshot.snapshot_sha256 == plan.source_snapshot_sha256
        ):
            raise ValueError("Revalidation runtime RED/GREEN source pair 不一致。")
        if self.red_identity.dirty or not self.green_identity.dirty:
            raise ValueError("Revalidation runtime source identity phase 不一致。")
        if self.green_materialized_tree_sha256 != _green_tree_sha256(
            plan.red_tree_sha256,
            plan.green_overlay_source_sha256,
        ):
            raise ValueError("Revalidation GREEN materialized tree digest 不一致。")


class EvolutionRevalidationRuntimeSourceService:
    """Materialize both evaluation phases without a Candidate Lease/worktree."""

    def __init__(
        self,
        *,
        validation_plan_service: EvolutionRevalidationValidationPlanService,
        source_store: EvolutionRevalidationEvaluationSourceStore,
    ) -> None:
        self._plan_service = validation_plan_service
        self._source_store = source_store

    async def materialize(
        self,
        *,
        workspace_root: str | Path,
        validation_plan_id: str,
    ) -> EvolutionRevalidationRuntimeSourcePair:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        view = await self._plan_service.inspect(
            workspace_root=workspace,
            validation_plan_id=validation_plan_id,
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationRuntimeSourceError(
                "revalidation_runtime_validation_plan_stale",
                "Revalidation Validation Plan 已 stale，不能物化评测源码。",
            )
        plan = view.plan
        if plan.workspace_root != workspace:
            raise EvolutionRevalidationRuntimeSourceError(
                "revalidation_runtime_workspace_mismatch",
                "Runtime source 工作区与 Validation Plan 不一致。",
            )
        snapshot = await self._source_store.get(plan.source_snapshot_id)
        if snapshot is None or snapshot.snapshot_sha256 != plan.source_snapshot_sha256:
            raise EvolutionRevalidationRuntimeSourceError(
                "revalidation_runtime_source_missing",
                "Immutable Evaluation Source 不存在或 digest 不一致。",
            )
        overlays = await self._source_store.load_overlays(snapshot.snapshot_id)
        _require_exact_overlay(plan, snapshot, overlays)

        async def source_is_current() -> bool:
            try:
                current = await self._plan_service.inspect(
                    workspace_root=workspace,
                    validation_plan_id=plan.validation_plan_id,
                )
                observed = await self._source_store.get(snapshot.snapshot_id)
                if not current.execution_eligible or current.plan != plan or observed != snapshot:
                    return False
                await self._source_store.verify_blobs(snapshot)
                return True
            except (OSError, TypeError, ValueError, RuntimeError):
                return False

        red = HarnessSandboxEvalSource(
            revision=plan.red_revision,
            revision_tree_sha256=plan.red_tree_sha256,
            source_is_current=source_is_current,
        )
        green = HarnessSandboxEvalSource(
            revision=plan.green_revision,
            revision_tree_sha256=plan.green_tree_sha256,
            overlays=overlays,
            overlay_source_sha256=plan.green_overlay_source_sha256,
            source_is_current=source_is_current,
        )
        materialized = _green_tree_sha256(
            plan.green_tree_sha256,
            plan.green_overlay_source_sha256,
        )
        return EvolutionRevalidationRuntimeSourcePair(
            validation_plan=plan,
            source_snapshot=snapshot,
            red=red,
            green=green,
            red_identity=HarnessEvalSourceIdentity(
                commit=plan.red_revision,
                tree_sha256=f"sha256:{plan.red_tree_sha256}",
                dirty=False,
            ),
            green_identity=HarnessEvalSourceIdentity(
                commit=plan.green_revision,
                tree_sha256=f"sha256:{materialized}",
                dirty=True,
            ),
            overlays=overlays,
            green_materialized_tree_sha256=materialized,
        )


def _require_exact_overlay(plan, snapshot, overlays) -> None:
    planned = tuple(
        (item.path, item.green_sha256, item.executable) for item in plan.files
    )
    captured = tuple(
        (item.path, item.sha256, item.executable) for item in snapshot.blobs
    )
    loaded = tuple((item.path, item.sha256, item.executable) for item in overlays)
    if not (
        planned == captured == loaded
        and snapshot.target_head == plan.green_revision
        and snapshot.target_tree_sha256 == plan.green_tree_sha256
        and snapshot.overlay_source_sha256 == plan.green_overlay_source_sha256
    ):
        raise EvolutionRevalidationRuntimeSourceError(
            "revalidation_runtime_overlay_mismatch",
            "Validation Plan、Source Snapshot 与 loaded overlays 不一致。",
        )


def _green_tree_sha256(revision_tree_sha256: str, overlay_sha256: str) -> str:
    payload = {
        "revision_tree_sha256": revision_tree_sha256,
        "overlay_source_sha256": overlay_sha256,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


__all__ = [
    "EvolutionRevalidationRuntimeSourceError",
    "EvolutionRevalidationRuntimeSourcePair",
    "EvolutionRevalidationRuntimeSourceService",
]
