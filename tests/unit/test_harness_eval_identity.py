from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from naumi_agent.config.settings import ModelConfig, ModelMeta
from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    build_eval_baseline_identity,
    capture_eval_platform_identity,
)
from naumi_agent.model.reasoning import (
    ReasoningEffort,
    ReasoningEffortSetting,
    ReasoningEffortStatus,
)
from naumi_agent.model.router import (
    ModelCapabilityContract,
    ModelContractStatus,
    ModelRouter,
)


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "tests@naumi.local")
    _git(workspace, "config", "user.name", "Naumi Tests")
    (workspace / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "-qm", "fixture")
    return workspace


def _configuration(
    *,
    suite_sha256: str = "a" * 64,
) -> HarnessEvalConfigurationIdentity:
    return HarnessEvalConfigurationIdentity.create(
        suite_id="protocol-regression",
        suite_sha256=suite_sha256,
        profile_sha256="b" * 64,
        runner_version="protocol_hello@1",
        repetitions=1,
        live=False,
    )


def _capability(
    *,
    max_context: int = 131_072,
    status: ModelContractStatus = ModelContractStatus.VERIFIED,
) -> ModelCapabilityContract:
    return ModelCapabilityContract(
        requested_model="kimi-for-coding",
        canonical_model="openai/kimi-for-coding",
        upstream_model="kimi-for-coding",
        provider="openai",
        api_format="openai_responses",
        max_context=max_context,
        max_output=16_384,
        request_max_tokens=8_192,
        input_cost_per_million=1.0,
        output_cost_per_million=4.0,
        supports_tools=True,
        supports_streaming=True,
        supports_parallel_tools=True,
        supports_structured_output=True,
        supports_reasoning=True,
        supports_vision=False,
        input_modalities=("text",),
        output_modalities=("text",),
        field_sources=MappingProxyType(
            {
                "max_context": "catalog",
                "max_output": "catalog",
                "supports_tools": "catalog",
            }
        ),
        status=status,
        warnings=() if status is ModelContractStatus.VERIFIED else ("能力未验证",),
    )


def _reasoning(
    *,
    effective: ReasoningEffortSetting = ReasoningEffortSetting.HIGH,
    warning: str | None = None,
) -> ReasoningEffortStatus:
    return ReasoningEffortStatus(
        model="kimi-for-coding",
        effective=effective,
        source="runtime",
        supported=(ReasoningEffort.LOW, ReasoningEffort.HIGH),
        default=ReasoningEffort.LOW,
        warning=warning,
    )


def _platform() -> HarnessEvalPlatformIdentity:
    return HarnessEvalPlatformIdentity(
        system="macos",
        release="26.0",
        machine="arm64",
        python_implementation="CPython",
        python_version="3.13.5",
        naumi_version="0.1.214",
    )


def test_clean_identity_is_deterministic_and_baseline_eligible(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    first = build_eval_baseline_identity(
        workspace,
        configuration=_configuration(),
        capability=_capability(),
        reasoning=_reasoning(),
        platform_identity=_platform(),
    )
    second = build_eval_baseline_identity(
        workspace,
        configuration=_configuration(),
        capability=_capability(),
        reasoning=_reasoning(),
        platform_identity=_platform(),
    )

    assert first == second
    assert first.identity_sha256 == second.identity_sha256
    assert len(first.identity_sha256) == 64
    assert first.source.dirty is False
    assert first.baseline_eligible is True
    assert first.warnings == ()
    assert first.configuration.digest == _configuration().digest
    assert first.model.reasoning_effort == "high"
    assert first.model.capability_status == "verified"


def test_dirty_tree_changes_identity_and_blocks_baseline_promotion(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clean = build_eval_baseline_identity(
        workspace,
        configuration=_configuration(),
        capability=_capability(),
        reasoning=_reasoning(),
        platform_identity=_platform(),
    )
    (workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")

    dirty = build_eval_baseline_identity(
        workspace,
        configuration=_configuration(),
        capability=_capability(),
        reasoning=_reasoning(),
        platform_identity=_platform(),
    )

    assert dirty.source.commit == clean.source.commit
    assert dirty.source.tree_sha256 != clean.source.tree_sha256
    assert dirty.identity_sha256 != clean.identity_sha256
    assert dirty.source.dirty is True
    assert dirty.baseline_eligible is False
    assert any("工作区" in warning for warning in dirty.warnings)


def test_every_comparison_dimension_changes_identity(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    base = build_eval_baseline_identity(
        workspace,
        configuration=_configuration(),
        capability=_capability(),
        reasoning=_reasoning(),
        platform_identity=_platform(),
    )
    variants = (
        build_eval_baseline_identity(
            workspace,
            configuration=_configuration(suite_sha256="c" * 64),
            capability=_capability(),
            reasoning=_reasoning(),
            platform_identity=_platform(),
        ),
        build_eval_baseline_identity(
            workspace,
            configuration=_configuration(),
            capability=_capability(max_context=200_000),
            reasoning=_reasoning(),
            platform_identity=_platform(),
        ),
        build_eval_baseline_identity(
            workspace,
            configuration=_configuration(),
            capability=_capability(),
            reasoning=_reasoning(effective=ReasoningEffortSetting.LOW),
            platform_identity=_platform(),
        ),
        build_eval_baseline_identity(
            workspace,
            configuration=_configuration(),
            capability=_capability(),
            reasoning=_reasoning(),
            platform_identity=_platform().model_copy(update={"machine": "x86_64"}),
        ),
    )

    assert len({base.identity_sha256, *(item.identity_sha256 for item in variants)}) == 5


@pytest.mark.parametrize(
    ("capability", "reasoning"),
    [
        (_capability(status=ModelContractStatus.UNVERIFIED), _reasoning()),
        (_capability(), _reasoning(warning="当前模型不支持 high")),
    ],
)
def test_untrusted_model_contract_cannot_be_promoted_to_baseline(
    tmp_path: Path,
    capability: ModelCapabilityContract,
    reasoning: ReasoningEffortStatus,
) -> None:
    identity = build_eval_baseline_identity(
        _workspace(tmp_path),
        configuration=_configuration(),
        capability=capability,
        reasoning=reasoning,
        platform_identity=_platform(),
    )

    assert identity.baseline_eligible is False
    assert identity.warnings


def test_configuration_and_final_digest_are_tamper_evident(tmp_path: Path) -> None:
    configuration = _configuration()
    with pytest.raises(ValidationError, match="digest"):
        HarnessEvalConfigurationIdentity.model_validate(
            {**configuration.model_dump(mode="json"), "digest": "0" * 64}
        )

    identity = build_eval_baseline_identity(
        _workspace(tmp_path),
        configuration=configuration,
        capability=_capability(),
        reasoning=_reasoning(),
        platform_identity=_platform(),
    )
    payload = identity.model_dump(mode="json")
    payload["identity_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="identity_sha256"):
        type(identity).model_validate(payload)


def test_identity_payload_contains_no_model_warning_text_when_verified(
    tmp_path: Path,
) -> None:
    capability = replace(_capability(), warnings=("should-not-be-copied",))
    identity = build_eval_baseline_identity(
        _workspace(tmp_path),
        configuration=_configuration(),
        capability=capability,
        reasoning=_reasoning(),
        platform_identity=_platform(),
    )

    serialized = identity.model_dump_json()
    assert "should-not-be-copied" not in serialized
    assert "api_key" not in serialized


def test_identity_rejects_reasoning_status_from_another_model(tmp_path: Path) -> None:
    mismatched = replace(_reasoning(), model="other-model")

    with pytest.raises(ValueError, match="不属于同一个"):
        build_eval_baseline_identity(
            _workspace(tmp_path),
            configuration=_configuration(),
            capability=_capability(),
            reasoning=mismatched,
            platform_identity=_platform(),
        )


def test_real_model_router_contract_builds_baseline_identity(tmp_path: Path) -> None:
    router = ModelRouter(
        ModelConfig(
            provider="local-test",
            default_model="verified-model",
            max_tokens=4_096,
            model_info={
                "verified-model": ModelMeta(
                    max_context=64_000,
                    max_output=8_192,
                    input_cost_per_million=0,
                    output_cost_per_million=0,
                    supports_tools=True,
                    supports_streaming=True,
                    supports_parallel_tools=True,
                    supports_structured_output=True,
                    supports_reasoning=True,
                    supports_vision=False,
                    input_modalities=("text",),
                    output_modalities=("text",),
                    reasoning_effort=ReasoningEffortSetting.HIGH,
                    reasoning_efforts=(ReasoningEffort.LOW, ReasoningEffort.HIGH),
                    default_reasoning_effort=ReasoningEffort.LOW,
                )
            },
        )
    )
    capability = router.get_model_capability_contract("verified-model")
    reasoning = router.get_reasoning_effort_status("verified-model")

    identity = build_eval_baseline_identity(
        _workspace(tmp_path),
        configuration=_configuration(),
        capability=capability,
        reasoning=reasoning,
        platform_identity=_platform(),
    )

    assert capability.status is ModelContractStatus.VERIFIED
    assert identity.model.canonical_model == "verified-model"
    assert identity.model.provider == "local-test"
    assert identity.model.reasoning_effort == "high"
    assert identity.baseline_eligible is True


def test_capture_current_platform_returns_bounded_runtime_facts() -> None:
    identity = capture_eval_platform_identity()

    assert identity.system in {"macos", "linux", "windows", "unknown"}
    assert identity.release
    assert identity.machine
    assert identity.python_implementation
    assert identity.python_version
    assert identity.naumi_version
    assert "=" not in identity.model_dump_json()
