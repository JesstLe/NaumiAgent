"""CLI 分析命令路由 — 所有 /chaos /scale /eval 等分析模式的命令分发."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from naumi_agent.cli.display import console
from naumi_agent.tools.base import ToolCall

ANALYSIS_TOOL_NAMES = {
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


def _build_analysis_kwargs(mode: str, target: str) -> dict[str, Any]:
    """Build execute kwargs matching the current analysis tool contracts."""
    if mode == "scale":
        normalized = target.strip()
        if normalized.isdigit():
            return {"target": "当前项目", "qps": int(normalized)}
        return {"target": target}
    if mode == "vibe":
        return {"description": target}
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


async def run_analysis(engine: Any, mode: str, target: str) -> None:
    """执行分析模式命令."""
    tool_name = ANALYSIS_TOOL_NAMES.get(mode)
    if not tool_name:
        console.print(f"[red]未知分析模式: {mode}[/red]")
        return

    tool = engine.tool_registry.get(tool_name)
    if not tool:
        console.print(f"[red]工具未注册: {tool_name}[/red]")
        return

    console.print(f"[bold magenta]🔬 {mode} 分析中...[/bold magenta]")
    with console.status("[bold green]分析中...[/bold green]"):
        kwargs = _build_analysis_kwargs(mode, target)
        execute_tool = getattr(engine, "_execute_tool", None)
        if callable(execute_tool):
            tool_call = ToolCall(
                id=f"slash-analysis-{mode}-{uuid4()}",
                name=tool_name,
                arguments=json.dumps(kwargs, ensure_ascii=False),
            )
            tool_result = await execute_tool(tool_call, agent_name="cli")
            if tool_result.status != "success":
                console.print(f"[yellow]{tool_result.content}[/yellow]")
                return
            result = tool_result.content
        else:
            result = await tool.execute(**kwargs)

    from rich.markdown import Markdown
    from rich.panel import Panel

    mode_labels = {
        "chaos": "🔥 灾难演练",
        "scale": "🌊 并发海啸",
        "state": "☁️ 状态审查",
        "vibe": "⚡ 极速构建",
        "eval": "📊 评测驱动",
        "page": "📄 内存分页",
        "heal": "🩹 自愈修复",
        "graph": "🕸️ 图谱推演",
        "mcts": "🎲 蒙特卡洛树搜索",
        "route": "🧭 MoE 混合专家",
        "speculate": "🔮 推测解码",
        "jit": "⚙️ JIT 即时工具",
        "pointer": "🧠 语义指针",
        "cooe": "🔀 认知乱序执行",
        "sleep": "💤 昼夜节律修剪",
        "entropy": "🌡️ 耗散结构熵减",
        "ooda": "🎯 OODA 战场指挥",
        "probe": "📡 黑盒探测",
        "hook": "🪝 逆向插桩",
        "vision": "👁️ AI 视觉提取",
        "spar": "⚔️ 对抗自博弈",
        "world": "🌍 世界模型审计",
        "fusion": "🔮 决定论-概率论融合",
        "consensus": "🤝 拜占庭共识",
        "pid": "🎛️ PID 闭环纠偏",
        "zkp": "🔐 零知识证明",
        "genesis": "🧬 系统自重构",
        "macro": "📈 多智能体市场博弈",
        "cosmos": "🌌 创世引擎审计",
        "watchdog": "🐕 看门狗",
        "supervisor": "🌲 守护者树",
        "autopsy": "🔪 执行迹切片",
        "dspy": "🧪 DSPy 编译优化",
    }

    label = mode_labels.get(mode, f"🔬 {mode}")
    console.print()
    console.print(
        Panel(
            Markdown(result),
            title=f"[bold magenta]{label}[/bold magenta]",
            border_style="magenta",
            padding=(1, 2),
        ),
    )
    console.print()


def get_analysis_command_map() -> dict[str, tuple[str, str]]:
    """Return mapping of command -> (mode, usage_hint).

    Used by the command router to dispatch analysis commands.
    """
    return {
        "/chaos": ("chaos", "当前项目"),
        "/scale": ("scale", "当前项目"),
        "/state": ("state", "当前项目"),
        "/page": ("page", "memory"),
        "/sleep": ("sleep", ""),
        "/dspy": ("dspy", ""),
        "/graph": ("graph", ""),
    }


def get_analysis_commands_with_args() -> dict[str, str]:
    """Return commands that require arguments."""
    return {
        "/vibe": "vibe",
        "/eval": "eval",
        "/heal": "heal",
        "/mcts": "mcts",
        "/route": "route",
        "/speculate": "speculate",
        "/jit": "jit",
        "/pointer": "pointer",
        "/cooe": "cooe",
        "/entropy": "entropy",
        "/ooda": "ooda",
        "/probe": "probe",
        "/vision": "vision",
        "/spar": "spar",
        "/world": "world",
        "/fusion": "fusion",
        "/consensus": "consensus",
        "/pid": "pid",
        "/zkp": "zkp",
        "/genesis": "genesis",
        "/macro": "macro",
        "/cosmos": "cosmos",
        "/watchdog": "watchdog",
        "/supervisor": "supervisor",
        "/autopsy": "autopsy",
        "/hook": "hook",
    }
