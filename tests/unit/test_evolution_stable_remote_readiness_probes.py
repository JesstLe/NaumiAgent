from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import naumi_agent.evolution as evolution_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.evolution.stable_remote_readiness_probes import (
    EvolutionStableRemoteReadinessProbeError,
    EvolutionStableRemoteReadinessProbeService,
    EvolutionStableRemoteReadinessProbeStore,
    EvolutionStableRemoteReadinessProbeSubmission,
    decode_stable_remote_readiness_probe_submission,
    encode_stable_remote_readiness_probe_challenge,
    encode_stable_remote_readiness_probe_submission,
    execute_stable_remote_readiness_probe,
    render_stable_remote_readiness_probe,
)
from naumi_agent.release.installation_keys import (
    ReleaseInstallationKeyService,
    ReleaseInstallationSignature,
)
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStableRemoteReadinessProbeTool,
)
from tests.unit.test_evolution_stable_remote_readiness_claims import (
    _assertion,
    _private_key_for_credential,
    _readiness_fixture,
)
from tests.unit.test_evolution_stable_remote_readiness_claims import (
    _service as _claim_service,
)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.lock = threading.Lock()

    def set_password(self, service: str, account: str, value: str) -> None:
        with self.lock:
            self.values[(service, account)] = value

    def get_password(self, service: str, account: str) -> str | None:
        with self.lock:
            return self.values.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        with self.lock:
            self.values.pop((service, account), None)


async def _fixture(tmp_path: Path):
    fixture = await _readiness_fixture(tmp_path)
    clock = _Clock(datetime(2026, 8, 11, 9, 0, tzinfo=UTC))
    claim_service = _claim_service(fixture, clock)
    member = fixture.completion.receipt.installation_member_ids[1]
    claim_challenge = await claim_service.issue_challenge(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        installation_member_id=member,
        validity_seconds=300,
    )
    credential = next(
        item
        for item in fixture.data["snapshot"].payload.credentials
        if item.payload.member_id == member
    )
    assertion = _assertion(fixture, claim_challenge)
    signature = _private_key_for_credential(credential).sign(
        assertion.canonical_bytes()
    )
    claim = await claim_service.ingest(
        challenge_id=claim_challenge.challenge_id,
        assertion_base64=base64.b64encode(assertion.canonical_bytes()).decode(),
        signature_base64=base64.b64encode(signature).decode(),
    )
    service = EvolutionStableRemoteReadinessProbeService(
        claim_service=claim_service,
        store=EvolutionStableRemoteReadinessProbeStore(
            fixture.data["store"].db_path
        ),
        clock=clock,
        random_bytes=lambda size: b"p" * size,
    )
    seed = next(
        hashlib.sha256(f"managed-installation-{index}".encode()).digest()
        for index in range(2)
        if _private_key_for_credential(credential).private_bytes_raw()
        == hashlib.sha256(f"managed-installation-{index}".encode()).digest()
    )
    key_service = ReleaseInstallationKeyService(
        fixture.store.release_root,
        backend=_MemoryBackend(),
        key_factory=lambda size: seed,
        clock=clock,
    )
    key_service.provision(channel="stable")
    return SimpleNamespace(
        fixture=fixture,
        clock=clock,
        claim=claim,
        credential=credential,
        service=service,
        key_service=key_service,
    )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_probe_real_store_signature_shared_slash_and_concurrency(
    tmp_path: Path,
) -> None:
    data = await _fixture(tmp_path)
    assert (
        evolution_api.EvolutionStableRemoteReadinessProbeService
        is EvolutionStableRemoteReadinessProbeService
    )
    challenge = await data.service.prepare(
        claim_receipt_id=data.claim.receipt.receipt_id,
        validity_seconds=180,
    )
    engine = SimpleNamespace(
        evolution_stable_remote_readiness_probe_service=data.service,
        evolution_release_population_snapshot_store=(
            data.fixture.data["population_store"]
        ),
        evolution_release_slot_store=data.fixture.store,
        release_installation_key_service=data.key_service,
    )
    tool = EvolutionStableRemoteReadinessProbeTool(engine)
    assert not tool.metadata.read_only
    assert not tool.metadata.destructive
    assert tool.metadata.concurrency_safe
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

    active_before = data.fixture.store.active()
    local_output = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-readiness-probe execute-local "
        + encode_stable_remote_readiness_probe_challenge(challenge),
    )
    encoded_match = re.search(
        r"Submission\s+Base64：`([^`]+)`",
        strip_ansi(local_output),
    )
    assert encoded_match is not None, strip_ansi(local_output)
    submission = decode_stable_remote_readiness_probe_submission(
        "".join(encoded_match.group(1).split())
    )
    assert data.fixture.store.active() == active_before
    second = EvolutionStableRemoteReadinessProbeService(
        claim_service=data.service.claim_service,
        store=EvolutionStableRemoteReadinessProbeStore(
            data.fixture.data["store"].db_path
        ),
        clock=data.clock,
    )
    encoded_submission = encode_stable_remote_readiness_probe_submission(submission)
    views = await asyncio.gather(*(
        (data.service if index % 2 == 0 else second).ingest(
            challenge_id=challenge.challenge_id,
            submission_base64=encoded_submission,
        )
        for index in range(8)
    ))
    assert len({item.receipt.receipt_id for item in views}) == 1
    assert views[0].source_runtime_revalidation_authority
    assert views[0].binary_rollback_readiness_authority
    assert not views[0].config_data_rollback_readiness_authority
    assert not views[0].stable_rollout_authority
    assert not views[0].remote_execution_authority
    assert not views[0].promotion_authority
    inspect_output = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-remote-readiness-probe inspect "
        + views[0].receipt.receipt_id,
    )
    normalized_inspect = " ".join(strip_ansi(inspect_output).split())
    normalized_expected = " ".join(
        render_stable_remote_readiness_probe(views[0]).split()
    )
    assert normalized_inspect == normalized_expected


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_probe_fails_closed_on_store_change_and_pointer_toctou(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _fixture(tmp_path)
    challenge = await data.service.prepare(
        claim_receipt_id=data.claim.receipt.receipt_id,
    )
    actual = data.fixture.store.active()
    changed = actual.model_copy(update={"pointer_id": "relactive_" + "f" * 24})
    calls = iter((actual, changed))
    monkeypatch.setattr(data.fixture.store, "active", lambda: next(calls))
    with pytest.raises(EvolutionStableRemoteReadinessProbeError) as error:
        execute_stable_remote_readiness_probe(
            challenge=challenge,
            credential=data.credential,
            release_slot_store=data.fixture.store,
            installation_key_service=data.key_service,
            clock=data.clock,
        )
    assert error.value.code == "stable_remote_probe_pointer_changed_during_read"


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_remote_probe_authority_expires_and_corrupt_receipt_is_rejected(
    tmp_path: Path,
) -> None:
    data = await _fixture(tmp_path)
    data.clock.value += timedelta(microseconds=250_000)
    challenge = await data.service.prepare(
        claim_receipt_id=data.claim.receipt.receipt_id,
        validity_seconds=300,
    )
    assert challenge.validity_seconds == 299
    submission = execute_stable_remote_readiness_probe(
        challenge=challenge,
        credential=data.credential,
        release_slot_store=data.fixture.store,
        installation_key_service=data.key_service,
        clock=data.clock,
    )
    forged_bytes = b"x" * 64
    forged_core = submission.signature.model_dump(
        mode="json",
        exclude={"signature_id", "signature_artifact_sha256"},
    )
    forged_core["signature_base64"] = base64.b64encode(forged_bytes).decode()
    forged_core["signature_sha256"] = hashlib.sha256(forged_bytes).hexdigest()
    forged_digest = hashlib.sha256(json.dumps(
        forged_core,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    forged_signature = ReleaseInstallationSignature.model_validate({
        **forged_core,
        "signature_id": f"relinstallsig_{forged_digest[:24]}",
        "signature_artifact_sha256": forged_digest,
    })
    forged = EvolutionStableRemoteReadinessProbeSubmission(
        result=submission.result,
        signature=forged_signature,
    )
    with pytest.raises(EvolutionStableRemoteReadinessProbeError) as untrusted:
        await data.service.ingest(
            challenge_id=challenge.challenge_id,
            submission_base64=encode_stable_remote_readiness_probe_submission(forged),
        )
    assert untrusted.value.code == "release_installation_signature_untrusted"
    current = await data.service.ingest(
        challenge_id=challenge.challenge_id,
        submission_base64=encode_stable_remote_readiness_probe_submission(submission),
    )
    data.clock.value += timedelta(seconds=300)
    expired = await data.service.inspect(receipt_id=current.receipt.receipt_id)
    assert not expired.freshness_current
    assert not expired.binary_rollback_readiness_authority

    with sqlite3.connect(data.fixture.data["store"].db_path) as db:
        db.execute(
            "UPDATE evolution_stable_remote_readiness_probe_receipts "
            "SET receipt_json = '{}' WHERE receipt_id = ?",
            (current.receipt.receipt_id,),
        )
        db.commit()
    with pytest.raises(EvolutionStableRemoteReadinessProbeError) as corrupt:
        await data.service.inspect(receipt_id=current.receipt.receipt_id)
    assert corrupt.value.code == "stable_remote_probe_store_corrupt"
