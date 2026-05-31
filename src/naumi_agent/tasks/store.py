"""任务存储 — SQLite 后端."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import aiosqlite

from naumi_agent.tasks.models import Task, TaskStatus

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    active_form TEXT,
    owner TEXT,
    blocks TEXT NOT NULL DEFAULT '[]',
    blocked_by TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, id)
)
"""


@dataclass(frozen=True)
class TaskWriteItem:
    """Normalized input for batch task writes."""

    subject: str
    status: TaskStatus = TaskStatus.PENDING
    id: str | None = None
    description: str = ""
    active_form: str | None = None
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaskWriteResult:
    """Result of a batch task write."""

    tasks: list[Task]
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def _task_to_row(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "session_id": task.session_id,
        "subject": task.subject,
        "description": task.description,
        "status": task.status,
        "active_form": task.active_form,
        "owner": task.owner,
        "blocks": json.dumps(task.blocks),
        "blocked_by": json.dumps(task.blocked_by),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _row_to_task(row: dict[str, Any]) -> Task:
    raw_blocks = row.get("blocks")
    raw_blocked_by = row.get("blocked_by")
    return Task(
        id=row["id"],
        session_id=row["session_id"],
        subject=row["subject"],
        description=row["description"],
        status=TaskStatus(row["status"]),
        active_form=row.get("active_form"),
        owner=row.get("owner"),
        blocks=cast(list[str], json.loads(raw_blocks)) if isinstance(raw_blocks, str) else [],
        blocked_by=(
            cast(list[str], json.loads(raw_blocked_by))
            if isinstance(raw_blocked_by, str)
            else []
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class TaskStore:
    """SQLite-backed task storage, scoped to a session."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._session_id: str = ""
        self._initialized = False

    def set_session(self, session_id: str) -> None:
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    async def _ensure_table(self, db: aiosqlite.Connection) -> None:
        if not self._initialized:
            await db.execute(_CREATE_TABLE)
            await db.commit()
            self._initialized = True

    async def _get_task(self, db: aiosqlite.Connection, task_id: str) -> Task | None:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE id = ? AND session_id = ?",
            (task_id, self._session_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_task(dict(row))

    async def _list_tasks(self, db: aiosqlite.Connection) -> list[Task]:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE session_id = ? ORDER BY CAST(id AS INTEGER)",
            (self._session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_task(dict(r)) for r in rows]

    async def _read_blocks(self, db: aiosqlite.Connection, task_id: str) -> list[str]:
        """Read the current blocks list for a task."""
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT blocks FROM tasks WHERE id = ? AND session_id = ?",
            (task_id, self._session_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return []
        raw = row["blocks"]
        if raw is None:
            return []
        return cast(list[str], json.loads(raw)) if isinstance(raw, str) else raw

    async def create_task(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[str] | None = None,
    ) -> Task:
        now = datetime.now().isoformat()
        blocked_by = list(dict.fromkeys(blocked_by or []))

        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_table(db)

            # Validate blocker IDs exist (same connection, best-effort isolation)
            for bid in blocked_by:
                existing = await self._get_task(db, bid)
                if existing is None:
                    raise ValueError(f"依赖任务 #{bid} 不存在")

            cursor = await db.execute(
                "SELECT MAX(CAST(id AS INTEGER)) FROM tasks WHERE session_id = ?",
                (self._session_id,),
            )
            row = await cursor.fetchone()
            highest = int(row[0] or 0) if row and row[0] is not None else 0
            task_id = str(highest + 1)

            task = Task(
                id=task_id,
                session_id=self._session_id,
                subject=subject,
                description=description,
                blocked_by=blocked_by,
                created_at=now,
                updated_at=now,
            )

            await db.execute(
                """INSERT INTO tasks
                   (id, session_id, subject, description, status, active_form,
                    owner, blocks, blocked_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.id, task.session_id, task.subject, task.description,
                    task.status, task.active_form, task.owner,
                    json.dumps(task.blocks), json.dumps(task.blocked_by),
                    task.created_at, task.updated_at,
                ),
            )

            # Update reverse blocking edges: blocker.tasks_that_i_block += [new_task_id]
            for blocker_id in blocked_by:
                current_blocks = await self._read_blocks(db, blocker_id)
                if task_id not in current_blocks:
                    current_blocks.append(task_id)
                    await db.execute(
                        "UPDATE tasks SET blocks = ?, updated_at = ? "
                        "WHERE id = ? AND session_id = ?",
                        (json.dumps(current_blocks), now, blocker_id, self._session_id),
                    )

            await db.commit()

        return task

    async def update_task(
        self,
        task_id: str,
        status: TaskStatus | None = None,
        active_form: str | None = None,
        owner: str | None = None,
    ) -> Task | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_table(db)
            existing = await self._get_task(db, task_id)
            if existing is None:
                return None

            now = datetime.now().isoformat()
            new_status = status if status is not None else existing.status
            new_active_form = active_form if active_form is not None else existing.active_form
            new_owner = owner if owner is not None else existing.owner

            await db.execute(
                """UPDATE tasks
                   SET status = ?, active_form = ?, owner = ?, updated_at = ?
                   WHERE id = ? AND session_id = ?""",
                (new_status, new_active_form, new_owner, now, task_id, self._session_id),
            )
            await db.commit()

            return Task(
                id=existing.id,
                session_id=existing.session_id,
                subject=existing.subject,
                description=existing.description,
                status=new_status,
                active_form=new_active_form,
                owner=new_owner,
                blocks=existing.blocks,
                blocked_by=existing.blocked_by,
                created_at=existing.created_at,
                updated_at=now,
            )

    async def get_task(self, task_id: str) -> Task | None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_table(db)
            return await self._get_task(db, task_id)

    async def delete_task(self, task_id: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_table(db)
            existing = await self._get_task(db, task_id)
            if existing is None:
                return False

            await db.execute(
                "DELETE FROM tasks WHERE id = ? AND session_id = ?",
                (task_id, self._session_id),
            )

            # Remove reverse blocking references from all other tasks
            now = datetime.now().isoformat()
            all_tasks = await self._list_tasks(db)
            for t in all_tasks:
                if task_id in t.blocks:
                    new_blocks = [b for b in t.blocks if b != task_id]
                    await db.execute(
                        "UPDATE tasks SET blocks = ?, updated_at = ? "
                        "WHERE id = ? AND session_id = ?",
                        (json.dumps(new_blocks), now, t.id, self._session_id),
                    )
                if task_id in t.blocked_by:
                    new_blocked = [b for b in t.blocked_by if b != task_id]
                    await db.execute(
                        "UPDATE tasks SET blocked_by = ?, updated_at = ? "
                        "WHERE id = ? AND session_id = ?",
                        (json.dumps(new_blocked), now, t.id, self._session_id),
                    )

            await db.commit()
            return True

    async def write_tasks(
        self,
        items: list[TaskWriteItem],
        *,
        replace: bool = False,
    ) -> TaskWriteResult:
        """Batch-create/update tasks, optionally replacing the visible task list."""
        if not self._session_id:
            raise ValueError("当前没有活跃会话")

        now = datetime.now().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_table(db)
            existing_tasks = await self._list_tasks(db)
            existing = {task.id: task for task in existing_tasks}
            final = dict(existing)
            touched: set[str] = set()
            created: list[str] = []
            updated: list[str] = []
            seen_ids: set[str] = set()

            highest = _highest_task_id(existing_tasks)
            for item in items:
                subject = item.subject.strip()
                if not subject:
                    raise ValueError("任务标题不能为空")

                if item.id:
                    task_id = item.id.strip()
                    if not task_id:
                        raise ValueError("任务 ID 不能为空")
                    if task_id in seen_ids:
                        raise ValueError(f"任务 #{task_id} 在 todo 列表中重复出现")
                    if task_id not in existing:
                        raise ValueError(f"任务 #{task_id} 不存在，新增任务请省略 id")
                    previous = existing[task_id]
                    if (
                        previous.status == TaskStatus.COMPLETED
                        and item.status != TaskStatus.COMPLETED
                    ):
                        raise ValueError(f"任务 #{task_id} 已完成，不能回退状态")
                    created_at = previous.created_at
                    updated.append(task_id)
                else:
                    highest += 1
                    task_id = str(highest)
                    created_at = now
                    created.append(task_id)

                seen_ids.add(task_id)
                touched.add(task_id)
                active_form = (
                    item.active_form
                    if item.status in {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}
                    else None
                )
                final[task_id] = Task(
                    id=task_id,
                    session_id=self._session_id,
                    subject=subject,
                    description=item.description,
                    status=item.status,
                    active_form=active_form,
                    owner=item.owner,
                    blocked_by=list(dict.fromkeys(item.blocked_by)),
                    created_at=created_at,
                    updated_at=now,
                )

            deleted: list[str] = []
            if replace:
                deleted = [task_id for task_id in existing if task_id not in touched]
                for task_id in deleted:
                    final.pop(task_id, None)

            _validate_task_dependencies(final)
            _rebuild_reverse_edges(final)
            for task_id, task in final.items():
                previous = existing.get(task_id)
                if previous is not None and previous.blocks != task.blocks:
                    task.updated_at = now

            for task_id in deleted:
                await db.execute(
                    "DELETE FROM tasks WHERE id = ? AND session_id = ?",
                    (task_id, self._session_id),
                )

            for task in final.values():
                row = _task_to_row(task)
                await db.execute(
                    """INSERT OR REPLACE INTO tasks
                       (id, session_id, subject, description, status, active_form,
                        owner, blocks, blocked_by, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["id"], row["session_id"], row["subject"], row["description"],
                        row["status"], row["active_form"], row["owner"],
                        row["blocks"], row["blocked_by"], row["created_at"], row["updated_at"],
                    ),
                )

            await db.commit()
            tasks = await self._list_tasks(db)

        return TaskWriteResult(
            tasks=tasks,
            created=created,
            updated=updated,
            deleted=deleted,
        )

    async def list_tasks(self) -> list[Task]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_table(db)
            return await self._list_tasks(db)

    async def clear_session_tasks(self) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_table(db)
            cursor = await db.execute(
                "DELETE FROM tasks WHERE session_id = ?",
                (self._session_id,),
            )
            await db.commit()
            return cursor.rowcount


def format_task_list(tasks: list[Task], all_tasks: list[Task] | None = None) -> str:
    """Format task list for display to the LLM or user."""
    if not tasks:
        return "当前没有任务。"

    reference = all_tasks or tasks
    lines: list[str] = []
    for t in tasks:
        icon = {
            TaskStatus.PENDING: "○",
            TaskStatus.IN_PROGRESS: "●",
            TaskStatus.BLOCKED: "!",
            TaskStatus.COMPLETED: "✓",
        }[t.status]

        subject = t.subject
        if t.status in {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED} and t.active_form:
            subject = t.active_form

        blocked = t.is_blocked(reference)
        block_suffix = f" (blocked by #{', #'.join(t.blocked_by)})" if blocked else ""

        line = f" {icon} #{t.id} {subject}{block_suffix}"
        if t.status == TaskStatus.COMPLETED:
            line = f"~~{line}~~"
        lines.append(line)

    completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)
    in_progress = sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)
    blocked_count = sum(1 for t in tasks if t.status == TaskStatus.BLOCKED)
    pending = sum(1 for t in tasks if t.status == TaskStatus.PENDING)

    header = (
        f"📋 任务进度 "
        f"({len(tasks)} 项：{completed} 完成，{in_progress} 进行中，"
        f"{blocked_count} 阻塞，{pending} 待处理)"
    )
    return header + "\n" + "\n".join(lines)


def _highest_task_id(tasks: list[Task]) -> int:
    highest = 0
    for task in tasks:
        try:
            highest = max(highest, int(task.id))
        except ValueError:
            continue
    return highest


def _validate_task_dependencies(tasks: dict[str, Task]) -> None:
    ids = set(tasks)
    for task in tasks.values():
        for blocker_id in task.blocked_by:
            if blocker_id not in ids:
                raise ValueError(f"依赖任务 #{blocker_id} 不存在")
            if blocker_id == task.id:
                raise ValueError(f"任务 #{task.id} 不能依赖自身")


def _rebuild_reverse_edges(tasks: dict[str, Task]) -> None:
    blocks = {task_id: [] for task_id in tasks}
    for task in tasks.values():
        for blocker_id in task.blocked_by:
            blocks[blocker_id].append(task.id)
    for task_id, task in tasks.items():
        task.blocks = blocks[task_id]
