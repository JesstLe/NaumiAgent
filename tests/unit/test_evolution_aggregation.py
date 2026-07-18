from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.aggregation import aggregate_candidate
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import FeedbackIntakeService, build_direct_user_feedback

ANCHOR = datetime(2026, 7, 18, 20, 0, tzinfo=UTC)


async def _aggregate(
    root: Path,
    offsets: tuple[int, ...],
    *,
    dimensions: bool = True,
):
    root.mkdir(parents=True, exist_ok=True)
    store = EvolutionCandidateStore(root / "evolution.db")
    intake = FeedbackIntakeService(store)
    result = None
    for index, days in enumerate(offsets):
        result = await intake.ingest(
            root,
            build_direct_user_feedback(
                session_id="aggregation",
                category="defect",
                scope="ui:footer",
                topic="truncation",
                summary=f"底栏截断 {index}",
                provider=("openai" if index >= 2 else "anthropic") if dimensions else "",
                model=("model-a" if index % 2 == 0 else "model-b") if dimensions else "",
                platform=("darwin" if index >= 3 else "linux") if dimensions else "",
                now=ANCHOR + timedelta(days=days),
            ),
        )
    assert result is not None
    stored = await store.get_candidate(root, result.candidate_id)
    assert stored is not None
    return aggregate_candidate(stored.draft)


@pytest.mark.asyncio
async def test_aggregation_uses_candidate_anchor_and_detects_increasing_trend(
    tmp_path: Path,
) -> None:
    aggregation = await _aggregate(tmp_path, (-12, -10, -5, -4, -3, 0))

    assert aggregation.policy_version == "candidate-aggregation-v1"
    assert aggregation.anchor_at == ANCHOR.isoformat()
    assert aggregation.total_count == 6
    assert aggregation.count_24h == 1
    assert aggregation.count_7d == 4
    assert aggregation.count_30d == 6
    assert aggregation.previous_7d_count == 2
    assert aggregation.trend == "increasing"
    assert [(item.value, item.count) for item in aggregation.provider_counts] == [
        ("openai", 4),
        ("anthropic", 2),
    ]
    assert sum(item.percentage for item in aggregation.provider_counts) == 100.0
    assert {item.value for item in aggregation.platform_counts} == {"darwin", "linux"}
    assert len(aggregation.representatives) == 2


@pytest.mark.asyncio
async def test_aggregation_detects_decrease_and_preserves_unknown_dimensions(
    tmp_path: Path,
) -> None:
    decreasing = await _aggregate(tmp_path / "decreasing", (-13, -12, -11, -8, -6, 0))
    unknown = await _aggregate(tmp_path / "unknown", (0,), dimensions=False)

    assert decreasing.trend == "decreasing"
    assert decreasing.previous_7d_count == 4
    assert decreasing.count_7d == 2
    assert unknown.trend == "new"
    assert unknown.provider_counts[0].value == "unknown"
    assert unknown.provider_counts[0].percentage == 100.0
    assert len(unknown.representatives) == 1


@pytest.mark.asyncio
async def test_aggregation_bounds_dimensions_and_reports_omitted_unique_values(
    tmp_path: Path,
) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    intake = FeedbackIntakeService(store)
    result = None
    for index in range(25):
        result = await intake.ingest(
            tmp_path,
            build_direct_user_feedback(
                session_id="many-dimensions",
                category="defect",
                scope="ui:footer",
                topic="truncation",
                summary=f"底栏截断 {index}",
                provider=f"provider-{index:02d}",
                now=ANCHOR + timedelta(minutes=index),
            ),
        )
    assert result is not None
    stored = await store.get_candidate(tmp_path, result.candidate_id)
    assert stored is not None

    aggregation = aggregate_candidate(stored.draft)

    assert len(aggregation.provider_counts) == 20
    assert aggregation.provider_unique_count == 25
    assert all(item.percentage == 4.0 for item in aggregation.provider_counts)


def test_aggregation_rejects_non_candidate() -> None:
    with pytest.raises(TypeError, match="EvolutionCandidateDraft"):
        aggregate_candidate(object())  # type: ignore[arg-type]
