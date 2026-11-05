"""Committed real Google-adapter HTTP mirrors, enrolled feed and exact authority."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
from datetime import timedelta

import pytest

from naumi_agent.personal.calendar_changes import normalize_change_ref
from naumi_agent.personal.connection_models import digest
from naumi_agent.personal.connection_store import _SPEC
from naumi_agent.personal.connections import PersonalConnectionService
from naumi_agent.personal.google_calendar import normalize_event
from naumi_agent.personal.models import PersonalAgentError
from tests.unit.test_personal_calendar_sync import event, seed_cursor, seed_event
from tests.unit.test_personal_connection_service import (
    CONNECTION,
    MANUAL,
    FixtureService,
    seed_connection,
)
from tests.unit.test_personal_connection_service import calendar_endpoint as _calendar_endpoint
from tests.unit.test_personal_connection_service import connection_system as _connection_system
from tests.unit.test_personal_connection_service import signing_material as _signing_material
from tests.unit.test_personal_connection_service import system as _item_system

calendar_endpoint = _calendar_endpoint
connection_system = _connection_system
signing_material = _signing_material
system = _item_system


async def synchronize(env, values, token="next-token", *, full=False):
    service = env.service
    if full:
        original = await service._record("sync", CONNECTION)
        await service._write(
            lambda tx: tx.put(
                "sync",
                {**original, "committed_token": None, "status": "ready"},
                expected_revision=original["revision"],
            )
        )
    env.endpoint.calendar = lambda _query, _path: (200, {"items": values, "nextSyncToken": token})
    await service._run_sync(CONNECTION)
    saved = await service._record("sync", CONNECTION)
    assert saved["status"] == "ready" and saved["committed_token"] == token


async def reference(env, row, actor="main"):
    connection = await env.service._record("connection", CONNECTION)
    return env.service.change_reference(row, connection, actor)


async def initialized(env):
    await seed_connection(env)
    await synchronize(env, [event()], "initial")
    return await env.service.enroll_changes(CONNECTION, "main")


async def assert_error(code, awaitable):
    with pytest.raises(PersonalAgentError) as exc:
        await awaitable
    assert exc.value.code == code


async def test_public_composition_and_legacy_schema_checksum_are_unchanged(connection_system):
    env = connection_system
    assert hasattr(PersonalConnectionService, "capture_change")
    assert not _SPEC.domain_hashes and not _SPEC.lease_proofs
    await seed_connection(env)
    await seed_event(env)
    before = env.service.store.db_path.read_bytes()
    head = await env.service.change_head(CONNECTION, "main")
    assert head["head"] == 0 and head["last_sync_at"] is None
    assert (await env.service.change_page(CONNECTION, "main"))["changes"] == []
    assert env.service.store.db_path.read_bytes() == before
    assert env.endpoint.requests == []
    with sqlite3.connect(env.service.store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert {
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } == _SPEC.tables


async def test_explicit_enrollment_is_idempotent_baseline_not_provider_creation(connection_system):
    env = connection_system
    await seed_connection(env)
    await synchronize(env, [event("first"), event("second")], "initial")
    assert (await env.service.change_head(CONNECTION, "main"))["head"] == 0
    calls = len(env.endpoint.requests)
    heads = await asyncio.gather(
        *(env.service.enroll_changes(CONNECTION, "main") for _ in range(4))
    )
    assert all(h == heads[0] for h in heads) and heads[0]["head"] == 2
    page = await env.service.change_page(CONNECTION, "main", limit=1)
    assert page["next_sequence"] == 1 and page["partial"] and page["head"] == 2
    second = await env.service.change_page(CONNECTION, "main", after_sequence=1)
    assert second["next_sequence"] == 2 and not second["partial"]
    for row in page["changes"] + second["changes"]:
        assert row["change_kind"] == "baseline" and row["reason"] == "enrollment_snapshot"
        assert row["before_ref"] is None and row["revision"] == 1
        assert event()["summary"] not in json.dumps(row, ensure_ascii=False)
        assert "description" not in json.dumps(row["after_ref"])
    assert len(env.endpoint.requests) == calls


async def test_committed_a_b_a_and_repeated_same_payload_preserve_distinct_sequence(
    connection_system,
):
    env = connection_system
    await initialized(env)
    original = (await env.service.change_page(CONNECTION, "main"))["changes"][0]
    await synchronize(env, [event(summary="B", etag='"B"')], "B")
    await synchronize(env, [event()], "A-returned")
    await synchronize(env, [event()], "same-A")
    page = await env.service.change_page(CONNECTION, "main")
    assert page["head"] == 3
    assert [r["sequence"] for r in page["changes"]] == [1, 2, 3]
    assert [r["change_kind"] for r in page["changes"]] == ["baseline", "updated", "updated"]
    assert (
        page["changes"][2]["after_ref"]["payload_sha256"] == original["after_ref"]["payload_sha256"]
    )
    assert page["changes"][2]["after_ref"]["event_revision"] == 3
    ref = await reference(env, page["changes"][1])
    captured = await env.service.capture_change(ref, "main")
    assert captured["before"]["summary"] == event()["summary"]
    assert captured["after"]["summary"] == "B"
    assert captured["current"]["event"]["summary"] == event()["summary"]
    await assert_error(
        "source_stale", env.service.capture_change(ref, "main", require_current=True)
    )
    last_ref = await reference(env, page["changes"][-1])
    assert (await env.service.capture_change(last_ref, "main", require_current=True))[
        "reference"
    ] == last_ref


async def test_created_cancelled_missing_and_restored_are_not_conflated(connection_system):
    env = connection_system
    await initialized(env)
    await synchronize(env, [event("second")], "created")
    await synchronize(
        env, [{"id": "event_original", "status": "cancelled", "etag": '"cancel"'}], "cancelled"
    )
    rows = (await env.service.change_page(CONNECTION, "main"))["changes"]
    assert rows[1]["change_kind"] == "created"
    cancelled = rows[-1]
    assert cancelled["change_kind"] == "cancelled" and cancelled["reason"] == "provider_event"
    cancelled_ref = await reference(env, cancelled)
    captured = await env.service.capture_change(cancelled_ref, "main", require_current=True)
    assert set(captured["before"]) == set(captured["after"]) == {"status", "available"}
    assert set(captured["current"]["event"]) == {"status", "available"}
    assert event()["summary"] not in json.dumps(captured, ensure_ascii=False)
    await synchronize(env, [event()], "restored")
    restored = (await env.service.change_page(CONNECTION, "main"))["changes"][-1]
    assert restored["change_kind"] == "restored"
    await synchronize(env, [], "full-empty", full=True)
    rows = (await env.service.change_page(CONNECTION, "main"))["changes"]
    missing = next(r for r in reversed(rows) if r["after_ref"]["event_id"] == "event_original")
    assert (
        missing["change_kind"] == "unavailable" and missing["reason"] == "missing_in_full_snapshot"
    )
    captured = await env.service.capture_change(
        await reference(env, missing), "main", require_current=True
    )
    assert captured["current"]["available"] is False
    assert set(captured["current"]["event"]) == {"status", "available"}
    assert event()["description"] not in json.dumps(captured, ensure_ascii=False)


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "master_change,reason", [("cancel", "master_cancelled"), ("replace", "series_replaced")]
)
async def test_recurring_instance_original_identity_and_master_reason(
    connection_system, full, master_change, reason
):
    env = connection_system
    await seed_connection(env)
    await seed_event(env, event("master", recurrence=["RRULE:FREQ=DAILY"]))
    raw = event(
        "instance",
        recurringEventId="master",
        originalStartTime={"dateTime": "2030-01-02T09:00:00+08:00"},
    )
    await seed_event(env, raw)
    await seed_cursor(env)
    await env.service.enroll_changes(CONNECTION, "main")
    changed = (
        {"id": "master", "status": "cancelled"} if master_change == "cancel" else event("master")
    )
    await synchronize(env, [changed], "master-change", full=full)
    rows = (await env.service.change_page(CONNECTION, "main"))["changes"]
    instance = next(r for r in reversed(rows) if r["after_ref"]["event_id"] == "instance")
    assert instance["change_kind"] == "unavailable" and instance["reason"] == reason
    assert instance["after_ref"]["occurrence_key"] == instance["before_ref"]["occurrence_key"]
    captured = await env.service.capture_change(
        await reference(env, instance), "main", require_current=True
    )
    assert set(captured["current"]["event"]) == {"status", "available"}


async def test_failed_staging_has_no_change_and_commit_failure_rolls_back_feed_with_event(
    connection_system, monkeypatch
):
    env = connection_system
    await initialized(env)
    entered, release = threading.Event(), threading.Event()

    def pages(query, _path):
        if query.get("pageToken"):
            entered.set()
            assert release.wait(10)
            return 503, {"error": "busy"}
        return 200, {"items": [event(summary="staged-only")], "nextPageToken": "page2"}

    env.endpoint.calendar = pages
    task = asyncio.create_task(env.service._run_sync(CONNECTION))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (await env.service.change_head(CONNECTION, "main"))["head"] == 1
        assert (await env.service.enroll_changes(CONNECTION, "main"))["head"] == 1
    finally:
        release.set()
        await task
    assert (await env.service.change_head(CONNECTION, "main"))["head"] == 1
    connection = await env.service._record("connection", CONNECTION)
    env.clock[0] = env.service._parse_sync_time(connection["next_sync_at"]) + timedelta(seconds=1)
    env.endpoint.calendar = lambda _q, _p: (200, {"items": [], "nextSyncToken": "finished"})
    original = env.service._append_calendar_change

    def fail_after_change(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash before committed mirror/token")

    monkeypatch.setattr(env.service, "_append_calendar_change", fail_after_change)
    await env.service._run_sync(CONNECTION)
    assert (await env.service.change_head(CONNECTION, "main"))["head"] == 1
    assert (
        await env.service._record("event", env.service._event_key(CONNECTION, "event_original"))
    )["event"]["summary"] == event()["summary"]
    assert (await env.service._record("sync", CONNECTION))["committed_token"] == "initial"


async def test_reopen_same_original_store_and_credential_does_not_enroll_again(connection_system):
    env = connection_system
    first = await initialized(env)
    await synchronize(env, [event(summary="updated")], "updated")
    replacement = FixtureService(
        env.engine,
        store=env.service.store,
        vault=env.service.vault,
        adapter=env.service.adapter,
        clock=lambda: env.clock[0],
    )
    assert (await replacement.change_head(CONNECTION, "main"))["head"] == 2
    assert (await replacement.enroll_changes(CONNECTION, "main"))["head"] == 2
    assert (await replacement.change_head(CONNECTION, "main"))["connection_instance"] == first[
        "connection_instance"
    ]
    assert len(env.endpoint.requests) == 2


@pytest.mark.parametrize(
    "field", ["connection_instance", "auth_epoch", "change_digest", "grant_id", "grant_revision"]
)
async def test_reference_bindings_fail_closed(connection_system, field):
    env = connection_system
    await initialized(env)
    row = (await env.service.change_page(CONNECTION, "main"))["changes"][0]
    ref = await reference(env, row)
    if field in {"auth_epoch", "grant_revision"}:
        ref[field] += 1
    elif field == "grant_id":
        ref[field], ref["grant_revision"] = "cg_" + "a" * 32, 1
    else:
        ref[field] = ("a" if ref[field][0] != "a" else "b") + ref[field][1:]
    with pytest.raises(PersonalAgentError):
        await env.service.capture_change(ref, "main")


async def test_actor_grant_revoke_regrant_and_profile_version_cannot_replace_old_source(
    connection_system,
):
    env = connection_system
    await initialized(env)
    actor = env.engine.personal_agents.create(
        name="Mira", instructions="日程跟进", request_id="changes-agent", tool_names=[]
    )
    row = (await env.service.change_page(CONNECTION, "main"))["changes"][0]
    await assert_error("scope_denied", env.service.change_page(CONNECTION, actor.id))
    for index, revoke in enumerate((False, True, False)):
        connection = await env.service._record("connection", CONNECTION)
        await env.service.grant(
            CONNECTION,
            {
                "request_id": "changes-grant-" + str(index),
                "expected_revision": connection["revision"],
                "actor_id": actor.id,
                **({} if revoke else {"actor_version": actor.version}),
            },
            trusted_channel=MANUAL,
            revoke=revoke,
        )
        if index == 0:
            old_ref = await reference(env, row, actor.id)
            assert (await env.service.capture_change(old_ref, actor.id))["after"][
                "summary"
            ] == event()["summary"]
    await assert_error("connection_source_revoked", env.service.capture_change(old_ref, actor.id))
    ref = await reference(env, row, actor.id)
    env.engine.personal_agents.update(actor.id, actor.version, instructions="新职责")
    await assert_error("scope_denied", env.service.capture_change(ref, actor.id))


async def test_epoch_changes_never_return_previous_epoch_before_body(connection_system):
    env = connection_system
    await initialized(env)
    old_row = (await env.service.change_page(CONNECTION, "main"))["changes"][0]
    old_ref = await reference(env, old_row)
    connection = await env.service._record("connection", CONNECTION)
    await env.service._write(
        lambda tx: tx.put(
            "connection", {**connection, "auth_epoch": 2}, expected_revision=connection["revision"]
        )
    )
    await synchronize(env, [event(summary="current-epoch")], "new-epoch")
    await assert_error("connection_source_revoked", env.service.capture_change(old_ref, "main"))
    new_row = (await env.service.change_page(CONNECTION, "main"))["changes"][-1]
    result = await env.service.capture_change(
        await reference(env, new_row), "main", require_current=True
    )
    assert result["before"] is None and result["after"]["summary"] == "current-epoch"


async def test_final_snapshot_after_source_await_hides_new_tombstone(
    connection_system, monkeypatch
):
    env = connection_system
    await initialized(env)
    row = (await env.service.change_page(CONNECTION, "main"))["changes"][0]
    ref = await reference(env, row)
    original = env.service._credential
    calls = 0

    async def revoke_during_credential(connection):
        nonlocal calls
        result = await original(connection)
        calls += 1
        if calls == 2:
            current = await env.service._record(
                "event", env.service._event_key(CONNECTION, "event_original")
            )
            await env.service._write(
                lambda tx: env.service._publish_calendar_event(
                    tx,
                    {**current, "available": False},
                    current["revision"],
                    "missing_in_full_snapshot",
                    "a" * 32,
                )
            )
        return result

    monkeypatch.setattr(env.service, "_credential", revoke_during_credential)
    captured = await env.service.capture_change(ref, "main")
    assert set(captured["after"]) == {"status", "available"}
    assert set(captured["current"]["event"]) == {"status", "available"}


@pytest.mark.parametrize("corruption", ["head", "gap", "history", "parent"])
async def test_feed_corruption_does_not_reset_or_skip_missing_history(
    connection_system, corruption
):
    env = connection_system
    await initialized(env)
    await synchronize(env, [event(summary="second-version")], "second")
    rows = (await env.service.change_page(CONNECTION, "main"))["changes"]
    with sqlite3.connect(env.service.store.db_path) as db:
        if corruption == "head":
            db.execute("DELETE FROM connection_records WHERE kind='calendar_change_head'")
        elif corruption == "gap":
            db.execute(
                "DELETE FROM connection_records WHERE kind='calendar_change' AND id=?",
                (rows[0]["id"],),
            )
        elif corruption == "parent":
            db.execute(
                "UPDATE connection_records SET parent_id='foreign' "
                "WHERE kind='calendar_change' AND id=?",
                (rows[0]["id"],),
            )
        else:
            db.execute("DROP TRIGGER connection_history_immutable_update")
            db.execute(
                "UPDATE connection_history SET payload='{}' WHERE kind='event' AND revision=1"
            )
            sql = _SPEC.triggers["connection_history_immutable_update"]
            db.execute(sql)
    if corruption != "history":
        await assert_error("storage_error", env.service.change_page(CONNECTION, "main"))
    else:
        await assert_error(
            "storage_error", env.service.capture_change(await reference(env, rows[0]), "main")
        )


async def test_append_only_changes_strict_inputs_and_cursor_bounds(connection_system):
    env = connection_system
    await initialized(env)
    row = (await env.service.change_page(CONNECTION, "main"))["changes"][0]
    await assert_error(
        "immutable_calendar_change",
        env.service._write(
            lambda tx: tx.put(
                "calendar_change", {**row, "reason": "provider_event"}, expected_revision=1
            )
        ),
    )
    for value in (-1, True, 1.5):
        with pytest.raises(PersonalAgentError):
            await env.service.change_page(CONNECTION, "main", after_sequence=value)
    await assert_error(
        "source_stale", env.service.change_page(CONNECTION, "main", after_sequence=2)
    )
    ref = await reference(env, row)
    for key, value in (("sequence", True), ("extra", "invalid"), ("change_id", "cch_invalid")):
        with pytest.raises(PersonalAgentError):
            normalize_change_ref({**copy.deepcopy(ref), key: value})


async def test_change_page_never_contains_cached_private_bodies_or_credentials(connection_system):
    env = connection_system
    await initialized(env)
    await synchronize(
        env, [event(summary="private-updated", description="private-description")], "changed"
    )
    page = await env.service.change_page(CONNECTION, "main")
    raw = json.dumps(page, ensure_ascii=False)
    assert "private-updated" not in raw and "private-description" not in raw
    assert "fixture-refresh" not in raw and "credential" not in raw
    current = await env.service._record("connection", CONNECTION)
    row = page["changes"][-1]
    assert normalize_change_ref(env.service.change_reference(row, current, "main"))[
        "change_digest"
    ] == digest(row)


async def test_complete_event_history_is_verified_not_only_the_referenced_revision(
    connection_system,
):
    env = connection_system
    await initialized(env)
    row = (await env.service.change_page(CONNECTION, "main"))["changes"][0]
    ref = await reference(env, row)
    await synchronize(env, [event(summary="intermediate")], "intermediate")
    await synchronize(env, [event()], "latest")
    with sqlite3.connect(env.service.store.db_path) as db:
        db.execute("DROP TRIGGER connection_history_immutable_update")
        db.execute(
            "UPDATE connection_history SET sha256=? WHERE kind='event' AND revision=2", ("a" * 64,)
        )
        db.execute(_SPEC.triggers["connection_history_immutable_update"])
    await assert_error("storage_error", env.service.capture_change(ref, "main"))


async def test_moved_instance_keeps_original_start_identity_and_authenticates_full_series(
    connection_system,
):
    env = connection_system
    await seed_connection(env)
    await seed_event(env, event("master", recurrence=["RRULE:FREQ=DAILY"]))
    raw = event(
        "instance",
        recurringEventId="master",
        originalStartTime={"dateTime": "2030-01-02T09:00:00+08:00"},
    )
    await seed_event(env, raw)
    await seed_cursor(env)
    await env.service.enroll_changes(CONNECTION, "main")
    moved = {**raw, "start": {"dateTime": "2030-01-03T11:00:00+08:00", "timeZone": "Asia/Shanghai"}}
    await synchronize(env, [moved], "moved")
    row = (await env.service.change_page(CONNECTION, "main"))["changes"][-1]
    assert row["after_ref"]["event_id"] == "instance"
    assert row["before_ref"]["occurrence_key"] == row["after_ref"]["occurrence_key"]
    assert row["changed_fields"] == ["start"]
    result = await env.service.capture_change(
        await reference(env, row), "main", require_current=True
    )
    assert result["after"]["start"] == normalize_event(moved)["start"]


async def test_final_connection_revocation_during_credential_await_clears_entire_projection(
    connection_system, monkeypatch
):
    env = connection_system
    await initialized(env)
    row = (await env.service.change_page(CONNECTION, "main"))["changes"][0]
    ref = await reference(env, row)
    original, calls = env.service._credential, 0

    async def disconnect_after_await(connection):
        nonlocal calls
        result = await original(connection)
        calls += 1
        if calls == 2:
            current = await env.service._record("connection", CONNECTION)
            await env.service._write(
                lambda tx: tx.put(
                    "connection",
                    {**current, "status": "disconnected"},
                    expected_revision=current["revision"],
                )
            )
        return result

    monkeypatch.setattr(env.service, "_credential", disconnect_after_await)
    await assert_error("connection_revoked", env.service.capture_change(ref, "main"))
