from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from naumi_agent.evolution.promotion_package_inputs import (
    EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY,
    EvolutionPromotionRollbackOperation,
    EvolutionPromotionRollbackPlan,
    EvolutionPromotionRollbackStep,
)
from naumi_agent.evolution.revalidation_rollback_executions import (
    EvolutionRevalidationRollbackExecutionError,
    EvolutionRevalidationRollbackExecutionService,
    EvolutionRevalidationRollbackExecutionStore,
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
from naumi_agent.release.slots import ReleaseSlotStore
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


async def _scenario(tmp_path: Path, *, data_restore_required: bool = False):
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
    db_path = root / ".naumi" / "evolution.db"
    db_path.parent.mkdir(parents=True)
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
            "CREATE TABLE evolution_revalidation_promotion_inputs ("
            "input_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL)"
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
            "INSERT INTO evolution_revalidation_promotion_inputs VALUES (?, ?)",
            (request.promotion_input_id, request.promotion_input_sha256),
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
