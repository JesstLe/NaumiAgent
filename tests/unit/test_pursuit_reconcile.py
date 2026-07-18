from __future__ import annotations

from naumi_agent.background.models import BackgroundStatus, BackgroundTask
from naumi_agent.background.store import BackgroundTaskStore
from naumi_agent.orchestrator.pursuit_action_ledger import (
    PursuitActionRecord,
    PursuitActionState,
    canonical_action_arguments,
    make_action_key,
)
from naumi_agent.orchestrator.pursuit_reconcile import (
    ReconcileDisposition,
    ReconcileReason,
    decide_background_reconcile,
)


class _BackgroundSource:
    def __init__(
        self,
        store: BackgroundTaskStore,
        *,
        managed_ids: set[str] | None = None,
    ) -> None:
        self.store = store
        self.managed_ids = managed_ids or set()

    def get(self, task_id: str) -> BackgroundTask | None:
        return self.store.get(task_id)

    def get_by_idempotency_key(self, key: str) -> BackgroundTask | None:
        return self.store.get_by_idempotency_key(key)

    def is_managed_active(self, task_id: str) -> bool:
        return task_id in self.managed_ids


class _BrokenManagedSource(_BackgroundSource):
    def is_managed_active(self, task_id: str) -> bool:
        raise RuntimeError(f"ownership unavailable for {task_id}")


def _action(
    *,
    state: PursuitActionState,
    tool_name: str = "background_run",
    action_id: str = "a1",
    task_id: str = "",
    iteration: int = 1,
) -> PursuitActionRecord:
    arguments = {"command": "echo ok", "cwd": "", "timeout_seconds": 1800}
    _, digest, size = canonical_action_arguments(arguments)
    action_key = make_action_key(
        run_id="pursuit_reconcile",
        iteration=iteration,
        action_id=action_id,
        tool_name=tool_name,
        arguments_sha256=digest,
    )
    return PursuitActionRecord(
        action_key=action_key,
        run_id="pursuit_reconcile",
        iteration=iteration,
        action_id=action_id,
        tool_name=tool_name,
        arguments_sha256=digest,
        arguments_size_bytes=size,
        argument_summary=f"tool={tool_name}; arguments_sha256={digest}",
        state=state,
        sequence=1 if state is PursuitActionState.PREPARED else 2,
        dispatch_token=action_key,
        background_task_id=task_id,
        result_status="",
        result_summary="",
        result_sha256="",
        prepared_at=100.0,
        updated_at=100.0,
    )


def _task(
    action: PursuitActionRecord,
    *,
    status: BackgroundStatus,
    task_id: str = "bg_0001",
    started_at: str = "1970-01-01T00:16:30",
    pid: int | None = 42,
    idempotency_key: str | None = None,
) -> BackgroundTask:
    return BackgroundTask(
        id=task_id,
        command="echo ok",
        cwd="/tmp",
        status=status,
        output_path=f"/tmp/{task_id}.log",
        started_at=started_at,
        pid=pid,
        idempotency_key=(
            action.dispatch_token if idempotency_key is None else idempotency_key
        ),
    )


def test_missing_current_iteration_ledger_is_legacy_unknown() -> None:
    decision = decide_background_reconcile(
        actions=[_action(state=PursuitActionState.COMPLETED, iteration=2)],
        iteration=1,
        background_tasks=None,
        now=1_000.0,
        pid_probe=lambda _: False,
    )

    assert decision.disposition is ReconcileDisposition.BLOCKED
    assert decision.reason is ReconcileReason.LEGACY_UNKNOWN


def test_prepared_and_terminal_actions_are_safe_to_continue() -> None:
    prepared = _action(state=PursuitActionState.PREPARED, action_id="a1")
    terminal = _action(state=PursuitActionState.COMPLETED, action_id="a2")

    decision = decide_background_reconcile(
        actions=[terminal, prepared],
        iteration=1,
        background_tasks=None,
        now=1_000.0,
        pid_probe=lambda _: False,
    )

    assert decision.disposition is ReconcileDisposition.SAFE_CONTINUE
    assert decision.reason is ReconcileReason.ALL_ACCOUNTED
    assert decision.abandon_action_keys == (prepared.action_key,)


def test_dispatched_sync_action_remains_ambiguous() -> None:
    action = _action(
        state=PursuitActionState.DISPATCHED,
        tool_name="bash_run",
    )

    decision = decide_background_reconcile(
        actions=[action],
        iteration=1,
        background_tasks=None,
        now=1_000.0,
        pid_probe=lambda _: False,
    )

    assert decision.disposition is ReconcileDisposition.BLOCKED
    assert decision.reason is ReconcileReason.NON_BACKGROUND_AMBIGUOUS


def test_background_missing_or_mismatched_identity_blocks(tmp_path) -> None:
    store = BackgroundTaskStore(tmp_path / "background")
    action = _action(state=PursuitActionState.DISPATCHED)

    missing = decide_background_reconcile(
        actions=[action],
        iteration=1,
        background_tasks=_BackgroundSource(store),
        now=1_000.0,
        pid_probe=lambda _: False,
    )
    mismatched_task = _task(
        action,
        status=BackgroundStatus.RUNNING,
        idempotency_key="pact_different-1",
    )
    store.save(mismatched_task)
    waiting_action = action.model_copy(update={
        "state": PursuitActionState.WAITING,
        "sequence": 3,
        "background_task_id": mismatched_task.id,
    })
    mismatch = decide_background_reconcile(
        actions=[waiting_action],
        iteration=1,
        background_tasks=_BackgroundSource(store, managed_ids={"bg_0001"}),
        now=1_000.0,
        pid_probe=lambda _: True,
    )

    assert missing.reason is ReconcileReason.BACKGROUND_TASK_MISSING
    assert mismatch.reason is ReconcileReason.BACKGROUND_IDENTITY_MISMATCH


def test_corrupted_background_store_becomes_typed_blocker(tmp_path) -> None:
    base_dir = tmp_path / "background"
    base_dir.mkdir()
    (base_dir / "tasks.json").write_text("{broken", encoding="utf-8")
    action = _action(state=PursuitActionState.DISPATCHED)

    decision = decide_background_reconcile(
        actions=[action],
        iteration=1,
        background_tasks=_BackgroundSource(BackgroundTaskStore(base_dir)),
        now=1_000.0,
        pid_probe=lambda _: False,
    )

    assert decision.disposition is ReconcileDisposition.BLOCKED
    assert decision.reason is ReconcileReason.BACKGROUND_STORE_ERROR
    assert "ValueError" in decision.summary


def test_running_ownership_read_failure_becomes_typed_blocker(tmp_path) -> None:
    action = _action(state=PursuitActionState.DISPATCHED)
    store = BackgroundTaskStore(tmp_path / "background")
    store.save(_task(action, status=BackgroundStatus.RUNNING))

    decision = decide_background_reconcile(
        actions=[action],
        iteration=1,
        background_tasks=_BrokenManagedSource(store),
        now=1_000.0,
        pid_probe=lambda _: True,
    )

    assert decision.disposition is ReconcileDisposition.BLOCKED
    assert decision.reason is ReconcileReason.BACKGROUND_STORE_ERROR
    assert "RuntimeError" in decision.summary


def test_fresh_preparing_waits_but_stale_preparing_blocks(tmp_path) -> None:
    action = _action(state=PursuitActionState.DISPATCHED)
    fresh_store = BackgroundTaskStore(tmp_path / "fresh")
    fresh_store.save(_task(
        action,
        status=BackgroundStatus.PREPARING,
        started_at="1970-01-01T00:16:30+00:00",
        pid=None,
    ))
    stale_store = BackgroundTaskStore(tmp_path / "stale")
    stale_store.save(_task(
        action,
        status=BackgroundStatus.PREPARING,
        started_at="1970-01-01T00:15:00+00:00",
        pid=None,
    ))

    fresh = decide_background_reconcile(
        actions=[action],
        iteration=1,
        background_tasks=_BackgroundSource(fresh_store),
        now=1_000.0,
        pid_probe=lambda _: False,
    )
    stale = decide_background_reconcile(
        actions=[action],
        iteration=1,
        background_tasks=_BackgroundSource(stale_store),
        now=1_000.0,
        pid_probe=lambda _: False,
    )
    assert fresh.disposition is ReconcileDisposition.WAITING
    assert fresh.waits[0].task_id == "bg_0001"
    assert stale.disposition is ReconcileDisposition.BLOCKED
    assert stale.reason is ReconcileReason.STALE_PREPARING


def test_running_requires_live_pid_and_terminal_creates_update(tmp_path) -> None:
    action = _action(state=PursuitActionState.DISPATCHED)
    running_store = BackgroundTaskStore(tmp_path / "running")
    running_store.save(_task(action, status=BackgroundStatus.RUNNING))
    terminal_store = BackgroundTaskStore(tmp_path / "terminal")
    terminal_store.save(_task(
        action,
        status=BackgroundStatus.COMPLETED,
        pid=None,
    ))

    live = decide_background_reconcile(
        actions=[action],
        iteration=1,
        background_tasks=_BackgroundSource(
            running_store,
            managed_ids={"bg_0001"},
        ),
        now=1_000.0,
        pid_probe=lambda pid: pid == 42,
    )
    stale = decide_background_reconcile(
        actions=[action],
        iteration=1,
        background_tasks=_BackgroundSource(running_store),
        now=1_000.0,
        pid_probe=lambda _: False,
    )
    orphan = decide_background_reconcile(
        actions=[action],
        iteration=1,
        background_tasks=_BackgroundSource(running_store),
        now=1_000.0,
        pid_probe=lambda _: True,
    )
    terminal = decide_background_reconcile(
        actions=[action],
        iteration=1,
        background_tasks=_BackgroundSource(terminal_store),
        now=1_000.0,
        pid_probe=lambda _: False,
    )

    assert live.disposition is ReconcileDisposition.WAITING
    assert stale.reason is ReconcileReason.STALE_RUNNING
    assert orphan.reason is ReconcileReason.ORPHAN_RUNNING
    assert terminal.disposition is ReconcileDisposition.SAFE_CONTINUE
    assert terminal.terminal_updates[0].succeeded is True
    assert terminal.terminal_updates[0].result_status == "completed"


def test_ambiguous_action_blocks_even_when_another_background_is_live(tmp_path) -> None:
    sync_action = _action(
        state=PursuitActionState.DISPATCHED,
        tool_name="bash_run",
        action_id="a1",
    )
    background_action = _action(
        state=PursuitActionState.DISPATCHED,
        action_id="a2",
    )
    store = BackgroundTaskStore(tmp_path / "background")
    store.save(_task(
        background_action,
        status=BackgroundStatus.RUNNING,
        pid=42,
    ))

    decision = decide_background_reconcile(
        actions=[background_action, sync_action],
        iteration=1,
        background_tasks=_BackgroundSource(store, managed_ids={"bg_0001"}),
        now=1_000.0,
        pid_probe=lambda _: True,
    )

    assert decision.disposition is ReconcileDisposition.BLOCKED
    assert decision.reason is ReconcileReason.NON_BACKGROUND_AMBIGUOUS
    assert len(decision.waits) == 1
