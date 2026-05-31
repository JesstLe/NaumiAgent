"""User-facing /team command helpers."""

from __future__ import annotations

from typing import Any

from naumi_agent.agents.team_protocol import (
    execute_team_signal,
    execute_team_status,
    format_team_signal_result,
)


async def run_team_command(manager: Any, arg: str) -> str:
    """Execute a manual /team command using the same protocol as tools."""
    parts = arg.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "status"
    rest = parts[1] if len(parts) > 1 else ""

    match action:
        case "" | "status" | "list":
            return await execute_team_status(manager, agent=rest.strip())
        case "handoff":
            sender, recipient, content = _split_three(rest)
            if not sender or not recipient or not content:
                return "用法：/team handoff <发送方> <接收方> <交接内容>"
            result = await execute_team_signal(
                manager,
                event_type="handoff",
                sender=sender,
                recipient=recipient,
                content=content,
                priority="high",
            )
            return format_team_signal_result(result)
        case "blocker":
            sender, content = _split_two(rest)
            if not sender or not content:
                return "用法：/team blocker <发送方> <阻塞说明>"
            result = await execute_team_signal(
                manager,
                event_type="blocker",
                sender=sender,
                content=content,
                priority="critical",
            )
            return format_team_signal_result(result)
        case "decision" | "update" | "result":
            sender, content = _split_two(rest)
            if not sender or not content:
                return f"用法：/team {action} <发送方> <内容>"
            result = await execute_team_signal(
                manager,
                event_type=action,
                sender=sender,
                content=content,
            )
            return format_team_signal_result(result)
        case "request":
            sender, recipient, content = _split_three(rest)
            if not sender or not recipient or not content:
                return "用法：/team request <发送方> <接收方> <请求内容>"
            result = await execute_team_signal(
                manager,
                event_type="request",
                sender=sender,
                recipient=recipient,
                content=content,
                priority="high",
            )
            return format_team_signal_result(result)
        case _:
            return (
                "用法：/team [status <agent>]\n"
                "/team handoff <发送方> <接收方> <交接内容>\n"
                "/team blocker <发送方> <阻塞说明>\n"
                "/team decision <发送方> <内容>\n"
                "/team update <发送方> <内容>\n"
                "/team request <发送方> <接收方> <请求内容>\n"
                "/team result <发送方> <内容>"
            )


def _split_two(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return (parts[0], "") if parts else ("", "")
    return parts[0], parts[1]


def _split_three(text: str) -> tuple[str, str, str]:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 3:
        padded = parts + [""] * (3 - len(parts))
        return padded[0], padded[1], padded[2]
    return parts[0], parts[1], parts[2]
