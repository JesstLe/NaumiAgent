"""Real local-HTTP paging, immutable sources and actual Item/Artifact outbox contracts."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from naumi_agent.personal.connection_models import digest
from naumi_agent.personal.google_calendar import normalize_event, occurrence_key
from naumi_agent.personal.models import PersonalAgentError
from tests.unit.test_personal_connection_service import (
    CONNECTION,
    MANUAL,
    assert_error,
    seed_connection,
)
from tests.unit.test_personal_connection_service import (
    calendar_endpoint as _calendar_endpoint,
)
from tests.unit.test_personal_connection_service import (
    connection_system as _connection_system,
)
from tests.unit.test_personal_connection_service import (
    signing_material as _signing_material,
)
from tests.unit.test_personal_connection_service import (
    system as _item_system,
)

calendar_endpoint = _calendar_endpoint
connection_system = _connection_system
signing_material = _signing_material
system = _item_system
LIST_PATH = "/calendar/v3/calendars/primary/events"


def event(identity="event_original", **updates):
    return {
        "id": identity,
        "status": "confirmed",
        "etag": '"etag-' + identity + '"',
        "summary": "准备实际评审材料",
        "description": "外部日程正文，不是执行授权。",
        "start": {"dateTime": "2030-01-02T09:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "end": {"dateTime": "2030-01-02T10:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "htmlLink": "https://calendar.google.com/calendar/event?eid=fixture",
        **updates,
    }


async def seed_event(fixture, raw=None):
    authority = fixture.service
    normalized = normalize_event(raw or event())
    connection = await authority._record("connection", CONNECTION)
    identity = authority._event_key(CONNECTION, normalized["id"])

    def save(tx):
        previous = tx.get("event", identity, missing=True)
        return tx.put(
            "event",
            {
                "id": identity,
                "connection_id": CONNECTION,
                "status": normalized["status"],
                "event_id": normalized["id"],
                "event": normalized,
                "auth_epoch": connection["auth_epoch"],
                "occurrence_key": occurrence_key(normalized),
                "payload_sha256": digest(normalized),
                "fetched_at": authority.stamp(),
                "available": True,
            },
            expected_revision=0 if previous is None else previous["revision"],
        )

    return await authority._write(save)


async def seed_cursor(fixture, token="cursor-old"):
    authority = fixture.service
    connection = await authority._record("connection", CONNECTION)
    return await authority._write(
        lambda tx: tx.put(
            "sync",
            {
                "id": CONNECTION,
                "connection_id": CONNECTION,
                "status": "ready",
                "auth_epoch": connection["auth_epoch"],
                "query_sha256": digest(
                    {
                        "provider": "google_calendar",
                        "calendar": "primary",
                        "singleEvents": False,
                        "showDeleted": True,
                    }
                ),
                "committed_token": token,
                "generation": "0" * 32,
                "page_count": 0,
                "event_count": 0,
                "masters_done": [],
            },
            expected_revision=0,
        )
    )


def queries(endpoint, *, instances=False):
    return (
        [
            parse_qs(urlsplit(row[1]).query)
            for row in endpoint.requests
            if urlsplit(row[1]).path.endswith("/instances")
            if instances
        ]
        if instances
        else [
            parse_qs(urlsplit(row[1]).query)
            for row in endpoint.requests
            if urlsplit(row[1]).path == LIST_PATH
        ]
    )


async def test_cursor_and_mirror_publish_only_after_all_actual_http_pages(connection_system):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    entered, release = threading.Event(), threading.Event()

    def calendar(query, _path):
        if query.get("pageToken") == ["page-two"]:
            entered.set()
            assert release.wait(timeout=10)
            return 200, {"items": [event("second_event")], "nextSyncToken": "cursor-final"}
        return 200, {"items": [event("first_event")], "nextPageToken": "page-two"}

    fixture.endpoint.calendar = calendar
    task = asyncio.create_task(authority._run_sync(CONNECTION))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        checkpoint = await authority._record("sync", CONNECTION)
        assert checkpoint["status"] == "reading" and checkpoint["page_count"] == 1
        assert checkpoint.get("committed_token") is None
        assert (await authority.events(CONNECTION))["events"] == []
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=10)
    page = await authority.events(CONNECTION)
    assert {row["event_id"] for row in page["events"]} == {"first_event", "second_event"}
    checkpoint = await authority._record("sync", CONNECTION)
    assert checkpoint["status"] == "ready" and checkpoint["committed_token"] == "cursor-final"
    assert len(queries(fixture.endpoint)) == 2


async def test_failed_incremental_page_preserves_cursor_and_resumes_exact_page(connection_system):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    await seed_event(fixture)
    await seed_cursor(fixture)
    failure = [True]

    def calendar(query, _path):
        assert query["syncToken"] == ["cursor-old"]
        if query.get("pageToken") == ["page-two"]:
            return (
                (503, {"error": "busy"})
                if failure[0]
                else (200, {"items": [event("second_new")], "nextSyncToken": "cursor-new"})
            )
        return 200, {"items": [event("first_new")], "nextPageToken": "page-two"}

    fixture.endpoint.calendar = calendar
    await authority._run_sync(CONNECTION)
    checkpoint = await authority._record("sync", CONNECTION)
    assert checkpoint["status"] == "reading" and checkpoint["committed_token"] == "cursor-old"
    assert [row["event_id"] for row in (await authority.events(CONNECTION))["events"]] == [
        "event_original"
    ]
    generation = checkpoint["generation"]
    failure[0] = False
    retry = await authority._record("connection", CONNECTION)
    deadline = authority._parse_sync_time(retry["next_sync_at"])
    assert deadline > authority.now()
    requests_before_deadline = len(fixture.endpoint.requests)
    # A recovered provider does not override the durable retry deadline.
    await authority._run_sync(CONNECTION)
    assert len(fixture.endpoint.requests) == requests_before_deadline
    waiting = await authority._record("sync", CONNECTION)
    assert waiting["generation"] == generation
    assert waiting["committed_token"] == "cursor-old"
    fixture.clock[0] = deadline + timedelta(seconds=1)
    await authority._run_sync(CONNECTION)
    final = await authority._record("sync", CONNECTION)
    assert final["generation"] == generation and final["committed_token"] == "cursor-new"
    assert {row["event_id"] for row in (await authority.events(CONNECTION))["events"]} == {
        "event_original",
        "first_new",
        "second_new",
    }
    assert [query.get("pageToken") for query in queries(fixture.endpoint)] == [
        None,
        ["page-two"],
        ["page-two"],
    ]


async def test_410_restarts_full_generation_and_invalidates_omitted_source_only_on_commit(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    await seed_event(fixture)
    old_ref = (await authority.events(CONNECTION))["events"][0]["source_ref"]
    cursor = await seed_cursor(fixture)

    def calendar(query, _path):
        if query.get("syncToken"):
            return 410, {"error": {"message": "expired cursor"}}
        return 200, {"items": [event("replacement")], "nextSyncToken": "cursor-full"}

    fixture.endpoint.calendar = calendar
    await authority._run_sync(CONNECTION)
    current = await authority._record("sync", CONNECTION)
    assert current["generation"] != cursor["generation"] and current["full"] is True
    assert current["committed_token"] == "cursor-full"
    assert [row["event_id"] for row in (await authority.events(CONNECTION))["events"]] == [
        "replacement"
    ]
    await assert_error("connection_source_revoked", authority.capture_source(old_ref, "main"))
    assert [query.get("syncToken") for query in queries(fixture.endpoint)] == [["cursor-old"], None]


async def test_sync_call_budget_resumes_generation_without_restarting_pages(connection_system):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)

    def calendar(query, _path):
        index = int(query.get("pageToken", ["page0"])[0][4:])
        result = {"items": [event(f"event_{index:03d}")]}
        result["nextSyncToken" if index == 32 else "nextPageToken"] = (
            "cursor-final" if index == 32 else f"page{index + 1}"
        )
        return 200, result

    fixture.endpoint.calendar = calendar
    await authority._run_sync(CONNECTION)
    checkpoint = await authority._record("sync", CONNECTION)
    assert checkpoint["status"] == "reading" and checkpoint["page_count"] == 32
    assert (await authority.events(CONNECTION))["events"] == []
    generation = checkpoint["generation"]
    await authority._run_sync(CONNECTION)
    current = await authority._record("sync", CONNECTION)
    assert current["status"] == "ready" and current["generation"] == generation
    assert len((await authority.events(CONNECTION, limit=100))["events"]) == 33
    assert len(queries(fixture.endpoint)) == 33


async def test_page_token_cycle_never_commits_a_cursor(connection_system):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    await seed_event(fixture)
    await seed_cursor(fixture)
    fixture.endpoint.calendar = lambda _query, _path: (
        200,
        {"items": [], "nextPageToken": "cycle-token"},
    )
    await authority._run_sync(CONNECTION)
    current = await authority._record("sync", CONNECTION)
    assert current["status"] == "reading" and current["committed_token"] == "cursor-old"
    connection = await authority._record("connection", CONNECTION)
    assert connection["sync_status"] == "error" and connection["last_error_code"] == "invalid_event"
    assert len(queries(fixture.endpoint)) == 2


async def test_instance_window_is_frozen_across_pages_and_bounded_continuation(connection_system):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    master = event("series_original", recurrence=["RRULE:FREQ=DAILY"])

    def calendar(query, path):
        if not path.endswith("/instances"):
            return 200, {"items": [master], "nextSyncToken": "cursor-series"}
        fixture.clock[0] += timedelta(seconds=1)
        index = int(query.get("pageToken", ["instance0"])[0][8:])
        instance = event(
            f"instance_{index:03d}",
            recurringEventId="series_original",
            originalStartTime={"dateTime": f"2030-01-{index % 28 + 1:02d}T09:00:00+08:00"},
        )
        body = {"items": [instance]}
        if index < 32:
            body["nextPageToken"] = f"instance{index + 1}"
        return 200, body

    fixture.endpoint.calendar = calendar
    await authority._run_sync(CONNECTION)
    checkpoint = await authority._record("sync", CONNECTION)
    assert checkpoint["status"] == "reading"
    assert len(queries(fixture.endpoint)) + len(queries(fixture.endpoint, instances=True)) == 32
    window = (checkpoint["window_start"], checkpoint["window_end"])
    fixture.clock[0] += timedelta(minutes=1)
    await authority._run_sync(CONNECTION)
    current = await authority._record("sync", CONNECTION)
    assert current["status"] == "ready"
    assert (current["window_start"], current["window_end"]) == window
    windows = {
        (tuple(query["timeMin"]), tuple(query["timeMax"]))
        for query in queries(fixture.endpoint, instances=True)
    }
    assert len(windows) == 1
    assert len(queries(fixture.endpoint, instances=True)) == 33


async def test_current_staged_pages_are_not_hidden_by_ten_thousand_old_generations(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)

    def old_pages(tx):
        for index in range(10001):
            tx.put(
                "sync",
                {
                    "id": f"old-page-{index:05d}",
                    "connection_id": CONNECTION,
                    "status": "page",
                    "generation": "old-generation",
                    "events": [],
                },
                expected_revision=0,
            )

    await authority._write(old_pages)
    fixture.endpoint.calendar = lambda _query, _path: (
        200,
        {"items": [event()], "nextSyncToken": "cursor-current"},
    )
    await authority._run_sync(CONNECTION)
    assert [value["event_id"] for value in (await authority.events(CONNECTION))["events"]] == [
        "event_original"
    ]
    assert (await authority._record("sync", CONNECTION))["committed_token"] == "cursor-current"


async def test_cancelled_master_invalidates_cached_instances_and_exact_source(connection_system):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    await seed_event(fixture, event("series_original", recurrence=["RRULE:FREQ=DAILY"]))
    await seed_event(
        fixture,
        event(
            "instance_original",
            recurringEventId="series_original",
            originalStartTime={"dateTime": "2030-01-02T09:00:00+08:00"},
        ),
    )
    ref = (await authority.events(CONNECTION))["events"][0]["source_ref"]
    await seed_cursor(fixture)
    fixture.endpoint.calendar = lambda _query, _path: (
        200,
        {
            "items": [{"id": "series_original", "status": "cancelled", "etag": '"cancelled"'}],
            "nextSyncToken": "cursor-cancelled",
        },
    )
    await authority._run_sync(CONNECTION)
    assert (await authority.events(CONNECTION))["events"] == []
    await assert_error("connection_source_revoked", authority.capture_source(ref, "main"))


async def test_source_refs_keep_exact_historical_bytes_and_new_grant_does_not_replace_old(
    connection_system,
):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    await seed_event(fixture)
    actor = fixture.engine.personal_agents.create(
        name="Mira", instructions="只读日程", request_id="agent-source", tool_names=[]
    )
    current = await authority._record("connection", CONNECTION)
    await authority.grant(
        CONNECTION,
        {
            "request_id": "grant-one",
            "expected_revision": current["revision"],
            "actor_id": actor.id,
            "actor_version": actor.version,
        },
        trusted_channel=MANUAL,
    )
    original = (await authority.events(CONNECTION, actor.id))["events"][0]["source_ref"]
    await seed_event(fixture, event(summary="已修改的新日程正文", etag='"new-etag"'))
    assert await authority.capture_source(original, actor.id) == original
    current = await authority._record("connection", CONNECTION)
    await authority.grant(
        CONNECTION,
        {
            "request_id": "revoke-one",
            "expected_revision": current["revision"],
            "actor_id": actor.id,
        },
        trusted_channel=MANUAL,
        revoke=True,
    )
    current = await authority._record("connection", CONNECTION)
    await authority.grant(
        CONNECTION,
        {
            "request_id": "grant-two",
            "expected_revision": current["revision"],
            "actor_id": actor.id,
            "actor_version": actor.version,
        },
        trusted_channel=MANUAL,
    )
    await assert_error("connection_source_revoked", authority.capture_source(original, actor.id))
    latest = (await authority.events(CONNECTION, actor.id))["events"][0]["source_ref"]
    assert (
        latest["grant_id"] != original["grant_id"]
        and latest["grant_revision"] > original["grant_revision"]
    )
    assert await authority.capture_source(latest, actor.id) == latest


async def test_event_batch_aborts_if_authorization_epoch_changes_after_mirror_read(
    connection_system, monkeypatch
):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    await seed_event(fixture)
    actual_read = authority._read
    fired = False

    async def read_then_reauthorize(callback):
        nonlocal fired
        result = await actual_read(callback)
        if not fired and isinstance(result, list) and result and result[0].get("event_id"):
            fired = True
            await authority._write(
                lambda tx: authority._change(
                    tx, "connection", CONNECTION, {"auth_epoch": 2, "grants": {}}
                )
            )
        return result

    monkeypatch.setattr(authority, "_read", read_then_reauthorize)
    with pytest.raises(PersonalAgentError):
        await authority.events(CONNECTION)
    assert fired


async def test_source_material_is_real_exact_text_and_disconnect_revokes_read(connection_system):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    await seed_event(fixture)
    original = (await authority.events(CONNECTION))["events"][0]["source_ref"]
    ref = await authority._source_artifact(original, "main")
    snapshot, raw = await fixture.engine.personal_artifacts.read(
        actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
    )
    assert snapshot["kind"] == "material" and snapshot["suffix"] == ".txt"
    content = json.loads(raw)
    assert content["source"] == original and content["event"]["summary"] == event()["summary"]
    current = await authority._record("connection", CONNECTION)
    await authority.disconnect(
        CONNECTION,
        {
            "request_id": "disconnect-source",
            "expected_revision": current["revision"],
            "remote_revoke": False,
        },
        trusted_channel=MANUAL,
    )
    with pytest.raises(PersonalAgentError):
        await fixture.engine.personal_artifacts.read(
            actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
        )


async def test_derived_material_cannot_remove_revoked_calendar_ancestry(connection_system):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    await seed_event(fixture)
    source = (await authority.events(CONNECTION))["events"][0]["source_ref"]
    source_artifact = await authority._source_artifact(source, "main")
    derived = await fixture.engine.personal_artifacts.publish(
        actor_id="main",
        request_id="derived-calendar-original",
        title="派生摘要",
        kind="material",
        filename="derived.txt",
        content="已从指定日历资料整理。".encode(),
        source_refs=[source_artifact],
        evidence={},
    )
    assert derived["source_refs"] == [source_artifact]
    current = await authority._record("connection", CONNECTION)
    await authority.disconnect(
        CONNECTION,
        {
            "request_id": "disconnect-ancestry",
            "expected_revision": current["revision"],
            "remote_revoke": False,
        },
        trusted_channel=MANUAL,
    )
    with pytest.raises(PersonalAgentError):
        await fixture.engine.personal_artifacts.read(
            actor_id="main", artifact_id=derived["artifact_id"], version=derived["version"]
        )


async def test_actual_item_create_then_binding_crash_recovers_one_item_and_schedule(
    connection_system, monkeypatch
):
    fixture, authority = connection_system, connection_system.service
    await seed_connection(fixture)
    await seed_event(fixture)
    source = (await authority.events(CONNECTION))["events"][0]
    payload = {
        "request_id": "event-item-original",
        "event_revision": source["revision"],
        "actor_id": "main",
        "title": "准备评审资料",
        "instructions": "核对已选择的日历来源并保存报告",
        "schedule": {
            "mode": "now",
            "timezone": "Asia/Shanghai",
            "misfire_policy": "run_once",
            "grace_seconds": 3600,
        },
    }
    actual_create = fixture.items.create
    committed = []

    async def create_then_crash(fields, caller_actor=None):
        item = await actual_create(fields, caller_actor=caller_actor)
        committed.append(item)
        raise RuntimeError("fixture crash after actual ItemStore commit, before connection binding")

    monkeypatch.setattr(fixture.items, "create", create_then_crash)
    with pytest.raises(RuntimeError, match="after actual ItemStore"):
        await authority.event_to_item(
            CONNECTION, source["event_id"], payload, trusted_channel=MANUAL
        )
    assert len(committed) == 1
    assert len(fixture.items.store.list(owner_key=fixture.items.owner_key)) == 1
    assert len(fixture.engine.scheduler_runner.store.list_jobs()) == 1
    monkeypatch.setattr(fixture.items, "create", actual_create)
    recovered = await authority.event_to_item(
        CONNECTION, source["event_id"], payload, trusted_channel=MANUAL
    )
    assert recovered["item_id"] == committed[0]["item_id"]
    assert len(fixture.items.store.list(owner_key=fixture.items.owner_key)) == 1
    assert len(fixture.engine.scheduler_runner.store.list_jobs()) == 1
    assert len(await fixture.engine.personal_artifacts.list(actor_id="main")) == 1
    assert fixture.port.submissions == []
    await seed_event(fixture, event(summary="后续修订日程", etag='"later-version"'))
    replay = await authority.event_to_item(
        CONNECTION, source["event_id"], payload, trusted_channel=MANUAL
    )
    assert replay["item_id"] == recovered["item_id"]
    assert len(fixture.items.store.list(owner_key=fixture.items.owner_key)) == 1
