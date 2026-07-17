"""Read-only user surface for authoritative Harness Eval baseline state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.harness.store import (
    HarnessStoredEvalBaseline,
    HarnessStoredEvalComparisonReceipt,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessEvalBaselineView(_StrictModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: int = Field(ge=1)
    batch_id: str
    sample_count: int = Field(ge=1, le=10_000)
    identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    samples_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    promoted_by: str
    promotion_reason: str
    created_at: str


class HarnessEvalComparisonView(_StrictModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_batch_id: str
    decision: Literal[
        "passed",
        "failed",
        "flaky",
        "inconclusive",
        "incompatible",
    ]
    statistical_verdict: str
    current_samples: int = Field(ge=1, le=10_000)
    created_at: str


class HarnessEvalBaselineStatus(_StrictModel):
    status: Literal["ok", "empty", "unavailable"]
    suite_id: str
    active: HarnessEvalBaselineView | None = None
    comparisons: tuple[HarnessEvalComparisonView, ...] = ()
    message: str = ""


class HarnessEvalBatchStatus(_StrictModel):
    status: Literal["completed", "partial", "error"]
    code: str = ""
    message: str = ""
    batch_id: str
    suite_id: str
    requested: int = Field(ge=5, le=100)
    completed: int = Field(ge=0, le=100)
    persisted: int = Field(ge=0, le=100)
    passed_cases: int = Field(default=0, ge=0)
    implementation_failures: int = Field(default=0, ge=0)
    evaluation_errors: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    duration_ms: float = Field(ge=0)
    baseline_eligible: bool = False
    identity_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")


class HarnessEvalPromotionStatus(_StrictModel):
    status: Literal["promoted", "already_active", "not_selected", "error"]
    code: str = ""
    message: str = ""
    suite_id: str
    batch_id: str
    baseline_id: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    active_baseline_id: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    previous_baseline_id: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    version: int = Field(default=0, ge=0)
    sample_count: int = Field(default=0, ge=0, le=10_000)
    promoted_by: str = ""
    promotion_reason: str = ""
    created_at: str = ""


class HarnessEvalComparisonRunStatus(_StrictModel):
    status: Literal["created", "existing", "stale_baseline", "error"]
    code: str = ""
    message: str = ""
    suite_id: str
    baseline_id: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    baseline_version: int = Field(default=0, ge=0)
    baseline_batch_id: str = ""
    candidate_batch_id: str
    baseline_samples: int = Field(default=0, ge=0, le=10_000)
    candidate_samples: int = Field(default=0, ge=0, le=10_000)
    receipt_id: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    decision: Literal[
        "",
        "passed",
        "failed",
        "flaky",
        "inconclusive",
        "incompatible",
    ] = ""
    statistical_verdict: str = ""
    policy_failed_samples: int = Field(default=0, ge=0, le=10_000)
    policy_inconclusive_samples: int = Field(default=0, ge=0, le=10_000)
    created_at: str = ""

    @model_validator(mode="after")
    def _authoritative_fields_match_status(self) -> HarnessEvalComparisonRunStatus:
        if self.status == "error":
            return self
        if (
            not self.baseline_id
            or self.baseline_version < 1
            or not self.baseline_batch_id
            or self.baseline_samples < 1
            or self.candidate_samples < 1
            or not self.receipt_id
            or not self.decision
            or not self.statistical_verdict
            or not self.created_at
        ):
            raise ValueError("非错误 Comparison 状态缺少权威 receipt 字段。")
        return self


def build_eval_baseline_status(
    suite_id: str,
    active: HarnessStoredEvalBaseline | None,
    comparisons: tuple[HarnessStoredEvalComparisonReceipt, ...],
) -> HarnessEvalBaselineStatus:
    if active is None:
        return HarnessEvalBaselineStatus(
            status="empty",
            suite_id=suite_id,
            message="当前 Suite 尚未晋升 Baseline。",
        )
    return HarnessEvalBaselineStatus(
        status="ok",
        suite_id=suite_id,
        active=HarnessEvalBaselineView(
            id=active.id,
            version=active.version,
            batch_id=active.batch_id,
            sample_count=active.sample_count,
            identity_sha256=active.identity_sha256,
            samples_sha256=active.samples_sha256,
            promoted_by=active.promoted_by,
            promotion_reason=active.promotion_reason,
            created_at=active.created_at,
        ),
        comparisons=tuple(
            HarnessEvalComparisonView(
                id=item.id,
                baseline_id=item.baseline_id,
                current_batch_id=item.current_batch_id,
                decision=item.decision,
                statistical_verdict=item.receipt.statistical_verdict.value,
                current_samples=item.receipt.current_samples,
                created_at=item.created_at,
            )
            for item in comparisons
        ),
    )


def render_eval_baseline_status(result: HarnessEvalBaselineStatus) -> str:
    lines = ["## Harness Eval Baseline", "", f"- Suite：`{result.suite_id}`"]
    if result.status == "unavailable":
        lines.extend(
            [
                "- 状态：不可用",
                f"- 原因：{result.message}",
                "- 下一步：运行 `/harness doctor` 检查用户状态目录。",
            ]
        )
        return "\n".join(lines)
    if result.status == "empty" or result.active is None:
        lines.extend(
            [
                "- 状态：尚无 Baseline",
                f"- 说明：{result.message}",
                "- 下一步：先生成稳定的重复 Eval cohort，再显式晋升。",
            ]
        )
        return "\n".join(lines)

    active = result.active
    lines.extend(
        [
            f"- 状态：已选择 v{active.version}",
            f"- Batch：`{active.batch_id}` · 样本 {active.sample_count}",
            f"- Baseline：`{active.id[:12]}` · Identity `{active.identity_sha256[:12]}`",
            f"- 晋升：{active.promoted_by} · {active.created_at}",
            f"- 原因：{active.promotion_reason}",
            "",
            "### 最近比较",
            "",
        ]
    )
    if not result.comparisons:
        lines.append("- 尚无引用此 Suite 的 Comparison receipt。")
        return "\n".join(lines)
    labels = {
        "passed": "通过",
        "failed": "未通过",
        "flaky": "波动",
        "inconclusive": "无法判断",
        "incompatible": "不可比较",
    }
    for item in result.comparisons:
        lines.append(
            f"- {labels[item.decision]} · Candidate `{item.current_batch_id}` · "
            f"统计 `{item.statistical_verdict}` · 样本 {item.current_samples} · "
            f"Receipt `{item.id[:12]}`"
        )
    return "\n".join(lines)


def render_eval_batch_status(result: HarnessEvalBatchStatus) -> str:
    lines = [
        "## Harness 重复 Eval Batch",
        "",
        f"- Batch：`{result.batch_id}`",
        f"- Suite：`{result.suite_id}`",
        f"- 进度：完成 {result.completed}/{result.requested} · 已保存 {result.persisted}",
        (
            f"- Case 汇总：通过 {result.passed_cases} · 实现回归 "
            f"{result.implementation_failures} · 评测错误 {result.evaluation_errors} · "
            f"跳过 {result.skipped}"
        ),
    ]
    if result.status == "error":
        lines.extend(
            [
                "- 状态：未完成",
                f"- 原因 `{result.code}`：{result.message}",
                "- 下一步：修复参数、Suite 或状态库后使用新的 batch id 重试。",
            ]
        )
        return "\n".join(lines)
    if result.status == "partial":
        lines.extend(
            [
                "- 状态：部分完成（预算耗尽）",
                "- 说明：已完成样本保留，但不能晋升或形成统计结论。",
            ]
        )
    else:
        lines.append("- 状态：重复评测完成")
    promotion = "可晋升" if result.baseline_eligible else "不可晋升"
    identity = result.identity_sha256[:12] if result.identity_sha256 else "不可用"
    lines.extend(
        [
            f"- Identity：`{identity}` · {promotion}",
            f"- 耗时：{result.duration_ms:.1f}ms",
            f"- 下一步：运行 `/harness baseline {result.suite_id}` 查看当前 Baseline。",
        ]
    )
    return "\n".join(lines)


def render_eval_promotion_status(result: HarnessEvalPromotionStatus) -> str:
    lines = [
        "## Harness Eval Baseline 晋升",
        "",
        f"- Suite：`{result.suite_id}`",
        f"- Batch：`{result.batch_id}`",
    ]
    if result.status == "error":
        lines.extend(
            [
                "- 状态：晋升被拒绝",
                f"- 原因 `{result.code}`：{result.message}",
                "- Selector：未改变",
            ]
        )
        return "\n".join(lines)
    if result.status == "not_selected":
        lines.extend(
            [
                "- 状态：该 Batch 已是历史 Baseline，未回拨 active selector",
                f"- 历史版本：v{result.version} · `{result.baseline_id[:12]}`",
                f"- 当前 Active：`{result.active_baseline_id[:12]}`",
            ]
        )
        return "\n".join(lines)
    state = "晋升完成" if result.status == "promoted" else "已是 Active，无需重复晋升"
    lines.extend(
        [
            f"- 状态：{state}",
            f"- 版本：v{result.version} · 样本 {result.sample_count}",
            f"- Baseline：`{result.baseline_id[:12]}`",
            f"- 操作者：{result.promoted_by} · {result.created_at}",
            f"- 原因：{result.promotion_reason}",
        ]
    )
    if result.previous_baseline_id:
        lines.append(f"- 上一版本：`{result.previous_baseline_id[:12]}`")
    lines.append(
        f"- 下一步：运行 `/harness baseline {result.suite_id}` 查看权威状态。"
    )
    return "\n".join(lines)


def render_eval_comparison_run_status(
    result: HarnessEvalComparisonRunStatus,
) -> str:
    lines = [
        "## Harness Eval Comparison",
        "",
        f"- Suite：`{result.suite_id}`",
        f"- Candidate：`{result.candidate_batch_id}`",
    ]
    if result.status == "error":
        lines.extend(
            [
                "- 状态：比较未创建",
                f"- 原因 `{result.code}`：{result.message}",
                "- Receipt：未写入",
            ]
        )
        return "\n".join(lines)
    labels = {
        "passed": "通过",
        "failed": "未通过",
        "flaky": "波动",
        "inconclusive": "无法判断",
        "incompatible": "不可比较",
    }
    state = {
        "created": "已创建权威回执",
        "existing": "已有相同权威回执",
        "stale_baseline": "回执已保存，但 active Baseline 已变化",
    }[result.status]
    lines.extend(
        [
            f"- 状态：{state}",
            f"- 结论：{labels[result.decision]}",
            (
                f"- Baseline：v{result.baseline_version} `{result.baseline_batch_id}` · "
                f"样本 {result.baseline_samples}"
            ),
            f"- Candidate 样本：{result.candidate_samples}",
            f"- 统计：`{result.statistical_verdict}`",
            (
                f"- Policy：失败样本 {result.policy_failed_samples} · "
                f"证据不足样本 {result.policy_inconclusive_samples}"
            ),
            f"- Receipt：`{result.receipt_id[:12]}` · {result.created_at}",
        ]
    )
    if result.status == "stale_baseline":
        lines.append("- 下一步：重新读取 active Baseline，再决定是否重新比较。")
    else:
        lines.append(
            f"- 下一步：运行 `/harness baseline {result.suite_id}` 查看权威状态。"
        )
    return "\n".join(lines)
