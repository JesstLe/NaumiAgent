"""Real local Item/Job/Run/delivery and notification journals; no provider transport."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from naumi_agent.api.routes.notifications import router
from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_models import default_preferences, identity, record_digest
from naumi_agent.personal.notifications import PersonalNotificationService
from tests.unit.test_personal_today_authority import (
    authority_system as _authority_system,
)
from tests.unit.test_personal_today_authority import (
    make_item,
    prepare_intent,
    revoke,
    settle,
    shared_item,
)

authority_system = _authority_system
pytestmark = pytest.mark.usefixtures("runtime_payload_key")
CLIENT = "nc_" + "1" * 32


def preferences(key="preferences", revision=0, **extra):
    return {
        "request_id": key,
        "expected_revision": revision,
        "enabled": True,
        "timezone": "UTC",
        "quiet_hours": [],
        "muted_actor_ids": [],
        "debounce_seconds": 0,
        "max_delay_seconds": 300,
        "bypass_kinds": [],
        "include_existing": True,
        **extra,
    }


@pytest.fixture
async def notifications(authority_system):
    env = authority_system
    env.notifications = PersonalNotificationService(env.engine, clock=lambda: env.clock[0])
    env.engine.personal_notifications = env.notifications
    yield env


async def configure(env, **kwargs):
    return await env.notifications.set_preferences(preferences(**kwargs), trusted_channel="web")


async def deliver(env, monkeypatch, key="notification-item", occurrence=None, item=None):
    async def local_stream(self, **_):
        yield StreamChunk(
            token="本地通知来源报告。",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=9, output_tokens=6, cost_usd=0.001),
        )

    monkeypatch.setattr(ModelRouter, "stream", local_stream)
    item = item or await make_item(env, key)
    occurrence = occurrence or "pio_" + "1" * 32
    intent = await prepare_intent(env, item, occurrence)
    result = await settle(env.items.executor, intent)
    return item, intent, result


async def prompt_claim(env, request="claim"):
    service = env.notifications
    prompt = await service.prompt()
    batch = prompt["batch"]
    assert batch
    payload = {
        "request_id": request,
        "batch_id": batch["id"],
        "expected_digest": batch["digest"],
        "client_id": CLIENT,
    }
    claimed = await service.claim(payload, trusted_channel="web")
    return batch, payload, claimed


async def test_unconfigured_is_lazy_and_models_cannot_configure_or_offer(notifications):
    env, service = notifications, notifications.notifications
    context = await service.context()
    assert context["configured"] is False and context["preferences"]["revision"] == 0
    assert not service.store.db_path.exists()
    await service.tick()
    assert not service.store.db_path.exists()
    for method, payload in (
        (service.set_preferences, preferences()),
        (service.claim, {}),
        (service.present, {}),
        (service.mark_read, {}),
    ):
        with pytest.raises(PersonalAgentError) as exc:
            await method(payload, caller_actor="main", trusted_channel="web")
        assert exc.value.code == "scope_denied"
    with pytest.raises(PersonalAgentError):
        await service.prompt(caller_actor="main")
    assert not env.items.store.db_path.exists()


async def test_real_delivery_merge_claim_present_read_and_original_read_separation(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    item, intent, result = await deliver(env, monkeypatch)
    job = await env.items.executor.jobs.get(result["job_id"])
    run = await env.engine.chat_run_store.get_run(intent["execution_session_id"], result["run_id"])
    await service.tick()
    await service.tick()
    listed = await service.list()
    assert len(listed["entries"]) == listed["unread_count"] == 1
    row = listed["entries"][0]
    assert (
        row["title"] == item["definition"]["title"] and row["jump"]["id"] == intent["occurrence_id"]
    )
    assert "报告。" not in json.dumps(listed, ensure_ascii=False)
    assert len(service.store.read(lambda tx: tx.list("event"))) == 1
    batch, claim_payload, claimed = await prompt_claim(env)
    assert claimed["accepted"] and claimed["identity"]["event_refs"] == batch["member_refs"]
    assert (await service.prompt())["batch"] is None
    assert (await service.claim(claim_payload, trusted_channel="web"))["identity"] == claimed[
        "identity"
    ]
    receipt = await service.request_status("claim")
    assert receipt["identity"] == claimed["identity"] and "batch" not in receipt
    assert "title" not in json.dumps(receipt)
    presentation = {"request_id": "present", **claimed["identity"]}
    presented = await service.present(presentation, trusted_channel="web")
    assert presented["accepted"]
    assert len(service.store.read(lambda tx: tx.list("presentation"))) == 1
    assert (await service.list())["unread_count"] == 1
    read = await service.mark_read(
        {"request_id": "read", "event_refs": batch["member_refs"]}, trusted_channel="web"
    )
    assert read["accepted"] and (await service.list())["unread_count"] == 0
    assert (await env.items.executor.view(intent))["read_receipt"] is None
    assert await env.items.executor.jobs.get(result["job_id"]) == job
    assert (
        await env.engine.chat_run_store.get_run(intent["execution_session_id"], result["run_id"])
        == run
    )
    assert await env.items._item(item["item_id"]) == {
        k: v for k, v in item.items() if k != "occurrence"
    }
    original_read = await env.items.executor.ack(
        intent,
        delivery_id=result["delivery_id"],
        expected_delivery_sha256=result["delivery_sha256"],
        trusted_channel="management_web",
    )
    assert original_read["read_receipt"]


async def test_concurrent_claim_is_one_logical_offer_and_replays_are_exact(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    await deliver(env, monkeypatch)
    await service.tick()
    batch = (await service.prompt())["batch"]
    payload = {
        "request_id": "same-claim",
        "batch_id": batch["id"],
        "expected_digest": batch["digest"],
        "client_id": CLIENT,
    }
    results = await asyncio.gather(
        *(service.claim(payload, trusted_channel="web") for _ in range(5))
    )
    assert len({r["identity"]["claim_id"] for r in results}) == 1
    with pytest.raises(PersonalAgentError) as exc:
        await service.claim({**payload, "client_id": "nc_" + "2" * 32}, trusted_channel="web")
    assert exc.value.code == "idempotency_conflict"
    with pytest.raises(PersonalAgentError):
        await service.claim(
            {**payload, "request_id": "another-client", "client_id": "nc_" + "2" * 32},
            trusted_channel="web",
        )
    assert len(service.store.read(lambda tx: tx.list("outbox", statuses={"claimed"}))) == 1


async def test_cold_reopen_preserves_claim_without_fabricating_presentation(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    await deliver(env, monkeypatch)
    await service.tick()
    _, _, claimed = await prompt_claim(env, "lost-claim-response")
    reopened = PersonalNotificationService(env.engine, clock=lambda: env.clock[0])
    assert (await reopened.request_status("lost-claim-response"))["identity"] == claimed["identity"]
    assert (await reopened.prompt())["batch"] is None
    assert len((await reopened.list())["entries"]) == 1
    assert reopened.store.read(lambda tx: tx.list("presentation")) == []
    assert reopened.store.read(lambda tx: tx.list("notice_read")) == []


async def test_dnd_disabled_and_muting_never_block_inbox_and_mute_beats_bypass(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(
        env,
        quiet_hours=[
            {"weekdays": list(range(7)), "start": "00:00", "end": "00:00", "full_day": True}
        ],
    )
    await deliver(env, monkeypatch)
    await service.tick()
    assert (await service.list())["unread_count"] == 1
    assert (await service.prompt())["batch"] is None
    assert (await service.preferences())["quiet"]["next_release"] is None
    await configure(env, key="disabled", revision=1, enabled=False)
    assert (await service.list())["unread_count"] == 1
    assert (await service.prompt())["batch"] is None
    await configure(
        env,
        key="muted",
        revision=2,
        muted_actor_ids=["main"],
        bypass_kinds=["item_failure", "calendar_approval"],
    )
    assert (await service.prompt())["batch"] is None
    await configure(env, key="unmuted", revision=3)
    assert (await service.prompt())["batch"] is not None


async def test_debounce_max_delay_and_immutable_batch_members(notifications, monkeypatch):
    env, service = notifications, notifications.notifications
    await configure(env, debounce_seconds=60, max_delay_seconds=120)
    await deliver(env, monkeypatch)
    await service.tick()
    assert (await service.prompt())["batch"] is None
    env.clock[0] += timedelta(seconds=61)
    await service.tick()
    original = (await service.prompt())["batch"]
    assert len(original["member_refs"]) == 1
    await deliver(env, monkeypatch, "second", "pio_" + "2" * 32)
    await service.tick()
    assert (await service.get_batch(original["id"]))["member_refs"] == original["member_refs"]
    env.clock[0] += timedelta(seconds=121)
    await service.tick()
    batches = service.store.read(lambda tx: tx.list("batch"))
    assert len(batches) == 2 and all(len(b["member_refs"]) == 1 for b in batches)


async def test_source_revocation_redacts_every_projection_and_read_is_opaque(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    owner, _, grant, item = await shared_item(env)
    await configure(env)
    _, intent, _ = await deliver(env, monkeypatch, item=item)
    await service.tick()
    batch, _, claimed = await prompt_claim(env)
    await revoke(env, owner, grant)
    listed = await service.list()
    entry = listed["entries"][0]
    assert entry["availability"] == "source_unavailable"
    assert entry["title"] is entry["actor_name"] is entry["jump"] is None
    assert (await service.request_status("claim"))["projection_available"] is False
    assert (await service.prompt())["batch"] is None
    with pytest.raises(PersonalAgentError):
        await service.present(
            {"request_id": "late-present", **claimed["identity"]}, trusted_channel="web"
        )
    read = await service.mark_read(
        {"request_id": "unavailable-read", "event_refs": batch["member_refs"]},
        trusted_channel="web",
    )
    saved = await service._record("notice_read", read["identity"]["notice_read_ids"][0])
    assert saved["availability"] == "source_unavailable"
    assert (await env.items.executor.view(intent))["read_receipt"] is None


async def test_cutover_defaults_to_future_and_preferences_cas_replay(notifications, monkeypatch):
    env, service = notifications, notifications.notifications
    await deliver(env, monkeypatch)
    env.clock[0] += timedelta(minutes=2)
    receipt = await configure(env, include_existing=False)
    await service.tick()
    assert (await service.list())["entries"] == []
    assert (await configure(env, include_existing=False))["identity"] == receipt["identity"]
    with pytest.raises(PersonalAgentError) as exc:
        await configure(env, key="stale-prefs")
    assert exc.value.code == "version_conflict"
    await configure(env, key="changed", revision=1, include_existing=True)
    await service.tick()
    assert (await service.list())["entries"] == [], (
        "later toggles must not silently move the original cutover"
    )


async def test_clock_rollback_holds_active_offers_but_preserves_list(notifications, monkeypatch):
    env, service = notifications, notifications.notifications
    await configure(env)
    await deliver(env, monkeypatch)
    await service.tick()
    env.clock[0] -= timedelta(hours=1)
    assert (await service.context())["quiet"]["hold_reason"] == "clock_rollback"
    assert (await service.prompt())["batch"] is None
    assert (await service.list())["unread_count"] == 1
    env.clock[0] += timedelta(hours=2)
    assert (await service.prompt())["batch"]


async def test_external_witness_rejects_new_empty_store_after_db_and_local_witness_delete(
    notifications,
):
    env, service = notifications, notifications.notifications
    await configure(env)
    service.store.db_path.unlink()
    (service.store.base_dir / ".personal-notifications-v1").unlink()
    with pytest.raises(PersonalAgentError) as exc:
        reopened = PersonalNotificationService(env.engine, clock=lambda: env.clock[0])
        await reopened.preferences()
    assert exc.value.code == "storage_error"


async def test_real_http_read_claim_and_unknown_lookup_never_repeat_execution(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    _, intent, result = await deliver(env, monkeypatch)
    await service.tick()
    before = await env.items.executor.jobs.get(result["job_id"])
    app = FastAPI()
    app.state.engine = env.engine
    app.state.api_token = "fixture-notification-token"
    app.state.config = SimpleNamespace(api=SimpleNamespace(api_keys=["fixture-notification-token"]))
    app.include_router(router, prefix="/api/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        headers = {"Authorization": "Bearer fixture-notification-token"}
        context = await client.get("/api/v1/personal/notifications/context", headers=headers)
        assert context.status_code == 200 and context.headers["cache-control"] == "no-store"
        prompt = (
            await client.get("/api/v1/personal/notifications/prompt", headers=headers)
        ).json()["batch"]
        payload = {
            "request_id": "http-lost-response",
            "batch_id": prompt["id"],
            "expected_digest": prompt["digest"],
            "client_id": CLIENT,
        }
        claim = await client.post(
            "/api/v1/personal/notifications/claim", json=payload, headers=headers
        )
        assert claim.status_code == 200
        lookup = await client.get(
            "/api/v1/personal/notifications/requests/http-lost-response", headers=headers
        )
        assert lookup.json()["identity"] == claim.json()["identity"]
        assert "entries" not in lookup.json()
        assert (await client.get("/api/v1/personal/notifications/prompt", headers=headers)).json()[
            "batch"
        ] is None
        rejected = await client.get(
            "/api/v1/personal/notifications?api_key=fixture-notification-token"
        )
        assert rejected.status_code == 401
    assert await env.items.executor.jobs.get(result["job_id"]) == before
    assert (await env.items.executor.view(intent))["read_receipt"] is None


async def test_exact_authenticated_job_lookup_does_not_use_truncated_inbox_page(
    notifications, monkeypatch
):
    env = notifications
    _, intent, result = await deliver(env, monkeypatch)

    async def forbidden_page(*_, **__):
        raise AssertionError("exact delivery must not depend on first100/1000 inbox page")

    monkeypatch.setattr(env.items.executor.jobs, "list_result_inbox_records", forbidden_page)
    view = await env.items.executor.view(intent)
    assert view["delivery_id"] == result["delivery_id"]
    exact = await env.items.executor.jobs.get_result_inbox_record_for_job(
        intent["execution_session_id"], result["job_id"]
    )
    assert exact.delivery.delivery_sha256 == result["delivery_sha256"]
    assert (
        await env.items.executor.jobs.get_result_inbox_record_for_job(
            "other-session", result["job_id"]
        )
        is None
    )


async def test_forged_event_ref_and_incomplete_or_other_client_presentation_are_rejected(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    await deliver(env, monkeypatch)
    await service.tick()
    batch, _, claimed = await prompt_claim(env)
    for payload in (
        {"request_id": "incomplete", **claimed["identity"], "event_refs": []},
        {"request_id": "other", **claimed["identity"], "client_id": "nc_" + "2" * 32},
    ):
        with pytest.raises(PersonalAgentError):
            await service.present(payload, trusted_channel="web")
    bad = copy.deepcopy(batch["member_refs"])
    bad[0]["event_digest"] = "0" * 64
    with pytest.raises(PersonalAgentError):
        await service.mark_read({"request_id": "forged", "event_refs": bad}, trusted_channel="web")
    assert service.store.read(lambda tx: tx.list("notice_read")) == []
    assert service.store.read(lambda tx: tx.list("presentation")) == []


async def test_persistent_cyclic_scan_resets_random_cursor_and_watches_late_completions(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    item = await make_item(env, "late")
    intent = await prepare_intent(env, item, "pio_" + "f" * 32)
    await service.tick()
    watches = service.store.read(lambda tx: tx.list("watch"))
    assert watches[0]["status"] == "pending" and (await service._scan())["intent_after"] is None
    await deliver(env, monkeypatch, item=item, occurrence=intent["occurrence_id"])
    await service.tick()
    await deliver(env, monkeypatch, "smaller", "pio_" + "0" * 32)
    await service.tick()
    assert (await service.list(filter="all"))["unread_count"] == 2
    assert len(service.store.read(lambda tx: tx.list("watch", statuses={"done"}))) == 2


async def test_worker_fence_blocks_competing_service_and_stale_writer(notifications):
    env, service = notifications, notifications.notifications
    await configure(env)
    lease = await service._write(
        lambda tx: tx.claim(
            "worker", service.workspace_key, "other-holder", now=service.now().timestamp(), ttl=90
        )
    )
    with pytest.raises(PersonalAgentError) as exc:
        await service.tick()
    assert exc.value.code == "notification_busy"
    env.clock[0] += timedelta(seconds=91)
    await service.tick()
    with pytest.raises(PersonalAgentError) as lost:
        service.store.write(
            lambda tx: tx.require_lease(
                "worker",
                service.workspace_key,
                "other-holder",
                lease["fence"],
                now=service.now().timestamp(),
            )
        )
    assert lost.value.code == "lease_lost"


async def test_batch_maximum_is_100_and_append_only(notifications):
    service = notifications.notifications
    await configure(notifications)

    # Synthetic load is explicitly confined to grouping, not source acceptance.
    def seed(tx):
        for n in range(101):
            key = identity("ne_", {"fixture": n})
            tx.put(
                "event",
                {
                    "id": key,
                    "workspace_key": service.workspace_key,
                    "actor_id": "main",
                    "kind": "item_result",
                    "source_ref": {},
                    "occurred_at": service.stamp(),
                },
                expected_revision=0,
            )
            tx.put(
                "outbox",
                {
                    "id": identity("no_", {"event_id": key}),
                    "workspace_key": service.workspace_key,
                    "event_id": key,
                    "actor_id": "main",
                    "kind": "item_result",
                    "status": "pending",
                    "first_seen_at": service.stamp(),
                    "batch_id": None,
                },
                expected_revision=0,
            )
        tx.put("scan", awaitless_scan(service), expected_revision=0)
        return tx.claim(
            "worker", service.workspace_key, service.holder, now=service.now().timestamp(), ttl=90
        )

    lease = await service._write(seed)
    await service._seal(await service._prefs(), lease, service.stamp())
    batches = service.store.read(lambda tx: tx.list("batch"))
    assert sorted(len(b["member_refs"]) for b in batches) == [1, 100]
    assert all(
        len({r["event_id"] for r in b["member_refs"]}) == len(b["member_refs"]) for b in batches
    )
    with pytest.raises(PersonalAgentError):
        service.store.write(lambda tx: tx.put("batch", batches[0], expected_revision=1))


def awaitless_scan(service):
    return {
        "id": "scan",
        "workspace_key": service.workspace_key,
        "intent_after": None,
        "watch_after": None,
        "action_after": None,
        "outbox_after": None,
        "max_wall": service.stamp(),
        "last_tick_at": service.stamp(),
        "partial": False,
        "last_error_code": None,
    }


async def test_context_summary_snapshot_marker_is_pinned_for_model(notifications):
    env, service = notifications, notifications.notifications
    await configure(env)
    result = await service.model_result(
        "context",
        {},
        "main",
        {"operation": "context"},
    )
    assert result["counts_consistency"] == "journal_snapshot"
    assert "actors" not in result and "preferences" not in result
    records = service.store.read(lambda tx: tx.list("read"))
    assert len(records) == 1 and records[0]["event_refs"] == []
    refs = await env.artifacts.session_refs(env.engine._session.id, "main")
    assert len(refs) == 1
    artifact = await env.artifacts.get(
        actor_id="main",
        artifact_id=refs[0]["artifact_id"],
        version=refs[0]["version"],
    )
    assert artifact["evidence"]["notification_read_refs"][0]["read_id"] == records[0]["id"]


async def test_partial_read_or_unavailable_batch_does_not_offer_unclaimable_prompt(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env, debounce_seconds=60)
    await deliver(env, monkeypatch)
    await deliver(env, monkeypatch, "batch-second", "pio_" + "2" * 32)
    await service.tick()
    env.clock[0] += timedelta(seconds=61)
    await service.tick()
    batch = (await service.prompt())["batch"]
    assert len(batch["member_refs"]) == 2
    await service.mark_read(
        {"request_id": "partial-read", "event_refs": batch["member_refs"][:1]},
        trusted_channel="web",
    )
    assert not (await service.get_batch(batch["id"]))["prompt_available"]
    assert (await service.prompt())["batch"] is None
    assert (await service.list())["unread_count"] == 1


async def test_claim_rechecks_dnd_after_final_source_await(notifications, monkeypatch):
    env, service = notifications, notifications.notifications
    env.clock[0] = env.clock[0].replace(hour=11, minute=59, second=59)
    await configure(
        env,
        quiet_hours=[
            {"weekdays": list(range(7)), "start": "12:00", "end": "13:00", "full_day": False}
        ],
    )
    await deliver(env, monkeypatch)
    await service.tick()
    batch = (await service.prompt())["batch"]
    original = service.get_batch

    async def cross_boundary(*args, **kwargs):
        value = await original(*args, **kwargs)
        env.clock[0] += timedelta(seconds=2)
        return value

    monkeypatch.setattr(service, "get_batch", cross_boundary)
    with pytest.raises(PersonalAgentError) as exc:
        await service.claim(
            {
                "request_id": "dnd-boundary",
                "batch_id": batch["id"],
                "expected_digest": batch["digest"],
                "client_id": CLIENT,
            },
            trusted_channel="web",
        )
    assert exc.value.code == "notification_unavailable"
    assert (await service.request_status("dnd-boundary"))["status"] == "unknown"


async def test_source_scan_reloads_highwater_after_new_worker_fence(notifications, monkeypatch):
    env, service = notifications, notifications.notifications
    await configure(env)
    await service.tick()
    future = env.clock[0] + timedelta(hours=1)
    original, writes = service._write, []

    async def advanced_after_acquire(callback):
        value = await original(callback)
        if not writes:
            writes.append("advanced")

            def advance(tx):
                old = tx.get("scan", "scan")
                tx.put(
                    "scan",
                    {**old, "max_wall": future.isoformat(timespec="microseconds")},
                    expected_revision=old["revision"],
                )

            await original(advance)
        return value

    monkeypatch.setattr(service, "_write", advanced_after_acquire)
    # The captured old wall does not revive an old fence; the fresh highwater
    # can expire this lease and conservatively stop, but never gets overwritten.
    with pytest.raises(PersonalAgentError) as exc:
        await service.tick()
    assert exc.value.code == "lease_lost"
    assert (await service._scan())["max_wall"] == future.isoformat(timespec="microseconds")


def _context_fixture_event(service, key, *, actor="main", workspace=None, kind="item_result"):
    """An authenticated local opaque ledger fact, deliberately without an execution source."""
    workspace = workspace or service.workspace_key
    source = {"fixture_key": key, "workspace_key": workspace, "actor_id": actor}
    return {
        "id": identity("ne_", {"kind": kind, "source_ref": source}),
        "workspace_key": workspace,
        "actor_id": actor,
        "kind": kind,
        "source_ref": source,
        "occurred_at": service.stamp(),
    }


def _context_fixture_read(service, event, *, workspace=None):
    """Use a real immutable journal receipt without asserting a source was read."""
    return {
        "id": identity("nr_", {"event_id": event["id"]}),
        "workspace_key": workspace or service.workspace_key,
        "event_ref": {"event_id": event["id"], "event_digest": record_digest(event)},
        "channel": "web",
        "read_at": service.stamp(),
        "availability": "source_unavailable",
    }


def _forbid_context_sources(service, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Opaque normal context statistics must not inspect source bodies")

    monkeypatch.setattr(service, "_entries", forbidden)
    monkeypatch.setattr(service, "_entry", forbidden)
    monkeypatch.setattr(service, "_authorize_event", forbidden)
    monkeypatch.setattr(service.engine.personal_today, "_authorize_item", forbidden)
    monkeypatch.setattr(service.items.executor, "view", forbidden)


async def test_context_summary_normal_statistics_never_call_source_or_entry(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)

    def seed(tx):
        first = tx.put(
            "event", _context_fixture_event(service, "opaque-first"), expected_revision=0
        )
        second = tx.put(
            "event", _context_fixture_event(service, "opaque-second"), expected_revision=0
        )
        tx.put("notice_read", _context_fixture_read(service, second), expected_revision=0)
        return first, second

    first, second = await service._write(seed)
    _forbid_context_sources(service, monkeypatch)
    context = await service.context()
    assert context["unread_count"] == 1 and context["attention_count"] == 0
    assert context["counts_consistency"] == "journal_snapshot"
    assert context["partial"] is False
    assert all(key not in context for key in ("entries", "source_ref", "title", "jump"))
    assert first["id"] not in json.dumps(context) and second["id"] not in json.dumps(context)
    assert not env.items.executor.jobs.db_path.exists(), "No new execution is needed for a count"


async def test_context_summary_actor_scope_and_paused_agent_keep_only_opaque_counts(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    first = env.engine.personal_agents.create(
        name="Mira", instructions="处理自己的通知", request_id="context-first-agent"
    )
    second = env.engine.personal_agents.create(
        name="Echo", instructions="处理自己的通知", request_id="context-second-agent"
    )

    def seed(tx):
        for actor in ("main", first.id, second.id):
            event = tx.put(
                "event", _context_fixture_event(service, actor, actor=actor), expected_revision=0
            )
            if actor == second.id:
                tx.put("notice_read", _context_fixture_read(service, event), expected_revision=0)

    await service._write(seed)
    _forbid_context_sources(service, monkeypatch)
    assert (await service.context())["unread_count"] == 2
    assert (await service.context(caller_actor="main"))["unread_count"] == 1
    first_view = await service.context(caller_actor=first.id)
    assert first_view["unread_count"] == 1
    assert [actor["id"] for actor in first_view["actors"]] == [first.id]
    second_view = await service.context(caller_actor=second.id)
    assert second_view["unread_count"] == 0
    assert [actor["id"] for actor in second_view["actors"]] == [second.id]
    env.engine.personal_agents.update(first.id, first.version, status="paused")
    with pytest.raises(PersonalAgentError) as caught:
        await service.context(caller_actor=first.id)
    assert caught.value.code == "agent_inactive"
    human = await service.context()
    assert human["unread_count"] == 2
    assert first.id not in {actor["id"] for actor in human["actors"]}


async def test_context_summary_foreign_workspace_event_is_not_counted(notifications, monkeypatch):
    env, service = notifications, notifications.notifications
    await configure(env)
    await service._write(
        lambda tx: [
            tx.put("event", _context_fixture_event(service, "own"), expected_revision=0),
            tx.put(
                "event",
                _context_fixture_event(service, "foreign", workspace="f" * 64),
                expected_revision=0,
            ),
        ]
    )
    _forbid_context_sources(service, monkeypatch)
    assert (await service.context())["unread_count"] == 1


async def test_context_summary_foreign_workspace_receipt_is_rejected(notifications, monkeypatch):
    env, service = notifications, notifications.notifications
    await configure(env)

    def seed(tx):
        event = tx.put("event", _context_fixture_event(service, "own"), expected_revision=0)
        tx.put(
            "notice_read",
            _context_fixture_read(service, event, workspace="f" * 64),
            expected_revision=0,
        )

    await service._write(seed)
    _forbid_context_sources(service, monkeypatch)
    with pytest.raises(PersonalAgentError) as caught:
        await service.context()
    assert caught.value.code in {"storage_error", "not_found"}


@pytest.mark.parametrize("kind", ["event", "notice_read"])
async def test_context_summary_rejects_bad_journal_record_instead_of_raw_sql_count(
    notifications, monkeypatch, kind
):
    env, service = notifications, notifications.notifications
    await configure(env)

    def seed(tx):
        event = tx.put("event", _context_fixture_event(service, "own"), expected_revision=0)
        tx.put("notice_read", _context_fixture_read(service, event), expected_revision=0)

    await service._write(seed)
    with sqlite3.connect(service.store.db_path) as db:
        db.execute("UPDATE notification_records SET sha256=? WHERE kind=?", ("0" * 64, kind))
    _forbid_context_sources(service, monkeypatch)
    with pytest.raises(PersonalAgentError) as caught:
        await service.context()
    assert caught.value.code == "storage_error"


async def test_context_summary_rejects_forged_parent_projection(notifications, monkeypatch):
    env, service = notifications, notifications.notifications
    await configure(env)
    foreign = await service._write(
        lambda tx: tx.put(
            "event",
            _context_fixture_event(service, "foreign", workspace="f" * 64),
            expected_revision=0,
        )
    )
    with sqlite3.connect(service.store.db_path) as db:
        db.execute(
            "UPDATE notification_records SET parent_id=? WHERE kind='event' AND id=?",
            (service.workspace_key, foreign["id"]),
        )
    _forbid_context_sources(service, monkeypatch)
    with pytest.raises(PersonalAgentError) as caught:
        await service.context()
    assert caught.value.code == "storage_error"


async def test_context_summary_keeps_bounded_partial_count_semantics(notifications, monkeypatch):
    env, service = notifications, notifications.notifications
    await configure(env)
    await service._write(
        lambda tx: [
            tx.put("event", _context_fixture_event(service, str(number)), expected_revision=0)
            for number in range(3)
        ]
    )
    from naumi_agent.personal import notifications as notifications_module

    monkeypatch.setattr(notifications_module, "VIEW_LIMIT", 2)
    _forbid_context_sources(service, monkeypatch)
    value = await service.context()
    assert value["unread_count"] == 2 and value["partial"] is True


async def test_context_summary_receipts_do_not_truncate_at_journal_default_limit(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)

    def seed(tx):
        for number in range(1002):
            event = tx.put(
                "event", _context_fixture_event(service, str(number)), expected_revision=0
            )
            if number < 1001:
                tx.put("notice_read", _context_fixture_read(service, event), expected_revision=0)

    await service._write(seed)
    _forbid_context_sources(service, monkeypatch)
    context = await service.context()
    assert context["unread_count"] == 1 and context["attention_count"] == 0
    assert context["partial"] is False


async def test_context_summary_read_high_priority_is_opaque_without_source_calls(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)

    def seed(tx):
        event = tx.put(
            "event",
            _context_fixture_event(service, "old-high", kind="item_failure"),
            expected_revision=0,
        )
        tx.put("notice_read", _context_fixture_read(service, event), expected_revision=0)

    await service._write(seed)
    _forbid_context_sources(service, monkeypatch)
    value = await service.context()
    assert value["unread_count"] == value["attention_count"] == 0


@pytest.fixture
async def context_calendar(notifications):
    # Reuse actual Calendar mixins and isolated fixture AES vault; no provider or Run.
    from tests.unit.test_personal_notification_authority import notification_calendar

    generator = notification_calendar.__wrapped__(notifications)
    try:
        yield await anext(generator)
    finally:
        await generator.aclose()


async def test_context_summary_high_priority_requires_actual_current_source(context_calendar):
    from tests.unit.test_personal_notification_authority import action_proposal, disconnect

    env, service = context_calendar, context_calendar.notifications
    await env.calendar.prepare_action(action_proposal(env))
    await service.tick()
    active = await service.context()
    assert active["unread_count"] == active["attention_count"] == 1
    env.clock[0] += timedelta(hours=1)
    expired = await service.context()
    assert expired["unread_count"] == 1 and expired["attention_count"] == 0
    await disconnect(env)
    unavailable = await service.context()
    assert unavailable["unread_count"] == 1 and unavailable["attention_count"] == 0
    assert not env.items.executor.jobs.db_path.exists()
    assert env.calendar_transport.calls == []


async def test_context_summary_high_priority_rechecks_after_source_await(
    context_calendar, monkeypatch
):
    from tests.unit.test_personal_notification_authority import action_proposal

    env, service = context_calendar, context_calendar.notifications
    await env.calendar.prepare_action(action_proposal(env))
    await service.tick()
    original, calls = service._entry, []

    async def expire_after_first_projection(*args, **kwargs):
        result = await original(*args, **kwargs)
        calls.append(result["availability"])
        if len(calls) == 1:
            env.clock[0] += timedelta(hours=1)
        return result

    monkeypatch.setattr(service, "_entry", expire_after_first_projection)
    value = await service.context()
    assert calls[:2] == ["available", "resolved"]
    assert value["unread_count"] == 1 and value["attention_count"] == 0


async def test_context_summary_read_receipt_race_is_a_snapshot_not_linearized(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = await service._write(
        lambda tx: tx.put(
            "event", _context_fixture_event(service, "read-race"), expected_revision=0
        )
    )
    _forbid_context_sources(service, monkeypatch)
    summary, committed = service._summary, []

    async def read_commits_after_summary_snapshot(caller):
        result = await summary(caller)
        if not committed:
            await service._write(
                lambda tx: tx.put(
                    "notice_read", _context_fixture_read(service, event), expected_revision=0
                )
            )
            committed.append(True)
        return result

    monkeypatch.setattr(service, "_summary", read_commits_after_summary_snapshot)
    first = await service.context()
    assert committed and first["unread_count"] == 1
    assert first["counts_consistency"] == "journal_snapshot"
    assert (await service._record("notice_read", identity("nr_", {"event_id": event["id"]})))[
        "event_ref"
    ]["event_id"] == event["id"]
    assert (await service.context())["unread_count"] == 0


async def test_request_lookup_scope_hides_foreign_preferences_identity(notifications):
    env, service = notifications, notifications.notifications
    own = await configure(env, key="lookup-scope-own-prefs")
    foreign_workspace, foreign_request = "f" * 64, "lookup-scope-foreign-prefs"

    def seed(tx):
        saved = tx.put("preferences", default_preferences(foreign_workspace), expected_revision=0)
        return service._receipt(
            "set_preferences",
            {"request_id": foreign_request},
            {"preferences_id": saved["id"], "preferences_revision": saved["revision"]},
        )

    foreign = await asyncio.to_thread(
        service.store.mutate,
        foreign_request,
        "set_preferences",
        {
            "workspace_key": foreign_workspace,
            "channel": "web",
            "payload": {"request_id": foreign_request},
        },
        seed,
    )
    persisted = service.store.read(lambda tx: tx.request(foreign_request))
    assert persisted["result"]["identity"] == foreign["identity"]
    assert foreign["identity"]["preferences_id"] != own["identity"]["preferences_id"]
    assert await service.request_status(foreign_request) == {
        "status": "unknown",
        "request_id": foreign_request,
    }
    current = await service.request_status("lookup-scope-own-prefs")
    assert current["status"] == "accepted" and current["identity"] == own["identity"]
    for request in (foreign_request, "lookup-scope-own-prefs"):
        assert await service.request_status(request, caller_actor="main") == {
            "status": "unknown",
            "request_id": request,
        }
    assert not env.items.executor.jobs.db_path.exists()


@pytest.mark.parametrize("operation", ["claim", "mark_read"])
async def test_request_lookup_scope_hides_foreign_batch_and_event_read_receipts(
    notifications, operation
):
    env, service = notifications, notifications.notifications
    await configure(env)
    foreign_workspace = "f" * 64
    request = "lookup-scope-foreign-" + operation

    def seed(tx):
        event = tx.put(
            "event",
            _context_fixture_event(service, request, workspace=foreign_workspace),
            expected_revision=0,
        )
        refs = [{"event_id": event["id"], "event_digest": record_digest(event)}]
        if operation == "claim":
            batch = tx.put(
                "batch",
                {
                    "id": identity(
                        "nb_", {"workspace_key": foreign_workspace, "member_refs": refs}
                    ),
                    "workspace_key": foreign_workspace,
                    "actor_id": "main",
                    "kind": "item_result",
                    "member_refs": refs,
                    "created_at": service.stamp(),
                },
                expected_revision=0,
            )
            value = {
                "claim_id": identity("no_", {"batch_id": batch["id"]}),
                "batch_id": batch["id"],
                "batch_digest": record_digest(batch),
                "client_id": CLIENT,
                "event_refs": refs,
            }
            tx.put(
                "outbox",
                {
                    "id": value["claim_id"],
                    "workspace_key": foreign_workspace,
                    "batch_id": batch["id"],
                    "identity": value,
                    "status": "claimed",
                    "claimed_at": service.stamp(),
                },
                expected_revision=0,
            )
        else:
            read = tx.put(
                "notice_read",
                _context_fixture_read(service, event, workspace=foreign_workspace),
                expected_revision=0,
            )
            value = {"event_refs": refs, "notice_read_ids": [read["id"]]}
        return service._receipt(operation, {"request_id": request}, value)

    receipt = await asyncio.to_thread(
        service.store.mutate,
        request,
        operation,
        {"workspace_key": foreign_workspace, "channel": "web", "payload": {"request_id": request}},
        seed,
    )
    assert service.store.read(lambda tx: tx.request(request))["result"] == receipt
    result = await service.request_status(request)
    assert result == {"status": "unknown", "request_id": request}
    assert "identity" not in result and "operation" not in result
    assert await service.request_status(request, caller_actor="main") == result
    assert not env.items.executor.jobs.db_path.exists()


async def test_request_lookup_scope_preserves_own_unavailable_source_opaque_receipt(
    notifications, monkeypatch
):
    env, service = notifications, notifications.notifications
    await configure(env)
    event = await service._write(
        lambda tx: tx.put(
            "event",
            _context_fixture_event(service, "lookup-own-unavailable"),
            expected_revision=0,
        )
    )

    async def unavailable_source(*args, **kwargs):
        raise PersonalAgentError("source_unavailable", "原来源已失效")

    monkeypatch.setattr(service, "_authorize_event", unavailable_source)
    request = "lookup-scope-own-unavailable-read"
    accepted = await service.mark_read(
        {
            "request_id": request,
            "event_refs": [{"event_id": event["id"], "event_digest": record_digest(event)}],
        },
        trusted_channel="web",
    )
    assert accepted["accepted"] is True
    receipt = await service.request_status(request)
    assert receipt["status"] == "accepted" and receipt["identity"] == accepted["identity"]
    assert all(key not in receipt for key in ("entries", "title", "actor_name", "jump"))
    read = await service._record("notice_read", accepted["identity"]["notice_read_ids"][0])
    assert read["availability"] == "source_unavailable"
    assert await service.request_status(request, caller_actor="main") == {
        "status": "unknown",
        "request_id": request,
    }
    assert not env.items.executor.jobs.db_path.exists()
