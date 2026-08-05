"""Shared exact/rebase dispatch for Evolution revalidation source replay."""

from __future__ import annotations

from pathlib import Path

from naumi_agent.evolution.experiment_leases import EvolutionExperimentLeaseStore
from naumi_agent.evolution.promotion_package_inputs import EvolutionPromotionPackageInputStore
from naumi_agent.evolution.revalidation_rebases import (
    EvolutionRevalidationRebaseExecutor,
    EvolutionRevalidationRebaseOutcome,
    render_evolution_revalidation_rebase,
)
from naumi_agent.evolution.revalidation_replays import (
    EvolutionRevalidationReplayError,
    EvolutionRevalidationReplayExecutor,
    EvolutionRevalidationReplayReceipt,
    render_evolution_revalidation_replay,
)
from naumi_agent.evolution.revalidation_requests import EvolutionRevalidationRequestService

type EvolutionRevalidationExecutionOutcome = (
    EvolutionRevalidationReplayReceipt | EvolutionRevalidationRebaseOutcome
)


class EvolutionRevalidationExecutionService:
    """Resolve current authorities once, then dispatch exact or advanced replay."""

    def __init__(
        self,
        *,
        request_service: EvolutionRevalidationRequestService,
        package_input_store: EvolutionPromotionPackageInputStore,
        lease_store: EvolutionExperimentLeaseStore,
        exact_executor: EvolutionRevalidationReplayExecutor,
        rebase_executor: EvolutionRevalidationRebaseExecutor,
    ) -> None:
        self._request_service = request_service
        self._package_input_store = package_input_store
        self._lease_store = lease_store
        self._exact_executor = exact_executor
        self._rebase_executor = rebase_executor

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        request_id: str,
    ) -> EvolutionRevalidationExecutionOutcome:
        view = await self._request_service.inspect(
            workspace_root=workspace_root,
            request_id=request_id,
        )
        package_view = await self._package_input_store.get(view.request.promotion_input_id)
        if package_view is None or not package_view.promotion_review_eligible:
            raise EvolutionRevalidationReplayError(
                "revalidation_execution_package_input_ineligible",
                "Promotion Package Input 不存在、已撤销或不可读取。",
            )
        lease = await self._lease_store.get(package_view.package_input.experiment_contract_id)
        if lease is None:
            raise EvolutionRevalidationReplayError(
                "revalidation_execution_lease_missing",
                "Experiment Lease 不存在，无法恢复 Candidate 源码。",
            )
        if view.current_target_relation == "same":
            return await self._exact_executor.execute(
                request_view=view,
                package_input=package_view.package_input,
                lease=lease,
            )
        if view.current_target_relation == "advanced":
            return await self._rebase_executor.execute(
                request_view=view,
                package_input=package_view.package_input,
                lease=lease,
            )
        raise EvolutionRevalidationReplayError(
            "revalidation_execution_target_ineligible",
            "当前 target 不满足 exact replay 或线性 rebase 条件。",
        )


def render_evolution_revalidation_execution(
    outcome: EvolutionRevalidationExecutionOutcome,
) -> str:
    if isinstance(outcome, EvolutionRevalidationReplayReceipt):
        return render_evolution_revalidation_replay(outcome)
    if isinstance(outcome, EvolutionRevalidationRebaseOutcome):
        return render_evolution_revalidation_rebase(outcome)
    raise TypeError("未知 Revalidation Execution Outcome。")


__all__ = [
    "EvolutionRevalidationExecutionOutcome",
    "EvolutionRevalidationExecutionService",
    "render_evolution_revalidation_execution",
]
