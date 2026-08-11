from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import naumi_agent.evolution as evolution_api
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.stable_promotion_observation_contracts import (
    EvolutionStablePromotionObservationContract,
    EvolutionStablePromotionObservationContractError,
    EvolutionStablePromotionObservationContractService,
    EvolutionStablePromotionObservationContractStore,
)
from naumi_agent.evolution.stable_promotion_runtime_observation_admissions import (
    EvolutionStablePromotionRuntimeObservationAdmissionService,
)
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePromotionObservationContractTool,
    EvolutionStablePromotionRuntimeObservationAdmissionTool,
)
from tests.unit.test_evolution_stable_remote_population_finalizations import (
    _population_fixture,
)


class _FinalizationService:
    def __init__(self, db_path: Path, receipt) -> None:
        self.store = SimpleNamespace(db_path=db_path)
        self.receipt = receipt
        self.authority = True

    async def inspect(self, *, receipt_id: str):
        if receipt_id != self.receipt.receipt_id:
            raise ValueError("missing finalization")
        return SimpleNamespace(
            receipt=self.receipt,
            stable_population_finalization_authority=self.authority,
        )


class _CompletionService:
    def __init__(self, db_path: Path, receipt) -> None:
        self.store = SimpleNamespace(db_path=db_path)
        self.receipt = receipt
        self.authority = True

    async def inspect(self, *, receipt_id: str):
        if receipt_id != self.receipt.receipt_id:
            raise ValueError("missing completion")
        return SimpleNamespace(
            receipt=self.receipt,
            stable_population_completion_authority=self.authority,
        )


class _PlanService:
    def __init__(self, db_path: Path, plan) -> None:
        self.store = SimpleNamespace(db_path=db_path)
        self.plan = plan
        self.authority = True

    async def inspect(self, *, plan_id: str):
        if plan_id != self.plan.plan_id:
            raise ValueError("missing plan")
        return SimpleNamespace(
            plan=self.plan,
            decision_current=self.authority,
            current_rollout_eligible=self.authority,
        )


class _PromotionInputStore:
    def __init__(self, db_path: Path, item) -> None:
        self.db_path = db_path
        self.item = item

    async def get(self, contract_id: str):
        return self.item if contract_id == self.item.contract_id else None


class _ExperimentStore:
    def __init__(self, db_path: Path, item) -> None:
        self.db_path = db_path
        self.item = item

    async def get(self, workspace_root: Path, contract_id: str):
        if str(workspace_root) != self.item.workspace_root:
            return None
        return self.item if contract_id == self.item.contract_id else None


def _fixture(tmp_path: Path):
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    db_path = tmp_path / "session.db"
    finalization = SimpleNamespace(
        receipt_id="evstableremotepopfinal_" + "1" * 24,
        receipt_sha256="1" * 64,
        population_snapshot_id="relpopsnapshot_" + "2" * 24,
        population_snapshot_sha256="2" * 64,
        population_sequence=7,
        population_denominator=2,
        population_completion_receipt_id="evstablepopcomplete_" + "3" * 24,
        population_completion_receipt_sha256="3" * 64,
        candidate_version="1.2.3",
        finalized_at="2026-08-11T08:00:00+00:00",
    )
    completion = SimpleNamespace(
        receipt_id=finalization.population_completion_receipt_id,
        receipt_sha256=finalization.population_completion_receipt_sha256,
        source_set_sha256="4" * 64,
        workspace_root=str(root),
        population_snapshot_id=finalization.population_snapshot_id,
        population_snapshot_sha256=finalization.population_snapshot_sha256,
        population_snapshot_sequence=finalization.population_sequence,
        population_denominator=finalization.population_denominator,
        candidate_version=finalization.candidate_version,
        candidate_target="darwin-arm64",
        plan_id="evrerolloutplan_" + "5" * 24,
        plan_sha256="5" * 64,
    )
    plan = SimpleNamespace(
        plan_id=completion.plan_id,
        plan_sha256=completion.plan_sha256,
        workspace_root=str(root),
        decision_id="evreapprovaldecision_" + "6" * 24,
        decision_sha256="6" * 64,
        decision_source_set_sha256="7" * 64,
        promotion_input_id="evrevalpromoin_" + "8" * 24,
        promotion_input_sha256="8" * 64,
        contract_id="evrevalruntime_" + "9" * 24,
        contract_sha256="9" * 64,
        candidate_id="evc_" + "a" * 24,
        candidate_revision=4,
    )
    source = SimpleNamespace(
        session_id="session-promoted-1",
        workbench_proposal_id="proposal-promoted-1",
        proposal_id="evp_" + "b" * 24,
        proposal_kind="code",
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
        candidate_sha256="a" * 64,
    )
    experiment = SimpleNamespace(
        workspace_root=str(root),
        contract_id="evx_" + "c" * 24,
        contract_manifest_sha256="c" * 64,
        authority_id="evxauth_" + "d" * 24,
        authority_sha256="d" * 64,
        contract=SimpleNamespace(source=source),
    )
    prior = SimpleNamespace(
        experiment_contract_id=experiment.contract_id,
        experiment_contract_sha256=experiment.contract_manifest_sha256,
        candidate_sha256=source.candidate_sha256,
    )
    promotion_input = SimpleNamespace(
        input_id=plan.promotion_input_id,
        input_sha256=plan.promotion_input_sha256,
        contract_id=plan.contract_id,
        contract_sha256=plan.contract_sha256,
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
        prior_input=prior,
    )
    _seed_dependencies(db_path, finalization, completion, plan, promotion_input, experiment)
    finalization_service = _FinalizationService(db_path, finalization)
    completion_service = _CompletionService(db_path, completion)
    plan_service = _PlanService(db_path, plan)
    promotion_input_service = SimpleNamespace(
        store=_PromotionInputStore(db_path, promotion_input)
    )
    experiment_store = _ExperimentStore(db_path, experiment)
    store = EvolutionStablePromotionObservationContractStore(db_path)
    service = EvolutionStablePromotionObservationContractService(
        workspace_root=root,
        finalization_service=finalization_service,  # type: ignore[arg-type]
        completion_service=completion_service,  # type: ignore[arg-type]
        plan_service=plan_service,  # type: ignore[arg-type]
        promotion_input_service=promotion_input_service,  # type: ignore[arg-type]
        experiment_store=experiment_store,  # type: ignore[arg-type]
        store=store,
    )
    return SimpleNamespace(
        root=root,
        db_path=db_path,
        finalization=finalization,
        completion=completion,
        plan=plan,
        experiment=experiment,
        finalization_service=finalization_service,
        completion_service=completion_service,
        plan_service=plan_service,
        promotion_input_service=promotion_input_service,
        experiment_store=experiment_store,
        store=store,
        service=service,
    )


def _seed_dependencies(db_path, finalization, completion, plan, promotion_input, experiment):
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE evolution_stable_remote_population_finalizations "
            "(receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_stable_remote_population_finalizations VALUES (?, ?)",
            (finalization.receipt_id, finalization.receipt_sha256),
        )
        db.execute(
            "CREATE TABLE evolution_stable_population_completions "
            "(receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_stable_population_completions VALUES (?, ?)",
            (completion.receipt_id, completion.receipt_sha256),
        )
        db.execute(
            "CREATE TABLE evolution_revalidation_rollout_plans "
            "(plan_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_revalidation_rollout_plans VALUES (?, ?)",
            (plan.plan_id, plan.plan_sha256),
        )
        db.execute(
            "CREATE TABLE evolution_revalidation_promotion_inputs "
            "(input_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_revalidation_promotion_inputs VALUES (?, ?)",
            (promotion_input.input_id, promotion_input.input_sha256),
        )
        db.execute(
            "CREATE TABLE evolution_experiment_contracts "
            "(workspace_root TEXT NOT NULL, contract_id TEXT NOT NULL, "
            "authority_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_experiment_contracts VALUES (?, ?, ?)",
            (
                experiment.workspace_root,
                experiment.contract_id,
                experiment.authority_sha256,
            ),
        )


def _upper_sources(root: Path, completion):
    plan = SimpleNamespace(
        plan_id=completion.plan_id,
        plan_sha256=completion.plan_sha256,
        workspace_root=str(root),
        decision_id="evreapprovaldecision_" + "6" * 24,
        decision_sha256="6" * 64,
        decision_source_set_sha256="7" * 64,
        promotion_input_id="evrevalpromoin_" + "8" * 24,
        promotion_input_sha256="8" * 64,
        contract_id="evrevalruntime_" + "9" * 24,
        contract_sha256="9" * 64,
        candidate_id="evc_" + "a" * 24,
        candidate_revision=4,
    )
    source = SimpleNamespace(
        session_id="session-real-population",
        workbench_proposal_id="proposal-real-population",
        proposal_id="evp_" + "b" * 24,
        proposal_kind="code",
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
        candidate_sha256="a" * 64,
    )
    experiment = SimpleNamespace(
        workspace_root=str(root),
        contract_id="evx_" + "c" * 24,
        contract_manifest_sha256="c" * 64,
        authority_id="evxauth_" + "d" * 24,
        authority_sha256="d" * 64,
        contract=SimpleNamespace(source=source),
    )
    promotion_input = SimpleNamespace(
        input_id=plan.promotion_input_id,
        input_sha256=plan.promotion_input_sha256,
        contract_id=plan.contract_id,
        contract_sha256=plan.contract_sha256,
        candidate_id=plan.candidate_id,
        candidate_revision=plan.candidate_revision,
        prior_input=SimpleNamespace(
            experiment_contract_id=experiment.contract_id,
            experiment_contract_sha256=experiment.contract_manifest_sha256,
            candidate_sha256=source.candidate_sha256,
        ),
    )
    return plan, promotion_input, experiment


def _seed_upper_dependencies(db_path, plan, promotion_input, experiment):
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE evolution_revalidation_rollout_plans "
            "(plan_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_revalidation_rollout_plans VALUES (?, ?)",
            (plan.plan_id, plan.plan_sha256),
        )
        db.execute(
            "CREATE TABLE evolution_revalidation_promotion_inputs "
            "(input_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_revalidation_promotion_inputs VALUES (?, ?)",
            (promotion_input.input_id, promotion_input.input_sha256),
        )
        db.execute(
            "CREATE TABLE evolution_experiment_contracts "
            "(workspace_root TEXT NOT NULL, contract_id TEXT NOT NULL, "
            "authority_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_experiment_contracts VALUES (?, ?, ?)",
            (
                experiment.workspace_root,
                experiment.contract_id,
                experiment.authority_sha256,
            ),
        )


@pytest.mark.asyncio
async def test_contract_freezes_exact_success_lineage_without_promoting(
    tmp_path: Path,
) -> None:
    data = _fixture(tmp_path)
    view = await data.service.record(
        finalization_receipt_id=data.finalization.receipt_id
    )

    assert view.status == "recorded"
    assert view.observation_contract_authority
    assert view.approval_authority
    assert view.active_stable_runtime_authority
    assert view.proposal_binding_authority
    assert view.contract.workbench_proposal_id == "proposal-promoted-1"
    assert view.contract.window_not_before_at == data.finalization.finalized_at
    assert view.contract.minimum_observation_seconds == 3600
    assert view.contract.minimum_operational_samples == 12
    assert not view.contract.long_term_metrics_recorded
    assert not view.contract.observation_window_authority
    assert not view.contract.promoted_outcome_authority
    assert not view.contract.learning_authority
    assert await data.store.get_by_finalization(data.finalization.receipt_id) == view.contract


@pytest.mark.skipif(os.name == "nt", reason="真实 release-slot fixture 使用 POSIX executable")
@pytest.mark.asyncio
async def test_real_signed_population_finalization_opens_contract_only_after_completion(
    tmp_path: Path,
) -> None:
    population = await _population_fixture(tmp_path / "real-population")
    finalization_view = await population.service.complete(
        snapshot_id=population.snapshot.snapshot_id
    )
    readiness = population.fixture.data.fixture
    completion_service = readiness.data["service"]
    completion = readiness.completion.receipt
    assert finalization_view.receipt.population_completion_receipt_id == completion.receipt_id
    plan, promotion_input, experiment = _upper_sources(readiness.root.resolve(), completion)
    _seed_upper_dependencies(population.fixture.db_path, plan, promotion_input, experiment)
    plan_service = _PlanService(population.fixture.db_path, plan)
    promotion_input_service = SimpleNamespace(
        store=_PromotionInputStore(population.fixture.db_path, promotion_input)
    )
    experiment_store = _ExperimentStore(population.fixture.db_path, experiment)
    service = EvolutionStablePromotionObservationContractService(
        workspace_root=readiness.root,
        finalization_service=population.service,
        completion_service=completion_service,
        plan_service=plan_service,  # type: ignore[arg-type]
        promotion_input_service=promotion_input_service,  # type: ignore[arg-type]
        experiment_store=experiment_store,  # type: ignore[arg-type]
        store=EvolutionStablePromotionObservationContractStore(
            population.fixture.db_path
        ),
    )

    view = await service.record(
        finalization_receipt_id=finalization_view.receipt.receipt_id
    )

    assert view.observation_contract_authority
    assert view.contract.population_denominator == 2
    assert view.contract.population_snapshot_id == population.snapshot.snapshot_id
    assert view.contract.window_not_before_at == finalization_view.receipt.finalized_at
    assert not view.promoted_outcome_authority


@pytest.mark.asyncio
async def test_concurrent_recording_converges_and_dynamic_authority_revokes(
    tmp_path: Path,
) -> None:
    data = _fixture(tmp_path)
    peer = EvolutionStablePromotionObservationContractService(
        workspace_root=data.root,
        finalization_service=data.finalization_service,  # type: ignore[arg-type]
        completion_service=data.completion_service,  # type: ignore[arg-type]
        plan_service=data.plan_service,  # type: ignore[arg-type]
        promotion_input_service=data.promotion_input_service,  # type: ignore[arg-type]
        experiment_store=data.experiment_store,  # type: ignore[arg-type]
        store=EvolutionStablePromotionObservationContractStore(data.db_path),
    )
    left, right = await asyncio.gather(
        data.service.record(finalization_receipt_id=data.finalization.receipt_id),
        peer.record(finalization_receipt_id=data.finalization.receipt_id),
    )
    assert left == right
    with sqlite3.connect(data.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_stable_promotion_observation_contracts"
        ).fetchone() == (1,)

    data.finalization_service.authority = False
    stale = await data.service.inspect(contract=left.contract)
    assert stale.status == "stale"
    assert not stale.active_stable_runtime_authority
    assert not stale.observation_contract_authority
    assert not stale.promoted_outcome_authority


@pytest.mark.asyncio
async def test_invalid_id_and_durable_dependency_tamper_fail_closed(
    tmp_path: Path,
) -> None:
    data = _fixture(tmp_path)
    with pytest.raises(EvolutionStablePromotionObservationContractError) as invalid:
        await data.service.record(finalization_receipt_id="../bad")
    assert invalid.value.code == "stable_promotion_observation_finalization_id_invalid"

    view = await data.service.record(
        finalization_receipt_id=data.finalization.receipt_id
    )
    with sqlite3.connect(data.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_observation_contracts "
            "SET contract_sha256 = ? WHERE contract_id = ?",
            ("f" * 64, view.contract.contract_id),
        )
    with pytest.raises(EvolutionStablePromotionObservationContractError) as corrupt:
        await data.store.get_by_finalization(data.finalization.receipt_id)
    assert corrupt.value.code == "stable_promotion_observation_contract_store_corrupt"


@pytest.mark.asyncio
async def test_upstream_durable_digest_change_revokes_without_rewriting_contract(
    tmp_path: Path,
) -> None:
    data = _fixture(tmp_path)
    recorded = await data.service.record(
        finalization_receipt_id=data.finalization.receipt_id
    )
    before = recorded.contract.model_dump_json()
    with sqlite3.connect(data.db_path) as db:
        db.execute(
            "UPDATE evolution_revalidation_rollout_plans SET plan_sha256 = ? "
            "WHERE plan_id = ?",
            ("0" * 64, data.plan.plan_id),
        )
    stale = await data.service.inspect(contract=recorded.contract)
    assert stale.status == "stale"
    assert not stale.durable_contract_valid
    assert stale.approval_authority
    assert stale.active_stable_runtime_authority
    assert stale.proposal_binding_authority
    assert recorded.contract.model_dump_json() == before


def test_contract_policy_tamper_is_rejected(tmp_path: Path) -> None:
    data = _fixture(tmp_path)
    view = asyncio.run(
        data.service.record(finalization_receipt_id=data.finalization.receipt_id)
    )
    payload = view.contract.model_dump(mode="json")
    payload["promoted_outcome_authority"] = True
    with pytest.raises(ValidationError):
        EvolutionStablePromotionObservationContract.model_validate_json(
            json.dumps(payload)
        )
    payload = view.contract.model_dump(mode="json")
    payload["workbench_proposal_id"] = "proposal\nforged"
    with pytest.raises(ValidationError):
        EvolutionStablePromotionObservationContract.model_validate_json(
            json.dumps(payload)
        )


@pytest.mark.asyncio
async def test_tool_and_slash_share_service_without_confirmation(tmp_path: Path) -> None:
    data = _fixture(tmp_path)
    tool = EvolutionStablePromotionObservationContractTool(
        SimpleNamespace(
            evolution_stable_promotion_observation_contract_service=data.service
        )
    )
    arguments = {"finalization_receipt_id": data.finalization.receipt_id}
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    rendered = await tool.execute(**arguments)
    assert "Stable Promotion Observation Contract" in rendered
    assert "长期指标 / Window / Promoted Outcome authority：`false / false / false`" in (
        rendered
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
        "/evolution stable-promotion-observation-contract "
        + data.finalization.receipt_id,
    )
    plain = strip_ansi(slash)
    assert "Stable Promotion Observation Contract" in plain
    assert data.finalization.receipt_id in plain
    assert "Promoted Outcome authority" in plain


@pytest.mark.asyncio
async def test_engine_composes_contract_service_and_tool(tmp_path: Path) -> None:
    assert (
        evolution_api.EvolutionStablePromotionObservationContractService
        is EvolutionStablePromotionObservationContractService
    )
    assert (
        evolution_api.EvolutionStablePromotionRuntimeObservationAdmissionService
        is EvolutionStablePromotionRuntimeObservationAdmissionService
    )
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
        tool = engine.tool_registry.get(
            "evolution_stable_promotion_observation_contract"
        )
        assert isinstance(tool, EvolutionStablePromotionObservationContractTool)
        assert tool._engine is engine
        assert (
            engine.evolution_stable_promotion_observation_contract_service.store
            is engine.evolution_stable_promotion_observation_contract_store
        )
        assert (
            engine.evolution_stable_promotion_observation_contract_store.db_path
            == session_db.resolve()
        )
        admission_tool = engine.tool_registry.get(
            "evolution_stable_promotion_runtime_observation_admission"
        )
        assert isinstance(
            admission_tool,
            EvolutionStablePromotionRuntimeObservationAdmissionTool,
        )
        assert isinstance(
            engine.evolution_stable_promotion_runtime_observation_admission_service,
            EvolutionStablePromotionRuntimeObservationAdmissionService,
        )
    finally:
        await engine.shutdown()
