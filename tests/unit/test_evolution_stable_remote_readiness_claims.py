from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import naumi_agent.evolution as evolution_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.stable_remote_readiness_claims import (
    EvolutionStableRemoteReadinessAssertion,
    EvolutionStableRemoteReadinessClaimError,
    EvolutionStableRemoteReadinessClaimReceipt,
    EvolutionStableRemoteReadinessClaimService,
    EvolutionStableRemoteReadinessClaimStore,
    encode_stable_remote_readiness_assertion,
    render_stable_remote_readiness_claim,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionStableRemoteReadinessClaimTool
from tests.unit.test_evolution_stable_rollback_readiness import _readiness_fixture


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _service(fixture, clock):
    return EvolutionStableRemoteReadinessClaimService(
        workspace_root=fixture.root,
        completion_service=fixture.data["service"],
        population_store=fixture.data["population_store"],
        store=EvolutionStableRemoteReadinessClaimStore(
            fixture.data["store"].db_path
        ),
        clock=clock,
        random_bytes=lambda size: b"r" * size,
    )


def _private_key_for_credential(credential) -> Ed25519PrivateKey:
    for index in range(2):
        seed = hashlib.sha256(f"managed-installation-{index}".encode()).digest()
        private = Ed25519PrivateKey.from_private_bytes(seed)
        public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
        if public == credential.payload.installation_public_key_base64:
            return private
    raise AssertionError("fixture credential private key missing")


def _assertion(fixture, challenge):
    return EvolutionStableRemoteReadinessAssertion(
        challenge_id=challenge.challenge_id,
        challenge_sha256=challenge.challenge_sha256,
        nonce_sha256=challenge.nonce_sha256,
        installation_member_id=challenge.installation_member_id,
        stable_intent_id=challenge.stable_intent_id,
        candidate_version=challenge.candidate_version,
        candidate_target=challenge.candidate_target,
        active_pointer_id=fixture.active.pointer_id,
        active_pointer_sha256=fixture.active.pointer_sha256,
        active_pointer_generation=fixture.active.generation,
        candidate_slot_id=fixture.candidate.slot_id,
        candidate_slot_sha256=fixture.candidate.slot_sha256,
        candidate_manifest_sha256=fixture.candidate.manifest_sha256,
        rollback_pointer_id=fixture.prior.pointer_id,
        rollback_pointer_sha256=fixture.prior.pointer_sha256,
        rollback_pointer_generation=fixture.prior.generation,
        rollback_slot_id=fixture.baseline.slot_id,
        rollback_slot_sha256=fixture.baseline.slot_sha256,
        rollback_boot_receipt_id=fixture.prior.boot_receipt_id,
        rollback_boot_receipt_sha256=fixture.prior.boot_receipt_sha256,
        observed_at=challenge.issued_at,
    )


@pytest.mark.asyncio
async def test_remote_readiness_claim_verifies_real_signature_and_shared_slash(
    tmp_path: Path,
) -> None:
    fixture = await _readiness_fixture(tmp_path)
    assert (
        evolution_api.EvolutionStableRemoteReadinessClaimService
        is EvolutionStableRemoteReadinessClaimService
    )
    clock = _Clock(datetime(2026, 8, 10, 9, 0, tzinfo=UTC))
    first = _service(fixture, clock)
    second = _service(fixture, clock)
    tool = EvolutionStableRemoteReadinessClaimTool(
        SimpleNamespace(evolution_stable_remote_readiness_claim_service=first)
    )
    assert not tool.metadata.read_only
    assert not tool.metadata.destructive
    assert not tool.metadata.requires_confirmation
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

    remote_member = fixture.completion.receipt.installation_member_ids[1]
    challenge_output = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-readiness challenge "
        f"{fixture.completion.receipt.receipt_id} {remote_member}",
    )
    challenge_id = re.search(
        r"evstableremotechal_[0-9a-f]{24}",
        strip_ansi(challenge_output),
    )
    assert challenge_id is not None
    challenge = await first.store.get_challenge(challenge_id.group())
    assert challenge is not None
    credential = next(
        item
        for item in fixture.data["snapshot"].payload.credentials
        if item.payload.member_id == remote_member
    )
    assertion = _assertion(fixture, challenge)
    signature = _private_key_for_credential(credential).sign(
        assertion.canonical_bytes()
    )
    arguments = {
        "challenge_id": challenge.challenge_id,
        "assertion_base64": encode_stable_remote_readiness_assertion(assertion),
        "signature_base64": base64.b64encode(signature).decode(),
    }
    views = await asyncio.gather(*(
        (first if index % 2 == 0 else second).ingest(**arguments)
        for index in range(8)
    ))
    assert len({view.receipt.receipt_id for view in views}) == 1
    view = views[0]
    assert view.authenticated_claim_authority
    assert view.signature_valid
    assert not view.binary_rollback_readiness_authority
    assert not view.stable_rollout_authority
    assert not view.remote_execution_authority
    assert not view.promotion_authority
    assert not view.receipt.source_runtime_revalidated
    assert (
        EvolutionStableRemoteReadinessClaimReceipt.model_validate_json(
            view.receipt.model_dump_json()
        )
        == view.receipt
    )
    with sqlite3.connect(fixture.data["store"].db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_stable_remote_readiness_claims"
        ).fetchone()[0] == 1

    ingest_output = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-readiness ingest "
        f"{challenge.challenge_id} {arguments['assertion_base64']} "
        f"{arguments['signature_base64']}",
    )
    assert strip_ansi(ingest_output) == render_stable_remote_readiness_claim(view)
    inspect_output = await execute_slash_command(
        _SlashEngine(),
        f"/evolution stable-remote-readiness inspect {view.receipt.receipt_id}",
    )
    assert strip_ansi(inspect_output) == render_stable_remote_readiness_claim(view)


@pytest.mark.asyncio
async def test_remote_readiness_claim_rejects_wrong_identity_and_expires(
    tmp_path: Path,
) -> None:
    fixture = await _readiness_fixture(tmp_path)
    clock = _Clock(datetime(2026, 8, 10, 9, 0, tzinfo=UTC))
    service = _service(fixture, clock)
    remote_member = fixture.completion.receipt.installation_member_ids[1]
    challenge = await service.issue_challenge(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        installation_member_id=remote_member,
        validity_seconds=60,
    )
    assertion = _assertion(fixture, challenge)
    wrong_signature = Ed25519PrivateKey.generate().sign(assertion.canonical_bytes())
    with pytest.raises(EvolutionStableRemoteReadinessClaimError) as error:
        await service.ingest(
            challenge_id=challenge.challenge_id,
            assertion_base64=encode_stable_remote_readiness_assertion(assertion),
            signature_base64=base64.b64encode(wrong_signature).decode(),
        )
    assert error.value.code == "stable_remote_readiness_signature_untrusted"

    credential = next(
        item
        for item in fixture.data["snapshot"].payload.credentials
        if item.payload.member_id == remote_member
    )
    signature = _private_key_for_credential(credential).sign(
        assertion.canonical_bytes()
    )
    current = await service.ingest(
        challenge_id=challenge.challenge_id,
        assertion_base64=encode_stable_remote_readiness_assertion(assertion),
        signature_base64=base64.b64encode(signature).decode(),
    )
    assert current.authenticated_claim_authority
    with pytest.raises(EvolutionStableRemoteReadinessClaimError) as conflict:
        await service.ingest(
            challenge_id=challenge.challenge_id,
            assertion_base64=encode_stable_remote_readiness_assertion(assertion),
            signature_base64=base64.b64encode(wrong_signature).decode(),
        )
    assert conflict.value.code == "stable_remote_readiness_claim_conflict"
    clock.value += timedelta(seconds=61)
    expired = await service.inspect(receipt_id=current.receipt.receipt_id)
    assert not expired.challenge_unexpired
    assert not expired.authenticated_claim_authority
    assert not expired.binary_rollback_readiness_authority


@pytest.mark.asyncio
async def test_remote_readiness_claim_rejects_non_member_and_corrupt_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _readiness_fixture(tmp_path)
    clock = _Clock(datetime(2026, 8, 10, 9, 0, tzinfo=UTC))
    service = _service(fixture, clock)
    with pytest.raises(EvolutionStableRemoteReadinessClaimError) as error:
        await service.issue_challenge(
            completion_receipt_id=fixture.completion.receipt.receipt_id,
            installation_member_id="relpopmember_" + "f" * 24,
        )
    assert error.value.code == "stable_remote_readiness_member_not_current"

    member = fixture.completion.receipt.installation_member_ids[1]
    challenge = await service.issue_challenge(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        installation_member_id=member,
    )
    credential = next(
        item
        for item in fixture.data["snapshot"].payload.credentials
        if item.payload.member_id == member
    )
    assertion = _assertion(fixture, challenge)
    signature = _private_key_for_credential(credential).sign(
        assertion.canonical_bytes()
    )
    current = await service.ingest(
        challenge_id=challenge.challenge_id,
        assertion_base64=encode_stable_remote_readiness_assertion(assertion),
        signature_base64=base64.b64encode(signature).decode(),
    )
    with sqlite3.connect(fixture.data["store"].db_path) as db:
        db.execute(
            "UPDATE evolution_stable_remote_readiness_claims "
            "SET receipt_json = '{}' WHERE receipt_id = ?",
            (current.receipt.receipt_id,),
        )
        db.commit()
    with pytest.raises(EvolutionStableRemoteReadinessClaimError) as error:
        await service.inspect(receipt_id=current.receipt.receipt_id)
    assert error.value.code == "stable_remote_readiness_store_corrupt"

    async def _stale_completion(*, receipt_id: str):
        assert receipt_id == fixture.completion.receipt.receipt_id
        return fixture.completion.model_copy(
            update={"stable_population_completion_authority": False}
        )

    monkeypatch.setattr(fixture.data["service"], "inspect", _stale_completion)
    with pytest.raises(EvolutionStableRemoteReadinessClaimError) as stale:
        await service.issue_challenge(
            completion_receipt_id=fixture.completion.receipt.receipt_id,
            installation_member_id=member,
        )
    assert stale.value.code == "stable_remote_readiness_source_unavailable"


@pytest.mark.asyncio
async def test_engine_composes_remote_readiness_claim_service_and_tool(
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
        tool = engine.tool_registry.get("evolution_stable_remote_readiness_claim")
        assert isinstance(tool, EvolutionStableRemoteReadinessClaimTool)
        assert tool._engine is engine
        assert (
            engine.evolution_stable_remote_readiness_claim_service.store
            is engine.evolution_stable_remote_readiness_claim_store
        )
        assert engine.evolution_stable_remote_readiness_claim_store.db_path == (
            session_db.resolve()
        )
    finally:
        await engine.shutdown()
