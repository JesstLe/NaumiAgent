from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent import __version__
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.post_rollback_behavioral_lanes import (
    EvolutionPostRollbackBehavioralLaneError,
    EvolutionPostRollbackBehavioralLaneService,
    EvolutionPostRollbackBehavioralLaneStore,
    _load_suite_request,
    render_post_rollback_behavioral_lane,
)
from naumi_agent.evolution.proposal_before_after_evidence import (
    EvolutionProposalBeforeAfterCohort,
    EvolutionProposalBeforeAfterLane,
)
from naumi_agent.harness.eval import evaluate_suite_repetitions
from naumi_agent.harness.eval_receipt import (
    EvalReceiptSample,
    build_eval_comparison_receipt,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.release.artifact import assemble_release_artifact
from naumi_agent.release.slots import ReleaseSlotStore, host_release_target
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionPostRollbackBehavioralLaneTool


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    eval_root = workspace / "docs" / "harness" / "evals"
    source = Path(__file__).resolve().parents[2] / "docs" / "harness" / "evals"
    shutil.copytree(source, eval_root)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "behavior@example.com")
    _git(workspace, "config", "user.name", "Behavior Test")
    _git(workspace, "add", "docs")
    _git(workspace, "commit", "-qm", "baseline suite")
    return workspace.resolve()


def _binary(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def _release_bundle(root: Path, *, source_commit: str, source_tree: str) -> Path:
    if host_release_target().startswith("windows-"):
        pytest.skip("该真实进程 fixture 需要 POSIX executable。")
    python = Path(sys.executable).absolute()
    source_root = Path(__file__).resolve().parents[2]
    backend = root / "backend"
    runtime = _binary(
        backend / "naumi-runtime",
        (
            f"#!{python}\n"
            "import sys\n"
            f"sys.path.insert(0, {str(source_root)!r})\n"
            "if sys.argv[1:] == ['--version']:\n"
            f"    print('naumi {__version__}')\n"
            "elif sys.argv[1:] == ['--runtime-eval-json']:\n"
            "    from naumi_agent.release.runtime_eval import parse_and_execute_runtime_eval\n"
            "    sys.stdout.buffer.write(parse_and_execute_runtime_eval(sys.stdin.buffer.read()))\n"
            "else:\n"
            "    raise SystemExit(64)\n"
        ).encode(),
    )
    launcher = _binary(root / "launcher" / "naumi", b"#!/bin/sh\nexit 0\n")
    ui = _binary(root / "ui" / "naumi-ui", b"#!/bin/sh\nexit 0\n")
    config = root / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")
    return assemble_release_artifact(
        backend_dir=runtime.parent,
        launcher_dir=launcher.parent,
        ui_binary=ui,
        config_example=config,
        output_dir=root / "release",
        version=__version__,
        target=host_release_target(),
        source_commit=source_commit,
        source_tree_sha256=source_tree,
        archive_format="tar.gz",
    ).bundle_dir


async def _original_h5c(workspace: Path, store: HarnessStore):
    suite_path = workspace / "docs" / "harness" / "evals" / "protocol-hello-core.yaml"
    profile_sha = "1" * 64
    batch = evaluate_suite_repetitions(
        workspace,
        suite_path,
        repetitions=5,
        profile_digest=profile_sha,
        profile_trusted=True,
    )
    assert batch.status == "completed"
    baseline_records = []
    candidate_records = []
    for index, result in enumerate(batch.results):
        baseline_records.append(
            await store.record_eval_result(
                workspace_root=workspace,
                batch_id="before-baseline",
                sample_index=index,
                result=result,
                created_at=f"2026-08-10T00:00:0{index}+00:00",
            )
        )
        candidate_records.append(
            await store.record_eval_result(
                workspace_root=workspace,
                batch_id="after-candidate",
                sample_index=index,
                result=result,
                created_at=f"2026-08-10T00:01:0{index}+00:00",
            )
        )
    baseline = await store.register_eval_comparison_reference(
        workspace_root=workspace,
        batch_id="before-baseline",
        suite_id="protocol-hello-core",
        registered_by="test",
        registration_reason="post rollback behavioral baseline",
        created_at="2026-08-10T00:02:00+00:00",
    )
    receipt = build_eval_comparison_receipt(
        workspace_root=workspace,
        suite_id="protocol-hello-core",
        baseline_id=baseline.id,
        baseline_batch_id=baseline.batch_id,
        baseline_samples_sha256=baseline.samples_sha256,
        baseline_samples=tuple(
            EvalReceiptSample(
                sample_index=item.sample_index,
                result_sha256=item.result_sha256,
                result=item.result,
            )
            for item in baseline_records
        ),
        current_batch_id="after-candidate",
        current_samples=tuple(
            EvalReceiptSample(
                sample_index=item.sample_index,
                result_sha256=item.result_sha256,
                result=item.result,
            )
            for item in candidate_records
        ),
        created_at="2026-08-10T00:03:00+00:00",
    )
    stored = await store.record_eval_comparison_receipt(receipt)
    return batch.results[0], baseline_records, candidate_records, stored


def _cohort(records) -> EvolutionProposalBeforeAfterCohort:
    first = records[0]
    return EvolutionProposalBeforeAfterCohort(
        batch_id=first.batch_id,
        identity_sha256=first.identity_sha256,
        samples=len(records),
        samples_sha256=hashlib.sha256(
            json.dumps(
                [item.result_sha256 for item in records],
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        passed_samples=len(records),
        failed_samples=0,
        evaluation_error_samples=0,
        passed_cases=sum(item.result.passed for item in records),
        implementation_failures=0,
        evaluation_errors=0,
        skipped_cases=0,
        duration_ms=sum(item.result.duration_ms for item in records),
        observed_tokens=None,
        token_samples=0,
        observed_cost_usd=None,
        cost_samples=0,
    )


class _StaticOutcomeService:
    def __init__(self, view) -> None:
        self.view = view

    async def inspect(self, *, request_id: str):
        assert request_id == self.view.outcome.request_id
        return self.view


class _StaticVerificationStore:
    def __init__(self, verification) -> None:
        self.verification = verification

    async def get_by_outcome(self, outcome_id: str):
        assert outcome_id == self.verification.outcome_id
        return self.verification


class _StaticVerificationService:
    def __init__(self, view) -> None:
        self.view = view
        self.store = _StaticVerificationStore(view.verification)

    async def record(self, *, request_id: str):
        assert request_id == self.view.verification.request_id
        return self.view

    async def inspect(self, *, verification):
        assert verification == self.view.verification
        return self.view


class _StaticEvidenceStore:
    def __init__(self, evidence) -> None:
        self.evidence = evidence

    async def get_by_outcome(self, outcome_id: str):
        assert outcome_id == self.evidence.outcome_id
        return self.evidence


class _StaticBeforeAfterService:
    def __init__(self, view) -> None:
        self.view = view
        self.evidence_store = _StaticEvidenceStore(view.evidence)

    async def record(self, *, request_id: str):
        assert request_id == self.view.evidence.request_id
        return self.view

    async def inspect(self, *, evidence):
        assert evidence == self.view.evidence
        return self.view


def test_post_rollback_behavioral_lane_rejects_unsupported_runner(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    suite_path = workspace / "docs" / "harness" / "evals" / "protocol-hello-core.yaml"
    suite_path.write_text(
        suite_path.read_text(encoding="utf-8").replace(
            "runner: protocol_hello",
            "runner: unsupported_runner",
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvolutionPostRollbackBehavioralLaneError) as exc_info:
        _load_suite_request(workspace, "protocol-hello-core")

    assert exc_info.value.code == "post_rollback_suite_unsupported"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="fixture is a POSIX executable")
async def test_post_rollback_behavioral_lane_runs_real_installed_h5c_and_revalidates(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    harness_store = HarnessStore(tmp_path / "harness.db")
    baseline_result, baseline_records, candidate_records, original = await _original_h5c(
        workspace,
        harness_store,
    )
    source = baseline_result.baseline_identity.source
    release_store = ReleaseSlotStore(tmp_path / "installed")
    slot = release_store.install(
        _release_bundle(
            tmp_path,
            source_commit=source.commit,
            source_tree=source.tree_sha256.removeprefix("sha256:"),
        )
    )
    boot = release_store.verify_bootable(slot.slot_id)
    assert boot.slot_id == slot.slot_id
    release_store.activate(slot.slot_id)

    outcome = SimpleNamespace(
        workspace_root=str(workspace),
        outcome_id="evrerollbackout_" + "1" * 24,
        outcome_sha256="2" * 64,
        request_id="evrerollbackreq_" + "3" * 24,
        workbench_session_id="session-behavior",
        workbench_proposal_id="proposal-behavior",
    )
    verified_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    verification = SimpleNamespace(
        verification_id="evpostrollback_" + "4" * 24,
        verification_sha256="5" * 64,
        verified_at=verified_at,
        outcome_id=outcome.outcome_id,
        outcome_sha256=outcome.outcome_sha256,
        request_id=outcome.request_id,
        workbench_proposal_id=outcome.workbench_proposal_id,
        baseline_slot_id=slot.slot_id,
        baseline_slot_sha256=slot.slot_sha256,
        baseline_manifest_sha256=slot.manifest_sha256,
        baseline_version=slot.version,
        baseline_target=slot.target,
    )
    lane = EvolutionProposalBeforeAfterLane(
        order=1,
        lane_kind="interventional",
        platform=host_release_target().split("-", 1)[0],
        suite_id="protocol-hello-core",
        comparison_id=original.receipt.id,
        comparison_receipt_sha256=original.receipt.receipt_sha256,
        baseline_id=original.receipt.baseline_id,
        decision=original.receipt.decision,
        statistical_verdict=original.receipt.statistical_verdict,
        statistical_code=original.receipt.statistical_code,
        before=_cohort(baseline_records),
        after=_cohort(candidate_records),
    )
    evidence = SimpleNamespace(
        evidence_id="evbeforeafter_" + "6" * 24,
        evidence_sha256="7" * 64,
        outcome_id=outcome.outcome_id,
        outcome_sha256=outcome.outcome_sha256,
        request_id=outcome.request_id,
        workbench_proposal_id=outcome.workbench_proposal_id,
        lanes=(lane,),
    )
    outcome_view = SimpleNamespace(
        outcome=outcome,
        outcome_authority=True,
        active_baseline_authority=True,
    )
    verification_view = SimpleNamespace(
        verification=verification,
        verification_authority=True,
        active_baseline_authority=True,
    )
    evidence_view = SimpleNamespace(evidence=evidence, before_after_authority=True)
    store = EvolutionPostRollbackBehavioralLaneStore(tmp_path / "evolution.db")
    service = EvolutionPostRollbackBehavioralLaneService(
        workspace_root=workspace,
        outcome_service=_StaticOutcomeService(outcome_view),  # type: ignore[arg-type]
        runtime_verification_service=_StaticVerificationService(  # type: ignore[arg-type]
            verification_view
        ),
        before_after_service=_StaticBeforeAfterService(  # type: ignore[arg-type]
            evidence_view
        ),
        release_slot_store=release_store,
        harness_store=harness_store,
        store=store,
    )

    view = await service.record(
        request_id=outcome.request_id,
        comparison_id=lane.comparison_id,
    )
    repeated = await service.record(
        request_id=outcome.request_id,
        comparison_id=lane.comparison_id,
    )
    restarted_service = EvolutionPostRollbackBehavioralLaneService(
        workspace_root=workspace,
        outcome_service=_StaticOutcomeService(outcome_view),  # type: ignore[arg-type]
        runtime_verification_service=_StaticVerificationService(  # type: ignore[arg-type]
            verification_view
        ),
        before_after_service=_StaticBeforeAfterService(  # type: ignore[arg-type]
            evidence_view
        ),
        release_slot_store=release_store,
        harness_store=harness_store,
        store=EvolutionPostRollbackBehavioralLaneStore(tmp_path / "evolution.db"),
    )
    restarted = await restarted_service.record(
        request_id=outcome.request_id,
        comparison_id=lane.comparison_id,
    )

    assert repeated == restarted == view
    assert view.lane_authority and view.active_baseline_authority
    assert view.lane.recovery_status == "recovered"
    assert view.lane.repetitions == 5
    assert len(view.lane.runtime_eval_receipts) == 5
    assert view.lane.fresh_comparison.statistical_verdict.value == "unchanged"
    assert all(
        item.mechanical_verdict.value == "unchanged"
        for item in view.lane.fresh_comparison.sample_evidence
    )
    assert not view.lane.behavioral_evaluation_recorded
    assert not view.behavioral_evaluation_authority
    rendered = render_post_rollback_behavioral_lane(view)
    assert "exact installed baseline" in rendered
    assert "行为级总体评测：尚未完成" in rendered

    tool = EvolutionPostRollbackBehavioralLaneTool(
        SimpleNamespace(evolution_post_rollback_behavioral_lane_service=restarted_service)
    )
    tool_args = {
        "request_id": outcome.request_id,
        "comparison_id": lane.comparison_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, tool_args, tool=tool)
        assert decision.allowed
        assert not decision.requires_confirmation
    assert await tool.execute(outcome.request_id, lane.comparison_id) == rendered
    registry = ToolRegistry()
    registry.register(tool)

    class _BehavioralSlashEngine:
        def __init__(self) -> None:
            self.tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            arguments = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**arguments),
            )

    slash_rendered = await execute_slash_command(
        _BehavioralSlashEngine(),
        (f"/evolution outcome-verify-behavior {outcome.request_id} {lane.comparison_id}"),
    )
    assert view.lane.lane_id in slash_rendered
    assert view.lane.fresh_comparison.id in slash_rendered
    with sqlite3.connect(release_store.db_path) as db:
        runtime_receipt_count = db.execute(
            "SELECT COUNT(*) FROM release_runtime_eval_receipts"
        ).fetchone()[0]
    assert runtime_receipt_count == 5

    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_behavioral_lanes SET lane_json = ? WHERE lane_id = ?",
            ("{}", view.lane.lane_id),
        )
        db.commit()
    corrupt = await service.inspect(lane=view.lane)
    assert not corrupt.durable_dependencies_valid
    assert not corrupt.lane_authority
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_behavioral_lanes SET lane_json = ? WHERE lane_id = ?",
            (view.lane.model_dump_json(), view.lane.lane_id),
        )
        db.commit()
    restored = await service.inspect(lane=view.lane)
    assert restored.lane_authority

    receipt = view.lane.runtime_eval_receipts[0]
    with sqlite3.connect(release_store.db_path) as db:
        db.execute(
            "UPDATE release_runtime_eval_receipts SET receipt_json = ? WHERE receipt_id = ?",
            ("{}", receipt.receipt_id),
        )
        db.commit()
    stale = await service.inspect(lane=view.lane)
    assert not stale.fresh_runtime_authority
    assert not stale.lane_authority

    with pytest.raises(EvolutionPostRollbackBehavioralLaneError) as invalid:
        await service.record(
            request_id=outcome.request_id,
            comparison_id="0" * 64,
        )
    assert invalid.value.code == "post_rollback_behavioral_source_invalid"
