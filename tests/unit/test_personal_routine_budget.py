"""Zero-paid routine ledger and real per-transport reservation contracts."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from naumi_agent.model.router import TokenUsage
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import digest
from naumi_agent.personal.routine_budget import (
    RoutineBudgetMixin,
    budget_initialize,
    budget_reserve,
    budget_settle,
    budget_started,
    budget_view,
)
from naumi_agent.personal.routine_store import RoutineStore

OWNER = "a" * 64
RID = "pr_" + "1" * 32
OID = "pro_" + "2" * 32
ITEM = "pi_" + "3" * 32
ROUTINE = {
    "id": RID,
    "epoch": "f" * 32,
    "revision": 1,
    "definition": {
        "policy": {"max_cost_microusd": 50000},
        "limits": {
            "max_executions": 4,
            "total_cost_microusd": 200000,
            "max_concurrent": 2,
            "max_pending": 4,
        },
    },
}
OCCURRENCE = {"id": OID, "routine_id": RID, "item_id": ITEM}
INTENT = {"item_id": ITEM, "request": {"request_sha256": "b" * 64}}
EVIDENCE = {"usage_source": "transport_response", "completed": True, "terminal": True}


class Router:
    def __init__(self, *, input_rate=1, output_rate=2, source="config", max_output=1000):
        self.input_rate, self.output_rate, self.source = input_rate, output_rate, source
        self.max_output = max_output

    def resolve_model(self, tier):
        return "fixture-model"

    def get_model_capability_contract(self, model):
        return SimpleNamespace(
            input_cost_per_million=self.input_rate,
            output_cost_per_million=self.output_rate,
            field_sources={
                "input_cost_per_million": self.source,
                "output_cost_per_million": self.source,
            },
        )

    def get_max_output(self, model):
        return self.max_output


class Service(RoutineBudgetMixin):
    def __init__(self, store, occurrence=OCCURRENCE):
        self.store, self.occurrence, self.authorizations = store, occurrence, 0

    async def occurrence_for_item(self, item_id):
        return self.occurrence if self.occurrence and item_id == ITEM else None

    async def authorize_occurrence_execution(self, occurrence, intent):
        self.authorizations += 1
        assert occurrence["item_id"] == intent["item_id"]

    async def _write(self, callback):
        return await asyncio.to_thread(self.store.write, callback)


@pytest.fixture
def store(tmp_path):
    value = RoutineStore(tmp_path / "routines", owner_key=OWNER)
    value.write(lambda tx: budget_initialize(tx, ROUTINE))
    return value


def reserve(store, oid=OID, routine=ROUTINE):
    return store.write(lambda tx: budget_reserve(tx, routine, oid))


def view(store, routine=ROUTINE):
    return store.read(lambda tx: budget_view(tx, routine))


def reservation(store, oid=OID):
    return store.read(lambda tx: tx.get("budget", "reservation:" + oid))


def assert_code(code, callback):
    with pytest.raises(PersonalAgentError) as caught:
        callback()
    assert caught.value.code == code


async def assert_async_code(code, callback):
    with pytest.raises(PersonalAgentError) as caught:
        await callback()
    assert caught.value.code == code


def test_occurrence_reservation_replay_does_not_consume_twice_or_touch_routine_revision(store):
    original = reserve(store)
    assert reserve(store) == original
    changed = deepcopy(ROUTINE)
    changed.update(epoch="e" * 32, revision=99)
    assert reserve(store, routine=changed) == original
    current = view(store)
    assert current["reserved_cost_microusd"] == 50000
    assert current["reserved_executions"] == 1 and current["started_executions"] == 0
    assert store.read(lambda tx: tx.get("routine", RID, missing=True)) is None


def test_started_and_failed_count_once_and_known_settlement_releases_only_unused_cost(store):
    reserve(store)
    first = store.write(lambda tx: budget_started(tx, OCCURRENCE))
    assert store.write(lambda tx: budget_started(tx, OCCURRENCE)) == first
    settled = store.write(
        lambda tx: budget_settle(tx, OCCURRENCE, 1234, {"job_state": "failed"}, True)
    )
    assert settled["known_cost_microusd"] == 1234 and settled["started"] is True
    current = view(store)
    assert current["reserved_executions"] == 0 and current["started_executions"] == 1
    assert current["settled_cost_microusd"] == 1234 and current["reserved_cost_microusd"] == 0
    assert (
        store.write(lambda tx: budget_settle(tx, OCCURRENCE, 1234, {"job_state": "failed"}, True))
        == settled
    )
    assert_code(
        "binding_conflict",
        lambda: store.write(lambda tx: budget_settle(tx, OCCURRENCE, 1235, {"forged": True}, True)),
    )


def test_unknown_keeps_full_cost_and_started_count_across_cold_reopen(store):
    reserve(store)
    first = store.write(
        lambda tx: budget_settle(tx, OCCURRENCE, None, {"job_state": "unknown"}, True)
    )
    reopened = RoutineStore(store.base_dir, owner_key=OWNER, expected_instance_id=store.identity())
    assert (
        reopened.write(
            lambda tx: budget_settle(tx, OCCURRENCE, None, {"job_state": "unknown"}, True)
        )
        == first
    )
    current = view(reopened)
    assert current["reserved_cost_microusd"] == 50000 and current["settled_cost_microusd"] == 0
    assert current["started_executions"] == 1 and current["usage_complete"] is False
    assert reservation(reopened)["known_cost_microusd"] is None


def test_prestart_cancel_requires_actual_lifecycle_receipt_not_missing_job_or_user_claim(store):
    reserve(store)
    store.write(lambda tx: budget_settle(tx, OCCURRENCE, 0, {"job_missing": True}, False))
    assert view(store)["reserved_executions"] == 1
    proof = {
        "job_id": "agent-job-original",
        "request_sha256": "b" * 64,
        "lifecycle_receipt": {
            "reason_code": "agent_job_cancelled_before_start",
            "receipt_sha256": "c" * 64,
        },
    }
    settled = store.write(lambda tx: budget_settle(tx, OCCURRENCE, 0, proof, False))
    assert settled["settlement"] == "prestart_cancelled"
    assert view(store)["reserved_executions"] == view(store)["reserved_cost_microusd"] == 0
    assert view(store)["unknown_reservations"] == 0
    assert_code("binding_conflict", lambda: store.write(lambda tx: budget_started(tx, OCCURRENCE)))


@pytest.mark.parametrize("field", ["max_executions", "total_cost_microusd"])
def test_zero_lifetime_limit_is_a_real_admission_stop(store, field):
    routine = deepcopy(ROUTINE)
    routine["definition"]["limits"][field] = 0
    assert_code("budget_exhausted", lambda: reserve(store, routine=routine))
    assert view(store)["reserved_cost_microusd"] == 0


def test_reduction_below_original_holds_is_rejected_and_last_partial_slot_is_bounded(store):
    routine = deepcopy(ROUTINE)
    routine["definition"]["limits"]["total_cost_microusd"] = 60000
    reserve(store, routine=routine)
    last = reserve(store, oid="pro_" + "4" * 32, routine=routine)
    assert last["cost_microusd"] == 10000
    assert_code("budget_exhausted", lambda: reserve(store, oid="pro_" + "5" * 32, routine=routine))
    reduced = deepcopy(routine)
    reduced["definition"]["limits"]["total_cost_microusd"] = 59999
    assert_code("budget_exhausted", lambda: view(store, reduced))


def test_overrun_evidence_never_fabricates_capped_cost_or_releases_unknown_hold(store):
    reserve(store)
    result = store.write(
        lambda tx: budget_settle(tx, OCCURRENCE, 50001, {"job_state": "error"}, True)
    )
    assert result["known_cost_microusd"] is None and result["settlement"] == "unknown"
    assert result["evidence"]["reported_overrun_cost_microusd"] == 50001
    assert view(store)["status"] == "blocked" and view(store)["reserved_cost_microusd"] == 50000
    assert_code("budget_exhausted", lambda: reserve(store, oid="pro_" + "4" * 32))


async def test_two_concurrent_admissions_cannot_take_the_same_last_slot(store):
    routine = deepcopy(ROUTINE)
    routine["definition"]["limits"].update(max_executions=1, total_cost_microusd=50000)

    def admit(i):
        try:
            independent = RoutineStore(store.base_dir, owner_key=OWNER)
            return independent.write(lambda tx: budget_reserve(tx, routine, f"occ-{i}"))
        except PersonalAgentError as exc:
            return exc.code

    results = await asyncio.gather(*(asyncio.to_thread(admit, i) for i in range(8)))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count("budget_exhausted") == 7


async def test_per_transport_reservation_is_persistent_and_max_output_is_shrunk_before_call(store):
    routine = deepcopy(ROUTINE)
    routine["definition"]["policy"]["max_cost_microusd"] = 6000
    reserve(store, routine=routine)
    service = Service(store)
    permit = await service.reserve_transport_call(
        INTENT,
        Router(),
        (),
        {"messages": [{"role": "user", "content": "预算"}], "max_tokens": 10000},
    )
    assert 0 < permit["max_tokens"] < 1000
    assert permit["reserved_cost_microusd"] <= 6000
    assert service.authorizations == 1
    call = store.read(lambda tx: tx.get("call", permit["call_id"]))
    assert call["status"] == "sent" and call["sequence"] == 1
    reopened = Service(
        RoutineStore(store.base_dir, owner_key=OWNER, expected_instance_id=store.identity())
    )
    await assert_async_code(
        "usage_unknown",
        lambda: reopened.reserve_transport_call(INTENT, Router(), (), {"messages": []}),
    )


@pytest.mark.parametrize("source", ["fallback", "unavailable", "mixed", "claimed-by-client"])
async def test_unverified_price_does_not_create_a_call_or_consume_transport_budget(store, source):
    reserve(store)
    service = Service(store)
    await assert_async_code(
        "budget_unpriced",
        lambda: service.reserve_transport_call(INTENT, Router(source=source), (), {"messages": []}),
    )
    assert store.read(lambda tx: tx.list("call")) == []
    assert reservation(store)["pending_calls"] == 0


@pytest.mark.parametrize("rate", [None, -1, float("nan"), float("inf"), True])
async def test_invalid_pricing_is_not_coerced_to_free(store, rate):
    reserve(store)
    await assert_async_code(
        "usage_unknown",
        lambda: Service(store).reserve_transport_call(
            INTENT, Router(input_rate=rate), (), {"messages": []}
        ),
    )
    assert store.read(lambda tx: tx.list("call")) == []


async def test_unbounded_remote_multimodal_input_is_blocked_before_a_call(store):
    reserve(store)
    await assert_async_code(
        "budget_unpriced",
        lambda: Service(store).reserve_transport_call(
            INTENT,
            Router(),
            (),
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.invalid/image"},
                            }
                        ],
                    }
                ]
            },
        ),
    )
    assert store.read(lambda tx: tx.list("call")) == []


async def test_zero_per_transport_usage_is_known_only_with_explicit_complete_source(store):
    reserve(store)
    service = Service(store)
    permit = await service.reserve_transport_call(INTENT, Router(), (), {"messages": []})
    known = await service.complete_transport_call(permit, TokenUsage(), EVIDENCE)
    assert known["status"] == "known" and known["known_cost_microusd"] == 0
    assert await service.complete_transport_call(permit, TokenUsage(), EVIDENCE) == known
    assert reservation(store)["transport_reserved_microusd"] == 0
    second = await service.reserve_transport_call(INTENT, Router(), (), {"messages": []})
    assert second["sequence"] == 2 and second["call_id"] != permit["call_id"]


@pytest.mark.parametrize(
    "evidence",
    [
        {"usage_source": "unavailable", "completed": True, "terminal": True},
        {"usage_source": "transport_response", "completed": False, "terminal": True},
        {"usage_source": "transport_response", "completed": True, "terminal": False},
    ],
)
async def test_missing_or_partial_usage_keeps_hold_and_prevents_automatic_retry(store, evidence):
    reserve(store)
    service = Service(store)
    permit = await service.reserve_transport_call(INTENT, Router(), (), {"messages": []})
    await assert_async_code(
        "usage_unknown", lambda: service.complete_transport_call(permit, TokenUsage(), evidence)
    )
    held = reservation(store)
    assert held["unknown_calls"] == 1 and held["pending_calls"] == 0
    assert held["transport_reserved_microusd"] == permit["reserved_cost_microusd"]
    await assert_async_code(
        "usage_unknown",
        lambda: service.reserve_transport_call(INTENT, Router(), (), {"messages": []}),
    )
    final = store.write(
        lambda tx: budget_settle(tx, OCCURRENCE, 0, {"run_usage": "zero-default"}, True)
    )
    assert final["known_cost_microusd"] is None and final["settlement"] == "unknown"


async def test_exact_known_calls_settle_once_and_final_run_rounding_does_not_erase_them(store):
    reserve(store)
    service = Service(store)
    permit = await service.reserve_transport_call(
        INTENT, Router(input_rate=0.1, output_rate=0.1), (), {"messages": []}
    )
    known = await service.complete_transport_call(
        permit, TokenUsage(input_tokens=1, output_tokens=1), EVIDENCE
    )
    assert known["known_cost_microusd"] == 1
    settled = store.write(
        lambda tx: budget_settle(tx, OCCURRENCE, 0, {"run_usage": "rounded-down"}, True)
    )
    assert settled["known_cost_microusd"] == 1 and view(store)["settled_cost_microusd"] == 1


@pytest.mark.parametrize(
    "field,value", [("input_tokens", -1), ("output_tokens", 1000000), ("cost_usd", 99)]
)
async def test_usage_outside_original_bound_is_unknown_not_silently_capped(store, field, value):
    reserve(store)
    service = Service(store)
    permit = await service.reserve_transport_call(INTENT, Router(), (), {"messages": []})
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0, field: value}
    with pytest.raises(PersonalAgentError):
        await service.complete_transport_call(permit, usage, EVIDENCE)
    assert reservation(store)["unknown_calls"] == 1
    if field == "cost_usd":
        assert view(store)["status"] == "blocked"
        call = store.read(lambda tx: tx.get("call", permit["call_id"]))
        assert call["evidence"]["observed_cost_microusd"] == 99000000


async def test_tampered_permit_is_not_a_new_authority_to_release_cost(store):
    reserve(store)
    service = Service(store)
    permit = await service.reserve_transport_call(INTENT, Router(), (), {"messages": []})
    changed = {**permit, "input_upper_tokens": permit["input_upper_tokens"] + 1}
    await assert_async_code(
        "binding_conflict", lambda: service.complete_transport_call(changed, TokenUsage(), EVIDENCE)
    )
    assert reservation(store)["pending_calls"] == 1


async def test_ordinary_item_hook_does_not_create_a_routine_or_read_pricing(store):
    service = Service(store, occurrence=None)
    assert await service.reserve_transport_call(INTENT, object(), (), {}) is None
    assert store.read(lambda tx: tx.list("call")) == [] and service.authorizations == 0


async def test_concurrent_transport_permits_for_one_occurrence_have_one_winner(store):
    reserve(store)
    service = Service(store)

    async def run():
        try:
            return await service.reserve_transport_call(INTENT, Router(), (), {"messages": []})
        except PersonalAgentError as exc:
            return exc.code

    results = await asyncio.gather(*(run() for _ in range(8)))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert results.count("usage_unknown") == 7
    assert len(store.read(lambda tx: tx.list("call"))) == 1


def _pre_dispatch(tx, *, attempted=False, status="blocked", item_id=None, wrong_digest=False):
    decision = {"reason": "input_guard_failed", "occurrence_id": OID}
    decision_sha = digest(decision)
    original = tx.put(
        "occurrence",
        {
            **OCCURRENCE,
            "status": status,
            "item_create_attempted": attempted,
            "item_id": item_id,
            "decision": decision,
            "decision_digest": "0" * 64 if wrong_digest else decision_sha,
        },
        expected_revision=0,
    )
    evidence = {
        "proof_kind": "routine_pre_dispatch",
        "occurrence_id": OID,
        "decision_sha256": decision_sha,
    }
    return original, evidence


def test_pre_dispatch_decision_cursor_and_zero_release_are_one_atomic_commit(store):
    def run(tx):
        budget_reserve(tx, ROUTINE, OID)
        original, evidence = _pre_dispatch(tx)
        return budget_settle(tx, original, 0, evidence, False)

    settled = store.mutate("pre-dispatch", "admit", {"slot": OID}, run)
    assert settled["settlement"] == "prestart_cancelled"
    assert view(store)["reserved_executions"] == view(store)["reserved_cost_microusd"] == 0
    assert (
        store.mutate("pre-dispatch", "admit", {"slot": OID}, lambda tx: pytest.fail("不重跑原决定"))
        == settled
    )


@pytest.mark.parametrize("change", ["attempted", "item", "status", "digest", "claim", "missing"])
def test_pre_dispatch_claims_are_checked_against_original_occurrence(store, change):
    reserve(store)
    if change == "missing":
        evidence = {
            "proof_kind": "routine_pre_dispatch",
            "occurrence_id": OID,
            "decision_sha256": "a" * 64,
        }
        assert_code(
            "not_found",
            lambda: store.write(lambda tx: budget_settle(tx, OCCURRENCE, 0, evidence, False)),
        )
        assert view(store)["reserved_cost_microusd"] == 50000
        return
    original, evidence = store.write(
        lambda tx: _pre_dispatch(
            tx,
            attempted=change == "attempted",
            item_id=ITEM if change == "item" else None,
            status="reserved" if change == "status" else "blocked",
            wrong_digest=change == "digest",
        )
    )
    if change == "claim":
        evidence["decision_sha256"] = "f" * 64
    result = store.write(lambda tx: budget_settle(tx, original, 0, evidence, False))
    assert result["settlement"] == "unknown"
    assert (
        view(store)["reserved_cost_microusd"] == 50000 and view(store)["reserved_executions"] == 1
    )


def test_pre_dispatch_cannot_erase_a_historical_item_create_attempt(store):
    reserve(store)
    original, evidence = store.write(lambda tx: _pre_dispatch(tx, attempted=True))
    reverted = store.write(
        lambda tx: tx.put(
            "occurrence", {**original, "item_create_attempted": False}, expected_revision=1
        )
    )
    result = store.write(lambda tx: budget_settle(tx, reverted, 0, evidence, False))
    assert result["settlement"] == "unknown" and view(store)["reserved_executions"] == 1


async def test_concurrent_pre_dispatch_release_and_create_attempt_have_one_mechanical_outcome(
    store,
):
    reserve(store)
    initial = store.write(
        lambda tx: tx.put(
            "occurrence",
            {
                **OCCURRENCE,
                "item_id": None,
                "item_create_attempted": False,
                "status": "reserved",
            },
            expected_revision=0,
        )
    )

    def mark_attempted(tx):
        current = tx.get("occurrence", OID)
        if current["status"] != "reserved":
            return "not-dispatched"
        return tx.put(
            "occurrence",
            {**current, "item_create_attempted": True},
            expected_revision=initial["revision"],
        )

    def decide_and_release(tx):
        current = tx.get("occurrence", OID)
        decision = {"reason": "cancelled", "occurrence_id": OID}
        saved = tx.put(
            "occurrence",
            {
                **current,
                "status": "blocked",
                "decision": decision,
                "decision_digest": digest(decision),
            },
            expected_revision=current["revision"],
        )
        return budget_settle(
            tx,
            saved,
            0,
            {
                "proof_kind": "routine_pre_dispatch",
                "occurrence_id": OID,
                "decision_sha256": digest(decision),
            },
            False,
        )

    await asyncio.gather(
        asyncio.to_thread(store.write, mark_attempted),
        asyncio.to_thread(store.write, decide_and_release),
    )
    final = store.read(lambda tx: tx.get("occurrence", OID))
    held = reservation(store)
    if final["item_create_attempted"]:
        assert held["settlement"] == "unknown" and view(store)["reserved_executions"] == 1
    else:
        assert (
            held["settlement"] == "prestart_cancelled" and view(store)["reserved_executions"] == 0
        )
