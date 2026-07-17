from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from naumi_agent.harness import eval as harness_eval
from naumi_agent.harness.eval import evaluate_suite_repetitions
from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalGuardrailStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_statistics import (
    EvalStatisticalVerdict,
    compare_eval_repetitions,
    render_eval_statistical_comparison,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError


def _identity(*, commit: str, repetitions: int = 5):
    return build_eval_baseline_identity(
        Path("."),
        configuration=HarnessEvalConfigurationIdentity.create(
            suite_id="statistical-protocol",
            suite_sha256="a" * 64,
            profile_sha256="b" * 64,
            policy_sha256=HarnessEvalComparisonPolicy().sha256,
            runner_version="protocol_hello@1",
            repetitions=repetitions,
            live=False,
        ),
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


def _suite(
    *,
    commit: str,
    statuses: tuple[EvalCaseStatus, ...],
    duration_ms: float,
    repetitions: int = 5,
) -> HarnessEvalSuiteResult:
    cases = tuple(
        HarnessEvalCaseResult(
            case_id=f"case-{index}",
            runner="protocol_hello",
            status=status,
        )
        for index, status in enumerate(statuses)
    )
    return HarnessEvalSuiteResult(
        suite_id="statistical-protocol",
        title="重复协议评测",
        suite_path="evals/statistical.yaml",
        suite_sha256="a" * 64,
        status=(
            EvalRunStatus.PASSED
            if all(status is EvalCaseStatus.PASSED for status in statuses)
            else EvalRunStatus.FAILED
        ),
        cases=cases,
        baseline_identity=_identity(commit=commit, repetitions=repetitions),
        duration_ms=duration_ms,
    )


def _runs(
    *,
    commit: str,
    statuses: tuple[EvalCaseStatus, ...],
    durations: tuple[float, ...] = (10, 11, 12, 13, 14),
    repetitions: int = 5,
) -> tuple[HarnessEvalSuiteResult, ...]:
    return tuple(
        _suite(
            commit=commit,
            statuses=statuses,
            duration_ms=duration,
            repetitions=repetitions,
        )
        for duration in durations
    )


def test_five_stable_samples_report_unchanged_with_real_statistics() -> None:
    baseline = _runs(
        commit="1",
        statuses=(EvalCaseStatus.PASSED, EvalCaseStatus.PASSED),
    )
    current = _runs(
        commit="2",
        statuses=(EvalCaseStatus.PASSED, EvalCaseStatus.PASSED),
        durations=(8, 9, 10, 11, 12),
    )

    result = compare_eval_repetitions(baseline, current)
    metrics = {item.metric: item for item in result.differences}

    assert result.verdict is EvalStatisticalVerdict.UNCHANGED
    assert result.code == ""
    assert result.flaky_cases == ()
    assert metrics["pass_rate"].delta == 0
    assert metrics["pass_rate"].confidence_low == 0
    assert metrics["pass_rate"].confidence_high == 0
    assert metrics["duration_ms"].delta == pytest.approx(-2.0)
    assert result.baseline[1].standard_deviation == pytest.approx(1.58113883)


def test_stable_pass_rate_drop_is_statistical_regression() -> None:
    baseline = _runs(
        commit="1",
        statuses=(EvalCaseStatus.PASSED, EvalCaseStatus.PASSED),
    )
    current = _runs(
        commit="2",
        statuses=(
            EvalCaseStatus.PASSED,
            EvalCaseStatus.IMPLEMENTATION_FAILURE,
        ),
    )

    result = compare_eval_repetitions(baseline, current)

    assert result.verdict is EvalStatisticalVerdict.REGRESSED
    pass_rate = next(item for item in result.differences if item.metric == "pass_rate")
    assert pass_rate.delta == -0.5
    assert pass_rate.confidence_high < 0


def test_stable_pass_rate_gain_is_statistical_improvement() -> None:
    baseline = _runs(
        commit="1",
        statuses=(
            EvalCaseStatus.PASSED,
            EvalCaseStatus.IMPLEMENTATION_FAILURE,
        ),
    )
    current = _runs(
        commit="2",
        statuses=(EvalCaseStatus.PASSED, EvalCaseStatus.PASSED),
    )

    result = compare_eval_repetitions(baseline, current)

    assert result.verdict is EvalStatisticalVerdict.IMPROVED
    pass_rate = next(item for item in result.differences if item.metric == "pass_rate")
    assert pass_rate.delta == 0.5
    assert pass_rate.confidence_low > 0


def test_case_status_variation_is_flaky_even_when_mean_looks_acceptable() -> None:
    baseline = _runs(
        commit="1",
        statuses=(EvalCaseStatus.PASSED, EvalCaseStatus.PASSED),
    )
    current = list(
        _runs(
            commit="2",
            statuses=(EvalCaseStatus.PASSED, EvalCaseStatus.PASSED),
        )
    )
    current[-1] = _suite(
        commit="2",
        statuses=(
            EvalCaseStatus.PASSED,
            EvalCaseStatus.IMPLEMENTATION_FAILURE,
        ),
        duration_ms=14,
    )

    result = compare_eval_repetitions(baseline, current)

    assert result.verdict is EvalStatisticalVerdict.FLAKY
    assert result.code == "case_status_flaky"
    assert [(item.case_id, item.cohort) for item in result.flaky_cases] == [
        ("case-1", "current")
    ]
    assert result.flaky_cases[0].observed_statuses == (
        "implementation_failure",
        "passed",
    )


def test_insufficient_or_identity_mismatched_samples_are_not_compared() -> None:
    short = _runs(
        commit="1",
        statuses=(EvalCaseStatus.PASSED,),
        durations=(10, 11, 12, 13),
        repetitions=4,
    )
    sufficient = _runs(commit="2", statuses=(EvalCaseStatus.PASSED,))

    insufficient = compare_eval_repetitions(short, sufficient)
    mismatched = compare_eval_repetitions(
        _runs(
            commit="1",
            statuses=(EvalCaseStatus.PASSED,),
            repetitions=6,
        ),
        sufficient,
    )

    assert insufficient.verdict is EvalStatisticalVerdict.INCONCLUSIVE
    assert insufficient.code == "sample_count_insufficient"
    assert mismatched.verdict is EvalStatisticalVerdict.INCOMPATIBLE
    assert mismatched.code == "sample_count_identity_mismatch"


def test_eval_error_remains_inconclusive_and_renderer_is_explicit() -> None:
    baseline = _runs(commit="1", statuses=(EvalCaseStatus.PASSED,))
    current = _runs(commit="2", statuses=(EvalCaseStatus.EVALUATION_ERROR,))

    result = compare_eval_repetitions(baseline, current)
    rendered = render_eval_statistical_comparison(result)

    assert result.verdict is EvalStatisticalVerdict.INCONCLUSIVE
    assert result.code == "evaluation_instability"
    assert "无法判断" in rendered
    assert "Baseline 5 · Current 5" in rendered
    assert "评测错误" in rendered


def test_minimum_samples_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="5..10000"):
        compare_eval_repetitions((), (), minimum_samples=4)


@pytest.mark.asyncio
async def test_real_static_suite_runs_persists_and_compares_five_samples(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "evals" / "fixtures" / "hello.json"
    fixture.parent.mkdir(parents=True)
    fixture_payload = {
        "type": "hello",
        "version": 1,
        "payload": {
            "client": "statistics-real",
            "minimum_version": 1,
            "maximum_version": 1,
            "capabilities": ["heartbeat", "typed_ui_messages"],
        },
    }
    fixture_raw = json.dumps(
        fixture_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    fixture.write_bytes(fixture_raw)
    suite = tmp_path / "evals" / "suite.yaml"
    suite.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "statistical-real",
                "title": "真实重复协议评测",
                "cases": [{
                    "id": "hello",
                    "runner": "protocol_hello",
                    "input": {"transport": "jsonl"},
                    "fixture": {
                        "path": "fixtures/hello.json",
                        "sha256": hashlib.sha256(fixture_raw).hexdigest(),
                    },
                    "expected": {
                        "outcome": "accepted",
                        "selected_version": 1,
                        "capabilities": ["heartbeat", "typed_ui_messages"],
                    },
                    "metrics": {
                        "primary": "protocol_outcome_match",
                        "guardrails": ["no_model", "no_side_effect"],
                    },
                }],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "tests@naumi.local")
    _git(tmp_path, "config", "user.name", "Naumi Tests")
    _git(tmp_path, "add", "evals")
    _git(tmp_path, "commit", "-qm", "eval fixture")

    batch = evaluate_suite_repetitions(
        tmp_path,
        suite,
        repetitions=5,
        profile_digest="b" * 64,
        profile_trusted=True,
    )
    runs = batch.results
    store = HarnessStore(tmp_path / "state" / "harness.db")
    for index, result in enumerate(runs):
        await store.record_eval_result(
            workspace_root=tmp_path,
            batch_id="real-static-001",
            sample_index=index,
            result=result,
            created_at="2026-07-18T10:00:00+08:00",
        )
    restored = await HarnessStore(store.db_path).list_eval_results(
        tmp_path,
        "real-static-001",
        "statistical-real",
    )
    comparison = compare_eval_repetitions(
        tuple(item.result for item in restored),
        tuple(item.result for item in restored),
    )
    first_baseline = await store.promote_eval_baseline(
        workspace_root=tmp_path,
        batch_id="real-static-001",
        suite_id="statistical-real",
        promoted_by="Harness-Test",
        promotion_reason="真实五次重复评测全绿",
        created_at="2026-07-18T10:01:00+08:00",
    )
    retry = await store.promote_eval_baseline(
        workspace_root=tmp_path,
        batch_id="real-static-001",
        suite_id="statistical-real",
        promoted_by="Different-Retry-Actor",
        promotion_reason="重试不得改写首次晋升事实",
        created_at="2026-07-18T10:02:00+08:00",
    )
    for index, result in enumerate(runs):
        await store.record_eval_result(
            workspace_root=tmp_path,
            batch_id="real-static-002",
            sample_index=index,
            result=result,
            created_at="2026-07-18T10:03:00+08:00",
        )
    second_baseline = await store.promote_eval_baseline(
        workspace_root=tmp_path,
        batch_id="real-static-002",
        suite_id="statistical-real",
        promoted_by="Harness-Test",
        promotion_reason="创建新版本并切换 selector",
        created_at="2026-07-18T10:04:00+08:00",
    )
    old_retry = await store.promote_eval_baseline(
        workspace_root=tmp_path,
        batch_id="real-static-001",
        suite_id="statistical-real",
        promoted_by="Late-Retry",
        promotion_reason="旧版本重试不得回拨 active selector",
        created_at="2026-07-18T10:04:30+08:00",
    )
    active = await HarnessStore(store.db_path).get_active_eval_baseline(
        tmp_path,
        "statistical-real",
    )
    versions = await store.list_eval_baselines(tmp_path, "statistical-real")
    events = await store.list_eval_baseline_events(tmp_path, "statistical-real")
    invalid_guardrails = tuple(
        item.model_copy(update={"status": EvalGuardrailStatus.UNVERIFIED})
        for item in runs[0].cases[0].guardrails
    )
    invalid_case = runs[0].cases[0].model_copy(
        update={"guardrails": invalid_guardrails}
    )
    invalid_result = runs[0].model_copy(update={"cases": (invalid_case,)})
    for index, result in enumerate((invalid_result, *runs[1:])):
        await store.record_eval_result(
            workspace_root=tmp_path,
            batch_id="real-static-invalid",
            sample_index=index,
            result=result,
            created_at="2026-07-18T10:05:00+08:00",
        )
    with pytest.raises(ValueError, match="guardrail"):
        await store.promote_eval_baseline(
            workspace_root=tmp_path,
            batch_id="real-static-invalid",
            suite_id="statistical-real",
            promoted_by="Harness-Test",
            promotion_reason="不得晋升未验证 guardrail",
            created_at="2026-07-18T10:06:00+08:00",
        )

    assert batch.status == "completed"
    assert batch.code == ""
    assert batch.completed == batch.requested == 5
    assert len(runs) == 5
    assert all(run.status is EvalRunStatus.PASSED for run in runs)
    assert all(run.baseline_identity is not None for run in runs)
    assert all(
        run.baseline_identity.configuration.repetitions == 5  # type: ignore[union-attr]
        for run in runs
    )
    assert len(restored) == 5
    assert comparison.verdict is EvalStatisticalVerdict.UNCHANGED
    assert retry == first_baseline
    assert first_baseline.version == 1
    assert second_baseline.version == 2
    assert old_retry == first_baseline
    assert active == second_baseline
    assert [item.version for item in versions] == [2, 1]
    assert [(item.baseline_id, item.previous_baseline_id) for item in events] == [
        (first_baseline.id, ""),
        (second_baseline.id, first_baseline.id),
    ]
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE harness_eval_baselines SET promotion_reason = ? "
            "WHERE id = ?",
            ("forged", second_baseline.id),
        )
        db.execute(
            "UPDATE harness_eval_baseline_events SET actor = ? WHERE baseline_id = ?",
            ("forged", second_baseline.id),
        )
        db.commit()
    with pytest.raises(HarnessStoreError, match="损坏"):
        await HarnessStore(store.db_path).get_active_eval_baseline(
            tmp_path,
            "statistical-real",
        )
    with pytest.raises(HarnessStoreError, match="损坏"):
        await HarnessStore(store.db_path).list_eval_baseline_events(
            tmp_path,
            "statistical-real",
        )


def test_repetition_runner_returns_explicit_partial_batch_on_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity(commit="1", repetitions=5)
    raw = _suite(
        commit="1",
        statuses=(EvalCaseStatus.PASSED,),
        duration_ms=1,
        repetitions=5,
    )
    clock = iter((0.0, 2.0, 3.0))
    monkeypatch.setattr(harness_eval.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(
        harness_eval,
        "_capture_baseline_source",
        lambda *_args, **_kwargs: (identity.source, ""),
    )
    monkeypatch.setattr(
        harness_eval,
        "_capture_baseline_source_after",
        lambda *_args, **_kwargs: (identity.source, ""),
    )
    monkeypatch.setattr(
        harness_eval,
        "_evaluate_suite_file_raw",
        lambda *_args, **_kwargs: raw,
    )

    batch = evaluate_suite_repetitions(
        ".",
        "suite.yaml",
        repetitions=5,
        profile_digest="b" * 64,
        profile_trusted=True,
        max_total_duration_ms=1_000,
    )

    assert batch.status == "partial"
    assert batch.code == "repetition_budget_exhausted"
    assert batch.requested == 5
    assert batch.completed == len(batch.results) == 1
    assert batch.results[0].baseline_identity is not None
    assert batch.results[0].baseline_identity.configuration.repetitions == 5


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
