"""Structured team protocol on top of the agent message bus."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from naumi_agent.agents.message_bus import AgentMessage, MessagePriority
from naumi_agent.runtime.ports.events import LegacyEventCallback, RuntimeEventType


class TeamEventType(StrEnum):
    HANDOFF = "handoff"
    DECISION = "decision"
    BLOCKER = "blocker"
    UPDATE = "update"
    REQUEST = "request"
    RESULT = "result"


_EVENT_LABELS = {
    TeamEventType.HANDOFF: "交接",
    TeamEventType.DECISION: "决策",
    TeamEventType.BLOCKER: "阻塞",
    TeamEventType.UPDATE: "进展",
    TeamEventType.REQUEST: "请求",
    TeamEventType.RESULT: "结果",
}


@dataclass(frozen=True)
class TeamSignalResult:
    event_type: TeamEventType
    sender: str
    recipient: str | None
    topic: str
    content: str
    priority: MessagePriority
    task_id: str
    blackboard_key: str
    delivered_to: int


async def execute_team_signal(
    manager: Any,
    *,
    event_type: str,
    sender: str,
    content: str,
    recipient: str = "",
    topic: str = "",
    priority: str = "normal",
    task_id: str = "",
    blackboard_key: str = "",
    record_to_blackboard: bool = True,
    event_callback: LegacyEventCallback | None = None,
) -> TeamSignalResult:
    """Publish one structured team event through bus + blackboard + UI callback."""
    normalized_event = _parse_event_type(event_type)
    normalized_priority = _parse_priority(priority)
    sender_name = sender.strip() or "main_agent"
    target = recipient.strip() or None
    event_topic = topic.strip() or f"team.{normalized_event.value}"
    body = content.strip()
    if not body:
        raise ValueError("团队事件内容不能为空。")

    key = blackboard_key.strip()
    timestamp_ms = int(time.time() * 1000)
    if record_to_blackboard and not key:
        key = f"team/{normalized_event.value}/{sender_name}/{timestamp_ms}"

    payload = {
        "type": normalized_event.value,
        "sender": sender_name,
        "recipient": target,
        "topic": event_topic,
        "priority": normalized_priority.value,
        "task_id": task_id.strip(),
        "content": body,
        "timestamp_ms": timestamp_ms,
        "protocol_version": 1,
    }

    bus = manager.message_bus
    if key:
        await bus.blackboard_set(key, payload, author=sender_name)

    message = AgentMessage(
        sender=sender_name,
        recipient=target,
        topic=event_topic,
        content=body,
        priority=normalized_priority,
        metadata={
            "team_event_type": normalized_event.value,
            "task_id": task_id.strip(),
            "blackboard_key": key,
            "protocol_version": 1,
        },
    )
    if target:
        await bus.send(message)
        delivered = 1
    else:
        delivered = await bus.publish(message)

    result = TeamSignalResult(
        event_type=normalized_event,
        sender=sender_name,
        recipient=target,
        topic=event_topic,
        content=body,
        priority=normalized_priority,
        task_id=task_id.strip(),
        blackboard_key=key,
        delivered_to=delivered,
    )
    await _emit_team_event(event_callback, result)
    return result


async def execute_team_status(
    manager: Any,
    *,
    agent: str = "",
    limit: int = 10,
) -> str:
    """Return a human-readable team protocol snapshot."""
    bus = manager.message_bus
    stats = bus.stats()
    safe_limit = max(1, min(limit, 50))
    history = bus.get_history(limit=safe_limit)
    blackboard = await bus.blackboard_get_all()

    lines = [
        "团队协议状态",
        f"- 消息总数：{stats['total_messages']}",
        f"- 待处理私信：{stats['pending_messages']}",
        f"- 黑板条目：{stats['blackboard_entries']}",
    ]

    agent_name = agent.strip()
    if agent_name:
        pending = await bus.peek(agent_name, limit=safe_limit)
        lines.append(f"- {agent_name} 待处理消息：{len(pending)}")
        for msg in pending:
            lines.append(
                f"  - [{msg.priority.value}] {msg.sender} → {agent_name}: "
                f"{msg.content[:120]}"
            )

    if history:
        lines.append("")
        lines.append(f"最近团队消息（{len(history)} 条）")
        for msg in history:
            event = msg.metadata.get("team_event_type", msg.topic)
            target = msg.recipient or "广播"
            lines.append(
                f"- {event}: {msg.sender} → {target} "
                f"[{msg.priority.value}] {msg.content[:120]}"
            )

    team_entries = [
        (key, entry) for key, entry in sorted(blackboard.items())
        if key.startswith("team/")
    ]
    if team_entries:
        lines.append("")
        lines.append(f"团队黑板（{len(team_entries)} 条）")
        for key, entry in team_entries[-safe_limit:]:
            value = entry.value
            content = value.get("content", value) if isinstance(value, dict) else value
            lines.append(f"- {key} (v{entry.version}, {entry.author}): {str(content)[:120]}")

    return "\n".join(lines)


def format_team_signal_result(result: TeamSignalResult) -> str:
    target = result.recipient or "广播"
    record = f"\n黑板记录：{result.blackboard_key}" if result.blackboard_key else ""
    return (
        f"团队{_EVENT_LABELS[result.event_type]}已发布："
        f"{result.sender} → {target}\n"
        f"主题：{result.topic}\n"
        f"优先级：{result.priority.value}\n"
        f"投递：{result.delivered_to} 个接收方"
        f"{record}\n\n"
        f"{result.content}"
    )


async def _emit_team_event(
    callback: LegacyEventCallback | None,
    result: TeamSignalResult,
) -> None:
    if callback is None:
        return
    await callback(RuntimeEventType.TEAM_EVENT.value, {
        "event_type": result.event_type.value,
        "sender": result.sender,
        "recipient": result.recipient or "",
        "topic": result.topic,
        "priority": result.priority.value,
        "task_id": result.task_id,
        "blackboard_key": result.blackboard_key,
        "message": result.content,
        "delivered_to": result.delivered_to,
    })


def _parse_event_type(raw: str) -> TeamEventType:
    try:
        return TeamEventType(raw.strip().lower())
    except ValueError as exc:
        valid = ", ".join(event.value for event in TeamEventType)
        raise ValueError(f"无效团队事件类型 '{raw}'。有效值：{valid}") from exc


def _parse_priority(raw: str) -> MessagePriority:
    try:
        return MessagePriority(raw.strip().lower())
    except ValueError as exc:
        valid = ", ".join(priority.value for priority in MessagePriority)
        raise ValueError(f"无效优先级 '{raw}'。有效值：{valid}") from exc
