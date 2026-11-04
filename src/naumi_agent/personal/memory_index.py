"""Local semantic revision index; SQLite remains the only memory/content authority."""

from __future__ import annotations

import hashlib
import threading
from functools import lru_cache
from pathlib import Path

from naumi_agent.personal.models import PersonalAgentError

_FILES = (
    "config.json",
    "model.onnx",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "vocab.txt",
)
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_EMBEDDING_LOCK = threading.RLock()


def _shared_lock(base_dir, owner_key):
    key = (str(base_dir.resolve()), owner_key)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@lru_cache(maxsize=1)
def cached_embedding():
    """Use the installed model only; never download model files during a memory read."""
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    class InstalledMiniLM(ONNXMiniLM_L6_V2):
        def _download_model_if_not_exists(self):
            directory = Path(self.DOWNLOAD_PATH) / self.EXTRACTED_FOLDER_NAME
            if any(
                not (directory / name).is_file() or (directory / name).is_symlink()
                for name in _FILES
            ):
                raise PersonalAgentError("memory_index_unavailable", "本地语义模型未就绪")

    directory = Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH) / ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME
    if any(not (directory / name).is_file() or (directory / name).is_symlink() for name in _FILES):
        raise PersonalAgentError("memory_index_unavailable", "本地语义模型未就绪")
    # Tokenizer/model identity fences derived embeddings across model upgrades.
    identity = hashlib.sha256()
    for name in ("model.onnx", "tokenizer.json"):
        with (directory / name).open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                identity.update(chunk)
    policy = "onnx-minilm-l6-v2-" + identity.hexdigest()[:24]
    return InstalledMiniLM(preferred_providers=["CPUExecutionProvider"]), policy


def revision_key(record):
    return f"{record['memory_id']}:r{record['revision']}:{record['content_sha256']}"


class PersonalMemoryIndex:
    def __init__(self, base_dir: Path, owner_key: str):
        self.base_dir, self.owner_key = Path(base_dir), owner_key
        self._client = self._collection = self._embedding = None
        self._policy = ""
        self._error = ""
        self._lock = _shared_lock(self.base_dir, self.owner_key)

    def status(self):
        return {
            "status": "ready"
            if self._collection is not None
            else ("unavailable" if self._error else "not_loaded"),
            "model": self._policy or "onnx-minilm-l6-v2",
            "error": self._error,
            "authority": "derived_revision_index",
            "documents_stored": False,
            "automatic_download": False,
            "cache_scope": "process",
            "rebuild_authority": "sqlite_current_heads",
        }

    def _open(self):
        if self._collection is not None:
            return
        import chromadb
        from chromadb.config import Settings

        if self.base_dir.is_symlink():
            raise PersonalAgentError("memory_index_unavailable", "语义索引目录存在链接别名")
        with _EMBEDDING_LOCK:
            self._embedding, self._policy = cached_embedding()
        # Local PersistentClient HNSW caches are not coherent across processes.
        # Only SQLite heads/outbox persist; each process rebuilds a private cache.
        self._client = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
        identity = "\0".join((str(self.base_dir.resolve()), self.owner_key, self._policy))
        self._collection = self._client.get_or_create_collection(
            name="personal_memory_" + hashlib.sha256(identity.encode()).hexdigest()[:40],
            metadata={
                "hnsw:space": "cosine",
                "owner_key": self.owner_key,
                "model_policy": self._policy,
                "content_authority": "sqlite",
            },
            embedding_function=None,
        )
        metadata = self._collection.metadata or {}
        if (
            metadata.get("owner_key") != self.owner_key
            or metadata.get("model_policy") != self._policy
            or metadata.get("content_authority") != "sqlite"
        ):
            self._collection = None
            raise PersonalAgentError("memory_index_unavailable", "语义索引归属不一致")

    def upsert(self, record):
        with self._lock:
            try:
                self._open()
                with _EMBEDDING_LOCK:
                    vector = self._embedding([record["topic"] + "\n" + record["content"]])[0]
                # Old revisions carry no plaintext, but are still removed mechanically.
                self._collection.delete(where={"memory_id": record["memory_id"]})
                self._collection.upsert(
                    ids=[revision_key(record)],
                    embeddings=[vector],
                    metadatas=[
                        {
                            "memory_id": record["memory_id"],
                            "revision": record["revision"],
                            "content_sha256": record["content_sha256"],
                            "owner_key": self.owner_key,
                            "actor_id": record["actor_id"],
                            "scope_kind": record["scope"]["kind"],
                            "workspace_key": record["scope"]["workspace_key"],
                            "item_id": record["scope"]["item_id"],
                            "ref_key": revision_key(record),
                        }
                    ],
                )
                self._error = ""
            except PersonalAgentError as exc:
                self._error = exc.message
                raise
            except Exception as exc:
                self._error = "本地语义索引暂不可用"
                raise PersonalAgentError("memory_index_unavailable", self._error) from exc

    def delete(self, memory_id):
        with self._lock:
            # A missing derived index contains no body and can stay uninitialized.
            if self._collection is None and not self.base_dir.exists():
                return
            try:
                self._open()
                self._collection.delete(where={"memory_id": memory_id})
            except Exception as exc:
                self._error = "本地语义索引清理待恢复"
                raise PersonalAgentError("memory_index_unavailable", self._error) from exc

    def query(self, query, records, *, limit=20):
        if not records:
            return []
        with self._lock:
            try:
                self._open()
                with _EMBEDDING_LOCK:
                    embedding = self._embedding([query])[0]
                self.ensure_current(records)
                result = []
                # Filter exact eligible revisions BEFORE top-k, not after global ranking.
                for offset in range(0, len(records), 500):
                    values = records[offset : offset + 500]
                    answer = self._collection.query(
                        query_embeddings=[embedding],
                        n_results=min(limit, len(values)),
                        where={"ref_key": {"$in": [revision_key(r) for r in values]}},
                        include=["metadatas", "distances"],
                    )
                    for identity, metadata, distance in zip(
                        answer["ids"][0],
                        answer["metadatas"][0],
                        answer["distances"][0],
                        strict=True,
                    ):
                        result.append(
                            {
                                "ref_key": identity,
                                "memory_id": metadata["memory_id"],
                                "revision": metadata["revision"],
                                "content_sha256": metadata["content_sha256"],
                                "similarity": max(0.0, min(1.0, 1.0 - float(distance))),
                            }
                        )
                self._error = ""
                return sorted(result, key=lambda r: (-r["similarity"], r["ref_key"]))[:limit]
            except PersonalAgentError:
                raise
            except Exception as exc:
                self._error = "本地语义检索暂不可用"
                raise PersonalAgentError("memory_index_unavailable", self._error) from exc

    def ensure_current(self, records):
        """Rebuild a lost/new-policy collection from exact current SQLite heads."""
        if not records:
            return 0
        with self._lock:
            self._open()
            repaired = 0
            for offset in range(0, len(records), 500):
                page = records[offset : offset + 500]
                present = set(
                    self._collection.get(ids=[revision_key(r) for r in page], include=[])["ids"]
                )
                for record in page:
                    if revision_key(record) not in present:
                        self.upsert(record)
                        repaired += 1
            return repaired

    def reconcile(self, records):
        """Full-head synchronization, unlike a query's authorized subset repair."""
        with self._lock:
            if not records and self._collection is None:
                return {"repaired": 0, "removed": 0}
            self._open()
            active = {revision_key(r) for r in records}
            stale = [key for key in self._collection.get(include=[])["ids"] if key not in active]
            if stale:
                self._collection.delete(ids=stale)
            return {"repaired": self.ensure_current(records), "removed": len(stale)}
