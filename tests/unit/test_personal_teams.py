"""Staged team execution through real Engines, Jobs, Runs, file tools and SQLite.

Only model transport is controlled; no completed Job/Run/receipt is fabricated.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.messages import router as messages_router
from naumi_agent.api.routes.personal import router as personal_router
from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.agent_jobs import AgentJobState
from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.teams import PersonalTeamService
from naumi_agent.runs.usage import RunUsageTotals, build_run_usage
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.base import ToolCall

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
_TERMINAL = {"completed", "error", "cancelled", "unknown", "blocked"}


@pytest.fixture
async def team_setup(tmp_path, monkeypatch):
    async def no_nonstream_http(self, **kwargs):
        raise AssertionError("Controlled team tests must never fall back to provider HTTP")

    monkeypatch.setattr(ModelRouter, "call", no_nonstream_http)
    work, data = tmp_path / "work", tmp_path / "state"
    work.mkdir()
    (work / "budget.csv").write_text("income,expense\n1200,375\n", encoding="utf-8")
    (work / ".gitignore").write_text(".naumi/\n*.db\n*.db-*\n", encoding="utf-8")
    for command in (
        ["init", "--quiet"],
        ["add", "budget.csv", ".gitignore"],
        [
            "-c",
            "user.name=Team Test",
            "-c",
            "user.email=team@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Record team acceptance input",
        ],
    ):
        subprocess.run(
            ["git", "-C", str(work), *command], check=True, capture_output=True, text=True
        )
    config = AppConfig(
        workspace_root=str(work),
        personal={"data_dir": str(data / "personal")},
        memory={
            "session_db_path": str(data / "sessions.db"),
            "vector_db_path": str(data / "vectors"),
            "long_term_enabled": False,
        },
        safety={
            "permission_mode": "bypass",
            "max_turns": 7,
            "max_parallel_agents": 1,
            "max_queued_agents": 0,
            "allowed_dirs": [str(work)],
        },
    )
    engine = create_agent_engine(config)
    try:
        await engine.get_or_create_session()
        engine._append_message({"role": "user", "content": "MAIN_PRIVATE_NEVER_SHARED"})
        await engine._save_session()
        teams, collaborations = engine.personal_teams, engine.personal_collaborations
        teams.start = collaborations.start = lambda: None
        members = [
            engine.personal_agents.create(
                name="用户创建成员" + letter,
                instructions="只处理显式资料 MEMBER_ROLE_" + letter,
                request_id="team-member-" + letter,
                model_tier="fast",
                tool_names=["file_read", "file_write", "personal_message"],
            )
            for letter in "AB"
        ]
        profile = engine.personal_agents.create(
            name="用户创建的预算团队",
            instructions="只使用已有成员进行真正分工，然后汇总实际结果。",
            request_id="team-profile",
            model_tier="fast",
            kind="team",
            member_ids=[m.id for m in members],
            tool_names=["personal_team"],
        )
        private = await engine.personal_agents.open_session(members[0].id)
        saved = await engine.session_store.load(private["session_id"])
        saved.add_message("user", "MEMBER_PRIVATE_NEVER_SHARED")
        await engine.session_store.save(saved)
        children = []

        def factory(step, job):
            child = collaborations._make_child(step, job)
            children.append((step, job, child))
            return child

        collaborations._child_factory = factory
        yield engine, teams, collaborations, profile, members, children, work
    finally:
        await engine.shutdown()


def _task(kwargs):
    return next(m["content"] for m in reversed(kwargs["messages"]) if m["role"] == "user")


def _tools(kwargs):
    return [m for m in kwargs["messages"] if m["role"] == "tool"]


def _usage():
    return StreamChunk(
        finish_reason="stop", usage=TokenUsage(input_tokens=11, output_tokens=7, cost_usd=0.002)
    )


def _tool(name, arguments, call_id="team-call"):
    return StreamChunk(
        tool_call={
            0: {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        },
        finish_reason="tool_calls",
        usage=TokenUsage(input_tokens=11, output_tokens=7, cost_usd=0.002),
    )


def _plan(members, *, parallel=False):
    return [
        {
            "id": letter.lower(),
            "agent_id": member.id,
            "agent_version": member.version,
            "task": "MEMBER_" + letter,
            "depends_on": [] if n == 0 or parallel else ["a"],
        }
        for n, (letter, member) in enumerate(zip("AB", members, strict=True))
    ]


async def _start(fixture, **changes):
    engine, teams, _, profile, _, _, _ = fixture
    return await teams.start_turn(
        **{
            "source_session_id": engine._session.id,
            "team_id": profile.id,
            "team_version": profile.version,
            "request_id": "team-request-one",
            "content": "TEAM_TASK",
            "context": "EXPLICIT_SHARED_BUDGET_ONLY",
            **changes,
        }
    )


async def _settle(teams, collaborations, identity, *, rounds=20):
    for _ in range(rounds):
        await teams.tick()
        await collaborations.tick()
        if collaborations._active:
            await asyncio.wait_for(
                asyncio.gather(*tuple(collaborations._active.values()), return_exceptions=True),
                timeout=30,
            )
        view = await teams.get(identity)
        if view["status"] in _TERMINAL:
            return view
    raise AssertionError(json.dumps(await teams.get(identity), ensure_ascii=False, indent=2))


def _flow(monkeypatch, members, directory, calls, *, proposed=None):
    async def stream(self, **kwargs):
        encoded = json.dumps(kwargs, ensure_ascii=False)
        assert "MAIN_PRIVATE_NEVER_SHARED" not in encoded
        assert "MEMBER_PRIVATE_NEVER_SHARED" not in encoded
        task, tools = _task(kwargs), _tools(kwargs)
        calls.append((task, len(tools)))
        names = {s["function"]["name"] for s in kwargs.get("tools") or []}
        if task.startswith("TEAM_TASK"):
            assert "personal_team" in names
            assert not {"file_read", "file_write", "personal_message"} & names
            if not tools:
                yield _tool(
                    "personal_team",
                    {
                        "operation": "propose",
                        "steps": proposed or _plan(members),
                        "context": "EXPLICIT_MEMBER_CONTEXT",
                    },
                    "plan-call",
                )
            else:
                assert json.loads(tools[-1]["content"])["status"] == "plan_saved"
                yield StreamChunk(token="分工已持久保存，等待后台成员执行。")
                yield _usage()
        elif task in {"MEMBER_A", "MEMBER_B"}:
            assert "file_read" in names and "file_write" not in names
            if not tools:
                yield _tool("file_read", {"path": str(directory / "budget.csv")}, "read-call")
            else:
                assert "1200" in tools[-1]["content"] and "375" in tools[-1]["content"]
                yield StreamChunk(token=task + "核验825元，来源budget.csv")
                yield _usage()
        else:
            assert "personal_team" not in names and "file_write" not in names
            assert "member_results" in encoded and "MEMBER_A核验825" in encoded
            assert "MEMBER_B核验825" in encoded
            yield StreamChunk(token="团队最终汇总825元，来源budget.csv，两位成员均已独立核验。")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)


async def test_single_capacity_three_phases_four_real_runs_and_once_team_delivery(
    team_setup, monkeypatch
):
    engine, teams, collaborations, profile, members, children, directory = team_setup
    calls = []
    _flow(monkeypatch, members, directory, calls)
    created = await _start(team_setup)
    view = await _settle(teams, collaborations, created["id"])
    assert view["status"] == "completed", json.dumps(view, ensure_ascii=False, indent=2)
    assert [p["phase"] for p in view["phases"]] == ["planning", "members", "summary"]
    steps = [s for p in view["phases"] for s in p["collaboration"]["steps"]]
    assert len({s["job_id"] for s in steps}) == len({s["run_id"] for s in steps}) == 4
    assert len(children) == 4
    jobs = [await collaborations.jobs.get(s["job_id"]) for s in steps]
    assert all(j.state is AgentJobState.COMPLETED for j in jobs)
    assert view["usage"]["job_count"] == 4
    assert view["usage"]["total_tokens"] == sum(j.result.total_tokens for j in jobs) == 126
    assert view["usage"]["total_cost_usd"] == 0.014
    saved = await engine.session_store.load(view["team_session_id"])
    assert saved.personal_agent_id == saved.personal_team_id == profile.id
    assert saved.personal_execution_id == ""
    assert [m["id"] for m in saved.messages] == [
        view["source_user_message_id"],
        view["result_message_id"],
    ]
    assert saved.total_tokens == 126 and saved.total_cost_usd == 0.014
    assert "825" in saved.messages[-1]["content"]
    assert saved.messages[-1]["metadata"]["run_id"] == steps[-1]["run_id"]
    proposal = next(e["data"] for e in view["events"] if e["kind"] == "plan_proposed")
    assert proposal["source_run_id"] == steps[0]["run_id"]
    assert proposal["task_id"] == steps[0]["task_id"]
    for step in steps:
        session = await engine.session_store.load(step["session_id"])
        assert session.personal_execution_id == step["task_id"] and session.personal_team_id == ""
        run = await engine.chat_run_store.get_run(step["session_id"], step["run_id"])
        assert run.status == run.receipt.outcome == "completed"
    await teams.tick()
    await collaborations.tick()
    assert len(children) == 4
    assert len((await engine.session_store.load(saved.id)).messages) == 2
    assert engine._session.id == view["source_session_id"]


async def test_intake_caller_cancel_and_second_coordinator_resume_exact_turn(
    team_setup, monkeypatch
):
    engine, teams, collaborations, _, members, children, directory = team_setup
    calls, entered = [], asyncio.Event()
    _flow(monkeypatch, members, directory, calls)
    original = teams._append_request

    async def interrupted_after_durable_turn(turn):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(teams, "_append_request", interrupted_after_durable_turn)
    pending = asyncio.create_task(_start(team_setup))
    await asyncio.wait_for(entered.wait(), timeout=5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    monkeypatch.setattr(teams, "_append_request", original)
    stored = teams.store.list(teams.workspace_key)
    assert len(stored) == 1 and not children
    other = PersonalTeamService(engine)
    other.start = lambda: None
    try:
        await asyncio.gather(teams.tick(), other.tick())
        view = await _settle(other, collaborations, stored[0]["id"])
        assert view["status"] == "completed", view
        assert len(children) == 4
        replay = await _start(team_setup)
        assert replay["id"] == view["id"]
        await asyncio.gather(teams.tick(), other.tick())
        assert len(children) == 4
        saved = await engine.session_store.load(view["team_session_id"])
        assert len(saved.messages) == 2 and len({m["id"] for m in saved.messages}) == 2
    finally:
        await other.stop()


@pytest.mark.parametrize(
    "change",
    [
        {"content": "different"},
        {"context": "different"},
        {"member_grants": []},
        {"source_ids": ["source-other"]},
    ],
)
async def test_raw_request_replay_and_payload_conflict_never_changes_original(team_setup, change):
    _, teams, _, _, _, children, _ = team_setup
    first = await _start(team_setup)
    assert (await _start(team_setup))["id"] == first["id"]
    with pytest.raises(PersonalAgentError) as caught:
        await _start(team_setup, **change)
    assert caught.value.code == "idempotency_conflict"
    assert caught.value.accepted is True and caught.value.turn_id == first["id"]
    assert not children
    saved = teams.store.get(first["id"], teams.workspace_key)
    assert saved["client_payload"]["content"] == "TEAM_TASK"
    assert len(teams.store.list(teams.workspace_key)) == 1


@pytest.mark.parametrize("paused", [False, True])
async def test_empty_tool_team_has_real_conversation_without_member_jobs(
    team_setup, monkeypatch, paused
):
    engine, teams, collaborations, _, members, children, _ = team_setup
    plain = engine.personal_agents.create(
        name="仅对话团队",
        instructions="用普通对话回答，不进行委派。",
        request_id="plain-team",
        kind="team",
        member_ids=[members[0].id],
        tool_names=[],
        model_tier="fast",
    )
    if paused:
        engine.personal_agents.update(members[0].id, members[0].version, status="paused")
    calls = []

    async def stream(self, **kwargs):
        assert not {s["function"]["name"] for s in kwargs.get("tools") or []}
        assert _task(kwargs) == "PLAIN_TASK"
        calls.append(_task(kwargs))
        yield StreamChunk(token="这是一条真实模型传输对应的团队普通回答。")
        yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await _start(
        team_setup,
        team_id=plain.id,
        team_version=plain.version,
        content="PLAIN_TASK",
    )
    assert created["roster"]["agents"] == []
    view = await _settle(teams, collaborations, created["id"])
    assert view["status"] == "completed", view
    assert [p["phase"] for p in view["phases"]] == ["planning"]
    assert len(children) == len(calls) == 1
    saved = await engine.session_store.load(view["team_session_id"])
    assert len(saved.messages) == 2 and "团队普通回答" in saved.messages[-1]["content"]


async def test_empty_tool_team_actual_propose_is_denied_even_with_bypass(team_setup, monkeypatch):
    engine, teams, collaborations, _, members, children, _ = team_setup
    plain = engine.personal_agents.create(
        name="无授权团队",
        instructions="仅对话",
        request_id="plain-denied",
        kind="team",
        member_ids=[m.id for m in members],
        tool_names=[],
        model_tier="fast",
    )

    async def stream(self, **kwargs):
        if not _tools(kwargs):
            yield _tool("personal_team", {"operation": "propose", "steps": _plan(members)})
        else:
            assert "未分配" in _tools(kwargs)[-1]["content"]
            yield StreamChunk(token="委派未获授权。")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await _start(
        team_setup, team_id=plain.id, team_version=plain.version, content="PLAIN_TASK"
    )
    view = await _settle(teams, collaborations, created["id"])
    assert view["status"] == "error", view
    assert len(children) == 1 and len(view["phases"]) == 1
    assert not any(e["kind"] == "plan_proposed" for e in view["events"])


async def test_default_member_scope_blocks_real_file_write_and_summary(team_setup, monkeypatch):
    _, teams, collaborations, _, members, _, directory = team_setup
    forbidden = directory / "forbidden.txt"

    async def stream(self, **kwargs):
        task, tools = _task(kwargs), _tools(kwargs)
        if task == "TEAM_TASK":
            if not tools:
                yield _tool("personal_team", {"operation": "propose", "steps": _plan(members)[:1]})
            else:
                yield StreamChunk(token="已提交。")
                yield _usage()
        else:
            assert task == "MEMBER_A"
            assert "file_write" not in {s["function"]["name"] for s in kwargs.get("tools") or []}
            if not tools:
                yield _tool("file_write", {"path": str(forbidden), "content": "unauthorized"})
            else:
                assert "未分配" in tools[-1]["content"]
                yield StreamChunk(token="写入未获本次授权。")
                yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await _start(team_setup)
    view = await _settle(teams, collaborations, created["id"])
    assert view["status"] == "error", view
    assert not forbidden.exists()
    assert [p["phase"] for p in view["phases"]] == ["planning", "members"]


async def test_two_queued_requests_append_results_without_overwriting_second_input(
    team_setup, monkeypatch
):
    engine, teams, collaborations, _, members, children, directory = team_setup
    calls = []
    _flow(monkeypatch, members, directory, calls)
    first = await _start(team_setup)
    second = await _start(team_setup, request_id="second-request", content="TEAM_TASK_2")
    saved = await engine.session_store.load(first["team_session_id"])
    assert [m["content"] for m in saved.messages] == ["TEAM_TASK", "TEAM_TASK_2"]
    assert (await _settle(teams, collaborations, first["id"]))["status"] == "completed"
    assert (await _settle(teams, collaborations, second["id"]))["status"] == "completed"
    saved = await engine.session_store.load(first["team_session_id"])
    assert len(saved.messages) == 4 and len({m["id"] for m in saved.messages}) == 4
    assert [m["content"] for m in saved.messages if m["role"] == "user"] == [
        "TEAM_TASK",
        "TEAM_TASK_2",
    ]
    assert len(children) == 8
    assert saved.total_tokens == 252 and saved.total_cost_usd == 0.028


@pytest.mark.parametrize("mutation", ["ancestor_members", "member_pause", "member_revision"])
async def test_nested_team_ancestor_and_member_changes_fence_actual_write(
    team_setup, monkeypatch, mutation
):
    engine, teams, collaborations, profile, members, _, directory = team_setup
    nested = engine.personal_agents.create(
        name="用户嵌套团队",
        instructions="保持成员归属",
        request_id="nested",
        kind="team",
        member_ids=[members[0].id],
        tool_names=[],
        model_tier="fast",
    )
    profile = engine.personal_agents.update(
        profile.id, profile.version, member_ids=[nested.id, members[1].id]
    )
    entered, release = asyncio.Event(), asyncio.Event()
    forbidden = directory / "revoked.txt"

    async def stream(self, **kwargs):
        task, tools = _task(kwargs), _tools(kwargs)
        if task == "TEAM_TASK":
            if not tools:
                yield _tool("personal_team", {"operation": "propose", "steps": _plan(members)[:1]})
            else:
                yield StreamChunk(token="已提交。")
                yield _usage()
        else:
            assert task == "MEMBER_A"
            entered.set()
            await release.wait()
            yield _tool("file_write", {"path": str(forbidden), "content": "revoked action"})

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await _start(
        team_setup,
        team_version=profile.version,
        member_grants=[
            {
                "agent_id": members[0].id,
                "agent_version": members[0].version,
                "tool_names": ["file_write"],
            }
        ],
    )
    driver = asyncio.create_task(_settle(teams, collaborations, created["id"]))
    await asyncio.wait_for(entered.wait(), timeout=20)
    if mutation == "ancestor_members":
        engine.personal_agents.update(nested.id, nested.version, member_ids=[])
    elif mutation == "member_pause":
        engine.personal_agents.update(members[0].id, members[0].version, status="paused")
    else:
        engine.personal_agents.update(members[0].id, members[0].version, instructions="新版职责")
    release.set()
    view = await asyncio.wait_for(driver, timeout=20)
    assert view["status"] in {"blocked", "error"}, view
    assert not forbidden.exists()
    assert "summary" not in [p["phase"] for p in view["phases"]]


@pytest.mark.parametrize("started", [False, True])
async def test_cancellation_never_dispatches_later_team_phases(team_setup, monkeypatch, started):
    _, teams, collaborations, _, _, children, _ = team_setup
    entered = asyncio.Event()

    async def stream(self, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await _start(team_setup)
    if started:
        await teams.tick()
        await collaborations.tick()
        await asyncio.wait_for(entered.wait(), timeout=15)
    await teams.cancel(created["id"])
    view = await _settle(teams, collaborations, created["id"])
    assert view["status"] == "cancelled", view
    assert view["cancel_requested"] is True
    assert len(children) == (1 if started else 0)
    assert not any(p["phase"] in {"members", "summary"} for p in view["phases"])


async def test_fenced_planning_stays_unknown_after_new_coordinator_and_never_reruns(
    team_setup, monkeypatch
):
    _, teams, collaborations, _, _, children, _ = team_setup
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def stream(self, **kwargs):
        calls.append(_task(kwargs))
        entered.set()
        await release.wait()
        yield StreamChunk(token="过期规划不作为成员派发依据。")
        yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    created = await _start(team_setup)
    await teams.tick()
    await collaborations.tick()
    await asyncio.wait_for(entered.wait(), timeout=15)
    view = await teams.get(created["id"])
    step = view["phases"][0]["collaboration"]["steps"][0]
    job = await collaborations.jobs.get(step["job_id"])
    collaborations.jobs._clock = lambda: (
        datetime.fromisoformat(job.claim_expires_at) + timedelta(seconds=1)
    )
    await collaborations.jobs.mark_recovery_unknown(
        job.job_id,
        expected_request_sha256=job.request_sha256,
        expected_session_id_sha256=job.request.session_id_sha256,
        expected_claim_epoch=job.claim_epoch,
        expected_latest_receipt_sha256=job.latest_receipt.receipt_sha256,
    )
    other = PersonalTeamService(teams.engine)
    other.start = lambda: None
    try:
        await other.tick()
        await collaborations.tick()
        assert (await other.get(created["id"]))["status"] == "unknown"
        assert len(children) == 1 and calls == ["TEAM_TASK"]
    finally:
        release.set()
        if collaborations._active:
            await asyncio.gather(*tuple(collaborations._active.values()), return_exceptions=True)
        await other.stop()
    assert (await teams.get(created["id"]))["status"] == "unknown"
    assert len(children) == 1


async def test_phase_budgets_use_cumulative_remainder_and_exact_unique_job_usage(
    team_setup, monkeypatch
):
    engine, teams, collaborations, _, members, children, directory = team_setup
    engine.config.safety.max_budget_usd = 0.05
    engine.config.safety.max_input_tokens = 1000
    engine.config.safety.max_output_tokens = 500
    _flow(monkeypatch, members, directory, [])
    view = await _settle(teams, collaborations, (await _start(team_setup))["id"])
    assert view["status"] == "completed", view
    grants = {
        phase: [
            (step, job, child)
            for step, job, child in children
            if step["team_execution"]["phase"] == phase
        ]
        for phase in ("planning", "members", "summary")
    }
    assert grants["planning"][0][2].config.safety.max_input_tokens == 1000
    assert all(child.config.safety.max_input_tokens == 326 for _, _, child in grants["members"])
    assert all(child.config.safety.max_output_tokens == 162 for _, _, child in grants["members"])
    assert all(job.request.max_cost_microusd == 15333 for _, job, _ in grants["members"])
    assert grants["summary"][0][2].config.safety.max_input_tokens == 934
    assert grants["summary"][0][2].config.safety.max_output_tokens == 458
    assert grants["summary"][0][1].request.max_cost_microusd == 38000
    assert view["usage"] == {
        "total_tokens": 126,
        "total_cost_usd": 0.014,
        "input_tokens": 77,
        "output_tokens": 49,
        "token_breakdown_complete": True,
        "job_count": 4,
    }


async def test_exhausted_budget_rejects_before_any_planning_model_call(team_setup, monkeypatch):
    engine, teams, collaborations, _, _, children, _ = team_setup
    engine.config.safety.max_input_tokens = 0

    async def stream(self, **kwargs):
        raise AssertionError("A zero-budget turn must not call a model")
        yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    view = await _settle(teams, collaborations, (await _start(team_setup))["id"])
    assert view["status"] == "blocked" and "预算" in view["error"]
    assert not children and not view["phases"]


async def _finish_planning_only(fixture, monkeypatch):
    engine, teams, collaborations, _, members, children, directory = fixture
    _flow(monkeypatch, members, directory, [])
    created = await _start(fixture)
    await teams.tick()
    await collaborations.tick()
    await asyncio.wait_for(asyncio.gather(*tuple(collaborations._active.values())), timeout=30)
    view = await teams.get(created["id"])
    assert view["status"] == "working" and len(children) == 1
    assert view["usage"]["total_tokens"] == 36
    return view


def _alter_planning_usage(engine, view, mutation):
    step = view["phases"][0]["collaboration"]["steps"][0]
    with sqlite3.connect(engine.chat_run_store.db_path) as db:
        if mutation == "missing":
            db.execute(
                "UPDATE chat_runs SET usage_id = '', usage_json = '' WHERE id = ?",
                (step["run_id"],),
            )
        else:
            # A valid content-addressed RunUsage can still disagree with the
            # independently issued root Job counter; both must reconcile.
            usage = build_run_usage(
                run_id=step["run_id"],
                before=RunUsageTotals(0, 0, 0, 0, Decimal(0)),
                after=RunUsageTotals(23, 14, 0, 2, Decimal("0.004")),
            )
            db.execute(
                "UPDATE chat_runs SET usage_id = ?, usage_json = ? WHERE id = ?",
                (usage.usage_id, usage.model_dump_json(), step["run_id"]),
            )


@pytest.mark.parametrize("mutation", ["missing", "mismatch"])
@pytest.mark.parametrize("limit", ["max_input_tokens", "max_output_tokens"])
async def test_finite_token_budget_preserves_unknown_run_usage_and_stops_new_phase(
    team_setup, monkeypatch, mutation, limit
):
    engine, teams, collaborations, _, _, children, _ = team_setup
    setattr(engine.config.safety, limit, 1000)
    view = await _finish_planning_only(team_setup, monkeypatch)
    _alter_planning_usage(engine, view, mutation)
    current = await teams.get(view["id"])
    assert current["status"] == "unknown" and current["error_code"] == "usage_unknown"
    assert "用量待核对" in current["error"]
    assert current["usage"] == {
        "total_tokens": 36,
        "total_cost_usd": 0.004,
        "input_tokens": None,
        "output_tokens": None,
        "token_breakdown_complete": False,
        "job_count": 1,
    }
    turn = teams.store.get(view["id"], teams.workspace_key)
    proposal = teams.event(turn, "plan-proposed")
    with pytest.raises(PersonalAgentError) as caught:
        await teams._create_phase(turn, "members", proposal["data"]["steps"])
    assert caught.value.code == "usage_unknown"
    await teams.tick()
    await collaborations.tick()
    assert len(children) == 1
    assert len((await teams.get(view["id"]))["phases"]) == 1
    assert len((await engine.session_store.load(view["team_session_id"])).messages) == 1


@pytest.mark.parametrize("mutation", ["missing", "mismatch"])
async def test_cost_only_budget_uses_root_job_cost_without_inventing_token_breakdown(
    team_setup, monkeypatch, mutation
):
    engine, teams, collaborations, _, _, children, _ = team_setup
    engine.config.safety.max_input_tokens = None
    engine.config.safety.max_output_tokens = None
    engine.config.safety.max_budget_usd = 0.05
    view = await _finish_planning_only(team_setup, monkeypatch)
    _alter_planning_usage(engine, view, mutation)
    finished = await _settle(teams, collaborations, view["id"])
    assert finished["status"] == "completed", finished
    assert finished["usage"] == {
        "total_tokens": 126,
        "total_cost_usd": 0.014,
        "input_tokens": None,
        "output_tokens": None,
        "token_breakdown_complete": False,
        "job_count": 4,
    }
    assert len(children) == 4
    summary = next(job for step, job, _ in children if step["team_execution"]["phase"] == "summary")
    assert summary.request.max_cost_microusd == 38000
    assert (await engine.session_store.load(view["team_session_id"])).total_tokens == 126


async def test_specialist_source_and_execution_source_are_not_team_intake_shortcuts(
    team_setup, monkeypatch
):
    engine, teams, collaborations, _, members, _, directory = team_setup
    daily = await engine.personal_agents.open_session(members[0].id)
    with pytest.raises(PersonalAgentError) as caught:
        await _start(team_setup, source_session_id=daily["session_id"])
    assert caught.value.code == "team_scope_denied"
    _flow(monkeypatch, members, directory, [])
    view = await _settle(teams, collaborations, (await _start(team_setup))["id"])
    source = view["phases"][0]["collaboration"]["steps"][0]["session_id"]
    with pytest.raises(PersonalAgentError) as caught:
        await _start(team_setup, source_session_id=source, request_id="execution-source")
    assert caught.value.code == "execution_session_readonly"


async def test_team_api_auth_strict_intake_list_replay_and_cancel(team_setup):
    engine, teams, _, profile, _, children, _ = team_setup
    app = FastAPI()
    app.include_router(personal_router)
    app.include_router(messages_router)
    app.state.engine = engine
    app.state.config = engine.config
    engine.config.api.api_keys = ["team-api-local-token"]
    body = {
        "source_session_id": engine._session.id,
        "team_id": profile.id,
        "team_version": profile.version,
        "request_id": "api-team-request",
        "content": "TEAM_TASK",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/personal/team-turns")).status_code == 401
        client.headers["X-API-Key"] = "team-api-local-token"
        for fields in (
            {"team_version": True},
            {"team_version": "1"},
            {"source_run_id": "forged"},
            {"content": ""},
            {"member_grants": [{"agent_id": profile.id, "agent_version": True}]},
        ):
            r = await client.post("/personal/team-turns", json={**body, **fields})
            assert r.status_code == 422, r.text
        r = await client.post("/personal/team-turns", json=body)
        assert r.status_code == 201, r.text
        turn = r.json()
        assert turn["request_id"] == body["request_id"] and turn["status"] == "queued"
        replay = await client.post("/personal/team-turns", json=body)
        assert replay.status_code == 201 and replay.json()["id"] == turn["id"]
        changed = await client.post("/personal/team-turns", json={**body, "content": "different"})
        assert changed.status_code == 409 and changed.json()["accepted"] is True
        assert changed.json()["turn_id"] == turn["id"]
        listed = await client.get(
            "/personal/team-turns", params={"team_session_id": turn["team_session_id"]}
        )
        assert listed.status_code == 200 and len(listed.json()["turns"]) == 1
        assert (await client.get("/personal/team-turns/" + turn["id"])).status_code == 200
        cancelled = await client.post("/personal/team-turns/" + turn["id"] + "/cancel", json={})
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert not children and len(teams.store.list(teams.workspace_key)) == 1


async def test_member_peer_usage_is_not_added_again_to_team_root_job_total(team_setup, monkeypatch):
    engine, teams, collaborations, _, members, children, directory = team_setup
    engine.config.safety.max_input_tokens = 1000
    engine.config.safety.max_output_tokens = 1000
    engine.config.safety.max_budget_usd = 0.1
    plan = [{**_plan(members)[0], "task": "MEMBER_PEER"}]

    async def stream(self, **kwargs):
        encoded = json.dumps(kwargs, ensure_ascii=False)
        assert "MAIN_PRIVATE_NEVER_SHARED" not in encoded
        assert "MEMBER_PRIVATE_NEVER_SHARED" not in encoded
        task, tools = _task(kwargs), _tools(kwargs)
        if task == "TEAM_TASK":
            if not tools:
                yield _tool("personal_team", {"operation": "propose", "steps": plan})
            else:
                yield StreamChunk(token="单个成员分工已保存。")
                yield _usage()
        elif task == "MEMBER_PEER":
            if not tools:
                yield _tool(
                    "personal_message",
                    {
                        "operation": "send",
                        "recipient_agent_id": members[1].id,
                        "recipient_agent_version": members[1].version,
                        "request_id": "team-budget-peer",
                        "content": "PEER_QUERY",
                        "context": "只处理budget.csv",
                    },
                    "peer-send",
                )
            elif len(tools) == 1:
                response = json.loads(tools[-1]["content"])
                assert response["response"] == "对端真实读取825元"
                yield _tool(
                    "personal_message",
                    {
                        "operation": "ack",
                        "message_id": response["id"],
                        "expected_response_sha256": response["response_sha256"],
                    },
                    "peer-ack",
                )
            else:
                yield StreamChunk(token="MEMBER_PEER核验825元")
                yield _usage()
        elif task == "PEER_QUERY":
            if not tools:
                yield _tool("file_read", {"path": str(directory / "budget.csv")}, "peer-read")
            else:
                assert "1200" in tools[-1]["content"] and "375" in tools[-1]["content"]
                yield StreamChunk(token="对端真实读取825元")
                yield _usage()
        else:
            assert "MEMBER_PEER核验825" in encoded
            yield StreamChunk(token="汇总825元，成员互询已确认。")
            yield _usage()

    monkeypatch.setattr(ModelRouter, "stream", stream)
    view = await _settle(teams, collaborations, (await _start(team_setup))["id"])
    assert view["status"] == "completed", view
    assert view["usage"] == {
        "total_tokens": 144,
        "total_cost_usd": 0.016,
        "input_tokens": 88,
        "output_tokens": 56,
        "token_breakdown_complete": True,
        "job_count": 3,
    }
    assert len(children) == 4
    member_phase = next(p["collaboration"] for p in view["phases"] if p["phase"] == "members")
    assert len(member_phase["steps"]) == len(member_phase["messages"]) == 1
    member_step, peer = member_phase["steps"][0], member_phase["messages"][0]
    member_job = await collaborations.jobs.get(member_step["job_id"])
    assert member_job.result.total_tokens == 90
    assert peer["state"] == "acknowledged" and peer["acknowledged_by"] == "agent"
    saved = await engine.session_store.load(view["team_session_id"])
    assert saved.total_tokens == 144


async def test_real_completed_result_before_delivery_journal_crash_recovers_once(
    team_setup, monkeypatch
):
    engine, teams, collaborations, _, members, children, directory = team_setup
    _flow(monkeypatch, members, directory, [])
    original, interrupted = teams.record, False

    def fail_after_session_commit(turn, key, kind, data):
        nonlocal interrupted
        if key == "delivered":
            interrupted = True
            raise OSError("controlled cross-database delivery crash")
        return original(turn, key, kind, data)

    monkeypatch.setattr(teams, "record", fail_after_session_commit)
    created = await _start(team_setup)
    for _ in range(15):
        await teams.tick()
        if interrupted:
            break
        await collaborations.tick()
        if collaborations._active:
            await asyncio.wait_for(
                asyncio.gather(*tuple(collaborations._active.values()), return_exceptions=True),
                timeout=30,
            )
    assert interrupted and len(children) == 4
    pending = await teams.get(created["id"])
    assert pending["status"] == "delivery_pending"
    assert len((await engine.session_store.load(pending["team_session_id"])).messages) == 2
    other = PersonalTeamService(engine)
    other.start = lambda: None
    try:
        finished = await _settle(other, collaborations, created["id"])
        assert finished["status"] == "completed"
        saved = await engine.session_store.load(finished["team_session_id"])
        assert len(saved.messages) == 2 and saved.total_tokens == 126
        assert saved.total_cost_usd == 0.014 and len(children) == 4
    finally:
        monkeypatch.setattr(teams, "record", original)
        await other.stop()


@pytest.mark.parametrize(
    "forged",
    [
        {"source_run_id": "forged"},
        {"turn_id": "pt_" + "a" * 32},
        {"sender_agent_id": "pa_" + "b" * 32},
    ],
)
async def test_public_team_tool_rejects_forged_execution_binding_arguments(team_setup, forged):
    engine, teams, _, _, members, children, _ = team_setup
    result = await engine.execute_tool(
        ToolCall(
            id="forged-context",
            name="personal_team",
            arguments=json.dumps({"operation": "propose", "steps": _plan(members), **forged}),
        )
    )
    assert result.status == "error" and result.error_code == "invalid_input"
    assert not children and teams.store.list(teams.workspace_key) == []


async def test_generic_team_chat_requires_stable_request_and_reuses_one_intake(team_setup):
    engine, teams, _, profile, _, children, _ = team_setup
    daily = await engine.personal_agents.open_session(profile.id)
    app = FastAPI()
    app.include_router(personal_router)
    app.include_router(messages_router)
    app.state.engine = engine
    app.state.config = engine.config
    url = f"/sessions/{daily['session_id']}/messages"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.post(url, json={"content": "TEAM_TASK", "stream": False})
        assert missing.status_code == 409
        assert teams.store.list(teams.workspace_key) == []
        body = {
            "content": "TEAM_TASK",
            "stream": True,
            "request_id": "generic-team-turn",
            "expected_team_version": profile.version,
        }
        first = await client.post(url, json=body)
        assert first.status_code == 202, first.text
        replay = await client.post(url, json=body)
        assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
        changed = await client.post(url, json={**body, "content": "different"})
        assert changed.status_code == 409
    assert not children and len(teams.store.list(teams.workspace_key)) == 1
    saved = await engine.session_store.load(daily["session_id"])
    assert len(saved.messages) == 1 and saved.messages[0]["content"] == "TEAM_TASK"
