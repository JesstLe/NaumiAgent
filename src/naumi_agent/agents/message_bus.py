"""Agent Message Bus — structured inter-agent communication.

Provides three communication primitives:
1. **Pub/Sub** — agents broadcast findings to topic subscribers
2. **Direct messages** — point-to-point agent communication
3. **Blackboard** — shared key/value state for parallel agents

The bus is scoped to a single orchestration session and reset between
tool invocations to prevent state leakage.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class MessagePriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class AgentMessage:
    """A typed message between agents."""

    sender: str
    topic: str
    content: str
    recipient: str | None = None
    priority: MessagePriority = MessagePriority.NORMAL
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    @property
    def is_broadcast(self) -> bool:
        return self.recipient is None


@dataclass
class BlackboardEntry:
    """An entry in the shared blackboard."""

    key: str
    value: Any
    author: str
    timestamp: float
    version: int = 1


# Type alias for async message handlers
MessageHandler = Callable[[AgentMessage], Coroutine[Any, Any, None]]


class AgentMessageBus:
    """Central communication hub for inter-agent messaging.

    Thread-safe via asyncio locks. Each bus instance is scoped to one
    orchestration session and should be reset between independent
    tool invocations.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Direct message queues: agent_name → list[AgentMessage]
        self._mailboxes: dict[str, list[AgentMessage]] = {}
        # Topic subscribers: topic → list of handler coroutines
        self._subscribers: dict[str, list[MessageHandler]] = {}
        # Shared blackboard: key → BlackboardEntry
        self._blackboard: dict[str, BlackboardEntry] = {}
        # Message history for audit/debugging
        self._history: list[AgentMessage] = []
        self._max_history = 500

    # ------------------------------------------------------------------
    #  Pub/Sub
    # ------------------------------------------------------------------

    async def publish(self, message: AgentMessage) -> int:
        """Broadcast a message to all subscribers of its topic.

        Returns the number of subscribers that received the message.
        """
        async with self._lock:
            self._history.append(message)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
            handlers = list(self._subscribers.get(message.topic, []))

        for handler in handlers:
            try:
                await handler(message)
            except Exception:
                logger.exception(
                    "Message handler error on topic %s", message.topic,
                )

        logger.debug(
            "Published to topic '%s' from '%s' → %d subscribers",
            message.topic, message.sender, len(handlers),
        )
        return len(handlers)

    def subscribe(
        self, topic: str, handler: MessageHandler,
    ) -> Callable[[], None]:
        """Subscribe a handler to a topic.

        Returns an unsubscribe function.
        """
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(handler)

        def _unsubscribe() -> None:
            handlers = self._subscribers.get(topic, [])
            if handler in handlers:
                handlers.remove(handler)

        return _unsubscribe

    # ------------------------------------------------------------------
    #  Direct messaging
    # ------------------------------------------------------------------

    async def send(self, message: AgentMessage) -> None:
        """Send a direct message to a specific agent.

        The message must have a `recipient`. It is also delivered to
        topic subscribers.
        """
        if not message.recipient:
            raise ValueError("Direct message requires a recipient")

        async with self._lock:
            mailbox = self._mailboxes.setdefault(message.recipient, [])
            mailbox.append(message)
            self._history.append(message)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        logger.debug(
            "Direct message: '%s' → '%s' (topic=%s)",
            message.sender, message.recipient, message.topic,
        )

        # Also deliver to topic subscribers
        handlers = self._subscribers.get(message.topic, [])
        for handler in handlers:
            try:
                await handler(message)
            except Exception:
                logger.exception(
                    "Handler error on topic %s", message.topic,
                )

    async def receive(
        self, agent_name: str, *, limit: int = 50,
    ) -> list[AgentMessage]:
        """Retrieve and clear pending messages for an agent.

        Returns messages oldest-first, up to `limit`.
        """
        async with self._lock:
            mailbox = self._mailboxes.get(agent_name, [])
            messages = mailbox[:limit]
            remaining = mailbox[limit:]
            if remaining:
                self._mailboxes[agent_name] = remaining
            else:
                self._mailboxes[agent_name] = []
            return messages

    async def peek(
        self, agent_name: str, *, limit: int = 10,
    ) -> list[AgentMessage]:
        """Peek at pending messages without consuming them."""
        async with self._lock:
            return list(self._mailboxes.get(agent_name, [])[:limit])

    # ------------------------------------------------------------------
    #  Blackboard (shared state)
    # ------------------------------------------------------------------

    async def blackboard_set(
        self, key: str, value: Any, author: str,
    ) -> BlackboardEntry:
        """Write or update a blackboard entry.

        If the key exists, increments the version counter.
        """
        async with self._lock:
            existing = self._blackboard.get(key)
            version = (existing.version + 1) if existing else 1
            entry = BlackboardEntry(
                key=key,
                value=value,
                author=author,
                timestamp=time.time(),
                version=version,
            )
            self._blackboard[key] = entry

            logger.debug(
                "Blackboard SET '%s' by '%s' (v%d)",
                key, author, version,
            )
            return entry

    async def blackboard_get(self, key: str) -> BlackboardEntry | None:
        """Read a blackboard entry (returns None if missing)."""
        async with self._lock:
            return self._blackboard.get(key)

    async def blackboard_get_all(self) -> dict[str, BlackboardEntry]:
        """Return a snapshot of the entire blackboard."""
        async with self._lock:
            return dict(self._blackboard)

    async def blackboard_updates_since(
        self, since: float,
    ) -> list[BlackboardEntry]:
        """Get all entries modified after the given timestamp."""
        async with self._lock:
            return [
                entry for entry in self._blackboard.values()
                if entry.timestamp > since
            ]

    async def blackboard_delete(self, key: str) -> bool:
        """Remove a blackboard entry. Returns True if it existed."""
        async with self._lock:
            removed = self._blackboard.pop(key, None)
            return removed is not None

    # ------------------------------------------------------------------
    #  Query / Debug
    # ------------------------------------------------------------------

    def get_history(
        self,
        *,
        topic: str | None = None,
        sender: str | None = None,
        limit: int = 50,
    ) -> list[AgentMessage]:
        """Query message history with optional filters."""
        messages = self._history
        if topic:
            messages = [m for m in messages if m.topic == topic]
        if sender:
            messages = [m for m in messages if m.sender == sender]
        return messages[-limit:]

    def get_topics(self) -> list[str]:
        """List all topics that have subscribers."""
        return list(self._subscribers.keys())

    def get_subscriber_count(self, topic: str) -> int:
        """Count subscribers for a topic."""
        return len(self._subscribers.get(topic, []))

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    async def reset(self) -> None:
        """Clear all state for a fresh orchestration session."""
        async with self._lock:
            self._mailboxes.clear()
            self._subscribers.clear()
            self._blackboard.clear()
            self._history.clear()

    def stats(self) -> dict[str, Any]:
        """Return bus statistics for monitoring."""
        return {
            "total_messages": len(self._history),
            "active_mailboxes": len(self._mailboxes),
            "pending_messages": sum(
                len(m) for m in self._mailboxes.values()
            ),
            "topics": len(self._subscribers),
            "blackboard_entries": len(self._blackboard),
        }
