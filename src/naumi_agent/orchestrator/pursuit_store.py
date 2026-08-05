"""SQLite persistence for pursuit run state."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from naumi_agent.orchestrator.pursuit import (
    PursuitBackgroundWait,
    PursuitEvidence,
    PursuitRun,
    PursuitRunStatus,
)
from naumi_agent.orchestrator.pursuit_action_ledger import (
    PursuitActionRecord,
    PursuitActionState,
    action_safe_text,
    digest_result,
)
from naumi_agent.orchestrator.pursuit_checkpoint import PursuitCheckpoint
from naumi_agent.orchestrator.pursuit_recovery_attempt import (
    PursuitRecoveryAttempt,
    PursuitRecoveryAttemptState,
)
from naumi_agent.orchestrator.pursuit_recovery_reconcile import (
    PursuitRecoveryReconcileError,
    PursuitRecoveryReconciliationReceipt,
    new_pursuit_reconciliation_receipt,
)
from naumi_agent.orchestrator.pursuit_terminal import PursuitBoundaryDecision
from naumi_agent.orchestrator.pursuit_terminal_outbox import (
    PursuitTerminalDispatchState,
    PursuitTerminalOutboxDispatch,
    PursuitTerminalOutboxRecord,
    PursuitTerminalOutboxRunReceipt,
    PursuitTerminalOutboxState,
    pursuit_terminal_outbox_id,
)


@dataclass(frozen=True, slots=True)
class PursuitTerminalOutboxClaim:
    outbox: PursuitTerminalOutboxRecord
    dispatch: PursuitTerminalOutboxDispatch


@dataclass(frozen=True, slots=True)
class PursuitTerminalOutboxBacklog:
    total_pending: int
    due: int
    backoff: int
    live_claimed: int
    expired_claimed: int
    assessed_at: float


class PursuitStoreError(RuntimeError):
    """Raised when durable Pursuit state is invalid or unavailable."""


class PursuitStoreConflictError(PursuitStoreError):
    """Raised when a checkpoint sequence would overwrite different history."""


class PursuitStore:
    """Durable store for pursuit runs, evidence, and async waits."""

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._db_path = self._base_dir / "pursuit.db"
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    def save_run(self, run: PursuitRun) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pursuit_runs (
                    id, goal, status, phase, started_at, updated_at, iteration,
                    criteria_total, criteria_verified, failure_count,
                    blocked_reason, next_action, worktree_name, worktree_path,
                    boundary_decision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    goal=excluded.goal,
                    status=excluded.status,
                    phase=excluded.phase,
                    updated_at=excluded.updated_at,
                    iteration=excluded.iteration,
                    criteria_total=excluded.criteria_total,
                    criteria_verified=excluded.criteria_verified,
                    failure_count=excluded.failure_count,
                    blocked_reason=excluded.blocked_reason,
                    next_action=excluded.next_action,
                    worktree_name=excluded.worktree_name,
                    worktree_path=excluded.worktree_path,
                    boundary_decision_id=excluded.boundary_decision_id
                """,
                (
                    run.id,
                    run.goal,
                    run.status.value,
                    run.phase,
                    run.started_at,
                    run.updated_at,
                    run.iteration,
                    run.criteria_total,
                    run.criteria_verified,
                    run.failure_count,
                    run.blocked_reason,
                    run.next_action,
                    run.worktree_name,
                    run.worktree_path,
                    (
                        run.boundary_decision.decision_id
                        if run.boundary_decision is not None
                        else ""
                    ),
                ),
            )
            conn.execute("DELETE FROM pursuit_evidence WHERE run_id = ?", (run.id,))
            for index, evidence in enumerate(run.evidence or []):
                conn.execute(
                    """
                    INSERT INTO pursuit_evidence (
                        run_id, seq, kind, source, summary, is_hard, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        index,
                        evidence.kind,
                        evidence.source,
                        evidence.summary,
                        int(evidence.is_hard),
                        evidence.timestamp,
                    ),
                )
            conn.execute("DELETE FROM pursuit_waits WHERE run_id = ?", (run.id,))
            for wait in run.waiting_on or []:
                conn.execute(
                    """
                    INSERT INTO pursuit_waits (
                        run_id, task_id, action_id, command, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        wait.task_id,
                        wait.action_id,
                        wait.command,
                        wait.created_at,
                    ),
                )
            if run.boundary_decision is not None:
                decision = run.boundary_decision
                payload = decision.model_dump_json()
                payload_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO pursuit_boundary_decisions (
                        run_id, decision_id, payload_json, payload_sha256,
                        recorded_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        decision.decision_id,
                        payload,
                        payload_digest,
                        run.updated_at,
                    ),
                )
                stored = conn.execute(
                    """
                    SELECT *
                    FROM pursuit_boundary_decisions
                    WHERE run_id = ? AND decision_id = ?
                    """,
                    (run.id, decision.decision_id),
                ).fetchone()
                if stored is None:
                    raise PursuitStoreConflictError(
                        "boundary decision 写入后缺少持久记录。"
                    )
                if (
                    not hmac.compare_digest(
                        str(stored["payload_sha256"]),
                        payload_digest,
                    )
                    and _boundary_decision_from_row(stored) != decision
                ):
                    raise PursuitStoreConflictError(
                        "相同 boundary decision identity 对应不同 payload。"
                    )

    def get_run(self, run_id: str) -> PursuitRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pursuit_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            evidence_rows = conn.execute(
                """
                SELECT * FROM pursuit_evidence
                WHERE run_id = ?
                ORDER BY seq ASC
                """,
                (run_id,),
            ).fetchall()
            wait_rows = conn.execute(
                """
                SELECT * FROM pursuit_waits
                WHERE run_id = ?
                ORDER BY created_at ASC, task_id ASC
                """,
                (run_id,),
            ).fetchall()
            boundary_id = str(row["boundary_decision_id"])
            boundary_row = (
                conn.execute(
                    """
                    SELECT * FROM pursuit_boundary_decisions
                    WHERE run_id = ? AND decision_id = ?
                    """,
                    (run_id, boundary_id),
                ).fetchone()
                if boundary_id
                else None
            )
            if boundary_id and boundary_row is None:
                raise PursuitStoreError(
                    "PursuitRun 当前 boundary decision 指针缺少对应记录。"
                )
        return _run_from_rows(row, evidence_rows, wait_rows, boundary_row)

    def list_boundary_decisions(
        self,
        run_id: str,
        *,
        limit: int = 50,
    ) -> list[PursuitBoundaryDecision]:
        """Return bounded, content-verified boundary decision history."""
        safe_limit = max(1, min(int(limit), 200))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM pursuit_boundary_decisions
                WHERE run_id = ?
                ORDER BY recorded_at DESC, decision_id DESC
                LIMIT ?
                """,
                (run_id, safe_limit),
            ).fetchall()
        return [_boundary_decision_from_row(row) for row in rows]

    def prepare_recovery_attempt(
        self,
        attempt: PursuitRecoveryAttempt,
    ) -> tuple[PursuitRecoveryAttempt, bool]:
        """Persist one idempotent recovery request before lease acquisition."""
        if (
            attempt.state is not PursuitRecoveryAttemptState.REQUESTED
            or attempt.sequence != 1
        ):
            raise ValueError("新 recovery attempt 必须从 requested/sequence=1 开始。")
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = self._get_recovery_attempt_with_connection(
                    conn,
                    attempt.attempt_id,
                )
                if existing is not None:
                    immutable = (
                        "run_id",
                        "source_request_sha256",
                    )
                    if all(
                        getattr(existing, field) == getattr(attempt, field)
                        for field in immutable
                    ):
                        return existing, False
                    raise PursuitStoreConflictError(
                        "recovery attempt identity 已绑定不同请求事实。"
                    )
                if conn.execute(
                    "SELECT 1 FROM pursuit_runs WHERE id = ?",
                    (attempt.run_id,),
                ).fetchone() is None:
                    raise PursuitStoreConflictError(
                        f"recovery attempt 对应的 PursuitRun 不存在：{attempt.run_id}"
                    )
                conn.execute(
                    """
                    INSERT INTO pursuit_recovery_attempts (
                        attempt_id, run_id, latest_sequence, state,
                        payload_json, payload_sha256, requested_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.attempt_id,
                        attempt.run_id,
                        attempt.sequence,
                        attempt.state.value,
                        attempt.canonical_json(),
                        attempt.digest(),
                        attempt.requested_at,
                        attempt.updated_at,
                    ),
                )
                self._append_recovery_attempt_event(
                    conn,
                    attempt,
                    previous_digest="",
                )
                return attempt, True
        except PursuitStoreError:
            raise
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"准备 recovery attempt 失败：{exc}") from exc

    def mark_recovery_attempt_admitted(
        self,
        attempt_id: str,
        *,
        admitted_at: float,
        lease_epoch: int,
        checkpoint_id: str,
    ) -> PursuitRecoveryAttempt:
        return self._transition_recovery_attempt(
            attempt_id,
            target=PursuitRecoveryAttemptState.ADMITTED,
            updated_at=admitted_at,
            admitted_at=admitted_at,
            lease_epoch=lease_epoch,
            checkpoint_id=checkpoint_id,
        )

    def resolve_recovery_attempt(
        self,
        attempt_id: str,
        *,
        resolved_at: float,
        result_code: str,
        boundary_decision_id: str = "",
    ) -> PursuitRecoveryAttempt:
        return self._transition_recovery_attempt(
            attempt_id,
            target=PursuitRecoveryAttemptState.RESOLVED,
            updated_at=resolved_at,
            resolved_at=resolved_at,
            result_code=result_code,
            boundary_decision_id=boundary_decision_id,
        )

    def fail_recovery_attempt(
        self,
        attempt_id: str,
        *,
        failed_at: float,
        result_code: str,
    ) -> PursuitRecoveryAttempt:
        return self._transition_recovery_attempt(
            attempt_id,
            target=PursuitRecoveryAttemptState.FAILED,
            updated_at=failed_at,
            resolved_at=failed_at,
            result_code=result_code,
        )

    def get_recovery_attempt(
        self,
        attempt_id: str,
    ) -> PursuitRecoveryAttempt | None:
        if not self._db_path.exists():
            return None
        try:
            with self._connect() as conn:
                return self._get_recovery_attempt_with_connection(conn, attempt_id)
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(
                f"recovery attempt 结构校验失败：{exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"读取 recovery attempt 失败：{exc}") from exc

    def list_recovery_attempts(
        self,
        run_id: str,
        *,
        limit: int = 20,
    ) -> list[PursuitRecoveryAttempt]:
        safe_limit = max(1, min(int(limit), 200))
        if not self._db_path.exists():
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT attempt_id
                    FROM pursuit_recovery_attempts
                    WHERE run_id = ?
                    ORDER BY requested_at DESC, attempt_id DESC
                    LIMIT ?
                    """,
                    (run_id, safe_limit),
                ).fetchall()
                return [
                    attempt
                    for row in rows
                    if (
                        attempt := self._get_recovery_attempt_with_connection(
                            conn,
                            str(row["attempt_id"]),
                        )
                    )
                    is not None
                ]
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(
                f"recovery attempt 列表校验失败：{exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"读取 recovery attempt 列表失败：{exc}") from exc

    def get_recovery_reconciliation(
        self,
        attempt_id: str,
    ) -> PursuitRecoveryReconciliationReceipt | None:
        """Read and authenticate one immutable recovery reconciliation receipt."""
        if not self._db_path.exists():
            return None
        try:
            with self._connect() as conn:
                return self._get_recovery_reconciliation_with_connection(
                    conn,
                    attempt_id,
                )
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(
                f"恢复对账回执结构校验失败：{exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"读取恢复对账回执失败：{exc}") from exc

    def reconcile_admitted_recovery_attempt(
        self,
        attempt_id: str,
        *,
        reconciled_at: float,
        minimum_admitted_age_seconds: float,
        fence_epoch: int,
        fence_operation_id: str,
    ) -> PursuitRecoveryReconciliationReceipt:
        """Atomically close an admitted attempt from post-admission terminal facts."""
        if (
            not math.isfinite(reconciled_at)
            or not math.isfinite(minimum_admitted_age_seconds)
            or not 1 <= minimum_admitted_age_seconds <= 86_400
            or isinstance(fence_epoch, bool)
            or not isinstance(fence_epoch, int)
            or fence_epoch <= 0
        ):
            raise ValueError("恢复请求对账时间、宽限期或 fence epoch 无效。")
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = self._get_recovery_reconciliation_with_connection(
                    conn,
                    attempt_id,
                )
                if existing is not None:
                    return existing
                current = self._get_recovery_attempt_with_connection(
                    conn,
                    attempt_id,
                )
                if current is None:
                    raise PursuitRecoveryReconcileError(
                        "attempt_not_found",
                        "没有找到该恢复请求。",
                    )
                if current.state in {
                    PursuitRecoveryAttemptState.RESOLVED,
                    PursuitRecoveryAttemptState.FAILED,
                }:
                    raise PursuitRecoveryReconcileError(
                        "already_terminal",
                        "该恢复请求已经处于终态，不需要对账。",
                    )
                if current.state is not PursuitRecoveryAttemptState.ADMITTED:
                    raise PursuitRecoveryReconcileError(
                        "request_not_admitted",
                        "该恢复请求尚未取得执行准入，不能按已执行结果收口。",
                    )
                if current.lease_epoch <= 0:
                    raise PursuitRecoveryReconcileError(
                        "embedded_authority_unsupported",
                        "该请求没有持久 RunLease epoch，需要人工审查。",
                    )
                if fence_epoch <= current.lease_epoch:
                    raise PursuitRecoveryReconcileError(
                        "fence_not_advanced",
                        "Fencing epoch 没有推进，拒绝收口恢复请求。",
                    )
                if reconciled_at < (
                    current.admitted_at + minimum_admitted_age_seconds
                ):
                    raise PursuitRecoveryReconcileError(
                        "grace_period_active",
                        "恢复请求仍处于安全宽限期，拒绝收口。",
                    )

                run_row = conn.execute(
                    "SELECT * FROM pursuit_runs WHERE id = ?",
                    (current.run_id,),
                ).fetchone()
                if run_row is None:
                    raise PursuitRecoveryReconcileError(
                        "run_missing",
                        "恢复请求对应的 PursuitRun 不存在。",
                    )
                boundary_id = str(run_row["boundary_decision_id"])
                if not boundary_id:
                    raise PursuitRecoveryReconcileError(
                        "terminal_evidence_missing",
                        "PursuitRun 没有可验证的后置机械裁判。",
                    )
                boundary_row = conn.execute(
                    """
                    SELECT * FROM pursuit_boundary_decisions
                    WHERE run_id = ? AND decision_id = ?
                    """,
                    (current.run_id, boundary_id),
                ).fetchone()
                if boundary_row is None:
                    raise PursuitRecoveryReconcileError(
                        "terminal_evidence_missing",
                        "PursuitRun 的机械裁判指针缺少对应记录。",
                    )
                boundary = _boundary_decision_from_row(boundary_row)
                boundary_recorded_at = float(boundary_row["recorded_at"])
                if boundary_recorded_at <= current.admitted_at:
                    raise PursuitRecoveryReconcileError(
                        "terminal_evidence_not_post_admission",
                        "机械裁判并非恢复准入后的新事实。",
                    )
                if boundary.status == "running":
                    raise PursuitRecoveryReconcileError(
                        "terminal_evidence_incomplete",
                        "恢复后的机械裁判仍为 running，不能收口。",
                    )

                checkpoint = self._get_checkpoint_with_connection(
                    conn,
                    current.run_id,
                )
                if checkpoint is None:
                    raise PursuitRecoveryReconcileError(
                        "checkpoint_missing",
                        "没有找到恢复准入后的 checkpoint。",
                    )
                checkpoint_id = checkpoint.checkpoint_id()
                if (
                    checkpoint.created_at <= current.admitted_at
                    or checkpoint_id == current.checkpoint_id
                ):
                    raise PursuitRecoveryReconcileError(
                        "checkpoint_not_post_admission",
                        "最新 checkpoint 不是恢复准入后的新事实。",
                    )
                if (
                    boundary_recorded_at > reconciled_at
                    or checkpoint.created_at > reconciled_at
                    or float(run_row["updated_at"]) > reconciled_at
                ):
                    raise PursuitRecoveryReconcileError(
                        "terminal_evidence_from_future",
                        "终态证据时间晚于本次对账，可能存在时钟回退。",
                    )

                expected_statuses = {
                    "waiting": PursuitRunStatus.WAITING.value,
                    "blocked": PursuitRunStatus.BLOCKED.value,
                    "completed": PursuitRunStatus.COMPLETED.value,
                    "cancelled": PursuitRunStatus.CANCELLED.value,
                    "budget_exceeded": PursuitRunStatus.BUDGET_EXCEEDED.value,
                }
                expected = expected_statuses.get(boundary.status)
                if expected is None:
                    raise PursuitRecoveryReconcileError(
                        "terminal_status_unsupported",
                        "该机械裁判状态不能用于自动收口。",
                    )
                if (
                    str(run_row["status"]) != expected
                    or checkpoint.status != expected
                ):
                    raise PursuitRecoveryReconcileError(
                        "terminal_evidence_inconsistent",
                        "PursuitRun、checkpoint 与机械裁判状态不一致。",
                    )
                if (
                    float(run_row["updated_at"]) < boundary_recorded_at
                    or checkpoint.created_at < boundary_recorded_at
                ):
                    raise PursuitRecoveryReconcileError(
                        "terminal_evidence_inconsistent",
                        "PursuitRun 或 checkpoint 的终态写入顺序不一致。",
                    )

                resolved = current.model_copy(update={
                    "sequence": current.sequence + 1,
                    "state": PursuitRecoveryAttemptState.RESOLVED,
                    "updated_at": reconciled_at,
                    "resolved_at": reconciled_at,
                    "result_code": boundary.code,
                    "boundary_decision_id": boundary.decision_id,
                })
                resolved = PursuitRecoveryAttempt.model_validate(
                    resolved.model_dump(mode="json")
                )
                self._append_recovery_attempt_event(
                    conn,
                    resolved,
                    previous_digest=current.digest(),
                )
                cursor = conn.execute(
                    """
                    UPDATE pursuit_recovery_attempts
                    SET latest_sequence = ?, state = ?, payload_json = ?,
                        payload_sha256 = ?, updated_at = ?
                    WHERE attempt_id = ? AND latest_sequence = ? AND state = ?
                    """,
                    (
                        resolved.sequence,
                        resolved.state.value,
                        resolved.canonical_json(),
                        resolved.digest(),
                        resolved.updated_at,
                        attempt_id,
                        current.sequence,
                        current.state.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PursuitStoreConflictError(
                        "recovery attempt 被并发更新，拒绝对账覆盖。"
                    )
                receipt = new_pursuit_reconciliation_receipt(
                    attempt_id=current.attempt_id,
                    run_id=current.run_id,
                    attempt_before_sha256=current.digest(),
                    attempt_after_sha256=resolved.digest(),
                    admitted_at=current.admitted_at,
                    reconciled_at=reconciled_at,
                    admitted_lease_epoch=current.lease_epoch,
                    fence_epoch=fence_epoch,
                    fence_operation_id=fence_operation_id,
                    checkpoint_id=checkpoint_id,
                    checkpoint_created_at=checkpoint.created_at,
                    boundary_decision_id=boundary.decision_id,
                    boundary_recorded_at=boundary_recorded_at,
                    result_code=boundary.code,
                )
                conn.execute(
                    """
                    INSERT INTO pursuit_recovery_reconciliations (
                        attempt_id, receipt_id, payload_json, payload_sha256,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.attempt_id,
                        receipt.receipt_id,
                        receipt.canonical_json(),
                        receipt.digest(),
                        receipt.reconciled_at,
                    ),
                )
                self._deliver_terminal_outbox_with_connection(
                    conn,
                    attempt=resolved,
                    delivered_at=reconciled_at,
                )
                return receipt
        except PursuitRecoveryReconcileError:
            raise
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(f"恢复请求对账失败：{exc}") from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"恢复请求对账失败：{exc}") from exc

    def list_runs(self, *, include_finished: bool = True) -> list[PursuitRun]:
        query = "SELECT * FROM pursuit_runs"
        params: tuple[str, ...] = ()
        if not include_finished:
            query += " WHERE status IN (?, ?)"
            params = (PursuitRunStatus.RUNNING.value, PursuitRunStatus.WAITING.value)
        query += " ORDER BY updated_at DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        runs: list[PursuitRun] = []
        for row in rows:
            run = self.get_run(row["id"])
            if run is not None:
                runs.append(run)
        return runs

    def save_checkpoint(self, checkpoint: PursuitCheckpoint) -> None:
        """Persist the latest monotonic checkpoint without rewriting history."""
        payload = checkpoint.canonical_json()
        digest = checkpoint.digest()
        checkpoint_id = checkpoint.checkpoint_id()
        try:
            with self._connect() as conn:
                # Serialize the read/compare/write sequence across processes.
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                    "SELECT 1 FROM pursuit_runs WHERE id = ?",
                    (checkpoint.run_id,),
                ).fetchone() is None:
                    raise PursuitStoreConflictError(
                        f"checkpoint 对应的 PursuitRun 不存在：{checkpoint.run_id}"
                    )
                current = conn.execute(
                    "SELECT sequence, payload_sha256 FROM pursuit_checkpoints "
                    "WHERE run_id = ?",
                    (checkpoint.run_id,),
                ).fetchone()
                if current is not None:
                    current_sequence = int(current["sequence"])
                    if checkpoint.sequence < current_sequence:
                        raise PursuitStoreConflictError(
                            "checkpoint 序号倒退："
                            f"{checkpoint.sequence} < {current_sequence}"
                        )
                    if checkpoint.sequence == current_sequence:
                        if hmac.compare_digest(current["payload_sha256"], digest):
                            self._enqueue_terminal_outbox_with_connection(
                                conn,
                                checkpoint=checkpoint,
                            )
                            return
                        raise PursuitStoreConflictError(
                            f"checkpoint 序号 {checkpoint.sequence} 已绑定不同内容。"
                        )
                conn.execute(
                    """
                    INSERT INTO pursuit_checkpoints (
                        run_id, sequence, schema_version, checkpoint_id,
                        payload_json, payload_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        sequence=excluded.sequence,
                        schema_version=excluded.schema_version,
                        checkpoint_id=excluded.checkpoint_id,
                        payload_json=excluded.payload_json,
                        payload_sha256=excluded.payload_sha256,
                        created_at=excluded.created_at
                    """,
                    (
                        checkpoint.run_id,
                        checkpoint.sequence,
                        checkpoint.schema_version,
                        checkpoint_id,
                        payload,
                        digest,
                        checkpoint.created_at,
                    ),
                )
                self._enqueue_terminal_outbox_with_connection(
                    conn,
                    checkpoint=checkpoint,
                )
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(
                f"保存 checkpoint 的 terminal outbox 失败：{exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"保存 checkpoint 失败：{exc}") from exc

    def get_checkpoint(self, run_id: str) -> PursuitCheckpoint | None:
        """Read and authenticate the latest checkpoint; reject corrupted state."""
        if not self._db_path.exists():
            return None
        try:
            with self._connect() as conn:
                return self._get_checkpoint_with_connection(conn, run_id)
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(f"checkpoint 结构校验失败：{exc}") from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"读取 checkpoint 失败：{exc}") from exc

    def get_terminal_outbox(
        self,
        outbox_id: str,
    ) -> PursuitTerminalOutboxRecord | None:
        """Read one authenticated terminal outbox record."""
        if not self._db_path.exists():
            return None
        try:
            with self._connect() as conn:
                return self._get_terminal_outbox_with_connection(conn, outbox_id)
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(
                f"terminal outbox 结构校验失败：{exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"读取 terminal outbox 失败：{exc}") from exc

    def list_pending_terminal_outbox(
        self,
        *,
        limit: int = 100,
    ) -> list[PursuitTerminalOutboxRecord]:
        """Return an authenticated, oldest-first and strictly bounded backlog."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("terminal outbox limit 必须在 1..1000。")
        if not self._db_path.exists():
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT outbox_id
                    FROM pursuit_terminal_outbox
                    WHERE state = 'pending'
                    ORDER BY created_at ASC, outbox_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                records = [
                    self._get_terminal_outbox_with_connection(
                        conn,
                        str(row["outbox_id"]),
                    )
                    for row in rows
                ]
            return [record for record in records if record is not None]
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(
                f"terminal outbox 恢复目录校验失败：{exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(
                f"读取 terminal outbox 恢复目录失败：{exc}"
            ) from exc

    def get_terminal_outbox_dispatch(
        self,
        outbox_id: str,
    ) -> PursuitTerminalOutboxDispatch | None:
        if not self._db_path.exists():
            return None
        try:
            with self._connect() as conn:
                return self._get_terminal_dispatch_with_connection(conn, outbox_id)
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(
                f"terminal outbox dispatch 校验失败：{exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(
                f"读取 terminal outbox dispatch 失败：{exc}"
            ) from exc

    def claim_next_terminal_outbox(
        self,
        *,
        owner_id: str,
        now: float,
        lease_seconds: int = 30,
        scan_limit: int = 100,
    ) -> PursuitTerminalOutboxClaim | None:
        owner_sha256 = _terminal_dispatch_owner_sha256(owner_id)
        if (
            not math.isfinite(now)
            or now <= 0
            or isinstance(lease_seconds, bool)
            or not 3 <= lease_seconds <= 300
            or isinstance(scan_limit, bool)
            or not 1 <= scan_limit <= 1000
        ):
            raise ValueError("terminal dispatch claim 策略无效。")
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT o.outbox_id
                    FROM pursuit_terminal_outbox AS o
                    JOIN pursuit_terminal_outbox_dispatch AS d
                      ON d.outbox_id = o.outbox_id
                    WHERE o.state = 'pending'
                      AND (
                        (d.state = 'idle' AND d.next_attempt_at <= ?)
                        OR (d.state = 'claimed' AND d.claim_expires_at <= ?)
                      )
                    ORDER BY o.created_at ASC, o.outbox_id ASC
                    LIMIT ?
                    """,
                    (now, now, scan_limit),
                ).fetchall()
                if not rows:
                    return None
                outbox_id = str(rows[0]["outbox_id"])
                outbox = self._get_terminal_outbox_with_connection(conn, outbox_id)
                current = self._get_terminal_dispatch_with_connection(
                    conn,
                    outbox_id,
                )
                if outbox is None or current is None:
                    raise PursuitStoreError("terminal dispatch 候选权威缺失。")
                candidate = PursuitTerminalOutboxDispatch.model_validate(
                    current.model_copy(update={
                        "sequence": current.sequence + 1,
                        "state": PursuitTerminalDispatchState.CLAIMED,
                        "claim_owner_sha256": owner_sha256,
                        "claim_epoch": current.claim_epoch + 1,
                        "claim_expires_at": now + lease_seconds,
                        "attempt_count": current.attempt_count + 1,
                        "next_attempt_at": 0,
                        "updated_at": now,
                    }).model_dump(mode="json")
                )
                self._update_terminal_dispatch_with_connection(
                    conn,
                    current=current,
                    candidate=candidate,
                )
                return PursuitTerminalOutboxClaim(
                    outbox=outbox,
                    dispatch=candidate,
                )
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(f"认领 terminal outbox 失败：{exc}") from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"认领 terminal outbox 失败：{exc}") from exc

    def release_terminal_outbox_claim(
        self,
        outbox_id: str,
        *,
        owner_id: str,
        claim_epoch: int,
        now: float,
        retry_delay_seconds: float,
        failure_code: str,
    ) -> PursuitTerminalOutboxDispatch:
        owner_sha256 = _terminal_dispatch_owner_sha256(owner_id)
        normalized_code = str(failure_code or "").strip()
        if (
            not math.isfinite(now)
            or now <= 0
            or not math.isfinite(retry_delay_seconds)
            or not 1 <= retry_delay_seconds <= 3600
            or isinstance(claim_epoch, bool)
            or claim_epoch <= 0
        ):
            raise ValueError("terminal dispatch release 策略无效。")
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                outbox = self._get_terminal_outbox_with_connection(conn, outbox_id)
                current = self._get_terminal_dispatch_with_connection(
                    conn,
                    outbox_id,
                )
                if outbox is None or current is None:
                    raise PursuitStoreConflictError("terminal dispatch 不存在。")
                if outbox.state is PursuitTerminalOutboxState.DELIVERED:
                    if current.state is PursuitTerminalDispatchState.DELIVERED:
                        return current
                    raise PursuitStoreConflictError("delivered outbox 调度状态不一致。")
                if (
                    current.state is not PursuitTerminalDispatchState.CLAIMED
                    or current.claim_epoch != claim_epoch
                    or not hmac.compare_digest(
                        current.claim_owner_sha256,
                        owner_sha256,
                    )
                    or current.claim_expires_at <= now
                ):
                    raise PursuitStoreConflictError(
                        "terminal dispatch claim 已失效或不属于当前 owner。"
                    )
                candidate = PursuitTerminalOutboxDispatch.model_validate(
                    current.model_copy(update={
                        "sequence": current.sequence + 1,
                        "state": PursuitTerminalDispatchState.IDLE,
                        "claim_owner_sha256": "",
                        "claim_expires_at": 0,
                        "next_attempt_at": now + retry_delay_seconds,
                        "last_failure_code": normalized_code,
                        "updated_at": now,
                    }).model_dump(mode="json")
                )
                self._update_terminal_dispatch_with_connection(
                    conn,
                    current=current,
                    candidate=candidate,
                )
                return candidate
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(f"释放 terminal outbox claim 失败：{exc}") from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"释放 terminal outbox claim 失败：{exc}") from exc

    def terminal_outbox_backlog(
        self,
        *,
        now: float,
        scan_limit: int = 10_000,
    ) -> PursuitTerminalOutboxBacklog:
        if (
            not math.isfinite(now)
            or now <= 0
            or isinstance(scan_limit, bool)
            or not 1 <= scan_limit <= 10_000
        ):
            raise ValueError("terminal outbox backlog 策略无效。")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT o.outbox_id
                FROM pursuit_terminal_outbox AS o
                WHERE o.state = 'pending'
                ORDER BY o.created_at ASC, o.outbox_id ASC
                LIMIT ?
                """,
                (scan_limit + 1,),
            ).fetchall()
            if len(rows) > scan_limit:
                raise PursuitStoreError("terminal outbox backlog 超过有界扫描上限。")
            due = backoff = live_claimed = expired_claimed = 0
            for row in rows:
                outbox_id = str(row["outbox_id"])
                self._get_terminal_outbox_with_connection(conn, outbox_id)
                dispatch = self._get_terminal_dispatch_with_connection(
                    conn,
                    outbox_id,
                )
                if dispatch is None:
                    raise PursuitStoreError("pending outbox 缺少 dispatch。")
                if dispatch.state is PursuitTerminalDispatchState.IDLE:
                    if dispatch.next_attempt_at <= now:
                        due += 1
                    else:
                        backoff += 1
                elif dispatch.claim_expires_at <= now:
                    expired_claimed += 1
                else:
                    live_claimed += 1
        return PursuitTerminalOutboxBacklog(
            total_pending=len(rows),
            due=due,
            backoff=backoff,
            live_claimed=live_claimed,
            expired_claimed=expired_claimed,
            assessed_at=now,
        )

    def get_terminal_outbox_run_receipt(
        self,
        source_request_sha256: str,
    ) -> PursuitTerminalOutboxRunReceipt | None:
        """Read and authenticate an immutable explicit-run receipt."""
        normalized = str(source_request_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("terminal outbox run request digest 格式无效。")
        if not self._db_path.exists():
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT receipt_id, source_request_sha256, payload_json, "
                    "payload_sha256 "
                    "FROM pursuit_terminal_outbox_run_receipts "
                    "WHERE source_request_sha256 = ?",
                    (normalized,),
                ).fetchone()
            if row is None:
                return None
            receipt = PursuitTerminalOutboxRunReceipt.model_validate_json(
                str(row["payload_json"])
            )
            if (
                not hmac.compare_digest(receipt.receipt_id, str(row["receipt_id"]))
                or not hmac.compare_digest(
                    receipt.source_request_sha256,
                    str(row["source_request_sha256"]),
                )
                or not hmac.compare_digest(receipt.source_request_sha256, normalized)
                or not hmac.compare_digest(
                    receipt.receipt_sha256,
                    str(row["payload_sha256"]),
                )
            ):
                raise PursuitStoreError("terminal outbox run receipt 存储摘要不匹配。")
            return receipt
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(f"terminal outbox run receipt 校验失败：{exc}") from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"读取 terminal outbox run receipt 失败：{exc}") from exc

    def save_terminal_outbox_run_receipt(
        self,
        receipt: PursuitTerminalOutboxRunReceipt,
    ) -> tuple[PursuitTerminalOutboxRunReceipt, bool]:
        """Persist once; duplicate request ids return the first authenticated receipt."""
        validated = PursuitTerminalOutboxRunReceipt.model_validate(
            receipt.model_dump(mode="json")
        )
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT receipt_id, source_request_sha256, payload_json, "
                    "payload_sha256 "
                    "FROM pursuit_terminal_outbox_run_receipts "
                    "WHERE source_request_sha256 = ?",
                    (validated.source_request_sha256,),
                ).fetchone()
                if row is not None:
                    existing = PursuitTerminalOutboxRunReceipt.model_validate_json(
                        str(row["payload_json"])
                    )
                    if (
                        not hmac.compare_digest(
                            existing.receipt_id,
                            str(row["receipt_id"]),
                        )
                        or not hmac.compare_digest(
                            existing.source_request_sha256,
                            str(row["source_request_sha256"]),
                        )
                        or not hmac.compare_digest(
                            existing.source_request_sha256,
                            validated.source_request_sha256,
                        )
                        or not hmac.compare_digest(
                            existing.receipt_sha256,
                            str(row["payload_sha256"]),
                        )
                    ):
                        raise PursuitStoreError(
                            "terminal outbox run receipt 存储摘要不匹配。"
                        )
                    return existing, False
                conn.execute(
                    "INSERT INTO pursuit_terminal_outbox_run_receipts ("
                    "receipt_id, source_request_sha256, payload_json, "
                    "payload_sha256, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        validated.receipt_id,
                        validated.source_request_sha256,
                        validated.model_dump_json(),
                        validated.receipt_sha256,
                        validated.created_at,
                    ),
                )
            return validated, True
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(f"terminal outbox run receipt 校验失败：{exc}") from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"保存 terminal outbox run receipt 失败：{exc}") from exc

    @staticmethod
    def _get_checkpoint_with_connection(
        conn: sqlite3.Connection,
        run_id: str,
    ) -> PursuitCheckpoint | None:
        row = conn.execute(
            "SELECT * FROM pursuit_checkpoints WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        payload = str(row["payload_json"])
        expected_digest = str(row["payload_sha256"])
        actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected_digest, actual_digest):
            raise PursuitStoreError("checkpoint 内容摘要校验失败，拒绝恢复。")
        checkpoint = PursuitCheckpoint.model_validate_json(payload)
        if checkpoint.run_id != run_id:
            raise PursuitStoreError("checkpoint run_id 与存储键不一致。")
        if checkpoint.sequence != int(row["sequence"]):
            raise PursuitStoreError("checkpoint 序号与存储元数据不一致。")
        if checkpoint.schema_version != int(row["schema_version"]):
            raise PursuitStoreError("checkpoint schema 版本与存储元数据不一致。")
        if not hmac.compare_digest(
            checkpoint.checkpoint_id(), str(row["checkpoint_id"])
        ):
            raise PursuitStoreError("checkpoint ID 校验失败，拒绝恢复。")
        return checkpoint

    def prepare_action(self, record: PursuitActionRecord) -> PursuitActionRecord:
        """Persist one immutable action identity before any external dispatch."""
        if record.state is not PursuitActionState.PREPARED or record.sequence != 1:
            raise ValueError("新行动必须从 prepared/sequence=1 开始。")
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = self._get_action_with_connection(conn, record.action_key)
                if existing is not None:
                    immutable_fields = (
                        "run_id", "iteration", "action_id", "tool_name",
                        "arguments_sha256", "arguments_size_bytes",
                        "argument_summary", "dispatch_token",
                    )
                    if all(
                        getattr(existing, field) == getattr(record, field)
                        for field in immutable_fields
                    ):
                        return existing
                    raise PursuitStoreConflictError(
                        f"action_key 已绑定不同的行动输入：{record.action_key}"
                    )
                if conn.execute(
                    "SELECT 1 FROM pursuit_runs WHERE id = ?", (record.run_id,)
                ).fetchone() is None:
                    raise PursuitStoreConflictError(
                        f"行动对应的 PursuitRun 不存在：{record.run_id}"
                    )
                conn.execute(
                    """
                    INSERT INTO pursuit_actions (
                        action_key, run_id, background_task_id, latest_sequence,
                        payload_json, payload_sha256
                    ) VALUES (?, ?, '', ?, ?, ?)
                    """,
                    (
                        record.action_key,
                        record.run_id,
                        record.sequence,
                        record.canonical_json(),
                        record.digest(),
                    ),
                )
                self._append_action_event(conn, record, previous_digest="")
                return record
        except PursuitStoreError:
            raise
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"准备行动账本失败：{exc}") from exc

    def mark_action_dispatched(
        self,
        action_key: str,
        *,
        updated_at: float,
    ) -> PursuitActionRecord:
        return self._transition_action(
            action_key,
            target=PursuitActionState.DISPATCHED,
            allowed_from={PursuitActionState.PREPARED},
            updated_at=updated_at,
        )

    def mark_action_waiting(
        self,
        action_key: str,
        *,
        background_task_id: str,
        updated_at: float,
        result_summary: str = "",
    ) -> PursuitActionRecord:
        task_id = action_safe_text(background_task_id, limit=256).strip()
        if not task_id:
            raise ValueError("background_task_id 不能为空。")
        return self._transition_action(
            action_key,
            target=PursuitActionState.WAITING,
            allowed_from={PursuitActionState.DISPATCHED},
            updated_at=updated_at,
            background_task_id=task_id,
            result_status="running",
            result_summary=result_summary,
        )

    def mark_action_terminal(
        self,
        action_key: str,
        *,
        succeeded: bool,
        result_status: str,
        result: object,
        updated_at: float,
    ) -> PursuitActionRecord:
        return self._transition_action(
            action_key,
            target=(
                PursuitActionState.COMPLETED
                if succeeded
                else PursuitActionState.FAILED
            ),
            allowed_from={
                PursuitActionState.DISPATCHED,
                PursuitActionState.WAITING,
            },
            updated_at=updated_at,
            result_status=action_safe_text(result_status, limit=64),
            result_summary=action_safe_text(result, limit=2_000),
            result_sha256=digest_result(result),
        )

    def mark_action_abandoned(
        self,
        action_key: str,
        *,
        reason: str,
        updated_at: float,
    ) -> PursuitActionRecord:
        """Close a prepared action that durable evidence proves was never dispatched."""
        return self._transition_action(
            action_key,
            target=PursuitActionState.FAILED,
            allowed_from={PursuitActionState.PREPARED},
            updated_at=updated_at,
            result_status="abandoned_before_dispatch",
            result_summary=action_safe_text(reason, limit=2_000),
            result_sha256=digest_result(reason),
        )

    def get_action(self, action_key: str) -> PursuitActionRecord | None:
        if not self._db_path.exists():
            return None
        try:
            with self._connect() as conn:
                return self._get_action_with_connection(conn, action_key)
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(f"行动账本结构校验失败：{exc}") from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"读取行动账本失败：{exc}") from exc

    def get_action_by_background_task(
        self,
        *,
        run_id: str,
        task_id: str,
    ) -> PursuitActionRecord | None:
        if not self._db_path.exists():
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT action_key FROM pursuit_actions "
                    "WHERE run_id = ? AND background_task_id = ?",
                    (run_id, task_id),
                ).fetchone()
                if row is None:
                    return None
                return self._get_action_with_connection(conn, str(row["action_key"]))
        except PursuitStoreError:
            raise
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"按后台任务读取行动账本失败：{exc}") from exc

    def list_action_events(self, action_key: str) -> list[PursuitActionRecord]:
        """Return the authenticated immutable lifecycle for diagnostics/tests."""
        if not self._db_path.exists():
            return []
        try:
            with self._connect() as conn:
                return self._verify_action_events(conn, action_key)
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(f"行动事件结构校验失败：{exc}") from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"读取行动事件失败：{exc}") from exc

    def list_actions(
        self,
        run_id: str,
        *,
        unresolved_only: bool = False,
    ) -> list[PursuitActionRecord]:
        """List authenticated actions for one run in deterministic lifecycle order."""
        if not self._db_path.exists():
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT action_key FROM pursuit_actions WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                records = [
                    self._get_action_with_connection(conn, str(row["action_key"]))
                    for row in rows
                ]
            result = [record for record in records if record is not None]
            if unresolved_only:
                result = [record for record in result if not record.is_terminal]
            return sorted(
                result,
                key=lambda item: (
                    item.iteration,
                    item.prepared_at,
                    item.action_id,
                    item.action_key,
                ),
            )
        except PursuitStoreError:
            raise
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"列出行动账本失败：{exc}") from exc

    def _transition_action(
        self,
        action_key: str,
        *,
        target: PursuitActionState,
        allowed_from: set[PursuitActionState],
        updated_at: float,
        background_task_id: str | None = None,
        result_status: str | None = None,
        result_summary: str | None = None,
        result_sha256: str | None = None,
    ) -> PursuitActionRecord:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._get_action_with_connection(conn, action_key)
                if current is None:
                    raise PursuitStoreConflictError(f"行动不存在：{action_key}")
                update = {
                    "state": target,
                    "sequence": current.sequence + 1,
                    "updated_at": updated_at,
                    "background_task_id": (
                        current.background_task_id
                        if background_task_id is None
                        else background_task_id
                    ),
                    "result_status": (
                        current.result_status
                        if result_status is None
                        else result_status
                    ),
                    "result_summary": (
                        current.result_summary
                        if result_summary is None
                        else action_safe_text(result_summary, limit=2_000)
                    ),
                    "result_sha256": (
                        current.result_sha256
                        if result_sha256 is None
                        else result_sha256
                    ),
                }
                candidate = current.model_copy(update=update)
                candidate = PursuitActionRecord.model_validate(
                    candidate.model_dump(mode="python")
                )
                if current.state is target:
                    comparable = candidate.model_copy(update={
                        "sequence": current.sequence,
                        "updated_at": current.updated_at,
                    })
                    if comparable == current:
                        return current
                    raise PursuitStoreConflictError(
                        f"行动 {action_key} 的 {target.value} 结果发生冲突。"
                    )
                if current.state not in allowed_from:
                    raise PursuitStoreConflictError(
                        f"行动状态不能从 {current.state.value} 转为 {target.value}。"
                    )
                self._append_action_event(
                    conn,
                    candidate,
                    previous_digest=current.digest(),
                )
                conn.execute(
                    """
                    UPDATE pursuit_actions
                    SET background_task_id = ?, latest_sequence = ?,
                        payload_json = ?, payload_sha256 = ?
                    WHERE action_key = ? AND latest_sequence = ?
                    """,
                    (
                        candidate.background_task_id,
                        candidate.sequence,
                        candidate.canonical_json(),
                        candidate.digest(),
                        action_key,
                        current.sequence,
                    ),
                )
                if conn.total_changes < 2:
                    raise PursuitStoreConflictError(
                        f"行动 {action_key} 被并发更新，拒绝覆盖。"
                    )
                return candidate
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(f"行动状态校验失败：{exc}") from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(f"更新行动账本失败：{exc}") from exc

    @staticmethod
    def _append_action_event(
        conn: sqlite3.Connection,
        record: PursuitActionRecord,
        *,
        previous_digest: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO pursuit_action_events (
                action_key, sequence, state, payload_json, payload_sha256,
                previous_payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.action_key,
                record.sequence,
                record.state.value,
                record.canonical_json(),
                record.digest(),
                previous_digest,
                record.updated_at,
            ),
        )

    def _get_action_with_connection(
        self,
        conn: sqlite3.Connection,
        action_key: str,
    ) -> PursuitActionRecord | None:
        row = conn.execute(
            "SELECT * FROM pursuit_actions WHERE action_key = ?", (action_key,)
        ).fetchone()
        if row is None:
            return None
        events = self._verify_action_events(conn, action_key)
        if not events:
            raise PursuitStoreError("行动快照存在但事件链为空，拒绝读取。")
        latest = events[-1]
        payload = str(row["payload_json"])
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, str(row["payload_sha256"])):
            raise PursuitStoreError("行动快照摘要校验失败，拒绝读取。")
        snapshot = PursuitActionRecord.model_validate_json(payload)
        if (
            snapshot != latest
            or str(row["action_key"]) != latest.action_key
            or str(row["run_id"]) != latest.run_id
            or str(row["background_task_id"]) != latest.background_task_id
            or int(row["latest_sequence"]) != latest.sequence
        ):
            raise PursuitStoreError("行动快照与事件链末端不一致，拒绝读取。")
        return latest

    def _transition_recovery_attempt(
        self,
        attempt_id: str,
        *,
        target: PursuitRecoveryAttemptState,
        updated_at: float,
        admitted_at: float = 0,
        resolved_at: float = 0,
        lease_epoch: int = 0,
        checkpoint_id: str = "",
        result_code: str = "",
        boundary_decision_id: str = "",
    ) -> PursuitRecoveryAttempt:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._get_recovery_attempt_with_connection(
                    conn,
                    attempt_id,
                )
                if current is None:
                    raise PursuitStoreConflictError(
                        f"recovery attempt 不存在：{attempt_id}"
                    )
                if current.state in {
                    PursuitRecoveryAttemptState.RESOLVED,
                    PursuitRecoveryAttemptState.FAILED,
                }:
                    if (
                        current.state is target
                        and current.updated_at == updated_at
                        and current.result_code == result_code
                        and current.boundary_decision_id == boundary_decision_id
                    ):
                        return current
                    raise PursuitStoreConflictError(
                        "terminal recovery attempt 不得再次迁移。"
                    )
                if target is PursuitRecoveryAttemptState.ADMITTED:
                    if current.state is not PursuitRecoveryAttemptState.REQUESTED:
                        if (
                            current.state is PursuitRecoveryAttemptState.ADMITTED
                            and current.admitted_at == admitted_at
                            and current.lease_epoch == lease_epoch
                            and current.checkpoint_id == checkpoint_id
                        ):
                            return current
                        raise PursuitStoreConflictError(
                            "recovery attempt admission 发生冲突。"
                        )
                    candidate = current.model_copy(update={
                        "sequence": 2,
                        "state": target,
                        "updated_at": updated_at,
                        "admitted_at": admitted_at,
                        "lease_epoch": lease_epoch,
                        "checkpoint_id": checkpoint_id,
                    })
                elif target in {
                    PursuitRecoveryAttemptState.RESOLVED,
                    PursuitRecoveryAttemptState.FAILED,
                }:
                    if current.state not in {
                        PursuitRecoveryAttemptState.REQUESTED,
                        PursuitRecoveryAttemptState.ADMITTED,
                    }:
                        raise PursuitStoreConflictError(
                            "recovery attempt 不能从当前状态进入终态。"
                        )
                    candidate = current.model_copy(update={
                        "sequence": current.sequence + 1,
                        "state": target,
                        "updated_at": updated_at,
                        "resolved_at": resolved_at,
                        "result_code": result_code,
                        "boundary_decision_id": boundary_decision_id,
                    })
                else:
                    raise ValueError("不支持的 recovery attempt 迁移。")
                candidate = PursuitRecoveryAttempt.model_validate(
                    candidate.model_dump(mode="json")
                )
                self._append_recovery_attempt_event(
                    conn,
                    candidate,
                    previous_digest=current.digest(),
                )
                cursor = conn.execute(
                    """
                    UPDATE pursuit_recovery_attempts
                    SET latest_sequence = ?, state = ?, payload_json = ?,
                        payload_sha256 = ?, updated_at = ?
                    WHERE attempt_id = ? AND latest_sequence = ?
                    """,
                    (
                        candidate.sequence,
                        candidate.state.value,
                        candidate.canonical_json(),
                        candidate.digest(),
                        candidate.updated_at,
                        attempt_id,
                        current.sequence,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PursuitStoreConflictError(
                        "recovery attempt 被并发更新，拒绝覆盖。"
                    )
                self._deliver_terminal_outbox_with_connection(
                    conn,
                    attempt=candidate,
                    delivered_at=updated_at,
                )
                return candidate
        except PursuitStoreError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PursuitStoreError(
                f"更新 recovery attempt 失败：{exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise PursuitStoreError(
                f"更新 recovery attempt 失败：{exc}"
            ) from exc

    def _get_recovery_attempt_with_connection(
        self,
        conn: sqlite3.Connection,
        attempt_id: str,
    ) -> PursuitRecoveryAttempt | None:
        row = conn.execute(
            "SELECT * FROM pursuit_recovery_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        events = self._verify_recovery_attempt_events(conn, attempt_id)
        if not events:
            raise PursuitStoreError(
                "recovery attempt 快照存在但事件链为空，拒绝读取。"
            )
        payload = str(row["payload_json"])
        actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_digest, str(row["payload_sha256"])):
            raise PursuitStoreError(
                "recovery attempt 快照摘要校验失败，拒绝读取。"
            )
        snapshot = PursuitRecoveryAttempt.model_validate_json(payload)
        latest = events[-1]
        if (
            snapshot != latest
            or str(row["attempt_id"]) != latest.attempt_id
            or str(row["run_id"]) != latest.run_id
            or int(row["latest_sequence"]) != latest.sequence
            or str(row["state"]) != latest.state.value
        ):
            raise PursuitStoreError(
                "recovery attempt 快照与事件链末端不一致，拒绝读取。"
            )
        return latest

    def _enqueue_terminal_outbox_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        checkpoint: PursuitCheckpoint,
    ) -> PursuitTerminalOutboxRecord | None:
        terminal_statuses = {
            PursuitRunStatus.WAITING.value,
            PursuitRunStatus.BLOCKED.value,
            PursuitRunStatus.COMPLETED.value,
            PursuitRunStatus.CANCELLED.value,
            PursuitRunStatus.BUDGET_EXCEEDED.value,
        }
        if checkpoint.status not in terminal_statuses:
            return None
        rows = conn.execute(
            """
            SELECT attempt_id
            FROM pursuit_recovery_attempts
            WHERE run_id = ? AND state = 'admitted'
            ORDER BY requested_at ASC, attempt_id ASC
            LIMIT 2
            """,
            (checkpoint.run_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise PursuitStoreConflictError(
                "同一 PursuitRun 存在多个 admitted recovery attempt，拒绝创建 outbox。"
            )
        attempt = self._get_recovery_attempt_with_connection(
            conn,
            str(rows[0]["attempt_id"]),
        )
        if attempt is None or attempt.state is not PursuitRecoveryAttemptState.ADMITTED:
            raise PursuitStoreConflictError("terminal outbox admission 事实不可用。")
        checkpoint_id = checkpoint.checkpoint_id()
        if (
            checkpoint.created_at <= attempt.admitted_at
            or checkpoint_id == attempt.checkpoint_id
        ):
            return None
        run_row = conn.execute(
            "SELECT status, boundary_decision_id FROM pursuit_runs WHERE id = ?",
            (checkpoint.run_id,),
        ).fetchone()
        if run_row is None:
            raise PursuitStoreConflictError("terminal outbox 对应 PursuitRun 不存在。")
        boundary_id = str(run_row["boundary_decision_id"])
        if str(run_row["status"]) != checkpoint.status or not boundary_id:
            raise PursuitStoreConflictError(
                "terminal checkpoint 与 PursuitRun 终态不一致，拒绝缺失 outbox 的提交。"
            )
        boundary_row = conn.execute(
            """
            SELECT *
            FROM pursuit_boundary_decisions
            WHERE run_id = ? AND decision_id = ?
            """,
            (checkpoint.run_id, boundary_id),
        ).fetchone()
        if boundary_row is None:
            raise PursuitStoreConflictError("terminal outbox 机械裁判指针无效。")
        boundary = _boundary_decision_from_row(boundary_row)
        boundary_recorded_at = float(boundary_row["recorded_at"])
        if (
            boundary.status != checkpoint.status
            or not attempt.admitted_at < boundary_recorded_at <= checkpoint.created_at
        ):
            raise PursuitStoreConflictError(
                "terminal checkpoint 缺少准入后的同状态机械裁判，拒绝提交。"
            )
        record = PursuitTerminalOutboxRecord(
            outbox_id=pursuit_terminal_outbox_id(
                attempt_id=attempt.attempt_id,
                admitted_attempt_sha256=attempt.digest(),
                boundary_decision_id=boundary_id,
                checkpoint_id=checkpoint_id,
            ),
            attempt_id=attempt.attempt_id,
            run_id=attempt.run_id,
            admitted_attempt_sha256=attempt.digest(),
            boundary_decision_id=boundary_id,
            checkpoint_id=checkpoint_id,
            sequence=1,
            state=PursuitTerminalOutboxState.PENDING,
            created_at=checkpoint.created_at,
            updated_at=checkpoint.created_at,
        )
        existing_row = conn.execute(
            "SELECT outbox_id FROM pursuit_terminal_outbox WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
        if existing_row is not None:
            existing = self._get_terminal_outbox_with_connection(
                conn,
                str(existing_row["outbox_id"]),
            )
            if existing is not None and existing.outbox_id == record.outbox_id:
                return existing
            raise PursuitStoreConflictError(
                "recovery attempt 已绑定不同 terminal outbox 事实。"
            )
        conn.execute(
            """
            INSERT INTO pursuit_terminal_outbox (
                outbox_id, attempt_id, run_id, latest_sequence, state,
                payload_json, payload_sha256, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.outbox_id,
                record.attempt_id,
                record.run_id,
                record.sequence,
                record.state.value,
                record.canonical_json(),
                record.digest(),
                record.created_at,
                record.updated_at,
            ),
        )
        self._append_terminal_outbox_event(conn, record, previous_digest="")
        self._create_terminal_dispatch_with_connection(
            conn,
            outbox=record,
            next_attempt_at=max(record.created_at, attempt.admitted_at + 30.0),
        )
        return record

    def _deliver_terminal_outbox_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        attempt: PursuitRecoveryAttempt,
        delivered_at: float,
    ) -> PursuitTerminalOutboxRecord | None:
        row = conn.execute(
            "SELECT outbox_id FROM pursuit_terminal_outbox WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
        if row is None:
            return None
        current = self._get_terminal_outbox_with_connection(
            conn,
            str(row["outbox_id"]),
        )
        if current is None:
            raise PursuitStoreError("terminal outbox 快照丢失。")
        terminal_digest = attempt.digest()
        if current.state is PursuitTerminalOutboxState.DELIVERED:
            if hmac.compare_digest(
                current.terminal_attempt_sha256,
                terminal_digest,
            ):
                return current
            raise PursuitStoreConflictError(
                "terminal outbox 已绑定不同 recovery attempt 终态。"
            )
        if attempt.state not in {
            PursuitRecoveryAttemptState.RESOLVED,
            PursuitRecoveryAttemptState.FAILED,
        }:
            raise PursuitStoreConflictError("terminal outbox 只能确认终态 attempt。")
        candidate = PursuitTerminalOutboxRecord.model_validate(
            current.model_copy(update={
                "sequence": 2,
                "state": PursuitTerminalOutboxState.DELIVERED,
                "updated_at": delivered_at,
                "delivered_at": delivered_at,
                "terminal_attempt_sha256": terminal_digest,
            }).model_dump(mode="json")
        )
        self._append_terminal_outbox_event(
            conn,
            candidate,
            previous_digest=current.digest(),
        )
        cursor = conn.execute(
            """
            UPDATE pursuit_terminal_outbox
            SET latest_sequence = ?, state = ?, payload_json = ?,
                payload_sha256 = ?, updated_at = ?
            WHERE outbox_id = ? AND latest_sequence = ? AND state = 'pending'
            """,
            (
                candidate.sequence,
                candidate.state.value,
                candidate.canonical_json(),
                candidate.digest(),
                candidate.updated_at,
                candidate.outbox_id,
                current.sequence,
            ),
        )
        if cursor.rowcount != 1:
            raise PursuitStoreConflictError("terminal outbox 被并发更新，拒绝覆盖。")
        self._deliver_terminal_dispatch_with_connection(
            conn,
            outbox=candidate,
            delivered_at=delivered_at,
        )
        return candidate

    def _create_terminal_dispatch_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        outbox: PursuitTerminalOutboxRecord,
        next_attempt_at: float,
    ) -> PursuitTerminalOutboxDispatch:
        existing = self._get_terminal_dispatch_with_connection(
            conn,
            outbox.outbox_id,
        )
        if existing is not None:
            if hmac.compare_digest(
                existing.pending_outbox_sha256,
                outbox.digest(),
            ):
                return existing
            raise PursuitStoreConflictError(
                "terminal dispatch 已绑定不同 pending outbox。"
            )
        dispatch = PursuitTerminalOutboxDispatch(
            outbox_id=outbox.outbox_id,
            pending_outbox_sha256=outbox.digest(),
            sequence=1,
            state=PursuitTerminalDispatchState.IDLE,
            next_attempt_at=next_attempt_at,
            created_at=outbox.created_at,
            updated_at=outbox.created_at,
        )
        conn.execute(
            """
            INSERT INTO pursuit_terminal_outbox_dispatch (
                outbox_id, latest_sequence, state, payload_json,
                payload_sha256, next_attempt_at, claim_expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dispatch.outbox_id,
                dispatch.sequence,
                dispatch.state.value,
                dispatch.canonical_json(),
                dispatch.digest(),
                dispatch.next_attempt_at,
                dispatch.claim_expires_at,
                dispatch.updated_at,
            ),
        )
        self._append_terminal_dispatch_event(conn, dispatch, previous_digest="")
        return dispatch

    def _deliver_terminal_dispatch_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        outbox: PursuitTerminalOutboxRecord,
        delivered_at: float,
    ) -> PursuitTerminalOutboxDispatch:
        current = self._get_terminal_dispatch_with_connection(
            conn,
            outbox.outbox_id,
        )
        if current is None:
            raise PursuitStoreError("delivered outbox 缺少 dispatch。")
        if current.state is PursuitTerminalDispatchState.DELIVERED:
            return current
        candidate = PursuitTerminalOutboxDispatch.model_validate(
            current.model_copy(update={
                "sequence": current.sequence + 1,
                "state": PursuitTerminalDispatchState.DELIVERED,
                "claim_owner_sha256": "",
                "claim_expires_at": 0,
                "next_attempt_at": 0,
                "last_failure_code": "",
                "updated_at": delivered_at,
            }).model_dump(mode="json")
        )
        self._update_terminal_dispatch_with_connection(
            conn,
            current=current,
            candidate=candidate,
        )
        return candidate

    def _get_terminal_dispatch_with_connection(
        self,
        conn: sqlite3.Connection,
        outbox_id: str,
    ) -> PursuitTerminalOutboxDispatch | None:
        row = conn.execute(
            "SELECT * FROM pursuit_terminal_outbox_dispatch WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            return None
        events = self._verify_terminal_dispatch_events(conn, outbox_id)
        if not events:
            raise PursuitStoreError("terminal dispatch 快照存在但事件链为空。")
        payload = str(row["payload_json"])
        actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_digest, str(row["payload_sha256"])):
            raise PursuitStoreError("terminal dispatch 快照摘要校验失败。")
        snapshot = PursuitTerminalOutboxDispatch.model_validate_json(payload)
        latest = events[-1]
        if (
            snapshot != latest
            or int(row["latest_sequence"]) != latest.sequence
            or str(row["state"]) != latest.state.value
            or float(row["next_attempt_at"]) != latest.next_attempt_at
            or float(row["claim_expires_at"]) != latest.claim_expires_at
        ):
            raise PursuitStoreError("terminal dispatch 快照与事件链末端不一致。")
        outbox_events = self._verify_terminal_outbox_events(conn, outbox_id)
        if not outbox_events or not hmac.compare_digest(
            outbox_events[0].digest(),
            latest.pending_outbox_sha256,
        ):
            raise PursuitStoreError("terminal dispatch 与 pending outbox 不一致。")
        if latest.state is PursuitTerminalDispatchState.DELIVERED and (
            len(outbox_events) != 2
            or outbox_events[-1].state is not PursuitTerminalOutboxState.DELIVERED
        ):
            raise PursuitStoreError("terminal dispatch 与 delivered outbox 不一致。")
        return latest

    def _update_terminal_dispatch_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        current: PursuitTerminalOutboxDispatch,
        candidate: PursuitTerminalOutboxDispatch,
    ) -> None:
        self._append_terminal_dispatch_event(
            conn,
            candidate,
            previous_digest=current.digest(),
        )
        cursor = conn.execute(
            """
            UPDATE pursuit_terminal_outbox_dispatch
            SET latest_sequence = ?, state = ?, payload_json = ?,
                payload_sha256 = ?, next_attempt_at = ?,
                claim_expires_at = ?, updated_at = ?
            WHERE outbox_id = ? AND latest_sequence = ? AND state = ?
            """,
            (
                candidate.sequence,
                candidate.state.value,
                candidate.canonical_json(),
                candidate.digest(),
                candidate.next_attempt_at,
                candidate.claim_expires_at,
                candidate.updated_at,
                candidate.outbox_id,
                current.sequence,
                current.state.value,
            ),
        )
        if cursor.rowcount != 1:
            raise PursuitStoreConflictError("terminal dispatch 被并发更新。")

    @staticmethod
    def _append_terminal_dispatch_event(
        conn: sqlite3.Connection,
        dispatch: PursuitTerminalOutboxDispatch,
        *,
        previous_digest: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO pursuit_terminal_outbox_dispatch_events (
                outbox_id, sequence, state, payload_json, payload_sha256,
                previous_payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dispatch.outbox_id,
                dispatch.sequence,
                dispatch.state.value,
                dispatch.canonical_json(),
                dispatch.digest(),
                previous_digest,
                dispatch.updated_at,
            ),
        )

    @staticmethod
    def _verify_terminal_dispatch_events(
        conn: sqlite3.Connection,
        outbox_id: str,
    ) -> list[PursuitTerminalOutboxDispatch]:
        rows = conn.execute(
            """
            SELECT * FROM pursuit_terminal_outbox_dispatch_events
            WHERE outbox_id = ? ORDER BY sequence ASC
            """,
            (outbox_id,),
        ).fetchall()
        records: list[PursuitTerminalOutboxDispatch] = []
        previous_digest = ""
        for expected_sequence, row in enumerate(rows, start=1):
            payload = str(row["payload_json"])
            actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if int(row["sequence"]) != expected_sequence:
                raise PursuitStoreError("terminal dispatch 事件序号不连续。")
            if not hmac.compare_digest(actual_digest, str(row["payload_sha256"])):
                raise PursuitStoreError("terminal dispatch 事件摘要校验失败。")
            if not hmac.compare_digest(
                previous_digest,
                str(row["previous_payload_sha256"]),
            ):
                raise PursuitStoreError("terminal dispatch 事件哈希链断裂。")
            record = PursuitTerminalOutboxDispatch.model_validate_json(payload)
            if (
                record.outbox_id != outbox_id
                or record.sequence != expected_sequence
                or record.state.value != str(row["state"])
            ):
                raise PursuitStoreError("terminal dispatch 事件元数据不一致。")
            records.append(record)
            previous_digest = actual_digest
        return records

    def _backfill_terminal_dispatches_with_connection(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        rows = conn.execute(
            """
            SELECT o.outbox_id
            FROM pursuit_terminal_outbox AS o
            LEFT JOIN pursuit_terminal_outbox_dispatch AS d
              ON d.outbox_id = o.outbox_id
            WHERE d.outbox_id IS NULL
            ORDER BY o.created_at ASC, o.outbox_id ASC
            LIMIT 10001
            """
        ).fetchall()
        if len(rows) > 10_000:
            raise PursuitStoreError(
                "terminal dispatch 迁移超过 10000 条安全上限。"
            )
        for row in rows:
            outbox_id = str(row["outbox_id"])
            current = self._get_terminal_outbox_with_connection(conn, outbox_id)
            events = self._verify_terminal_outbox_events(conn, outbox_id)
            if current is None or not events:
                raise PursuitStoreError("terminal dispatch 迁移源 outbox 无效。")
            pending = events[0]
            dispatch = self._create_terminal_dispatch_with_connection(
                conn,
                outbox=pending,
                next_attempt_at=pending.created_at,
            )
            if current.state is PursuitTerminalOutboxState.DELIVERED:
                self._deliver_terminal_dispatch_with_connection(
                    conn,
                    outbox=current,
                    delivered_at=current.delivered_at,
                )
            elif dispatch.state is not PursuitTerminalDispatchState.IDLE:
                raise PursuitStoreError("terminal dispatch 迁移初态无效。")

    def _get_terminal_outbox_with_connection(
        self,
        conn: sqlite3.Connection,
        outbox_id: str,
    ) -> PursuitTerminalOutboxRecord | None:
        row = conn.execute(
            "SELECT * FROM pursuit_terminal_outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            return None
        events = self._verify_terminal_outbox_events(conn, outbox_id)
        if not events:
            raise PursuitStoreError("terminal outbox 快照存在但事件链为空。")
        payload = str(row["payload_json"])
        actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_digest, str(row["payload_sha256"])):
            raise PursuitStoreError("terminal outbox 快照摘要校验失败。")
        snapshot = PursuitTerminalOutboxRecord.model_validate_json(payload)
        latest = events[-1]
        if (
            snapshot != latest
            or str(row["attempt_id"]) != latest.attempt_id
            or str(row["run_id"]) != latest.run_id
            or int(row["latest_sequence"]) != latest.sequence
            or str(row["state"]) != latest.state.value
        ):
            raise PursuitStoreError("terminal outbox 快照与事件链末端不一致。")
        attempt_events = self._verify_recovery_attempt_events(
            conn,
            latest.attempt_id,
        )
        if len(attempt_events) < 2 or not hmac.compare_digest(
            attempt_events[1].digest(),
            latest.admitted_attempt_sha256,
        ):
            raise PursuitStoreError("terminal outbox 与 admission 事件不一致。")
        if latest.state is PursuitTerminalOutboxState.DELIVERED and (
            len(attempt_events) != 3
            or not hmac.compare_digest(
                attempt_events[-1].digest(),
                latest.terminal_attempt_sha256,
            )
        ):
            raise PursuitStoreError("terminal outbox 与 attempt 终态不一致。")
        boundary_row = conn.execute(
            """
            SELECT 1 FROM pursuit_boundary_decisions
            WHERE run_id = ? AND decision_id = ?
            """,
            (latest.run_id, latest.boundary_decision_id),
        ).fetchone()
        if boundary_row is None:
            raise PursuitStoreError("terminal outbox 引用的机械裁判不存在。")
        return latest

    @staticmethod
    def _append_terminal_outbox_event(
        conn: sqlite3.Connection,
        record: PursuitTerminalOutboxRecord,
        *,
        previous_digest: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO pursuit_terminal_outbox_events (
                outbox_id, sequence, state, payload_json, payload_sha256,
                previous_payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.outbox_id,
                record.sequence,
                record.state.value,
                record.canonical_json(),
                record.digest(),
                previous_digest,
                record.updated_at,
            ),
        )

    @staticmethod
    def _verify_terminal_outbox_events(
        conn: sqlite3.Connection,
        outbox_id: str,
    ) -> list[PursuitTerminalOutboxRecord]:
        rows = conn.execute(
            """
            SELECT * FROM pursuit_terminal_outbox_events
            WHERE outbox_id = ? ORDER BY sequence ASC
            """,
            (outbox_id,),
        ).fetchall()
        records: list[PursuitTerminalOutboxRecord] = []
        previous_digest = ""
        for expected_sequence, row in enumerate(rows, start=1):
            payload = str(row["payload_json"])
            actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if int(row["sequence"]) != expected_sequence:
                raise PursuitStoreError("terminal outbox 事件序号不连续。")
            if not hmac.compare_digest(actual_digest, str(row["payload_sha256"])):
                raise PursuitStoreError("terminal outbox 事件摘要校验失败。")
            if not hmac.compare_digest(
                previous_digest,
                str(row["previous_payload_sha256"]),
            ):
                raise PursuitStoreError("terminal outbox 事件哈希链断裂。")
            record = PursuitTerminalOutboxRecord.model_validate_json(payload)
            if (
                record.outbox_id != outbox_id
                or record.sequence != expected_sequence
                or record.state.value != str(row["state"])
            ):
                raise PursuitStoreError("terminal outbox 事件元数据不一致。")
            records.append(record)
            previous_digest = actual_digest
        return records

    def _get_recovery_reconciliation_with_connection(
        self,
        conn: sqlite3.Connection,
        attempt_id: str,
    ) -> PursuitRecoveryReconciliationReceipt | None:
        row = conn.execute(
            """
            SELECT *
            FROM pursuit_recovery_reconciliations
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        payload = str(row["payload_json"])
        actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(
            actual_digest,
            str(row["payload_sha256"]),
        ):
            raise PursuitStoreError(
                "恢复对账回执摘要校验失败，拒绝读取。"
            )
        receipt = PursuitRecoveryReconciliationReceipt.model_validate_json(
            payload
        )
        if (
            receipt.attempt_id != attempt_id
            or receipt.receipt_id != str(row["receipt_id"])
            or receipt.reconciled_at != float(row["created_at"])
        ):
            raise PursuitStoreError(
                "恢复对账回执元数据与 payload 不一致。"
            )
        attempt = self._get_recovery_attempt_with_connection(
            conn,
            attempt_id,
        )
        events = self._verify_recovery_attempt_events(conn, attempt_id)
        if (
            attempt is None
            or attempt.state is not PursuitRecoveryAttemptState.RESOLVED
            or receipt.run_id != attempt.run_id
            or attempt.digest() != receipt.attempt_after_sha256
            or attempt.boundary_decision_id != receipt.boundary_decision_id
            or attempt.result_code != receipt.result_code
            or len(events) != 3
            or events[-2].state is not PursuitRecoveryAttemptState.ADMITTED
            or events[-2].digest() != receipt.attempt_before_sha256
            or events[-2].admitted_at != receipt.admitted_at
            or events[-2].lease_epoch != receipt.admitted_lease_epoch
        ):
            raise PursuitStoreError(
                "恢复对账回执与 recovery attempt 终态不一致。"
            )
        boundary_row = conn.execute(
            """
            SELECT * FROM pursuit_boundary_decisions
            WHERE run_id = ? AND decision_id = ?
            """,
            (receipt.run_id, receipt.boundary_decision_id),
        ).fetchone()
        if boundary_row is None:
            raise PursuitStoreError("恢复对账回执引用的机械裁判不存在。")
        boundary = _boundary_decision_from_row(boundary_row)
        if (
            boundary.code != receipt.result_code
            or float(boundary_row["recorded_at"])
            != receipt.boundary_recorded_at
        ):
            raise PursuitStoreError("恢复对账回执与机械裁判不一致。")
        checkpoint = self._get_checkpoint_with_connection(
            conn,
            receipt.run_id,
        )
        if (
            checkpoint is None
            or checkpoint.checkpoint_id() != receipt.checkpoint_id
            or checkpoint.created_at != receipt.checkpoint_created_at
        ):
            raise PursuitStoreError("恢复对账回执与后置 checkpoint 不一致。")
        return receipt

    @staticmethod
    def _append_recovery_attempt_event(
        conn: sqlite3.Connection,
        attempt: PursuitRecoveryAttempt,
        *,
        previous_digest: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO pursuit_recovery_attempt_events (
                attempt_id, sequence, state, payload_json, payload_sha256,
                previous_payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.attempt_id,
                attempt.sequence,
                attempt.state.value,
                attempt.canonical_json(),
                attempt.digest(),
                previous_digest,
                attempt.updated_at,
            ),
        )

    @staticmethod
    def _verify_recovery_attempt_events(
        conn: sqlite3.Connection,
        attempt_id: str,
    ) -> list[PursuitRecoveryAttempt]:
        rows = conn.execute(
            """
            SELECT *
            FROM pursuit_recovery_attempt_events
            WHERE attempt_id = ?
            ORDER BY sequence ASC
            """,
            (attempt_id,),
        ).fetchall()
        records: list[PursuitRecoveryAttempt] = []
        previous_digest = ""
        for expected_sequence, row in enumerate(rows, start=1):
            payload = str(row["payload_json"])
            actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if int(row["sequence"]) != expected_sequence:
                raise PursuitStoreError(
                    "recovery attempt 事件序号不连续，拒绝读取。"
                )
            if not hmac.compare_digest(
                actual_digest,
                str(row["payload_sha256"]),
            ):
                raise PursuitStoreError(
                    "recovery attempt 事件摘要校验失败，拒绝读取。"
                )
            if not hmac.compare_digest(
                previous_digest,
                str(row["previous_payload_sha256"]),
            ):
                raise PursuitStoreError(
                    "recovery attempt 事件哈希链断裂，拒绝读取。"
                )
            record = PursuitRecoveryAttempt.model_validate_json(payload)
            if (
                record.attempt_id != attempt_id
                or record.sequence != expected_sequence
                or record.state.value != str(row["state"])
            ):
                raise PursuitStoreError(
                    "recovery attempt 事件元数据与 payload 不一致。"
                )
            records.append(record)
            previous_digest = actual_digest
        return records

    @staticmethod
    def _verify_action_events(
        conn: sqlite3.Connection,
        action_key: str,
    ) -> list[PursuitActionRecord]:
        rows = conn.execute(
            "SELECT * FROM pursuit_action_events WHERE action_key = ? "
            "ORDER BY sequence ASC",
            (action_key,),
        ).fetchall()
        records: list[PursuitActionRecord] = []
        previous_digest = ""
        for expected_sequence, row in enumerate(rows, start=1):
            payload = str(row["payload_json"])
            actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if int(row["sequence"]) != expected_sequence:
                raise PursuitStoreError("行动事件序号不连续，拒绝读取。")
            if not hmac.compare_digest(actual_digest, str(row["payload_sha256"])):
                raise PursuitStoreError("行动事件摘要校验失败，拒绝读取。")
            if not hmac.compare_digest(
                previous_digest, str(row["previous_payload_sha256"])
            ):
                raise PursuitStoreError("行动事件哈希链断裂，拒绝读取。")
            record = PursuitActionRecord.model_validate_json(payload)
            if (
                record.action_key != action_key
                or record.sequence != expected_sequence
                or record.state.value != str(row["state"])
            ):
                raise PursuitStoreError("行动事件元数据与 payload 不一致。")
            records.append(record)
            previous_digest = actual_digest
        return records

    def _connect(self) -> sqlite3.Connection:
        self._ensure_initialized()
        return self._open_connection()

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._base_dir.mkdir(parents=True, exist_ok=True)
            with self._open_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_runs (
                        id TEXT PRIMARY KEY,
                        goal TEXT NOT NULL,
                        status TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        iteration INTEGER NOT NULL DEFAULT 0,
                        criteria_total INTEGER NOT NULL DEFAULT 0,
                        criteria_verified INTEGER NOT NULL DEFAULT 0,
                        failure_count INTEGER NOT NULL DEFAULT 0,
                        blocked_reason TEXT NOT NULL DEFAULT '',
                        next_action TEXT NOT NULL DEFAULT '',
                        worktree_name TEXT NOT NULL DEFAULT '',
                        worktree_path TEXT NOT NULL DEFAULT '',
                        boundary_decision_id TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_checkpoints (
                        run_id TEXT PRIMARY KEY,
                        sequence INTEGER NOT NULL CHECK(sequence >= 1),
                        schema_version INTEGER NOT NULL,
                        checkpoint_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES pursuit_runs(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_evidence (
                        run_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        source TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        is_hard INTEGER NOT NULL,
                        timestamp REAL NOT NULL,
                        PRIMARY KEY(run_id, seq),
                        FOREIGN KEY(run_id) REFERENCES pursuit_runs(id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_waits (
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        action_id TEXT NOT NULL,
                        command TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(run_id, task_id),
                        FOREIGN KEY(run_id) REFERENCES pursuit_runs(id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_boundary_decisions (
                        run_id TEXT NOT NULL,
                        decision_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        recorded_at REAL NOT NULL,
                        PRIMARY KEY(run_id, decision_id),
                        FOREIGN KEY(run_id) REFERENCES pursuit_runs(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                run_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(pursuit_runs)")
                }
                if "boundary_decision_id" not in run_columns:
                    conn.execute(
                        "ALTER TABLE pursuit_runs ADD COLUMN "
                        "boundary_decision_id TEXT NOT NULL DEFAULT ''"
                    )
                    conn.execute(
                        """
                        UPDATE pursuit_runs
                        SET boundary_decision_id = COALESCE((
                            SELECT decision_id
                            FROM pursuit_boundary_decisions
                            WHERE run_id = pursuit_runs.id
                            ORDER BY recorded_at DESC, decision_id DESC
                            LIMIT 1
                        ), '')
                        WHERE boundary_decision_id = ''
                        """
                    )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_actions (
                        action_key TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        background_task_id TEXT NOT NULL DEFAULT '',
                        latest_sequence INTEGER NOT NULL CHECK(latest_sequence >= 1),
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES pursuit_runs(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_pursuit_actions_background_task
                    ON pursuit_actions(run_id, background_task_id)
                    WHERE background_task_id != ''
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_action_events (
                        action_key TEXT NOT NULL,
                        sequence INTEGER NOT NULL CHECK(sequence >= 1),
                        state TEXT NOT NULL CHECK(state IN (
                            'prepared', 'dispatched', 'waiting',
                            'completed', 'failed'
                        )),
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        previous_payload_sha256 TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(action_key, sequence),
                        FOREIGN KEY(action_key) REFERENCES pursuit_actions(action_key)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_recovery_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        latest_sequence INTEGER NOT NULL
                            CHECK(latest_sequence BETWEEN 1 AND 3),
                        state TEXT NOT NULL CHECK(state IN (
                            'requested', 'admitted', 'resolved', 'failed'
                        )),
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        requested_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES pursuit_runs(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_pursuit_recovery_attempts_run_requested
                    ON pursuit_recovery_attempts(
                        run_id, requested_at DESC, attempt_id DESC
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_recovery_attempt_events (
                        attempt_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL
                            CHECK(sequence BETWEEN 1 AND 3),
                        state TEXT NOT NULL CHECK(state IN (
                            'requested', 'admitted', 'resolved', 'failed'
                        )),
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        previous_payload_sha256 TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(attempt_id, sequence),
                        FOREIGN KEY(attempt_id)
                            REFERENCES pursuit_recovery_attempts(attempt_id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_recovery_reconciliations (
                        attempt_id TEXT PRIMARY KEY,
                        receipt_id TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(attempt_id)
                            REFERENCES pursuit_recovery_attempts(attempt_id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_terminal_outbox (
                        outbox_id TEXT PRIMARY KEY,
                        attempt_id TEXT NOT NULL UNIQUE,
                        run_id TEXT NOT NULL,
                        latest_sequence INTEGER NOT NULL
                            CHECK(latest_sequence BETWEEN 1 AND 2),
                        state TEXT NOT NULL CHECK(state IN (
                            'pending', 'delivered'
                        )),
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        FOREIGN KEY(attempt_id)
                            REFERENCES pursuit_recovery_attempts(attempt_id)
                            ON DELETE CASCADE,
                        FOREIGN KEY(run_id) REFERENCES pursuit_runs(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_pursuit_terminal_outbox_recovery
                    ON pursuit_terminal_outbox(state, created_at, outbox_id)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_terminal_outbox_events (
                        outbox_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL
                            CHECK(sequence BETWEEN 1 AND 2),
                        state TEXT NOT NULL CHECK(state IN (
                            'pending', 'delivered'
                        )),
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        previous_payload_sha256 TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(outbox_id, sequence),
                        FOREIGN KEY(outbox_id)
                            REFERENCES pursuit_terminal_outbox(outbox_id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_terminal_outbox_dispatch (
                        outbox_id TEXT PRIMARY KEY,
                        latest_sequence INTEGER NOT NULL CHECK(latest_sequence >= 1),
                        state TEXT NOT NULL CHECK(state IN (
                            'idle', 'claimed', 'delivered'
                        )),
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        next_attempt_at REAL NOT NULL,
                        claim_expires_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        FOREIGN KEY(outbox_id)
                            REFERENCES pursuit_terminal_outbox(outbox_id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_pursuit_terminal_dispatch_recovery
                    ON pursuit_terminal_outbox_dispatch(
                        state, next_attempt_at, claim_expires_at, outbox_id
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                    pursuit_terminal_outbox_dispatch_events (
                        outbox_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL CHECK(sequence >= 1),
                        state TEXT NOT NULL CHECK(state IN (
                            'idle', 'claimed', 'delivered'
                        )),
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        previous_payload_sha256 TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(outbox_id, sequence),
                        FOREIGN KEY(outbox_id)
                            REFERENCES pursuit_terminal_outbox(outbox_id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pursuit_terminal_outbox_run_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        source_request_sha256 TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                    """
                )
                self._backfill_terminal_dispatches_with_connection(conn)
            self._initialized = True


def _terminal_dispatch_owner_sha256(owner_id: str) -> str:
    normalized = str(owner_id or "").strip()
    if not normalized or len(normalized) > 256:
        raise ValueError("terminal dispatch owner_id 必须为 1 到 256 个字符。")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def format_run(run: PursuitRun) -> str:
    """Format one pursuit run for users and tool output."""
    waits = run.waiting_on or []
    evidence = run.evidence or []
    wait_lines = "\n".join(
        f"  - {wait.task_id} / action {wait.action_id}: `{wait.command}`"
        for wait in waits
    ) or "  - 无"
    evidence_lines = "\n".join(
        f"  - [{item.kind}] {item.source}: {item.summary[:160]}"
        for item in evidence[-5:]
    ) or "  - 暂无"
    blocked = f"\n- 阻塞原因：{run.blocked_reason}" if run.blocked_reason else ""
    worktree = (
        f"\n- Worktree：{run.worktree_name} `{run.worktree_path}`"
        if run.worktree_name or run.worktree_path
        else ""
    )
    boundary = (
        f"\n- 最近裁判：`{run.boundary_decision.code}` · "
        f"{run.boundary_decision.status} · "
        f"`{run.boundary_decision.decision_id[:12]}`\n"
        f"- 裁判原因：{run.boundary_decision.reason}"
        if run.boundary_decision is not None
        else ""
    )
    return (
        f"### PursuitRun {run.id}\n"
        f"- 状态：{_status_label(run.status)}\n"
        f"- 阶段：{run.phase}\n"
        f"- 目标：{run.goal}\n"
        f"- 轮次：{run.iteration}\n"
        f"- 成功标准：{run.criteria_verified}/{run.criteria_total}\n"
        f"- 失败计数：{run.failure_count}\n"
        f"- 下一步：{run.next_action or '无'}"
        f"{worktree}{blocked}{boundary}\n"
        f"- 等待任务：\n{wait_lines}\n"
        f"- 最近证据：\n{evidence_lines}"
    )


def format_run_list(runs: list[PursuitRun]) -> str:
    if not runs:
        return "当前没有目标追踪运行记录。"
    return "\n\n".join(format_run(run) for run in runs)


def _run_from_rows(
    row: sqlite3.Row,
    evidence_rows: list[sqlite3.Row],
    wait_rows: list[sqlite3.Row],
    boundary_row: sqlite3.Row | None = None,
) -> PursuitRun:
    return PursuitRun(
        id=row["id"],
        goal=row["goal"],
        status=PursuitRunStatus(row["status"]),
        phase=row["phase"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        iteration=row["iteration"],
        criteria_total=row["criteria_total"],
        criteria_verified=row["criteria_verified"],
        failure_count=row["failure_count"],
        blocked_reason=row["blocked_reason"],
        next_action=row["next_action"],
        worktree_name=row["worktree_name"],
        worktree_path=row["worktree_path"],
        waiting_on=[
            PursuitBackgroundWait(
                task_id=item["task_id"],
                action_id=item["action_id"],
                command=item["command"],
                created_at=item["created_at"],
            )
            for item in wait_rows
        ],
        evidence=[
            PursuitEvidence(
                kind=item["kind"],
                source=item["source"],
                summary=item["summary"],
                is_hard=bool(item["is_hard"]),
                timestamp=item["timestamp"],
            )
            for item in evidence_rows
        ],
        boundary_decision=(
            _boundary_decision_from_row(boundary_row)
            if boundary_row is not None
            else None
        ),
    )


def _boundary_decision_from_row(row: sqlite3.Row) -> PursuitBoundaryDecision:
    payload = str(row["payload_json"])
    expected = str(row["payload_sha256"])
    actual = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise PursuitStoreError("boundary decision payload digest 不一致。")
    decision = PursuitBoundaryDecision.model_validate_json(payload)
    if decision.decision_id != str(row["decision_id"]):
        raise PursuitStoreError("boundary decision identity 与 payload 不一致。")
    return decision


def _status_label(status: PursuitRunStatus) -> str:
    return {
        PursuitRunStatus.RUNNING: "运行中",
        PursuitRunStatus.WAITING: "等待中",
        PursuitRunStatus.BLOCKED: "已阻塞",
        PursuitRunStatus.COMPLETED: "已完成",
        PursuitRunStatus.FAILED: "失败",
        PursuitRunStatus.CANCELLED: "已取消",
        PursuitRunStatus.BUDGET_EXCEEDED: "预算耗尽",
    }[status]
