"""Content-addressed inputs for non-executing Capability shadow observations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.capability_shadow_descriptors import (
    CapabilityShadowDescriptorView,
    EvolutionCapabilityShadowDescriptor,
    EvolutionCapabilityShadowDescriptorService,
)
from naumi_agent.evolution.capability_specification import (
    EvolutionCapabilitySpecification,
    EvolutionCapabilitySpecificationService,
)
from naumi_agent.model.reasoning import ReasoningEffortStatus
from naumi_agent.model.router import ModelCapabilityContract, ModelContractStatus, ModelTier
from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.base import ToolRegistry

_POLICY_VERSION = "evolution-capability-shadow-observation-contract-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SECRET_RE = re.compile(
    r"(?:\b(?:api[_-]?key|password|secret|token|authorization|cookie)\b\s*[:=]\s*\S+)"
    r"|(?:\bbearer\s+\S+)|(?:\bsk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|[\s`(\"':=,\[])"
    r"(?:/(?:Users|home|tmp|var)/[^\s\"']+|[A-Za-z]:[\\/][^\s\"']+)",
)
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CONTROL_TOOL_PREFIXES = ("evolution_", "harness_")
_CONTROL_TOOL_NAMES = frozenset({"request_user_input", "tool_search"})
_MAX_POSITIVE_SAMPLES = 8
_MAX_NEGATIVE_SAMPLES = 4
_MAX_COMPARISON_TOOLS = 8
type BaselineSnapshot = tuple[
    str,
    tuple[ShadowBaselineTool, ...],
    tuple[dict[str, Any], ...],
]
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {
            "type": "string",
            "enum": ["recommend", "not_recommend", "indeterminate"],
        },
        "reason_codes": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "candidate_semantic_match",
                    "candidate_schema_match",
                    "baseline_tool_better_fit",
                    "task_out_of_scope",
                    "insufficient_evidence",
                    "catalog_ambiguity",
                ],
            },
            "minItems": 1,
            "maxItems": 4,
            "uniqueItems": True,
        },
        "evidence_tool_names": {
            "type": "array",
            "items": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_.:-]{0,127}$",
            },
            "maxItems": 8,
            "uniqueItems": True,
        },
    },
    "required": ["recommendation", "reason_codes", "evidence_tool_names"],
    "additionalProperties": False,
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ShadowBaselineTool(_StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    schema_sha256: str = Field(pattern=_SHA256_RE)
    description_sha256: str = Field(pattern=_SHA256_RE)


class ShadowObservationTaskSample(_StrictModel):
    sample_id: str = Field(pattern=r"^evcsample_[0-9a-f]{24}$")
    source_kind: Literal["specification_scenario", "baseline_tool_control"]
    source_id: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=_SHA256_RE)
    task_text: str = Field(min_length=1, max_length=1_200)
    expected_recommendation: Literal["recommend", "not_recommend"]
    comparison_tool_names: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("task_text")
    @classmethod
    def _task_text_is_safe(cls, value: str) -> str:
        if (
            value != value.strip()
            or any(ord(char) < 32 and char not in {"\n", "\t"} for char in value)
            or _SECRET_RE.search(value)
            or _ABSOLUTE_PATH_RE.search(value)
        ):
            raise ValueError("Shadow task sample 必须已脱敏且不含本机绝对路径。")
        return value

    @model_validator(mode="after")
    def _identity_is_exact(self) -> ShadowObservationTaskSample:
        if (
            len(set(self.comparison_tool_names)) != len(self.comparison_tool_names)
            or any(_SAFE_TOOL_RE.fullmatch(name) is None for name in self.comparison_tool_names)
        ):
            raise ValueError("Shadow comparison tools 必须唯一且格式有效。")
        if self.source_kind == "specification_scenario":
            if re.fullmatch(
                r"ask-evcpspec-[0-9a-f]{24}-verification-\d{1,3}#\d{1,2}",
                self.source_id,
            ) is None:
                raise ValueError("Shadow specification sample source identity 无效。")
        elif _SAFE_TOOL_RE.fullmatch(self.source_id) is None:
            raise ValueError("Shadow baseline control source identity 无效。")
        core = self.model_dump(mode="json", exclude={"sample_id"})
        if self.sample_id != f"evcsample_{_digest(core)[:24]}":
            raise ValueError("Shadow task sample identity 不一致。")
        return self


class ShadowObservationModelContract(_StrictModel):
    requested_model: str = Field(min_length=1, max_length=300)
    canonical_model: str = Field(min_length=1, max_length=300)
    upstream_model: str = Field(min_length=1, max_length=300)
    provider: str = Field(min_length=1, max_length=100)
    api_format: str = Field(min_length=1, max_length=100)
    max_context: int = Field(ge=4_096)
    max_output: int = Field(ge=256)
    request_max_tokens: int = Field(ge=256)
    input_cost_per_million: float = Field(ge=0)
    output_cost_per_million: float = Field(ge=0)
    supports_tools: Literal[True] = True
    supports_streaming: bool | None
    supports_parallel_tools: bool | None
    supports_structured_output: Literal[True] = True
    supports_reasoning: bool | None
    supports_vision: bool | None
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    field_sources: dict[str, str]
    status: Literal["verified", "partial"]
    warnings: tuple[str, ...]
    errors: tuple[str, ...] = Field(default=(), max_length=0)
    contract_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator(
        "requested_model",
        "canonical_model",
        "upstream_model",
        "provider",
        "api_format",
    )
    @classmethod
    def _identity_is_safe(cls, value: str) -> str:
        if (
            value != value.strip()
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
            or _SECRET_RE.search(value)
        ):
            raise ValueError("Shadow observation model identity 格式无效。")
        return value

    @model_validator(mode="after")
    def _model_contract_is_trusted(self) -> ShadowObservationModelContract:
        if "text" not in self.input_modalities or "text" not in self.output_modalities:
            raise ValueError("Shadow observation 模型必须支持 text 输入输出。")
        critical = {
            "max_context",
            "max_output",
            "input_cost_per_million",
            "output_cost_per_million",
            "supports_tools",
            "supports_structured_output",
            "supports_reasoning",
        }
        if any(self.field_sources.get(field) in {None, "fallback"} for field in critical):
            raise ValueError("Shadow observation 模型关键参数不得来自 fallback。")
        if (
            not self.field_sources
            or len(self.field_sources) > 32
            or any(
                not key
                or not value
                or len(key) > 80
                or len(value) > 80
                or any(ord(char) < 33 or ord(char) == 127 for char in key + value)
                for key, value in self.field_sources.items()
            )
            or len(self.warnings) > 16
            or any(
                not warning.strip()
                or len(warning) > 500
                or any(ord(char) < 32 and char not in {"\n", "\t"} for char in warning)
                or _SECRET_RE.search(warning)
                for warning in self.warnings
            )
        ):
            raise ValueError("Shadow observation model provenance/warnings 格式无效。")
        core = self.model_dump(mode="json", exclude={"contract_sha256"})
        if not hmac.compare_digest(self.contract_sha256, _digest(core)):
            raise ValueError("Shadow observation model contract 摘要不一致。")
        return self


class ShadowObservationSamplingPolicy(_StrictModel):
    protocol_version: Literal["shadow-routing-recommendation-v1"] = (
        "shadow-routing-recommendation-v1"
    )
    temperature: Literal[0.0] = 0.0
    response_labels: tuple[
        Literal["recommend", "not_recommend", "indeterminate"], ...
    ] = ("recommend", "not_recommend", "indeterminate")
    response_schema: dict[str, Any]
    response_schema_sha256: str = Field(pattern=_SHA256_RE)
    reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ]
    max_input_tokens_per_call: int = Field(ge=1_024)
    max_output_tokens_per_call: int = Field(ge=128, le=512)
    max_model_calls: int = Field(ge=2, le=12)
    max_total_input_tokens: int = Field(ge=2_048)
    max_total_output_tokens: int = Field(ge=256)
    max_cost_microusd: int = Field(ge=0)
    timeout_seconds_per_call: Literal[60] = 60
    max_wall_clock_seconds: int = Field(ge=120, le=720)
    parallel_tool_calls: Literal[False] = False
    tool_execution_allowed: Literal[False] = False
    raw_chain_of_thought_requested: Literal[False] = False

    @model_validator(mode="after")
    def _budget_is_exact(self) -> ShadowObservationSamplingPolicy:
        if self.response_labels != ("recommend", "not_recommend", "indeterminate"):
            raise ValueError("Shadow observation response labels 不完整。")
        if self.response_schema != _RESPONSE_SCHEMA or not hmac.compare_digest(
            self.response_schema_sha256,
            _digest(self.response_schema),
        ):
            raise ValueError("Shadow observation structured response schema 不一致。")
        if self.max_total_input_tokens != (
            self.max_input_tokens_per_call * self.max_model_calls
        ) or self.max_total_output_tokens != (
            self.max_output_tokens_per_call * self.max_model_calls
        ):
            raise ValueError("Shadow observation 总 token 预算与单次预算不一致。")
        if self.max_wall_clock_seconds != self.timeout_seconds_per_call * self.max_model_calls:
            raise ValueError("Shadow observation wall-clock 预算不一致。")
        return self


class EvolutionCapabilityShadowObservationContract(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal[
        "evolution-capability-shadow-observation-contract-v1"
    ] = _POLICY_VERSION
    contract_id: str = Field(pattern=r"^evcsoc_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    binding_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    descriptor_id: str = Field(pattern=r"^evcsd_[0-9a-f]{24}$")
    descriptor_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evcrl_[0-9a-f]{24}$")
    lease_sha256: str = Field(pattern=_SHA256_RE)
    specification_id: str = Field(pattern=r"^evcs_[0-9a-f]{24}$")
    specification_sha256: str = Field(pattern=_SHA256_RE)
    baseline_catalog_sha256: str = Field(pattern=_SHA256_RE)
    baseline_tools: tuple[ShadowBaselineTool, ...] = Field(min_length=1, max_length=512)
    comparison_tool_names: tuple[str, ...] = Field(min_length=1, max_length=8)
    samples: tuple[ShadowObservationTaskSample, ...] = Field(min_length=2, max_length=12)
    model: ShadowObservationModelContract
    sampling: ShadowObservationSamplingPolicy
    created_at: str = Field(min_length=20, max_length=64)
    provider_call_authorized: Literal[False] = False
    observation_recorded: Literal[False] = False
    production_model_visible: Literal[False] = False
    candidate_execution_authorized: Literal[False] = False
    side_effects_allowed: Literal[False] = False
    activation_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _contract_is_exact(self) -> EvolutionCapabilityShadowObservationContract:
        _aware(self.created_at, field="created_at")
        names = tuple(item.name for item in self.baseline_tools)
        if names != tuple(sorted(names)) or len(set(names)) != len(names):
            raise ValueError("Shadow baseline tools 必须按名称排序且唯一。")
        if (
            len(set(self.comparison_tool_names)) != len(self.comparison_tool_names)
            or not set(self.comparison_tool_names).issubset(names)
        ):
            raise ValueError("Shadow comparison tools 必须来自固定 baseline。")
        if self.sampling.max_model_calls != len(self.samples):
            raise ValueError("Shadow observation 模型调用预算必须等于样本数。")
        if not (
            any(item.expected_recommendation == "recommend" for item in self.samples)
            and any(item.expected_recommendation == "not_recommend" for item in self.samples)
        ):
            raise ValueError("Shadow observation 必须同时包含正样本与负控制样本。")
        if any(
            not set(item.comparison_tool_names).issubset(self.comparison_tool_names)
            for item in self.samples
        ):
            raise ValueError("Shadow sample comparison tools 超出契约范围。")
        binding = _contract_binding_payload(self)
        if not hmac.compare_digest(self.binding_sha256, _digest(binding)):
            raise ValueError("Shadow observation contract binding 摘要不一致。")
        core = self.model_dump(mode="json", exclude={"contract_id", "contract_sha256"})
        digest = _digest(core)
        if not (
            hmac.compare_digest(self.contract_sha256, digest)
            and self.contract_id == f"evcsoc_{digest[:24]}"
        ):
            raise ValueError("Shadow observation contract content identity 不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))


class CapabilityShadowObservationContractView(_StrictModel):
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    contract: EvolutionCapabilityShadowObservationContract | None
    state: Literal[
        "missing",
        "ready",
        "descriptor_revoked",
        "catalog_changed",
        "model_changed",
        "sample_changed",
    ]
    descriptor_state: str = Field(max_length=32)
    baseline_current: bool
    model_current: bool
    samples_current: bool
    observation_input_eligible: bool
    provider_call_authorized: Literal[False] = False
    observation_recorded: Literal[False] = False
    candidate_execution_authorized: Literal[False] = False
    side_effects_allowed: Literal[False] = False
    activation_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _projection_is_exact(self) -> CapabilityShadowObservationContractView:
        if self.observation_input_eligible != (self.state == "ready"):
            raise ValueError("Shadow observation input eligibility 与状态不一致。")
        if self.contract is None and self.state != "missing":
            raise ValueError("无 Shadow observation contract 时状态只能是 missing。")
        return self


class CapabilityShadowObservationContractError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionCapabilityShadowObservationContractStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def latest(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> EvolutionCapabilityShadowObservationContract | None:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        if not self.db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                row = await (
                    await db.execute(
                        "SELECT contract_id, candidate_id, descriptor_id, "
                        "binding_sha256, created_at, payload_json, payload_sha256 "
                        "FROM evolution_capability_shadow_observation_contracts "
                        "WHERE workspace_root = ? AND candidate_id = ? "
                        "ORDER BY rowid DESC LIMIT 1",
                        (workspace, candidate_id),
                    )
                ).fetchone()
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityShadowObservationContractError(
                "shadow_observation_contract_store_read_failed",
                "无法读取 Capability Shadow observation contract。",
            ) from exc
        return (
            None
            if row is None
            else _restore(row, expected_candidate_id=candidate_id)
        )

    async def record(
        self,
        workspace_root: str | Path,
        contract: EvolutionCapabilityShadowObservationContract,
    ) -> EvolutionCapabilityShadowObservationContract:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        payload = contract.canonical_json()
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        if len(payload.encode()) > 512 * 1024:
            raise CapabilityShadowObservationContractError(
                "shadow_observation_contract_oversized",
                "Capability Shadow observation contract 超过 512 KiB。",
            )
        await self._ensure_schema()
        try:
            async with self._write_lock, aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                existing = await (
                    await db.execute(
                        "SELECT contract_id, candidate_id, descriptor_id, "
                        "binding_sha256, created_at, payload_json, payload_sha256 "
                        "FROM evolution_capability_shadow_observation_contracts "
                        "WHERE workspace_root = ? AND binding_sha256 = ?",
                        (workspace, contract.binding_sha256),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing)
                    await db.rollback()
                    return restored
                await db.execute(
                    "INSERT INTO evolution_capability_shadow_observation_contracts "
                    "(workspace_root, contract_id, candidate_id, descriptor_id, "
                    "binding_sha256, payload_json, payload_sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        contract.contract_id,
                        contract.candidate_id,
                        contract.descriptor_id,
                        contract.binding_sha256,
                        payload,
                        payload_sha256,
                        contract.created_at,
                    ),
                )
                await db.commit()
        except CapabilityShadowObservationContractError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityShadowObservationContractError(
                "shadow_observation_contract_store_write_failed",
                "无法保存 Capability Shadow observation contract。",
            ) from exc
        return contract

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    await db.executescript(_SCHEMA)
                    await db.commit()
                self.db_path.chmod(0o600)
            except (aiosqlite.Error, OSError) as exc:
                raise CapabilityShadowObservationContractError(
                    "shadow_observation_contract_store_init_failed",
                    "无法初始化 Capability Shadow observation contract Store。",
                ) from exc
            self._schema_ready = True


class EvolutionCapabilityShadowObservationContractService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        descriptor_service: EvolutionCapabilityShadowDescriptorService,
        specification_service: EvolutionCapabilitySpecificationService,
        tool_registry: ToolRegistry,
        model_port: ModelPort,
        store: EvolutionCapabilityShadowObservationContractStore,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.descriptor_service = descriptor_service
        self.specification_service = specification_service
        self.tool_registry = tool_registry
        self.model_port = model_port
        self.store = store
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self._lock = asyncio.Lock()

    async def inspect(
        self,
        candidate_id: str,
    ) -> CapabilityShadowObservationContractView:
        candidate = candidate_id.strip()
        async with self._lock:
            contract = await self.store.latest(self.workspace_root, candidate)
            if contract is None:
                return _view(candidate, None, state="missing", descriptor_state="missing")
            return await self._evaluate(contract)

    async def compile(
        self,
        candidate_id: str,
        *,
        model: str | None = None,
    ) -> CapabilityShadowObservationContractView:
        candidate = candidate_id.strip()
        async with self._lock:
            sources = await self._sources(candidate, model=model)
            descriptor_view, specification, baseline, model_contract, reasoning = sources
            assert descriptor_view.descriptor is not None
            proposed = _build_contract(
                descriptor_view=descriptor_view,
                specification=specification,
                baseline=baseline,
                model_contract=model_contract,
                reasoning=reasoning,
                created_at=self._now(),
            )
            stored = await self.store.record(self.workspace_root, proposed)
            return await self._evaluate(stored)

    async def _evaluate(
        self,
        contract: EvolutionCapabilityShadowObservationContract,
    ) -> CapabilityShadowObservationContractView:
        try:
            descriptor_view = await self.descriptor_service.inspect(contract.candidate_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            descriptor_view = None
        descriptor_current = bool(
            descriptor_view is not None
            and (
                descriptor_view.state == "ready"
                and descriptor_view.descriptor is not None
                and descriptor_view.descriptor.descriptor_id
                == contract.descriptor_id
                and hmac.compare_digest(
                    descriptor_view.descriptor.descriptor_sha256,
                    contract.descriptor_sha256,
                )
            )
        )
        descriptor_state = (
            "unavailable" if descriptor_view is None else descriptor_view.state
        )
        try:
            baseline = _baseline_snapshot(self.tool_registry)
        except (TypeError, ValueError):
            baseline = None
        baseline_current = bool(
            baseline is not None
            and hmac.compare_digest(baseline[0], contract.baseline_catalog_sha256)
        )
        try:
            model_contract, reasoning = _current_model_contract(
                self.model_port,
                contract.model.requested_model,
            )
        except (RuntimeError, TypeError, ValueError):
            model_contract = reasoning = None
        try:
            model_current = bool(
                model_contract is not None
                and reasoning is not None
                and model_contract == contract.model
                and _resolved_reasoning_effort(model_contract, reasoning)
                == contract.sampling.reasoning_effort
            )
        except ValueError:
            model_current = False
        rebuilt = None
        if (
            descriptor_current
            and baseline_current
            and model_current
            and descriptor_view is not None
            and baseline is not None
            and model_contract is not None
            and reasoning is not None
        ):
            try:
                specification_view = await self.specification_service.inspect(
                    self.workspace_root,
                    contract.candidate_id,
                )
                specification = specification_view.specification
                rebuilt = _build_contract(
                    descriptor_view=descriptor_view,
                    specification=specification,
                    baseline=baseline,
                    model_contract=model_contract,
                    reasoning=reasoning,
                    created_at=contract.created_at,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                rebuilt = None
        samples_current = bool(
            rebuilt is not None
            and hmac.compare_digest(rebuilt.binding_sha256, contract.binding_sha256)
        )
        if not descriptor_current:
            state = "descriptor_revoked"
        elif not baseline_current:
            state = "catalog_changed"
        elif not model_current:
            state = "model_changed"
        elif not samples_current:
            state = "sample_changed"
        else:
            state = "ready"
        return _view(
            contract.candidate_id,
            contract,
            state=state,
            descriptor_state=descriptor_state,
            baseline_current=baseline_current,
            model_current=model_current,
            samples_current=samples_current,
        )

    async def _sources(
        self,
        candidate_id: str,
        *,
        model: str | None,
    ) -> tuple[
        CapabilityShadowDescriptorView,
        EvolutionCapabilitySpecification,
        BaselineSnapshot,
        ShadowObservationModelContract,
        ReasoningEffortStatus,
    ]:
        descriptor_view = await self.descriptor_service.inspect(candidate_id)
        if (
            descriptor_view.state != "ready"
            or descriptor_view.descriptor is None
            or not descriptor_view.offline_shadow_input_eligible
        ):
            raise self._error(
                "descriptor_not_ready",
                "Shadow observation contract 需要 current 4a descriptor View。",
            )
        specification_view = await self.specification_service.inspect(
            self.workspace_root,
            candidate_id,
        )
        specification = specification_view.specification
        if (
            specification_view.state != "complete"
            or specification is None
            or specification.state != "complete"
            or specification.verification is None
            or specification.interface is None
            or specification.specification_id
            != descriptor_view.descriptor.specification_id
            or not hmac.compare_digest(
                specification.digest(),
                descriptor_view.descriptor.specification_sha256,
            )
        ):
            raise self._error(
                "specification_not_current",
                "Shadow observation task samples 需要 current complete Specification。",
            )
        baseline = _baseline_snapshot(self.tool_registry)
        requested = model.strip() if model and model.strip() else None
        model_contract, reasoning = _current_model_contract(self.model_port, requested)
        return descriptor_view, specification, baseline, model_contract, reasoning

    def _now(self) -> str:
        return _aware(self.now(), field="clock").astimezone(UTC).isoformat()

    @staticmethod
    def _error(code: str, message: str) -> CapabilityShadowObservationContractError:
        return CapabilityShadowObservationContractError(
            f"capability_shadow_observation_{code}",
            message,
        )


def _current_model_contract(
    model_port: ModelPort,
    model: str | None,
) -> tuple[ShadowObservationModelContract, ReasoningEffortStatus]:
    requested = model or model_port.resolve_model(ModelTier.CAPABLE)
    raw = model_port.get_model_capability_contract(requested)
    if (
        not isinstance(raw, ModelCapabilityContract)
        or raw.status not in {ModelContractStatus.VERIFIED, ModelContractStatus.PARTIAL}
        or raw.errors
        or raw.supports_tools is not True
        or raw.supports_structured_output is not True
        or raw.supports_reasoning is None
        or raw.max_context < 4_096
        or raw.max_output < 256
        or raw.request_max_tokens < 256
    ):
        raise CapabilityShadowObservationContractError(
            "capability_shadow_observation_model_unverified",
            "Shadow observation 模型身份、上下文、Tool 或 structured-output 能力未验证。",
        )
    data = raw.to_dict()
    data["status"] = raw.status.value
    data["field_sources"] = dict(raw.field_sources)
    core = {
        **data,
        "input_modalities": tuple(data["input_modalities"]),
        "output_modalities": tuple(data["output_modalities"]),
        "warnings": tuple(data["warnings"]),
        "errors": tuple(data["errors"]),
    }
    contract = ShadowObservationModelContract.model_validate({
        **core,
        "contract_sha256": _digest(core),
    })
    reasoning = model_port.get_reasoning_effort_status(requested)
    _resolved_reasoning_effort(contract, reasoning)
    return contract, reasoning


def _resolved_reasoning_effort(
    model: ShadowObservationModelContract,
    status: ReasoningEffortStatus,
) -> str:
    if status.model not in {model.requested_model, model.canonical_model}:
        raise ValueError("Shadow reasoning effort 与模型 identity 不一致。")
    if model.supports_reasoning is not True:
        return "none"
    if status.effective.value != "auto":
        effort = status.effective.value
    elif status.default is not None:
        effort = status.default.value
    else:
        raise ValueError("Reasoning 模型必须解析出固定 effort，不能保留 provider auto。")
    if status.supported and effort not in {item.value for item in status.supported}:
        raise ValueError("Shadow reasoning effort 不在模型支持集合中。")
    return effort


def _baseline_snapshot(
    registry: ToolRegistry,
) -> BaselineSnapshot:
    if not isinstance(registry, ToolRegistry):
        raise TypeError("tool_registry 必须是 ToolRegistry。")
    schemas = sorted(registry.get_openai_tools(), key=_tool_schema_name)
    if not schemas:
        raise ValueError("Shadow observation baseline Tool Catalog 不能为空。")
    entries: list[ShadowBaselineTool] = []
    normalized: list[dict[str, Any]] = []
    for schema in schemas:
        name = _tool_schema_name(schema)
        function = schema.get("function")
        if not isinstance(function, dict):
            raise ValueError("Tool schema 缺少 function。")
        description = str(function.get("description") or "").strip()
        parameters = function.get("parameters")
        if not description or not isinstance(parameters, dict):
            raise ValueError(f"Tool `{name}` schema 不完整。")
        exact = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }
        normalized.append(exact)
        entries.append(ShadowBaselineTool(
            name=name,
            schema_sha256=_digest(exact),
            description_sha256=_digest({"description": description}),
        ))
    return (
        _digest({"tools": normalized}),
        tuple(entries),
        tuple(normalized),
    )


def _tool_schema_name(schema: Mapping[str, Any]) -> str:
    function = schema.get("function") if isinstance(schema, Mapping) else None
    name = function.get("name") if isinstance(function, Mapping) else None
    if not isinstance(name, str) or _SAFE_TOOL_RE.fullmatch(name) is None:
        raise ValueError(f"Tool schema name `{name}` 格式无效。")
    return name


def _build_contract(
    *,
    descriptor_view: CapabilityShadowDescriptorView,
    specification: EvolutionCapabilitySpecification,
    baseline: BaselineSnapshot,
    model_contract: ShadowObservationModelContract,
    reasoning: ReasoningEffortStatus,
    created_at: str,
) -> EvolutionCapabilityShadowObservationContract:
    descriptor = descriptor_view.descriptor
    if descriptor is None or descriptor_view.state != "ready":
        raise ValueError("Shadow descriptor View 不具备输入资格。")
    if (
        specification is None
        or specification.verification is None
        or specification.interface is None
        or specification.specification_id != descriptor.specification_id
        or not hmac.compare_digest(specification.digest(), descriptor.specification_sha256)
    ):
        raise ValueError("Shadow task samples 与 descriptor Specification 不一致。")
    baseline_digest, baseline_entries, schemas = baseline
    comparison = _select_comparison_tools(descriptor, schemas)
    samples = _task_samples(specification, comparison, schemas)
    sampling = _sampling_policy(model_contract, reasoning, len(samples))
    source = {
        "candidate_id": descriptor.candidate_id,
        "descriptor_id": descriptor.descriptor_id,
        "descriptor_sha256": descriptor.descriptor_sha256,
        "lease_id": descriptor.lease_id,
        "lease_sha256": descriptor.lease_sha256,
        "specification_id": specification.specification_id,
        "specification_sha256": specification.digest(),
        "baseline_catalog_sha256": baseline_digest,
        "baseline_tools": [item.model_dump(mode="json") for item in baseline_entries],
        "comparison_tool_names": list(comparison),
        "samples": [item.model_dump(mode="json") for item in samples],
        "model": model_contract.model_dump(mode="json"),
        "sampling": sampling.model_dump(mode="json"),
    }
    binding_sha256 = _digest(source)
    core = {
        "schema_version": 1,
        "policy_version": _POLICY_VERSION,
        "binding_sha256": binding_sha256,
        **source,
        "created_at": _aware(created_at, field="created_at").astimezone(UTC).isoformat(),
        "provider_call_authorized": False,
        "observation_recorded": False,
        "production_model_visible": False,
        "candidate_execution_authorized": False,
        "side_effects_allowed": False,
        "activation_authorized": False,
    }
    digest = _digest(core)
    return EvolutionCapabilityShadowObservationContract.model_validate({
        **core,
        "contract_id": f"evcsoc_{digest[:24]}",
        "contract_sha256": digest,
    })


def _contract_binding_payload(
    contract: EvolutionCapabilityShadowObservationContract,
) -> dict[str, Any]:
    return {
        "candidate_id": contract.candidate_id,
        "descriptor_id": contract.descriptor_id,
        "descriptor_sha256": contract.descriptor_sha256,
        "lease_id": contract.lease_id,
        "lease_sha256": contract.lease_sha256,
        "specification_id": contract.specification_id,
        "specification_sha256": contract.specification_sha256,
        "baseline_catalog_sha256": contract.baseline_catalog_sha256,
        "baseline_tools": [item.model_dump(mode="json") for item in contract.baseline_tools],
        "comparison_tool_names": list(contract.comparison_tool_names),
        "samples": [item.model_dump(mode="json") for item in contract.samples],
        "model": contract.model.model_dump(mode="json"),
        "sampling": contract.sampling.model_dump(mode="json"),
    }


def _select_comparison_tools(
    descriptor: EvolutionCapabilityShadowDescriptor,
    schemas: Sequence[dict[str, Any]],
) -> tuple[str, ...]:
    query = f"{descriptor.declared_tool_name} {descriptor.routing_description}"
    query_tokens = _tokens(query)
    ranked: list[tuple[float, str]] = []
    for schema in schemas:
        function = schema["function"]
        name = str(function["name"])
        normalized_name = name.casefold()
        if (
            normalized_name
            in {
                descriptor.declared_tool_name.casefold(),
                descriptor.evaluation_tool_name.casefold(),
            }
            or normalized_name in _CONTROL_TOOL_NAMES
            or normalized_name.startswith(_CONTROL_TOOL_PREFIXES)
        ):
            continue
        text = f"{name} {function['description']}"
        if not _safe_sample_text(str(function["description"])):
            continue
        tokens = _tokens(text)
        overlap = len(query_tokens & tokens)
        union = len(query_tokens | tokens) or 1
        score = overlap / union
        ranked.append((score, name))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = tuple(name for _, name in ranked[:_MAX_COMPARISON_TOOLS])
    if not selected:
        raise ValueError("Tool Catalog 缺少可用的 baseline comparison tools。")
    return selected


def _task_samples(
    specification: EvolutionCapabilitySpecification,
    comparison: tuple[str, ...],
    schemas: Sequence[dict[str, Any]],
) -> tuple[ShadowObservationTaskSample, ...]:
    verification_source = next(
        (
            item
            for item in specification.interaction_sources
            if item.step == "verification"
        ),
        None,
    )
    if verification_source is None:
        raise ValueError("Specification 缺少 verification interaction source。")
    samples: list[ShadowObservationTaskSample] = []
    for index, scenario in enumerate(
        specification.verification.scenarios[:_MAX_POSITIVE_SAMPLES],
        start=1,
    ):
        task_text = f"场景：{scenario.name}\n任务输入：{scenario.fixture}"
        if not _safe_sample_text(task_text):
            raise ValueError("Specification verification scenario 不适合作为 Shadow 样本。")
        source = {
            "interaction_id": verification_source.interaction_id,
            "interaction_sha256": verification_source.interaction_sha256,
            "scenario_index": index,
            "scenario": scenario.model_dump(mode="json"),
        }
        samples.append(_sample(
            source_kind="specification_scenario",
            source_id=f"{verification_source.interaction_id}#{index}",
            source_sha256=_digest(source),
            task_text=task_text,
            expected="recommend",
            comparison=comparison,
        ))
    schema_by_name = {str(item["function"]["name"]): item for item in schemas}
    for name in comparison[:_MAX_NEGATIVE_SAMPLES]:
        schema = schema_by_name[name]
        description = str(schema["function"]["description"]).strip()
        if not _safe_sample_text(description):
            continue
        samples.append(_sample(
            source_kind="baseline_tool_control",
            source_id=name,
            source_sha256=_digest(schema),
            task_text=description,
            expected="not_recommend",
            comparison=(name,),
        ))
    if not samples or not any(
        item.expected_recommendation == "not_recommend" for item in samples
    ):
        raise ValueError("Shadow observation 缺少安全的 baseline 负控制样本。")
    return tuple(samples)


def _sample(
    *,
    source_kind: str,
    source_id: str,
    source_sha256: str,
    task_text: str,
    expected: str,
    comparison: tuple[str, ...],
) -> ShadowObservationTaskSample:
    core = {
        "source_kind": source_kind,
        "source_id": source_id,
        "source_sha256": source_sha256,
        "task_text": task_text,
        "expected_recommendation": expected,
        "comparison_tool_names": list(comparison),
    }
    return ShadowObservationTaskSample.model_validate({
        **core,
        "sample_id": f"evcsample_{_digest(core)[:24]}",
    })


def _sampling_policy(
    model: ShadowObservationModelContract,
    reasoning: ReasoningEffortStatus,
    sample_count: int,
) -> ShadowObservationSamplingPolicy:
    output = min(256, model.request_max_tokens, model.max_output)
    input_per_call = min(8_192, model.max_context - output - 1_024)
    if input_per_call < 1_024:
        raise ValueError("模型上下文不足以容纳 Shadow observation protocol。")
    total_input = input_per_call * sample_count
    total_output = output * sample_count
    max_cost_microusd = int((
        Decimal(total_input) * Decimal(str(model.input_cost_per_million))
        + Decimal(total_output) * Decimal(str(model.output_cost_per_million))
    ).to_integral_value(rounding=ROUND_CEILING))
    return ShadowObservationSamplingPolicy(
        response_schema=_RESPONSE_SCHEMA,
        response_schema_sha256=_digest(_RESPONSE_SCHEMA),
        reasoning_effort=_resolved_reasoning_effort(model, reasoning),
        max_input_tokens_per_call=input_per_call,
        max_output_tokens_per_call=output,
        max_model_calls=sample_count,
        max_total_input_tokens=total_input,
        max_total_output_tokens=total_output,
        max_cost_microusd=max_cost_microusd,
        max_wall_clock_seconds=60 * sample_count,
    )


def _safe_sample_text(value: str) -> bool:
    return bool(
        value
        and value == value.strip()
        and len(value) <= 1_200
        and not _SECRET_RE.search(value)
        and not _ABSOLUTE_PATH_RE.search(value)
        and not any(ord(char) < 32 and char not in {"\n", "\t"} for char in value)
    )


def _tokens(value: str) -> set[str]:
    expanded = _CAMEL_BOUNDARY_RE.sub(" ", value)
    expanded = re.sub(r"[_:.+/-]+", " ", expanded)
    result: set[str] = set()
    for token in _TOKEN_RE.findall(expanded):
        normalized = token.casefold()
        if re.fullmatch(r"[\u4e00-\u9fff]+", normalized):
            if len(normalized) == 1:
                continue
            result.add(normalized)
            result.update(
                normalized[index:index + 2]
                for index in range(len(normalized) - 1)
            )
        elif len(normalized) > 1:
            result.add(normalized)
    return result


def _view(
    candidate_id: str,
    contract: EvolutionCapabilityShadowObservationContract | None,
    *,
    state: str,
    descriptor_state: str,
    baseline_current: bool = False,
    model_current: bool = False,
    samples_current: bool = False,
) -> CapabilityShadowObservationContractView:
    return CapabilityShadowObservationContractView(
        candidate_id=candidate_id,
        contract=contract,
        state=state,
        descriptor_state=descriptor_state,
        baseline_current=baseline_current,
        model_current=model_current,
        samples_current=samples_current,
        observation_input_eligible=state == "ready",
    )


def render_capability_shadow_observation_contract(
    view: CapabilityShadowObservationContractView,
) -> str:
    lines = ["# Capability Shadow 观察契约", ""]
    if view.contract is None:
        return "\n".join([
            *lines,
            "尚未形成 Shadow observation contract。",
            "",
            "- Provider 调用：否 · Observation：未记录 · Candidate 执行：否",
        ])
    item = view.contract
    positive = sum(
        sample.expected_recommendation == "recommend" for sample in item.samples
    )
    negative = len(item.samples) - positive
    lines.extend([
        f"- Contract：`{item.contract_id}`",
        f"- 状态：`{view.state}` · Descriptor：`{view.descriptor_state}`",
        f"- Candidate / Descriptor：`{item.candidate_id}` / `{item.descriptor_id}`",
        f"- Baseline：`{len(item.baseline_tools)}` tools · `{item.baseline_catalog_sha256}`",
        f"- 样本：`{len(item.samples)}`（正样本 {positive} / 负控制 {negative}）",
        f"- 模型：`{item.model.provider}/{item.model.canonical_model}` · `{item.model.status}`",
        f"- Reasoning / 温度：`{item.sampling.reasoning_effort}` / `0`",
        (
            f"- 预算：{item.sampling.max_model_calls} calls · "
            f"{item.sampling.max_total_input_tokens} input tokens · "
            f"{item.sampling.max_total_output_tokens} output tokens · "
            f"{item.sampling.max_cost_microusd} µUSD"
        ),
        f"- 输入资格：{'是' if view.observation_input_eligible else '否'}",
        "- Provider 调用：否 · Observation：未记录 · Candidate 执行：否 · 激活：否",
        "- 说明：本阶段只冻结可撤销评价输入；尚未调用模型，也不会加载候选代码。",
    ])
    return "\n".join(lines)


def _restore(
    row: Any,
    *,
    expected_candidate_id: str | None = None,
) -> EvolutionCapabilityShadowObservationContract:
    (
        stored_contract_id,
        stored_candidate_id,
        stored_descriptor_id,
        stored_binding_sha256,
        stored_created_at,
        payload,
        stored_digest,
    ) = map(str, row)
    if not hmac.compare_digest(
        hashlib.sha256(payload.encode()).hexdigest(),
        stored_digest,
    ):
        raise CapabilityShadowObservationContractError(
            "shadow_observation_contract_store_tampered",
            "Capability Shadow observation contract 持久摘要不一致。",
        )
    try:
        contract = EvolutionCapabilityShadowObservationContract.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise CapabilityShadowObservationContractError(
            "shadow_observation_contract_store_invalid",
            "Capability Shadow observation contract 持久内容损坏。",
        ) from exc
    if (
        stored_contract_id != contract.contract_id
        or stored_candidate_id != contract.candidate_id
        or stored_descriptor_id != contract.descriptor_id
        or not hmac.compare_digest(stored_binding_sha256, contract.binding_sha256)
        or stored_created_at != contract.created_at
        or (
            expected_candidate_id is not None
            and stored_candidate_id != expected_candidate_id
        )
    ):
        raise CapabilityShadowObservationContractError(
            "shadow_observation_contract_store_tampered",
            "Capability Shadow observation contract 持久裁决字段不一致。",
        )
    return contract


def _aware(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO-8601 时间。") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return parsed


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_capability_shadow_observation_contracts (
    workspace_root TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    descriptor_id TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, contract_id),
    UNIQUE (workspace_root, binding_sha256)
);
CREATE INDEX IF NOT EXISTS evolution_capability_shadow_observation_candidate
ON evolution_capability_shadow_observation_contracts(
    workspace_root, candidate_id, created_at
);
"""


__all__ = [
    "CapabilityShadowObservationContractError",
    "CapabilityShadowObservationContractView",
    "EvolutionCapabilityShadowObservationContract",
    "EvolutionCapabilityShadowObservationContractService",
    "EvolutionCapabilityShadowObservationContractStore",
    "ShadowBaselineTool",
    "ShadowObservationModelContract",
    "ShadowObservationSamplingPolicy",
    "ShadowObservationTaskSample",
    "render_capability_shadow_observation_contract",
]
