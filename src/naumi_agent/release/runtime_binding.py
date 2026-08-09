"""Shared exact active-runtime self-verification primitive."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from naumi_agent.release.slots import (
    ReleaseSlotError,
    ReleaseSlotStore,
    ResolvedReleaseSlot,
)


@dataclass(frozen=True)
class VerifiedActiveRuntime:
    target: ResolvedReleaseSlot
    runtime_path: Path
    install_root: Path


class ReleaseRuntimeBindingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def verify_active_runtime_binding(
    store: ReleaseSlotStore,
    *,
    environment: Mapping[str, str],
    runtime_path: str | Path,
) -> VerifiedActiveRuntime:
    if not isinstance(store, ReleaseSlotStore):
        raise TypeError("Runtime Binding 需要 ReleaseSlotStore。")
    path = Path(runtime_path).expanduser().resolve()
    try:
        target = store.resolve_active_backend()
    except (OSError, TypeError, ValueError, ReleaseSlotError) as exc:
        raise ReleaseRuntimeBindingError(
            "release_runtime_binding_active_invalid",
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
        raise ReleaseRuntimeBindingError(
            "release_runtime_binding_environment_mismatch",
            "Runtime 环境未绑定 exact active slot、pointer generation 与 install root。",
        )
    if os.path.normcase(str(path)) != os.path.normcase(
        str(target.backend.resolve())
    ):
        raise ReleaseRuntimeBindingError(
            "release_runtime_binding_binary_mismatch",
            "执行中的 Runtime 不是 active pointer 绑定的 exact binary。",
        )
    return VerifiedActiveRuntime(
        target=target,
        runtime_path=target.backend.resolve(),
        install_root=store.release_root,
    )


__all__ = [
    "ReleaseRuntimeBindingError",
    "VerifiedActiveRuntime",
    "verify_active_runtime_binding",
]
