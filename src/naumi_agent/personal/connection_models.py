"""Bounded values shared by calendar connections, sources and exact action approval."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from naumi_agent.personal.models import PersonalAgentError

ID = re.compile(r"^(pc|co|ca)_[a-f0-9]{32}$")
ACTOR = re.compile(r"^(main|pa_[a-f0-9]{32})$")
SHA = re.compile(r"^[a-f0-9]{64}$")
REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
CALENDAR_READ = "https://www.googleapis.com/auth/calendar.events.owned.readonly"
CALENDAR_WRITE = "https://www.googleapis.com/auth/calendar.events.owned"
EMAIL = "https://www.googleapis.com/auth/userinfo.email"
KINDS = frozenset(
    {
        "client",
        "connection",
        "oauth",
        "event",
        "sync",
        "binding",
        "action",
        "calendar_change",
        "calendar_change_head",
    }
)
SOURCE_FIELDS = frozenset(
    {
        "connection_id",
        "auth_epoch",
        "event_id",
        "revision",
        "payload_sha256",
        "occurrence_key",
        "grant_id",
        "grant_revision",
    }
)


def invalid(message="连接请求字段不完整或格式不正确"):
    return PersonalAgentError("invalid_input", message)


def canonical(value):
    try:

        def visit(item, depth=0):
            if depth > 24:
                raise invalid("连接资料嵌套超过上限")
            if item is None or isinstance(item, bool):
                return
            if isinstance(item, str):
                if any(ord(c) < 32 and c not in "\t\r\n" for c in item):
                    raise invalid()
                item.encode("utf-8")
                return
            if isinstance(item, int) or isinstance(item, float) and math.isfinite(item):
                return
            if isinstance(item, list):
                for child in item:
                    visit(child, depth + 1)
                return
            if isinstance(item, dict) and all(isinstance(k, str) for k in item):
                for k, child in item.items():
                    visit(k, depth + 1)
                    visit(child, depth + 1)
                return
            raise invalid()

        visit(value)
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (UnicodeError, RecursionError, ValueError, TypeError) as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise invalid() from None


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text(value, label, maximum=500, *, pattern=None, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise invalid(f"{label}格式不正确")
    canonical(value)
    if pattern and not pattern.fullmatch(value):
        raise invalid(f"{label}格式不正确")
    return value


def integer(value, label, minimum=1, maximum=2**53 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise invalid(f"{label}格式不正确")
    return value


def timestamp(value, label="时间"):
    text(value, label, 80)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(UTC)
    except ValueError:
        raise invalid(f"{label}需要明确的时区") from None


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise invalid()
    canonical(value)
    return value


def source_ref(value):
    fields(value, SOURCE_FIELDS, SOURCE_FIELDS - {"grant_id", "grant_revision"})
    result = {
        **value,
        "grant_id": value.get("grant_id", ""),
        "grant_revision": value.get("grant_revision", 0),
    }
    text(result["connection_id"], "连接编号", 35, pattern=re.compile(r"pc_[a-f0-9]{32}"))
    text(result["event_id"], "日程编号", 1024)
    text(result["occurrence_key"], "日程实例标识", 2048)
    text(result["payload_sha256"], "日程内容摘要", 64, pattern=SHA)
    integer(result["auth_epoch"], "授权版本")
    integer(result["revision"], "日程版本")
    text(result["grant_id"], "分享编号", 64, empty=True)
    integer(result["grant_revision"], "分享版本", minimum=0)
    if bool(result["grant_id"]) != bool(result["grant_revision"]):
        raise invalid("分享编号与版本需要同时存在")
    return result


def action_source_ref(value):
    names = {
        "connection_id",
        "auth_epoch",
        "action_id",
        "revision",
        "digest",
        "actor_id",
        "grant_id",
        "grant_revision",
    }
    fields(value, names, names)
    text(value["connection_id"], "连接编号", 35, pattern=re.compile(r"pc_[a-f0-9]{32}"))
    text(value["action_id"], "行动编号", 35, pattern=re.compile(r"ca_[a-f0-9]{32}"))
    text(value["actor_id"], "行动所属 Agent", 35, pattern=ACTOR)
    integer(value["auth_epoch"], "账户授权版本")
    integer(value["revision"], "行动资料版本")
    text(value["digest"], "行动资料摘要", 64, pattern=SHA)
    text(value["grant_id"], "行动分享编号", 64, empty=True)
    integer(value["grant_revision"], "行动分享版本", minimum=0)
    if bool(value["grant_id"]) != bool(value["grant_revision"]):
        raise invalid("行动分享编号与版本需要同时存在")
    return dict(value)


def action_body(value):
    fields(value, {"summary", "description", "start", "end"}, {"summary", "start", "end"})
    result = {
        "summary": text(value["summary"], "日程标题", 500),
        "description": text(value.get("description", ""), "日程正文", 20000, empty=True),
    }
    dates, times = [], []
    for name in ("start", "end"):
        point = value[name]
        fields(point, {"date", "dateTime", "timeZone"})
        if "date" in point:
            if set(point) != {"date"}:
                raise invalid("全天日程只填写日期")
            try:
                parsed = date.fromisoformat(text(point["date"], "日程日期", 10))
            except ValueError:
                raise invalid("日程日期格式不正确") from None
            dates.append(parsed)
            result[name] = {"date": parsed.isoformat()}
        else:
            if set(point) != {"dateTime", "timeZone"}:
                raise invalid("日程起止需要日期时间和时区")
            instant = timestamp(point["dateTime"], "日程起止时间")
            zone = text(point["timeZone"], "日程时区", 100)
            try:
                timezone = ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError):
                raise invalid("请选择有效的日程时区") from None
            times.append(instant)
            # RFC3339 identifies an instant; the IANA zone controls its display.
            # The browser intentionally transmits UTC instants for DST folds.
            result[name] = {"dateTime": instant.astimezone(timezone).isoformat(), "timeZone": zone}
    if len(dates) not in {0, 2} or len(times) not in {0, 2}:
        raise invalid("日程开始和结束需要使用同一种时间格式")
    pair = dates or times
    if pair[1] <= pair[0] or (pair[1] - pair[0]).days > 366:
        raise invalid("日程结束需要晚于开始，且间隔不超过一年")
    return result


def scopes(value, *, write):
    if not isinstance(value, str) or len(value) > 4096:
        raise invalid("账户未返回可核验的授权范围")
    granted = {EMAIL if v == "email" else v for v in value.split()}
    required = {"openid", EMAIL, CALENDAR_WRITE if write else CALENDAR_READ}
    allowed = {"openid", EMAIL, CALENDAR_READ, CALENDAR_WRITE}
    if not required <= granted or not granted <= allowed:
        raise PersonalAgentError("scope_denied", "账户返回的权限与本次连接申请不一致")
    return sorted(granted)
