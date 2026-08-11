from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.evolution.stable_remote_finalization_deliveries import (
    EvolutionStableRemoteFinalizationDeliveryService,
)
from naumi_agent.evolution.stable_remote_finalization_delivery_worker import (
    EvolutionStableRemoteFinalizationDeliveryWorker,
    EvolutionStableRemoteFinalizationDeliveryWorkerPolicy,
    EvolutionStableRemoteFinalizationTransportError,
    LocalStableRemoteFinalizationInstallationTransport,
)
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionStableRemoteFinalizationTool
from tests.unit.test_evolution_stable_remote_finalization_deliveries import _delivery


def _worker(*, fixture, service, store, transport, **policy):
    return EvolutionStableRemoteFinalizationDeliveryWorker(
        service=EvolutionStableRemoteFinalizationDeliveryService(
            finalization_service=service,
            store=store,
            clock=fixture.data.clock,
        ),
        store=store,
        transport=transport,
        policy=EvolutionStableRemoteFinalizationDeliveryWorkerPolicy(
            interval_seconds=0.1,
            max_empty_backoff_seconds=1,
            max_failure_backoff_seconds=1,
            claim_lease_seconds=3,
            ack_timeout_seconds=0.2,
            retry_base_seconds=0.1,
            retry_max_seconds=1,
            shutdown_drain_seconds=1,
            jitter_ratio=0,
            **policy,
        ),
        owner_id="test-stable-finalization-worker",
        clock=fixture.data.clock,
        random_value=lambda: 0.5,
    )


@pytest.mark.asyncio
async def test_worker_delivers_real_local_installation_ack(tmp_path: Path) -> None:
    fixture, service, store, delivery, credential, journal = await _delivery(tmp_path)
    transport = LocalStableRemoteFinalizationInstallationTransport(
        journal=journal,
        trust_policy=fixture.policy,
        credential=credential,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    worker = _worker(fixture=fixture, service=service, store=store, transport=transport)

    result = await worker.run_once()

    assert result.claimed == result.acknowledged == 1
    assert result.failures == result.retry_scheduled == result.dead_lettered == 0
    view = await store.get(delivery.package.delivery_id)
    assert view is not None
    assert view.latest_event.state == "acknowledged"
    assert view.ack is not None
    assert view.ack.payload.durable_journal_written
    assert not view.ack.payload.writer_executed
    assert worker.snapshot().acknowledged_count == 1


@pytest.mark.asyncio
async def test_worker_retry_budget_ends_in_non_claimable_dead_letter(
    tmp_path: Path,
) -> None:
    fixture, service, store, delivery, _, _ = await _delivery(tmp_path)

    class OfflineTransport:
        async def receive(self, _package):
            raise EvolutionStableRemoteFinalizationTransportError(
                "installation_temporarily_offline", "offline"
            )

    worker = _worker(
        fixture=fixture,
        service=service,
        store=store,
        transport=OfflineTransport(),
        max_attempts=2,
    )
    first = await worker.run_once()
    assert first.retry_scheduled == 1
    fixture.data.clock.value += timedelta(seconds=1)

    second = await worker.run_once()

    assert second.dead_lettered == 1
    view = await store.get(delivery.package.delivery_id)
    assert view is not None
    assert view.latest_event.state == "dead_letter"
    assert view.latest_event.attempt_count == 2
    assert view.latest_event.failure_code == "installation_temporarily_offline"
    fixture.data.clock.value += timedelta(days=1)
    assert await store.claim(owner_id="unexpected", now=fixture.data.clock().isoformat()) is None


@pytest.mark.asyncio
async def test_worker_ack_timeout_is_persisted_for_retry(tmp_path: Path) -> None:
    fixture, service, store, delivery, _, _ = await _delivery(tmp_path)

    class HangingTransport:
        async def receive(self, _package):
            await asyncio.sleep(60)

    worker = _worker(
        fixture=fixture,
        service=service,
        store=store,
        transport=HangingTransport(),
    )

    result = await worker.run_once()

    assert result.retry_scheduled == 1
    assert result.failure_codes == ("stable_remote_delivery_ack_timeout",)
    view = await store.get(delivery.package.delivery_id)
    assert view is not None
    assert view.latest_event.state == "queued"
    assert view.latest_event.failure_code == "stable_remote_delivery_ack_timeout"


@pytest.mark.asyncio
async def test_worker_permanent_transport_failure_dead_letters_immediately(
    tmp_path: Path,
) -> None:
    fixture, service, store, delivery, _, _ = await _delivery(tmp_path)

    class WrongMemberTransport:
        async def receive(self, _package):
            raise EvolutionStableRemoteFinalizationTransportError(
                "installation_member_mismatch",
                "wrong member",
                retryable=False,
            )

    worker = _worker(
        fixture=fixture,
        service=service,
        store=store,
        transport=WrongMemberTransport(),
    )

    result = await worker.run_once()

    assert result.dead_lettered == 1
    assert result.retry_scheduled == 0
    view = await store.get(delivery.package.delivery_id)
    assert view is not None and view.latest_event.state == "dead_letter"
    assert view.latest_event.attempt_count == 1


@pytest.mark.asyncio
async def test_worker_shutdown_drains_in_flight_ack(tmp_path: Path) -> None:
    fixture, service, store, delivery, credential, journal = await _delivery(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    local = LocalStableRemoteFinalizationInstallationTransport(
        journal=journal,
        trust_policy=fixture.policy,
        credential=credential,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )

    class ControlledTransport:
        async def receive(self, package):
            entered.set()
            await release.wait()
            return await local.receive(package)

    worker = _worker(
        fixture=fixture,
        service=service,
        store=store,
        transport=ControlledTransport(),
    )
    assert worker.start()
    assert worker.wake()
    await asyncio.wait_for(entered.wait(), timeout=1)
    stop_task = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    release.set()

    assert await stop_task
    view = await store.get(delivery.package.delivery_id)
    assert view is not None and view.latest_event.state == "acknowledged"
    snapshot = worker.snapshot()
    assert snapshot.state == "stopped"
    assert snapshot.forced_shutdown_count == 0


@pytest.mark.asyncio
async def test_worker_agent_tool_and_slash_share_engine_backend(tmp_path: Path) -> None:
    fixture, service, store, _, credential, journal = await _delivery(tmp_path)
    delivery_service = EvolutionStableRemoteFinalizationDeliveryService(
        finalization_service=service,
        store=store,
        clock=fixture.data.clock,
    )
    worker = _worker(
        fixture=fixture,
        service=service,
        store=store,
        transport=LocalStableRemoteFinalizationInstallationTransport(
            journal=journal,
            trust_policy=fixture.policy,
            credential=credential,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        ),
    )

    class Engine:
        evolution_stable_remote_finalization_service = service
        evolution_stable_remote_finalization_delivery_service = delivery_service
        evolution_stable_remote_finalization_delivery_store = store
        evolution_stable_remote_finalization_delivery_worker = worker

        async def run_stable_remote_finalization_delivery_once(self):
            return await worker.run_once()

        def stable_remote_finalization_delivery_worker_snapshot(self):
            return worker.snapshot()

    engine = Engine()
    tool = EvolutionStableRemoteFinalizationTool(engine)
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
                content=await registered.execute(**registered.parse_arguments(call.arguments)),
            )

    direct = await tool.execute(action="inspect-delivery-worker")
    assert "**stopped**" in direct
    via_slash = strip_ansi(
        await execute_slash_command(
            SlashEngine(),
            "/evolution stable-remote-finalization run-delivery-worker",
        )
    )
    assert "**本轮完成**" in via_slash
    assert "ACK：`1`" in via_slash
