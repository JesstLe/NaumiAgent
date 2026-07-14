"""Read-only Agent tool tests for H2 L2 repository knowledge."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from naumi_agent.harness.service import HarnessService, render_harness_knowledge
from naumi_agent.harness.tools import create_harness_tools
from naumi_agent.harness.trust import HarnessTrustStore

PROFILE = """\
schema_version: 1
knowledge:
  entrypoints: [AGENTS.md]
  include: [src/**/*.py]
"""


def _service(tmp_path: Path) -> HarnessService:
    workspace = tmp_path / "workspace"
    (workspace / ".naumi").mkdir(parents=True)
    (workspace / "src").mkdir()
    (workspace / ".naumi/harness.yaml").write_text(PROFILE, encoding="utf-8")
    (workspace / "AGENTS.md").write_text("ROOT_RULE", encoding="utf-8")
    (workspace / "src/engine.py").write_text(
        "class AgentEngineKnowledge: pass",
        encoding="utf-8",
    )
    return HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )


@pytest.mark.asyncio
async def test_knowledge_tool_schema_metadata_and_shared_service(tmp_path: Path) -> None:
    service = _service(tmp_path)
    tools = create_harness_tools(service)
    tool = next(item for item in tools if item.name == "harness_read_knowledge")

    assert [item.name for item in tools] == [
        "harness_status",
        "harness_doctor",
        "harness_explain",
        "harness_read_knowledge",
        "harness_run_check",
    ]
    assert tool.metadata.read_only
    assert tool.metadata.concurrency_safe
    assert tool.metadata.path_argument_names == ("path",)
    assert tool.parameters_schema["additionalProperties"] is False
    assert set(tool.parameters_schema["properties"]) == {
        "query",
        "path",
        "max_tokens",
    }
    assert tool.parameters_schema["properties"]["max_tokens"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 4_000,
        "default": 2_000,
    }

    untrusted = await tool.execute(path="AGENTS.md")
    await service.trust(source="test")
    direct_result = await service.read_knowledge(
        path="AGENTS.md",
        max_tokens=2_000,
    )
    trusted = await tool.execute(path="AGENTS.md")

    assert "尚未受信任" in untrusted
    assert trusted == render_harness_knowledge(direct_result)
    assert "ROOT_RULE" in trusted


@pytest.mark.asyncio
async def test_knowledge_tool_validates_query_path_and_budget(tmp_path: Path) -> None:
    service = _service(tmp_path)
    await service.trust(source="test")
    tool = next(
        item for item in create_harness_tools(service)
        if item.name == "harness_read_knowledge"
    )

    missing = await tool.execute()
    both = await tool.execute(query="AgentEngine", path="src/engine.py")
    unsafe = await tool.execute(path="../secret", max_tokens=100)
    small = await tool.execute(query="AgentEngine", max_tokens=0)
    large = await tool.execute(query="AgentEngine", max_tokens=4_001)

    assert all(
        "参数无效" in output
        for output in (missing, both, small, large)
    )
    assert "越过工作区边界" in unsafe


@pytest.mark.asyncio
async def test_knowledge_tool_is_safe_for_concurrent_queries(tmp_path: Path) -> None:
    service = _service(tmp_path)
    await service.trust(source="test")
    tool = next(
        item for item in create_harness_tools(service)
        if item.name == "harness_read_knowledge"
    )

    results = await asyncio.gather(*(
        tool.execute(query="AgentEngineKnowledge", max_tokens=100)
        for _ in range(30)
    ))

    assert len(set(results)) == 1
    assert "src/engine.py" in results[0]
    assert "AgentEngineKnowledge" in results[0]
