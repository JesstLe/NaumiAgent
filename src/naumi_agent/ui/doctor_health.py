"""Typed, bounded health model derived from deterministic Doctor checks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.safety.guardrails import OutputGuardrail
from naumi_agent.ui.doctor import DoctorCheck, DoctorReport
from naumi_agent.ui.pursuit_recovery import PursuitRecoverySnapshot

DoctorHealthDomain = Literal[
    "runtime",
    "model",
    "provider",
    "store",
    "git",
    "node",
    "browser",
    "mcp",
    "terminal",
]
DoctorHealthSeverity = Literal["ok", "degraded", "error", "unknown"]
DoctorHealthResponsibility = Literal[
    "user_config",
    "local_environment",
    "external_service",
    "product_runtime",
    "unknown",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DoctorHealthItem(_StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    domain: DoctorHealthDomain
    label: str = Field(min_length=1, max_length=120)
    severity: DoctorHealthSeverity
    responsibility: DoctorHealthResponsibility
    detail: str = Field(max_length=500)
    suggestion: str = Field(default="", max_length=500)


class DoctorHealthSnapshot(_StrictModel):
    schema_version: Literal[1] = 1
    status: DoctorHealthSeverity
    generated_at: str
    live_probe: bool = False
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[DoctorHealthItem, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def _item_ids_are_unique(self) -> DoctorHealthSnapshot:
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Doctor Health item id 必须唯一。")
        return self


_DOMAIN_RULES: tuple[tuple[re.Pattern[str], DoctorHealthDomain], ...] = (
    (re.compile(r"^模型契约"), "model"),
    (re.compile(r"^(API key|model provider|模型实时连接)$"), "provider"),
    (re.compile(r"^(状态存储目录|debug log 写入权限)$"), "store"),
    (re.compile(r"^Worker authority$"), "runtime"),
    (re.compile(r"^git 状态$"), "git"),
    (re.compile(r"^Node\.js$"), "node"),
    (re.compile(r"^(browser daemon|网络搜索)$"), "browser"),
    (re.compile(r"^MCP servers$"), "mcp"),
    (re.compile(r"^terminal capability$"), "terminal"),
)


def build_doctor_health_snapshot(
    report: DoctorReport,
    *,
    live_probe: bool = False,
    generated_at: str | None = None,
    additional_items: Sequence[DoctorHealthItem] = (),
) -> DoctorHealthSnapshot:
    """Convert one local Doctor report into the stable UI-13 health contract."""
    items = (
        *(_health_item(check) for check in report.checks),
        *tuple(additional_items),
    )
    status = _worst_severity(
        (_severity(report.status), *(item.severity for item in additional_items))
    )
    canonical = {
        "schema_version": 1,
        "status": status,
        "live_probe": bool(live_probe),
        "items": [item.model_dump(mode="json") for item in items],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return DoctorHealthSnapshot(
        status=status,
        generated_at=generated_at or datetime.now(UTC).isoformat(),
        live_probe=live_probe,
        snapshot_sha256=digest,
        items=items,
    )


def pursuit_recovery_health_item(
    snapshot: PursuitRecoverySnapshot,
) -> DoctorHealthItem:
    """Project shared Pursuit recovery facts into the UI-13 health list."""
    severity: DoctorHealthSeverity = {
        "active": "ok",
        "waiting": "ok",
        "terminal": "ok",
        "blocked": "degraded",
        "unknown": "unknown",
        "reconcile_required": "error",
        "orphaned": "error",
        "inconsistent": "error",
    }[snapshot.recovery_state]
    detail = (
        f"状态 {_recovery_state_label(snapshot.recovery_state)}；"
        f"心跳 {_heartbeat_health_label(snapshot.heartbeat.health)}；"
        f"租约 {_lease_status_label(snapshot.lease.status)}；"
        f"Checkpoint {_checkpoint_status_label(snapshot.checkpoint.status)}。"
    )
    suggestion = {
        "reconcile_required": "打开 `/goal` 审查 reconcile reason，核对外部副作用后再恢复。",
        "orphaned": "运行状态缺少有效 lease；不要重复提交，先审查 checkpoint 与 worker。",
        "inconsistent": "Heartbeat 与 lease 不一致；运行 `/doctor` 并停止盲目恢复。",
        "blocked": "查看 Goal 页面中的阻塞原因与下一步。",
        "unknown": "刷新诊断；若持续未知，请检查 Harness Store。",
    }.get(snapshot.recovery_state, "")
    return DoctorHealthItem(
        id="runtime-pursuit-recovery",
        domain="runtime",
        label="Pursuit 恢复健康",
        severity=severity,
        responsibility="product_runtime",
        detail=_bounded(detail, 500),
        suggestion=_bounded(suggestion, 500),
    )


def doctor_health_payload(snapshot: DoctorHealthSnapshot) -> dict[str, object]:
    """Serialize the public contract; no raw configuration or exception object escapes."""
    return snapshot.model_dump(mode="json")


def _health_item(check: DoctorCheck) -> DoctorHealthItem:
    domain = _domain(check.name)
    stable_suffix = hashlib.sha256(check.name.encode("utf-8")).hexdigest()[:12]
    return DoctorHealthItem(
        id=f"{domain}-{stable_suffix}",
        domain=domain,
        label=_bounded(check.name, 120),
        severity=_severity(check.status),
        responsibility=_responsibility(check.name, check.status),
        detail=_bounded(check.detail, 500),
        suggestion=_bounded(check.suggestion, 500),
    )


def _domain(name: str) -> DoctorHealthDomain:
    for pattern, domain in _DOMAIN_RULES:
        if pattern.search(name):
            return domain
    return "runtime"


def _responsibility(
    name: str,
    status: str,
) -> DoctorHealthResponsibility:
    if status == "pass":
        return "unknown"
    if name in {"API key", "model provider", "config 文件"}:
        return "user_config"
    if name in {"模型实时连接", "browser daemon", "网络搜索", "MCP servers"}:
        return "external_service"
    if name in {
        "状态存储目录",
        "debug log 写入权限",
        "Doctor 运行时",
        "Worker authority",
    }:
        return "product_runtime"
    return "local_environment"


def _severity(status: str) -> DoctorHealthSeverity:
    return {"pass": "ok", "warn": "degraded", "error": "error"}.get(status, "unknown")


def _worst_severity(
    values: Sequence[DoctorHealthSeverity],
) -> DoctorHealthSeverity:
    rank = {"ok": 0, "unknown": 1, "degraded": 2, "error": 3}
    return max(values, key=lambda item: rank[item], default="unknown")


def _recovery_state_label(value: str) -> str:
    return {
        "active": "运行健康", "waiting": "安全等待", "blocked": "已阻塞",
        "reconcile_required": "需要核对", "orphaned": "疑似孤立",
        "inconsistent": "状态不一致", "terminal": "已终止", "unknown": "未知",
    }.get(value, value)


def _heartbeat_health_label(value: str) -> str:
    return {
        "starting": "启动中", "healthy": "健康", "draining": "排空中",
        "stale": "陈旧", "offline": "离线", "stopped": "已停止",
        "failed": "失败", "clock_regression": "时钟倒退", "missing": "缺失",
        "error": "读取失败",
    }.get(value, value)


def _lease_status_label(value: str) -> str:
    return {
        "active": "生效", "released": "已释放", "missing": "缺失",
        "error": "读取失败",
    }.get(value, value)


def _checkpoint_status_label(value: str) -> str:
    return {"ready": "可用", "missing": "缺失", "error": "校验失败"}.get(
        value, value,
    )


def _bounded(value: object, limit: int) -> str:
    text = " ".join(OutputGuardrail.redact(str(value or "")).split())
    return text[:limit]


__all__ = [
    "DoctorHealthItem",
    "DoctorHealthSnapshot",
    "build_doctor_health_snapshot",
    "doctor_health_payload",
    "pursuit_recovery_health_item",
]
