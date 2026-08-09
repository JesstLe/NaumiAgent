"""Content-addressed identity for a managed terminal runtime process."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.release.runtime_binding import (
    ReleaseRuntimeBindingError,
    verify_active_runtime_binding,
)
from naumi_agent.release.slots import ReleaseSlotStore

RELEASE_RUNTIME_IDENTITY_POLICY = "naumi-release-runtime-identity-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_BINDING_ENVIRONMENT = (
    "NAUMI_ACTIVE_SLOT_ID",
    "NAUMI_ACTIVE_POINTER_GENERATION",
    "NAUMI_INSTALL_ROOT",
)
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


class ReleaseRuntimeIdentity(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-runtime-identity-v1"] = (
        RELEASE_RUNTIME_IDENTITY_POLICY
    )
    identity_id: str = Field(pattern=r"^relruntimeidentity_[0-9a-f]{24}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    invocation_kind: Literal["terminal_session"] = "terminal_session"
    pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    pointer_sha256: str = Field(pattern=_SHA256_RE)
    pointer_generation: int = Field(ge=1)
    slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    slot_sha256: str = Field(pattern=_SHA256_RE)
    version: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    boot_receipt_sha256: str = Field(pattern=_SHA256_RE)
    binary_sha256: str = Field(pattern=_SHA256_RE)
    runtime_path: str = Field(min_length=1, max_length=4096)
    install_root: str = Field(min_length=1, max_length=4096)
    checks: tuple[str, ...] = Field(min_length=5, max_length=5)
    runtime_process_started: Literal[True] = True
    terminal_session_process: Literal[True] = True
    health_probe_process: Literal[False] = False
    verified_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.runtime_path != str(Path(self.runtime_path).expanduser().resolve()):
            raise ValueError("Runtime Identity runtime_path 必须 canonical。")
        if self.install_root != str(Path(self.install_root).expanduser().resolve()):
            raise ValueError("Runtime Identity install_root 必须 canonical。")
        if self.checks != _CHECKS:
            raise ValueError("Runtime Identity checks 必须是完整固定集合。")
        _aware(self.verified_at)
        core = self.model_dump(mode="json", exclude={"identity_id", "identity_sha256"})
        digest = _digest(core)
        if self.identity_sha256 != digest or self.identity_id != (
            f"relruntimeidentity_{digest[:24]}"
        ):
            raise ValueError("Runtime Identity content identity 不一致。")
        return self


class ReleaseRuntimeIdentityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def inspect_runtime_identity(
    store: ReleaseSlotStore,
    *,
    environment: Mapping[str, str],
    runtime_path: str | Path,
    verified_at: str | None = None,
) -> ReleaseRuntimeIdentity:
    try:
        binding = verify_active_runtime_binding(
            store,
            environment=environment,
            runtime_path=runtime_path,
        )
    except ReleaseRuntimeBindingError as exc:
        suffix = exc.code.removeprefix("release_runtime_binding_")
        raise ReleaseRuntimeIdentityError(
            f"release_runtime_identity_{suffix}",
            str(exc),
        ) from exc
    target = binding.target
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_RUNTIME_IDENTITY_POLICY,
        "invocation_kind": "terminal_session",
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
        "runtime_path": str(binding.runtime_path),
        "install_root": str(binding.install_root),
        "checks": list(_CHECKS),
        "runtime_process_started": True,
        "terminal_session_process": True,
        "health_probe_process": False,
        "verified_at": _aware(
            verified_at or datetime.now(UTC).isoformat()
        ).isoformat(),
    }
    digest = _digest(core)
    return ReleaseRuntimeIdentity.model_validate(
        {
            **core,
            "identity_id": f"relruntimeidentity_{digest[:24]}",
            "identity_sha256": digest,
        }
    )


def discover_runtime_identity(
    *,
    environment: Mapping[str, str],
    runtime_path: str | Path,
    verified_at: str | None = None,
) -> ReleaseRuntimeIdentity | None:
    values = {
        name: str(environment.get(name, "")).strip()
        for name in _BINDING_ENVIRONMENT
    }
    present = tuple(name for name, value in values.items() if value)
    if not present:
        return None
    if len(present) != len(_BINDING_ENVIRONMENT):
        raise ReleaseRuntimeIdentityError(
            "release_runtime_identity_environment_incomplete",
            "Managed Runtime 环境绑定不完整，不能签发 terminal identity。",
        )
    return inspect_runtime_identity(
        ReleaseSlotStore(values["NAUMI_INSTALL_ROOT"]),
        environment=environment,
        runtime_path=runtime_path,
        verified_at=verified_at,
    )


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Runtime Identity timestamp 必须包含时区。")
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
    "RELEASE_RUNTIME_IDENTITY_POLICY",
    "ReleaseRuntimeIdentity",
    "ReleaseRuntimeIdentityError",
    "discover_runtime_identity",
    "inspect_runtime_identity",
]
