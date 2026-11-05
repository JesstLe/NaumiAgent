"""Strict durable references; followup facts never replace original domain authority."""

from __future__ import annotations

import re

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical, digest, integer, text

SHA = re.compile(r"^[a-f0-9]{64}$")
ACTOR = re.compile(r"^(main|pa_[a-f0-9]{32})$")
PREFIXES = frozenset({"fr_", "fp_", "fd_", "fe_", "fl_", "fc_"})


def error(code="source_unavailable"):
    messages = {
        "invalid_input": "跟进字段不完整或格式不正确",
        "scope_denied": "请由用户在跟进入口确认这项操作",
        "source_unavailable": "原跟进来源暂不可读，请核对资料与分享范围",
        "source_stale": "原截止、事件或规则已变化，请刷新后重新核对",
        "version_conflict": "跟进或目标状态已更新，请刷新后核对原操作",
        "not_found": "这条跟进记录不在当前范围",
        "storage_error": "跟进与原资料库不一致，请恢复原目录后继续",
        "input_limit": "本次跟进超过读取容量，请缩小范围",
        "followup_busy": "跟进正在更新，请稍后核对原请求",
        "followup_unknown": "操作结果待核对，请查询原请求编号",
        "idempotency_conflict": "原请求编号已绑定其他内容，请核对原请求",
        "clock_rollback": "系统时间发生回退，新的跟进提示已暂停",
        "timezone_data_changed": "时区规则已变化，请重新核对原截止和跟进规则",
        "calendar_stale": "日历镜像需要重新同步，当前建议已暂停",
        "configuration_error": "跟进服务尚未就绪，请核对运行时配置",
    }
    return PersonalAgentError(
        code if code in messages else "source_unavailable",
        messages.get(code, messages["source_unavailable"]),
    )


def identity(prefix, value):
    if prefix not in PREFIXES or type(value) is not dict:
        raise error("invalid_input")
    return prefix + digest({"domain": "personal_followups_v1", "kind": prefix, "value": value})[:32]


def record_digest(value):
    return digest({key: val for key, val in value.items() if key != "digest"})


def reference(value, kind):
    names = {
        "proposal": ("proposal_id", "fp_"),
        "read": ("read_id", "fl_"),
        "effect": ("effect_id", "fe_"),
    }
    field, _ = names[kind]
    return {
        field: value["id"],
        "revision": value["revision"],
        "digest": record_digest(value),
        "workspace_key": value["workspace_key"],
    }


def normalize_reference(raw, kind):
    field, prefix = {
        "proposal": ("proposal_id", "fp_"),
        "read": ("read_id", "fl_"),
        "effect": ("effect_id", "fe_"),
    }[kind]
    if type(raw) is not dict or set(raw) != {field, "revision", "digest", "workspace_key"}:
        raise error("invalid_input")
    canonical(raw)
    text(raw[field], "跟进来源编号", 35, pattern=re.compile(prefix + "[a-f0-9]{32}"))
    integer(raw["revision"], "原跟进来源版本", minimum=1, maximum=1)
    for key in ("digest", "workspace_key"):
        text(raw[key], "跟进来源摘要", 64, pattern=SHA)
    return dict(raw)


def rule_reference(value):
    return {"id": value["id"], "revision": value["revision"], "digest": record_digest(value)}


def rule_view(value):
    return {
        key: value[key]
        for key in (
            "id",
            "workspace_key",
            "revision",
            "body",
            "epoch",
            "cutover_at",
            "source_binding",
            "created_at",
            "updated_at",
        )
    } | {"digest": record_digest(value)}


def decision_view(value):
    return {
        key: value[key]
        for key in (
            "revision",
            "status",
            "presentation_epoch",
            "snooze_until",
            "effect_id",
        )
    }
