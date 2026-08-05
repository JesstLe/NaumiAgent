from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from naumi_agent.evolution.promotion_package_inputs import (
    EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY,
    EvolutionPromotionRollbackOperation,
    EvolutionPromotionRollbackPlan,
    EvolutionPromotionRollbackStep,
)
from naumi_agent.evolution.revalidation_rollback_requests import (
    EVOLUTION_REVALIDATION_ROLLBACK_REQUEST_POLICY,
    EvolutionRevalidationRollbackRequest,
    EvolutionRevalidationRollbackRequestStore,
)
from naumi_agent.evolution.revalidation_rollback_sources import (
    EvolutionRevalidationRollbackSourceError,
    EvolutionRevalidationRollbackSourceService,
    EvolutionRevalidationRollbackSourceStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
    EvolutionRevalidationRolloutControlStore,
)

T0 = datetime.fromisoformat("2026-07-19T01:00:00+00:00")


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    ).stdout


async def _scenario(root: Path, *, baseline: bytes = b"print('baseline')\n"):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "rollback@example.test")
    _git(root, "config", "user.name", "Rollback Test")
    candidate = b"print('candidate')\n"
    (root / "app.py").write_bytes(baseline)
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "baseline")
    commit = _git(root, "rev-parse", "HEAD").decode().strip()
    tree_listing = _git(root, "ls-tree", "-r", "-z", "--full-tree", commit)
    (root / "app.py").write_bytes(candidate)
    (root / "new.py").write_text("created = True\n", encoding="utf-8")

    db_path = root / ".naumi" / "evolution.db"
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
        changed_at=T0.isoformat(),
    )
    steps = (
        EvolutionPromotionRollbackStep(
            order=1,
            path="app.py",
            operation=EvolutionPromotionRollbackOperation.RESTORE_BASELINE_BLOB,
            baseline_sha256=hashlib.sha256(baseline).hexdigest(),
            candidate_sha256=hashlib.sha256(candidate).hexdigest(),
        ),
        EvolutionPromotionRollbackStep(
            order=2,
            path="new.py",
            operation=EvolutionPromotionRollbackOperation.REMOVE_CREATED_FILE,
            baseline_sha256=None,
            candidate_sha256=hashlib.sha256(b"created = True\n").hexdigest(),
        ),
    )
    plan_core = {
        "policy_version": EVOLUTION_PROMOTION_ROLLBACK_PLAN_POLICY,
        "baseline_commit": commit,
        "baseline_tree_sha256": hashlib.sha256(tree_listing).hexdigest(),
        "source_snapshot_id": "evs_" + "1" * 24,
        "source_snapshot_sha256": "2" * 64,
        "steps": [item.model_dump(mode="json") for item in steps],
        "data_restore_required": False,
        "rollback_review_required": True,
        "rollback_authority": False,
        "rollback_executed": False,
    }
    plan = EvolutionPromotionRollbackPlan.model_validate(
        {**plan_core, "plan_sha256": _digest(plan_core)}
    )
    request_core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_REVALIDATION_ROLLBACK_REQUEST_POLICY,
        "workspace_root": str(root.resolve()),
        "observation_id": "evreruntimeobs_" + "3" * 24,
        "observation_sha256": "4" * 64,
        "plan_id": "evrerolloutplan_" + "5" * 24,
        "plan_sha256": "6" * 64,
        "baseline_id": "evrerolloutbaseline_" + "7" * 24,
        "baseline_sha256": "8" * 64,
        "entry_receipt_id": "evrerolloutentry_" + "9" * 24,
        "entry_receipt_sha256": "a" * 64,
        "promotion_input_id": "evrevalpromoin_" + "b" * 24,
        "promotion_input_sha256": "c" * 64,
        "rollback_plan": plan.model_dump(mode="json"),
        "rollback_plan_sha256": plan.plan_sha256,
        "pause_event_id": pause.event_id,
        "pause_event_sha256": pause.event_sha256,
        "pause_actor": pause.actor.value,
        "pause_reason_code": pause.reason_code,
        "breach_reasons": ["error_rate"],
        "data_restore_required": False,
        "automatic_pause_satisfied": True,
        "monitor_pause_created": True,
        "rollback_request_authority": True,
        "rollback_execution_authority": False,
        "workspace_write_executed": False,
        "git_write_executed": False,
        "rollback_executed": False,
        "promotion_authority": False,
        "requested_at": (T0 + timedelta(minutes=1)).isoformat(),
    }
    request_digest = _digest(request_core)
    request = EvolutionRevalidationRollbackRequest.model_validate({
        **request_core,
        "request_id": f"evrerollbackreq_{request_digest[:24]}",
        "request_sha256": request_digest,
    })
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
            "INSERT INTO evolution_revalidation_runtime_observations VALUES (?, ?)",
            (request.observation_id, request.observation_sha256),
        )
        await db.execute(
            "INSERT INTO evolution_revalidation_promotion_inputs VALUES (?, ?)",
            (request.promotion_input_id, request.promotion_input_sha256),
        )
        await db.commit()
    request_store = EvolutionRevalidationRollbackRequestStore(db_path)
    await request_store.record(request)
    source_store = EvolutionRevalidationRollbackSourceStore(
        db_path,
        storage_dir=root / ".naumi" / "evolution" / "rollback-sources",
    )
    service = EvolutionRevalidationRollbackSourceService(
        workspace_root=root,
        request_store=request_store,
        control_store=control_store,
        store=source_store,
        now=lambda: (T0 + timedelta(hours=1)).isoformat(),
    )
    return service, request, control_service


@pytest.mark.asyncio
async def test_freeze_reads_exact_git_baseline_into_content_addressed_blobs(
    tmp_path: Path,
) -> None:
    service, request, _control = await _scenario(tmp_path)

    sources = await asyncio.gather(*(
        service.freeze(request_id=request.request_id) for _ in range(8)
    ))
    source = sources[0]

    assert all(item == source for item in sources)
    assert source.request_sha256 == request.request_sha256
    assert source.rollback_plan_sha256 == request.rollback_plan_sha256
    assert source.baseline_commit == request.rollback_plan.baseline_commit
    assert source.source_verified and source.content_addressed
    assert source.restore_file_count == 1
    assert source.remove_file_count == 1
    assert not source.rollback_execution_authority
    assert not source.workspace_write_executed
    assert not source.git_write_executed
    restored = next(item for item in source.files if item.storage_key is not None)
    assert (service.store.storage_dir / (restored.storage_key or "")).read_bytes() == (
        b"print('baseline')\n"
    )
    assert await service.freeze(request_id=request.request_id) == source


@pytest.mark.asyncio
async def test_resume_after_request_blocks_new_rollback_source(tmp_path: Path) -> None:
    service, request, control = await _scenario(tmp_path)
    await control.resume(
        reason_code="operator_cancelled_rollback",
        actor=EvolutionRevalidationRolloutControlActor.OPERATOR,
        changed_at=(T0 + timedelta(minutes=2)).isoformat(),
    )

    with pytest.raises(EvolutionRevalidationRollbackSourceError) as blocked:
        await service.freeze(request_id=request.request_id)

    assert blocked.value.code == "rollback_source_pause_stale"
    assert await service.store.get_by_request(request.request_id) is None


@pytest.mark.asyncio
async def test_existing_source_rejects_tampered_blob(tmp_path: Path) -> None:
    service, request, _control = await _scenario(tmp_path)
    source = await service.freeze(request_id=request.request_id)
    blob = next(item for item in source.files if item.storage_key is not None)
    path = service.store.storage_dir / (blob.storage_key or "")
    path.chmod(0o600)
    path.write_bytes(b"tampered")

    with pytest.raises(EvolutionRevalidationRollbackSourceError) as blocked:
        await service.freeze(request_id=request.request_id)

    assert blocked.value.code == "rollback_source_blob_corrupt"


@pytest.mark.asyncio
async def test_empty_baseline_file_is_a_real_restore_blob(tmp_path: Path) -> None:
    service, request, _control = await _scenario(tmp_path, baseline=b"")

    source = await service.freeze(request_id=request.request_id)

    restored = next(item for item in source.files if item.path == "app.py")
    assert restored.storage_key is not None
    assert restored.size_bytes == 0
    assert (service.store.storage_dir / restored.storage_key).read_bytes() == b""
