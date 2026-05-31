"""CLI 元命令 — evolve/forge/reload/history/memory/session/skill 等非分析命令."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel

from naumi_agent.cli.display import console


async def run_pursue(engine: Any, goal: str) -> None:
    """执行目标追踪循环."""
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
        result = await tool.execute(active_only="--active" in arg.split())
    else:
        result = await tool.execute(run_id=arg.strip())
    console.print(
        Panel(
            Markdown(result),
            title="[bold cyan]目标追踪状态[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


async def run_worktree(engine: Any, arg: str) -> None:
    """执行 worktree 隔离区命令."""
    parts = arg.strip().split()
    subcommand = parts[0] if parts else "status"

    async def _execute(tool_name: str, **kwargs: Any) -> None:
        tool = engine.tool_registry.get(tool_name)
        if not tool:
            console.print(f"[red]工具未注册: {tool_name}[/red]")
            return
        result = await tool.execute(**kwargs)
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


async def run_background(engine: Any, arg: str) -> None:
    """执行后台任务命令."""
    parts = arg.strip().split(maxsplit=2)
    subcommand = parts[0] if parts else "list"

    async def _execute(tool_name: str, **kwargs: Any) -> None:
        tool = engine.tool_registry.get(tool_name)
        if not tool:
            console.print(f"[red]工具未注册: {tool_name}[/red]")
            return
        result = await tool.execute(**kwargs)
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
        case "output":
            if len(parts) < 2:
                console.print("[yellow]用法: /background output <任务ID>[/yellow]")
                return
            await _execute("background_read_output", task_id=parts[1])
        case _:
            console.print(
                "[yellow]未知后台任务子命令[/yellow]\n"
                "[dim]可用: run/status/list/cancel/output[/dim]"
            )


async def run_schedule(engine: Any, arg: str) -> None:
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
        result = await tool.execute(**kwargs)
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
            kind = parts[1]
            expression = parts[2]
            prompt = " ".join(parts[3:])
            await _execute(
                "schedule_create",
                kind=kind,
                expression=expression,
                prompt=prompt,
            )
        case "list":
            active_only = "--active" in parts[1:]
            await _execute("schedule_list", active_only=active_only)
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


async def run_self_review(engine: Any, arg: str) -> None:
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


async def run_reload(engine: Any, arg: str) -> None:
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


async def run_evolve(engine: Any, arg: str) -> None:
    """执行自我进化 — 反思循环: LLM生成方案 → 验证修改 → 质量评估 → 采纳/回滚."""
    import json
    import re

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


def show_evolve_history() -> None:
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
        style = (
            "green" if delta and delta > 0
            else "red" if delta and delta < 0
            else None
        )
        table.add_row(
            step.step_id,
            step.target_file,
            str(step.round_number),
            f"[{style}]{delta_str}[/{style}]" if style else delta_str,
            step.description[:40],
        )

    console.print(table)


async def run_forge(engine: Any, arg: str) -> None:
    """执行工具锻造 — 根据描述生成新工具."""
    import re

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

    if "锻造成功" in result_str:
        from naumi_agent.tools.forge import load_generated_tool

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


def show_forge_list() -> None:
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


def run_forge_remove(arg: str) -> None:
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


async def show_history(engine: Any) -> None:
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


def show_hooks(engine: Any) -> None:
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


async def load_session(engine: Any, session_id: str) -> None:
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

        user_msgs = [
            m for m in session.messages if m.get("role") in ("user", "assistant")
        ]
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


async def delete_session(engine: Any, session_id: str) -> None:
    """删除指定会话."""
    ok = await engine.delete_session(session_id)
    if ok:
        console.print(f"[green]已删除会话:[/green] {session_id}")
    else:
        console.print(f"[red]会话 {session_id} 不存在[/red]")


async def new_conversation(engine: Any) -> None:
    """保存当前会话并开始新对话."""
    if engine._messages and any(m.get("role") == "user" for m in engine._messages):
        try:
            await engine._save_session()
            console.print("[dim]已保存当前会话[/dim]")
        except Exception:
            pass
    engine.reset()
    engine._session = None
    console.print("[green]新对话已开始[/green]")


def show_skills(engine: Any) -> None:
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


async def run_skill(engine: Any, skill_name: str, arguments: str) -> None:
    """通过 CLI 执行一个 Skill."""
    from naumi_agent.cli.display import cli_event_handler

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

    with console.status("[bold green]执行中...[/bold green]"):
        result = await engine.run_streaming(rendered, cli_event_handler)

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


async def handle_memory(engine: Any, arg: str) -> None:
    """处理 /memory 命令及其子命令."""
    subcmd = arg.strip().split(maxsplit=1)[0] if arg.strip() else "stats"
    subarg = arg.strip().split(maxsplit=1)[1] if len(arg.strip().split(maxsplit=1)) > 1 else ""

    handlers = {
        "stats": lambda: _memory_stats(engine),
        "search": lambda: _memory_search(engine, subarg),
        "clean": lambda: _memory_clean(engine),
        "export": lambda: _memory_export(engine),
    }

    handler = handlers.get(subcmd)
    if handler:
        await handler()
    else:
        console.print("[yellow]用法: /memory <子命令>[/yellow]")
        console.print("[dim]子命令: stats, search <查询>, clean, export[/dim]")


async def _memory_stats(engine: Any) -> None:
    """显示记忆统计."""
    from rich.table import Table

    stats = engine.long_term_memory.stats()
    console.print("[bold]记忆统计[/bold]")
    console.print(f"  总数: {stats.total} | 活跃: {stats.active} | 休眠: {stats.dormant}")
    console.print(f"  平均访问次数: {stats.avg_access_count:.1f}")

    if stats.by_category:
        table = Table(title="按类别", show_header=True, header_style="bold cyan")
        table.add_column("类别")
        table.add_column("数量", justify="right")
        for cat, count in sorted(stats.by_category.items()):
            table.add_row(cat, str(count))
        console.print(table)
    console.print()


async def _memory_search(engine: Any, query: str) -> None:
    """搜索记忆."""
    if not query:
        console.print("[yellow]用法: /memory search <查询>[/yellow]")
        return

    results = engine.long_term_memory.search(query, limit=5)
    if not results:
        console.print("[dim]未找到相关记忆[/dim]")
        return

    console.print(f"[bold]搜索结果: '{query}'[/bold]")
    for r in results:
        console.print(f"  [{r.category}] {r.content[:80]}...")
    console.print()


async def _memory_clean(engine: Any) -> None:
    """清理记忆."""
    result = engine.long_term_memory.consolidate()
    console.print("[bold]记忆整理[/bold]")
    console.print(f"  去重合并: {result.get('dedup_merged', 0)}")
    console.print(f"  标记休眠: {result.get('forget_dormant', 0)}")
    console.print(f"  永久删除: {result.get('forget_deleted', 0)}")
    console.print()


async def _memory_export(engine: Any) -> None:
    """导出记忆."""
    import json

    data = engine.long_term_memory.export_memories()
    path = "memory_export.json"
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]已导出 {len(data)} 条记忆到 {path}[/green]")
