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
    EvolutionRevalidationRolloutControlStore,
)
from naumi_agent.evolution.stable_rollout_authorizations import (
    EvolutionStableRolloutAuthorization,
    EvolutionStableRolloutAuthorizationError,
    EvolutionStableRolloutAuthorizationService,
    EvolutionStableRolloutAuthorizationStore,
    render_stable_rollout_authorization,
)
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionStableRolloutAuthorizationTool
from tests.unit.test_evolution_stable_rollback_readiness import (
    _readiness_fixture,
)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


async def _services(tmp_path: Path):
    fixture = await _readiness_fixture(tmp_path)
    db_path = fixture.data["store"].db_path

    def key() -> bytes:
        return b"stable-rollout-test-key" * 2

    control_store = EvolutionRevalidationRolloutControlStore(
        db_path,
        control_plane_key_provider=key,
    )
    store = EvolutionStableRolloutAuthorizationStore(db_path)
    clock = _Clock(datetime(2026, 8, 11, 4, 0, tzinfo=UTC))

    def make_service() -> EvolutionStableRolloutAuthorizationService:
        return EvolutionStableRolloutAuthorizationService(
            workspace_root=fixture.root,
            completion_inspector=fixture.data["service"],
            readiness_inspector=fixture.service,
            control_store=control_store,
            store=store,
            clock=clock,
            random_bytes=lambda size: b"n" * size,
        )

    return fixture, db_path, control_store, store, clock, make_service


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_authorization_is_durable_idempotent_and_single_use(
    tmp_path: Path,
) -> None:
    fixture, db_path, _control, _store, _clock, make_service = await _services(tmp_path)
    first_service = make_service()
    second_service = make_service()
    views = await asyncio.gather(
        *(
            (first_service if index % 2 == 0 else second_service).issue(
                completion_receipt_id=fixture.completion.receipt.receipt_id,
                intent_id=fixture.intent_id,
                validity_seconds=300,
            )
            for index in range(6)
        )
    )
    assert len({view.authorization.authorization_id for view in views}) == 1
    view = views[0]
    item = view.authorization
    assert view.stable_rollout_authority
    assert item.operation_scope == "binary_only"
    assert not item.config_data_mutation_allowed
    assert not item.promotion_authority
    assert EvolutionStableRolloutAuthorization.model_validate_json(item.model_dump_json()) == item
    assert "Stable rollout authority：`true`" in render_stable_rollout_authorization(view)
    with sqlite3.connect(db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM evolution_stable_rollout_authorizations"
        ).fetchone()[0]
    assert count == 1

    tool = EvolutionStableRolloutAuthorizationTool(
        SimpleNamespace(evolution_stable_rollout_authorization_service=first_service)
    )
    assert not tool.metadata.read_only
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

    slash = await execute_slash_command(
        _SlashEngine(),
        f"/evolution stable-rollout-authorization inspect {item.authorization_id}",
    )
    assert strip_ansi(slash) == render_stable_rollout_authorization(view)

    receipt = await first_service.consume(
        authorization_id=item.authorization_id,
        nonce_base64=item.start_nonce_base64,
        consumer_id="stable.executor-1",
    )
    assert receipt.single_use_consumed
    assert (
        await first_service.consume(
            authorization_id=item.authorization_id,
            nonce_base64=item.start_nonce_base64,
            consumer_id="stable.executor-1",
        )
        == receipt
    )
    consumed = await first_service.inspect(authorization_id=item.authorization_id)
    assert consumed.consumed
    assert not consumed.stable_rollout_authority
    with pytest.raises(EvolutionStableRolloutAuthorizationError) as error:
        await first_service.consume(
            authorization_id=item.authorization_id,
            nonce_base64=item.start_nonce_base64,
            consumer_id="stable.executor-2",
        )
    assert error.value.code == "stable_rollout_authorization_consumed"


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_control_generation_dynamically_revokes_authorization(
    tmp_path: Path,
) -> None:
    fixture, _db, control_store, _store, clock, make_service = await _services(tmp_path)
    service = make_service()
    view = await service.issue(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
    )
    control = EvolutionRevalidationRolloutControlService(
        workspace_root=fixture.root,
        store=control_store,
        control_plane_key_provider=lambda: b"stable-rollout-test-key" * 2,
    )
    clock.value += timedelta(seconds=1)
    await control.pause(
        reason_code="stable_rollout_test_pause",
        actor=EvolutionRevalidationRolloutControlActor.MONITOR,
        changed_at=clock.value.isoformat(),
    )
    revoked = await service.inspect(authorization_id=view.authorization.authorization_id)
    assert not revoked.control_current
    assert not revoked.stable_rollout_authority
    assert "rollout_control_changed" in revoked.invalidation_reasons
    with pytest.raises(EvolutionStableRolloutAuthorizationError) as error:
        await service.issue(
            completion_receipt_id=fixture.completion.receipt.receipt_id,
            intent_id=fixture.intent_id,
        )
    assert error.value.code == "stable_rollout_control_paused"


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_expiry_allows_chained_reissue_but_old_ticket_stays_revoked(
    tmp_path: Path,
) -> None:
    fixture, _db, _control, _store, clock, make_service = await _services(tmp_path)
    service = make_service()
    first = await service.issue(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
        validity_seconds=60,
    )
    clock.value += timedelta(seconds=61)
    expired = await service.inspect(authorization_id=first.authorization.authorization_id)
    assert expired.expired
    assert not expired.stable_rollout_authority
    second = await service.issue(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
        validity_seconds=60,
    )
    assert second.authorization.attempt == 2
    assert second.authorization.previous_authorization_id == first.authorization.authorization_id
    assert second.authorization.authorization_id != first.authorization.authorization_id
    assert second.stable_rollout_authority
