"""任务系统单元测试."""

from __future__ import annotations

import pytest

from naumi_agent.tasks.models import Task, TaskStatus
from naumi_agent.tasks.store import TaskStore, format_task_list
from naumi_agent.tasks.tools import (
    TaskCreateTool,
    TaskDeleteTool,
    TaskListTool,
    TaskUpdateTool,
    create_task_tools,
)


@pytest.fixture
def store(tmp_path) -> TaskStore:
    s = TaskStore(str(tmp_path / "test_tasks.db"))
    s.set_session("test-session")
    return s


class TestTaskModel:
    def test_is_blocked_when_blocker_completed(self) -> None:
        t1 = Task(id="1", session_id="s", subject="A", description="", status=TaskStatus.COMPLETED)
        t2 = Task(id="2", session_id="s", subject="B", description="", blocked_by=["1"])
        assert not t2.is_blocked([t1, t2])

    def test_is_blocked_when_blocker_pending(self) -> None:
        t1 = Task(id="1", session_id="s", subject="A", description="", status=TaskStatus.PENDING)
        t2 = Task(id="2", session_id="s", subject="B", description="", blocked_by=["1"])
        assert t2.is_blocked([t1, t2])

    def test_is_blocked_no_blockers(self) -> None:
        t = Task(id="1", session_id="s", subject="A", description="")
        assert not t.is_blocked([t])


class TestTaskStore:
    @pytest.mark.asyncio
    async def test_create_task(self, store: TaskStore) -> None:
        task = await store.create_task(subject="读取配置文件")
        assert task.id == "1"
        assert task.subject == "读取配置文件"
        assert task.status == TaskStatus.PENDING
        assert task.session_id == "test-session"

    @pytest.mark.asyncio
    async def test_auto_increment_ids(self, store: TaskStore) -> None:
        t1 = await store.create_task(subject="A")
        t2 = await store.create_task(subject="B")
        t3 = await store.create_task(subject="C")
        assert t1.id == "1"
        assert t2.id == "2"
        assert t3.id == "3"

    @pytest.mark.asyncio
    async def test_create_with_dependencies(self, store: TaskStore) -> None:
        t1 = await store.create_task(subject="Read files")
        t2 = await store.create_task(subject="Analyze", blocked_by=[t1.id])
        assert t2.blocked_by == ["1"]

        # Verify reverse edge
        t1_refreshed = await store.get_task("1")
        assert t1_refreshed is not None
        assert "2" in t1_refreshed.blocks

    @pytest.mark.asyncio
    async def test_update_status(self, store: TaskStore) -> None:
        await store.create_task(subject="A")
        updated = await store.update_task("1", status=TaskStatus.IN_PROGRESS, active_form="正在执行")
        assert updated is not None
        assert updated.status == TaskStatus.IN_PROGRESS
        assert updated.active_form == "正在执行"

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, store: TaskStore) -> None:
        result = await store.update_task("999", status=TaskStatus.COMPLETED)
        assert result is None

    @pytest.mark.asyncio
    async def test_complete_task(self, store: TaskStore) -> None:
        await store.create_task(subject="A")
        updated = await store.update_task("1", status=TaskStatus.COMPLETED)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_list_tasks(self, store: TaskStore) -> None:
        await store.create_task(subject="A")
        await store.create_task(subject="B")
        tasks = await store.list_tasks()
        assert len(tasks) == 2
        assert tasks[0].id == "1"
        assert tasks[1].id == "2"

    @pytest.mark.asyncio
    async def test_delete_task(self, store: TaskStore) -> None:
        await store.create_task(subject="A")
        await store.create_task(subject="B", blocked_by=["1"])
        deleted = await store.delete_task("1")
        assert deleted is True

        # Verify task is gone
        assert await store.get_task("1") is None

        # Verify reverse edge cleaned up
        t2 = await store.get_task("2")
        assert t2 is not None
        assert t2.blocked_by == []

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, store: TaskStore) -> None:
        assert not await store.delete_task("999")

    @pytest.mark.asyncio
    async def test_session_isolation(self, tmp_path) -> None:
        store_a = TaskStore(str(tmp_path / "test.db"))
        store_a.set_session("session-a")
        await store_a.create_task(subject="A only")

        store_b = TaskStore(str(tmp_path / "test.db"))
        store_b.set_session("session-b")
        await store_b.create_task(subject="B only")

        tasks_a = await store_a.list_tasks()
        tasks_b = await store_b.list_tasks()
        assert len(tasks_a) == 1
        assert tasks_a[0].subject == "A only"
        assert len(tasks_b) == 1
        assert tasks_b[0].subject == "B only"

    @pytest.mark.asyncio
    async def test_clear_session_tasks(self, store: TaskStore) -> None:
        await store.create_task(subject="A")
        await store.create_task(subject="B")
        count = await store.clear_session_tasks()
        assert count == 2
        assert await store.list_tasks() == []

    @pytest.mark.asyncio
    async def test_no_session_returns_empty(self, tmp_path) -> None:
        store = TaskStore(str(tmp_path / "test.db"))
        # No set_session called
        tasks = await store.list_tasks()
        assert tasks == []


class TestFormatTaskList:
    def test_empty_list(self) -> None:
        result = format_task_list([])
        assert "当前没有任务" in result

    def test_basic_format(self) -> None:
        tasks = [
            Task(id="1", session_id="s", subject="Read files", description="", status=TaskStatus.COMPLETED),
            Task(id="2", session_id="s", subject="Analyze", description="", status=TaskStatus.IN_PROGRESS),
            Task(id="3", session_id="s", subject="Report", description="", status=TaskStatus.PENDING),
        ]
        result = format_task_list(tasks)
        assert "✓" in result
        assert "●" in result
        assert "○" in result
        assert "3 项" in result
        assert "1 完成" in result
        assert "1 进行中" in result
        assert "1 待处理" in result

    def test_blocked_task_shows_dependency(self) -> None:
        tasks = [
            Task(id="1", session_id="s", subject="A", description="", status=TaskStatus.PENDING),
            Task(id="2", session_id="s", subject="B", description="", blocked_by=["1"]),
        ]
        result = format_task_list(tasks)
        assert "blocked by #1" in result

    def test_active_form_shown_for_in_progress(self) -> None:
        tasks = [
            Task(
                id="1",
                session_id="s",
                subject="Test",
                description="",
                status=TaskStatus.IN_PROGRESS,
                active_form="正在运行测试",
            ),
        ]
        result = format_task_list(tasks)
        assert "正在运行测试" in result


class TestTaskTools:
    @pytest.mark.asyncio
    async def test_create_tool(self, store: TaskStore) -> None:
        tool = TaskCreateTool(store)
        result = await tool.execute(subject="读取文件")
        assert "已创建任务" in result
        assert "读取文件" in result

    @pytest.mark.asyncio
    async def test_create_with_invalid_blocker(self, store: TaskStore) -> None:
        tool = TaskCreateTool(store)
        result = await tool.execute(subject="A", blocked_by=["999"])
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_update_tool(self, store: TaskStore) -> None:
        await store.create_task(subject="A")
        tool = TaskUpdateTool(store)
        result = await tool.execute(task_id="1", status="in_progress", active_form="工作中")
        assert "进行中" in result

    @pytest.mark.asyncio
    async def test_update_invalid_status(self, store: TaskStore) -> None:
        await store.create_task(subject="A")
        tool = TaskUpdateTool(store)
        result = await tool.execute(task_id="1", status="invalid")
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_list_tool(self, store: TaskStore) -> None:
        await store.create_task(subject="A")
        await store.create_task(subject="B")
        tool = TaskListTool(store)
        result = await tool.execute()
        assert "2 项" in result

    @pytest.mark.asyncio
    async def test_delete_tool(self, store: TaskStore) -> None:
        await store.create_task(subject="A")
        tool = TaskDeleteTool(store)
        result = await tool.execute(task_id="1")
        assert "已删除" in result

    @pytest.mark.asyncio
    async def test_create_tool_returns_4_tools(self, store: TaskStore) -> None:
        tools = create_task_tools(store)
        assert len(tools) == 4
        names = {t.name for t in tools}
        assert names == {"task_create", "task_update", "task_list", "task_delete"}

    @pytest.mark.asyncio
    async def test_no_session_error(self, tmp_path) -> None:
        store = TaskStore(str(tmp_path / "test.db"))
        # No set_session
        tool = TaskCreateTool(store)
        result = await tool.execute(subject="test")
        assert "错误" in result
