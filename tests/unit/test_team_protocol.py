"""Team protocol tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from naumi_agent.agents.team_commands import run_team_command
from naumi_agent.agents.team_protocol import execute_team_signal, execute_team_status
from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType
from naumi_agent.streaming.publisher import RuntimeEventPublisher
from naumi_agent.tools.base import ToolCall


@pytest.fixture
def engine(tmp_path, request) -> AgentEngine:
    from naumi_agent.config.settings import MemoryConfig

    instance = AgentEngine(AppConfig(
        memory=MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
    ))

    def cleanup() -> None:
        asyncio.run(instance.shutdown())

    request.addfinalizer(cleanup)
    return instance


class TestTeamProtocol:
    @pytest.mark.asyncio
    async def test_team_signal_enters_parent_publisher_sequence(
        self,
        engine: AgentEngine,
    ) -> None:
        events: list[RuntimeEvent] = []

        class RecordingSink:
            async def emit(self, event: RuntimeEvent) -> None:
                events.append(event)

        publisher = RuntimeEventPublisher(
            RecordingSink(),
            session_id="session-team",
            run_id="run-team",
        )
        await execute_team_signal(
            engine.subagent_manager,
            event_type="handoff",
            sender="coder",
            recipient="researcher",
            content="接手验证。",
            event_callback=publisher.legacy_callback(),
        )

        assert [event.type for event in events] == [RuntimeEventType.TEAM_EVENT]
        assert events[0].sequence == 1
        assert events[0].run_id == "run-team"

    @pytest.mark.asyncio
    async def test_team_signal_rejects_unknown_protocol_event_before_callback(
        self,
        engine: AgentEngine,
    ) -> None:
        callback_called = False

        async def callback(_: str, __: dict[str, object]) -> None:
            nonlocal callback_called
            callback_called = True

        with pytest.raises(ValueError, match="无效团队事件类型"):
            await execute_team_signal(
                engine.subagent_manager,
                event_type="invented",
                sender="coder",
                content="不会发送。",
                event_callback=callback,
            )
        assert callback_called is False

    @pytest.mark.asyncio
    async def test_signal_delivers_direct_message_and_records_blackboard(
        self,
        engine: AgentEngine,
    ) -> None:
        manager = engine.subagent_manager

        result = await execute_team_signal(
            manager,
            event_type="handoff",
            sender="coder",
            recipient="researcher",
            content="请接手依赖风险确认。",
            priority="high",
            task_id="7",
        )

        pending = await manager.message_bus.peek("researcher")
        blackboard = await manager.message_bus.blackboard_get_all()

        assert result.recipient == "researcher"
        assert result.blackboard_key.startswith("team/handoff/coder/")
        assert pending[0].content == "请接手依赖风险确认。"
        assert pending[0].metadata["team_event_type"] == "handoff"
        assert blackboard[result.blackboard_key].value["task_id"] == "7"

    @pytest.mark.asyncio
    async def test_team_signal_tool_emits_visible_event(
        self,
        engine: AgentEngine,
    ) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        async def on_event(event: str, data: dict[str, object]) -> None:
            events.append((event, data))

        result = await engine._execute_tool(ToolCall(
            id="team-1",
            name="team_signal",
            arguments=json.dumps({
                "event_type": "blocker",
                "sender": "researcher",
                "content": "等待用户确认 API 范围。",
                "priority": "critical",
            }, ensure_ascii=False),
        ), on_event=on_event)

        assert result.status == "success"
        team_events = [data for event, data in events if event == "team_event"]
        assert team_events
        assert team_events[-1]["event_type"] == "blocker"
        assert team_events[-1]["priority"] == "critical"

    @pytest.mark.asyncio
    async def test_team_command_uses_same_protocol(
        self,
        engine: AgentEngine,
    ) -> None:
        output = await run_team_command(
            engine.subagent_manager,
            "request main_agent coder 补充边界测试",
        )
        status = await execute_team_status(engine.subagent_manager, agent="coder")

        assert "团队请求已发布" in output
        assert "补充边界测试" in status
        assert "main_agent → coder" in status
