from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import naumi_agent.evolution as evolution_api
import naumi_agent.release as release_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.stable_promotion_runtime_admission_deliveries import (
    EvolutionStablePromotionRuntimeAdmissionDeliveryError,
    EvolutionStablePromotionRuntimeAdmissionDeliveryService,
    EvolutionStablePromotionRuntimeAdmissionDeliveryStore,
    decode_stable_promotion_runtime_admission_submission,
    encode_stable_promotion_runtime_admission_submission,
)
from naumi_agent.release.installation_keys import ReleaseInstallationKeyService
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePromotionRuntimeAdmissionDeliveryTool,
)
from tests.unit.test_evolution_stable_promotion_runtime_observation_admissions import (
    _fixture,
)
from tests.unit.test_release_installation_keys import _MemoryBackend


def _delivery_service(data, admission_service, key_service):
    return EvolutionStablePromotionRuntimeAdmissionDeliveryService(
        admission_store=admission_service.store,
        admission_service=admission_service,
        contract_store=data.contract_service.store,
        contract_service=data.contract_service,
        finalization_service=data.finalization_service,
        population_store=data.context["population_store"],
        installation_key_service=key_service,
        store=EvolutionStablePromotionRuntimeAdmissionDeliveryStore(
            admission_service.store.db_path
        ),
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_signed_runtime_admission_delivery_closes_real_authority_and_retry_edges(
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
    member_id = admission.installation_member_id
    private_key = data.context["keys"][member_id]
    signing_now = [datetime.fromisoformat(admission.admitted_at) + timedelta(seconds=1)]
    key_service = ReleaseInstallationKeyService(
        tmp_path / "installation-release",
        backend=_MemoryBackend(),
        key_factory=lambda _size: private_key.private_bytes_raw(),
        clock=lambda: signing_now[0],
    )
    key_service.provision(channel="stable")
    service = _delivery_service(data, admission_service, key_service)

    left, right = await asyncio.gather(
        service.prepare(admission_id=admission.admission_id),
        service.prepare(admission_id=admission.admission_id),
    )
    assert left == right
    assert left.payload.admission == admission
    assert left.signature.installation_member_id == member_id
    assert left.signature.domain == (
        "naumi.release.stable-promotion-runtime-admission.v1"
    )
    assert release_api.RELEASE_INSTALLATION_STABLE_PROMOTION_ADMISSION_SIGNATURE_DOMAIN == (
        left.signature.domain
    )
    assert not left.payload.remote_harness_directly_revalidated
    assert decode_stable_promotion_runtime_admission_submission(
        encode_stable_promotion_runtime_admission_submission(left)
    ) == left
    with sqlite3.connect(service.store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM "
            "evolution_stable_promotion_runtime_admission_outbox"
        ).fetchone() == (1,)
        db.execute(
            "UPDATE evolution_stable_promotion_runtime_admission_outbox "
            "SET submitted_at = ? WHERE admission_id = ?",
            ("2000-01-01T00:00:00+00:00", admission.admission_id),
        )
    with pytest.raises(EvolutionStablePromotionRuntimeAdmissionDeliveryError) as corrupt_outbox:
        await service.prepare(admission_id=admission.admission_id)
    assert corrupt_outbox.value.code == "stable_promotion_admission_delivery_store_corrupt"
    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_runtime_admission_outbox "
            "SET submitted_at = ? WHERE admission_id = ?",
            (left.payload.submitted_at, admission.admission_id),
        )

        db.execute(
            "DELETE FROM evolution_stable_remote_finalization_receipts "
            "WHERE receipt_id = ?",
            (admission.member_receipt_id,),
        )
    with pytest.raises(EvolutionStablePromotionRuntimeAdmissionDeliveryError) as missing:
        await service.receive(
            submission=left,
            received_at=signing_now[0] + timedelta(seconds=1),
        )
    assert missing.value.code == (
        "stable_promotion_admission_delivery_control_source_mismatch"
    )
    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "INSERT INTO evolution_stable_remote_finalization_receipts "
            "(receipt_id, receipt_sha256) VALUES (?, ?)",
            (admission.member_receipt_id, admission.member_receipt_sha256),
        )

    received_at = signing_now[0] + timedelta(seconds=2)
    first, retry = await asyncio.gather(
        service.receive(submission=left, received_at=received_at),
        service.receive(
            submission=left,
            received_at=received_at + timedelta(microseconds=1),
        ),
    )
    assert first == retry
    assert first.status == "received"
    assert first.remote_admission_delivery_authority
    assert first.durable_receipt_valid
    assert first.observation_contract_authority
    assert first.population_member_authority
    assert first.current_credential_authority
    assert first.installation_signature_authority
    assert not first.remote_harness_directly_revalidated
    assert not first.observation_window_authority
    assert not first.long_term_metrics_authority
    assert not first.promoted_outcome_authority
    assert not first.learning_authority
    assert not first.promotion_authority
    assert not first.execution_authority
    later_retry = await service.receive(
        submission=left,
        received_at=received_at + timedelta(hours=1),
    )
    assert later_retry == first
    with sqlite3.connect(service.store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM "
            "evolution_stable_promotion_runtime_admission_receipts"
        ).fetchone() == (1,)

    tampered = left.model_copy(
        update={"submission_sha256": "0" * 64},
    )
    with pytest.raises(EvolutionStablePromotionRuntimeAdmissionDeliveryError) as invalid:
        await service.receive(submission=tampered)
    assert invalid.value.code == "stable_promotion_admission_delivery_submission_invalid"

    tool = EvolutionStablePromotionRuntimeAdmissionDeliveryTool(
        SimpleNamespace(
            evolution_stable_promotion_runtime_admission_delivery_service=service
        )
    )
    arguments = {"action": "inspect", "admission_id": admission.admission_id}
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    assert "Remote Admission delivery authority：`true`" in await tool.execute(
        **arguments
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
        "/evolution stable-promotion-admission-delivery inspect "
        + admission.admission_id,
    )
    assert "Stable Promotion Runtime Admission Delivery" in slash

    snapshot = data.context["snapshot"]
    original_clock = data.context["population_clock"][0]
    data.context["population_clock"][0] = (
        datetime.fromisoformat(snapshot.payload.expires_at) + timedelta(seconds=1)
    )
    stale = await service.inspect(
        submission=left,
        receipt=first.receipt,
    )
    assert stale.status == "stale"
    assert stale.observation_contract_authority
    assert stale.population_member_authority
    assert not stale.current_credential_authority
    assert stale.installation_signature_authority
    assert not stale.remote_admission_delivery_authority
    with pytest.raises(EvolutionStablePromotionRuntimeAdmissionDeliveryError) as revoked:
        await service.prepare(admission_id=admission.admission_id)
    assert revoked.value.code == (
        "stable_promotion_admission_delivery_credential_missing"
    )
    data.context["population_clock"][0] = original_clock

    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_runtime_admission_receipts "
            "SET receipt_sha256 = ? WHERE receipt_id = ?",
            ("f" * 64, first.receipt.receipt_id),
        )
    corrupt = await service.inspect(submission=left, receipt=first.receipt)
    assert not corrupt.durable_receipt_valid
    assert not corrupt.remote_admission_delivery_authority

    assert (
        evolution_api.EvolutionStablePromotionRuntimeAdmissionDeliveryService
        is EvolutionStablePromotionRuntimeAdmissionDeliveryService
    )


def test_runtime_admission_delivery_rejects_noncanonical_external_payloads() -> None:
    for encoded in ("", "not-base64", "YQ"):
        with pytest.raises(EvolutionStablePromotionRuntimeAdmissionDeliveryError) as invalid:
            decode_stable_promotion_runtime_admission_submission(encoded)
        assert invalid.value.code == "stable_promotion_admission_delivery_payload_invalid"
