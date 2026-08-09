"""Fenced, bounded streaming fetch for signed release-channel artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import stat
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, Self

import aiosqlite
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.release.channel_catalog import (
    RELEASE_CHANNEL_RESOLUTION_POLICY,
    ReleaseChannelCatalogError,
    ReleaseChannelCatalogStore,
    ReleaseChannelResolution,
)

RELEASE_ARTIFACT_DOWNLOAD_POLICY = "naumi-release-artifact-download-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_RECEIPT_BYTES = 512 * 1024
_CHUNK_BYTES = 256 * 1024


class ReleaseArtifactFetchError(RuntimeError):
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


class ReleaseArtifactFetchAttempt(_StrictModel):
    schema_version: Literal[1] = 1
    url_index: int = Field(ge=0, le=7)
    url_sha256: str = Field(pattern=_SHA256_RE)
    status: Literal[
        "transport_error",
        "http_error",
        "header_invalid",
        "size_mismatch",
        "digest_mismatch",
        "completed",
        "existing_verified",
    ]
    http_status: int | None = Field(default=None, ge=100, le=599)
    declared_bytes: int | None = Field(default=None, ge=0, le=8 * 1024**3)
    received_bytes: int = Field(ge=0, le=8 * 1024**3)
    received_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    chunk_count: int = Field(ge=0, le=100_000_000)
    duration_ms: int = Field(ge=0, le=86_400_000)


class ReleaseArtifactDownloadReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "naumi-release-artifact-download-v1"
    ] = RELEASE_ARTIFACT_DOWNLOAD_POLICY
    receipt_id: str = Field(pattern=r"^reldownload_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    source_id: str = Field(pattern=r"^reldownloadsource_[0-9a-f]{24}$")
    source_sha256: str = Field(pattern=_SHA256_RE)
    resolution: ReleaseChannelResolution
    archive_path: str = Field(min_length=1, max_length=4096)
    archive_sha256: str = Field(pattern=_SHA256_RE)
    archive_size_bytes: int = Field(gt=0, le=8 * 1024**3)
    attempts: tuple[ReleaseArtifactFetchAttempt, ...] = Field(min_length=1, max_length=8)
    selected_url_index: int = Field(ge=0, le=7)
    selected_url_sha256: str = Field(pattern=_SHA256_RE)
    claim_epoch: int = Field(ge=1, le=1_000_000_000)
    claim_owner_sha256: str = Field(pattern=_SHA256_RE)
    downloaded_at: str = Field(min_length=1, max_length=100)
    content_size_verified: Literal[True] = True
    http_content_length_verified: bool
    archive_digest_verified: Literal[True] = True
    regular_file_verified: Literal[True] = True
    file_fsync_completed: Literal[True] = True
    directory_fsync_completed: Literal[True] = True
    atomic_commit_completed: Literal[True] = True
    immutable: Literal[True] = True
    verified_archive_authority: Literal[True] = True
    installation_input_authority: Literal[True] = True
    extraction_executed: Literal[False] = False
    installation_executed: Literal[False] = False
    deployment_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        archive = Path(self.archive_path).expanduser().resolve()
        if self.archive_path != str(archive):
            raise ValueError("Download Receipt archive path 必须 canonical。")
        entry = self.resolution.entry
        successful = self.attempts[-1]
        if not (
            self.resolution.policy_version == RELEASE_CHANNEL_RESOLUTION_POLICY
            and self.archive_sha256 == entry.archive_sha256
            and self.archive_size_bytes == entry.archive_size_bytes
            and self.selected_url_index == successful.url_index
            and self.selected_url_sha256 == successful.url_sha256
            and successful.status in {"completed", "existing_verified"}
            and successful.received_bytes == self.archive_size_bytes
            and successful.received_sha256 == self.archive_sha256
            and self.http_content_length_verified
            is (successful.status == "completed")
            and all(
                item.status not in {"completed", "existing_verified"}
                for item in self.attempts[:-1]
            )
        ):
            raise ValueError("Download Receipt source/attempt projection 不一致。")
        expected_source = _source_identity(self.resolution)
        if (self.source_id, self.source_sha256) != expected_source:
            raise ValueError("Download Receipt source identity 不一致。")
        _aware(self.downloaded_at)
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if not (
            self.receipt_sha256 == digest
            and self.receipt_id == f"reldownload_{digest[:24]}"
        ):
            raise ValueError("Download Receipt content identity 不一致。")
        return self


class ReleaseArtifactDownloadView(_StrictModel):
    receipt: ReleaseArtifactDownloadReceipt
    receipt_source_current: bool
    resolution_source_current: bool
    catalog_authority_current: bool
    archive_file_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=8)
    verified_archive_authority: bool
    installation_input_authority: bool
    extraction_executed: Literal[False] = False
    installation_executed: Literal[False] = False
    deployment_authority: Literal[False] = False

    @model_validator(mode="after")
    def _project(self) -> Self:
        current = bool(
            self.receipt_source_current
            and self.resolution_source_current
            and self.catalog_authority_current
            and self.archive_file_current
        )
        if not (
            self.verified_archive_authority is current
            and self.installation_input_authority is current
            and self.invalidation_reasons
            == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Download View authority projection 不一致。")
        return self


class ReleaseArtifactStreamResponse(Protocol):
    status_code: int
    headers: httpx.Headers

    def aiter_raw(self, chunk_size: int) -> AsyncIterator[bytes]: ...


class ReleaseArtifactTransport(Protocol):
    def stream(
        self,
        url: str,
    ) -> AbstractAsyncContextManager[ReleaseArtifactStreamResponse]: ...


class HttpxReleaseArtifactTransport:
    def __init__(
        self,
        *,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout = timeout or httpx.Timeout(connect=10, read=60, write=30, pool=10)
        self.transport = transport

    @asynccontextmanager
    async def stream(
        self,
        url: str,
    ) -> AsyncIterator[ReleaseArtifactStreamResponse]:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=self.timeout,
            transport=self.transport,
            trust_env=True,
        ) as client:
            async with client.stream(
                "GET",
                url,
                headers={
                    "Accept": "application/octet-stream",
                    "Accept-Encoding": "identity",
                    "User-Agent": "NaumiAgent-Updater/1",
                },
            ) as response:
                yield response


@dataclass(frozen=True)
class _Claim:
    status: Literal["acquired", "busy", "completed"]
    source_id: str
    source_sha256: str
    owner_id: str
    epoch: int
    expires_at: str
    receipt: ReleaseArtifactDownloadReceipt | None = None


class ReleaseArtifactDownloadStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        catalog_store: ReleaseChannelCatalogStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not isinstance(catalog_store, ReleaseChannelCatalogStore):
            raise TypeError("Download Store 需要 Release Channel Catalog Store。")
        if self.db_path != catalog_store.db_path:
            raise ValueError("Download Receipt 必须与 Channel Catalog 共用 exact DB。")
        self.catalog_store = catalog_store
        self.clock = clock or (lambda: datetime.now(UTC))

    async def claim(
        self,
        resolution: ReleaseChannelResolution,
        *,
        owner_id: str,
        lease_seconds: int,
    ) -> _Claim:
        item = _validated_resolution(resolution)
        _require_owner(owner_id)
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("Download claim lease 必须为 5..3600 秒。")
        source_id, source_sha = _source_identity(item)
        now = _aware(self.clock())
        expires = now + timedelta(seconds=lease_seconds)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return await self._claim_transaction(
                source_id=source_id,
                source_sha=source_sha,
                owner_id=owner_id,
                now=now,
                expires=expires,
            )
        except ReleaseArtifactFetchError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleaseArtifactFetchError(
                "release_download_claim_error",
                "无法 claim Release Artifact download。",
            ) from exc

    async def _claim_transaction(
        self,
        *,
        source_id,
        source_sha,
        owner_id,
        now,
        expires,
    ) -> _Claim:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await _claim_row(db, source_id)
            claim = _claim_from_row(row) if row is not None else None
            available = _available_claim(
                claim,
                source_sha=source_sha,
                owner_id=owner_id,
                now=now,
            )
            if available is not None:
                await db.rollback()
                return available
            epoch = 1 if claim is None else claim.epoch + 1
            await _upsert_claim(
                db,
                source_id=source_id,
                source_sha=source_sha,
                owner_id=owner_id,
                epoch=epoch,
                expires=expires,
                now=now,
            )
            await db.commit()
        return _Claim(
            status="acquired",
            source_id=source_id,
            source_sha256=source_sha,
            owner_id=owner_id,
            epoch=epoch,
            expires_at=expires.isoformat(),
        )

    async def renew(self, claim: _Claim, *, lease_seconds: int) -> _Claim:
        now = _aware(self.clock())
        expires = now + timedelta(seconds=lease_seconds)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "UPDATE release_artifact_downloads SET claim_expires_at=?, updated_at=? "
                    "WHERE source_id=? AND source_sha256=? AND owner_id=? "
                    "AND claim_epoch=? AND claim_expires_at>? AND receipt_json IS NULL",
                    (
                        expires.isoformat(),
                        now.isoformat(),
                        claim.source_id,
                        claim.source_sha256,
                        claim.owner_id,
                        claim.epoch,
                        now.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    raise ReleaseArtifactFetchError(
                        "release_download_claim_fenced",
                        "Download claim 已被更新执行者 fencing。",
                    )
                await db.commit()
            return _Claim(
                status="acquired",
                source_id=claim.source_id,
                source_sha256=claim.source_sha256,
                owner_id=claim.owner_id,
                epoch=claim.epoch,
                expires_at=expires.isoformat(),
            )
        except ReleaseArtifactFetchError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleaseArtifactFetchError(
                "release_download_claim_error",
                "无法续期 Release Artifact download claim。",
            ) from exc

    async def complete(
        self,
        claim: _Claim,
        receipt: ReleaseArtifactDownloadReceipt,
    ) -> ReleaseArtifactDownloadReceipt:
        item = _validated_receipt(receipt)
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_RECEIPT_BYTES:
            raise ReleaseArtifactFetchError(
                "release_download_receipt_oversized",
                "Release Artifact Download Receipt 超过 512 KiB。",
            )
        if (item.source_id, item.source_sha256, item.claim_epoch) != (
            claim.source_id,
            claim.source_sha256,
            claim.epoch,
        ):
            raise ReleaseArtifactFetchError(
                "release_download_receipt_claim_mismatch",
                "Download Receipt 未绑定 exact claim。",
            )
        now = _aware(self.clock())
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await _claim_row(db, claim.source_id)
                current = _claim_from_row(row) if row is not None else None
                if current is not None and current.receipt is not None:
                    await db.rollback()
                    return current.receipt
                if not _claim_matches(current, claim) or _aware(claim.expires_at) <= now:
                    raise ReleaseArtifactFetchError(
                        "release_download_claim_fenced",
                        "Download completion claim 已过期或被 fencing。",
                    )
                await db.execute(
                    "UPDATE release_artifact_downloads SET receipt_json=?, updated_at=? "
                    "WHERE source_id=?",
                    (encoded, now.isoformat(), claim.source_id),
                )
                await db.commit()
            return item
        except ReleaseArtifactFetchError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleaseArtifactFetchError(
                "release_download_store_error",
                "无法持久化 Download Receipt。",
            ) from exc

    async def release(self, claim: _Claim) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "UPDATE release_artifact_downloads SET owner_id='', "
                    "claim_expires_at=?, updated_at=? WHERE source_id=? AND owner_id=? "
                    "AND claim_epoch=? AND receipt_json IS NULL",
                    (
                        datetime.min.replace(tzinfo=UTC).isoformat(),
                        _aware(self.clock()).isoformat(),
                        claim.source_id,
                        claim.owner_id,
                        claim.epoch,
                    ),
                )
                await db.commit()
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleaseArtifactFetchError(
                "release_download_claim_error",
                "无法释放 Release Artifact download claim。",
            ) from exc

    async def get(self, source_id: str) -> ReleaseArtifactDownloadReceipt | None:
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await _claim_row(db, source_id)
            claim = _claim_from_row(row) if row is not None else None
            return None if claim is None else claim.receipt
        except ReleaseArtifactFetchError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise ReleaseArtifactFetchError(
                "release_download_source_invalid",
                "Download Receipt durable source 无效。",
            ) from exc


class ReleaseArtifactFetchService:
    def __init__(
        self,
        *,
        download_root: str | Path,
        catalog_store: ReleaseChannelCatalogStore,
        store: ReleaseArtifactDownloadStore,
        transport: ReleaseArtifactTransport | None = None,
        owner_id: str | None = None,
        lease_seconds: int = 30,
        wait_timeout_seconds: float = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            isinstance(catalog_store, ReleaseChannelCatalogStore)
            and isinstance(store, ReleaseArtifactDownloadStore)
            and store.catalog_store is catalog_store
        ):
            raise ValueError("Artifact Fetch Service durable dependency 不一致。")
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("Download lease 必须为 5..3600 秒。")
        if not 1 <= wait_timeout_seconds <= 7200:
            raise ValueError("Download wait timeout 必须为 1..7200 秒。")
        self.download_root = Path(download_root).expanduser().resolve()
        self.catalog_store = catalog_store
        self.store = store
        self.transport = transport or HttpxReleaseArtifactTransport()
        self.owner_id = owner_id or f"release-fetch-{uuid.uuid4().hex}"
        _require_owner(self.owner_id)
        self.lease_seconds = lease_seconds
        self.wait_timeout_seconds = wait_timeout_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}

    async def fetch(
        self,
        resolution: ReleaseChannelResolution,
    ) -> ReleaseArtifactDownloadView:
        item = _validated_resolution(resolution)
        source_id, _source_sha = _source_identity(item)
        lock = self._locks.setdefault(source_id, asyncio.Lock())
        async with lock:
            return await self._fetch_locked(item)

    async def _fetch_locked(self, resolution):
        await self._require_current_resolution(resolution)
        claim = await self._claim_or_wait(resolution)
        if claim.receipt is not None:
            return await self._view(claim.receipt)
        renewer = _ClaimRenewer(
            store=self.store,
            claim=claim,
            lease_seconds=self.lease_seconds,
        )
        await renewer.start()
        stored = None
        try:
            receipt = await self._fetch_claimed(resolution, claim, renewer)
            await renewer.stop()
            await self._require_current_resolution(resolution)
            stored = await self.store.complete(renewer.claim, receipt)
            return await self._view(stored)
        finally:
            if stored is None:
                await renewer.stop(suppress=True)
                await self.store.release(renewer.claim)

    async def inspect(self, *, source_id: str) -> ReleaseArtifactDownloadView:
        receipt = await self.store.get(source_id)
        if receipt is None:
            raise ReleaseArtifactFetchError(
                "release_download_receipt_missing",
                "指定的 Download Receipt 不存在。",
            )
        return await self._view(receipt)

    async def _claim_or_wait(self, resolution) -> _Claim:
        deadline = time.monotonic() + self.wait_timeout_seconds
        while True:
            claim = await self.store.claim(
                resolution,
                owner_id=self.owner_id,
                lease_seconds=self.lease_seconds,
            )
            if claim.status in {"acquired", "completed"}:
                return claim
            if time.monotonic() >= deadline:
                raise ReleaseArtifactFetchError(
                    "release_download_wait_timeout",
                    "等待另一下载执行者完成时超时。",
                )
            await asyncio.sleep(0.05)

    async def _fetch_claimed(self, resolution, claim, renewer):
        destination = _destination(self.download_root, resolution)
        destination.parent.mkdir(parents=True, exist_ok=True)
        existing = _existing_attempt(destination, resolution)
        if existing is not None:
            return _build_receipt(
                resolution=resolution,
                destination=destination,
                attempts=(existing,),
                claim=claim,
                downloaded_at=_aware(self.clock()).isoformat(),
            )
        attempts: list[ReleaseArtifactFetchAttempt] = []
        for index, url in enumerate(resolution.archive_urls):
            await renewer.assert_current()
            attempt = await self._fetch_url(
                resolution=resolution,
                destination=destination,
                url=url,
                url_index=index,
                claim=claim,
                renewer=renewer,
            )
            attempts.append(attempt)
            if attempt.status == "completed":
                return _build_receipt(
                    resolution=resolution,
                    destination=destination,
                    attempts=tuple(attempts),
                    claim=claim,
                    downloaded_at=_aware(self.clock()).isoformat(),
                )
        raise ReleaseArtifactFetchError(
            "release_download_all_origins_failed",
            "所有 pinned archive origin 均未返回可信 artifact。",
        )

    async def _fetch_url(
        self,
        *,
        resolution,
        destination,
        url,
        url_index,
        claim,
        renewer,
    ) -> ReleaseArtifactFetchAttempt:
        started = time.monotonic()
        staging = destination.with_name(
            f".{destination.name}.part.{claim.source_id}.{claim.epoch}"
        )
        _unlink_staging(staging)
        try:
            async with self.transport.stream(url) as response:
                return await self._fetch_response(
                    resolution=resolution,
                    destination=destination,
                    staging=staging,
                    url=url,
                    url_index=url_index,
                    response=response,
                    renewer=renewer,
                    started=started,
                )
        except (httpx.HTTPError, OSError, TimeoutError):
            return _attempt(
                url_index=url_index,
                url=url,
                status="transport_error",
                started=started,
            )
        finally:
            # The successful path atomically moves staging away; every other
            # path, including cancellation and unexpected parser failures,
            # must leave no reusable partial artifact behind.
            _unlink_staging(staging)

    async def _fetch_response(
        self,
        *,
        resolution,
        destination,
        staging,
        url,
        url_index,
        response,
        renewer,
        started,
    ):
        header = _response_header_state(response, resolution.entry.archive_size_bytes)
        if header[0] is not None:
            return _attempt(
                url_index=url_index,
                url=url,
                status=header[0],
                http_status=response.status_code,
                declared_bytes=header[1],
                started=started,
            )
        received, digest, chunks = await _stream_to_file(
            response=response,
            staging=staging,
            maximum_bytes=resolution.entry.archive_size_bytes,
            renewer=renewer,
        )
        status = _download_status(
            received=received,
            digest=digest,
            expected_bytes=resolution.entry.archive_size_bytes,
            expected_digest=resolution.entry.archive_sha256,
        )
        if status == "completed":
            await renewer.assert_current()
            _commit_staging(staging, destination)
        return _attempt(
            url_index=url_index,
            url=url,
            status=status,
            http_status=response.status_code,
            declared_bytes=header[1],
            received_bytes=received,
            received_sha256=digest,
            chunk_count=chunks,
            started=started,
        )

    async def _require_current_resolution(self, resolution) -> None:
        try:
            view = await self.catalog_store.inspect(
                catalog_id=resolution.catalog_id
            )
            channel_policy, build_policy = (
                self.catalog_store.current_trust_policies()
            )
        except ReleaseChannelCatalogError as exc:
            raise ReleaseArtifactFetchError(
                "release_download_resolution_stale",
                "Release Channel Resolution 当前不可用。",
            ) from exc
        entry = next(
            (
                item
                for item in view.catalog.payload.entries
                if item.target == resolution.target
            ),
            None,
        )
        urls = tuple(
            sorted(
                f"{origin}/{resolution.entry.archive_path}"
                for origin in channel_policy.archive_origins
            )
        )
        if not (
            view.download_input_authority
            and view.catalog.catalog_id == resolution.catalog_id
            and view.catalog.catalog_sha256 == resolution.catalog_sha256
            and entry == resolution.entry
            and resolution.channel_trust_policy_id == channel_policy.policy_id
            and resolution.channel_trust_policy_sha256 == channel_policy.policy_sha256
            and resolution.build_trust_policy_id == build_policy.policy_id
            and resolution.build_trust_policy_sha256 == build_policy.policy_sha256
            and resolution.archive_urls == urls
        ):
            raise ReleaseArtifactFetchError(
                "release_download_resolution_stale",
                "Catalog、Trust Policy、target Entry 或 pinned origins 已变化。",
            )

    async def _view(self, receipt):
        source_id, _source_sha = _source_identity(receipt.resolution)
        try:
            stored = await self.store.get(source_id)
            receipt_current = stored == receipt
        except ReleaseArtifactFetchError:
            receipt_current = False
        try:
            await self._require_current_resolution(receipt.resolution)
            resolution_current = True
            catalog_current = True
        except ReleaseArtifactFetchError:
            resolution_current = False
            catalog_current = False
        archive_current = _archive_current(
            Path(receipt.archive_path),
            expected_bytes=receipt.archive_size_bytes,
            expected_sha256=receipt.archive_sha256,
        )
        checks = (
            ("receipt_source_changed", receipt_current),
            ("resolution_source_changed", resolution_current),
            ("catalog_authority_changed", catalog_current),
            ("archive_file_changed", archive_current),
        )
        reasons = tuple(sorted(reason for reason, passed in checks if not passed))
        current = all(passed for _reason, passed in checks)
        return ReleaseArtifactDownloadView(
            receipt=receipt,
            receipt_source_current=receipt_current,
            resolution_source_current=resolution_current,
            catalog_authority_current=catalog_current,
            archive_file_current=archive_current,
            invalidation_reasons=reasons,
            verified_archive_authority=current,
            installation_input_authority=current,
        )


class _ClaimRenewer:
    def __init__(self, *, store, claim, lease_seconds) -> None:
        self.store = store
        self.claim = claim
        self.lease_seconds = lease_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._error: BaseException | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=max(1.0, self.lease_seconds / 3),
                    )
                except TimeoutError:
                    self.claim = await self.store.renew(
                        self.claim,
                        lease_seconds=self.lease_seconds,
                    )
        except ReleaseArtifactFetchError as exc:
            self._error = exc

    async def assert_current(self) -> None:
        if self._error is not None:
            raise ReleaseArtifactFetchError(
                "release_download_claim_fenced",
                "Download claim renewal 已失败。",
            ) from self._error

    async def stop(self, *, suppress: bool = False) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
        if self._error is not None and not suppress:
            raise ReleaseArtifactFetchError(
                "release_download_claim_fenced",
                "Download claim renewal 已失败。",
            ) from self._error


async def _stream_to_file(*, response, staging, maximum_bytes, renewer):
    digest = hashlib.sha256()
    received = 0
    chunks = 0
    with staging.open("xb") as output:
        async for chunk in response.aiter_raw(_CHUNK_BYTES):
            if not chunk:
                continue
            received += len(chunk)
            chunks += 1
            digest.update(chunk)
            if received > maximum_bytes:
                break
            output.write(chunk)
            await renewer.assert_current()
        output.flush()
        os.fsync(output.fileno())
    return received, digest.hexdigest(), chunks


def _response_header_state(response, expected_bytes):
    if response.status_code != 200:
        return "http_error", _content_length(response.headers)
    encoding = response.headers.get("content-encoding", "identity").casefold()
    if encoding not in {"", "identity"}:
        return "header_invalid", _content_length(response.headers)
    declared = _content_length(response.headers)
    if declared is None:
        return "header_invalid", None
    if declared != expected_bytes:
        return "size_mismatch", declared
    return None, declared


def _content_length(headers) -> int | None:
    raw = headers.get("content-length")
    if raw is None or not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if value <= 8 * 1024**3 else None


def _download_status(*, received, digest, expected_bytes, expected_digest):
    if received != expected_bytes:
        return "size_mismatch"
    if not hmac.compare_digest(digest, expected_digest):
        return "digest_mismatch"
    return "completed"


def _attempt(
    *,
    url_index,
    url,
    status,
    started,
    http_status=None,
    declared_bytes=None,
    received_bytes=0,
    received_sha256="",
    chunk_count=0,
):
    return ReleaseArtifactFetchAttempt(
        url_index=url_index,
        url_sha256=hashlib.sha256(url.encode()).hexdigest(),
        status=status,
        http_status=http_status,
        declared_bytes=declared_bytes,
        received_bytes=received_bytes,
        received_sha256=received_sha256,
        chunk_count=chunk_count,
        duration_ms=min(86_400_000, int((time.monotonic() - started) * 1000)),
    )


def _existing_attempt(destination, resolution):
    if not destination.exists():
        return None
    started = time.monotonic()
    if not _archive_current(
        destination,
        expected_bytes=resolution.entry.archive_size_bytes,
        expected_sha256=resolution.entry.archive_sha256,
    ):
        raise ReleaseArtifactFetchError(
            "release_download_existing_artifact_invalid",
            "Content-addressed download path 已存在不匹配文件。",
        )
    _make_read_only(destination)
    _fsync_file(destination)
    _fsync_directory(destination.parent)
    return _attempt(
        url_index=0,
        url=resolution.archive_urls[0],
        status="existing_verified",
        declared_bytes=resolution.entry.archive_size_bytes,
        received_bytes=resolution.entry.archive_size_bytes,
        received_sha256=resolution.entry.archive_sha256,
        chunk_count=0,
        started=started,
    )


def _build_receipt(*, resolution, destination, attempts, claim, downloaded_at):
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_ARTIFACT_DOWNLOAD_POLICY,
        "source_id": claim.source_id,
        "source_sha256": claim.source_sha256,
        "resolution": resolution.model_dump(mode="json"),
        "archive_path": str(destination),
        "archive_sha256": resolution.entry.archive_sha256,
        "archive_size_bytes": resolution.entry.archive_size_bytes,
        "attempts": [item.model_dump(mode="json") for item in attempts],
        "selected_url_index": attempts[-1].url_index,
        "selected_url_sha256": attempts[-1].url_sha256,
        "claim_epoch": claim.epoch,
        "claim_owner_sha256": hashlib.sha256(claim.owner_id.encode()).hexdigest(),
        "downloaded_at": downloaded_at,
        "content_size_verified": True,
        "http_content_length_verified": attempts[-1].status == "completed",
        "archive_digest_verified": True,
        "regular_file_verified": True,
        "file_fsync_completed": True,
        "directory_fsync_completed": True,
        "atomic_commit_completed": True,
        "immutable": True,
        "verified_archive_authority": True,
        "installation_input_authority": True,
        "extraction_executed": False,
        "installation_executed": False,
        "deployment_authority": False,
    }
    digest = _digest(core)
    return ReleaseArtifactDownloadReceipt.model_validate(
        {
            **core,
            "resolution": resolution,
            "attempts": attempts,
            "receipt_id": f"reldownload_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _destination(root: Path, resolution: ReleaseChannelResolution) -> Path:
    return (
        root
        / "sha256"
        / resolution.entry.archive_sha256
        / resolution.entry.archive_name
    ).resolve()


def _commit_staging(staging: Path, destination: Path) -> None:
    if not staging.is_file() or staging.is_symlink():
        raise ReleaseArtifactFetchError(
            "release_download_staging_invalid",
            "Download staging 不是 regular file。",
        )
    _make_read_only(staging)
    _fsync_file(staging)
    os.replace(staging, destination)
    _fsync_directory(destination.parent)


def _make_read_only(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(mode & ~0o222)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _archive_current(path: Path, *, expected_bytes: int, expected_sha256: str) -> bool:
    try:
        stat_result = path.lstat()
        if not stat.S_ISREG(stat_result.st_mode) or path.is_symlink():
            return False
        if stat_result.st_size != expected_bytes or stat_result.st_mode & 0o222:
            return False
        return hmac.compare_digest(_sha256_file(path), expected_sha256)
    except OSError:
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


def _unlink_staging(path: Path) -> None:
    path.unlink(missing_ok=True)


def _source_identity(resolution: ReleaseChannelResolution) -> tuple[str, str]:
    core = {
        "catalog_id": resolution.catalog_id,
        "catalog_sha256": resolution.catalog_sha256,
        "channel": resolution.channel,
        "target": resolution.target,
        "entry": resolution.entry.model_dump(mode="json"),
        "channel_trust_policy_sha256": resolution.channel_trust_policy_sha256,
        "build_trust_policy_sha256": resolution.build_trust_policy_sha256,
        "archive_urls": resolution.archive_urls,
    }
    digest = _digest(core)
    return f"reldownloadsource_{digest[:24]}", digest


def _validated_resolution(value) -> ReleaseChannelResolution:
    try:
        return ReleaseChannelResolution.model_validate_json(value.model_dump_json())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseArtifactFetchError(
            "release_download_resolution_invalid",
            "Release Channel Resolution artifact 无效。",
        ) from exc


def _validated_receipt(value) -> ReleaseArtifactDownloadReceipt:
    try:
        return ReleaseArtifactDownloadReceipt.model_validate_json(
            value.model_dump_json()
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseArtifactFetchError(
            "release_download_receipt_invalid",
            "Release Artifact Download Receipt 无效。",
        ) from exc


def _restore_receipt(value: str) -> ReleaseArtifactDownloadReceipt:
    if len(value.encode()) > _MAX_RECEIPT_BYTES:
        raise ValueError("Download Receipt durable source 超过 512 KiB。")
    return ReleaseArtifactDownloadReceipt.model_validate_json(value)


def _claim_from_row(row) -> _Claim:
    receipt = (
        None
        if row["receipt_json"] is None
        else _restore_receipt(str(row["receipt_json"]))
    )
    return _Claim(
        status="completed" if receipt is not None else "busy",
        source_id=str(row["source_id"]),
        source_sha256=str(row["source_sha256"]),
        owner_id=str(row["owner_id"]),
        epoch=int(row["claim_epoch"]),
        expires_at=str(row["claim_expires_at"]),
        receipt=receipt,
    )


def _claim_matches(current: _Claim | None, presented: _Claim) -> bool:
    return bool(
        current is not None
        and current.receipt is None
        and current.owner_id == presented.owner_id
        and current.epoch == presented.epoch
        and current.source_sha256 == presented.source_sha256
    )


def _available_claim(
    claim: _Claim | None,
    *,
    source_sha: str,
    owner_id: str,
    now: datetime,
) -> _Claim | None:
    if claim is None:
        return None
    if claim.source_sha256 != source_sha:
        raise ReleaseArtifactFetchError(
            "release_download_source_conflict",
            "Download source identity collision。",
        )
    if claim.receipt is not None:
        return claim
    if _aware(claim.expires_at) <= now:
        return None
    if claim.owner_id != owner_id:
        return claim
    return _Claim(
        status="acquired",
        source_id=claim.source_id,
        source_sha256=claim.source_sha256,
        owner_id=claim.owner_id,
        epoch=claim.epoch,
        expires_at=claim.expires_at,
    )


async def _upsert_claim(
    db,
    *,
    source_id,
    source_sha,
    owner_id,
    epoch,
    expires,
    now,
) -> None:
    await db.execute(
        "INSERT INTO release_artifact_downloads "
        "(source_id, source_sha256, owner_id, claim_epoch, claim_expires_at, "
        "receipt_json, updated_at) VALUES (?, ?, ?, ?, ?, NULL, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET owner_id=excluded.owner_id, "
        "claim_epoch=excluded.claim_epoch, "
        "claim_expires_at=excluded.claim_expires_at, updated_at=excluded.updated_at",
        (
            source_id,
            source_sha,
            owner_id,
            epoch,
            expires.isoformat(),
            now.isoformat(),
        ),
    )


def _require_owner(value: str) -> None:
    if not 1 <= len(value) <= 256 or any(char in value for char in "\x00\n\r"):
        raise ValueError("Download owner_id 必须为 1..256 个无控制字符文本。")


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Download timestamp 必须包含 offset。")
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


async def _claim_row(db, source_id):
    return await (
        await db.execute(
            "SELECT * FROM release_artifact_downloads WHERE source_id = ?",
            (source_id,),
        )
    ).fetchone()


async def _ensure_schema(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS release_artifact_downloads ("
        "source_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL UNIQUE, "
        "owner_id TEXT NOT NULL, claim_epoch INTEGER NOT NULL, "
        "claim_expires_at TEXT NOT NULL, receipt_json TEXT, updated_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "RELEASE_ARTIFACT_DOWNLOAD_POLICY",
    "HttpxReleaseArtifactTransport",
    "ReleaseArtifactDownloadReceipt",
    "ReleaseArtifactDownloadStore",
    "ReleaseArtifactDownloadView",
    "ReleaseArtifactFetchAttempt",
    "ReleaseArtifactFetchError",
    "ReleaseArtifactFetchService",
    "ReleaseArtifactStreamResponse",
    "ReleaseArtifactTransport",
]
