"""Typed, authenticated contracts for Agent Worker supervisor fencing."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTIONABLE_HEALTH = frozenset({"stale", "offline", "stopped", "failed"})
_DEAD_PROCESS_OBSERVATIONS = frozenset({"dead", "reused", "zombie"})
_FENCE_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "evidence",
        "supervisor_owner_id",
        "supervisor_epoch",
        "process_id",
        "process_started_at_us",
        "process_observation",
        "registry_reason_code",
        "receipt_sha256",
        "authentication_sha256",
    }
)
_FENCE_EVIDENCE_FIELDS = frozenset(
    {
        "operation_id",
        "worker_id",
        "instance_id",
        "worker_epoch",
        "contract_sha256",
        "heartbeat_sequence",
        "heartbeat_phase",
        "heartbeat_health",
        "heartbeat_observed_at",
        "job_id",
        "request_sha256",
        "job_owner_id",
        "claim_epoch",
        "claim_expires_at",
        "latest_job_receipt_sha256",
        "reservation_id",
        "reservation_state",
        "reservation_expires_at",
        "decided_at",
    }
)


class AgentWorkerProcessObservationState(StrEnum):
    """Mechanical comparison between a durable witness and the current PID."""

    ALIVE = "alive"
    DEAD = "dead"
    REUSED = "reused"
    ZOMBIE = "zombie"
    UNVERIFIABLE = "unverifiable"


class AgentWorkerSupervisorLeaseState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class AgentWorkerProcessWitness:
    worker_id: str
    instance_id: str
    epoch: int
    contract_sha256: str
    process_id: int
    process_started_at_us: int
    witnessed_at: str

    def __post_init__(self) -> None:
        for field in ("worker_id", "instance_id"):
            _require_identifier(getattr(self, field), field=field)
        _require_positive_int(self.epoch, field="epoch")
        _require_sha256(self.contract_sha256, field="contract_sha256")
        _require_positive_int(self.process_id, field="process_id")
        _require_positive_int(
            self.process_started_at_us,
            field="process_started_at_us",
        )
        _aware_time(self.witnessed_at, field="witnessed_at")


@dataclass(frozen=True, slots=True)
class AgentWorkerProcessObservation:
    witness: AgentWorkerProcessWitness
    state: AgentWorkerProcessObservationState
    observed_process_started_at_us: int | None
    assessed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.witness, AgentWorkerProcessWitness):
            raise TypeError("witness 必须是 AgentWorkerProcessWitness。")
        if not isinstance(self.state, AgentWorkerProcessObservationState):
            raise TypeError("state 必须是 AgentWorkerProcessObservationState。")
        if self.observed_process_started_at_us is not None:
            _require_positive_int(
                self.observed_process_started_at_us,
                field="observed_process_started_at_us",
            )
        if self.state in {
            AgentWorkerProcessObservationState.ALIVE,
            AgentWorkerProcessObservationState.ZOMBIE,
        } and self.observed_process_started_at_us != self.witness.process_started_at_us:
            raise ValueError("精确进程观测的出生时间与 witness 不一致。")
        if (
            self.state is AgentWorkerProcessObservationState.REUSED
            and self.observed_process_started_at_us == self.witness.process_started_at_us
        ):
            raise ValueError("PID reused 观测必须包含不同的进程出生时间。")
        _aware_time(self.assessed_at, field="assessed_at")


@dataclass(frozen=True, slots=True)
class AgentWorkerSupervisorLease:
    worker_id: str
    owner_id: str
    epoch: int
    state: AgentWorkerSupervisorLeaseState
    acquired_at: str
    expires_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _require_identifier(self.worker_id, field="worker_id")
        _require_identifier(self.owner_id, field="owner_id")
        _require_positive_int(self.epoch, field="epoch")
        if not isinstance(self.state, AgentWorkerSupervisorLeaseState):
            raise TypeError("state 必须是 AgentWorkerSupervisorLeaseState。")
        acquired = _aware_time(self.acquired_at, field="acquired_at")
        expires = _aware_time(self.expires_at, field="expires_at")
        updated = _aware_time(self.updated_at, field="updated_at")
        if updated < acquired:
            raise ValueError("Supervisor lease updated_at 早于 acquired_at。")
        if self.state is AgentWorkerSupervisorLeaseState.ACTIVE and expires <= updated:
            raise ValueError("Active Supervisor lease 必须尚未到期。")
        if self.state is AgentWorkerSupervisorLeaseState.RELEASED and expires != updated:
            raise ValueError("Released Supervisor lease expiry 必须等于 updated_at。")


@dataclass(frozen=True, slots=True)
class AgentWorkerSupervisorFenceEvidence:
    operation_id: str
    worker_id: str
    instance_id: str
    worker_epoch: int
    contract_sha256: str
    heartbeat_sequence: int
    heartbeat_phase: str
    heartbeat_health: str
    heartbeat_observed_at: str
    job_id: str
    request_sha256: str
    job_owner_id: str
    claim_epoch: int
    claim_expires_at: str
    latest_job_receipt_sha256: str
    reservation_id: str
    reservation_state: str
    reservation_expires_at: str
    decided_at: str

    def __post_init__(self) -> None:
        for field in ("operation_id", "worker_id", "instance_id"):
            _require_identifier(getattr(self, field), field=field)
        _require_positive_int(self.worker_epoch, field="worker_epoch")
        _require_sha256(self.contract_sha256, field="contract_sha256")
        _require_positive_int(self.heartbeat_sequence, field="heartbeat_sequence")
        _require_identifier(self.heartbeat_phase, field="heartbeat_phase")
        if self.heartbeat_health not in _ACTIONABLE_HEALTH:
            raise ValueError("Supervisor fencing 需要非健康且可裁决的 heartbeat。")
        heartbeat_time = _aware_time(
            self.heartbeat_observed_at,
            field="heartbeat_observed_at",
        )
        decided = _aware_time(self.decided_at, field="decided_at")
        if heartbeat_time > decided:
            raise ValueError("heartbeat_observed_at 不能晚于 decided_at。")
        job_fields = (
            self.job_id,
            self.request_sha256,
            self.job_owner_id,
            self.claim_expires_at,
            self.latest_job_receipt_sha256,
            self.reservation_id,
            self.reservation_state,
            self.reservation_expires_at,
        )
        has_job = bool(self.job_id)
        if (has_job and not all(bool(value) for value in job_fields)) or (
            not has_job and any(bool(value) for value in job_fields)
        ):
            raise ValueError("Supervisor Job evidence 必须完整提供或全部为空。")
        if has_job:
            for field in ("job_id", "job_owner_id", "reservation_id"):
                _require_identifier(getattr(self, field), field=field)
            for field in ("request_sha256", "latest_job_receipt_sha256"):
                _require_sha256(getattr(self, field), field=field)
            _require_positive_int(self.claim_epoch, field="claim_epoch")
            claim_expiry = _aware_time(self.claim_expires_at, field="claim_expires_at")
            reservation_expiry = _aware_time(
                self.reservation_expires_at,
                field="reservation_expires_at",
            )
            if claim_expiry > decided or reservation_expiry > decided:
                raise ValueError("Supervisor 不能 fencing 尚未到期的 Job authority。")
            if self.reservation_state not in {"expired", "fenced"}:
                raise ValueError("Supervisor 只接受已过期或已 fencing 的物理 slot。")
        elif self.claim_epoch != 0:
            raise ValueError("无 Job evidence 时 claim_epoch 必须为 0。")


@dataclass(frozen=True, slots=True)
class AgentWorkerSupervisorFenceReceipt:
    schema_version: int
    evidence: AgentWorkerSupervisorFenceEvidence
    supervisor_owner_id: str
    supervisor_epoch: int
    process_id: int
    process_started_at_us: int
    process_observation: AgentWorkerProcessObservationState
    registry_reason_code: str
    receipt_sha256: str
    authentication_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Supervisor fence receipt schema_version 必须为 1。")
        if not isinstance(self.evidence, AgentWorkerSupervisorFenceEvidence):
            raise TypeError("evidence 必须是 AgentWorkerSupervisorFenceEvidence。")
        _require_identifier(self.supervisor_owner_id, field="supervisor_owner_id")
        _require_positive_int(self.supervisor_epoch, field="supervisor_epoch")
        _require_positive_int(self.process_id, field="process_id")
        _require_positive_int(
            self.process_started_at_us,
            field="process_started_at_us",
        )
        if self.process_observation.value not in _DEAD_PROCESS_OBSERVATIONS:
            raise ValueError("Supervisor receipt 必须证明原进程已不可执行。")
        _require_identifier(self.registry_reason_code, field="registry_reason_code")
        _require_sha256(self.receipt_sha256, field="receipt_sha256")
        _require_sha256(self.authentication_sha256, field="authentication_sha256")
        if not hmac.compare_digest(self.receipt_sha256, _receipt_digest(self)):
            raise ValueError("Supervisor fence receipt 摘要校验失败。")

    @property
    def has_job(self) -> bool:
        return bool(self.evidence.job_id)


def issue_agent_worker_supervisor_fence_receipt(
    *,
    evidence: AgentWorkerSupervisorFenceEvidence,
    supervisor_owner_id: str,
    supervisor_epoch: int,
    witness: AgentWorkerProcessWitness,
    process_observation: AgentWorkerProcessObservationState,
    authentication_key: bytes,
) -> AgentWorkerSupervisorFenceReceipt:
    if not isinstance(authentication_key, bytes) or len(authentication_key) < 32:
        raise ValueError("Supervisor receipt authentication key 至少需要 256 bit。")
    registry_reason_code = "agent_worker_supervisor_fenced"
    digest = _receipt_digest_fields(
        schema_version=1,
        evidence=evidence,
        supervisor_owner_id=supervisor_owner_id,
        supervisor_epoch=supervisor_epoch,
        process_id=witness.process_id,
        process_started_at_us=witness.process_started_at_us,
        process_observation=process_observation,
        registry_reason_code=registry_reason_code,
    )
    authentication = hmac.new(
        authentication_key,
        digest.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return AgentWorkerSupervisorFenceReceipt(
        schema_version=1,
        evidence=evidence,
        supervisor_owner_id=supervisor_owner_id,
        supervisor_epoch=supervisor_epoch,
        process_id=witness.process_id,
        process_started_at_us=witness.process_started_at_us,
        process_observation=process_observation,
        registry_reason_code=registry_reason_code,
        receipt_sha256=digest,
        authentication_sha256=authentication,
    )


def verify_agent_worker_supervisor_fence_receipt(
    receipt: AgentWorkerSupervisorFenceReceipt,
    *,
    authentication_key: bytes,
) -> bool:
    if not isinstance(receipt, AgentWorkerSupervisorFenceReceipt):
        return False
    if not isinstance(authentication_key, bytes) or len(authentication_key) < 32:
        return False
    expected = hmac.new(
        authentication_key,
        receipt.receipt_sha256.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(receipt.authentication_sha256, expected)


def supervisor_fence_receipt_json(receipt: AgentWorkerSupervisorFenceReceipt) -> str:
    return _canonical_json(_json_value(asdict(receipt)))


def supervisor_fence_receipt_from_json(raw: str) -> AgentWorkerSupervisorFenceReceipt:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 64 * 1024:
        raise ValueError("Supervisor fence receipt JSON 大小无效。")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Supervisor fence receipt JSON 结构无效。")
    if frozenset(payload) != _FENCE_RECEIPT_FIELDS:
        raise ValueError("Supervisor fence receipt JSON 字段集合无效。")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("Supervisor fence receipt evidence 结构无效。")
    if frozenset(evidence) != _FENCE_EVIDENCE_FIELDS:
        raise ValueError("Supervisor fence receipt evidence 字段集合无效。")
    try:
        return AgentWorkerSupervisorFenceReceipt(
            schema_version=payload["schema_version"],
            evidence=AgentWorkerSupervisorFenceEvidence(**evidence),
            supervisor_owner_id=payload["supervisor_owner_id"],
            supervisor_epoch=payload["supervisor_epoch"],
            process_id=payload["process_id"],
            process_started_at_us=payload["process_started_at_us"],
            process_observation=AgentWorkerProcessObservationState(
                payload["process_observation"]
            ),
            registry_reason_code=payload["registry_reason_code"],
            receipt_sha256=payload["receipt_sha256"],
            authentication_sha256=payload["authentication_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Supervisor fence receipt JSON 内容无效。") from exc


def _receipt_digest(receipt: AgentWorkerSupervisorFenceReceipt) -> str:
    return _receipt_digest_fields(
        schema_version=receipt.schema_version,
        evidence=receipt.evidence,
        supervisor_owner_id=receipt.supervisor_owner_id,
        supervisor_epoch=receipt.supervisor_epoch,
        process_id=receipt.process_id,
        process_started_at_us=receipt.process_started_at_us,
        process_observation=receipt.process_observation,
        registry_reason_code=receipt.registry_reason_code,
    )


def _receipt_digest_fields(
    *,
    schema_version: int,
    evidence: AgentWorkerSupervisorFenceEvidence,
    supervisor_owner_id: str,
    supervisor_epoch: int,
    process_id: int,
    process_started_at_us: int,
    process_observation: AgentWorkerProcessObservationState,
    registry_reason_code: str,
) -> str:
    payload = {
        "schema_version": schema_version,
        "evidence": _json_value(asdict(evidence)),
        "supervisor_owner_id": supervisor_owner_id,
        "supervisor_epoch": supervisor_epoch,
        "process_id": process_id,
        "process_started_at_us": process_started_at_us,
        "process_observation": process_observation.value,
        "registry_reason_code": registry_reason_code,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} 格式无效。")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} 必须是小写 SHA-256。")


def _require_positive_int(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} 必须是正整数。")


def _aware_time(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return parsed


__all__ = [
    "AgentWorkerProcessObservation",
    "AgentWorkerProcessObservationState",
    "AgentWorkerProcessWitness",
    "AgentWorkerSupervisorFenceEvidence",
    "AgentWorkerSupervisorFenceReceipt",
    "AgentWorkerSupervisorLease",
    "AgentWorkerSupervisorLeaseState",
    "issue_agent_worker_supervisor_fence_receipt",
    "supervisor_fence_receipt_from_json",
    "supervisor_fence_receipt_json",
    "verify_agent_worker_supervisor_fence_receipt",
]
