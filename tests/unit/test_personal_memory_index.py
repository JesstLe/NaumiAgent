"""Actual cached ONNX embeddings and revision-filtered process-local Chroma."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from naumi_agent.personal.memory_adapter import ReadOnlyLegacyMemory
from naumi_agent.personal.memory_index import PersonalMemoryIndex, cached_embedding, revision_key
from naumi_agent.personal.models import PersonalAgentError


def record(key, content, *, revision=1, actor="main"):
    return {
        "memory_id": "pm_" + key * 32,
        "revision": revision,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "actor_id": actor,
        "topic": "Reference " + key,
        "content": content,
        "scope": {"kind": "workspace", "workspace_key": "b" * 64, "item_id": ""},
    }


def test_real_embedding_semantic_filter_no_documents_and_partial_repair(tmp_path):
    index = PersonalMemoryIndex(tmp_path / "semantic", "a" * 64)
    travel = record("1", "Use the railway for intercity journeys instead of flying.")
    cooking = record("2", "Whisk the eggs and fold the flour into the batter.")
    blocked = record("3", "Train tickets and railway timetables are confidential to another agent.")
    for value in (travel, cooking, blocked):
        index.upsert(value)
    # No lexical token overlap with the preferred note is required for this ranking.
    query = "How should I travel between cities by train?"
    result = index.query(query, [travel, cooking], limit=1)
    assert result[0]["ref_key"] == revision_key(travel)
    assert result[0]["similarity"] > 0.25
    assert all(v["memory_id"] != blocked["memory_id"] for v in result)
    raw = index._collection.get(include=["documents", "metadatas"])
    assert raw["documents"] == [None, None, None]
    assert all("content" not in metadata for metadata in raw["metadatas"])
    assert index.status()["automatic_download"] is False
    assert index.status()["documents_stored"] is False
    index._collection.delete(ids=[revision_key(travel)])
    assert index.ensure_current([travel, cooking]) == 1
    assert index.query(query, [travel], limit=1)[0]["ref_key"] == revision_key(travel)
    synchronized = index.reconcile([travel, cooking])
    assert synchronized == {"repaired": 0, "removed": 1}
    assert revision_key(blocked) not in index._collection.get(include=[])["ids"]


def test_new_policy_collection_rebuilds_from_current_heads(tmp_path):
    index = PersonalMemoryIndex(tmp_path / "semantic", "a" * 64)
    old = record("1", "OLD_REFERENCE_NO_LONGER_CURRENT")
    current = record("1", "Prefers train journeys between cities.", revision=2)
    index.upsert(old)
    index.upsert(current)
    values = index._collection.get(include=["documents"])
    assert values["ids"] == [revision_key(current)]
    # A new policy/index generation is derived, not a new memory authority.
    fresh = PersonalMemoryIndex(tmp_path / "new-policy-generation", "a" * 64)
    assert fresh.ensure_current([current]) == 1
    assert fresh.query("travel by railway", [current])[0]["revision"] == 2
    fresh.delete(current["memory_id"])
    assert fresh._collection.count() == 0


def test_owner_mismatch_remains_closed_after_first_failure(tmp_path):
    path = tmp_path / "semantic"
    original = PersonalMemoryIndex(path, "a" * 64)
    original.upsert(record("1", "A private note."))
    metadata = {k: v for k, v in original._collection.metadata.items() if k != "hnsw:space"}
    original._collection.modify(metadata={**metadata, "owner_key": "b" * 64})
    invalid = PersonalMemoryIndex(path, "a" * 64)
    for _ in range(2):
        with pytest.raises(PersonalAgentError, match="归属"):
            invalid.query("note", [record("1", "A private note.")])
        assert invalid._collection is None


def test_missing_cached_model_does_not_download(tmp_path, monkeypatch):
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    cached_embedding.cache_clear()
    monkeypatch.setattr(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH", str(tmp_path / "missing"))
    try:
        with pytest.raises(PersonalAgentError, match="本地语义模型未就绪"):
            cached_embedding()
        assert not (tmp_path / "missing").exists()
    finally:
        cached_embedding.cache_clear()


async def test_legacy_export_reads_actual_chroma_metadata_without_sdk_or_original_writes(
    tmp_path,
    monkeypatch,
):
    import chromadb
    from chromadb.config import Settings

    directory = tmp_path / "legacy"
    client = chromadb.PersistentClient(
        path=str(directory), settings=Settings(anonymized_telemetry=False)
    )
    collection = client.get_or_create_collection("naumi_memory", embedding_function=None)
    # These explicit fixture vectors only build metadata; they are not semantic evidence.
    collection.upsert(
        ids=["legacy-actual"],
        embeddings=[[0.1, 0.2, 0.3]],
        documents=["LEGACY_EXACT_ORIGINAL_720"],
        metadatas=[{"category": "preference"}],
    )
    database = directory / "chroma.sqlite3"

    def snapshot():
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return list(connection.iterdump())
        finally:
            connection.close()

    before = snapshot()

    def forbidden_sdk(*args, **kwargs):
        raise AssertionError("Legacy browsing must not initialize/migrate a Chroma client")

    monkeypatch.setattr(chromadb, "PersistentClient", forbidden_sdk)
    reader = ReadOnlyLegacyMemory(SimpleNamespace(vector_db_path=str(directory)))
    exported = json.loads(await reader.export_memories())
    assert exported == [
        {"id": "legacy-actual", "content": "LEGACY_EXACT_ORIGINAL_720", "category": "preference"}
    ]
    assert snapshot() == before and reader._client is None


async def test_legacy_missing_or_incompatible_database_is_never_bootstrapped(tmp_path):
    directory = tmp_path / "missing"
    reader = ReadOnlyLegacyMemory(SimpleNamespace(vector_db_path=str(directory)))
    assert json.loads(await reader.export_memories()) == []
    assert not directory.exists()
    directory.mkdir()
    database = directory / "chroma.sqlite3"
    database.write_bytes(b"NOT_A_SQLITE_DATABASE")
    before = database.read_bytes()
    with pytest.raises(PersonalAgentError, match="未作升级"):
        await reader.export_memories()
    assert database.read_bytes() == before
