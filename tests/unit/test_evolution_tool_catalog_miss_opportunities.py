from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from naumi_agent.evolution.eligibility import (
    CandidateGovernanceContext,
    assess_candidate_eligibility,
)
from naumi_agent.evolution.proposal import generate_proposal_preview
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.evolution.tool_catalog_miss_opportunities import (
    EvolutionToolCatalogMissOpportunityError,
    EvolutionToolCatalogMissOpportunityService,
    ToolCatalogMissStore,
    tool_catalog_sha256,
)
from naumi_agent.evolution.validation_cohorts import BaselineCohortMetricCase
from naumi_agent.evolution.validation_metric_bindings import (
    EvolutionMetricRunnerRegistry,
)
from naumi_agent.tools.base import Tool, ToolMetadata, ToolRegistry
from naumi_agent.tools.builtin import create_builtin_tools
from naumi_agent.tools.evolution_review import (
    EvolutionToolCatalogMissOpportunityTool,
)
from naumi_agent.tools.search import ToolSearchTool


class _NamedTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "测试工具。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(read_only=True, concurrency_safe=True)

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def _runtime(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "state" / "evolution.db"
    misses = ToolCatalogMissStore(db_path)
    candidates = EvolutionCandidateStore(db_path)
    registry = ToolRegistry()
    for tool in create_builtin_tools():
        registry.register(tool)
    search = ToolSearchTool(
        registry,
        miss_store=misses,
        workspace_root=workspace,
    )
    registry.register(search)
    service = EvolutionToolCatalogMissOpportunityService(
        workspace_root=workspace,
        miss_store=misses,
        tool_catalog=registry,
        candidate_store=candidates,
    )
    return workspace, misses, candidates, registry, search, service


def _miss_id(output: str) -> str:
    match = re.search(r"`(tsm_[0-9a-f]{24})`", output)
    assert match is not None
    return match.group(1)


@pytest.mark.asyncio
async def test_exact_search_miss_is_durable_private_and_idempotent(
    tmp_path: Path,
) -> None:
    workspace, misses, candidates, registry, search, service = _runtime(tmp_path)

    outputs = await asyncio.gather(
        *(search.execute(query="select:browser_trace_compare") for _ in range(8))
    )
    miss_ids = {_miss_id(output) for output in outputs}
    assert len(miss_ids) == 1
    miss_id = miss_ids.pop()
    stored_miss = await misses.get(workspace, miss_id)
    assert stored_miss is not None
    assert stored_miss.record.requested_name == "browser_trace_compare"
    assert stored_miss.record.catalog_sha256 == tool_catalog_sha256(registry.names)

    results = await asyncio.gather(
        *(service.discover(miss_id=miss_id) for _ in range(8))
    )
    assert all(item == results[0] for item in results)
    result = results[0]
    assert result.candidate_revision == 1
    assert result.occurrence_count == 1
    candidate = await candidates.get_candidate(workspace, result.candidate_id)
    assert candidate is not None
    assert candidate.draft.kind == "capability"
    assert candidate.draft.finding_code == "missing_tool_capability"
    assert candidate.draft.source_kinds == ("tool_catalog_miss",)
    assert candidate.draft.scope == "capability:tool:browser_trace_compare"
    assert candidate.draft.expected_metrics[0].verifier == "tool_catalog_presence"
    assert await service.validate_candidate_sources(candidate.draft)
    encoded = candidate.draft.model_dump_json()
    assert "select:" not in encoded
    assert str(workspace) not in encoded

    governance = CandidateGovernanceContext(
        allowed=True,
        reason="no_active_cooldown",
    )
    assessment = assess_candidate_eligibility(
        candidate.draft,
        governance=governance,
    )
    assert assessment.review_ready
    assert not assessment.experiment_eligible
    preview = generate_proposal_preview(candidate, governance=governance)
    assert preview is not None
    assert preview.proposal_kind == "tool"
    assert preview.validation_plan[0].verifier == "tool_catalog_presence"
    assert not preview.executable


@pytest.mark.asyncio
async def test_natural_language_and_unsafe_select_misses_are_not_persisted(
    tmp_path: Path,
) -> None:
    workspace, misses, _, _, search, _ = _runtime(tmp_path)

    natural = await search.execute(query="能力不存在 SECRET-QUERY-42")
    unsafe = await search.execute(query="select:secret capability with spaces")

    assert "tsm_" not in natural
    assert "未持久化非安全工具标识符" in unsafe
    assert "tsm_" not in unsafe
    assert not misses.db_path.exists()
    assert await misses.get(workspace, "tsm_0123456789abcdef01234567") is None


def test_invalid_catalog_name_is_rejected_before_miss_state_creation(
    tmp_path: Path,
) -> None:
    _, misses, _, registry, _, _ = _runtime(tmp_path)

    with pytest.raises(ValueError, match="不含空白或控制字符") as invalid:
        registry.register(_NamedTool("invalid tool name"))

    assert "tsm_" not in str(invalid.value)
    assert "invalid tool name" not in registry.names
    assert not misses.db_path.exists()


@pytest.mark.asyncio
async def test_catalog_change_or_new_tool_revokes_miss_authority(tmp_path: Path) -> None:
    workspace, _, candidates, registry, search, service = _runtime(tmp_path)
    missing_name = "browser_trace_compare"
    miss_id = _miss_id(await search.execute(query=f"select:{missing_name}"))
    result = await service.discover(miss_id=miss_id)
    candidate = await candidates.get_candidate(workspace, result.candidate_id)
    assert candidate is not None

    registry.register(_NamedTool("unrelated_tool"))
    assert not await service.validate_candidate_sources(candidate.draft)
    with pytest.raises(EvolutionToolCatalogMissOpportunityError) as changed:
        await service.discover(miss_id=miss_id)
    assert changed.value.code == "tool_catalog_miss_catalog_changed"

    registry.register(_NamedTool(missing_name))
    with pytest.raises(EvolutionToolCatalogMissOpportunityError) as satisfied:
        await service.discover(miss_id=miss_id)
    assert satisfied.value.code == "tool_catalog_miss_satisfied"


@pytest.mark.asyncio
async def test_tamper_and_cross_workspace_reads_fail_closed(tmp_path: Path) -> None:
    workspace, misses, candidates, _, search, service = _runtime(tmp_path)
    miss_id = _miss_id(await search.execute(query="select:browser_trace_compare"))
    result = await service.discover(miss_id=miss_id)
    candidate = await candidates.get_candidate(workspace, result.candidate_id)
    assert candidate is not None

    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    assert await misses.get(other_workspace, miss_id) is None

    with sqlite3.connect(misses.db_path) as db:
        db.execute(
            """
            UPDATE evolution_tool_catalog_misses
            SET requested_name = ?
            WHERE workspace_root = ? AND miss_id = ?
            """,
            ("forged_tool", str(workspace), miss_id),
        )
        db.commit()

    assert not await service.validate_candidate_sources(candidate.draft)
    with pytest.raises(EvolutionToolCatalogMissOpportunityError) as corrupted:
        await service.discover(miss_id=miss_id)
    assert corrupted.value.code == "tool_catalog_miss_source_unavailable"


def test_tool_catalog_presence_verifier_is_blocked_without_acceptance_runner() -> None:
    resolution = EvolutionMetricRunnerRegistry().resolve(
        BaselineCohortMetricCase(
            order=1,
            metric_name="tool.catalog.requested_capability.availability",
            direction="increase",
            target=1,
            verifier="tool_catalog_presence",
            procedure_sha256="a" * 64,
        ),
        validation_paths=("src/naumi_agent/tools/example.py",),
    )

    assert resolution.status == "blocked"
    assert resolution.fixture_kind == "tool_catalog_authority"
    assert resolution.blocking_code == "tool_catalog_acceptance_runner_unavailable"
    assert resolution.runner_version is None


@pytest.mark.asyncio
async def test_agent_tool_and_slash_share_catalog_miss_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, _, search, service = _runtime(tmp_path)
    miss_id = _miss_id(await search.execute(query="select:browser_trace_compare"))
    tool = EvolutionToolCatalogMissOpportunityTool(SimpleNamespace(
        evolution_tool_catalog_miss_opportunity_service=service,
    ))

    rendered = await tool.execute(miss_id)

    assert tool.parameters_schema["properties"]["miss_id"]["pattern"] == (
        "^tsm_[0-9a-f]{24}$"
    )
    assert "Tool Catalog 缺失能力已进入机会发现" in rendered
    assert "自然语言查询" in rendered

    from naumi_agent import main

    calls: list[dict[str, object]] = []

    async def fake_run_tool(engine, **kwargs):
        calls.append({"engine": engine, **kwargs})

    monkeypatch.setattr(main, "_run_tool_slash_command", fake_run_tool)
    engine = SimpleNamespace()
    await main._run_evolution_review(engine, f"discover-miss {miss_id}")

    assert calls[0]["engine"] is engine
    assert calls[0]["tool_name"] == (
        "evolution_discover_tool_catalog_miss_opportunity"
    )
    assert calls[0]["parse_args"]("") == {"miss_id": miss_id}
