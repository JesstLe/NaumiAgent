from __future__ import annotations

import pytest
from pydantic import ValidationError

from naumi_agent.harness.checks import HarnessCheckResult, HarnessCheckStatus
from naumi_agent.harness.completion import (
    CompletionGate,
    CompletionGateInput,
    HarnessEvidenceRef,
    build_completion_contract,
)
from naumi_agent.harness.models import HarnessTaskKind


def _contract(**overrides: object):
    values: dict[str, object] = {
        "run_id": "run-1",
        "session_id": "session-1",
        "task_kind": HarnessTaskKind.CHANGE,
        "objective": "修改 Harness 并完成验证",
        "acceptance_criteria": (
            {"id": "ac-tests", "description": "定向测试通过"},
        ),
        "allowed_scope": ("src/naumi_agent/harness/**", "tests/unit/test_harness_*.py"),
        "prohibited_scope": ("src/naumi_agent/safety/secrets.py",),
        "required_checks": ("unit",),
        "required_evidence": ("test_report",),
        "correction_attempts": 1,
        "unverified_status": "completed_unverified",
        "source_refs": ("user:turn-1",),
    }
    values.update(overrides)
    return build_completion_contract(**values)


def _passed_check(*, fingerprint: str = "sha256:current") -> HarnessCheckResult:
    return HarnessCheckResult(
        check_id="unit",
        run_id="run-1",
        status=HarnessCheckStatus.PASSED,
        tree_fingerprint=fingerprint,
        profile_digest="profile",
        message="passed",
    )


def _evidence(*, criterion_ids: tuple[str, ...] = ("ac-tests",)) -> HarnessEvidenceRef:
    return HarnessEvidenceRef(
        id="evidence-1",
        kind="test_report",
        summary="定向测试通过",
        criterion_ids=criterion_ids,
    )


def test_contract_is_strict_bounded_and_normalized() -> None:
    contract = _contract(objective="  修改 Harness  ")

    assert contract.objective == "修改 Harness"
    assert contract.task_kind is HarnessTaskKind.CHANGE
    assert contract.acceptance_criteria[0].id == "ac-tests"

    with pytest.raises(ValidationError):
        _contract(unknown_field=True)
    with pytest.raises(ValidationError, match="objective"):
        _contract(objective=" ")
    with pytest.raises(ValidationError, match="重复"):
        _contract(required_checks=("unit", "unit"))


def test_mutating_tool_mechanically_upgrades_answer_to_change() -> None:
    answer = _contract(
        task_kind=HarnessTaskKind.ANSWER,
        required_checks=(),
        required_evidence=(),
        acceptance_criteria=(),
    )

    assert answer.effective_task_kind(mutating_tool_used=False) is HarnessTaskKind.ANSWER
    assert answer.effective_task_kind(mutating_tool_used=True) is HarnessTaskKind.CHANGE


def test_answer_contract_cannot_verify_after_persistent_change() -> None:
    answer = _contract(
        task_kind=HarnessTaskKind.ANSWER,
        required_checks=(),
        required_evidence=(),
        acceptance_criteria=(),
        allowed_scope=(),
        prohibited_scope=(),
    )

    result = CompletionGate().evaluate(
        answer,
        CompletionGateInput(
            current_tree_fingerprint="sha256:current",
            changed_paths=("answer-wrote-file.txt",),
            mutating_tool_used=True,
        ),
        correction_attempt=0,
    )

    assert result.status == "needs_correction"
    assert "change 契约" in result.correction_instruction

    final = CompletionGate().evaluate(
        answer,
        CompletionGateInput(
            current_tree_fingerprint="sha256:current",
            changed_paths=("answer-wrote-file.txt",),
        ),
        correction_attempt=1,
    )
    assert final.receipt is not None
    assert final.receipt.task_kind is HarnessTaskKind.CHANGE


def test_missing_check_gets_one_correction_then_unverified_receipt() -> None:
    contract = _contract()
    gate = CompletionGate()
    facts = CompletionGateInput(
        current_tree_fingerprint="sha256:current",
        changed_paths=("src/naumi_agent/harness/completion.py",),
        evidence=(_evidence(),),
    )

    first = gate.evaluate(contract, facts, correction_attempt=0)
    final = gate.evaluate(contract, facts, correction_attempt=1)

    assert first.status == "needs_correction"
    assert first.receipt is None
    assert "unit" in first.correction_instruction
    assert final.status == "completed_unverified"
    assert final.receipt is not None
    assert final.receipt.status == "completed_unverified"
    assert final.receipt.checks[0].status == "missing"
    assert "缺少必需检查" in final.receipt.warnings[0]


def test_current_check_and_criterion_evidence_produce_verified_receipt() -> None:
    result = CompletionGate().evaluate(
        _contract(profile_digest="profile"),
        CompletionGateInput(
            current_tree_fingerprint="sha256:current",
            current_profile_digest="profile",
            changed_paths=("src/naumi_agent/harness/completion.py",),
            checks=(_passed_check(),),
            evidence=(_evidence(),),
        ),
        correction_attempt=0,
    )

    assert result.status == "completed_verified"
    assert result.receipt is not None
    assert result.receipt.status == "completed_verified"
    assert result.receipt.criteria[0].status == "satisfied"
    assert result.receipt.criteria[0].evidence_ids == ("evidence-1",)
    assert result.receipt.changed_files == (
        "src/naumi_agent/harness/completion.py",
    )


def test_stale_passed_check_does_not_verify_current_tree() -> None:
    result = CompletionGate().evaluate(
        _contract(),
        CompletionGateInput(
            current_tree_fingerprint="sha256:current",
            changed_paths=("src/naumi_agent/harness/completion.py",),
            checks=(_passed_check(fingerprint="sha256:old"),),
            evidence=(_evidence(),),
        ),
        correction_attempt=0,
    )

    assert result.status == "needs_correction"
    assert "当前工作树" in result.correction_instruction


def test_profile_digest_change_invalidates_contract_and_check() -> None:
    result = CompletionGate().evaluate(
        _contract(profile_digest="profile"),
        CompletionGateInput(
            current_tree_fingerprint="sha256:current",
            current_profile_digest="different-profile",
            changed_paths=("src/naumi_agent/harness/completion.py",),
            checks=(_passed_check(),),
            evidence=(_evidence(),),
        ),
        correction_attempt=0,
    )

    assert result.status == "needs_correction"
    assert "Profile digest" in result.correction_instruction


@pytest.mark.parametrize(
    "changed_path",
    [
        "README.md",
        "src/naumi_agent/safety/secrets.py",
        "../outside.py",
        "/tmp/outside.py",
        "C:/outside.py",
    ],
)
def test_scope_violation_is_never_verified(changed_path: str) -> None:
    facts = CompletionGateInput(
        current_tree_fingerprint="sha256:current",
        changed_paths=(changed_path,),
        checks=(_passed_check(),),
        evidence=(_evidence(),),
    )
    gate = CompletionGate()

    first = gate.evaluate(_contract(), facts, correction_attempt=0)
    final = gate.evaluate(_contract(), facts, correction_attempt=1)

    assert first.status == "needs_correction"
    assert final.status == "blocked"
    assert final.receipt is not None
    assert any("scope" in warning for warning in final.receipt.warnings)


def test_pending_todo_and_undisclosed_failure_block_verified_completion() -> None:
    result = CompletionGate().evaluate(
        _contract(),
        CompletionGateInput(
            current_tree_fingerprint="sha256:current",
            changed_paths=("src/naumi_agent/harness/completion.py",),
            checks=(_passed_check(),),
            evidence=(_evidence(),),
            pending_todo_ids=("todo-2",),
            known_failure_ids=("failure-1",),
            disclosed_failure_ids=(),
        ),
        correction_attempt=0,
    )

    assert result.status == "needs_correction"
    assert "todo-2" in result.correction_instruction
    assert "failure-1" in result.correction_instruction


def test_missing_criterion_evidence_is_distinct_from_missing_evidence_kind() -> None:
    result = CompletionGate().evaluate(
        _contract(),
        CompletionGateInput(
            current_tree_fingerprint="sha256:current",
            changed_paths=("src/naumi_agent/harness/completion.py",),
            checks=(_passed_check(),),
            evidence=(_evidence(criterion_ids=()),),
        ),
        correction_attempt=0,
    )

    assert result.status == "needs_correction"
    assert "验收标准 ac-tests" in result.correction_instruction


def test_profile_can_require_blocked_instead_of_unverified() -> None:
    result = CompletionGate().evaluate(
        _contract(unverified_status="blocked"),
        CompletionGateInput(
            current_tree_fingerprint="sha256:current",
            changed_paths=("src/naumi_agent/harness/completion.py",),
        ),
        correction_attempt=1,
    )

    assert result.status == "blocked"
    assert result.receipt is not None
    assert result.receipt.status == "blocked"


def test_gate_input_rejects_blank_tree_fingerprint() -> None:
    with pytest.raises(ValidationError, match="current_tree_fingerprint"):
        CompletionGateInput(current_tree_fingerprint=" ")


def test_gate_input_rejects_ambiguous_duplicate_evidence_and_checks() -> None:
    with pytest.raises(ValidationError, match="重复 evidence id"):
        CompletionGateInput(
            current_tree_fingerprint="sha256:current",
            evidence=(_evidence(), _evidence()),
        )
    with pytest.raises(ValidationError, match="重复 check 结果"):
        CompletionGateInput(
            current_tree_fingerprint="sha256:current",
            checks=(_passed_check(), _passed_check()),
        )
