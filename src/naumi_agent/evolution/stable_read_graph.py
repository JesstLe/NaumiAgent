"""Compose the durable stable evidence graph behind one read-only inspector."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from naumi_agent.evolution.revalidation_opt_in_execution_outcome_ledger import (
    EvolutionRevalidationOptInExecutionOutcomeLedgerService,
    EvolutionRevalidationOptInExecutionOutcomeLedgerStore,
)
from naumi_agent.evolution.revalidation_opt_in_observation_window_assessments import (
    EvolutionRevalidationOptInObservationWindowService,
    EvolutionRevalidationOptInObservationWindowStore,
)
from naumi_agent.evolution.revalidation_opt_in_runtime_health import (
    EvolutionRevalidationOptInRuntimeHealthService,
)
from naumi_agent.evolution.revalidation_opt_in_stage_advances import (
    EvolutionRevalidationOptInStageAdvanceService,
    EvolutionRevalidationOptInStageAdvanceStore,
)
from naumi_agent.evolution.revalidation_opt_in_stage_completions import (
    EvolutionRevalidationOptInStageCompletionService,
    EvolutionRevalidationOptInStageCompletionStore,
)
from naumi_agent.evolution.revalidation_percentage_boot_preparations import (
    EvolutionRevalidationPercentageBootPreparationService,
    EvolutionRevalidationPercentageBootPreparationStore,
)
from naumi_agent.evolution.revalidation_percentage_cohort_assignments import (
    EvolutionRevalidationPercentageCohortAssignmentService,
    EvolutionRevalidationPercentageCohortAssignmentStore,
)
from naumi_agent.evolution.revalidation_percentage_deployment_intents import (
    EvolutionRevalidationPercentageDeploymentIntentService,
    EvolutionRevalidationPercentageDeploymentIntentStore,
)
from naumi_agent.evolution.revalidation_percentage_deployments import (
    EvolutionRevalidationPercentageDeploymentService,
    EvolutionRevalidationPercentageDeploymentStore,
)
from naumi_agent.evolution.revalidation_percentage_execution_outcome_ledger import (
    EvolutionRevalidationPercentageExecutionOutcomeLedgerService,
    EvolutionRevalidationPercentageExecutionOutcomeLedgerStore,
)
from naumi_agent.evolution.revalidation_percentage_observation_window_assessments import (
    EvolutionRevalidationPercentageObservationWindowService,
    EvolutionRevalidationPercentageObservationWindowStore,
)
from naumi_agent.evolution.revalidation_percentage_runtime_exposures import (
    EvolutionRevalidationPercentageRuntimeExposureService,
    EvolutionRevalidationPercentageRuntimeExposureStore,
)
from naumi_agent.evolution.revalidation_percentage_stage_advances import (
    EvolutionRevalidationPercentageStageAdvanceService,
    EvolutionRevalidationPercentageStageAdvanceStore,
)
from naumi_agent.evolution.revalidation_percentage_stage_completions import (
    EvolutionRevalidationPercentageStageCompletionService,
    EvolutionRevalidationPercentageStageCompletionStore,
)
from naumi_agent.evolution.revalidation_rollout_baselines import (
    EvolutionRevalidationRolloutBaselineService,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EvolutionRevalidationRolloutPlanService,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlStore,
)
from naumi_agent.evolution.revalidation_stable_boot_preparations import (
    EvolutionRevalidationStableBootPreparationService,
    EvolutionRevalidationStableBootPreparationStore,
)
from naumi_agent.evolution.revalidation_stable_deployment_intents import (
    EvolutionRevalidationStableDeploymentIntentService,
    EvolutionRevalidationStableDeploymentIntentStore,
)
from naumi_agent.evolution.revalidation_stable_deployments import (
    EvolutionRevalidationStableDeploymentService,
    EvolutionRevalidationStableDeploymentStore,
    EvolutionRevalidationStableDeploymentView,
)
from naumi_agent.evolution.revalidation_stable_execution_outcome_ledger import (
    EvolutionRevalidationStableExecutionOutcomeLedgerService,
    EvolutionRevalidationStableExecutionOutcomeLedgerStore,
)
from naumi_agent.evolution.revalidation_stable_observation_window_assessments import (
    EvolutionRevalidationStableObservationWindowService,
    EvolutionRevalidationStableObservationWindowStore,
)
from naumi_agent.evolution.revalidation_stable_runtime_exposures import (
    EvolutionRevalidationStableRuntimeExposureService,
    EvolutionRevalidationStableRuntimeExposureStore,
)
from naumi_agent.evolution.revalidation_stable_stage_completions import (
    EvolutionRevalidationStableStageCompletionService,
    EvolutionRevalidationStableStageCompletionStore,
    EvolutionRevalidationStableStageCompletionView,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.release.archive_admission import (
    ReleaseArchiveAdmissionService,
    ReleaseArchiveAdmissionStore,
)
from naumi_agent.release.artifact_fetch import ReleaseArtifactFetchService
from naumi_agent.release.population_registry import ReleasePopulationSnapshotStore
from naumi_agent.release.slots import ReleaseSlotStore
from naumi_agent.runs.store import ChatRunStore


async def _deny_write_port(*_args, **_kwargs):
    raise RuntimeError("stable_read_graph_write_port_disabled")


@dataclass(frozen=True, slots=True)
class EvolutionStableReadGraphInspector:
    """Narrow wrapper that deliberately exposes no assess/write operation."""

    _service: EvolutionRevalidationStableStageCompletionService
    _deployment_service: EvolutionRevalidationStableDeploymentService

    def __post_init__(self) -> None:
        if not isinstance(
            self._service,
            EvolutionRevalidationStableStageCompletionService,
        ):
            raise TypeError("Stable read graph 需要 exact 5f5r Service。")
        if not isinstance(
            self._deployment_service,
            EvolutionRevalidationStableDeploymentService,
        ):
            raise TypeError("Stable read graph 需要 exact Stable Deployment Service。")

    async def inspect(
        self,
        *,
        evidence_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationStableStageCompletionView:
        return await self._service.inspect(
            evidence_id=evidence_id,
            subject_id=subject_id,
        )

    async def inspect_stable_deployment(
        self,
        *,
        intent_id: str,
    ) -> EvolutionRevalidationStableDeploymentView:
        return await self._deployment_service.inspect(intent_id=intent_id)


class EvolutionLazyStableReadGraphInspector:
    """Build the source graph only when the first durable candidate needs it."""

    def __init__(
        self,
        factory: Callable[[], EvolutionStableReadGraphInspector],
    ) -> None:
        if not callable(factory):
            raise TypeError("Stable read graph factory 必须可调用。")
        self._factory = factory
        self._inspector: EvolutionStableReadGraphInspector | None = None
        self._lock = asyncio.Lock()

    @property
    def initialized(self) -> bool:
        return self._inspector is not None

    async def inspect(
        self,
        *,
        evidence_id: str,
        subject_id: str,
    ) -> EvolutionRevalidationStableStageCompletionView:
        inspector = await self._resolve()
        return await inspector.inspect(
            evidence_id=evidence_id,
            subject_id=subject_id,
        )

    async def inspect_stable_deployment(
        self,
        *,
        intent_id: str,
    ) -> EvolutionRevalidationStableDeploymentView:
        inspector = await self._resolve()
        return await inspector.inspect_stable_deployment(intent_id=intent_id)

    async def _resolve(self) -> EvolutionStableReadGraphInspector:
        inspector = self._inspector
        if inspector is None:
            async with self._lock:
                inspector = self._inspector
                if inspector is None:
                    inspector = self._factory()
                    if not isinstance(inspector, EvolutionStableReadGraphInspector):
                        raise TypeError("Stable read graph factory 返回类型无效。")
                    self._inspector = inspector
        return inspector


def build_evolution_stable_read_graph_inspector(
    *,
    workspace_root: str | Path,
    evolution_db_path: str | Path,
    release_staging_root: str | Path,
    harness_store: HarnessStore,
    chat_run_store: ChatRunStore,
    opt_in_runtime_health_service: EvolutionRevalidationOptInRuntimeHealthService,
    plan_service: EvolutionRevalidationRolloutPlanService,
    baseline_service: EvolutionRevalidationRolloutBaselineService,
    control_store: EvolutionRevalidationRolloutControlStore,
    population_store: ReleasePopulationSnapshotStore,
    artifact_fetch_service: ReleaseArtifactFetchService,
    release_slot_store: ReleaseSlotStore,
    channel: str = "stable",
) -> EvolutionStableReadGraphInspector:
    """Rebuild all durable inspect dependencies without touching their sources."""

    root = Path(workspace_root).expanduser().resolve(strict=True)
    db_path = Path(evolution_db_path).expanduser().resolve()
    if not (
        db_path
        == opt_in_runtime_health_service.store.db_path
        == plan_service.store.db_path
        == baseline_service.store.db_path
        == control_store.db_path
    ):
        raise ValueError("Stable read graph 必须复用同一 Evolution evidence DB。")
    if not (
        root
        == opt_in_runtime_health_service.workspace_root
        == plan_service.workspace_root
        == baseline_service.workspace_root
    ):
        raise ValueError("Stable read graph workspace dependency 不一致。")

    archive_store = ReleaseArchiveAdmissionStore(
        artifact_fetch_service.store.db_path,
        download_store=artifact_fetch_service.store,
    )
    archive_service = ReleaseArchiveAdmissionService(
        staging_root=release_staging_root,
        fetch_service=artifact_fetch_service,
        slot_store=release_slot_store,
        store=archive_store,
    )

    opt_in_window_store = EvolutionRevalidationOptInObservationWindowStore(
        db_path,
        runtime_health_store=opt_in_runtime_health_service.store,
        harness_store=harness_store,
    )
    opt_in_window_service = EvolutionRevalidationOptInObservationWindowService(
        workspace_root=root,
        runtime_health_service=opt_in_runtime_health_service,
        harness_store=harness_store,
        store=opt_in_window_store,
    )
    opt_in_outcome_store = EvolutionRevalidationOptInExecutionOutcomeLedgerStore(
        db_path,
        window_store=opt_in_window_store,
        chat_run_store=chat_run_store,
        harness_store=harness_store,
    )
    opt_in_outcome_service = EvolutionRevalidationOptInExecutionOutcomeLedgerService(
        workspace_root=root,
        window_service=opt_in_window_service,
        harness_store=harness_store,
        chat_run_store=chat_run_store,
        store=opt_in_outcome_store,
    )
    opt_in_completion_store = EvolutionRevalidationOptInStageCompletionStore(
        db_path,
        outcome_service=opt_in_outcome_service,
        window_service=opt_in_window_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
    )
    opt_in_completion_service = EvolutionRevalidationOptInStageCompletionService(
        workspace_root=root,
        outcome_service=opt_in_outcome_service,
        window_service=opt_in_window_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
        store=opt_in_completion_store,
    )
    opt_in_advance_store = EvolutionRevalidationOptInStageAdvanceStore(
        db_path,
        completion_service=opt_in_completion_service,
        plan_service=plan_service,
        control_store=control_store,
        interaction_store=harness_store,
    )
    opt_in_advance_service = EvolutionRevalidationOptInStageAdvanceService(
        workspace_root=root,
        completion_service=opt_in_completion_service,
        plan_service=plan_service,
        control_store=control_store,
        interaction_store=harness_store,
        store=opt_in_advance_store,
        request_user_input=_deny_write_port,
    )

    assignment_store = EvolutionRevalidationPercentageCohortAssignmentStore(
        db_path,
        advance_service=opt_in_advance_service,
        population_store=population_store,
        plan_service=plan_service,
    )
    assignment_service = EvolutionRevalidationPercentageCohortAssignmentService(
        workspace_root=root,
        channel=channel,
        advance_service=opt_in_advance_service,
        population_store=population_store,
        plan_service=plan_service,
        store=assignment_store,
        sign_assignment_challenge=_deny_write_port,
    )
    percentage_intent_store = EvolutionRevalidationPercentageDeploymentIntentStore(
        db_path,
        assignment_service=assignment_service,
        archive_service=archive_service,
    )
    percentage_intent_service = EvolutionRevalidationPercentageDeploymentIntentService(
        workspace_root=root,
        assignment_service=assignment_service,
        archive_service=archive_service,
        store=percentage_intent_store,
    )
    percentage_boot_store = EvolutionRevalidationPercentageBootPreparationStore(
        db_path,
        intent_service=percentage_intent_service,
        release_slot_store=release_slot_store,
    )
    percentage_boot_service = EvolutionRevalidationPercentageBootPreparationService(
        workspace_root=root,
        owner_id="stable-read-graph-percentage",
        intent_service=percentage_intent_service,
        store=percentage_boot_store,
    )
    percentage_deployment_store = EvolutionRevalidationPercentageDeploymentStore(
        db_path,
        preparation_store=percentage_boot_store,
        release_slot_store=release_slot_store,
    )
    percentage_deployment_service = EvolutionRevalidationPercentageDeploymentService(
        workspace_root=root,
        preparation_service=percentage_boot_service,
        store=percentage_deployment_store,
    )
    percentage_exposure_store = EvolutionRevalidationPercentageRuntimeExposureStore(
        db_path,
        deployment_store=percentage_deployment_store,
        harness_store=harness_store,
    )
    percentage_exposure_service = EvolutionRevalidationPercentageRuntimeExposureService(
        workspace_root=root,
        deployment_service=percentage_deployment_service,
        harness_store=harness_store,
        store=percentage_exposure_store,
    )
    percentage_window_store = EvolutionRevalidationPercentageObservationWindowStore(
        db_path,
        exposure_store=percentage_exposure_store,
        harness_store=harness_store,
    )
    percentage_window_service = EvolutionRevalidationPercentageObservationWindowService(
        workspace_root=root,
        exposure_service=percentage_exposure_service,
        harness_store=harness_store,
        store=percentage_window_store,
    )
    percentage_outcome_store = EvolutionRevalidationPercentageExecutionOutcomeLedgerStore(
        db_path,
        window_store=percentage_window_store,
        chat_run_store=chat_run_store,
        harness_store=harness_store,
    )
    percentage_outcome_service = (
        EvolutionRevalidationPercentageExecutionOutcomeLedgerService(
            workspace_root=root,
            window_service=percentage_window_service,
            harness_store=harness_store,
            chat_run_store=chat_run_store,
            store=percentage_outcome_store,
        )
    )
    percentage_completion_store = EvolutionRevalidationPercentageStageCompletionStore(
        db_path,
        outcome_service=percentage_outcome_service,
        window_service=percentage_window_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
    )
    percentage_completion_service = EvolutionRevalidationPercentageStageCompletionService(
        workspace_root=root,
        outcome_service=percentage_outcome_service,
        window_service=percentage_window_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
        store=percentage_completion_store,
    )
    percentage_advance_store = EvolutionRevalidationPercentageStageAdvanceStore(
        db_path,
        completion_service=percentage_completion_service,
        plan_service=plan_service,
        control_store=control_store,
        interaction_store=harness_store,
    )
    percentage_advance_service = EvolutionRevalidationPercentageStageAdvanceService(
        workspace_root=root,
        completion_service=percentage_completion_service,
        plan_service=plan_service,
        control_store=control_store,
        interaction_store=harness_store,
        store=percentage_advance_store,
        request_user_input=_deny_write_port,
    )

    stable_intent_store = EvolutionRevalidationStableDeploymentIntentStore(
        db_path,
        stage_advance_service=percentage_advance_service,
        plan_service=plan_service,
        population_store=population_store,
        archive_service=archive_service,
    )
    stable_intent_service = EvolutionRevalidationStableDeploymentIntentService(
        workspace_root=root,
        channel=channel,
        store=stable_intent_store,
        sign_challenge=_deny_write_port,
    )
    stable_boot_store = EvolutionRevalidationStableBootPreparationStore(
        db_path,
        intent_service=stable_intent_service,
        release_slot_store=release_slot_store,
    )
    stable_boot_service = EvolutionRevalidationStableBootPreparationService(
        workspace_root=root,
        owner_id="stable-read-graph-stable",
        intent_service=stable_intent_service,
        store=stable_boot_store,
    )
    stable_deployment_store = EvolutionRevalidationStableDeploymentStore(
        db_path,
        preparation_store=stable_boot_store,
        release_slot_store=release_slot_store,
    )
    stable_deployment_service = EvolutionRevalidationStableDeploymentService(
        workspace_root=root,
        preparation_service=stable_boot_service,
        store=stable_deployment_store,
    )
    stable_exposure_store = EvolutionRevalidationStableRuntimeExposureStore(
        db_path,
        deployment_store=stable_deployment_store,
        harness_store=harness_store,
    )
    stable_exposure_service = EvolutionRevalidationStableRuntimeExposureService(
        workspace_root=root,
        deployment_service=stable_deployment_service,
        harness_store=harness_store,
        store=stable_exposure_store,
    )
    stable_window_store = EvolutionRevalidationStableObservationWindowStore(
        db_path,
        exposure_store=stable_exposure_store,
        harness_store=harness_store,
    )
    stable_window_service = EvolutionRevalidationStableObservationWindowService(
        workspace_root=root,
        exposure_service=stable_exposure_service,
        harness_store=harness_store,
        store=stable_window_store,
    )
    stable_outcome_store = EvolutionRevalidationStableExecutionOutcomeLedgerStore(
        db_path,
        window_store=stable_window_store,
        chat_run_store=chat_run_store,
        harness_store=harness_store,
    )
    stable_outcome_service = EvolutionRevalidationStableExecutionOutcomeLedgerService(
        workspace_root=root,
        window_service=stable_window_service,
        harness_store=harness_store,
        chat_run_store=chat_run_store,
        store=stable_outcome_store,
    )
    stable_completion_store = EvolutionRevalidationStableStageCompletionStore(
        db_path,
        outcome_service=stable_outcome_service,
        window_service=stable_window_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
    )
    stable_completion_service = EvolutionRevalidationStableStageCompletionService(
        workspace_root=root,
        outcome_service=stable_outcome_service,
        window_service=stable_window_service,
        plan_service=plan_service,
        baseline_service=baseline_service,
        store=stable_completion_store,
    )
    return EvolutionStableReadGraphInspector(
        stable_completion_service,
        stable_deployment_service,
    )


__all__ = [
    "EvolutionLazyStableReadGraphInspector",
    "EvolutionStableReadGraphInspector",
    "build_evolution_stable_read_graph_inspector",
]
