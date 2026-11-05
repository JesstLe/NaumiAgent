"""Real IANA civil-time quiet windows with exact gap/fold and weekly coverage rules."""

from __future__ import annotations

import hashlib
import struct
from datetime import UTC, datetime, time, timedelta
from functools import lru_cache
from importlib import resources
from pathlib import Path
from zoneinfo import TZPATH

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_models import validate_preferences
from naumi_agent.personal.routine_schedule import _wall_candidates, _zone


def _iso(instant):
    return instant.astimezone(UTC).isoformat(timespec="microseconds")


def _instant(value):
    try:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError()
        return value.astimezone(UTC)
    except (ValueError, OverflowError):
        raise PersonalAgentError("invalid_input", "勿扰检查需要带时区的当前时间") from None


def _resolve_wall(wall, zone, *, ending=False):
    """Choose the earliest start/latest end, or the actual first post-gap instant."""
    candidates = _wall_candidates(wall, zone, "both")
    if candidates:
        return candidates[-1 if ending else 0][0]
    try:
        lower, upper = sorted(
            wall.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)
        )
        if (
            lower >= upper
            or lower.astimezone(zone).replace(tzinfo=None) >= wall
            or upper.astimezone(zone).replace(tzinfo=None) < wall
        ):
            raise ValueError()
        while upper - lower > timedelta(microseconds=1):
            middle = lower + (upper - lower) // 2
            if middle.astimezone(zone).replace(tzinfo=None) >= wall:
                upper = middle
            else:
                lower = middle
        return upper
    except (ValueError, OverflowError):
        raise PersonalAgentError("invalid_input", "勿扰时间超出了所选时区的有效范围") from None


def _merge(windows):
    merged = []
    for start, end in sorted(windows):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _minutes(clock):
    hour, minute = map(int, clock.split(":"))
    return hour * 60 + minute


def _continuous_week(windows):
    """Prove full recurring civil-week coverage instead of inventing a release date."""
    week = 7 * 24 * 60
    pieces = []
    for window in windows:
        start = _minutes(window["start"])
        end = _minutes(window["end"])
        duration = (end - start) % (24 * 60)
        if window["full_day"]:
            duration = 24 * 60
        for weekday in window["weekdays"]:
            left, right = weekday * 24 * 60 + start, weekday * 24 * 60 + start + duration
            pieces.append((left, min(right, week)))
            if right > week:
                pieces.append((0, right - week))
    merged = _merge(pieces)
    return merged == [(0, week)]


def _actual_windows(windows, zone, first_day, days):
    actual = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        midnight = _resolve_wall(datetime.combine(day, time.min), zone)
        # A wholly skipped civil date owns no window, even an overnight one.
        if midnight.astimezone(zone).date() != day:
            continue
        for window in windows:
            if day.weekday() not in window["weekdays"]:
                continue
            start_wall = datetime.combine(day, time.fromisoformat(window["start"]))
            crosses = window["full_day"] or window["end"] < window["start"]
            end_day = day + timedelta(days=int(crosses))
            end_wall = datetime.combine(end_day, time.fromisoformat(window["end"]))
            actual.append(
                (
                    _resolve_wall(start_wall, zone),
                    _resolve_wall(end_wall, zone, ending=True),
                )
            )
    return _merge(actual)


@lru_cache(maxsize=128)
def _skipped_dates(timezone, timezone_sha256):
    """Read TZif transition instants to avoid a fictitious perpetual quiet claim.

    A weekly plan can cover every minute yet lose a real overnight segment when
    an entire start date is skipped. Such discontinuities are explicit TZif
    transitions, not ordinary recurring POSIX daylight-saving rules. Inspecting
    the transition table avoids scanning years or assuming a one-hour change.
    """
    try:
        data = None
        for root in TZPATH:
            path = Path(root).joinpath(*timezone.split("/"))
            if path.is_file():
                data = path.read_bytes()
                break
        if data is None:
            data = resources.files("tzdata.zoneinfo").joinpath(*timezone.split("/")).read_bytes()
        if hashlib.sha256(data).hexdigest() != timezone_sha256:
            raise PersonalAgentError("timezone_data_changed", "时区资料已更新，请重新核对勿扰设置")

        def header(offset):
            if data[offset : offset + 4] != b"TZif":
                raise ValueError()
            return struct.unpack(">6I", data[offset + 20 : offset + 44])

        counts = header(0)
        size, position = 4, 44
        if data[4:5] in {b"2", b"3", b"4"}:
            gmt, std, leap, transitions, types, chars = counts
            first_size = transitions * 5 + types * 6 + chars + leap * 8 + std + gmt
            position = 44 + first_size
            counts = header(position)
            size, position = 8, position + 44
        transition_count = counts[3]
        times = struct.iter_unpack(
            ">q" if size == 8 else ">l",
            data[position : position + transition_count * size],
        )
        zone, loaded_sha = _zone(timezone)
        if loaded_sha != timezone_sha256:
            raise PersonalAgentError("timezone_data_changed", "时区资料已更新，请重新核对勿扰设置")
        found = []
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        for (stamp,) in times:
            try:
                instant = epoch + timedelta(seconds=stamp)
                before = (instant - timedelta(microseconds=1)).astimezone(zone).date()
                after = instant.astimezone(zone).date()
                if (after - before).days > 1:
                    found.append((instant, after))
            except (ValueError, OverflowError):
                continue
        return tuple(found)
    except (OSError, ValueError, ImportError, UnicodeError, struct.error):
        raise PersonalAgentError(
            "invalid_timezone", "时区资料格式不正确，请重新核对勿扰设置"
        ) from None


def _continuous_release(windows, zone, timezone, timezone_sha256, now, current, horizon):
    """Find actual dateline-skip gaps even beyond the ordinary weekly projection."""
    if current[1] < horizon:
        return current[1]
    for transition, after_day in _skipped_dates(timezone, timezone_sha256):
        if transition < now:
            continue
        surrounding = _actual_windows(windows, zone, after_day - timedelta(days=8), 17)
        for (_, previous_end), (next_start, _) in zip(surrounding, surrounding[1:]):
            if now < previous_end < next_start:
                return previous_end
    return None


def quiet_state(preferences, aware_now):
    """Project bounded real windows; DND changes prompts, never source-result storage.

    Start weekdays refer to the window's start date, including overnight windows.
    Twenty-four civil dates cover a previous weekly chain and two future cycles;
    a non-continuous weekly plan necessarily has its next release in those cycles.
    No UTC twenty-four-hour or fixed one-hour DST assumption is used.
    """
    preferences = validate_preferences(preferences)
    now = _instant(aware_now)
    zone, current_sha = _zone(preferences["timezone"])
    changed = preferences["timezone_sha256"] != current_sha
    windows = preferences["quiet_hours"]
    result = {
        "quiet": False,
        "next_release": None,
        "timezone_sha256": current_sha,
        "timezone_data_changed": changed,
        "windows": [],
    }
    if not windows:
        return result
    try:
        local_day = now.astimezone(zone).date()
        merged = _actual_windows(windows, zone, local_day - timedelta(days=8), 24)
        result["windows"] = [
            {"start_at": _iso(start), "end_at": _iso(end)} for start, end in merged
        ]
        current = next(((start, end) for start, end in merged if start <= now < end), None)
        if current is not None:
            result["quiet"] = True
            release = current[1]
            if _continuous_week(windows):
                horizon = _resolve_wall(
                    datetime.combine(local_day + timedelta(days=14), time.min), zone
                )
                release = _continuous_release(
                    windows, zone, preferences["timezone"], current_sha, now, current, horizon
                )
            result["next_release"] = None if release is None else _iso(release)
        return result
    except (ValueError, OverflowError):
        raise PersonalAgentError("invalid_input", "当前时间超出了勿扰日期的有效范围") from None
