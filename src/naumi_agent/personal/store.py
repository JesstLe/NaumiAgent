"""Transactional SQLite catalog; no specialists are implicitly provisioned."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from naumi_agent.personal.models import PersonalAgent, PersonalAgentBinding, PersonalAgentError

_SCHEMA_VERSION = 1
_AGENT_ID = re.compile(r"^pa_[0-9a-f]{32}$")
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_UPDATE_FIELDS = frozenset(
    {
        "name",
        "description",
        "instructions",
        "model_tier",
        "tool_names",
        "member_ids",
        "status",
    }
)
_TABLES = (
    """CREATE TABLE IF NOT EXISTS personal_agents (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
        instructions TEXT NOT NULL, model_tier TEXT NOT NULL,
        tool_names TEXT NOT NULL, kind TEXT NOT NULL, member_ids TEXT NOT NULL,
        status TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK(model_tier IN ('fast', 'capable', 'reasoning')),
        CHECK(kind IN ('specialist', 'team')),
        CHECK(status IN ('active', 'paused', 'archived'))
    )""",
    """CREATE TABLE IF NOT EXISTS personal_agent_requests (
        request_id TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL,
        agent_id TEXT NOT NULL REFERENCES personal_agents(id)
    )""",
    """CREATE TABLE IF NOT EXISTS personal_agent_history (
        agent_id TEXT NOT NULL REFERENCES personal_agents(id),
        version INTEGER NOT NULL, snapshot TEXT NOT NULL,
        PRIMARY KEY(agent_id, version)
    )""",
    """CREATE TABLE IF NOT EXISTS personal_agent_bindings (
        agent_id TEXT NOT NULL REFERENCES personal_agents(id), workspace_key TEXT NOT NULL,
        session_id TEXT NOT NULL, session_db_key TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(agent_id, workspace_key),
        UNIQUE(workspace_key, session_db_key, session_id)
    )""",
    """CREATE TABLE IF NOT EXISTS personal_agent_sessions (
        workspace_key TEXT NOT NULL, session_db_key TEXT NOT NULL,
        session_id TEXT NOT NULL, agent_id TEXT NOT NULL REFERENCES personal_agents(id),
        created_at TEXT NOT NULL,
        PRIMARY KEY(workspace_key, session_db_key, session_id)
    )""",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _text(value: Any, field: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise PersonalAgentError("invalid_input", f"{field}必须是文字")
    if _CONTROL.search(value):
        raise PersonalAgentError("invalid_input", f"{field}包含无效控制字符")
    result = value.strip()
    if required and not result:
        raise PersonalAgentError("invalid_input", f"{field}不能为空")
    if len(result) > limit:
        raise PersonalAgentError("invalid_input", f"{field}最多允许 {limit} 个字符")
    return result


def _agent_id(value: Any) -> str:
    result = _text(value, "助理编号", 64)
    if not _AGENT_ID.fullmatch(result):
        raise PersonalAgentError("invalid_input", "助理编号格式不正确")
    return result


def _choice(value: Any, choices: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise PersonalAgentError("invalid_input", f"{field}选项不正确")
    return str(value)


def _names(value: Any, *, members: bool = False) -> tuple[str, ...]:
    field = "成员列表" if members else "工具列表"
    if not isinstance(value, (tuple, list)) or len(value) > 128:
        raise PersonalAgentError("invalid_input", f"{field}必须是最多 128 项的列表")
    result: list[str] = []
    for item in value:
        name = _agent_id(item) if members else _text(item, "工具名称", 128)
        if not members and not _TOOL_NAME.fullmatch(name):
            raise PersonalAgentError("invalid_input", "工具名称格式不正确")
        if name in result:
            raise PersonalAgentError("invalid_input", f"{field}包含重复项目")
        result.append(name)
    return tuple(sorted(result))


def _profile(row: sqlite3.Row | dict[str, Any]) -> PersonalAgent:
    data = dict(row)
    for field in ("tool_names", "member_ids"):
        raw = data[field]
        data[field] = tuple(json.loads(raw) if isinstance(raw, str) else raw)
    return PersonalAgent(**data)


def _snapshot(agent: PersonalAgent) -> str:
    return json.dumps(agent.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PersonalAgentStore:
    """One owner's catalog, independent from workspace-scoped execution records.

    Construction performs no I/O. Public methods are synchronous and return immutable
    records. Callers running in an async server may use ``asyncio.to_thread``.
    ``create`` idempotency binds a request ID to normalized creation fields; replay
    returns the current profile without undoing later edits or archival.
    ``update`` requires the last observed version and records each actual change.
    ``bind_session`` without an expected ID is get-or-create: concurrent callers get
    the winning binding. Replacing an old binding requires an exact session-ID CAS.
    A session_db_key identifies the session database; it is not a filesystem path to open.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir).expanduser().resolve()
        self._db_path = self._base_dir / "personal_agents.db"
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
                        version = conn.execute("PRAGMA user_version").fetchone()[0]
                        if version > _SCHEMA_VERSION:
                            raise PersonalAgentError(
                                "unsupported_schema", "助理资料库来自较新版本，请先更新应用"
                            )
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("BEGIN IMMEDIATE")
                        # Recheck under the migration lock before writing a schema version.
                        if conn.execute("PRAGMA user_version").fetchone()[0] > _SCHEMA_VERSION:
                            raise PersonalAgentError(
                                "unsupported_schema", "助理资料库来自较新版本，请先更新应用"
                            )
                        for statement in _TABLES:
                            conn.execute(statement)
                        conn.execute(
                            "INSERT OR IGNORE INTO personal_agent_sessions "
                            "SELECT workspace_key,session_db_key,session_id,agent_id,created_at "
                            "FROM personal_agent_bindings"
                        )
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
                    "storage_error", "助理资料库暂时不可用，请检查目录或稍后重试"
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
            raise PersonalAgentError("storage_error", "助理资料保存未完成，请稍后重试") from exc
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _get(conn: sqlite3.Connection, agent_id: str) -> PersonalAgent | None:
        row = conn.execute("SELECT * FROM personal_agents WHERE id=?", (agent_id,)).fetchone()
        return _profile(row) if row is not None else None

    @staticmethod
    def _required(conn: sqlite3.Connection, agent_id: str) -> PersonalAgent:
        agent = PersonalAgentStore._get(conn, agent_id)
        if agent is None:
            raise PersonalAgentError("not_found", "该助理不存在，请刷新列表")
        return agent

    def list(self, *, include_archived: bool = False) -> tuple[PersonalAgent, ...]:
        if not isinstance(include_archived, bool):
            raise PersonalAgentError("invalid_input", "归档筛选必须是开关值")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM personal_agents WHERE (? OR status != 'archived') "
                "ORDER BY created_at, id",
                (int(include_archived),),
            ).fetchall()
            return tuple(_profile(row) for row in rows)

    def get(self, agent_id: str) -> PersonalAgent | None:
        agent_id = _agent_id(agent_id)
        with self._connection() as conn:
            return self._get(conn, agent_id)

    @staticmethod
    def _validate_members(conn: sqlite3.Connection, agent: PersonalAgent) -> None:
        if agent.kind == "specialist" and agent.member_ids:
            raise PersonalAgentError("invalid_members", "专员不包含团队成员，请创建团队后添加成员")
        pending = list(agent.member_ids)
        visited: set[str] = set()
        while pending:
            current_id = pending.pop()
            if current_id == agent.id:
                raise PersonalAgentError("member_cycle", "团队成员形成循环，请调整成员关系")
            if current_id in visited:
                continue
            visited.add(current_id)
            member = PersonalAgentStore._get(conn, current_id)
            if member is None:
                raise PersonalAgentError("member_not_found", "团队包含不存在的成员，请刷新后选择")
            if agent.status == "active" and member.status != "active":
                raise PersonalAgentError("member_inactive", "团队包含已停用或归档的成员")
            pending.extend(member.member_ids)

    @staticmethod
    def _write(conn: sqlite3.Connection, agent: PersonalAgent, *, create: bool = False) -> None:
        values = agent.to_dict()
        for field in ("tool_names", "member_ids"):
            values[field] = json.dumps(values[field], separators=(",", ":"))
        if create:
            conn.execute(
                "INSERT INTO personal_agents VALUES (:id,:name,:description,:instructions,"
                ":model_tier,:tool_names,:kind,:member_ids,:status,:version,:created_at,:updated_at)",
                values,
            )
        else:
            conn.execute(
                "UPDATE personal_agents SET name=:name, description=:description, "
                "instructions=:instructions, model_tier=:model_tier, tool_names=:tool_names, "
                "member_ids=:member_ids, status=:status, version=:version, updated_at=:updated_at "
                "WHERE id=:id",
                values,
            )
        conn.execute(
            "INSERT INTO personal_agent_history VALUES (?,?,?)",
            (agent.id, agent.version, _snapshot(agent)),
        )

    def create(
        self,
        *,
        name: str,
        instructions: str,
        request_id: str,
        description: str = "",
        model_tier: str = "capable",
        tool_names: Sequence[str] = (),
        kind: str = "specialist",
        member_ids: Sequence[str] = (),
    ) -> PersonalAgent:
        request_id = _text(request_id, "请求编号", 128)
        if not _REQUEST_ID.fullmatch(request_id):
            raise PersonalAgentError("invalid_input", "请求编号格式不正确")
        payload = {
            "name": _text(name, "助理名称", 80),
            "description": _text(description, "助理简介", 500, required=False),
            "instructions": _text(instructions, "助理职责", 12_000),
            "model_tier": _choice(model_tier, {"fast", "capable", "reasoning"}, "模型等级"),
            "tool_names": _names(tool_names),
            "kind": _choice(kind, {"specialist", "team"}, "助理类型"),
            "member_ids": _names(member_ids, members=True),
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        with self._connection(write=True) as conn:
            previous = conn.execute(
                "SELECT payload_sha256, agent_id FROM personal_agent_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if previous is not None:
                if previous["payload_sha256"] != fingerprint:
                    raise PersonalAgentError(
                        "idempotency_conflict", "该请求编号已用于不同内容，请刷新后重新提交"
                    )
                return self._required(conn, previous["agent_id"])
            now = _now()
            agent = PersonalAgent(
                id="pa_" + uuid.uuid4().hex,
                **payload,
                status="active",
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._validate_members(conn, agent)
            self._write(conn, agent, create=True)
            conn.execute(
                "INSERT INTO personal_agent_requests VALUES (?,?,?)",
                (request_id, fingerprint, agent.id),
            )
            return agent

    def update(self, agent_id: str, expected_version: int, **fields: Any) -> PersonalAgent:
        agent_id = _agent_id(agent_id)
        if type(expected_version) is not int or expected_version < 1:
            raise PersonalAgentError("invalid_input", "请提供有效的助理资料版本")
        if not fields or set(fields) - _UPDATE_FIELDS:
            raise PersonalAgentError("invalid_input", "没有可更新的字段或包含不支持的字段")
        normalized: dict[str, Any] = {}
        for key, value in fields.items():
            if key in {"name", "description", "instructions"}:
                labels = {
                    "name": ("助理名称", 80),
                    "description": ("助理简介", 500),
                    "instructions": ("助理职责", 12_000),
                }
                label, limit = labels[key]
                normalized[key] = _text(value, label, limit, required=key != "description")
            elif key in {"tool_names", "member_ids"}:
                normalized[key] = _names(value, members=key == "member_ids")
            elif key == "status":
                normalized[key] = _choice(value, {"active", "paused", "archived"}, "助理状态")
            else:
                normalized[key] = _choice(value, {"fast", "capable", "reasoning"}, "模型等级")
        with self._connection(write=True) as conn:
            current = self._required(conn, agent_id)
            if current.version != expected_version:
                raise PersonalAgentError("version_conflict", "助理资料已更新，请刷新后再修改")
            candidate = replace(current, **normalized)
            self._validate_members(conn, candidate)
            if candidate == current:
                return current
            candidate = replace(candidate, version=current.version + 1, updated_at=_now())
            self._write(conn, candidate)
            return candidate

    def history(self, agent_id: str) -> tuple[PersonalAgent, ...]:
        agent_id = _agent_id(agent_id)
        with self._connection() as conn:
            self._required(conn, agent_id)
            rows = conn.execute(
                "SELECT snapshot FROM personal_agent_history WHERE agent_id=? ORDER BY version",
                (agent_id,),
            ).fetchall()
            return tuple(_profile(json.loads(row["snapshot"])) for row in rows)

    def get_binding(self, agent_id: str, workspace_key: str) -> PersonalAgentBinding | None:
        agent_id = _agent_id(agent_id)
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM personal_agent_bindings WHERE agent_id=? AND workspace_key=?",
                (agent_id, workspace_key),
            ).fetchone()
            return PersonalAgentBinding(**dict(row)) if row is not None else None

    def bind_session(
        self,
        agent_id: str,
        workspace_key: str,
        session_id: str,
        *,
        session_db_key: str = "",
        expected_session_id: str | None = None,
    ) -> PersonalAgentBinding:
        agent_id = _agent_id(agent_id)
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        session_id = _text(session_id, "会话编号", 128)
        session_db_key = _text(session_db_key, "会话库标识", 2048, required=False)
        if expected_session_id is not None:
            expected_session_id = _text(expected_session_id, "原会话编号", 128)
        with self._connection(write=True) as conn:
            agent = self._required(conn, agent_id)
            if agent.status != "active":
                raise PersonalAgentError("agent_inactive", "助理已停用或归档，请启用后继续")
            row = conn.execute(
                "SELECT * FROM personal_agent_bindings WHERE agent_id=? AND workspace_key=?",
                (agent_id, workspace_key),
            ).fetchone()
            current = PersonalAgentBinding(**dict(row)) if row is not None else None
            if current is not None and expected_session_id is None:
                return current
            if expected_session_id is not None and (
                current is None or current.session_id != expected_session_id
            ):
                raise PersonalAgentError("binding_conflict", "助理会话已切换，请刷新后继续")
            occupied = conn.execute(
                "SELECT agent_id FROM personal_agent_sessions WHERE workspace_key=? "
                "AND session_db_key=? AND session_id=? AND agent_id != ?",
                (workspace_key, session_db_key, session_id, agent_id),
            ).fetchone()
            if occupied is not None:
                raise PersonalAgentError("session_conflict", "该会话已属于另一位助理")
            if current and (current.session_id, current.session_db_key) == (
                session_id,
                session_db_key,
            ):
                return current
            now = _now()
            binding = PersonalAgentBinding(
                agent_id=agent_id,
                workspace_key=workspace_key,
                session_id=session_id,
                session_db_key=session_db_key,
                created_at=current.created_at if current else now,
                updated_at=now,
            )
            # Historical ownership outlives the current-session pointer. Otherwise
            # opening a replaced session could silently bypass its agent policy.
            conn.execute(
                "INSERT OR IGNORE INTO personal_agent_sessions VALUES (?,?,?,?,?)",
                (workspace_key, session_db_key, session_id, agent_id, now),
            )
            conn.execute(
                "INSERT INTO personal_agent_bindings VALUES "
                "(:agent_id,:workspace_key,:session_id,:session_db_key,:created_at,:updated_at) "
                "ON CONFLICT(agent_id,workspace_key) DO UPDATE SET session_id=excluded.session_id, "
                "session_db_key=excluded.session_db_key, updated_at=excluded.updated_at",
                binding.to_dict(),
            )
            return binding

    def agent_for_session(
        self,
        workspace_key: str,
        session_id: str,
        *,
        session_db_key: str | None = None,
    ) -> PersonalAgent | None:
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        session_id = _text(session_id, "会话编号", 128)
        if session_db_key is not None:
            session_db_key = _text(session_db_key, "会话库标识", 2048, required=False)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT a.* FROM personal_agents a "
                "JOIN personal_agent_sessions b ON a.id=b.agent_id "
                "WHERE b.workspace_key=? AND b.session_id=? "
                "AND (? IS NULL OR b.session_db_key=?) ORDER BY a.id",
                (workspace_key, session_id, session_db_key, session_db_key),
            ).fetchall()
            if len(rows) > 1:
                raise PersonalAgentError(
                    "ambiguous_session", "会话编号属于不同资料库，请指定会话库"
                )
            return _profile(rows[0]) if rows else None

    def register_execution_session(
        self, agent_id: str, workspace_key: str, session_id: str, *, session_db_key: str
    ) -> None:
        """Record immutable execution ownership without replacing the daily chat pointer."""
        agent_id = _agent_id(agent_id)
        workspace_key = _text(workspace_key, "工作区标识", 2048)
        session_id = _text(session_id, "会话编号", 128)
        session_db_key = _text(session_db_key, "会话库标识", 2048)
        with self._connection(write=True) as conn:
            profile = self._required(conn, agent_id)
            if profile.status != "active" or profile.kind != "specialist":
                raise PersonalAgentError("agent_inactive", "请先启用专员，再发起协作")
            existing = conn.execute(
                "SELECT agent_id FROM personal_agent_sessions WHERE workspace_key=? "
                "AND session_db_key=? AND session_id=?",
                (workspace_key, session_db_key, session_id),
            ).fetchone()
            if existing is not None and existing["agent_id"] != agent_id:
                raise PersonalAgentError("session_conflict", "执行会话已属于另一位助理")
            conn.execute(
                "INSERT OR IGNORE INTO personal_agent_sessions VALUES (?,?,?,?,?)",
                (workspace_key, session_db_key, session_id, agent_id, _now()),
            )
