"""任务系统单元测试."""

from __future__ import annotations

import sqlite3

import pytest

from naumi_agent.tasks.commands import run_todo_command
from naumi_agent.tasks.models import Task, TaskStatus
from naumi_agent.tasks.store import TaskStore, TaskWriteItem, format_task_list
from naumi_agent.tasks.tools import (
    TaskCreateTool,
    TaskDeleteTool,
    TaskListTool,
    TaskUpdateTool,
    TodoWriteTool,
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
    async def test_legacy_global_task_id_schema_is_migrated_before_creating_task(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "legacy_tasks.db"
        with sqlite3.connect(db_path) as db:
            db.execute(
                """CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    active_form TEXT,
                    owner TEXT,
                    blocks TEXT NOT NULL DEFAULT '[]',
                    blocked_by TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """INSERT INTO tasks
                   (id, session_id, subject, description, status, active_form,
                    owner, blocks, blocked_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "1",
                    "legacy-session",
                    "旧任务",
                    "",
                    "pending",
                    None,
                    None,
                    "[]",
                    "[]",
                    "2026-07-12T00:00:00",
                    "2026-07-12T00:00:00",
                ),
            )

        new_session = TaskStore(str(db_path))
        new_session.set_session("new-session")

        created = await new_session.create_task(subject="新会话任务")

        assert created.id == "1"
        legacy_session = TaskStore(str(db_path))
        legacy_session.set_session("legacy-session")
        assert [task.subject for task in await legacy_session.list_tasks()] == ["旧任务"]
        with sqlite3.connect(db_path) as db:
            primary_key = [
                row[1]
                for row in sorted(
                    db.execute("PRAGMA table_info(tasks)").fetchall(),
                    key=lambda row: row[5],
                )
                if row[5] > 0
            ]
        assert primary_key == ["session_id", "id"]

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
        updated = await store.update_task(
            "1", status=TaskStatus.IN_PROGRESS, active_form="正在执行"
        )
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

    @pytest.mark.asyncio
    async def test_write_tasks_creates_and_updates_in_batch(self, store: TaskStore) -> None:
        base = await store.create_task(subject="读取代码")

        result = await store.write_tasks([
            TaskWriteItem(
                id=base.id,
                subject="读取代码",
                status=TaskStatus.COMPLETED,
            ),
            TaskWriteItem(
                subject="编写测试",
                status=TaskStatus.IN_PROGRESS,
                active_form="正在编写测试",
                blocked_by=[base.id],
            ),
        ])

        assert result.created == ["2"]
        assert result.updated == ["1"]
        tasks = await store.list_tasks()
        assert tasks[0].status == TaskStatus.COMPLETED
        assert tasks[1].active_form == "正在编写测试"
        assert tasks[0].blocks == ["2"]

    @pytest.mark.asyncio
    async def test_write_tasks_replace_deletes_omitted_tasks(self, store: TaskStore) -> None:
        first = await store.create_task(subject="A")
        await store.create_task(subject="B")

        result = await store.write_tasks([
            TaskWriteItem(id=first.id, subject="A", status=TaskStatus.COMPLETED),
        ], replace=True)

        assert result.deleted == ["2"]
        tasks = await store.list_tasks()
        assert [task.id for task in tasks] == ["1"]

    @pytest.mark.asyncio
    async def test_write_tasks_rejects_completed_rollback(self, store: TaskStore) -> None:
        task = await store.create_task(subject="A")
        await store.update_task(task.id, status=TaskStatus.COMPLETED)

        with pytest.raises(ValueError, match="不能回退"):
            await store.write_tasks([
                TaskWriteItem(id=task.id, subject="A", status=TaskStatus.PENDING),
            ])

    @pytest.mark.asyncio
    async def test_write_tasks_updates_reverse_edge_timestamp(self, store: TaskStore) -> None:
        blocker = await store.create_task(subject="Blocker")

        await store.write_tasks([
            TaskWriteItem(subject="Blocked", status=TaskStatus.PENDING, blocked_by=[blocker.id]),
        ])

        refreshed = await store.get_task(blocker.id)
        assert refreshed is not None
        assert refreshed.blocks == ["2"]
        assert refreshed.updated_at >= blocker.updated_at


class TestFormatTaskList:
    def test_empty_list(self) -> None:
        result = format_task_list([])
        assert "当前没有任务" in result

    def test_basic_format(self) -> None:
        tasks = [
            Task(
                id="1", session_id="s", subject="Read files",
                description="", status=TaskStatus.COMPLETED,
            ),
            Task(
                id="2", session_id="s", subject="Analyze",
                description="", status=TaskStatus.IN_PROGRESS,
            ),
            Task(
                id="3", session_id="s", subject="Report",
                description="", status=TaskStatus.PENDING,
            ),
            Task(
                id="4", session_id="s", subject="Blocked",
                description="", status=TaskStatus.BLOCKED, active_form="阻塞：等待输入",
            ),
        ]
        result = format_task_list(tasks)
        assert "✓" in result
        assert "●" in result
        assert "○" in result
        assert "!" in result
        assert "4 项" in result
        assert "1 完成" in result
        assert "1 进行中" in result
        assert "1 阻塞" in result
        assert "1 待处理" in result
        assert "阻塞：等待输入" in result

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
        assert len(tools) == 5
        names = {t.name for t in tools}
        assert names == {
            "todo_write",
            "task_create",
            "task_update",
            "task_list",
            "task_delete",
        }

    @pytest.mark.asyncio
    async def test_no_session_error(self, tmp_path) -> None:
        store = TaskStore(str(tmp_path / "test.db"))
        # No set_session
        tool = TaskCreateTool(store)
        result = await tool.execute(subject="test")
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_create_with_duplicate_blockers(self, store: TaskStore) -> None:
        t1 = await store.create_task(subject="Base")
        t2 = await store.create_task(subject="Dup", blocked_by=[t1.id, t1.id])
        assert t2.blocked_by == ["1"]  # deduplicated
        t1_refreshed = await store.get_task("1")
        assert t1_refreshed is not None
        assert t1_refreshed.blocks == ["2"]  # only one reverse edge

    @pytest.mark.asyncio
    async def test_delete_updates_reverse_edges_updated_at(self, store: TaskStore) -> None:
        t1 = await store.create_task(subject="Blocker")
        await store.create_task(subject="Blocked", blocked_by=[t1.id])
        await store.delete_task("1")
        t2_refreshed = await store.get_task("2")
        assert t2_refreshed is not None
        assert t2_refreshed.blocked_by == []
        assert t2_refreshed.updated_at >= t2_refreshed.created_at

    @pytest.mark.asyncio
    async def test_update_invalid_transition(self, store: TaskStore) -> None:
        await store.create_task(subject="A")
        await store.update_task("1", status=TaskStatus.COMPLETED)
        tool = TaskUpdateTool(store)
        result = await tool.execute(task_id="1", status="pending")
        assert "错误" in result
        assert "不可回退" in result

    @pytest.mark.asyncio
    async def test_create_tool_rejects_empty_subject(self, store: TaskStore) -> None:
        tool = TaskCreateTool(store)
        result = await tool.execute(subject="   ")
        assert "错误" in result
        assert "不能为空" in result

    @pytest.mark.asyncio
    async def test_todo_write_tool_syncs_visible_list(self, store: TaskStore) -> None:
        tool = TodoWriteTool(store)

        result = await tool.execute(todos=[
            {"content": "读取实现", "status": "completed"},
            {
                "content": "补测试",
                "status": "in_progress",
                "active_form": "正在补测试",
                "blocked_by": ["1"],
            },
        ])

        assert "todo 已同步" in result
        assert "新增 2 项" in result
        assert "正在补测试" in result

    @pytest.mark.asyncio
    async def test_todo_write_merge_updates_existing_item_by_content(
        self,
        store: TaskStore,
    ) -> None:
        tool = TodoWriteTool(store)
        await tool.execute(todos=[
            {"content": "创建项目目录结构", "status": "pending"},
            {"content": "编写 HTML 文件", "status": "pending"},
        ])

        result = await tool.execute(todos=[
            {"content": "创建项目目录结构", "status": "completed"},
        ])
        tasks = await store.list_tasks()

        assert "更新 1 项" in result
        assert "新增" not in result
        assert [task.subject for task in tasks] == ["创建项目目录结构", "编写 HTML 文件"]
        assert tasks[0].status == TaskStatus.COMPLETED
        assert tasks[1].status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_todo_write_merge_removes_existing_duplicate_subjects(
        self,
        store: TaskStore,
    ) -> None:
        tool = TodoWriteTool(store)
        await store.create_task(subject="编写 HTML 文件")
        await store.create_task(subject="编写 HTML 文件")

        result = await tool.execute(todos=[
            {
                "content": "编写 HTML 文件",
                "status": "in_progress",
                "active_form": "正在编写 HTML 文件",
            },
        ])
        tasks = await store.list_tasks()

        assert "更新 1 项" in result
        assert "删除 1 项" in result
        assert len(tasks) == 1
        assert tasks[0].id == "1"
        assert tasks[0].status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_todo_write_rejects_duplicate_content_in_payload(
        self,
        store: TaskStore,
    ) -> None:
        tool = TodoWriteTool(store)

        result = await tool.execute(todos=[
            {"content": "编写 HTML 文件", "status": "pending"},
            {"content": " 编写   HTML 文件 ", "status": "pending"},
        ])

        assert "错误" in result
        assert "重复任务" in result

    @pytest.mark.asyncio
    async def test_todo_write_tool_rejects_multiple_in_progress(
        self,
        store: TaskStore,
    ) -> None:
        tool = TodoWriteTool(store)

        result = await tool.execute(todos=[
            {"content": "A", "status": "in_progress"},
            {"content": "B", "status": "in_progress"},
        ])

        assert "错误" in result
        assert "最多只能有一个" in result


class TestTodoCommand:
    @pytest.mark.asyncio
    async def test_manual_todo_command_uses_store(self, store: TaskStore) -> None:
        added = await run_todo_command(store, "add 读取实现")
        started = await run_todo_command(store, "start 1 正在读取实现")
        blocked = await run_todo_command(store, "blocked 1 等待用户确认")
        done = await run_todo_command(store, "done 1")

        assert "已添加" in added
        assert "进行中" in started
        assert "阻塞" in blocked
        assert "已完成" in done
        tasks = await store.list_tasks()
        assert tasks[0].status == TaskStatus.COMPLETED


class TestTaskFormatting:
    @pytest.mark.asyncio
    async def test_format_task_list_shows_owner(self, store: TaskStore) -> None:
        task = await store.create_task(subject="隔离实现")
        await store.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            active_form="在隔离 worktree `demo` 中推进",
            owner="worktree:demo",
        )
        tasks = await store.list_tasks()

        output = format_task_list(tasks)

        assert "worktree:demo" in output
        assert "在隔离 worktree `demo` 中推进" in output


class TestDanglingReference:
    def test_is_blocked_with_dangling_reference(self) -> None:
        # dangling reference: task claims to be blocked by "99" which doesn't exist
        task = Task(id="1", session_id="s", subject="A", description="", blocked_by=["99"])
        # With no matching blocker in all_tasks, should still be treated as blocked
        assert task.is_blocked([task])
