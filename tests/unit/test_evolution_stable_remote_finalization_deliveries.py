from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.evolution.stable_remote_finalization_deliveries import (
    EvolutionStableRemoteFinalizationDeliveryError,
    EvolutionStableRemoteFinalizationDeliveryService,
    EvolutionStableRemoteFinalizationDeliveryStore,
    EvolutionStableRemoteFinalizationTargetJournal,
    decode_stable_remote_finalization_delivery_ack,
    decode_stable_remote_finalization_delivery_package,
    encode_stable_remote_finalization_delivery_ack,
    encode_stable_remote_finalization_delivery_package,
)
from naumi_agent.evolution.stable_remote_finalizations import (
    EvolutionStableRemoteFinalizationError,
    EvolutionStableRemoteFinalizationService,
    EvolutionStableRemoteFinalizationStore,
    encode_stable_remote_finalization_submission,
)
from naumi_agent.release.installation_keys import ReleaseInstallationKeyError
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionStableRemoteFinalizationTool
from tests.unit.test_evolution_stable_remote_finalization_authorizations import (
    _authorization_fixture,
)


def _service(fixture):
    return EvolutionStableRemoteFinalizationService(
        workspace_root=fixture.data.fixture.root,
        authorization_service=fixture.make_service(),
        rollout_key_service=fixture.rollout_key,
        trust_policy_path=fixture.policy_path,
        store=EvolutionStableRemoteFinalizationStore(fixture.db_path),
        clock=fixture.data.clock,
    )


async def _delivery(tmp_path: Path):
    fixture = await _authorization_fixture(tmp_path)
    authorization = await fixture.make_service().issue(
        probe_receipt_id=fixture.probe.receipt.receipt_id,
        validity_seconds=120,
    )
    service = _service(fixture)
    package = await service.prepare(
        authorization_id=authorization.envelope.authorization.authorization_id
    )
    store = EvolutionStableRemoteFinalizationDeliveryStore(fixture.db_path)
    delivery = await store.enqueue(
        package, enqueued_at=fixture.data.clock().isoformat()
    )
    credential = fixture.data.credential
    journal = EvolutionStableRemoteFinalizationTargetJournal(
        fixture.data.fixture.store.release_root
    )
    return fixture, service, store, delivery, credential, journal


def _artifact(text: str, heading: str) -> str:
    return "".join(
        line.strip()
        for line in text.split(heading, maxsplit=1)[1].splitlines()
        if re.fullmatch(r"[A-Za-z0-9+/=]+", line.strip())
    )


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_delivery_claim_ack_execute_and_complete_real_store(tmp_path: Path) -> None:
    fixture, service, store, delivery, credential, journal = await _delivery(tmp_path)
    encoded = encode_stable_remote_finalization_delivery_package(delivery.package)
    assert decode_stable_remote_finalization_delivery_package(encoded) == delivery.package
    claims = await asyncio.gather(*(
        EvolutionStableRemoteFinalizationDeliveryStore(fixture.db_path).claim(
            owner_id=f"worker-{index}",
            now=fixture.data.clock().isoformat(),
        )
        for index in range(12)
    ))
    winners = [item for item in claims if item is not None]
    assert len(winners) == 1
    assert winners[0].latest_event.claim_epoch == 1

    ack = journal.receive(
        package=delivery.package,
        trust_policy=fixture.policy,
        credential=credential,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    ack_encoded = encode_stable_remote_finalization_delivery_ack(ack)
    assert decode_stable_remote_finalization_delivery_ack(ack_encoded) == ack
    fixture.data.clock.value += timedelta(seconds=1)
    assert journal.receive(
        package=delivery.package,
        trust_policy=fixture.policy,
        credential=credential,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    ) == ack
    acknowledged = await store.acknowledge(
        delivery_id=delivery.package.delivery_id,
        ack=ack,
        credential=credential,
        now=fixture.data.clock().isoformat(),
    )
    assert acknowledged.latest_event.state == "acknowledged"
    assert not ack.payload.writer_executed

    submission = journal.execute(
        package=delivery.package,
        trust_policy=fixture.policy,
        credential=credential,
        release_slot_store=fixture.data.fixture.store,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    assert journal.execute(
        package=delivery.package,
        trust_policy=fixture.policy,
        credential=credential,
        release_slot_store=fixture.data.fixture.store,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    ) == submission
    final = await service.ingest(
        grant_id=delivery.package.execution_package.grant.grant_id,
        submission_base64=encode_stable_remote_finalization_submission(submission),
    )
    completed = await store.complete(
        delivery_id=delivery.package.delivery_id,
        receipt=final.receipt,
        completed_at=fixture.data.clock().isoformat(),
    )
    assert completed.latest_event.state == "completed"
    assert completed.latest_event.sequence == 4
    assert (await EvolutionStableRemoteFinalizationDeliveryStore(
        fixture.db_path
    ).get(delivery.package.delivery_id)) == completed


@pytest.mark.asyncio
async def test_delivery_retry_backoff_and_expired_takeover(tmp_path: Path) -> None:
    fixture, _, store, delivery, _, _ = await _delivery(tmp_path)
    fixture.data.clock.value += timedelta(seconds=1)
    repeated = await store.enqueue(
        delivery.package.execution_package,
        enqueued_at=fixture.data.clock().isoformat(),
    )
    assert repeated.package == delivery.package
    claimed = await store.claim(
        owner_id="first", now=fixture.data.clock().isoformat(), lease_seconds=3
    )
    assert claimed is not None
    queued = await store.retry(
        delivery_id=delivery.package.delivery_id,
        owner_id="first",
        claim_epoch=1,
        failure_code="transport_offline",
        now=fixture.data.clock().isoformat(),
    )
    assert queued.latest_event.state == "queued"
    assert await store.claim(
        owner_id="early", now=fixture.data.clock().isoformat()
    ) is None
    fixture.data.clock.value += timedelta(seconds=5)
    second = await store.claim(
        owner_id="second", now=fixture.data.clock().isoformat(), lease_seconds=3
    )
    assert second is not None
    fixture.data.clock.value += timedelta(seconds=4)
    takeover = await store.claim(
        owner_id="third", now=fixture.data.clock().isoformat(), lease_seconds=3
    )
    assert takeover is not None
    assert takeover.latest_event.claim_epoch == 3
    with pytest.raises(EvolutionStableRemoteFinalizationDeliveryError) as fenced:
        await store.retry(
            delivery_id=delivery.package.delivery_id,
            owner_id="second",
            claim_epoch=2,
            failure_code="stale_owner",
            now=fixture.data.clock().isoformat(),
        )
    assert fenced.value.code == "stable_remote_delivery_claim_fenced"


@pytest.mark.asyncio
async def test_delivery_event_chain_tamper_fails_closed(tmp_path: Path) -> None:
    fixture, _, store, delivery, _, _ = await _delivery(tmp_path)
    await store.claim(owner_id="worker", now=fixture.data.clock().isoformat())
    with sqlite3.connect(fixture.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_remote_finalization_delivery_events "
            "SET event_json = replace(event_json, '\"attempt_count\":1', "
            "'\"attempt_count\":9') WHERE delivery_id = ? AND sequence = 2",
            (delivery.package.delivery_id,),
        )
    with pytest.raises((ValueError, EvolutionStableRemoteFinalizationDeliveryError)):
        await store.get(delivery.package.delivery_id)


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_delivery_get_verifies_one_consistent_database_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from naumi_agent.evolution import stable_remote_finalization_deliveries as module

    fixture, _, store, delivery, _, _ = await _delivery(tmp_path)
    original_rows_with_chain = module._rows_with_chain
    writer_task: asyncio.Task | None = None

    async def read_snapshot_then_start_writer(db, delivery_id):
        nonlocal writer_task
        rows = await original_rows_with_chain(db, delivery_id)
        writer_task = asyncio.create_task(
            store.claim(
                owner_id="concurrent-worker",
                now=fixture.data.clock().isoformat(),
            )
        )
        await asyncio.sleep(0.05)
        return rows

    monkeypatch.setattr(module, "_rows_with_chain", read_snapshot_then_start_writer)
    snapshot = await store.get(delivery.package.delivery_id)
    assert writer_task is not None
    claimed = await writer_task

    assert snapshot is not None
    assert snapshot.latest_event.state == "queued"
    assert claimed is not None
    assert claimed.latest_event.state == "in_flight"


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_late_recovery_only_signs_existing_writer_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture, service, store, delivery, credential, journal = await _delivery(tmp_path)
    await store.claim(owner_id="worker", now=fixture.data.clock().isoformat())
    ack = journal.receive(
        package=delivery.package,
        trust_policy=fixture.policy,
        credential=credential,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    await store.acknowledge(
        delivery_id=delivery.package.delivery_id,
        ack=ack,
        credential=credential,
        now=fixture.data.clock().isoformat(),
    )
    original = fixture.data.key_service.sign_remote_finalization_result

    def _unavailable(**_kwargs):
        raise ReleaseInstallationKeyError(
            "release_installation_key_temporarily_unavailable", "keyring offline"
        )

    monkeypatch.setattr(
        fixture.data.key_service, "sign_remote_finalization_result", _unavailable
    )
    with pytest.raises(EvolutionStableRemoteFinalizationError):
        journal.execute(
            package=delivery.package,
            trust_policy=fixture.policy,
            credential=credential,
            release_slot_store=fixture.data.fixture.store,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        )
    assert fixture.data.fixture.store.get_stable_member_finalization(
        delivery.package.execution_package.grant.authorization_id
    ) is not None
    with sqlite3.connect(journal.db_path) as db:
        assert db.execute(
            "SELECT state FROM stable_finalization_delivery_journal WHERE delivery_id = ?",
            (delivery.package.delivery_id,),
        ).fetchone()[0] == "writer_committed"
    fixture.data.clock.value += timedelta(seconds=121)
    monkeypatch.setattr(
        fixture.data.key_service, "sign_remote_finalization_result", original
    )
    recovered = journal.execute(
        package=delivery.package,
        trust_policy=fixture.policy,
        credential=credential,
        release_slot_store=fixture.data.fixture.store,
        installation_key_service=fixture.data.key_service,
        recover_existing_only=True,
        clock=fixture.data.clock,
    )
    with pytest.raises(EvolutionStableRemoteFinalizationError) as ordinary:
        await service.ingest(
            grant_id=delivery.package.execution_package.grant.grant_id,
            submission_base64=encode_stable_remote_finalization_submission(recovered),
        )
    assert ordinary.value.code == "stable_remote_finalization_grant_expired"
    late = await service.ingest_late_recovery(
        grant_id=delivery.package.execution_package.grant.grant_id,
        submission_base64=encode_stable_remote_finalization_submission(recovered),
    )
    completed = await store.complete(
        delivery_id=delivery.package.delivery_id,
        receipt=late.receipt,
        completed_at=fixture.data.clock().isoformat(),
    )
    assert completed.latest_event.state == "completed"


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_late_recovery_refuses_to_execute_missing_writer(tmp_path: Path) -> None:
    fixture, _, _, delivery, credential, journal = await _delivery(tmp_path)
    journal.receive(
        package=delivery.package,
        trust_policy=fixture.policy,
        credential=credential,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    fixture.data.clock.value += timedelta(seconds=121)
    with pytest.raises(EvolutionStableRemoteFinalizationError) as missing:
        journal.execute(
            package=delivery.package,
            trust_policy=fixture.policy,
            credential=credential,
            release_slot_store=fixture.data.fixture.store,
            installation_key_service=fixture.data.key_service,
            recover_existing_only=True,
            clock=fixture.data.clock,
        )
    assert missing.value.code == "stable_remote_finalization_recovery_fact_missing"


@pytest.mark.skipif(__import__("os").name == "nt", reason="slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_delivery_agent_tool_and_slash_share_backend(tmp_path: Path) -> None:
    fixture, service, store, delivery, _, _ = await _delivery(tmp_path)
    delivery_service = EvolutionStableRemoteFinalizationDeliveryService(
        finalization_service=service,
        store=store,
        clock=fixture.data.clock,
    )
    engine = type("Engine", (), {
        "evolution_stable_remote_finalization_service": service,
        "evolution_stable_remote_finalization_delivery_service": delivery_service,
        "evolution_stable_remote_finalization_delivery_store": store,
        "evolution_release_population_snapshot_store": (
            fixture.data.fixture.data["population_store"]
        ),
        "evolution_release_slot_store": fixture.data.fixture.store,
        "release_installation_key_service": fixture.data.key_service,
        "evolution_release_rollout_control_trust_policy_path": fixture.policy_path,
    })()
    tool = EvolutionStableRemoteFinalizationTool(engine)
    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
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

    grant_id = delivery.package.execution_package.grant.grant_id
    direct = await tool.execute(action="queue-delivery", grant_id=grant_id)
    assert "Remote Stable Finalization Delivery" in direct, direct
    queued = strip_ansi(await execute_slash_command(
        _SlashEngine(), f"/evolution stable-remote-finalization queue-delivery {grant_id}"
    ))
    delivery_id = re.search(r"evstableremotedelivery_[0-9a-f]{24}", queued)
    assert delivery_id is not None, queued
    claimed = strip_ansi(await execute_slash_command(
        _SlashEngine(), "/evolution stable-remote-finalization claim-delivery cli-worker"
    ))
    package_base64 = _artifact(claimed, "Delivery Package Base64")
    received = strip_ansi(await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-finalization receive-delivery-local "
        + package_base64,
    ))
    ack_base64 = _artifact(received, "Delivery ACK Base64")
    acknowledged = strip_ansi(await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-finalization ack-delivery "
        f"{delivery_id.group()} {ack_base64}",
    ))
    assert "**acknowledged**" in acknowledged
    executed = strip_ansi(await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-finalization execute-delivery-local "
        + package_base64,
    ))
    submission_base64 = _artifact(executed, "Finalization Submission Base64")
    completed = strip_ansi(await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-finalization ingest-delivery "
        f"{delivery_id.group()} {submission_base64}",
    ))
    assert "**completed**" in completed
    assert not tool.metadata.requires_confirmation
