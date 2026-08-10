from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.evolution.stable_rollback_readiness import (
    EvolutionStableRollbackReadiness,
    EvolutionStableRollbackReadinessError,
    EvolutionStableRollbackReadinessService,
    render_stable_rollback_readiness,
)
from naumi_agent.release.slots import ReleaseSlotStore, host_release_target
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionStableRollbackReadinessTool
from tests.unit.test_evolution_stable_population_completions import (
    _authority_services,
)
from tests.unit.test_release_slots import _bundle


class _DeploymentInspector:
    def __init__(self, view) -> None:
        self.view = view
        self.calls: list[str] = []

    async def inspect_stable_deployment(self, *, intent_id: str):
        self.calls.append(intent_id)
        return self.view


async def _readiness_fixture(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    data = await _authority_services(root)
    completion = await data["service"].complete(
        snapshot_id=data["snapshot"].snapshot_id
    )
    target = host_release_target()
    store = ReleaseSlotStore(root / "release" / "installed")
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
    intent_id = completion.receipt.intent_ids[0]
    member_id = completion.receipt.installation_member_ids[0]
    intent = SimpleNamespace(
        intent_id=intent_id,
        proof=SimpleNamespace(
            installation_credential=SimpleNamespace(
                payload=SimpleNamespace(member_id=member_id)
            )
        ),
        population_snapshot_id=completion.receipt.population_snapshot_id,
        population_snapshot_sha256=completion.receipt.population_snapshot_sha256,
        plan=SimpleNamespace(
            plan_id=completion.receipt.plan_id,
            plan_sha256=completion.receipt.plan_sha256,
        ),
        candidate_version=completion.receipt.candidate_version,
        installation_target=target,
        candidate_slot_id=candidate.slot_id,
        candidate_slot_sha256=candidate.slot_sha256,
        candidate_manifest_sha256=candidate.manifest_sha256,
    )
    deployment = SimpleNamespace(
        receipt_id="evrestabledeployment_" + "a" * 24,
        receipt_sha256="b" * 64,
        workspace_root=str(root.resolve()),
        preparation=SimpleNamespace(intent=intent),
        activated_pointer=active,
    )
    deployment_view = SimpleNamespace(
        receipt=deployment,
        active_deployment_authority=True,
    )
    deployment_inspector = _DeploymentInspector(deployment_view)
    service = EvolutionStableRollbackReadinessService(
        workspace_root=root,
        completion_inspector=data["service"],
        deployment_inspector=deployment_inspector,
        release_slot_store=store,
    )
    return SimpleNamespace(
        root=root,
        data=data,
        completion=completion,
        intent_id=intent_id,
        member_id=member_id,
        store=store,
        baseline=baseline,
        candidate=candidate,
        prior=prior,
        active=active,
        deployment_view=deployment_view,
        deployment_inspector=deployment_inspector,
        service=service,
    )


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_readiness_verifies_real_previous_slot_and_shared_slash(
    tmp_path: Path,
) -> None:
    fixture = await _readiness_fixture(tmp_path)
    first = await fixture.service.inspect(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
    )
    second = await fixture.service.inspect(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
    )
    assert first == second
    assert first.installation_member_id == fixture.member_id
    assert first.expected_active_pointer_sha256 == fixture.active.pointer_sha256
    assert first.rollback_pointer_sha256 == fixture.prior.pointer_sha256
    assert first.rollback_slot_id == fixture.baseline.slot_id
    assert first.binary_rollback_readiness_authority
    assert not first.config_data_rollback_readiness_authority
    assert not first.stable_rollout_authority
    assert not first.promotion_authority
    assert EvolutionStableRollbackReadiness.model_validate_json(
        first.model_dump_json()
    ) == first
    rendered = render_stable_rollback_readiness(first)
    assert "Binary rollback ready" in rendered
    assert "Stable rollout authority：`false`" in rendered

    tool = EvolutionStableRollbackReadinessTool(
        SimpleNamespace(evolution_stable_rollback_readiness_service=fixture.service)
    )
    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert not tool.metadata.requires_confirmation
    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None) -> ToolResult:
            assert agent_name == "cli"
            registered = self.tool_registry.get(call.name)
            assert registered is not None
            arguments = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**arguments),
            )

    slash = await execute_slash_command(
        _SlashEngine(),
        "/evolution stable-rollback-readiness "
        f"{first.completion_receipt_id} {first.stable_intent_id}",
    )
    assert strip_ansi(slash) == rendered


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_readiness_fails_closed_after_pointer_moves(tmp_path: Path) -> None:
    fixture = await _readiness_fixture(tmp_path)
    first = await fixture.service.inspect(
        completion_receipt_id=fixture.completion.receipt.receipt_id,
        intent_id=fixture.intent_id,
    )
    assert first.binary_rollback_readiness_authority
    fixture.store.rollback(activated_at="2026-08-11T03:00:00+00:00")
    with pytest.raises(EvolutionStableRollbackReadinessError) as error:
        await fixture.service.inspect(
            completion_receipt_id=fixture.completion.receipt.receipt_id,
            intent_id=fixture.intent_id,
        )
    assert error.value.code == "stable_rollback_pointer_changed"


@pytest.mark.skipif(os.name == "nt", reason="真实 slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_readiness_rejects_intent_outside_completion(tmp_path: Path) -> None:
    fixture = await _readiness_fixture(tmp_path)
    with pytest.raises(EvolutionStableRollbackReadinessError) as error:
        await fixture.service.inspect(
            completion_receipt_id=fixture.completion.receipt.receipt_id,
            intent_id="evrestableintent_" + "f" * 24,
        )
    assert error.value.code == "stable_rollback_intent_not_in_completion"
