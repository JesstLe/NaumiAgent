"""Strictly read-only health projection for the durable worker authority."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from naumi_agent.daemons.worker_contract import WorkerContract
from naumi_agent.daemons.worker_registry import (
    WORKER_REGISTRY_SCHEMA_VERSION,
    WorkerRegistrationState,
    deserialize_worker_registration,
)
from naumi_agent.harness.heartbeat import (
    HarnessHeartbeat,
    HarnessHeartbeatHealth,
    HarnessHeartbeatPhase,
    assess_heartbeat,
)
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HARNESS_STORE_SCHEMA_VERSION

WorkerRegistryHealth = Literal["absent", "ready"]
HeartbeatStoreHealth = Literal["not_needed", "absent", "ready", "incompatible", "error"]
WorkerHeartbeatHealth = Literal[
    "starting",
    "healthy",
    "draining",
    "stale",
    "offline",
    "stopped",
    "failed",
    "clock_regression",
    "missing",
    "identity_mismatch",
    "invalid",
    "unavailable",
]


class WorkerAuthorityHealthError(RuntimeError):
    """Raised when registry authority facts cannot be trusted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkerAuthorityEntry:
    worker_id: str
    kind: str
    epoch: int
    platform: str
    machine: str
    max_concurrent_jobs: int
    heartbeat_health: WorkerHeartbeatHealth
    heartbeat_age_seconds: float | None


@dataclass(frozen=True, slots=True)
class WorkerAuthoritySnapshot:
    registry_health: WorkerRegistryHealth
    heartbeat_store_health: HeartbeatStoreHealth
    active_count: int
    workers: tuple[WorkerAuthorityEntry, ...]
    truncated: bool


def inspect_worker_authority_health(
    *,
    registry_db_path: str | Path,
    harness_db_path: str | Path,
    workspace_root: str | Path,
    now: str | None = None,
    limit: int = 5,
) -> WorkerAuthoritySnapshot:
    """Inspect registry contracts and matching heartbeats without creating a Store."""
    registry_path = _absolute_path(registry_db_path, field="registry_db_path")
    harness_path = _absolute_path(harness_db_path, field="harness_db_path")
    workspace = str(Path(workspace_root).expanduser().resolve(strict=False))
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit 必须在 1 到 20 之间。")
    assessed_at = now or datetime.now(UTC).isoformat()

    registry_kind = _file_kind(registry_path)
    if registry_kind == "absent":
        return WorkerAuthoritySnapshot("absent", "not_needed", 0, (), False)
    if registry_kind != "file":
        raise WorkerAuthorityHealthError("registry_wrong_type", "Worker registry 路径不是文件。")

    try:
        with closing(_open_read_only(registry_path)) as db:
            version = _user_version(db)
            if version != WORKER_REGISTRY_SCHEMA_VERSION:
                raise WorkerAuthorityHealthError(
                    "registry_schema_incompatible",
                    f"Worker registry schema v{version} 不受支持。",
                )
            active_count = int(
                db.execute(
                    "SELECT COUNT(*) FROM worker_registrations WHERE state = 'active'"
                ).fetchone()[0]
            )
            rows = db.execute(
                """
                SELECT * FROM worker_registrations
                WHERE state = 'active' ORDER BY worker_id ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            registrations = tuple(deserialize_worker_registration(dict(row)) for row in rows)
            if any(item.state is not WorkerRegistrationState.ACTIVE for item in registrations):
                raise ValueError("Worker registry active 查询返回了非 active 记录。")
    except WorkerAuthorityHealthError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise WorkerAuthorityHealthError(
            "registry_unreadable", "Worker registry 无法可信读取。"
        ) from exc

    if not registrations:
        return WorkerAuthoritySnapshot("ready", "not_needed", active_count, (), False)

    heartbeat_store_health, heartbeats = _read_heartbeats(
        harness_path,
        workspace=workspace,
        worker_ids=tuple(item.contract.worker_id for item in registrations),
    )
    workers = tuple(
        _entry(
            registration.contract,
            heartbeats=heartbeats,
            heartbeat_store_health=heartbeat_store_health,
            now=assessed_at,
        )
        for registration in registrations
    )
    return WorkerAuthoritySnapshot(
        "ready",
        heartbeat_store_health,
        active_count,
        workers,
        active_count > len(workers),
    )


def _read_heartbeats(
    path: Path,
    *,
    workspace: str,
    worker_ids: tuple[str, ...],
) -> tuple[HeartbeatStoreHealth, dict[str, tuple[HarnessHeartbeat, ...]]]:
    try:
        kind = _file_kind(path)
    except WorkerAuthorityHealthError:
        return "error", {}
    if kind == "absent":
        return "absent", {}
    if kind != "file":
        return "error", {}
    try:
        with closing(_open_read_only(path)) as db:
            if _user_version(db) != HARNESS_STORE_SCHEMA_VERSION:
                return "incompatible", {}
            placeholders = ",".join("?" for _ in worker_ids)
            rows = db.execute(
                f"""
                SELECT workspace_root, subject_kind, subject_id, instance_id, epoch,
                       sequence, phase, observed_at, timeout_seconds, detail_code
                FROM harness_heartbeats
                WHERE workspace_root = ? AND subject_id IN ({placeholders})
                ORDER BY subject_id, subject_kind
                """,
                (workspace, *worker_ids),
            ).fetchall()
        grouped: dict[str, list[HarnessHeartbeat]] = {}
        for row in rows:
            heartbeat = _heartbeat_from_record(dict(row))
            grouped.setdefault(heartbeat.subject_id, []).append(heartbeat)
        return "ready", {key: tuple(value) for key, value in grouped.items()}
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return "error", {}


def _entry(
    contract: WorkerContract,
    *,
    heartbeats: dict[str, tuple[HarnessHeartbeat, ...]],
    heartbeat_store_health: HeartbeatStoreHealth,
    now: str,
) -> WorkerAuthorityEntry:
    health: WorkerHeartbeatHealth
    age: float | None = None
    if heartbeat_store_health != "ready":
        health = "missing" if heartbeat_store_health == "absent" else "unavailable"
    else:
        expected_kind = HarnessRunKind(contract.kind.value)
        candidates = heartbeats.get(contract.worker_id, ())
        matches = tuple(item for item in candidates if item.subject_kind is expected_kind)
        if len(matches) != 1:
            health = "identity_mismatch" if candidates else "missing"
        else:
            heartbeat = matches[0]
            if heartbeat.instance_id != contract.instance_id or heartbeat.epoch != contract.epoch:
                health = "identity_mismatch"
            else:
                try:
                    snapshot = assess_heartbeat(heartbeat, now=now)
                except ValueError:
                    health = "invalid"
                else:
                    health = _HEARTBEAT_HEALTH[snapshot.health]
                    age = snapshot.age_seconds
    return WorkerAuthorityEntry(
        worker_id=contract.worker_id,
        kind=contract.kind.value,
        epoch=contract.epoch,
        platform=contract.platform.system,
        machine=contract.platform.machine,
        max_concurrent_jobs=contract.resources.max_concurrent_jobs,
        heartbeat_health=health,
        heartbeat_age_seconds=age,
    )


def _heartbeat_from_record(record: dict[str, object]) -> HarnessHeartbeat:
    heartbeat = HarnessHeartbeat(
        workspace_root=str(record["workspace_root"]),
        subject_kind=HarnessRunKind(str(record["subject_kind"])),
        subject_id=str(record["subject_id"]),
        instance_id=str(record["instance_id"]),
        epoch=int(record["epoch"]),
        sequence=int(record["sequence"]),
        phase=HarnessHeartbeatPhase(str(record["phase"])),
        observed_at=str(record["observed_at"]),
        timeout_seconds=int(record["timeout_seconds"]),
        detail_code=str(record["detail_code"]),
    )
    if heartbeat.epoch < 1 or heartbeat.sequence < 1:
        raise ValueError("Heartbeat epoch/sequence 无效。")
    return heartbeat


def _open_read_only(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only = ON")
    db.execute("PRAGMA busy_timeout = 200")
    return db


def _user_version(db: sqlite3.Connection) -> int:
    row = db.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise ValueError("SQLite user_version 不可读。")
    return int(row[0])


def _absolute_path(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} 必须是绝对路径。")
    return path.resolve(strict=False)


def _file_kind(path: Path) -> Literal["absent", "file", "other"]:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise WorkerAuthorityHealthError("path_unreadable", "Worker 状态路径不可读。") from exc
    if stat.S_ISREG(mode):
        return "file"
    return "other"


_HEARTBEAT_HEALTH: dict[HarnessHeartbeatHealth, WorkerHeartbeatHealth] = {
    HarnessHeartbeatHealth.STARTING: "starting",
    HarnessHeartbeatHealth.HEALTHY: "healthy",
    HarnessHeartbeatHealth.DRAINING: "draining",
    HarnessHeartbeatHealth.STALE: "stale",
    HarnessHeartbeatHealth.OFFLINE: "offline",
    HarnessHeartbeatHealth.STOPPED: "stopped",
    HarnessHeartbeatHealth.FAILED: "failed",
    HarnessHeartbeatHealth.CLOCK_REGRESSION: "clock_regression",
}


__all__ = [
    "WorkerAuthorityEntry",
    "WorkerAuthorityHealthError",
    "WorkerAuthoritySnapshot",
    "inspect_worker_authority_health",
]
