from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.heartbeat_runtime import RuntimeHeartbeatProducer
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore


class _Clock:
    def __init__(self) -> None:
        self.second = 0

    def now(self) -> str:
        value = f"2026-07-20T00:00:{self.second:02d}+00:00"
        self.second += 1
        return value


def _producer(
    store: object,
    workspace,
    *,
    clock: _Clock | None = None,
    auto_pulse: bool = False,
    sleep_provider: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_failure=None,
) -> RuntimeHeartbeatProducer:
    timer = clock or _Clock()
    return RuntimeHeartbeatProducer(
        port=store,
        workspace_root=workspace,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id="terminal-ui-runtime-1",
        instance_id="terminal-ui-instance-1",
        interval_seconds=1,
        timeout_seconds=3,
        now_provider=timer.now,
        sleep_provider=sleep_provider,
        on_failure=on_failure,
        auto_pulse=auto_pulse,
    )


@pytest.mark.asyncio
async def test_runtime_heartbeat_persists_full_graceful_lifecycle(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    producer = _producer(store, workspace)

    started = await producer.start()
    assert started.phase is HarnessHeartbeatPhase.RUNNING
    assert started.sequence == 2
    pulse = await producer.pulse_now()
    assert pulse.sequence == 3
    draining = await producer.begin_draining()
    assert draining is not None
    assert draining.phase is HarnessHeartbeatPhase.DRAINING
    assert draining.sequence == 4
    assert await producer.close()
    assert not await producer.close()

    reopened = await HarnessStore(store.db_path).get_heartbeat(
        workspace_root=workspace,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=producer.subject_id,
    )
    assert reopened is not None
    assert reopened.phase is HarnessHeartbeatPhase.STOPPED
    assert reopened.sequence == 5


@pytest.mark.asyncio
async def test_runtime_heartbeat_records_failed_shutdown(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    producer = _producer(store, workspace)

    await producer.start()
    await producer.begin_draining()
    assert await producer.fail()
    heartbeat = await store.get_heartbeat(
        workspace_root=workspace,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=producer.subject_id,
    )
    assert heartbeat is not None
    assert heartbeat.phase is HarnessHeartbeatPhase.FAILED
    assert heartbeat.detail_code == "runtime_shutdown_failed"


@pytest.mark.asyncio
async def test_runtime_heartbeat_reports_periodic_write_failure_once(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    durable = HarnessStore(tmp_path / "harness.db")
    failure_seen = asyncio.Event()
    failure_codes: list[str] = []

    class FailOncePort:
        def __init__(self) -> None:
            self.calls = 0

        async def record_heartbeat(self, **kwargs):
            self.calls += 1
            if self.calls == 3:
                raise OSError("simulated store outage")
            return await durable.record_heartbeat(**kwargs)

    async def immediate_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    async def on_failure(code: str) -> None:
        failure_codes.append(code)
        failure_seen.set()

    producer = _producer(
        FailOncePort(),
        workspace,
        auto_pulse=True,
        sleep_provider=immediate_sleep,
        on_failure=on_failure,
    )
    await producer.start()
    await asyncio.wait_for(failure_seen.wait(), timeout=1)
    assert producer.failure_code == "heartbeat_write_failed"
    assert failure_codes == ["heartbeat_write_failed"]
    assert await producer.close()


def test_runtime_heartbeat_rejects_invalid_cadence(tmp_path) -> None:
    with pytest.raises(ValueError, match="interval"):
        RuntimeHeartbeatProducer(
            port=object(),
            workspace_root=tmp_path,
            subject_kind=HarnessRunKind.RUNTIME,
            subject_id="runtime-1",
            instance_id="instance-1",
            interval_seconds=3,
            timeout_seconds=3,
            now_provider=_Clock().now,
        )
    with pytest.raises(ValueError, match="timeout"):
        RuntimeHeartbeatProducer(
            port=object(),
            workspace_root=tmp_path,
            subject_kind=HarnessRunKind.RUNTIME,
            subject_id="runtime-1",
            instance_id="instance-1",
            interval_seconds=1,
            timeout_seconds=True,
            now_provider=_Clock().now,
        )
