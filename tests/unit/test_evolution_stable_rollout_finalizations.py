from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlActor,
    EvolutionRevalidationRolloutControlService,
)
from naumi_agent.evolution.stable_rollout_finalizations import (
    EvolutionStableRolloutFinalizationError,
    EvolutionStableRolloutFinalizationReceipt,
    EvolutionStableRolloutFinalizationService,
    EvolutionStableRolloutFinalizationStore,
    render_stable_rollout_finalization,
)
from naumi_agent.release.slots import ReleaseStableMemberFinalizationAuthority
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionStableRolloutFinalizationTool
from tests.unit.test_evolution_stable_rollout_authorizations import _services


def _finalization_service(fixture, authorization_service, db_path, clock):
    return EvolutionStableRolloutFinalizationService(
        workspace_root=fixture.root,
        authorization_service=authorization_service,
        release_slot_store=fixture.store,
        store=EvolutionStableRolloutFinalizationStore(db_path),
        clock=clock,
    )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_finalization_executes_real_pointer_cas_and_shared_slash(
    tmp_path: Path,
) -> None:
    fixture, db_path, _control, _store, clock, make_authorization = await _services(tmp_path)
    authorization_service = make_authorization()
    authorization = await authorization_service.issue(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
    )
    first_service = _finalization_service(fixture, authorization_service, db_path, clock)
    second_service = _finalization_service(fixture, authorization_service, db_path, clock)
    views = await asyncio.gather(
        *(
            (first_service if index % 2 == 0 else second_service).execute(
                authorization_id=authorization.authorization.authorization_id
            )
            for index in range(6)
        )
    )
    assert len({view.receipt.receipt_id for view in views}) == 1
    view = views[0]
    receipt = view.receipt
    assert view.stable_member_completion_fact
    assert view.active_stable_member_authority
    assert not view.stable_population_rollout_authority
    assert not view.promotion_authority
    assert receipt.release_finalization.active_pointer == fixture.active
    assert receipt.release_finalization.expected_pointer_cas_satisfied
    assert not receipt.config_data_mutation_executed
    assert (
        EvolutionStableRolloutFinalizationReceipt.model_validate_json(receipt.model_dump_json())
        == receipt
    )
    with sqlite3.connect(db_path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM evolution_stable_rollout_finalizations").fetchone()[0]
            == 1
        )
    with sqlite3.connect(fixture.store.db_path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM release_stable_member_finalizations").fetchone()[0]
            == 1
        )

    tool = EvolutionStableRolloutFinalizationTool(
        SimpleNamespace(evolution_stable_rollout_finalization_service=first_service)
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
            arguments = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**arguments),
            )

    rendered = render_stable_rollout_finalization(view)
    slash = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-rollout-finalization inspect "
        f"{authorization.authorization.authorization_id}",
    )
    assert strip_ansi(slash) == rendered


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_finalization_fails_closed_before_consuming_when_pointer_changed(
    tmp_path: Path,
) -> None:
    fixture, db_path, _control, store, clock, make_authorization = await _services(tmp_path)
    authorization_service = make_authorization()
    authorization = await authorization_service.issue(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
    )
    fixture.store.rollback(activated_at="2026-08-11T03:30:00+00:00")
    service = _finalization_service(fixture, authorization_service, db_path, clock)
    with pytest.raises(EvolutionStableRolloutFinalizationError) as error:
        await service.execute(authorization_id=authorization.authorization.authorization_id)
    assert error.value.code == "stable_rollout_finalization_authorization_not_current"
    assert await store.consumption(authorization.authorization.authorization_id) is None
    assert (
        fixture.store.get_stable_member_finalization(authorization.authorization.authorization_id)
        is None
    )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_finalization_preserves_fact_after_later_pointer_change(
    tmp_path: Path,
) -> None:
    fixture, db_path, _control, _store, clock, make_authorization = await _services(tmp_path)
    authorization_service = make_authorization()
    authorization = await authorization_service.issue(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
    )
    service = _finalization_service(fixture, authorization_service, db_path, clock)
    completed = await service.execute(authorization_id=authorization.authorization.authorization_id)
    assert completed.active_stable_member_authority
    fixture.store.rollback(activated_at="2026-08-11T04:30:00+00:00")
    historical = await service.inspect(
        authorization_id=authorization.authorization.authorization_id
    )
    assert historical.stable_member_completion_fact
    assert not historical.active_pointer_current
    assert not historical.active_stable_member_authority


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_finalization_recovers_after_authorization_consumption(
    tmp_path: Path,
) -> None:
    fixture, db_path, _control, _store, clock, make_authorization = await _services(tmp_path)
    authorization_service = make_authorization()
    view = await authorization_service.issue(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
    )
    item = view.authorization
    await authorization_service.consume(
        authorization_id=item.authorization_id,
        nonce_base64=item.start_nonce_base64,
        consumer_id=f"stable.finalizer:{item.installation_member_id}",
    )
    service = _finalization_service(fixture, authorization_service, db_path, clock)
    recovered = await service.execute(authorization_id=item.authorization_id)
    assert recovered.stable_member_completion_fact
    assert recovered.release_finalization_valid


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_finalization_recovers_release_event_before_evolution_receipt(
    tmp_path: Path,
) -> None:
    fixture, db_path, _control, _store, clock, make_authorization = await _services(tmp_path)
    authorization_service = make_authorization()
    view = await authorization_service.issue(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
    )
    item = view.authorization
    await authorization_service.consume(
        authorization_id=item.authorization_id,
        nonce_base64=item.start_nonce_base64,
        consumer_id=f"stable.finalizer:{item.installation_member_id}",
    )
    fixture.store.finalize_stable_member(
        ReleaseStableMemberFinalizationAuthority(
            authority_id=item.authorization_id,
            authority_sha256=item.authorization_sha256,
            completion_receipt_id=item.completion_receipt_id,
            completion_receipt_sha256=item.completion_receipt_sha256,
            installation_member_id=item.installation_member_id,
            expected_pointer_id=item.expected_active_pointer_id,
            expected_pointer_sha256=item.expected_active_pointer_sha256,
            expected_pointer_generation=item.expected_active_pointer_generation,
        ),
        finalized_at=clock().isoformat(),
    )
    service = _finalization_service(fixture, authorization_service, db_path, clock)
    recovered = await service.execute(authorization_id=item.authorization_id)
    assert recovered.stable_member_completion_fact
    with sqlite3.connect(db_path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM evolution_stable_rollout_finalizations").fetchone()[0]
            == 1
        )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_finalization_stops_if_control_changes_after_consumption(
    tmp_path: Path,
) -> None:
    fixture, db_path, control_store, _store, clock, make_authorization = await _services(tmp_path)
    authorization_service = make_authorization()
    view = await authorization_service.issue(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
    )
    item = view.authorization
    await authorization_service.consume(
        authorization_id=item.authorization_id,
        nonce_base64=item.start_nonce_base64,
        consumer_id=f"stable.finalizer:{item.installation_member_id}",
    )
    clock.value += timedelta(seconds=1)
    control = EvolutionRevalidationRolloutControlService(
        workspace_root=fixture.root,
        store=control_store,
        control_plane_key_provider=lambda: b"stable-rollout-test-key" * 2,
    )
    await control.pause(
        reason_code="stable_finalization_test_pause",
        actor=EvolutionRevalidationRolloutControlActor.MONITOR,
        changed_at=clock().isoformat(),
    )
    service = _finalization_service(fixture, authorization_service, db_path, clock)
    with pytest.raises(EvolutionStableRolloutFinalizationError) as error:
        await service.execute(authorization_id=item.authorization_id)
    assert error.value.code == "stable_rollout_finalization_sources_changed"
    assert fixture.store.get_stable_member_finalization(item.authorization_id) is None


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_finalization_rejects_expired_authorization(tmp_path: Path) -> None:
    fixture, db_path, _control, store, clock, make_authorization = await _services(tmp_path)
    authorization_service = make_authorization()
    authorization = await authorization_service.issue(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
        validity_seconds=60,
    )
    clock.value = datetime(2026, 8, 11, 4, 2, tzinfo=UTC)
    service = _finalization_service(fixture, authorization_service, db_path, clock)
    with pytest.raises(EvolutionStableRolloutFinalizationError) as error:
        await service.execute(authorization_id=authorization.authorization.authorization_id)
    assert error.value.code == "stable_rollout_finalization_authorization_not_current"
    assert await store.consumption(authorization.authorization.authorization_id) is None
