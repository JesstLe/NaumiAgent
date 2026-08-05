from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.revalidation_outcomes import (
    EvolutionRevalidationOutcomeError,
    EvolutionRevalidationOutcomeService,
    EvolutionRevalidationOutcomeStatus,
    EvolutionRevalidationOutcomeStore,
)
from naumi_agent.evolution.revalidation_replays import (
    EvolutionRevalidationReplayExecutor,
    EvolutionRevalidationReplayStore,
)
from naumi_agent.evolution.revalidation_validations import (
    EvolutionRevalidationValidationService,
    EvolutionRevalidationValidationStore,
)
from naumi_agent.harness.sandbox_checks import HarnessSandboxCheckStatus
from naumi_agent.safety.permissions import (
    TOOL_PERMISSIONS,
    PermissionRiskLevel,
)
from naumi_agent.tools.evolution_review import EvolutionRevalidationOutcomeTool
from tests.unit.test_evolution_revalidation_replays import _fixture
from tests.unit.test_evolution_revalidation_validations import (
    _FakeHarness,
    _StaticExecutionService,
    _StaticRequestService,
    _StaticStore,
)


async def _scenario(tmp_path: Path, *, status: HarnessSandboxCheckStatus):
    now, storage, package, lease, view, _path, _before, _after = _fixture(tmp_path)
    replay = await EvolutionRevalidationReplayExecutor(
        store=EvolutionRevalidationReplayStore(tmp_path / ".naumi" / "state.db"),
        worktree_storage_dir=storage,
        clock=lambda: now,
    ).execute(request_view=view, package_input=package, lease=lease)
    harness = _FakeHarness(status=status)
    validation_store = EvolutionRevalidationValidationStore(
        tmp_path / ".naumi" / "state.db"
    )
    validation = EvolutionRevalidationValidationService(
        execution_service=_StaticExecutionService(replay),
        request_service=_StaticRequestService(view),
        package_input_store=_StaticStore(
            type("PackageView", (), {
                "promotion_review_eligible": True,
                "package_input": package,
            })()
        ),
        lease_store=_StaticStore(lease),
        harness=harness,
        store=validation_store,
        clock=lambda: now,
    )
    receipt = await validation.execute(
        workspace_root=tmp_path, request_id=view.request.request_id
    )
    outcome_store = EvolutionRevalidationOutcomeStore(
        tmp_path / ".naumi" / "state.db"
    )
    outcome_service = EvolutionRevalidationOutcomeService(
        request_service=_StaticRequestService(view),
        validation_store=validation_store,
        package_input_store=_StaticStore(
            type("PackageView", (), {"package_input": package})()
        ),
        harness=harness,
        store=outcome_store,
        clock=lambda: now,
    )
    return (
        package,
        view,
        receipt,
        harness,
        validation,
        outcome_store,
        outcome_service,
    )


@pytest.mark.asyncio
async def test_passed_outcome_atomically_invalidates_old_promotion_authorities(
    tmp_path: Path,
) -> None:
    package, view, receipt, _harness, _validation, store, service = await _scenario(
        tmp_path, status=HarnessSandboxCheckStatus.PASSED
    )

    first = await service.issue(
        workspace_root=tmp_path, request_id=view.request.request_id
    )
    repeated = await service.issue(
        workspace_root=tmp_path, request_id=view.request.request_id
    )

    assert first == repeated
    assert first.outcome.status is EvolutionRevalidationOutcomeStatus.VALIDATED
    assert first.outcome.old_evidence_invalidated
    assert first.outcome.approval_reaggregation_required
    assert first.outcome.professional_signatures_reissue_required
    assert first.rollout_candidate_eligible
    assert not first.outcome.promotion_authority
    invalidated = await store.invalidation(
        kind="final_evaluation",
        authority_id=package.final_evaluation_receipt_id,
    )
    assert invalidated is not None
    assert invalidated.authority_sha256 == package.final_evaluation_receipt_sha256
    approval = await store.invalidation(
        kind="approval_decision", authority_id=view.request.decision_id
    )
    assert approval is not None
    assert receipt.receipt_id not in {
        item.authority_id for item in first.outcome.invalidated_authorities
    }


@pytest.mark.asyncio
async def test_failed_validation_still_invalidates_old_authority_and_blocks_rollout(
    tmp_path: Path,
) -> None:
    _package, view, _receipt, _harness, _validation, store, service = await _scenario(
        tmp_path, status=HarnessSandboxCheckStatus.FAILED
    )

    result = await service.issue(
        workspace_root=tmp_path, request_id=view.request.request_id
    )

    assert result.outcome.status is EvolutionRevalidationOutcomeStatus.VALIDATION_FAILED
    assert not result.rollout_candidate_eligible
    assert await store.invalidation(
        kind="approval_decision", authority_id=view.request.decision_id
    ) is not None


@pytest.mark.asyncio
async def test_profile_drift_makes_existing_outcome_stale(tmp_path: Path) -> None:
    _package, view, _receipt, harness, validation, _store, service = await _scenario(
        tmp_path, status=HarnessSandboxCheckStatus.PASSED
    )
    issued = await service.issue(
        workspace_root=tmp_path, request_id=view.request.request_id
    )
    harness.check = harness.check.model_copy(update={"timeout_seconds": 31})

    inspected = await service.inspect(
        workspace_root=tmp_path, outcome_id=issued.outcome.outcome_id
    )

    assert not inspected.source_current
    assert inspected.current_status == "stale"
    assert not inspected.rollout_candidate_eligible

    refreshed_receipt = await validation.execute(
        workspace_root=tmp_path, request_id=view.request.request_id
    )
    refreshed = await service.issue(
        workspace_root=tmp_path, request_id=view.request.request_id
    )
    assert refreshed_receipt.receipt_id != issued.outcome.validation_receipt_id
    assert refreshed.outcome.outcome_id != issued.outcome.outcome_id
    assert refreshed.source_current
    assert refreshed.rollout_candidate_eligible


@pytest.mark.asyncio
async def test_stale_sources_cannot_issue_first_outcome(tmp_path: Path) -> None:
    _package, view, _receipt, harness, _validation, _store, service = await _scenario(
        tmp_path, status=HarnessSandboxCheckStatus.PASSED
    )
    harness.check = harness.check.model_copy(update={"timeout_seconds": 31})

    with pytest.raises(EvolutionRevalidationOutcomeError) as exc:
        await service.issue(
            workspace_root=tmp_path, request_id=view.request.request_id
        )

    assert exc.value.code == "revalidation_outcome_source_stale"


@pytest.mark.asyncio
async def test_outcome_is_available_to_agent_tool_and_slash_command(tmp_path: Path) -> None:
    _package, view, _receipt, _harness, _validation, _store, service = await _scenario(
        tmp_path, status=HarnessSandboxCheckStatus.PASSED
    )
    engine = type(
        "Engine",
        (),
        {
            "workspace_root": tmp_path,
            "evolution_revalidation_outcome_service": service,
        },
    )()

    tool_output = await EvolutionRevalidationOutcomeTool(engine).execute(
        view.request.request_id
    )
    slash_output = await execute_slash_command(
        engine, f"/evolution revalidation-outcome {view.request.request_id}"
    )

    assert "Old evidence invalidated：`true`" in tool_output
    assert "Old evidence invalidated" in slash_output
    assert "Promotion authority" in slash_output
    rule = TOOL_PERMISSIONS["evolution_revalidation_outcome"]
    assert rule.risk_level is PermissionRiskLevel.MEDIUM
    assert not rule.requires_confirmation
