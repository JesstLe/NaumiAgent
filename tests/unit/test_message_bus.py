"""AgentMessageBus tests."""

import asyncio
import time

import pytest

from naumi_agent.agents.message_bus import (
    AgentMessage,
    AgentMessageBus,
    MessagePriority,
)


@pytest.fixture
def bus() -> AgentMessageBus:
    return AgentMessageBus()


def _make_msg(
    sender: str = "agent_a",
    topic: str = "test",
    content: str = "hello",
    recipient: str | None = None,
    priority: MessagePriority = MessagePriority.NORMAL,
) -> AgentMessage:
    return AgentMessage(
        sender=sender,
        topic=topic,
        content=content,
        recipient=recipient,
        priority=priority,
    )


class TestAgentMessage:
    def test_broadcast_message(self) -> None:
        msg = _make_msg()
        assert msg.is_broadcast is True
        assert msg.recipient is None

    def test_direct_message(self) -> None:
        msg = _make_msg(recipient="agent_b")
        assert msg.is_broadcast is False
        assert msg.recipient == "agent_b"

    def test_metadata_default(self) -> None:
        msg = _make_msg()
        assert msg.metadata == {}

    def test_timestamp_auto(self) -> None:
        msg = _make_msg()
        assert msg.timestamp > 0


class TestPubSub:
    @pytest.mark.asyncio
    async def test_publish_delivers_to_subscribers(
        self, bus: AgentMessageBus,
    ) -> None:
        received: list[AgentMessage] = []

        async def handler(msg: AgentMessage) -> None:
            received.append(msg)

        bus.subscribe("analysis", handler)
        msg = _make_msg(topic="analysis", content="finding")
        count = await bus.publish(msg)

        assert count == 1
        assert len(received) == 1
        assert received[0].content == "finding"

    @pytest.mark.asyncio
    async def test_multiple_subscribers(
        self, bus: AgentMessageBus,
    ) -> None:
        received_a: list[AgentMessage] = []
        received_b: list[AgentMessage] = []

        bus.subscribe("event", lambda m: received_a.append(m))
        bus.subscribe("event", lambda m: received_b.append(m))

        await bus.publish(_make_msg(topic="event"))
        assert len(received_a) == 1
        assert len(received_b) == 1

    @pytest.mark.asyncio
    async def test_no_subscribers(
        self, bus: AgentMessageBus,
    ) -> None:
        count = await bus.publish(_make_msg(topic="orphan"))
        assert count == 0

    @pytest.mark.asyncio
    async def test_unsubscribe(
        self, bus: AgentMessageBus,
    ) -> None:
        received: list[AgentMessage] = []

        unsub = bus.subscribe("temp", lambda m: received.append(m))
        await bus.publish(_make_msg(topic="temp"))
        assert len(received) == 1

        unsub()
        await bus.publish(_make_msg(topic="temp"))
        assert len(received) == 1  # no new delivery

    @pytest.mark.asyncio
    async def test_handler_error_doesnt_break_others(
        self, bus: AgentMessageBus,
    ) -> None:
        received: list[AgentMessage] = []

        async def bad_handler(msg: AgentMessage) -> None:
            raise RuntimeError("boom")

        bus.subscribe("resilient", bad_handler)
        bus.subscribe("resilient", lambda m: received.append(m))

        await bus.publish(_make_msg(topic="resilient"))
        assert len(received) == 1


class TestDirectMessaging:
    @pytest.mark.asyncio
    async def test_send_and_receive(
        self, bus: AgentMessageBus,
    ) -> None:
        msg = _make_msg(
            sender="alice", recipient="bob",
            topic="dm", content="hello bob",
        )
        await bus.send(msg)

        messages = await bus.receive("bob")
        assert len(messages) == 1
        assert messages[0].content == "hello bob"
        assert messages[0].sender == "alice"

    @pytest.mark.asyncio
    async def test_receive_clears_mailbox(
        self, bus: AgentMessageBus,
    ) -> None:
        await bus.send(_make_msg(recipient="agent", topic="t1"))
        await bus.send(_make_msg(recipient="agent", topic="t2"))

        first = await bus.receive("agent")
        assert len(first) == 2

        second = await bus.receive("agent")
        assert len(second) == 0

    @pytest.mark.asyncio
    async def test_receive_with_limit(
        self, bus: AgentMessageBus,
    ) -> None:
        for i in range(5):
            await bus.send(
                _make_msg(recipient="agent", topic="t", content=str(i)),
            )

        first = await bus.receive("agent", limit=2)
        assert len(first) == 2
        assert first[0].content == "0"
        assert first[1].content == "1"

        remaining = await bus.receive("agent")
        assert len(remaining) == 3

    @pytest.mark.asyncio
    async def test_peek_doesnt_consume(
        self, bus: AgentMessageBus,
    ) -> None:
        await bus.send(_make_msg(recipient="agent", topic="t"))

        peeked = await bus.peek("agent")
        assert len(peeked) == 1

        peeked_again = await bus.peek("agent")
        assert len(peeked_again) == 1

        consumed = await bus.receive("agent")
        assert len(consumed) == 1

    @pytest.mark.asyncio
    async def test_send_requires_recipient(
        self, bus: AgentMessageBus,
    ) -> None:
        with pytest.raises(ValueError, match="recipient"):
            await bus.send(_make_msg())  # no recipient

    @pytest.mark.asyncio
    async def test_receive_empty_mailbox(
        self, bus: AgentMessageBus,
    ) -> None:
        messages = await bus.receive("nobody")
        assert messages == []

    @pytest.mark.asyncio
    async def test_send_also_delivers_to_topic_subscribers(
        self, bus: AgentMessageBus,
    ) -> None:
        received: list[AgentMessage] = []
        bus.subscribe("dm", lambda m: received.append(m))

        await bus.send(_make_msg(
            recipient="bob", topic="dm", content="hi",
        ))
        assert len(received) == 1


class TestBlackboard:
    @pytest.mark.asyncio
    async def test_set_and_get(
        self, bus: AgentMessageBus,
    ) -> None:
        entry = await bus.blackboard_set("key1", "value1", "author_a")
        assert entry.key == "key1"
        assert entry.value == "value1"
        assert entry.author == "author_a"
        assert entry.version == 1

        retrieved = await bus.blackboard_get("key1")
        assert retrieved is not None
        assert retrieved.value == "value1"

    @pytest.mark.asyncio
    async def test_version_increments(
        self, bus: AgentMessageBus,
    ) -> None:
        await bus.blackboard_set("key", "v1", "a")
        entry = await bus.blackboard_set("key", "v2", "b")
        assert entry.version == 2
        assert entry.value == "v2"
        assert entry.author == "b"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(
        self, bus: AgentMessageBus,
    ) -> None:
        result = await bus.blackboard_get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_all(
        self, bus: AgentMessageBus,
    ) -> None:
        await bus.blackboard_set("k1", "v1", "a")
        await bus.blackboard_set("k2", "v2", "b")

        all_entries = await bus.blackboard_get_all()
        assert len(all_entries) == 2
        assert "k1" in all_entries
        assert "k2" in all_entries

    @pytest.mark.asyncio
    async def test_updates_since(
        self, bus: AgentMessageBus,
    ) -> None:
        await asyncio.sleep(0.01)
        await bus.blackboard_set("early", "v1", "a")
        mid = time.time()
        await asyncio.sleep(0.01)
        await bus.blackboard_set("late", "v2", "b")

        updates = await bus.blackboard_updates_since(mid)
        assert len(updates) == 1
        assert updates[0].key == "late"

    @pytest.mark.asyncio
    async def test_delete(
        self, bus: AgentMessageBus,
    ) -> None:
        await bus.blackboard_set("temp", "val", "a")
        assert await bus.blackboard_delete("temp") is True
        assert await bus.blackboard_get("temp") is None
        assert await bus.blackboard_delete("temp") is False

    @pytest.mark.asyncio
    async def test_complex_value(
        self, bus: AgentMessageBus,
    ) -> None:
        data = {"findings": ["x", "y"], "score": 0.85}
        await bus.blackboard_set("analysis", data, "expert")
        entry = await bus.blackboard_get("analysis")
        assert entry is not None
        assert entry.value["score"] == 0.85


class TestHistoryAndStats:
    @pytest.mark.asyncio
    async def test_history_records_publishes(
        self, bus: AgentMessageBus,
    ) -> None:
        await bus.publish(_make_msg(topic="t1"))
        await bus.publish(_make_msg(topic="t2"))
        await bus.send(_make_msg(recipient="bob", topic="t3"))

        history = bus.get_history()
        assert len(history) == 3

    @pytest.mark.asyncio
    async def test_history_filter_by_topic(
        self, bus: AgentMessageBus,
    ) -> None:
        await bus.publish(_make_msg(topic="alpha"))
        await bus.publish(_make_msg(topic="beta"))
        await bus.publish(_make_msg(topic="alpha"))

        alpha = bus.get_history(topic="alpha")
        assert len(alpha) == 2

    @pytest.mark.asyncio
    async def test_history_filter_by_sender(
        self, bus: AgentMessageBus,
    ) -> None:
        await bus.publish(_make_msg(sender="alice"))
        await bus.publish(_make_msg(sender="bob"))

        alice = bus.get_history(sender="alice")
        assert len(alice) == 1

    @pytest.mark.asyncio
    async def test_stats(
        self, bus: AgentMessageBus,
    ) -> None:
        await bus.publish(_make_msg())
        await bus.send(_make_msg(recipient="x", topic="dm"))
        await bus.blackboard_set("k", "v", "a")

        stats = bus.stats()
        assert stats["total_messages"] == 2
        assert stats["blackboard_entries"] == 1

    @pytest.mark.asyncio
    async def test_get_topics(
        self, bus: AgentMessageBus,
    ) -> None:
        bus.subscribe("topic_a", lambda m: None)
        bus.subscribe("topic_b", lambda m: None)

        topics = bus.get_topics()
        assert "topic_a" in topics
        assert "topic_b" in topics

    @pytest.mark.asyncio
    async def test_subscriber_count(
        self, bus: AgentMessageBus,
    ) -> None:
        bus.subscribe("test", lambda m: None)
        bus.subscribe("test", lambda m: None)
        assert bus.get_subscriber_count("test") == 2
        assert bus.get_subscriber_count("empty") == 0


class TestReset:
    @pytest.mark.asyncio
    async def test_reset_clears_all(
        self, bus: AgentMessageBus,
    ) -> None:
        await bus.publish(_make_msg())
        await bus.send(_make_msg(recipient="x", topic="dm"))
        await bus.blackboard_set("k", "v", "a")
        bus.subscribe("t", lambda m: None)

        await bus.reset()

        stats = bus.stats()
        assert stats["total_messages"] == 0
        assert stats["active_mailboxes"] == 0
        assert stats["pending_messages"] == 0
        assert stats["topics"] == 0
        assert stats["blackboard_entries"] == 0


class TestIntegrationWithSubAgentManager:
    def test_manager_has_message_bus(self) -> None:
        from naumi_agent.config.settings import AppConfig
        from naumi_agent.orchestrator.engine import AgentEngine
        from naumi_agent.orchestrator.subagent_manager import SubAgentManager

        engine = AgentEngine(AppConfig())
        manager = SubAgentManager(engine)
        assert manager.message_bus is not None

    @pytest.mark.asyncio
    async def test_delegate_auto_publishes_to_bus(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from naumi_agent.agents.base import AgentResult
        from naumi_agent.config.settings import AppConfig
        from naumi_agent.orchestrator.engine import AgentEngine
        from naumi_agent.orchestrator.subagent_manager import (
            SubAgentManager,
            SubTask,
        )

        engine = AgentEngine(AppConfig())
        manager = SubAgentManager(engine)

        # Mock the agent's execute to return a completed result
        mock_agent = MagicMock()
        mock_agent.execute = AsyncMock(return_value=AgentResult(
            status="completed",
            response="analysis complete",
            total_tokens=100,
            total_cost_usd=0.01,
            turns=1,
        ))

        # Inject mock agent
        manager._agents["test_expert"] = mock_agent

        task = SubTask(
            id="test_task",
            description="analyze this",
            agent_name="test_expert",
        )
        result = await manager.delegate(task)
        assert result.status == "completed"

        # Verify bus received the completion message
        history = manager.message_bus.get_history(
            topic="task.test_task.completed",
        )
        assert len(history) == 1
        assert history[0].content == "analysis complete"

    @pytest.mark.asyncio
    async def test_delegate_injects_blackboard_into_context(self) -> None:
        from unittest.mock import MagicMock

        from naumi_agent.agents.base import AgentResult
        from naumi_agent.config.settings import AppConfig
        from naumi_agent.orchestrator.engine import AgentEngine
        from naumi_agent.orchestrator.subagent_manager import (
            SubAgentManager,
            SubTask,
        )

        engine = AgentEngine(AppConfig())
        manager = SubAgentManager(engine)

        # Write to blackboard
        await manager.message_bus.blackboard_set(
            "shared_key", "shared_value", "expert_1",
        )

        # Mock agent to capture context
        captured_context: list[str] = []
        mock_agent = MagicMock()

        async def mock_execute(
            task: str, context: str = "",
        ) -> AgentResult:
            captured_context.append(context)
            return AgentResult(
                status="completed", response="done",
                total_tokens=10, total_cost_usd=0.001, turns=1,
            )

        mock_agent.execute = mock_execute
        manager._agents["reader"] = mock_agent

        task = SubTask(
            id="read_task",
            description="read data",
            agent_name="reader",
        )
        await manager.delegate(task)

        # Context should contain blackboard data
        assert len(captured_context) == 1
        assert "shared_key" in captured_context[0]
        assert "shared_value" in captured_context[0]
