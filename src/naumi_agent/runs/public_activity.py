"""Bounded public activity descriptions derived only from execution facts."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from naumi_agent.safety.guardrails import OutputGuardrail


def public_excerpt(value: Any, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    text = OutputGuardrail.redact(value)
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+|"
        r"(?:password|token|secret|api[_-]?key)\s*[=:]\s*)"
        r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;"']+)''',
        r"\1[已隐藏]",
        text,
    )
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_action(data: dict[str, Any]) -> str:
    name = str(data.get("name") or data.get("tool_name") or "工具")
    args = data.get("arguments", data.get("args", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            args = {}
    if not isinstance(args, dict):
        args = {}

    def field(*keys: str) -> str:
        return next((public_excerpt(args[k]) for k in keys if public_excerpt(args.get(k))), "")

    path = field("path", "file_path", "filename", "directory")
    if name in {"bash_run", "shell", "run_command", "exec_command"}:
        command = field("command", "cmd")
        directory = field("cwd", "workdir", "working_directory") or "工作目录"
        return f"在 {directory} 执行命令" + (f"：{command}" if command else "")
    if name in {"read", "file_read", "read_file"}:
        return "读取文件" + (f"：{path}" if path else "")
    if name in {"write", "file_write", "write_file", "file_edit", "edit", "apply_patch"}:
        return "修改文件" + (f"：{path}" if path else "")
    if name in {"glob", "grep", "search", "file_search"}:
        target = field("pattern", "query")
        return f"在 {path or '工作目录'} 搜索" + (f"：{target}" if target else "")
    if name in {"browser_observe", "browser_screenshot"}:
        return "查看页面元素与布局" if name == "browser_observe" else "截取页面，记录当前布局"
    if name == "browser_evaluate":
        return "在页面执行检查脚本"
    if name in {"browser_goto", "web_fetch", "fetch"}:
        try:
            url = urlsplit(str(args.get("url", "")))
            target = public_excerpt(urlunsplit((url.scheme, url.hostname or "", url.path, "", "")))
        except ValueError:
            target = ""
        return "访问页面" + (f"：{target}" if target else "")
    if name.startswith("browser_"):
        return "操作页面：" + public_excerpt(name)
    if name in {"task_create", "task_update", "goal_create", "goal_update", "delegate_task"}:
        target = field("subject", "objective", "description", "task")
        return "更新执行计划" + (f"：{target}" if target else "")
    return "调用工具：" + public_excerpt(name)


def progress_summary(event: str, data: dict[str, Any]) -> str:
    if event == "phase_summary":
        items = data.get("items")
        if not isinstance(items, (list, tuple)):
            return ""
        actions: list[str] = []
        failed = 0
        stopped = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            action = public_excerpt(item.get("action"), 160)
            if action and action not in actions:
                actions.append(action)
            status = str(item.get("status") or "").lower()
            if status in {"error", "failed", "denied", "aborted"}:
                failed += 1
            elif status in {"skipped", "cancelled"}:
                stopped += 1
        if not actions:
            return ""
        shown = actions[:3]
        suffix = f"；另有 {len(actions) - len(shown)} 项" if len(actions) > len(shown) else ""
        if failed:
            lead = f"本阶段执行存在 {failed} 项失败"
        elif stopped:
            lead = f"本阶段有 {stopped} 项未执行完成"
        else:
            lead = "本阶段已完成"
        return f"{lead}：{'；'.join(shown)}{suffix}。"
    if event == "context_compacted":
        before, after = data.get("before"), data.get("after")
        text = "已压缩上下文"
        if all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (before, after)):
            text += f"：{before:,} → {after:,} 条消息"
        archived = data.get("archived_tool_results")
        if isinstance(archived, int) and not isinstance(archived, bool) and archived > 0:
            text += f"；归档 {archived} 条工具结果"
        return text
    if event == "task_snapshot":
        items = data.get("items")
        if not isinstance(items, (list, tuple)):
            return ""
        labels = {"pending": "待处理", "in_progress": "进行中", "blocked": "受阻"}
        tasks = []
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            subject = public_excerpt(item.get("subject"), 100)
            if subject:
                state = labels.get(str(item.get("status")), "状态未确认")
                tasks.append(f"{state}：{subject}")
        count = data.get("completed_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            tasks.append(f"已完成 {count} 项")
        return "执行计划 · " + "；".join(tasks) if tasks else ""
    return ""
