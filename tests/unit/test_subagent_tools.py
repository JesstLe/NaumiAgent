"""Subagent tool boundary tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from naumi_agent.agents.message_bus import AgentMessageBus
from naumi_agent.tools.subagent import (
    MAX_AGENT_NAME_CHARS,
    MAX_BLACKBOARD_KEY_CHARS,
    MAX_BLACKBOARD_VALUE_CHARS,
    MAX_SUBAGENT_CONTEXT_CHARS,
    MAX_SUBAGENT_TASK_CHARS,
    MAX_TEAM_CONTENT_CHARS,
    MAX_TEAM_STATUS_LIMIT,
    BlackboardReadTool,
    BlackboardWriteTool,
    DelegateTaskTool,
    DestroyAgentTool,
    ListAgentsTool,
    SpawnAgentTool,
    TeamSignalTool,
    TeamStatusTool,
)


def _manager() -> SimpleNamespace:
    return SimpleNamespace(message_bus=AgentMessageBus())


class FakeSubagentManager:
    def __init__(self) -> None:
        self.message_bus = AgentMessageBus()
        self.agents: dict[str, SimpleNamespace] = {}
        self.delegated: list[Any] = []
        self.spawned: list[dict[str, str]] = []
        self.destroyed: list[str] = []

    async def delegate(
        self,
        subtask: Any,
        *,
        event_callback: Any = None,
    ) -> SimpleNamespace:
        self.delegated.append(subtask)
        return SimpleNamespace(status="completed", response="delegate ok", error="")

    def get_agent(self, name: str) -> SimpleNamespace | None:
        return self.agents.get(name)

    async def spawn_for_task_with_llm(
        self,
        *,
        name: str,
        task_description: str,
        role: str,
        focus: str,
    ) -> SimpleNamespace:
        self.spawned.append(
            {
                "name": name,
                "task_description": task_description,
                "role": role,
                "focus": focus,
            }
        )
        agent = SimpleNamespace(
            config=SimpleNamespace(
                name=name,
                description=task_description,
                capabilities=[SimpleNamespace(value="coding")],
            )
        )
        self.agents[name] = agent
        return agent

    def destroy(self, name: str) -> bool:
        if name == "coder":
            return False
        if name not in self.agents:
            return False
        self.destroyed.append(name)
        del self.agents[name]
        return True

    def list_agents(self) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "state": "idle",
                "tasks": "0",
                "age_s": "1.0",
                "idle_s": "1.0",
                "description": agent.config.description,
            }
            for name, agent in self.agents.items()
        ]


class TestDelegateTaskTool:
    def test_metadata_marks_delegate_as_state_change(self) -> None:
        metadata = DelegateTaskTool(FakeSubagentManager()).metadata
        assert metadata.read_only is False
        assert metadata.requires_confirmation is False
        assert metadata.user_facing_name == "委派子任务"

    @pytest.mark.asyncio
    async def test_delegates_normalized_task_to_agent(self) -> None:
        manager = FakeSubagentManager()

        result = await DelegateTaskTool(manager).execute(
            task=" inspect logs ",
            agent=" coder ",
            success_criteria=" return root cause ",
            context=" recent failure ",
        )

        assert result == "delegate ok"
        subtask = manager.delegated[0]
        assert subtask.agent_name == "coder"
        assert subtask.description == "inspect logs\n\n成功标准：return root cause"
        assert subtask.context == "recent failure"

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"task": ""}, "task 不能为空"),
            ({"task": 123}, "task 必须是字符串"),
            ({"task": "x" * (MAX_SUBAGENT_TASK_CHARS + 1)}, "task 过长"),
            ({"task": "ok", "agent": "../bad"}, "agent 只能使用字母开头"),
            (
                {"task": "ok", "agent": "a" * (MAX_AGENT_NAME_CHARS + 1)},
                "agent 过长",
            ),
            (
                {"task": "ok", "context": "x" * (MAX_SUBAGENT_CONTEXT_CHARS + 1)},
                "context 过长",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_rejects_invalid_delegate_inputs(
        self,
        kwargs: dict[str, Any],
        expected: str,
    ) -> None:
        manager = FakeSubagentManager()

        result = await DelegateTaskTool(manager).execute(**kwargs)

        assert "已拒绝" in result
        assert expected in result
        assert manager.delegated == []


class TestSpawnAgentTool:
    def test_metadata_marks_spawn_as_state_change(self) -> None:
        metadata = SpawnAgentTool(FakeSubagentManager()).metadata
        assert metadata.read_only is False
        assert metadata.requires_confirmation is False
        assert metadata.user_facing_name == "创建子 Agent"

    @pytest.mark.asyncio
    async def test_spawns_agent_with_normalized_inputs(self) -> None:
        manager = FakeSubagentManager()

        result = await SpawnAgentTool(manager).execute(
            name=" security_auditor ",
            task_description=" review auth flows ",
            role=" coder ",
            focus=" security ",
        )

        assert "已创建子 Agent 'security_auditor'" in result
        assert manager.spawned == [
            {
                "name": "security_auditor",
                "task_description": "review auth flows",
                "role": "coder",
                "focus": "security",
            }
        ]

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"name": "", "task_description": "task"}, "name 不能为空"),
            ({"name": "bad/name", "task_description": "task"}, "name 只能使用字母开头"),
            ({"name": "agent1", "task_description": ""}, "task_description 不能为空"),
            (
                {"name": "agent1", "task_description": "x" * (MAX_SUBAGENT_TASK_CHARS + 1)},
                "task_description 过长",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_rejects_invalid_spawn_inputs(
        self,
        kwargs: dict[str, Any],
        expected: str,
    ) -> None:
        manager = FakeSubagentManager()

        result = await SpawnAgentTool(manager).execute(**kwargs)

        assert "已拒绝" in result
        assert expected in result
        assert manager.spawned == []


class TestDestroyAgentTool:
    def test_metadata_marks_destroy_as_destructive_and_confirmed(self) -> None:
        metadata = DestroyAgentTool(FakeSubagentManager()).metadata
        assert metadata.destructive is True
        assert metadata.requires_confirmation is True
        assert metadata.user_facing_name == "销毁子 Agent"

    @pytest.mark.asyncio
    async def test_destroys_existing_agent_with_normalized_name(self) -> None:
        manager = FakeSubagentManager()
        await SpawnAgentTool(manager).execute(
            name="reviewer",
            task_description="review code",
        )

        result = await DestroyAgentTool(manager).execute(name=" reviewer ")

        assert "已销毁子 Agent 'reviewer'" in result
        assert manager.destroyed == ["reviewer"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_destroy_name(self) -> None:
        manager = FakeSubagentManager()

        result = await DestroyAgentTool(manager).execute(name="../bad")

        assert "已拒绝" in result
        assert "name 只能使用字母开头" in result
        assert manager.destroyed == []


class TestListAgentsTool:
    def test_metadata_marks_list_as_read_only(self) -> None:
        metadata = ListAgentsTool(FakeSubagentManager()).metadata
        assert metadata.read_only is True
        assert metadata.concurrency_safe is True
        assert metadata.user_facing_name == "列出子 Agent"

    @pytest.mark.asyncio
    async def test_lists_available_agents(self) -> None:
        manager = FakeSubagentManager()
        await SpawnAgentTool(manager).execute(
            name="reviewer",
            task_description="review code",
        )

        result = await ListAgentsTool(manager).execute()

        assert "可用 Agent:" in result
        assert "reviewer" in result
        assert "review code" in result


class TestBlackboardReadTool:
    def test_metadata_marks_blackboard_read_as_read_only(self) -> None:
        metadata = BlackboardReadTool(_manager()).metadata
        assert metadata.read_only is True
        assert metadata.concurrency_safe is True
        assert metadata.user_facing_name == "读取共享状态板"

    @pytest.mark.asyncio
    async def test_reads_existing_key_with_normalization(self) -> None:
        manager = _manager()
        await manager.message_bus.blackboard_set("plan/current", "ship it", "tester")

        result = await BlackboardReadTool(manager).execute(key=" plan/current ")

        assert "**plan/current**" in result
        assert "ship it" in result

    @pytest.mark.asyncio
    async def test_rejects_invalid_read_key(self) -> None:
        result = await BlackboardReadTool(_manager()).execute(key="../secret")

        assert "已拒绝" in result
        assert "路径越界" in result


class TestBlackboardWriteTool:
    def test_metadata_marks_blackboard_write_as_state_change(self) -> None:
        metadata = BlackboardWriteTool(_manager()).metadata
        assert metadata.read_only is False
        assert metadata.requires_confirmation is False
        assert metadata.user_facing_name == "写入共享状态板"

    @pytest.mark.asyncio
    async def test_writes_normalized_key_and_value(self) -> None:
        manager = _manager()

        result = await BlackboardWriteTool(manager).execute(
            key=" plan/current ",
            value=" ready ",
        )

        entry = await manager.message_bus.blackboard_get("plan/current")
        assert "已写入共享状态" in result
        assert entry is not None
        assert entry.value == "ready"

    @pytest.mark.parametrize(
        ("key", "value", "expected"),
        [
            ("", "value", "key 不能为空"),
            (123, "value", "key 必须是字符串"),
            ("x" * (MAX_BLACKBOARD_KEY_CHARS + 1), "value", "key 过长"),
            ("../secret", "value", "路径越界"),
            ("valid", "", "value 不能为空"),
            ("valid", 123, "value 不能为空"),
            ("valid", "x" * (MAX_BLACKBOARD_VALUE_CHARS + 1), "value 过长"),
        ],
    )
    @pytest.mark.asyncio
    async def test_rejects_invalid_write_inputs(
        self,
        key: Any,
        value: Any,
        expected: str,
    ) -> None:
        manager = _manager()

        result = await BlackboardWriteTool(manager).execute(key=key, value=value)

        assert "已拒绝" in result
        assert expected in result
        assert await manager.message_bus.blackboard_get_all() == {}


class TestTeamSignalTool:
    def test_metadata_marks_team_signal_as_state_change(self) -> None:
        metadata = TeamSignalTool(_manager()).metadata
        assert metadata.read_only is False
        assert metadata.requires_confirmation is False
        assert metadata.user_facing_name == "发布团队信号"

    @pytest.mark.asyncio
    async def test_sends_normalized_team_signal(self) -> None:
        manager = _manager()

        result = await TeamSignalTool(manager).execute(
            event_type=" update ",
            sender=" main_agent ",
            content=" shipped ",
            recipient="",
            record_to_blackboard=True,
        )

        assert "团队进展已发布" in result
        assert "shipped" in result
        blackboard = await manager.message_bus.blackboard_get_all()
        assert len(blackboard) == 1

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"content": ""}, "content 不能为空"),
            (
                {"content": "x" * (MAX_TEAM_CONTENT_CHARS + 1)},
                "content 过长",
            ),
            ({"sender": 123}, "sender 必须是字符串"),
            ({"blackboard_key": "../secret"}, "路径越界"),
            ({"record_to_blackboard": "yes"}, "record_to_blackboard 必须是布尔值"),
            ({"event_type": "unknown"}, "无效团队事件类型"),
        ],
    )
    @pytest.mark.asyncio
    async def test_rejects_invalid_team_signal_inputs(
        self,
        kwargs: dict[str, Any],
        expected: str,
    ) -> None:
        base = {
            "event_type": "update",
            "sender": "main_agent",
            "content": "progress",
        }
        base.update(kwargs)

        result = await TeamSignalTool(_manager()).execute(**base)

        assert "已拒绝" in result
        assert expected in result


class TestTeamStatusTool:
    def test_metadata_marks_team_status_as_read_only(self) -> None:
        metadata = TeamStatusTool(_manager()).metadata
        assert metadata.read_only is True
        assert metadata.concurrency_safe is True
        assert metadata.user_facing_name == "团队状态"

    @pytest.mark.asyncio
    async def test_reads_team_status_with_normalized_agent(self) -> None:
        manager = _manager()
        await TeamSignalTool(manager).execute(
            event_type="request",
            sender="main_agent",
            content="please review",
            recipient="coder",
            record_to_blackboard=False,
        )

        result = await TeamStatusTool(manager).execute(agent=" coder ", limit=5)

        assert "团队协议状态" in result
        assert "coder 待处理消息" in result

    @pytest.mark.parametrize(
        ("agent", "limit", "expected"),
        [
            (123, 10, "agent 必须是字符串"),
            ("coder", 0, "limit 必须在"),
            ("coder", MAX_TEAM_STATUS_LIMIT + 1, "limit 必须在"),
            ("coder", True, "limit 必须是整数"),
        ],
    )
    @pytest.mark.asyncio
    async def test_rejects_invalid_team_status_inputs(
        self,
        agent: Any,
        limit: Any,
        expected: str,
    ) -> None:
        result = await TeamStatusTool(_manager()).execute(agent=agent, limit=limit)

        assert "已拒绝" in result
        assert expected in result
