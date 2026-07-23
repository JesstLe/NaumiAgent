from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from naumi_agent.harness.sandbox_batch import (
    HarnessSandboxBatchAdmission,
    HarnessSandboxBatchCheckpoint,
    HarnessSandboxBatchCoordinator,
    HarnessSandboxBatchError,
)
from naumi_agent.harness.store import HarnessStore, HarnessStoreConflictError


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class _SampleReceipt(_FrozenModel):
    sample_index: int


class _BatchReceipt(_FrozenModel):
    persisted_samples: int


class _PermissionStore:
    async def get(self, _receipt_id: str):
        return SimpleNamespace(
            authorizes_execution=True,
            run_id="run-batch",
            delegated_tool_names=("bash_run",),
        )


class _Store:
    def __init__(self) -> None:
        self.records = []
        self.released = []
        self.epoch = 0

    async def acquire_run_lease(self, **kwargs):
        self.epoch += 1
        return SimpleNamespace(epoch=self.epoch, owner_id=kwargs["owner_id"])

    async def release_run_lease(self, **kwargs):
        self.released.append(kwargs)
        return SimpleNamespace(state="released")


class _RunGrantAuthority:
    def __init__(self, workspace_root: Path, permission_store: _PermissionStore) -> None:
        self._workspace_root = workspace_root
        self._permission_store = permission_store
        self.issued = []
        self.revoked = []

    async def issue(self, request, **_kwargs):
        self.issued.append(request)
        index = len(self.issued)
        return SimpleNamespace(
            contract=SimpleNamespace(
                grant_id=f"grant-{index}",
                grant_sha256=f"{index:x}" * 64,
            )
        )

    async def revoke(self, **kwargs):
        self.revoked.append(kwargs)


def _record(index: int):
    return SimpleNamespace(sample_index=index, result_sha256=f"{index + 1:x}" * 64)


def _coordinator(
    tmp_path: Path,
    store: _Store,
    permissions: _PermissionStore,
    grants: _RunGrantAuthority,
    *,
    token: str,
    admission: HarnessSandboxBatchAdmission | None = None,
    compatibility_scope: str = "harness",
) -> HarnessSandboxBatchCoordinator:
    return HarnessSandboxBatchCoordinator(
        workspace_root=tmp_path,
        store=store,  # type: ignore[arg-type]
        permission_store=permissions,  # type: ignore[arg-type]
        run_grant_authority=grants,  # type: ignore[arg-type]
        now=lambda: "2026-07-20T00:00:00+00:00",
        token=lambda: token,
        admission=admission,
        compatibility_scope=compatibility_scope,  # type: ignore[arg-type]
    )


def test_sandbox_batch_rejects_cross_workspace_authority(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    permissions = _PermissionStore()
    grants = _RunGrantAuthority(other, permissions)
    with pytest.raises(ValueError, match="workspace 不一致"):
        HarnessSandboxBatchCoordinator(
            workspace_root=tmp_path,
            store=_Store(),  # type: ignore[arg-type]
            permission_store=permissions,  # type: ignore[arg-type]
            run_grant_authority=grants,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("max_active", "max_queued"),
    [(0, 0), (33, 0), (1, -1), (1, 10_001), (True, 1), (1, False)],
)
def test_sandbox_batch_admission_rejects_invalid_capacity(
    max_active: int,
    max_queued: int,
) -> None:
    with pytest.raises(ValueError, match="容量"):
        HarnessSandboxBatchAdmission(
            max_active=max_active,
            max_queued=max_queued,
        )


@pytest.mark.asyncio
async def test_sandbox_batch_admission_bounds_queue_and_reclaims_cancelled_waiter(
) -> None:
    admission = HarnessSandboxBatchAdmission(max_active=1, max_queued=1)
    active_started = asyncio.Event()
    release_active = asyncio.Event()

    async def hold_active() -> None:
        async with admission.admit():
            active_started.set()
            await release_active.wait()

    active = asyncio.create_task(hold_active())
    await active_started.wait()
    queued = asyncio.create_task(hold_active())
    for _ in range(20):
        if admission.snapshot().queued == 1:
            break
        await asyncio.sleep(0)
    assert admission.snapshot().active == 1
    assert admission.snapshot().queued == 1

    with pytest.raises(
        HarnessSandboxBatchError,
        match="等待队列已满",
    ) as saturated:
        async with admission.admit():
            raise AssertionError("capacity exhaustion must not enter the batch")
    assert saturated.value.code == "sandbox_batch_capacity_exhausted"

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert admission.snapshot().queued == 0

    replacement = asyncio.create_task(hold_active())
    for _ in range(20):
        if admission.snapshot().queued == 1:
            break
        await asyncio.sleep(0)
    release_active.set()
    await asyncio.gather(active, replacement)
    assert admission.snapshot().active == 0
    assert admission.snapshot().queued == 0


@pytest.mark.asyncio
async def test_sandbox_batch_admission_rejects_self_waiting_nested_batch() -> None:
    admission = HarnessSandboxBatchAdmission(max_active=2, max_queued=2)
    async with admission.admit():
        with pytest.raises(HarnessSandboxBatchError) as nested:
            async with admission.admit():
                raise AssertionError("nested admission must fail closed")
    assert nested.value.code == "sandbox_batch_nested_admission"
    assert admission.snapshot().active == 0


@pytest.mark.asyncio
async def test_inherited_admission_context_expires_when_parent_batch_releases(
) -> None:
    admission = HarnessSandboxBatchAdmission(max_active=1, max_queued=1)
    parent_released = asyncio.Event()
    child_entered = asyncio.Event()

    async def delayed_child() -> None:
        await parent_released.wait()
        async with admission.admit():
            child_entered.set()

    async with admission.admit():
        child = asyncio.create_task(delayed_child())
    parent_released.set()
    await child

    assert child_entered.is_set()
    assert admission.snapshot().active == 0
    assert admission.snapshot().queued == 0


@pytest.mark.asyncio
async def test_durable_admission_cancellation_removes_waiter_across_instances(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    active_gate = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=1,
        store=HarnessStore(db_path),
        workspace_root=tmp_path,
        owner_id="runtime-a",
        lease_seconds=2,
        poll_interval_seconds=0.01,
        token=lambda: "a" * 32,
    )
    waiting_gate = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=1,
        store=HarnessStore(db_path),
        workspace_root=tmp_path,
        owner_id="runtime-b",
        lease_seconds=2,
        poll_interval_seconds=0.01,
        token=lambda: "b" * 32,
    )
    active_started = asyncio.Event()
    release_active = asyncio.Event()
    waiting_transitions = []

    async def hold_active() -> None:
        async with active_gate.admit(
            authority_key="a" * 64,
            lane="sandbox",
            requested_samples=5,
        ):
            active_started.set()
            await release_active.wait()

    async def wait_for_slot() -> None:
        async def capture(transition) -> None:
            waiting_transitions.append(transition)

        async with waiting_gate.admit(
            authority_key="b" * 64,
            lane="sandbox",
            requested_samples=5,
            on_transition=capture,
        ):
            pytest.fail("cancelled waiter must not enter")

    active = asyncio.create_task(hold_active())
    await active_started.wait()
    waiting = asyncio.create_task(wait_for_slot())
    for _ in range(100):
        snapshot = await waiting_gate.snapshot_durable()
        if snapshot.queued == 1:
            break
        await asyncio.sleep(0.01)
    assert snapshot.active == 1
    assert snapshot.queued == 1

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    snapshot = await waiting_gate.snapshot_durable()
    assert snapshot.active == 1
    assert snapshot.queued == 0
    assert [item.stage for item in waiting_transitions] == ["queued", "cancelled"]
    assert waiting_transitions[0].ticket.queue_position == 1
    assert waiting_transitions[-1].ticket.state == "cancelled"

    release_active.set()
    await active
    assert (await active_gate.snapshot_durable()).active == 0


@pytest.mark.asyncio
async def test_durable_admission_cancellation_during_enqueue_cleans_committed_ticket(
    tmp_path: Path,
) -> None:
    class _DelayedHarnessStore(HarnessStore):
        def __init__(self, db_path: Path) -> None:
            super().__init__(db_path)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def enqueue_sandbox_admission(self, **kwargs):
            self.started.set()
            await self.release.wait()
            return await super().enqueue_sandbox_admission(**kwargs)

    store = _DelayedHarnessStore(tmp_path / "harness.db")
    admission = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=0,
        store=store,
        workspace_root=tmp_path,
        owner_id="runtime-enqueue-cancel",
        lease_seconds=2,
        poll_interval_seconds=0.01,
        token=lambda: "d" * 32,
    )

    async def enter() -> None:
        async with admission.admit(
            authority_key="d" * 64,
            lane="sandbox",
            requested_samples=5,
        ):
            pytest.fail("cancelled enqueue must not enter")

    task = asyncio.create_task(enter())
    await store.started.wait()
    task.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = await admission.snapshot_durable()
    assert snapshot.active == 0
    assert snapshot.queued == 0


@pytest.mark.asyncio
async def test_durable_admission_fence_loss_cancels_active_body(
    tmp_path: Path,
) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    admission = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=0,
        store=store,
        workspace_root=tmp_path,
        owner_id="runtime-fence",
        lease_seconds=2,
        poll_interval_seconds=0.05,
        token=lambda: "c" * 32,
    )
    entered = asyncio.Event()

    async def execute() -> None:
        with pytest.raises(HarnessSandboxBatchError) as captured:
            async with admission.admit(
                authority_key="c" * 64,
                lane="sandbox",
                requested_samples=5,
            ):
                entered.set()
                await asyncio.sleep(1)
        assert captured.value.code == "sandbox_batch_admission_fence_lost"

    task = asyncio.create_task(execute())
    await entered.wait()
    await store.finish_sandbox_admission(
        workspace_root=tmp_path,
        ticket_id=f"hsadm_{'c' * 24}",
        owner_id=f"runtime-fence-{'c' * 16}",
        epoch=1,
        state="cancelled",
        terminal_code="external_fence",
        now=datetime.now(UTC).isoformat(),
    )
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_durable_admission_cancel_is_exact_audited_and_idempotent(
    tmp_path: Path,
) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    ticket = await store.enqueue_sandbox_admission(
        workspace_root=tmp_path,
        ticket_id=f"hsadm_{'e' * 24}",
        authority_key="e" * 64,
        lane="sandbox",
        requested_samples=5,
        owner_id="runtime-exact",
        now=datetime.now(UTC).isoformat(),
        lease_seconds=30,
        max_active=1,
        max_queued=1,
    )

    rejected, unchanged = await store.cancel_sandbox_admission(
        workspace_root=tmp_path,
        action_id=f"hsac_{'1' * 24}",
        ticket_id=ticket.ticket_id,
        authority_key=ticket.authority_key,
        epoch=ticket.epoch,
        expected_state="queued",
        actor_id="ui:test",
        reason="用户取消",
        now=datetime.now(UTC).isoformat(),
    )
    assert rejected.decision == "rejected"
    assert rejected.code == "sandbox_batch_cancel_state_changed"
    assert rejected.observed_state == "active"
    assert unchanged is not None and unchanged.state == "active"

    wrong_authority, _ = await store.cancel_sandbox_admission(
        workspace_root=tmp_path,
        action_id=f"hsac_{'6' * 24}",
        ticket_id=ticket.ticket_id,
        authority_key="0" * 64,
        epoch=ticket.epoch,
        expected_state="active",
        actor_id="ui:test",
        reason="错误 authority",
        now=datetime.now(UTC).isoformat(),
    )
    stale_epoch, _ = await store.cancel_sandbox_admission(
        workspace_root=tmp_path,
        action_id=f"hsac_{'7' * 24}",
        ticket_id=ticket.ticket_id,
        authority_key=ticket.authority_key,
        epoch=ticket.epoch + 1,
        expected_state="active",
        actor_id="ui:test",
        reason="错误 epoch",
        now=datetime.now(UTC).isoformat(),
    )
    missing, missing_ticket = await store.cancel_sandbox_admission(
        workspace_root=tmp_path,
        action_id=f"hsac_{'8' * 24}",
        ticket_id=f"hsadm_{'0' * 24}",
        authority_key=ticket.authority_key,
        epoch=ticket.epoch,
        expected_state="active",
        actor_id="ui:test",
        reason="不存在 ticket",
        now=datetime.now(UTC).isoformat(),
    )
    assert wrong_authority.code == "sandbox_batch_cancel_authority_mismatch"
    assert stale_epoch.code == "sandbox_batch_cancel_epoch_mismatch"
    assert missing.code == "sandbox_batch_cancel_ticket_not_found"
    assert missing_ticket is None

    accepted, cancelled = await store.cancel_sandbox_admission(
        workspace_root=tmp_path,
        action_id=f"hsac_{'2' * 24}",
        ticket_id=ticket.ticket_id,
        authority_key=ticket.authority_key,
        epoch=ticket.epoch,
        expected_state="active",
        actor_id="ui:test",
        reason="用户取消",
        now=datetime.now(UTC).isoformat(),
    )
    replayed, replayed_ticket = await store.cancel_sandbox_admission(
        workspace_root=tmp_path,
        action_id=f"hsac_{'2' * 24}",
        ticket_id=ticket.ticket_id,
        authority_key=ticket.authority_key,
        epoch=ticket.epoch,
        expected_state="active",
        actor_id="ui:test",
        reason="用户取消",
        now=datetime.now(UTC).isoformat(),
    )
    assert accepted.decision == "accepted"
    assert accepted.code == "sandbox_batch_cancelled_by_user"
    assert accepted.receipt_sha256 == replayed.receipt_sha256
    assert cancelled is not None and cancelled.state == "cancelled"
    assert replayed_ticket is not None and replayed_ticket.state == "cancelled"
    with pytest.raises(HarnessStoreConflictError, match="不同请求"):
        await store.cancel_sandbox_admission(
            workspace_root=tmp_path,
            action_id=f"hsac_{'2' * 24}",
            ticket_id=ticket.ticket_id,
            authority_key=ticket.authority_key,
            epoch=ticket.epoch,
            expected_state="active",
            actor_id="ui:test",
            reason="复用 action 但修改原因",
            now=datetime.now(UTC).isoformat(),
        )
    terminal, terminal_ticket = await store.cancel_sandbox_admission(
        workspace_root=tmp_path,
        action_id=f"hsac_{'9' * 24}",
        ticket_id=ticket.ticket_id,
        authority_key=ticket.authority_key,
        epoch=ticket.epoch,
        expected_state="active",
        actor_id="ui:test",
        reason="终态重复取消",
        now=datetime.now(UTC).isoformat(),
    )
    assert terminal.code == "sandbox_batch_cancel_already_terminal"
    assert terminal_ticket is not None and terminal_ticket.state == "cancelled"


@pytest.mark.asyncio
async def test_durable_admission_cancel_interrupts_local_active_owner(
    tmp_path: Path,
) -> None:
    admission = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=0,
        store=HarnessStore(tmp_path / "harness.db"),
        workspace_root=tmp_path,
        owner_id="runtime-local-cancel",
        lease_seconds=30,
        token=lambda: "f" * 32,
    )
    entered = asyncio.Event()
    transitions = []

    async def execute() -> None:
        async def capture(transition) -> None:
            transitions.append(transition)

        async with admission.admit(
            authority_key="f" * 64,
            lane="sandbox",
            requested_samples=5,
            on_transition=capture,
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(execute())
    await entered.wait()
    receipt, ticket = await admission.cancel(
        action_id=f"hsac_{'3' * 24}",
        ticket_id=f"hsadm_{'f' * 24}",
        authority_key="f" * 64,
        epoch=1,
        expected_state="active",
        actor_id="ui:test",
        reason="停止当前批次",
    )
    assert receipt.decision == "accepted"
    assert ticket is not None and ticket.state == "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert transitions[-1].stage == "cancelled"
    assert (await admission.snapshot_durable()).active == 0


@pytest.mark.asyncio
async def test_durable_admission_cancel_is_observed_by_another_runtime(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "harness.db"
    owner = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=0,
        store=HarnessStore(db_path),
        workspace_root=tmp_path,
        owner_id="runtime-remote-owner",
        lease_seconds=3,
        token=lambda: "7" * 32,
    )
    controller = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=0,
        store=HarnessStore(db_path),
        workspace_root=tmp_path,
        owner_id="runtime-remote-controller",
        lease_seconds=3,
        token=lambda: "8" * 32,
    )
    entered = asyncio.Event()

    async def execute() -> None:
        with pytest.raises(HarnessSandboxBatchError) as captured:
            async with owner.admit(
                authority_key="7" * 64,
                lane="sandbox",
                requested_samples=5,
            ):
                entered.set()
                await asyncio.Event().wait()
        assert captured.value.code == "sandbox_batch_admission_fence_lost"

    task = asyncio.create_task(execute())
    await entered.wait()
    receipt, _ticket = await controller.cancel(
        action_id=f"hsac_{'4' * 24}",
        ticket_id=f"hsadm_{'7' * 24}",
        authority_key="7" * 64,
        epoch=1,
        expected_state="active",
        actor_id="ui:remote",
        reason="跨运行时取消",
    )
    assert receipt.decision == "accepted"
    await asyncio.wait_for(task, timeout=1.5)
    assert (await owner.snapshot_durable()).active == 0


@pytest.mark.asyncio
async def test_active_cancel_revokes_run_grant_and_releases_runtime_lease(
    tmp_path: Path,
) -> None:
    runtime_store = _Store()
    permissions = _PermissionStore()
    grants = _RunGrantAuthority(tmp_path, permissions)
    admission = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=0,
        store=HarnessStore(tmp_path / "harness.db"),
        workspace_root=tmp_path,
        owner_id="runtime-cleanup",
        lease_seconds=30,
        token=lambda: "9" * 32,
    )
    executing = asyncio.Event()

    async def load_records():
        return tuple(runtime_store.records)

    async def validate_prefix(records):
        return [_SampleReceipt(sample_index=item.sample_index) for item in records]

    async def execute_sample(_index, _authority):
        executing.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled execution must not resume")

    task = asyncio.create_task(
        _coordinator(
            tmp_path,
            runtime_store,
            permissions,
            grants,
            token="a" * 32,
            admission=admission,
        ).execute(
            phase="sandbox",
            authority_key="9" * 64,
            parent_receipt_id="parent",
            requested_samples=5,
            max_total_duration_seconds=60,
            load_records=load_records,
            validate_existing_prefix=validate_prefix,
            validate_run_evidence=lambda _records: None,
            execute_sample=execute_sample,
            build_receipt=lambda records, _receipts: _BatchReceipt(
                persisted_samples=len(records)
            ),
        )
    )
    await asyncio.wait_for(executing.wait(), timeout=2)
    receipt, _ticket = await admission.cancel(
        action_id=f"hsac_{'5' * 24}",
        ticket_id=f"hsadm_{'9' * 24}",
        authority_key="9" * 64,
        epoch=1,
        expected_state="active",
        actor_id="ui:test",
        reason="验证执行权威清理",
    )
    assert receipt.decision == "accepted"
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert grants.revoked[-1]["reason"] == "sandbox_batch_finished"
    assert len(runtime_store.released) == 1
    assert (await admission.snapshot_durable()).active == 0


@pytest.mark.asyncio
async def test_completed_sandbox_batch_bypasses_saturated_admission(
    tmp_path: Path,
) -> None:
    store = _Store()
    store.records.extend(_record(index) for index in range(5))
    permissions = _PermissionStore()
    grants = _RunGrantAuthority(tmp_path, permissions)
    admission = HarnessSandboxBatchAdmission(max_active=1, max_queued=0)

    async def load_records():
        return tuple(store.records)

    async def validate_prefix(records):
        return [_SampleReceipt(sample_index=item.sample_index) for item in records]

    checkpoints: list[HarnessSandboxBatchCheckpoint] = []

    async def capture(checkpoint: HarnessSandboxBatchCheckpoint) -> None:
        checkpoints.append(checkpoint)

    async with admission.admit():
        receipt = await _coordinator(
            tmp_path,
            store,
            permissions,
            grants,
            token="c" * 32,
            admission=admission,
        ).execute(
            phase="sandbox",
            authority_key="c" * 64,
            parent_receipt_id="parent",
            requested_samples=5,
            max_total_duration_seconds=60,
            load_records=load_records,
            validate_existing_prefix=validate_prefix,
            validate_run_evidence=lambda _records: None,
            execute_sample=lambda _index, _authority: pytest.fail(
                "completed batch must not execute a sample"
            ),
            build_receipt=lambda records, _receipts: _BatchReceipt(
                persisted_samples=len(records)
            ),
            on_progress=capture,
        )

    assert receipt.persisted_samples == 5
    assert not grants.issued
    assert len(checkpoints) == 1
    assert checkpoints[0].lane == "sandbox"


@pytest.mark.asyncio
async def test_durable_coordinator_projects_admitted_and_released_ticket_facts(
    tmp_path: Path,
) -> None:
    store = _Store()
    permissions = _PermissionStore()
    grants = _RunGrantAuthority(tmp_path, permissions)
    admission = HarnessSandboxBatchAdmission(
        max_active=1,
        max_queued=1,
        store=HarnessStore(tmp_path / "harness.db"),
        workspace_root=tmp_path,
        owner_id="runtime-progress",
        lease_seconds=2,
        poll_interval_seconds=0.01,
        token=lambda: "e" * 32,
    )
    checkpoints: list[HarnessSandboxBatchCheckpoint] = []

    async def load_records():
        return tuple(store.records)

    async def validate_prefix(records):
        return [_SampleReceipt(sample_index=item.sample_index) for item in records]

    async def execute_sample(index, _authority):
        store.records.append(_record(index))
        return _SampleReceipt(sample_index=index)

    async def capture(checkpoint: HarnessSandboxBatchCheckpoint) -> None:
        checkpoints.append(checkpoint)

    receipt = await _coordinator(
        tmp_path,
        store,
        permissions,
        grants,
        token="f" * 32,
        admission=admission,
    ).execute(
        phase="sandbox",
        authority_key="e" * 64,
        parent_receipt_id="parent",
        requested_samples=5,
        max_total_duration_seconds=60,
        load_records=load_records,
        validate_existing_prefix=validate_prefix,
        validate_run_evidence=lambda _records: None,
        execute_sample=execute_sample,
        build_receipt=lambda records, _receipts: _BatchReceipt(
            persisted_samples=len(records)
        ),
        on_progress=capture,
    )

    assert receipt.persisted_samples == 5
    assert [item.stage for item in checkpoints] == [
        "admitted",
        "recovering",
        "acquiring",
        *("executing" for _ in range(5)),
        "completed",
    ]
    assert {item.admission_ticket_id for item in checkpoints} == {
        f"hsadm_{'e' * 24}"
    }
    assert all(item.admission_epoch == 1 for item in checkpoints)
    assert all(item.admission_state == "active" for item in checkpoints[:-1])
    assert checkpoints[-1].admission_state == "completed"
    assert checkpoints[-1].active_count == 0
    assert checkpoints[-1].code == ""
    with pytest.raises(ValueError, match="digest"):
        HarnessSandboxBatchCheckpoint.model_validate(
            checkpoints[-1].model_copy(
                update={"active_count": 1}
            ).model_dump(mode="json")
        )


@pytest.mark.asyncio
async def test_evolution_compatibility_rejects_native_sandbox_lane_before_io(
    tmp_path: Path,
) -> None:
    store = _Store()
    permissions = _PermissionStore()
    grants = _RunGrantAuthority(tmp_path, permissions)

    async def unexpected_io(*_args, **_kwargs):
        pytest.fail("invalid compatibility lane must fail before IO")

    with pytest.raises(HarnessSandboxBatchError) as captured:
        await _coordinator(
            tmp_path,
            store,
            permissions,
            grants,
            token="e" * 32,
            compatibility_scope="evolution",
        ).execute(
            phase="sandbox",
            authority_key="e" * 64,
            parent_receipt_id="parent",
            requested_samples=5,
            max_total_duration_seconds=60,
            load_records=unexpected_io,
            validate_existing_prefix=unexpected_io,
            validate_run_evidence=lambda _records: None,
            execute_sample=unexpected_io,
            build_receipt=lambda _records, _receipts: pytest.fail(
                "invalid compatibility lane must not build a receipt"
            ),
        )

    assert captured.value.code == "cohort_phase_invalid"
    assert not grants.issued


@pytest.mark.asyncio
async def test_sandbox_batch_emits_partial_checkpoint_and_resumes(
    tmp_path: Path,
) -> None:
    store = _Store()
    permissions = _PermissionStore()
    grants = _RunGrantAuthority(tmp_path, permissions)
    interrupted_progress: list[HarnessSandboxBatchCheckpoint] = []

    async def load_records():
        return tuple(store.records)

    async def validate_prefix(records):
        return [_SampleReceipt(sample_index=item.sample_index) for item in records]

    def validate_evidence(_records) -> None:
        return None

    async def interrupted_sample(index, authority):
        assert authority.run_id == "run-batch"
        if index == 1:
            raise RuntimeError("simulated interruption")
        store.records.append(_record(index))
        return _SampleReceipt(sample_index=index)

    def build_receipt(records, _receipts):
        return _BatchReceipt(persisted_samples=len(records))

    async def capture(checkpoint):
        if checkpoint.stage == "failed":
            assert grants.revoked
            assert store.released
        interrupted_progress.append(checkpoint)

    interrupted_coordinator = _coordinator(
        tmp_path,
        store,
        permissions,
        grants,
        token="a" * 32,
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await interrupted_coordinator.execute(
            phase="adversarial",
            authority_key="a" * 64,
            parent_receipt_id="parent",
            requested_samples=5,
            max_total_duration_seconds=60,
            load_records=load_records,
            validate_existing_prefix=validate_prefix,
            validate_run_evidence=validate_evidence,
            execute_sample=interrupted_sample,
            build_receipt=build_receipt,
            on_progress=capture,
        )

    assert [item.stage for item in interrupted_progress] == [
        "recovering",
        "acquiring",
        "executing",
        "failed",
    ]
    partial = interrupted_progress[-1]
    assert partial.persisted_samples == 1
    assert partial.sample_result_sha256 == ("1" * 64,)
    assert partial.code == "sample_execution_interrupted"
    with pytest.raises(ValueError):
        HarnessSandboxBatchCheckpoint.model_validate(
            partial.model_copy(update={"persisted_samples": 2}).model_dump(mode="json")
        )
    assert grants.revoked[-1]["reason"] == "sandbox_batch_finished"
    assert len(store.released) == 1
    assert interrupted_coordinator.admission.snapshot().active == 0
    assert interrupted_coordinator.admission.snapshot().queued == 0

    resumed_progress: list[HarnessSandboxBatchCheckpoint] = []

    async def remaining_sample(index, _authority):
        store.records.append(_record(index))
        return _SampleReceipt(sample_index=index)

    async def noisy_observer(checkpoint):
        resumed_progress.append(checkpoint)
        raise RuntimeError("observer unavailable")

    receipt = await _coordinator(
        tmp_path,
        store,
        permissions,
        grants,
        token="b" * 32,
    ).execute(
        phase="adversarial",
        authority_key="a" * 64,
        parent_receipt_id="parent",
        requested_samples=5,
        max_total_duration_seconds=60,
        load_records=load_records,
        validate_existing_prefix=validate_prefix,
        validate_run_evidence=validate_evidence,
        execute_sample=remaining_sample,
        build_receipt=build_receipt,
        on_progress=noisy_observer,
    )

    assert receipt.persisted_samples == 5
    assert [item.persisted_samples for item in resumed_progress] == [1, 1, 2, 3, 4, 5, 5]
    assert resumed_progress[-1].stage == "completed"
    assert len(store.released) == 2
    assert len(grants.revoked) == 2
