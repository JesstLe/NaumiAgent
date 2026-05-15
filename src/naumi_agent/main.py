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
    arg = parts[1] if len(parts) > 1 else ""

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
        case "/history":
            await _show_history(engine)
        case "/load":
            if not arg:
                console.print("[yellow]用法: /load <session_id>[/yellow]")
                console.print("[dim]使用 /history 查看会话列表[/dim]")
            else:
                await _load_session(engine, arg)
        case "/delete":
            if not arg:
                console.print("[yellow]用法: /delete <session_id>[/yellow]")
                console.print("[dim]使用 /history 查看会话列表[/dim]")
            else:
                await _delete_session(engine, arg)
        case "/chaos":
            await _run_analysis(engine, "chaos", arg or "当前项目")
        case "/scale":
            await _run_analysis(engine, "scale", arg or "当前项目")
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
        case "/hook":
            if not arg:
                console.print("[yellow]用法: /hook <逆向目标描述>[/yellow]")
            else:
                await _run_analysis(engine, "hook", arg)
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
        ("/history", "查看历史会话列表"),
        ("/load <id>", "加载指定会话并继续对话"),
        ("/delete <id>", "删除指定会话"),
        ("/chaos [目标]", "灾难演练 — SPOF 分析"),
        ("/scale [QPS]", "并发海啸 — 高并发分析"),
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
        ("/hook <目标>", "逆向插桩 — 黑盒解剖"),
        ("/clear", "清除当前会话"),
        ("/quit", "退出"),
    ]
    for cmd, desc in commands:
        console.print(f"  [cyan]{cmd:12s}[/cyan] {desc}")
    console.print()


async def _run_analysis(engine: Any, mode: str, target: str) -> None:
    """执行分析模式命令."""
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
    }

    labels = {
        "chaos": "灾难演练",
        "scale": "并发海啸 (10K QPS)",
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
    }

    tool_name = tool_names[mode]
    label = labels[mode]
    tool = engine.tool_registry.get(tool_name)
    if tool is None:
        console.print(f"[red]工具 {tool_name} 未注册[/red]")
        return

    console.print(f"[bold yellow]⚡ {label} 分析中...[/bold yellow]")
    with console.status("[bold green]分析中...[/bold green]"):
        if mode == "vibe":
            result = await tool.execute(description=target)
        elif mode == "scale":
            result = await tool.execute(target=target, qps=10000)
        elif mode == "eval":
            result = await tool.execute(target=target)
        elif mode == "page":
            result = await tool.execute()
        elif mode == "heal":
            result = await tool.execute(error_log=target)
        elif mode == "dspy":
            result = await tool.execute(prompt_target=target)
        elif mode == "graph":
            result = await tool.execute(target=target)
        elif mode == "mcts":
            result = await tool.execute(problem=target)
        elif mode == "route":
            result = await tool.execute(task=target)
        elif mode == "speculate":
            result = await tool.execute(target=target)
        elif mode == "jit":
            result = await tool.execute(task=target)
        elif mode == "pointer":
            result = await tool.execute(target=target)
        elif mode == "cooe":
            result = await tool.execute(task=target)
        elif mode == "sleep":
            result = await tool.execute(session_context=target)
        elif mode == "entropy":
            result = await tool.execute(context=target)
        elif mode == "ooda":
            result = await tool.execute(target=target)
        elif mode == "probe":
            result = await tool.execute(task=target)
        elif mode == "hook":
            result = await tool.execute(task=target)
        else:
            result = await tool.execute(target=target)

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


async def _show_history(engine: Any) -> None:
    """显示历史会话列表."""
    from rich.table import Table

    sessions, total = await engine.list_sessions(page=1, page_size=20)
    if not sessions:
        console.print("[dim]暂无历史会话[/dim]")
        return

    table = Table(title=f"历史会话 (共 {total} 个)", show_lines=False)
    table.add_column("ID", style="cyan", width=12)
    table.add_column("标题", max_width=40)
    table.add_column("消息数", justify="right", width=6)
    table.add_column("Token", justify="right", width=8)
    table.add_column("更新时间", width=20)

    for s in sessions:
        title = s.title or "新会话"
        if len(title) > 38:
            title = title[:36] + "…"
        table.add_row(
            s.id,
            title,
            str(len(s.messages)),
            str(s.total_tokens),
            s.updated_at.strftime("%Y-%m-%d %H:%M"),
        )

    console.print(table)
    console.print("[dim]使用 /load <id> 加载指定会话[/dim]")
    console.print()


async def _load_session(engine: Any, session_id: str) -> None:
    """加载历史会话."""
    loaded = await engine.load_session(session_id)
    if loaded:
        session = engine._session
        title = session.title if session else session_id
        msg_count = len(session.messages) if session else 0
        console.print(f"[green]已加载会话:[/green] {title}")
        console.print(
            f"[dim]消息数: {msg_count} | Token: {session.total_tokens} | "
            f"费用: ${session.total_cost_usd:.4f}[/dim]"
        )

        # 显示最近几条对话
        user_msgs = [m for m in session.messages if m.get("role") in ("user", "assistant")]
        if user_msgs:
            console.print("[dim]--- 最近对话 ---[/dim]")
            for m in user_msgs[-6:]:
                role = m.get("role", "")
                content = m.get("content", "")
                if len(content) > 100:
                    content = content[:97] + "..."
                label = "[blue]你[/blue]" if role == "user" else "[green]Naumi[/green]"
                console.print(f"  {label}: {content}")
        console.print()
    else:
        console.print(f"[red]会话 {session_id} 不存在[/red]")
        console.print("[dim]使用 /history 查看可用会话[/dim]")


async def _delete_session(engine: Any, session_id: str) -> None:
    """删除指定会话."""
    ok = await engine.delete_session(session_id)
    if ok:
        console.print(f"[green]已删除会话:[/green] {session_id}")
    else:
        console.print(f"[red]会话 {session_id} 不存在[/red]")


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
