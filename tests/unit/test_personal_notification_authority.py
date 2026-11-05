"""Local durable-source authority: real journals/artifacts, zero external provider calls."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from naumi_agent.daemons.agent_jobs import AgentJobPayload, AgentJobTerminalPayload
from naumi_agent.daemons.agent_worker_contract import issue_agent_worker_result
from naumi_agent.personal import runtime
from naumi_agent.personal.connection_vault import ConnectionVault
from naumi_agent.personal.item_execution import _request
from naumi_agent.personal.item_models import canonical
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_models import record_digest
from tests.unit.test_personal_connection_service import (
    CONNECTION,
    MANUAL,
    FixtureService,
    MemoryBackend,
    seed_connection,
)
from tests.unit.test_personal_notification_service import (
    authority_system as _authority_system,
)
from tests.unit.test_personal_notification_service import (
    configure,
    deliver,
    prompt_claim,
)
from tests.unit.test_personal_notification_service import (
    notifications as _notifications,
)
from tests.unit.test_personal_today_authority import make_item, prepare_intent, revoke, shared_item

authority_system = _authority_system
notifications = _notifications
pytestmark = pytest.mark.usefixtures("runtime_payload_key")
PRIVATE_ACTION_BODY = "NOTIFICATION_ACTION_BODY_MUST_STAY_AT_ORIGINAL_ACTION"
PRIVATE_ERROR = "INFRASTRUCTURE_FAILURE_DETAIL_MUST_NOT_ENTER_NOTIFICATION"


class NoProvider:
    """Tripwire transport; these tests stop at durable approval, not execution."""

    def __init__(self):
        self.calls = []

    async def insert_event(self, *args, **kwargs):
        self.calls.append("insert")
        raise AssertionError("Notification authority tests make zero provider writes")

    async def update_event(self, *args, **kwargs):
        self.calls.append("update")
        raise AssertionError("Notification authority tests make zero provider writes")

    async def get_event(self, *args, **kwargs):
        self.calls.append("get")
        raise AssertionError("Notification authority tests make zero provider reads")


@pytest.fixture
async def notification_calendar(notifications):
    """Compose the production connection mixins and isolated AES fixture vault."""
    env = notifications
    await env.engine.personal_connections.stop()
    backend, adapter = MemoryBackend(), NoProvider()
    service = FixtureService(
        env.engine,
        vault=ConnectionVault(backend),
        adapter=adapter,
        clock=lambda: env.clock[0],
    )
    service._closing = True
    env.engine.personal_connections = service
    await seed_connection(SimpleNamespace(service=service), write=True)
    env.calendar = service
    env.calendar_backend, env.calendar_transport = backend, adapter
    await configure(env)
    yield env
    assert adapter.calls == [], "Local authority evidence is not live Google acceptance"


def action_proposal(env, key="calendar-notice-draft", **extra):
    return {
        "request_id": key,
        "connection_id": CONNECTION,
        "actor_id": "main",
        "source_refs": [],
        "operation": "insert",
        "body": {
            "summary": PRIVATE_ACTION_BODY,
            "description": "Only the original action owns its exact approved body",
            "start": {"dateTime": "2030-06-03T09:00:00+08:00", "timeZone": "Asia/Shanghai"},
            "end": {"dateTime": "2030-06-03T10:00:00+08:00", "timeZone": "Asia/Shanghai"},
        },
        "expires_at": (env.clock[0] + timedelta(hours=1)).isoformat(),
        **extra,
    }


async def calendar_draft(env, **kwargs):
    action = await env.calendar.prepare_action(action_proposal(env, **kwargs))
    assert action["status"] == "draft" and action["approval"] is None
    await env.notifications.tick()
    entry = (await env.notifications.list(filter="all"))["entries"][0]
    assert entry["kind"] == "calendar_approval"
    return action, entry


async def disconnect(env):
    connection = await env.calendar._record("connection", CONNECTION)
    return await env.calendar.disconnect(
        CONNECTION,
        {
            "request_id": "disconnect-notification-source",
            "expected_revision": connection["revision"],
            "remote_revoke": False,
        },
        trusted_channel=MANUAL,
    )


async def pin_list(env, actor="main"):
    return await env.notifications.model_result(
        "list",
        {"untrusted": "caller DTO must not become source evidence"},
        actor,
        {"operation": "list", "filter": "all"},
    )


async def notification_material(env, actor="main"):
    refs = await env.artifacts.session_refs(env.engine._session.id, actor)
    snapshots = [
        await env.artifacts.get(
            actor_id=actor, artifact_id=ref["artifact_id"], version=ref["version"]
        )
        for ref in refs
    ]
    return next(
        snapshot for snapshot in snapshots if snapshot["evidence"].get("notification_read_refs")
    )


async def test_real_calendar_draft_notification_has_exact_source_but_no_action_body_or_credentials(
    notification_calendar,
):
    env = notification_calendar
    action, entry = await calendar_draft(env)
    assert entry["availability"] == "available" and entry["priority"] == "high"
    assert entry["jump"] == {"kind": "calendar_action", "id": action["id"]}
    assert entry["title"] == "日历行动待审批"
    stored = await env.notifications._record("event", entry["id"])
    source = stored["source_ref"]
    assert source["action_id"] == action["id"]
    assert source["action_revision"] == action["revision"]
    assert source["action_digest"] == action["digest"]
    assert (
        source["auth_epoch"] == action["auth_epoch"]
        and source["expires_at"] == action["expires_at"]
    )
    assert source["connection_instance"] == env.calendar._required_identity
    serialized = json.dumps([entry, stored, await env.notifications.context()], ensure_ascii=False)
    for private in (
        PRIVATE_ACTION_BODY,
        "calendar@example.invalid",
        "fixture-access-original",
        "fixture-refresh-never-public",
    ):
        assert private not in serialized
    assert env.calendar_transport.calls == []


async def test_notice_read_does_not_approve_but_actual_human_approval_resolves(
    notification_calendar,
):
    env = notification_calendar
    action, entry = await calendar_draft(env)
    await env.notifications.mark_read(
        {
            "request_id": "read-not-approve",
            "event_refs": [{"event_id": entry["id"], "event_digest": entry["event_digest"]}],
        },
        trusted_channel="web",
    )
    still_draft = await env.calendar.get_action(action["id"])
    assert still_draft["status"] == "draft" and still_draft["approval"] is None
    approved = await env.calendar.approve_action(
        action["id"],
        {
            "request_id": "human-approve",
            "expected_revision": action["revision"],
            "expected_digest": action["digest"],
        },
        trusted_channel=MANUAL,
    )
    assert approved["status"] == "approved" and approved["approval"]["digest"] == action["digest"]
    rows = (await env.notifications.list(filter="all"))["entries"]
    assert rows[0]["id"] == entry["id"] and rows[0]["availability"] == "resolved"
    assert rows[0]["unread"] is False and rows[0]["jump"]["id"] == action["id"]
    assert (await env.notifications.prompt())["batch"] is None
    assert env.calendar_transport.calls == []


async def test_expiry_is_original_instant_and_holds_claim_without_renewing_draft(
    notification_calendar,
):
    env = notification_calendar
    action, entry = await calendar_draft(env)
    batch = (await env.notifications.prompt())["batch"]
    assert batch is not None
    env.clock[0] += timedelta(hours=1)
    expired = (await env.notifications.list(filter="all"))["entries"][0]
    assert expired["id"] == entry["id"] and expired["availability"] == "resolved"
    assert (await env.notifications.prompt())["batch"] is None
    with pytest.raises(PersonalAgentError):
        await env.notifications.claim(
            {
                "request_id": "expired-claim",
                "batch_id": batch["id"],
                "expected_digest": batch["digest"],
                "client_id": "nc_" + "1" * 32,
            },
            trusted_channel="web",
        )
    assert (await env.calendar.get_action(action["id"]))["expires_at"] == action["expires_at"]
    assert (await env.notifications.request_status("expired-claim"))["status"] == "unknown"


async def test_connection_revoke_redacts_every_projection_without_erasing_original_notice(
    notification_calendar,
):
    env = notification_calendar
    action, entry = await calendar_draft(env)
    batch, _, claim = await prompt_claim(env)
    await disconnect(env)
    assert (await env.calendar.get_action(action["id"]))["status"] == "draft"
    redacted = (await env.notifications.list(filter="all"))["entries"][0]
    assert redacted["id"] == entry["id"] and redacted["availability"] == "source_unavailable"
    assert redacted["title"] is redacted["actor_name"] is redacted["jump"] is None
    assert (await env.notifications.get_batch(batch["id"]))["entries"][0] == redacted
    assert (await env.notifications.request_status("claim"))["projection_available"] is False
    with pytest.raises(PersonalAgentError):
        await env.notifications.present(
            {"request_id": "present-after-revoke", **claim["identity"]}, trusted_channel="web"
        )
    saved = await env.notifications.mark_read(
        {"request_id": "read-after-revoke", "event_refs": batch["member_refs"]},
        trusted_channel="web",
    )
    assert saved["accepted"] is True
    read = await env.notifications._record("notice_read", saved["identity"]["notice_read_ids"][0])
    assert read["availability"] == "source_unavailable"
    assert read["event_ref"] == {"event_id": entry["id"], "event_digest": entry["event_digest"]}
    assert env.calendar_transport.calls == []


async def test_revised_calendar_body_creates_new_exact_notice_and_resolves_old_reference(
    notification_calendar,
):
    env = notification_calendar
    action, entry = await calendar_draft(env)
    revised = await env.calendar.revise_action(
        action["id"],
        {
            "request_id": "revise-original-draft",
            "expected_revision": action["revision"],
            "body": {**action["body"], "summary": "A separate revised body"},
            "expires_at": action["expires_at"],
        },
    )
    await env.notifications.tick()
    rows = (await env.notifications.list(filter="all"))["entries"]
    assert len(rows) == 2
    old = next(row for row in rows if row["id"] == entry["id"])
    new = next(row for row in rows if row["id"] != entry["id"])
    assert old["availability"] == "resolved" and new["availability"] == "available"
    old_source = (await env.notifications._record("event", old["id"]))["source_ref"]
    new_source = (await env.notifications._record("event", new["id"]))["source_ref"]
    assert old_source["action_digest"] == action["digest"]
    assert new_source["action_digest"] == revised["digest"] != action["digest"]
    assert new_source["action_revision"] == revised["revision"]


async def test_calendar_notification_pin_is_revoked_on_disconnect_before_next_send(
    notification_calendar,
):
    env = notification_calendar
    await calendar_draft(env)
    body = await pin_list(env)
    assert body["entries"][0]["availability"] == "available"
    snapshot = await notification_material(env)
    await runtime.authorize_execution(env.engine)
    await disconnect(env)
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.engine)
    with pytest.raises(PersonalAgentError):
        await env.artifacts.read(
            actor_id="main", artifact_id=snapshot["artifact_id"], version=snapshot["version"]
        )


async def test_new_connection_grant_never_reanimates_an_old_agent_draft_notification(
    notification_calendar,
):
    env = notification_calendar
    actor = env.engine.personal_agents.create(
        name="Mira", instructions="处理明确分享的日历草稿", request_id="notification-calendar-agent"
    )

    async def grant(key, *, revoke=False):
        current = await env.calendar._record("connection", CONNECTION)
        payload = {
            "request_id": key,
            "expected_revision": current["revision"],
            "actor_id": actor.id,
        }
        if not revoke:
            payload["actor_version"] = actor.version
        return await env.calendar.grant(CONNECTION, payload, trusted_channel=MANUAL, revoke=revoke)

    await grant("original-calendar-grant")
    action = await env.calendar.prepare_action(action_proposal(env, actor_id=actor.id))
    await env.notifications.tick()
    original = (await env.notifications.list(filter="all", actor_id=actor.id))["entries"][0]
    assert original["availability"] == "available"
    assert (await env.notifications.list(filter="all", caller_actor="main"))["entries"] == []
    source = await env.calendar._record("action", action["id"], revision=action["revision"])
    original_grant = source["grant"]

    await grant("revoke-original-calendar-grant", revoke=True)
    revoked = (await env.notifications.list(filter="all", actor_id=actor.id))["entries"][0]
    assert revoked["id"] == original["id"] and revoked["availability"] == "source_unavailable"
    assert revoked["title"] is revoked["actor_name"] is revoked["jump"] is None
    await grant("independent-new-calendar-grant")
    connection = await env.calendar._record("connection", CONNECTION)
    new_grant = connection["grants"][env.calendar.workspace_key + ":" + actor.id]
    assert new_grant["id"] != original_grant["id"]
    assert new_grant["revision"] > original_grant["revision"]
    await env.notifications.tick()
    after = (await env.notifications.list(filter="all", actor_id=actor.id))["entries"]
    assert len(after) == 1 and after[0]["id"] == original["id"]
    assert after[0]["availability"] == "source_unavailable"
    assert after[0]["title"] is after[0]["actor_name"] is after[0]["jump"] is None
    assert (await env.notifications.prompt())["batch"] is None


async def terminal_job(env, status):
    item = await make_item(env, "terminal-" + status)
    intent = await prepare_intent(
        env,
        item,
        "pio_" + {"error": "1", "timeout": "2", "max_turns": "3", "cancelled": "4"}[status] * 32,
    )
    jobs = env.items.executor.jobs
    request = _request(intent)
    admitted = await jobs.admit(request=request, payload=AgentJobPayload(**intent["payload"]))
    claimed = await jobs.claim(admitted.job_id, owner_id="local-terminal-fixture", lease_seconds=30)
    await jobs.mark_running(
        admitted.job_id, owner_id="local-terminal-fixture", claim_epoch=claimed.job.claim_epoch
    )
    result = issue_agent_worker_result(
        request=request,
        status=status,
        response="",
        error=PRIVATE_ERROR,
        total_tokens=0,
        total_cost_usd=0.0,
        turns=0,
        completed_at=env.clock[0].isoformat(),
    )
    await jobs.finish(
        admitted.job_id,
        owner_id="local-terminal-fixture",
        claim_epoch=claimed.job.claim_epoch,
        result=result,
        terminal_payload=AgentJobTerminalPayload(response="", error=PRIVATE_ERROR),
    )
    return item, intent, await jobs.get(admitted.job_id)


@pytest.mark.parametrize("status", ["error", "timeout", "max_turns"])
async def test_authenticated_terminal_failure_without_personal_manifest_or_error_body(
    notifications, status
):
    env = notifications
    await configure(env)
    item, _, job = await terminal_job(env, status)
    assert job.state.value == status
    recovered = await env.items.executor.jobs.recover_terminal_payload(
        job.job_id, expected_result_sha256=job.result.result_sha256
    )
    assert recovered.error == PRIVATE_ERROR and recovered.response == ""
    await env.notifications.tick()
    value = await env.notifications.list(filter="all")
    assert len(value["entries"]) == 1
    entry = value["entries"][0]
    assert entry["kind"] == "item_failure" and entry["availability"] == "available"
    assert entry["jump"] == {"kind": "item", "id": item["item_id"]}
    event = await env.notifications._record("event", entry["id"])
    assert event["source_ref"]["terminal_receipt_id"] == job.latest_receipt.receipt_id
    assert event["source_ref"]["terminal_receipt_sha256"] == job.latest_receipt.receipt_sha256
    assert event["source_ref"]["state"] == status
    assert "run_id" not in event["source_ref"] and "delivery_id" not in event["source_ref"]
    assert PRIVATE_ERROR not in json.dumps([value, event, await env.notifications.prompt()])


async def test_cancelled_job_is_not_relabelled_as_failure_or_result(notifications):
    env = notifications
    await configure(env)
    await terminal_job(env, "cancelled")
    await env.notifications.tick()
    assert (await env.notifications.list(filter="all"))["entries"] == []
    watches = env.notifications.store.read(lambda tx: tx.list("watch"))
    assert len(watches) == 1 and watches[0]["status"] == "done"


async def test_model_return_is_actual_immutable_material_bytes_bound_to_current_session(
    notifications, monkeypatch
):
    env = notifications
    await configure(env)
    _, intent, _ = await deliver(env, monkeypatch)
    await env.notifications.tick()
    body = await pin_list(env)
    assert "untrusted" not in body and len(body["entries"]) == 1
    assert "notice_read" not in body["entries"][0]
    snapshot = await notification_material(env)
    stored, content = await env.artifacts.read(
        actor_id="main", artifact_id=snapshot["artifact_id"], version=snapshot["version"]
    )
    assert stored["kind"] == "material" and content == canonical(body).encode()
    assert snapshot["sha256"] == hashlib.sha256(content).hexdigest()
    pointer = snapshot["evidence"]["notification_read_refs"][0]
    record = await env.notifications._record("read", pointer["read_id"], revision=1)
    assert (
        pointer["digest"] == record_digest(record) and record["body_sha256"] == snapshot["sha256"]
    )
    assert pointer["revision"] == record["revision"] == 1
    assert (
        record["actor_id"] == "main" and record["workspace_key"] == env.notifications.workspace_key
    )
    assert record["event_refs"] == [
        {"event_id": body["entries"][0]["id"], "event_digest": body["entries"][0]["event_digest"]}
    ]
    assert (await env.items.executor.view(intent))["read_receipt"] is None
    assert env.notifications.store.read(lambda tx: tx.list("notice_read")) == []
    await runtime.authorize_execution(env.engine)


async def test_pinned_notification_source_revoke_blocks_next_send_and_derived_material(
    notifications, monkeypatch
):
    env = notifications
    owner, _, grant, item = await shared_item(env)
    await configure(env)
    await deliver(env, monkeypatch, item=item)
    await env.notifications.tick()
    await pin_list(env)
    original = await notification_material(env)
    derived = await env.artifacts.publish(
        actor_id="main",
        request_id="notification-derived",
        title="衍生资料",
        kind="material",
        filename="derived.txt",
        content=b"Derived local source",
        source_refs=[env.artifacts.reference(original)],
    )
    await runtime.authorize_execution(env.engine)
    await revoke(env, owner, grant)
    for snapshot in (original, derived):
        with pytest.raises(PersonalAgentError):
            await env.artifacts.read(
                actor_id="main", artifact_id=snapshot["artifact_id"], version=snapshot["version"]
            )
    with pytest.raises(PersonalAgentError):
        await runtime.authorize_execution(env.engine)
    assert (await env.notifications.list(filter="all"))["entries"][0]["title"] is None


async def test_missing_notification_component_fails_closed_through_complete_ancestry(
    notifications, monkeypatch
):
    env = notifications
    await configure(env)
    await deliver(env, monkeypatch)
    await env.notifications.tick()
    await pin_list(env)
    original = await notification_material(env)
    derived = await env.artifacts.publish(
        actor_id="main",
        request_id="notification-derived",
        title="衍生资料",
        kind="material",
        filename="derived.txt",
        content=b"Derived local source",
        source_refs=[env.artifacts.reference(original)],
    )
    with monkeypatch.context() as change:
        change.setattr(env.engine, "personal_notifications", None)
        for snapshot in (original, derived):
            with pytest.raises(PersonalAgentError) as caught:
                await env.artifacts.get(
                    actor_id="main",
                    artifact_id=snapshot["artifact_id"],
                    version=snapshot["version"],
                )
            assert caught.value.code == "source_unavailable"
        with pytest.raises(PersonalAgentError):
            await runtime.authorize_execution(env.engine)


async def test_session_switch_during_pin_never_returns_body_as_if_bound_to_new_conversation(
    notifications, monkeypatch
):
    env = notifications
    await configure(env)
    await deliver(env, monkeypatch)
    await env.notifications.tick()
    original_session = env.engine._session
    replacement = await env.engine.create_session(
        title="Separate local conversation", model="no-provider"
    )
    bind = env.artifacts.bind_refs

    async def switch_after_bind(session_id, actor, refs):
        result = await bind(session_id, actor, refs)
        env.engine._session = replacement
        return result

    with monkeypatch.context() as change:
        change.setattr(env.artifacts, "bind_refs", switch_after_bind)
        with pytest.raises(PersonalAgentError) as caught:
            await pin_list(env)
        assert caught.value.code == "binding_conflict"
    assert await env.artifacts.session_refs(replacement.id, "main") == []
    assert len(await env.artifacts.session_refs(original_session.id, "main")) == 1


async def test_revocation_between_material_commit_and_binding_still_blocks_return(
    notifications, monkeypatch
):
    env = notifications
    owner, _, grant, item = await shared_item(env)
    await configure(env)
    await deliver(env, monkeypatch, item=item)
    await env.notifications.tick()
    publish = env.artifacts.publish
    published = []

    async def revoke_after_publish(**kwargs):
        snapshot = await publish(**kwargs)
        if kwargs.get("evidence", {}).get("notification_read_refs"):
            published.append(snapshot)
            await revoke(env, owner, grant)
        return snapshot

    with monkeypatch.context() as change:
        change.setattr(env.artifacts, "publish", revoke_after_publish)
        with pytest.raises(PersonalAgentError):
            await pin_list(env)
    assert len(published) == 1
    assert await env.artifacts.session_refs(env.engine._session.id, "main") == []
    with pytest.raises(PersonalAgentError):
        await env.artifacts.get(actor_id="main", artifact_id=published[0]["artifact_id"], version=1)
