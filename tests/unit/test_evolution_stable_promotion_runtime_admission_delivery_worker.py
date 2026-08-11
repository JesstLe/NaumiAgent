from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import (
    StablePromotionRuntimeAdmissionDeliveryWorkerConfig,
)
from naumi_agent.evolution.stable_promotion_runtime_admission_delivery_worker import (
    EvolutionStablePromotionRuntimeAdmissionDeliveryWorker,
    EvolutionStablePromotionRuntimeAdmissionDispatchError,
    EvolutionStablePromotionRuntimeAdmissionDispatchStore,
    EvolutionStablePromotionRuntimeAdmissionTransportError,
    EvolutionStablePromotionRuntimeAdmissionWorkerPolicy,
    LocalStablePromotionRuntimeAdmissionControlPlaneTransport,
)
from naumi_agent.release.installation_keys import ReleaseInstallationKeyService
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePromotionRuntimeAdmissionDeliveryTool,
)
from tests.unit.test_evolution_stable_promotion_runtime_admission_deliveries import (
    _delivery_service,
)
from tests.unit.test_evolution_stable_promotion_runtime_observation_admissions import (
    _fixture,
)
from tests.unit.test_release_installation_keys import _MemoryBackend


class _RetryOnceTransport:
    def __init__(self, target) -> None:
        self.target = target
        self.calls = 0

    async def submit(self, submission):
        self.calls += 1
        if self.calls == 1:
            raise EvolutionStablePromotionRuntimeAdmissionTransportError(
                "stable_promotion_admission_test_unavailable",
                "测试链路暂时不可用。",
            )
        return await self.target.submit(submission)


@pytest.mark.parametrize(
    "overrides",
    [
        {"receipt_timeout_seconds": 60, "claim_lease_seconds": 60},
        {"retry_base_seconds": 10, "retry_max_seconds": 5},
        {"interval_seconds": 10, "max_empty_backoff_seconds": 5},
        {"interval_seconds": 10, "max_failure_backoff_seconds": 5},
    ],
)
def test_runtime_admission_worker_config_fails_closed(overrides) -> None:
    with pytest.raises(ValueError):
        StablePromotionRuntimeAdmissionDeliveryWorkerConfig(**overrides)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_runtime_admission_worker_closes_fencing_retry_ack_and_dead_letter(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = await _fixture(tmp_path, monkeypatch)
    admission_service = data.build_service()
    admission_view = await admission_service.record(
        finalization_receipt_id=data.contract.population_finalization_receipt_id,
        stable_intent_id=data.intent_id,
        subject_id=data.lifecycle.subject_id,
    )
    admission = admission_view.admission
    private_key = data.context["keys"][admission.installation_member_id]
    now = [datetime.fromisoformat(admission.admitted_at) + timedelta(seconds=1)]
    key_service = ReleaseInstallationKeyService(
        tmp_path / "installation-release",
        backend=_MemoryBackend(),
        key_factory=lambda _size: private_key.private_bytes_raw(),
        clock=lambda: now[0],
    )
    key_service.provision(channel="stable")
    sender = _delivery_service(data, admission_service, key_service)
    submission = await sender.prepare(admission_id=admission.admission_id)
    store = EvolutionStablePromotionRuntimeAdmissionDispatchStore(sender.store.db_path)

    left, right = await asyncio.gather(
        store.enqueue(submission, enqueued_at=now[0]),
        store.enqueue(submission, enqueued_at=now[0]),
    )
    assert left == right
    first = await store.claim(owner_id="owner-a", now=now[0], lease_seconds=3)
    assert first is not None and first.latest_event.claim_epoch == 1
    now[0] += timedelta(seconds=4)
    second = await store.claim(owner_id="owner-b", now=now[0], lease_seconds=3)
    assert second is not None and second.latest_event.claim_epoch == 2
    with pytest.raises(EvolutionStablePromotionRuntimeAdmissionDispatchError) as fenced:
        await store.retry(
            admission_id=admission.admission_id,
            owner_id="owner-a",
            claim_epoch=1,
            failure_code="stale-owner",
            now=now[0],
        )
    assert fenced.value.code == "stable_promotion_admission_dispatch_claim_fenced"
    queued = await store.retry(
        admission_id=admission.admission_id,
        owner_id="owner-b",
        claim_epoch=2,
        failure_code="retryable-test",
        now=now[0],
        base_seconds=1,
        max_seconds=1,
    )
    assert queued.latest_event.state == "queued"

    dead_db = tmp_path / "dead-letter.sqlite3"
    with sqlite3.connect(sender.store.db_path) as source, sqlite3.connect(dead_db) as target:
        source.backup(target)
    dead_store = EvolutionStablePromotionRuntimeAdmissionDispatchStore(dead_db)
    dead_claim = await dead_store.claim(
        owner_id="dead-owner",
        now=now[0] + timedelta(seconds=2),
        lease_seconds=10,
    )
    assert dead_claim is not None
    dead = await dead_store.dead_letter(
        admission_id=admission.admission_id,
        owner_id="dead-owner",
        claim_epoch=dead_claim.latest_event.claim_epoch,
        failure_code="permanent-test",
        now=now[0] + timedelta(seconds=2),
    )
    assert dead.latest_event.state == "dead_letter"
    assert await dead_store.claim(
        owner_id="future-owner",
        now=now[0] + timedelta(days=30),
    ) is None

    target = LocalStablePromotionRuntimeAdmissionControlPlaneTransport(
        receiver=sender,
        clock=lambda: now[0],
    )
    transport = _RetryOnceTransport(target)
    worker = EvolutionStablePromotionRuntimeAdmissionDeliveryWorker(
        sender=sender,
        store=store,
        transport=transport,
        policy=EvolutionStablePromotionRuntimeAdmissionWorkerPolicy(
            interval_seconds=1,
            max_empty_backoff_seconds=2,
            max_failure_backoff_seconds=2,
            claim_lease_seconds=10,
            scan_limit=1,
            receipt_timeout_seconds=5,
            retry_base_seconds=1,
            retry_max_seconds=1,
            max_attempts=8,
            shutdown_drain_seconds=2,
            jitter_ratio=0,
        ),
        owner_id="runtime-admission-worker",
        clock=lambda: now[0],
        random_value=lambda: 0.5,
    )
    now[0] += timedelta(seconds=2)
    failed = await worker.run_once()
    assert failed.claimed == failed.retry_scheduled == failed.failures == 1
    assert failed.acknowledged == failed.dead_lettered == 0
    now[0] += timedelta(seconds=2)
    delivered = await worker.run_once()
    assert delivered.claimed == delivered.acknowledged == 1
    assert delivered.failures == 0
    view = await store.get(admission.admission_id)
    assert view is not None
    assert view.latest_event.state == "acknowledged"
    assert view.receipt is not None
    assert view.receipt.admission_id == admission.admission_id
    assert await store.claim(
        owner_id="late-owner",
        now=now[0] + timedelta(days=30),
    ) is None

    engine = SimpleNamespace(
        evolution_stable_promotion_runtime_admission_delivery_service=sender,
        evolution_stable_promotion_runtime_admission_dispatch_store=store,
        enqueue_stable_promotion_runtime_admission_delivery=worker.enqueue,
        run_stable_promotion_runtime_admission_delivery_once=worker.run_once,
        stable_promotion_runtime_admission_delivery_worker_snapshot=worker.snapshot,
    )
    tool = EvolutionStablePromotionRuntimeAdmissionDeliveryTool(engine)
    assert "状态：**acknowledged**" in await tool.execute(
        action="inspect-dispatch",
        admission_id=admission.admission_id,
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
        "/evolution stable-promotion-admission-delivery inspect-dispatch "
        + admission.admission_id,
    )
    assert "Runtime Admission Dispatch" in slash

    with sqlite3.connect(store.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM "
            "evolution_stable_promotion_runtime_admission_dispatch_events "
            "WHERE admission_id = ?",
            (admission.admission_id,),
        ).fetchone()
        assert count is not None and count[0] >= 7
        db.execute(
            "UPDATE evolution_stable_promotion_runtime_admission_dispatches "
            "SET submission_id = ? WHERE admission_id = ?",
            ("evstablepromsubmit_" + "f" * 24, admission.admission_id),
        )
    with pytest.raises(EvolutionStablePromotionRuntimeAdmissionDispatchError) as row_corrupt:
        await store.get(admission.admission_id)
    assert row_corrupt.value.code == "stable_promotion_admission_dispatch_store_corrupt"
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_runtime_admission_dispatches "
            "SET submission_id = ? WHERE admission_id = ?",
            (submission.submission_id, admission.admission_id),
        )
        db.execute(
            "UPDATE evolution_stable_promotion_runtime_admission_dispatch_events "
            "SET event_json = '{}' WHERE admission_id = ? AND sequence = 1",
            (admission.admission_id,),
        )
    with pytest.raises(EvolutionStablePromotionRuntimeAdmissionDispatchError) as corrupt:
        await store.get(admission.admission_id)
    assert corrupt.value.code == "stable_promotion_admission_dispatch_chain_corrupt"
