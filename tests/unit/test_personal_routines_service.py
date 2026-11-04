"""Real routine/Item/Job/Run integration with explicitly controlled zero-paid transport."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.agent_jobs import AgentJobPayload
from naumi_agent.model.router import ModelRouter, StreamChunk
from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.item_execution import _request
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.routines import PersonalRoutineService
from naumi_agent.runtime.composition import create_agent_engine

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
MODEL = "routine-integration-fixture"


def definition(**updates):
    value = {
        "title": "资料核对",
        "instructions": "核对给定资料，输出有依据的报告。",
        "assignee_id": "main",
        "assignee_version": 0,
        "tags": ["工作"],
        "schedule": {"kind": "cron", "expression": "* * * * *", "timezone": "UTC"},
        "policy": {
            "tool_names": ["personal_artifacts"],
            "artifact_operations": ["get", "read", "publish"],
            "max_turns": 3,
            "max_cost_microusd": 100000,
            "timeout_seconds": 30,
        },
        "limits": {
            "max_executions": 10,
            "total_cost_microusd": 1000000,
            "max_concurrent": 1,
            "max_pending": 10,
        },
        "recovery": {
            "misfire_policy": "catch_up",
            "grace_seconds": 10,
            "max_catch_up": 4,
            "overlap_policy": "queue",
        },
        "input_refs": [],
        "directories": [],
    }
    value.update(updates)
    return value


@pytest.fixture
async def system(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    calls = []

    async def forbidden(self, **kwargs):
        raise AssertionError("No external model request belongs to this fixture")

    async def stream(self, **kwargs):
        calls.append(kwargs)
        hook = getattr(system_state, "transport_hook", None)
        if hook is not None:
            await hook()
        raw = SimpleNamespace(prompt_tokens=12, completion_tokens=5)
        usage = self._build_usage(raw, self.resolve_model(kwargs.get("tier", "capable")))
        yield StreamChunk(
            token="实际输入已经核对，缺少凭证的结论保持未知。", finish_reason="stop", usage=usage
        )

    monkeypatch.setattr(ModelRouter, "call", forbidden)
    monkeypatch.setattr(ModelRouter, "stream", stream)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("实际本地资料：收入 120，支出 35。", encoding="utf-8")
    for argv in (
        ["init", "-q"],
        ["add", "source.txt"],
        [
            "-c",
            "user.name=Routine Fixture",
            "-c",
            "user.email=routine@example.invalid",
            "commit",
            "-qm",
            "Record source",
        ],
    ):
        subprocess.run(["git", "-C", str(workspace), *argv], check=True, capture_output=True)
    config = AppConfig(
        workspace_root=str(workspace),
        personal={"data_dir": str(tmp_path / "personal")},
        memory={
            "session_db_path": str(tmp_path / "runtime" / "sessions.db"),
            "vector_db_path": str(tmp_path / "vectors"),
            "long_term_enabled": False,
        },
        models={
            "default_model": MODEL,
            "fast_model": MODEL,
            "reasoning_model": MODEL,
            "model_info": {
                MODEL: {
                    "max_context": 200000,
                    "max_output": 512,
                    "input_cost_per_million": 1,
                    "output_cost_per_million": 2,
                }
            },
        },
        safety={"permission_mode": "bypass", "max_turns": 3},
    )
    engine = create_agent_engine(config)
    clock = [datetime.now(UTC).replace(second=1, microsecond=0) - timedelta(days=1)]
    engine.personal_routines.clock = lambda: clock[0]
    engine.personal_items.clock = lambda: clock[0]
    engine.scheduler_runner._now_fn = lambda: clock[0]
    system_state = SimpleNamespace(
        engine=engine,
        routines=engine.personal_routines,
        items=engine.personal_items,
        clock=clock,
        calls=calls,
        config=config,
        workspace=workspace,
    )
    try:
        yield system_state
    finally:
        await engine.shutdown()


async def create(system, raw=None, *, enabled=True, prefix=None):
    prefix = prefix or uuid.uuid4().hex
    preview = await system.routines.preview(
        {"request_id": prefix + ":preview", "definition": raw or definition()}
    )
    payload = {
        "request_id": prefix + ":create",
        "preview_id": preview["preview_id"],
        "expected_digest": preview["definition_digest"],
        "enabled": enabled,
    }
    return await system.routines.create(payload), payload


async def rows(system, identity):
    return (await system.routines.occurrences(identity))["occurrences"]


async def reserve(system, routine):
    system.clock[0] += timedelta(seconds=60)
    await system.routines.tick()
    return await rows(system, routine["id"])


async def drain(system, identity):
    for _ in range(12):
        await system.routines.tick()
        system.engine.scheduler_runner.tick_due()
        await system.items.tick()
        active = list(system.items.executor._active.values())
        if active:
            await asyncio.wait(active, timeout=30)
        values = await rows(system, identity)
        if values and all(
            row["status"] in {"settled", "skipped", "blocked", "unknown"} for row in values
        ):
            return values
        await asyncio.sleep(0.02)
    raise AssertionError(f"Original occurrences did not settle: {values}")


async def control(system, routine, action):
    return await system.routines.control(
        routine["id"],
        action,
        {"request_id": uuid.uuid4().hex, "expected_revision": routine["revision"]},
    )


async def revise(system, routine, raw):
    preview = await system.routines.preview(
        {
            "request_id": uuid.uuid4().hex,
            "routine_id": routine["id"],
            "expected_revision": routine["revision"],
            "definition": raw,
        }
    )
    return await system.routines.revise(
        routine["id"],
        {
            "request_id": uuid.uuid4().hex,
            "preview_id": preview["preview_id"],
            "expected_digest": preview["definition_digest"],
            "expected_revision": routine["revision"],
        },
    )


async def test_preview_is_not_a_routine_create_replay_and_cas_are_durable(system):
    routine, payload = await create(system)
    assert await system.routines.create(payload) == routine
    assert len((await system.routines.list())["routines"]) == 1
    assert system.engine.personal_agents.list() == ()
    assert routine["revision"] == 1 and routine["budget"]["remaining_slots"] == 10
    receipt = await system.routines.request_status(payload["request_id"])
    assert receipt["result_id"] == routine["id"]
    with pytest.raises(PersonalAgentError) as caught:
        await system.routines.create({**payload, "enabled": False})
    assert caught.value.code == "idempotency_conflict"
    paused = await control(system, routine, "pause")
    assert paused["revision"] == 2 and paused["status"] == "paused"
    with pytest.raises(PersonalAgentError) as caught:
        await control(system, routine, "resume")
    assert caught.value.code == "version_conflict"
    assert not system.calls


async def test_two_nominal_occurrences_produce_original_jobs_runs_reports_and_inbox(system):
    routine, _ = await create(system)
    await reserve(system, routine)
    first = await drain(system, routine["id"])
    assert len(first) == 1 and first[0]["status"] == "settled", first
    assert first[0]["child"]["occurrence"]["status"] == "completed"
    assert first[0]["child"]["occurrence"]["artifacts"]
    assert first[0]["child"]["occurrence"]["delivery_id"]
    assert first[0]["child"]["occurrence"]["read_receipt"] is None
    await reserve(system, routine)
    both = await drain(system, routine["id"])
    assert len(both) == 2 and all(row["status"] == "settled" for row in both)
    assert len({row["id"] for row in both}) == len({row["item_id"] for row in both}) == 2
    assert len({row["child"]["occurrence"]["job_id"] for row in both}) == 2
    assert len({row["child"]["occurrence"]["run_id"] for row in both}) == 2
    assert len(system.calls) == 2
    current = await system.routines.get(routine["id"])
    assert current["revision"] == 1 and current["budget"]["started_executions"] == 2
    assert current["budget"]["reserved_slots"] == current["budget"]["unknown_count"] == 0
    assert current["budget"]["settled_cost_microusd"] > 0
    for _ in range(3):
        await system.routines.tick()
        await system.items.tick()
    assert len(system.calls) == 2


@pytest.mark.parametrize("action", ["pause", "cancel", "new_epoch"])
async def test_stopped_unattempted_occurrences_release_only_proven_zero_admission(system, action):
    routine, _ = await create(system)
    original = (await reserve(system, routine))[0]
    assert original["item_id"] is None
    if action == "new_epoch":
        changed = await revise(
            system,
            routine,
            definition(schedule={"kind": "cron", "expression": "*/5 * * * *", "timezone": "UTC"}),
        )
        assert changed["epoch"] != routine["epoch"]
    else:
        await control(system, routine, action)
    await system.routines.tick()
    stopped = await system.routines.occurrence(original["id"])
    assert stopped["status"] == "skipped" and stopped["item_id"] is None
    reservation = await system.routines._record("budget", "reservation:" + original["id"])
    assert reservation["settlement"] == "prestart_cancelled"
    assert reservation["known_cost_microusd"] == 0 and not system.calls


@pytest.mark.parametrize("code", ["storage_error", "invalid_input"])
async def test_crash_after_item_commit_before_link_recovers_original_request_without_create(
    system, code
):
    routine, _ = await create(system)
    await reserve(system, routine)
    original_bind = system.routines._bind_item

    async def crash(row, receipt):
        raise PersonalAgentError(code, "Controlled crash after atomic Item receipt")

    system.routines._bind_item = crash
    if code == "storage_error":
        with pytest.raises(PersonalAgentError):
            await system.routines.tick()
    else:
        await system.routines.tick()
    system.routines._bind_item = original_bind
    raw = await system.routines._record("occurrence", (await rows(system, routine["id"]))[0]["id"])
    receipt = await system.items._call("find_request", request_id=raw["item_request_id"])
    assert raw["item_id"] is None and receipt is not None
    if code != "storage_error":
        assert raw["status"] == "unknown"

    async def forbidden_create(*args, **kwargs):
        raise AssertionError("Recovery must use the original receipt")

    system.items.create = forbidden_create
    await control(system, routine, "pause")
    await system.routines.tick()
    current = await system.routines._record("occurrence", raw["id"])
    assert current["item_id"] == receipt["committed_item"]["item_id"]
    assert len((await system.items.list())["items"]) == 1 and not system.calls


async def test_attempted_unlinked_unknown_never_releases_fee_or_replays_while_paused(system):
    routine, _ = await create(system)
    occurrence = (await reserve(system, routine))[0]

    def attempt(tx):
        row = tx.get("occurrence", occurrence["id"])
        tx.put(
            "occurrence", {**row, "item_create_attempted": True}, expected_revision=row["revision"]
        )

    await system.routines._write(attempt)
    await control(system, routine, "pause")
    await system.routines.tick()
    assert (await system.routines.occurrence(occurrence["id"]))["status"] == "unknown"
    reservation = await system.routines._record("budget", "reservation:" + occurrence["id"])
    assert reservation["known_cost_microusd"] is None
    assert reservation["settlement"] == "unknown" and not system.calls


async def test_revise_title_keeps_epoch_budget_and_old_occurrence_definition(system):
    routine, _ = await create(system)
    occurrence = (await reserve(system, routine))[0]
    revised = await revise(system, routine, definition(title="修订标题"))
    assert revised["epoch"] == routine["epoch"] and revised["revision"] == 2
    assert revised["budget"]["reserved_slots"] == 1
    await system.routines.tick()
    linked = await system.routines.occurrence(occurrence["id"])
    assert linked["definition_revision"] == 1
    assert linked["child"]["definition"]["title"] == "资料核对"


@pytest.mark.parametrize("field", ["max_executions", "total_cost_microusd"])
async def test_zero_limits_are_real_prohibitions(system, field):
    raw = definition()
    raw["limits"][field] = 0
    routine, _ = await create(system, raw)
    assert await reserve(system, routine) == []
    current = await system.routines.get(routine["id"])
    assert current["attention_code"] == "budget_exhausted"
    assert not system.calls and (await system.items.list())["items"] == []


async def test_concurrent_workers_have_one_nominal_identity_and_atomic_admission(system):
    raw = definition()
    raw["recovery"]["overlap_policy"] = "allow"
    routine, _ = await create(system, raw)
    system.clock[0] += timedelta(minutes=3)
    other = PersonalRoutineService(system.engine, clock=lambda: system.clock[0])
    await asyncio.gather(system.routines.tick(), other.tick())
    reserved = await rows(system, routine["id"])
    assert len(reserved) == 3 and len({row["id"] for row in reserved}) == 3
    original = await system.routines._routine(routine["id"])
    raw_rows = [await system.routines._record("occurrence", row["id"]) for row in reserved]
    await asyncio.gather(
        system.routines._dispatch_occurrence(raw_rows[0], original),
        other._dispatch_occurrence(raw_rows[1], original),
        return_exceptions=True,
    )
    current = await rows(system, routine["id"])
    assert len([row for row in current if row["item_id"]]) == 1
    assert len((await system.items.list())["items"]) == 1 and not system.calls


async def test_lost_bridge_is_fenced_and_cleanup_does_not_release_successor(system):
    routine, _ = await create(system)
    await reserve(system, routine)
    original = system.routines.create_occurrence_guard

    async def expire(row, historical):
        value = await original(row, historical)
        system.clock[0] += timedelta(seconds=301)
        await system.routines._write(
            lambda tx: tx.claim(
                "bridge", row["id"], "successor", now=system.clock[0].timestamp(), ttl=300
            )
        )
        return value

    system.routines.create_occurrence_guard = expire
    await system.routines.tick()
    row = (await rows(system, routine["id"]))[0]
    lease = await system.routines._read(lambda tx: tx.lease("bridge", row["id"]))
    assert lease["holder"] == "successor" and lease["phase"] == "claimed"
    raw = await system.routines._record("occurrence", row["id"])
    assert not raw["item_create_attempted"] and raw["item_id"] is None
    assert (await system.items.list())["items"] == [] and not system.calls


async def test_cold_service_reopen_preserves_instance_budget_and_original_occurrence(system):
    routine, _ = await create(system)
    original = await reserve(system, routine)
    reopened = PersonalRoutineService(system.engine, clock=lambda: system.clock[0])
    assert await reopened.get(routine["id"]) == await system.routines.get(routine["id"])
    await reopened.tick()
    after = await reopened.occurrences(routine["id"])
    assert [row["id"] for row in after["occurrences"]] == [row["id"] for row in original]
    assert (await reopened.get(routine["id"]))["budget"]["reserved_slots"] == 1


async def test_external_instance_pin_rejects_deleted_database_and_witness(system):
    await create(system)
    base = system.routines.store.base_dir
    assert system.routines.boundary.is_file()
    shutil.rmtree(base)
    reopened = PersonalRoutineService(system.engine, clock=lambda: system.clock[0])
    with pytest.raises(PersonalAgentError) as caught:
        await reopened.list()
    assert caught.value.code == "storage_error" and not base.exists()


async def test_opaque_model_lists_and_real_pinned_plan_material(system):
    routine, _ = await create(system)
    model = await system.routines.list(caller_actor="main")
    assert "definition" not in model["routines"][0]
    session = await system.engine.get_or_create_session()
    public = await system.routines.get(routine["id"], caller_actor="main")
    assert await system.routines.pin_plan_read(public, "main") == public
    refs = await system.engine.personal_artifacts.session_refs(session.id, "main")
    assert refs
    ref = refs[-1]
    snapshot, content = await system.engine.personal_artifacts.read(
        actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
    )
    assert snapshot["evidence"]["routine_plan_refs"][0]["routine_id"] == routine["id"]
    assert strict_json_loads(content)["definition"]["instructions"] == (
        "核对给定资料，输出有依据的报告。"
    )


async def test_pause_during_real_running_job_allows_that_original_job_to_finish(system):
    routine, _ = await create(system)

    async def pause():
        current = await system.routines.get(routine["id"])
        await control(system, current, "pause")

    system.transport_hook = pause
    await reserve(system, routine)
    values = await drain(system, routine["id"])
    assert values[0]["status"] == "settled", values
    assert values[0]["child"]["occurrence"]["status"] == "completed"
    assert len(system.calls) == 1
    assert (await system.routines.get(routine["id"]))["status"] == "paused"


async def test_unknown_transport_holds_budget_and_never_replays_original_request(system):
    routine, _ = await create(system)

    async def disconnect():
        raise ConnectionError("Controlled transport outcome unknown")

    system.transport_hook = disconnect
    await reserve(system, routine)
    values = await drain(system, routine["id"])
    assert values[0]["status"] == "unknown", values
    assert len(system.calls) == 1
    for _ in range(3):
        await system.routines.tick()
        await system.items.tick()
    current = await system.routines.get(routine["id"])
    assert current["budget"]["unknown_count"] == 1
    assert current["budget"]["reserved_cost_microusd"] == 100000 and len(system.calls) == 1


async def test_settlement_uses_internal_job_receipts_not_redacted_ui_usage(system):
    routine, _ = await create(system)
    await reserve(system, routine)
    await system.routines.tick()
    system.engine.scheduler_runner.tick_due()
    await system.items.tick()
    for task in list(system.items.executor._active.values()):
        await task
    original_view = system.items.executor.view

    async def redacted(intent):
        value = await original_view(intent)
        value.update(usage_known=False, usage=None, answer="", artifacts=[])
        return value

    system.items.executor.view = redacted
    await system.routines.tick()
    values = await rows(system, routine["id"])
    assert values[0]["status"] == "settled" and values[0]["settled_cost_microusd"] > 0
    raw = await system.routines._record("occurrence", values[0]["id"])
    item = await system.items._call("get", item_id=raw["item_id"])
    intent = await system.items._current_intent(item)
    assert _request(intent).request_sha256
    reservation = await system.routines._record("budget", "reservation:" + raw["id"])
    assert reservation["evidence"]["result_sha256"]
    assert reservation["evidence"]["receipt_sha256"]


@pytest.mark.parametrize("raw", [True, [], {"title": ""}, {"title": "x", "trusted": True}])
async def test_invalid_definitions_do_not_create_partial_journals(system, raw):
    with pytest.raises(PersonalAgentError):
        await system.routines.preview({"request_id": "invalid-input", "definition": raw})
    assert not system.routines.store.db_path.exists() and not system.calls


async def test_history_has_current_budget_and_keyset_pagination_has_no_missing_ids(system):
    created = [(await create(system))[0] for _ in range(3)]
    identities, after = [], None
    while True:
        page = await system.routines.list(limit=1, after=after)
        identities.extend(row["id"] for row in page["routines"])
        after = page["next_after"]
        if after is None:
            break
    assert identities == sorted(row["id"] for row in created)
    routine = created[0]
    await reserve(system, routine)
    changed = definition(title="新的标题")
    changed["limits"]["max_executions"] = 12
    await revise(system, routine, changed)
    history = await system.routines.history(routine["id"], limit=1)
    assert history["next_after"] == 1 and history["history"][0]["revision"] == 1
    assert history["history"][0]["budget"]["remaining_slots"] == 11


@pytest.mark.parametrize("constraint", ["pending", "executions", "cost"])
async def test_catch_up_commits_legal_partial_batch_and_advances_only_those_slots(
    system, constraint
):
    raw = definition()
    if constraint == "pending":
        raw["limits"]["max_pending"] = 1
    elif constraint == "executions":
        raw["limits"]["max_executions"] = 1
    else:
        raw["limits"]["total_cost_microusd"] = 100000
    routine, _ = await create(system, raw)
    system.clock[0] += timedelta(minutes=4)
    await system.routines.tick()
    values = await rows(system, routine["id"])
    assert len(values) == 1 and values[0]["status"] == "reserved"
    current = await system.routines.get(routine["id"])
    assert current["budget"]["reserved_slots"] == 1
    assert current["attention_code"] in {"routine_queue_full", "budget_exhausted"}
    assert datetime.fromisoformat(current["next_due_at"]) == (
        datetime.fromisoformat(values[0]["nominal_due_at"]) + timedelta(minutes=1)
    )
    assert not system.calls


async def test_pending_limit_reduction_drains_old_reservations_without_storage_corruption(system):
    routine, _ = await create(system)
    system.clock[0] += timedelta(minutes=2)
    await system.routines.tick()
    assert len(await rows(system, routine["id"])) == 2
    raw = definition()
    raw["limits"]["max_pending"] = 1
    await revise(system, routine, raw)
    system.clock[0] += timedelta(minutes=1)
    await system.routines.tick()
    current = await system.routines.get(routine["id"])
    assert current["attention_code"] == "routine_queue_full"
    assert len(await rows(system, routine["id"])) == 2


async def test_parallel_create_and_manual_extra_run_preserve_one_original_request(system):
    routine, payload = await create(system)
    values = await asyncio.gather(*(system.routines.create(payload) for _ in range(8)))
    assert {value["id"] for value in values} == {routine["id"]}
    command = {"request_id": "manual-extra-original", "expected_revision": routine["revision"]}
    first = await system.routines.control(routine["id"], "extra-run", command)
    system.clock[0] += timedelta(hours=1)
    repeated = await system.routines.control(routine["id"], "extra-run", command)
    assert repeated == first
    assert (await system.routines.get(routine["id"]))["budget"]["reserved_slots"] == 1


async def test_cancel_linked_item_without_job_keeps_an_explicit_unknown_hold(system):
    routine, _ = await create(system)
    await reserve(system, routine)
    await system.routines.tick()
    await control(system, routine, "cancel")
    await system.routines.tick()
    current = (await rows(system, routine["id"]))[0]
    assert current["status"] == "unknown" and current["settled_cost_microusd"] is None
    assert not system.calls


async def test_unknown_after_mark_running_before_first_transport_counts_original_start(system):
    routine, _ = await create(system)
    await reserve(system, routine)
    await system.routines.tick()
    system.items.executor._closing = True
    system.engine.scheduler_runner.tick_due()
    await system.items.tick()
    system.items.executor._closing = False
    occurrence = (await rows(system, routine["id"]))[0]
    child = await system.items._call("get", item_id=occurrence["item_id"])
    intent = await system.items._current_intent(child)
    assert intent is not None
    jobs = system.items.executor.jobs
    original = await jobs.admit(
        request=_request(intent), payload=AgentJobPayload(**intent["payload"])
    )
    system.items.executor._bind(intent, job_id=original.job_id)
    claimed = (await jobs.claim(original.job_id, owner_id="crashed-owner", lease_seconds=90)).job
    running = (
        await jobs.mark_running(
            original.job_id, owner_id="crashed-owner", claim_epoch=claimed.claim_epoch
        )
    ).job
    await jobs.mark_execution_unknown(
        original.job_id,
        owner_id="crashed-owner",
        claim_epoch=running.claim_epoch,
        expected_request_sha256=running.request_sha256,
        expected_session_id_sha256=running.request.session_id_sha256,
        expected_latest_receipt_sha256=running.latest_receipt.receipt_sha256,
    )
    await system.routines.tick()
    current = await system.routines.get(routine["id"])
    assert current["budget"]["started_executions"] == 1
    assert current["budget"]["reserved_slots"] == 0
    assert current["budget"]["unknown_count"] == 1
    assert current["budget"]["reserved_cost_microusd"] == 100000
    assert not system.calls


async def test_full_app_routine_middleware_errors_are_not_cacheable():
    from naumi_agent.api.app import create_app

    app = create_app()
    app.state.config = SimpleNamespace(
        api=SimpleNamespace(api_keys=["fixture-key"], rate_limit_rpm=1, cors_origins=[])
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/api/v1/personal/routines/context")
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"
        first = await client.get(
            "/api/v1/personal/routines/requests/unknown-request",
            headers={"X-API-Key": "fixture-key"},
        )
        assert first.status_code == 503
        limited = await client.get(
            "/api/v1/personal/routines/context", headers={"X-API-Key": "fixture-key"}
        )
        assert limited.status_code == 429
        assert limited.headers["Cache-Control"] == "no-store"
        assert limited.headers["Pragma"] == "no-cache"


@pytest.mark.parametrize("case", ["unconfigured", "query_key", "valid_header"])
async def test_web_slash_gateway_does_not_weaken_routine_header_authentication(case):
    from naumi_agent.api.app import create_app

    access = []

    async def load(identity):
        access.append(identity)
        return None

    app = create_app()
    app.state.config = SimpleNamespace(
        api=SimpleNamespace(
            api_keys=[] if case == "unconfigured" else ["fixture-key"],
            rate_limit_rpm=1000,
            cors_origins=[],
        )
    )
    app.state.engine = SimpleNamespace(session_store=SimpleNamespace(load=load))
    query = "?api_key=fixture-key" if case == "query_key" else ""
    headers = {"X-API-Key": "fixture-key"} if case == "valid_header" else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/api/v1/sessions/missing-session/commands" + query,
            headers=headers,
            json={"command": "/routines context"},
        )
    assert (
        response.status_code == {"unconfigured": 503, "query_key": 401, "valid_header": 404}[case]
    )
    assert len(access) == (1 if case == "valid_header" else 0)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
