from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.stable_promotion_observation_chain_cursors import (
    EvolutionStablePromotionObservationChainCursorError,
    EvolutionStablePromotionObservationChainCursorService,
    EvolutionStablePromotionObservationChainCursorStore,
)
from naumi_agent.evolution.stable_promotion_runtime_admission_delivery_worker import (
    EvolutionStablePromotionRuntimeAdmissionDispatchStore,
)
from naumi_agent.release.installation_keys import ReleaseInstallationKeyService
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePromotionObservationChainCursorTool,
)
from tests.unit.test_evolution_stable_promotion_runtime_admission_deliveries import (
    _delivery_service,
)
from tests.unit.test_evolution_stable_promotion_runtime_observation_admissions import (
    _fixture,
)
from tests.unit.test_release_installation_keys import _MemoryBackend


async def _delivery_setup(data, admission_service, admission, tmp_path):
    private_key = data.context["keys"][admission.installation_member_id]
    now = [datetime.fromisoformat(admission.admitted_at) + timedelta(seconds=1)]
    key_service = ReleaseInstallationKeyService(
        tmp_path / "installation-release",
        backend=_MemoryBackend(),
        key_factory=lambda _size: private_key.private_bytes_raw(),
        clock=lambda: now[0],
    )
    key_service.provision(channel="stable")
    delivery_service = _delivery_service(data, admission_service, key_service)
    submission = await delivery_service.prepare(admission_id=admission.admission_id)
    dispatch_store = EvolutionStablePromotionRuntimeAdmissionDispatchStore(
        admission_service.store.db_path
    )
    return delivery_service, dispatch_store, submission, now


async def _acknowledge(delivery_service, dispatch_store, submission, now):
    await dispatch_store.enqueue(submission, enqueued_at=now[0])
    claimed = await dispatch_store.claim(
        owner_id="observation-cursor-test",
        now=now[0],
        lease_seconds=10,
    )
    assert claimed is not None
    now[0] += timedelta(seconds=1)
    received = await delivery_service.receive(
        submission=submission,
        received_at=now[0],
    )
    now[0] += timedelta(seconds=1)
    await dispatch_store.acknowledge(
        admission_id=submission.payload.admission.admission_id,
        owner_id="observation-cursor-test",
        claim_epoch=claimed.latest_event.claim_epoch,
        receipt=received.receipt,
        now=now[0],
    )


def _service(data, admission_service, delivery_service, dispatch_store):
    store = EvolutionStablePromotionObservationChainCursorStore(
        admission_service.store.db_path,
        admission_service=admission_service,
        dispatch_store=dispatch_store,
    )
    return EvolutionStablePromotionObservationChainCursorService(
        admission_service=admission_service,
        delivery_service=delivery_service,
        dispatch_store=dispatch_store,
        harness_store=data.harness_store,
        store=store,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_observation_cursor_recovers_exact_har_chain_and_concurrency(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _fixture(tmp_path, monkeypatch)
    unacked_admission_service = data.build_service()
    unacked = await unacked_admission_service.record(
        finalization_receipt_id=data.contract.population_finalization_receipt_id,
        stable_intent_id=data.intent_id,
        subject_id=data.lifecycle.subject_id,
    )
    delivery_service, dispatch_store, submission, now = await _delivery_setup(
        data,
        unacked_admission_service,
        unacked.admission,
        tmp_path,
    )
    unacked_service = _service(
        data,
        unacked_admission_service,
        delivery_service,
        dispatch_store,
    )
    with pytest.raises(EvolutionStablePromotionObservationChainCursorError) as missing:
        await unacked_service.advance(admission_id=unacked.admission.admission_id)
    assert missing.value.code == (
        "stable_promotion_observation_cursor_admission_not_acknowledged"
    )

    await _acknowledge(delivery_service, dispatch_store, submission, now)
    admission_service = unacked_admission_service
    first_service = _service(
        data,
        admission_service,
        delivery_service,
        dispatch_store,
    )
    first = await first_service.advance(
        admission_id=submission.payload.admission.admission_id
    )
    assert first.status == "ready"
    assert first.remote_admission_delivery_authority
    assert first.cursor_delivery_input_authority
    assert first.cursor.revision_count == first.cursor.after_sequence == 2
    assert first.cursor.latest_revision.observation.phase.value == "running"
    assert not first.cursor.signed_page_delivery_authority
    assert not first.cursor.observation_window_authority
    assert not first.cursor.population_observation_authority
    assert not first.cursor.promoted_outcome_authority

    snapshot = data.context["snapshot"]
    original_population_clock = data.context["population_clock"][0]
    data.context["population_clock"][0] = (
        datetime.fromisoformat(snapshot.payload.expires_at) + timedelta(seconds=1)
    )
    stale_remote = await first_service.inspect(
        admission_id=first.cursor.admission.admission_id
    )
    assert stale_remote.status == "stale"
    assert stale_remote.admission_delivery_acknowledged
    assert not stale_remote.remote_admission_delivery_authority
    assert not stale_remote.cursor_delivery_input_authority
    with pytest.raises(
        EvolutionStablePromotionObservationChainCursorError
    ) as stale_advance:
        await first_service.advance(admission_id=first.cursor.admission.admission_id)
    assert stale_advance.value.code == (
        "stable_promotion_observation_cursor_delivery_stale"
    )
    data.context["population_clock"][0] = original_population_clock

    with pytest.raises(ValidationError, match="Input should be False"):
        first.cursor.model_copy(
            update={"signed_page_delivery_authority": True}
        ).model_validate_json(
            first.cursor.model_copy(
                update={"signed_page_delivery_authority": True}
            ).model_dump_json()
        )

    data.runtime_now[0] += timedelta(seconds=10)
    await data.lifecycle._producer.pulse_now()
    second_service = _service(
        data,
        admission_service,
        delivery_service,
        dispatch_store,
    )
    left, right = await asyncio.gather(
        first_service.advance(admission_id=first.cursor.admission.admission_id),
        second_service.advance(admission_id=first.cursor.admission.admission_id),
    )
    assert left == right
    assert left.cursor.revision_count == 3
    assert left.cursor.latest_revision.observation.phase.value == "running"
    assert (
        await first_service.advance(admission_id=first.cursor.admission.admission_id)
    ) == left
    revisions = await first_service.store.revisions(first.cursor.admission.admission_id)
    assert tuple(item.revision_sequence for item in revisions) == (1, 2, 3)
    assert revisions[1].previous_revision_sha256 == revisions[0].revision_sha256
    assert revisions[2].observation.previous_sample_sha256 == (
        revisions[1].observation.sample_sha256
    )
    dispatch = await dispatch_store.get(first.cursor.admission.admission_id)
    assert dispatch is not None and dispatch.receipt is not None
    replayed = await first_service.store.record(
        submission=dispatch.submission,
        receipt=dispatch.receipt,
        observations=tuple(item.observation for item in revisions[:2]),
        window_not_before_at=data.contract.window_not_before_at,
    )
    assert replayed == left.cursor

    tool = EvolutionStablePromotionObservationChainCursorTool(
        SimpleNamespace(
            evolution_stable_promotion_observation_chain_cursor_service=(
                first_service
            )
        )
    )
    arguments = {
        "action": "inspect",
        "admission_id": first.cursor.admission.admission_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    assert "Cursor delivery input：`true`" in await tool.execute(**arguments)
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
        "/evolution stable-promotion-observation-chain inspect "
        f"{first.cursor.admission.admission_id}",
    )
    assert "稳定推广 Observation Chain Cursor" in slash

    with sqlite3.connect(data.harness_store.db_path) as db:
        db.execute(
            "DELETE FROM harness_runtime_release_observations WHERE sample_id = ?",
            (revisions[2].observation.sample_id,),
        )
    stale = await first_service.inspect(
        admission_id=first.cursor.admission.admission_id
    )
    assert stale.status == "stale"
    assert not stale.observation_chain_source_current
    assert not stale.cursor_delivery_input_authority

    with sqlite3.connect(first_service.store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_observation_cursor_revisions "
            "SET revision_json = ? WHERE heartbeat_sequence = 2",
            (revisions[0].model_dump_json(),),
        )
    with pytest.raises(EvolutionStablePromotionObservationChainCursorError) as corrupt:
        await first_service.store.get(first.cursor.admission.admission_id)
    assert corrupt.value.code == "stable_promotion_observation_cursor_chain_corrupt"
