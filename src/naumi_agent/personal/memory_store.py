"""Owner-bound memory authority with erasable bodies and metadata-only receipts."""

from __future__ import annotations

import errno
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from naumi_agent.personal.artifact_models import normalize_refs
from naumi_agent.personal.memory_models import (
    ACTOR,
    CATEGORIES,
    GRANT_ID,
    INDEX_ID,
    MEMORY_ID,
    OWNER,
    REQUEST_ID,
    SHA256,
    STATES,
    TOKENIZER_VERSION,
    canonical,
    choice,
    content_digest,
    digest,
    human_semantics,
    integer,
    lexical_query,
    memory_id_for_request,
    normalize_human,
    normalize_ref,
    normalize_scope,
    normalize_source,
    object_value,
    reference,
    request_fingerprint,
    source_semantics,
    text,
    tokenize,
    topic_key,
    within_ceiling,
)
from naumi_agent.personal.models import PersonalAgentError

_VERSION = 1
_LIVE = frozenset({"active", "proposed"})
_SCHEMA = (
    """CREATE TABLE memory_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
       schema_version INTEGER NOT NULL,owner_key TEXT NOT NULL,instance_id TEXT NOT NULL,
       tokenizer_version TEXT NOT NULL)""",
    """CREATE TABLE memory_heads (memory_id TEXT PRIMARY KEY,owner_key TEXT NOT NULL,
       actor_id TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,
       scope_kind TEXT NOT NULL,workspace_key TEXT NOT NULL,item_id TEXT NOT NULL,
       topic_key TEXT NOT NULL,record_json TEXT NOT NULL,record_sha256 TEXT NOT NULL)""",
    """CREATE UNIQUE INDEX memory_active_topic ON memory_heads
       (owner_key,actor_id,scope_kind,workspace_key,item_id,topic_key) WHERE state='active'""",
    """CREATE TABLE memory_payloads (memory_id TEXT PRIMARY KEY REFERENCES memory_heads(memory_id),
       revision INTEGER NOT NULL,payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL)""",
    """CREATE TABLE memory_events (memory_id TEXT NOT NULL REFERENCES memory_heads(memory_id),
       revision INTEGER NOT NULL,record_json TEXT NOT NULL,record_sha256 TEXT NOT NULL,
       PRIMARY KEY(memory_id,revision))""",
    """CREATE TABLE memory_requests (owner_key TEXT NOT NULL,request_id TEXT NOT NULL,
       actor_id TEXT NOT NULL,operation TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
       result_kind TEXT NOT NULL,result_id TEXT NOT NULL,memory_id TEXT NOT NULL,
       committed_revision INTEGER NOT NULL,record_json TEXT NOT NULL,record_sha256 TEXT NOT NULL,
       PRIMARY KEY(owner_key,request_id))""",
    """CREATE TABLE memory_grants (grant_id TEXT PRIMARY KEY,owner_key TEXT NOT NULL,
       memory_id TEXT NOT NULL REFERENCES memory_heads(memory_id),recipient_actor_id TEXT NOT NULL,
       revision INTEGER NOT NULL,state TEXT NOT NULL,record_json TEXT NOT NULL,
       record_sha256 TEXT NOT NULL)""",
    """CREATE UNIQUE INDEX memory_live_grant ON memory_grants
       (owner_key,memory_id,recipient_actor_id) WHERE state='active'""",
    """CREATE TABLE memory_grant_events (grant_id TEXT NOT NULL REFERENCES memory_grants(grant_id),
       revision INTEGER NOT NULL,record_json TEXT NOT NULL,record_sha256 TEXT NOT NULL,
       PRIMARY KEY(grant_id,revision))""",
    """CREATE TABLE memory_index_outbox (event_id TEXT PRIMARY KEY,owner_key TEXT NOT NULL,
       memory_id TEXT NOT NULL,revision INTEGER NOT NULL,action TEXT NOT NULL,
       record_json TEXT NOT NULL,record_sha256 TEXT NOT NULL)""",
    """CREATE TABLE memory_index_acks (event_id TEXT PRIMARY KEY
       REFERENCES memory_index_outbox(event_id),record_json TEXT NOT NULL,
       record_sha256 TEXT NOT NULL)""",
    """CREATE TABLE memory_gui_requests (owner_key TEXT NOT NULL,request_id TEXT NOT NULL,
       actor_id TEXT NOT NULL,workspace_key TEXT NOT NULL,operation TEXT NOT NULL,
       fingerprint TEXT NOT NULL,target_memory_id TEXT NOT NULL,payload_json TEXT,
       payload_sha256 TEXT NOT NULL,result_json TEXT,state TEXT NOT NULL,
       created_at TEXT NOT NULL,updated_at TEXT NOT NULL,record_sha256 TEXT NOT NULL,
       PRIMARY KEY(owner_key,request_id))""",
    "CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED,revision UNINDEXED,tokens)",
)
_IMMUTABLE = {
    "memory_meta",
    "memory_events",
    "memory_requests",
    "memory_grant_events",
    "memory_index_outbox",
    "memory_index_acks",
}
_TRIGGERS = tuple(
    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
    "BEGIN SELECT RAISE(ABORT,'immutable memory fact'); END"
    for table in sorted(_IMMUTABLE)
    for action in ("UPDATE", "DELETE")
)
_FTS_TABLES = {
    "memory_fts",
    "memory_fts_data",
    "memory_fts_idx",
    "memory_fts_content",
    "memory_fts_docsize",
    "memory_fts_config",
}
_TABLES = {
    "memory_meta",
    "memory_heads",
    "memory_payloads",
    "memory_events",
    "memory_requests",
    "memory_grants",
    "memory_grant_events",
    "memory_index_outbox",
    "memory_index_acks",
    "memory_gui_requests",
    *_FTS_TABLES,
}


def _broken():
    return PersonalAgentError("storage_error", "个人记忆资料库校验未通过，请恢复原资料库后继续")


def _ordinary(path):
    raw = str(path)
    if os.name == "nt" and raw.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + raw[8:])
    return Path(raw[4:]) if os.name == "nt" and raw.startswith("\\\\?\\") else path


class PersonalMemoryStore:
    """One authoritative database; indexes and request bodies never own memory truth."""

    def __init__(self, base_dir, *, owner_key=None, clock=None):
        self.base_dir = _ordinary(Path(base_dir).expanduser().absolute())
        self.db_path = self.base_dir / "personal_memory.db"
        self.owner_key = None if owner_key is None else text(owner_key, "所有者", 64, pattern=OWNER)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._seen = False
        self._witness = self.base_dir / ".personal-memory-v1"
        self._bootstrap = self.base_dir / ".personal-memory-bootstrap.lock"

    def _owner(self, value):
        value = text(value, "所有者", 64, pattern=OWNER)
        if self.owner_key is not None and value != self.owner_key:
            raise PersonalAgentError("not_found", "该记忆不存在")
        return value

    @staticmethod
    def _actor(value):
        return text(value, "Agent 身份", 35, pattern=ACTOR)

    def _path(self):
        for path in (self.base_dir, self.db_path, self._witness, self._bootstrap):
            resolved = _ordinary(path.resolve())
            if os.path.normcase(str(path)) != os.path.normcase(str(resolved)) or path.is_symlink():
                raise _broken()
            if path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise _broken()
        for path in (self.db_path, self._witness, self._bootstrap):
            if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
                raise _broken()

    @contextmanager
    def _bootstrap_guard(self, *, create):
        """Serialize first WAL/schema/witness initialization across OS processes."""
        self._path()
        if not create and not self._bootstrap.exists():
            yield
            return
        try:
            stream = self._bootstrap.open("x+b") if create else self._bootstrap.open("r+b")
        except FileExistsError:
            stream = self._bootstrap.open("r+b")
        with stream:
            self._path()
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not os.path.samestat(info, self._bootstrap.stat())
            ):
                raise _broken()
            if os.name == "nt":
                import msvcrt

                def lock():
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

                def unlock():
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                def lock():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                def unlock():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

            deadline = time.monotonic() + 15
            while True:
                try:
                    lock()
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise _broken() from exc
                    time.sleep(0.01)
            try:
                self._path()
                stream.seek(0)
                current = stream.read(2)
                if current not in {b"", b"\x00"}:
                    raise _broken()
                if not current:
                    stream.write(b"\x00")
                    stream.flush()
                    os.fsync(stream.fileno())
                yield
            finally:
                unlock()

    def _exists(self):
        self._path()
        exists = self.db_path.is_file()
        if (self._seen or self._witness.exists()) and not exists:
            raise _broken()
        self._seen = self._seen or exists
        return exists

    def _now(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise _broken()
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def _schema(self, db, owner, *, create):
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > _VERSION:
            raise PersonalAgentError("unsupported_schema", "记忆资料库来自较新版本，请先更新应用")
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            )
        }
        if create and not tables and version == 0:
            for statement in (*_SCHEMA, *_TRIGGERS):
                db.execute(statement)
            db.execute(
                "INSERT INTO memory_meta VALUES (1,?,?,?,?)",
                (_VERSION, owner, uuid.uuid4().hex, TOKENIZER_VERSION),
            )
            db.execute("INSERT INTO memory_fts(memory_fts,rank) VALUES ('secure-delete',1)")
            db.execute(f"PRAGMA user_version={_VERSION}")
        elif version != _VERSION:
            raise _broken()
        actual = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            )
        }
        if actual != _TABLES:
            raise _broken()
        for statement in _SCHEMA:
            match = re.match(r"CREATE (?:UNIQUE )?(?:VIRTUAL )?(TABLE|INDEX) (\w+)", statement)
            if match is None:
                raise _broken()
            kind, name = match.group(1).lower(), match.group(2)
            actual_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)
            ).fetchone()
            if actual_sql is None or " ".join(actual_sql[0].split()) != " ".join(statement.split()):
                raise _broken()
        if (
            db.execute(
                "SELECT 1 FROM memory_heads WHERE "
                "memory_id IS NOT json_extract(record_json,'$.memory_id') "
                "OR owner_key IS NOT json_extract(record_json,'$.owner_key') "
                "OR actor_id IS NOT json_extract(record_json,'$.actor_id') "
                "OR revision IS NOT json_extract(record_json,'$.revision') "
                "OR state IS NOT json_extract(record_json,'$.state') "
                "OR topic_key IS NOT json_extract(record_json,'$.topic_key') "
                "OR scope_kind IS NOT json_extract(record_json,'$.scope.kind') "
                "OR workspace_key IS NOT json_extract(record_json,'$.scope.workspace_key') "
                "OR item_id IS NOT json_extract(record_json,'$.scope.item_id') LIMIT 1",
            ).fetchone()
            is not None
        ):
            raise _broken()
        rows = db.execute("SELECT * FROM memory_meta").fetchall()
        if len(rows) != 1 or (
            rows[0]["schema_version"] != _VERSION
            or rows[0]["owner_key"] != owner
            or rows[0]["tokenizer_version"] != TOKENIZER_VERSION
            or not re_instance(rows[0]["instance_id"])
        ):
            raise _broken()
        triggers = {
            r[0]: r[1]
            for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'",
            )
        }
        if set(triggers) != {f"{t}_no_{a}" for t in _IMMUTABLE for a in ("update", "delete")}:
            raise _broken()
        for sql in _TRIGGERS:
            name = sql.split()[2]
            if " ".join(triggers[name].split()) != " ".join(sql.split()):
                raise _broken()
        return rows[0]["instance_id"]

    @contextmanager
    def _connection(self, owner, *, write=False):
        db = None
        try:
            with self._lock:
                self._exists()
                if write:
                    self.base_dir.mkdir(parents=True, exist_ok=True)
                with self._bootstrap_guard(create=write):
                    # Another process may have completed the authority while we waited.
                    exists = self._exists()
                    if write:
                        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
                    else:
                        db = sqlite3.connect(
                            self.db_path.as_uri() + "?mode=ro",
                            uri=True,
                            timeout=15,
                            isolation_level=None,
                        )
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA busy_timeout=15000")
                    db.execute("PRAGMA foreign_keys=ON")
                    db.execute("PRAGMA secure_delete=ON")
                    if write and not exists:
                        db.execute("PRAGMA journal_mode=WAL")
                    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    instance = self._schema(db, owner, create=write and not exists)
                    if write and not exists:
                        try:
                            with self._witness.open("xb") as stream:
                                stream.write(instance.encode("ascii"))
                                stream.flush()
                                os.fsync(stream.fileno())
                        except FileExistsError:
                            if self._witness.read_text("ascii") != instance:
                                raise _broken() from None
                        db.commit()
                        db.execute("BEGIN IMMEDIATE")
                    if not self._witness.is_file() or self._witness.read_text("ascii") != instance:
                        raise _broken()
                yield db
                db.commit()
                self._seen = True
        except PersonalAgentError:
            if db is not None:
                db.rollback()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            if db is not None:
                db.rollback()
            raise _broken() from exc
        finally:
            if db is not None:
                db.close()

    def identity(self, *, owner_key):
        owner = self._owner(owner_key)
        if not self._exists():
            return None
        with self._connection(owner) as db:
            return db.execute("SELECT instance_id FROM memory_meta").fetchone()[0]

    @staticmethod
    def _decode(raw, checksum):
        try:
            value = object_value(raw, "记忆记录", maximum=1024 * 1024)
        except PersonalAgentError as exc:
            raise _broken() from exc
        if digest(value) != checksum:
            raise _broken()
        return value

    def _record(self, db, owner, memory_id):
        row = db.execute(
            "SELECT * FROM memory_heads WHERE owner_key=? AND memory_id=?", (owner, memory_id)
        ).fetchone()
        if row is None:
            raise PersonalAgentError("not_found", "该记忆不存在")
        value = self._decode(row["record_json"], row["record_sha256"])
        try:
            scope = normalize_scope(value["scope"])
            if (
                value["memory_id"] != memory_id
                or value["owner_key"] != owner
                or value["actor_id"] != row["actor_id"]
                or value["revision"] != row["revision"]
                or value["state"] != row["state"]
                or scope["kind"] != row["scope_kind"]
                or scope["workspace_key"] != row["workspace_key"]
                or scope["item_id"] != row["item_id"]
                or value["topic_key"] != row["topic_key"]
                or value["state"] not in STATES
                or value["category"] not in CATEGORIES
            ):
                raise _broken()
            maximum = db.execute(
                "SELECT MAX(revision),COUNT(*) FROM memory_events WHERE memory_id=?", (memory_id,)
            ).fetchone()
            event_row = db.execute(
                "SELECT * FROM memory_events WHERE memory_id=? AND revision=?",
                (memory_id, value["revision"]),
            ).fetchone()
            if tuple(maximum) != (value["revision"], value["revision"]) or event_row is None:
                raise _broken()
            event = self._decode(event_row["record_json"], event_row["record_sha256"])
            if event["new_record_sha256"] != row["record_sha256"]:
                raise _broken()
            payload = db.execute(
                "SELECT * FROM memory_payloads WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if value["state"] in _LIVE:
                if payload is None or payload["revision"] != value["revision"]:
                    raise _broken()
                body = self._decode(payload["payload_json"], payload["payload_sha256"])
                if set(body) != {"content", "excerpt"}:
                    raise _broken()
                content = text(body["content"], "记忆内容", 8000)
                source = normalize_source({**value["source"], "excerpt": body["excerpt"]})
                if content_digest(content) != value["content_sha256"] or (
                    digest(source) != value["source_sha256"]
                    or not within_ceiling(scope, source["scope_ceiling"])
                ):
                    raise _broken()
                return {**value, "content": content, "source": source}
            if payload is not None or value["source"] is not None:
                raise _broken()
            return {**value, "content": ""}
        except PersonalAgentError as exc:
            if exc.code == "storage_error":
                raise
            raise _broken() from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise _broken() from exc

    @staticmethod
    def _own(record, actor):
        if record["actor_id"] != actor:
            raise PersonalAgentError("not_found", "该记忆不属于当前 Agent")

    def get(self, *, owner_key, memory_id):
        owner = self._owner(owner_key)
        text(memory_id, "记忆编号", 35, pattern=MEMORY_ID)
        if not self._exists():
            raise PersonalAgentError("not_found", "该记忆不存在")
        with self._connection(owner) as db:
            return self._record(db, owner, memory_id)

    def list(
        self, *, owner_key, actor_id=None, scope=None, state=None, query="", limit=100, offset=0
    ):
        owner = self._owner(owner_key)
        if actor_id is not None:
            self._actor(actor_id)
        if scope is not None:
            scope = normalize_scope(scope)
        if state is not None:
            choice(state, STATES, "记忆状态")
        text(query, "记忆查询", 2000, empty=True)
        integer(limit, "页大小", maximum=500)
        integer(offset, "页位置", minimum=0)
        if not self._exists():
            return []
        with self._connection(owner) as db:
            rows = db.execute(
                "SELECT memory_id FROM memory_heads WHERE owner_key=? ORDER BY memory_id", (owner,)
            ).fetchall()
            records = [self._record(db, owner, r[0]) for r in rows]
            selected = [
                v
                for v in records
                if (
                    (actor_id is None or v["actor_id"] == actor_id)
                    and (scope is None or v["scope"] == scope)
                    and (state is None or v["state"] == state)
                    and (
                        not query
                        or query.casefold() in (v["topic"] + "\n" + v["content"]).casefold()
                    )
                )
            ]
            selected.sort(key=lambda v: (v["updated_at"], v["memory_id"]), reverse=True)
            return selected[offset : offset + limit]

    def active_records(self, *, owner_key):
        owner = self._owner(owner_key)
        if not self._exists():
            return []
        with self._connection(owner) as db:
            rows = db.execute(
                "SELECT memory_id FROM memory_heads WHERE owner_key=? AND state='active' "
                "ORDER BY memory_id",
                (owner,),
            ).fetchall()
            return [self._record(db, owner, r[0]) for r in rows]

    @staticmethod
    def _topic_active(db, owner, actor, scope, topic):
        row = db.execute(
            "SELECT memory_id FROM memory_heads WHERE owner_key=? AND actor_id=? AND scope_kind=? "
            "AND workspace_key=? AND item_id=? AND topic_key=? AND state='active'",
            (owner, actor, scope["kind"], scope["workspace_key"], scope["item_id"], topic),
        ).fetchone()
        return row[0] if row else None

    def _human(self, human, operation, request_id):
        value = normalize_human(human)
        if value is None:
            raise PersonalAgentError("human_confirmation_required", "请通过人工记忆面板确认该操作")
        if value.get("action", operation) != operation:
            raise PersonalAgentError("binding_conflict", "人工确认与本次操作不一致")
        if value.get("request_id", request_id) != request_id:
            raise PersonalAgentError("binding_conflict", "人工确认与原请求不一致")
        return {
            **value,
            "actor_kind": "human",
            "action": operation,
            "request_id": request_id,
            "receipt_id": "pmh_" + uuid.uuid4().hex,
            "confirmed_at": self._now(),
        }

    def _save(self, db, record, *, operation, human=None, previous=None):
        source = record.get("source")
        metadata = {k: v for k, v in record.items() if k != "content"}
        if source is not None:
            metadata["source"] = {k: v for k, v in source.items() if k != "excerpt"}
        raw, sha = canonical(metadata), digest(metadata)
        scope = record["scope"]
        db.execute(
            "INSERT INTO memory_heads VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(memory_id) "
            "DO UPDATE SET revision=excluded.revision,state=excluded.state,"
            "topic_key=excluded.topic_key,record_json=excluded.record_json,record_sha256=excluded.record_sha256",
            (
                record["memory_id"],
                record["owner_key"],
                record["actor_id"],
                record["revision"],
                record["state"],
                scope["kind"],
                scope["workspace_key"],
                scope["item_id"],
                record["topic_key"],
                raw,
                sha,
            ),
        )
        if record["state"] in _LIVE:
            payload = {"content": record["content"], "excerpt": source["excerpt"]}
            db.execute(
                "INSERT INTO memory_payloads VALUES (?,?,?,?) ON CONFLICT(memory_id) "
                "DO UPDATE SET revision=excluded.revision,payload_json=excluded.payload_json,"
                "payload_sha256=excluded.payload_sha256",
                (record["memory_id"], record["revision"], canonical(payload), digest(payload)),
            )
        event = {
            "memory_id": record["memory_id"],
            "revision": record["revision"],
            "owner_key": record["owner_key"],
            "actor_id": record["actor_id"],
            "scope": scope,
            "operation": operation,
            "state": record["state"],
            "category": record["category"],
            "topic_sha256": content_digest(record["topic_key"]),
            "content_sha256": record["content_sha256"],
            "source_sha256": record["source_sha256"],
            "source_authority": source["authority"] if source else None,
            "source_kind": source["kind"] if source else None,
            "previous_source_sha256": previous.get("source_sha256", "") if previous else "",
            "previous_record_sha256": digest(
                {
                    k: (
                        {x: y for x, y in v.items() if x != "excerpt"} if k == "source" and v else v
                    )
                    for k, v in previous.items()
                    if k != "content"
                }
            )
            if previous
            else "",
            "new_record_sha256": sha,
            "human": human,
            "occurred_at": record["updated_at"],
        }
        db.execute(
            "INSERT INTO memory_events VALUES (?,?,?,?)",
            (record["memory_id"], record["revision"], canonical(event), digest(event)),
        )
        db.execute("DELETE FROM memory_fts WHERE memory_id=?", (record["memory_id"],))
        if record["state"] == "active":
            tokens = " ".join(tokenize(record["topic"] + "\n" + record["content"]))
            db.execute(
                "INSERT INTO memory_fts(memory_id,revision,tokens) VALUES (?,?,?)",
                (record["memory_id"], record["revision"], tokens),
            )
            self._outbox(db, record, "upsert")

    def _outbox(self, db, record, action):
        event_id = (
            "pmidx_"
            + digest({"id": record["memory_id"], "revision": record["revision"], "action": action})[
                :32
            ]
        )
        value = {
            "event_id": event_id,
            "owner_key": record["owner_key"],
            "memory_id": record["memory_id"],
            "revision": record["revision"],
            "content_sha256": record["content_sha256"],
            "action": action,
            "created_at": self._now(),
            "tokenizer_version": TOKENIZER_VERSION,
        }
        db.execute(
            "INSERT OR IGNORE INTO memory_index_outbox VALUES (?,?,?,?,?,?,?)",
            (
                event_id,
                value["owner_key"],
                value["memory_id"],
                value["revision"],
                action,
                canonical(value),
                digest(value),
            ),
        )

    def _erase(self, db, record):
        db.execute("DELETE FROM memory_payloads WHERE memory_id=?", (record["memory_id"],))
        db.execute("DELETE FROM memory_fts WHERE memory_id=?", (record["memory_id"],))
        self._outbox(db, record, "delete")
        for row in db.execute(
            "SELECT grant_id FROM memory_grants WHERE memory_id=? AND state='active'",
            (record["memory_id"],),
        ).fetchall():
            grant = self._grant(db, record["owner_key"], row[0])
            self._write_grant(
                db,
                {
                    **grant,
                    "revision": grant["revision"] + 1,
                    "state": "revoked",
                    "updated_at": self._now(),
                },
                operation="memory_revision_revoked",
                human=None,
            )
        rows = db.execute(
            "SELECT request_id FROM memory_gui_requests WHERE target_memory_id=? "
            "AND payload_json IS NOT NULL",
            (record["memory_id"],),
        ).fetchall()
        for row in rows:
            prepared = self._prepared(db, record["owner_key"], row[0])
            prepared.update(payload=None, state="erased", updated_at=self._now())
            self._write_prepared(db, prepared)

    def _receipt(self, db, owner, request_id):
        row = db.execute(
            "SELECT * FROM memory_requests WHERE owner_key=? AND request_id=?", (owner, request_id)
        ).fetchone()
        if row is None:
            return None
        value = self._decode(row["record_json"], row["record_sha256"])
        if any(
            value[key] != row[key]
            for key in (
                "owner_key",
                "request_id",
                "actor_id",
                "operation",
                "payload_sha256",
                "result_kind",
                "result_id",
                "memory_id",
                "committed_revision",
            )
        ):
            raise _broken()
        record = self._record(db, owner, value["memory_id"])
        result = (
            self._grant(db, owner, value["result_id"])
            if value["result_kind"] == "grant"
            else record
        )
        return {**value, "record": record, "result": result}

    def find_request(self, *, owner_key, request_id):
        owner = self._owner(owner_key)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        if not self._exists():
            return None
        with self._connection(owner) as db:
            return self._receipt(db, owner, request_id)

    def _replay(self, db, owner, request_id, actor, operation, payload):
        old = self._receipt(db, owner, request_id)
        if old is not None:
            if (old["actor_id"], old["operation"], old["payload_sha256"]) != (
                actor,
                operation,
                digest(payload),
            ):
                raise PersonalAgentError("idempotency_conflict", "该记忆请求编号已用于另一项操作")
            return old["result"]
        row = db.execute(
            "SELECT 1 FROM memory_gui_requests WHERE owner_key=? AND request_id=?",
            (owner, request_id),
        ).fetchone()
        if row is not None:
            prepared = self._prepared(db, owner, request_id)
            if prepared["actor_id"] != actor or not self._operation_matches(
                prepared["operation"],
                operation,
            ):
                raise PersonalAgentError("idempotency_conflict", "原记忆请求已绑定其他主体或操作")
            if prepared["state"] == "erased":
                raise PersonalAgentError(
                    "request_discarded",
                    "原记忆草稿已丢弃，请明确创建新的请求",
                )
            if prepared["state"] == "applied":
                raise _broken()
        return None

    def _save_receipt(
        self, db, owner, request_id, actor, operation, payload, result, *, grant=False
    ):
        prepared = db.execute(
            "SELECT actor_id,operation,target_memory_id FROM memory_gui_requests "
            "WHERE owner_key=? AND request_id=?",
            (owner, request_id),
        ).fetchone()
        if prepared is not None and (
            prepared["actor_id"] != actor
            or not self._operation_matches(prepared["operation"], operation)
            or prepared["target_memory_id"] not in {"", result["memory_id"]}
        ):
            raise PersonalAgentError("idempotency_conflict", "原记忆请求已绑定其他主体或操作")
        value = {
            "owner_key": owner,
            "request_id": request_id,
            "actor_id": actor,
            "operation": operation,
            "payload_sha256": digest(payload),
            "result_kind": "grant" if grant else "memory",
            "result_id": result["grant_id"] if grant else result["memory_id"],
            "memory_id": result["memory_id"],
            "committed_revision": result["revision"],
            "created_at": self._now(),
        }
        db.execute(
            "INSERT INTO memory_requests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                *[
                    value[k]
                    for k in (
                        "owner_key",
                        "request_id",
                        "actor_id",
                        "operation",
                        "payload_sha256",
                        "result_kind",
                        "result_id",
                        "memory_id",
                        "committed_revision",
                    )
                ],
                canonical(value),
                digest(value),
            ),
        )

    def create(
        self,
        *,
        owner_key,
        actor_id,
        request_id,
        scope,
        topic,
        category,
        content,
        source,
        human=None,
    ):
        owner, actor = self._owner(owner_key), self._actor(actor_id)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        scope, source = normalize_scope(scope), normalize_source(source)
        topic = text(topic, "记忆主题", 120).strip()
        key = topic_key(topic)
        choice(category, CATEGORIES, "记忆类别")
        text(content, "记忆内容", 8000)
        if not within_ceiling(scope, source["scope_ceiling"]):
            raise PersonalAgentError("scope_denied", "原来源范围不支持保存到所选记忆范围")
        payload = {
            "scope": scope,
            "topic": topic,
            "category": category,
            "content": content,
            "source": source_semantics(source),
            "human": human_semantics(human),
        }
        with self._connection(owner, write=True) as db:
            replay = self._replay(db, owner, request_id, actor, "create", payload)
            if replay is not None:
                return replay
            now = self._now()
            active = self._topic_active(db, owner, actor, scope, key)
            confirmation = self._human(human, "create", request_id) if human else None
            state = "active" if confirmation and active is None else "proposed"
            record = {
                "memory_id": memory_id_for_request(owner, request_id),
                "owner_key": owner,
                "actor_id": actor,
                "scope": scope,
                "topic": topic,
                "topic_key": key,
                "category": category,
                "revision": 1,
                "state": state,
                "content": content,
                "content_sha256": content_digest(content),
                "source": source,
                "source_sha256": digest(source),
                "confirmation": confirmation if state == "active" else None,
                "created_at": now,
                "updated_at": now,
            }
            self._save(db, record, operation="create", human=confirmation)
            self._save_receipt(db, owner, request_id, actor, "create", payload, record)
            return self._record(db, owner, record["memory_id"])

    def _cas(self, record, actor, revision, checksum=None):
        self._own(record, actor)
        integer(revision, "记忆修订")
        if record["revision"] != revision:
            raise PersonalAgentError("version_conflict", "记忆已更新，请刷新后再操作")
        if checksum is not None:
            text(checksum, "记忆摘要", 64, pattern=SHA256)
            if record["content_sha256"] != checksum:
                raise PersonalAgentError("version_conflict", "记忆正文已变化，请核对原版本")

    def confirm(
        self,
        *,
        owner_key,
        actor_id,
        memory_id,
        expected_revision,
        expected_content_sha256,
        expected_active_ref,
        request_id,
        human,
    ):
        owner, actor = self._owner(owner_key), self._actor(actor_id)
        text(memory_id, "记忆编号", 35, pattern=MEMORY_ID)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        expected = None if expected_active_ref is None else normalize_ref(expected_active_ref)
        payload = {
            "memory_id": memory_id,
            "expected_revision": expected_revision,
            "expected_content_sha256": expected_content_sha256,
            "expected_active_ref": expected,
            "human": human_semantics(human),
        }
        with self._connection(owner, write=True) as db:
            replay = self._replay(db, owner, request_id, actor, "confirm", payload)
            if replay is not None:
                return replay
            record = self._record(db, owner, memory_id)
            self._cas(record, actor, expected_revision, expected_content_sha256)
            if record["state"] != "proposed":
                raise PersonalAgentError("lifecycle_conflict", "只有待确认记忆可进行确认")
            receipt = self._human(human, "confirm", request_id)
            active_id = self._topic_active(db, owner, actor, record["scope"], record["topic_key"])
            if expected is None:
                if active_id is not None:
                    raise PersonalAgentError(
                        "memory_conflict", "该主题已有记忆，请明确选择要替换的版本"
                    )
            else:
                old = self._record(db, owner, expected["memory_id"])
                if (
                    expected["grant_id"]
                    or expected != reference(old)
                    or old["state"] != "active"
                    or (
                        old["actor_id"] != actor
                        or old["scope"] != record["scope"]
                        or active_id not in {None, old["memory_id"]}
                    )
                ):
                    raise PersonalAgentError("version_conflict", "原有效记忆已变化，请重新确认替换")
                self._erase(db, old)
                retired = {
                    **old,
                    "revision": old["revision"] + 1,
                    "state": "superseded",
                    "content": "",
                    "source": None,
                    "confirmation": None,
                    "updated_at": self._now(),
                }
                self._save(db, retired, operation="supersede", human=receipt, previous=old)
            updated = {
                **record,
                "revision": record["revision"] + 1,
                "state": "active",
                "confirmation": receipt,
                "updated_at": self._now(),
            }
            self._save(db, updated, operation="confirm", human=receipt, previous=record)
            self._save_receipt(db, owner, request_id, actor, "confirm", payload, updated)
            return self._record(db, owner, memory_id)

    def revise(
        self,
        *,
        owner_key,
        actor_id,
        memory_id,
        expected_revision,
        expected_content_sha256,
        content,
        source,
        request_id,
        human,
    ):
        owner, actor = self._owner(owner_key), self._actor(actor_id)
        text(memory_id, "记忆编号", 35, pattern=MEMORY_ID)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        text(content, "记忆内容", 8000)
        source = normalize_source(source)
        if source["kind"] != "manual" or source["authority"] != "user":
            raise PersonalAgentError("scope_denied", "人工纠正请保存实际输入的新内容")
        payload = {
            "memory_id": memory_id,
            "expected_revision": expected_revision,
            "expected_content_sha256": expected_content_sha256,
            "content": content,
            "source": source_semantics(source),
            "human": human_semantics(human),
        }
        with self._connection(owner, write=True) as db:
            replay = self._replay(db, owner, request_id, actor, "revise", payload)
            if replay is not None:
                return replay
            record = self._record(db, owner, memory_id)
            self._cas(record, actor, expected_revision, expected_content_sha256)
            if record["state"] not in _LIVE:
                raise PersonalAgentError("lifecycle_conflict", "该记忆已结束，请明确创建新记忆")
            if not within_ceiling(record["scope"], source["scope_ceiling"]):
                raise PersonalAgentError("scope_denied", "纠正来源与原记忆范围不一致")
            receipt = self._human(human, "revise", request_id)
            self._erase(db, record)
            updated = {
                **record,
                "revision": record["revision"] + 1,
                "content": content,
                "content_sha256": content_digest(content),
                "source": source,
                "source_sha256": digest(source),
                "updated_at": self._now(),
                "confirmation": receipt if record["state"] == "active" else None,
            }
            self._save(db, updated, operation="revise", human=receipt, previous=record)
            self._save_receipt(db, owner, request_id, actor, "revise", payload, updated)
            return self._record(db, owner, memory_id)

    def control(
        self, *, owner_key, actor_id, memory_id, expected_revision, request_id, action, human
    ):
        owner, actor = self._owner(owner_key), self._actor(actor_id)
        text(memory_id, "记忆编号", 35, pattern=MEMORY_ID)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        choice(action, {"delete", "reject"}, "记忆操作")
        payload = {
            "memory_id": memory_id,
            "expected_revision": expected_revision,
            "action": action,
            "human": human_semantics(human),
        }
        with self._connection(owner, write=True) as db:
            replay = self._replay(db, owner, request_id, actor, action, payload)
            if replay is not None:
                return replay
            record = self._record(db, owner, memory_id)
            self._cas(record, actor, expected_revision)
            receipt = self._human(human, action, request_id)
            if action == "reject" and record["state"] != "proposed":
                raise PersonalAgentError("lifecycle_conflict", "只有待确认记忆可以拒绝")
            if record["state"] == "deleted":
                updated = record
            else:
                self._erase(db, record)
                updated = {
                    **record,
                    "revision": record["revision"] + 1,
                    "state": "deleted" if action == "delete" else "rejected",
                    "content": "",
                    "source": None,
                    "confirmation": None,
                    "topic": "" if action == "delete" else record["topic"],
                    "topic_key": "" if action == "delete" else record["topic_key"],
                    "updated_at": self._now(),
                }
                self._save(db, updated, operation=action, human=receipt, previous=record)
            self._save_receipt(db, owner, request_id, actor, action, payload, updated)
            return self._record(db, owner, memory_id)

    def history(self, *, owner_key, memory_id):
        owner = self._owner(owner_key)
        text(memory_id, "记忆编号", 35, pattern=MEMORY_ID)
        if not self._exists():
            raise PersonalAgentError("not_found", "该记忆不存在")
        with self._connection(owner) as db:
            record = self._record(db, owner, memory_id)
            rows = db.execute(
                "SELECT * FROM memory_events WHERE memory_id=? ORDER BY revision", (memory_id,)
            ).fetchall()
            result = []
            previous = ""
            for number, row in enumerate(rows, 1):
                event = self._decode(row["record_json"], row["record_sha256"])
                if (
                    event["revision"] != number
                    or event["memory_id"] != memory_id
                    or (event["previous_record_sha256"] != previous)
                ):
                    raise _broken()
                previous = event["new_record_sha256"]
                result.append(event)
            if len(result) != record["revision"]:
                raise _broken()
            return result

    def _grant(self, db, owner, grant_id):
        row = db.execute(
            "SELECT * FROM memory_grants WHERE owner_key=? AND grant_id=?", (owner, grant_id)
        ).fetchone()
        if row is None:
            raise PersonalAgentError("not_found", "该记忆分享不存在")
        value = self._decode(row["record_json"], row["record_sha256"])
        if any(
            value[k] != row[k]
            for k in (
                "grant_id",
                "owner_key",
                "memory_id",
                "recipient_actor_id",
                "revision",
                "state",
            )
        ):
            raise _broken()
        event = db.execute(
            "SELECT * FROM memory_grant_events WHERE grant_id=? ORDER BY revision DESC LIMIT 1",
            (grant_id,),
        ).fetchone()
        if event is None:
            raise _broken()
        receipt = self._decode(event["record_json"], event["record_sha256"])
        if (
            receipt["grant_sha256"] != row["record_sha256"]
            or event["revision"] != value["revision"]
        ):
            raise _broken()
        return value

    def _write_grant(self, db, grant, *, operation, human):
        db.execute(
            "INSERT INTO memory_grants VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(grant_id) "
            "DO UPDATE SET revision=excluded.revision,state=excluded.state,"
            "record_json=excluded.record_json,record_sha256=excluded.record_sha256",
            (
                grant["grant_id"],
                grant["owner_key"],
                grant["memory_id"],
                grant["recipient_actor_id"],
                grant["revision"],
                grant["state"],
                canonical(grant),
                digest(grant),
            ),
        )
        event = {
            "grant_id": grant["grant_id"],
            "revision": grant["revision"],
            "memory_id": grant["memory_id"],
            "operation": operation,
            "grant_sha256": digest(grant),
            "state": grant["state"],
            "human": human,
            "occurred_at": grant["updated_at"],
        }
        db.execute(
            "INSERT INTO memory_grant_events VALUES (?,?,?,?)",
            (grant["grant_id"], grant["revision"], canonical(event), digest(event)),
        )

    def share(
        self,
        *,
        owner_key,
        actor_id,
        memory_id,
        expected_revision,
        expected_content_sha256,
        request_id,
        recipient_actor_id,
        recipient_version,
        source_refs,
        human,
    ):
        owner, actor = self._owner(owner_key), self._actor(actor_id)
        recipient = self._actor(recipient_actor_id)
        integer(recipient_version, "接收 Agent 版本", minimum=0)
        if actor == recipient or (recipient == "main") != (recipient_version == 0):
            raise PersonalAgentError("invalid_input", "请选择其他真实 Agent 及其确切版本")
        text(memory_id, "记忆编号", 35, pattern=MEMORY_ID)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        refs = normalize_refs(source_refs)
        payload = {
            "memory_id": memory_id,
            "expected_revision": expected_revision,
            "expected_content_sha256": expected_content_sha256,
            "recipient_actor_id": recipient,
            "recipient_version": recipient_version,
            "source_refs": refs,
            "human": human_semantics(human),
        }
        with self._connection(owner, write=True) as db:
            replay = self._replay(db, owner, request_id, actor, "share", payload)
            if replay is not None:
                return replay
            record = self._record(db, owner, memory_id)
            self._cas(record, actor, expected_revision, expected_content_sha256)
            if record["state"] != "active":
                raise PersonalAgentError("lifecycle_conflict", "只有当前已确认记忆可以分享")

            def identity(ref):
                return ref["artifact_id"], ref["version"], ref["sha256"]

            if {identity(r) for r in refs} != {
                identity(r) for r in record["source"]["artifact_refs"]
            }:
                raise PersonalAgentError("binding_conflict", "分享缺少原记忆来源的确切授权")
            receipt = self._human(human, "share", request_id)
            old = db.execute(
                "SELECT grant_id FROM memory_grants WHERE owner_key=? AND memory_id=? "
                "AND recipient_actor_id=? AND state='active'",
                (owner, memory_id, recipient),
            ).fetchone()
            if old is not None:
                grant = self._grant(db, owner, old[0])
                if (
                    grant["memory_revision"],
                    grant["content_sha256"],
                    grant["recipient_version"],
                    grant["source_refs"],
                ) != (expected_revision, expected_content_sha256, recipient_version, refs):
                    raise PersonalAgentError(
                        "version_conflict", "已有分享与当前版本不一致，请先撤销"
                    )
            else:
                now = self._now()
                grant = {
                    "grant_id": "pmg_" + uuid.uuid4().hex,
                    "owner_key": owner,
                    "actor_id": actor,
                    "memory_id": memory_id,
                    "memory_revision": record["revision"],
                    "content_sha256": record["content_sha256"],
                    "recipient_actor_id": recipient,
                    "recipient_version": recipient_version,
                    "revision": 1,
                    "state": "active",
                    "source_refs": refs,
                    "created_at": now,
                    "updated_at": now,
                }
                self._write_grant(db, grant, operation="share", human=receipt)
            self._save_receipt(db, owner, request_id, actor, "share", payload, grant, grant=True)
            return grant

    def revoke(self, *, owner_key, actor_id, grant_id, expected_revision, request_id, human):
        owner, actor = self._owner(owner_key), self._actor(actor_id)
        text(grant_id, "分享编号", 36, pattern=GRANT_ID)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        integer(expected_revision, "分享版本")
        payload = {
            "grant_id": grant_id,
            "expected_revision": expected_revision,
            "human": human_semantics(human),
        }
        with self._connection(owner, write=True) as db:
            replay = self._replay(db, owner, request_id, actor, "revoke", payload)
            if replay is not None:
                return replay
            grant = self._grant(db, owner, grant_id)
            if grant["actor_id"] != actor:
                raise PersonalAgentError("not_found", "该记忆分享不属于当前 Agent")
            if grant["revision"] != expected_revision:
                raise PersonalAgentError("version_conflict", "记忆分享已更新，请刷新后再撤销")
            receipt = self._human(human, "revoke", request_id)
            if grant["state"] == "active":
                grant = {
                    **grant,
                    "revision": grant["revision"] + 1,
                    "state": "revoked",
                    "updated_at": self._now(),
                }
                self._write_grant(db, grant, operation="revoke", human=receipt)
            self._save_receipt(db, owner, request_id, actor, "revoke", payload, grant, grant=True)
            return grant

    def grants(self, *, owner_key, memory_id):
        owner = self._owner(owner_key)
        text(memory_id, "记忆编号", 35, pattern=MEMORY_ID)
        if not self._exists():
            return []
        with self._connection(owner) as db:
            self._record(db, owner, memory_id)
            rows = db.execute(
                "SELECT grant_id FROM memory_grants WHERE owner_key=? AND memory_id=? "
                "ORDER BY grant_id",
                (owner, memory_id),
            ).fetchall()
            return [self._grant(db, owner, r[0]) for r in rows]

    def visible_grants(self, *, owner_key, recipient_actor_id):
        owner, recipient = self._owner(owner_key), self._actor(recipient_actor_id)
        if not self._exists():
            return []
        with self._connection(owner) as db:
            rows = db.execute(
                "SELECT grant_id FROM memory_grants WHERE owner_key=? "
                "AND recipient_actor_id=? AND state='active' ORDER BY grant_id",
                (owner, recipient),
            ).fetchall()
            return [self._grant(db, owner, r[0]) for r in rows]

    def authorize_ref(self, *, owner_key, actor_id, ref):
        owner, actor = self._owner(owner_key), self._actor(actor_id)
        ref = normalize_ref(ref)
        if not self._exists():
            raise PersonalAgentError("memory_access_revoked", "原记忆版本或分享已失效")
        with self._connection(owner) as db:
            record = self._record(db, owner, ref["memory_id"])
            if record["state"] != "active" or (record["revision"], record["content_sha256"]) != (
                ref["revision"],
                ref["content_sha256"],
            ):
                raise PersonalAgentError("memory_access_revoked", "原记忆已纠正、删除或撤销")
            if not ref["grant_id"]:
                if record["actor_id"] != actor:
                    raise PersonalAgentError("memory_access_revoked", "当前 Agent 未获该记忆授权")
                return {**record, "access_grant": None}
            grant = self._grant(db, owner, ref["grant_id"])
            if (
                grant["memory_id"],
                grant["memory_revision"],
                grant["content_sha256"],
                grant["recipient_actor_id"],
                grant["revision"],
                grant["state"],
            ) != (
                record["memory_id"],
                record["revision"],
                record["content_sha256"],
                actor,
                ref["grant_revision"],
                "active",
            ):
                raise PersonalAgentError("memory_access_revoked", "原记忆分享已失效，请核对原授权")
            return {**record, "access_grant": grant}

    def lexical_search(
        self, *, owner_key, query, actor_id=None, scopes=None, memory_ids=None, limit=100
    ):
        owner = self._owner(owner_key)
        if actor_id is not None:
            self._actor(actor_id)
        integer(limit, "检索数量", maximum=1000)
        if scopes is not None:
            if not isinstance(scopes, list) or len(scopes) > 64:
                raise PersonalAgentError("invalid_input", "检索范围数量不正确")
            scopes = [normalize_scope(s) for s in scopes]
        if memory_ids is not None:
            if not isinstance(memory_ids, (list, tuple, set)):
                raise PersonalAgentError("invalid_input", "检索候选类型不正确")
            for identity in memory_ids:
                text(identity, "记忆编号", 35, pattern=MEMORY_ID)
        expression = lexical_query(query)
        if not expression or not self._exists() or scopes == [] or memory_ids == []:
            return []
        with self._connection(owner) as db:
            rows = db.execute(
                "SELECT memory_fts.memory_id,memory_fts.revision,bm25(memory_fts) AS score "
                "FROM memory_fts JOIN memory_heads h ON h.memory_id=memory_fts.memory_id "
                "AND h.revision=memory_fts.revision WHERE memory_fts MATCH ? "
                "AND h.owner_key=? AND h.state='active' ORDER BY score,memory_fts.memory_id",
                (expression, owner),
            ).fetchall()
            result = []
            for row in rows:
                record = self._record(db, owner, row["memory_id"])
                if (
                    (actor_id is not None and record["actor_id"] != actor_id)
                    or (scopes is not None and record["scope"] not in scopes)
                    or memory_ids is not None
                    and record["memory_id"] not in memory_ids
                ):
                    continue
                result.append(
                    {
                        **record,
                        "lexical_score": float(row["score"]),
                        "tokenizer_version": TOKENIZER_VERSION,
                    }
                )
                if len(result) >= limit:
                    break
            return result

    search = lexical_search

    def pending_index(self, *, owner_key, limit=100):
        owner = self._owner(owner_key)
        integer(limit, "索引批量", maximum=1000)
        if not self._exists():
            return []
        with self._connection(owner) as db:
            rows = db.execute(
                "SELECT o.* FROM memory_index_outbox o LEFT JOIN memory_index_acks a "
                "ON a.event_id=o.event_id WHERE o.owner_key=? AND a.event_id IS NULL "
                "ORDER BY o.rowid LIMIT ?",
                (owner, limit),
            ).fetchall()
            return [self._decode(r["record_json"], r["record_sha256"]) for r in rows]

    def ack_index(self, *, owner_key, event_id):
        owner = self._owner(owner_key)
        text(event_id, "索引事件", 38, pattern=INDEX_ID)
        with self._connection(owner, write=True) as db:
            row = db.execute(
                "SELECT * FROM memory_index_outbox WHERE owner_key=? AND event_id=?",
                (owner, event_id),
            ).fetchone()
            if row is None:
                raise PersonalAgentError("not_found", "原索引事件不存在")
            event = self._decode(row["record_json"], row["record_sha256"])
            prior = db.execute(
                "SELECT * FROM memory_index_acks WHERE event_id=?", (event_id,)
            ).fetchone()
            if prior is not None:
                receipt = self._decode(prior["record_json"], prior["record_sha256"])
                if receipt["event_sha256"] != digest(event):
                    raise _broken()
                return
            receipt = {
                "event_id": event_id,
                "event_sha256": digest(event),
                "acknowledged_at": self._now(),
            }
            db.execute(
                "INSERT INTO memory_index_acks VALUES (?,?,?)",
                (event_id, canonical(receipt), digest(receipt)),
            )

    @staticmethod
    def _result_ids(result):
        result = object_value(result, "记忆操作结果", maximum=16384)
        allowed = {
            "memory_id",
            "grant_id",
            "revision",
            "state",
            "ref",
            "active_ref",
            "grant_revision",
            "content_sha256",
            "type",
            "operation",
            "receipt_id",
            "conflict",
        }
        if set(result) - allowed:
            raise PersonalAgentError("invalid_input", "持久请求结果只保存对象身份，不保存正文")
        for name, pattern, maximum in (
            ("memory_id", MEMORY_ID, 35),
            ("grant_id", GRANT_ID, 36),
            ("content_sha256", SHA256, 64),
        ):
            if name in result:
                text(result[name], "请求结果身份", maximum, pattern=pattern)
        for name in {"revision", "grant_revision"} & result.keys():
            integer(result[name], "结果版本", minimum=0 if name == "grant_revision" else 1)
        if "state" in result:
            choice(result["state"], STATES | {"revoked"}, "结果状态")
        if "type" in result:
            choice(
                result["type"],
                {"memory", "memory_grant", "personal_memory", "personal_memory_ref"},
                "结果类型",
            )
        for name in {"operation", "receipt_id"} & result.keys():
            text(result[name], "结果操作标识", 160, pattern=REQUEST_ID)
        if "conflict" in result and type(result["conflict"]) is not bool:
            raise PersonalAgentError("invalid_input", "结果冲突状态必须是布尔值")
        for name in ("ref", "active_ref"):
            if result.get(name) is not None:
                result[name] = normalize_ref(result[name])
        return result

    def _prepared(self, db, owner, request_id):
        row = db.execute(
            "SELECT * FROM memory_gui_requests WHERE owner_key=? AND request_id=?",
            (owner, request_id),
        ).fetchone()
        if row is None:
            raise PersonalAgentError("not_found", "原记忆请求不存在")
        raw = dict(row)
        checksum = raw.pop("record_sha256")
        if digest(raw) != checksum:
            raise _broken()
        payload = (
            None if raw["payload_json"] is None else object_value(raw["payload_json"], "原记忆请求")
        )
        if payload is not None and digest(payload) != raw["payload_sha256"]:
            raise _broken()
        if payload is not None and raw["fingerprint"] != request_fingerprint(
            raw["actor_id"],
            raw["workspace_key"],
            raw["operation"],
            payload,
        ):
            raise _broken()
        if raw["state"] not in {"prepared", "applied", "erased"} or (
            raw["state"] == "prepared"
            and payload is None
            or raw["state"] != "prepared"
            and payload is not None
        ):
            raise _broken()
        result = None if raw["result_json"] is None else self._result_ids(raw["result_json"])
        return {k: v for k, v in raw.items() if k not in {"payload_json", "result_json"}} | {
            "payload": payload,
            "result": result,
        }

    def _write_prepared(self, db, value):
        fields = {k: v for k, v in value.items() if k not in {"payload", "result"}}
        fields["payload_json"] = None if value["payload"] is None else canonical(value["payload"])
        fields["result_json"] = None if value["result"] is None else canonical(value["result"])
        order = (
            "owner_key",
            "request_id",
            "actor_id",
            "workspace_key",
            "operation",
            "fingerprint",
            "target_memory_id",
            "payload_json",
            "payload_sha256",
            "result_json",
            "state",
            "created_at",
            "updated_at",
        )
        db.execute(
            "INSERT INTO memory_gui_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(owner_key,request_id) DO UPDATE SET payload_json=excluded.payload_json,"
            "result_json=excluded.result_json,state=excluded.state,updated_at=excluded.updated_at,"
            "record_sha256=excluded.record_sha256",
            (*[fields[k] for k in order], digest(fields)),
        )

    @staticmethod
    def _prepared_public(value):
        return {k: v for k, v in value.items() if k != "payload"}

    @staticmethod
    def _operation_matches(prepared, mutation):
        return (
            prepared == mutation
            or mutation == "create"
            and prepared in {"propose", "import_legacy"}
        )

    def prepare_request(
        self, *, owner_key, request_id, actor_id, workspace_key, operation, payload, fingerprint
    ):
        owner, actor = self._owner(owner_key), self._actor(actor_id)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        text(workspace_key, "工作区标识", 64, pattern=SHA256)
        text(operation, "记忆操作", 64)
        text(fingerprint, "请求摘要", 64, pattern=SHA256)
        payload = object_value(payload, "原记忆请求")
        if payload.get(
            "request_id", request_id
        ) != request_id or fingerprint != request_fingerprint(
            actor,
            workspace_key,
            operation,
            payload,
        ):
            raise PersonalAgentError("binding_conflict", "原记忆请求与身份摘要不一致")
        target = payload.get("memory_id", "")
        if target:
            text(target, "目标记忆编号", 35, pattern=MEMORY_ID)
        elif operation in {"create", "propose", "import_legacy"}:
            target = memory_id_for_request(owner, request_id)
        with self._connection(owner, write=True) as db:
            row = db.execute(
                "SELECT 1 FROM memory_gui_requests WHERE owner_key=? AND request_id=?",
                (owner, request_id),
            ).fetchone()
            if row:
                existing = self._prepared(db, owner, request_id)
                if (
                    existing["actor_id"],
                    existing["workspace_key"],
                    existing["operation"],
                    existing["fingerprint"],
                ) != (actor, workspace_key, operation, fingerprint):
                    raise PersonalAgentError(
                        "idempotency_conflict", "该记忆请求编号已用于另一项操作"
                    )
                return self._prepared_public(existing)
            if (
                db.execute(
                    "SELECT 1 FROM memory_requests WHERE owner_key=? AND request_id=?",
                    (owner, request_id),
                ).fetchone()
                is not None
            ):
                raise PersonalAgentError(
                    "idempotency_conflict", "该请求编号已由其他入口使用，请核对原结果"
                )
            now = self._now()
            value = {
                "owner_key": owner,
                "request_id": request_id,
                "actor_id": actor,
                "workspace_key": workspace_key,
                "operation": operation,
                "fingerprint": fingerprint,
                "target_memory_id": target,
                "payload": payload,
                "payload_sha256": digest(payload),
                "result": None,
                "state": "prepared",
                "created_at": now,
                "updated_at": now,
            }
            self._write_prepared(db, value)
            return self._prepared_public(value)

    def get_prepared_request(self, *, owner_key, request_id):
        owner = self._owner(owner_key)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        if not self._exists():
            raise PersonalAgentError("not_found", "原记忆请求不存在")
        with self._connection(owner) as db:
            return self._prepared(db, owner, request_id)

    def finish_request(self, *, owner_key, request_id, result):
        owner = self._owner(owner_key)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        result = self._result_ids(result)
        with self._connection(owner, write=True) as db:
            value = self._prepared(db, owner, request_id)
            receipt = self._receipt(db, owner, request_id)
            if receipt is None:
                raise PersonalAgentError("binding_conflict", "记忆请求尚未得到真实操作回执")
            if value["actor_id"] != receipt["actor_id"] or not self._operation_matches(
                value["operation"],
                receipt["operation"],
            ):
                raise PersonalAgentError("binding_conflict", "记忆请求与真实操作主体不一致")
            if result.get("memory_id", receipt["memory_id"]) != receipt["memory_id"]:
                raise PersonalAgentError("binding_conflict", "记忆请求结果与原操作不一致")
            if result.get("grant_id", receipt["result_id"]) != receipt["result_id"]:
                raise PersonalAgentError("binding_conflict", "记忆请求分享结果与原操作不一致")
            if value["state"] == "applied":
                if value["result"] != result:
                    raise PersonalAgentError("idempotency_conflict", "该请求已绑定其他操作结果")
                return self._prepared_public(value)
            value.update(payload=None, result=result, state="applied", updated_at=self._now())
            self._write_prepared(db, value)
            return self._prepared_public(value)

    def discard_request(self, *, owner_key, request_id, expected_fingerprint):
        """Erase an unapplied draft or release its body, never cancel committed truth."""
        owner = self._owner(owner_key)
        text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        text(expected_fingerprint, "原请求摘要", 64, pattern=SHA256)
        if not self._exists():
            raise PersonalAgentError("not_found", "原记忆请求不存在")
        with self._connection(owner, write=True) as db:
            value = self._prepared(db, owner, request_id)
            if value["fingerprint"] != expected_fingerprint:
                raise PersonalAgentError("version_conflict", "原记忆请求已变化，请核对原请求")
            receipt = self._receipt(db, owner, request_id)
            if receipt is not None:
                if (
                    value["actor_id"] != receipt["actor_id"]
                    or not self._operation_matches(
                        value["operation"],
                        receipt["operation"],
                    )
                    or value["target_memory_id"] not in {"", receipt["memory_id"]}
                ):
                    raise PersonalAgentError("binding_conflict", "原草稿与已提交记忆的主体不一致")
                if value["state"] == "applied" and value["payload"] is None:
                    return self._prepared_public(value)
                result = {"memory_id": receipt["memory_id"]}
                if receipt["result_kind"] == "grant":
                    result["grant_id"] = receipt["result_id"]
                value.update(payload=None, state="applied", result=result, updated_at=self._now())
            elif value["state"] == "applied":
                raise _broken()
            elif value["state"] == "erased":
                return self._prepared_public(value)
            else:
                value.update(payload=None, state="erased", updated_at=self._now())
            self._write_prepared(db, value)
            return self._prepared_public(value)


def re_instance(value):
    return (
        isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)
    )
