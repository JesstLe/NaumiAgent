"""Owner-bound SQLite journals with immutable facts and short fenced transactions.

Specs are internal constants, never request data. The legacy connection spec keeps
its original SQL and checksum format; new journals opt into stronger lease proofs.
"""

from __future__ import annotations

import errno
import hashlib
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
from dataclasses import dataclass
from pathlib import Path

from naumi_agent.personal.models import PersonalAgentError

SHA = re.compile(r"^[a-f0-9]{64}$")
INSTANCE = re.compile(r"^[a-f0-9]{32}$")
REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def canonical(value):
    """Serialize bounded JSON without coercion, NaN, or ambiguous object keys."""

    def visit(item, depth=0):
        if depth > 24:
            raise PersonalAgentError("invalid_input", "资料嵌套超过上限")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if any(ord(c) < 32 and c not in "\t\r\n" for c in item):
                raise ValueError("control character")
            item.encode("utf-8")
            return
        if isinstance(item, int) or isinstance(item, float) and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict) and all(isinstance(k, str) for k in item):
            for key, child in item.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
            return
        raise ValueError("not JSON")

    try:
        visit(value)
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (UnicodeError, RecursionError, ValueError, TypeError):
        raise PersonalAgentError("invalid_input", "资料字段格式不正确") from None


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text(value, label, maximum=500, *, pattern=None, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise PersonalAgentError("invalid_input", f"{label}格式不正确")
    canonical(value)
    if pattern and not pattern.fullmatch(value):
        raise PersonalAgentError("invalid_input", f"{label}格式不正确")
    return value


def integer(value, label, minimum=1, maximum=2**53 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PersonalAgentError("invalid_input", f"{label}格式不正确")
    return value


def schema_sql(value):
    return " ".join(value.split()).rstrip(";").casefold()


@dataclass(frozen=True, slots=True)
class JournalSpec:
    namespace: str
    kinds: frozenset[str]
    parent_field: str
    label: str
    db_filename: str
    witness_filename: str
    lock_filename: str
    lease_proofs: bool = False
    domain_hashes: bool = False

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", self.namespace):
            raise ValueError("Journal namespace is an internal SQL identifier")
        if not self.kinds or not isinstance(self.kinds, frozenset):
            raise ValueError("Journal kinds must be a fixed frozenset")
        for filename in (self.db_filename, self.witness_filename, self.lock_filename):
            if Path(filename).name != filename or filename in {".", ".."}:
                raise ValueError("Journal filenames must be fixed basenames")

    def broken(self):
        return PersonalAgentError(
            "storage_error", f"{self.label}资料校验未通过，请恢复原资料库后继续"
        )

    def busy(self):
        return PersonalAgentError(
            f"{self.namespace}_busy", f"{self.label}正在处理其他操作，请稍后核对原请求"
        )

    @property
    def schema(self):
        n = self.namespace
        statements = (
            f"""CREATE TABLE {n}_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner_key TEXT NOT NULL,
        instance_id TEXT NOT NULL, schema_version INTEGER NOT NULL)""",
            f"""CREATE TABLE {n}_records (
        kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
        parent_id TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,
        PRIMARY KEY(kind,id))""",
            f"""CREATE TABLE {n}_history (
        kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
        payload TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(kind,id,revision))""",
            f"""CREATE TABLE {n}_requests (
        request_id TEXT PRIMARY KEY,operation TEXT NOT NULL,fingerprint TEXT NOT NULL,
        result TEXT NOT NULL,sha256 TEXT NOT NULL)""",
            f"""CREATE TABLE {n}_leases (
        scope TEXT NOT NULL,id TEXT NOT NULL,fence INTEGER NOT NULL CHECK(fence>0),
        holder TEXT NOT NULL,expires_at REAL NOT NULL,phase TEXT NOT NULL,PRIMARY KEY(scope,id))""",
            f"CREATE INDEX {n}_parent ON {n}_records(kind,parent_id,status,id)",
        )
        if self.lease_proofs:
            statements += (
                f"""CREATE TABLE {n}_lease_history (
        scope TEXT NOT NULL,id TEXT NOT NULL,sequence INTEGER NOT NULL CHECK(sequence>0),
        payload TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(scope,id,sequence))""",
            )
        return statements

    @property
    def tables(self):
        return {
            statement.split()[2]
            for statement in self.schema
            if statement.startswith("CREATE TABLE")
        }

    @property
    def triggers(self):
        immutable = ("meta", "history", "requests")
        if self.lease_proofs:
            immutable += ("lease_history",)
        return {
            f"{self.namespace}_{table}_immutable_{operation.lower()}": (
                f"CREATE TRIGGER {self.namespace}_{table}_immutable_{operation.lower()} "
                f"BEFORE {operation} ON {self.namespace}_{table} "
                f"BEGIN SELECT RAISE(ABORT,'immutable {self.namespace} fact'); END"
            )
            for table in immutable
            for operation in ("UPDATE", "DELETE")
        }


def ordinary(path):
    raw = str(path)
    return Path(raw[4:]) if os.name == "nt" and raw.startswith("\\\\?\\") else Path(path)


def checked_path(path, spec):
    path = ordinary(Path(path).absolute())
    if os.path.normcase(str(path)) != os.path.normcase(str(ordinary(path.resolve()))):
        raise spec.broken()
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise spec.broken()
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise spec.broken()
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise spec.broken()
    return path


class JournalTransaction:
    def __init__(self, db, spec, owner_key):
        self.db, self.spec, self.owner_key = db, spec, owner_key

    def _digest(self, value):
        if self.spec.domain_hashes:
            value = {"journal": self.spec.namespace, "owner_key": self.owner_key, "fact": value}
        return digest(value)

    def _decode(self, row):
        try:
            value = json.loads(row["payload"])
            if (
                self._digest({"kind": row["kind"], "value": value}) != row["sha256"]
                or value["id"] != row["id"]
                or value["revision"] != row["revision"]
            ):
                raise self.spec.broken()
            integer(value["revision"], f"{self.spec.label}记录版本")
            if self.spec.domain_hashes and canonical(value) != row["payload"]:
                raise self.spec.broken()
            return value
        except (ValueError, KeyError, TypeError):
            raise self.spec.broken() from None
        except PersonalAgentError:
            if self.spec.domain_hashes:
                raise self.spec.broken() from None
            raise

    def get(self, kind, identity, *, revision=None, missing=False):
        if kind not in self.spec.kinds:
            raise self.spec.broken()
        if revision is not None:
            integer(revision, f"{self.spec.label}记录版本")
            if self.spec.domain_hashes:
                self.get(kind, identity)
        table = (
            f"{self.spec.namespace}_records"
            if revision is None
            else f"{self.spec.namespace}_history"
        )
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
                    f"SELECT 1 FROM {self.spec.namespace}_history WHERE kind=? AND id=? LIMIT 1",
                    (kind, identity),
                ).fetchone()
            ):
                raise self.spec.broken()
            if missing:
                return None
            raise PersonalAgentError("not_found", f"该{self.spec.label}记录不存在")
        value = self._decode(row)
        if revision is None and (
            value.get(self.spec.parent_field, "") != row["parent_id"]
            or value.get("status", "") != row["status"]
        ):
            raise self.spec.broken()
        if revision is None:
            count, first, last, integers = self.db.execute(
                "SELECT count(*),min(revision),max(revision),"
                f"sum(typeof(revision)='integer') FROM {self.spec.namespace}_history "
                "WHERE kind=? AND id=?",
                (kind, identity),
            ).fetchone()
            if (count, first, last, integers) != (
                value["revision"],
                1,
                value["revision"],
                value["revision"],
            ):
                raise self.spec.broken()
            original = self.db.execute(
                f"SELECT payload,sha256 FROM {self.spec.namespace}_history "
                "WHERE kind=? AND id=? AND revision=?",
                (kind, identity, value["revision"]),
            ).fetchone()
            if (
                original is None
                or original["payload"] != row["payload"]
                or original["sha256"] != row["sha256"]
            ):
                raise self.spec.broken()
            if self.spec.domain_hashes:
                for historical in self.db.execute(
                    f"SELECT * FROM {self.spec.namespace}_history WHERE kind=? AND id=? "
                    "ORDER BY revision",
                    (kind, identity),
                ).fetchall():
                    self._decode(historical)
        return value

    def list(self, kind, *, parent=None, statuses=None, limit=1000, after=None):
        if kind not in self.spec.kinds or type(limit) is not int or not 1 <= limit <= 10001:
            raise self.spec.broken()
        orphan = self.db.execute(
            f"SELECT 1 FROM {self.spec.namespace}_history h "
            f"LEFT JOIN {self.spec.namespace}_records r "
            "ON h.kind=r.kind AND h.id=r.id WHERE h.kind=? AND r.id IS NULL LIMIT 1",
            (kind,),
        ).fetchone()
        if orphan:
            raise self.spec.broken()
        if self.spec.domain_hashes:
            if parent is not None:
                text(parent, "父记录编号", 2300, empty=True)
            if statuses is not None:
                if not isinstance(statuses, (list, tuple, frozenset, set)) or len(statuses) > 64:
                    raise PersonalAgentError("invalid_input", "记录状态筛选格式不正确")
                for status in statuses:
                    text(status, "记录状态", 64, empty=True)
        query, params = f"SELECT id FROM {self.spec.namespace}_records WHERE kind=?", [kind]
        if parent is not None:
            query += " AND parent_id=?"
            params.append(parent)
        if after is not None:
            text(after, "续读位置", 2300)
            query += " AND id>?"
            params.append(after)
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        query += " ORDER BY id LIMIT ?"
        params.append(limit)
        return [self.get(kind, r[0]) for r in self.db.execute(query, params).fetchall()]

    def page(self, kind, *, parent=None, statuses=None, limit=1000, after=None):
        integer(limit, "每页记录数", maximum=10000)
        rows = self.list(kind, parent=parent, statuses=statuses, limit=limit + 1, after=after)
        return {
            "records": rows[:limit],
            "next_after": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    def put(self, kind, value, *, expected_revision):
        integer(expected_revision, f"预期{self.spec.label}版本", minimum=0)
        if kind not in self.spec.kinds or not isinstance(value, dict):
            raise self.spec.broken()
        identity = text(value.get("id"), f"{self.spec.label}记录编号", 2300)
        existing = self.get(kind, identity, missing=True)
        current_revision = 0 if existing is None else existing["revision"]
        if current_revision != expected_revision:
            raise PersonalAgentError(
                "version_conflict", f"{self.spec.label}资料已更新，请刷新后核对"
            )
        document = {**value, "revision": current_revision + 1}
        raw = canonical(document)
        if len(raw.encode("utf-8")) > 2 * 1024**2:
            raise PersonalAgentError("invalid_input", f"单份{self.spec.label}资料超过 2 MB 上限")
        checksum = self._digest({"kind": kind, "value": document})
        parent, status = document.get(self.spec.parent_field, ""), document.get("status", "")
        text(parent, f"{self.spec.label}父编号", 2300, empty=True)
        text(status, f"{self.spec.label}状态", 64, empty=True)
        self.db.execute(
            f"INSERT INTO {self.spec.namespace}_history VALUES (?,?,?,?,?)",
            (kind, identity, document["revision"], raw, checksum),
        )
        self.db.execute(
            f"INSERT INTO {self.spec.namespace}_records VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(kind,id) DO UPDATE "
            "SET revision=excluded.revision,parent_id=excluded.parent_id,status=excluded.status,"
            "payload=excluded.payload,sha256=excluded.sha256",
            (kind, identity, document["revision"], parent, status, raw, checksum),
        )
        return document

    def request(self, request_id):
        text(request_id, "请求编号", 128, pattern=REQUEST)
        row = self.db.execute(
            f"SELECT * FROM {self.spec.namespace}_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row["result"])
            if self.spec.domain_hashes and (
                canonical(result) != row["result"]
                or SHA.fullmatch(row["fingerprint"]) is None
                or not row["operation"]
            ):
                raise self.spec.broken()
            if (
                self._digest(
                    {
                        "request_id": request_id,
                        "operation": row["operation"],
                        "fingerprint": row["fingerprint"],
                        "result": result,
                    }
                )
                != row["sha256"]
            ):
                raise self.spec.broken()
            return {
                "request_id": request_id,
                "operation": row["operation"],
                "fingerprint": row["fingerprint"],
                "result": result,
            }
        except (ValueError, TypeError):
            raise self.spec.broken() from None
        except PersonalAgentError:
            if self.spec.domain_hashes:
                raise self.spec.broken() from None
            raise

    def save_request(self, request_id, operation, fingerprint, result):
        text(request_id, "请求编号", 128, pattern=REQUEST)
        text(operation, "请求操作", 128)
        text(fingerprint, "请求摘要", 64, pattern=SHA)
        if self.request(request_id) is not None:
            raise PersonalAgentError("idempotency_conflict", "原请求已保存，请读取原记录")
        payload = {
            "request_id": request_id,
            "operation": operation,
            "fingerprint": fingerprint,
            "result": result,
        }
        raw = canonical(result)
        if self.spec.domain_hashes and len(raw.encode("utf-8")) > 2 * 1024**2:
            raise PersonalAgentError("invalid_input", "单份请求回执超过 2 MB 上限")
        self.db.execute(
            f"INSERT INTO {self.spec.namespace}_requests VALUES (?,?,?,?,?)",
            (request_id, operation, fingerprint, raw, self._digest(payload)),
        )

    def lease(self, scope, identity):
        text(scope, "租约类型", 64)
        text(identity, "租约编号", 2300)
        row = self.db.execute(
            f"SELECT * FROM {self.spec.namespace}_leases WHERE scope=? AND id=?", (scope, identity)
        ).fetchone()
        if row is None:
            if (
                self.spec.lease_proofs
                and self.db.execute(
                    f"SELECT 1 FROM {self.spec.namespace}_lease_history "
                    "WHERE scope=? AND id=? LIMIT 1",
                    (scope, identity),
                ).fetchone()
            ):
                raise self.spec.broken()
            return None
        result = dict(row)
        try:
            integer(result["fence"], "租约版本")
            text(result["holder"], "执行者编号", 128)
            self._time(result["expires_at"])
        except PersonalAgentError:
            if self.spec.lease_proofs:
                raise self.spec.broken() from None
            raise
        if result["phase"] not in {"claimed", "sent", "finished", "unknown"}:
            raise self.spec.broken()
        if self.spec.lease_proofs:
            previous, latest = "", None
            for sequence, event in enumerate(
                self.db.execute(
                    f"SELECT * FROM {self.spec.namespace}_lease_history WHERE scope=? AND id=? "
                    "ORDER BY sequence",
                    (scope, identity),
                ).fetchall(),
                1,
            ):
                payload = json.loads(event["payload"])
                if (
                    type(event["sequence"]) is not int
                    or event["sequence"] != sequence
                    or not isinstance(payload, dict)
                    or payload.get("sequence") != sequence
                    or payload.get("previous_sha256") != previous
                    or self._digest(payload) != event["sha256"]
                    or canonical(payload) != event["payload"]
                ):
                    raise self.spec.broken()
                latest, previous = payload.get("lease"), event["sha256"]
            if latest != result:
                raise self.spec.broken()
        return result

    def _append_lease(self, scope, identity):
        if not self.spec.lease_proofs:
            return
        head = self.db.execute(
            f"SELECT sequence,sha256 FROM {self.spec.namespace}_lease_history "
            "WHERE scope=? AND id=? ORDER BY sequence DESC LIMIT 1",
            (scope, identity),
        ).fetchone()
        row = self.db.execute(
            f"SELECT * FROM {self.spec.namespace}_leases WHERE scope=? AND id=?",
            (scope, identity),
        ).fetchone()
        sequence = 1 if head is None else head["sequence"] + 1
        payload = {
            "sequence": sequence,
            "previous_sha256": "" if head is None else head["sha256"],
            "lease": dict(row),
        }
        self.db.execute(
            f"INSERT INTO {self.spec.namespace}_lease_history VALUES (?,?,?,?,?)",
            (scope, identity, sequence, canonical(payload), self._digest(payload)),
        )

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
            raise self.spec.busy()
        if previous and once and previous["phase"] in {"sent", "unknown"}:
            raise PersonalAgentError(
                f"{self.spec.namespace}_unknown", f"原{self.spec.label}请求结果待核对，未重新发送"
            )
        fence = 1 if previous is None else previous["fence"] + 1
        if self.spec.lease_proofs and (not math.isfinite(now + ttl) or now + ttl <= now):
            raise PersonalAgentError("invalid_input", "租约时限超过有效时间范围")
        self.db.execute(
            f"INSERT INTO {self.spec.namespace}_leases VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(scope,id) DO UPDATE "
            "SET fence=excluded.fence,holder=excluded.holder,expires_at=excluded.expires_at,"
            "phase=excluded.phase",
            (scope, identity, fence, holder, now + ttl, "claimed"),
        )
        self._append_lease(scope, identity)
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
            raise PersonalAgentError(
                "lease_lost", f"{self.spec.label}操作执行权已变化，已停止旧执行"
            )
        if phase is not None:
            if phase not in {"claimed", "sent", "finished", "unknown"}:
                raise self.spec.broken()
            transitions = {
                "claimed": {"claimed", "sent", "finished", "unknown"},
                "sent": {"sent", "finished", "unknown"},
                "unknown": {"unknown", "finished"},
                "finished": {"finished"},
            }
            if phase not in transitions[lease["phase"]]:
                raise PersonalAgentError(
                    "lease_lost", f"已发出的{self.spec.label}请求未重新进入可发送状态"
                )
            self.db.execute(
                f"UPDATE {self.spec.namespace}_leases SET phase=?,expires_at=? "
                "WHERE scope=? AND id=?",
                (phase, now if phase == "finished" else lease["expires_at"], scope, identity),
            )
            self._append_lease(scope, identity)
        return self.lease(scope, identity)

    def fence(self, scope, identity, holder, fence, *, now, phase=None):
        return self.require_lease(scope, identity, holder, fence, now=now, phase=phase)

    def release(self, scope, identity, holder, fence, *, now):
        return self.require_lease(scope, identity, holder, fence, now=now, phase="finished")


class OwnerJournal:
    transaction_type = JournalTransaction

    def __init__(self, base_dir, *, owner_key, spec, expected_instance_id=None):
        self.spec = spec
        self.base_dir = checked_path(Path(base_dir).expanduser().absolute(), spec)
        self.owner_key = text(owner_key, f"{spec.label}所有者标识", 64, pattern=SHA)
        self.db_path = self.base_dir / spec.db_filename
        self.witness_path = self.base_dir / spec.witness_filename
        self.lock_path = self.base_dir / spec.lock_filename
        self.expected_instance_id = expected_instance_id
        if expected_instance_id is not None:
            text(expected_instance_id, f"{spec.label}库身份", 32, pattern=INSTANCE)
        self._lock = threading.RLock()
        self._seen = False

    def _digest(self, value):
        if self.spec.domain_hashes:
            value = {"journal": self.spec.namespace, "owner_key": self.owner_key, "fact": value}
        return digest(value)

    def identity(self):
        with self.connection() as tx:
            if tx is None:
                return None
            return tx.db.execute(f"SELECT instance_id FROM {self.spec.namespace}_meta").fetchone()[
                0
            ]

    def _paths(self):
        paths = (self.base_dir, self.db_path, self.witness_path, self.lock_path)
        if self.spec.domain_hashes:
            paths += tuple(
                Path(str(self.db_path) + suffix) for suffix in ("-wal", "-shm", "-journal")
            )
        for path in paths:
            checked_path(path, self.spec)
            try:
                info = path.lstat()
            except FileNotFoundError:
                # SQLite may legitimately unlink WAL/SHM/journal files between
                # observations. Stable DB/witness identity remains checked by
                # _observe/_validate; absence is never a new empty admission.
                continue
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or path == self.base_dir
                and not stat.S_ISDIR(info.st_mode)
                or path != self.base_dir
                and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
            ):
                raise self.spec.broken()

    def _handle(self, stream, path):
        self._paths()
        a, b = os.fstat(stream.fileno()), path.lstat()
        if not stat.S_ISREG(a.st_mode) or a.st_nlink != 1 or not os.path.samestat(a, b):
            raise self.spec.broken()

    @contextmanager
    def _bootstrap(self, create):
        self._paths()
        if create and (self._seen or self.expected_instance_id) and not self.db_path.is_file():
            raise self.spec.broken()
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
                        raise self.spec.busy() from None
                    time.sleep(0.01)
            try:
                self._handle(stream, self.lock_path)
                stream.seek(0)
                if stream.read(2) not in {b"", b"\0"}:
                    raise self.spec.broken()
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
            if (
                self._seen or self.expected_instance_id or self.witness_path.exists()
            ) and not exists:
                raise self.spec.broken()
        except PersonalAgentError:
            if not locked and self.lock_path.exists():
                return None
            raise
        if not locked and self.lock_path.exists():
            return None
        return exists

    def _validate(self, db):
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise self.spec.broken()
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != self.spec.tables:
            raise self.spec.broken()
        table_definitions = {
            r[0]: schema_sql(r[1])
            for r in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_tables = {
            statement.split()[2]: schema_sql(statement)
            for statement in self.spec.schema
            if statement.startswith("CREATE TABLE")
        }
        if table_definitions != expected_tables:
            raise self.spec.broken()
        triggers = {
            r[0]: schema_sql(r[1])
            for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
        }
        if triggers != {name: schema_sql(sql) for name, sql in self.spec.triggers.items()}:
            raise self.spec.broken()
        if self.spec.domain_hashes:
            indices = {
                r[0]: schema_sql(r[1])
                for r in db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
                )
            }
            if indices != {
                statement.split()[2]: schema_sql(statement)
                for statement in self.spec.schema
                if statement.startswith("CREATE INDEX")
            }:
                raise self.spec.broken()
        rows = db.execute(f"SELECT * FROM {self.spec.namespace}_meta").fetchall()
        if (
            len(rows) != 1
            or rows[0]["owner_key"] != self.owner_key
            or rows[0]["schema_version"] != 1
        ):
            raise self.spec.broken()
        identity = rows[0]["instance_id"]
        if self.spec.domain_hashes and (
            not isinstance(identity, str) or INSTANCE.fullmatch(identity) is None
        ):
            raise self.spec.broken()
        identity = text(identity, f"{self.spec.label}库身份", 32, pattern=INSTANCE)
        if self.expected_instance_id is not None and identity != self.expected_instance_id:
            raise self.spec.broken()
        with self.witness_path.open("rb") as stream:
            self._handle(stream, self.witness_path)
            if stream.read(33) != identity.encode("ascii"):
                raise self.spec.broken()
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
                            if self.expected_instance_id is not None:
                                raise self.spec.broken()
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
                                raise self.spec.broken()
                            for statement in self.spec.schema:
                                db.execute(statement)
                            identity = uuid.uuid4().hex
                            db.execute(
                                f"INSERT INTO {self.spec.namespace}_meta VALUES (1,?,?,1)",
                                (self.owner_key, identity),
                            )
                            db.execute("PRAGMA user_version=1")
                            for statement in self.spec.triggers.values():
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
                        if self.spec.domain_hashes:
                            self.expected_instance_id = self._validate(db)
                        break
            if db is None:
                yield None
                return
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._validate(db)
            yield self.transaction_type(db, self.spec, self.owner_key)
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
                raise self.spec.busy() from None
            raise self.spec.broken() from None
        except (OSError, ValueError, KeyError, TypeError):
            if db is not None:
                db.rollback()
            raise self.spec.broken() from None
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
