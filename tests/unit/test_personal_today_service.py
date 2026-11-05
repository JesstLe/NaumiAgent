"""Today plans over actual Engine/Item/Journal/Session stores, with zero provider I/O."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.router import ModelRouter
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today import PersonalTodayService
from naumi_agent.personal.today_models import item_ref
from naumi_agent.runtime.composition import create_agent_engine

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
DATE, ZONE = "2030-01-01", "UTC"


@pytest.fixture
async def today_system(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("NAUMI_PERSONAL__DATA_DIR", str(tmp_path / "personal"))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    calls = []

    async def forbidden_call(self, **kwargs):
        calls.append("call")
        raise AssertionError("Today planning has no provider transport")

    async def forbidden_stream(self, **kwargs):
        calls.append("stream")
        raise AssertionError("Today planning has no provider transport")
        yield  # pragma: no cover

    monkeypatch.setattr(ModelRouter, "call", forbidden_call)
    monkeypatch.setattr(ModelRouter, "stream", forbidden_stream)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = create_agent_engine(
        AppConfig(
            workspace_root=str(workspace),
            personal={"data_dir": str(tmp_path / "personal")},
            memory={
                "session_db_path": str(tmp_path / "runtime/sessions.db"),
                "vector_db_path": str(tmp_path / "vectors"),
                "long_term_enabled": False,
            },
            safety={"permission_mode": "bypass", "max_turns": 3, "max_parallel_agents": 1},
        )
    )
    clock = [datetime(2030, 1, 1, 12, tzinfo=UTC)]
    engine.personal_items.clock = lambda: clock[0]
    service = PersonalTodayService(engine, clock=lambda: clock[0])
    engine.personal_today = service
    engine._session = await engine.create_session(title="Today local fixture", model="no-provider")
    env = SimpleNamespace(
        engine=engine,
        today=service,
        items=engine.personal_items,
        clock=clock,
        calls=calls,
        root=tmp_path,
    )
    try:
        yield env
        assert not calls, "Planning unexpectedly attempted model transport"
    finally:
        await engine.shutdown()


async def create_item(env, key="item-1", **extra):
    return await env.items.create(
        {
            "request_id": key,
            "title": "真实事项 " + key,
            "instructions": "只整理明确指定的资料，不执行外部行动。",
            "schedule": {"mode": "once", "local_time": "2035-01-01T09:00:00", "timezone": ZONE},
            **extra,
        }
    )


def meta_payload(item, key="metadata-1", revision=0, **extra):
    return {
        "request_id": key,
        "expected_revision": revision,
        "source_ref": item_ref(item),
        "deadline": {"kind": "date", "local_date": DATE, "timezone": ZONE},
        "priority": "high",
        "status": "active",
        **extra,
    }


def place_payload(item, key="place-1", day_revision=0, metadata_revision=0, **extra):
    return {
        "request_id": key,
        "date": DATE,
        "timezone": ZONE,
        "expected_day_revision": day_revision,
        "expected_metadata_revision": metadata_revision,
        "source_ref": item_ref(item),
        **extra,
    }


def included(view):
    return [row for row in view["entries"] if row["planned"] and row["disposition"] == "include"]


async def test_real_item_metadata_place_reorder_remove_check_move_and_history(today_system):
    env = today_system
    first, second = await create_item(env), await create_item(env, "item-2")
    originals = [await env.items._item(row["item_id"]) for row in (first, second)]
    schedules = copy.deepcopy(env.engine.scheduler_runner.store.list_jobs())
    assert (await env.today.metadata(first["item_id"]))["revision"] == 0
    metadata = await env.today.set_metadata(meta_payload(first), trusted_channel="web")
    assert metadata["accepted"] and metadata["projection_available"]
    first_place = await env.today.place(
        place_payload(first, metadata_revision=1), trusted_channel="web"
    )
    second_place = await env.today.place(place_payload(second, "place-2", 1), trusted_channel="web")
    day_id = first_place["identity"]["day_id"]
    view = second_place["view"]
    placements = [row["placement_id"] for row in included(view)]
    assert len(placements) == 2 and view["day"]["revision"] == 2
    with pytest.raises(PersonalAgentError) as incomplete:
        await env.today.reorder(
            {
                "request_id": "incomplete-order",
                "day_id": day_id,
                "expected_revision": 2,
                "placement_ids": placements[:1],
            },
            trusted_channel="web",
        )
    assert incomplete.value.code == "version_conflict"
    assert (await env.today.get(day_id))["day"]["revision"] == 2
    reordered = await env.today.reorder(
        {
            "request_id": "reorder-1",
            "day_id": day_id,
            "expected_revision": 2,
            "placement_ids": list(reversed(placements)),
        },
        trusted_channel="cli",
    )
    assert [row["placement_id"] for row in included(reordered["view"])] == list(
        reversed(placements)
    )
    removed = await env.today.remove(
        {
            "request_id": "remove-1",
            "day_id": day_id,
            "expected_revision": 3,
            "placement_id": placements[1],
        },
        trusted_channel="tui",
    )
    assert [row["placement_id"] for row in included(removed["view"])] == placements[:1]
    excluded = next(
        row for row in removed["view"]["entries"] if row["placement_id"] == placements[1]
    )
    assert excluded["disposition"] == "exclude"
    checked = await env.today.check(
        {
            "request_id": "check-1",
            "metadata_id": metadata["identity"]["metadata_ids"][0],
            "expected_revision": 1,
            "source_ref": item_ref(first),
            "checked": True,
        },
        trusted_channel="web",
    )
    saved_meta = checked["metadata"]["metadata"]
    assert saved_meta["human_check"]["checked"] is True
    assert saved_meta["human_check"]["channel"] == "web"
    moved = await env.today.move(
        {
            "request_id": "move-1",
            "day_id": day_id,
            "expected_revision": 4,
            "placement_id": placements[0],
            "target_date": "2030-01-02",
            "target_timezone": ZONE,
            "expected_target_revision": 0,
        },
        trusted_channel="web",
    )
    assert moved["view"]["day"]["date"] == "2030-01-02"
    assert len(included(moved["view"])) == 1
    assert included(await env.today.get(day_id)) == []
    after_meta = (await env.today.metadata(first["item_id"]))["metadata"]
    assert after_meta == saved_meta, "Moving a day must not duplicate/rewrite deadline metadata"
    assert [await env.items._item(row["item_id"]) for row in (first, second)] == originals
    assert env.engine.scheduler_runner.store.list_jobs() == schedules
    assert await env.items._call("list_intents", workspace_key=env.today.workspace_key) == []
    assert await env.engine.chat_run_store.list_runs(env.engine._session.id) == []
    assert await env.items.executor.jobs.list_result_inbox_records(env.engine._session.id) == ()
    history = await env.today.history("day", day_id, limit=2)
    assert [row["revision"] for row in history["history"]] == [5, 4]
    older = await env.today.history(
        "day", day_id, limit=2, after_revision=history["next_after_revision"]
    )
    assert [row["revision"] for row in older["history"]] == [3, 2]
    receipt = await env.today.request_status("move-1")
    assert receipt["status"] == "accepted" and receipt["identity"] == moved["identity"]
    assert "_scope" not in receipt and "instructions" not in json.dumps(receipt)


async def test_idempotency_parallel_admission_and_stale_metadata_cas(today_system):
    env = today_system
    item = await create_item(env)
    payload = place_payload(item)
    results = await asyncio.gather(
        *(env.today.place(payload, trusted_channel="web") for _ in range(8))
    )
    assert len({value["identity"]["change_id"] for value in results}) == 1
    day_id = results[0]["identity"]["day_id"]
    assert (await env.today.get(day_id))["day"]["revision"] == 1
    changes = env.today.store.read(lambda tx: tx.list("change"))
    assert len(changes) == 1
    with pytest.raises(PersonalAgentError) as conflict:
        await env.today.place({**payload, "date": "2030-01-02"}, trusted_channel="web")
    assert conflict.value.code == "idempotency_conflict"
    edits = [
        meta_payload(item, "edit-a", 1, priority="high"),
        meta_payload(item, "edit-b", 1, priority="low"),
    ]
    outcomes = await asyncio.gather(
        *(env.today.set_metadata(p, trusted_channel="web") for p in edits), return_exceptions=True
    )
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert [x.code for x in outcomes if isinstance(x, PersonalAgentError)] == ["version_conflict"]
    assert (await env.today.metadata(item["item_id"]))["revision"] == 2


async def test_place_never_silently_rebases_existing_metadata_and_move_is_atomic(today_system):
    env = today_system
    item = await create_item(env)
    await env.today.set_metadata(meta_payload(item), trusted_channel="web")
    with pytest.raises(PersonalAgentError) as stale:
        await env.today.place(place_payload(item), trusted_channel="web")
    assert stale.value.code == "version_conflict"
    placed = await env.today.place(
        place_payload(item, "place-current", metadata_revision=1), trusted_channel="web"
    )
    before = placed["view"]["day"]
    placement = included(placed["view"])[0]["placement_id"]
    other = await env.today.place(
        place_payload(item, "target-place", metadata_revision=1, date="2030-01-02"),
        trusted_channel="web",
    )
    with pytest.raises(PersonalAgentError) as target_conflict:
        await env.today.move(
            {
                "request_id": "move-stale",
                "day_id": before["id"],
                "expected_revision": before["revision"],
                "placement_id": placement,
                "target_date": "2030-01-02",
                "target_timezone": ZONE,
                "expected_target_revision": 0,
            },
            trusted_channel="web",
        )
    assert target_conflict.value.code == "version_conflict"
    assert (await env.today.get(before["id"]))["day"] == before
    assert (await env.today.get(other["identity"]["day_id"]))["day"]["revision"] == 1
    assert (await env.today.request_status("move-stale"))["status"] == "unknown"


async def test_lifecycle_close_is_not_semantic_stale_restart_requires_explicit_rebase(today_system):
    env = today_system
    original = await create_item(env)
    metadata = await env.today.set_metadata(meta_payload(original), trusted_channel="web")
    placed = await env.today.place(
        place_payload(original, metadata_revision=1), trusted_channel="web"
    )
    day_id = placed["identity"]["day_id"]
    mid = metadata["identity"]["metadata_ids"][0]
    await env.today.check(
        {
            "request_id": "check-old",
            "metadata_id": mid,
            "expected_revision": 1,
            "source_ref": item_ref(original),
            "checked": True,
        },
        trusted_channel="web",
    )
    closed = await env.items.control(
        original["item_id"],
        "close",
        {"request_id": "close-original", "expected_revision": original["revision"]},
    )
    closed_entry = included(await env.today.get(day_id))[0]
    assert closed_entry["availability"] == "available" and closed_entry["item_status"] == "closed"
    assert closed_entry["item_ref"] == item_ref(original)
    restarted = await env.items.control(
        original["item_id"],
        "restart",
        {
            "request_id": "restart-original",
            "expected_revision": closed["revision"],
            "schedule": {"mode": "once", "local_time": "2035-01-02T09:00:00", "timezone": ZONE},
        },
    )
    assert restarted["once_key"] != original["once_key"]
    stale = included(await env.today.get(day_id))[0]
    assert stale["availability"] == "stale"
    assert all(
        stale[key] is None
        for key in ("title", "assignee", "report", "due_at", "execution_status", "item_status")
    )
    assert stale["item_ref"] == item_ref(original)
    rebase = {
        "request_id": "rebase-current",
        "day_id": day_id,
        "expected_day_revision": 1,
        "metadata_id": mid,
        "expected_metadata_revision": 2,
        "old_source_ref": item_ref(original),
        "new_source_ref": item_ref(restarted),
        "preserve_deadline": True,
        "preserve_priority": False,
    }
    changed = await env.today.rebase(rebase, trusted_channel="web")
    current = included(changed["view"])[0]
    assert current["availability"] == "available" and current["item_ref"] == item_ref(restarted)
    assert current["human_check"] is None and current["priority"] == "normal"
    assert current["deadline"]["local_date"] == DATE
    assert (await env.today.rebase(rebase, trusted_channel="web"))["identity"] == changed[
        "identity"
    ]


@pytest.mark.parametrize("channel", [None, "agent", "manual_api"])
async def test_human_checks_and_moves_do_not_accept_forged_channels(today_system, channel):
    env = today_system
    item = await create_item(env)
    placed = await env.today.place(place_payload(item), trusted_channel="web")
    mid = placed["identity"]["metadata_ids"][0]
    with pytest.raises(PersonalAgentError) as error:
        await env.today.check(
            {
                "request_id": "bad-check",
                "metadata_id": mid,
                "expected_revision": 1,
                "source_ref": item_ref(item),
                "checked": True,
            },
            trusted_channel=channel,
        )
    assert error.value.code == "scope_denied"
    with pytest.raises(PersonalAgentError):
        await env.today.check(
            {
                "request_id": "model-check",
                "metadata_id": mid,
                "expected_revision": 1,
                "source_ref": item_ref(item),
                "checked": True,
            },
            trusted_channel="web",
            caller_actor="main",
        )
    assert (await env.today.metadata(item["item_id"]))["metadata"]["human_check"] is None


async def test_real_keyset_sources_and_view_cursor_are_bound_to_day_revision(today_system):
    env = today_system
    items = [await create_item(env, f"source-{index}") for index in range(5)]
    seen, after = [], ""
    while True:
        page = await env.today.sources(limit=2, after=after)
        seen.extend(row["item_ref"]["item_id"] for row in page["entries"])
        if page["next_after"] is None:
            break
        assert page["partial"] is True
        after = page["next_after"]
    assert seen == sorted(row["item_id"] for row in items) and len(set(seen)) == 5
    virtual = await env.today.day(DATE, ZONE, limit=1)
    assert virtual["next_cursor"] and virtual["day"]["revision"] == 0
    await env.today.place(place_payload(items[0]), trusted_channel="web")
    with pytest.raises(PersonalAgentError) as error:
        await env.today.day(DATE, ZONE, limit=1, cursor=virtual["next_cursor"])
    assert error.value.code == "version_conflict"


async def test_service_reopen_preserves_exact_requests_history_and_instance(today_system):
    env = today_system
    item = await create_item(env)
    receipt = await env.today.place(place_payload(item), trusted_channel="web")
    instance = env.today.store.identity()
    boundary = env.today.boundary.read_bytes()
    reopened = PersonalTodayService(env.engine, clock=lambda: env.clock[0])
    env.engine.personal_today = reopened
    assert reopened.store.identity() == instance and reopened.boundary.read_bytes() == boundary
    assert (await reopened.request_status("place-1"))["identity"] == receipt["identity"]
    assert len((await reopened.history("day", receipt["identity"]["day_id"]))["history"]) == 1
    assert (await reopened.place(place_payload(item), trusted_channel="web"))[
        "identity"
    ] == receipt["identity"]


async def test_move_change_journal_freezes_exact_source_and_target_identity(today_system):
    env = today_system
    item = await create_item(env)
    placed = await env.today.place(place_payload(item), trusted_channel="web")
    source_id = placed["identity"]["day_id"]
    placement_id = included(placed["view"])[0]["placement_id"]
    payload = {
        "request_id": "move-exact-journal",
        "day_id": source_id,
        "expected_revision": 1,
        "placement_id": placement_id,
        "target_date": "2030-01-02",
        "target_timezone": ZONE,
        "expected_target_revision": 0,
    }
    moved = await env.today.move(payload, trusted_channel="web")
    identity = moved["identity"]
    assert identity["source_day_id"] == source_id
    assert identity["source_day_revision"] == 2
    assert identity["day_id"] != source_id and identity["day_revision"] == 1
    change = await env.today._record("change", identity["change_id"], revision=1)
    assert change["identity"] == identity
    assert change["operation"] == "move" and change["request_id"] == payload["request_id"]
    assert change["source_refs"] == [item_ref(item)]
    source = await env.today.get(source_id, revision=identity["source_day_revision"])
    target = await env.today.get(identity["day_id"], revision=identity["day_revision"])
    assert included(source) == [] and len(included(target)) == 1
    assert source["day"]["placements"][0]["disposition"] == "exclude"
    assert included(target)[0]["item_ref"] == item_ref(item)
    await env.today.reorder(
        {
            "request_id": "later-target-review",
            "day_id": identity["day_id"],
            "expected_revision": 1,
            "placement_ids": [included(target)[0]["placement_id"]],
        },
        trusted_channel="cli",
    )
    assert await env.today._record("change", identity["change_id"], revision=1) == change
    assert (await env.today.move(payload, trusted_channel="web"))["identity"] == identity
    assert (await env.today.request_status(payload["request_id"]))["identity"] == identity


@pytest.mark.parametrize("replacement", ["missing", "new-instance", "foreign-owner"])
async def test_old_external_instance_pin_rejects_missing_or_replaced_today_database(
    today_system, replacement
):
    from naumi_agent.personal.today_store import TodayStore

    env = today_system
    item = await create_item(env)
    await env.today.place(place_payload(item), trusted_channel="web")
    original = env.today.store
    moved = original.base_dir.with_name("original-today-preserved")
    original.base_dir.rename(moved)
    if replacement != "missing":
        owner = env.today.owner_key if replacement == "new-instance" else "f" * 64
        other = TodayStore(original.base_dir, owner_key=owner)
        other.write(
            lambda tx: tx.put(
                "change",
                {"id": "tc_" + "e" * 32, "day_id": "", "workspace_key": env.today.workspace_key},
                expected_revision=0,
            )
        )
    try:
        with pytest.raises(PersonalAgentError) as error:
            reopened = PersonalTodayService(env.engine, clock=lambda: env.clock[0])
            await reopened.request_status("place-1")
        assert error.value.code == "storage_error"
        assert env.today.boundary.exists() and moved.exists()
    finally:
        # Preserve both real directories; no deletion or replacement of unrelated data.
        if original.base_dir.exists():
            original.base_dir.rename(original.base_dir.with_name("replacement-today-preserved"))
        moved.rename(original.base_dir)
