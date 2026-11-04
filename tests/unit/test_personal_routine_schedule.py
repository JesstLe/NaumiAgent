"""Real IANA, Gregorian, exchange-session and bounded-recovery calendar contracts."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.routine_schedule import (
    MAX_QUERY_DAYS,
    available_calendars,
    catch_up_occurrences,
    coalesce_occurrences,
    count_occurrences,
    next_occurrences,
    normalize_schedule,
    validate_schedule,
)
from naumi_agent.personal.trading_calendar import (
    build_calendar_snapshot,
    validate_calendar_snapshot,
)

NOW = datetime(2026, 11, 5, tzinfo=UTC)


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def cron(expression="0 9 * * *", **changes):
    return normalize_schedule(
        {"kind": "cron", "timezone": "UTC", "expression": expression} | (changes), NOW
    )


def trading(**changes):
    value = {
        "kind": "trading_day",
        "timezone": "America/New_York",
        "calendar_id": "XNYS",
        "coverage_start": "2026-11-24",
        "coverage_end": "2026-11-30",
        "at": "close",
        "starts_at": "2026-11-24T00:00:00-05:00",
    } | changes
    return normalize_schedule(value, NOW)


def dues(values):
    return [dt(value["due_at"]) for value in values]


def test_resolved_protocol_is_detached_serializable_and_reopenable():
    raw = {"kind": "cron", "expression": "  0   9  * * * ", "timezone": "UTC"}
    resolved = normalize_schedule(raw, NOW)
    raw["expression"] = "0 2 * * *"
    assert resolved["version"] == 1 and resolved["expression"] == "0 9 * * *"
    assert resolved["starts_at"] == "2026-11-05T00:00:00.000000+00:00"
    assert resolved["dst_gap"] == "skip" and resolved["dst_fold"] == "first"
    assert len(resolved["timezone_sha256"]) == 64
    reopened = validate_schedule(json.loads(json.dumps(resolved)))
    assert reopened == resolved and reopened is not resolved


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "every_week"},
        {"kind": []},
        {"timezone": "Not/AZone"},
        {"timezone": "../UTC"},
        {"timezone": "C:/Windows/file"},
        {"timezone": 0},
        {"dst_gap": "shift"},
        {"dst_fold": "random"},
        {"unknown": True},
        {"starts_at": "2026-11-05"},
        {"ends_at": "2026-11-04T00:00:00Z"},
        {"version": 1},
        {"calendar_sessions": []},
    ],
)
def test_new_input_rejects_undefined_fields_and_semantics(changes):
    with pytest.raises(PersonalAgentError):
        cron(**changes)


def test_naive_clock_and_nonobject_inputs_fail_explicitly():
    for value, now in [([], NOW), ({"kind": "cron"}, NOW.replace(tzinfo=None))]:
        with pytest.raises(PersonalAgentError):
            normalize_schedule(value, now)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"timezone_sha256": "0" * 64}, "calendar_timezone_changed"),
        ({"version": True}, "invalid_schedule"),
        ({"starts_at": "2026-11-05T08:00:00+08:00"}, "invalid_schedule"),
        ({"expression": []}, "invalid_schedule"),
        ({"expression": "0  9 * * *"}, "invalid_schedule"),
        ({"ends_at": "2026-11-04T00:00:00.000000+00:00"}, "invalid_schedule"),
    ],
)
def test_persisted_protocol_is_strict_and_timezone_changes_do_not_reinterpret(changes, code):
    with pytest.raises(PersonalAgentError) as exc:
        validate_schedule(cron() | changes)
    assert exc.value.code == code


def test_once_start_end_and_after_boundaries_are_exact():
    resolved = normalize_schedule(
        {
            "kind": "once",
            "timezone": "Asia/Shanghai",
            "local_time": "2026-11-05T09:00:00",
            "starts_at": "2026-11-05T01:00:00Z",
            "ends_at": "2026-11-05T01:00:00Z",
        },
        NOW,
    )
    due = dt("2026-11-05T01:00:00Z")
    result = next_occurrences(resolved, due - timedelta(microseconds=1), 1, until=due)
    assert dues(result) == [due] and result[0]["local_time"] == "2026-11-05T09:00:00"
    assert next_occurrences(resolved, due, 1) == []
    assert next_occurrences(resolved, NOW, 1, until=due - timedelta(microseconds=1)) == []


@pytest.mark.parametrize(
    "local,fold,code",
    [
        ("2030-03-10T02:30:00", None, "nonexistent_time"),
        ("2030-11-03T01:30:00", None, "ambiguous_time"),
        ("2030-11-03T01:30:00", True, "invalid_schedule"),
        ("2030-11-03T01:30:00-04:00", 0, "invalid_schedule"),
        ("2030-11-03", 0, "invalid_schedule"),
    ],
)
def test_once_requires_real_unambiguous_local_time(local, fold, code):
    with pytest.raises(PersonalAgentError) as exc:
        normalize_schedule(
            {
                "kind": "once",
                "timezone": "America/New_York",
                "local_time": local,
                "fold": fold,
            },
            NOW,
        )
    assert exc.value.code == code


def test_once_two_folds_have_distinct_stable_utc_instants():
    results = [
        normalize_schedule(
            {
                "kind": "once",
                "timezone": "America/New_York",
                "local_time": "2030-11-03T01:30:00",
                "fold": fold,
            },
            NOW,
        )
        for fold in (0, 1)
    ]
    assert [value["due_at"] for value in results] == [
        "2030-11-03T05:30:00.000000+00:00",
        "2030-11-03T06:30:00.000000+00:00",
    ]
    changed = results[0] | {"due_at": results[1]["due_at"]}
    with pytest.raises(PersonalAgentError):
        validate_schedule(changed)


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("0 9 1 * 1", ["2026-12-01", "2026-12-07", "2026-12-14"]),
        ("0 9 * * 1", ["2026-12-07", "2026-12-14", "2026-12-21"]),
        ("0 9 1 * *", ["2026-12-01", "2027-01-01", "2027-02-01"]),
        ("0 9 31 2 1", ["2027-02-01", "2027-02-08", "2027-02-15"]),
        ("0 9 */2 * 1", ["2026-12-01", "2026-12-03", "2026-12-05"]),
    ],
)
def test_crontab_or_and_wildcard_day_contract(expression, expected):
    result = next_occurrences(cron(expression), "2026-11-30T09:00:00Z", 3)
    assert [value["local_time"][:10] for value in result] == expected


def test_number_step_extends_to_field_end_and_sunday_alias_deduplicates():
    result = next_occurrences(cron("5/15 9 * * *"), NOW, 4)
    assert [value.minute for value in dues(result)] == [5, 20, 35, 50]
    sunday = next_occurrences(cron("0 9 * * 0,7"), NOW, 2)
    assert [value.date().isoformat() for value in dues(sunday)] == ["2026-11-08", "2026-11-15"]


@pytest.mark.parametrize(
    "expression",
    [
        "* * * *",
        "* * * * * *",
        "*/0 * * * *",
        "*/-1 * * * *",
        "1-0 * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "* * * * MON",
        "*,,2 * * * *",
        "1//2 * * * *",
        "/2 * * * *",
        "1-2-3 * * * *",
        "0 9 31 2 *",
        "1/a * * * *",
        "1-/2 * * * *",
        "*" * 257,
        None,
    ],
)
def test_invalid_cron_is_rejected_without_searching(expression):
    with pytest.raises(PersonalAgentError):
        cron(expression)


def test_leap_day_search_crosses_century_nonleap_gap_without_minute_scanning():
    resolved = cron("0 9 29 2 *", starts_at="2096-03-01T00:00:00Z")
    start = perf_counter()
    result = next_occurrences(resolved, "2096-03-01T00:00:00Z", 1)
    assert dues(result) == [dt("2104-02-29T09:00:00Z")]
    assert perf_counter() - start < 2


def test_cron_microsecond_start_and_inclusive_end_do_not_round_backwards():
    resolved = cron(starts_at="2026-11-05T09:00:00.000001Z", ends_at="2026-11-06T09:00:00Z")
    assert dues(next_occurrences(resolved, NOW, 10)) == [dt("2026-11-06T09:00:00Z")]


def test_cron_gap_skips_nonexistent_wall_time():
    resolved = cron("30 2 * * *", timezone="America/New_York", starts_at="2030-03-09T00:00:00Z")
    result = next_occurrences(resolved, "2030-03-09T07:30:00Z", 1)
    assert dues(result) == [dt("2030-03-11T06:30:00Z")]


@pytest.mark.parametrize(
    "policy,hours,folds",
    [
        ("first", [5], [0]),
        ("second", [6], [1]),
        ("both", [5, 6], [0, 1]),
    ],
)
def test_cron_overlap_policy_selects_utc_ordered_fold_occurrences(policy, hours, folds):
    resolved = cron(
        "30 1 * * *",
        timezone="America/New_York",
        dst_fold=policy,
        starts_at="2030-11-03T00:00:00Z",
    )
    result = next_occurrences(resolved, "2030-11-03T00:00:00Z", 10, "2030-11-03T09:00:00Z")
    assert [value.hour for value in dues(result)] == hours
    assert [value["fold"] for value in result] == folds
    assert len({value["due_at"] for value in result}) == len(result)


def test_fold_both_is_globally_ordered_not_wall_minute_interleaved():
    resolved = cron(
        "* 1 * * *",
        timezone="America/New_York",
        dst_fold="both",
        starts_at="2030-11-03T00:00:00Z",
    )
    result = next_occurrences(resolved, "2030-11-03T05:58:00Z", 3)
    assert dues(result) == [
        dt(value)
        for value in (
            "2030-11-03T05:59:00Z",
            "2030-11-03T06:00:00Z",
            "2030-11-03T06:01:00Z",
        )
    ]


def test_non_hour_dst_and_skipped_date_are_actual_iana_cases():
    lord_howe = cron(
        "15 2 * * *",
        timezone="Australia/Lord_Howe",
        starts_at="2030-10-05T00:00:00Z",
    )
    result = next_occurrences(lord_howe, "2030-10-05T00:00:00Z", 1)
    assert result[0]["local_time"] == "2030-10-07T02:15:00"
    apia = cron("0 9 * * *", timezone="Pacific/Apia", starts_at="2011-12-29T00:00:00Z")
    skipped = next_occurrences(apia, "2011-12-29T19:00:00Z", 1)
    assert skipped[0]["local_time"] == "2011-12-31T09:00:00"


def test_host_timezone_environment_does_not_change_nominal_utc():
    script = (
        "import json;from datetime import UTC,datetime;"
        "from naumi_agent.personal.routine_schedule import normalize_schedule,next_occurrences;"
        "n=datetime(2026,11,5,tzinfo=UTC);"
        "s=normalize_schedule({'kind':'cron','timezone':'America/New_York',"
        "'expression':'0 9 * * *'},n);print(json.dumps(next_occurrences(s,n,1)))"
    )
    outputs = []
    for name in ("Asia/Shanghai", "America/Los_Angeles"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=dict(os.environ, TZ=name),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        outputs.append(json.loads(result.stdout))
    assert outputs[0] == outputs[1]
    assert outputs[0][0]["due_at"] == "2026-11-05T14:00:00.000000+00:00"


def test_count_coalesce_and_catchup_retain_original_due_identity():
    resolved = cron()
    after, until = "2026-11-05T09:00:00Z", "2026-11-10T09:00:00Z"
    expected = next_occurrences(resolved, after, 100, until)
    assert count_occurrences(resolved, after, until) == 5
    merged = coalesce_occurrences(resolved, after, until)
    assert merged == {"occurrence": expected[-1], "count": 5}
    collected, cursor, more = [], after, True
    while more:
        page = catch_up_occurrences(resolved, cursor, until, 2)
        collected.extend(page["occurrences"])
        more, cursor = page["has_more"], page["resume_after"]
    assert collected == expected
    assert len({value["due_at"] for value in collected}) == 5


def test_empty_window_and_exact_count_capacity_have_no_truncation():
    resolved = cron()
    before = "2026-11-05T08:00:00Z"
    assert count_occurrences(resolved, NOW, before, max_count=0) == 0
    assert coalesce_occurrences(resolved, NOW, before) == {"occurrence": None, "count": 0}
    assert catch_up_occurrences(resolved, NOW, before, 1) == {
        "occurrences": [],
        "has_more": False,
        "resume_after": "2026-11-05T00:00:00.000000+00:00",
    }
    for function in (count_occurrences, coalesce_occurrences):
        with pytest.raises(PersonalAgentError) as exc:
            function(resolved, NOW, "2026-11-05T09:00:00Z", max_count=0)
        assert exc.value.code == "calendar_query_limit"


@pytest.mark.parametrize("limit", [0, -1, True, 1001, 1.0])
def test_next_and_catchup_limits_are_strict(limit):
    for function in (next_occurrences, catch_up_occurrences):
        with pytest.raises(PersonalAgentError):
            if function is next_occurrences:
                function(cron(), NOW, limit)
            else:
                function(cron(), NOW, NOW + timedelta(days=1), limit)


def test_query_width_invalid_range_and_count_capacity_are_bounded():
    resolved = cron()
    with pytest.raises(PersonalAgentError) as exc:
        next_occurrences(resolved, NOW, 1, NOW + timedelta(days=MAX_QUERY_DAYS + 1))
    assert exc.value.code == "calendar_query_limit"
    with pytest.raises(PersonalAgentError):
        next_occurrences(resolved, NOW, 1, NOW - timedelta(seconds=1))
    for function in (count_occurrences, coalesce_occurrences):
        with pytest.raises(PersonalAgentError):
            function(resolved, NOW, None)
        with pytest.raises(PersonalAgentError):
            function(resolved, NOW, NOW, max_count=True)


def test_dense_query_has_bounded_real_work_and_exact_counts():
    resolved = cron("* * * * *")
    start = perf_counter()
    assert count_occurrences(resolved, NOW, NOW + timedelta(days=4)) == 5760
    assert len(next_occurrences(resolved, NOW, 1000)) == 1000
    assert perf_counter() - start < 5


def test_xnys_real_thanksgiving_holiday_and_half_day_close():
    resolved = trading()
    sessions = {value["date"]: value for value in resolved["calendar_sessions"]}
    assert "2026-11-26" not in sessions
    assert sessions["2026-11-27"]["early_close"] is True
    assert sessions["2026-11-27"]["close"] == "2026-11-27T18:00:00.000000+00:00"
    assert sessions["2026-11-30"]["close"] == "2026-11-30T21:00:00.000000+00:00"
    result = next_occurrences(resolved, "2026-11-25T22:00:00Z", 2)
    assert dues(result) == [dt("2026-11-27T18:00:00Z"), dt("2026-11-30T21:00:00Z")]
    assert result[0]["calendar_evidence"]["session_date"] == "2026-11-27"
    assert result[0]["calendar_evidence"]["sha256"] == resolved["calendar_evidence"]["sha256"]


def test_trading_close_offset_and_open_are_actual_session_times():
    shifted = trading(offset_minutes=10)
    assert dues(next_occurrences(shifted, "2026-11-26T00:00:00Z", 1)) == [
        dt("2026-11-27T18:10:00Z"),
    ]
    opened = trading(at="open")
    assert dues(next_occurrences(opened, "2026-11-26T00:00:00Z", 1)) == [
        dt("2026-11-27T14:30:00Z"),
    ]


def test_trading_local_time_is_filtered_by_real_session_dates():
    resolved = trading(at="time", local_time="09:00")
    result = next_occurrences(resolved, "2026-11-25T22:00:00Z", 1)
    assert dues(result) == [dt("2026-11-27T14:00:00Z")]
    assert result[0]["local_time"] == "2026-11-27T09:00:00"


def test_xshg_national_holiday_and_xhkg_half_day_are_not_weekday_guesses():
    shanghai = trading(
        calendar_id="XSHG",
        timezone="Asia/Shanghai",
        coverage_start="2026-09-25",
        coverage_end="2026-10-12",
        starts_at="2026-09-25T00:00:00+08:00",
    )
    days = [value["date"] for value in shanghai["calendar_sessions"]]
    assert all(f"2026-10-{number:02d}" not in days for number in range(1, 8))
    assert dues(next_occurrences(shanghai, "2026-09-30T08:00:00Z", 1)) == [
        dt("2026-10-08T07:00:00Z"),
    ]
    hongkong = trading(
        calendar_id="XHKG",
        timezone="Asia/Hong_Kong",
        coverage_start="2021-12-29",
        coverage_end="2022-01-04",
        starts_at="2021-12-29T00:00:00+08:00",
    )
    half = next(value for value in hongkong["calendar_sessions"] if value["date"] == "2021-12-31")
    assert half["close"] == "2021-12-31T04:00:00.000000+00:00"
    assert half["break_start"] is None and half["early_close"] is True


def test_snapshot_can_cover_a_single_holiday_with_zero_sessions():
    evidence, sessions = build_calendar_snapshot("XNYS", "2026-11-26", "2026-11-26")
    assert sessions == []
    assert validate_calendar_snapshot(evidence, sessions) == (evidence, sessions)
    resolved = trading(
        coverage_start="2026-11-26",
        coverage_end="2026-11-26",
        starts_at="2026-11-26T00:00:00-05:00",
        ends_at="2026-11-26T23:59:59-05:00",
    )
    assert count_occurrences(resolved, resolved["starts_at"], resolved["ends_at"]) == 0


def test_known_closed_window_and_unknown_future_are_distinct():
    resolved = trading()
    with pytest.raises(PersonalAgentError) as exc:
        next_occurrences(resolved, "2026-11-30T22:00:00Z", 1)
    assert exc.value.code == "calendar_coverage_unknown"
    assert (
        next_occurrences(
            resolved,
            "2026-11-28T00:00:00Z",
            1,
            until="2026-11-29T23:59:59-05:00",
        )
        == []
    )
    for function in (count_occurrences, coalesce_occurrences):
        with pytest.raises(PersonalAgentError) as exc:
            function(resolved, resolved["starts_at"], "2026-12-01T00:00:00-05:00")
        assert exc.value.code == "calendar_coverage_unknown"
    ended = trading(ends_at="2026-11-30T23:59:59-05:00")
    assert next_occurrences(ended, "2026-11-30T22:00:00Z", 1) == []


def test_xshg_unknown_2027_and_precomputed_precoverage_are_rejected():
    for start, end in [("2027-01-01", "2027-01-31"), ("1990-01-01", "1990-01-31")]:
        with pytest.raises(PersonalAgentError) as exc:
            build_calendar_snapshot("XSHG", start, end)
        assert exc.value.code == "calendar_coverage_unknown"


@pytest.mark.parametrize(
    "changes",
    [
        {"calendar_id": "NOT_AN_EXCHANGE"},
        {"coverage_start": "2026-02-30"},
        {"coverage_end": "2026-11-01"},
        {"at": "auction"},
        {"at": "close", "local_time": "12:00"},
        {"at": "time", "local_time": "25:00"},
        {"offset_minutes": True},
        {"offset_minutes": 10081},
        {"starts_at": "2026-11-23T00:00:00-05:00"},
    ],
)
def test_trading_invalid_or_incomplete_input_fails(changes):
    with pytest.raises(PersonalAgentError):
        trading(**changes)


def test_snapshot_limits_fail_instead_of_truncating_sessions():
    with pytest.raises(PersonalAgentError) as exc:
        build_calendar_snapshot("XNYS", "2020-01-01", "2026-01-01")
    assert exc.value.code == "calendar_query_limit"
    evidence, sessions = build_calendar_snapshot("XNYS", "2026-11-24", "2026-11-30")
    with pytest.raises(PersonalAgentError) as exc:
        validate_calendar_snapshot(evidence, sessions * 2000)
    assert exc.value.code == "calendar_query_limit"


def test_snapshot_tampering_cannot_be_hidden_by_current_provider():
    resolved = trading()
    changed = copy.deepcopy(resolved)
    changed["calendar_sessions"][0]["open"] = "2026-11-24T14:31:00.000000+00:00"
    with pytest.raises(PersonalAgentError) as exc:
        validate_schedule(changed)
    assert exc.value.code == "calendar_evidence_mismatch"
    changed = copy.deepcopy(resolved)
    changed["coverage_end"] = "2026-12-01"
    with pytest.raises(PersonalAgentError) as exc:
        validate_schedule(changed)
    assert exc.value.code == "calendar_evidence_mismatch"


def test_persisted_snapshot_reopens_without_importing_new_provider(monkeypatch):
    resolved = trading()
    original = copy.deepcopy(resolved)
    monkeypatch.setitem(sys.modules, "exchange_calendars", None)
    reopened = validate_schedule(json.loads(json.dumps(resolved)))
    assert next_occurrences(reopened, "2026-11-26T00:00:00Z", 1)[0]["due_at"] == (
        "2026-11-27T18:00:00.000000+00:00"
    )
    reopened["calendar_sessions"][0]["date"] = "2000-01-01"
    assert resolved == original
    with pytest.raises(PersonalAgentError) as exc:
        build_calendar_snapshot("XNYS", "2026-11-24", "2026-11-30")
    assert exc.value.code == "calendar_provider_missing"


def test_available_calendar_metadata_is_real_and_does_not_claim_realtime():
    values = {value["id"]: value for value in available_calendars(NOW)}
    assert set(values) == {"XSHG", "XNYS", "XHKG", "XLON", "XTKS"}
    assert values["XSHG"]["coverage_start"] == "1991-01-01"
    assert values["XSHG"]["coverage_end"] == "2026-12-31"
    assert values["XNYS"]["timezone"] == "America/New_York"
    assert all(value["source"] == "versioned_rules_not_realtime" for value in values.values())
    assert all(value["version"].startswith("4.") for value in values.values())


def test_all_provider_calendars_can_be_listed_without_alias_duplication():
    import exchange_calendars as xcals

    values = available_calendars(include_all=True)
    ids = [value["id"] for value in values]
    assert ids == xcals.get_calendar_names(include_aliases=False)
    assert len(ids) > 50 and len(set(ids)) == len(ids)
