"""Research schema, exact evidence, Decimal review and real journal invariants."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_models import (
    Calculation,
    Citation,
    CreateNotebook,
    Quantity,
    ResearchClaim,
    ResearchConclusion,
    ReviseNotebook,
    decimal_value,
    validate_payload,
)
from naumi_agent.personal.research_review import review_notebook
from naumi_agent.personal.research_store import (
    APPEND_ONLY,
    KINDS,
    ResearchStore,
    ResearchTransaction,
)

NOW = datetime(2026, 11, 6, 9, tzinfo=UTC)
SOURCE = "rs_" + "1" * 32
NOTEBOOK = "rq_" + "2" * 32
UPSTREAM = "rq_" + "3" * 32
OWNER = "a" * 64
WORKSPACE = "b" * 64
BODY = "😀销售收入 USD 100；销售成本 USD 25；明细已更新。"


def source(body=BODY, **updates):
    checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return {
        "revision": 1,
        "body": body,
        "content_sha256": checksum,
        "current_digest": checksum,
        "status": "available",
        "latest_revision": 1,
        "captured_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
        **updates,
    }


def citation(quote="销售收入 USD 100", **updates):
    return {"source_id": SOURCE, "source_revision": 1, "quote": quote, **updates}


def claim(identity="income", *, value="100", quote="销售收入 USD 100", **updates):
    return {
        "id": identity,
        "text": "原资料的明确数量",
        "kind": "fact",
        "citations": [citation(quote)],
        "quantity": {"value": value, "unit": "currency", "currency": "USD", "period": "2026Q3"},
        **updates,
    }


def notebook(claims=None, **updates):
    return {
        "id": NOTEBOOK,
        "revision": 1,
        "claims": [claim()] if claims is None else claims,
        "conclusion": {"stance": "undecided", "premises": [], "invalidators": []},
        **updates,
    }


def review(value=None, sources=None, upstream=None, now=NOW):
    return review_notebook(
        notebook() if value is None else value,
        {SOURCE: source()} if sources is None else sources,
        {} if upstream is None else upstream,
        now,
    )


def codes(result):
    return {item["code"] for item in result["findings"]}


def calculation_book(operation="percent", expected="25", declared=None):
    inputs = [claim(), claim("cost", value="25", quote="销售成本 USD 25")]
    identifiers = ["cost", "income"] if operation in {"percent", "ratio"} else ["income", "cost"]
    unit = "percent" if operation == "percent" else "ratio" if operation == "ratio" else "currency"
    output = claim(
        "result",
        value=expected if declared is None else declared,
        citations=[citation(), citation("销售成本 USD 25")],
        quantity={
            "value": expected if declared is None else declared,
            "unit": unit,
            "currency": None if unit != "currency" else "USD",
            "period": "2026Q3",
        },
        calculation={
            "operation": operation,
            "inputs": identifiers,
            "expected": expected,
            "tolerance": "0.000001",
        },
    )
    return notebook([*inputs, output])


def enum_nodes(value):
    if isinstance(value, dict):
        if "enum" in value or "const" in value:
            yield value
        for key in ("properties", "$defs", "definitions"):
            for child in value.get(key, {}).values():
                yield from enum_nodes(child)
        for key in ("oneOf", "allOf", "anyOf", "prefixItems"):
            for child in value.get(key, []):
                yield from enum_nodes(child)
        if isinstance(value.get("items"), dict):
            yield from enum_nodes(value["items"])


@pytest.mark.parametrize(
    "model",
    [
        CreateNotebook,
        ReviseNotebook,
        ResearchClaim,
        ResearchConclusion,
        Citation,
        Quantity,
        Calculation,
    ],
)
def test_public_schemas_have_explicit_provider_types(model):
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert all(node.get("type") == "string" for node in enum_nodes(schema))


def test_models_normalize_defaults_without_authority_or_identity_injection():
    create = {
        "request_id": "create-original",
        "title": "收入核对",
        "question": "原比例正确吗？",
        "domain": "operations",
    }
    assert validate_payload(CreateNotebook, create) == create
    revision = {**create, "expected_revision": 1, "claims": [claim()], "conclusion": {}}
    value = validate_payload(ReviseNotebook, revision)
    assert value["claims"][0]["assumptions"] == []
    assert value["claims"][0]["subject"] == ""
    assert value["conclusion"]["stance"] == "undecided"
    for field in ("actor_id", "workspace_key", "verified", "review_status", "trusted_channel"):
        with pytest.raises(PersonalAgentError) as caught:
            validate_payload(ReviseNotebook, {**revision, field: "RAW-PRIVATE-SECRET-7391"})
        assert caught.value.code == "invalid_input"
        assert "RAW-PRIVATE-SECRET-7391" not in caught.value.message


@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        1,
        False,
        True,
        0.25,
        "NaN",
        "Infinity",
        "-Infinity",
        "1e31",
        "1000000000000000000000000000000.1",
        "1e-19",
        "0.0000000000000000001",
        "1e999",
        "1e9999",
        " 1",
        "1 ",
        "1_000",
        "1,000",
        "",
        "9" * 81,
    ],
)
def test_quantities_reject_coercion_nonfinite_unbounded_values_and_excess_precision(value):
    with pytest.raises(PersonalAgentError):
        validate_payload(Quantity, {"value": value, "unit": "count"})


@pytest.mark.parametrize(
    "value", ["0", "-0", "+0", ".5", "001.50", "-123.456", "1e30", "0.000000000000000001"]
)
def test_exact_decimal_strings_are_preserved(value):
    result = validate_payload(Quantity, {"value": value, "unit": "count"})
    assert result["value"] == value and decimal_value(value) == Decimal(value)


@pytest.mark.parametrize(
    "change",
    [
        {"operation": "eval"},
        {"inputs": ["a", "a"]},
        {"inputs": ["a"]},
        {"inputs": ["a", "b", "c"]},
        {"expected": "NaN"},
        {"tolerance": "-0.1"},
        {"tolerance": 0.1},
        {"__import__": "os"},
    ],
)
def test_calculation_contract_never_accepts_evaluation_or_ambiguous_inputs(change):
    value = {"operation": "ratio", "inputs": ["a", "b"], "expected": "0.5", **change}
    with pytest.raises(PersonalAgentError):
        validate_payload(Calculation, value)


@pytest.mark.parametrize(
    "change",
    [
        {"source_revision": True},
        {"source_revision": 0},
        {"source_id": "rs_bad"},
        {"quote": "   "},
        {"occurrence": -1},
        {"occurrence": True},
        {"quote": "x\0y"},
        {"trusted": True},
    ],
)
def test_citation_contract_rejects_forged_metadata_and_invalid_positions(change):
    with pytest.raises(PersonalAgentError):
        validate_payload(Citation, {**citation(), **change})


def test_duplicate_claims_and_references_are_rejected_before_storage():
    create = {
        "request_id": "revise-1",
        "title": "核对",
        "question": "原比例？",
        "domain": "general",
        "expected_revision": 1,
        "conclusion": {},
    }
    with pytest.raises(PersonalAgentError):
        validate_payload(ReviseNotebook, {**create, "claims": [claim(), claim()]})
    with pytest.raises(PersonalAgentError):
        validate_payload(ResearchClaim, claim(citations=[citation(), citation()]))
    reference = {"notebook_id": UPSTREAM, "revision": 1, "claim_id": "original"}
    with pytest.raises(PersonalAgentError):
        validate_payload(ResearchClaim, claim(upstream_claims=[reference, reference]))


def test_exact_original_quote_has_codepoint_byte_offsets_hash_and_no_semantic_upgrade():
    result = review()
    assert result["status"] == "mechanically_checked" and result["findings"] == []
    assert result["semantic_verification"] == "not_performed"
    anchor = result["anchors"][0]
    text = citation()["quote"]
    assert BODY[anchor["start"] : anchor["end"]] == text
    assert BODY.encode()[anchor["byte_start"] : anchor["byte_end"]].decode() == text
    assert anchor["byte_start"] > anchor["start"]
    assert anchor["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert result["claim_states"] == [
        {
            "claim_id": "income",
            "kind": "fact",
            "state": "anchored",
            "independently_verified": False,
            "upstream_unverified": False,
        }
    ]
    assert result["checked_at"] == NOW.isoformat(timespec="microseconds")


def test_repeated_quote_occurrence_is_exact_and_missing_occurrence_does_not_guess():
    body = "😀收入100；收入100。"
    value = notebook(
        [
            claim(
                value="100",
                quote="收入100",
                quantity={"value": "100", "unit": "count"},
            )
        ]
    )
    value["claims"][0]["citations"][0]["occurrence"] = 1
    result = review(value, {SOURCE: source(body)})
    assert result["findings"] == []
    assert result["anchors"][0]["start"] == body.rindex("收入100")
    value["claims"][0]["citations"][0]["occurrence"] = 2
    result = review(value, {SOURCE: source(body)})
    assert "quote_not_found" in codes(result) and result["anchors"] == []


@pytest.mark.parametrize(
    "updates,code",
    [
        ({"status": "unavailable"}, "source_unavailable"),
        ({"current_status": "withdrawn"}, "source_unavailable"),
        ({"available": False}, "source_unavailable"),
        ({"content_sha256": "f" * 64}, "source_integrity_mismatch"),
        ({"content_sha256": None, "current_digest": None}, "source_digest_unavailable"),
        ({"revision": 2}, "source_version_unavailable"),
        ({"revision": True}, "source_version_unavailable"),
    ],
)
def test_unavailable_or_corrupt_source_never_exposes_an_anchor(updates, code):
    result = review(sources={SOURCE: source(**updates)})
    assert code in codes(result) and result["anchors"] == []
    assert result["status"] == "issues_found"


def test_missing_source_and_changed_current_body_are_not_treated_as_current_proof():
    assert "source_version_unavailable" in codes(review(sources={}))
    result = review(sources={SOURCE: source(current_digest="f" * 64, latest_revision=2)})
    assert {"source_changed", "historical_source"} <= codes(result)
    assert result["anchors"] and result["status"] == "issues_found"


def test_explicit_historical_version_does_not_substitute_current_body():
    old = source()
    current = source("新正文不含原引用", revision=2)
    current["revisions"] = {"1": old}
    result = review(sources={SOURCE: current})
    assert result["anchors"][0]["quote"] == citation()["quote"]
    assert {"historical_source", "source_changed"} <= codes(result)


@pytest.mark.parametrize("expiry", [NOW.isoformat(), (NOW - timedelta(seconds=1)).isoformat()])
def test_expiry_is_visible_at_the_exact_boundary_and_keeps_historical_anchor(expiry):
    result = review(sources={SOURCE: source(expires_at=expiry)})
    assert "source_expired" in codes(result) and result["anchors"]
    assert result["status"] == "needs_sources"
    assert result["claim_states"][0]["state"] == "unverified"


@pytest.mark.parametrize("expiry", [None, "", "not-a-time", "2026-11-07T09:00:00"])
def test_missing_or_naive_source_expiry_remains_unknown(expiry):
    result = review(sources={SOURCE: source(expires_at=expiry)})
    assert "source_expiry_unknown" in codes(result)


@pytest.mark.parametrize(
    "observed", ["not-a-time", "2026-11-06T09:00:00", (NOW + timedelta(seconds=1)).isoformat()]
)
def test_future_or_invalid_observation_time_is_not_accepted(observed):
    result = review(sources={SOURCE: source(captured_at=observed)})
    assert "source_observation_invalid" in codes(result)


@pytest.mark.parametrize("kind", ["unknown", "estimate"])
def test_unknown_and_estimates_do_not_acquire_fact_status(kind):
    value = notebook([{"id": "uncertain", "text": "尚未确认的判断", "kind": kind}])
    result = review(value)
    assert ("unknown_claim" if kind == "unknown" else "missing_assumptions") in codes(result)
    assert result["claim_states"][0]["kind"] == kind
    assert result["claim_states"][0]["independently_verified"] is False
    if kind == "unknown":
        assert result["claim_states"][0]["state"] == "unknown"


def test_fact_without_citation_and_estimate_with_explicit_method_are_distinct():
    result = review(notebook([claim(citations=[])]))
    assert "missing_citation" in codes(result)
    value = notebook(
        [
            {
                "id": "forecast",
                "text": "下期约增长10%",
                "kind": "estimate",
                "assumptions": ["按同口径、需求不变的情形估算"],
            }
        ]
    )
    result = review(value)
    assert result["status"] == "mechanically_checked"
    assert result["claim_states"][0]["state"] == "estimated"
    assert result["semantic_verification"] == "not_performed"


@pytest.mark.parametrize("origin", [{"origin": "derived"}, {"upstream_unverified": True}])
def test_derived_source_keeps_its_upstream_unverified_tag(origin):
    result = review(sources={SOURCE: source(**origin)})
    assert "upstream_unverified" in codes(result)
    assert result["claim_states"][0]["upstream_unverified"] is True


@pytest.mark.parametrize(
    "upstream_value",
    [
        None,
        {"available": False},
        {"available": True, "kind": "estimate", "semantic_verification": "verified"},
        {"available": True, "kind": "fact", "semantic_verification": "not_performed"},
        {"available": True, "kind": "unknown", "semantic_verification": "verified"},
    ],
)
def test_unavailable_or_unverified_upstream_never_becomes_an_independent_check(upstream_value):
    reference = {"notebook_id": UPSTREAM, "revision": 1, "claim_id": "original"}
    value = notebook([claim(upstream_claims=[reference])])
    upstream = {} if upstream_value is None else {(UPSTREAM, 1, "original"): upstream_value}
    result = review(value, upstream=upstream)
    assert codes(result) & {"upstream_unverified", "upstream_unavailable"}
    assert result["claim_states"][0]["upstream_unverified"] is True
    assert result["claim_states"][0]["independently_verified"] is False


def test_even_verified_upstream_does_not_upgrade_a_declared_unknown():
    reference = {"notebook_id": UPSTREAM, "revision": 1, "claim_id": "original"}
    value = notebook([claim(kind="unknown", upstream_claims=[reference])])
    result = review(
        value,
        upstream={
            f"{UPSTREAM}:1:original": {
                "available": True,
                "kind": "fact",
                "semantic_verification": "verified",
            }
        },
    )
    assert result["claim_states"][0]["state"] == "unknown"


@pytest.mark.parametrize(
    "operation,expected",
    [("percent", "25"), ("ratio", "0.25"), ("sum", "125"), ("difference", "75")],
)
def test_exact_decimal_calculations_use_compatible_original_inputs(operation, expected):
    result = review(calculation_book(operation, expected))
    assert result["findings"] == []
    assert result["status"] == "mechanically_checked"
    assert result["calculations"][0]["computed"] == expected
    assert result["calculations"][0]["matches"] is True
    assert result["calculations"][0]["inputs_anchored"] is True
    assert all(item["independently_verified"] is False for item in result["claim_states"])


def test_injected_wrong_ratio_is_detected_and_a_real_revision_can_correct_it():
    original = calculation_book(expected="20")
    result = review(original)
    assert {"calculation_mismatch", "calculated_quantity_mismatch"} <= codes(result)
    assert result["calculations"][0]["computed"] == "25"
    revised = copy.deepcopy(original)
    revised["revision"] = 2
    revised["claims"][-1]["quantity"]["value"] = "25"
    revised["claims"][-1]["calculation"]["expected"] = "25"
    corrected = review(revised)
    assert corrected["findings"] == []
    assert original["revision"] == 1 and original["claims"][-1]["calculation"]["expected"] == "20"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("unit", "person", "calculation_unit_mismatch"),
        ("currency", "EUR", "calculation_currency_mismatch"),
        ("period", "2026Q2", "calculation_period_mismatch"),
    ],
)
def test_incompatible_input_dimensions_currencies_and_periods_stop_calculation(field, value, code):
    value_book = calculation_book()
    value_book["claims"][1]["quantity"][field] = value
    result = review(value_book)
    assert code in codes(result) and result["calculations"] == []


def test_wrong_output_unit_or_currency_is_detected_separately_from_arithmetic():
    value = calculation_book()
    value["claims"][-1]["quantity"].update(unit="ratio", currency="USD", period="2026Q4")
    result = review(value)
    assert {
        "calculation_output_unit_mismatch",
        "calculation_output_currency_mismatch",
        "calculation_output_period_mismatch",
    } <= codes(result)


def test_zero_denominator_and_unknown_or_missing_inputs_never_produce_a_result():
    value = calculation_book()
    value["claims"][0] = claim(value="0", quote="收入 USD 0")
    result = review(value, {SOURCE: source(BODY + "收入 USD 0")})
    assert "division_by_zero" in codes(result) and result["calculations"] == []
    value = calculation_book()
    value["claims"][0]["kind"] = "unknown"
    assert "calculation_input_unknown" in codes(review(value))
    value["claims"][-1]["calculation"]["inputs"] = ["missing", "cost"]
    assert "calculation_input_missing" in codes(review(value))


def test_calculation_cycles_and_invalid_input_evidence_are_retained():
    value = calculation_book()
    value["claims"][0]["calculation"] = {
        "operation": "sum",
        "inputs": ["cost", "result"],
        "expected": "100",
    }
    result = review(value)
    assert "calculation_cycle" in codes(result)
    value = calculation_book()
    value["claims"][0]["citations"] = []
    result = review(value)
    assert "calculation_inputs_unverified" in codes(result)
    assert result["calculations"][0]["inputs_anchored"] is False


def test_declared_tolerance_does_not_hide_a_large_discrepancy_as_strict_proof():
    value = calculation_book(expected="900")
    value["claims"][-1]["calculation"]["tolerance"] = "1000"
    result = review(value)
    assert {"calculation_rounded", "calculation_tolerance_large"} <= codes(result)
    assert result["calculations"][0]["exact_matches"] is False
    assert result["status"] != "mechanically_checked"


def test_result_out_of_bounds_is_not_serialized_as_infinity_or_rounded_float():
    value = calculation_book("sum", "1e30")
    value["claims"][0] = claim(value="1e30", quote="收入 USD 1e30")
    value["claims"][1] = claim("cost", value="1e30", quote="成本 USD 1e30")
    result = review(value, {SOURCE: source("收入 USD 1e30；成本 USD 1e30")})
    assert "calculation_out_of_range" in codes(result) and result["calculations"] == []
    json.dumps(result, allow_nan=False)


def test_number_currency_and_percentage_injections_are_anchored_to_the_correct_literal():
    result = review(notebook([claim(value="999")]))
    assert "quantity_not_in_quote" in codes(result)
    result = review(
        notebook(
            [
                claim(
                    quantity={
                        "value": "100",
                        "unit": "currency",
                        "currency": "EUR",
                    }
                )
            ]
        )
    )
    assert "currency_mismatch" in codes(result)
    body = "收入 USD 100，增长25%。"
    value = notebook([claim(quote=body)])
    result = review(value, {SOURCE: source(body)})
    assert "unit_mismatch" not in codes(result)
    value = notebook(
        [claim(value="25", quote="增长25%", quantity={"value": "25", "unit": "ratio"})]
    )
    assert "unit_mismatch" in codes(review(value, {SOURCE: source(body)}))


def test_same_subject_conflicts_are_not_hidden_and_distinct_periods_are_not_conflated():
    body = "收入 USD 100，收入 USD 101。"
    first = claim(quote="收入 USD 100", subject="营业收入")
    second = claim("other", value="101", quote="收入 USD 101", subject="营业收入")
    result = review(notebook([first, second]), {SOURCE: source(body)})
    assert "quantity_conflict" in codes(result)
    second["quantity"]["period"] = "2026Q4"
    result = review(notebook([first, second]), {SOURCE: source(body)})
    assert "quantity_conflict" not in codes(result)


def test_clear_stance_requires_visible_premises_and_invalidation_conditions():
    value = notebook(conclusion={"stance": "act"})
    assert {"missing_conclusion_premises", "missing_invalidators"} <= codes(review(value))
    value["conclusion"].update(premises=["统计口径一致"], invalidators=["原资料修订后重新核对"])
    assert review(value)["findings"] == []


def test_empty_notebook_and_naive_review_clock_do_not_look_completed():
    assert review(notebook([]))["status"] == "needs_sources"
    with pytest.raises(PersonalAgentError) as caught:
        review(now=NOW.replace(tzinfo=None))
    assert caught.value.code == "invalid_input"


@pytest.fixture
def store(tmp_path):
    return ResearchStore(tmp_path / "research", owner_key=OWNER)


def document(identity=NOTEBOOK, **updates):
    return {
        "id": identity,
        "actor_id": "main",
        "workspace_key": WORKSPACE,
        "notebook_id": NOTEBOOK,
        "status": "draft",
        "title": "原研究",
        **updates,
    }


def create(store, request_id="research-original-request"):
    return store.mutate(
        request_id,
        "create",
        {"notebook_id": NOTEBOOK},
        lambda tx: tx.put("notebook", document(), expected_revision=0),
    )


def test_research_store_is_distinct_strong_owner_journal_without_worker(store):
    assert KINDS == frozenset(
        {
            "notebook",
            "source",
            "review",
            "read",
            "intent",
            "publication",
            "discovery",
            "egress",
            "egress_decision",
            "context_update",
            "context_result",
            "context_head",
            "provenance",
            "semantic",
            "semantic_input",
            "semantic_submission",
            "peer_read",
        }
    )
    assert store.spec.namespace == "research" and store.spec.parent_field == "notebook_id"
    assert store.spec.domain_hashes is True and store.spec.lease_proofs is True
    assert store.transaction_type is ResearchTransaction
    assert not hasattr(store, "start") and not hasattr(store, "tick")
    assert store.read(lambda tx: tx) is None
    assert not store.base_dir.exists()


def test_original_cas_history_request_and_cold_reopen_preserve_identity(store):
    original = create(store)
    instance = store.identity()
    changed = store.write(
        lambda tx: tx.put(
            "notebook",
            document(title="修订后的研究"),
            expected_revision=1,
        )
    )
    reopened = ResearchStore(store.base_dir, owner_key=OWNER, expected_instance_id=instance)
    assert reopened.read(lambda tx: tx.get("notebook", NOTEBOOK)) == changed
    assert reopened.read(lambda tx: tx.get("notebook", NOTEBOOK, revision=1)) == original
    assert create(reopened) == original
    assert changed["revision"] == 2
    with pytest.raises(PersonalAgentError) as caught:
        reopened.write(lambda tx: tx.put("notebook", document(), expected_revision=1))
    assert caught.value.code == "version_conflict"


@pytest.mark.parametrize(
    "field,value",
    [("actor_id", "pa_" + "4" * 32), ("workspace_key", "c" * 64), ("notebook_id", UPSTREAM)],
)
def test_record_identity_and_scope_do_not_change_in_place(store, field, value):
    original = create(store)
    with pytest.raises(PersonalAgentError) as caught:
        store.write(lambda tx: tx.put("notebook", document(**{field: value}), expected_revision=1))
    assert caught.value.code == "binding_conflict"
    assert store.read(lambda tx: tx.get("notebook", NOTEBOOK)) == original


@pytest.mark.parametrize("kind", sorted(APPEND_ONLY))
def test_review_discovery_and_read_are_immutable_facts(store, kind):
    record = store.write(lambda tx: tx.put(kind, document("rr_" + "5" * 32), expected_revision=0))
    with pytest.raises(PersonalAgentError) as caught:
        store.write(lambda tx: tx.put(kind, record, expected_revision=1))
    assert caught.value.code == "immutable_" + kind


def test_duplicate_request_conflict_cannot_rebind_payload_or_operation(store):
    create(store)
    for operation, request in (
        ("revise", {"notebook_id": NOTEBOOK}),
        ("create", {"notebook_id": UPSTREAM}),
    ):
        with pytest.raises(PersonalAgentError) as caught:
            store.mutate(
                "research-original-request", operation, request, lambda tx: pytest.fail("replayed")
            )
        assert caught.value.code == "idempotency_conflict"


def test_concurrent_duplicate_admission_has_one_record_history_and_receipt(store):
    create(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: create(ResearchStore(store.base_dir, owner_key=OWNER)),
                range(16),
            )
        )
    assert all(value == results[0] for value in results)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert tuple(
            db.execute(f"SELECT count(*) FROM research_{name}").fetchone()[0]
            for name in ("records", "history", "requests")
        ) == (1, 1, 1)


def test_two_exact_revision_writers_have_one_winner_and_no_partial_history(store):
    create(store)

    def write(number):
        connection = ResearchStore(store.base_dir, owner_key=OWNER)
        try:
            return connection.write(
                lambda tx: tx.put(
                    "notebook",
                    document(title=str(number)),
                    expected_revision=1,
                )
            )
        except PersonalAgentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert results.count("version_conflict") == 1
    assert store.read(lambda tx: tx.get("notebook", NOTEBOOK))["revision"] == 2


def test_owner_binding_and_missing_original_history_fail_closed(store):
    create(store)
    wrong = ResearchStore(store.base_dir, owner_key="f" * 64)
    with pytest.raises(PersonalAgentError) as caught:
        wrong.read(lambda tx: tx.list("notebook"))
    assert caught.value.code == "storage_error"
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TRIGGER research_history_immutable_delete")
        db.execute("DELETE FROM research_history WHERE kind='notebook'")
    with pytest.raises(PersonalAgentError) as caught:
        store.read(lambda tx: tx.get("notebook", NOTEBOOK))
    assert caught.value.code == "storage_error"


def test_once_sent_lease_is_not_reclaimed_and_keeps_fencing_history(store):
    create(store)
    lease = store.write(
        lambda tx: tx.claim(
            "capture",
            NOTEBOOK,
            "holder-a",
            now=100,
            ttl=1,
            once=True,
        )
    )
    store.write(
        lambda tx: tx.require_lease(
            "capture",
            NOTEBOOK,
            "holder-a",
            lease["fence"],
            now=100,
            phase="sent",
        )
    )
    with pytest.raises(PersonalAgentError) as caught:
        store.write(lambda tx: tx.claim("capture", NOTEBOOK, "holder-b", now=102, ttl=1, once=True))
    assert caught.value.code == "research_unknown"
    current = store.read(lambda tx: tx.lease("capture", NOTEBOOK))
    assert current["fence"] == lease["fence"] and current["phase"] == "sent"
