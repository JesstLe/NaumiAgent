"""Deterministic high-confidence memory extraction."""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class MemoryCandidate:
    """A high-confidence memory candidate extracted from conversation text."""

    content: str
    category: str
    reason: str
    source_role: Literal["user", "assistant"]
    source_quote: str


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

_QUOTED_PATTERNS = tuple(re.compile(pattern, re.MULTILINE) for pattern in (
    r"```[\s\S]*?(?:```|\Z)",
    r"~~~[\s\S]*?(?:~~~|\Z)",
    r"(?<!`)`[^`\n]*(?:`|$)",
    r"“[^”]*(?:”|\Z)",
    r"‘[^’]*(?:’|\Z)",
    r'"(?:\\.|[^"\\])*(?:"|\Z)',
    r"(?<!\w)'(?:\\.|[^'\\])*(?:'|\Z)",
    r"^\s*>[^\n]*",
))
_NEGATED_PREFIX = re.compile(
    r"(?:不要|别把|不曾|不是|并非|否认|没有|尚未|从未|未曾|未经|不|没|未|"
    r"\b(?:not|never|don['’]t|doesn['’]t|didn['’]t|isn['’]t|wasn['’]t|"
    r"haven['’]t|hasn['’]t|without|deny|denied)\b)[^。.!?！？;；\n]{0,24}$",
    re.IGNORECASE,
)
_HYPOTHETICAL_PREFIX = re.compile(
    r"(?:假如|假设|如果|举例|例如|示例|比如|\b(?:if|suppose|example)\b)"
    r"[^。.!?！？;；\n]{0,24}$",
    re.IGNORECASE,
)


def extract_memory_candidates(
    user_text: str,
    assistant_text: str = "",
) -> list[MemoryCandidate]:
    """Propose attributed candidates; extraction never confirms memory truth."""
    if not isinstance(user_text, str) or not isinstance(assistant_text, str):
        raise TypeError("记忆提取的消息内容必须是文本")
    candidates: list[MemoryCandidate] = []
    seen: set[tuple[str, str, str]] = set()

    for role, original in (("user", user_text), ("assistant", assistant_text)):
        quoted = _quoted_ranges(original)
        for category, patterns in (
            ("preference", _PREFERENCE_PATTERNS),
            ("decision", _DECISION_PATTERNS),
            ("fact", _FACT_PATTERNS),
        ):
            for pattern, reason in patterns:
                for match in pattern.finditer(original):
                    if not _is_direct_statement(original, match, quoted):
                        continue
                    value = _clean_value(match.group("value"))
                    if not _is_useful_value(value):
                        continue
                    content = _format_memory_content(category, value, role)
                    key = (role, category, content)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(MemoryCandidate(
                        content=content,
                        category=category,
                        reason=reason,
                        source_role=role,
                        source_quote=match.group(0),
                    ))
                    if len(candidates) >= _MAX_CANDIDATES:
                        return candidates
    return candidates


def _quoted_ranges(original: str) -> list[tuple[int, int]]:
    spans = sorted(
        match.span() for pattern in _QUOTED_PATTERNS for match in pattern.finditer(original)
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _is_direct_statement(original: str, match: re.Match, quoted: list[tuple[int, int]]) -> bool:
    position = bisect_right(quoted, (match.start(), len(original) + 1)) - 1
    if position >= 0 and match.start() < quoted[position][1]:
        return False
    nearby = original[max(0, match.start() - 48):match.start()]
    prefix = re.split(r"[。.!?！？;；，,\n]", nearby)[-1]
    return not (_NEGATED_PREFIX.search(prefix) or _HYPOTHETICAL_PREFIX.search(prefix))


def _format_memory_content(category: str, value: str, role: str) -> str:
    if role == "assistant":
        label = {
            "preference": "助手建议",
            "decision": "助手提出的决策",
            "fact": "助手陈述的项目事实",
        }
        return f"{label[category]}：{value}"
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
