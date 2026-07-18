"""Authoritative physical Store Catalog and read-only runtime inspection."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from naumi_agent.evolution.store import (
    EVOLUTION_STORE_SCHEMA_VERSION,
    resolve_evolution_db_path,
)
from naumi_agent.harness.store import (
    HARNESS_STORE_SCHEMA_VERSION,
    resolve_harness_db_path,
)
from naumi_agent.harness.trust import resolve_harness_trust_db_path

if TYPE_CHECKING:
    from naumi_agent.config.settings import AppConfig

_MAX_JSON_CATALOG_READ_BYTES = 8 * 1024 * 1024


class StoreCatalogError(ValueError):
    """Raised when the static catalog itself is ambiguous or incomplete."""


class _StoreReadLimitError(RuntimeError):
    """Internal signal for valid-looking metadata that exceeds bounded inspection."""


class CatalogStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    ERROR = "error"


class StorageKind(StrEnum):
    SQLITE = "sqlite"
    JSON = "json"
    DIRECTORY = "directory"


class VersionStrategy(StrEnum):
    SQLITE_USER_VERSION = "sqlite_user_version"
    JSON_SCHEMA_VERSION = "json_schema_version"
    UNVERSIONED = "unversioned"
    EXTERNAL = "external"


class DataSensitivity(StrEnum):
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class RetentionPolicy(StrEnum):
    USER_MANAGED = "user_managed"
    SESSION_COUPLED = "session_coupled"
    BOUNDED_HISTORY = "bounded_history"
    AUDIT_LONG_TERM = "audit_long_term"
    EXTERNAL_MANAGED = "external_managed"


class StoreState(StrEnum):
    ABSENT = "absent"
    READY = "ready"
    LEGACY_UNVERSIONED = "legacy_unversioned"
    UPGRADE_REQUIRED = "upgrade_required"
    UNSUPPORTED_NEWER = "unsupported_newer"
    CORRUPT = "corrupt"
    WRONG_TYPE = "wrong_type"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class StoreDefinition:
    """One physical persistence location and its governance metadata."""

    store_id: str
    path: Path
    kind: StorageKind
    owners: tuple[str, ...]
    version_strategy: VersionStrategy
    supported_schema_version: int | None
    sensitivity: DataSensitivity
    retention: RetentionPolicy
    lazy: bool
    description: str


@dataclass(frozen=True, slots=True)
class StoreIssue:
    code: str
    message: str
    status: CatalogStatus


@dataclass(frozen=True, slots=True)
class StoreObservation:
    definition: StoreDefinition
    state: StoreState
    status: CatalogStatus
    observed_schema_version: int | None
    size_bytes: int | None
    permission_mode: int | None
    issues: tuple[StoreIssue, ...] = ()

    @property
    def issue_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)


@dataclass(frozen=True, slots=True)
class StoreCatalogReport:
    stores: tuple[StoreObservation, ...]

    @property
    def status(self) -> CatalogStatus:
        return _max_status(*(store.status for store in self.stores))

    @property
    def existing_count(self) -> int:
        return sum(store.state is not StoreState.ABSENT for store in self.stores)

    @property
    def absent_count(self) -> int:
        return sum(store.state is StoreState.ABSENT for store in self.stores)

    @property
    def warning_count(self) -> int:
        return sum(store.status is CatalogStatus.WARN for store in self.stores)

    @property
    def error_count(self) -> int:
        return sum(store.status is CatalogStatus.ERROR for store in self.stores)


def build_store_catalog(config: AppConfig) -> tuple[StoreDefinition, ...]:
    """Build the static physical Store Catalog without touching the filesystem."""
    runtime_dir = _canonical_path(Path(config.memory.session_db_path).parent)
    definitions = (
        _definition(
            "runtime.core",
            config.memory.session_db_path,
            StorageKind.SQLITE,
            ("memory.sessions", "tasks", "workbench"),
            VersionStrategy.UNVERSIONED,
            None,
            DataSensitivity.RESTRICTED,
            RetentionPolicy.USER_MANAGED,
            "共享 Session、Task 与 Workbench 状态库",
        ),
        _definition(
            "runtime.runs",
            runtime_dir / "chat-runs.db",
            StorageKind.SQLITE,
            ("runs",),
            VersionStrategy.UNVERSIONED,
            None,
            DataSensitivity.RESTRICTED,
            RetentionPolicy.SESSION_COUPLED,
            "会话执行记录、步骤、artifact 引用和来源",
        ),
        _definition(
            "runtime.goals",
            runtime_dir / "goals" / "goals.db",
            StorageKind.SQLITE,
            ("orchestrator.goal",),
            VersionStrategy.UNVERSIONED,
            None,
            DataSensitivity.CONFIDENTIAL,
            RetentionPolicy.USER_MANAGED,
            "跨轮次持久 Goal",
        ),
        _definition(
            "runtime.pursuit",
            runtime_dir / "pursuit" / "pursuit.db",
            StorageKind.SQLITE,
            ("orchestrator.pursuit",),
            VersionStrategy.UNVERSIONED,
            None,
            DataSensitivity.RESTRICTED,
            RetentionPolicy.AUDIT_LONG_TERM,
            "Pursuit 运行、证据和等待状态",
        ),
        _definition(
            "tasks.scheduler",
            runtime_dir / "scheduler" / "schedules.db",
            StorageKind.SQLITE,
            ("scheduler",),
            VersionStrategy.UNVERSIONED,
            None,
            DataSensitivity.CONFIDENTIAL,
            RetentionPolicy.USER_MANAGED,
            "调度任务及触发事件",
        ),
        _definition(
            "harness.evidence",
            resolve_harness_db_path(),
            StorageKind.SQLITE,
            ("harness",),
            VersionStrategy.SQLITE_USER_VERSION,
            HARNESS_STORE_SCHEMA_VERSION,
            DataSensitivity.RESTRICTED,
            RetentionPolicy.AUDIT_LONG_TERM,
            "Harness Run、Evidence、Replay、Session 协调记录与重试 tombstone",
        ),
        _definition(
            "harness.trust",
            resolve_harness_trust_db_path(),
            StorageKind.SQLITE,
            ("harness.trust",),
            VersionStrategy.UNVERSIONED,
            None,
            DataSensitivity.RESTRICTED,
            RetentionPolicy.USER_MANAGED,
            "用户级 Harness Profile 信任状态",
        ),
        _definition(
            "evolution.candidates",
            resolve_evolution_db_path(),
            StorageKind.SQLITE,
            ("evolution", "harness.feedback"),
            VersionStrategy.SQLITE_USER_VERSION,
            EVOLUTION_STORE_SCHEMA_VERSION,
            DataSensitivity.RESTRICTED,
            RetentionPolicy.AUDIT_LONG_TERM,
            "不可变 Evolution Evidence、候选聚合物与修订审计事件",
        ),
        _definition(
            "tasks.background",
            runtime_dir / "background" / "tasks.json",
            StorageKind.JSON,
            ("background",),
            VersionStrategy.UNVERSIONED,
            None,
            DataSensitivity.CONFIDENTIAL,
            RetentionPolicy.BOUNDED_HISTORY,
            "后台任务元数据；日志位于同目录 artifacts",
        ),
        _definition(
            "browser.runtime",
            runtime_dir / "browser",
            StorageKind.DIRECTORY,
            ("tools.browser",),
            VersionStrategy.UNVERSIONED,
            None,
            DataSensitivity.RESTRICTED,
            RetentionPolicy.BOUNDED_HISTORY,
            "浏览器 task-runs、截图、视频、trace 与事件",
        ),
        _definition(
            "browser.daemon",
            runtime_dir / "browser-daemon",
            StorageKind.DIRECTORY,
            ("browser.daemon",),
            VersionStrategy.UNVERSIONED,
            None,
            DataSensitivity.CONFIDENTIAL,
            RetentionPolicy.BOUNDED_HISTORY,
            "浏览器 daemon 日志与进程诊断状态",
        ),
        _definition(
            "memory.vector",
            config.memory.vector_db_path,
            StorageKind.DIRECTORY,
            ("memory.long_term",),
            VersionStrategy.EXTERNAL,
            None,
            DataSensitivity.RESTRICTED,
            RetentionPolicy.EXTERNAL_MANAGED,
            "Chroma 长期记忆目录，由上游引擎管理内部 schema",
        ),
    )
    _validate_definitions(definitions)
    return definitions


def inspect_store_catalog(
    definitions: tuple[StoreDefinition, ...],
) -> StoreCatalogReport:
    """Inspect Store metadata without creating, migrating, or modifying state."""
    _validate_definitions(definitions)
    return StoreCatalogReport(stores=tuple(_inspect_store(item) for item in definitions))


def _definition(
    store_id: str,
    path: str | Path,
    kind: StorageKind,
    owners: tuple[str, ...],
    version_strategy: VersionStrategy,
    supported_schema_version: int | None,
    sensitivity: DataSensitivity,
    retention: RetentionPolicy,
    description: str,
) -> StoreDefinition:
    return StoreDefinition(
        store_id=store_id,
        path=_canonical_path(path),
        kind=kind,
        owners=owners,
        version_strategy=version_strategy,
        supported_schema_version=supported_schema_version,
        sensitivity=sensitivity,
        retention=retention,
        lazy=True,
        description=description,
    )


def _validate_definitions(definitions: tuple[StoreDefinition, ...]) -> None:
    if not definitions:
        raise StoreCatalogError("Store Catalog 不能为空。")
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for item in definitions:
        if not item.store_id.strip() or item.store_id in seen_ids:
            raise StoreCatalogError(f"Store Catalog ID 重复或为空：{item.store_id!r}")
        if not item.path.is_absolute() or item.path in seen_paths:
            raise StoreCatalogError(f"Store Catalog 路径重复或非绝对路径：{item.path}")
        if not item.owners or any(not owner.strip() for owner in item.owners):
            raise StoreCatalogError(f"Store {item.store_id} 缺少 owner。")
        if not item.description.strip():
            raise StoreCatalogError(f"Store {item.store_id} 缺少用途说明。")
        if item.version_strategy in {
            VersionStrategy.SQLITE_USER_VERSION,
            VersionStrategy.JSON_SCHEMA_VERSION,
        } and (item.supported_schema_version is None or item.supported_schema_version < 1):
            raise StoreCatalogError(f"Store {item.store_id} 缺少有效 supported schema version。")
        if (
            item.version_strategy is VersionStrategy.SQLITE_USER_VERSION
            and item.kind is not StorageKind.SQLITE
        ):
            raise StoreCatalogError(
                f"Store {item.store_id} 的 SQLite version strategy 与类型不符。"
            )
        if (
            item.version_strategy is VersionStrategy.JSON_SCHEMA_VERSION
            and item.kind is not StorageKind.JSON
        ):
            raise StoreCatalogError(
                f"Store {item.store_id} 的 JSON version strategy 与类型不符。"
            )
        seen_ids.add(item.store_id)
        seen_paths.add(item.path)


def _inspect_store(definition: StoreDefinition) -> StoreObservation:
    path = definition.path
    try:
        exists = path.exists()
    except OSError as exc:
        return _observation_error(definition, StoreState.UNREADABLE, "stat_failed", exc)
    if not exists:
        return StoreObservation(
            definition=definition,
            state=StoreState.ABSENT,
            status=CatalogStatus.PASS if definition.lazy else CatalogStatus.ERROR,
            observed_schema_version=None,
            size_bytes=None,
            permission_mode=None,
        )

    if definition.kind is StorageKind.DIRECTORY:
        if not path.is_dir():
            return _wrong_type(definition, expected="目录")
        state = (
            StoreState.READY
            if definition.version_strategy is VersionStrategy.EXTERNAL
            else StoreState.LEGACY_UNVERSIONED
        )
        version = None
        size = None
    else:
        if not path.is_file():
            return _wrong_type(definition, expected="文件")
        try:
            size = path.stat().st_size
        except OSError as exc:
            return _observation_error(definition, StoreState.UNREADABLE, "stat_failed", exc)
        if definition.kind is StorageKind.SQLITE:
            try:
                version = _read_sqlite_user_version(path)
            except (OSError, sqlite3.Error) as exc:
                return _observation_error(definition, StoreState.CORRUPT, "sqlite_unreadable", exc)
            state = _version_state(definition, version)
        else:
            try:
                version = _read_json_schema_version(path, definition.version_strategy)
            except _StoreReadLimitError as exc:
                return _observation_error(
                    definition,
                    StoreState.UNREADABLE,
                    "json_read_limit_exceeded",
                    exc,
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                return _observation_error(definition, StoreState.CORRUPT, "json_unreadable", exc)
            state = _version_state(definition, version)

    issues = _permission_issues(definition)
    return StoreObservation(
        definition=definition,
        state=state,
        status=_max_status(_state_status(state), *(issue.status for issue in issues)),
        observed_schema_version=version,
        size_bytes=size,
        permission_mode=_permission_mode(path),
        issues=issues,
    )


def _read_sqlite_user_version(path: Path) -> int:
    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute("PRAGMA user_version").fetchone()
        if row is None:
            raise sqlite3.DatabaseError("PRAGMA user_version 未返回结果")
        return int(row[0])
    finally:
        connection.close()


def _read_json_schema_version(path: Path, strategy: VersionStrategy) -> int | None:
    size = path.stat().st_size
    if size > _MAX_JSON_CATALOG_READ_BYTES:
        raise _StoreReadLimitError("JSON metadata 超过 Catalog 有界读取上限")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if strategy is not VersionStrategy.JSON_SCHEMA_VERSION:
        return None
    if not isinstance(payload, dict):
        raise ValueError("JSON 根节点必须是对象")
    version = payload.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("schema_version 必须是正整数")
    return version


def _version_state(definition: StoreDefinition, observed: int | None) -> StoreState:
    if definition.version_strategy in {VersionStrategy.UNVERSIONED}:
        return StoreState.LEGACY_UNVERSIONED
    if definition.version_strategy is VersionStrategy.EXTERNAL:
        return StoreState.READY
    supported = definition.supported_schema_version
    assert supported is not None
    assert observed is not None
    if observed == supported:
        return StoreState.READY
    if observed < supported:
        return StoreState.UPGRADE_REQUIRED
    return StoreState.UNSUPPORTED_NEWER


def _permission_issues(definition: StoreDefinition) -> tuple[StoreIssue, ...]:
    if os.name == "nt":
        return ()
    issues: list[StoreIssue] = []
    mode = _permission_mode(definition.path)
    if mode is not None and mode & 0o077:
        issues.append(
            StoreIssue(
                code="permissions_too_open",
                message=f"权限 {mode:04o} 向 group/other 开放",
                status=CatalogStatus.WARN,
            )
        )
    if definition.kind is not StorageKind.DIRECTORY:
        parent_mode = _permission_mode(definition.path.parent)
        if parent_mode is not None and parent_mode & 0o077:
            issues.append(
                StoreIssue(
                    code="parent_permissions_too_open",
                    message=f"父目录权限 {parent_mode:04o} 向 group/other 开放",
                    status=CatalogStatus.WARN,
                )
            )
    return tuple(issues)


def _permission_mode(path: Path) -> int | None:
    if os.name == "nt":
        return None
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _state_status(state: StoreState) -> CatalogStatus:
    if state in {StoreState.ABSENT, StoreState.READY}:
        return CatalogStatus.PASS
    if state in {StoreState.LEGACY_UNVERSIONED, StoreState.UPGRADE_REQUIRED}:
        return CatalogStatus.WARN
    return CatalogStatus.ERROR


def _wrong_type(definition: StoreDefinition, *, expected: str) -> StoreObservation:
    issue = StoreIssue(
        code="wrong_type",
        message=f"路径存在，但不是预期的{expected}",
        status=CatalogStatus.ERROR,
    )
    return StoreObservation(
        definition=definition,
        state=StoreState.WRONG_TYPE,
        status=CatalogStatus.ERROR,
        observed_schema_version=None,
        size_bytes=None,
        permission_mode=_permission_mode(definition.path),
        issues=(issue,),
    )


def _observation_error(
    definition: StoreDefinition,
    state: StoreState,
    code: str,
    exc: BaseException,
) -> StoreObservation:
    issue = StoreIssue(
        code=code,
        message=f"{type(exc).__name__}: {str(exc)[:160]}",
        status=CatalogStatus.ERROR,
    )
    return StoreObservation(
        definition=definition,
        state=state,
        status=CatalogStatus.ERROR,
        observed_schema_version=None,
        size_bytes=None,
        permission_mode=_permission_mode(definition.path),
        issues=(issue,),
    )


def _max_status(*statuses: CatalogStatus) -> CatalogStatus:
    rank = {
        CatalogStatus.PASS: 0,
        CatalogStatus.WARN: 1,
        CatalogStatus.ERROR: 2,
    }
    return max(statuses, key=rank.__getitem__, default=CatalogStatus.PASS)


def _canonical_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)
