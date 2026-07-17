"""Session persistence port contract and Engine injection tests."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.coordinator import (
    ReconciliationCoordinatorOutcome,
    ReconciliationCoordinatorResult,
)
from naumi_agent.harness.retention import LifecycleActor
from naumi_agent.harness.retention_planner import (
    SessionRetentionPolicy,
    SessionRetentionPreview,
    SessionRetentionReason,
    SessionRetentionSelection,
)
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.model.router import StreamChunk, TokenUsage
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.ports.session import SessionPort

EXPECTED_SESSION_PORT_METHODS = {
    "archive",
    "close",
    "create_session",
    "delete",
    "list_sessions",
    "load",
    "save",
}


class _IncompleteSessionPort:
    """Deliberately omits close so runtime validation must reject it."""

    async def create_session(self) -> object:
        return object()

    async def save(self, session: object) -> None:
        del session

    async def load(self, session_id: str) -> object | None:
        del session_id
        return None

    async def list_sessions(self) -> tuple[list[object], int]:
        return [], 0

    async def delete(self, session_id: str) -> bool:
        del session_id
        return False

    async def archive(self, session_id: str) -> bool:
        del session_id
        return False


class _RecordingSessionPort:
    """Observe calls while delegating real persistence to SQLite."""

    def __init__(self, delegate: SessionStore) -> None:
        self.delegate = delegate
        self.calls: list[str] = []
        self.close_count = 0

    async def create_session(
        self,
        title: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> Session:
        self.calls.append("create_session")
        return await self.delegate.create_session(title, model, system_prompt)

    async def save(self, session: Session) -> None:
        self.calls.append("save")
        await self.delegate.save(session)

    async def load(self, session_id: str) -> Session | None:
        self.calls.append("load")
        return await self.delegate.load(session_id)

    async def list_sessions(
        self,
        page: int = 1,
        page_size: int = 20,
        query: str = "",
    ) -> tuple[list[Session], int]:
        self.calls.append("list_sessions")
        return await self.delegate.list_sessions(page, page_size, query)

    async def delete(self, session_id: str) -> bool:
        self.calls.append("delete")
        return await self.delegate.delete(session_id)

    async def archive(self, session_id: str) -> bool:
        self.calls.append("archive")
        return await self.delegate.archive(session_id)

    async def close(self) -> None:
        self.calls.append("close")
        self.close_count += 1
        await self.delegate.close()


class _FalseyRecordingSessionPort(_RecordingSessionPort):
    def __bool__(self) -> bool:
        return False


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
    )


def _recording_port(tmp_path: Path) -> _RecordingSessionPort:
    return _RecordingSessionPort(SessionStore(_config(tmp_path).memory))


def test_session_port_exposes_exact_persistence_operations() -> None:
    public_methods = {
        name
        for name, value in vars(SessionPort).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }

    assert public_methods == EXPECTED_SESSION_PORT_METHODS


def test_session_store_structurally_implements_session_port(tmp_path) -> None:
    store = SessionStore(
        MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
    )

    assert isinstance(store, SessionPort)


def test_incomplete_session_port_is_rejected() -> None:
    assert not isinstance(_IncompleteSessionPort(), SessionPort)


@pytest.mark.asyncio
async def test_agent_engine_uses_injected_port_and_exposes_legacy_alias(
    tmp_path: Path,
) -> None:
    port = _recording_port(tmp_path)
    engine = AgentEngine(_config(tmp_path), session_port=port)

    try:
        assert engine.session_store is port
        assert await engine.get_or_create_session(title="Port 注入") is engine._session
        assert port.calls == ["create_session"]
    finally:
        await engine.shutdown()

    assert port.close_count == 1


@pytest.mark.asyncio
async def test_agent_engine_keeps_default_sqlite_session_store(tmp_path: Path) -> None:
    engine = AgentEngine(_config(tmp_path))

    try:
        assert isinstance(engine.session_store, SessionStore)
        assert isinstance(engine.session_store, SessionPort)
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_agent_engine_does_not_replace_explicit_falsey_port(tmp_path: Path) -> None:
    port = _FalseyRecordingSessionPort(
        SessionStore(_config(tmp_path).memory)
    )
    engine = AgentEngine(_config(tmp_path), session_port=port)

    try:
        assert engine.session_store is port
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_all_engine_session_operations_route_through_injected_port(
    tmp_path: Path,
) -> None:
    port = _recording_port(tmp_path)
    engine = AgentEngine(_config(tmp_path), session_port=port)

    try:
        session = await engine.get_or_create_session(title="完整 Port 路由")
        engine._messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "保存我"},
        ]
        engine._full_history = list(engine._messages)

        await engine._save_session()
        assert await engine.load_session(session.id)
        sessions, total = await engine.list_sessions(query="完整 Port")
        assert total == 1
        assert [item.id for item in sessions] == [session.id]
        assert await engine.archive_session(session.id)
        assert await engine.delete_session(session.id)

        assert port.calls == [
            "create_session",
            "save",
            "load",
            "list_sessions",
            "archive",
            "load",
            "delete",
            "load",
        ]
    finally:
        await engine.shutdown()

    assert port.calls[-1] == "close"


@pytest.mark.asyncio
async def test_default_store_load_reactivates_archived_session_and_marks_access(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(_config(tmp_path))
    old = datetime.now() - timedelta(days=90)
    session = Session(
        title="待恢复",
        status="archived",
        updated_at=old,
        last_accessed_at=old,
        archived_at=old,
    )
    await engine.session_store.save(session)

    try:
        assert await engine.load_session(session.id) is True
        persisted = await engine.session_store.load(session.id)
        assert persisted is not None
        assert persisted.status == "active"
        assert persisted.last_accessed_at > old
        assert persisted.archived_at is None
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_archiving_current_session_cannot_be_undone_by_later_save(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(_config(tmp_path))
    session = await engine.get_or_create_session(title="当前会话")

    try:
        assert await engine.archive_session(session.id) is True
        assert engine._session is session
        assert session.status == "archived"
        await engine._save_session()
        persisted = await engine.session_store.load(session.id)
        assert persisted is not None
        assert persisted.status == "archived"
        assert persisted.archived_at is not None
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_builds_retention_preview_from_default_store_config(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.memory.session_retention.delete_archived_after_days = 30
    engine = AgentEngine(config)
    old = datetime.now() - timedelta(days=45)
    await engine.session_store.save(
        Session(
            id="expired",
            title="已过期",
            status="archived",
            updated_at=old,
            last_accessed_at=old,
            archived_at=old,
        )
    )

    try:
        preview = await engine.preview_session_retention()
        assert [item.session_id for item in preview.selected] == ["expired"]
        assert preview.total_archived_count == 1
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_retention_preview_fails_closed_for_port_without_scan_capability(
    tmp_path: Path,
) -> None:
    port = _recording_port(tmp_path)
    engine = AgentEngine(_config(tmp_path), session_port=port)

    try:
        with pytest.raises(RuntimeError, match="不支持保留策略预览"):
            await engine.preview_session_retention()
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_retention_pass_uses_retention_actor_and_bounded_preview(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(_config(tmp_path))
    preview = SessionRetentionPreview(
        selected=(
            SessionRetentionSelection(
                session_id="archived",
                title="归档会话",
                effective_last_accessed_at=datetime.now() - timedelta(days=90),
                payload_bytes=100,
                reason=SessionRetentionReason.AGE_EXPIRED,
            ),
        ),
        total_archived_count=1,
        total_archived_bytes=100,
        scanned_count=1,
        eligible_count=1,
        deferred_eligible_count=0,
        selected_bytes=100,
        storage_excess_bytes=0,
        scan_truncated=False,
        budget_exhausted=False,
        policy=SessionRetentionPolicy(),
    )
    engine.preview_session_retention = AsyncMock(return_value=preview)
    engine._session_reconciliation_coordinator.delete_session = AsyncMock(
        return_value=ReconciliationCoordinatorResult(
            session_id="archived",
            request_id="request-archived",
            outcome=ReconciliationCoordinatorOutcome.COMPLETED,
            reconciliation_state=None,
            tombstone_status=None,
            message="完成",
        )
    )

    try:
        result = await engine.run_session_retention_once()
        assert result.completed_count == 1
        engine._session_reconciliation_coordinator.delete_session.assert_awaited_once_with(
            "archived",
            actor=LifecycleActor.RETENTION_WORKER,
        )
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_real_streaming_run_persists_through_injected_session_port(
    tmp_path: Path,
) -> None:
    port = _recording_port(tmp_path)
    engine = AgentEngine(_config(tmp_path), session_port=port)
    events: list[tuple[str, dict[str, object]]] = []

    async def on_event(event: str, data: dict[str, object]) -> None:
        events.append((event, data))

    async def stream_response(**_: object):
        yield StreamChunk(token="Port 持久化完成")
        yield StreamChunk(
            finish_reason="stop",
            usage=TokenUsage(
                input_tokens=3,
                output_tokens=4,
                total_tokens=7,
                cost_usd=0.002,
            ),
        )

    try:
        with patch.object(engine._router, "stream", new=stream_response):
            result = await engine.run_streaming("验证 SessionPort", on_event)

        assert result.status == "completed"
        assert result.response == "Port 持久化完成"
        assert result.receipt is not None
        assert events[0][0] == "run_started"
        receipts = [data for event, data in events if event == "completion_receipt"]
        assert receipts == [result.receipt.to_dict()]
        assert engine._session is not None

        saved = await port.load(engine._session.id)
        assert saved is not None
        assert [message["role"] for message in saved.messages] == [
            "system",
            "user",
            "assistant",
        ]
        assert saved.messages[1]["content"] == "验证 SessionPort"
        assert saved.messages[2]["content"] == "Port 持久化完成"
        assert saved.workspace_root == str(tmp_path.resolve())
        assert saved.total_tokens == 7
        assert saved.total_cost_usd == pytest.approx(0.002)
        assert port.calls.count("create_session") == 1
        assert port.calls.count("save") >= 1
        assert port.calls[-1] == "load"
    finally:
        await engine.shutdown()

    assert port.close_count == 1


def test_agent_engine_rejects_incomplete_session_port_in_chinese(tmp_path: Path) -> None:
    with pytest.raises(
        TypeError,
        match="session_port 必须实现完整的 SessionPort 契约",
    ):
        AgentEngine(
            _config(tmp_path),
            session_port=_IncompleteSessionPort(),  # type: ignore[arg-type]
        )

    assert not (tmp_path / ".naumi").exists()
