from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.review import (
    EvolutionReviewFilter,
    EvolutionReviewService,
    render_evolution_review,
)
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import (
    FeedbackIntakeService,
    build_direct_user_feedback,
)
from naumi_agent.tools.evolution_review import EvolutionCandidatesTool

NOW = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)


async def _seed(
    root: Path,
    store: EvolutionCandidateStore,
) -> tuple[str, str]:
    intake = FeedbackIntakeService(store)
    first = await intake.ingest(
        root,
        build_direct_user_feedback(
            session_id="session-review",
            category="defect",
            scope="ui:footer",
            topic="truncation",
            summary="底栏内容被截断 secret-never-render",
            provider="openai",
            model="openai/kimi-for-coding",
            platform="darwin",
            now=NOW,
        ),
    )
    await intake.ingest(
        root,
        build_direct_user_feedback(
            session_id="session-review",
            category="defect",
            scope="ui:footer",
            topic="truncation",
            summary="底栏仍然被截断",
            provider="openai",
            model="openai/kimi-for-coding",
            platform="darwin",
            now=NOW + timedelta(minutes=1),
        ),
    )
    second = await intake.ingest(
        root,
        build_direct_user_feedback(
            session_id="session-review",
            category="correction",
            scope="ui:task_panel",
            topic="subagent_status",
            summary="子智能体状态不正确",
            provider="anthropic",
            model="anthropic/claude",
            platform="linux",
            now=NOW + timedelta(minutes=2),
        ),
    )
    return first.candidate_id, second.candidate_id


@pytest.mark.asyncio
async def test_review_list_empty_and_filtered_state(tmp_path: Path) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    service = EvolutionReviewService(store)
    empty = await service.list_snapshot(tmp_path)
    assert "没有 Candidate" in render_evolution_review(empty)

    footer_id, _task_id = await _seed(tmp_path, store)
    snapshot = await service.list_snapshot(
        tmp_path,
        filters=EvolutionReviewFilter(
            query="footer",
            risk="medium",
            source_kind="user_feedback",
            limit=1,
        ),
    )

    assert [item.candidate_id for item in snapshot.items] == [footer_id]
    item = snapshot.items[0]
    assert item.occurrence_count == 2
    assert item.providers == ("openai",)
    assert item.models == ("openai/kimi-for-coding",)
    assert item.platforms == ("darwin",)
    rendered = render_evolution_review(snapshot)
    assert "ui:footer" in rendered
    assert "secret-never-render" not in rendered


@pytest.mark.asyncio
async def test_review_detail_contains_verified_evidence_and_audit_chain(
    tmp_path: Path,
) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    footer_id, _task_id = await _seed(tmp_path, store)
    service = EvolutionReviewService(store)

    snapshot = await service.detail_snapshot(tmp_path, footer_id)
    rendered = render_evolution_review(snapshot)

    assert snapshot.selected is not None
    assert snapshot.selected.experiment_eligible is False
    assert snapshot.selected.revision == 2
    assert len(snapshot.events) == 2
    assert "feedback_recurrence" in rendered
    assert "artifact://feedback/" in rendered
    assert "r1 `created`" in rendered
    assert "r2 `evidence_merged`" in rendered
    assert "Eligibility、approve/reject/defer 尚未开放" in rendered
    assert "secret-never-render" not in rendered

    missing = await service.detail_snapshot(tmp_path, "evc_" + "0" * 24)
    assert "不存在" in render_evolution_review(missing)


def test_review_filter_rejects_unbounded_or_unknown_values() -> None:
    with pytest.raises(ValueError, match="risk"):
        EvolutionReviewFilter(risk="urgent")
    with pytest.raises(ValueError, match="source"):
        EvolutionReviewFilter(source_kind="model_claim")
    with pytest.raises(ValueError, match="1..100"):
        EvolutionReviewFilter(limit=101)
    with pytest.raises(ValueError, match="控制字符"):
        EvolutionReviewFilter(query="bad\nquery")
    with pytest.raises(ValueError, match="256"):
        EvolutionReviewFilter(query="x" * 257)


class _FakeEngine:
    def __init__(self, root: Path, service: EvolutionReviewService) -> None:
        self.workspace_root = root
        self.evolution_review_service = service
        self.router = SimpleNamespace(current_model="openai/test")


@pytest.mark.asyncio
async def test_tool_and_slash_share_review_service(tmp_path: Path) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    footer_id, _task_id = await _seed(tmp_path, store)
    service = EvolutionReviewService(store)
    engine = _FakeEngine(tmp_path, service)
    tool = EvolutionCandidatesTool(engine, service)

    tool_list = await tool.execute(action="list", query="footer")
    tool_detail = await tool.execute(action="detail", candidate_id=footer_id)
    slash_list = await execute_slash_command(
        engine,
        "/evolution list --source user_feedback --limit 10",
    )
    slash_detail = await execute_slash_command(
        engine,
        f"/evolution detail {footer_id}",
    )

    assert footer_id in tool_list
    assert footer_id in tool_detail
    assert footer_id in slash_list
    assert "审计链" in slash_detail
    assert "secret-never-render" not in "\n".join(
        (tool_list, tool_detail, slash_list, slash_detail)
    )


@pytest.mark.asyncio
async def test_tool_errors_are_safe_and_non_mutating(tmp_path: Path) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    service = EvolutionReviewService(store)
    tool = EvolutionCandidatesTool(_FakeEngine(tmp_path, service), service)

    assert "用法" in await tool.execute(action="detail")
    assert "仅支持" in await tool.execute(action="approve")
    assert "过滤条件无效" in await tool.execute(action="list", limit=0)
    assert not (tmp_path / "evolution.db").exists()
