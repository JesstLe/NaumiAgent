"""Safe archive extraction and immutable-slot admission for release downloads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import tarfile
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.release.artifact_fetch import (
    ReleaseArtifactDownloadReceipt,
    ReleaseArtifactDownloadStore,
    ReleaseArtifactDownloadView,
    ReleaseArtifactFetchError,
    ReleaseArtifactFetchService,
)
from naumi_agent.release.build_attestations import (
    ReleaseBuildAttestationError,
    ReleaseBuildTrustPolicyDocument,
    ReleaseTrustedBuilderKey,
    verify_release_build_attestation,
)
from naumi_agent.release.slots import (
    ReleaseInstalledSlot,
    ReleaseSlotError,
    ReleaseSlotStore,
)

RELEASE_ARCHIVE_ADMISSION_POLICY = "naumi-release-archive-admission-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_DOWNLOAD_SOURCE_RE = re.compile(r"^reldownloadsource_[0-9a-f]{24}$")
_MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_MAX_MEMBERS = 100_000
_MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
_MAX_EXPANSION_RATIO = 200
_EXPANSION_SLACK_BYTES = 64 * 1024 * 1024
_MAX_PATH_BYTES = 4096
_MAX_COMPONENT_BYTES = 255
_COPY_CHUNK_BYTES = 256 * 1024
_WINDOWS_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


class ReleaseArchiveAdmissionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ReleaseArchiveAdmissionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "naumi-release-archive-admission-v1"
    ] = RELEASE_ARCHIVE_ADMISSION_POLICY
    admission_id: str = Field(pattern=r"^relarchiveadmission_[0-9a-f]{24}$")
    admission_sha256: str = Field(pattern=_SHA256_RE)
    source_id: str = Field(pattern=r"^relarchiveadmissionsource_[0-9a-f]{24}$")
    source_sha256: str = Field(pattern=_SHA256_RE)
    download_receipt: ReleaseArtifactDownloadReceipt
    build_trust_policy_id: str = Field(pattern=r"^relbuildtrust_[0-9a-f]{24}$")
    build_trust_policy_sha256: str = Field(pattern=_SHA256_RE)
    trusted_builder_key: ReleaseTrustedBuilderKey
    archive_format: Literal["tar.gz", "zip"]
    bundle_member_count: int = Field(ge=1, le=_MAX_MEMBERS)
    extracted_payload_bytes: int = Field(gt=0, le=_MAX_UNCOMPRESSED_BYTES)
    installed_slot: ReleaseInstalledSlot
    admitted_at: str = Field(min_length=1, max_length=100)
    archive_structure_verified: Literal[True] = True
    expansion_bounds_verified: Literal[True] = True
    build_attestation_verified: Literal[True] = True
    manifest_verified: Literal[True] = True
    extraction_executed: Literal[True] = True
    installation_executed: Literal[True] = True
    inactive_slot_verified: Literal[True] = True
    archive_admission_authority: Literal[True] = True
    percentage_deployment_intent_input_authority: Literal[True] = True
    boot_executed: Literal[False] = False
    active_pointer_switched: Literal[False] = False
    process_started: Literal[False] = False
    deployment_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        resolution = self.download_receipt.resolution
        entry = resolution.entry
        payload = entry.build_attestation.payload
        slot = self.installed_slot
        expected_format = "tar.gz" if entry.archive_name.endswith(".tar.gz") else "zip"
        if not (
            self.build_trust_policy_id == resolution.build_trust_policy_id
            and self.build_trust_policy_sha256 == resolution.build_trust_policy_sha256
            and self.trusted_builder_key.identity == payload.builder
            and self.archive_format == expected_format
            and slot.manifest_sha256 == payload.manifest_sha256
            and slot.version == payload.version
            and slot.target == payload.target
            and slot.source_commit == payload.source_commit
            and slot.source_tree_sha256 == payload.source_tree_sha256
            and self.admitted_at == slot.installed_at
        ):
            raise ValueError("Archive Admission dependency projection 不一致。")
        source = _source_identity(
            self.download_receipt,
            build_policy_sha256=self.build_trust_policy_sha256,
            slot=self.installed_slot,
            member_count=self.bundle_member_count,
            payload_bytes=self.extracted_payload_bytes,
        )
        if (self.source_id, self.source_sha256) != source:
            raise ValueError("Archive Admission source identity 不一致。")
        _aware(self.admitted_at)
        core = self.model_dump(mode="json", exclude={"admission_id", "admission_sha256"})
        digest = _digest(core)
        if not (
            self.admission_sha256 == digest
            and self.admission_id == f"relarchiveadmission_{digest[:24]}"
        ):
            raise ValueError("Archive Admission content identity 不一致。")
        return self


class ReleaseArchiveAdmissionView(_StrictModel):
    receipt: ReleaseArchiveAdmissionReceipt
    receipt_source_current: bool
    download_source_current: bool
    build_trust_current: bool
    installed_slot_current: bool
    inactive_slot_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    archive_admission_authority: bool
    percentage_deployment_intent_input_authority: bool
    boot_executed: Literal[False] = False
    active_pointer_switched: Literal[False] = False
    process_started: Literal[False] = False
    deployment_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        admitted = bool(
            self.receipt_source_current
            and self.download_source_current
            and self.build_trust_current
            and self.installed_slot_current
        )
        intent = admitted and self.inactive_slot_current
        if not (
            self.archive_admission_authority is admitted
            and self.percentage_deployment_intent_input_authority is intent
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Archive Admission View authority projection 不一致。")
        return self


@dataclass(frozen=True, slots=True)
class _Member:
    path: PurePosixPath
    kind: Literal["directory", "file", "symlink"]
    size: int
    mode: int
    source: tarfile.TarInfo | zipfile.ZipInfo
    link_target: str = ""


@dataclass(frozen=True, slots=True)
class _ExtractionResult:
    bundle_dir: Path
    archive_format: Literal["tar.gz", "zip"]
    member_count: int
    payload_bytes: int


class ReleaseArchiveAdmissionStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        download_store: ReleaseArtifactDownloadStore,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not isinstance(download_store, ReleaseArtifactDownloadStore):
            raise TypeError("Archive Admission Store 需要 Download Store。")
        if self.db_path != download_store.db_path:
            raise ValueError("Archive Admission 必须与 Download Receipt 共用 exact DB。")
        self.download_store = download_store

    async def record(
        self,
        receipt: ReleaseArchiveAdmissionReceipt,
    ) -> ReleaseArchiveAdmissionReceipt:
        item = _validated_receipt(receipt)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_RECEIPT_BYTES:
            raise ReleaseArchiveAdmissionError(
                "release_archive_admission_oversized",
                "Archive Admission Receipt 超过 2 MiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_download_dependency(db, item.download_receipt)
                await db.execute(
                    "INSERT OR IGNORE INTO release_archive_admissions "
                    "(source_id, source_sha256, download_source_id, receipt_json, admitted_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        item.source_id,
                        item.source_sha256,
                        item.download_receipt.source_id,
                        encoded,
                        item.admitted_at,
                    ),
                )
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM release_archive_admissions "
                        "WHERE download_source_id = ?",
                        (item.download_receipt.source_id,),
                    )
                ).fetchone()
                stored = _restore_receipt(str(row["receipt_json"])) if row else None
                if stored != item:
                    await db.rollback()
                    raise ReleaseArchiveAdmissionError(
                        "release_archive_admission_conflict",
                        "同一 Download Receipt 已绑定不同 Archive Admission。",
                    )
                await db.commit()
            return item
        except ReleaseArchiveAdmissionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleaseArchiveAdmissionError(
                "release_archive_admission_store_error",
                "无法持久化 Archive Admission Receipt。",
            ) from exc

    async def get_by_download_source(
        self,
        download_source_id: str,
    ) -> ReleaseArchiveAdmissionReceipt | None:
        _require_download_source_id(download_source_id)
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT receipt_json FROM release_archive_admissions "
                        "WHERE download_source_id = ?",
                        (download_source_id,),
                    )
                ).fetchone()
            return None if row is None else _restore_receipt(str(row["receipt_json"]))
        except ReleaseArchiveAdmissionError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleaseArchiveAdmissionError(
                "release_archive_admission_source_invalid",
                "Archive Admission durable source 无效。",
            ) from exc


class ReleaseArchiveAdmissionService:
    def __init__(
        self,
        *,
        staging_root: str | Path,
        fetch_service: ReleaseArtifactFetchService,
        slot_store: ReleaseSlotStore,
        store: ReleaseArchiveAdmissionStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            isinstance(fetch_service, ReleaseArtifactFetchService)
            and isinstance(slot_store, ReleaseSlotStore)
            and isinstance(store, ReleaseArchiveAdmissionStore)
            and store.download_store is fetch_service.store
        ):
            raise ValueError("Archive Admission Service dependency 不一致。")
        self.staging_root = Path(staging_root).expanduser().resolve()
        self.fetch_service = fetch_service
        self.slot_store = slot_store
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def admit(
        self,
        *,
        download_source_id: str,
    ) -> ReleaseArchiveAdmissionView:
        _require_download_source_id(download_source_id)
        lock = self._locks.setdefault(download_source_id, asyncio.Lock())
        async with lock:
            return await self._admit_locked(download_source_id)

    async def inspect(
        self,
        *,
        download_source_id: str,
    ) -> ReleaseArchiveAdmissionView:
        _require_download_source_id(download_source_id)
        receipt = await self.store.get_by_download_source(download_source_id)
        if receipt is None:
            raise ReleaseArchiveAdmissionError(
                "release_archive_admission_missing",
                "指定 Download Receipt 尚未形成 Archive Admission。",
            )
        return await self._view(receipt)

    async def _admit_locked(
        self,
        download_source_id: str,
    ) -> ReleaseArchiveAdmissionView:
        download = await self._current_download(download_source_id)
        existing = await self.store.get_by_download_source(download_source_id)
        if existing is not None:
            return await self._view(existing)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        transaction = Path(
            tempfile.mkdtemp(prefix=".archive-admission-", dir=self.staging_root)
        )
        try:
            extracted, build_policy, trusted_key, slot = (
                await self._extract_and_install(download.receipt, transaction)
            )
            await self._revalidate_before_record(
                download_source_id=download_source_id,
                download=download.receipt,
                build_policy=build_policy,
                trusted_key=trusted_key,
                slot=slot,
            )
            receipt = _build_receipt(
                download_receipt=download.receipt,
                build_policy=build_policy,
                trusted_key=trusted_key,
                extracted=extracted,
                slot=slot,
            )
            stored = await self.store.record(receipt)
            return await self._view(stored)
        finally:
            shutil.rmtree(transaction)

    async def _extract_and_install(self, download, transaction):
        extracted = await asyncio.to_thread(
            _extract_archive,
            Path(download.archive_path),
            transaction,
            download,
        )
        build_policy, trusted_key = await asyncio.to_thread(
            self._verify_build,
            download,
            extracted.bundle_dir / "manifest.json",
        )
        try:
            slot = await asyncio.to_thread(
                self.slot_store.install,
                extracted.bundle_dir,
                installed_at=_aware(self.clock()).isoformat(),
                expected_manifest_sha256=download.resolution.entry.manifest_sha256,
            )
        except (ReleaseSlotError, OSError, TypeError, ValueError) as exc:
            raise ReleaseArchiveAdmissionError(
                "release_archive_slot_install_failed",
                "安全解包后的 bundle 无法安装到 immutable slot。",
            ) from exc
        _require_slot_projection(slot, download)
        return extracted, build_policy, trusted_key, slot

    async def _revalidate_before_record(
        self,
        *,
        download_source_id,
        download,
        build_policy,
        trusted_key,
        slot,
    ) -> None:
        refreshed = await self._current_download(download_source_id)
        if refreshed.receipt != download:
            raise ReleaseArchiveAdmissionError(
                "release_archive_download_changed",
                "解包/安装期间 Download Receipt authority 已变化。",
            )
        refreshed_policy, refreshed_key = await asyncio.to_thread(
            self._verify_build,
            download,
            Path(slot.bundle_dir) / "manifest.json",
        )
        if refreshed_policy != build_policy or refreshed_key != trusted_key:
            raise ReleaseArchiveAdmissionError(
                "release_archive_build_trust_changed",
                "解包/安装期间 Build Trust Policy 已变化。",
            )
        active = await asyncio.to_thread(self.slot_store.active)
        if active is not None and active.current_slot_id == slot.slot_id:
            raise ReleaseArchiveAdmissionError(
                "release_archive_slot_already_active",
                "Archive Admission 只允许形成 inactive slot。",
            )

    async def _current_download(
        self,
        source_id: str,
    ) -> ReleaseArtifactDownloadView:
        try:
            view = await self.fetch_service.inspect(source_id=source_id)
        except ReleaseArtifactFetchError as exc:
            raise ReleaseArchiveAdmissionError(
                "release_archive_download_stale",
                "Archive Admission 需要 current verified Download Receipt。",
            ) from exc
        if not view.verified_archive_authority:
            raise ReleaseArchiveAdmissionError(
                "release_archive_download_stale",
                "Download Receipt 已失去 verified archive authority。",
            )
        return view

    def _verify_build(
        self,
        download: ReleaseArtifactDownloadReceipt,
        manifest_path: Path,
    ) -> tuple[ReleaseBuildTrustPolicyDocument, ReleaseTrustedBuilderKey]:
        try:
            _channel_policy, build_policy = (
                self.fetch_service.catalog_store.current_trust_policies()
            )
            trusted_key = verify_release_build_attestation(
                download.resolution.entry.build_attestation,
                trust_policy=build_policy,
                manifest_path=manifest_path,
                archive_path=Path(download.archive_path),
            )
        except (ReleaseBuildAttestationError, OSError, TypeError, ValueError) as exc:
            raise ReleaseArchiveAdmissionError(
                "release_archive_build_attestation_invalid",
                "解包内容未通过 current Build Attestation 验证。",
            ) from exc
        if not (
            build_policy.policy_id == download.resolution.build_trust_policy_id
            and build_policy.policy_sha256
            == download.resolution.build_trust_policy_sha256
        ):
            raise ReleaseArchiveAdmissionError(
                "release_archive_build_trust_changed",
                "Build Trust Policy 与 Download Resolution 不一致。",
            )
        return build_policy, trusted_key

    async def _view(
        self,
        receipt: ReleaseArchiveAdmissionReceipt,
    ) -> ReleaseArchiveAdmissionView:
        receipt_current = await self._receipt_current(receipt)
        download_current = await self._download_current(receipt)
        slot_current = await self._slot_current(receipt)
        build_current = await self._build_current(receipt)
        inactive_current = await self._inactive_current(receipt, slot_current)
        checks = (
            ("receipt_source_changed", receipt_current),
            ("download_source_changed", download_current),
            ("build_trust_changed", build_current),
            ("installed_slot_changed", slot_current),
            ("slot_no_longer_inactive", inactive_current),
        )
        reasons = tuple(sorted(reason for reason, passed in checks if not passed))
        admitted = all(passed for _reason, passed in checks[:-1])
        return ReleaseArchiveAdmissionView(
            receipt=receipt,
            receipt_source_current=receipt_current,
            download_source_current=download_current,
            build_trust_current=build_current,
            installed_slot_current=slot_current,
            inactive_slot_current=inactive_current,
            invalidation_reasons=reasons,
            archive_admission_authority=admitted,
            percentage_deployment_intent_input_authority=(
                admitted and inactive_current
            ),
        )

    async def _receipt_current(self, receipt) -> bool:
        try:
            stored = await self.store.get_by_download_source(
                receipt.download_receipt.source_id
            )
            return stored == receipt
        except ReleaseArchiveAdmissionError:
            return False

    async def _download_current(self, receipt) -> bool:
        try:
            download = await self._current_download(receipt.download_receipt.source_id)
            return download.receipt == receipt.download_receipt
        except ReleaseArchiveAdmissionError:
            return False

    async def _slot_current(self, receipt) -> bool:
        try:
            slot = await asyncio.to_thread(
                self.slot_store.inspect_installed_slot,
                receipt.installed_slot.slot_id,
            )
            return slot == receipt.installed_slot
        except (ReleaseSlotError, OSError, TypeError, ValueError):
            return False

    async def _build_current(self, receipt) -> bool:
        try:
            policy, key = await asyncio.to_thread(
                self._verify_build,
                receipt.download_receipt,
                Path(receipt.installed_slot.bundle_dir) / "manifest.json",
            )
            return bool(
                policy.policy_id == receipt.build_trust_policy_id
                and policy.policy_sha256 == receipt.build_trust_policy_sha256
                and key == receipt.trusted_builder_key
            )
        except ReleaseArchiveAdmissionError:
            return False

    async def _inactive_current(self, receipt, slot_current: bool) -> bool:
        try:
            active = await asyncio.to_thread(self.slot_store.active)
            return bool(
                slot_current
                and (active is None or active.current_slot_id != receipt.installed_slot.slot_id)
            )
        except (ReleaseSlotError, OSError, TypeError, ValueError):
            return False


def _extract_archive(
    archive_path: Path,
    transaction: Path,
    download: ReleaseArtifactDownloadReceipt,
) -> _ExtractionResult:
    expected_name = download.resolution.entry.archive_name
    if archive_path.name != expected_name:
        raise ReleaseArchiveAdmissionError(
            "release_archive_name_mismatch",
            "Download archive filename 与 signed Entry 不一致。",
        )
    archive_format: Literal["tar.gz", "zip"] = (
        "tar.gz" if expected_name.endswith(".tar.gz") else "zip"
    )
    bundle_name = _bundle_name(expected_name, archive_format)
    maximum_bytes = _expansion_limit(download.archive_size_bytes)
    try:
        if archive_format == "tar.gz":
            with tarfile.open(archive_path, mode="r:gz") as archive:
                members = _tar_members(archive, bundle_name, maximum_bytes)
                payload_bytes = _extract_tar(archive, members, transaction)
        else:
            with zipfile.ZipFile(archive_path, mode="r") as archive:
                members = _zip_members(archive, bundle_name, maximum_bytes)
                payload_bytes = _extract_zip(archive, members, transaction)
    except ReleaseArchiveAdmissionError:
        raise
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ReleaseArchiveAdmissionError(
            "release_archive_invalid",
            "Release archive 无法安全解析或完整解压。",
        ) from exc
    bundle = (transaction / bundle_name).resolve()
    try:
        bundle.relative_to(transaction.resolve())
    except ValueError as exc:
        raise ReleaseArchiveAdmissionError(
            "release_archive_root_invalid",
            "Release archive 顶层 bundle 越界。",
        ) from exc
    if not bundle.is_dir() or bundle.is_symlink():
        raise ReleaseArchiveAdmissionError(
            "release_archive_root_invalid",
            "Release archive 缺少 exact 单一顶层 bundle。",
        )
    return _ExtractionResult(
        bundle_dir=bundle,
        archive_format=archive_format,
        member_count=len(members),
        payload_bytes=payload_bytes,
    )


def _tar_members(
    archive: tarfile.TarFile,
    bundle_name: str,
    maximum_bytes: int,
) -> tuple[_Member, ...]:
    members: list[_Member] = []
    total = 0
    for info in archive:
        if len(members) >= _MAX_MEMBERS:
            raise ReleaseArchiveAdmissionError(
                "release_archive_member_limit",
                "Release archive 文件数量超过 100000。",
            )
        if info.isdir():
            kind: Literal["directory", "file", "symlink"] = "directory"
            size = 0
            target = ""
        elif info.isreg() and not info.sparse:
            kind = "file"
            size = info.size
            target = ""
        elif info.issym():
            kind = "symlink"
            size = len(info.linkname.encode("utf-8"))
            target = info.linkname
        else:
            raise ReleaseArchiveAdmissionError(
                "release_archive_member_type",
                "Release archive 含 hardlink、设备、FIFO 或 sparse member。",
            )
        path = _member_path(info.name, directory=kind == "directory", bundle_name=bundle_name)
        total = _bounded_total(total, size, maximum_bytes)
        members.append(
            _Member(
                path=path,
                kind=kind,
                size=size,
                mode=info.mode,
                source=info,
                link_target=target,
            )
        )
    return _validated_members(members, bundle_name)


def _zip_members(
    archive: zipfile.ZipFile,
    bundle_name: str,
    maximum_bytes: int,
) -> tuple[_Member, ...]:
    if len(archive.infolist()) > _MAX_MEMBERS:
        raise ReleaseArchiveAdmissionError(
            "release_archive_member_limit",
            "Release archive 文件数量超过 100000。",
        )
    members: list[_Member] = []
    total = 0
    for info in archive.infolist():
        if info.flag_bits & 0x1:
            raise ReleaseArchiveAdmissionError(
                "release_archive_encrypted",
                "Release archive 不允许 encrypted ZIP member。",
            )
        mode = (info.external_attr >> 16) & 0xFFFF
        if info.is_dir() or stat.S_ISDIR(mode):
            kind: Literal["directory", "file", "symlink"] = "directory"
            size = 0
            target = ""
        elif stat.S_ISLNK(mode):
            kind = "symlink"
            if info.file_size > _MAX_PATH_BYTES:
                raise ReleaseArchiveAdmissionError(
                    "release_archive_symlink_invalid",
                    "ZIP symlink target 过长。",
                )
            target = archive.read(info).decode("utf-8")
            size = len(target.encode("utf-8"))
        elif mode == 0 or stat.S_ISREG(mode):
            kind = "file"
            size = info.file_size
            target = ""
        else:
            raise ReleaseArchiveAdmissionError(
                "release_archive_member_type",
                "Release archive 含设备、FIFO 或其他特殊 ZIP member。",
            )
        path = _member_path(
            info.filename,
            directory=kind == "directory",
            bundle_name=bundle_name,
        )
        total = _bounded_total(total, size, maximum_bytes)
        members.append(
            _Member(
                path=path,
                kind=kind,
                size=size,
                mode=mode,
                source=info,
                link_target=target,
            )
        )
    return _validated_members(members, bundle_name)


def _validated_members(
    members: list[_Member],
    bundle_name: str,
) -> tuple[_Member, ...]:
    if not members:
        raise ReleaseArchiveAdmissionError(
            "release_archive_empty",
            "Release archive 为空。",
        )
    exact: dict[str, _Member] = {}
    normalized: set[str] = set()
    for member in members:
        text = member.path.as_posix()
        folded = unicodedata.normalize("NFC", text).casefold()
        if text in exact or folded in normalized:
            raise ReleaseArchiveAdmissionError(
                "release_archive_path_collision",
                "Release archive 含重复或跨平台路径碰撞。",
            )
        exact[text] = member
        normalized.add(folded)
        if member.kind == "symlink":
            _safe_link_target(member.path, member.link_target, bundle_name)
    for member in members:
        parents = member.path.parents[:-1]
        for parent in parents:
            ancestor = exact.get(parent.as_posix())
            if ancestor is not None and ancestor.kind != "directory":
                raise ReleaseArchiveAdmissionError(
                    "release_archive_parent_type",
                    "Release archive member 位于 file/symlink 之下。",
                )
    roots = {member.path.parts[0] for member in members}
    if roots != {bundle_name}:
        raise ReleaseArchiveAdmissionError(
            "release_archive_root_invalid",
            "Release archive 必须只有 exact 单一顶层 bundle。",
        )
    return tuple(members)


def _extract_tar(
    archive: tarfile.TarFile,
    members: tuple[_Member, ...],
    destination: Path,
) -> int:
    payload = 0
    _create_directories(members, destination)
    for member in members:
        if member.kind != "file":
            continue
        source = archive.extractfile(member.source)
        if source is None:
            raise ReleaseArchiveAdmissionError(
                "release_archive_file_missing",
                "Tar regular member 无法读取。",
            )
        with source:
            _write_member(source, destination / member.path, member.size, member.mode)
        payload += member.size
    payload += _create_symlinks(members, destination)
    return payload


def _extract_zip(
    archive: zipfile.ZipFile,
    members: tuple[_Member, ...],
    destination: Path,
) -> int:
    payload = 0
    _create_directories(members, destination)
    for member in members:
        if member.kind != "file":
            continue
        with archive.open(member.source, mode="r") as source:
            _write_member(source, destination / member.path, member.size, member.mode)
        payload += member.size
    payload += _create_symlinks(members, destination)
    return payload


def _create_directories(members: tuple[_Member, ...], destination: Path) -> None:
    directories = {destination / member.path.parent for member in members}
    directories.update(
        destination / member.path
        for member in members
        if member.kind == "directory"
    )
    for directory in sorted(directories, key=lambda item: len(item.parts)):
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise ReleaseArchiveAdmissionError(
                "release_archive_directory_invalid",
                "Release archive directory extraction 冲突。",
            )


def _write_member(source: BinaryIO, target: Path, expected: int, mode: int) -> None:
    received = 0
    with target.open("xb") as output:
        while True:
            chunk = source.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            received += len(chunk)
            if received > expected:
                raise ReleaseArchiveAdmissionError(
                    "release_archive_member_size_mismatch",
                    "Release archive member 实际字节数超过声明。",
                )
            output.write(chunk)
    if received != expected:
        raise ReleaseArchiveAdmissionError(
            "release_archive_member_size_mismatch",
            "Release archive member 实际字节数与声明不一致。",
        )
    target.chmod(0o755 if mode & 0o111 else 0o644)


def _create_symlinks(members: tuple[_Member, ...], destination: Path) -> int:
    links = tuple(member for member in members if member.kind == "symlink")
    for member in links:
        os.symlink(member.link_target, destination / member.path)
    root = destination.resolve()
    for member in links:
        try:
            (destination / member.path).resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ReleaseArchiveAdmissionError(
                "release_archive_symlink_escape",
                "Release archive symlink 越界、循环或失效。",
            ) from exc
    return sum(member.size for member in links)


def _member_path(
    raw: str,
    *,
    directory: bool,
    bundle_name: str,
) -> PurePosixPath:
    candidate = raw[:-1] if directory and raw.endswith("/") else raw
    path = PurePosixPath(candidate)
    try:
        encoded = candidate.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReleaseArchiveAdmissionError(
            "release_archive_path_invalid",
            "Release archive member path 不是有效 UTF-8。",
        ) from exc
    if not (
        candidate
        and candidate == path.as_posix()
        and not path.is_absolute()
        and "\\" not in candidate
        and len(encoded) <= _MAX_PATH_BYTES
        and path.parts
        and path.parts[0] == bundle_name
        and all(
            part not in {"", ".", ".."}
            and len(part.encode("utf-8")) <= _MAX_COMPONENT_BYTES
            and not any(ord(char) < 32 or ord(char) == 127 for char in part)
            and not part.endswith((" ", "."))
            and ":" not in part
            and part.split(".", 1)[0].casefold() not in _WINDOWS_RESERVED
            for part in path.parts
        )
    ):
        raise ReleaseArchiveAdmissionError(
            "release_archive_path_invalid",
            "Release archive member path 不安全或不具备跨平台确定性。",
        )
    return path


def _safe_link_target(path: PurePosixPath, target: str, bundle_name: str) -> None:
    try:
        encoded = target.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReleaseArchiveAdmissionError(
            "release_archive_symlink_invalid",
            "Release archive symlink target 不是有效 UTF-8。",
        ) from exc
    combined = PurePosixPath(posixpath.normpath((path.parent / target).as_posix()))
    if not (
        target
        and len(encoded) <= _MAX_PATH_BYTES
        and "\\" not in target
        and ":" not in target
        and not PurePosixPath(target).is_absolute()
        and not any(ord(char) < 32 or ord(char) == 127 for char in target)
        and combined.parts
        and combined.parts[0] == bundle_name
        and all(part not in {"", ".", ".."} for part in combined.parts)
    ):
        raise ReleaseArchiveAdmissionError(
            "release_archive_symlink_invalid",
            "Release archive symlink target 越界或不安全。",
        )


def _bounded_total(current: int, size: int, maximum: int) -> int:
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ReleaseArchiveAdmissionError(
            "release_archive_size_invalid",
            "Release archive member size 无效。",
        )
    total = current + size
    if total > maximum:
        raise ReleaseArchiveAdmissionError(
            "release_archive_expansion_limit",
            "Release archive 声明解压体积超过安全上限。",
        )
    return total


def _require_download_source_id(value: str) -> None:
    if not isinstance(value, str) or _DOWNLOAD_SOURCE_RE.fullmatch(value) is None:
        raise ReleaseArchiveAdmissionError(
            "release_archive_download_source_invalid",
            "Download source ID 格式无效。",
        )


def _expansion_limit(archive_bytes: int) -> int:
    return min(
        _MAX_UNCOMPRESSED_BYTES,
        archive_bytes * _MAX_EXPANSION_RATIO + _EXPANSION_SLACK_BYTES,
    )


def _bundle_name(archive_name: str, archive_format: str) -> str:
    suffix = ".tar.gz" if archive_format == "tar.gz" else ".zip"
    return archive_name[: -len(suffix)]


def _require_slot_projection(
    slot: ReleaseInstalledSlot,
    download: ReleaseArtifactDownloadReceipt,
) -> None:
    payload = download.resolution.entry.build_attestation.payload
    if not (
        slot.manifest_sha256 == payload.manifest_sha256
        and slot.version == payload.version
        and slot.target == payload.target
        and slot.source_commit == payload.source_commit
        and slot.source_tree_sha256 == payload.source_tree_sha256
    ):
        raise ReleaseArchiveAdmissionError(
            "release_archive_slot_projection_mismatch",
            "Installed slot 与 signed Build Attestation 不一致。",
        )


def _build_receipt(
    *,
    download_receipt: ReleaseArtifactDownloadReceipt,
    build_policy: ReleaseBuildTrustPolicyDocument,
    trusted_key: ReleaseTrustedBuilderKey,
    extracted: _ExtractionResult,
    slot: ReleaseInstalledSlot,
) -> ReleaseArchiveAdmissionReceipt:
    source_id, source_sha = _source_identity(
        download_receipt,
        build_policy_sha256=build_policy.policy_sha256,
        slot=slot,
        member_count=extracted.member_count,
        payload_bytes=extracted.payload_bytes,
    )
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_ARCHIVE_ADMISSION_POLICY,
        "source_id": source_id,
        "source_sha256": source_sha,
        "download_receipt": download_receipt.model_dump(mode="json"),
        "build_trust_policy_id": build_policy.policy_id,
        "build_trust_policy_sha256": build_policy.policy_sha256,
        "trusted_builder_key": trusted_key.model_dump(mode="json"),
        "archive_format": extracted.archive_format,
        "bundle_member_count": extracted.member_count,
        "extracted_payload_bytes": extracted.payload_bytes,
        "installed_slot": slot.model_dump(mode="json"),
        "admitted_at": slot.installed_at,
        "archive_structure_verified": True,
        "expansion_bounds_verified": True,
        "build_attestation_verified": True,
        "manifest_verified": True,
        "extraction_executed": True,
        "installation_executed": True,
        "inactive_slot_verified": True,
        "archive_admission_authority": True,
        "percentage_deployment_intent_input_authority": True,
        "boot_executed": False,
        "active_pointer_switched": False,
        "process_started": False,
        "deployment_authority": False,
    }
    digest = _digest(core)
    return ReleaseArchiveAdmissionReceipt.model_validate(
        {
            **core,
            "download_receipt": download_receipt,
            "trusted_builder_key": trusted_key,
            "installed_slot": slot,
            "admission_id": f"relarchiveadmission_{digest[:24]}",
            "admission_sha256": digest,
        }
    )


def _source_identity(
    download: ReleaseArtifactDownloadReceipt,
    *,
    build_policy_sha256: str,
    slot: ReleaseInstalledSlot,
    member_count: int,
    payload_bytes: int,
) -> tuple[str, str]:
    digest = _digest(
        {
            "download_receipt_id": download.receipt_id,
            "download_receipt_sha256": download.receipt_sha256,
            "build_policy_sha256": build_policy_sha256,
            "slot_id": slot.slot_id,
            "slot_sha256": slot.slot_sha256,
            "member_count": member_count,
            "payload_bytes": payload_bytes,
        }
    )
    return f"relarchiveadmissionsource_{digest[:24]}", digest


def _validated_receipt(value) -> ReleaseArchiveAdmissionReceipt:
    try:
        return ReleaseArchiveAdmissionReceipt.model_validate_json(value.model_dump_json())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseArchiveAdmissionError(
            "release_archive_admission_receipt_invalid",
            "Archive Admission Receipt 无效。",
        ) from exc


def _restore_receipt(value: str) -> ReleaseArchiveAdmissionReceipt:
    if len(value.encode()) > _MAX_RECEIPT_BYTES:
        raise ValueError("Archive Admission durable source 超过 2 MiB。")
    return ReleaseArchiveAdmissionReceipt.model_validate_json(value)


async def _require_download_dependency(
    db,
    receipt: ReleaseArtifactDownloadReceipt,
) -> None:
    row = await (
        await db.execute(
            "SELECT receipt_json FROM release_artifact_downloads WHERE source_id = ?",
            (receipt.source_id,),
        )
    ).fetchone()
    if row is None:
        raise ReleaseArchiveAdmissionError(
            "release_archive_download_missing",
            "Archive Admission 缺少 durable Download Receipt dependency。",
        )
    try:
        stored = ReleaseArtifactDownloadReceipt.model_validate_json(row["receipt_json"])
    except (TypeError, ValueError) as exc:
        raise ReleaseArchiveAdmissionError(
            "release_archive_download_invalid",
            "Durable Download Receipt dependency 无效。",
        ) from exc
    if stored != receipt:
        raise ReleaseArchiveAdmissionError(
            "release_archive_download_changed",
            "Durable Download Receipt dependency 已变化。",
        )


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS release_archive_admissions ("
        "source_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL UNIQUE, "
        "download_source_id TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, "
        "admitted_at TEXT NOT NULL)"
    )
    await db.commit()


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Archive Admission timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "RELEASE_ARCHIVE_ADMISSION_POLICY",
    "ReleaseArchiveAdmissionError",
    "ReleaseArchiveAdmissionReceipt",
    "ReleaseArchiveAdmissionService",
    "ReleaseArchiveAdmissionStore",
    "ReleaseArchiveAdmissionView",
]
