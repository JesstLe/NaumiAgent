"""Real Engine lifecycle coverage for HAR-06.5b2b."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.store import HarnessStore, resolve_harness_db_path
from naumi_agent.runtime.composition import create_agent_engine


@pytest.mark.asyncio
async def test_long_running_engine_starts_real_worker_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    engine = create_agent_engine(
        AppConfig(
            workspace_root=str(tmp_path),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                long_term_enabled=False,
                session_retention={
                    "periodic_enabled": True,
                    "interval_seconds": 0.05,
                    "max_empty_backoff_seconds": 0.1,
                    "max_runtime_seconds": 0.1,
                    "worker_lease_seconds": 1,
                    "standby_retry_seconds": 0.05,
                    "jitter_ratio": 0,
                },
            ),
        )
    )

    await engine.start_long_running_services()
    for _ in range(100):
        if engine.session_retention_worker_status()["pass_count"] >= 1:
            break
        await asyncio.sleep(0.01)
    running = engine.session_retention_worker_status()
    await engine.shutdown()

    assert running["configured_enabled"] is True
    assert running["pass_count"] >= 1
    assert running["lease_held"] is True
    assert engine.session_retention_worker_status()["state"] == "stopped"
    probe = HarnessStore(resolve_harness_db_path())
    assert await probe.acquire_retention_worker_lease(
        owner_id="post-shutdown-probe",
        now=datetime.now(UTC).isoformat(),
        lease_seconds=1,
    ) is True
    assert await probe.release_retention_worker_lease(
        owner_id="post-shutdown-probe"
    ) is True
