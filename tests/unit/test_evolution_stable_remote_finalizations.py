from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

import naumi_agent.evolution as evolution_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
)
from naumi_agent.evolution.stable_remote_finalizations import (
    EvolutionStableRemoteFinalizationError,
    EvolutionStableRemoteFinalizationExecutionPackage,
    EvolutionStableRemoteFinalizationService,
    EvolutionStableRemoteFinalizationStore,
    decode_stable_remote_finalization_execution_package,
    encode_stable_remote_finalization_execution_package,
    encode_stable_remote_finalization_submission,
    execute_stable_remote_finalization,
)
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionStableRemoteFinalizationTool
from tests.unit.test_evolution_stable_remote_finalization_authorizations import (
    _authorization_fixture,
)


def _service(fixture):
    authorization_service = fixture.make_service()
    return EvolutionStableRemoteFinalizationService(
        workspace_root=fixture.data.fixture.root,
        authorization_service=authorization_service,
        rollout_key_service=fixture.rollout_key,
        trust_policy_path=fixture.policy_path,
        store=EvolutionStableRemoteFinalizationStore(fixture.db_path),
        clock=fixture.data.clock,
    )


def _rendered_base64(value: str, heading: str) -> str:
    tail = value.split(heading, maxsplit=1)[1]
    return "".join(
        line.strip()
        for line in tail.splitlines()
        if re.fullmatch(r"[A-Za-z0-9+/=]+", line.strip())
    )


async def _package(fixture):
    authorization = await fixture.make_service().issue(
        probe_receipt_id=fixture.probe.receipt.receipt_id,
        validity_seconds=120,
    )
    item_id = authorization.envelope.authorization.authorization_id
    services = tuple(_service(fixture) for _ in range(4))
    packages = await asyncio.gather(*(
        services[index % len(services)].prepare(authorization_id=item_id)
        for index in range(8)
    ))
    assert len({item.grant.grant_id for item in packages}) == 1
    return services[0], packages[0]


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_finalization_real_store_cas_signed_ingest_and_recovery(
    tmp_path: Path,
) -> None:
    fixture = await _authorization_fixture(tmp_path)
    assert (
        evolution_api.EvolutionStableRemoteFinalizationService
        is EvolutionStableRemoteFinalizationService
    )
    service, package = await _package(fixture)
    encoded = encode_stable_remote_finalization_execution_package(package)
    assert decode_stable_remote_finalization_execution_package(encoded) == package
    assert str(fixture.data.fixture.root) not in encoded

    active_before = fixture.data.fixture.store.active()
    submissions = await asyncio.gather(*(
        asyncio.to_thread(
            execute_stable_remote_finalization,
            package=package,
            trust_policy=fixture.policy,
            credential=fixture.data.credential,
            release_slot_store=fixture.data.fixture.store,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        )
        for _ in range(8)
    ))
    assert len({item.result.result_id for item in submissions}) == 1
    submission = submissions[0]
    finalization = submission.result.release_finalization
    assert finalization.active_pointer == active_before
    assert finalization.expected_pointer_cas_satisfied
    assert finalization.authority.kind == (
        "evolution_stable_remote_finalization_authorization"
    )
    assert fixture.data.fixture.store.active() == active_before

    views = await asyncio.gather(*(
        service.ingest(
            grant_id=package.grant.grant_id,
            submission_base64=encode_stable_remote_finalization_submission(submission),
        )
        for _ in range(8)
    ))
    assert len({item.receipt.receipt_id for item in views}) == 1
    view = views[0]
    assert view.stable_member_completion_fact
    assert view.current_control_plane_authority
    assert view.remote_active_pointer_current_unverified
    assert not view.stable_population_completion_authority
    assert not view.promotion_authority
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_stable_remote_finalization_grants"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_stable_remote_finalization_receipts"
        ).fetchone()[0] == 1


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_finalization_fences_pointer_change_before_writer(
    tmp_path: Path,
) -> None:
    fixture = await _authorization_fixture(tmp_path)
    _, package = await _package(fixture)
    fixture.data.fixture.store.rollback(
        activated_at=(fixture.data.clock.value + timedelta(seconds=1)).isoformat()
    )
    fixture.data.clock.value += timedelta(seconds=2)
    with pytest.raises(EvolutionStableRemoteFinalizationError) as error:
        execute_stable_remote_finalization(
            package=package,
            trust_policy=fixture.policy,
            credential=fixture.data.credential,
            release_slot_store=fixture.data.fixture.store,
            installation_key_service=fixture.data.key_service,
            clock=fixture.data.clock,
        )
    assert error.value.code == "stable_remote_finalization_release_store_mismatch"
    assert (
        fixture.data.fixture.store.get_stable_member_finalization(
            package.grant.authorization_id
        )
        is None
    )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_finalization_recovers_writer_before_signature_after_pointer_moves(
    tmp_path: Path,
) -> None:
    fixture = await _authorization_fixture(tmp_path)
    _, package = await _package(fixture)
    first = execute_stable_remote_finalization(
        package=package,
        trust_policy=fixture.policy,
        credential=fixture.data.credential,
        release_slot_store=fixture.data.fixture.store,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    fixture.data.fixture.store.rollback(
        activated_at=(fixture.data.clock.value + timedelta(seconds=1)).isoformat()
    )
    fixture.data.clock.value += timedelta(seconds=2)
    recovered = execute_stable_remote_finalization(
        package=package,
        trust_policy=fixture.policy,
        credential=fixture.data.credential,
        release_slot_store=fixture.data.fixture.store,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    assert recovered.result == first.result
    assert recovered.result.release_finalization == (
        fixture.data.fixture.store.get_stable_member_finalization(
            package.grant.authorization_id
        )
    )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_finalization_ingest_fails_closed_after_kill_switch(
    tmp_path: Path,
) -> None:
    fixture = await _authorization_fixture(tmp_path)
    service, package = await _package(fixture)
    submission = execute_stable_remote_finalization(
        package=package,
        trust_policy=fixture.policy,
        credential=fixture.data.credential,
        release_slot_store=fixture.data.fixture.store,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    fixture.data.clock.value += timedelta(seconds=1)
    control = EvolutionRevalidationRolloutControlService(
        workspace_root=fixture.data.fixture.root,
        store=fixture.control_store,
        control_plane_key_provider=lambda: b"remote-finalization-control" * 2,
    )
    await control.pause(
        reason_code="remote_finalization_ingest_pause",
        actor=EvolutionRevalidationRolloutControlActor.MONITOR,
        changed_at=fixture.data.clock().isoformat(),
    )
    with pytest.raises(EvolutionStableRemoteFinalizationError) as error:
        await service.ingest(
            grant_id=package.grant.grant_id,
            submission_base64=encode_stable_remote_finalization_submission(submission),
        )
    assert error.value.code == "stable_remote_finalization_sources_changed"
    assert await service.store.get_receipt_by_grant(package.grant.grant_id) is None
    with pytest.raises(EvolutionStableRemoteFinalizationError) as stale:
        await service.prepare(
            authorization_id=package.grant.authorization_id
        )
    assert stale.value.code == "stable_remote_finalization_grant_not_current"


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_finalization_fences_consume_to_grant_control_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _authorization_fixture(tmp_path)
    authorization = await fixture.make_service().issue(
        probe_receipt_id=fixture.probe.receipt.receipt_id,
        validity_seconds=120,
    )
    service = _service(fixture)
    original = service.store.create_package

    async def _pause_before_grant_writer(**kwargs):
        fixture.data.clock.value += timedelta(seconds=1)
        control = EvolutionRevalidationRolloutControlService(
            workspace_root=fixture.data.fixture.root,
            store=fixture.control_store,
            control_plane_key_provider=lambda: b"remote-finalization-control" * 2,
        )
        await control.pause(
            reason_code="remote_finalization_grant_race",
            actor=EvolutionRevalidationRolloutControlActor.MONITOR,
            changed_at=fixture.data.clock().isoformat(),
        )
        return await original(**kwargs)

    monkeypatch.setattr(service.store, "create_package", _pause_before_grant_writer)
    auth_id = authorization.envelope.authorization.authorization_id
    with pytest.raises(EvolutionStableRemoteFinalizationError) as error:
        await service.prepare(authorization_id=auth_id)
    assert error.value.code == "stable_remote_finalization_control_changed"
    assert await service.store.get_package_by_authorization(auth_id) is None
    assert await service.authorization_service.store.consumption(auth_id) is not None


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_finalization_rejects_tampered_package_and_result(
    tmp_path: Path,
) -> None:
    fixture = await _authorization_fixture(tmp_path)
    service, package = await _package(fixture)
    cross_domain = package.model_dump(mode="json")
    cross_domain["signature"] = package.authorization.signature.model_dump(
        mode="json"
    )
    with pytest.raises(ValueError):
        EvolutionStableRemoteFinalizationExecutionPackage.model_validate(
            cross_domain
        )
    raw = package.model_dump(mode="json")
    raw["signature"]["signature_base64"] = base64.b64encode(b"z" * 64).decode()
    raw["signature"]["signature_sha256"] = __import__("hashlib").sha256(
        b"z" * 64
    ).hexdigest()
    with pytest.raises(ValueError):
        EvolutionStableRemoteFinalizationExecutionPackage.model_validate(raw)

    submission = execute_stable_remote_finalization(
        package=package,
        trust_policy=fixture.policy,
        credential=fixture.data.credential,
        release_slot_store=fixture.data.fixture.store,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    probe_replay = submission.model_dump(mode="json")
    probe_replay["signature"] = (
        fixture.probe.receipt.submission.signature.model_dump(mode="json")
    )
    with pytest.raises(ValueError):
        type(submission).model_validate(probe_replay)
    encoded = encode_stable_remote_finalization_submission(submission)
    decoded = json.loads(base64.b64decode(encoded))
    decoded["result"]["release_root_sha256"] = "f" * 64
    forged = base64.b64encode(
        json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    with pytest.raises(EvolutionStableRemoteFinalizationError) as error:
        await service.ingest(
            grant_id=package.grant.grant_id,
            submission_base64=forged,
        )
    assert error.value.code == "stable_remote_finalization_submission_invalid"


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_finalization_shared_agent_tool_and_slash_flow(
    tmp_path: Path,
) -> None:
    fixture = await _authorization_fixture(tmp_path)
    authorization = await fixture.make_service().issue(
        probe_receipt_id=fixture.probe.receipt.receipt_id,
        validity_seconds=120,
    )
    service = _service(fixture)
    engine = type("Engine", (), {
        "evolution_stable_remote_finalization_service": service,
        "evolution_release_population_snapshot_store": (
            fixture.data.fixture.data["population_store"]
        ),
        "evolution_release_slot_store": fixture.data.fixture.store,
        "release_installation_key_service": fixture.data.key_service,
        "evolution_release_rollout_control_trust_policy_path": fixture.policy_path,
    })()
    tool = EvolutionStableRemoteFinalizationTool(engine)
    assert not tool.metadata.requires_confirmation
    assert tool.metadata.concurrency_safe
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

    auth_id = authorization.envelope.authorization.authorization_id
    prepared = strip_ansi(await execute_slash_command(
        _SlashEngine(),
        f"/evolution stable-remote-finalization prepare {auth_id}",
    ))
    grant = re.search(r"evstableremotefinalgrant_[0-9a-f]{24}", prepared)
    assert grant is not None
    exported = strip_ansi(await execute_slash_command(
        _SlashEngine(),
        f"/evolution stable-remote-finalization export {grant.group()}",
    ))
    package_base64 = _rendered_base64(exported, "Execution Package Base64")
    assert decode_stable_remote_finalization_execution_package(package_base64)

    executed = strip_ansi(await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-finalization execute-local " + package_base64,
    ))
    submission_base64 = _rendered_base64(
        executed, "Finalization Submission Base64"
    )
    ingested = strip_ansi(await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-finalization ingest "
        f"{grant.group()} {submission_base64}",
    ))
    receipt = re.search(r"evstableremotefinalreceipt_[0-9a-f]{24}", ingested)
    assert receipt is not None
    inspected = strip_ansi(await execute_slash_command(
        _SlashEngine(),
        f"/evolution stable-remote-finalization inspect {receipt.group()}",
    ))
    assert "Completion fact：`true`" in inspected
