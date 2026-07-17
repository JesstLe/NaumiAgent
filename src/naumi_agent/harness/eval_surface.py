"""Read-only user surface for authoritative Harness Eval baseline state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
