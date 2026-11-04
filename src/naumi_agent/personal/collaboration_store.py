"""Immutable collaboration plans and an ordered, idempotent handoff journal.

Execution state and terminal payloads belong to AgentJobStore. This store keeps
only the user's plan, stable execution identities, cancellation intent, and
observations. No observation here can make an Agent job completed or replayable.
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
from naumi_agent.personal.peer_models import agent_id as normalized_agent_id
from naumi_agent.personal.peer_models import dispatch, grants
from naumi_agent.personal.peer_models import tool_names as normalized_tool_names
from naumi_agent.personal.peer_models import version as normalized_version

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
            or set(raw) - _STEP_FIELDS - {"message", "tool_names"}
        ):
            raise _invalid("协作步骤缺少必填字段或包含不支持的字段")
        step_id = _identifier(raw["id"], _STEP_ID, "步骤编号", 64)
        if step_id in ids:
            raise _invalid("协作步骤编号重复")
        ids.add(step_id)
        agent_id = _identifier(raw["agent_id"], _AGENT_ID, "Agent 编号", 35)
        version = _integer(raw["agent_version"], "Agent 版本", 1, 2_147_483_647)
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
        if "tool_names" in raw:
            steps[-1]["tool_names"] = normalized_tool_names(raw["tool_names"])
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


def _team_execution(value: Any) -> dict | None:
    if value is None:
        return None
    fields = {
        "turn_id",
        "phase",
        "team_id",
        "team_version",
        "max_budget_usd",
        "max_input_tokens",
        "max_output_tokens",
        "max_turns",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise _invalid("团队阶段凭据字段不正确")
    if (
        not isinstance(value["turn_id"], str)
        or not re.fullmatch(r"pt_[0-9a-f]{32}", value["turn_id"])
        or value["phase"] not in {"planning", "members", "summary"}
    ):
        raise _invalid("团队阶段身份不正确")
    result = dict(value)
    result["team_id"] = normalized_agent_id(value["team_id"])
    result["team_version"] = normalized_version(value["team_version"])
    result["max_turns"] = _integer(value["max_turns"], "团队阶段执行轮次", 1, 1000)
    for key in ("max_input_tokens", "max_output_tokens"):
        if result[key] is not None:
            result[key] = _integer(result[key], "团队阶段 token 预算", 0, 2**63 - 1)
    cost = result["max_budget_usd"]
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(cost)
        or not 0 <= cost <= 1e9
    ):
        raise _invalid("团队阶段费用预算不正确")
    return result


def _event_data(value: Any) -> str:
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
        if len(encoded.encode("utf-8")) > _MAX_EVENT_BYTES:
            raise _invalid("协作事件内容最多允许 64 KB")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise _invalid("协作事件内容不是有效的 JSON 资料") from exc
    return encoded


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
        team_execution: dict | None = None,
    ) -> dict[str, Any]:
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        source_session_id = _text(source_session_id, "来源会话编号", 128)
        source_agent_id = _text(source_agent_id, "来源 Agent 编号", 35, required=False)
        if source_agent_id:
            source_agent_id = _identifier(source_agent_id, _AGENT_ID, "来源 Agent 编号", 35)
        request_id = _identifier(request_id, _REQUEST_ID, "请求编号", 128)
        normalized_steps = _steps(steps)
        owner = _team_execution(team_execution)
        if owner is None and any("tool_names" in step for step in normalized_steps):
            raise _invalid("单步工具范围需要可信团队执行凭据")
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
        if owner is not None:
            payload["team_execution"] = owner
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
        payload_sha256 = _sha256(
            _canonical(
                {
                    "kind": kind,
                    "step_id": step_id,
                    "data": json.loads(data_json),
                }
            )
        )
        with self._connection(write=True) as conn:
            plan = _plan(self._required(conn, collaboration_id, workspace_key))
            if step_id and step_id not in {step["id"] for step in plan["steps"]}:
                raise _invalid("交接记录引用的步骤不存在")
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
                "SELECT COALESCE(MAX(sequence),0)+1 FROM collaboration_events "
                "WHERE collaboration_id=?",
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
