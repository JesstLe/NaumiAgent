"""Strict planning identities and real civil-time deadlines, independent of execution."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, time, timedelta
from datetime import date as civil_date

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical, digest, integer, text
from naumi_agent.personal.routine_schedule import _wall_candidates, _zone

SHA = re.compile(r"^[a-f0-9]{64}$")
ACTOR = re.compile(r"^(main|pa_[a-f0-9]{32})$")
ITEM = re.compile(r"^pi_[a-f0-9]{32}$")
ONCE = re.compile(r"^[a-f0-9]{32}$")
DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
LOCAL = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?$"
)
ITEM_REF_FIELDS = frozenset(
    {
        "kind",
        "workspace_key",
        "item_id",
        "item_revision",
        "once_key",
        "definition_sha256",
        "assignee_id",
        "assignee_version",
    }
)
TODAY_REF_FIELDS = frozenset(
    {
        "day_id",
        "day_revision",
        "day_digest",
        "workspace_key",
        "planner_id",
        "date",
        "timezone",
        "timezone_sha256",
    }
)
METADATA_REF_FIELDS = frozenset(
    {"metadata_id", "metadata_revision", "metadata_digest", "workspace_key", "item_ref"}
)


def _error(code, message):
    return PersonalAgentError(code, message)


def _fields(value, allowed, required=None, *, code="invalid_input"):
    required = allowed if required is None else required
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        raise _error(code, "计划字段不完整或包含未支持字段")
    canonical(value)
    return value


def _date(value):
    if not isinstance(value, str) or DATE.fullmatch(value) is None:
        raise _error("invalid_date", "日期需要使用 YYYY-MM-DD 格式")
    try:
        return civil_date.fromisoformat(value)
    except ValueError:
        raise _error("invalid_date", "请选择真实存在的公历日期") from None


def _local(value):
    if not isinstance(value, str) or LOCAL.fullmatch(value) is None:
        raise _error("invalid_deadline", "截止时间需要完整本地日期和时刻，不包含时区偏移")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise _error("invalid_deadline", "截止时间的日期或时刻不正确") from None


def _instant(value):
    try:
        if isinstance(value, str) and 0 < len(value) <= 64:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError()
        return value.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise _error("invalid_deadline", "时间需要包含明确的时区偏移") from None


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _identity(prefix, document):
    return prefix + digest({"domain": "personal_today_v1", "kind": prefix, **document})[:32]


def normalize_item_ref(value):
    """Validate exact source identity without substituting a current source revision."""
    value = _fields(value, ITEM_REF_FIELDS)
    if value["kind"] != "item":
        raise _error("invalid_input", "计划来源需要确切的个人事项引用")
    result = {
        "kind": "item",
        "workspace_key": text(value["workspace_key"], "工作区标识", 64, pattern=SHA),
        "item_id": text(value["item_id"], "事项编号", 35, pattern=ITEM),
        "item_revision": integer(value["item_revision"], "原事项版本"),
        "once_key": text(value["once_key"], "原单次委托编号", 32, pattern=ONCE),
        "definition_sha256": text(value["definition_sha256"], "原事项摘要", 64, pattern=SHA),
        "assignee_id": text(value["assignee_id"], "原执行 Agent", 35, pattern=ACTOR),
        "assignee_version": integer(value["assignee_version"], "原 Agent 版本", minimum=0),
    }
    if (result["assignee_id"] == "main") != (result["assignee_version"] == 0):
        raise _error("invalid_input", "原执行 Agent 的编号与版本不一致")
    return result


def item_ref(item):
    """Extract a reference from a source snapshot; this does not authorize the source."""
    try:
        definition = item["definition"]
        workspace = definition["route"]["workspace_key"]
        if "workspace_key" in item and item["workspace_key"] != workspace:
            raise _error("invalid_input", "事项工作区与原执行路由不一致")
        return normalize_item_ref(
            {
                "kind": "item",
                "workspace_key": workspace,
                "item_id": item["item_id"],
                "item_revision": item["revision"],
                "once_key": item["once_key"],
                "definition_sha256": item["definition_sha256"],
                "assignee_id": definition["assignee"]["id"],
                "assignee_version": definition["assignee"]["version"],
            }
        )
    except (KeyError, TypeError):
        raise _error("invalid_input", "事项缺少原始身份、修订或执行路由") from None


def semantic_item_ref(value):
    """Lifecycle revisions do not change the original execution's semantic identity."""
    return {key: item for key, item in normalize_item_ref(value).items() if key != "item_revision"}


def day_identity(workspace_key, planner_id, date, timezone):
    workspace = text(workspace_key, "工作区标识", 64, pattern=SHA)
    planner = text(planner_id, "规划 Agent", 35, pattern=ACTOR)
    day = _date(date).isoformat()
    _zone(timezone)
    return _identity(
        "td_",
        {"workspace_key": workspace, "planner_id": planner, "date": day, "timezone": timezone},
    )


def metadata_identity(workspace_key, item_id):
    return _identity(
        "tm_",
        {
            "workspace_key": text(workspace_key, "工作区标识", 64, pattern=SHA),
            "item_id": text(item_id, "事项编号", 35, pattern=ITEM),
        },
    )


def placement_identity(day_id, metadata_id):
    return _identity(
        "tp_",
        {
            "day_id": text(day_id, "日计划编号", 35, pattern=re.compile(r"^td_[a-f0-9]{32}$")),
            "metadata_id": text(
                metadata_id, "计划资料编号", 35, pattern=re.compile(r"^tm_[a-f0-9]{32}$")
            ),
        },
    )


def _civil_boundary(day, zone):
    """Resolve midnight's earliest actual boundary, including forward midnight gaps."""
    wall = datetime.combine(day, time.min)
    candidates = _wall_candidates(wall, zone, "both")
    if candidates:
        return candidates[0][0]
    try:
        # For a nonexistent wall time the two offsets map to opposite sides of
        # its forward transition. Find the exact first real UTC instant, rather
        # than shifting midnight by an assumed hour or scanning minute samples.
        endpoints = sorted(wall.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1))
        lower, upper = endpoints
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
        raise _error("invalid_date", "该日期边界超出了所选时区的有效范围") from None


def day_window(date, timezone):
    """Return real civil-day UTC bounds, never an assumed twenty-four-hour span."""
    day = _date(date)
    zone, timezone_sha = _zone(timezone)
    try:
        start = _civil_boundary(day, zone)
        end = _civil_boundary(day + timedelta(days=1), zone)
        if start.astimezone(zone).date() != day or end <= start:
            raise _error("nonexistent_date", "该日期在所选时区不存在，请选择其他日期")
    except (ValueError, OverflowError) as exc:
        if isinstance(exc, PersonalAgentError):
            raise
        raise _error("invalid_date", "日期超出了可表示的计划范围") from None
    return {
        "date": day.isoformat(),
        "timezone": timezone,
        "timezone_sha256": timezone_sha,
        "start_at": _iso(start),
        "end_at": _iso(end),
    }


def normalize_deadline(value):
    """Resolve strict user input; resolved timestamps and TZif digests are server-owned."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _error("invalid_deadline", "请选择全天截止、具体截止或无截止")
    kind = value.get("kind")
    if kind == "date":
        value = _fields(value, {"kind", "local_date", "timezone"}, code="invalid_deadline")
        window = day_window(value["local_date"], value["timezone"])
        return {
            "kind": "date",
            "local_date": window["date"],
            "timezone": window["timezone"],
            "boundary_at": window["end_at"],
            "timezone_sha256": window["timezone_sha256"],
        }
    if kind != "instant":
        raise _error("invalid_deadline", "截止类型需要为 date 或 instant")
    value = _fields(
        value,
        {"kind", "local_time", "timezone", "fold"},
        {"kind", "local_time", "timezone"},
        code="invalid_deadline",
    )
    local = _local(value["local_time"])
    zone, timezone_sha = _zone(value["timezone"])
    fold = value.get("fold")
    if "fold" in value:
        integer(fold, "重复时刻选择", minimum=0, maximum=1)
    choices = _wall_candidates(local, zone, "both")
    if not choices:
        raise _error("nonexistent_time", "该本地截止时刻不存在，请选择其他时刻")
    if len(choices) > 1 and fold is None:
        raise _error("ambiguous_time", "该截止时刻出现两次，请明确选择第一次或第二次")
    selected = next((pair for pair in choices if pair[1] == (fold or 0)), None)
    if selected is None:
        raise _error("invalid_deadline", "该截止时刻没有所选的重复时间序号")
    return {
        "kind": "instant",
        "local_time": local.isoformat(),
        "timezone": value["timezone"],
        "fold": selected[1],
        "resolved_at": _iso(selected[0]),
        "timezone_sha256": timezone_sha,
    }


def validate_deadline(value, *, allow_timezone_change=True):
    """Validate persisted facts; changed TZif data never rewrites an earlier deadline."""
    if type(allow_timezone_change) is not bool:
        raise _error("invalid_input", "时区版本检查选项不正确")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("kind"), str)
        or value["kind"] not in {"date", "instant"}
    ):
        raise _error("invalid_deadline", "保存的截止记录格式不正确")
    if value["kind"] == "date":
        _fields(
            value,
            {"kind", "local_date", "timezone", "boundary_at", "timezone_sha256"},
            code="invalid_deadline",
        )
        _date(value["local_date"])
        raw = {key: value[key] for key in ("kind", "local_date", "timezone")}
        stamp = value["boundary_at"]
    else:
        _fields(
            value,
            {"kind", "local_time", "timezone", "fold", "resolved_at", "timezone_sha256"},
            code="invalid_deadline",
        )
        if _local(value["local_time"]).isoformat() != value["local_time"]:
            raise _error("invalid_deadline", "保存的本地截止时刻不是原标准格式")
        integer(value["fold"], "原重复时刻选择", minimum=0, maximum=1)
        raw = {key: value[key] for key in ("kind", "local_time", "timezone", "fold")}
        stamp = value["resolved_at"]
    if not isinstance(stamp, str) or _iso(_instant(stamp)) != stamp:
        raise _error("invalid_deadline", "保存的截止时间不是原 UTC 标准格式")
    text(value["timezone_sha256"], "原时区资料摘要", 64, pattern=SHA)
    _, current_sha = _zone(value["timezone"])
    if current_sha != value["timezone_sha256"]:
        if not allow_timezone_change:
            raise _error("calendar_timezone_changed", "时区资料已更新，请核对原截止时间")
    elif normalize_deadline(raw) != value:
        raise _error("invalid_deadline", "截止时间与原本地时刻、时区或摘要不一致")
    return json.loads(canonical(value))


def deadline_status(deadline, now):
    """Describe a deadline without mutating a source or asserting task completion."""
    now = _instant(now)
    value = validate_deadline(deadline)
    if value is None:
        return "none"
    if _zone(value["timezone"])[1] != value["timezone_sha256"]:
        return "timezone_data_changed"
    bound = _instant(value["boundary_at"] if value["kind"] == "date" else value["resolved_at"])
    if now < bound:
        return "upcoming"
    if now == bound and value["kind"] == "instant":
        return "due"
    return "overdue"


def record_digest(record):
    """Hash every journal field, excluding only an external view's added digest."""
    if not isinstance(record, dict):
        raise _error("invalid_input", "计划记录需要有效对象")
    canonical(record)
    return digest({key: value for key, value in record.items() if key != "digest"})


def normalize_today_ref(value):
    value = _fields(value, TODAY_REF_FIELDS)
    expected = day_identity(
        value["workspace_key"], value["planner_id"], value["date"], value["timezone"]
    )
    if value["day_id"] != expected:
        raise _error("invalid_input", "日计划编号与原工作区、Agent、日期或时区不一致")
    integer(value["day_revision"], "原日计划版本", minimum=0)
    text(value["day_digest"], "原日计划摘要", 64, pattern=SHA)
    text(value["timezone_sha256"], "原日计划时区摘要", 64, pattern=SHA)
    return json.loads(canonical(value))


def normalize_metadata_ref(value):
    value = _fields(value, METADATA_REF_FIELDS)
    source = normalize_item_ref(value["item_ref"])
    workspace = text(value["workspace_key"], "工作区标识", 64, pattern=SHA)
    if source["workspace_key"] != workspace or value["metadata_id"] != metadata_identity(
        workspace, source["item_id"]
    ):
        raise _error("invalid_input", "计划资料编号与原事项或工作区不一致")
    integer(value["metadata_revision"], "原计划资料版本", minimum=0)
    text(value["metadata_digest"], "原计划资料摘要", 64, pattern=SHA)
    return {**json.loads(canonical(value)), "item_ref": source}
