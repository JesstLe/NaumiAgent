"""Shared formatting for runtime budget status."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def _nonnegative_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else 0.0


def _optional_nonnegative_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _format_tokens(value: Any) -> str:
    number = _optional_nonnegative_float(value)
    return f"{int(number or 0):,}"


def format_budget_detail(info: Mapping[str, Any]) -> str:
    """Render nullable runtime limits without inventing a finite budget."""
    used = _nonnegative_float(info.get("used_usd"))
    max_usd = _optional_nonnegative_float(info.get("max_usd"))
    parts: list[str] = []
    if max_usd is None:
        label = "不限" if not info.get("enabled") else "不限费用"
        parts.append(f"{label} · 已用 ${used:.4f}")
    else:
        percent = _optional_nonnegative_float(info.get("cost_percentage"))
        suffix = f" ({percent:.1f}%)" if percent is not None else ""
        parts.append(f"${used:.4f}/${max_usd:.2f}{suffix}")
    if info.get("max_input_tokens") is not None:
        parts.append(
            f"输入 {_format_tokens(info.get('input_tokens'))}/"
            f"{_format_tokens(info.get('max_input_tokens'))}"
        )
    if info.get("max_output_tokens") is not None:
        parts.append(
            f"输出 {_format_tokens(info.get('output_tokens'))}/"
            f"{_format_tokens(info.get('max_output_tokens'))}"
        )
    return " · ".join(parts)
