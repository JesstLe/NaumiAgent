from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.stable_promotion_observation_revision_deliveries import (
    EvolutionStablePromotionObservationRevisionDeliveryError,
    EvolutionStablePromotionObservationRevisionDeliveryService,
    EvolutionStablePromotionObservationRevisionDeliveryStore,
    EvolutionStablePromotionObservationRevisionSubmission,
    decode_stable_promotion_observation_revision_submission,
    encode_stable_promotion_observation_revision_submission,
)
from naumi_agent.release.installation_keys import (
    RELEASE_INSTALLATION_STABLE_PROMOTION_OBSERVATION_SIGNATURE_DOMAIN,
    verify_release_installation_signature,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
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
from tests.unit.test_evolution_stable_promotion_runtime_observation_admissions import (
    _fixture,
)


def _revision_service(data, cursor_service, delivery_service):
    return EvolutionStablePromotionObservationRevisionDeliveryService(
        cursor_store=cursor_service.store,
        cursor_service=cursor_service,
        admission_delivery_service=delivery_service,
        population_store=data.context["population_store"],
        installation_key_service=delivery_service.installation_key_service,
        store=EvolutionStablePromotionObservationRevisionDeliveryStore(
            cursor_service.store.db_path
        ),
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_signed_revision_delivery_closes_real_chain_and_retry_edges(
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
    delivery_service, dispatch_store, admission_submission, now = (
        await _delivery_setup(
            data,
            admission_service,
            admission_view.admission,
            tmp_path,
        )
    )
    await _acknowledge(delivery_service, dispatch_store, admission_submission, now)
    cursor_service = _cursor_service(
        data,
        admission_service,
        delivery_service,
        dispatch_store,
    )
    cursor_view = await cursor_service.advance(
        admission_id=admission_view.admission.admission_id
    )
    service = _revision_service(data, cursor_service, delivery_service)
    signing_clock = [now[0]]
    signing_calls = [0]

    def _ticking_signing_clock():
        signing_calls[0] += 1
        return signing_clock[0] + timedelta(microseconds=signing_calls[0])

    delivery_service.installation_key_service.clock = _ticking_signing_clock

    first, retry = await asyncio.gather(
        service.prepare(admission_id=admission_view.admission.admission_id),
        service.prepare(admission_id=admission_view.admission.admission_id),
    )
    assert first == retry
    assert first.payload.first_sequence == 1
    assert first.payload.last_sequence == cursor_view.cursor.after_sequence == 2
    assert first.payload.revision_count == 2
    assert first.signature.domain == (
        RELEASE_INSTALLATION_STABLE_PROMOTION_OBSERVATION_SIGNATURE_DOMAIN
    )
    assert first.signature.payload_bytes <= 64 * 1024
    credential = next(
        item
        for item in data.context["snapshot"].payload.credentials
        if item.payload.member_id == admission_view.admission.installation_member_id
    )
    verify_release_installation_signature(
        credential=credential,
        payload=first.payload.canonical_bytes(),
        artifact=first.signature,
        expected_domain=(
            RELEASE_INSTALLATION_STABLE_PROMOTION_OBSERVATION_SIGNATURE_DOMAIN
        ),
    )
    assert decode_stable_promotion_observation_revision_submission(
        encode_stable_promotion_observation_revision_submission(first)
    ) == first

    received_at = now[0] + timedelta(seconds=1)
    left, right = await asyncio.gather(
        service.receive(submission=first, received_at=received_at),
        service.receive(
            submission=first,
            received_at=received_at + timedelta(microseconds=1),
        ),
    )
    assert left == right
    assert left.status == "received"
    assert left.remote_revision_delivery_authority
    assert left.remote_revision_chain_current
    assert left.installation_cursor_source_current
    assert (await service.receive(submission=first, received_at=received_at)) == left
    assert await service.store.remote_head(first.payload.admission_id) == (
        2,
        first.payload.revisions[-1].revision_sha256,
    )
    assert not left.observation_window_authority
    assert not left.population_observation_authority
    assert not left.promoted_outcome_authority

    tool = EvolutionStablePromotionObservationRevisionDeliveryTool(
        SimpleNamespace(
            evolution_stable_promotion_observation_revision_delivery_service=(
                service
            )
        )
    )
    arguments = {
        "action": "inspect",
        "receipt_id": left.receipt.receipt_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    assert "Remote revision delivery authority：`true`" in await tool.execute(
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
        "/evolution stable-promotion-observation-revisions inspect "
        + left.receipt.receipt_id,
    )
    assert "Stable Promotion Observation Revision Delivery" in slash

    with pytest.raises(ValidationError, match="Input should be False"):
        left.receipt.model_copy(
            update={"observation_window_authority": True}
        ).model_validate_json(
            left.receipt.model_copy(
                update={"observation_window_authority": True}
            ).model_dump_json()
        )

    data.runtime_now[0] += timedelta(seconds=10)
    await data.lifecycle._producer.pulse_now()
    advanced = await cursor_service.advance(
        admission_id=admission_view.admission.admission_id
    )
    assert advanced.cursor.after_sequence == 3
    now[0] = data.runtime_now[0] + timedelta(seconds=1)
    signing_clock[0] = now[0]

    skipped = await service.prepare(
        admission_id=admission_view.admission.admission_id,
        after_sequence=1,
    )
    with pytest.raises(
        EvolutionStablePromotionObservationRevisionDeliveryError
    ) as wrong_head:
        await service.receive(
            submission=skipped,
            received_at=now[0] + timedelta(seconds=1),
        )
    assert wrong_head.value.code == (
        "stable_promotion_observation_revision_remote_head_conflict"
    )

    second = await service.prepare(
        admission_id=admission_view.admission.admission_id,
        after_sequence=2,
    )
    assert second.payload.first_sequence == second.payload.last_sequence == 3
    completed = await service.receive(
        submission=second,
        received_at=now[0] + timedelta(seconds=1),
    )
    assert completed.status == "received"
    assert await service.store.remote_head(first.payload.admission_id) == (
        3,
        second.payload.revisions[-1].revision_sha256,
    )

    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_observation_revision_heads "
            "SET last_revision_sha256 = ? WHERE admission_id = ?",
            ("e" * 64, first.payload.admission_id),
        )
    broken_head = await service.inspect(
        submission=second,
        receipt=completed.receipt,
    )
    assert broken_head.status == "stale"
    assert not broken_head.remote_revision_chain_current
    assert not broken_head.remote_revision_delivery_authority
    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_observation_revision_heads "
            "SET last_revision_sha256 = ? WHERE admission_id = ?",
            (second.payload.revisions[-1].revision_sha256, first.payload.admission_id),
        )

    original_population_clock = data.context["population_clock"][0]
    data.context["population_clock"][0] = (
        datetime.fromisoformat(data.context["snapshot"].payload.expires_at)
        + timedelta(seconds=1)
    )
    stale = await service.inspect(
        submission=second,
        receipt=completed.receipt,
    )
    assert stale.status == "stale"
    assert stale.durable_receipt_valid
    assert not stale.current_credential_authority
    assert not stale.remote_revision_delivery_authority
    with pytest.raises(
        EvolutionStablePromotionObservationRevisionDeliveryError
    ) as expired:
        await service.prepare(
            admission_id=admission_view.admission.admission_id,
            after_sequence=2,
        )
    assert expired.value.code in {
        "stable_promotion_observation_revision_cursor_stale",
        "stable_promotion_observation_revision_admission_stale",
    }
    data.context["population_clock"][0] = original_population_clock

    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_observation_revision_receipts "
            "SET receipt_sha256 = ? WHERE receipt_id = ?",
            ("f" * 64, completed.receipt.receipt_id),
        )
    corrupt = await service.inspect(
        submission=second,
        receipt=completed.receipt,
    )
    assert corrupt.status == "stale"
    assert not corrupt.durable_receipt_valid
    assert not corrupt.remote_revision_delivery_authority


def test_revision_delivery_rejects_noncanonical_external_payloads() -> None:
    for encoded in ("", "not-base64", "YQ"):
        with pytest.raises(
            EvolutionStablePromotionObservationRevisionDeliveryError
        ) as invalid:
            decode_stable_promotion_observation_revision_submission(encoded)
        assert invalid.value.code == (
            "stable_promotion_observation_revision_payload_invalid"
        )

    with pytest.raises(ValidationError):
        EvolutionStablePromotionObservationRevisionSubmission.model_validate({})
