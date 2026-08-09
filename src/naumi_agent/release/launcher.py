"""Stable launcher that resolves and starts the active immutable runtime slot."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.release.slots import ReleaseSlotError, ReleaseSlotStore, host_release_target

RELEASE_LAUNCH_RESOLUTION_POLICY = "naumi-release-launch-resolution-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class ReleaseLaunchResolution(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )

    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-launch-resolution-v1"] = RELEASE_LAUNCH_RESOLUTION_POLICY
    resolution_id: str = Field(pattern=r"^rellaunch_[0-9a-f]{24}$")
    resolution_sha256: str = Field(pattern=_SHA256_RE)
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
    backend_path: str = Field(min_length=1, max_length=4096)
    argument_count: int = Field(ge=0, le=4096)
    arguments_persisted: Literal[False] = False
    active_chain_verified: Literal[True] = True
    manifest_verified: Literal[True] = True
    boot_receipt_verified: Literal[True] = True
    process_start_requested: bool
    process_start_authority: bool
    process_started: Literal[False] = False
    resolved_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.backend_path != str(Path(self.backend_path).expanduser().resolve()):
            raise ValueError("Launch Resolution backend_path 必须 canonical。")
        if datetime.fromisoformat(self.resolved_at).utcoffset() is None:
            raise ValueError("Launch Resolution resolved_at 必须包含 offset。")
        if self.process_start_authority is not self.process_start_requested:
            raise ValueError("Launch Resolution 启动授权必须与本次请求一致。")
        core = self.model_dump(mode="json", exclude={"resolution_id", "resolution_sha256"})
        digest = _digest(core)
        if self.resolution_sha256 != digest or self.resolution_id != (f"rellaunch_{digest[:24]}"):
            raise ValueError("Launch Resolution identity 不一致。")
        return self


def default_release_root() -> Path:
    configured = os.environ.get("NAUMI_INSTALL_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            return (Path(local) / "NaumiAgent").resolve()
        return (Path.home() / "AppData" / "Local" / "NaumiAgent").resolve()
    return (Path.home() / ".local" / "share" / "naumi-agent").resolve()


def resolve_launch(
    store: ReleaseSlotStore,
    *,
    argument_count: int,
    process_start_requested: bool = True,
    resolved_at: str | None = None,
) -> ReleaseLaunchResolution:
    if not 0 <= argument_count <= 4096:
        raise ReleaseSlotError("release_launch_arguments_oversized", "Launcher 参数数量超过 4096。")
    target = store.resolve_active_backend()
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_LAUNCH_RESOLUTION_POLICY,
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
        "backend_path": str(target.backend.resolve()),
        "argument_count": argument_count,
        "arguments_persisted": False,
        "active_chain_verified": True,
        "manifest_verified": True,
        "boot_receipt_verified": True,
        "process_start_requested": process_start_requested,
        "process_start_authority": process_start_requested,
        "process_started": False,
        "resolved_at": _aware(resolved_at or datetime.now(UTC).isoformat()).isoformat(),
    }
    digest = _digest(core)
    return ReleaseLaunchResolution.model_validate(
        {
            **core,
            "resolution_id": f"rellaunch_{digest[:24]}",
            "resolution_sha256": digest,
        }
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--launcher-self-test"]:
        print(
            json.dumps(
                {"ok": True, "component": "naumi-launcher", "target": host_release_target()},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    store = ReleaseSlotStore(default_release_root())
    if len(args) == 2 and args[0] == "--launcher-install":
        return _install_bundle(store, Path(args[1]))
    status_only = args == ["--launcher-status"]
    try:
        resolution = resolve_and_record_launch(
            store,
            argument_count=0 if status_only else len(args),
            process_start_requested=not status_only,
        )
    except (ReleaseSlotError, OSError, ValueError) as exc:
        code = getattr(exc, "code", "release_launcher_failed")
        print(f"Naumi 启动器错误 [{code}]：{exc}", file=sys.stderr)
        return 78
    if status_only:
        print(resolution.model_dump_json())
        return 0
    environment = {
        **os.environ,
        "NAUMI_ACTIVE_SLOT_ID": resolution.slot_id,
        "NAUMI_ACTIVE_POINTER_GENERATION": str(resolution.pointer_generation),
        "NAUMI_INSTALL_ROOT": str(store.release_root),
    }
    command = [resolution.backend_path, *args]
    if os.name != "nt":
        try:
            os.execve(resolution.backend_path, command, environment)
        except OSError as exc:
            print(f"Naumi 启动器无法启动 active runtime：{exc}", file=sys.stderr)
            return 78
        raise AssertionError("execve 返回了不可达控制流。")
    try:
        completed = subprocess.run(command, check=False, env=environment)
    except OSError as exc:
        print(f"Naumi 启动器无法启动 active runtime：{exc}", file=sys.stderr)
        return 78
    return int(completed.returncode)


def _install_bundle(store: ReleaseSlotStore, bundle: Path) -> int:
    try:
        slot = store.install(bundle)
        boot = store.verify_bootable(slot.slot_id)
        pointer = store.activate(slot.slot_id)
    except (ReleaseSlotError, OSError, ValueError) as exc:
        code = getattr(exc, "code", "release_install_failed")
        print(f"Naumi 安装激活失败 [{code}]：{exc}", file=sys.stderr)
        return 78
    print(
        json.dumps(
            {
                "ok": True,
                "component": "naumi-launcher",
                "action": "install-and-activate",
                "slot_id": slot.slot_id,
                "boot_receipt_id": boot.receipt_id,
                "pointer_id": pointer.pointer_id,
                "pointer_generation": pointer.generation,
                "version": slot.version,
                "target": slot.target,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def resolve_and_record_launch(
    store: ReleaseSlotStore,
    *,
    argument_count: int,
    process_start_requested: bool,
    resolved_at: str | None = None,
) -> ReleaseLaunchResolution:
    for attempt in range(3):
        resolution = resolve_launch(
            store,
            argument_count=argument_count,
            process_start_requested=process_start_requested,
            resolved_at=resolved_at,
        )
        try:
            store.record_launch_resolution(resolution)
        except ReleaseSlotError as exc:
            if exc.code == "release_launch_pointer_stale" and attempt < 2:
                continue
            raise
        return resolution
    raise AssertionError("Launcher resolution retry 控制流不可达。")


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Launch Resolution 时间必须包含 offset。")
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


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RELEASE_LAUNCH_RESOLUTION_POLICY",
    "ReleaseLaunchResolution",
    "default_release_root",
    "main",
    "resolve_and_record_launch",
    "resolve_launch",
]
