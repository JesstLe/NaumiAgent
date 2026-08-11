from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from naumi_agent.evolution.eval_metric_opportunities import (
    EvolutionEvalMetricOpportunityError,
    EvolutionEvalMetricOpportunityService,
    render_eval_metric_opportunity,
)
from naumi_agent.evolution.evidence import EvolutionQuantitativeMetric
from naumi_agent.evolution.store import EvolutionCandidateStore
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
    HarnessEvalMetricObservation,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_receipt import (
    EvalReceiptSample,
    build_eval_comparison_receipt,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.tools.evolution_review import EvolutionEvalMetricOpportunityTool

_NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC).isoformat()
_METRIC = "runtime.request.latency_ms"


def _identity(*, workspace: Path, commit: str):
    return build_eval_baseline_identity(
        workspace,
        configuration=HarnessEvalConfigurationIdentity.create(
            suite_id="runtime-budget",
            suite_sha256="a" * 64,
            profile_sha256="b" * 64,
            policy_sha256=HarnessEvalComparisonPolicy().sha256,
            runner_version="runtime_budget@1",
            repetitions=5,
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
    workspace: Path,
    commit: str,
    value: float,
    metric: str = _METRIC,
    unit: str = "milliseconds",
    direction: str = "decrease",
    target: float = 100,
) -> HarnessEvalSuiteResult:
    observation = HarnessEvalMetricObservation(
        metric=metric,
        value=value,
        unit=unit,
        direction=direction,
        target=target,
        primary=True,
    )
    return HarnessEvalSuiteResult(
        suite_id="runtime-budget",
        title="Runtime 预算",
        suite_path="evolution:runtime-budget",
        suite_sha256="a" * 64,
        status=EvalRunStatus.PASSED,
        cases=(HarnessEvalCaseResult(
            case_id="request",
            runner="runtime_budget@1",
            status=EvalCaseStatus.PASSED,
            primary_metric=metric,
            metric_observations=(observation,),
        ),),
        baseline_identity=_identity(workspace=workspace, commit=commit),
    )


def _status_suite(
    *,
    workspace: Path,
    commit: str,
    statuses: tuple[EvalCaseStatus, ...],
) -> HarnessEvalSuiteResult:
    return HarnessEvalSuiteResult(
        suite_id="runtime-budget",
        title="Runtime 状态",
        suite_path="evolution:runtime-budget",
        suite_sha256="a" * 64,
        status=(
            EvalRunStatus.PASSED
            if all(item is EvalCaseStatus.PASSED for item in statuses)
            else EvalRunStatus.FAILED
        ),
        cases=tuple(
            HarnessEvalCaseResult(
                case_id=f"status-{index}",
                runner="runtime_budget@1",
                status=status,
            )
            for index, status in enumerate(statuses)
        ),
        baseline_identity=_identity(workspace=workspace, commit=commit),
    )


async def _comparison(
    tmp_path: Path,
    *,
    current_values: tuple[float, ...] = (30, 31, 32, 33, 34),
    baseline_values: tuple[float, ...] = (10, 11, 12, 13, 14),
    metric: str = _METRIC,
    unit: str = "milliseconds",
    direction: str = "decrease",
    target: float = 100,
) -> tuple[HarnessStore, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = HarnessStore(tmp_path / "harness.db")
    for index, value in enumerate(baseline_values):
        await harness.record_eval_result(
            workspace_root=workspace,
            batch_id="baseline-001",
            sample_index=index,
            result=_suite(
                workspace=workspace,
                commit="1",
                value=value,
                metric=metric,
                unit=unit,
                direction=direction,
                target=target,
            ),
            created_at=_NOW,
        )
    baseline = await harness.promote_eval_baseline(
        workspace_root=workspace,
        batch_id="baseline-001",
        suite_id="runtime-budget",
        promoted_by="Harness-Test",
        promotion_reason="稳定基线",
        created_at=_NOW,
    )
    for index, value in enumerate(current_values):
        await harness.record_eval_result(
            workspace_root=workspace,
            batch_id="current-001",
            sample_index=index,
            result=_suite(
                workspace=workspace,
                commit="2",
                value=value,
                metric=metric,
                unit=unit,
                direction=direction,
                target=target,
            ),
            created_at=_NOW,
        )
    baseline_rows = await harness.list_eval_results(
        workspace,
        "baseline-001",
        "runtime-budget",
    )
    current_rows = await harness.list_eval_results(
        workspace,
        "current-001",
        "runtime-budget",
    )
    receipt = build_eval_comparison_receipt(
        workspace_root=workspace,
        suite_id="runtime-budget",
        baseline_id=baseline.id,
        baseline_batch_id=baseline.batch_id,
        baseline_samples_sha256=baseline.samples_sha256,
        baseline_samples=tuple(
            EvalReceiptSample(
                sample_index=item.sample_index,
                result_sha256=item.result_sha256,
                result=item.result,
            )
            for item in baseline_rows
        ),
        current_batch_id="current-001",
        current_samples=tuple(
            EvalReceiptSample(
                sample_index=item.sample_index,
                result_sha256=item.result_sha256,
                result=item.result,
            )
            for item in current_rows
        ),
        created_at=_NOW,
    )
    await harness.record_eval_comparison_receipt(receipt)
    return harness, receipt.id


@pytest.mark.asyncio
async def test_real_h5c_regression_discovers_one_idempotent_candidate(
    tmp_path: Path,
) -> None:
    harness, comparison_id = await _comparison(tmp_path)
    workspace = tmp_path / "workspace"
    candidate_store = EvolutionCandidateStore(tmp_path / "evolution.db")
    service = EvolutionEvalMetricOpportunityService(
        workspace_root=workspace,
        harness_store=harness,
        candidate_store=candidate_store,
    )

    results = await asyncio.gather(
        *(service.discover(comparison_id=comparison_id) for _ in range(8))
    )

    assert all(result == results[0] for result in results)
    result = results[0]
    assert len(result.candidates) == 1
    candidate_result = result.candidates[0]
    assert candidate_result.finding_code == "eval_latency_regression"
    assert candidate_result.metric.name == _METRIC
    assert candidate_result.metric.baseline_mean == 12
    assert candidate_result.metric.current_mean == 32
    assert candidate_result.metric.confidence_low > 0
    assert candidate_result.candidate_revision == 1
    assert candidate_result.occurrence_count == 1
    stored = await candidate_store.get_candidate(
        workspace,
        candidate_result.candidate_id,
    )
    assert stored is not None
    assert stored.draft.source_kinds == ("eval_metric_regression",)
    assert stored.draft.evidence[0].schema_version == 2
    assert stored.draft.expected_metrics[0].name == _METRIC
    assert stored.draft.expected_metrics[0].target == 100
    assert await service.validate_candidate_sources(stored.draft)
    encoded = stored.draft.model_dump_json()
    assert str(workspace) not in encoded
    assert "baseline-001" not in encoded
    assert '"request"' not in encoded
    rendered = render_eval_metric_opportunity(result)
    assert "12 → 32 milliseconds" in rendered
    assert "95% CI" in rendered


@pytest.mark.asyncio
async def test_source_tamper_invalidates_existing_candidate(tmp_path: Path) -> None:
    harness, comparison_id = await _comparison(tmp_path)
    workspace = tmp_path / "workspace"
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    service = EvolutionEvalMetricOpportunityService(
        workspace_root=workspace,
        harness_store=harness,
        candidate_store=store,
    )
    result = await service.discover(comparison_id=comparison_id)
    candidate = await store.get_candidate(workspace, result.candidates[0].candidate_id)
    assert candidate is not None

    with sqlite3.connect(harness.db_path) as db:
        row = db.execute(
            "SELECT result_json FROM harness_eval_results "
            "WHERE batch_id = 'current-001' AND sample_index = 0"
        ).fetchone()
        payload = json.loads(row[0])
        payload["cases"][0]["metric_observations"][0]["value"] = 99
        db.execute(
            "UPDATE harness_eval_results SET result_json = ? "
            "WHERE batch_id = 'current-001' AND sample_index = 0",
            (json.dumps(payload),),
        )
        db.commit()

    assert not await service.validate_candidate_sources(candidate.draft)
    with pytest.raises(HarnessStoreError, match="损坏|不一致"):
        await service.discover(comparison_id=comparison_id)


@pytest.mark.asyncio
async def test_non_regressed_h5c_is_not_promoted_to_candidate(tmp_path: Path) -> None:
    harness, comparison_id = await _comparison(
        tmp_path,
        current_values=(8, 9, 10, 11, 12),
    )
    service = EvolutionEvalMetricOpportunityService(
        workspace_root=tmp_path / "workspace",
        harness_store=harness,
        candidate_store=EvolutionCandidateStore(tmp_path / "evolution.db"),
    )

    with pytest.raises(EvolutionEvalMetricOpportunityError) as rejected:
        await service.discover(comparison_id=comparison_id)

    assert rejected.value.code == "eval_metric_comparison_not_regressed"


@pytest.mark.asyncio
async def test_pass_rate_only_regression_does_not_masquerade_as_typed_metric(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = HarnessStore(tmp_path / "harness.db")
    baseline_result = _status_suite(
        workspace=workspace,
        commit="1",
        statuses=(EvalCaseStatus.PASSED, EvalCaseStatus.PASSED),
    )
    current_result = _status_suite(
        workspace=workspace,
        commit="2",
        statuses=(EvalCaseStatus.PASSED, EvalCaseStatus.IMPLEMENTATION_FAILURE),
    )
    for index in range(5):
        await harness.record_eval_result(
            workspace_root=workspace,
            batch_id="baseline-status",
            sample_index=index,
            result=baseline_result,
            created_at=_NOW,
        )
    baseline = await harness.promote_eval_baseline(
        workspace_root=workspace,
        batch_id="baseline-status",
        suite_id="runtime-budget",
        promoted_by="Harness-Test",
        promotion_reason="稳定状态基线",
        created_at=_NOW,
    )
    for index in range(5):
        await harness.record_eval_result(
            workspace_root=workspace,
            batch_id="current-status",
            sample_index=index,
            result=current_result,
            created_at=_NOW,
        )
    baseline_rows = await harness.list_eval_results(
        workspace, "baseline-status", "runtime-budget"
    )
    current_rows = await harness.list_eval_results(
        workspace, "current-status", "runtime-budget"
    )
    receipt = build_eval_comparison_receipt(
        workspace_root=workspace,
        suite_id="runtime-budget",
        baseline_id=baseline.id,
        baseline_batch_id=baseline.batch_id,
        baseline_samples_sha256=baseline.samples_sha256,
        baseline_samples=tuple(
            EvalReceiptSample(
                sample_index=item.sample_index,
                result_sha256=item.result_sha256,
                result=item.result,
            )
            for item in baseline_rows
        ),
        current_batch_id="current-status",
        current_samples=tuple(
            EvalReceiptSample(
                sample_index=item.sample_index,
                result_sha256=item.result_sha256,
                result=item.result,
            )
            for item in current_rows
        ),
        created_at=_NOW,
    )
    await harness.record_eval_comparison_receipt(receipt)
    service = EvolutionEvalMetricOpportunityService(
        workspace_root=workspace,
        harness_store=harness,
        candidate_store=EvolutionCandidateStore(tmp_path / "evolution.db"),
    )

    with pytest.raises(EvolutionEvalMetricOpportunityError) as rejected:
        await service.discover(comparison_id=receipt.id)

    assert rejected.value.code == "eval_metric_regression_absent"


@pytest.mark.parametrize(
    ("metric", "unit", "direction", "target", "baseline", "current", "finding"),
    [
        (
            "runtime.request.cost_usd",
            "usd",
            "decrease",
            1.0,
            (0.10, 0.11, 0.12, 0.13, 0.14),
            (0.40, 0.41, 0.42, 0.43, 0.44),
            "eval_cost_regression",
        ),
        (
            "runtime.request.tokens",
            "tokens",
            "decrease",
            100,
            (10, 11, 12, 13, 14),
            (30, 31, 32, 33, 34),
            "eval_token_regression",
        ),
        (
            "runtime.quality.score",
            "scalar",
            "increase",
            50,
            (90, 91, 92, 93, 94),
            (70, 71, 72, 73, 74),
            "eval_metric_regression",
        ),
    ],
)
@pytest.mark.asyncio
async def test_unit_and_direction_select_the_correct_finding(
    tmp_path: Path,
    metric: str,
    unit: str,
    direction: str,
    target: float,
    baseline: tuple[float, ...],
    current: tuple[float, ...],
    finding: str,
) -> None:
    harness, comparison_id = await _comparison(
        tmp_path,
        baseline_values=baseline,
        current_values=current,
        metric=metric,
        unit=unit,
        direction=direction,
        target=target,
    )
    result = await EvolutionEvalMetricOpportunityService(
        workspace_root=tmp_path / "workspace",
        harness_store=harness,
        candidate_store=EvolutionCandidateStore(tmp_path / "evolution.db"),
    ).discover(comparison_id=comparison_id)

    assert result.candidates[0].finding_code == finding
    assert result.candidates[0].metric.name == metric
    assert result.candidates[0].metric.direction == direction


def test_quantitative_metric_rejects_non_regression() -> None:
    with pytest.raises(ValidationError, match="置信区间确认的回归"):
        EvolutionQuantitativeMetric(
            name=_METRIC,
            case_fingerprint="a" * 64,
            unit="milliseconds",
            direction="decrease",
            target=100,
            baseline_mean=10,
            current_mean=11,
            delta=1,
            confidence_low=-1,
            confidence_high=3,
        )
    with pytest.raises(ValidationError, match="finite number"):
        EvolutionQuantitativeMetric(
            name=_METRIC,
            case_fingerprint="a" * 64,
            unit="milliseconds",
            direction="decrease",
            target=float("nan"),
            baseline_mean=10,
            current_mean=20,
            delta=10,
            confidence_low=5,
            confidence_high=15,
        )


@pytest.mark.asyncio
async def test_agent_tool_and_slash_share_the_registered_discovery_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, comparison_id = await _comparison(tmp_path)
    service = EvolutionEvalMetricOpportunityService(
        workspace_root=tmp_path / "workspace",
        harness_store=harness,
        candidate_store=EvolutionCandidateStore(tmp_path / "evolution.db"),
    )
    tool = EvolutionEvalMetricOpportunityTool(SimpleNamespace(
        evolution_eval_metric_opportunity_service=service,
    ))

    rendered = await tool.execute(comparison_id)

    assert tool.parameters_schema["properties"]["comparison_id"]["pattern"] == (
        "^[0-9a-f]{64}$"
    )
    assert "H5c 定量回归已进入机会发现" in rendered

    from naumi_agent import main

    calls: list[dict[str, object]] = []

    async def fake_run_tool(engine, **kwargs):
        calls.append({"engine": engine, **kwargs})

    monkeypatch.setattr(main, "_run_tool_slash_command", fake_run_tool)
    engine = SimpleNamespace()
    await main._run_evolution_review(
        engine,
        f"discover-metric {comparison_id}",
    )

    assert calls[0]["engine"] is engine
    assert calls[0]["tool_name"] == "evolution_discover_eval_metric_opportunity"
    assert calls[0]["parse_args"]("") == {"comparison_id": comparison_id}
