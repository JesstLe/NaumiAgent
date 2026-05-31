"""Small strict five-field cron parser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_MAX_SEARCH_MINUTES = 366 * 24 * 60


@dataclass(frozen=True)
class CronSchedule:
    """Parsed five-field cron expression."""

    expression: str
    minutes: set[int]
    hours: set[int]
    days: set[int]
    months: set[int]
    weekdays: set[int]

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        fields = expression.strip().split()
        if len(fields) != 5:
            raise ValueError("cron 表达式必须是 5 段：分 时 日 月 周")
        minute, hour, day, month, weekday = fields
        return cls(
            expression=expression.strip(),
            minutes=_parse_field(minute, minimum=0, maximum=59, name="分"),
            hours=_parse_field(hour, minimum=0, maximum=23, name="时"),
            days=_parse_field(day, minimum=1, maximum=31, name="日"),
            months=_parse_field(month, minimum=1, maximum=12, name="月"),
            weekdays=_parse_weekday_field(weekday),
        )

    def next_after(self, after: datetime) -> datetime:
        """Return the first matching minute strictly after ``after``."""
        candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(_MAX_SEARCH_MINUTES):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("无法在未来 366 天内找到下一次 cron 触发时间")

    def matches(self, value: datetime) -> bool:
        cron_weekday = (value.weekday() + 1) % 7
        return (
            value.minute in self.minutes
            and value.hour in self.hours
            and value.day in self.days
            and value.month in self.months
            and cron_weekday in self.weekdays
        )


def _parse_weekday_field(raw: str) -> set[int]:
    values = _parse_field(raw, minimum=0, maximum=7, name="周")
    normalized = {0 if value == 7 else value for value in values}
    return normalized


def _parse_field(raw: str, *, minimum: int, maximum: int, name: str) -> set[int]:
    raw = raw.strip()
    if not raw:
        raise ValueError(f"cron 的{name}字段不能为空")

    values: set[int] = set()
    for part in raw.split(","):
        values.update(_parse_part(part.strip(), minimum=minimum, maximum=maximum, name=name))
    if not values:
        raise ValueError(f"cron 的{name}字段没有可用取值")
    return values


def _parse_part(raw: str, *, minimum: int, maximum: int, name: str) -> set[int]:
    if not raw:
        raise ValueError(f"cron 的{name}字段包含空片段")

    base = raw
    step = 1
    if "/" in raw:
        base, step_raw = raw.split("/", 1)
        if not step_raw.isdigit():
            raise ValueError(f"cron 的{name}字段步长必须是正整数")
        step = int(step_raw)
        if step <= 0:
            raise ValueError(f"cron 的{name}字段步长必须大于 0")

    if base == "*":
        start, end = minimum, maximum
    elif "-" in base:
        start_raw, end_raw = base.split("-", 1)
        start = _parse_int(start_raw, minimum=minimum, maximum=maximum, name=name)
        end = _parse_int(end_raw, minimum=minimum, maximum=maximum, name=name)
        if start > end:
            raise ValueError(f"cron 的{name}字段范围不能倒序")
    else:
        value = _parse_int(base, minimum=minimum, maximum=maximum, name=name)
        start, end = value, value

    return set(range(start, end + 1, step))


def _parse_int(raw: str, *, minimum: int, maximum: int, name: str) -> int:
    if not raw.isdigit():
        raise ValueError(f"cron 的{name}字段必须是数字、*、范围或步长")
    value = int(raw)
    if value < minimum or value > maximum:
        raise ValueError(f"cron 的{name}字段取值必须在 {minimum}-{maximum} 之间")
    return value
