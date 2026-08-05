from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
    _lease_id,
    _worktree_name,
)
from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionEvidenceKind,
    EvolutionPromotionPackageInput,
    _migration_assessment,
    _patch_manifest,
    _rollback_plan,
    _sha256_payload,
)
from naumi_agent.evolution.revalidation_replays import (
    EVOLUTION_REVALIDATION_REPLAY_POLICY,
    EvolutionRevalidationReplayError,
    EvolutionRevalidationReplayExecutor,
    EvolutionRevalidationReplayStore,
    _unified_diff_sha256,
)
from naumi_agent.evolution.revalidation_requests import (
    EvolutionRevalidationRequest,
    EvolutionRevalidationRequestView,
)
from naumi_agent.safety.permissions import TOOL_PERMISSIONS, PermissionRiskLevel
from naumi_agent.tools.evolution_review import EvolutionRevalidationReplayTool
from tests.unit.test_evolution_promotion_package_inputs import (
    _accepted_reflection,
    _package,
)
from tests.unit.test_evolution_promotion_packages import _git, _repo


class _ReplayService:
    def __init__(self, receipt) -> None:
        self.receipt = receipt

    async def execute(self, **_kwargs):
        return self.receipt


def _rehash_input(payload: dict) -> EvolutionPromotionPackageInput:
    artifact = {
        key: value for key, value in payload.items() if key not in {"input_id", "input_sha256"}
    }
    digest = _sha256_payload(artifact)
    return EvolutionPromotionPackageInput.model_validate(
        {**artifact, "input_id": f"evpromoin_{digest[:24]}", "input_sha256": digest}
    )


def _rehash_request(payload: dict) -> EvolutionRevalidationRequest:
    artifact = {
        key: value for key, value in payload.items() if key not in {"request_id", "request_sha256"}
    }
    digest = _sha256_payload(artifact)
    return EvolutionRevalidationRequest.model_validate(
        {
            **artifact,
            "request_id": f"evrevalidation_{digest[:24]}",
            "request_sha256": digest,
        }
    )


def _fixture(root: Path):
    baseline = _repo(root)
    path = "src/naumi_agent/state/schema.py"
    before = (root / path).read_bytes()
    after = b"VERSION = 2\nFEATURE = 'replayed'\n"
    before_sha = hashlib.sha256(before).hexdigest()
    after_sha = hashlib.sha256(after).hexdigest()
    fact = SimpleNamespace(
        path=path,
        operation="modify",
        before_sha256=before_sha,
        after_sha256=after_sha,
        unified_diff_sha256=_unified_diff_sha256(path, before, after),
        added_lines=2,
        deleted_lines=1,
        api_change="additive",
    )
    fact.fact_sha256 = _sha256_payload(vars(fact))
    mutation = SimpleNamespace(
        mutation_receipt_id=f"evmr_{'6' * 24}",
        receipt_sha256="6" * 64,
        files=(fact,),
        files_sha256=_sha256_payload([{**vars(fact), "fact_sha256": fact.fact_sha256}]),
        total_added_lines=2,
        total_deleted_lines=1,
    )
    patch = _patch_manifest(mutation)
    memory = _accepted_reflection(root)
    package = _package(root, memory, baseline_commit=baseline, patch_path=path)
    migration = _migration_assessment(patch.files)
    rollback = _rollback_plan(patch.files, package.baseline, migration)
    package_payload = package.model_dump(mode="json")
    package_payload.update(
        mutation_receipt_id=mutation.mutation_receipt_id,
        mutation_receipt_sha256=mutation.receipt_sha256,
        patch=patch.model_dump(mode="json"),
        migration=migration.model_dump(mode="json"),
        rollback=rollback.model_dump(mode="json"),
    )
    for ref in package_payload["evidence_refs"]:
        if ref["kind"] == EvolutionPromotionEvidenceKind.MUTATION_RECEIPT.value:
            ref["authority_id"] = mutation.mutation_receipt_id
            ref["authority_sha256"] = mutation.receipt_sha256
    package = _rehash_input(package_payload)

    storage = root / ".naumi" / "worktrees"
    storage.mkdir(parents=True)
    worktree_name = _worktree_name(package.experiment_contract_id)
    source = storage / worktree_name
    branch = f"naumi/worktree-{worktree_name}"
    _git(root, "worktree", "add", "-b", branch, str(source), baseline)
    (source / path).write_bytes(after)
    now = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    lease = ExperimentWorktreeLease(
        lease_id=_lease_id(
            package.experiment_contract_id,
            package.experiment_contract_sha256,
        ),
        contract_id=package.experiment_contract_id,
        manifest_sha256=package.experiment_contract_sha256,
        session_id="session-replay",
        mission_id="mission-replay",
        task_id="task-replay",
        owner="owner-replay",
        state=ExperimentLeaseState.ACTIVE,
        worktree_name=worktree_name,
        worktree_path=str(source),
        branch=branch,
        baseline_commit=baseline,
        expires_at=(now + timedelta(hours=1)).isoformat(),
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        worktree_ready=True,
    )
    target_tree = _git(root, "rev-parse", f"{baseline}^{{tree}}")
    request_payload = {
        "schema_version": 1,
        "policy_version": "evolution-revalidation-request-v1",
        "workspace_root": str(root.resolve()),
        "decision_id": f"evapprovaldecision_{'1' * 24}",
        "decision_sha256": "1" * 64,
        "decision_source_set_sha256": "2" * 64,
        "requirement_id": f"evapprovalreq_{'3' * 24}",
        "requirement_sha256": "3" * 64,
        "package_id": f"evpromopkg_{'4' * 24}",
        "package_sha256": "4" * 64,
        "promotion_input_id": package.input_id,
        "promotion_input_sha256": package.input_sha256,
        "candidate_id": package.candidate_id,
        "candidate_revision": package.candidate_revision,
        "reflection_id": package.reflection_id,
        "reflection_sha256": package.reflection_sha256,
        "target_branch": "main",
        "target_head": baseline,
        "target_tree": target_tree,
        "baseline_commit": baseline,
        "target_relation": "same",
        "operation": "validate_exact_tree",
        "manual_reconciliation_required": False,
        "patch_manifest_sha256": package.patch.manifest_sha256,
        "baseline_sha256": package.baseline.baseline_sha256,
        "migration_assessment_sha256": package.migration.assessment_sha256,
        "rollback_plan_sha256": package.rollback.plan_sha256,
        "required_platforms": ["linux", "macos", "windows"],
        "sandbox_required": True,
        "exact_target_required": True,
        "current_approval_required": True,
        "validation_receipts_must_be_reissued": True,
        "network_allowed": False,
        "dependency_install_allowed": False,
        "main_worktree_write_allowed": False,
        "target_branch_write_allowed": False,
        "execution_started": False,
        "rebase_executed": False,
        "validation_executed": False,
        "promotion_authority": False,
        "merge_executed": False,
        "push_executed": False,
        "publish_executed": False,
        "contains_source_code": False,
        "contains_freeform_narrative": False,
        "llm_generated": False,
    }
    request = _rehash_request(request_payload)
    view = EvolutionRevalidationRequestView(
        request=request,
        source_readable=True,
        decision_current=True,
        decision_rebase_eligible=False,
        package_current=True,
        target_current=True,
        current_target_head=baseline,
        current_target_relation="same",
        current_status="ready",
        execution_eligible=True,
    )
    return now, storage, package, lease, view, path, before, after


@pytest.mark.asyncio
async def test_exact_candidate_is_replayed_detached_and_persisted_once(tmp_path: Path) -> None:
    now, storage, package, lease, view, path, before, after = _fixture(tmp_path)
    store = EvolutionRevalidationReplayStore(tmp_path / ".naumi" / "state.db")
    executor = EvolutionRevalidationReplayExecutor(
        store=store,
        worktree_storage_dir=storage,
        clock=lambda: now,
    )

    receipts = await asyncio.gather(
        *(executor.execute(request_view=view, package_input=package, lease=lease) for _ in range(6))
    )

    assert all(item == receipts[0] for item in receipts)
    receipt = receipts[0]
    assert receipt.policy_version == EVOLUTION_REVALIDATION_REPLAY_POLICY
    assert receipt.files[0].candidate_sha256 == hashlib.sha256(after).hexdigest()
    assert receipt.files[0].replay_sha256 == hashlib.sha256(after).hexdigest()
    assert receipt.source_worktree_unchanged
    assert receipt.main_worktree_unchanged
    assert receipt.target_branch_unchanged
    assert receipt.detached_worktree_removed
    assert not receipt.validation_executed
    assert not receipt.promotion_authority
    assert (tmp_path / path).read_bytes() == before
    assert (Path(lease.worktree_path) / path).read_bytes() == after
    assert not (storage / f"revalidation-{view.request.request_id[-24:]}").exists()
    assert await store.get_by_request(view.request.request_id) == receipt

    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_revalidation_replay_service=_ReplayService(receipt),
    )
    tool_output = await EvolutionRevalidationReplayTool(engine).execute(view.request.request_id)
    slash_output = await execute_slash_command(
        engine,
        f"/evolution revalidation-replay {view.request.request_id}",
    )
    assert receipt.replay_id in tool_output
    assert receipt.replay_id in slash_output
    rule = TOOL_PERMISSIONS["evolution_revalidation_replay"]
    assert not rule.requires_confirmation
    assert rule.risk_level is PermissionRiskLevel.MEDIUM
    assert rule.max_calls_per_session == 50

    import naumi_agent.evolution as evolution

    assert evolution.EvolutionRevalidationReplayExecutor is EvolutionRevalidationReplayExecutor


@pytest.mark.asyncio
async def test_stale_rebase_and_source_drift_fail_closed(tmp_path: Path) -> None:
    now, storage, package, lease, view, path, _before, _after = _fixture(tmp_path)
    executor = EvolutionRevalidationReplayExecutor(
        store=EvolutionRevalidationReplayStore(tmp_path / ".naumi" / "state.db"),
        worktree_storage_dir=storage,
        clock=lambda: now,
    )
    stale = view.model_copy(
        update={
            "target_current": False,
            "current_target_head": None,
            "current_target_relation": "unavailable",
            "current_status": "stale",
            "execution_eligible": False,
        }
    )
    with pytest.raises(EvolutionRevalidationReplayError) as ineligible:
        await executor.execute(request_view=stale, package_input=package, lease=lease)
    assert ineligible.value.code == "revalidation_replay_request_ineligible"

    rebase_request = _rehash_request(
        {
            **view.request.model_dump(mode="json"),
            "target_head": "a" * 40,
            "target_relation": "advanced",
            "operation": "rebase_then_validate",
        }
    )
    rebase = view.model_copy(
        update={
            "request": rebase_request,
            "decision_current": False,
            "decision_rebase_eligible": True,
            "target_current": False,
            "current_target_head": "a" * 40,
            "current_target_relation": "advanced",
            "current_status": "stale",
            "execution_eligible": True,
        }
    )
    with pytest.raises(EvolutionRevalidationReplayError) as unsupported:
        await executor.execute(request_view=rebase, package_input=package, lease=lease)
    assert unsupported.value.code == "revalidation_replay_rebase_not_implemented"

    (Path(lease.worktree_path) / path).write_text("tampered\n", encoding="utf-8")
    with pytest.raises(EvolutionRevalidationReplayError) as drifted:
        await executor.execute(request_view=view, package_input=package, lease=lease)
    assert drifted.value.code == "revalidation_replay_source_digest_mismatch"
