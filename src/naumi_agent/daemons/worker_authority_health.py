"""Strictly read-only health projection for the durable worker authority."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from naumi_agent.daemons.agent_worker_supervisor_contract import (
    supervisor_fence_receipt_from_json,
)
from naumi_agent.daemons.worker_contract import (
    WorkerContract,
    normalize_worker_timestamp,
)
from naumi_agent.daemons.worker_registry import (
    WORKER_REGISTRY_SCHEMA_VERSION,
    WorkerCapacityReservationState,
    WorkerCapacityWaiterState,
    WorkerRegistrationState,
    deserialize_worker_capacity_reservation,
    deserialize_worker_capacity_waiter,
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
    capabilities: tuple[str, ...]
    dispatch_ready: bool
    max_concurrent_jobs: int
    reserved_jobs: int
    available_jobs: int
    queue_max_waiters: int | None
    waiting_jobs: int
    active_claims: int
    expired_waiting_jobs: int
    expired_claims: int
    oldest_wait_seconds: float | None
    heartbeat_health: WorkerHeartbeatHealth
    heartbeat_age_seconds: float | None


@dataclass(frozen=True, slots=True)
class WorkerAuthoritySnapshot:
    registry_health: WorkerRegistryHealth
    heartbeat_store_health: HeartbeatStoreHealth
    active_count: int
    workers: tuple[WorkerAuthorityEntry, ...]
    truncated: bool
    supervisor_fence_count: int
    latest_supervisor_fenced_at: str
    latest_supervisor_job_id: str


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
    assessed_at = normalize_worker_timestamp(
        now or datetime.now(UTC).isoformat(),
        field="now",
    )

    registry_kind = _file_kind(registry_path)
    if registry_kind == "absent":
        return WorkerAuthoritySnapshot("absent", "not_needed", 0, (), False, 0, "", "")
    if registry_kind != "file":
        raise WorkerAuthorityHealthError("registry_wrong_type", "Worker registry 路径不是文件。")

    try:
        with closing(_open_read_only(registry_path)) as db:
            db.execute("BEGIN")
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
            capacities = {
                item.contract.worker_id: _read_capacity(
                    db,
                    contract=item.contract,
                    assessed_at=assessed_at,
                )
                for item in registrations
            }
            queues = {
                item.contract.worker_id: _read_queue_backlog(
                    db,
                    contract=item.contract,
                    assessed_at=assessed_at,
                )
                for item in registrations
            }
            supervisor_fence_count, latest_supervisor_fenced_at, latest_supervisor_job_id = (
                _read_supervisor_fences(db)
            )
    except WorkerAuthorityHealthError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise WorkerAuthorityHealthError(
            "registry_unreadable", "Worker registry 无法可信读取。"
        ) from exc

    if not registrations:
        return WorkerAuthoritySnapshot(
            "ready",
            "not_needed",
            active_count,
            (),
            False,
            supervisor_fence_count,
            latest_supervisor_fenced_at,
            latest_supervisor_job_id,
        )

    heartbeat_store_health, heartbeats = _read_heartbeats(
        harness_path,
        workspace=workspace,
        worker_ids=tuple(item.contract.worker_id for item in registrations),
    )
    workers = tuple(
        _entry(
            registration.contract,
            capacity=capacities[registration.contract.worker_id],
            queue=queues[registration.contract.worker_id],
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
        supervisor_fence_count,
        latest_supervisor_fenced_at,
        latest_supervisor_job_id,
    )


def _read_supervisor_fences(db: sqlite3.Connection) -> tuple[int, str, str]:
    count = int(
        db.execute("SELECT COUNT(*) FROM worker_supervisor_fence_receipts").fetchone()[0]
    )
    if count == 0:
        return 0, "", ""
    row = db.execute(
        """
        SELECT * FROM worker_supervisor_fence_receipts
        ORDER BY decided_at DESC, operation_id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise ValueError("Supervisor fencing count 与历史不一致。")
    receipt = supervisor_fence_receipt_from_json(str(row["receipt_json"]))
    if (
        receipt.evidence.operation_id != str(row["operation_id"])
        or receipt.evidence.worker_id != str(row["worker_id"])
        or receipt.evidence.worker_epoch != int(row["worker_epoch"])
        or receipt.receipt_sha256 != str(row["receipt_sha256"])
        or receipt.authentication_sha256 != str(row["authentication_sha256"])
    ):
        raise ValueError("Supervisor fencing 索引列与 receipt 不一致。")
    return count, receipt.evidence.decided_at, receipt.evidence.job_id


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


def _read_capacity(
    db: sqlite3.Connection,
    *,
    contract: WorkerContract,
    assessed_at: str,
) -> tuple[int, int]:
    maximum = contract.resources.max_concurrent_jobs
    rows = db.execute(
        """
        SELECT * FROM worker_capacity_reservations
        WHERE worker_id = ? AND epoch = ? AND state = 'active'
        ORDER BY reservation_id ASC LIMIT ?
        """,
        (contract.worker_id, contract.epoch, maximum + 1),
    ).fetchall()
    if len(rows) > maximum:
        raise ValueError("Worker active capacity reservation 超过合同上限。")
    reservations = tuple(
        deserialize_worker_capacity_reservation(dict(row)) for row in rows
    )
    if any(
        item.worker_id != contract.worker_id
        or item.instance_id != contract.instance_id
        or item.epoch != contract.epoch
        or item.state is not WorkerCapacityReservationState.ACTIVE
        for item in reservations
    ):
        raise ValueError("Worker capacity reservation 与 active incarnation 不一致。")
    assessed = datetime.fromisoformat(assessed_at)
    reserved = sum(
        datetime.fromisoformat(item.expires_at) > assessed for item in reservations
    )
    return reserved, maximum - reserved


def _read_queue_backlog(
    db: sqlite3.Connection,
    *,
    contract: WorkerContract,
    assessed_at: str,
) -> tuple[int | None, int, int, int, int, float | None]:
    policy_row = db.execute(
        """
        SELECT * FROM worker_capacity_queue_policies
        WHERE worker_id = ? AND epoch = ?
        """,
        (contract.worker_id, contract.epoch),
    ).fetchone()
    if policy_row is None:
        unexpected = int(
            db.execute(
                """
                SELECT COUNT(*) FROM worker_capacity_waiters
                WHERE worker_id = ? AND epoch = ?
                  AND state IN ('waiting', 'claimed')
                """,
                (contract.worker_id, contract.epoch),
            ).fetchone()[0]
        )
        if unexpected:
            raise ValueError("Worker capacity queue 缺少 durable policy。")
        return None, 0, 0, 0, 0, None
    if (
        str(policy_row["instance_id"]) != contract.instance_id
        or int(policy_row["epoch"]) != contract.epoch
    ):
        raise ValueError("Worker capacity queue policy 与 active incarnation 不一致。")
    max_waiters = int(policy_row["max_waiters"])
    if not 0 <= max_waiters <= 10_000:
        raise ValueError("Worker capacity queue policy 上限无效。")
    configured_at = normalize_worker_timestamp(
        str(policy_row["configured_at"]),
        field="queue_configured_at",
    )
    assessed = datetime.fromisoformat(assessed_at)
    if datetime.fromisoformat(configured_at) > assessed:
        raise ValueError("Worker capacity queue policy configured_at 晚于诊断时间。")

    waiting_rows = db.execute(
        """
        SELECT * FROM worker_capacity_waiters
        WHERE worker_id = ? AND epoch = ? AND state = 'waiting'
        ORDER BY enqueued_at, queue_id
        LIMIT ?
        """,
        (contract.worker_id, contract.epoch, max_waiters + 1),
    ).fetchall()
    if len(waiting_rows) > max_waiters:
        raise ValueError("Worker capacity waiting 数量超过 durable policy。")
    waiting = tuple(
        deserialize_worker_capacity_waiter(dict(row)) for row in waiting_rows
    )
    if any(
        item.worker_id != contract.worker_id
        or item.instance_id != contract.instance_id
        or item.epoch != contract.epoch
        or item.state is not WorkerCapacityWaiterState.WAITING
        for item in waiting
    ):
        raise ValueError("Worker capacity waiter 与 active incarnation 不一致。")
    live_waiting = tuple(
        item
        for item in waiting
        if datetime.fromisoformat(item.deadline_at) > assessed
    )
    expired_waiting = len(waiting) - len(live_waiting)
    if any(datetime.fromisoformat(item.enqueued_at) > assessed for item in live_waiting):
        raise ValueError("Worker capacity waiter enqueued_at 晚于诊断时间。")
    oldest_wait_seconds = (
        max(
            0.0,
            (
                assessed
                - min(datetime.fromisoformat(item.enqueued_at) for item in live_waiting)
            ).total_seconds(),
        )
        if live_waiting
        else None
    )

    broken_claim_links = int(
        db.execute(
            """
            SELECT COUNT(*)
            FROM worker_capacity_waiters AS w
            LEFT JOIN worker_capacity_reservations AS r
              ON r.reservation_id = w.reservation_id
            WHERE w.worker_id = ? AND w.epoch = ? AND w.state = 'claimed'
              AND (
                r.reservation_id IS NULL
                OR r.worker_id != w.worker_id
                OR r.instance_id != w.instance_id
                OR r.epoch != w.epoch
                OR r.job_id != w.job_id
              )
            """,
            (contract.worker_id, contract.epoch),
        ).fetchone()[0]
    )
    if broken_claim_links:
        raise ValueError("Worker capacity claimed waiter 关联已损坏。")
    orphan_scheduler_reservations = int(
        db.execute(
            """
            SELECT COUNT(*)
            FROM worker_capacity_reservations AS r
            LEFT JOIN worker_capacity_waiters AS w
              ON w.reservation_id = r.reservation_id
            WHERE r.worker_id = ? AND r.epoch = ? AND r.state = 'active'
              AND r.reservation_id GLOB 'scheduler:*'
              AND (
                w.queue_id IS NULL
                OR w.state != 'claimed'
                OR w.worker_id != r.worker_id
                OR w.instance_id != r.instance_id
                OR w.epoch != r.epoch
                OR w.job_id != r.job_id
              )
            """,
            (contract.worker_id, contract.epoch),
        ).fetchone()[0]
    )
    if orphan_scheduler_reservations:
        raise ValueError("Worker scheduler reservation 缺少 claimed waiter。")

    claim_rows = db.execute(
        """
        SELECT
            w.queue_id, w.worker_id, w.instance_id, w.epoch, w.job_id,
            w.workspace_sha256, w.state, w.enqueued_at, w.deadline_at,
            w.reservation_id, w.terminal_at, w.reason_code,
            r.reservation_id AS r_reservation_id,
            r.worker_id AS r_worker_id, r.instance_id AS r_instance_id,
            r.epoch AS r_epoch, r.job_id AS r_job_id, r.state AS r_state,
            r.reserved_at AS r_reserved_at, r.expires_at AS r_expires_at,
            r.terminal_at AS r_terminal_at, r.reason_code AS r_reason_code
        FROM worker_capacity_waiters AS w
        JOIN worker_capacity_reservations AS r
          ON r.reservation_id = w.reservation_id
        WHERE w.worker_id = ? AND w.epoch = ? AND w.state = 'claimed'
          AND r.state = 'active'
        ORDER BY w.terminal_at, w.queue_id
        LIMIT ?
        """,
        (
            contract.worker_id,
            contract.epoch,
            contract.resources.max_concurrent_jobs + 1,
        ),
    ).fetchall()
    if len(claim_rows) > contract.resources.max_concurrent_jobs:
        raise ValueError("Worker active capacity claim 超过合同上限。")
    active_claims = 0
    expired_claims = 0
    for row in claim_rows:
        record = dict(row)
        waiter = deserialize_worker_capacity_waiter(
            {
                key: record[key]
                for key in (
                    "queue_id",
                    "worker_id",
                    "instance_id",
                    "epoch",
                    "job_id",
                    "workspace_sha256",
                    "state",
                    "enqueued_at",
                    "deadline_at",
                    "reservation_id",
                    "terminal_at",
                    "reason_code",
                )
            }
        )
        reservation = deserialize_worker_capacity_reservation(
            {
                "reservation_id": record["r_reservation_id"],
                "worker_id": record["r_worker_id"],
                "instance_id": record["r_instance_id"],
                "epoch": record["r_epoch"],
                "job_id": record["r_job_id"],
                "state": record["r_state"],
                "reserved_at": record["r_reserved_at"],
                "expires_at": record["r_expires_at"],
                "terminal_at": record["r_terminal_at"],
                "reason_code": record["r_reason_code"],
            }
        )
        if (
            waiter.worker_id != contract.worker_id
            or waiter.instance_id != contract.instance_id
            or waiter.epoch != contract.epoch
            or waiter.job_id != reservation.job_id
            or waiter.reservation_id != reservation.reservation_id
            or reservation.worker_id != contract.worker_id
            or reservation.instance_id != contract.instance_id
            or reservation.epoch != contract.epoch
            or reservation.state is not WorkerCapacityReservationState.ACTIVE
        ):
            raise ValueError("Worker capacity claim 与 active incarnation 不一致。")
        if datetime.fromisoformat(reservation.expires_at) > assessed:
            active_claims += 1
        else:
            expired_claims += 1
    return (
        max_waiters,
        len(live_waiting),
        active_claims,
        expired_waiting,
        expired_claims,
        oldest_wait_seconds,
    )


def _entry(
    contract: WorkerContract,
    *,
    capacity: tuple[int, int],
    queue: tuple[int | None, int, int, int, int, float | None],
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
        capabilities=tuple(item.value for item in contract.capabilities),
        dispatch_ready=(
            contract.kind.value != "agent"
            or "agent_context_scope" in {
                item.value for item in contract.capabilities
            }
        ),
        max_concurrent_jobs=contract.resources.max_concurrent_jobs,
        reserved_jobs=capacity[0],
        available_jobs=capacity[1],
        queue_max_waiters=queue[0],
        waiting_jobs=queue[1],
        active_claims=queue[2],
        expired_waiting_jobs=queue[3],
        expired_claims=queue[4],
        oldest_wait_seconds=queue[5],
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
