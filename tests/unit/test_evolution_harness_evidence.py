from __future__ import annotations

from dataclasses import replace

from naumi_agent.evolution.evidence import adapt_harness_failure_evidence
from naumi_agent.harness.completion import HarnessCompletionReceipt
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.store import HarnessStoredCheck, HarnessStoredRun


def _failed_run(run_id: str = "evolution-run") -> HarnessStoredRun:
    contract = HarnessCompletionContract(
        run_id=run_id,
        session_id="session-private",
        task_kind=HarnessTaskKind.CHANGE,
        objective="用户原始目标不得进入 Evolution Evidence",
    )
    receipt = HarnessCompletionReceipt.model_validate(
        {
            "run_id": run_id,
            "status": "completed_unverified",
            "task_kind": "change",
            "changed_files": ["src/private.py"],
            "checks": [],
            "criteria": [],
            "warnings": ["必需检查 unit 状态为 failed，不能作为通过证据。"],
            "tree_fingerprint": "b" * 64,
        }
    )
    check = HarnessStoredCheck(
        id="check-record",
        check_key="unit",
        argv=("python3", "-m", "pytest", "tests/unit/test_small.py"),
        cwd="/workspace/project",
        status="failed",
        exit_code=1,
        duration_ms=25,
        started_at="2026-07-18T10:00:10+00:00",
        completed_at="2026-07-18T10:00:11+00:00",
        tree_fingerprint="b" * 64,
        profile_digest="a" * 64,
        artifact_path="",
    )
    return HarnessStoredRun(
        id=run_id,
        workspace_root="/workspace/project",
        session_id=contract.session_id,
        task_id=None,
        issue_id=None,
        task_kind="change",
        objective=contract.objective,
        status="completed_unverified",
        profile_digest="a" * 64,
        tree_fingerprint_before="a" * 64,
        tree_fingerprint_after="b" * 64,
        started_at="2026-07-18T10:00:00+00:00",
        completed_at="2026-07-18T10:01:00+00:00",
        contract=contract,
        receipt=receipt,
        criteria=(),
        checks=(check,),
        evidence=(),
    )


def test_failed_harness_check_becomes_redacted_hard_evidence() -> None:
    evidence = adapt_harness_failure_evidence(_failed_run())

    assert len(evidence) == 1
    item = evidence[0]
    assert item.failure_class.value == "verification_failure"
    assert item.finding_code == "verification_failure"
    assert item.scope == "checks:unit"
    assert item.hard_evidence is True
    assert item.source_uri == "harness://runs/evolution-run/checks/unit"
    assert len(item.refs) == 2
    assert all(len(ref.sha256) == 64 for ref in item.refs)
    serialized = item.model_dump_json()
    assert "用户原始目标" not in serialized
    assert "session-private" not in serialized
    assert "src/private.py" not in serialized

    legacy_payload = item.model_dump(mode="json", exclude={"finding_code", "scope"})
    restored = type(item).model_validate(legacy_payload)
    assert restored.finding_code == "verification_failure"
    assert restored.scope == "harness:run"


def test_root_fingerprint_deduplicates_same_root_across_runs() -> None:
    first = adapt_harness_failure_evidence(_failed_run("run-one"))[0]
    second = adapt_harness_failure_evidence(_failed_run("run-two"))[0]

    assert first.root_fingerprint == second.root_fingerprint
    assert first.evidence_id != second.evidence_id

    other_check = replace(
        _failed_run("run-three").checks[0],
        argv=("python3", "-m", "pytest", "tests/unit/test_other.py"),
    )
    other_run = replace(_failed_run("run-three"), checks=(other_check,))
    other = adapt_harness_failure_evidence(other_run)[0]
    assert other.root_fingerprint != first.root_fingerprint


def test_verified_and_running_runs_do_not_create_failure_evidence() -> None:
    failed = _failed_run()
    verified_receipt = failed.receipt.model_copy(update={"status": "completed_verified"})
    verified = replace(failed, status="completed_verified", receipt=verified_receipt, checks=())
    running = replace(failed, status="running", completed_at="", receipt=None, checks=())

    assert adapt_harness_failure_evidence(verified) == ()
    assert adapt_harness_failure_evidence(running) == ()
