"""User-safe rendering for immutable Sandbox retry prune authorization receipts."""

from __future__ import annotations

from naumi_agent.harness.store import HarnessSandboxRetryPruneReceipt

_CODE_LABELS = {
    "sandbox_retry_prune_authorized": "已授权",
    "sandbox_retry_prune_already_authorized": "候选已被授权",
    "sandbox_retry_prune_candidate_drift": "候选 fence 已漂移",
    "sandbox_retry_prune_candidate_missing": "候选已不存在",
    "sandbox_retry_prune_preview_mismatch": "预览不匹配",
}


def render_sandbox_retry_prune_receipt(
    receipt: HarnessSandboxRetryPruneReceipt,
) -> str:
    """Render the authorization decision without leaking workspace authority."""
    accepted = receipt.decision == "accepted"
    headline = "已签发" if accepted else "已拒绝"
    outcome = _CODE_LABELS.get(receipt.code, receipt.code)
    lines = [
        f"## Sandbox retry prune 回执：{headline}",
        "",
        f"- 结果：`{outcome}`（`{receipt.code}`）",
        f"- Candidate：`{receipt.candidate_id}` / `{receipt.candidate_sha256}`",
        f"- Preview：`{receipt.preview_id}` / `{receipt.preview_sha256}`",
        f"- Retry/Dispatch：`{receipt.retry_action_id}` / `{receipt.dispatch_id}`",
        f"- Dispatch epoch：{receipt.dispatch_epoch}",
        f"- Dispatch request：`{receipt.dispatch_request_sha256}`",
        f"- Dispatch updated：`{receipt.dispatch_updated_at}`",
        f"- 保护引用摘要：`{receipt.protection_refs_sha256}`",
        "- 父权限回执："
        f"`{receipt.parent_permission_receipt_id}` / "
        f"`{receipt.parent_permission_receipt_sha256}`",
        f"- 操作者：`{receipt.actor_id}`",
        f"- 原因：{receipt.reason}",
        f"- 签发时间：`{receipt.created_at}`",
        "",
    ]
    if accepted:
        lines.extend(
            (
                "该回执只证明当前候选通过重新校验，**本轮没有删除任何记录**。",
                "未来清理执行必须重新校验并原子消费此回执，不能直接复用预览。",
            )
        )
    else:
        lines.extend(
            (
                "该候选未获得清理授权，所有 retry 事实保持不变。",
                "请重新生成 retention preview 后再决定是否提交新的清理意图。",
            )
        )
    lines.extend(
        (
            "",
            f"Receipt：`{receipt.receipt_id}` / `{receipt.receipt_sha256}`",
        )
    )
    return "\n".join(lines)


__all__ = ["render_sandbox_retry_prune_receipt"]
