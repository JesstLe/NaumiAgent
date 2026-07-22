"""Read-only baseline observation derived from approved Claude source history."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.claude_source.governance import (
    LegacyMappingEvidence,
    SourceAuditResult,
    SourceGitIdentity,
    SourceLicenseEvidence,
    load_source_identity,
    verify_source_identity,
)
from naumi_agent.claude_source.refresh import (
    SourceRefreshStore,
    manifest_sha256,
    resolve_claude_source_db_path,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")


class SourceBaselineObservation(BaseModel):
    """Deterministic read model for one approved source baseline and live audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    observation_id: str
    baseline_entry_id: str
    baseline_revision: int = Field(ge=1)
    source_name: str = Field(min_length=1, max_length=128)
    source_identity_schema_version: Literal[2] = 2
    manifest_sha256: str
    identity_sha256: str
    decision_kind: Literal["baseline_import", "refresh_approved"]
    approved_at: str
    reviewed_by: str
    approved_git: SourceGitIdentity
    license: SourceLicenseEvidence
    mapping: LegacyMappingEvidence
    mapping_format: Literal["legacy_unversioned_v1"] = "legacy_unversioned_v1"
    mapping_schema_version: None = None
    audit: SourceAuditResult
    status: Literal["current", "change_detected", "invalid"]

    @field_validator(
        "observation_id",
        "baseline_entry_id",
        "manifest_sha256",
        "identity_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("baseline digest 必须是完整小写 SHA-256。")
        return value

    @field_validator("approved_at")
    @classmethod
    def _approved_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("approved_at 必须包含时区。")
        return value

    @field_validator("reviewed_by")
    @classmethod
    def _reviewer(cls, value: str) -> str:
        if not _ACTOR_RE.fullmatch(value):
            raise ValueError("reviewed_by 格式无效。")
        return value

    @model_validator(mode="after")
    def _integrity(self) -> SourceBaselineObservation:
        expected_status = {
            "valid": "current",
            "stale": "change_detected",
            "invalid": "invalid",
        }[self.audit.status]
        if self.status != expected_status:
            raise ValueError("baseline status 与 live audit 不一致。")
        if self.observation_id != source_baseline_observation_sha256(self):
            raise ValueError("baseline observation digest 不一致。")
        return self


def observe_source_baseline(
    store: SourceRefreshStore,
    *,
    manifest_path: str | Path,
    source_root: str | Path,
    project_root: str | Path,
) -> SourceBaselineObservation:
    """Build a read-only observation from the latest approved history entry."""
    current_manifest = load_source_identity(manifest_path)
    approved = store.latest(current_manifest.source_name)
    if approved is None:
        raise ValueError("尚无已批准 source history，不能建立监控基线。")
    if approved.manifest_sha256 != manifest_sha256(current_manifest):
        raise ValueError("当前 manifest 与已批准 source history 不一致。")
    audit = verify_source_identity(approved.manifest, source_root, project_root)
    values = {
        "schema_version": 1,
        "baseline_entry_id": approved.entry_id,
        "baseline_revision": approved.revision,
        "source_name": approved.source_name,
        "source_identity_schema_version": approved.manifest.schema_version,
        "manifest_sha256": approved.manifest_sha256,
        "identity_sha256": approved.identity_sha256,
        "decision_kind": approved.decision_kind,
        "approved_at": approved.reviewed_at,
        "reviewed_by": approved.reviewed_by,
        "approved_git": approved.manifest.git,
        "license": approved.manifest.license,
        "mapping": approved.manifest.legacy_mapping,
        "mapping_format": "legacy_unversioned_v1",
        "mapping_schema_version": None,
        "audit": audit,
        "status": {
            "valid": "current",
            "stale": "change_detected",
            "invalid": "invalid",
        }[audit.status],
    }
    return SourceBaselineObservation(
        observation_id=_sha256_json(values),
        **values,
    )


def source_baseline_observation_sha256(
    observation: SourceBaselineObservation,
) -> str:
    payload = observation.model_dump(mode="json")
    payload.pop("observation_id", None)
    return _sha256_json(payload)


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="读取已批准 Claude Code source baseline 并执行当前状态审计"
    )
    parser.add_argument("--manifest", default="frontend/terminal-ui/cc-source-map.v2.json")
    parser.add_argument("--source", default="../claude-code")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--store", default=str(resolve_claude_source_db_path()))
    args = parser.parse_args()
    try:
        observation = observe_source_baseline(
            SourceRefreshStore(args.store),
            manifest_path=args.manifest,
            source_root=args.source,
            project_root=args.project_root,
        )
    except (OSError, sqlite3.Error, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(observation.model_dump_json(indent=2))
    return 0 if observation.status == "current" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
