from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.evolution.capability_shadow_descriptors import (
    CapabilityShadowDescriptorView,
    EvolutionCapabilityShadowDescriptor,
)
from naumi_agent.evolution.capability_shadow_observation_contracts import (
    CapabilityShadowObservationContractError,
    EvolutionCapabilityShadowObservationContractService,
    EvolutionCapabilityShadowObservationContractStore,
    render_capability_shadow_observation_contract,
)
from naumi_agent.evolution.capability_specification import (
    CapabilityScenarioSpecification,
    CapabilitySpecificationInteractionSource,
    CapabilityVerificationSpecification,
)
from naumi_agent.model.reasoning import (
    ReasoningEffort,
    ReasoningEffortSetting,
    ReasoningEffortStatus,
)
from naumi_agent.model.router import ModelCapabilityContract, ModelContractStatus
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.safety.permissions import (
    TOOL_PERMISSIONS,
    PermissionChecker,
    PermissionMode,
    PermissionReasonCode,
    PermissionRiskLevel,
)
from naumi_agent.tools.base import Tool, ToolRegistry

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
CANDIDATE_ID = "evc_" + "a" * 24
SPECIFICATION_ID = "evcs_" + "b" * 24
SPECIFICATION_SHA256 = "c" * 64


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _descriptor() -> EvolutionCapabilityShadowDescriptor:
    core = {
        "schema_version": 1,
        "policy_version": "evolution-capability-shadow-descriptor-v1",
        "candidate_id": CANDIDATE_ID,
        "candidate_revision": 1,
        "candidate_sha256": "d" * 64,
        "lease_id": "evcrl_" + "e" * 24,
        "lease_sha256": "f" * 64,
        "lease_expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "artifact_id": "evcia_" + "1" * 24,
        "artifact_sha256": "2" * 64,
        "specification_id": SPECIFICATION_ID,
        "specification_sha256": SPECIFICATION_SHA256,
        "declared_tool_name": "browser_trace_compare",
        "evaluation_tool_name": f"shadow_{'a' * 24}_browser_trace_compare",
        "routing_description": "比较两份浏览器执行轨迹并报告关键差异。",
        "parameters_schema": {
            "type": "object",
            "properties": {"left": {"type": "string"}, "right": {"type": "string"}},
            "required": ["left", "right"],
            "additionalProperties": False,
        },
        "parameters_schema_sha256": "",
        "result_schema_sha256": _digest({"type": "object"}),
        "error_codes": ["invalid_trace"],
        "permission_families": ["workspace_read"],
        "scenario_names": ["比较有效轨迹"],
        "evaluation_mode": "counterfactual_recommendation_only",
        "offline_shadow_input_authorized": False,
        "production_model_visible": False,
        "registry_resolvable": False,
        "side_effects_allowed": False,
        "execution_authorized": False,
        "activation_authorized": False,
        "created_at": NOW.isoformat(),
    }
    core["parameters_schema_sha256"] = _digest(core["parameters_schema"])
    digest = _digest(core)
    return EvolutionCapabilityShadowDescriptor.model_validate({
        **core,
        "descriptor_id": f"evcsd_{digest[:24]}",
        "descriptor_sha256": digest,
    })


def test_shadow_descriptor_rejects_json_embedded_local_path() -> None:
    descriptor = _descriptor()
    core = descriptor.model_dump(
        mode="json",
        exclude={"descriptor_id", "descriptor_sha256"},
    )
    core["routing_description"] = '比较 {"path":"/Users/example/private.json"}'
    digest = _digest(core)

    with pytest.raises(ValueError, match="已脱敏"):
        EvolutionCapabilityShadowDescriptor.model_validate({
            **core,
            "descriptor_id": f"evcsd_{digest[:24]}",
            "descriptor_sha256": digest,
        })


class _DescriptorService:
    def __init__(self) -> None:
        self.state = "ready"
        self.descriptor = _descriptor()

    async def inspect(self, candidate_id: str) -> CapabilityShadowDescriptorView:
        return CapabilityShadowDescriptorView(
            candidate_id=candidate_id,
            descriptor=self.descriptor,
            state=self.state,
            lease_state="active" if self.state == "ready" else "revoked",
            source_current=self.state == "ready",
            offline_shadow_input_eligible=self.state == "ready",
        )


class _Specification:
    def __init__(self) -> None:
        self.specification_id = SPECIFICATION_ID
        self.state = "complete"
        self.interface = SimpleNamespace(tool_name="browser_trace_compare")
        self.verification = CapabilityVerificationSpecification(scenarios=(
            CapabilityScenarioSpecification(
                name="比较有效轨迹",
                fixture='{"left":"trace-a","right":"trace-b"}',
                expected="返回结构化差异列表。",
            ),
            CapabilityScenarioSpecification(
                name="拒绝损坏轨迹",
                fixture='{"left":"broken","right":"trace-b"}',
                expected="返回 invalid_trace。",
            ),
        ))
        self.interaction_sources = (
            CapabilitySpecificationInteractionSource(
                step="verification",
                interaction_id=(
                    "ask-evcpspec-" + "b" * 24 + "-verification-1"
                ),
                interaction_sequence=2,
                interaction_sha256="3" * 64,
                answered_at=NOW.isoformat(),
            ),
        )

    def digest(self) -> str:
        return SPECIFICATION_SHA256


class _SpecificationService:
    def __init__(self) -> None:
        self.specification = _Specification()

    async def inspect(self, _workspace: Path, _candidate_id: str):
        return SimpleNamespace(state="complete", specification=self.specification)


class _Tool(Tool):
    def __init__(self, name: str, description: str) -> None:
        self._name = name
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, **_kwargs: object) -> str:
        return "ok"


class _ModelPort:
    def __init__(self) -> None:
        self.canonical_model = "test-shadow-model"
        self.supports_structured_output = True
        self.supports_reasoning: bool | None = True
        self.max_context_source = "config"
        self.reasoning_effective = ReasoningEffortSetting.LOW
        self.reasoning_default: ReasoningEffort | None = ReasoningEffort.LOW

    def resolve_model(self, _tier: object) -> str:
        return "test-shadow-model"

    def get_model_capability_contract(self, model: str | None = None):
        requested = model or "test-shadow-model"
        return ModelCapabilityContract(
            requested_model=requested,
            canonical_model=self.canonical_model,
            upstream_model=self.canonical_model,
            provider="test-provider",
            api_format="openai_chat",
            max_context=32_768,
            max_output=4_096,
            request_max_tokens=2_048,
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
            supports_tools=True,
            supports_streaming=True,
            supports_parallel_tools=True,
            supports_structured_output=self.supports_structured_output,
            supports_reasoning=self.supports_reasoning,
            supports_vision=False,
            input_modalities=("text",),
            output_modalities=("text",),
            field_sources=MappingProxyType({
                "max_context": self.max_context_source,
                "max_output": "config",
                "input_cost_per_million": "config",
                "output_cost_per_million": "config",
                "supports_tools": "config",
                "supports_structured_output": "config",
                "supports_reasoning": "config",
            }),
            status=ModelContractStatus.VERIFIED,
        )

    def get_reasoning_effort_status(self, model: str | None = None):
        return ReasoningEffortStatus(
            model=model or "test-shadow-model",
            effective=self.reasoning_effective,
            source="runtime",
            supported=(ReasoningEffort.LOW, ReasoningEffort.MEDIUM),
            default=self.reasoning_default,
        )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_Tool("browser_open", "打开浏览器页面并返回可交互页面状态。"))
    registry.register(_Tool("file_compare", "比较两个工作区文件并返回文本差异。"))
    registry.register(_Tool("trace_read", "读取一份执行轨迹并返回事件摘要。"))
    return registry


def _service(tmp_path: Path, *, now: datetime = NOW):
    descriptor = _DescriptorService()
    specification = _SpecificationService()
    registry = _registry()
    model = _ModelPort()
    service = EvolutionCapabilityShadowObservationContractService(
        workspace_root=tmp_path,
        descriptor_service=descriptor,  # type: ignore[arg-type]
        specification_service=specification,  # type: ignore[arg-type]
        tool_registry=registry,
        model_port=model,  # type: ignore[arg-type]
        store=EvolutionCapabilityShadowObservationContractStore(
            tmp_path / "evolution.db"
        ),
        now=lambda: now.isoformat(),
    )
    return service, descriptor, specification, registry, model


def test_shadow_observation_contract_tool_permission_is_bounded() -> None:
    rule = TOOL_PERMISSIONS["evolution_capability_shadow_observation_contract"]

    assert rule.allowed_modes == [
        PermissionMode.BYPASS,
        PermissionMode.PERMISSIVE,
        PermissionMode.MODERATE,
        PermissionMode.STRICT,
    ]
    assert rule.requires_confirmation is False
    assert rule.max_calls_per_session == 50
    assert rule.risk_level is PermissionRiskLevel.MEDIUM


@pytest.mark.asyncio
async def test_real_engine_registers_shadow_observation_contract_on_shared_surfaces(
    tmp_path: Path,
) -> None:
    engine = AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
    ))
    try:
        tool = engine.tool_registry.get_exact(
            "evolution_capability_shadow_observation_contract"
        )
        assert tool is not None
        assert tool.metadata.requires_confirmation is False
        decision = PermissionChecker(
            PermissionMode.MODERATE,
            allowed_dirs=[str(tmp_path)],
        ).check(tool.name, {}, tool=tool)
        assert decision.allowed is True
        assert decision.code is not PermissionReasonCode.UNKNOWN_TOOL
        assert (
            engine.evolution_capability_shadow_observation_contract_service.tool_registry
            is engine.tool_registry
        )
        view = (
            await engine.evolution_capability_shadow_observation_contract_service.inspect(
                CANDIDATE_ID
            )
        )
        assert view.state == "missing"
        assert view.provider_call_authorized is False
        slash = await execute_slash_command(
            engine,
            f"/evolution capability-shadow-observation-status {CANDIDATE_ID}",
        )
        assert "尚未形成 Shadow observation contract" in slash
        assert "Provider 调用：否" in slash
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_shadow_observation_contract_freezes_real_inputs_without_authority(
    tmp_path: Path,
) -> None:
    service, _, _, _, _ = _service(tmp_path)

    view = await service.compile(CANDIDATE_ID)

    assert view.state == "ready"
    assert view.observation_input_eligible is True
    assert view.contract is not None
    contract = view.contract
    assert contract.provider_call_authorized is False
    assert contract.observation_recorded is False
    assert contract.candidate_execution_authorized is False
    assert contract.activation_authorized is False
    assert contract.model.canonical_model == "test-shadow-model"
    assert contract.sampling.reasoning_effort == "low"
    assert contract.sampling.temperature == 0
    assert contract.sampling.response_schema["additionalProperties"] is False
    assert contract.sampling.response_schema["required"] == [
        "recommendation",
        "reason_codes",
        "evidence_tool_names",
    ]
    assert contract.sampling.max_model_calls == len(contract.samples)
    assert contract.sampling.max_cost_microusd == (
        contract.sampling.max_total_input_tokens
        + 2 * contract.sampling.max_total_output_tokens
    )
    assert {sample.expected_recommendation for sample in contract.samples} == {
        "recommend",
        "not_recommend",
    }
    assert all("/Users/" not in sample.task_text for sample in contract.samples)
    assert "Provider 调用：否" in render_capability_shadow_observation_contract(view)


@pytest.mark.asyncio
async def test_shadow_observation_contract_rejects_untrusted_model_parameters(
    tmp_path: Path,
) -> None:
    service, _, _, _, model = _service(tmp_path)
    model.max_context_source = "fallback"
    with pytest.raises(ValueError, match="fallback"):
        await service.compile(CANDIDATE_ID)

    model.max_context_source = "config"
    model.supports_structured_output = False
    with pytest.raises(
        CapabilityShadowObservationContractError,
        match="structured-output",
    ):
        await service.compile(CANDIDATE_ID)

    model.supports_structured_output = True
    model.supports_reasoning = None
    with pytest.raises(
        CapabilityShadowObservationContractError,
        match="模型身份",
    ):
        await service.compile(CANDIDATE_ID)

    model.supports_reasoning = True
    model.reasoning_effective = ReasoningEffortSetting.AUTO
    model.reasoning_default = None
    with pytest.raises(ValueError, match="固定 effort"):
        await service.compile(CANDIDATE_ID)


@pytest.mark.asyncio
async def test_shadow_observation_contract_rejects_private_sample_text(
    tmp_path: Path,
) -> None:
    service, _, specification, _, _ = _service(tmp_path)
    specification.specification.verification = CapabilityVerificationSpecification(
        scenarios=(
            CapabilityScenarioSpecification(
                name="本机轨迹",
                fixture='{"path":"/Users/example/private/trace.json"}',
                expected="返回差异。",
            ),
        )
    )

    with pytest.raises(ValueError, match="不适合作为 Shadow 样本"):
        await service.compile(CANDIDATE_ID)


@pytest.mark.asyncio
async def test_shadow_observation_contract_is_first_wins_across_stores(
    tmp_path: Path,
) -> None:
    first, _, _, _, _ = _service(tmp_path, now=NOW)
    second, _, _, _, _ = _service(tmp_path, now=NOW + timedelta(seconds=1))

    first_view, second_view = await asyncio.gather(
        first.compile(CANDIDATE_ID),
        second.compile(CANDIDATE_ID),
    )

    assert first_view.contract is not None
    assert second_view.contract == first_view.contract
    with sqlite3.connect(tmp_path / "evolution.db") as db:
        count = db.execute(
            "SELECT COUNT(*) FROM evolution_capability_shadow_observation_contracts"
        ).fetchone()
    assert count == (1,)


@pytest.mark.asyncio
async def test_shadow_observation_contract_revokes_on_each_dynamic_source(
    tmp_path: Path,
) -> None:
    service, descriptor, specification, registry, model = _service(tmp_path)
    ready = await service.compile(CANDIDATE_ID)
    assert ready.state == "ready"

    added = _Tool("web_fetch", "获取公开网页并返回正文。")
    registry.register(added)
    assert (await service.inspect(CANDIDATE_ID)).state == "catalog_changed"
    assert registry.unregister_if_same("web_fetch", added)

    model.canonical_model = "changed-shadow-model"
    assert (await service.inspect(CANDIDATE_ID)).state == "model_changed"
    model.canonical_model = "test-shadow-model"

    specification.specification.verification = CapabilityVerificationSpecification(
        scenarios=(
            CapabilityScenarioSpecification(
                name="新的场景",
                fixture='{"left":"new","right":"trace-b"}',
                expected="返回差异。",
            ),
        )
    )
    assert (await service.inspect(CANDIDATE_ID)).state == "sample_changed"

    descriptor.state = "revoked"
    assert (await service.inspect(CANDIDATE_ID)).state == "descriptor_revoked"


@pytest.mark.asyncio
async def test_shadow_observation_contract_store_rejects_inner_and_outer_tamper(
    tmp_path: Path,
) -> None:
    service, _, _, _, _ = _service(tmp_path)
    view = await service.compile(CANDIDATE_ID)
    assert view.contract is not None
    with sqlite3.connect(tmp_path / "evolution.db") as db:
        row = db.execute(
            "SELECT payload_json FROM evolution_capability_shadow_observation_contracts "
            "WHERE contract_id = ?",
            (view.contract.contract_id,),
        ).fetchone()
        assert row is not None
        altered = json.loads(str(row[0]))
        altered["sampling"]["temperature"] = 0.1
        payload = _canonical(altered)
        db.execute(
            "UPDATE evolution_capability_shadow_observation_contracts "
            "SET payload_json = ?, payload_sha256 = ? WHERE contract_id = ?",
            (payload, hashlib.sha256(payload.encode()).hexdigest(), view.contract.contract_id),
        )
        db.commit()
    with pytest.raises(CapabilityShadowObservationContractError, match="持久内容"):
        await service.store.latest(tmp_path, CANDIDATE_ID)

    with sqlite3.connect(tmp_path / "evolution.db") as db:
        db.execute(
            "UPDATE evolution_capability_shadow_observation_contracts "
            "SET payload_sha256 = ? WHERE contract_id = ?",
            ("0" * 64, view.contract.contract_id),
        )
        db.commit()
    with pytest.raises(CapabilityShadowObservationContractError, match="持久摘要"):
        await service.store.latest(tmp_path, CANDIDATE_ID)


@pytest.mark.asyncio
async def test_shadow_observation_contract_store_rejects_relational_tamper(
    tmp_path: Path,
) -> None:
    service, _, _, _, _ = _service(tmp_path)
    view = await service.compile(CANDIDATE_ID)
    assert view.contract is not None

    with sqlite3.connect(tmp_path / "evolution.db") as db:
        db.execute(
            "UPDATE evolution_capability_shadow_observation_contracts "
            "SET descriptor_id = ? WHERE contract_id = ?",
            ("evcsd_" + "f" * 24, view.contract.contract_id),
        )
        db.commit()

    with pytest.raises(
        CapabilityShadowObservationContractError,
        match="持久裁决字段",
    ):
        await service.store.latest(tmp_path, CANDIDATE_ID)
