"""Read-only effect projection keeps real blob, domain, and last-source authority."""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from naumi_agent.personal import runtime
from naumi_agent.personal.followup_models import reference
from naumi_agent.personal.item_models import canonical
from naumi_agent.personal.models import PersonalAgentError
from tests.unit.test_personal_followup_effects import (
    MANUAL,
    acceptance,
    deadline,
    effect_of,
    human,
)
from tests.unit.test_personal_followup_effects import authority_system as _authority_system
from tests.unit.test_personal_followup_effects import calendar_effect_env as _calendar_effect_env
from tests.unit.test_personal_followup_effects import calendar_endpoint as _calendar_endpoint
from tests.unit.test_personal_followup_effects import connection_system as _connection_system
from tests.unit.test_personal_followup_effects import effect_env as _effect_env
from tests.unit.test_personal_followup_effects import signing_material as _signing_material
from tests.unit.test_personal_followup_effects import system as _item_system
from tests.unit.test_personal_today_authority import PRIVATE, revoke, shared_item

authority_system = _authority_system
calendar_effect_env = _calendar_effect_env
calendar_endpoint = _calendar_endpoint
connection_system = _connection_system
effect_env = _effect_env
signing_material = _signing_material
system = _item_system


async def materialize(env):
    receipt = await human(env, "accept", payload=await acceptance(env))
    effect = await effect_of(env, receipt)
    assert effect["phase"] == "materialized", effect
    return effect


async def forbidden_mutation(*args, **kwargs):
    raise AssertionError("A readback must not ensure, publish, write, or dispatch a target")


async def assert_error(code, awaitable):
    with pytest.raises(PersonalAgentError) as caught:
        await awaitable
    assert caught.value.code == code


async def test_get_reads_exact_existing_blob_with_both_original_checked_passes(
    effect_env, monkeypatch
):
    env = effect_env
    effect = await materialize(env)
    current, initial, proposal = await env.followups._accepted_effect(effect["id"])
    expected = canonical(
        {
            "effect_id": initial["id"],
            "proposal_ref": initial["proposal_ref"],
            "rule_ref": initial["rule_ref"],
            "source": proposal["source"],
            "target": initial["target"],
        }
    ).encode("utf-8")
    artifact_id = current["artifact_ref"]["artifact_id"]
    original_checked, original_call = env.artifacts._checked, env.artifacts._call
    original_read = env.artifacts.read
    events = []
    reading = False

    async def read(**kwargs):
        nonlocal reading
        assert not reading
        reading = True
        try:
            return await original_read(**kwargs)
        finally:
            reading = False

    async def checked(actor, snapshot):
        if reading and snapshot["artifact_id"] == artifact_id:
            events.append("checked")
        return await original_checked(actor, snapshot)

    async def call(method, **kwargs):
        result = await original_call(method, **kwargs)
        if method == "read_content" and kwargs.get("artifact_id") == artifact_id:
            assert result == expected
            events.append("blob")
        return result

    monkeypatch.setattr(env.artifacts, "_checked", checked)
    monkeypatch.setattr(env.artifacts, "_call", call)
    monkeypatch.setattr(env.artifacts, "read", read)
    monkeypatch.setattr(env.artifacts, "publish", forbidden_mutation)
    monkeypatch.setattr(env.followups, "_effect_artifact", forbidden_mutation)
    monkeypatch.setattr(env.followups, "_write", forbidden_mutation)
    monkeypatch.setattr(env.today, "place_from_followup", forbidden_mutation)
    result = await env.followups.get(env.proposal["id"])
    assert result["target"] == effect["target_result"]
    assert result["source"] is not None
    assert events == ["checked", "blob", "checked"]
    assert await env.followups._record("effect", effect["id"]) == effect
    assert not env.items.executor.jobs.db_path.exists()


async def test_missing_artifact_ref_get_is_opaque_and_cannot_repair_or_repeat_target(
    effect_env, monkeypatch
):
    env = effect_env
    effect = await materialize(env)

    def lose_ref(tx):
        current = tx.get("effect", effect["id"])
        return tx.put(
            "effect", {**current, "artifact_ref": None}, expected_revision=current["revision"]
        )

    missing = await env.followups._write(lose_ref)
    original_request = await env.followups._target_request(missing)
    monkeypatch.setattr(env.artifacts, "publish", forbidden_mutation)
    monkeypatch.setattr(env.followups, "_effect_artifact", forbidden_mutation)
    monkeypatch.setattr(env.followups, "_write", forbidden_mutation)
    monkeypatch.setattr(env.today, "place_from_followup", forbidden_mutation)
    await assert_error("source_stale", env.followups._read_target(missing))
    view = await env.followups.get(env.proposal["id"])
    assert view["availability"] == "source_stale"
    assert view["target"] is view["source"] is view["title"] is None
    assert await env.followups._record("effect", effect["id"]) == missing
    assert await env.followups._target_request(missing) == original_request
    assert not env.items.executor.jobs.db_path.exists()


async def test_existing_verifier_checks_actual_bytes_sha_effect_ref_and_full_artifact_ref(
    effect_env, monkeypatch
):
    env = effect_env
    effect = await materialize(env)
    current, initial, proposal = await env.followups._accepted_effect(effect["id"])
    original_read = env.artifacts.read
    cases = ("actual_bytes", "metadata_sha", "effect_revision", "effect_workspace", "grant")
    for case in cases:
        async def altered_read(_case=case, **kwargs):
            snapshot, actual = await original_read(**kwargs)
            snapshot = copy.deepcopy(snapshot)
            if _case == "actual_bytes":
                actual = b"different bytes despite unchanged metadata sha"
            elif _case == "metadata_sha":
                snapshot["sha256"] = "f" * 64
            elif _case == "effect_revision":
                snapshot["evidence"]["followup_effect_refs"][0]["revision"] += 1
            elif _case == "effect_workspace":
                snapshot["evidence"]["followup_effect_refs"][0]["workspace_key"] = "f" * 64
            else:
                snapshot["grant_id"] = "asg_" + "f" * 32
                snapshot["grant_revision"] = 1
            return snapshot, actual

        with monkeypatch.context() as patch:
            patch.setattr(env.artifacts, "read", altered_read)
            patch.setattr(env.artifacts, "publish", forbidden_mutation)
            await assert_error(
                "storage_error",
                env.followups._read_existing_effect_artifact(current, initial, proposal),
            )
    assert await env.followups._read_existing_effect_artifact(
        current, initial, proposal
    ) == effect["artifact_ref"]
    await assert_error(
        "scope_denied",
        env.followups._authorize_effect_ref(
            reference(initial, "effect"), "pa_" + "f" * 32
        ),
    )


async def test_real_missing_or_tampered_blob_is_not_repaired_from_unchanged_metadata(
    effect_env, monkeypatch
):
    env = effect_env
    effect = await materialize(env)
    ref = effect["artifact_ref"]
    snapshot, original = await env.artifacts.read(
        actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
    )
    blob = env.artifacts.store._blob(snapshot["sha256"], snapshot["suffix"])
    monkeypatch.setattr(env.artifacts, "publish", forbidden_mutation)
    monkeypatch.setattr(env.followups, "_effect_artifact", forbidden_mutation)
    for state in ("same_length_tamper", "missing"):
        try:
            if state == "missing":
                blob.unlink()
            else:
                blob.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            await assert_error("storage_error", env.followups._read_target(effect))
            view = await env.followups.get(env.proposal["id"])
            assert view["target"] is view["source"] is view["title"] is None
        finally:
            blob.write_bytes(original)
    restored, actual = await env.artifacts.read(
        actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
    )
    assert restored["sha256"] == snapshot["sha256"] and actual == original
    assert await env.followups._record("effect", effect["id"]) == effect


async def test_only_full_identical_day_snapshots_deduplicate_provenance(
    effect_env, monkeypatch
):
    env = effect_env
    effect = await materialize(env)
    target = effect["target_result"]
    original_sources, original_scope = env.today._authorize_metadata_sources, env.today._day_scope
    original_record = env.today._record
    sources, scoped = [], []

    async def authorize(value, actor):
        if value["id"] == target["day_id"]:
            sources.append(copy.deepcopy(value))
        return await original_sources(value, actor)

    def scope(value, actor=None):
        if value["id"] == target["day_id"]:
            scoped.append(copy.deepcopy(value))
        return original_scope(value, actor)

    monkeypatch.setattr(env.today, "_authorize_metadata_sources", authorize)
    monkeypatch.setattr(env.today, "_day_scope", scope)
    assert await env.followups._read_target(effect) == target
    assert len(sources) == 1 and len(scoped) == 2

    async def changed_current(kind, identity, *, revision=None, missing=False):
        value = await original_record(kind, identity, revision=revision, missing=missing)
        if kind == "day" and identity == target["day_id"] and revision is None:
            value = {**value, "updated_at": (env.clock[0] + timedelta(seconds=1)).isoformat()}
        return value

    sources.clear()
    scoped.clear()
    with monkeypatch.context() as patch:
        patch.setattr(env.today, "_record", changed_current)
        assert await env.followups._read_target(effect) == target
    assert len(sources) == 2 and len(scoped) == 2
    assert sources[0]["revision"] == sources[1]["revision"]
    assert sources[0] != sources[1]

    await env.today.reorder(
        {
            "request_id": "readpath-later-day-revision",
            "day_id": target["day_id"],
            "expected_revision": target["day_revision"],
            "placement_ids": [target["placement_id"]],
        },
        caller_actor="main",
    )
    sources.clear()
    scoped.clear()
    result = await env.followups._read_target(effect)
    assert result["day_revision"] == target["day_revision"] + 1
    assert len(sources) == 2 and len(scoped) == 2
    assert sources[0]["revision"] != sources[1]["revision"]


@pytest.mark.parametrize("operation", ["remove", "move"])
async def test_original_domain_removal_or_move_never_reuses_old_placement(effect_env, operation):
    env = effect_env
    effect = await materialize(env)
    target = effect["target_result"]
    payload = {
        "request_id": "readpath-" + operation,
        "day_id": target["day_id"],
        "expected_revision": target["day_revision"],
        "placement_id": target["placement_id"],
    }
    if operation == "move":
        payload.update(
            target_date=(env.clock[0] + timedelta(days=1)).date().isoformat(),
            target_timezone="UTC",
            expected_target_revision=0,
        )
    await getattr(env.today, operation)(payload, trusted_channel="web")
    await assert_error("source_stale", env.followups._read_target(effect))
    view = await env.followups.get(env.proposal["id"])
    assert view["target"] is view["source"] is view["title"] is None
    assert (await env.followups._record("effect", effect["id"]))["phase"] == "materialized"


@pytest.mark.parametrize(
    "boundary",
    ["before_blob", "after_blob", "before_provenance", "after_provenance", "last_source"],
)
async def test_real_source_revocation_at_read_boundaries_never_returns_a_body(
    authority_system, monkeypatch, boundary
):
    env = authority_system
    owner, _, grant, item = await shared_item(env)
    await deadline(env, item)
    effect = await materialize(env)
    target, ref = effect["target_result"], effect["artifact_ref"]
    derived = None
    if boundary == "after_blob":
        derived = await env.artifacts.publish(
            actor_id="main",
            request_id="readpath-derived",
            title="派生资料",
            kind="material",
            filename="derived.txt",
            content=b"Independently bound derived effect material",
            source_refs=[ref],
        )
        await env.artifacts.bind_refs(
            env.engine._session.id, "main", [env.artifacts.reference(derived)]
        )
        await runtime.authorize_execution(env.engine)
    original_call = env.artifacts._call
    original_sources = env.today._authorize_metadata_sources
    original_item = env.today._authorize_item
    original_effect_authority = env.followups._authorize_effect_ref
    original_checked = env.artifacts._checked
    state = {"revoked": False, "day_done": False, "last_attempted": False, "checked": 0}

    async def withdraw():
        assert not state["revoked"]
        await revoke(env, owner, grant, "readpath-revoke-" + boundary)
        state["revoked"] = True

    async def call(method, **kwargs):
        selected = method == "read_content" and kwargs.get("artifact_id") == ref["artifact_id"]
        if selected and boundary == "before_blob":
            await withdraw()
        result = await original_call(method, **kwargs)
        if selected and boundary == "after_blob":
            await withdraw()
        return result

    async def checked(actor, snapshot):
        if snapshot["artifact_id"] == ref["artifact_id"]:
            state["checked"] += 1
        return await original_checked(actor, snapshot)

    async def authorize_sources(value, actor):
        selected = value["id"] == target["day_id"]
        if selected and boundary == "before_provenance":
            await withdraw()
        result = await original_sources(value, actor)
        if selected:
            state["day_done"] = True
            if boundary == "after_provenance":
                await withdraw()
        return result

    async def authorize_item(*args, **kwargs):
        result = await original_item(*args, **kwargs)
        if boundary == "last_source" and state["day_done"] and not state["revoked"]:
            await withdraw()
        return result

    async def authorize_effect(*args, **kwargs):
        if boundary == "last_source" and state["revoked"]:
            state["last_attempted"] = True
        return await original_effect_authority(*args, **kwargs)

    monkeypatch.setattr(env.artifacts, "_call", call)
    monkeypatch.setattr(env.artifacts, "_checked", checked)
    monkeypatch.setattr(env.today, "_authorize_metadata_sources", authorize_sources)
    monkeypatch.setattr(env.today, "_authorize_item", authorize_item)
    monkeypatch.setattr(env.followups, "_authorize_effect_ref", authorize_effect)
    monkeypatch.setattr(env.followups, "_effect_artifact", forbidden_mutation)
    monkeypatch.setattr(env.today, "place_from_followup", forbidden_mutation)
    view = await env.followups.get(env.proposal["id"])
    assert state["revoked"], state
    assert view["target"] is view["source"] is view["title"] is view["reason"] is None
    assert PRIVATE not in canonical(view)
    if boundary == "after_blob":
        assert state["checked"] == 2
    if boundary == "last_source":
        assert state["last_attempted"]
    assert await env.followups._record("effect", effect["id"]) == effect
    assert not env.items.executor.jobs.db_path.exists()
    if derived is not None:
        with pytest.raises(PersonalAgentError):
            await env.artifacts.read(
                actor_id="main", artifact_id=derived["artifact_id"], version=derived["version"]
            )
        with pytest.raises(PersonalAgentError):
            await runtime.authorize_execution(env.engine)


async def test_calendar_readback_is_read_only_and_keeps_original_rejected_status(
    calendar_effect_env, monkeypatch
):
    env = calendar_effect_env
    effect = await materialize(env)
    action = await env.service._action_record(effect["target_result"]["action_id"])
    await env.service.reject_action(
        action["id"],
        {"request_id": "readpath-reject-original", "expected_revision": action["revision"]},
        trusted_channel=MANUAL,
    )
    original_requests = len(env.endpoint.requests)
    monkeypatch.setattr(env.artifacts, "publish", forbidden_mutation)
    monkeypatch.setattr(env.followups, "_effect_artifact", forbidden_mutation)
    monkeypatch.setattr(env.followups, "_write", forbidden_mutation)
    monkeypatch.setattr(env.service, "prepare_action_from_followup", forbidden_mutation)
    view = await env.followups.get(env.proposal["id"])
    assert view["target"]["status"] == "rejected"
    assert view["target"]["action_id"] == action["id"]
    assert len(env.endpoint.requests) == original_requests
    assert await env.followups._record("effect", effect["id"]) == effect
