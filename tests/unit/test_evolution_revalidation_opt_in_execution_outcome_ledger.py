from __future__ import annotations

import asyncio
import math
import os
import subprocess
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from naumi_agent.evolution.revalidation_opt_in_execution_outcome_ledger import (
    EvolutionRevalidationOptInExecutionOutcomeLedgerService,
    EvolutionRevalidationOptInExecutionOutcomeLedgerStore,
)
from naumi_agent.evolution.revalidation_opt_in_execution_outcomes import (
    EvolutionRevalidationOptInExecutionOutcome,
    EvolutionRevalidationOptInExecutionOutcomeError,
)
from naumi_agent.evolution.revalidation_opt_in_observation_window_assessments import (
    EvolutionRevalidationOptInObservationWindowService,
    EvolutionRevalidationOptInObservationWindowStore,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.release.runtime_identity import inspect_runtime_identity
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runs.recorder import ChatRunRecorder
from naumi_agent.runs.store import ChatRunRecord, ChatRunStore
from naumi_agent.runs.usage import RunUsageTotals, build_run_usage
from tests.unit.test_evolution_revalidation_opt_in_observation_windows import (
    _record_chain,
)
from tests.unit.test_evolution_revalidation_opt_in_runtime_health import (
    _CapturingExecutor,
    _health_service,
    _runtime_backend,
)


async def _sources(tmp_path: Path):
    health_service, health_store, _deployment, completion, slots = (
        await _health_service(
            tmp_path,
            backend=_runtime_backend(valid_health=True),
            executor=_CapturingExecutor(),
        )
    )
    health_view = await health_service.observe(
        completion_id=completion.completion_id
    )
    target = slots.resolve_active_backend()
    identity = inspect_runtime_identity(
        slots,
        environment={
            "NAUMI_ACTIVE_SLOT_ID": target.slot.slot_id,
            "NAUMI_ACTIVE_POINTER_GENERATION": str(target.pointer.generation),
            "NAUMI_INSTALL_ROOT": str(slots.release_root),
        },
        runtime_path=target.backend,
        verified_at=health_view.receipt.completed_at,
    )
    stage = health_view.receipt.deployment.intent.cohort.stage
    timeout = max(3, math.ceil(stage.minimum_observation_seconds / 500))
    minimum_samples = math.ceil(stage.minimum_observation_seconds / timeout) + 1
    harness_store = HarnessStore(tmp_path / "harness-execution-outcome.db")
    binding, samples = await _record_chain(
        store=harness_store,
        workspace_root=tmp_path,
        health=health_view.receipt,
        identity=identity,
        subject_id="new-ui-opt-in-execution",
        timeout_seconds=timeout,
        operational_samples=minimum_samples,
        interval_seconds=timeout,
        origin_offset_seconds=0,
        first_operational_offset_seconds=0.001,
    )
    now = [datetime.fromisoformat(samples[-1].observed_at)]
    window_store = EvolutionRevalidationOptInObservationWindowStore(
        health_store.db_path,
        runtime_health_store=health_store,
        harness_store=harness_store,
    )
    window_service = EvolutionRevalidationOptInObservationWindowService(
        workspace_root=tmp_path,
        runtime_health_service=health_service,
        harness_store=harness_store,
        store=window_store,
        clock=lambda: now[0],
    )
    window_view = await window_service.assess(
        completion_id=completion.completion_id,
        subject_id=binding.subject_id,
    )
    assert window_view.runtime_liveness_window_authority
    chat_store = ChatRunStore(
        tmp_path.with_name(f"{tmp_path.name}-chat-runs") / "chat-runs.db"
    )
    outcome_store = EvolutionRevalidationOptInExecutionOutcomeLedgerStore(
        health_store.db_path,
        window_store=window_store,
        chat_run_store=chat_store,
        harness_store=harness_store,
    )
    service = EvolutionRevalidationOptInExecutionOutcomeLedgerService(
        workspace_root=tmp_path,
        window_service=window_service,
        harness_store=harness_store,
        chat_run_store=chat_store,
        store=outcome_store,
        clock=lambda: now[0],
    )
    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return (
        completion,
        binding,
        samples,
        chat_store,
        outcome_store,
        service,
    )


async def _terminal_run(
    *,
    store: ChatRunStore,
    binding,
    outcome: str,
    receipt_completed_at: str | None = None,
) -> ChatRunRecord:
    if receipt_completed_at is None:
        recorder = await ChatRunRecorder.start(
            store=store,
            workspace_root=binding.workspace_root,
            session_id="session-opt-in",
            task=f"验证 {outcome} execution outcome",
            release_binding=binding,
        )
        usage = build_run_usage(
            run_id=recorder.run_id,
            before=RunUsageTotals(0, 0, 0, 0, Decimal("0")),
            after=RunUsageTotals(120, 30, 8, 2, Decimal("0.00425")),
        )
        await recorder.finish(
            "completed" if outcome == "completed" else outcome,
            "真实 managed terminal run 已结束。",
            usage=usage,
        )
        restored = await store.get_run("session-opt-in", recorder.run_id)
        assert restored is not None
        return restored

    run = await store.start_run(
        session_id="session-opt-in",
        user_message_id=f"message-{outcome}",
        release_binding=binding,
    )
    receipt = CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": f"receipt-{run.id}",
            "run_id": run.id,
            "outcome": outcome,
            "summary": "真实 managed terminal run 已结束。",
            "git_state": {"available": True, "dirty": False},
            "started_at": run.started_at,
            "completed_at": receipt_completed_at,
            "duration_ms": 25,
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
@pytest.mark.skipif(os.name == "nt", reason="真实执行夹具使用 POSIX shebang")
async def test_execution_outcome_ledger_records_real_run_and_revalidates_sources(
    tmp_path: Path,
) -> None:
    completion, binding, samples, chat_store, store, service = await _sources(
        tmp_path
    )
    run = await _terminal_run(
        store=chat_store,
        binding=binding,
        outcome="completed",
    )

    views = await asyncio.gather(
        *(
            service.record(
                completion_id=completion.completion_id,
                subject_id=binding.subject_id,
                session_id=run.session_id,
                run_id=run.id,
            )
            for _ in range(6)
        )
    )
    view = views[0]

    assert all(item == view for item in views)
    assert view.execution_outcome_authority
    assert view.successful_completed_run_authority
    assert view.receipt.release_provenance == run.release_provenance
    assert view.receipt.completion_receipt == run.receipt
    assert view.receipt.usage == run.usage
    assert view.receipt.coverage_samples[0].observed_at <= run.started_at
    assert (
        datetime.fromisoformat(view.receipt.coverage_samples[-1].observed_at)
        >= datetime.fromisoformat(run.receipt.completed_at)
    )
    assert view.receipt.usage.reported_cost_usd == Decimal("0.004250000000")
    assert not view.receipt.opt_in_stage_completion_authority
    assert EvolutionRevalidationOptInExecutionOutcome.model_validate_json(
        view.receipt.model_dump_json()
    ) == view.receipt
    assert await store.get_by_run(run.id) == view.receipt
    assert await store.list_for_completion(completion.completion_id) == (
        view.receipt,
    )

    failed_run = await _terminal_run(
        store=chat_store,
        binding=binding,
        outcome="failed",
    )
    failed = await service.record(
        completion_id=completion.completion_id,
        subject_id=binding.subject_id,
        session_id=failed_run.session_id,
        run_id=failed_run.id,
    )
    assert failed.execution_outcome_authority
    assert failed.receipt.result == "failed"
    assert not failed.successful_completed_run_authority
    assert len(await store.list_for_completion(completion.completion_id)) == 2

    pending_run = await _terminal_run(
        store=chat_store,
        binding=binding,
        outcome="completed",
        receipt_completed_at=(
            datetime.fromisoformat(samples[-1].observed_at) + timedelta(seconds=1)
        ).isoformat(),
    )
    with pytest.raises(
        EvolutionRevalidationOptInExecutionOutcomeError,
        match="尚未.*覆盖",
    ) as pending:
        await service.record(
            completion_id=completion.completion_id,
            subject_id=binding.subject_id,
            session_id=pending_run.session_id,
            run_id=pending_run.id,
        )
    assert pending.value.code == "opt_in_execution_coverage_pending"

    assert run.usage is not None
    async with aiosqlite.connect(chat_store.db_path) as db:
        await db.execute(
            "UPDATE chat_runs SET usage_id = ? WHERE id = ?",
            ("runusage_" + "0" * 24, run.id),
        )
        await db.commit()
    stale = await service.inspect(outcome_id=view.receipt.outcome_id)
    assert not stale.chat_run_source_current
    assert not stale.execution_outcome_authority
    assert not stale.successful_completed_run_authority
    assert "chat_run_source_changed" in stale.invalidation_reasons

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE evolution_revalidation_opt_in_execution_outcomes "
            "SET outcome_json = replace(outcome_json, ?, ?) WHERE outcome_id = ?",
            (
                view.receipt.outcome_sha256,
                "0" * 64,
                view.receipt.outcome_id,
            ),
        )
        await db.commit()
    with pytest.raises(EvolutionRevalidationOptInExecutionOutcomeError) as corrupt:
        await store.get(view.receipt.outcome_id)
    assert corrupt.value.code == "opt_in_execution_outcome_source_invalid"
