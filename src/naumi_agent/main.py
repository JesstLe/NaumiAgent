"""NaumiAgent CLI 入口."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from naumi_agent.config.settings import AppConfig

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer(
    name="naumi",
    help="NaumiAgent — 通用智能 Agent",
    no_args_is_help=True,
)
console = Console()


def _resolve_config_path(path: str) -> str:
    """如果指定路径存在就直接用，否则回退到项目根目录的 config.yaml."""
    if Path(path).exists():
        return path
    fallback = str(_PROJECT_ROOT / "config.yaml")
    return fallback


@app.command()
def chat(
    config: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径"),
    tui: bool = typer.Option(False, "--tui", "-t", help="启动 TUI 界面"),
) -> None:
    """启动交互式对话."""
    if tui:
        _launch_tui(config)
    else:
        asyncio.run(_chat(config))


def _launch_tui(config_path: str) -> None:
    from naumi_agent.log_setup import setup_logging
    from naumi_agent.orchestrator.engine import AgentEngine
    from naumi_agent.tui.app import NaumiApp

    resolved = _resolve_config_path(config_path)
    config = AppConfig.from_yaml(resolved)
    setup_logging(config.log_level)
    _check_api_key(config)
    engine = AgentEngine(config)
    app = NaumiApp(engine)
    app.run()


async def _chat(config_path: str) -> None:
    from naumi_agent.log_setup import setup_logging
    from naumi_agent.orchestrator.engine import AgentEngine

    resolved = _resolve_config_path(config_path)
    config = AppConfig.from_yaml(resolved)
    setup_logging(config.log_level)
    _check_api_key(config)
    engine = AgentEngine(config)

    _print_banner()

    while True:
        try:
            user_input = console.input("[bold blue]你>[/bold blue] ").strip()
        except (EOFError, KeyboardInterrupt):
            await engine.shutdown()
            console.print("\n[green]再见！[/green]")
            break

        if not user_input:
            continue

        if user_input in ("/quit", "/exit", "exit"):
            await engine.shutdown()
            console.print("[green]再见！[/green]")
            break

        if user_input.startswith("/"):
            await _handle_command(engine, user_input)
            continue

        with console.status("[bold green]NaumiAgent 思考中...[/bold green]"):
            result = await engine.run(user_input)

        if result.status == "error" and result.error:
            console.print(f"[red]错误: {result.error}[/red]")
            continue

        # 渲染 Markdown 响应
        console.print()
        console.print(
            Panel(
                Markdown(result.response),
                title="[bold green]NaumiAgent[/bold green]",
                border_style="green",
                padding=(1, 2),
            )
        )

        # 显示统计
        stats = Text()
        stats.append(f"轮次: {result.usage.turns}", style="dim")
        stats.append(" | ", style="dim")
        total_tok = result.usage.total_input_tokens + result.usage.total_output_tokens
        stats.append(f"Token: {total_tok}", style="dim")
        stats.append(" | ", style="dim")
        stats.append(f"费用: ${result.usage.total_cost_usd:.4f}", style="dim")
        if result.status != "completed":
            stats.append(f" | 状态: {result.status}", style="yellow")
        console.print(stats)
        console.print()


@app.command()
def run(
    task: str = typer.Argument(help="要执行的任务"),
    config: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径"),
) -> None:
    """执行单个任务."""
    asyncio.run(_run_task(task, config))


async def _run_task(task: str, config_path: str) -> None:
    from naumi_agent.log_setup import setup_logging
    from naumi_agent.orchestrator.engine import AgentEngine

    resolved = _resolve_config_path(config_path)
    config = AppConfig.from_yaml(resolved)
    setup_logging(config.log_level)
    engine = AgentEngine(config)

    with console.status("[bold green]执行中...[/bold green]"):
        result = await engine.run(task)

    console.print(Markdown(result.response))


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="监听地址"),
    port: int = typer.Option(8080, "--port", "-p", help="监听端口"),
    config: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径"),
    reload: bool = typer.Option(False, "--reload", help="开发模式热重载"),
) -> None:
    """启动 REST API 服务."""
    import uvicorn

    if reload:
        uvicorn.run(
            "naumi_agent.api.app:app",
            host=host,
            port=port,
            reload=True,
            reload_dirs=["src/naumi_agent"],
        )
    else:
        uvicorn.run(
            "naumi_agent.api.app:app",
            host=host,
            port=port,
            workers=1,
            log_level="info",
        )


async def _handle_command(engine: Any, cmd: str) -> None:
    """处理斜杠命令."""
    parts = cmd.strip().split(maxsplit=1)
    command = parts[0]

    match command:
        case "/tools":
            tools = engine.tool_registry.all()
            console.print("[bold]可用工具:[/bold]")
            for t in tools:
                console.print(f"  • [cyan]{t.name}[/cyan] — {t.description}")
        case "/clear":
            engine.reset()
            console.print("[green]会话已清除[/green]")
        case "/usage":
            u = engine.usage
            console.print(
                f"Token: {u.total_input_tokens + u.total_output_tokens} | "
                f"费用: ${u.total_cost_usd:.4f} | "
                f"轮次: {u.turns}"
            )
        case "/model":
            console.print(f"默认模型: {engine.router.resolve_model('capable')}")
            console.print(f"快速模型: {engine.router.resolve_model('fast')}")
            console.print(f"推理模型: {engine.router.resolve_model('reasoning')}")
        case "/help":
            _print_help()
        case _:
            console.print(f"[yellow]未知命令: {command}[/yellow]")
            _print_help()


def _print_banner() -> None:
    from naumi_agent.assets import BANNER_TEXT

    console.print(
        Panel(
            BANNER_TEXT,
            border_style="green",
            padding=(1, 2),
        )
    )
    console.print()


def _print_help() -> None:
    console.print("[bold]可用命令:[/bold]")
    commands = [
        ("/help", "显示帮助"),
        ("/tools", "列出可用工具"),
        ("/model", "显示模型配置"),
        ("/usage", "显示 token 用量"),
        ("/clear", "清除当前会话"),
        ("/quit", "退出"),
    ]
    for cmd, desc in commands:
        console.print(f"  [cyan]{cmd:12s}[/cyan] {desc}")
    console.print()


def _check_api_key(config: AppConfig) -> None:
    if not config.models.api_key:
        console.print("[yellow]警告: 未设置 API Key。请通过环境变量设置:[/yellow]")
        console.print("  [dim]export NAUMI_MODELS__API_KEY=your-key-here[/dim]")
        console.print("  [dim]或在 config.yaml 中配置 api_key[/dim]")
        console.print()


def cli() -> None:
    app()


if __name__ == "__main__":
    cli()
