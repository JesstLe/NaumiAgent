from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from naumi_agent.harness.completion import HarnessCompletionReceipt
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.replay import (
    capture_replay_baseline,
    replay_stored_run,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredCheck,
    HarnessStoredEvidence,
    HarnessStoredRun,
)
from naumi_agent.harness.trust import HarnessTrustStore


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _run(
    workspace: Path,
    *,
    status: str = "completed_verified",
    artifact_path: str = "artifacts/unit.txt",
    start_missing: bool = False,
) -> HarnessStoredRun:
    contract = HarnessCompletionContract(
        run_id="replay-run",
        session_id="session-1",
        task_kind=HarnessTaskKind.CHANGE,
        objective="安全回放一次真实运行",
    )
    receipt = None
    completed_at = ""
    if status != "running":
        receipt = HarnessCompletionReceipt(
            run_id=contract.run_id,
            status=status,
            task_kind=HarnessTaskKind.CHANGE,
            changed_files=("source.py",),
            checks=(),
            criteria=(),
            warnings=(),
            tree_fingerprint="b" * 64,
        )
        completed_at = "2026-07-15T10:01:00+00:00"
    summary = {
        "tool_name": "read_file",
        "status": "success",
        "start_missing": start_missing,
        "permission_status": "not_observed",
    }
    evidence = HarnessStoredEvidence(
        id="tool-evidence",
        kind="tool_execution",
        uri="chat-run://replay-run/tool/tool-evidence",
        sha256=_canonical_digest(summary),
        description="规范化工具证据",
        summary=summary,
        producer="harness_evidence_collector",
        created_at="2026-07-15T10:00:20+00:00",
        criterion_ids=(),
    )
    check = HarnessStoredCheck(
        id="check-record",
        check_key="unit",
        argv=("python3", "-m", "pytest", "tests/unit/test_small.py"),
        cwd=str(workspace),
        status="passed",
        exit_code=0,
        duration_ms=25,
        started_at="2026-07-15T10:00:10+00:00",
        completed_at="2026-07-15T10:00:11+00:00",
        tree_fingerprint="b" * 64,
        profile_digest="a" * 64,
        artifact_path=artifact_path,
    )
    return HarnessStoredRun(
        id=contract.run_id,
        workspace_root=str(workspace),
        session_id=contract.session_id,
        task_id=None,
        issue_id=None,
        task_kind="change",
        objective=contract.objective,
        status=status,
        profile_digest="a" * 64,
        tree_fingerprint_before="a" * 64,
        tree_fingerprint_after="b" * 64 if receipt else "",
        started_at="2026-07-15T10:00:00+00:00",
        completed_at=completed_at,
        contract=contract,
        receipt=receipt,
        criteria=(),
        checks=(check,),
        evidence=(evidence,),
    )


def test_replay_is_deterministic_and_does_not_embed_artifact_content(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "artifacts" / "unit.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("private artifact body", encoding="utf-8")
    run = _run(workspace)
    baseline = capture_replay_baseline(run, workspace_root=workspace)

    results = tuple(
        replay_stored_run(run, baseline=baseline, workspace_root=workspace) for _ in range(50)
    )

    assert all(result == results[0] for result in results)
    assert results[0].status == "reproduced"
    assert results[0].timeline[0].kind == "run_started"
    assert results[0].timeline[-1].kind == "run_finished"
    assert "private artifact body" not in repr(results[0])


def test_replay_distinguishes_missing_and_modified_artifact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "artifacts" / "unit.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"original")
    run = _run(workspace)
    baseline = capture_replay_baseline(run, workspace_root=workspace)

    artifact.unlink()
    missing = replay_stored_run(run, baseline=baseline, workspace_root=workspace)
    artifact.write_bytes(b"modified")
    modified = replay_stored_run(run, baseline=baseline, workspace_root=workspace)

    assert missing.status == "partial"
    assert missing.artifacts[0].status == "missing"
    assert modified.status == "corrupt"
    assert modified.artifacts[0].status == "digest_mismatch"


def test_replay_reports_rule_version_change_without_mutating_baseline(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "artifacts" / "unit.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("ok", encoding="utf-8")
    run = _run(workspace)
    baseline = capture_replay_baseline(run, workspace_root=workspace)

    result = replay_stored_run(
        run,
        baseline=baseline,
        workspace_root=workspace,
        rule_version="future-rule",
    )

    assert result.status == "changed"
    assert result.baseline_rule_version != result.current_rule_version
    assert any(item.field == "rule_version" for item in result.differences)
    assert baseline.rule_version != "future-rule"


def test_running_and_missing_tool_start_are_partial(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "artifacts" / "unit.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("ok", encoding="utf-8")
    running = _run(workspace, status="running")
    incomplete_tool = _run(workspace, start_missing=True)

    running_result = replay_stored_run(
        running,
        baseline=capture_replay_baseline(running, workspace_root=workspace),
        workspace_root=workspace,
    )
    tool_result = replay_stored_run(
        incomplete_tool,
        baseline=capture_replay_baseline(
            incomplete_tool,
            workspace_root=workspace,
        ),
        workspace_root=workspace,
    )

    assert running_result.status == "partial"
    assert "run_not_finished" in running_result.anomalies
    assert tool_result.status == "partial"
    assert "tool_start_missing:tool-evidence" in tool_result.anomalies


def test_replay_rejects_workspace_escape_without_reading_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must stay unread", encoding="utf-8")
    run = _run(workspace, artifact_path="../outside.txt")
    baseline = capture_replay_baseline(run, workspace_root=workspace)

    result = replay_stored_run(run, baseline=baseline, workspace_root=workspace)

    assert result.status == "partial"
    assert result.artifacts[0].status == "unsafe_path"
    assert "must stay unread" not in repr(result)


def test_unknown_kind_and_duplicate_evidence_are_partial(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "artifacts" / "unit.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("ok", encoding="utf-8")
    original = _run(workspace)
    unknown = replace(
        original,
        evidence=(replace(original.evidence[0], kind="future_evidence_kind"),),
    )
    duplicate = replace(
        original,
        evidence=(original.evidence[0], original.evidence[0]),
    )

    unknown_result = replay_stored_run(
        unknown,
        baseline=capture_replay_baseline(unknown, workspace_root=workspace),
        workspace_root=workspace,
    )
    duplicate_result = replay_stored_run(
        duplicate,
        baseline=capture_replay_baseline(duplicate, workspace_root=workspace),
        workspace_root=workspace,
    )

    assert unknown_result.status == "partial"
    assert "unknown_evidence_kind:tool-evidence" in unknown_result.anomalies
    assert duplicate_result.status == "partial"
    assert "duplicate_evidence:tool-evidence" in duplicate_result.anomalies


def test_manifest_version_compatibility_is_explicit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "artifacts" / "unit.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("ok", encoding="utf-8")
    run = _run(workspace)
    baseline = capture_replay_baseline(run, workspace_root=workspace)

    def versioned(version: int):
        manifest = json.loads(baseline.manifest_json)
        manifest["manifest_version"] = version
        manifest_json = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return replace(
            baseline,
            manifest_json=manifest_json,
            manifest_sha256=hashlib.sha256(manifest_json.encode()).hexdigest(),
        )

    old = replay_stored_run(
        run,
        baseline=versioned(0),
        workspace_root=workspace,
    )
    future = replay_stored_run(
        run,
        baseline=versioned(99),
        workspace_root=workspace,
    )

    assert old.status == "partial"
    assert "old_manifest_version:0" in old.anomalies
    assert future.status == "corrupt"
    assert "unsupported_manifest_version:99" in future.anomalies


async def _start_service_run(
    store: HarnessStore,
    workspace: Path,
    *,
    run_id: str,
    finished: bool,
) -> None:
    contract = HarnessCompletionContract(
        run_id=run_id,
        session_id="service-session",
        task_kind=HarnessTaskKind.ANALYSIS,
        objective=f"回放 {run_id}",
    )
    await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before="a" * 64,
        started_at="2026-07-15T10:00:00+00:00",
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
            completed_at="2026-07-15T10:01:00+00:00",
        )


@pytest.mark.asyncio
async def test_service_replay_is_workspace_isolated_and_cross_store(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    foreign_workspace = tmp_path / "foreign"
    workspace.mkdir()
    foreign_workspace.mkdir()
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    await _start_service_run(store, workspace, run_id="local-run", finished=True)
    await _start_service_run(store, foreign_workspace, run_id="foreign-run", finished=True)
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(db_path),
    )

    latest = await service.replay_run()
    explicit = await service.replay_run("local-run")
    foreign = await service.replay_run("foreign-run")

    assert latest.status == "ok"
    assert latest.result is not None and latest.result.status == "reproduced"
    assert explicit.result == latest.result
    assert foreign.status == "not_found"
    assert foreign.result is None
    assert "foreign-run" not in foreign.message


@pytest.mark.asyncio
async def test_service_replay_marks_first_legacy_baseline_partial(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _start_service_run(store, workspace, run_id="legacy-run", finished=False)
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=HarnessStore(store.db_path),
    )

    first = await service.replay_run("legacy-run")
    second = await service.replay_run("legacy-run")

    assert first.status == "ok" and first.result is not None
    assert first.result.status == "partial"
    assert first.result.legacy_baseline_created is True
    assert second.result is not None
    assert second.result.legacy_baseline_created is False
    assert "run_not_finished" in second.result.anomalies


@pytest.mark.asyncio
async def test_service_replay_reports_unavailable_store_safely(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    damaged = tmp_path / "damaged.db"
    damaged.write_text("not sqlite", encoding="utf-8")
    missing_store = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust-a.db"),
    )
    damaged_store = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust-b.db"),
        store=HarnessStore(damaged),
    )

    missing = await missing_store.replay_run()
    unavailable = await damaged_store.replay_run("run-id")

    assert missing.status == "unavailable"
    assert unavailable.status == "unavailable"
    assert "sqlite" not in unavailable.message.casefold()
    assert str(damaged) not in unavailable.message
