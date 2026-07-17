from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from naumi_agent.harness.eval import evaluate_declared_suites
from naumi_agent.harness.eval_identity import HarnessEvalConfigurationIdentity
from naumi_agent.harness.eval_models import (
    EvalGuardrailStatus,
    HarnessEvalComparisonPolicy,
    HarnessEvalSuite,
    HarnessEvalSuiteResult,
)


def _suite_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "policy-protocol",
        "title": "Policy 协议评测",
        "cases": [
            {
                "id": "legacy",
                "runner": "protocol_hello",
                "input": {"transport": "jsonl"},
                "fixture": {"path": "fixtures/hello.json", "sha256": "a" * 64},
                "expected": {
                    "outcome": "accepted",
                    "selected_version": 1,
                    "capabilities": [
                        "heartbeat",
                        "typed_ui_messages",
                        "workbench_snapshot",
                    ],
                },
                "metrics": {
                    "primary": "protocol_outcome_match",
                    "guardrails": ["no_model", "no_side_effect"],
                },
            }
        ],
    }


def test_policy_schema_has_strict_safe_defaults_and_stable_digest() -> None:
    suite = HarnessEvalSuite.model_validate(_suite_payload())
    policy = suite.comparison_policy

    assert policy.min_pass_rate == 1.0
    assert policy.max_regressions == 0
    assert policy.max_implementation_failures == 0
    assert policy.max_pass_rate_drop == 0.0
    assert len(policy.sha256) == 64
    assert policy.sha256 == HarnessEvalComparisonPolicy().sha256
    assert policy.canonical_payload() == {
        "max_implementation_failures": 0,
        "max_pass_rate_drop": 0.0,
        "max_regressions": 0,
        "min_pass_rate": 1.0,
    }


def test_policy_schema_accepts_explicit_tolerance_and_rejects_unsafe_values() -> None:
    payload = _suite_payload()
    payload["comparison_policy"] = {
        "min_pass_rate": 0.95,
        "max_regressions": 1,
        "max_implementation_failures": 2,
        "max_pass_rate_drop": 0.02,
    }
    policy = HarnessEvalSuite.model_validate(payload).comparison_policy
    assert policy.min_pass_rate == 0.95
    assert policy.max_regressions == 1

    invalid_values = (
        {"min_pass_rate": -0.1},
        {"min_pass_rate": float("nan")},
        {"max_regressions": -1},
        {"max_pass_rate_drop": 1.1},
        {"max_evaluation_errors": 1},
        {"max_skipped": 1},
        {"unknown_threshold": 1},
    )
    for invalid in invalid_values:
        broken = _suite_payload()
        broken["comparison_policy"] = invalid
        with pytest.raises(ValidationError):
            HarnessEvalSuite.model_validate(broken)

    missing_guardrail = _suite_payload()
    case = missing_guardrail["cases"][0]  # type: ignore[index]
    case["metrics"]["guardrails"] = ["no_model"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="no_side_effect"):
        HarnessEvalSuite.model_validate(missing_guardrail)


def test_result_policy_digest_is_self_validating() -> None:
    policy = HarnessEvalComparisonPolicy(min_pass_rate=0.9)
    result = HarnessEvalSuiteResult(
        suite_id="policy-protocol",
        title="Policy 协议评测",
        suite_path="evals/policy.yaml",
        suite_sha256="a" * 64,
        status="passed",
        comparison_policy=policy,
    )

    assert result.policy_sha256 == policy.sha256
    with pytest.raises(ValidationError, match="policy_sha256"):
        HarnessEvalSuiteResult.model_validate(
            {**result.model_dump(mode="json"), "policy_sha256": "0" * 64}
        )


def test_configuration_identity_binds_policy_digest() -> None:
    strict = HarnessEvalConfigurationIdentity.create(
        suite_id="policy-protocol",
        suite_sha256="a" * 64,
        profile_sha256="b" * 64,
        policy_sha256=HarnessEvalComparisonPolicy().sha256,
        runner_version="protocol_hello@1",
        repetitions=1,
        live=False,
    )
    tolerant = HarnessEvalConfigurationIdentity.create(
        suite_id="policy-protocol",
        suite_sha256="a" * 64,
        profile_sha256="b" * 64,
        policy_sha256=HarnessEvalComparisonPolicy(min_pass_rate=0.9).sha256,
        runner_version="protocol_hello@1",
        repetitions=1,
        live=False,
    )

    assert strict.policy_sha256 != tolerant.policy_sha256
    assert strict.digest != tolerant.digest


def test_real_static_eval_snapshots_policy_metric_and_verified_guardrails(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    fixture = workspace / "evals" / "fixtures" / "hello.json"
    fixture.parent.mkdir(parents=True)
    raw = json.dumps(
        {"type": "hello", "version": 1, "payload": {"client": "legacy"}},
        sort_keys=True,
    ).encode("utf-8")
    fixture.write_bytes(raw)
    payload = _suite_payload()
    case = payload["cases"][0]  # type: ignore[index]
    case["fixture"]["sha256"] = hashlib.sha256(raw).hexdigest()  # type: ignore[index]
    payload["comparison_policy"] = {"min_pass_rate": 1.0}
    suite_path = workspace / "evals" / "policy.yaml"
    suite_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "tests@naumi.local")
    _git(workspace, "config", "user.name", "Naumi Tests")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "policy fixture")

    report = evaluate_declared_suites(
        workspace,
        ("evals/policy.yaml",),
        None,
        profile_digest="b" * 64,
        profile_trusted=True,
    )
    result = report.suites[0]
    case_result = result.cases[0]

    assert result.policy_sha256 == result.comparison_policy.sha256
    assert result.baseline_identity is not None
    assert result.baseline_identity.configuration.policy_sha256 == result.policy_sha256
    assert case_result.primary_metric == "protocol_outcome_match"
    assert [item.guardrail for item in case_result.guardrails] == [
        "no_model",
        "no_side_effect",
    ]
    assert all(item.status is EvalGuardrailStatus.PASSED for item in case_result.guardrails)


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
