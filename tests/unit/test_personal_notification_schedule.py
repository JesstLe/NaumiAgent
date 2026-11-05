"""Actual DST, dateline skips, anchored weekdays and continuous quiet coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from naumi_agent.personal import notification_schedule as schedule
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_models import normalize_preferences


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def window(days, start, end, full_day=False):
    return {"weekdays": days, "start": start, "end": end, "full_day": full_day}


def preferences(windows=None, timezone="UTC"):
    return normalize_preferences(
        {
            "enabled": True,
            "timezone": timezone,
            "quiet_hours": [] if windows is None else windows,
            "muted_actor_ids": [],
            "debounce_seconds": 60,
            "max_delay_seconds": 300,
            "bypass_kinds": [],
            "include_existing": False,
        }
    )


def state(windows, now, timezone="UTC"):
    return schedule.quiet_state(preferences(windows, timezone), instant(now))


def test_empty_windows_return_no_quiet_or_release_and_actual_tzif_digest():
    prefs = preferences()
    value = schedule.quiet_state(prefs, instant("2026-11-05T12:00:00Z"))
    assert value == {
        "quiet": False,
        "next_release": None,
        "timezone_sha256": prefs["timezone_sha256"],
        "timezone_data_changed": False,
        "windows": [],
    }


@pytest.mark.parametrize(
    "now,quiet,release",
    [
        ("2026-11-02T21:59:59.999999Z", False, None),
        ("2026-11-02T22:00:00Z", True, "2026-11-03T08:00:00.000000+00:00"),
        ("2026-11-03T07:59:59.999999Z", True, "2026-11-03T08:00:00.000000+00:00"),
        ("2026-11-03T08:00:00Z", False, None),
        ("2026-11-03T23:00:00Z", False, None),
        ("2026-11-09T23:00:00Z", True, "2026-11-10T08:00:00.000000+00:00"),
    ],
)
def test_overnight_window_uses_start_weekday_and_half_open_actual_interval(now, quiet, release):
    value = state([window([0], "22:00", "08:00")], now)
    assert value["quiet"] is quiet and value["next_release"] == release


def test_local_weekday_and_offset_are_not_taken_from_utc_date():
    windows = [window([0], "00:00", "01:00")]
    value = state(windows, "2026-11-01T16:30:00Z", "Asia/Shanghai")
    assert value["quiet"] is True
    assert value["next_release"] == "2026-11-01T17:00:00.000000+00:00"
    assert state(windows, "2026-11-02T00:30:00Z", "Asia/Shanghai")["quiet"] is False


def test_overlapping_and_touching_windows_merge_into_one_real_interval():
    windows = [
        window([0], "22:00", "02:00"),
        window([1], "01:00", "04:00"),
        window([1], "04:00", "08:00"),
    ]
    value = state(windows, "2026-11-03T01:30:00Z")
    assert value["next_release"] == "2026-11-03T08:00:00.000000+00:00"
    matching = [
        entry
        for entry in value["windows"]
        if instant(entry["start_at"]) <= instant("2026-11-03T01:30:00Z") < instant(entry["end_at"])
    ]
    assert matching == [
        {"start_at": "2026-11-02T22:00:00.000000+00:00", "end_at": value["next_release"]}
    ]


def test_full_day_is_anchored_to_start_time_and_not_assumed_midnight():
    windows = [window([0], "08:00", "08:00", True)]
    assert state(windows, "2026-11-02T07:59:59Z")["quiet"] is False
    value = state(windows, "2026-11-03T07:59:59Z")
    assert value["quiet"] is True
    assert value["next_release"] == "2026-11-03T08:00:00.000000+00:00"
    assert state(windows, "2026-11-03T08:00:00Z")["quiet"] is False


def test_new_york_spring_gap_starts_at_exact_first_real_instant():
    windows = [window([6], "02:30", "04:00")]
    assert state(windows, "2026-03-08T06:59:59.999999Z", "America/New_York")["quiet"] is False
    value = state(windows, "2026-03-08T07:00:00Z", "America/New_York")
    assert value["quiet"] is True
    assert value["next_release"] == "2026-03-08T08:00:00.000000+00:00"
    assert any(
        entry["start_at"] == "2026-03-08T07:00:00.000000+00:00" for entry in value["windows"]
    )


def test_gap_end_resolves_to_transition_not_shifted_requested_minute():
    windows = [window([6], "01:30", "02:30")]
    value = state(windows, "2026-03-08T06:45:00Z", "America/New_York")
    assert value["next_release"] == "2026-03-08T07:00:00.000000+00:00"
    assert state(windows, "2026-03-08T07:00:00Z", "America/New_York")["quiet"] is False


def test_wholly_nonexistent_gap_window_has_no_fictional_duration():
    windows = [window([6], "02:10", "02:50")]
    value = state(windows, "2026-03-08T07:10:00Z", "America/New_York")
    assert value["quiet"] is False
    assert not any(entry["start_at"].startswith("2026-03-08") for entry in value["windows"])


@pytest.mark.parametrize("now", ["2026-11-01T05:20:00Z", "2026-11-01T06:40:00Z"])
def test_new_york_fold_covers_earliest_start_through_latest_end(now):
    windows = [window([6], "01:15", "01:45")]
    value = state(windows, now, "America/New_York")
    assert value["quiet"] is True
    assert value["next_release"] == "2026-11-01T06:45:00.000000+00:00"
    assert {
        "start_at": "2026-11-01T05:15:00.000000+00:00",
        "end_at": value["next_release"],
    } in value["windows"]


def test_lord_howe_half_hour_gap_uses_actual_transition():
    windows = [window([6], "02:10", "03:00")]
    value = state(windows, "2026-10-03T15:30:00Z", "Australia/Lord_Howe")
    assert value["quiet"] is True
    assert value["next_release"] == "2026-10-03T16:00:00.000000+00:00"
    assert any(
        entry["start_at"] == "2026-10-03T15:30:00.000000+00:00" for entry in value["windows"]
    )


def test_lord_howe_half_hour_fold_uses_earliest_start_and_latest_end():
    windows = [window([6], "01:40", "01:50")]
    value = state(windows, "2026-04-04T15:15:00Z", "Australia/Lord_Howe")
    assert value["quiet"] is True
    assert value["next_release"] == "2026-04-04T15:20:00.000000+00:00"
    assert {
        "start_at": "2026-04-04T14:40:00.000000+00:00",
        "end_at": value["next_release"],
    } in value["windows"]


def test_midnight_gap_and_full_day_use_real_civil_boundary():
    windows = [window([6], "00:00", "00:00", True)]
    value = state(windows, "2018-11-04T03:00:00Z", "America/Sao_Paulo")
    assert value["quiet"] is True
    assert value["next_release"] == "2018-11-05T02:00:00.000000+00:00"
    selected = next(
        entry for entry in value["windows"] if entry["start_at"].startswith("2018-11-04")
    )
    assert instant(selected["end_at"]) - instant(selected["start_at"]) == timedelta(hours=23)


def test_apia_skipped_friday_does_not_create_a_friday_or_overnight_phantom():
    for windows in (
        [window([4], "00:00", "00:00", True)],
        [window([4], "22:00", "08:00")],
    ):
        value = state(windows, "2011-12-30T12:00:00Z", "Pacific/Apia")
        assert value["quiet"] is False
        assert not any(entry["start_at"].startswith("2011-12-30") for entry in value["windows"])


@pytest.mark.parametrize(
    "windows",
    [
        [window(list(range(7)), "00:00", "00:00", True)],
        [window(list(range(7)), "22:00", "22:00", True)],
        [window(list(range(7)), "00:00", "12:00"), window(list(range(7)), "12:00", "00:00")],
        [window([0, 2, 4, 6], "00:00", "00:00", True), window([1, 3, 5], "00:00", "00:00", True)],
    ],
)
def test_continuous_week_has_no_invented_release(windows):
    for now in ("2026-11-01T01:30:00Z", "2026-11-05T15:00:00Z"):
        value = state(windows, now)
        assert value["quiet"] is True and value["next_release"] is None


def test_single_minute_weekly_gap_is_not_misreported_as_continuous():
    windows = [window([0, 1, 2, 3, 4, 5], "00:00", "00:00", True), window([6], "00:00", "23:59")]
    value = state(windows, "2026-11-02T12:00:00Z")
    assert value["quiet"] is True
    assert value["next_release"] == "2026-11-08T23:59:00.000000+00:00"
    assert state(windows, "2026-11-08T23:59:00Z")["quiet"] is False


@pytest.mark.parametrize("now", ["2011-12-29T12:00:00Z", "2011-01-01T12:00:00Z"])
def test_nominal_full_week_does_not_hide_a_real_future_dateline_skip_gap(now):
    windows = [window(list(range(7)), "22:00", "22:00", True)]
    value = state(windows, now, "Pacific/Apia")
    assert value["quiet"] is True
    assert value["next_release"] == "2011-12-30T10:00:00.000000+00:00"
    assert state(windows, "2011-12-30T10:00:00Z", "Pacific/Apia")["quiet"] is False
    assert state(windows, "2011-12-31T08:00:00Z", "Pacific/Apia")["quiet"] is True


def test_midnight_all_week_coverage_remains_continuous_through_apia_skip():
    windows = [window(list(range(7)), "00:00", "00:00", True)]
    for now in ("2011-01-01T12:00:00Z", "2011-12-29T12:00:00Z", "2011-12-30T10:00:00Z"):
        value = state(windows, now, "Pacific/Apia")
        assert value["quiet"] is True and value["next_release"] is None


def test_timezone_version_drift_is_reported_without_rewriting_persisted_preferences():
    prefs = preferences([window(list(range(7)), "22:00", "08:00")])
    prefs["timezone_sha256"] = "f" * 64
    value = schedule.quiet_state(prefs, instant("2026-11-05T23:00:00Z"))
    assert value["timezone_data_changed"] is True
    assert value["timezone_sha256"] != prefs["timezone_sha256"]
    assert prefs["timezone_sha256"] == "f" * 64
    assert value["quiet"] is True


@pytest.mark.parametrize("now", [None, "2026-11-05T23:00:00Z", datetime(2026, 11, 5), 0])
def test_naive_or_untyped_now_is_not_accepted(now):
    with pytest.raises(PersonalAgentError) as caught:
        schedule.quiet_state(preferences(), now)
    assert caught.value.code == "invalid_input"


def test_now_offset_is_converted_without_modifying_the_original_input():
    now = instant("2026-11-05T23:00:00+08:00")
    value = schedule.quiet_state(preferences([window([3], "14:00", "16:00")]), now)
    assert value["quiet"] is True
    assert now.utcoffset() == timedelta(hours=8)
    assert value["next_release"] == datetime(2026, 11, 5, 16, tzinfo=UTC).isoformat(
        timespec="microseconds"
    )
