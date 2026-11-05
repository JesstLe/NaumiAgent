"""Exact source identities, real civil-day windows and immutable deadline semantics."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from naumi_agent.personal import today_models as models
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import digest
from naumi_agent.personal.today_models import (
    day_identity,
    day_window,
    deadline_status,
    item_ref,
    metadata_identity,
    normalize_deadline,
    normalize_item_ref,
    normalize_metadata_ref,
    normalize_today_ref,
    placement_identity,
    record_digest,
    semantic_item_ref,
    validate_deadline,
)

WORKSPACE = "a" * 64
ITEM = "pi_" + "1" * 32
ACTOR = "pa_" + "2" * 32
REF = {
    "kind": "item",
    "workspace_key": WORKSPACE,
    "item_id": ITEM,
    "item_revision": 1,
    "once_key": "3" * 32,
    "definition_sha256": "4" * 64,
    "assignee_id": "main",
    "assignee_version": 0,
}


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def assert_error(code, callback):
    with pytest.raises(PersonalAgentError) as caught:
        callback()
    if code is not None:
        assert caught.value.code == code


def test_exact_item_reference_is_detached_and_lifecycle_semantics_preserve_once_identity():
    ref = normalize_item_ref(copy.deepcopy(REF))
    assert ref == REF and ref is not REF
    assert semantic_item_ref(ref) == semantic_item_ref(ref | {"item_revision": 9})
    for change in (
        {"once_key": "5" * 32},
        {"definition_sha256": "6" * 64},
        {"workspace_key": "7" * 64},
        {"assignee_id": ACTOR, "assignee_version": 1},
    ):
        assert semantic_item_ref(ref) != semantic_item_ref(ref | change)
    assert "item_revision" not in semantic_item_ref(ref)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "routine"},
        {"kind": []},
        {"workspace_key": "A" * 64},
        {"item_id": "pi_" + "0" * 31},
        {"item_revision": 0},
        {"item_revision": True},
        {"item_revision": 1.0},
        {"once_key": "1" * 31},
        {"definition_sha256": "x" * 64},
        {"assignee_id": "team"},
        {"assignee_id": ACTOR, "assignee_version": 0},
        {"assignee_version": 1},
        {"assignee_version": False},
        {"unknown": "value"},
        {"owner_key": "a" * 64},
        {"trusted_channel": "web"},
    ],
)
def test_exact_source_ref_rejects_invalid_or_trusted_fields(changes):
    assert_error(None, lambda: normalize_item_ref(REF | changes))


@pytest.mark.parametrize(
    "value", [None, [], "ref", {}, {k: v for k, v in REF.items() if k != "kind"}]
)
def test_exact_reference_requires_complete_object(value):
    assert_error(None, lambda: normalize_item_ref(value))


def test_item_snapshot_reference_comes_from_original_definition_route():
    item = {
        "item_id": ITEM,
        "revision": 1,
        "once_key": REF["once_key"],
        "definition_sha256": REF["definition_sha256"],
        "workspace_key": WORKSPACE,
        "definition": {
            "route": {"workspace_key": WORKSPACE},
            "assignee": {"id": "main", "version": 0},
        },
    }
    assert item_ref(item) == REF
    assert_error("invalid_input", lambda: item_ref(item | {"workspace_key": "b" * 64}))
    assert_error("invalid_input", lambda: item_ref(item | {"definition": {}}))
    for value in (None, [], {}, {"definition": []}):
        assert_error("invalid_input", lambda v=value: item_ref(v))


def test_derived_identities_are_stable_domain_separated_and_field_bound():
    day = day_identity(WORKSPACE, "main", "2026-11-05", "UTC")
    metadata = metadata_identity(WORKSPACE, ITEM)
    placement = placement_identity(day, metadata)
    assert day.startswith("td_") and metadata.startswith("tm_") and placement.startswith("tp_")
    assert len(day) == len(metadata) == len(placement) == 35
    assert day == day_identity(WORKSPACE, "main", "2026-11-05", "UTC")
    assert metadata == metadata_identity(WORKSPACE, ITEM)
    assert placement == placement_identity(day, metadata)
    for args in (
        ("b" * 64, "main", "2026-11-05", "UTC"),
        (WORKSPACE, ACTOR, "2026-11-05", "UTC"),
        (WORKSPACE, "main", "2026-11-06", "UTC"),
        (WORKSPACE, "main", "2026-11-05", "Asia/Shanghai"),
    ):
        assert day_identity(*args) != day
    assert metadata_identity(WORKSPACE, "pi_" + "9" * 32) != metadata
    assert (
        placement_identity(day_identity(WORKSPACE, ACTOR, "2026-11-05", "UTC"), metadata)
        != placement
    )


@pytest.mark.parametrize(
    "callback",
    [
        lambda: day_identity("bad", "main", "2026-11-05", "UTC"),
        lambda: day_identity(WORKSPACE, "operator", "2026-11-05", "UTC"),
        lambda: day_identity(WORKSPACE, "main", "2026-02-30", "UTC"),
        lambda: day_identity(WORKSPACE, "main", "2026-11-05", "../UTC"),
        lambda: metadata_identity(WORKSPACE, "main"),
        lambda: placement_identity("tm_" + "1" * 32, "tm_" + "2" * 32),
        lambda: placement_identity("td_" + "1" * 32, "pi_" + "2" * 32),
    ],
)
def test_invalid_identity_components_never_generate_ids(callback):
    assert_error(None, callback)


def test_record_digest_covers_every_field_but_not_an_external_digest():
    record = {"id": "record", "revision": 1, "deadline": {"kind": "date"}, "placements": []}
    assert record_digest(record) == digest(record)
    assert record_digest(record | {"digest": "a" * 64}) == record_digest(record)
    for changes in ({"revision": 2}, {"id": "other"}, {"deadline": None}, {"placements": ["a"]}):
        assert record_digest(record | changes) != record_digest(record)
    assert_error("invalid_input", lambda: record_digest([]))
    assert_error("invalid_input", lambda: record_digest({"value": float("nan")}))


def today_reference(**changes):
    window = day_window("2026-11-05", "UTC")
    return {
        "day_id": day_identity(WORKSPACE, "main", window["date"], window["timezone"]),
        "day_revision": 0,
        "day_digest": "b" * 64,
        "workspace_key": WORKSPACE,
        "planner_id": "main",
        **{k: window[k] for k in ("date", "timezone", "timezone_sha256")},
    } | changes


def metadata_reference(**changes):
    return {
        "metadata_id": metadata_identity(WORKSPACE, ITEM),
        "metadata_revision": 0,
        "metadata_digest": "b" * 64,
        "workspace_key": WORKSPACE,
        "item_ref": copy.deepcopy(REF),
    } | changes


def test_virtual_and_persisted_today_and_metadata_refs_are_detached():
    for normalizer, factory, revision in (
        (normalize_today_ref, today_reference, "day_revision"),
        (normalize_metadata_ref, metadata_reference, "metadata_revision"),
    ):
        for number in (0, 1, 99):
            raw = factory(**{revision: number})
            normalized = normalizer(raw)
            assert normalized == raw and normalized is not raw
            raw[revision] = 123
            assert normalized[revision] == number


@pytest.mark.parametrize(
    "changes",
    [
        {"day_id": "td_" + "0" * 32},
        {"day_revision": -1},
        {"day_revision": True},
        {"day_digest": "x" * 64},
        {"timezone_sha256": None},
        {"date": "2026-11-06"},
        {"planner_id": ACTOR},
        {"timezone": "Asia/Shanghai"},
        {"unknown": True},
    ],
)
def test_today_refs_cannot_replace_derived_identity_or_forge_revision(changes):
    assert_error(None, lambda: normalize_today_ref(today_reference(**changes)))


@pytest.mark.parametrize(
    "changes",
    [
        {"metadata_id": "tm_" + "0" * 32},
        {"metadata_revision": True},
        {"metadata_revision": -1},
        {"metadata_digest": "bad"},
        {"workspace_key": "c" * 64},
        {"item_ref": REF | {"workspace_key": "c" * 64}},
        {"unknown": True},
    ],
)
def test_metadata_refs_remain_tied_to_the_original_workspace_item(changes):
    assert_error(None, lambda: normalize_metadata_ref(metadata_reference(**changes)))


@pytest.mark.parametrize(
    "day,zone,hours,start,end",
    [
        ("2026-11-05", "Asia/Shanghai", 24, "2026-11-04T16:00:00Z", "2026-11-05T16:00:00Z"),
        ("2026-03-08", "America/New_York", 23, "2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z"),
        ("2026-11-01", "America/New_York", 25, "2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z"),
        ("2020-11-01", "America/Havana", 25, "2020-11-01T04:00:00Z", "2020-11-02T05:00:00Z"),
        ("2018-11-04", "America/Sao_Paulo", 23, "2018-11-04T03:00:00Z", "2018-11-05T02:00:00Z"),
        ("2011-12-29", "Pacific/Apia", 24, "2011-12-29T10:00:00Z", "2011-12-30T10:00:00Z"),
        ("1969-09-30", "Pacific/Kwajalein", 47, "1969-09-29T13:00:00Z", "1969-10-01T12:00:00Z"),
    ],
)
def test_civil_windows_use_real_transitions_not_fixed_day_lengths(day, zone, hours, start, end):
    window = day_window(day, zone)
    assert window["date"] == day and window["timezone"] == zone
    assert len(window["timezone_sha256"]) == 64
    assert instant(window["start_at"]) == instant(start)
    assert instant(window["end_at"]) == instant(end)
    assert instant(window["end_at"]) - instant(window["start_at"]) == timedelta(hours=hours)
    assert instant(window["start_at"]).astimezone(ZoneInfo(zone)).date().isoformat() == day
    assert (instant(window["end_at"]) - timedelta(microseconds=1)).astimezone(
        ZoneInfo(zone)
    ).date().isoformat() == day


def test_historical_havana_midnight_fold_is_independent_of_today_day_resolver():
    # Use an elapsed transition: current TZif no longer predicts Havana's 2026 fall-back.
    zone = ZoneInfo("America/Havana")
    midnight = datetime(2020, 11, 1)
    first = midnight.replace(tzinfo=zone, fold=0)
    second = midnight.replace(tzinfo=zone, fold=1)
    assert first.utcoffset() == timedelta(hours=-4)
    assert second.utcoffset() == timedelta(hours=-5)
    assert first.astimezone(UTC) == instant("2020-11-01T04:00:00Z")
    assert second.astimezone(UTC) == instant("2020-11-01T05:00:00Z")
    assert first.astimezone(UTC).astimezone(zone).fold == 0
    assert second.astimezone(UTC).astimezone(zone).fold == 1
    next_midnight = datetime(2020, 11, 2, tzinfo=zone)
    assert next_midnight.utcoffset() == timedelta(hours=-5)
    assert next_midnight.astimezone(UTC) == instant("2020-11-02T05:00:00Z")


@pytest.mark.parametrize(
    "day,zone,code",
    [
        ("2011-12-30", "Pacific/Apia", "nonexistent_date"),
        ("1993-08-21", "Pacific/Kwajalein", "nonexistent_date"),
        ("2026-02-29", "UTC", "invalid_date"),
        ("2026-1-01", "UTC", "invalid_date"),
        ("9999-12-31", "UTC", "invalid_date"),
        ("0000-01-01", "UTC", "invalid_date"),
        ("2026-11-05", "C:/Windows", "invalid_timezone"),
        ("2026-11-05", "../UTC", "invalid_timezone"),
    ],
)
def test_skipped_dates_invalid_dates_and_paths_are_explicit_errors(day, zone, code):
    assert_error(code, lambda: day_window(day, zone))


def test_past_deadlines_are_valid_and_date_means_exclusive_civil_end():
    value = normalize_deadline(
        {"kind": "date", "local_date": "2011-12-29", "timezone": "Pacific/Apia"}
    )
    assert value["boundary_at"] == day_window("2011-12-29", "Pacific/Apia")["end_at"]
    boundary = instant(value["boundary_at"])
    assert deadline_status(value, boundary - timedelta(microseconds=1)) == "upcoming"
    assert deadline_status(value, boundary) == "overdue"
    assert deadline_status(value, boundary + timedelta(days=1)) == "overdue"
    assert normalize_deadline(None) is None
    assert deadline_status(None, datetime.now(UTC)) == "none"


def test_exact_deadline_normalizes_and_requires_explicit_dst_fold():
    raw = {"kind": "instant", "local_time": "2026-11-01T01:30:00", "timezone": "America/New_York"}
    assert_error("ambiguous_time", lambda: normalize_deadline(raw))
    first = normalize_deadline(raw | {"fold": 0})
    second = normalize_deadline(raw | {"fold": 1})
    assert instant(first["resolved_at"]) == instant("2026-11-01T05:30:00Z")
    assert instant(second["resolved_at"]) == instant("2026-11-01T06:30:00Z")
    assert first["fold"] == 0 and second["fold"] == 1
    assert deadline_status(first, instant(first["resolved_at"])) == "due"
    assert (
        deadline_status(first, instant(first["resolved_at"]) + timedelta(microseconds=1))
        == "overdue"
    )


def test_non_hour_dst_fold_and_microseconds_are_not_rounded():
    raw = {
        "kind": "instant",
        "local_time": "2026-04-05T01:45:00.000123",
        "timezone": "Australia/Lord_Howe",
    }
    a = normalize_deadline(raw | {"fold": 0})
    b = normalize_deadline(raw | {"fold": 1})
    assert instant(b["resolved_at"]) - instant(a["resolved_at"]) == timedelta(minutes=30)
    assert instant(a["resolved_at"]).microsecond == instant(b["resolved_at"]).microsecond == 123


@pytest.mark.parametrize(
    "value,code",
    [
        ([], "invalid_deadline"),
        ({"kind": []}, "invalid_deadline"),
        (
            {
                "kind": "instant",
                "local_time": "2026-03-08T02:30:00",
                "timezone": "America/New_York",
            },
            "nonexistent_time",
        ),
        ({"kind": "instant", "local_time": "2026-11-05", "timezone": "UTC"}, "invalid_deadline"),
        (
            {"kind": "instant", "local_time": "2026-11-05T00:00:00Z", "timezone": "UTC"},
            "invalid_deadline",
        ),
        (
            {"kind": "instant", "local_time": "2026-11-05T00:00:00", "timezone": "UTC", "fold": 1},
            "invalid_deadline",
        ),
        (
            {
                "kind": "instant",
                "local_time": "2026-11-05T00:00:00",
                "timezone": "UTC",
                "fold": True,
            },
            "invalid_input",
        ),
        (
            {"kind": "date", "local_date": "2011-12-30", "timezone": "Pacific/Apia"},
            "nonexistent_date",
        ),
        (
            {
                "kind": "date",
                "local_date": "2026-11-05",
                "timezone": "UTC",
                "boundary_at": "forged",
            },
            "invalid_deadline",
        ),
        (
            {
                "kind": "date",
                "local_date": "2026-11-05",
                "timezone": "UTC",
                "timezone_sha256": "a" * 64,
            },
            "invalid_deadline",
        ),
        (
            {
                "kind": "instant",
                "local_time": "2026-11-05T00:00:00",
                "timezone": "UTC",
                "fold": None,
            },
            "invalid_input",
        ),
    ],
)
def test_deadline_raw_inputs_never_accept_forged_server_fields(value, code):
    assert_error(code, lambda: normalize_deadline(value))


@pytest.mark.parametrize("kind", ["date", "instant"])
def test_saved_deadlines_roundtrip_without_mutating_original(kind):
    raw = {"kind": kind, "timezone": "UTC"}
    raw.update(
        {"local_date": "2026-11-05"} if kind == "date" else {"local_time": "2026-11-05T09:00"}
    )
    value = normalize_deadline(raw)
    restored = validate_deadline(value)
    assert restored == value and restored is not value
    restored["timezone"] = "Asia/Shanghai"
    assert value["timezone"] == "UTC"
    assert_error("invalid_deadline", lambda: normalize_deadline(value))
    assert_error(None, lambda: validate_deadline(value | {"extra": True}))
    stamp = "boundary_at" if kind == "date" else "resolved_at"
    assert_error(
        "invalid_deadline",
        lambda: validate_deadline(value | {stamp: "2026-11-05T01:00:00.000000+00:00"}),
    )


def test_timezone_data_change_preserves_original_resolved_deadline_and_requests_reconfirmation(
    monkeypatch,
):
    raw = {"kind": "instant", "local_time": "2026-11-05T09:00", "timezone": "UTC"}
    value = normalize_deadline(raw)
    before = copy.deepcopy(value)
    monkeypatch.setattr(models, "_zone", lambda _name: (ZoneInfo("UTC"), "b" * 64))
    assert validate_deadline(value) == before
    assert deadline_status(value, instant("2026-11-05T12:00:00Z")) == "timezone_data_changed"
    assert value == before
    assert_error(
        "calendar_timezone_changed", lambda: validate_deadline(value, allow_timezone_change=False)
    )


@pytest.mark.parametrize(
    "now", [None, True, datetime(2026, 11, 5), "2026-11-05T00:00:00", "not-time"]
)
def test_deadline_clock_must_be_explicitly_aware_even_for_no_deadline(now):
    assert_error("invalid_deadline", lambda: deadline_status(None, now))


def test_saved_deadline_rejects_invalid_kinds_noncanonical_utc_and_invalid_tzif_fields():
    for value in ([], {}, {"kind": []}, {"kind": "other"}):
        assert_error("invalid_deadline", lambda v=value: validate_deadline(v))
    value = normalize_deadline({"kind": "date", "local_date": "2026-11-05", "timezone": "UTC"})
    for patch in ({"timezone_sha256": "bad"}, {"boundary_at": "2026-11-06T00:00:00Z"}):
        assert_error(None, lambda p=patch: validate_deadline(value | p))
    assert_error("invalid_input", lambda: validate_deadline(value, allow_timezone_change=1))
