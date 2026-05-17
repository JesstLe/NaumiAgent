"""NaumiAgent CLI 入口."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from naumi_agent.cli.commands_analysis import (
    get_analysis_command_map,
    get_analysis_commands_with_args,
    run_analysis,
)
from naumi_agent.cli.commands_meta import (
    delete_session,
    handle_memory,
    load_session,
    new_conversation,
    run_evolve,
    run_forge,
    run_forge_remove,
    run_pursue,
    run_reload,
    run_self_review,
    run_skill,
    show_evolve_history,
    show_forge_list,
    show_history,
    show_hooks,
    show_skills,
)
from naumi_agent.cli.display import (
    cli_event_handler,
    console,
    print_banner,
    print_help,
)
from naumi_agent.config.settings import AppConfig

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer(
    name="naumi",
    help="NaumiAgent — 通用智能 Agent",
    no_args_is_help=True,
)


def _resolve_config_path(path: str) -> str:
    if Path(path).exists():
        return path
    return str(_PROJECT_ROOT / "config.yaml")


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
    tui_app = NaumiApp(engine)
    tui_app.run()


async def _chat(config_path: str) -> None:
    from naumi_agent.log_setup import setup_logging
    from naumi_agent.orchestrator.engine import AgentEngine

    resolved = _resolve_config_path(config_path)
    config = AppConfig.from_yaml(resolved)
    setup_logging(config.log_level)
    _check_api_key(config)
    engine = AgentEngine(config)

    print_banner()

    from naumi_agent.cli.completer import prompt_with_completion

    while True:
        try:
            user_input = prompt_with_completion()
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
            result = await engine.run_streaming(user_input, cli_event_handler)

        if result.status == "error" and result.error:
            console.print(f"[red]错误: {result.error}[/red]")
            continue

        if result.response:
            console.print()
            console.print(
                Panel(
                    Markdown(result.response),
                    title="[bold green]NaumiAgent[/bold green]",
                    border_style="green",
                    padding=(1, 2),
                ),
            )

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
    arg = parts[1] if len(parts) > 1 else ""

    match command:
        # --- 基础命令 ---
        case "/hooks":
            show_hooks(engine)
        case "/skills":
            show_skills(engine)
        case "/tools":
            tools = engine.tool_registry.all()
            console.print("[bold]可用工具:[/bold]")
            for t in tools:
                console.print(f"  • [cyan]{t.name}[/cyan] — {t.description}")
        case "/clear":
            engine.reset()
            console.print("[green]会话已清除[/green]")
        case "/new":
            await new_conversation(engine)
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
        case "/history":
            await show_history(engine)
        case "/memory":
            await handle_memory(engine, arg)
        case "/load":
            if not arg:
                console.print("[yellow]用法: /load <session_id>[/yellow]")
                console.print("[dim]使用 /history 查看会话列表[/dim]")
            else:
                await load_session(engine, arg)
        case "/delete":
            if not arg:
                console.print("[yellow]用法: /delete <session_id>[/yellow]")
                console.print("[dim]使用 /history 查看会话列表[/dim]")
            else:
                await delete_session(engine, arg)

        # --- 分析命令：无参数 ---
        case "/pursue":
            if not arg:
                console.print(
                    "[yellow]用法: /pursue <目标描述>[/yellow]"
                )
                console.print(
                    "[dim]例: /pursue 为 NaumiAgent 添加一个 CSV 导出工具[/dim]"
                )
            else:
                await run_pursue(engine, arg)
        case "/self-review":
            await run_self_review(engine, arg)
        case "/reload":
            await run_reload(engine, arg)
        case "/evolve":
            await run_evolve(engine, arg)
        case "/evolve-history":
            show_evolve_history()
        case "/forge":
            await run_forge(engine, arg)
        case "/forge-list":
            show_forge_list()
        case "/forge-remove":
            run_forge_remove(arg)
        case "/help":
            print_help()

        case _:
            # 分析命令分发
            dispatched = await _dispatch_analysis(command, arg, engine)
            if not dispatched:
                # 尝试匹配已加载的 Skill
                skill_name = command.lstrip("/")
                skill = (
                    engine.skill_loader.get(skill_name)
                    if hasattr(engine, "skill_loader")
                    else None
                )
                if skill:
                    await run_skill(engine, skill_name, arg)
                else:
                    console.print(f"[yellow]未知命令: {command}[/yellow]")
                    print_help()


async def _dispatch_analysis(command: str, arg: str, engine: Any) -> bool:
    """尝试将命令分派为分析命令。成功返回 True。"""
    # 无参数分析命令
    no_arg_map = get_analysis_command_map()
    if command in no_arg_map:
        mode, default_target = no_arg_map[command]
        await run_analysis(engine, mode, arg or default_target)
        return True

    # 需要参数的分析命令
    arg_map = get_analysis_commands_with_args()
    if command in arg_map:
        mode = arg_map[command]
        if not arg:
            console.print(f"[yellow]用法: {command} <参数>[/yellow]")
        else:
            await run_analysis(engine, mode, arg)
        return True

    return False


def _check_api_key(config: AppConfig) -> None:
    """检查必要的环境变量."""
    import os

    if not os.environ.get("OPENAI_API_KEY") and not os.environ.get(
        "ANTHROPIC_API_KEY"
    ):
        console.print(
            "[yellow]⚠️ 未设置 OPENAI_API_KEY 或 ANTHROPIC_API_KEY 环境变量。"
            "部分功能可能不可用。[/yellow]"
        )


def cli() -> None:
    """CLI 入口点."""
    app()
