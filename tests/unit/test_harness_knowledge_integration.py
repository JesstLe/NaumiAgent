"""H2 trusted knowledge integration with the ephemeral Engine snapshot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.orchestrator.context_assembly import is_harness_context_message
from naumi_agent.orchestrator.engine import AgentEngine, AgentRuntimeMode
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType
from naumi_agent.streaming.publisher import RuntimeEventPublisher
from naumi_agent.tools.base import Tool, ToolCall, ToolMetadata

PROFILE = """\
schema_version: 1
knowledge:
  entrypoints: [AGENTS.md]
  include: [src/**/*.py, frontend/**/*.js]
  exclude: [data/**]
  max_turn_tokens: 8000
"""


class _CreateKnowledgeFileTool(Tool):
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "create_knowledge_fixture"

    @property
    def description(self) -> str:
        return "创建知识刷新测试文件"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(read_only=False)

    async def execute(self, **kwargs: Any) -> str:
        _write(
            self._workspace / "src/new_knowledge.py",
            "class NewKnowledgeSymbol: pass",
        )
        return "created"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
async def trusted_engine(tmp_path: Path) -> AgentEngine:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write(workspace / ".naumi/harness.yaml", PROFILE)
    _write(workspace / "AGENTS.md", "ROOT_TRUSTED_RULE")
    _write(
        workspace / "src/engine.py",
        "class AgentEngineKnowledge: pass",
    )
    _write(
        workspace / "frontend/state.js",
        "export const semanticStatusBar = true;",
    )
    config = AppConfig(
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            vector_db_path=str(tmp_path / "chroma"),
        ),
        workspace_root=str(workspace),
    )
    engine = AgentEngine(config)
    engine.harness_service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "harness-trust.db"),
    )
    await engine.harness_service.trust(source="test")
    try:
        yield engine
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_injects_trusted_knowledge_without_persisting(
    trusted_engine: AgentEngine,
) -> None:
    trusted_engine._messages = [
        {"role": "system", "content": "base"},
        {"role": "user", "content": "修改 AgentEngineKnowledge"},
    ]
    trusted_engine._full_history = list(trusted_engine._messages)
    events: list[RuntimeEvent] = []

    class RecordingSink:
        async def emit(self, event: RuntimeEvent) -> None:
            events.append(event)

    publisher = RuntimeEventPublisher(
        RecordingSink(),
        session_id="knowledge-session",
        run_id="knowledge-run",
    )

    await trusted_engine._inject_harness_context_snapshot(publisher)

    active = [
        message for message in trusted_engine._messages
        if is_harness_context_message(message)
    ]
    assert len(active) == 1
    assert "Repository Knowledge" in active[0]["content"]
    assert "ROOT_TRUSTED_RULE" in active[0]["content"]
    assert "src/engine.py" in active[0]["content"]
    assert [event.type for event in events] == [RuntimeEventType.HARNESS_KNOWLEDGE]
    assert events[0].data["status"] == "ready"
    assert events[0].run_id == "knowledge-run"
    assert not any(
        "Repository Knowledge" in str(message.get("content", ""))
        for message in trusted_engine._full_history
    )


@pytest.mark.asyncio
async def test_engine_uses_latest_user_task_and_replaces_previous_bundle(
    trusted_engine: AgentEngine,
) -> None:
    trusted_engine._messages = [
        {"role": "user", "content": "修改 AgentEngineKnowledge"},
        {"role": "assistant", "content": "上一轮"},
        {"role": "user", "content": "优化 semanticStatusBar"},
    ]

    await trusted_engine._inject_harness_context_snapshot()
    first = trusted_engine._messages[-1]["content"]
    await trusted_engine._inject_harness_context_snapshot()
    second = trusted_engine._messages[-1]["content"]

    assert "frontend/state.js" in first
    first_knowledge = first.split("## Repository Knowledge Manifest", 1)[1]
    second_knowledge = second.split("## Repository Knowledge Manifest", 1)[1]
    assert first_knowledge == second_knowledge
    assert len([
        message for message in trusted_engine._messages
        if is_harness_context_message(message)
    ]) == 1


@pytest.mark.asyncio
async def test_engine_refreshes_changed_knowledge_bytes(
    trusted_engine: AgentEngine,
) -> None:
    trusted_engine._messages = [
        {"role": "user", "content": "修改 AgentEngineKnowledge"},
    ]
    source = trusted_engine.workspace_root / "src/engine.py"

    await trusted_engine._inject_harness_context_snapshot()
    before = trusted_engine._messages[-1]["content"]
    source.write_text("class AgentEngineKnowledgeV2: pass", encoding="utf-8")
    await trusted_engine._inject_harness_context_snapshot()
    after = trusted_engine._messages[-1]["content"]

    assert "AgentEngineKnowledgeV2" in after
    assert before != after


@pytest.mark.asyncio
async def test_engine_does_not_inject_untrusted_repository_body(
    trusted_engine: AgentEngine,
) -> None:
    await trusted_engine.harness_service.untrust()
    trusted_engine._messages = [
        {"role": "user", "content": "读取 ROOT_TRUSTED_RULE"},
    ]

    await trusted_engine._inject_harness_context_snapshot()
    content = trusted_engine._messages[-1]["content"]

    assert "## Harness 状态快照" in content
    assert "Repository Knowledge" not in content
    assert "ROOT_TRUSTED_RULE" not in content


@pytest.mark.asyncio
async def test_successful_mutating_tool_invalidates_knowledge_for_new_files(
    trusted_engine: AgentEngine,
) -> None:
    await trusted_engine.harness_service.knowledge_context(
        "读取 AgentEngineKnowledge",
        model_window=124_000,
    )
    trusted_engine._tool_registry.register(
        _CreateKnowledgeFileTool(trusted_engine.workspace_root)
    )
    trusted_engine.set_runtime_mode(AgentRuntimeMode.BYPASS)
    events: list[RuntimeEvent] = []

    class RecordingSink:
        async def emit(self, event: RuntimeEvent) -> None:
            events.append(event)

    publisher = RuntimeEventPublisher(
        RecordingSink(),
        session_id="knowledge-session",
        run_id="knowledge-run",
    )

    result = await trusted_engine._execute_tool(ToolCall(
        id="create_knowledge",
        name="create_knowledge_fixture",
        arguments="{}",
    ), events=publisher)
    refreshed = await trusted_engine.harness_service.knowledge_context(
        "修改 NewKnowledgeSymbol",
        model_window=124_000,
    )

    assert result.status == "success"
    assert [event.type for event in events] == [
        RuntimeEventType.HARNESS_KNOWLEDGE_INVALIDATED,
    ]
    assert events[0].run_id == "knowledge-run"
    assert events[0].data["source"] == "create_knowledge_fixture"
    assert refreshed.bundle is not None
    assert "src/new_knowledge.py" in refreshed.bundle.source_paths
