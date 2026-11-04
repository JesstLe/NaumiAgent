"""Immutable collaboration plans and an ordered, idempotent handoff journal.

Execution state and terminal payloads belong to AgentJobStore. This store keeps
only the user's plan, stable execution identities, cancellation intent, and
observations. No observation here can make an Agent job completed or replayable.
"""

from __future__ import annotations

import hashlib
import hmac
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

from naumi_agent.config.credentials import resolve_runtime_payload_key
from naumi_agent.daemons.permission_decisions import permission_arguments_sha256
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.peer_models import dispatch, grants
from naumi_agent.safety.payload_envelope import (
    PayloadEnvelope,
    RuntimePayloadKey,
    open_runtime_payload,
    seal_runtime_payload,
)

_SCHEMA_VERSION = 1
_COLLABORATION_ID = re.compile(r"^pc_[0-9a-f]{32}$")
_AGENT_ID = re.compile(r"^pa_[0-9a-f]{32}$")
_STEP_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_EVENT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_EVENT_KIND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_STEP_FIELDS = frozenset({"id", "agent_id", "agent_version", "task", "depends_on"})
_MAX_EVENT_BYTES = 64 * 1024
_MAX_PERMISSION_PAYLOAD_BYTES = 512 * 1024
_PERMISSION_REF = "permission_payload_ref"
_TABLES = (
    """CREATE TABLE IF NOT EXISTS collaborations (
        id TEXT PRIMARY KEY,
        workspace_key TEXT NOT NULL,
        source_session_id TEXT NOT NULL,
        source_agent_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
        UNIQUE(workspace_key, source_session_id, request_id)
    )""",
    """CREATE TABLE IF NOT EXISTS collaboration_events (
        collaboration_id TEXT NOT NULL REFERENCES collaborations(id),
        sequence INTEGER NOT NULL CHECK(sequence > 0),
        event_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        step_id TEXT NOT NULL,
        data_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(collaboration_id, sequence),
        UNIQUE(collaboration_id, event_key)
    )""",
    """CREATE TABLE IF NOT EXISTS collaboration_sessions (
        collaboration_id TEXT NOT NULL REFERENCES collaborations(id),
        workspace_key TEXT NOT NULL,
        session_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        PRIMARY KEY(workspace_key, session_id),
        UNIQUE(collaboration_id, step_id)
    )""",
    "CREATE INDEX IF NOT EXISTS collaborations_scope "
    "ON collaborations(workspace_key, source_session_id, created_at DESC, id)",
    # Additive schema-1 extension: older readers can still open plans and journals.
    """CREATE TABLE IF NOT EXISTS collaboration_permission_payloads (
        collaboration_id TEXT NOT NULL REFERENCES collaborations(id),
        event_key TEXT NOT NULL,
        step_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        call_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        payload_bytes INTEGER NOT NULL CHECK(payload_bytes > 0),
        envelope_json TEXT NOT NULL,
        PRIMARY KEY(collaboration_id, event_key)
    )""",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _invalid(message: str) -> PersonalAgentError:
    return PersonalAgentError("invalid_input", message)


def _text(value: Any, field: str, limit: int, *, required: bool = True, strip: bool = True) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field}必须是文字")
    if _CONTROL.search(value):
        raise _invalid(f"{field}包含无效控制字符")
    result = value.strip() if strip else value
    if required and not result.strip():
        raise _invalid(f"{field}不能为空")
    if len(result) > limit:
        raise _invalid(f"{field}最多允许 {limit} 个字符")
    try:
        result.encode("utf-8")
    except UnicodeError as exc:
        raise _invalid(f"{field}包含无效文字编码") from exc
    return result


def _identifier(value: Any, pattern: re.Pattern[str], field: str, limit: int) -> str:
    result = _text(value, field, limit)
    if not pattern.fullmatch(result):
        raise _invalid(f"{field}格式不正确")
    return result


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _invalid(f"{field}必须是 {minimum} 至 {maximum} 之间的整数")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise _invalid("协作资料包含无效文字编码") from exc


def _steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise _invalid("协作步骤必须是 1 至 12 项的列表")
    steps: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in value:
        if (
            not isinstance(raw, dict)
            or not _STEP_FIELDS <= set(raw)
            or set(raw) - _STEP_FIELDS - {"message"}
        ):
            raise _invalid("协作步骤缺少必填字段或包含不支持的字段")
        step_id = _identifier(raw["id"], _STEP_ID, "步骤编号", 64)
        if step_id in ids:
            raise _invalid("协作步骤编号重复")
        ids.add(step_id)
        agent_id = _identifier(raw["agent_id"], _AGENT_ID, "助理编号", 35)
        version = _integer(raw["agent_version"], "助理版本", 1, 2_147_483_647)
        task = _text(raw["task"], "步骤任务", 16000, strip=False)
        dependencies = raw["depends_on"]
        if not isinstance(dependencies, list) or len(dependencies) > 11:
            raise _invalid("前置步骤必须是最多 11 项的列表")
        normalized = [_identifier(item, _STEP_ID, "前置步骤编号", 64) for item in dependencies]
        if len(set(normalized)) != len(normalized):
            raise _invalid("前置步骤包含重复项目")
        if step_id in normalized:
            raise _invalid("步骤不能依赖自己")
        steps.append(
            {
                "id": step_id,
                "agent_id": agent_id,
                "agent_version": version,
                "task": task,
                "depends_on": sorted(normalized),
            }
        )
        if "message" in raw:
            steps[-1]["message"] = dispatch(raw["message"])
    for step in steps:
        if set(step["depends_on"]) - ids:
            raise _invalid("前置步骤不存在，请检查步骤编号")
    pending = {step["id"]: set(step["depends_on"]) for step in steps}
    resolved: set[str] = set()
    while pending:
        ready = {key for key, dependencies in pending.items() if dependencies <= resolved}
        if not ready:
            raise _invalid("协作步骤存在循环依赖，请调整交接顺序")
        resolved.update(ready)
        for key in ready:
            del pending[key]
    return steps


def _event_data(value: Any, *, maximum_bytes: int = _MAX_EVENT_BYTES) -> str:
    if not isinstance(value, dict):
        raise _invalid("协作事件内容必须是 JSON 对象")

    def validate(item: Any, depth: int = 0) -> None:
        if depth > 64:
            raise _invalid("协作事件内容嵌套过深")
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float) and math.isfinite(item):
            return
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise _invalid("协作事件字段名称必须是文字")
            for child in item.values():
                validate(child, depth + 1)
            return
        if isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
            return
        raise _invalid("协作事件内容包含不支持的 JSON 值")

    validate(value)
    try:
        encoded = _canonical(value)
        if len(encoded.encode("utf-8")) > maximum_bytes:
            raise _invalid(f"协作事件内容最多允许 {maximum_bytes // 1024} KB")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise _invalid("协作事件内容不是有效的 JSON 资料") from exc
    return encoded


def _permission_data(value: Any) -> str:
    """Keep the existing arguments limit distinct from envelope metadata overhead."""
    if not isinstance(value, dict) or not isinstance(value.get("arguments"), dict):
        raise _invalid("权限确认资料必须包含完整工具参数")
    try:
        permission_arguments_sha256(value["arguments"])
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid("权限确认参数无效或超过 256 KB") from exc
    for field, label, limit in (
        ("session_id", "权限确认会话编号", 128),
        ("call_id", "权限确认调用编号", 128),
        ("tool_name", "权限确认工具名称", 256),
    ):
        _text(value.get(field), label, limit, strip=False)
    if _PERMISSION_REF in value:
        raise _invalid("权限确认资料含保留字段")
    return _event_data(value, maximum_bytes=_MAX_PERMISSION_PAYLOAD_BYTES)


def _permission_aad(
    plan: dict[str, Any],
    key: str,
    step_id: str,
    session_id: str,
    call_id: str,
    digest: str,
) -> bytes:
    return _canonical(
        {
            "domain": "naumi-personal-permission-payload-v1",
            "workspace_key": plan["workspace_key"],
            "collaboration_id": plan["id"],
            "event_key": key,
            "step_id": step_id,
            "session_id": session_id,
            "call_id": call_id,
            "payload_sha256": digest,
        }
    ).encode("utf-8")


def _permission_key() -> RuntimePayloadKey:
    # Resolve only when sealing/opening overflow data; never during store startup.
    try:
        return RuntimePayloadKey.from_bytes(resolve_runtime_payload_key())
    except Exception as exc:
        raise PersonalAgentError(
            "storage_error", "权限确认资料密钥暂不可用，请恢复凭据后重试"
        ) from exc


def _permission_step(
    plan: dict[str, Any],
    step_id: str,
    payload: dict[str, Any],
    base_dir: Path,
) -> None:
    step = next((item for item in plan["steps"] if item["id"] == step_id), None)
    if step is None:
        raise PersonalAgentError("binding_conflict", "权限确认与协作执行会话不一致")
    peer_message_id = payload.get("peer_message_id")
    if not peer_message_id:
        if payload["session_id"] != step["session_id"] or (
            payload.get("actor_id") and payload["actor_id"] != step["agent_id"]
        ):
            raise PersonalAgentError("binding_conflict", "权限确认与协作执行会话不一致")
        return
    # Receiver sessions belong to immutable peer messages, not the root step.
    # get() is read-only and never creates a missing peer database.
    from naumi_agent.personal.message_store import PersonalMessageStore

    try:
        message = PersonalMessageStore(base_dir.parent / "messages").get(
            peer_message_id,
            plan["workspace_key"],
        )
    except PersonalAgentError as exc:
        raise PersonalAgentError("binding_conflict", "权限确认的专员消息不存在或校验失败") from exc
    if (
        message["workspace_key"] != plan["workspace_key"]
        or message["collaboration_id"] != plan["id"]
        or message["step_id"] != step_id
        or message["receiver_session_id"] != payload["session_id"]
        or message["recipient_agent_id"] != payload.get("actor_id")
    ):
        raise PersonalAgentError("binding_conflict", "权限确认与已登记的专员消息不一致")


def _plan(row: sqlite3.Row) -> dict[str, Any]:
    try:
        result = json.loads(row["plan_json"])
        result["cancel_requested"] = bool(row["cancel_requested"])
        result["updated_at"] = row["updated_at"]
        return result
    except (TypeError, ValueError, KeyError) as exc:
        raise PersonalAgentError("storage_error", "协作计划资料不完整，请恢复资料库后继续") from exc


def _event(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return {
            "sequence": row["sequence"],
            "key": row["event_key"],
            "kind": row["kind"],
            "step_id": row["step_id"],
            "data": json.loads(row["data_json"]),
            "created_at": row["created_at"],
        }
    except (TypeError, ValueError, KeyError) as exc:
        raise PersonalAgentError("storage_error", "协作交接记录不完整，请恢复资料库后继续") from exc


class CollaborationStore:
    """One scope-checked plan and append-only journal, not an execution state machine.

    Construction does no I/O. Each returned JSON-compatible record is detached
    from stored state. Writes use SQLite's cross-process transaction lock; the
    in-process lock only serializes lazy schema setup on a single instance.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir).expanduser().resolve()
        self._db_path = self._base_dir / "collaborations.db"
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10, isolation_level=None)
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
                        if conn.execute("PRAGMA user_version").fetchone()[0] > _SCHEMA_VERSION:
                            raise PersonalAgentError(
                                "unsupported_schema", "协作资料库来自较新版本，请先更新应用"
                            )
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("BEGIN IMMEDIATE")
                        if conn.execute("PRAGMA user_version").fetchone()[0] > _SCHEMA_VERSION:
                            raise PersonalAgentError(
                                "unsupported_schema", "协作资料库来自较新版本，请先更新应用"
                            )
                        for statement in _TABLES:
                            conn.execute(statement)
                        conn.execute("PRAGMA user_version=1")
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
                raise PersonalAgentError(
                    "storage_error", "协作资料库暂时不可用，请检查目录或稍后重试"
                ) from exc

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self._initialize()
        conn: sqlite3.Connection | None = None
        try:
            conn = self._open()
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            if conn is not None:
                conn.rollback()
            raise PersonalAgentError("storage_error", "协作资料保存未完成，请稍后重试") from exc
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _required(
        conn: sqlite3.Connection, collaboration_id: str, workspace_key: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM collaborations WHERE id=? AND workspace_key=?",
            (collaboration_id, workspace_key),
        ).fetchone()
        if row is None:
            raise PersonalAgentError("not_found", "该协作不存在，请刷新列表")
        return row

    @staticmethod
    def _scope(collaboration_id: Any, workspace_key: Any) -> tuple[str, str]:
        return (
            _identifier(collaboration_id, _COLLABORATION_ID, "协作编号", 35),
            _text(workspace_key, "工作区标识", 2048),
        )

    def create(
        self,
        *,
        workspace_key: str,
        source_session_id: str,
        source_agent_id: str,
        request_id: str,
        steps: list[dict[str, Any]],
        context: str = "",
        title: str = "",
        peer_agents: list[dict] | None = None,
    ) -> dict[str, Any]:
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        source_session_id = _text(source_session_id, "来源会话编号", 128)
        source_agent_id = _text(source_agent_id, "来源助理编号", 35, required=False)
        if source_agent_id:
            source_agent_id = _identifier(source_agent_id, _AGENT_ID, "来源助理编号", 35)
        request_id = _identifier(request_id, _REQUEST_ID, "请求编号", 128)
        normalized_steps = _steps(steps)
        payload = {
            "workspace_key": workspace_key,
            "source_session_id": source_session_id,
            "source_agent_id": source_agent_id,
            "request_id": request_id,
            "title": _text(title, "协作名称", 160, required=False),
            "context": _text(context, "共享上下文", 32000, required=False, strip=False),
            "steps": normalized_steps,
        }
        normalized_grants = grants(peer_agents)
        if normalized_grants:
            for item in normalized_grants:
                item.setdefault("tool_names", [])
            payload["peer_agents"] = normalized_grants
        payload_sha256 = _sha256(_canonical(payload))
        with self._connection(write=True) as conn:
            existing = conn.execute(
                "SELECT * FROM collaborations "
                "WHERE workspace_key=? AND source_session_id=? AND request_id=?",
                (workspace_key, source_session_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != payload_sha256:
                    raise PersonalAgentError(
                        "idempotency_conflict", "该请求编号已用于不同协作，请核对原任务后重新发起"
                    )
                return _plan(existing)
            collaboration_id = f"pc_{uuid.uuid4().hex}"
            now = _now()
            plan = {
                "id": collaboration_id,
                **payload,
                "created_at": now,
                "steps": [
                    {
                        **step,
                        "task_id": f"{collaboration_id}-{step['id']}",
                        "session_id": uuid.uuid4().hex,
                    }
                    for step in normalized_steps
                ],
            }
            conn.execute(
                "INSERT INTO collaborations "
                "(id,workspace_key,source_session_id,source_agent_id,request_id,"
                "payload_sha256,plan_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    collaboration_id,
                    workspace_key,
                    source_session_id,
                    source_agent_id,
                    request_id,
                    payload_sha256,
                    _canonical(plan),
                    now,
                    now,
                ),
            )
            conn.executemany(
                "INSERT INTO collaboration_sessions VALUES (?,?,?,?)",
                [
                    (collaboration_id, workspace_key, step["session_id"], step["id"])
                    for step in plan["steps"]
                ],
            )
            return _plan(self._required(conn, collaboration_id, workspace_key))

    def find_request(
        self, workspace_key: str, source_session_id: str, request_id: str
    ) -> dict[str, Any] | None:
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        source_session_id = _text(source_session_id, "来源会话编号", 128)
        request_id = _identifier(request_id, _REQUEST_ID, "请求编号", 128)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM collaborations WHERE workspace_key=? "
                "AND source_session_id=? AND request_id=?",
                (workspace_key, source_session_id, request_id),
            ).fetchone()
            return _plan(row) if row is not None else None

    def get(self, collaboration_id: str, workspace_key: str) -> dict[str, Any]:
        collaboration_id, workspace_key = self._scope(collaboration_id, workspace_key)
        with self._connection() as conn:
            return _plan(self._required(conn, collaboration_id, workspace_key))

    def list(
        self,
        workspace_key: str,
        source_session_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        *,
        oldest_first: bool = False,
    ) -> list[dict[str, Any]]:
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        limit = _integer(limit, "协作数量", 1, 500)
        offset = _integer(offset, "协作列表起点", 0, 9_223_372_036_854_775_807)
        if not isinstance(oldest_first, bool):
            raise _invalid("协作排序必须是布尔值")
        ordering = "ASC" if oldest_first else "DESC"
        if source_session_id is not None:
            source_session_id = _text(source_session_id, "来源会话编号", 128)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM collaborations WHERE workspace_key=? "
                "AND (? IS NULL OR source_session_id=?) "
                f"ORDER BY created_at {ordering},id {ordering} LIMIT ? OFFSET ?",
                (workspace_key, source_session_id, source_session_id, limit, offset),
            ).fetchall()
            return [_plan(row) for row in rows]

    def lookup_for_session(self, workspace_key: str, session_id: str) -> dict[str, Any] | None:
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        session_id = _text(session_id, "协作会话编号", 128)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT c.* FROM collaborations c JOIN collaboration_sessions s "
                "ON s.collaboration_id=c.id AND s.workspace_key=c.workspace_key "
                "WHERE s.workspace_key=? AND s.session_id=?",
                (workspace_key, session_id),
            ).fetchone()
            return _plan(row) if row is not None else None

    def request_cancel(self, collaboration_id: str, workspace_key: str) -> dict[str, Any]:
        collaboration_id, workspace_key = self._scope(collaboration_id, workspace_key)
        with self._connection(write=True) as conn:
            row = self._required(conn, collaboration_id, workspace_key)
            if not row["cancel_requested"]:
                conn.execute(
                    "UPDATE collaborations SET cancel_requested=1,updated_at=? "
                    "WHERE id=? AND workspace_key=?",
                    (_now(), collaboration_id, workspace_key),
                )
            return _plan(self._required(conn, collaboration_id, workspace_key))

    def append_event(
        self,
        collaboration_id: str,
        workspace_key: str,
        *,
        key: str,
        kind: str,
        step_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        collaboration_id, workspace_key = self._scope(collaboration_id, workspace_key)
        key = _identifier(key, _EVENT_KEY, "交接记录编号", 256)
        kind = _identifier(kind, _EVENT_KIND, "交接记录类型", 64)
        step_id = _text(step_id, "步骤编号", 64, required=False)
        if step_id:
            step_id = _identifier(step_id, _STEP_ID, "步骤编号", 64)
        data_json = _event_data({} if data is None else data)
        with self._connection(write=True) as conn:
            plan = _plan(self._required(conn, collaboration_id, workspace_key))
            return self._append_record(conn, plan, key, kind, step_id, data_json)

    @staticmethod
    def _append_record(
        conn: sqlite3.Connection,
        plan: dict[str, Any],
        key: str,
        kind: str,
        step_id: str,
        data_json: str,
    ) -> dict[str, Any]:
        collaboration_id = plan["id"]
        if step_id and step_id not in {step["id"] for step in plan["steps"]}:
            raise _invalid("交接记录引用的步骤不存在")
        payload_sha256 = _sha256(
            _canonical(
                {
                    "kind": kind,
                    "step_id": step_id,
                    "data": json.loads(data_json),
                }
            )
        )
        existing = conn.execute(
            "SELECT * FROM collaboration_events WHERE collaboration_id=? AND event_key=?",
            (collaboration_id, key),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != payload_sha256:
                raise PersonalAgentError(
                    "idempotency_conflict", "该交接记录编号已用于不同内容，请保留原记录"
                )
            return _event(existing)
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM collaboration_events WHERE collaboration_id=?",
            (collaboration_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO collaboration_events VALUES (?,?,?,?,?,?,?,?)",
            (collaboration_id, sequence, key, kind, step_id, data_json, payload_sha256, _now()),
        )
        row = conn.execute(
            "SELECT * FROM collaboration_events WHERE collaboration_id=? AND sequence=?",
            (collaboration_id, sequence),
        ).fetchone()
        return _event(row)

    def append_permission_request(
        self,
        collaboration_id: str,
        workspace_key: str,
        *,
        key: str,
        step_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically journal a request, encrypting overflow without truncating approval."""
        collaboration_id, workspace_key = self._scope(collaboration_id, workspace_key)
        key = _identifier(key, _EVENT_KEY, "交接记录编号", 256)
        step_id = _identifier(step_id, _STEP_ID, "步骤编号", 64)
        encoded = _permission_data(data)
        detached = json.loads(encoded)
        plaintext = encoded.encode("utf-8")
        digest = _sha256(encoded)
        with self._connection(write=True) as conn:
            plan = _plan(self._required(conn, collaboration_id, workspace_key))
            _permission_step(plan, step_id, detached, self.base_dir)
            existing = conn.execute(
                "SELECT * FROM collaboration_events WHERE collaboration_id=? AND event_key=?",
                (collaboration_id, key),
            ).fetchone()
            if existing is not None:
                if existing["kind"] != "permission_requested" or existing["step_id"] != step_id:
                    raise PersonalAgentError("idempotency_conflict", "权限记录编号已用于其他内容")
                previous = self._permission_record_payload(conn, plan, existing)
                if _canonical(previous) != encoded:
                    raise PersonalAgentError("idempotency_conflict", "权限记录编号已用于不同参数")
                return _event(existing)
            journal = encoded
            if len(plaintext) > _MAX_EVENT_BYTES:
                aad = _permission_aad(
                    plan,
                    key,
                    step_id,
                    detached["session_id"],
                    detached["call_id"],
                    digest,
                )
                try:
                    envelope = seal_runtime_payload(plaintext, aad=aad, key=_permission_key())
                except PersonalAgentError:
                    raise
                except Exception as exc:
                    raise PersonalAgentError("storage_error", "权限确认资料加密未完成") from exc
                conn.execute(
                    "INSERT INTO collaboration_permission_payloads "
                    "(collaboration_id,event_key,step_id,session_id,call_id,payload_sha256,"
                    "payload_bytes,envelope_json) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        collaboration_id,
                        key,
                        step_id,
                        detached["session_id"],
                        detached["call_id"],
                        digest,
                        len(plaintext),
                        _canonical(envelope.to_dict()),
                    ),
                )
                # Older schema-1 UIs require session/call IDs to show approve buttons.
                # Omit them here: only a reader that authenticates the payload can approve.
                journal = _event_data(
                    {
                        _PERMISSION_REF: {
                            "schema_version": 1,
                            "sha256": digest,
                            "bytes": len(plaintext),
                        }
                    }
                )
            return self._append_record(
                conn,
                plan,
                key,
                "permission_requested",
                step_id,
                journal,
            )

    def _permission_record_payload(
        self,
        conn: sqlite3.Connection,
        plan: dict[str, Any],
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        """Authenticate the persisted event and its scope-bound overflow before display."""
        try:
            event = _event(row)
            if event["kind"] != "permission_requested":
                raise ValueError("not a permission event")
            event_digest = _sha256(
                _canonical(
                    {
                        "kind": event["kind"],
                        "step_id": event["step_id"],
                        "data": event["data"],
                    }
                )
            )
            if not hmac.compare_digest(event_digest, row["payload_sha256"]):
                raise ValueError("journal digest mismatch")
            payload = event["data"]
            stored = conn.execute(
                "SELECT * FROM collaboration_permission_payloads "
                "WHERE collaboration_id=? AND event_key=?",
                (plan["id"], event["key"]),
            ).fetchone()
            if _PERMISSION_REF in payload:
                reference = payload[_PERMISSION_REF]
                if (
                    set(payload) != {_PERMISSION_REF}
                    or not isinstance(reference, dict)
                    or set(reference) != {"schema_version", "sha256", "bytes"}
                    or type(reference["schema_version"]) is not int
                    or reference["schema_version"] != 1
                    or type(reference["bytes"]) is not int
                    or not _MAX_EVENT_BYTES < reference["bytes"] <= _MAX_PERMISSION_PAYLOAD_BYTES
                    or not isinstance(reference["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]) is None
                    or stored is None
                    or stored["step_id"] != event["step_id"]
                    or stored["payload_bytes"] != reference["bytes"]
                    or stored["payload_sha256"] != reference["sha256"]
                ):
                    raise ValueError("permission reference mismatch")
                aad = _permission_aad(
                    plan,
                    event["key"],
                    event["step_id"],
                    stored["session_id"],
                    stored["call_id"],
                    reference["sha256"],
                )
                plaintext = open_runtime_payload(
                    PayloadEnvelope.from_dict(json.loads(stored["envelope_json"])),
                    aad=aad,
                    key=_permission_key(),
                )
                if len(plaintext) != reference["bytes"] or not hmac.compare_digest(
                    hashlib.sha256(plaintext).hexdigest(), reference["sha256"]
                ):
                    raise ValueError("permission payload digest mismatch")
                payload = json.loads(plaintext)
                if (
                    payload["session_id"] != stored["session_id"]
                    or payload["call_id"] != stored["call_id"]
                ):
                    raise ValueError("permission identity mismatch")
            elif stored is not None:
                raise ValueError("permission reference removed")
            encoded = _permission_data(payload)
            _permission_step(plan, event["step_id"], payload, self.base_dir)
            return json.loads(encoded)
        except PersonalAgentError:
            raise
        except Exception as exc:
            raise PersonalAgentError(
                "storage_error",
                "权限确认资料缺失或校验失败，已停止展示与批准，请恢复原始资料",
            ) from exc

    def permission_payload(
        self,
        collaboration_id: str,
        workspace_key: str,
        *,
        key: str,
    ) -> dict[str, Any]:
        collaboration_id, workspace_key = self._scope(collaboration_id, workspace_key)
        key = _identifier(key, _EVENT_KEY, "交接记录编号", 256)
        with self._connection() as conn:
            plan = _plan(self._required(conn, collaboration_id, workspace_key))
            row = conn.execute(
                "SELECT * FROM collaboration_events WHERE collaboration_id=? AND event_key=?",
                (collaboration_id, key),
            ).fetchone()
            if row is None:
                raise PersonalAgentError("not_found", "权限确认记录不存在，请刷新后重试")
            return self._permission_record_payload(conn, plan, row)

    def events(
        self, collaboration_id: str, workspace_key: str, after: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        collaboration_id, workspace_key = self._scope(collaboration_id, workspace_key)
        after = _integer(after, "交接记录起点", 0, 9_223_372_036_854_775_807)
        limit = _integer(limit, "交接记录数量", 1, 500)
        with self._connection() as conn:
            self._required(conn, collaboration_id, workspace_key)
            rows = conn.execute(
                "SELECT * FROM collaboration_events WHERE collaboration_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (collaboration_id, after, limit),
            ).fetchall()
            return [_event(row) for row in rows]
