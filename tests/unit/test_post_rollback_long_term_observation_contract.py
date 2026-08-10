from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.post_rollback_long_term_observation_contracts import (
    EvolutionPostRollbackLongTermObservationContract,
    EvolutionPostRollbackLongTermObservationContractError,
    EvolutionPostRollbackLongTermObservationContractService,
    EvolutionPostRollbackLongTermObservationContractStore,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionPostRollbackLongTermObservationContractTool,
)


class _ArtifactStore:
    def __init__(self, db_path: Path, artifact) -> None:
        self.db_path = db_path
        self.artifact = artifact

    async def get_by_outcome(self, outcome_id: str):
        return self.artifact if outcome_id == self.artifact.outcome_id else None


class _MatrixService:
    def __init__(self, matrix) -> None:
        self.matrix = matrix
        self.authority = True

    async def record(self, *, request_id: str):
        assert request_id == self.matrix.request_id
        return SimpleNamespace(
            matrix=self.matrix,
            behavioral_evaluation_authority=self.authority,
        )

    async def inspect(self, *, matrix):
        assert matrix == self.matrix
        return SimpleNamespace(behavioral_evaluation_authority=self.authority)


class _VerificationService:
    def __init__(self, verification) -> None:
        self.verification = verification
        self.authority = True

    async def inspect(self, *, verification):
        assert verification == self.verification
        return SimpleNamespace(verification_authority=self.authority)


def _fixture(
    tmp_path: Path,
    *,
    outcome_id: str | None = None,
    outcome_sha256: str | None = None,
    request_id: str | None = None,
):
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir(exist_ok=True)
    db_path = tmp_path / "session.db"
    outcome_id = outcome_id or "evrerollbackout_" + "1" * 24
    outcome_sha256 = outcome_sha256 or "4" * 64
    request_id = request_id or "evrerollbackreq_" + "2" * 24
    verification_id = "evpostrollback_" + "3" * 24
    matrix = SimpleNamespace(
        workspace_root=str(workspace),
        outcome_id=outcome_id,
        outcome_sha256=outcome_sha256,
        request_id=request_id,
        matrix_id="evpostmatrix_" + "5" * 24,
        matrix_sha256="6" * 64,
        runtime_verification_id=verification_id,
        runtime_verification_sha256="7" * 64,
        before_after_evidence_id="evbeforeafter_" + "8" * 24,
        before_after_evidence_sha256="9" * 64,
        final_evaluation_id="evfinal_" + "a" * 24,
        final_evaluation_sha256="b" * 64,
        behavioral_evaluation_recorded=True,
        recovery_verdict="recovered",
        recorded_at="2026-08-10T09:02:00+00:00",
    )
    verification = SimpleNamespace(
        workspace_root=str(workspace),
        outcome_id=outcome_id,
        outcome_sha256=matrix.outcome_sha256,
        request_id=request_id,
        verification_id=verification_id,
        verification_sha256=matrix.runtime_verification_sha256,
        runtime_identity_recovered=True,
        baseline_slot_id="relslot_" + "c" * 24,
        baseline_slot_sha256="d" * 64,
        baseline_manifest_sha256="e" * 64,
        baseline_version="1.2.3",
        baseline_target="darwin-arm64",
        rollback_pointer_id="relactive_" + "f" * 24,
        rollback_pointer_sha256="0" * 64,
        rollback_pointer_generation=8,
        fresh_boot_receipt=SimpleNamespace(binary_sha256="1" * 64),
        runtime_identity_sha256="2" * 64,
        verified_at="2026-08-10T09:01:00+00:00",
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE evolution_post_rollback_behavioral_matrices ("
            "matrix_id TEXT PRIMARY KEY, matrix_sha256 TEXT NOT NULL, "
            "outcome_id TEXT NOT NULL UNIQUE)"
        )
        db.execute(
            "INSERT INTO evolution_post_rollback_behavioral_matrices VALUES (?, ?, ?)",
            (matrix.matrix_id, matrix.matrix_sha256, outcome_id),
        )
        db.execute(
            "CREATE TABLE evolution_post_rollback_runtime_verifications ("
            "verification_id TEXT PRIMARY KEY, verification_sha256 TEXT NOT NULL, "
            "outcome_id TEXT NOT NULL UNIQUE)"
        )
        db.execute(
            "INSERT INTO evolution_post_rollback_runtime_verifications VALUES (?, ?, ?)",
            (verification_id, verification.verification_sha256, outcome_id),
        )
    matrix_store = _ArtifactStore(db_path, matrix)
    verification_store = _ArtifactStore(db_path, verification)
    matrix_service = _MatrixService(matrix)
    verification_service = _VerificationService(verification)
    store = EvolutionPostRollbackLongTermObservationContractStore(db_path)
    service = EvolutionPostRollbackLongTermObservationContractService(
        workspace_root=workspace,
        matrix_store=matrix_store,  # type: ignore[arg-type]
        matrix_service=matrix_service,  # type: ignore[arg-type]
        runtime_verification_store=verification_store,  # type: ignore[arg-type]
        runtime_verification_service=verification_service,  # type: ignore[arg-type]
        store=store,
    )
    return service, store, matrix, verification, matrix_service, verification_service


@pytest.mark.asyncio
async def test_observation_contract_freezes_exact_policy_and_lineage(
    tmp_path: Path,
) -> None:
    service, store, matrix, verification, _matrix_service, _verification_service = (
        _fixture(tmp_path)
    )
    view = await service.record(request_id=matrix.request_id)

    assert view.status == "recorded"
    assert view.observation_contract_authority
    assert view.contract.behavioral_matrix_id == matrix.matrix_id
    assert view.contract.runtime_verification_id == verification.verification_id
    assert view.contract.minimum_observation_seconds == 3600
    assert view.contract.minimum_operational_samples == 12
    assert view.contract.window_not_before_at == matrix.recorded_at
    assert view.contract.gap_limit_rule == "sample_timeout_seconds"
    assert view.contract.censor_phases == ("draining", "stopped")
    assert not view.contract.observation_window_authority
    assert not view.contract.learning_authority
    assert await store.get_by_outcome(matrix.outcome_id) == view.contract


@pytest.mark.asyncio
async def test_concurrent_observation_contract_recording_converges(
    tmp_path: Path,
) -> None:
    service, store, matrix, verification, matrix_service, verification_service = (
        _fixture(tmp_path)
    )
    peer = EvolutionPostRollbackLongTermObservationContractService(
        workspace_root=service.workspace_root,
        matrix_store=service.matrix_store,
        matrix_service=matrix_service,  # type: ignore[arg-type]
        runtime_verification_store=service.runtime_verification_store,
        runtime_verification_service=verification_service,  # type: ignore[arg-type]
        store=EvolutionPostRollbackLongTermObservationContractStore(store.db_path),
    )
    left, right = await asyncio.gather(
        service.record(request_id=matrix.request_id),
        peer.record(request_id=matrix.request_id),
    )
    assert left == right
    assert left.contract.runtime_verification_id == verification.verification_id
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_post_rollback_observation_contracts"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_dependency_authority_change_revokes_contract(tmp_path: Path) -> None:
    service, _store, matrix, _verification, matrix_service, verification_service = (
        _fixture(tmp_path)
    )
    recorded = await service.record(request_id=matrix.request_id)
    matrix_service.authority = False
    stale = await service.inspect(contract=recorded.contract)
    assert stale.status == "stale"
    assert not stale.behavioral_matrix_authority
    assert stale.runtime_verification_authority
    assert not stale.observation_contract_authority

    matrix_service.authority = True
    verification_service.authority = False
    stale = await service.inspect(contract=recorded.contract)
    assert not stale.runtime_verification_authority
    assert not stale.observation_contract_authority


@pytest.mark.asyncio
async def test_non_recovered_matrix_cannot_open_observation_contract(
    tmp_path: Path,
) -> None:
    service, store, matrix, _verification, _matrix_service, _verification_service = (
        _fixture(tmp_path)
    )
    matrix.recovery_verdict = "changed"
    with pytest.raises(EvolutionPostRollbackLongTermObservationContractError) as exc:
        await service.record(request_id=matrix.request_id)
    assert exc.value.code == "post_rollback_observation_contract_lineage_invalid"
    assert await store.get_by_outcome(matrix.outcome_id) is None


def test_observation_authority_stores_must_share_session_database(
    tmp_path: Path,
) -> None:
    service, store, _matrix, _verification, _matrix_service, verification_service = (
        _fixture(tmp_path)
    )
    split_store = _ArtifactStore(
        tmp_path / "split.db",
        service.runtime_verification_store.artifact,
    )
    with pytest.raises(ValueError, match="共享 session SQLite"):
        EvolutionPostRollbackLongTermObservationContractService(
            workspace_root=service.workspace_root,
            matrix_store=service.matrix_store,
            matrix_service=service.matrix_service,
            runtime_verification_store=split_store,  # type: ignore[arg-type]
            runtime_verification_service=verification_service,  # type: ignore[arg-type]
            store=store,
        )

@pytest.mark.asyncio
async def test_store_dependency_tamper_fails_closed(tmp_path: Path) -> None:
    service, store, matrix, _verification, _matrix_service, _verification_service = (
        _fixture(tmp_path)
    )
    recorded = await service.record(request_id=matrix.request_id)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_observation_contracts "
            "SET contract_sha256 = ? WHERE contract_id = ?",
            ("f" * 64, recorded.contract.contract_id),
        )
    with pytest.raises(EvolutionPostRollbackLongTermObservationContractError) as exc:
        await store.get_by_outcome(matrix.outcome_id)
    assert exc.value.code == "post_rollback_observation_contract_store_corrupt"


def test_contract_policy_tamper_is_rejected(tmp_path: Path) -> None:
    service, _store, matrix, _verification, _matrix_service, _verification_service = (
        _fixture(tmp_path)
    )
    view = asyncio.run(service.record(request_id=matrix.request_id))
    payload = view.contract.model_dump(mode="json")
    payload["minimum_operational_samples"] = 1
    with pytest.raises(ValidationError):
        EvolutionPostRollbackLongTermObservationContract.model_validate_json(
            json.dumps(payload)
        )


@pytest.mark.asyncio
async def test_observation_contract_tool_and_slash_share_service(
    tmp_path: Path,
) -> None:
    service, _store, matrix, _verification, _matrix_service, _verification_service = (
        _fixture(tmp_path)
    )
    tool = EvolutionPostRollbackLongTermObservationContractTool(
        SimpleNamespace(evolution_post_rollback_observation_contract_service=service)
    )
    arguments = {"request_id": matrix.request_id}
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    rendered = await tool.execute(**arguments)
    assert "最短观察：`3600s`" in rendered
    assert "Learning / Promotion / Execution authority：`false / false / false`" in (
        rendered
    )

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
        f"/evolution outcome-observation-contract {matrix.request_id}",
    )
    assert "Post-Rollback Long-Term Observation Contract" in slash
    assert matrix.outcome_id in slash
