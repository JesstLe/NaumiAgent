"""Immutable private artifact versions and revocable, exact-version access grants.

This store owns bytes and metadata, not executable identity or another Job state
machine. Its callers supply a previously verified workspace/session principal.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from naumi_agent.personal.artifact_models import (
    ARTIFACT_ID,
    GRANT_ID,
    MAX_SESSION_REFS,
    PRINCIPAL,
    REF_FIELDS,
    REQUEST_KEY,
    canonical,
    digest,
    integer,
    invalid,
    json_object,
    normalize_recipient_scope,
    normalize_refs,
    strict_json_loads,
    text,
    validate_content,
)
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.output_publish import MAX_ASSET_BYTES

_SCHEMA_VERSION = 1


def _connection_evidence(evidence):
    from naumi_agent.personal.connection_models import action_source_ref, source_ref

    if not isinstance(evidence, dict):
        raise invalid("产物来源证据格式不正确")
    refs = evidence.get("connection_sources", [])
    if not isinstance(refs, list) or len(refs) > 64:
        raise invalid("日历来源引用需要有效列表，且不超过 64 项")
    for ref in refs:
        source_ref(ref)
    actions = evidence.get("connection_action_refs", [])
    if not isinstance(actions, list) or len(actions) > 64:
        raise invalid("日历行动来源需要有效列表，且不超过 64 项")
    for ref in actions:
        action_source_ref(ref)
    return bool(refs or actions)


_VERSION_FIELDS = {
    "artifact_id",
    "workspace_key",
    "version",
    "owner_id",
    "kind",
    "title",
    "sha256",
    "bytes",
    "mime_type",
    "suffix",
    "created_at",
    "source_refs",
    "evidence",
    "request_id",
    "previous_sha256",
}
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id TEXT PRIMARY KEY, workspace_key TEXT NOT NULL, owner_id TEXT NOT NULL,
        kind TEXT NOT NULL, latest_version INTEGER NOT NULL, head_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL, CHECK(latest_version>0), CHECK(kind IN ('material','output'))
    )""",
    """CREATE TABLE IF NOT EXISTS artifact_versions (
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id), version INTEGER NOT NULL,
        record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
        PRIMARY KEY(artifact_id,version), CHECK(version>0)
    )""",
    """CREATE TABLE IF NOT EXISTS artifact_grants (
        grant_id TEXT PRIMARY KEY, workspace_key TEXT NOT NULL,
        artifact_id TEXT NOT NULL, version INTEGER NOT NULL,
        owner_id TEXT NOT NULL, recipient_id TEXT NOT NULL, recipient_scope_json TEXT NOT NULL,
        revision INTEGER NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT '',
        record_sha256 TEXT NOT NULL,
        FOREIGN KEY(artifact_id,version) REFERENCES artifact_versions(artifact_id,version),
        CHECK(revision IN (1,2)), CHECK(owner_id<>recipient_id)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS artifact_live_grant ON artifact_grants
        (workspace_key,artifact_id,version,recipient_id) WHERE revoked_at=''
    """,
    """CREATE TABLE IF NOT EXISTS artifact_requests (
        workspace_key TEXT NOT NULL, actor_id TEXT NOT NULL, request_id TEXT NOT NULL,
        operation TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        result_json TEXT NOT NULL, result_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(workspace_key,actor_id,request_id)
    )""",
    """CREATE TABLE IF NOT EXISTS artifact_session_refs (
        workspace_key TEXT NOT NULL, session_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        refs_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
        PRIMARY KEY(workspace_key,session_id)
    )""",
    """CREATE TRIGGER IF NOT EXISTS artifact_version_no_update
        BEFORE UPDATE ON artifact_versions BEGIN SELECT RAISE(ABORT,'immutable version'); END
    """,
    """CREATE TRIGGER IF NOT EXISTS artifact_version_no_delete
        BEFORE DELETE ON artifact_versions BEGIN SELECT RAISE(ABORT,'immutable version'); END
    """,
    "CREATE INDEX IF NOT EXISTS artifact_scope ON artifacts(workspace_key,owner_id)",
    "CREATE INDEX IF NOT EXISTS artifact_recipient ON artifact_grants(workspace_key,recipient_id)",
)


def _error(code: str, message: str) -> PersonalAgentError:
    return PersonalAgentError(code, message)


def _missing() -> PersonalAgentError:
    return _error("not_found", "资料不存在或当前 Agent未获授权")


def _broken() -> PersonalAgentError:
    return _error("storage_error", "资料记录或文件校验失败，请恢复原始资料后重试")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _ordinary_path(value: Path) -> Path:
    # GetFinalPathNameByHandle may transiently preserve the extended namespace
    # while SQLite creates the file. Normalize only equivalent Win32 spelling;
    # the resolved destination still must equal the lexical, non-aliased path.
    raw = str(value)
    if os.name == "nt" and raw.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + raw[8:])
    if os.name == "nt" and raw.startswith("\\\\?\\"):
        return Path(raw[4:])
    return value


class ArtifactStore:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = _ordinary_path(Path(base_dir).expanduser().absolute())
        self.db_path = self.base_dir / "artifacts.db"
        self.blob_dir = self.base_dir / "blobs"
        self._lock = threading.RLock()
        self._check_base()

    def _check_base(self) -> None:
        if os.path.normcase(str(self.base_dir)) != os.path.normcase(
            str(_ordinary_path(self.base_dir.resolve()))
        ):
            raise _broken()

    def _path(self, path: Path) -> Path:
        self._check_base()
        resolved = _ordinary_path(path.resolve())
        if (
            not resolved.is_relative_to(self.base_dir)
            or os.path.normcase(str(path.absolute())) != os.path.normcase(str(resolved))
            or path.is_symlink()
        ):
            raise _broken()
        return path

    def _database_exists(self) -> bool:
        self._path(self.db_path)
        if self.db_path.exists() and not self.db_path.is_file():
            raise _broken()
        return self.db_path.is_file()

    @contextmanager
    def _connection(self, *, write=False):
        connection = None
        try:
            with self._lock:
                self._path(self.db_path)
                if write:
                    self.base_dir.mkdir(parents=True, exist_ok=True)
                    connection = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
                else:
                    connection = sqlite3.connect(
                        self.db_path.as_uri() + "?mode=ro",
                        uri=True,
                        isolation_level=None,
                        timeout=10,
                    )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if connection.execute("PRAGMA user_version").fetchone()[0] > _SCHEMA_VERSION:
                    raise _error("unsupported_schema", "资料库来自较新版本，请先更新应用")
                if write:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                    # A rejected first publication must not roll back the schema
                    # and leave a misleading empty database for read-only callers.
                    connection.commit()
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
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
    def _scope(workspace_key, actor_id):
        return (
            text(workspace_key, "工作区标识", 2048),
            text(actor_id, "Agent 身份", 35, PRINCIPAL),
        )

    @staticmethod
    def _identity(artifact_id, version=None):
        identity = text(artifact_id, "产物编号", 36, ARTIFACT_ID)
        return identity, integer(
            version, "产物版本", maximum=10000
        ) if version is not None else None

    @staticmethod
    def _stored(value):
        try:
            return strict_json_loads(value)
        except PersonalAgentError as exc:
            raise _broken() from exc

    def _artifact(self, conn, workspace, identity):
        row = conn.execute(
            "SELECT * FROM artifacts WHERE workspace_key=? AND artifact_id=?",
            (workspace, identity),
        ).fetchone()
        if row is None:
            raise _missing()
        count = conn.execute(
            "SELECT COUNT(*),MIN(version),MAX(version) FROM artifact_versions WHERE artifact_id=?",
            (identity,),
        ).fetchone()
        head = conn.execute(
            "SELECT record_sha256 FROM artifact_versions WHERE artifact_id=? AND version=?",
            (identity, row["latest_version"]),
        ).fetchone()
        if (
            count[0] != row["latest_version"]
            or count[1] != 1
            or count[2] != row["latest_version"]
            or head is None
            or head[0] != row["head_sha256"]
        ):
            raise _broken()
        return row

    def _version(self, conn, artifact, version):
        row = conn.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id=? AND version=?",
            (artifact["artifact_id"], version),
        ).fetchone()
        if row is None:
            raise _missing()
        value = self._stored(row["record_json"])
        if (
            not isinstance(value, dict)
            or set(value) not in (_VERSION_FIELDS, _VERSION_FIELDS | {"authority_version"})
            or digest(value) != row["record_sha256"]
        ):
            raise _broken()
        if not isinstance(value.get("evidence"), dict):
            raise _broken()
        try:
            connection_bound = _connection_evidence(value["evidence"])
        except PersonalAgentError:
            raise _broken() from None
        if connection_bound != (value.get("authority_version") == 2):
            raise _broken()
        if "authority_version" in value and (
            type(value["authority_version"]) is not int or value["authority_version"] != 2
        ):
            raise _broken()
        if (
            any(
                value[key] != artifact[key]
                for key in ("artifact_id", "workspace_key", "owner_id", "kind")
            )
            or value["version"] != version
        ):
            raise _broken()
        prior = ""
        if version > 1:
            prior_row = conn.execute(
                "SELECT record_sha256 FROM artifact_versions WHERE artifact_id=? AND version=?",
                (artifact["artifact_id"], version - 1),
            ).fetchone()
            if prior_row is None:
                raise _broken()
            prior = prior_row[0]
        if value["previous_sha256"] != prior:
            raise _broken()
        try:
            refs = value["source_refs"]
            if not isinstance(refs, list) or any(
                not isinstance(ref, dict) or set(ref) != REF_FIELDS | {"reader_id"} for ref in refs
            ):
                raise _broken()
            for ref in refs:
                text(ref["reader_id"], "原读取 Agent", 35, PRINCIPAL)
            normalize_refs([{k: ref[k] for k in REF_FIELDS} for ref in refs])
        except PersonalAgentError as exc:
            raise _broken() from exc
        return value

    def _grant(self, row):
        scope = self._stored(row["recipient_scope_json"])
        try:
            if normalize_recipient_scope(scope, row["recipient_id"]) != scope:
                raise _broken()
        except PersonalAgentError as exc:
            raise _broken() from exc
        value = {
            k: row[k]
            for k in (
                "grant_id",
                "workspace_key",
                "artifact_id",
                "version",
                "owner_id",
                "recipient_id",
                "revision",
                "created_at",
                "revoked_at",
            )
        } | {"recipient_scope": scope}
        if digest(value) != row["record_sha256"] or bool(value["revoked_at"]) != (
            value["revision"] == 2
        ):
            raise _broken()
        return value

    @staticmethod
    def _recipient(grant, actor):
        return grant["recipient_id"] == actor or (
            grant["recipient_scope"].get("kind") == "team"
            and actor in grant["recipient_scope"]["member_ids"]
        )

    def _direct_grant(self, conn, record, actor, exact=None, collected=None):
        if actor == record["owner_id"]:
            if exact is not None and (exact["grant_id"] != "" or exact["grant_revision"] != 0):
                raise _missing()
            return {"grant_id": "", "grant_revision": 0}
        if exact is not None and not exact["grant_id"]:
            raise _missing()
        rows = conn.execute(
            "SELECT * FROM artifact_grants WHERE workspace_key=? AND artifact_id=? "
            "AND version=? "
            "AND revoked_at='' ORDER BY CASE WHEN recipient_id=? THEN 0 ELSE 1 END,"
            "created_at,grant_id",
            (record["workspace_key"], record["artifact_id"], record["version"], actor),
        )
        for row in rows:
            grant = self._grant(row)
            if not self._recipient(grant, actor):
                continue
            if exact is not None and (
                grant["grant_id"] != exact["grant_id"]
                or grant["revision"] != exact["grant_revision"]
            ):
                continue
            if collected is not None:
                collected[grant["grant_id"]] = grant
            return {"grant_id": grant["grant_id"], "grant_revision": grant["revision"]}
        raise _missing()

    def _authorize(
        self, conn, record, actor, *, exact=None, walking=None, memo=None, collected=None
    ):
        walking = set() if walking is None else walking
        memo = {} if memo is None else memo
        key = (record["artifact_id"], record["version"], actor, canonical(exact) if exact else "*")
        if key in memo:
            return memo[key]
        if key in walking or len(walking) > 32 or len(memo) > 1024:
            raise _broken()
        grant = self._direct_grant(conn, record, actor, exact, collected)
        if exact is not None and record["sha256"] != exact["sha256"]:
            raise _error("version_conflict", "资料版本的内容摘要与原引用不一致")
        walking.add(key)
        try:
            for dependency in record["source_refs"]:
                source = self._artifact(conn, record["workspace_key"], dependency["artifact_id"])
                version = self._version(conn, source, dependency["version"])
                original = {name: dependency[name] for name in REF_FIELDS}
                self._authorize(
                    conn,
                    version,
                    dependency["reader_id"],
                    exact=original,
                    walking=walking,
                    memo=memo,
                    collected=collected,
                )
                # A new derived artifact cannot launder a read-only source into
                # broader access. Every reader needs the upstream version too.
                self._authorize(
                    conn, version, actor, walking=walking, memo=memo, collected=collected
                )
        finally:
            walking.remove(key)
        memo[key] = grant
        return grant

    def _snapshot(self, conn, record, actor):
        grant = self._authorize(conn, record, actor)
        public = {
            key: record[key]
            for key in (
                "artifact_id",
                "version",
                "owner_id",
                "title",
                "kind",
                "sha256",
                "bytes",
                "mime_type",
                "suffix",
                "created_at",
                "evidence",
            )
        }
        public["source_refs"] = [
            {key: ref[key] for key in REF_FIELDS} for ref in record["source_refs"]
        ]
        return public | grant

    def _select(self, conn, workspace, actor, identity, version=None):
        artifact = self._artifact(conn, workspace, identity)
        if version is not None:
            record = self._version(conn, artifact, version)
            return record, self._snapshot(conn, record, actor)
        versions = (
            [artifact["latest_version"]]
            if actor == artifact["owner_id"]
            else [
                row[0]
                for row in conn.execute(
                    "SELECT version FROM artifact_grants WHERE workspace_key=? AND artifact_id=? "
                    "AND revoked_at='' GROUP BY version ORDER BY version DESC",
                    (workspace, identity),
                )
            ]
        )
        for number in versions:
            record = self._version(conn, artifact, number)
            try:
                return record, self._snapshot(conn, record, actor)
            except PersonalAgentError as exc:
                if exc.code != "not_found":
                    raise
        raise _missing()

    def _authorize_refs(self, conn, workspace, actor, refs):
        memo = {}
        for ref in refs:
            artifact = self._artifact(conn, workspace, ref["artifact_id"])
            record = self._version(conn, artifact, ref["version"])
            self._authorize(conn, record, actor, exact=ref, memo=memo)
        return refs

    def _request(self, conn, workspace, actor, request, operation, payload):
        row = conn.execute(
            "SELECT * FROM artifact_requests WHERE workspace_key=? AND actor_id=? AND request_id=?",
            (workspace, actor, request),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["payload_sha256"] != digest(payload):
            raise _error("idempotency_conflict", "该请求编号已有不同资料操作，请保留原请求")
        result = self._stored(row["result_json"])
        if digest(result) != row["result_sha256"]:
            raise _broken()
        return result

    @staticmethod
    def _save_request(conn, workspace, actor, request, operation, payload, result):
        conn.execute(
            "INSERT INTO artifact_requests VALUES (?,?,?,?,?,?,?,?)",
            (
                workspace,
                actor,
                request,
                operation,
                digest(payload),
                canonical(result),
                digest(result),
                _now(),
            ),
        )

    def _blob(self, content_sha, suffix):
        return self._path(self.blob_dir / content_sha[:2] / (content_sha + suffix))

    def _read_blob(self, record):
        path = self._blob(record["sha256"], record["suffix"])
        try:
            if not path.is_file() or path.stat().st_size != record["bytes"]:
                raise _broken()
            with path.open("rb") as stream:
                content = stream.read(MAX_ASSET_BYTES + 1)
            self._path(path)
            if (
                len(content) != record["bytes"]
                or hashlib.sha256(content).hexdigest() != record["sha256"]
            ):
                raise _broken()
            return content
        except OSError as exc:
            raise _broken() from exc

    def _publish_blob(self, content, content_sha, suffix):
        path = self._blob(content_sha, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path(path)
        descriptor, temporary = tempfile.mkstemp(prefix=".import-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if (
                    self._read_blob(
                        {"sha256": content_sha, "suffix": suffix, "bytes": len(content)}
                    )
                    != content
                ):
                    raise _broken()
        finally:
            Path(temporary).unlink(missing_ok=True)

    def publish(
        self,
        *,
        workspace_key,
        actor_id,
        request_id,
        title,
        kind,
        content,
        suffix,
        artifact_id=None,
        expected_version=None,
        source_refs=None,
        evidence=None,
    ):
        workspace, actor = self._scope(workspace_key, actor_id)
        request = text(request_id, "请求编号", 128, REQUEST_KEY)
        title = text(title, "资料标题", 200)
        if not isinstance(kind, str) or kind not in {"material", "output"}:
            raise invalid("资料类型必须为 material 或 output")
        content, suffix, mime = validate_content(content, suffix, kind=kind)
        refs = normalize_refs([] if source_refs is None else source_refs)
        evidence = json_object({} if evidence is None else evidence, "产物证据")
        _connection_evidence(evidence)
        if artifact_id is None:
            if expected_version is not None:
                raise invalid("新资料不需要旧版本编号")
        else:
            artifact_id, expected_version = self._identity(artifact_id, expected_version)
            if expected_version is None:
                raise invalid("修订资料需要明确旧版本编号")
        content_sha = hashlib.sha256(content).hexdigest()
        payload = {
            "title": title,
            "kind": kind,
            "sha256": content_sha,
            "bytes": len(content),
            "suffix": suffix,
            "artifact_id": artifact_id,
            "expected_version": expected_version,
            "source_refs": refs,
            "evidence": evidence,
        }
        with self._connection(write=True) as conn:
            previous = self._request(conn, workspace, actor, request, "publish", payload)
            if previous is not None:
                return self._select(
                    conn, workspace, actor, previous["artifact_id"], previous["version"]
                )[1]
            self._authorize_refs(conn, workspace, actor, refs)
            prior_sha, number, identity = "", 1, artifact_id or "art_" + uuid.uuid4().hex
            if artifact_id:
                artifact = self._artifact(conn, workspace, identity)
                if artifact["owner_id"] != actor:
                    raise _missing()
                if artifact["latest_version"] != expected_version:
                    raise _error("version_conflict", "资料已更新，请打开最新版本后再修订")
                if artifact["kind"] != kind:
                    raise invalid("修订不能改变资料类型")
                previous_record = self._version(conn, artifact, expected_version)
                self._authorize(conn, previous_record, actor)
                # Revisions retain every prior dependency; omission does not
                # erase a revoked upstream read from the document's lineage.
                merged = {canonical(ref): ref for ref in previous_record["source_refs"]}
                merged.update(
                    {
                        canonical({**ref, "reader_id": actor}): {**ref, "reader_id": actor}
                        for ref in refs
                    }
                )
                stored_refs = sorted(merged.values(), key=canonical)
                if len(stored_refs) > 64:
                    raise invalid("修订资料的来源过多，请拆分为明确产物")
                prior_sha, number = artifact["head_sha256"], expected_version + 1
                integer(number, "产物版本", maximum=10000)
            else:
                stored_refs = [{**ref, "reader_id": actor} for ref in refs]
            timestamp = _now()
            record = {
                "artifact_id": identity,
                "workspace_key": workspace,
                "version": number,
                "owner_id": actor,
                "kind": kind,
                "title": title,
                "sha256": content_sha,
                "bytes": len(content),
                "mime_type": mime,
                "suffix": suffix,
                "created_at": timestamp,
                "source_refs": stored_refs,
                "evidence": evidence,
                "request_id": request,
                "previous_sha256": prior_sha,
            }
            if _connection_evidence(evidence):
                # An old reader rejects this unknown top-level field instead
                # of ignoring the new dynamic source-authorization protocol.
                record["authority_version"] = 2
            self._publish_blob(content, content_sha, suffix)
            if artifact_id is None:
                conn.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)",
                    (
                        identity,
                        workspace,
                        actor,
                        kind,
                        number,
                        digest(record),
                        timestamp,
                    ),
                )
            conn.execute(
                "INSERT INTO artifact_versions VALUES (?,?,?,?)",
                (
                    identity,
                    number,
                    canonical(record),
                    digest(record),
                ),
            )
            if artifact_id is not None:
                conn.execute(
                    "UPDATE artifacts SET latest_version=?,head_sha256=? WHERE artifact_id=?",
                    (
                        number,
                        digest(record),
                        identity,
                    ),
                )
            self._save_request(
                conn,
                workspace,
                actor,
                request,
                "publish",
                payload,
                {"artifact_id": identity, "version": number},
            )
            return self._snapshot(conn, record, actor)

    def get(self, *, workspace_key, actor_id, artifact_id, version=None):
        workspace, actor = self._scope(workspace_key, actor_id)
        identity, version = self._identity(artifact_id, version)
        if not self._database_exists():
            raise _missing()
        with self._connection() as conn:
            return self._select(conn, workspace, actor, identity, version)[1]

    def read_content(self, *, workspace_key, actor_id, artifact_id, version=None):
        workspace, actor = self._scope(workspace_key, actor_id)
        identity, version = self._identity(artifact_id, version)
        if not self._database_exists():
            raise _missing()
        with self._connection() as conn:
            record, _ = self._select(conn, workspace, actor, identity, version)
            return self._read_blob(record)

    def list(self, *, workspace_key, actor_id, kind=None, query="", limit=50, offset=0):
        workspace, actor = self._scope(workspace_key, actor_id)
        if kind is not None and (not isinstance(kind, str) or kind not in {"material", "output"}):
            raise invalid("资料筛选类型不正确")
        if not isinstance(query, str) or len(query) > 200:
            raise invalid("搜索词最多允许 200 个字符")
        if query:
            text(query, "搜索词", 200)
        integer(limit, "列表数量", maximum=500)
        integer(offset, "列表偏移", minimum=0, maximum=2**63 - 1)
        if not self._database_exists():
            return []
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT artifact_id FROM artifacts WHERE workspace_key=? "
                "AND (? IS NULL OR kind=?) AND (owner_id=? OR artifact_id IN "
                "(SELECT artifact_id FROM artifact_grants WHERE workspace_key=? "
                "AND revoked_at='')) "
                "ORDER BY created_at DESC,artifact_id DESC",
                (workspace, kind, kind, actor, workspace),
            )
            result = []
            skipped = 0
            for row in rows:
                try:
                    view = self._select(conn, workspace, actor, row[0])[1]
                except PersonalAgentError as exc:
                    if exc.code != "not_found":
                        raise
                    continue
                if query.casefold() not in view["title"].casefold():
                    continue
                if skipped < offset:
                    skipped += 1
                else:
                    result.append(view)
                    if len(result) == limit:
                        break
            return result

    def versions(self, *, workspace_key, actor_id, artifact_id):
        workspace, actor = self._scope(workspace_key, actor_id)
        identity, _ = self._identity(artifact_id)
        if not self._database_exists():
            raise _missing()
        with self._connection() as conn:
            artifact = self._artifact(conn, workspace, identity)
            result = []
            for number in range(artifact["latest_version"], 0, -1):
                try:
                    result.append(
                        self._snapshot(conn, self._version(conn, artifact, number), actor)
                    )
                except PersonalAgentError as exc:
                    if exc.code != "not_found":
                        raise
            if not result:
                raise _missing()
            return result

    def share(
        self,
        *,
        workspace_key,
        actor_id,
        artifact_id,
        version,
        recipient_id,
        request_id,
        recipient_scope=None,
    ):
        workspace, actor = self._scope(workspace_key, actor_id)
        identity, version = self._identity(artifact_id, version)
        if version is None:
            raise invalid("分享需要确切资料版本")
        recipient = text(recipient_id, "接收 Agent", 35, PRINCIPAL)
        if actor == recipient:
            raise invalid("自有资料无需分享给自己")
        request = text(request_id, "请求编号", 128, REQUEST_KEY)
        scope = normalize_recipient_scope(
            {} if recipient_scope is None else recipient_scope, recipient
        )
        payload = {
            "artifact_id": identity,
            "version": version,
            "recipient_id": recipient,
            "recipient_scope": scope,
        }
        with self._connection(write=True) as conn:
            previous = self._request(conn, workspace, actor, request, "share", payload)
            if previous is not None:
                row = conn.execute(
                    "SELECT * FROM artifact_grants WHERE grant_id=?", (previous["grant_id"],)
                ).fetchone()
                if row is None:
                    raise _broken()
                return self._grant(row)
            artifact = self._artifact(conn, workspace, identity)
            if artifact["owner_id"] != actor:
                raise _missing()
            record = self._version(conn, artifact, version)
            self._authorize(conn, record, actor)
            readers = {recipient, *scope.get("member_ids", [])}
            for dependency in record["source_refs"]:
                source = self._artifact(conn, workspace, dependency["artifact_id"])
                for reader in readers:
                    self._authorize(
                        conn, self._version(conn, source, dependency["version"]), reader
                    )
            existing = conn.execute(
                "SELECT * FROM artifact_grants WHERE workspace_key=? AND artifact_id=? "
                "AND version=? "
                "AND recipient_id=? AND revoked_at=''",
                (workspace, identity, version, recipient),
            ).fetchone()
            if existing:
                grant = self._grant(existing)
                if grant["recipient_scope"] != scope:
                    raise _error("version_conflict", "原分享范围已存在，请先撤销后重新确认")
            else:
                grant = {
                    "grant_id": "asg_" + uuid.uuid4().hex,
                    "workspace_key": workspace,
                    "artifact_id": identity,
                    "version": version,
                    "owner_id": actor,
                    "recipient_id": recipient,
                    "recipient_scope": scope,
                    "revision": 1,
                    "created_at": _now(),
                    "revoked_at": "",
                }
                conn.execute(
                    "INSERT INTO artifact_grants VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant["grant_id"],
                        workspace,
                        identity,
                        version,
                        actor,
                        recipient,
                        canonical(scope),
                        1,
                        grant["created_at"],
                        "",
                        digest(grant),
                    ),
                )
            self._save_request(
                conn, workspace, actor, request, "share", payload, {"grant_id": grant["grant_id"]}
            )
            return grant

    def revoke(self, *, workspace_key, actor_id, grant_id, expected_revision, request_id):
        workspace, actor = self._scope(workspace_key, actor_id)
        grant_id = text(grant_id, "授权编号", 36, GRANT_ID)
        integer(expected_revision, "原授权版本", maximum=2)
        request = text(request_id, "请求编号", 128, REQUEST_KEY)
        payload = {"grant_id": grant_id, "expected_revision": expected_revision}
        with self._connection(write=True) as conn:
            previous = self._request(conn, workspace, actor, request, "revoke", payload)
            row = conn.execute(
                "SELECT * FROM artifact_grants WHERE workspace_key=? AND owner_id=? AND grant_id=?",
                (workspace, actor, grant_id),
            ).fetchone()
            if row is None:
                raise _missing()
            grant = self._grant(row)
            if previous is not None:
                return grant
            if grant["revision"] != expected_revision:
                raise _error("version_conflict", "分享授权已更新，请刷新后重试")
            if not grant["revoked_at"]:
                grant = grant | {"revision": 2, "revoked_at": _now()}
                conn.execute(
                    "UPDATE artifact_grants SET revision=2,revoked_at=?,record_sha256=? "
                    "WHERE grant_id=?",
                    (
                        grant["revoked_at"],
                        digest(grant),
                        grant_id,
                    ),
                )
            self._save_request(
                conn, workspace, actor, request, "revoke", payload, {"grant_id": grant_id}
            )
            return grant

    def grants(self, *, workspace_key, actor_id, artifact_id, version):
        workspace, actor = self._scope(workspace_key, actor_id)
        identity, version = self._identity(artifact_id, version)
        if version is None:
            raise invalid("授权列表需要确切资料版本")
        if not self._database_exists():
            raise _missing()
        with self._connection() as conn:
            artifact = self._artifact(conn, workspace, identity)
            if artifact["owner_id"] != actor:
                raise _missing()
            self._version(conn, artifact, version)
            return [
                self._grant(row)
                for row in conn.execute(
                    "SELECT * FROM artifact_grants WHERE workspace_key=? AND artifact_id=? "
                    "AND version=? ORDER BY created_at,grant_id",
                    (workspace, identity, version),
                )
            ]

    def authorize_refs(self, *, workspace_key, actor_id, refs):
        workspace, actor = self._scope(workspace_key, actor_id)
        refs = normalize_refs(refs, maximum=MAX_SESSION_REFS)
        if not refs:
            return []
        if not self._database_exists():
            raise _missing()
        with self._connection() as conn:
            return self._authorize_refs(conn, workspace, actor, refs)

    def get_grant(self, *, workspace_key, actor_id, grant_id):
        workspace, actor = self._scope(workspace_key, actor_id)
        grant_id = text(grant_id, "授权编号", 36, GRANT_ID)
        if not self._database_exists():
            raise _missing()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_grants WHERE workspace_key=? AND grant_id=?",
                (workspace, grant_id),
            ).fetchone()
            if row is None:
                raise _missing()
            grant = self._grant(row)
            if actor != grant["owner_id"] and not self._recipient(grant, actor):
                raise _missing()
            return grant

    def access_grants(self, *, workspace_key, actor_id, artifact_id, version):
        workspace, actor = self._scope(workspace_key, actor_id)
        identity, version = self._identity(artifact_id, version)
        if version is None:
            raise invalid("资料授权核验需要确切版本")
        if not self._database_exists():
            raise _missing()
        with self._connection() as conn:
            record, _ = self._select(conn, workspace, actor, identity, version)
            collected = {}
            self._authorize(conn, record, actor, collected=collected)
            return sorted(collected.values(), key=lambda item: item["grant_id"])

    def _session(self, conn, workspace, session, actor):
        row = conn.execute(
            "SELECT * FROM artifact_session_refs WHERE workspace_key=? AND session_id=?",
            (workspace, session),
        ).fetchone()
        if row is None:
            return []
        if row["actor_id"] != actor:
            raise _error("binding_conflict", "原对话的资料身份不一致")
        refs = self._stored(row["refs_json"])
        if (
            digest(
                {"workspace_key": workspace, "session_id": session, "actor_id": actor, "refs": refs}
            )
            != row["record_sha256"]
        ):
            raise _broken()
        try:
            return normalize_refs(refs, maximum=MAX_SESSION_REFS)
        except PersonalAgentError as exc:
            raise _broken() from exc

    def bind_session_refs(self, *, workspace_key, session_id, actor_id, refs):
        workspace, actor = self._scope(workspace_key, actor_id)
        session = text(session_id, "对话编号", 128, REQUEST_KEY)
        refs = normalize_refs(refs, maximum=MAX_SESSION_REFS)
        with self._connection(write=True) as conn:
            current = self._session(conn, workspace, session, actor)
            merged = {canonical(ref): ref for ref in [*current, *refs]}
            combined = normalize_refs(
                sorted(merged.values(), key=canonical), maximum=MAX_SESSION_REFS
            )
            self._authorize_refs(conn, workspace, actor, combined)
            data = {
                "workspace_key": workspace,
                "session_id": session,
                "actor_id": actor,
                "refs": combined,
            }
            conn.execute(
                "INSERT INTO artifact_session_refs VALUES (?,?,?,?,?) "
                "ON CONFLICT(workspace_key,session_id) "
                "DO UPDATE SET refs_json=excluded.refs_json,record_sha256=excluded.record_sha256",
                (
                    workspace,
                    session,
                    actor,
                    canonical(combined),
                    digest(data),
                ),
            )
            return combined

    def session_refs(self, *, workspace_key, session_id, actor_id):
        workspace, actor = self._scope(workspace_key, actor_id)
        session = text(session_id, "对话编号", 128, REQUEST_KEY)
        if not self._database_exists():
            return []
        with self._connection() as conn:
            return self._authorize_refs(
                conn, workspace, actor, self._session(conn, workspace, session, actor)
            )
