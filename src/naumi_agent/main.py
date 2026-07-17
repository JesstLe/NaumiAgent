"""NaumiAgent CLI 入口."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import typer
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.configurator import ConfigurationError, configure_project
from naumi_agent.config.paths import DEFAULT_CONFIG_PATH, resolve_config_path
from naumi_agent.config.settings import AppConfig
from naumi_agent.log_setup import suppress_startup_import_warnings
from naumi_agent.streaming.sinks import CallbackEventSink
from naumi_agent.ui.budget import format_budget_detail
from naumi_agent.ui.code_excerpt import (
    DEFAULT_CODE_BLOCK_MAX_LINES,
    excerpt_markdown_code_blocks,
)
from naumi_agent.ui.doctor import render_doctor_report, run_doctor
from naumi_agent.ui.keybindings import build_keybindings, render_keybinding_help
from naumi_agent.ui.tool_activity import format_tool_prepare_status
from naumi_agent.workbench.export import export_audit_events
from naumi_agent.workbench.store import WorkbenchStore

suppress_startup_import_warnings()

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLI_STREAM_FRAME_INTERVAL_SECONDS = 0.2


def _configure_windows_utf8(
    *,
    platform: str | None = None,
    streams: tuple[Any, ...] | None = None,
) -> None:
    """Keep Rich output Unicode-safe in legacy Windows console code pages."""
    if (sys.platform if platform is None else platform) != "win32":
        return
    for stream in streams or (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


_configure_windows_utf8()


def _get_git_info() -> dict[str, str | bool]:
    """Get current git branch and dirty status (TTL-cached 5s)."""
    import subprocess
    import time

    # TTL cache: refresh every 5 seconds so branch switches show up
    now = time.monotonic()
    if (
        hasattr(_get_git_info, "_cache")
        and now - _get_git_info._cache_time < 5  # type: ignore[attr-defined]
    ):
        return _get_git_info._cache.copy()  # type: ignore[attr-defined]

    result: dict[str, str | bool] = {"branch": "", "dirty": False}
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=str(Path.cwd()),
        ).decode().strip()
        if branch:
            result["branch"] = branch
            try:
                result["dirty"] = bool(
                    subprocess.check_output(
                        ["git", "status", "--porcelain"],
                        stderr=subprocess.DEVNULL,
                        cwd=str(Path.cwd()),
                    ).decode().strip()
                )
            except Exception:
                pass
    except Exception:
        pass

    _get_git_info._cache = result  # type: ignore[attr-defined]
    _get_git_info._cache_time = now  # type: ignore[attr-defined]
    return result.copy()

app = typer.Typer(
    name="naumi",
    help="NaumiAgent — 通用智能 Agent",
    no_args_is_help=False,
)
naumiagent_app = typer.Typer(
    name="naumiagent",
    help="NaumiAgent 兼容命令入口",
    add_completion=False,
)
workbench_app = typer.Typer(
    name="workbench",
    help="Workbench 治理与审计命令",
)
app.add_typer(workbench_app, name="workbench")
console = Console()


def _ensure_onboarding_ready(config: str) -> None:
    """Migrate legacy credentials and complete first-run configuration."""
    from naumi_agent.cli.onboarding import (
        migrate_legacy_model_api_key,
        needs_onboarding,
        run_onboarding,
    )
    from naumi_agent.config.credentials import CredentialStoreError

    config_path = Path(_resolve_config_path(config))
    try:
        migrate_legacy_model_api_key(config_path)
    except CredentialStoreError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if needs_onboarding(config_path) and not run_onboarding(
        config_path,
        project_root=_PROJECT_ROOT,
    ):
        console.print("[yellow]配置未完成，退出。[/yellow]")
        raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def _default_command(
    ctx: typer.Context,
    config: str = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径"),
    tui: bool = typer.Option(False, "--tui", help="显式启动 Textual TUI fallback"),
    version: bool = typer.Option(False, "--version", "-v", help="显示版本"),
) -> None:
    """默认无子命令时启动新一代终端 UI."""
    if version:
        import importlib.metadata

        try:
            console.print(f"naumi {importlib.metadata.version('naumi-agent')}")
        except importlib.metadata.PackageNotFoundError:
            console.print("naumi (development)")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _ensure_onboarding_ready(config)
        if tui:
            _launch_tui(config)
            return
        _exit_after_terminal_ui(config)


def _build_ui_style_from_config(config: Any):
    from naumi_agent.ui.theme import build_ui_style_from_config

    return build_ui_style_from_config(config)


def _build_style_help_text(config: Any) -> str:
    from naumi_agent.ui.theme import render_style_help

    return render_style_help(_build_ui_style_from_config(config))


class TerminalUiLaunchError(RuntimeError):
    """Raised when the next terminal UI cannot be launched."""


_TERMINAL_UI_NO_FALLBACK_EXIT_CODES = frozenset({0, 130, 143})


def _safe_launch_error(exc: BaseException) -> str:
    """Return one short, redacted launch failure for terminal output."""
    from naumi_agent.safety.guardrails import OutputGuardrail

    raw = str(exc).strip()
    first_line = raw.splitlines()[0] if raw else type(exc).__name__
    return OutputGuardrail.redact(first_line)[:300]

# Friendly tool name mapping for display
_TOOL_ICONS: dict[str, str] = {
    "file_read": "📖",
    "file_write": "📝",
    "file_edit": "✏️",
    "bash_run": "🖥️",
    "code_execute": "⌨️",
    "web_search": "🔍",
    "web_fetch": "🌐",
    "memory_store": "💾",
    "memory_recall": "🧠",
    "delegate_task": "👥",
    "spawn_agent": "🚀",
    "destroy_agent": "🗑️",
    "list_agents": "📋",
    "task_create": "📌",
    "task_update": "🔄",
    "task_list": "📋",
    "task_delete": "🗑️",
    "todo_write": "📋",
    "background_run": "⏱️",
    "background_status": "⏱️",
    "background_list": "📋",
    "background_cancel": "⏹️",
    "background_read_output": "📄",
    "schedule_create": "⏰",
    "schedule_list": "📋",
    "schedule_cancel": "⏹️",
    "schedule_pause": "⏸️",
    "schedule_resume": "▶️",
    "worktree_create": "🌿",
    "worktree_status": "🌿",
    "worktree_bind_task": "🔗",
    "worktree_keep": "📦",
    "worktree_remove": "🧹",
}

# ANSI separators for visual hierarchy
def _sep(thin: bool = True) -> str:
    """Build a terminal-width separator line."""
    char = "─" if thin else "━"
    try:
        width = shutil.get_terminal_size().columns
    except Exception:
        width = 80
    return f"\033[2m{char * width}\033[0m"


def _tool_label(name: str, args: str = "") -> str:
    """Return a friendly display string for a tool call."""
    icon = _TOOL_ICONS.get(name, "⚙️")
    # Extract key argument for context
    hint = ""
    if args:
        import json
        try:
            d = json.loads(args) if isinstance(args, str) else args
            if isinstance(d, dict):
                # Pick the most informative arg
                for key in (
                    "path", "file_path", "command",
                    "query", "url", "task", "description", "goal",
                ):
                    if key in d:
                        val = str(d[key])
                        if len(val) > 50:
                            val = val[:47] + "…"
                        hint = f" {val}"
                        break
        except (json.JSONDecodeError, TypeError):
            pass
    return f"{icon} {name}{hint}"


def _show_cli_status(cli: Any, engine: Any) -> None:
    """Show model, context, budget, git stats in the CLI output area."""
    runtime_mode = getattr(engine, "runtime_mode", None)
    runtime_mode_text = getattr(runtime_mode, "value", str(runtime_mode or "default"))
    if hasattr(cli, "set_mode_status"):
        cli.set_mode_status(runtime_mode_text)
    parts: list[str] = []
    model = engine.router.resolve_model("capable")
    parts.append(model)
    if not hasattr(cli, "set_mode_status"):
        parts.append(f"mode: {runtime_mode_text}")
    workspace_root = getattr(engine, "workspace_root", Path.cwd())
    parts.append(f"工作区: {workspace_root}")
    u = engine.usage
    total_tok = u.total_input_tokens + u.total_output_tokens
    parts.append(f"Token: {total_tok}")
    ctx = engine.get_context_info()
    used_k = ctx["used"] / 1000
    window_k = ctx["window"] / 1000
    parts.append(f"上下文: {used_k:.0f}K/{window_k:.0f}K ({ctx['percentage']}%)")
    budget = engine.get_budget_info()
    parts.append(f"预算: {format_budget_detail(budget)}")
    git = _get_git_info()
    if git["branch"]:
        tag = git["branch"] + ("*" if git["dirty"] else "")
        parts.append(f"📂 {tag}")
    status = " | ".join(parts)
    if hasattr(cli, "set_status"):
        cli.set_status(status)
    else:
        cli.append_output("\033[2m  " + status + "\033[0m\n\n")


def _resolve_config_path(path: str) -> str:
    """Resolve the active project configuration through the shared contract."""
    return resolve_config_path(path)


def _runtime_debug_metadata(
    config: AppConfig,
    resolved_config_path: str,
    engine: Any,
) -> dict[str, str]:
    """Build path metadata shown by /debug and stored in debug-runs."""
    debug_runs_dir = Path(config.memory.session_db_path).parent / "debug-runs"
    return {
        "config_path": str(Path(resolved_config_path).resolve()),
        "config_dir": str(Path(resolved_config_path).resolve().parent),
        "cwd": str(Path.cwd()),
        "workspace_root": str(engine.workspace_root),
        "session_db_path": str(Path(config.memory.session_db_path).resolve()),
        "vector_db_path": str(Path(config.memory.vector_db_path).resolve()),
        "debug_runs_dir": str(debug_runs_dir.resolve()),
        "model": engine.router.resolve_model("capable"),
    }


@contextlib.contextmanager
def _capture_tui_launch_noise() -> Any:
    """Capture startup stdout/stderr and suppress noisy launch-time logs."""
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    noisy_loggers = ("litellm", "LiteLLM", "naumi_agent")
    previous_levels = {
        name: logging.getLogger(name).level for name in noisy_loggers
    }
    try:
        for name in noisy_loggers:
            logging.getLogger(name).setLevel(logging.ERROR)
        with (
            contextlib.redirect_stdout(stdout_buf),
            contextlib.redirect_stderr(stderr_buf),
        ):
            yield stdout_buf, stderr_buf
    finally:
        for name, level in previous_levels.items():
            logging.getLogger(name).setLevel(level)


@app.command("configure")
def configure_command(
    config: str = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径"),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="模型提供商：kimi、openai、anthropic 或 custom",
    ),
    default_model: str | None = typer.Option(None, "--model", help="默认模型覆盖"),
    fast_model: str | None = typer.Option(None, "--fast-model", help="快速模型覆盖"),
    reasoning_model: str | None = typer.Option(
        None,
        "--reasoning-model",
        help="推理模型覆盖",
    ),
    api_base: str | None = typer.Option(None, "--api-base", help="API Base 覆盖"),
    workspace: Path | None = typer.Option(None, "--workspace", help="工作区目录"),
    permission_mode: str | None = typer.Option(
        None,
        "--permission-mode",
        help="权限模式：strict、moderate、relaxed 或 bypass",
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="从标准输入读取模型密钥，避免进入命令历史",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="禁用所有提示，用于自动化",
    ),
) -> None:
    """安全更新模型、凭据、工作区和权限配置."""
    selected_provider = provider
    if not selected_provider:
        if non_interactive:
            console.print("[red]非交互模式必须指定 --provider。[/red]")
            raise typer.Exit(2)
        selected_provider = typer.prompt(
            "模型提供商 (kimi/openai/anthropic/custom)",
            default="kimi",
        )

    if selected_provider.strip().lower() == "custom" and not non_interactive:
        default_model = default_model or typer.prompt("默认模型")
        api_base = api_base or typer.prompt("API Base")
        fast_model = fast_model or typer.prompt("快速模型", default=default_model)
        reasoning_model = reasoning_model or typer.prompt(
            "推理模型",
            default=default_model,
        )

    api_key: str | None = None
    if api_key_stdin:
        api_key = sys.stdin.readline().rstrip("\r\n")
        if not api_key:
            console.print("[red]标准输入中没有模型 API Key。[/red]")
            raise typer.Exit(2)
    elif not non_interactive and typer.confirm("是否更新模型 API Key？", default=True):
        api_key = typer.prompt("模型 API Key", hide_input=True)

    try:
        result = configure_project(
            _resolve_config_path(config),
            provider=selected_provider,
            api_key=api_key,
            default_model=default_model,
            fast_model=fast_model,
            reasoning_model=reasoning_model,
            api_base=api_base,
            workspace=workspace,
            permission_mode=permission_mode,
        )
    except ConfigurationError as exc:
        console.print(f"[red]配置失败：{exc}[/red]")
        raise typer.Exit(2) from exc

    credential_status = "已更新" if result.credential_updated else "保持现有来源"
    console.print("[green]配置已安全更新。[/green]")
    console.print(f"  Provider: {result.provider}")
    console.print(f"  默认模型: {result.default_model}")
    console.print(f"  API Base: {result.api_base}")
    console.print(f"  系统凭据: {credential_status}")
    console.print(f"  配置文件: {result.config_path}")


@app.command("doctor")
def doctor_command(
    config: str = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径"),
    live: bool = typer.Option(
        False,
        "--live",
        help="执行一次最小真实模型请求，会产生少量 token 用量",
    ),
) -> None:
    """诊断本机环境，并可显式验证模型连接."""
    resolved = _resolve_config_path(config)
    app_config = AppConfig.from_yaml(resolved)
    from naumi_agent.model.catalog import load_provider_catalog
    from naumi_agent.model.router import ModelRouter

    model_router = None
    model_router_error = None
    try:
        catalog = (
            load_provider_catalog(app_config.models.catalog_path)
            if app_config.models.catalog_path
            else None
        )
        model_router = ModelRouter(app_config.models, catalog=catalog)
    except Exception as exc:
        model_router_error = str(exc)
    report = asyncio.run(
        run_doctor(
            app_config,
            workspace_root=app_config.resolve_workspace_root(),
            live=live,
            model_router=model_router,
            model_router_error=model_router_error,
        )
    )
    console.print(Markdown(render_doctor_report(report)))
    if report.status == "error":
        raise typer.Exit(1)


@app.command()
def chat(
    config: str = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径"),
    tui: bool = typer.Option(
        False,
        "--tui",
        "-t",
        help="显式启动 Textual TUI fallback",
    ),
) -> None:
    """兼容入口：启动新一代终端 UI。"""
    _ensure_onboarding_ready(config)
    if tui:
        _launch_tui(config)
    else:
        _exit_after_terminal_ui(config)


@app.command("tui")
def textual_ui(
    config: str = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径"),
) -> None:
    """显式启动 Textual TUI fallback。"""
    _ensure_onboarding_ready(config)
    _launch_tui(config)


@app.command("ui")
def terminal_ui(
    config: str = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径"),
    legacy: bool = typer.Option(
        False,
        "--legacy",
        help="弃用别名：显式启动 Textual TUI fallback",
    ),
) -> None:
    """兼容入口：启动新一代终端 UI。"""
    _ensure_onboarding_ready(config)
    if legacy:
        console.print("[yellow]“--legacy” 已弃用，请改用 “naumi tui”。[/yellow]")
        _launch_tui(config)
        return
    _exit_after_terminal_ui(config)


def _exit_after_terminal_ui(config: str) -> None:
    """Launch the preferred interactive UI and translate its exit for Typer."""
    raise typer.Exit(_launch_interactive_ui(config))


def _launch_interactive_ui(config_path: str) -> int:
    """Launch the Node UI and fall back once to Textual on failure."""
    failure: str
    try:
        returncode = _launch_terminal_ui(config_path)
    except (TerminalUiLaunchError, OSError) as exc:
        failure = _safe_launch_error(exc)
    else:
        if returncode in _TERMINAL_UI_NO_FALLBACK_EXIT_CODES:
            return returncode
        failure = f"新终端 UI 异常退出（退出码 {returncode}）"

    console.print(f"[yellow]新终端 UI 启动失败：{failure}[/yellow]")
    console.print("[yellow]正在切换到 Textual TUI。[/yellow]")
    try:
        _launch_tui(config_path)
    except Exception as exc:
        console.print(
            "[red]Textual TUI 也无法启动："
            f"{_safe_launch_error(exc)}[/red]"
        )
        return 1
    return 0


@naumiagent_app.callback(invoke_without_command=True)
def naumiagent_entry(
    tui: bool = typer.Option(
        False,
        "--tui",
        help="显式启动 Textual TUI fallback",
    ),
    config: str = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="配置文件路径",
    ),
) -> None:
    """兼容入口：默认启动新 Terminal UI。"""
    _ensure_onboarding_ready(config)
    if tui:
        _launch_tui(config)
        return
    _exit_after_terminal_ui(config)


def _launch_terminal_ui(config_path: str, *, cwd: Path | None = None) -> int:
    """Launch the next-generation JS terminal UI from the Python CLI."""
    cmd = _build_terminal_ui_command(config_path)
    return subprocess.run(cmd, cwd=str(cwd or Path.cwd()), check=False).returncode


def _resolve_terminal_ui_frontend_dir(
    *,
    frontend_dir: Path | None = None,
    project_root: Path | None = None,
    package_root: Path | None = None,
) -> Path:
    """Resolve the terminal UI runtime directory in source and installed layouts."""
    if frontend_dir is not None:
        entry = frontend_dir / "src" / "index.js"
        if entry.exists():
            return frontend_dir
        raise TerminalUiLaunchError(f"未找到新终端 UI 入口: {entry}")

    source_root = project_root or _PROJECT_ROOT
    installed_root = package_root or Path(__file__).resolve().parent
    candidates = [
        source_root / "frontend" / "terminal-ui",
        installed_root / "frontend" / "terminal-ui",
    ]
    for candidate in candidates:
        if (candidate / "src" / "index.js").exists():
            return candidate

    searched = "\n".join(
        f"- {candidate / 'src' / 'index.js'}" for candidate in candidates
    )
    raise TerminalUiLaunchError(f"未找到新终端 UI 入口，已检查：\n{searched}")


def _build_terminal_ui_command(
    config_path: str,
    *,
    frontend_dir: Path | None = None,
    terminal_ui_executable: Path | None = None,
    frozen_backend_executable: Path | None = None,
    node_executable: str | None = None,
    bridge_python_executable: str | None = None,
    project_root: Path | None = None,
    package_root: Path | None = None,
) -> list[str]:
    """Build the direct Node command for the next-generation terminal UI."""
    packaged_ui = _resolve_packaged_terminal_ui(
        terminal_ui_executable=terminal_ui_executable,
    )
    if packaged_ui is not None:
        backend = frozen_backend_executable or Path(sys.executable)
        bridge_command = [
            str(backend),
            "__ui-bridge",
            "--config",
            config_path,
        ]
        return [
            str(packaged_ui),
            "--config",
            config_path,
            "--bridge-command-json",
            json.dumps(bridge_command, ensure_ascii=False, separators=(",", ":")),
        ]

    frontend = _resolve_terminal_ui_frontend_dir(
        frontend_dir=frontend_dir,
        project_root=project_root,
        package_root=package_root,
    )
    entry = frontend / "src" / "index.js"

    node = node_executable or shutil.which("node")
    if not node:
        raise TerminalUiLaunchError(
            "未找到 Node.js，无法启动新一代终端 UI。请先安装 Node.js 20+。"
        )
    _validate_node_runtime(node)

    bridge_command = [
        bridge_python_executable or sys.executable,
        "-m",
        "naumi_agent.ui.bridge",
        "--config",
        config_path,
    ]
    return [
        node,
        str(entry),
        "--config",
        config_path,
        "--bridge-command-json",
        json.dumps(bridge_command, ensure_ascii=False, separators=(",", ":")),
    ]


def _resolve_packaged_terminal_ui(
    *,
    terminal_ui_executable: Path | None = None,
) -> Path | None:
    """Resolve a compiled UI companion only in explicit or frozen layouts."""
    candidate = terminal_ui_executable
    if candidate is None:
        configured = os.environ.get("NAUMI_TERMINAL_UI_BINARY", "").strip()
        if configured:
            candidate = Path(configured).expanduser()
    if candidate is None and getattr(sys, "frozen", False):
        name = "naumi-ui.exe" if os.name == "nt" else "naumi-ui"
        candidate = Path(sys.executable).resolve().with_name(name)
    if candidate is None:
        return None
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise TerminalUiLaunchError(f"未找到已编译的新终端 UI：{candidate}")
    if os.name != "nt" and not os.access(candidate, os.X_OK):
        raise TerminalUiLaunchError(f"已编译的新终端 UI 不可执行：{candidate}")
    return candidate


def _validate_node_runtime(node_executable: str) -> None:
    """Fail early with a readable message when the terminal UI runtime is too old."""
    try:
        version = subprocess.check_output(
            [node_executable, "--version"],
            stderr=subprocess.STDOUT,
            timeout=5,
            text=True,
        ).strip()
    except Exception as exc:
        raise TerminalUiLaunchError(
            f"无法检测 Node.js 版本，无法启动新一代终端 UI：{exc}"
        ) from exc

    parsed_version = _parse_node_version(version)
    if parsed_version is None:
        raise TerminalUiLaunchError(
            f"无法识别 Node.js 版本输出：{version or '<empty>'}。请安装 Node.js 20.10+。"
        )
    if parsed_version < (20, 10, 0):
        raise TerminalUiLaunchError(
            f"当前 Node.js 版本为 {version}，新一代终端 UI 需要 Node.js 20.10+。"
        )


def _parse_node_major(version: str) -> int | None:
    parsed = _parse_node_version(version)
    if parsed is None:
        return None
    return parsed[0]


def _parse_node_version(version: str) -> tuple[int, int, int] | None:
    normalized = version.strip()
    if normalized.startswith("v"):
        normalized = normalized[1:]
    normalized = normalized.split("-", 1)[0]
    components = normalized.split(".")
    if not 1 <= len(components) <= 3 or not all(
        component.isdigit() for component in components
    ):
        return None
    parsed = [int(component) for component in components]
    parsed.extend([0] * (3 - len(parsed)))
    return parsed[0], parsed[1], parsed[2]


def _launch_tui(config_path: str) -> None:
    from naumi_agent.debug_trace import DebugTrace
    from naumi_agent.log_setup import setup_logging
    from naumi_agent.runtime.composition import create_agent_engine
    from naumi_agent.tui.app import NaumiApp

    resolved = _resolve_config_path(config_path)
    config = AppConfig.from_yaml(resolved)
    config.bind_runtime_workspace(Path.cwd())
    global _show_reasoning_text
    _show_reasoning_text = bool(getattr(config.ui, "show_reasoning", False))
    setup_logging(config.log_level)
    _check_api_key(config)
    with _capture_tui_launch_noise() as (stdout_buf, stderr_buf):
        engine = create_agent_engine(config)
        keybindings = build_keybindings(config.keybindings)
        style_config = _build_ui_style_from_config(config)
    debug_trace = DebugTrace.create(
        interface="tui",
        base_dir=Path(config.memory.session_db_path).parent / "debug-runs",
        metadata=_runtime_debug_metadata(config, resolved, engine),
    )
    startup_noise = stdout_buf.getvalue() + stderr_buf.getvalue()
    if startup_noise.strip():
        debug_trace.output(
            "tui.startup_noise",
            startup_noise,
            hidden=True,
            source="launch",
        )
    app = NaumiApp(
        engine,
        debug_trace=debug_trace,
        keybindings=keybindings,
        style_config=style_config,
        show_reasoning=bool(getattr(config.ui, "show_reasoning", False)),
    )
    app.run()


_show_reasoning_text = False


async def _cli_event_handler(event: str, data: dict[str, Any]) -> None:
    """实时显示 Agent 思考、工具调用过程（fallback for non-CLIApp modes）."""
    if event == "turn_start":
        model = data.get("model", "")
        if model:
            sys.stdout.write(f"\033[2m  ⚙ {model}\033[0m\n")
            sys.stdout.flush()
    elif event == "thinking_delta":
        content = data.get("content", "")
        if content and _show_reasoning_text:
            sys.stdout.write(content)
            sys.stdout.flush()
    elif event == "thinking_start":
        sys.stdout.write(f"\n{_sep()}\n\033[2m💭 思考中...\033[0m\n")
        sys.stdout.flush()
    elif event == "thinking_end":
        sys.stdout.write(f"\033[0m\n{_sep()}\n")
        sys.stdout.flush()
    elif event == "tool_prepare_start":
        sys.stdout.write(f"\033[2m  {format_tool_prepare_status(data)}\033[0m\n")
        sys.stdout.flush()
    elif event == "tool_start":
        name = data.get("name", "?")
        args = data.get("args", "")
        label = _tool_label(name, args)
        sys.stdout.write(f"  {_sep()}\n\033[36m  ⏳ {label}\033[0m\n")
        sys.stdout.flush()
    elif event == "tool_end":
        name = data.get("name", "?")
        status = data.get("status", "?")
        content = data.get("content", "")
        duration = data.get("duration_ms", 0)
        label = _tool_label(name)
        if status == "error":
            console.print(f"\033[31m  ✗ {label} 失败 ({duration:.0f}ms)\033[0m")
        else:
            console.print(f"\033[32m  ✓ {label}\033[0m \033[2m({duration:.0f}ms)\033[0m")
        if content:
            _print_tool_output(name, content)
    elif event == "hook_trace":
        console.print(_format_hook_trace(data))
    elif event == "task_snapshot":
        console.print(_format_task_snapshot(data))
    elif event == "subagent_event":
        console.print(_format_subagent_event(data))
    elif event == "permission_bubble":
        console.print(_format_permission_bubble(data))
    elif event == "team_event":
        console.print(_format_team_event(data))
    elif event == "runtime_notification":
        console.print(_format_runtime_notification(data))
    elif event == "context_compacted":
        console.print(_format_context_compacted(data))
    elif event == "recovery_event":
        console.print(_format_recovery_event(data))
    elif event == "token":
        console.print(data.get("content", ""), end="")
    elif event == "response_start":
        sys.stdout.write(f"{_sep(thin=False)}\n")
        sys.stdout.flush()
    elif event == "response_end":
        console.print()
    elif event == "error":
        console.print(f"[red]错误: {data.get('message', '')}[/red]")


def _print_tool_output(name: str, content: str) -> None:
    """Print tool result with diff highlighting for file edits."""
    renderables: list[Any] = []
    lines = content.split("\n")
    diff_block = _extract_diff_block(content)
    if diff_block is not None:
        prefix, diff_text, suffix = diff_block
        if prefix:
            renderables.append(Text(prefix.rstrip(), style="dim"))
        renderables.append(Syntax(diff_text, "diff", theme="ansi_dark", line_numbers=False))
        if suffix:
            renderables.append(Text(suffix.lstrip(), style="dim"))
    elif _looks_like_diff(lines):
        renderables.append(Syntax(content, "diff", theme="ansi_dark", line_numbers=False))
    elif "```" in content:
        code_block = _extract_code_block(content)
        if code_block is None:
            renderables.append(Text(content, style="dim"))
        else:
            prefix, language, code_text, suffix = code_block
            if prefix:
                renderables.append(Text(prefix.rstrip(), style="dim"))
            code_lines = code_text.splitlines()
            max_lines = 80
            preview = "\n".join(code_lines[:max_lines])
            if len(code_lines) > max_lines:
                preview += f"\n... ({len(code_lines) - max_lines} more code lines)"
            renderables.append(
                Syntax(
                    preview,
                    language or "text",
                    theme="ansi_dark",
                    line_numbers=False,
                    word_wrap=True,
                )
            )
            if suffix:
                renderables.append(Text(suffix.lstrip(), style="dim"))
    elif name in ("file_read",):
        preview = "\n".join(lines[:50])
        if len(lines) > 50:
            preview += f"\n  ... ({len(lines) - 50} more lines)"
        renderables.append(Text(preview, style="dim"))
    else:
        preview = "\n".join(lines[:30])
        if len(lines) > 30:
            preview += f"\n  ... ({len(lines) - 30} more lines)"
        renderables.append(Text(preview, style="dim"))
    console.print(
        Panel(
            Group(*renderables),
            title=f"tool output · {name}",
            border_style="cyan",
            padding=(0, 1),
        )
    )


def _format_hook_trace(data: dict[str, Any]) -> str:
    """Format one hook trace event for user-visible output."""
    point = str(data.get("point", "?"))
    callback = str(data.get("callback", "?"))
    duration = int(data.get("duration_ms", 0) or 0)
    error = str(data.get("error", "") or "")
    aborted = bool(data.get("aborted", False))
    status = "拦截" if aborted else "异常" if error else "触发"
    color = "33" if aborted else "31" if error else "35"
    suffix = f" · {error}" if error else ""
    return (
        f"\033[{color}m  hook {status}: "
        f"{point} → {callback} ({duration}ms){suffix}\033[0m"
    )


def _format_task_snapshot(data: dict[str, Any]) -> str:
    """Format a task snapshot event for user-visible output."""
    source = str(data.get("source", "todo"))
    summary = str(data.get("summary", "当前没有任务。"))
    return f"\033[36m  todo 更新: {source}\033[0m\n{summary}"


def _format_todo_bar(data: dict[str, Any]) -> str:
    """Format a compact bottom todo bar; return empty when all todos are complete."""
    try:
        open_count = int(data.get("open_count", 0) or 0)
        total = int(data.get("count", open_count) or open_count)
        completed = int(data.get("completed_count", max(total - open_count, 0)) or 0)
    except (TypeError, ValueError):
        open_count = 0
        total = 0
        completed = 0
    if open_count <= 0:
        return ""

    raw_items = data.get("items", [])
    items = raw_items if isinstance(raw_items, list) else []
    current: dict[str, Any] | None = None
    priority = {"in_progress": 0, "blocked": 1, "pending": 2}
    for item in items:
        if not isinstance(item, dict):
            continue
        if current is None or priority.get(str(item.get("status")), 99) < priority.get(
            str(current.get("status")),
            99,
        ):
            current = item

    if current is None:
        summary = str(data.get("summary", "") or "")
        first_line = summary.splitlines()[0] if summary else "有未完成任务"
        return f"todo: {completed}/{total} 完成 | {first_line}"

    status = str(current.get("status", "pending"))
    icon = {
        "pending": "○",
        "in_progress": "●",
        "blocked": "!",
    }.get(status, "○")
    task_id = str(current.get("id", "?"))
    subject = str(current.get("subject", "") or "未命名任务")
    return f"todo: {completed}/{total} 完成 | {icon} #{task_id} {subject}"


def _format_subagent_event(data: dict[str, Any]) -> str:
    """Format subagent lifecycle events for user-visible output."""
    status = str(data.get("status", "?"))
    agent = str(data.get("agent_name", "") or "未匹配")
    task_id = str(data.get("task_id", "?"))
    message = str(data.get("message", "") or "")
    color = "32" if status == "completed" else "31" if status in {"error", "failed"} else "36"
    suffix = f" · {message}" if message else ""
    return f"\033[{color}m  subagent {status}: {agent} / {task_id}{suffix}\033[0m"


def _format_permission_bubble(data: dict[str, Any]) -> str:
    """Format subagent permission decisions that bubble to the parent."""
    agent = str(data.get("agent_name", "?"))
    tool = str(data.get("tool_name", "?"))
    status = str(data.get("status", "?"))
    reason = str(data.get("reason", "") or "")
    color = (
        "31"
        if status in {"blocked", "blocked_by_hook", "denied", "confirmation_error"}
        else "32"
        if status in {"confirmed", "bypass_enabled"}
        else "33"
    )
    suffix = f" · {reason[:120]}" if reason else ""
    return f"\033[{color}m  permission bubble: {agent} → {tool} [{status}]{suffix}\033[0m"


def _format_team_event(data: dict[str, Any]) -> str:
    """Format team protocol events for user-visible output."""
    event_type = str(data.get("event_type", "?"))
    sender = str(data.get("sender", "?"))
    recipient = str(data.get("recipient", "") or "广播")
    priority = str(data.get("priority", "normal"))
    message = str(data.get("message", "") or "")
    color = "31" if priority == "critical" else "33" if priority == "high" else "36"
    suffix = f" · {message[:120]}" if message else ""
    return (
        f"\033[{color}m  team {event_type}: "
        f"{sender} → {recipient} [{priority}]{suffix}\033[0m"
    )


def _format_runtime_notification(data: dict[str, Any]) -> str:
    """Format background and scheduler notifications for visible output."""
    title = str(data.get("title", "") or "运行时通知")
    source = str(data.get("source", "runtime"))
    count = int(data.get("count", 0) or 0)
    preview = str(data.get("preview", "") or "").replace("\n", " ")
    suffix = f" · {preview[:160]}" if preview else ""
    return f"\033[36m  {title}: {source} ×{count}{suffix}\033[0m"


def _format_context_compacted(data: dict[str, Any]) -> str:
    """Format context compaction events for user-visible output."""
    before = data.get("before", "?")
    after = data.get("after", "?")
    archived = int(data.get("archived_tool_results", 0) or 0)
    preserved = data.get("preserved_sections", [])
    warnings = data.get("warnings", [])
    if not isinstance(preserved, list):
        preserved = []
    if not isinstance(warnings, list):
        warnings = []
    parts = [f"\033[35m  context compacted: {before} → {after} messages\033[0m"]
    if archived:
        parts.append(f"  归档：{archived} 个大型工具结果")
    if preserved:
        parts.append("  保留：" + "、".join(str(item) for item in preserved))
    if warnings:
        parts.append("  风险：" + "；".join(str(item) for item in warnings))
    return "\n".join(parts)


def _format_recovery_event(data: dict[str, Any]) -> str:
    """Format model recovery events for user-visible output."""
    reason = str(data.get("reason", "?"))
    action = str(data.get("action", "?"))
    phase = str(data.get("phase", "?"))
    before = data.get("before", "?")
    after = data.get("after", "?")
    unit = str(data.get("unit", "messages"))
    color = "32" if phase == "completed" else "31" if phase == "failed" else "33"
    suffix = f" {before} → {after} {unit}" if after != "?" else f" before={before}"
    return f"\033[{color}m  recovery {phase}: {action} ({reason}){suffix}\033[0m"


def _extract_diff_block(content: str) -> tuple[str, str, str] | None:
    """Return prefix, fenced diff body, suffix when content contains ```diff."""
    start = content.find("```diff")
    if start < 0:
        return None
    body_start = content.find("\n", start)
    if body_start < 0:
        return None
    body_start += 1
    end = content.find("```", body_start)
    if end < 0:
        return None
    return content[:start], content[body_start:end].rstrip("\n"), content[end + 3 :]


def _extract_code_block(content: str) -> tuple[str, str, str, str] | None:
    """Return prefix, language, fenced code body, suffix for first code fence.

    Tool previews may be truncated before the closing fence. Treat the remaining
    text as the code body so the preview stays readable instead of showing raw
    Markdown fence markers.
    """
    start = content.find("```")
    if start < 0:
        return None
    header_end = content.find("\n", start)
    if header_end < 0:
        return None
    language = content[start + 3:header_end].strip().split(maxsplit=1)[0]
    body_start = header_end + 1
    end = content.find("```", body_start)
    if end < 0:
        return content[:start], language, content[body_start:].rstrip("\n"), ""
    return (
        content[:start],
        language,
        content[body_start:end].rstrip("\n"),
        content[end + 3:],
    )


def _looks_like_diff(lines: list[str]) -> bool:
    """Detect raw unified diff output, including +foo/-foo lines."""
    sample = [line for line in lines[:12] if line.strip()]
    return any(line.startswith(("---", "+++", "@@")) for line in sample) and any(
        line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        for line in sample
    )


def _ansi_syntax(code: str, language: str, *, theme: str = "ansi_dark") -> str:
    """Render code as ANSI-highlighted text for prompt_toolkit's ANSI parser."""
    lexer = (language or "text").strip().split()[0] or "text"
    has_trailing_newline = code.endswith("\n")
    source = code[:-1] if has_trailing_newline else code
    try:
        from pygments import highlight
        from pygments.formatters import Terminal256Formatter
        from pygments.lexers import get_lexer_by_name

        highlighted = highlight(
            code,
            get_lexer_by_name(lexer, stripnl=False, ensurenl=False),
            Terminal256Formatter(style="native"),
        )
        return highlighted
    except Exception:
        pass
    try:
        rendered = _capture(
            lambda: console.print(
                Syntax(
                    source,
                    lexer,
                    theme=theme,
                    line_numbers=False,
                    word_wrap=False,
                    background_color="default",
                ),
                end="",
            )
        )
        if not has_trailing_newline and rendered.endswith("\n"):
            return rendered[:-1]
        return rendered
    except Exception:
        return code


class _StreamingMarkdownHighlighter:
    """Incrementally color fenced code blocks in streamed Markdown."""

    def __init__(self) -> None:
        self._state = "text"
        self._text_buffer = ""
        self._fence_header = ""
        self._code_buffer = ""
        self._language = "text"
        self._code_line_count = 0
        self._omitted_code_lines = 0

    def reset(self) -> None:
        self.__init__()

    def feed(self, text: str) -> str:
        out: list[str] = []
        remaining = text
        while remaining:
            if self._state == "text":
                self._text_buffer += remaining
                remaining = ""
                out.append(self._drain_text_buffer(complete=False))
            elif self._state == "fence_header":
                newline = remaining.find("\n")
                if newline < 0:
                    self._fence_header += remaining
                    remaining = ""
                else:
                    self._fence_header += remaining[:newline]
                    remaining = remaining[newline + 1:]
                    self._language = self._fence_header.strip() or "text"
                    self._code_line_count = 0
                    self._omitted_code_lines = 0
                    out.append(f"\033[2m```{self._fence_header}\033[0m\n")
                    self._fence_header = ""
                    self._state = "code"
            else:
                self._code_buffer += remaining
                remaining = ""
                out.append(self._drain_code_buffer(complete=False))
        return "".join(out)

    def flush(self) -> str:
        out: list[str] = []
        if self._state == "text":
            out.append(self._drain_text_buffer(complete=True))
        elif self._state == "fence_header":
            out.append(f"```{self._fence_header}")
            self._fence_header = ""
            self._state = "text"
        else:
            out.append(self._drain_code_buffer(complete=True))
        return "".join(out)

    def _drain_text_buffer(self, *, complete: bool) -> str:
        out: list[str] = []
        while True:
            fence = self._text_buffer.find("```")
            if fence < 0:
                if complete:
                    safe_len = len(self._text_buffer)
                else:
                    trailing_ticks = len(self._text_buffer) - len(
                        self._text_buffer.rstrip("`")
                    )
                    safe_len = max(0, len(self._text_buffer) - min(trailing_ticks, 2))
                if safe_len:
                    out.append(self._text_buffer[:safe_len])
                    self._text_buffer = self._text_buffer[safe_len:]
                return "".join(out)

            out.append(self._text_buffer[:fence])
            self._text_buffer = self._text_buffer[fence + 3:]
            newline = self._text_buffer.find("\n")
            if newline < 0:
                self._fence_header = self._text_buffer
                self._text_buffer = ""
                self._state = "fence_header"
                return "".join(out)

            header = self._text_buffer[:newline]
            self._code_buffer += self._text_buffer[newline + 1:]
            self._text_buffer = ""
            self._language = header.strip() or "text"
            self._code_line_count = 0
            self._omitted_code_lines = 0
            self._state = "code"
            out.append(f"\033[2m```{header}\033[0m\n")
            out.append(self._drain_code_buffer(complete=False))

    def _drain_code_buffer(self, *, complete: bool) -> str:
        out: list[str] = []
        while "\n" in self._code_buffer:
            line, self._code_buffer = self._code_buffer.split("\n", 1)
            if line.strip() == "```":
                if self._omitted_code_lines:
                    out.append(self._code_excerpt_marker())
                out.append("\033[2m```\033[0m\n")
                self._text_buffer += self._code_buffer
                self._code_buffer = ""
                self._state = "text"
                self._code_line_count = 0
                self._omitted_code_lines = 0
                out.append(self._drain_text_buffer(complete=False))
                return "".join(out)
            if self._code_line_count < DEFAULT_CODE_BLOCK_MAX_LINES:
                out.append(_ansi_syntax(line + "\n", self._language))
            else:
                self._omitted_code_lines += 1
            self._code_line_count += 1
        if complete and self._code_buffer:
            if self._code_buffer.strip() == "```":
                if self._omitted_code_lines:
                    out.append(self._code_excerpt_marker())
                out.append("\033[2m```\033[0m")
                self._state = "text"
                self._code_line_count = 0
                self._omitted_code_lines = 0
            else:
                if self._code_line_count < DEFAULT_CODE_BLOCK_MAX_LINES:
                    out.append(_ansi_syntax(self._code_buffer, self._language))
                else:
                    self._omitted_code_lines += 1
            self._code_buffer = ""
        if complete and self._omitted_code_lines:
            out.append(self._code_excerpt_marker())
            self._omitted_code_lines = 0
        return "".join(out)

    def _code_excerpt_marker(self) -> str:
        return (
            "\033[2m"
            f"... 已隐藏 {self._omitted_code_lines} 行代码，"
            f"仅展示前 {DEFAULT_CODE_BLOCK_MAX_LINES} 行摘录。"
            "\033[0m\n"
        )


_active_cli: Any = None


def _cli_event_factory(cli: Any):
    """Create event handler that writes to CLIApp instead of stdout.

    Uses EngineEventAdapter + CLIRenderer for typed message dispatch,
    falling back to the legacy if/elif for streaming-only paths
    (markdown highlighting, timing metrics) that need local state.
    """
    import time

    from naumi_agent.cli.renderers import CLIRenderer
    from naumi_agent.ui.messages import EngineEventAdapter

    thinking_started = False
    has_streamed_tokens = False
    model_name = ""
    token_count = 0
    first_token_time = 0.0
    last_token_time = 0.0
    turn_start_time = 0.0
    first_feedback_latency = 0.0
    first_model_chunk_latency = 0.0
    first_token_latency = 0.0
    markdown_highlighter = _StreamingMarkdownHighlighter()
    stream_buffer: list[str] = []
    last_stream_flush_time: float | None = None
    adapter = EngineEventAdapter()
    renderer = CLIRenderer(show_reasoning=_show_reasoning_text)

    def flush_stream_buffer(*, force: bool = False) -> None:
        nonlocal last_stream_flush_time
        if not stream_buffer:
            return
        now = time.monotonic()
        if (
            not force
            and last_stream_flush_time is not None
            and now - last_stream_flush_time + 1e-9 < _CLI_STREAM_FRAME_INTERVAL_SECONDS
        ):
            return
        cli.append_live("".join(stream_buffer))
        stream_buffer.clear()
        last_stream_flush_time = now

    async def handler(event: str, data: dict[str, Any]) -> None:
        nonlocal thinking_started, has_streamed_tokens
        nonlocal model_name, token_count, first_token_time, last_token_time
        nonlocal turn_start_time, last_stream_flush_time
        nonlocal first_feedback_latency, first_model_chunk_latency, first_token_latency
        if hasattr(cli, "record_debug_event"):
            cli.record_debug_event("engine.stream_event", {"event": event, "data": data})

        # --- Streaming-only events that need local state ---
        # Token/response events own their own rendering (markdown highlighter
        # is stateful and lives in this closure).  After handling them we
        # return — the adapter/renderer path would otherwise duplicate output.
        if event == "turn_start":
            model_name = data.get("model", "")
            token_count = 0
            first_token_time = 0.0
            last_token_time = 0.0
            turn_start_time = time.monotonic()
            first_feedback_latency = 0.0
            first_model_chunk_latency = 0.0
            first_token_latency = 0.0
            # Fall through to adapter for ⚙ model display
        elif event == "response_start":
            markdown_highlighter.reset()
            stream_buffer.clear()
            last_stream_flush_time = None
            cli.finalize_live()
            # Fall through to adapter for separator line
        elif event == "token":
            has_streamed_tokens = True
            content = data.get("content", "")
            if content:
                now = time.monotonic()
                if first_token_time == 0.0:
                    first_token_time = now
                last_token_time = now
                token_count += 1
                rendered = markdown_highlighter.feed(content)
                if rendered:
                    stream_buffer.append(rendered)
                    flush_stream_buffer()
            return  # streaming block owns token rendering
        elif event == "response_end":
            tail = markdown_highlighter.flush()
            if tail:
                stream_buffer.append(tail)
            flush_stream_buffer(force=True)
            return  # streaming block owns response_end rendering

        flush_stream_buffer(force=True)

        # --- Adapter-driven rendering for all other events ---
        msg = adapter.adapt(event, data)
        if msg is None:
            return

        # Status bar / activity bar / todo bar updates (non-rendering side-effects)
        from naumi_agent.ui.messages.events import (
            ErrorMessage,
            RuntimeStatusMessage,
            TodoStatusMessage,
            ToolPrepareMessage,
        )
        if isinstance(msg, ErrorMessage):
            # Errors finalize the live area so the message is clearly visible
            cli.finalize_live()
        elif isinstance(msg, RuntimeStatusMessage):
            if hasattr(cli, "set_activity_status"):
                if msg.phase == "perf_phase":
                    cli.set_activity_status(
                        f"{msg.label}: {msg.duration_ms}ms"
                    )
                elif msg.phase == "latency_metric":
                    metric = str(data.get("metric", ""))
                    seconds = msg.duration_ms / 1000
                    if metric == "first_progress":
                        first_feedback_latency = seconds
                    elif metric == "first_model_chunk":
                        first_model_chunk_latency = seconds
                    elif metric == "first_token":
                        first_token_latency = seconds
                    cli.set_activity_status(
                        f"{msg.label}: {msg.duration_ms}ms"
                    )
        elif isinstance(msg, ToolPrepareMessage):
            if hasattr(cli, "set_activity_status"):
                if msg.phase == "end":
                    cli.set_activity_status(None)
                else:
                    parts = [f"准备 {msg.tool_name}"]
                    if msg.path:
                        parts.append(msg.path)
                    if msg.content_lines and msg.content_chars:
                        parts.append(
                            f"内容 {msg.content_lines} 行"
                        )
                    elif msg.argument_chars:
                        parts.append(
                            f"参数 {msg.argument_chars} 字符"
                        )
                    if msg.elapsed_ms >= 1000:
                        parts.append(f"{msg.elapsed_ms / 1000:.1f}s")
                    cli.set_activity_status(" · ".join(parts))
                return  # activity bar owns the display — skip renderer
        elif isinstance(msg, TodoStatusMessage):
            if hasattr(cli, "set_todo_status"):
                cli.set_todo_status(_format_todo_bar(data) or None)

        # Main rendering via registry
        ansi_text = renderer.render(msg)
        if ansi_text is not None:
            cli.append_live(ansi_text)

    def _get_model() -> str:
        return model_name

    def _get_token_speed() -> float:
        if token_count > 0 and first_token_time > 0 and last_token_time > first_token_time:
            return token_count / (last_token_time - first_token_time)
        return 0.0

    def _get_ttft() -> float:
        """End-to-end time to first token (seconds)."""
        if first_token_latency > 0:
            return first_token_latency
        if first_token_time > 0 and turn_start_time > 0:
            return first_token_time - turn_start_time
        return 0.0

    def _get_first_feedback() -> float:
        return first_feedback_latency

    def _get_first_model_chunk() -> float:
        return first_model_chunk_latency

    def _get_duration() -> float:
        """Total response duration (seconds)."""
        if turn_start_time > 0:
            end = last_token_time if last_token_time > 0 else time.monotonic()
            return end - turn_start_time
        return 0.0

    handler._has_streamed_tokens = lambda: has_streamed_tokens
    handler._get_model = _get_model
    handler._get_token_speed = _get_token_speed
    handler._get_ttft = _get_ttft
    handler._get_first_feedback = _get_first_feedback
    handler._get_first_model_chunk = _get_first_model_chunk
    handler._get_duration = _get_duration
    return handler


async def _chat(config_path: str) -> None:
    from naumi_agent.cli.layout import CLIApp
    from naumi_agent.debug_trace import DebugTrace
    from naumi_agent.log_setup import setup_logging
    from naumi_agent.runtime.composition import create_agent_engine

    resolved = _resolve_config_path(config_path)
    config = AppConfig.from_yaml(resolved)
    setup_logging(config.log_level)
    _check_api_key(config)
    engine = create_agent_engine(config)
    reconciliation_recovery = await engine.recover_session_reconciliations()
    keybindings = build_keybindings(config.keybindings)
    style_config = _build_ui_style_from_config(config)
    debug_trace = DebugTrace.create(
        interface="cli",
        base_dir=Path(config.memory.session_db_path).parent / "debug-runs",
        metadata=_runtime_debug_metadata(config, resolved, engine),
    )

    cli = CLIApp(debug_trace=debug_trace, keybindings=keybindings, style_config=style_config)
    engine.set_permission_confirmer(cli.confirm_permission)

    def toggle_runtime_mode() -> str:
        mode = engine.cycle_runtime_mode()
        cli.record_debug_event("cli.runtime_mode_changed", {"mode": mode.value})
        return mode.value

    cli.set_mode_toggle_handler(toggle_runtime_mode)
    global _active_cli
    _active_cli = cli

    cli.append_output(_render_startup_banner(engine))
    if reconciliation_recovery:
        completed = sum(
            result.outcome.value == "completed" for result in reconciliation_recovery
        )
        cli.append_output(
            f"启动恢复：处理 {len(reconciliation_recovery)} 个会话协调任务，"
            f"完成 {completed} 个。"
        )

    # Inject git info into prompt prefix
    git = _get_git_info()
    if git["branch"]:
        cli.set_git_info(git["branch"], git["dirty"])

    # Show startup stats line immediately
    _show_cli_status(cli, engine)

    async def on_submit(text: str) -> None:
        if text in ("/quit", "/q", "/exit", "exit"):
            cli.record_debug_event("cli.exit_requested", {"text": text})
            await engine.shutdown()
            debug_trace.close()
            cli.exit()
            return

        cli.append_output(f"\033[32m❯\033[0m {text}\n")

        if text.startswith("/"):
            cli.record_debug_event("cli.command_start", {"command": text})
            output = await execute_slash_command(engine, text)
            cli.append_output(output)
            cli.record_debug_event("cli.command_end", {"command": text, "output": output})
            return

        # Suppress log noise during streaming
        logging.getLogger("litellm").setLevel(logging.ERROR)
        logging.getLogger("LiteLLM").setLevel(logging.ERROR)
        logging.getLogger("naumi_agent").setLevel(logging.ERROR)

        event_handler = _cli_event_factory(cli)
        cli.record_debug_event("cli.agent_run_start", {"task": text})
        result = await engine.run_streaming(
            text,
            CallbackEventSink(event_handler),
        )
        cli.record_debug_event(
            "cli.agent_run_end",
            {
                "status": result.status,
                "response": result.response,
                "error": result.error,
                "usage": result.usage,
            },
        )

        # Restore log levels
        logging.getLogger("litellm").setLevel(logging.WARNING)
        logging.getLogger("LiteLLM").setLevel(logging.WARNING)
        logging.getLogger("naumi_agent").setLevel(logging.INFO)

        # Finalize any remaining live content
        cli.finalize_live()
        skip_response = event_handler._has_streamed_tokens()
        cli.append_output(
            _capture(
                lambda: _render_result(
                    console,
                    result,
                    skip_response=skip_response,
                    model=event_handler._get_model(),
                    token_speed=event_handler._get_token_speed(),
                    first_feedback=event_handler._get_first_feedback(),
                    first_model_chunk=event_handler._get_first_model_chunk(),
                    ttft=event_handler._get_ttft(),
                    duration=event_handler._get_duration(),
                    engine=engine,
                    show_environment_stats=False,
                )
            )
        )
        _show_cli_status(cli, engine)

    cli.set_submit_handler(on_submit)
    try:
        await cli.run()
    finally:
        debug_trace.close()
        await engine.shutdown()


@app.command()
def run(
    task: str = typer.Argument(help="要执行的任务"),
    config: str = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径"),
) -> None:
    """执行单个任务."""
    asyncio.run(_run_task(task, config))


async def _run_task(task: str, config_path: str) -> None:
    from naumi_agent.log_setup import setup_logging
    from naumi_agent.runtime.composition import create_agent_engine

    resolved = _resolve_config_path(config_path)
    config = AppConfig.from_yaml(resolved)
    setup_logging(config.log_level)
    _check_api_key(config)
    engine = create_agent_engine(config)

    try:
        await engine.recover_session_reconciliations()
        with console.status("[bold green]执行中...[/bold green]"):
            result = await engine.run(task)
    except Exception as e:
        console.print(f"[red]错误: {e}[/red]")
        return
    finally:
        await engine.shutdown()

    console.print(Markdown(excerpt_markdown_code_blocks(result.response)))
    console.print()

    # Show stats line
    stats = Text()
    model = engine.router.resolve_model("capable")
    stats.append(f"{model}", style="dim")
    stats.append(" | ", style="dim")
    u = result.usage
    total_tok = u.total_input_tokens + u.total_output_tokens
    stats.append(f"Token: {total_tok}", style="dim")
    stats.append(" | ", style="dim")
    stats.append(f"费用: ${u.total_cost_usd:.4f}", style="dim")
    stats.append(" | ", style="dim")
    stats.append(f"轮次: {u.turns}", style="dim")
    if result.status != "completed":
        stats.append(f" | 状态: {result.status}", style="yellow")
    console.print(stats)
    console.print()


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        "-h",
        help="监听地址（默认 127.0.0.1，仅本地访问；如需暴露网络请显式指定 0.0.0.0）",
    ),
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="监听端口（默认 8765，与 Mac Workbench 本地 daemon 一致；支持 --port 显式覆盖）",
    ),
    config: str = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径"),
    reload: bool = typer.Option(False, "--reload", help="开发模式热重载"),
) -> None:
    """启动 REST API 服务."""
    import uvicorn

    resolved_config = _resolve_config_path(config)
    os.environ["NAUMI_CONFIG"] = resolved_config

    cfg = AppConfig.from_yaml(resolved_config)
    final_port = port if port is not None else cfg.api.port

    if reload:
        uvicorn.run(
            "naumi_agent.api.app:app",
            host=host,
            port=final_port,
            reload=True,
            reload_dirs=["src/naumi_agent"],
        )
    else:
        uvicorn.run(
            "naumi_agent.api.app:app",
            host=host,
            port=final_port,
            workers=1,
            log_level="info",
        )


def _capture(func: Any) -> str:
    """Capture console output as ANSI text."""
    buf = io.StringIO()
    width = shutil.get_terminal_size().columns
    c = Console(
        file=buf,
        force_terminal=True,
        color_system="standard",
        legacy_windows=False,
        width=width,
    )
    import naumi_agent.main as _self

    orig = _self.console
    _self.console = c
    try:
        func()
    finally:
        _self.console = orig
    return buf.getvalue()


def _render_startup_banner(engine: Any) -> str:
    """Render the opening banner as a persistent transcript entry."""
    return _capture(lambda: _print_banner(engine))


async def _capture_async(func: Any) -> str:
    """Capture console output from an async function as ANSI text."""
    buf = io.StringIO()
    width = shutil.get_terminal_size().columns
    c = Console(
        file=buf,
        force_terminal=True,
        color_system="standard",
        legacy_windows=False,
        width=width,
    )
    import naumi_agent.main as _self

    orig = _self.console
    _self.console = c
    try:
        await func()
    finally:
        _self.console = orig
    return buf.getvalue()


def _render_result(
    c: Console,
    result: Any,
    *,
    skip_response: bool = False,
    model: str = "",
    token_speed: float = 0.0,
    first_feedback: float = 0.0,
    first_model_chunk: float = 0.0,
    ttft: float = 0.0,
    duration: float = 0.0,
    engine: Any = None,
    show_environment_stats: bool = True,
) -> None:
    if result.status == "error" and result.error:
        c.print(f"[red]错误: {result.error}[/red]")
        return

    if result.response and not skip_response:
        c.print()
        c.print(
            Panel(
                Markdown(excerpt_markdown_code_blocks(result.response)),
                title="[bold green]NaumiAgent[/bold green]",
                border_style="green",
                padding=(1, 2),
            )
        )

    # --- Line 1: Model | Turns | Tokens (speed) | Cache | TTFT | Duration ---
    line1 = Text()
    if model:
        line1.append(model, style="dim")
        line1.append(" | ", style="dim")
    line1.append(f"轮次: {result.usage.turns}", style="dim")
    line1.append(" | ", style="dim")
    inp = result.usage.total_input_tokens
    out = result.usage.total_output_tokens
    line1.append(f"↑{inp}", style="cyan")
    line1.append(" ", style="dim")
    line1.append(f"↓{out}", style="green")
    if token_speed > 0:
        line1.append(f" ({token_speed:.1f} tok/s)", style="dim")
    if result.usage.cache_tokens > 0:
        line1.append(" | ", style="dim")
        line1.append(f"缓存: {result.usage.cache_tokens}", style="dim")
    if first_feedback > 0:
        line1.append(" | ", style="dim")
        line1.append(f"首反馈: {first_feedback:.1f}s", style="dim")
    if first_model_chunk > 0:
        line1.append(" | ", style="dim")
        line1.append(f"首包: {first_model_chunk:.1f}s", style="dim")
    if ttft > 0:
        line1.append(" | ", style="dim")
        line1.append(f"首字: {ttft:.1f}s", style="dim")
    if duration > 0:
        line1.append(" | ", style="dim")
        line1.append(f"耗时: {duration:.1f}s", style="dim")
    if result.status != "completed":
        line1.append(f" | 状态: {result.status}", style="yellow")
    c.print(line1)

    # --- Line 2: Context | Budget | Git ---
    line2 = Text()
    has_line2 = False

    if engine and show_environment_stats:
        # Context window
        ctx = engine.get_context_info()
        ctx_pct = ctx["percentage"]
        used_k = ctx["used"] / 1000
        window_k = ctx["window"] / 1000
        ctx_style = "yellow" if ctx_pct > 80 else "dim"
        line2.append(f"上下文: {used_k:.0f}K/{window_k:.0f}K ({ctx_pct}%)", style=ctx_style)
        has_line2 = True

        # Budget
        budget = engine.get_budget_info()
        budget_pct = budget["percentage"]
        budget_style = (
            "yellow"
            if isinstance(budget_pct, int | float) and budget_pct > 80
            else "dim"
        )
        line2.append(" | ", style="dim")
        line2.append(
            f"预算: {format_budget_detail(budget)}",
            style=budget_style,
        )
        has_line2 = True

    if show_environment_stats:
        # Cost on line 2 as well
        line2.append(" | ", style="dim")
        line2.append(f"费用: ${result.usage.total_cost_usd:.4f}", style="dim")
        has_line2 = True

    # Git
    if show_environment_stats:
        git = _get_git_info()
        if git["branch"]:
            git_label = git["branch"]
            if git["dirty"]:
                git_label += "*"
            line2.append(" | ", style="dim")
            line2.append(f"📂 {git_label}", style="dim")
            has_line2 = True

    if has_line2:
        c.print(line2)
    c.print()


def _parse_bool_arg(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "on", "yes", "y"}:
        return True
    if value in {"0", "false", "off", "no", "n"}:
        return False
    raise ValueError(f"布尔参数无效: {raw}")


def _parse_int_arg(raw: str, *, name: str, default: int | None = None) -> int:
    if raw == "":
        if default is not None:
            return default
        raise ValueError(f"{name} 参数不能为空")
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 需为整数: {raw}") from exc


def _normalize_tool_kv_tokens(raw: str) -> tuple[dict[str, str], list[str]]:
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"参数解析失败: {exc}")

    kv: dict[str, str] = {}
    args: list[str] = []
    for token in tokens:
        if token.startswith("--") and "=" in token:
            key, value = token[2:].split("=", 1)
            kv[key.strip().lower()] = value
        elif "=" in token:
            key, value = token.split("=", 1)
            kv[key.strip().lower()] = value
        else:
            args.append(token)
    return kv, args


def _build_tool_arg_for_glob(arg: str) -> dict[str, Any]:
    kv, args = _normalize_tool_kv_tokens(arg)
    pattern = kv.get("pattern") or (args[0] if args else "")
    if not pattern:
        raise ValueError("用法: /glob <pattern> [directory='.' ]")

    directory = kv.get("directory") or kv.get("path") or (args[1] if len(args) > 1 else ".")
    parsed: dict[str, Any] = {"pattern": pattern, "directory": directory}

    if "limit" in kv:
        parsed["limit"] = _parse_int_arg(kv["limit"], name="limit")
    if "include_hidden" in kv:
        parsed["include_hidden"] = _parse_bool_arg(kv["include_hidden"])
    if "hidden" in kv:
        parsed["include_hidden"] = _parse_bool_arg(kv["hidden"])
    return parsed


def _build_tool_arg_for_grep(arg: str) -> dict[str, Any]:
    kv, args = _normalize_tool_kv_tokens(arg)
    pattern = kv.get("pattern") or (args[0] if args else "")
    if not pattern:
        raise ValueError("用法: /grep <pattern> [path='.']")

    parsed: dict[str, Any] = {
        "pattern": pattern,
        "path": kv.get("path") or (args[1] if len(args) > 1 else "."),
    }
    if "glob" in kv:
        parsed["glob"] = kv["glob"]
    if "file_type" in kv:
        parsed["file_type"] = kv["file_type"]
    if "literal" in kv:
        parsed["literal"] = _parse_bool_arg(kv["literal"])
    if "case_sensitive" in kv:
        parsed["case_sensitive"] = _parse_bool_arg(kv["case_sensitive"])
    if "max_matches" in kv:
        parsed["max_matches"] = _parse_int_arg(kv["max_matches"], name="max_matches")
    return parsed


def _build_tool_arg_for_read(arg: str) -> dict[str, Any]:
    kv, args = _normalize_tool_kv_tokens(arg)
    path = kv.get("path") or (args[0] if args else "")
    if not path:
        raise ValueError("用法: /read <path> [offset=0] [limit=-1]")

    parsed = {"path": path}
    if "offset" in kv:
        parsed["offset"] = _parse_int_arg(kv["offset"], name="offset", default=0)
    elif len(args) > 1:
        parsed["offset"] = _parse_int_arg(args[1], name="offset")
    if "limit" in kv:
        parsed["limit"] = _parse_int_arg(kv["limit"], name="limit", default=-1)
    elif len(args) > 2:
        parsed["limit"] = _parse_int_arg(args[2], name="limit")
    return parsed


def _build_tool_arg_for_file_write(arg: str) -> dict[str, Any]:
    kv, args = _normalize_tool_kv_tokens(arg)
    path = kv.get("path") or (args[0] if args else "")
    if not path:
        raise ValueError("用法: /file_write <path> <content>")

    if "content" in kv:
        content = kv["content"]
    else:
        if len(args) < 2:
            raise ValueError("用法: /file_write <path> <content>")
        content = " ".join(args[1:])
    return {"path": path, "content": content}


def _build_tool_arg_for_file_edit(arg: str) -> dict[str, Any]:
    kv, args = _normalize_tool_kv_tokens(arg)
    path = kv.get("path") or (args[0] if args else "")
    if not path:
        raise ValueError("用法: /file_edit <path> <old_text> <new_text>")

    if "old_text" in kv and "new_text" in kv:
        old_text = kv["old_text"]
        new_text = kv["new_text"]
    else:
        if len(args) < 3:
            raise ValueError("用法: /file_edit <path> <old_text> <new_text>")
        old_text = args[1]
        new_text = " ".join(args[2:])
    return {"path": path, "old_text": old_text, "new_text": new_text}


_SLASH_TOOL_COMMANDS: dict[str, tuple[str, Any]] = {
    "/glob": ("glob", _build_tool_arg_for_glob),
    "/grep": ("grep", _build_tool_arg_for_grep),
    "/read": ("file_read", _build_tool_arg_for_read),
    "/file_read": ("file_read", _build_tool_arg_for_read),
    "/write": ("file_write", _build_tool_arg_for_file_write),
    "/file_write": ("file_write", _build_tool_arg_for_file_write),
    "/edit": ("file_edit", _build_tool_arg_for_file_edit),
    "/file_edit": ("file_edit", _build_tool_arg_for_file_edit),
}


async def _run_tool_slash_command(
    engine: Any,
    *,
    slash_command: str,
    tool_name: str,
    parse_args: Any,
    arg: str,
) -> None:
    from naumi_agent.tools.base import ToolCall

    try:
        kwargs = parse_args(arg)
    except ValueError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return

    tool = engine.tool_registry.get(tool_name)
    if tool is None:
        console.print(f"[red]工具未注册: {tool_name}[/red]")
        return

    tool_call = ToolCall(
        id=f"slash-{slash_command}-{uuid.uuid4()}",
        name=tool_name,
        arguments=json.dumps(kwargs, ensure_ascii=False),
    )
    result = await engine.execute_tool(tool_call, agent_name="cli")
    if result.status != "success":
        console.print(f"[yellow]{result.content}[/yellow]")
        return
    if result.content:
        console.print(result.content)
    else:
        console.print(f"[green]命令已执行: {slash_command}[/green]")


def _parse_models_command_arg(arg: str) -> tuple[str | None, bool] | None:
    try:
        parts = shlex.split(arg)
    except ValueError:
        return None
    refresh = False
    provider_id: str | None = None
    for part in parts:
        if part == "--refresh":
            if refresh:
                return None
            refresh = True
            continue
        if part.startswith("--") or provider_id is not None:
            return None
        provider_id = part.strip().lower() or None
    return provider_id, refresh


async def _show_available_models(engine: Any, arg: str) -> None:
    from naumi_agent.model.discovery import ModelDiscoveryError

    parsed = _parse_models_command_arg(arg)
    if parsed is None:
        console.print(
            "用法: /models [provider] [--refresh]",
            style="yellow",
            markup=False,
        )
        return
    provider_id, refresh = parsed
    try:
        listings = await engine.router.list_available_models(
            provider_id,
            refresh=refresh,
        )
    except ModelDiscoveryError as exc:
        console.print(str(exc), style="yellow", markup=False)
        return

    if not listings:
        console.print("[yellow]当前未配置 provider catalog，无法获取模型列表。[/yellow]")
        return

    for index, listing in enumerate(listings):
        if index:
            console.print()
        cache_label = "旧缓存" if listing.stale else "已刷新"
        cache_style = "yellow" if listing.stale else "green"
        heading = Text()
        heading.append(
            f"{listing.provider_name} ({listing.provider_id})",
            style="bold",
        )
        heading.append(f" {cache_label}", style=cache_style)
        console.print(heading)
        visible = listing.models[:100]
        if not visible:
            console.print("  [dim]没有可用模型[/dim]")
        for model in visible:
            source = "静态" if model.source == "static" else "发现"
            name = f" — {model.name}" if model.name != model.id else ""
            row = Text("  • ")
            row.append(model.canonical_id, style="cyan")
            row.append(name)
            row.append(f" [{source}]", style="dim")
            if model.reasoning_efforts:
                efforts = "/".join(value.value for value in model.reasoning_efforts)
                row.append(f" [强度 {efforts}", style="magenta")
                if model.default_reasoning_effort is not None:
                    row.append(
                        f" · 默认 {model.default_reasoning_effort.value}",
                        style="dim",
                    )
                row.append("]", style="magenta")
            console.print(row)
        omitted = len(listing.models) - len(visible)
        if omitted:
            console.print(f"  [dim]另有 {omitted} 个模型未显示[/dim]")
        if listing.warning:
            console.print(
                f"  警告: {listing.warning}",
                style="yellow",
                markup=False,
            )


async def _handle_command(engine: Any, cmd: str) -> None:
    """处理斜杠命令."""
    parts = cmd.strip().split(maxsplit=1)
    if not parts:
        return
    command = parts[0].lower()
    if command == "/":
        command = "/help"
    arg = parts[1] if len(parts) > 1 else ""

    if command in _SLASH_TOOL_COMMANDS:
        tool_name, parse_args = _SLASH_TOOL_COMMANDS[command]
        await _run_tool_slash_command(
            engine,
            slash_command=command,
            tool_name=tool_name,
            parse_args=parse_args,
            arg=arg,
        )
        return

    match command:
        case "/h" | "/help":
            _print_help()
        case "/keybindings" | "/keys":
            if _active_cli and hasattr(_active_cli, "keybinding_help"):
                console.print(Markdown(_active_cli.keybinding_help()))
            else:
                config = getattr(engine, "_config", None)
                keybindings = build_keybindings(
                    getattr(config, "keybindings", {}) if config is not None else {}
                )
                console.print(Markdown(render_keybinding_help(keybindings, interface="cli")))
        case "/style" | "/theme":
            config = getattr(engine, "_config", None)
            console.print(Markdown(_build_style_help_text(config)))
        case "/reasoning":
            _handle_reasoning_command(engine, arg)
        case "/effort":
            _handle_effort_command(engine, arg)
        case "/doctor":
            report = await run_doctor(
                engine._config,
                workspace_root=getattr(engine, "workspace_root", Path.cwd()),
                mcp_manager=getattr(engine, "_mcp_manager", None),
                model_router=engine.router,
            )
            console.print(Markdown(render_doctor_report(report)))
        case "/harness":
            await _run_harness(engine, arg)
        case "/debug":
            if _active_cli and hasattr(_active_cli, "debug_info"):
                console.print(_active_cli.debug_info())
            else:
                console.print("[yellow]当前界面未暴露调试日志路径[/yellow]")
        case "/debug-replay":
            from naumi_agent.debug_trace import find_latest_run, render_debug_replay

            if arg:
                replay_target = Path(arg)
            else:
                replay_base = Path(engine._config.memory.session_db_path).parent / "debug-runs"
                replay_target = find_latest_run(replay_base) or replay_base
            console.print(render_debug_replay(replay_target))
        case "/permissions":
            from naumi_agent.ui.permission_panel import render_permission_panel

            if arg:
                console.print("[yellow]权限面板不接受参数，已忽略额外参数。[/yellow]")
            pending = {}
            confirmer = getattr(engine, "_permission_confirmer", None)
            try:
                raw_pending = getattr(confirmer, "pending_permissions", {})
                if isinstance(raw_pending, dict):
                    pending = {
                        str(key): dict(value)
                        for key, value in raw_pending.items()
                        if isinstance(value, dict)
                    }
            except Exception:
                pending = {}

            panel = render_permission_panel(
                engine,
                pending=pending,
                limit=12,
            )
            console.print(Markdown(panel))
        case "/diff":
            from rich.text import Text

            from naumi_agent.ui.diff_viewer import render_git_diff_viewer

            scope = arg.strip() or "all"
            workspace_root = getattr(engine, "workspace_root", Path.cwd())
            console.print(
                Text.from_ansi(
                    render_git_diff_viewer(
                        workspace_root,
                        scope=scope,
                        style_config=_build_ui_style_from_config(getattr(engine, "_config", None)),
                    )
                )
            )
        case "/hooks":
            _show_hooks(engine)
        case "/copy":
            if _active_cli:
                _active_cli.copy_transcript(arg or "all")
            else:
                console.print("[yellow]当前界面不支持复制完整记录[/yellow]")
        case "/pwd":
            workspace_root = getattr(engine, "workspace_root", Path.cwd())
            console.print(f"工作区根目录: [cyan]{workspace_root}[/cyan]")
            console.print(f"启动目录: [dim]{Path.cwd()}[/dim]")
            config = getattr(engine, "_config", None)
            if config is not None:
                console.print(f"会话库: [dim]{Path(config.memory.session_db_path).resolve()}[/dim]")
                console.print(
                    "[dim]完整调试路径可用 /debug 查看[/dim]"
                )
        case "/skills":
            _show_skills(engine)
        case "/tools" | "/t":
            tools = engine.tool_registry.all()
            console.print("[bold]可用工具:[/bold]")
            for t in tools:
                console.print(f"  • [cyan]{t.name}[/cyan] — {t.description}")
        case "/q" | "/quit" | "/exit":
            console.print("[green]退出命令已接收，请关闭界面或发送新会话。[/green]")
        case "/clear" | "/c":
            engine.reset()
            if _active_cli:
                _active_cli.clear_output()
                _show_cli_status(_active_cli, engine)
            console.print("[green]会话已清除[/green]")
        case "/new" | "/n":
            await _new_conversation(engine)
            if _active_cli:
                _show_cli_status(_active_cli, engine)
        case "/usage" | "/u":
            u = engine.usage
            console.print(
                f"Token: {u.total_input_tokens + u.total_output_tokens} | "
                f"费用: ${u.total_cost_usd:.4f} | "
                f"轮次: {u.turns}"
            )
        case "/model" | "/m":
            console.print(f"默认模型: {engine.router.resolve_model('capable')}")
            console.print(f"快速模型: {engine.router.resolve_model('fast')}")
            console.print(f"推理模型: {engine.router.resolve_model('reasoning')}")
            _print_reasoning_effort_status(
                engine.router.get_reasoning_effort_status()
            )
        case "/models":
            await _show_available_models(engine, arg)
        case "/version" | "/v":
            from naumi_agent import __version__

            console.print(f"[bold green]NaumiAgent[/bold green] v{__version__}")
        case "/history":
            await _show_history(engine, arg)
        case "/memory":
            await _handle_memory(engine, arg)
        case "/load" | "/l":
            await _interactive_load(engine, arg)
        case "/resume" | "/r":
            await _resume_latest(engine)
        case "/delete":
            if not arg:
                console.print("[yellow]用法: /delete <session_id>[/yellow]")
                console.print("[dim]使用 /history 查看会话列表[/dim]")
            else:
                await _delete_session(engine, arg)
        case "/chaos":
            await _run_analysis(engine, "chaos", arg or "当前项目")
        case "/scale":
            scale_target, scale_qps = _parse_scale_arg(arg)
            await _run_analysis(engine, "scale", scale_target, qps=scale_qps)
        case "/state":
            await _run_analysis(engine, "state", arg or "当前项目")
        case "/vibe":
            if not arg:
                console.print("[yellow]用法: /vibe <功能描述>[/yellow]")
            else:
                await _run_analysis(engine, "vibe", arg)
        case "/eval":
            if not arg:
                console.print("[yellow]用法: /eval <文件或目录路径>[/yellow]")
                console.print("[dim]例: /eval src/naumi_agent/orchestrator/[/dim]")
            else:
                await _run_analysis(engine, "eval", arg)
        case "/page":
            await _run_analysis(engine, "page", "memory")
        case "/heal":
            if not arg:
                console.print(
                    "[yellow]用法: /heal <错误日志或错误描述>[/yellow]"
                )
                console.print("[dim]例: /heal \"TypeError: unsupported operand\"[/dim]")
            else:
                await _run_analysis(engine, "heal", arg)
        case "/dspy":
            await _run_analysis(engine, "dspy", arg or "")
        case "/graph":
            await _run_analysis(engine, "graph", arg or "")
        case "/mcts":
            if not arg:
                console.print(
                    "[yellow]用法: /mcts <问题描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /mcts \"如何设计一个高可用的分布式锁\"[/dim]"
                )
            else:
                await _run_analysis(engine, "mcts", arg)
        case "/route":
            if not arg:
                console.print(
                    "[yellow]用法: /route <任务描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /route \"设计一个AI股票分析系统\"[/dim]"
                )
            else:
                await _run_analysis(engine, "route", arg)
        case "/speculate":
            if not arg:
                console.print(
                    "[yellow]用法: /speculate <文件或目录路径>[/yellow]"
                )
                console.print(
                    "[dim]例: /speculate src/naumi_agent/orchestrator/[/dim]"
                )
            else:
                await _run_analysis(engine, "speculate", arg)
        case "/jit":
            if not arg:
                console.print(
                    "[yellow]用法: /jit <计算任务描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /jit \"计算斐波那契数列第100项\"[/dim]"
                )
            else:
                await _run_analysis(engine, "jit", arg)
        case "/pointer":
            if not arg:
                console.print(
                    "[yellow]用法: /pointer <文件或目录路径>[/yellow]"
                )
                console.print(
                    "[dim]例: /pointer src/naumi_agent/tools/[/dim]"
                )
            else:
                await _run_analysis(engine, "pointer", arg)
        case "/cooe":
            if not arg:
                console.print(
                    "[yellow]用法: /cooe <多步骤任务描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /cooe \"生成宁德时代深度投资研报\"[/dim]"
                )
            else:
                await _run_analysis(engine, "cooe", arg)
        case "/sleep":
            await _run_analysis(engine, "sleep", arg or "")
        case "/entropy":
            if not arg:
                console.print("[yellow]用法: /entropy <长文本或上下文>[/yellow]")
            else:
                await _run_analysis(engine, "entropy", arg)
        case "/ooda":
            if not arg:
                console.print("[yellow]用法: /ooda <文件或目录路径>[/yellow]")
            else:
                await _run_analysis(engine, "ooda", arg)
        case "/probe":
            if not arg:
                console.print("[yellow]用法: /probe <功能需求描述>[/yellow]")
            else:
                await _run_analysis(engine, "probe", arg)
        case "/vision":
            if not arg:
                console.print("[yellow]用法: /vision <数据提取目标描述>[/yellow]")
            else:
                await _run_analysis(engine, "vision", arg)
        case "/spar":
            if not arg:
                console.print(
                    "[yellow]用法: /spar <目标代码路径或功能描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /spar src/naumi_agent/tools/[/dim]"
                )
            else:
                await _run_analysis(engine, "spar", arg)
        case "/world":
            if not arg:
                console.print(
                    "[yellow]用法: /world <代码路径或系统描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /world src/naumi_agent/orchestrator/[/dim]"
                )
            else:
                await _run_analysis(engine, "world", arg)
        case "/fusion":
            if not arg:
                console.print(
                    "[yellow]用法: /fusion <代码路径或系统描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /fusion src/naumi_agent/tools/[/dim]"
                )
            else:
                await _run_analysis(engine, "fusion", arg)
        case "/consensus":
            if not arg:
                console.print(
                    "[yellow]用法: /consensus <代码路径或系统描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /consensus src/naumi_agent/trading/[/dim]"
                )
            else:
                await _run_analysis(engine, "consensus", arg)
        case "/pid":
            if not arg:
                console.print(
                    "[yellow]用法: /pid <代码路径或流程描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /pid src/naumi_agent/pipeline/[/dim]"
                )
            else:
                await _run_analysis(engine, "pid", arg)
        case "/zkp":
            if not arg:
                console.print(
                    "[yellow]用法: /zkp <代码路径或系统描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /zkp src/naumi_agent/tools/[/dim]"
                )
            else:
                await _run_analysis(engine, "zkp", arg)
        case "/genesis":
            if not arg:
                console.print(
                    "[yellow]用法: /genesis <代码路径或系统描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /genesis src/naumi_agent/[/dim]"
                )
            else:
                await _run_analysis(engine, "genesis", arg)
        case "/macro":
            if not arg:
                console.print(
                    "[yellow]用法: /macro <任务或系统描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /macro \"设计全球宏观经济分析系统\"[/dim]"
                )
            else:
                await _run_analysis(engine, "macro", arg)
        case "/cosmos":
            if not arg:
                console.print(
                    "[yellow]用法: /cosmos <代码路径或系统描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /cosmos src/naumi_agent/[/dim]"
                )
            else:
                await _run_analysis(engine, "cosmos", arg)
        case "/watchdog":
            if not arg:
                console.print(
                    "[yellow]用法: /watchdog <代码路径或系统描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /watchdog src/naumi_agent/[/dim]"
                )
            else:
                await _run_analysis(engine, "watchdog", arg)
        case "/supervisor":
            if not arg:
                console.print(
                    "[yellow]用法: /supervisor <代码路径或系统描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /supervisor src/naumi_agent/[/dim]"
                )
            else:
                await _run_analysis(engine, "supervisor", arg)
        case "/autopsy":
            if not arg:
                console.print(
                    "[yellow]用法: /autopsy <代码路径或 Bug 描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /autopsy src/naumi_agent/engine.py[/dim]"
                )
            else:
                await _run_analysis(engine, "autopsy", arg)
        case "/hook":
            if not arg:
                console.print("[yellow]用法: /hook <逆向目标描述>[/yellow]")
            else:
                await _run_analysis(engine, "hook", arg)
        case "/pursue":
            if not arg:
                console.print(
                    "[yellow]用法: /pursue <目标描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /pursue 为 NaumiAgent 添加一个 CSV 导出工具，"
                    "支持自定义分隔符和编码[/dim]"
                )
            else:
                await _run_pursue(engine, arg)
        case "/goal":
            await _run_goal(engine, arg)
        case "/worktree":
            await _run_worktree(engine, arg)
        case "/background":
            await _run_background(engine, arg)
        case "/schedule":
            await _run_schedule(engine, arg)
        case "/todo":
            await _run_todo(engine, arg)
        case "/team":
            await _run_team(engine, arg)
        case "/runtime":
            await _run_runtime(engine, arg)
        case "/self-review":
            await _run_self_review(engine, arg)
        case "/reload":
            await _run_reload(engine, arg)
        case "/evolve":
            await _run_evolve(engine, arg)
        case "/evolve-history":
            _show_evolve_history()
        case "/forge":
            await _run_forge(engine, arg)
        case "/forge-list":
            _show_forge_list()
        case "/forge-remove":
            _run_forge_remove(arg)
        case "/browse":
            await _run_browse(engine, arg)
        case "/autobrowse":
            await _run_autobrowse(engine, arg)
        case "/browser-stop":
            await _run_browser_stop(engine)
        case "/browser-state":
            await _run_browser_state(engine)
        case "/browser-screenshot":
            await _run_browser_screenshot(engine)
        case "/bdaemon":
            await _run_browser_daemon(engine, arg)
        case "/tasks":
            await _run_tasks_list(engine)
        case "/task":
            await _run_task_detail(engine, arg)
        case "/task-reply":
            await _run_task_reply(engine, arg)
        case "/task-abort":
            await _run_task_abort(engine, arg)
        case "/task-resume":
            await _run_task_resume(engine, arg)
        case "/scan":
            await _run_security_scan(engine, arg, profile="quick")
        case "/scan-full":
            await _run_security_scan(engine, arg, profile="full")
        case "/scan-report":
            await _run_scan_report(engine, arg)
        case "/scan-baseline":
            await _run_scan_baseline(engine, arg)
        case "/btemplate-list":
            _run_btemplate_list(engine)
        case "/btemplate-run":
            await _run_btemplate_run(engine, arg)
        case "/btemplate-compare":
            await _run_btemplate_compare(engine, arg)
        case _:
            # 尝试匹配已加载的 Skill
            skill_name = command.lstrip("/")
            skill = engine.skill_loader.get(skill_name) if hasattr(engine, "skill_loader") else None
            if skill:
                await _run_skill(engine, skill_name, arg)
            else:
                console.print(f"[yellow]未知命令: {command}[/yellow]")
                _print_help()


def _parse_reasoning_toggle(arg: str, current: bool) -> tuple[bool | None, str]:
    raw = arg.strip().lower()
    if not raw or raw == "toggle":
        return not current, ""
    if raw in {"on", "true", "1", "show", "open"}:
        return True, ""
    if raw in {"off", "false", "0", "hide", "close"}:
        return False, ""
    return None, "用法: /reasoning on|off|toggle"


def _handle_reasoning_command(engine: Any, arg: str) -> None:
    global _show_reasoning_text
    enabled, error = _parse_reasoning_toggle(arg, _show_reasoning_text)
    if enabled is None:
        console.print(f"[yellow]{error}[/yellow]")
        return
    _show_reasoning_text = enabled
    config = getattr(engine, "_config", None)
    if config is not None and getattr(config, "ui", None) is not None:
        config.ui.show_reasoning = enabled
    status = "开启" if enabled else "关闭"
    console.print(f"[green]reasoning 文本显示已{status}[/green]")


def _handle_effort_command(engine: Any, arg: str) -> None:
    """Show or update the process-local model reasoning intensity."""
    from naumi_agent.model.reasoning import ReasoningEffortError

    value = arg.strip().lower()
    try:
        if not value:
            status = engine.router.get_reasoning_effort_status()
        elif value == "reset":
            status = engine.router.reset_reasoning_effort()
            console.print("[green]已清除临时思考强度，恢复配置解析。[/green]")
        else:
            status = engine.router.set_reasoning_effort(value)
            if value == "auto":
                console.print("[green]思考强度已切换为供应商默认（auto）。[/green]")
            else:
                console.print(f"[green]思考强度已切换为 {value}。[/green]")
    except ReasoningEffortError as exc:
        console.print(str(exc), style="yellow", markup=False)
        return
    _print_reasoning_effort_status(status)


def _print_reasoning_effort_status(status: Any) -> None:
    source_names = {
        "runtime": "临时覆盖",
        "model": "单模型配置",
        "global": "全局配置",
        "auto": "供应商默认",
    }
    source = source_names.get(str(status.source), str(status.source))
    console.print(f"思考强度: [cyan]{status.effective.value}[/cyan]（来源: {source}）")
    if status.supported:
        supported = " / ".join(value.value for value in status.supported)
        console.print(f"可选强度: {supported}")
    else:
        console.print("可选强度: [dim]未声明（仅可安全使用 auto）[/dim]")
    if status.default is not None:
        console.print(f"模型默认: {status.default.value}")
    if status.warning:
        console.print(status.warning, style="yellow", markup=False)


def _print_banner(engine: Any) -> None:
    from naumi_agent import __version__
    from naumi_agent.assets import BANNER_TEXT

    model = engine.router.resolve_model("capable")
    console.print(
        Panel(
            BANNER_TEXT,
            title=f"[bold]v{__version__}[/bold]",
            subtitle=f"[dim]{model}[/dim]",
            border_style="green",
            padding=(1, 2),
        )
    )
    console.print()


def _print_help() -> None:
    console.print("[bold]可用命令:[/bold]")
    commands = [
        ("/help", "显示帮助"),
        ("/keybindings", "显示当前快捷键配置"),
        ("/style", "显示当前主题和输出风格"),
        ("/reasoning [on|off|toggle]", "显示或隐藏模型思考文本"),
        ("/effort [auto|none|minimal|low|medium|high|xhigh|max|reset]", "查看或切换模型思考强度"),
        ("/doctor", "运行环境诊断"),
        (
            "/harness [status|doctor|explain|replay|eval|knowledge|check|trust|untrust]",
            "管理仓库 Harness Profile、离线评测、运行解释、知识与验证检查",
        ),
        ("/copy [all|last|error]", "复制/导出完整记录、最近一轮或最近错误 (Ctrl+Y)"),
        ("/debug", "显示本次 CLI/TUI 结构化调试日志位置"),
        ("/debug-replay [路径]", "回放 debug-runs 结构化事件"),
        ("/diff [all|worktree|staged]", "查看本轮结构化 git diff"),
        ("/pwd", "显示当前工作目录"),
        ("/tools", "列出可用工具"),
        ("/model", "显示模型配置"),
        ("/models [provider] [--refresh]", "列出 provider 的可用模型"),
        ("/usage", "显示 token 用量"),
        ("/version", "显示版本号"),
        ("/hooks", "显示已注册的钩子"),
        ("/skills", "列出已加载的 Skill"),
        ("/glob <pattern> [directory='.' ]", "按 glob 规则搜索工作区文件路径"),
        ("/grep <pattern> [path='.'] [glob='**/*.py'] [max_matches=200] [case_sensitive=false]",
         "搜索文件内容（可配置过滤）"),
        ("/read <path> [offset=0] [limit=-1]", "读取文件内容（可分页）"),
        ("/write <path> <内容>", "写入文件（覆盖）"),
        ("/edit <path> <旧文本> <新文本>", "按文本替换更新文件"),
        ("/history", "查看历史会话列表"),
        ("/memory [子命令]", "记忆管理 (stats/search/clean/export)"),
        ("/load <编号或id>", "加载会话 (无参数显示列表)"),
        ("/resume", "继续最近的对话 (/r)"),
        ("/delete <id>", "删除指定会话"),
        ("/chaos [目标]", "灾难演练 — SPOF 分析"),
        ("/scale [目标|QPS]", "并发海啸 — 高并发分析"),
        ("/state", "状态审查 — 云原生合规"),
        ("/vibe <描述>", "极速构建 — 生成 Demo"),
        ("/eval <路径>", "评测驱动 — 生成 pytest 测试"),
        ("/page", "内存分页 — 上下文压力分析"),
        ("/heal <错误>", "自愈修复 — 分析并修复错误"),
        ("/dspy [描述]", "DSPy 编译优化 — Prompt 工程优化"),
        ("/graph [路径]", "图谱推演 — GraphRAG 拓扑分析"),
        ("/mcts <问题>", "蒙特卡洛树搜索 — 多路径决策"),
        ("/route <任务>", "MoE 混合专家调度 — 多视角分析"),
        ("/speculate <路径>", "推测解码 — 快速起草+深度审查"),
        ("/jit <任务>", "JIT 即时工具 — 用代码保证确定性"),
        ("/pointer <路径>", "语义指针(SPA) — 推理态/物理态分离"),
        ("/cooe <任务>", "认知乱序执行(COOE) — DAG并行调度"),
        ("/sleep", "昼夜节律突触修剪 — 知识压缩"),
        ("/entropy <文本>", "耗散结构熵减 — 锚点重启"),
        ("/ooda <路径>", "OODA 战场指挥 — 反脆弱架构"),
        ("/probe <需求>", "黑盒探测 — 反幻觉协议"),
        ("/spar <目标>", "对抗自博弈 — 蓝军写代码 vs 红军搞破坏"),
        ("/world <目标>", "世界模型审计 — 状态转移·因果链·反事实推演"),
        ("/fusion <目标>", "决定论-概率论融合 — AI与传统代码边界审计"),
        ("/consensus <目标>", "拜占庭共识 — 多模型表决防幻觉"),
        ("/pid <目标>", "PID 闭环纠偏 — 开环→闭环改造"),
        ("/zkp <目标>", "零知识证明 — 执行轨迹校验"),
        ("/genesis <目标>", "系统自重构 — 元编程与热演化"),
        ("/macro <目标>", "多智能体市场博弈 — 自由市场涌现"),
        ("/cosmos <目标>", "创世引擎审计 — 评估创世潜力"),
        ("/watchdog <目标>", "看门狗 — 不死鸟灾难恢复协议"),
        ("/supervisor <目标>", "守护者树 — Let-it-crash 双子星架构"),
        ("/autopsy <目标>", "执行迹切片 — SWE-bench 级 Bug 解剖"),
        ("/vision <目标>", "AI 视觉数据提取 — 反封锁视觉管线"),
        ("/hook <目标>", "逆向插桩 — 黑盒解剖"),
        ("/pursue <目标>", "目标追踪 — 自主循环执行直至真正达成"),
        ("/goal [目标|子命令]", "持久目标 — 跨轮次保持方向，可选启动 Pursuit"),
        ("/worktree <子命令>", "隔离执行区 — create/status/bind/keep/remove"),
        ("/background <子命令>", "后台任务 — run/status/list/cancel/output/cleanup"),
        ("/schedule <子命令>", "调度提醒 — create/list/cancel/pause/resume"),
        ("/todo <子命令>", "todo 清单 — list/add/start/done/pending/delete/clear"),
        ("/team <子命令>", "团队协议 — status/handoff/blocker/decision/request/result"),
        ("/runtime [分区]", "运行时状态 — all/context/todo/team/subagent/hooks/resources"),
        ("/self-review [模块]", "自我审查 — 扫描自身源码质量与架构"),
        ("/reload [域]", "热重载 — 重载模块无需重启 (tools/memory/skills/all)"),
        ("/evolve <描述>", "自我进化 — 反思循环修改自身工具代码并验证"),
        ("/evolve-history", "查看自我进化历史记录"),
        ("/forge <描述>", "工具锻造 — 自主生成新工具并注册"),
        ("/forge-list", "列出所有已锻造的工具"),
        ("/forge-remove <名称>", "移除已锻造的工具"),
        ("/browse <url>", "打开 URL 并显示 SoM 元素"),
        ("/autobrowse <任务>", "自主浏览器任务"),
        ("/browser-stop", "停止浏览器会话"),
        ("/browser-state", "显示浏览器调试状态"),
        ("/browser-screenshot", "截取当前页面截图"),
        (
            "/bdaemon <子命令>",
            "外部浏览器 daemon — start/health/run/list/status/watch/reply/resume/abort/manual",
        ),
        ("/tasks", "任务面板 — todo/subagent/background/browser"),
        ("/task <id>", "查看任务运行详情"),
        ("/task-reply <id> <指令>", "回复等待中的任务"),
        ("/task-abort <id>", "中止运行中的任务"),
        ("/task-resume <id>", "从手动控制中恢复"),
        ("/scan <url>", "快速安全扫描"),
        ("/scan-full <url>", "完整 25 模块安全扫描"),
        ("/scan-report [format]", "导出最新扫描报告"),
        ("/scan-baseline <url>", "保存扫描为基线"),
        ("/btemplate-list", "列出浏览器任务模板"),
        ("/btemplate-run <id>", "从模板创建运行"),
        ("/btemplate-compare <id>", "比较模板运行结果"),
        ("/new", "保存当前会话并开始新对话"),
        ("/clear", "清除当前会话（不保存）"),
        ("/permissions", "显示待确认权限面板"),
        ("/q", "退出"),
        ("/quit", "退出"),
    ]
    for cmd, desc in commands:
        console.print(f"  [cyan]{cmd:12s}[/cyan] {desc}")
    console.print()


async def _run_harness(engine: Any, arg: str) -> None:
    """Run user-only Harness commands through the shared service facade."""
    from naumi_agent.harness.eval import render_harness_eval
    from naumi_agent.harness.explain import render_harness_explanation
    from naumi_agent.harness.service import (
        HarnessStatusCode,
        render_harness_check,
        render_harness_doctor,
        render_harness_knowledge,
        render_harness_replay,
        render_harness_status,
    )
    from naumi_agent.harness.trust import HarnessTrustStoreError

    usage = (
        "用法：/harness [status|doctor|explain|replay|eval|knowledge|check|trust|untrust]\n"
        "      /harness explain [run-id|latest]\n"
        "      /harness replay [run-id|latest]\n"
        "      /harness eval [suite-id|相对路径]\n"
        "      /harness knowledge <查询|相对路径> [--max-tokens 1..4000]\n"
        "      /harness check <check-id>\n"
        "      /harness trust --confirm"
    )
    try:
        parts = shlex.split(arg)
    except ValueError as exc:
        console.print(f"[yellow]Harness 参数解析失败：{exc}[/yellow]\n{usage}")
        return
    subcommand = parts[0].lower() if parts else "status"
    service = getattr(engine, "harness_service", None)
    if service is None:
        console.print("[red]Harness Service 尚未初始化。请重启 NaumiAgent。[/red]")
        return

    if subcommand == "status" and len(parts) == 1:
        console.print(Markdown(render_harness_status(await service.status())))
        return
    if subcommand == "doctor" and len(parts) == 1:
        console.print(Markdown(render_harness_doctor(await service.doctor())))
        return
    if subcommand == "explain" and len(parts) <= 2:
        target = parts[1] if len(parts) == 2 else None
        try:
            result = await service.explain_run(target)
        except ValueError as exc:
            console.print(f"[yellow]Harness 解释参数无效：{exc}[/yellow]")
            return
        console.print(Markdown(render_harness_explanation(result)))
        return
    if subcommand == "replay" and len(parts) <= 2:
        target = parts[1] if len(parts) == 2 else None
        try:
            result = await service.replay_run(target)
        except ValueError as exc:
            console.print(f"[yellow]Harness Replay 参数无效：{exc}[/yellow]")
            return
        console.print(Markdown(render_harness_replay(result)))
        return
    if subcommand == "eval" and len(parts) <= 2:
        target = parts[1] if len(parts) == 2 else None
        try:
            result = await service.eval_suites(target)
        except ValueError as exc:
            console.print(f"[yellow]Harness Eval 参数无效：{exc}[/yellow]")
            return
        console.print(Markdown(render_harness_eval(result)))
        return
    if subcommand == "knowledge":
        knowledge_args = parts[1:]
        max_tokens = 2_000
        if "--max-tokens" in knowledge_args:
            option_index = knowledge_args.index("--max-tokens")
            if (
                option_index != len(knowledge_args) - 2
                or knowledge_args.count("--max-tokens") != 1
            ):
                console.print(f"[yellow]{usage}[/yellow]")
                return
            try:
                max_tokens = int(knowledge_args[-1])
            except ValueError:
                console.print(f"[yellow]{usage}[/yellow]")
                return
            knowledge_args = knowledge_args[:option_index]
        if (
            not knowledge_args
            or any(item.startswith("--") for item in knowledge_args)
            or not 1 <= max_tokens <= 4_000
        ):
            console.print(f"[yellow]{usage}[/yellow]")
            return
        target = " ".join(knowledge_args).strip()
        try:
            if _looks_like_harness_knowledge_path(target, knowledge_args):
                result = await service.read_knowledge(
                    path=target,
                    max_tokens=max_tokens,
                )
            else:
                result = await service.read_knowledge(
                    query=target,
                    max_tokens=max_tokens,
                )
        except ValueError as exc:
            console.print(f"[yellow]Harness 知识参数无效：{exc}[/yellow]")
            return
        console.print(Markdown(render_harness_knowledge(result)))
        return
    if subcommand == "check" and len(parts) == 2:
        session = getattr(engine, "_session", None)
        session_id = getattr(session, "id", "")
        run_id = f"manual:{session_id or uuid.uuid4().hex}"
        try:
            result = await service.run_check(
                check_id=parts[1],
                run_id=run_id,
            )
        except ValueError as exc:
            console.print(f"[yellow]Harness 检查参数无效：{exc}[/yellow]")
            return
        console.print(Markdown(render_harness_check(result)))
        return
    if subcommand == "trust" and len(parts) == 1:
        report = await service.doctor()
        status = report.status
        if status.code in {HarnessStatusCode.MISSING, HarnessStatusCode.INVALID}:
            console.print(Markdown(render_harness_doctor(report)))
            return
        digest = status.profile_digest or "-"
        lines = [
            "## Harness 信任预览（仅预览）",
            "",
            f"工作区：`{service.workspace_root}`",
            f"Profile digest：`{digest}`",
            "",
            "### 配置中的命令（信任后仅按需执行）",
        ]
        lines.extend(
            f"- `{command}`" for command in report.command_summaries
        )
        if not report.command_summaries:
            lines.append("- 没有配置命令")
        lines.extend(
            (
                "",
                "下一步：确认内容无误后运行 `/harness trust --confirm`。",
            )
        )
        console.print(Markdown("\n".join(lines)))
        return
    if subcommand == "trust" and parts == ["trust", "--confirm"]:
        try:
            record = await service.trust(source="user_slash")
        except (ValueError, HarnessTrustStoreError) as exc:
            console.print(f"[yellow]{exc}[/yellow]\n{usage}")
            return
        console.print(
            "[green]Harness Profile 已信任。[/green]\n"
            f"digest: [dim]{record.profile_digest}[/dim]"
        )
        return
    if subcommand == "untrust" and len(parts) == 1:
        try:
            removed = await service.untrust()
        except HarnessTrustStoreError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            return
        if removed:
            console.print("[green]Harness Profile 信任已撤销。[/green]")
        else:
            console.print("[yellow]当前工作区没有 Harness 信任记录。[/yellow]")
        return
    console.print(f"[yellow]{usage}[/yellow]")


def _looks_like_harness_knowledge_path(
    target: str,
    parts: list[str],
) -> bool:
    if len(parts) != 1:
        return False
    if "/" in target or "\\" in target or target.startswith("."):
        return True
    return Path(target).suffix.lower() in {
        ".js",
        ".json",
        ".md",
        ".mdx",
        ".py",
        ".rst",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }


def _parse_scale_arg(arg: str) -> tuple[str, int]:
    """Parse `/scale [target|QPS]` while preserving legacy target-path input."""
    raw = arg.strip()
    if not raw:
        return "当前项目", 10000

    normalized = raw.replace(",", "").replace("_", "")
    if normalized.isdigit():
        return "当前项目", max(1, int(normalized))

    return raw, 10000


def _build_main_analysis_kwargs(
    mode: str,
    target: str,
    *,
    effective_qps: int,
) -> dict[str, Any]:
    """Build execute kwargs for main CLI analysis commands."""
    if mode == "vibe":
        return {"description": target}
    if mode == "scale":
        return {"target": target, "qps": effective_qps}
    if mode == "page":
        return {"session_context": target}
    if mode == "heal":
        return {"error_log": target}
    if mode == "dspy":
        return {"prompt_target": target}
    if mode == "mcts":
        return {"problem": target}
    if mode in {
        "route",
        "jit",
        "cooe",
        "probe",
        "hook",
        "vision",
        "spar",
        "macro",
    }:
        return {"task": target}
    if mode == "sleep":
        return {"session_context": target}
    if mode == "entropy":
        return {"context": target}
    return {"target": target}


async def _run_analysis(engine: Any, mode: str, target: str, *, qps: int | None = None) -> None:
    """执行分析模式命令."""
    effective_qps = qps or 10000
    tool_names = {
        "chaos": "analysis_chaos",
        "scale": "analysis_scale",
        "state": "analysis_state",
        "vibe": "analysis_vibe",
        "eval": "analysis_eval",
        "page": "analysis_page",
        "heal": "analysis_heal",
        "dspy": "analysis_dspy",
        "graph": "analysis_graph",
        "mcts": "analysis_mcts",
        "route": "analysis_route",
        "speculate": "analysis_speculate",
        "jit": "analysis_jit",
        "pointer": "analysis_pointer",
        "cooe": "analysis_cooe",
        "sleep": "analysis_sleep",
        "entropy": "analysis_entropy",
        "ooda": "analysis_ooda",
        "probe": "analysis_probe",
        "hook": "analysis_hook",
        "vision": "analysis_vision",
        "spar": "analysis_spar",
        "world": "analysis_world",
        "fusion": "analysis_fusion",
        "consensus": "analysis_consensus",
        "pid": "analysis_pid",
        "zkp": "analysis_zkp",
        "genesis": "analysis_genesis",
        "macro": "analysis_macro",
        "cosmos": "analysis_cosmos",
        "watchdog": "analysis_watchdog",
        "supervisor": "analysis_supervisor",
        "autopsy": "analysis_autopsy",
    }

    labels = {
        "chaos": "灾难演练",
        "scale": f"并发海啸 ({effective_qps:,} QPS)",
        "state": "状态审查",
        "vibe": "极速构建",
        "eval": "评测驱动开发 (EDD)",
        "page": "内存分页调度 (LLM OS)",
        "heal": "自愈修复",
        "dspy": "DSPy 编译优化",
        "graph": "图谱推演 (GraphRAG)",
        "mcts": "蒙特卡洛树搜索 (MCTS)",
        "route": "MoE 混合专家调度",
        "speculate": "推测解码 (Draft+Review)",
        "jit": "JIT 即时工具生成",
        "pointer": "语义指针架构 (SPA)",
        "cooe": "认知乱序执行 (COOE)",
        "sleep": "昼夜节律突触修剪",
        "entropy": "耗散结构熵减重置",
        "ooda": "OODA 战场指挥",
        "probe": "黑盒探测 (Probe)",
        "hook": "逆向插桩 (Hook)",
        "vision": "AI 视觉数据提取 (Vision)",
        "spar": "对抗性自博弈 (GAN for Code)",
        "world": "世界模型审计 (World Model)",
        "fusion": "决定论-概率论融合审计 (Fusion)",
        "consensus": "拜占庭容错共识 (Consensus)",
        "pid": "PID 闭环纠偏 (Control Theory)",
        "zkp": "零知识证明与轨迹校验 (ZKP)",
        "genesis": "系统自重构与热演化 (Genesis)",
        "macro": "多智能体自由市场博弈 (Agentic Economy)",
        "cosmos": "创世引擎审计 (Cosmos)",
        "watchdog": "看门狗与灾难隔离 (Watchdog)",
        "supervisor": "Erlang 守护者树 (Supervisor)",
        "autopsy": "执行迹切片与爆炸半径隔离 (DTS-CHE)",
    }

    tool_name = tool_names[mode]
    label = labels[mode]
    tool = engine.tool_registry.get(tool_name)
    if tool is None:
        console.print(f"[red]工具 {tool_name} 未注册[/red]")
        return

    console.print(f"[bold yellow]⚡ {label} 分析中...[/bold yellow]")
    with console.status("[bold green]分析中...[/bold green]"):
        kwargs = _build_main_analysis_kwargs(
            mode,
            target,
            effective_qps=effective_qps,
        )
        from naumi_agent.tools.base import ToolCall

        tool_call = ToolCall(
            id=f"slash-analysis-{mode}-{uuid.uuid4()}",
            name=tool_name,
            arguments=json.dumps(kwargs, ensure_ascii=False),
        )
        tool_result = await engine.execute_tool(tool_call, agent_name="cli")
        if tool_result.status != "success":
            console.print(f"[yellow]{tool_result.content}[/yellow]")
            return
        result = tool_result.content

    console.print()
    console.print(
        Panel(
            Markdown(result),
            title=f"[bold yellow]⚡ {label}[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        )
    )
    console.print()


async def _run_pursue(engine: Any, goal: str) -> None:
    """执行目标追踪循环."""
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn

    parts = goal.strip().split(maxsplit=1)
    if parts and parts[0] in {"list", "status", "resume"}:
        await _run_pursue_meta(engine, parts[0], parts[1] if len(parts) > 1 else "")
        return

    console.print(
        Panel(
            f"[bold green]目标追踪启动[/bold green]\n\n{goal}",
            border_style="green",
            title="🎯 /pursue",
        )
    )

    tool = engine.tool_registry.get("pursue_goal")
    if not tool:
        console.print("[red]目标追踪工具未注册[/red]")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("追踪中...", total=None)

        try:
            from naumi_agent.tools.base import ToolCall

            tool_call = ToolCall(
                id=f"slash-pursue-{uuid.uuid4()}",
                name="pursue_goal",
                arguments=json.dumps({"goal": goal}, ensure_ascii=False),
            )
            tool_result = await engine.execute_tool(tool_call, agent_name="cli")
            if tool_result.status != "success":
                console.print(f"[yellow]{tool_result.content}[/yellow]")
                return
            result = tool_result.content
        except KeyboardInterrupt:
            console.print("\n[yellow]⚠️ 目标追踪被中断[/yellow]")
            return

    console.print(
        Panel(
            result,
            border_style="green" if "达成" in result else "yellow",
            title="🎯 目标追踪报告",
        )
    )


async def _run_pursue_meta(engine: Any, subcommand: str, arg: str) -> None:
    """执行 pursuit 持久化状态命令."""
    tool_map = {
        "list": "pursuit_list",
        "status": "pursuit_status",
        "resume": "pursuit_resume",
    }
    tool_name = tool_map[subcommand]
    tool = engine.tool_registry.get(tool_name)
    if not tool:
        console.print(f"[red]工具未注册: {tool_name}[/red]")
        return
    if subcommand in {"status", "resume"} and not arg:
        console.print(f"[yellow]用法: /pursue {subcommand} <运行ID>[/yellow]")
        return
    if subcommand == "list":
        kwargs = {"active_only": "--active" in arg.split()}
    else:
        kwargs = {"run_id": arg.strip()}

    from naumi_agent.tools.base import ToolCall

    tool_call = ToolCall(
        id=f"slash-pursue-{subcommand}-{uuid.uuid4()}",
        name=tool_name,
        arguments=json.dumps(kwargs, ensure_ascii=False),
    )
    tool_result = await engine.execute_tool(tool_call, agent_name="cli")
    if tool_result.status != "success":
        console.print(f"[yellow]{tool_result.content}[/yellow]")
        return
    result = tool_result.content
    console.print(
        Panel(
            Markdown(result),
            title="[bold cyan]目标追踪状态[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


async def _run_worktree(engine: Any, arg: str) -> None:
    """执行 worktree 隔离区命令."""
    parts = arg.strip().split()
    subcommand = parts[0] if parts else "status"

    async def _execute(tool_name: str, **kwargs: Any) -> None:
        tool = engine.tool_registry.get(tool_name)
        if not tool:
            console.print(f"[red]工具未注册: {tool_name}[/red]")
            return
        from naumi_agent.tools.base import ToolCall

        tool_call = ToolCall(
            id=f"slash-worktree-{tool_name}-{uuid.uuid4()}",
            name=tool_name,
            arguments=json.dumps(kwargs, ensure_ascii=False),
        )
        tool_result = await engine.execute_tool(tool_call, agent_name="cli")
        if tool_result.status != "success":
            console.print(f"[yellow]{tool_result.content}[/yellow]")
            return
        result = tool_result.content
        console.print(
            Panel(
                Markdown(result),
                title="[bold cyan]Worktree 隔离区[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    match subcommand:
        case "status" | "list":
            name = parts[1] if len(parts) > 1 else ""
            await _execute("worktree_status", name=name)
        case "create":
            if len(parts) < 2:
                console.print("[yellow]用法: /worktree create <名称> [任务ID][/yellow]")
                return
            task_id = parts[2] if len(parts) > 2 else ""
            await _execute("worktree_create", name=parts[1], task_id=task_id)
        case "bind":
            if len(parts) < 3:
                console.print("[yellow]用法: /worktree bind <名称> <任务ID>[/yellow]")
                return
            await _execute("worktree_bind_task", name=parts[1], task_id=parts[2])
        case "keep":
            if len(parts) < 2:
                console.print("[yellow]用法: /worktree keep <名称> [原因][/yellow]")
                return
            reason = " ".join(parts[2:]) if len(parts) > 2 else ""
            await _execute("worktree_keep", name=parts[1], reason=reason)
        case "remove":
            if len(parts) < 2:
                console.print("[yellow]用法: /worktree remove <名称> [--discard][/yellow]")
                return
            discard = "--discard" in parts[2:] or "--force" in parts[2:]
            await _execute("worktree_remove", name=parts[1], discard_changes=discard)
        case _:
            console.print(
                "[yellow]未知 worktree 子命令[/yellow]\n"
                "[dim]可用: status/list/create/bind/keep/remove[/dim]"
            )


async def _run_background(engine: Any, arg: str) -> None:
    """执行后台任务命令."""
    parts = arg.strip().split(maxsplit=2)
    subcommand = parts[0] if parts else "list"

    async def _execute(tool_name: str, **kwargs: Any) -> None:
        tool = engine.tool_registry.get(tool_name)
        if not tool:
            console.print(f"[red]工具未注册: {tool_name}[/red]")
            return
        from naumi_agent.tools.base import ToolCall

        tool_call = ToolCall(
            id=f"slash-background-{tool_name}-{uuid.uuid4()}",
            name=tool_name,
            arguments=json.dumps(kwargs, ensure_ascii=False),
        )
        tool_result = await engine.execute_tool(tool_call, agent_name="cli")
        if tool_result.status != "success":
            console.print(f"[yellow]{tool_result.content}[/yellow]")
            return
        result = tool_result.content
        console.print(
            Panel(
                Markdown(result),
                title="[bold cyan]后台任务[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    match subcommand:
        case "run":
            if len(parts) < 2:
                console.print("[yellow]用法: /background run <命令>[/yellow]")
                return
            command = arg.strip()[len("run"):].strip()
            await _execute("background_run", command=command)
        case "status":
            if len(parts) < 2:
                console.print("[yellow]用法: /background status <任务ID>[/yellow]")
                return
            await _execute("background_status", task_id=parts[1])
        case "list":
            await _execute("background_list")
        case "cancel":
            if len(parts) < 2:
                console.print("[yellow]用法: /background cancel <任务ID>[/yellow]")
                return
            await _execute("background_cancel", task_id=parts[1])
        case "cleanup":
            await _execute("background_cleanup")
        case "output":
            if len(parts) < 2:
                console.print("[yellow]用法: /background output <任务ID>[/yellow]")
                return
            await _execute("background_read_output", task_id=parts[1])
        case _:
            console.print(
                "[yellow]未知后台任务子命令[/yellow]\n"
                "[dim]可用: run/status/list/cancel/cleanup/output[/dim]"
            )


async def _run_schedule(engine: Any, arg: str) -> None:
    """执行调度/提醒命令."""
    try:
        parts = shlex.split(arg.strip())
    except ValueError as e:
        console.print(f"[yellow]参数解析失败：{e}[/yellow]")
        return
    subcommand = parts[0] if parts else "list"

    async def _execute(tool_name: str, **kwargs: Any) -> None:
        tool = engine.tool_registry.get(tool_name)
        if not tool:
            console.print(f"[red]工具未注册: {tool_name}[/red]")
            return
        from naumi_agent.tools.base import ToolCall

        tool_call = ToolCall(
            id=f"slash-schedule-{tool_name}-{uuid.uuid4()}",
            name=tool_name,
            arguments=json.dumps(kwargs, ensure_ascii=False),
        )
        tool_result = await engine.execute_tool(tool_call, agent_name="cli")
        if tool_result.status != "success":
            console.print(f"[yellow]{tool_result.content}[/yellow]")
            return
        result = tool_result.content
        console.print(
            Panel(
                Markdown(result),
                title="[bold cyan]调度提醒[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    match subcommand:
        case "create":
            if len(parts) < 4:
                console.print(
                    "[yellow]用法: /schedule create once <ISO时间> <提醒内容>[/yellow]\n"
                    "[dim]或: /schedule create cron \"*/15 * * * *\" <提醒内容>[/dim]"
                )
                return
            await _execute(
                "schedule_create",
                kind=parts[1],
                expression=parts[2],
                prompt=" ".join(parts[3:]),
            )
        case "list":
            await _execute("schedule_list", active_only="--active" in parts[1:])
        case "cancel":
            if len(parts) < 2:
                console.print("[yellow]用法: /schedule cancel <调度ID>[/yellow]")
                return
            await _execute("schedule_cancel", schedule_id=parts[1])
        case "pause":
            if len(parts) < 2:
                console.print("[yellow]用法: /schedule pause <调度ID>[/yellow]")
                return
            await _execute("schedule_pause", schedule_id=parts[1])
        case "resume":
            if len(parts) < 2:
                console.print("[yellow]用法: /schedule resume <调度ID>[/yellow]")
                return
            await _execute("schedule_resume", schedule_id=parts[1])
        case _:
            console.print(
                "[yellow]未知调度子命令[/yellow]\n"
                "[dim]可用: create/list/cancel/pause/resume[/dim]"
            )


async def _run_todo(engine: Any, arg: str) -> None:
    """执行 todo 命令."""
    from naumi_agent.tasks.commands import run_todo_command

    session = await engine.get_or_create_session()
    engine.task_store.set_session(session.id)
    result = await run_todo_command(engine.task_store, arg)
    if _active_cli is not None and hasattr(_active_cli, "set_todo_status"):
        tasks = await engine.task_store.list_tasks()
        open_tasks = [task for task in tasks if task.status.value != "completed"]
        _active_cli.set_todo_status(_format_todo_bar({
            "count": len(tasks),
            "open_count": len(open_tasks),
            "completed_count": len(tasks) - len(open_tasks),
            "items": [
                {
                    "id": task.id,
                    "status": task.status.value,
                    "subject": task.active_form or task.subject,
                }
                for task in open_tasks
            ],
            "summary": result,
        }) or None)
    console.print(
        Panel(
            Markdown(result),
            title="[bold cyan]todo[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


async def _run_team(engine: Any, arg: str) -> None:
    """执行 team protocol 命令."""
    from naumi_agent.agents.team_commands import run_team_command

    result = await run_team_command(engine.subagent_manager, arg)
    console.print(
        Panel(
            Markdown(result),
            title="[bold cyan]team[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


async def _run_runtime(engine: Any, arg: str) -> None:
    """执行 runtime 状态命令."""
    from naumi_agent.tools.runtime import run_runtime_command

    result = await run_runtime_command(engine, arg)
    console.print(
        Panel(
            Markdown(result),
            title="[bold cyan]runtime[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


async def _run_self_review(engine: Any, arg: str) -> None:
    """执行自我审查."""
    tool = engine.tool_registry.get("self_review")
    if not tool:
        console.print("[red]自我审查工具未注册[/red]")
        return

    parts = arg.strip().split(maxsplit=1) if arg else []
    focus = parts[0] if parts else "all"
    module = parts[1] if len(parts) > 1 else ""

    console.print("[bold yellow]🔍 自我审查启动...[/bold yellow]")
    with console.status("[bold green]扫描自身源码中...[/bold green]"):
        kwargs = {"focus": focus, "module": module}
        from naumi_agent.tools.base import ToolCall

        tool_call = ToolCall(
            id=f"slash-self-review-{uuid.uuid4()}",
            name="self_review",
            arguments=json.dumps(kwargs, ensure_ascii=False),
        )
        tool_result = await engine.execute_tool(tool_call, agent_name="cli")
        if tool_result.status != "success":
            console.print(f"[yellow]{tool_result.content}[/yellow]")
            return
        result = tool_result.content

    console.print()
    console.print(
        Panel(
            Markdown(result),
            title="[bold yellow]🔍 自我审查报告[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        ),
    )
    console.print()


async def _run_goal(engine: Any, arg: str) -> None:
    """Manage the durable workspace goal through Engine tool execution."""
    from rich.panel import Panel

    normalized = arg.strip()
    subcommand, _, remainder = normalized.partition(" ")
    subcommand = subcommand.lower()
    remainder = remainder.strip()

    if not normalized:
        tool_name = "goal_status"
        kwargs: dict[str, Any] = {}
    elif subcommand == "status":
        tool_name = "goal_status"
        kwargs = {"goal_id": remainder} if remainder else {}
    elif subcommand == "list":
        extra = remainder.split()
        unknown = [item for item in extra if item != "--active"]
        if unknown:
            console.print("[yellow]用法: /goal list [--active][/yellow]")
            return
        tool_name = "goal_list"
        kwargs = {"include_finished": "--active" not in extra}
    elif subcommand == "create":
        if not remainder:
            console.print("[yellow]用法: /goal create <目标>[/yellow]")
            return
        tool_name = "goal_create"
        kwargs = {"objective": remainder}
    elif subcommand in {"pause", "resume", "block", "complete", "cancel"}:
        if subcommand == "block" and not remainder:
            console.print("[yellow]用法: /goal block <阻塞原因>[/yellow]")
            return
        target_status = {
            "pause": "paused",
            "resume": "active",
            "block": "blocked",
            "complete": "completed",
            "cancel": "cancelled",
        }[subcommand]
        tool_name = "goal_update"
        kwargs = {"status": target_status, "note": remainder}
    elif subcommand == "pursue":
        if remainder:
            console.print("[yellow]用法: /goal pursue[/yellow]")
            return
        tool_name = "goal_pursue"
        kwargs = {}
    else:
        tool_name = "goal_create"
        kwargs = {"objective": normalized}

    tool = engine.tool_registry.get(tool_name)
    if tool is None:
        console.print(f"[red]工具未注册: {tool_name}[/red]")
        return

    from naumi_agent.tools.base import ToolCall

    tool_call = ToolCall(
        id=f"slash-goal-{uuid.uuid4()}",
        name=tool_name,
        arguments=json.dumps(kwargs, ensure_ascii=False),
    )
    tool_result = await engine.execute_tool(tool_call, agent_name="cli")
    if tool_result.status != "success":
        console.print(f"[yellow]{tool_result.content}[/yellow]")
        return
    result = tool_result.content

    console.print(
        Panel(
            Markdown(result),
            title="[bold cyan]持久目标[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()


async def _run_reload(engine: Any, arg: str) -> None:
    """执行热重载."""
    domain = arg.strip() or "tools"
    valid_domains = {"tools", "memory", "skills", "agents", "hooks", "all"}

    if domain not in valid_domains:
        console.print(
            f"[yellow]未知域: {domain}[/yellow]\n"
            f"[dim]可选: {', '.join(sorted(valid_domains))}[/dim]",
        )
        return

    console.print(f"[bold yellow]🔄 热重载 {domain}...[/bold yellow]")
    result = await engine.reload_tools(domain)

    if result["errors"] > 0:
        console.print(f"[green]✅ {result['reloaded']} 个模块已重载[/green]")
        console.print(f"[red]❌ {result['errors']} 个模块重载失败[/red]")
        for d in result["details"]:
            if d["status"] == "error":
                console.print(f"  [red]{d['module']}: {d['error']}[/red]")
    else:
        console.print(f"[green]✅ {result['reloaded']} 个模块已重载[/green]")
    console.print()



async def _run_evolve(engine: Any, arg: str) -> None:
    """执行自我进化 — 反思循环: LLM生成方案 → 验证修改 → 质量评估 → 采纳/回滚."""
    import json
    import re

    from rich.markdown import Markdown
    from rich.panel import Panel

    description = arg.strip()
    if not description:
        console.print("[yellow]用法: /evolve <修改描述>[/yellow]")
        console.print("[dim]例: /evolve 优化 analysis.py 中的 _scan_chaos 函数性能[/dim]")
        return

    self_modify = engine.tool_registry.get("self_modify")
    self_evolve = engine.tool_registry.get("self_evolve")
    if not self_modify:
        console.print("[red]自我修改工具未注册[/red]")
        return
    if not self_evolve:
        console.print("[red]自我进化评估工具未注册，已停止修改流程[/red]")
        return

    console.print(f"[bold yellow]🧬 自我进化: {description}[/bold yellow]")

    # Phase 1: Generate modification via LLM
    console.print("[dim]Phase 1: 分析目标并生成修改方案...[/dim]")

    with console.status("[bold green]分析中...[/bold green]"):
        from naumi_agent.tools.self_modify import (
            _find_agent_source_dir,
            _is_protected_file,
        )

        source_dir = _find_agent_source_dir()

        prompt = (
            f"Agent 自我进化请求: {description}\n\n"
            "请分析以下请求，确定需要修改的目标文件，并生成完整的修改后文件内容。\n\n"
            "要求:\n"
            "1. 只能修改 tools/memory/skills 目录下的模块\n"
            "2. 不能修改 engine/safety/config 等核心模块\n"
            "3. 修改后的代码必须保持所有现有接口兼容\n"
            "4. target_file 必须从下方可修改文件列表中选择，不能自行构造路径\n"
            '5. 输出 JSON: {"target_file": "路径", '
            '"new_content": "内容", "description": "说明"}\n'
        )

        modifiable_files = []
        for domain_dir in ["tools", "memory", "skills"]:
            domain_path = source_dir / domain_dir
            if domain_path.is_dir():
                for py_file in sorted(domain_path.rglob("*.py")):
                    if (
                        py_file.name != "__init__.py"
                        and not _is_protected_file(py_file)
                    ):
                        modifiable_files.append(py_file.relative_to(source_dir).as_posix())

        file_list = "\n".join(f"- {f}" for f in modifiable_files)
        prompt += f"\n可修改的文件列表:\n{file_list}"

        def code_tokens(text: str) -> set[str]:
            tokens = set(re.findall(r"[a-z0-9_]+", text.lower()))
            for token in list(tokens):
                tokens.update(part for part in token.split("_") if part)
            return tokens

        query_tokens = code_tokens(description)

        def context_rank(file_name: str) -> tuple[int, str]:
            path_tokens = code_tokens(file_name)
            return (-len(query_tokens & path_tokens), file_name)

        file_contexts = []
        for f in sorted(modifiable_files, key=context_rank)[:10]:
            fp = source_dir / f
            try:
                content = fp.read_text(encoding="utf-8")
                if len(content) > 5000:
                    content = content[:5000] + "\n... (truncated)"
                file_contexts.append(f"### {f}\n```python\n{content}\n```")
            except Exception:
                pass

        prompt += "\n\n当前源码上下文:\n" + "\n".join(file_contexts[:5])

    try:
        response = await engine._router.call(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 Agent 自我进化系统。根据修改请求生成代码修改方案。"
                        "只输出 JSON，不要其他内容。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            tier="capable",
        )
        llm_output = response.content.strip()
    except Exception as e:
        console.print(f"[red]LLM 调用失败: {e}[/red]")
        return

    def extract_evolve_proposal_json(text: str) -> str:
        decoder = json.JSONDecoder()
        json_fence = re.search(r"```json\s*\n?(.*?)\n?```", text, re.DOTALL | re.IGNORECASE)
        if json_fence:
            return json_fence.group(1)

        for fence in re.finditer(r"```\s*\n?(.*?)\n?```", text, re.DOTALL):
            candidate_text = fence.group(1).strip()
            try:
                candidate, _ = decoder.raw_decode(candidate_text)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                return candidate_text

        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                return text[index:index + end]
        return text

    json_str = extract_evolve_proposal_json(llm_output)

    try:
        proposal = json.loads(json_str)
    except json.JSONDecodeError:
        console.print("[red]无法解析 LLM 输出的修改方案[/red]")
        console.print(Panel(llm_output[:2000], title="[red]LLM 输出[/red]"))
        return

    if not isinstance(proposal, dict):
        console.print("[red]LLM 修改方案格式错误: 必须是 JSON 对象[/red]")
        return

    direct_proposal_keys = (
        "target_file",
        "file_path",
        "path",
        "new_content",
        "content",
        "new_file_content",
        "updated_content",
        "code",
    )
    wrapped_proposal = next(
        (
            proposal.get(key)
            for key in ("proposal", "modification", "result")
            if isinstance(proposal.get(key), dict)
        ),
        None,
    )
    changes = proposal.get("changes")
    if (
        wrapped_proposal is None
        and isinstance(changes, list)
        and len(changes) == 1
        and isinstance(changes[0], dict)
    ):
        wrapped_proposal = changes[0]
    if wrapped_proposal and not any(proposal.get(key) for key in direct_proposal_keys):
        proposal = wrapped_proposal

    def normalize_evolve_target_file(value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        normalized = re.sub(r"^`+([^`]+?)`+$", r"\1", normalized).strip()
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
            normalized = normalized[1:-1].strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = re.sub(r"(?<=\.py):\d+(?::\d+)?$", "", normalized)
        normalized = re.sub(r"(?<=\.py)#L\d+(?:-L?\d+)?$", "", normalized, flags=re.IGNORECASE)
        try:
            resolved = Path(normalized).expanduser().resolve()
            return resolved.relative_to(source_dir.resolve()).as_posix()
        except (OSError, ValueError):
            pass
        for prefix in ("src/naumi_agent/", "naumi_agent/"):
            if normalized.startswith(prefix):
                return normalized[len(prefix):]
        if "/" not in normalized and not normalized.endswith(".py") and " " not in normalized:
            module_name = normalized
            for prefix in ("src.naumi_agent.", "naumi_agent."):
                if module_name.startswith(prefix):
                    module_name = module_name[len(prefix):]
                    break
            parts = module_name.split(".")
            if len(parts) >= 2 and parts[0] in {"tools", "memory", "skills"}:
                return "/".join(parts) + ".py"
        return normalized

    target_file = (
        proposal.get("target_file")
        or proposal.get("file_path")
        or proposal.get("path", "")
    )
    new_content = (
        proposal.get("new_content")
        or proposal.get("content")
        or proposal.get("new_file_content")
        or proposal.get("updated_content")
        or proposal.get("code", "")
    )
    change_desc = proposal.get("description", description)

    if not isinstance(target_file, str) or not isinstance(new_content, str):
        console.print("[red]LLM 修改方案格式错误: target_file 和 new_content 必须是字符串[/red]")
        return
    if not isinstance(change_desc, str):
        console.print("[red]LLM 修改方案格式错误: description 必须是字符串[/red]")
        return

    target_file = normalize_evolve_target_file(target_file)

    if not target_file or not new_content:
        console.print("[red]修改方案缺少 target_file 或 new_content[/red]")
        return

    console.print(f"[dim]目标文件: {target_file}[/dim]")
    console.print(f"[dim]修改说明: {change_desc}[/dim]")

    # Read original content for evolution evaluation
    from naumi_agent.tools.self_modify import (
        _is_modifiable_file,
        _is_protected_file,
        _resolve_target_path,
    )

    try:
        original_path = _resolve_target_path(target_file)
        if _is_protected_file(original_path):
            console.print("[red]目标文件受保护，已停止自我修改流程[/red]")
            return
        if not _is_modifiable_file(original_path):
            console.print("[red]目标文件不在可修改范围内，已停止自我修改流程[/red]")
            return
        original_content = original_path.read_text(encoding="utf-8")
    except Exception as e:
        console.print(f"[red]无法读取原始文件: {e}[/red]")
        return

    # Phase 2: Validate and apply modification
    console.print("[dim]Phase 2: 验证并应用修改...[/dim]")

    with console.status("[bold green]验证中...[/bold green]"):
        kwargs = {
            "target_file": target_file,
            "new_content": new_content,
            "description": change_desc,
            "apply_to_workspace": True,
            "return_json": True,
        }
        from naumi_agent.tools.base import ToolCall

        tool_call = ToolCall(
            id=f"slash-evolve-self-modify-{uuid.uuid4()}",
            name="self_modify",
            arguments=json.dumps(kwargs, ensure_ascii=False),
        )
        tool_result = await engine.execute_tool(tool_call, agent_name="cli")
        if tool_result.status != "success":
            console.print(f"[red]自我修改执行失败: {tool_result.content}[/red]")
            return
        modify_result_str = tool_result.content

    try:
        modify_payload = json.loads(modify_result_str)
        modify_result = modify_payload["result"]
        modify_report = modify_payload["report"]
        if not isinstance(modify_result, dict):
            raise TypeError("result 必须是对象")
        if not isinstance(modify_report, str):
            raise TypeError("report 必须是字符串")
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        console.print(f"[red]无法解析自我修改结果: {e}[/red]")
        return

    # Check if modification was applied
    if modify_result.get("status") != "applied":
        status = modify_result.get("status")
        title = "[bold red]❌ 修改未通过验证[/bold red]"
        border_style = "red"
        if status == "noop":
            title = "[bold yellow]⏭️ 无变更，已停止自我进化[/bold yellow]"
            border_style = "yellow"
        elif status == "rejected":
            title = "[bold red]❌ 自我修改已拒绝[/bold red]"
        console.print()
        console.print(
            Panel(
                Markdown(modify_report),
                title=title,
                border_style=border_style,
                padding=(1, 2),
            ),
        )
        return

    # Phase 3: Reflective evaluation
    console.print("[dim]Phase 3: 反思评估 — 对比修改前后质量...[/dim]")

    if self_evolve:
        with console.status("[bold green]评估质量变化...[/bold green]"):
            evolution_kwargs = {
                "target_file": target_file,
                "original_content": original_content,
                "new_content": new_content,
                "description": change_desc,
                "apply_decision": True,
                "return_json": True,
            }
            from naumi_agent.tools.base import ToolCall

            tool_call = ToolCall(
                id=f"slash-evolve-self-evolve-{uuid.uuid4()}",
                name="self_evolve",
                arguments=json.dumps(evolution_kwargs, ensure_ascii=False),
            )
            tool_result = await engine.execute_tool(tool_call, agent_name="cli")
            if tool_result.status != "success":
                console.print(f"[red]自我进化评估失败: {tool_result.content}[/red]")
                return
            evolution_result_str = tool_result.content

        try:
            evolution_payload = json.loads(evolution_result_str)
            cycle_result = evolution_payload["cycle_result"]
            eval_report = evolution_payload["report"]
            if not isinstance(cycle_result, dict):
                raise TypeError("cycle_result 必须是对象")
            if not isinstance(eval_report, str):
                raise TypeError("report 必须是字符串")
            apply_result = cycle_result.get("apply_result")
            if apply_result is not None and not isinstance(apply_result, dict):
                raise TypeError("apply_result 必须是对象")
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            console.print(f"[red]无法解析自我进化评估结果: {e}[/red]")
            return

        action = cycle_result.get("action")
        if action not in {"commit", "iterate", "rejected", "rollback"}:
            console.print(f"[red]未知自我进化动作: {action}[/red]")
            return

        console.print()
        console.print(
            Panel(
                Markdown(eval_report),
                title="[bold yellow]🧬 自我进化报告[/bold yellow]",
                border_style="yellow",
                padding=(1, 2),
            ),
        )

        # Phase 4: Act on decision
        if action == "rejected":
            console.print(f"[yellow]⚠️ {cycle_result.get('message', '自我进化评估已拒绝')}[/yellow]")
            console.print()
            return

        if action == "rollback":
            apply_result = cycle_result.get("apply_result") or {}
            if apply_result.get("action") == "reverted":
                console.print("[green]✅ 已通过安全闭环回滚到修改前状态[/green]")
            else:
                console.print(
                    f"[yellow]⚠️ {cycle_result.get('message', '建议检查后手动处理')}[/yellow]"
                )
            console.print()
            return

        if action == "iterate":
            console.print(
                "[yellow]🔄 效果不明确，修改已保留但建议继续迭代优化[/yellow]"
            )
        else:
            console.print("[green]✅ 质量提升，采纳修改[/green]")

    # Phase 5: Hot-reload
    reload_domain = target_file.split("/", 1)[0]
    if reload_domain not in {"tools", "memory", "skills"}:
        reload_domain = "tools"
    console.print(f"[bold yellow]🔄 正在热重载 {reload_domain} 域...[/bold yellow]")
    try:
        reload_result = await engine.reload_tools(reload_domain)
        msg = f"✅ 重载完成: {reload_result['reloaded']} 个模块"
        console.print(f"[green]{msg}[/green]")
    except Exception as e:
        console.print(f"[yellow]⚠️ 热重载失败: {e}[/yellow]")

    console.print()


def _show_evolve_history() -> None:
    """显示自我进化历史记录."""
    from rich.table import Table

    from naumi_agent.tools.self_evolve import get_evolution_history

    history = get_evolution_history()
    if not history:
        console.print("[dim]暂无进化记录[/dim]")
        return

    table = Table(
        title="🧬 自我进化历史",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("ID", style="dim")
    table.add_column("文件")
    table.add_column("轮次", justify="center")
    table.add_column("评分变化", justify="right")
    table.add_column("说明")

    for step in history:
        delta = step.score_delta
        delta_str = f"{delta:+.1f}" if delta else "-"
        style = "green" if delta and delta > 0 else "red" if delta and delta < 0 else None
        table.add_row(
            step.step_id,
            step.target_file,
            str(step.round_number),
            f"[{style}]{delta_str}[/{style}]" if style else delta_str,
            step.description[:40],
        )

    console.print(table)








def _show_hooks(engine: Any) -> None:
    """显示已注册的钩子."""
    from naumi_agent.hooks import HookPoint

    hooks = engine.hooks.list_hooks()
    if not hooks:
        console.print("[dim]没有已注册的钩子[/dim]")
    else:
        console.print("[bold]已注册钩子:[/bold]")
        for point, callbacks in hooks.items():
            try:
                label = HookPoint(point).value
            except ValueError:
                label = point
            console.print(f"  [cyan]{label}[/cyan]")
            for cb in callbacks:
                console.print(f"    • {cb}")

    trace = engine.hooks.get_trace()[-10:]
    if trace:
        console.print("\n[bold]最近触发:[/bold]")
        for entry in trace:
            suffix = " [yellow]拦截[/yellow]" if entry.aborted else ""
            error = f" [red]{entry.error}[/red]" if entry.error else ""
            console.print(
                f"  [magenta]{entry.point}[/magenta] → {entry.callback} "
                f"({entry.duration_ms}ms){suffix}{error}"
            )
    console.print()


async def _run_forge(engine: Any, arg: str) -> None:
    """执行工具锻造 — 根据描述生成新工具."""
    import re

    from rich.markdown import Markdown
    from rich.panel import Panel

    description = arg.strip()
    if not description:
        console.print("[yellow]用法: /forge <工具功能描述>[/yellow]")
        console.print("[dim]例: /forge 统计代码注释率的工具[/dim]")
        return

    forge_tool_instance = engine.tool_registry.get("forge_tool")
    if not forge_tool_instance:
        console.print("[red]锻造工具未注册[/red]")
        return

    console.print(f"[bold cyan]🔨 工具锻造: {description}[/bold cyan]")

    # Phase 1: Generate tool code via LLM, then fall back to deterministic scaffold.
    console.print("[dim]Phase 1: 尝试 LLM 生成工具代码...[/dim]")

    from naumi_agent.tools.forge import _TOOL_GENERATION_SYSTEM

    llm_output = None
    try:
        response = await engine._router.call(
            messages=[
                {"role": "system", "content": _TOOL_GENERATION_SYSTEM},
                {"role": "user", "content": f"请生成一个工具: {description}"},
            ],
            tier="capable",
        )
        llm_output = response.content.strip()
    except Exception as e:
        console.print(f"[yellow]LLM 调用失败，将使用确定性工具骨架: {e}[/yellow]")

    # Phase 2: Validate and save
    console.print("[dim]Phase 2: 验证并保存工具...[/dim]")

    with console.status("[bold green]锻造中...[/bold green]"):
        kwargs = {"description": description}
        if llm_output:
            kwargs["llm_output"] = llm_output
        from naumi_agent.tools.base import ToolCall

        tool_call = ToolCall(
            id=f"slash-forge-{uuid.uuid4()}",
            name="forge_tool",
            arguments=json.dumps(kwargs, ensure_ascii=False),
        )
        tool_result = await engine.execute_tool(tool_call, agent_name="cli")
        if tool_result.status != "success":
            console.print(f"[yellow]{tool_result.content}[/yellow]")
            return
        result_str = tool_result.content

    console.print()
    console.print(
        Panel(
            Markdown(result_str),
            title="[bold cyan]🔨 锻造结果[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        ),
    )

    # Phase 3: Register if forged
    if "锻造成功" in result_str:
        from naumi_agent.tools.forge import load_generated_tool

        # Extract tool name from result
        name_match = re.search(r"\*\*名称\*\*:\s*`(\w+)`", result_str)
        if name_match:
            tool_name = name_match.group(1)
            new_tool = load_generated_tool(tool_name)
            if new_tool:
                engine.tool_registry.register(new_tool)
                console.print(
                    f"[green]✅ 工具 `{tool_name}` 已注册到工具表，立即可用[/green]"
                )
            else:
                console.print(
                    "[yellow]⚠️ 工具已保存但加载失败，请重启 Agent[/yellow]"
                )

    console.print()


def _show_forge_list() -> None:
    """列出所有已锻造的工具."""
    from rich.table import Table

    from naumi_agent.tools.forge import list_generated_tools

    tools = list_generated_tools()
    if not tools:
        console.print("[dim]暂无锻造的工具[/dim]")
        return

    table = Table(
        title="🔨 已锻造工具",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("名称", style="cyan")
    table.add_column("描述")
    table.add_column("路径", style="dim")

    for t in tools:
        table.add_row(t["name"], t["description"], t["path"])

    console.print(table)


def _run_forge_remove(arg: str) -> None:
    """移除已锻造的工具."""
    from naumi_agent.tools.forge import remove_generated_tool

    tool_name = arg.strip()
    if not tool_name:
        console.print("[yellow]用法: /forge-remove <工具名称>[/yellow]")
        return

    if remove_generated_tool(tool_name):
        console.print(f"[green]✅ 已移除工具: {tool_name}[/green]")
    else:
        console.print(f"[red]未找到工具: {tool_name}[/red]")

async def _show_history(engine: Any, arg: str = "") -> None:
    """显示历史会话列表、搜索结果或预览."""
    from rich.markdown import Markdown

    from naumi_agent.ui.history_screen import (
        build_history_snapshot,
        render_history_preview,
        render_history_screen,
        render_session_delete_preview,
    )

    parts = arg.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else ""
    sub_arg = parts[1].strip() if len(parts) > 1 else ""

    if subcommand == "preview":
        if not sub_arg:
            console.print("[yellow]用法: /history preview <session_id>[/yellow]")
            return
        session = await engine.session_store.load(sub_arg)
        if session is None:
            console.print(f"[red]会话 {sub_arg} 不存在[/red]")
            return
        console.print(Markdown(render_history_preview(session)))
        return

    if subcommand in {"delete-preview", "delete_preview"}:
        if not sub_arg:
            console.print(
                "[yellow]用法: /history delete-preview <session_id>[/yellow]"
            )
            return
        preview = await engine.preview_session_delete(sub_arg)
        if preview is None:
            console.print(f"[red]会话 {sub_arg} 不存在[/red]")
            return
        console.print(Markdown(render_session_delete_preview(preview)))
        return

    if subcommand == "archive":
        if not sub_arg:
            console.print("[yellow]用法: /history archive <session_id>[/yellow]")
            return
        if await engine.archive_session(sub_arg):
            console.print(f"[green]已归档会话:[/green] {sub_arg}")
        else:
            console.print(f"[red]会话 {sub_arg} 不存在[/red]")
        return

    if subcommand == "delete":
        if not sub_arg:
            console.print("[yellow]用法: /history delete <session_id>[/yellow]")
            return
        await _delete_session(engine, sub_arg)
        return

    query = arg.strip()
    sessions, total = await engine.list_sessions(page=1, page_size=20, query=query)
    git = _get_git_info()
    snapshot = build_history_snapshot(
        sessions,
        total=total,
        query=query,
        current_session_id=engine._session.id if engine._session else None,
        fallback_workspace=str(getattr(engine, "workspace_root", "")),
        fallback_git_branch=str(git["branch"] or ""),
    )
    console.print(render_history_screen(snapshot))


def _replay_session_to_cli(cli: Any, session: Any, engine: Any = None) -> None:
    """将会话消息通过 UIMessage 适配器回放到 CLI 输出区."""
    if hasattr(cli, "reset_output"):
        cli.reset_output()
    elif hasattr(cli, "clear_output"):
        cli.clear_output()

    title = session.title or session.id
    display_messages = (
        getattr(engine, "_full_history", None)
        if engine is not None
        else None
    ) or session.messages
    msg_count = len(display_messages)

    from naumi_agent.cli.renderers import CLIRenderer
    from naumi_agent.ui.messages.replay import replay_messages

    renderer = CLIRenderer(show_reasoning=_show_reasoning_text)
    ui_messages = replay_messages(display_messages)

    cli.append_output(
        f"\033[2m━━━ 恢复会话: {title} ({msg_count}条消息) ━━━\033[0m\n"
    )

    for msg in ui_messages:
        ansi_text = renderer.render(msg)
        if ansi_text is not None:
            cli.append_output(ansi_text)

    cli.append_output(
        "\033[2m━━━ 会话已恢复，继续对话或 /new 开始新会话 ━━━\033[0m\n\n"
    )
    if engine is not None:
        _show_cli_status(cli, engine)


def _build_session_stats(session: Any, engine: Any = None) -> str:
    """Build ANSI stats text from session data for immediate display."""
    parts: list[str] = []
    # Model
    model = getattr(session, "model", "")
    if model:
        parts.append(model)
    # Messages
    user_msgs = sum(1 for m in session.messages if m.get("role") == "user")
    parts.append(f"消息: {user_msgs}条")
    # Tokens & cost
    tokens = getattr(session, "total_tokens", 0)
    cost = getattr(session, "total_cost_usd", 0.0)
    if tokens:
        parts.append(f"Token: {tokens}")
    if cost > 0:
        parts.append(f"费用: ${cost:.4f}")
    # Context window
    if engine:
        ctx = engine.get_context_info()
        ctx_pct = ctx["percentage"]
        used_k = ctx["used"] / 1000
        window_k = ctx["window"] / 1000
        parts.append(f"上下文: {used_k:.0f}K/{window_k:.0f}K ({ctx_pct}%)")
        budget = engine.get_budget_info()
        parts.append(f"预算: {format_budget_detail(budget)}")
    # Git
    git = _get_git_info()
    if git["branch"]:
        tag = git["branch"] + ("*" if git["dirty"] else "")
        parts.append(f"📂 {tag}")
    if not parts:
        return ""
    return "\033[2m  " + " | ".join(parts) + "\033[0m\n"


async def _load_session(engine: Any, session_id: str) -> None:
    """加载历史会话 — 完整回放到显示区."""
    loaded = await engine.load_session(session_id)
    if loaded:
        session = engine._session
        if session:
            if _active_cli:
                _replay_session_to_cli(_active_cli, session, engine=engine)
            else:
                console.print(
                    f"[green]已加载会话:[/green] {session.title or session_id} "
                    f"({len(session.messages)}条消息)"
                )
                # Non-interactive mode fallback: brief summary
                user_msgs = [
                    m for m in session.messages
                    if m.get("role") in ("user", "assistant")
                ]
                if user_msgs:
                    console.print("[dim]--- 最近对话 ---[/dim]")
                    for m in user_msgs[-6:]:
                        role = m.get("role", "")
                        content = m.get("content") or ""
                        if len(content) > 100:
                            content = content[:97] + "..."
                        if not content:
                            tool_names = [
                                tc.get("function", {}).get("name", "")
                                for tc in m.get("tool_calls", [])
                                if isinstance(tc, dict)
                            ]
                            content = (
                                f"[调用工具: {', '.join(tool_names)}]"
                                if tool_names
                                else "[无文本内容]"
                            )
                        label = "[blue]你[/blue]" if role == "user" else "[green]Naumi[/green]"
                        console.print(f"  {label}: {content}")
                console.print()
    else:
        console.print(f"[red]会话 {session_id} 不存在[/red]")
        console.print("[dim]使用 /history 查看可用会话[/dim]")


async def _resume_latest(engine: Any) -> None:
    """加载最近一个历史会话并继续对话."""
    page = 1
    page_size = 20
    checked = 0

    while True:
        sessions, total = await engine.list_sessions(page=page, page_size=page_size)
        if not sessions:
            break

        for session in sessions:
            if _has_user_conversation(session):
                await _load_session(engine, session.id)
                return

        checked += len(sessions)
        if checked >= total:
            break
        page += 1

    console.print("[dim]暂无可继续的历史对话[/dim]")


def _has_user_conversation(session: Any) -> bool:
    """判断会话是否包含真实用户对话，跳过仅 system prompt 的空会话."""
    return any(m.get("role") == "user" for m in session.messages)


async def _interactive_load(engine: Any, arg: str) -> None:
    """加载会话：支持 ID、编号选择，无参数时显示编号列表."""
    if arg:
        # Try as number from recent list first, then as session ID
        if arg.isdigit():
            sessions, _ = await engine.list_sessions(page=1, page_size=10)
            idx = int(arg) - 1
            if 0 <= idx < len(sessions):
                await _load_session(engine, sessions[idx].id)
                return
        await _load_session(engine, arg)
        return

    # No arg: show numbered picker
    sessions, total = await engine.list_sessions(page=1, page_size=10)
    if not sessions:
        console.print("[dim]暂无历史会话[/dim]")
        return

    from rich.table import Table

    table = Table(title="选择会话", show_lines=False)
    table.add_column("#", style="bold yellow", width=3)
    table.add_column("ID", style="cyan", width=12)
    table.add_column("标题", max_width=36)
    table.add_column("消息数", justify="right", width=6)
    table.add_column("更新时间", width=16)

    for i, s in enumerate(sessions, 1):
        title = s.title or "新会话"
        if len(title) > 34:
            title = title[:32] + "…"
        table.add_row(
            str(i),
            s.id,
            title,
            str(len(s.messages)),
            s.updated_at.strftime("%m-%d %H:%M"),
        )

    console.print(table)
    console.print("[dim]输入 /load <编号或ID> 加载，或 /r 继续最近对话[/dim]")


async def _delete_session(engine: Any, session_id: str) -> None:
    """删除指定会话."""
    from naumi_agent.harness.coordinator import ReconciliationCoordinatorOutcome

    result = await engine.delete_session_detailed(session_id)
    if result.outcome is ReconciliationCoordinatorOutcome.COMPLETED:
        console.print(f"[green]已删除会话:[/green] {session_id}")
    elif result.outcome is ReconciliationCoordinatorOutcome.NOT_FOUND:
        console.print(f"[red]会话 {session_id} 不存在[/red]")
    elif result.outcome is ReconciliationCoordinatorOutcome.RETRY_SCHEDULED:
        console.print(
            "[yellow]会话删除已进入安全重试队列:[/yellow] "
            f"{session_id} · request {result.request_id}"
        )
    elif result.outcome is ReconciliationCoordinatorOutcome.RETRY_EXHAUSTED:
        console.print(
            "[red]会话删除协调重试已耗尽，请人工检查:[/red] "
            f"{session_id} · request {result.request_id}"
        )
    else:
        console.print(f"[red]会话 {session_id} 的生命周期策略阻止删除[/red]")


async def _new_conversation(engine: Any) -> None:
    """保存当前会话并开始新对话."""
    # Save current session if it has messages
    if engine._messages and any(m.get("role") == "user" for m in engine._messages):
        try:
            await engine._save_session()
            console.print("[dim]已保存当前会话[/dim]")
        except Exception:
            pass
    engine.reset()
    engine._session = None
    if _active_cli:
        _active_cli.clear_output()
    console.print("[green]新对话已开始[/green]")


def _show_skills(engine: Any) -> None:
    """显示已加载的 Skill."""
    if not hasattr(engine, "skill_loader"):
        console.print("[dim]Skill 系统未加载[/dim]")
        return

    skills = engine.skill_loader.all()
    if not skills:
        console.print("[dim]没有已加载的 Skill[/dim]")
        console.print(
            "[dim]提示：将 SKILL.md 文件放入 .naumi/skills/<skill-name>/ 目录[/dim]",
        )
        return

    console.print("[bold]已加载的 Skill:[/bold]")
    for skill in skills:
        args_info = ""
        if skill.arguments:
            parts = []
            for a in skill.arguments:
                tag = "*" if a.required else ""
                parts.append(f"{a.name}{tag}")
            args_info = f" ({', '.join(parts)})"
        console.print(
            f"  [cyan]/{skill.name}[/cyan] — {skill.description}{args_info}",
        )
    console.print()


async def _run_skill(engine: Any, skill_name: str, arguments: str) -> None:
    """通过 CLI 执行一个 Skill."""
    skill = engine.skill_loader.get(skill_name)
    if not skill:
        console.print(f"[red]Skill '{skill_name}' 未找到[/red]")
        return

    if not arguments and skill.arguments:
        required = [a for a in skill.arguments if a.required]
        if required:
            names = ", ".join(a.name for a in required)
            console.print(
                f"[yellow]用法: /{skill_name} <{names}>[/yellow]",
            )
            return

    console.print(f"[bold yellow]⚡ Skill: {skill_name}[/bold yellow]")
    rendered = skill.render(arguments=arguments)

    # 将渲染后的指令作为用户消息注入 engine 执行
    with console.status("[bold green]执行中...[/bold green]"):
        result = await engine.run_streaming(
            rendered,
            CallbackEventSink(_cli_event_handler),
        )

    if result.status == "error" and result.error:
        console.print(f"[red]错误: {result.error}[/red]")
        return

    if result.response:
        console.print()
        console.print(
            Panel(
                Markdown(excerpt_markdown_code_blocks(result.response)),
                title=f"[bold yellow]⚡ {skill_name}[/bold yellow]",
                border_style="yellow",
                padding=(1, 2),
            ),
        )


async def _handle_memory(engine: Any, arg: str) -> None:
    """处理 /memory 命令及其子命令."""
    parts = arg.strip().split(maxsplit=1) if arg else []
    subcmd = parts[0] if parts else "stats"
    sub_arg = parts[1] if len(parts) > 1 else ""

    if subcmd == "stats":
        await _memory_stats(engine)
    elif subcmd == "search":
        if not sub_arg:
            console.print("[yellow]用法: /memory search <查询>[/yellow]")
        else:
            await _memory_search(engine, sub_arg)
    elif subcmd == "clean":
        await _memory_clean(engine)
    elif subcmd == "export":
        await _memory_export(engine)
    else:
        console.print("[bold]/memory 子命令:[/bold]")
        console.print("  [cyan]stats[/cyan]          显示记忆统计")
        console.print("  [cyan]search <查询>[/cyan]  搜索记忆")
        console.print("  [cyan]clean[/cyan]          整理记忆（去重+遗忘）")
        console.print("  [cyan]export[/cyan]         导出所有记忆为 JSON")


async def _memory_stats(engine: Any) -> None:
    """显示记忆统计."""
    from rich.table import Table

    stats = await engine.long_term_memory.stats()

    if stats.total == 0:
        console.print("[dim]记忆库为空[/dim]")
        return

    table = Table(title="记忆统计", show_lines=False)
    table.add_column("指标", style="cyan", width=16)
    table.add_column("值", justify="right", width=10)

    table.add_row("总计", str(stats.total))
    table.add_row("活跃", str(stats.active))
    table.add_row("休眠", str(stats.dormant))
    table.add_row("平均访问", f"{stats.avg_access_count:.1f}")

    console.print(table)

    if stats.by_category:
        console.print("\n[bold]按类别:[/bold]")
        for cat, count in sorted(stats.by_category.items()):
            bar = "█" * min(count, 20)
            console.print(f"  {cat:12s} {bar} {count}")
    console.print()


async def _memory_search(engine: Any, query: str) -> None:
    """搜索记忆."""
    results = await engine.long_term_memory.search(query, top_k=10)
    if not results:
        console.print("[dim]没有找到匹配的记忆[/dim]")
        return

    console.print(f"[bold]搜索结果 ({len(results)} 条):[/bold]")
    for r in results:
        status_tag = ""
        if r.entry.status == "dormant":
            status_tag = " [dim][休眠][/dim]"
        console.print(
            f"  • [{r.entry.category}] "
            f"(相关度 {r.relevance:.0%}) "
            f"{r.entry.content[:80]}{status_tag}",
        )
    console.print()


async def _memory_clean(engine: Any) -> None:
    """整理记忆."""
    console.print("[bold yellow]整理记忆中...[/bold yellow]")
    result = await engine.long_term_memory.consolidate()
    console.print(
        f"[green]整理完成:[/green] "
        f"去重 {result['deduped']} 条，遗忘 {result['forgotten']} 条",
    )


async def _memory_export(engine: Any) -> None:
    """导出记忆."""
    from pathlib import Path

    data = await engine.long_term_memory.export_memories()
    export_path = Path("memory_export.json")
    export_path.write_text(data, encoding="utf-8")
    count = data.count('"id"')
    console.print(f"[green]已导出 {count} 条记忆到 {export_path}[/green]")


async def _run_browse(engine: Any, arg: str) -> None:
    """打开 URL 并显示 SoM 交互元素."""
    if not arg:
        console.print("[yellow]用法: /browse <url>[/yellow]")
        return

    console.print(f"[bold cyan]🌐 导航到 {arg}...[/bold cyan]")
    try:
        result = await engine._browser_session.goto(arg.strip())
        elements = result.get("elements", [])
        console.print(f"[green]✅ 页面已加载，发现 {len(elements)} 个交互元素[/green]")
        if elements:
            from rich.table import Table

            table = Table(title="交互元素 (SoM)", show_lines=False)
            table.add_column("ID", style="bold yellow", width=4)
            table.add_column("标签", max_width=30)
            table.add_column("类型", style="cyan", width=12)
            table.add_column("操作", style="dim", max_width=40)
            for el in elements[:20]:
                tag = el.get("tag", "?")
                label = el.get("label", el.get("text", ""))[:30]
                action = el.get("action", "")
                if isinstance(action, dict):
                    action = action.get("type", "")
                table.add_row(str(el.get("id", "")), label, tag, str(action)[:40])
            console.print(table)
            if len(elements) > 20:
                console.print(f"[dim]... 还有 {len(elements) - 20} 个元素[/dim]")
    except Exception as exc:
        console.print(f"[red]导航失败: {exc}[/red]")


async def _run_autobrowse(engine: Any, arg: str) -> None:
    """启动自主浏览器任务."""
    if not arg:
        console.print("[yellow]用法: /autobrowse <任务描述>[/yellow]")
        return

    console.print(f"[bold cyan]🤖 启动自主浏览器任务: {arg}[/bold cyan]")
    try:
        runner = engine.task_runner
        run = runner.create_run(instruction=arg.strip())
        run_id = run["id"]
        console.print(f"[green]任务已创建: {run_id}[/green]")

        with console.status("[bold green]执行中...[/bold green]"):
            await runner.process_queue()

        updated = runner.get_run(run_id)
        if updated:
            status = updated.get("status", "unknown")
            summary = updated.get("summary", "")
            if status == "completed":
                console.print("[green]✅ 任务完成[/green]")
                if summary:
                    console.print(
                        Panel(
                            summary,
                            title="[bold green]结果[/bold green]",
                            border_style="green",
                        )
                    )
            elif status == "waiting_for_instruction":
                console.print("[yellow]⏸ 任务等待指令[/yellow]")
                console.print(f"[dim]使用 /task-reply {run_id} <指令> 回复[/dim]")
            elif status == "failed":
                error = updated.get("error", {})
                msg = error.get("message", summary) if isinstance(error, dict) else str(error)
                console.print(f"[red]❌ 任务失败: {msg}[/red]")
            else:
                console.print(f"[dim]状态: {status}[/dim]")
                if summary:
                    console.print(summary[:500])
    except Exception as exc:
        console.print(f"[red]任务执行失败: {exc}[/red]")


async def _run_browser_stop(engine: Any) -> None:
    """停止浏览器会话."""
    console.print("[bold yellow]🛑 停止浏览器...[/bold yellow]")
    try:
        await engine._browser_session.stop()
        console.print("[green]✅ 浏览器已停止[/green]")
    except Exception as exc:
        console.print(f"[red]停止失败: {exc}[/red]")


async def _run_browser_state(engine: Any) -> None:
    """显示浏览器调试状态."""
    import json

    try:
        state = engine._browser_session.get_debug_state(20)
        console.print(json.dumps(state, indent=2, default=str))
    except Exception as exc:
        console.print(f"[red]获取状态失败: {exc}[/red]")


async def _run_browser_screenshot(engine: Any) -> None:
    """截取当前页面截图."""
    try:
        b64 = await engine._browser_session.screenshot_base64()
        out = Path("screenshot.png")
        import base64

        out.write_bytes(base64.b64decode(b64))
        console.print(f"[green]✅ 截图已保存到 {out}[/green]")
    except Exception as exc:
        console.print(f"[red]截图失败: {exc}[/red]")


async def _run_browser_daemon(engine: Any, arg: str) -> None:
    """调用外部 browser-debugging-daemon."""
    parts = shlex.split(arg) if arg.strip() else []
    subcommand = parts[0] if parts else "health"

    async def _execute(tool_name: str, **kwargs: Any) -> None:
        tool = engine.tool_registry.get(tool_name)
        if not tool:
            console.print(f"[red]工具未注册: {tool_name}[/red]")
            return
        from naumi_agent.tools.base import ToolCall

        tool_call = ToolCall(
            id=f"slash-bdaemon-{tool_name}-{uuid.uuid4()}",
            name=tool_name,
            arguments=json.dumps(kwargs, ensure_ascii=False),
        )
        tool_result = await engine.execute_tool(tool_call, agent_name="cli")
        if tool_result.status != "success":
            console.print(f"[yellow]{tool_result.content}[/yellow]")
            return
        result = tool_result.content
        console.print(
            Panel(
                Markdown(result),
                title="[bold cyan]browser-debugging-daemon[/bold cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    match subcommand:
        case "health":
            await _execute("browser_daemon_health")
        case "start":
            await _execute("browser_daemon_start")
        case "dashboard":
            await _execute("browser_daemon_dashboard")
        case "run":
            task = " ".join(parts[1:]).strip()
            if not task:
                console.print("[yellow]用法: /bdaemon run <任务描述>[/yellow]")
                return
            await _execute("browser_daemon_run", task_instruction=task)
        case "list" | "runs":
            limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
            await _execute("browser_daemon_list_runs", limit=limit)
        case "status":
            if len(parts) < 2:
                console.print("[yellow]用法: /bdaemon status <运行ID>[/yellow]")
                return
            await _execute("browser_daemon_run_status", run_id=parts[1])
        case "watch":
            if len(parts) < 2:
                console.print("[yellow]用法: /bdaemon watch <运行ID> [超时毫秒][/yellow]")
                return
            timeout_ms = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30000
            await _execute("browser_daemon_watch", run_id=parts[1], timeout_ms=timeout_ms)
        case "reply":
            if len(parts) < 3:
                console.print("[yellow]用法: /bdaemon reply <运行ID> <指令>[/yellow]")
                return
            await _execute(
                "browser_daemon_reply",
                run_id=parts[1],
                instruction=" ".join(parts[2:]),
            )
        case "resume":
            if len(parts) < 2:
                console.print("[yellow]用法: /bdaemon resume <运行ID> [指令][/yellow]")
                return
            instruction = " ".join(parts[2:]) if len(parts) > 2 else ""
            await _execute("browser_daemon_resume", run_id=parts[1], instruction=instruction)
        case "abort":
            if len(parts) < 2:
                console.print("[yellow]用法: /bdaemon abort <运行ID> [原因][/yellow]")
                return
            reason = " ".join(parts[2:]) if len(parts) > 2 else ""
            await _execute("browser_daemon_abort", run_id=parts[1], reason=reason)
        case "manual" | "manual-control":
            if len(parts) < 2:
                console.print("[yellow]用法: /bdaemon manual <运行ID> [原因][/yellow]")
                return
            reason = " ".join(parts[2:]) if len(parts) > 2 else ""
            await _execute("browser_daemon_manual_control", run_id=parts[1], reason=reason)
        case _:
            console.print(
                "[yellow]未知 bdaemon 子命令[/yellow]\n"
                "[dim]可用: health/start/dashboard/run/list/status/watch/reply/"
                "resume/abort/manual[/dim]"
            )


async def _run_tasks_list(engine: Any) -> None:
    """显示 todo / subagent / background / browser 综合任务面板."""
    from rich.text import Text

    from naumi_agent.ui.task_panel import render_task_panel

    console.print(Text.from_ansi(await render_task_panel(engine, limit=20)))


async def _run_task_detail(engine: Any, arg: str) -> None:
    """查看任务运行详情."""
    if not arg:
        console.print("[yellow]用法: /task <id>[/yellow]")
        return
    runner = engine.task_runner
    run = runner.get_run(arg.strip())
    if not run:
        console.print(f"[red]任务 {arg} 不存在[/red]")
        return

    import json

    console.print(json.dumps(run, indent=2, default=str, ensure_ascii=False)[:2000])


async def _run_task_reply(engine: Any, arg: str) -> None:
    """回复等待中的任务."""
    parts = arg.strip().split(maxsplit=1)
    if len(parts) < 2:
        console.print("[yellow]用法: /task-reply <id> <指令>[/yellow]")
        return
    run_id, instruction = parts
    runner = engine.task_runner
    try:
        await runner.reply_to_run(run_id, instruction)
        console.print(f"[green]✅ 已回复任务 {run_id}[/green]")
        with console.status("[bold green]继续执行中...[/bold green]"):
            await runner.process_queue()
        updated = runner.get_run(run_id)
        if updated:
            console.print(f"[dim]状态: {updated.get('status')}[/dim]")
    except Exception as exc:
        console.print(f"[red]回复失败: {exc}[/red]")


async def _run_task_abort(engine: Any, arg: str) -> None:
    """中止运行中的任务."""
    if not arg:
        console.print("[yellow]用法: /task-abort <id>[/yellow]")
        return
    runner = engine.task_runner
    run = runner.get_run(arg.strip())
    if not run:
        console.print(f"[red]任务 {arg} 不存在[/red]")
        return
    runner.abort_run(arg.strip(), reason="User requested")
    console.print(f"[green]✅ 已中止任务 {arg}[/green]")


async def _run_task_resume(engine: Any, arg: str) -> None:
    """从手动控制中恢复任务."""
    if not arg:
        console.print("[yellow]用法: /task-resume <id>[/yellow]")
        return
    runner = engine.task_runner
    try:
        await runner.resume_run(arg.strip())
        console.print(f"[green]✅ 已恢复任务 {arg}[/green]")
        with console.status("[bold green]继续执行中...[/bold green]"):
            await runner.process_queue()
    except Exception as exc:
        console.print(f"[red]恢复失败: {exc}[/red]")


async def _run_security_scan(engine: Any, arg: str, profile: str = "quick") -> None:
    """执行安全扫描."""
    if not arg:
        console.print(f"[yellow]用法: /scan{'-full' if profile == 'full' else ''} <url>[/yellow]")
        return

    url = arg.strip()
    label = "完整" if profile == "full" else "快速"
    console.print(f"[bold red]🔒 {label}安全扫描: {url}[/bold red]")

    try:
        if not engine._browser_session.page:
            await engine._browser_session.start({"source": "auto"})
        await engine._browser_session.goto(url)
    except Exception as exc:
        console.print(f"[red]无法导航到 {url}: {exc}[/red]")
        return

    auditor = engine.security_auditor
    auditor.clear()
    console.print(f"[dim]扫描中 (profile={profile})...[/dim]")

    with console.status("[bold red]安全扫描中...[/bold red]"):
        try:
            result = await auditor.full_audit(profile=profile)
        except Exception as exc:
            console.print(f"[red]扫描失败: {exc}[/red]")
            return

    summary = result.get("summary", {})
    total = summary.get("totalFindings", 0)
    critical = summary.get("criticalCount", 0)
    high = summary.get("highCount", 0)
    medium = summary.get("mediumCount", 0)
    low = summary.get("lowCount", 0)

    console.print(
        Panel(
            f"[bold]扫描完成[/bold]\n\n"
            f"总发现: {total}\n"
            f"[red]严重: {critical}[/red] | "
            f"[yellow]高危: {high}[/yellow] | "
            f"[cyan]中危: {medium}[/cyan] | "
            f"[dim]低危: {low}[/dim]",
            title=f"[bold red]🔒 {label}安全扫描[/bold red]",
            border_style="red",
        )
    )

    findings = auditor.get_results(min_severity="high")
    if findings:
        console.print("[bold]高/严重发现:[/bold]")
        for f in findings[:15]:
            severity = f.get("severity", "?")
            title = f.get("title", "?")
            cat = f.get("category", "?")
            style = "red" if severity == "critical" else "yellow"
            console.print(f"  [{style}][{severity}][/{style}] [{cat}] {title}")
        if len(findings) > 15:
            console.print(f"[dim]... 还有 {len(findings) - 15} 个发现[/dim]")


async def _run_scan_report(engine: Any, arg: str) -> None:
    """导出最新扫描报告."""
    fmt = arg.strip() or "json"
    if fmt not in ("json", "sarif", "html"):
        console.print("[yellow]格式: json, sarif, html[/yellow]")
        return

    auditor = engine.security_auditor
    if not auditor.results:
        console.print("[dim]暂无扫描结果，先执行 /scan <url>[/dim]")
        return

    try:
        result = await auditor.export_report(fmt=fmt)
    except Exception as exc:
        console.print(f"[red]导出失败: {exc}[/red]")
        return

    if fmt == "json":
        import json

        out = Path("security_report.json")
        out.write_text(
            json.dumps(
                result.get("data"), indent=2, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        console.print(f"[green]✅ JSON 报告已保存到 {out}[/green]")
    elif fmt == "sarif":
        import json

        out = Path("security_report.sarif")
        out.write_text(json.dumps(result.get("sarif"), indent=2), encoding="utf-8")
        console.print(f"[green]✅ SARIF 报告已保存到 {out}[/green]")
    elif fmt == "html":
        out = Path("security_report.html")
        out.write_text(result.get("html", ""), encoding="utf-8")
        console.print(f"[green]✅ HTML 报告已保存到 {out}[/green]")


async def _run_scan_baseline(engine: Any, arg: str) -> None:
    """保存扫描为基线."""
    if not arg:
        console.print("[yellow]用法: /scan-baseline <url>[/yellow]")
        return

    url = arg.strip()
    console.print(f"[bold cyan]📊 保存基线: {url}[/bold cyan]")

    try:
        if not engine._browser_session.page:
            await engine._browser_session.start({"source": "auto"})
        await engine._browser_session.goto(url)
    except Exception as exc:
        console.print(f"[red]无法导航到 {url}: {exc}[/red]")
        return

    auditor = engine.security_auditor
    with console.status("[bold green]扫描并保存基线...[/bold green]"):
        await auditor.full_audit(profile="standard")

    baseline_path = Path("security_baseline.json")
    auditor.save_baseline(str(baseline_path))
    console.print(
        f"[green]✅ 基线已保存到 {baseline_path} "
        f"({len(auditor.results)} 个发现)[/green]"
    )


def _run_btemplate_list(engine: Any) -> None:
    """列出浏览器任务模板."""
    from rich.table import Table

    runner = engine.task_runner
    templates = runner.list_templates()
    if not templates:
        console.print("[dim]暂无浏览器任务模板[/dim]")
        return

    table = Table(title="浏览器任务模板", show_lines=False)
    table.add_column("ID", style="cyan", width=8)
    table.add_column("名称", max_width=30)
    table.add_column("超时", justify="right", width=8)
    table.add_column("规则数", justify="right", width=6)

    for t in templates:
        tid = (t.get("id") or "")[:8]
        name = (t.get("name") or "")[:30]
        tp = t.get("timeoutPolicy", {})
        max_steps = tp.get("maxSteps", "?")
        rules = len(t.get("successRules", []))
        table.add_row(tid, name, str(max_steps), str(rules))

    console.print(table)


async def _run_btemplate_run(engine: Any, arg: str) -> None:
    """从模板创建运行."""
    if not arg:
        console.print("[yellow]用法: /btemplate-run <template_id>[/yellow]")
        return

    runner = engine.task_runner
    template = runner.get_template(arg.strip())
    if not template:
        console.print(f"[red]模板 {arg} 不存在[/red]")
        return

    try:
        run = runner.create_run_from_template(arg.strip())
        run_id = run["id"]
        console.print(f"[green]任务已创建: {run_id}[/green]")

        with console.status("[bold green]执行中...[/bold green]"):
            await runner.process_queue()

        updated = runner.get_run(run_id)
        if updated:
            console.print(f"[dim]状态: {updated.get('status')}[/dim]")
            summary = updated.get("summary", "")
            if summary:
                console.print(summary[:500])
    except Exception as exc:
        console.print(f"[red]执行失败: {exc}[/red]")


async def _run_btemplate_compare(engine: Any, arg: str) -> None:
    """比较模板运行结果."""
    if not arg:
        console.print("[yellow]用法: /btemplate-compare <template_id>[/yellow]")
        return

    runner = engine.task_runner
    comparison = runner.compare_template_runs(arg.strip())
    if not comparison:
        console.print("[dim]没有可比较的运行结果[/dim]")
        return

    import json

    console.print(json.dumps(comparison, indent=2, default=str, ensure_ascii=False)[:2000])


def _check_api_key(config: AppConfig) -> None:
    if not config.models.api_key:
        console.print("[yellow]警告: 未设置 API Key。[/yellow]")
        console.print("  [dim]export NAUMI_MODELS__API_KEY=your-key-here[/dim]")
        console.print("  [dim]也可重新运行首次引导，将密钥保存到系统凭据库。[/dim]")
        console.print()


def cli() -> None:
    app()


def naumiagent_cli() -> None:
    naumiagent_app(prog_name="naumiagent")


@workbench_app.command("export-audit")
def export_audit(
    session_id: str = typer.Option(..., "--session-id", "-s", help="会话 ID"),
    output: Path = typer.Option(..., "--output", "-o", help="输出文件路径"),
    event_type: str | None = typer.Option(None, "--event-type", "-t", help="事件类型"),
    actor: str | None = typer.Option(None, "--actor", "-a", help="执行者"),
    subject_id: str | None = typer.Option(None, "--subject-id", help="对象 ID"),
    severity: str | None = typer.Option(None, "--severity", help="严重级别"),
    correlation_id: str | None = typer.Option(None, "--correlation-id", help="关联 ID"),
    parent_event_id: str | None = typer.Option(None, "--parent-event-id", help="父事件 ID"),
    since: str | None = typer.Option(None, "--since", help="起始时间 ISO 字符串"),
    fmt: str = typer.Option("json", "--format", help="输出格式: json 或 ndjson"),
    config: str = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="配置文件路径"),
) -> None:
    """导出审计事件到本地文件，并自动脱敏."""
    resolved = _resolve_config_path(config)
    cfg = AppConfig.from_yaml(resolved)

    async def _run() -> dict[str, Any]:
        store = WorkbenchStore(cfg.memory.session_db_path)
        return await export_audit_events(
            store,
            session_id,
            str(output),
            event_type=event_type,
            subject_id=subject_id,
            actor=actor,
            severity=severity,
            correlation_id=correlation_id,
            parent_event_id=parent_event_id,
            since=since,
            fmt=fmt,
        )

    try:
        result = asyncio.run(_run())
    except Exception as e:
        console.print(f"[red]导出失败: {e}[/red]")
        raise typer.Exit(code=1) from e

    console.print(
        f"[green]已导出 {result['count']} 条事件到 {result['output_path']} "
        f"(格式: {result['format']})[/green]"
    )


if __name__ == "__main__":
    cli()
