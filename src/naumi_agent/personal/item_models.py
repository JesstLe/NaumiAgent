"""Strict immutable personal-item contracts and their pure validation helpers."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from naumi_agent.daemons.agent_jobs import AgentJobPayload, agent_job_id_for_request
from naumi_agent.daemons.agent_worker_contract import AgentWorkerRequest
from naumi_agent.personal.artifact_models import json_object, normalize_refs, strict_json_loads
from naumi_agent.personal.models import PersonalAgentError

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
ITEM_ID = re.compile(r"^pi_[0-9a-f]{32}$")
INTENT_ID = re.compile(r"^pio_[0-9a-f]{32}$")
SESSION_ID = re.compile(r"^pie_[0-9a-f]{32}$")
ONCE_KEY = re.compile(r"^[0-9a-f]{32}$")
PRINCIPAL = re.compile(r"^(main|pa_[0-9a-f]{32})$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,511}$")
STATES = frozenset({"active", "paused", "cancelled", "closed"})
TOOLS = frozenset({"personal_artifacts", "code_execute", "bash_run", "file_read", "read"})


def invalid(message: str) -> PersonalAgentError:
    return PersonalAgentError("invalid_input", message)


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text(value: Any, label: str, maximum: int, *, empty=False, strip=False, pattern=None) -> str:
    if not isinstance(value, str):
        raise invalid(f"{label}必须是文字")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise invalid(f"{label}包含无效编码") from exc
    result = value.strip() if strip else value
    if len(result) > maximum or _CONTROL.search(result) or not empty and not result.strip():
        raise invalid(f"{label}内容为空、过长或包含无效字符")
    if pattern is not None and pattern.fullmatch(result) is None:
        raise invalid(f"{label}格式不正确")
    return result


def integer(value: Any, label: str, minimum=1, maximum=2**31 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise invalid(f"{label}必须是有效整数")
    return value


def keys(value: Any, allowed: set[str], required: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        raise invalid(f"{label}包含未知字段或缺少必要内容")
    return value


def normalized_object(value: Any, label: str, *, maximum=1024 * 1024) -> dict:
    """Detach nested caller-owned values and reject nonfinite/unbounded JSON."""
    if isinstance(value, (str, bytes)):
        value = strict_json_loads(value)
    return json_object(value, label, maximum=maximum)


def timestamp(value: Any, label: str) -> str:
    raw = text(value, label, 64)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("naive timestamp")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as exc:
        raise invalid(f"{label}需要带时区的时间") from exc


def _choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise invalid(f"{label}选项不正确")
    return value


def _array(value, label, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise invalid(f"{label}数量或类型不正确")
    return value


def _names(value, label, allowed=None, maximum=32):
    values = [text(v, label, 128, strip=True) for v in _array(value, label, maximum)]
    if len(values) != len(set(values)) or allowed is not None and not set(values) <= allowed:
        raise invalid(f"{label}包含重复或不支持的内容")
    return sorted(values)


def _path(value, label, *, absolute=False):
    raw = text(value, label, 4096)
    parsed = Path(raw)
    if ".." in parsed.parts or ".." in raw.replace("\\", "/").split("/"):
        raise invalid(f"{label}包含越级路径")
    if absolute and not parsed.is_absolute():
        raise invalid(f"{label}需要绝对路径")
    return raw


def normalize_policy(value: Any) -> dict:
    names = {
        "tool_names",
        "artifact_operations",
        "model_tier",
        "max_turns",
        "max_cost_microusd",
        "timeout_seconds",
    }
    v = keys(value, names, names, "事项授权策略")
    cost = v["max_cost_microusd"]
    return {
        "tool_names": _names(v["tool_names"], "事项工具", TOOLS, maximum=len(TOOLS)),
        "artifact_operations": _names(
            v["artifact_operations"],
            "资料操作",
            {"get", "read", "publish"},
            maximum=3,
        ),
        "model_tier": _choice(v["model_tier"], {"fast", "capable", "reasoning"}, "模型等级"),
        "max_turns": integer(v["max_turns"], "执行轮数", maximum=1000),
        "max_cost_microusd": None if cost is None else integer(cost, "费用额度", 0, 10**15),
        "timeout_seconds": integer(v["timeout_seconds"], "执行时限", maximum=604800),
    }


def normalize_schedule(value: Any) -> dict:
    fields = {
        "mode",
        "local_time",
        "timezone",
        "fold",
        "due_at",
        "misfire_policy",
        "grace_seconds",
    }
    v = keys(value, fields, fields, "事项时间")
    mode = _choice(v["mode"], {"once", "now"}, "触发方式")
    zone_name = text(v["timezone"], "时区", 128)
    local_raw = text(v["local_time"], "本地时间", 64)
    fold = v["fold"]
    if fold is not None:
        integer(fold, "重叠时刻选择", 0, 1)
    try:
        zone = ZoneInfo(zone_name)
        local = datetime.fromisoformat(local_raw)
        if local.tzinfo is not None:
            raise ValueError("local_time carries offset")
        options = []
        for candidate_fold in (0, 1):
            candidate = local.replace(tzinfo=zone, fold=candidate_fold).astimezone(UTC)
            back = candidate.astimezone(zone)
            if back.replace(tzinfo=None) == local:
                options.append((candidate_fold, candidate))
        unique = {candidate for _, candidate in options}
        if not unique or len(unique) > 1 and fold is None:
            raise ValueError("nonexistent or ambiguous local time")
        resolved = next(candidate for f, candidate in options if f == (fold or 0))
        due = timestamp(v["due_at"], "实际触发时间")
        if datetime.fromisoformat(due) != resolved:
            raise ValueError("UTC does not match local time")
    except (ValueError, KeyError, StopIteration, ZoneInfoNotFoundError, OverflowError) as exc:
        raise invalid("事项时间与时区不一致，或夏令时刻需要明确选择") from exc
    return {
        "mode": mode,
        "local_time": local.isoformat(),
        "timezone": zone_name,
        "fold": fold,
        "due_at": due,
        "misfire_policy": _choice(v["misfire_policy"], {"run_once", "skip"}, "错过触发策略"),
        "grace_seconds": integer(v["grace_seconds"], "补执行窗口", 0, 604800),
    }


def normalize_definition(value: Any) -> dict:
    fields = {
        "title",
        "instructions",
        "tags",
        "assignee",
        "origin",
        "route",
        "schedule",
        "inputs",
        "policy",
        "output",
    }
    value = normalized_object(value, "事项定义")
    v = keys(value, fields, fields, "事项定义")
    assignee = keys(
        v["assignee"],
        {"id", "version", "name", "kind"},
        {"id", "version", "name", "kind"},
        "执行 Agent",
    )
    actor = text(assignee["id"], "执行 Agent 编号", 35, pattern=PRINCIPAL)
    version = integer(assignee["version"], "Agent 版本", 0)
    kind = _choice(assignee["kind"], {"main", "specialist"}, "执行 Agent 类型")
    if (actor == "main") != (kind == "main") or (version == 0) != (actor == "main"):
        raise invalid("执行 Agent 的编号、类型与版本不一致")
    origin = keys(v["origin"], {"actor_id", "session_id"}, {"actor_id", "session_id"}, "事项来源")
    origin_actor = text(origin["actor_id"], "来源 Agent", 35, pattern=PRINCIPAL)
    session = text(origin["session_id"], "来源会话", 512, empty=True)
    if session and IDENTIFIER.fullmatch(session) is None:
        raise invalid("来源会话格式不正确")
    route_fields = {"workspace_key", "workspace_root", "session_db_key"}
    route = keys(v["route"], route_fields, route_fields, "执行路由")
    inputs = keys(
        v["inputs"], {"artifact_refs", "directories"}, {"artifact_refs", "directories"}, "事项资料"
    )
    directories, seen = [], set()
    for raw in _array(inputs["directories"], "资料目录", 8):
        fs = {"path", "pattern", "max_files", "max_bytes"}
        entry = keys(raw, fs, fs, "资料目录")
        path = _path(entry["path"], "资料目录")
        pattern = _path(entry["pattern"], "文件筛选")
        if len(pattern) > 256 or Path(pattern).is_absolute() or path in seen:
            raise invalid("资料目录重复或筛选格式不正确")
        seen.add(path)
        directories.append(
            {
                "path": path,
                "pattern": pattern,
                "max_files": integer(entry["max_files"], "目录文件数量", maximum=100),
                "max_bytes": integer(entry["max_bytes"], "目录文件大小", maximum=50 * 1024**2),
            }
        )
    output = keys(v["output"], {"destination", "format"}, {"destination", "format"}, "交付位置")
    return {
        "title": text(v["title"], "事项名称", 120, strip=True),
        "instructions": text(v["instructions"], "事项要求", 12000),
        "tags": _names(v["tags"], "事项分类", maximum=16),
        "assignee": {
            "id": actor,
            "version": version,
            "name": text(assignee["name"], "Agent 名称", 80),
            "kind": kind,
        },
        "origin": {"actor_id": origin_actor, "session_id": session},
        "route": {
            "workspace_key": text(route["workspace_key"], "工作区标识", 64, pattern=SHA256),
            "workspace_root": _path(route["workspace_root"], "工作区目录", absolute=True),
            "session_db_key": text(route["session_db_key"], "会话库标识", 64, pattern=SHA256),
        },
        "schedule": normalize_schedule(v["schedule"]),
        "inputs": {
            "artifact_refs": normalize_refs(inputs["artifact_refs"]),
            "directories": directories,
        },
        "policy": normalize_policy(v["policy"]),
        "output": {
            "destination": _choice(output["destination"], {"personal_library"}, "交付位置"),
            "format": _choice(output["format"], {"report"}, "报告格式"),
        },
    }


def definition_digest(value: Any) -> str:
    return digest(normalize_definition(value))


def normalize_intent(value: Any) -> dict:
    fields = {
        "occurrence_id",
        "item_id",
        "item_revision",
        "once_key",
        "definition_sha256",
        "workspace_key",
        "schedule_id",
        "event_id",
        "execution_session_id",
        "task_id",
        "actor_id",
        "agent_version",
        "input_refs",
        "input_manifest",
        "policy",
        "main_policy_sha256",
        "request",
        "payload",
        "issued_at",
        "prepared_sha256",
    }
    v = normalized_object(value, "事项执行意图", maximum=20 * 1024**2)
    v.pop("references", None)
    v.pop("references_sha256", None)
    keys(v, fields, fields, "事项执行意图")
    text(v["occurrence_id"], "执行意图编号", 36, pattern=INTENT_ID)
    text(v["item_id"], "事项编号", 35, pattern=ITEM_ID)
    integer(v["item_revision"], "事项版本")
    text(v["once_key"], "单次委托编号", 32, pattern=ONCE_KEY)
    for key in ("definition_sha256", "workspace_key", "prepared_sha256"):
        text(v[key], "执行意图摘要", 64, pattern=SHA256)
    text(v["execution_session_id"], "执行会话", 36, pattern=SESSION_ID)
    for key in ("schedule_id", "event_id", "task_id"):
        text(v[key], "调度或任务编号", 512, pattern=IDENTIFIER)
    actor = text(v["actor_id"], "执行 Agent", 35, pattern=PRINCIPAL)
    version = integer(v["agent_version"], "执行 Agent 版本", 0)
    if (actor == "main") != (version == 0):
        raise invalid("执行 Agent 与版本不一致")
    main_sha = text(v["main_policy_sha256"], "主 Agent 策略摘要", 64, empty=True)
    if actor == "main" and SHA256.fullmatch(main_sha) is None or actor != "main" and main_sha:
        raise invalid("主 Agent 策略摘要与执行身份不一致")
    normalize_refs(v["input_refs"])
    manifest = _array(v["input_manifest"], "输入清单", 64)
    total_bytes = 0
    for raw in manifest:
        fs = {
            "source",
            "artifact_ref",
            "filename",
            "bytes",
            "sha256",
            "directory_path",
            "relative_path",
        }
        entry = keys(raw, fs, {"source", "artifact_ref", "filename", "bytes", "sha256"}, "输入清单")
        _choice(entry["source"], {"artifact", "directory"}, "输入来源")
        ref = normalize_refs([entry["artifact_ref"]])[0]
        name = text(entry["filename"], "输入文件名", 240)
        if any(c in name for c in "/\\"):
            raise invalid("输入文件名不包含目录")
        total_bytes += integer(entry["bytes"], "输入文件大小", maximum=20 * 1024**2)
        if text(entry["sha256"], "输入文件摘要", 64, pattern=SHA256) != ref["sha256"]:
            raise invalid("输入文件摘要与确切引用不一致")
        for key in ("directory_path", "relative_path"):
            if key in entry:
                _path(entry[key], "输入来源路径")
        if "relative_path" in entry and Path(entry["relative_path"]).is_absolute():
            raise invalid("输入相对路径格式不正确")
    if total_bytes > 50 * 1024**2:
        raise invalid("本次输入资料超过 50 MiB")
    normalize_policy(v["policy"])
    try:
        request_fields = dict(v["request"])
        request_fields["tool_scope"] = tuple(request_fields["tool_scope"])
        request = AgentWorkerRequest(**request_fields)
        payload = AgentJobPayload(**v["payload"])
    except (KeyError, ValueError, TypeError) as exc:
        raise invalid("执行请求或正文不符合已有 AgentJob 合同") from exc
    comparisons = {
        "session_id_sha256": payload.session_id,
        "task_id_sha256": payload.task_id,
        "task_sha256": payload.task,
        "context_sha256": payload.context,
        "message_topic_sha256": payload.message_topic,
    }
    for field, raw in comparisons.items():
        if getattr(request, field) != hashlib.sha256(raw.encode("utf-8")).hexdigest():
            raise invalid("执行请求与原始正文摘要不一致")
    if (
        payload.session_id != v["execution_session_id"]
        or payload.task_id != v["task_id"]
        or request.task_bytes != len(payload.task.encode("utf-8"))
        or request.context_bytes != len(payload.context.encode("utf-8"))
        or timestamp(v["issued_at"], "执行签发时间") != timestamp(request.issued_at, "请求签发时间")
    ):
        raise invalid("执行请求与会话、任务或时间不一致")
    policy = v["policy"]
    if (
        not set(request.tool_scope) <= set(policy["tool_names"])
        or request.model_tier != policy["model_tier"]
        or request.max_turns != policy["max_turns"]
        or request.max_cost_microusd != policy["max_cost_microusd"]
        or request.timeout_milliseconds != policy["timeout_seconds"] * 1000
    ):
        raise invalid("执行请求超出冻结授权策略")
    if v["prepared_sha256"] != digest({k: val for k, val in v.items() if k != "prepared_sha256"}):
        raise invalid("执行意图摘要不一致")
    # Exercise upstream deterministic identity without opening any Job database.
    agent_job_id_for_request(request)
    return json.loads(canonical(v))


validate_definition = normalize_definition
validate_intent = normalize_intent
definition_sha256 = definition_digest
canonical_json = canonical
