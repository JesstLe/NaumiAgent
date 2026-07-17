"""Shared interactive orchestration for explicit Eval Baseline promotion."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from naumi_agent.harness.eval_surface import (
    HarnessEvalPromotionFlowStatus,
    eval_promotion_flow_from_result,
)
from naumi_agent.harness.service import HarnessService
from naumi_agent.user_interaction import (
    normalize_interaction_request,
    normalize_interaction_response,
)

type InteractionCallback = Callable[[dict[str, Any]], Awaitable[Mapping[str, Any]]]
type PromotionProgressCallback = Callable[
    [HarnessEvalPromotionFlowStatus], Awaitable[None]
]

_RECOMMENDED_REASON = "用户审阅完整 Eval Batch 后确认晋升为 Active Baseline"


async def run_eval_promotion_flow(
    service: HarnessService,
    *,
    suite_id: str,
    batch_id: str,
    reason: str = "",
    interact: InteractionCallback | None = None,
    on_progress: PromotionProgressCallback | None = None,
) -> HarnessEvalPromotionFlowStatus:
    """Collect an optional reason/confirmation, then call the one H5b gate."""
    suite = str(suite_id or "").strip()
    batch = str(batch_id or "").strip()
    normalized_reason = str(reason or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", suite):
        raise ValueError("suite_id 格式无效。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", batch):
        raise ValueError("batch_id 格式无效。")
    if normalized_reason:
        result = await service.promote_eval_baseline(
            suite,
            batch,
            actor="user",
            reason=normalized_reason,
        )
        return eval_promotion_flow_from_result(result)
    if interact is None:
        raise ValueError("未提供 reason 时需要可用的用户交互通道。")

    await _notify(
        on_progress,
        HarnessEvalPromotionFlowStatus(
            stage="awaiting_reason",
            suite_id=suite,
            batch_id=batch,
            message="请选择推荐理由，或输入 3..2000 字符的自定义理由。",
        ),
    )
    response = await _ask(
        interact,
        {
            "header": "Baseline 晋升理由",
            "question": f"为什么要把 Batch `{batch}` 晋升为 Suite `{suite}` 的 Active Baseline？",
            "options": [
                {
                    "value": "recommended",
                    "label": "使用推荐理由",
                    "description": _RECOMMENDED_REASON,
                },
                {
                    "value": "cancel",
                    "label": "取消晋升",
                    "description": "不修改 Active Baseline selector。",
                },
            ],
            "allow_custom": True,
            "custom_label": "自定义理由",
        },
    )
    if response["kind"] == "option" and response["value"] == "cancel":
        return _cancelled(suite, batch, "用户在理由步骤取消晋升。")
    normalized_reason = (
        _RECOMMENDED_REASON
        if response["kind"] == "option"
        else response["custom_text"].strip()
    )
    if not 3 <= len(normalized_reason) <= 2_000:
        raise ValueError("晋升理由必须是 3..2000 个字符。")

    await _notify(
        on_progress,
        HarnessEvalPromotionFlowStatus(
            stage="awaiting_confirmation",
            suite_id=suite,
            batch_id=batch,
            promotion_reason=normalized_reason,
            message="确认后将通过 H5b gate 原子切换 Active Baseline selector。",
        ),
    )
    confirmation = await _ask(
        interact,
        {
            "header": "确认 Baseline 晋升",
            "question": (
                f"确认晋升 Suite `{suite}` / Batch `{batch}`？\n\n"
                f"理由：{normalized_reason}"
            ),
            "options": [
                {
                    "value": "confirm",
                    "label": "确认晋升",
                    "description": "执行 eligibility gate 并原子更新 selector。",
                },
                {
                    "value": "cancel",
                    "label": "取消",
                    "description": "保持当前 Active Baseline 不变。",
                },
            ],
            "allow_custom": False,
            "custom_label": "其他",
        },
    )
    if confirmation["value"] != "confirm":
        return _cancelled(suite, batch, "用户在确认步骤取消晋升。")
    result = await service.promote_eval_baseline(
        suite,
        batch,
        actor="user",
        reason=normalized_reason,
    )
    return eval_promotion_flow_from_result(result)


async def _ask(
    callback: InteractionCallback,
    payload: Mapping[str, Any],
) -> dict[str, str]:
    request = normalize_interaction_request(payload)
    raw = await callback(request.to_public_dict())
    return normalize_interaction_response(request, raw)


async def _notify(
    callback: PromotionProgressCallback | None,
    status: HarnessEvalPromotionFlowStatus,
) -> None:
    if callback is not None:
        await callback(status)


def _cancelled(
    suite_id: str,
    batch_id: str,
    message: str,
) -> HarnessEvalPromotionFlowStatus:
    return HarnessEvalPromotionFlowStatus(
        stage="cancelled",
        suite_id=suite_id,
        batch_id=batch_id,
        code="user_cancelled",
        message=message,
    )


__all__ = ["run_eval_promotion_flow"]
