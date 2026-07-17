"""Transactional forward migrations for catalogued SQLite stores."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from naumi_agent.persistence.store_catalog import (
    StorageKind,
    StoreDefinition,
    VersionStrategy,
)

MigrationApply = Callable[[sqlite3.Connection], None]
ProgressCallback = Callable[["MigrationProgress"], None]
CancellationCheck = Callable[[], bool]

_LOGGER = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """Base class for safe, user-visible migration failures."""


class MigrationExecutionError(MigrationError):
    """Raised when a migration cannot be planned or applied safely."""


class MigrationLockedError(MigrationError):
    """Raised when another process owns the SQLite migration lock."""


class MigrationCancelledError(MigrationError):
    """Raised when cancellation is observed between migration steps."""


@dataclass(frozen=True, slots=True)
class MigrationStep:
    """One trusted, adjacent schema transition for one physical store."""

    store_id: str
    from_version: int
    to_version: int
    description: str
    apply: MigrationApply
    estimate_query: str | None = None
    irreversible: bool = False

    def __post_init__(self) -> None:
        if not self.store_id.strip():
            raise ValueError("迁移步骤必须声明 store_id。")
        if (
            isinstance(self.from_version, bool)
            or not isinstance(self.from_version, int)
            or self.from_version < 0
        ):
            raise ValueError("迁移起始版本必须是非负整数。")
        if isinstance(self.to_version, bool) or not isinstance(self.to_version, int):
            raise ValueError("迁移目标版本必须是整数。")
        if self.to_version != self.from_version + 1:
            raise ValueError("迁移步骤必须按相邻版本向前推进。")
        if not self.description.strip():
            raise ValueError("迁移步骤必须提供说明。")
        if not callable(self.apply):
            raise ValueError("迁移步骤必须提供可调用的 apply。")
        if self.estimate_query is not None and not self.estimate_query.strip():
            raise ValueError("estimate_query 不能是空字符串。")


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """Read-only migration preview for one Store."""

    store_id: str
    path: Path
    current_version: int
    target_version: int
    steps: tuple[MigrationStep, ...]
    size_bytes: int
    estimated_rows: int
    irreversible_steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MigrationProgress:
    """Structured progress emitted at transaction boundaries and steps."""

    phase: Literal["started", "step", "completed"]
    store_id: str
    current_version: int
    target_version: int
    completed_steps: int
    total_steps: int
    step_from: int | None = None
    step_to: int | None = None


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Outcome of one atomic migration attempt."""

    store_id: str
    path: Path
    from_version: int
    to_version: int
    changed: bool
    applied_versions: tuple[int, ...]


class MigrationRegistry:
    """Validated in-process registry of contiguous forward migrations."""

    def __init__(self, steps: Iterable[MigrationStep] = ()) -> None:
        grouped: dict[str, list[MigrationStep]] = {}
        for step in steps:
            grouped.setdefault(step.store_id, []).append(step)

        self._steps: dict[str, tuple[MigrationStep, ...]] = {}
        for store_id, candidates in grouped.items():
            ordered = sorted(candidates, key=lambda item: item.from_version)
            starts = [item.from_version for item in ordered]
            if len(starts) != len(set(starts)):
                raise ValueError(f"Store {store_id} 存在重复的迁移起始版本。")
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if previous.to_version != current.from_version:
                    raise ValueError(f"Store {store_id} 的迁移版本不连续。")
            self._steps[store_id] = tuple(ordered)

    def path(
        self,
        store_id: str,
        current_version: int,
        target_version: int,
    ) -> tuple[MigrationStep, ...]:
        if current_version > target_version:
            raise ValueError("当前版本不能高于目标版本。")
        if current_version == target_version:
            return ()

        by_start = {
            step.from_version: step for step in self._steps.get(store_id, ())
        }
        selected: list[MigrationStep] = []
        cursor = current_version
        while cursor < target_version:
            step = by_start.get(cursor)
            if step is None or step.to_version > target_version:
                raise ValueError(
                    f"Store {store_id} 缺少 {cursor} -> {cursor + 1} 的迁移步骤。"
                )
            selected.append(step)
            cursor = step.to_version
        return tuple(selected)


class MigrationRunner:
    """Plan and atomically apply one Store's SQLite migrations."""

    def __init__(
        self,
        registry: MigrationRegistry,
        *,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if (
            isinstance(busy_timeout_seconds, bool)
            or not isinstance(busy_timeout_seconds, int | float)
            or busy_timeout_seconds <= 0
        ):
            raise ValueError("busy_timeout_seconds 必须大于 0。")
        self._registry = registry
        self._busy_timeout_seconds = float(busy_timeout_seconds)

    def plan(self, definition: StoreDefinition) -> MigrationPlan:
        """Inspect a Store in read-only mode without creating or changing it."""
        path, target_version = self._validate_definition(definition)
        current_version, estimated_rows = self._inspect_read_only(
            definition,
            path,
            target_version,
        )
        try:
            steps = self._registry.path(
                definition.store_id,
                current_version,
                target_version,
            )
        except ValueError as exc:
            raise MigrationExecutionError(str(exc)) from exc

        if steps:
            estimated_rows = self._estimate_rows(path, definition.store_id, steps)
        return MigrationPlan(
            store_id=definition.store_id,
            path=path,
            current_version=current_version,
            target_version=target_version,
            steps=steps,
            size_bytes=path.stat().st_size,
            estimated_rows=estimated_rows,
            irreversible_steps=tuple(
                f"{step.from_version}->{step.to_version}"
                for step in steps
                if step.irreversible
            ),
        )

    def apply(
        self,
        definition: StoreDefinition,
        *,
        progress: ProgressCallback | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> MigrationResult:
        """Apply all required steps under one exclusive SQLite transaction."""
        plan = self.plan(definition)
        if not plan.steps:
            return MigrationResult(
                store_id=plan.store_id,
                path=plan.path,
                from_version=plan.current_version,
                to_version=plan.target_version,
                changed=False,
                applied_versions=(),
            )

        connection: sqlite3.Connection | None = None
        active_step: MigrationStep | None = None
        try:
            connection = sqlite3.connect(
                plan.path,
                timeout=self._busy_timeout_seconds,
                isolation_level=None,
            )
            connection.execute(
                f"PRAGMA busy_timeout = {round(self._busy_timeout_seconds * 1000)}"
            )
            connection.execute("BEGIN EXCLUSIVE")
            locked_version = self._read_user_version(connection)
            if locked_version != plan.current_version:
                raise MigrationExecutionError(
                    f"Store {plan.store_id} 在迁移前版本已变化，请重新预检。"
                )

            self._emit(
                progress,
                MigrationProgress(
                    phase="started",
                    store_id=plan.store_id,
                    current_version=plan.current_version,
                    target_version=plan.target_version,
                    completed_steps=0,
                    total_steps=len(plan.steps),
                ),
            )
            applied: list[int] = []
            for index, step in enumerate(plan.steps):
                if cancelled is not None and cancelled():
                    raise MigrationCancelledError(
                        f"Store {plan.store_id} 迁移已取消，全部改动已回滚。"
                    )
                active_step = step
                connection.set_authorizer(self._migration_authorizer)
                try:
                    step.apply(connection)
                finally:
                    connection.set_authorizer(None)
                if not connection.in_transaction:
                    raise MigrationExecutionError(
                        "迁移步骤提前结束了事务；禁止在 apply 中提交或使用 executescript。"
                    )
                connection.execute(f"PRAGMA user_version = {step.to_version}")
                applied.append(step.to_version)
                self._emit(
                    progress,
                    MigrationProgress(
                        phase="step",
                        store_id=plan.store_id,
                        current_version=plan.current_version,
                        target_version=plan.target_version,
                        completed_steps=index + 1,
                        total_steps=len(plan.steps),
                        step_from=step.from_version,
                        step_to=step.to_version,
                    ),
                )
            connection.commit()
            self._emit(
                progress,
                MigrationProgress(
                    phase="completed",
                    store_id=plan.store_id,
                    current_version=plan.current_version,
                    target_version=plan.target_version,
                    completed_steps=len(plan.steps),
                    total_steps=len(plan.steps),
                ),
            )
            return MigrationResult(
                store_id=plan.store_id,
                path=plan.path,
                from_version=plan.current_version,
                to_version=plan.target_version,
                changed=True,
                applied_versions=tuple(applied),
            )
        except MigrationCancelledError:
            self._rollback(connection)
            raise
        except MigrationLockedError:
            self._rollback(connection)
            raise
        except sqlite3.OperationalError as exc:
            self._rollback(connection)
            if self._is_locked(exc):
                raise MigrationLockedError(
                    f"Store {plan.store_id} 正被其他进程使用，请稍后重试。"
                ) from exc
            raise self._step_failure(plan.store_id, active_step) from exc
        except MigrationExecutionError:
            self._rollback(connection)
            raise
        except Exception as exc:
            self._rollback(connection)
            raise self._step_failure(plan.store_id, active_step) from exc
        finally:
            if connection is not None:
                connection.close()

    def _validate_definition(self, definition: StoreDefinition) -> tuple[Path, int]:
        if definition.kind is not StorageKind.SQLITE:
            raise MigrationExecutionError("迁移运行器当前仅支持 SQLite Store。")
        if definition.version_strategy is not VersionStrategy.SQLITE_USER_VERSION:
            raise MigrationExecutionError(
                "SQLite Store 必须使用 sqlite_user_version 版本策略。"
            )
        target = definition.supported_schema_version
        if isinstance(target, bool) or not isinstance(target, int) or target < 0:
            raise MigrationExecutionError("Store 必须声明非负整数目标版本。")

        path = definition.path.expanduser().resolve()
        if not path.exists():
            raise MigrationExecutionError(
                f"Store {definition.store_id} 不存在，预检不会自动创建文件。"
            )
        if not path.is_file():
            raise MigrationExecutionError(f"Store {definition.store_id} 不是普通文件。")
        return path, target

    def _inspect_read_only(
        self,
        definition: StoreDefinition,
        path: Path,
        target_version: int,
    ) -> tuple[int, int]:
        try:
            with closing(self._read_only_connection(path)) as connection:
                version = self._read_user_version(connection)
        except sqlite3.OperationalError as exc:
            if self._is_locked(exc):
                raise MigrationLockedError(
                    f"Store {definition.store_id} 正被其他进程使用，请稍后重试。"
                ) from exc
            raise MigrationExecutionError(
                f"Store {definition.store_id} 无法读取，可能已损坏或无权限。"
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise MigrationExecutionError(
                f"Store {definition.store_id} 无法读取，可能已损坏或无权限。"
            ) from exc
        if version > target_version:
            raise MigrationExecutionError(
                f"Store {definition.store_id} 的版本 {version} 高于当前支持版本 "
                f"{target_version}。"
            )
        return version, 0

    def _estimate_rows(
        self,
        path: Path,
        store_id: str,
        steps: tuple[MigrationStep, ...],
    ) -> int:
        total = 0
        try:
            with closing(self._read_only_connection(path)) as connection:
                for step in steps:
                    if step.estimate_query is None:
                        continue
                    row = connection.execute(step.estimate_query).fetchone()
                    if (
                        row is None
                        or len(row) != 1
                        or isinstance(row[0], bool)
                        or not isinstance(row[0], int)
                        or row[0] < 0
                    ):
                        raise MigrationExecutionError(
                            f"Store {store_id} 的迁移规模估算未返回非负整数。"
                        )
                    total += row[0]
        except MigrationExecutionError:
            raise
        except sqlite3.DatabaseError as exc:
            raise MigrationExecutionError(
                f"Store {store_id} 无法完成迁移规模估算。"
            ) from exc
        return total

    def _read_only_connection(self, path: Path) -> sqlite3.Connection:
        uri = f"{path.as_uri()}?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=self._busy_timeout_seconds,
            isolation_level=None,
        )
        connection.execute(
            f"PRAGMA busy_timeout = {round(self._busy_timeout_seconds * 1000)}"
        )
        connection.execute("PRAGMA query_only = ON")
        return connection

    @staticmethod
    def _read_user_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("PRAGMA user_version").fetchone()
        if row is None or len(row) != 1 or not isinstance(row[0], int):
            raise MigrationExecutionError("SQLite Store 未返回有效的 user_version。")
        return row[0]

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        event: MigrationProgress,
    ) -> None:
        if callback is not None:
            try:
                callback(event)
            except Exception:
                _LOGGER.warning(
                    "Migration progress callback failed for store %s at phase %s",
                    event.store_id,
                    event.phase,
                    exc_info=True,
                )

    @staticmethod
    def _migration_authorizer(
        action_code: int,
        _arg1: str | None,
        _arg2: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        denied = {
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_TRANSACTION,
        }
        return sqlite3.SQLITE_DENY if action_code in denied else sqlite3.SQLITE_OK

    @staticmethod
    def _rollback(connection: sqlite3.Connection | None) -> None:
        if connection is not None and connection.in_transaction:
            connection.rollback()

    @staticmethod
    def _is_locked(error: sqlite3.Error) -> bool:
        message = str(error).lower()
        return "locked" in message or "busy" in message

    @staticmethod
    def _step_failure(
        store_id: str,
        step: MigrationStep | None,
    ) -> MigrationExecutionError:
        if step is None:
            return MigrationExecutionError(
                f"Store {store_id} 无法启动迁移，未写入任何变更。"
            )
        return MigrationExecutionError(
            f"Store {store_id} 迁移步骤 {step.from_version} -> "
            f"{step.to_version} 失败，全部改动已回滚。"
        )


__all__ = [
    "MigrationCancelledError",
    "MigrationError",
    "MigrationExecutionError",
    "MigrationLockedError",
    "MigrationPlan",
    "MigrationProgress",
    "MigrationRegistry",
    "MigrationResult",
    "MigrationRunner",
    "MigrationStep",
]
