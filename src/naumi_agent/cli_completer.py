"""Slash command autocomplete with regex matching."""

from __future__ import annotations

import re
from typing import Any as _Any

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

# (command, description, takes_argument)
COMMANDS: list[tuple[str, str, bool]] = [
    ("/help", "显示帮助", False),
    ("/tools", "列出可用工具", False),
    ("/model", "显示模型配置", False),
    ("/usage", "显示 token 用量", False),
    ("/hooks", "显示已注册的钩子", False),
    ("/skills", "列出已加载的 Skill", False),
    ("/history", "查看历史会话列表", False),
    ("/memory", "记忆管理 (stats/search/clean/export)", True),
    ("/load", "加载指定会话并继续对话", True),
    ("/delete", "删除指定会话", True),
    ("/chaos", "灾难演练 — SPOF 分析", True),
    ("/scale", "并发海啸 — 高并发分析", True),
    ("/state", "状态审查 — 云原生合规", False),
    ("/vibe", "极速构建 — 生成 Demo", True),
    ("/eval", "评测驱动 — 生成 pytest 测试", True),
    ("/page", "内存分页 — 上下文压力分析", False),
    ("/heal", "自愈修复 — 分析并修复错误", True),
    ("/dspy", "DSPy 编译优化 — Prompt 工程优化", True),
    ("/graph", "图谱推演 — GraphRAG 拓扑分析", True),
    ("/mcts", "蒙特卡洛树搜索 — 多路径决策", True),
    ("/route", "MoE 混合专家调度 — 多视角分析", True),
    ("/speculate", "推测解码 — 快速起草+深度审查", True),
    ("/jit", "JIT 即时工具 — 用代码保证确定性", True),
    ("/pointer", "语义指针(SPA) — 推理态/物理态分离", True),
    ("/cooe", "认知乱序执行(COOE) — DAG并行调度", True),
    ("/sleep", "昼夜节律突触修剪 — 知识压缩", False),
    ("/entropy", "耗散结构熵减 — 锚点重启", True),
    ("/ooda", "OODA 战场指挥 — 反脆弱架构", True),
    ("/probe", "黑盒探测 — 反幻觉协议", True),
    ("/spar", "对抗自博弈 — 蓝军写代码 vs 红军搞破坏", True),
    ("/world", "世界模型审计 — 状态转移·因果链·反事实推演", True),
    ("/fusion", "决定论-概率论融合 — AI与传统代码边界审计", True),
    ("/consensus", "拜占庭共识 — 多模型表决防幻觉", True),
    ("/pid", "PID 闭环纠偏 — 开环→闭环改造", True),
    ("/zkp", "零知识证明 — 执行轨迹校验", True),
    ("/genesis", "系统自重构 — 元编程与热演化", True),
    ("/macro", "多智能体市场博弈 — 自由市场涌现", True),
    ("/cosmos", "创世引擎审计 — 评估创世潜力", True),
    ("/watchdog", "看门狗 — 不死鸟灾难恢复协议", True),
    ("/supervisor", "守护者树 — Let-it-crash 双子星架构", True),
    ("/autopsy", "执行迹切片 — SWE-bench 级 Bug 解剖", True),
    ("/vision", "AI 视觉数据提取 — 反封锁视觉管线", True),
    ("/hook", "逆向插桩 — 黑盒解剖", True),
    ("/pursue", "目标追踪 — 自主循环执行直至真正达成", True),
    ("/self-review", "自我审查 — 扫描自身源码质量与架构", True),
    ("/reload", "热重载 — 重载模块无需重启", True),
    ("/evolve", "自我进化 — 反思循环修改自身工具代码并验证", True),
    ("/evolve-history", "查看自我进化历史记录", False),
    ("/forge", "工具锻造 — 自主生成新工具并注册", True),
    ("/forge-list", "列出所有已锻造的工具", False),
    ("/forge-remove", "移除已锻造的工具", True),
    ("/new", "保存当前会话并开始新对话", False),
    ("/clear", "清除当前会话（不保存）", False),
    ("/quit", "退出", False),
    ("/exit", "退出", False),
]


class SlashCommandCompleter(Completer):
    """Regex-based slash command completer for prompt_toolkit."""

    def get_completions(self, document: Document, complete_event: _Any) -> list[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return []
        query = text.lstrip("/")
        if " " in query:
            return []
        results = []
        for cmd, desc, takes_arg in COMMANDS:
            if query == "" or re.search(query, cmd.lstrip("/")):
                suffix = " <参数>" if takes_arg else ""
                results.append(
                    Completion(
                        cmd,
                        start_position=-len(text),
                        display=f"{cmd}{suffix}",
                        display_meta=desc,
                    )
                )
        return results


async def prompt_with_completion() -> str:
    """Read user input with slash command autocomplete. Falls back to console.input()."""
    from naumi_agent.main import console

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout as _patch_stdout

        session: PromptSession[str] = PromptSession(
            "你> ",
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
        )
        with _patch_stdout():
            result = await session.prompt_async()
        return result.strip()
    except (RuntimeError, OSError, EOFError):
        return console.input("[bold blue]你>[/bold blue] ").strip()
