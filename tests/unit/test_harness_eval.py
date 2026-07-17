from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from naumi_agent.harness.eval import (
    evaluate_declared_suites,
    evaluate_suite_file,
    render_harness_eval,
)
from naumi_agent.harness.eval_models import EvalCaseStatus, EvalRunStatus


def _write_fixture(root: Path, name: str, payload: dict[str, object]) -> tuple[str, str]:
    relative = f"fixtures/{name}.json"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    return relative, hashlib.sha256(raw).hexdigest()


def _case(
    case_id: str,
    fixture_path: str,
    digest: str,
    *,
    outcome: str,
    error_code: str | None = None,
    selected_version: int | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, object]:
    expected: dict[str, object] = {"outcome": outcome}
    if error_code is not None:
        expected["error_code"] = error_code
    if selected_version is not None:
        expected["selected_version"] = selected_version
    if capabilities is not None:
        expected["capabilities"] = capabilities
    return {
        "id": case_id,
        "runner": "protocol_hello",
        "input": {"transport": "jsonl"},
        "fixture": {"path": fixture_path, "sha256": digest},
        "expected": expected,
        "metrics": {
            "primary": "protocol_outcome_match",
            "guardrails": ["no_model", "no_side_effect"],
        },
        "budget": {"max_duration_ms": 100},
    }


def _write_suite(root: Path, cases: list[dict[str, object]], **extra: object) -> Path:
    import yaml

    path = root / "suite.yaml"
    payload: dict[str, object] = {
        "schema_version": 1,
        "id": "protocol-hello-test",
        "title": "协议 hello 测试",
        "cases": cases,
        "budget": {"max_duration_ms": 5_000},
        **extra,
    }
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _modern_record() -> dict[str, object]:
    return {
        "type": "hello",
        "version": 1,
        "payload": {
            "client": "eval-ui",
            "minimum_version": 1,
            "maximum_version": 1,
            "capabilities": [
                "typed_ui_messages",
                "heartbeat",
                "unknown_client_feature",
            ],
        },
    }


def test_offline_protocol_eval_runs_production_negotiation_and_is_deterministic(
    tmp_path: Path,
) -> None:
    suite_root = tmp_path / "evals"
    fixture, digest = _write_fixture(suite_root, "modern", _modern_record())
    suite = _write_suite(
        suite_root,
        [
            _case(
                "modern-compatible",
                fixture,
                digest,
                outcome="accepted",
                selected_version=1,
                capabilities=["heartbeat", "typed_ui_messages"],
            )
        ],
    )

    first = evaluate_suite_file(tmp_path, suite)
    second = evaluate_suite_file(tmp_path, suite)

    assert first.status is EvalRunStatus.PASSED
    assert first.cases[0].status is EvalCaseStatus.PASSED
    assert first.cases[0].actual is not None
    assert first.cases[0].actual.capabilities == ("heartbeat", "typed_ui_messages")
    assert first.canonical_payload() == second.canonical_payload()


def test_eval_separates_implementation_failure_from_evaluation_error(
    tmp_path: Path,
) -> None:
    suite_root = tmp_path / "evals"
    fixture, digest = _write_fixture(suite_root, "modern", _modern_record())
    mismatch = _case(
        "wrong-expectation",
        fixture,
        digest,
        outcome="rejected",
        error_code="protocol_version_unsupported",
    )
    broken_digest = _case(
        "broken-fixture",
        fixture,
        "0" * 64,
        outcome="accepted",
        selected_version=1,
        capabilities=["heartbeat", "typed_ui_messages"],
    )
    suite = _write_suite(suite_root, [mismatch, broken_digest])

    result = evaluate_suite_file(tmp_path, suite)

    assert result.status is EvalRunStatus.FAILED
    assert [case.status for case in result.cases] == [
        EvalCaseStatus.IMPLEMENTATION_FAILURE,
        EvalCaseStatus.EVALUATION_ERROR,
    ]
    assert result.implementation_failures == 1
    assert result.evaluation_errors == 1
    assert "预期 rejected" in result.cases[0].message
    assert result.cases[1].code == "fixture_digest_mismatch"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda case: case.update({"unknown": True}), "suite_schema_invalid"),
        (lambda case: case.update({"runner": "shell"}), "suite_schema_invalid"),
        (
            lambda case: case["expected"].update({"error_code": "Bad Code"}),  # type: ignore[union-attr]
            "suite_schema_invalid",
        ),
        (
            lambda case: case["fixture"].update({"path": "../outside.json"}),  # type: ignore[union-attr]
            "suite_schema_invalid",
        ),
    ],
)
def test_eval_rejects_unknown_fields_runners_and_dangerous_paths(
    tmp_path: Path,
    mutate,
    code: str,
) -> None:
    suite_root = tmp_path / "evals"
    fixture, digest = _write_fixture(suite_root, "modern", _modern_record())
    case = _case(
        "invalid-case",
        fixture,
        digest,
        outcome="accepted",
        selected_version=1,
        capabilities=["heartbeat", "typed_ui_messages"],
    )
    mutate(case)
    suite = _write_suite(suite_root, [case])

    result = evaluate_suite_file(tmp_path, suite)

    assert result.status is EvalRunStatus.EVALUATION_ERROR
    assert result.code == code
    assert "Eval Suite" in result.message


def test_declared_suite_selection_is_allowlisted_and_actionable(tmp_path: Path) -> None:
    suite_root = tmp_path / "docs" / "harness" / "evals"
    fixture, digest = _write_fixture(suite_root, "modern", _modern_record())
    suite = _write_suite(
        suite_root,
        [
            _case(
                "modern",
                fixture,
                digest,
                outcome="accepted",
                selected_version=1,
                capabilities=["heartbeat", "typed_ui_messages"],
            )
        ],
    )
    declared = (suite.relative_to(tmp_path).as_posix(),)

    selected = evaluate_declared_suites(tmp_path, declared, "protocol-hello-test")
    windows_path = evaluate_declared_suites(
        tmp_path,
        declared,
        declared[0].replace("/", "\\"),
    )
    unknown = evaluate_declared_suites(tmp_path, declared, "other-suite")
    none = evaluate_declared_suites(tmp_path, (), None)

    assert selected.status is EvalRunStatus.PASSED
    assert len(selected.suites) == 1
    assert windows_path.status is EvalRunStatus.PASSED
    assert unknown.status is EvalRunStatus.EVALUATION_ERROR
    assert unknown.code == "suite_not_declared"
    assert "Profile" in unknown.message
    assert none.code == "no_suites_declared"
    assert "evals.suites" in none.message


def test_eval_renderer_is_compact_chinese_and_exposes_failure_classes(
    tmp_path: Path,
) -> None:
    suite_root = tmp_path / "evals"
    fixture, digest = _write_fixture(suite_root, "modern", _modern_record())
    suite = _write_suite(
        suite_root,
        [
            _case(
                "wrong",
                fixture,
                digest,
                outcome="rejected",
                error_code="protocol_version_unsupported",
            )
        ],
    )

    rendered = render_harness_eval(evaluate_suite_file(tmp_path, suite))

    assert "Harness 离线 Eval" in rendered
    assert "实现回归 1" in rendered
    assert "评测错误 0" in rendered
    assert "wrong" in rendered
    assert "下一步" in rendered
    assert "Traceback" not in rendered


def test_eval_rejects_duplicate_case_ids_and_oversized_suite(tmp_path: Path) -> None:
    suite_root = tmp_path / "evals"
    fixture, digest = _write_fixture(suite_root, "modern", _modern_record())
    case = _case(
        "duplicate",
        fixture,
        digest,
        outcome="accepted",
        selected_version=1,
        capabilities=["heartbeat", "typed_ui_messages"],
    )
    duplicate = _write_suite(suite_root, [case, case])
    duplicate_result = evaluate_suite_file(tmp_path, duplicate)

    oversized = suite_root / "oversized.yaml"
    oversized.write_text("x" * (256 * 1024 + 1), encoding="utf-8")
    oversized_result = evaluate_suite_file(tmp_path, oversized)

    assert duplicate_result.code == "suite_schema_invalid"
    assert oversized_result.code == "eval_suite_too_large"


def test_eval_rejects_fixture_symlink_escape_invalid_json_and_oversize(
    tmp_path: Path,
) -> None:
    suite_root = tmp_path / "evals"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    linked = suite_root / "fixtures" / "linked.json"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)
    linked_case = _case(
        "linked",
        "fixtures/linked.json",
        hashlib.sha256(outside.read_bytes()).hexdigest(),
        outcome="rejected",
        error_code="bad_request",
    )

    invalid = suite_root / "fixtures" / "invalid.json"
    invalid.write_text("{broken", encoding="utf-8")
    invalid_case = _case(
        "invalid-json",
        "fixtures/invalid.json",
        hashlib.sha256(invalid.read_bytes()).hexdigest(),
        outcome="rejected",
        error_code="bad_request",
    )

    huge = suite_root / "fixtures" / "huge.json"
    huge.write_text(" " * (64 * 1024 + 1), encoding="utf-8")
    huge_case = _case(
        "huge",
        "fixtures/huge.json",
        hashlib.sha256(huge.read_bytes()).hexdigest(),
        outcome="rejected",
        error_code="bad_request",
    )
    suite = _write_suite(suite_root, [linked_case, invalid_case, huge_case])

    result = evaluate_suite_file(tmp_path, suite)

    assert [case.code for case in result.cases] == [
        "fixture_outside_suite",
        "fixture_invalid_json",
        "eval_fixture_too_large",
    ]
    assert all(case.status is EvalCaseStatus.EVALUATION_ERROR for case in result.cases)


def test_suite_budget_skips_cases_without_running_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import naumi_agent.harness.eval as eval_module

    suite_root = tmp_path / "evals"
    fixture, digest = _write_fixture(suite_root, "modern", _modern_record())
    suite = _write_suite(
        suite_root,
        [
            _case(
                "never-runs",
                fixture,
                digest,
                outcome="accepted",
                selected_version=1,
                capabilities=["heartbeat", "typed_ui_messages"],
            )
        ],
    )
    values = iter((0.0, 10.0, 10.0, 10.0))
    monkeypatch.setattr(eval_module.time, "perf_counter", lambda: next(values))

    result = evaluate_suite_file(tmp_path, suite)

    assert result.status is EvalRunStatus.FAILED
    assert result.skipped == 1
    assert result.cases[0].code == "suite_budget_exhausted"


def test_suite_budget_overrun_after_last_case_fails_the_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import naumi_agent.harness.eval as eval_module

    suite_root = tmp_path / "evals"
    fixture, digest = _write_fixture(suite_root, "modern", _modern_record())
    suite = _write_suite(
        suite_root,
        [
            _case(
                "finishes-late",
                fixture,
                digest,
                outcome="accepted",
                selected_version=1,
                capabilities=["heartbeat", "typed_ui_messages"],
            )
        ],
    )
    values = iter((0.0, 0.0, 0.0, 0.0, 10.0, 10.0))
    monkeypatch.setattr(eval_module.time, "perf_counter", lambda: next(values))

    result = evaluate_suite_file(tmp_path, suite)

    assert result.status is EvalRunStatus.FAILED
    assert result.code == "suite_budget_exhausted"
    assert result.passed == 1
