"""Real source rules and original Today/Calendar draft adoption, without execution."""

from __future__ import annotations

import asyncio
import copy
import sqlite3
import time
from datetime import timedelta

import pytest

from naumi_agent.personal import runtime
from naumi_agent.personal.followup_effects import FollowupEffectsMixin
from naumi_agent.personal.followup_models import identity, reference
from naumi_agent.personal.followups import PersonalFollowupService
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today import PersonalTodayService
from naumi_agent.personal.today_models import item_ref
from tests.unit.test_personal_calendar_sync import event
from tests.unit.test_personal_connection_service import CONNECTION, MANUAL, seed_connection
from tests.unit.test_personal_connection_service import calendar_endpoint as _calendar_endpoint
from tests.unit.test_personal_connection_service import connection_system as _connection_system
from tests.unit.test_personal_connection_service import signing_material as _signing_material
from tests.unit.test_personal_connection_service import system as _item_system
from tests.unit.test_personal_today_authority import authority_system as _authority_system
from tests.unit.test_personal_today_authority import make_item, revoke, shared_item

authority_system = _authority_system
calendar_endpoint = _calendar_endpoint
connection_system = _connection_system
signing_material = _signing_material
system = _item_system


class EffectsService(PersonalFollowupService, FollowupEffectsMixin):
    """Keep the fixture compatible before and after Root's mixin composition."""


def install(env):
    service = EffectsService(env.engine, clock=lambda: env.clock[0])
    env.engine.personal_followups = service
    env.followups = service
    return service


async def human(env, operation, **fields):
    return await env.followups.execute_operation(
        {"operation": operation, **fields}, trusted_channel="web"
    )


def rule_body(trigger):
    return {
        "name": "明确跟进规则",
        "actor_id": "main",
        "actor_version": 0,
        "status": "active",
        "timezone": "UTC",
        "include_existing": True,
        "max_lateness_minutes": 10080,
        "freshness_seconds": 3600,
        "misfire_policy": "latest",
        "trigger": trigger,
    }


async def deadline(env, item=None):
    service = install(env)
    item = item or await make_item(env)
    await env.today.set_metadata(
        {
            "request_id": "effect-source-deadline",
            "expected_revision": 0,
            "source_ref": item_ref(item),
            "priority": "normal",
            "deadline": {
                "kind": "instant",
                "local_time": (env.clock[0] + timedelta(minutes=30))
                .replace(tzinfo=None)
                .isoformat(),
                "timezone": "UTC",
            },
        },
        trusted_channel="web",
    )
    receipt = await human(
        env,
        "create_rule",
        payload={
            "request_id": "effect-rule",
            "rule": rule_body(
                {"kind": "deadline", "item_ids": [item["item_id"]], "offsets_minutes": [60]}
            ),
        },
    )
    env.rule = await service._rule(receipt["identity"]["rule_id"])
    await service.tick()
    rows = (await service._page("proposal"))["records"]
    assert len(rows) == 1
    env.proposal, env.item = rows[0], item
    return service


@pytest.fixture
async def effect_env(authority_system):
    await deadline(authority_system)
    return authority_system


@pytest.fixture
async def calendar_effect_env(connection_system):
    env = connection_system
    env.today = env.engine.personal_today = PersonalTodayService(
        env.engine, clock=lambda: env.clock[0]
    )
    env.artifacts = env.engine.personal_artifacts
    service = install(env)
    await seed_connection(env, write=False)
    env.endpoint.calendar = lambda _q, _p: (
        200,
        {"items": [event()], "nextSyncToken": "effect-initial"},
    )
    await env.service._run_sync(CONNECTION)
    receipt = await human(
        env,
        "create_rule",
        payload={
            "request_id": "calendar-effect-rule",
            "rule": rule_body(
                {
                    "kind": "calendar_change",
                    "connection_id": CONNECTION,
                    "event_id": None,
                    "change_kinds": ["created", "updated", "cancelled", "unavailable", "restored"],
                    "fields": ["summary", "description", "start", "end", "status", "available"],
                }
            ),
        },
    )
    env.rule = await service._rule(receipt["identity"]["rule_id"])
    await service.tick()
    rows = (await service._page("proposal"))["records"]
    assert len(rows) == 1
    env.proposal = rows[0]
    return env


async def acceptance(env, request_id="effect-accept", *, target=None):
    decision = await env.followups._decision(env.proposal)
    if target is None:
        if env.proposal["effect_kind"] == "today_place":
            day = await env.today.day(env.clock[0].date().isoformat(), "UTC", planner_id="main")
            meta = await env.today._metadata_for(env.proposal["source"]["item_ref"])
            target = {
                "kind": "today_place",
                "date": env.clock[0].date().isoformat(),
                "timezone": "UTC",
                "expected_day_revision": day["day"]["revision"],
                "expected_metadata_revision": meta["revision"],
            }
        else:
            target = {
                "kind": "calendar_draft",
                "body": {
                    "summary": "人工确认的跟进安排",
                    "description": "保留原事件来源，不代表外部批准",
                    "start": {
                        "dateTime": (env.clock[0] + timedelta(hours=2)).isoformat(),
                        "timeZone": "UTC",
                    },
                    "end": {
                        "dateTime": (env.clock[0] + timedelta(hours=3)).isoformat(),
                        "timeZone": "UTC",
                    },
                },
                "expires_at": (env.clock[0] + timedelta(hours=4)).isoformat(),
            }
    return {
        "request_id": request_id,
        "proposal_ref": reference(env.proposal, "proposal"),
        "expected_decision_revision": decision["revision"],
        "target": target,
    }


async def effect_of(env, receipt):
    return await env.followups._record("effect", receipt["identity"]["effect_id"])


async def assert_error(code, awaitable):
    with pytest.raises(PersonalAgentError) as exc:
        await awaitable
    assert exc.value.code == code


async def test_deadline_adopts_original_today_with_exact_lineage_and_no_item_execution(effect_env):
    env = effect_env
    payload = await acceptance(env)
    receipt = await human(env, "accept", payload=payload)
    effect = await effect_of(env, receipt)
    assert effect["phase"] == "materialized", effect
    readback = await env.followups._read_target(effect)
    assert readback["kind"] == "today_place" and readback["item_ref"] == item_ref(env.item)
    day = await env.today._record("day", readback["day_id"])
    assert len(day["placements"]) == 1
    assert day["provenance"][0]["context"]["artifact_refs"] == [effect["artifact_ref"]]
    assert len((await env.items.list())["items"]) == 1
    assert not env.items.executor.jobs.db_path.exists()
    old_revision = day["revision"]
    assert (await human(env, "accept", payload=payload))["identity"] == receipt["identity"]
    assert (await env.today._record("day", day["id"]))["revision"] == old_revision
    material, content = await env.artifacts.read(
        actor_id="main", artifact_id=effect["artifact_ref"]["artifact_id"], version=1
    )
    assert material["evidence"]["followup_effect_refs"][0]["revision"] == 1 and content


async def test_calendar_adopts_real_original_read_connected_insert_draft_only(calendar_effect_env):
    env = calendar_effect_env
    calls = len(env.endpoint.requests)
    receipt = await human(env, "accept", payload=await acceptance(env))
    effect = await effect_of(env, receipt)
    assert effect["phase"] == "materialized", effect
    target = await env.followups._read_target(effect)
    action = await env.service._action_record(target["action_id"])
    assert action["status"] == "draft" and action["operation"] == "insert"
    assert action["approval"] is action["execution"] is action["job_id"] is action["run_id"] is None
    assert action["artifact_refs"] == [effect["artifact_ref"]]
    assert len(env.endpoint.requests) == calls
    await env.service.reject_action(
        action["id"],
        {"request_id": "reject-original-draft", "expected_revision": action["revision"]},
        trusted_channel=MANUAL,
    )
    assert (await env.followups._read_target(effect))["status"] == "rejected"
    assert len(env.endpoint.requests) == calls


async def test_concurrent_identical_acceptance_has_one_effect_and_original_target(effect_env):
    env = effect_env
    payload = await acceptance(env)
    results = await asyncio.gather(
        *(human(env, "accept", payload=copy.deepcopy(payload)) for _ in range(5))
    )
    assert all(r["identity"] == results[0]["identity"] for r in results)
    effects = (await env.followups._page("effect"))["records"]
    assert len(effects) == 1 and effects[0]["phase"] == "materialized"
    target = await env.followups._read_target(effects[0])
    assert len((await env.today._record("day", target["day_id"]))["placements"]) == 1


async def test_conflicting_human_decisions_keep_first_exact_cas(effect_env):
    env = effect_env
    first = await acceptance(env, "first-user-accept")
    second = {**first, "request_id": "second-user-accept"}
    results = await asyncio.gather(
        human(env, "accept", payload=first),
        human(env, "accept", payload=second),
        return_exceptions=True,
    )
    assert len([v for v in results if not isinstance(v, Exception)]) == 1
    assert len((await env.followups._page("effect"))["records"]) == 1


async def test_post_target_commit_exception_reads_original_receipt_without_repeat(
    effect_env, monkeypatch
):
    env = effect_env
    original = env.today.place_from_followup
    calls = 0

    async def fail_after_commit(effect_id):
        nonlocal calls
        calls += 1
        await original(effect_id)
        raise RuntimeError("response lost after committed original Today request")

    monkeypatch.setattr(env.today, "place_from_followup", fail_after_commit)
    receipt = await human(env, "accept", payload=await acceptance(env))
    effect = await effect_of(env, receipt)
    assert calls == 1 and effect["phase"] == "materialized"
    await env.followups._apply_effect(effect["id"])
    assert calls == 1


async def test_unknown_reopens_and_reconciles_same_payload_child_id_without_new_accept(
    effect_env, monkeypatch
):
    env = effect_env
    original = env.today.place_from_followup

    async def unknown(_effect_id):
        raise RuntimeError("unknown local effect response")

    monkeypatch.setattr(env.today, "place_from_followup", unknown)
    receipt = await human(env, "accept", payload=await acceptance(env))
    first = await effect_of(env, receipt)
    assert first["phase"] == "unknown" and first["receipt_missing"]
    monkeypatch.setattr(env.today, "place_from_followup", original)
    env.followups = env.engine.personal_followups = EffectsService(
        env.engine, clock=lambda: env.clock[0]
    )
    recovered = await human(
        env, "reconcile", payload={"request_id": "cold-reconcile", "effect_id": first["id"]}
    )
    final = await effect_of(env, recovered)
    assert final["phase"] == "materialized" and final["target_payload"] == first["target_payload"]
    assert recovered["identity"]["proposal_id"] == env.proposal["id"]
    assert len((await env.followups._page("effect"))["records"]) == 1


async def test_known_missing_cas_conflict_requires_new_exact_human_acceptance(effect_env):
    env = effect_env
    payload = await acceptance(env)
    payload["target"]["expected_day_revision"] = 999
    receipt = await human(env, "accept", payload=payload)
    blocked = await effect_of(env, receipt)
    assert blocked["phase"] == "blocked" and blocked["receipt_missing"]
    assert await env.followups._target_request(blocked) is None
    again = await human(env, "accept", payload=await acceptance(env, "fresh-human-CAS"))
    replacement = await effect_of(env, again)
    assert replacement["id"] != blocked["id"] and replacement["phase"] == "materialized"
    assert (await env.followups._record("effect", blocked["id"]))["phase"] == "blocked"


async def test_occupied_child_request_with_wrong_fingerprint_is_not_a_target_receipt(effect_env):
    env = effect_env
    payload = await acceptance(env)
    effect_id = identity(
        "fe_", {"workspace_key": env.followups.workspace_key, "request_id": payload["request_id"]}
    )
    await env.today.place(
        {
            "request_id": env.followups._child_id(effect_id),
            "date": payload["target"]["date"],
            "timezone": "UTC",
            "planner_id": "main",
            "expected_day_revision": 0,
            "expected_metadata_revision": payload["target"]["expected_metadata_revision"],
            "source_ref": item_ref(env.item),
        },
        trusted_channel="web",
    )
    receipt = await human(env, "accept", payload=payload)
    effect = await effect_of(env, receipt)
    assert effect["phase"] == "unknown" and not effect["receipt_missing"]
    assert effect["last_error_code"] == "idempotency_conflict"
    assert effect["target_result"] is None


async def test_rule_pause_keeps_exact_accepted_effect_material_and_target(effect_env):
    env = effect_env
    receipt = await human(env, "accept", payload=await acceptance(env))
    effect = await effect_of(env, receipt)
    await human(
        env,
        "set_status",
        payload={
            "request_id": "pause-after-accept",
            "rule_id": env.rule["id"],
            "expected_revision": env.rule["revision"],
            "status": "paused",
        },
    )
    assert await env.followups._read_target(effect)
    assert await env.followups._effect_artifact(effect) == effect["artifact_ref"]
    assert (await env.followups._record("effect", effect["id"], revision=1))["phase"] == "pending"


async def test_effect_and_derived_material_still_require_original_source_acl(authority_system):
    env = authority_system
    owner, _, grant, item = await shared_item(env)
    await deadline(env, item)
    receipt = await human(env, "accept", payload=await acceptance(env))
    effect = await effect_of(env, receipt)
    assert effect["phase"] == "materialized", effect
    derived = await env.artifacts.publish(
        actor_id="main",
        request_id="derived-effect",
        title="派生",
        kind="material",
        filename="derived.txt",
        content=b"Derived human arrangement",
        source_refs=[effect["artifact_ref"]],
    )
    await env.artifacts.bind_refs(
        env.engine._session.id, "main", [env.artifacts.reference(derived)]
    )
    await runtime.authorize_execution(env.engine)
    await revoke(env, owner, grant)
    for ref in (effect["artifact_ref"], derived):
        with pytest.raises(PersonalAgentError):
            await env.artifacts.read(actor_id="main", artifact_id=ref["artifact_id"], version=1)
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.engine)


async def test_effect_artifact_content_is_immutable_after_unrelated_metadata_revision(effect_env):
    env = effect_env
    receipt = await human(env, "accept", payload=await acceptance(env))
    effect = await effect_of(env, receipt)
    ref = await env.followups._effect_artifact(effect)
    _, first = await env.artifacts.read(actor_id="main", artifact_id=ref["artifact_id"], version=1)
    metadata = await env.today._metadata_for(item_ref(env.item))
    deadline_value = metadata["deadline"]
    raw_deadline = {key: deadline_value[key] for key in ("kind", "local_time", "timezone", "fold")}
    await env.today.set_metadata(
        {
            "request_id": "only-priority-changed",
            "source_ref": item_ref(env.item),
            "expected_revision": metadata["revision"],
            "priority": "high",
            "deadline": raw_deadline,
        },
        trusted_channel="web",
    )
    assert await env.followups._effect_artifact(effect) == ref
    _, second = await env.artifacts.read(actor_id="main", artifact_id=ref["artifact_id"], version=1)
    assert first == second


async def test_snooze_wakes_once_without_creating_an_effect_or_marking_original_read(effect_env):
    env = effect_env
    initial = await env.followups._decision(env.proposal)
    await human(
        env,
        "snooze",
        payload={
            "request_id": "snooze-source",
            "proposal_ref": reference(env.proposal, "proposal"),
            "expected_decision_revision": initial["revision"],
            "until": (env.clock[0] + timedelta(minutes=2)).isoformat(),
        },
    )
    snoozed = await env.followups._decision(env.proposal)
    assert snoozed["status"] == "snoozed" and snoozed["presentation_epoch"] == 0
    env.clock[0] += timedelta(minutes=3)
    await env.followups.tick()
    awake = await env.followups._decision(env.proposal)
    assert awake["status"] == "pending" and awake["presentation_epoch"] == 1
    await env.followups.tick()
    assert await env.followups._decision(env.proposal) == awake
    assert (await env.followups._page("effect"))["records"] == []
    assert not env.items.executor.jobs.db_path.exists()


async def test_snoozed_source_that_becomes_completed_does_not_revive_body_or_epoch(effect_env):
    env = effect_env
    decision = await env.followups._decision(env.proposal)
    await human(
        env,
        "snooze",
        payload={
            "request_id": "snooze-then-check",
            "proposal_ref": reference(env.proposal, "proposal"),
            "expected_decision_revision": decision["revision"],
            "until": (env.clock[0] + timedelta(minutes=2)).isoformat(),
        },
    )
    metadata = await env.today._metadata_for(item_ref(env.item))
    await env.today.check(
        {
            "request_id": "human-completed",
            "metadata_id": metadata["id"],
            "expected_revision": metadata["revision"],
            "source_ref": item_ref(env.item),
            "checked": True,
        },
        trusted_channel="web",
    )
    env.clock[0] += timedelta(minutes=3)
    await env.followups.tick()
    assert (await env.followups._decision(env.proposal))["presentation_epoch"] == 0
    assert (await env.followups.get(env.proposal["id"]))["source"] is None


@pytest.mark.parametrize("seconds", [30, 8 * 86400])
async def test_snooze_time_bounds_and_dismiss_decision_cas(effect_env, seconds):
    env = effect_env
    decision = await env.followups._decision(env.proposal)
    base = {
        "proposal_ref": reference(env.proposal, "proposal"),
        "expected_decision_revision": decision["revision"],
    }
    await assert_error(
        "invalid_input",
        human(
            env,
            "snooze",
            payload={
                "request_id": "bad-snooze",
                **base,
                "until": (env.clock[0] + timedelta(seconds=seconds)).isoformat(),
            },
        ),
    )
    receipt = await human(env, "dismiss", payload={"request_id": "dismiss-source", **base})
    assert receipt["identity"]["decision_revision"] == decision["revision"] + 1
    await assert_error(
        "version_conflict",
        human(env, "accept", payload={**await acceptance(env), "request_id": "accept-dismissed"}),
    )


async def test_expired_worker_cannot_borrow_new_effect_fence_to_commit(effect_env, monkeypatch):
    env = effect_env
    original = env.today.place_from_followup
    entered, release = asyncio.Event(), asyncio.Event()
    old = {}

    async def delayed_bridge(effect_id):
        old.update(await env.followups.resolve_effect(effect_id, "today_place"))
        entered.set()
        await release.wait()
        with pytest.raises(PersonalAgentError):
            await env.followups.resolve_effect(effect_id, "today_place")
        await asyncio.to_thread(
            env.followups.check_effect_commit, effect_id, old["proof"], old["payload"]
        )
        raise AssertionError("Expired worker must never pass the original target callback")

    monkeypatch.setattr(env.today, "place_from_followup", delayed_bridge)
    payload = await acceptance(env)
    task = asyncio.create_task(human(env, "accept", payload=payload))
    await asyncio.wait_for(entered.wait(), timeout=30)
    effect_id = old["effect_id"]

    def steal(tx):
        current = tx.get("effect", effect_id)
        lease = tx.claim("effect", effect_id, "new-worker", now=time.time() + 121, ttl=120)
        proof = {"holder": "new-worker", "fence": lease["fence"]}
        tx.put("effect", {**current, "apply_lease": proof}, expected_revision=current["revision"])
        return proof

    new_proof = await env.followups._write(steal)
    release.set()
    await task
    effect = await env.followups._record("effect", effect_id)
    assert await env.followups._target_request(effect) is None
    with pytest.raises(PersonalAgentError):
        env.followups.check_effect_commit(effect_id, old["proof"], old["payload"])
    await env.followups._write(
        lambda tx: tx.release(
            "effect", effect_id, new_proof["holder"], new_proof["fence"], now=time.time()
        )
    )
    monkeypatch.setattr(env.today, "place_from_followup", original)
    await human(
        env, "reconcile", payload={"request_id": "recover-new-worker", "effect_id": effect_id}
    )
    assert (await env.followups._record("effect", effect_id))["phase"] == "materialized"


async def test_private_resolver_and_commit_reject_detached_or_changed_payload(
    effect_env, monkeypatch
):
    env = effect_env
    original = env.today.place_from_followup
    observed = {}

    async def verify(effect_id):
        bound = await env.followups.resolve_effect(effect_id, "today_place")
        observed.update(bound)
        assert env.followups.check_effect_commit(effect_id, bound["proof"], bound["payload"])
        wrong = {**bound["payload"], "date": "2031-02-03"}
        with pytest.raises(PersonalAgentError):
            env.followups.check_effect_commit(effect_id, bound["proof"], wrong)
        return await original(effect_id)

    monkeypatch.setattr(env.today, "place_from_followup", verify)
    receipt = await human(env, "accept", payload=await acceptance(env))
    assert (await effect_of(env, receipt))["phase"] == "materialized"
    await assert_error(
        "scope_denied", env.followups.resolve_effect(observed["effect_id"], "today_place")
    )
    with pytest.raises(PersonalAgentError):
        env.followups.check_effect_commit(
            observed["effect_id"], observed["proof"], observed["payload"]
        )


async def test_frozen_human_request_and_effect_history_are_verified(effect_env):
    env = effect_env
    receipt = await human(env, "accept", payload=await acceptance(env))
    effect = await effect_of(env, receipt)
    initial = await env.followups._record("effect", effect["id"], revision=1)
    foreign = reference(initial, "effect") | {"workspace_key": "f" * 64}
    await assert_error("source_stale", env.followups._authorize_effect_ref(foreign, "main"))
    with sqlite3.connect(env.followups.store.db_path) as db:
        db.execute("DROP TRIGGER followup_requests_immutable_update")
        db.execute(
            "UPDATE followup_requests SET fingerprint=? WHERE request_id=?",
            ("0" * 64, initial["accept_request_id"]),
        )
        db.execute(env.followups.store.spec.triggers["followup_requests_immutable_update"])
    await assert_error(
        "storage_error", env.followups._authorize_effect_ref(reference(initial, "effect"), "main")
    )


@pytest.mark.parametrize("change", ["rule_paused", "source_completed"])
async def test_human_can_dismiss_an_opaque_stale_local_proposal(effect_env, change):
    env = effect_env
    if change == "rule_paused":
        await human(
            env,
            "set_status",
            payload={
                "request_id": "pause-before-dismiss",
                "rule_id": env.rule["id"],
                "expected_revision": env.rule["revision"],
                "status": "paused",
            },
        )
    else:
        metadata = await env.today._metadata_for(item_ref(env.item))
        await env.today.check(
            {
                "request_id": "complete-before-dismiss",
                "metadata_id": metadata["id"],
                "expected_revision": metadata["revision"],
                "source_ref": item_ref(env.item),
                "checked": True,
            },
            trusted_channel="web",
        )
    assert (await env.followups.get(env.proposal["id"]))["source"] is None
    decision = await env.followups._decision(env.proposal)
    result = await human(
        env,
        "dismiss",
        payload={
            "request_id": "dismiss-opaque-proposal",
            "proposal_ref": reference(env.proposal, "proposal"),
            "expected_decision_revision": decision["revision"],
        },
    )
    assert (
        result["accepted"]
        and (await env.followups._decision(env.proposal))["status"] == "dismissed"
    )


async def test_legitimate_original_placement_removal_does_not_return_stale_placement(effect_env):
    env = effect_env
    receipt = await human(env, "accept", payload=await acceptance(env))
    effect = await effect_of(env, receipt)
    target = await env.followups._read_target(effect)
    await env.today.remove(
        {
            "request_id": "remove-original-placement",
            "day_id": target["day_id"],
            "expected_revision": target["day_revision"],
            "placement_id": target["placement_id"],
        },
        trusted_channel="web",
    )
    await assert_error("source_stale", env.followups._read_target(effect))
    assert (await env.followups._record("effect", effect["id"]))["phase"] == "materialized"
