"""Shared infrastructure for analysis tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".rb", ".rs", ".php",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
}


def resolve_target(target: str) -> list[Path]:
    """Resolve a file, directory, or glob-like target into source files."""
    p = Path(os.path.expanduser(target))
    if p.is_file():
        return [p]
    if p.is_dir():
        files = []
        for ext in SOURCE_EXTENSIONS:
            files.extend(p.rglob(f"*{ext}"))
        return sorted(files)[:200]
    return []


def read_sources(files: list[Path], max_chars: int = 80000) -> str:
    """Read source files into a single annotated text block."""
    parts: list[str] = []
    total = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        header = f"\n### {f}\n"
        if total + len(header) + len(content) > max_chars:
            remaining = max_chars - total
            if remaining > len(header) + 200:
                parts.append(header + content[: remaining - len(header)] + "\n... (truncated)")
            break
        parts.append(header + content)
        total += len(header) + len(content)
    return "".join(parts)


async def run_analysis(router: Any, system_prompt: str, user_msg: str) -> str:
    """Call the configured model router for analysis synthesis."""
    from naumi_agent.model.router import ModelTier

    try:
        response = await router.call(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            tier=ModelTier.CAPABLE,
            max_tokens=16384,
            temperature=1.0,
        )
        return response.content
    except Exception as e:
        return f"分析失败: {type(e).__name__}: {e}"


def router_unavailable(mode: str, target: str) -> str:
    """Return a user-facing message when analysis tools are not initialized."""
    return (
        f"⚠️ 分析工具尚未初始化（Router 未注入）。\n"
        f"模式: {mode}\n"
        f"目标: {target[:200]}\n\n"
        f"请在 Agent 启动后使用。"
    )
