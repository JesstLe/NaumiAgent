"""Immutable team turns and anchored workflow facts, never another Job lifecycle."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from naumi_agent.personal.models import PersonalAgentError

_VERSION = 1
_ID = re.compile(r"^pt_[0-9a-f]{32}$")
_AGENT = re.compile(r"^pa_[0-9a-f]{32}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_TOOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_PAYLOAD = {
    "workspace_key",
    "source_session_id",
    "team_session_id",
    "team_id",
    "team_version",
    "request_id",
    "client_payload",
    "roster",
    "policy",
}
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS team_turns (
        id TEXT PRIMARY KEY, workspace_key TEXT NOT NULL, source_session_id TEXT NOT NULL,
        team_session_id TEXT NOT NULL, team_id TEXT NOT NULL, request_id TEXT NOT NULL,
        turn_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
        event_count INTEGER NOT NULL DEFAULT 0, event_head_sha256 TEXT NOT NULL DEFAULT '',
        UNIQUE(workspace_key, source_session_id, request_id),
        CHECK(cancel_requested IN (0,1)), CHECK(event_count>=0))""",
    """CREATE TABLE IF NOT EXISTS team_turn_events (
        turn_id TEXT NOT NULL REFERENCES team_turns(id), sequence INTEGER NOT NULL,
        event_key TEXT NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL, previous_sha256 TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(turn_id, sequence), UNIQUE(turn_id,event_key), CHECK(sequence>0))""",
    "CREATE INDEX IF NOT EXISTS team_turn_scope ON team_turns "
    "(workspace_key,team_session_id,created_at,id)",
)


def _error(code: str, message: str) -> PersonalAgentError:
    return PersonalAgentError(code, message)


def _text(value: Any, label: str, maximum: int, pattern=None) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in value)
        or pattern is not None
        and pattern.fullmatch(value) is None
    ):
        raise _error("invalid_input", f"{label}格式不正确")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise _error("invalid_input", f"{label}包含无效编码") from exc
    return value


def _integer(value: Any, label: str, minimum=1, maximum=2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _error("invalid_input", f"{label}必须是有效整数")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _object(value: Any, label: str, maximum: int = 1024**2) -> dict:
    if not isinstance(value, dict):
        raise _error("invalid_input", f"{label}必须是 JSON 对象")
    nodes = 0

    def validate(item: Any, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 32 or nodes > 100000:
            raise _error("invalid_input", f"{label}嵌套过深或项目过多")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in item):
                raise _error("invalid_input", f"{label}包含无效控制字符")
            item.encode("utf-8")
            return
        if isinstance(item, int) or isinstance(item, float) and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for key, child in item.items():
                validate(key, depth + 1)
                validate(child, depth + 1)
            return
        raise _error("invalid_input", f"{label}包含不支持的 JSON 值")

    try:
        validate(value)
        encoded = _canonical(value)
        if len(encoded.encode("utf-8")) > maximum:
            raise _error("invalid_input", f"{label}超过本次保存上限")
        return json.loads(encoded)
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise _error("invalid_input", f"{label}编码不正确") from exc


def _roster(value: Any, team_id: str, team_version: int) -> dict:
    value = _object(value, "团队成员快照")
    if set(value) != {"teams", "agents"}:
        raise _error("invalid_input", "团队快照需要 teams 与 agents")
    result, seen = {"teams": [], "agents": []}, set()
    for kind, extra in (("teams", "member_ids"), ("agents", "tool_names")):
        entries = value[kind]
        if not isinstance(entries, list) or len(entries) > 128:
            raise _error("invalid_input", "团队快照每类最多允许 128 项")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"agent_id", "agent_version", extra}:
                raise _error("invalid_input", "团队快照字段不完整或包含无关字段")
            identity = _text(entry["agent_id"], "成员编号", 35, _AGENT)
            if identity in seen:
                raise _error("invalid_input", "团队快照包含重复身份")
            seen.add(identity)
            revision = _integer(entry["agent_version"], "成员版本")
            values = entry[extra]
            if not isinstance(values, list) or len(values) > 128:
                raise _error("invalid_input", "团队成员或工具最多允许 128 项")
            selected = [
                _text(item, "成员或工具编号", 128, _AGENT if kind == "teams" else _TOOL)
                for item in values
            ]
            if len(set(selected)) != len(selected):
                raise _error("invalid_input", "团队成员或工具包含重复项目")
            result[kind].append(
                {"agent_id": identity, "agent_version": revision, extra: sorted(selected)}
            )
        result[kind].sort(key=lambda entry: entry["agent_id"])
    teams = {entry["agent_id"]: entry for entry in result["teams"]}
    if team_id not in teams or teams[team_id]["agent_version"] != team_version:
        raise _error("invalid_input", "根团队与成员快照版本不一致")
    visited, visiting = set(), set()

    def walk(identity):
        # Teams freeze the actual membership graph; agents freeze only the
        # explicitly granted leaves. An omitted identity can be an ungranted
        # leaf. Catalog-aware validation owns detecting omitted nested teams.
        if identity not in seen:
            return
        if identity in visiting:
            raise _error("invalid_input", "团队快照存在循环")
        if identity in visited:
            return
        visiting.add(identity)
        if identity in teams:
            for member in teams[identity]["member_ids"]:
                walk(member)
        visiting.remove(identity)
        visited.add(identity)

    walk(team_id)
    if visited != seen:
        raise _error("invalid_input", "团队快照包含不属于根团队的身份")
    return result


def _policy(value: Any) -> dict:
    value = _object(value, "团队执行策略", 16384)
    if set(value) != {
        "permission_mode",
        "max_budget_usd",
        "max_turns",
        "max_input_tokens",
        "max_output_tokens",
    }:
        raise _error("invalid_input", "团队执行策略字段不完整或包含无关字段")
    if not isinstance(value["permission_mode"], str) or value["permission_mode"] not in {
        "bypass",
        "permissive",
        "moderate",
        "strict",
        "lockdown",
    }:
        raise _error("invalid_input", "团队执行权限模式不正确")
    _integer(value["max_turns"], "团队执行轮次", maximum=1000)
    for name in ("max_input_tokens", "max_output_tokens"):
        if value[name] is not None:
            _integer(value[name], "团队 token 预算", minimum=0, maximum=2**63 - 1)
    cost = value["max_budget_usd"]
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(cost)
        or not 0 <= cost <= 1e9
    ):
        raise _error("invalid_input", "团队费用预算不正确")
    if cost is not None:
        value["max_budget_usd"] = float(cost)
    return value


class TeamTurnStore:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.db_path = self.base_dir / "team-turns.db"
        self._initialize_lock = threading.Lock()

    @contextmanager
    def _connection(self, *, write=False):
        conn = None
        try:
            if write:
                self.base_dir.mkdir(parents=True, exist_ok=True)
                with self._initialize_lock:
                    conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
                    conn.row_factory = sqlite3.Row
                    if conn.execute("PRAGMA user_version").fetchone()[0] > _VERSION:
                        raise _error("unsupported_schema", "团队资料来自较新版本，请先更新应用")
                    conn.execute("PRAGMA busy_timeout=10000")
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("PRAGMA user_version").fetchone()[0] > _VERSION:
                        raise _error("unsupported_schema", "团队资料来自较新版本，请先更新应用")
                    for statement in _SCHEMA:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version={_VERSION}")
            else:
                conn = sqlite3.connect(
                    self.db_path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=10
                )
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                if conn.execute("PRAGMA user_version").fetchone()[0] > _VERSION:
                    raise _error("unsupported_schema", "团队资料来自较新版本，请先更新应用")
            yield conn
            conn.commit()
        except PersonalAgentError:
            if conn is not None:
                conn.rollback()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
            if conn is not None:
                conn.rollback()
            raise _error("storage_error", "团队回合资料暂不可用，请恢复资料库后重试") from exc
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _scope(identity, workspace):
        return (_text(identity, "团队回合编号", 35, _ID), _text(workspace, "工作区标识", 2048))

    @staticmethod
    def _raw(conn, identity, workspace):
        row = conn.execute(
            "SELECT * FROM team_turns WHERE id=? AND workspace_key=?", (identity, workspace)
        ).fetchone()
        if row is None:
            raise _error("not_found", "团队回合不存在，请刷新记录")
        return row

    @staticmethod
    def _read(conn, row):
        try:
            turn = json.loads(row["turn_json"])
            if set(turn) != _PAYLOAD | {"id", "created_at", "payload_sha256"}:
                raise ValueError
            payload = {key: turn[key] for key in _PAYLOAD}
            if (
                _digest(payload) != row["payload_sha256"]
                or turn["payload_sha256"] != row["payload_sha256"]
            ):
                raise ValueError
            if any(
                turn[key] != row[key]
                for key in (
                    "id",
                    "workspace_key",
                    "source_session_id",
                    "team_session_id",
                    "team_id",
                    "request_id",
                    "created_at",
                )
            ):
                raise ValueError
            rows = conn.execute(
                "SELECT * FROM team_turn_events WHERE turn_id=? ORDER BY sequence", (turn["id"],)
            ).fetchall()
            previous, result = "", []
            for number, event in enumerate(rows, 1):
                body = json.loads(event["data_json"])
                core = {
                    "turn_id": turn["id"],
                    "sequence": number,
                    "key": event["event_key"],
                    "kind": event["kind"],
                    "data": body,
                    "created_at": event["created_at"],
                    "previous_sha256": previous,
                }
                if (
                    event["sequence"] != number
                    or event["previous_sha256"] != previous
                    or event["payload_sha256"] != _digest({"kind": event["kind"], "data": body})
                    or event["receipt_sha256"] != _digest(core)
                ):
                    raise ValueError
                if number == 1 and (
                    event["kind"] != "created"
                    or event["event_key"] != "created"
                    or body != {"record_sha256": _digest(turn)}
                ):
                    raise ValueError
                previous = event["receipt_sha256"]
                result.append(
                    {key: value for key, value in core.items() if key != "turn_id"}
                    | {"receipt_sha256": previous}
                )
            if (
                not rows
                or row["event_count"] != len(rows)
                or row["event_head_sha256"] != previous
                or bool(row["cancel_requested"])
                != any(e["kind"] == "cancel_requested" for e in result)
            ):
                raise ValueError
            return {
                **turn,
                "updated_at": row["updated_at"],
                "cancel_requested": bool(row["cancel_requested"]),
            }, result
        except (TypeError, ValueError, KeyError, RecursionError, UnicodeError) as exc:
            raise _error("storage_error", "团队回合或回执不完整，请恢复原始记录") from exc

    @staticmethod
    def _append(conn, identity, key, kind, data):
        row = conn.execute("SELECT * FROM team_turns WHERE id=?", (identity,)).fetchone()
        timestamp = datetime.now(UTC).isoformat(timespec="microseconds")
        count = row["event_count"] + 1
        core = {
            "turn_id": identity,
            "sequence": count,
            "key": key,
            "kind": kind,
            "data": data,
            "created_at": timestamp,
            "previous_sha256": row["event_head_sha256"],
        }
        receipt = _digest(core)
        conn.execute(
            "INSERT INTO team_turn_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                identity,
                count,
                key,
                kind,
                _canonical(data),
                _digest({"kind": kind, "data": data}),
                core["previous_sha256"],
                receipt,
                timestamp,
            ),
        )
        conn.execute(
            "UPDATE team_turns SET event_count=?,event_head_sha256=?,updated_at=?,"
            "cancel_requested=CASE WHEN ?='cancel_requested' THEN 1 ELSE cancel_requested END "
            "WHERE id=?",
            (count, receipt, timestamp, kind, identity),
        )

    def create(
        self,
        *,
        workspace_key,
        source_session_id,
        team_session_id,
        team_id,
        team_version,
        request_id,
        client_payload: dict,
        roster: dict,
        policy: dict,
    ) -> dict:
        identity = _text(team_id, "团队编号", 35, _AGENT)
        revision = _integer(team_version, "团队版本")
        payload = {
            "workspace_key": _text(workspace_key, "工作区标识", 2048),
            "source_session_id": _text(source_session_id, "原始会话编号", 128, _KEY),
            "team_session_id": _text(team_session_id, "团队会话编号", 128, _KEY),
            "team_id": identity,
            "team_version": revision,
            "request_id": _text(request_id, "请求编号", 128, _KEY),
            "client_payload": _object(client_payload, "团队请求"),
            "roster": _roster(roster, identity, revision),
            "policy": _policy(policy),
        }
        payload_hash = _digest(payload)
        with self._connection(write=True) as conn:
            existing = conn.execute(
                "SELECT * FROM team_turns WHERE workspace_key=? AND "
                "source_session_id=? AND request_id=?",
                (workspace_key, source_session_id, request_id),
            ).fetchone()
            if existing is not None:
                record = self._read(conn, existing)[0]
                if record["payload_sha256"] != payload_hash:
                    raise _error("idempotency_conflict", "该请求编号已有不同团队委托，请保留原请求")
                return record
            turn = {
                **payload,
                "id": "pt_" + uuid.uuid4().hex,
                "created_at": datetime.now(UTC).isoformat(timespec="microseconds"),
                "payload_sha256": payload_hash,
            }
            conn.execute(
                "INSERT INTO team_turns (id,workspace_key,source_session_id,team_session_id,"
                "team_id,request_id,turn_json,payload_sha256,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    turn["id"],
                    workspace_key,
                    source_session_id,
                    team_session_id,
                    team_id,
                    request_id,
                    _canonical(turn),
                    payload_hash,
                    turn["created_at"],
                    turn["created_at"],
                ),
            )
            self._append(conn, turn["id"], "created", "created", {"record_sha256": _digest(turn)})
            return self._read(conn, self._raw(conn, turn["id"], workspace_key))[0]

    def get(self, turn_id, workspace_key) -> dict:
        turn_id, workspace_key = self._scope(turn_id, workspace_key)
        if not self.db_path.exists():
            raise _error("not_found", "团队回合不存在，请刷新记录")
        with self._connection() as conn:
            return self._read(conn, self._raw(conn, turn_id, workspace_key))[0]

    def find_request(self, workspace_key, source_session_id, request_id):
        _text(workspace_key, "工作区标识", 2048)
        _text(source_session_id, "原始会话编号", 128, _KEY)
        _text(request_id, "请求编号", 128, _KEY)
        if not self.db_path.exists():
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM team_turns WHERE workspace_key=? AND "
                "source_session_id=? AND request_id=?",
                (workspace_key, source_session_id, request_id),
            ).fetchone()
            return self._read(conn, row)[0] if row else None

    def list(self, workspace_key, team_session_id=None, limit=100, offset=0, oldest_first=False):
        _text(workspace_key, "工作区标识", 2048)
        if team_session_id is not None:
            _text(team_session_id, "团队会话编号", 128, _KEY)
        _integer(limit, "列表数量", maximum=500)
        _integer(offset, "列表偏移", minimum=0, maximum=2**63 - 1)
        if not isinstance(oldest_first, bool):
            raise _error("invalid_input", "团队排序参数不正确")
        if not self.db_path.exists():
            return []
        with self._connection() as conn:
            direction = "ASC" if oldest_first else "DESC"
            rows = conn.execute(
                "SELECT * FROM team_turns WHERE workspace_key=? "
                "AND (? IS NULL OR team_session_id=?) "
                f"ORDER BY created_at {direction},id {direction} LIMIT ? OFFSET ?",
                (workspace_key, team_session_id, team_session_id, limit, offset),
            ).fetchall()
            return [self._read(conn, row)[0] for row in rows]

    def append_event(self, turn_id, workspace_key, *, key, kind, data):
        turn_id, workspace_key = self._scope(turn_id, workspace_key)
        _text(key, "团队事件编号", 256, _KEY)
        _text(kind, "团队事件类型", 64, _KEY)
        data = _object(data, "团队事件", 1024**2)
        if kind == "created" or (
            kind == "cancel_requested" and (key != "cancel_requested" or data)
        ):
            raise _error("invalid_input", "团队事件使用了保留字段")
        with self._connection(write=True) as conn:
            _, events = self._read(conn, self._raw(conn, turn_id, workspace_key))
            previous = next((event for event in events if event["key"] == key), None)
            if previous is not None:
                if _digest({"kind": previous["kind"], "data": previous["data"]}) != _digest(
                    {"kind": kind, "data": data}
                ):
                    raise _error("idempotency_conflict", "该团队事件已有不同内容，请保留原记录")
                return previous
            self._append(conn, turn_id, key, kind, data)
            return self._read(conn, self._raw(conn, turn_id, workspace_key))[1][-1]

    def events(self, turn_id, workspace_key, *, after=0, limit=500):
        turn_id, workspace_key = self._scope(turn_id, workspace_key)
        _integer(after, "团队事件偏移", minimum=0, maximum=2**63 - 1)
        _integer(limit, "团队事件数量", maximum=500)
        if not self.db_path.exists():
            raise _error("not_found", "团队回合不存在，请刷新记录")
        with self._connection() as conn:
            return self._read(conn, self._raw(conn, turn_id, workspace_key))[1][
                after : after + limit
            ]

    def request_cancel(self, turn_id, workspace_key):
        self.append_event(
            turn_id, workspace_key, key="cancel_requested", kind="cancel_requested", data={}
        )
        return self.get(turn_id, workspace_key)
