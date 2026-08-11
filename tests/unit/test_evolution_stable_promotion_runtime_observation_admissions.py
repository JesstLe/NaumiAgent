from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.stable_promotion_observation_contracts import (
    EvolutionStablePromotionObservationContract,
)
from naumi_agent.evolution.stable_promotion_runtime_observation_admissions import (
    EvolutionStablePromotionRuntimeObservationAdmissionError,
    EvolutionStablePromotionRuntimeObservationAdmissionService,
    EvolutionStablePromotionRuntimeObservationAdmissionStore,
)
from naumi_agent.evolution.stable_remote_population_finalizations import (
    EvolutionStableRemotePopulationFinalizationMember,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionStablePromotionRuntimeObservationAdmissionTool,
)
from tests.unit.test_evolution_revalidation_stable_runtime_exposures import (
    _exposure_context,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=lambda item: item.model_dump(mode="json"),
        ).encode()
    ).hexdigest()


def _contract(workspace: Path, deployment, *, finalized_at: str):
    intent = deployment.preparation.intent
    core = {
        "schema_version": 1,
        "policy_version": "evolution-stable-promotion-observation-contract-v1",
        "workspace_root": str(workspace.resolve()),
        "population_finalization_receipt_id": "evstableremotepopfinal_" + "1" * 24,
        "population_finalization_receipt_sha256": "2" * 64,
        "population_snapshot_id": intent.population_snapshot_id,
        "population_snapshot_sha256": intent.population_snapshot_sha256,
        "population_snapshot_sequence": intent.population_snapshot_sequence,
        "population_denominator": intent.population_denominator,
        "population_completion_receipt_id": "evstablepopcomplete_" + "3" * 24,
        "population_completion_receipt_sha256": "4" * 64,
        "population_completion_source_set_sha256": "5" * 64,
        "rollout_plan_id": intent.plan.plan_id,
        "rollout_plan_sha256": intent.plan.plan_sha256,
        "approval_decision_id": intent.plan.decision_id,
        "approval_decision_sha256": intent.plan.decision_sha256,
        "approval_source_set_sha256": intent.plan.decision_source_set_sha256,
        "promotion_input_id": intent.plan.promotion_input_id,
        "promotion_input_sha256": intent.plan.promotion_input_sha256,
        "experiment_contract_id": "evx_" + "6" * 24,
        "experiment_contract_sha256": "7" * 64,
        "experiment_authority_id": "evxauth_" + "8" * 24,
        "experiment_authority_sha256": "9" * 64,
        "workbench_session_id": "stable-promotion-runtime-session",
        "workbench_proposal_id": "stable-promotion-runtime-proposal",
        "proposal_id": "evp_" + "a" * 24,
        "proposal_kind": "code",
        "candidate_id": intent.plan.candidate_id,
        "candidate_revision": intent.plan.candidate_revision,
        "candidate_sha256": "b" * 64,
        "candidate_version": intent.candidate_version,
        "candidate_target": intent.installation_target,
        "eligible_surfaces": ["new_ui", "tui"],
        "required_chain_origin_kind": "startup",
        "required_chain_origin_sequence": 1,
        "operational_phases": ["running", "waiting"],
        "breach_phases": ["failed"],
        "censor_phases": ["draining", "stopped"],
        "minimum_observation_seconds": 3600,
        "minimum_operational_samples": 12,
        "maximum_sample_count": 5000,
        "gap_limit_rule": "sample_timeout_seconds",
        "latest_age_limit_rule": "sample_timeout_seconds",
        "require_constant_runtime_binding": True,
        "require_contiguous_sequence_and_hash": True,
        "approval_current_at_issue": True,
        "active_stable_runtime_at_issue": True,
        "proposal_binding_verified": True,
        "observation_contract_recorded": True,
        "long_term_metrics_recorded": False,
        "observation_window_authority": False,
        "promoted_outcome_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "execution_authority": False,
        "window_not_before_at": finalized_at,
    }
    digest = _digest(core)
    return EvolutionStablePromotionObservationContract.model_validate(
        {
            **core,
            "contract_id": f"evstablepromobserve_{digest[:24]}",
            "contract_sha256": digest,
        }
    )


def _member_sources(contract, deployment):
    intent = deployment.preparation.intent
    proof = intent.proof.payload
    pointer = deployment.activated_pointer
    auth = SimpleNamespace(
        authorization_id="evstableremotefinalauth_" + "c" * 24,
        authorization_sha256="d" * 64,
        population_snapshot_id=contract.population_snapshot_id,
        population_snapshot_sha256=contract.population_snapshot_sha256,
        completion_receipt_id=contract.population_completion_receipt_id,
        completion_receipt_sha256=contract.population_completion_receipt_sha256,
        installation_member_id=proof.member_id,
        stable_intent_id=intent.intent_id,
        candidate_version=intent.candidate_version,
        candidate_target=intent.installation_target,
        expected_active_pointer_id=pointer.pointer_id,
        expected_active_pointer_sha256=pointer.pointer_sha256,
        expected_active_pointer_generation=pointer.generation,
        expected_candidate_slot_id=intent.candidate_slot_id,
        expected_candidate_slot_sha256=intent.candidate_slot_sha256,
        expected_candidate_boot_receipt_id=deployment.preparation.boot_receipt.receipt_id,
        expected_candidate_boot_receipt_sha256=(
            deployment.preparation.boot_receipt.receipt_sha256
        ),
    )
    finalization = SimpleNamespace(
        finalization_id="relstablefinal_" + "e" * 24,
        finalization_sha256="f" * 64,
        authority=SimpleNamespace(installation_member_id=proof.member_id),
        active_pointer=pointer,
    )
    receipt = SimpleNamespace(
        receipt_id="evstableremotefinalreceipt_" + "0" * 24,
        receipt_sha256="1" * 64,
        execution_package=SimpleNamespace(
            authorization=SimpleNamespace(authorization=auth)
        ),
        submission=SimpleNamespace(
            result=SimpleNamespace(release_finalization=finalization)
        ),
    )
    member_core = {
        "schema_version": 1,
        "installation_member_id": proof.member_id,
        "installation_credential_id": proof.credential_id,
        "installation_credential_sha256": proof.credential_sha256,
        "installation_public_key_sha256": (
            intent.proof.installation_credential.payload.installation_public_key_sha256
        ),
        "member_receipt_id": receipt.receipt_id,
        "member_receipt_sha256": receipt.receipt_sha256,
        "authorization_id": auth.authorization_id,
        "authorization_sha256": auth.authorization_sha256,
        "authorization_attempt": 1,
        "grant_id": "evstableremotefinalgrant_" + "2" * 24,
        "grant_sha256": "3" * 64,
        "result_id": "evstableremotefinalresult_" + "4" * 24,
        "result_sha256": "5" * 64,
        "release_finalization_id": finalization.finalization_id,
        "release_finalization_sha256": finalization.finalization_sha256,
        "expected_pointer_id": pointer.pointer_id,
        "expected_pointer_sha256": pointer.pointer_sha256,
        "expected_pointer_generation": pointer.generation,
        "completed_at": contract.window_not_before_at,
        "recorded_at": contract.window_not_before_at,
    }
    member = EvolutionStableRemotePopulationFinalizationMember.model_validate(
        {**member_core, "member_source_sha256": _digest(member_core)}
    )
    return member, receipt


class _ContractStore:
    def __init__(self, db_path: Path, contract) -> None:
        self.db_path = db_path
        self.contract = contract

    async def get_by_finalization(self, receipt_id: str):
        if receipt_id == self.contract.population_finalization_receipt_id:
            return self.contract
        return None


class _ContractService:
    def __init__(self, store, finalization_service, contract) -> None:
        self.store = store
        self.finalization_service = finalization_service
        self.contract = contract
        self.authority = True
        self.record_calls = 0
        self.inspect_calls = 0

    async def record(self, *, finalization_receipt_id: str):
        self.record_calls += 1
        assert finalization_receipt_id == self.contract.population_finalization_receipt_id
        return SimpleNamespace(
            contract=self.contract,
            observation_contract_authority=self.authority,
        )

    async def inspect(self, *, contract):
        self.inspect_calls += 1
        assert contract == self.contract
        return SimpleNamespace(
            contract=contract,
            observation_contract_authority=self.authority,
        )


class _MemberStore:
    def __init__(self, db_path: Path, receipt) -> None:
        self.db_path = db_path
        self.receipt = receipt

    async def get_receipt(self, receipt_id: str):
        return self.receipt if receipt_id == self.receipt.receipt_id else None


class _FinalizationService:
    def __init__(
        self, workspace: Path, db_path: Path, member, member_receipt, contract
    ) -> None:
        self.workspace_root = workspace.resolve()
        self.store = SimpleNamespace(db_path=db_path)
        self.member_store = _MemberStore(db_path, member_receipt)
        self.member = member
        self.authority = True
        self.receipt = SimpleNamespace(
            receipt_id=contract.population_finalization_receipt_id,
            receipt_sha256=contract.population_finalization_receipt_sha256,
            members=(member,),
            stable_population_finalization_authority=True,
        )

    async def inspect(self, *, receipt_id: str):
        return SimpleNamespace(
            receipt=self.receipt,
            stable_population_finalization_authority=self.authority,
        )


class _DeploymentInspector:
    def __init__(self, service) -> None:
        self.service = service

    async def inspect_stable_deployment(self, *, intent_id: str):
        return await self.service.inspect(intent_id=intent_id)


async def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = await _exposure_context(tmp_path, monkeypatch)
    deployment = data["deployment_view"].receipt
    finalized_at = data["next_now"]()
    contract = _contract(tmp_path, deployment, finalized_at=finalized_at)
    member, member_receipt = _member_sources(contract, deployment)
    lifecycle = data["factory"].create(
        surface="new_ui",
        identity="stable-promotion-runtime",
    )
    assert await lifecycle.start()
    db_path = data["deployment_store"].db_path
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE evolution_stable_promotion_observation_contracts ("
            "contract_id TEXT PRIMARY KEY, contract_sha256 TEXT NOT NULL UNIQUE, "
            "population_finalization_receipt_id TEXT NOT NULL UNIQUE, "
            "population_completion_receipt_id TEXT NOT NULL UNIQUE, "
            "rollout_plan_id TEXT NOT NULL, experiment_contract_id TEXT NOT NULL, "
            "contract_json TEXT NOT NULL, window_not_before_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_stable_promotion_observation_contracts "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                contract.contract_id,
                contract.contract_sha256,
                contract.population_finalization_receipt_id,
                contract.population_completion_receipt_id,
                contract.rollout_plan_id,
                contract.experiment_contract_id,
                contract.model_dump_json(),
                contract.window_not_before_at,
            ),
        )
        db.execute(
            "CREATE TABLE evolution_stable_remote_population_finalizations ("
            "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_stable_remote_population_finalizations VALUES (?, ?)",
            (
                contract.population_finalization_receipt_id,
                contract.population_finalization_receipt_sha256,
            ),
        )
        db.execute(
            "CREATE TABLE evolution_stable_remote_finalization_receipts ("
            "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_stable_remote_finalization_receipts VALUES (?, ?)",
            (member_receipt.receipt_id, member_receipt.receipt_sha256),
        )
    finalization_service = _FinalizationService(
        tmp_path, db_path, member, member_receipt, contract
    )
    contract_store = _ContractStore(db_path, contract)
    contract_service = _ContractService(
        contract_store, finalization_service, contract
    )
    store = EvolutionStablePromotionRuntimeObservationAdmissionStore(db_path)
    deployment_inspector = _DeploymentInspector(data["deployment_service"])

    def build_service():
        return EvolutionStablePromotionRuntimeObservationAdmissionService(
            workspace_root=tmp_path,
            contract_store=contract_store,
            contract_service=contract_service,  # type: ignore[arg-type]
            finalization_service=finalization_service,  # type: ignore[arg-type]
            deployment_inspector=deployment_inspector,
            evolution_db_path=db_path,
            harness_store=data["harness_store"],
            store=EvolutionStablePromotionRuntimeObservationAdmissionStore(db_path),
        )

    return SimpleNamespace(
        **data,
        contract=contract,
        contract_service=contract_service,
        finalization_service=finalization_service,
        member=member,
        member_receipt=member_receipt,
        lifecycle=lifecycle,
        store=store,
        build_service=build_service,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="真实 release fixture 使用 POSIX executable")
async def test_real_release_runtime_admission_closes_authority_and_tamper_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = await _fixture(tmp_path, monkeypatch)
    service = data.build_service()
    arguments = {
        "finalization_receipt_id": data.contract.population_finalization_receipt_id,
        "stable_intent_id": data.intent_id,
        "subject_id": data.lifecycle.subject_id,
    }

    later = data.contract.model_copy(
        update={
            "window_not_before_at": (
                data.runtime_now[0] + timedelta(minutes=1)
            ).isoformat()
        }
    )
    data.contract_service.contract = later
    with pytest.raises(EvolutionStablePromotionRuntimeObservationAdmissionError) as old:
        await service.record(**arguments)
    assert old.value.code == "stable_promotion_runtime_admission_origin_predates_finalization"

    data.contract_service.contract = data.contract
    auth = data.member_receipt.execution_package.authorization.authorization
    expected_target = auth.candidate_target
    auth.candidate_target = "windows-x64"
    with pytest.raises(EvolutionStablePromotionRuntimeObservationAdmissionError) as wrong:
        await service.record(**arguments)
    assert wrong.value.code == "stable_promotion_runtime_admission_release_mismatch"
    auth.candidate_target = expected_target

    left, view = await asyncio.gather(
        service.record(**arguments),
        data.build_service().record(**arguments),
    )

    assert left == view
    assert view.status == "admitted"
    assert view.runtime_observation_input_authority
    assert view.population_member_authority
    assert view.active_deployment_authority
    assert view.exact_stable_release_authority
    assert view.admission.installation_member_id == data.member.installation_member_id
    assert view.admission.binding_id == data.lifecycle.release_binding().binding_id
    assert view.admission.origin_sequence == 1
    assert not view.admission.observation_window_authority
    assert not view.admission.promoted_outcome_authority
    record_calls = data.contract_service.record_calls
    inspect_calls = data.contract_service.inspect_calls
    assert (await service.inspect(admission=view.admission)).status == "admitted"
    assert data.contract_service.record_calls == record_calls
    assert data.contract_service.inspect_calls == inspect_calls + 1
    with sqlite3.connect(data.store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_stable_promotion_runtime_admissions"
        ).fetchone() == (1,)

    tool = EvolutionStablePromotionRuntimeObservationAdmissionTool(
        SimpleNamespace(
            evolution_stable_promotion_runtime_observation_admission_service=service
        )
    )
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    assert "Runtime observation input authority：`true`" in await tool.execute(
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
        "/evolution stable-promotion-admit-runtime "
        f"{arguments['finalization_receipt_id']} "
        f"{arguments['stable_intent_id']} {arguments['subject_id']}",
    )
    assert "Stable Promotion Runtime Observation Admission" in slash

    data.contract_service.authority = False
    stale = await service.inspect(admission=view.admission)
    assert not stale.observation_contract_authority
    assert not stale.runtime_observation_input_authority
    data.contract_service.authority = True

    with sqlite3.connect(data.store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_observation_contracts "
            "SET contract_sha256 = ? WHERE contract_id = ?",
            ("0" * 64, data.contract.contract_id),
        )
    with pytest.raises(EvolutionStablePromotionRuntimeObservationAdmissionError) as changed:
        await data.store.get(
            data.contract.contract_id,
            data.member.installation_member_id,
            data.lifecycle.subject_id,
        )
    assert changed.value.code == "stable_promotion_runtime_admission_dependency_changed"
    with sqlite3.connect(data.store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_observation_contracts "
            "SET contract_sha256 = ? WHERE contract_id = ?",
            (data.contract.contract_sha256, data.contract.contract_id),
        )

    with sqlite3.connect(data.harness_store.db_path) as db:
        db.execute(
            "DELETE FROM harness_runtime_release_observations WHERE subject_id = ?",
            (data.lifecycle.subject_id,),
        )
    stale = await service.inspect(admission=view.admission)
    assert not stale.startup_origin_authority
    assert not stale.runtime_observation_input_authority

    with sqlite3.connect(data.store.db_path) as db:
        db.execute(
            "UPDATE evolution_stable_promotion_runtime_admissions SET binding_id = ? "
            "WHERE admission_id = ?",
            ("hrreleasebinding_" + "0" * 24, view.admission.admission_id),
        )
    with pytest.raises(EvolutionStablePromotionRuntimeObservationAdmissionError) as corrupt:
        await data.store.get(
            data.contract.contract_id,
            data.member.installation_member_id,
            data.lifecycle.subject_id,
        )
    assert corrupt.value.code == "stable_promotion_runtime_admission_store_corrupt"


def test_invalid_external_ids_are_rejected_before_io(tmp_path: Path) -> None:
    store = EvolutionStablePromotionRuntimeObservationAdmissionStore(tmp_path / "missing.db")
    with pytest.raises(EvolutionStablePromotionRuntimeObservationAdmissionError) as invalid:
        asyncio.run(store.get("bad\ncontract", "bad-member", "bad subject"))
    assert invalid.value.code == "stable_promotion_runtime_admission_contract_id_invalid"
    with pytest.raises(EvolutionStablePromotionRuntimeObservationAdmissionError) as bad_id:
        asyncio.run(store.get_by_id("bad\nadmission"))
    assert bad_id.value.code == "stable_promotion_runtime_admission_id_invalid"
