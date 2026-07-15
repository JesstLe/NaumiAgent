"""Deterministic, read-only explanations for durable Harness runs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from naumi_agent.harness.store import HarnessStoredRun

HARNESS_EXPLAIN_RULE_VERSION = "1"


class HarnessFailureClass(StrEnum):
    SPECIFICATION_GAP = "specification_gap"
    KNOWLEDGE_GAP = "knowledge_gap"
    CONTEXT_OVERFLOW = "context_overflow"
    TOOL_CONTRACT_ERROR = "tool_contract_error"
    PERMISSION_BLOCK = "permission_block"
    ENVIRONMENT_ERROR = "environment_error"
    IMPLEMENTATION_ERROR = "implementation_error"
    VERIFICATION_FAILURE = "verification_failure"
    EVALUATION_ERROR = "evaluation_error"
    AGENT_PREMATURE_FINISH = "agent_premature_finish"
    AGENT_REPETITION = "agent_repetition"
    HUMAN_JUDGMENT_REQUIRED = "human_judgment_required"


@dataclass(frozen=True, slots=True)
class HarnessExplainFinding:
    failure_class: HarnessFailureClass
    source: str
    message: str
    next_step: str
    check_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HarnessExplainCheck:
    id: str
    status: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class HarnessExplainEvidence:
    id: str
    kind: str
    status: str
    digest_prefix: str
    uri: str


@dataclass(frozen=True, slots=True)
class HarnessRunExplanation:
    run_id: str
    status: str
    objective: str
    started_at: str
    completed_at: str
    verified: bool
    running: bool
    summary: str
    failure_classes: tuple[HarnessFailureClass, ...]
    findings: tuple[HarnessExplainFinding, ...]
    checks: tuple[HarnessExplainCheck, ...]
    evidence: tuple[HarnessExplainEvidence, ...]

    @property
    def check_count(self) -> int:
        return len(self.checks)

    @property
    def evidence_count(self) -> int:
        return len(self.evidence)


@dataclass(frozen=True, slots=True)
class HarnessExplainLookup:
    status: Literal["ok", "not_found", "unavailable"]
    explanation: HarnessRunExplanation | None = None
    message: str = ""


class HarnessExplainer:
    """Classify one stored run from mechanical facts only."""

    def explain(self, run: HarnessStoredRun) -> HarnessRunExplanation:
        findings: dict[HarnessFailureClass, HarnessExplainFinding] = {}

        def add(
            failure_class: HarnessFailureClass,
            *,
            source: str,
            message: str,
            next_step: str,
            check_ids: tuple[str, ...] = (),
            evidence_ids: tuple[str, ...] = (),
        ) -> None:
            existing = findings.get(failure_class)
            if existing is None:
                findings[failure_class] = HarnessExplainFinding(
                    failure_class=failure_class,
                    source=source,
                    message=message,
                    next_step=next_step,
                    check_ids=check_ids,
                    evidence_ids=evidence_ids,
                )
                return
            findings[failure_class] = replace(
                existing,
                source=_merge_sources(existing.source, source),
                check_ids=_merge_ids(existing.check_ids, check_ids),
                evidence_ids=_merge_ids(existing.evidence_ids, evidence_ids),
            )

        for check in run.checks:
            source = f"check:{check.check_key}"
            check_ids = (check.check_key,)
            if check.status in {"infrastructure_error", "timed_out"}:
                add(
                    HarnessFailureClass.ENVIRONMENT_ERROR,
                    source=source,
                    message="验证检查未能在可用环境中正常完成。",
                    next_step=(
                        "检查依赖、进程和超时设置，然后重新运行对应 Harness 检查。"
                    ),
                    check_ids=check_ids,
                )
            elif check.status == "blocked_by_policy":
                add(
                    HarnessFailureClass.PERMISSION_BLOCK,
                    source=source,
                    message="验证检查被 Harness 命令策略阻止。",
                    next_step="审查 Profile 中的 argv 与信任状态，再重新运行检查。",
                    check_ids=check_ids,
                )
            elif check.status == "failed":
                add(
                    HarnessFailureClass.VERIFICATION_FAILURE,
                    source=source,
                    message="真实验证检查执行完成但没有通过。",
                    next_step="修复检查暴露的问题，并在当前工作树上重新运行该检查。",
                    check_ids=check_ids,
                )
            elif check.status == "cancelled":
                add(
                    HarnessFailureClass.HUMAN_JUDGMENT_REQUIRED,
                    source=source,
                    message="验证检查被取消，当前记录无法判断代码是否正确。",
                    next_step="由用户决定继续、重新运行或接受未验证状态。",
                    check_ids=check_ids,
                )

        for evidence in run.evidence:
            if evidence.kind != "tool_execution":
                continue
            tool_name = str(evidence.summary.get("tool_name") or "tool")
            status = str(evidence.summary.get("status") or "unknown").lower()
            permission_status = str(
                evidence.summary.get("permission_status") or "not_observed"
            ).lower()
            source = f"evidence:{evidence.id}"
            evidence_ids = (evidence.id,)
            if permission_status in {"denied", "rejected", "blocked"} or (
                status == "blocked"
            ):
                add(
                    HarnessFailureClass.PERMISSION_BLOCK,
                    source=source,
                    message=f"工具 {tool_name} 因权限或策略决定未执行完成。",
                    next_step="审查该工具的权限范围；需要授权时由用户明确确认后重试。",
                    evidence_ids=evidence_ids,
                )
                continue
            if (
                tool_name == "invalid_tool_call"
                or bool(evidence.summary.get("start_missing"))
            ):
                add(
                    HarnessFailureClass.TOOL_CONTRACT_ERROR,
                    source=source,
                    message="工具调用格式无效或执行事件不完整。",
                    next_step="检查 tool schema、call id 和 start/end 事件生产链路。",
                    evidence_ids=evidence_ids,
                )
                continue
            if status == "skipped":
                add(
                    HarnessFailureClass.AGENT_REPETITION,
                    source=source,
                    message="Agent 重复无进展调用，后续工具执行被保护机制跳过。",
                    next_step="基于已有证据调整方案，不要用相同参数继续重复调用。",
                    evidence_ids=evidence_ids,
                )
            elif status in {"aborted", "error", "failed", "failure"}:
                add(
                    HarnessFailureClass.HUMAN_JUDGMENT_REQUIRED,
                    source=source,
                    message=f"工具 {tool_name} 未正常完成，但规范化证据没有记录根因类别。",
                    next_step="查看关联 ChatRun 事件，由用户确认根因后再选择修复或重试。",
                    evidence_ids=evidence_ids,
                )

        receipt = run.receipt
        if receipt is not None:
            for warning in receipt.warnings:
                warning_class = _classify_warning(warning)
                if warning_class is None:
                    continue
                message, next_step = _warning_copy(warning_class)
                add(
                    warning_class,
                    source="receipt",
                    message=message,
                    next_step=next_step,
                )

        running = run.status == "running" and receipt is None
        verified = run.status == "completed_verified" and receipt is not None
        if not running and not verified and not findings:
            add(
                HarnessFailureClass.HUMAN_JUDGMENT_REQUIRED,
                source="run",
                message="运行未验证或被阻止，但现有规范化事实不足以可靠归类。",
                next_step="请人工审查完成回执与关联 ChatRun，再决定重试或接受结果。",
            )

        ordered_findings = tuple(
            findings[failure_class]
            for failure_class in _FAILURE_PRIORITY
            if failure_class in findings
        )
        if running:
            summary = "运行仍在进行，尚未形成完成回执。"
        elif verified and not ordered_findings:
            summary = "验证完成，无已知失败。"
        elif ordered_findings:
            summary = f"发现 {len(ordered_findings)} 类需要关注的问题。"
        else:
            summary = "运行已结束。"

        return HarnessRunExplanation(
            run_id=run.id,
            status=run.status,
            objective=run.objective,
            started_at=run.started_at,
            completed_at=run.completed_at,
            verified=verified,
            running=running,
            summary=summary,
            failure_classes=tuple(
                finding.failure_class for finding in ordered_findings
            ),
            findings=ordered_findings,
            checks=tuple(
                HarnessExplainCheck(
                    id=check.check_key,
                    status=check.status,
                    duration_ms=check.duration_ms,
                )
                for check in run.checks
            ),
            evidence=tuple(
                HarnessExplainEvidence(
                    id=evidence.id,
                    kind=evidence.kind,
                    status=str(evidence.summary.get("status") or "recorded"),
                    digest_prefix=evidence.sha256[:12],
                    uri=evidence.uri,
                )
                for evidence in run.evidence
            ),
        )


def render_harness_explanation(result: HarnessExplainLookup) -> str:
    """Render one safe, scan-friendly Markdown explanation."""
    if result.status != "ok" or result.explanation is None:
        title = (
            "Harness 运行记录不可用"
            if result.status == "unavailable"
            else "没有找到 Harness 运行"
        )
        return f"## {title}\n\n{result.message}"

    explanation = result.explanation
    lines = [
        "## Harness 运行解释",
        "",
        f"- 运行：`{explanation.run_id}`",
        f"- 状态：`{explanation.status}`",
        f"- 目标：{explanation.objective}",
        f"- 开始：`{explanation.started_at}`",
        f"- 完成：`{explanation.completed_at or '-'}`",
        f"- 检查：{explanation.check_count}；证据：{explanation.evidence_count}",
        "",
        f"### 结论\n\n{explanation.summary}",
    ]
    if explanation.failure_classes:
        lines.extend(
            (
                "",
                "### 失败分类",
                "",
                *(
                    f"- `{failure_class.value}`"
                    for failure_class in explanation.failure_classes
                ),
                "",
                "### 为什么",
                "",
            )
        )
        for finding in explanation.findings:
            refs = (*finding.check_ids, *finding.evidence_ids)
            suffix = f"（关联：{', '.join(f'`{item}`' for item in refs)}）" if refs else ""
            lines.append(
                f"- **{finding.failure_class.value}**：{finding.message}{suffix}"
            )
            lines.append(f"  - 下一步：{finding.next_step}")
    else:
        lines.extend(("", "### 失败分类", "", "- 无"))

    if explanation.checks:
        lines.extend(("", "### 检查事实", ""))
        lines.extend(
            f"- `{check.id}`：`{check.status}` · {check.duration_ms}ms"
            for check in explanation.checks
        )
    if explanation.evidence:
        lines.extend(("", "### 证据索引", ""))
        lines.extend(
            (
                f"- `{evidence.id}` · `{evidence.kind}` · `{evidence.status}` · "
                f"digest `{evidence.digest_prefix}` · `{evidence.uri}`"
            )
            for evidence in explanation.evidence
        )
    return "\n".join(lines)


_FAILURE_PRIORITY = (
    HarnessFailureClass.ENVIRONMENT_ERROR,
    HarnessFailureClass.PERMISSION_BLOCK,
    HarnessFailureClass.TOOL_CONTRACT_ERROR,
    HarnessFailureClass.VERIFICATION_FAILURE,
    HarnessFailureClass.AGENT_REPETITION,
    HarnessFailureClass.IMPLEMENTATION_ERROR,
    HarnessFailureClass.AGENT_PREMATURE_FINISH,
    HarnessFailureClass.SPECIFICATION_GAP,
    HarnessFailureClass.HUMAN_JUDGMENT_REQUIRED,
)


def _classify_warning(warning: str) -> HarnessFailureClass | None:
    normalized = warning.casefold()
    if normalized.startswith("infrastructure_error:"):
        return HarnessFailureClass.ENVIRONMENT_ERROR
    if any(marker in normalized for marker in ("scope", "权限", "策略阻止")):
        return HarnessFailureClass.PERMISSION_BLOCK
    if any(marker in normalized for marker in ("状态为 failed", "检查失败")):
        return HarnessFailureClass.VERIFICATION_FAILURE
    if any(
        marker in normalized
        for marker in (
            "缺少必需检查",
            "缺少必需证据",
            "验收标准",
            "todo",
            "旧 profile",
            "不对应当前工作树",
            "尚未在完成说明中披露",
        )
    ):
        return HarnessFailureClass.AGENT_PREMATURE_FINISH
    if any(marker in normalized for marker in ("profile digest", "契约", "完成标准")):
        return HarnessFailureClass.SPECIFICATION_GAP
    return None


def _warning_copy(
    failure_class: HarnessFailureClass,
) -> tuple[str, str]:
    copy = {
        HarnessFailureClass.ENVIRONMENT_ERROR: (
            "Harness 基础设施未能完整保存或执行本次验证。",
            "检查用户状态目录、依赖和运行环境后重试。",
        ),
        HarnessFailureClass.PERMISSION_BLOCK: (
            "运行触及了范围、信任或命令策略边界。",
            "审查 Profile 和权限决定，明确授权或收窄操作范围后重试。",
        ),
        HarnessFailureClass.VERIFICATION_FAILURE: (
            "完成回执记录了真实检查失败。",
            "修复失败原因，并在未继续改动的当前工作树上重新运行检查。",
        ),
        HarnessFailureClass.AGENT_PREMATURE_FINISH: (
            "Agent 在检查、证据或任务对账不完整时尝试完成。",
            "补齐回执指出的检查、证据或 Todo 对账后重新完成。",
        ),
        HarnessFailureClass.SPECIFICATION_GAP: (
            "运行契约或 Profile 在执行期间不再一致。",
            "重新加载并确认 Profile，重建完成契约后再运行。",
        ),
    }
    return copy[failure_class]


def _merge_ids(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))


def _merge_sources(left: str, right: str) -> str:
    return ",".join(dict.fromkeys((*left.split(","), right)))


__all__ = [
    "HARNESS_EXPLAIN_RULE_VERSION",
    "HarnessExplainLookup",
    "HarnessExplainer",
    "HarnessFailureClass",
    "HarnessRunExplanation",
    "render_harness_explanation",
]
