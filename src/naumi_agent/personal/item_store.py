"""Transactional personal-item facts; execution and delivery remain external authorities."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from naumi_agent.daemons.agent_jobs import agent_job_id_for_request
from naumi_agent.daemons.agent_worker_contract import AgentWorkerRequest
from naumi_agent.personal.item_models import (
    IDENTIFIER,
    INTENT_ID,
    ITEM_ID,
    ONCE_KEY,
    REQUEST_ID,
    SHA256,
    STATES,
    canonical,
    digest,
    integer,
    normalize_definition,
    normalize_intent,
    normalized_object,
    text,
    timestamp,
)
from naumi_agent.personal.models import PersonalAgentError

_SCHEMA_VERSION = 1
_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE personal_items (
        owner_key TEXT NOT NULL, item_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        state TEXT NOT NULL CHECK(state IN ('active','paused','cancelled','closed')),
        once_key TEXT NOT NULL, definition_sha256 TEXT NOT NULL, workspace_key TEXT NOT NULL,
        snapshot TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
        PRIMARY KEY(owner_key,item_id)
    )""",
    """CREATE TABLE personal_item_revisions (
        owner_key TEXT NOT NULL, item_id TEXT NOT NULL, revision INTEGER NOT NULL,
        snapshot TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
        PRIMARY KEY(owner_key,item_id,revision),
        FOREIGN KEY(owner_key,item_id) REFERENCES personal_items(owner_key,item_id)
    )""",
    """CREATE TABLE personal_item_requests (
        owner_key TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL, request_fingerprint TEXT, item_id TEXT NOT NULL,
        revision INTEGER NOT NULL, receipt_sha256 TEXT NOT NULL,
        PRIMARY KEY(owner_key,request_id),
        FOREIGN KEY(owner_key,item_id) REFERENCES personal_items(owner_key,item_id)
    )""",
    """CREATE TABLE personal_item_intents (
        owner_key TEXT NOT NULL, occurrence_id TEXT NOT NULL, item_id TEXT NOT NULL,
        once_key TEXT NOT NULL, workspace_key TEXT NOT NULL, event_id TEXT NOT NULL,
        prepared_sha256 TEXT NOT NULL, prepared TEXT NOT NULL, semantics_sha256 TEXT NOT NULL,
        PRIMARY KEY(owner_key,occurrence_id), UNIQUE(owner_key,item_id,once_key),
        UNIQUE(owner_key,workspace_key,event_id),
        FOREIGN KEY(owner_key,item_id) REFERENCES personal_items(owner_key,item_id)
    )""",
    """CREATE TABLE personal_item_reference_history (
        owner_key TEXT NOT NULL, occurrence_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        references_json TEXT NOT NULL, references_sha256 TEXT NOT NULL,
        PRIMARY KEY(owner_key,occurrence_id,sequence),
        FOREIGN KEY(owner_key,occurrence_id)
            REFERENCES personal_item_intents(owner_key,occurrence_id)
    )""",
    """CREATE TABLE personal_item_reference_heads (
        owner_key TEXT NOT NULL, occurrence_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        references_sha256 TEXT NOT NULL, PRIMARY KEY(owner_key,occurrence_id),
        FOREIGN KEY(owner_key,occurrence_id)
            REFERENCES personal_item_intents(owner_key,occurrence_id)
    )""",
    """CREATE TABLE personal_item_decisions (
        owner_key TEXT NOT NULL, item_id TEXT NOT NULL, once_key TEXT NOT NULL,
        event_id TEXT NOT NULL, decision TEXT NOT NULL, detail TEXT NOT NULL,
        occurred_at TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
        PRIMARY KEY(owner_key,item_id,once_key,event_id),
        FOREIGN KEY(owner_key,item_id) REFERENCES personal_items(owner_key,item_id)
    )""",
)
_IMMUTABLE = (
    "item_store_meta",
    "personal_item_revisions",
    "personal_item_requests",
    "personal_item_intents",
    "personal_item_reference_history",
    "personal_item_decisions",
)
_REFERENCE_FIELDS = frozenset(
    {
        "job_id",
        "namespace",
        "run_id",
        "publication_id",
        "delivery_id",
        "delivery_sha256",
        "acknowledgement_id",
    }
)
_META = """CREATE TABLE item_store_meta (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    schema_version INTEGER NOT NULL, owner_key TEXT NOT NULL,
    instance_id TEXT NOT NULL
)"""


def _broken() -> PersonalAgentError:
    return PersonalAgentError("storage_error", "个人事项资料库校验未通过，请恢复原资料库后继续")


def _ordinary(path: Path) -> Path:
    raw = str(path)
    return Path(raw[4:]) if os.name == "nt" and raw.startswith("\\\\?\\") else path


def utc_now() -> datetime:
    return datetime.now(UTC)


_NOW = utc_now


class PersonalItemStore:
    """Synchronous, owner-bound facts with immutable history and no runtime side effects."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        owner_key: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_dir = _ordinary(Path(base_dir).expanduser().absolute())
        self.db_path = self.base_dir / "personal_items.db"
        self.owner_key = (
            None
            if owner_key is None
            else text(
                owner_key,
                "个人事项所有者标识",
                64,
                pattern=SHA256,
            )
        )
        self._clock = clock or _NOW
        self._lock = threading.RLock()
        self._seen_database = False
        self._witness = self.base_dir / ".personal-items-v1"

    def _owner(self, owner_key) -> str:
        owner = text(owner_key, "个人事项所有者标识", 64, pattern=SHA256)
        if self.owner_key is not None and self.owner_key != owner:
            raise PersonalAgentError("not_found", "该个人事项不存在")
        return owner

    def _path(self) -> None:
        base = _ordinary(self.base_dir.resolve())
        actual = _ordinary(self.db_path.resolve())
        if (
            os.path.normcase(str(base)) != os.path.normcase(str(self.base_dir))
            or os.path.normcase(str(actual)) != os.path.normcase(str(self.db_path))
            or self.db_path.is_symlink()
            or self.db_path.exists()
            and not self.db_path.is_file()
        ):
            raise _broken()

    def _exists(self) -> bool:
        self._path()
        exists = self.db_path.is_file()
        if (self._seen_database or self._witness.exists()) and not exists:
            raise _broken()
        self._seen_database = self._seen_database or exists
        return exists

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise _broken()
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def _validate_schema(self, connection: sqlite3.Connection, *, create: bool, owner: str) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > _SCHEMA_VERSION:
            raise PersonalAgentError(
                "unsupported_schema", "个人事项资料库来自较新版本，请先更新应用"
            )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables and create and version == 0:
            connection.execute(_META)
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO item_store_meta VALUES (1,?,?,?)",
                (_SCHEMA_VERSION, owner, uuid.uuid4().hex),
            )
            for table in _IMMUTABLE:
                for action in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER {table}_immutable_{action.lower()} BEFORE {action} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT,'immutable item fact'); END"
                    )
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        elif version != _SCHEMA_VERSION or "item_store_meta" not in tables:
            raise _broken()
        metadata = connection.execute("SELECT * FROM item_store_meta").fetchall()
        if len(metadata) != 1 or (
            metadata[0]["schema_version"] != _SCHEMA_VERSION or metadata[0]["owner_key"] != owner
        ):
            raise _broken()
        if (
            not isinstance(metadata[0]["instance_id"], str)
            or ONCE_KEY.fullmatch(metadata[0]["instance_id"]) is None
        ):
            raise _broken()
        expected_tables = {
            "item_store_meta",
            *[name for name in _IMMUTABLE if name != "item_store_meta"],
            "personal_items",
            "personal_item_reference_heads",
        }
        actual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if actual_tables != expected_tables:
            raise _broken()
        triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        if triggers != {
            f"{table}_immutable_{action}" for table in _IMMUTABLE for action in ("update", "delete")
        }:
            raise _broken()

    @contextmanager
    def _connection(self, owner: str, *, write=False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            with self._lock:
                exists = self._exists()
                if write:
                    self.base_dir.mkdir(parents=True, exist_ok=True)
                    self._path()
                    connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
                elif exists:
                    connection = sqlite3.connect(
                        self.db_path.as_uri() + "?mode=ro",
                        uri=True,
                        timeout=10,
                        isolation_level=None,
                    )
                else:
                    raise _broken()
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("PRAGMA foreign_keys=ON")
                if write and not exists:
                    connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                self._validate_schema(connection, create=write and not exists, owner=owner)
                if write and not exists:
                    identity = connection.execute(
                        "SELECT instance_id FROM item_store_meta"
                    ).fetchone()[0]
                    try:
                        with self._witness.open("xb") as witness:
                            witness.write(identity.encode("ascii"))
                    except FileExistsError:
                        if (
                            self._witness.is_symlink()
                            or self._witness.read_text("ascii") != identity
                        ):
                            raise _broken() from None
                    connection.commit()
                    self._seen_database = True
                    connection.execute("BEGIN IMMEDIATE")
                if self._witness.exists():
                    identity = connection.execute(
                        "SELECT instance_id FROM item_store_meta"
                    ).fetchone()[0]
                    if self._witness.is_symlink() or self._witness.read_text("ascii") != identity:
                        raise _broken()
                else:
                    raise _broken()
                yield connection
                connection.commit()
                self._seen_database = True
        except PersonalAgentError:
            if connection is not None:
                connection.rollback()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            if connection is not None:
                connection.rollback()
            raise _broken() from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _id(value):
        return text(value, "事项编号", 35, pattern=ITEM_ID)

    @staticmethod
    def _decode(raw, expected):
        try:
            value = normalized_object(raw, "已保存的事项事实", maximum=20 * 1024**2)
        except PersonalAgentError as exc:
            raise _broken() from exc
        if digest(value) != expected:
            raise _broken()
        return value

    def _item(self, db, owner, item_id):
        row = db.execute(
            "SELECT * FROM personal_items WHERE owner_key=? AND item_id=?",
            (owner, item_id),
        ).fetchone()
        if row is None:
            raise PersonalAgentError("not_found", "该个人事项不存在")
        item = self._decode(row["snapshot"], row["snapshot_sha256"])
        required = {
            "item_id",
            "owner_key",
            "revision",
            "once_key",
            "state",
            "definition",
            "definition_sha256",
            "created_at",
            "updated_at",
        }
        if set(item) != required:
            raise _broken()
        try:
            definition = normalize_definition(item["definition"])
            timestamp(item["created_at"], "事项创建时间")
            timestamp(item["updated_at"], "事项更新时间")
        except PersonalAgentError as exc:
            raise _broken() from exc
        if (
            item["item_id"] != item_id
            or item["owner_key"] != owner
            or type(item["revision"]) is not int
            or item["revision"] < 1
            or item["state"] not in STATES
            or ONCE_KEY.fullmatch(item["once_key"]) is None
            or item["definition_sha256"] != digest(definition)
            or canonical(definition) != canonical(item["definition"])
        ):
            raise _broken()
        for key in ("revision", "once_key", "state", "definition_sha256"):
            if row[key] != item[key]:
                raise _broken()
        if row["workspace_key"] != definition["route"]["workspace_key"]:
            raise _broken()
        history = db.execute(
            "SELECT COUNT(*),MIN(revision),MAX(revision) FROM personal_item_revisions "
            "WHERE owner_key=? AND item_id=?",
            (owner, item_id),
        ).fetchone()
        latest = db.execute(
            "SELECT snapshot,snapshot_sha256 FROM personal_item_revisions "
            "WHERE owner_key=? AND item_id=? AND revision=?",
            (owner, item_id, item["revision"]),
        ).fetchone()
        if (
            tuple(history) != (item["revision"], 1, item["revision"])
            or latest is None
            or (
                latest["snapshot"] != row["snapshot"]
                or latest["snapshot_sha256"] != row["snapshot_sha256"]
            )
        ):
            raise _broken()
        return item

    @staticmethod
    def _write_item(db, item, *, create=False):
        raw, checksum = canonical(item), digest(item)
        values = (
            item["revision"],
            item["state"],
            item["once_key"],
            item["definition_sha256"],
            item["definition"]["route"]["workspace_key"],
            raw,
            checksum,
            item["owner_key"],
            item["item_id"],
        )
        if create:
            db.execute(
                "INSERT INTO personal_items(revision,state,once_key,definition_sha256,"
                "workspace_key,"
                "snapshot,snapshot_sha256,owner_key,item_id) VALUES (?,?,?,?,?,?,?,?,?)",
                values,
            )
        else:
            changed = db.execute(
                "UPDATE personal_items SET revision=?,state=?,once_key=?,definition_sha256=?,"
                "workspace_key=?,snapshot=?,snapshot_sha256=? WHERE owner_key=? AND item_id=? "
                "AND revision=?",
                (*values, item["revision"] - 1),
            ).rowcount
            if changed != 1:
                raise PersonalAgentError("version_conflict", "事项资料已更新，请刷新后再修改")
        db.execute(
            "INSERT INTO personal_item_revisions VALUES (?,?,?,?,?)",
            (item["owner_key"], item["item_id"], item["revision"], raw, checksum),
        )

    def _request(self, db, owner, request_id):
        row = db.execute(
            "SELECT * FROM personal_item_requests WHERE owner_key=? AND request_id=?",
            (owner, request_id),
        ).fetchone()
        if row is None:
            return None
        receipt = dict(row)
        checksum = receipt.pop("receipt_sha256")
        if checksum != digest(receipt):
            raise _broken()
        receipt["item"] = self._item(db, owner, receipt["item_id"])
        committed = db.execute(
            "SELECT snapshot,snapshot_sha256 FROM personal_item_revisions "
            "WHERE owner_key=? AND item_id=? AND revision=?",
            (owner, receipt["item_id"], receipt["revision"]),
        ).fetchone()
        if committed is None:
            raise _broken()
        receipt["committed_item"] = self._decode(
            committed["snapshot"],
            committed["snapshot_sha256"],
        )
        return receipt

    def _replay(self, db, owner, request_id, operation, payload):
        previous = self._request(db, owner, request_id)
        if previous is not None:
            if previous["operation"] != operation or previous["payload_sha256"] != digest(payload):
                raise PersonalAgentError("idempotency_conflict", "该请求编号已用于其他事项操作")
            return previous["item"]
        return None

    @staticmethod
    def _save_request(db, owner, request_id, operation, payload, item, fingerprint=None):
        receipt = {
            "owner_key": owner,
            "request_id": request_id,
            "operation": operation,
            "payload_sha256": digest(payload),
            "request_fingerprint": fingerprint,
            "item_id": item["item_id"],
            "revision": item["revision"],
        }
        db.execute(
            "INSERT INTO personal_item_requests VALUES (?,?,?,?,?,?,?,?)",
            (*receipt.values(), digest(receipt)),
        )

    def create(self, *, owner_key, request_id, definition, request_fingerprint=None):
        owner = self._owner(owner_key)
        request_id = text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        definition = normalize_definition(definition)
        if request_fingerprint is not None:
            text(request_fingerprint, "原始请求摘要", 64, pattern=SHA256)
        payload = (
            {"request_fingerprint": request_fingerprint}
            if request_fingerprint is not None
            else {
                "definition": definition,
            }
        )
        with self._connection(owner, write=True) as db:
            previous = self._replay(db, owner, request_id, "create", payload)
            if previous is not None:
                return previous
            now = self._now()
            item = {
                "item_id": "pi_" + uuid.uuid4().hex,
                "owner_key": owner,
                "revision": 1,
                "once_key": uuid.uuid4().hex,
                "state": "active",
                "definition": definition,
                "definition_sha256": digest(definition),
                "created_at": now,
                "updated_at": now,
            }
            self._write_item(db, item, create=True)
            self._save_request(db, owner, request_id, "create", payload, item, request_fingerprint)
            return json.loads(canonical(item))

    def find_request(self, *, owner_key, request_id):
        owner = self._owner(owner_key)
        request_id = text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        if not self._exists():
            return None
        with self._connection(owner) as db:
            return self._request(db, owner, request_id)

    def identity(self, *, owner_key):
        """Return a validated catalog witness without creating a new database."""
        owner = self._owner(owner_key)
        if not self._exists():
            return None
        with self._connection(owner) as db:
            return db.execute("SELECT instance_id FROM item_store_meta").fetchone()[0]

    def get(self, *, owner_key, item_id):
        owner, item_id = self._owner(owner_key), self._id(item_id)
        if not self._exists():
            raise PersonalAgentError("not_found", "该个人事项不存在")
        with self._connection(owner) as db:
            return self._item(db, owner, item_id)

    def list(self, *, owner_key, workspace_key=None, state=None, query="", limit=50, offset=0):
        owner = self._owner(owner_key)
        integer(limit, "事项页大小", 1, 500)
        integer(offset, "事项页位置", 0)
        query = text(query, "事项查询", 200, empty=True)
        if state is not None and (not isinstance(state, str) or state not in STATES):
            raise PersonalAgentError("invalid_input", "事项状态不正确")
        if workspace_key is not None:
            text(workspace_key, "工作区标识", 64, pattern=SHA256)
        if not self._exists():
            return []
        with self._connection(owner) as db:
            rows = db.execute(
                "SELECT item_id FROM personal_items WHERE owner_key=? ORDER BY item_id",
                (owner,),
            ).fetchall()
            items = [self._item(db, owner, row[0]) for row in rows]
            result = [
                item
                for item in items
                if (state is None or item["state"] == state)
                and (
                    workspace_key is None
                    or item["definition"]["route"]["workspace_key"] == workspace_key
                )
                and (
                    not query
                    or query.casefold() in item["definition"]["title"].casefold()
                    or query.casefold() in item["definition"]["instructions"].casefold()
                )
            ]
            result.sort(key=lambda item: (item["updated_at"], item["item_id"]), reverse=True)
            return result[offset : offset + limit]

    def history(self, *, owner_key, item_id):
        owner, item_id = self._owner(owner_key), self._id(item_id)
        if not self._exists():
            raise PersonalAgentError("not_found", "该个人事项不存在")
        with self._connection(owner) as db:
            self._item(db, owner, item_id)
            rows = db.execute(
                "SELECT * FROM personal_item_revisions WHERE owner_key=? AND item_id=? "
                "ORDER BY revision",
                (owner, item_id),
            ).fetchall()
            result = []
            for row in rows:
                item = self._decode(row["snapshot"], row["snapshot_sha256"])
                if (item["owner_key"], item["item_id"], item["revision"]) != (
                    owner,
                    item_id,
                    row["revision"],
                ) or digest(normalize_definition(item["definition"])) != item["definition_sha256"]:
                    raise _broken()
                result.append(item)
            return result

    def _mutate(
        self,
        *,
        owner_key,
        item_id,
        expected_revision,
        request_id,
        operation,
        definition=None,
        action=None,
        request_fingerprint=None,
    ):
        owner, item_id = self._owner(owner_key), self._id(item_id)
        integer(expected_revision, "事项版本")
        request_id = text(request_id, "请求编号", 128, pattern=REQUEST_ID)
        definition = None if definition is None else normalize_definition(definition)
        if operation == "control" and action not in (
            "pause",
            "resume",
            "cancel",
            "close",
            "run-now",
        ):
            raise PersonalAgentError("invalid_input", "事项控制操作不正确")
        if request_fingerprint is not None:
            text(request_fingerprint, "原始请求摘要", 64, pattern=SHA256)
        if not self._exists():
            raise PersonalAgentError("not_found", "该个人事项不存在")
        payload = {
            "operation": operation,
            "item_id": item_id,
            "expected_revision": expected_revision,
            "action": action,
        }
        payload.update(
            {"request_fingerprint": request_fingerprint}
            if request_fingerprint is not None
            else {"definition": definition}
        )
        with self._connection(owner, write=True) as db:
            previous = self._replay(db, owner, request_id, operation, payload)
            if previous is not None:
                return previous
            item = self._item(db, owner, item_id)
            if item["revision"] != expected_revision:
                raise PersonalAgentError("version_conflict", "事项资料已更新，请刷新后再修改")
            candidate = json.loads(canonical(item))
            if operation == "update_definition":
                if item["state"] in {"cancelled", "closed"}:
                    raise PersonalAgentError(
                        "lifecycle_conflict", "该事项已结束，请明确创建或重新执行"
                    )
                if db.execute(
                    "SELECT 1 FROM personal_item_intents WHERE owner_key=? AND item_id=? "
                    "AND once_key=? LIMIT 1",
                    (owner, item_id, item["once_key"]),
                ).fetchone():
                    raise PersonalAgentError(
                        "definition_conflict", "事项已准备执行，请查看原执行后明确重新安排"
                    )
                candidate["definition"], candidate["definition_sha256"] = (
                    definition,
                    digest(definition),
                )
            elif operation == "restart":
                if item["state"] not in {"cancelled", "closed"}:
                    raise PersonalAgentError(
                        "lifecycle_conflict", "请先核实并结束原执行，再明确重新执行"
                    )
                candidate.update(
                    state="active",
                    once_key=uuid.uuid4().hex,
                    definition=definition,
                    definition_sha256=digest(definition),
                )
            else:
                targets = {
                    "pause": "paused",
                    "resume": "active",
                    "cancel": "cancelled",
                    "close": "closed",
                    "run-now": "active",
                }
                if action not in targets:
                    raise PersonalAgentError("invalid_input", "事项控制操作不正确")
                target = targets[action]
                if action == "run-now" and item["state"] != "active":
                    raise PersonalAgentError("item_inactive", "请先启用事项，再明确立即执行")
                if target != item["state"] and item["state"] in {"cancelled", "closed"}:
                    raise PersonalAgentError("lifecycle_conflict", "已结束事项不恢复到原单次执行")
                candidate["state"] = target
            if candidate != item:
                candidate.update(revision=item["revision"] + 1, updated_at=self._now())
                self._write_item(db, candidate)
            self._save_request(
                db,
                owner,
                request_id,
                operation,
                payload,
                candidate,
                request_fingerprint,
            )
            return candidate

    def update_definition(self, **kwargs):
        return self._mutate(operation="update_definition", **kwargs)

    def control(self, **kwargs):
        return self._mutate(operation="control", **kwargs)

    def restart(self, **kwargs):
        return self._mutate(operation="restart", **kwargs)

    @staticmethod
    def _semantics(intent):
        return digest(
            {
                key: intent[key]
                for key in (
                    "item_id",
                    "once_key",
                    "definition_sha256",
                    "workspace_key",
                    "schedule_id",
                    "event_id",
                    "actor_id",
                    "agent_version",
                    "input_refs",
                    "input_manifest",
                    "policy",
                    "main_policy_sha256",
                )
            }
            | {
                "task": intent["payload"]["task"],
                "tool_scope": intent["request"]["tool_scope"],
                "permission_mode": intent["request"]["permission_mode"],
            }
        )

    def _intent(self, db, owner, occurrence_id):
        row = db.execute(
            "SELECT * FROM personal_item_intents WHERE owner_key=? AND occurrence_id=?",
            (owner, occurrence_id),
        ).fetchone()
        if row is None:
            raise PersonalAgentError("not_found", "该事项执行记录不存在")
        try:
            intent = normalize_intent(row["prepared"])
        except PersonalAgentError as exc:
            raise _broken() from exc
        if any(
            row[key] != intent[key]
            for key in (
                "occurrence_id",
                "item_id",
                "once_key",
                "workspace_key",
                "event_id",
                "prepared_sha256",
            )
        ) or row["semantics_sha256"] != self._semantics(intent):
            raise _broken()
        self._item(db, owner, intent["item_id"])
        revision_row = db.execute(
            "SELECT snapshot,snapshot_sha256 FROM personal_item_revisions "
            "WHERE owner_key=? AND item_id=? AND revision=?",
            (owner, intent["item_id"], intent["item_revision"]),
        ).fetchone()
        if revision_row is None:
            raise _broken()
        original = self._decode(revision_row["snapshot"], revision_row["snapshot_sha256"])
        if (
            original["definition_sha256"] != intent["definition_sha256"]
            or original["once_key"] != intent["once_key"]
            or original["definition"]["route"]["workspace_key"] != intent["workspace_key"]
            or original["definition"]["assignee"]["id"] != intent["actor_id"]
            or original["definition"]["assignee"]["version"] != intent["agent_version"]
            or original["definition"]["policy"] != intent["policy"]
        ):
            raise _broken()
        references = {}
        rows = db.execute(
            "SELECT * FROM personal_item_reference_history WHERE owner_key=? AND occurrence_id=? "
            "ORDER BY sequence",
            (owner, occurrence_id),
        ).fetchall()
        for sequence, ref_row in enumerate(rows, 1):
            updated = self._decode(ref_row["references_json"], ref_row["references_sha256"])
            if (
                ref_row["sequence"] != sequence
                or set(updated) - _REFERENCE_FIELDS
                or any(
                    key not in updated or updated[key] != value for key, value in references.items()
                )
            ):
                raise _broken()
            references = updated
        head = db.execute(
            "SELECT sequence,references_sha256 FROM personal_item_reference_heads "
            "WHERE owner_key=? AND occurrence_id=?",
            (owner, occurrence_id),
        ).fetchone()
        if head is None or tuple(head) != (len(rows), digest(references)):
            raise _broken()
        for key, value in references.items():
            try:
                text(
                    value,
                    "执行权威编号",
                    512,
                    pattern=SHA256 if key == "delivery_sha256" else IDENTIFIER,
                )
            except PersonalAgentError as exc:
                raise _broken() from exc
        if "job_id" in references:
            fields = dict(intent["request"])
            fields["tool_scope"] = tuple(fields["tool_scope"])
            if references["job_id"] != agent_job_id_for_request(AgentWorkerRequest(**fields)):
                raise _broken()
        return {**intent, "references": references, "references_sha256": digest(references)}

    def prepare(self, *, owner_key, item_id, expected_definition_sha256, intent):
        owner, item_id = self._owner(owner_key), self._id(item_id)
        text(expected_definition_sha256, "事项定义摘要", 64, pattern=SHA256)
        intent = normalize_intent(intent)
        if not self._exists():
            raise PersonalAgentError("not_found", "该个人事项不存在")
        if (
            intent["item_id"] != item_id
            or intent["definition_sha256"] != expected_definition_sha256
        ):
            raise PersonalAgentError("binding_conflict", "执行意图与事项定义不一致")
        with self._connection(owner, write=True) as db:
            item = self._item(db, owner, item_id)
            prior = db.execute(
                "SELECT occurrence_id FROM personal_item_intents WHERE owner_key=? AND item_id=? "
                "AND once_key=?",
                (owner, item_id, intent["once_key"]),
            ).fetchone()
            if prior is not None:
                existing = self._intent(db, owner, prior[0])
                if self._semantics(existing) != self._semantics(intent):
                    raise PersonalAgentError(
                        "idempotency_conflict", "该单次委托已绑定其他输入或执行策略"
                    )
                return existing
            if item["state"] != "active":
                raise PersonalAgentError("item_inactive", "事项已暂停或结束，请核实后继续")
            if (
                item["once_key"] != intent["once_key"]
                or item["revision"] != intent["item_revision"]
                or item["definition_sha256"] != expected_definition_sha256
                or item["definition"]["route"]["workspace_key"] != intent["workspace_key"]
                or item["definition"]["assignee"]["id"] != intent["actor_id"]
                or item["definition"]["assignee"]["version"] != intent["agent_version"]
                or item["definition"]["policy"] != intent["policy"]
            ):
                raise PersonalAgentError("binding_conflict", "执行意图与当前事项身份或策略不一致")
            db.execute(
                "INSERT INTO personal_item_intents VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    owner,
                    intent["occurrence_id"],
                    item_id,
                    intent["once_key"],
                    intent["workspace_key"],
                    intent["event_id"],
                    intent["prepared_sha256"],
                    canonical(intent),
                    self._semantics(intent),
                ),
            )
            db.execute(
                "INSERT INTO personal_item_reference_heads VALUES (?,?,0,?)",
                (owner, intent["occurrence_id"], digest({})),
            )
            return self._intent(db, owner, intent["occurrence_id"])

    def get_intent(self, *, owner_key, occurrence_id):
        owner = self._owner(owner_key)
        occurrence_id = text(occurrence_id, "事项执行编号", 36, pattern=INTENT_ID)
        if not self._exists():
            raise PersonalAgentError("not_found", "该事项执行记录不存在")
        with self._connection(owner) as db:
            return self._intent(db, owner, occurrence_id)

    def find_intent(self, *, owner_key, item_id, once_key=None):
        owner, item_id = self._owner(owner_key), self._id(item_id)
        if not self._exists():
            return None
        with self._connection(owner) as db:
            item = self._item(db, owner, item_id)
            once = (
                item["once_key"]
                if once_key is None
                else text(once_key, "单次委托编号", 32, pattern=ONCE_KEY)
            )
            row = db.execute(
                "SELECT occurrence_id FROM personal_item_intents WHERE owner_key=? AND item_id=? "
                "AND once_key=?",
                (owner, item_id, once),
            ).fetchone()
            return None if row is None else self._intent(db, owner, row[0])

    def list_intents(self, *, owner_key, item_id=None, workspace_key=None, limit=100, offset=0):
        owner = self._owner(owner_key)
        integer(limit, "执行记录页大小", 1, 500)
        integer(offset, "执行记录页位置", 0)
        if item_id is not None:
            self._id(item_id)
        if workspace_key is not None:
            text(workspace_key, "工作区标识", 64, pattern=SHA256)
        if not self._exists():
            return []
        with self._connection(owner) as db:
            rows = db.execute(
                "SELECT occurrence_id FROM personal_item_intents WHERE owner_key=? "
                "AND (? IS NULL OR item_id=?) AND (? IS NULL OR workspace_key=?) "
                "ORDER BY occurrence_id LIMIT ? OFFSET ?",
                (owner, item_id, item_id, workspace_key, workspace_key, limit, offset),
            ).fetchall()
            return [self._intent(db, owner, row[0]) for row in rows]

    def bind(self, *, owner_key, occurrence_id, expected_prepared_sha256, references):
        owner = self._owner(owner_key)
        occurrence_id = text(occurrence_id, "事项执行编号", 36, pattern=INTENT_ID)
        text(expected_prepared_sha256, "执行意图摘要", 64, pattern=SHA256)
        references = normalized_object(references, "执行权威引用", maximum=16384)
        if not self._exists():
            raise PersonalAgentError("not_found", "该事项执行记录不存在")
        if not references or set(references) - _REFERENCE_FIELDS:
            raise PersonalAgentError("invalid_input", "执行引用为空或包含不支持字段")
        for key, value in references.items():
            text(
                value,
                "执行权威编号",
                512,
                pattern=SHA256 if key == "delivery_sha256" else IDENTIFIER,
            )
        with self._connection(owner, write=True) as db:
            intent = self._intent(db, owner, occurrence_id)
            if intent["prepared_sha256"] != expected_prepared_sha256:
                raise PersonalAgentError("version_conflict", "原执行意图摘要已变化")
            merged = {**intent["references"], **references}
            if any(
                key in intent["references"] and intent["references"][key] != value
                for key, value in references.items()
            ):
                raise PersonalAgentError("binding_conflict", "该执行已绑定其他权威记录")
            if "job_id" in merged:
                fields = dict(intent["request"])
                fields["tool_scope"] = tuple(fields["tool_scope"])
                if merged["job_id"] != agent_job_id_for_request(AgentWorkerRequest(**fields)):
                    raise PersonalAgentError("binding_conflict", "Job 编号与原始执行请求不一致")
            if "namespace" in merged and merged["namespace"] != (
                "personal-engine-v1:" + intent["workspace_key"]
            ):
                raise PersonalAgentError("binding_conflict", "执行权威命名空间与原工作区不一致")
            if merged != intent["references"]:
                sequence = (
                    db.execute(
                        "SELECT COUNT(*) FROM personal_item_reference_history WHERE owner_key=? "
                        "AND occurrence_id=?",
                        (owner, occurrence_id),
                    ).fetchone()[0]
                    + 1
                )
                db.execute(
                    "INSERT INTO personal_item_reference_history VALUES (?,?,?,?,?)",
                    (owner, occurrence_id, sequence, canonical(merged), digest(merged)),
                )
                changed = db.execute(
                    "UPDATE personal_item_reference_heads SET sequence=?,references_sha256=? "
                    "WHERE owner_key=? AND occurrence_id=? AND sequence=? AND references_sha256=?",
                    (
                        sequence,
                        digest(merged),
                        owner,
                        occurrence_id,
                        sequence - 1,
                        intent["references_sha256"],
                    ),
                ).rowcount
                if changed != 1:
                    raise _broken()
            return self._intent(db, owner, occurrence_id)

    def record_decision(self, *, owner_key, item_id, once_key, event_id, decision, detail):
        owner, item_id = self._owner(owner_key), self._id(item_id)
        text(once_key, "单次委托编号", 32, pattern=ONCE_KEY)
        text(event_id, "到期事件", 512, pattern=IDENTIFIER)
        if not isinstance(decision, str) or decision not in {
            "skip",
            "expired",
            "cancel-before-admission",
            "input-error",
        }:
            raise PersonalAgentError("invalid_input", "调度决定类型不正确")
        detail = text(detail, "调度决定说明", 4000, empty=True)
        if not self._exists():
            raise PersonalAgentError("not_found", "该个人事项不存在")
        with self._connection(owner, write=True) as db:
            self._item(db, owner, item_id)
            known_once = any(
                self._decode(row["snapshot"], row["snapshot_sha256"])["once_key"] == once_key
                for row in db.execute(
                    "SELECT snapshot,snapshot_sha256 FROM personal_item_revisions "
                    "WHERE owner_key=? AND item_id=?",
                    (owner, item_id),
                )
            )
            if not known_once:
                raise PersonalAgentError("binding_conflict", "调度决定不属于该事项的单次委托")
            row = db.execute(
                "SELECT * FROM personal_item_decisions WHERE owner_key=? AND item_id=? "
                "AND once_key=? AND event_id=?",
                (owner, item_id, once_key, event_id),
            ).fetchone()
            if row is not None:
                result = dict(row)
                checksum = result.pop("receipt_sha256")
                if digest(result) != checksum:
                    raise _broken()
                if (row["decision"], row["detail"]) != (decision, detail):
                    raise PersonalAgentError("idempotency_conflict", "该事件已记录其他调度决定")
                return {**result, "receipt_sha256": checksum}
            result = {
                "owner_key": owner,
                "item_id": item_id,
                "once_key": once_key,
                "event_id": event_id,
                "decision": decision,
                "detail": detail,
                "occurred_at": self._now(),
            }
            db.execute(
                "INSERT INTO personal_item_decisions VALUES (?,?,?,?,?,?,?,?)",
                (*result.values(), digest(result)),
            )
            return {**result, "receipt_sha256": digest(result)}

    def list_decisions(self, *, owner_key, item_id):
        """Read mechanical pre-admission decisions without dispatching or marking read."""
        owner, item_id = self._owner(owner_key), self._id(item_id)
        if not self._exists():
            return []
        with self._connection(owner) as db:
            self._item(db, owner, item_id)
            rows = db.execute(
                "SELECT * FROM personal_item_decisions WHERE owner_key=? AND item_id=? "
                "ORDER BY occurred_at,event_id",
                (owner, item_id),
            ).fetchall()
            result = []
            for row in rows:
                receipt = dict(row)
                checksum = receipt.pop("receipt_sha256")
                if digest(receipt) != checksum or receipt["decision"] not in {
                    "skip",
                    "expired",
                    "cancel-before-admission",
                    "input-error",
                }:
                    raise _broken()
                try:
                    text(receipt["once_key"], "单次委托编号", 32, pattern=ONCE_KEY)
                    text(receipt["event_id"], "到期事件", 512, pattern=IDENTIFIER)
                    text(receipt["detail"], "调度决定说明", 4000, empty=True)
                    timestamp(receipt["occurred_at"], "调度决定时间")
                except PersonalAgentError as exc:
                    raise _broken() from exc
                result.append({**receipt, "receipt_sha256": checksum})
            return result

    update_control = control
    prepare_intent = prepare
    bind_refs = bind
