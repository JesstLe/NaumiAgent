"""User-safe rendering for immutable Sandbox retry prune receipts."""

from __future__ import annotations

from naumi_agent.harness.store import (
    HarnessSandboxRetryPruneExecutionReceipt,
    HarnessSandboxRetryPruneReceipt,
)

_CODE_LABELS = {
    "sandbox_retry_prune_authorized": "已授权",
    "sandbox_retry_prune_already_authorized": "候选已被授权",
    "sandbox_retry_prune_candidate_drift": "候选 fence 已漂移",
    "sandbox_retry_prune_candidate_missing": "候选已不存在",
    "sandbox_retry_prune_preview_mismatch": "预览不匹配",
}

_EXECUTION_CODE_LABELS = {
    "sandbox_retry_prune_executed": "原子清理已完成",
    "sandbox_retry_prune_already_executed": "授权回执已被消费",
    "sandbox_retry_prune_authorization_missing": "授权回执不存在",
    "sandbox_retry_prune_authorization_rejected": "授权回执未获接受",
    "sandbox_retry_prune_authorization_mismatch": "授权回执参数不匹配",
    "sandbox_retry_prune_candidate_drift": "候选保护引用已漂移",
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
                "清理执行仍会重新校验此回执、dispatch fence 与全部保护引用。",
                "",
                "可复制执行命令：",
                "",
                "```text",
                (
                    "/harness eval sandbox retry-prune-execute "
                    f"{receipt.action_id} "
                    f"--receipt {receipt.receipt_id} "
                    f"--receipt-sha256 {receipt.receipt_sha256} "
                    f"--candidate {receipt.candidate_id} "
                    f"--candidate-sha256 {receipt.candidate_sha256} "
                    f"--retry-action {receipt.retry_action_id} "
                    f"--dispatch {receipt.dispatch_id} "
                    f"--refs-sha256 {receipt.protection_refs_sha256} "
                    '--reason "执行已授权的终态 retry 原子清理"'
                ),
                "```",
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


def render_sandbox_retry_prune_execution_receipt(
    receipt: HarnessSandboxRetryPruneExecutionReceipt,
) -> str:
    """Render one atomic execution outcome without exposing workspace authority."""
    executed = receipt.decision == "executed"
    headline = "已完成" if executed else "已拒绝"
    outcome = _EXECUTION_CODE_LABELS.get(receipt.code, receipt.code)
    lines = [
        f"## Sandbox retry prune 执行回执：{headline}",
        "",
        f"- 结果：`{outcome}`（`{receipt.code}`）",
        "- 授权回执："
        f"`{receipt.authorization_receipt_id}` / "
        f"`{receipt.authorization_receipt_sha256}`",
        f"- Candidate：`{receipt.candidate_id}` / `{receipt.candidate_sha256}`",
        f"- Retry/Dispatch：`{receipt.retry_action_id}` / `{receipt.dispatch_id}`",
        f"- 保护引用摘要：`{receipt.protection_refs_sha256}`",
        "- 父权限回执："
        f"`{receipt.parent_permission_receipt_id}` / "
        f"`{receipt.parent_permission_receipt_sha256}`",
        f"- 操作者：`{receipt.actor_id}`",
        f"- 原因：{receipt.reason}",
        f"- 执行时间：`{receipt.created_at}`",
        "",
    ]
    if executed:
        lines.extend(
            (
                "**原子事务已提交，没有发生部分删除。**",
                f"- 已删除 dispatch：{receipt.deleted_dispatch_count}",
                f"- 已删除 retry receipt：{receipt.deleted_retry_attempt_count}",
                f"- 已删除无共享引用的当前 ticket：{receipt.deleted_ticket_count}",
                f"- 明确保留的保护引用：{receipt.retained_reference_count}",
                (
                    "- 一次性消费 tombstone："
                    f"`{receipt.cancel_receipt_id}` → "
                    f"`{receipt.retry_receipt_id}`"
                ),
                (
                    "Request Manifest、source ticket、cancel receipt、H5a "
                    "样本与授权审计回执继续保留。"
                ),
            )
        )
    else:
        lines.extend(
            (
                "**事务未执行，删除计数均为 0。**",
                "请核对授权回执或重新生成 retention preview，禁止绕过 fence。",
            )
        )
    lines.extend(
        (
            "",
            f"Receipt：`{receipt.receipt_id}` / `{receipt.receipt_sha256}`",
        )
    )
    return "\n".join(lines)


__all__ = [
    "render_sandbox_retry_prune_execution_receipt",
    "render_sandbox_retry_prune_receipt",
]
