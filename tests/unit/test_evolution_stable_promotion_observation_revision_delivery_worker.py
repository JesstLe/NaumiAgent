from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import (
    StablePromotionObservationRevisionDeliveryWorkerConfig,
)
from naumi_agent.evolution.stable_promotion_observation_revision_delivery_worker import (
    EvolutionStablePromotionObservationRevisionDeliveryWorker,
    EvolutionStablePromotionObservationRevisionDispatchError,
    EvolutionStablePromotionObservationRevisionDispatchStore,
    EvolutionStablePromotionObservationRevisionTransportError,
    EvolutionStablePromotionObservationRevisionWorkerPolicy,
    LocalStablePromotionObservationRevisionControlPlaneTransport,
)
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePromotionObservationRevisionDeliveryTool,
)
from tests.unit.test_evolution_stable_promotion_observation_chain_cursors import (
    _acknowledge,
    _delivery_setup,
)
from tests.unit.test_evolution_stable_promotion_observation_chain_cursors import (
    _service as _cursor_service,
)
from tests.unit.test_evolution_stable_promotion_observation_revision_deliveries import (
    _revision_service,
)
from tests.unit.test_evolution_stable_promotion_runtime_observation_admissions import (
    _fixture,
)


class _RetryOnceTransport:
    def __init__(self, target) -> None:
        self.target = target
        self.calls = 0

    async def submit(self, submission):
        self.calls += 1
        if self.calls == 1:
            raise EvolutionStablePromotionObservationRevisionTransportError(
                "revision-test-transient",
                "temporary",
                retryable=True,
            )
        return await self.target.submit(submission)


class _PermanentTransport:
    async def submit(self, _submission):
        raise EvolutionStablePromotionObservationRevisionTransportError(
            "revision-test-permanent",
            "permanent",
            retryable=False,
        )


def _policy():
    return EvolutionStablePromotionObservationRevisionWorkerPolicy(
        interval_seconds=1,
        max_empty_backoff_seconds=2,
        max_failure_backoff_seconds=2,
        claim_lease_seconds=5,
        scan_limit=4,
        receipt_timeout_seconds=2,
        retry_base_seconds=1,
        retry_max_seconds=1,
        max_attempts=4,
        shutdown_drain_seconds=2,
        jitter_ratio=0,
    )


def test_revision_worker_config_rejects_unfenced_receipt_timeout() -> None:
    with pytest.raises(ValueError, match="Receipt timeout 必须小于 claim lease"):
        StablePromotionObservationRevisionDeliveryWorkerConfig(
            claim_lease_seconds=10,
            receipt_timeout_seconds=10,
        )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_revision_worker_fences_retries_chains_and_dead_letters(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _fixture(tmp_path, monkeypatch)
    admission_service = data.build_service()
    admission_view = await admission_service.record(
        finalization_receipt_id=data.contract.population_finalization_receipt_id,
        stable_intent_id=data.intent_id,
        subject_id=data.lifecycle.subject_id,
    )
    delivery_service, admission_dispatch, admission_submission, now = (
        await _delivery_setup(
            data,
            admission_service,
            admission_view.admission,
            tmp_path,
        )
    )
    await _acknowledge(
        delivery_service,
        admission_dispatch,
        admission_submission,
        now,
    )
    cursor_service = _cursor_service(
        data,
        admission_service,
        delivery_service,
        admission_dispatch,
    )
    await cursor_service.advance(admission_id=admission_view.admission.admission_id)
    sender = _revision_service(data, cursor_service, delivery_service)
    store = EvolutionStablePromotionObservationRevisionDispatchStore(
        sender.store.db_path
    )
    target = LocalStablePromotionObservationRevisionControlPlaneTransport(
        receiver=sender,
        clock=lambda: now[0],
    )
    retry_transport = _RetryOnceTransport(target)
    worker = EvolutionStablePromotionObservationRevisionDeliveryWorker(
        sender=sender,
        store=store,
        transport=retry_transport,
        policy=_policy(),
        owner_id="revision-worker",
        clock=lambda: now[0],
        random_value=lambda: 0.5,
    )

    queued = await worker.enqueue_next(admission_view.admission.admission_id)
    assert queued.latest_event.state == "queued"
    assert queued.submission.payload.first_sequence == 1
    first_claim = await store.claim(
        owner_id="old-owner",
        now=now[0],
        lease_seconds=3,
    )
    assert first_claim is not None
    now[0] += timedelta(seconds=4)
    replacement = await store.claim(
        owner_id="new-owner",
        now=now[0],
        lease_seconds=3,
    )
    assert replacement is not None
    assert replacement.latest_event.claim_epoch == 2
    with pytest.raises(
        EvolutionStablePromotionObservationRevisionDispatchError
    ) as fenced:
        await store.retry(
            submission_id=queued.submission.submission_id,
            owner_id="old-owner",
            claim_epoch=first_claim.latest_event.claim_epoch,
            failure_code="stale-owner",
            now=now[0],
        )
    assert fenced.value.code == (
        "stable_promotion_observation_revision_dispatch_claim_fenced"
    )
    await store.retry(
        submission_id=queued.submission.submission_id,
        owner_id="new-owner",
        claim_epoch=replacement.latest_event.claim_epoch,
        failure_code="recovered-expired-lease",
        now=now[0],
        base_seconds=1,
        max_seconds=1,
    )

    now[0] += timedelta(seconds=2)
    failed = await worker.run_once()
    assert failed.claimed == failed.retry_scheduled == failed.failures == 1
    assert failed.acknowledged == failed.dead_lettered == 0
    now[0] += timedelta(seconds=2)
    delivered = await worker.run_once()
    assert delivered.claimed == delivered.acknowledged == 1
    assert delivered.failures == delivered.chained == 0
    assert await store.acknowledged_head(admission_view.admission.admission_id) == (
        2,
        queued.submission.payload.revisions[-1].revision_sha256,
    )

    data.runtime_now[0] += timedelta(seconds=10)
    await data.lifecycle._producer.pulse_now()
    now[0] = data.runtime_now[0] + timedelta(seconds=1)
    await cursor_service.advance(admission_id=admission_view.admission.admission_id)
    next_pass = await worker.run_once()
    second = await store.latest_for_admission(
        admission_view.admission.admission_id
    )
    assert second is not None
    assert second.submission.payload.first_sequence == 3
    assert next_pass.claimed == next_pass.acknowledged == next_pass.chained == 1
    assert await store.acknowledged_head(admission_view.admission.admission_id) == (
        3,
        second.submission.payload.revisions[-1].revision_sha256,
    )

    engine = SimpleNamespace(
        evolution_stable_promotion_observation_revision_delivery_service=sender,
        evolution_stable_promotion_observation_revision_dispatch_store=store,
        enqueue_stable_promotion_observation_revision_delivery=worker.enqueue_next,
        run_stable_promotion_observation_revision_delivery_once=worker.run_once,
        stable_promotion_observation_revision_delivery_worker_snapshot=(
            worker.snapshot
        ),
    )
    tool = EvolutionStablePromotionObservationRevisionDeliveryTool(engine)
    assert "状态：**acknowledged**" in await tool.execute(
        action="inspect-dispatch",
        admission_id=admission_view.admission.admission_id,
    )
    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(
                    **registered.parse_arguments(call.arguments)
                ),
            )

    slash = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-promotion-observation-revisions inspect-dispatch "
        + admission_view.admission.admission_id,
    )
    assert "Observation Revision Dispatch" in slash

    data.runtime_now[0] += timedelta(seconds=10)
    await data.lifecycle._producer.pulse_now()
    now[0] = data.runtime_now[0] + timedelta(seconds=1)
    await cursor_service.advance(admission_id=admission_view.admission.admission_id)
    dead = await worker.enqueue_next(admission_view.admission.admission_id)
    worker.transport = _PermanentTransport()
    dead_pass = await worker.run_once()
    assert dead_pass.claimed == dead_pass.dead_lettered == dead_pass.failures == 1
    dead_view = await store.get(dead.submission.submission_id)
    assert dead_view is not None and dead_view.latest_event.state == "dead_letter"
    assert await store.claim(
        owner_id="late-owner",
        now=now[0] + timedelta(days=30),
    ) is None

    assert worker.start()
    await asyncio.sleep(0)
    assert await worker.stop()
    assert worker.snapshot().state.value == "stopped"

    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_observation_revision_dispatches "
            "SET latest_event_json = ? WHERE submission_id = ?",
            ("{}", second.submission.submission_id),
        )
    with pytest.raises(
        EvolutionStablePromotionObservationRevisionDispatchError
    ) as corrupt:
        await store.get(second.submission.submission_id)
    assert corrupt.value.code == (
        "stable_promotion_observation_revision_dispatch_store_corrupt"
    )
