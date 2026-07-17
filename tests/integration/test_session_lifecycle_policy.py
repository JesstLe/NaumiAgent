"""Real Session Store adapter scenario for HAR-06 lifecycle policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.harness.retention import (
    LifecycleActor,
    LifecyclePolicy,
    decide_lifecycle_transition,
    policy_from_session_status,
)
from naumi_agent.memory.session import SessionStore


@pytest.mark.asyncio
async def test_real_archived_session_can_be_evaluated_without_mutation(
    tmp_path: Path,
) -> None:
    store = SessionStore(
        MemoryConfig(session_db_path=str(tmp_path / "sessions" / "sessions.db"))
    )
    try:
        session = await store.create_session(title="待保留检查")
        assert await store.archive(session.id) is True
        archived = await store.load(session.id)
        assert archived is not None

        current = policy_from_session_status(archived.status)
        decision = decide_lifecycle_transition(
            current,
            LifecyclePolicy.DELETE,
            actor=LifecycleActor.RETENTION_WORKER,
        )

        assert current is LifecyclePolicy.ARCHIVE
        assert decision.allowed is True
        assert decision.automatic_cleanup_allowed is True
        persisted = await store.load(session.id)
        assert persisted is not None
        assert persisted.status == "archived"
    finally:
        await store.close()
