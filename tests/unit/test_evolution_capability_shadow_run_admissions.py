from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionReceiptStore,
    PermissionDecisionSource,
)
from naumi_agent.daemons.run_delegation_grants import (
    RunDelegationGrantAuthority,
    RunDelegationGrantStore,
)
from naumi_agent.evolution.capability_shadow_run_admissions import (
    CapabilityShadowRunAdmissionError,
    EvolutionCapabilityShadowRunAdmissionService,
    EvolutionCapabilityShadowRunAdmissionStore,
    render_capability_shadow_run_admission,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import (
    TOOL_PERMISSIONS,
    PermissionChecker,
    PermissionMode,
    PermissionReasonCode,
    PermissionRiskLevel,
)
from tests.unit.test_evolution_capability_shadow_observation_contracts import (
    CANDIDATE_ID,
    NOW,
)
from tests.unit.test_evolution_capability_shadow_observation_contracts import (
    _service as _observation_service,
)

RUNNER_TOOL = "evolution_capability_shadow_observation_run"


@pytest.mark.asyncio
async def test_real_engine_registers_shadow_run_admission_with_bounded_permission(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
    ))
    try:
        tool = engine.tool_registry.get_exact(
            "evolution_capability_shadow_run_admission"
        )
        assert tool is not None
        assert tool.metadata.delegated_tool_names == (RUNNER_TOOL,)
        assert tool.metadata.requires_persistent_authorization is True
        rule = TOOL_PERMISSIONS[tool.name]
        assert rule.allowed_modes == [
            PermissionMode.BYPASS,
            PermissionMode.PERMISSIVE,
            PermissionMode.MODERATE,
            PermissionMode.STRICT,
        ]
        assert rule.requires_confirmation is False
        assert rule.max_calls_per_session == 20
        assert rule.risk_level is PermissionRiskLevel.MEDIUM
        decision = PermissionChecker(
            PermissionMode.MODERATE,
            allowed_dirs=[str(tmp_path)],
        ).check(tool.name, {}, tool=tool)
        assert decision.allowed is True
        assert decision.code is not PermissionReasonCode.UNKNOWN_TOOL
        missing = await engine.evolution_capability_shadow_run_admission_service.inspect(
            CANDIDATE_ID
        )
        assert missing.state == "missing"
        assert missing.provider_call_authorized is False
    finally:
        await engine.shutdown()


async def _admission_fixture(tmp_path: Path):
    observation, descriptor, _, _, model = _observation_service(tmp_path)
    await observation.compile(CANDIDATE_ID)
    permission_store = PermissionDecisionReceiptStore(
        tmp_path / "runtime" / "permission-decisions.db"
    )
    harness_store = HarnessStore(tmp_path / "runtime" / "harness.db")
    grant_store = RunDelegationGrantStore(
        tmp_path / "runtime" / "run-delegation-grants.db"
    )
    grant_authority = RunDelegationGrantAuthority(
        store=grant_store,
        permission_store=permission_store,
        harness_store=harness_store,
        workspace_root=tmp_path,
    )
    clock = [NOW + timedelta(seconds=1)]
    service = EvolutionCapabilityShadowRunAdmissionService(
        workspace_root=tmp_path,
        observation_contract_service=observation,
        store=EvolutionCapabilityShadowRunAdmissionStore(tmp_path / "evolution.db"),
        harness_store=harness_store,
        permission_store=permission_store,
        run_grant_authority=grant_authority,
        runtime_instance_id="evcsrart_" + "1" * 24,
        now=lambda: clock[0].isoformat(),
    )
    return (
        service,
        observation,
        descriptor,
        model,
        permission_store,
        harness_store,
        grant_store,
        clock,
    )


async def _parent(
    store: PermissionDecisionReceiptStore,
    *,
    action: str,
    run_id: str,
    decided_at: datetime,
):
    return await store.issue(
        request_id=f"shadow-{action}-{run_id}",
        session_id="session-shadow",
        run_id=run_id,
        call_id=f"shadow-{action}-{run_id}",
        agent_name="main",
        tool_name="evolution_capability_shadow_run_admission",
        tool_family="evolution_capability_shadow",
        arguments={
            "action": action,
            "candidate_id": CANDIDATE_ID,
            "run_id": run_id,
        },
        outcome=PermissionDecisionOutcome.BYPASS_ENABLED,
        actor=PermissionDecisionActor.RUNTIME,
        source=PermissionDecisionSource.BYPASS,
        permission_mode=PermissionMode.BYPASS,
        risk_level="medium",
        delegated_tool_names=(RUNNER_TOOL,),
        decided_at=decided_at.isoformat(),
    )


@pytest.mark.asyncio
async def test_shadow_run_admission_reserves_exact_bounded_authority(
    tmp_path: Path,
) -> None:
    service, _, _, _, permissions, harness, _, clock = await _admission_fixture(tmp_path)
    parent = await _parent(
        permissions,
        action="issue",
        run_id="shadow-run-a",
        decided_at=clock[0],
    )

    view = await service.issue(
        candidate_id=CANDIDATE_ID,
        run_id=parent.run_id,
        parent_permission=parent,
    )

    assert view.state == "ready"
    assert view.runner_input_eligible is True
    assert view.provider_call_authorized is True
    assert view.provider_call_completed is False
    assert view.observation_recorded is False
    assert view.candidate_execution_authorized is False
    assert view.side_effects_allowed is False
    assert view.activation_authorized is False
    assert view.admission is not None
    item = view.admission
    assert item.max_model_calls >= 2
    assert item.delegated_tool_name == RUNNER_TOOL
    assert item.parent_permission_receipt_sha256 == parent.receipt_sha256
    lease = await harness.get_run_lease(
        workspace_root=tmp_path,
        run_kind="runtime",
        run_id=parent.run_id,
    )
    assert lease is not None
    assert lease.owner_id == item.lease_owner_id
    assert lease.epoch == item.lease_epoch
    assert "尚未发送任何 Provider 请求" in render_capability_shadow_run_admission(view)


@pytest.mark.asyncio
async def test_shadow_run_admission_revokes_on_contract_grant_runtime_and_expiry(
    tmp_path: Path,
) -> None:
    (
        service,
        observation,
        descriptor,
        _,
        permissions,
        harness,
        grant_store,
        clock,
    ) = await _admission_fixture(tmp_path)
    parent = await _parent(
        permissions,
        action="issue",
        run_id="shadow-run-b",
        decided_at=clock[0],
    )
    ready = await service.issue(
        candidate_id=CANDIDATE_ID,
        run_id=parent.run_id,
        parent_permission=parent,
    )
    assert ready.admission is not None

    detached = EvolutionCapabilityShadowRunAdmissionService(
        workspace_root=tmp_path,
        observation_contract_service=observation,
        store=EvolutionCapabilityShadowRunAdmissionStore(tmp_path / "evolution.db"),
        harness_store=harness,
        permission_store=permissions,
        run_grant_authority=RunDelegationGrantAuthority(
            store=RunDelegationGrantStore(grant_store.db_path),
            permission_store=PermissionDecisionReceiptStore(permissions.db_path),
            harness_store=HarnessStore(harness.db_path),
            workspace_root=tmp_path,
        ),
        runtime_instance_id="evcsrart_" + "2" * 24,
        now=lambda: clock[0].isoformat(),
    )
    assert (await detached.inspect(CANDIDATE_ID)).state == "runtime_detached"

    descriptor.state = "revoked"
    assert (await service.inspect(CANDIDATE_ID)).state == "contract_revoked"
    descriptor.state = "ready"

    await service.run_grant_authority.revoke(
        grant_id=ready.admission.run_grant_id,
        reason="test_revoked",
        revoked_at=clock[0].isoformat(),
    )
    assert (await service.inspect(CANDIDATE_ID)).state == "authority_revoked"

    clock[0] = datetime.fromisoformat(ready.admission.expires_at) + timedelta(seconds=1)
    assert (await service.inspect(CANDIDATE_ID)).state == "expired"


@pytest.mark.asyncio
async def test_shadow_run_admission_revoke_cleans_grant_and_exact_lease(
    tmp_path: Path,
) -> None:
    service, _, _, _, permissions, harness, _, clock = await _admission_fixture(tmp_path)
    issue_parent = await _parent(
        permissions,
        action="issue",
        run_id="shadow-run-c",
        decided_at=clock[0],
    )
    ready = await service.issue(
        candidate_id=CANDIDATE_ID,
        run_id=issue_parent.run_id,
        parent_permission=issue_parent,
    )
    assert ready.admission is not None
    revoke_parent = await _parent(
        permissions,
        action="revoke",
        run_id=issue_parent.run_id,
        decided_at=clock[0] + timedelta(seconds=1),
    )
    clock[0] += timedelta(seconds=1)

    revoked = await service.revoke(
        candidate_id=CANDIDATE_ID,
        run_id=issue_parent.run_id,
        parent_permission=revoke_parent,
    )

    assert revoked.state == "revoked"
    assert revoked.provider_call_authorized is False
    grant = await service.run_grant_authority.validate(
        grant_id=ready.admission.run_grant_id,
        now=clock[0].isoformat(),
    )
    assert grant.allowed is False
    lease = await harness.get_run_lease(
        workspace_root=tmp_path,
        run_kind="runtime",
        run_id=issue_parent.run_id,
    )
    assert lease is not None
    assert lease.state.value == "released"


@pytest.mark.asyncio
async def test_shadow_run_admission_concurrency_allows_one_active_owner(
    tmp_path: Path,
) -> None:
    service, observation, _, _, permissions, harness, grant_store, clock = (
        await _admission_fixture(tmp_path)
    )
    second = EvolutionCapabilityShadowRunAdmissionService(
        workspace_root=tmp_path,
        observation_contract_service=observation,
        store=EvolutionCapabilityShadowRunAdmissionStore(tmp_path / "evolution.db"),
        harness_store=harness,
        permission_store=permissions,
        run_grant_authority=RunDelegationGrantAuthority(
            store=RunDelegationGrantStore(grant_store.db_path),
            permission_store=permissions,
            harness_store=harness,
            workspace_root=tmp_path,
        ),
        runtime_instance_id="evcsrart_" + "2" * 24,
        now=lambda: clock[0].isoformat(),
    )
    first_parent = await _parent(
        permissions,
        action="issue",
        run_id="shadow-run-d1",
        decided_at=clock[0],
    )
    second_parent = await _parent(
        permissions,
        action="issue",
        run_id="shadow-run-d2",
        decided_at=clock[0],
    )

    outcomes = await asyncio.gather(
        service.issue(
            candidate_id=CANDIDATE_ID,
            run_id=first_parent.run_id,
            parent_permission=first_parent,
        ),
        second.issue(
            candidate_id=CANDIDATE_ID,
            run_id=second_parent.run_id,
            parent_permission=second_parent,
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    assert sum(isinstance(item, CapabilityShadowRunAdmissionError) for item in outcomes) == 1


@pytest.mark.asyncio
async def test_shadow_run_admission_store_rejects_relational_tamper(
    tmp_path: Path,
) -> None:
    service, _, _, _, permissions, _, _, clock = await _admission_fixture(tmp_path)
    parent = await _parent(
        permissions,
        action="issue",
        run_id="shadow-run-e",
        decided_at=clock[0],
    )
    ready = await service.issue(
        candidate_id=CANDIDATE_ID,
        run_id=parent.run_id,
        parent_permission=parent,
    )
    assert ready.admission is not None
    with sqlite3.connect(tmp_path / "evolution.db") as db:
        db.execute(
            "UPDATE evolution_capability_shadow_run_admissions "
            "SET run_grant_id = ? WHERE admission_id = ?",
            ("tampered-grant", ready.admission.admission_id),
        )
        db.commit()

    with pytest.raises(CapabilityShadowRunAdmissionError, match="持久裁决字段"):
        await service.store.latest(tmp_path, CANDIDATE_ID)


@pytest.mark.asyncio
async def test_shadow_run_admission_rejects_unstored_parent_projection(
    tmp_path: Path,
) -> None:
    service, _, _, _, permissions, _, _, clock = await _admission_fixture(tmp_path)
    parent = await _parent(
        permissions,
        action="issue",
        run_id="shadow-run-f",
        decided_at=clock[0],
    )
    forged = replace(parent, agent_name="forged-agent")

    with pytest.raises(CapabilityShadowRunAdmissionError, match="持久 authority"):
        await service.issue(
            candidate_id=CANDIDATE_ID,
            run_id=parent.run_id,
            parent_permission=forged,
        )
