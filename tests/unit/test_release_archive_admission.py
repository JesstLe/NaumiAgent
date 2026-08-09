from __future__ import annotations

import asyncio
import io
import tarfile
from datetime import timedelta
from pathlib import Path, PurePosixPath

import aiosqlite
import httpx
import pytest

from naumi_agent.release.archive_admission import (
    ReleaseArchiveAdmissionError,
    ReleaseArchiveAdmissionService,
    ReleaseArchiveAdmissionStore,
    _bounded_total,
    _member_path,
    _safe_link_target,
)
from naumi_agent.release.artifact import assemble_release_artifact
from naumi_agent.release.artifact_fetch import (
    HttpxReleaseArtifactTransport,
    ReleaseArtifactDownloadStore,
    ReleaseArtifactFetchService,
)
from naumi_agent.release.build_attestations import (
    create_release_build_attestation,
    load_release_build_attestation,
)
from naumi_agent.release.channel_catalog import (
    ReleaseChannelCatalogStore,
    ReleaseChannelEntry,
)
from naumi_agent.release.slots import ReleaseSlotStore
from tests.unit.test_release_build_attestations import (
    SOURCE_COMMIT,
    SOURCE_TREE_SHA256,
    _signer,
)
from tests.unit.test_release_build_attestations import (
    _context as _build_context,
)
from tests.unit.test_release_channel_catalog import (
    T0,
    _channel_signer,
    _policies,
)


class _BytesStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self):
        for offset in range(0, len(self.content), 4096):
            yield self.content[offset : offset + 4096]


def _binary(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def _artifact(
    root: Path,
    *,
    archive_format: str,
    target: str | None = None,
    source_commit: str = SOURCE_COMMIT,
    source_tree_sha256: str = SOURCE_TREE_SHA256,
):
    signer, _private = _signer()
    windows = archive_format == "zip"
    target = target or ("windows-x64" if windows else "linux-x64")
    windows = target.startswith("windows-")
    backend_name = "naumi-runtime.exe" if windows else "naumi-runtime"
    launcher_name = "naumi.exe" if windows else "naumi"
    backend = root / "backend"
    _binary(backend / backend_name, b"frozen-runtime")
    launcher = _binary(root / "launcher" / launcher_name, b"stable-launcher")
    ui = _binary(root / ("naumi-ui.exe" if windows else "naumi-ui"), b"terminal-ui")
    config = root / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")
    artifact = assemble_release_artifact(
        backend_dir=backend,
        launcher_dir=launcher.parent,
        ui_binary=ui,
        config_example=config,
        output_dir=root / "release",
        version="1.2.3",
        target=target,
        source_commit=source_commit,
        source_tree_sha256=source_tree_sha256,
        archive_format=archive_format,
        build_signer=signer,
        build_context=_build_context(),
    )
    return signer, artifact, target


async def _runtime(
    tmp_path: Path,
    *,
    archive_format: str = "tar.gz",
    archive_builder=None,
    target: str | None = None,
    source_commit: str = SOURCE_COMMIT,
    source_tree_sha256: str = SOURCE_TREE_SHA256,
    base_time=T0,
):
    signer, artifact, target = _artifact(
        tmp_path / "artifact-input",
        archive_format=archive_format,
        target=target,
        source_commit=source_commit,
        source_tree_sha256=source_tree_sha256,
    )
    archive = artifact.archive
    if archive_builder is not None:
        malicious = tmp_path / "malicious" / archive.name
        malicious.parent.mkdir(parents=True)
        archive_builder(malicious, archive.name.removesuffix(".tar.gz"))
        archive = malicious
        attestation = create_release_build_attestation(
            signer=signer,
            context=_build_context(),
            manifest_path=artifact.manifest,
            archive_path=archive,
        )
    else:
        assert artifact.attestation is not None
        attestation = load_release_build_attestation(artifact.attestation)
    channel_signer = _channel_signer()
    policies = list(_policies(channel_signer, signer))
    entry = ReleaseChannelEntry(
        target=target,
        version="1.2.3",
        release_generation=123,
        archive_path=f"releases/v1.2.3/{archive.name}",
        archive_name=archive.name,
        archive_sha256=attestation.payload.archive_sha256,
        archive_size_bytes=archive.stat().st_size,
        manifest_sha256=attestation.payload.manifest_sha256,
        build_attestation=attestation,
    )
    catalog = channel_signer.issue(
        channel="stable",
        entries=(entry,),
        previous=None,
        generated_at=base_time.isoformat(),
        valid_from=(base_time + timedelta(seconds=1)).isoformat(),
        expires_at=(base_time + timedelta(days=7)).isoformat(),
    )
    catalog_store = ReleaseChannelCatalogStore(
        tmp_path / ".naumi" / "release-channel.db",
        channel_trust_policy_provider=lambda: policies[0],
        build_trust_policy_provider=lambda: policies[1],
        clock=lambda: base_time + timedelta(seconds=2),
    )
    await catalog_store.record(catalog)
    resolution = await catalog_store.resolve(channel="stable", target=target)
    archive_bytes = archive.read_bytes()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(archive_bytes))},
            stream=_BytesStream(archive_bytes),
        )

    download_store = ReleaseArtifactDownloadStore(
        catalog_store.db_path,
        catalog_store=catalog_store,
        clock=lambda: base_time + timedelta(seconds=3),
    )
    fetch_service = ReleaseArtifactFetchService(
        download_root=tmp_path / ".naumi" / "downloads",
        catalog_store=catalog_store,
        store=download_store,
        transport=HttpxReleaseArtifactTransport(transport=httpx.MockTransport(handler)),
        owner_id="archive-admission-fetch",
        lease_seconds=5,
        clock=lambda: base_time + timedelta(seconds=3),
    )
    download = await fetch_service.fetch(resolution)
    slot_store = ReleaseSlotStore(tmp_path / "install-root")
    admission_store = ReleaseArchiveAdmissionStore(
        catalog_store.db_path,
        download_store=download_store,
    )
    return {
        "signer": signer,
        "channel_signer": channel_signer,
        "policies": policies,
        "download": download,
        "fetch_service": fetch_service,
        "slot_store": slot_store,
        "admission_store": admission_store,
        "staging_root": tmp_path / ".naumi" / "archive-staging",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
async def test_signed_archive_admission_converges_and_dynamically_revokes(
    tmp_path: Path,
    archive_format: str,
) -> None:
    runtime = await _runtime(tmp_path, archive_format=archive_format)
    services = tuple(
        ReleaseArchiveAdmissionService(
            staging_root=runtime["staging_root"],
            fetch_service=runtime["fetch_service"],
            slot_store=runtime["slot_store"],
            store=runtime["admission_store"],
            clock=lambda: T0 + timedelta(seconds=4),
        )
        for _index in range(3)
    )
    source_id = runtime["download"].receipt.source_id
    views = await asyncio.gather(
        *(service.admit(download_source_id=source_id) for service in services)
    )
    view = views[0]
    assert all(item == view for item in views)
    assert view.archive_admission_authority
    assert view.percentage_deployment_intent_input_authority
    assert view.receipt.archive_format == archive_format
    assert view.receipt.extraction_executed
    assert view.receipt.installation_executed
    assert not view.receipt.boot_executed
    assert not view.receipt.deployment_authority
    assert runtime["slot_store"].active() is None
    slot = runtime["slot_store"].inspect_installed_slot(
        view.receipt.installed_slot.slot_id
    )
    assert slot == view.receipt.installed_slot
    assert not tuple(runtime["staging_root"].glob(".archive-admission-*"))

    async with aiosqlite.connect(runtime["admission_store"].db_path) as db:
        await db.execute(
            "DELETE FROM release_archive_admissions WHERE download_source_id = ?",
            (source_id,),
        )
        await db.commit()
    recovered = await services[0].admit(download_source_id=source_id)
    assert recovered.receipt == view.receipt
    assert recovered.installed_slot_current

    original_build_policy = runtime["policies"][1]
    runtime["policies"][1] = _policies(
        runtime["channel_signer"],
        runtime["signer"],
        build_state="revoked",
    )[1]
    revoked = await services[0].inspect(download_source_id=source_id)
    assert not revoked.build_trust_current
    assert not revoked.archive_admission_authority
    runtime["policies"][1] = original_build_policy
    assert (await services[0].inspect(download_source_id=source_id)).archive_admission_authority

    archive_path = Path(view.receipt.download_receipt.archive_path)
    archive_bytes = archive_path.read_bytes()
    archive_path.chmod(0o600)
    archive_path.write_bytes(archive_bytes + b"tampered")
    archive_path.chmod(0o400)
    archive_tampered = await services[0].inspect(download_source_id=source_id)
    assert not archive_tampered.download_source_current
    assert not archive_tampered.archive_admission_authority
    archive_path.chmod(0o600)
    archive_path.write_bytes(archive_bytes)
    archive_path.chmod(0o400)

    runtime_path = Path(slot.bundle_dir) / slot.backend_path
    runtime_path.chmod(0o600)
    runtime_path.write_bytes(b"tampered-runtime")
    tampered = await services[0].inspect(download_source_id=source_id)
    assert not tampered.installed_slot_current
    assert not tampered.percentage_deployment_intent_input_authority


def _tar_with_traversal(path: Path, _bundle: str) -> None:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("../outside")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))


def _tar_with_symlink_escape(path: Path, bundle: str) -> None:
    with tarfile.open(path, "w:gz") as archive:
        root = tarfile.TarInfo(bundle)
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        link = tarfile.TarInfo(f"{bundle}/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)


def _tar_with_hardlink(path: Path, bundle: str) -> None:
    with tarfile.open(path, "w:gz") as archive:
        link = tarfile.TarInfo(f"{bundle}/hard")
        link.type = tarfile.LNKTYPE
        link.linkname = f"{bundle}/target"
        archive.addfile(link)


def _tar_with_case_collision(path: Path, bundle: str) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name in ("Runtime", "runtime"):
            info = tarfile.TarInfo(f"{bundle}/{name}")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))


def _tar_with_wrong_manifest(path: Path, bundle: str) -> None:
    encoded = b"{}\n"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(f"{bundle}/manifest.json")
        info.size = len(encoded)
        archive.addfile(info, io.BytesIO(encoded))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "archive_builder",
    [
        _tar_with_traversal,
        _tar_with_symlink_escape,
        _tar_with_hardlink,
        _tar_with_case_collision,
        _tar_with_wrong_manifest,
    ],
)
async def test_signed_malicious_archive_is_rejected_before_slot_install(
    tmp_path: Path,
    archive_builder,
) -> None:
    runtime = await _runtime(tmp_path, archive_builder=archive_builder)
    service = ReleaseArchiveAdmissionService(
        staging_root=runtime["staging_root"],
        fetch_service=runtime["fetch_service"],
        slot_store=runtime["slot_store"],
        store=runtime["admission_store"],
        clock=lambda: T0 + timedelta(seconds=4),
    )
    with pytest.raises(ReleaseArchiveAdmissionError):
        await service.admit(download_source_id=runtime["download"].receipt.source_id)
    assert runtime["slot_store"].active() is None
    assert not runtime["slot_store"].slots_dir.exists()
    assert not (tmp_path / "outside").exists()
    assert not tuple(runtime["staging_root"].glob(".archive-admission-*"))


def test_archive_boundaries_reject_expansion_and_cross_platform_names() -> None:
    with pytest.raises(ReleaseArchiveAdmissionError) as expansion:
        _bounded_total(64, 65, 128)
    assert expansion.value.code == "release_archive_expansion_limit"
    for path in (
        "bundle/CON.txt",
        "bundle/name. ",
        "bundle/a\\b",
        "bundle/../escape",
    ):
        with pytest.raises(ReleaseArchiveAdmissionError):
            _member_path(path, directory=False, bundle_name="bundle")
    with pytest.raises(ReleaseArchiveAdmissionError) as windows_link:
        _safe_link_target(PurePosixPath("bundle/link"), "C:/outside", "bundle")
    assert windows_link.value.code == "release_archive_symlink_invalid"


@pytest.mark.asyncio
async def test_archive_admission_receipt_tamper_fails_closed(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    service = ReleaseArchiveAdmissionService(
        staging_root=runtime["staging_root"],
        fetch_service=runtime["fetch_service"],
        slot_store=runtime["slot_store"],
        store=runtime["admission_store"],
        clock=lambda: T0 + timedelta(seconds=4),
    )
    with pytest.raises(ReleaseArchiveAdmissionError) as invalid_source:
        await service.inspect(download_source_id="../../release")
    assert invalid_source.value.code == "release_archive_download_source_invalid"
    source_id = runtime["download"].receipt.source_id
    view = await service.admit(download_source_id=source_id)
    async with aiosqlite.connect(runtime["admission_store"].db_path) as db:
        await db.execute(
            "UPDATE release_archive_admissions SET receipt_json = replace("
            "receipt_json, ?, ?) WHERE download_source_id = ?",
            (view.receipt.admission_sha256, "0" * 64, source_id),
        )
        await db.commit()
    with pytest.raises(ReleaseArchiveAdmissionError) as corrupt:
        await runtime["admission_store"].get_by_download_source(source_id)
    assert corrupt.value.code == "release_archive_admission_source_invalid"
