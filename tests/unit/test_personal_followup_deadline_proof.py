"""Single original Item proof, exact metadata identity and unchanged final fences."""
from __future__ import annotations

import copy

import pytest

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today_models import item_ref
from tests.unit.test_personal_followup_authority import (
    followup_authority as _followup_authority,
)
from tests.unit.test_personal_followup_authority import seed
from tests.unit.test_personal_today_authority import authority_system as _authority_system
from tests.unit.test_personal_today_authority import make_item, revoke, shared_item

authority_system = _authority_system
followup_authority = _followup_authority
pytestmark = pytest.mark.usefixtures("runtime_payload_key")


@pytest.mark.parametrize("current", [False, True])
async def test_deadline_source_has_one_original_item_proof_per_phase(
    followup_authority, monkeypatch, current
):
    env = followup_authority
    item, receipt, view = await seed(env)
    proposal = await env.followups._proposal(view["id"])
    rule = await env.followups._rule(receipt["identity"]["rule_id"])
    original = env.today._authorize_item
    calls = []

    async def counted(ref, actor, *, require_current=False):
        calls.append((copy.deepcopy(ref), actor, require_current))
        return await original(ref, actor, require_current=require_current)

    monkeypatch.setattr(env.today, "_authorize_item", counted)
    title, source = await env.followups._proposal_source(proposal, rule, current=current)
    assert title == item["definition"]["title"]
    assert source["item_ref"] == item_ref(item)
    assert calls == [(item_ref(item), "main", current)]


async def test_old_metadata_ref_interface_keeps_original_value_and_historical_semantics(
    followup_authority, monkeypatch
):
    env = followup_authority
    item, _, view = await seed(env)
    ref = view["source"]["metadata_ref"]
    expected = await env.today._record("metadata", ref["metadata_id"], revision=1)
    original = env.today._authorize_item
    calls = []

    async def counted(raw, actor, *, require_current=False):
        calls.append(require_current)
        return await original(raw, actor, require_current=require_current)

    monkeypatch.setattr(env.today, "_authorize_item", counted)
    actual = await env.today._authorized_metadata_ref(ref, "main")
    assert actual == expected and actual["item_ref"] == item_ref(item)
    assert calls == [False]


async def test_metadata_cannot_supply_another_items_body_with_its_own_valid_ref(
    followup_authority, monkeypatch
):
    env = followup_authority
    _, _, view = await seed(env)
    other = await make_item(env, "different-original-item")
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Mismatched identities must be rejected before Item proof")

    monkeypatch.setattr(env.today, "_authorize_item", forbidden)
    with pytest.raises(PersonalAgentError) as failure:
        await env.today._authorized_metadata_source(
            view["source"]["metadata_ref"], "main",
            expected_item_ref=item_ref(other), require_current=True,
        )
    assert failure.value.code == "source_unavailable" and not calls


async def test_single_proof_rechecks_original_grant_on_every_invocation(followup_authority):
    env = followup_authority
    owner, _, grant, item = await shared_item(env)
    _, receipt, view = await seed(env, item=item)
    proposal = await env.followups._proposal(view["id"])
    rule = await env.followups._rule(receipt["identity"]["rule_id"])
    await env.followups._proposal_source(proposal, rule, current=False)
    await revoke(env, owner, grant)
    for current in (False, True):
        with pytest.raises(PersonalAgentError):
            await env.followups._proposal_source(proposal, rule, current=current)
    assert (await env.followups.get(proposal["id"]))["title"] is None


async def test_each_phase_has_fresh_authority_when_grant_is_revoked_after_first_proof(
    followup_authority, monkeypatch
):
    env = followup_authority
    owner, _, grant, item = await shared_item(env)
    _, _, view = await seed(env, item=item)
    original = env.followups._proposal_source
    count = 0

    async def revoke_after_source(*args, **kwargs):
        nonlocal count
        result = await original(*args, **kwargs)
        count += 1
        if count == 1:
            await revoke(env, owner, grant)
        return result

    monkeypatch.setattr(env.followups, "_proposal_source", revoke_after_source)
    value = await env.followups.get(view["id"])
    assert value["title"] is value["source"] is value["target"] is None
    assert value["availability"] == "source_unavailable"
    assert count == 1
