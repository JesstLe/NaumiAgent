from __future__ import annotations

import subprocess
from pathlib import Path
from types import MappingProxyType

import pytest

from naumi_agent.harness.eval_compare import (
    EvalIdentityComparisonStatus,
    EvalIdentityDifferenceSeverity,
    compare_eval_identities,
    render_eval_identity_comparison,
)
from naumi_agent.harness.eval_identity import (
    HarnessEvalBaselineIdentity,
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.model.reasoning import (
    ReasoningEffort,
    ReasoningEffortSetting,
    ReasoningEffortStatus,
)
from naumi_agent.model.router import ModelCapabilityContract, ModelContractStatus


def _configuration(
    *,
    suite_id: str = "protocol-regression",
    suite_sha256: str = "a" * 64,
    profile_sha256: str = "b" * 64,
    runner_version: str = "protocol_hello@1",
    repetitions: int = 1,
    live: bool = False,
) -> HarnessEvalConfigurationIdentity:
    return HarnessEvalConfigurationIdentity.create(
        suite_id=suite_id,
        suite_sha256=suite_sha256,
        profile_sha256=profile_sha256,
        runner_version=runner_version,
        repetitions=repetitions,
        live=live,
    )


def _source(*, commit: str = "1" * 40, tree: str = "2" * 64, dirty: bool = False):
    return HarnessEvalSourceIdentity(
        commit=commit,
        tree_sha256=f"sha256:{tree}",
        dirty=dirty,
    )


def _platform(**updates: str) -> HarnessEvalPlatformIdentity:
    payload = {
        "system": "linux",
        "release": "6.12",
        "machine": "x86_64",
        "python_implementation": "CPython",
        "python_version": "3.13.5",
        "naumi_version": "0.1.214",
        **updates,
    }
    return HarnessEvalPlatformIdentity.model_validate(payload)


def _capability(*, context: int = 128_000, model: str = "model-a"):
    return ModelCapabilityContract(
        requested_model=model,
        canonical_model=f"provider/{model}",
        upstream_model=model,
        provider="provider",
        api_format="openai_responses",
        max_context=context,
        max_output=8_192,
        request_max_tokens=4_096,
        input_cost_per_million=1.0,
        output_cost_per_million=3.0,
        supports_tools=True,
        supports_streaming=True,
        supports_parallel_tools=True,
        supports_structured_output=True,
        supports_reasoning=True,
        supports_vision=False,
        input_modalities=("text",),
        output_modalities=("text",),
        field_sources=MappingProxyType({"max_context": "catalog"}),
        status=ModelContractStatus.VERIFIED,
    )


def _reasoning(
    *,
    effort: ReasoningEffortSetting = ReasoningEffortSetting.HIGH,
    model: str = "model-a",
):
    return ReasoningEffortStatus(
        model=model,
        effective=effort,
        source="runtime",
        supported=(ReasoningEffort.LOW, ReasoningEffort.HIGH),
        default=ReasoningEffort.LOW,
    )


def _identity(
    *,
    configuration: HarnessEvalConfigurationIdentity | None = None,
    source: HarnessEvalSourceIdentity | None = None,
    platform: HarnessEvalPlatformIdentity | None = None,
    profile_trusted: bool = True,
    capability: ModelCapabilityContract | None = None,
    reasoning: ReasoningEffortStatus | None = None,
) -> HarnessEvalBaselineIdentity:
    return build_eval_baseline_identity(
        Path("."),
        configuration=configuration or _configuration(),
        capability=capability,
        reasoning=reasoning,
        platform_identity=platform or _platform(),
        profile_trusted=profile_trusted,
        source_identity=source or _source(),
    )


def test_source_revision_change_is_the_comparison_subject_not_a_blocker() -> None:
    baseline = _identity()
    current = _identity(
        source=_source(commit="3" * 40, tree="4" * 64),
    )

    result = compare_eval_identities(baseline, current)

    assert result.status is EvalIdentityComparisonStatus.COMPARABLE
    assert result.source_changed is True
    assert result.blocking_codes == ()
    assert result.caveat_codes == ()
    assert any(item.dimension == "source.commit" for item in result.differences)
    assert all(not item.blocking for item in result.differences)
    assert all(
        item.severity is EvalIdentityDifferenceSeverity.INFORMATIONAL
        for item in result.differences
    )


def test_exact_repeat_is_comparable_without_source_change() -> None:
    identity = _identity()

    result = compare_eval_identities(identity, identity)

    assert result.status is EvalIdentityComparisonStatus.COMPARABLE
    assert result.source_changed is False
    assert result.differences == ()


@pytest.mark.parametrize(
    ("configuration", "code"),
    [
        (_configuration(suite_id="other-suite"), "suite_id_mismatch"),
        (_configuration(suite_sha256="c" * 64), "suite_digest_mismatch"),
        (_configuration(profile_sha256="d" * 64), "profile_digest_mismatch"),
        (_configuration(runner_version="protocol_hello@2"), "runner_version_mismatch"),
        (_configuration(repetitions=5), "repetitions_mismatch"),
        (_configuration(live=True), "live_mode_mismatch"),
    ],
)
def test_eval_definition_changes_are_hard_incompatibilities(
    configuration: HarnessEvalConfigurationIdentity,
    code: str,
) -> None:
    result = compare_eval_identities(_identity(), _identity(configuration=configuration))

    assert result.status is EvalIdentityComparisonStatus.INCOMPATIBLE
    assert code in result.blocking_codes
    assert any(item.code == code and item.blocking for item in result.differences)


def test_platform_change_is_comparable_with_explicit_caveats() -> None:
    current = _identity(
        platform=_platform(
            system="macos",
            release="26.0",
            machine="arm64",
            python_version="3.14.0",
            naumi_version="0.1.215",
        )
    )

    result = compare_eval_identities(_identity(), current)

    assert result.status is EvalIdentityComparisonStatus.COMPARABLE_WITH_CAVEATS
    assert result.platform_changed is True
    assert set(result.caveat_codes) == {
        "platform_system_changed",
        "platform_runtime_changed",
        "naumi_version_changed",
    }
    assert result.blocking_codes == ()
    assert all(
        item.severity is EvalIdentityDifferenceSeverity.CAVEAT
        for item in result.differences
    )


@pytest.mark.parametrize(
    ("current", "code"),
    [
        (_identity(source=_source(dirty=True)), "current_source_dirty"),
        (_identity(profile_trusted=False), "current_profile_untrusted"),
    ],
)
def test_non_promotable_current_result_is_provisionally_comparable(
    current: HarnessEvalBaselineIdentity,
    code: str,
) -> None:
    result = compare_eval_identities(_identity(), current)

    assert result.status is EvalIdentityComparisonStatus.COMPARABLE_WITH_CAVEATS
    assert result.current_provisional is True
    assert code in result.caveat_codes


@pytest.mark.parametrize(
    "baseline",
    [
        _identity(source=_source(dirty=True)),
        _identity(profile_trusted=False),
    ],
)
def test_non_promotable_baseline_is_rejected(
    baseline: HarnessEvalBaselineIdentity,
) -> None:
    result = compare_eval_identities(baseline, _identity())

    assert result.status is EvalIdentityComparisonStatus.INCOMPATIBLE
    assert "baseline_not_eligible" in result.blocking_codes


def test_model_presence_capability_and_reasoning_changes_are_blocking() -> None:
    model_baseline = _identity(capability=_capability(), reasoning=_reasoning())
    no_model = compare_eval_identities(model_baseline, _identity())
    capability = compare_eval_identities(
        model_baseline,
        _identity(capability=_capability(context=200_000), reasoning=_reasoning()),
    )
    reasoning = compare_eval_identities(
        model_baseline,
        _identity(
            capability=_capability(),
            reasoning=_reasoning(effort=ReasoningEffortSetting.LOW),
        ),
    )

    assert "model_presence_mismatch" in no_model.blocking_codes
    assert "model_capability_mismatch" in capability.blocking_codes
    assert "reasoning_effort_mismatch" in reasoning.blocking_codes


def test_renderer_explains_blockers_without_dumping_full_identity() -> None:
    result = compare_eval_identities(
        _identity(),
        _identity(configuration=_configuration(suite_sha256="f" * 64)),
    )

    rendered = render_eval_identity_comparison(result)

    assert "不可比较" in rendered
    assert "Suite 内容摘要不同" in rendered
    assert result.baseline_identity_sha256[:12] in rendered
    assert result.current_identity_sha256[:12] in rendered
    assert "tree_sha256" not in rendered
    assert len(rendered) < 2_000


def test_real_git_two_commits_are_comparable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "tests@naumi.local")
    _git(workspace, "config", "user.name", "Naumi Tests")
    target = workspace / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "baseline")
    baseline = build_eval_baseline_identity(
        workspace,
        configuration=_configuration(),
        platform_identity=_platform(),
    )
    target.write_text("VALUE = 2\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "current")
    current = build_eval_baseline_identity(
        workspace,
        configuration=_configuration(),
        platform_identity=_platform(),
    )

    result = compare_eval_identities(baseline, current)

    assert result.status is EvalIdentityComparisonStatus.COMPARABLE
    assert result.source_changed is True
    assert baseline.source.commit != current.source.commit


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
