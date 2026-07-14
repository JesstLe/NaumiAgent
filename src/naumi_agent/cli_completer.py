"""Slash command autocomplete with regex matching."""

from __future__ import annotations

import re
from typing import Any as _Any

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

# (command, description, takes_argument)
try:
    from naumi_agent.cli.completer import COMMANDS as _COMPLETER_COMMANDS
except Exception:  # pragma: no cover - backward-compatible fallback path
    _COMPLETER_COMMANDS = [
        ("/help", "显示帮助", False),
        ("/keybindings", "显示当前快捷键配置", False),
        ("/style", "显示当前主题和输出风格", False),
        ("/reasoning", "显示或隐藏模型思考文本", True),
        ("/effort", "查看或切换模型思考强度", True),
        ("/doctor", "运行环境诊断", False),
        ("/harness", "Harness Profile 状态、知识、检查与信任", True),
        ("/copy", "复制/导出完整记录、最近一轮或最近错误", True),
        ("/debug", "显示本次结构化调试日志位置", False),
        ("/debug-replay", "回放 debug-runs 结构化事件", True),
        ("/diff", "查看本轮结构化 git diff", True),
        ("/permissions", "显示待确认权限面板", False),
        ("/pwd", "显示当前工作目录", False),
        ("/tools", "列出可用工具", False),
        ("/model", "显示模型配置", False),
        ("/version", "显示版本号", False),
        ("/usage", "显示 token 用量", False),
        ("/hooks", "显示已注册的钩子", False),
        ("/skills", "列出已加载的 Skill", False),
        ("/glob", "按 glob 规则搜索工作区文件路径", True),
        ("/grep", "搜索文件内容（可配置过滤）", True),
        ("/read", "读取文件内容", True),
        ("/file_read", "读取文件内容（别名）", True),
        ("/write", "写入文件（覆盖）", True),
        ("/file_write", "写入文件（覆盖）", True),
        ("/edit", "按文本替换更新文件", True),
        ("/file_edit", "按文本替换更新文件", True),
        ("/history", "查看历史会话列表", False),
        ("/memory", "记忆管理 (stats/search/clean/export)", True),
        ("/load", "加载指定会话 (支持编号选择)", True),
        ("/resume", "继续最近的对话", False),
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
        ("/worktree", "隔离执行区 — create/status/bind/keep/remove", True),
        ("/background", "后台任务 — run/status/list/cancel/output/cleanup", True),
        ("/schedule", "调度提醒 — create/list/cancel/pause/resume", True),
        ("/todo", "todo 清单 — list/add/start/done/pending/delete/clear", True),
        ("/team", "团队协议 — status/handoff/blocker/decision/request/result", True),
        ("/runtime", "运行时状态 — all/context/todo/team/subagent/hooks/resources", True),
        ("/self-review", "自我审查 — 扫描自身源码质量与架构", True),
        ("/reload", "热重载 — 重载模块无需重启", True),
        ("/evolve", "自我进化 — 反思循环修改自身工具代码并验证", True),
        ("/evolve-history", "查看自我进化历史记录", False),
        ("/forge", "工具锻造 — 自主生成新工具并注册", True),
        ("/forge-list", "列出所有已锻造的工具", False),
        ("/forge-remove", "移除已锻造的工具", True),
        ("/browse", "打开 URL 并显示 SoM 元素", True),
        ("/autobrowse", "自主浏览器任务", True),
        ("/browser-stop", "停止浏览器会话", False),
        ("/browser-state", "显示浏览器调试状态", False),
        ("/browser-screenshot", "截取当前页面截图", False),
        (
            "/bdaemon",
            "外部浏览器 daemon — start/health/run/list/status/watch/reply/resume/abort/manual",
            True,
        ),
        ("/tasks", "任务面板 — todo/subagent/background/browser", False),
        ("/task", "查看任务运行详情", True),
        ("/task-reply", "回复等待中的任务", True),
        ("/task-abort", "中止运行中的任务", True),
        ("/task-resume", "从手动控制中恢复任务", True),
        ("/scan", "快速安全扫描", True),
        ("/scan-full", "完整 25 模块安全扫描", True),
        ("/scan-report", "导出最新扫描报告 (json/sarif/html)", True),
        ("/scan-baseline", "保存扫描为基线", True),
        ("/btemplate-list", "列出浏览器任务模板", False),
        ("/btemplate-run", "从模板创建运行", True),
        ("/btemplate-compare", "比较模板运行结果", True),
        ("/new", "保存当前会话并开始新对话", False),
        ("/clear", "清除当前会话（不保存）", False),
        ("/quit", "退出", False),
        ("/exit", "退出", False),
    ]


COMMANDS: list[tuple[str, str, bool]] = list(_COMPLETER_COMMANDS)


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
            cmd_name = cmd.lstrip("/")
            matched = query == ""
            if not matched:
                try:
                    matched = re.search(query, cmd_name) is not None
                except re.error:
                    matched = query in cmd_name
            if matched:
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
