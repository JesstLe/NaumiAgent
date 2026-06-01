"""Tests for shared render cache helpers."""

from naumi_agent.ui.messages.base import MessageType, UIMessage
from naumi_agent.ui.render_cache import RenderLRUCache, message_render_cache_key


def test_render_lru_cache_tracks_hits_misses_and_eviction() -> None:
    cache: RenderLRUCache[str, str] = RenderLRUCache(capacity=2)

    assert cache.get("a") is None
    cache.set("a", "A")
    cache.set("b", "B")
    assert cache.get("a") == "A"
    cache.set("c", "C")

    assert cache.get("b") is None
    stats = cache.stats()
    assert stats.hits == 1
    assert stats.misses == 2
    assert stats.size == 2


def test_message_render_cache_key_uses_type_and_id() -> None:
    message = UIMessage(type=MessageType.SYSTEM_NOTICE, message_id="fixed")

    assert message_render_cache_key(message) == ("system_notice", "fixed")
