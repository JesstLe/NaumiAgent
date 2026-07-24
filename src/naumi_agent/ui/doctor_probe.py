"""Bounded explicit provider probe shared by every terminal surface."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from naumi_agent.config.settings import AppConfig
from naumi_agent.ui.doctor import (
    DoctorCheck,
    DoctorReport,
    default_live_model_probe,
    normalize_doctor_diagnostic_code,
    render_doctor_report,
    run_doctor,
)

if TYPE_CHECKING:
    from naumi_agent.model.router import ModelResponse
    from naumi_agent.runtime.ports.model import ModelPort

DOCTOR_LIVE_PROBE_SCHEMA_VERSION = 1
DOCTOR_LIVE_PROBE_DEFAULT_TIMEOUT_MS = 15_000
DOCTOR_LIVE_PROBE_MIN_TIMEOUT_MS = 1_000
DOCTOR_LIVE_PROBE_MAX_TIMEOUT_MS = 60_000
DOCTOR_LIVE_PROBE_REQUEST_LIMIT = 1
DOCTOR_LIVE_PROBE_MAX_OUTPUT_TOKENS = 8

DoctorLiveProbeStatus = Literal["passed", "failed", "blocked"]


@dataclass(frozen=True)
class DoctorLiveProbeResult:
    """One terminal result with its complete Doctor report and bounded receipt."""

    report: DoctorReport
    status: DoctorLiveProbeStatus
    diagnostic_code: str
    message: str
    suggestion: str
    request_count: int
    duration_ms: int
    timeout_ms: int


def normalize_doctor_live_probe_timeout(value: object) -> int:
    """Validate a public probe timeout without silently widening its budget."""
    if isinstance(value, bool):
        raise ValueError("Doctor 在线探测 timeout_ms 必须是整数。")
    try:
        timeout_ms = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Doctor 在线探测 timeout_ms 必须是整数。") from exc
    if not DOCTOR_LIVE_PROBE_MIN_TIMEOUT_MS <= timeout_ms <= DOCTOR_LIVE_PROBE_MAX_TIMEOUT_MS:
        raise ValueError(
            "Doctor 在线探测 timeout_ms 必须在 "
            f"{DOCTOR_LIVE_PROBE_MIN_TIMEOUT_MS}..{DOCTOR_LIVE_PROBE_MAX_TIMEOUT_MS}。"
        )
    return timeout_ms


async def run_bounded_doctor_live_probe(
    config: AppConfig,
    *,
    workspace_root: str | Path,
    timeout_ms: int = DOCTOR_LIVE_PROBE_DEFAULT_TIMEOUT_MS,
    mcp_manager: Any | None = None,
    live_probe: Callable[[AppConfig], Awaitable[ModelResponse]] | None = None,
    on_request_start: Callable[[], None] | None = None,
    browser_fallback_available: bool | None = None,
    model_router: ModelPort | None = None,
    model_router_error: str | None = None,
) -> DoctorLiveProbeResult:
    """Run local Doctor plus at most one timed, minimal provider request.

    Opening or refreshing Doctor never calls this function. Cancellation is
    intentionally allowed to propagate so the owning surface can emit a
    terminal cancellation receipt rather than misclassifying it as a provider
    failure.
    """
    bounded_timeout_ms = normalize_doctor_live_probe_timeout(timeout_ms)
    request_count = 0
    request_duration_ms = 0
    probe = live_probe or default_live_model_probe

    async def timed_probe(probe_config: AppConfig) -> ModelResponse:
        nonlocal request_count, request_duration_ms
        if request_count >= DOCTOR_LIVE_PROBE_REQUEST_LIMIT:
            raise RuntimeError("Doctor 在线探测超过单次请求预算。")
        request_count += 1
        if on_request_start is not None:
            on_request_start()
        started = time.monotonic()
        try:
            async with asyncio.timeout(bounded_timeout_ms / 1000):
                return await probe(probe_config)
        finally:
            request_duration_ms = max(
                0,
                round((time.monotonic() - started) * 1000),
            )

    report = await run_doctor(
        config,
        workspace_root=workspace_root,
        mcp_manager=mcp_manager,
        live=True,
        live_probe=timed_probe,
        browser_fallback_available=browser_fallback_available,
        model_router=model_router,
        model_router_error=model_router_error,
    )
    check = _live_check(report)
    diagnostic_code = normalize_doctor_diagnostic_code(check.diagnostic_code)
    status: DoctorLiveProbeStatus
    if diagnostic_code == "provider_prerequisite_failed":
        status = "blocked"
    elif check.status == "pass":
        status = "passed"
    else:
        status = "failed"
    return DoctorLiveProbeResult(
        report=report,
        status=status,
        diagnostic_code=diagnostic_code,
        message=check.detail,
        suggestion=check.suggestion,
        request_count=request_count,
        duration_ms=request_duration_ms,
        timeout_ms=bounded_timeout_ms,
    )


def doctor_live_probe_payload(
    result: DoctorLiveProbeResult,
    *,
    snapshot_sha256: str,
) -> dict[str, object]:
    """Build the bounded public action receipt; report details travel via Health."""
    return {
        "schema_version": DOCTOR_LIVE_PROBE_SCHEMA_VERSION,
        "status": result.status,
        "diagnostic_code": result.diagnostic_code,
        "message": result.message[:500],
        "suggestion": result.suggestion[:500],
        "request_count": result.request_count,
        "request_limit": DOCTOR_LIVE_PROBE_REQUEST_LIMIT,
        "max_output_tokens": DOCTOR_LIVE_PROBE_MAX_OUTPUT_TOKENS,
        "duration_ms": result.duration_ms,
        "timeout_ms": result.timeout_ms,
        "snapshot_sha256": snapshot_sha256,
    }


def cancelled_doctor_live_probe_payload(
    *,
    timeout_ms: int,
    request_count: int,
) -> dict[str, object]:
    """Build a terminal cancellation receipt without provider or secret data."""
    return {
        "schema_version": DOCTOR_LIVE_PROBE_SCHEMA_VERSION,
        "status": "cancelled",
        "diagnostic_code": "provider_probe_cancelled",
        "message": "在线探测已取消；不会自动重试。",
        "suggestion": "需要时可再次显式运行 `/doctor probe`。",
        "request_count": min(
            DOCTOR_LIVE_PROBE_REQUEST_LIMIT,
            max(0, int(request_count)),
        ),
        "request_limit": DOCTOR_LIVE_PROBE_REQUEST_LIMIT,
        "max_output_tokens": DOCTOR_LIVE_PROBE_MAX_OUTPUT_TOKENS,
        "duration_ms": 0,
        "timeout_ms": normalize_doctor_live_probe_timeout(timeout_ms),
        "snapshot_sha256": "",
    }


def render_doctor_live_probe_result(result: DoctorLiveProbeResult) -> str:
    """Render the shared receipt after the full Doctor report."""
    return (
        render_doctor_report(result.report)
        + "\n\n"
        + "### 在线探测回执\n\n"
        + f"- 状态：`{result.status}`\n"
        + f"- 请求：`{result.request_count}/{DOCTOR_LIVE_PROBE_REQUEST_LIMIT}`\n"
        + f"- 最大输出：`{DOCTOR_LIVE_PROBE_MAX_OUTPUT_TOKENS} tokens`\n"
        + f"- 超时：`{result.timeout_ms} ms`\n"
        + f"- 请求耗时：`{result.duration_ms} ms`\n"
        + "- 自动重试：`关闭`"
    )


def _live_check(report: DoctorReport) -> DoctorCheck:
    for check in reversed(report.checks):
        if check.name == "模型实时连接":
            return check
    raise RuntimeError("Doctor 在线探测未返回模型实时连接结果。")


__all__ = [
    "DOCTOR_LIVE_PROBE_DEFAULT_TIMEOUT_MS",
    "DOCTOR_LIVE_PROBE_MAX_OUTPUT_TOKENS",
    "DOCTOR_LIVE_PROBE_MAX_TIMEOUT_MS",
    "DOCTOR_LIVE_PROBE_MIN_TIMEOUT_MS",
    "DOCTOR_LIVE_PROBE_REQUEST_LIMIT",
    "DOCTOR_LIVE_PROBE_SCHEMA_VERSION",
    "DoctorLiveProbeResult",
    "cancelled_doctor_live_probe_payload",
    "doctor_live_probe_payload",
    "normalize_doctor_live_probe_timeout",
    "render_doctor_live_probe_result",
    "run_bounded_doctor_live_probe",
]
