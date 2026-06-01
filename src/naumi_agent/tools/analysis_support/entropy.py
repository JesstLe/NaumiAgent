"""Deterministic entropy reduction helpers."""

from __future__ import annotations

import re


def scan_entropy(source_text: str, conversation: str) -> str:
    """Measure repetition and context temperature for a reasoning chain."""
    findings: list[str] = []
    sentences = split_entropy_sentences(source_text + conversation)
    sentence_counts: dict[str, int] = {}
    for sentence in sentences:
        key = sentence[:50].lower()
        sentence_counts[key] = sentence_counts.get(key, 0) + 1
    repeated = sum(1 for count in sentence_counts.values() if count > 1)
    total = max(len(sentence_counts), 1)
    findings.append(f"- 语义重复率: {repeated / total:.1%}")
    if sentences:
        avg_len = sum(len(sentence) for sentence in sentences) / len(sentences)
        findings.append(f"- 平均句长: {avg_len:.0f} 字符")
    entropy_score = min(100, (repeated / total) * 100)
    temp = (
        "CRITICAL"
        if entropy_score > 60
        else "HIGH"
        if entropy_score > 35
        else "MEDIUM"
        if entropy_score > 15
        else "LOW"
    )
    findings.append(f"- 上下文温度: {entropy_score:.0f} ({temp})")
    return "\n".join(findings)


def split_entropy_sentences(text: str) -> list[str]:
    """Split mixed Chinese/English text into meaningful reasoning sentences."""
    sentences = re.split(r"[。！？.!?\n]+", text)
    return [sentence.strip() for sentence in sentences if len(sentence.strip()) > 5]


def build_entropy_anchor(context: str, goal: str = "") -> str:
    """Build a three-sentence anchor for restarting long reasoning chains."""
    sentences = dedupe_entropy_sentences(split_entropy_sentences(context))
    goal_text = (
        compact_entropy_sentence(goal)
        if goal.strip()
        else pick_entropy_sentence(
            sentences,
            ("目标", "任务", "objective", "goal", "实现", "修复", "对齐"),
            fallback="当前目标需要继续推进，但上下文中没有清晰的目标句。",
        )
    )
    facts_text = pick_entropy_sentence(
        sentences,
        ("通过", "passed", "验证", "已", "完成", "提交", "commit", "修复"),
        fallback="当前没有可确认的验证事实，应先回到可执行证据。",
    )
    remaining_text = pick_entropy_sentence(
        sentences,
        ("剩余", "下一", "需要", "待", "未", "失败", "todo", "pending", "继续"),
        fallback="下一步应选择最小可验证动作，并在完成后立即验证。",
    )
    return "\n".join(
        [
            "## 熵减锚点",
            f"1. 核心任务：{goal_text}",
            f"2. 已验证事实：{facts_text}",
            f"3. 剩余工作：{remaining_text}",
            "",
            "## 重启协议",
            "- 丢弃重复推理、历史死路和无证据猜测。",
            "- 只保留上面 3 句锚点作为下一步推理入口。",
            "- 下一步必须产出可验证动作或明确阻塞条件。",
        ]
    )


def dedupe_entropy_sentences(sentences: list[str]) -> list[str]:
    """Keep first occurrences while removing repeated reasoning fragments."""
    seen: set[str] = set()
    deduped: list[str] = []
    for sentence in sentences:
        key = re.sub(r"\s+", " ", sentence[:80].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(sentence)
    return deduped


def pick_entropy_sentence(
    sentences: list[str],
    keywords: tuple[str, ...],
    *,
    fallback: str,
) -> str:
    """Pick the most relevant compact sentence for an anchor slot."""
    if not sentences:
        return fallback
    scored: list[tuple[int, int, int, str]] = []
    for idx, sentence in enumerate(sentences):
        lower = sentence.lower()
        keyword_score = sum(1 for keyword in keywords if keyword.lower() in lower)
        length_score = min(len(sentence), 240) // 80
        scored.append((keyword_score, length_score, -idx, sentence))
    best = max(scored)
    if best[0] <= 0:
        return fallback
    return compact_entropy_sentence(best[3])


def compact_entropy_sentence(sentence: str, limit: int = 140) -> str:
    """Normalize whitespace and trim long anchor sentences."""
    compacted = re.sub(r"\s+", " ", sentence.strip())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 1].rstrip() + "…"
