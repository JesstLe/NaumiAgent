"""Immutable peer requests and once-only, digest-bound delivery receipts.

The root AgentJob owns execution authority. This database records message facts:
request, dispatch claim, receiving Run, exact result, and sender acknowledgement.
A delivery claim is never reclaimed here and cannot authorize a new Agent job.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from naumi_agent.personal.models import PersonalAgentError

_SCHEMA_VERSION = 1
_MESSAGE_ID = re.compile(r"^pm_[0-9a-f]{32}$")
_COLLABORATION_ID = re.compile(r"^pc_[0-9a-f]{32}$")
_AGENT_ID = re.compile(r"^pa_[0-9a-f]{32}$")
_STEP_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_TOOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_MAX_DEPTH = 8
_MAX_MESSAGES = 32
_MAX_RESPONSE_BYTES = 2 * 1024**2
_REQUEST_FIELDS = (
    "workspace_key",
    "collaboration_id",
    "step_id",
    "source_session_id",
    "sender_agent_id",
    "sender_version",
    "recipient_agent_id",
    "recipient_version",
    "request_id",
    "content",
    "context",
    "tool_names",
    "parent_message_id",
    "origin",
)
_GENERATED_FIELDS = ("id", "receiver_session_id", "receiver_task_id", "depth", "created_at")
_BINDING_FIELDS = ("root_job_id", "root_owner_id", "root_claim_epoch", "source_run_id")
_OUTCOMES = {"completed", "error", "timeout", "cancelled", "unknown"}
_TABLES = (
    """CREATE TABLE IF NOT EXISTS peer_messages (
        id TEXT PRIMARY KEY, workspace_key TEXT NOT NULL, collaboration_id TEXT NOT NULL,
        step_id TEXT NOT NULL, source_session_id TEXT NOT NULL,
        sender_agent_id TEXT NOT NULL, recipient_agent_id TEXT NOT NULL,
        request_id TEXT NOT NULL, receiver_session_id TEXT NOT NULL,
        request_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
        dispatch_token TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '',
        receipt_count INTEGER NOT NULL DEFAULT 0 CHECK(receipt_count >= 0),
        receipt_head_sha256 TEXT NOT NULL DEFAULT '',
        UNIQUE(workspace_key, source_session_id, request_id),
        UNIQUE(workspace_key, receiver_session_id)
    )""",
    """CREATE TABLE IF NOT EXISTS peer_message_receipts (
        message_id TEXT NOT NULL REFERENCES peer_messages(id),
        sequence INTEGER NOT NULL CHECK(sequence > 0), kind TEXT NOT NULL,
        data_json TEXT NOT NULL, created_at TEXT NOT NULL,
        previous_sha256 TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
        PRIMARY KEY(message_id, sequence)
    )""",
    "CREATE INDEX IF NOT EXISTS peer_message_scope ON peer_messages "
    "(workspace_key, collaboration_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS peer_message_sender ON peer_messages "
    "(workspace_key, collaboration_id, sender_agent_id)",
    "CREATE INDEX IF NOT EXISTS peer_message_recipient ON peer_messages "
    "(workspace_key, collaboration_id, recipient_agent_id)",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _error(code: str, message: str) -> PersonalAgentError:
    return PersonalAgentError(code, message)


def _invalid(message: str) -> PersonalAgentError:
    return _error("invalid_input", message)


def _corrupt() -> PersonalAgentError:
    return _error("storage_error", "Agent 消息与回执校验不一致，请恢复资料库后继续")


def _text(value: Any, field: str, limit: int, *, empty: bool = False, strip: bool = True) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field}必须是文字")
    result = value.strip() if strip else value
    if _CONTROL.search(value):
        raise _invalid(f"{field}包含无效控制字符")
    if not empty and not result.strip():
        raise _invalid(f"{field}不能为空")
    if len(result) > limit:
        raise _invalid(f"{field}最多允许 {limit} 个字符")
    try:
        result.encode("utf-8")
    except UnicodeError as exc:
        raise _invalid(f"{field}包含无效文字编码") from exc
    return result


def _identifier(value: Any, pattern: re.Pattern[str], field: str, limit: int = 128) -> str:
    result = _text(value, field, limit)
    if not pattern.fullmatch(result):
        raise _invalid(f"{field}格式不正确")
    return result


def _integer(value: Any, field: str, minimum: int = 1, maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _invalid(f"{field}必须是 {minimum} 至 {maximum} 之间的整数")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_object(value: Any, field: str, maximum: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{field}必须是 JSON 对象")

    def validate(item: Any, depth: int = 0) -> None:
        if depth > 32:
            raise _invalid(f"{field}嵌套过深")
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, str):
            _text(item, field, maximum, empty=True, strip=False)
            return
        if isinstance(item, float) and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise _invalid(f"{field}的字段名称必须是文字")
                _text(key, field, maximum, empty=True, strip=False)
                validate(child, depth + 1)
            return
        raise _invalid(f"{field}包含不支持的 JSON 值")

    try:
        validate(value)
        encoded = _canonical(value)
        if len(encoded.encode("utf-8")) > maximum:
            raise _invalid(f"{field}超过允许的大小")
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise _invalid(f"{field}不是有效的 JSON 资料") from exc


def _tools(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 128:
        raise _invalid("消息工具范围必须是最多 128 项的列表")
    result = [_identifier(item, _TOOL, "工具名称") for item in value]
    if len(set(result)) != len(result):
        raise _invalid("消息工具范围包含重复项目")
    return sorted(result)


def _timestamp(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include an offset")


def _result_binding(message: dict, result: dict) -> dict:
    return {
        "message_id": message["id"],
        "payload_sha256": message["payload_sha256"],
        **{key: message[key] for key in _BINDING_FIELDS},
        "dispatch_token_sha256": _digest(message["dispatch_token"]),
        "receiver_session_id": message["receiver_session_id"],
        "receiver_run_id": result["receiver_run_id"],
        "outcome": result["outcome"],
        "response_sha256": _digest(result["response"]),
        "error_sha256": _digest(result["error"]),
        "usage": result["usage"],
        "run_receipt_sha256": _digest(_canonical(result["run_receipt"])),
    }


class PersonalMessageStore:
    """Scoped SQLite message facts with an irreversible single-dispatch claim.

    All returned dictionaries are detached. ``dispatch_token`` is deliberately
    private and must be stripped by user-facing service projections. Read-only
    methods never create a missing database. A claim cannot be replaced after a
    crash: only the root Job authority can decide how to reconcile delivery.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir).expanduser().resolve()
        self._db_path = self._base_dir / "personal_messages.db"
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _open(self, *, readonly: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path.as_uri() + "?mode=ro" if readonly else self._db_path,
            uri=readonly,
            timeout=10,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            try:
                self._base_dir.mkdir(parents=True, exist_ok=True)
                for attempt in range(3):
                    conn = self._open()
                    try:
                        self._check_version(conn)
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("BEGIN IMMEDIATE")
                        self._check_version(conn)
                        for statement in _TABLES:
                            conn.execute(statement)
                        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                        conn.commit()
                        self._initialized = True
                        return
                    except sqlite3.OperationalError as exc:
                        conn.rollback()
                        if "locked" not in str(exc).lower() or attempt == 2:
                            raise
                        time.sleep(0.05 * (attempt + 1))
                    finally:
                        conn.close()
            except (OSError, sqlite3.Error) as exc:
                raise _error("storage_error", "Agent 消息资料库暂不可用，请检查目录后重试") from exc

    @staticmethod
    def _check_version(conn: sqlite3.Connection) -> None:
        if conn.execute("PRAGMA user_version").fetchone()[0] > _SCHEMA_VERSION:
            raise _error("unsupported_schema", "Agent 消息资料库来自较新版本，请先更新应用")

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            self._initialize()
        conn = None
        try:
            conn = self._open(readonly=not write)
            self._check_version(conn)
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            if conn is not None:
                conn.rollback()
            raise _error("storage_error", "Agent 消息资料库暂不可用，请稍后重试") from exc
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _scope(message_id: Any, workspace_key: Any) -> tuple[str, str]:
        return (
            _identifier(message_id, _MESSAGE_ID, "消息编号", 35),
            _text(workspace_key, "工作区标识", 2048),
        )

    @staticmethod
    def _raw(conn: sqlite3.Connection, message_id: str, workspace_key: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM peer_messages WHERE id=? AND workspace_key=?",
            (message_id, workspace_key),
        ).fetchone()
        if row is None:
            raise _error("not_found", "该 Agent 消息不存在，请刷新记录")
        return row

    def _append(
        self,
        conn: sqlite3.Connection,
        message_id: str,
        kind: str,
        data: dict,
        *,
        occurred_at: str | None = None,
    ) -> None:
        previous = conn.execute(
            "SELECT sequence,receipt_sha256 FROM peer_message_receipts "
            "WHERE message_id=? ORDER BY sequence DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        sequence = previous["sequence"] + 1 if previous is not None else 1
        payload = {
            "message_id": message_id,
            "sequence": sequence,
            "kind": kind,
            "data": data,
            "created_at": occurred_at or _now(),
            "previous_sha256": previous["receipt_sha256"] if previous is not None else "",
        }
        receipt_sha = _digest(_canonical(payload))
        conn.execute(
            "INSERT INTO peer_message_receipts VALUES (?,?,?,?,?,?,?)",
            (
                message_id,
                sequence,
                kind,
                _canonical(data),
                payload["created_at"],
                payload["previous_sha256"],
                receipt_sha,
            ),
        )
        conn.execute(
            "UPDATE peer_messages SET receipt_count=?,receipt_head_sha256=? WHERE id=?",
            (sequence, receipt_sha, message_id),
        )

    def _read(self, conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict, list[dict]]:
        try:
            request = json.loads(row["request_json"])
            if not isinstance(request, dict) or set(request) != set(
                _REQUEST_FIELDS + _GENERATED_FIELDS
            ):
                raise _corrupt()
            for key in (
                "id",
                "workspace_key",
                "collaboration_id",
                "step_id",
                "source_session_id",
                "sender_agent_id",
                "recipient_agent_id",
                "request_id",
                "receiver_session_id",
                "created_at",
            ):
                if request[key] != row[key]:
                    raise _corrupt()
            if (
                _digest(_canonical({k: request[k] for k in _REQUEST_FIELDS}))
                != row["payload_sha256"]
            ):
                raise _corrupt()
            _timestamp(request["created_at"])
            message = {
                **request,
                "payload_sha256": row["payload_sha256"],
                "dispatch_token": row["dispatch_token"],
                "root_job_id": "",
                "root_owner_id": "",
                "root_claim_epoch": 0,
                "source_run_id": "",
                "receiver_run_id": "",
                "response": "",
                "response_sha256": "",
                "result_sha256": "",
                "error": "",
                "outcome": "",
                "usage": {},
                "run_receipt": {},
                "dispatch_started_at": "",
                "received_at": "",
                "completed_at": "",
                "acknowledged_at": "",
                "acknowledged_by": "",
            }
            rows = conn.execute(
                "SELECT * FROM peer_message_receipts WHERE message_id=? ORDER BY sequence",
                (row["id"],),
            ).fetchall()
            receipts, seen, previous = [], set(), ""
            for index, receipt in enumerate(rows, start=1):
                data = json.loads(receipt["data_json"])
                kind = receipt["kind"]
                payload = {
                    "message_id": row["id"],
                    "sequence": receipt["sequence"],
                    "kind": kind,
                    "data": data,
                    "created_at": receipt["created_at"],
                    "previous_sha256": receipt["previous_sha256"],
                }
                if (
                    index != receipt["sequence"]
                    or receipt["previous_sha256"] != previous
                    or receipt["receipt_sha256"] != _digest(_canonical(payload))
                    or kind in seen
                    or not isinstance(data, dict)
                ):
                    raise _corrupt()
                _timestamp(receipt["created_at"])
                if kind == "requested":
                    if index != 1 or data != {
                        "payload_sha256": row["payload_sha256"],
                        "record_sha256": _digest(_canonical(request)),
                    }:
                        raise _corrupt()
                elif kind == "dispatch_started":
                    if (
                        seen != {"requested"}
                        or set(data) != set(_BINDING_FIELDS) | {"dispatch_token_sha256"}
                        or data["dispatch_token_sha256"] != _digest(row["dispatch_token"])
                    ):
                        raise _corrupt()
                    if not row["dispatch_token"]:
                        raise _corrupt()
                    message.update({key: data[key] for key in _BINDING_FIELDS})
                    message["dispatch_started_at"] = receipt["created_at"]
                elif kind == "received":
                    if (
                        "dispatch_started" not in seen
                        or "result" in seen
                        or set(data) != {"receiver_run_id"}
                        or not data["receiver_run_id"]
                    ):
                        raise _corrupt()
                    message.update(
                        receiver_run_id=data["receiver_run_id"], received_at=receipt["created_at"]
                    )
                elif kind == "result":
                    if "dispatch_started" not in seen or not row["result_json"]:
                        raise _corrupt()
                    result = json.loads(row["result_json"])
                    if (
                        set(result)
                        != {
                            "receiver_run_id",
                            "outcome",
                            "response",
                            "error",
                            "usage",
                            "run_receipt",
                            "response_sha256",
                            "result_sha256",
                        }
                        or result["outcome"] not in _OUTCOMES
                        or any(
                            not isinstance(result[field], expected)
                            for field, expected in (
                                ("response", str),
                                ("error", str),
                                ("usage", dict),
                                ("run_receipt", dict),
                            )
                        )
                    ):
                        raise _corrupt()
                    if result["receiver_run_id"] != message["receiver_run_id"]:
                        raise _corrupt()
                    if result["outcome"] == "completed" and (
                        not message["receiver_run_id"]
                        or result["run_receipt"].get("run_id") != message["receiver_run_id"]
                    ):
                        raise _corrupt()
                    response_sha = _digest(result["response"])
                    result_sha = _digest(_canonical(_result_binding(message, result)))
                    if (
                        result["response_sha256"] != response_sha
                        or result["result_sha256"] != result_sha
                        or data
                        != {
                            "result_sha256": result_sha,
                            "response_sha256": response_sha,
                            "outcome": result["outcome"],
                        }
                    ):
                        raise _corrupt()
                    message.update(result)
                    message["completed_at"] = receipt["created_at"]
                elif kind == "acknowledged":
                    if (
                        message["outcome"] != "completed"
                        or data.get("actor_kind") not in {"agent", "user"}
                        or data
                        != {
                            "sender_agent_id": request["sender_agent_id"],
                            "response_sha256": message["response_sha256"],
                            "result_sha256": message["result_sha256"],
                            "actor_kind": data["actor_kind"],
                        }
                    ):
                        raise _corrupt()
                    message["acknowledged_at"] = receipt["created_at"]
                    message["acknowledged_by"] = data["actor_kind"]
                else:
                    raise _corrupt()
                receipts.append(
                    {
                        "sequence": receipt["sequence"],
                        "kind": kind,
                        "data": data,
                        "created_at": receipt["created_at"],
                        "previous_sha256": receipt["previous_sha256"],
                        "receipt_sha256": receipt["receipt_sha256"],
                    }
                )
                seen.add(kind)
                previous = receipt["receipt_sha256"]
            if not rows or rows[0]["kind"] != "requested":
                raise _corrupt()
            if len(rows) != row["receipt_count"] or previous != row["receipt_head_sha256"]:
                raise _corrupt()
            if bool(row["dispatch_token"]) != ("dispatch_started" in seen):
                raise _corrupt()
            if bool(row["result_json"]) != ("result" in seen):
                raise _corrupt()
            return message, receipts
        except PersonalAgentError:
            raise
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
            RecursionError,
        ) as exc:
            raise _corrupt() from exc

    def _required(self, conn: sqlite3.Connection, message_id: str, workspace_key: str) -> dict:
        return self._read(conn, self._raw(conn, message_id, workspace_key))[0]

    def create(
        self,
        *,
        workspace_key: str,
        collaboration_id: str,
        step_id: str,
        source_session_id: str,
        sender_agent_id: str,
        sender_version: int,
        recipient_agent_id: str,
        recipient_version: int,
        request_id: str,
        content: str,
        context: str,
        tool_names: list[str],
        parent_message_id: str = "",
        origin: str = "agent",
    ) -> dict:
        parent_message_id = _text(parent_message_id, "上级消息编号", 35, empty=True)
        if parent_message_id:
            parent_message_id = _identifier(parent_message_id, _MESSAGE_ID, "上级消息编号", 35)
        if not isinstance(origin, str) or origin not in {"agent", "manual"}:
            raise _invalid("消息来源必须是 Agent 或用户手动操作")
        payload = {
            "workspace_key": _text(workspace_key, "工作区标识", 2048),
            "collaboration_id": _identifier(collaboration_id, _COLLABORATION_ID, "协作编号", 35),
            "step_id": _identifier(step_id, _STEP_ID, "步骤编号", 64),
            "source_session_id": _text(source_session_id, "发送会话编号", 128),
            "sender_agent_id": _identifier(sender_agent_id, _AGENT_ID, "发送 Agent 编号", 35),
            "sender_version": _integer(sender_version, "发送 Agent 版本"),
            "recipient_agent_id": _identifier(recipient_agent_id, _AGENT_ID, "接收 Agent 编号", 35),
            "recipient_version": _integer(recipient_version, "接收 Agent 版本"),
            "request_id": _identifier(request_id, _REQUEST_ID, "请求编号"),
            "content": _text(content, "消息内容", 16000, strip=False),
            "context": _text(context, "共享上下文", 32000, empty=True, strip=False),
            "tool_names": _tools(tool_names),
            "parent_message_id": parent_message_id,
            "origin": origin,
        }
        payload_sha = _digest(_canonical(payload))
        with self._connection(write=True) as conn:
            existing = conn.execute(
                "SELECT * FROM peer_messages WHERE workspace_key=? "
                "AND source_session_id=? AND request_id=?",
                (payload["workspace_key"], payload["source_session_id"], payload["request_id"]),
            ).fetchone()
            if existing is not None:
                message = self._read(conn, existing)[0]
                if message["payload_sha256"] != payload_sha:
                    raise _error(
                        "idempotency_conflict", "该请求编号已用于其他 Agent 消息，请保留原请求"
                    )
                return message
            depth = 0
            ancestor_id = parent_message_id
            while ancestor_id:
                ancestor = self._required(conn, ancestor_id, payload["workspace_key"])
                if (
                    ancestor["collaboration_id"] != payload["collaboration_id"]
                    or ancestor["step_id"] != payload["step_id"]
                ):
                    raise _error("binding_conflict", "上级消息不属于本次协作步骤")
                if ancestor_id == parent_message_id:
                    if (
                        ancestor["recipient_agent_id"] != payload["sender_agent_id"]
                        or ancestor["recipient_version"] != payload["sender_version"]
                        or ancestor["receiver_session_id"] != payload["source_session_id"]
                    ):
                        raise _error("binding_conflict", "发送 Agent、版本或会话与上级接收方不一致")
                    depth = ancestor["depth"] + 1
                    if depth > _MAX_DEPTH:
                        raise _error(
                            "peer_depth", "Agent 互询层级已达到上限，请由主 Agent 整理后继续"
                        )
                if all(
                    ancestor[key] == payload[key]
                    for key in (
                        "recipient_agent_id",
                        "content",
                        "context",
                    )
                ):
                    raise _error("peer_cycle", "调用链中已出现相同资料请求，已停止重复互询")
                ancestor_id = ancestor["parent_message_id"]
            count = conn.execute(
                "SELECT COUNT(*) FROM peer_messages WHERE workspace_key=? "
                "AND collaboration_id=? AND step_id=?",
                (payload["workspace_key"], payload["collaboration_id"], payload["step_id"]),
            ).fetchone()[0]
            if count >= _MAX_MESSAGES:
                raise _error("peer_limit", "本步骤的 Agent 消息已达到上限，请先整理现有结果")
            identity = uuid.uuid4().hex
            request = {
                **payload,
                "id": "pm_" + identity,
                "receiver_session_id": uuid.uuid4().hex,
                "receiver_task_id": "peer-" + identity,
                "depth": depth,
                "created_at": _now(),
            }
            conn.execute(
                "INSERT INTO peer_messages "
                "(id,workspace_key,collaboration_id,step_id,source_session_id,sender_agent_id,"
                "recipient_agent_id,request_id,receiver_session_id,request_json,payload_sha256,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    request[key]
                    for key in (
                        "id",
                        "workspace_key",
                        "collaboration_id",
                        "step_id",
                        "source_session_id",
                        "sender_agent_id",
                        "recipient_agent_id",
                        "request_id",
                        "receiver_session_id",
                    )
                )
                + (_canonical(request), payload_sha, request["created_at"]),
            )
            self._append(
                conn,
                request["id"],
                "requested",
                {
                    "payload_sha256": payload_sha,
                    "record_sha256": _digest(_canonical(request)),
                },
                occurred_at=request["created_at"],
            )
            return self._required(conn, request["id"], payload["workspace_key"])

    def get(self, message_id: str, workspace_key: str) -> dict:
        message_id, workspace_key = self._scope(message_id, workspace_key)
        if not self._db_path.exists():
            raise _error("not_found", "该 Agent 消息不存在，请刷新记录")
        with self._connection() as conn:
            return self._required(conn, message_id, workspace_key)

    def list(
        self,
        workspace_key: str,
        collaboration_id: str,
        *,
        agent_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        collaboration_id = _identifier(collaboration_id, _COLLABORATION_ID, "协作编号", 35)
        if agent_id is not None:
            agent_id = _identifier(agent_id, _AGENT_ID, "Agent 编号", 35)
        limit = _integer(limit, "消息数量", 1, 500)
        offset = _integer(offset, "消息起点", 0, 9_223_372_036_854_775_807)
        if not self._db_path.exists():
            return []
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM peer_messages WHERE workspace_key=? AND collaboration_id=? "
                "AND (? IS NULL OR sender_agent_id=? OR recipient_agent_id=?) "
                "ORDER BY created_at,id LIMIT ? OFFSET ?",
                (workspace_key, collaboration_id, agent_id, agent_id, agent_id, limit, offset),
            ).fetchall()
            return [self._read(conn, row)[0] for row in rows]

    def lookup_session(self, workspace_key: str, session_id: str) -> dict | None:
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        session_id = _text(session_id, "接收会话编号", 128)
        if not self._db_path.exists():
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM peer_messages WHERE workspace_key=? AND receiver_session_id=?",
                (workspace_key, session_id),
            ).fetchone()
            return self._read(conn, row)[0] if row is not None else None

    def events(self, message_id: str, workspace_key: str) -> list[dict]:
        message_id, workspace_key = self._scope(message_id, workspace_key)
        if not self._db_path.exists():
            raise _error("not_found", "该 Agent 消息不存在，请刷新记录")
        with self._connection() as conn:
            return self._read(conn, self._raw(conn, message_id, workspace_key))[1]

    def claim(
        self,
        message_id: str,
        workspace_key: str,
        *,
        root_job_id: str,
        root_owner_id: str,
        root_claim_epoch: int,
        source_run_id: str,
        dispatch_token: str,
    ) -> bool:
        message_id, workspace_key = self._scope(message_id, workspace_key)
        binding = {
            "root_job_id": _identifier(root_job_id, _IDENTIFIER, "根执行编号"),
            "root_owner_id": _identifier(root_owner_id, _IDENTIFIER, "执行所有者编号"),
            "root_claim_epoch": _integer(root_claim_epoch, "执行租约版本", 1, 2**63 - 1),
            "source_run_id": _identifier(source_run_id, _IDENTIFIER, "发送执行记录编号"),
        }
        dispatch_token = _identifier(dispatch_token, _TOKEN, "派送凭据", 256)
        with self._connection(write=True) as conn:
            message = self._required(conn, message_id, workspace_key)
            if message["dispatch_token"]:
                if any(message[key] != binding[key] for key in _BINDING_FIELDS):
                    raise _error("binding_conflict", "消息已绑定其他执行或租约，已停止重复派送")
                return False
            if message["parent_message_id"]:
                parent = self._required(conn, message["parent_message_id"], workspace_key)
                if (
                    any(parent[key] != binding[key] for key in _BINDING_FIELDS[:3])
                    or parent["receiver_run_id"] != binding["source_run_id"]
                ):
                    raise _error("binding_conflict", "嵌套消息与上级执行归属不一致")
            conn.execute(
                "UPDATE peer_messages SET dispatch_token=? WHERE id=?", (dispatch_token, message_id)
            )
            self._append(
                conn,
                message_id,
                "dispatch_started",
                {
                    **binding,
                    "dispatch_token_sha256": _digest(dispatch_token),
                },
            )
            return True

    @staticmethod
    def _require_token(message: dict, dispatch_token: str) -> None:
        if not message["dispatch_token"] or message["dispatch_token"] != dispatch_token:
            raise _error("binding_conflict", "Agent 消息派送凭据已失效，请核对原始执行")

    def mark_received(
        self, message_id: str, workspace_key: str, *, dispatch_token: str, receiver_run_id: str
    ) -> dict:
        message_id, workspace_key = self._scope(message_id, workspace_key)
        dispatch_token = _identifier(dispatch_token, _TOKEN, "派送凭据", 256)
        receiver_run_id = _identifier(receiver_run_id, _IDENTIFIER, "接收执行记录编号")
        with self._connection(write=True) as conn:
            message = self._required(conn, message_id, workspace_key)
            self._require_token(message, dispatch_token)
            if message["receiver_run_id"]:
                if message["receiver_run_id"] != receiver_run_id:
                    raise _error("binding_conflict", "消息已由另一条执行记录接收")
                return message
            if message["outcome"]:
                raise _error("binding_conflict", "消息已有结果，已停止补写新的接收记录")
            self._append(conn, message_id, "received", {"receiver_run_id": receiver_run_id})
            return self._required(conn, message_id, workspace_key)

    def complete(
        self,
        message_id: str,
        workspace_key: str,
        *,
        dispatch_token: str,
        receiver_run_id: str,
        outcome: str,
        response: str,
        error: str = "",
        usage: dict | None = None,
        run_receipt: dict | None = None,
    ) -> dict:
        message_id, workspace_key = self._scope(message_id, workspace_key)
        dispatch_token = _identifier(dispatch_token, _TOKEN, "派送凭据", 256)
        receiver_run_id = _text(receiver_run_id, "接收执行记录编号", 128, empty=True)
        if receiver_run_id:
            receiver_run_id = _identifier(receiver_run_id, _IDENTIFIER, "接收执行记录编号")
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise _invalid("消息结果类型不正确")
        response = _text(response, "Agent 回答", _MAX_RESPONSE_BYTES, empty=True, strip=False)
        if len(response.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise _invalid("Agent 回答最多允许 2 MB，请改用明确的产物引用")
        error = _text(error, "消息错误说明", 16000, empty=True, strip=False)
        usage = _json_object({} if usage is None else usage, "用量记录", 16384)
        run_receipt = _json_object({} if run_receipt is None else run_receipt, "执行回执", 65536)
        if outcome == "completed" and (
            not receiver_run_id or run_receipt.get("run_id") != receiver_run_id
        ):
            raise _invalid("完成的消息必须绑定接收执行记录和对应回执")
        if run_receipt and run_receipt.get("run_id") != receiver_run_id:
            raise _invalid("执行回执与接收记录不一致")
        result = {
            "receiver_run_id": receiver_run_id,
            "outcome": outcome,
            "response": response,
            "error": error,
            "usage": usage,
            "run_receipt": run_receipt,
            "response_sha256": _digest(response),
        }
        with self._connection(write=True) as conn:
            message = self._required(conn, message_id, workspace_key)
            self._require_token(message, dispatch_token)
            if message["receiver_run_id"] and message["receiver_run_id"] != receiver_run_id:
                raise _error("binding_conflict", "消息结果与接收执行记录不一致")
            result["result_sha256"] = _digest(_canonical(_result_binding(message, result)))
            if message["outcome"]:
                if message["result_sha256"] != result["result_sha256"]:
                    raise _error("idempotency_conflict", "消息已有不同结果，请保留原始回执")
                return message
            if receiver_run_id and not message["receiver_run_id"]:
                self._append(conn, message_id, "received", {"receiver_run_id": receiver_run_id})
            conn.execute(
                "UPDATE peer_messages SET result_json=? WHERE id=?",
                (_canonical(result), message_id),
            )
            self._append(
                conn,
                message_id,
                "result",
                {
                    "result_sha256": result["result_sha256"],
                    "response_sha256": result["response_sha256"],
                    "outcome": outcome,
                },
            )
            return self._required(conn, message_id, workspace_key)

    def acknowledge(
        self,
        message_id: str,
        workspace_key: str,
        *,
        sender_agent_id: str,
        expected_response_sha256: str,
        actor_kind: str = "agent",
    ) -> dict:
        message_id, workspace_key = self._scope(message_id, workspace_key)
        sender_agent_id = _identifier(sender_agent_id, _AGENT_ID, "发送 Agent 编号", 35)
        expected_response_sha256 = _identifier(
            expected_response_sha256,
            _SHA256,
            "回答摘要",
            64,
        )
        if not isinstance(actor_kind, str) or actor_kind not in {"agent", "user"}:
            raise _invalid("回答确认者必须是 Agent 或用户")
        with self._connection(write=True) as conn:
            message = self._required(conn, message_id, workspace_key)
            if message["sender_agent_id"] != sender_agent_id:
                raise _error("message_scope_denied", "只有原发送 Agent 可以确认这条回答")
            if message["outcome"] != "completed":
                raise _error("message_not_ready", "该消息尚无已完成的回答，请查看真实执行状态")
            if message["response_sha256"] != expected_response_sha256:
                raise _error("binding_conflict", "回答版本已变更，请核对原始回答摘要")
            if not message["acknowledged_at"]:
                self._append(
                    conn,
                    message_id,
                    "acknowledged",
                    {
                        "sender_agent_id": sender_agent_id,
                        "response_sha256": expected_response_sha256,
                        "result_sha256": message["result_sha256"],
                        "actor_kind": actor_kind,
                    },
                )
                message = self._required(conn, message_id, workspace_key)
            return message
