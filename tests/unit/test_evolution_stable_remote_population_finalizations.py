from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import naumi_agent.evolution as evolution_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
)
from naumi_agent.evolution.stable_remote_finalization_authorizations import (
    EvolutionStableRemoteFinalizationAuthorizationService,
    EvolutionStableRemoteFinalizationAuthorizationStore,
)
from naumi_agent.evolution.stable_remote_finalizations import (
    EvolutionStableRemoteFinalizationError,
    EvolutionStableRemoteFinalizationService,
    EvolutionStableRemoteFinalizationStore,
    encode_stable_remote_finalization_submission,
    execute_stable_remote_finalization,
)
from naumi_agent.evolution.stable_remote_population_finalizations import (
    EvolutionStableRemotePopulationFinalizationError,
    EvolutionStableRemotePopulationFinalizationReceipt,
    EvolutionStableRemotePopulationFinalizationService,
    EvolutionStableRemotePopulationFinalizationStore,
    render_stable_remote_population_finalization,
)
from naumi_agent.evolution.stable_remote_population_finalizations import (
    _bounded as _bounded_population_receipt,
)
from naumi_agent.evolution.stable_remote_readiness_probes import (
    EvolutionStableRemoteReadinessProbeService,
    EvolutionStableRemoteReadinessProbeStore,
    encode_stable_remote_readiness_probe_submission,
    execute_stable_remote_readiness_probe,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.release.installation_keys import ReleaseInstallationKeyService
from naumi_agent.release.slots import ReleaseSlotStore
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStableRemotePopulationFinalizationTool,
)
from tests.unit.test_evolution_stable_population_completions import T0
from tests.unit.test_evolution_stable_remote_finalization_authorizations import (
    _authorization_fixture,
)
from tests.unit.test_evolution_stable_remote_finalizations import _package
from tests.unit.test_evolution_stable_remote_readiness_claims import (
    _assertion,
    _private_key_for_credential,
)
from tests.unit.test_evolution_stable_remote_readiness_claims import (
    _service as _claim_service,
)
from tests.unit.test_evolution_stable_remote_readiness_probes import _MemoryBackend
from tests.unit.test_release_population_registry import _policy
from tests.unit.test_release_slots import _bundle


def _target_store(tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "target"
    root.mkdir(parents=True)
    store = ReleaseSlotStore(root / "installed")
    baseline = store.install(
        _bundle(root, version="1.0.0", output_name="rollback-baseline"),
        installed_at="2026-08-11T01:00:00+00:00",
    )
    store.verify_bootable(
        baseline.slot_id,
        checked_at="2026-08-11T01:01:00+00:00",
    )
    prior = store.activate(
        baseline.slot_id,
        activated_at="2026-08-11T01:02:00+00:00",
    )
    candidate = store.install(
        _bundle(root, version="1.2.3", output_name="stable-candidate"),
        installed_at="2026-08-11T02:00:00+00:00",
    )
    store.verify_bootable(
        candidate.slot_id,
        checked_at="2026-08-11T02:01:00+00:00",
    )
    active = store.activate(
        candidate.slot_id,
        activated_at="2026-08-11T02:02:00+00:00",
    )
    return SimpleNamespace(
        root=root,
        store=store,
        baseline=baseline,
        prior=prior,
        candidate=candidate,
        active=active,
    )


async def _finish_first_member(fixture, tmp_path: Path):
    base = fixture.data.fixture
    member_id = base.completion.receipt.installation_member_ids[0]
    credential = next(
        item
        for item in base.data["snapshot"].payload.credentials
        if item.payload.member_id == member_id
    )
    target = _target_store(tmp_path)
    claim_service = _claim_service(base, fixture.data.clock)
    claim_challenge = await claim_service.issue_challenge(
        completion_receipt_id=base.completion.receipt.receipt_id,
        installation_member_id=member_id,
        validity_seconds=300,
    )
    assertion = _assertion(target, claim_challenge)
    claim = await claim_service.ingest(
        challenge_id=claim_challenge.challenge_id,
        assertion_base64=base64.b64encode(assertion.canonical_bytes()).decode(),
        signature_base64=base64.b64encode(
            _private_key_for_credential(credential).sign(assertion.canonical_bytes())
        ).decode(),
    )
    probe_service = EvolutionStableRemoteReadinessProbeService(
        claim_service=claim_service,
        store=EvolutionStableRemoteReadinessProbeStore(fixture.db_path),
        clock=fixture.data.clock,
        random_bytes=lambda size: b"q" * size,
    )
    challenge = await probe_service.prepare(
        claim_receipt_id=claim.receipt.receipt_id,
        validity_seconds=180,
    )
    key_service = ReleaseInstallationKeyService(
        target.store.release_root,
        backend=_MemoryBackend(),
        key_factory=lambda size: _private_key_for_credential(credential).private_bytes_raw(),
        clock=fixture.data.clock,
    )
    key_service.provision(channel="stable")
    probe_submission = execute_stable_remote_readiness_probe(
        challenge=challenge,
        credential=credential,
        release_slot_store=target.store,
        installation_key_service=key_service,
        clock=fixture.data.clock,
    )
    probe = await probe_service.ingest(
        challenge_id=challenge.challenge_id,
        submission_base64=encode_stable_remote_readiness_probe_submission(probe_submission),
    )
    authorization_service = EvolutionStableRemoteFinalizationAuthorizationService(
        workspace_root=base.root,
        probe_service=probe_service,
        control_store=fixture.control_store,
        rollout_key_service=fixture.rollout_key,
        trust_policy_path=fixture.policy_path,
        store=EvolutionStableRemoteFinalizationAuthorizationStore(fixture.db_path),
        clock=fixture.data.clock,
        random_bytes=lambda size: b"b" * size,
    )
    authorization = await authorization_service.issue(
        probe_receipt_id=probe.receipt.receipt_id,
        validity_seconds=120,
    )
    member_service = EvolutionStableRemoteFinalizationService(
        workspace_root=base.root,
        authorization_service=authorization_service,
        rollout_key_service=fixture.rollout_key,
        trust_policy_path=fixture.policy_path,
        store=EvolutionStableRemoteFinalizationStore(fixture.db_path),
        clock=fixture.data.clock,
    )
    package = await member_service.prepare(
        authorization_id=authorization.envelope.authorization.authorization_id
    )
    submission = execute_stable_remote_finalization(
        package=package,
        trust_policy=fixture.policy,
        credential=credential,
        release_slot_store=target.store,
        installation_key_service=key_service,
        clock=fixture.data.clock,
    )
    return await member_service.ingest(
        grant_id=package.grant.grant_id,
        submission_base64=encode_stable_remote_finalization_submission(submission),
    )


async def _population_fixture(tmp_path: Path, *, complete: bool = True):
    fixture = await _authorization_fixture(tmp_path / "control")
    member_service, package = await _package(fixture)
    submission = execute_stable_remote_finalization(
        package=package,
        trust_policy=fixture.policy,
        credential=fixture.data.credential,
        release_slot_store=fixture.data.fixture.store,
        installation_key_service=fixture.data.key_service,
        clock=fixture.data.clock,
    )
    second = await member_service.ingest(
        grant_id=package.grant.grant_id,
        submission_base64=encode_stable_remote_finalization_submission(submission),
    )
    first = None
    if complete:
        first = await _finish_first_member(fixture, tmp_path / "first-member")
    store = EvolutionStableRemotePopulationFinalizationStore(fixture.db_path)
    service = EvolutionStableRemotePopulationFinalizationService(
        workspace_root=fixture.data.fixture.root,
        population_store=fixture.data.fixture.data["population_store"],
        member_service=member_service,
        store=store,
    )
    return SimpleNamespace(
        fixture=fixture,
        member_service=member_service,
        first=first,
        second=second,
        store=store,
        service=service,
        snapshot=fixture.data.fixture.data["snapshot"],
    )


async def _finish_second_member_again(data):
    member_service, package = await _package(data.fixture)
    submission = execute_stable_remote_finalization(
        package=package,
        trust_policy=data.fixture.policy,
        credential=data.fixture.data.credential,
        release_slot_store=data.fixture.data.fixture.store,
        installation_key_service=data.fixture.data.key_service,
        clock=data.fixture.data.clock,
    )
    return await member_service.ingest(
        grant_id=package.grant.grant_id,
        submission_base64=encode_stable_remote_finalization_submission(submission),
    )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_population_finalization_real_two_member_concurrent_and_survives_grant_expiry(
    tmp_path: Path,
) -> None:
    data = await _population_fixture(tmp_path)
    services = tuple(
        EvolutionStableRemotePopulationFinalizationService(
            workspace_root=data.fixture.data.fixture.root,
            population_store=data.fixture.data.fixture.data["population_store"],
            member_service=data.member_service,
            store=EvolutionStableRemotePopulationFinalizationStore(data.fixture.db_path),
        )
        for _ in range(4)
    )
    views = await asyncio.gather(
        *(
            services[index % len(services)].complete(snapshot_id=data.snapshot.snapshot_id)
            for index in range(8)
        )
    )
    assert len({item.receipt.receipt_id for item in views}) == 1
    view = views[0]
    assert view.stable_population_finalization_fact
    assert view.stable_population_finalization_authority
    assert view.receipt.population_denominator == 2
    assert len(view.receipt.members) == 2
    assert not view.config_data_finalization_authority
    assert not view.promotion_authority
    assert (
        EvolutionStableRemotePopulationFinalizationReceipt.model_validate_json(
            view.receipt.model_dump_json()
        )
        == view.receipt
    )
    strict_payload = view.receipt.model_dump(mode="json")
    strict_payload["unknown_authority"] = True
    with pytest.raises(ValueError):
        EvolutionStableRemotePopulationFinalizationReceipt.model_validate(strict_payload)
    with pytest.raises(EvolutionStableRemotePopulationFinalizationError) as oversized:
        _bounded_population_receipt("x" * (16 * 1024 * 1024 + 1))
    assert oversized.value.code == "stable_remote_population_receipt_oversized"
    assert (
        evolution_api.EvolutionStableRemotePopulationFinalizationReceipt
        is EvolutionStableRemotePopulationFinalizationReceipt
    )
    with sqlite3.connect(data.fixture.db_path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM evolution_stable_remote_population_finalizations"
            ).fetchone()[0]
            == 1
        )

    data.fixture.data.clock.value += timedelta(seconds=301)
    expired_capabilities = await data.service.inspect(receipt_id=view.receipt.receipt_id)
    assert expired_capabilities.stable_population_finalization_authority

    tool = EvolutionStableRemotePopulationFinalizationTool(
        SimpleNamespace(evolution_stable_remote_population_finalization_service=data.service)
    )
    assert not tool.metadata.read_only
    assert not tool.metadata.destructive
    assert tool.metadata.concurrency_safe
    assert not tool.metadata.requires_confirmation
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

    slash = await execute_slash_command(
        SlashEngine(),
        "/evolution stable-remote-population-finalization inspect " + view.receipt.receipt_id,
    )
    assert strip_ansi(slash) == render_stable_remote_population_finalization(expired_capabilities)


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_population_finalization_rejects_missing_member(
    tmp_path: Path,
) -> None:
    data = await _population_fixture(tmp_path, complete=False)
    with pytest.raises(EvolutionStableRemotePopulationFinalizationError) as error:
        await data.service.complete(snapshot_id=data.snapshot.snapshot_id)
    assert error.value.code == "stable_remote_population_member_receipt_missing"


@pytest.mark.asyncio
async def test_member_receipt_reader_enforces_population_limit(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "population-limit.db"
    sqlite3.connect(db_path).close()
    store = EvolutionStableRemoteFinalizationStore(db_path)
    snapshot_id = "relpopsnapshot_" + "a" * 24
    assert await store.list_receipts_for_population_snapshot(snapshot_id) == ()
    payload = json.dumps(
        {
            "execution_package": {
                "authorization": {"authorization": {"population_snapshot_id": snapshot_id}}
            }
        }
    )
    with sqlite3.connect(db_path) as db:
        db.executemany(
            "INSERT INTO evolution_stable_remote_finalization_receipts "
            "(receipt_id, receipt_sha256, grant_id, authorization_id, "
            "receipt_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    f"receipt-{index}",
                    f"sha-{index}",
                    f"grant-{index}",
                    f"authorization-{index}",
                    payload,
                    "2026-08-11T03:00:00+00:00",
                )
                for index in range(10_001)
            ),
        )
        db.commit()

    with pytest.raises(EvolutionStableRemoteFinalizationError) as error:
        await store.list_receipts_for_population_snapshot(snapshot_id)
    assert error.value.code == "stable_remote_finalization_population_limit_exceeded"


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_population_finalization_dynamically_revokes_on_control_and_new_snapshot(
    tmp_path: Path,
) -> None:
    data = await _population_fixture(tmp_path)
    issued = await data.service.complete(snapshot_id=data.snapshot.snapshot_id)
    control = EvolutionRevalidationRolloutControlService(
        workspace_root=data.fixture.data.fixture.root,
        store=data.fixture.control_store,
        control_plane_key_provider=lambda: b"remote-finalization-control" * 2,
    )
    data.fixture.data.clock.value += timedelta(seconds=1)
    await control.pause(
        reason_code="population_finalization_pause",
        actor=EvolutionRevalidationRolloutControlActor.MONITOR,
        changed_at=data.fixture.data.clock().isoformat(),
    )
    paused = await data.service.inspect(receipt_id=issued.receipt.receipt_id)
    assert paused.stable_population_finalization_fact
    assert not paused.stable_population_finalization_authority
    assert "member_finalization_authority_changed" in paused.invalidation_reasons

    data.fixture.data.fixture.data["policy"][0] = _policy(data.fixture.data.fixture.data["signer"])
    next_snapshot = data.fixture.data.fixture.data["signer"].issue_snapshot(
        channel="stable",
        credentials=data.fixture.data.fixture.data["credentials"],
        previous=data.snapshot,
        generated_at=(T0 + timedelta(days=1)).isoformat(),
        valid_from=(T0 + timedelta(days=1, seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=8)).isoformat(),
    )
    await data.fixture.data.fixture.data["population_store"].record(next_snapshot)
    stale = await data.service.inspect(receipt_id=issued.receipt.receipt_id)
    assert not stale.population_snapshot_current
    assert not stale.stable_population_finalization_authority


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_population_finalization_fails_closed_on_tampered_member_receipt(
    tmp_path: Path,
) -> None:
    data = await _population_fixture(tmp_path)
    issued = await data.service.complete(snapshot_id=data.snapshot.snapshot_id)
    target = issued.receipt.members[0]
    with sqlite3.connect(data.fixture.db_path) as db:
        row = db.execute(
            "SELECT receipt_json FROM evolution_stable_remote_finalization_receipts "
            "WHERE receipt_id = ?",
            (target.member_receipt_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["receipt_sha256"] = "f" * 64
        db.execute(
            "UPDATE evolution_stable_remote_finalization_receipts "
            "SET receipt_json = ? WHERE receipt_id = ?",
            (json.dumps(payload), target.member_receipt_id),
        )
        db.commit()
    revoked = await data.service.inspect(receipt_id=issued.receipt.receipt_id)
    assert revoked.stable_population_finalization_fact
    assert not revoked.receipt_set_current
    assert not revoked.stable_population_finalization_authority


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_population_finalization_conflict_revokes_and_writer_fences(
    tmp_path: Path,
) -> None:
    data = await _population_fixture(tmp_path)
    issued = await data.service.complete(snapshot_id=data.snapshot.snapshot_id)
    conflicting = await _finish_second_member_again(data)
    assert conflicting.receipt.receipt_id not in {
        item.member_receipt_id for item in issued.receipt.members
    }

    revoked = await data.service.inspect(receipt_id=issued.receipt.receipt_id)
    assert revoked.stable_population_finalization_fact
    assert not revoked.receipt_set_current
    assert not revoked.stable_population_finalization_authority
    with pytest.raises(EvolutionStableRemotePopulationFinalizationError) as writer:
        await data.store.record(
            issued.receipt,
            workspace_root=data.fixture.data.fixture.root,
        )
    assert writer.value.code == "stable_remote_population_member_receipt_conflict"
    with pytest.raises(EvolutionStableRemotePopulationFinalizationError) as complete:
        await data.service.complete(snapshot_id=data.snapshot.snapshot_id)
    assert complete.value.code == "stable_remote_population_member_receipt_conflict"


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_population_finalization_writer_fails_closed_on_corrupt_control(
    tmp_path: Path,
) -> None:
    data = await _population_fixture(tmp_path)
    issued = await data.service.complete(snapshot_id=data.snapshot.snapshot_id)
    with sqlite3.connect(data.fixture.db_path) as db:
        db.execute(
            "INSERT INTO evolution_revalidation_rollout_control_events "
            "(event_id, event_sha256, workspace_root, sequence, state, "
            "event_json, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "evrerolloutctrl_" + "a" * 24,
                "b" * 64,
                str(data.fixture.data.fixture.root),
                1,
                "active",
                '{"schema_version":999}',
                "2026-08-11T03:00:00+00:00",
            ),
        )
        db.commit()

    with pytest.raises(EvolutionStableRemotePopulationFinalizationError) as error:
        await data.store.record(
            issued.receipt,
            workspace_root=data.fixture.data.fixture.root,
        )
    assert error.value.code == "stable_remote_population_control_corrupt"


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_population_finalization_writer_recomputes_source_metadata(
    tmp_path: Path,
) -> None:
    data = await _population_fixture(tmp_path)
    issued = await data.service.complete(snapshot_id=data.snapshot.snapshot_id)
    payload = issued.receipt.model_dump(mode="json")
    payload["candidate_version"] = "forged-version"
    core = {
        key: value for key, value in payload.items() if key not in {"receipt_id", "receipt_sha256"}
    }
    digest = hashlib.sha256(
        json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    forged = EvolutionStableRemotePopulationFinalizationReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evstableremotepopfinal_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )

    with pytest.raises(EvolutionStableRemotePopulationFinalizationError) as error:
        await data.store.record(
            forged,
            workspace_root=data.fixture.data.fixture.root,
        )
    assert error.value.code == "stable_remote_population_metadata_mismatch"


@pytest.mark.asyncio
async def test_engine_composes_population_finalization_service_and_tool(
    tmp_path: Path,
) -> None:
    session_db = tmp_path / ".naumi" / "sessions.db"
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(
                session_db_path=str(session_db),
                vector_db_path=str(tmp_path / ".naumi" / "chroma"),
                long_term_enabled=False,
            ),
        )
    )
    try:
        tool = engine.tool_registry.get("evolution_stable_remote_population_finalization")
        assert isinstance(tool, EvolutionStableRemotePopulationFinalizationTool)
        assert tool._engine is engine
        assert (
            engine.evolution_stable_remote_population_finalization_service.store
            is engine.evolution_stable_remote_population_finalization_store
        )
        assert (
            engine.evolution_stable_remote_population_finalization_store.db_path
            == session_db.resolve()
        )
    finally:
        await engine.shutdown()
