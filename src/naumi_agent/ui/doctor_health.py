"""Typed, bounded health model derived from deterministic Doctor checks."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.safety.guardrails import OutputGuardrail
from naumi_agent.ui.doctor import DoctorCheck, DoctorReport

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
) -> DoctorHealthSnapshot:
    """Convert one local Doctor report into the stable UI-13 health contract."""
    items = tuple(_health_item(check) for check in report.checks)
    status = _severity(report.status)
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
    if name in {"状态存储目录", "debug log 写入权限", "Doctor 运行时"}:
        return "product_runtime"
    return "local_environment"


def _severity(status: str) -> DoctorHealthSeverity:
    return {"pass": "ok", "warn": "degraded", "error": "error"}.get(status, "unknown")


def _bounded(value: object, limit: int) -> str:
    text = " ".join(OutputGuardrail.redact(str(value or "")).split())
    return text[:limit]


__all__ = [
    "DoctorHealthItem",
    "DoctorHealthSnapshot",
    "build_doctor_health_snapshot",
    "doctor_health_payload",
]
