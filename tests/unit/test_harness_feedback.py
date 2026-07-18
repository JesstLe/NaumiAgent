from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import (
    FeedbackIntakeService,
    FeedbackSourceEnvelope,
    build_agent_interpreted_feedback,
    build_direct_user_feedback,
)
from naumi_agent.orchestrator.engine import AgentEngine, AgentResult
from naumi_agent.tools.feedback import FeedbackIntakeTool

NOW = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)


def _service(tmp_path: Path) -> tuple[FeedbackIntakeService, EvolutionCandidateStore]:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    return FeedbackIntakeService(store), store


def test_feedback_intake_requires_explicit_evolution_store() -> None:
    with pytest.raises(TypeError, match="store 必须是 EvolutionCandidateStore"):
        FeedbackIntakeService(object())  # type: ignore[arg-type]


def _direct(*, now: datetime = NOW, summary: str = "子智能体状态显示错误"):
    return build_direct_user_feedback(
        session_id="session-1",
        category="correction",
        scope="ui:task_panel",
        topic="subagent_status",
        summary=summary,
        provider="openai",
        model="openai/kimi-for-coding",
        platform="darwin",
        now=now,
    )


@pytest.mark.parametrize(
    "scope",
    [
        "files:src/one.py",
        "files:src/one.py,src/one.py",
        "files:src/one.py,../secret.py",
        "files:src/one.py,/tmp/two.py",
    ],
)
def test_feedback_rejects_invalid_multi_file_scope_before_persistence(scope: str) -> None:
    with pytest.raises(ValueError):
        build_direct_user_feedback(
            session_id="session-1",
            category="defect",
            scope=scope,
            topic="multi_file_scope",
            summary="多文件范围异常",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_direct_feedback_is_idempotent_per_minute_and_never_stores_summary(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    secret_summary = "状态错了 token=do-not-persist-this-value"

    first = await service.ingest(tmp_path, _direct(summary=secret_summary))
    retry = await service.ingest(tmp_path, _direct(summary=secret_summary))
    later = await service.ingest(
        tmp_path,
        _direct(now=NOW + timedelta(minutes=1), summary=secret_summary),
    )

    assert first.status == "recorded"
    assert retry.candidate_id == first.candidate_id
    assert retry.occurrence_count == 1
    assert later.candidate_id == first.candidate_id
    assert later.occurrence_count == 2
    stored = await store.get_candidate(tmp_path, first.candidate_id)
    assert stored is not None
    assert stored.draft.experiment_eligible is False
    assert stored.draft.expected_metrics[0].verifier == "feedback_recurrence"
    assert secret_summary.encode("utf-8") not in (tmp_path / "evolution.db").read_bytes()


@pytest.mark.asyncio
async def test_agent_interpretation_and_direct_user_merge_without_origin_confusion(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    direct = await service.ingest(tmp_path, _direct())
    envelope = FeedbackSourceEnvelope(
        run_id="run-1",
        user_message_id="msg-1",
        content_sha256="a" * 64,
        observed_at=(NOW + timedelta(minutes=2)).isoformat(),
    )
    agent_observation = build_agent_interpreted_feedback(
        envelope,
        category="correction",
        scope="ui:task_panel",
        topic="subagent_status",
        summary="用户指出子智能体状态与真实执行不一致",
    )

    agent = await service.ingest(tmp_path, agent_observation)
    agent_retry = await service.ingest(
        tmp_path,
        build_agent_interpreted_feedback(
            envelope,
            category="correction",
            scope="ui:task_panel",
            topic="subagent_status",
            summary="同一 Run 中换一种说法也不能放大频次",
        ),
    )

    assert agent.candidate_id == direct.candidate_id
    assert agent.occurrence_count == 2
    assert agent_retry.occurrence_count == 2
    stored = await store.get_candidate(tmp_path, agent.candidate_id)
    assert stored is not None
    assert stored.draft.source_kinds == (
        "agent_interpreted_feedback",
        "user_feedback",
    )
    assert {item.source_kind for item in stored.draft.evidence} == {
        "agent_interpreted_feedback",
        "user_feedback",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["preference", "cancel", "praise"])
async def test_non_defect_feedback_is_ignored_without_creating_store(
    tmp_path: Path,
    category: str,
) -> None:
    service, _store = _service(tmp_path)
    observation = build_direct_user_feedback(
        session_id="session-1",
        category=category,
        scope="ui:theme",
        topic="accent_color",
        summary="我更喜欢蓝色",
        now=NOW,
    )

    result = await service.ingest(tmp_path, observation)

    assert result.status == "ignored"
    assert result.reason_code == f"non_defect_{category}"
    assert not (tmp_path / "evolution.db").exists()


def test_feedback_requires_stable_relative_scope_and_topic() -> None:
    with pytest.raises(ValueError, match="相对 scope"):
        build_direct_user_feedback(
            session_id="session-1",
            category="defect",
            scope="/Users/lv/secret",
            topic="path_leak",
            summary="路径错误",
            now=NOW,
        )
    with pytest.raises(ValueError, match="稳定的小写标识符"):
        build_direct_user_feedback(
            session_id="session-1",
            category="defect",
            scope="ui:task_panel",
            topic="状态显示",
            summary="状态错误",
            now=NOW,
        )


class _FakeEngine:
    def __init__(
        self,
        workspace_root: Path,
        service: FeedbackIntakeService,
        envelope: FeedbackSourceEnvelope | None,
    ) -> None:
        self.workspace_root = workspace_root
        self.feedback_intake_service = service
        self.router = SimpleNamespace(current_model="openai/test-model")
        self._envelope = envelope

    def current_feedback_turn(self) -> FeedbackSourceEnvelope | None:
        return self._envelope

    async def get_or_create_session(self):
        return SimpleNamespace(id="session-1")


@pytest.mark.asyncio
async def test_agent_tool_fails_closed_without_durable_turn(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)
    engine = _FakeEngine(tmp_path, service, None)
    tool = FeedbackIntakeTool(engine, service)

    output = await tool.execute(
        category="correction",
        scope="ui:task_panel",
        topic="subagent_status",
        summary="用户纠正了状态",
    )

    assert "没有可验证的 durable 用户消息" in output
    assert not (tmp_path / "evolution.db").exists()


@pytest.mark.asyncio
async def test_agent_tool_and_direct_slash_share_intake_service(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    envelope = FeedbackSourceEnvelope(
        run_id="run-tool",
        user_message_id="msg-tool",
        content_sha256="b" * 64,
        observed_at=NOW.isoformat(),
    )
    engine = _FakeEngine(tmp_path, service, envelope)
    tool = FeedbackIntakeTool(engine, service)

    tool_output = await tool.execute(
        category="defect",
        scope="ui:footer",
        topic="truncation",
        summary="用户报告底栏被截断",
    )
    slash_output = await execute_slash_command(
        engine,
        '/feedback defect ui:footer truncation "底栏内容被省略"',
    )

    assert "agent_interpreted_feedback" in tool_output
    assert "user_feedback" in slash_output
    candidates = await store.list_candidates(tmp_path)
    assert len(candidates) == 1
    assert candidates[0].draft.occurrence_count == 2


@pytest.mark.asyncio
async def test_direct_feedback_fails_closed_without_composed_service(
    tmp_path: Path,
) -> None:
    service, _store = _service(tmp_path)
    engine = _FakeEngine(tmp_path, service, None)
    del engine.feedback_intake_service

    output = await execute_slash_command(
        engine,
        '/feedback defect ui:footer truncation "底栏内容被省略"',
    )

    assert "未装配 FeedbackIntakeService" in output
    assert not (tmp_path / "evolution.db").exists()


@pytest.mark.asyncio
async def test_streaming_engine_mints_and_clears_trusted_feedback_turn(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        ),
    ))
    observed: list[FeedbackSourceEnvelope] = []

    async def fake_core(*_args, **_kwargs) -> AgentResult:
        envelope = engine.current_feedback_turn()
        assert envelope is not None
        observed.append(envelope)
        return AgentResult(status="completed", response="ok")

    async def on_event(_event: str, _data: dict[str, object]) -> None:
        return None

    try:
        engine._run_streaming_core = fake_core  # type: ignore[method-assign]
        result = await engine.run_streaming("用户纠正内容", on_event)

        assert result.status == "completed"
        assert len(observed) == 1
        assert observed[0].run_id
        assert observed[0].user_message_id.startswith("msg-")
        assert observed[0].content_sha256 != "0" * 64
        assert engine.current_feedback_turn() is None
        assert engine.tool_registry.get("feedback_intake") is not None
    finally:
        await engine.shutdown()
