"""Redacted, deterministic evidence adapters for evolution candidates."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.harness.explain import HarnessExplainer, HarnessFailureClass
from naumi_agent.harness.store import HarnessStoredCheck, HarnessStoredEvidence, HarnessStoredRun

_ID_RE = re.compile(r"^eve_[0-9a-f]{24}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_URI_SCHEMES = frozenset({"artifact", "chat-run", "harness"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvolutionEvidenceRef(_StrictModel):
    uri: str = Field(min_length=1, max_length=2_048)
    sha256: str

    @field_validator("uri")
    @classmethod
    def _safe_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in _SAFE_URI_SCHEMES or parsed.query or parsed.fragment:
            raise ValueError("evidence URI 必须使用允许的无参数内部 scheme。")
        return value

    @field_validator("sha256")
    @classmethod
    def _full_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("evidence digest 必须是完整 SHA-256。")
        return value


class EvolutionEvidence(_StrictModel):
    schema_version: Literal[1] = 1
    evidence_id: str
    source_kind: Literal["harness_failure"] = "harness_failure"
    source_uri: str
    observed_at: str = Field(min_length=1, max_length=128)
    failure_class: HarnessFailureClass
    hard_evidence: Literal[True] = True
    root_fingerprint: str
    refs: tuple[EvolutionEvidenceRef, ...] = Field(min_length=1, max_length=128)

    @field_validator("observed_at")
    @classmethod
    def _timezone_aware_observation(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observed_at 必须是 ISO-8601 时间。") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("observed_at 必须包含时区。")
        return value

    @model_validator(mode="after")
    def _identity_is_valid(self) -> EvolutionEvidence:
        if not _ID_RE.fullmatch(self.evidence_id):
            raise ValueError("evidence_id 格式无效。")
        if not _SHA256_RE.fullmatch(self.root_fingerprint):
            raise ValueError("root_fingerprint 必须是 SHA-256。")
        if self.source_uri != self.refs[0].uri:
            raise ValueError("source_uri 必须指向首个机械证据。")
        if len({ref.uri for ref in self.refs}) != len(self.refs):
            raise ValueError("evidence refs 不得重复。")
        return self


def adapt_harness_failure_evidence(
    run: HarnessStoredRun,
) -> tuple[EvolutionEvidence, ...]:
    """Convert mechanical Harness failures without retaining objective or messages."""
    explanation = HarnessExplainer().explain(run)
    if explanation.running or explanation.verified:
        return ()
    check_by_id = {check.check_key: check for check in run.checks}
    evidence_by_id = {item.id: item for item in run.evidence}
    receipt_ref = _receipt_ref(run)
    observed_at = run.completed_at or run.started_at
    adapted: list[EvolutionEvidence] = []
    for finding in explanation.findings:
        refs = [
            _check_ref(run.id, check_by_id[check_id])
            for check_id in finding.check_ids
            if check_id in check_by_id
        ]
        refs.extend(
            EvolutionEvidenceRef(uri=item.uri, sha256=item.sha256)
            for evidence_id in finding.evidence_ids
            if (item := evidence_by_id.get(evidence_id)) is not None
        )
        if "receipt" in finding.source.split(",") and receipt_ref is not None:
            refs.append(receipt_ref)
        if not refs:
            refs.append(_run_ref(run))
        refs = _dedupe_refs(refs)
        root_material = {
            "failure_class": finding.failure_class.value,
            "checks": sorted(
                _check_root(check_by_id[check_id])
                for check_id in finding.check_ids
                if check_id in check_by_id
            ),
            "evidence_roots": sorted(
                _evidence_root(evidence_by_id[evidence_id])
                for evidence_id in finding.evidence_ids
                if evidence_id in evidence_by_id
            ),
            "receipt": "receipt" in finding.source.split(","),
        }
        root_fingerprint = _digest(root_material)
        observation = _digest(
            {
                "run_id": run.id,
                "observed_at": observed_at,
                "root_fingerprint": root_fingerprint,
                "refs": [ref.model_dump(mode="json") for ref in refs],
            }
        )
        adapted.append(
            EvolutionEvidence(
                evidence_id=f"eve_{observation[:24]}",
                source_uri=refs[0].uri,
                observed_at=observed_at,
                failure_class=finding.failure_class,
                root_fingerprint=root_fingerprint,
                refs=tuple(refs),
            )
        )
    return tuple(adapted)


def _check_ref(run_id: str, check: HarnessStoredCheck) -> EvolutionEvidenceRef:
    uri = f"harness://runs/{quote(run_id, safe='')}/checks/{quote(check.check_key, safe='')}"
    return EvolutionEvidenceRef(
        uri=uri,
        sha256=_digest(
            {
                "argv": check.argv,
                "check_key": check.check_key,
                "duration_ms": check.duration_ms,
                "exit_code": check.exit_code,
                "profile_digest": check.profile_digest,
                "status": check.status,
                "tree_fingerprint": check.tree_fingerprint,
            }
        ),
    )


def _receipt_ref(run: HarnessStoredRun) -> EvolutionEvidenceRef | None:
    if run.receipt is None:
        return None
    return EvolutionEvidenceRef(
        uri=f"harness://runs/{quote(run.id, safe='')}/receipt",
        sha256=_digest(run.receipt.model_dump(mode="json")),
    )


def _run_ref(run: HarnessStoredRun) -> EvolutionEvidenceRef:
    return EvolutionEvidenceRef(
        uri=f"harness://runs/{quote(run.id, safe='')}/status",
        sha256=_digest(
            {
                "completed_at": run.completed_at,
                "profile_digest": run.profile_digest,
                "status": run.status,
                "tree_fingerprint_after": run.tree_fingerprint_after,
            }
        ),
    )


def _evidence_root(evidence: HarnessStoredEvidence) -> str:
    return ":".join(
        (
            evidence.kind,
            evidence.producer,
            str(evidence.summary.get("tool_name") or ""),
        )
    )


def _dedupe_refs(refs: list[EvolutionEvidenceRef]) -> list[EvolutionEvidenceRef]:
    unique: dict[str, EvolutionEvidenceRef] = {}
    for ref in refs:
        existing = unique.get(ref.uri)
        if existing is not None and existing.sha256 != ref.sha256:
            raise ValueError(f"同一 evidence URI 存在冲突摘要：{ref.uri}")
        unique.setdefault(ref.uri, ref)
    return list(unique.values())


def _check_root(check: HarnessStoredCheck) -> str:
    return _digest({"argv": check.argv, "check_key": check.check_key})


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = ["EvolutionEvidence", "EvolutionEvidenceRef", "adapt_harness_failure_evidence"]
