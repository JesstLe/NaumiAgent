"""User-facing todo slash command helpers."""

from __future__ import annotations

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore, format_task_list


async def run_todo_command(store: TaskStore, arg: str) -> str:
    """Execute a manual /todo command against the same store used by tools."""
    if not store.session_id:
        return "错误：当前没有活跃会话，无法操作 todo。"

    text = arg.strip()
    if not text or text == "list":
        return format_task_list(await store.list_tasks())

    command, _, rest = text.partition(" ")
    match command:
        case "add":
            subject = rest.strip()
            if not subject:
                return "用法：/todo add <任务标题>"
            task = await store.create_task(subject=subject)
            return f"已添加 todo #{task.id}：{task.subject}\n\n" + format_task_list(
                await store.list_tasks()
            )
        case "start":
            task_id, active_form = _split_id_and_tail(rest)
            if not task_id:
                return "用法：/todo start <id> [当前动作]"
            task = await store.update_task(
                task_id,
                status=TaskStatus.IN_PROGRESS,
                active_form=active_form or None,
            )
            if task is None:
                return f"错误：todo #{task_id} 不存在。"
            return f"todo #{task.id} 已标记为进行中。\n\n" + format_task_list(
                await store.list_tasks()
            )
        case "done":
            task_id = rest.strip()
            if not task_id:
                return "用法：/todo done <id>"
            task = await store.update_task(task_id, status=TaskStatus.COMPLETED)
            if task is None:
                return f"错误：todo #{task_id} 不存在。"
            return f"todo #{task.id} 已完成。\n\n" + format_task_list(await store.list_tasks())
        case "pending":
            task_id = rest.strip()
            if not task_id:
                return "用法：/todo pending <id>"
            task = await store.update_task(task_id, status=TaskStatus.PENDING)
            if task is None:
                return f"错误：todo #{task_id} 不存在。"
            return f"todo #{task.id} 已恢复为待处理。\n\n" + format_task_list(
                await store.list_tasks()
            )
        case "blocked":
            task_id, reason = _split_id_and_tail(rest)
            if not task_id:
                return "用法：/todo blocked <id> <阻塞原因>"
            task = await store.update_task(
                task_id,
                status=TaskStatus.BLOCKED,
                active_form=f"阻塞：{reason}" if reason else "阻塞：等待外部条件",
            )
            if task is None:
                return f"错误：todo #{task_id} 不存在。"
            return f"todo #{task.id} 已标记为阻塞。\n\n" + format_task_list(
                await store.list_tasks()
            )
        case "delete":
            task_id = rest.strip()
            if not task_id:
                return "用法：/todo delete <id>"
            task = await store.get_task(task_id)
            if task is None:
                return f"错误：todo #{task_id} 不存在。"
            await store.delete_task(task_id)
            return f"已删除 todo #{task_id}：{task.subject}\n\n" + format_task_list(
                await store.list_tasks()
            )
        case "clear":
            count = await store.clear_session_tasks()
            return f"已清空 {count} 个 todo。"
        case _:
            return (
                "用法：/todo [list]\n"
                "/todo add <任务标题>\n"
                "/todo start <id> [当前动作]\n"
                "/todo done <id>\n"
                "/todo blocked <id> <阻塞原因>\n"
                "/todo pending <id>\n"
                "/todo delete <id>\n"
                "/todo clear"
            )


def _split_id_and_tail(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped:
        return "", ""
    parts = stripped.split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()
