"""Explainable compatibility gate for Harness eval baseline identities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from naumi_agent.harness.eval_identity import HarnessEvalBaselineIdentity


class EvalIdentityComparisonStatus(StrEnum):
    """Whether metric comparison is valid for a directional baseline/current pair."""

    COMPARABLE = "comparable"
    COMPARABLE_WITH_CAVEATS = "comparable_with_caveats"
    INCOMPATIBLE = "incompatible"


class EvalIdentityDifferenceSeverity(StrEnum):
    BLOCKING = "blocking"
    CAVEAT = "caveat"
    INFORMATIONAL = "informational"


@dataclass(frozen=True, slots=True)
class EvalIdentityDifference:
    """One bounded, display-safe identity dimension difference."""

    dimension: str
    code: str
    baseline: str
    current: str
    severity: EvalIdentityDifferenceSeverity

    @property
    def blocking(self) -> bool:
        return self.severity is EvalIdentityDifferenceSeverity.BLOCKING


@dataclass(frozen=True, slots=True)
class EvalIdentityComparison:
    """Directional compatibility decision before any metric arithmetic."""

    status: EvalIdentityComparisonStatus
    baseline_identity_sha256: str
    current_identity_sha256: str
    source_changed: bool
    platform_changed: bool
    current_provisional: bool
    blocking_codes: tuple[str, ...]
    caveat_codes: tuple[str, ...]
    differences: tuple[EvalIdentityDifference, ...]


class _DifferenceAdder(Protocol):
    def __call__(
        self,
        dimension: str,
        code: str,
        baseline_value: Any,
        current_value: Any,
        *,
        blocking: bool,
        caveat: bool = True,
    ) -> None: ...


def compare_eval_identities(
    baseline: HarnessEvalBaselineIdentity,
    current: HarnessEvalBaselineIdentity,
) -> EvalIdentityComparison:
    """Compare semantic dimensions without treating source revision as a blocker."""
    differences: list[EvalIdentityDifference] = []
    blocking_codes: list[str] = []
    caveat_codes: list[str] = []

    def add(
        dimension: str,
        code: str,
        baseline_value: Any,
        current_value: Any,
        *,
        blocking: bool,
        caveat: bool = True,
    ) -> None:
        if baseline_value == current_value:
            return
        differences.append(
            EvalIdentityDifference(
                dimension=dimension,
                code=code,
                baseline=_display_value(baseline_value),
                current=_display_value(current_value),
                severity=(
                    EvalIdentityDifferenceSeverity.BLOCKING
                    if blocking
                    else (
                        EvalIdentityDifferenceSeverity.CAVEAT
                        if caveat
                        else EvalIdentityDifferenceSeverity.INFORMATIONAL
                    )
                ),
            )
        )
        if blocking:
            if code not in blocking_codes:
                blocking_codes.append(code)
        elif caveat and code not in caveat_codes:
            caveat_codes.append(code)

    if not baseline.baseline_eligible:
        blocking_codes.append("baseline_not_eligible")

    add(
        "schema_version",
        "schema_version_mismatch",
        baseline.schema_version,
        current.schema_version,
        blocking=True,
    )
    add(
        "source.commit",
        "source_commit_changed",
        baseline.source.commit,
        current.source.commit,
        blocking=False,
        caveat=False,
    )
    add(
        "source.tree_sha256",
        "source_tree_changed",
        baseline.source.tree_sha256,
        current.source.tree_sha256,
        blocking=False,
        caveat=False,
    )

    baseline_config = baseline.configuration
    current_config = current.configuration
    for dimension, code, baseline_value, current_value in (
        (
            "configuration.suite_id",
            "suite_id_mismatch",
            baseline_config.suite_id,
            current_config.suite_id,
        ),
        (
            "configuration.suite_sha256",
            "suite_digest_mismatch",
            baseline_config.suite_sha256,
            current_config.suite_sha256,
        ),
        (
            "configuration.profile_sha256",
            "profile_digest_mismatch",
            baseline_config.profile_sha256,
            current_config.profile_sha256,
        ),
        (
            "configuration.runner_version",
            "runner_version_mismatch",
            baseline_config.runner_version,
            current_config.runner_version,
        ),
        (
            "configuration.repetitions",
            "repetitions_mismatch",
            baseline_config.repetitions,
            current_config.repetitions,
        ),
        (
            "configuration.live",
            "live_mode_mismatch",
            baseline_config.live,
            current_config.live,
        ),
    ):
        add(dimension, code, baseline_value, current_value, blocking=True)

    _compare_models(baseline, current, add)

    baseline_platform = baseline.platform
    current_platform = current.platform
    add(
        "platform.system",
        "platform_system_changed",
        baseline_platform.system,
        current_platform.system,
        blocking=False,
    )
    baseline_runtime = (
        baseline_platform.release,
        baseline_platform.machine,
        baseline_platform.python_implementation,
        baseline_platform.python_version,
    )
    current_runtime = (
        current_platform.release,
        current_platform.machine,
        current_platform.python_implementation,
        current_platform.python_version,
    )
    add(
        "platform.runtime",
        "platform_runtime_changed",
        baseline_runtime,
        current_runtime,
        blocking=False,
    )
    add(
        "platform.naumi_version",
        "naumi_version_changed",
        baseline_platform.naumi_version,
        current_platform.naumi_version,
        blocking=False,
    )

    if current.source.dirty:
        caveat_codes.append("current_source_dirty")
    if not current.profile_trusted:
        caveat_codes.append("current_profile_untrusted")
    if current.model is not None and current.model.capability_status == "partial":
        caveat_codes.append("current_model_partial")
    if not current.baseline_eligible and not any(
        code in caveat_codes
        for code in (
            "current_source_dirty",
            "current_profile_untrusted",
            "current_model_partial",
        )
    ):
        caveat_codes.append("current_not_eligible")

    blocking_codes = list(dict.fromkeys(blocking_codes))
    caveat_codes = list(dict.fromkeys(caveat_codes))
    if blocking_codes:
        status = EvalIdentityComparisonStatus.INCOMPATIBLE
    elif caveat_codes:
        status = EvalIdentityComparisonStatus.COMPARABLE_WITH_CAVEATS
    else:
        status = EvalIdentityComparisonStatus.COMPARABLE
    return EvalIdentityComparison(
        status=status,
        baseline_identity_sha256=baseline.identity_sha256,
        current_identity_sha256=current.identity_sha256,
        source_changed=(baseline.source != current.source),
        platform_changed=(baseline.platform != current.platform),
        current_provisional=not current.baseline_eligible,
        blocking_codes=tuple(blocking_codes),
        caveat_codes=tuple(caveat_codes),
        differences=tuple(differences),
    )


def _compare_models(
    baseline: HarnessEvalBaselineIdentity,
    current: HarnessEvalBaselineIdentity,
    add: _DifferenceAdder,
) -> None:
    baseline_model = baseline.model
    current_model = current.model
    if (baseline_model is None) != (current_model is None):
        add(
            "model.presence",
            "model_presence_mismatch",
            baseline_model is not None,
            current_model is not None,
            blocking=True,
        )
        return
    if baseline_model is None or current_model is None:
        return
    add(
        "model.target",
        "model_target_mismatch",
        (
            baseline_model.requested_model,
            baseline_model.canonical_model,
            baseline_model.upstream_model,
            baseline_model.provider,
            baseline_model.api_format,
        ),
        (
            current_model.requested_model,
            current_model.canonical_model,
            current_model.upstream_model,
            current_model.provider,
            current_model.api_format,
        ),
        blocking=True,
    )
    add(
        "model.capability_sha256",
        "model_capability_mismatch",
        baseline_model.capability_sha256,
        current_model.capability_sha256,
        blocking=True,
    )
    add(
        "model.capability_status",
        "model_capability_status_mismatch",
        baseline_model.capability_status,
        current_model.capability_status,
        blocking=True,
    )
    add(
        "model.reasoning_effort",
        "reasoning_effort_mismatch",
        baseline_model.reasoning_effort,
        current_model.reasoning_effort,
        blocking=True,
    )
    add(
        "model.reasoning_contract",
        "reasoning_contract_mismatch",
        (
            baseline_model.reasoning_source,
            baseline_model.reasoning_supported,
            baseline_model.reasoning_default,
            baseline_model.reasoning_warning,
        ),
        (
            current_model.reasoning_source,
            current_model.reasoning_supported,
            current_model.reasoning_default,
            current_model.reasoning_warning,
        ),
        blocking=True,
    )


def render_eval_identity_comparison(result: EvalIdentityComparison) -> str:
    """Render a compact Chinese explanation without dumping nested identities."""
    status_text = {
        EvalIdentityComparisonStatus.COMPARABLE: "可比较",
        EvalIdentityComparisonStatus.COMPARABLE_WITH_CAVEATS: "可比较（有提示）",
        EvalIdentityComparisonStatus.INCOMPATIBLE: "不可比较",
    }[result.status]
    lines = [
        "## Harness Eval 身份比较",
        "",
        f"- 状态：{status_text}",
        f"- Baseline：`{result.baseline_identity_sha256[:12]}`",
        f"- 当前：`{result.current_identity_sha256[:12]}`",
        f"- 源码变化：{'是' if result.source_changed else '否'}",
    ]
    if result.blocking_codes:
        lines.append("- 阻断原因：")
        lines.extend(f"  - {_CODE_MESSAGES.get(code, code)}" for code in result.blocking_codes)
    if result.caveat_codes:
        lines.append("- 比较提示：")
        lines.extend(f"  - {_CODE_MESSAGES.get(code, code)}" for code in result.caveat_codes)
    return "\n".join(lines)


def _display_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        text = " / ".join(_display_value(item) for item in value)
    else:
        text = str(value)
    if len(text) == 64 and all(char in "0123456789abcdef" for char in text):
        return text[:12]
    if text.startswith("sha256:") and len(text) == 71:
        return f"sha256:{text[7:19]}"
    return text[:512]


_CODE_MESSAGES = {
    "baseline_not_eligible": "选定 Baseline 本身不具备晋升资格。",
    "schema_version_mismatch": "Identity schema 版本不同。",
    "suite_id_mismatch": "Suite ID 不同。",
    "suite_digest_mismatch": "Suite 内容摘要不同。",
    "profile_digest_mismatch": "Harness Profile 摘要不同。",
    "runner_version_mismatch": "Runner 版本不同。",
    "repetitions_mismatch": "重复次数不同。",
    "live_mode_mismatch": "Live/离线模式不同。",
    "model_presence_mismatch": "一侧调用模型，另一侧为 no-model。",
    "model_target_mismatch": "模型路由目标不同。",
    "model_capability_mismatch": "模型能力摘要不同。",
    "model_capability_status_mismatch": "模型能力可信状态不同。",
    "reasoning_effort_mismatch": "模型思考强度不同。",
    "reasoning_contract_mismatch": "模型思考能力声明不同。",
    "platform_system_changed": "操作系统不同；功能指标可比较，性能指标需分组。",
    "platform_runtime_changed": "架构、OS 或 Python 运行时不同。",
    "naumi_version_changed": "NaumiAgent 发布版本不同。",
    "current_source_dirty": "当前结果来自脏工作区，仅可作为临时比较。",
    "current_profile_untrusted": "当前 Profile 未受信任，仅可作为临时比较。",
    "current_model_partial": "当前模型能力合同仅部分验证。",
    "current_not_eligible": "当前结果不具备 Baseline 晋升资格。",
}


__all__ = [
    "EvalIdentityComparison",
    "EvalIdentityComparisonStatus",
    "EvalIdentityDifference",
    "EvalIdentityDifferenceSeverity",
    "compare_eval_identities",
    "render_eval_identity_comparison",
]
