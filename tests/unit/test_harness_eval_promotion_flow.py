from __future__ import annotations

from typing import Any

import pytest

from naumi_agent.harness.eval_promotion_flow import run_eval_promotion_flow
from naumi_agent.harness.eval_surface import HarnessEvalPromotionStatus


class _PromotionService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def promote_eval_baseline(
        self,
        suite_id: str,
        batch_id: str,
        *,
        actor: str,
        reason: str,
    ) -> HarnessEvalPromotionStatus:
        self.calls.append(
            {
                "suite_id": suite_id,
                "batch_id": batch_id,
                "actor": actor,
                "reason": reason,
            }
        )
        return HarnessEvalPromotionStatus(
            status="promoted",
            suite_id=suite_id,
            batch_id=batch_id,
            baseline_id="a" * 64,
            active_baseline_id="a" * 64,
            version=1,
            sample_count=5,
            promoted_by=actor,
            promotion_reason=reason,
            created_at="2026-07-18T10:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_guided_promotion_uses_recommended_reason_and_confirms_once() -> None:
    service = _PromotionService()
    requests: list[dict[str, Any]] = []
    stages: list[str] = []

    async def interact(payload: dict[str, Any]) -> dict[str, str]:
        requests.append(payload)
        value = "recommended" if len(requests) == 1 else "confirm"
        return {"kind": "option", "value": value}

    async def progress(status: Any) -> None:
        stages.append(status.stage)

    result = await run_eval_promotion_flow(  # type: ignore[arg-type]
        service,
        suite_id="surface-protocol",
        batch_id="candidate-1",
        interact=interact,
        on_progress=progress,
    )

    assert stages == ["awaiting_reason", "awaiting_confirmation"]
    assert [request["allow_custom"] for request in requests] == [True, False]
    assert result.stage == "promoted" and result.terminal is True
    assert service.calls == [
        {
            "suite_id": "surface-protocol",
            "batch_id": "candidate-1",
            "actor": "user",
            "reason": "用户审阅完整 Eval Batch 后确认晋升为 Active Baseline",
        }
    ]


@pytest.mark.asyncio
async def test_guided_promotion_accepts_custom_reason_and_can_cancel_confirmation() -> None:
    service = _PromotionService()
    responses = iter(
        [
            {"kind": "custom", "custom_text": "真实回归场景已经全部通过"},
            {"kind": "option", "value": "cancel"},
        ]
    )

    async def interact(_: dict[str, Any]) -> dict[str, str]:
        return next(responses)

    result = await run_eval_promotion_flow(  # type: ignore[arg-type]
        service,
        suite_id="surface-protocol",
        batch_id="candidate-1",
        interact=interact,
    )

    assert result.stage == "cancelled"
    assert result.code == "user_cancelled"
    assert service.calls == []


@pytest.mark.asyncio
async def test_guided_promotion_cancel_reason_and_invalid_custom_never_mutate() -> None:
    service = _PromotionService()

    async def cancel(_: dict[str, Any]) -> dict[str, str]:
        return {"kind": "option", "value": "cancel"}

    cancelled = await run_eval_promotion_flow(  # type: ignore[arg-type]
        service,
        suite_id="surface-protocol",
        batch_id="candidate-1",
        interact=cancel,
    )

    async def invalid(_: dict[str, Any]) -> dict[str, str]:
        return {"kind": "custom", "custom_text": "短"}

    with pytest.raises(ValueError, match="3..2000"):
        await run_eval_promotion_flow(  # type: ignore[arg-type]
            service,
            suite_id="surface-protocol",
            batch_id="candidate-1",
            interact=invalid,
        )
    assert cancelled.stage == "cancelled"
    assert service.calls == []


@pytest.mark.asyncio
async def test_explicit_reason_skips_interaction() -> None:
    service = _PromotionService()

    async def unexpected(_: dict[str, Any]) -> dict[str, str]:
        raise AssertionError("显式理由不应触发交互")

    result = await run_eval_promotion_flow(  # type: ignore[arg-type]
        service,
        suite_id="surface-protocol",
        batch_id="candidate-1",
        reason="用户已经显式确认晋升",
        interact=unexpected,
    )

    assert result.stage == "promoted"
    assert service.calls[0]["reason"] == "用户已经显式确认晋升"


@pytest.mark.asyncio
async def test_invalid_identity_is_rejected_before_interaction() -> None:
    service = _PromotionService()

    async def unexpected(_: dict[str, Any]) -> dict[str, str]:
        raise AssertionError("非法标识不应触发交互")

    with pytest.raises(ValueError, match="suite_id"):
        await run_eval_promotion_flow(  # type: ignore[arg-type]
            service,
            suite_id="../other",
            batch_id="candidate-1",
            interact=unexpected,
        )
    assert service.calls == []
