"""Environment diagnostics shared by CLI, TUI, and the doctor tool."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from naumi_agent.config.settings import AppConfig

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
) -> DoctorReport:
    """Run deterministic local diagnostics without mutating user data."""
    root = Path(workspace_root).expanduser()
    checks = [
        _check_python(),
        _check_config(config),
        _check_api_key(config),
        _check_model_provider(config),
        _check_workspace(root),
        _check_git(root),
        _check_command("ripgrep", "rg", ["rg", "--version"]),
        _check_command("Docker", "docker", ["docker", "--version"]),
        await _check_browser_daemon(config),
        _check_mcp(config, mcp_manager),
        _check_debug_log(config),
        _check_terminal(),
    ]
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
        return DoctorCheck("API key", "pass", "已配置 models.api_key")
    env_keys = [key for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if os.getenv(key)]
    if env_keys:
        return DoctorCheck("API key", "pass", f"已通过环境变量配置: {', '.join(env_keys)}")
    return DoctorCheck(
        "API key",
        "error",
        "未检测到 models.api_key、OPENAI_API_KEY 或 ANTHROPIC_API_KEY",
        "在 config.yaml 中配置 models.api_key，或导出对应模型服务的环境变量。",
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
    return DoctorCheck("model provider", "pass", f"默认模型: {model}")


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
