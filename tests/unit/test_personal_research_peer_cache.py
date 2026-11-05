"""Operation-local optimization never caches current heads or grant decisions."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager

import pytest

from naumi_agent.personal.research import PersonalResearchService
from naumi_agent.personal.research_peer_authority import _RECORD_CACHE, ResearchPeerAuthorityMixin


class Reader(ResearchPeerAuthorityMixin):
    _record = PersonalResearchService._record
    owner_key, workspace_key = "owner", "workspace"

    def __init__(self):
        self.reads, self.authorizations = 0, 0
        self.value = {"id": "original", "revision": 1, "nested": {"values": ["original"]}}

    async def _read(self, callback):
        self.reads += 1
        return callback(self)

    def get(self, *_args, **_kwargs):
        return copy.deepcopy(self.value)

    async def _authorize_peer_input(self, _value):
        self.authorizations += 1

    @asynccontextmanager
    async def _context_archive_scope(self, *_args):
        yield


async def test_exact_immutable_records_are_copied_and_only_cached_within_one_operation():
    reader = Reader()
    await reader._record("provenance", "original", revision=1)
    await reader._record("provenance", "original", revision=1)
    assert reader.reads == 2
    async with reader._peer_record_scope():
        first = await reader._record("provenance", "original", revision=1)
        first["nested"]["values"].append("caller mutation")
        async with reader._peer_record_scope():
            second = await reader._record("provenance", "original", revision=1)
        assert second == reader.value and reader.reads == 3
    await reader._record("provenance", "original", revision=1)
    assert reader.reads == 4 and _RECORD_CACHE.get() is None


@pytest.mark.parametrize("kind", ["notebook", "source", "semantic", "provenance"])
async def test_latest_heads_are_never_cached(kind):
    reader = Reader()
    async with reader._peer_record_scope():
        await reader._record(kind, "original")
        await reader._record(kind, "original")
    assert reader.reads == 2


async def test_missing_records_and_boolean_revisions_never_enter_the_cache():
    reader = Reader()
    async with reader._peer_record_scope():
        reader.value = None
        assert await reader._record("provenance", "original", revision=1, missing=True) is None
        reader.value = {"id": "now present", "revision": 1}
        assert (await reader._record("provenance", "original", revision=1))["id"] == "now present"
        await reader._record("provenance", "original", revision=True)
        await reader._record("provenance", "original", revision=True)
    assert reader.reads == 4


async def test_exception_discards_the_operation_cache():
    reader = Reader()
    with pytest.raises(RuntimeError):
        async with reader._peer_record_scope():
            await reader._record("provenance", "original", revision=1)
            raise RuntimeError("interrupt")
    assert _RECORD_CACHE.get() is None
    async with reader._peer_record_scope():
        await reader._record("provenance", "original", revision=1)
    assert reader.reads == 2


async def test_nested_exact_input_reuses_basis_but_explicit_boundary_and_next_operation_recheck():
    reader = Reader()
    value = {"id": "input", "read_records": [], "context_records": [], "provenance_records": []}
    reader.value = value
    async with reader._peer_input_scope([value]):
        assert reader.authorizations == 1
        async with reader._peer_input_scope([value]):
            assert reader.authorizations == 1
        async with reader._peer_input_scope([value], force=True):
            assert reader.authorizations == 2
    async with reader._peer_input_scope([value]):
        assert reader.authorizations == 3
    assert _RECORD_CACHE.get() is None


async def test_complete_immutable_input_graph_is_read_in_one_journal_snapshot():
    reader = Reader()
    read, context, provenance = ({"id": "read"}, {"id": "context"}, {"id": "provenance"})
    value = {
        "id": "input",
        "read_records": [read],
        "context_records": [context],
        "provenance_records": [provenance],
    }
    records = {
        ("semantic_input", "input"): value,
        ("read", "read"): read,
        ("context_update", "context"): context,
        ("provenance", "provenance"): provenance,
    }
    reader.get = lambda kind, identity, **_kwargs: copy.deepcopy(records[(kind, identity)])
    async with reader._peer_input_scope([value]):
        assert reader.reads == 1
        assert await reader._record("provenance", "provenance", revision=1) == provenance
        async with reader._peer_input_scope([value], force=True):
            assert reader.reads == 1 and reader.authorizations == 2
    async with reader._peer_input_scope([value]):
        assert reader.reads == 2 and reader.authorizations == 3


async def test_independent_concurrent_operations_have_distinct_caches():
    release, entered = asyncio.Event(), asyncio.Event()
    caches = []

    async def operation():
        reader = Reader()
        async with reader._peer_record_scope():
            caches.append(_RECORD_CACHE.get())
            if len(caches) == 2:
                entered.set()
            await release.wait()

    tasks = [asyncio.create_task(operation()) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert caches[0] is not caches[1]
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert _RECORD_CACHE.get() is None
