from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.post_rollback_runtime_observation_admissions import (
    EvolutionPostRollbackRuntimeObservationAdmissionError,
    EvolutionPostRollbackRuntimeObservationAdmissionService,
    EvolutionPostRollbackRuntimeObservationAdmissionStore,
)
from naumi_agent.harness.runtime_release_binding import build_runtime_release_binding
from naumi_agent.harness.store import HarnessStore
from naumi_agent.release.runtime_identity import ReleaseRuntimeIdentity
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionPostRollbackRuntimeObservationAdmissionTool,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class _ContractStore:
    def __init__(self, db_path: Path, contract) -> None:
        self.db_path = db_path
        self.contract = contract

    async def get_by_outcome(self, outcome_id: str):
        return self.contract if outcome_id == self.contract.outcome_id else None


class _ContractService:
    def __init__(self, contract) -> None:
        self.contract = contract
        self.authority = True

    async def record(self, *, request_id: str):
        assert request_id == self.contract.request_id
        return SimpleNamespace(
            contract=self.contract,
            observation_contract_authority=self.authority,
        )

    async def inspect(self, *, contract):
        assert contract == self.contract
        return SimpleNamespace(observation_contract_authority=self.authority)


def _runtime_identity(workspace: Path) -> ReleaseRuntimeIdentity:
    core = {
        "schema_version": 1,
        "policy_version": "naumi-release-runtime-identity-v1",
        "invocation_kind": "terminal_session",
        "pointer_id": "relactive_" + "1" * 24,
        "pointer_sha256": "2" * 64,
        "pointer_generation": 7,
        "slot_id": "relslot_" + "3" * 24,
        "slot_sha256": "4" * 64,
        "version": "1.2.3",
        "target": "darwin-arm64",
        "boot_receipt_id": "relboot_" + "5" * 24,
        "boot_receipt_sha256": "6" * 64,
        "binary_sha256": "7" * 64,
        "runtime_path": str((workspace / "bin" / "naumi").resolve()),
        "install_root": str((workspace / "install").resolve()),
        "checks": [
            "active_chain_verified",
            "manifest_verified",
            "boot_receipt_verified",
            "runtime_binary_verified",
            "environment_binding_verified",
        ],
        "runtime_process_started": True,
        "terminal_session_process": True,
        "health_probe_process": False,
        "verified_at": "2026-08-10T09:59:59+00:00",
    }
    digest = _digest(core)
    return ReleaseRuntimeIdentity.model_validate(
        {
            **core,
            "identity_id": f"relruntimeidentity_{digest[:24]}",
            "identity_sha256": digest,
        }
    )


async def _fixture(tmp_path: Path):
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    identity = _runtime_identity(workspace)
    contract = SimpleNamespace(
        workspace_root=str(workspace),
        outcome_id="evrerollbackout_" + "8" * 24,
        outcome_sha256="9" * 64,
        request_id="evrerollbackreq_" + "a" * 24,
        contract_id="evpostobservecontract_" + "b" * 24,
        contract_sha256="c" * 64,
        eligible_surfaces=("new_ui", "tui"),
        required_chain_origin_kind="startup",
        required_chain_origin_sequence=1,
        baseline_slot_id=identity.slot_id,
        baseline_slot_sha256=identity.slot_sha256,
        baseline_version=identity.version,
        baseline_target=identity.target,
        rollback_pointer_id=identity.pointer_id,
        rollback_pointer_sha256=identity.pointer_sha256,
        rollback_pointer_generation=identity.pointer_generation,
        baseline_binary_sha256=identity.binary_sha256,
        window_not_before_at="2026-08-10T10:00:00+00:00",
        recorded_at="2026-08-10T10:00:00+00:00",
    )
    session_db = tmp_path / "session.db"
    with sqlite3.connect(session_db) as db:
        db.execute(
            "CREATE TABLE evolution_post_rollback_observation_contracts ("
            "contract_id TEXT PRIMARY KEY, contract_sha256 TEXT NOT NULL, "
            "outcome_id TEXT NOT NULL UNIQUE)"
        )
        db.execute(
            "INSERT INTO evolution_post_rollback_observation_contracts VALUES (?, ?, ?)",
            (contract.contract_id, contract.contract_sha256, contract.outcome_id),
        )
    harness_store = HarnessStore(tmp_path / "harness.db")
    binding = build_runtime_release_binding(
        workspace_root=workspace,
        surface="new_ui",
        subject_id="runtime-admission",
        instance_id="instance-admission",
        epoch=1,
        runtime_identity=identity,
        bound_at="2026-08-10T10:00:01+00:00",
    )
    await harness_store.record_runtime_release_binding_startup(
        binding=binding,
        observed_at="2026-08-10T10:00:02+00:00",
        timeout_seconds=60,
        detail_code="runtime_starting",
    )
    contract_store = _ContractStore(session_db, contract)
    contract_service = _ContractService(contract)
    store = EvolutionPostRollbackRuntimeObservationAdmissionStore(session_db)
    service = EvolutionPostRollbackRuntimeObservationAdmissionService(
        workspace_root=workspace,
        contract_store=contract_store,  # type: ignore[arg-type]
        contract_service=contract_service,  # type: ignore[arg-type]
        harness_store=harness_store,
        store=store,
    )
    return service, store, contract, binding, contract_service, harness_store


@pytest.mark.asyncio
async def test_exact_managed_runtime_startup_is_admitted(tmp_path: Path) -> None:
    service, store, contract, binding, _contract_service, _harness_store = (
        await _fixture(tmp_path)
    )
    view = await service.record(
        request_id=contract.request_id,
        subject_id=binding.subject_id,
    )

    assert view.status == "admitted"
    assert view.runtime_observation_input_authority
    assert view.exact_baseline_authority
    assert view.admission.binding_id == binding.binding_id
    assert view.admission.origin_sequence == 1
    assert view.admission.origin_phase == "starting"
    assert view.admission.timeout_seconds == 60
    assert not view.admission.observation_window_authority
    assert await store.get(contract.outcome_id, binding.subject_id) == view.admission


@pytest.mark.asyncio
async def test_concurrent_runtime_admission_converges(tmp_path: Path) -> None:
    service, store, contract, binding, contract_service, harness_store = (
        await _fixture(tmp_path)
    )
    peer = EvolutionPostRollbackRuntimeObservationAdmissionService(
        workspace_root=service.workspace_root,
        contract_store=service.contract_store,
        contract_service=contract_service,  # type: ignore[arg-type]
        harness_store=harness_store,
        store=EvolutionPostRollbackRuntimeObservationAdmissionStore(store.db_path),
    )
    left, right = await asyncio.gather(
        service.record(request_id=contract.request_id, subject_id=binding.subject_id),
        peer.record(request_id=contract.request_id, subject_id=binding.subject_id),
    )
    assert left == right
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_post_rollback_runtime_admissions"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_different_runtime_baseline_is_rejected(tmp_path: Path) -> None:
    service, store, contract, binding, _contract_service, _harness_store = (
        await _fixture(tmp_path)
    )
    contract.baseline_binary_sha256 = "f" * 64
    with pytest.raises(EvolutionPostRollbackRuntimeObservationAdmissionError) as exc:
        await service.record(
            request_id=contract.request_id,
            subject_id=binding.subject_id,
        )
    assert exc.value.code == "post_rollback_runtime_admission_baseline_mismatch"
    assert await store.get(contract.outcome_id, binding.subject_id) is None


@pytest.mark.asyncio
async def test_runtime_started_before_contract_requires_fresh_restart(
    tmp_path: Path,
) -> None:
    service, store, contract, binding, _contract_service, _harness_store = (
        await _fixture(tmp_path)
    )
    contract.window_not_before_at = "2026-08-10T10:00:03+00:00"
    contract.recorded_at = contract.window_not_before_at
    with pytest.raises(EvolutionPostRollbackRuntimeObservationAdmissionError) as exc:
        await service.record(
            request_id=contract.request_id,
            subject_id=binding.subject_id,
        )
    assert exc.value.code == "post_rollback_runtime_admission_origin_predates_contract"
    assert "重启 Naumi" in str(exc.value)
    assert await store.get(contract.outcome_id, binding.subject_id) is None


@pytest.mark.asyncio
async def test_origin_or_contract_authority_change_revokes_admission(
    tmp_path: Path,
) -> None:
    service, _store, contract, binding, contract_service, harness_store = (
        await _fixture(tmp_path)
    )
    recorded = await service.record(
        request_id=contract.request_id,
        subject_id=binding.subject_id,
    )
    contract_service.authority = False
    stale = await service.inspect(admission=recorded.admission)
    assert stale.status == "stale"
    assert not stale.observation_contract_authority
    assert not stale.runtime_observation_input_authority

    contract_service.authority = True
    with sqlite3.connect(harness_store.db_path) as db:
        db.execute(
            "DELETE FROM harness_runtime_release_observations "
            "WHERE workspace_root = ? AND subject_id = ?",
            (str(service.workspace_root), binding.subject_id),
        )
    stale = await service.inspect(admission=recorded.admission)
    assert not stale.startup_origin_authority
    assert not stale.runtime_observation_input_authority


@pytest.mark.asyncio
async def test_runtime_admission_row_tamper_fails_closed(tmp_path: Path) -> None:
    service, store, contract, binding, _contract_service, _harness_store = (
        await _fixture(tmp_path)
    )
    recorded = await service.record(
        request_id=contract.request_id,
        subject_id=binding.subject_id,
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_runtime_admissions "
            "SET binding_id = ? WHERE admission_id = ?",
            ("hrreleasebinding_" + "0" * 24, recorded.admission.admission_id),
        )
    with pytest.raises(EvolutionPostRollbackRuntimeObservationAdmissionError) as exc:
        await store.get(contract.outcome_id, binding.subject_id)
    assert exc.value.code == "post_rollback_runtime_admission_store_corrupt"


@pytest.mark.asyncio
async def test_runtime_admission_tool_and_slash_share_service(tmp_path: Path) -> None:
    service, _store, contract, binding, _contract_service, _harness_store = (
        await _fixture(tmp_path)
    )
    tool = EvolutionPostRollbackRuntimeObservationAdmissionTool(
        SimpleNamespace(evolution_post_rollback_runtime_admission_service=service)
    )
    arguments = {
        "request_id": contract.request_id,
        "subject_id": binding.subject_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    rendered = await tool.execute(**arguments)
    assert "Startup Origin" in rendered
    assert "Runtime observation input authority：`true`" in rendered

    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            parsed = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**parsed),
            )

    slash = await execute_slash_command(
        _SlashEngine(),
        f"/evolution outcome-admit-runtime {contract.request_id} {binding.subject_id}",
    )
    assert "Post-Rollback Runtime Observation Admission" in slash
    assert binding.binding_id in slash
