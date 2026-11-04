"""Independent v1 personal-routine calendars with frozen evidence and bounded queries."""

from __future__ import annotations

import hashlib
import io
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from importlib import resources
from pathlib import Path
from zoneinfo import TZPATH, ZoneInfo

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.trading_calendar import available_calendars as available_calendars
from naumi_agent.personal.trading_calendar import (
    build_calendar_snapshot,
    validate_calendar_snapshot,
)

MAX_QUERY_DAYS = 2936
MAX_OCCURRENCES = 1000
MAX_COUNT = 100_000
_COMMON_INPUT = {"kind", "timezone", "starts_at", "ends_at", "dst_gap", "dst_fold"}
_COMMON_RESOLVED = _COMMON_INPUT | {"version", "timezone_sha256"}
_ONCE_INPUT = {"local_time", "fold"}
_CRON_INPUT = {"expression"}
_TRADING_INPUT = {
    "calendar_id",
    "coverage_start",
    "coverage_end",
    "at",
    "local_time",
    "offset_minutes",
}
_TRADING_RESOLVED = _TRADING_INPUT | {"calendar_evidence", "calendar_sessions"}
_KINDS = {"once", "cron", "trading_day"}


def _error(code, message):
    return PersonalAgentError(code, message)


def _integer(value, label, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise _error("invalid_schedule", f"{label}需要 {lower} 到 {upper} 的整数")
    return value


def _choice(value, options, label):
    if not isinstance(value, str) or value not in options:
        raise _error("invalid_schedule", f"{label}不是有效选项")
    return value


def _instant(value, label="计划时间"):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _error("invalid_schedule", f"{label}需要带时区的完整时间") from exc
    else:
        raise _error("invalid_schedule", f"{label}需要带时区的完整时间")
    try:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("naive schedule timestamp")
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise _error("invalid_schedule", f"{label}需要带时区的完整时间") from exc


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _in_zone(value, zone):
    try:
        return value.astimezone(zone)
    except (ValueError, OverflowError) as exc:
        raise _error("invalid_schedule", "该时间超出了所选时区可表示的日期范围") from exc


def _local(value, *, clock=False):
    if not isinstance(value, str) or len(value) > 64:
        raise _error("invalid_schedule", "请提供完整的本地时间")
    try:
        if clock:
            if re.fullmatch(r"[0-2]\d:[0-5]\d", value) is None:
                raise ValueError("expected HH:MM")
            result = time.fromisoformat(value)
        else:
            if "T" not in value and " " not in value:
                raise ValueError("missing time")
            result = datetime.fromisoformat(value)
        if result.tzinfo is not None:
            raise ValueError("local time carries offset")
        return result
    except ValueError as exc:
        raise _error("invalid_schedule", "本地时间与 IANA 时区需要分别提供") from exc


@lru_cache(maxsize=128)
def _zone_from_bytes(name, data):
    return ZoneInfo.from_file(io.BytesIO(data), key=name)


def _zone(name):
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 128
        or name.startswith(("/", "\\"))
        or re.fullmatch(r"[A-Za-z0-9_+./-]+", name) is None
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise _error("invalid_timezone", "请选择有效的 IANA 时区")
    try:
        data = None
        for root in TZPATH:
            path = Path(root).joinpath(*name.split("/"))
            if path.is_file():
                data = path.read_bytes()
                break
        if data is None:
            data = resources.files("tzdata.zoneinfo").joinpath(*name.split("/")).read_bytes()
        zone = _zone_from_bytes(name, data)
        return zone, hashlib.sha256(data).hexdigest()
    except (OSError, ValueError, ImportError, UnicodeError) as exc:
        raise _error("invalid_timezone", "请选择有效的 IANA 时区") from exc


def _wall_candidates(local, zone, fold_policy):
    found = {}
    for fold in (0, 1):
        try:
            utc = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            back = utc.astimezone(zone)
        except (ValueError, OverflowError):
            continue
        if back.replace(tzinfo=None) == local:
            found[utc] = back.fold
    ordered = sorted(found.items())
    if len(ordered) > 1 and fold_policy != "both":
        ordered = [ordered[0 if fold_policy == "first" else -1]]
    return ordered


def _field(raw, minimum, maximum):
    if not raw or re.fullmatch(r"[0-9*/,-]+", raw) is None:
        raise _error("invalid_schedule", "Cron 字段只接受数字、星号、列表、范围与步长")
    values = set()
    for part in raw.split(","):
        if not part or part.count("/") > 1:
            raise _error("invalid_schedule", "Cron 列表或步长格式无效")
        base, slash, step_raw = part.partition("/")
        if slash and (not step_raw.isascii() or not step_raw.isdigit()):
            raise _error("invalid_schedule", "Cron 步长需要正整数")
        step = int(step_raw) if slash else 1
        if step <= 0:
            raise _error("invalid_schedule", "Cron 步长需要大于零")
        if base == "*":
            first, last = minimum, maximum
        elif "-" in base:
            if base.count("-") != 1:
                raise _error("invalid_schedule", "Cron 范围格式无效")
            left, right = base.split("-")
            if not left.isdigit() or not right.isdigit():
                raise _error("invalid_schedule", "Cron 范围需要两个整数")
            first, last = int(left), int(right)
        else:
            if not base.isdigit():
                raise _error("invalid_schedule", "Cron 字段需要整数")
            first = int(base)
            last = maximum if slash else first
        if not minimum <= first <= last <= maximum:
            raise _error("invalid_schedule", "Cron 字段超出取值范围或使用了倒序范围")
        values.update(range(first, last + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class _Cron:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_star: bool
    weekday_star: bool

    def matches_date(self, day):
        if day.month not in self.months:
            return False
        dom, dow = day.day in self.days, (day.weekday() + 1) % 7 in self.weekdays
        if self.day_star:
            return dow
        if self.weekday_star:
            return dom
        return dom or dow


@lru_cache(maxsize=256)
def _cron(expression):
    if not isinstance(expression, str) or len(expression) > 256:
        raise _error("invalid_schedule", "Cron 表达式需要不超过 256 字的文字")
    fields = expression.strip().split()
    if len(fields) != 5:
        raise _error("invalid_schedule", "Cron 表达式需要 5 段：分 时 日 月 周")
    minute, hour, day, month, weekday = fields
    result = _Cron(
        tuple(sorted(_field(minute, 0, 59))),
        tuple(sorted(_field(hour, 0, 23))),
        _field(day, 1, 31),
        _field(month, 1, 12),
        frozenset(value % 7 for value in _field(weekday, 0, 7)),
        day == "*",
        weekday == "*",
    )
    if result.weekday_star and not any(
        d <= monthrange(2000, m)[1] for m in result.months for d in result.days
    ):
        raise _error("invalid_schedule", "Cron 在所选月份没有实际存在的日期")
    return result


def normalize_schedule(value, now):
    """Resolve new input once; persist this detached version instead of resampling now."""
    if not isinstance(value, dict):
        raise _error("invalid_schedule", "请提供结构化的计划时间")
    kind = _choice(value.get("kind"), _KINDS, "计划类型")
    extras = {"once": _ONCE_INPUT, "cron": _CRON_INPUT, "trading_day": _TRADING_INPUT}[kind]
    if set(value) - (_COMMON_INPUT | extras):
        raise _error("invalid_schedule", "计划时间包含未知字段")
    now = _instant(now, "当前时钟")
    name = value.get("timezone", "Asia/Shanghai")
    zone, zone_sha = _zone(name)
    start = _instant(value.get("starts_at", now), "计划开始")
    end = None if value.get("ends_at") is None else _instant(value["ends_at"], "计划结束")
    if end is not None and end < start:
        raise _error("invalid_schedule", "计划结束需要晚于或等于开始")
    result = {
        "version": 1,
        "kind": kind,
        "timezone": name,
        "timezone_sha256": zone_sha,
        "starts_at": _iso(start),
        "ends_at": None if end is None else _iso(end),
        "dst_gap": _choice(value.get("dst_gap", "skip"), {"skip"}, "夏令时跳过策略"),
        "dst_fold": _choice(
            value.get("dst_fold", "first"),
            {"first", "second", "both"},
            "夏令时重叠策略",
        ),
    }
    if kind == "once":
        local = _local(value.get("local_time"))
        fold = value.get("fold")
        if fold is not None:
            _integer(fold, "重叠时刻选择", 0, 1)
        choices = _wall_candidates(local, zone, "both")
        if not choices:
            raise _error("nonexistent_time", "夏令时跳过了该时刻，请选择其他时间")
        if len(choices) > 1 and fold is None:
            raise _error("ambiguous_time", "该时刻出现两次，请明确选择 fold 0 或 1")
        selected = next((pair for pair in choices if pair[1] == (fold or 0)), None)
        if selected is None:
            raise _error("invalid_schedule", "该本地时刻没有所选 fold")
        if selected[0] < start or end is not None and selected[0] > end:
            raise _error("invalid_schedule", "一次性触发时刻需要位于计划开始与结束之间")
        result.update(local_time=local.isoformat(), fold=selected[1], due_at=_iso(selected[0]))
    elif kind == "cron":
        expression = value.get("expression")
        if not isinstance(expression, str):
            raise _error("invalid_schedule", "请填写 5 段 Cron 表达式")
        expression = " ".join(expression.split())
        _cron(expression)
        result["expression"] = expression
    else:
        evidence, sessions = build_calendar_snapshot(
            value.get("calendar_id"),
            value.get("coverage_start"),
            value.get("coverage_end"),
        )
        at = _choice(value.get("at", "time"), {"open", "close", "time"}, "交易日触发位置")
        local_time = value.get("local_time")
        if at == "time":
            local_time = _local(local_time, clock=True).isoformat(timespec="minutes")
        elif local_time is not None:
            raise _error("invalid_schedule", "按开闭市触发时请勿同时填写本地时刻")
        result.update(
            calendar_id=evidence["calendar_id"],
            coverage_start=evidence["coverage_start"],
            coverage_end=evidence["coverage_end"],
            at=at,
            local_time=local_time,
            offset_minutes=_integer(value.get("offset_minutes", 0), "交易时刻偏移", -10080, 10080),
            calendar_evidence=evidence,
            calendar_sessions=sessions,
        )
        if (
            not evidence["coverage_start"]
            <= _in_zone(start, zone).date().isoformat()
            <= (evidence["coverage_end"])
        ):
            raise _error("calendar_coverage_unknown", "计划开始日期超出本次交易日历覆盖")
    return validate_schedule(result)


def validate_schedule(value):
    """Verify a persisted v1 calendar and detach every nested caller-owned object."""
    if not isinstance(value, dict):
        raise _error("invalid_schedule", "原计划时间不是完整对象")
    kind = _choice(value.get("kind"), _KINDS, "计划类型")
    extras = {
        "once": _ONCE_INPUT | {"due_at"},
        "cron": _CRON_INPUT,
        "trading_day": _TRADING_RESOLVED,
    }[kind]
    if (
        set(value) != _COMMON_RESOLVED | extras
        or type(value["version"]) is not int
        or (value["version"] != 1)
    ):
        raise _error("invalid_schedule", "原计划的版本或字段不完整")
    zone, zone_sha = _zone(value["timezone"])
    if value["timezone_sha256"] != zone_sha:
        raise _error(
            "calendar_timezone_changed",
            "IANA 时区规则与原计划不同，请核对后创建新的计划修订",
        )
    _choice(value["dst_gap"], {"skip"}, "夏令时跳过策略")
    _choice(value["dst_fold"], {"first", "second", "both"}, "夏令时重叠策略")
    start = _instant(value["starts_at"], "计划开始")
    end = None if value["ends_at"] is None else _instant(value["ends_at"], "计划结束")
    if (
        value["starts_at"] != _iso(start)
        or end is not None
        and (value["ends_at"] != _iso(end) or end < start)
    ):
        raise _error("invalid_schedule", "原计划开始与结束不是规范 UTC 时间")
    if kind == "once":
        local = _local(value["local_time"])
        fold = _integer(value["fold"], "重叠时刻选择", 0, 1)
        due = _instant(value["due_at"])
        if (
            value["due_at"] != _iso(due)
            or value["local_time"] != local.isoformat()
            or (due, fold) not in _wall_candidates(local, zone, "both")
            or due < start
            or end is not None
            and due > end
        ):
            raise _error("invalid_schedule", "原单次计划的本地时刻与 UTC 不一致")
    elif kind == "cron":
        if not isinstance(value["expression"], str):
            raise _error("invalid_schedule", "原 Cron 表达式需要文字")
        _cron(value["expression"])
        if value["expression"] != " ".join(value["expression"].split()):
            raise _error("invalid_schedule", "原 Cron 表达式不是规范格式")
    else:
        evidence, sessions = validate_calendar_snapshot(
            value["calendar_evidence"],
            value["calendar_sessions"],
        )
        if any(
            value[key] != evidence[key]
            for key in (
                "calendar_id",
                "coverage_start",
                "coverage_end",
            )
        ):
            raise _error("calendar_evidence_mismatch", "计划与原交易日历覆盖不一致")
        at = _choice(value["at"], {"open", "close", "time"}, "交易日触发位置")
        _integer(value["offset_minutes"], "交易时刻偏移", -10080, 10080)
        if at == "time":
            _local(value["local_time"], clock=True)
        elif value["local_time"] is not None:
            raise _error("invalid_schedule", "开闭市计划不接受额外本地时刻")
        if (
            not value["coverage_start"]
            <= _in_zone(start, zone).date().isoformat()
            <= (value["coverage_end"])
        ):
            raise _error("calendar_coverage_unknown", "计划开始日期超出原交易日历覆盖")
        return dict(value) | {"calendar_evidence": evidence, "calendar_sessions": sessions}
    return dict(value)


def _occurrence(instant, zone, evidence=None):
    local = _in_zone(instant, zone)
    return {
        "due_at": _iso(instant),
        "local_time": local.replace(tzinfo=None).isoformat(),
        "fold": local.fold,
        "calendar_evidence": None if evidence is None else dict(evidence),
    }


def _bounds(resolved, after, until):
    after = _instant(after, "查询起点")
    start = _instant(resolved["starts_at"])
    end = None if resolved["ends_at"] is None else _instant(resolved["ends_at"])
    requested = None if until is None else _instant(until, "查询终点")
    effective = max(after, start)
    if requested is not None and requested < after:
        raise _error("invalid_schedule", "查询终点需要晚于或等于起点")
    if requested is not None and requested - effective > timedelta(days=MAX_QUERY_DAYS):
        raise _error("calendar_query_limit", "一次日历查询最多覆盖 2936 天，请按原游标分段查询")
    try:
        horizon = effective + timedelta(days=MAX_QUERY_DAYS)
    except OverflowError:
        horizon = datetime.max.replace(tzinfo=UTC)
    upper = min(t for t in (horizon, requested, end) if t is not None)
    return after, start, upper, requested, end


def _iter_cron(resolved, after, start, upper, zone):
    cron = _cron(resolved["expression"])
    lower = max(after, start)
    day = _in_zone(lower, zone).date()
    last = _in_zone(upper, zone).date()
    # One prior local day covers historical backward date-line changes as well as folds.
    if day > date.min:
        day -= timedelta(days=1)
    while day <= last:
        if cron.matches_date(day):
            candidates = []
            for hour in cron.hours:
                for minute in cron.minutes:
                    wall = datetime.combine(day, time(hour, minute))
                    candidates.extend(_wall_candidates(wall, zone, resolved["dst_fold"]))
            for instant, _ in sorted(candidates):
                if after < instant and start <= instant <= upper:
                    yield _occurrence(instant, zone)
        if day == date.max:
            break
        day += timedelta(days=1)


def _trading_candidates(resolved, zone):
    offset = timedelta(minutes=resolved["offset_minutes"])
    values = []
    for session in resolved["calendar_sessions"]:
        evidence = dict(resolved["calendar_evidence"]) | {"session_date": session["date"]}
        if resolved["at"] == "time":
            local = datetime.combine(
                date.fromisoformat(session["date"]),
                _local(
                    resolved["local_time"],
                    clock=True,
                ),
            )
            instants = _wall_candidates(local, zone, resolved["dst_fold"])
        else:
            instants = [(_instant(session[resolved["at"]]), 0)]
        for instant, _ in instants:
            values.append(_occurrence(instant + offset, zone, evidence))
    return sorted(values, key=lambda value: value["due_at"])


def _iter_occurrences(resolved, after, until=None, *, complete=False):
    after, start, upper, requested, end = _bounds(resolved, after, until)
    if upper < start or upper <= after:
        return
    zone, _ = _zone(resolved["timezone"])
    if resolved["kind"] == "once":
        instant = _instant(resolved["due_at"])
        if after < instant and start <= instant <= upper:
            yield _occurrence(instant, zone)
        return
    if resolved["kind"] == "cron":
        yield from _iter_cron(resolved, after, start, upper, zone)
        return
    # Coverage is in session labels, not first/last *open* dates; holidays are evidence too.
    coverage_end = date.fromisoformat(resolved["coverage_end"])
    coverage_limit = datetime.combine(coverage_end, time.max, tzinfo=zone).astimezone(UTC)
    coverage_limit += timedelta(minutes=min(resolved["offset_minutes"], 0))
    if complete and upper > coverage_limit:
        raise _error("calendar_coverage_unknown", "查询区间超过原交易日历覆盖，请先更新日历版本")
    for occurrence in _trading_candidates(resolved, zone):
        instant = _instant(occurrence["due_at"])
        if after < instant and start <= instant <= upper:
            yield occurrence
    if (
        upper > coverage_limit
        and (end is None or end > coverage_limit)
        and (requested is None or requested > coverage_limit)
    ):
        raise _error("calendar_coverage_unknown", "后续日期尚无交易日历凭据，请先更新日历版本")


def next_occurrences(resolved, after, limit, until=None):
    """Return at most ``limit`` stable nominal UTC instants in (after, until]."""
    resolved = validate_schedule(resolved)
    _integer(limit, "发生次数", 1, MAX_OCCURRENCES)
    output = []
    for occurrence in _iter_occurrences(resolved, after, until):
        output.append(occurrence)
        if len(output) == limit:
            break
    return output


def count_occurrences(resolved, after, until, *, max_count=MAX_COUNT):
    """Return an exact bounded count, or fail rather than report a truncated count."""
    resolved = validate_schedule(resolved)
    _integer(max_count, "计数容量", 0, MAX_COUNT)
    if until is None:
        raise _error("invalid_schedule", "精确计数需要明确查询终点")
    count = 0
    for _ in _iter_occurrences(resolved, after, until, complete=True):
        count += 1
        if count > max_count:
            raise _error("calendar_query_limit", "发生次数超过本次计数容量，请按原游标分段处理")
    return count


def coalesce_occurrences(resolved, after, until, *, max_count=MAX_COUNT):
    """Select the latest *original* nominal occurrence and retain an exact merged count."""
    resolved = validate_schedule(resolved)
    _integer(max_count, "合并计数容量", 0, MAX_COUNT)
    if until is None:
        raise _error("invalid_schedule", "合并查询需要明确终点")
    count, latest = 0, None
    for occurrence in _iter_occurrences(resolved, after, until, complete=True):
        count += 1
        if count > max_count:
            raise _error("calendar_query_limit", "合并次数超过本次容量，请按原游标分段处理")
        latest = occurrence
    return {"occurrence": latest, "count": count}


def catch_up_occurrences(resolved, after, until, limit):
    """Page chronological original occurrences; never relabel missed work as now."""
    resolved = validate_schedule(resolved)
    _integer(limit, "补跑次数", 1, MAX_OCCURRENCES)
    if until is None:
        raise _error("invalid_schedule", "补跑查询需要明确终点")
    output, has_more = [], False
    for occurrence in _iter_occurrences(resolved, after, until, complete=True):
        if len(output) == limit:
            has_more = True
            break
        output.append(occurrence)
    return {
        "occurrences": output,
        "has_more": has_more,
        "resume_after": output[-1]["due_at"] if output else _iso(_instant(after)),
    }
