"""Real team intake through headless Textual, Session SQLite and queue authority."""

from __future__ import annotations

import sqlite3

import pytest
from textual.widgets import Input, Markdown

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelRouter
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tui.app import ChatPanel, NaumiApp, Spinner, StatusBar

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


@pytest.fixture
async def team_tui(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))

    async def no_provider_call(self, **kwargs):
        raise AssertionError("Team intake must not contact a model provider")

    async def no_provider_stream(self, **kwargs):
        raise AssertionError("Team intake must not start background model execution")
        yield  # pragma: no cover

    monkeypatch.setattr(ModelRouter, "call", no_provider_call)
    monkeypatch.setattr(ModelRouter, "stream", no_provider_stream)
    work = tmp_path / "work"
    work.mkdir()
    engine = create_agent_engine(
        AppConfig(
            workspace_root=str(work),
            personal={"data_dir": str(tmp_path / "personal")},
            memory={
                "session_db_path": str(tmp_path / "sessions.db"),
                "vector_db_path": str(tmp_path / "vectors"),
                "long_term_enabled": False,
            },
            safety={"permission_mode": "bypass", "allowed_dirs": [str(work)]},
        )
    )
    await engine.get_or_create_session()
    team = engine.personal_agents.create(
        name="用户创建的日常团队",
        instructions="仅处理用户的明确任务。",
        request_id="team-tui-profile",
        model_tier="fast",
        kind="team",
        member_ids=[],
        tool_names=[],
    )
    daily = await engine.personal_agents.open_session(team.id)
    await engine.load_session(daily["session_id"])

    # Disable automatic execution/recovery only; intake and every store stay real.
    engine.personal_teams.start = lambda: None
    engine.personal_collaborations.start = lambda: None

    async def no_background_services():
        return ()

    monkeypatch.setattr(engine, "start_long_running_services", no_background_services)
    try:
        yield engine, team, NaumiApp(engine)
    finally:
        await engine.shutdown()


def _observe_chat(app):
    chat = app.query_one(ChatPanel)
    tokens, finalizations = [], []
    add_token, finalize = chat.add_response_token, chat.finalize

    def record_token(token):
        tokens.append(token)
        add_token(token)

    def record_finalize(*args, **kwargs):
        finalizations.append((args, kwargs))
        finalize(*args, **kwargs)

    chat.add_response_token = record_token
    chat.finalize = record_finalize
    return tokens, finalizations


def _chat_text(app):
    return "\n".join(str(widget.source or "") for widget in app.query(Markdown))


async def _wait_for_finalizations(app, pilot, observed, count):
    for _ in range(100):
        await pilot.pause(0.05)
        if len(observed) == count and not app._agent_busy:
            return
    pytest.fail(f"TUI did not settle: {app.query_one(StatusBar).status_text}")


async def test_team_daily_input_shows_real_admitted_id_without_claiming_completion(team_tui):
    engine, team, app = team_tui
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        tokens, finalizations = _observe_chat(app)
        msg_input = app.query_one("#msg-input", Input)
        msg_input.value = "请整理今天的三项待办"
        msg_input.focus()
        await pilot.press("enter")
        await _wait_for_finalizations(app, pilot, finalizations, 1)

        turns = await engine.personal_teams.list(team_session_id=engine._session.id)
        assert len(turns) == 1
        turn = turns[0]
        assert turn["id"].startswith("pt_")
        assert turn["team_id"] == team.id
        assert turn["status"] == "queued" and turn["phases"] == []
        assert "团队任务已受理：" + turn["id"] in "".join(tokens)
        assert turn["id"] in _chat_text(app)
        assert "/assistants team-turn " + turn["id"] in _chat_text(app)
        assert app.query_one(StatusBar).status_text == "后台任务已受理"
        assert app.query_one("#msg-input", Input).disabled is False
        assert app.query_one(Spinner)._active is False
        session = await engine.session_store.load(engine._session.id)
        assert session.personal_team_id == team.id
        assert [(m["role"], m["content"]) for m in session.messages] == [
            ("user", "请整理今天的三项待办")
        ]
        assert session.messages[0]["metadata"]["team_turn_id"] == turn["id"]


async def test_team_queued_inputs_finish_handoff_not_background_work(team_tui):
    engine, _, app = team_tui
    session_id = engine._session.id
    authority = app._conversation_queue_authority(session_id)
    assert authority is not None
    items = [
        await authority.enqueue(
            request_id=f"team-tui-queued-{index}",
            text=text,
            client_id=app._queue_owner_id,
        )
        for index, text in enumerate(("整理工作清单", "整理生活清单"))
    ]
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        tokens, finalizations = _observe_chat(app)
        assert await app._start_next_queued_conversation()
        await _wait_for_finalizations(app, pilot, finalizations, 2)
        assert await authority.store.list_queued_conversations(
            workspace_root=engine.workspace_root, session_id=session_id
        ) == ()
        turns = await engine.personal_teams.list(team_session_id=session_id)
        assert len(turns) == 2
        assert all(turn["status"] == "queued" and turn["phases"] == [] for turn in turns)
        assert all(turn["id"] in "".join(tokens) for turn in turns)
        assert all(turn["id"] in _chat_text(app) for turn in turns)
        assert app.query_one(StatusBar).status_text == "后台任务已受理"
        with sqlite3.connect(authority.store.db_path) as db:
            persisted = db.execute(
                "SELECT request_id, state, terminal_reason FROM harness_conversation_queue "
                "WHERE session_id = ? ORDER BY request_id",
                (session_id,),
            ).fetchall()
        assert persisted == [
            (item.request_id, "completed", "background_task_admitted") for item in items
        ]
        for item in items:
            lease = await authority.store.get_run_lease(
                workspace_root=engine.workspace_root,
                run_kind="runtime",
                run_id=authority.claim_run_id(item.request_id),
            )
            assert lease is not None and lease.state.value == "released"
        session = await engine.session_store.load(session_id)
        assert [m["content"] for m in session.messages] == [item.text for item in items]
        assert len({m["metadata"]["team_turn_id"] for m in session.messages}) == 2


async def test_paused_team_remains_a_real_error_and_does_not_admit_work(team_tui):
    engine, team, app = team_tui
    engine.personal_agents.update(team.id, team.version, status="paused")
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        tokens, finalizations = _observe_chat(app)
        msg_input = app.query_one("#msg-input", Input)
        msg_input.value = "这个任务不应被受理"
        msg_input.focus()
        await pilot.press("enter")
        await _wait_for_finalizations(app, pilot, finalizations, 1)
        assert app.query_one(StatusBar).status_text.startswith("❌")
        assert "**错误**" in "".join(tokens)
        assert "团队任务已受理" not in _chat_text(app)
        assert await engine.personal_teams.list(team_session_id=engine._session.id) == []
        assert app.query_one("#msg-input", Input).disabled is False
        assert app.query_one(Spinner)._active is False
