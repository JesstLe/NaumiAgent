from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.evolution.stable_remote_finalization_deliveries import (
    EvolutionStableRemoteFinalizationDeliveryError,
    EvolutionStableRemoteFinalizationDeliveryService,
)
from naumi_agent.evolution.stable_remote_finalization_result_return_worker import (
    EvolutionStableRemoteFinalizationResultReturnError,
    EvolutionStableRemoteFinalizationResultReturnStore,
    EvolutionStableRemoteFinalizationResultReturnWorker,
    EvolutionStableRemoteFinalizationResultReturnWorkerPolicy,
    EvolutionStableRemoteFinalizationResultTransportError,
    LocalStableRemoteFinalizationControlPlaneTransport,
)
from naumi_agent.release.installation_keys import ReleaseInstallationKeyError
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionStableRemoteFinalizationTool
from tests.unit.test_evolution_stable_remote_finalization_deliveries import _delivery


async def _ready(tmp_path: Path):
    fixture, service, delivery_store, delivery, credential, journal = await _delivery(tmp_path)
    delivery_service = EvolutionStableRemoteFinalizationDeliveryService(
        finalization_service=service,
        store=delivery_store,
        clock=fixture.data.clock,
    )
    await delivery_store.claim(owner_id="delivery-worker", now=fixture.data.clock().isoformat())
    ack = journal.receive(
        package=delivery.package,
        trust_policy=fixture.policy,
        credential=credential,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    await delivery_store.acknowledge(
        delivery_id=delivery.package.delivery_id,
        ack=ack,
        credential=credential,
        now=fixture.data.clock().isoformat(),
    )
    return fixture, delivery_service, delivery_store, delivery, credential, journal


def _worker(*, fixture, delivery_service, credential, journal, transport=None, **policy):
    async def resolve(package):
        assert package.installation_member_id == credential.payload.member_id
        return credential

    policy_values = {
        "interval_seconds": 0.1,
        "max_empty_backoff_seconds": 1,
        "max_failure_backoff_seconds": 1,
        "journal_scan_limit": 10,
        "return_scan_limit": 10,
        "claim_lease_seconds": 3,
        "result_timeout_seconds": 0.2,
        "retry_base_seconds": 0.1,
        "retry_max_seconds": 1,
        "shutdown_drain_seconds": 1,
        "jitter_ratio": 0,
    }
    policy_values.update(policy)
    return EvolutionStableRemoteFinalizationResultReturnWorker(
        journal=journal,
        store=EvolutionStableRemoteFinalizationResultReturnStore(journal.db_path),
        release_slot_store=fixture.data.fixture.store,
        installation_key_service=fixture.data.key_service,
        trust_policy_provider=lambda: fixture.policy,
        credential_resolver=resolve,
        transport=(
            transport
            if transport is not None
            else LocalStableRemoteFinalizationControlPlaneTransport(delivery_service)
        ),
        policy=EvolutionStableRemoteFinalizationResultReturnWorkerPolicy(**policy_values),
        owner_id="result-return-test-worker",
        clock=fixture.data.clock,
        random_value=lambda: 0.5,
    )


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_return_worker_closes_real_writer_to_receipt_loop(
    tmp_path: Path,
) -> None:
    fixture, delivery_service, delivery_store, delivery, credential, journal = await _ready(
        tmp_path
    )
    worker = _worker(
        fixture=fixture,
        delivery_service=delivery_service,
        credential=credential,
        journal=journal,
    )

    result = await worker.run_once()

    assert result.synchronized == 1
    assert result.claimed == 2
    assert result.executed == result.returned == 1
    assert result.failures == result.retry_scheduled == result.dead_lettered == 0
    outbox = await worker.store.get(delivery.package.delivery_id)
    assert outbox is not None
    assert outbox.latest_event.state == "completed"
    assert outbox.submission is not None and outbox.receipt is not None
    control = await delivery_store.get(delivery.package.delivery_id)
    assert control is not None and control.latest_event.state == "completed"
    assert control.receipt == outbox.receipt
    assert await worker.run_once() == type(result)()


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_return_retries_transport_then_completes_exact_receipt(
    tmp_path: Path,
) -> None:
    fixture, delivery_service, delivery_store, delivery, credential, journal = await _ready(
        tmp_path
    )
    local = LocalStableRemoteFinalizationControlPlaneTransport(delivery_service)
    calls = 0

    class FlakyTransport:
        async def submit(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise EvolutionStableRemoteFinalizationResultTransportError(
                    "control_plane_temporarily_offline", "offline"
                )
            return await local.submit(**kwargs)

    worker = _worker(
        fixture=fixture,
        delivery_service=delivery_service,
        credential=credential,
        journal=journal,
        transport=FlakyTransport(),
    )

    first = await worker.run_once()
    assert first.executed == 1
    assert first.retry_scheduled == 1
    queued = await worker.store.get(delivery.package.delivery_id)
    assert queued is not None and queued.latest_event.state == "queued"
    assert queued.latest_event.phase == "return"
    fixture.data.clock.value += timedelta(seconds=1)

    second = await worker.run_once()

    assert second.returned == 1
    assert (await delivery_store.get(delivery.package.delivery_id)).latest_event.state == (
        "completed"
    )


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_return_recovers_writer_after_key_failure_and_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture, delivery_service, delivery_store, delivery, credential, journal = await _ready(
        tmp_path
    )
    worker = _worker(
        fixture=fixture,
        delivery_service=delivery_service,
        credential=credential,
        journal=journal,
    )
    original = fixture.data.key_service.sign_remote_finalization_result

    def unavailable(**_kwargs):
        raise ReleaseInstallationKeyError(
            "release_installation_key_temporarily_unavailable", "offline"
        )

    monkeypatch.setattr(fixture.data.key_service, "sign_remote_finalization_result", unavailable)
    first = await worker.run_once()
    assert first.retry_scheduled == 1
    assert journal.get(delivery.package.delivery_id).state == "writer_committed"
    fixture.data.clock.value += timedelta(seconds=121)
    monkeypatch.setattr(fixture.data.key_service, "sign_remote_finalization_result", original)

    recovered = await worker.run_once()

    assert recovered.executed == recovered.returned == 1
    final = await delivery_store.get(delivery.package.delivery_id)
    assert final is not None and final.latest_event.state == "completed"


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_return_store_fences_stale_owner_and_detects_journal_tamper(
    tmp_path: Path,
) -> None:
    fixture, delivery_service, _, delivery, credential, journal = await _ready(tmp_path)
    store = EvolutionStableRemoteFinalizationResultReturnStore(journal.db_path)
    await store.enqueue(
        package=delivery.package,
        submission=None,
        enqueued_at=fixture.data.clock().isoformat(),
    )
    first = await store.claim(
        owner_id="first", now=fixture.data.clock().isoformat(), lease_seconds=3
    )
    assert first is not None
    fixture.data.clock.value += timedelta(seconds=4)
    second = await store.claim(
        owner_id="second", now=fixture.data.clock().isoformat(), lease_seconds=3
    )
    assert second is not None and second.latest_event.claim_epoch == 2
    with pytest.raises(EvolutionStableRemoteFinalizationResultReturnError) as fenced:
        await store.retry(
            delivery_id=delivery.package.delivery_id,
            owner_id="first",
            claim_epoch=1,
            failure_code="stale",
            now=fixture.data.clock().isoformat(),
            base_seconds=1,
            max_seconds=2,
        )
    assert fenced.value.code == "stable_remote_result_return_claim_fenced"

    with sqlite3.connect(journal.db_path) as db:
        db.execute(
            "UPDATE stable_finalization_delivery_journal SET ack_sha256 = ? WHERE delivery_id = ?",
            ("0" * 64, delivery.package.delivery_id),
        )
    with pytest.raises(EvolutionStableRemoteFinalizationDeliveryError) as corrupt:
        journal.get(delivery.package.delivery_id)
    assert corrupt.value.code == "stable_remote_delivery_journal_corrupt"


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_return_claim_is_single_winner_under_concurrency(
    tmp_path: Path,
) -> None:
    fixture, _, _, delivery, _, journal = await _ready(tmp_path)
    store = EvolutionStableRemoteFinalizationResultReturnStore(journal.db_path)
    await store.enqueue(
        package=delivery.package,
        submission=None,
        enqueued_at=fixture.data.clock().isoformat(),
    )

    claims = await asyncio.gather(*(
        EvolutionStableRemoteFinalizationResultReturnStore(journal.db_path).claim(
            owner_id=f"worker-{index}",
            now=fixture.data.clock().isoformat(),
            lease_seconds=3,
        )
        for index in range(12)
    ))

    winners = [item for item in claims if item is not None]
    assert len(winners) == 1
    assert winners[0].latest_event.claim_epoch == 1


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_return_journal_anti_join_does_not_starve_new_records(
    tmp_path: Path,
) -> None:
    first = await _ready(tmp_path / "first")
    second = await _ready(tmp_path / "second")
    first_fixture, _, _, first_delivery, _, first_journal = first
    _, _, _, second_delivery, _, second_journal = second
    with sqlite3.connect(second_journal.db_path) as source:
        row = source.execute(
            "SELECT delivery_id, package_json, state, ack_json, ack_sha256, "
            "writer_json, writer_sha256, result_json, result_sha256 FROM "
            "stable_finalization_delivery_journal WHERE delivery_id = ?",
            (second_delivery.package.delivery_id,),
        ).fetchone()
    with sqlite3.connect(first_journal.db_path) as target:
        target.execute(
            "INSERT INTO stable_finalization_delivery_journal "
            "(delivery_id, package_json, state, ack_json, ack_sha256, writer_json, "
            "writer_sha256, result_json, result_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    store = EvolutionStableRemoteFinalizationResultReturnStore(first_journal.db_path)
    await store.enqueue(
        package=first_delivery.package,
        submission=None,
        enqueued_at=first_fixture.data.clock().isoformat(),
    )

    synchronized = await store.synchronize_from_journal(
        journal=first_journal,
        limit=1,
        enqueued_at=first_fixture.data.clock().isoformat(),
    )

    assert synchronized == 1
    assert await store.get(second_delivery.package.delivery_id) is not None


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_return_shutdown_drains_in_flight_transport(tmp_path: Path) -> None:
    fixture, delivery_service, _, delivery, credential, journal = await _ready(tmp_path)
    local = LocalStableRemoteFinalizationControlPlaneTransport(delivery_service)
    entered = asyncio.Event()
    release = asyncio.Event()

    class ControlledTransport:
        async def submit(self, **kwargs):
            entered.set()
            await release.wait()
            return await local.submit(**kwargs)

    worker = _worker(
        fixture=fixture,
        delivery_service=delivery_service,
        credential=credential,
        journal=journal,
        transport=ControlledTransport(),
    )
    assert worker.start() and worker.wake()
    await asyncio.wait_for(entered.wait(), timeout=1)
    stop_task = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    release.set()

    assert await stop_task
    assert (await worker.store.get(delivery.package.delivery_id)).latest_event.state == (
        "completed"
    )
    assert worker.snapshot().forced_shutdown_count == 0


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_result_return_agent_tool_and_slash_share_worker(tmp_path: Path) -> None:
    fixture, delivery_service, _, _, credential, journal = await _ready(tmp_path)
    worker = _worker(
        fixture=fixture,
        delivery_service=delivery_service,
        credential=credential,
        journal=journal,
    )

    class Engine:
        evolution_stable_remote_finalization_service = (
            delivery_service.finalization_service
        )
        evolution_stable_remote_finalization_result_return_worker = worker

        async def run_stable_remote_finalization_result_return_once(self):
            return await worker.run_once()

        def stable_remote_finalization_result_return_worker_snapshot(self):
            return worker.snapshot()

    tool = EvolutionStableRemoteFinalizationTool(Engine())
    registry = ToolRegistry()
    registry.register(tool)

    class SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None) -> ToolResult:
            assert agent_name == "cli"
            registered = self.tool_registry.get(call.name)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(
                    **registered.parse_arguments(call.arguments)
                ),
            )

    before = await tool.execute(action="inspect-result-return-worker")
    assert "**stopped**" in before
    via_slash = strip_ansi(await execute_slash_command(
        SlashEngine(),
        "/evolution stable-remote-finalization run-result-return-worker",
    ))
    assert "**本轮完成**" in via_slash
    assert "Receipt returned：`1`" in via_slash
    assert not tool.metadata.requires_confirmation
