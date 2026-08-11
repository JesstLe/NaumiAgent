from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.experiments import (
    EvolutionExperimentContract,
    EvolutionExperimentContractStore,
    _manifest_digest,
)
from naumi_agent.evolution.opportunity_discovery import (
    EvolutionOutcomeOpportunityError,
    EvolutionOutcomeOpportunityService,
)
from naumi_agent.evolution.post_rollback_runtime_verifications import (
    EvolutionPostRollbackRuntimeVerificationError,
    EvolutionPostRollbackRuntimeVerificationService,
    EvolutionPostRollbackRuntimeVerificationStore,
    render_post_rollback_runtime_verification,
)
from naumi_agent.evolution.promotion_package_inputs import (
    EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY,
    EvolutionPromotionEvidenceKind,
    EvolutionPromotionPackageInput,
    EvolutionPromotionRollbackOperation,
    EvolutionPromotionRollbackPlan,
    EvolutionPromotionRollbackStep,
    _baseline,
    _migration_assessment,
    _patch_manifest,
    _rollback_plan,
    _sha256_payload,
)
from naumi_agent.evolution.proposal_outcomes import (
    EvolutionProposalOutcomeProjectionError,
    EvolutionProposalOutcomeProjectionService,
)
from naumi_agent.evolution.revalidation_promotion_inputs import (
    EVOLUTION_REVALIDATION_PROMOTION_INPUT_POLICY,
    EvolutionRevalidationPromotionInput,
    EvolutionRevalidationPromotionInputStore,
)
from naumi_agent.evolution.revalidation_promotion_inputs import (
    _sha256_payload as _fresh_sha256,
)
from naumi_agent.evolution.revalidation_rollback_executions import (
    EvolutionRevalidationRollbackExecutionError,
    EvolutionRevalidationRollbackExecutionService,
    EvolutionRevalidationRollbackExecutionStore,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcomeError,
    EvolutionRevalidationRollbackOutcomeService,
    EvolutionRevalidationRollbackOutcomeStore,
)
from naumi_agent.evolution.revalidation_rollback_requests import (
    EVOLUTION_REVALIDATION_ROLLBACK_REQUEST_POLICY,
    EvolutionRevalidationRollbackRequest,
    EvolutionRevalidationRollbackRequestStore,
)
from naumi_agent.evolution.revalidation_rollback_sources import (
    EvolutionRevalidationRollbackSourceService,
    EvolutionRevalidationRollbackSourceStore,
)
from naumi_agent.evolution.revalidation_rollout_plans import (
    EVOLUTION_REVALIDATION_ROLLOUT_PLAN_POLICY,
    EvolutionRevalidationRolloutPlan,
    EvolutionRevalidationRolloutPlanStore,
    _stages,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
    EvolutionRevalidationRolloutControlStore,
)
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.release.slots import ReleaseSlotStore
from naumi_agent.safety.permissions import (
    PermissionChecker,
    PermissionMode,
    PermissionOutcome,
    PermissionReasonCode,
    PermissionRiskLevel,
)
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionOutcomeOpportunityTool,
    EvolutionPostRollbackRuntimeVerificationTool,
    EvolutionRevalidationRollbackExecutionTool,
    EvolutionRevalidationRollbackOutcomeTool,
)
from naumi_agent.ui.command_index import build_terminal_command_index
from tests.unit.test_evolution_promotion_package_inputs import (
    _accepted_reflection,
    _package,
)
from tests.unit.test_evolution_revalidation_promotion_inputs import (
    _persist_prior_dependency,
)
from tests.unit.test_release_slots import _bundle

T0 = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    ).stdout


class _FailOnceStore(EvolutionRevalidationRollbackExecutionStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.failed = False

    async def record(self, receipt):
        if not self.failed:
            self.failed = True
            raise OSError("simulated crash after release pointer commit")
        return await super().record(receipt)


async def _scenario(
    tmp_path: Path,
    *,
    data_restore_required: bool = False,
    with_outcome_lineage: bool = False,
):
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "rollback@example.com")
    _git(root, "config", "user.name", "Rollback Test")
    baseline_bytes = b"print('baseline')\n"
    candidate_bytes = b"print('candidate')\n"
    (root / "app.py").write_bytes(baseline_bytes)
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "baseline")
    baseline_commit = _git(root, "rev-parse", "HEAD").decode().strip()
    baseline_listing = _git(root, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    baseline_tree = hashlib.sha256(baseline_listing).hexdigest()
    (root / "app.py").write_bytes(candidate_bytes)
    (root / "new.py").write_bytes(b"created = True\n")
    _git(root, "add", "app.py", "new.py")
    _git(root, "commit", "-qm", "candidate")
    candidate_commit = _git(root, "rev-parse", "HEAD").decode().strip()
    candidate_listing = _git(root, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    candidate_tree = hashlib.sha256(candidate_listing).hexdigest()

    steps = (
        EvolutionPromotionRollbackStep(
            order=1,
            path="app.py",
            operation=EvolutionPromotionRollbackOperation.RESTORE_BASELINE_BLOB,
            baseline_sha256=hashlib.sha256(baseline_bytes).hexdigest(),
            candidate_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
        ),
        EvolutionPromotionRollbackStep(
            order=2,
            path="new.py",
            operation=EvolutionPromotionRollbackOperation.REMOVE_CREATED_FILE,
            baseline_sha256=None,
            candidate_sha256=hashlib.sha256(b"created = True\n").hexdigest(),
        ),
    )
    rollback_core = {
        "policy_version": EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY,
        "baseline_commit": baseline_commit,
        "baseline_tree_sha256": baseline_tree,
        "source_snapshot_id": "evs_" + "1" * 24,
        "source_snapshot_sha256": "2" * 64,
        "steps": [step.model_dump(mode="json") for step in steps],
        "data_restore_required": data_restore_required,
        "rollback_review_required": True,
        "rollback_authority": False,
        "rollback_executed": False,
    }
    rollback = EvolutionPromotionRollbackPlan.model_validate(
        {**rollback_core, "plan_sha256": _digest(rollback_core)}
    )
    db_path = root / ".naumi" / "evolution.db"
    db_path.parent.mkdir(parents=True)
    plan_core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_ROLLOUT_PLAN_POLICY,
        "workspace_root": str(root),
        "contract_id": "evrevalruntime_" + "3" * 24,
        "contract_sha256": "4" * 64,
        "decision_id": "evreapprovaldecision_" + "5" * 24,
        "decision_sha256": "6" * 64,
        "decision_source_set_sha256": "7" * 64,
        "requirement_id": "evreapprovalreq_" + "8" * 24,
        "requirement_sha256": "9" * 64,
        "promotion_input_id": "evrevalpromoin_" + "a" * 24,
        "promotion_input_sha256": "b" * 64,
        "final_evaluation_id": "evrevalfinal_" + "c" * 24,
        "final_evaluation_sha256": "d" * 64,
        "candidate_id": "evc_" + "e" * 24,
        "candidate_revision": 1,
        "risk_level": "medium",
        "required_platforms": ["macos"],
        "target_branch": "main",
        "target_head": candidate_commit,
        "target_tree_sha256": candidate_tree,
        "patch_manifest_sha256": "f" * 64,
        "migration_assessment_sha256": "0" * 64,
        "rollback_plan_sha256": rollback.plan_sha256,
        "data_backup_required": data_restore_required,
        "stages": [
            item.model_dump(mode="json")
            for item in _stages("medium", data_backup_required=data_restore_required)
        ],
        "current_decision_at_issue": True,
        "immutable_plan": True,
        "monitor_required": True,
        "rollback_required": True,
        "rollout_plan_authority": True,
        "stage_entry_authority": False,
        "execution_authority": False,
        "git_write_executed": False,
        "merge_executed": False,
        "push_executed": False,
        "publish_executed": False,
        "planned_at": T0.isoformat(),
    }
    plan_digest = _digest(plan_core)
    plan = EvolutionRevalidationRolloutPlan.model_validate(
        {
            **plan_core,
            "plan_id": f"evrerolloutplan_{plan_digest[:24]}",
            "plan_sha256": plan_digest,
        }
    )
    if with_outcome_lineage:
        fresh, _authority, _experiment_store = await _persist_outcome_lineage(
            root,
            SimpleNamespace(rollback_plan=rollback),
            plan,
            db_path,
        )
        plan_core.update(
            promotion_input_id=fresh.input_id,
            promotion_input_sha256=fresh.input_sha256,
        )
        plan_digest = _digest(plan_core)
        plan = EvolutionRevalidationRolloutPlan.model_validate(
            {
                **plan_core,
                "plan_id": f"evrerolloutplan_{plan_digest[:24]}",
                "plan_sha256": plan_digest,
            }
        )
    control_store = EvolutionRevalidationRolloutControlStore(
        db_path, control_plane_key_provider=lambda: b"r" * 32
    )
    control_service = EvolutionRevalidationRolloutControlService(
        workspace_root=root,
        store=control_store,
        control_plane_key_provider=lambda: b"r" * 32,
    )
    pause = await control_service.pause(
        reason_code="runtime_guardrail_breach",
        actor=EvolutionRevalidationRolloutControlActor.MONITOR,
        changed_at=(T0 + timedelta(minutes=1)).isoformat(),
    )
    request_core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_ROLLBACK_REQUEST_POLICY,
        "workspace_root": str(root),
        "observation_id": "evreruntimeobs_" + "1" * 24,
        "observation_sha256": "2" * 64,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "baseline_id": "evrerolloutbaseline_" + "3" * 24,
        "baseline_sha256": "4" * 64,
        "entry_receipt_id": "evrerolloutentry_" + "5" * 24,
        "entry_receipt_sha256": "6" * 64,
        "promotion_input_id": plan.promotion_input_id,
        "promotion_input_sha256": plan.promotion_input_sha256,
        "rollback_plan": rollback.model_dump(mode="json"),
        "rollback_plan_sha256": rollback.plan_sha256,
        "pause_event_id": pause.event_id,
        "pause_event_sha256": pause.event_sha256,
        "pause_actor": pause.actor.value,
        "pause_reason_code": pause.reason_code,
        "breach_reasons": ["error_rate"],
        "data_restore_required": data_restore_required,
        "automatic_pause_satisfied": True,
        "monitor_pause_created": True,
        "rollback_request_authority": True,
        "rollback_execution_authority": False,
        "workspace_write_executed": False,
        "git_write_executed": False,
        "rollback_executed": False,
        "promotion_authority": False,
        "requested_at": (T0 + timedelta(minutes=2)).isoformat(),
    }
    request_digest = _digest(request_core)
    request = EvolutionRevalidationRollbackRequest.model_validate(
        {
            **request_core,
            "request_id": f"evrerollbackreq_{request_digest[:24]}",
            "request_sha256": request_digest,
        }
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE evolution_revalidation_runtime_observations ("
            "observation_id TEXT PRIMARY KEY, observation_sha256 TEXT NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS evolution_revalidation_promotion_inputs ("
            "input_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL UNIQUE, "
            "contract_id TEXT NOT NULL UNIQUE, input_json TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE evolution_revalidation_rollout_plans ("
            "plan_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL UNIQUE, "
            "decision_id TEXT NOT NULL UNIQUE, contract_id TEXT NOT NULL, "
            "plan_json TEXT NOT NULL, planned_at TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT INTO evolution_revalidation_runtime_observations VALUES (?, ?)",
            (request.observation_id, request.observation_sha256),
        )
        await db.execute(
            "INSERT OR IGNORE INTO evolution_revalidation_promotion_inputs "
            "VALUES (?, ?, ?, ?, ?)",
            (
                request.promotion_input_id,
                request.promotion_input_sha256,
                plan.contract_id,
                "{}",
                T0.isoformat(),
            ),
        )
        await db.execute(
            "INSERT INTO evolution_revalidation_rollout_plans VALUES (?, ?, ?, ?, ?, ?)",
            (
                plan.plan_id,
                plan.plan_sha256,
                plan.decision_id,
                plan.contract_id,
                plan.model_dump_json(),
                plan.planned_at,
            ),
        )
        await db.commit()
    request_store = EvolutionRevalidationRollbackRequestStore(db_path)
    await request_store.record(request)
    source_store = EvolutionRevalidationRollbackSourceStore(
        db_path,
        storage_dir=root / ".naumi" / "evolution" / "rollback-sources",
    )
    source_service = EvolutionRevalidationRollbackSourceService(
        workspace_root=root,
        request_store=request_store,
        control_store=control_store,
        store=source_store,
        now=lambda: (T0 + timedelta(minutes=3)).isoformat(),
    )
    source = await source_service.freeze(request_id=request.request_id)

    release_store = ReleaseSlotStore(tmp_path / "installed")
    baseline_slot = release_store.install(
        _bundle(
            tmp_path,
            version="1.0.0",
            output_name="baseline-release",
            source_commit=baseline_commit,
            source_tree_sha256=baseline_tree,
        )
    )
    release_store.verify_bootable(baseline_slot.slot_id)
    release_store.activate(
        baseline_slot.slot_id,
        activated_at=(T0 + timedelta(minutes=4)).isoformat(),
    )
    candidate_slot = release_store.install(
        _bundle(
            tmp_path,
            version="1.1.0",
            output_name="candidate-release",
            source_commit=candidate_commit,
            source_tree_sha256=candidate_tree,
        )
    )
    release_store.verify_bootable(candidate_slot.slot_id)
    candidate_pointer = release_store.activate(
        candidate_slot.slot_id,
        activated_at=(T0 + timedelta(minutes=5)).isoformat(),
    )

    def service(store=None):
        return EvolutionRevalidationRollbackExecutionService(
            workspace_root=root,
            request_store=request_store,
            source_store=source_store,
            plan_store=EvolutionRevalidationRolloutPlanStore(db_path),
            control_store=control_store,
            release_slot_store=release_store,
            store=store or EvolutionRevalidationRollbackExecutionStore(db_path),
            now=lambda: (T0 + timedelta(minutes=6)).isoformat(),
        )

    return root, request, source, release_store, candidate_pointer, service, db_path


async def _persist_outcome_lineage(root: Path, request, plan, db_path: Path):
    source = {
        "session_id": "session-rollback-outcome",
        "mission_id": "mission-rollback-outcome",
        "task_id": "task-rollback-outcome",
        "workbench_proposal_id": "proposal-rollback-outcome",
        "proposal_id": "evp_" + "1" * 24,
        "candidate_id": plan.candidate_id,
        "candidate_revision": plan.candidate_revision,
        "candidate_sha256": "c" * 64,
        "proposal_kind": "code",
        "generator_version": "evolution-proposal-v1",
        "governance_policy_version": "proposal-governance-v1",
        "reviewer": "Human",
        "approved_at": T0.isoformat(),
    }
    contract_core = {
        "schema_version": 1,
        "policy_version": "evolution-experiment-contract-v1",
        "source": source,
        "baseline": {
            "commit": request.rollback_plan.baseline_commit,
            "workspace_dirty_at_issue": False,
        },
        "scope": {
            "policy_version": "evolution-experiment-scope-v1",
            "impact_scope": "files:app.py,new.py",
            "allowed_files": ["app.py", "new.py"],
        },
        "budget": {
            "policy_version": "evolution-experiment-budget-v1",
            "max_changed_files": 2,
            "max_changed_lines": 10,
            "max_tool_calls": 20,
            "max_duration_seconds": 300,
            "max_attempts": 1,
        },
        "allowed_tools": ["file_read", "glob", "grep", "file_edit", "file_write"],
        "allowed_checks": [
            {
                "metric_name": "rollback_error_rate",
                "direction": "decrease",
                "target": 0.0,
                "verifier": "harness_replay",
                "procedure": "回放同一失败场景并比较错误率。",
            }
        ],
        "seed": 1,
        "network_access": False,
        "dependency_installation": False,
        "requires_worktree_lease": True,
        "requires_source_snapshot": True,
        "requires_static_guard": True,
        "execution_ready": False,
        "state": "contract",
    }
    contract_sha = _manifest_digest(contract_core)
    contract = EvolutionExperimentContract.model_validate(
        {
            **contract_core,
            "contract_id": f"evx_{contract_sha[:24]}",
            "manifest_sha256": contract_sha,
        }
    )
    experiment_store = EvolutionExperimentContractStore(db_path)
    authority = await experiment_store.record(workspace_root=root, contract=contract)

    baseline_bytes = b"print('baseline')\n"
    candidate_bytes = b"print('candidate')\n"
    created_bytes = b"created = True\n"
    facts = []
    for values in (
        {
            "path": "app.py",
            "operation": "modify",
            "before_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
            "after_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            "unified_diff_sha256": "1" * 64,
            "added_lines": 1,
            "deleted_lines": 1,
            "api_change": "unchanged",
        },
        {
            "path": "new.py",
            "operation": "create",
            "before_sha256": None,
            "after_sha256": hashlib.sha256(created_bytes).hexdigest(),
            "unified_diff_sha256": "2" * 64,
            "added_lines": 1,
            "deleted_lines": 0,
            "api_change": "not_applicable",
        },
    ):
        facts.append(SimpleNamespace(**values, fact_sha256=_sha256_payload(values)))
    mutation = SimpleNamespace(
        mutation_receipt_id="evmr_" + "6" * 24,
        receipt_sha256="6" * 64,
        files=tuple(facts),
        files_sha256=_sha256_payload(
            [{**vars(item), "fact_sha256": item.fact_sha256} for item in facts]
        ),
        total_added_lines=2,
        total_deleted_lines=1,
    )
    patch = _patch_manifest(mutation)
    baseline = _baseline(
        False,
        SimpleNamespace(
            baseline_commit=request.rollback_plan.baseline_commit,
            source_snapshot_id=request.rollback_plan.source_snapshot_id,
            source_snapshot_sha256=request.rollback_plan.source_snapshot_sha256,
            baseline_tree_sha256=request.rollback_plan.baseline_tree_sha256,
            profile_sha256="9" * 64,
            experiment_config_sha256="a" * 64,
            toolset_sha256="b" * 64,
        ),
    )
    migration = _migration_assessment(patch.files)
    rollback = _rollback_plan(patch.files, baseline, migration)
    assert rollback == request.rollback_plan

    memory = _accepted_reflection(root)
    prior_payload = _package(root, memory).model_dump(mode="json")
    prior_payload.update(
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
        candidate_sha256=source["candidate_sha256"],
        risk_level=plan.risk_level,
        experiment_contract_id=contract.contract_id,
        experiment_contract_sha256=contract.manifest_sha256,
        mutation_receipt_id=mutation.mutation_receipt_id,
        mutation_receipt_sha256=mutation.receipt_sha256,
        required_platforms=list(plan.required_platforms),
        patch=patch.model_dump(mode="json"),
        baseline=baseline.model_dump(mode="json"),
        migration=migration.model_dump(mode="json"),
        rollback=rollback.model_dump(mode="json"),
    )
    for ref in prior_payload["evidence_refs"]:
        if ref["kind"] == EvolutionPromotionEvidenceKind.EXPERIMENT_CONTRACT.value:
            ref["authority_id"] = contract.contract_id
            ref["authority_sha256"] = contract.manifest_sha256
        elif ref["kind"] == EvolutionPromotionEvidenceKind.MUTATION_RECEIPT.value:
            ref["authority_id"] = mutation.mutation_receipt_id
            ref["authority_sha256"] = mutation.receipt_sha256
    prior_core = {
        key: value
        for key, value in prior_payload.items()
        if key not in {"input_id", "input_sha256"}
    }
    prior_sha = _sha256_payload(prior_core)
    prior = EvolutionPromotionPackageInput.model_validate(
        {
            **prior_core,
            "input_id": f"evpromoin_{prior_sha[:24]}",
            "input_sha256": prior_sha,
        }
    )
    _persist_prior_dependency(db_path, prior)

    fresh_core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_PROMOTION_INPUT_POLICY,
        "workspace_root": str(root),
        "candidate_id": plan.candidate_id,
        "candidate_revision": plan.candidate_revision,
        "risk_level": plan.risk_level,
        "contract_id": plan.contract_id,
        "contract_sha256": plan.contract_sha256,
        "final_evaluation_id": plan.final_evaluation_id,
        "final_evaluation_sha256": plan.final_evaluation_sha256,
        "reapproval_authority_id": "evreapproval_" + "3" * 24,
        "reapproval_authority_sha256": "3" * 64,
        "prior_input_id": prior.input_id,
        "prior_input_sha256": prior.input_sha256,
        "prior_input": prior.model_dump(mode="json"),
        "required_platforms": list(plan.required_platforms),
        "prior_final_invalidated": True,
        "prior_decision_reusable": False,
        "prior_approval_reusable": False,
        "prior_signature_reusable": False,
        "patch_and_rollback_carried_forward": True,
        "source_current_at_issue": True,
        "input_complete": True,
        "approval_requirement_ready": True,
        "approval_decided": False,
        "promotion_authority": False,
        "contains_source_code": False,
        "contains_freeform_narrative": False,
        "llm_generated": False,
        "created_at": T0.isoformat(),
    }
    fresh_sha = _fresh_sha256(fresh_core)
    fresh = EvolutionRevalidationPromotionInput.model_validate(
        {
            **fresh_core,
            "input_id": f"evrevalpromoin_{fresh_sha[:24]}",
            "input_sha256": fresh_sha,
        }
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS evolution_revalidation_promotion_inputs ("
            "input_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL UNIQUE, "
            "contract_id TEXT NOT NULL UNIQUE, input_json TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT INTO evolution_revalidation_promotion_inputs "
            "(input_id, input_sha256, contract_id, input_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                fresh.input_id,
                fresh.input_sha256,
                fresh.contract_id,
                fresh.model_dump_json(),
                fresh.created_at,
            ),
        )
        await db.commit()
    return fresh, authority, experiment_store


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
async def test_fenced_slot_rollback_recovers_after_receipt_crash_and_converges(
    tmp_path: Path,
) -> None:
    root, request, source, release_store, candidate_pointer, service, db_path = await _scenario(
        tmp_path
    )
    flaky = _FailOnceStore(db_path)

    with pytest.raises(OSError, match="simulated crash"):
        await service(flaky).execute(request_id=request.request_id)

    switched = release_store.active()
    assert switched is not None
    assert switched.generation == candidate_pointer.generation + 1
    assert switched.rollback_authority is not None
    assert switched.rollback_authority.authority_id == source.source_id

    views = await asyncio.gather(
        *(service().execute(request_id=request.request_id) for _ in range(8))
    )
    view = views[0]

    assert all(item == view for item in views)
    assert view.rollback_fact_authority
    assert view.active_baseline_authority
    assert view.receipt.rollback_pointer == switched
    assert view.receipt.candidate_slot.version == "1.1.0"
    assert view.receipt.baseline_slot.version == "1.0.0"
    assert not view.receipt.process_started
    assert not view.receipt.workspace_write_executed
    assert not view.receipt.git_write_executed
    assert not view.outcome_recorded
    assert not view.promotion_authority

    assert (root / "app.py").read_bytes() == b"print('candidate')\n"
    assert (root / "new.py").is_file()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_rollback_sources "
            "SET source_json = replace(source_json, ?, ?) WHERE source_id = ?",
            (source.source_sha256, "0" * 64, source.source_id),
        )
        await db.commit()
    stale = await service().inspect(request_id=request.request_id)
    assert not stale.durable_dependencies_valid
    assert not stale.rollback_fact_authority
    assert not stale.active_baseline_authority


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
async def test_data_restore_requirement_blocks_pointer_switch(tmp_path: Path) -> None:
    _root, request, _source, release_store, candidate_pointer, service, _db = await _scenario(
        tmp_path, data_restore_required=True
    )

    with pytest.raises(EvolutionRevalidationRollbackExecutionError) as blocked:
        await service().execute(request_id=request.request_id)

    assert blocked.value.code == "rollback_execution_data_restore_unavailable"
    assert release_store.active() == candidate_pointer


def test_rollback_tool_permission_is_one_confirmation_and_bypass_is_direct() -> None:
    tool = EvolutionRevalidationRollbackExecutionTool(SimpleNamespace())
    arguments = {"request_id": "evrerollbackreq_" + "1" * 24}

    guarded = PermissionChecker(PermissionMode.MODERATE).check(
        tool.name,
        arguments,
        tool=tool,
    )
    bypass = PermissionChecker(PermissionMode.BYPASS).check(
        tool.name,
        arguments,
        tool=tool,
    )
    blocked = PermissionChecker(PermissionMode.LOCKDOWN).check(
        tool.name,
        arguments,
        tool=tool,
    )

    assert guarded.allowed
    assert guarded.outcome is PermissionOutcome.CONFIRM
    assert guarded.requires_confirmation
    assert not guarded.requires_double_confirm
    assert not guarded.allow_session_grant
    assert guarded.risk_level is PermissionRiskLevel.HIGH
    assert guarded.tool_family == "evolution_release_rollback"
    assert bypass.allowed
    assert bypass.outcome is PermissionOutcome.ALLOW
    assert not bypass.requires_confirmation
    assert not blocked.allowed
    assert blocked.code is PermissionReasonCode.MODE_BLOCKED

    outcome_tool = EvolutionRevalidationRollbackOutcomeTool(SimpleNamespace())
    outcome_guarded = PermissionChecker(PermissionMode.MODERATE).check(
        outcome_tool.name,
        arguments,
        tool=outcome_tool,
    )
    outcome_bypass = PermissionChecker(PermissionMode.BYPASS).check(
        outcome_tool.name,
        arguments,
        tool=outcome_tool,
    )
    assert outcome_guarded.allowed
    assert outcome_guarded.outcome is PermissionOutcome.ALLOW
    assert not outcome_guarded.requires_confirmation
    assert outcome_guarded.risk_level is PermissionRiskLevel.MEDIUM
    assert outcome_bypass.allowed
    assert outcome_bypass.outcome is PermissionOutcome.ALLOW

    verification_tool = EvolutionPostRollbackRuntimeVerificationTool(
        SimpleNamespace()
    )
    verification_guarded = PermissionChecker(PermissionMode.MODERATE).check(
        verification_tool.name,
        arguments,
        tool=verification_tool,
    )
    verification_bypass = PermissionChecker(PermissionMode.BYPASS).check(
        verification_tool.name,
        arguments,
        tool=verification_tool,
    )
    assert verification_guarded.allowed
    assert verification_guarded.outcome is PermissionOutcome.ALLOW
    assert not verification_guarded.requires_confirmation
    assert verification_bypass.allowed
    assert verification_bypass.outcome is PermissionOutcome.ALLOW


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
async def test_rollback_outcome_binds_real_receipt_to_proposal_and_fails_closed(
    tmp_path: Path,
) -> None:
    root, request, _source, _release, _pointer, execution_factory, db_path = (
        await _scenario(tmp_path, with_outcome_lineage=True)
    )
    execution_service = execution_factory()
    execution_view = await execution_service.execute(request_id=request.request_id)
    outcome_clock = iter(
        (T0 + timedelta(minutes=7, microseconds=index)).isoformat()
        for index in range(16)
    )
    outcome_service = EvolutionRevalidationRollbackOutcomeService(
        workspace_root=root,
        execution_service=execution_service,
        request_store=EvolutionRevalidationRollbackRequestStore(db_path),
        plan_store=EvolutionRevalidationRolloutPlanStore(db_path),
        promotion_input_store=EvolutionRevalidationPromotionInputStore(db_path),
        experiment_store=EvolutionExperimentContractStore(db_path),
        store=EvolutionRevalidationRollbackOutcomeStore(db_path),
        now=lambda: next(outcome_clock),
    )

    views = await asyncio.gather(
        *(outcome_service.record(request_id=request.request_id) for _ in range(8))
    )
    view = views[0]
    assert all(item == view for item in views)
    assert view.outcome_authority
    assert view.rollback_fact_authority
    assert view.proposal_binding_valid
    assert view.active_baseline_authority
    assert view.outcome.status == "rolled_back"
    assert view.outcome.rollback_receipt_id == execution_view.receipt.receipt_id
    assert view.outcome.workbench_proposal_id == "proposal-rollback-outcome"
    assert view.outcome.experiment_contract_id.startswith("evx_")
    assert view.outcome.outcome_recorded
    assert not view.outcome.promoted
    assert not view.outcome.superseded
    assert not view.outcome.long_term_metrics_recorded
    assert not view.outcome.learning_authority
    assert not view.promotion_authority

    candidate_store = EvolutionCandidateStore(tmp_path / "opportunity.db")
    opportunity_service = EvolutionOutcomeOpportunityService(
        workspace_root=root,
        outcome_service=outcome_service,
        candidate_store=candidate_store,
    )
    opportunities = await asyncio.gather(*(
        opportunity_service.discover(outcome_id=view.outcome.outcome_id)
        for _ in range(8)
    ))
    opportunity = opportunities[0]
    assert all(item == opportunity for item in opportunities)
    assert opportunity.candidate_revision == 1
    assert opportunity.occurrence_count == 1
    assert opportunity.candidate_id != view.outcome.candidate_id
    assert await EvolutionRevalidationRollbackOutcomeStore(db_path).get(
        view.outcome.outcome_id
    ) == view.outcome
    candidate = await candidate_store.get_candidate(root, opportunity.candidate_id)
    assert candidate is not None
    assert candidate.draft.source_kinds == ("rollback_outcome",)
    assert await opportunity_service.validate_candidate_sources(candidate.draft)

    projection_service = EvolutionProposalOutcomeProjectionService(
        rollback_outcome_store=EvolutionRevalidationRollbackOutcomeStore(db_path),
        rollback_outcome_service=outcome_service,
    )
    projected = await projection_service.project_session(
        view.outcome.workbench_session_id
    )
    proposal_outcome = projected[view.outcome.workbench_proposal_id]
    assert proposal_outcome.status == "rolled_back"
    assert proposal_outcome.governance_state_unchanged
    assert proposal_outcome.authority_valid
    assert proposal_outcome.active_baseline
    assert not proposal_outcome.contract_issue_allowed
    assert not proposal_outcome.before_after_recorded
    assert not proposal_outcome.long_term_metrics_recorded
    assert not proposal_outcome.promoted
    assert not proposal_outcome.learning_authority
    assert not proposal_outcome.promotion_authority

    class _AmbiguousOutcomeStore:
        async def list_by_session(self, session_id: str):
            return (view.outcome, view.outcome)

    ambiguous_service = EvolutionProposalOutcomeProjectionService(
        rollback_outcome_store=_AmbiguousOutcomeStore(),  # type: ignore[arg-type]
        rollback_outcome_service=outcome_service,
    )
    with pytest.raises(EvolutionProposalOutcomeProjectionError) as ambiguous:
        await ambiguous_service.project_session(view.outcome.workbench_session_id)
    assert ambiguous.value.code == "proposal_outcome_ambiguous"
    with pytest.raises(EvolutionProposalOutcomeProjectionError) as invalid_session:
        await projection_service.project_session("forged\x00session")
    assert invalid_session.value.code == "proposal_outcome_session_id_invalid"

    tool = EvolutionRevalidationRollbackOutcomeTool(
        SimpleNamespace(evolution_revalidation_rollback_outcome_service=outcome_service)
    )
    rendered = await tool.execute(request.request_id)
    assert view.outcome.outcome_id in rendered
    assert "长期指标：尚未记录" in rendered
    assert "不授予 promotion authority" in rendered

    registry = ToolRegistry()
    registry.register(tool)

    class _OutcomeSlashEngine:
        def __init__(self) -> None:
            self.tool_registry = registry
            self.calls: list[tuple[ToolCall, str | None]] = []

        async def execute_tool(
            self, call: ToolCall, *, agent_name: str | None = None
        ) -> ToolResult:
            self.calls.append((call, agent_name))
            registered = self.tool_registry.get(call.name)
            assert registered is not None
            arguments = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**arguments),
            )

    slash_engine = _OutcomeSlashEngine()
    slash_rendered = await execute_slash_command(
        slash_engine,
        f"/evolution revalidation-rollback-outcome {request.request_id}",
    )
    assert view.outcome.outcome_id in slash_rendered
    assert "不授予 promotion authority" in slash_rendered
    assert len(slash_engine.calls) == 1
    assert slash_engine.calls[0][0].name == "evolution_revalidation_rollback_outcome"
    assert slash_engine.calls[0][1] == "cli"

    opportunity_tool = EvolutionOutcomeOpportunityTool(
        SimpleNamespace(evolution_outcome_opportunity_service=opportunity_service)
    )
    registry.register(opportunity_tool)
    opportunity_rendered = await execute_slash_command(
        slash_engine,
        f"/evolution discover-outcome {view.outcome.outcome_id}",
    )
    assert opportunity.candidate_id in opportunity_rendered
    assert "未授予实验或推广权限" in opportunity_rendered
    assert slash_engine.calls[-1][0].name == (
        "evolution_discover_outcome_opportunity"
    )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE evolution_experiment_contracts SET authority_sha256 = ? "
            "WHERE contract_id = ?",
            ("0" * 64, view.outcome.experiment_contract_id),
        )
        await db.commit()
    stale = await outcome_service.inspect(request_id=request.request_id)
    assert not stale.durable_dependencies_valid
    assert not stale.proposal_binding_valid
    assert not stale.outcome_authority
    assert not stale.long_term_metrics_authority
    assert not await opportunity_service.validate_candidate_sources(candidate.draft)
    with pytest.raises(EvolutionOutcomeOpportunityError) as stale_opportunity:
        await opportunity_service.discover(outcome_id=view.outcome.outcome_id)
    assert stale_opportunity.value.code == "outcome_opportunity_source_stale"
    stale_projection = await projection_service.project_session(
        view.outcome.workbench_session_id
    )
    assert not stale_projection[view.outcome.workbench_proposal_id].authority_valid
    assert not stale_projection[view.outcome.workbench_proposal_id].active_baseline
    with pytest.raises(EvolutionRevalidationRollbackOutcomeError) as blocked:
        await outcome_service.record(request_id=request.request_id)
    assert blocked.value.code == "rollback_outcome_stale"


@pytest.mark.asyncio
async def test_rollback_outcome_rejects_invalid_request_id(tmp_path: Path) -> None:
    service = EvolutionRevalidationRollbackOutcomeService(
        workspace_root=tmp_path,
        execution_service=SimpleNamespace(),
        request_store=SimpleNamespace(),
        plan_store=SimpleNamespace(),
        promotion_input_store=SimpleNamespace(),
        experiment_store=SimpleNamespace(),
        store=EvolutionRevalidationRollbackOutcomeStore(tmp_path / "outcome.db"),
    )
    with pytest.raises(EvolutionRevalidationRollbackOutcomeError) as invalid:
        await service.record(request_id="../forged")
    assert invalid.value.code == "rollback_outcome_request_id_invalid"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
async def test_post_rollback_runtime_verification_runs_fresh_probes_and_converges(
    tmp_path: Path,
) -> None:
    root, request, _source, release_store, _pointer, execution_factory, db_path = (
        await _scenario(tmp_path, with_outcome_lineage=True)
    )
    execution_service = execution_factory()
    execution_view = await execution_service.execute(request_id=request.request_id)
    outcome_service = EvolutionRevalidationRollbackOutcomeService(
        workspace_root=root,
        execution_service=execution_service,
        request_store=EvolutionRevalidationRollbackRequestStore(db_path),
        plan_store=EvolutionRevalidationRolloutPlanStore(db_path),
        promotion_input_store=EvolutionRevalidationPromotionInputStore(db_path),
        experiment_store=EvolutionExperimentContractStore(db_path),
        store=EvolutionRevalidationRollbackOutcomeStore(db_path),
        now=lambda: (T0 + timedelta(minutes=7)).isoformat(),
    )
    outcome_view = await outcome_service.record(request_id=request.request_id)
    verification_store = EvolutionPostRollbackRuntimeVerificationStore(db_path)

    def verification_service(index: int):
        times = iter(
            (T0 + timedelta(minutes=8, microseconds=index * 10 + offset)).isoformat()
            for offset in range(2)
        )
        return EvolutionPostRollbackRuntimeVerificationService(
            workspace_root=root,
            outcome_service=outcome_service,
            rollback_service=execution_service,
            release_slot_store=release_store,
            store=verification_store,
            now=lambda: next(times),
        )

    views = await asyncio.gather(*(
        verification_service(index).record(request_id=request.request_id)
        for index in range(8)
    ))
    view = views[0]
    assert all(item == view for item in views)
    item = view.verification
    assert view.verification_authority
    assert view.active_baseline_authority
    assert item.outcome_id == outcome_view.outcome.outcome_id
    assert item.rollback_receipt_id == execution_view.receipt.receipt_id
    assert item.baseline_slot_id == execution_view.receipt.baseline_slot.slot_id
    assert item.fresh_boot_receipt.receipt_id != (
        execution_view.receipt.rollback_boot_receipt.receipt_id
    )
    assert item.fresh_launch_resolution.resolution_id != (
        execution_view.receipt.launch_resolution.resolution_id
    )
    assert item.fresh_boot_receipt.binary_sha256 == (
        item.fresh_launch_resolution.binary_sha256
    )
    assert item.post_rollback_evaluation_recorded
    assert not item.behavioral_evaluation_recorded
    assert not item.long_term_metrics_recorded
    assert not item.learning_authority
    assert not item.promotion_authority
    projection_service = EvolutionProposalOutcomeProjectionService(
        rollback_outcome_store=EvolutionRevalidationRollbackOutcomeStore(db_path),
        rollback_outcome_service=outcome_service,
        post_rollback_store=verification_store,
        post_rollback_service=verification_service(20),
    )
    projected = await projection_service.project_session(
        outcome_view.outcome.workbench_session_id
    )
    projection = projected[outcome_view.outcome.workbench_proposal_id]
    assert projection.post_rollback_verification == item
    assert projection.post_rollback_verification_recorded
    assert projection.post_rollback_evaluation_recorded
    assert not projection.post_rollback_behavioral_evaluation_recorded
    assert not projection.long_term_metrics_recorded
    assert not projection.learning_authority
    rendered = render_post_rollback_runtime_verification(view)
    assert "重新执行受控启动探针" in rendered
    assert "行为级 Eval：尚未记录" in rendered

    tool = EvolutionPostRollbackRuntimeVerificationTool(
        SimpleNamespace(
            evolution_post_rollback_runtime_verification_service=(
                verification_service(8)
            )
        )
    )
    assert await tool.execute(request.request_id) == rendered
    registry = ToolRegistry()
    registry.register(tool)

    class _VerificationSlashEngine:
        def __init__(self):
            self.tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            arguments = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**arguments),
            )

    slash_rendered = await execute_slash_command(
        _VerificationSlashEngine(),
        f"/evolution outcome-verify-runtime {request.request_id}",
    )
    assert item.verification_id in slash_rendered
    assert item.fresh_boot_receipt.receipt_id in slash_rendered
    assert "行为级 Eval：尚未记录" in slash_rendered

    with pytest.raises(EvolutionPostRollbackRuntimeVerificationError) as invalid:
        await verification_service(9).record(request_id="../forged")
    assert invalid.value.code == "post_rollback_request_id_invalid"

    with release_store._connect() as db:  # noqa: SLF001 - tamper fixture
        db.execute(
            "UPDATE release_launch_resolutions SET resolution_json = ? "
            "WHERE resolution_id = ?",
            ("{}", item.fresh_launch_resolution.resolution_id),
        )
        db.commit()
    stale = await verification_service(10).inspect(verification=item)
    assert not stale.fresh_launch_authority
    assert not stale.verification_authority
    assert not stale.active_baseline_authority
    assert not stale.behavioral_evaluation_authority
    assert not stale.promotion_authority
    with pytest.raises(EvolutionProposalOutcomeProjectionError) as stale_projection:
        await projection_service.project_session(
            outcome_view.outcome.workbench_session_id
        )
    assert stale_projection.value.code == "proposal_outcome_post_rollback_stale"

    for surface in ("new_ui", "tui"):
        command = next(
            item
            for item in build_terminal_command_index(surface)
            if item.command == "/evolution"
        )
        assert "revalidation-rollback-execute" in command.arguments.syntax
        assert "revalidation-rollback-outcome" in command.arguments.syntax
        assert command.permission_risk == "tool_execution"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
async def test_rollback_agent_tool_and_shared_slash_return_same_durable_receipt(
    tmp_path: Path,
) -> None:
    root, request, _source, _release_store, _pointer, service_factory, _db_path = (
        await _scenario(tmp_path)
    )
    service = service_factory()
    tool = EvolutionRevalidationRollbackExecutionTool(
        SimpleNamespace(
            workspace_root=root,
            evolution_revalidation_rollback_execution_service=service,
        )
    )
    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        def __init__(self) -> None:
            self.tool_registry = registry
            self.calls: list[tuple[ToolCall, str | None]] = []

        async def execute_tool(
            self,
            call: ToolCall,
            *,
            agent_name: str | None = None,
        ) -> ToolResult:
            self.calls.append((call, agent_name))
            registered = self.tool_registry.get(call.name)
            assert registered is not None
            arguments = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**arguments),
            )

    agent_result = await tool.execute(request.request_id)
    engine = _SlashEngine()
    slash_result = await execute_slash_command(
        engine,
        f"/evolution revalidation-rollback-execute {request.request_id}",
    )

    receipt = await service.store.get_by_request(request.request_id)
    assert receipt is not None
    assert receipt.receipt_id in agent_result
    assert receipt.receipt_id in slash_result
    assert "Candidate → Baseline" in agent_result
    assert "本回执不授予 promotion 权限" in slash_result
    assert len(engine.calls) == 1
    assert engine.calls[0][0].name == "evolution_revalidation_rollback_execute"
    assert engine.calls[0][1] == "cli"
