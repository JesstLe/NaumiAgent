"""Tests for the structured task status renderer."""

from __future__ import annotations

from naumi_agent.ui.task_status_renderer import (
    AgentStatus,
    BackgroundTaskStatus,
    TaskPhase,
    TodoItem,
    render_agent_status,
    render_background_status,
    render_task_summary_bar,
    render_todo_bar,
    render_todo_detail_panel,
)


class TestTodoBar:
    def test_empty_items(self) -> None:
        assert render_todo_bar(()) == ""

    def test_single_completed(self) -> None:
        items = (TodoItem(text="Task A", status="completed", id="1"),)
        result = render_todo_bar(items)
        assert "1/1" in result
        assert "📋" in result

    def test_mixed_status(self) -> None:
        items = (
            TodoItem(text="Done", status="completed", id="1"),
            TodoItem(text="Active", status="in_progress", id="2"),
            TodoItem(text="Todo", status="pending", id="3"),
        )
        result = render_todo_bar(items)
        assert "1/3" in result
        assert "Active" in result

    def test_blocked_shown(self) -> None:
        items = (
            TodoItem(text="Blocked task", status="blocked", id="1"),
        )
        result = render_todo_bar(items)
        assert "blocked" in result

    def test_all_completed(self) -> None:
        items = (
            TodoItem(text="A", status="completed", id="1"),
            TodoItem(text="B", status="completed", id="2"),
        )
        result = render_todo_bar(items)
        assert "2/2" in result


class TestAgentStatus:
    def test_empty(self) -> None:
        assert render_agent_status(()) == ""

    def test_running_agent(self) -> None:
        agents = (
            AgentStatus(name="planner", phase=TaskPhase.RUNNING, description="analyzing"),
        )
        result = render_agent_status(agents)
        assert "planner" in result
        assert "analyzing" in result

    def test_failed_agent_with_error(self) -> None:
        agents = (
            AgentStatus(name="coder", phase=TaskPhase.FAILED, error="timeout"),
        )
        result = render_agent_status(agents)
        assert "coder" in result
        assert "timeout" in result

    def test_multiple_agents(self) -> None:
        agents = (
            AgentStatus(name="a1", phase=TaskPhase.RUNNING),
            AgentStatus(name="a2", phase=TaskPhase.COMPLETED),
        )
        result = render_agent_status(agents)
        assert "a1" in result
        assert "a2" in result
        # Two lines
        assert result.count("\n") == 2


class TestBackgroundStatus:
    def test_empty(self) -> None:
        assert render_background_status(()) == ""

    def test_running_task(self) -> None:
        tasks = (
            BackgroundTaskStatus(
                task_id="bg1",
                command="npm test",
                phase=TaskPhase.RUNNING,
                runtime_s=12.5,
            ),
        )
        result = render_background_status(tasks)
        assert "npm test" in result
        assert "12" in result

    def test_completed_task(self) -> None:
        tasks = (
            BackgroundTaskStatus(
                task_id="bg2",
                command="ls",
                phase=TaskPhase.COMPLETED,
                exit_code=0,
            ),
        )
        result = render_background_status(tasks)
        assert "exit 0" in result

    def test_failed_task(self) -> None:
        tasks = (
            BackgroundTaskStatus(
                task_id="bg3",
                command="false",
                phase=TaskPhase.FAILED,
                exit_code=1,
            ),
        )
        result = render_background_status(tasks)
        assert "exit 1" in result


class TestTaskSummaryBar:
    def test_empty(self) -> None:
        assert render_task_summary_bar() == ""

    def test_all_components(self) -> None:
        result = render_task_summary_bar(
            todo_items=(TodoItem(text="A", status="completed"),),
            agents=(AgentStatus(name="agent1", phase=TaskPhase.RUNNING),),
            background_tasks=(
                BackgroundTaskStatus(command="cmd", phase=TaskPhase.RUNNING),
            ),
        )
        assert "📋" in result
        assert "🚀" in result
        assert "bg" in result

    def test_todo_only(self) -> None:
        result = render_task_summary_bar(
            todo_items=(
                TodoItem(text="A", status="completed"),
                TodoItem(text="B", status="pending"),
            ),
        )
        assert "📋" in result
        assert "1/2" in result


class TestTodoDetailPanel:
    def test_empty(self) -> None:
        result = render_todo_detail_panel(())
        assert "暂无任务" in result

    def test_full_panel(self) -> None:
        items = (
            TodoItem(text="Design API", status="completed", id="1"),
            TodoItem(text="Write tests", status="in_progress", id="2"),
            TodoItem(text="Deploy", status="pending", id="3"),
            TodoItem(text="Fix bug", status="blocked", id="4"),
        )
        result = render_todo_detail_panel(items)
        assert "任务清单" in result
        assert "Design API" in result
        assert "Write tests" in result
        assert "1/4 完成" in result
        assert "25%" in result
