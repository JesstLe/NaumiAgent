"""Evolution compatibility adapter for the shared Harness Sandbox Batch."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import RunDelegationGrantAuthority
from naumi_agent.harness.sandbox_batch import (
    HarnessSandboxBatchCoordinator,
    HarnessSandboxBatchError,
)
from naumi_agent.harness.store import HarnessStore

EvolutionInterventionalCohortKernelError = HarnessSandboxBatchError


class EvolutionInterventionalCohortKernel(HarnessSandboxBatchCoordinator):
    """Preserve Evolution owner/idempotency/error semantics on shared governance."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store: HarnessStore,
        permission_store: PermissionDecisionReceiptStore,
        run_grant_authority: RunDelegationGrantAuthority,
        now: Callable[[], str] | None = None,
        token: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(
            workspace_root=workspace_root,
            store=store,
            permission_store=permission_store,
            run_grant_authority=run_grant_authority,
            now=now,
            token=token,
            compatibility_scope="evolution",
        )


__all__ = [
    "EvolutionInterventionalCohortKernel",
    "EvolutionInterventionalCohortKernelError",
]
