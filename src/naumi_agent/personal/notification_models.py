"""Strict, detached notification identities and user-owned presentation preferences."""

from __future__ import annotations

import json
import re

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical, digest, integer, text
from naumi_agent.personal.routine_schedule import _zone

SHA = re.compile(r"^[a-f0-9]{64}$")
ACTOR = re.compile(r"^(main|pa_[a-f0-9]{32})$")
EVENT = re.compile(r"^ne_[a-f0-9]{32}$")
CLOCK = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
PREFIXES = frozenset({"ne_", "no_", "nb_", "np_", "ns_", "nr_", "nx_", "nw_", "nc_"})
PREFERENCE_FIELDS = frozenset(
    {
        "enabled",
        "timezone",
        "quiet_hours",
        "muted_actor_ids",
        "debounce_seconds",
        "max_delay_seconds",
        "bypass_kinds",
        "include_existing",
    }
)
PREFERENCE_METADATA = frozenset(
    {
        "id",
        "workspace_key",
        "revision",
        "status",
        "cutover_at",
        "created_at",
        "updated_at",
        "digest",
    }
)


def _fields(value, expected):
    if not isinstance(value, dict) or set(value) != expected:
        raise PersonalAgentError("invalid_input", "通知字段不完整或包含未支持字段")
    canonical(value)
    return value


def _boolean(value, label):
    if type(value) is not bool:
        raise PersonalAgentError("invalid_input", f"{label}需要明确选择")
    return value


def _unique_list(value, label, maximum, normalize):
    if not isinstance(value, list) or len(value) > maximum:
        raise PersonalAgentError("invalid_input", f"{label}格式不正确或超过上限")
    values = [normalize(item) for item in value]
    if len({canonical(item) for item in values}) != len(values):
        raise PersonalAgentError("invalid_input", f"{label}包含重复选项")
    return values


def identity(prefix, document):
    """Derive IDs from a nested domain-separated document, not caller-supplied metadata."""
    if not isinstance(prefix, str) or prefix not in PREFIXES or not isinstance(document, dict):
        raise PersonalAgentError("invalid_input", "通知身份字段不正确")
    return (
        prefix
        + digest({"domain": "personal_notifications_v1", "kind": prefix, "document": document})[:32]
    )


def record_digest(record):
    """Cover all journal fields except a view's separately attached digest."""
    if not isinstance(record, dict):
        raise PersonalAgentError("invalid_input", "通知记录需要有效对象")
    canonical(record)
    return digest({key: value for key, value in record.items() if key != "digest"})


def _window(raw):
    raw = _fields(raw, {"weekdays", "start", "end", "full_day"})
    weekdays = _unique_list(
        raw["weekdays"],
        "勿扰日期",
        7,
        lambda value: integer(value, "勿扰星期", minimum=0, maximum=6),
    )
    if not weekdays:
        raise PersonalAgentError("invalid_input", "请至少选择一个勿扰日期")
    start = text(raw["start"], "勿扰开始时间", 5, pattern=CLOCK)
    end = text(raw["end"], "勿扰结束时间", 5, pattern=CLOCK)
    full_day = _boolean(raw["full_day"], "全天勿扰")
    if (start == end) != full_day:
        raise PersonalAgentError("invalid_input", "相同起止时间需要选择全天，全天起止时间需要一致")
    return {"weekdays": sorted(weekdays), "start": start, "end": end, "full_day": full_day}


def _normalize_preference_values(raw, *, timezone_sha256):
    windows = _unique_list(raw["quiet_hours"], "勿扰时段", 14, _window)
    muted = _unique_list(
        raw["muted_actor_ids"],
        "静音 Agent",
        1000,
        lambda value: text(value, "静音 Agent", 35, pattern=ACTOR),
    )

    def bypass(value):
        if not isinstance(value, str) or value not in {"item_failure", "calendar_approval"}:
            raise PersonalAgentError("invalid_input", "勿扰例外只支持执行失败或日历审批")
        return value

    bypass_kinds = _unique_list(raw["bypass_kinds"], "勿扰例外", 2, bypass)
    debounce = integer(raw["debounce_seconds"], "通知合并等待", minimum=0, maximum=300)
    maximum = integer(raw["max_delay_seconds"], "最长通知等待", minimum=1, maximum=3600)
    if maximum < debounce:
        raise PersonalAgentError("invalid_input", "最长通知等待需要大于或等于合并等待")
    return {
        "enabled": _boolean(raw["enabled"], "主动通知"),
        "timezone": raw["timezone"],
        "timezone_sha256": timezone_sha256,
        "quiet_hours": sorted(windows, key=canonical),
        "muted_actor_ids": sorted(muted),
        "debounce_seconds": debounce,
        "max_delay_seconds": maximum,
        "bypass_kinds": sorted(bypass_kinds),
        "include_existing": _boolean(raw["include_existing"], "纳入已有结果"),
    }


def normalize_preferences(raw):
    """Resolve raw user input once; the actual TZif digest is always server-owned."""
    raw = _fields(raw, PREFERENCE_FIELDS)
    _, timezone_sha256 = _zone(raw["timezone"])
    return _normalize_preference_values(raw, timezone_sha256=timezone_sha256)


def validate_preferences(value):
    """Validate persisted values without replacing their original TZif version.

    Metadata ownership belongs to the service. This helper validates only the
    preference body and its strict known envelope, leaving timestamps, identity
    binding and revision history to the journal/service's exact-source checks.
    """
    if (
        not isinstance(value, dict)
        or not (PREFERENCE_FIELDS | {"timezone_sha256"}) <= set(value)
        or set(value) - PREFERENCE_FIELDS - PREFERENCE_METADATA - {"timezone_sha256"}
    ):
        raise PersonalAgentError("invalid_input", "保存的通知设置字段不正确")
    canonical(value)
    _zone(value["timezone"])
    original_sha = text(value["timezone_sha256"], "原时区资料摘要", 64, pattern=SHA)
    raw = {key: value[key] for key in PREFERENCE_FIELDS}
    normalized = _normalize_preference_values(raw, timezone_sha256=original_sha)
    if any(value[key] != normalized[key] for key in normalized):
        raise PersonalAgentError("invalid_input", "保存的通知设置不是原标准格式")
    return json.loads(canonical(value))


def default_preferences(workspace_key):
    """Return a virtual disabled record without creating a store or accepting history."""
    workspace = text(workspace_key, "工作区标识", 64, pattern=SHA)
    raw = normalize_preferences(
        {
            "enabled": False,
            "timezone": "UTC",
            "quiet_hours": [],
            "muted_actor_ids": [],
            "debounce_seconds": 60,
            "max_delay_seconds": 300,
            "bypass_kinds": [],
            "include_existing": False,
        }
    )
    return {
        "id": identity("np_", {"workspace_key": workspace}),
        "workspace_key": workspace,
        "revision": 0,
        **raw,
        "cutover_at": None,
        "created_at": None,
        "updated_at": None,
    }


def normalize_event_ref(value):
    """Keep the original event and digest inseparable across presentation/read receipts."""
    value = _fields(value, {"event_id", "event_digest"})
    return {
        "event_id": text(value["event_id"], "通知编号", 35, pattern=EVENT),
        "event_digest": text(value["event_digest"], "原通知摘要", 64, pattern=SHA),
    }
