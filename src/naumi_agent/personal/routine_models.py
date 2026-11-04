"""Strict routine definitions and identities, separate from immutable v1 Items."""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from naumi_agent.personal.artifact_models import normalize_refs, strict_json_loads
from naumi_agent.personal.connection_models import canonical, digest, integer, text, timestamp
from naumi_agent.personal.models import PersonalAgentError

ROUTINE_ID = re.compile(r"^pr_[a-f0-9]{32}$")
OCCURRENCE_ID = re.compile(r"^pro_[a-f0-9]{32}$")
PREVIEW_ID = re.compile(r"^prp_[a-f0-9]{32}$")
SHA = re.compile(r"^[a-f0-9]{64}$")
REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
ACTOR = re.compile(r"^(main|pa_[a-f0-9]{32})$")
STATUSES = frozenset({"active", "paused", "cancelled", "closed"})
DEFINITION_FIELDS = frozenset(
    {
        "title",
        "instructions",
        "tags",
        "assignee_id",
        "assignee_version",
        "schedule",
        "policy",
        "input_refs",
        "directories",
        "limits",
        "recovery",
    }
)
REQUIRED_DEFINITION_FIELDS = frozenset(
    {
        "title",
        "instructions",
        "assignee_id",
        "assignee_version",
        "schedule",
        "limits",
        "recovery",
    }
)
ROUTINE_REF_FIELDS = frozenset(
    {
        "routine_id",
        "epoch",
        "occurrence_id",
        "definition_revision",
        "definition_sha256",
        "actor_id",
        "workspace_key",
    }
)
PLAN_REF_FIELDS = ROUTINE_REF_FIELDS - {"occurrence_id", "epoch"}


def invalid(code, message):
    return PersonalAgentError(code, message)


def fields(value, allowed, required):
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        raise invalid("invalid_input", "周期请求字段不完整或包含未支持字段")


def detached(value, label="周期资料", *, maximum=4 * 1024 * 1024):
    if not isinstance(value, dict):
        raise invalid("invalid_input", f"{label}需要有效对象")
    try:
        raw = canonical(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise invalid("invalid_input", f"{label}包含无效字段") from exc
    if len(raw.encode("utf-8")) > maximum:
        raise invalid("invalid_input", f"{label}超过容量上限")
    return strict_json_loads(raw)


def request_id(value):
    return text(value, "原请求编号", 128, pattern=REQUEST)


def routine_id(value):
    return text(value, "周期计划编号", 35, pattern=ROUTINE_ID)


def occurrence_id(value):
    return text(value, "周期发生编号", 36, pattern=OCCURRENCE_ID)


def normalize_limits(value):
    required = {"max_executions", "total_cost_microusd", "max_concurrent", "max_pending"}
    fields(value, required, required)
    result = {
        "max_executions": integer(value["max_executions"], "总执行次数", minimum=0, maximum=10**6),
        "total_cost_microusd": integer(
            value["total_cost_microusd"], "总估算费用", minimum=0, maximum=10**15
        ),
        "max_concurrent": integer(value["max_concurrent"], "同时执行数", maximum=64),
        "max_pending": integer(value["max_pending"], "待执行数量", maximum=10000),
    }
    if result["max_pending"] < result["max_concurrent"]:
        raise invalid("invalid_input", "待执行数量必须覆盖同时执行数")
    return result


def normalize_recovery(value):
    required = {"misfire_policy", "grace_seconds", "max_catch_up", "overlap_policy"}
    fields(value, required, required)
    if type(value["misfire_policy"]) is not str or value["misfire_policy"] not in {
        "skip",
        "latest",
        "catch_up",
    }:
        raise invalid("invalid_input", "请选择跳过、仅补最近一次或有界补执行")
    if type(value["overlap_policy"]) is not str or value["overlap_policy"] not in {
        "queue",
        "skip",
        "allow",
    }:
        raise invalid("invalid_input", "请选择排队、跳过重叠或允许并行")
    return {
        "misfire_policy": value["misfire_policy"],
        "grace_seconds": integer(value["grace_seconds"], "正常延迟窗口", minimum=0, maximum=604800),
        "max_catch_up": integer(value["max_catch_up"], "单批补执行次数", maximum=1000),
        "overlap_policy": value["overlap_policy"],
    }


def raw_definition(value):
    value = detached(value)
    fields(value, set(DEFINITION_FIELDS), set(REQUIRED_DEFINITION_FIELDS))
    text(value["title"], "周期名称", 120)
    text(value["instructions"], "周期执行要求", 12000)
    text(value["assignee_id"], "执行 Agent", 35, pattern=ACTOR)
    version = integer(value["assignee_version"], "Agent 版本", minimum=0)
    if (value["assignee_id"] == "main") != (version == 0):
        raise invalid("invalid_input", "执行 Agent 与版本不一致")
    normalize_refs(value.get("input_refs", []))
    normalize_limits(value["limits"])
    normalize_recovery(value["recovery"])
    if not isinstance(value["schedule"], dict) or not isinstance(value.get("policy", {}), dict):
        raise invalid("invalid_input", "周期规则或执行策略格式不正确")
    return value


def definition_digest(value):
    return digest(value)


def normalize_routine_ref(value):
    fields(value, set(ROUTINE_REF_FIELDS), set(ROUTINE_REF_FIELDS))
    return {
        "routine_id": routine_id(value["routine_id"]),
        "epoch": text(value["epoch"], "周期代号", 32, pattern=re.compile(r"^[a-f0-9]{32}$")),
        "occurrence_id": occurrence_id(value["occurrence_id"]),
        "definition_revision": integer(value["definition_revision"], "原周期版本"),
        "definition_sha256": text(value["definition_sha256"], "原周期摘要", 64, pattern=SHA),
        "actor_id": text(value["actor_id"], "原执行 Agent", 35, pattern=ACTOR),
        "workspace_key": text(value["workspace_key"], "原工作区", 64, pattern=SHA),
    }


def normalize_plan_ref(value):
    fields(value, set(PLAN_REF_FIELDS), set(PLAN_REF_FIELDS))
    return {
        "routine_id": routine_id(value["routine_id"]),
        "definition_revision": integer(value["definition_revision"], "原周期版本"),
        "definition_sha256": text(value["definition_sha256"], "原周期摘要", 64, pattern=SHA),
        "actor_id": text(value["actor_id"], "原执行 Agent", 35, pattern=ACTOR),
        "workspace_key": text(value["workspace_key"], "原工作区", 64, pattern=SHA),
    }


def slot_identity(owner_key, identity, epoch, slot, *, manual_request=None, slot_kind="scheduled"):
    core = {
        "version": 1,
        "owner_key": owner_key,
        "routine_id": routine_id(identity),
        "epoch": text(epoch, "周期代号", 32, pattern=re.compile(r"^[a-f0-9]{32}$")),
        "slot_kind": "manual" if manual_request is not None else slot_kind,
        "nominal_due_at": timestamp(slot["due_at"]).isoformat(),
    }
    if manual_request is not None:
        core["manual_request_id"] = request_id(manual_request)
    return "pro_" + digest(core)[:32], core


def money_microusd(value):
    if isinstance(value, bool):
        raise invalid("usage_unknown", "原费用证据格式不正确")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError()
        return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
        raise invalid("usage_unknown", "原费用证据格式不正确") from exc
