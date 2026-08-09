from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from naumi_agent.release.artifact import assemble_release_artifact
from naumi_agent.release.build_attestations import (
    RELEASE_BUILD_ATTESTATION_POLICY,
    ReleaseBuildAttestation,
    ReleaseBuildAttestationError,
    ReleaseBuildContext,
    ReleaseBuildSigner,
    ReleaseTrustedBuilderKey,
    create_release_build_trust_policy,
    load_release_build_attestation,
    load_release_build_trust_policy,
    verify_release_build_attestation,
)

SOURCE_COMMIT = "a" * 40
SOURCE_TREE_SHA256 = "b" * 64
BUILT_AT = "2026-08-09T03:04:05+00:00"
ROOT = Path(__file__).resolve().parents[2]


def _binary(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def _signer(seed: bytes = bytes(range(32))) -> tuple[ReleaseBuildSigner, str]:
    private_base64 = base64.b64encode(seed).decode("ascii")
    return (
        ReleaseBuildSigner.from_private_key_base64(
            builder_id="naumi-github-release",
            key_id="release-2026-q3",
            key_generation=3,
            private_key_base64=private_base64,
        ),
        private_base64,
    )


def _context() -> ReleaseBuildContext:
    return ReleaseBuildContext(
        repository="JesstLe/NaumiAgent",
        workflow_ref="JesstLe/NaumiAgent/.github/workflows/release-binaries.yml@refs/tags/v1.2.3",
        run_id="123456789",
        run_attempt=2,
        built_at=BUILT_AT,
    )


def _trusted_key(signer: ReleaseBuildSigner, *, state: str = "active"):
    return ReleaseTrustedBuilderKey(
        identity=signer.identity,
        state=state,
        valid_from="2026-08-01T00:00:00+00:00",
        valid_until="2026-09-01T00:00:00+00:00",
        revoked_at="2026-08-10T00:00:00+00:00" if state == "revoked" else None,
    )


def _artifact(root: Path, signer: ReleaseBuildSigner):
    backend = root / "backend"
    _binary(backend / "naumi-runtime", b"frozen-runtime")
    launcher = _binary(root / "launcher" / "naumi", b"stable-launcher")
    ui = _binary(root / "naumi-ui", b"terminal-ui")
    config = root / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")
    return assemble_release_artifact(
        backend_dir=backend,
        launcher_dir=launcher.parent,
        ui_binary=ui,
        config_example=config,
        output_dir=root / "release",
        version="1.2.3",
        target="linux-x64",
        source_commit=SOURCE_COMMIT,
        source_tree_sha256=SOURCE_TREE_SHA256,
        archive_format="tar.gz",
        build_signer=signer,
        build_context=_context(),
    )


def test_real_detached_attestation_binds_archive_manifest_source_and_ci_identity(
    tmp_path: Path,
) -> None:
    signer, private_base64 = _signer()
    artifact = _artifact(tmp_path, signer)
    assert artifact.attestation is not None
    attestation = load_release_build_attestation(artifact.attestation)
    trust_policy = create_release_build_trust_policy((_trusted_key(signer),))
    policy_path = tmp_path / "trusted-builders.json"
    policy_path.write_text(trust_policy.model_dump_json(), encoding="utf-8")
    assert load_release_build_trust_policy(policy_path) == trust_policy

    trusted = verify_release_build_attestation(
        attestation,
        trust_policy=trust_policy,
        manifest_path=artifact.manifest,
        archive_path=artifact.archive,
    )

    assert trusted.identity == signer.identity
    assert attestation.payload.source_commit == SOURCE_COMMIT
    assert attestation.payload.source_tree_sha256 == SOURCE_TREE_SHA256
    assert attestation.payload.build.run_id == "123456789"
    assert attestation.payload.build.run_attempt == 2
    assert attestation.payload.archive_sha256 == hashlib.sha256(
        artifact.archive.read_bytes()
    ).hexdigest()
    assert attestation.payload.manifest_sha256 == hashlib.sha256(
        artifact.manifest.read_bytes()
    ).hexdigest()
    assert private_base64 not in artifact.attestation.read_text(encoding="utf-8")
    assert private_base64.encode() not in artifact.archive.read_bytes()


def test_attestation_rejects_manifest_archive_tamper_and_untrusted_key(
    tmp_path: Path,
) -> None:
    signer, _private = _signer()
    artifact = _artifact(tmp_path, signer)
    assert artifact.attestation is not None
    attestation = load_release_build_attestation(artifact.attestation)
    trusted_policy = create_release_build_trust_policy((_trusted_key(signer),))
    other_signer, _ = _signer(b"z" * 32)
    untrusted_policy = create_release_build_trust_policy((_trusted_key(other_signer),))
    original_manifest = artifact.manifest.read_bytes()

    with pytest.raises(ReleaseBuildAttestationError) as untrusted:
        verify_release_build_attestation(
            attestation,
            trust_policy=untrusted_policy,
            manifest_path=artifact.manifest,
        )
    assert untrusted.value.code == "release_builder_untrusted"

    artifact.manifest.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ReleaseBuildAttestationError) as manifest_tamper:
        verify_release_build_attestation(
            attestation,
            trust_policy=trusted_policy,
            manifest_path=artifact.manifest,
        )
    assert manifest_tamper.value.code == "release_build_manifest_mismatch"

    artifact.manifest.write_bytes(original_manifest)
    archive = artifact.archive
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(ReleaseBuildAttestationError) as archive_tamper:
        verify_release_build_attestation(
            attestation,
            trust_policy=trusted_policy,
            manifest_path=artifact.manifest,
            archive_path=archive,
        )
    assert archive_tamper.value.code == "release_build_archive_mismatch"


def test_revoked_and_out_of_window_builder_keys_fail_closed(tmp_path: Path) -> None:
    signer, _private = _signer()
    artifact = _artifact(tmp_path, signer)
    assert artifact.attestation is not None
    attestation = load_release_build_attestation(artifact.attestation)
    revoked_policy = create_release_build_trust_policy(
        (_trusted_key(signer, state="revoked"),)
    )
    future_key = ReleaseTrustedBuilderKey(
        identity=signer.identity,
        state="active",
        valid_from="2026-08-10T00:00:00+00:00",
        valid_until=None,
    )

    with pytest.raises(ReleaseBuildAttestationError) as revoked:
        verify_release_build_attestation(
            attestation,
            trust_policy=revoked_policy,
            manifest_path=artifact.manifest,
        )
    assert revoked.value.code == "release_builder_revoked"

    with pytest.raises(ReleaseBuildAttestationError) as not_yet_valid:
        verify_release_build_attestation(
            attestation,
            trust_policy=create_release_build_trust_policy((future_key,)),
            manifest_path=artifact.manifest,
        )
    assert not_yet_valid.value.code == "release_builder_outside_validity"


def test_structurally_valid_attestation_with_wrong_signature_is_rejected(
    tmp_path: Path,
) -> None:
    signer, _private = _signer()
    artifact = _artifact(tmp_path, signer)
    assert artifact.attestation is not None
    original = load_release_build_attestation(artifact.attestation)
    wrong_signature = Ed25519PrivateKey.generate().sign(original.payload.canonical_bytes())
    core = original.model_dump(
        mode="json", exclude={"attestation_id", "attestation_sha256"}
    )
    core["signature_base64"] = base64.b64encode(wrong_signature).decode("ascii")
    core["signature_sha256"] = hashlib.sha256(wrong_signature).hexdigest()
    digest = hashlib.sha256(
        json.dumps(
            core,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    forged = ReleaseBuildAttestation.model_validate(
        {
            **core,
            "attestation_id": f"relbuildatt_{digest[:24]}",
            "attestation_sha256": digest,
            "policy_version": RELEASE_BUILD_ATTESTATION_POLICY,
        }
    )

    with pytest.raises(ReleaseBuildAttestationError) as invalid:
        verify_release_build_attestation(
            forged,
            trust_policy=create_release_build_trust_policy((_trusted_key(signer),)),
            manifest_path=artifact.manifest,
        )
    assert invalid.value.code == "release_build_signature_invalid"


def test_release_cli_requires_key_and_emits_detached_attestation(tmp_path: Path) -> None:
    signer, private_base64 = _signer()
    backend = tmp_path / "backend"
    _binary(backend / "naumi-runtime", b"frozen-runtime")
    launcher = _binary(tmp_path / "launcher" / "naumi", b"stable-launcher")
    ui = _binary(tmp_path / "naumi-ui", b"terminal-ui")
    config = tmp_path / "config.yaml.example"
    config.write_text("models: {}\n", encoding="utf-8")
    command = [
        sys.executable,
        str(ROOT / "scripts" / "release" / "assemble_artifact.py"),
        "--backend-dir",
        str(backend),
        "--launcher-dir",
        str(launcher.parent),
        "--ui-binary",
        str(ui),
        "--config-example",
        str(config),
        "--output-dir",
        str(tmp_path / "release-cli"),
        "--version",
        "1.2.3",
        "--target",
        "linux-x64",
        "--source-commit",
        SOURCE_COMMIT,
        "--source-tree-sha256",
        SOURCE_TREE_SHA256,
        "--archive-format",
        "tar.gz",
    ]
    env = {
        **os.environ,
        "NAUMI_RELEASE_BUILDER_PRIVATE_KEY_BASE64": private_base64,
        "NAUMI_RELEASE_BUILDER_ID": signer.identity.builder_id,
        "NAUMI_RELEASE_BUILDER_KEY_ID": signer.identity.key_id,
        "NAUMI_RELEASE_BUILDER_KEY_GENERATION": str(signer.identity.key_generation),
        "GITHUB_REPOSITORY": "JesstLe/NaumiAgent",
        "GITHUB_WORKFLOW_REF": (
            "JesstLe/NaumiAgent/.github/workflows/"
            "release-binaries.yml@refs/tags/v1.2.3"
        ),
        "GITHUB_RUN_ID": "123456789",
        "GITHUB_RUN_ATTEMPT": "2",
        "NAUMI_RELEASE_BUILT_AT": BUILT_AT,
    }

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr
    emitted = tuple(Path(line) for line in completed.stdout.splitlines())
    assert len(emitted) == 3
    assert emitted[0].is_file() and emitted[1].is_file() and emitted[2].is_file()
    attestation = load_release_build_attestation(emitted[2])
    verify_release_build_attestation(
        attestation,
        trust_policy=create_release_build_trust_policy((_trusted_key(signer),)),
        manifest_path=tmp_path
        / "release-cli"
        / "naumi-1.2.3-linux-x64"
        / "manifest.json",
        archive_path=emitted[0],
    )

    unsigned_env = dict(env)
    unsigned_env.pop("NAUMI_RELEASE_BUILDER_PRIVATE_KEY_BASE64")
    unsigned_command = [
        *command[: command.index("--output-dir") + 1],
        str(tmp_path / "unsigned"),
        *command[command.index("--version") :],
    ]
    refused = subprocess.run(
        unsigned_command,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=unsigned_env,
        cwd=ROOT,
    )
    assert refused.returncode != 0
    assert "正式发行必须生成可信构建证明" in refused.stderr
    assert not (tmp_path / "unsigned").exists()
