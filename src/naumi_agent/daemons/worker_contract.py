"""Tamper-evident worker capability, health, and admission contracts.

This module deliberately does not execute jobs.  It defines the fail-closed
boundary that a future daemon producer and Runtime scheduler must satisfy
before an execution can be admitted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import platform as platform_module
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from naumi_agent.harness.heartbeat import (
    HarnessHeartbeat,
    HarnessHeartbeatHealth,
    assess_heartbeat,
)
from naumi_agent.harness.run_lease import HarnessRunKind

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkerKind(StrEnum):
    TOOL = "tool"
    BROWSER = "browser"
    AGENT = "agent"


class WorkerCapability(StrEnum):
    SHELL_NON_PTY = "shell_non_pty"
    SHELL_PTY = "shell_pty"
    PROCESS_TREE_CANCEL = "process_tree_cancel"
    WORKSPACE_EPHEMERAL = "workspace_ephemeral"
    NETWORK_POLICY = "network_policy"
    ENVIRONMENT_ALLOWLIST = "environment_allowlist"
    RESOURCE_LIMITS = "resource_limits"
    ARTIFACT_DIGEST = "artifact_digest"
    BROWSER_PROFILE_ISOLATION = "browser_profile_isolation"
    AGENT_CONTEXT_SCOPE = "agent_context_scope"


class WorkerAdmissionDecision(StrEnum):
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class WorkerAdmissionReason(StrEnum):
    ADMITTED = "admitted"
    CONTRACT_TAMPERED = "contract_tampered"
    HEALTH_TAMPERED = "health_tampered"
    IDENTITY_MISMATCH = "identity_mismatch"
    KIND_MISMATCH = "kind_mismatch"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    PLATFORM_MISMATCH = "platform_mismatch"
    CAPABILITY_MISSING = "capability_missing"
    RESOURCE_INSUFFICIENT = "resource_insufficient"
    ISOLATION_INSUFFICIENT = "isolation_insufficient"
    HEALTH_NOT_READY = "health_not_ready"
    NOT_ACCEPTING_JOBS = "not_accepting_jobs"
    CAPACITY_EXHAUSTED = "capacity_exhausted"


@dataclass(frozen=True, slots=True)
class WorkerPlatform:
    system: str
    machine: str
    python_implementation: str
    python_version: str

    def __post_init__(self) -> None:
        if self.system not in {"darwin", "linux", "windows"}:
            raise ValueError("Worker platform.system 仅支持 darwin、linux 或 windows。")
        _require_bounded_text(self.machine, field="platform.machine", maximum=64)
        _require_bounded_text(
            self.python_implementation,
            field="platform.python_implementation",
            maximum=32,
        )
        if not _VERSION_RE.fullmatch(self.python_version):
            raise ValueError("platform.python_version 格式无效。")


@dataclass(frozen=True, slots=True)
class WorkerResourceEnvelope:
    max_concurrent_jobs: int
    max_memory_bytes: int
    max_cpu_seconds: int
    max_wall_seconds: int
    max_output_bytes: int

    def __post_init__(self) -> None:
        _require_int_range(
            self.max_concurrent_jobs,
            field="max_concurrent_jobs",
            minimum=1,
            maximum=10_000,
        )
        _require_int_range(
            self.max_memory_bytes,
            field="max_memory_bytes",
            minimum=16 * 1024 * 1024,
            maximum=16 * 1024**4,
        )
        _require_int_range(
            self.max_cpu_seconds,
            field="max_cpu_seconds",
            minimum=1,
            maximum=7 * 24 * 60 * 60,
        )
        _require_int_range(
            self.max_wall_seconds,
            field="max_wall_seconds",
            minimum=1,
            maximum=7 * 24 * 60 * 60,
        )
        _require_int_range(
            self.max_output_bytes,
            field="max_output_bytes",
            minimum=1024,
            maximum=1024**4,
        )


@dataclass(frozen=True, slots=True)
class WorkerIsolationContract:
    ephemeral_workspace: bool
    network_default_deny: bool
    environment_allowlist: bool
    resource_limits_enforced: bool
    process_tree_cancel: bool
    artifact_digest: bool

    def __post_init__(self) -> None:
        for field, value in asdict(self).items():
            if not isinstance(value, bool):
                raise TypeError(f"isolation.{field} 必须是布尔值。")


@dataclass(frozen=True, slots=True)
class WorkerContract:
    schema_version: int
    worker_id: str
    instance_id: str
    epoch: int
    kind: WorkerKind
    protocol_min: int
    protocol_max: int
    software_version: str
    platform: WorkerPlatform
    capabilities: tuple[WorkerCapability, ...]
    resources: WorkerResourceEnvelope
    isolation: WorkerIsolationContract
    issued_at: str
    contract_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Worker contract schema_version 必须为 1。")
        _require_identifier(self.worker_id, field="worker_id")
        _require_identifier(self.instance_id, field="instance_id")
        if not isinstance(self.kind, WorkerKind):
            raise TypeError("kind 必须是 WorkerKind。")
        _require_int_range(self.epoch, field="epoch", minimum=1, maximum=2**63 - 1)
        _require_int_range(self.protocol_min, field="protocol_min", minimum=1, maximum=65_535)
        _require_int_range(self.protocol_max, field="protocol_max", minimum=1, maximum=65_535)
        if self.protocol_min > self.protocol_max:
            raise ValueError("protocol_min 不能大于 protocol_max。")
        if not _VERSION_RE.fullmatch(self.software_version):
            raise ValueError("software_version 格式无效。")
        if not self.capabilities:
            raise ValueError("Worker contract 至少需要声明一项能力。")
        if any(not isinstance(item, WorkerCapability) for item in self.capabilities):
            raise TypeError("Worker capabilities 必须使用 WorkerCapability。")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Worker capabilities 不能重复。")
        if tuple(sorted(self.capabilities, key=str)) != self.capabilities:
            raise ValueError("Worker capabilities 必须按名称排序。")
        if not isinstance(self.platform, WorkerPlatform):
            raise TypeError("platform 必须是 WorkerPlatform。")
        if not isinstance(self.resources, WorkerResourceEnvelope):
            raise TypeError("resources 必须是 WorkerResourceEnvelope。")
        if not isinstance(self.isolation, WorkerIsolationContract):
            raise TypeError("isolation 必须是 WorkerIsolationContract。")
        _validate_capability_consistency(self)
        _require_aware_iso(self.issued_at, field="issued_at")
        if not _SHA256_RE.fullmatch(self.contract_sha256):
            raise ValueError("contract_sha256 必须是小写 SHA-256。")


@dataclass(frozen=True, slots=True)
class WorkerHealthReport:
    contract_sha256: str
    heartbeat: HarnessHeartbeat
    active_jobs: int
    accepting_jobs: bool
    report_sha256: str

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.contract_sha256):
            raise ValueError("health.contract_sha256 必须是小写 SHA-256。")
        _require_int_range(self.active_jobs, field="active_jobs", minimum=0, maximum=10_000)
        if not isinstance(self.accepting_jobs, bool):
            raise TypeError("accepting_jobs 必须是布尔值。")
        if not _SHA256_RE.fullmatch(self.report_sha256):
            raise ValueError("report_sha256 必须是小写 SHA-256。")
        if not isinstance(self.heartbeat, HarnessHeartbeat):
            raise TypeError("heartbeat 必须是 HarnessHeartbeat。")


@dataclass(frozen=True, slots=True)
class WorkerAdmissionRequirements:
    kind: WorkerKind
    protocol_version: int
    capabilities: tuple[WorkerCapability, ...]
    allowed_platforms: tuple[str, ...] = ()
    min_memory_bytes: int = 0
    min_cpu_seconds: int = 0
    min_wall_seconds: int = 0
    min_output_bytes: int = 0
    isolation: WorkerIsolationContract = WorkerIsolationContract(
        ephemeral_workspace=False,
        network_default_deny=False,
        environment_allowlist=False,
        resource_limits_enforced=False,
        process_tree_cancel=False,
        artifact_digest=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WorkerKind):
            raise TypeError("kind 必须是 WorkerKind。")
        _require_int_range(
            self.protocol_version,
            field="protocol_version",
            minimum=1,
            maximum=65_535,
        )
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Admission capabilities 不能重复。")
        if any(not isinstance(item, WorkerCapability) for item in self.capabilities):
            raise TypeError("Admission capabilities 必须使用 WorkerCapability。")
        if tuple(sorted(self.capabilities, key=str)) != self.capabilities:
            raise ValueError("Admission capabilities 必须按名称排序。")
        if len(set(self.allowed_platforms)) != len(self.allowed_platforms):
            raise ValueError("allowed_platforms 不能重复。")
        if tuple(sorted(self.allowed_platforms)) != self.allowed_platforms:
            raise ValueError("allowed_platforms 必须排序。")
        if any(item not in {"darwin", "linux", "windows"} for item in self.allowed_platforms):
            raise ValueError("allowed_platforms 包含未知平台。")
        if not isinstance(self.isolation, WorkerIsolationContract):
            raise TypeError("isolation 必须是 WorkerIsolationContract。")
        for field in (
            "min_memory_bytes",
            "min_cpu_seconds",
            "min_wall_seconds",
            "min_output_bytes",
        ):
            _require_int_range(getattr(self, field), field=field, minimum=0, maximum=16 * 1024**4)


@dataclass(frozen=True, slots=True)
class WorkerAdmissionResult:
    decision: WorkerAdmissionDecision
    reasons: tuple[WorkerAdmissionReason, ...]
    checked_at: str
    heartbeat_health: HarnessHeartbeatHealth | None

    @property
    def admitted(self) -> bool:
        return self.decision is WorkerAdmissionDecision.ADMITTED


def detect_worker_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
    python_implementation: str | None = None,
    python_version: str | None = None,
) -> WorkerPlatform:
    """Capture normalized cross-platform facts without using the current directory."""
    raw_system = system if system is not None else platform_module.system()
    normalized = raw_system.strip().lower()
    aliases = {"macos": "darwin", "win32": "windows"}
    normalized = aliases.get(normalized, normalized)
    return WorkerPlatform(
        system=normalized,
        machine=(machine if machine is not None else platform_module.machine()).strip().lower(),
        python_implementation=(
            python_implementation
            if python_implementation is not None
            else platform_module.python_implementation()
        )
        .strip()
        .lower(),
        python_version=(
            python_version if python_version is not None else platform_module.python_version()
        ).strip(),
    )


def issue_worker_contract(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    kind: WorkerKind,
    protocol_min: int,
    protocol_max: int,
    software_version: str,
    platform: WorkerPlatform,
    capabilities: tuple[WorkerCapability, ...],
    resources: WorkerResourceEnvelope,
    isolation: WorkerIsolationContract,
    issued_at: str,
) -> WorkerContract:
    """Validate and seal one immutable worker registration contract."""
    ordered_capabilities = tuple(sorted(capabilities, key=str))
    draft = WorkerContract(
        schema_version=1,
        worker_id=worker_id,
        instance_id=instance_id,
        epoch=epoch,
        kind=kind,
        protocol_min=protocol_min,
        protocol_max=protocol_max,
        software_version=software_version,
        platform=platform,
        capabilities=ordered_capabilities,
        resources=resources,
        isolation=isolation,
        issued_at=_canonical_timestamp(issued_at, field="issued_at"),
        contract_sha256="0" * 64,
    )
    return replace(draft, contract_sha256=_contract_digest(draft))


def verify_worker_contract(contract: WorkerContract) -> bool:
    """Verify the contract digest after transport or durable reload."""
    return hmac.compare_digest(contract.contract_sha256, _contract_digest(contract))


def issue_worker_health_report(
    *,
    contract: WorkerContract,
    heartbeat: HarnessHeartbeat,
    active_jobs: int,
    accepting_jobs: bool,
) -> WorkerHealthReport:
    """Bind current capacity to the exact contract and heartbeat generation."""
    draft = WorkerHealthReport(
        contract_sha256=contract.contract_sha256,
        heartbeat=heartbeat,
        active_jobs=active_jobs,
        accepting_jobs=accepting_jobs,
        report_sha256="0" * 64,
    )
    return replace(draft, report_sha256=_health_digest(draft))


def verify_worker_health_report(report: WorkerHealthReport) -> bool:
    return hmac.compare_digest(report.report_sha256, _health_digest(report))


def assess_worker_admission(
    contract: WorkerContract,
    report: WorkerHealthReport,
    requirements: WorkerAdmissionRequirements,
    *,
    now: str,
) -> WorkerAdmissionResult:
    """Mechanically admit a ready worker or return every blocking reason."""
    checked_at = _canonical_timestamp(now, field="now")
    reasons: list[WorkerAdmissionReason] = []
    heartbeat_health: HarnessHeartbeatHealth | None = None

    if not verify_worker_contract(contract):
        reasons.append(WorkerAdmissionReason.CONTRACT_TAMPERED)
    if not verify_worker_health_report(report):
        reasons.append(WorkerAdmissionReason.HEALTH_TAMPERED)

    expected_kind = HarnessRunKind(contract.kind.value)
    heartbeat = report.heartbeat
    if (
        report.contract_sha256 != contract.contract_sha256
        or heartbeat.subject_kind is not expected_kind
        or heartbeat.subject_id != contract.worker_id
        or heartbeat.instance_id != contract.instance_id
        or heartbeat.epoch != contract.epoch
    ):
        reasons.append(WorkerAdmissionReason.IDENTITY_MISMATCH)

    if contract.kind is not requirements.kind:
        reasons.append(WorkerAdmissionReason.KIND_MISMATCH)
    if not contract.protocol_min <= requirements.protocol_version <= contract.protocol_max:
        reasons.append(WorkerAdmissionReason.PROTOCOL_INCOMPATIBLE)
    if (
        requirements.allowed_platforms
        and contract.platform.system not in requirements.allowed_platforms
    ):
        reasons.append(WorkerAdmissionReason.PLATFORM_MISMATCH)
    if not set(requirements.capabilities).issubset(contract.capabilities):
        reasons.append(WorkerAdmissionReason.CAPABILITY_MISSING)
    if not _resources_satisfy(contract.resources, requirements):
        reasons.append(WorkerAdmissionReason.RESOURCE_INSUFFICIENT)
    if not _isolation_satisfies(contract.isolation, requirements.isolation):
        reasons.append(WorkerAdmissionReason.ISOLATION_INSUFFICIENT)

    try:
        heartbeat_health = assess_heartbeat(heartbeat, now=checked_at).health
    except ValueError:
        reasons.append(WorkerAdmissionReason.HEALTH_NOT_READY)
    else:
        if heartbeat_health is not HarnessHeartbeatHealth.HEALTHY:
            reasons.append(WorkerAdmissionReason.HEALTH_NOT_READY)
    if not report.accepting_jobs:
        reasons.append(WorkerAdmissionReason.NOT_ACCEPTING_JOBS)
    if report.active_jobs >= contract.resources.max_concurrent_jobs:
        reasons.append(WorkerAdmissionReason.CAPACITY_EXHAUSTED)

    unique_reasons = tuple(dict.fromkeys(reasons))
    if unique_reasons:
        return WorkerAdmissionResult(
            decision=WorkerAdmissionDecision.BLOCKED,
            reasons=unique_reasons,
            checked_at=checked_at,
            heartbeat_health=heartbeat_health,
        )
    return WorkerAdmissionResult(
        decision=WorkerAdmissionDecision.ADMITTED,
        reasons=(WorkerAdmissionReason.ADMITTED,),
        checked_at=checked_at,
        heartbeat_health=heartbeat_health,
    )


def _contract_digest(contract: WorkerContract) -> str:
    payload = asdict(contract)
    payload.pop("contract_sha256")
    return _canonical_sha256(payload)


def _health_digest(report: WorkerHealthReport) -> str:
    payload = asdict(report)
    payload.pop("report_sha256")
    return _canonical_sha256(payload)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        _json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _resources_satisfy(
    actual: WorkerResourceEnvelope,
    required: WorkerAdmissionRequirements,
) -> bool:
    return (
        actual.max_memory_bytes >= required.min_memory_bytes
        and actual.max_cpu_seconds >= required.min_cpu_seconds
        and actual.max_wall_seconds >= required.min_wall_seconds
        and actual.max_output_bytes >= required.min_output_bytes
    )


def _isolation_satisfies(
    actual: WorkerIsolationContract,
    required: WorkerIsolationContract,
) -> bool:
    return all(not getattr(required, field) or getattr(actual, field) for field in asdict(required))


def _validate_capability_consistency(contract: WorkerContract) -> None:
    capabilities = set(contract.capabilities)
    isolation_capabilities = {
        "ephemeral_workspace": WorkerCapability.WORKSPACE_EPHEMERAL,
        "network_default_deny": WorkerCapability.NETWORK_POLICY,
        "environment_allowlist": WorkerCapability.ENVIRONMENT_ALLOWLIST,
        "resource_limits_enforced": WorkerCapability.RESOURCE_LIMITS,
        "process_tree_cancel": WorkerCapability.PROCESS_TREE_CANCEL,
        "artifact_digest": WorkerCapability.ARTIFACT_DIGEST,
    }
    missing = [
        capability.value
        for field, capability in isolation_capabilities.items()
        if getattr(contract.isolation, field) and capability not in capabilities
    ]
    if missing:
        raise ValueError("Worker isolation 声明缺少对应能力: " + ", ".join(sorted(missing)))
    kind_specific = {
        WorkerCapability.SHELL_NON_PTY: WorkerKind.TOOL,
        WorkerCapability.SHELL_PTY: WorkerKind.TOOL,
        WorkerCapability.BROWSER_PROFILE_ISOLATION: WorkerKind.BROWSER,
        WorkerCapability.AGENT_CONTEXT_SCOPE: WorkerKind.AGENT,
    }
    incompatible = [
        capability.value
        for capability, kind in kind_specific.items()
        if capability in capabilities and contract.kind is not kind
    ]
    if incompatible:
        raise ValueError("Worker kind 与能力不兼容: " + ", ".join(sorted(incompatible)))


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} 格式无效。")


def _require_bounded_text(value: str, *, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} 必须是 1 到 {maximum} 个字符。")


def _require_int_range(value: int, *, field: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} 必须是 {minimum} 到 {maximum} 之间的整数。")


def _canonical_timestamp(value: str, *, field: str) -> str:
    parsed = _require_aware_iso(value, field=field)
    return parsed.isoformat()


def _require_aware_iso(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区偏移。")
    return parsed


__all__ = [
    "WorkerAdmissionDecision",
    "WorkerAdmissionReason",
    "WorkerAdmissionRequirements",
    "WorkerAdmissionResult",
    "WorkerCapability",
    "WorkerContract",
    "WorkerHealthReport",
    "WorkerIsolationContract",
    "WorkerKind",
    "WorkerPlatform",
    "WorkerResourceEnvelope",
    "assess_worker_admission",
    "detect_worker_platform",
    "issue_worker_contract",
    "issue_worker_health_report",
    "verify_worker_contract",
    "verify_worker_health_report",
]
