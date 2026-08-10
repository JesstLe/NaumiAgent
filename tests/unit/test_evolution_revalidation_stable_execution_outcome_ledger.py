from __future__ import annotations

import asyncio
import math
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_stable_execution_outcome_ledger import (
    EvolutionRevalidationStableExecutionOutcomeLedgerService,
    EvolutionRevalidationStableExecutionOutcomeLedgerStore,
)
from naumi_agent.evolution.revalidation_stable_execution_outcomes import (
    EvolutionRevalidationStableExecutionOutcome,
    EvolutionRevalidationStableExecutionOutcomeError,
)
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runs.store import ChatRunRecord, ChatRunStore
from naumi_agent.runs.usage import RunUsageTotals, build_run_usage
from tests.unit.test_evolution_revalidation_stable_observation_window_assessments import (
    _assessment_service,
)
from tests.unit.test_evolution_revalidation_stable_observation_windows import (
    _started_exposure,
)


async def _sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    subject_id = "stable-execution-primary"
    data, lifecycle, exposure = await _started_exposure(
        tmp_path,
        monkeypatch,
        subject_id=subject_id,
    )
    stage = exposure.deployment.preparation.intent.plan.stages[3]
    timeout = exposure.ready_observation.timeout_seconds
    minimum_samples = math.ceil(stage.minimum_observation_seconds / timeout) + 1
    for _ in range(minimum_samples - 1):
        data["runtime_now"][0] += timedelta(seconds=timeout - 1)
        await lifecycle._producer.pulse_now()
    exposure_service, exposure_store = data["build_exposure_service"]()
    window_service, window_store = _assessment_service(
        data,
        exposure_service=exposure_service,
        exposure_store=exposure_store,
    )
    window = await window_service.assess(
        intent_id=data["intent_id"],
        subject_id=subject_id,
    )
    assert window.stable_runtime_window_authority
    chat_store = ChatRunStore(tmp_path / "stable-chat-runs.db")
    outcome_store = EvolutionRevalidationStableExecutionOutcomeLedgerStore(
        exposure_store.db_path,
        window_store=window_store,
        chat_run_store=chat_store,
        harness_store=data["harness_store"],
    )
    service = EvolutionRevalidationStableExecutionOutcomeLedgerService(
        workspace_root=tmp_path,
        window_service=window_service,
        harness_store=data["harness_store"],
        chat_run_store=chat_store,
        store=outcome_store,
        clock=lambda: max(data["runtime_now"][0], datetime.now(UTC)),
    )
    return data, lifecycle, exposure, chat_store, outcome_store, service


async def _terminal_run(
    *,
    store: ChatRunStore,
    binding,
    outcome: str,
    index: int,
    receipt_completed_at: str | None = None,
) -> ChatRunRecord:
    run = await store.start_run(
        session_id="session-stable",
        user_message_id=f"message-{index}",
        release_binding=binding,
    )
    completed_at = receipt_completed_at or run.started_at
    receipt = CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": f"receipt-{run.id}",
            "run_id": run.id,
            "outcome": outcome,
            "summary": "真实 stable managed run 已结束。",
            "git_state": {"available": True, "dirty": False},
            "started_at": run.started_at,
            "completed_at": completed_at,
            "duration_ms": 0,
        }
    )
    usage = build_run_usage(
        run_id=run.id,
        before=RunUsageTotals(0, 0, 0, 0, Decimal("0")),
        after=RunUsageTotals(120, 30, 8, 2, Decimal("0.00425")),
    )
    status = {
        "completed": "completed",
        "partial": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }[outcome]
    await store.finish_run(
        run.id,
        status=status,
        receipt=receipt,
        usage=usage,
    )
    restored = await store.get_run(run.session_id, run.id)
    assert restored is not None
    return restored


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_stable_outcome_records_real_runs_and_revalidates_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, lifecycle, exposure, chat_store, store, service = await _sources(tmp_path, monkeypatch)
    intent_id = data["intent_id"]
    subject_id = exposure.binding.subject_id
    run_now = data["runtime_now"][0] - timedelta(seconds=1)
    monkeypatch.setattr(
        "naumi_agent.runs.store._now_iso",
        lambda: run_now.isoformat(),
    )
    completed_run = await _terminal_run(
        store=chat_store,
        binding=exposure.binding,
        outcome="completed",
        index=1,
    )

    views = await asyncio.gather(
        *(
            service.record(
                intent_id=intent_id,
                subject_id=subject_id,
                session_id=completed_run.session_id,
                run_id=completed_run.id,
            )
            for _ in range(6)
        )
    )
    view = views[0]
    assert all(item == view for item in views)
    assert view.execution_outcome_authority
    assert view.stable_population_observation_input_authority
    assert view.successful_completed_run_authority
    assert view.stable_completed_run_authority
    assert view.receipt.release_provenance == completed_run.release_provenance
    assert view.receipt.completion_receipt == completed_run.receipt
    assert view.receipt.usage == completed_run.usage
    assert view.receipt.coverage_samples[0].observed_at <= completed_run.started_at
    assert (
        datetime.fromisoformat(view.receipt.coverage_samples[-1].observed_at)
        >= datetime.fromisoformat(completed_run.receipt.completed_at)
    )
    assert view.receipt.usage.reported_cost_usd == Decimal("0.004250000000")
    assert view.receipt.liveness_source.intent_id == intent_id
    assert view.receipt.liveness_source.stage_order == 4
    assert view.receipt.liveness_source.exposure_percent == 100
    assert view.receipt.liveness_source.population_snapshot_id == (
        exposure.deployment.preparation.intent.population_snapshot_id
    )
    assert view.receipt.liveness_source.installation_member_id == (
        exposure.deployment.preparation.intent.proof.payload.member_id
    )
    assert not view.receipt.stable_stage_completion_authority
    assert not view.receipt.stable_rollout_authority
    assert not view.receipt.promotion_authority
    assert EvolutionRevalidationStableExecutionOutcome.model_validate_json(
        view.receipt.model_dump_json()
    ) == view.receipt
    assert await store.get_by_run(completed_run.id) == view.receipt
    assert await store.list_for_intent(intent_id) == (view.receipt,)
    with pytest.raises(ValueError, match="intent_id"):
        await store.list_for_intent("' OR 1=1 --")

    failed_run = await _terminal_run(
        store=chat_store,
        binding=exposure.binding,
        outcome="failed",
        index=2,
    )
    failed = await service.record(
        intent_id=intent_id,
        subject_id=subject_id,
        session_id=failed_run.session_id,
        run_id=failed_run.id,
    )
    assert failed.execution_outcome_authority
    assert failed.stable_population_observation_input_authority
    assert failed.receipt.result == "failed"
    assert not failed.successful_completed_run_authority
    assert not failed.stable_completed_run_authority

    pending_run = await _terminal_run(
        store=chat_store,
        binding=exposure.binding,
        outcome="completed",
        index=3,
        receipt_completed_at=(
            data["runtime_now"][0] + timedelta(seconds=1)
        ).isoformat(),
    )
    with pytest.raises(
        EvolutionRevalidationStableExecutionOutcomeError,
        match="尚未.*覆盖",
    ) as pending:
        await service.record(
            intent_id=intent_id,
            subject_id=subject_id,
            session_id=pending_run.session_id,
            run_id=pending_run.id,
        )
    assert pending.value.code == "stable_execution_coverage_pending"

    duplicate_run = await _terminal_run(
        store=chat_store,
        binding=exposure.binding,
        outcome="completed",
        index=4,
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS evolution_revalidation_opt_in_execution_outcomes "
            "(run_id TEXT PRIMARY KEY)"
        )
        columns = await (
            await db.execute(
                "PRAGMA table_info(evolution_revalidation_opt_in_execution_outcomes)"
            )
        ).fetchall()
        if len(columns) == 1:
            await db.execute(
                "INSERT INTO evolution_revalidation_opt_in_execution_outcomes "
                "(run_id) VALUES (?)",
                (duplicate_run.id,),
            )
        else:
            await db.execute(
                "INSERT INTO evolution_revalidation_opt_in_execution_outcomes "
                "(outcome_id, outcome_sha256, completion_id, window_id, run_id, "
                "session_id, result, successful, execution_completed_at, outcome_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "evreoptinoutcome_" + "a" * 24,
                    "a" * 64,
                    "cross-stage-fixture",
                    "evreoptinwindow_" + "a" * 24,
                    duplicate_run.id,
                    duplicate_run.session_id,
                    "completed",
                    1,
                    duplicate_run.completed_at,
                    "{}",
                ),
            )
        await db.commit()
    with pytest.raises(EvolutionRevalidationStableExecutionOutcomeError) as duplicate:
        await service.record(
            intent_id=intent_id,
            subject_id=subject_id,
            session_id=duplicate_run.session_id,
            run_id=duplicate_run.id,
        )
    assert duplicate.value.code == "stable_execution_cross_stage_duplicate"

    percentage_duplicate_run = await _terminal_run(
        store=chat_store,
        binding=exposure.binding,
        outcome="completed",
        index=5,
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS "
            "evolution_revalidation_percentage_execution_outcomes "
            "(run_id TEXT PRIMARY KEY)"
        )
        columns = await (
            await db.execute(
                "PRAGMA table_info("
                "evolution_revalidation_percentage_execution_outcomes)"
            )
        ).fetchall()
        if len(columns) == 1:
            await db.execute(
                "INSERT INTO evolution_revalidation_percentage_execution_outcomes "
                "(run_id) VALUES (?)",
                (percentage_duplicate_run.id,),
            )
        else:
            await db.execute(
                "INSERT INTO evolution_revalidation_percentage_execution_outcomes "
                "(outcome_id, outcome_sha256, assignment_id, window_id, run_id, "
                "session_id, result, successful, execution_completed_at, outcome_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "evrepercentoutcome_" + "b" * 24,
                    "b" * 64,
                    "evrepercentassign_" + "b" * 24,
                    "evrepercentwindow_" + "b" * 24,
                    percentage_duplicate_run.id,
                    percentage_duplicate_run.session_id,
                    "completed",
                    1,
                    percentage_duplicate_run.completed_at,
                    "{}",
                ),
            )
        await db.commit()
    with pytest.raises(
        EvolutionRevalidationStableExecutionOutcomeError,
    ) as percentage_duplicate:
        await service.record(
            intent_id=intent_id,
            subject_id=subject_id,
            session_id=percentage_duplicate_run.session_id,
            run_id=percentage_duplicate_run.id,
        )
    assert percentage_duplicate.value.code == (
        "stable_execution_cross_stage_duplicate"
    )

    first_coverage = view.receipt.coverage_samples[0]
    async with aiosqlite.connect(data["harness_store"].db_path) as db:
        await db.execute(
            "UPDATE harness_runtime_release_observations "
            "SET sample_json = replace(sample_json, ?, ?) "
            "WHERE subject_id = ? AND heartbeat_sequence = ?",
            (
                first_coverage.sample_sha256,
                "0" * 64,
                subject_id,
                first_coverage.heartbeat_sequence,
            ),
        )
        await db.commit()
    coverage_stale = await service.inspect(outcome_id=view.receipt.outcome_id)
    assert not coverage_stale.heartbeat_coverage_current
    assert not coverage_stale.execution_outcome_authority
    assert "heartbeat_coverage_changed" in coverage_stale.invalidation_reasons

    assert completed_run.usage is not None
    async with aiosqlite.connect(chat_store.db_path) as db:
        await db.execute(
            "UPDATE chat_runs SET usage_id = ? WHERE id = ?",
            ("runusage_" + "0" * 24, completed_run.id),
        )
        await db.commit()
    stale = await service.inspect(outcome_id=view.receipt.outcome_id)
    assert not stale.chat_run_source_current
    assert not stale.execution_outcome_authority
    assert not stale.successful_completed_run_authority
    assert "chat_run_source_changed" in stale.invalidation_reasons

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_stable_execution_outcomes "
            "SET outcome_json = replace(outcome_json, ?, ?) WHERE outcome_id = ?",
            (
                view.receipt.outcome_sha256,
                "0" * 64,
                view.receipt.outcome_id,
            ),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationStableExecutionOutcomeError) as corrupt:
        await store.get(view.receipt.outcome_id)
    assert corrupt.value.code == "stable_execution_outcome_source_invalid"
    assert await lifecycle.close()
