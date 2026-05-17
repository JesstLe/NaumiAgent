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


async def _cli_event_handler(event: str, data: dict[str, Any]) -> None:
    """实时显示 Agent 思考、工具调用过程."""
    if event == "thinking_delta":
        console.print(f"[dim bright_black]{data.get('content', '')}[/dim bright_black]", end="")
    elif event == "thinking_start":
        console.print("[dim]💭 思考中...[/dim]")
    elif event == "thinking_end":
        console.print()
    elif event == "tool_start":
        name = data.get("name", "?")
        args = data.get("args", {})
        if isinstance(args, dict):
            parts = list(args.items())[:3]
            summary = " ".join(f"{k}={v}" for k, v in parts)
            console.print(f"[cyan]🔧 {name}[/cyan] [dim]{summary}[/dim]")
        else:
            console.print(f"[cyan]🔧 {name}[/cyan]")
    elif event == "tool_end":
        name = data.get("name", "?")
        status = data.get("status", "?")
        content = data.get("content", "")
        duration = data.get("duration_ms", 0)
        if status == "error":
            console.print(f"[red]  ✗ {name} 失败 ({duration:.0f}ms)[/red]")
        else:
            console.print(f"[green]  ✓ {name}[/green] [dim]({duration:.0f}ms)[/dim]")
        if content:
            _print_tool_output(name, content)
    elif event == "token":
        console.print(data.get("content", ""), end="")
    elif event == "response_start":
        console.print()
    elif event == "response_end":
        console.print()
    elif event == "error":
        console.print(f"[red]错误: {data.get('message', '')}[/red]")


def _print_tool_output(name: str, content: str) -> None:
    """Print tool result with diff highlighting for file edits."""
    lines = content.split("\n")
    is_diff = any(ln.startswith(("---", "+++", "@@", "- ", "+ ")) for ln in lines[:6])
    if is_diff:
        for line in lines:
            if line.startswith("-") and not line.startswith("---"):
                console.print(f"[red]{line}[/red]")
            elif line.startswith("+") and not line.startswith("+++"):
                console.print(f"[green]{line}[/green]")
            elif line.startswith("@@"):
                console.print(f"[blue]{line}[/blue]")
            else:
                console.print(f"[dim]{line}[/dim]")
    elif name in ("file_read",):
        preview = "\n".join(lines[:30])
        if len(lines) > 30:
            preview += f"\n  ... ({len(lines) - 30} more lines)"
        console.print(f"[dim]{preview}[/dim]")
    else:
        preview = "\n".join(lines[:8])
        if len(lines) > 8:
            preview += f"\n  ... ({len(lines) - 8} more lines)"
        console.print(f"[dim]{preview}[/dim]")


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
            from naumi_agent.cli_completer import prompt_with_completion

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
            result = await engine.run_streaming(user_input, _cli_event_handler)

        if result.status == "error" and result.error:
            console.print(f"[red]错误: {result.error}[/red]")
            continue

        # 渲染 Markdown 响应
        if result.response:
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
        case "/hooks":
            _show_hooks(engine)
        case "/skills":
            _show_skills(engine)
        case "/tools":
            tools = engine.tool_registry.all()
            console.print("[bold]可用工具:[/bold]")
            for t in tools:
                console.print(f"  • [cyan]{t.name}[/cyan] — {t.description}")
        case "/clear":
            engine.reset()
            console.print("[green]会话已清除[/green]")
        case "/new":
            await _new_conversation(engine)
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
        case "/memory":
            await _handle_memory(engine, arg)
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
        case "/help":
            _print_help()
        case _:
            # 尝试匹配已加载的 Skill
            skill_name = command.lstrip("/")
            skill = engine.skill_loader.get(skill_name) if hasattr(engine, "skill_loader") else None
            if skill:
                await _run_skill(engine, skill_name, arg)
            else:
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
        ("/hooks", "显示已注册的钩子"),
        ("/skills", "列出已加载的 Skill"),
        ("/history", "查看历史会话列表"),
        ("/memory [子命令]", "记忆管理 (stats/search/clean/export)"),
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
        ("/self-review [模块]", "自我审查 — 扫描自身源码质量与架构"),
        ("/reload [域]", "热重载 — 重载模块无需重启 (tools/memory/skills/all)"),
        ("/evolve <描述>", "自我进化 — 反思循环修改自身工具代码并验证"),
        ("/evolve-history", "查看自我进化历史记录"),
        ("/forge <描述>", "工具锻造 — 自主生成新工具并注册"),
        ("/forge-list", "列出所有已锻造的工具"),
        ("/forge-remove <名称>", "移除已锻造的工具"),
        ("/new", "保存当前会话并开始新对话"),
        ("/clear", "清除当前会话（不保存）"),
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
        elif mode == "vision":
            result = await tool.execute(task=target)
        elif mode == "spar":
            result = await tool.execute(task=target)
        elif mode == "world":
            result = await tool.execute(target=target)
        elif mode == "fusion":
            result = await tool.execute(target=target)
        elif mode == "consensus":
            result = await tool.execute(target=target)
        elif mode == "pid":
            result = await tool.execute(target=target)
        elif mode == "zkp":
            result = await tool.execute(target=target)
        elif mode == "genesis":
            result = await tool.execute(target=target)
        elif mode == "macro":
            result = await tool.execute(task=target)
        elif mode == "cosmos":
            result = await tool.execute(target=target)
        elif mode == "watchdog":
            result = await tool.execute(target=target)
        elif mode == "supervisor":
            result = await tool.execute(target=target)
        elif mode == "autopsy":
            result = await tool.execute(target=target)
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


async def _run_pursue(engine: Any, goal: str) -> None:
    """执行目标追踪循环."""
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn

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
            result = await tool.execute(goal=goal)
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
        result = await tool.execute(focus=focus, module=module)

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
            '4. 输出 JSON: {"target_file": "路径", '
            '"new_content": "内容", "description": "说明"}\n'
        )

        modifiable_files = []
        for domain_dir in ["tools", "memory", "skills"]:
            domain_path = source_dir / domain_dir
            if domain_path.is_dir():
                for py_file in domain_path.glob("*.py"):
                    if (
                        py_file.name != "__init__.py"
                        and not _is_protected_file(py_file)
                    ):
                        modifiable_files.append(
                            str(py_file.relative_to(source_dir))
                        )

        file_list = "\n".join(f"- {f}" for f in modifiable_files)
        prompt += f"\n可修改的文件列表:\n{file_list}"

        file_contexts = []
        for f in modifiable_files[:10]:
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

    json_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?```", llm_output, re.DOTALL
    )
    json_str = json_match.group(1) if json_match else llm_output

    try:
        proposal = json.loads(json_str)
    except json.JSONDecodeError:
        console.print("[red]无法解析 LLM 输出的修改方案[/red]")
        console.print(Panel(llm_output[:2000], title="[red]LLM 输出[/red]"))
        return

    target_file = proposal.get("target_file", "")
    new_content = proposal.get("new_content", "")
    change_desc = proposal.get("description", description)

    if not target_file or not new_content:
        console.print("[red]修改方案缺少 target_file 或 new_content[/red]")
        return

    console.print(f"[dim]目标文件: {target_file}[/dim]")
    console.print(f"[dim]修改说明: {change_desc}[/dim]")

    # Read original content for evolution evaluation
    from naumi_agent.tools.self_modify import _resolve_target_path

    try:
        original_path = _resolve_target_path(target_file)
        original_content = original_path.read_text(encoding="utf-8")
    except Exception as e:
        console.print(f"[red]无法读取原始文件: {e}[/red]")
        return

    # Phase 2: Validate and apply modification
    console.print("[dim]Phase 2: 验证并应用修改...[/dim]")

    with console.status("[bold green]验证中...[/bold green]"):
        modify_result_str = await self_modify.execute(
            target_file=target_file,
            new_content=new_content,
            description=change_desc,
        )

    # Check if modification was applied
    if "已应用" not in modify_result_str:
        console.print()
        console.print(
            Panel(
                Markdown(modify_result_str),
                title="[bold red]❌ 修改未通过验证[/bold red]",
                border_style="red",
                padding=(1, 2),
            ),
        )
        return

    # Phase 3: Reflective evaluation
    console.print("[dim]Phase 3: 反思评估 — 对比修改前后质量...[/dim]")

    if self_evolve:
        with console.status("[bold green]评估质量变化...[/bold green]"):
            from naumi_agent.tools.self_evolve import (
                format_evolution_report,
                run_evolution_cycle,
            )

            cycle_result = run_evolution_cycle(
                target_file=target_file,
                original_content=original_content,
                new_content=new_content,
                description=change_desc,
            )

        eval_report = format_evolution_report(
            cycle_result["eval_result"]
        )

        action = cycle_result["action"]
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
        if action == "rollback":
            console.print("[bold red]🔄 质量下降，正在回滚...[/bold red]")
            from naumi_agent.tools.self_modify import _rollback_file

            rolled_back = _rollback_file(original_path)
            if rolled_back:
                console.print("[green]✅ 已回滚到修改前的状态[/green]")
            else:
                console.print(
                    "[yellow]⚠️ 自动回滚失败，请手动 git checkout[/yellow]"
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
    console.print("[bold yellow]🔄 正在热重载修改后的模块...[/bold yellow]")
    try:
        reload_result = await engine.reload_tools("tools")
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

    # Phase 1: Generate tool code via LLM
    console.print("[dim]Phase 1: LLM 生成工具代码...[/dim]")

    from naumi_agent.tools.forge import _TOOL_GENERATION_SYSTEM

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
        console.print(f"[red]LLM 调用失败: {e}[/red]")
        return

    # Phase 2: Validate and save
    console.print("[dim]Phase 2: 验证并保存工具...[/dim]")

    with console.status("[bold green]锻造中...[/bold green]"):
        result_str = await forge_tool_instance.execute(
            description=description,
            llm_output=llm_output,
        )

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

async def _show_history(engine: Any) -> None:
    """显示历史会话列表."""


def _show_hooks(engine: Any) -> None:
    """显示已注册的钩子."""
    from naumi_agent.hooks import HookPoint

    hooks = engine.hooks.list_hooks()
    if not hooks:
        console.print("[dim]没有已注册的钩子[/dim]")
        return

    console.print("[bold]已注册钩子:[/bold]")
    for point, callbacks in hooks.items():
        try:
            label = HookPoint(point).value
        except ValueError:
            label = point
        console.print(f"  [cyan]{label}[/cyan]")
        for cb in callbacks:
            console.print(f"    • {cb}")
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

    # Phase 1: Generate tool code via LLM
    console.print("[dim]Phase 1: LLM 生成工具代码...[/dim]")

    from naumi_agent.tools.forge import _TOOL_GENERATION_SYSTEM

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
        console.print(f"[red]LLM 调用失败: {e}[/red]")
        return

    # Phase 2: Validate and save
    console.print("[dim]Phase 2: 验证并保存工具...[/dim]")

    with console.status("[bold green]锻造中...[/bold green]"):
        result_str = await forge_tool_instance.execute(
            description=description,
            llm_output=llm_output,
        )

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
    # Reset session so next interaction creates a fresh one
    engine._session = None
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
        result = await engine.run_streaming(rendered, _cli_event_handler)

    if result.status == "error" and result.error:
        console.print(f"[red]错误: {result.error}[/red]")
        return

    if result.response:
        console.print()
        console.print(
            Panel(
                Markdown(result.response),
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
