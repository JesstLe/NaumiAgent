"""Real Today/source/session provenance, with controlled local transport only for delivery."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.today import router as today_router
from naumi_agent.config.settings import AppConfig
from naumi_agent.memory.session import Session
from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.personal import runtime
from naumi_agent.personal.item_models import canonical
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today import PersonalTodayService
from naumi_agent.personal.today_models import day_window, item_ref, metadata_identity
from naumi_agent.personal.today_tools import PersonalTodayTool
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.base import ToolExecutionError

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
PRIVATE = "TODAY_PRIVATE_SHARED_MATERIAL_2030"


@pytest.fixture
async def authority_system(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("NAUMI_PERSONAL__DATA_DIR", str(tmp_path / "personal"))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    async def forbidden_call(self, **kwargs):
        raise AssertionError("No real model/provider is allowed in Today authority tests")

    async def forbidden_stream(self, **kwargs):
        raise AssertionError("No real model/provider is allowed in Today authority tests")
        yield  # pragma: no cover

    monkeypatch.setattr(ModelRouter, "call", forbidden_call)
    monkeypatch.setattr(ModelRouter, "stream", forbidden_stream)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text("Explicit local fixture", encoding="utf-8")
    for command in (
        ["init", "-q"],
        ["add", "fixture.txt"],
        [
            "-c",
            "user.name=Today Fixture",
            "-c",
            "user.email=today@example.invalid",
            "commit",
            "-qm",
            "Record isolated fixture",
        ],
    ):
        subprocess.run(["git", "-C", str(workspace), *command], check=True, capture_output=True)
    engine = create_agent_engine(
        AppConfig(
            workspace_root=str(workspace),
            personal={"data_dir": str(tmp_path / "personal")},
            memory={
                "session_db_path": str(tmp_path / "runtime/sessions.db"),
                "vector_db_path": str(tmp_path / "vectors"),
                "long_term_enabled": False,
            },
            safety={
                "permission_mode": "bypass",
                "max_turns": 3,
                "max_parallel_agents": 1,
                "max_queued_agents": 0,
            },
        )
    )
    clock = [datetime.now(UTC)]
    engine.personal_items.clock = lambda: clock[0]
    engine.personal_today = PersonalTodayService(engine, clock=lambda: clock[0])
    engine._session = await engine.create_session(
        title="Today authority fixture", model="no-provider"
    )
    env = SimpleNamespace(
        engine=engine,
        today=engine.personal_today,
        items=engine.personal_items,
        artifacts=engine.personal_artifacts,
        clock=clock,
        root=tmp_path,
    )
    try:
        yield env
    finally:
        await engine.shutdown()


async def make_item(env, key="source-item", *, actor="main", version=0, refs=(), origin=None):
    payload = {
        "request_id": key,
        "title": "真实来源 " + key,
        "instructions": "按已授权资料生成报告",
        "assignee_id": actor,
        "assignee_version": version,
        "input_refs": list(refs),
        "schedule": {"mode": "once", "local_time": "2035-01-01T09:00:00", "timezone": "UTC"},
        "policy": {
            "tool_names": [],
            "artifact_operations": [],
            "model_tier": "capable",
            "max_turns": 3,
            "max_cost_microusd": 1000000,
            "timeout_seconds": 30,
        },
    }
    if origin is not None:
        payload["origin_session_id"] = origin
    return await env.items.create(payload)


def placement(env, item, request="place-source", revision=0, metadata_revision=0, **fields):
    return {
        "request_id": request,
        "date": env.clock[0].date().isoformat(),
        "timezone": "UTC",
        "expected_day_revision": revision,
        "expected_metadata_revision": metadata_revision,
        "source_ref": item_ref(item),
        **fields,
    }


async def shared_item(env):
    owner = env.engine.personal_agents.create(
        name="山海", instructions="保管明确授权的材料", request_id="source-owner"
    )
    artifact = await env.artifacts.publish(
        actor_id=owner.id,
        request_id="private-source",
        title="共享来源",
        kind="material",
        filename="source.txt",
        content=PRIVATE.encode(),
        source_refs=[],
    )
    grant = await env.artifacts.share(
        actor_id=owner.id,
        artifact_id=artifact["artifact_id"],
        version=artifact["version"],
        recipient_id="main",
        request_id="share-source",
    )
    snapshot = await env.artifacts.get(
        actor_id="main", artifact_id=artifact["artifact_id"], version=1
    )
    item = await make_item(env, refs=[env.artifacts.reference(snapshot)])
    return owner, artifact, grant, item


async def revoke(env, owner, grant, key="revoke-source"):
    await env.artifacts.revoke(
        actor_id=owner.id, grant_id=grant["id"], expected_revision=grant["revision"], request_id=key
    )


async def pin_metadata(env, item, actor="main"):
    value = await env.today.metadata(item["item_id"], caller_actor=actor)
    return await env.today.model_result(
        "metadata", value, actor, {"operation": "metadata", "item_id": item["item_id"]}
    )


async def test_real_pin_binds_session_and_source_revocation_blocks_next_execution(authority_system):
    env = authority_system
    owner, _, grant, item = await shared_item(env)
    placed = await env.today.place(placement(env, item), trusted_channel="web")
    body = await pin_metadata(env, item)
    assert body["entry"]["title"] == item["definition"]["title"]
    refs = await env.artifacts.session_refs(env.engine._session.id, "main")
    assert len(refs) == 1
    pinned = await env.artifacts.get(
        actor_id="main", artifact_id=refs[0]["artifact_id"], version=refs[0]["version"]
    )
    assert pinned["evidence"]["today_read_refs"] and "item_refs" not in pinned["evidence"]
    await runtime.authorize_execution(env.engine)
    await revoke(env, owner, grant)
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.engine)
    with pytest.raises(PersonalAgentError):
        await env.artifacts.get(actor_id="main", artifact_id=pinned["artifact_id"], version=1)
    view = await env.today.get(placed["identity"]["day_id"])
    entry = next(row for row in view["entries"] if row["planned"])
    assert entry["availability"] == "source_unavailable"
    assert entry["title"] is entry["assignee"] is entry["report"] is None
    source = item["definition"]["inputs"]["artifact_refs"][0]
    await env.artifacts.share(
        actor_id=owner.id,
        artifact_id=source["artifact_id"],
        version=source["version"],
        recipient_id="main",
        request_id="new-independent-share",
    )
    with pytest.raises(PersonalAgentError):
        await env.today._authorize_item(item_ref(item), "main")


async def test_source_replay_history_and_receipts_reauthorize_without_erasing_acceptance(
    authority_system,
):
    env = authority_system
    owner, _, grant, item = await shared_item(env)
    payload = placement(env, item)
    receipt = await env.today.place(payload, trusted_channel="web")
    metadata_id = receipt["identity"]["metadata_ids"][0]
    await revoke(env, owner, grant)
    replay = await env.today.place(payload, trusted_channel="web")
    assert replay["accepted"] is True and replay["identity"] == receipt["identity"]
    assert replay["projection_available"] is False and "view" not in replay
    lookup = await env.today.request_status(payload["request_id"])
    assert lookup["status"] == "accepted" and lookup["projection_available"] is False
    history = await env.today.history("metadata", metadata_id)
    assert len(history["history"]) == 1
    entry = history["history"][0]["view"]["entry"]
    assert entry["title"] is entry["assignee"] is entry["report"] is None
    opaque = await env.today.model_result("history", history, "main", {"operation": "history"})
    assert "view" not in json.dumps(opaque) and PRIVATE not in json.dumps(opaque)


async def test_after_commit_source_revocation_returns_original_opaque_receipt(
    authority_system, monkeypatch
):
    env = authority_system
    owner, _, grant, item = await shared_item(env)
    original = env.today._finish
    count = 0

    async def revoke_before_projection(receipt, caller_actor):
        nonlocal count
        count += 1
        if count == 1:
            await revoke(env, owner, grant)
        return await original(receipt, caller_actor)

    monkeypatch.setattr(env.today, "_finish", revoke_before_projection)
    result = await env.today.place(placement(env, item), trusted_channel="web")
    assert result["accepted"] is True and result["projection_available"] is False
    assert "view" not in result and result["request_id"] == "place-source"
    assert len(env.today.store.read(lambda tx: tx.list("change"))) == 1
    assert (await env.today.request_status("place-source"))["identity"] == result["identity"]


async def test_pin_then_plan_reorder_does_not_revoke_historical_read(authority_system):
    env = authority_system
    first, second = await make_item(env), await make_item(env, "second-item")
    placed = await env.today.place(placement(env, first), trusted_channel="web")
    second_place = await env.today.place(
        placement(env, second, "place-second", 1), trusted_channel="web"
    )
    day_id = placed["identity"]["day_id"]
    view = await env.today.get(day_id, caller_actor="main")
    await env.today.model_result("get", view, "main", {"operation": "get", "day_id": day_id})
    refs = await env.artifacts.session_refs(env.engine._session.id, "main")
    ids = [row["id"] for row in second_place["view"]["day"]["placements"]]
    await env.today.reorder(
        {
            "request_id": "later-order",
            "day_id": day_id,
            "expected_revision": 2,
            "placement_ids": list(reversed(ids)),
        },
        trusted_channel="web",
    )
    await runtime.authorize_execution(env.engine)
    assert await env.artifacts.session_refs(env.engine._session.id, "main") == refs


async def test_today_missing_component_fails_closed_for_direct_and_derived_artifact(
    authority_system, monkeypatch
):
    env = authority_system
    item = await make_item(env)
    await pin_metadata(env, item)
    ref = (await env.artifacts.session_refs(env.engine._session.id, "main"))[0]
    derived = await env.artifacts.publish(
        actor_id="main",
        request_id="derive-today",
        title="衍生资料",
        kind="material",
        filename="derived.txt",
        content=b"Derived local note",
        source_refs=[ref],
    )
    monkeypatch.setattr(env.engine, "personal_today", None)
    for snapshot in (ref, derived):
        with pytest.raises(PersonalAgentError) as error:
            await env.artifacts.get(actor_id="main", artifact_id=snapshot["artifact_id"], version=1)
        assert error.value.code == "source_unavailable"
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.engine)


async def test_agent_scope_planner_scope_and_foreign_workspace_refs_are_distinct(authority_system):
    env = authority_system
    first = env.engine.personal_agents.create(
        name="Mira", instructions="处理自己的事项", request_id="mira"
    )
    second = env.engine.personal_agents.create(
        name="Echo", instructions="处理自己的事项", request_id="echo"
    )
    opened = await env.engine.personal_agents.open_session(first.id)
    private_item = await make_item(
        env, actor=first.id, version=first.version, origin=opened["session_id"]
    )
    assert (await env.today.metadata(private_item["item_id"], caller_actor=first.id))["entry"][
        "availability"
    ] == "available"
    for actor in (second.id, "main"):
        with pytest.raises(PersonalAgentError):
            await env.today.metadata(private_item["item_id"], caller_actor=actor)
    with pytest.raises(PersonalAgentError) as scope:
        await env.today.day(
            env.clock[0].date().isoformat(), "UTC", planner_id=first.id, caller_actor=second.id
        )
    assert scope.value.code == "scope_denied"
    foreign = {**item_ref(private_item), "workspace_key": "f" * 64}
    with pytest.raises(PersonalAgentError):
        await env.today.place(
            {**placement(env, private_item), "source_ref": foreign}, trusted_channel="web"
        )
    assert (await env.today.request_status("place-source"))["status"] == "unknown"


async def test_main_origin_does_not_bypass_other_agents_private_material(authority_system):
    env = authority_system
    owner = env.engine.personal_agents.create(
        name="山海", instructions="仅保存自己的资料", request_id="owner"
    )
    secret = await env.artifacts.publish(
        actor_id=owner.id,
        request_id="owned-material",
        title="私有来源",
        kind="material",
        filename="secret.txt",
        content=PRIVATE.encode(),
        source_refs=[],
    )
    origin = env.engine._session.id
    item = await make_item(
        env,
        actor=owner.id,
        version=owner.version,
        refs=[env.artifacts.reference(secret)],
        origin=origin,
    )
    view = await env.today.metadata(item["item_id"], caller_actor="main")
    assert view["entry"]["availability"] == "source_unavailable" and view["entry"]["title"] is None
    projected = await env.today.model_result(
        "metadata", view, "main", {"operation": "metadata", "item_id": item["item_id"]}
    )
    assert "entry" not in projected and PRIVATE not in json.dumps(projected)
    with pytest.raises(PersonalAgentError):
        await env.today.place(placement(env, item), caller_actor="main")


async def test_session_change_during_pin_never_returns_unbound_body(authority_system, monkeypatch):
    env = authority_system
    item = await make_item(env)
    old_session = env.engine._session
    other = await env.engine.create_session(title="Other actual session", model="no-provider")
    original_bind = env.artifacts.bind_refs

    async def switch_after_real_bind(*args, **kwargs):
        result = await original_bind(*args, **kwargs)
        env.engine._session = other
        return result

    monkeypatch.setattr(env.artifacts, "bind_refs", switch_after_real_bind)
    with pytest.raises(PersonalAgentError) as error:
        await pin_metadata(env, item)
    assert error.value.code == "binding_conflict"
    assert await env.artifacts.session_refs(other.id, "main") == []
    assert len(await env.artifacts.session_refs(old_session.id, "main")) == 1


async def test_revocation_after_artifact_publish_stops_tool_body_before_return(
    authority_system, monkeypatch
):
    env = authority_system
    owner, _, grant, item = await shared_item(env)
    publish = env.artifacts.publish
    published = []

    async def revoke_after_actual_publication(**kwargs):
        snapshot = await publish(**kwargs)
        published.append(snapshot)
        await revoke(env, owner, grant)
        return snapshot

    monkeypatch.setattr(env.artifacts, "publish", revoke_after_actual_publication)
    with pytest.raises(ToolExecutionError) as error:
        await PersonalTodayTool(env.today).execute(operation="metadata", item_id=item["item_id"])
    assert PRIVATE not in str(error.value) and len(published) == 1
    assert env.today.store.read(lambda tx: len(tx.list("read"))) == 1


async def test_model_metadata_retains_implicit_source_context_after_session_changes(
    authority_system,
):
    env = authority_system
    owner, _, grant, source_item = await shared_item(env)
    source = source_item["definition"]["inputs"]["artifact_refs"][0]
    await env.artifacts.bind_refs(env.engine._session.id, "main", [source])
    unrelated = await make_item(env, "unrelated-item")
    payload = {
        "request_id": "model-deadline",
        "expected_revision": 0,
        "source_ref": item_ref(unrelated),
        "deadline": {"kind": "date", "local_date": "2030-01-01", "timezone": "UTC"},
        "priority": "high",
        "status": "active",
    }
    accepted = await env.today.set_metadata(payload, caller_actor="main")
    metadata = (await env.today.metadata(unrelated["item_id"], caller_actor="main"))["metadata"]
    assert metadata["provenance"][0]["context"]["artifact_refs"] == [source]
    assert accepted["accepted"] is True
    env.engine._session = await env.engine.create_session(
        title="Later main session", model="no-provider"
    )
    env.engine._personal_artifact_refs = []
    await revoke(env, owner, grant)
    view = await env.today.metadata(unrelated["item_id"], caller_actor="main")
    assert view["entry"]["availability"] == "source_unavailable"
    assert view["entry"]["deadline"] is None and view["entry"]["priority"] == "normal"
    replay = await env.today.set_metadata(payload, caller_actor="main")
    assert replay["accepted"] is True and replay["projection_available"] is False
    assert replay["identity"] == accepted["identity"]


async def test_receipt_replay_reauthorizes_exact_old_metadata_not_a_clean_new_revision(
    authority_system,
):
    env = authority_system
    owner, _, grant, source_item = await shared_item(env)
    source = source_item["definition"]["inputs"]["artifact_refs"][0]
    await env.artifacts.bind_refs(env.engine._session.id, "main", [source])
    item = await make_item(env, "independent-receipt-item")
    payload = {
        "request_id": "private-revision-one",
        "expected_revision": 0,
        "source_ref": item_ref(item),
        "deadline": {"kind": "date", "local_date": "2030-01-01", "timezone": "UTC"},
        "priority": "high",
        "status": "active",
    }
    first = await env.today.set_metadata(payload, caller_actor="main")
    metadata_id = first["identity"]["metadata_ids"][0]
    exact = await env.today._record("metadata", metadata_id, revision=1)
    assert exact["provenance"][0]["context"]["artifact_refs"] == [source]
    reviewed = await env.today.set_metadata(
        {
            **payload,
            "request_id": "human-clean-revision-two",
            "expected_revision": 1,
            "deadline": None,
            "priority": "normal",
        },
        trusted_channel="web",
    )
    clean = await env.today._record("metadata", metadata_id, revision=2)
    assert clean["provenance"] == [] and clean["deadline"] is None
    assert reviewed["accepted"] is True
    env.engine._session = await env.engine.create_session(
        title="Fresh receipt lookup session", model="no-provider"
    )
    env.engine._personal_artifact_refs = []
    await revoke(env, owner, grant)
    current = await env.today.metadata(item["item_id"], caller_actor="main")
    assert current["entry"]["availability"] == "available"
    replay = await env.today.set_metadata(payload, caller_actor="main")
    lookup = await env.today.request_status(payload["request_id"], caller_actor="main")
    for response in (replay, lookup):
        assert response["accepted"] is True and response["projection_available"] is False
        assert response["identity"] == first["identity"]
        assert "metadata" not in response and "view" not in response
    assert (await env.today._record("metadata", metadata_id))["revision"] == 2
    assert await env.today._record("metadata", metadata_id, revision=1) == exact


async def test_finish_rechecks_redacted_entry_instead_of_returning_raw_metadata(
    authority_system, monkeypatch
):
    env = authority_system
    owner, _, grant, item = await shared_item(env)
    payload = {
        "request_id": "entry-boundary-receipt",
        "expected_revision": 0,
        "source_ref": item_ref(item),
        "deadline": {"kind": "date", "local_date": "2030-04-03", "timezone": "UTC"},
        "priority": "high",
        "status": "active",
    }
    accepted = await env.today.set_metadata(payload, trusted_channel="web")
    metadata_id = accepted["identity"]["metadata_ids"][0]
    exact = await env.today._record("metadata", metadata_id, revision=1)
    original_entry = env.today._entry
    original_exact = env.today._authorized_metadata_ref
    checkpoints = []

    async def observe_exact_authorization(*args, **kwargs):
        result = await original_exact(*args, **kwargs)
        checkpoints.append("exact-authorized")
        return result

    async def revoke_before_entry_source_check(*args, **kwargs):
        if "revoked-at-entry" not in checkpoints:
            assert "exact-authorized" in checkpoints
            await revoke(env, owner, grant)
            checkpoints.append("revoked-at-entry")
        entry = await original_entry(*args, **kwargs)
        assert entry["availability"] == "source_unavailable" and entry["title"] is None
        return entry

    monkeypatch.setattr(env.today, "_authorized_metadata_ref", observe_exact_authorization)
    monkeypatch.setattr(env.today, "_entry", revoke_before_entry_source_check)
    receipt = await env.today.request_status(payload["request_id"])
    assert checkpoints.index("exact-authorized") < checkpoints.index("revoked-at-entry")
    assert receipt["status"] == "accepted" and receipt["accepted"] is True
    assert receipt["identity"] == accepted["identity"]
    assert receipt["projection_available"] is False
    assert "metadata" not in receipt and "view" not in receipt
    assert "2030-04-03" not in canonical(receipt) and '"priority":"high"' not in canonical(receipt)
    assert await env.today._record("metadata", metadata_id, revision=1) == exact


async def test_history_clears_all_derived_fields_if_provenance_revoked_after_entry(
    authority_system, monkeypatch
):
    env = authority_system
    owner, _, grant, source_item = await shared_item(env)
    source = source_item["definition"]["inputs"]["artifact_refs"][0]
    await env.artifacts.bind_refs(env.engine._session.id, "main", [source])
    item = await make_item(env, "history-clean-item")
    accepted = await env.today.set_metadata(
        {
            "request_id": "history-tainted-metadata",
            "expected_revision": 0,
            "source_ref": item_ref(item),
            "deadline": {"kind": "date", "local_date": "2030-04-03", "timezone": "UTC"},
            "priority": "high",
            "status": "active",
        },
        caller_actor="main",
    )
    metadata_id = accepted["identity"]["metadata_ids"][0]
    await env.today.check(
        {
            "request_id": "history-human-check",
            "metadata_id": metadata_id,
            "expected_revision": 1,
            "source_ref": item_ref(item),
            "checked": True,
        },
        trusted_channel="web",
    )
    before = await env.today.history("metadata", metadata_id, limit=1)
    entry = before["history"][0]["view"]["entry"]
    assert entry["deadline"] and entry["priority"] == "high" and entry["human_check"]["checked"]
    original_projection = env.today._metadata_projection
    original_entry = env.today._entry
    checkpoints = []

    async def observe_entry(*args, **kwargs):
        result = await original_entry(*args, **kwargs)
        assert result["priority"] == "high" and result["human_check"]["checked"]
        checkpoints.append("entry-authorized")
        return result

    async def revoke_at_projection(value, caller_actor):
        assert checkpoints == ["entry-authorized"]
        await revoke(env, owner, grant)
        checkpoints.append("revoked-at-projection")
        return await original_projection(value, caller_actor)

    monkeypatch.setattr(env.today, "_entry", observe_entry)
    monkeypatch.setattr(env.today, "_metadata_projection", revoke_at_projection)
    history = await env.today.history("metadata", metadata_id, limit=1)
    view = history["history"][0]["view"]
    assert checkpoints == ["entry-authorized", "revoked-at-projection"]
    assert view["metadata"] is None
    assert view["entry"]["deadline"] is None and view["entry"]["priority"] == "normal"
    assert view["entry"]["human_check"] is None
    assert "2030-04-03" not in canonical(view)


def assert_opaque_day(view, day_id, revision, placement_ids, titles):
    assert view["day"]["id"] == day_id and view["day"]["revision"] == revision
    assert [row["id"] for row in view["day"]["placements"]] == sorted(placement_ids)
    rendered = canonical(view)
    assert PRIVATE not in rendered and all(title not in rendered for title in titles)
    for entry in view["entries"]:
        assert entry["title"] is entry["assignee"] is entry["report"] is None
        assert entry["deadline"] is None and entry["priority"] == "normal"
        assert entry["human_check"] is None


async def tainted_day(env):
    owner, _, grant, source_item = await shared_item(env)
    source = source_item["definition"]["inputs"]["artifact_refs"][0]
    await env.artifacts.bind_refs(env.engine._session.id, "main", [source])
    items = [await make_item(env, "tainted-order-" + str(index)) for index in range(3)]
    receipts = []
    for index, item in enumerate(items):
        receipts.append(
            await env.today.place(
                placement(env, item, "tainted-place-" + str(index), index), caller_actor="main"
            )
        )
    day_id = receipts[-1]["identity"]["day_id"]
    ids = [row["id"] for row in receipts[-1]["view"]["day"]["placements"]]
    await env.today.reorder(
        {
            "request_id": "model-tainted-order",
            "day_id": day_id,
            "expected_revision": 3,
            "placement_ids": list(reversed(sorted(ids))),
        },
        caller_actor="main",
    )
    return owner, grant, items, day_id, ids


async def test_human_cold_http_can_clean_tainted_day_by_opaque_ids_and_explicit_reconfirmation(
    authority_system,
):
    env = authority_system
    owner, grant, items, day_id, ids = await tainted_day(env)
    original = await env.today._record("day", day_id, revision=4)
    assert original["provenance"] and [row["id"] for row in original["placements"]] == list(
        reversed(sorted(ids))
    )
    await revoke(env, owner, grant)
    env.engine._session = await env.engine.create_session(
        title="Cold human recovery", model="no-provider"
    )
    env.engine._personal_artifact_refs = []
    cold = PersonalTodayService(env.engine, clock=lambda: env.clock[0])
    env.engine.personal_today = cold
    app = FastAPI()
    app.include_router(today_router)
    app.state.engine = env.engine
    app.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=["cold-today-fixture-key"]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://today-local",
        headers={"X-API-Key": "cold-today-fixture-key"},
    ) as client:
        response = await client.get("/personal/today/days/" + day_id)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert_opaque_day(
            response.json(), day_id, 4, ids, [item["definition"]["title"] for item in items]
        )
    with pytest.raises(PersonalAgentError):
        await cold.get(day_id, caller_actor="main")
    removed = await cold.remove(
        {
            "request_id": "opaque-human-remove",
            "day_id": day_id,
            "expected_revision": 4,
            "placement_id": ids[0],
        },
        trusted_channel="web",
    )
    assert removed["accepted"] is True and removed["projection_available"] is False
    after_remove = await cold._record("day", day_id, revision=5)
    assert after_remove["provenance"] == original["provenance"]
    assert (
        next(row for row in after_remove["placements"] if row["id"] == ids[0])["disposition"]
        == "exclude"
    )
    assert_opaque_day(
        await cold.get(day_id), day_id, 5, ids, [item["definition"]["title"] for item in items]
    )
    remaining = [row["id"] for row in after_remove["placements"] if row["disposition"] == "include"]
    confirmed = await cold.reorder(
        {
            "request_id": "explicit-human-clean-order",
            "day_id": day_id,
            "expected_revision": 5,
            "placement_ids": list(reversed(sorted(remaining))),
        },
        trusted_channel="web",
    )
    assert confirmed["accepted"] is True and confirmed["projection_available"] is True
    final = await cold._record("day", day_id, revision=6)
    assert final["provenance"] == []
    body = await cold.model_result("get", {}, "main", {"operation": "get", "day_id": day_id})
    assert {entry["placement_id"] for entry in body["entries"]} == set(remaining)
    await runtime.authorize_execution(env.engine)


async def test_human_rebase_can_discard_all_revoked_derivatives_but_not_keep_any(
    authority_system,
):
    env = authority_system
    owner, _, grant, source_item = await shared_item(env)
    source = source_item["definition"]["inputs"]["artifact_refs"][0]
    await env.artifacts.bind_refs(env.engine._session.id, "main", [source])
    old = await make_item(env, "rebase-clean-source")
    metadata = await env.today.set_metadata(
        {
            "request_id": "revoked-derived-deadline",
            "expected_revision": 0,
            "source_ref": item_ref(old),
            "deadline": {"kind": "date", "local_date": "2030-04-03", "timezone": "UTC"},
            "priority": "high",
            "status": "active",
        },
        caller_actor="main",
    )
    placed = await env.today.place(placement(env, old, metadata_revision=1), trusted_channel="web")
    metadata_id = metadata["identity"]["metadata_ids"][0]
    await env.today.check(
        {
            "request_id": "rebase-old-check",
            "metadata_id": metadata_id,
            "expected_revision": 1,
            "source_ref": item_ref(old),
            "checked": True,
        },
        trusted_channel="web",
    )
    closed = await env.items.control(
        old["item_id"],
        "close",
        {"request_id": "close-tainted-once", "expected_revision": old["revision"]},
    )
    new = await env.items.control(
        old["item_id"],
        "restart",
        {
            "request_id": "restart-tainted-once",
            "expected_revision": closed["revision"],
            "schedule": {"mode": "once", "local_time": "2035-01-02T09:00:00", "timezone": "UTC"},
        },
    )
    await revoke(env, owner, grant)
    payload = {
        "request_id": "discard-all-old-derived",
        "day_id": placed["identity"]["day_id"],
        "expected_day_revision": 1,
        "metadata_id": metadata_id,
        "expected_metadata_revision": 2,
        "old_source_ref": item_ref(old),
        "new_source_ref": item_ref(new),
        "preserve_deadline": False,
        "preserve_priority": False,
    }
    for index, (deadline, priority) in enumerate(((True, False), (False, True), (True, True))):
        with pytest.raises(PersonalAgentError):
            await env.today.rebase(
                {
                    **payload,
                    "request_id": "keep-revoked-" + str(index),
                    "preserve_deadline": deadline,
                    "preserve_priority": priority,
                },
                trusted_channel="web",
            )
    with pytest.raises(PersonalAgentError):
        await env.today.rebase(
            {**payload, "request_id": "discard-wrong-cas", "expected_metadata_revision": 99},
            trusted_channel="web",
        )
    with pytest.raises(PersonalAgentError):
        await env.today.rebase(
            {
                **payload,
                "request_id": "discard-unauthorized-new-ref",
                "new_source_ref": {**item_ref(new), "definition_sha256": "f" * 64},
            },
            trusted_channel="web",
        )
    assert (await env.today._record("metadata", metadata_id))["revision"] == 2
    result = await env.today.rebase(payload, trusted_channel="web")
    assert result["accepted"] is True and result["projection_available"] is True
    clean = await env.today._record("metadata", metadata_id, revision=3)
    assert clean["item_ref"] == item_ref(new) and clean["provenance"] == []
    assert (
        clean["deadline"] is None and clean["priority"] == "normal" and clean["human_check"] is None
    )


@pytest.mark.parametrize("reader", [None, "main"])
async def test_day_rechecks_provenance_after_real_source_paging_await(
    authority_system, monkeypatch, reader
):
    env = authority_system
    owner, grant, items, day_id, ids = await tainted_day(env)
    original_sources = env.today.sources
    checkpoints = []

    async def revoke_after_real_source_page(*args, **kwargs):
        page = await original_sources(*args, **kwargs)
        assert page["entries"]
        checkpoints.append("page-authorized")
        await revoke(env, owner, grant)
        checkpoints.append("revoked-after-page")
        return page

    monkeypatch.setattr(env.today, "sources", revoke_after_real_source_page)
    if reader is None:
        view = await env.today.get(day_id)
        assert_opaque_day(view, day_id, 4, ids, [item["definition"]["title"] for item in items])
    else:
        with pytest.raises(PersonalAgentError):
            await env.today.get(day_id, caller_actor=reader)
    assert checkpoints == ["page-authorized", "revoked-after-page"]


async def test_revoked_excluded_tombstone_does_not_block_or_leak_into_clean_today_pin(
    authority_system,
):
    env = authority_system
    owner, _, grant, excluded_item = await shared_item(env)
    clean_item = await make_item(env, "clean-included-item")
    first = await env.today.place(placement(env, excluded_item), trusted_channel="web")
    second = await env.today.place(
        placement(env, clean_item, "place-clean", 1), trusted_channel="web"
    )
    day_id = first["identity"]["day_id"]
    excluded_id = first["view"]["day"]["placements"][0]["id"]
    await env.today.remove(
        {
            "request_id": "exclude-private-item",
            "day_id": day_id,
            "expected_revision": second["identity"]["day_revision"],
            "placement_id": excluded_id,
        },
        trusted_channel="web",
    )
    await revoke(env, owner, grant)
    raw = await env.today.get(day_id, caller_actor="main")
    tombstone = next(row for row in raw["entries"] if row["placement_id"] == excluded_id)
    assert tombstone["availability"] == "source_unavailable"
    assert tombstone["title"] is None and tombstone["disposition"] == "exclude"
    arguments = {"operation": "get", "day_id": day_id}
    body = await env.today.model_result("get", raw, "main", arguments)
    assert {row["item_ref"]["item_id"] for row in body["entries"]} == {clean_item["item_id"]}
    serialized = canonical(body)
    assert excluded_item["definition"]["title"] not in serialized and PRIVATE not in serialized
    assert clean_item["definition"]["title"] in serialized
    refs = await env.artifacts.session_refs(env.engine._session.id, "main")
    assert len(refs) == 1
    snapshot, content = await env.artifacts.read(
        actor_id="main", artifact_id=refs[0]["artifact_id"], version=refs[0]["version"]
    )
    assert content == serialized.encode()
    pointer = snapshot["evidence"]["today_read_refs"][0]
    record = await env.today._record("read", pointer["read_id"], revision=1)
    assert {ref["item_id"] for ref in record["item_refs"]} == {clean_item["item_id"]}
    assert all(
        ref["item_ref"]["item_id"] != excluded_item["item_id"] for ref in record["metadata_refs"]
    )
    await runtime.authorize_execution(env.engine)


@pytest.mark.parametrize("operation", ["get", "metadata"])
async def test_public_projection_cannot_forge_title_source_or_availability_before_pinning(
    authority_system, operation
):
    env = authority_system
    item = await make_item(env)
    if operation == "metadata":
        arguments = {"operation": operation, "item_id": item["item_id"]}
        raw = await env.today.metadata(item["item_id"], caller_actor="main")
        forged = copy.deepcopy(raw)
        forged["entry"].update(title="FORGED_TODAY_TITLE", priority="high")
        forged["entry"]["item_ref"]["definition_sha256"] = "f" * 64
    else:
        placed = await env.today.place(placement(env, item), trusted_channel="web")
        arguments = {"operation": operation, "day_id": placed["identity"]["day_id"]}
        raw = await env.today.get(arguments["day_id"], caller_actor="main")
        forged = copy.deepcopy(raw)
        forged["entries"][0].update(title="FORGED_TODAY_TITLE", availability="source_unavailable")
        forged["deliveries"] = [{"title": "FORGED_REPORT_TITLE", "answer": PRIVATE}]
    body = await env.today.model_result(operation, forged, "main", arguments)
    assert "FORGED_TODAY_TITLE" not in canonical(body)
    assert "FORGED_REPORT_TITLE" not in canonical(body) and PRIVATE not in canonical(body)
    assert item["definition"]["title"] in canonical(body)
    assert "observed_at" not in body
    refs = await env.artifacts.session_refs(env.engine._session.id, "main")
    assert len(refs) == 1
    _, content = await env.artifacts.read(
        actor_id="main", artifact_id=refs[0]["artifact_id"], version=refs[0]["version"]
    )
    assert content == canonical(body).encode()


async def test_pin_detaches_mutable_body_and_read_record_remains_immutable(
    authority_system, monkeypatch
):
    env = authority_system
    item = await make_item(env)
    metadata = await env.today.metadata(item["item_id"], caller_actor="main")
    body = {"source": metadata, "controlled_notes": ["original"]}
    frozen = copy.deepcopy(body)
    original_authorize = env.today._read_authority
    changed = False

    async def mutate_caller_object_after_real_authority(record, reader):
        nonlocal changed
        await original_authorize(record, reader)
        if not changed:
            changed = True
            body["controlled_notes"].append("MUTATED_AFTER_BOUNDARY")

    monkeypatch.setattr(env.today, "_read_authority", mutate_caller_object_after_real_authority)
    returned = await env.today._pin_body(
        body,
        "main",
        metadata_refs=[env.today._metadata_reference(metadata["metadata"])],
        item_refs=[item_ref(item)],
    )
    assert changed and body != frozen
    assert returned == frozen and returned is not body
    refs = await env.artifacts.session_refs(env.engine._session.id, "main")
    snapshot, content = await env.artifacts.read(
        actor_id="main", artifact_id=refs[0]["artifact_id"], version=refs[0]["version"]
    )
    assert content == canonical(frozen).encode() and b"MUTATED_AFTER_BOUNDARY" not in content
    pointer = snapshot["evidence"]["today_read_refs"][0]
    record = await env.today._record("read", pointer["read_id"], revision=1)
    with pytest.raises(PersonalAgentError) as immutable:
        env.today.store.write(
            lambda tx: tx.put("read", {**record, "body_sha256": "f" * 64}, expected_revision=1)
        )
    assert immutable.value.code == "immutable_read"
    assert await env.today._record("read", record["id"], revision=1) == record


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", True),
        ("revision", 1.0),
        ("revision", "1"),
        ("revision", 0),
        ("read_id", "tr_bad"),
        ("read_id", "td_" + "1" * 32),
        ("read_id", None),
        ("digest", True),
        ("digest", "not-a-digest"),
        ("digest", "f" * 64),
        ("workspace_key", "f" * 64),
    ],
)
async def test_today_read_pointer_rejects_boolean_revision_and_malformed_identity(
    authority_system, field, value
):
    env = authority_system
    item = await make_item(env)
    await pin_metadata(env, item)
    ref = (await env.artifacts.session_refs(env.engine._session.id, "main"))[0]
    snapshot = await env.artifacts.get(
        actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
    )
    forged = copy.deepcopy(snapshot)
    forged["evidence"]["today_read_refs"][0][field] = value
    with pytest.raises(PersonalAgentError):
        await env.today.authorize_artifact(forged, "main")
    await env.today.authorize_artifact(snapshot, "main")


async def test_trusted_delegated_session_pin_uses_actual_actor_and_live_runtime_fence(
    authority_system, monkeypatch
):
    env = authority_system
    profile = env.engine.personal_agents.create(
        name="Runtime Agent",
        instructions="读取本次授权的今日安排",
        request_id="runtime-agent",
        tool_names=["personal_today"],
    )
    first = await make_item(env, "execution-owned-one", actor=profile.id, version=profile.version)
    second = await make_item(env, "execution-owned-two", actor=profile.id, version=profile.version)
    task_id = "pio_" + uuid.uuid4().hex
    execution = Session(
        id="pie_" + uuid.uuid4().hex,
        personal_agent_id=profile.id,
        personal_execution_id=task_id,
        workspace_root=str(env.engine.workspace_root),
        title="Actual delegated execution session",
        model="no-provider",
    )
    await env.engine.session_store.save(execution)
    env.engine.personal_agents.store.register_execution_session(
        profile.id,
        env.today.workspace_key,
        execution.id,
        session_db_key=env.engine.personal_agents.session_db_key,
    )
    env.engine._session = execution
    env.engine._personal_collaboration_task_id = task_id
    env.engine._personal_active_turn = (execution.id, profile.id, profile.version)
    live, checks = [True], []

    async def actual_runtime_fence():
        checks.append((env.engine._session.id, env.engine._personal_collaboration_task_id))
        if not live[0] or env.engine._session.id != execution.id:
            raise PersonalAgentError("lease_lost", "本次受控运行租约已结束")

    monkeypatch.setattr(
        env.engine, "_personal_execution_authorizer", actual_runtime_fence, raising=False
    )
    body = await pin_metadata(env, first, profile.id)
    assert body["entry"]["title"] == first["definition"]["title"]
    assert checks and all(value == (execution.id, task_id) for value in checks)
    assert len(await env.artifacts.session_refs(execution.id, profile.id)) == 1
    live[0] = False
    with pytest.raises(PersonalAgentError):
        await pin_metadata(env, second, profile.id)
    assert len(env.today.store.read(lambda tx: tx.list("read"))) == 1
    live[0] = True
    publish = env.artifacts.publish

    async def end_fence_after_real_publish(**kwargs):
        snapshot = await publish(**kwargs)
        live[0] = False
        return snapshot

    monkeypatch.setattr(env.artifacts, "publish", end_fence_after_real_publish)
    with pytest.raises(PersonalAgentError):
        await pin_metadata(env, second, profile.id)
    assert (await env.engine.session_store.load(execution.id)).personal_execution_id == task_id
    assert env.engine.personal_agents.current(execution.id).id == profile.id
    env.engine._personal_active_turn = None


async def test_large_projection_pins_one_opaque_journal_read_not_over_64k_evidence(
    authority_system,
):
    env = authority_system
    item = await make_item(env)
    metadata = await env.today.metadata(item["item_id"], caller_actor="main")
    body = {"entry": metadata["entry"], "controlled_large_projection": "今日" * 40000}
    result = await env.today._pin_body(
        body,
        "main",
        metadata_refs=[env.today._metadata_reference(metadata["metadata"])],
        item_refs=[item_ref(item)],
    )
    assert result == body and len(canonical(body).encode()) > 65536
    refs = await env.artifacts.session_refs(env.engine._session.id, "main")
    snapshot, content = await env.artifacts.read(
        actor_id="main", artifact_id=refs[0]["artifact_id"], version=1
    )
    assert (
        content == canonical(body).encode()
        and snapshot["sha256"] == hashlib.sha256(content).hexdigest()
    )
    assert len(canonical(snapshot["evidence"]).encode()) < 4096
    read_ref = snapshot["evidence"]["today_read_refs"][0]
    stored = await env.today._record("read", read_ref["read_id"], revision=1)
    assert (
        stored["body_sha256"] == snapshot["sha256"] and "controlled_large_projection" not in stored
    )
    await runtime.authorize_execution(env.engine)


async def prepare_intent(env, item, occurrence):
    executor = env.items.executor
    raw = executor.prepare(
        item,
        occurrence_id=occurrence,
        execution_session_id="pie_" + uuid.uuid4().hex,
        task=item["definition"]["instructions"],
        context="Explicit local delivery fixture",
        input_refs=item["definition"]["inputs"]["artifact_refs"],
        input_manifest=[],
        schedule_id="fixture-" + item["once_key"],
        event_id="fixture-" + occurrence,
    )
    return await env.items._call(
        "prepare",
        item_id=item["item_id"],
        expected_definition_sha256=item["definition_sha256"],
        intent=raw,
    )


async def settle(executor, intent):
    await executor.submit(intent)
    if executor._active:
        results = await asyncio.wait_for(
            asyncio.gather(*list(executor._active.values()), return_exceptions=True), 45
        )
        assert not [value for value in results if isinstance(value, BaseException)], results
    result = await executor.reconcile(intent)
    assert result["status"] == "completed", result
    return result


async def test_actual_job_run_delivery_time_old_once_paging_and_check_separation(
    authority_system, monkeypatch
):
    env = authority_system
    calls = []

    async def controlled_stream(self, **kwargs):
        calls.append(kwargs)
        yield StreamChunk(
            token="本地受控模型报告。",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=9, output_tokens=6, cost_usd=0.001),
        )

    monkeypatch.setattr(ModelRouter, "stream", controlled_stream)
    # Only the lexical fixture time differs; Job/Run/delivery stores and execution are real.
    env.items.store._clock = lambda: env.clock[0] - timedelta(days=3)
    item = await make_item(env)
    original_session = env.engine._session
    old = await prepare_intent(env, item, "pio_" + "1" * 32)
    result = await settle(env.items.executor, old)
    assert len(calls) == 1 and result["delivery_id"] and result["read_receipt"] is None
    assert result["delivered_at"] != old["issued_at"]
    day = datetime.fromisoformat(result["delivered_at"]).astimezone(UTC).date().isoformat()
    rows, _ = await env.today._deliveries("", 1, None, day_window(day, "UTC"))
    assert [row["occurrence_id"] for row in rows] == [old["occurrence_id"]]
    assert rows[0]["delivered_at"] == result["delivered_at"]
    assert not ({"answer", "steps", "receipt", "directories"} & set(rows[0]))
    job_before = await env.items.executor.jobs.get(result["job_id"])
    run_before = await env.engine.chat_run_store.get_run(
        old["execution_session_id"], result["run_id"]
    )
    placed = await env.today.place(placement(env, item), trusted_channel="web")
    mid = metadata_identity(env.today.workspace_key, item["item_id"])
    await env.today.check(
        {
            "request_id": "human-plan-check",
            "metadata_id": mid,
            "expected_revision": 1,
            "source_ref": item_ref(item),
            "checked": True,
        },
        trusted_channel="web",
    )
    assert await env.items.executor.jobs.get(result["job_id"]) == job_before
    assert (
        await env.engine.chat_run_store.get_run(old["execution_session_id"], result["run_id"])
        == run_before
    )
    unchanged = await env.items.executor.view(old)
    assert unchanged["read_receipt"] is None and unchanged["status"] == "completed"
    assert (await env.items._item(item["item_id"]))["state"] == "active"
    closed = await env.items.control(
        item["item_id"],
        "close",
        {"request_id": "close-before-restart", "expected_revision": item["revision"]},
    )
    restarted = await env.items.control(
        item["item_id"],
        "restart",
        {
            "request_id": "next-once",
            "expected_revision": closed["revision"],
            "schedule": {"mode": "once", "local_time": "2035-01-02T09:00:00", "timezone": "UTC"},
        },
    )
    new = await prepare_intent(env, restarted, "pio_" + "2" * 32)
    second_result = await settle(env.items.executor, new)
    assert len(calls) == 2 and second_result["delivery_id"] != result["delivery_id"]
    env.engine._session = original_session
    first_page, cursor = await env.today._deliveries("", 1, None, day_window(day, "UTC"))
    second_page, tail = await env.today._deliveries(cursor, 1, None, day_window(day, "UTC"))
    assert cursor == old["occurrence_id"] and tail is None
    assert [row["occurrence_id"] for row in first_page + second_page] == [
        old["occurrence_id"],
        new["occurrence_id"],
    ]
    assert first_page[0]["item_ref"]["once_key"] == item["once_key"]
    assert second_page[0]["item_ref"]["once_key"] == restarted["once_key"]
    assert placed["identity"]["day_id"]
