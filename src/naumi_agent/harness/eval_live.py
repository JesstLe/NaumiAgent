"""Bounded live model transport evaluation for Harness."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.harness.eval_identity import (
    HarnessEvalModelIdentity,
    build_eval_model_identity,
)
from naumi_agent.model.router import ModelContractStatus, ModelResponse
from naumi_agent.runtime.ports.model import ModelPort

LIVE_EVAL_RUNNER_VERSION = "live_transport_echo@1"
LIVE_EVAL_PROMPT_VERSION = "naumi_live_echo@1"
_REQUEST_ID_RE = re.compile(r"hlive_[0-9a-f]{24}\Z")
_SAFE_PROVIDER_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}\Z")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessLiveEvalStatus(StrEnum):
    PASSED = "passed"
    IMPLEMENTATION_FAILURE = "implementation_failure"
    EVALUATION_ERROR = "evaluation_error"
    PARTIAL = "partial"


class HarnessLiveEvalError(RuntimeError):
    """Stable failure raised before a typed Live Eval receipt can be created."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HarnessLiveEvalRequest(_StrictModel):
    """Explicit live invocation and its hard local execution budgets."""

    schema_version: Literal[1] = 1
    live: Literal[True]
    model: str = Field(min_length=1, max_length=512)
    max_duration_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    max_cost_usd: float = Field(default=0.05, gt=0.0, le=10.0)
    max_output_tokens: int = Field(default=32, ge=1, le=64)
    runner_version: Literal["live_transport_echo@1"] = LIVE_EVAL_RUNNER_VERSION
    prompt_version: Literal["naumi_live_echo@1"] = LIVE_EVAL_PROMPT_VERSION

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("model 不能包含控制字符。")
        return normalized

    @field_validator("max_duration_seconds", "max_cost_usd", mode="before")
    @classmethod
    def _reject_boolean_float(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Live Eval 预算不得使用布尔值。")
        return value

    @field_validator("max_output_tokens", mode="before")
    @classmethod
    def _reject_boolean_integer(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("max_output_tokens 不得使用布尔值。")
        return value

    @property
    def request_sha256(self) -> str:
        return _sha256_payload(self.model_dump(mode="json"))


class HarnessLiveEvalReceipt(_StrictModel):
    """Tamper-evident, output-free evidence for one bounded live request."""

    schema_version: Literal[1] = 1
    request_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    status: HarnessLiveEvalStatus
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    message: str = Field(min_length=1, max_length=500)
    model: HarnessEvalModelIdentity | None = None
    provider_model: str = Field(default="", max_length=512)
    finish_reason: str = Field(default="", max_length=64)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0, le=10_000.0)
    cost_source: Literal[
        "rate_card_estimate", "provider_billing", "unavailable"
    ] = "unavailable"
    rate_card_source: Literal[
        "catalog", "config", "litellm", "mixed", "fallback", "unavailable"
    ] = "unavailable"
    billing_status: Literal[
        "supported", "unsupported", "unavailable"
    ] = "unavailable"
    usage_source: Literal["transport_response", "unavailable"] = "unavailable"
    provider_response_id_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    duration_ms: float = Field(default=0.0, ge=0.0)
    max_duration_seconds: float = Field(ge=1.0, le=120.0)
    max_cost_usd: float = Field(gt=0.0, le=10.0)
    max_output_tokens: int = Field(ge=1, le=64)
    preflight_max_cost_usd: float = Field(default=0.0, ge=0.0, le=10_000.0)
    provider_call_attempted: bool = False
    response_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    exact_match: bool = False
    persisted: Literal[False] = False
    baseline_eligible: Literal[False] = False
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("request_id")
    @classmethod
    def _valid_request_id(cls, value: str) -> str:
        if not _REQUEST_ID_RE.fullmatch(value):
            raise ValueError("Live Eval request_id 格式无效。")
        return value

    @field_validator("created_at")
    @classmethod
    def _canonical_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Live Eval created_at 无效。") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Live Eval created_at 必须包含 UTC offset。")
        canonical = parsed.astimezone(UTC).isoformat(timespec="seconds")
        if canonical != value:
            raise ValueError("Live Eval created_at 不是 canonical UTC 时间。")
        return value

    @field_validator("provider_model")
    @classmethod
    def _safe_provider_model(cls, value: str) -> str:
        if value and not _SAFE_PROVIDER_MODEL_RE.fullmatch(value):
            raise ValueError("provider_model 包含不安全字符。")
        return value

    @field_validator(
        "cost_usd",
        "duration_ms",
        "max_duration_seconds",
        "max_cost_usd",
        "preflight_max_cost_usd",
        mode="before",
    )
    @classmethod
    def _finite_numbers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Live Eval 数值字段不得使用布尔值。")
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            raise ValueError("Live Eval 数值字段必须有限。")
        return value

    @model_validator(mode="after")
    def _receipt_is_self_consistent(self) -> Self:
        expected = _sha256_payload(self.model_dump(mode="json", exclude={"receipt_sha256"}))
        if not hmac.compare_digest(expected, self.receipt_sha256):
            raise ValueError("Live Eval receipt_sha256 与回执内容不一致。")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("Live Eval total_tokens 与输入输出用量不一致。")
        if self.status is HarnessLiveEvalStatus.PASSED and (
            not self.exact_match
            or not self.provider_call_attempted
            or not self.response_sha256
            or not self.provider_model
            or self.cost_usd > self.max_cost_usd
        ):
            raise ValueError("通过的 Live Eval 回执缺少完整成功证据。")
        if self.exact_match and not self.response_sha256:
            raise ValueError("exact_match 必须绑定 response digest。")
        if self.cost_source == "rate_card_estimate" and (
            self.usage_source != "transport_response"
            or self.rate_card_source == "unavailable"
        ):
            raise ValueError("Live Eval rate-card 估算缺少用量或单价来源。")
        if self.cost_source != "rate_card_estimate" and self.rate_card_source != "unavailable":
            raise ValueError("非 rate-card 成本不得声明单价来源。")
        if (self.cost_source == "provider_billing") != (
            self.billing_status == "supported"
        ):
            raise ValueError("provider_billing 与 supported 账单状态必须同时出现。")
        if self.provider_response_id_sha256 and not self.provider_call_attempted:
            raise ValueError("Provider response id 必须绑定已尝试调用。")
        if self.status is HarnessLiveEvalStatus.PASSED and (
            self.usage_source != "transport_response"
            or self.cost_source == "unavailable"
            or self.billing_status == "unavailable"
            or self.rate_card_source == "fallback"
        ):
            raise ValueError("通过的 Live Eval 回执缺少调用证据来源。")
        return self


class HarnessLiveEvalRunner:
    """Execute one fixed, no-tool live transport challenge under bounded budgets."""

    def __init__(
        self,
        model_port: ModelPort,
        *,
        request_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._model_port = model_port
        self._request_id_factory = request_id_factory or (lambda: f"hlive_{secrets.token_hex(12)}")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.perf_counter

    async def run(self, request: HarnessLiveEvalRequest) -> HarnessLiveEvalReceipt:
        request_id = self._request_id_factory()
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            raise HarnessLiveEvalError(
                "live_request_id_invalid",
                "Live Eval 无法生成安全请求身份。",
            )
        created_at = _utc_timestamp(self._clock())
        started = self._monotonic()

        try:
            capability_before = self._model_port.get_model_capability_contract(request.model)
            reasoning_before = self._model_port.get_reasoning_effort_status(request.model)
            model_identity = build_eval_model_identity(
                capability_before,
                reasoning_before,
            )
        except Exception as exc:
            raise HarnessLiveEvalError(
                "live_model_contract_invalid",
                "Live Eval 无法建立模型能力与思考强度身份。",
            ) from exc

        if capability_before.status is ModelContractStatus.INCOMPATIBLE:
            return _receipt(
                request=request,
                request_id=request_id,
                created_at=created_at,
                status=HarnessLiveEvalStatus.EVALUATION_ERROR,
                code="model_incompatible",
                message="模型能力合同明确与 Agent Harness 不兼容，未发送请求。",
                model=model_identity,
                duration_ms=_elapsed_ms(self._monotonic(), started),
            )

        cost_sources = {
            capability_before.field_sources.get("input_cost_per_million"),
            capability_before.field_sources.get("output_cost_per_million"),
        }
        if None in cost_sources or "fallback" in cost_sources:
            return _receipt(
                request=request,
                request_id=request_id,
                created_at=created_at,
                status=HarnessLiveEvalStatus.EVALUATION_ERROR,
                code="cost_contract_unverified",
                message="模型价格来源未验证，无法执行有成本上限的 Live Eval。",
                model=model_identity,
                duration_ms=_elapsed_ms(self._monotonic(), started),
            )

        challenge = f"NAUMI_LIVE_OK_{request_id.removeprefix('hlive_').upper()}"
        messages = _challenge_messages(challenge)
        input_token_ceiling = (
            sum(len(str(message["content"]).encode("utf-8")) for message in messages) + 256
        )
        preflight_max_cost = _maximum_request_cost(
            input_tokens=input_token_ceiling,
            output_tokens=request.max_output_tokens,
            input_cost_per_million=capability_before.input_cost_per_million,
            output_cost_per_million=capability_before.output_cost_per_million,
        )
        if preflight_max_cost > request.max_cost_usd:
            return _receipt(
                request=request,
                request_id=request_id,
                created_at=created_at,
                status=HarnessLiveEvalStatus.EVALUATION_ERROR,
                code="preflight_cost_exceeded",
                message=(
                    "Live Eval 最坏成本估算超过本次预算，未发送请求；"
                    "请提高 max_cost_usd 或选择更低成本模型。"
                ),
                model=model_identity,
                duration_ms=_elapsed_ms(self._monotonic(), started),
                preflight_max_cost_usd=preflight_max_cost,
            )

        try:
            async with asyncio.timeout(request.max_duration_seconds):
                response = await self._model_port.call(
                    messages,
                    model=request.model,
                    tools=None,
                    max_tokens=request.max_output_tokens,
                    temperature=0.0,
                )
        except TimeoutError:
            return _receipt(
                request=request,
                request_id=request_id,
                created_at=created_at,
                status=HarnessLiveEvalStatus.PARTIAL,
                code="deadline_exceeded",
                message=(
                    "Live Eval 已达到本地时限并取消等待；Provider 是否已停止计费需由其账单确认。"
                ),
                model=model_identity,
                duration_ms=_elapsed_ms(self._monotonic(), started),
                preflight_max_cost_usd=preflight_max_cost,
                provider_call_attempted=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return _receipt(
                request=request,
                request_id=request_id,
                created_at=created_at,
                status=HarnessLiveEvalStatus.EVALUATION_ERROR,
                code="provider_request_failed",
                message="Provider 请求失败；私有异常未写入 Live Eval 回执。",
                model=model_identity,
                duration_ms=_elapsed_ms(self._monotonic(), started),
                preflight_max_cost_usd=preflight_max_cost,
                provider_call_attempted=True,
            )

        duration_ms = _elapsed_ms(self._monotonic(), started)
        usage_error = _usage_error(response)
        provider_model = _provider_model(response.provider_model)
        content_invalid = not isinstance(response.content, str)
        response_text = response.content.strip() if not content_invalid else ""
        response_sha256 = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
        exact_match = hmac.compare_digest(response_text, challenge)

        try:
            capability_after = self._model_port.get_model_capability_contract(request.model)
            reasoning_after = self._model_port.get_reasoning_effort_status(request.model)
            model_after = build_eval_model_identity(capability_after, reasoning_after)
        except Exception:
            capability_after = None
            model_after = None

        common = {
            "request": request,
            "request_id": request_id,
            "created_at": created_at,
            "model": model_identity,
            "provider_model": provider_model,
            "finish_reason": _bounded_finish_reason(response.finish_reason),
            "input_tokens": _nonnegative_int(response.usage.input_tokens),
            "output_tokens": _nonnegative_int(response.usage.output_tokens),
            "total_tokens": _nonnegative_int(response.usage.input_tokens)
            + _nonnegative_int(response.usage.output_tokens),
            "cost_usd": _nonnegative_float(response.usage.cost_usd),
            "cost_source": response.call_evidence.cost_source,
            "rate_card_source": response.call_evidence.rate_card_source,
            "billing_status": response.call_evidence.billing_status,
            "usage_source": response.call_evidence.usage_source,
            "provider_response_id_sha256": (
                response.call_evidence.provider_response_id_sha256
            ),
            "duration_ms": duration_ms,
            "preflight_max_cost_usd": preflight_max_cost,
            "provider_call_attempted": True,
            "response_sha256": response_sha256,
            "exact_match": exact_match,
        }
        if model_after is None or model_after != model_identity:
            return _receipt(
                **common,
                status=HarnessLiveEvalStatus.PARTIAL,
                code="model_contract_drift",
                message="请求前后的模型能力或思考强度身份不一致，结果不可比较。",
            )
        if usage_error:
            return _receipt(
                **common,
                status=HarnessLiveEvalStatus.PARTIAL,
                code=usage_error,
                message="Provider 未返回可信完整用量，结果不可进入成本比较。",
            )
        if response.call_evidence.usage_source != "transport_response":
            return _receipt(
                **common,
                status=HarnessLiveEvalStatus.PARTIAL,
                code="usage_provenance_unavailable",
                message="模型适配器未证明 token 用量来自 transport response。",
            )
        if response.call_evidence.cost_source == "unavailable":
            return _receipt(
                **common,
                status=HarnessLiveEvalStatus.PARTIAL,
                code="cost_provenance_unavailable",
                message="模型适配器未声明成本证据来源。",
            )
        if (
            response.call_evidence.cost_source == "rate_card_estimate"
            and response.call_evidence.rate_card_source in {"fallback", "unavailable"}
        ):
            return _receipt(
                **common,
                status=HarnessLiveEvalStatus.PARTIAL,
                code="cost_rate_source_unverified",
                message="模型适配器使用未验证单价来源，成本估算不可进入 Live Eval。",
            )
        if content_invalid:
            return _receipt(
                **common,
                status=HarnessLiveEvalStatus.PARTIAL,
                code="response_content_invalid",
                message="Provider 响应正文不是有效文本，结果不可验证。",
            )
        if response.usage.cost_usd > request.max_cost_usd:
            return _receipt(
                **common,
                status=HarnessLiveEvalStatus.PARTIAL,
                code="observed_cost_exceeded",
                message="已记录成本超过本次预算，已停止后续调用；其来源见成本证据。",
            )
        if not provider_model:
            return _receipt(
                **common,
                status=HarnessLiveEvalStatus.PARTIAL,
                code="provider_identity_missing",
                message="Provider 响应未返回安全可记录的模型身份。",
            )
        if response.finish_reason not in {"stop", "end_turn"}:
            return _receipt(
                **common,
                status=HarnessLiveEvalStatus.PARTIAL,
                code="response_not_terminal",
                message="Provider 响应没有以正常终止原因结束。",
            )
        if not exact_match:
            return _receipt(
                **common,
                status=HarnessLiveEvalStatus.IMPLEMENTATION_FAILURE,
                code="challenge_mismatch",
                message="模型完成了请求，但没有精确返回固定挑战文本。",
            )
        return _receipt(
            **common,
            status=HarnessLiveEvalStatus.PASSED,
            code="live_transport_verified",
            message="Live 模型传输、身份、用量和固定挑战均已验证。",
        )


def render_harness_live_eval(receipt: HarnessLiveEvalReceipt) -> str:
    """Render a bounded Chinese receipt shared by every terminal surface."""
    tones = {
        HarnessLiveEvalStatus.PASSED: "通过",
        HarnessLiveEvalStatus.IMPLEMENTATION_FAILURE: "实现失败",
        HarnessLiveEvalStatus.EVALUATION_ERROR: "评测错误",
        HarnessLiveEvalStatus.PARTIAL: "部分完成",
    }
    lines = [
        "# Harness Live Eval",
        "",
        f"- 状态：{tones[receipt.status]}（`{receipt.code}`）",
        f"- 请求：`{receipt.request_id}`",
        f"- 说明：{receipt.message}",
    ]
    if receipt.model is not None:
        lines.extend(
            [
                f"- 模型：`{receipt.model.requested_model}`",
                f"- Provider：`{receipt.model.provider}` / `{receipt.model.api_format}`",
                f"- 能力合同：`{receipt.model.capability_status}` / "
                f"`{receipt.model.capability_sha256[:12]}…`",
                f"- 思考强度：`{receipt.model.reasoning_effort}` "
                f"（{receipt.model.reasoning_source}）",
            ]
        )
    if receipt.provider_model:
        lines.append(f"- Provider 返回模型：`{receipt.provider_model}`")
    lines.extend(
        [
            f"- 用量：{receipt.input_tokens} 输入 / {receipt.output_tokens} 输出 / "
            f"{receipt.total_tokens} 合计 token",
            f"- 成本：${receipt.cost_usd:.6f} / 上限 ${receipt.max_cost_usd:.6f} "
            f"（{_cost_source_label(receipt.cost_source)}）",
            f"- 单价来源：{_rate_card_source_label(receipt.rate_card_source)}",
            f"- Provider 账单：{_billing_status_label(receipt.billing_status)}",
            "- Provider Response ID："
            + (
                f"`{receipt.provider_response_id_sha256[:12]}…`（仅保存摘要）"
                if receipt.provider_response_id_sha256
                else "不可用"
            ),
            f"- 耗时：{receipt.duration_ms:.0f}ms / 上限 {receipt.max_duration_seconds:.1f}s",
            f"- 响应摘要：`{receipt.response_sha256[:12]}…`"
            if receipt.response_sha256
            else "- 响应摘要：不可用",
            "- 持久化：否；本切片不会自动成为 Baseline",
        ]
    )
    return "\n".join(lines)


def _receipt(
    *,
    request: HarnessLiveEvalRequest,
    request_id: str,
    created_at: str,
    status: HarnessLiveEvalStatus,
    code: str,
    message: str,
    model: HarnessEvalModelIdentity | None,
    provider_model: str = "",
    finish_reason: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    cost_usd: float = 0.0,
    cost_source: str = "unavailable",
    rate_card_source: str = "unavailable",
    billing_status: str = "unavailable",
    usage_source: str = "unavailable",
    provider_response_id_sha256: str = "",
    duration_ms: float = 0.0,
    preflight_max_cost_usd: float = 0.0,
    provider_call_attempted: bool = False,
    response_sha256: str = "",
    exact_match: bool = False,
) -> HarnessLiveEvalReceipt:
    raw = {
        "schema_version": 1,
        "request_id": request_id,
        "request_sha256": request.request_sha256,
        "created_at": created_at,
        "status": status,
        "code": code,
        "message": message,
        "model": model.model_dump(mode="json") if model is not None else None,
        "provider_model": provider_model,
        "finish_reason": finish_reason,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": round(cost_usd, 9),
        "cost_source": cost_source,
        "rate_card_source": rate_card_source,
        "billing_status": billing_status,
        "usage_source": usage_source,
        "provider_response_id_sha256": provider_response_id_sha256,
        "duration_ms": round(duration_ms, 3),
        "max_duration_seconds": request.max_duration_seconds,
        "max_cost_usd": request.max_cost_usd,
        "max_output_tokens": request.max_output_tokens,
        "preflight_max_cost_usd": round(preflight_max_cost_usd, 9),
        "provider_call_attempted": provider_call_attempted,
        "response_sha256": response_sha256,
        "exact_match": exact_match,
        "persisted": False,
        "baseline_eligible": False,
    }
    return HarnessLiveEvalReceipt.model_validate({**raw, "receipt_sha256": _sha256_payload(raw)})


def _cost_source_label(value: str) -> str:
    return {
        "rate_card_estimate": "能力单价估算",
        "provider_billing": "Provider 账单",
        "unavailable": "来源不可用",
    }.get(value, "来源不可用")


def _rate_card_source_label(value: str) -> str:
    return {
        "catalog": "Provider catalog",
        "config": "用户模型配置",
        "litellm": "LiteLLM 元数据",
        "mixed": "混合来源",
        "fallback": "未验证 fallback",
        "unavailable": "不可用",
    }.get(value, "不可用")


def _billing_status_label(value: str) -> str:
    return {
        "supported": "已取得 Provider 账单证据",
        "unsupported": "当前适配器未集成账单 API",
        "unavailable": "状态不可用",
    }.get(value, "状态不可用")


def _challenge_messages(challenge: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a transport verifier. Return only the exact ASCII token "
                "requested by the user. Do not add markdown or explanation."
            ),
        },
        {"role": "user", "content": f"Return exactly: {challenge}"},
    ]


def _maximum_request_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    input_cost_per_million: float,
    output_cost_per_million: float,
) -> float:
    values = (
        input_cost_per_million,
        output_cost_per_million,
    )
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise HarnessLiveEvalError(
            "live_cost_contract_invalid",
            "Live Eval 模型价格合同包含无效数值。",
        )
    return (
        input_tokens * input_cost_per_million + output_tokens * output_cost_per_million
    ) / 1_000_000


def _usage_error(response: ModelResponse) -> str:
    usage = response.usage
    token_counts = (usage.input_tokens, usage.output_tokens, usage.total_tokens)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in token_counts):
        return "usage_invalid"
    if any(value < 0 for value in token_counts):
        return "usage_invalid"
    cost = usage.cost_usd
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return "usage_invalid"
    if not math.isfinite(float(cost)) or cost < 0:
        return "usage_invalid"
    if usage.total_tokens != usage.input_tokens + usage.output_tokens:
        return "usage_inconsistent"
    if usage.total_tokens == 0:
        return "usage_missing"
    return ""


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _nonnegative_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        return 0.0
    return converted


def _provider_model(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    return normalized if _SAFE_PROVIDER_MODEL_RE.fullmatch(normalized) else ""


def _bounded_finish_reason(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        return ""
    return normalized


def _utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise HarnessLiveEvalError(
            "live_clock_invalid",
            "Live Eval authority clock 必须包含 UTC offset。",
        )
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _elapsed_ms(now: float, started: float) -> float:
    if not all(math.isfinite(value) for value in (now, started)):
        return 0.0
    return max(0.0, (now - started) * 1_000)


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "LIVE_EVAL_PROMPT_VERSION",
    "LIVE_EVAL_RUNNER_VERSION",
    "HarnessLiveEvalError",
    "HarnessLiveEvalReceipt",
    "HarnessLiveEvalRequest",
    "HarnessLiveEvalRunner",
    "HarnessLiveEvalStatus",
    "render_harness_live_eval",
]
