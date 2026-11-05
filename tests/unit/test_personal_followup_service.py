"""Real original Today deadlines and fenced rule scans, without executing any work."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from naumi_agent.personal.followup_models import record_digest
from naumi_agent.personal.followups import PersonalFollowupService
from naumi_agent.personal.item_models import canonical
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today_models import item_ref
from tests.unit.test_personal_today_authority import authority_system as _authority_system
from tests.unit.test_personal_today_authority import make_item

authority_system = _authority_system
pytestmark = pytest.mark.usefixtures("runtime_payload_key")


@pytest.fixture
async def followups(authority_system):
    env = authority_system
    env.clock[0] = datetime(2030, 5, 1, 12, tzinfo=UTC)
    env.followups = PersonalFollowupService(env.engine, clock=lambda: env.clock[0])
    env.engine.personal_followups = env.followups
    yield env
    await env.followups.stop()


def rule_body(*, item_ids=(), actor="main", version=0, **extra):
    return {
        "name": "明确截止跟进",
        "actor_id": actor,
        "actor_version": version,
        "status": "active",
        "timezone": "UTC",
        "include_existing": True,
        "max_lateness_minutes": 1440,
        "freshness_seconds": 3600,
        "misfire_policy": "latest",
        "trigger": {"kind": "deadline", "item_ids": list(item_ids), "offsets_minutes": [60, 0]},
        **extra,
    }


async def manual(service, operation, payload):
    return await service.execute_operation(
        {"operation": operation, "payload": payload}, trusted_channel="web"
    )


async def create_rule(env, key="rule", **body):
    accepted = await manual(
        env.followups, "create_rule", {"request_id": key, "rule": rule_body(**body)}
    )
    assert accepted["accepted"] is True and accepted["projection_available"] is True
    return accepted["view"]


async def set_deadline(env, item, *, key="metadata", deadline=None, revision=0, **extra):
    if deadline is None:
        deadline = {
            "kind": "instant",
            "local_time": (env.clock[0] + timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            "timezone": "UTC",
        }
    receipt = await env.today.set_metadata(
        {
            "request_id": key,
            "expected_revision": revision,
            "source_ref": item_ref(item),
            "deadline": deadline,
            "priority": "normal",
            "status": "active",
            **extra,
        },
        trusted_channel="web",
    )
    assert receipt["accepted"] is True
    return await env.today._record("metadata", receipt["identity"]["metadata_ids"][0])


async def due_source(env, key="source", **item_options):
    item = await make_item(env, key, **item_options)
    metadata = await set_deadline(env, item, key="metadata-" + key)
    return item, metadata


async def raw_records(service, kind):
    return await service._read(
        lambda tx: [] if tx is None else tx.list(kind, parent=service.workspace_key, limit=1000)
    )


def execution_counts(env):
    result = {}
    for path in sorted(env.root.rglob("*.db")):
        with sqlite3.connect(path) as db:
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for (table,) in tables:
                if (
                    "job" in table.lower()
                    or table in {"runs", "run_steps", "personal_item_intents"}
                    or table.startswith("personal_item_execution")
                ):
                    quoted = table.replace('"', '""')
                    result[(str(path.relative_to(env.root)), table)] = db.execute(
                        f'SELECT COUNT(*) FROM "{quoted}"'
                    ).fetchone()[0]
    return result


async def test_context_and_empty_reads_do_not_enroll_or_start_any_worker(followups):
    service = followups.followups
    assert not service.store.db_path.exists() and not service.boundary.exists()
    assert (await service.execute_operation({"operation": "context"}))["configured"] is False
    assert await service.execute_operation({"operation": "rules"}) == {"rules": []}
    page = await service.execute_operation({"operation": "list"})
    assert page["proposals"] == [] and page["next_after"] is None
    await service.tick()
    assert not service.store.db_path.exists() and not service.boundary.exists()
    assert service._worker is None and followups.items._worker is None


async def test_rule_is_explicit_manual_persistent_and_main_version_zero(followups):
    env = followups
    rule = await create_rule(env)
    assert rule["body"]["actor_id"] == "main" and rule["body"]["actor_version"] == 0
    assert len(rule["epoch"]) == 32 and rule["revision"] == 1
    assert "actor_digest" not in rule and "timezone_sha256" not in rule
    assert env.followups.enrolled() and env.followups.boundary.is_file()
    reopened = PersonalFollowupService(env.engine, clock=lambda: env.clock[0])
    assert (await reopened.rules())["rules"] == [rule]
    receipt = await reopened.request_status("rule")
    assert receipt["status"] == "accepted" and receipt["identity"]["rule_id"] == rule["id"]
    assert reopened._worker is None


@pytest.mark.parametrize("channel", [None, "model", "background", "WEB"])
async def test_rule_mutation_rejects_untrusted_or_missing_human_channel(followups, channel):
    with pytest.raises(PersonalAgentError):
        await followups.followups.execute_operation(
            {
                "operation": "create_rule",
                "payload": {"request_id": "not-human", "rule": rule_body()},
            },
            trusted_channel=channel,
        )
    assert await followups.followups.rules() == {"rules": []}


async def test_idempotent_rule_receipt_replay_never_creates_a_second_rule(followups):
    env = followups
    original = await create_rule(env)
    repeated = await create_rule(env)
    assert repeated == original and len(await raw_records(env.followups, "rule")) == 1
    with pytest.raises(PersonalAgentError) as failure:
        await create_rule(env, name="different-body-same-request")
    assert failure.value.code == "idempotency_conflict"


async def test_deadline_scan_reads_real_metadata_by_item_parent_not_workspace_parent(followups):
    env = followups
    item, metadata = await due_source(env)
    rule = await create_rule(env, item_ids=[item["item_id"]])
    wrong_parent = await env.today._read(
        lambda tx: tx.page("metadata", parent=env.followups.workspace_key, limit=50)
    )
    assert wrong_parent["records"] == []
    actual = await env.today._read(lambda tx: tx.page("metadata", limit=50))
    assert actual["records"][0]["id"] == metadata["id"]
    await env.followups.tick()
    proposals = (await env.followups.list())["proposals"]
    assert len(proposals) == 1
    value = proposals[0]
    assert value["availability"] == "available" and value["rule"]["id"] == rule["id"]
    assert value["source"]["item_ref"] == item_ref(item)
    assert value["source"]["metadata_ref"] == env.today._metadata_reference(metadata)
    assert value["source"]["deadline"] == metadata["deadline"]
    assert value["reason"]["offset_minutes"] == 60
    assert value["reason"]["nominal_at"] == env.followups.stamp()
    assert item["definition"]["schedule"]["due_at"] not in canonical(value["source"]["deadline"])


async def test_scanning_never_creates_jobs_runs_intents_or_provider_calls(followups, monkeypatch):
    env = followups

    async def forbidden_http(*args, **kwargs):
        raise AssertionError("Deadline scans do not call a provider")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden_http)
    await due_source(env)
    await create_rule(env)
    before = execution_counts(env)
    await env.followups.tick()
    await env.followups.tick()
    assert len(await raw_records(env.followups, "proposal")) == 1
    assert execution_counts(env) == before
    assert env.items._worker is None and env.followups._worker is None
    assert all(not task.get_name().startswith("personal-items") for task in asyncio.all_tasks())


async def test_no_deadline_does_not_fall_back_to_original_execution_due_at(followups):
    env = followups
    item = await make_item(env, "execution-is-not-deadline")
    await env.today.set_metadata(
        {
            "request_id": "explicit-no-deadline",
            "expected_revision": 0,
            "source_ref": item_ref(item),
            "deadline": None,
            "priority": "high",
            "status": "active",
        },
        trusted_channel="web",
    )
    await create_rule(env)
    env.clock[0] = datetime(2035, 1, 1, 9, tzinfo=UTC)
    await env.followups.tick()
    assert not await raw_records(env.followups, "proposal")


@pytest.mark.parametrize("stop", ["checked", "archived", "closed", "cancelled"])
async def test_original_human_check_archive_close_or_cancel_stops_pending_source(followups, stop):
    env = followups
    item, metadata = await due_source(env)
    await create_rule(env)
    await env.followups.tick()
    original = (await env.followups.list())["proposals"][0]
    if stop == "checked":
        await env.today.check(
            {
                "request_id": "human-checked",
                "metadata_id": metadata["id"],
                "expected_revision": metadata["revision"],
                "source_ref": item_ref(item),
                "checked": True,
            },
            trusted_channel="web",
        )
    elif stop == "archived":
        await set_deadline(
            env,
            item,
            key="archive-deadline",
            revision=metadata["revision"],
            deadline={"kind": "instant", "local_time": "2030-05-01T13:00:00", "timezone": "UTC"},
            status="archived",
        )
    else:
        await env.items.control(
            item["item_id"],
            "close" if stop == "closed" else "cancel",
            {"request_id": "stop-original-" + stop, "expected_revision": item["revision"]},
        )
    view = await env.followups.get(original["id"])
    assert view["availability"] in {"source_stale", "source_unavailable"}
    assert all(view[field] is None for field in ("title", "reason", "rule", "source", "target"))
    await env.followups.tick()
    assert len(await raw_records(env.followups, "proposal")) == 1
    current = await env.items.get(item["item_id"])
    assert current["state"] == (stop if stop in {"closed", "cancelled"} else "active")


async def test_dedup_excludes_metadata_priority_revision_rule_name_and_scan_time(followups):
    env = followups
    item, metadata = await due_source(env)
    rule = await create_rule(env)
    await env.followups.tick()
    original = (await env.followups.list())["proposals"][0]
    await set_deadline(
        env,
        item,
        key="priority-only",
        revision=metadata["revision"],
        deadline={"kind": "instant", "local_time": "2030-05-01T13:00:00", "timezone": "UTC"},
        priority="high",
    )
    renamed = await manual(
        env.followups,
        "update_rule",
        {
            "request_id": "rename-only",
            "rule_id": rule["id"],
            "expected_revision": 1,
            "rule": {**rule["body"], "name": "仅改名称"},
        },
    )
    assert renamed["view"]["epoch"] == rule["epoch"]
    env.clock[0] += timedelta(minutes=10)
    for _ in range(3):
        await env.followups.tick()
    records = await raw_records(env.followups, "proposal")
    assert [row["id"] for row in records] == [original["id"]]
    current = await env.followups.get(original["id"])
    assert current["source"]["current_metadata_revision"] == 2
    assert current["source"]["metadata_ref"]["metadata_revision"] == 1


async def test_item_lifecycle_pause_resume_keeps_original_semantic_proposal_identity(followups):
    env = followups
    item, _ = await due_source(env)
    await create_rule(env)
    await env.followups.tick()
    first = (await raw_records(env.followups, "proposal"))[0]
    paused = await env.items.control(
        item["item_id"],
        "pause",
        {"request_id": "pause-source", "expected_revision": item["revision"]},
    )
    await env.items.control(
        item["item_id"],
        "resume",
        {"request_id": "resume-source", "expected_revision": paused["revision"]},
    )
    await env.followups.tick()
    assert [
        (row["id"], row["key_sha256"]) for row in await raw_records(env.followups, "proposal")
    ] == [(first["id"], first["key_sha256"])]


async def test_deadline_change_produces_a_distinct_semantic_key_without_rewriting_old_fact(
    followups,
):
    env = followups
    item, metadata = await due_source(env)
    await create_rule(env)
    await env.followups.tick()
    original = (await raw_records(env.followups, "proposal"))[0]
    await set_deadline(
        env,
        item,
        key="new-real-deadline",
        revision=metadata["revision"],
        deadline={"kind": "instant", "local_time": "2030-05-01T12:30:00", "timezone": "UTC"},
    )
    await env.followups.tick()
    records = await raw_records(env.followups, "proposal")
    assert len(records) == 2 and len({row["key_sha256"] for row in records}) == 2
    assert await env.followups._record("proposal", original["id"]) == original
    assert (await env.followups.get(original["id"]))["availability"] == "source_stale"


async def test_rule_pause_resume_preserves_epoch_and_archive_is_terminal(followups):
    env = followups
    await due_source(env)
    rule = await create_rule(env)
    await env.followups.tick()
    proposal = (await raw_records(env.followups, "proposal"))[0]
    paused = await manual(
        env.followups,
        "set_status",
        {
            "request_id": "pause-rule",
            "rule_id": rule["id"],
            "expected_revision": 1,
            "status": "paused",
        },
    )
    assert paused["view"]["epoch"] == rule["epoch"]
    assert (await env.followups.get(proposal["id"]))["source"] is None
    await env.followups.tick()
    resumed = await manual(
        env.followups,
        "set_status",
        {
            "request_id": "resume-rule",
            "rule_id": rule["id"],
            "expected_revision": 2,
            "status": "active",
        },
    )
    assert resumed["view"]["epoch"] == rule["epoch"]
    await env.followups.tick()
    assert len(await raw_records(env.followups, "proposal")) == 1
    await manual(
        env.followups,
        "set_status",
        {
            "request_id": "archive-rule",
            "rule_id": rule["id"],
            "expected_revision": 3,
            "status": "archived",
        },
    )
    with pytest.raises(PersonalAgentError):
        await manual(
            env.followups,
            "set_status",
            {
                "request_id": "revive-archived",
                "rule_id": rule["id"],
                "expected_revision": 4,
                "status": "active",
            },
        )
    assert (await env.followups.get(proposal["id"]))["title"] is None
    assert (await env.items.get(proposal["source"]["item_ref"]["item_id"]))["state"] == "active"


async def test_semantic_rule_update_rotates_explicit_epoch_and_hides_old_pending_body(followups):
    env = followups
    await due_source(env)
    rule = await create_rule(env)
    await env.followups.tick()
    first = (await raw_records(env.followups, "proposal"))[0]
    changed = copy.deepcopy(rule["body"])
    changed["trigger"]["offsets_minutes"] = [120, 0]
    updated = await manual(
        env.followups,
        "update_rule",
        {
            "request_id": "semantic-rule-change",
            "rule_id": rule["id"],
            "expected_revision": 1,
            "rule": changed,
        },
    )
    assert updated["view"]["epoch"] != rule["epoch"]
    await env.followups.tick()
    assert len(await raw_records(env.followups, "proposal")) == 2
    assert (await env.followups.get(first["id"]))["availability"] == "source_stale"


async def test_rule_cas_conflict_leaves_original_history_and_epoch_intact(followups):
    env = followups
    rule = await create_rule(env)
    await manual(
        env.followups,
        "set_status",
        {
            "request_id": "first-update",
            "rule_id": rule["id"],
            "expected_revision": 1,
            "status": "paused",
        },
    )
    with pytest.raises(PersonalAgentError) as failure:
        await manual(
            env.followups,
            "update_rule",
            {
                "request_id": "stale-rule-update",
                "rule_id": rule["id"],
                "expected_revision": 1,
                "rule": {**rule["body"], "name": "stale"},
            },
        )
    assert failure.value.code == "version_conflict"
    assert (await env.followups._rule(rule["id"]))["revision"] == 2
    assert record_digest(await env.followups._rule(rule["id"], revision=1)) == rule["digest"]


async def test_future_only_rule_does_not_retroactively_generate_past_stage(followups):
    env = followups
    item, _ = await due_source(env)
    env.clock[0] += timedelta(seconds=1)
    await create_rule(env, item_ids=[item["item_id"]], include_existing=False)
    await env.followups.tick()
    assert not await raw_records(env.followups, "proposal")
    env.clock[0] = datetime(2030, 5, 1, 13, tzinfo=UTC)
    await env.followups.tick()
    values = await raw_records(env.followups, "proposal")
    assert len(values) == 1 and values[0]["offset_minutes"] == 0


@pytest.mark.parametrize(
    "policy,minutes,expected", [("latest", 90, 1), ("latest", 1501, 0), ("skip", 2, 0)]
)
async def test_latest_or_skip_misfire_is_bounded_and_never_bursts_all_stages(
    followups, policy, minutes, expected
):
    env = followups
    await due_source(env)
    await create_rule(
        env,
        misfire_policy=policy,
        trigger={"kind": "deadline", "item_ids": [], "offsets_minutes": [120, 60, 0]},
    )
    env.clock[0] += timedelta(minutes=minutes)
    await env.followups.tick()
    values = await raw_records(env.followups, "proposal")
    assert len(values) == expected
    if values:
        assert values[0]["offset_minutes"] == 0


async def test_clock_rollback_hold_survives_service_reopen_and_does_not_emit_new_stage(followups):
    env = followups
    await due_source(env)
    await create_rule(env)
    await env.followups.tick()
    env.clock[0] += timedelta(hours=2)
    await env.followups.tick()
    before = await raw_records(env.followups, "proposal")
    env.clock[0] -= timedelta(hours=1)
    reopened = PersonalFollowupService(env.engine, clock=lambda: env.clock[0])
    await reopened.tick()
    assert (await reopened.context())["worker"]["last_error_code"] == "clock_rollback"
    assert await raw_records(reopened, "proposal") == before


async def test_tzif_change_holds_original_deadline_without_rewriting_it(followups, monkeypatch):
    env = followups
    _, metadata = await due_source(env)
    await create_rule(env)
    import naumi_agent.personal.followup_sources as sources

    original_zone = sources._zone
    monkeypatch.setattr(sources, "_zone", lambda name: (original_zone(name)[0], "f" * 64))
    await env.followups.tick()
    assert not await raw_records(env.followups, "proposal")
    assert (await env.followups.context())["worker"]["last_error_code"] == "timezone_data_changed"
    assert (await env.today._record("metadata", metadata["id"]))["deadline"] == metadata["deadline"]


async def test_date_only_deadline_uses_exclusive_civil_end_not_midnight_start(followups):
    env = followups
    env.clock[0] = datetime(2030, 5, 1, 23, 59, tzinfo=UTC)
    item = await make_item(env, "civil-day")
    metadata = await set_deadline(
        env, item, deadline={"kind": "date", "local_date": "2030-05-01", "timezone": "UTC"}
    )
    await create_rule(env, trigger={"kind": "deadline", "item_ids": [], "offsets_minutes": [1, 0]})
    await env.followups.tick()
    value = (await env.followups.list())["proposals"][0]
    assert datetime.fromisoformat(metadata["deadline"]["boundary_at"]) == datetime(
        2030, 5, 2, tzinfo=UTC
    )
    assert (
        value["reason"]["offset_minutes"] == 1 and value["reason"]["code"] == "deadline_approaching"
    )


@pytest.mark.parametrize("fold,hour", [(0, 5), (1, 6)])
async def test_dst_repeated_deadline_uses_original_explicit_fold(followups, fold, hour):
    env = followups
    env.clock[0] = datetime(2026, 11, 1, hour, 30, tzinfo=UTC)
    item = await make_item(env, "fold-" + str(fold))
    metadata = await set_deadline(
        env,
        item,
        deadline={
            "kind": "instant",
            "local_time": "2026-11-01T01:30:00",
            "timezone": "America/New_York",
            "fold": fold,
        },
    )
    await create_rule(
        env,
        timezone="America/New_York",
        trigger={"kind": "deadline", "item_ids": [], "offsets_minutes": [0]},
    )
    await env.followups.tick()
    value = (await env.followups.list())["proposals"][0]
    assert datetime.fromisoformat(metadata["deadline"]["resolved_at"]) == env.clock[0]
    assert value["source"]["deadline"]["fold"] == fold and value["reason"]["code"] == "deadline_due"


async def test_nonexistent_dst_deadline_is_rejected_by_original_today_before_any_rule(followups):
    env = followups
    item = await make_item(env, "gap")
    with pytest.raises(PersonalAgentError) as failure:
        await set_deadline(
            env,
            item,
            deadline={
                "kind": "instant",
                "local_time": "2026-03-08T02:30:00",
                "timezone": "America/New_York",
            },
        )
    assert failure.value.code == "nonexistent_time"
    assert not env.followups.enrolled()


async def private_source(env, key):
    agent = env.engine.personal_agents.create(
        name=key, instructions="仅处理我的原事项", request_id=key
    )
    opened = await env.engine.personal_agents.open_session(agent.id)
    item, metadata = await due_source(
        env, key, actor=agent.id, version=agent.version, origin=opened["session_id"]
    )
    return agent, item, metadata


async def test_actor_isolation_uses_original_private_item_scope_not_rule_declared_actor(followups):
    env = followups
    first, _, _ = await private_source(env, "first-private")
    second, _, _ = await private_source(env, "second-private")
    first_rule = await create_rule(env, "first-rule", actor=first.id, version=first.version)
    await create_rule(env, "second-rule", actor=second.id, version=second.version)
    await create_rule(env, "main-cannot-scan-private")
    await env.followups.tick()
    values = (await env.followups.list(status="all"))["proposals"]
    assert {row["actor_id"] for row in values} == {first.id, second.id}
    first_values = (await env.followups.list(caller_actor=first.id))["proposals"]
    assert len(first_values) == 1 and first_values[0]["rule"]["id"] == first_rule["id"]
    with pytest.raises(PersonalAgentError) as failure:
        await env.followups.get(first_values[0]["id"], caller_actor=second.id)
    assert failure.value.code == "not_found"
    with pytest.raises(PersonalAgentError):
        await env.followups.list(actor_id=second.id, caller_actor=first.id)


async def test_actor_profile_change_never_silently_rebinds_existing_rule(followups):
    env = followups
    actor, _, _ = await private_source(env, "versioned-agent")
    rule = await create_rule(env, actor=actor.id, version=actor.version)
    await env.followups.tick()
    first = (await raw_records(env.followups, "proposal"))[0]
    updated = env.engine.personal_agents.update(
        actor.id, actor.version, instructions="已明确更改的职责"
    )
    await env.followups.tick()
    assert len(await raw_records(env.followups, "proposal")) == 1
    assert (await env.followups.get(first["id"]))["source"] is None
    with pytest.raises(PersonalAgentError):
        await manual(
            env.followups,
            "set_status",
            {
                "request_id": "resume-old-profile",
                "rule_id": rule["id"],
                "expected_revision": 1,
                "status": "active",
            },
        )
    explicit = {**rule["body"], "actor_version": updated.version}
    rebound = await manual(
        env.followups,
        "update_rule",
        {
            "request_id": "explicit-profile-rebind",
            "rule_id": rule["id"],
            "expected_revision": 1,
            "rule": explicit,
        },
    )
    assert rebound["view"]["epoch"] != rule["epoch"]


async def test_empty_raw_filtered_page_keeps_opaque_cursor_and_reopens_exact_query(followups):
    env = followups
    first, _, _ = await private_source(env, "cursor-first")
    second, _, _ = await private_source(env, "cursor-second")
    await create_rule(env, "cursor-rule-one", actor=first.id, version=first.version)
    await create_rule(env, "cursor-rule-two", actor=second.id, version=second.version)
    await env.followups.tick()
    raw = await raw_records(env.followups, "proposal")
    desired = second.id if raw[0]["actor_id"] == first.id else first.id
    page = await env.followups.list(actor_id=desired, limit=1)
    assert page["proposals"] == [] and page["next_after"].startswith("fc_")
    reopened = PersonalFollowupService(env.engine, clock=lambda: env.clock[0])
    following = await reopened.list(actor_id=desired, limit=1, after=page["next_after"])
    assert len(following["proposals"]) == 1 and following["proposals"][0]["actor_id"] == desired
    for patch in ({"status": "all"}, {"limit": 2}, {"actor_id": raw[0]["actor_id"]}):
        arguments = {"actor_id": desired, "limit": 1, "after": page["next_after"], **patch}
        with pytest.raises(PersonalAgentError) as failure:
            await reopened.list(**arguments)
        assert failure.value.code == "source_stale"


async def test_rule_scan_is_bounded_and_cycles_past_five_rules_without_duplicates(followups):
    env = followups
    await due_source(env)
    created = [await create_rule(env, f"bounded-rule-{number}") for number in range(7)]
    await env.followups.tick()
    assert len(await raw_records(env.followups, "proposal")) == 5
    assert (await env.followups.context())["worker"]["partial"] is True
    await env.followups.tick()
    await env.followups.tick()
    records = await raw_records(env.followups, "proposal")
    assert len(records) == 7
    assert {row["rule_ref"]["id"] for row in records} == {rule["id"] for rule in created}


async def test_metadata_scan_pages_fifty_real_original_records_and_resumes_after_restart(followups):
    env = followups
    for number in range(51):
        await due_source(env, f"bounded-item-{number:03}")
    await create_rule(env)
    await env.followups.tick()
    assert len(await raw_records(env.followups, "proposal")) == 50
    reopened = PersonalFollowupService(env.engine, clock=lambda: env.clock[0])
    await reopened.tick()
    assert len(await raw_records(reopened, "proposal")) == 51
    await reopened.tick()
    assert len(await raw_records(reopened, "proposal")) == 51


def child_fence_script():
    return """
import json,sys,time
from naumi_agent.personal.followup_store import FollowupStore
store=FollowupStore(sys.argv[1],owner_key=sys.argv[2],expected_instance_id=sys.argv[3])
workspace=sys.argv[4]
def replace(tx):
    old=tx.lease('worker',workspace)
    if old and old['phase']!='finished':
        tx.release('worker',workspace,old['holder'],old['fence'],now=time.time())
    return tx.claim('worker',workspace,'separate-process-owner',now=time.time(),ttl=120)
print(json.dumps(store.write(replace)))
"""


async def child_claim(service):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_fence_script(),
        str(service.store.base_dir),
        service.owner_key,
        await service._directory_id(),
        service.workspace_key,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(process.communicate(), timeout=40)
    assert process.returncode == 0, error.decode("utf-8", errors="replace")
    return json.loads(output)


async def test_real_second_process_worker_lease_blocks_competing_scan(followups):
    env = followups
    await due_source(env)
    await create_rule(env)
    lease = await child_claim(env.followups)
    with pytest.raises(PersonalAgentError) as failure:
        await env.followups.tick()
    assert failure.value.code == "followup_busy"
    assert not await raw_records(env.followups, "proposal")
    await env.followups._write(
        lambda tx: tx.release(
            "worker", env.followups.workspace_key, lease["holder"], lease["fence"], now=time.time()
        )
    )
    await env.followups.tick()
    assert len(await raw_records(env.followups, "proposal")) == 1


async def test_real_second_process_replacement_fence_prevents_late_proposal_or_scan_write(
    followups, monkeypatch
):
    env = followups
    await due_source(env)
    await create_rule(env)
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.followups._source_page

    async def paused_source(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(env.followups, "_source_page", paused_source)
    running = asyncio.create_task(env.followups.tick())
    try:
        await asyncio.wait_for(entered.wait(), timeout=15)
        replacement = await child_claim(env.followups)
        assert replacement["fence"] >= 2
    finally:
        release.set()
        result = await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), timeout=20)
    assert result[0] is None or isinstance(result[0], PersonalAgentError)
    assert not await raw_records(env.followups, "proposal")
    assert not await raw_records(env.followups, "scan")
    await env.followups._write(
        lambda tx: tx.release(
            "worker",
            env.followups.workspace_key,
            replacement["holder"],
            replacement["fence"],
            now=time.time(),
        )
    )


async def test_rule_cas_change_during_scan_cannot_publish_stale_proposal(followups, monkeypatch):
    env = followups
    await due_source(env)
    rule = await create_rule(env)
    original = env.followups._source_page
    changed = False

    async def pause_after_source(*args, **kwargs):
        nonlocal changed
        result = await original(*args, **kwargs)
        if not changed:
            changed = True
            await manual(
                env.followups,
                "set_status",
                {
                    "request_id": "pause-during-scan",
                    "rule_id": rule["id"],
                    "expected_revision": rule["revision"],
                    "status": "paused",
                },
            )
        return result

    monkeypatch.setattr(env.followups, "_source_page", pause_after_source)
    await env.followups.tick()
    assert not await raw_records(env.followups, "proposal")
    assert (await env.followups._rule(rule["id"]))["status"] == "paused"


async def test_clock_maxwall_is_rechecked_after_competing_worker_acquisition(
    followups, monkeypatch
):
    env = followups
    await due_source(env)
    await create_rule(env)
    await env.followups.tick()
    baseline = await raw_records(env.followups, "proposal")
    old_write = env.followups._write
    advanced = False

    async def advance_highwater_before_claim(callback):
        nonlocal advanced
        if not advanced:
            advanced = True
            current = await env.followups._record(
                "scan", "workspace:" + env.followups.workspace_key
            )
            await old_write(
                lambda tx: tx.put(
                    "scan",
                    {
                        **current,
                        "max_wall": (env.clock[0] + timedelta(hours=2)).isoformat(
                            timespec="microseconds"
                        ),
                    },
                    expected_revision=current["revision"],
                )
            )
        return await old_write(callback)

    monkeypatch.setattr(env.followups, "_write", advance_highwater_before_claim)
    await env.followups.tick()
    scan = await env.followups._record("scan", "workspace:" + env.followups.workspace_key)
    assert datetime.fromisoformat(scan["max_wall"]) > env.clock[0]
    assert await raw_records(env.followups, "proposal") == baseline
    assert env.followups._last_error == "clock_rollback"


async def test_bad_rule_actor_version_does_not_create_a_business_rule(followups):
    env = followups
    with pytest.raises(PersonalAgentError):
        await create_rule(env, version=1)
    assert await raw_records(env.followups, "rule") == []


async def test_naive_clock_is_rejected_without_creating_an_observation(followups):
    service = followups.followups
    service.clock = lambda: datetime(2030, 5, 1, 12)
    with pytest.raises(PersonalAgentError) as failure:
        await service.context()
    assert failure.value.code == "storage_error"
    assert not service.store.db_path.exists()


async def test_clock_injection_is_local_condition_time_not_lease_authority(followups, monkeypatch):
    env = followups
    await due_source(env)
    await create_rule(env)
    import naumi_agent.personal.followups as module

    lease_clock = time.time()
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: lease_clock))
    await env.followups.tick()
    lease = await env.followups._read(lambda tx: tx.lease("worker", env.followups.workspace_key))
    assert lease["phase"] == "finished"
    assert lease["expires_at"] < env.clock[0].timestamp()
