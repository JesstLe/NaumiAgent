from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.harness.completion import HarnessCompletionReceipt
from naumi_agent.harness.eval_identity import HarnessEvalSourceIdentity
from naumi_agent.harness.eval_models import EvalCaseStatus, EvalRunStatus
from naumi_agent.harness.eval_replay import build_safe_replay_eval_result
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.replay_models import (
    HarnessReplayLookup,
    HarnessReplayResult,
    HarnessReplayStatus,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore


def _replay(status: HarnessReplayStatus = "reproduced") -> HarnessReplayResult:
    return HarnessReplayResult(
        run_id="run-safe-1",
        status=status,
        baseline_manifest_sha256="a" * 64,
        current_manifest_sha256="a" * 64,
        baseline_rule_version="safe-replay@1",
        current_rule_version="safe-replay@1",
        baseline_explanation_sha256="b" * 64,
        current_explanation_sha256="b" * 64,
        timeline=(),
        artifacts=(),
        anomalies=(),
        differences=(),
    )


def _source(*, dirty: bool = False) -> HarnessEvalSourceIdentity:
    return HarnessEvalSourceIdentity(
        commit="c" * 40,
        tree_sha256=f"sha256:{'d' * 64}",
        dirty=dirty,
    )


@pytest.mark.parametrize(
    ("replay_status", "case_status", "run_status", "code"),
    [
        ("reproduced", EvalCaseStatus.PASSED, EvalRunStatus.PASSED, ""),
        (
            "changed",
            EvalCaseStatus.IMPLEMENTATION_FAILURE,
            EvalRunStatus.FAILED,
            "replay_behavior_changed",
        ),
        (
            "partial",
            EvalCaseStatus.EVALUATION_ERROR,
            EvalRunStatus.FAILED,
            "replay_evidence_partial",
        ),
        (
            "corrupt",
            EvalCaseStatus.EVALUATION_ERROR,
            EvalRunStatus.FAILED,
            "replay_evidence_corrupt",
        ),
    ],
)
def test_safe_replay_eval_maps_replay_status_without_raw_evidence(
    tmp_path: Path,
    replay_status: HarnessReplayStatus,
    case_status: EvalCaseStatus,
    run_status: EvalRunStatus,
    code: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    replay = _replay(replay_status)

    result = build_safe_replay_eval_result(
        HarnessReplayLookup(status="ok", result=replay),
        workspace_root=workspace,
        profile_digest="e" * 64,
        profile_trusted=True,
        source_before=_source(),
        source_after=_source(),
    )

    assert result.cases[0].status is case_status
    assert result.cases[0].code == code
    assert result.status is run_status
    assert {item.guardrail for item in result.cases[0].guardrails} == {
        "no_model",
        "no_side_effect",
    }
    assert "timeline" not in result.model_dump_json()
    assert "artifacts" not in result.model_dump_json()


def test_safe_replay_eval_identity_detects_source_change(tmp_path: Path) -> None:
    result = build_safe_replay_eval_result(
        HarnessReplayLookup(status="ok", result=_replay()),
        workspace_root=tmp_path,
        profile_digest="e" * 64,
        profile_trusted=True,
        source_before=_source(),
        source_after=_source(dirty=True),
    )

    assert result.baseline_identity is None
    assert result.baseline_identity_code == "baseline_source_changed"


async def _start_run(
    store: HarnessStore,
    workspace: Path,
    *,
    run_id: str,
    finished: bool,
) -> None:
    contract = HarnessCompletionContract(
        run_id=run_id,
        session_id="eval-replay-session",
        task_kind=HarnessTaskKind.ANALYSIS,
        objective="验证安全 Replay Eval",
    )
    await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before="a" * 64,
        started_at="2026-07-18T00:00:00+00:00",
    )
    if finished:
        await store.finish_run(
            run_id=run_id,
            receipt=HarnessCompletionReceipt(
                run_id=run_id,
                status="completed_verified",
                task_kind=HarnessTaskKind.ANALYSIS,
                changed_files=(),
                checks=(),
                criteria=(),
                warnings=(),
                tree_fingerprint="b" * 64,
            ),
            completed_at="2026-07-18T00:01:00+00:00",
        )


@pytest.mark.asyncio
async def test_eval_replay_requires_existing_baseline_without_creating_one(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _start_run(store, workspace, run_id="unfinished", finished=False)
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(store.db_path),
    )

    result = await service.eval_replay_run("unfinished")

    assert result.cases[0].status is EvalCaseStatus.EVALUATION_ERROR
    assert result.cases[0].code == "replay_unavailable"
    assert result.status is EvalRunStatus.EVALUATION_ERROR
    assert "/harness replay" in result.message
    assert await store.get_replay_baseline("unfinished") is None


@pytest.mark.asyncio
async def test_eval_replay_uses_persisted_finished_run_baseline(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _start_run(store, workspace, run_id="finished", finished=True)
    baseline_before = await store.get_replay_baseline("finished")
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(store.db_path),
    )

    result = await service.eval_replay_run("finished")
    baseline_after = await store.get_replay_baseline("finished")

    assert result.status is EvalRunStatus.PASSED
    assert result.cases[0].status is EvalCaseStatus.PASSED
    assert baseline_before is not None
    assert baseline_after == baseline_before
