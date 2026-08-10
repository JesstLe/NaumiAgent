from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.post_rollback_behavioral_coverage import (
    EvolutionPostRollbackBehavioralCoverageContract,
    EvolutionPostRollbackBehavioralCoverageLane,
    EvolutionPostRollbackBehavioralCoverageLaneView,
    EvolutionPostRollbackBehavioralCoverageView,
)
from naumi_agent.evolution.post_rollback_behavioral_matrix import (
    EvolutionPostRollbackBehavioralMatrixError,
    EvolutionPostRollbackBehavioralMatrixService,
    EvolutionPostRollbackBehavioralMatrixStore,
    aggregate_post_rollback_recovery_status,
)
from naumi_agent.harness.eval_statistics import EvalStatisticalVerdict
from naumi_agent.harness.eval_suite_compare import EvalMechanicalVerdict
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionPostRollbackBehavioralMatrixTool,
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


class _CoverageStore:
    def __init__(self, db_path: Path, contract) -> None:
        self.db_path = db_path
        self.contract = contract

    async def get_by_outcome(self, outcome_id: str):
        assert outcome_id == self.contract.outcome_id
        return self.contract


class _CoverageService:
    def __init__(self, view) -> None:
        self.view = view

    async def record(self, *, request_id: str):
        assert request_id == self.view.contract.request_id
        return self.view

    async def inspect(self, *, contract):
        assert contract == self.view.contract
        return self.view


class _LaneStore:
    def __init__(self, db_path: Path, lane) -> None:
        self.db_path = db_path
        self.lane = lane

    async def get(self, outcome_id: str, comparison_id: str):
        if (
            outcome_id == self.lane.outcome_id
            and comparison_id == self.lane.original_comparison_id
        ):
            return self.lane
        return None


class _LaneService:
    def __init__(self) -> None:
        self.authority = True

    async def inspect(self, *, lane):
        return SimpleNamespace(lane=lane, lane_authority=self.authority)


class _RemoteResultStore:
    def __init__(self, db_path: Path, manifest) -> None:
        self.db_path = db_path
        self.manifest = manifest
        self.available = True

    async def list_by_lane(self, *, outcome_id: str, comparison_id: str, limit: int):
        assert limit == 2
        if not self.available:
            return ()
        payload = self.manifest.payload
        if outcome_id == payload.outcome_id and comparison_id == payload.comparison_id:
            return (self.manifest,)
        return ()


class _RemoteResultService:
    def __init__(self, manifest, receipt) -> None:
        self.manifest = manifest
        self.receipt = receipt
        self.authority = True

    async def inspect(self, *, manifest_id: str):
        assert manifest_id == self.manifest.manifest_id
        return SimpleNamespace(
            status="ingested" if self.authority else "stale",
            lane_evaluation_authority=self.authority,
            receipt=self.receipt,
        )


class _HarnessStore:
    def __init__(self, receipt) -> None:
        self.receipt = receipt

    async def get_eval_comparison_receipt_by_id(self, workspace_root, receipt_id: str):
        del workspace_root
        if receipt_id != self.receipt.id:
            return None
        return SimpleNamespace(receipt=self.receipt)


def _coverage(tmp_path: Path):
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    lanes = (
        EvolutionPostRollbackBehavioralCoverageLane(
            order=1,
            lane_kind="interventional",
            platform="macos",
            suite_id="protocol-hello-core",
            original_comparison_id="1" * 64,
            original_comparison_sha256="a" * 64,
            original_baseline_id="b" * 64,
            original_baseline_samples_sha256="c" * 64,
            execution_scope="local_installed_baseline",
            local_execution_eligible=True,
            remote_target_required=False,
        ),
        EvolutionPostRollbackBehavioralCoverageLane(
            order=2,
            lane_kind="adversarial",
            platform="windows",
            suite_id="protocol-hello-core",
            original_comparison_id="2" * 64,
            original_comparison_sha256="d" * 64,
            original_baseline_id="e" * 64,
            original_baseline_samples_sha256="f" * 64,
            execution_scope="remote_target_required",
            local_execution_eligible=False,
            remote_target_required=True,
        ),
    )
    core = {
        "schema_version": 1,
        "policy_version": "evolution-post-rollback-behavioral-coverage-v1",
        "workspace_root": str(workspace),
        "outcome_id": "evrerollbackout_" + "1" * 24,
        "outcome_sha256": "2" * 64,
        "request_id": "evrerollbackreq_" + "3" * 24,
        "workbench_session_id": "session-matrix",
        "workbench_proposal_id": "proposal-matrix",
        "runtime_verification_id": "evpostrollback_" + "4" * 24,
        "runtime_verification_sha256": "5" * 64,
        "before_after_evidence_id": "evbeforeafter_" + "6" * 24,
        "before_after_evidence_sha256": "7" * 64,
        "final_evaluation_id": "evfinal_" + "8" * 24,
        "final_evaluation_sha256": "9" * 64,
        "baseline_slot_id": "relslot_" + "a" * 24,
        "baseline_slot_sha256": "b" * 64,
        "baseline_manifest_sha256": "c" * 64,
        "baseline_version": "1.2.3",
        "baseline_target": "darwin-arm64",
        "baseline_platform": "macos",
        "baseline_source_commit": "d" * 40,
        "baseline_source_tree_sha256": "e" * 64,
        "lane_count": 2,
        "local_lane_count": 1,
        "remote_lane_count": 1,
        "required_platforms": ["windows"],
        "lanes": [item.model_dump(mode="json") for item in lanes],
        "coverage_contract_recorded": True,
        "behavioral_evaluation_recorded": False,
        "execution_authority": False,
        "learning_authority": False,
        "promotion_authority": False,
        "recorded_at": "2026-08-10T09:00:00+00:00",
    }
    digest = _digest(core)
    contract = EvolutionPostRollbackBehavioralCoverageContract.model_validate(
        {
            **core,
            "contract_id": f"evpostcoverage_{digest[:24]}",
            "contract_sha256": digest,
        }
    )
    lane_views = (
        EvolutionPostRollbackBehavioralCoverageLaneView(
            expected=lanes[0],
            status="missing",
            lane_authority=False,
            active_baseline_authority=False,
            dispatch_required=False,
        ),
        EvolutionPostRollbackBehavioralCoverageLaneView(
            expected=lanes[1],
            status="missing",
            lane_authority=False,
            active_baseline_authority=False,
            dispatch_required=True,
        ),
    )
    view = EvolutionPostRollbackBehavioralCoverageView(
        contract=contract,
        lanes=lane_views,
        durable_dependencies_valid=True,
        outcome_authority=True,
        runtime_verification_authority=True,
        before_after_authority=True,
        active_baseline_authority=True,
        recorded_lane_count=0,
        missing_lane_count=2,
        stale_lane_count=0,
        remote_dispatch_count=1,
        matrix_ready=False,
    )
    db_path = tmp_path / "session.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE evolution_post_rollback_behavioral_coverage ("
            "contract_id TEXT PRIMARY KEY, contract_sha256 TEXT NOT NULL, "
            "outcome_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL UNIQUE, "
            "contract_json TEXT NOT NULL, recorded_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO evolution_post_rollback_behavioral_coverage "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                contract.contract_id,
                contract.contract_sha256,
                contract.outcome_id,
                contract.request_id,
                contract.model_dump_json(),
                contract.recorded_at,
            ),
        )
    return workspace, db_path, contract, view


def _fixture(tmp_path: Path):
    workspace, db_path, contract, coverage_view = _coverage(tmp_path)
    local = SimpleNamespace(
        outcome_id=contract.outcome_id,
        original_comparison_id=contract.lanes[0].original_comparison_id,
        original_comparison_sha256=contract.lanes[0].original_comparison_sha256,
        original_baseline_id=contract.lanes[0].original_baseline_id,
        original_baseline_samples_sha256=(
            contract.lanes[0].original_baseline_samples_sha256
        ),
        lane_id="evpostbehavior_" + "1" * 24,
        lane_sha256="3" * 64,
        lane_order=1,
        lane_kind="interventional",
        platform="macos",
        suite_id="protocol-hello-core",
        fresh_comparison=SimpleNamespace(id="4" * 64, receipt_sha256="5" * 64),
        recovery_status="recovered",
        evaluated_at="2026-08-10T09:01:00+00:00",
    )
    remote_payload = SimpleNamespace(
        outcome_id=contract.outcome_id,
        request_id=contract.request_id,
        comparison_id=contract.lanes[1].original_comparison_id,
        suite_id="protocol-hello-core",
        release_target="windows-x64",
        repetitions=5,
    )
    manifest = SimpleNamespace(
        manifest_id="evpostresultmanifest_" + "6" * 24,
        payload=remote_payload,
    )
    remote_receipt = SimpleNamespace(
        receipt_id="evpostresultreceipt_" + "7" * 24,
        receipt_sha256="8" * 64,
        repetitions=5,
        h5c_comparison_id="9" * 64,
        h5c_comparison_sha256="a" * 64,
        ingested_at="2026-08-10T09:02:00+00:00",
    )
    h5c = SimpleNamespace(
        id=remote_receipt.h5c_comparison_id,
        receipt_sha256=remote_receipt.h5c_comparison_sha256,
        statistical_verdict=EvalStatisticalVerdict.UNCHANGED,
        sample_evidence=tuple(
            SimpleNamespace(mechanical_verdict=EvalMechanicalVerdict.UNCHANGED)
            for _ in range(5)
        ),
    )
    lane_store = _LaneStore(db_path, local)
    lane_service = _LaneService()
    result_store = _RemoteResultStore(db_path, manifest)
    result_service = _RemoteResultService(manifest, remote_receipt)
    matrix_store = EvolutionPostRollbackBehavioralMatrixStore(db_path)
    service = EvolutionPostRollbackBehavioralMatrixService(
        workspace_root=workspace,
        coverage_store=_CoverageStore(db_path, contract),  # type: ignore[arg-type]
        coverage_service=_CoverageService(coverage_view),  # type: ignore[arg-type]
        lane_store=lane_store,  # type: ignore[arg-type]
        lane_service=lane_service,  # type: ignore[arg-type]
        remote_result_store=result_store,  # type: ignore[arg-type]
        remote_result_service=result_service,  # type: ignore[arg-type]
        harness_store=_HarnessStore(h5c),  # type: ignore[arg-type]
        store=matrix_store,
    )
    return service, matrix_store, contract, lane_service, result_store, result_service


@pytest.mark.asyncio
async def test_complete_local_and_remote_matrix_is_recorded_idempotently(
    tmp_path: Path,
) -> None:
    service, store, contract, _lane_service, _result_store, _result_service = (
        _fixture(tmp_path)
    )
    first = await service.record(request_id=contract.request_id)
    repeated = await service.record(request_id=contract.request_id)

    assert repeated == first
    assert first.status == "recorded"
    assert first.behavioral_evaluation_authority
    assert first.matrix.recovery_verdict == "recovered"
    assert first.matrix.local_lane_count == 1
    assert first.matrix.remote_lane_count == 1
    assert first.matrix.lanes[1].evidence_kind == "remote_result_ingestion"
    assert not first.matrix.long_term_metrics_recorded
    assert not first.matrix.learning_authority
    assert await store.get_by_outcome(contract.outcome_id) == first.matrix


@pytest.mark.asyncio
async def test_missing_or_stale_lane_fails_closed(tmp_path: Path) -> None:
    service, store, contract, _lane_service, result_store, result_service = _fixture(
        tmp_path
    )
    result_store.available = False
    with pytest.raises(EvolutionPostRollbackBehavioralMatrixError) as missing:
        await service.record(request_id=contract.request_id)
    assert missing.value.code == "post_rollback_behavioral_matrix_lane_missing"
    assert await store.get_by_outcome(contract.outcome_id) is None

    result_store.available = True
    recorded = await service.record(request_id=contract.request_id)
    result_service.authority = False
    inspected = await service.inspect(matrix=recorded.matrix)
    assert inspected.status == "stale"
    assert inspected.coverage_authority
    assert not inspected.complete_lane_authority
    assert not inspected.behavioral_evaluation_authority


@pytest.mark.asyncio
async def test_matrix_store_row_tamper_fails_closed(tmp_path: Path) -> None:
    service, store, contract, _lane_service, _result_store, _result_service = (
        _fixture(tmp_path)
    )
    recorded = await service.record(request_id=contract.request_id)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_behavioral_matrices "
            "SET matrix_sha256 = ? WHERE matrix_id = ?",
            ("0" * 64, recorded.matrix.matrix_id),
        )
    with pytest.raises(EvolutionPostRollbackBehavioralMatrixError) as tampered:
        await store.get_by_outcome(contract.outcome_id)
    assert tampered.value.code == "post_rollback_behavioral_matrix_store_corrupt"


@pytest.mark.asyncio
async def test_concurrent_matrix_recording_converges(tmp_path: Path) -> None:
    service, store, contract, lane_service, result_store, result_service = _fixture(
        tmp_path
    )
    peer = EvolutionPostRollbackBehavioralMatrixService(
        workspace_root=service.workspace_root,
        coverage_store=service.coverage_store,
        coverage_service=service.coverage_service,
        lane_store=service.lane_store,
        lane_service=lane_service,  # type: ignore[arg-type]
        remote_result_store=result_store,  # type: ignore[arg-type]
        remote_result_service=result_service,  # type: ignore[arg-type]
        harness_store=service.harness_store,
        store=EvolutionPostRollbackBehavioralMatrixStore(store.db_path),
    )
    left, right = await asyncio.gather(
        service.record(request_id=contract.request_id),
        peer.record(request_id=contract.request_id),
    )
    assert left == right
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_post_rollback_behavioral_matrices"
        ).fetchone() == (1,)


def test_matrix_verdict_policy_is_fail_closed() -> None:
    assert aggregate_post_rollback_recovery_status(("recovered", "recovered")) == (
        "recovered"
    )
    assert aggregate_post_rollback_recovery_status(("recovered", "changed")) == (
        "changed"
    )
    assert aggregate_post_rollback_recovery_status(("changed", "inconclusive")) == (
        "inconclusive"
    )
    assert aggregate_post_rollback_recovery_status(("inconclusive", "incompatible")) == (
        "incompatible"
    )
    with pytest.raises(ValueError):
        aggregate_post_rollback_recovery_status(())


@pytest.mark.asyncio
async def test_matrix_tool_and_slash_share_service_without_confirmation(
    tmp_path: Path,
) -> None:
    service, _store, contract, _lane_service, _result_store, _result_service = (
        _fixture(tmp_path)
    )
    tool = EvolutionPostRollbackBehavioralMatrixTool(
        SimpleNamespace(evolution_post_rollback_behavioral_matrix_service=service)
    )
    arguments = {"request_id": contract.request_id}
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    rendered = await tool.execute(**arguments)
    assert "总体判定：`recovered`" in rendered
    assert "Learning / Promotion authority：`false / false`" in rendered

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
        f"/evolution outcome-behavior-matrix {contract.request_id}",
    )
    assert "总体判定：`recovered`" in slash
    assert contract.outcome_id in slash
