"""Content-addressed per-run usage derived from cumulative engine counters."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

RUN_USAGE_POLICY = "run-usage-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_COST_QUANTUM = Decimal("0.000000000001")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


@dataclass(frozen=True, slots=True)
class RunUsageTotals:
    input_tokens: int
    output_tokens: int
    cache_tokens: int
    turns: int
    reported_cost_usd: Decimal

    @classmethod
    def capture(cls, value: Any) -> RunUsageTotals:
        try:
            return cls(
                input_tokens=_counter(value.total_input_tokens, "input_tokens"),
                output_tokens=_counter(value.total_output_tokens, "output_tokens"),
                cache_tokens=_counter(value.cache_tokens, "cache_tokens"),
                turns=_counter(value.turns, "turns"),
                reported_cost_usd=_cost(value.total_cost_usd),
            )
        except AttributeError as exc:
            raise ValueError("运行用量来源缺少累计计数器。") from exc


class RunUsage(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["run-usage-v1"] = RUN_USAGE_POLICY
    usage_id: str = Field(pattern=r"^runusage_[0-9a-f]{24}$")
    usage_sha256: str = Field(pattern=_SHA256_RE)
    run_id: str = Field(min_length=1, max_length=128)
    source_kind: Literal["engine_counter_delta"] = "engine_counter_delta"
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_tokens: int = Field(ge=0)
    turns: int = Field(ge=0)
    reported_cost_usd: Decimal = Field(ge=0, decimal_places=12)
    usage_source_authority: Literal[True] = True
    billing_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("Run Usage run_id 无效。")
        core = self.model_dump(mode="json", exclude={"usage_id", "usage_sha256"})
        digest = _digest(core)
        if self.usage_sha256 != digest or self.usage_id != f"runusage_{digest[:24]}":
            raise ValueError("Run Usage identity 不一致。")
        return self


def build_run_usage(
    *,
    run_id: str,
    before: RunUsageTotals,
    after: RunUsageTotals,
) -> RunUsage:
    if not isinstance(before, RunUsageTotals) or not isinstance(after, RunUsageTotals):
        raise TypeError("Run Usage 需要前后两个累计计数快照。")
    deltas = {
        "input_tokens": after.input_tokens - before.input_tokens,
        "output_tokens": after.output_tokens - before.output_tokens,
        "cache_tokens": after.cache_tokens - before.cache_tokens,
        "turns": after.turns - before.turns,
    }
    if any(value < 0 for value in deltas.values()):
        raise ValueError("运行用量累计计数器发生倒退。")
    cost = (after.reported_cost_usd - before.reported_cost_usd).quantize(
        _COST_QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )
    if cost < 0:
        raise ValueError("运行费用累计计数器发生倒退。")
    core = {
        "schema_version": 1,
        "policy_version": RUN_USAGE_POLICY,
        "run_id": run_id,
        "source_kind": "engine_counter_delta",
        **deltas,
        "reported_cost_usd": str(cost),
        "usage_source_authority": True,
        "billing_authority": False,
    }
    digest = _digest(core)
    return RunUsage.model_validate(
        {
            **core,
            "usage_id": f"runusage_{digest[:24]}",
            "usage_sha256": digest,
        }
    )


def _counter(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"运行用量 {field} 必须是非负整数。")
    return value


def _cost(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("运行费用必须是非负有限数值。")
    try:
        cost = Decimal(str(value)).quantize(_COST_QUANTUM, rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("运行费用必须是非负有限数值。") from exc
    if not cost.is_finite() or cost < 0:
        raise ValueError("运行费用必须是非负有限数值。")
    return cost


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "RUN_USAGE_POLICY",
    "RunUsage",
    "RunUsageTotals",
    "build_run_usage",
]
