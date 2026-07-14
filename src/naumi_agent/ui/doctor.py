"""Environment diagnostics shared by CLI, TUI, and the doctor tool."""

from __future__ import annotations

import os
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
from naumi_agent.tools.browser.runtime.chrome_launcher import (
    find_system_browser_executable,
)

if TYPE_CHECKING:
    from naumi_agent.model.router import ModelResponse

DoctorStatus = Literal["pass", "warn", "error"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: DoctorStatus
    detail: str
    suggestion: str = ""


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
        _check_search_readiness(
            search_config=config.search,
            browser_fallback_available=browser_fallback_available,
        ),
        _check_workspace(root),
        _check_git(root),
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
                )
            )
        else:
            checks.append(await _check_live_model(config, probe=live_probe))
    return DoctorReport(checks=tuple(checks))


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
    )


def _check_model_provider(config: AppConfig) -> DoctorCheck:
    model = config.models.default_model
    if not model:
        return DoctorCheck(
            "model provider",
            "error",
            "默认模型为空",
            "请配置 models.default_model。",
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
        response = await (probe or _default_live_probe)(config)
    except Exception as exc:
        return _classify_live_model_error(exc)
    duration_ms = max(0, round((time.monotonic() - started) * 1000))
    model = response.model or config.models.fast_model
    return DoctorCheck(
        "模型实时连接",
        "pass",
        f"连接成功：{model}，耗时 {duration_ms} ms",
    )


async def _default_live_probe(config: AppConfig) -> ModelResponse:
    from naumi_agent.model.router import ModelRouter, ModelTier

    probe_config = config.models.model_copy(update={"max_tokens": 8})
    router = ModelRouter(probe_config)
    return await router.call(
        messages=[{"role": "user", "content": "Reply with OK."}],
        tier=ModelTier.FAST,
        max_tokens=8,
    )


def _classify_live_model_error(exc: Exception) -> DoctorCheck:
    evidence = f"{type(exc).__name__} {exc}".lower()
    if "401" in evidence or "authentication" in evidence or "unauthorized" in evidence:
        return DoctorCheck(
            "模型实时连接",
            "error",
            "认证失败（401）",
            "运行 `naumi configure` 更新系统凭据，然后重试。",
        )
    if "404" in evidence or "notfound" in evidence or "not found" in evidence:
        return DoctorCheck(
            "模型实时连接",
            "error",
            "模型或 API 地址不存在（404）",
            "检查 provider、模型和 API Base；代理服务请使用 custom provider。",
        )
    if "429" in evidence or "ratelimit" in evidence or "rate limit" in evidence:
        return DoctorCheck(
            "模型实时连接",
            "warn",
            "服务限流（429）",
            "凭据与地址通常有效，请稍后重试或检查服务额度。",
        )
    if "timeout" in evidence or "timed out" in evidence:
        return DoctorCheck(
            "模型实时连接",
            "error",
            "连接超时",
            "检查网络、代理和 API Base 可达性。",
        )
    return DoctorCheck(
        "模型实时连接",
        "error",
        f"连接失败（{type(exc).__name__}）",
        "查看 debug log，并检查 provider、网络和服务状态。",
    )


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


def _check_terminal() -> DoctorCheck:
    width = shutil.get_terminal_size((80, 24)).columns
    term = os.getenv("TERM", "unknown")
    color = os.getenv("COLORTERM", "")
    if width < 60:
        return DoctorCheck(
            "terminal capability",
            "warn",
            f"TERM={term} width={width}",
            "窗口过窄会影响表格和 diff 显示，建议至少 80 列。",
        )
    detail = f"TERM={term} width={width}" + (f" COLORTERM={color}" if color else "")
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
