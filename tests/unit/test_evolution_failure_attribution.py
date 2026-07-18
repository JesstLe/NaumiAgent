from __future__ import annotations

from pathlib import Path

from naumi_agent.evolution.failure_attribution import (
    FailureAttributionCategory,
    _classify,
)
from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_receipt import (
    EvalReceiptSample,
    build_eval_comparison_receipt,
    eval_result_sha256,
    eval_sample_set_sha256,
)


def _result(
    *,
    commit: str,
    status: EvalCaseStatus,
    repetitions: int,
    policy: HarnessEvalComparisonPolicy | None = None,
    suite_sha256: str = "a" * 64,
) -> HarnessEvalSuiteResult:
    comparison_policy = policy or HarnessEvalComparisonPolicy()
    configuration = HarnessEvalConfigurationIdentity.create(
        suite_id="attribution",
        suite_sha256=suite_sha256,
        profile_sha256="b" * 64,
        policy_sha256=comparison_policy.sha256,
        runner_version="attribution@1",
        repetitions=repetitions,
        live=False,
    )
    identity = build_eval_baseline_identity(
        Path("."),
        configuration=configuration,
        source_identity=HarnessEvalSourceIdentity(
            commit=commit * 40,
            tree_sha256=f"sha256:{commit * 64}",
            dirty=False,
        ),
        platform_identity=HarnessEvalPlatformIdentity(
            system="linux",
            release="6.12",
            machine="x86_64",
            python_implementation="CPython",
            python_version="3.13.5",
            naumi_version="0.1.214",
        ),
    )
    return HarnessEvalSuiteResult(
        suite_id="attribution",
        title="Failure attribution fixture",
        suite_path="evals/attribution.yaml",
        suite_sha256=suite_sha256,
        status=(
            EvalRunStatus.PASSED
            if status is EvalCaseStatus.PASSED
            else EvalRunStatus.FAILED
        ),
        cases=(HarnessEvalCaseResult(
            case_id="target",
            runner="attribution@1",
            status=status,
            primary_metric="outcome",
        ),),
        comparison_policy=comparison_policy,
        baseline_identity=identity,
        duration_ms=10,
    )


def _samples(results: tuple[HarnessEvalSuiteResult, ...]) -> tuple[EvalReceiptSample, ...]:
    return tuple(
        EvalReceiptSample(
            sample_index=index,
            result_sha256=eval_result_sha256(result),
            result=result,
        )
        for index, result in enumerate(results)
    )


def _receipt(
    tmp_path: Path,
    baseline: tuple[HarnessEvalSuiteResult, ...],
    current: tuple[HarnessEvalSuiteResult, ...],
):
    baseline_samples = _samples(baseline)
    return build_eval_comparison_receipt(
        workspace_root=tmp_path,
        suite_id="attribution",
        baseline_id="c" * 64,
        baseline_batch_id="red",
        baseline_samples_sha256=eval_sample_set_sha256(baseline_samples),
        baseline_samples=baseline_samples,
        current_batch_id="green",
        current_samples=_samples(current),
        created_at="2026-07-19T04:00:00+08:00",
    )


def test_failure_attribution_classifies_candidate_flaky_and_infrastructure(
    tmp_path: Path,
) -> None:
    baseline = tuple(
        _result(commit="1", status=EvalCaseStatus.PASSED, repetitions=5)
        for _ in range(5)
    )
    failed = tuple(
        _result(
            commit="2",
            status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
            repetitions=5,
        )
        for _ in range(5)
    )
    permissive = HarnessEvalComparisonPolicy(
        min_pass_rate=0,
        max_implementation_failures=1,
        max_regressions=1,
        max_pass_rate_drop=1,
    )
    permissive_baseline = tuple(
        _result(
            commit="3",
            status=EvalCaseStatus.PASSED,
            repetitions=5,
            policy=permissive,
        )
        for _ in range(5)
    )
    mixed = [
        _result(
            commit="4",
            status=EvalCaseStatus.PASSED,
            repetitions=5,
            policy=permissive,
        )
        for _ in range(5)
    ]
    mixed[-1] = _result(
        commit="4",
        status=EvalCaseStatus.IMPLEMENTATION_FAILURE,
        repetitions=5,
        policy=permissive,
    )
    unstable = tuple(
        _result(
            commit="5",
            status=EvalCaseStatus.EVALUATION_ERROR,
            repetitions=5,
        )
        for _ in range(5)
    )

    candidate = _classify(_receipt(tmp_path, baseline, failed))
    flaky = _classify(_receipt(tmp_path, permissive_baseline, tuple(mixed)))
    infrastructure = _classify(_receipt(tmp_path, baseline, unstable))

    assert candidate["category"] == FailureAttributionCategory.CANDIDATE_DEFECT
    assert candidate["candidate_fault"] is True
    assert flaky["category"] == FailureAttributionCategory.FLAKY_EVIDENCE
    assert flaky["requires_rerun"] is True
    assert infrastructure["category"] == (
        FailureAttributionCategory.EVALUATION_INFRASTRUCTURE
    )
    assert infrastructure["retryable"] is True


def test_failure_attribution_classifies_incomplete_and_incompatible(
    tmp_path: Path,
) -> None:
    short_baseline = tuple(
        _result(commit="1", status=EvalCaseStatus.PASSED, repetitions=4)
        for _ in range(4)
    )
    short_current = tuple(
        _result(commit="2", status=EvalCaseStatus.PASSED, repetitions=4)
        for _ in range(4)
    )
    baseline = tuple(
        _result(commit="3", status=EvalCaseStatus.PASSED, repetitions=5)
        for _ in range(5)
    )
    incompatible = tuple(
        _result(
            commit="4",
            status=EvalCaseStatus.PASSED,
            repetitions=5,
            suite_sha256="d" * 64,
        )
        for _ in range(5)
    )

    incomplete = _classify(_receipt(tmp_path, short_baseline, short_current))
    environment = _classify(_receipt(tmp_path, baseline, incompatible))

    assert incomplete["category"] == FailureAttributionCategory.EVIDENCE_INCOMPLETE
    assert incomplete["reason_code"] == "sample_count_insufficient"
    assert environment["category"] == (
        FailureAttributionCategory.ENVIRONMENT_INCOMPATIBLE
    )
    assert environment["action"] == "rebuild_environment"
