"""Exact followup bytes, original source revocation and version-five downgrade fences."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import timedelta

import pytest

from naumi_agent.personal import runtime
from naumi_agent.personal.followup_models import normalize_reference
from naumi_agent.personal.followups import PersonalFollowupService
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notifications import PersonalNotificationService
from naumi_agent.personal.today_models import item_ref
from tests.unit.test_personal_today_authority import authority_system as _authority_system
from tests.unit.test_personal_today_authority import make_item, revoke, shared_item

authority_system = _authority_system
pytestmark = pytest.mark.usefixtures("runtime_payload_key")


@pytest.fixture
async def followup_authority(authority_system):
    env = authority_system
    env.engine.personal_followups = env.followups = PersonalFollowupService(
        env.engine, clock=lambda: env.clock[0]
    )
    env.engine.personal_notifications = env.notifications = PersonalNotificationService(
        env.engine, clock=lambda: env.clock[0]
    )
    return env


async def human(service, operation, **fields):
    return await service.execute_operation(
        {"operation": operation, **fields}, trusted_channel="web"
    )


async def seed(env, *, item=None, request="rule"):
    item = item or await make_item(env, request + "-item")
    boundary = (env.clock[0] + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    await env.today.set_metadata(
        {
            "request_id": request + "-deadline",
            "expected_revision": 0,
            "source_ref": item_ref(item),
            "deadline": {"kind": "instant", "local_time": boundary, "timezone": "UTC"},
            "priority": "normal",
        },
        trusted_channel="web",
    )
    body = {
        "name": "明确截止跟进",
        "actor_id": "main",
        "actor_version": 0,
        "status": "active",
        "timezone": "UTC",
        "include_existing": True,
        "max_lateness_minutes": 10080,
        "freshness_seconds": 3600,
        "misfire_policy": "latest",
        "trigger": {"kind": "deadline", "item_ids": [item["item_id"]], "offsets_minutes": [120, 0]},
    }
    rule = await human(env.followups, "create_rule", payload={"request_id": request, "rule": body})
    await env.followups.tick()
    view = await env.followups.list()
    assert len(view["proposals"]) == 1
    return item, rule, view["proposals"][0]


async def pin(env, arguments=None):
    arguments = arguments or {"operation": "list"}
    return await env.followups.model_result(
        arguments["operation"], {"FORGED": "NOT EVIDENCE"}, "main", arguments
    )


async def test_actual_bytes_ignore_dto_and_next_send_checks_original_private_grant(
    followup_authority,
):
    env = followup_authority
    owner, _, grant, item = await shared_item(env)
    await seed(env, item=item)
    body = await pin(env)
    assert body["proposals"][0]["title"] == item["definition"]["title"]
    assert "FORGED" not in json.dumps(body)
    refs = await env.artifacts.session_refs(env.engine._session.id, "main")
    assert len(refs) == 1
    snapshot, content = await env.artifacts.read(
        actor_id="main", artifact_id=refs[0]["artifact_id"], version=1
    )
    assert json.loads(content) == body and snapshot["sha256"] == hashlib.sha256(content).hexdigest()
    assert snapshot["evidence"]["followup_read_refs"]
    await runtime.authorize_execution(env.engine)
    await revoke(env, owner, grant)
    assert (await env.followups.list())["proposals"][0]["title"] is None
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.engine)


@pytest.mark.parametrize("change", ["dismiss", "snooze", "paused", "deadline", "human_check"])
async def test_pending_material_does_not_survive_decision_or_source_change(
    followup_authority, change
):
    env = followup_authority
    item, rule, proposal = await seed(env)
    await pin(env)
    p = {
        "request_id": "change",
        "proposal_ref": proposal["proposal_ref"],
        "expected_decision_revision": proposal["decision"]["revision"],
    }
    if change in {"dismiss", "snooze"}:
        if change == "snooze":
            p["until"] = (env.clock[0] + timedelta(minutes=10)).isoformat()
        await human(env.followups, change, payload=p)
    elif change == "paused":
        await human(
            env.followups,
            "set_status",
            payload={
                "request_id": "pause",
                "rule_id": rule["identity"]["rule_id"],
                "expected_revision": 1,
                "status": "paused",
            },
        )
    elif change == "deadline":
        await env.today.set_metadata(
            {
                "request_id": "new-deadline",
                "expected_revision": 1,
                "source_ref": item_ref(item),
                "deadline": None,
                "priority": "normal",
            },
            trusted_channel="web",
        )
    else:
        metadata = await env.today._metadata_for(item_ref(item))
        await env.today.check(
            {
                "request_id": "original-human-check",
                "metadata_id": metadata["id"],
                "expected_revision": metadata["revision"],
                "source_ref": item_ref(item),
                "checked": True,
            },
            trusted_channel="web",
        )
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.engine)


async def test_missing_component_blocks_direct_derived_and_revision_ancestry(
    followup_authority, monkeypatch
):
    env = followup_authority
    await seed(env)
    await pin(env)
    ref = (await env.artifacts.session_refs(env.engine._session.id, "main"))[0]
    derived = await env.artifacts.publish(
        actor_id="main",
        request_id="derive",
        title="跟进整理",
        kind="material",
        filename="derived.txt",
        content=b"Derived followup evidence",
        source_refs=[ref],
    )
    revised = await env.artifacts.publish(
        actor_id="main",
        request_id="revise",
        title="修订资料",
        kind="material",
        filename="followups.txt",
        content=b"Revised human content",
        source_refs=[],
        evidence={},
        artifact_id=ref["artifact_id"],
        expected_version=1,
    )
    assert any(
        v["artifact_id"] == ref["artifact_id"] and v["version"] == 1 for v in revised["source_refs"]
    )
    with monkeypatch.context() as patch:
        patch.setattr(env.engine, "personal_followups", None)
        for value in (ref, derived, revised):
            with pytest.raises(PersonalAgentError):
                await env.artifacts.get(
                    actor_id="main", artifact_id=value["artifact_id"], version=value["version"]
                )


async def test_old_broker_rejects_followup_material_v5(followup_authority, tmp_path):
    env = followup_authority
    await seed(env)
    await pin(env)
    ref = (await env.artifacts.session_refs(env.engine._session.id, "main"))[0]
    with env.artifacts.store._connection() as db:
        record = json.loads(
            db.execute(
                "SELECT record_json FROM artifact_versions WHERE artifact_id=? AND version=1",
                (ref["artifact_id"],),
            ).fetchone()[0]
        )
    assert record["authority_version"] == 5
    source = subprocess.check_output(
        ["git", "show", "7e0a3063:src/naumi_agent/personal/artifact_store.py"]
    )
    namespace = {"__name__": "followup_old_broker", "__file__": str(tmp_path / "old.py")}
    exec(compile(source, str(tmp_path / "old.py"), "exec"), namespace)
    store = namespace["ArtifactStore"](env.artifacts.store.base_dir)
    with pytest.raises(PersonalAgentError):
        store.get(
            workspace_key=env.followups.workspace_key,
            actor_id="main",
            artifact_id=ref["artifact_id"],
            version=1,
        )


async def test_snooze_creates_one_new_notice_and_dnd_does_not_drop_proposal(followup_authority):
    from tests.unit.test_personal_notification_service import preferences

    env = followup_authority
    _, _, proposal = await seed(env)
    await env.notifications.set_preferences(preferences(enabled=False), trusted_channel="web")
    await env.notifications.tick()
    first = await env.notifications.list(filter="all")
    assert len(first["entries"]) == 1 and first["entries"][0]["kind"] == "followup_proposal"
    assert (await env.notifications.prompt())["batch"] is None
    await human(
        env.followups,
        "snooze",
        payload={
            "request_id": "later",
            "proposal_ref": proposal["proposal_ref"],
            "expected_decision_revision": 1,
            "until": (env.clock[0] + timedelta(minutes=2)).isoformat(),
        },
    )
    env.clock[0] += timedelta(minutes=3)
    await env.followups.tick()
    await env.notifications.tick()
    page = await env.notifications.list(filter="all")
    assert len(page["entries"]) == 2
    assert len({v["id"] for v in page["entries"]}) == 2
    await env.followups.tick()
    await env.notifications.tick()
    assert len((await env.notifications.list(filter="all"))["entries"]) == 2


async def test_notification_model_body_gets_v5_and_resolved_read_contains_no_old_title(
    followup_authority,
):
    from tests.unit.test_personal_notification_service import preferences

    env = followup_authority
    _, _, proposal = await seed(env)
    await env.notifications.set_preferences(preferences(), trusted_channel="web")
    await env.notifications.tick()
    body = await env.notifications.model_result(
        "list", {}, "main", {"operation": "list", "filter": "all"}
    )
    assert body["entries"][0]["title"] == proposal["title"]
    refs = await env.artifacts.session_refs(env.engine._session.id, "main")
    with env.artifacts.store._connection() as db:
        value = json.loads(
            db.execute(
                "SELECT record_json FROM artifact_versions WHERE artifact_id=?",
                (refs[-1]["artifact_id"],),
            ).fetchone()[0]
        )
    assert value["authority_version"] == 5
    await human(
        env.followups,
        "dismiss",
        payload={
            "request_id": "dismiss",
            "proposal_ref": proposal["proposal_ref"],
            "expected_decision_revision": 1,
        },
    )
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.engine)
    env.engine._session = await env.engine.create_session(
        title="Resolved notice", model="no-provider"
    )
    # Notification material created after resolution has no original source body.
    projection = await env.notifications.model_result(
        "list", {}, "main", {"operation": "list", "filter": "all"}
    )
    assert projection["entries"][0]["title"] is None


async def test_rules_model_read_cannot_bind_old_body_to_new_current_reference(
    followup_authority, monkeypatch
):
    env = followup_authority
    _, receipt, _ = await seed(env)
    original_rules = env.followups.rules

    async def rename_after_read(*args, **kwargs):
        result = await original_rules(*args, **kwargs)
        rule = result["rules"][0]
        await human(
            env.followups,
            "update_rule",
            payload={
                "request_id": "concurrent-rule-rename",
                "rule_id": receipt["identity"]["rule_id"],
                "expected_revision": rule["revision"],
                "rule": {**rule["body"], "name": "新名称"},
            },
        )
        return result

    monkeypatch.setattr(env.followups, "rules", rename_after_read)
    with pytest.raises(PersonalAgentError) as failure:
        await pin(env, {"operation": "rules"})
    assert failure.value.code == "source_stale"
    assert not await env.artifacts.session_refs(env.engine._session.id, "main")


async def test_accepted_receipt_survives_race_and_effect_bytes_do_not_launder_material(
    followup_authority, monkeypatch,
):
    env = followup_authority
    _, rule_receipt, proposal = await seed(env)
    original_source = env.followups._decision_source
    committed = None

    async def other_request_commits_before_final_source_check(payload, channel):
        nonlocal committed
        if committed is None:
            committed = False
            committed = await human(env.followups, "accept", payload=payload)
            await human(
                env.followups, "set_status",
                payload={"request_id": "pause-after-other-accept",
                         "rule_id": rule_receipt["identity"]["rule_id"],
                         "expected_revision": 1, "status": "paused"},
            )
        return await original_source(payload, channel)

    monkeypatch.setattr(env.followups, "_decision_source",
                        other_request_commits_before_final_source_check)
    receipt = await human(
        env.followups,
        "accept",
        payload={
            "request_id": "accept-effect-material",
            "proposal_ref": proposal["proposal_ref"],
            "expected_decision_revision": proposal["decision"]["revision"],
            "target": {
                "kind": "today_place",
                "date": env.clock[0].date().isoformat(),
                "timezone": "UTC",
                "expected_day_revision": 0,
                "expected_metadata_revision": proposal["source"]["current_metadata_revision"],
            },
        },
    )
    effect = await env.followups._record("effect", receipt["identity"]["effect_id"])
    assert receipt["identity"] == committed["identity"]
    assert len((await env.followups._page("effect"))["records"]) == 1
    assert effect["phase"] == "materialized"
    artifact_ref = effect["artifact_ref"]
    snapshot, _ = await env.artifacts.read(
        actor_id="main", artifact_id=artifact_ref["artifact_id"], version=artifact_ref["version"]
    )
    derived = await env.artifacts.publish(
        actor_id="main",
        request_id="legitimate-effect-derived",
        title="安排摘要",
        kind="material",
        filename="summary.txt",
        content=b"Human-written derived summary",
        source_refs=[artifact_ref],
    )
    assert await env.artifacts.get(
        actor_id="main", artifact_id=derived["artifact_id"], version=derived["version"]
    )
    # Exercise the original durable store to model a forged origin, not a DTO return.
    forged = await env.artifacts._call(
        "publish", actor_id="main", request_id="forged-effect-body",
        title="伪造的原采纳内容", kind="material", suffix=".txt",
        content=b"Unconfirmed input with a copied genuine effect reference",
        source_refs=[], evidence=snapshot["evidence"],
    )
    with pytest.raises(PersonalAgentError):
        await env.artifacts.get(
            actor_id="main", artifact_id=forged["artifact_id"], version=forged["version"]
        )
    with pytest.raises(PersonalAgentError):
        await env.artifacts.publish(
            actor_id="main", request_id="launder-forged-effect", title="派生内容",
            kind="material", filename="laundered.txt", content=b"Derived forged input",
            source_refs=[env.artifacts.reference(forged)],
        )


@pytest.mark.parametrize(
    "kind,field", [("proposal", "proposal_id"), ("read", "read_id"), ("effect", "effect_id")]
)
@pytest.mark.parametrize("bad", [True, 1.0, 0, 2])
def test_ref_versions_are_strict_and_immutable(kind, field, bad):
    prefix = {"proposal": "fp_", "read": "fl_", "effect": "fe_"}[kind]
    with pytest.raises(PersonalAgentError):
        normalize_reference(
            {
                field: prefix + "1" * 32,
                "revision": bad,
                "digest": "a" * 64,
                "workspace_key": "b" * 64,
            },
            kind,
        )
