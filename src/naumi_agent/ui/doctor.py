"""Environment diagnostics shared by CLI, TUI, and the doctor tool."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx

from naumi_agent.config.configurator import validate_provider_configuration
from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.worker_authority_health import (
    WorkerAuthorityEntry,
    WorkerAuthorityHealthError,
    WorkerAuthoritySnapshot,
    inspect_worker_authority_health,
)
from naumi_agent.persistence.store_catalog import (
    CatalogStatus,
    build_store_catalog,
    inspect_store_catalog,
)
from naumi_agent.tools.browser.runtime.chrome_launcher import (
    find_system_browser_executable,
)
from naumi_agent.ui.terminal_capabilities import (
    TerminalCapabilities,
    detect_terminal_capabilities,
)

if TYPE_CHECKING:
    from naumi_agent.model.router import ModelResponse
    from naumi_agent.runtime.ports.model import ModelPort

DoctorStatus = Literal["pass", "warn", "error"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: DoctorStatus
    detail: str
    suggestion: str = ""
    diagnostic_code: str = ""


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def status(self) -> DoctorStatus:
        if any(check.status == "error" for check in self.checks):
            return "error"
        if any(check.status == "warn" for check in self.checks):
            return "warn"
        return "pass"


async def run_doctor(
    config: AppConfig,
    *,
    workspace_root: str | Path,
    mcp_manager: Any | None = None,
    live: bool = False,
    live_probe: Callable[[AppConfig], Awaitable[ModelResponse]] | None = None,
    browser_fallback_available: bool | None = None,
    model_router: ModelPort | None = None,
    model_router_error: str | None = None,
) -> DoctorReport:
    """Run local diagnostics and an optional explicit model connectivity probe."""
    root = Path(workspace_root).expanduser()
    api_key_check = _check_api_key(config)
    provider_check = _check_model_provider(config)
    if browser_fallback_available is None:
        browser_fallback_available = await _detect_browser_fallback()
    checks = [
        _check_python(),
        _check_config(config),
        api_key_check,
        provider_check,
        *(
            (
                DoctorCheck(
                    "模型契约",
                    "error",
                    f"provider catalog 无法加载：{model_router_error}",
                    "修复 catalog 的字段、类型或模型能力声明后重试。",
                ),
            )
            if model_router_error
            else ()
        ),
        *(_check_model_contracts(model_router) if model_router is not None else ()),
        _check_search_readiness(
            search_config=config.search,
            browser_fallback_available=browser_fallback_available,
        ),
        _check_workspace(root),
        _check_state_store_catalog(config),
        *_check_worker_authority(config, workspace_root=root),
        _check_git(root),
        _check_command("Node.js", "node", ["node", "--version"]),
        _check_command("ripgrep", "rg", ["rg", "--version"]),
        _check_command("Docker", "docker", ["docker", "--version"]),
        await _check_browser_daemon(config),
        _check_mcp(config, mcp_manager),
        _check_debug_log(config),
        _check_terminal(),
    ]
    if live:
        failed_prerequisites = [
            check.name
            for check in (api_key_check, provider_check)
            if check.status == "error"
        ]
        if failed_prerequisites:
            checks.append(
                DoctorCheck(
                    "模型实时连接",
                    "error",
                    "已跳过：本地前置检查未通过（"
                    + "、".join(failed_prerequisites)
                    + "）",
                    "先运行 `naumi configure` 修复配置和凭据。",
                    "provider_prerequisite_failed",
                )
            )
        else:
            checks.append(await _check_live_model(config, probe=live_probe))
    return DoctorReport(checks=tuple(checks))


def _check_model_contracts(router: ModelPort) -> tuple[DoctorCheck, ...]:
    """Summarize unique configured tiers without probing paid provider APIs."""
    checks: list[DoctorCheck] = []
    seen: set[str] = set()
    for tier in ("fast", "capable", "reasoning"):
        model = router.resolve_model(tier)
        if model in seen:
            continue
        seen.add(model)
        try:
            contract = router.get_model_capability_contract(model)
        except Exception as exc:
            checks.append(
                DoctorCheck(
                    f"模型契约 {tier}",
                    "error",
                    f"{model}: 无法解析（{exc}）",
                    "检查 provider catalog、模型别名和 models.model_info。",
                )
            )
            continue
        if contract.status.value == "incompatible":
            status: DoctorStatus = "error"
            detail = "；".join(contract.errors) or "模型能力与 Agent Harness 不兼容。"
        elif contract.status.value in {"partial", "unverified"}:
            status = "warn"
            detail = "；".join(contract.warnings) or "模型元数据未完整验证。"
        else:
            status = "pass"
            detail = (
                f"{contract.canonical_model}: context {contract.max_context}，"
                f"output {contract.max_output}，tools/streaming 已验证。"
            )
        checks.append(
            DoctorCheck(
                f"模型契约 {tier}",
                status,
                f"{model}: {detail}",
                "在 provider catalog 或 models.model_info 补齐限制、价格、能力和模态。"
                if status != "pass"
                else "",
            )
        )
    return tuple(checks)


def render_doctor_report(report: DoctorReport) -> str:
    title = {
        "pass": "环境诊断通过",
        "warn": "环境诊断存在提醒",
        "error": "环境诊断发现错误",
    }[report.status]
    lines = [f"## {title}", ""]
    for check in report.checks:
        icon = {"pass": "PASS", "warn": "WARN", "error": "ERROR"}[check.status]
        lines.append(f"- **{icon} {check.name}**：{check.detail}")
        diagnostic_code = normalize_doctor_diagnostic_code(check.diagnostic_code)
        if diagnostic_code:
            lines.append(f"  诊断码：`{diagnostic_code}`")
        if check.suggestion:
            lines.append(f"  建议：{check.suggestion}")
    lines.append("")
    lines.append("这份报告可直接复制给 Agent 或维护者，用于定位本机环境问题。")
    return "\n".join(lines)


def _check_python() -> DoctorCheck:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return DoctorCheck("Python 环境", "pass", f"Python {version}: {sys.executable}")


def _check_config(config: AppConfig) -> DoctorCheck:
    session_path = Path(config.memory.session_db_path).expanduser()
    vector_path = Path(config.memory.vector_db_path).expanduser()
    if not session_path.parent.exists():
        return DoctorCheck(
            "config 文件",
            "warn",
            f"会话目录尚不存在: {session_path.parent}",
            "首次运行会自动创建；若失败，请检查父目录权限。",
        )
    return DoctorCheck(
        "config 文件",
        "pass",
        f"会话库: {session_path}；向量库: {vector_path}",
    )


def _check_state_store_catalog(config: AppConfig) -> DoctorCheck:
    """Summarize the authoritative physical Store Catalog without writes."""
    try:
        report = inspect_store_catalog(build_store_catalog(config))
    except (OSError, ValueError) as exc:
        return DoctorCheck(
            "状态存储目录",
            "error",
            f"无法构建 Store Catalog：{type(exc).__name__}: {str(exc)[:180]}",
            "检查持久化路径是否重复、可解析，并修复 Catalog 声明。",
        )

    status: DoctorStatus = {
        CatalogStatus.PASS: "pass",
        CatalogStatus.WARN: "warn",
        CatalogStatus.ERROR: "error",
    }[report.status]
    detail = (
        f"已登记 {len(report.stores)} 个物理 Store；"
        f"已存在 {report.existing_count}，未创建 {report.absent_count}，"
        f"提醒 {report.warning_count}，错误 {report.error_count}。"
    )
    concerns = [
        item.definition.store_id
        for item in report.stores
        if item.status is not CatalogStatus.PASS
    ]
    if concerns:
        detail += " 需治理：" + "、".join(concerns[:5])
        if len(concerns) > 5:
            detail += f" 等 {len(concerns)} 项"
    suggestion = ""
    if report.error_count:
        suggestion = "先停止写入相关 Store，升级程序或导出诊断；不要手工覆盖高版本或损坏数据库。"
    elif report.warning_count:
        suggestion = "未版本化或权限过宽的 Store 将由 ARC-05 迁移与备份流程治理。"
    return DoctorCheck("状态存储目录", status, detail, suggestion)


def _check_worker_authority(
    config: AppConfig,
    *,
    workspace_root: Path,
) -> tuple[DoctorCheck, ...]:
    """Project strictly read-only worker registration and heartbeat facts."""
    from naumi_agent.runtime.composition import build_runtime_paths

    try:
        paths = build_runtime_paths(config)
        snapshot = inspect_worker_authority_health(
            registry_db_path=paths.worker_registry_db_path,
            harness_db_path=paths.harness_db_path,
            workspace_root=workspace_root,
        )
    except (OSError, ValueError, WorkerAuthorityHealthError) as exc:
        code = exc.code if isinstance(exc, WorkerAuthorityHealthError) else "path_invalid"
        reason = {
            "registry_wrong_type": "注册库路径类型错误",
            "registry_schema_incompatible": "注册库版本不兼容",
            "registry_unreadable": "注册库损坏或不可读",
            "path_unreadable": "状态路径不可读",
            "path_invalid": "状态路径配置无效",
        }.get(code, "未知的只读诊断错误")
        return (
            DoctorCheck(
                "Worker authority",
                "error",
                f"注册 authority 无法可信读取：{reason}。",
                "停止向 Worker 派发任务；保留数据库并运行迁移预检或导出脱敏诊断。",
            ),
        )
    return _worker_authority_check(snapshot), _worker_queue_check(snapshot)


def _worker_authority_check(snapshot: WorkerAuthoritySnapshot) -> DoctorCheck:
    if snapshot.registry_health == "absent":
        return DoctorCheck(
            "Worker authority",
            "pass",
            "尚未启动隔离 Worker；注册中心会在首次真实注册时按需创建。",
        )
    if snapshot.active_count == 0:
        return DoctorCheck(
            "Worker authority",
            "pass",
            "注册中心 schema 正常，当前没有 active Worker。",
        )

    unhealthy = [
        worker for worker in snapshot.workers if worker.heartbeat_health != "healthy"
    ]
    dispatch_disabled = [
        worker for worker in snapshot.workers if not worker.dispatch_ready
    ]
    status: DoctorStatus = (
        "pass"
        if not unhealthy and not dispatch_disabled and not snapshot.truncated
        else "warn"
    )
    if any(
        worker.heartbeat_health
        in {
            "stale",
            "offline",
            "stopped",
            "failed",
            "clock_regression",
            "missing",
            "identity_mismatch",
            "invalid",
            "unavailable",
        }
        for worker in snapshot.workers
    ):
        status = "error"

    heartbeat_store = {
        "not_needed": "无需读取",
        "absent": "尚未创建",
        "ready": "正常",
        "incompatible": "版本不兼容",
        "error": "读取失败",
    }[snapshot.heartbeat_store_health]
    detail = (
        f"active Worker {snapshot.active_count} 个；心跳 Store {heartbeat_store}。"
    )
    summaries = [_worker_authority_summary(worker) for worker in snapshot.workers[:3]]
    if summaries:
        detail += " " + "；".join(summaries)
    if snapshot.active_count > len(summaries):
        detail += f"；另有 {snapshot.active_count - len(summaries)} 个未展开"
    suggestion = ""
    if status == "error":
        suggestion = "暂停新任务派发，核对 Worker instance/epoch 与 Harness heartbeat 后再恢复。"
    elif status == "warn":
        if dispatch_disabled:
            suggestion = (
                "独立 Agent 控制进程已就绪，但任务仍由 embedded Runtime 执行；"
                "完成 durable dispatch 接入前不要向该 Worker 派发任务。"
            )
        else:
            suggestion = "等待启动或排空完成后刷新；若状态持续不变，请检查 Worker 日志。"
    return DoctorCheck("Worker authority", status, detail, suggestion)


def _worker_queue_check(snapshot: WorkerAuthoritySnapshot) -> DoctorCheck:
    if snapshot.registry_health == "absent":
        return DoctorCheck(
            "Worker 容量队列",
            "pass",
            "尚未启动隔离 Worker；容量队列会在首次真实入队时按需创建。",
        )
    configured = tuple(
        worker for worker in snapshot.workers if worker.queue_max_waiters is not None
    )
    if not configured:
        return DoctorCheck(
            "Worker 容量队列",
            "pass",
            "当前 active Worker 尚未启用持久容量队列。",
        )
    queue_pressure = any(
        worker.queue_max_waiters is not None
        and worker.queue_max_waiters > 0
        and worker.waiting_jobs >= worker.queue_max_waiters
        for worker in configured
    )
    queue_stale = any(
        worker.expired_waiting_jobs or worker.expired_claims
        for worker in configured
    )
    status: DoctorStatus = (
        "warn" if queue_pressure or queue_stale or snapshot.truncated else "pass"
    )
    summaries = [_worker_queue_summary(worker) for worker in configured[:3]]
    detail = f"已配置队列 Worker {len(configured)} 个。 " + "；".join(summaries)
    if snapshot.active_count > len(snapshot.workers):
        detail += f"；另有 {snapshot.active_count - len(snapshot.workers)} 个未检查"
    suggestion = ""
    if queue_stale:
        suggestion = "存在到期但尚未收口的队列事实；运行调度器核对后刷新。"
    elif queue_pressure:
        suggestion = "Worker 等待队列已满；等待容量释放，避免无界重试。"
    elif snapshot.truncated:
        suggestion = "active Worker 数量超过本页上限；使用后续队列详情页检查其余 Worker。"
    return DoctorCheck("Worker 容量队列", status, detail, suggestion)


def _worker_authority_summary(worker: WorkerAuthorityEntry) -> str:
    health = {
        "starting": "启动中",
        "healthy": "健康",
        "draining": "排空中",
        "stale": "陈旧",
        "offline": "离线",
        "stopped": "已停止",
        "failed": "失败",
        "clock_regression": "时钟倒退",
        "missing": "缺失",
        "identity_mismatch": "身份不匹配",
        "invalid": "内容无效",
        "unavailable": "不可读取",
    }.get(worker.heartbeat_health, "未知")
    age = (
        f"/{worker.heartbeat_age_seconds:.1f}s"
        if worker.heartbeat_age_seconds is not None
        else ""
    )
    worker_id = worker.worker_id if len(worker.worker_id) <= 48 else worker.worker_id[:47] + "…"
    machine = worker.machine if len(worker.machine) <= 32 else worker.machine[:31] + "…"
    identity = (
        f"{worker_id} {worker.kind} epoch {worker.epoch} "
        f"{worker.platform}/{machine} "
    )
    if not worker.dispatch_ready:
        return identity + f"控制通道就绪、任务调度未开放 心跳{health}{age}"
    return (
        identity
        + f"容量占用 {worker.reserved_jobs}/{worker.max_concurrent_jobs}、"
        + f"可用 {worker.available_jobs} 心跳{health}{age}"
    )


def _worker_queue_summary(worker: WorkerAuthorityEntry) -> str:
    assert worker.queue_max_waiters is not None
    worker_id = worker.worker_id if len(worker.worker_id) <= 48 else worker.worker_id[:47] + "…"
    detail = (
        f"{worker_id} 等待 {worker.waiting_jobs}/{worker.queue_max_waiters}、"
        f"领取 {worker.active_claims}"
    )
    if worker.oldest_wait_seconds is not None:
        detail += f"、最久 {worker.oldest_wait_seconds:.1f}s"
    pending_expiry = worker.expired_waiting_jobs + worker.expired_claims
    if pending_expiry:
        detail += f"、待收口 {pending_expiry}"
    return detail


def _check_api_key(config: AppConfig) -> DoctorCheck:
    if config.models.api_key:
        return DoctorCheck("API key", "pass", "已从安全凭据来源加载")
    env_keys = [key for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if os.getenv(key)]
    if env_keys:
        return DoctorCheck("API key", "pass", f"已通过环境变量配置: {', '.join(env_keys)}")
    return DoctorCheck(
        "API key",
        "error",
        "未检测到模型 API Key 或 provider 环境变量",
        "重新运行首次引导写入系统凭据库，或导出对应模型服务的环境变量。",
        "provider_credentials_missing",
    )


def _check_model_provider(config: AppConfig) -> DoctorCheck:
    model = config.models.default_model
    if not model:
        return DoctorCheck(
            "model provider",
            "error",
            "默认模型为空",
            "请配置 models.default_model。",
            "provider_model_missing",
        )
    provider, error = validate_provider_configuration(
        provider=config.models.provider,
        default_model=config.models.default_model,
        fast_model=config.models.fast_model,
        reasoning_model=config.models.reasoning_model,
        api_base=config.models.api_base,
        temperature=config.models.temperature,
    )
    if error:
        suggestion = "运行 `naumi configure` 统一 provider、模型和 API Base。"
        if "temperature" in error:
            suggestion = (
                "运行 `naumi configure`，并清理覆盖它的 "
                "NAUMI_MODELS__TEMPERATURE 或 .env 设置。"
            )
        return DoctorCheck(
            "model provider",
            "error",
            error,
            suggestion,
            (
                "provider_temperature_invalid"
                if "temperature" in error
                else "provider_config_invalid"
            ),
        )
    return DoctorCheck(
        "model provider",
        "pass",
        f"provider: {provider}；默认模型: {model}",
    )


def _check_search_readiness(
    *,
    search_config: Any | None = None,
    direct_search_available: bool = True,
    browser_fallback_available: bool = True,
) -> DoctorCheck:
    """Report search capability separately from the required model credentials."""
    provider_order = tuple(
        getattr(search_config, "provider_order", ("brave", "duckduckgo", "browser"))
    )
    brave = getattr(search_config, "brave", None)
    brave_key = (
        brave.resolve_api_key()
        if brave is not None
        else os.getenv("BRAVE_SEARCH_API_KEY", "").strip() or None
    )
    if "brave" in provider_order and brave_key:
        return DoctorCheck(
            "网络搜索",
            "pass",
            "已增强：检测到 Brave Search 凭据；失败时仍会自动回退。",
        )
    direct_enabled = direct_search_available and "duckduckgo" in provider_order
    browser_enabled = browser_fallback_available and "browser" in provider_order
    if direct_enabled:
        if not browser_enabled:
            return DoctorCheck(
                "网络搜索",
                "warn",
                "可用（零配置）：免 Key 直连可用，但浏览器回退不可用。",
                "运行 `python -m playwright install chromium` 安装浏览器运行时。",
            )
        return DoctorCheck(
            "网络搜索",
            "pass",
            "可用（零配置）：免 Key 直连搜索，并支持浏览器自动回退。",
            "BRAVE_SEARCH_API_KEY 仅用于提升质量和稳定性，不是安装必需项。",
        )
    if browser_enabled:
        return DoctorCheck(
            "网络搜索",
            "warn",
            "受限：免 Key 直连不可用，仅可使用浏览器回退。",
            "检查网络后重试；也可选配 BRAVE_SEARCH_API_KEY。",
        )
    return DoctorCheck(
        "网络搜索",
        "warn",
        "受限：当前没有可用的直连搜索或浏览器回退。",
        "检查网络和浏览器依赖；也可选配 BRAVE_SEARCH_API_KEY。",
    )


async def _detect_browser_fallback() -> bool:
    """Check the Playwright-managed Chromium executable without launching it."""
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            bundled = Path(playwright.chromium.executable_path).is_file()
        return bundled or find_system_browser_executable() is not None
    except Exception:
        return False


async def _check_live_model(
    config: AppConfig,
    *,
    probe: Callable[[AppConfig], Awaitable[ModelResponse]] | None = None,
) -> DoctorCheck:
    started = time.monotonic()
    try:
        response = await (probe or default_live_model_probe)(config)
    except Exception as exc:
        return _classify_live_model_error(exc)
    duration_ms = max(0, round((time.monotonic() - started) * 1000))
    model = response.model or config.models.fast_model
    return DoctorCheck(
        "模型实时连接",
        "pass",
        f"连接成功：{model}，耗时 {duration_ms} ms",
    )


async def default_live_model_probe(config: AppConfig) -> ModelResponse:
    """Send exactly one minimal provider request for an explicit Doctor probe."""
    from naumi_agent.model.router import ModelRouter, ModelTier

    probe_config = config.models.model_copy(update={"max_tokens": 8})
    router = ModelRouter(probe_config)
    return await router.call(
        messages=[{"role": "user", "content": "Reply with OK."}],
        tier=ModelTier.FAST,
        max_tokens=8,
    )


def _classify_live_model_error(exc: Exception) -> DoctorCheck:
    status_code = _provider_http_status_code(exc)
    evidence = f"{type(exc).__name__} {exc}"[:2000].lower()
    unstructured = status_code is None
    if status_code in {401, 403} or (
        unstructured
        and any(
            token in evidence
            for token in ("authentication", "unauthorized", "forbidden")
        )
    ):
        return DoctorCheck(
            "模型实时连接",
            "error",
            f"认证失败（{status_code or 401}）",
            "运行 `naumi configure` 更新系统凭据，然后重试。",
            "provider_auth_failed",
        )
    if status_code == 404 or (
        unstructured and ("notfound" in evidence or "not found" in evidence)
    ):
        return DoctorCheck(
            "模型实时连接",
            "error",
            "模型或 API 地址不存在（404）",
            "检查 provider、模型和 API Base；代理服务请使用 custom provider。",
            "provider_resource_not_found",
        )
    if status_code == 429 or (
        unstructured and ("ratelimit" in evidence or "rate limit" in evidence)
    ):
        return DoctorCheck(
            "模型实时连接",
            "warn",
            "服务限流（429）",
            "凭据与地址通常有效，请稍后重试或检查服务额度。",
            "provider_rate_limited",
        )
    if status_code is not None and 500 <= status_code <= 599:
        return DoctorCheck(
            "模型实时连接",
            "error",
            f"模型服务暂时不可用（{status_code}）",
            "稍后重试；若持续失败，请检查 provider 状态页或切换已验证模型。",
            "provider_server_error",
        )
    if (
        status_code == 408
        or isinstance(exc, TimeoutError | httpx.TimeoutException)
        or (
            unstructured
            and any(token in evidence for token in ("timeout", "timed out"))
        )
    ):
        return DoctorCheck(
            "模型实时连接",
            "error",
            "连接超时",
            "检查网络、代理和 API Base 可达性。",
            "provider_timeout",
        )
    if isinstance(exc, httpx.NetworkError | ConnectionError):
        return DoctorCheck(
            "模型实时连接",
            "error",
            "无法连接模型服务",
            "检查网络、代理、DNS 和 API Base 可达性。",
            "provider_connection_failed",
        )
    return DoctorCheck(
        "模型实时连接",
        "error",
        f"连接失败（{type(exc).__name__}）",
        "查看 debug log，并检查 provider、网络和服务状态。",
        "provider_request_failed",
    )


def _provider_http_status_code(exc: Exception) -> int | None:
    """Extract one provider HTTP status without trusting the exception body."""
    response = getattr(exc, "response", None)
    for value in (getattr(exc, "status_code", None), getattr(response, "status_code", None)):
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    evidence = f"{type(exc).__name__} {exc}"[:2000]
    match = re.search(r"(?<!\d)(401|403|404|429|5\d\d)(?!\d)", evidence)
    return int(match.group(1)) if match else None


def normalize_doctor_diagnostic_code(value: object) -> str:
    """Return one bounded low-cardinality code safe for every Doctor surface."""
    code = str(value or "").strip()
    if not code:
        return ""
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        return code
    return "diagnostic_code_invalid"


def _check_workspace(root: Path) -> DoctorCheck:
    if not root.exists():
        return DoctorCheck(
            "workspace 权限",
            "error",
            f"工作区不存在: {root}",
            "检查 config.workspace_root 或启动目录。",
        )
    if not os.access(root, os.R_OK | os.W_OK):
        return DoctorCheck(
            "workspace 权限",
            "error",
            f"工作区不可读写: {root}",
            "修复目录权限后重试。",
        )
    return DoctorCheck("workspace 权限", "pass", f"可读写: {root}")


def _check_git(root: Path) -> DoctorCheck:
    result = _run_command(["git", "status", "--short", "--branch"], cwd=root)
    if result is None:
        return DoctorCheck("git 状态", "warn", "未找到 git 命令", "安装 git 后可获得分支状态。")
    code, output = result
    if code != 0:
        return DoctorCheck("git 状态", "warn", output or "当前目录不是 git 仓库")
    first_line = output.splitlines()[0] if output else "git 仓库"
    return DoctorCheck("git 状态", "pass", first_line)


def _check_command(name: str, binary: str, command: list[str]) -> DoctorCheck:
    if shutil.which(binary) is None:
        return DoctorCheck(name, "warn", f"未找到 `{binary}`", f"安装 {binary} 可启用相关能力。")
    result = _run_command(command)
    if result is None:
        return DoctorCheck(name, "warn", f"`{binary}` 不可执行")
    code, output = result
    if code != 0:
        return DoctorCheck(name, "warn", output or f"`{binary}` 返回非零退出码")
    first_line = output.splitlines()[0] if output else f"{binary} 可用"
    return DoctorCheck(name, "pass", first_line)


async def _check_browser_daemon(config: AppConfig) -> DoctorCheck:
    daemon = config.browser_daemon
    if not daemon.enabled:
        return DoctorCheck("browser daemon", "warn", "browser daemon 集成已禁用")
    url = daemon.base_url.rstrip("/") + "/health"
    headers = {"Authorization": f"Bearer {daemon.token}"} if daemon.token else {}
    try:
        async with httpx.AsyncClient(timeout=0.8) as client:
            response = await client.get(url, headers=headers)
        if response.status_code >= 400:
            return DoctorCheck(
                "browser daemon",
                "warn",
                f"HTTP {response.status_code}: {url}",
                "执行 /bdaemon start 或检查 browser_daemon.base_url/token。",
            )
        return DoctorCheck("browser daemon", "pass", f"可访问: {url}")
    except Exception as exc:
        return DoctorCheck(
            "browser daemon",
            "warn",
            f"不可访问: {url} ({type(exc).__name__})",
            "需要浏览器自动化时，执行 /bdaemon start。",
        )


def _check_mcp(config: AppConfig, manager: Any | None) -> DoctorCheck:
    configured = sorted(config.mcp.servers)
    connected = list(getattr(manager, "connected_servers", []) or []) if manager else []
    if configured and not connected:
        return DoctorCheck(
            "MCP servers",
            "warn",
            f"已配置 {len(configured)} 个，当前未连接",
            "检查 MCP server 命令是否可执行，或查看启动日志。",
        )
    if connected:
        return DoctorCheck("MCP servers", "pass", "已连接: " + ", ".join(connected))
    return DoctorCheck("MCP servers", "pass", "未配置 MCP server")


def _check_debug_log(config: AppConfig) -> DoctorCheck:
    base = Path(config.memory.session_db_path).expanduser().parent / "debug-runs"
    try:
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=base, prefix=".doctor-", delete=True) as tmp:
            tmp.write(b"ok")
            tmp.flush()
        return DoctorCheck("debug log 写入权限", "pass", f"可写: {base}")
    except Exception as exc:
        return DoctorCheck(
            "debug log 写入权限",
            "error",
            f"不可写: {base} ({type(exc).__name__})",
            "修复 data/debug-runs 所在目录权限。",
        )


def _check_terminal(
    profile: TerminalCapabilities | None = None,
    *,
    width: int | None = None,
) -> DoctorCheck:
    profile = profile or detect_terminal_capabilities()
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns
    term = profile.terminal or "unknown"
    program = f" program={profile.terminal_program}" if profile.terminal_program else ""
    detail = (
        f"TERM={term}{program} width={width} color={profile.color_level} "
        f"unicode={'yes' if profile.unicode else 'no'} "
        f"fullscreen={'yes' if profile.full_screen else 'no'} "
        f"sync={'yes' if profile.synchronized_output else 'no'} "
        f"keyboard={'enhanced' if profile.enhanced_keyboard else 'baseline'} "
        f"mouse={profile.mouse_protocol} signal={profile.signal_mode}"
    )
    if not profile.interactive:
        return DoctorCheck(
            "terminal capability",
            "warn",
            detail,
            "当前不是交互式 TTY；New UI 不会发送控制序列，请在终端中运行或使用 Textual fallback。",
        )
    if not profile.full_screen:
        return DoctorCheck(
            "terminal capability",
            "warn",
            detail,
            "终端未通过安全全屏能力检测；New UI 会拒绝启动，可使用 `naumi --tui`。",
        )
    if width < 60:
        return DoctorCheck(
            "terminal capability",
            "warn",
            detail,
            "窗口过窄会影响表格和 diff 显示，建议至少 80 列。",
        )
    return DoctorCheck("terminal capability", "pass", detail)


def _run_command(command: list[str], *, cwd: Path | None = None) -> tuple[int, str] | None:
    if shutil.which(command[0]) is None:
        return None
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or proc.stderr).strip()
