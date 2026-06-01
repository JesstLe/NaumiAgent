"""Deterministic high-confidence memory extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryCandidate:
    """A high-confidence memory candidate extracted from conversation text."""

    content: str
    category: str
    reason: str


_MAX_CANDIDATES = 3
_MAX_CONTENT_CHARS = 220

_PREFERENCE_PATTERNS = (
    (re.compile(r"(?:以后|后续|之后)请(?P<value>[^。.!?\n]{2,120})"), "user_request"),
    (re.compile(r"我(?:更)?(?:喜欢|偏好|希望)(?P<value>[^。.!?\n]{2,120})"), "preference"),
    (re.compile(r"(?:my preference is|i prefer)\s+(?P<value>[^.!?\n]{2,120})", re.I), "preference"),
)

_DECISION_PATTERNS = (
    (re.compile(r"(?:决定|确定|选定|采用|选择)(?P<value>[^。.!?\n]{2,120})"), "decision"),
    (re.compile(r"(?:we decided to|decision:\s*)(?P<value>[^.!?\n]{2,120})", re.I), "decision"),
)

_FACT_PATTERNS = (
    (re.compile(r"项目(?:使用|采用|基于)(?P<value>[^。.!?\n]{2,120})"), "project_fact"),
    (re.compile(r"(?:端口|默认端口|服务端口)(?:是|为|:)\s*(?P<value>\d{2,5})"), "project_fact"),
    (
        re.compile(
            r"(?:the project uses|this project uses)\s+(?P<value>[^.!?\n]{2,120})",
            re.I,
        ),
        "project_fact",
    ),
)


def extract_memory_candidates(
    user_text: str,
    assistant_text: str = "",
) -> list[MemoryCandidate]:
    """Extract high-confidence memory candidates without an LLM call."""
    candidates: list[MemoryCandidate] = []
    seen: set[tuple[str, str]] = set()

    combined = "\n".join(part.strip() for part in (user_text, assistant_text) if part.strip())
    for category, patterns in (
        ("preference", _PREFERENCE_PATTERNS),
        ("decision", _DECISION_PATTERNS),
        ("fact", _FACT_PATTERNS),
    ):
        for pattern, reason in patterns:
            for match in pattern.finditer(combined):
                value = _clean_value(match.group("value"))
                if not _is_useful_value(value):
                    continue
                content = _format_memory_content(category, value)
                key = (category, content)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(MemoryCandidate(
                    content=content,
                    category=category,
                    reason=reason,
                ))
                if len(candidates) >= _MAX_CANDIDATES:
                    return candidates
    return candidates


def _format_memory_content(category: str, value: str) -> str:
    if category == "preference":
        return f"用户偏好：{value}"
    if category == "decision":
        return f"已决定：{value}"
    return f"项目事实：{value}"


def _clean_value(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip(" ：:，,;；-"))
    return value[:_MAX_CONTENT_CHARS]


def _is_useful_value(value: str) -> bool:
    if len(value) < 2:
        return False
    weak = {"一下", "一下吧", "继续", "开始", "帮我", "处理"}
    return value not in weak
