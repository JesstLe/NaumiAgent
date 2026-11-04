from __future__ import annotations

import asyncio
import copy
import json
import socket
import threading
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from naumi_agent.daemons.agent_jobs import AgentJobPayload, AgentJobState
from naumi_agent.personal.calendar_action_execution import action_request
from naumi_agent.personal.google_calendar import GoogleCalendarAdapter
from naumi_agent.personal.models import PersonalAgentError
from tests.unit.test_personal_calendar_actions import (
    CONNECTION,
    FixtureService,
    approve,
    prepare,
    settle,
)
from tests.unit.test_personal_calendar_actions import action_env as action_env
from tests.unit.test_personal_google_calendar import Forward


@pytest.fixture
async def protocol(action_env):
    state = SimpleNamespace(
        requests=[],
        events={},
        mode="ok",
        post_entered=threading.Event(),
        post_release=threading.Event(),
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.mutate()

        def do_PATCH(self):
            self.mutate()

        def mutate(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            identity = body.get("id", urlsplit(self.path).path.rsplit("/", 1)[-1])
            state.requests.append((self.command, self.path, dict(self.headers), body))
            if state.mode == "412":
                return self.answer(412, {"error": {"code": 412}})
            if self.command == "PATCH":
                existing = state.events.get(identity)
                if existing is None:
                    return self.answer(404, {"error": {"code": 404}})
                if self.headers.get("If-Match") != existing["etag"]:
                    return self.answer(412, {"error": {"code": 412}})
            remote = {
                **(state.events.get(identity, {}) if self.command == "PATCH" else {}),
                **body,
                "id": identity,
                "etag": '"remote-etag"',
                "status": "confirmed",
                "htmlLink": "https://calendar.google.com/calendar/event?eid=fixture",
            }
            if state.mode == "409_mismatch":
                remote["summary"] = "其他正文"
            if state.mode != "409_wrong":
                state.events[identity] = remote
            if state.mode == "block":
                state.post_entered.set()
                assert state.post_release.wait(timeout=5)
            if state.mode == "reset":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            return self.answer(
                503 if state.mode == "503" else 409 if state.mode.startswith("409") else 200, remote
            )

        def do_GET(self):
            state.requests.append((self.command, self.path, dict(self.headers), None))
            identity = urlsplit(self.path).path.rsplit("/", 1)[-1]
            if state.mode == "get403":
                return self.answer(403, {"error": {"errors": [{"reason": "forbidden"}]}})
            if state.mode in {"get404", "get503"}:
                return self.answer(int(state.mode[3:]), {"error": {"code": int(state.mode[3:])}})
            if (
                state.mode == "etag_race"
                and identity in state.events
                and not getattr(state, "raced", False)
            ):
                state.raced = True
                snapshot = copy.deepcopy(state.events[identity])
                state.events[identity]["etag"] = '"changed-after-get"'
                return self.answer(200, snapshot)
            return (
                self.answer(200, state.events[identity])
                if identity in state.events
                else self.answer(404, {"error": {"code": 404}})
            )

        def answer(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    async with httpx.AsyncClient(
        transport=Forward(f"http://127.0.0.1:{server.server_port}")
    ) as client:
        action_env.service.adapter = GoogleCalendarAdapter(http_client=client)
        try:
            yield action_env, state
        finally:
            state.post_release.set()
            await action_env.service.stop_actions()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


async def execute(env, action=None):
    action = action or await prepare(env)
    approved = await approve(env, action)
    env.service._closing = False
    await settle(env)
    return await env.service.get_action(approved["id"])


@pytest.mark.asyncio
async def test_real_http_sqlite_job_run_receipt_publication_inbox_zero_models(protocol):
    env, state = protocol
    action = await execute(env)
    assert action["status"] == "verified", await env.service._record("action", action["id"])
    assert [request[0] for request in state.requests] == ["POST", "GET"]
    assert state.requests[0][3]["id"] == action["event_id"]
    value = await env.service._record("action", action["id"])
    job = await env.engine.personal_collaborations.jobs.get(action["job_id"])
    assert (
        job.state is AgentJobState.COMPLETED
        and job.result.total_tokens == 0
        and job.result.turns == 0
    )
    run = await env.engine.chat_run_store.get_run(
        value["execution"]["session_id"], action["run_id"]
    )
    assert run.status == "completed" and run.receipt.outcome == "completed" and run.usage is None
    assert len(run.steps) == 3 and all(step.metadata["model_calls"] == 0 for step in run.steps)
    inbox = await env.engine.personal_collaborations.jobs.list_result_inbox_records(
        value["execution"]["session_id"]
    )
    assert len(inbox) == 1 and inbox[0].delivery.job_id == job.job_id
    assert env.engine._session == "do-not-switch-active-session"
    await env.service.action_tick()
    assert len(state.requests) == 2


@pytest.mark.parametrize("mode", ["reset", "503", "get403"])
@pytest.mark.asyncio
async def test_unknown_single_dispatch_then_explicit_original_id_readback(protocol, mode):
    env, state = protocol
    state.mode = mode
    action = await execute(env)
    assert action["status"] == "unknown"
    assert (
        action["last_error_code"]
        == {"reset": "transport_unknown", "503": "service_unavailable", "get403": "access_denied"}[
            mode
        ]
    )
    value = await env.service._record("action", action["id"])
    approval = copy.deepcopy(value["approval"])
    job = await env.engine.personal_collaborations.jobs.get(action["job_id"])
    assert job.state is AgentJobState.UNKNOWN
    before = len(state.requests)
    for _ in range(2):
        await env.service.action_tick()
    assert len(state.requests) == before
    state.mode = "ok"
    request = {"request_id": uuid.uuid4().hex, "expected_revision": action["revision"]}
    result = await env.service.reconcile_action(action["id"], request)
    assert result["status"] == "verified"
    assert sum(row[0] == "POST" for row in state.requests) == 1
    assert state.requests[-1][1].endswith(action["event_id"])
    assert (await env.service._record("action", action["id"]))["approval"] == approval
    assert (
        await env.engine.personal_collaborations.jobs.get(job.job_id)
    ).state is AgentJobState.UNKNOWN
    count = len(state.requests)
    assert (await env.service.reconcile_action(action["id"], request))["status"] == "verified"
    assert len(state.requests) == count


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("409", "verified"),
        ("409_wrong", "unknown"),
        ("409_mismatch", "conflict"),
        ("412", "conflict"),
    ],
)
@pytest.mark.asyncio
async def test_conflicts_get_exact_original_id_without_reposting(protocol, mode, expected):
    env, state = protocol
    state.mode = mode
    action = await execute(env)
    assert action["status"] == expected
    assert sum(row[0] == "POST" for row in state.requests) == 1
    assert sum(row[0] == "GET" for row in state.requests) == (0 if mode == "412" else 1)
    if expected == "conflict":
        record = await env.service._record("action", action["id"])
        run = await env.engine.chat_run_store.get_run(
            record["execution"]["session_id"], action["run_id"]
        )
        assert run.status == "error" and run.receipt.outcome == "failed"


@pytest.mark.asyncio
async def test_revoke_before_send_fences_and_zero_provider_writes(protocol):
    env, state = protocol
    env.service.token_entered, env.service.token_release = asyncio.Event(), asyncio.Event()
    draft = await prepare(env)
    await approve(env, draft)
    env.service._closing = False
    await env.service.action_tick()
    await asyncio.wait_for(env.service.token_entered.wait(), 3)
    await env.service._write(
        lambda tx: env.service._change(
            tx, "connection", CONNECTION, {"status": "disconnected", "auth_epoch": 2}
        )
    )
    env.service.token_release.set()
    await settle(env)
    assert state.requests == []
    assert (await env.service.get_action(draft["id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_revoke_after_dispatch_unknown_manual_same_subject_reauthorization_only(protocol):
    env, state = protocol
    state.mode = "block"
    draft = await prepare(env)
    await approve(env, draft)
    env.service._closing = False
    await env.service.action_tick()
    assert await asyncio.to_thread(state.post_entered.wait, 3)
    await env.service._write(
        lambda tx: env.service._change(
            tx, "connection", CONNECTION, {"status": "disconnected", "auth_epoch": 2}
        )
    )
    state.post_release.set()
    await settle(env)
    action = await env.service.get_action(draft["id"])
    assert action["status"] == "unknown" and len(state.requests) == 1
    request = {"request_id": uuid.uuid4().hex, "expected_revision": action["revision"]}
    assert (await env.service.reconcile_action(action["id"], request))["status"] == "unknown"
    assert len(state.requests) == 1
    await env.service._write(
        lambda tx: env.service._change(
            tx, "connection", CONNECTION, {"status": "connected", "auth_epoch": 3}
        )
    )
    action = await env.service.get_action(action["id"])
    with pytest.raises(PersonalAgentError):
        await env.service.reconcile_action(
            action["id"],
            {"request_id": uuid.uuid4().hex, "expected_revision": action["revision"]},
            caller_actor="main",
        )
    state.mode = "ok"
    result = await env.service.reconcile_action(
        action["id"], {"request_id": uuid.uuid4().hex, "expected_revision": action["revision"]}
    )
    assert result["status"] == "verified" and result["auth_epoch"] == 1
    assert sum(row[0] == "POST" for row in state.requests) == 1


@pytest.mark.asyncio
async def test_missing_job_after_admission_attempt_never_readmits(protocol):
    env, state = protocol
    draft = await prepare(env)
    await approve(env, draft)
    value = await env.service._record("action", draft["id"])
    await env.service._write(
        lambda tx: env.service._change(
            tx,
            "action",
            value["id"],
            {"execution": {**value["execution"], "admission": "attempted"}},
        )
    )
    env.service._closing = False
    await settle(env)
    assert (await env.service.get_action(value["id"]))["status"] == "unknown"
    assert await env.engine.personal_collaborations.jobs.get(value["execution"]["job_id"]) is None
    assert state.requests == []


@pytest.mark.asyncio
async def test_queued_expiry_and_shared_capacity_do_not_bypass_fifo(protocol):
    env, state = protocol
    env.service.token_entered, env.service.token_release = asyncio.Event(), asyncio.Event()
    first, second = await prepare(env), await prepare(env)
    await approve(env, first)
    await approve(env, second)
    env.service._closing = False
    await env.service.action_tick()
    await asyncio.wait_for(env.service.token_entered.wait(), 3)
    second_value = await env.service._record("action", second["id"])
    # Exactly one claimed/running Job; the other remains durably admitted.
    jobs = [
        await env.engine.personal_collaborations.jobs.get(
            (await env.service._record("action", a["id"]))["execution"]["job_id"]
        )
        for a in (first, second)
    ]
    assert sum(job.state is AgentJobState.ADMITTED for job in jobs) == 1
    env.clock[0] += timedelta(hours=2)
    env.service.token_release.set()
    await settle(env)
    await settle(env)
    assert state.requests == []
    assert (await env.service.get_action(second_value["id"]))["status"] in {
        "expired",
        "cancelled",
        "unknown",
    }


@pytest.mark.asyncio
async def test_expired_missing_admission_proof_stays_unknown_not_unaccepted(protocol):
    env, state = protocol
    draft = await prepare(env)
    await approve(env, draft)
    value = await env.service._record("action", draft["id"])
    await env.service._write(
        lambda tx: env.service._change(
            tx,
            "action",
            value["id"],
            {"execution": {**value["execution"], "admission": "attempted"}},
        )
    )
    env.clock[0] += timedelta(hours=2)
    env.service._closing = False
    await settle(env)
    assert (await env.service.get_action(value["id"]))["status"] == "unknown"
    assert state.requests == []


@pytest.mark.asyncio
async def test_real_update_uses_frozen_etag_and_original_target(protocol):
    env, state = protocol
    draft = await prepare(
        env, operation="update", target_event_id="source_event", base_etag='"original-etag"'
    )
    state.events["source_event"] = original_target(draft)
    action = await execute(env, draft)
    assert action["status"] == "verified"
    assert [request[0] for request in state.requests] == ["GET", "PATCH", "GET"]
    assert state.requests[1][2]["If-Match"] == '"original-etag"'
    assert state.requests[1][1].split("?")[0].endswith("source_event")


@pytest.mark.asyncio
async def test_lost_approval_response_replays_original_job_without_second_write(protocol):
    env, state = protocol
    draft = await prepare(env)
    payload = {
        "request_id": uuid.uuid4().hex,
        "expected_revision": draft["revision"],
        "expected_digest": draft["digest"],
    }
    await env.service.approve_action(draft["id"], payload, trusted_channel="manual_api")
    env.service._closing = False
    await settle(env)
    first = await env.service.get_action(draft["id"])
    second = await env.service.approve_action(draft["id"], payload, trusted_channel="manual_api")
    assert first["job_id"] == second["job_id"] and first["run_id"] == second["run_id"]
    assert sum(request[0] == "POST" for request in state.requests) == 1


@pytest.mark.asyncio
async def test_cold_recovery_dispatch_marker_never_reposts_and_original_run_unknown(protocol):
    env, state = protocol
    draft = await prepare(env)
    await approve(env, draft)
    value = await env.service._record("action", draft["id"])
    intent = value["execution"]
    executor = env.service._calendar_action_executor()
    transition = await executor.jobs.admit_for_capacity(
        request=action_request(intent),
        payload=AgentJobPayload(**intent["payload"]),
        owner_id=executor.owner,
        lease_seconds=90,
        max_active_jobs=1,
        max_waiters=10,
    )
    job = transition.job
    await executor._session_run(value)
    await executor.jobs.mark_running(
        job.job_id, owner_id=executor.owner, claim_epoch=job.claim_epoch
    )
    await env.service._write(
        lambda tx: env.service._change(
            tx,
            "action",
            value["id"],
            {
                "status": "dispatching",
                "job_id": job.job_id,
                "dispatch": {"job_id": job.job_id},
                "execution": {**intent, "admission": "bound", "job_bound": True},
            },
        )
    )
    env.clock[0] += timedelta(seconds=91)
    recovered = FixtureService(
        env.engine,
        store=env.service.store,
        vault=object(),
        adapter=env.service.adapter,
        clock=lambda: env.clock[0],
    )
    recovered.source_revision = 1
    recovered.source_revoked = False
    recovered.token_entered = recovered.token_release = None
    await recovered.action_tick()
    assert (await recovered.get_action(draft["id"]))["status"] == "unknown"
    assert (await executor.jobs.get(job.job_id)).state is AgentJobState.UNKNOWN
    run = await env.engine.chat_run_store.get_run(intent["session_id"], intent["run_id"])
    assert run.status == "unknown" and run.receipt.outcome == "partial"
    await recovered.action_tick()
    assert state.requests == []
    await recovered.stop_actions()


@pytest.mark.asyncio
async def test_background_exception_is_observed_without_erasing_provider_proof(
    protocol, monkeypatch
):
    env, state = protocol
    original = env.engine.chat_run_store.finish_run

    async def unavailable(*args, **kwargs):
        raise RuntimeError("fixture-storage-detail-not-for-public")

    monkeypatch.setattr(env.engine.chat_run_store, "finish_run", unavailable)
    draft = await prepare(env)
    await approve(env, draft)
    env.service._closing = False
    await env.service.action_tick()
    executor = env.service._calendar_action_executor()
    await asyncio.gather(*executor.active.values(), return_exceptions=True)
    monkeypatch.setattr(env.engine.chat_run_store, "finish_run", original)
    await env.service.action_tick()
    value = await env.service._record("action", draft["id"])
    assert value["status"] == "verified"
    assert value["execution_observation"]["error_code"] == "background_execution_error"
    assert (await env.service.get_action(value["id"]))[
        "last_error_code"
    ] == "background_execution_error"
    assert "fixture-storage" not in json.dumps(await env.service.get_action(value["id"]))
    assert [request[0] for request in state.requests] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_two_service_workers_claim_one_existing_job_and_single_provider_write(protocol):
    env, state = protocol
    draft = await prepare(env)
    await approve(env, draft)
    sibling = FixtureService(
        env.engine,
        store=env.service.store,
        vault=object(),
        adapter=env.service.adapter,
        clock=lambda: env.clock[0],
    )
    sibling.source_revision = 1
    sibling.source_revoked = False
    sibling.token_entered = sibling.token_release = None
    env.service._closing = False
    await asyncio.gather(env.service.action_tick(), sibling.action_tick())
    tasks = [
        *env.service._calendar_action_executor().active.values(),
        *sibling._calendar_action_executor().active.values(),
    ]
    assert tasks
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert not any(isinstance(outcome, BaseException) for outcome in outcomes), outcomes
    await asyncio.gather(env.service.action_tick(), sibling.action_tick())
    action = await env.service.get_action(draft["id"])
    assert action["status"] == "verified"
    assert [request[0] for request in state.requests] == ["POST", "GET"]
    await sibling.stop_actions()


@pytest.mark.asyncio
async def test_same_service_stop_start_recreates_executor_and_runs_new_approved_action(protocol):
    env, state = protocol
    old = env.service._calendar_action_executor()
    env.service._closing = True
    await env.service.stop_actions()
    assert not hasattr(env.service, "_calendar_actions")
    action = await execute(env)
    assert env.service._calendar_action_executor() is not old
    assert action["status"] == "verified"
    assert [request[0] for request in state.requests] == ["POST", "GET"]


def original_target(action):
    return {
        "id": "source_event",
        "etag": '"original-etag"',
        "status": "confirmed",
        "summary": "原日程",
        "description": "原正文",
        "start": copy.deepcopy(action["body"]["start"]),
        "end": copy.deepcopy(action["body"]["end"]),
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("attendees", [{"email": "extra@example.invalid"}]),
        ("attachments", [{"fileId": "fixture-file"}]),
        ("conferenceData", {"conferenceId": "fixture-conference"}),
        ("hangoutLink", "https://meet.google.com/fixture"),
        ("eventType", "outOfOffice"),
        ("recurrence", ["RRULE:FREQ=DAILY"]),
        ("recurringEventId", "fixture-series"),
        ("status", "cancelled"),
        ("etag", '"not-the-approved-version"'),
    ],
)
@pytest.mark.asyncio
async def test_update_raw_target_preflight_blocks_unapproved_original_before_patch(
    protocol, field, value
):
    env, state = protocol
    draft = await prepare(
        env, operation="update", target_event_id="source_event", base_etag='"original-etag"'
    )
    state.events["source_event"] = {**original_target(draft), field: value}
    action = await execute(env, draft)
    assert sum(request[0] == "PATCH" for request in state.requests) == 0
    assert [request[0] for request in state.requests] == ["GET"]
    assert action["status"] == "conflict"
    internal = await env.service._record("action", action["id"])
    assert not internal.get("dispatch")
    assert action["last_error_code"] == (
        "preflight_etag_conflict" if field == "etag" else "preflight_unsupported_target"
    )
    job = await env.engine.personal_collaborations.jobs.get(action["job_id"])
    assert job.state is AgentJobState.ERROR


@pytest.mark.parametrize("mode", ["get403", "get404", "get503"])
@pytest.mark.asyncio
async def test_update_failed_raw_get_keeps_original_job_error_and_proves_zero_patch(protocol, mode):
    env, state = protocol
    draft = await prepare(
        env, operation="update", target_event_id="source_event", base_etag='"original-etag"'
    )
    state.events["source_event"] = original_target(draft)
    state.mode = mode
    action = await execute(env, draft)
    assert action["status"] == "conflict" and action["last_error_code"] == "preflight_read_failed"
    assert [request[0] for request in state.requests] == ["GET"]
    record = await env.service._record("action", action["id"])
    assert not record.get("dispatch") and record["preflight"]["phase"] == "before_patch"
    job = await env.engine.personal_collaborations.jobs.get(action["job_id"])
    run = await env.engine.chat_run_store.get_run(
        record["execution"]["session_id"], action["run_id"]
    )
    assert job.state is AgentJobState.ERROR and run.status == "error"


@pytest.mark.asyncio
async def test_update_etag_change_after_raw_get_is_one_conditional_patch_412_no_retry(protocol):
    env, state = protocol
    draft = await prepare(
        env, operation="update", target_event_id="source_event", base_etag='"original-etag"'
    )
    state.events["source_event"] = original_target(draft)
    state.mode = "etag_race"
    action = await execute(env, draft)
    assert action["status"] == "conflict" and action["last_error_code"] == "precondition_failed"
    assert [request[0] for request in state.requests] == ["GET", "PATCH"]
    assert state.requests[1][2]["If-Match"] == '"original-etag"'
    assert state.events["source_event"]["summary"] == "原日程"
    await env.service.action_tick()
    assert len(state.requests) == 2
