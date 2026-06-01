# ruff: noqa: E501
"""斜杠命令自动补全 — prompt_toolkit 集成.

支持:
- 正则匹配
- 模糊搜索 (fuzzy)
- 分类分组显示
- 参数提示
- 只读/需参数标记
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

# ---------------------------------------------------------------------------
#  Command metadata — structured version of the old flat tuples
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandMeta:
    """Metadata for a single slash command."""

    name: str
    description: str
    takes_arg: bool = False
    category: str = "基础"  # 基础 | 分析(无参) | 分析(需参) | 元命令 | 会话
    readonly: bool = True  # whether the command is read-only (safe in plan mode)
    arg_hint: str = ""  # e.g. "<路径>", "<描述>", "<id>"


def _build_commands() -> list[CommandMeta]:
    """Build the full command registry."""
    return [  # noqa: E501
        # 基础
        CommandMeta("/help", "显示帮助", category="基础"),
        CommandMeta("/copy", "复制/导出完整记录、最近一轮或最近错误", takes_arg=True, arg_hint="<all|last|error>", readonly=True, category="基础"),
        CommandMeta("/debug", "显示本次结构化调试日志位置", category="基础"),
        CommandMeta("/debug-replay", "回放 debug-runs 结构化事件", takes_arg=True, arg_hint="<路径>", readonly=True, category="基础"),
        CommandMeta("/diff", "查看本轮结构化 git diff", takes_arg=True, arg_hint="[all|worktree|staged]", readonly=True, category="基础"),
        CommandMeta("/pwd", "显示当前工作目录", category="基础"),
        CommandMeta("/quit", "退出", readonly=False, category="基础"),
        CommandMeta("/exit", "退出", readonly=False, category="基础"),
        CommandMeta("/tools", "列出可用工具", readonly=True, category="基础"),
        CommandMeta("/model", "显示模型配置", readonly=True, category="基础"),
        CommandMeta("/usage", "显示 token 用量", readonly=True, category="基础"),
        CommandMeta("/hooks", "显示已注册的钩子", readonly=True, category="基础"),
        CommandMeta("/skills", "列出已加载的 Skill", readonly=True, category="基础"),
        CommandMeta("/resume", "继续最近的对话", readonly=False, category="会话"),
        CommandMeta("/history", "查看历史会话列表", readonly=True, category="会话"),
        CommandMeta("/memory", "记忆管理 (stats/search/clean/export)", readonly=False, category="会话"),
        CommandMeta("/load", "加载指定会话并继续对话", takes_arg=True, arg_hint="<id>", category="会话"),
        CommandMeta("/delete", "删除指定会话", takes_arg=True, arg_hint="<id>", readonly=False, category="会话"),
        CommandMeta("/clear", "清除当前会话（不保存）", readonly=False, category="会话"),
        CommandMeta("/new", "保存当前会话并开始新对话", readonly=False, category="会话"),
        # 分析 — 无参数
        CommandMeta("/chaos", "灾难演练 — SPOF 分析", readonly=True, category="分析"),
        CommandMeta("/scale", "并发海啸 — 高并发分析", readonly=True, category="分析"),
        CommandMeta("/state", "状态审查 — 云原生合规", readonly=True, category="分析"),
        CommandMeta("/page", "内存分页 — 上下文压力分析", readonly=True, category="分析"),
        CommandMeta("/sleep", "昼夜节律突触修剪 — 知识压缩", readonly=True, category="分析"),
        CommandMeta("/dspy", "DSPy 编译优化 — Prompt 工程优化", readonly=True, category="分析"),
        CommandMeta("/graph", "图谱推演 — GraphRAG 拓扑分析", readonly=True, category="分析"),
        CommandMeta("/self-review", "自我审查 — 扫描自身源码质量与架构", readonly=True, category="分析"),
        CommandMeta("/evolve-history", "查看自我进化历史记录", readonly=True, category="分析"),
        CommandMeta("/forge-list", "列出所有已锻造的工具", readonly=True, category="分析"),
        # 分析 — 需参数
        CommandMeta("/vibe", "极速构建 — 生成 Demo", takes_arg=True, arg_hint="<描述>", readonly=False, category="分析"),
        CommandMeta("/eval", "评测驱动 — 生成 pytest 测试", takes_arg=True, arg_hint="<路径>", readonly=True, category="分析"),
        CommandMeta("/heal", "自愈修复 — 分析并修复错误", takes_arg=True, arg_hint="<错误>", readonly=False, category="分析"),
        CommandMeta("/mcts", "蒙特卡洛树搜索 — 多路径决策", takes_arg=True, arg_hint="<问题>", readonly=True, category="分析"),
        CommandMeta("/route", "MoE 混合专家调度 — 多视角分析", takes_arg=True, arg_hint="<任务>", readonly=True, category="分析"),
        CommandMeta("/speculate", "推测解码 — 快速起草+深度审查", takes_arg=True, arg_hint="<路径>", readonly=True, category="分析"),
        CommandMeta("/jit", "JIT 即时工具 — 用代码保证确定性", takes_arg=True, arg_hint="<任务>", readonly=False, category="分析"),
        CommandMeta("/pointer", "语义指针(SPA) — 推理态/物理态分离", takes_arg=True, arg_hint="<路径>", readonly=True, category="分析"),
        CommandMeta("/cooe", "认知乱序执行(COOE) — DAG并行调度", takes_arg=True, arg_hint="<任务>", readonly=True, category="分析"),
        CommandMeta("/entropy", "耗散结构熵减 — 锚点重启", takes_arg=True, arg_hint="<文本>", readonly=True, category="分析"),
        CommandMeta("/ooda", "OODA 战场指挥 — 反脆弱架构", takes_arg=True, arg_hint="<路径>", readonly=True, category="分析"),
        CommandMeta("/probe", "黑盒探测 — 反幻觉协议", takes_arg=True, arg_hint="<需求>", readonly=True, category="分析"),
        CommandMeta("/hook", "逆向插桩 — 黑盒解剖", takes_arg=True, arg_hint="<目标>", readonly=True, category="分析"),
        CommandMeta("/vision", "AI 视觉数据提取 — 反封锁视觉管线", takes_arg=True, arg_hint="<目标>", readonly=True, category="分析"),
        CommandMeta("/spar", "对抗自博弈 — 蓝军写代码 vs 红军搞破坏", takes_arg=True, arg_hint="<目标>", readonly=False, category="分析"),
        CommandMeta("/world", "世界模型审计 — 状态转移·因果链·反事实推演", takes_arg=True, arg_hint="<目标>", readonly=True, category="分析"),
        CommandMeta("/fusion", "决定论-概率论融合 — AI与传统代码边界审计", takes_arg=True, arg_hint="<目标>", readonly=True, category="分析"),
        CommandMeta("/consensus", "拜占庭共识 — 多模型表决防幻觉", takes_arg=True, arg_hint="<目标>", readonly=True, category="分析"),
        CommandMeta("/pid", "PID 闭环纠偏 — 开环→闭环改造", takes_arg=True, arg_hint="<目标>", readonly=True, category="分析"),
        CommandMeta("/zkp", "零知识证明 — 执行轨迹校验", takes_arg=True, arg_hint="<目标>", readonly=True, category="分析"),
        CommandMeta("/genesis", "系统自重构 — 元编程与热演化", takes_arg=True, arg_hint="<目标>", readonly=False, category="分析"),
        CommandMeta("/macro", "多智能体市场博弈 — 自由市场涌现", takes_arg=True, arg_hint="<目标>", readonly=True, category="分析"),
        CommandMeta("/cosmos", "创世引擎审计 — 评估创世潜力", takes_arg=True, arg_hint="<目标>", readonly=True, category="分析"),
        CommandMeta("/watchdog", "看门狗 — 不死鸟灾难恢复协议", takes_arg=True, arg_hint="<目标>", readonly=False, category="分析"),
        CommandMeta("/supervisor", "守护者树 — Let-it-crash 双子星架构", takes_arg=True, arg_hint="<目标>", readonly=False, category="分析"),
        CommandMeta("/autopsy", "执行迹切片 — SWE-bench 级 Bug 解剖", takes_arg=True, arg_hint="<目标>", readonly=True, category="分析"),
        # 元命令
        CommandMeta("/pursue", "目标追踪 — 自主循环执行直至真正达成", takes_arg=True, arg_hint="<目标>", readonly=False, category="元命令"),
        CommandMeta("/worktree", "隔离执行区 — create/status/bind/keep/remove", takes_arg=True, arg_hint="<子命令>", readonly=False, category="元命令"),
        CommandMeta("/background", "后台任务 — run/status/list/cancel/output/cleanup", takes_arg=True, arg_hint="<子命令>", readonly=False, category="元命令"),
        CommandMeta("/schedule", "调度提醒 — create/list/cancel/pause/resume", takes_arg=True, arg_hint="<子命令>", readonly=False, category="元命令"),
        CommandMeta("/todo", "todo 清单 — list/add/start/done/pending/delete/clear", takes_arg=True, arg_hint="<子命令>", readonly=False, category="元命令"),
        CommandMeta("/bdaemon", "外部浏览器 daemon — start/health/run/list/status/watch", takes_arg=True, arg_hint="<子命令>", readonly=False, category="元命令"),
        CommandMeta("/reload", "热重载 — 重载模块无需重启", readonly=False, category="元命令"),
        CommandMeta("/evolve", "自我进化 — 反思循环修改自身工具代码并验证", takes_arg=True, arg_hint="<描述>", readonly=False, category="元命令"),
        CommandMeta("/forge", "工具锻造 — 自主生成新工具并注册", takes_arg=True, arg_hint="<描述>", readonly=False, category="元命令"),
        CommandMeta("/forge-remove", "移除已锻造的工具", takes_arg=True, arg_hint="<名称>", readonly=False, category="元命令"),
    ]


COMMANDS_META: list[CommandMeta] = _build_commands()

# Backward-compatible flat tuple list for code that still uses it
COMMANDS: list[tuple[str, str, bool]] = [
    (c.name, c.description, c.takes_arg) for c in COMMANDS_META
]


def _fuzzy_match(query: str, text: str) -> bool:
    """Check if query characters appear in order in text (fuzzy matching)."""
    qi = 0
    for ch in text.lower():
        if qi < len(query) and ch == query[qi].lower():
            qi += 1
    return qi == len(query)


class SlashCommandCompleter(Completer):
    """斜杠命令补全器 — 支持正则匹配和模糊搜索.

    输入 / 显示全部；输入 /cha 匹配 /chaos；输入 hs 可匹配 /history。
    """

    def get_completions(
        self, document: Document, complete_event: Any,
    ) -> Any:
        text = document.text_before_cursor

        # Only trigger when input starts with /
        if not text.startswith("/"):
            return

        query = text.lstrip("/")

        # If query contains a space, user is typing arguments — no more completion
        if " " in query:
            return

        # Sort by category for consistent display
        sorted_cmds = sorted(COMMANDS_META, key=lambda c: (c.category, c.name))

        for cmd in sorted_cmds:
            cmd_name = cmd.name.lstrip("/")
            matched = False

            if query == "":
                matched = True
            else:
                try:
                    matched = re.search(query, cmd_name) is not None
                except re.error:
                    matched = query in cmd_name
                if not matched and _fuzzy_match(query, cmd_name):
                    matched = True

            if not matched:
                continue

            # Build display text with arg hint
            suffix = f" {cmd.arg_hint}" if cmd.takes_arg else ""

            # Category prefix for grouping in display
            display = f"{cmd.name}{suffix}"
            meta_parts: list[str] = []
            if cmd.category not in ("基础",):
                meta_parts.append(f"[{cmd.category}]")
            meta_parts.append(cmd.description)
            if not cmd.readonly:
                meta_parts.append("⚡写操作")
            meta_text = " ".join(meta_parts)

            yield Completion(
                cmd.name,
                start_position=-len(text),
                display=display,
                display_meta=meta_text,
            )


def prompt_with_completion() -> str:
    """带自动补全的命令行输入. 回退到普通 input()."""
    try:
        from prompt_toolkit import prompt as pt_prompt
        from prompt_toolkit.formatted_text import FormattedText

        completer = SlashCommandCompleter()
        message = FormattedText([("bold blue", "你> ")])
        result = pt_prompt(
            message,
            completer=completer,
            complete_while_typing=True,
            enable_history_search=False,
        )
        return result.strip()
    except (ImportError, Exception):
        from naumi_agent.cli.display import console
        return console.input("[bold blue]你>[/bold blue] ").strip()
