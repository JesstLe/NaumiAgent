"""Crash-safe installed release slots with boot checks and atomic activation."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Self

if TYPE_CHECKING:
    from naumi_agent.release.launcher import ReleaseLaunchResolution

from pydantic import BaseModel, ConfigDict, Field, model_validator

RELEASE_SLOT_POLICY = "naumi-release-slot-v1"
RELEASE_BOOT_RECEIPT_POLICY = "naumi-release-boot-receipt-v1"
RELEASE_ACTIVE_POINTER_POLICY = "naumi-release-active-pointer-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_LABEL_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_FILES = 100_000
_MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
_BOOT_TIMEOUT_SECONDS = 20
_FORBIDDEN_COMPONENTS = frozenset({".git", "__pycache__", "docs", "frontend", "src", "tests"})
_FORBIDDEN_NAMES = frozenset({"package.json", "pyproject.toml", "manifest.in", "uv.lock"})
_FORBIDDEN_SUFFIXES = (".py", ".pyc", ".pyo", ".js", ".jsx", ".ts", ".tsx", ".map", ".ipynb")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class ReleaseInstalledSlot(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-slot-v1"] = RELEASE_SLOT_POLICY
    slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    slot_sha256: str = Field(pattern=_SHA256_RE)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    version: str = Field(pattern=_SAFE_LABEL_RE)
    target: str = Field(pattern=_SAFE_LABEL_RE)
    source_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_tree_sha256: str = Field(pattern=_SHA256_RE)
    bundle_dir: str = Field(min_length=1, max_length=4096)
    backend_path: str = Field(pattern=r"^naumi-runtime(?:\.exe)?$")
    ui_path: str = Field(pattern=r"^naumi-ui(?:\.exe)?$")
    file_count: int = Field(ge=3, le=_MAX_FILES)
    total_bytes: int = Field(gt=0, le=_MAX_TOTAL_BYTES)
    immutable: Literal[True] = True
    boot_authority: Literal[False] = False
    activation_authority: Literal[False] = False
    installed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.bundle_dir != str(Path(self.bundle_dir).expanduser().resolve()):
            raise ValueError("Release Slot bundle_dir 必须 canonical。")
        windows = self.target.casefold().startswith("windows-")
        if not (
            self.backend_path == ("naumi-runtime.exe" if windows else "naumi-runtime")
            and self.ui_path == ("naumi-ui.exe" if windows else "naumi-ui")
        ):
            raise ValueError("Release Slot 入口与 target 不一致。")
        _aware(self.installed_at)
        core = self.model_dump(mode="json", exclude={"slot_id", "slot_sha256"})
        digest = _digest(core)
        if self.slot_sha256 != digest or self.slot_id != (f"relslot_{self.manifest_sha256[:24]}"):
            raise ValueError("Release Slot identity 不一致。")
        return self


class ReleaseSlotBootReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-boot-receipt-v1"] = RELEASE_BOOT_RECEIPT_POLICY
    receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    slot_sha256: str = Field(pattern=_SHA256_RE)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    binary_sha256: str = Field(pattern=_SHA256_RE)
    arguments: tuple[Literal["--version"], ...] = Field(
        default=("--version",), min_length=1, max_length=1
    )
    version_output: str = Field(min_length=1, max_length=4096)
    output_sha256: str = Field(pattern=_SHA256_RE)
    output_bytes: int = Field(ge=1, le=64 * 1024)
    exit_code: Literal[0] = 0
    duration_ms: int = Field(ge=0, le=_BOOT_TIMEOUT_SECONDS * 1000)
    version_matched: Literal[True] = True
    bootable: Literal[True] = True
    activation_input_authority: Literal[True] = True
    checked_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.checked_at)
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != f"relboot_{digest[:24]}":
            raise ValueError("Release Boot Receipt identity 不一致。")
        return self


class ReleaseActivationAuthority(_StrictModel):
    kind: Literal["evolution_opt_in_deployment_intent"]
    authority_id: str = Field(pattern=r"^evredeployintent_[0-9a-f]{24}$")
    authority_sha256: str = Field(pattern=_SHA256_RE)


class ReleaseActivePointer(_StrictModel):
    schema_version: Literal[1, 2] = 1
    policy_version: Literal["naumi-release-active-pointer-v1"] = RELEASE_ACTIVE_POINTER_POLICY
    pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    pointer_sha256: str = Field(pattern=_SHA256_RE)
    generation: int = Field(ge=1)
    previous_pointer_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    action: Literal["activate", "rollback"]
    current_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    current_slot_sha256: str = Field(pattern=_SHA256_RE)
    previous_slot_id: str | None = Field(default=None, pattern=r"^relslot_[0-9a-f]{24}$")
    previous_slot_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    boot_receipt_sha256: str = Field(pattern=_SHA256_RE)
    activation_authority: ReleaseActivationAuthority | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    atomic_switch_satisfied: Literal[True] = True
    old_slot_retained: Literal[True] = True
    activated_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if (self.schema_version == 1) is not (self.activation_authority is None):
            raise ValueError("Release Active Pointer authority schema 不一致。")
        if self.activation_authority is not None and self.action != "activate":
            raise ValueError("Release Active Pointer rollback 不得携带 activation authority。")
        if (self.previous_slot_id is None) is not (self.previous_slot_sha256 is None):
            raise ValueError("Release Active Pointer previous slot 投影不一致。")
        if self.generation == 1 and (
            self.previous_pointer_sha256 is not None or self.previous_slot_id is not None
        ):
            raise ValueError("首个 Release Active Pointer 不得声明 previous。")
        if self.generation > 1 and self.previous_pointer_sha256 is None:
            raise ValueError("后续 Release Active Pointer 必须链接 previous。")
        _aware(self.activated_at)
        core = self.model_dump(mode="json", exclude={"pointer_id", "pointer_sha256"})
        digest = _digest(core)
        if self.pointer_sha256 != digest or self.pointer_id != f"relactive_{digest[:24]}":
            raise ValueError("Release Active Pointer identity 不一致。")
        return self


class ReleaseSlotError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedReleaseSlot:
    pointer: ReleaseActivePointer
    slot: ReleaseInstalledSlot
    boot_receipt: ReleaseSlotBootReceipt
    backend: Path


@dataclass(frozen=True)
class ResolvedBootedReleaseSlot:
    slot: ReleaseInstalledSlot
    boot_receipt: ReleaseSlotBootReceipt
    backend: Path


class ReleaseSlotStore:
    def __init__(self, release_root: str | Path) -> None:
        self.release_root = Path(release_root).expanduser().resolve()
        self.slots_dir = self.release_root / "slots"
        self.db_path = self.release_root / "release-slots.db"

    def install(
        self,
        bundle_dir: str | Path,
        *,
        installed_at: str | None = None,
        expected_manifest_sha256: str | None = None,
    ) -> ReleaseInstalledSlot:
        source = Path(bundle_dir).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ReleaseSlotError("release_bundle_missing", "发行 bundle 目录不存在。")
        if _overlaps(source, self.release_root):
            raise ReleaseSlotError(
                "release_bundle_overlaps_slots",
                "发行 bundle 不得与版本槽目录重叠。",
            )
        manifest, manifest_sha, file_count, total_bytes = _verify_bundle(
            source,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        installed = _aware(installed_at or datetime.now(UTC).isoformat()).isoformat()
        target = manifest["target"]
        windows = str(target).casefold().startswith("windows-")
        slot_id = f"relslot_{manifest_sha[:24]}"
        slot_dir = (self.slots_dir / slot_id).resolve()
        core = {
            "schema_version": 1,
            "policy_version": RELEASE_SLOT_POLICY,
            "manifest_sha256": manifest_sha,
            "version": manifest["version"],
            "target": target,
            "source_commit": manifest["source_commit"],
            "source_tree_sha256": manifest["source_tree_sha256"],
            "bundle_dir": str(slot_dir),
            "backend_path": "naumi-runtime.exe" if windows else "naumi-runtime",
            "ui_path": "naumi-ui.exe" if windows else "naumi-ui",
            "file_count": file_count,
            "total_bytes": total_bytes,
            "immutable": True,
            "boot_authority": False,
            "activation_authority": False,
            "installed_at": installed,
        }
        digest = _digest(core)
        slot = ReleaseInstalledSlot.model_validate(
            {**core, "slot_id": slot_id, "slot_sha256": digest}
        )
        existing = self.get_slot(slot.slot_id)
        if existing is not None:
            if existing.manifest_sha256 != manifest_sha:
                raise ReleaseSlotError("release_slot_conflict", "版本槽 identity 冲突。")
            _verify_bundle(Path(existing.bundle_dir), expected_manifest_sha256=manifest_sha)
            _make_immutable(Path(existing.bundle_dir))
            _verify_immutable(Path(existing.bundle_dir))
            return existing
        self.slots_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".slot-", dir=self.slots_dir))
        staged_bundle = staging / "bundle"
        try:
            shutil.copytree(source, staged_bundle, symlinks=True)
            _verify_bundle(staged_bundle, expected_manifest_sha256=manifest_sha)
            _fsync_tree(staged_bundle)
            try:
                os.rename(staged_bundle, slot_dir)
                _fsync_directory(self.slots_dir)
            except OSError:
                if not slot_dir.is_dir():
                    raise
                _verify_bundle(slot_dir, expected_manifest_sha256=manifest_sha)
            _make_immutable(slot_dir)
            _fsync_tree(slot_dir)
            _verify_bundle(slot_dir, expected_manifest_sha256=manifest_sha)
            _verify_immutable(slot_dir)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT slot_json FROM release_slots WHERE slot_id = ?", (slot.slot_id,)
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO release_slots "
                    "(slot_id, slot_sha256, manifest_sha256, slot_json, installed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        slot.slot_id,
                        slot.slot_sha256,
                        slot.manifest_sha256,
                        slot.model_dump_json(),
                        slot.installed_at,
                    ),
                )
                db.commit()
                return slot
            restored = ReleaseInstalledSlot.model_validate_json(row["slot_json"])
            db.rollback()
        if not (
            restored.manifest_sha256 == slot.manifest_sha256
            and restored.bundle_dir == slot.bundle_dir
        ):
            raise ReleaseSlotError("release_slot_conflict", "版本槽并发安装结果冲突。")
        return restored

    def verify_bootable(
        self,
        slot_id: str,
        *,
        checked_at: str | None = None,
        timeout_seconds: int = _BOOT_TIMEOUT_SECONDS,
    ) -> ReleaseSlotBootReceipt:
        slot = self._require_slot(slot_id)
        _require_host_target(slot.target)
        _verify_bundle(Path(slot.bundle_dir), expected_manifest_sha256=slot.manifest_sha256)
        _verify_immutable(Path(slot.bundle_dir))
        binary = Path(slot.bundle_dir) / slot.backend_path
        binary_sha = _sha256_file(binary)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [str(binary), "--version"],
                check=False,
                capture_output=True,
                timeout=max(1, min(timeout_seconds, _BOOT_TIMEOUT_SECONDS)),
                env={**os.environ, "NAUMI_RELEASE_BOOT_CHECK": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            raise ReleaseSlotError(
                "release_slot_boot_timeout",
                "版本槽启动探测超过允许时间。",
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseSlotError("release_slot_boot_failed", "版本槽启动探测无法执行。") from exc
        duration = min(int((time.monotonic() - started) * 1000), _BOOT_TIMEOUT_SECONDS * 1000)
        output = completed.stdout + completed.stderr
        if len(output) > 64 * 1024:
            raise ReleaseSlotError(
                "release_slot_boot_output_oversized", "启动探测输出超过 64 KiB。"
            )
        version_pattern = re.compile(
            rb"(?<![A-Za-z0-9._-])" + re.escape(slot.version.encode()) + rb"(?![A-Za-z0-9._-])"
        )
        try:
            version_output = output.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ReleaseSlotError(
                "release_slot_boot_output_invalid", "启动探测输出不是有效 UTF-8。"
            ) from exc
        if (
            completed.returncode != 0
            or not version_output
            or len(version_output) > 4096
            or version_pattern.search(output) is None
        ):
            raise ReleaseSlotError(
                "release_slot_not_bootable",
                "版本槽启动探测失败，或输出版本与 manifest 不一致。",
            )
        core = {
            "schema_version": 1,
            "policy_version": RELEASE_BOOT_RECEIPT_POLICY,
            "slot_id": slot.slot_id,
            "slot_sha256": slot.slot_sha256,
            "manifest_sha256": slot.manifest_sha256,
            "binary_sha256": binary_sha,
            "arguments": ["--version"],
            "version_output": version_output,
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "output_bytes": len(output),
            "exit_code": 0,
            "duration_ms": duration,
            "version_matched": True,
            "bootable": True,
            "activation_input_authority": True,
            "checked_at": _aware(checked_at or datetime.now(UTC).isoformat()).isoformat(),
        }
        digest = _digest(core)
        receipt = ReleaseSlotBootReceipt.model_validate(
            {**core, "receipt_id": f"relboot_{digest[:24]}", "receipt_sha256": digest}
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO release_boot_receipts "
                "(receipt_id, receipt_sha256, slot_id, receipt_json, checked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.receipt_sha256,
                    receipt.slot_id,
                    receipt.model_dump_json(),
                    receipt.checked_at,
                ),
            )
            db.commit()
        return receipt

    def activate(
        self,
        slot_id: str,
        *,
        activated_at: str | None = None,
        action: Literal["activate", "rollback"] = "activate",
        _expected_pointer_sha256: str | None = None,
        _activation_authority: ReleaseActivationAuthority | None = None,
    ) -> ReleaseActivePointer:
        if _activation_authority is not None:
            _activation_authority = ReleaseActivationAuthority.model_validate_json(
                _activation_authority.model_dump_json()
            )
        if action == "rollback" and _activation_authority is not None:
            raise ReleaseSlotError(
                "release_activation_authority_forbidden",
                "Rollback 不接受 activation authority。",
            )
        slot = self._require_slot(slot_id)
        _require_host_target(slot.target)
        _verify_bundle(Path(slot.bundle_dir), expected_manifest_sha256=slot.manifest_sha256)
        _verify_immutable(Path(slot.bundle_dir))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._validated_active(db)
            if _expected_pointer_sha256 is not None and (
                current is None or current.pointer_sha256 != _expected_pointer_sha256
            ):
                db.rollback()
                raise ReleaseSlotError(
                    "release_active_pointer_conflict",
                    "Active version pointer 已被其他执行者推进。",
                )
            if current is not None and current.current_slot_id == slot.slot_id:
                db.rollback()
                return current
            if action == "rollback" and (
                current is None or current.previous_slot_id != slot.slot_id
            ):
                db.rollback()
                raise ReleaseSlotError(
                    "release_slot_rollback_target_mismatch",
                    "Rollback 目标不是 current pointer 的 previous slot。",
                )
            if current is not None:
                previous = self._slot_row(db, current.current_slot_id)
                if previous is None:
                    db.rollback()
                    raise ReleaseSlotError(
                        "release_previous_slot_missing",
                        "Active pointer 的旧版本槽不存在。",
                    )
                _verify_bundle(
                    Path(previous.bundle_dir),
                    expected_manifest_sha256=previous.manifest_sha256,
                )
                _verify_immutable(Path(previous.bundle_dir))
            boot = self._latest_boot(db, slot.slot_id)
            if not (
                boot is not None
                and boot.slot_sha256 == slot.slot_sha256
                and boot.binary_sha256 == _sha256_file(Path(slot.bundle_dir) / slot.backend_path)
            ):
                db.rollback()
                raise ReleaseSlotError(
                    "release_slot_boot_receipt_missing",
                    "版本槽缺少 current bootability receipt。",
                )
            core = {
                "schema_version": 2 if _activation_authority is not None else 1,
                "policy_version": RELEASE_ACTIVE_POINTER_POLICY,
                "generation": 1 if current is None else current.generation + 1,
                "previous_pointer_sha256": None if current is None else current.pointer_sha256,
                "action": action,
                "current_slot_id": slot.slot_id,
                "current_slot_sha256": slot.slot_sha256,
                "previous_slot_id": None if current is None else current.current_slot_id,
                "previous_slot_sha256": None if current is None else current.current_slot_sha256,
                "boot_receipt_id": boot.receipt_id,
                "boot_receipt_sha256": boot.receipt_sha256,
                **(
                    {
                        "activation_authority": _activation_authority.model_dump(
                            mode="json"
                        )
                    }
                    if _activation_authority is not None
                    else {}
                ),
                "atomic_switch_satisfied": True,
                "old_slot_retained": True,
                "activated_at": _aware(activated_at or datetime.now(UTC).isoformat()).isoformat(),
            }
            digest = _digest(core)
            pointer = ReleaseActivePointer.model_validate(
                {
                    **core,
                    "pointer_id": f"relactive_{digest[:24]}",
                    "pointer_sha256": digest,
                }
            )
            db.execute(
                "INSERT INTO release_active_events "
                "(pointer_id, pointer_sha256, generation, pointer_json, activated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    pointer.pointer_id,
                    pointer.pointer_sha256,
                    pointer.generation,
                    pointer.model_dump_json(),
                    pointer.activated_at,
                ),
            )
            db.execute(
                "INSERT INTO release_active_pointer(singleton, pointer_json) VALUES (1, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET pointer_json = excluded.pointer_json",
                (pointer.model_dump_json(),),
            )
            db.commit()
        return pointer

    def rollback(self, *, activated_at: str | None = None) -> ReleaseActivePointer:
        current = self.active()
        if current is None or current.previous_slot_id is None:
            raise ReleaseSlotError(
                "release_slot_rollback_unavailable", "当前没有可回滚的 previous version slot。"
            )
        return self.activate(
            current.previous_slot_id,
            activated_at=activated_at,
            action="rollback",
            _expected_pointer_sha256=current.pointer_sha256,
        )

    def active(self) -> ReleaseActivePointer | None:
        if not self.db_path.is_file():
            return None
        with self._connect() as db:
            return self._validated_active(db)

    def get_activation_event(self, generation: int) -> ReleaseActivePointer | None:
        """Resolve one immutable event only after validating the complete chain."""
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise TypeError("Release activation generation 必须是整数。")
        if not 1 <= generation <= 1_000_000_000:
            raise ValueError("Release activation generation 超出支持范围。")
        if not self.db_path.is_file():
            return None
        with self._connect() as db:
            db.execute("BEGIN")
            self._validated_active(db)
            row = db.execute(
                "SELECT pointer_json FROM release_active_events WHERE generation = ?",
                (generation,),
            ).fetchone()
            db.rollback()
        return (
            None
            if row is None
            else ReleaseActivePointer.model_validate_json(row["pointer_json"])
        )

    def resolve_active_backend(self) -> ResolvedReleaseSlot:
        with self._connect() as db:
            db.execute("BEGIN")
            pointer = self._validated_active(db)
            if pointer is None:
                db.rollback()
                raise ReleaseSlotError(
                    "release_active_pointer_missing", "尚未激活任何 Naumi 版本槽。"
                )
            slot = self._slot_row(db, pointer.current_slot_id)
            boot = self._boot_row(db, pointer.boot_receipt_id)
            db.rollback()
        if slot is None or slot.slot_sha256 != pointer.current_slot_sha256:
            raise ReleaseSlotError(
                "release_active_slot_missing", "Active pointer 绑定的版本槽不存在或摘要不符。"
            )
        _require_host_target(slot.target)
        bundle = Path(slot.bundle_dir)
        _verify_bundle(bundle, expected_manifest_sha256=slot.manifest_sha256)
        _verify_immutable(bundle)
        backend = bundle / slot.backend_path
        if not (
            boot is not None
            and boot.slot_sha256 == slot.slot_sha256
            and boot.manifest_sha256 == slot.manifest_sha256
            and boot.receipt_sha256 == pointer.boot_receipt_sha256
            and boot.binary_sha256 == _sha256_file(backend)
        ):
            raise ReleaseSlotError(
                "release_active_boot_receipt_stale",
                "Active slot 缺少与当前 runtime bytes 匹配的 Boot Receipt。",
            )
        return ResolvedReleaseSlot(
            pointer=pointer,
            slot=slot,
            boot_receipt=boot,
            backend=backend,
        )

    def record_launch_resolution(self, resolution) -> None:
        encoded = resolution.model_dump_json()
        if len(encoded.encode()) > 64 * 1024:
            raise ReleaseSlotError(
                "release_launch_resolution_oversized", "Launch Resolution 超过 64 KiB。"
            )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            dependency = self._validated_active(db)
            slot = None if dependency is None else self._slot_row(db, dependency.current_slot_id)
            boot = None if dependency is None else self._boot_row(db, dependency.boot_receipt_id)
            if dependency is None or dependency.pointer_sha256 != resolution.pointer_sha256:
                db.rollback()
                raise ReleaseSlotError(
                    "release_launch_pointer_stale",
                    "Launch Resolution 绑定的 Active Pointer 已变化。",
                )
            backend = None if slot is None else Path(slot.bundle_dir) / slot.backend_path
            if not (
                slot is not None
                and boot is not None
                and resolution.pointer_id == dependency.pointer_id
                and resolution.pointer_generation == dependency.generation
                and resolution.slot_id == slot.slot_id
                and resolution.slot_sha256 == slot.slot_sha256
                and resolution.boot_receipt_id == boot.receipt_id
                and resolution.boot_receipt_sha256 == boot.receipt_sha256
                and boot.receipt_sha256 == dependency.boot_receipt_sha256
                and resolution.binary_sha256 == boot.binary_sha256
                and resolution.backend_path == str(backend.resolve())
                and _sha256_file(backend) == boot.binary_sha256
            ):
                db.rollback()
                raise ReleaseSlotError(
                    "release_launch_dependency_mismatch",
                    "Launch Resolution 与 active slot/boot dependency 不一致。",
                )
            db.execute(
                "INSERT OR IGNORE INTO release_launch_resolutions "
                "(resolution_id, resolution_sha256, pointer_sha256, resolution_json, "
                "resolved_at) VALUES (?, ?, ?, ?, ?)",
                (
                    resolution.resolution_id,
                    resolution.resolution_sha256,
                    resolution.pointer_sha256,
                    encoded,
                    resolution.resolved_at,
                ),
            )
            db.commit()

    def get_launch_resolution(
        self,
        resolution_id: str,
    ) -> ReleaseLaunchResolution | None:
        """Read one immutable launch fact after validating its typed identity."""
        if not self.db_path.is_file():
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT resolution_json FROM release_launch_resolutions "
                "WHERE resolution_id = ?",
                (resolution_id,),
            ).fetchone()
        if row is None:
            return None
        from naumi_agent.release.launcher import ReleaseLaunchResolution

        return ReleaseLaunchResolution.model_validate_json(row["resolution_json"])

    def get_slot(self, slot_id: str) -> ReleaseInstalledSlot | None:
        if not self.db_path.is_file():
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT slot_json FROM release_slots WHERE slot_id = ?", (slot_id,)
            ).fetchone()
        return None if row is None else ReleaseInstalledSlot.model_validate_json(row["slot_json"])

    def inspect_installed_slot(self, slot_id: str) -> ReleaseInstalledSlot:
        """Return one installed slot only after revalidating bytes and immutability."""
        slot = self._require_slot(slot_id)
        bundle = Path(slot.bundle_dir)
        _verify_bundle(bundle, expected_manifest_sha256=slot.manifest_sha256)
        _verify_immutable(bundle)
        return slot

    def get_boot_receipt(self, receipt_id: str) -> ReleaseSlotBootReceipt | None:
        if not self.db_path.is_file():
            return None
        with self._connect() as db:
            return self._boot_row(db, receipt_id)

    def resolve_booted_slot(
        self,
        slot_id: str,
        boot_receipt_id: str,
    ) -> ResolvedBootedReleaseSlot:
        with self._connect() as db:
            slot = self._slot_row(db, slot_id)
            boot = self._boot_row(db, boot_receipt_id)
        if slot is None or boot is None:
            raise ReleaseSlotError(
                "release_booted_slot_missing",
                "版本槽或指定 Boot Receipt 不存在。",
            )
        _require_host_target(slot.target)
        bundle = Path(slot.bundle_dir)
        _verify_bundle(bundle, expected_manifest_sha256=slot.manifest_sha256)
        _verify_immutable(bundle)
        backend = bundle / slot.backend_path
        if not (
            boot.slot_id == slot.slot_id
            and boot.slot_sha256 == slot.slot_sha256
            and boot.manifest_sha256 == slot.manifest_sha256
            and boot.binary_sha256 == _sha256_file(backend)
        ):
            raise ReleaseSlotError(
                "release_booted_slot_receipt_stale",
                "Boot Receipt 与当前版本槽 runtime bytes 不匹配。",
            )
        return ResolvedBootedReleaseSlot(
            slot=slot,
            boot_receipt=boot,
            backend=backend,
        )

    def _require_slot(self, slot_id: str) -> ReleaseInstalledSlot:
        slot = self.get_slot(slot_id)
        if slot is None:
            raise ReleaseSlotError("release_slot_missing", "版本槽不存在。")
        return slot

    @staticmethod
    def _active_row(db) -> ReleaseActivePointer | None:
        row = db.execute(
            "SELECT pointer_json FROM release_active_pointer WHERE singleton = 1"
        ).fetchone()
        return (
            None if row is None else ReleaseActivePointer.model_validate_json(row["pointer_json"])
        )

    @classmethod
    def _validated_active(cls, db) -> ReleaseActivePointer | None:
        current = cls._active_row(db)
        rows = db.execute(
            "SELECT pointer_json FROM release_active_events ORDER BY generation"
        ).fetchall()
        if not rows:
            if current is not None:
                raise ReleaseSlotError(
                    "release_active_chain_broken",
                    "Active pointer 存在但 activation history 为空。",
                )
            return None
        previous = None
        for generation, row in enumerate(rows, 1):
            item = ReleaseActivePointer.model_validate_json(row["pointer_json"])
            if not (
                item.generation == generation
                and item.previous_pointer_sha256
                == (None if previous is None else previous.pointer_sha256)
            ):
                raise ReleaseSlotError(
                    "release_active_chain_broken",
                    "Activation history generation/hash chain 不连续。",
                )
            previous = item
        if current != previous:
            raise ReleaseSlotError(
                "release_active_chain_broken",
                "Active pointer 与 activation history tail 不一致。",
            )
        return current

    @staticmethod
    def _latest_boot(db, slot_id: str) -> ReleaseSlotBootReceipt | None:
        row = db.execute(
            "SELECT receipt_json FROM release_boot_receipts WHERE slot_id = ? "
            "ORDER BY checked_at DESC, receipt_id DESC LIMIT 1",
            (slot_id,),
        ).fetchone()
        return (
            None if row is None else ReleaseSlotBootReceipt.model_validate_json(row["receipt_json"])
        )

    @staticmethod
    def _boot_row(db, receipt_id: str) -> ReleaseSlotBootReceipt | None:
        row = db.execute(
            "SELECT receipt_json FROM release_boot_receipts WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        return (
            None if row is None else ReleaseSlotBootReceipt.model_validate_json(row["receipt_json"])
        )

    @staticmethod
    def _slot_row(db, slot_id: str) -> ReleaseInstalledSlot | None:
        row = db.execute(
            "SELECT slot_json FROM release_slots WHERE slot_id = ?", (slot_id,)
        ).fetchone()
        return None if row is None else ReleaseInstalledSlot.model_validate_json(row["slot_json"])

    def _connect(self):
        self.release_root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        _ensure_schema(db)
        return db


def host_release_target() -> str:
    machine = platform.machine().casefold()
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"x86_64", "amd64"}:
        arch = "x64"
    else:
        raise ReleaseSlotError(
            "release_host_unsupported", f"当前 CPU 架构不支持版本槽：{machine}。"
        )
    if sys.platform == "darwin":
        return f"macos-{arch}"
    if sys.platform.startswith("linux"):
        return f"linux-{arch}"
    if sys.platform == "win32":
        return f"windows-{arch}"
    raise ReleaseSlotError("release_host_unsupported", "当前操作系统不支持版本槽激活。")


def _require_host_target(target: str) -> None:
    if target != host_release_target():
        raise ReleaseSlotError(
            "release_slot_target_mismatch",
            f"版本槽 target={target} 与当前主机 {host_release_target()} 不一致。",
        )


def _verify_bundle(bundle: Path, *, expected_manifest_sha256: str | None = None):
    manifest_path = bundle / "manifest.json"
    try:
        encoded = manifest_path.read_bytes()
    except OSError as exc:
        raise ReleaseSlotError("release_manifest_missing", "发行 manifest 不存在。") from exc
    if not encoded or len(encoded) > _MAX_MANIFEST_BYTES:
        raise ReleaseSlotError("release_manifest_oversized", "发行 manifest 为空或超过 8 MiB。")
    manifest_sha = hashlib.sha256(encoded).hexdigest()
    if expected_manifest_sha256 is not None and manifest_sha != expected_manifest_sha256:
        raise ReleaseSlotError("release_manifest_digest_mismatch", "发行 manifest 摘要不一致。")
    try:
        manifest = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseSlotError(
            "release_manifest_invalid", "发行 manifest 不是有效 UTF-8 JSON。"
        ) from exc
    if not (
        isinstance(manifest, dict)
        and set(manifest)
        == {
            "schema_version",
            "product",
            "version",
            "target",
            "source_commit",
            "source_tree_sha256",
            "files",
        }
        and manifest["schema_version"] == 1
        and manifest["product"] == "NaumiAgent"
        and isinstance(manifest["version"], str)
        and isinstance(manifest["target"], str)
        and isinstance(manifest["source_commit"], str)
        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", manifest["source_commit"])
        and isinstance(manifest["source_tree_sha256"], str)
        and re.fullmatch(_SHA256_RE, manifest["source_tree_sha256"])
        and isinstance(manifest["files"], list)
        and 1 <= len(manifest["files"]) <= _MAX_FILES
    ):
        raise ReleaseSlotError("release_manifest_invalid", "发行 manifest schema 不受支持。")
    records = manifest["files"]
    records_by_path: dict[str, dict] = {}
    expected_paths: set[str] = set()
    total_bytes = 0
    for record in records:
        if not isinstance(record, dict) or record.get("kind") not in {"file", "symlink"}:
            raise ReleaseSlotError("release_manifest_invalid", "发行 manifest file record 无效。")
        relative = record.get("path")
        if (
            not isinstance(relative, str)
            or not _safe_relative(relative)
            or relative in expected_paths
        ):
            raise ReleaseSlotError(
                "release_manifest_path_invalid", "发行 manifest 路径不安全或重复。"
            )
        expected_paths.add(relative)
        records_by_path[relative] = record
        if _forbidden_release_path(relative):
            raise ReleaseSlotError(
                "release_bundle_source_exposure", f"发行 bundle 检测到源码泄漏：{relative}"
            )
        path = bundle / relative
        if record["kind"] == "file":
            if (
                set(record) != {"path", "kind", "size", "sha256"}
                or not path.is_file()
                or path.is_symlink()
            ):
                raise ReleaseSlotError(
                    "release_bundle_file_mismatch", f"发行文件缺失或类型不符：{relative}"
                )
            size = path.stat().st_size
            if record["size"] != size or record["sha256"] != _sha256_file(path):
                raise ReleaseSlotError(
                    "release_bundle_file_mismatch", f"发行文件摘要不一致：{relative}"
                )
            total_bytes += size
        else:
            if set(record) != {"path", "kind", "target", "sha256"} or not path.is_symlink():
                raise ReleaseSlotError(
                    "release_bundle_symlink_mismatch", f"发行 symlink 缺失：{relative}"
                )
            target = os.readlink(path)
            if (
                record["target"] != target
                or record["sha256"] != hashlib.sha256(target.encode()).hexdigest()
            ):
                raise ReleaseSlotError(
                    "release_bundle_symlink_mismatch", f"发行 symlink 摘要不一致：{relative}"
                )
            try:
                path.resolve(strict=True).relative_to(bundle.resolve())
            except (OSError, ValueError) as exc:
                raise ReleaseSlotError(
                    "release_bundle_symlink_escape", f"发行 symlink 越界：{relative}"
                ) from exc
    actual = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if (path.is_symlink() or not path.is_dir()) and path != manifest_path
    }
    if actual != expected_paths:
        raise ReleaseSlotError(
            "release_bundle_file_set_mismatch", "发行 bundle 文件集合与 manifest 不一致。"
        )
    windows = manifest["target"].casefold().startswith("windows-")
    for required in (
        "launcher/naumi.exe" if windows else "launcher/naumi",
        "naumi-runtime.exe" if windows else "naumi-runtime",
        "naumi-ui.exe" if windows else "naumi-ui",
        "config.yaml.example",
    ):
        record = records_by_path.get(required)
        if record is None or record["kind"] != "file" or record.get("size", 0) <= 0:
            raise ReleaseSlotError(
                "release_bundle_entry_missing", f"发行 bundle 缺少入口：{required}"
            )
    if total_bytes <= 0 or total_bytes > _MAX_TOTAL_BYTES:
        raise ReleaseSlotError("release_bundle_size_invalid", "发行 bundle 总大小无效。")
    return manifest, manifest_sha, len(records) + 1, total_bytes + len(encoded)


def _safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(
        value
        and not path.is_absolute()
        and path.parts
        and all(part not in {"", ".", ".."} for part in path.parts)
        and "\\" not in value
        and not any(char in value for char in ("\x00", "\r", "\n"))
    )


def _forbidden_release_path(value: str) -> bool:
    path = PurePosixPath(value)
    lowered_parts = {part.casefold() for part in path.parts}
    lowered_name = path.name.casefold()
    parts = tuple(part.casefold() for part in path.parts)
    inside_third_party_runtime = bool(
        (len(parts) >= 2 and parts[0] == "_internal" and parts[1] != "naumi_agent")
        or (
            len(parts) >= 3 and parts[:2] == ("launcher", "_internal") and parts[2] != "naumi_agent"
        )
    )
    return bool(
        not inside_third_party_runtime
        and (
            lowered_parts & _FORBIDDEN_COMPONENTS
            or lowered_name in _FORBIDDEN_NAMES
            or lowered_name.endswith(_FORBIDDEN_SUFFIXES)
        )
    )


def _make_immutable(root: Path) -> None:
    if os.name == "nt":
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                path.chmod(stat.S_IREAD)
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(0o555)
        elif mode & stat.S_IXUSR:
            path.chmod(0o555)
        else:
            path.chmod(0o444)
    root.chmod(0o555)


def _verify_immutable(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            continue
        if os.name == "nt" and path.is_dir():
            continue
        mode = path.stat().st_mode
        if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise ReleaseSlotError(
                "release_slot_mutable", f"版本槽仍可写：{path.relative_to(root)}"
            )


def _overlaps(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for current, _dirs, files in os.walk(root, followlinks=False):
        directory = Path(current)
        directories.append(directory)
        for name in files:
            path = directory / name
            if path.is_symlink():
                continue
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for directory in reversed(directories):
        _fsync_directory(directory)


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Release Slot 时间必须包含 offset。")
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


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        "CREATE TABLE IF NOT EXISTS release_slots ("
        "slot_id TEXT PRIMARY KEY, slot_sha256 TEXT NOT NULL UNIQUE, "
        "manifest_sha256 TEXT NOT NULL, slot_json TEXT NOT NULL, installed_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS release_boot_receipts ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "slot_id TEXT NOT NULL, receipt_json TEXT NOT NULL, checked_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS release_active_events ("
        "pointer_id TEXT PRIMARY KEY, pointer_sha256 TEXT NOT NULL UNIQUE, "
        "generation INTEGER NOT NULL UNIQUE, pointer_json TEXT NOT NULL, "
        "activated_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS release_active_pointer ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), pointer_json TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS release_launch_resolutions ("
        "resolution_id TEXT PRIMARY KEY, resolution_sha256 TEXT NOT NULL UNIQUE, "
        "pointer_sha256 TEXT NOT NULL, resolution_json TEXT NOT NULL, "
        "resolved_at TEXT NOT NULL);"
    )


__all__ = [
    "RELEASE_ACTIVE_POINTER_POLICY",
    "RELEASE_BOOT_RECEIPT_POLICY",
    "RELEASE_SLOT_POLICY",
    "ReleaseActivationAuthority",
    "ReleaseActivePointer",
    "ReleaseInstalledSlot",
    "ReleaseSlotBootReceipt",
    "ReleaseSlotError",
    "ReleaseSlotStore",
    "ResolvedBootedReleaseSlot",
    "ResolvedReleaseSlot",
    "host_release_target",
]
