from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.evolution.candidate import build_candidate_draft
from naumi_agent.evolution.evidence import EvolutionEvidence, EvolutionEvidenceRef
from naumi_agent.evolution.review import EvolutionReviewService
from naumi_agent.evolution.source_authority import (
    EvolutionCandidateSourceAuthorityRouter,
)
from naumi_agent.evolution.store import EvolutionCandidateStore


class _Reader:
    def __init__(self, result: object = True, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def validate_candidate_sources(self, candidate) -> object:
        self.calls += 1
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.result


class _BarrierReader(_Reader):
    def __init__(
        self,
        name: str,
        entered: set[str],
        both_entered: asyncio.Event,
    ) -> None:
        super().__init__()
        self.name = name
        self.entered = entered
        self.both_entered = both_entered

    async def validate_candidate_sources(self, candidate) -> bool:
        self.calls += 1
        self.entered.add(self.name)
        if len(self.entered) == 2:
            self.both_entered.set()
        await asyncio.wait_for(self.both_entered.wait(), timeout=1)
        return True


def _dynamic_candidate(*source_kinds: str):
    root = "1" * 64
    now = datetime.now(UTC)
    evidence = []
    for index, source_kind in enumerate(source_kinds):
        token = format(index + 1, "x") * 24
        if source_kind == "rollback_outcome":
            uri = f"evolution-outcome://rollback/evrerollbackout_{token}"
        elif source_kind == "promoted_outcome":
            uri = f"evolution-outcome://promoted/evstablepromout_{token}"
        else:
            raise AssertionError(source_kind)
        ref = EvolutionEvidenceRef(uri=uri, sha256=format(index + 2, "x") * 64)
        evidence.append(EvolutionEvidence(
            evidence_id=f"eve_{format(index + 3, 'x') * 24}",
            source_kind=source_kind,  # type: ignore[arg-type]
            source_uri=uri,
            observed_at=(now + timedelta(seconds=index)).isoformat(),
            finding_code="stable_promotion_improvement",
            scope="evolution:authority:test",
            root_fingerprint=root,
            refs=(ref,),
        ))
    return build_candidate_draft(evidence)


def _static_candidate():
    ref = EvolutionEvidenceRef(
        uri="artifact://self-review/example",
        sha256="9" * 64,
    )
    return build_candidate_draft((EvolutionEvidence(
        evidence_id=f"eve_{'8' * 24}",
        source_kind="self_review_static",
        source_uri=ref.uri,
        observed_at=datetime.now(UTC).isoformat(),
        finding_code="broad_except",
        scope="src/naumi_agent/example.py",
        root_fingerprint="7" * 64,
        refs=(ref,),
    ),))


def _router(
    rollback: object,
    promoted: object,
    metric: object | None = None,
    goal: object | None = None,
    catalog: object | None = None,
):
    return EvolutionCandidateSourceAuthorityRouter({
        "eval_metric_regression": metric or _Reader(),  # type: ignore[dict-item]
        "goal_need": goal or _Reader(),  # type: ignore[dict-item]
        "rollback_outcome": rollback,  # type: ignore[dict-item]
        "promoted_outcome": promoted,  # type: ignore[dict-item]
        "tool_catalog_miss": catalog or _Reader(),  # type: ignore[dict-item]
    })


@pytest.mark.asyncio
async def test_router_calls_each_distinct_reader_once_and_runs_them_concurrently() -> None:
    shared = _Reader()
    router = _router(shared, shared)
    candidate = _dynamic_candidate("rollback_outcome", "promoted_outcome")

    assert await router.validate_candidate_sources(candidate)
    assert shared.calls == 1
    assert router.source_kinds == (
        "eval_metric_regression",
        "goal_need",
        "promoted_outcome",
        "rollback_outcome",
        "tool_catalog_miss",
    )

    entered: set[str] = set()
    both_entered = asyncio.Event()
    left = _BarrierReader("left", entered, both_entered)
    right = _BarrierReader("right", entered, both_entered)
    split = _router(left, right)
    assert await split.validate_candidate_sources(candidate)
    assert left.calls == right.calls == 1


@pytest.mark.asyncio
async def test_router_fails_closed_for_false_non_bool_and_reader_errors() -> None:
    candidate = _dynamic_candidate("promoted_outcome")
    for reader in (
        _Reader(False),
        _Reader("true"),
        _Reader(error=OSError("unavailable")),
        _Reader(error=RuntimeError("stale")),
    ):
        router = _router(_Reader(), reader)
        assert not await router.validate_candidate_sources(candidate)


@pytest.mark.asyncio
async def test_static_candidate_needs_no_dynamic_reader_call() -> None:
    rollback = _Reader(error=AssertionError("must not be called"))
    promoted = _Reader(error=AssertionError("must not be called"))
    router = _router(rollback, promoted)

    assert await router.validate_candidate_sources(_static_candidate())
    assert rollback.calls == promoted.calls == 0


@pytest.mark.asyncio
async def test_review_without_router_blocks_dynamic_but_not_static_sources(
    tmp_path: Path,
) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    dynamic = await store.upsert_candidate(
        tmp_path,
        _dynamic_candidate("promoted_outcome"),
    )
    static = await store.upsert_candidate(tmp_path, _static_candidate())
    review = EvolutionReviewService(store)

    dynamic_view = await review.detail_snapshot(tmp_path, dynamic.draft.candidate_id)
    static_view = await review.detail_snapshot(tmp_path, static.draft.candidate_id)

    assert dynamic_view.selected is not None
    assert static_view.selected is not None
    dynamic_source = next(
        item
        for item in dynamic_view.selected.eligibility.checks
        if item.code == "source_authority"
    )
    static_source = next(
        item
        for item in static_view.selected.eligibility.checks
        if item.code == "source_authority"
    )
    assert not dynamic_source.passed and dynamic_source.hard_block
    assert static_source.passed


def test_router_rejects_missing_unknown_or_invalid_readers() -> None:
    reader = _Reader()
    with pytest.raises(ValueError, match="缺失"):
        EvolutionCandidateSourceAuthorityRouter({"rollback_outcome": reader})
    with pytest.raises(ValueError, match="未知"):
        EvolutionCandidateSourceAuthorityRouter({
            "eval_metric_regression": reader,
            "goal_need": reader,
            "rollback_outcome": reader,
            "promoted_outcome": reader,
            "tool_catalog_miss": reader,
            "future_claim": reader,
        })
    with pytest.raises(TypeError, match="validate_candidate_sources"):
        _router(reader, SimpleNamespace())


def test_review_reader_binding_cannot_be_silently_replaced(tmp_path) -> None:
    service = EvolutionReviewService(EvolutionCandidateStore(tmp_path / "evolution.db"))
    first = _router(_Reader(), _Reader())
    second = _router(_Reader(), _Reader())

    service.bind_source_authority_reader(first)
    service.bind_source_authority_reader(first)
    with pytest.raises(RuntimeError, match="组合器"):
        service.bind_source_authority_reader(second)
