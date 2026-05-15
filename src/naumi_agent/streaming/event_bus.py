"""异步事件总线 — 发布/订阅模式."""

from __future__ import annotations

import asyncio
import logging

from naumi_agent.streaming.events import EventType, StreamEvent

logger = logging.getLogger(__name__)


class EventEmitter:
    """异步事件总线."""

    def __init__(self, max_queue_size: int = 1000, history_limit: int = 500) -> None:
        self._max_queue_size = max_queue_size
        self._history_limit = history_limit
        self._subscribers: dict[str, asyncio.Queue[StreamEvent]] = {}
        self._filters: dict[str, set[EventType]] = {}
        self._history: list[StreamEvent] = []

    def subscribe(
        self,
        subscriber_id: str,
        event_types: set[EventType] | None = None,
    ) -> asyncio.Queue[StreamEvent]:
        """订阅事件流，返回异步队列."""
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers[subscriber_id] = queue
        if event_types:
            self._filters[subscriber_id] = event_types
        return queue

    def unsubscribe(self, subscriber_id: str) -> None:
        """取消订阅."""
        self._subscribers.pop(subscriber_id, None)
        self._filters.pop(subscriber_id, None)

    async def emit(self, event: StreamEvent) -> None:
        """发布事件到所有订阅者."""
        self._history.append(event)
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit :]

        for sub_id, queue in self._subscribers.items():
            allowed = self._filters.get(sub_id)
            if allowed and event.type not in allowed:
                continue

            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

            await queue.put(event)

    def get_history(
        self,
        event_types: set[EventType] | None = None,
        limit: int = 50,
    ) -> list[StreamEvent]:
        """获取历史事件."""
        events = self._history
        if event_types:
            events = [e for e in events if e.type in event_types]
        return events[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
