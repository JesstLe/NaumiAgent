from __future__ import annotations

import asyncio
import hashlib
import stat
from datetime import timedelta
from pathlib import Path

import aiosqlite
import httpx
import pytest

from naumi_agent.release.artifact_fetch import (
    HttpxReleaseArtifactTransport,
    ReleaseArtifactDownloadStore,
    ReleaseArtifactFetchError,
    ReleaseArtifactFetchService,
    _source_identity,
)
from naumi_agent.release.channel_catalog import ReleaseChannelCatalogStore
from tests.unit.test_release_channel_catalog import (
    T0,
    _build_signer,
    _channel_signer,
    _entry,
    _policies,
)


async def _context(tmp_path: Path):
    channel_signer = _channel_signer()
    build_signer = _build_signer()
    channel_policy, build_policy = _policies(channel_signer, build_signer)
    entry = _entry(
        tmp_path,
        build_signer,
        version="1.2.3",
        release_generation=123,
    )
    catalog = channel_signer.issue(
        channel="stable",
        entries=(entry,),
        previous=None,
        generated_at=T0.isoformat(),
        valid_from=(T0 + timedelta(seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=7)).isoformat(),
    )
    catalog_store = ReleaseChannelCatalogStore(
        tmp_path / ".naumi" / "release-channel.db",
        channel_trust_policy_provider=lambda: channel_policy,
        build_trust_policy_provider=lambda: build_policy,
        clock=lambda: T0 + timedelta(seconds=2),
    )
    await catalog_store.record(catalog)
    resolution = await catalog_store.resolve(channel="stable", target="linux-x64")
    archive_bytes = b"source-free-1.2.3-linux-x64"
    assert len(archive_bytes) == entry.archive_size_bytes
    assert hashlib.sha256(archive_bytes).hexdigest() == entry.archive_sha256
    return (
        channel_signer,
        build_signer,
        catalog,
        catalog_store,
        resolution,
        archive_bytes,
    )


def _transport(handler):
    return HttpxReleaseArtifactTransport(transport=httpx.MockTransport(handler))


class _BytesStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes, *, chunk_bytes: int = 7) -> None:
        self.content = content
        self.chunk_bytes = chunk_bytes

    async def __aiter__(self):
        for offset in range(0, len(self.content), self.chunk_bytes):
            yield self.content[offset : offset + self.chunk_bytes]


def _response(status: int, content: bytes, *, declared: int | None = None):
    length = len(content) if declared is None else declared
    return httpx.Response(
        status,
        headers={"Content-Length": str(length)},
        stream=_BytesStream(content),
    )


def _raw_response(
    status: int,
    content: bytes,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers,
        stream=_BytesStream(content),
    )


@pytest.mark.asyncio
async def test_verified_fetch_converges_and_revokes_on_file_or_catalog_change(
    tmp_path: Path,
) -> None:
    (
        channel_signer,
        build_signer,
        first_catalog,
        catalog_store,
        resolution,
        archive_bytes,
    ) = await _context(tmp_path)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        assert request.headers["accept-encoding"] == "identity"
        return _response(200, archive_bytes)

    def service(index: int):
        store = ReleaseArtifactDownloadStore(
            catalog_store.db_path,
            catalog_store=catalog_store,
            clock=lambda: T0 + timedelta(seconds=3),
        )
        return ReleaseArtifactFetchService(
            download_root=tmp_path / ".naumi" / "downloads",
            catalog_store=catalog_store,
            store=store,
            transport=_transport(handler),
            owner_id=f"fetch-worker-{index}",
            lease_seconds=5,
            wait_timeout_seconds=5,
            clock=lambda: T0 + timedelta(seconds=3),
        )

    services = tuple(service(index) for index in range(4))
    views = await asyncio.gather(*(item.fetch(resolution) for item in services))
    view = views[0]
    assert all(item == view for item in views)
    assert calls == 1
    assert view.verified_archive_authority
    assert view.installation_input_authority
    assert view.receipt.http_content_length_verified
    assert view.receipt.attempts[-1].status == "completed"
    archive_path = Path(view.receipt.archive_path)
    assert archive_path.read_bytes() == archive_bytes
    assert not stat.S_IMODE(archive_path.stat().st_mode) & 0o222
    assert not view.receipt.extraction_executed
    assert not view.receipt.installation_executed
    assert not view.deployment_authority

    archive_path.chmod(0o600)
    archive_path.write_bytes(b"x" * len(archive_bytes))
    archive_path.chmod(0o400)
    corrupt = await services[0].inspect(source_id=view.receipt.source_id)
    assert not corrupt.archive_file_current
    assert not corrupt.installation_input_authority
    archive_path.chmod(0o600)
    archive_path.write_bytes(archive_bytes)
    archive_path.chmod(0o400)

    next_entry = _entry(
        tmp_path,
        build_signer,
        version="1.2.4",
        release_generation=124,
    )
    next_catalog = channel_signer.issue(
        channel="stable",
        entries=(next_entry,),
        previous=first_catalog,
        generated_at=(T0 + timedelta(hours=1)).isoformat(),
        valid_from=(T0 + timedelta(hours=1, seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=7)).isoformat(),
    )
    catalog_store.clock = lambda: T0 + timedelta(hours=1, seconds=2)
    await catalog_store.record(next_catalog)
    historical = await services[0].inspect(source_id=view.receipt.source_id)
    assert not historical.catalog_authority_current
    assert not historical.verified_archive_authority


@pytest.mark.asyncio
async def test_fetch_falls_back_recovers_atomic_file_and_rejects_bad_streams(
    tmp_path: Path,
) -> None:
    *_prefix, catalog_store, resolution, archive_bytes = await _context(tmp_path)
    request_index = 0

    def fallback_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_index
        request_index += 1
        if request_index == 1:
            return _response(503, b"")
        return _response(200, archive_bytes)

    store = ReleaseArtifactDownloadStore(
        catalog_store.db_path,
        catalog_store=catalog_store,
        clock=lambda: T0 + timedelta(seconds=3),
    )
    service = ReleaseArtifactFetchService(
        download_root=tmp_path / ".naumi" / "downloads",
        catalog_store=catalog_store,
        store=store,
        transport=_transport(fallback_handler),
        owner_id="fallback-worker",
        lease_seconds=5,
        clock=lambda: T0 + timedelta(seconds=3),
    )
    view = await service.fetch(resolution)
    assert [item.status for item in view.receipt.attempts] == [
        "http_error",
        "completed",
    ]
    assert view.receipt.selected_url_index == 1

    source_id, _source_sha = _source_identity(resolution)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "DELETE FROM release_artifact_downloads WHERE source_id = ?",
            (source_id,),
        )
        await db.commit()
    no_network_calls = 0

    def forbidden_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal no_network_calls
        no_network_calls += 1
        raise AssertionError("verified crash-recovery file must be reused")

    recovered = await ReleaseArtifactFetchService(
        download_root=tmp_path / ".naumi" / "downloads",
        catalog_store=catalog_store,
        store=store,
        transport=_transport(forbidden_handler),
        owner_id="recovery-worker",
        lease_seconds=5,
        clock=lambda: T0 + timedelta(seconds=4),
    ).fetch(resolution)
    assert no_network_calls == 0
    assert recovered.receipt.attempts[-1].status == "existing_verified"
    assert not recovered.receipt.http_content_length_verified

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "DELETE FROM release_artifact_downloads WHERE source_id = ?",
            (source_id,),
        )
        await db.commit()
    recovered_path = Path(recovered.receipt.archive_path)
    recovered_path.chmod(stat.S_IREAD | stat.S_IWRITE)
    recovered_path.unlink()

    def bad_digest(_request: httpx.Request) -> httpx.Response:
        return _response(200, b"z" * len(archive_bytes))

    failing = ReleaseArtifactFetchService(
        download_root=tmp_path / ".naumi" / "downloads",
        catalog_store=catalog_store,
        store=store,
        transport=_transport(bad_digest),
        owner_id="bad-digest-worker",
        lease_seconds=5,
        clock=lambda: T0 + timedelta(seconds=5),
    )
    with pytest.raises(ReleaseArtifactFetchError) as rejected:
        await failing.fetch(resolution)
    assert rejected.value.code == "release_download_all_origins_failed"
    assert await store.get(source_id) is None
    assert not Path(recovered.receipt.archive_path).exists()
    assert not tuple(Path(recovered.receipt.archive_path).parent.glob("*.part.*"))


@pytest.mark.asyncio
async def test_download_claim_expiry_fences_old_owner_and_receipt_tamper(
    tmp_path: Path,
) -> None:
    *_prefix, catalog_store, resolution, archive_bytes = await _context(tmp_path)
    now = [T0 + timedelta(seconds=3)]
    store = ReleaseArtifactDownloadStore(
        catalog_store.db_path,
        catalog_store=catalog_store,
        clock=lambda: now[0],
    )
    first = await store.claim(resolution, owner_id="owner-one", lease_seconds=5)
    now[0] += timedelta(seconds=6)
    second = await store.claim(resolution, owner_id="owner-two", lease_seconds=5)
    assert second.epoch == first.epoch + 1
    with pytest.raises(ReleaseArtifactFetchError) as fenced:
        await store.renew(first, lease_seconds=5)
    assert fenced.value.code == "release_download_claim_fenced"
    await store.release(second)

    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(200, archive_bytes)

    now[0] += timedelta(seconds=1)
    service = ReleaseArtifactFetchService(
        download_root=tmp_path / ".naumi" / "downloads",
        catalog_store=catalog_store,
        store=store,
        transport=_transport(handler),
        owner_id="owner-three",
        lease_seconds=5,
        clock=lambda: now[0],
    )
    view = await service.fetch(resolution)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE release_artifact_downloads SET receipt_json = replace("
            "receipt_json, ?, ?) WHERE source_id = ?",
            (view.receipt.receipt_sha256, "0" * 64, view.receipt.source_id),
        )
        await db.commit()
    with pytest.raises(ReleaseArtifactFetchError) as corrupt:
        await store.get(view.receipt.source_id)
    assert corrupt.value.code == "release_download_source_invalid"


@pytest.mark.asyncio
async def test_fetch_rejects_redirect_encoding_and_body_size_boundaries(
    tmp_path: Path,
) -> None:
    *_prefix, catalog_store, resolution, archive_bytes = await _context(tmp_path)
    store = ReleaseArtifactDownloadStore(
        catalog_store.db_path,
        catalog_store=catalog_store,
        clock=lambda: T0 + timedelta(seconds=3),
    )
    expected = len(archive_bytes)
    cases = (
        lambda: _raw_response(
            302,
            b"",
            headers={"Location": "https://redirect.invalid/archive"},
        ),
        lambda: _raw_response(200, archive_bytes),
        lambda: _raw_response(
            200,
            archive_bytes,
            headers={
                "Content-Length": str(expected),
                "Content-Encoding": "gzip",
            },
        ),
        lambda: _raw_response(
            200,
            archive_bytes[:-1],
            headers={"Content-Length": str(expected)},
        ),
        lambda: _raw_response(
            200,
            archive_bytes + b"x",
            headers={"Content-Length": str(expected)},
        ),
    )
    source_id, _source_sha = _source_identity(resolution)
    download_root = tmp_path / ".naumi" / "downloads"

    for index, response_factory in enumerate(cases):
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return response_factory()

        service = ReleaseArtifactFetchService(
            download_root=download_root,
            catalog_store=catalog_store,
            store=store,
            transport=_transport(handler),
            owner_id=f"boundary-worker-{index}",
            lease_seconds=5,
            clock=lambda: T0 + timedelta(seconds=3),
        )
        with pytest.raises(ReleaseArtifactFetchError) as rejected:
            await service.fetch(resolution)
        assert rejected.value.code == "release_download_all_origins_failed"
        assert calls == len(resolution.archive_urls)
        assert await store.get(source_id) is None
        assert not tuple(path for path in download_root.rglob("*") if path.is_file())
