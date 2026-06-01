"""Small LRU helpers for UI message rendering caches."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import TypeVar

from naumi_agent.ui.messages.base import UIMessage

K = TypeVar("K")
V = TypeVar("V")


@dataclass(frozen=True)
class RenderCacheStats:
    hits: int
    misses: int
    size: int
    capacity: int


class RenderLRUCache[K, V]:
    """Bounded LRU cache with hit/miss counters."""

    def __init__(self, capacity: int = 2_048) -> None:
        self.capacity = max(1, capacity)
        self._items: OrderedDict[K, V] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, key: K) -> V | None:
        if key not in self._items:
            self._misses += 1
            return None
        self._hits += 1
        self._items.move_to_end(key)
        return self._items[key]

    def set(self, key: K, value: V) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        if len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
        self._hits = 0
        self._misses = 0

    def stats(self) -> RenderCacheStats:
        return RenderCacheStats(
            hits=self._hits,
            misses=self._misses,
            size=len(self._items),
            capacity=self.capacity,
        )


def message_render_cache_key(message: UIMessage) -> tuple[str, str]:
    """Return the stable cache key for one immutable UIMessage."""
    return (message.type.value, message.message_id)
