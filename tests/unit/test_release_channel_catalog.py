from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.release.build_attestations import (
    ReleaseBuildContext,
    ReleaseBuildSigner,
    ReleaseTrustedBuilderKey,
    create_release_build_attestation,
    create_release_build_trust_policy,
)
from naumi_agent.release.channel_catalog import (
    ReleaseChannelCatalog,
    ReleaseChannelCatalogError,
    ReleaseChannelCatalogSigner,
    ReleaseChannelCatalogStore,
    ReleaseChannelEntry,
    ReleaseTrustedChannelKey,
    create_release_channel_trust_policy,
    load_release_channel_trust_policy,
)

T0 = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _channel_signer(seed: bytes = bytes(range(32))):
    return ReleaseChannelCatalogSigner.from_private_key_base64(
        signer_id="naumi-release-channel",
        key_id="channel-2026-q3",
        key_generation=2,
        private_key_base64=_b64(seed),
    )


def _build_signer():
    return ReleaseBuildSigner.from_private_key_base64(
        builder_id="naumi-github-release",
        key_id="release-2026-q3",
        key_generation=3,
        private_key_base64=_b64(b"b" * 32),
    )


def _policies(channel_signer, build_signer, *, channel_state="active", build_state="active"):
    channel_key = ReleaseTrustedChannelKey(
        identity=channel_signer.identity,
        state=channel_state,
        channels=("stable",),
        valid_from=(T0 - timedelta(days=7)).isoformat(),
        valid_until=(T0 + timedelta(days=30)).isoformat(),
        revoked_at=T0.isoformat() if channel_state == "revoked" else None,
    )
    build_key = ReleaseTrustedBuilderKey(
        identity=build_signer.identity,
        state=build_state,
        valid_from=(T0 - timedelta(days=7)).isoformat(),
        valid_until=(T0 + timedelta(days=30)).isoformat(),
        revoked_at=T0.isoformat() if build_state == "revoked" else None,
    )
    return (
        create_release_channel_trust_policy(
            (channel_key,),
            archive_origins=(
                "https://downloads.naumi.dev",
                "https://mirror.naumi.dev",
            ),
        ),
        create_release_build_trust_policy((build_key,)),
    )


def _entry(
    tmp_path: Path,
    build_signer,
    *,
    version: str,
    release_generation: int,
    target="linux-x64",
):
    root = tmp_path / version / target
    root.mkdir(parents=True)
    archive = root / f"naumi-{version}-{target}.tar.gz"
    archive.write_bytes(f"source-free-{version}-{target}".encode())
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "NaumiAgent",
                "version": version,
                "target": target,
                "source_commit": "a" * 40,
                "source_tree_sha256": "b" * 64,
                "files": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    attestation = create_release_build_attestation(
        signer=build_signer,
        context=ReleaseBuildContext(
            repository="JesstLe/NaumiAgent",
            workflow_ref=(
                "JesstLe/NaumiAgent/.github/workflows/"
                f"release-binaries.yml@refs/tags/v{version}"
            ),
            run_id=version.replace(".", ""),
            run_attempt=1,
            built_at=(T0 - timedelta(days=1)).isoformat(),
        ),
        manifest_path=manifest,
        archive_path=archive,
    )
    return ReleaseChannelEntry(
        target=target,
        version=version,
        release_generation=release_generation,
        archive_path=f"releases/v{version}/{archive.name}",
        archive_name=archive.name,
        archive_sha256=attestation.payload.archive_sha256,
        archive_size_bytes=archive.stat().st_size,
        manifest_sha256=attestation.payload.manifest_sha256,
        build_attestation=attestation,
    )


def test_channel_trust_policy_loader_is_bounded_and_rejects_links(
    tmp_path: Path,
) -> None:
    channel_signer = _channel_signer()
    build_signer = _build_signer()
    policy, _ = _policies(channel_signer, build_signer)
    path = tmp_path / "trusted-channels.json"
    path.write_text(policy.model_dump_json(), encoding="utf-8")

    assert load_release_channel_trust_policy(path) == policy

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ReleaseChannelCatalogError) as invalid:
        load_release_channel_trust_policy(path)
    assert invalid.value.code == "release_channel_trust_policy_invalid"

    path.write_bytes(b"")
    with pytest.raises(ReleaseChannelCatalogError) as empty:
        load_release_channel_trust_policy(path)
    assert empty.value.code == "release_channel_trust_policy_size_invalid"

    path.write_bytes(b"x" * (512 * 1024 + 1))
    with pytest.raises(ReleaseChannelCatalogError) as oversized:
        load_release_channel_trust_policy(path)
    assert oversized.value.code == "release_channel_trust_policy_size_invalid"

    path.unlink()
    with pytest.raises(ReleaseChannelCatalogError) as missing:
        load_release_channel_trust_policy(path)
    assert missing.value.code == "release_channel_trust_policy_unreadable"

    if os.name != "nt":
        target = tmp_path / "trusted-channels-target.json"
        target.write_text(policy.model_dump_json(), encoding="utf-8")
        link = tmp_path / "trusted-channels-link.json"
        link.symlink_to(target)
        with pytest.raises(ReleaseChannelCatalogError) as linked:
            load_release_channel_trust_policy(link)
        assert linked.value.code == "release_channel_trust_policy_file_invalid"


@pytest.mark.asyncio
async def test_channel_catalog_is_chained_concurrent_and_dynamically_trusted(
    tmp_path: Path,
) -> None:
    channel_signer = _channel_signer()
    build_signer = _build_signer()
    policies = list(_policies(channel_signer, build_signer))
    first_entry = _entry(
        tmp_path,
        build_signer,
        version="1.2.3",
        release_generation=123,
    )
    first = channel_signer.issue(
        channel="stable",
        entries=(first_entry,),
        previous=None,
        generated_at=T0.isoformat(),
        valid_from=(T0 + timedelta(seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=7)).isoformat(),
    )
    db_path = tmp_path / ".naumi" / "release-channel.db"

    def _store():
        return ReleaseChannelCatalogStore(
            db_path,
            channel_trust_policy_provider=lambda: policies[0],
            build_trust_policy_provider=lambda: policies[1],
            clock=lambda: T0 + timedelta(seconds=2),
        )

    stores = tuple(_store() for _ in range(4))
    views = await asyncio.gather(*(store.record(first) for store in stores))
    assert all(view == views[0] for view in views)
    assert views[0].catalog_resolution_authority
    assert views[0].download_input_authority
    resolution = await stores[0].resolve(channel="stable", target="linux-x64")
    assert resolution.entry == first_entry
    assert resolution.archive_urls == (
        f"https://downloads.naumi.dev/{first_entry.archive_path}",
        f"https://mirror.naumi.dev/{first_entry.archive_path}",
    )
    assert not resolution.download_executed
    assert not resolution.installation_authority
    assert not resolution.deployment_authority
    with pytest.raises(ReleaseChannelCatalogError) as missing_target:
        await stores[0].resolve(channel="stable", target="windows-x64")
    assert missing_target.value.code == "release_channel_target_missing"

    second_entry = _entry(
        tmp_path,
        build_signer,
        version="1.2.4",
        release_generation=124,
    )
    second = channel_signer.issue(
        channel="stable",
        entries=(second_entry,),
        previous=first,
        generated_at=(T0 + timedelta(hours=1)).isoformat(),
        valid_from=(T0 + timedelta(hours=1, seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=7)).isoformat(),
    )
    for store in stores:
        store.clock = lambda: T0 + timedelta(hours=1, seconds=2)
    await stores[0].record(second)
    historical = await stores[1].inspect(catalog_id=first.catalog_id)
    assert not historical.latest_for_channel
    assert not historical.catalog_resolution_authority

    policies[0] = _policies(
        channel_signer,
        build_signer,
        channel_state="revoked",
    )[0]
    revoked = await stores[0].inspect(catalog_id=second.catalog_id)
    assert not revoked.channel_trust_current
    assert not revoked.download_input_authority
    policies[:] = list(_policies(channel_signer, build_signer, build_state="revoked"))
    builder_revoked = await stores[0].inspect(catalog_id=second.catalog_id)
    assert not builder_revoked.builder_trust_current
    assert not builder_revoked.catalog_resolution_authority


@pytest.mark.asyncio
async def test_channel_catalog_rejects_signature_chain_path_and_durable_tamper(
    tmp_path: Path,
) -> None:
    channel_signer = _channel_signer()
    build_signer = _build_signer()
    channel_policy, build_policy = _policies(channel_signer, build_signer)
    entry = _entry(
        tmp_path,
        build_signer,
        version="1.2.3",
        release_generation=123,
    )
    first = channel_signer.issue(
        channel="stable",
        entries=(entry,),
        previous=None,
        generated_at=T0.isoformat(),
        valid_from=(T0 + timedelta(seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=7)).isoformat(),
    )

    with pytest.raises(ValueError, match="安全相对路径"):
        ReleaseChannelEntry(
            **{
                **entry.model_dump(mode="json"),
                "archive_path": f"../{entry.archive_name}",
            }
        )

    wrong_signature = Ed25519PrivateKey.from_private_bytes(b"w" * 32).sign(
        first.payload.canonical_bytes()
    )
    forged_core = first.model_dump(
        mode="json",
        exclude={"catalog_id", "catalog_sha256"},
    )
    forged_core["signature_base64"] = _b64(wrong_signature)
    forged_core["signature_sha256"] = hashlib.sha256(wrong_signature).hexdigest()
    forged_digest = hashlib.sha256(
        json.dumps(
            forged_core,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    forged = ReleaseChannelCatalog.model_validate(
        {
            **forged_core,
            "catalog_id": f"relchannelcatalog_{forged_digest[:24]}",
            "catalog_sha256": forged_digest,
        }
    )
    store = ReleaseChannelCatalogStore(
        tmp_path / ".naumi" / "release-channel.db",
        channel_trust_policy_provider=lambda: channel_policy,
        build_trust_policy_provider=lambda: build_policy,
        clock=lambda: T0 + timedelta(seconds=2),
    )
    with pytest.raises(ReleaseChannelCatalogError) as invalid_signature:
        await store.record(forged)
    assert invalid_signature.value.code == "release_channel_signature_invalid"

    await store.record(first)
    second = channel_signer.issue(
        channel="stable",
        entries=(entry,),
        previous=first,
        generated_at=(T0 + timedelta(hours=1)).isoformat(),
        valid_from=(T0 + timedelta(hours=1, seconds=1)).isoformat(),
        expires_at=(T0 + timedelta(days=7)).isoformat(),
    )
    store.clock = lambda: T0 + timedelta(hours=1, seconds=2)
    with pytest.raises(ReleaseChannelCatalogError) as rollback_generation:
        await store.record(second)
    assert rollback_generation.value.code == "release_channel_generation_rollback"
    disconnected = ReleaseChannelCatalogStore(
        tmp_path / ".naumi" / "disconnected.db",
        channel_trust_policy_provider=lambda: channel_policy,
        build_trust_policy_provider=lambda: build_policy,
        clock=lambda: T0 + timedelta(hours=1, seconds=2),
    )
    with pytest.raises(ReleaseChannelCatalogError) as gap:
        await disconnected.record(second)
    assert gap.value.code == "release_channel_catalog_chain_conflict"

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE release_channel_catalogs SET catalog_json = replace("
            "catalog_json, ?, ?) WHERE catalog_id = ?",
            (first.catalog_sha256, "0" * 64, first.catalog_id),
        )
        await db.commit()
    with pytest.raises(ReleaseChannelCatalogError) as corrupt:
        await store.get(first.catalog_id)
    assert corrupt.value.code == "release_channel_catalog_source_invalid"
