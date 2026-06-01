"""Tool discovery helpers and agent-facing search tool."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from naumi_agent.tools.base import Tool, ToolMetadata, ToolRegistry


@dataclass(frozen=True)
class ToolSearchMatch:
    name: str
    user_facing_name: str
    description: str
    score: int
    read_only: bool
    destructive: bool
    requires_confirmation: bool | None


def create_tool_search_tools(registry: ToolRegistry) -> list[Tool]:
    return [ToolSearchTool(registry)]


class ToolSearchTool(Tool):
    """Search currently registered tools by name, hint, and description."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "tool_search"

    @property
    def description(self) -> str:
        return (
            "搜索当前已注册工具。支持关键词查询，也支持 select:<工具名> "
            "直接选择一个或多个工具。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="搜索工具",
            search_hint="discover search select registered tools capabilities",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "搜索关键词。可使用 select:file_read,bash_run 直接选择工具；"
                        "可用 +term 表示必须匹配的词。"
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "最多返回多少个匹配工具，默认 5。",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        *,
        query: str,
        max_results: int = 5,
        **kwargs: Any,
    ) -> str:
        safe_limit = max(1, min(int(max_results or 5), 20))
        all_tools = [tool for tool in self._registry.all() if tool.name != self.name]
        if not query.strip():
            return "工具搜索失败：query 不能为空。"

        matches, missing = search_registered_tools(
            query=query,
            tools=all_tools,
            max_results=safe_limit,
        )
        return format_tool_search_result(
            query=query,
            matches=matches,
            missing=missing,
            total_tools=len(all_tools),
        )


def search_registered_tools(
    *,
    query: str,
    tools: list[Tool],
    max_results: int = 5,
) -> tuple[list[ToolSearchMatch], list[str]]:
    """Search tools with deterministic keyword scoring."""
    normalized_query = query.strip()
    direct = _parse_select_query(normalized_query)
    if direct is not None:
        found: list[ToolSearchMatch] = []
        missing: list[str] = []
        for requested_name in direct:
            tool = _find_tool(tools, requested_name)
            if tool is None:
                missing.append(requested_name)
                continue
            if all(match.name != tool.name for match in found):
                found.append(_match_from_tool(tool, score=100))
        return found[:max_results], missing

    required_terms, optional_terms = _split_query_terms(normalized_query)
    scoring_terms = [*required_terms, *optional_terms] if required_terms else optional_terms
    if not scoring_terms:
        return [], []

    matches: list[ToolSearchMatch] = []
    for tool in tools:
        haystack = _tool_search_text(tool)
        if required_terms and not all(term in haystack for term in required_terms):
            continue
        score = _score_tool(tool, scoring_terms)
        if score > 0:
            matches.append(_match_from_tool(tool, score=score))

    matches.sort(key=lambda item: (-item.score, item.name))
    return matches[:max_results], []


def format_tool_search_result(
    *,
    query: str,
    matches: list[ToolSearchMatch],
    missing: list[str],
    total_tools: int,
) -> str:
    lines = [
        f"工具搜索：`{query}`",
        f"已扫描 {total_tools} 个工具，匹配 {len(matches)} 个。",
    ]
    if missing:
        lines.append("未找到：" + "、".join(f"`{name}`" for name in missing))
    if not matches:
        lines.append("没有找到匹配工具。请换用更具体的能力词，例如 file、browser、memory、task。")
        return "\n".join(lines)

    lines.append("")
    for match in matches:
        traits: list[str] = []
        if match.read_only:
            traits.append("只读")
        if match.destructive:
            traits.append("会修改状态")
        if match.requires_confirmation:
            traits.append("需要确认")
        trait_text = f" [{', '.join(traits)}]" if traits else ""
        description = _single_line(match.description, max_chars=140)
        lines.append(
            f"- `{match.name}` — {match.user_facing_name}{trait_text}；{description}"
        )
    return "\n".join(lines)


def _parse_select_query(query: str) -> list[str] | None:
    match = re.match(r"^select:(.+)$", query, flags=re.IGNORECASE)
    if match is None:
        return None
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def _split_query_terms(query: str) -> tuple[list[str], list[str]]:
    required: list[str] = []
    optional: list[str] = []
    for raw in re.split(r"\s+", query.lower()):
        term = raw.strip()
        if not term:
            continue
        if term.startswith("+") and len(term) > 1:
            required.append(term[1:])
        else:
            optional.append(term)
    return required, optional


def _find_tool(tools: list[Tool], name: str) -> Tool | None:
    normalized = name.lower()
    for tool in tools:
        if tool.name.lower() == normalized:
            return tool
    for tool in tools:
        if _normalized_tool_name(tool.name) == _normalized_tool_name(name):
            return tool
    return None


def _score_tool(tool: Tool, terms: list[str]) -> int:
    name_parts = _tool_name_parts(tool.name)
    search_text = _tool_search_text(tool)
    score = 0
    for term in terms:
        if tool.name.lower() == term:
            score += 40
        if term in name_parts:
            score += 12
        elif any(term in part for part in name_parts):
            score += 6
        if term and term in tool.metadata.search_hint.lower():
            score += 5
        if term and term in tool.user_facing_name.lower():
            score += 4
        if term and term in tool.description.lower():
            score += 2
        if score == 0 and term in search_text:
            score += 1
    return score


def _match_from_tool(tool: Tool, *, score: int) -> ToolSearchMatch:
    metadata = tool.metadata
    return ToolSearchMatch(
        name=tool.name,
        user_facing_name=tool.user_facing_name,
        description=tool.description,
        score=score,
        read_only=metadata.read_only,
        destructive=metadata.destructive,
        requires_confirmation=metadata.requires_confirmation,
    )


def _tool_search_text(tool: Tool) -> str:
    return " ".join(
        [
            tool.name,
            _normalized_tool_name(tool.name),
            tool.user_facing_name,
            tool.description,
            tool.metadata.search_hint,
        ]
    ).lower()


def _tool_name_parts(name: str) -> set[str]:
    normalized = _normalized_tool_name(name)
    return {part for part in normalized.split(" ") if part}


def _normalized_tool_name(name: str) -> str:
    with_spaces = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return with_spaces.replace("__", " ").replace("_", " ").replace(".", " ").lower()


def _single_line(text: str, *, max_chars: int) -> str:
    line = " ".join(text.strip().split())
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 1].rstrip() + "…"
