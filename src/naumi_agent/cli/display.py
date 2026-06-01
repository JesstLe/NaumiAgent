"""CLI 显示层 — banner、帮助、工具输出、事件处理."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel

console = Console()


def print_banner() -> None:
    from naumi_agent.assets import BANNER_TEXT

    console.print(
        Panel(
            BANNER_TEXT,
            border_style="green",
            padding=(1, 2),
        )
    )
    console.print()


def print_help() -> None:
    console.print("[bold]可用命令:[/bold]")
    commands = [
        ("/help", "显示帮助"),
        ("/copy", "复制/导出当前完整记录 (Ctrl+Y)"),
        ("/pwd", "显示当前工作目录"),
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
        ("/hook <目标>", "逆向插桩 — 黑盒解剖"),
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
        ("/pursue <目标>", "目标追踪 — 自主循环执行直至真正达成"),
        ("/worktree <子命令>", "隔离执行区 — create/status/bind/keep/remove"),
        ("/background <子命令>", "后台任务 — run/status/list/cancel/output"),
        ("/schedule <子命令>", "调度提醒 — create/list/cancel/pause/resume"),
        ("/todo <子命令>", "todo 清单 — list/add/start/done/pending/delete/clear"),
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


def print_tool_output(name: str, content: str) -> None:
    """Print tool result with diff highlighting for file edits."""
    from rich.syntax import Syntax

    if "```diff" in content or "--- a/" in content:
        try:
            syntax = Syntax(content, "diff", theme="monokai", line_numbers=False)
            console.print(syntax)
        except Exception:
            console.print(content)
    elif len(content) > 500:
        console.print(content[:500] + "...")
    else:
        console.print(content)


async def cli_event_handler(event: str, data: dict[str, Any]) -> None:
    """实时显示 Agent 思考、工具调用过程."""
    if event == "thinking_delta":
        console.print(
            f"[dim bright_black]{data.get('content', '')}[/dim bright_black]",
            end="",
        )
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
        duration = data.get("duration_ms", 0)
        if status == "error":
            console.print(
                f"[red]  ✗ {name} 失败 ({duration:.0f}ms)[/red]"
            )
        else:
            console.print(
                f"[green]  ✓ {name}[/green] [dim]({duration:.0f}ms)[/dim]"
            )
        content = data.get("content", "")
        if content:
            print_tool_output(name, content)
    elif event == "hook_trace":
        point = str(data.get("point", "?"))
        callback = str(data.get("callback", "?"))
        duration = int(data.get("duration_ms", 0) or 0)
        error = str(data.get("error", "") or "")
        aborted = bool(data.get("aborted", False))
        style = "yellow" if aborted else "red" if error else "magenta"
        status = "拦截" if aborted else "异常" if error else "触发"
        suffix = f" · {error}" if error else ""
        console.print(
            f"[{style}]hook {status}: {point} → {callback} ({duration}ms){suffix}[/{style}]"
        )
    elif event == "task_snapshot":
        source = str(data.get("source", "todo"))
        summary = str(data.get("summary", "当前没有任务。"))
        console.print(f"[cyan]todo 更新: {source}[/cyan]")
        console.print(summary)
    elif event == "subagent_event":
        status = str(data.get("status", "?"))
        agent = str(data.get("agent_name", "") or "未匹配")
        task_id = str(data.get("task_id", "?"))
        message = str(data.get("message", "") or "")
        style = (
            "green"
            if status == "completed"
            else "red"
            if status in {"error", "failed"}
            else "cyan"
        )
        suffix = f" · {message}" if message else ""
        console.print(
            f"[{style}]subagent {status}: {agent} / {task_id}{suffix}[/{style}]"
        )
    elif event == "permission_bubble":
        agent = str(data.get("agent_name", "?"))
        tool = str(data.get("tool_name", "?"))
        status = str(data.get("status", "?"))
        reason = str(data.get("reason", "") or "")
        style = "red" if status in {"blocked", "blocked_by_hook"} else "yellow"
        suffix = f" · {reason[:120]}" if reason else ""
        console.print(
            f"[{style}]permission bubble: {agent} → {tool} "
            f"[{status}]{suffix}[/{style}]"
        )
    elif event == "team_event":
        event_type = str(data.get("event_type", "?"))
        sender = str(data.get("sender", "?"))
        recipient = str(data.get("recipient", "") or "广播")
        priority = str(data.get("priority", "normal"))
        message = str(data.get("message", "") or "")
        style = "red" if priority == "critical" else "yellow" if priority == "high" else "cyan"
        suffix = f" · {message[:120]}" if message else ""
        console.print(
            f"[{style}]team {event_type}: "
            f"{sender} → {recipient} [{priority}]{suffix}[/{style}]"
        )
    elif event == "runtime_notification":
        title = str(data.get("title", "") or "运行时通知")
        source = str(data.get("source", "runtime"))
        count = int(data.get("count", 0) or 0)
        preview = str(data.get("preview", "") or "").replace("\n", " ")
        suffix = f" · {preview[:160]}" if preview else ""
        console.print(f"[cyan]{title}: {source} ×{count}{suffix}[/cyan]")
    elif event == "context_compacted":
        before = data.get("before", "?")
        after = data.get("after", "?")
        archived = int(data.get("archived_tool_results", 0) or 0)
        preserved = data.get("preserved_sections", [])
        warnings = data.get("warnings", [])
        console.print(f"[magenta]context compacted: {before} → {after} messages[/magenta]")
        if archived:
            console.print(f"[magenta]归档：[/magenta]{archived} 个大型工具结果")
        if isinstance(preserved, list) and preserved:
            console.print("[magenta]保留：[/magenta]" + "、".join(str(item) for item in preserved))
        if isinstance(warnings, list) and warnings:
            console.print("[yellow]风险：[/yellow]" + "；".join(str(item) for item in warnings))
    elif event == "recovery_event":
        reason = str(data.get("reason", "?"))
        action = str(data.get("action", "?"))
        phase = str(data.get("phase", "?"))
        before = data.get("before", "?")
        after = data.get("after", "?")
        unit = str(data.get("unit", "messages"))
        style = "green" if phase == "completed" else "red" if phase == "failed" else "yellow"
        suffix = f" {before} → {after} {unit}" if after != "?" else f" before={before}"
        console.print(f"[{style}]recovery {phase}: {action} ({reason}){suffix}[/{style}]")
    elif event == "token":
        console.print(data.get("content", ""), end="")
    elif event == "response_start":
        console.print()
    elif event == "response_end":
        console.print()
    elif event == "error":
        console.print(f"[red]错误: {data.get('message', '')}[/red]")
