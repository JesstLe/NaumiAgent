"""Versioned exchange-calendar snapshots; never substitute weekdays for sessions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version

from naumi_agent.personal.models import PersonalAgentError

MAX_COVERAGE_DAYS = 1831
MAX_SESSIONS = 5000
_EVIDENCE_FIELDS = {
    "provider",
    "provider_version",
    "calendar_id",
    "timezone",
    "coverage_start",
    "coverage_end",
    "source",
    "sha256",
}
_SESSION_FIELDS = {
    "date",
    "open",
    "close",
    "break_start",
    "break_end",
    "early_close",
    "late_open",
}


def _error(code: str, message: str):
    return PersonalAgentError(code, message)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(evidence, sessions) -> str:
    metadata = {key: value for key, value in evidence.items() if key != "sha256"}
    payload = _canonical({"evidence": metadata, "sessions": sessions}).encode()
    return hashlib.sha256(payload).hexdigest()


def _date(value, label):
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise _error("invalid_schedule", f"{label}需要完整的年、月、日")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _error("invalid_schedule", f"{label}不是有效日期") from exc


def _range(start, end):
    first, last = _date(start, "日历覆盖开始"), _date(end, "日历覆盖结束")
    if first > last:
        raise _error("invalid_schedule", "日历覆盖结束需要晚于或等于开始")
    if (last - first).days + 1 > MAX_COVERAGE_DAYS:
        raise _error("calendar_query_limit", "一次交易日历快照最多覆盖 1831 天，请分段更新计划")
    return first, last


def _coverage_bounds(calendar):
    from exchange_calendars.precomputed_exchange_calendar import PrecomputedExchangeCalendar

    lower, upper = calendar.bound_min(), calendar.bound_max()
    if isinstance(calendar, PrecomputedExchangeCalendar):
        # Provider subclasses intersect multiple holiday tables (for example XHKG).
        # XSHG's bound_min predates its actual table; use both sources, not its stale docstring.
        first = date(calendar._earliest_precomputed_year(), 1, 1)
        last = date(calendar._latest_precomputed_year(), 12, 31)
        return max(first, lower.date()) if lower is not None else first, (
            min(last, upper.date()) if upper is not None else last
        )
    return lower.date() if lower is not None else None, (
        upper.date() if upper is not None else None
    )


def _instant(value):
    if not isinstance(value, str) or len(value) > 64:
        raise _error("invalid_schedule", "交易时刻需要带时区的完整时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("naive exchange instant")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as exc:
        raise _error("invalid_schedule", "交易时刻需要带时区的完整时间") from exc


def validate_calendar_snapshot(evidence, sessions):
    """Detach and verify the original snapshot without importing the current provider."""
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_FIELDS:
        raise _error("invalid_schedule", "交易日历凭据缺少原始字段")
    release = evidence["provider_version"]
    release_match = re.fullmatch(r"4\.(\d+)\.\d+", release) if isinstance(release, str) else None
    if (
        evidence["provider"] != "exchange_calendars"
        or release_match is None
        or int(release_match[1]) < 11
        or evidence["source"] != "versioned_rules_not_realtime"
        or not isinstance(evidence["calendar_id"], str)
        or not 1 <= len(evidence["calendar_id"]) <= 32
        or not isinstance(evidence["timezone"], str)
        or not 1 <= len(evidence["timezone"]) <= 128
        or not isinstance(evidence["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"]) is None
    ):
        raise _error("invalid_schedule", "交易日历的来源、版本或摘要不完整")
    first, last = _range(evidence["coverage_start"], evidence["coverage_end"])
    if not isinstance(sessions, list) or len(sessions) > MAX_SESSIONS:
        raise _error("calendar_query_limit", "交易日历会话数量超出 5000 条容量")
    normalized, previous = [], None
    for session in sessions:
        if not isinstance(session, dict) or set(session) != _SESSION_FIELDS:
            raise _error("invalid_schedule", "交易会话缺少原始开闭市凭据")
        day = _date(session["date"], "交易会话日期")
        if not first <= day <= last or previous is not None and day <= previous:
            raise _error("invalid_schedule", "交易会话超出覆盖、重复或顺序不一致")
        if type(session["early_close"]) is not bool or type(session["late_open"]) is not bool:
            raise _error("invalid_schedule", "特殊交易会话标记无效")
        item = dict(session)
        for field in ("open", "close", "break_start", "break_end"):
            item[field] = None if session[field] is None else _instant(session[field])
        if item["open"] is None or item["close"] is None or item["open"] >= item["close"]:
            raise _error("invalid_schedule", "交易会话开闭市顺序无效")
        start, end = item["break_start"], item["break_end"]
        if (
            (start is None) != (end is None)
            or start is not None
            and not (item["open"] <= start < end <= item["close"])
        ):
            raise _error("invalid_schedule", "交易会话午间休市顺序无效")
        normalized.append(item)
        previous = day
    detached = dict(evidence)
    if _digest(detached, normalized) != evidence["sha256"]:
        raise _error("calendar_evidence_mismatch", "交易日历快照与原始摘要不一致，请保留原版本核对")
    return detached, normalized


def build_calendar_snapshot(calendar_id, coverage_start, coverage_end):
    """Materialize real exchange sessions from a bounded, versioned library calendar."""
    first, last = _range(coverage_start, coverage_end)
    if not isinstance(calendar_id, str) or not 1 <= len(calendar_id) <= 32:
        raise _error("invalid_schedule", "请选择有效的交易所日历编号")
    try:
        import exchange_calendars as xcals
        import pandas as pd

        provider_version = version("exchange-calendars")
    except (ImportError, PackageNotFoundError) as exc:
        raise _error(
            "calendar_provider_missing",
            "交易日历组件尚未安装，请恢复项目依赖后继续",
        ) from exc
    try:
        if calendar_id not in xcals.get_calendar_names():
            raise _error("invalid_schedule", "该交易所日历不存在，请选择组件中真实可用的日历")
        prototype = xcals.get_calendar(calendar_id)
        bound_min, bound_max = _coverage_bounds(prototype)
        if (
            bound_min is not None
            and first < bound_min
            or bound_max is not None
            and last > bound_max
        ):
            raise _error(
                "calendar_coverage_unknown",
                "交易日历尚未覆盖所选日期，请先取得该日期的日历版本",
            )
        # Padding permits an all-closed requested window, including a single holiday.
        lower, upper = first - timedelta(days=32), last + timedelta(days=32)
        if bound_min is not None:
            lower = max(lower, bound_min)
        if bound_max is not None:
            upper = min(upper, bound_max)
        calendar = xcals.get_calendar(calendar_id, start=lower.isoformat(), end=upper.isoformat())
        schedule = calendar.schedule.loc[coverage_start:coverage_end]
        sessions = []
        for label, row in schedule.iterrows():
            item = {"date": label.date().isoformat()}
            for field in ("open", "close", "break_start", "break_end"):
                value = row[field]
                item[field] = None if pd.isna(value) else _instant(value.isoformat())
            item["early_close"] = label in calendar.early_closes
            item["late_open"] = label in calendar.late_opens
            sessions.append(item)
        evidence = {
            "provider": "exchange_calendars",
            "provider_version": provider_version,
            "calendar_id": calendar.name,
            "timezone": str(calendar.tz),
            "coverage_start": first.isoformat(),
            "coverage_end": last.isoformat(),
            "source": "versioned_rules_not_realtime",
        }
        evidence["sha256"] = _digest(evidence, sessions)
        return validate_calendar_snapshot(evidence, sessions)
    except PersonalAgentError:
        raise
    except (ValueError, OverflowError, KeyError, TypeError) as exc:
        raise _error(
            "calendar_coverage_unknown",
            "当前交易日历组件没有完整覆盖该区间，请核对日历范围和版本",
        ) from exc


def available_calendars(now=None, *, include_all=False):
    """Read actual provider metadata, without generating a requested-range snapshot."""
    if now is not None and (
        not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
    ):
        raise _error("invalid_schedule", "当前时钟需要带时区的完整时间")
    if type(include_all) is not bool:
        raise _error("invalid_schedule", "交易日历列表选项无效")
    try:
        import exchange_calendars as xcals

        provider_version = version("exchange-calendars")
    except (ImportError, PackageNotFoundError) as exc:
        raise _error(
            "calendar_provider_missing",
            "交易日历组件尚未安装，请恢复项目依赖后继续",
        ) from exc
    names = (
        xcals.get_calendar_names(include_aliases=False)
        if include_all
        else [
            "XSHG",
            "XNYS",
            "XHKG",
            "XLON",
            "XTKS",
        ]
    )
    display = {
        "XSHG": "上海证券交易所",
        "XNYS": "纽约证券交易所",
        "XHKG": "香港交易所",
        "XLON": "伦敦证券交易所",
        "XTKS": "东京证券交易所",
    }
    output = []
    for name in names:
        calendar = xcals.get_calendar(name)
        lower, upper = _coverage_bounds(calendar)
        output.append(
            {
                "id": name,
                "name": display.get(name, name),
                "version": provider_version,
                "coverage_start": (lower or calendar.default_start().date()).isoformat(),
                "coverage_end": (upper or calendar.default_end().date()).isoformat(),
                "timezone": str(calendar.tz),
                "source": "versioned_rules_not_realtime",
            }
        )
    return output
