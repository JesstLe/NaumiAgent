"""Machine-readable self-verification contract for an active release runtime."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.release.slots import ReleaseSlotError, ReleaseSlotStore

RELEASE_RUNTIME_HEALTH_POLICY = "naumi-release-runtime-health-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_SLOT_RE = r"^relslot_[0-9a-f]{24}$"
_MAX_REPORT_BYTES = 64 * 1024
_CHECKS = (
    "active_chain_verified",
    "manifest_verified",
    "boot_receipt_verified",
    "runtime_binary_verified",
    "environment_binding_verified",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ReleaseRuntimeHealthReport(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-runtime-health-v1"] = (
        RELEASE_RUNTIME_HEALTH_POLICY
    )
    report_id: str = Field(pattern=r"^relruntimehealth_[0-9a-f]{24}$")
    report_sha256: str = Field(pattern=_SHA256_RE)
    component: Literal["naumi-runtime"] = "naumi-runtime"
    status: Literal["healthy"] = "healthy"
    pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    pointer_sha256: str = Field(pattern=_SHA256_RE)
    pointer_generation: int = Field(ge=1)
    slot_id: str = Field(pattern=_SLOT_RE)
    slot_sha256: str = Field(pattern=_SHA256_RE)
    version: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    boot_receipt_sha256: str = Field(pattern=_SHA256_RE)
    binary_sha256: str = Field(pattern=_SHA256_RE)
    runtime_path: str = Field(min_length=1, max_length=4096)
    install_root: str = Field(min_length=1, max_length=4096)
    command: tuple[Literal["--runtime-health-check"], ...] = Field(
        default=("--runtime-health-check",), min_length=1, max_length=1
    )
    checks: tuple[str, ...] = Field(min_length=5, max_length=5)
    process_started: Literal[True] = True
    user_session_started: Literal[False] = False
    checked_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.runtime_path != str(Path(self.runtime_path).expanduser().resolve()):
            raise ValueError("Runtime Health runtime_path 必须 canonical。")
        if self.install_root != str(Path(self.install_root).expanduser().resolve()):
            raise ValueError("Runtime Health install_root 必须 canonical。")
        if self.checks != _CHECKS:
            raise ValueError("Runtime Health checks 必须是完整固定集合。")
        _aware(self.checked_at)
        core = self.model_dump(mode="json", exclude={"report_id", "report_sha256"})
        digest = _digest(core)
        if self.report_sha256 != digest or self.report_id != (
            f"relruntimehealth_{digest[:24]}"
        ):
            raise ValueError("Runtime Health identity 不一致。")
        return self


class ReleaseRuntimeHealthError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def inspect_runtime_health(
    store: ReleaseSlotStore,
    *,
    environment: Mapping[str, str],
    runtime_path: str | Path,
    checked_at: str | None = None,
) -> ReleaseRuntimeHealthReport:
    if not isinstance(store, ReleaseSlotStore):
        raise TypeError("Runtime Health 需要 ReleaseSlotStore。")
    path = Path(runtime_path).expanduser().resolve()
    try:
        target = store.resolve_active_backend()
    except (OSError, TypeError, ValueError, ReleaseSlotError) as exc:
        raise ReleaseRuntimeHealthError(
            "release_runtime_health_active_invalid",
            "Runtime 无法验证 active release chain。",
        ) from exc
    expected_environment = {
        "NAUMI_ACTIVE_SLOT_ID": target.slot.slot_id,
        "NAUMI_ACTIVE_POINTER_GENERATION": str(target.pointer.generation),
        "NAUMI_INSTALL_ROOT": str(store.release_root),
    }
    actual_environment = {
        name: str(environment.get(name, "")).strip()
        for name in expected_environment
    }
    if actual_environment != expected_environment:
        raise ReleaseRuntimeHealthError(
            "release_runtime_health_environment_mismatch",
            "Runtime 环境未绑定 exact active slot、pointer generation 与 install root。",
        )
    if os.path.normcase(str(path)) != os.path.normcase(str(target.backend.resolve())):
        raise ReleaseRuntimeHealthError(
            "release_runtime_health_binary_mismatch",
            "执行中的 Runtime 不是 active pointer 绑定的 exact binary。",
        )
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_RUNTIME_HEALTH_POLICY,
        "component": "naumi-runtime",
        "status": "healthy",
        "pointer_id": target.pointer.pointer_id,
        "pointer_sha256": target.pointer.pointer_sha256,
        "pointer_generation": target.pointer.generation,
        "slot_id": target.slot.slot_id,
        "slot_sha256": target.slot.slot_sha256,
        "version": target.slot.version,
        "target": target.slot.target,
        "boot_receipt_id": target.boot_receipt.receipt_id,
        "boot_receipt_sha256": target.boot_receipt.receipt_sha256,
        "binary_sha256": target.boot_receipt.binary_sha256,
        "runtime_path": str(target.backend.resolve()),
        "install_root": str(store.release_root),
        "command": ["--runtime-health-check"],
        "checks": list(_CHECKS),
        "process_started": True,
        "user_session_started": False,
        "checked_at": _aware(
            checked_at or datetime.now(UTC).isoformat()
        ).isoformat(),
    }
    digest = _digest(core)
    return ReleaseRuntimeHealthReport.model_validate(
        {
            **core,
            "report_id": f"relruntimehealth_{digest[:24]}",
            "report_sha256": digest,
        }
    )


def parse_runtime_health_report(payload: str | bytes) -> ReleaseRuntimeHealthReport:
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if not raw or len(raw) > _MAX_REPORT_BYTES:
        raise ReleaseRuntimeHealthError(
            "release_runtime_health_output_size",
            "Runtime Health 输出为空或超过 64 KiB。",
        )
    try:
        text = raw.decode("utf-8")
        decoded = json.loads(text)
        if not isinstance(decoded, dict):
            raise TypeError("report must be an object")
        return ReleaseRuntimeHealthReport.model_validate(decoded)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReleaseRuntimeHealthError(
            "release_runtime_health_output_invalid",
            "Runtime Health 输出不是严格、完整的可信 JSON report。",
        ) from exc


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Runtime Health timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "RELEASE_RUNTIME_HEALTH_POLICY",
    "ReleaseRuntimeHealthError",
    "ReleaseRuntimeHealthReport",
    "inspect_runtime_health",
    "parse_runtime_health_report",
]
