"""Trusted builder identities and detached Ed25519 release attestations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

RELEASE_BUILD_ATTESTATION_POLICY = "naumi-release-build-attestation-v1"
RELEASE_BUILD_ATTESTATION_DOMAIN = "naumi.release.build-attestation.v1"
RELEASE_BUILD_TRUST_POLICY = "naumi-release-build-trust-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_IDENTIFIER_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SOURCE_COMMIT_RE = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_MAX_ATTESTATION_BYTES = 256 * 1024


class ReleaseBuildAttestationError(ValueError):
    """Raised when builder trust or release attestation evidence is invalid."""

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


class ReleaseBuilderIdentity(_StrictModel):
    schema_version: Literal[1] = 1
    builder_id: str = Field(pattern=_IDENTIFIER_RE)
    key_id: str = Field(pattern=_IDENTIFIER_RE)
    key_generation: int = Field(ge=1, le=1_000_000)
    public_key_base64: str = Field(min_length=44, max_length=44)
    public_key_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def _identity_is_exact(self) -> Self:
        public_key = _decode_base64(
            self.public_key_base64,
            expected_bytes=32,
            label="builder public key",
        )
        if not hmac.compare_digest(
            base64.b64encode(public_key).decode("ascii"),
            self.public_key_base64,
        ):
            raise ValueError("Builder public key 必须使用 canonical Base64。")
        if not hmac.compare_digest(
            hashlib.sha256(public_key).hexdigest(),
            self.public_key_sha256,
        ):
            raise ValueError("Builder public key 摘要不一致。")
        return self


class ReleaseTrustedBuilderKey(_StrictModel):
    schema_version: Literal[1] = 1
    identity: ReleaseBuilderIdentity
    state: Literal["active", "revoked"]
    valid_from: str = Field(min_length=1, max_length=100)
    valid_until: str | None = Field(default=None, min_length=1, max_length=100)
    revoked_at: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _trust_window_is_exact(self) -> Self:
        valid_from = _aware(self.valid_from)
        valid_until = _aware(self.valid_until) if self.valid_until else None
        revoked_at = _aware(self.revoked_at) if self.revoked_at else None
        if valid_until is not None and valid_until <= valid_from:
            raise ValueError("Builder key 有效期终点必须晚于起点。")
        if self.state == "active" and revoked_at is not None:
            raise ValueError("Active builder key 不得声明 revoked_at。")
        if self.state == "revoked" and revoked_at is None:
            raise ValueError("Revoked builder key 必须声明 revoked_at。")
        return self


class ReleaseBuildTrustPolicyDocument(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-build-trust-v1"] = RELEASE_BUILD_TRUST_POLICY
    policy_id: str = Field(pattern=r"^relbuildtrust_[0-9a-f]{24}$")
    policy_sha256: str = Field(pattern=_SHA256_RE)
    keys: tuple[ReleaseTrustedBuilderKey, ...] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def _policy_is_exact(self) -> Self:
        identities = [
            (
                key.identity.builder_id,
                key.identity.key_id,
                key.identity.key_generation,
            )
            for key in self.keys
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("Build Trust Policy 含重复 builder key identity。")
        core = self.model_dump(mode="json", exclude={"policy_id", "policy_sha256"})
        digest = _digest(core)
        if not hmac.compare_digest(self.policy_sha256, digest):
            raise ValueError("Build Trust Policy 摘要不一致。")
        if self.policy_id != f"relbuildtrust_{digest[:24]}":
            raise ValueError("Build Trust Policy identity 不一致。")
        return self

    def find(self, identity: ReleaseBuilderIdentity) -> ReleaseTrustedBuilderKey | None:
        for key in self.keys:
            if key.identity == identity:
                return key
        return None


class ReleaseBuildContext(_StrictModel):
    schema_version: Literal[1] = 1
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    workflow_ref: str = Field(min_length=1, max_length=1_024)
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
    run_attempt: int = Field(ge=1, le=1_000_000)
    built_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _context_is_exact(self) -> Self:
        _aware(self.built_at)
        if "\x00" in self.workflow_ref or "\n" in self.workflow_ref or "\r" in self.workflow_ref:
            raise ValueError("Build workflow_ref 含非法控制字符。")
        return self


class ReleaseBuildAttestationPayload(_StrictModel):
    schema_version: Literal[1] = 1
    domain: Literal["naumi.release.build-attestation.v1"] = (
        RELEASE_BUILD_ATTESTATION_DOMAIN
    )
    product: Literal["NaumiAgent"] = "NaumiAgent"
    version: str = Field(pattern=_IDENTIFIER_RE)
    target: str = Field(pattern=_IDENTIFIER_RE)
    source_commit: str = Field(pattern=_SOURCE_COMMIT_RE)
    source_tree_sha256: str = Field(pattern=_SHA256_RE)
    manifest_sha256: str = Field(pattern=_SHA256_RE)
    archive_name: str = Field(pattern=r"^naumi-[A-Za-z0-9._-]+\.(?:tar\.gz|zip)$")
    archive_sha256: str = Field(pattern=_SHA256_RE)
    builder: ReleaseBuilderIdentity
    build: ReleaseBuildContext

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


class ReleaseBuildAttestation(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["naumi-release-build-attestation-v1"] = (
        RELEASE_BUILD_ATTESTATION_POLICY
    )
    attestation_id: str = Field(pattern=r"^relbuildatt_[0-9a-f]{24}$")
    attestation_sha256: str = Field(pattern=_SHA256_RE)
    payload: ReleaseBuildAttestationPayload
    signature_algorithm: Literal["ed25519"] = "ed25519"
    signature_base64: str = Field(min_length=88, max_length=88)
    signature_sha256: str = Field(pattern=_SHA256_RE)
    private_key_persisted: Literal[False] = False

    @model_validator(mode="after")
    def _attestation_is_exact(self) -> Self:
        signature = _decode_base64(
            self.signature_base64,
            expected_bytes=64,
            label="builder signature",
        )
        if not hmac.compare_digest(
            base64.b64encode(signature).decode("ascii"),
            self.signature_base64,
        ):
            raise ValueError("Builder signature 必须使用 canonical Base64。")
        if not hmac.compare_digest(
            hashlib.sha256(signature).hexdigest(),
            self.signature_sha256,
        ):
            raise ValueError("Builder signature 摘要不一致。")
        core = self.model_dump(mode="json", exclude={"attestation_id", "attestation_sha256"})
        digest = _digest(core)
        if not hmac.compare_digest(self.attestation_sha256, digest):
            raise ValueError("Build Attestation 摘要不一致。")
        if self.attestation_id != f"relbuildatt_{digest[:24]}":
            raise ValueError("Build Attestation identity 不一致。")
        return self


@dataclass(frozen=True)
class ReleaseBuildSigner:
    """In-memory signer; private key bytes are never serialized by this module."""

    identity: ReleaseBuilderIdentity
    _private_key: Ed25519PrivateKey = field(repr=False)

    @classmethod
    def from_private_key_base64(
        cls,
        *,
        builder_id: str,
        key_id: str,
        key_generation: int,
        private_key_base64: str,
    ) -> ReleaseBuildSigner:
        private_bytes = _decode_base64(
            private_key_base64,
            expected_bytes=32,
            label="builder private key",
        )
        if not hmac.compare_digest(
            base64.b64encode(private_bytes).decode("ascii"),
            private_key_base64,
        ):
            raise ReleaseBuildAttestationError(
                "release_builder_private_key_noncanonical",
                "Builder private key 必须使用 canonical Base64。",
            )
        try:
            private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        except ValueError as exc:
            raise ReleaseBuildAttestationError(
                "release_builder_private_key_invalid",
                "Builder private key 不是有效 Ed25519 seed。",
            ) from exc
        public_bytes = private_key.public_key().public_bytes_raw()
        identity = ReleaseBuilderIdentity(
            builder_id=builder_id,
            key_id=key_id,
            key_generation=key_generation,
            public_key_base64=base64.b64encode(public_bytes).decode("ascii"),
            public_key_sha256=hashlib.sha256(public_bytes).hexdigest(),
        )
        return cls(identity=identity, _private_key=private_key)

    def attest(self, payload: ReleaseBuildAttestationPayload) -> ReleaseBuildAttestation:
        if payload.builder != self.identity:
            raise ReleaseBuildAttestationError(
                "release_builder_identity_mismatch",
                "Build payload 未绑定当前 signer identity。",
            )
        signature = self._private_key.sign(payload.canonical_bytes())
        signature_base64 = base64.b64encode(signature).decode("ascii")
        core = {
            "schema_version": 1,
            "policy_version": RELEASE_BUILD_ATTESTATION_POLICY,
            "payload": payload.model_dump(mode="json"),
            "signature_algorithm": "ed25519",
            "signature_base64": signature_base64,
            "signature_sha256": hashlib.sha256(signature).hexdigest(),
            "private_key_persisted": False,
        }
        digest = _digest(core)
        return ReleaseBuildAttestation.model_validate(
            {
                **core,
                "attestation_id": f"relbuildatt_{digest[:24]}",
                "attestation_sha256": digest,
            }
        )


def create_release_build_trust_policy(
    keys: tuple[ReleaseTrustedBuilderKey, ...],
) -> ReleaseBuildTrustPolicyDocument:
    ordered = tuple(
        sorted(
            keys,
            key=lambda item: (
                item.identity.builder_id,
                item.identity.key_generation,
                item.identity.key_id,
            ),
        )
    )
    core = {
        "schema_version": 1,
        "policy_version": RELEASE_BUILD_TRUST_POLICY,
        "keys": [item.model_dump(mode="json") for item in ordered],
    }
    digest = _digest(core)
    return ReleaseBuildTrustPolicyDocument.model_validate(
        {
            **core,
            "policy_id": f"relbuildtrust_{digest[:24]}",
            "policy_sha256": digest,
        }
    )


def create_release_build_attestation(
    *,
    signer: ReleaseBuildSigner,
    context: ReleaseBuildContext,
    manifest_path: Path,
    archive_path: Path,
) -> ReleaseBuildAttestation:
    manifest_bytes = _read_bounded(manifest_path, 8 * 1024 * 1024, "release manifest")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildAttestationError(
            "release_build_manifest_invalid",
            "无法为无效 release manifest 生成构建证明。",
        ) from exc
    required = {
        "schema_version",
        "product",
        "version",
        "target",
        "source_commit",
        "source_tree_sha256",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ReleaseBuildAttestationError(
            "release_build_manifest_invalid",
            "Release manifest schema 不支持构建证明。",
        )
    payload = ReleaseBuildAttestationPayload(
        version=manifest["version"],
        target=manifest["target"],
        source_commit=manifest["source_commit"],
        source_tree_sha256=manifest["source_tree_sha256"],
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        archive_name=archive_path.name,
        archive_sha256=_sha256_file(archive_path),
        builder=signer.identity,
        build=context,
    )
    return signer.attest(payload)


def verify_release_build_attestation(
    attestation: ReleaseBuildAttestation,
    *,
    trust_policy: ReleaseBuildTrustPolicyDocument,
    manifest_path: Path,
    archive_path: Path | None = None,
) -> ReleaseTrustedBuilderKey:
    payload = attestation.payload
    trusted_key = trust_policy.find(payload.builder)
    if trusted_key is None:
        raise ReleaseBuildAttestationError(
            "release_builder_untrusted",
            "构建证明的 builder key 不在当前信任策略中。",
        )
    if trusted_key.state != "active":
        raise ReleaseBuildAttestationError(
            "release_builder_revoked",
            "构建证明使用的 builder key 已撤销。",
        )
    built_at = _aware(payload.build.built_at)
    valid_from = _aware(trusted_key.valid_from)
    valid_until = _aware(trusted_key.valid_until) if trusted_key.valid_until else None
    if built_at < valid_from or (valid_until is not None and built_at >= valid_until):
        raise ReleaseBuildAttestationError(
            "release_builder_outside_validity",
            "构建时间不在 trusted builder key 有效期内。",
        )
    signature = _decode_base64(
        attestation.signature_base64,
        expected_bytes=64,
        label="builder signature",
    )
    public_key = _decode_base64(
        trusted_key.identity.public_key_base64,
        expected_bytes=32,
        label="builder public key",
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            payload.canonical_bytes(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ReleaseBuildAttestationError(
            "release_build_signature_invalid",
            "构建证明的 Ed25519 signature 无效。",
        ) from exc
    manifest_bytes = _read_bounded(manifest_path, 8 * 1024 * 1024, "release manifest")
    if not hmac.compare_digest(
        hashlib.sha256(manifest_bytes).hexdigest(),
        payload.manifest_sha256,
    ):
        raise ReleaseBuildAttestationError(
            "release_build_manifest_mismatch",
            "构建证明未绑定当前 release manifest。",
        )
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildAttestationError(
            "release_build_manifest_invalid",
            "Release manifest 不是有效 UTF-8 JSON。",
        ) from exc
    expected_projection = (
        "NaumiAgent",
        payload.version,
        payload.target,
        payload.source_commit,
        payload.source_tree_sha256,
    )
    actual_projection = (
        manifest.get("product") if isinstance(manifest, dict) else None,
        manifest.get("version") if isinstance(manifest, dict) else None,
        manifest.get("target") if isinstance(manifest, dict) else None,
        manifest.get("source_commit") if isinstance(manifest, dict) else None,
        manifest.get("source_tree_sha256") if isinstance(manifest, dict) else None,
    )
    if actual_projection != expected_projection:
        raise ReleaseBuildAttestationError(
            "release_build_subject_mismatch",
            "构建证明与 manifest subject projection 不一致。",
        )
    if archive_path is not None:
        if not hmac.compare_digest(_sha256_file(archive_path), payload.archive_sha256):
            raise ReleaseBuildAttestationError(
                "release_build_archive_mismatch",
                "构建证明未绑定当前 release archive。",
            )
    return trusted_key


def load_release_build_attestation(path: Path) -> ReleaseBuildAttestation:
    encoded = _read_bounded(path, _MAX_ATTESTATION_BYTES, "build attestation")
    try:
        return ReleaseBuildAttestation.model_validate_json(encoded)
    except ValueError as exc:
        raise ReleaseBuildAttestationError(
            "release_build_attestation_invalid",
            "Build Attestation 不是受支持的 exact artifact。",
        ) from exc


def load_release_build_trust_policy(path: Path) -> ReleaseBuildTrustPolicyDocument:
    encoded = _read_bounded(path, _MAX_ATTESTATION_BYTES, "build trust policy")
    try:
        return ReleaseBuildTrustPolicyDocument.model_validate_json(encoded)
    except ValueError as exc:
        raise ReleaseBuildAttestationError(
            "release_build_trust_policy_invalid",
            "Build Trust Policy 不是受支持的 exact artifact。",
        ) from exc


def write_release_build_attestation(path: Path, attestation: ReleaseBuildAttestation) -> None:
    if path.exists():
        raise ReleaseBuildAttestationError(
            "release_build_attestation_exists",
            f"拒绝覆盖现有构建证明：{path}",
        )
    path.write_text(
        json.dumps(
            attestation.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _decode_base64(value: str, *, expected_bytes: int, label: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} 不是有效 Base64。") from exc
    if len(decoded) != expected_bytes:
        raise ValueError(f"{label} 长度无效。")
    return decoded


def _aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("时间必须是 ISO-8601。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _read_bounded(path: Path, maximum: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
        if size <= 0 or size > maximum:
            raise ReleaseBuildAttestationError(
                "release_build_artifact_size_invalid",
                f"{label} 为空或超过大小上限。",
            )
        return path.read_bytes()
    except OSError as exc:
        raise ReleaseBuildAttestationError(
            "release_build_artifact_unreadable",
            f"无法读取 {label}。",
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseBuildAttestationError(
            "release_build_artifact_unreadable",
            f"无法读取 release artifact：{path}",
        ) from exc
    return digest.hexdigest()
