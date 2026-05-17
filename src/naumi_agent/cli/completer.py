"""斜杠命令自动补全 — prompt_toolkit 集成."""

from __future__ import annotations

import re
from typing import Any

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

# ---------------------------------------------------------------------------
#  命令注册表 — (command, description, takes_arg)
# ---------------------------------------------------------------------------

COMMANDS: list[tuple[str, str, bool]] = [
    # 基础
    ("/help", "显示帮助", False),
    ("/quit", "退出", False),
    ("/exit", "退出", False),
    ("/tools", "列出可用工具", False),
    ("/model", "显示模型配置", False),
    ("/usage", "显示 token 用量", False),
    ("/hooks", "显示已注册的钩子", False),
    ("/skills", "列出已加载的 Skill", False),
    ("/history", "查看历史会话列表", False),
    ("/memory", "记忆管理 (stats/search/clean/export)", False),
    ("/load", "加载指定会话并继续对话", True),
    ("/delete", "删除指定会话", True),
    ("/clear", "清除当前会话（不保存）", False),
    ("/new", "保存当前会话并开始新对话", False),
    # 分析 — 无参数
    ("/chaos", "灾难演练 — SPOF 分析", False),
    ("/scale", "并发海啸 — 高并发分析", False),
    ("/state", "状态审查 — 云原生合规", False),
    ("/page", "内存分页 — 上下文压力分析", False),
    ("/sleep", "昼夜节律突触修剪 — 知识压缩", False),
    ("/dspy", "DSPy 编译优化 — Prompt 工程优化", False),
    ("/graph", "图谱推演 — GraphRAG 拓扑分析", False),
    ("/self-review", "自我审查 — 扫描自身源码质量与架构", False),
    ("/evolve-history", "查看自我进化历史记录", False),
    ("/forge-list", "列出所有已锻造的工具", False),
    # 分析 — 需参数
    ("/vibe", "极速构建 — 生成 Demo", True),
    ("/eval", "评测驱动 — 生成 pytest 测试", True),
    ("/heal", "自愈修复 — 分析并修复错误", True),
    ("/mcts", "蒙特卡洛树搜索 — 多路径决策", True),
    ("/route", "MoE 混合专家调度 — 多视角分析", True),
    ("/speculate", "推测解码 — 快速起草+深度审查", True),
    ("/jit", "JIT 即时工具 — 用代码保证确定性", True),
    ("/pointer", "语义指针(SPA) — 推理态/物理态分离", True),
    ("/cooe", "认知乱序执行(COOE) — DAG并行调度", True),
    ("/entropy", "耗散结构熵减 — 锚点重启", True),
    ("/ooda", "OODA 战场指挥 — 反脆弱架构", True),
    ("/probe", "黑盒探测 — 反幻觉协议", True),
    ("/hook", "逆向插桩 — 黑盒解剖", True),
    ("/vision", "AI 视觉数据提取 — 反封锁视觉管线", True),
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
    # 元命令
    ("/pursue", "目标追踪 — 自主循环执行直至真正达成", True),
    ("/reload", "热重载 — 重载模块无需重启", False),
    ("/evolve", "自我进化 — 反思循环修改自身工具代码并验证", True),
    ("/forge", "工具锻造 — 自主生成新工具并注册", True),
    ("/forge-remove", "移除已锻造的工具", True),
]


class SlashCommandCompleter(Completer):
    """斜杠命令补全器 — 支持正则匹配. 输入 / 为空则显示全部."""

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

        for cmd, desc, takes_arg in COMMANDS:
            # Empty query = show all; otherwise regex match
            if query == "" or re.search(query, cmd.lstrip("/")):
                suffix = " <参数>" if takes_arg else ""
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=f"{cmd}{suffix}",
                    display_meta=desc,
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
