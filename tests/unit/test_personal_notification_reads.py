"""Bounded notification reads: real journals, controlled authority and zero new execution."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.notifications import router
from naumi_agent.personal import runtime
from naumi_agent.personal.item_models import canonical
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_models import identity, record_digest
from naumi_agent.personal.notification_store import NotificationStore
from naumi_agent.personal.notifications import PersonalNotificationService
from tests.unit.test_personal_notification_service import (
    _context_fixture_event,
    _context_fixture_read,
    configure,
)
from tests.unit.test_personal_notification_service import (
    authority_system as _authority_system,
)
from tests.unit.test_personal_notification_service import (
    context_calendar as _context_calendar,
)
from tests.unit.test_personal_notification_service import (
    notifications as _notifications,
)

authority_system = _authority_system
notifications = _notifications
context_calendar = _context_calendar
pytestmark = pytest.mark.usefixtures("runtime_payload_key")


def box(service, event):
    return {
        "id": identity("no_", {"event_id": event["id"]}),
        "workspace_key": event["workspace_key"],
        "event_id": event["id"],
        "actor_id": event["actor_id"],
        "kind": event["kind"],
        "status": "pending",
        "first_seen_at": service.stamp(),
        "batch_id": None,
    }


async def seed(service, count=1, *, actor="main", kind="item_result", key="read", read=False):
    """Write only explicit temporary notification facts, never an Item/Job/Run."""

    def write(tx):
        values = []
        for number in range(count):
            event = tx.put(
                "event",
                _context_fixture_event(service, f"{key}-{number}", actor=actor, kind=kind),
                expected_revision=0,
            )
            tx.put("outbox", box(service, event), expected_revision=0)
            if read:
                tx.put("notice_read", _context_fixture_read(service, event), expected_revision=0)
            values.append(event)
        return sorted(values, key=lambda value: value["id"])

    return await service._write(write)


async def append_read(service, event):
    return await service._write(
        lambda tx: tx.put("notice_read", _context_fixture_read(service, event), expected_revision=0)
    )


def controlled_authority(service, monkeypatch, *, hook=None, unavailable=()):
    """Test the shared authorization seam, not a substitute provider implementation."""
    calls, denied = [], set(unavailable)

    async def authorize(event, caller):
        if event["workspace_key"] != service.workspace_key or (
            caller is not None and caller != event["actor_id"]
        ):
            raise PersonalAgentError("source_unavailable", "来源不在当前读取范围")
        calls.append((event["id"], caller))
        ordinal = sum(identity == event["id"] for identity, _ in calls)
        if hook is not None:
            await hook(event, ordinal)
        if event["id"] in denied:
            raise PersonalAgentError("source_unavailable", "受控来源当前已失权")
        return {
            "title": f"授权标题 {event['id']} {ordinal}",
            "actor_name": "主 Agent"
            if event["actor_id"] == "main"
            else service.personal.get(event["actor_id"]).name,
            "availability": "available",
            "jump": {"kind": "result", "id": "pio_" + event["id"][3:]},
        }

    monkeypatch.setattr(service, "_authorize_event", authorize)
    return calls, denied


def facts(service):
    """Authenticate every notification fact and request count for GET no-write checks."""
    return service.store.read(
        lambda tx: {
            "records": {
                kind: tx.list(kind, limit=10001) for kind in sorted(service.store.spec.kinds)
            },
            "requests": tx.db.execute("SELECT count(*) FROM notification_requests").fetchone()[0],
            "lease_history": tx.db.execute(
                "SELECT count(*) FROM notification_lease_history"
            ).fetchone()[0],
        }
    )


def seal(tx, service, event, *, workspace=None, wrong_member=False):
    workspace = workspace or service.workspace_key
    refs = [
        {
            "event_id": event["id"],
            "event_digest": "0" * 64 if wrong_member else record_digest(event),
        }
    ]
    batch = tx.put(
        "batch",
        {
            "id": identity("nb_", {"workspace_key": workspace, "members": refs}),
            "workspace_key": workspace,
            "actor_id": event["actor_id"],
            "kind": event["kind"],
            "member_refs": refs,
            "created_at": service.stamp(),
        },
        expected_revision=0,
    )
    current = tx.get("outbox", identity("no_", {"event_id": event["id"]}))
    tx.put(
        "outbox",
        {**current, "status": "sealed", "batch_id": batch["id"]},
        expected_revision=current["revision"],
    )
    return batch


async def test_list_reads_source_traversal_is_bounded_by_page_not_all_normal_events(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    events = await seed(service, 8)
    calls, _ = controlled_authority(service, monkeypatch)

    async def forbidden_global_projection(*args, **kwargs):
        raise AssertionError("List counts must not globally project normal sources")

    monkeypatch.setattr(service, "_entries", forbidden_global_projection)
    before = facts(service)
    result = await service.list(filter="all", limit=2)
    assert [row["id"] for row in result["entries"]] == [row["id"] for row in events[:2]]
    assert Counter(identity for identity, _ in calls) == {events[0]["id"]: 2, events[1]["id"]: 2}
    assert all(row["title"].endswith(" 2") for row in result["entries"])
    assert result["unread_count"] == 8 and result["attention_count"] == 0
    assert result["counts_consistency"] == "journal_snapshot"
    assert result["consistency"] == "live_keyset" and result["partial"] is False
    assert result["next_after"] == events[1]["id"]
    assert facts(service) == before
    assert not env.items.executor.jobs.db_path.exists()


async def test_list_reads_receipt_during_source_await_refilters_without_losing_raw_cursor(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    events = await seed(service, 2)

    async def read_first(event, ordinal):
        if event["id"] == events[0]["id"] and ordinal == 1:
            await append_read(service, event)

    calls, _ = controlled_authority(service, monkeypatch, hook=read_first)
    first = await service.list(filter="unread", limit=1)
    assert first["entries"] == [] and first["next_after"] == events[0]["id"]
    assert first["counts_consistency"] == "journal_snapshot"
    second = await service.list(filter="unread", limit=1, after=first["next_after"])
    assert [row["id"] for row in second["entries"]] == [events[1]["id"]]
    assert second["entries"][0]["unread"] is True and second["next_after"] is None
    assert {identity for identity, _ in calls} == {row["id"] for row in events}


async def test_list_reads_all_refreshes_notice_read_receipt_without_reading_original_result(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]

    async def read_during_source(value, ordinal):
        if ordinal == 1:
            await append_read(service, value)

    controlled_authority(service, monkeypatch, hook=read_during_source)
    result = await service.list(filter="all")
    row = result["entries"][0]
    assert row["id"] == event["id"] and row["unread"] is False
    assert row["notice_read"]["event_ref"] == {
        "event_id": event["id"],
        "event_digest": record_digest(event),
    }
    assert row["title"].endswith(" 2")
    assert not env.items.executor.jobs.db_path.exists()


@pytest.mark.parametrize("filter", ["all", "unread"])
async def test_list_reads_final_source_revoke_clears_every_readable_field(
    notifications, monkeypatch, filter
):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]
    denied = set()

    async def revoke_on_second(value, ordinal):
        if ordinal == 2:
            denied.add(value["id"])

    calls, current_denied = controlled_authority(service, monkeypatch, hook=revoke_on_second)
    # The hook executes inside the real projection seam; share its denied set.
    denied = current_denied
    result = await service.list(filter=filter)
    assert Counter(identity for identity, _ in calls) == {event["id"]: 2}
    row = result["entries"][0]
    assert row["availability"] == "source_unavailable" and row["unread"] is True
    assert row["title"] is row["actor_name"] is row["jump"] is None
    assert "授权标题" not in json.dumps(result, ensure_ascii=False)


async def test_list_reads_foreign_actor_empty_page_preserves_cursor_and_counts_scope(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    actor = env.engine.personal_agents.create(
        name="Mira", instructions="自己的通知", request_id="list-agent"
    )
    main = (await seed(service, key="main"))[0]
    foreign = (await seed(service, actor=actor.id, key="agent"))[0]
    ordered = sorted([main, foreign], key=lambda value: value["id"])
    selected_actor = ordered[1]["actor_id"]
    calls, _ = controlled_authority(service, monkeypatch)
    first = await service.list(filter="all", actor_id=selected_actor, limit=1)
    assert first["entries"] == [] and first["next_after"] == ordered[0]["id"]
    assert first["unread_count"] == 2 and calls == []
    next_page = await service.list(
        filter="all", actor_id=selected_actor, limit=1, after=first["next_after"]
    )
    assert [row["id"] for row in next_page["entries"]] == [ordered[1]["id"]]
    assert next_page["unread_count"] == 2
    model_page = await service.list(filter="all", caller_actor="main", limit=100)
    assert [row["id"] for row in model_page["entries"]] == [main["id"]]
    assert model_page["unread_count"] == 1
    with pytest.raises(PersonalAgentError):
        await service.list(actor_id=actor.id, caller_actor="main")


async def test_list_reads_raw_keyset_continues_past_bounded_count_window(
    notifications, monkeypatch
):
    from naumi_agent.personal import notifications as notification_module

    env, service = notifications, notifications.notifications
    await configure(env)
    events = await seed(service, 5)
    controlled_authority(service, monkeypatch)
    monkeypatch.setattr(notification_module, "VIEW_LIMIT", 2)
    result = await service.list(filter="all", limit=2, after=events[1]["id"])
    assert result["unread_count"] == 2 and result["partial"] is True
    assert [row["id"] for row in result["entries"]] == [row["id"] for row in events[2:4]]
    assert result["next_after"] == events[3]["id"]


async def test_list_reads_over_thousand_receipts_are_authenticated_without_truncation(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    events = await seed(service, 1002)
    await service._write(
        lambda tx: [
            tx.put("notice_read", _context_fixture_read(service, event), expected_revision=0)
            for event in events[:1001]
        ]
    )
    calls, _ = controlled_authority(service, monkeypatch)
    result = await service.list(filter="all", limit=1, after=events[1000]["id"])
    assert [row["id"] for row in result["entries"]] == [events[1001]["id"]]
    assert result["unread_count"] == 1 and result["partial"] is False
    assert Counter(identity for identity, _ in calls) == {events[1001]["id"]: 2}


async def test_list_reads_source_await_holds_no_journal_transaction_or_writer_lock(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]
    original_connection = service.store.connection
    state = {"active": 0}
    guard = threading.Lock()

    @contextmanager
    def tracked_connection(*args, **kwargs):
        with original_connection(*args, **kwargs) as tx:
            with guard:
                state["active"] += 1
            try:
                yield tx
            finally:
                with guard:
                    state["active"] -= 1

    monkeypatch.setattr(service.store, "connection", tracked_connection)

    async def probe_writer(value, ordinal):
        with guard:
            assert state["active"] == 0, "Source await must not hold its metadata transaction"

        def actual_writer():
            with sqlite3.connect(service.store.db_path, timeout=0.05) as db:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()

        await asyncio.to_thread(actual_writer)

    calls, _ = controlled_authority(service, monkeypatch, hook=probe_writer)
    result = await service.list(filter="all")
    assert result["entries"][0]["id"] == event["id"]
    assert len(calls) == 2 and state["active"] == 0


async def test_list_reads_valid_outbox_progress_is_refreshed_without_revision_conflict(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]
    batches = []

    async def progress(value, ordinal):
        if ordinal == 1:
            batches.append(await service._write(lambda tx: seal(tx, service, value)))

    calls, _ = controlled_authority(service, monkeypatch, hook=progress)
    project = service._project_entry
    captured = []

    async def inspect_detached_metadata(value, caller, metadata):
        assert set(metadata) == {"event", "notice_read", "outbox"}
        json.loads(canonical(metadata))
        captured.append(copy.deepcopy(metadata))
        return await project(value, caller, metadata)

    monkeypatch.setattr(service, "_project_entry", inspect_detached_metadata)
    result = await service.list(filter="all")
    assert result["entries"][0]["batch_id"] == batches[0]["id"]
    assert len(calls) == 2
    current = await service._record("outbox", identity("no_", {"event_id": event["id"]}))
    assert current["revision"] == 2 and current["status"] == "sealed"
    assert [metadata["outbox"]["status"] for metadata in captured] == ["pending", "sealed"]
    assert captured[0]["event"] == captured[1]["event"] == event


@pytest.mark.parametrize("invalid", ["workspace", "event_ref"])
async def test_list_reads_bad_receipt_binding_fails_closed(notifications, monkeypatch, invalid):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]
    receipt = _context_fixture_read(service, event)
    if invalid == "workspace":
        receipt["workspace_key"] = "f" * 64
    else:
        receipt["event_ref"]["event_digest"] = "0" * 64
    await service._write(lambda tx: tx.put("notice_read", receipt, expected_revision=0))
    controlled_authority(service, monkeypatch)
    with pytest.raises(PersonalAgentError) as caught:
        await service.list(filter="all")
    assert caught.value.code in {"storage_error", "not_found", "version_conflict"}


@pytest.mark.parametrize("invalid", ["workspace", "event_id", "actor_id", "kind", "batch"])
async def test_list_reads_bad_outbox_binding_fails_closed(notifications, monkeypatch, invalid):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]

    def corrupt(tx):
        current = tx.get("outbox", identity("no_", {"event_id": event["id"]}))
        changes = {
            "workspace": {"workspace_key": "f" * 64},
            "event_id": {"event_id": "ne_" + "0" * 32},
            "actor_id": {"actor_id": "pa_" + "0" * 32},
            "kind": {"kind": "calendar_approval"},
            "batch": {"batch_id": "nb_" + "0" * 32, "status": "sealed"},
        }[invalid]
        tx.put("outbox", {**current, **changes}, expected_revision=current["revision"])

    await service._write(corrupt)
    controlled_authority(service, monkeypatch)
    with pytest.raises(PersonalAgentError) as caught:
        await service.list(filter="all")
    assert caught.value.code in {"storage_error", "not_found", "version_conflict"}


@pytest.mark.parametrize("invalid", ["foreign_workspace", "wrong_member"])
async def test_list_reads_outbox_batch_must_bind_exact_own_event(
    notifications, monkeypatch, invalid
):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]
    await service._write(
        lambda tx: seal(
            tx,
            service,
            event,
            workspace="f" * 64 if invalid == "foreign_workspace" else None,
            wrong_member=invalid == "wrong_member",
        )
    )
    controlled_authority(service, monkeypatch)
    with pytest.raises(PersonalAgentError) as caught:
        await service.list(filter="all")
    assert caught.value.code in {"storage_error", "not_found", "version_conflict"}


@pytest.mark.parametrize("kind", ["event", "notice_read", "outbox"])
async def test_list_reads_bad_journal_checksum_cannot_be_hidden_by_opaque_counts(
    notifications, monkeypatch, kind
):
    env, service = notifications, notifications.notifications
    await configure(env)
    await seed(service, read=True)
    with sqlite3.connect(service.store.db_path) as db:
        db.execute("UPDATE notification_records SET sha256=? WHERE kind=?", ("0" * 64, kind))
    controlled_authority(service, monkeypatch)
    with pytest.raises(PersonalAgentError) as caught:
        await service.list(filter="all")
    assert caught.value.code == "storage_error"


async def test_list_reads_owner_bound_journal_is_not_a_cross_owner_metadata_cache(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    await seed(service)
    wrong_store = NotificationStore(
        service.store.base_dir,
        owner_key="0" * 64,
        expected_instance_id=service.store.identity(),
    )
    wrong_service = PersonalNotificationService(
        env.engine, store=wrong_store, clock=lambda: env.clock[0]
    )
    controlled_authority(wrong_service, monkeypatch)
    with pytest.raises(PersonalAgentError) as caught:
        await wrong_service.list(filter="all")
    assert caught.value.code == "storage_error"


async def test_list_reads_first_source_failure_never_reuses_an_earlier_page_body(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]
    calls, denied = controlled_authority(service, monkeypatch)
    first = await service.list(filter="all")
    assert first["entries"][0]["availability"] == "available"
    denied.add(event["id"])
    calls.clear()
    second = await service.list(filter="all")
    assert second["entries"][0]["availability"] == "source_unavailable"
    assert second["entries"][0]["title"] is None
    assert "授权标题" not in json.dumps(second, ensure_ascii=False)
    assert len(calls) == 2


async def test_list_reads_model_body_is_real_material_and_next_send_rechecks_source(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]
    calls, denied = controlled_authority(service, monkeypatch)
    result = await service.model_result(
        "list", {"forged": "caller body"}, "main", {"operation": "list", "filter": "all"}
    )
    assert "forged" not in result and result["entries"][0]["id"] == event["id"]
    assert "notice_read" not in result["entries"][0]
    refs = await env.artifacts.session_refs(env.engine._session.id, "main")
    assert len(refs) == 1
    snapshot, content = await env.artifacts.read(
        actor_id="main", artifact_id=refs[0]["artifact_id"], version=refs[0]["version"]
    )
    assert content == canonical(result).encode()
    pointer = snapshot["evidence"]["notification_read_refs"][0]
    read = await service._record("read", pointer["read_id"], revision=1)
    assert read["event_refs"] == [{"event_id": event["id"], "event_digest": record_digest(event)}]
    assert pointer["digest"] == record_digest(read)
    await runtime.authorize_execution(env.engine)
    denied.add(event["id"])
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.engine)
    with pytest.raises(PersonalAgentError):
        await env.artifacts.read(actor_id="main", artifact_id=refs[0]["artifact_id"], version=1)
    assert calls and not env.items.executor.jobs.db_path.exists()


async def test_list_reads_real_calendar_attention_rechecks_expiry_and_count_source(
    context_calendar,
):
    from tests.unit.test_personal_notification_authority import action_proposal

    env, service = context_calendar, context_calendar.notifications
    await env.calendar.prepare_action(action_proposal(env))
    await service.tick()
    value = await service.list(filter="attention")
    assert len(value["entries"]) == 1 and value["attention_count"] == 1
    env.clock[0] += timedelta(hours=1)
    expired = await service.list(filter="attention")
    assert expired["entries"] == [] and expired["attention_count"] == 0
    all_values = await service.list(filter="all")
    assert all_values["entries"][0]["availability"] == "resolved"
    assert env.calendar_transport.calls == [] and not env.items.executor.jobs.db_path.exists()


async def test_list_reads_real_high_source_expiry_on_final_pass_does_not_enter_attention(
    context_calendar, monkeypatch
):
    from tests.unit.test_personal_notification_authority import action_proposal

    env, service = context_calendar, context_calendar.notifications
    await env.calendar.prepare_action(action_proposal(env))
    await service.tick()
    real = service._authorize_event
    original_summary = service._summary
    page_phase, calls = [], []

    async def summary_then_page(caller):
        result = await original_summary(caller)
        page_phase.append(True)
        return result

    async def expire_after_page_first(event, caller):
        result = await real(event, caller)
        if page_phase:
            calls.append(result["availability"])
            if len(calls) == 1:
                env.clock[0] += timedelta(hours=1)
        return result

    monkeypatch.setattr(service, "_summary", summary_then_page)
    monkeypatch.setattr(service, "_authorize_event", expire_after_page_first)
    value = await service.list(filter="attention")
    assert calls == ["available", "resolved"]
    assert value["entries"] == []
    assert value["unread_count"] == 1 and value["attention_count"] == 1
    assert value["counts_consistency"] == "journal_snapshot"


async def test_list_reads_metadata_await_precedes_final_source_check(notifications, monkeypatch):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]
    calls, denied = controlled_authority(service, monkeypatch)
    refresh = service._refresh_entry_metadata

    async def revoke_after_metadata(events):
        result = await refresh(events)
        denied.add(event["id"])
        return result

    monkeypatch.setattr(service, "_refresh_entry_metadata", revoke_after_metadata)
    value = await service.list(filter="all")
    assert len(calls) == 2
    assert value["entries"][0]["availability"] == "source_unavailable"
    assert value["entries"][0]["title"] is value["entries"][0]["jump"] is None


async def test_list_reads_no_journal_await_follows_last_source_authorization(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    await seed(service)
    final = []

    async def remember_final(event, ordinal):
        if ordinal == 2:
            final.append(True)

    controlled_authority(service, monkeypatch, hook=remember_final)
    read = service._read

    async def guarded_read(callback):
        assert not final, "No new local await may follow final source authorization"
        return await read(callback)

    monkeypatch.setattr(service, "_read", guarded_read)
    value = await service.list(filter="all")
    assert final and value["entries"][0]["availability"] == "available"


async def test_list_reads_mid_await_event_corruption_does_not_return_first_body(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = (await seed(service))[0]

    async def corrupt_after_first(value, ordinal):
        if ordinal == 1:
            with sqlite3.connect(service.store.db_path) as db:
                db.execute(
                    "UPDATE notification_records SET sha256=? WHERE kind='event' AND id=?",
                    ("0" * 64, value["id"]),
                )

    controlled_authority(service, monkeypatch, hook=corrupt_after_first)
    with pytest.raises(PersonalAgentError) as caught:
        await service.list(filter="all")
    assert caught.value.code == "storage_error"
    assert event["id"]


async def test_list_reads_mid_await_deleted_read_fact_is_not_resurrected_as_unread(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    await seed(service, read=True)

    async def remove_projection(value, ordinal):
        if ordinal == 1:
            with sqlite3.connect(service.store.db_path) as db:
                db.execute("DELETE FROM notification_records WHERE kind='notice_read'")

    controlled_authority(service, monkeypatch, hook=remove_projection)
    with pytest.raises(PersonalAgentError) as caught:
        await service.list(filter="all")
    assert caught.value.code == "storage_error"


async def test_list_reads_real_http_get_projects_page_without_business_writes(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    events = await seed(service, 3)
    calls, _ = controlled_authority(service, monkeypatch)
    token = "local-notification-reads-test-token"
    env.engine.config.api.api_keys = [token]
    app = FastAPI()
    app.state.engine, app.state.config = env.engine, env.engine.config
    app.include_router(router, prefix="/api/v1")
    before = facts(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local-test"
    ) as client:
        response = await client.get(
            "/api/v1/personal/notifications?filter=all&limit=1", headers={"X-API-Key": token}
        )
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [entry["id"] for entry in body["entries"]] == [events[0]["id"]]
    assert body["counts_consistency"] == "journal_snapshot"
    assert Counter(identity for identity, _ in calls) == {events[0]["id"]: 2}
    assert facts(service) == before
    assert not env.items.executor.jobs.db_path.exists()
