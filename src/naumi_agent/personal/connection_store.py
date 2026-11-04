"""Owner-bound, hash-checked connection facts and short fenced SQLite transactions."""

from __future__ import annotations

import errno
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from naumi_agent.personal.connection_models import (
    KINDS,
    REQUEST,
    SHA,
    canonical,
    digest,
    integer,
    text,
)
from naumi_agent.personal.models import PersonalAgentError

_SCHEMA = (
    """CREATE TABLE connection_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner_key TEXT NOT NULL,
        instance_id TEXT NOT NULL, schema_version INTEGER NOT NULL)""",
    """CREATE TABLE connection_records (
        kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
        parent_id TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,
        PRIMARY KEY(kind,id))""",
    """CREATE TABLE connection_history (
        kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
        payload TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(kind,id,revision))""",
    """CREATE TABLE connection_requests (
        request_id TEXT PRIMARY KEY,operation TEXT NOT NULL,fingerprint TEXT NOT NULL,
        result TEXT NOT NULL,sha256 TEXT NOT NULL)""",
    """CREATE TABLE connection_leases (
        scope TEXT NOT NULL,id TEXT NOT NULL,fence INTEGER NOT NULL CHECK(fence>0),
        holder TEXT NOT NULL,expires_at REAL NOT NULL,phase TEXT NOT NULL,PRIMARY KEY(scope,id))""",
    "CREATE INDEX connection_parent ON connection_records(kind,parent_id,status,id)",
)
_TABLES = {
    "connection_meta",
    "connection_records",
    "connection_history",
    "connection_requests",
    "connection_leases",
}
_TRIGGERS = {
    f"{table}_immutable_{operation.lower()}": (
        f"CREATE TRIGGER {table}_immutable_{operation.lower()} "
        f"BEFORE {operation} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable connection fact'); END"
    )
    for table in ("connection_meta", "connection_history", "connection_requests")
    for operation in ("UPDATE", "DELETE")
}


def schema_sql(value):
    return " ".join(value.split()).rstrip(";").casefold()


def broken():
    return PersonalAgentError("storage_error", "账户连接资料校验未通过，请恢复原资料库后继续")


def busy():
    return PersonalAgentError("connection_busy", "账户连接正在处理其他操作，请稍后核对原请求")


def ordinary(path):
    raw = str(path)
    return Path(raw[4:]) if os.name == "nt" and raw.startswith("\\\\?\\") else Path(path)


def checked_path(path):
    path = ordinary(Path(path).absolute())
    if os.path.normcase(str(path)) != os.path.normcase(str(ordinary(path.resolve()))):
        raise broken()
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise broken()
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise broken()
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise broken()
    return path


class ConnectionTransaction:
    def __init__(self, db):
        self.db = db

    def _decode(self, row):
        try:
            value = json.loads(row["payload"])
            if (
                digest({"kind": row["kind"], "value": value}) != row["sha256"]
                or value["id"] != row["id"]
                or value["revision"] != row["revision"]
            ):
                raise broken()
            integer(value["revision"], "连接记录版本")
            return value
        except (ValueError, KeyError, TypeError):
            raise broken() from None

    def get(self, kind, identity, *, revision=None, missing=False):
        if kind not in KINDS:
            raise broken()
        if revision is not None:
            integer(revision, "连接记录版本")
        table = "connection_records" if revision is None else "connection_history"
        query = f"SELECT * FROM {table} WHERE kind=? AND id=?"
        params = [kind, identity]
        if revision is not None:
            query += " AND revision=?"
            params.append(revision)
        row = self.db.execute(query, params).fetchone()
        if row is None:
            if revision is not None:
                self.get(kind, identity, missing=True)
            if (
                revision is None
                and self.db.execute(
                    "SELECT 1 FROM connection_history WHERE kind=? AND id=? LIMIT 1",
                    (kind, identity),
                ).fetchone()
            ):
                raise broken()
            if missing:
                return None
            raise PersonalAgentError("not_found", "该连接记录不存在")
        value = self._decode(row)
        if revision is None and (
            value.get("connection_id", "") != row["parent_id"]
            or value.get("status", "") != row["status"]
        ):
            raise broken()
        if revision is None:
            count, first, last, integers = self.db.execute(
                "SELECT count(*),min(revision),max(revision),"
                "sum(typeof(revision)='integer') FROM connection_history "
                "WHERE kind=? AND id=?",
                (kind, identity),
            ).fetchone()
            if (count, first, last, integers) != (
                value["revision"],
                1,
                value["revision"],
                value["revision"],
            ):
                raise broken()
            original = self.db.execute(
                "SELECT payload,sha256 FROM connection_history "
                "WHERE kind=? AND id=? AND revision=?",
                (kind, identity, value["revision"]),
            ).fetchone()
            if (
                original is None
                or original["payload"] != row["payload"]
                or original["sha256"] != row["sha256"]
            ):
                raise broken()
        return value

    def list(self, kind, *, parent=None, statuses=None, limit=1000):
        if kind not in KINDS or type(limit) is not int or not 1 <= limit <= 10001:
            raise broken()
        orphan = self.db.execute(
            "SELECT 1 FROM connection_history h LEFT JOIN connection_records r "
            "ON h.kind=r.kind AND h.id=r.id WHERE h.kind=? AND r.id IS NULL LIMIT 1",
            (kind,),
        ).fetchone()
        if orphan:
            raise broken()
        query, params = "SELECT id FROM connection_records WHERE kind=?", [kind]
        if parent is not None:
            query += " AND parent_id=?"
            params.append(parent)
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        query += " ORDER BY id LIMIT ?"
        params.append(limit)
        return [self.get(kind, r[0]) for r in self.db.execute(query, params).fetchall()]

    def put(self, kind, value, *, expected_revision):
        integer(expected_revision, "预期连接版本", minimum=0)
        if kind not in KINDS or not isinstance(value, dict):
            raise broken()
        identity = text(value.get("id"), "连接记录编号", 2300)
        existing = self.get(kind, identity, missing=True)
        current_revision = 0 if existing is None else existing["revision"]
        if current_revision != expected_revision:
            raise PersonalAgentError("version_conflict", "连接资料已更新，请刷新后核对")
        document = {**value, "revision": current_revision + 1}
        raw = canonical(document)
        if len(raw.encode("utf-8")) > 2 * 1024**2:
            raise PersonalAgentError("invalid_input", "单份连接资料超过 2 MB 上限")
        checksum = digest({"kind": kind, "value": document})
        parent, status = document.get("connection_id", ""), document.get("status", "")
        text(parent, "连接父编号", 2300, empty=True)
        text(status, "连接状态", 64, empty=True)
        self.db.execute(
            "INSERT INTO connection_history VALUES (?,?,?,?,?)",
            (kind, identity, document["revision"], raw, checksum),
        )
        self.db.execute(
            "INSERT INTO connection_records VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE "
            "SET revision=excluded.revision,parent_id=excluded.parent_id,status=excluded.status,"
            "payload=excluded.payload,sha256=excluded.sha256",
            (kind, identity, document["revision"], parent, status, raw, checksum),
        )
        return document

    def request(self, request_id):
        text(request_id, "请求编号", 128, pattern=REQUEST)
        row = self.db.execute(
            "SELECT * FROM connection_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row["result"])
            if (
                digest(
                    {
                        "request_id": request_id,
                        "operation": row["operation"],
                        "fingerprint": row["fingerprint"],
                        "result": result,
                    }
                )
                != row["sha256"]
            ):
                raise broken()
            return {
                "request_id": request_id,
                "operation": row["operation"],
                "fingerprint": row["fingerprint"],
                "result": result,
            }
        except (ValueError, TypeError):
            raise broken() from None

    def save_request(self, request_id, operation, fingerprint, result):
        text(request_id, "请求编号", 128, pattern=REQUEST)
        text(fingerprint, "请求摘要", 64, pattern=SHA)
        if self.request(request_id) is not None:
            raise PersonalAgentError("idempotency_conflict", "原请求已保存，请读取原记录")
        payload = {
            "request_id": request_id,
            "operation": operation,
            "fingerprint": fingerprint,
            "result": result,
        }
        self.db.execute(
            "INSERT INTO connection_requests VALUES (?,?,?,?,?)",
            (request_id, operation, fingerprint, canonical(result), digest(payload)),
        )

    def lease(self, scope, identity):
        text(scope, "租约类型", 64)
        text(identity, "租约编号", 2300)
        row = self.db.execute(
            "SELECT * FROM connection_leases WHERE scope=? AND id=?", (scope, identity)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        integer(result["fence"], "租约版本")
        text(result["holder"], "执行者编号", 128)
        self._time(result["expires_at"])
        if result["phase"] not in {"claimed", "sent", "finished", "unknown"}:
            raise broken()
        return result

    @staticmethod
    def _time(value):
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise PersonalAgentError("invalid_input", "租约时间需要有限的有效时间戳")

    def claim(self, scope, identity, holder, *, now, ttl=60, once=False):
        integer(ttl, "租约时限", maximum=900)
        self._time(now)
        text(holder, "执行者编号", 128)
        if type(once) is not bool:
            raise PersonalAgentError("invalid_input", "租约执行模式格式不正确")
        previous = self.lease(scope, identity)
        if previous and previous["phase"] != "finished" and previous["expires_at"] > now:
            raise busy()
        if previous and once and previous["phase"] in {"sent", "unknown"}:
            raise PersonalAgentError("connection_unknown", "原账户请求结果待核对，未重新发送")
        fence = 1 if previous is None else previous["fence"] + 1
        self.db.execute(
            "INSERT INTO connection_leases VALUES (?,?,?,?,?,?) ON CONFLICT(scope,id) DO UPDATE "
            "SET fence=excluded.fence,holder=excluded.holder,expires_at=excluded.expires_at,"
            "phase=excluded.phase",
            (scope, identity, fence, holder, now + ttl, "claimed"),
        )
        return self.lease(scope, identity)

    def require_lease(self, scope, identity, holder, fence, *, now, phase=None):
        self._time(now)
        integer(fence, "租约版本")
        text(holder, "执行者编号", 128)
        lease = self.lease(scope, identity)
        if (
            not lease
            or lease["holder"] != holder
            or lease["fence"] != fence
            or lease["expires_at"] <= now
        ):
            raise PersonalAgentError("lease_lost", "账户操作执行权已变化，已停止旧执行")
        if phase is not None:
            if phase not in {"claimed", "sent", "finished", "unknown"}:
                raise broken()
            transitions = {
                "claimed": {"claimed", "sent", "finished", "unknown"},
                "sent": {"sent", "finished", "unknown"},
                "unknown": {"unknown", "finished"},
                "finished": {"finished"},
            }
            if phase not in transitions[lease["phase"]]:
                raise PersonalAgentError("lease_lost", "已发出的账户请求未重新进入可发送状态")
            self.db.execute(
                "UPDATE connection_leases SET phase=?,expires_at=? WHERE scope=? AND id=?",
                (phase, now if phase == "finished" else lease["expires_at"], scope, identity),
            )
        return self.lease(scope, identity)


class ConnectionStore:
    def __init__(self, base_dir, *, owner_key):
        self.base_dir = checked_path(Path(base_dir).expanduser().absolute())
        self.owner_key = text(owner_key, "连接所有者标识", 64, pattern=SHA)
        self.db_path = self.base_dir / "personal_connections.db"
        self.witness_path = self.base_dir / ".personal-connections-v1"
        self.lock_path = self.base_dir / ".personal-connections-bootstrap.lock"
        self._lock = threading.RLock()
        self._seen = False

    def _paths(self):
        for path in (self.base_dir, self.db_path, self.witness_path, self.lock_path):
            checked_path(path)
            if path.exists():
                if (
                    path == self.base_dir
                    and not path.is_dir()
                    or path != self.base_dir
                    and not path.is_file()
                ):
                    raise broken()

    def _handle(self, stream, path):
        self._paths()
        a, b = os.fstat(stream.fileno()), path.lstat()
        if not stat.S_ISREG(a.st_mode) or a.st_nlink != 1 or not os.path.samestat(a, b):
            raise broken()

    @contextmanager
    def _bootstrap(self, create):
        self._paths()
        if create:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self._paths()
        elif not self.lock_path.exists():
            yield False
            return
        try:
            stream = self.lock_path.open("x+b") if create else self.lock_path.open("r+b")
        except FileExistsError:
            stream = self.lock_path.open("r+b")
        with stream:
            self._handle(stream, self.lock_path)
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
                        raise busy() from None
                    time.sleep(0.01)
            try:
                self._handle(stream, self.lock_path)
                stream.seek(0)
                if stream.read(2) not in {b"", b"\0"}:
                    raise broken()
                if os.fstat(stream.fileno()).st_size == 0 and create:
                    stream.write(b"\0")
                    stream.flush()
                    os.fsync(stream.fileno())
                yield True
            finally:
                unlock()

    def _observe(self, locked):
        try:
            self._paths()
            exists = self.db_path.is_file()
            if (self._seen or self.witness_path.exists()) and not exists:
                raise broken()
        except PersonalAgentError:
            if not locked and self.lock_path.exists():
                return None
            raise
        if not locked and self.lock_path.exists():
            return None
        return exists

    def _validate(self, db):
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise broken()
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != _TABLES:
            raise broken()
        table_definitions = {
            r[0]: schema_sql(r[1])
            for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_tables = {statement.split()[2]: schema_sql(statement) for statement in _SCHEMA[:5]}
        if table_definitions != expected_tables:
            raise broken()
        triggers = {
            r[0]: schema_sql(r[1])
            for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
        }
        if triggers != {name: schema_sql(sql) for name, sql in _TRIGGERS.items()}:
            raise broken()
        rows = db.execute("SELECT * FROM connection_meta").fetchall()
        if (
            len(rows) != 1
            or rows[0]["owner_key"] != self.owner_key
            or rows[0]["schema_version"] != 1
        ):
            raise broken()
        identity = text(
            rows[0]["instance_id"], "连接库身份", 32, pattern=re.compile(r"[a-f0-9]{32}")
        )
        with self.witness_path.open("rb") as stream:
            self._handle(stream, self.witness_path)
            if stream.read(33) != identity.encode("ascii"):
                raise broken()
            self._handle(stream, self.witness_path)
        return identity

    @contextmanager
    def connection(self, *, write=False):
        db = None
        try:
            with self._lock:
                while True:
                    with self._bootstrap(write) as locked:
                        exists = self._observe(locked)
                        if exists is None:
                            continue
                        if not exists and not write:
                            break
                        if not exists:
                            db = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
                            db.row_factory = sqlite3.Row
                            db.execute("PRAGMA journal_mode=WAL")
                            db.execute("BEGIN IMMEDIATE")
                            if (
                                db.execute(
                                    "SELECT count(*) FROM sqlite_master WHERE type='table'"
                                ).fetchone()[0]
                                or db.execute("PRAGMA user_version").fetchone()[0]
                            ):
                                raise broken()
                            for statement in _SCHEMA:
                                db.execute(statement)
                            identity = uuid.uuid4().hex
                            db.execute(
                                "INSERT INTO connection_meta VALUES (1,?,?,1)",
                                (self.owner_key, identity),
                            )
                            db.execute("PRAGMA user_version=1")
                            for statement in _TRIGGERS.values():
                                db.execute(statement)
                            with self.witness_path.open("xb") as stream:
                                self._handle(stream, self.witness_path)
                                stream.write(identity.encode("ascii"))
                                stream.flush()
                                os.fsync(stream.fileno())
                        else:
                            db = (
                                sqlite3.connect(
                                    self.db_path.as_uri() + "?mode=ro",
                                    uri=True,
                                    isolation_level=None,
                                    timeout=10,
                                )
                                if not write
                                else sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
                            )
                            db.row_factory = sqlite3.Row
                            db.execute("BEGIN")
                        self._validate(db)
                        db.commit()
                        self._seen = True
                        break
            if db is None:
                yield None
                return
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._validate(db)
            yield ConnectionTransaction(db)
            db.commit()
        except PersonalAgentError:
            if db is not None:
                db.rollback()
            raise
        except sqlite3.Error as exc:
            if db is not None:
                db.rollback()
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                raise busy() from None
            raise broken() from None
        except (OSError, ValueError, KeyError, TypeError):
            if db is not None:
                db.rollback()
            raise broken() from None
        finally:
            if db is not None:
                db.close()

    def read(self, callback):
        with self.connection() as tx:
            return callback(tx)

    def write(self, callback):
        with self.connection(write=True) as tx:
            return callback(tx)

    def mutate(self, request_id, operation, payload, callback):
        fingerprint = digest({"operation": operation, "payload": payload})

        def run(tx):
            existing = tx.request(request_id)
            if existing is not None:
                if existing["operation"] != operation or existing["fingerprint"] != fingerprint:
                    raise PersonalAgentError(
                        "idempotency_conflict", "同一请求编号对应不同内容，请核对原请求"
                    )
                return existing["result"]
            result = callback(tx)
            tx.save_request(request_id, operation, fingerprint, result)
            return result

        return self.write(run)
